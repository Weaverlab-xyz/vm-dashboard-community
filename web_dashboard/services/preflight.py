"""One place to ask what is actually reachable right now.

Eight endpoints in this tree answer "can you reach X?" — ``connections/{id}/test``,
``notifications/endpoints/{id}/test``, ``secrets/test``, ``secrets/wlc/test``,
``setup/oidc/test``, ``setup/skytap/test``, ``setup/pov-cloud/test``, ``storage/test`` —
and every one of them answers for exactly one thing, from its own panel, in its own shape.
Between them they use three different admin checks, two different failure conventions
(200-with-the-outcome vs raising 400/502 — the Skytap handler's docstring calls out the
disagreement in so many words), and two different names for the same field (``error`` and
``message``). Nothing anywhere answers "is this instance healthy?" without a person
clicking through eight panels.

This module is the missing half. The registry below is the *live probe* to sit beside the
configuration question ``pov_use_cases`` already answers for a POV — "can I run this here?"
read from stored config rather than from a wire.

**It reuses the services, never the handlers.** The eight endpoints stay exactly as they
are: other UI calls them, they carry per-panel wording worth keeping, and two of them do
things a sweep must not. Routing this through them would also inherit three auth styles.

**Two deliberate exclusions, and they are the whole safety story.**

``notifications/endpoints/{id}/test`` **sends a real message** — that is its entire value,
and its docstring says so. A "check everything" button that quietly posted to every
configured Slack, Teams and webhook endpoint would be a way to spam an operator's channels
by pressing refresh. It stays a per-endpoint button somebody chooses to press.

``connections/{id}/test`` dials a hypervisor and STAMPS THE RESULT on the row. Connections
already have a per-row status the connections page renders from exactly that stamp, so a
second surface would be two sources for one fact, drifting apart the moment either is
touched — and agent-bound connections cannot be dialled from here at all. Their liveness
belongs to the agent's own status, which is where it already lives.

So what is left is the six *configuration-level* integrations, which are the ones with no
aggregate view anywhere and no side effect worth fearing: the secrets backend, storage,
OIDC discovery, Skytap, the POV cloud, and Workload Credentials — whose check is
explicitly unmetered, so it is free to press repeatedly.

**Nothing here raises.** One misconfigured integration must not sink the sweep, the same
degrade-per-item discipline ``cost_service._cloud_entry`` follows for clouds. A probe that
blows up in an unexpected way is reported as a failed probe with its message, not as a
500 for the whole page.
"""
from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

# A probe that hangs is worse than one that fails: it holds the whole sweep open behind
# `gather`. Each gets its own ceiling, well past a healthy round-trip and well short of a
# page load anyone would wait through.
PROBE_TIMEOUT_S = 15


class Result(NamedTuple):
    """One integration's answer.

    ``ok`` is deliberately three-valued, the same shape ``connections/{id}/test`` already
    uses for an agent-bound connection:

      * ``True``  — probed, and it answered.
      * ``False`` — probed, and it did not. ``detail`` carries the upstream's own wording,
        which is the part that separates a revoked token from a wrong host.
      * ``None``  — not probed. Either it is not configured here, or it is configured and
        cannot be reached from this process. Rendering that as ``False`` would report
        every switched-off integration as broken.
    """
    key: str
    label: str
    configured: bool
    ok: Optional[bool]
    detail: str


def _unconfigured(key: str, label: str, detail: str = "") -> Result:
    """Not set up here. Not an error — most instances use a handful of these."""
    return Result(key, label, False, None, detail or "Not configured.")


# ── The probes ────────────────────────────────────────────────────────────────
# Each imports its service lazily, the convention every sweeper and warmer in this tree
# follows: this module is imported at startup by the API router, and none of these
# services should be pulled in behind it.

async def _probe_secrets_backend() -> Result:
    key, label = "secrets_backend", "Secrets backend"
    from . import config_service as cs
    backend = cs.get("secrets_backend", "database")
    if backend == "database":
        # Not a probe at all: it is the same database this request is already served from,
        # so there is nothing to reach. Reported ok rather than skipped, because it IS
        # working and an operator scanning this list should see that.
        return Result(key, label, True, True, "Database backend is always available.")
    from . import secrets_backend_service as sbs
    result = await asyncio.to_thread(sbs.test_sync, backend)
    return Result(key, label, True, bool(result.get("ok")),
                  result.get("message") or result.get("error") or "")


async def _probe_storage() -> Result:
    key, label = "storage", "Storage backend"
    from . import storage_service
    backend = storage_service.active_backend()
    if not backend:
        return _unconfigured(key, label, "No active storage backend is selected.")
    result = await storage_service.test_backend(backend)
    return Result(key, f"{label} ({backend})", True, bool(result.get("ok")),
                  result.get("message") or result.get("error") or "")


