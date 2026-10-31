"""On-demand Azure VM egress (services/azure_nat_service.py, the vm-egress section of
services/azure_service.py): a NAT gateway and an NSG allow that exist only while a VM does.

What has to hold, and what each test pins:

  * **It acts only where egress is closed.** The sandbox's NSG denies the internet under
    a VNet allow; a private subnet has no default outbound path. Either opens it. A subnet
    that already reaches the internet, or that has a NAT gateway of its own, is untouched.
  * **The allow outranks the deny and nothing else it need not,** takes a free priority,
    is scoped to the subnet's range, and never edits the sandbox's own rules.
  * **A second ensure re-asserts rather than reading its own allow as "open".**
  * **Release waits for the subnet to empty and for the grace window,** closes the NSGs
    first, and deletes the gateway and its address only when no subnet still uses them.
    It never releases a gateway it did not tag.
  * **Every Azure VM create opens egress first, and every delete releases it after,**
    and none of it is attempted while the setting is off.

Runs under pytest, or standalone:  python tests/test_azure_vm_nat.py
"""
import asyncio
import os
import sys
import time
import types
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)


class _Model(SimpleNamespace):
    """Stands in for an azure.mgmt.network model: keyword arguments become attributes,
    and, as on the SDK's models, a field never set reads as None."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return None


def _stub_network_models():
    """The SDK is not installed here; the code under test only constructs these."""
    names = ("NatGateway", "NatGatewaySku", "SecurityRule", "SubResource",
             "PublicIPAddress", "PublicIPAddressSku")
    try:
        import azure.mgmt.network.models  # noqa: F401
        return
    except ImportError:
        pass
    for pkg in ("azure", "azure.mgmt", "azure.mgmt.network"):
        sys.modules.setdefault(pkg, types.ModuleType(pkg))
    models = types.ModuleType("azure.mgmt.network.models")
    for n in names:
        setattr(models, n, type(n, (_Model,), {}))
    sys.modules["azure.mgmt.network.models"] = models


_stub_network_models()
from web_dashboard.services import azure_nat_service, azure_service as az  # noqa: E402
from web_dashboard.services import config_service  # noqa: E402

if not getattr(az, "_azure_available", False):
    _m = sys.modules["azure.mgmt.network.models"]
    az.PublicIPAddress, az.PublicIPAddressSku = _m.PublicIPAddress, _m.PublicIPAddressSku

    class _NotFound(Exception):
        pass
    az.ResourceNotFoundError = _NotFound

SUB = "00000000-0000-0000-0000-000000000000"
RG = "dashboard-sandbox-rg"
VNET = "dashboard-sandbox-vnet"


def _sid(name):
    return (f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Network/"
            f"virtualNetworks/{VNET}/subnets/{name}")


def _nsg_id(name):
    return (f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Network/"
            f"networkSecurityGroups/{name}")


def _nat_id(name):
    return (f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Network/"
            f"natGateways/{name}")


def _rule(name, priority, access, dest="Internet", port="*", direction="Outbound"):
    return _Model(name=name, priority=priority, access=access, direction=direction,
                  protocol="*", destination_address_prefix=dest,
                  destination_address_prefixes=[], destination_port_range=port,
                  destination_port_ranges=[])


class _Poller:
    def __init__(self, value=None):
        self.value = value

    def result(self):
        return self.value


class FakeArm:
    """An in-memory slice of ARM's network API, as much as the egress code touches."""

    def __init__(self):
        self.subnets, self.nsgs, self.nats, self.pips = {}, {}, {}, {}
        self.calls = []
        arm = self

        class _Subnets:
            def get(self, rg, vnet, name):
                return arm.subnets[name]

            def begin_create_or_update(self, rg, vnet, name, subnet):
                arm.calls.append(("subnet", name, bool(subnet.nat_gateway)))
                arm.subnets[name] = subnet
                for nat in arm.nats.values():
                    nat.subnets = [s for s in nat.subnets if s.id != subnet.id]
                    if subnet.nat_gateway and subnet.nat_gateway.id == nat.id:
                        nat.subnets.append(_Model(id=subnet.id))
                return _Poller(subnet)

        class _Nsgs:
            def get(self, rg, name):
                return arm.nsgs[name]

        class _Rules:
            def begin_create_or_update(self, rg, nsg, name, rule):
                arm.calls.append(("rule+", nsg, name, rule.priority))
                rule.name, rule.direction = name, "Outbound"
                rules = [r for r in arm.nsgs[nsg].security_rules if r.name != name]
                arm.nsgs[nsg].security_rules = rules + [rule]
                return _Poller(rule)

            def begin_delete(self, rg, nsg, name):
                before = arm.nsgs[nsg].security_rules
                after = [r for r in before if r.name != name]
                if len(after) == len(before):
                    raise az.ResourceNotFoundError("no such rule")
                arm.calls.append(("rule-", nsg, name))
                arm.nsgs[nsg].security_rules = after
                return _Poller()

        class _Vnets:
            def get(self, rg, name):
                return _Model(location="centralus")

        class _Pips:
            def begin_create_or_update(self, rg, name, pip):
                pip.id, pip.ip_address = f"/pip/{name}", "20.1.2.3"
                arm.pips[name] = pip
                return _Poller(pip)

            def begin_delete(self, rg, name):
                arm.calls.append(("pip-", name))
                arm.pips.pop(name)
                return _Poller()

        class _Nats:
            def get(self, rg, name):
                if name not in arm.nats:
                    raise az.ResourceNotFoundError(name)
                return arm.nats[name]

            def begin_create_or_update(self, rg, name, nat):
                nat.id, nat.name = _nat_id(name), name
                nat.subnets = arm.nats[name].subnets if name in arm.nats else []
                arm.nats[name] = nat
                return _Poller(nat)

            def begin_delete(self, rg, name):
                arm.calls.append(("nat-", name))
                arm.nats.pop(name)
                return _Poller()

            def list_all(self):
                return list(arm.nats.values())

        self.subnets_api = _Subnets()
        self.client = _Model(subnets=_Subnets(), network_security_groups=_Nsgs(),
                             security_rules=_Rules(), virtual_networks=_Vnets(),
                             public_ip_addresses=_Pips(), nat_gateways=_Nats())

    def add_subnet(self, name, prefix, nsg=None, nat=None, nics=0, default_outbound=None):
        self.subnets[name] = _Model(
            id=_sid(name), name=name, address_prefix=prefix, address_prefixes=[],
            network_security_group=_Model(id=_nsg_id(nsg)) if nsg else None,
            nat_gateway=_Model(id=nat) if nat else None,
            ip_configurations=[_Model(id=f"ipc{i}") for i in range(nics)],
            default_outbound_access=default_outbound)

    def add_nsg(self, name, rules):
        self.nsgs[name] = _Model(id=_nsg_id(name), name=name, security_rules=list(rules))


