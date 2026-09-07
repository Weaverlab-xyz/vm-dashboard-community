"""Which failed jobs are worth running again, and which must never be.

Pure policy — stdlib only, no database, no clock of its own — so the reasoning can be
tested on two strings, the way ``expiry_policy``, ``spend_policy`` and ``vm_suspend_policy``
already split. ``job_service.set_failed`` asks this; ``jobs_worker`` honours the answer.

Sixty-plus job types and no retry at all, against a class of failures that are transient by
nature: an API throttle, a capacity shortfall, a 5xx, a token that expired mid-run. Today an
``ec2_deploy`` that hits a throttle three seconds in is failed exactly as hard as one given
a nonexistent AMI, and the operator's only recourse is to notice and redeploy by hand.

**Two allowlists, because both axes fail open if written the other way round.**

1. **Which job types may be re-entered at all.** This is the axis that matters, and the
   answer is narrow on purpose. ``aws_vm_service`` calls ``set_failed`` and does **not**
   clean up an instance it may already have launched — Azure's ``_deploy_vm_sync`` does,
   via ``_best_effort_cleanup``; the AWS path does not. Retrying a failed ``ec2_deploy``
   would launch a SECOND instance while the first is still running and billing, and the
   operator would be told a retry had helped. So a deploy is never retried, and no type is
   on this list because its name looks safe — only because someone has read its runner and
   shown it is safe to re-enter.

2. **Which failures are worth re-entering for.** Recognised transient signals only. A
   denylist ("retry unless it looks permanent") fails open on the next unfamiliar error
   message, which is how a permanent misconfiguration gets retried three times and reaches
   the operator twenty minutes late. Same reasoning as ``mcp_server._safe_extra``'s
   allowlist over ``extra_data``: the set of things that exist grows, and a rule that has
   to enumerate the bad ones is wrong the moment it does.

The failure mode this errs toward is **"no retry where one would have helped"**, never
"retried something that should not have been". On this feature that is the only acceptable
direction: the first is an operator doing by hand what they already do by hand today; the
second is duplicate cloud infrastructure nobody asked for.
"""
from __future__ import annotations

# Backoff between attempts, indexed by attempts already made. Deliberately the same shape
# as ``notify_policy.BACKOFF_SECONDS`` — that schedule is already proven on the
# notification drain, and a second invented one would be two things to reason about.
BACKOFF_SECONDS = (30, 120, 600, 1800)

# Attempts INCLUDING the first. Three means: run, retry, retry, then dead-letter — enough
# to outlast a throttle or a capacity blip, few enough that a real failure surfaces inside
# roughly three minutes rather than half an hour.
DEFAULT_MAX_ATTEMPTS = 3
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

# Job types safe to run again after a failure. **Built, not planned** — the same discipline
# `pov_cloud_cost.PRICED_CLOUDS` and `lab_platforms.CLOUD_PLATFORMS` follow, and for a
# sharper reason here: a type listed without a re-entrant runner does not degrade, it
# duplicates infrastructure.
#
# Power: starting a started VM, or stopping a stopped one, is a no-op on all four clouds —
# each runner makes exactly one API call and records the outcome.
#
# Destroy: terminating a terminated resource is a no-op, and a half-finished destroy is the
# case that most NEEDS retrying, because what it leaves behind is a VM that goes on billing.
# The wire-up teardowns a destroy runs first (PRA jump, Password Safe system, Entitle
# registration) are each guarded by a metadata key that the successful pass clears, so a
# second pass skips what the first completed.
#
# Deliberately absent: every `*_deploy` (see the module docstring), every image build and
# export, the bulk deploys, and the sweeps — a sweep that fails is re-enqueued by its own
# loop on the next interval, so a retry here would only race that.
RETRYABLE_TYPES = frozenset((
    "ec2_power", "azure_power", "gce_power", "oci_power", "pov_env_power",
    "ec2_destroy", "azure_destroy", "gce_destroy", "oci_destroy", "pov_env_destroy",
))

