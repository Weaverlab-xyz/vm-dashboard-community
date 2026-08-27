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

from .skytap_client import DEFAULT_BASE_URL, SkytapClient, SkytapCreds, SkytapError

logger = logging.getLogger(__name__)

# Re-exported so callers can catch one error type without importing the client too.
__all__ = ["SkytapError", "VALID_RUNSTATES", "configured", "credentials",
           "list_templates", "list_environments", "get_environment",
           "create_environment", "set_runstate", "delete_environment",
           "inject_bootstrap", "stored_credentials"]

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


def _interfaces(raw_vm: dict) -> tuple[str, list[dict]]:
    """(private_ip, published_services) for a VM.

    A published service is Skytap NAT-ing a guest port to a public ip:port. We surface them
    because they are useful to see, NOT because the wire-up uses them — POV wiring reaches
    VMs on their private IPs through an in-environment Gateway. See docs/pov-instance.md.
    """
    private_ip = ""
    published: list[dict] = []
    for iface in (raw_vm.get("interfaces") or []):
        if not private_ip and iface.get("ip"):
            private_ip = str(iface["ip"])
        for svc in (iface.get("services") or []):
            published.append({
                "id": str(svc.get("id") or ""),
                "internal_port": svc.get("internal_port"),
                "external_ip": svc.get("external_ip") or "",
                "external_port": svc.get("external_port"),
            })
    return private_ip, published


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
    private_ip, published = _interfaces(raw)
    return {
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "runstate": raw.get("runstate") or "",
        "os_family": _os_family(raw),
        "private_ip": private_ip,
        "published_services": published,
    }


# ── the READ_CONTRACT ────────────────────────────────────────────────────────

async def list_templates() -> list[dict]:
    """Every template this account can instantiate."""
    raw = await _client().list("/v2/templates")
    return [_template(t) for t in raw if isinstance(t, dict)]


async def list_environments() -> list[dict]:
    """Every environment this account can see.

    Includes environments the dashboard did not create. That is deliberate for a read-only
    view — an SE's existing hand-built POVs are exactly what they want to see next to the
    managed ones — and it is why the eventual POV table keys on the Skytap id rather than
    assuming it owns everything in the account.
    """
    raw = await _client().list("/v2/configurations")
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

    body: dict = {"template_id": template_id}
    if project_id:
        body["project_id"] = str(project_id)

    raw = await _client().request("POST", "/v2/configurations", json=body)
    if not isinstance(raw, dict) or not raw.get("id"):
        raise SkytapError(
            f"Skytap accepted the create for template {template_id} but returned no "
            f"environment id; check the account for an orphan before retrying")

    env = _environment(raw)
    # Naming is a separate PUT: the create call takes a template, not a name.
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
    template contract in docs/integrations/skytap.md — that contract is the other half of
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
