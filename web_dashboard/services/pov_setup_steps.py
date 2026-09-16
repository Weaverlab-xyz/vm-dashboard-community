"""What is still to be done to a POV, in order, derived from what the row already says.

Standing a POV up is **provision, broker agent, Gateway, Resource Broker, Entitle agent,
wire-up, share** — and until this module the page stated that order nowhere. Every step was
a button, none of them said whether it was the *next* one, and a step pressed out of turn
fails with a refusal that reads like a bug rather than like a queue. So an SE learned the
sequence from a runbook or from somebody else, and the page they were looking at knew it all
along.

This turns the readiness signals ``api/pov.py::_serialize`` has already computed into one
ladder, so the page can answer "what do I press next" instead of offering eleven controls
and a guess.

**It reads, and it reads nothing new.** Every key comes from a ``describe`` that already ran
for that row — ``pov_broker``, ``pov_gateway``, ``pov_resource_broker``,
``pov_entitle_agent``, ``pov_wireup``, ``pov_share`` — each of which states in its own
docstring that it makes no network call. This module holds no session, runs no query and
reaches nothing. That is what makes it free to put on a list endpoint which renders one row
per POV, where a per-step live check would make the page as slow as the slowest customer's
network.

**A finished job is not evidence.** The one place that matters most is the wire-up.
``run_env_wireup``'s failure gate is ``if failed and not wired``, and a VM that ``wireable``
refuses increments *skipped*, not *failed* — so a POV whose guests all report no OS runs the
wire-up, skips every guest, and finishes **completed** with ``wired == 0``. The /jobs row is
green and nothing was wired. Every step here is therefore derived from the *artifact*
(``wired_count``, an enrolled agent, a minted token, a published link), never from a job's
status, and ``wireup`` refuses to call itself done on a count of zero. Surfacing that is the
most useful thing this module does, because it is a failure the page currently reports as a
success.

**Two steps can never be `done`, and that is why there are six states.** There is no stored
"installed" truth for the Gateway or the Resource Broker. A Gateway is a *cluster*: its name
is present whether or not a node of it is connected, which is why ``pov_gateway.status`` is a
live read behind a button. And of the Resource Broker, ``templates/pov/detail.html`` already
says it outright — what this dashboard knows is whether the install has everything it needs,
not whether it ran. Those two therefore report ``configured``: everything needed is in place,
and whether it took is a live question. Inventing a green tick there would be the one
genuinely harmful thing this module could do, and calling them ``ready`` forever would park
the ladder's cursor on them and never advance past.

``skipped`` is a property of the **recipe** — no Password Safe tenant, a platform that
publishes no share link. ``blocked`` is a property of the **run**. Conflating the two is how
a correctly scoped PRA-only POV reads as half broken, so they are separate states and the
page must render ``skipped`` as grey rather than as a warning.
"""
from __future__ import annotations

# The artifact exists. Derived from the artifact, never from a job's status.
DONE = "done"
# Something is in flight. Pressing anything else is refused by `may_act_on` anyway.
RUNNING = "running"
# This is the next thing to press.
READY = "ready"
# It needs something first, and `detail` says what.
BLOCKED = "blocked"
# This POV was never going to have it. Not a warning.
SKIPPED = "skipped"
# Everything this dashboard needs is in place; whether the install ran is a LIVE question.
# Only the Gateway and the Resource Broker ever report this — see the module docstring.
CONFIGURED = "configured"

# States that stop the ladder: the cursor points at the first step in one of them.
_CURSOR_STATES = (READY, BLOCKED)

# Where a step's job id lives on the row, for the steps that have one. A mapping rather
# than a special case, so a step that gains a job id later is one line here.
_JOB_KEYS = {"environment": "provision_job_id"}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _join(items: list) -> str:
    """"a, b and c" — for a refusal that names several missing things at once.

    Naming all of them matters: a message that reveals one missing field per press is how
    an operator presses the same button four times.
    """
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _no_tenant(product: str) -> str:
    """Why a product's steps are grey, phrased as a choice rather than a verdict.

    "Choose later" is a real answer on the create form — a POV is routinely stood up while
    the customer's appliance is still being provisioned — so a POV with no tenant for a
    product is not necessarily a POV that will never have one. Grey either way, because it
    is not a warning, but the remedy has to be in the text or the ladder reads as though it
    has decided something it has not.
    """
    return (f"this POV names no {product} tenant — pick one in the Tenants column to "
            f"bring these steps into its ladder")


# ── one resolver per step: (state, detail, action) ───────────────────────────
#
# `action` names what the page should offer, never a URL — the row already owns the
# handlers, and a target built here would be a second place the route lives.