def _sandbox():
    arm = FakeArm()
    arm.add_nsg("vm-nsg", [_rule("allow-vnet-out", 100, "Allow", dest="VirtualNetwork"),
                           _rule("deny-internet-out", 200, "Deny"),
                           _rule("allow-vnet-in", 100, "Allow", direction="Inbound")])
    arm.add_subnet("vm-subnet", "10.99.2.0/24", nsg="vm-nsg")
    return arm


def _ensure(arm, subnet="vm-subnet", nic_nsgs=()):
    return az._ensure_vm_egress_sync(None, SUB, _sid(subnet), list(nic_nsgs))


def _release(arm, subnet="vm-subnet", grace_s=0):
    return az._release_vm_egress_sync(None, SUB, _sid(subnet), grace_s)


class _Patch:
    def __init__(self, *triples):
        self.triples, self.saved = triples, []

    def __enter__(self):
        for mod, name, value in self.triples:
            self.saved.append((mod, name, getattr(mod, name)))
            setattr(mod, name, value)
        return self

    def __exit__(self, *exc):
        for mod, name, value in reversed(self.saved):
            setattr(mod, name, value)


def _with(arm):
    return _Patch((az, "_get_network", lambda cred, sub: arm.client))


# ── deciding whether a subnet is closed ──────────────────────────────────────