# Substrings that mark a failure as worth another attempt. Lowercased comparison. Drawn
# from what the four clouds' SDKs actually say — boto3, azure-core, google-api-core and
# the OCI SDK each phrase the same condition differently, which is why this is a list of
# phrasings rather than a list of exception types this module would have to import.
_TRANSIENT_SIGNALS = (
    # Rate limiting. AWS says Throttling/RequestLimitExceeded, Azure and GCP say 429.
    "throttl", "rate exceeded", "requestlimitexceeded", "toomanyrequests",
    "too many requests", "429", "quota exceeded temporarily", "retry later",
    # Capacity. A shape unavailable in one AZ now is often available minutes later.
    "insufficientinstancecapacity", "insufficient capacity", "capacityerror",
    "zone_resource_pool_exhausted", "out of capacity", "allocationfailed",
    "servicebusy", "resourcenotavailable",
    # Server-side faults. A 5xx is the remote's problem, not the request's.
    "internalerror", "internal server error", "internalservererror",
    "serviceunavailable", "service unavailable", "500", "502", "503", "504",
    "badgateway", "gatewaytimeout",
    # Transport and time. A socket that died mid-call says nothing about the request.
    "timed out", "timeout", "connection reset", "connection aborted",
    "connectionerror", "temporarily unavailable", "eof occurred",
    # Credentials that expired DURING a run — distinct from credentials that were never
    # valid, which is a configuration error and must not be retried.
    "expiredtoken", "token has expired", "securitytokenexpired",
    "request has expired", "credentials have expired",
    # boto3's actual ExpiredToken text, which none of the shorter forms above match.
    # Found by the test rather than by reading the SDK, which is the argument for
    # keeping these as observed phrasings rather than as tidy guesses.
    "token included in the request is expired",
)

# Signals that a failure is permanent even though something above also matched. Checked
# LAST and deliberately short: this is not a denylist standing in for judgement, it is the
# handful of messages where a transient word appears inside a permanent condition — an
# "InvalidAMIID" carrying a request id that happens to contain "500", or a quota that is
# exhausted for the account rather than for the moment.
_PERMANENT_OVERRIDES = (
    "quotaexceeded", "limitexceeded", "quota_exceeded",
    "invalidparameter", "validationerror", "validationexception",
    "notfound", "does not exist", "accessdenied", "unauthorized", "forbidden",
    "invalidclienttokenid", "authfailure", "signaturedoesnotmatch",
)


def max_attempts(configured=None) -> int:
    """Attempts including the first, clamped into a range where it means something.

    Below 1 nothing would ever run; above ``len(BACKOFF_SECONDS) + 1`` there is no delay
    left to apply and the tail would be a hot loop at the last interval.
    """
    try:
        value = int(configured if configured is not None else DEFAULT_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS
    return max(MIN_MAX_ATTEMPTS, min(MAX_MAX_ATTEMPTS, value))


def backoff_seconds(attempts_made: int) -> int:
    """How long to wait before the next attempt, given how many have already been made.

    Clamped at the last entry rather than raising, so a configured ``max_attempts`` that
    outruns the schedule degrades to a repeated final interval instead of an exception on
    a failure path — which is the worst possible place to raise.
    """
    index = max(0, int(attempts_made or 0))
    return BACKOFF_SECONDS[min(index, len(BACKOFF_SECONDS) - 1)]


def retryable_type(job_type: str) -> bool:
    """Whether this kind of job may be re-entered at all. See ``RETRYABLE_TYPES``."""
    return (job_type or "") in RETRYABLE_TYPES


def is_transient(error: str) -> bool:
    """Whether this failure looks like one another attempt could get past.

    Allowlist first, then the short permanent-override pass — so a message that names a
    validation error does not qualify merely because its request id contains "503".
    """
    text = (error or "").lower()
    if not text:
        # No message at all says nothing about why. Nothing is not a transient signal.
        return False
    if not any(signal in text for signal in _TRANSIENT_SIGNALS):
        return False
    return not any(bad in text for bad in _PERMANENT_OVERRIDES)


def should_retry(job_type: str, error: str, attempts_made: int,
                 *, limit: int = None) -> bool:
    """The whole question in one call: may this failed job go back on the queue?

    All three conditions, in the order that makes a refusal cheapest to explain: a type
    that may be re-entered, a failure worth re-entering for, and attempts left.
    """
    if not retryable_type(job_type):
        return False
    if not is_transient(error):
        return False
    return int(attempts_made or 0) + 1 < max_attempts(limit)


def describe(job_type: str, error: str, attempts_made: int,
             *, limit: int = None) -> dict:
    """Why this job was or was not requeued, in the shape a job page can render.

    Kept beside the decision so the answer and its reason cannot drift — a retry that
    happened for a reason the page states differently is worse than no explanation.
    """
    ceiling = max_attempts(limit)
    ok = should_retry(job_type, error, attempts_made, limit=limit)
    if ok:
        why = (f"attempt {int(attempts_made or 0) + 1} of {ceiling} — retrying in "
               f"{backoff_seconds(attempts_made)}s")
    elif not retryable_type(job_type):
        why = (f"{job_type} is not re-entrant, so a failed run is never repeated "
               f"automatically")
    elif not is_transient(error):
        why = "this failure does not look transient, so repeating it would not help"
    else:
        why = f"no attempts left ({ceiling} of {ceiling} used)"
    return {"will_retry": ok, "attempts": int(attempts_made or 0),
            "max_attempts": ceiling, "reason": why,
            "retry_in_seconds": backoff_seconds(attempts_made) if ok else None}
