"""The lab-platform registry, and the Skytap adapter's mapping.

A POV environment is a template instantiated whole on some lab platform. Only a thin slice
of that is platform-specific — auth, template listing, environment create/delete, runstate,
bootstrap injection, share links, idle suspend — while the broker wait, the PAM wire-up,
the reaping manifest and the expiry ladder are shared. This registry is what keeps that
line drawn, so the second platform is an adapter module rather than a second feature.

What is pinned here:

  * every platform in ``VALID_PLATFORMS`` has an adapter AND a capability entry, so a
    half-added platform fails at import rather than at 2am mid-provision;
  * every adapter satisfies ``READ_CONTRACT``;
  * the capability table is *complete* per platform — a missing key reads as falsy, which
    would silently disable a feature the platform actually supports;
  * the adapter's mapping returns ABSENT rather than zero for counts it did not measure.

Runs against httpx.MockTransport where it needs a response: no network, no app.

Runs under pytest, or standalone:
    python tests/test_lab_platforms.py
"""
import asyncio
import inspect
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-lab-platforms")

try:
    import httpx
except ImportError:  # pragma: no cover
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services import lab_platforms as lp  # noqa: E402
from web_dashboard.services import skytap_service  # noqa: E402
from web_dashboard.services.skytap_client import SkytapClient, SkytapCreds  # noqa: E402

# Every key CAPABILITIES must answer for. A platform that simply omits one reads as
# "cannot", which would silently switch off a capability it really has.
_REQUIRED_CAPABILITY_KEYS = {
    "label", "templates", "runstate", "idle_suspend",
    "bootstrap_injection", "share_link", "stored_credentials",
    "verify", "projects",
}

# bootstrap_injection is one INTENT with different mechanisms, so it is an enum.
_INJECTION_MECHANISMS = {"metadata", "remote_exec", None}


# ── the registry ─────────────────────────────────────────────────────────────

def test_every_platform_has_an_adapter():
    for name in lp.VALID_PLATFORMS:
        assert lp.adapter(name) is not None, f"{name} has no adapter module"


def test_every_platform_has_complete_capabilities():
    for name in lp.VALID_PLATFORMS:
        caps = lp.capabilities(name)
        missing = _REQUIRED_CAPABILITY_KEYS - set(caps)
        assert not missing, (
            f"{name} declares no value for {sorted(missing)} — an absent key reads as "
            f"'cannot', which silently disables a capability the platform may have")


def test_injection_mechanisms_are_from_the_known_set():
    for name in lp.VALID_PLATFORMS:
        mech = lp.capabilities(name)["bootstrap_injection"]
        assert mech in _INJECTION_MECHANISMS, (
            f"{name}: unknown bootstrap mechanism {mech!r}; the orchestrator switches on "
            f"this value, so a new one needs a branch, not a new string")


def test_every_adapter_satisfies_the_read_contract():
    for name in lp.VALID_PLATFORMS:
        mod = lp.adapter(name)
        for fn in lp.READ_CONTRACT:
            assert hasattr(mod, fn), f"{name} adapter is missing {fn}()"
            assert callable(getattr(mod, fn)), f"{name}.{fn} is not callable"


def test_the_read_contract_reads_are_async():
    """They are awaited from the router and, later, from job handlers."""
    for name in lp.VALID_PLATFORMS:
        mod = lp.adapter(name)
        for fn in ("list_templates", "list_environments", "get_environment"):
            assert inspect.iscoroutinefunction(getattr(mod, fn)), \
                f"{name}.{fn} must be async"


def test_an_unknown_platform_is_refused_by_name():
    for bad in ("", "cloudshare", "SKYTAPP"):
        try:
            lp.normalize(bad)
        except lp.LabPlatformError as exc:
            assert "skytap" in str(exc), "the error should list what IS supported"
        else:  # pragma: no cover
            raise AssertionError(f"{bad!r} was accepted as a platform")


def test_platform_names_are_normalised():
    assert lp.normalize("  Skytap ") == "skytap"
    assert lp.valid("SKYTAP") is True


def test_supports_answers_for_a_real_capability():
    assert lp.supports("skytap", "share_link") is True
    assert lp.supports("skytap", "a_capability_nobody_has") is False


def test_the_registry_imports_no_adapter_at_module_scope():
    """Reading the registry — which the UI does constantly — must not drag in every
    adapter's HTTP stack."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "lab_platforms.py"),
               encoding="utf-8").read()
    head = src.split("VALID_PLATFORMS", 1)[0]
    assert "skytap_service" not in head, \
        "lab_platforms imports an adapter at module scope"


def test_configured_platforms_survives_a_broken_adapter():
    """One platform failing its credential check must not take the list down."""
    original = skytap_service.configured

    def _boom():
        raise RuntimeError("config backend down")

    skytap_service.configured = _boom
    try:
        assert lp.configured_platforms() == []
    finally:
        skytap_service.configured = original


# ── the Skytap adapter's mapping ─────────────────────────────────────────────

def _adapter_with(payload, status=200):
    """Point the adapter's client at a canned response."""
    creds = SkytapCreds(username="u", api_token="t", base_url="https://skytap.test")

    def handler(_request):
        return httpx.Response(status, json=payload)

    async def _sleep(_s):
        pass

    client = SkytapClient(creds, transport=httpx.MockTransport(handler), sleep=_sleep)
    original = skytap_service._client
    skytap_service._client = lambda: client
    return original


