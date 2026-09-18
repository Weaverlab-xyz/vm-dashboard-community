"""Entitle's own egress addresses — the source IPs its cloud dials your targets from.

A resource registered with `private = false` is reached **directly** by Entitle's
cloud, not through the shared agent. So every such target's firewall has to admit
Entitle, and nothing else in this dashboard knows to do that: registration talks to
Entitle's *API*, never to the target, so it succeeds regardless and the first sign of
the problem is a **grant** that times out — which reads like a broken integration
rather than a missing firewall rule.

This module is the single place those ranges live, so the Rancher node (today) and any
other directly-registered target (later) resolve the same answer.

Two sources, in order:

  1. ``entitle_source_cidrs`` — an operator-supplied CSV. Always wins, so a tenant on
     dedicated addresses, or one that learns of a change before this file does, is
     never blocked waiting on a dashboard release.
  2. :data:`_PUBLISHED` — BeyondTrust's published list for the tenant's region, keyed
     off the region already encoded in ``entitle_api_url`` (``api.us.entitle.io`` → us).

**These are inbound-firewall values, so a wrong one fails in one of two bad ways:** a
range that is too narrow silently drops grants, and one that is too broad opens a
management plane to strangers. Nothing here is guessed or derived from a hostname
lookup — the API host is behind a load balancer and is not the connector's egress. An
empty set is reported as "not configured", never as "none needed".
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from . import config_service

logger = logging.getLogger(__name__)

#: BeyondTrust's published egress ranges for Entitle's cloud, per tenant region.
#:
#: EMPTY ON PURPOSE, and not a stub to be filled with a plausible guess: see the module
#: docstring for why a wrong value here is worse than no value. Until a region is
#: populated, an operator supplies the ranges through ``entitle_source_cidrs`` and the
#: register path says so out loud rather than pretending the firewall is handled.
#:
#: Populate from the tenant's documented list (docs.beyondtrust.com → Entitle → the
#: allow-list article), as CIDR strings — a bare address needs its ``/32``.
_PUBLISHED: dict = {
    "us": (),
    "eu": (),
}

#: Region assumed when ``entitle_api_url`` is unset or unrecognisable. Matches the
#: default in config.Settings (``https://api.us.entitle.io/v1``), so the two cannot
#: disagree about which region an unconfigured install is in.
_DEFAULT_REGION = "us"


def region() -> str:
    """Tenant region, from the host in ``entitle_api_url``.

    ``https://api.us.entitle.io/v1`` → ``us``. Derived rather than configured
    separately because a second key for the same fact is a second thing to get wrong,
    and the API URL is already the regional one.
    """
    host = ""
    api_url = (config_service.get("entitle_api_url") or "").strip()
    if api_url:
        host = (urlsplit(api_url).netloc or "").lower()
    # api.<region>.entitle.io — take the label after "api". Anything else (a bare
    # api.entitle.io, a proxy, a private host) falls back rather than guessing.
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] in _PUBLISHED:
        return parts[1]
    return _DEFAULT_REGION


def _csv(raw: str) -> list:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def cidrs() -> list:
    """The ranges to admit for Entitle's cloud, or ``[]`` when none are known.

    ``[]`` means **unknown**, not "no ranges needed" — callers must not read it as
    permission to skip the firewall. Use :func:`configured` to tell the two apart.
    """
    override = _csv(config_service.get("entitle_source_cidrs") or "")
    if override:
        return sorted(set(override))
    return sorted(set(_PUBLISHED.get(region(), ())))


def configured() -> bool:
    """Whether we can answer the firewall question at all."""
    return bool(cidrs())


def unconfigured_warning() -> str:
    """One sentence naming the gap and the fix, or ``""`` when there is no gap.

    Returned rather than logged so the caller can put it where an operator will
    actually see it — a job result, an API response — instead of only in the log of
    the worker that happened to run the registration.
    """
    if configured():
        return ""
    return (
        "Entitle's egress ranges are not known to this dashboard, so the target's "
        "firewall was not opened to them. Registration still succeeded — it talks to "
        "Entitle's API, not to the target — but a grant will time out until you set "
        f"entitle_source_cidrs (region {region()!r}) or switch the integration to "
        "agent-brokered (private) mode."
    )
