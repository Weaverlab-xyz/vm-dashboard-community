"""
The dashboard's BeyondTrust platform identity, shared by every product API.

Pathfinder Workload Identity authenticates calls to **every** BeyondTrust product
on the platform, not only Workload Credentials. The token source was built for
Workload Credentials first (``workload_credentials_service._workload_token``: Azure
managed identity, GCP metadata, a projected file, or the dashboard's own SPIFFE
SVID), and this module is the face the other products use: PRA, Password Safe,
EPM for Linux and Entitle.

**A facade, not a second copy.** The token fetch, its memo and its per-platform
error hints stay in ``workload_credentials_service``, so one Pathfinder
registration, one token cache, and one panel (*BeyondTrust platform identity*)
serve all five products. The registration's **Product Scopes** decide which
products that identity may call; a product missing from them is a flat 401.

What each product adds is only:

``<product>_auth_mode``           ``workload``, or its own native mode (anything
                                  else). Unknown reads as native, so a typo fails
                                  with the product's familiar credential error
                                  rather than a hunt for a metadata endpoint.
``<product>_workload_api_url``    Where calls go on workload auth. A template that
                                  may carry ``{site}``. The defaults are educated
                                  guesses at the gateway path, and are a panel
                                  field precisely so a wrong guess is a settings
                                  edit, not a rebuild.
``<product>_workload_service_name``
                                  Optional ``X-BT-Service-Name`` override, for an
                                  install that registered one identity per product.

**Terraform, ps-cli and the Ansible runner are untouched.** The sra, passwordsafe
and entitle providers and ps-cli accept only client credentials or an API key, so
they keep reading the stored ones. Workload auth here covers the dashboard's own
REST calls.

**This install only.** A customer tenant from ``bt_tenant_service`` is a different
site with a different trust, so the tenant-scoped clients (``pra_tenant_api``,
``bt_tenant_verify``, ``ps_api_service`` with a ``tenant``) keep their client
credentials; only the install's own singleton calls honour workload mode.
"""
from __future__ import annotations

import base64
import json
from . import workload_credentials_service as wlc

AUTH_MODE_WORKLOAD = "workload"
AUTH_MODE_NATIVE = "native"

# Products that can use the platform identity, and the native mode each one
# falls back to. The value is only for messages and panel defaults: anything
# other than ``workload`` IS native, whatever it is spelled.
PRODUCTS = {
    "pra": "oauth",
    "ps": "oauth",
    "epml": "pat",
    "entitle": "token",
}

DEFAULT_GATEWAY = "https://api.beyondtrust.io"
# Best guesses at the gateway path per product. EPM-L's is the one PROVEN shape
# (``/site/{site}/epm/linux``, see epml_service) and is not here because EPM-L
# already talks to the gateway. The others are unverified until a live call.
DEFAULT_URLS = {
    "pra": DEFAULT_GATEWAY + "/site/{site}/pra",
    "ps": DEFAULT_GATEWAY + "/site/{site}/passwordsafe",
    "entitle": DEFAULT_GATEWAY + "/site/{site}/entitle",
}

_LABELS = {
    "pra": "PRA",
    "ps": "Password Safe",
    "epml": "EPM-L",
    "entitle": "Entitle",
}


class IdentityError(Exception):
    """The platform identity could not produce a usable request.

    Raised with the product named, so a message that surfaces on the PRA page
    does not read as a Workload Credentials problem.
    """


def _cfg(key: str) -> str:
    return (wlc._cfg(key) or "").strip()


def auth_mode(product: str) -> str:
    """``workload`` or ``native`` for ``product``. Unknown reads as native."""
    raw = _cfg(f"{product}_auth_mode").lower()
    return AUTH_MODE_WORKLOAD if raw == AUTH_MODE_WORKLOAD else AUTH_MODE_NATIVE


def uses_workload(product: str) -> bool:
    return auth_mode(product) == AUTH_MODE_WORKLOAD


def service_name(product: str = "") -> str:
    """The ``X-BT-Service-Name`` for ``product``: its override, else the shared one.

    Stripped, unlike the raw config read: a trailing space pasted into the field
    rides into the header and is an unexplained 401.
    """
    override = _cfg(f"{product}_workload_service_name") if product else ""
    return override or _cfg("wlc_service_name")


