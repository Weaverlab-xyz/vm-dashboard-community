"""The Skytap lab-platform adapter.

Implements ``lab_platforms.READ_CONTRACT`` over ``skytap_client``. Everything here is
mapping: Skytap's JSON in, the dashboard's shape out. The transport concerns — Basic auth,
the 423/Retry-After retry, the ``keep_idle=true`` guarantee, count/offset pagination — all
live in the client and are deliberately not repeated.

Skytap's vocabulary maps onto the feature's like this:

    template       -> the thing a POV is created FROM
    configuration  -> a running environment. The API calls it a "configuration"; the UI and
                      the docs call it an environment. This module speaks "environment"
                      outward and only uses "configuration" in the URL paths.
    vm             -> a VM inside one

One naming rule worth stating because breaking it is silent: every read is keyed on the
environment's **id**, never its name. An SE may rename an environment in Skytap to whatever
their approvals allow, and nothing here should notice.
"""
from __future__ import annotations

import logging

import httpx

from .skytap_client import (DEFAULT_BASE_URL, SkytapAuthError, SkytapClient, SkytapCreds,
                            SkytapError)

logger = logging.getLogger(__name__)

# Re-exported so callers can catch one error type without importing the client too.
__all__ = ["SkytapError", "SkytapAuthError", "VALID_RUNSTATES", "SHARE_ACCESS",
           "configured", "credentials", "configured_project_id", "verify",
           "list_templates", "list_environments", "get_environment",
           "create_environment", "update_environment", "add_vms", "set_runstate",
           "wait_for_runstate", "delete_environment", "inject_bootstrap",
           "stored_credentials", "create_share", "delete_share",
           "get_template", "create_template", "delete_template",
           "publish_service", "delete_published_service"]

# Runstates a caller may ASK for. Skytap also reports "busy" while a transition is in
# flight, which is a state you observe and never request — asking for it would be asking
# the platform to be mid-something.
VALID_RUNSTATES = ("running", "suspended", "stopped", "halted")

# Terminal states a poll can stop on. "busy" is deliberately absent.
_SETTLED = frozenset(("running", "suspended", "stopped", "halted"))


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:  # noqa: BLE001
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def credentials() -> SkytapCreds:
    return SkytapCreds(
        username=_cfg("skytap_username"),
        api_token=_cfg("skytap_api_token"),
        base_url=_cfg("skytap_base_url") or DEFAULT_BASE_URL,
    )


def configured() -> bool:
    return credentials().valid()


def configured_project_id() -> str:
    """The Skytap project every read and every new environment is scoped to, or "".

    Blank is the widest scope and the right default: a project id this token cannot see
    makes the whole catalogue invisible, which is exactly what the Troubleshooting table's
    "clear the Project ID" remedy is about.
    """
    return _cfg("skytap_project_id").strip()


# UNVERIFIED against a live Skytap account: the two project sub-resource paths below.
#
# Three things are assumed and none is confirmed — that they return the same object shape
# as the flat collections (so `_template()` and `_environment()` map unchanged), that they
# honour `count`/`offset` the way `SkytapClient.list` requires, and that a project id this
# token cannot see answers 404 rather than an empty list.
#
# They were chosen over a `project_id` filter on the flat list precisely because of that
# last point: a filter parameter an API does not implement is IGNORED, and a silently
# unscoped list looks exactly like a correct one. A 404 is a fact you can report and act
# on, which is what `_scoped_list` below turns into a remedy. If a live read 404s with a
# project id that is definitely right, these two lines are the first place to look.
def _templates_path() -> str:
    pid = configured_project_id()
    return f"/v2/projects/{pid}/templates" if pid else "/v2/templates"


def _environments_path() -> str:
    pid = configured_project_id()
    return f"/v2/projects/{pid}/configurations" if pid else "/v2/configurations"


async def _scoped_list(path: str, what: str) -> list:
    """A collection read, with a 404 turned into the remedy for a bad Project ID.

    Without this, a stale project id would surface as a bare 502 on the POV page (see
    `api/pov._platform_error`) — turning today's harmless empty list into a hard failure
    whose cause is named nowhere. Same `"(404)" in str(exc)` idiom the other two
    idempotent paths in this module use.
    """
    try:
        return await _client().list(path)
    except SkytapError as exc:
        pid = configured_project_id()
        if pid and "(404)" in str(exc):
            raise SkytapError(
                f"Skytap has no project {pid} visible to this account (404), so no "
                f"{what} can be listed. Correct or clear the Project ID in Settings → "
                f"Integrations → Skytap — blank lists everything the token can see."
            ) from exc
        raise


def _now() -> float:
    import time
    return time.monotonic()


def _client() -> SkytapClient:
    return SkytapClient(credentials())


# ── mapping ──────────────────────────────────────────────────────────────────

