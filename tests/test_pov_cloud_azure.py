"""The Azure POV driver: the four decisions that cost real money if they regress.

Azure maps onto this feature more naturally than AWS — a resource group IS an environment,
so teardown is one call rather than six resource types unpicked in dependency order. What
it does NOT give you free is any of the following, and each one fails quietly rather than
loudly:

  * **deallocate, not power off** — `begin_power_off` leaves the VM "Stopped" and still
    billing for compute, which a nightly suspend schedule would do to every POV;
  * **static private addresses** — a deallocated VM with a dynamic one can come back on a
    different address, silently invalidating the PRA jump item, the Password Safe managed
    system and the Entitle integration the wire-up wrote;
  * **base64 custom_data** — the SDK passes it through untouched, so plain text gives a VM
    that boots fine and never runs its bootstrap;
  * **an admin account at creation** — Azure has no equivalent of launching an AMI with no
    key, which is why this is the one cloud whose `stored_credentials` capability is True.

No Azure SDK and no network: the parts that need ARM models are pinned at the source, and
the parts that do not are called directly.

Runs under pytest, or standalone:
    python tests/test_pov_cloud_azure.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-cloud-azure")

from web_dashboard.services import lab_platforms as lp  # noqa: E402
from web_dashboard.services import pov_cloud_azure as az  # noqa: E402
from web_dashboard.services import pov_cloud_env  # noqa: E402

_SRC = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_azure.py"),
            encoding="utf-8").read()


def _body(fn: str) -> str:
    return _SRC.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]


def _code(fn: str) -> str:
    """A function body with its comments stripped.

    Needed because this driver's comments NAME the calls it deliberately does not make —
    "deallocate, never begin_power_off" — and an assertion scanning raw source would read
    the warning as the bug it warns about, and then pass again on the day somebody
    actually made it.
    """
    kept = []
    for line in _body(fn).splitlines():
        bare = line.split("#", 1)[0]
        if bare.strip():
            kept.append(bare)
    return "\n".join(kept)


# ── the registry entry ───────────────────────────────────────────────────────

def test_azure_is_a_built_cloud_with_an_adapter_and_a_driver():
    assert "azure" in lp.CLOUD_PLATFORMS
    assert "azure" in pov_cloud_env._DRIVER_MODULE
    adapter = lp.adapter("azure")
    for fn in lp.READ_CONTRACT:
        assert callable(getattr(adapter, fn, None)), f"azure adapter lacks {fn}"


def test_azure_claims_a_platform_login_and_aws_does_not():
    """Not an inconsistency to tidy away. Azure's `os_profile` REQUIRES an admin account
    at creation, so a POV built there has a login whether anybody wanted one or not — and
    the capability table exists to say exactly that kind of thing out loud."""
    assert lp.supports("azure", "stored_credentials") is True
    assert lp.supports("aws", "stored_credentials") is False
    assert callable(getattr(lp.adapter("azure"), "stored_credentials", None))


def test_azure_has_no_idle_timer_and_therefore_has_a_schedule():
    assert lp.supports("azure", "idle_suspend") is False
    assert lp.supports("azure", "scheduled_suspend") is True


def test_azure_offers_no_share_link_so_the_ui_says_pra_only():
    assert lp.supports("azure", "share_link") is False


# ── the four expensive mistakes ──────────────────────────────────────────────

def test_suspending_deallocates_and_never_merely_powers_off():
    """`begin_power_off` leaves the VM "Stopped" and still billing for compute. The
    schedule would do it to every POV every night while the page reported it suspended."""
    body = _code("_power_sync")
    assert "begin_deallocate" in body, "the suspend path does not deallocate"
    assert "begin_power_off" not in body, (
        "the suspend path calls begin_power_off, which stops the VM and keeps billing "
        "for its compute")


def test_private_addresses_are_static():
    """A deallocated VM with a dynamic private address can return on a different one, and
    the wire-up has written the old one into a PRA jump item, a Password Safe managed
    system and an Entitle integration."""
    body = _code("_create_vms_sync")
    assert '"private_ip_allocation_method": "Static"' in body, (
        "private addresses are not static, so every scheduled suspend can silently "
        "invalidate the whole PAM wire-up")
    assert "_next_free_ip(" in body, "static allocation with no address chosen"


def test_custom_data_is_base64_because_the_sdk_does_not_encode_it():
    """Plain text here gives a VM that boots fine and never runs its bootstrap — the same
    silent failure `cloud_init` earns its own capability value to avoid."""
    assert az._custom_data("") is None, "an empty payload must be omitted, not encoded"
    encoded = az._custom_data("#cloud-config\nruncmd: [echo, hi]\n")
    import base64
    assert base64.b64decode(encoded).decode() == "#cloud-config\nruncmd: [echo, hi]\n"
    assert "custom_data=_custom_data(" in _body("_create_vms_sync"), \
        "the create path passes custom_data without encoding it"


def test_only_the_broker_receives_custom_data():
    """A target carrying the enrolment code would enrol a second agent for the POV. The
    shared `vm_specs` puts the payload only on the broker; this pins that the driver does
    not go looking for it anywhere else."""
    body = _code("_create_vms_sync")
    assert body.count("user_data") == 1, \
        "the driver reads user_data more than once — one of them is not the broker's"


# ── the resource group is the environment ────────────────────────────────────

def test_teardown_is_one_call_on_the_resource_group():
    """The whole reason Azure reads more naturally than AWS: the group takes the VMs,
    disks, NICs, public addresses, VNet and NSG with it, in ARM's own dependency order."""
    body = _SRC.split("async def delete_environment(", 1)[1]
    assert "resource_groups.begin_delete" in body
    assert "ResourceNotFoundError" in body, "teardown is not idempotent on a missing group"