def _sanitize_site_id(raw: str) -> str:
    return (raw or "").strip("\"'{}/ \t\r\n")


def site_id() -> str:
    """The Pathfinder site: the shared setting, else WLC's, else EPM-L's.

    The fallbacks are there because an install that set up either product
    before this panel existed already told us its site.
    """
    for key in ("bt_platform_site_id", "wlc_site_id", "epml_site_id"):
        val = _sanitize_site_id(_cfg(key))
        if val:
            return val
    return ""


def resolve_url(template: str, product: str = "") -> str:
    """``template`` with ``{site}`` filled in and no trailing slash."""
    template = (template or "").strip()
    if not template:
        raise IdentityError(
            f"{_LABELS.get(product, product)} is on workload identity auth but has "
            "no workload API URL set.")
    if "{site}" in template:
        site = site_id()
        if not site:
            raise IdentityError(
                f"{_LABELS.get(product, product)}'s workload API URL needs the "
                "Pathfinder site id — set it in Settings → BeyondTrust platform "
                "identity.")
        template = template.replace("{site}", site)
    return template.rstrip("/")


def api_url(product: str) -> str:
    """The resolved workload API URL for ``product``, from config or the default."""
    template = _cfg(f"{product}_workload_api_url") or DEFAULT_URLS.get(product, "")
    return resolve_url(template, product)


def missing(product: str = "") -> list:
    """Settings the identity still needs before ``product`` can use it."""
    return wlc.identity_missing(service_name(product) if product else "")


def headers(product: str = "") -> dict:
    """``Authorization`` + ``X-BT-Service-Name`` for this container's identity.

    Raises :class:`IdentityError` naming the product and what is missing, or
    why the token fetch failed — never returns a header with an empty bearer.
    """
    label = _LABELS.get(product, product) or "BeyondTrust"
    gaps = missing(product)
    if gaps:
        raise IdentityError(
            f"{label} is on workload identity auth, but the BeyondTrust platform "
            f"identity is not fully configured — missing: {', '.join(gaps)}. "
            "Set it in Settings → BeyondTrust platform identity.")
    try:
        token = wlc._workload_token()
    except wlc.WorkloadCredentialsError as exc:
        raise IdentityError(f"BeyondTrust platform identity ({label}): {exc}") from exc
    return {
        "Authorization": f"Bearer {token}",
        "X-BT-Service-Name": service_name(product),
    }


def rejection_hint(product: str) -> str:
    """What a 401/403 on workload auth most likely means, for error messages."""
    return (f" — {_LABELS.get(product, product)} is on workload identity auth: check "
            "that the Pathfinder Workload Identity is on the right site, lists this "
            "product under Product Scopes, and that its Service Name matches "
            f"'{service_name(product)}'.")


def unverified_claims(token: str) -> dict:
    """The JWT payload, **unverified** — for showing an operator what was minted.

    Never used to make a decision. Pathfinder verifies the token; this only
    lets the Test identity button print ``iss``/``aud``/``sub``/``exp``.
    """
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:  # noqa: BLE001 — display only
        return {}
    return claims if isinstance(claims, dict) else {}


def test_identity() -> dict:
    """Fetch a token and report its claims. Free: calls no BeyondTrust endpoint.

    Separates "this container has no usable identity" (the fetch fails here)
    from "Pathfinder declined it" (this passes, a product call 401s).
    """
    gaps = wlc.identity_missing()
    if gaps:
        raise IdentityError("missing: " + ", ".join(gaps))
    try:
        token = wlc._workload_token()
    except wlc.WorkloadCredentialsError as exc:
        raise IdentityError(str(exc)) from exc
    claims = unverified_claims(token)
    keep = {k: claims.get(k) for k in ("iss", "aud", "sub", "exp") if k in claims}
    return {
        "ok": True,
        "platform": wlc.identity_platform(),
        "service_name": _cfg("wlc_service_name"),
        "site_id": site_id(),
        "claims": keep,
        "message": (f"Got a {wlc.identity_platform()} identity token"
                    + (f" for sub {keep['sub']}" if keep.get("sub") else "")
                    + ". This proves the token source only — a product call is what "
                      "proves Pathfinder accepts it."),
    }