def _vm_count(raw: dict) -> int | None:
    """VM count, or None when the payload did not carry one.

    None, never 0: a collection listing may omit the nested `vms` array entirely, and
    rendering "0 VMs" for a template that has eight is worse than rendering nothing. Same
    rule the hypervisor projections follow for live-only fields.
    """
    vms = raw.get("vms")
    if isinstance(vms, list):
        return len(vms)
    for key in ("vm_count", "svms"):
        if isinstance(raw.get(key), int):
            return raw[key]
    return None


def _os_family(raw: dict) -> str:
    """Best-effort guess, used to pick the wire-up path (SSH vs RDP) later.

    Returns "" rather than guessing wrong: an empty value shows as unknown in the UI and
    makes the later wire-up ask, whereas a confident wrong answer would send a Windows VM
    down the Password-Safe-over-SSH path.
    """
    blob = " ".join(str(raw.get(k) or "") for k in ("guestos", "os", "name", "hardware"))
    low = blob.lower()
    if "windows" in low or "win" in low.split():
        return "windows"
    for token in ("linux", "ubuntu", "debian", "centos", "rhel", "red hat", "suse",
                  "rocky", "alma", "fedora"):
        if token in low:
            return "linux"
    return ""


def _interfaces(raw_vm: dict) -> tuple[str, list[dict], list[dict]]:
    """(private_ip, published_services, interfaces) for a VM.

    A published service is Skytap NAT-ing a guest port to a public ip:port. We surface them
    because they are useful to see, NOT because the wire-up uses them — POV wiring reaches
    VMs on their private IPs through an in-environment Gateway. See docs/profiles/pov/README.md.

    The third return value is the NICs themselves, each with its **id**. Publishing a
    service is a POST *under an interface*, so a caller that only has the flattened service
    list above has no way to name where a new one should go. The one caller today is the
    template builder's SSH prepare step (``services/pov_template_builder``), which publishes
    port 22 for the length of one build and revokes it in a ``finally``.

    ``nic_type`` and ``network_id`` come along because they are how a caller tells an
    automatic network from a manual one — the distinction Skytap's metadata service turns
    on, and therefore the one the template contract check has to report.
    """
    private_ip = ""
    published: list[dict] = []
    nics: list[dict] = []
    for iface in (raw_vm.get("interfaces") or []):
        if not private_ip and iface.get("ip"):
            private_ip = str(iface["ip"])
        services = []
        for svc in (iface.get("services") or []):
            entry = {
                "id": str(svc.get("id") or ""),
                "internal_port": svc.get("internal_port"),
                "external_ip": svc.get("external_ip") or "",
                "external_port": svc.get("external_port"),
            }
            published.append(entry)
            services.append(entry)
        nics.append({
            "id": str(iface.get("id") or ""),
            "ip": str(iface.get("ip") or ""),
            "nic_type": str(iface.get("nic_type") or ""),
            "network_id": str((iface.get("network_id")
                               or (iface.get("network") or {}).get("id") or "")),
            "network_type": str((iface.get("network") or {}).get("network_type") or ""),
            "services": services,
        })
    return private_ip, published, nics


def _template(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "description": raw.get("description") or "",
        "vm_count": _vm_count(raw),
        "region": raw.get("region") or "",
        "url": raw.get("url") or "",
    }


def _environment(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "runstate": raw.get("runstate") or "",
        "vm_count": _vm_count(raw),
        "region": raw.get("region") or "",
        "url": raw.get("url") or "",
        # Skytap's own view of whether it is throttling this environment. Surfaced rather
        # than hidden: it explains a slow provision without anyone reading a log.
        "rate_limited": bool(raw.get("rate_limited")),
        "suspend_on_idle": raw.get("suspend_on_idle"),
        "shutdown_on_idle": raw.get("shutdown_on_idle"),
    }


def _vm(raw: dict) -> dict:
    private_ip, published, nics = _interfaces(raw)
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "runstate": raw.get("runstate") or "",
        "os_family": _os_family(raw),
        "private_ip": private_ip,
        "published_services": published,
        # The NICs, with their ids. `published_services` above stays flattened because
        # every existing caller renders it as a list and none of them cares which NIC a
        # service hangs off; this is the addressable form the publish call needs.
        "interfaces": nics,
    }


# ── verify ───────────────────────────────────────────────────────────────────

