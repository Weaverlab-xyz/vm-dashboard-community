"""Agent-synced guest tags, and the edit that used to erase them.

The agent's Proxmox sync built every inventory row without `tags`, the cache stored that
as "[]", and an agent-brokered tag edit computes the COMPLETE desired set from the cache —
so adding one tag to a guest wrote that one tag and nothing else. Pinned here end to end:

  * the agent reports tags on every row, empty or not;
  * the cache keeps "not reported" (NULL) apart from "none" ("[]");
  * the edit refuses an unknown set rather than guessing it empty, and keeps a known one.

Also here, because it is the same "the edit looked like it did nothing" family: writing a
Password Safe attribute clears the cache /inventory reads attributes through.

Run directly:
    python tests/test_hypervisor_tag_sync.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(_TMP, "hvtags.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the app deps BY NAME — see tests/test_import_guard_narrowness.py.
try:
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
    import requests  # noqa: F401
    import yaml  # noqa: F401
    import cryptography  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.database import Base, HypervisorVMCache, SessionLocal, engine
from web_dashboard.services import hypervisor_sync_service
from web_dashboard.utils.clock import utcnow

Base.metadata.create_all(bind=engine)


def _load_agent():
    path = os.path.join(_ROOT, "runners", "agent", "agent.py")
    spec = importlib.util.spec_from_file_location("agent_under_test_tags", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENT = _load_agent()


# ── 1. the agent reports tags ────────────────────────────────────────────────

def _sync_rows(resources):
    saved = {n: getattr(AGENT, n) for n in
             ("_conn_endpoint", "_check_endpoint", "_secret_for", "_hv_request")}
    AGENT._conn_endpoint = lambda conn, port: ("pve.lab", port)
    AGENT._check_endpoint = lambda policy, host, port: None
    AGENT._secret_for = lambda conn, checkins: "s3cret"
    AGENT._hv_request = lambda conn, method, url, headers=None, **kw: {"data": resources}
    try:
        return AGENT._sync_proxmox({"name": "pve", "token_id": "dash"}, {}, None,
                                   lambda msg: None)["vms"]
    finally:
        for n, fn in saved.items():
            setattr(AGENT, n, fn)


def test_the_agent_reports_a_guests_tags_as_a_list():
    rows = _sync_rows([{"vmid": 101, "name": "web", "node": "pve1", "type": "qemu",
                        "status": "stopped", "tags": "prod;web"}])
    assert rows[0]["tags"] == ["prod", "web"]


def test_the_agent_reports_an_untagged_guest_as_an_empty_list_not_a_missing_key():
    """The missing key is what the dashboard reads as 'this agent cannot tell me'."""
    rows = _sync_rows([{"vmid": 102, "name": "db", "node": "pve1", "type": "lxc",
                        "status": "running"}])
    assert "tags" in rows[0] and rows[0]["tags"] == []


def test_the_tag_split_accepts_every_separator_pve_does():
    assert AGENT._proxmox_tag_list("a;b, c d") == ["a", "b", "c", "d"]
    assert AGENT._proxmox_tag_list(None) == []


# ── 2. the cache keeps unknown apart from none ──────────────────────────────

def _upsert(conn_id, vm):
    db = SessionLocal()
    try:
        hypervisor_sync_service._upsert(db, conn_id, [vm], utcnow())
        return {r["vm_id"]: r for r in hypervisor_sync_service.list_vms(db, conn_id)}
    finally:
        db.close()


def test_a_row_without_tags_is_stored_as_unknown():
    rows = _upsert("c-old", {"vm_id": "201", "name": "a"})
    assert rows["201"]["tags"] == [] and rows["201"]["tags_known"] is False
    db = SessionLocal()
    try:
        assert db.get(HypervisorVMCache, ("c-old", "201")).tags is None
    finally:
        db.close()


def test_a_row_with_an_empty_tag_list_is_known_to_have_none():
    rows = _upsert("c-new", {"vm_id": "202", "name": "b", "tags": []})
    assert rows["202"]["tags"] == [] and rows["202"]["tags_known"] is True


# ── 3. the edit ─────────────────────────────────────────────────────────────

def _queue(cached_row, add=None, remove=None):
    from web_dashboard.api import proxmox

    jobs = []

    class _Job:
        id = "job-1"

    def fake_job(db, conn, *, tags, **kw):
        jobs.append(list(tags))
        return _Job()

    class _User:
        username = "tester"

    saved = (proxmox.hypervisor_sync_service.list_vms, proxmox.agent_tag_job)
    proxmox.hypervisor_sync_service.list_vms = lambda db, conn_id: [cached_row]
    proxmox.agent_tag_job = fake_job
    try:
        payload = proxmox.TagEditRequest(
            targets=[proxmox.TagTarget(vmid=int(cached_row["vm_id"]),
                                       node=cached_row["scope"])],
            add=add or {}, remove=remove or [])
        out = asyncio.run(proxmox._queue_tag_jobs(
            None, type("C", (), {"name": "pve", "id": "c1"})(), payload, add or {}, remove or [], _User()))
    finally:
        proxmox.hypervisor_sync_service.list_vms, proxmox.agent_tag_job = saved
    return out, jobs


def test_an_edit_keeps_the_tags_the_guest_already_has():
    out, jobs = _queue({"vm_id": "101", "scope": "pve1", "tags": ["prod", "web"],
                        "tags_known": True}, add={"pci": ""})
    assert jobs == [["prod", "web", "pci"]], jobs
    assert out["queued"] and not out["failed"]


def test_an_edit_against_unreported_tags_is_refused_and_queues_nothing():
    from web_dashboard.api import proxmox
    out, jobs = _queue({"vm_id": "101", "scope": "pve1", "tags": [], "tags_known": False},
                       add={"pci": ""})
    assert jobs == []
    assert out["failed"][0]["error"] == proxmox.TAGS_UNKNOWN_REFUSAL


# ── 4. Password Safe attribute writes clear the inventory cache ─────────────

def test_an_attribute_write_clears_the_inventory_attribute_cache():
    from web_dashboard.api import ps_attributes as psa
    from web_dashboard.services import cache_service

    key = cache_service.key_param("ps_attributes", workgroup="*")

    async def _vocab():
        return {}

    async def _write(object_id, attribute_id, assign=True):
        return None

    saved = (psa._require_ready, psa._vocabulary, psa.pac.assert_assignable,
             psa.ps_api_service.set_asset_attribute, psa.job_service.log_audit)
    psa._require_ready = lambda: None
    psa._vocabulary = _vocab
    psa.pac.assert_assignable = lambda vocab, aid: ({"name": "Env"}, {"value": "Prod"})
    psa.ps_api_service.set_asset_attribute = _write
    psa.job_service.log_audit = lambda *a, **kw: None

    async def _go():
        await cache_service.set(key, {"stale": True}, 900)
        assert await cache_service.get(key) is not None
        await psa.set_asset_attributes(
            psa.AssetAttributeRequest(asset_ids=[7], attribute_id=3), db=None,
            current_user=type("U", (), {"username": "admin"})())
        return await cache_service.get(key)

    try:
        assert asyncio.run(_go()) is None
    finally:
        (psa._require_ready, psa._vocabulary, psa.pac.assert_assignable,
         psa.ps_api_service.set_asset_attribute, psa.job_service.log_audit) = saved


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
