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


class ClusterProvisionRequest(BaseModel):
    """Provision a new cluster with Terraform. The dashboard provisions the cluster,
    stores the generated kubeconfig as a reference, and flips the record to
    ``registered`` so the manage / broker / secrets / delete flows apply unchanged.
    Implemented for ``aws`` (EKS), ``azure`` (AKS), and ``gcp`` (GKE). All three
    create their own network + egress; the EKS build additionally peers its VPC
    back to the sandbox VPC for direct management-plane access.
    """
    name: str                                 # dashboard-unique cluster name
    cloud: str = "aws"                        # aws (EKS) | azure (AKS) | gcp (GKE)
    region: str                               # cloud region/location (e.g. us-east-2, eastus, us-central1)
    k8s_version: Optional[str] = None         # control-plane version (else config / module default)
    node_instance_type: Optional[str] = None  # node size: EC2 type / AKS vm_size / GKE machine type
    node_count: Optional[int] = None          # desired node count (else module default)
    vpc_cidr: Optional[str] = None            # AWS only — the EKS module's own VPC CIDR (default 10.97.0.0/16; distinct per concurrent cluster)
    authorized_cidrs: Optional[list[str]] = None  # restrict the public API endpoint (empty = open)
    zone: Optional[str] = None                # GCP only — zonal cluster zone (else <region>-a)
    enable_ebs_csi: Optional[bool] = None     # AWS only — install the EBS CSI driver addon (dynamic PVCs); off by default, opt in for stateful workloads (e.g. Rancher)


class K8sProvisionOptions(BaseModel):
    """Served pickers for the provision modal (region-scoped). Strict-select sources:
    every list is curated and always includes the configured/sandbox value so the
    form can't lock the operator out. AWS additionally serves the live VPC subnets
    (the EKS subnet override) + the two configured sandbox subnet ids the frontend
    pre-selects; AKS/GKE create their own network, so ``subnets`` /
    ``configured_subnet_ids`` are empty."""
    cloud: str
    region: str
    regions: list[str]
    node_instance_types: list[str]
    k8s_versions: list[str]
    subnets: list[dict] = []               # AWS only: [{id, name, vpc_id, az, cidr}]
    configured_subnet_ids: list[str] = []  # AWS only: aws_k8s_subnet_a_id / _b_id
    cached_at: Optional[str] = None


class ManagementRequest(BaseModel):
    """Launch a management plane into a registered cluster (Phase 2).

    Phase 2 wires ``rancher`` (central Rancher + import); other kinds are accepted
    but not yet launched.
    """
    mgmt_kind: str = "rancher"             # rancher | argocd | headlamp


class BrokerAccessRequest(BaseModel):
    """Per-cluster broker overrides (Phase 3b). All optional — omitted fields fall
    back to the configured defaults. ``pra_credential_ref`` is a secrets-backend
    *reference* (e.g. ``aws_sm://…``), not a raw secret."""
    jump_group: Optional[str] = None          # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name: Optional[str] = None       # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref: Optional[str] = None  # secret ref → bt_client_secret override for the apply
    vault_inject: bool = False                # mint a cluster SA token + store it in the PRA Vault for injection (PRA-only access)
    vault_account_group_id: Optional[int] = None  # PRA Vault account group for the injected token (else bt_vault_account_group_id)


class SecretDeliveryRequest(BaseModel):
    """Choose the in-cluster Password Safe secret-delivery mechanism (Phase 4).
    v1: ``eso`` (External Secrets Operator → Password Safe) or ``none`` (remove)."""
    kind: str = "eso"                         # eso | none  (secrets_agent is a later kind)


class EntitleAgentRequest(BaseModel):
    """Install or remove the Entitle agent in a managed cluster (agent-cluster
    bootstrap). The token is resolved server-side from ``entitle_agent_token_ref``."""
    action: str = "install"                   # install | remove


class EntitleClusterRegisterRequest(BaseModel):
    """Register (or deregister) a managed cluster as a generic Entitle **Kubernetes**
    integration (EKS/AKS/GKE). In-Cluster when the agent is installed; External
    (minted ServiceAccount + token) otherwise."""
    action: str = "register"                  # register | deregister


class EntraGroupRequest(BaseModel):
    """Bind an Entra (AAD) group to a ClusterRole on a cluster for real-identity JIT.
    Both optional — fall back to config (entra_rbac_group_id / entra_rbac_group_role)."""
    group_id: Optional[str] = None            # Entra group Object ID (else entra_rbac_group_id)
    role: Optional[str] = None                # ClusterRole to bind (else entra_rbac_group_role, default cluster-admin)


class ClusterInfo(BaseModel):
    id: str
    cloud: str
    name: str
    status: str
    source: Optional[str] = None              # registered | provisioned (§1.1a)
    region: Optional[str] = None
    api_server: Optional[str] = None
    mgmt_kind: Optional[str] = None
    mgmt_endpoint: Optional[str] = None
    pra_jump_id: Optional[str] = None
    jump_group: Optional[str] = None
    jumpoint_name: Optional[str] = None
    pra_credential_ref: Optional[str] = None
    secrets_delivery_kind: Optional[str] = None
    entitle_agent_installed: bool = False
    api_tunnel_jump: bool = False             # true when a direct API TCP tunnel jump exists (config-tracked)
    entra_group_bound: bool = False           # true when an Entra group is bound to a ClusterRole (config-tracked)
    entra_federation_enabled: bool = False    # true when the cluster trusts Entra as an OIDC IdP (AKS native; EKS via action)
    created_by: Optional[str] = None
    created_at: str
