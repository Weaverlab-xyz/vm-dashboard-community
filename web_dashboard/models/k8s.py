"""Pydantic models for Kubernetes management (`/api/k8s`).

Phase 1 of docs/saas-kubernetes-management-plan.md — register/list managed
clusters and store the kubeconfig as a backend reference. Provisioning,
management-plane launch, brokered access, and in-cluster secret delivery land
in later phases.
"""
from typing import Optional

from pydantic import BaseModel


class ClusterRegisterRequest(BaseModel):
    """Register an existing reachable cluster from its kubeconfig.

    Phase 1's dev-testable path — the cluster is provisioned out-of-band (a
    local cluster, or a cloud cluster stood up elsewhere); the dashboard
    records it and stows the kubeconfig as a reference.
    """
    name: str                              # dashboard-unique cluster name
    cloud: str = "local"                   # aws | azure | gcp | local
    kubeconfig: str                        # full kubeconfig YAML (stored as a reference, never in the row)
    mgmt_kind: Optional[str] = None        # portainer | rancher | argocd | headlamp (optional; set when known)


class ManagementRequest(BaseModel):
    """Launch a management plane into a registered cluster (Phase 2).

    Phase 2 wires ``portainer`` (agent + brokered Portainer server); other kinds
    are accepted but not yet launched.
    """
    mgmt_kind: str = "portainer"           # portainer | rancher | argocd | headlamp


class ClusterInfo(BaseModel):
    id: str
    cloud: str
    name: str
    status: str
    api_server: Optional[str] = None
    mgmt_kind: Optional[str] = None
    mgmt_endpoint: Optional[str] = None
    pra_jump_id: Optional[str] = None
    secrets_delivery_kind: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str