def test_the_sandbox_nsg_reads_as_closed_and_a_plain_one_as_open():
    arm = _sandbox()
    assert az._egress_verdict(arm.nsgs["vm-nsg"]) == ("deny", 200)
    arm.add_nsg("open", [_rule("allow-vnet-out", 100, "Allow", dest="VirtualNetwork")])
    assert az._egress_verdict(arm.nsgs["open"]) == ("allow", None)
    # A deny that does not cover HTTPS does not close the subnet.
    arm.add_nsg("smtp", [_rule("deny-smtp", 150, "Deny", port="25")])
    assert az._egress_verdict(arm.nsgs["smtp"]) == ("allow", None)
    # An allow above the deny wins.
    arm.add_nsg("https", [_rule("allow-https", 150, "Allow", port="443"),
                          _rule("deny-all", 200, "Deny")])
    assert az._egress_verdict(arm.nsgs["https"]) == ("allow", None)


def test_the_allow_takes_the_highest_free_priority_under_the_deny():
    arm = FakeArm()
    arm.add_nsg("n", [_rule("a", 199, "Allow", dest="VirtualNetwork"),
                      _rule("deny", 200, "Deny"),
                      _rule("in", 198, "Allow", direction="Inbound")])
    # 199 is taken outbound; an inbound rule's priority does not collide.
    assert az._free_priority_below(arm.nsgs["n"], 200, "ours") == 198
    arm.add_nsg("full", [_rule("deny", 100, "Deny")])
    assert az._free_priority_below(arm.nsgs["full"], 100, "ours") is None


# ── ensure ───────────────────────────────────────────────────────────────────

def test_ensure_opens_the_sandbox_subnet_without_editing_its_rules():
    arm = _sandbox()
    with _with(arm):
        res = _ensure(arm)
    assert res["status"] == "ready" and res["attached"] is True
    nat = arm.nats["dashboard-vm-nat-dashboard-sandbox-vnet"]
    assert arm.subnets["vm-subnet"].nat_gateway.id == nat.id
    assert nat.tags["managed-by"] == "vm-dashboard" and nat.tags["purpose"] == "vm-egress"
    assert any(v == _nsg_id("vm-nsg") for k, v in nat.tags.items() if k.startswith("egress-nsg-"))
    assert arm.pips["dashboard-vm-nat-dashboard-sandbox-vnet-pip"].sku.name == "Standard"
    rules = {r.name: r for r in arm.nsgs["vm-nsg"].security_rules}
    allow = rules["dashboard-vm-egress-vm-subnet"]
    assert (allow.priority, allow.access, allow.destination_address_prefix) == (199, "Allow", "Internet")
    assert allow.source_address_prefixes == ["10.99.2.0/24"]
    # Web traffic only: the sandbox's deny still closes every other port.
    assert allow.protocol == "Tcp" and list(allow.destination_port_ranges) == ["80", "443"]
    assert not getattr(allow, "destination_port_range", None)
    # The sandbox's own rules are exactly as they were.
    assert rules["deny-internet-out"].priority == 200 and rules["allow-vnet-out"].priority == 100


def test_a_second_ensure_reasserts_and_does_not_reattach():
    arm = _sandbox()
    with _with(arm):
        _ensure(arm)
        arm.calls.clear()
        res = _ensure(arm)
    assert res["status"] == "ready" and res["attached"] is False
    assert not [c for c in arm.calls if c[0] == "subnet"]
    # Same rule, same priority: it was recognised as ours, not stacked.
    names = [r.name for r in arm.nsgs["vm-nsg"].security_rules]
    assert names.count("dashboard-vm-egress-vm-subnet") == 1
    assert ("rule+", "vm-nsg", "dashboard-vm-egress-vm-subnet", 199) in arm.calls


