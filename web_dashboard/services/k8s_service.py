"""Kubernetes cluster lifecycle — Phase 1 (register + record).

Phase 1 of docs/saas-kubernetes-management-plan.md. A **thin lifecycle**
service over the ``k8s_clusters`` table: register a reachable cluster from its
kubeconfig (the dev-testable path — the cluster is provisioned out-of-band),
store the kubeconfig as a **secrets-backend reference** (never in the row), and
list/get/delete. No kubectl wrapping and no live cluster calls — the
management-plane launch (Phase 2; Portainer-k8s first, then Rancher), brokered
access + native PRA ``tunnel_type=k8s`` jump (Phase 3), and in-cluster Password
Safe delivery (Phase 4) build on this.

Cloud-provisioned clusters (a ``terraform/k8s_cluster/*`` module) are a later
sub-phase; ``create_cluster`` raises until then — register an existing cluster.
"""
import asyncio
import base64
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from urllib.parse import urlparse

import yaml
from typing import Optional

from sqlalchemy.orm import Session

from ..database import Job, K8sCluster

logger = logging.getLogger(__name__)

VALID_CLOUDS = ("aws", "azure", "gcp", "local")
VALID_MGMT_KINDS = ("portainer", "rancher", "argocd", "headlamp")

# config_service key that holds a cluster's kubeconfig; the row stores this
# string as kubeconfig_ref and config_service.get() resolves it.
_KUBECONFIG_KEY = "k8s_kubeconfig_{cluster_id}"

# Phase 2 — management-plane launch. The apply runs in a transient kubectl
# container (mirrors ansible_local_service's local-docker runner) so the app
# process never holds cluster-admin. Phase 2's first plane is Portainer-k8s:
# deploy the Portainer Agent into the cluster, then register it in the Portainer
# server the dashboard already brokers (portainer_service.add_agent_endpoint).
_PORTAINER_AGENT_MANIFEST_URL = "https://downloads.portainer.io/ce-lts/portainer-agent-k8s-nodeport.yaml"
_PORTAINER_AGENT_NODEPORT = 30778

# Phase 4 (Feature D) — in-cluster Password Safe secret delivery. The dashboard
# installs BeyondTrust's own integration rather than proxying secrets itself.
# v1 ships the External Secrets Operator path: ESO (Helm) + a BeyondTrust
# ClusterSecretStore that syncs Password Safe → native K8s Secrets. Helm runs in
# a transient container (same throwaway-runner pattern as the kubectl apply).
_ESO_HELM_REPO_NAME = "external-secrets"
_ESO_HELM_REPO_URL = "https://charts.external-secrets.io"
_ESO_HELM_CHART = "external-secrets/external-secrets"


class K8sError(Exception):
    pass


def _parse_api_server(kubeconfig: str) -> str:
    """The current-context cluster's API server URL from a kubeconfig (or "")."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        current = cfg.get("current-context")
        clusters = {c["name"]: c.get("cluster", {}) for c in cfg.get("clusters", [])}
        if current:
            for ctx in cfg.get("contexts", []):
                if ctx.get("name") == current:
                    cl = ctx.get("context", {}).get("cluster")
                    return clusters.get(cl, {}).get("server", "") or ""
        if cfg.get("clusters"):
            return cfg["clusters"][0].get("cluster", {}).get("server", "") or ""
    except Exception as exc:
        logger.warning("kubeconfig parse failed: %s", exc)
    return ""


def _serialize(r: K8sCluster) -> dict:
    return {
        "id":                    r.id,
        "cloud":                 r.cloud,
        "name":                  r.name,
        "status":                r.status,
        "source":                r.source,
        "region":                r.region,
        "api_server":            r.api_server,
        "mgmt_kind":             r.mgmt_kind,
        "mgmt_endpoint":         r.mgmt_endpoint,
        "pra_jump_id":           r.pra_jump_id,
        "jump_group":            r.jump_group,
        "jumpoint_name":         r.jumpoint_name,
        "pra_credential_ref":    r.pra_credential_ref,
        "secrets_delivery_kind": r.secrets_delivery_kind,
        "created_by":            r.created_by,
        "created_at":            r.created_at.isoformat() if r.created_at else "",
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_clusters(db: Session) -> list[dict]:
    rows = db.query(K8sCluster).order_by(K8sCluster.created_at.desc()).all()
    return [_serialize(r) for r in rows]


def get_cluster(db: Session, cluster_id: str) -> dict:
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    return _serialize(row)


def resolve_kubeconfig(db: Session, cluster_id: str) -> str:
    """The cluster's kubeconfig, resolved from its reference. For Phase 2+
    (apply a management plane); kept out of the row + list/get responses."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not row.kubeconfig_ref:
        raise K8sError(f"cluster {cluster_id} has no stored kubeconfig")
    from . import config_service
    kubeconfig = config_service.get(row.kubeconfig_ref)
    if not kubeconfig:
        raise K8sError(f"kubeconfig for {cluster_id} could not be resolved")
    return kubeconfig


# ── Writes ────────────────────────────────────────────────────────────────────

