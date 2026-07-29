"""Secret hygiene — age / staleness signal for stored secrets (community Phase 1).

Read-only, no rotation. Flags secrets that haven't changed in a while. The
"last-changed" clock is chosen per secret by the caller and passed in as
``changed_at``:

- **External-vault references** (``aws_sm://`` / ``azure_kv://`` / ``gcp_sm://`` /
  ``bt_safe://``): the vault's own last-changed / last-rotated timestamp, so a
  secret you rotate in the backend reads as fresh (not falsely stale).
- **DB-stored secrets** (or when the vault can't report a date): the dashboard's
  ``AppConfig.updated_at`` — stamped by ``config_service.set()`` on every write.

``score`` and ``summarize`` are pure (no DB / no vault I/O) so they unit-test
trivially — ``tests/test_secret_hygiene.py`` loads this file by path with no stubs at
all, so **module-level imports must stay stdlib-only**. :func:`collect` is the one
function here that touches the world, and it keeps that promise by importing inside
its body, the same way the routers do.

:func:`collect` exists because the notification scanner runs in the worker and a
service may not import from ``..api``, where this gathering used to live.
"""
from datetime import datetime
from typing import Iterable, Optional


# ── Secret registry ──────────────────────────────────────────────────────────
# Every secret the dashboard manages, regardless of which feature owns it. Kept here
# rather than in api/secrets.py so both the router and the worker-side scanner can
# read it without one importing the other. api/secrets.py re-exports these.

SECRET_REGISTRY: list = [
    # (config_service key, human-readable description)
    ("aws_secret_access_key",     "AWS Secret Access Key"),
    ("azure_client_secret",       "Azure Service Principal Client Secret"),
    ("azure_oauth_client_secret", "Azure OAuth App Client Secret"),
    ("gcp_service_account_json",  "GCP Service Account JSON Key"),
    ("pscli_client_secret",       "BeyondTrust ps-cli Client Secret"),
    ("bt_client_secret",          "BeyondTrust Privileged Remote Access Client Secret"),
    ("epml_pat",                  "BeyondTrust EPM-L Personal Access Token"),
    ("entitle_api_token",         "Entitle API Token"),
    ("entitle_api_key",           "Entitle Terraform Provider API Key"),
    ("proxmox_token_secret",      "Proxmox API Token Secret"),
    ("proxmox_password",          "Proxmox Password"),
    ("vsphere_password",          "vSphere Password"),
    ("hyperv_password",           "Hyper-V Password"),
    ("nutanix_password",          "Nutanix Password"),
    ("xcpng_password",            "XCP-ng Password"),
]

# Prefix → backend id — must match secrets_backend_service._EXT_PREFIXES keys
BACKEND_PREFIXES: dict = {
    "database":        "",
    "aws_sm":          "aws_sm://",
    "azure_kv":        "azure_kv://",
    "gcp_sm":          "gcp_sm://",
    "bt_secrets_safe": "bt_safe://",
}


def score(changed_at: Optional[datetime], max_age_days: int,
          now: Optional[datetime] = None) -> dict:
    """Age of a secret and whether it's stale.

    ``max_age_days <= 0`` disables the staleness check (never stale). A secret
    with no ``changed_at`` has ``age_days = None`` and is not stale.
    """
    now = now or datetime.utcnow()
    if changed_at is None:
        return {"age_days": None, "stale": False}
    age_days = (now - changed_at).days
    stale = bool(max_age_days and max_age_days > 0 and age_days >= max_age_days)
    return {"age_days": age_days, "stale": stale}


def summarize(items: Iterable[dict], max_age_days: int,
              now: Optional[datetime] = None) -> dict:
    """Roll up per-secret staleness.

    ``items`` is an iterable of ``{key, source, changed_at}`` where ``source`` is
    ``"database"`` or a backend id (``"aws_sm"`` …) and ``changed_at`` is a
    ``datetime`` (or ``None`` if unknown). Returns ``{enabled, max_age_days,
    items, stale_count, stale_keys}``; each returned item adds ``age_days`` and
    ``stale``. Items are sorted oldest-first so the UI leads with the worst.
    """
    now = now or datetime.utcnow()
    out = []
    for it in items:
        s = score(it.get("changed_at"), max_age_days, now=now)
        ca = it.get("changed_at")
        out.append({
            "key": it["key"],
            "source": it.get("source", "database"),
            "changed_at": ca.isoformat() if isinstance(ca, datetime) else None,
            "age_days": s["age_days"],
            "stale": s["stale"],
        })

    out.sort(key=lambda i: (i["age_days"] is None, -(i["age_days"] or 0)))
    stale = [i for i in out if i["stale"]]
    return {
        "enabled": bool(max_age_days and max_age_days > 0),
        "max_age_days": max_age_days,
        "items": out,
        "stale_count": len(stale),
        "stale_keys": [i["key"] for i in stale],
    }


def collect(db) -> dict:
    """Resolve every registered secret's last-changed date and roll it up.

    For external-vault references the age comes from the backend's own last-changed
    date (so a secret rotated in the vault reads as fresh); DB-stored secrets — and
    refs the vault can't date — use the dashboard's ``AppConfig.updated_at``.

    All imports are function-local: this module is loaded by file path in its own test
    with no package context, and a top-level ``from ..database import …`` would break
    that. Same reason the API routers do it.
    """
    from . import config_service as cs
    from ..config import settings
    from ..database import AppConfig

    try:
        max_age = int(cs.get("secret_max_age_days")
                      or getattr(settings, "secret_max_age_days", 0) or 0)
    except (TypeError, ValueError):
        max_age = 0

    cs._ensure_loaded()
    keys = [k for k, _ in SECRET_REGISTRY]
    updated = {
        r.key: r.updated_at
        for r in db.query(AppConfig).filter(
            AppConfig.key.in_(keys), AppConfig.workgroup.is_(None)).all()
    }

    items = []
    for key in keys:
        raw = cs.get_raw(key)
        if not raw:
            continue  # unset → nothing to age
        source = "database"
        changed_at = updated.get(key)
        for b_id, prefix in BACKEND_PREFIXES.items():
            if prefix and raw.startswith(prefix):
                source = b_id
                # Prefer the vault's real last-changed date; fall back to when the
                # reference was configured in the dashboard.
                changed_at = cs.describe_reference(raw) or updated.get(key)
                break
        items.append({"key": key, "source": source, "changed_at": changed_at})

    return summarize(items, max_age)