def test_the_environment_id_is_the_resource_group_name():
    """Derived from the POV name, so a build that dies partway still leaves a group
    somebody can find and delete."""
    assert pov_cloud_env.env_id_for("acme-eval") == "povenv-acme-eval"
    assert "resource_groups.create_or_update(env_id" in _body("_create_network_sync")


def test_the_stored_password_is_cleared_only_after_the_group_is_gone():
    """A failed delete must leave the credential in place, or the retry builds VMs nobody
    can log into."""
    body = _SRC.split("async def delete_environment(", 1)[1]
    assert body.index("begin_delete") < body.index("_forget_password"), \
        "the password is forgotten before the group is confirmed deleted"


def test_the_nsg_carries_no_custom_rules():
    """Azure's defaults ARE the policy: AllowVnetInBound, DenyAllInBound,
    AllowInternetOutBound. Restating them would be three more things to keep correct."""
    body = _code("_create_network_sync")
    assert "network_security_groups.begin_create_or_update" in body
    assert "security_rules" not in body, (
        "the NSG declares custom rules; the platform defaults already allow intra-VNet "
        "in, deny everything else in, and allow outbound")


def test_no_nat_gateway_is_created():
    """A NAT gateway is a standing monthly charge before a byte moves, on a community
    user's own bill. A public address per VM is cheaper for a handful of VMs and exposes
    nothing, because the NSG denies inbound."""
    for fn in ("_create_network_sync", "_create_vms_sync"):
        assert "nat_gateway" not in _code(fn).lower(), f"{fn} creates a NAT gateway"


# ── the generated platform login ─────────────────────────────────────────────