def register_cluster(db: Session, *, name: str, cloud: str, kubeconfig: str,
                     created_by: str, mgmt_kind: str = None) -> dict:
    """Record an existing reachable cluster. Parses the API server from the
    kubeconfig, stores the kubeconfig as a secrets-backend reference, and
    inserts a ``registered`` row. The kubeconfig is never written to the row."""
    name = (name or "").strip()
    if not name:
        raise K8sError("cluster name is required")
    if cloud not in VALID_CLOUDS:
        raise K8sError(f"unknown cloud {cloud!r} (expected one of {', '.join(VALID_CLOUDS)})")
    if mgmt_kind and mgmt_kind not in VALID_MGMT_KINDS:
        raise K8sError(f"unknown mgmt_kind {mgmt_kind!r} (expected one of {', '.join(VALID_MGMT_KINDS)})")
    if not (kubeconfig or "").strip():
        raise K8sError("kubeconfig is required")
    if db.query(K8sCluster).filter(K8sCluster.name == name).first():
        raise K8sError(f"a cluster named {name!r} is already registered")

    api_server = _parse_api_server(kubeconfig)
    if not api_server:
        raise K8sError("could not parse an API server from the kubeconfig")

    cluster_id = str(uuid.uuid4())
    ref = _KUBECONFIG_KEY.format(cluster_id=cluster_id)
    from . import config_service
    config_service.set(ref, kubeconfig)

    row = K8sCluster(
        id=cluster_id, cloud=cloud, name=name, status="registered",
        api_server=api_server, kubeconfig_ref=ref, mgmt_kind=mgmt_kind,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    logger.info("Registered k8s cluster %s (%s, api=%s)", name, cloud, api_server)
    return _serialize(row)


# ── §1.1a: cluster provisioning (Terraform) ───────────────────────────────────
# A per-cloud terraform/k8s_cluster/<dir> module driven by the terraform.py
# subprocess wrapper with a per-job_id deploy dir — the exact shape
# cloud_database_service proved. AWS EKS first; GCP/Azure fan out later. The
# generated kubeconfig is stored via the same secrets-backend path
# register_cluster uses, and the row flips to ``registered`` so every downstream
# flow (manage / broker / secrets / delete) applies unchanged.

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CLUSTER_TEMPLATE_DIRS = {
    "aws": os.path.join(_REPO_ROOT, "terraform", "k8s_cluster", "aws_eks"),
    "azure": os.path.join(_REPO_ROOT, "terraform", "k8s_cluster", "azure_aks"),
    "gcp": os.path.join(_REPO_ROOT, "terraform", "k8s_cluster", "gcp_gke"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")
_PROVISION_IMPLEMENTED = ("aws", "azure", "gcp")


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def _cluster_template_dir(cloud: str) -> str:
    return _CLUSTER_TEMPLATE_DIRS[cloud]


def _eks_name(name: str) -> str:
    """A valid EKS cluster name from the dashboard cluster name: keep alnum + '-',
    ensure it starts alphanumeric, cap length."""
    slug = "".join(c if (c.isalnum() or c == "-") else "-" for c in (name or "")).strip("-")
    if not slug or not slug[0].isalnum():
        slug = "k8s-" + slug
    return slug[:100]


def create_cluster(db: Session, *, cloud: str, name: str, region: str,
                   created_by: str, **opts) -> dict:
    """Provision a new cluster with Terraform (the ``terraform/k8s_cluster/<cloud>``
    module), then store its kubeconfig and flip to ``registered`` (§1.1a).

    Synchronous record-keeping only — validate, insert a ``provisioning`` row + a
    ``k8s_provision`` Job, and return the Terraform variables the apply will use.
    Does **not** run Terraform — the API schedules :func:`run_provision_apply` as a
    background task. Mirrors ``cloud_database_service.provision``."""
    name = (name or "").strip()
    if not name:
        raise K8sError("cluster name is required")
    if cloud not in VALID_CLOUDS:
        raise K8sError(f"unknown cloud {cloud!r} (expected one of {', '.join(VALID_CLOUDS)})")
    if cloud not in _PROVISION_IMPLEMENTED:
        raise NotImplementedError(
            f"cluster provisioning for {cloud!r} is not wired yet — §1.1a implements aws (EKS) first"
        )
    if not (region or "").strip():
        raise K8sError("region is required")
    if db.query(K8sCluster).filter(K8sCluster.name == name).first():
        raise K8sError(f"a cluster named {name!r} is already registered")

    cluster_id = str(uuid.uuid4())
    row = K8sCluster(
        id=cluster_id, cloud=cloud, name=name, status="provisioning",
        source="provisioned", region=region, created_by=created_by,
    )
    db.add(row)
    db.commit()

    from . import job_service
    job = job_service.create_job(
        db, job_type="k8s_provision", created_by=created_by,
        metadata={"cluster_id": cluster_id, "cloud": cloud, "name": name, "region": region},
    )
    row.deploy_job_id = job.id
    db.commit()

    tf_variables = _build_cluster_tf_variables(
        cloud=cloud, cluster_id=cluster_id, name=name, region=region, opts=opts)
    logger.info("k8s provision record cluster_id=%s cloud=%s name=%s job_id=%s",
                cluster_id, cloud, name, job.id)
    return {"ok": True, "cluster_id": cluster_id, "job_id": job.id, "tf_variables": tf_variables}


def _gke_name(name: str) -> str:
    """A valid GKE cluster name: lowercase alnum + '-', start alphanumeric, no
    trailing '-', cap 40 (GKE's limit)."""
    return _eks_name(name).lower()[:40].rstrip("-") or "k8s-cluster"


def _cfg_list(key: str) -> list:
    """A comma-separated config value as a trimmed list (empty when unset)."""
    return [s.strip() for s in _cfg(key).split(",") if s.strip()]


# ── Provision-form pickers (§1.1a): curated per-cloud static lists ─────────────
# Strict-select sources for the provision modal (mirrors aws_service's curated
# DB_INSTANCE_CLASSES — there's no live EKS-version/instance-type discovery API).
# provision_options() always merges the configured value in + first, so a strict
# select can't exclude it.
K8S_VERSIONS = {
    "aws":   ["1.36", "1.35", "1.34", "1.33"],
    "azure": ["1.36", "1.35", "1.34", "1.33"],
    "gcp":   ["1.36", "1.35", "1.34", "1.33"],
}
K8S_NODE_TYPES = {
    "aws":   ["t3.small", "t3.medium", "t3.large", "t3.xlarge",
              "m5.large", "m5.xlarge", "c5.large", "c5.xlarge"],
    "azure": ["Standard_B2s", "Standard_B2ms", "Standard_D2s_v3",
              "Standard_D4s_v3", "Standard_DS2_v2"],
    "gcp":   ["e2-small", "e2-medium", "e2-standard-2", "e2-standard-4",
              "n2-standard-2", "n2-standard-4"],
}
K8S_REGIONS = {
    "aws":   ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1",
              "eu-central-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1"],
    "azure": ["eastus", "eastus2", "westus2", "westus3", "centralus",
              "northeurope", "westeurope", "uksouth", "australiaeast"],
    "gcp":   ["us-central1", "us-east1", "us-west1", "europe-west1",
              "europe-west4", "asia-southeast1", "asia-northeast1", "australia-southeast1"],
}


def _with_configured_first(values: list, configured: str) -> list:
    """Return ``values`` with ``configured`` guaranteed present and first
    (order-preserving, de-duplicated). Empty ``configured`` → ``values`` unchanged.
    Keeps a strict ``<select>`` from ever excluding the configured/sandbox value."""
    seen, out = set(), []
    for v in [configured, *values]:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _build_cluster_tf_variables(*, cloud: str, cluster_id: str, name: str,
                                region: str, opts: dict) -> dict:
    """The Terraform ``-var`` set for the cluster module (aws EKS / azure AKS /
    gcp GKE).

    AWS subnet ids default to the two private k8s subnets the sandbox emits
    (``aws_k8s_subnet_a_id`` / ``aws_k8s_subnet_b_id``). AKS/GKE create their own
    network (self-contained + egress) so they take none. k8s version + node size
    fall back to config then the module defaults; ``node_instance_type`` maps to
    the per-cloud node-size var (EKS instance type / AKS vm_size / GKE machine
    type)."""
    _tags = {"managed-by": "vm-dashboard", "k8s-cluster-id": cluster_id}
    if cloud == "aws":
        subnets = opts.get("subnet_ids") or [
            s for s in (_cfg("aws_k8s_subnet_a_id"), _cfg("aws_k8s_subnet_b_id")) if s
        ]
        tf = {
            "region": region,
            "cluster_name": _eks_name(f"k8s-{name}"),
            "subnet_ids": subnets,
            "tags": {"managed-by": "vm-dashboard", "k8s-cluster-id": cluster_id},
        }
        version = opts.get("k8s_version") or _cfg("aws_eks_k8s_version")
        if version:
            tf["k8s_version"] = version
        node_type = opts.get("node_instance_type") or _cfg("aws_eks_node_instance_type")
        if node_type:
            tf["node_instance_type"] = node_type
        if opts.get("node_count"):
            tf["node_desired"] = int(opts["node_count"])
        return tf

    if cloud == "azure":
        # Deploy into the dashboard's configured resource group (region-resolved,
        # exactly like VMs / containers / VDI desktops) instead of letting the
        # module create a dedicated '<cluster>-rg'. The dashboard's service
        # principal is typically scoped to that existing RG, not the whole
        # subscription, so creating a fresh RG fails with
        #   403 AuthorizationFailed on Microsoft.Resources/.../resourcegroups/read.
        # Passing resource_group_name flips the module's RG count to 0 (uses the
        # existing RG); the cluster's VNet/subnet are still self-contained inside it.
        from .region_config import resolve_azure_region
        rg = (resolve_azure_region(region) or {}).get("resource_group") or "vm-cli-rg"
        tf = {
            "location": region,
            "cluster_name": _eks_name(f"k8s-{name}"),
            "resource_group_name": rg,
            "tags": _tags,
        }
        version = opts.get("k8s_version") or _cfg("azure_aks_k8s_version")
        if version:
            tf["k8s_version"] = version
        vm_size = opts.get("node_instance_type") or _cfg("azure_aks_node_vm_size")
        if vm_size:
            tf["vm_size"] = vm_size
        if opts.get("node_count"):
            tf["node_count"] = int(opts["node_count"])
        cidrs = opts.get("authorized_cidrs") or _cfg_list("azure_aks_authorized_cidrs")
        if cidrs:
            tf["authorized_ip_ranges"] = cidrs
        return tf

    if cloud == "gcp":
        project = _cfg("gcp_project") or _cfg("gcp_project_id")
        if not project:
            raise K8sError("gcp_project is not configured — GKE provisioning needs a project id")
        tf = {
            "project": project,
            "region": region,
            "cluster_name": _gke_name(f"k8s-{name}"),
            "tags": _tags,
        }
        if opts.get("zone"):
            tf["zone"] = opts["zone"]
        version = opts.get("k8s_version") or _cfg("gcp_gke_k8s_version")
        if version:
            tf["k8s_version"] = version
        machine = opts.get("node_instance_type") or _cfg("gcp_gke_machine_type")
        if machine:
            tf["machine_type"] = machine
        if opts.get("node_count"):
            tf["node_count"] = int(opts["node_count"])
        cidrs = opts.get("authorized_cidrs") or _cfg_list("gcp_gke_authorized_cidrs")
        if cidrs:
            tf["authorized_cidrs"] = cidrs
        return tf

    raise NotImplementedError(f"{cloud} cluster Terraform variables not implemented")


async def provision_options(cloud: str, region: str = "") -> dict:
    """Assemble the provision-form pickers for one cloud (region-scoped). Curated
    static lists for regions / node sizes / k8s versions (the configured value is
    merged in + first); AWS additionally serves live VPC subnets for the EKS subnet
    override + the two configured sandbox subnet ids to pre-select. AKS/GKE create
    their own network → subnets / configured_subnet_ids empty. Raises K8sError on an
    unknown cloud; AWS subnet discovery errors propagate as aws_service.AWSError."""
    cloud = (cloud or "aws").strip().lower()
    if cloud not in _PROVISION_IMPLEMENTED:
        raise K8sError(f"unknown cloud {cloud!r} (expected one of {', '.join(_PROVISION_IMPLEMENTED)})")

    cfg_region = {"aws": "aws_region", "azure": "azure_location", "gcp": "gcp_region"}[cloud]
    cfg_node = {"aws": "aws_eks_node_instance_type", "azure": "azure_aks_node_vm_size",
                "gcp": "gcp_gke_machine_type"}[cloud]
    cfg_ver = {"aws": "aws_eks_k8s_version", "azure": "azure_aks_k8s_version",
               "gcp": "gcp_gke_k8s_version"}[cloud]

    configured_region = _cfg(cfg_region)
    region = (region or "").strip() or configured_region
    # configured region first, then the just-picked region, then the curated set.
    regions = _with_configured_first(
        _with_configured_first(K8S_REGIONS[cloud], region), configured_region)

    out = {
        "cloud": cloud,
        "region": region,
        "regions": regions,
        "node_instance_types": _with_configured_first(K8S_NODE_TYPES[cloud], _cfg(cfg_node)),
        "k8s_versions": _with_configured_first(K8S_VERSIONS[cloud], _cfg(cfg_ver)),
        "subnets": [],
        "configured_subnet_ids": [],
    }
    if cloud == "aws":
        from . import aws_service
        net = await aws_service.get_network_options(region)
        out["subnets"] = net.get("subnets", [])
        out["configured_subnet_ids"] = [
            s for s in (_cfg("aws_k8s_subnet_a_id"), _cfg("aws_k8s_subnet_b_id")) if s
        ]
    return out


def _assemble_eks_kubeconfig(*, cluster_name: str, endpoint: str, ca_b64: str,
                             region: str) -> str:
    """An exec-based kubeconfig for an EKS cluster: API server + inline CA + an
    ``aws eks get-token`` exec block. The CA is inline so the PRA tunnel's
    ``_parse_ca_cert`` works; auth is via the exec plugin. The transient
    kubectl/helm runner can't run the ``aws`` plugin, so ``_runner_kubeconfig``
    swaps in a server-minted bearer token (``aws_service.eks_get_token``) for
    runner use."""
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": cluster_name,
            "cluster": {"server": endpoint, "certificate-authority-data": ca_b64},
        }],
        "contexts": [{
            "name": cluster_name,
            "context": {"cluster": cluster_name, "user": cluster_name},
        }],
        "current-context": cluster_name,
        "users": [{
            "name": cluster_name,
            "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": "aws",
                "args": ["eks", "get-token", "--cluster-name", cluster_name,
                         "--region", region],
            }},
        }],
    }
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


def _exec_kubeconfig(*, cluster_name: str, endpoint: str, ca_b64: str,
                     command: str, args: list) -> str:
    """A kubeconfig whose user auths via an exec plugin. The exec block is only a
    marker for the runner — ``_runner_kubeconfig`` recognises ``command`` (aws /
    kubelogin / gke-gcloud-auth-plugin) and swaps it for a server-minted bearer
    token, so the plugin binary never has to exist in the throwaway container."""
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": cluster_name,
            "cluster": {"server": endpoint, "certificate-authority-data": ca_b64},
        }],
        "contexts": [{
            "name": cluster_name,
            "context": {"cluster": cluster_name, "user": cluster_name},
        }],
        "current-context": cluster_name,
        "users": [{
            "name": cluster_name,
            "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": command,
                "args": args,
            }},
        }],
    }
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


