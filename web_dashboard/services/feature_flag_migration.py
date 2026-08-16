"""One-time translations of retired feature flags into their replacements.

Each function here answers the same question: an operator set a flag on the old build,
the new build reads a different key — what should the new key say? Called from
``init_db``'s data-seed block, outside the advisory-locked DDL transaction.
"""
import logging

from sqlalchemy.exc import IntegrityError

from . import config_service

logger = logging.getLogger(__name__)

_LEGACY_BEYONDTRUST = "beyondtrust_enabled"
_BEYONDTRUST_SPLIT = ("password_safe_enabled", "pra_enabled", "epml_enabled")

# get_bool's parse, kept in step deliberately: legacy rows may hold "True"/"False"
# from a pre-normalization _write_feature, not just "1"/"0".
_TRUTHY = ("1", "true", "yes", "on")


def seed_beyondtrust_split() -> int:
    """Give an existing install the three flags that replaced ``beyondtrust_enabled``.

    Password Safe, PRA and EPM-L used to share one flag and one Settings panel. They now
    gate independently, so an upgrade has to decide what the three new keys say — and the
    only safe answer is "whatever the operator already chose". Without this, an install
    that had BeyondTrust switched **off** would come back with all three defaulting to
    ``True`` in config.py and silently re-enable three products.

    **Copies, never moves.** The legacy row is left exactly as it was, which is what makes
    rolling back to the previous image a no-op rather than an outage: the old build reads
    the key it always read. Deleting it would leave a rolled-back build with no row at
    all, falling through to ``settings.beyondtrust_enabled = True`` — the same silent
    re-enable, just deferred. Same reasoning as
    ``hypervisor_connection_service.seed_from_settings``.

    **There is deliberately no seed mark.** The guard is "does any of the three already
    have a row", which is strictly stronger: a mark would stop this from ever translating
    a ``beyondtrust_enabled`` that arrives *after* first boot, which is exactly what
    happens when an operator imports a config bundle exported from a pre-split instance
    through ``POST /api/setup/import``. With no mark the next boot picks it up. The cost
    is three dict lookups per boot on an install that never touched these flags.

    Returns the number of keys written (0 or 3).
    """
    try:
        # An explicit operator choice on the new build always wins over a legacy value.
        if any(config_service.get_opt(k) is not None for k in _BEYONDTRUST_SPLIT):
            return 0
        legacy = config_service.get_opt(_LEGACY_BEYONDTRUST)
    except Exception:  # noqa: BLE001 — a config read must not stop the app booting
        logger.debug("beyondtrust split seed: config unreadable", exc_info=True)
        return 0

    if not legacy:
        # No row, or a row holding "" (which neither _write_feature nor _apply_config
        # ever produce — both write "1"/"0"). Either way there is no operator decision
        # to carry, so leave all three unset and let each fall through to its config.py
        # default. get_opt rather than get() is what lets us tell these apart at all.
        return 0

    on = legacy.strip().lower() in _TRUTHY
    try:
        config_service.set_many({k: "1" if on else "0" for k in _BEYONDTRUST_SPLIT})
    except IntegrityError:
        # Two gunicorn workers plus the jobs worker all call init_db() and can race past
        # the guard above. app_config.key is the primary key, so the loser rolls back —
        # benign, because both were writing identical values, and the next boot's guard
        # sees the winner's rows and no-ops.
        return 0
    except Exception:  # noqa: BLE001
        logger.warning("beyondtrust split seed failed", exc_info=True)
        return 0

    logger.info("seeded %s from %s=%s", ", ".join(_BEYONDTRUST_SPLIT),
                _LEGACY_BEYONDTRUST, legacy)
    return len(_BEYONDTRUST_SPLIT)
