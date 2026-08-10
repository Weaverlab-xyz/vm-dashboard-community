"""Keep each PRA Vault token account in step with the ServiceAccount token Password
Safe rotates.

Password Safe owns the rotation schedule and there is no webhook, so this is a
**watermark reconciler**, not an event pipeline. That single choice is what makes missed
rotations, lost state, double rotations and a rotation landing mid-sync all fall out
correctly with no queue and no per-rotation bookkeeping: the persisted state describes
what PRA is known to hold, and any difference is reconciled.

    main._k8s_token_sync_loop  ──►  enqueue_sweep_if_due()   [app, gunicorn -w 2]
                                       └─ creates ONE `k8s_token_sync` job
    jobs_worker._claim_one     ──►  run()                    [worker, replicas: 3]
        per due cluster:  ps_api_service.rotate_pra_vault_token(source, target)
                              └─ checkout → PUT …/Credentials (UpdateSystemPassword)
                                   └─ Password Safe runs the "PRA Vault Token" plugin
                                        └─ PATCH /api/config/v1/vault/account/{id}

The loop only *enqueues*, for the reason ``expiry_reaper`` spells out: under
``gunicorn -w 2`` every task started in the app runs twice, and ``_claim_one``'s
``UPDATE ... WHERE status='pending'`` rowcount is the lock that makes a pass
single-flight across app workers and worker replicas alike, on SQLite as well as
PostgreSQL. It also puts the credential write in the worker with every other privileged
operation, and gives each pass a /jobs row with Live Output and cancel.

**The plaintext token never enters this module.** ``rotate_pra_vault_token`` checks out,
pushes and checks in inside one Password Safe session and returns only a short digest,
so nothing here can leak a credential into a job result or a JobLog row.

See docs/design/k8s-sa-token-rotation.md.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import Job, K8sCluster
from . import job_service

logger = logging.getLogger(__name__)

SYNC_JOB_TYPE = "k8s_token_sync"

# 20260101 init_db's DDL lock, 20260102 the audit chain, 20260103 the expiry enqueue.
_ENQUEUE_LOCK_ID = 20260104

_STATE_KEY = "k8s_token_sync_{cluster_id}"

# Clusters whose token may be synced. Reuses the reaper's mapping rather than repeating
# the strings: it fails safe by construction, so a status added elsewhere can only make
# this do less.
_SYNCABLE_STATES = frozenset({"registered", "managed", "awaiting_agent"})

# Checkouts per pass. A fleet-wide rotation must not become forty checkouts in one job;
# the remainder comes back next pass, oldest drift first.
_DEFAULT_MAX_PER_PASS = 5
# Consecutive failures before a cluster stops being attempted. A 403 from a missing
# Smart Rule will never fix itself, and retrying every interval forever both spams
# Password Safe's audit log and buries the one job row an operator would read.
_DEFAULT_MAX_FAILURES = 5


def _utcnow() -> datetime:
    """Naive UTC, matching ``Job.created_at``'s ``datetime.utcnow()`` — mixing an aware
    value into a comparison would let the session TimeZone decide the cutoff."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or default


def _cfg_bool(key: str, default: bool = False) -> bool:
    try:
        from . import config_service
        return config_service.get_bool(key, default)
    except Exception:
        from ..config import settings
        return bool(getattr(settings, key, default))


def _cfg_int(key: str, default: int) -> int:
    raw = str(_cfg(key, "")).strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def enabled() -> bool:
    return _cfg_bool("k8s_token_sync_enabled", True)


def sweep_interval_seconds() -> int:
    """Cadence, re-read live each tick so a Settings change lands on the next pass.
    Floored at 5 minutes: below that the Password Safe API cost starts to matter and
    ``LastChangeDate`` granularity stops rewarding it."""
    return max(5, _cfg_int("k8s_token_sync_interval_minutes", 15)) * 60


# ── per-cluster state ─────────────────────────────────────────────────────────

def get_state(cluster_id: str) -> dict:
    """``{synced_change, token_sha256, synced_at, tgt_change_at_push, pra_verified,
    state, fail_count, next_attempt_at, error}``.

    Nothing secret: ``token_sha256`` is a 12-hex prefix, which proves the value changed
    without being the value. A full digest would be needless exposure — this blob is read
    back by ``k8s_service._serialize`` and served by the clusters API."""
    try:
        from . import config_service
        raw = config_service.get(_STATE_KEY.format(cluster_id=cluster_id))
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(cluster_id: str, state: dict) -> None:
    from . import config_service
    config_service.set(_STATE_KEY.format(cluster_id=cluster_id), json.dumps(state))


