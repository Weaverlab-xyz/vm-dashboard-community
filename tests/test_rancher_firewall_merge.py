"""Unit test: rancher_node_service.refresh_rancher_firewall must apply the MERGED
firewall source set — manual CSV CIDRs + dashboard-provisioned cluster egress /32s
+ the dashboard-managed Web-Jump Jumpoint /32 — deduped and sorted, while staying
fail-closed and no-op safe.

This pins the Rancher firewall automation: private clusters egress through a NAT
whose public IP the operator can't know ahead of time, so the dashboard captures
each provisioned cluster's egress IP and auto-adds it (as a /32) to the Rancher
node firewall; the Web Jump's Jumpoint egress IP is added the same way. The exact
``source_cidrs`` handed to ``gcp_service.ensure_rancher_firewall`` is asserted.

Heavy deps (database, config, config_service, gcp_service, job_service,
rancher_service, httpx) are stubbed in sys.modules so no DB or cloud account is
needed. Runs under pytest, or standalone:
    python tests/test_rancher_firewall_merge.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    def __getattr__(self, _key):
        return ""


# ── config_service stub backed by a mutable dict ──────────────────────────────
_CFG = {}


def _cfg_get(key):
    return _CFG.get(key, "")


def _cfg_get_bool(key, default=False):
    v = _CFG.get(key)
    if v is None or v == "":
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def _cfg_set(key, value):
    _CFG[key] = value


# ── gcp_service stub: capture what refresh applies ────────────────────────────
_APPLIED = {}


async def _fake_ensure_rancher_firewall(project_id, network, tag, source_cidrs, name):
    _APPLIED["called"] = True
    _APPLIED["source_cidrs"] = list(source_cidrs)
    _APPLIED["name"] = name
    return {"name": name, "opened": bool(source_cidrs)}


# ── database stub: K8sCluster + a fake query returning our rows ───────────────
class _Col:
    def isnot(self, _other):
        return ("isnot", _other)


class _K8sCluster:
    egress_ip = _Col()


class _Row:
    def __init__(self, name, cloud, egress_ip):
        self.name, self.cloud, self.egress_ip = name, cloud, egress_ip


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows=None):
        self._rows = rows or []

    def query(self, *a, **k):
        return _Query(self._rows)


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.SessionLocal = lambda: _FakeDB()
    dbmod.K8sCluster = _K8sCluster
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = _cfg_get
    cfg.get_bool = _cfg_get_bool
    cfg.set = _cfg_set
    sys.modules["web_dashboard.services.config_service"] = cfg

    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.ensure_rancher_firewall = _fake_ensure_rancher_firewall
    sys.modules["web_dashboard.services.gcp_service"] = gcp

    js = types.ModuleType("web_dashboard.services.job_service")
    sys.modules["web_dashboard.services.job_service"] = js

    rs = types.ModuleType("web_dashboard.services.rancher_service")
    sys.modules["web_dashboard.services.rancher_service"] = rs

    # rancher_node_service does `import httpx` at module top (only used in a
    # coroutine we never call) — a bare stub avoids the dependency.
    sys.modules.setdefault("httpx", types.ModuleType("httpx"))


_install_stubs()
try:
    from web_dashboard.services import rancher_node_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"rancher_node_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _reset(**cfg):
    _CFG.clear()
    _APPLIED.clear()
    # A GCP project is required for refresh to do anything; default it on.
    _CFG["gcp_project_id"] = "proj-test"
    _CFG.update(cfg)


def _run_refresh(rows=None):
    return asyncio.run(svc.refresh_rancher_firewall(_FakeDB(rows or [])))


def test_merge_dedup_and_sorted():
    _reset(rancher_allowed_source_cidrs="203.0.113.4/32, 10.0.0.0/24",
           rancher_ui_web_jump_enabled="1", rancher_ui_jumpoint_egress_ip="9.9.9.9")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4"), _Row("gke-b", "gcp", "5.6.7.8")])
    assert _APPLIED["source_cidrs"] == sorted([
        "203.0.113.4/32", "10.0.0.0/24", "1.2.3.4/32", "5.6.7.8/32", "9.9.9.9/32"])


def test_fail_closed_when_empty():
    _reset()  # no manual, no clusters, allow_open off
    _run_refresh(rows=[])
    # ensure_rancher_firewall is still called with [] (it deletes the rule → closed).
    assert _APPLIED["called"] is True
    assert _APPLIED["source_cidrs"] == []


def test_allow_open_opens_world_when_nothing_else():
    _reset(gcp_rancher_allow_open="1")
    _run_refresh(rows=[])
    assert _APPLIED["source_cidrs"] == ["0.0.0.0/0"]


def test_jumpoint_only_when_enabled_and_ip_set():
    # enabled but no IP → not included
    _reset(rancher_ui_web_jump_enabled="1")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert _APPLIED["source_cidrs"] == ["1.2.3.4/32"]

    # IP set but web jump DISABLED → not included
    _reset(rancher_ui_jumpoint_egress_ip="9.9.9.9")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert _APPLIED["source_cidrs"] == ["1.2.3.4/32"]

    # enabled AND IP set → included
    _reset(rancher_ui_web_jump_enabled="true", rancher_ui_jumpoint_egress_ip="9.9.9.9")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert _APPLIED["source_cidrs"] == sorted(["1.2.3.4/32", "9.9.9.9/32"])


def test_egress_ip_trimmed_and_slash32():
    _reset()
    _run_refresh(rows=[_Row("eks-a", "aws", "  1.2.3.4  "), _Row("blank", "gcp", "   ")])
    # whitespace trimmed, /32 appended, blank egress_ip skipped
    assert _APPLIED["source_cidrs"] == ["1.2.3.4/32"]


def test_noop_when_no_project():
    _reset()
    del _CFG["gcp_project_id"]
    result = _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert result.get("skipped")
    assert _APPLIED == {}  # ensure_rancher_firewall NOT called


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
    sys.exit(1 if failures else 0)