def _restore(original):
    skytap_service._client = original


def test_environment_vm_count_is_absent_not_zero_when_unmeasured():
    """A collection read may omit the vms array entirely. Rendering '0 VMs' for an
    environment that has eight is worse than rendering nothing — the same rule the
    hypervisor projections follow for live-only fields."""
    original = _adapter_with([{"id": "1", "name": "poc-a", "runstate": "running"}])
    try:
        envs = asyncio.run(skytap_service.list_environments())
    finally:
        _restore(original)
    assert envs[0]["vm_count"] is None, \
        f"expected None for an unmeasured count, got {envs[0]['vm_count']!r}"


def test_a_nested_read_counts_the_vms_it_actually_got():
    original = _adapter_with({
        "id": "9", "name": "poc-b", "runstate": "running",
        "vms": [{"id": "v1", "name": "dc01"}, {"id": "v2", "name": "web01"}],
    })
    try:
        env = asyncio.run(skytap_service.get_environment("9"))
    finally:
        _restore(original)
    assert env["vm_count"] == 2
    assert [v["name"] for v in env["vms"]] == ["dc01", "web01"]


def test_private_ip_and_published_services_are_pulled_off_the_interfaces():
    original = _adapter_with({
        "id": "9",
        "vms": [{
            "id": "v1", "name": "web01", "guestos": "Ubuntu 22.04",
            "interfaces": [{
                "ip": "10.0.0.5",
                "services": [{"id": "3389", "internal_port": 3389,
                              "external_ip": "76.191.118.29", "external_port": 12345}],
            }],
        }],
    })
    try:
        env = asyncio.run(skytap_service.get_environment("9"))
    finally:
        _restore(original)
    vm = env["vms"][0]
    assert vm["private_ip"] == "10.0.0.5"
    assert vm["published_services"][0]["external_port"] == 12345
    assert vm["os_family"] == "linux"


def test_os_family_is_blank_rather_than_a_wrong_guess():
    """A confident wrong answer would send a Windows VM down the Password-Safe-over-SSH
    path. Blank makes the later wire-up ask."""
    original = _adapter_with({"id": "9", "vms": [{"id": "v1", "name": "appliance-01"}]})
    try:
        env = asyncio.run(skytap_service.get_environment("9"))
    finally:
        _restore(original)
    assert env["vms"][0]["os_family"] == ""


def test_windows_is_detected():
    original = _adapter_with({
        "id": "9", "vms": [{"id": "v1", "name": "dc01", "guestos": "Windows Server 2019"}]})
    try:
        env = asyncio.run(skytap_service.get_environment("9"))
    finally:
        _restore(original)
    assert env["vms"][0]["os_family"] == "windows"


def test_an_empty_environment_id_is_refused_before_a_request_is_made():
    try:
        asyncio.run(skytap_service.get_environment("  "))
    except Exception as exc:
        assert "environment id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a blank environment id was accepted")


def test_ids_are_strings_so_they_survive_json_round_trips():
    """Skytap returns numeric ids in some payloads and strings in others; the POV row will
    key on this, and 42 != '42' is a lookup that silently finds nothing."""
    original = _adapter_with([{"id": 42, "name": "poc-a"}])
    try:
        envs = asyncio.run(skytap_service.list_environments())
    finally:
        _restore(original)
    assert envs[0]["id"] == "42"


def test_a_platform_that_claims_verify_actually_has_one():
    """The capability is what the UI renders a Test button from, so a platform claiming it
    without the function would offer a button that 500s. Same shape as the pairing
    `bt_tenant_service.VERIFIABLE_KINDS` keeps with its own verifiers."""
    for name in lp.VALID_PLATFORMS:
        fn = getattr(lp.adapter(name), "verify", None)
        claims = lp.supports(name, "verify")
        assert claims == callable(fn), (
            f"{name}: capabilities say verify={claims} but the adapter "
            f"{'has' if callable(fn) else 'has no'} verify()")
        if claims:
            assert inspect.iscoroutinefunction(fn), (
                f"{name}.verify must be async — every other adapter read is, and the "
                f"endpoint awaits it")


def test_a_platform_that_claims_projects_can_report_the_configured_one():
    """`api/pov.provision` asks the registry for the configured project so it can record
    what a new environment was really created in. A platform claiming `projects` without
    the accessor would make that read blow up at provision time."""
    for name in lp.VALID_PLATFORMS:
        if not lp.supports(name, "projects"):
            continue
        fn = getattr(lp.adapter(name), "configured_project_id", None)
        assert callable(fn), f"{name} claims projects but has no configured_project_id()"
        assert isinstance(fn(), str), f"{name}.configured_project_id() must return a str"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