def _environment(p: dict):
    status = str(p.get("status") or "")
    if status == "active":
        return DONE, "", ""
    if status == "provisioning":
        return RUNNING, "creating the environment on the lab platform", ""
    if status == "destroying":
        return SKIPPED, "this POV is being destroyed", ""
    if status == "failed":
        return BLOCKED, str(p.get("error_message") or "the provision failed"), ""
    return BLOCKED, f"the environment is {status or 'in an unknown state'}", ""


def _guest_os(p: dict):
    """Every guest's OS known — the step with no job, and the usual real blocker.

    Skytap reports no guest-OS field, so the dashboard infers one and answers *unknown*
    rather than guessing: an SSH jump built to a Windows box fails at session launch, in
    front of whoever clicked it. Unknown is therefore a state an operator has to end, and
    until they do, the broker's auto-detection, the wire-up, the Password Safe half and the
    guest steps are all blind to that guest.
    """
    if str(p.get("status") or "") != "active":
        return BLOCKED, "the environment has to be running first", ""
    total = _int(p.get("vm_count"))
    if not total:
        return BLOCKED, "the environment has reported no VMs yet", ""
    unknown = _int(p.get("os_unknown_count"))
    if not unknown:
        return DONE, "", ""
    named = [str(n) for n in (p.get("os_unknown_names") or [])]
    more = " and others" if unknown > len(named) else ""
    detail = f"{unknown} of {total} guests report no OS"
    if named:
        detail += f": {', '.join(named)}{more}"
    detail += (". Set it on the POV's VMs tab — the wire-up skips a guest whose OS is "
               "unknown and still reports success.")
    return BLOCKED, detail, "vms"


def _broker(p: dict):
    """The broker agent, which every in-environment install is leased by.

    ``pov_broker.describe`` reports ``agent_service.status_of`` plus one value that function
    does not have: ``none``, for a POV with no broker at all.
    """
    status = str(p.get("broker_status") or "none")
    if status == "online":
        return DONE, "", ""
    if status == "enrolling":
        return RUNNING, "waiting for the agent to redeem its enrolment code", ""
    error = str(p.get("broker_error") or "")
    if status == "none":
        if str(p.get("status") or "") != "active":
            return BLOCKED, "the environment has to be running first", ""
        # NOT "not started yet". `run_env_provision` installs the broker inline at its last
        # step, and `ensure_broker` waits out the enrolment code's full TTL before
        # returning — so by the time the environment is active there is nothing still
        # coming, and the remedy is already recorded on the row.
        return READY, error or ("the broker agent was never enrolled. Press Broker to "
                                "install and enrol it."), "broker"
    # offline | revoked — the agent existed and does not answer. Re-brokering replaces it.
    return READY, error or f"the broker agent is {status}. Re-broker to replace it.", "broker"


def _gateway(p: dict):
    if not p.get("pra_tenant_id"):
        return SKIPPED, _no_tenant("PRA"), ""
    if p.get("gateway_ready"):
        # `configured`, never `done` — see the module docstring.
        return (CONFIGURED,
                "configured; Check asks the appliance whether a node is connected",
                "gateway")
    missing = []
    if not p.get("gateway_name"):
        missing.append("a Gateway name")
    if not p.get("gateway_has_key"):
        missing.append("its deploy key")
    if not p.get("broker_agent_id"):
        missing.append("an enrolled broker agent")
    return BLOCKED, "needs " + _join(missing), "gateway"


def _resource_broker(p: dict):
    if not p.get("ps_tenant_id"):
        return SKIPPED, _no_tenant("Password Safe"), ""
    if p.get("rb_ready"):
        return (CONFIGURED,
                "configured; this dashboard cannot read back whether the install ran",
                "resource_broker")
    missing = []
    if not p.get("rb_asset"):
        missing.append("a staged installer")
    if not p.get("rb_zone"):
        missing.append("a resource zone")
    if not p.get("rb_has_key"):
        missing.append("an installer key")
    if not p.get("broker_agent_id"):
        missing.append("an enrolled broker agent")
    return BLOCKED, "needs " + _join(missing), "resource_broker"


def _entitle_agent(p: dict):
    if not p.get("entitle_tenant_id"):
        return SKIPPED, _no_tenant("Entitle"), ""
    if p.get("entitle_agent_installed"):
        return DONE, "", ""
    if p.get("entitle_agent_ready"):
        return READY, "", "entitle_agent"
    return BLOCKED, "needs an enrolled broker agent", "entitle_agent"


