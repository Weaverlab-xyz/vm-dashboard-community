"""Does this BeyondTrust tenant's credential actually work?

A registry that only stores credentials tells an operator nothing until the first POV
wire-up fails, minutes into a job, against a customer's appliance. This is the cheap
answer up front: one token handshake per kind, no side effects, and a message that names
the product rather than the transport.

**Every check here is one this codebase already performs.** PRA's is
``pra_api_service._token``; Password Safe's is ``ps_api_service._sign_in``. Reproducing
the handshake against a *tenant* rather than the singletons is the whole difference — and
it is why a green Verify means something: it proves the same call the real work makes.

**A failed check is a return value, not an exception.** ``verify`` answers
``(ok, message)``. "These credentials do not work" is the *expected* outcome of a
credential check, and modelling it as an exception meant the endpoint caught one and put
its ``str()`` into an HTTP response — which is how a transport exception's text, a local
path or a chained cause from somewhere unrelated ends up in front of a user. CodeQL
flagged that flow twice before this shape existed, and it was right both times.

Exceptions are kept for the two things that really are not outcomes: a kind that cannot be
verified at all, and a tenant with no URL to dial. Both are refusals about the *request*,
raised fresh with no cause attached, and the endpoint turns them into a 409 without
recording a failed check against the row.

Entitle is deliberately not verifiable. Everything the dashboard does with it goes through
the Terraform provider or POSTs an access request, and an access request is a side effect,
not a check. There is no read this codebase already makes that would prove a bearer token,
and an invented endpoint is how a Verify starts reporting green for a token that does not
work. ``bt_tenant_service.VERIFIABLE_KINDS`` records that, the UI offers no button, and a
test pins the two together.
"""
from __future__ import annotations

import logging

import httpx

from .bt_tenant_service import VERIFIABLE_KINDS, BTTenantError, Tenant

logger = logging.getLogger(__name__)

# A credential check is not a provisioning job. It should answer or fail well inside the
# time an operator will sit and watch a spinner, and a tenant whose appliance takes 20
# seconds to issue a token has a problem this timeout should surface rather than absorb.
_TIMEOUT_S = 20.0


