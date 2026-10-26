"""What an ``agent_k8s`` job carries: on its row, on the wire, and sealed.

Three places, three allowlists, for the reason ``agent_ansible_meta`` gives:

  * **The job row** (:func:`run_meta`) holds refs and the steps ENCRYPTED. A step can carry
    a Secret manifest or Helm values with a credential in them, so the steps are never
    stored readable; they are wiped when the job ends (:func:`wipe_steps`).
  * **The signed envelope** (:func:`envelope_payload`) carries the cluster endpoint and the
    step KINDS — nothing a credential could ride in. The agent resolves and checks the
    endpoint before it fetches anything, and refuses a bundle whose steps are not the kinds
    the envelope listed.
  * **The sealed bundle** (k8s_agent_runner.build_bundle) carries the kubeconfig, minted at
    lease time, and the steps.

The step kinds are the agent's closed set (runners/agent/agent.py ``K8S_STEP_KINDS``): the
dashboard can describe a step, never a command.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

STEP_KINDS = ("apply", "delete", "helm_upgrade", "helm_uninstall")
MAX_STEPS = 16
# The agent's cap on one manifest or values file (runners/agent/agent.py K8S_MAX_DOC_BYTES).
MAX_DOC_BYTES = 256 * 1024
ENVELOPE_KEYS = ("cluster_host", "cluster_port", "step_kinds")


class K8sMetaError(Exception):
    pass


def endpoint(api_server: str) -> tuple:
    """``(host, port)`` of a cluster API URL; 443 when the URL names no port."""
    parsed = urlparse((api_server or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise K8sMetaError("This cluster has no HTTPS API server recorded, so an agent has "
                           "nothing to connect to.")
    return parsed.hostname, parsed.port or 443


def check_steps(steps) -> list:
    """The step list, or raise. The agent validates every field again; this catches a
    malformed list before a job exists, where the error is a stack trace not a refusal."""
    if not isinstance(steps, list) or not steps:
        raise K8sMetaError("No Kubernetes steps to run.")
    if len(steps) > MAX_STEPS:
        raise K8sMetaError(f"At most {MAX_STEPS} Kubernetes steps run in one job.")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("kind") not in STEP_KINDS:
            raise K8sMetaError(f"Step {index} is not one of {', '.join(STEP_KINDS)}.")
        for key in ("manifest", "values"):
            if len(str(step.get(key) or "").encode("utf-8")) > MAX_DOC_BYTES:
                raise K8sMetaError(f"Step {index}'s {key} is over {MAX_DOC_BYTES} bytes.")
    return steps


def run_meta(*, cluster, steps: list, parent_job_id: str = "", description: str = "") -> dict:
    from . import config_service
    host, port = endpoint(cluster.api_server or "")
    check_steps(steps)
    return {"description": description or f"Kubernetes work on {cluster.name}",
            "cluster_id": cluster.id, "cluster_host": host, "cluster_port": port,
            "step_kinds": [s["kind"] for s in steps], "parent_job_id": parent_job_id,
            "steps_enc": config_service.encrypt_value(json.dumps(steps))}


def envelope_payload(meta: dict) -> dict:
    meta = meta or {}
    try:
        port = int(meta.get("cluster_port") or 0)
    except (TypeError, ValueError):
        port = 0
    return {"cluster_host": str(meta.get("cluster_host") or ""), "cluster_port": port,
            "step_kinds": [str(k) for k in (meta.get("step_kinds") or [])
                           if k in STEP_KINDS][:MAX_STEPS]}


def steps_of(meta: dict) -> list:
    from . import config_service
    raw = (meta or {}).get("steps_enc") or ""
    if not raw:
        raise K8sMetaError("This job's steps are gone: it already ended.")
    return check_steps(json.loads(config_service.decrypt_value(raw) or "null"))


def wipe_steps(db, job) -> None:
    """Drop the encrypted steps from an ended job."""
    meta = dict(job.metadata_dict or {})
    if meta.pop("steps_enc", None) is not None:
        job.metadata_dict = meta
        db.commit()