async def _probe_oidc() -> Result:
    key, label = "oidc", "OIDC discovery"
    from . import oidc_service
    if not oidc_service.is_configured():
        return _unconfigured(key, label, "No issuer and client id are set.")
    # Always live, never a cached answer — a stale success is the one result this page
    # must not show. Same reason the panel's own probe clears it first.
    oidc_service.clear_cache()
    doc = await asyncio.to_thread(oidc_service.discovery)
    issuer = (doc or {}).get("issuer") or ""
    return Result(key, label, True, True,
                  f"Discovery document served by {issuer}." if issuer
                  else "Discovery document fetched.")


async def _probe_skytap() -> Result:
    key, label = "skytap", "Skytap"
    from . import skytap_service
    if not skytap_service.configured():
        return _unconfigured(key, label)
    ok, message = await skytap_service.verify()
    return Result(key, label, True, bool(ok), message or "")


async def _probe_pov_cloud() -> Result:
    key, label = "pov_cloud", "POV cloud provider"
    from . import lab_platforms
    chosen = lab_platforms.selected_cloud()
    if not chosen:
        return _unconfigured(key, label, "No POV cloud provider is selected.")
    if not lab_platforms.supports(chosen, "verify"):
        # Configured, and genuinely unprobeable — `ok=None`, not False. The adapter
        # offering no credential check says nothing about whether the cloud works.
        return Result(key, f"{label} ({chosen})", True, None,
                      f"The {chosen} adapter offers no credential check.")
    ok, message = await lab_platforms.adapter(chosen).verify()
    return Result(key, f"{label} ({chosen})", True, bool(ok), message or "")


async def _probe_workload_credentials() -> Result:
    key, label = "workload_credentials", "Workload Credentials"
    from . import workload_credentials_service as wlc
    if not wlc.configured():
        return _unconfigured(key, label, "No site ID and Personal Access Token are set.")
    # GET /session validates the PAT, site id and API version in one UNMETERED call, so
    # this sweep is free to run as often as anyone reloads. A dynamic issuance would be
    # billed, and must never be what a health page does.
    result = await asyncio.to_thread(wlc.test_connection)
    return Result(key, label, True, bool(result.get("ok", True)),
                  result.get("message") or result.get("detail") or "")


# Registry order is display order: the two that break a login or a deploy first, then the
# platform integrations. Adding one here is the only edit an aggregate probe needs.
PROBES = (
    ("oidc", _probe_oidc),
    ("secrets_backend", _probe_secrets_backend),
    ("storage", _probe_storage),
    ("workload_credentials", _probe_workload_credentials),
    ("skytap", _probe_skytap),
    ("pov_cloud", _probe_pov_cloud),
)

_LABELS = {
    "oidc": "OIDC discovery",
    "secrets_backend": "Secrets backend",
    "storage": "Storage backend",
    "workload_credentials": "Workload Credentials",
    "skytap": "Skytap",
    "pov_cloud": "POV cloud provider",
}


async def _run_one(key: str, probe) -> Result:
    """One probe, which cannot raise and cannot hang the sweep."""
    label = _LABELS.get(key, key)
    try:
        return await asyncio.wait_for(probe(), timeout=PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return Result(key, label, True, False,
                      f"No answer within {PROBE_TIMEOUT_S}s.")
    except Exception as exc:                           # noqa: BLE001
        # Every integration's SDK raises its own type, and an unfamiliar one is still an
        # answer to "can you reach it?" — so it is reported, not propagated.
        logger.info("preflight probe %s failed: %s", key, exc)
        return Result(key, label, True, False, str(exc)[:500])


async def run_all() -> list:
    """Probe every registered integration concurrently. Never raises.

    Concurrent because these are independent network round-trips against different
    providers and running them in series would make the page wait for the sum. The
    per-probe timeout above bounds the whole sweep at roughly one probe's ceiling.
    """
    results = await asyncio.gather(*(_run_one(k, p) for k, p in PROBES))
    return list(results)


def summarize(results) -> dict:
    """Counts a page header can render without re-deriving the tri-state rule."""
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.ok is True),
        "failing": sum(1 for r in results if r.ok is False),
        # Configured-but-unprobeable and not-configured-at-all are both "no answer", and
        # neither is a failure. Counting them with failures is how a healthy instance
        # ends up looking broken because it does not use Skytap.
        "not_probed": sum(1 for r in results if r.ok is None),
        "configured": sum(1 for r in results if r.configured),
    }
