"""Unit tests: making the AWS gateway host carry a PRA Network Tunnel.

AWS already satisfied the first of the three requirements and nobody noticed the other
two were missing, because a PROTOCOL tunnel needs none of them: the ECS task is
``networkMode: "host"``, so the container has always used the instance's real ENI. A
NETWORK tunnel additionally needs

  * ``SourceDestCheck=false`` on the host, or the VPC drops every packet it forwards
    on a leased address that is not its own primary;
  * the pool registered as SECONDARY private IPs on the DeviceIndex-0 ENI, or the
    fabric never answers ARP for a leased address and the agent refuses its own lease.

Pinned here:

  * ``subnet_cidr`` reads ``CidrBlock`` and is best-effort — a pool we cannot derive
    must never stop a gateway coming up, so it returns "" where its sibling
    ``subnet_availability_zone`` deliberately raises;
  * ``assign_secondary_private_ips`` is additive and idempotent, never re-assigns an
    address already on the ENI, and never touches the primary;
  * it uses ``AllowReassignment=False`` — stealing an address off a live host would be
    a far worse failure than having no network tunnel;
  * an ARM/boto failure is reported as an empty pool, NOT raised: every other Gateway
    function works without a pool.

Runs under pytest or standalone:  python tests/test_aws_jumpoint_tunnel_pool.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

try:
    from web_dashboard.services import aws_service as aws
except Exception as exc:  # pragma: no cover - boto3 absent
    print(f"SKIP: aws_service unavailable ({exc})")
    sys.exit(0)


try:
    from botocore.exceptions import ClientError as _ClientError  # noqa: F401
    _HAVE_BOTO = True
except Exception:  # pragma: no cover - boto3 absent on some dev machines
    _HAVE_BOTO = False


def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if sys.version_info < (3, 10) else asyncio.run(coro)


class _FakeEc2:
    """Only the four calls these helpers make."""

    def __init__(self, subnets=None, enis=None, raise_on=None):
        self._subnets = subnets if subnets is not None else []
        self._enis = enis if enis is not None else []
        self.raise_on = raise_on or ()
        self.assigned = []          # [(eni_id, [ips], allow_reassignment)]
        self.sdc = []               # [(instance_id, value)]

    def _maybe_raise(self, what):
        if what in self.raise_on:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "InvalidParameterValue",
                                         "Message": "nope"}}, what)

    __test_needs_boto__ = True

    def describe_subnets(self, SubnetIds=None, **kw):
        self._maybe_raise("describe_subnets")
        return {"Subnets": self._subnets}

    def describe_network_interfaces(self, NetworkInterfaceIds=None, **kw):
        self._maybe_raise("describe_network_interfaces")
        return {"NetworkInterfaces": self._enis}

    def assign_private_ip_addresses(self, NetworkInterfaceId=None,
                                    PrivateIpAddresses=None, AllowReassignment=None):
        self._maybe_raise("assign_private_ip_addresses")
        self.assigned.append((NetworkInterfaceId, list(PrivateIpAddresses or []),
                              AllowReassignment))
        return {}

    def modify_instance_attribute(self, InstanceId=None, SourceDestCheck=None):
        self._maybe_raise("modify_instance_attribute")
        self.sdc.append((InstanceId, (SourceDestCheck or {}).get("Value")))
        return {}


class _patched:
    """Swap aws_service._get_ec2 for a fake, restore on exit."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self._orig = aws._get_ec2
        aws._get_ec2 = lambda region: self.fake
        return self.fake

    def __exit__(self, *a):
        aws._get_ec2 = self._orig
        return False


def _eni(*ips, primary="10.99.5.4"):
    addrs = [{"PrivateIpAddress": primary, "Primary": True}]
    addrs += [{"PrivateIpAddress": ip, "Primary": False} for ip in ips]
    return [{"NetworkInterfaceId": "eni-1", "PrivateIpAddresses": addrs}]


# ── subnet_cidr ──────────────────────────────────────────────────────────────

def test_subnet_cidr_reads_the_cidr_block():
    with _patched(_FakeEc2(subnets=[{"CidrBlock": "10.99.5.0/24"}])):
        assert _run_async(aws.subnet_cidr("us-east-1", "subnet-abc")) == "10.99.5.0/24"


def test_subnet_cidr_on_a_missing_subnet_is_blank_not_an_exception():
    with _patched(_FakeEc2(subnets=[])):
        assert _run_async(aws.subnet_cidr("us-east-1", "subnet-gone")) == ""


def test_subnet_cidr_swallows_an_api_error():
    if not _HAVE_BOTO:
        return   # boto3 absent: the except clauses cannot be evaluated at all
    # Deliberately unlike subnet_availability_zone, which raises: a missing AZ makes
    # an unmountable volume, a missing CIDR just means no network tunnel.
    with _patched(_FakeEc2(raise_on=("describe_subnets",))):
        assert _run_async(aws.subnet_cidr("us-east-1", "subnet-abc")) == ""


# ── assign_secondary_private_ips ─────────────────────────────────────────────

def test_assign_adds_only_the_missing_addresses():
    fake = _FakeEc2(enis=_eni("10.99.5.247"))
    with _patched(fake):
        got = _run_async(aws.assign_secondary_private_ips(
            "us-east-1", "eni-1", ["10.99.5.247", "10.99.5.248"]))
    assert got == ["10.99.5.247", "10.99.5.248"]
    assert len(fake.assigned) == 1
    assert fake.assigned[0][1] == ["10.99.5.248"], "already-present .247 re-assigned"


