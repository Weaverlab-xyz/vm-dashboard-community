"""An ECS / Cloud Run Ansible runner collects its run's credential from the dashboard.

docs/design/dashboard-workload-identity.md, Slice 5. With
``ansible_runner_credential_callback`` on, a managed-account credential for a run on the
ECS or Cloud Run runner is no longer copied into AWS or GCP Secrets Manager. Instead:

  * ``issue`` stores the run's resolved credential vars, encrypted with the dashboard's
    own key, under the SHA-256 of a 256-bit token, for ten minutes, single-use;
  * the task (``services/runner_fetch``, shipped in its environment) proves its own cloud
    identity, bound to that token, and calls ``POST /api/agent/runner-credential``;
  * ``redeem`` burns the token FIRST (single-use even on failure), checks the reply key
    before anything else is touched, verifies the identity against the configured runner
    — ECS: STS says the presigned caller is the configured task role; Cloud Run: Google
    signed an ID token for the configured service account, with the binding audience —
    and seals the values to the task's key (``agent_sealing``);
  * ``revoke_for_job`` deletes the grant when the run ends, redeemed or not.

The token alone is worth nothing: a reader of ``ecs:DescribeTasks`` or the Cloud Run job
who copies it still has to BE the runner's identity. A token presented with a proof made
for another token is refused, because the proof carries the token's hash.

**Not SPIFFE.** A Fargate or Cloud Run task cannot run a SPIRE agent; the platform's own
attestation is the stronger statement here, and the win is collect-and-seal instead of a
copy in a second store.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urlparse

from . import agent_sealing, config_service, runner_fetch

SETTING = "ansible_runner_credential_callback"
ECS_TASK_ROLE = "ansible_ecs_task_role_arn"
GCP_RUNNER_SA = "gcp_ansible_runner_service_account"
TTL = timedelta(minutes=10)
ROUTE = "/api/agent/runner-credential"
REF = runner_fetch.REF
RUNNERS = ("ecs", "gcp")

_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_ROLE_ARN = re.compile(r"^arn:aws:iam::(\d{12}):role/(?:[\w+=,.@-]+/)*([\w+=,.@-]+)$")
_ASSUMED = re.compile(r"^arn:aws:sts::(\d{12}):assumed-role/([\w+=,.@-]+)/[\w+=,.@-]+$")
_FORWARDED = ("authorization", "content-type", "x-amz-date", "x-amz-security-token",
              runner_fetch.BINDING_HEADER)


class RunnerCredentialError(Exception):
    """A refusal. The message names the check that failed and never a token or value."""


def enabled() -> bool:
    return config_service.get_bool(SETTING)


def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val:
        return str(val).strip()
    from ..config import settings
    return str(getattr(settings, key, "") or "").strip()


def audience() -> str:
    """The pinned agent URL — what the task calls and what the seal is bound to."""
    from . import agent_service
    return (config_service.get(agent_service.AUDIENCE_CONFIG) or "").strip().rstrip("/")


def callback_url() -> str:
    base = audience()
    return f"{base}{ROUTE}" if base else ""


def problem(runner: str) -> str:
    """Why collect-from-dashboard cannot be used for ``runner`` now, or "". Checked
    before a run starts, so a misconfiguration never reaches a launched task."""
    if runner not in RUNNERS:
        return f"the {runner} runner has no collect-from-dashboard path"
    if not callback_url():
        return ("collect-from-dashboard needs the agent URL pinned (Settings → Remote "
                "Agents): the runner task calls the dashboard there")
    if runner == "ecs" and not _ROLE_ARN.match(_cfg(ECS_TASK_ROLE)):
        return ("collect-from-dashboard on ECS needs the runner's task role ARN "
                f"({ECS_TASK_ROLE}): it is the identity the task proves")
    if runner == "gcp" and "@" not in _cfg(GCP_RUNNER_SA):
        return ("collect-from-dashboard on Cloud Run needs the runner's service account "
                f"({GCP_RUNNER_SA}): it is the identity the task proves")
    return ""


def use_for(runner: str) -> bool:
    """Whether a run on ``runner`` collects its credential from the dashboard."""
    return enabled() and not problem(runner)


def cloud_delivery_available(runner: str = "") -> bool:
    """Can a managed-account credential reach an ECS / Cloud Run task at all: the
    Secrets Manager copy is on, or collect-from-dashboard is on and configured for this
    runner (any runner, when none is named — what the pickers ask)."""
    if config_service.get_bool("ansible_cloud_ephemeral_secrets_enabled"):
        return True
    if not enabled():
        return False
    return use_for(runner) if runner else any(use_for(r) for r in RUNNERS)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(db, *, job_id: str, runner: str, values: dict,
          now: Optional[datetime] = None) -> str:
    from ..database import RunnerCredentialGrant
    if runner not in RUNNERS:
        raise RunnerCredentialError(f"no collect-from-dashboard path for {runner}")
    now = now or datetime.utcnow()
    # Opportunistic: a run that crashed before its own revoke left an expired grant —
    # unusable, values encrypted, but not worth keeping. Each issue clears them.
    sweep(db, now)
    token = secrets.token_urlsafe(32)
    db.add(RunnerCredentialGrant(
        token_hash=_hash(token), job_id=str(job_id), runner=runner,
        values_enc=config_service._encrypt(json.dumps(dict(values))),
        expires_at=now + TTL))
    db.commit()
    return token


def revoke_for_job(db, job_id: str) -> int:
    from ..database import RunnerCredentialGrant
    n = db.query(RunnerCredentialGrant).filter(
        RunnerCredentialGrant.job_id == str(job_id)).delete(synchronize_session=False)
    db.commit()
    return n


def sweep(db, now: Optional[datetime] = None) -> int:
    """Delete grants past their expiry — a run that crashed before its own revoke."""
    from ..database import RunnerCredentialGrant
    n = db.query(RunnerCredentialGrant).filter(
        RunnerCredentialGrant.expires_at < (now or datetime.utcnow())).delete(
        synchronize_session=False)
    db.commit()
    return n


# ── the two proofs ────────────────────────────────────────────────────────────

def _sts_post(url: str, headers: dict, body: str) -> tuple:
    import requests
    r = requests.post(url, headers=headers, data=body, timeout=10, allow_redirects=False)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def verify_ecs(proof: dict, token: str, *, post: Callable = _sts_post) -> str:
    """The caller ARN STS reports for the presigned request, if it is the configured task
    role and the request is bound to ``token``. The URL is checked to be exactly a
    regional STS endpoint BEFORE anything is sent, so this can never reach another host."""
    role = _ROLE_ARN.match(_cfg(ECS_TASK_ROLE))
    if not role:
        raise RunnerCredentialError("no ECS task role is configured to verify against")
    if not isinstance(proof, dict) or proof.get("method") != "POST":
        raise RunnerCredentialError("the ECS proof is not a presigned POST")
    url = str(proof.get("url") or "")
    p = urlparse(url)
    region = p.hostname[len("sts."):-len(".amazonaws.com")] if (
        p.hostname or "").startswith("sts.") and (p.hostname or "").endswith(
        ".amazonaws.com") else ""
    if (p.scheme != "https" or p.netloc != f"sts.{region}.amazonaws.com"
            or not _REGION.match(region) or p.path != "/" or p.query or p.params
            or p.fragment):
        raise RunnerCredentialError("the ECS proof is not addressed to a regional STS "
                                    "endpoint")
    if proof.get("body") != runner_fetch.STS_BODY:
        raise RunnerCredentialError("the ECS proof is not a GetCallerIdentity call")
    headers = {str(k).lower(): str(v) for k, v in (proof.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    signed = (re.search(r"SignedHeaders=([^,\s]+)", auth) or [None, ""])[1].split(";")
    if runner_fetch.BINDING_HEADER not in signed or "host" not in signed:
        raise RunnerCredentialError("the ECS proof does not sign this run's binding")
    if headers.get(runner_fetch.BINDING_HEADER) != runner_fetch.binding(token):
        raise RunnerCredentialError("the ECS proof was made for a different token")
    forward = {k: headers[k] for k in _FORWARDED if k in headers}
    forward["accept"] = "application/json"
    status, doc = post(url, forward, runner_fetch.STS_BODY)
    if status != 200:
        raise RunnerCredentialError(f"STS did not accept the ECS proof ({status})")
    arn = str(((doc.get("GetCallerIdentityResponse") or {})
               .get("GetCallerIdentityResult") or {}).get("Arn") or "")
    m = _ASSUMED.match(arn)
    if not m or m.group(1) != role.group(1) or m.group(2) != role.group(2):
        raise RunnerCredentialError("the ECS task is not running as the configured runner "
                                    "task role")
    return arn


def _verify_google(id_token: str, aud: str) -> dict:
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token
    return g_id_token.verify_oauth2_token(id_token, g_requests.Request(), audience=aud)


def verify_gcp(proof: dict, token: str, *, verify: Callable = _verify_google) -> str:
    """The service account Google says signed in, if it is the configured runner SA and
    the token's audience carries this run's binding."""
    sa = _cfg(GCP_RUNNER_SA)
    if "@" not in sa:
        raise RunnerCredentialError("no Cloud Run runner service account is configured")
    raw = str((proof or {}).get("id_token") or "") if isinstance(proof, dict) else ""
    if raw.count(".") != 2:
        raise RunnerCredentialError("the Cloud Run proof is not an ID token")
    want = runner_fetch.gcp_audience(callback_url(), token)
    try:
        claims = verify(raw, want)
    except Exception as exc:  # noqa: BLE001 — google-auth raises several types
        raise RunnerCredentialError(
            f"Google did not verify the Cloud Run proof ({type(exc).__name__})") from exc
    if claims.get("aud") != want:
        raise RunnerCredentialError("the Cloud Run proof was made for a different token")
    if claims.get("email") != sa or claims.get("email_verified") is not True:
        raise RunnerCredentialError("the Cloud Run task is not running as the configured "
                                    "runner service account")
    return sa


def redeem(db, *, token: str, proof: dict, reply_key: str,
           now: Optional[datetime] = None, sts_post: Callable = _sts_post,
           google_verify: Callable = _verify_google) -> tuple:
    """``(sealed envelope, {job_id, runner, identity})``, or RunnerCredentialError."""
    from ..database import RunnerCredentialGrant
    now = now or datetime.utcnow()
    h = _hash(str(token or ""))
    # Burn it first: an atomic claim, so two racing callers cannot both proceed and a
    # failed proof still spends the token.
    claimed = db.query(RunnerCredentialGrant).filter(
        RunnerCredentialGrant.token_hash == h,
        RunnerCredentialGrant.redeemed_at.is_(None),
        RunnerCredentialGrant.expires_at >= now).update(
        {"redeemed_at": now}, synchronize_session=False)
    db.commit()
    if claimed != 1:
        raise RunnerCredentialError("this runner token is unknown, expired or already used")
    grant = db.query(RunnerCredentialGrant).filter(
        RunnerCredentialGrant.token_hash == h).one()
    try:
        agent_sealing.check_reply_key(reply_key)
    except agent_sealing.SealError as exc:
        raise RunnerCredentialError(f"the reply key cannot be sealed to: {exc}") from exc
    identity = (verify_ecs(proof, token, post=sts_post) if grant.runner == "ecs"
                else verify_gcp(proof, token, verify=google_verify))
    values = config_service._decrypt(grant.values_enc)
    envelope = agent_sealing.seal(reply_key, values, agent_id=f"runner:{grant.runner}",
                                  audience=audience(), job_id=grant.job_id, ref=REF)
    meta = {"job_id": grant.job_id, "runner": grant.runner, "identity": identity}
    grant.values_enc = ""          # spent: nothing left to decrypt even before the revoke
    db.commit()
    return envelope, meta
