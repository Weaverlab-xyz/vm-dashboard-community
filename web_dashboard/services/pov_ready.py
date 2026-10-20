"""Is this POV ready to demo right now? One green/red answer before a customer call.

Every signal it combines already existed, on a different tab or a different page: the
setup ladder (``pov_setup_steps``), the runstate the reconcile last read, the Health page's
rows for this POV's own tenants, the PRA→Password Safe link count, the Gateway's live
status, share-link and auto-delete dates, the spend cap, and the instance preflight. An SE
checked them one by one, or found out which one was red in front of the customer.

**Two halves, and the split is what keeps the page fast.**

:func:`describe` is pure: it reads the serialised row, the stored health rows and the last
live run, and makes no call of any kind. That is what the ``GET`` serves, so opening the
POV page never waits on a customer's appliance.

:func:`run_live` is the job body. It refreshes the inputs -- this platform's reconcile, the
health areas a demo leans on, the PRA→PS link check, the Gateway's live status and the
instance preflight -- and stores what only it can know (the Gateway answer, the preflight,
any step that failed) on the row under :data:`RUN_KEY`. Nothing it does writes to a tenant:
``pov_pra_ps_link.check`` and the health collectors read, and the preflight registry
deliberately excludes the notification test that would post to a channel.

Statuses are the health vocabulary -- ok / warn / fail / unknown, worst wins, unknown never
outranks a real answer -- so the badge means what the Health page's badges mean.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .health import core
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)

# Where the last live run is stored, in PovEnvironment.extra_data.
RUN_KEY = "ready_run"
JOB_TYPE = "pov_env_ready_check"
# A second run inside this window is refused, the same floor the Health page's Run now has
# (health.runner.RUN_NOW_MIN_SECONDS): a live run reads a customer's appliance, and a
# button mashed before a call should not turn into a burst against it.
RUN_MIN_SECONDS = 60
# How far past the demo time a deadline still counts as "during the demo".
DEMO_LENGTH = timedelta(hours=2)
# The health areas a demo leans on, refreshed by a live run. Run through
# health.runner.run_now, which applies its own per-area floor.
HEALTH_AREAS = ("beyondtrust", "pra_gateways", "password_safe", "entitle", "epml")

_TENANT_KEYS = (("pra", "pra_tenant_id", "PRA tenant"),
                ("password_safe", "ps_tenant_id", "Password Safe tenant"))


def _when(value) -> Optional[datetime]:
    return core.parse_when(value) if isinstance(value, str) else value


def _iso(value: Optional[datetime]) -> str:
    return value.isoformat() + "Z" if value else ""


def _check(key: str, label: str, status: str, detail: str, *,
           checked_at: str = "", href: str = "") -> dict:
    out = core.check(key, label, status, detail)
    out["checked_at"] = checked_at or ""
    out["href"] = href
    return out


def last_run(env) -> dict:
    """The stored result of the last live run, or {}."""
    run = (getattr(env, "metadata_dict", None) or {}).get(RUN_KEY)
    return run if isinstance(run, dict) else {}


# How long before the demo a warm-up's ready check runs, and the least time it gives the
# lab to come back after the start. See :func:`schedule_after_warmup`.
WARMUP_CHECK_BEFORE = timedelta(minutes=5)
WARMUP_CHECK_MIN_DELAY = timedelta(minutes=5)


def _active_jobs(db, environment_id: str) -> list:
    from ..database import Job
    from . import job_service
    rows = (db.query(Job)
              .filter(Job.job_type == JOB_TYPE,
                      Job.status.in_(job_service.ACTIVE_STATUSES)).all())
    return [r for r in rows if r.metadata_dict.get("environment_id") == environment_id]


def job_in_flight(db, environment_id: str, now: Optional[datetime] = None) -> str:
    """A queued or running ready check for this POV that is due now, or "".

    A check BOOKED for later (``scheduled_for`` in the future, as a warm-up books one) is
    not in flight: counting it would grey out "Check now" until the booking ran.
    """
    now = now or utcnow()
    return next((r.id for r in _active_jobs(db, environment_id)
                 if r.scheduled_for is None or r.scheduled_for <= now), "")


def booked_for(db, environment_id: str, now: Optional[datetime] = None) -> str:
    """When the next booked ready check runs (ISO, UTC), or "" when none is booked."""
    now = now or utcnow()
    times = sorted(r.scheduled_for for r in _active_jobs(db, environment_id)
                   if r.scheduled_for is not None and r.scheduled_for > now)
    return _iso(times[0]) if times else ""


def schedule_after_warmup(db, env, *, demo_at: Optional[datetime],
                          now: Optional[datetime] = None) -> str:
    """Book a ready check for a POV a warm-up just started. Returns its job id, or "".

    It runs :data:`WARMUP_CHECK_BEFORE` the demo -- so the badge an SE opens the POV to is
    a few minutes old, not from before the lab woke -- but never sooner than
    :data:`WARMUP_CHECK_MIN_DELAY` from now, which is the lab's time to resume: a check
    run straight after the start would read a lab still coming up and report it amber.
    Nothing is booked when one is already queued or booked for this POV.
    """
    from . import job_service
    now = now or utcnow()
    if _active_jobs(db, env.id):
        return ""
    when = now + WARMUP_CHECK_MIN_DELAY
    if demo_at is not None:
        when = max(when, demo_at - WARMUP_CHECK_BEFORE)
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by="pov-warmup",
                                 workgroup=env.workgroup, scheduled_for=when,
                                 metadata={"environment_id": env.id})
    return job.id


def throttled(env, now: Optional[datetime] = None) -> int:
    """Seconds until another live run is allowed; 0 when it is."""
    started = _when((last_run(env) or {}).get("started_at"))
    if not started:
        return 0
    left = RUN_MIN_SECONDS - int(((now or utcnow()) - started).total_seconds())
    return max(0, left)


# ── the pure half ────────────────────────────────────────────────────────────

def _environment(row: dict) -> dict:
    status = row.get("status") or ""
    if status == "active":
        return _check("environment", "Environment", core.OK, "Provisioned and active.")
    if status == "provisioning":
        return _check("environment", "Environment", core.WARN, "Still provisioning.")
    return _check("environment", "Environment", core.FAIL,
                  f"The POV is {status or 'in an unknown state'}."
                  + (f" {row['error_message']}" if row.get("error_message") else ""))


def _power(row: dict) -> dict:
    seen = row.get("platform_seen_at") or ""
    if row.get("platform_missing"):
        return _check("power", "Power", core.FAIL,
                      "The platform no longer lists this environment.", checked_at=seen)
    state = (row.get("runstate") or "").lower()
    note = " The platform was rate-limiting the last read." if row.get("rate_limited") else ""
    if state == "running":
        return _check("power", "Power", core.OK, "Running." + note, checked_at=seen)
    if state in ("suspended", "stopped", "halted"):
        return _check("power", "Power", core.WARN,
                      f"{state.capitalize()}. Start it before the demo: a lab can take "
                      f"several minutes to come back." + note, checked_at=seen)
    if state == "busy":
        return _check("power", "Power", core.UNKNOWN,
                      "The platform is changing its state." + note, checked_at=seen)
    return _check("power", "Power", core.UNKNOWN,
                  "The platform has not reported a runstate yet." + note, checked_at=seen)


def _setup(row: dict) -> dict:
    setup = row.get("setup") or {}
    if setup.get("busy"):
        return _check("setup", "Setup", core.UNKNOWN, "A setup job is running.")
    nxt = setup.get("next_label") or ""
    if not nxt:
        return _check("setup", "Setup", core.OK, "Every setup step is done or configured.")
    detail = f"Not finished: next is {nxt}."
    if setup.get("next_detail"):
        detail += f" {setup['next_detail']}"
    return _check("setup", "Setup", core.WARN, detail)


def _wiring(row: dict) -> Optional[dict]:
    vms = row.get("vms")
    if vms is None:
        return None
    broken = [v.get("name") or v.get("id") or "?" for v in vms if v.get("wiring_error")]
    if not broken:
        return None
    shown = ", ".join(broken[:5]) + (f" and {len(broken) - 5} more" if len(broken) > 5 else "")
    return _check("wiring", "Guest wiring", core.FAIL,
                  f"{len(broken)} guest(s) have a wiring error: {shown}. See the VMs tab.")


def _health_check(key: str, label: str, hc: Optional[dict], missing: str) -> dict:
    if not hc:
        return _check(key, label, core.UNKNOWN, missing)
    bad = [c for c in (hc.get("checks") or []) if c.get("status") != core.OK]
    detail = "; ".join(f"{c.get('label')}: {c.get('detail')}" for c in bad) or \
        "; ".join(c.get("detail") or "" for c in (hc.get("checks") or [])) or "OK."
    return _check(key, label, hc.get("status") or core.UNKNOWN, detail,
                  checked_at=hc.get("checked_at") or "", href=hc.get("href") or "")


def _tenants(row: dict, health: dict) -> list:
    out = []
    for kind, field, label in _TENANT_KEYS:
        tenant_id = row.get(field) or ""
        if not tenant_id:
            continue
        out.append(_health_check(
            f"tenant:{kind}", label, health.get(("beyondtrust", f"{kind}:{tenant_id}")),
            "Not checked yet. Run the check, or wait for the Health page's next pass."))
    return out


def _area(health_areas: dict, area: str, key: str, label: str) -> Optional[dict]:
    """One check summarising a whole health area, or None when it has no rows."""
    rows = health_areas.get(area) or []
    if not rows:
        return None
    worst = core.overall([{"status": r.get("status")} for r in rows])
    bad = [r for r in rows if r.get("status") in (core.FAIL, core.WARN)]
    detail = ("; ".join(f"{r.get('name')}: {r.get('status')}" for r in bad[:4])
              or f"{len(rows)} item(s) healthy.")
    checked = max((r.get("checked_at") or "" for r in rows), default="")
    return _check(key, label, worst, detail, checked_at=checked, href="/health")


def _gateway(row: dict, run: dict) -> Optional[dict]:
    if not row.get("gateway_name"):
        return None
    live = run.get("gateway") or {}
    state = live.get("state") or ""
    at = run.get("finished_at") or ""
    if not state:
        return _check("gateway", "PRA Gateway", core.UNKNOWN,
                      "Its connection is a live question. Run the check.")
    status = {"connected": core.OK, "disconnected": core.FAIL,
              "missing": core.FAIL}.get(state, core.UNKNOWN)
    return _check("gateway", "PRA Gateway", status, live.get("detail") or state,
                  checked_at=at)


def _pra_ps(row: dict) -> Optional[dict]:
    if not (row.get("pra_tenant_id") and row.get("ps_tenant_id")):
        return None
    at = row.get("pra_ps_checked_at") or ""
    if not row.get("pra_ps_checked"):
        return _check("pra_ps", "PRA sees Password Safe", core.UNKNOWN,
                      "Never checked. Run the check.")
    matched = int(row.get("pra_ps_matched_count") or 0)
    linked = int(row.get("pra_ps_linked_count") or 0)
    if not matched:
        return _check("pra_ps", "PRA sees Password Safe", core.WARN,
                      "PRA sees none of this POV's Password Safe accounts, so credential "
                      "injection will not work in the demo.", checked_at=at)
    if linked < matched:
        return _check("pra_ps", "PRA sees Password Safe", core.WARN,
                      f"{matched - linked} of {matched} account(s) are not linked to a "
                      f"Jump Item yet.", checked_at=at)
    return _check("pra_ps", "PRA sees Password Safe", core.OK,
                  f"{linked} account(s) linked.", checked_at=at)


def _deadline(key: str, label: str, when: Optional[datetime], now: datetime,
              horizon: datetime, what: str) -> dict:
    stamp = when.strftime("%Y-%m-%d %H:%M UTC")
    if when <= now:
        return _check(key, label, core.FAIL, f"{what} {stamp}, which has passed.")
    if when <= horizon:
        return _check(key, label, core.WARN, f"{what} {stamp}, during or before the demo.")
    return _check(key, label, core.OK, f"{what} {stamp}.")


def _share(row: dict, now: datetime, horizon: datetime) -> Optional[dict]:
    if not row.get("share_url"):
        return None
    if row.get("share_expired"):
        return _check("share", "Share link", core.FAIL,
                      "The customer's link has expired. Re-share it.")
    when = _when(row.get("share_expires_at"))
    if not when:
        return _check("share", "Share link", core.OK, "Published.")
    return _deadline("share", "Share link", when, now, horizon, "Expires")


def _expiry(row: dict, now: datetime, horizon: datetime) -> Optional[dict]:
    when = _when(row.get("expires_at")) if row.get("expiry_enabled") else None
    if not when:
        return None
    return _deadline("expiry", "Auto-delete", when, now, horizon, "Deletes itself")


def _spend(row: dict) -> Optional[dict]:
    s = row.get("spend") or {}
    if not (row.get("spend_priced") and s.get("capped")):
        return None
    if s.get("over"):
        return _check("spend", "Spend cap", core.FAIL,
                      f"{s.get('summary', 'Over its cap')}. Raise the cap or it stays "
                      f"suspended.")
    if int(s.get("percent") or 0) >= int(s.get("warn_percent") or 100):
        return _check("spend", "Spend cap", core.WARN, s.get("summary") or "Near its cap.")
    return _check("spend", "Spend cap", core.OK, s.get("summary") or "Under its cap.")


def _preflight(run: dict) -> Optional[dict]:
    results = run.get("preflight")
    if not isinstance(results, list):
        return None
    failing = [r for r in results if r.get("ok") is False]
    at = run.get("finished_at") or ""
    if failing:
        return _check("preflight", "Instance integrations", core.FAIL,
                      "; ".join(f"{r.get('label')}: {r.get('detail')}" for r in failing),
                      checked_at=at, href="/health")
    probed = sum(1 for r in results if r.get("ok") is True)
    return _check("preflight", "Instance integrations", core.OK if probed else core.UNKNOWN,
                  f"{probed} integration(s) answered." if probed else
                  "Nothing on this instance could be probed.", checked_at=at)


def describe(row: dict, health: dict, health_areas: dict, run: dict, *,
             now: Optional[datetime] = None,
             demo_at: Optional[datetime] = None) -> dict:
    """The ready check for one POV. Pure: no session, no query, no call.

    ``row`` is ``api/pov._serialize`` output (with ``vms`` for the wiring check).
    ``health`` maps ``(area, object_id)`` to a stored health row and ``health_areas``
    maps an area to its rows, both as :func:`health_inputs` builds them. ``run`` is
    :func:`last_run`. ``demo_at`` (naive UTC) moves the "during the demo" window; it
    defaults to now.
    """
    now = now or utcnow()
    horizon = (demo_at or now) + DEMO_LENGTH
    checks = [_environment(row), _power(row), _setup(row)]
    checks += [c for c in (_wiring(row),) if c]
    checks += _tenants(row, health)
    if row.get("entitle_tenant_id"):
        checks += [c for c in (_area(health_areas, "entitle", "entitle", "Entitle"),) if c]
    checks += [c for c in (
        _area(health_areas, "epml", "epml", "EPM for Linux"),
        _gateway(row, run), _pra_ps(row),
        _share(row, now, horizon), _expiry(row, now, horizon), _spend(row),
        _preflight(run),
    ) if c]
    errors = [str(e) for e in (run.get("errors") or [])]
    return {
        "overall": core.overall(checks),
        "checks": checks,
        "counts": {s: sum(1 for c in checks if c["status"] == s) for s in core.STATUSES},
        "demo_at": _iso(demo_at),
        "last_run": {
            "started_at": run.get("started_at") or "",
            "finished_at": run.get("finished_at") or "",
            "job_id": run.get("job_id") or "",
            "errors": errors,
        },
        "retry_in_seconds": 0,
    }


def health_inputs(db, env) -> tuple:
    """The stored health rows :func:`describe` reads. One query; no calls."""
    from .health.runner import _loads
    from ..database import HealthCheck
    rows = db.query(HealthCheck).filter(HealthCheck.area.in_(HEALTH_AREAS)).all()
    by_key, by_area = {}, {}
    for r in rows:
        item = {"name": r.name, "status": r.status, "href": r.href or "",
                "checks": _loads(r.checks),
                "checked_at": _iso(r.checked_at)}
        by_key[(r.area, r.object_id)] = item
        by_area.setdefault(r.area, []).append(item)
    return by_key, by_area


# ── the live half ────────────────────────────────────────────────────────────

def _store(db, env, run: dict) -> None:
    meta = dict(env.metadata_dict or {})
    meta[RUN_KEY] = run
    env.metadata_dict = meta
    db.commit()


async def run_live(job_id: str, meta: dict) -> None:
    """The job body: refresh every input :func:`describe` reads, for one POV.

    Each step is best-effort and recorded, never raised: a PRA appliance that does not
    answer is exactly the finding an SE needs before a call, and it must not stop the
    spend cap or the share link being reported.
    """
    from ..database import SessionLocal
    from . import (feature_flags, job_service, pov_env_service, pov_gateway,
                   pov_pra_ps_link, pov_reconcile, preflight)
    from .health import registry, runner

    db = SessionLocal()
    try:
        env = pov_env_service.get(db, meta.get("environment_id", ""))
        if env is None:
            job_service.set_failed(db, job_id, "the POV environment row is gone")
            return
        run = {"job_id": job_id, "started_at": _iso(utcnow()), "errors": []}
        _store(db, env, run)

        def note(pct: int, line: str) -> None:
            job_service.update_progress(db, job_id, pct, line)

        note(10, "Reading the platform's runstate…")
        try:
            await pov_reconcile.reconcile(db, env.platform, job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            run["errors"].append(f"platform read: {core.text(exc, 300)}")

        for i, area in enumerate(HEALTH_AREAS):
            collector = registry.get(area)
            if collector is None or not runner.enabled(collector):
                continue
            note(25 + i * 8, f"Checking {collector.label}…")
            try:
                result = await runner.run_now(db, area)
                if result and result.get("ok") is False:
                    run["errors"].append(f"{collector.label}: the check could not run; "
                                         f"see the Health page.")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                run["errors"].append(f"{collector.label}: {core.text(exc, 300)}")

        db.refresh(env)
        if env.pra_tenant_id and env.ps_tenant_id and feature_flags.enabled("pra_enabled"):
            note(70, "Checking which Password Safe accounts PRA sees…")
            try:
                await pov_pra_ps_link.check(db, env)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                run["errors"].append(f"PRA→Password Safe: {core.text(exc, 300)}")

        if env.gateway_name:
            note(80, "Asking PRA about the Gateway…")
            try:
                run["gateway"] = await pov_gateway.status(db, env)
            except Exception as exc:  # noqa: BLE001
                run["gateway"] = {"state": "unknown", "detail": core.text(exc, 300)}

        note(90, "Probing this instance's integrations…")
        try:
            run["preflight"] = [{"key": r.key, "label": r.label, "ok": r.ok,
                                 "detail": core.text(r.detail, 300)}
                                for r in await preflight.run_all() if r.configured]
        except Exception as exc:  # noqa: BLE001
            run["errors"].append(f"preflight: {core.text(exc, 300)}")

        run["finished_at"] = _iso(utcnow())
        db.refresh(env)
        _store(db, env, run)
        job_service.set_completed(db, job_id, {"environment_id": env.id,
                                               "errors": len(run["errors"])})
    finally:
        db.close()