def _assemble_aks_kubeconfig(*, cluster_name: str, endpoint: str, ca_b64: str) -> str:
    """An exec (``kubelogin``) kubeconfig for an AKS cluster. The runner swaps the
    exec for a server-minted AAD token (``azure_service.aks_get_token``)."""
    from . import azure_service
    return _exec_kubeconfig(
        cluster_name=cluster_name, endpoint=endpoint, ca_b64=ca_b64,
        command="kubelogin",
        args=["get-token", "--server-id", azure_service.AKS_AAD_SERVER_APP_ID, "--login", "spn"])


def _assemble_gke_kubeconfig(*, cluster_name: str, endpoint: str, ca_b64: str) -> str:
    """An exec (``gke-gcloud-auth-plugin``) kubeconfig for a GKE cluster. The runner
    swaps the exec for a server-minted OAuth token (``gcp_service.gke_get_token``)."""
    return _exec_kubeconfig(
        cluster_name=cluster_name, endpoint=endpoint, ca_b64=ca_b64,
        command="gke-gcloud-auth-plugin", args=[])


def _assemble_cluster_kubeconfig(*, cloud: str, cluster_name: str, endpoint: str,
                                 ca_b64: str, region: str) -> str:
    """Pick the per-cloud kubeconfig assembler for a freshly provisioned cluster."""
    if cloud == "azure":
        return _assemble_aks_kubeconfig(cluster_name=cluster_name, endpoint=endpoint, ca_b64=ca_b64)
    if cloud == "gcp":
        return _assemble_gke_kubeconfig(cluster_name=cluster_name, endpoint=endpoint, ca_b64=ca_b64)
    return _assemble_eks_kubeconfig(
        cluster_name=cluster_name, endpoint=endpoint, ca_b64=ca_b64, region=region)


# Terraform line → (progress_pct, message) milestones for the job's progress bar.
# Needles are full ``resource: action`` phrases so a provision stream and a
# decommission stream never cross-match. pct is applied with max() so progress is
# monotonic; unmatched (plain) lines keep the current pct/msg.
_MILESTONES = [
    ("initializing the backend",                  10, "Initializing Terraform…"),
    ("terraform has been successfully initialized", 12, "Initialized; planning…"),
    ("plan:",                                     18, "Planning…"),
    # provision — the EKS control plane is the long pole (~10 min)
    ("aws_eks_cluster.this: creating",            35, "Creating the EKS control plane (~10 min)…"),
    ("aws_eks_cluster.this: still creating",      45, "Creating the EKS control plane (~10 min)…"),
    ("aws_eks_cluster.this: creation complete",   78, "Control plane ready; creating the node group…"),
    ("aws_eks_node_group.this: creating",         85, "Creating the node group…"),
    ("aws_eks_node_group.this: still creating",   88, "Creating the node group…"),
    ("aws_eks_node_group.this: creation complete", 95, "Node group ready; finalizing…"),
    # decommission
    ("aws_eks_node_group.this: destroying",       25, "Destroying the node group…"),
    ("aws_eks_node_group.this: still destroying", 35, "Destroying the node group…"),
    ("aws_eks_cluster.this: destroying",          45, "Destroying the EKS control plane…"),
    ("aws_eks_cluster.this: still destroying",    60, "Destroying the EKS control plane…"),
    ("aws_eks_cluster.this: destruction complete", 90, "Cleaning up IAM roles…"),
    # provision — Azure AKS (control plane is the long pole)
    ("azurerm_kubernetes_cluster.this: creating",            35, "Creating the AKS cluster (~5-10 min)…"),
    ("azurerm_kubernetes_cluster.this: still creating",      55, "Creating the AKS cluster (~5-10 min)…"),
    ("azurerm_kubernetes_cluster.this: creation complete",   90, "AKS cluster ready; finalizing…"),
    ("azurerm_kubernetes_cluster.this: destroying",          40, "Destroying the AKS cluster…"),
    ("azurerm_kubernetes_cluster.this: still destroying",    60, "Destroying the AKS cluster…"),
    ("azurerm_kubernetes_cluster.this: destruction complete", 88, "Cleaning up the resource group…"),
    # provision — GCP GKE (cluster, then node pool)
    ("google_container_cluster.this: creating",              30, "Creating the GKE cluster (~5-10 min)…"),
    ("google_container_cluster.this: still creating",        45, "Creating the GKE cluster (~5-10 min)…"),
    ("google_container_cluster.this: creation complete",     75, "Cluster ready; creating the node pool…"),
    ("google_container_node_pool.this: creating",            82, "Creating the node pool…"),
    ("google_container_node_pool.this: still creating",      88, "Creating the node pool…"),
    ("google_container_node_pool.this: creation complete",   95, "Node pool ready; finalizing…"),
    ("google_container_node_pool.this: destroying",          25, "Destroying the node pool…"),
    ("google_container_cluster.this: destroying",            45, "Destroying the GKE cluster…"),
    ("google_container_cluster.this: still destroying",      60, "Destroying the GKE cluster…"),
    ("google_container_cluster.this: destruction complete",  88, "Cleaning up the VPC + NAT…"),
]


def _tf_milestone(line: str, cur_pct: int, cur_msg: str) -> tuple:
    """Map a terraform line to (pct, message); plain lines keep the current pair."""
    low = line.lower()
    for needle, pct, msg in _MILESTONES:
        if needle in low:
            return max(cur_pct, pct), msg
    return cur_pct, cur_msg


async def run_provision_apply(db: Session, *, cluster_id: str, job_id: str,
                              cloud: str, tf_variables: dict) -> None:
    """**§1.1a** background task: ``terraform apply`` the cluster module, assemble a
    kubeconfig from its outputs, store it as a secrets-backend reference (the same
    path :func:`register_cluster` uses), and flip the row to ``registered`` — after
    which the Phase 2-4 flows treat it like any registered cluster. Marks the row +
    job failed on apply error. Mirrors ``cloud_database_service.run_provision_apply``."""
    from . import config_service, job_service, terraform, terraform_provider_env
    from ..api.websocket import broadcast_progress
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if not row:
        logger.warning("k8s provision: row %s vanished", cluster_id)
        return
    job_service.set_running(db, job_id)
    _p = {"pct": 5, "msg": "Starting provision…"}

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, _p)  # stop terraform if the job was cancelled
        _p["pct"], _p["msg"] = _tf_milestone(line, _p["pct"], _p["msg"])
        await broadcast_progress(job_id, _p["pct"], _p["msg"], log_line=line)

    try:
        outputs = await terraform.apply(
            _deploy_dir(job_id), tf_variables,
            template_dir=_cluster_template_dir(cloud),
            env=terraform_provider_env.provider_env(cloud),
            on_line=on_line,
        )
        endpoint = str(outputs.get("endpoint") or "")
        ca_b64 = str(outputs.get("ca_certificate") or "")
        cluster_out_name = str(outputs.get("cluster_name") or tf_variables.get("cluster_name") or row.name)
        if not (endpoint and ca_b64):
            raise K8sError("cluster apply did not return endpoint + ca_certificate outputs")

        kubeconfig = _assemble_cluster_kubeconfig(
            cloud=cloud, cluster_name=cluster_out_name, endpoint=endpoint,
            ca_b64=ca_b64, region=row.region or "")
        ref = _KUBECONFIG_KEY.format(cluster_id=cluster_id)
        config_service.set(ref, kubeconfig)
        row.kubeconfig_ref = ref
        row.api_server = endpoint
        row.status = "registered"
        db.commit()
        job_service.set_completed(db, job_id)
        logger.info("k8s provision complete cluster_id=%s cluster=%s endpoint=%s",
                    cluster_id, cluster_out_name, endpoint)
    except Exception as exc:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s provision failed cluster_id=%s: %s", cluster_id, exc)


def start_decommission(db: Session, cluster_id: str, created_by: str = "") -> dict:
    """Record intent to decommission a **provisioned** cluster + schedule teardown:
    flip to ``decommissioning`` and create a ``k8s_decommission`` Job. The teardown
    (PRA tunnel → ``terraform destroy`` → drop the record) runs in
    :func:`run_decommission` as a background task — it's many minutes long and must
    not block the request. Mirrors ``cloud_database_service.start_decommission``."""
    from . import job_service
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if row.status == "decommissioning":
        existing = (db.query(Job)
                      .filter(Job.job_type == "k8s_decommission")
                      .order_by(Job.created_at.desc()).all())
        job = next((j for j in existing if (j.metadata_dict or {}).get("cluster_id") == cluster_id), None)
        if job:
            return {"ok": True, "cluster_id": cluster_id, "job_id": job.id}
    row.status = "decommissioning"
    db.commit()
    job = job_service.create_job(
        db, job_type="k8s_decommission", created_by=created_by or row.created_by or "system",
        metadata={"cluster_id": cluster_id, "cloud": row.cloud, "name": row.name},
    )
    return {"ok": True, "cluster_id": cluster_id, "job_id": job.id}


