"""Unit test: rancher_node_service.refresh_rancher_firewall must apply the MERGED
firewall source set — manual CSV CIDRs + dashboard-provisioned cluster egress /32s
+ the dashboard-managed Web-Jump Jumpoint /32 + the dashboard's OWN egress /32 —
deduped and sorted, while staying fail-closed and no-op safe.

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
import logging
import re
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


async def _fake_ensure_rancher_firewall(project_id, network, tag, source_cidrs, name,
                                        *, acme_open=False):
    _APPLIED["called"] = True
    _APPLIED["source_cidrs"] = list(source_cidrs)
    _APPLIED["name"] = name
    _APPLIED["acme_open"] = acme_open
    return {"name": name, "opened": bool(source_cidrs), "acme_open": acme_open}


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
    from web_dashboard.services import managed_node_service as mns
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


def test_dashboard_egress_cidr_merged():
    # The dashboard's own egress IP (bare) is normalized to /32 and merged in, so
    # the worker can reach the node's public IP to bootstrap + poll it.
    _reset(rancher_dashboard_egress_cidr="198.51.100.7")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert _APPLIED["source_cidrs"] == sorted(["198.51.100.7/32", "1.2.3.4/32"])

    # An explicit CIDR is honored as-is (not re-suffixed).
    _reset(rancher_dashboard_egress_cidr="198.51.100.0/24")
    _run_refresh(rows=[])
    assert _APPLIED["source_cidrs"] == ["198.51.100.0/24"]


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


def test_runner_source_cidr_merged_only_when_runner_transport():
    # transport=runner → the VPC connector's range joins the merge (GCE ingress
    # rules apply to internal traffic too, so the in-cloud API runner needs it).
    _reset(rancher_api_transport="runner", rancher_runner_source_cidr="10.8.0.0/28")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert _APPLIED["source_cidrs"] == sorted(["1.2.3.4/32", "10.8.0.0/28"])
    # direct transport → the connector range stays out.
    _reset(rancher_api_transport="direct", rancher_runner_source_cidr="10.8.0.0/28")
    _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")])
    assert _APPLIED["source_cidrs"] == ["1.2.3.4/32"]


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_warnings(fn):
    """Run fn while capturing log records; return warnings+.

    BOTH loggers are watched, because the allow-list sources live in
    ``managed_node_service`` (shared with the Portainer node) while the
    applied-outcome warnings live in ``rancher_node_service``. Watching only one
    would let a "stays closed"-class regression through on whichever side it moved
    to — which is the exact failure these tests exist to catch.
    """
    h = _LogCapture()
    loggers = [svc.logger, mns.logger]
    old = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        for lg, lvl in old:
            lg.setLevel(lvl)
            lg.removeHandler(h)
    return [r for r in h.records if r.levelno >= logging.WARNING]


def test_no_stays_closed_warning_when_merged_nonempty():
    # Regression: an empty manual CSV used to make _allowed_cidrs() warn
    # "firewall stays closed" on EVERY refresh, even when the MERGED set
    # (cluster /32s, Jumpoint, dashboard egress) was non-empty and the firewall
    # actually opened. The warning must key on the FINAL merged set.
    _reset()  # manual CSV empty, allow_open off
    warnings = _capture_warnings(lambda: _run_refresh(rows=[_Row("eks-a", "aws", "1.2.3.4")]))
    assert _APPLIED["source_cidrs"] == ["1.2.3.4/32"]
    assert not any("stays closed" in r.getMessage() for r in warnings)


def test_stays_closed_warning_when_merged_empty():
    # The warning still fires when the merged set really IS empty (fail-closed).
    _reset()
    warnings = _capture_warnings(lambda: _run_refresh(rows=[]))
    assert _APPLIED["source_cidrs"] == []
    assert any("stays closed" in r.getMessage() for r in warnings)


def test_world_open_warning_fires_from_refresh():
    _reset(gcp_rancher_allow_open="1")
    warnings = _capture_warnings(lambda: _run_refresh(rows=[]))
    assert _APPLIED["source_cidrs"] == ["0.0.0.0/0"]
    assert any("0.0.0.0/0" in r.getMessage() for r in warnings)


def test_firewall_status_logs_no_warnings():
    # Read-only status must stay silent (it used to warn via _allowed_cidrs()).
    _reset()
    out = {}
    warnings = _capture_warnings(lambda: out.update(svc.firewall_status(_FakeDB([]))))
    assert out["merged"] == [] and out["opened"] is False
    assert not warnings


def _run_ensure_egress(detected_ip: str):
    """Drive _ensure_dashboard_egress_cidr with a stubbed detector."""
    orig = svc._detect_egress_ip

    async def fake():
        return detected_ip
    svc._detect_egress_ip = fake
    try:
        return asyncio.run(svc._ensure_dashboard_egress_cidr())
    finally:
        svc._detect_egress_ip = orig


def test_egress_containment_keeps_operator_pool():
    # Corp proxies egress from a POOL: an operator-set CIDR that CONTAINS the
    # detected IP must be kept — clobbering it with this connection's /32 would
    # drop the next connection (per-destination pool hashing).
    _reset(rancher_dashboard_egress_cidr="104.28.182.0/24")
    assert _run_ensure_egress("104.28.182.70") == "104.28.182.0/24"
    assert _CFG["rancher_dashboard_egress_cidr"] == "104.28.182.0/24"  # unchanged


def test_egress_detection_outside_pool_replaces():
    # A detected IP OUTSIDE the stored CIDR = the egress genuinely moved → track it.
    _reset(rancher_dashboard_egress_cidr="104.28.182.0/24")
    assert _run_ensure_egress("9.9.9.9") == "9.9.9.9/32"
    assert _CFG["rancher_dashboard_egress_cidr"] == "9.9.9.9/32"


def test_egress_detection_failure_keeps_existing():
    _reset(rancher_dashboard_egress_cidr="104.28.182.0/24")
    assert _run_ensure_egress("") == "104.28.182.0/24"
    assert _CFG["rancher_dashboard_egress_cidr"] == "104.28.182.0/24"


def _run_reapply(detected_ip: str, rows=None):
    """Drive reapply_firewall with a stubbed egress detector (never a real probe)."""
    orig = svc._detect_egress_ip

    async def fake():
        return detected_ip
    svc._detect_egress_ip = fake
    try:
        return asyncio.run(svc.reapply_firewall(_FakeDB(rows or [])))
    finally:
        svc._detect_egress_ip = orig


def test_reapply_applies_the_manual_cidr_the_operator_just_added():
    # The bug this exists for: adding your browser's public IP in Settings writes
    # CONFIG. Nothing recomputes the cloud rule, and the Settings readout shows the
    # set that WOULD be applied — so the IP looks allowed while the node stays shut.
    _reset(rancher_allowed_source_cidrs="203.0.113.4/32")
    out = _run_reapply("198.51.100.7")
    assert _APPLIED["called"] is True
    assert "203.0.113.4/32" in _APPLIED["source_cidrs"]
    assert out["opened"] is True
    assert out["detected_egress_ip"] == "198.51.100.7/32"


def test_reapply_reports_what_changed():
    # before/after are both COMPUTED sets, so the diff only shows sources this run
    # discovered — here, a dashboard egress that moved. A manual CIDR saved in
    # Settings is already in `before` (config is the input to both), which is why the
    # UI also prints the whole merged set: that is what tells the operator their own
    # IP is now on the rule.
    _reset(rancher_allowed_source_cidrs="203.0.113.4/32",
           rancher_dashboard_egress_cidr="9.9.9.9/32")
    out = _run_reapply("198.51.100.7")
    assert out["before"] == sorted(["203.0.113.4/32", "9.9.9.9/32"])
    assert out["added"] == ["198.51.100.7/32"] and out["removed"] == ["9.9.9.9/32"]
    assert out["changed"] is True
    assert "203.0.113.4/32" in out["merged"]


def test_reapply_is_idempotent_and_says_so():
    # Safe to click on a healthy node: the second run changes nothing and must not
    # dress that up as a repair.
    _reset(rancher_allowed_source_cidrs="203.0.113.4/32")
    _run_reapply("198.51.100.7")
    out = _run_reapply("198.51.100.7")
    assert out["added"] == [] and out["removed"] == [] and out["changed"] is False
    assert out["merged"] == sorted(["203.0.113.4/32", "198.51.100.7/32"])


def test_reapply_stays_fail_closed():
    # An empty allow-list still means CLOSED, and the caller is told plainly rather
    # than being handed a successful-looking no-op.
    _reset()
    out = _run_reapply("")
    assert out["merged"] == [] and out["opened"] is False


def test_generate_admin_password_strong_and_distinct():
    # Rancher requires ≥12 chars + forbids reusing the bootstrap password, so the
    # generated one must be long, mixed-class, and different every call.
    import string
    a = svc._generate_admin_password()
    b = svc._generate_admin_password()
    assert len(a) >= 12 and a != b
    assert any(c.islower() for c in a) and any(c.isupper() for c in a)
    assert any(c.isdigit() for c in a) and any(c in string.punctuation for c in a)


# ── ACME: the port-80 opening is separate from the source set ─────────────────
# A TLS-inspecting proxy verifies the ORIGIN certificate, so a self-signed node is
# unreachable from a browser whatever the allow-list says. The fix is a real
# certificate, and its HTTP-01 challenge cannot be source-restricted -- Let's
# Encrypt validates from addresses it does not publish. What must NOT happen is
# that widening for the challenge also widens 443, so these pin the two apart.

def test_acme_off_by_default_leaves_port_80_closed():
    _reset(rancher_allowed_source_cidrs="203.0.113.4/32")
    _run_refresh(rows=[])
    assert _APPLIED["acme_open"] is False


def test_acme_domain_opens_http01_without_touching_the_source_set():
    _reset(rancher_allowed_source_cidrs="203.0.113.4/32",
           rancher_acme_domain="rancher.example.com")
    _run_refresh(rows=[])
    assert _APPLIED["acme_open"] is True
    # 0.0.0.0/0 must NOT have leaked into the set that governs 443.
    assert _APPLIED["source_cidrs"] == ["203.0.113.4/32"]


def test_acme_open_does_not_defeat_fail_closed():
    # An empty merged set still closes the management ports. ACME opening 80 for a
    # challenge is not "the node is reachable" -- `opened` stays False.
    _reset(rancher_acme_domain="rancher.example.com")
    res = _run_refresh(rows=[])
    assert _APPLIED["source_cidrs"] == []
    assert res["opened"] is False
    assert _APPLIED["acme_open"] is True


def test_acme_domain_is_normalised_and_reported_in_status():
    _reset(rancher_acme_domain="  Rancher.Example.COM  ")
    assert svc._acme_domain() == "rancher.example.com"
    status = svc.firewall_status(_FakeDB([]))
    assert status["acme_domain"] == "rancher.example.com"
    assert status["acme_http01_open"] is True


def test_container_args_carry_acme_domain_only_when_set():
    _reset()
    assert svc._container_args() == ()
    _reset(rancher_acme_domain="rancher.example.com")
    assert svc._container_args() == ("--acme-domain", "rancher.example.com")


# ── the DNS pre-flight: a wrong A record must fail LOUDLY, before the wait ────

def _run_check(domain, external_ip, resolved):
    """Run check_acme_dns with name resolution stubbed to return ``resolved``.

    ``socket.getaddrinfo`` is what ``loop.getaddrinfo`` delegates to in an
    executor, so stubbing it there leaves the real async path under test.
    """
    import socket
    mns = svc.managed_node_service

    def _fake(host, port, *a, **k):
        if isinstance(resolved, Exception):
            raise resolved
        return [(socket.AF_INET, None, None, None, (ip, 0)) for ip in resolved]

    real = socket.getaddrinfo
    socket.getaddrinfo = _fake
    try:
        return asyncio.run(mns.check_acme_dns(mns.RANCHER, domain, external_ip))
    finally:
        socket.getaddrinfo = real


# ── asserting on the DNS pre-flight message ──────────────────────────────────
# These parse the hostnames and addresses OUT of the message and compare them by
# EQUALITY, rather than asking whether the message contains a given substring.
# Two reasons, and they happen to agree:
#
#   * containment is too weak. Asking whether the message merely CONTAINS the
#     domain also passes on one that only ever mentions a longer name the domain
#     happens to be a prefix of -- which would tell the operator nothing about
#     the record they actually have to create.
#   * CodeQL reads a hostname literal on either side of `in` as an incomplete URL
#     sanitization check, wherever it appears. Equality is the form it asks for.
#
# Pinning the WHOLE set also catches the message naming some other host as well
# as the right one, which a per-item check would wave through.
_HOST_RE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_ADDR_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")


def _hosts_named(msg):
    return set(_HOST_RE.findall(msg))


def _addrs_named(msg):
    return set(_ADDR_RE.findall(msg))


def test_acme_dns_ok_when_record_points_at_the_node():
    assert _run_check("rancher.example.com", "40.78.191.25", ["40.78.191.25"]) == ""


def test_acme_dns_names_the_record_to_create_when_unresolvable():
    msg = _run_check("rancher.example.com", "40.78.191.25", OSError("NXDOMAIN"))
    assert "does not resolve" in msg
    # The message has to carry BOTH halves of the record the operator must create,
    # or it is just another "it didn't work".
    assert _hosts_named(msg) == {"rancher.example.com"}
    assert _addrs_named(msg) == {"40.78.191.25"}


def test_acme_dns_rejects_a_record_pointing_elsewhere():
    msg = _run_check("rancher.example.com", "40.78.191.25", ["203.0.113.9"])
    # Both addresses, so the operator can see what the record says versus where
    # the node actually is -- naming only one of them explains nothing.
    assert _addrs_named(msg) == {"203.0.113.9", "40.78.191.25"}
    assert _hosts_named(msg) == {"rancher.example.com"}
    assert "does not resolve" not in msg


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