def _network_reason(exc: Exception) -> str:
    """Turn a transport failure into something that names the likely cause.

    A local copy of ``bt_tenant_verify._http_reason`` rather than an import: that is a
    private function in a BeyondTrust-tenant module, and a lab-platform adapter reaching
    into it is the wrong dependency direction. The rule it encodes is the part worth
    copying — **the exception's own text is never interpolated, only its type.** A caught
    exception's ``str()`` is not written for an operator: it can carry local paths, the
    resolved address behind a hostname, or a chained cause from somewhere else entirely,
    and all of it would land in an HTTP response body. The cause belongs in the log.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return "the host did not answer in time — check the API URL and any firewall"
    if isinstance(exc, httpx.ReadTimeout):
        return "Skytap accepted the connection but did not answer — try again shortly"
    if isinstance(exc, httpx.ConnectError):
        return "the host could not be reached — check the API URL and DNS"
    if isinstance(exc, httpx.InvalidURL):
        return "that API URL is not one this dashboard can dial — check it for typos"
    if isinstance(exc, httpx.HTTPError):
        return f"the request failed ({type(exc).__name__}) — see the dashboard log"
    return f"the check failed unexpectedly ({type(exc).__name__}) — see the dashboard log"


async def verify() -> tuple[bool, str]:
    """Prove the stored Skytap credentials, and say what is wrong when they fail.

    **Returns ``(ok, message)`` and does not raise for an expected outcome.** That shape is
    load-bearing rather than a style choice: CodeQL ``py/stack-trace-exposure`` fires on any
    ``str(caught_exception)`` reaching a response body, and it took three rounds on #646 and
    #647 to learn the fix is structural. "These credentials do not work" is an ANSWER, not
    an error — so it is a return value, and the only exceptions this raises are about the
    request rather than the outcome. Do not "simplify" this back into raising.

    Reads ONE page of the template catalogue — the same read the real work makes
    (:func:`list_templates`, and the template-id check in ``api/pov.provision``). A verify
    that authenticates differently from the work can pass while the work fails.

    Deliberately **not** ``_client().list()``: that walks to the end of the collection at
    100 rows a page, so a large account would issue nine GETs to answer a yes/no question.

    An empty catalogue is reported as a FAILURE, not a warning. A POV cannot be created
    from zero templates — ``api/pov.provision`` refuses before anything exists — so
    reporting green for a connection that cannot do the one thing it is for is exactly the
    false positive a verify exists to prevent.
    """
    # Before constructing a client: SkytapClient.__init__ raises on invalid credentials,
    # and "not configured yet" must not arrive as an exception.
    if not configured():
        return False, ("Skytap is not configured — set the API URL, username and API "
                       "security token, save, then test.")

    path = _templates_path()
    try:
        # max_retries=1 and a short timeout: this answers while somebody watches a
        # spinner. The provisioning default of six retries at up to 30s each is right for
        # a job that has already waited minutes and wrong for a button.
        client = SkytapClient(credentials(), timeout=15.0, max_retries=1)
        raw = await client.request("GET", path, params={"count": 1, "offset": 0})
    except SkytapAuthError as exc:
        # The client authored this message and it already names the API-token trap. This
        # is a typed, fully-authored error, not an arbitrary exception's str().
        return False, str(exc)
    except SkytapError as exc:
        # The client's other authored refusals — rate limiting, a non-JSON body, and the
        # project 404 shape (which cannot reach here, since verify does not go through
        # _scoped_list; a bad project id arrives as a plain 404 below).
        text = str(exc)
        if "(404)" in text and configured_project_id():
            return False, (
                f"the credentials work, but Skytap has no project "
                f"{configured_project_id()} visible to this account (404). Correct or "
                f"clear the Project ID — blank lists everything the token can see.")
        return False, text
    except Exception as exc:  # noqa: BLE001 - the cause goes to the log, not the body
        logger.warning("Skytap verify failed", exc_info=True)
        return False, _network_reason(exc)

    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    if not items:
        pid = configured_project_id()
        where = f"project {pid}" if pid else "this account"
        return False, (
            f"the credentials work, but {where} exposes no templates — so there is "
            f"nothing to build a POV from. Check the user's access in Skytap"
            + (", or clear the Project ID to widen the scope." if pid else "."))

    pid = configured_project_id()
    scope = f" in project {pid}" if pid else ""
    return True, f"Connected. Templates are visible{scope}."


# ── the READ_CONTRACT ────────────────────────────────────────────────────────

async def list_templates() -> list[dict]:
    """Every template this account can instantiate, within the configured project."""
    raw = await _scoped_list(_templates_path(), "templates")
    return [_template(t) for t in raw if isinstance(t, dict)]


async def list_environments() -> list[dict]:
    """Every environment this account can see, within the configured project.

    Includes environments the dashboard did not create. That is deliberate for a read-only
    view — an SE's existing hand-built POVs are exactly what they want to see next to the
    managed ones — and it is why the eventual POV table keys on the Skytap id rather than
    assuming it owns everything in the account.
    """
    raw = await _scoped_list(_environments_path(), "environments")
    return [_environment(e) for e in raw if isinstance(e, dict)]


async def create_environment(template_id: str, name: str = "",
                             project_id: str = "") -> dict:
    """Instantiate a template as a new environment.

    Returns as soon as Skytap has created it — the VMs will still be stopped and the
    environment may report ``busy``. Powering it on is a separate call, because the
    caller must persist the new id BEFORE anything else can fail: an environment that
    exists on the platform and not in our database is the one failure mode nothing can
    clean up automatically.
    """
    template_id = str(template_id or "").strip()
    if not template_id:
        raise SkytapError("a template id is required to create an environment")

    # An explicit argument wins; the configured project is the DEFAULT. That ordering is
    # what makes the Settings field mean "where new POVs go unless told otherwise" rather
    # than an override nobody can escape.
    project_id = str(project_id or "").strip() or configured_project_id()

    body: dict = {"template_id": template_id}
    if project_id:
        body["project_id"] = project_id

    # **v1, not v2, and that is not a typo.** Skytap's v2 API documents GET/PUT/DELETE on
    # `/v2/configurations` and no POST at all — environments are created only by the v1
    # `POST /configurations.json`, which takes `template_id` and, optionally, `project_id`
    # and `name`. Posting to the v2 collection answers `404 {"error":"Not Found"}`, which
    # reads exactly like "that template id does not exist" and sent the first live
    # template build looking at the wrong field. Every *other* call in this module is v2
    # because every other call has a v2 form; these two creates do not. See
    # `create_template`.
    raw = await _client().request("POST", "/configurations.json", json=body)
    if not isinstance(raw, dict) or not raw.get("id"):
        raise SkytapError(
            f"Skytap accepted the create for template {template_id} but returned no "
            f"environment id; check the account for an orphan before retrying")

    env = _environment(raw)
    # Naming is a separate PUT. v1 does accept a `name` at create, but keeping it here
    # means the create body carries only what the environment cannot exist without —
    # and a rejected name can never be the thing that strands a real environment.
    if name:
        try:
            env = await update_environment(env["id"], {"name": name})
        except SkytapError:
            # Non-fatal on purpose. The environment exists and its id is what we key on;
            # a wrong display name is cosmetic, and failing here would strand it.
            logger.warning("Skytap: created environment %s but could not name it %r",
                           env["id"], name, exc_info=True)
    return env


async def update_environment(env_id: str, changes: dict) -> dict:
    """PUT a partial update — name, suspend_on_idle, and friends.

    Skytap accepts only ONE of suspend_on_idle / suspend_at_time / shutdown_on_idle /
    shutdown_at_time: setting any of them nulls the other three server-side. So callers
    must send at most one, and this does not try to merge them.
    """
    env_id = str(env_id or "").strip()
    if not env_id:
        raise SkytapError("an environment id is required")
    raw = await _client().request("PUT", f"/v2/configurations/{env_id}", json=changes)
    return _environment(raw) if isinstance(raw, dict) else await get_environment(env_id)


async def add_vms(env_id: str, template_id: str, vm_ids: list) -> dict:
    """Copy specific VMs from a template into an environment that already exists.

    Skytap calls this **merging a template into an environment**, and it is the only way
    VMs are ever added to one: its v2 reference says outright that "VMs are created
    indirectly, either by creating an environment from a template or by merging a template
    into an existing environment", and documents no endpoint for the second half.

    **v1, and a PUT — which makes this the most mistakable call in this module.** The path
    is ``PUT /configurations/{id}.json``, not ``PUT /v2/configurations/{id}``. Both exist,
    both are a PUT at the same resource, and the v2 one is what ``update_environment``
    already uses for the name and the idle timer. Send ``template_id`` to the v2 form and
    it answers **200 with the environment unchanged** — no error, no VMs, nothing to
    investigate. That is worse than the 404 the two creates give, which at least says
    something happened. See "The two calls that are v1" in docs/profiles/pov/skytap.md.

    ``vm_ids`` is optional in the API and required here. Omitting it merges the template
    **whole**, and against a POV that means silently doubling the environment — a
    destructive default nobody reaches for on purpose.

    Returns the environment, which will usually report ``busy``: the copy is asynchronous
    and the new VMs arrive **stopped** even when the rest of the environment is running.
    A caller wanting them on powers the environment on afterwards, which is what the
    Skytap UI's own "Add VMs" flow tells an operator to do.
    """
    env_id = str(env_id or "").strip()
    template_id = str(template_id or "").strip()
    ids = [str(v).strip() for v in (vm_ids or []) if str(v).strip()]
    if not env_id:
        raise SkytapError("an environment id is required")
    if not template_id:
        raise SkytapError("a source template id is required to add VMs")
    if not ids:
        raise SkytapError(
            "name at least one VM to add. Adding none would merge the whole template "
            "into this environment, which is not what an empty selection means.")

    body = {"template_id": template_id, "vm_ids": ids}
    try:
        raw = await _client().request("PUT", f"/configurations/{env_id}.json", json=body)
    except SkytapError as exc:
        # 409 has exactly one documented cause and it is not obvious from the message:
        # a running IBM Power VM cannot be suspended for the copy. Named here because
        # "conflict" against an environment that is plainly fine reads as a Skytap fault.
        if "409" in str(exc):
            raise SkytapError(
                f"Skytap refused the VM copy into {env_id} with a conflict. The "
                f"documented cause is a running Power VM among the ones being copied — "
                f"those cannot be suspended for the copy. Shut them down and retry.")                 from None
        raise
    return _environment(raw) if isinstance(raw, dict) else await get_environment(env_id)


async def set_runstate(env_id: str, runstate: str) -> dict:
    """Ask the environment to change state. Does NOT wait — see ``wait_for_runstate``."""
    env_id = str(env_id or "").strip()
    runstate = (runstate or "").strip().lower()
    if runstate not in VALID_RUNSTATES:
        raise SkytapError(
            f"unsupported runstate {runstate!r}; expected one of "
            f"{', '.join(VALID_RUNSTATES)}")
    raw = await _client().request("PUT", f"/v2/configurations/{env_id}",
                                  json={"runstate": runstate})
    return _environment(raw) if isinstance(raw, dict) else await get_environment(env_id)


async def wait_for_runstate(env_id: str, target: str, *, timeout_s: float = 1800.0,
                            interval_s: float = 10.0, sleep=None) -> dict:
    """Poll until the environment settles on ``target``.

    Skytap transitions are asynchronous and there is no operation handle to wait on, so
    polling the resource IS the mechanism — which is why the client's ``keep_idle``
    guarantee matters here more than anywhere: this loop would otherwise hold the
    environment awake and defeat the idle timer it was just given.

    Settling on the WRONG terminal state is an error rather than a silent success. A
    request to run that ends up ``stopped`` means the platform refused, and returning it
    as though it worked is how a POV gets wired against VMs that are not on.
    """
    import asyncio as _asyncio
    sleep = sleep or _asyncio.sleep
    target = (target or "").strip().lower()
    deadline = _now() + timeout_s
    last = {}
    while _now() < deadline:
        last = await get_environment(env_id)
        state = (last.get("runstate") or "").lower()
        if state == target:
            return last
        if state in _SETTLED:
            raise SkytapError(
                f"environment {env_id} settled on {state!r} while waiting for "
                f"{target!r}; the platform refused the transition")
        await sleep(interval_s)
    raise SkytapError(
        f"environment {env_id} did not reach {target!r} within {int(timeout_s)}s "
        f"(last seen {last.get('runstate') or 'unknown'!r})")


async def inject_bootstrap(env_id: str, vm_id: str, payload: str) -> None:
    """Write a VM's ``user_data`` — the ``bootstrap_injection: "metadata"`` mechanism.

    Skytap hands the payload to the guest and **nothing executes it**. There is no
    cloud-init datasource here: the guest fetches it from the metadata service itself, so
    a template with no runner reads this and does nothing at all, silently. See the
    template contract in docs/profiles/pov/skytap.md — that contract is the other half of
    this function, and neither half works alone.

    Two consequences the caller must know rather than discover:

    * The metadata service answers **only on VMs attached to an automatic network.** A VM
      on a manual network gets no metadata at all, which looks exactly like a runner that
      is not installed.
    * ``user_data`` is readable by anyone who can read the environment in Skytap. What
      goes in it is therefore single-use and short-lived by construction — an enrolment
      code, never a durable credential — and ``pov_broker`` clears it once redeemed.

    An empty ``payload`` clears the field, which is how that clearing is done.
    """
    env_id = str(env_id or "").strip()
    vm_id = str(vm_id or "").strip()
    if not env_id or not vm_id:
        raise SkytapError("an environment id and a VM id are required to inject bootstrap "
                          "data")
    # The documented v2 path. A 404 here means this account's API does not expose per-VM
    # user_data, which is a platform-capability problem and not a bad id -- so it is worth
    # saying so rather than letting the generic message blame the VM.
    path = f"/v2/configurations/{env_id}/vms/{vm_id}/user_data.json"
    try:
        await _client().request("PUT", path, json={"contents": payload or ""})
    except SkytapError as exc:
        if "(404)" in str(exc):
            raise SkytapError(
                f"Skytap has no user_data endpoint for VM {vm_id} in environment "
                f"{env_id} (404). The VM id may be stale, or this account's API does not "
                f"expose per-VM user_data — re-read the environment and try again."
            ) from exc
        raise


async def stored_credentials(env_id: str, vm_id: str) -> list[dict]:
    """The credentials Skytap holds for one VM.

    Normalised to ``[{"text": ..., "notes": ...}]`` — the shape ``lab_platforms``
    declared for this capability in slice 1 and nothing has filled until now.

    **``text`` is free text, and that is the platform's model rather than a shortcoming
    here.** Skytap stores what somebody typed into a box on the VM's settings page, so it
    may be ``administrator / Passw0rd``, ``administrator:Passw0rd``, or a sentence with
    the pair somewhere inside. Parsing it belongs to the caller — see
    ``services/pov_credentials`` — because the right response to an unparseable one is a
    refusal naming the VM, and that is a decision this transport layer should not make.

    Deliberately returns the raw list rather than a parsed pair: a mapper that guessed
    here would be a second parser, in the module least able to explain itself.
    """
    env_id = str(env_id or "").strip()
    vm_id = str(vm_id or "").strip()
    if not env_id or not vm_id:
        raise SkytapError("an environment id and a VM id are required to read credentials")
    raw = await _client().list(f"/v2/configurations/{env_id}/vms/{vm_id}/credentials")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            out.append({"text": text, "notes": str(item.get("notes") or "").strip()})
    return out


# ── the share link ───────────────────────────────────────────────────────────
#
# Skytap calls this a **publish set**: a URL an unauthenticated visitor opens to reach the
# environment's desktops. It is the one part of a POV a customer touches directly, and the
# one place where a mistake is visible to somebody outside the account.
#
# UNVERIFIED against a live Skytap account: the `vm_ref` below is built as
# `{base_url}/vms/{vm_id}`, which is the absolute-reference form the publish_sets API
# documents. If a live create returns 422 on `vms`, this is the field to look at first.

# What a share-link visitor may do. `run_and_use` is the POV answer — a customer who can
# see a desktop but not power it on has been handed a screenshot.
SHARE_ACCESS = "run_and_use"


def _vm_ref(vm_id: str) -> str:
    """The absolute VM reference a publish set wants, rather than a bare id."""
    return f"{credentials().base_url.rstrip('/')}/vms/{str(vm_id).strip()}"


def _share(raw: dict) -> dict:
    """Normalise a publish set to the ``{url, id}`` shape ``lab_platforms`` declared.

    ``desktops_url`` is the customer-facing one. ``url`` is the API's own self-reference —
    it is an api.skytap.com address that answers 401 to a browser, so handing it to a
    customer looks exactly like a broken link. Prefer the first and never fall back to the
    second.
    """
    url = str(raw.get("desktops_url") or "").strip()
    return {
        "url": url,
        "id": str(raw.get("id") or "").strip(),
        "expires_at": str(raw.get("expiration_date") or "").strip(),
    }


async def create_share(env_id: str, password: str = "",
                       expires_at: str = "", *, name: str = "") -> dict:
    """Publish the environment's desktops at one customer-facing URL.

    Returns ``{url, id, expires_at}``. ``expires_at`` is an ISO-8601 string or blank.

    Every VM in the environment is published, because the caller's unit is the POV and a
    per-VM selection here would be a second, quieter place to get the scope wrong.

    The password is **required** by this adapter even though Skytap's API treats it as
    optional. An unauthenticated URL into a running lab holding Password Safe, PRA and
    Entitle components is not a default anyone should reach by leaving a field blank — see
    ``services/pov_share``, which generates one rather than asking.
    """
    env_id = str(env_id or "").strip()
    if not env_id:
        raise SkytapError("an environment id is required to create a share link")
    if not str(password or "").strip():
        raise SkytapError(
            "a share link password is required; Skytap will happily publish an "
            "unauthenticated URL and this adapter will not")

    env = await get_environment(env_id)
    vms = [v for v in (env.get("vms") or []) if v.get("id")]
    if not vms:
        raise SkytapError(
            f"environment {env_id} has no VMs to publish; refresh it and try again")

    body: dict = {
        "name": (name or env.get("name") or f"POV {env_id}")[:255],
        "publish_set_type": "single_url",
        "password": str(password),
        "vms": [{"vm_ref": _vm_ref(v["id"]), "access": SHARE_ACCESS} for v in vms],
    }
    if expires_at:
        body["expiration_date"] = expires_at

    raw = await _client().request("POST", f"/v2/configurations/{env_id}/publish_sets",
                                  json=body)
    if not isinstance(raw, dict) or not raw.get("id"):
        raise SkytapError(
            f"Skytap accepted the share for environment {env_id} but returned no publish "
            f"set id; check the environment in Skytap for an orphaned sharing portal "
            f"before retrying")
    out = _share(raw)
    if not out["url"]:
        raise SkytapError(
            f"Skytap created publish set {out['id']} for environment {env_id} but "
            f"returned no desktops URL. The set exists and must be removed by hand.")
    return out


async def delete_share(env_id: str, share_id: str) -> None:
    """Revoke a share link.

    Idempotent for the same reason ``delete_environment`` is: a 404 means it is already
    gone, and a revoke that fails on "it was already revoked" leaves a row whose stored
    ``share_id`` can never be cleared.
    """
    env_id = str(env_id or "").strip()
    share_id = str(share_id or "").strip()
    if not env_id or not share_id:
        raise SkytapError("an environment id and a share id are required to revoke a link")
    try:
        await _client().request(
            "DELETE", f"/v2/configurations/{env_id}/publish_sets/{share_id}")
    except SkytapError as exc:
        if "(404)" in str(exc):
            logger.info("Skytap: publish set %s on environment %s was already gone",
                        share_id, env_id)
            return
        raise


async def delete_environment(env_id: str) -> None:
    """Delete the environment and everything Skytap keeps inside it.

    Idempotent by design: a 404 means somebody already deleted it, and a teardown that
    fails on "it is already gone" leaves a row nobody can ever clean up.
    """
    env_id = str(env_id or "").strip()
    if not env_id:
        raise SkytapError("an environment id is required")
    try:
        await _client().request("DELETE", f"/v2/configurations/{env_id}")
    except SkytapError as exc:
        if "(404)" in str(exc):
            logger.info("Skytap: environment %s was already gone", env_id)
            return
        raise


async def get_environment(env_id: str) -> dict:
    """One environment, with its VMs, private IPs and published services."""
    env_id = str(env_id or "").strip()
    if not env_id:
        raise SkytapError("an environment id is required")
    raw = await _client().get(f"/v2/configurations/{env_id}")
    if not isinstance(raw, dict):
        raise SkytapError(f"Skytap returned no environment for id {env_id!r}")
    out = _environment(raw)
    out["vms"] = [_vm(v) for v in (raw.get("vms") or []) if isinstance(v, dict)]
    # The nested read is authoritative where the collection read was not.
    out["vm_count"] = len(out["vms"])
    return out


# ── authoring templates ──────────────────────────────────────────────────────
#
# The write half of the catalogue. Until this landed the dashboard could only *read*
# `/v2/templates`, which made the whole POV feature downstream of a catalogue nobody could
# author from here — see docs/profiles/pov/skytap.md#building-a-template.
#
# Skytap has no "edit a template" call, and that is not an omission this module should try
# to paper over. A template is immutable; the way you change one is to instantiate it, change
# the *environment*, and save that back as a new template. So the pipeline is
# create_environment -> (prepare it) -> create_template, which is exactly what
# services/pov_template_builder drives.
#
# The create is the v1 `POST /templates.json` with a `configuration_id`; the description
# is set by a follow-up PUT (v2), because the create call is documented only for
# `configuration_id` and `name`. If a live create 422s, `configuration_id` is the field to
# look at first.


async def get_template(template_id: str) -> dict:
    """One template, with its VMs.

    The collection read (`list_templates`) gives a `vm_count` and nothing else, so the
    template contract cannot be checked from it — a check needs the VM *names*, to answer
    "is there a broker in here?". This is the nested read that can.
    """
    template_id = str(template_id or "").strip()
    if not template_id:
        raise SkytapError("a template id is required")
    raw = await _client().get(f"/v2/templates/{template_id}")
    if not isinstance(raw, dict):
        raise SkytapError(f"Skytap returned no template for id {template_id!r}")
    out = _template(raw)
    out["vms"] = [_vm(v) for v in (raw.get("vms") or []) if isinstance(v, dict)]
    # Authoritative here where the collection read was a guess, exactly as get_environment
    # overrides its own vm_count.
    out["vm_count"] = len(out["vms"])
    return out


async def create_template(env_id: str, name: str, description: str = "") -> dict:
    """Save an environment back as a new template.

    Returns the new template in `_template` shape. The caller must persist
    ``id`` before doing anything else that can fail: a template that exists in Skytap and
    not in this database is an orphan nobody can attribute later, which is the same rule
    `create_environment` follows for the same reason.

    The description is a **separate PUT**, and a failure to set it is deliberately not
    fatal — the template exists and its id is what everything keys on, so stranding a real
    template over a cosmetic field would be the wrong trade. `create_environment` makes the
    identical call for the identical reason.
    """
    env_id = str(env_id or "").strip()
    name = str(name or "").strip()
    if not env_id:
        raise SkytapError("an environment id is required to create a template")
    if not name:
        raise SkytapError("a template name is required")

    # v1, for the same reason `create_environment` posts to `/configurations.json`: there
    # is no POST on the v2 templates collection. The follow-up PUT below is v2, which does
    # implement it.
    try:
        raw = await _client().request("POST", "/templates.json",
                                      json={"configuration_id": env_id, "name": name})
    except SkytapError as exc:
        # Skytap answers a bake it will not do with `409 {"error":"The machine was busy.
        # Try again later."}`, and "try again later" is a lie: a multi-VM environment that
        # is running answers that way every time, forever. The caller is expected to have
        # shut it down (`pov_template_builder._quiesce`), so this fires only when
        # something else is mid-transition — but the remedy has to be IN the message,
        # because a failed job surfaces nothing but this string.
        if "(409)" in str(exc):
            raise SkytapError(
                f"Skytap will not save environment {env_id} as a template while it is "
                f"busy (409). Its VMs must be stopped — not running, not mid-transition. "
                f"Shut the environment down and bake it again."
            ) from exc
        raise
    if not isinstance(raw, dict) or not raw.get("id"):
        raise SkytapError(
            f"Skytap accepted the template create from environment {env_id} but returned "
            f"no template id; check the account for an orphan before retrying")

    out = _template(raw)
    if description:
        try:
            updated = await _client().request(
                "PUT", f"/v2/templates/{out['id']}", json={"description": description})
            if isinstance(updated, dict):
                out = _template(updated)
        except SkytapError:
            logger.warning("Skytap: created template %s but could not set its description",
                           out["id"], exc_info=True)
    return out


async def delete_template(template_id: str) -> None:
    """Delete a template.

    Idempotent on 404 for the same reason `delete_environment` is: a teardown that fails
    because the thing is already gone leaves a row nobody can ever clean up.
    """
    template_id = str(template_id or "").strip()
    if not template_id:
        raise SkytapError("a template id is required")
    try:
        await _client().request("DELETE", f"/v2/templates/{template_id}")
    except SkytapError as exc:
        if "(404)" in str(exc):
            logger.info("Skytap: template %s was already gone", template_id)
            return
        raise


# ── published services ───────────────────────────────────────────────────────
#
# Skytap NATs a guest port to a public ip:port. docs/profiles/pov/skytap.md is explicit
# that POV *wiring* does not use these, because a published address changes per environment
# and per power cycle — the wire-up reaches VMs on their private IPs through a Gateway
# inside the environment.
#
# A template BUILD is the one case where that objection does not apply, and it is worth
# saying so here rather than leaving a reader to reconcile two rules. The build publishes
# port 22 on one VM, uses the address once within the same job, and revokes it in a
# `finally`. Nothing persists it and nothing reads it later, so there is no address to
# churn. See services/pov_template_builder.prepare_broker_vm.
#
# UNVERIFIED against a live account: the path is built as
# /v2/configurations/{env}/vms/{vm}/interfaces/{nic}/services, which is the nested form the
# services API documents. If a live create 404s, this path is the field to look at first.


def _published_service(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or ""),
        "internal_port": raw.get("internal_port"),
        "external_ip": str(raw.get("external_ip") or ""),
        "external_port": raw.get("external_port"),
    }


def _service_path(env_id: str, vm_id: str, interface_id: str) -> str:
    return (f"/v2/configurations/{env_id}/vms/{vm_id}"
            f"/interfaces/{interface_id}/services")


async def publish_service(env_id: str, vm_id: str, interface_id: str,
                          internal_port: int) -> dict:
    """NAT one guest port to a public ip:port, and return where it landed.

    Skytap allocates the external ip and port; asking for a specific one is not offered
    here because nothing in this dashboard needs a stable address — the one caller uses it
    once and revokes it.
    """
    env_id = str(env_id or "").strip()
    vm_id = str(vm_id or "").strip()
    interface_id = str(interface_id or "").strip()
    if not (env_id and vm_id and interface_id):
        raise SkytapError(
            "an environment id, a VM id and an interface id are required to publish a "
            "service")
    try:
        port = int(internal_port)
    except (TypeError, ValueError) as exc:
        raise SkytapError(f"internal_port must be a number, not {internal_port!r}") from exc

    raw = await _client().request("POST", _service_path(env_id, vm_id, interface_id),
                                  json={"internal_port": port})
    if not isinstance(raw, dict) or not raw.get("id"):
        raise SkytapError(
            f"Skytap accepted the publish of port {port} on VM {vm_id} but returned no "
            f"service id; check the environment for a stray published service")
    out = _published_service(raw)
    if not out["external_ip"] or not out["external_port"]:
        raise SkytapError(
            f"Skytap published port {port} on VM {vm_id} as service {out['id']} but "
            f"returned no external address; revoke it and retry")
    return out


async def delete_published_service(env_id: str, vm_id: str, interface_id: str,
                                   service_id: str) -> None:
    """Revoke a published service.

    Idempotent on 404. This runs in a caller's ``finally``, where raising would replace the
    real failure with a bookkeeping one.
    """
    env_id = str(env_id or "").strip()
    vm_id = str(vm_id or "").strip()
    interface_id = str(interface_id or "").strip()
    service_id = str(service_id or "").strip()
    if not (env_id and vm_id and interface_id and service_id):
        raise SkytapError("an environment, VM, interface and service id are all required")
    try:
        await _client().request(
            "DELETE", f"{_service_path(env_id, vm_id, interface_id)}/{service_id}")
    except SkytapError as exc:
        if "(404)" in str(exc):
            logger.info("Skytap: published service %s on VM %s was already gone",
                        service_id, vm_id)
            return
        raise
