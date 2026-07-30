"""Where this dashboard is reachable from the outside.

Three things need an absolute URL for the dashboard itself: the Entra OAuth callback,
the OIDC callback, and the audience a remote agent signs against. All three used to
derive it from the incoming request — `f"{request.url.scheme}://{request.url.netloc}"`
— which quietly couples them to something unrelated: **which proxy headers the app
trusts**.

That coupling is the whole problem. `request.url.scheme` only says `https` behind a
proxy because `ProxyHeadersMiddleware` rewrote it from `X-Forwarded-Proto`, and it only
does that for peers in `trusted_proxy_hosts`. So tightening proxy trust — the correct
thing to do, since the rate limiter keys off the same machinery — would silently turn
every OAuth redirect URI into `http://…`, which the identity provider rejects as a
mismatch. Two unrelated concerns, one shared failure.

Setting ``public_base_url`` breaks the coupling: the URL becomes a fact the operator
states once, rather than something re-inferred from headers on every request. Request
derivation stays as the fallback, because it is genuinely convenient for a laptop
install reached over localhost, an IP, and a hostname on different days.

Pure and stdlib-only, so it is testable without FastAPI.
"""
from __future__ import annotations

from typing import Optional

CONFIG_KEY = "public_base_url"


def configured() -> str:
    """The operator-stated origin, or "" when unset. Never raises — an unreadable
    config store must fall through to derivation, not break every login."""
    try:
        from . import config_service
        from ..config import settings
        value = (config_service.get(CONFIG_KEY) or getattr(settings, CONFIG_KEY, "") or "")
    except Exception:  # noqa: BLE001
        return ""
    return normalize(value)


def normalize(value: str) -> str:
    """Trim to a bare scheme://host[:port] origin.

    Operators paste whatever is in their address bar, so a trailing slash or a path
    like ``/login`` is common — and silently concatenating that onto a callback path
    produces a redirect URI that fails to match the one registered with the identity
    provider, with no clue as to why.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        # A bare hostname is ambiguous, and guessing http:// for something the operator
        # meant to be public would be the wrong guess.
        value = f"https://{value}"
    scheme, _, rest = value.partition("://")
    origin = rest.split("/", 1)[0]
    return f"{scheme.lower()}://{origin}".rstrip("/") if origin else ""


def derive(request) -> str:
    """The origin as the request appears to the app, post-ProxyHeaders."""
    try:
        return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def resolve(request=None, *, fallback: str = "") -> str:
    """The configured origin, else the request's, else ``fallback``."""
    origin = configured()
    if origin:
        return origin
    if request is not None:
        origin = derive(request)
    return origin or fallback


def join(path: str, request=None, *, fallback: str = "") -> str:
    """An absolute URL for a dashboard-relative path, or "" if the origin is unknown."""
    base = resolve(request, fallback=fallback)
    if not base:
        return ""
    return f"{base}/{(path or '').lstrip('/')}"


class ForwardedHeaderAuditor:
    """Decides whether an ``X-Forwarded-*`` header from a given peer deserves a warning.

    Pinning ``trusted_proxy_hosts`` is correct, but its failure mode is invisible: the
    headers are ignored, the scheme silently becomes ``http``, and the first symptom is
    an OAuth callback the identity provider rejects — with nothing linking the two. This
    turns that into one actionable line.

    A separate object rather than logic inline in the middleware so it is testable
    without importing the application, which drags in every warmer and optional
    integration. The state is the set of peers already warned about, so a misconfigured
    proxy logs once instead of once per request.
    """

    def __init__(self, trusted, cap: int = 64):
        self.trusted = {h.strip() for h in trusted if str(h).strip()} \
            if not isinstance(trusted, str) else \
            {h.strip() for h in trusted.split(",") if h.strip()}
        # Bounded: a spray of forged headers from many sources must not grow this
        # without limit. Past the cap we stop warning, which is the right way for a
        # diagnostic to fail.
        self.cap = cap
        self.seen: set = set()

    def reset(self) -> None:
        self.seen.clear()

    def check(self, peer: str, has_forwarded: bool) -> Optional[str]:
        """The warning to log, or None. Calling this is what marks the peer as seen."""
        if "*" in self.trusted or not has_forwarded or not peer:
            return None
        if peer in self.trusted or peer in self.seen:
            return None
        if len(self.seen) >= self.cap:
            return None
        self.seen.add(peer)
        trusted_desc = ",".join(sorted(self.trusted)) or "(none)"
        return (
            f"Ignoring X-Forwarded-* from {peer}: it is not in trusted_proxy_hosts "
            f"({trusted_desc}). If {peer} is your reverse proxy, set "
            f"TRUSTED_PROXY_HOSTS={peer} — otherwise the dashboard sees this request as "
            f"plain http, which breaks OAuth redirect URIs and lets clients spoof their "
            f"address past rate limits. Setting PUBLIC_BASE_URL fixes the OAuth half "
            f"independently."
        )


def is_https(request=None) -> Optional[bool]:
    """Whether the dashboard believes it is reached over TLS, or None if unknown.

    Read from the configured origin when there is one, because that is the operator's
    statement of fact — the request's scheme is only as trustworthy as the proxy
    configuration behind it.
    """
    base = configured()
    if base:
        return base.startswith("https://")
    derived = derive(request) if request is not None else ""
    return derived.startswith("https://") if derived else None
