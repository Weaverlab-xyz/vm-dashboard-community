"""Job metadata for an ``agent_ui_robot`` run: a browser robot started beside a POV control
node's agent, to do the two setup steps the Password Safe and PRA APIs cannot.

Design: ``docs/profiles/pov/design/ui-robot.md``.

The allowlist is the same funnel ``agent_gateway_meta`` is, and for the same reason: this
crosses into somebody else's network, so **every field is a scalar or an enum and there is
no free-form string anywhere.** No image, no URL, no selector, no path.

Two things a reader will look for and not find:

**The image is not here.** It comes from the agent's own ``policy.yaml`` (``ui_robot.image``).
A job says only which of two tasks to do.

**No credential is here, and no environment id.** The Password Safe and PRA logins ride the
sealed ``/jobs/{id}/ui-robot-bundle`` channel. ``environment_id`` lives on the job ROW, where
the bundle route reads it; the agent has no use for it, so it is not in the envelope.

Pure and stdlib-only.
"""
import copy

UI_ROBOT_META_KEYS = (
    "task",        # one of VALID_TASKS
    "timeout_s",   # int, how long the agent lets the robot run
)

# ``installer``: fetch the Resource Broker installer and key from Password Safe, upload the
# installer, stage the key on the POV. ``integration``: create the PRA to Password Safe
# integration. An enum, and a closed one: adding a task is an edit here and in the pinning test.
VALID_TASKS = ("installer", "integration")

_DEFAULTS = {"task": "installer", "timeout_s": 900}

_TIMEOUT_MIN = 60
_TIMEOUT_MAX = 3600


def ui_robot_meta(payload, *, description: str) -> dict:
    """The job row's metadata. ``description`` is for the Jobs page, not the envelope."""
    meta = normalize(payload)
    meta["description"] = str(description or "")[:200]
    return meta


def envelope_payload(meta: dict) -> dict:
    """The subset that crosses to the agent in the signed envelope: the two scalars.

    The ONLY builder for this job type. The agent's ``run_ui_robot`` reads
    ``payload["task"]`` and ``payload["timeout_s"]`` straight off the envelope, so a rename
    here that is not made there does not fail, it runs the default task.
    """
    clean = normalize(meta)
    return {key: clean[key] for key in UI_ROBOT_META_KEYS}


def normalize(meta: dict) -> dict:
    """Coerce anything into the closed shape, falling back rather than raising.

    Forgiving so an old row stays actionable. An unknown task becomes the default, which
    only fetches and stages; it can never become a different, larger action.
    """
    src = meta if isinstance(meta, dict) else {}
    out = copy.deepcopy(_DEFAULTS)
    task = str(src.get("task") or "").strip().lower()
    if task in VALID_TASKS:
        out["task"] = task
    try:
        n = int(src.get("timeout_s"))
    except (TypeError, ValueError):
        n = _DEFAULTS["timeout_s"]
    out["timeout_s"] = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, n))
    return out


def check(meta: dict) -> str:
    """Why this metadata could not be run, or "" if it can. For the queueing path."""
    task = str((meta or {}).get("task") or "").strip().lower()
    if task and task not in VALID_TASKS:
        return (f"{task!r} is not a UI robot task this build knows "
                f"(known: {', '.join(VALID_TASKS)})")
    return ""
