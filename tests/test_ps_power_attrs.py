"""Power-state attributes: a start or stop stamps Online/Offline on the VM's Password Safe
records (services/ps_power_attrs.py).

Pinned here:
  * every power verb any path uses is CLASSIFIED, so a new one cannot silently mean
    "no change";
  * the gate — feature off, Password Safe off or unconfigured — makes no PS call;
  * a recorded system id is written even without an index; an IP match pulls in the
    linked asset; an IP that matches two systems is refused, not guessed;
  * apply-then-remove, a per-record failure that never escapes, audit rows only after a
    write that happened;
  * a POV's own tenant gets the configured values translated by NAME, or a reason;
  * the settings keys survive a save and a typed label is refused;
  * every path calls the hook AFTER its job is terminal.

Run directly:
    python tests/test_ps_power_attrs.py
"""
import asyncio
import inspect
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(_TMP, "psp.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the app deps BY NAME — see tests/test_import_guard_narrowness.py.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
    import httpx  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.services import ps_attribute_catalog as pac
from web_dashboard.services import ps_power_attrs as ppa
from _settings_src import settings_source

_WEB = os.path.join(_ROOT, "web_dashboard")


def _src(rel):
    with open(os.path.join(_WEB, rel), encoding="utf-8") as fh:
        return fh.read()


# ── harness ──────────────────────────────────────────────────────────────────

class _Env:
    """Patches the module's collaborators for one test and records every PS write."""

    def __init__(self, *, ids=("11", "12"), index=None, fail=(), on=True):
        self.writes, self.audits, self.logs = [], [], []
        self.ids, self.index, self.fail, self.on = ids, index, set(fail), on

    def __enter__(self):
        self.saved = {
            "enabled": ppa.enabled, "configured_ids": ppa.configured_ids,
            "_index": ppa._index,
            "set_a": ppa.ps_api_service.set_asset_attribute,
            "set_s": ppa.ps_api_service.set_managed_system_attribute,
            "configured": ppa.ps_api_service.configured,
            "audit": ppa.job_service.log_audit, "log": ppa.job_service.append_job_log,
        }
        ppa.enabled = lambda: self.on
        ppa.configured_ids = lambda: {ppa.STATE_ONLINE: self.ids[0],
                                      ppa.STATE_OFFLINE: self.ids[1]}

        async def _index(tenant, key):
            return self.index
        ppa._index = _index

        def writer(kind):
            async def w(object_id, attr_id, *, assign, tenant=None):
                ref = f"{kind}:{object_id}"
                if ref in self.fail:
                    raise ppa.ps_api_service.PSApiError("Password Safe refused (403)")
                self.writes.append((ref, attr_id, assign))
            return w
        ppa.ps_api_service.set_asset_attribute = writer(pac.KIND_ASSET)
        ppa.ps_api_service.set_managed_system_attribute = writer(pac.KIND_SYSTEM)
        ppa.ps_api_service.configured = lambda: True
        ppa.job_service.log_audit = lambda db, user, action, **kw: self.audits.append(
            (action, kw.get("target_vm")))
        ppa.job_service.append_job_log = lambda db, job_id, line: self.logs.append(line)
        return self

    def __exit__(self, *exc):
        s = self.saved
        ppa.enabled, ppa.configured_ids, ppa._index = s["enabled"], s["configured_ids"], s["_index"]
        ppa.ps_api_service.set_asset_attribute = s["set_a"]
        ppa.ps_api_service.set_managed_system_attribute = s["set_s"]
        ppa.ps_api_service.configured = s["configured"]
        ppa.job_service.log_audit, ppa.job_service.append_job_log = s["audit"], s["log"]


def _after(op, resource, **kw):
    return asyncio.run(ppa.after_power(None, "job-1", op, resource, actor="tester", **kw))


def _index():
    """Two hosts. 10.0.0.5 is one system with its asset; 10.0.0.9 is shared by two."""
    assets = [{"AssetID": 17, "AssetName": "web", "IPAddress": "10.0.0.5"}]
    systems = [{"ManagedSystemID": 42, "SystemName": "web", "AssetID": 17,
                "IPAddress": "10.0.0.5"},
               {"ManagedSystemID": 50, "SystemName": "a", "IPAddress": "10.0.0.9"},
               {"ManagedSystemID": 51, "SystemName": "b", "IPAddress": "10.0.0.9"}]
    return pac.build_index(assets, systems)


# ── 1. classification ───────────────────────────────────────────────────────

def test_every_router_power_op_is_classified():
    missing = []
    for name in os.listdir(os.path.join(_WEB, "api")):
        if not name.endswith(".py"):
            continue
        src = _src(os.path.join("api", name))
        for m in re.finditer(r'_power_endpoint\("([a-z_]+)"\)', src):
            if m.group(1) not in ppa.STATE_OF_OP:
                missing.append(f"{name}: {m.group(1)}")
        bulk = re.search(r"^BULK_OPS = \(([^)]*)\)", src, re.M)
        for op in re.findall(r'"([a-z_]+)"', bulk.group(1) if bulk else ""):
            if op not in ppa.STATE_OF_OP:
                missing.append(f"{name} BULK_OPS: {op}")
    assert not missing, "power ops ps_power_attrs does not classify: " + ", ".join(missing)


def test_every_agent_write_verb_and_pov_runstate_is_classified():
    from web_dashboard.services import agent_hypervisor_meta as ahm
    assert set(ahm.WRITE_VERBS) <= set(ppa.STATE_OF_OP), set(ahm.WRITE_VERBS) - set(ppa.STATE_OF_OP)
    runstates = re.search(r"VALID_RUNSTATES = \(([^)]*)\)",
                          _src("services/pov_cloud_env.py")).group(1)
    for rs in re.findall(r'"([a-z_]+)"', runstates):
        assert rs in ppa.STATE_OF_OP, rs


def test_the_scans_find_something():
    assert "start" in _src("api/proxmox.py") and ppa.state_of("start") == ppa.STATE_ONLINE
    assert ppa.state_of("STOP") == ppa.STATE_OFFLINE
    assert ppa.state_of("reboot") == "" and ppa.state_of("power_reset") == ""


# ── 2. the gate ─────────────────────────────────────────────────────────────

def test_feature_off_makes_no_call_and_no_log():
    with _Env(on=False) as env:
        assert _after("stop", {"ps_system_id": "42"}) is None
    assert env.writes == [] and env.logs == []


def test_a_verb_that_changes_no_power_state_does_nothing():
    with _Env() as env:
        assert _after("reboot", {"ps_system_id": "42"}) is None
    assert env.writes == []


def test_an_unconfigured_password_safe_makes_no_call():
    with _Env() as env:
        ppa.ps_api_service.configured = lambda: False
        assert _after("stop", {"ps_system_id": "42"}) is None
    assert env.writes == []


def test_no_attribute_configured_is_a_reason_not_a_write():
    with _Env(ids=("", "")) as env:
        out = _after("stop", {"ps_system_id": "42"})
    assert env.writes == [] and "no attribute is configured" in out["reason"]


def test_the_gate_reads_both_switches():
    src = inspect.getsource(ppa.enabled)
    assert "password_safe_enabled" in src and "passwordsafe_power_attributes_enabled" in src


# ── 3. resolving and writing ────────────────────────────────────────────────

def test_a_recorded_id_is_written_without_an_index():
    with _Env(index=None) as env:
        out = _after("stop", {"ps_system_id": "42"})
    assert env.writes == [("managed_system:42", "12", True), ("managed_system:42", "11", False)]
    assert out["reason"] == ""


def test_a_recorded_id_pulls_in_the_linked_asset_when_the_index_has_it():
    with _Env(index=_index()) as env:
        _after("start", {"ps_system_id": "42"})
    refs = {w[0] for w in env.writes}
    assert refs == {"managed_system:42", "asset:17"}
    assert ("asset:17", "11", True) in env.writes and ("asset:17", "12", False) in env.writes


def test_an_address_match_finds_the_system_and_its_asset():
    with _Env(index=_index()) as env:
        _after("stop", {"ips": ["10.0.0.5"]})
    assert {w[0] for w in env.writes} == {"managed_system:42", "asset:17"}


def test_an_address_shared_by_two_systems_is_skipped_not_guessed():
    with _Env(index=_index()) as env:
        out = _after("stop", {"ips": ["10.0.0.9"]})
    assert env.writes == []
    assert "skipped rather than guessed" in out["reason"]
    assert "not applied" in env.logs[0]


def test_no_match_is_a_reason():
    with _Env(index=_index()) as env:
        out = _after("stop", {"ips": ["192.0.2.1"]})
    assert env.writes == [] and "no Password Safe record" in out["reason"]


def test_no_resource_at_all_is_said_so_on_the_job():
    with _Env() as env:
        out = _after("stop", None)
    assert env.writes == [] and "no recorded Password Safe system" in out["reason"]
    assert env.logs and "not applied" in env.logs[0]


def test_apply_comes_before_remove():
    with _Env(index=None) as env:
        _after("stop", {"ps_system_id": "42"})
    assert [w[2] for w in env.writes] == [True, False]


def test_a_blank_opposite_value_is_not_removed():
    with _Env(ids=("", "12"), index=None) as env:
        _after("stop", {"ps_system_id": "42"})
    assert env.writes == [("managed_system:42", "12", True)]


def test_a_refused_write_is_reported_and_never_raises():
    with _Env(index=_index(), fail={"asset:17"}) as env:
        out = _after("stop", {"ps_system_id": "42"})
    failed = [o for o in out["outcomes"] if not o["ok"]]
    assert failed and all(o["ref"] == "asset:17" for o in failed)
    assert "FAILED" in env.logs[0] and "403" in env.logs[0]
    # The system write still happened.
    assert ("managed_system:42", "12", True) in env.writes


def test_audit_rows_only_for_writes_that_happened():
    with _Env(index=_index(), fail={"asset:17"}) as env:
        _after("stop", {"ps_system_id": "42"})
    assert all(target == "managed_system:42" for _, target in env.audits)
    assert ("attributes.assign", "managed_system:42") in env.audits
    assert ("attributes.remove", "managed_system:42") in env.audits


def test_audit_actions_match_the_attribute_route():
    from web_dashboard.api import ps_attributes
    assert ppa.AUDIT_ASSIGN == ps_attributes.AUDIT_ASSIGN
    assert ppa.AUDIT_REMOVE == ps_attributes.AUDIT_REMOVE


def test_an_unexpected_error_inside_never_escapes_after_power():
    with _Env() as env:
        async def boom(tenant, key):
            raise RuntimeError("x")
        ppa._index = boom
        assert _after("stop", {"ps_system_id": "42"}) is None
    assert env.writes == []


def test_the_path_hooks_are_sealed():
    for fn in (ppa.after_cloud_power, ppa.after_hypervisor_power,
               ppa.after_agent_job, ppa.after_pov_power):
        assert getattr(fn, "__wrapped__", None) is not None, fn.__name__


def test_a_failed_agent_job_stamps_nothing():
    calls = []
    saved = ppa.after_power

    async def spy(*a, **kw):
        calls.append(a)
    ppa.after_power = spy
    try:
        job = type("J", (), {"job_type": "agent_hypervisor", "id": "j",
                             "metadata_dict": {"verb": "power_off"},
                             "cloud_resource_id": "", "created_by": "x"})()
        with _Env():
            asyncio.run(ppa.after_agent_job(None, job, "failed"))
    finally:
        ppa.after_power = saved
    assert calls == []


# ── 4. a POV's own tenant ───────────────────────────────────────────────────

def _vocab(type_name, values, read_only=False):
    return [{"type_id": "1", "name": type_name, "read_only": read_only,
             "values": [{"attribute_id": aid, "value": v} for aid, v in values]}]


def _with_vocab(home, there, fn):
    saved = (ppa._vocabulary, ppa.configured_ids)

    async def vocab(tenant, key):
        return home if tenant is None else there
    ppa._vocabulary = vocab
    ppa.configured_ids = lambda: {ppa.STATE_ONLINE: "11", ppa.STATE_OFFLINE: "12"}
    try:
        return asyncio.run(fn())
    finally:
        ppa._vocabulary, ppa.configured_ids = saved


def test_a_pov_tenant_gets_the_values_by_name():
    home = _vocab("Power State", [("11", "Online"), ("12", "Offline")])
    there = _vocab("power state", [("901", "online"), ("902", "OFFLINE")])
    ids, reason = _with_vocab(home, there, lambda: ppa.ids_for_tenant({"t": 1}, "pov:x"))
    assert reason == "" and ids == {ppa.STATE_ONLINE: "901", ppa.STATE_OFFLINE: "902"}


def test_a_pov_tenant_without_the_value_is_skipped_with_its_name():
    home = _vocab("Power State", [("11", "Online"), ("12", "Offline")])
    there = _vocab("Power State", [("901", "Online")])
    ids, reason = _with_vocab(home, there, lambda: ppa.ids_for_tenant({"t": 1}, "pov:x"))
    assert ids == {} and "Power State = Offline" in reason


def test_a_read_only_type_in_the_pov_tenant_does_not_count():
    home = _vocab("Power State", [("11", "Online"), ("12", "Offline")])
    there = _vocab("Power State", [("901", "Online"), ("902", "Offline")], read_only=True)
    ids, reason = _with_vocab(home, there, lambda: ppa.ids_for_tenant({"t": 1}, "pov:x"))
    assert ids == {} and reason


def test_the_default_tenant_uses_the_ids_as_configured():
    ids, reason = _with_vocab([], [], lambda: ppa.ids_for_tenant(None, "default"))
    assert reason == "" and ids == {ppa.STATE_ONLINE: "11", ppa.STATE_OFFLINE: "12"}


# ── 5. settings ─────────────────────────────────────────────────────────────

def test_the_settings_keys_exist_in_both_places():
    from web_dashboard.api.setup import PasswordSafeFeatureConfig
    from web_dashboard.config import Settings
    for key in ("passwordsafe_power_attributes_enabled", "passwordsafe_power_attr_online",
                "passwordsafe_power_attr_offline"):
        assert key in PasswordSafeFeatureConfig.model_fields, key
        assert key in Settings.model_fields, key
        assert f"panelCfg.{key}" in settings_source() or \
            f"'{key}'" in settings_source(), key


def test_a_typed_label_is_refused_and_an_id_is_kept():
    from pydantic import ValidationError
    from web_dashboard.api.setup import PasswordSafeFeatureConfig
    ok = PasswordSafeFeatureConfig(passwordsafe_power_attr_online=" 11 ")
    assert ok.passwordsafe_power_attr_online == "11"
    try:
        PasswordSafeFeatureConfig(passwordsafe_power_attr_offline="Offline")
    except ValidationError as exc:
        assert "attribute id" in str(exc)
    else:
        raise AssertionError("a label was accepted as an attribute id")


# ── 6. wiring ───────────────────────────────────────────────────────────────

_HOOKED = {
    "services/aws_vm_service.py": "after_cloud_power",
    "services/azure_vm_service.py": "after_cloud_power",
    "services/gcp_vm_service.py": "after_cloud_power",
    "services/oci_vm_service.py": "after_cloud_power",
    "api/proxmox.py": "after_hypervisor_power",
    "api/hyperv.py": "after_hypervisor_power",
    "api/nutanix.py": "after_hypervisor_power",
    "api/vsphere.py": "after_hypervisor_power",
    "api/xcpng.py": "after_hypervisor_power",
    "services/pov_env_service.py": "after_pov_power",
}


def test_every_path_stamps_after_its_job_is_terminal():
    for rel, hook in _HOOKED.items():
        src = _src(rel)
        at = src.find(f"ps_power_attrs.{hook}(")
        assert at > 0, f"{rel} does not call {hook}"
        before = src[:at]
        assert before.rfind("set_completed(") > before.rfind("async def "), \
            f"{rel}: {hook} is not after set_completed in its function"


def test_the_agent_route_stamps_after_complete_job():
    src = _src("api/agent.py")
    at = src.find("ps_power_attrs.after_agent_job(db, job, applied)")
    assert at > src.find("applied = agent_service.complete_job(") > 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