class _FakeConfig:
    """Stands in for config_service, so nothing touches the shared vm_cli.db or .env."""

    def __init__(self):
        self.store = {}

    def get_opt(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def _with_fake_config():
    import sys as _sys
    from web_dashboard import services
    fake = _FakeConfig()
    original = _sys.modules.get("web_dashboard.services.config_service")
    _sys.modules["web_dashboard.services.config_service"] = fake
    services.config_service = fake
    return original, fake


def _restore_config(original):
    import sys as _sys
    from web_dashboard import services
    if original is not None:
        _sys.modules["web_dashboard.services.config_service"] = original
        services.config_service = original


def test_the_platform_password_is_generated_once_and_remembered():
    """Regenerating it would lock the operator out of VMs already built with the old one."""
    original, fake = _with_fake_config()
    try:
        first = az._platform_password("povenv-x")
        second = az._platform_password("povenv-x")
        assert first == second, "a second call minted a different password"
        assert len(first) >= 12, "Azure refuses an admin password under 12 characters"
        assert fake.store[az.password_config_key("povenv-x")] == first
    finally:
        _restore_config(original)


def test_the_generated_password_satisfies_azures_complexity_rule():
    """Three of four classes, and never containing the username."""
    original, _fake = _with_fake_config()
    try:
        pw = az._platform_password("povenv-complexity")
    finally:
        _restore_config(original)
    classes = sum([any(c.islower() for c in pw), any(c.isupper() for c in pw),
                   any(c.isdigit() for c in pw)])
    assert classes >= 3, f"only {classes} character classes in {len(pw)} characters"
    assert az.ADMIN_USERNAME not in pw


def test_the_admin_username_is_one_azure_will_accept():
    """Azure rejects `admin`, `administrator`, `root`, `user` and friends outright."""
    assert az.ADMIN_USERNAME not in (
        "admin", "administrator", "root", "user", "guest", "test", "azureuser")


def test_stored_credentials_parse_back_into_a_username_and_password():
    """The contract's shape is `[{text, notes}]`, because Skytap stores what somebody
    typed in a box. `pov_credentials` is what reads it, so what this returns has to be
    something that parser accepts — not merely something a human could read."""
    import asyncio
    from web_dashboard.services import pov_credentials
    original, _fake = _with_fake_config()
    try:
        password = az._platform_password("povenv-creds")
        entries = asyncio.run(az.stored_credentials("povenv-creds"))
    finally:
        _restore_config(original)
    assert len(entries) == 1
    username, parsed = pov_credentials.pick(entries, vm_label="broker")
    assert username == az.ADMIN_USERNAME
    assert parsed == password


def test_no_stored_credential_is_invented_for_an_environment_that_has_none():
    """An empty list, not a guess. A wrong password comes back from WinRM as an
    authentication failure and sends somebody to reset one that was fine."""
    import asyncio
    original, _fake = _with_fake_config()
    try:
        assert asyncio.run(az.stored_credentials("povenv-never-built")) == []
    finally:
        _restore_config(original)


# ── image references ─────────────────────────────────────────────────────────

def test_a_marketplace_urn_and_a_resource_id_are_both_understood():
    try:
        from azure.mgmt.compute.models import ImageReference  # noqa: F401
    except ImportError:
        print("  (skipped: azure-mgmt-compute not installed)")
        return
    urn = az._image_reference("Canonical:ubuntu-24_04-lts:server:latest")
    assert urn.publisher == "Canonical" and urn.version == "latest"
    rid = az._image_reference("/subscriptions/s/resourceGroups/g/providers/"
                              "Microsoft.Compute/images/golden")
    assert rid.id.endswith("/images/golden")


def test_an_unreadable_image_is_refused_by_name():
    """ARM's own error for a malformed reference names neither the field nor the value."""
    for bad in ("", "ubuntu", "a:b:c", "a:b:c:"):
        try:
            az._image_reference(bad)
        except pov_cloud_env.CloudEnvError as exc:
            assert "Azure image" in str(exc)
        except ImportError:
            print("  (skipped: azure-mgmt-compute not installed)")
            return
        else:  # pragma: no cover
            raise AssertionError(f"{bad!r} was accepted as an Azure image")


# ── listing is subscription-wide ─────────────────────────────────────────────

def test_azure_lists_every_region_at_once_and_the_shared_lister_knows():
    """Resource groups list subscription-wide. Asking per region would return the same
    environments N times — and the per-region path can only look where a POV row already
    exists, so an orphan in another region would stay invisible."""
    assert az.LISTS_ALL_REGIONS is True
    body = pov_cloud_env.list_environments.__doc__ or ""
    assert "LISTS_ALL_REGIONS" in body
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_env.py"),
               encoding="utf-8").read()
    listing = src.split("async def list_environments(", 1)[1].split("\nasync def ", 1)[0]
    assert 'getattr(mod, "LISTS_ALL_REGIONS", False)' in listing


def test_a_group_with_no_vms_left_is_not_reported_as_gone():
    """That state is a teardown which removed the machines and then failed. Answering
    None would let the reconcile mark the POV missing while its network still exists."""
    body = _body("_read_environment_sync")
    assert "ResourceNotFoundError" in body, \
        "the read does not distinguish a missing group from an empty one"
    assert body.index("resource_groups.get") < body.index("virtual_machines.list"), \
        "the group is not checked before its VMs, so an empty group reads as absent"


# ── addressing ───────────────────────────────────────────────────────────────

class _FakeNic:
    def __init__(self, ip):
        self.ip_configurations = [type("C", (), {"private_ip_address": ip})()]


class _FakeNetwork:
    def __init__(self, ips, boom=False):
        self._ips, self._boom = ips, boom

    class _NICs:
        def __init__(self, outer):
            self.outer = outer

        def list(self, _rg):
            if self.outer._boom:
                raise RuntimeError("ARM said no")
            return [_FakeNic(ip) for ip in self.outer._ips]

    @property
    def network_interfaces(self):
        return self._NICs(self)


def test_the_first_address_skips_the_four_azure_reserves():
    net = {"subnet_cidr": "10.20.0.0/24"}
    assert az._next_free_ip(_FakeNetwork([]), net, "rg") == "10.20.0.4"


def test_addresses_already_in_use_are_skipped():
    net = {"subnet_cidr": "10.20.0.0/24"}
    used = ["10.20.0.4", "10.20.0.5", "10.20.0.7"]
    assert az._next_free_ip(_FakeNetwork(used), net, "rg") == "10.20.0.6"


def test_an_unreadable_address_list_raises_rather_than_guessing():
    """Falling back to dynamic would hand two VMs the same address, and ARM reports that
    as a failure on the SECOND one with no hint that the first is why."""
    try:
        az._next_free_ip(_FakeNetwork([], boom=True), {"subnet_cidr": "10.20.0.0/24"},
                         "rg")
    except pov_cloud_env.CloudEnvError as exc:
        assert "collide" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a failed address read fell through to a guess")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