async def run_decommission(db: Session, *, cluster_id: str, job_id: str) -> None:
    """Background teardown for a **provisioned** cluster: best-effort remove the PRA
    tunnel, then ``terraform destroy`` the cluster, then drop the record + stored
    kubeconfig. Errors are ACCUMULATED → the row/job end ``failed`` (an orphaned
    cluster stays visible) rather than a false ``decommissioned``. Mirrors
    ``cloud_database_service.run_decommission``."""
    from . import config_service, job_service, terraform, terraform_provider_env
    from ..api.websocket import broadcast_progress
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"cluster {cluster_id} not found")
        return
    job_service.set_running(db, job_id)
    _p = {"pct": 40, "msg": "Destroying the cluster…"}

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, _p)  # stop terraform if the job was cancelled
        _p["pct"], _p["msg"] = _tf_milestone(line, _p["pct"], _p["msg"])
        await broadcast_progress(job_id, _p["pct"], _p["msg"], log_line=line)

    errors: list = []

    # 1. PRA tunnel (best-effort; clears pra_jump_id / pra_tunnel_state on the row).
    job_service.update_progress(db, job_id, 15, "Removing PRA tunnel…")
    try:
        await deregister_pra_tunnel(db, cluster_id)
    except Exception as exc:
        errors.append(f"PRA tunnel removal: {exc}")
        logger.warning("k8s decommission: tunnel removal for %s failed: %s", cluster_id, exc)

    # 2. terraform destroy (the long step). State lives in the active storage
    #    backend, so destroy recovers a deploy dir lost to a container recreate —
    #    pass template_dir so the module is rebuilt from it + the remote state.
    job_service.update_progress(db, job_id, 40, "Destroying the cluster…")
    if row.deploy_job_id:
        try:
            # terraform destroy evaluates the module config, so it needs the same
            # -var set apply used (else "No value for required variable"). The values
            # don't change what's destroyed (resources come from state), but the
            # provider's region must be correct — reconstruct from the row.
            destroy_vars = _build_cluster_tf_variables(
                cloud=row.cloud, cluster_id=row.id, name=row.name,
                region=row.region or "", opts={})
            await terraform.destroy(
                _deploy_dir(row.deploy_job_id),
                env=terraform_provider_env.provider_env(row.cloud),
                template_dir=_cluster_template_dir(row.cloud),
                variables=destroy_vars,
                on_line=on_line,
            )
            logger.info("k8s cluster destroyed cluster_id=%s cloud=%s", cluster_id, row.cloud)
        except Exception as exc:
            errors.append(f"cluster destroy: {exc}")
            logger.warning("k8s destroy for %s failed: %s", cluster_id, exc)
    else:
        errors.append("no provisioning job recorded — the cluster may need manual "
                      "teardown in the cloud console")

    if errors:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, "; ".join(errors))
        logger.error("k8s decommission cluster_id=%s ended with errors: %s", cluster_id, errors)
        return

    # 3. Drop the record + stored kubeconfig (same as the register-delete path).
    name = row.name
    if row.kubeconfig_ref:
        try:
            config_service.set(row.kubeconfig_ref, "")
        except Exception as exc:
            logger.warning("k8s decommission: clearing kubeconfig for %s failed: %s", cluster_id, exc)
    db.delete(row)
    db.commit()
    job_service.set_completed(db, job_id, {"cluster_id": cluster_id, "deregistered": name})
    logger.info("k8s decommissioned cluster_id=%s", cluster_id)


def delete_cluster(db: Session, cluster_id: str) -> dict:
    """Drop the cluster record and its stored kubeconfig. Phase 1 deregisters;
    it does not tear down a cloud-provisioned cluster (no Terraform yet)."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if row.kubeconfig_ref:
        from . import config_service
        try:
            config_service.set(row.kubeconfig_ref, "")
        except Exception as exc:
            logger.warning("clearing kubeconfig for %s failed: %s", cluster_id, exc)
    name = row.name
    db.delete(row)
    db.commit()
    logger.info("Deregistered k8s cluster %s", name)
    return {"ok": True, "deregistered": name}


# ── Phase 2: management-plane launch ───────────────────────────────────────────

def _api_host(api_server: str) -> str:
    """The hostname/IP from a cluster API URL (where the agent NodePort is
    reachable). Best-effort — the live test confirms the agent's actual
    reachable address for the operator's network."""
    try:
        return urlparse(api_server).hostname or ""
    except Exception:
        return ""


def _run_sync(cmd: list, stdin_data: Optional[str] = None, env: Optional[dict] = None) -> str:
    """Run a command, returning stdout; raise K8sError with stderr on failure.
    asyncio's subprocess support is unreliable under uvicorn's SelectorEventLoop
    on Windows, so callers wrap this in asyncio.to_thread (same as the rest of
    the codebase). ``stdin_data`` is streamed to the process stdin (used to pipe
    secret-bearing manifests to ``kubectl apply -f -`` without touching disk)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, input=stdin_data, env=env)
    if proc.returncode != 0:
        raise K8sError(f"command failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _runner_kubeconfig(kubeconfig: str) -> str:
    """Prepare a kubeconfig for a transient kubectl/helm runner. A managed cluster's
    kubeconfig authenticates with a cloud **exec** block — ``aws eks get-token``
    (EKS), ``kubelogin get-token`` (AAD-integrated AKS), or ``gke-gcloud-auth-plugin``
    (GKE) — but the throwaway container has none of those CLIs or cloud creds. So for
    each, mint a short-lived bearer token server-side (``aws_service.eks_get_token`` /
    ``azure_service.aks_get_token`` / ``gcp_service.gke_get_token``) and swap the exec
    block for a static ``token``. Any other kubeconfig (registered clusters using a
    raw token / client-cert / AKS ``--admin``) is returned **unchanged**. Best-effort:
    on any parse/mint error, return the original so working paths are unaffected."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        users = cfg.get("users") or []
        # The current-context's user (fall back to the only/first user).
        user_name = None
        current = cfg.get("current-context")
        if current:
            for ctx in cfg.get("contexts", []):
                if ctx.get("name") == current:
                    user_name = ctx.get("context", {}).get("user")
                    break
        entry = next((u for u in users if u.get("name") == user_name), None) or (users[0] if users else None)
        if not entry:
            return kubeconfig
        exec_blk = (entry.get("user") or {}).get("exec") or {}
        command = exec_blk.get("command") or ""
        args = exec_blk.get("args") or []

        def _arg(flag: str) -> str:
            return args[args.index(flag) + 1] if (flag in args and args.index(flag) + 1 < len(args)) else ""

        if command == "aws" and "get-token" in args:
            cluster_name = _arg("--cluster-name")
            if not cluster_name:
                return kubeconfig
            from . import aws_service
            token = aws_service.eks_get_token(cluster_name, _arg("--region"))
        elif command in ("kubelogin", "kubelogin.exe"):
            from . import azure_service
            token = azure_service.aks_get_token(_arg("--server-id") or azure_service.AKS_AAD_SERVER_APP_ID)
        elif command in ("gke-gcloud-auth-plugin", "gcloud"):
            from . import gcp_service
            token = gcp_service.gke_get_token()
        else:
            return kubeconfig  # not a cloud exec-auth kubeconfig we mint for — leave as-is

        entry["user"] = {"token": token}
        return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)
    except Exception as exc:
        logger.warning("runner kubeconfig token-prep failed (using exec kubeconfig as-is): %s", exc)
        return kubeconfig


def _write_kubeconfig(kubeconfig: str) -> str:
    """Token-prep the kubeconfig (swap the cloud exec block for a static bearer
    token -- see ``_runner_kubeconfig``) and write it to a fresh private tmpdir;
    return the dir. ``kubectl`` / ``helm`` are baked into the image and run
    **in-process** (subprocess), NOT as sibling containers over a Docker socket, so
    this file is read by the same process -- no docker.sock, no shared volume, no
    cross-container file perms. Dropping that dependency is what lets the dashboard
    run on managed/serverless runtimes (ACI / Cloud Run / ECS-Fargate). Caller
    cleans up the dir with shutil.rmtree."""
    tmpdir = tempfile.mkdtemp(prefix="k8s_")
    with open(os.path.join(tmpdir, "kubeconfig"), "w") as fh:
        fh.write(_runner_kubeconfig(kubeconfig))
    return tmpdir


def _helm_env(tmpdir: str) -> dict:
    """Environment for an in-process ``helm`` run: KUBECONFIG plus helm's
    cache/config/data homes pointed at the per-op tmpdir, since the runtime user's
    ``$HOME`` may be read-only on a managed runtime."""
    env = dict(os.environ)
    env["KUBECONFIG"] = os.path.join(tmpdir, "kubeconfig")
    env["HELM_CACHE_HOME"] = os.path.join(tmpdir, "cache")
    env["HELM_CONFIG_HOME"] = os.path.join(tmpdir, "config")
    env["HELM_DATA_HOME"] = os.path.join(tmpdir, "data")
    return env


