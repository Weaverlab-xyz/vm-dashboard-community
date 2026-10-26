"""Kubernetes runner operations on a cluster's owned agent (``agent_k8s``).

The remote worker runs ``k8s_runner_service.run`` inline, awaited inside the job that needs
it (ESO install, Entitle agent, RBAC, bindings). An agent leases its work asynchronously,
so this is the bridge: :func:`run_steps` queues an ``agent_k8s`` job for the cluster's
executor and waits for it, copying its Live Output into the caller's job, and returns or
raises exactly what ``k8s_runner_service.run`` does. A caller does not change shape.

Only for steps that return nothing secret. The token mints (Entitle external, PRA vault,
Password Safe) hand a credential back to the dashboard mid-job and stay on the worker.

**Fallback.** When the cluster has no executor, or it no longer qualifies
(k8s_executor_service), or it is not online and its host will not be up, the work runs on
the remote worker as it always did, and the caller's Live Output says so.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from . import agent_k8s_meta, job_service

logger = logging.getLogger(__name__)

JOB_TYPE = "agent_k8s"
POLL_S = 2.0
WAIT_S = 25 * 60          # the worker's own ceiling is 20 minutes of task time
TERMINAL = ("completed", "failed", "cancelled")


def build_bundle(db, *, job, agent) -> tuple:
    """``(bundle, scrub)`` for a leased ``agent_k8s`` job: the kubeconfig, minted now so a
    15-minute EKS token is fresh, and the decrypted steps. Raises K8sMetaError."""
    from ..database import K8sCluster
    from . import ansible_cloud_run_service, k8s_executor_service, k8s_service
    meta = job.metadata_dict or {}
    cluster = (db.query(K8sCluster)
               .filter(K8sCluster.id == (meta.get("cluster_id") or "")).first())
    if cluster is None:
        raise agent_k8s_meta.K8sMetaError("That cluster no longer exists.")
    why = k8s_executor_service.agent_refusal(db, agent, cluster)
    if why or (cluster.executor_agent_id or "") != agent.id:
        raise agent_k8s_meta.K8sMetaError(
            why or "This cluster's Kubernetes work no longer runs on this agent.")
    host, port = agent_k8s_meta.endpoint(cluster.api_server or "")
    if (host, port) != (meta.get("cluster_host"), meta.get("cluster_port")):
        raise agent_k8s_meta.K8sMetaError("The cluster's API endpoint changed since this "
                                          "job was queued. Run it again.")
    kubeconfig = k8s_service._runner_kubeconfig(k8s_service.resolve_kubeconfig(db, cluster.id))
    steps = agent_k8s_meta.steps_of(meta)
    scrub = list(ansible_cloud_run_service._kubeconfig_tokens(kubeconfig))
    for step in steps:
        for key in ("manifest", "values"):
            if step.get(key):
                scrub.append(str(step[key]))
    return {"kubeconfig": kubeconfig, "steps": steps}, [s for s in scrub if len(s) >= 4]


def _usable_now(db, agent) -> bool:
    """Online, or a deployed agent whose host is up for a run starting now."""
    from . import agent_host_service, agent_service
    if agent_service.status_of(agent) == "online":
        return True
    return not agent_host_service.booking_refusal(db, agent, None)


async def run_steps(db, cluster, steps: list, *, parent_job_id: str = "",
                    legacy: Optional[Callable] = None, created_by: str = "system") -> str:
    """Run ``steps`` on the cluster's owned agent and wait, or call ``legacy()`` (the remote
    worker path, an awaitable factory) when there is none. Returns the log text; raises
    ``K8sRunnerError`` with it when a step fails."""
    from . import k8s_executor_service
    from .k8s_runner_service import K8sRunnerError

    def _note(line: str) -> None:
        if parent_job_id:
            job_service.append_job_log(db, parent_job_id, line)

    agent = k8s_executor_service.executor_for(db, cluster)
    if agent is not None and not _usable_now(db, agent):
        _note(f"Agent '{agent.name}' is not online, so this Kubernetes work runs on the "
              f"remote worker.")
        agent = None
    if agent is None:
        if legacy is None:
            raise K8sRunnerError("This cluster has no agent to run its Kubernetes work.")
        return await legacy()

    meta = agent_k8s_meta.run_meta(cluster=cluster, steps=steps, parent_job_id=parent_job_id)
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                 workgroup="k8s", metadata=meta, agent_id=agent.id)
    _note(f"Running {len(steps)} Kubernetes step(s) on agent '{agent.name}' (job {job.id}).")
    lines, seen = [], 0
    deadline = time.monotonic() + WAIT_S
    try:
        while True:
            await asyncio.sleep(POLL_S)
            db.expire_all()
            for seq, line in job_service.get_job_logs(db, job.id, after_seq=seen):
                seen = seq
                lines.append(line)
                _note(line)
            row = job_service.get_job(db, job.id)
            if row is not None and row.status in TERMINAL:
                break
            if time.monotonic() > deadline:
                job_service.set_cancelled(db, job.id)
                raise K8sRunnerError(
                    f"Agent '{agent.name}' did not finish the Kubernetes work in "
                    f"{WAIT_S // 60} minutes; it was cancelled.", "\n".join(lines))
    finally:
        row = job_service.get_job(db, job.id)
        if row is not None:
            agent_k8s_meta.wipe_steps(db, row)
    output = "\n".join(lines)
    if row.status != "completed":
        raise K8sRunnerError(
            f"Kubernetes work on agent '{agent.name}' {row.status}: "
            f"{row.error_message or 'see its Live Output'}", output)
    return output
