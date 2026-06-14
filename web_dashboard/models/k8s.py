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


class BrokerAccessRequest(BaseModel):
    """Per-cluster broker overrides (Phase 3b). All optional — omitted fields fall
    back to the configured defaults. ``pra_credential_ref`` is a secrets-backend
    *reference* (e.g. ``aws_sm://…``), not a raw secret."""
    jump_group: Optional[str] = None          # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name: Optional[str] = None       # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref: Optional[str] = None  # secret ref → bt_client_secret override for the apply


class SecretDeliveryRequest(BaseModel):
    """Choose the in-cluster Password Safe secret-delivery mechanism (Phase 4).
    v1: ``eso`` (External Secrets Operator → Password Safe) or ``none`` (remove)."""
    kind: str = "eso"                         # eso | none  (secrets_agent is a later kind)


class ClusterInfo(BaseModel):
    id: str
    cloud: str
    name: str
    status: str
    api_server: Optional[str] = None
    mgmt_kind: Optional[str] = None
    mgmt_endpoint: Optional[str] = None
    pra_jump_id: Optional[str] = None
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None
    pra_credential_ref: Optional[str] = None
    secrets_delivery_kind: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str