def test_assign_never_offers_the_primary_address():
    fake = _FakeEc2(enis=_eni(primary="10.99.5.4"))
    with _patched(fake):
        _run_async(aws.assign_secondary_private_ips("us-east-1", "eni-1", ["10.99.5.254"]))
    assert "10.99.5.4" not in fake.assigned[0][1]


def test_assign_is_a_noop_when_everything_is_already_registered():
    fake = _FakeEc2(enis=_eni("10.99.5.253", "10.99.5.254"))
    with _patched(fake):
        got = _run_async(aws.assign_secondary_private_ips(
            "us-east-1", "eni-1", ["10.99.5.253", "10.99.5.254"]))
    assert got == ["10.99.5.253", "10.99.5.254"]
    assert fake.assigned == [], "wrote to ARM with nothing to add"


def test_assign_refuses_to_steal_an_address_off_a_live_host():
    fake = _FakeEc2(enis=_eni())
    with _patched(fake):
        _run_async(aws.assign_secondary_private_ips("us-east-1", "eni-1", ["10.99.5.254"]))
    assert fake.assigned[0][2] is False, "AllowReassignment must stay False"


def test_assign_with_an_empty_pool_does_nothing():
    fake = _FakeEc2(enis=_eni())
    with _patched(fake):
        assert _run_async(aws.assign_secondary_private_ips("us-east-1", "eni-1", [])) == []
    assert fake.assigned == []


def test_assign_reports_empty_when_the_api_refuses_rather_than_raising():
    if not _HAVE_BOTO:
        return   # boto3 absent: the except clauses cannot be evaluated at all
    fake = _FakeEc2(enis=_eni(), raise_on=("assign_private_ip_addresses",))
    with _patched(fake):
        assert _run_async(aws.assign_secondary_private_ips(
            "us-east-1", "eni-1", ["10.99.5.254"])) == []


def test_assign_reports_empty_when_the_eni_cannot_be_read():
    if not _HAVE_BOTO:
        return   # boto3 absent: the except clauses cannot be evaluated at all
    fake = _FakeEc2(raise_on=("describe_network_interfaces",))
    with _patched(fake):
        assert _run_async(aws.assign_secondary_private_ips(
            "us-east-1", "eni-1", ["10.99.5.254"])) == []


def test_assign_on_an_unknown_eni_is_empty():
    with _patched(_FakeEc2(enis=[])):
        assert _run_async(aws.assign_secondary_private_ips(
            "us-east-1", "eni-nope", ["10.99.5.254"])) == []


# ── source/dest check, the other half ────────────────────────────────────────

def test_source_dest_check_is_cleared_with_a_false_value():
    fake = _FakeEc2()
    with _patched(fake):
        _run_async(aws.set_source_dest_check("us-east-1", "i-123", False))
    assert fake.sdc == [("i-123", False)]


# ── the wiring: all three gateway return paths apply the pool ────────────────

def test_every_gateway_return_path_applies_the_pool():
    """Source-level guard. The AWS ensure path has THREE returns — reuse, lost-race
    and create — and Azure's equivalent deliberately applies on reuse too so a gateway
    built before this existed picks the pool up without a rebuild. A new early return
    that forgets the call is invisible at runtime: the gateway comes up fine and only
    network tunnels break."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "jumpoint_host_service.py"), encoding="utf-8").read()
    start = src.index("async def _ensure_jumpoint_host_aws")
    body = src[start:src.index("\nasync def ", start + 10)]
    assert body.count("_ensure_aws_tunnel_pool(") == 3, (
        "every return path of _ensure_jumpoint_host_aws must apply the tunnel pool; "
        f"found {body.count('_ensure_aws_tunnel_pool(')}")
    # Anchor on the statements that return a HOST ID (the `return None` guards for the
    # FARGATE branch and a missing deploy key never reach a host).
    lines = body.splitlines()
    id_returns = [i for i, ln in enumerate(lines)
                  if ln.strip().startswith(("return existing[", "return recheck[",
                                            "return host_id"))]
    assert len(id_returns) == 3, f"expected 3 host-id returns, found {len(id_returns)}"
    for i in id_returns:
        near = "\n".join(lines[max(0, i - 10):i])
        assert "_ensure_aws_tunnel_pool(" in near, (
            f"the host-id return at offset {i} ({lines[i].strip()!r}) is not preceded "
            "by _ensure_aws_tunnel_pool — that gateway silently loses network tunnels")


def test_the_pool_helper_prefers_the_region_config_over_the_flat_key():
    """A flat pool is the wrong answer in every region but the default — it would be
    outside that region's subnet prefix entirely."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "jumpoint_host_service.py"), encoding="utf-8").read()
    start = src.index("async def _ensure_aws_tunnel_pool")
    body = src[start:src.index("\nasync def ", start + 10)]
    per_region = body.index('rc.get("jumpoint_tunnel_pool")')
    flat = body.index('_cfg("bt_ecs_jumpoint_tunnel_pool")')
    assert per_region < flat, "the flat key must be the FALLBACK, not read first"


def _run():
    mod = sys.modules[__name__]
    fns = [getattr(mod, n) for n in dir(mod) if n.startswith("test_")]
    failed = 0
    for fn in sorted(fns, key=lambda f: f.__name__):
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