def test_a_subnet_that_already_reaches_the_internet_is_left_alone():
    arm = FakeArm()
    arm.add_nsg("open", [_rule("allow-vnet-out", 100, "Allow", dest="VirtualNetwork")])
    arm.add_subnet("open-subnet", "10.0.1.0/24", nsg="open")
    arm.add_subnet("bare", "10.0.2.0/24")
    with _with(arm):
        assert _ensure(arm, "open-subnet")["status"] == "skipped"
        assert _ensure(arm, "bare")["status"] == "skipped"
    assert not arm.nats and not arm.pips and not arm.calls


def test_a_subnet_with_its_own_nat_gateway_is_left_alone():
    arm = _sandbox()
    arm.add_subnet("vm-subnet", "10.99.2.0/24", nsg="vm-nsg", nat=_nat_id("corp-nat"))
    with _with(arm):
        res = _ensure(arm)
    assert res["status"] == "skipped" and "its own NAT gateway" in res["reason"]
    assert not arm.nats and not arm.calls


def test_a_private_subnet_gets_a_gateway_and_no_rule():
    arm = FakeArm()
    arm.add_subnet("private", "10.0.5.0/24", default_outbound=False)
    with _with(arm):
        res = _ensure(arm, "private")
    assert res["status"] == "ready" and res["nsg_rules"] == []
    assert arm.subnets["private"].nat_gateway is not None


def test_a_nic_nsg_that_denies_is_opened_too():
    arm = FakeArm()
    arm.add_subnet("vm-subnet", "10.99.2.0/24")
    arm.add_nsg("nic-nsg", [_rule("deny-internet-out", 300, "Deny")])
    with _with(arm):
        res = _ensure(arm, nic_nsgs=[_nsg_id("nic-nsg")])
    assert res["nsg_rules"] == ["nic-nsg:299"]


# ── release ──────────────────────────────────────────────────────────────────

def test_release_waits_for_the_subnet_to_empty():
    arm = _sandbox()
    with _with(arm):
        _ensure(arm)
        arm.subnets["vm-subnet"].ip_configurations = [_Model(id="nic")]
        assert _release(arm).startswith("in use")
    assert arm.nats and arm.subnets["vm-subnet"].nat_gateway


def test_release_defers_inside_the_grace_window():
    arm = _sandbox()
    with _with(arm):
        _ensure(arm)
        assert _release(arm, grace_s=600).startswith("ensured recently")
        nat = arm.nats["dashboard-vm-nat-dashboard-sandbox-vnet"]
        nat.tags["egress-ensured-at"] = str(int(time.time()) - 601)
        assert _release(arm, grace_s=600) == "released"


def test_release_closes_the_nsg_first_then_removes_gateway_and_address():
    arm = _sandbox()
    with _with(arm):
        _ensure(arm)
        arm.calls.clear()
        assert _release(arm) == "released"
    kinds = [c[0] for c in arm.calls]
    assert kinds == ["rule-", "subnet", "nat-", "pip-"], kinds
    assert not arm.nats and not arm.pips
    assert arm.subnets["vm-subnet"].nat_gateway is None
    assert [r.name for r in arm.nsgs["vm-nsg"].security_rules] == [
        "allow-vnet-out", "deny-internet-out", "allow-vnet-in"]


def test_a_gateway_serving_another_subnet_survives_one_release():
    arm = _sandbox()
    arm.add_subnet("desktops", "10.99.6.0/24", nsg="vm-nsg")
    with _with(arm):
        _ensure(arm, "vm-subnet")
        # vm-subnet's allow is scoped to vm-subnet, so desktops still reads as closed.
        assert _ensure(arm, "desktops")["nsg_rules"] == ["vm-nsg:198"]
        assert _release(arm, "vm-subnet").startswith("detached")
        # Only this subnet's rule went; the other subnet's is still there.
        names = [r.name for r in arm.nsgs["vm-nsg"].security_rules]
        assert "dashboard-vm-egress-desktops" in names
        assert "dashboard-vm-egress-vm-subnet" not in names
        assert _release(arm, "desktops") == "released"
    assert not arm.nats


def test_release_never_touches_a_gateway_it_did_not_tag():
    arm = _sandbox()
    with _with(arm):
        _ensure(arm)
        arm.nats["dashboard-vm-nat-dashboard-sandbox-vnet"].tags = {"owner": "someone"}
        assert "not tagged as ours" in _release(arm)
    assert arm.nats