async def _apply_manifest_via_runner(kubeconfig: str, manifest_ref: str, target_cloud: str = "") -> str:
    """Apply a manifest with ``kubectl``. ``manifest_ref`` is a URL
    (``kubectl apply -f <url>``) or inline YAML; the latter is streamed to
    ``kubectl apply -f -`` over stdin so secret-bearing manifests never touch disk.

    Default (``k8s_runner=local``): the baked-in ``kubectl`` runs in-process
    (subprocess -- no Docker socket, so it works on managed/serverless runtimes).
    Otherwise the equivalent command runs as a one-shot cloud task with clean
    egress to the cluster API (see ``k8s_runner_service``)."""
    from . import k8s_runner_service
    if k8s_runner_service.mode(target_cloud) == "local":
        tmpdir = _write_kubeconfig(kubeconfig)
        try:
            kpath = os.path.join(tmpdir, "kubeconfig")
            is_url = manifest_ref.startswith(("http://", "https://"))
            if is_url:
                cmd = ["kubectl", "--kubeconfig", kpath, "apply", "-f", manifest_ref]
                stdin_data = None
            else:
                cmd = ["kubectl", "--kubeconfig", kpath, "apply", "-f", "-"]
                stdin_data = manifest_ref
            logger.info("k8s apply: source=%s", "url" if is_url else "inline")
            return await asyncio.to_thread(_run_sync, cmd, stdin_data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    is_url = manifest_ref.startswith(("http://", "https://"))
    if is_url:
        command = f"kubectl apply -f {shlex.quote(manifest_ref)}"
        stdin_text = None
    else:
        command = "kubectl apply -f -"
        stdin_text = manifest_ref
    logger.info("k8s apply (cloud runner): source=%s", "url" if is_url else "inline")
    return await k8s_runner_service.run(
        kubeconfig=_runner_kubeconfig(kubeconfig), command=command,
        target_cloud=target_cloud, stdin_text=stdin_text, job_id="")


async def _delete_manifest_via_runner(kubeconfig: str, manifest: str, target_cloud: str = "") -> str:
    """``kubectl delete -f -`` an inline manifest over stdin (best-effort teardown).

    Local (``k8s_runner=local``) runs the baked-in ``kubectl`` in-process;
    otherwise it runs as a one-shot cloud task (see ``k8s_runner_service``)."""
    from . import k8s_runner_service
    if k8s_runner_service.mode(target_cloud) == "local":
        tmpdir = _write_kubeconfig(kubeconfig)
        try:
            kpath = os.path.join(tmpdir, "kubeconfig")
            cmd = ["kubectl", "--kubeconfig", kpath, "delete", "--ignore-not-found", "-f", "-"]
            return await asyncio.to_thread(_run_sync, cmd, manifest)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return await k8s_runner_service.run(
        kubeconfig=_runner_kubeconfig(kubeconfig),
        command="kubectl delete --ignore-not-found -f -",
        target_cloud=target_cloud, stdin_text=manifest, job_id="")


async def _helm_via_runner(kubeconfig: str, helm_args: list, add_eso_repo: bool = True,
                           values_stdin: Optional[str] = None, target_cloud: str = "") -> str:
    """Run ``helm`` against the cluster. ``values_stdin`` is streamed to helm's
    stdin -- pass ``-f -`` in ``helm_args`` so secret values ride stdin instead of
    the process args.

    Local (``k8s_runner=local``) runs the baked-in ``helm`` in-process (no Docker
    socket); otherwise the equivalent ``helm`` command runs as a one-shot cloud
    task with clean egress to the cluster API (see ``k8s_runner_service``)."""
    from . import k8s_runner_service
    if k8s_runner_service.mode(target_cloud) == "local":
        tmpdir = _write_kubeconfig(kubeconfig)
        try:
            env = _helm_env(tmpdir)
            if add_eso_repo:
                await asyncio.to_thread(
                    _run_sync, ["helm", "repo", "add", _ESO_HELM_REPO_NAME, _ESO_HELM_REPO_URL], None, env)
                await asyncio.to_thread(_run_sync, ["helm", "repo", "update"], None, env)
            # NB: don't log helm_args — they can carry --set secrets
            # (operator-supplied entitle_agent_helm_extra_set, etc.).
            logger.info("k8s helm: local in-process (%d args)", len(helm_args))
            return await asyncio.to_thread(_run_sync, ["helm", *helm_args], values_stdin, env)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # KUBECONFIG is exported by the runner shell, so no --kubeconfig is needed.
    prefix = ""
    if add_eso_repo:
        prefix = f"helm repo add {_ESO_HELM_REPO_NAME} {_ESO_HELM_REPO_URL} && helm repo update && "
    command = prefix + "helm " + " ".join(shlex.quote(a) for a in helm_args)
    logger.info("k8s helm: cloud runner (%d args)", len(helm_args))
    return await k8s_runner_service.run(
        kubeconfig=_runner_kubeconfig(kubeconfig), command=command,
        target_cloud=target_cloud, stdin_text=values_stdin, job_id="")


def _yaml_quote(value: str) -> str:
    """Double-quoted YAML scalar, safe for arbitrary strings."""
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _nested_from_dotted(dotted_key: str, value: str) -> dict:
    """Turn ``a.b.c`` + value into ``{"a": {"b": {"c": value}}}`` so a secret can
    ride a Helm ``-f -`` (stdin) values doc instead of a ``--set`` argument."""
    parts = dotted_key.split(".")
    root: dict = {}
    node = root
    for p in parts[:-1]:
        node[p] = {}
        node = node[p]
    node[parts[-1]] = value
    return root


def _eso_credentials_secret_manifest(namespace: str, secret_name: str,
                                     client_id: str, client_secret: str) -> str:
    """Namespace + the K8s Secret the ESO BeyondTrust ClusterSecretStore reads its
    Password Safe OAuth client id/secret from (keys ClientId / ClientSecret)."""
    return (
        "apiVersion: v1\n"
        "kind: Namespace\n"
        "metadata:\n"
        f"  name: {namespace}\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {secret_name}\n"
        f"  namespace: {namespace}\n"
        "type: Opaque\n"
        "stringData:\n"
        f"  ClientId: {_yaml_quote(client_id)}\n"
        f"  ClientSecret: {_yaml_quote(client_secret)}\n"
    )


def _eso_clustersecretstore_manifest(name: str, api_url: str, retrieval_type: str,
                                     secret_namespace: str, secret_name: str,
                                     bt_api_version: str = "3.1") -> str:
    """A BeyondTrust ClusterSecretStore (external-secrets.io/v1) that syncs Password
    Safe → K8s Secrets. Schema confirmed against the ESO BeyondTrust provider docs;
    OAuth client id/secret come from the ``secret_name`` Secret (ClusterSecretStore
    is cluster-scoped, so the secretRef carries an explicit namespace)."""
    return (
        "apiVersion: external-secrets.io/v1\n"
        "kind: ClusterSecretStore\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  provider:\n"
        "    beyondtrust:\n"
        "      server:\n"
        f"        apiUrl: {_yaml_quote(api_url)}\n"
        f"        retrievalType: {retrieval_type}\n"
        "        verifyCA: true\n"
        "        clientTimeOutSeconds: 45\n"
        f"        apiVersion: {_yaml_quote(bt_api_version)}\n"
        "      auth:\n"
        "        clientId:\n"
        "          secretRef:\n"
        f"            name: {secret_name}\n"
        f"            namespace: {secret_namespace}\n"
        "            key: ClientId\n"
        "        clientSecret:\n"
        "          secretRef:\n"
        f"            name: {secret_name}\n"
        f"            namespace: {secret_namespace}\n"
        "            key: ClientSecret\n"
    )


def _eso_bt_api_url() -> str:
    """The BeyondTrust public API URL for the ESO provider. Explicit override
    (eso_bt_api_url) wins; otherwise derive from the Password Safe URL."""
    override = _cfg("eso_bt_api_url")
    if override:
        return override
    base = (_cfg("pscli_api_url") or "").rstrip("/")
    return f"{base}/BeyondTrust/api/public/v3/" if base else ""


VALID_DELIVERY_KINDS = ("eso", "none")


async def setup_secret_delivery(cluster_id: str, kind: str) -> None:
    """**Phase 4 (Feature D)** — install BeyondTrust's in-cluster Password Safe
    secret delivery into a managed cluster. Background task. v1 supports the
    External Secrets Operator path:

      * ``kind=eso``  → Helm-install ESO, write the BeyondTrust OAuth credentials
        Secret, and apply a BeyondTrust ClusterSecretStore (Password Safe →
        native K8s Secrets). Records ``secrets_delivery_kind=eso``.
      * ``kind=none`` → remove the ClusterSecretStore + credentials and uninstall
        ESO (best-effort); clears ``secrets_delivery_kind``.

    The dashboard installs + configures only — ESO owns the sync; secrets are
    never proxied through the dashboard. (Secrets-Agent is a later kind.)"""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        kubeconfig = resolve_kubeconfig(db, cluster_id)
        namespace = _cfg("eso_namespace", "external-secrets")
        secret_name = _cfg("eso_bt_credentials_secret", "beyondtrust-credentials")
        css_name = _cfg("eso_bt_clustersecretstore", "beyondtrust-store")

        if kind == "none":
            try:
                await _delete_manifest_via_runner(kubeconfig, _eso_clustersecretstore_manifest(
                    css_name, _eso_bt_api_url() or "https://x/", _cfg("eso_bt_retrieval_type", "SECRET"),
                    namespace, secret_name), target_cloud=row.cloud)
                await _helm_via_runner(kubeconfig, ["uninstall", "external-secrets", "-n", namespace],
                                       add_eso_repo=False, target_cloud=row.cloud)
            except Exception as exc:
                logger.warning("ESO teardown for %s partially failed: %s", cluster_id, exc)
            row.secrets_delivery_kind = None
            db.commit()
            return

        # kind == "eso"
        from . import config_service
        client_id = config_service.get("pscli_client_id")
        client_secret = config_service.get("pscli_client_secret")
        api_url = _eso_bt_api_url()
        if not (client_id and client_secret and api_url):
            raise K8sError(
                "ESO secret delivery needs the Password Safe API URL + OAuth client "
                "(pscli_api_url, pscli_client_id, pscli_client_secret) configured"
            )
        row.secrets_delivery_kind = "installing"
        db.commit()
        try:
            ver = _cfg("eso_helm_version")
            helm_args = ["upgrade", "--install", "external-secrets", _ESO_HELM_CHART,
                         "--namespace", namespace, "--create-namespace", "--wait",
                         "--set", "installCRDs=true"]
            if ver:
                helm_args += ["--version", ver]
            await _helm_via_runner(kubeconfig, helm_args, target_cloud=row.cloud)
            await _apply_manifest_via_runner(kubeconfig, _eso_credentials_secret_manifest(
                namespace, secret_name, client_id, client_secret), target_cloud=row.cloud)
            await _apply_manifest_via_runner(kubeconfig, _eso_clustersecretstore_manifest(
                css_name, api_url, _cfg("eso_bt_retrieval_type", "SECRET"),
                namespace, secret_name, _cfg("eso_bt_api_version", "3.1")), target_cloud=row.cloud)
            row.secrets_delivery_kind = "eso"
            db.commit()
            logger.info("ESO + Password Safe secret delivery installed on cluster %s", row.name)
        except Exception as exc:
            row.secrets_delivery_kind = "failed"
            db.commit()
            logger.warning("ESO secret delivery install failed cluster=%s: %s", cluster_id, exc)
            raise  # surface to the job runner so the failure + reason land on the Job
    finally:
        db.close()


async def launch_management_plane(cluster_id: str, mgmt_kind: str = "portainer") -> None:
    """**Phase 2** — launch a management plane into a registered cluster, then
    register it in the brokered Portainer server. Scheduled as a background task
    by the API (the apply is slow).

    Phase 2's first plane is **Portainer-k8s** (operator's chosen model: agent +
    brokered server): apply the Portainer Agent into the cluster, then
    ``portainer_service.add_agent_endpoint`` registers it as an endpoint in the
    Portainer server the dashboard already brokers. Other ``mgmt_kind`` values
    are accepted by Phase 1 registration but not yet launched here."""
    from ..database import SessionLocal
    from . import portainer_service
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        if mgmt_kind != "portainer":
            row.status = "failed"
            db.commit()
            logger.warning("management-plane launch: only 'portainer' is wired in Phase 2 (got %r)", mgmt_kind)
            return
        row.status = "deploying"
        db.commit()
        try:
            kubeconfig = resolve_kubeconfig(db, cluster_id)
            await _apply_manifest_via_runner(kubeconfig, _PORTAINER_AGENT_MANIFEST_URL, target_cloud=row.cloud)
            host = _api_host(row.api_server)
            endpoint = await portainer_service.add_agent_endpoint(
                name=row.name, ip=host, port=_PORTAINER_AGENT_NODEPORT)
            row.mgmt_kind = "portainer"
            row.mgmt_endpoint = str(endpoint.get("Id") or endpoint.get("Name") or host)
            row.status = "managed"
            db.commit()
            logger.info("Cluster %s management plane up (portainer endpoint %s)", row.name, row.mgmt_endpoint)
        except Exception as exc:
            row.status = "failed"
            db.commit()
            logger.warning("management-plane launch failed cluster=%s: %s", cluster_id, exc)
            raise  # surface to the job runner so the failure + reason land on the Job
    finally:
        db.close()


async def run_management_plane(db: Session, *, cluster_id: str, job_id: str,
                               mgmt_kind: str = "portainer") -> None:
    """Worker entry for a ``k8s_management`` job: drive :func:`launch_management_plane`
    with Job tracking + a heartbeat, so the outcome and any error are visible in the
    UI (it was a fire-and-forget background task with no Job). Mirrors the job
    lifecycle of ``run_provision_apply``."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        await broadcast_progress(job_id, 20, f"Launching {mgmt_kind} management plane…")
        await launch_management_plane(cluster_id, mgmt_kind)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("management-plane job failed cluster=%s", cluster_id)
        return
    db.expire_all()
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row and row.status == "managed":
        job_service.set_completed(db, job_id)
    else:
        job_service.set_failed(
            db, job_id,
            f"management plane did not reach 'managed' (status={row.status if row else 'gone'})")


async def run_secret_delivery(db: Session, *, cluster_id: str, job_id: str,
                              kind: str) -> None:
    """Worker entry for a ``k8s_secret_delivery`` job: drive
    :func:`setup_secret_delivery` with Job tracking so an ESO install/removal
    failure is visible + durable instead of log-only."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        await broadcast_progress(job_id, 20, f"Configuring secret delivery ({kind})…")
        await setup_secret_delivery(cluster_id, kind)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("secret-delivery job failed cluster=%s", cluster_id)
        return
    job_service.set_completed(db, job_id)


# ── Entitle agent install (agent-cluster bootstrap, Task 7) ────────────────────
# Helm-installs BeyondTrust's Entitle agent into a managed cluster so PRIVATE
# resources (private RDS, PRA-only VMs) registered in Entitle are reachable. Shaped
# like setup_secret_delivery — an in-cluster install run as a tracked Job — rather
# than the Portainer-coupled management plane. The agent token is resolved
# server-side from the secrets backend and applied as a K8s Secret; it never lands
# on a row, in Terraform state, or in Helm values. See
# docs/design/entitle-resource-registration.md.
#
# ⚠️ VERIFICATION GATE: confirm the entitle-agent chart repo URL
# (entitle_agent_chart_repo) and the Helm value that points at the token Secret
# (entitle_agent_existing_secret_helm_key) against the published chart. Defaults
# follow BeyondTrust's documented `helm upgrade --install entitle-agent …`; if the
# chart only accepts a plaintext token, set entitle_agent_token_plaintext_helm_key.

VALID_ENTITLE_AGENT_ACTIONS = ("install", "remove")


def _entitle_agent_secret_manifest(namespace: str, secret_name: str, token: str) -> str:
    """Namespace + the K8s Secret the entitle-agent reads ``ENTITLE_TOKEN`` from
    (the key the entitleio/entitle provider's own Kubernetes example uses)."""
    return (
        "apiVersion: v1\n"
        "kind: Namespace\n"
        "metadata:\n"
        f"  name: {namespace}\n"
        "---\n"
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {secret_name}\n"
        f"  namespace: {namespace}\n"
        "type: Opaque\n"
        "stringData:\n"
        f"  ENTITLE_TOKEN: {_yaml_quote(token)}\n"
    )


async def setup_entitle_agent(cluster_id: str, action: str = "install") -> None:
    """Install (or remove) the Entitle agent in a managed cluster. Background task.

      * ``install`` → resolve the agent token server-side (auto-minting one via the
        entitleio/entitle provider when ``entitle_agent_token_ref`` is unset), apply the
        ``ENTITLE_TOKEN`` Secret via the kubectl runner, then
        ``helm upgrade --install entitle-agent`` referencing it. Records the hosting
        cluster in ``entitle_agent_cluster_id``.
      * ``remove``  → ``helm uninstall`` + delete the Secret (best-effort); clears
        ``entitle_agent_cluster_id`` when it pointed here.
    """
    from ..database import SessionLocal
    from . import config_service
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        kubeconfig = resolve_kubeconfig(db, cluster_id)
        namespace = _cfg("entitle_agent_namespace", "entitle")
        secret_name = _cfg("entitle_agent_secret_name", "entitle-agent-token")

        if action == "remove":
            try:
                await _helm_via_runner(kubeconfig, ["uninstall", "entitle-agent", "-n", namespace],
                                       add_eso_repo=False, target_cloud=row.cloud)
                await _delete_manifest_via_runner(
                    kubeconfig, _entitle_agent_secret_manifest(namespace, secret_name, "x"), target_cloud=row.cloud)
            except Exception as exc:
                logger.warning("entitle-agent teardown for %s partially failed: %s", cluster_id, exc)
            if config_service.get("entitle_agent_cluster_id") == cluster_id:
                config_service.set("entitle_agent_cluster_id", "")
            return

        # action == "install"
        repo = _cfg("entitle_agent_chart_repo")
        if not repo:
            raise K8sError(
                "entitle_agent_chart_repo is not configured (Helm repo URL for the entitle-agent chart)")
        # Resolve the agent token, auto-minting one (via the entitleio/entitle provider)
        # when none is configured yet — so the install stays one-click. Resolved
        # server-side; never persisted on this install's row/TF state.
        from . import entitle_registration_service
        try:
            token = await entitle_registration_service.ensure_agent_token()
        except entitle_registration_service.EntitleRegistrationError as exc:
            raise K8sError(f"Entitle agent token unavailable: {exc}") from exc
        if not token:
            raise K8sError("Entitle agent token resolved empty after mint")

        helm_args = ["upgrade", "--install", "entitle-agent", _cfg("entitle_agent_chart", "entitle-agent"),
                     "--repo", repo, "--namespace", namespace, "--create-namespace", "--wait",
                     "--set", f"kmsType={_cfg('entitle_agent_kms_type', 'kubernetes_secret_manager')}"]
        ver = _cfg("entitle_agent_chart_version")
        if ver:
            helm_args += ["--version", ver]

        plaintext_key = _cfg("entitle_agent_token_plaintext_helm_key")
        helm_values_stdin = None
        if plaintext_key:
            # The published chart takes the token as a plaintext value (no
            # existingSecret option). Pass it as a Helm values doc over stdin
            # (`-f -`) rather than `--set-string`, so the token never appears in
            # the runner's process args. Resolved server-side, never on a row/TF
            # state; it does still land in the in-cluster Helm release Secret
            # (chart limitation — unavoidable with this chart).
            helm_values_stdin = yaml.safe_dump(
                _nested_from_dotted(plaintext_key, token), default_flow_style=False)
            helm_args += ["-f", "-"]
        else:
            # Existing-Secret path (for a future chart version): apply the Secret +
            # point the chart at it so the token stays out of Helm values.
            await _apply_manifest_via_runner(
                kubeconfig, _entitle_agent_secret_manifest(namespace, secret_name, token), target_cloud=row.cloud)
            helm_args += ["--set",
                          f"{_cfg('entitle_agent_existing_secret_helm_key', 'agent.existingSecret')}={secret_name}"]

        # Operator-supplied extra --set args (the chart bundles Datadog, which may
        # need datadog.datadog.apiKey etc.). Comma-separated key=value list.
        for extra in (s.strip() for s in _cfg("entitle_agent_helm_extra_set").split(",")):
            if extra:
                helm_args += ["--set", extra]

        await _helm_via_runner(kubeconfig, helm_args, add_eso_repo=False,
                               values_stdin=helm_values_stdin, target_cloud=row.cloud)
        config_service.set("entitle_agent_cluster_id", cluster_id)
        logger.info("Entitle agent installed on cluster %s (ns=%s)", row.name, namespace)
    finally:
        db.close()


async def run_entitle_agent(db: Session, *, cluster_id: str, job_id: str,
                            action: str = "install") -> None:
    """Worker entry for a ``k8s_entitle_agent`` job: drive :func:`setup_entitle_agent`
    with Job tracking so an install/remove failure is visible + durable."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        await broadcast_progress(job_id, 20, f"Entitle agent: {action}…")
        await setup_entitle_agent(cluster_id, action)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("entitle-agent job failed cluster=%s", cluster_id)
        return
    job_service.set_completed(db, job_id)


# ── Register the cluster as an Entitle Kubernetes integration ──────────────────
# The generic Entitle "Kubernetes" application (covers EKS/AKS/GKE via the K8s API),
# registered through entitle_registration_service like VMs/DBs. Two modes:
#   * In-Cluster — when the Entitle agent is installed on THIS cluster: register with
#     just a user_prefix; the agent provides access (private API clusters).
#   * External   — mint a least-privilege Entitle ServiceAccount + token in-cluster
#     and register host + token + CA (public API clusters).
# Integration id + Terraform state are stashed (encrypted) in config_service so the
# deregister path can tear it down. ⚠️ The ServiceAccount is bound to cluster-admin
# for v1 (Entitle manages native RBAC); scope down once the required ClusterRole is
# confirmed against the Entitle Kubernetes integration docs.

VALID_ENTITLE_CLUSTER_ACTIONS = ("register", "deregister")


def _kubeconfig_host_ca(kubeconfig: str) -> tuple:
    """(API server URL, CA PEM) for the current-context cluster — or ("",""). """
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        cur = cfg.get("current-context")
        cl_name = None
        for ctx in cfg.get("contexts", []):
            if ctx.get("name") == cur:
                cl_name = (ctx.get("context") or {}).get("cluster")
                break
        clusters = cfg.get("clusters") or []
        cluster = next((c for c in clusters if c.get("name") == cl_name), None) or (clusters[0] if clusters else {})
        cdata = cluster.get("cluster") or {}
        host = cdata.get("server", "") or ""
        ca_b64 = cdata.get("certificate-authority-data", "") or ""
        ca = base64.b64decode(ca_b64).decode("utf-8") if ca_b64 else ""
        return host, ca
    except Exception as exc:  # noqa: BLE001
        logger.warning("kubeconfig host/CA parse failed: %s", exc)
        return "", ""


def _entitle_k8s_rbac_manifest(namespace: str, sa: str, secret: str) -> str:
    """Namespace + a ServiceAccount bound to cluster-admin + a long-lived SA token
    Secret (K8s 1.24+ no longer auto-creates token Secrets) for Entitle's
    External-Access connection."""
    return (
        "apiVersion: v1\nkind: Namespace\nmetadata:\n"
        f"  name: {namespace}\n---\n"
        "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
        f"  name: {sa}\n  namespace: {namespace}\n---\n"
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n"
        f"  name: {sa}-binding\n"
        "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n  name: cluster-admin\n"
        "subjects:\n"
        f"- kind: ServiceAccount\n  name: {sa}\n  namespace: {namespace}\n---\n"
        "apiVersion: v1\nkind: Secret\nmetadata:\n"
        f"  name: {secret}\n  namespace: {namespace}\n"
        "  annotations:\n"
        f"    kubernetes.io/service-account.name: {sa}\n"
        "type: kubernetes.io/service-account-token\n"
    )


async def _get_secret_b64_via_runner(kubeconfig: str, namespace: str, secret: str, key: str, target_cloud: str = "") -> str:
    """Return the **base64** value of ``.data[<key>]`` from a Secret (the caller
    decodes it in Python). Local (``k8s_runner=local``) uses the baked-in kubectl
    in-process; otherwise it runs as a one-shot cloud task (see
    ``k8s_runner_service``). Missing secret/key → ``""`` either way."""
    from . import k8s_runner_service
    jsonpath = "{.data." + key.replace(".", "\\.") + "}"
    if k8s_runner_service.mode(target_cloud) == "local":
        tmpdir = _write_kubeconfig(kubeconfig)
        try:
            kpath = os.path.join(tmpdir, "kubeconfig")
            cmd = ["kubectl", "--kubeconfig", kpath, "-n", namespace,
                   "get", "secret", secret, "-o", "jsonpath=" + jsonpath]
            try:
                return (await asyncio.to_thread(_run_sync, cmd)).strip()
            except K8sError:
                return ""
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    command = (
        f"kubectl -n {shlex.quote(namespace)} get secret {shlex.quote(secret)} "
        f"-o {shlex.quote('jsonpath=' + jsonpath)}"
    )
    try:
        out = await k8s_runner_service.run(
            kubeconfig=_runner_kubeconfig(kubeconfig), command=command,
            target_cloud=target_cloud, stdin_text=None, job_id="")
        return out.strip()
    except (K8sError, k8s_runner_service.K8sRunnerError):
        return ""


async def register_cluster_in_entitle(cluster_id: str, action: str = "register") -> None:
    """Register (or deregister) a managed cluster as an Entitle Kubernetes integration.
    Background task. In-Cluster when the Entitle agent is installed on this cluster;
    External (mint SA + token) otherwise."""
    from ..database import SessionLocal
    from . import config_service, entitle_registration_service as ent
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        user_prefix = _cfg("entitle_k8s_user_prefix", "entitle")

        if action == "deregister":
            state = config_service.get(f"entitle_k8s_tfstate_{cluster_id}")
            if state:
                try:
                    await ent.deregister(state)
                except Exception as exc:
                    logger.warning("entitle k8s deregister %s failed (non-fatal): %s", cluster_id, exc)
            config_service.set(f"entitle_k8s_tfstate_{cluster_id}", "")
            config_service.set(f"entitle_k8s_integration_id_{cluster_id}", "")
            return

        kubeconfig = resolve_kubeconfig(db, cluster_id)
        in_cluster = config_service.get("entitle_agent_cluster_id") == cluster_id
        if in_cluster:
            result = await ent.register_kubernetes(name=row.name, private=True, user_prefix=user_prefix)
        else:
            host, ca = _kubeconfig_host_ca(kubeconfig)
            if not host:
                raise K8sError("could not parse the API server host from the cluster kubeconfig")
            ns = _cfg("entitle_agent_namespace", "entitle")
            sa = _cfg("entitle_k8s_sa_name", "entitle-access")
            secret = f"{sa}-token"
            await _apply_manifest_via_runner(kubeconfig, _entitle_k8s_rbac_manifest(ns, sa, secret), target_cloud=row.cloud)
            token = ""
            for _ in range(6):  # the token controller populates .data.token async
                b64 = await _get_secret_b64_via_runner(kubeconfig, ns, secret, "token", target_cloud=row.cloud)
                if b64:
                    token = base64.b64decode(b64).decode("utf-8")
                    break
                await asyncio.sleep(2)
            if not token:
                raise K8sError("the Entitle service-account token did not populate — retry")
            result = await ent.register_kubernetes(
                name=row.name, private=False, user_prefix=user_prefix,
                host=host, token=token, ca_cert=ca)

        config_service.set(f"entitle_k8s_integration_id_{cluster_id}", result.get("integration_id") or "")
        config_service.set(f"entitle_k8s_tfstate_{cluster_id}", result.get("tf_state_json") or "")
        logger.info("cluster %s registered as Entitle Kubernetes integration %s (%s)",
                    row.name, result.get("integration_id"), "in-cluster" if in_cluster else "external")
    finally:
        db.close()


async def run_entitle_register(db: Session, *, cluster_id: str, job_id: str,
                               action: str = "register") -> None:
    """Worker entry for a ``k8s_entitle_register`` job: drive
    :func:`register_cluster_in_entitle` with Job tracking."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        await broadcast_progress(job_id, 20, f"Entitle Kubernetes integration: {action}…")
        await register_cluster_in_entitle(cluster_id, action)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("entitle k8s register job failed cluster=%s", cluster_id)
        return
    job_service.set_completed(db, job_id)


# ── Phase 3: brokered access ───────────────────────────────────────────────────

def console_url(db: Session, cluster_id: str) -> dict:
    """A link to the cluster's management console (Phase 3a). For **Portainer-k8s**
    the cluster was registered as a Portainer endpoint at launch (Phase 2), so the
    console is that endpoint's view on the Portainer server the dashboard already
    brokers — built from the configured server URL + the endpoint id stored in
    ``mgmt_endpoint``. For a plane whose ``mgmt_endpoint`` is already a URL
    (Rancher / Argo ingress), return it directly. (The native PRA
    ``tunnel_type=k8s`` jump + a true short-lived brokered session are Phase 3b.)"""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not row.mgmt_kind or not row.mgmt_endpoint:
        raise K8sError("no management plane launched yet — launch one first")
    ep = row.mgmt_endpoint
    if ep.startswith(("http://", "https://")):
        return {"url": ep, "kind": row.mgmt_kind}
    if row.mgmt_kind == "portainer":
        from ..config import settings
        from . import config_service
        base = (config_service.get("portainer_url") or getattr(settings, "portainer_url", "")).rstrip("/")
        if not base:
            raise K8sError("Portainer server URL is not configured (set portainer_url)")
        return {"url": f"{base}/#!/{ep}/kubernetes/dashboard", "kind": "portainer", "endpoint_id": ep}
    raise K8sError(f"no console URL builder for mgmt_kind={row.mgmt_kind!r}")


# ── Phase 3b: native PRA tunnel_type=k8s jump (beyondtrust/sra) + Entitle JIT ──
#
# Community provisions the PRA tunnel with the beyondtrust/sra Terraform provider
# (terraform_pra_service) — never btapi — matching the managed-database tunnel.
# The tunnel routes through a configurable Jumpoint (the "separate jumpoint"),
# looked up by NAME. The in-cluster Jumpoint pod (the prod plan's primary host)
# is out of scope here.

def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    from ..config import settings
    val = config_service.get(key)
    if val:
        return val
    return str(getattr(settings, key, "") or default)


def _parse_ca_cert(kubeconfig: str) -> str:
    """The current-context cluster's CA certificate (PEM) from a kubeconfig.
    Decodes ``certificate-authority-data``; "" when the kubeconfig uses an
    on-disk CA path or insecure-skip-tls (the k8s tunnel requires the inline cert)."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        clusters = {c["name"]: c.get("cluster", {}) for c in cfg.get("clusters", [])}
        target = None
        current = cfg.get("current-context")
        if current:
            for ctx in cfg.get("contexts", []):
                if ctx.get("name") == current:
                    target = clusters.get(ctx.get("context", {}).get("cluster"))
                    break
        if target is None and cfg.get("clusters"):
            target = cfg["clusters"][0].get("cluster", {})
        data = (target or {}).get("certificate-authority-data")
        if data:
            return base64.b64decode(data).decode("utf-8", "replace")
    except Exception as exc:
        logger.warning("kubeconfig CA parse failed: %s", exc)
    return ""


def _pra_configured() -> bool:
    """True when the sra provider creds + a Jumpoint name for the k8s tunnel are set."""
    return bool(_cfg("bt_api_host") and _cfg("bt_client_id")
                and (_cfg("bt_jumpoint_name")))


def _api_host_from_url(api_url: str) -> str:
    try:
        return urlparse(api_url).hostname or api_url
    except Exception:
        return api_url


def _apply_overrides(row, *, jump_group=None, jumpoint_name=None, pra_credential_ref=None) -> None:
    """Persist provided per-cluster overrides (blank clears, None leaves unchanged)."""
    if jump_group is not None:
        row.jump_group = jump_group.strip() or None
    if jumpoint_name is not None:
        row.jumpoint_name = jumpoint_name.strip() or None
    if pra_credential_ref is not None:
        row.pra_credential_ref = pra_credential_ref.strip() or None


async def _mint_pra_sa_token(kubeconfig: str, target_cloud: str = "") -> str:
    """Mint a cluster-admin ServiceAccount bearer token for PRA Vault injection:
    apply the SA + cluster-admin binding + token Secret (idempotent), then read the
    token (the token controller populates ``.data.token`` asynchronously). Reuses
    the same RBAC manifest + runner the Entitle External registration uses."""
    ns = _cfg("pra_k8s_namespace", "kube-system")
    sa = _cfg("pra_k8s_sa_name", "pra-access")
    secret = f"{sa}-token"
    await _apply_manifest_via_runner(kubeconfig, _entitle_k8s_rbac_manifest(ns, sa, secret), target_cloud=target_cloud)
    for _ in range(6):
        b64 = await _get_secret_b64_via_runner(kubeconfig, ns, secret, "token", target_cloud=target_cloud)
        if b64:
            return base64.b64decode(b64).decode("utf-8")
        await asyncio.sleep(2)
    raise K8sError("the PRA ServiceAccount token did not populate — retry")


async def register_pra_tunnel(db: Session, cluster_id: str, *, jump_group: str = None,
                              jumpoint_name: str = None, pra_credential_ref: str = None,
                              vault_inject: bool = False,
                              vault_account_group_id: Optional[int] = None) -> dict:
    """Provision the cluster's ``tunnel_type=k8s`` sra protocol-tunnel jump and
    record its id + Terraform state on the row. Idempotent: returns the existing
    ``pra_jump_id`` without recreating when one is already set.

    Per-cluster overrides (persisted; config is the fallback): ``jump_group`` (else
    ``bt_jump_group_name``), ``jumpoint_name`` (else ``bt_jumpoint_name`` — the
    Jumpoint the tunnel routes through), ``pra_credential_ref`` (a secret ref
    resolved to a bt_client_secret override for the apply).

    When ``vault_inject`` is set, a cluster-admin ServiceAccount bearer token is
    minted in the cluster and stored as a PRA Vault token account associated to the
    jump, so PRA injects the credential at session launch — PRA-only access with no
    Entitle. ``vault_account_group_id`` (else ``bt_vault_account_group_id``) places
    the Vault account in a group so a group policy grants it to users."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not _pra_configured():
        raise K8sError(
            "PRA is not configured — set bt_api_host, bt_client_id/secret, and a "
            "Jumpoint name (bt_jumpoint_name, the Jumpoint the k8s tunnel routes through)"
        )
    _apply_overrides(row, jump_group=jump_group, jumpoint_name=jumpoint_name,
                     pra_credential_ref=pra_credential_ref)

    # Bring up the shared on-demand Jumpoint host so the tunnel has something to
    # route through (VM/DB deploys do the same). Best-effort; real clouds only —
    # a registered 'local' cluster has no cloud host to manage.
    if row.cloud in ("aws", "azure", "gcp"):
        try:
            from . import jumpoint_host_service
            await jumpoint_host_service.ensure_jumpoint_host(
                row.cloud, _cfg(row.cloud + "_region") or row.region or "")
        except Exception as exc:
            logger.warning("k8s tunnel: ensure jumpoint host failed (non-fatal): %s", exc)

    if row.pra_jump_id:
        db.commit()
        return {"pra_jump_id": row.pra_jump_id, "already_registered": True}

    kubeconfig = resolve_kubeconfig(db, cluster_id)
    api_url = row.api_server or _parse_api_server(kubeconfig)
    if not api_url:
        raise K8sError("cluster API URL is unknown — cannot register a k8s tunnel")
    ca = _parse_ca_cert(kubeconfig)
    if not ca:
        raise K8sError(
            "could not extract a CA certificate from the kubeconfig — the k8s "
            "tunnel requires inline ca_certificates (certificate-authority-data)"
        )

    from . import config_service, terraform_pra_service as pra
    cred_ref = row.pra_credential_ref
    client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""

    # Optional PRA-Vault credential injection: mint a cluster SA bearer token and
    # hand it to the Vault token account associated with the jump.
    vault_account_name = ""
    sa_token = ""
    group_id = vault_account_group_id
    if vault_inject:
        sa_token = await _mint_pra_sa_token(kubeconfig, target_cloud=row.cloud)
        vault_account_name = f"k8s-{row.name}-sa"
        if group_id is None:
            cfg_group = _cfg("bt_vault_account_group_id")
            group_id = int(cfg_group) if cfg_group.strip().isdigit() else None

    result = await pra.provision_k8s_tunnel(
        name=f"k8s-{row.name}",
        hostname=_api_host_from_url(api_url),
        api_url=api_url,
        ca_certificates=ca,
        jump_group_name=row.jump_group or _cfg("bt_jump_group_name"),
        jumpoint_name=row.jumpoint_name or _cfg("bt_jumpoint_name"),
        client_secret=client_secret,
        vault_account_name=vault_account_name,
        sa_token=sa_token,
        vault_account_group_id=group_id,
    )
    row.pra_jump_id = str(result.get("tunnel_jump_id") or "")
    row.pra_tunnel_state = result.get("tf_state_json")
    db.commit()
    logger.info("Registered k8s PRA tunnel for cluster %s (jump id %s, vault account %s)",
                row.name, row.pra_jump_id, result.get("vault_account_id") or "none")
    return {"pra_jump_id": row.pra_jump_id, "jump_group_name": result.get("jump_group_name"),
            "vault_account_id": result.get("vault_account_id")}


async def deregister_pra_tunnel(db: Session, cluster_id: str) -> dict:
    """Tear down the cluster's k8s tunnel jump (REST DELETE by id) + its Vault token
    account (TF destroy from stored state, if any) and clear ``pra_jump_id`` /
    ``pra_tunnel_state`` (best-effort)."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not row.pra_jump_id:
        return {"ok": True, "removed": False}
    from . import terraform_pra_service as pra
    try:
        await pra.remove_k8s_tunnel(row.pra_tunnel_state, row.pra_jump_id)
    except Exception as exc:
        logger.warning("removing k8s tunnel for %s failed: %s", cluster_id, exc)

    # Revoke the in-cluster PRA ServiceAccount (the injected bearer token is
    # long-lived and would otherwise stay valid in the cluster after the Vault
    # account is gone). Deleting the dedicated namespace removes the SA + token
    # Secret; the ClusterRoleBinding is deleted by name. Best-effort, idempotent.
    try:
        ns = _cfg("pra_k8s_namespace", "pra-access")
        sa = _cfg("pra_k8s_sa_name", "pra-access")
        await _delete_manifest_via_runner(
            resolve_kubeconfig(db, cluster_id), _entitle_k8s_rbac_manifest(ns, sa, f"{sa}-token"), target_cloud=row.cloud)
    except Exception as exc:
        logger.warning("k8s tunnel: PRA ServiceAccount revoke for %s failed (non-fatal): %s",
                       cluster_id, exc)

    row.pra_jump_id = None
    row.pra_tunnel_state = None
    db.commit()

    # The cluster no longer needs the shared Jumpoint — tear it down if nothing
    # else (VM / DB / another tunneled cluster) is using it. Best-effort.
    if row.cloud in ("aws", "azure", "gcp"):
        try:
            from . import jumpoint_host_service
            await jumpoint_host_service.teardown_jumpoint_host_if_idle(
                db, row.cloud, _cfg(row.cloud + "_region") or row.region or "")
        except Exception as exc:
            logger.warning("k8s tunnel: jumpoint idle-teardown failed (non-fatal): %s", exc)

    return {"ok": True, "removed": True}


async def _entitle_rancher_grant(cluster_id: str, username: str) -> dict:
    """Open a time-boxed Entitle request for Rancher RBAC (the JIT *authorization*
    layer; the PRA tunnel is the *connection*). Only meaningful for
    ``mgmt_kind=rancher`` — Entitle's only management-plane connector."""
    from . import config_service, entitle_service
    from ..config import settings
    bundle = _cfg("k8s_rancher_entitle_bundle")
    if not bundle:
        raise K8sError(
            "k8s_rancher_entitle_bundle is not configured (the Entitle bundle/role "
            "id that grants the requested Rancher RBAC)"
        )
    email = (config_service.get("entitle_machine_identity_email")
             or getattr(settings, "entitle_machine_identity_email", "") or username)
    duration = int(_cfg("k8s_entitle_duration_minutes", "60") or 60)
    payload_hash = hashlib.sha256(f"{cluster_id}:{username}".encode()).hexdigest()
    rid = await entitle_service.submit_machine_request(
        operation="k8s_rancher_console",
        target={"bundle_id": bundle},
        duration_minutes=duration,
        payload_hash=payload_hash,
        behalf_of_email=email,
        justification=f"Rancher console access for {username} (cluster {cluster_id})",
    )
    payload = await entitle_service.get_machine_request(rid)
    category, reason = entitle_service.classify_machine_status(payload)
    return {"request_id": rid, "status": category, "reason": reason}


async def open_console(db: Session, cluster_id: str, username: str = "system", *,
                       jump_group: str = None, jumpoint_name: str = None,
                       pra_credential_ref: str = None, vault_inject: bool = False,
                       vault_account_group_id: Optional[int] = None) -> dict:
    """**Phase 3b** — broker access. Layers the PRA tunnel (connection) + the
    Entitle-Rancher JIT (authorization) over the Phase-3a console link:

      * **PRA configured** → ensure the sra ``tunnel_type=k8s`` jump exists and
        return a ``pra_tunnel`` descriptor (connect via the PRA representative
        console — no public ingress).
      * **otherwise** → the Phase-3a brokered ingress (``console_url``).
      * **mgmt_kind=rancher + entitle_enabled** → also open a time-boxed Entitle
        grant and report its status.

    The optional jump_group / jumpoint_name / pra_credential_ref are per-cluster
    overrides (config defaults are the fallback)."""
    from . import config_service
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    _apply_overrides(row, jump_group=jump_group, jumpoint_name=jumpoint_name,
                     pra_credential_ref=pra_credential_ref)
    db.commit()
    out: dict = {"cluster_id": cluster_id, "name": row.name, "mgmt_kind": row.mgmt_kind}

    if row.mgmt_kind == "rancher" and config_service.get_bool("entitle_enabled", True):
        out["entitle"] = await _entitle_rancher_grant(cluster_id, username)

    if _pra_configured():
        if not row.pra_jump_id:
            await register_pra_tunnel(db, cluster_id, vault_inject=vault_inject,
                                      vault_account_group_id=vault_account_group_id)
            db.refresh(row)
        out["access"] = "pra_tunnel"
        out["pra"] = {
            "tunnel_jump_id": row.pra_jump_id,
            "jump_group": row.jump_group or _cfg("bt_jump_group_name"),
            "jumpoint": row.jumpoint_name or _cfg("bt_jumpoint_name"),
            "note": "Connect through the BeyondTrust PRA representative console — no public ingress.",
        }
    else:
        out["access"] = "ingress"
        out.update(console_url(db, cluster_id))
    return out