def _wireup(p: dict):
    """The VMs in PRA, Password Safe and Entitle — judged on artifacts, never on the job."""
    if not p.get("pra_tenant_id"):
        return SKIPPED, _no_tenant("PRA"), ""
    total = _int(p.get("vm_count"))
    wired = _int(p.get("wired_count"))
    errors = _int(p.get("wiring_error_count"))
    unknown = _int(p.get("os_unknown_count"))
    if total and wired >= total and not errors:
        return DONE, "", ""
    if not p.get("wireup_ready"):
        missing = []
        if not p.get("gateway_name"):
            missing.append("a Gateway name")
        if not total:
            missing.append("at least one VM")
        return BLOCKED, "needs " + _join(missing or ["a resolvable PRA tenant"]), "wireup"
    if not wired and total and unknown >= total:
        # The failure this module exists to surface: every guest would be skipped, the job
        # would complete, and the row would read as a success with nothing behind it.
        return BLOCKED, ("no guest has a known OS, so this would skip every one of them "
                         "and still report success. Set their OS first."), "vms"
    if errors and not wired:
        return READY, (f"{errors} of {total} VMs failed to wire. The POV's Wired tab has "
                       f"each row's reason."), "wireup"
    if wired < total:
        detail = f"{wired} of {total} VMs wired"
        if errors:
            detail += f", {errors} with errors"
        if unknown:
            detail += f", {unknown} with no OS"
        return READY, detail + ". Run it again to pick up the rest.", "wireup"
    return READY, "", "wireup"


def _share(p: dict):
    if not p.get("shareable"):
        return SKIPPED, "this lab platform publishes no share link", ""
    if p.get("share_url") and not p.get("share_expired"):
        return DONE, "", ""
    if p.get("share_expired"):
        return READY, "the link has expired. Re-share to replace it.", "share"
    if str(p.get("status") or "") != "active":
        return BLOCKED, "the environment has to be running first", ""
    return READY, "", "share"


# The order is the dependency order, and it is the whole point of the module. `guest_os`
# sits ahead of `broker` deliberately: broker auto-detection is blind to a guest with no
# OS, so a POV that reaches the broker step with unknown guests is one whose inline install
# has usually already failed for exactly that reason.
STEPS = (
    ("environment",     "Environment running",               _environment),
    ("guest_os",        "Every guest's OS known",            _guest_os),
    ("broker",          "Broker agent enrolled",             _broker),
    ("gateway",         "PRA Gateway",                       _gateway),
    ("resource_broker", "Resource Broker",                   _resource_broker),
    ("entitle_agent",   "Entitle agent",                     _entitle_agent),
    ("wireup",          "VMs wired into PRA / PS / Entitle", _wireup),
    ("share",           "Customer share link",               _share),
)

STEP_KEYS = tuple(key for key, _label, _fn in STEPS)


def describe(parts: dict) -> dict:
    """The ladder for one POV row.

    ``parts`` is the dict ``_serialize`` has built so far, so this MUST be called after
    every ``update()`` that contributes to it. Called earlier it would read a half-built
    row and report every step blocked — which would look like a POV problem rather than an
    ordering one, and is the single easiest way to break this module by accident.

    Returns the steps plus a cursor, because "which one is next" is a rule and it belongs
    in one place rather than in each template that renders a ladder.
    """
    steps = []
    for key, label, resolve in STEPS:
        state, detail, action = resolve(parts)
        step = {"key": key, "label": label, "state": state,
                "detail": detail, "action": action, "job_id": ""}
        job_key = _JOB_KEYS.get(key)
        if job_key and state == RUNNING:
            step["job_id"] = str(parts.get(job_key) or "")
        steps.append(step)

    # A step in flight stops the cursor dead rather than pointing past it: `may_act_on`
    # refuses every other action while a POV is mid-job, so offering one would be offering
    # a button that answers 409.
    busy = any(s["state"] == RUNNING for s in steps)
    nxt = {}
    if not busy:
        nxt = next((s for s in steps if s["state"] in _CURSOR_STATES), {})

    return {
        "steps": steps,
        "busy": busy,
        # "" when there is nothing to press: either something is running, or the POV is as
        # far along as this dashboard can take it.
        "next": nxt.get("key", ""),
        "next_label": nxt.get("label", ""),
        "next_action": nxt.get("action", ""),
        "next_detail": nxt.get("detail", ""),
        "next_blocked": nxt.get("state", "") == BLOCKED,
        # For the row's summary. `skipped` counts as settled — a PRA-only POV is finished
        # without a Resource Broker, and a denominator that included one would make a
        # correctly scoped evaluation look incomplete forever.
        "settled": sum(1 for s in steps
                       if s["state"] in (DONE, SKIPPED, CONFIGURED)),
        "total": len(steps),
        "complete": not busy and not nxt,
    }