# ── the policy and the hooks ─────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_nothing_is_attempted_while_the_setting_is_off():
    async def boom(*a, **k):
        raise AssertionError("called the cloud with the feature off")
    with _Patch((config_service, "get_bool", lambda key: False),
                (az, "ensure_vm_egress", boom), (az, "release_vm_egress", boom),
                (az, "list_vm_egress_nats", boom)):
        assert _run(azure_nat_service.ensure(_sid("vm-subnet"))) is None
        assert _run(azure_nat_service.release(_sid("vm-subnet"))) == ""
        assert _run(azure_nat_service.reclaim_once()) == 0


def test_a_failed_ensure_never_fails_the_deploy():
    async def broken(*a, **k):
        raise az.AzureError("AuthorizationFailed")
    with _Patch((config_service, "get_bool", lambda key: key == azure_nat_service.ENABLED),
                (az, "ensure_vm_egress", broken)):
        assert _run(azure_nat_service.ensure(_sid("vm-subnet"))) is None


def test_deploy_opens_egress_before_the_vm_and_delete_releases_after():
    order = []

    async def ensure(subnet_id, nsg_ids=None):
        order.append(("ensure", subnet_id, list(nsg_ids or [])))
        return {"status": "ready", "nat_gateway": "dashboard-vm-nat-x"}

    async def release(subnet_id, *, grace_s=600):
        order.append(("release", subnet_id))
        return "released"

    async def creds():
        return None, SUB

    async def vm_subnet(rg, name):
        return _sid("vm-subnet")

    def deploy_sync(*a, **k):
        order.append(("create",))
        return {"vm_name": "v"}

    def terminate_sync(*a, **k):
        order.append(("delete",))

    with _Patch((azure_nat_service, "ensure", ensure), (azure_nat_service, "release", release),
                (azure_nat_service, "enabled", lambda: True),
                (az, "_ensure_creds", creds), (az, "_deploy_vm_sync", deploy_sync),
                (az, "_terminate_vm_sync", terminate_sync), (az, "vm_subnet", vm_subnet)):
        out = _run(az.deploy_vm("rg", "centralus", "v", "Standard_B2s", "", _sid("vm-subnet"),
                                [_nsg_id("a"), _nsg_id("b")], False, "u", "key"))
        _run(az.terminate_vm("rg", "v"))
    assert out["egress_nat_gateway"] == "dashboard-vm-nat-x"
    # Only the NIC's NSG (the first) is applied to the NIC, so only it is considered.
    assert order == [("ensure", _sid("vm-subnet"), [_nsg_id("a")]), ("create",),
                     ("delete",), ("release", _sid("vm-subnet"))]


def test_a_container_node_opens_and_releases_its_subnet():
    order = []

    async def ensure(subnet_id, nsg_ids=None):
        order.append("ensure")

    async def release(subnet_id, *, grace_s=600):
        order.append(("release", subnet_id))

    async def creds():
        return None, SUB

    with _Patch((azure_nat_service, "ensure", ensure), (azure_nat_service, "release", release),
                (azure_nat_service, "enabled", lambda: True), (az, "_ensure_creds", creds),
                (az, "_run_vm_container_node_sync", lambda *a: order.append("create") or {}),
                (az, "_stop_vm_container_node_sync", lambda *a: order.append("delete")),
                (az, "_get_network", lambda c, s: None),
                (az, "_subnet_of_nic_sync", lambda net, rg, nic: _sid("vm-subnet"))):
        _run(az.run_vm_container_node("rg", "centralus", subnet_id=_sid("vm-subnet"),
                                      name="agent", vm_size="Standard_B2s",
                                      admin_password="p", cloud_init_b64="", public_ip=False))
        _run(az.stop_vm_container_node("rg", "agent"))
    assert order == ["ensure", "create", "delete", ("release", _sid("vm-subnet"))]


def test_the_worker_runs_the_reclaim_loop():
    with open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py"), encoding="utf-8") as f:
        src = f.read()
    assert "azure_nat_service.reclaim_loop()" in src


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
