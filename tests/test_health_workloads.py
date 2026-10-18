"""Health: workload credentials.

What these pin:

- a Workload Credentials lease in failure backoff is failing; an expired one not yet
  renewed is degraded; one never issued is unknown;
- a ServiceAccount token whose vault holds the placeholder is failing, a PRA Vault mirror
  that is not linked is failing, a missing mirror is degraded, and a read that fails is
  unknown with no exception text;
- a SPIRE lab's admin SVID expired / inside a day, a failed k3s link or federation;
- an agent cell's token revoked or expired, or inside a day;
- integration tokens: only ones in use are counted, by name, never by owner;
- only enabled sections are collected, and nothing reads users or roles.

Uses a real temp SQLite database; the Password Safe read is stubbed.

Run: python tests/test_health_workloads.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="health-wl-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-health-workload-tests")

from web_dashboard.database import (AgentCell, Base, K8sCluster,  # noqa: E402
                                    PersonalAccessToken, SessionLocal, SpireLab, engine)
from web_dashboard.utils.clock import utcnow  # noqa: E402
from web_dashboard.services import feature_flags  # noqa: E402
from web_dashboard.services.health.collectors import workloads as W  # noqa: E402

Base.metadata.create_all(bind=engine)
NOW = datetime(2026, 10, 12, 12, 0, 0)


def _lease(**kw):
    base = dict(payload="enc", issued_at=NOW - timedelta(minutes=10),
                expires_at=NOW + timedelta(minutes=50), last_error=None,
                consecutive_failures=0, cooldown_until=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── pure ──────────────────────────────────────────────────────────────────────

def test_lease_states():
    assert W.lease_check(None, now=NOW)["status"] == "unknown"
    assert W.lease_check(_lease(), now=NOW)["status"] == "ok"
    failing = W.lease_check(_lease(consecutive_failures=3, last_error="403 secret not found",
                                   cooldown_until=NOW + timedelta(minutes=20)), now=NOW)
    assert failing["status"] == "fail" and "Next attempt in 20 min" in failing["detail"]
    stale = W.lease_check(_lease(expires_at=NOW - timedelta(minutes=5)), now=NOW)
    assert stale["status"] == "warn"
    assert W.lease_check(_lease(payload=None), now=NOW)["status"] == "unknown"


def test_token_sync_states():
    by = lambda checks: {c["key"]: c["status"] for c in checks}  # noqa: E731
    assert by(W.token_sync_checks({"vault_has_real_credential": True, "linked": True})) == {
        "vault": "ok", "pra": "ok"}
    assert by(W.token_sync_checks({"vault_has_real_credential": False, "linked": True}))[
        "vault"] == "fail"
    assert by(W.token_sync_checks({"vault_has_real_credential": True, "linked": False,
                                   "parent_exists": True}))["pra"] == "fail"
    assert by(W.token_sync_checks({"vault_has_real_credential": True,
                                   "note": "no PRA Vault Token account"}))["pra"] == "warn"
    unread = W.token_sync_checks(None)
    assert unread[0]["status"] == "unknown" and "log" in unread[0]["detail"]


def _lab(**kw):
    base = dict(status="available", admin_svid_expires_at=NOW + timedelta(days=3),
                trust_bundle_pem="-----BEGIN CERTIFICATE-----", k8s_status=None,
                k8s_error_message=None, federation_status=None, federation_error=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_spire_lab():
    ok = {c["key"]: c["status"] for c in W.spire_checks(_lab(), now=NOW)}
    assert ok == {"svid": "ok", "bundle": "ok"}, ok
    soon = W.spire_checks(_lab(admin_svid_expires_at=NOW + timedelta(hours=3)), now=NOW)
    assert soon[0]["status"] == "warn"
    gone = W.spire_checks(_lab(admin_svid_expires_at=NOW - timedelta(hours=1)), now=NOW)
    assert gone[0]["status"] == "fail"
    broken = {c["key"]: c for c in W.spire_checks(_lab(
        k8s_status="failed", k8s_error_message="join token rejected",
        federation_status="failed", federation_error="bundle endpoint refused"), now=NOW)}
    assert broken["k8s"]["status"] == "fail" and broken["k8s"]["detail"] == "join token rejected"
    assert broken["federation"]["status"] == "fail"
    building = W.spire_checks(_lab(status="provisioning"), now=NOW)
    assert [c["key"] for c in building] == ["state"]


def _cell(**kw):
    base = dict(status="available", error_message=None, pat_revoked_at=None,
                pat_expires_at=NOW + timedelta(hours=40))
    base.update(kw)
    return SimpleNamespace(**base)


def test_agent_cell():
    assert [c["status"] for c in W.agent_cell_checks(_cell(), now=NOW)] == ["ok", "ok"]
    assert W.agent_cell_checks(_cell(pat_expires_at=NOW + timedelta(hours=5)),
                               now=NOW)[1]["status"] == "warn"
    assert W.agent_cell_checks(_cell(pat_expires_at=NOW - timedelta(hours=1)),
                               now=NOW)[1]["status"] == "fail"
    revoked = W.agent_cell_checks(_cell(pat_revoked_at=NOW - timedelta(hours=1)), now=NOW)
    assert revoked[1]["status"] == "fail" and "Revoked" in revoked[1]["detail"]
    assert W.agent_cell_checks(_cell(error_message="vm deploy failed"), now=NOW)[1][
        "status"] == "fail"


def _pat(name, used_days_ago, expires_in, active=True, user_id="u-secret-owner"):
    return SimpleNamespace(
        name=name, is_active=active, user_id=user_id,
        last_used_at=None if used_days_ago is None else NOW - timedelta(days=used_days_ago),
        expires_at=None if expires_in is None else NOW + expires_in)


def test_integration_tokens():
    assert W.pat_item([_pat("idle", None, timedelta(days=2))], now=NOW) is None
    assert W.pat_item([_pat("old", 90, timedelta(days=2))], now=NOW) is None
    fine = W.pat_item([_pat("ci", 1, None), _pat("mcp", 2, timedelta(days=60))], now=NOW)
    assert fine.status == "ok" and "2 token(s)" in fine.checks[0]["detail"]
    soon = W.pat_item([_pat("github-actions", 1, timedelta(days=2)),
                       _pat("mcp", 2, timedelta(days=60))], now=NOW)
    detail = soon.checks[0]["detail"]
    assert soon.status == "warn" and "github-actions (2 d)" in detail
    assert "u-secret-owner" not in detail, "a token's owner was reported"


# ── collection ────────────────────────────────────────────────────────────────

class _Flags:
    def __init__(self, *on):
        self.on = set(on)

    def __enter__(self):
        self.real = feature_flags.enabled
        feature_flags.enabled = lambda flag, default=False: flag in self.on

    def __exit__(self, *exc):
        feature_flags.enabled = self.real


def test_collect_respects_flags_and_reads_password_safe_once_per_cluster():
    from web_dashboard.services import ps_k8s_token_service
    db = SessionLocal()
    cluster = K8sCluster(id=str(uuid.uuid4()), cloud="aws", name="prod-eks", status="active",
                         source="registered", ps_token_account_id="41", created_by="t")
    plain = K8sCluster(id=str(uuid.uuid4()), cloud="aws", name="no-token", status="active",
                       source="registered", created_by="t")
    lab = SpireLab(id=str(uuid.uuid4()), name="td-lab", trust_domain="lab.example",
                   status="available", created_by="t")
    cell = AgentCell(id=str(uuid.uuid4()), name="cell-1", status="available",
                     pat_expires_at=utcnow() + timedelta(hours=30))
    pat = PersonalAccessToken(id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), name="ci",
                              token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                              last_used_at=utcnow(), is_active=True)
    db.add_all([cluster, plain, lab, cell, pat])
    db.commit()
    calls = []
    real = ps_k8s_token_service.sync_status

    async def fake(_db, cluster_id):
        calls.append(cluster_id)
        raise RuntimeError("PS said 500 with a body")
    try:
        ps_k8s_token_service.sync_status = fake
        with _Flags("password_safe_enabled", "spire_lab_enabled"):
            items = {i.object_id: i for i in asyncio.run(W.collect(db))}
    finally:
        ps_k8s_token_service.sync_status = real
    assert calls == [cluster.id], calls
    token = items[f"k8s-token:{cluster.id}"]
    assert token.status == "unknown"
    assert all("500" not in c["detail"] for c in token.checks), "exception text leaked"
    assert f"spire:{lab.id}" in items
    assert f"agentcell:{cell.id}" not in items, "a disabled section was collected"
    assert "pats" in items
    assert not any(k.startswith("lease:") for k in items), "a lease was reported while off"
    db.close()


def test_registered_and_identity_free():
    from web_dashboard.services.health import registry
    c = registry.get("workloads")
    assert c is not None and "aiops" in c.personas and "spire_lab_enabled" in c.flags
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "health", "collectors",
                            "workloads.py"), encoding="utf-8").read()
    for forbidden in ("list_users", "list_roles", "roleAssignments", "User)", ".user_id"):
        assert forbidden not in src, f"workloads reads {forbidden}"


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
