"""Which agent runs a cluster's Kubernetes work — and the rules for letting one.

Kubernetes work hands the runner a **cluster-admin** kubeconfig. The remote worker gets one
for a task that lives minutes and has no host to compromise between jobs. An agent is a
persistent host, so only an **owned** agent may take that role
(docs/design/remote-worker-and-agent.md, phase 3). Every rule is read from the dashboard's
own rows, never from anything the agent reports:

  1. it has an ``agent_hosts`` row: the dashboard deployed it, into a host it created;
  2. it is SPIRE-attested (``auth_mode == "spiffe"``), whatever the global
     ``dashboard_secrets_require_spire`` says — this is the most powerful credential the
     dashboard hands out;
  3. its host has Kubernetes work allowed (``k8s_enabled``), so its policy carries the
     ``k8s:`` block and ``agent_k8s``;
  4. it runs agent 2.13 or later;
  5. it is active and holds the ``agent_k8s`` grant;
  6. it runs in the cluster's cloud.

Those decide whether an agent may be CHOSEN. Whether work goes to it NOW also needs the
updater beside it to have verified the Kubernetes image's cosign signature against the
policy's ``k8s.verify`` (:func:`signature_refusal`). That one is the agent's own report:
the agent enforces the same check before it runs anything, and a host still verifying, or
one whose image did not verify, sends the cluster's work to the remote worker meanwhile.

A cluster names its executor explicitly (``k8s_clusters.executor_agent_id``): its API
endpoint is public and EKS/AKS clusters sit in their own peered networks, so no route can
pick the agent by address. Checked when set and again at every use (:func:`executor_for`):
an agent that stops qualifying sends the work back to the remote worker rather than
failing it.
"""
from __future__ import annotations

from typing import Optional

K8S_JOB_TYPE = "agent_k8s"
CLOUDS = ("aws", "azure", "gcp")


class ExecutorError(Exception):
    """An executor choice the rules refuse; the message names the rule."""


def agent_refusal(db, agent, cluster=None) -> str:
    """Why ``agent`` may not run Kubernetes work (for ``cluster``), or "" if it may."""
    from . import agent_host_service, agent_service
    if agent is None:
        return "That agent is not registered."
    if not agent.is_active:
        return f"Agent '{agent.name}' is revoked."
    host = agent_host_service.for_agent(db, agent.id)
    if host is None:
        return (f"Agent '{agent.name}' was not deployed by this dashboard. Only an agent "
                f"the dashboard deployed may run Kubernetes work: it receives a "
                f"cluster-admin kubeconfig.")
    if (agent.auth_mode or "") != "spiffe":
        return (f"Agent '{agent.name}' is not SPIRE-attested. Kubernetes work needs a "
                f"SPIRE-attested agent; turn on SPIRE attestation and restart its host.")
    if not host.k8s_enabled:
        return (f"Kubernetes work is not allowed on agent '{agent.name}'s host. Turn on "
                f"Allow Kubernetes work for it on the Agents page, then restart it.")
    if not agent_service.supports_k8s(agent):
        return agent_service.k8s_upgrade_hint(agent)
    if K8S_JOB_TYPE not in agent_service.allowed_job_types(agent):
        return f"Agent '{agent.name}' is not granted Kubernetes work (`{K8S_JOB_TYPE}`)."
    if cluster is not None:
        cloud = (cluster.cloud or "").lower()
        if cloud not in CLOUDS:
            return ("Only a cluster in AWS, Azure or GCP can run its Kubernetes work on a "
                    "deployed agent; an on-premises cluster keeps the dashboard's runner.")
        if (host.cloud or "").lower() != cloud:
            return (f"Agent '{agent.name}' runs in {host.cloud}, and this cluster is in "
                    f"{cloud}. Choose an agent deployed into the cluster's cloud.")
    return ""


def signature_refusal(agent) -> str:
    """Why ``agent``'s Kubernetes image is not known to be signed, or "" if its updater
    verified it. As the agent last reported it (2.13+)."""
    state = (getattr(agent, "control_k8s_signature", None) or "").strip()
    if state == "verified":
        return ""
    name = getattr(agent, "name", "")
    if state == "pending":
        return (f"Agent '{name}' is still verifying its Kubernetes image's signature; its "
                f"Kubernetes work runs on the remote worker until it has.")
    if state.startswith("failed"):
        return (f"Agent '{name}''s Kubernetes image did not verify "
                f"({state.partition(':')[2].strip() or 'no reason given'}), so it runs no "
                f"Kubernetes work.")
    if state == "unchecked":
        return (f"Agent '{name}''s policy names no signer for its Kubernetes image "
                f"(`k8s.verify`). Restart its host so the dashboard writes one.")
    return f"Agent '{name}' has not reported a check of its Kubernetes image's signature."


def ready_refusal(db, agent, cluster=None) -> str:
    """:func:`agent_refusal`, then :func:`signature_refusal`: whether work goes to
    ``agent`` now, rather than whether it may be chosen."""
    return agent_refusal(db, agent, cluster) or (
        signature_refusal(agent) if agent is not None else "")


def qualifies(db, agent) -> bool:
    """Whether ``agent`` may lease Kubernetes work now (no particular cluster)."""
    return not ready_refusal(db, agent)


def executor_for(db, cluster) -> Optional[object]:
    """The agent that runs ``cluster``'s Kubernetes work, or None for the remote worker.
    An executor that no longer qualifies reads as None, so the work goes back to the
    worker instead of failing."""
    from ..database import RemoteAgent
    agent_id = getattr(cluster, "executor_agent_id", None) or ""
    if not agent_id:
        return None
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    return agent if agent is not None and not ready_refusal(db, agent, cluster) else None


def candidates(db, cluster) -> list:
    """``[{id, name, reason}]`` of every deployed agent in the cluster's cloud, with the
    reason one cannot be chosen ("" if it can), for the cluster's executor picker."""
    from ..database import AgentHost, RemoteAgent
    out = []
    for host in db.query(AgentHost).all():
        if (host.cloud or "").lower() != (cluster.cloud or "").lower():
            continue
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
        if agent is None:
            continue
        out.append({"id": agent.id, "name": agent.name,
                    "reason": agent_refusal(db, agent, cluster),
                    # Choosable while still verifying; the work waits on the worker.
                    "signature": agent.control_k8s_signature or "",
                    "signature_note": signature_refusal(agent)})
    return sorted(out, key=lambda a: a["name"].lower())


def set_executor(db, cluster, agent_id: str) -> None:
    """Name ``agent_id`` as the cluster's executor, or "" for the remote worker. Raises
    ExecutorError naming the rule an agent breaks."""
    from ..database import RemoteAgent
    agent_id = (agent_id or "").strip()
    if agent_id:
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        why = agent_refusal(db, agent, cluster)
        if why:
            raise ExecutorError(why)
    cluster.executor_agent_id = agent_id or None
    db.commit()


def clear_for_agent(db, agent_id: str) -> int:
    """Send every cluster that names ``agent_id`` back to the remote worker. For when the
    agent or its host is deleted."""
    from ..database import K8sCluster
    rows = db.query(K8sCluster).filter(K8sCluster.executor_agent_id == agent_id).all()
    for row in rows:
        row.executor_agent_id = None
    db.commit()
    return len(rows)