def _http_reason(exc: Exception) -> str:
    """Turn a transport failure into something that names the likely cause.

    "connection failed" against a customer's appliance is the message an SE will read
    three times before checking DNS, so the common cases say which is which.

    **The exception's own text is never interpolated**, only its type. A caught
    exception's ``str()`` is not written for an operator: it can carry local paths, the
    resolved address behind a hostname, or a chained cause from somewhere else entirely,
    and all of it would land in an HTTP response body. The type name is the part that is
    diagnostic; the rest belongs in the log, which is where ``verify_tenant`` puts it.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return "the host did not answer in time — check the hostname and any firewall"
    if isinstance(exc, httpx.ReadTimeout):
        return "the host accepted the connection but did not answer — check the URL path"
    if isinstance(exc, httpx.ConnectError):
        return "the host could not be reached — check the hostname and DNS"
    if isinstance(exc, httpx.InvalidURL):
        return "that URL is not one this dashboard can dial — check it for typos"
    if isinstance(exc, httpx.HTTPError):
        return f"the request failed ({type(exc).__name__}) — see the dashboard log"
    return f"the check failed unexpectedly ({type(exc).__name__}) — see the dashboard log"


async def _verify_pra(tenant: Tenant) -> tuple[bool, str]:
    """OAuth2 client credentials against the PRA appliance. Mirrors pra_api_service._token.

    A 401 here has exactly one common cause and it is worth naming: the OAuth client in
    PRA is a separate object from the account an operator logs in with, and pasting the
    latter produces this and nothing else.
    """
    if not tenant.client_id or not tenant.secret:
        return False, (
            "this tenant has no OAuth client id and secret. PRA authenticates with an API "
            "account created under /login > Management > API Configuration, not with a "
            "user login.")
    # The SAME token call the Gateway install makes, not a second copy of it. A verify
    # that authenticates differently from the real work is a check that can pass while the
    # work fails.
    from . import pra_tenant_api
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            await pra_tenant_api.get_token(client, tenant)
    except pra_tenant_api.PRATenantError as exc:
        # Authored text, no exception interpolated — see the module docstring.
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        # Logged here, where the cause is still whole, and deliberately not carried out
        # of this function in any form.
        logger.warning("PRA verify against %s failed", tenant.api_base, exc_info=True)
        return False, f"PRA at {tenant.api_base}: {_http_reason(exc)}"
    return True, f"PRA at {tenant.api_base} issued a token."


async def _verify_password_safe(tenant: Tenant) -> tuple[bool, str]:
    """Token, then SignAppIn, then Signout. Mirrors ps_api_service._sign_in.

    Both halves matter and they fail for different reasons. The token proves the OAuth
    client; **SignAppIn proves the BeyondInsight user it is linked to** — an OAuth client
    with no linked user, or one lacking Password Safe API access, gets a perfectly good
    token and then fails here. Checking only the token would report green for a tenant
    that cannot do anything.

    Signout is best-effort: the session expires on its own, and failing a verify that
    already succeeded because the tidy-up did not would be reporting the wrong thing.
    """
    if not tenant.client_id or not tenant.secret:
        return False, (
            "this tenant has no OAuth client id and secret. Password Safe authenticates "
            "with an API registration linked to a BeyondInsight user.")
    base = tenant.api_base
    try:
        async with httpx.AsyncClient(base_url=f"{base}/", timeout=_TIMEOUT_S,
                                     headers={"Accept": "application/json"}) as client:
            token_resp = await client.post(
                "Auth/Connect/Token",
                data={"grant_type": "client_credentials",
                      "client_id": tenant.client_id,
                      "client_secret": tenant.secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            if token_resp.status_code in (400, 401, 403):
                return False, (
                    f"Password Safe rejected these credentials "
                    f"({token_resp.status_code}). Check the API registration's client id "
                    f"and secret.")
            if token_resp.status_code != 200:
                return False, (
                    f"Password Safe token request failed ({token_resp.status_code}).")
            token = (token_resp.json() or {}).get("access_token", "")
            if not token:
                return False, (
                    "Password Safe answered 200 with no access_token in the body.")

            client.headers["Authorization"] = f"Bearer {token}"
            sign = await client.post("Auth/SignAppIn")
            if sign.status_code not in (200, 201):
                return False, (
                    f"Password Safe issued a token but SignAppIn failed "
                    f"({sign.status_code}). The OAuth client is valid; the BeyondInsight "
                    f"user it is linked to is missing, disabled, or has no Password Safe "
                    f"API access.")
            try:
                await client.post("Auth/Signout")
            except Exception:  # noqa: BLE001 - the session expires on its own
                logger.debug("Password Safe verify: sign-out failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Password Safe verify against %s failed", base, exc_info=True)
        return False, f"Password Safe at {base}: {_http_reason(exc)}"

    return True, f"Password Safe at {base} issued a token and signed in."


_VERIFIERS = {
    "pra": _verify_pra,
    "password_safe": _verify_password_safe,
}


async def verify(tenant: Tenant) -> tuple[bool, str]:
    """Check a tenant's credential. Returns ``(ok, message)`` — see the module docstring.

    Raises only for the two things that are not check outcomes: a kind nothing here can
    check, and a tenant with no URL to dial. Both are refusals about the request, raised
    fresh with no cause attached, and the endpoint answers 409 without recording a failed
    check against the row — because "we did not check" and "we checked and it failed" are
    different, and a row that reads red for the first is lying about the second.
    """
    if tenant.kind not in VERIFIABLE_KINDS:
        # Plural, to dodge the a/an problem: the label is data, and "a Entitle tenant"
        # is what picking an article for it in advance gets you.
        raise BTTenantError(
            f"{tenant.label} tenants cannot be verified: this dashboard has no read "
            f"against one that would prove a credential without also doing something. "
            f"Its first real use is the check.")
    if not tenant.api_base:
        raise BTTenantError("this tenant has no URL to check")
    return await _VERIFIERS[tenant.kind](tenant)