def clear_state(cluster_id: str) -> None:
    try:
        from . import config_service
        config_service.delete(_STATE_KEY.format(cluster_id=cluster_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("clearing token-sync state for %s failed: %s", cluster_id, exc)


# ── scope ─────────────────────────────────────────────────────────────────────

def scope(db: Session) -> list:
    """Clusters this pass considers.

    Both Password Safe account ids are required — an unbound cluster is out of scope,
    not an error, and most clusters never use this. ``pra_jump_id`` too: with no tunnel
    nothing consumes the vaulted token, so pushing would be pointless Password Safe
    traffic. That does mean removing the tunnel silently stops syncing, which is
    correct — and ``deregister_pra_tunnel`` clears the watermark so a re-provisioned
    tunnel is not suppressed by stale state."""
    return (
        db.query(K8sCluster)
        .filter(K8sCluster.ps_token_account_id.isnot(None),
                K8sCluster.ps_pra_vault_account_id.isnot(None),
                K8sCluster.pra_jump_id.isnot(None),
                K8sCluster.status.in_(tuple(_SYNCABLE_STATES)))
        .all()
    )


# ── enqueue ───────────────────────────────────────────────────────────────────

def enqueue_sweep_if_due(db: Session, *, min_gap_seconds: Optional[int] = None) -> Optional[str]:
    """Create one ``k8s_token_sync`` job unless a pass is active or just ran.

    THREE layers, and the third is not redundant. ``ACTIVE_STATUSES`` is a liveness
    test, and a pass with nothing to sync finishes in well under a second — so whenever
    a worker replica claims inside the fraction of a second separating the two app
    workers' ticks, the first row is already ``completed`` before the second worker
    looks, and a duplicate is created. ``expiry_reaper`` measured exactly that: 5 of 55
    rows were pairs 0.13–0.4s apart before its recency term existed. **A liveness-based
    dedupe cannot hold when the work is instantaneous; it needs a recency term.**

    Duplicates matter more here than for a report-only sweep: two passes are two
    checkouts and two credential writes to the same account, possibly with different
    values.

    ``min_gap_seconds=0`` skips the recency check — the operator-facing force-sweep
    endpoint passes it, because a human who just pressed the button means now."""
    if not enabled():
        return None
    if min_gap_seconds is None:
        min_gap_seconds = max(30, sweep_interval_seconds() // 2)
    try:
        from ..database import _is_sqlite
        if not _is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _ENQUEUE_LOCK_ID})
        existing = (
            db.query(Job.id)
            .filter(Job.job_type == SYNC_JOB_TYPE,
                    Job.status.in_(job_service.ACTIVE_STATUSES))
            .first()
        )
        if existing:
            return None
        if min_gap_seconds > 0:
            floor = _utcnow() - timedelta(seconds=min_gap_seconds)
            recent = (
                db.query(Job.id)
                .filter(Job.job_type == SYNC_JOB_TYPE, Job.created_at >= floor)
                .first()
            )
            if recent:
                return None
        job = job_service.create_job(db, job_type=SYNC_JOB_TYPE, created_by="system")
        return job.id
    except Exception:
        logger.warning("could not enqueue a k8s token sync", exc_info=True)
        db.rollback()
        return None


# ── one cluster ───────────────────────────────────────────────────────────────

def _backoff_seconds(fail_count: int) -> int:
    """Exponential, capped at six hours."""
    return min(sweep_interval_seconds() * (2 ** max(0, fail_count)), 6 * 3600)


def _due(state: dict) -> bool:
    at = state.get("next_attempt_at")
    if not at:
        return True
    try:
        return _utcnow() >= datetime.fromisoformat(at)
    except Exception:  # noqa: BLE001
        return True


async def sync_cluster(db: Session, cluster_id: str, *, force: bool = False) -> dict:
    """Reconcile one cluster's PRA Vault copy with Password Safe's current token.

    The read/record ORDER is the correctness argument. ``d0`` — the source account's
    ``LastChangeDate`` read BEFORE the checkout — is what gets recorded on success,
    never a re-read afterwards. If Password Safe rotates between the read and the
    checkout we push token *B* while recording date *A*; the next pass sees B's date
    differ, re-checks-out, the hash matches, and the watermark advances: one wasted
    checkout, self-corrected. Recording a post-checkout re-read would instead store
    *B*'s date having pushed *A*'s token — a silent, permanent desync that no later pass
    can detect."""
    from . import ps_api_service
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not (row.ps_token_account_id and row.ps_pra_vault_account_id):
        return {"cluster_id": cluster_id, "skipped": "not registered"}

    state = get_state(cluster_id)
    if not force and not _due(state):
        return {"cluster_id": cluster_id, "skipped": "backing off",
                "next_attempt_at": state.get("next_attempt_at")}

    src = int(row.ps_token_account_id)
    tgt = int(row.ps_pra_vault_account_id)
    # Deliberately unguarded: a transport or auth failure here is not this cluster's
    # fault, so it propagates and fails the whole pass without charging any cluster's
    # backoff. Catching it per cluster would stagger forty recoveries over hours for one
    # blip.
    states = await ps_api_service.get_managed_account_states([src, tgt])

    source = states.get(str(src)) or {}
    target = states.get(str(tgt)) or {}
    if not source:
        # 404 by id AND absent from the collection scan: genuinely deleted. Sticky, not
        # charged to backoff, and the column is deliberately NOT cleared — a permissions
        # blip presenting as 404 must not silently erase the operator's binding.
        state.update({"state": "unregistered", "next_attempt_at": None,
                      "error": f"Password Safe managed account {src} no longer exists"})
        _save_state(cluster_id, state)
        return {"cluster_id": cluster_id, "state": "unregistered"}

    d0 = source.get("last_change_date") or ""

    # Verification of the PREVIOUS push, free: Password Safe is the only party that knows
    # whether the plugin's PATCH ran, and its change date on the target is the receipt.
    if state.get("pra_verified") == "pending" and state.get("tgt_change_at_push"):
        moved = (target.get("last_change_date") or "") != state["tgt_change_at_push"]
        state["pra_verified"] = "yes" if moved else "no"
        if not moved:
            fails = int(state.get("fail_count") or 0) + 1
            state.update({
                "state": "error", "fail_count": fails,
                "next_attempt_at": (_utcnow() + timedelta(
                    seconds=_backoff_seconds(fails))).isoformat(),
                "error": ("Password Safe accepted the new value but the PRA update did not "
                          "complete — check the Password Safe change log for the PRA Vault "
                          "Token account"),
            })
            _save_state(cluster_id, state)
            return {"cluster_id": cluster_id, "state": "error",
                    "pra_verified": "no", "error": state["error"]}

    if not force and d0 and d0 == state.get("synced_change"):
        state.update({"state": "ok", "error": "", "fail_count": 0,
                      "next_attempt_at": None})
        _save_state(cluster_id, state)
        return {"cluster_id": cluster_id, "changed": False, "state": "ok"}

    # The rotation-loop circuit breaker. If the source account has "Change Password After
    # Release" enabled, every sync triggers another rotation and this never settles — a
    # real cluster rotation per pass, with a dead-credential window each time.
    window_start = state.get("rate_window_start")
    count = int(state.get("rate_count") or 0)
    now = _utcnow()
    if window_start:
        try:
            if now - datetime.fromisoformat(window_start) > timedelta(hours=1):
                window_start, count = None, 0
        except Exception:  # noqa: BLE001
            window_start, count = None, 0
    max_per_hour = _cfg_int("k8s_token_sync_max_per_hour", 4)
    if count >= max_per_hour:
        state.update({
            "state": "error",
            "error": (f"refusing to sync: {count} pushes in the last hour exceeds "
                      f"k8s_token_sync_max_per_hour ({max_per_hour}). The source account "
                      f"most likely has 'Change Password After Release' enabled, which makes "
                      f"every sync trigger another rotation."),
            "next_attempt_at": (now + timedelta(hours=1)).isoformat(),
        })
        _save_state(cluster_id, state)
        logger.error("k8s token sync: rate limit tripped for cluster %s — check the "
                     "access policy on managed account %s", cluster_id, src)
        return {"cluster_id": cluster_id, "state": "error", "error": state["error"]}

    tgt_before = target.get("last_change_date") or ""
    try:
        pushed = await ps_api_service.rotate_pra_vault_token(
            source_account_id=src, target_account_id=tgt,
            duration_min=_cfg_int("k8s_token_sync_request_duration_min", 15),
            reason=f"k8s ServiceAccount token → PRA Vault ({row.name})",
            expect_target_platform=_cfg("k8s_ps_pravault_token_platform", "PRA Vault Token"))
    except Exception as exc:  # noqa: BLE001
        fails = int(state.get("fail_count") or 0) + 1
        stop = fails >= _cfg_int("k8s_token_sync_max_failures", _DEFAULT_MAX_FAILURES)
        state.update({
            "state": "error", "fail_count": fails, "error": str(exc)[:600],
            # The watermark is NOT advanced on failure — that is the difference between
            # "retries next pass" and "silently stale forever".
            "next_attempt_at": None if stop else (
                _utcnow() + timedelta(seconds=_backoff_seconds(fails))).isoformat(),
        })
        _save_state(cluster_id, state)
        logger.warning("k8s token sync failed for cluster %s (attempt %d%s): %s",
                       cluster_id, fails, ", giving up until a manual sync" if stop else "",
                       exc)
        return {"cluster_id": cluster_id, "state": "error", "error": str(exc)[:600],
                "fail_count": fails, "stopped": stop}

    digest = pushed.get("sha256") or ""
    # Reported, not used to skip the push. Skipping would mean knowing the value before
    # deciding, and checkout+push are one atomic call precisely so the plaintext never
    # crosses back into this module. A redundant push is idempotent; the digest is here
    # so an operator can see whether the value actually changed.
    value_unchanged = bool(digest) and digest == state.get("token_sha256")
    state.update({
        "synced_change": d0,
        "token_sha256": digest,
        "synced_at": now.isoformat(),
        "tgt_change_at_push": tgt_before,
        # Accepted, not necessarily reflected: Password Safe queues change operations, so
        # "pending" is the normal case and the next pass's read resolves it.
        "pra_verified": "pending",
        "state": "ok", "error": "", "fail_count": 0, "next_attempt_at": None,
        "rate_window_start": window_start or now.isoformat(),
        "rate_count": count + 1,
    })
    _save_state(cluster_id, state)
    return {"cluster_id": cluster_id, "changed": True, "pushed": True,
            "sha256": digest, "state": "ok", "value_unchanged": value_unchanged}


# ── one pass ──────────────────────────────────────────────────────────────────

async def sync_once(db: Session, *, job_id: str = "", cluster_id: str = "",
                    force: bool = False) -> dict:
    """One pass: poll every in-scope cluster's change date, push the ones that drifted."""
    from ..api.websocket import broadcast_progress
    started = _utcnow()
    if not enabled():
        return {"skipped": "k8s_token_sync_enabled is off", "scanned": 0}
    from . import ps_api_service
    if not ps_api_service.configured():
        return {"skipped": "Password Safe is not configured", "scanned": 0}

    rows = ([db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()]
            if cluster_id else scope(db))
    rows = [r for r in rows if r is not None]
    if not rows:
        return {"scanned": 0, "due": 0, "pushed": 0, "failed": 0,
                "started_at": started.isoformat(), "ended_at": _utcnow().isoformat()}

    # Oldest drift first, so a capped pass fixes the longest-broken tunnel first and the
    # report reads in triage order.
    def _staleness(r):
        st = get_state(r.id)
        return st.get("synced_at") or ""
    rows.sort(key=_staleness)

    cap = _cfg_int("k8s_token_sync_max_per_pass", _DEFAULT_MAX_PER_PASS)
    results, pushed, failed, deferred = [], 0, 0, 0
    for i, row in enumerate(rows):
        if pushed >= cap and not cluster_id:
            deferred += len(rows) - i
            break
        if job_id:
            await broadcast_progress(
                job_id, min(90, 10 + int(80 * (i + 1) / max(1, len(rows)))),
                f"Checking {row.name}…")
        try:
            out = await sync_cluster(db, row.id, force=force or bool(cluster_id))
        except Exception as exc:  # noqa: BLE001
            # One unreachable Password Safe fails the pass, not the cluster — see
            # sync_cluster. Anything else per-cluster is already captured in its state.
            logger.warning("k8s token sync: pass aborted at cluster %s: %s", row.id, exc)
            raise
        results.append(out)
        if out.get("pushed"):
            pushed += 1
        if out.get("state") == "error":
            failed += 1
    return {
        "started_at": started.isoformat(), "ended_at": _utcnow().isoformat(),
        "scanned": len(rows), "pushed": pushed, "failed": failed, "deferred": deferred,
        "due": sum(1 for r in results if r.get("changed")),
        "clusters": results,
    }


async def run(db: Session, *, job_id: str, meta: dict = None) -> None:
    """``k8s_token_sync`` job handler. Owns its terminal state and never raises."""
    meta = meta or {}
    job_service.set_running(db, job_id)
    try:
        result = await sync_once(db, job_id=job_id,
                                 cluster_id=str(meta.get("cluster_id") or ""),
                                 force=bool(meta.get("force")))
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s token sync pass failed")
        return
    if result.get("skipped"):
        job_service.update_progress(db, job_id, 100, f"Sync skipped: {result['skipped']}")
    else:
        job_service.update_progress(
            db, job_id, 100,
            f"Scanned {result.get('scanned', 0)}, pushed {result.get('pushed', 0)}, "
            f"{result.get('failed', 0)} failed")
    job_service.set_completed(db, job_id, {"token_sync": result})


def status() -> dict:
    """Feature status for the operator-facing endpoint."""
    return {"enabled": enabled(),
            "interval_minutes": sweep_interval_seconds() // 60,
            "max_per_pass": _cfg_int("k8s_token_sync_max_per_pass", _DEFAULT_MAX_PER_PASS),
            "max_per_hour": _cfg_int("k8s_token_sync_max_per_hour", 4)}
