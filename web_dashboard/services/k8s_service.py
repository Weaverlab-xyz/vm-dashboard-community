"""Kubernetes cluster lifecycle.

A lifecycle service over the ``k8s_clusters`` table with two entry paths:
**provision** a new cluster with Terraform (``terraform/k8s_cluster/<cloud>`` for
aws/azure/gcp — see ``_PROVISION_IMPLEMENTED``) or **register** an
already-reachable cluster from its kubeconfig. Either way the kubeconfig is
stored as a **secrets-backend reference** (never in the row), plus list/get/
delete. On top of that: management-plane launch (Portainer / Rancher / Argo CD /
Headlamp — see ``VALID_MGMT_KINDS``), kubectl/helm run via the cloud-task
runner, brokered access via native PRA ``tunnel_type=k8s`` jump, and Entitle
cluster registration.
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
VALID_MGMT_KINDS = ("rancher", "argocd", "headlamp")

# config_service key that holds a cluster's kubeconfig; the row stores this
# string as kubeconfig_ref and config_service.get() resolves it.
_KUBECONFIG_KEY = "k8s_kubeconfig_{cluster_id}"

# Phase 2 — management-plane launch = Rancher (import model). The central Rancher
# server runs as a single privileged container on a PUBLIC (source-restricted) GCE
# COS node (stood up on the Containers page — see gcp_service.run_gce_rancher /
# rancher_node_service), NOT as an in-cluster Helm workload. Every k8s cluster is
# imported into it via the direct HTTPS v3 API (cattle-cluster-agent dials OUT to
# the public server-url — fits private clusters on any cloud / on-prem). See
# launch_management_plane / _import_into_rancher.

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
    from . import config_service
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
        "entitle_agent_installed": config_service.get("entitle_agent_cluster_id") == r.id,
        "api_tunnel_jump":       bool(config_service.get(f"k8s_api_tunnel_jump_{r.id}")),
        "entra_group_bound":     bool(config_service.get(f"k8s_entra_group_{r.id}")),
        # AKS is natively Entra-integrated (always federated); EKS/GKE are federated
        # once the "Entra federation" action runs (tracked in config).
        "entra_federation_enabled": (r.cloud == "azure")
                                    or bool(config_service.get(f"k8s_entra_fed_{r.id}")),
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


def count_rancher_imports(db: Session) -> int:
    """Number of clusters currently imported into the Rancher node
    (``mgmt_kind='rancher'``). Feeds the node-teardown soft-guard, which warns
    before orphaning imports (see ``rancher_node_service.run_teardown``)."""
    return db.query(K8sCluster).filter(K8sCluster.mgmt_kind == "rancher").count()


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
            f"cluster provisioning for {cloud!r} is not supported "
            f"(implemented: {', '.join(_PROVISION_IMPLEMENTED)})"
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

    # Build the -var set BEFORE creating the job so it can be embedded in the job
    # metadata atomically. The apply runs in a separate process (the dedicated job
    # runner) that polls for pending jobs. If the job were committed without
    # tf_variables and patched in by a follow-up call, the runner could claim it in
    # that gap and dispatch with no tf_variables → KeyError('tf_variables'). k8s
    # tf_variables carry no secrets, so embedding them at create time is safe.
    tf_variables = _build_cluster_tf_variables(
        cloud=cloud, cluster_id=cluster_id, name=name, region=region, opts=opts)

    from . import job_service
    job = job_service.create_job(
        db, job_type="k8s_provision", created_by=created_by,
        metadata={"cluster_id": cluster_id, "cloud": cloud, "name": name,
                  "region": region, "tf_variables": tf_variables},
    )
    row.deploy_job_id = job.id
    db.commit()

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

    All three clouds' modules create their own network (self-contained VPC +
    egress). The AWS module additionally peers its VPC back to the sandbox VPC
    (``aws_vpc_id`` / ``aws_vpc_cidr`` / ``aws_private_route_table_id``) and opens
    the DB/VM SGs for direct management-plane access. k8s version + node size
    fall back to config then the module defaults; ``node_instance_type`` maps to
    the per-cloud node-size var (EKS instance type / AKS vm_size / GKE machine
    type)."""
    _tags = {"managed-by": "vm-dashboard", "k8s-cluster-id": cluster_id}
    if cloud == "aws":
        tf = {
            "region": region,
            "cluster_name": _eks_name(f"k8s-{name}"),
            "tags": {"managed-by": "vm-dashboard", "k8s-cluster-id": cluster_id},
        }
        # The EKS module builds its own VPC / subnets / NAT-instance egress
        # (self-contained, like AKS/GKE). Optional per-cluster VPC CIDR override so
        # concurrent peered clusters don't overlap (each needs a distinct block).
        vpc_cidr = opts.get("vpc_cidr") or _cfg("aws_eks_vpc_cidr")
        if vpc_cidr:
            tf["vpc_cidr"] = vpc_cidr
        version = opts.get("k8s_version") or _cfg("aws_eks_k8s_version")
        if version:
            tf["k8s_version"] = version
        node_type = opts.get("node_instance_type") or _cfg("aws_eks_node_instance_type")
        if node_type:
            tf["node_instance_type"] = node_type
        if opts.get("node_count"):
            tf["node_desired"] = int(opts["node_count"])
        # EBS CSI driver (dynamic PVCs) — opt-in; the module default is OFF (most
        # demo/federation clusters have no storage workloads + it's the slowest,
        # most failure-prone provision step). The node launch template raises the
        # IMDS hop limit so the driver's controller can fetch node-role creds when
        # it IS enabled (e.g. a Rancher management plane).
        if opts.get("enable_ebs_csi"):
            tf["enable_ebs_csi"] = True
        # Peer the cluster's own VPC back to the sandbox VPC so an in-cluster agent
        # can reach the private VMs/DBs directly (Entitle/PRA also brokers access
        # without this). Only when the sandbox emitted its VPC id + return RT.
        sandbox_vpc = _cfg("aws_vpc_id")
        if sandbox_vpc:
            tf["sandbox_vpc_id"] = sandbox_vpc
            tf["sandbox_vpc_cidr"] = _cfg("aws_vpc_cidr")
            tf["sandbox_private_route_table_id"] = _cfg("aws_private_route_table_id")
            db_sg = _cfg("aws_db_security_group_id")
            if db_sg:
                tf["db_security_group_id"] = db_sg
            vm_sg = _cfg("aws_default_security_group_id")
            if vm_sg:
                tf["vm_security_group_id"] = vm_sg
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
        # Entitle agent workload identity + per-cluster Key Vault (azure_secret_manager):
        # the module derives the Key Vault name from cluster_id and sets the federated
        # credential subject from the namespace/SA — which must match the agent install
        # (setup_entitle_agent). cluster_id is passed on destroy too (row.id), so the
        # vault name stays stable.
        tf["cluster_id"] = cluster_id
        tf["agent_namespace"] = _cfg("entitle_agent_namespace", "entitle")
        tf["agent_service_account"] = _cfg("entitle_agent_service_account", "entitle-agent-sa")
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
    # All three cloud modules build their own network now (EKS self-contained +
    # peered), so no per-cloud subnet options are served.
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
        # Azure: capture the agent's workload-identity client id + per-cluster Key
        # Vault name (module outputs) so setup_entitle_agent can thread them into the
        # chart's platform.azure.* values for kmsType=azure_secret_manager.
        if cloud == "azure":
            azure_client_id = str(outputs.get("agent_identity_client_id") or "")
            azure_kv_name = str(outputs.get("agent_key_vault_name") or "")
            if azure_client_id:
                config_service.set(f"entitle_agent_azure_client_id_{cluster_id}", azure_client_id)
            if azure_kv_name:
                config_service.set(f"entitle_agent_azure_key_vault_name_{cluster_id}", azure_kv_name)
        # Capture the cluster's stable NAT egress IP (module output) so the Rancher
        # node firewall can auto-allow it — the private cluster's cattle-cluster-agent
        # dials OUT from this address to import.
        egress_ip = str(outputs.get("nat_public_ip") or "").strip()
        if egress_ip:
            row.egress_ip = egress_ip
        row.kubeconfig_ref = ref
        row.api_server = endpoint
        row.status = "registered"
        db.commit()
        job_service.set_completed(db, job_id)
        logger.info("k8s provision complete cluster_id=%s cluster=%s endpoint=%s egress_ip=%s",
                    cluster_id, cluster_out_name, endpoint, egress_ip or "-")
        # Best-effort: refresh the Rancher node firewall so this cluster's egress /32
        # is whitelisted before it's imported. Never fails the provision; no-ops when
        # the Rancher node isn't configured.
        try:
            from . import rancher_node_service
            await rancher_node_service.refresh_rancher_firewall(db)
        except Exception as exc:
            logger.warning("k8s provision: rancher firewall refresh failed (non-fatal): %s", exc)
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
    # NB: no central-Rancher guard here anymore — the central Rancher runs on an
    # external GCE COS node (not a K8sCluster row), so decommissioning a cluster
    # only removes its own import. The "don't orphan imports" guard now lives at
    # NODE teardown (see rancher_node_service.run_teardown + count_rancher_imports).
    if row.status == "decommissioning":
        # Only short-circuit if a teardown is actually still in flight. A prior
        # decommission that was cancelled/failed (e.g. the worker was busy and the
        # user cancelled it) leaves the row wedged at "decommissioning"; without the
        # status filter the stale job is returned and re-Delete is a silent no-op, so
        # the cluster can never be removed from the UI. Fall through to a fresh job.
        existing = (db.query(Job)
                      .filter(Job.job_type == "k8s_decommission",
                              Job.status.in_(("pending", "running")))
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

    # 1. PRA tunnels + Entra wiring (best-effort; clears the k8s tunnel's pra_jump_id /
    #    state on the row and the config_service keys). disable_entra_federation matters
    #    most on GKE — its gateway IAM grant + fleet state are project-level and would
    #    otherwise outlive the destroyed cluster (EKS's OIDC config dies with it).
    job_service.update_progress(db, job_id, 15, "Removing PRA tunnels…")
    for _dereg in (deregister_pra_tunnel, deregister_api_tunnel, unbind_entra_group,
                   disable_entra_federation):
        try:
            await _dereg(db, cluster_id)
        except Exception as exc:
            errors.append(f"PRA tunnel removal: {exc}")
            logger.warning("k8s decommission: tunnel removal for %s failed: %s", cluster_id, exc)

    # 1b. Rancher: remove this cluster's import from the central Rancher node via
    #     the direct HTTPS API (best-effort, log-only — a stale Rancher entry must
    #     not fail the cloud teardown).
    rancher_import_id = _cfg(f"rancher_cluster_id_{cluster_id}")
    if rancher_import_id:
        job_service.update_progress(db, job_id, 25, "Removing from Rancher…")
        try:
            from . import rancher_service
            if _cfg("rancher_server_url") and _cfg("rancher_api_token"):
                await rancher_service.delete_cluster_direct(cluster_id=rancher_import_id)
            config_service.set(f"rancher_cluster_id_{cluster_id}", "")
            config_service.set(f"rancher_manifest_url_{cluster_id}", "")
        except Exception as exc:
            logger.warning("k8s decommission: Rancher removal for %s failed: %s", cluster_id, exc)

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
    # Clear the per-cluster Entitle-agent azure_secret_manager values (the MI + Key
    # Vault themselves are TF-managed and destroyed with the cluster above).
    for _k in (f"entitle_agent_azure_client_id_{cluster_id}",
               f"entitle_agent_azure_key_vault_name_{cluster_id}"):
        try:
            config_service.set(_k, "")
        except Exception as exc:
            logger.warning("k8s decommission: clearing %s failed: %s", _k, exc)
    db.delete(row)
    db.commit()
    # Re-tighten the Rancher node firewall now the row is gone — its egress /32 must
    # drop out of the merged source set. Best-effort (must run AFTER the delete).
    try:
        from . import rancher_node_service
        await rancher_node_service.refresh_rancher_firewall(db)
    except Exception as exc:
        logger.warning("k8s decommission: rancher firewall refresh failed (non-fatal): %s", exc)
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


# ── Phase 2: management-plane launch (Rancher: central + import) ───────────────

async def _run_cluster_command(kubeconfig: str, command: str, target_cloud: str = "") -> str:
    """Run an arbitrary shell command against the cluster (KUBECONFIG exported),
    returning stdout. Backs the Rancher API calls, which run as one-shot
    ``kubectl run … curl`` pods inside the cluster (rancher_service builds them).
    Local mode runs in-process; otherwise a one-shot cloud task (k8s_runner_service)."""
    from . import k8s_runner_service
    if k8s_runner_service.mode(target_cloud) == "local":
        tmpdir = _write_kubeconfig(kubeconfig)
        try:
            env = _helm_env(tmpdir)  # exports KUBECONFIG into the tmpdir
            return await asyncio.to_thread(_run_sync, ["sh", "-c", command], None, env)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return await k8s_runner_service.run(
        kubeconfig=_runner_kubeconfig(kubeconfig), command=command,
        target_cloud=target_cloud, job_id="")


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


async def _import_into_rancher(db: Session, row, kubeconfig) -> None:
    """Import THIS cluster into the central Rancher NODE (an external, public GCE
    COS VM). Creates the imported cluster via the direct HTTPS v3 API, then either
    auto-applies the registration manifest (when the dashboard holds a kubeconfig
    for this cluster) or records the ``kubectl apply`` command for the operator to
    run against an external/private cluster. Either way the cattle-cluster-agent
    dials OUT to the public server-url, so private clusters on any cloud / on-prem
    work with egress only. Sets mgmt_endpoint to the Rancher dashboard deep-link."""
    from . import config_service, rancher_service
    server_url = _cfg("rancher_server_url")
    api_token = _cfg("rancher_api_token")
    if not (server_url and api_token):
        raise K8sError("central Rancher node is not running — stand it up on the Containers page first")

    # Belt-and-suspenders: make sure this cluster's egress /32 is in the node
    # firewall before the agent tries to dial out (no-op for registered clusters
    # that have no captured egress_ip, and when the node isn't GCP-configured).
    try:
        from . import rancher_node_service
        await rancher_node_service.refresh_rancher_firewall(db)
    except Exception as exc:
        logger.warning("rancher import: firewall refresh failed (non-fatal): %s", exc)

    # Direct HTTPS to the public Rancher API (no in-cluster curl pods).
    rancher_cluster_id, manifest_url = await rancher_service.create_import_cluster_direct(name=row.name)

    config_service.set(f"rancher_cluster_id_{row.id}", rancher_cluster_id)
    row.mgmt_kind = "rancher"
    row.mgmt_endpoint = f"{server_url.rstrip('/')}/dashboard/c/{rancher_cluster_id}"

    if kubeconfig:
        # Dashboard holds a kubeconfig — auto-apply the registration manifest; the
        # applied cattle-cluster-agent dials out to server-url (works for private
        # clusters anywhere — the apply itself only needs runner egress).
        await _apply_manifest_via_runner(kubeconfig, manifest_url, target_cloud=row.cloud)
        row.status = "managed"
        logger.info("Cluster %s imported into Rancher + agent applied (%s)", row.name, row.mgmt_endpoint)
    else:
        # No kubeconfig (external/private cluster) — surface the apply command so
        # the operator registers it themselves. Reaches 'managed' once the agent
        # dials in (a follow-up can poll Rancher to flip the status).
        config_service.set(f"rancher_manifest_url_{row.id}", manifest_url)
        row.status = "awaiting_agent"
        logger.info("Cluster %s import created in Rancher — awaiting agent: kubectl apply -f %s",
                    row.name, manifest_url)


async def launch_management_plane(cluster_id: str, mgmt_kind: str = "rancher") -> None:
    """**Phase 2** — import a cluster into the central Rancher node.

    The central Rancher runs as an external, PUBLIC GCE COS node (stood up on the
    Containers page), so there is no "first cluster becomes central" step — EVERY
    cluster is an import (:func:`_import_into_rancher`). Requires the node's
    server-url + API token. Only ``rancher`` is wired; other kinds are accepted at
    registration but not launched here. Runs as a background task. The row reaches
    ``managed`` when the dashboard applies the agent, or ``awaiting_agent`` when the
    operator must apply it to an external cluster."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
        if row is None:
            return
        if mgmt_kind != "rancher":
            row.status = "failed"
            db.commit()
            logger.warning("management-plane launch: only 'rancher' is wired (got %r)", mgmt_kind)
            return
        if not (_cfg("rancher_server_url") and _cfg("rancher_api_token")):
            row.status = "failed"
            db.commit()
            raise K8sError("central Rancher node is not running — stand it up on the Containers page first")
        row.status = "deploying"
        db.commit()
        try:
            # "Does the dashboard hold a usable kubeconfig?" is the discriminator
            # (registered clusters can also carry one) — not provisioned-vs-registered.
            try:
                kubeconfig = resolve_kubeconfig(db, cluster_id)
            except K8sError:
                kubeconfig = None
            await _import_into_rancher(db, row, kubeconfig)
            db.commit()  # _import_into_rancher set the status (managed | awaiting_agent)
            logger.info("Cluster %s management plane up (rancher, status=%s, endpoint %s)",
                        row.name, row.status, row.mgmt_endpoint)
        except Exception as exc:
            row.status = "failed"
            db.commit()
            logger.warning("management-plane launch failed cluster=%s: %s", cluster_id, exc)
            raise  # surface to the job runner so the failure + reason land on the Job
    finally:
        db.close()


async def run_management_plane(db: Session, *, cluster_id: str, job_id: str,
                               mgmt_kind: str = "rancher") -> None:
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
        job_service.set_completed(db, job_id, {"mgmt_endpoint": row.mgmt_endpoint})
    elif row and row.status == "awaiting_agent":
        # External cluster (no dashboard kubeconfig) — success, but the operator
        # must apply the registration manifest to their cluster.
        manifest_url = _cfg(f"rancher_manifest_url_{cluster_id}")
        job_service.set_completed(db, job_id, {
            "mgmt_endpoint": row.mgmt_endpoint,
            "manifest_url": manifest_url,
            "apply_command": f"kubectl apply -f {manifest_url}" if manifest_url else "",
            "message": "Import created in Rancher — run the apply command on the target cluster.",
        })
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


def _entitle_agent_clusterrolebinding_manifest(namespace: str, sa: str) -> str:
    """A ClusterRoleBinding granting the Entitle **agent** ServiceAccount
    cluster-admin. In In-Cluster (agent-brokered) mode Entitle drives this SA to
    enumerate the cluster (namespaces, roles, clusterroles) and to create/delete
    (Cluster)RoleBindings for JIT grants. The agent Helm chart only grants a
    namespace-scoped Role for self-management, so without this the integration
    reports "Failed to fetch the resources of <cluster>". Same cluster-admin
    requirement as the External SA (see _entitle_k8s_rbac_manifest); applied as a
    separate manifest so it doesn't depend on the chart's RBAC value schema."""
    return (
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n"
        "  name: entitle-agent-cluster-admin\n"
        "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n  name: cluster-admin\n"
        "subjects:\n"
        f"- kind: ServiceAccount\n  name: {sa}\n  namespace: {namespace}\n"
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
                agent_sa = _cfg("entitle_agent_service_account", "entitle-agent-sa")
                await _delete_manifest_via_runner(
                    kubeconfig, _entitle_agent_clusterrolebinding_manifest(namespace, agent_sa),
                    target_cloud=row.cloud)
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

        # Per-cloud KMS backend: AKS uses azure_secret_manager (Azure Key Vault via
        # workload identity — the in-cluster-Secrets path 401s there); EKS/GKE keep
        # kubernetes_secret_manager. Blank per-cloud → the shared entitle_agent_kms_type.
        kms_type = (_cfg(f"entitle_agent_kms_type_{row.cloud}")
                    or _cfg("entitle_agent_kms_type")
                    or "kubernetes_secret_manager")
        helm_args = ["upgrade", "--install", "entitle-agent", _cfg("entitle_agent_chart", "entitle-agent"),
                     "--repo", repo, "--namespace", namespace, "--create-namespace", "--wait",
                     "--set", f"kmsType={kms_type}"]
        # azure_secret_manager: the agent vaults its keys in the per-cluster Key Vault
        # via workload identity. The azure_aks module built the MI + federated credential
        # + vault + Secrets-Officer grant and run_provision_apply captured the client id +
        # vault name; thread them into the chart. The chart auto-wires the SA annotation +
        # pod label from platform.mode=azure + platform.azure.clientId (verified via
        # `helm template`), so no manual annotation/label --set is needed.
        if row.cloud == "azure" and kms_type == "azure_secret_manager":
            client_id = config_service.get(f"entitle_agent_azure_client_id_{cluster_id}")
            kv_name = config_service.get(f"entitle_agent_azure_key_vault_name_{cluster_id}")
            tenant_id = _cfg("azure_tenant_id")
            sa_name = _cfg("entitle_agent_service_account", "entitle-agent-sa")
            missing = [n for n, v in (("client id", client_id), ("key vault name", kv_name),
                                      ("tenant id", tenant_id)) if not v]
            if missing:
                raise K8sError(
                    f"azure_secret_manager needs the agent managed-identity {', '.join(missing)} — "
                    "provision the AKS cluster with the workload-identity module, or set "
                    "entitle_agent_kms_type_azure to kubernetes_secret_manager")
            helm_args += ["--set", "platform.mode=azure",
                          "--set", f"platform.azure.clientId={client_id}",
                          "--set", f"platform.azure.keyVaultName={kv_name}",
                          "--set", f"platform.azure.tenantId={tenant_id}",
                          "--set", f"serviceAccount.name={sa_name}"]
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
        # Grant the agent ServiceAccount cluster-admin. In-Cluster mode drives this
        # SA to enumerate the cluster and manage JIT (Cluster)RoleBindings, but the
        # chart only gives it a namespace Role — without this the Entitle integration
        # reports "Failed to fetch the resources". The chart creates the SA (name from
        # entitle_agent_service_account, matching the chart default), so bind it now.
        agent_sa = _cfg("entitle_agent_service_account", "entitle-agent-sa")
        await _apply_manifest_via_runner(
            kubeconfig, _entitle_agent_clusterrolebinding_manifest(namespace, agent_sa),
            target_cloud=row.cloud)
        config_service.set("entitle_agent_cluster_id", cluster_id)
        logger.info("Entitle agent installed on cluster %s (ns=%s)", row.name, namespace)
        # If a k8s connector was already registered for this cluster (before the agent
        # existed) it used External/service-account mode; now that the agent is present,
        # re-register it In-Cluster (agent-brokered) so it no longer depends on Entitle's
        # cloud reaching the API directly. Best-effort, non-fatal.
        if config_service.get(f"entitle_k8s_integration_id_{cluster_id}"):
            try:
                await register_cluster_in_entitle(cluster_id, action="deregister")
                await register_cluster_in_entitle(cluster_id, action="register")
                logger.info("re-registered Entitle k8s connector for %s In-Cluster after agent install", row.name)
            except Exception as exc:
                logger.warning("In-Cluster re-registration of the Entitle connector for %s failed "
                               "(non-fatal): %s", row.name, exc)
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
#   * External   — mint an Entitle ServiceAccount (cluster-admin — see below) + token
#     in-cluster and register host + token + CA (public API clusters).
# Integration id + Terraform state are stashed (encrypted) in config_service so the
# deregister path can tear it down. The ServiceAccount is bound to cluster-admin by
# design: Entitle's Kubernetes integration documents and requires it. A general RBAC
# broker can only grant permissions it already holds (K8s privilege-escalation
# prevention), so cluster-admin is necessary for Entitle to create short-lived access
# to arbitrary resources on a user's behalf — it is not a placeholder to scope down.
# See https://docs.beyondtrust.com/entitle/docs/entitle-integration-kubernetes.

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
    External-Access connection. cluster-admin is required by Entitle's K8s
    integration (see the register-cluster note above), not a placeholder. Also
    reused by ``_mint_pra_sa_token`` for the PRA-injected, brokered-session token,
    which is intentionally privileged."""
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
            logger.warning(
                "cluster %s has no Entitle agent installed — registering the k8s connector in "
                "External/service-account mode: Entitle's cloud connects directly to the API server (%s) "
                "with a minted SA token, which is unhealthy for a private/unreachable API. Install the "
                "Entitle agent and re-register for In-Cluster (agent-brokered) access.", row.name, host)
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


async def register_rancher_in_entitle(action: str = "register") -> None:
    """Register (or deregister) the central Rancher NODE as an Entitle **Rancher**
    integration. Node-scoped (not per-cluster) — the node is the singleton mgmt
    plane. With a public source-restricted server-url, Entitle's cloud can reach
    it directly (``private=False``, no agent token); keep ``entitle_rancher_private``
    for tenants who lock the node behind CIDRs Entitle can't traverse. Writes
    ``entitle_rancher_integration_id`` + ``entitle_rancher_tfstate`` (consumed by
    deregister). Background task."""
    from . import config_service, entitle_registration_service as ent

    if action == "deregister":
        state = config_service.get("entitle_rancher_tfstate")
        if state:
            try:
                await ent.deregister(state)
            except Exception as exc:
                logger.warning("entitle rancher deregister failed (non-fatal): %s", exc)
        config_service.set("entitle_rancher_tfstate", "")
        config_service.set("entitle_rancher_integration_id", "")
        return

    server_url = _cfg("rancher_server_url")
    api_token = _cfg("rancher_api_token")
    if not (server_url and api_token):
        raise K8sError("Rancher node is not running — deploy it before registering in Entitle")
    verify = config_service.get_bool("rancher_verify_tls", False)
    private = config_service.get_bool("entitle_rancher_private", False)
    result = await ent.register_rancher(
        name=_cfg("entitle_rancher_app_slug", "rancher"),
        server_url=server_url, api_token=api_token, verify=verify, private=private)
    config_service.set("entitle_rancher_integration_id", result.get("integration_id") or "")
    config_service.set("entitle_rancher_tfstate", result.get("tf_state_json") or "")
    logger.info("Rancher node registered as Entitle integration %s (private=%s)",
                result.get("integration_id"), private)


async def run_rancher_entitle_register(db: Session, *, job_id: str,
                                       action: str = "register") -> None:
    """Worker entry for a ``rancher_entitle_register`` job: drive
    :func:`register_rancher_in_entitle` with Job tracking."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        await broadcast_progress(job_id, 20, f"Entitle Rancher integration: {action}…")
        await register_rancher_in_entitle(action)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("entitle rancher register job failed action=%s", action)
        return
    job_service.set_completed(db, job_id)


async def run_tunnel(db: Session, *, cluster_id: str, job_id: str, action: str = "register",
                     jump_group: str = None, jumpoint_name: str = None,
                     pra_credential_ref: str = None, vault_inject: bool = False,
                     vault_account_group_id: Optional[int] = None) -> None:
    """Worker entry for a ``k8s_tunnel`` job: provision/remove the cluster's
    ``tunnel_type=k8s`` PRA jump with Job tracking. Runs as a background job (not
    inline in the request) because the vault-inject path mints a cluster-admin SA
    token via the cluster runner — on a GCP Cloud Run runner that's several minutes,
    which would otherwise hang the request past the gunicorn worker timeout."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        if action == "remove":
            await broadcast_progress(job_id, 20, "Removing the PRA tunnel…")
            await deregister_pra_tunnel(db, cluster_id)
        else:
            await broadcast_progress(job_id, 20, "Provisioning the PRA tunnel…")
            await register_pra_tunnel(
                db, cluster_id, jump_group=jump_group, jumpoint_name=jumpoint_name,
                pra_credential_ref=pra_credential_ref, vault_inject=vault_inject,
                vault_account_group_id=vault_account_group_id)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s tunnel job failed cluster=%s action=%s", cluster_id, action)
        return
    job_service.set_completed(db, job_id)


# ── Phase 3: brokered access ───────────────────────────────────────────────────

def console_url(db: Session, cluster_id: str) -> dict:
    """A link to the cluster's management console (Phase 3a). For Rancher (the only
    wired plane) ``mgmt_endpoint`` holds the Rancher dashboard URL — the server-url
    for the central cluster, a ``/dashboard/c/<id>`` deep-link for imported ones —
    so it's returned directly. Reaching that internal URL is brokered separately by
    :func:`open_console` (the PRA tcp-tunnel) — no public ingress."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not row.mgmt_kind or not row.mgmt_endpoint:
        raise K8sError("no management plane launched yet — launch one first")
    ep = row.mgmt_endpoint
    if ep.startswith(("http://", "https://")):
        return {"url": ep, "kind": row.mgmt_kind}
    raise K8sError(f"management plane {row.mgmt_kind!r} has no console URL (mgmt_endpoint is not a URL)")


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
    """Mint a cluster-admin ServiceAccount bearer token for PRA Vault injection in a
    **single** runner invocation: apply the SA + cluster-admin binding + token Secret,
    then read the token — preferring the long-lived Secret token (K8s populates
    ``.data.token`` for a manually-created service-account-token Secret) and falling
    back to a bound ``kubectl create token`` (TokenRequest) when the controller doesn't
    populate it, e.g. GKE. The token is echoed sentinel-wrapped (``BTKN<…>BTKN``) so it
    survives the runner's combined stdout/stderr log capture.

    One command on purpose: on a Cloud Run runner each kubectl call is a fresh ~2-min
    container, so the old apply-then-poll-``.data.token``-6× loop was ~7 containers
    (~14 min) and still timed out on GKE, which never populated the Secret."""
    ns = _cfg("pra_k8s_namespace", "kube-system")
    sa = _cfg("pra_k8s_sa_name", "pra-access")
    secret = f"{sa}-token"
    manifest = _entitle_k8s_rbac_manifest(ns, sa, secret)
    q_ns, q_sa, q_sec = shlex.quote(ns), shlex.quote(sa), shlex.quote(secret)
    # stdin (the manifest) is piped into `kubectl apply -f -`; the rest runs after `&&`
    # and reads no stdin. KUBECONFIG is exported by the runner (cloud) / _helm_env (local).
    command = (
        "kubectl apply -f - 1>&2 && { tok=''; "
        "for i in $(seq 1 10); do "
        f"v=$(kubectl -n {q_ns} get secret {q_sec} -o jsonpath='{{.data.token}}' 2>/dev/null || true); "
        "if [ -n \"$v\" ]; then tok=$(printf '%s' \"$v\" | base64 -d 2>/dev/null); break; fi; sleep 2; done; "
        f"if [ -z \"$tok\" ]; then tok=$(kubectl -n {q_ns} create token {q_sa} --duration=24h 2>/dev/null || true); fi; "
        "if [ -z \"$tok\" ]; then echo pra-token-unavailable 1>&2; exit 3; fi; "
        "printf 'BTKN<%s>BTKN\\n' \"$tok\"; }"
    )
    from . import k8s_runner_service
    if k8s_runner_service.mode(target_cloud) == "local":
        tmpdir = _write_kubeconfig(kubeconfig)
        try:
            out = await asyncio.to_thread(_run_sync, ["sh", "-c", command], manifest, _helm_env(tmpdir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        out = await k8s_runner_service.run(
            kubeconfig=_runner_kubeconfig(kubeconfig), command=command,
            target_cloud=target_cloud, stdin_text=manifest, job_id="")
    # rfind → the LAST marker, so a reused runner-job name that pulled more than one
    # run's logs still yields THIS run's (newest) token.
    _i = (out or "").rfind("BTKN<")
    token = out[_i + 5:].partition(">BTKN")[0].strip() if _i != -1 else ""
    if not token:
        logger.error("PRA token mint: no BTKN marker in runner output (len=%d): %r",
                     len(out or ""), (out or "")[-1200:])
        raise K8sError("could not mint the PRA ServiceAccount token — see the job logs")
    return token


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


# ── k8s API TCP tunnel (direct-kubectl, token-free kubeconfig) ────────────────
#
# A parallel action to the tunnel_type=k8s tunnel above. This one creates a
# GENERIC tunnel_type=tcp jump straight to the API endpoint (host:443) with a
# pinned local listen port, and hands back a kubeconfig that authenticates with
# the cluster's own (cloud-native, exec-plugin) auth — NO injected token. Because
# raw TCP forwards bytes only, kubectl's `--as` impersonation reaches the API
# intact, so an operator can consume per-user Entitle grants (unlike the k8s
# tunnel, whose proxy strips impersonation). Tracked in config_service keys
# (k8s_api_tunnel_jump_{cid} / k8s_api_tunnel_state_{cid}) — no DB column.

async def register_api_tunnel(db: Session, cluster_id: str, *, jump_group: str = None,
                              jumpoint_name: str = None, pra_credential_ref: str = None) -> dict:
    """Provision the cluster's ``tunnel_type=tcp`` API tunnel jump (pinned local
    port) and record its id + Terraform state in config_service. Idempotent:
    returns the existing jump id without recreating when one is already set."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if not _pra_configured():
        raise K8sError(
            "PRA is not configured — set bt_api_host, bt_client_id/secret, and a "
            "Jumpoint name (bt_jumpoint_name, the Jumpoint the tunnel routes through)"
        )
    _apply_overrides(row, jump_group=jump_group, jumpoint_name=jumpoint_name,
                     pra_credential_ref=pra_credential_ref)

    if row.cloud in ("aws", "azure", "gcp"):
        try:
            from . import jumpoint_host_service
            await jumpoint_host_service.ensure_jumpoint_host(
                row.cloud, _cfg(row.cloud + "_region") or row.region or "")
        except Exception as exc:
            logger.warning("k8s API tunnel: ensure jumpoint host failed (non-fatal): %s", exc)

    from . import config_service, terraform_pra_service as pra
    jump_key = f"k8s_api_tunnel_jump_{cluster_id}"
    state_key = f"k8s_api_tunnel_state_{cluster_id}"
    if config_service.get(jump_key):
        return {"api_tunnel_jump_id": config_service.get(jump_key), "already_registered": True}

    kubeconfig = resolve_kubeconfig(db, cluster_id)
    api_url = row.api_server or _parse_api_server(kubeconfig)
    if not api_url:
        raise K8sError("cluster API URL is unknown — cannot register an API tunnel")
    host = _api_host_from_url(api_url)
    remote_port = urlparse(api_url).port or 443
    local_port = int(_cfg("k8s_api_tunnel_local_port", "6443"))

    cred_ref = row.pra_credential_ref
    client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""

    result = await pra.provision_api_tunnel(
        name=f"k8s-{row.name}-api",
        hostname=host,
        jump_group_name=row.jump_group or _cfg("bt_jump_group_name"),
        jumpoint_name=row.jumpoint_name or _cfg("bt_jumpoint_name"),
        local_port=local_port,
        remote_port=remote_port,
        client_secret=client_secret,
    )
    config_service.set(jump_key, str(result.get("tunnel_jump_id") or ""))
    config_service.set(state_key, result.get("tf_state_json") or "")
    logger.info("Registered k8s API TCP tunnel for cluster %s (jump id %s, local port %s)",
                row.name, result.get("tunnel_jump_id"), local_port)
    return {"api_tunnel_jump_id": str(result.get("tunnel_jump_id") or ""),
            "jump_group_name": result.get("jump_group_name"), "local_port": local_port}


async def deregister_api_tunnel(db: Session, cluster_id: str) -> dict:
    """Tear down the cluster's API TCP tunnel jump (TF destroy from stored state)
    and clear its config_service keys (best-effort). No in-cluster SA to revoke —
    this tunnel injects no credential."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    from . import config_service, terraform_pra_service as pra
    jump_key = f"k8s_api_tunnel_jump_{cluster_id}"
    state_key = f"k8s_api_tunnel_state_{cluster_id}"
    if row is None or not config_service.get(jump_key):
        return {"ok": True, "removed": False}
    state = config_service.get(state_key)
    if state:
        try:
            await pra.remove_api_tunnel(state)
        except Exception as exc:
            logger.warning("removing k8s API tunnel for %s failed: %s", cluster_id, exc)
    config_service.set(jump_key, "")
    config_service.set(state_key, "")

    if row.cloud in ("aws", "azure", "gcp"):
        try:
            from . import jumpoint_host_service
            await jumpoint_host_service.teardown_jumpoint_host_if_idle(
                db, row.cloud, _cfg(row.cloud + "_region") or row.region or "")
        except Exception as exc:
            logger.warning("k8s API tunnel: jumpoint idle-teardown failed (non-fatal): %s", exc)

    return {"ok": True, "removed": True}


def _repoint_kubeconfig_to_tunnel(kubeconfig: str, local_port: int) -> str:
    """Pure transform: return ``kubeconfig`` with the current-context cluster's
    ``server`` repointed at ``https://127.0.0.1:<local_port>`` and ``tls-server-name``
    set to the original API host (so its cert SAN still validates through localhost).
    The CA and the ``users`` (cloud-native exec-plugin) auth are kept verbatim — the
    result is token-free and carries no injected credential."""
    cfg = yaml.safe_load(kubeconfig) or {}
    clusters = cfg.get("clusters") or []
    target = None
    cur = cfg.get("current-context")
    if cur:
        cl_name = next((c.get("context", {}).get("cluster")
                        for c in cfg.get("contexts", []) if c.get("name") == cur), None)
        target = next((c for c in clusters if c.get("name") == cl_name), None)
    if target is None and clusters:
        target = clusters[0]
    if target is None or "cluster" not in target:
        raise K8sError("stored kubeconfig has no cluster entry to repoint")
    cl = target["cluster"]
    orig_host = _api_host_from_url(cl.get("server", ""))
    cl["server"] = f"https://127.0.0.1:{int(local_port)}"
    if orig_host:
        cl["tls-server-name"] = orig_host
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


def build_api_tunnel_kubeconfig(db: Session, cluster_id: str) -> str:
    """Return a kubeconfig for the cluster's API TCP tunnel: the STORED kubeconfig
    repointed at the local tunnel port (see ``_repoint_kubeconfig_to_tunnel``).
    Token-free — reuses the cluster's own cloud-native exec-plugin auth."""
    kubeconfig = resolve_kubeconfig(db, cluster_id)
    return _repoint_kubeconfig_to_tunnel(kubeconfig, int(_cfg("k8s_api_tunnel_local_port", "6443")))


async def run_api_tunnel(db: Session, *, cluster_id: str, job_id: str, action: str = "register",
                         jump_group: str = None, jumpoint_name: str = None,
                         pra_credential_ref: str = None) -> None:
    """Worker entry for a ``k8s_api_tunnel`` job: provision/remove the cluster's
    ``tunnel_type=tcp`` API tunnel with Job tracking (a background job because the
    terraform apply against the sra provider can take a minute or two)."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        if action == "remove":
            await broadcast_progress(job_id, 20, "Removing the API TCP tunnel…")
            await deregister_api_tunnel(db, cluster_id)
        else:
            await broadcast_progress(job_id, 20, "Provisioning the API TCP tunnel…")
            await register_api_tunnel(
                db, cluster_id, jump_group=jump_group, jumpoint_name=jumpoint_name,
                pra_credential_ref=pra_credential_ref)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s API tunnel job failed cluster=%s action=%s", cluster_id, action)
        return
    job_service.set_completed(db, job_id)


# ── Entra/IdP group → cluster RBAC (real-identity JIT) ────────────────────────
#
# Bind an Entra (AAD) group to a ClusterRole on a managed cluster: any member of
# the group gets that role when they authenticate as themselves (their AAD token
# carries the group OID → matches a `Group` RBAC subject). Entitle's Entra-ID
# integration JIT-grants group membership, so this is real-identity JIT without
# the synthetic `entitle:` impersonation subject the k8s (agent) connector uses.
# The group id + role default from config (entra_rbac_group_id / _role) and are
# overridable per call; tracked in config_service (k8s_entra_group_{cid}). The
# ClusterRoleBinding is a fixed name per cluster; rebind delete-then-creates so a
# role change works (roleRef is immutable under `apply`).

_ENTRA_GROUP_BINDING_NAME = "entra-group-binding"


def _entra_group_bind_command(role: str, group_id: str) -> str:
    """Shell command that (re)binds the Entra group to ``role`` on the cluster:
    delete-then-create the fixed-name ClusterRoleBinding (so a role change applies —
    roleRef is immutable under apply) with a ``Group`` subject = the Entra Object ID.
    Prints ``ENTRA_BOUND_OK`` only when create succeeds."""
    q_name = shlex.quote(_ENTRA_GROUP_BINDING_NAME)
    q_role, q_gid = shlex.quote(role), shlex.quote(group_id)
    return (
        f"kubectl delete clusterrolebinding {q_name} --ignore-not-found 1>&2 || true; "
        f"kubectl create clusterrolebinding {q_name} --clusterrole={q_role} --group={q_gid} 1>&2 "
        "&& echo ENTRA_BOUND_OK"
    )


def _entra_group_unbind_command() -> str:
    """Shell command that removes the Entra-group ClusterRoleBinding (idempotent)."""
    q_name = shlex.quote(_ENTRA_GROUP_BINDING_NAME)
    return f"kubectl delete clusterrolebinding {q_name} --ignore-not-found 1>&2; echo ENTRA_UNBOUND_OK"


def _workforce_principalset(group_oid: str) -> str:
    """The GKE Workforce-Identity RBAC subject for an Entra group: the same group
    Object ID wrapped in the workforce-pool ``principalSet`` URI (GKE can't take a
    bare group id — its authenticator only knows workforce principals). Requires
    gcp_workforce_pool_id (set on Settings → Kubernetes)."""
    pool = _cfg("gcp_workforce_pool_id")
    if not pool:
        raise K8sError("no GCP workforce pool configured — set gcp_workforce_pool_id on "
                       "Settings (k8s panel) to federate GKE (the pool your Entra WIF "
                       "provider lives in)")
    loc = _cfg("gcp_workforce_location", "global") or "global"
    return (f"principalSet://iam.googleapis.com/locations/{loc}"
            f"/workforcePools/{pool}/group/{group_oid}")


async def bind_entra_group(db: Session, cluster_id: str, *, group_id: str = None,
                           role: str = None) -> dict:
    """Bind an Entra group (Object ID) to a ClusterRole on the cluster. group_id/role
    fall back to config (entra_rbac_group_id / entra_rbac_group_role, default
    cluster-admin). Idempotent — delete-then-create so a role change applies. The RBAC
    subject is cloud-aware: the bare group OID on EKS/AKS (their tokens carry the OID
    directly), the workforce ``principalSet`` URI on GKE (Workforce Identity
    Federation) — same Entra group either way."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    gid = (group_id or "").strip() or _cfg("entra_rbac_group_id")
    if not gid:
        raise K8sError("no Entra group configured — set entra_rbac_group_id on Settings "
                       "(k8s panel) or pass a group Object ID")
    role = (role or "").strip() or _cfg("entra_rbac_group_role", "cluster-admin")
    subject = _workforce_principalset(gid) if row.cloud == "gcp" else gid
    kubeconfig = resolve_kubeconfig(db, cluster_id)
    out = await _run_cluster_command(
        kubeconfig, _entra_group_bind_command(role, subject), target_cloud=row.cloud)
    if "ENTRA_BOUND_OK" not in (out or ""):
        logger.error("Entra group bind: no success sentinel (len=%d): %r",
                     len(out or ""), (out or "")[-800:])
        raise K8sError("failed to bind the Entra group to the cluster — see the job logs")
    from . import config_service
    config_service.set(f"k8s_entra_group_{cluster_id}", gid)
    config_service.set(f"k8s_entra_group_role_{cluster_id}", role)
    logger.info("Bound Entra group %s → %s on cluster %s", gid, role, row.name)
    return {"group_id": gid, "role": role}


async def unbind_entra_group(db: Session, cluster_id: str) -> dict:
    """Remove the cluster's Entra-group ClusterRoleBinding + clear its config keys
    (best-effort, idempotent)."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    from . import config_service
    if row is None or not config_service.get(f"k8s_entra_group_{cluster_id}"):
        return {"ok": True, "removed": False}
    try:
        await _run_cluster_command(
            resolve_kubeconfig(db, cluster_id), _entra_group_unbind_command(),
            target_cloud=row.cloud)
    except Exception as exc:
        logger.warning("Entra group unbind for %s failed (non-fatal): %s", cluster_id, exc)
    config_service.set(f"k8s_entra_group_{cluster_id}", "")
    config_service.set(f"k8s_entra_group_role_{cluster_id}", "")
    return {"ok": True, "removed": True}


async def run_group_binding(db: Session, *, cluster_id: str, job_id: str, action: str = "bind",
                            group_id: str = None, role: str = None) -> None:
    """Worker entry for a ``k8s_group_binding`` job: bind/unbind an Entra group to a
    ClusterRole on the cluster (runs as a job — the bind drives the cloud runner)."""
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        if action == "unbind":
            await broadcast_progress(job_id, 20, "Removing the Entra-group binding…")
            await unbind_entra_group(db, cluster_id)
        else:
            await broadcast_progress(job_id, 20, "Binding the Entra group to the cluster…")
            await bind_entra_group(db, cluster_id, group_id=group_id, role=role)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s Entra-group job failed cluster=%s action=%s", cluster_id, action)
        return
    job_service.set_completed(db, job_id)


# ── Entra OIDC federation → cluster trust (real-identity JIT) ─────────────────
#
# Make a cluster TRUST Entra as the token issuer so a user's own Entra token (not
# an impersonation subject, not the dashboard's cloud SP) authenticates and its
# group OIDs match the RBAC `Group` binding done by bind_entra_group. Per-cluster
# action, per cloud: EKS associates a shared Entra app as an OIDC identity provider
# (bare group OID subject, matching AKS); AKS is native (no-op); GKE uses Workforce
# Identity Federation + Connect Gateway (Phase 2). Enable state is tracked in
# config_service (k8s_entra_fed_{cid}); the EKS name+region are cached alongside
# (k8s_entra_fed_eks_{cid}) so disable works without re-reading the kubeconfig.

def _entra_oidc_settings() -> dict:
    """Resolve the shared Entra OIDC app settings for EKS federation. Issuer derives
    from azure_tenant_id when entra_oidc_issuer_url is unset. Raises when the client
    id (audience) or a resolvable issuer is missing."""
    client_id = _cfg("entra_oidc_client_id")
    if not client_id:
        raise K8sError("no Entra OIDC app configured — set entra_oidc_client_id on "
                       "Settings (k8s panel): the shared Entra app registration's "
                       "Application (client) ID")
    issuer = _cfg("entra_oidc_issuer_url")
    if not issuer:
        tenant = _cfg("azure_tenant_id")
        if not tenant:
            raise K8sError("no Entra issuer — set entra_oidc_issuer_url, or azure_tenant_id "
                           "to derive https://login.microsoftonline.com/<tenant>/v2.0")
        issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
    return {
        "client_id": client_id,
        "issuer": issuer,
        "username_claim": _cfg("entra_oidc_username_claim", "oid") or "oid",
        "groups_claim": _cfg("entra_oidc_groups_claim", "groups") or "groups",
    }


def _eks_name_region(kubeconfig: str) -> tuple:
    """Extract the EKS cluster name + region from a stored EKS kubeconfig's
    ``aws eks get-token`` exec args (robust for both provisioned and registered
    clusters). ("", "") when the kubeconfig isn't an EKS exec config."""
    try:
        cfg = yaml.safe_load(kubeconfig) or {}
        for u in (cfg.get("users") or []):
            exec_blk = (u.get("user") or {}).get("exec") or {}
            args = exec_blk.get("args") or []
            if exec_blk.get("command") == "aws" and "get-token" in args:
                def _arg(flag: str) -> str:
                    return args[args.index(flag) + 1] if (flag in args and args.index(flag) + 1 < len(args)) else ""
                return _arg("--cluster-name"), _arg("--region")
    except Exception:
        pass
    return "", ""


async def enable_entra_federation(db: Session, cluster_id: str) -> dict:
    """Make the cluster TRUST Entra so users authenticate as themselves. EKS: associate
    a shared Entra app as the cluster's OIDC identity provider (async on AWS's side —
    the worker polls to ACTIVE). AKS: native (no-op). GKE: Phase 2 (Workforce Identity
    Federation). Records enable state in config_service."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    from . import config_service
    if row.cloud == "azure":
        config_service.set(f"k8s_entra_fed_{cluster_id}", "1")
        return {"cloud": "azure", "native": True, "status": "ACTIVE"}
    if row.cloud == "aws":
        oidc = _entra_oidc_settings()
        name, region = _eks_name_region(resolve_kubeconfig(db, cluster_id))
        if not name:
            raise K8sError("could not determine the EKS cluster name from its kubeconfig "
                           "— is this an EKS cluster with an `aws eks get-token` kubeconfig?")
        region = region or _cfg("aws_region")
        from . import aws_service
        res = await asyncio.to_thread(
            aws_service.associate_eks_oidc, name, region,
            issuer_url=oidc["issuer"], client_id=oidc["client_id"],
            username_claim=oidc["username_claim"], groups_claim=oidc["groups_claim"])
        config_service.set(f"k8s_entra_fed_{cluster_id}", "1")
        config_service.set(f"k8s_entra_fed_eks_{cluster_id}", f"{name}|{region}")
        logger.info("Enabled Entra OIDC federation on EKS cluster %s (region %s, already=%s)",
                    name, region, res.get("already"))
        return {"cloud": "aws", "eks_cluster": name, "region": region, **res}
    if row.cloud == "gcp":
        # GKE: Workforce Identity Federation + Connect Gateway. Register the cluster
        # to the fleet, enable the Connect Gateway APIs, and grant the workforce
        # group's principalSet the gkehub.gateway* roles. The RBAC binding (the
        # principalSet ClusterRoleBinding) is applied by the separate Entra-group action.
        from . import gcp_service
        project = _cfg("gcp_project_id")
        if not project:
            raise K8sError("gcp_project_id is not configured")
        gid = _cfg("entra_rbac_group_id")
        if not gid:
            raise K8sError("no Entra group configured — set entra_rbac_group_id on Settings "
                           "(k8s panel); GKE grants the Connect Gateway IAM to that group")
        principal = _workforce_principalset(gid)   # also validates gcp_workforce_pool_id
        name, location = await asyncio.to_thread(
            gcp_service.find_gke_cluster, _gke_name(f"k8s-{row.name}"), project)
        await asyncio.to_thread(gcp_service.enable_connect_gateway_apis, project)
        membership = await asyncio.to_thread(
            gcp_service.register_fleet_membership, project, location, name)
        await asyncio.to_thread(gcp_service.grant_gateway_iam, principal, project)
        config_service.set(f"k8s_entra_fed_{cluster_id}", "1")
        config_service.set(f"k8s_entra_fed_gke_{cluster_id}", membership)
        logger.info("Enabled GKE WIF federation for %s (membership %s, principalSet=%s)",
                    name, membership, principal)
        return {"cloud": "gcp", "membership": membership, "location": location,
                "principal_set": principal, "status": "ACTIVE"}
    raise K8sError(f"Entra federation is not supported for {row.cloud} clusters")


async def disable_entra_federation(db: Session, cluster_id: str) -> dict:
    """Remove the cluster's Entra trust (idempotent, best-effort). EKS: disassociate
    the OIDC IdP. AKS: native (no-op). Clears the enable state."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    from . import config_service
    if row is None or not config_service.get(f"k8s_entra_fed_{cluster_id}"):
        return {"ok": True, "removed": False}
    if row.cloud == "aws":
        try:
            cached = config_service.get(f"k8s_entra_fed_eks_{cluster_id}") or ""
            if "|" in cached:
                name, region = cached.split("|", 1)
            else:
                name, region = _eks_name_region(resolve_kubeconfig(db, cluster_id))
                region = region or _cfg("aws_region")
            from . import aws_service
            await asyncio.to_thread(aws_service.disassociate_eks_oidc, name, region)
        except Exception as exc:
            logger.warning("EKS OIDC disassociate for %s failed (non-fatal): %s", cluster_id, exc)
    elif row.cloud == "gcp":
        # Revoke the workforce group's Connect Gateway IAM. Leave the fleet membership
        # in place (harmless, and re-enable reuses it — deregistering is slow).
        try:
            gid = _cfg("entra_rbac_group_id")
            if gid:
                from . import gcp_service
                await asyncio.to_thread(
                    gcp_service.revoke_gateway_iam, _workforce_principalset(gid), _cfg("gcp_project_id"))
        except Exception as exc:
            logger.warning("GKE gateway IAM revoke for %s failed (non-fatal): %s", cluster_id, exc)
    config_service.set(f"k8s_entra_fed_{cluster_id}", "")
    config_service.set(f"k8s_entra_fed_eks_{cluster_id}", "")
    config_service.set(f"k8s_entra_fed_gke_{cluster_id}", "")
    return {"ok": True, "removed": True}


def _entra_oidc_login_kubeconfig(kubeconfig: str, oidc: dict) -> str:
    """Pure transform: replace every user's exec block with int128 ``kubectl
    oidc-login`` against the shared Entra app (``oidc`` from _entra_oidc_settings).
    Token-free — no static credential (token / client-key-data) is written; the CA
    and cluster ``server`` are left untouched (repoint happens before this).

    Uses the ``device-code`` grant (not the default authcode browser flow) so the
    downloaded file works unchanged on Entra-joined, cross-tenant and headless
    machines: the authcode flow SSOs the operator into the *machine's own* tenant,
    which is the wrong one for a lab/demo tenant. Device-code prints a URL + code the
    operator completes in any browser (InPrivate) as the correct account. Requires
    the Entra app's public-client flows (documented in the federation guide §1a)."""
    cfg = yaml.safe_load(kubeconfig) or {}
    exec_args = [
        "oidc-login", "get-token",
        f"--oidc-issuer-url={oidc['issuer']}",
        f"--oidc-client-id={oidc['client_id']}",
        "--oidc-extra-scope=openid", "--oidc-extra-scope=email", "--oidc-extra-scope=profile",
        "--grant-type=device-code",
    ]
    for u in (cfg.get("users") or []):
        u["user"] = {"exec": {
            "apiVersion": "client.authentication.k8s.io/v1beta1",
            "command": "kubectl",
            "args": exec_args,
            "interactiveMode": "IfAvailable",
        }}
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


def _connect_gateway_kubeconfig(context_name: str, server: str) -> str:
    """A token-free Connect Gateway kubeconfig for GKE: server = the public gateway
    URL, auth = ``gke-gcloud-auth-plugin`` (picks up the active gcloud/workforce
    identity). No CA (the gateway serves a public cert). The user runs
    ``gcloud auth login --login-config`` as their Entra-federated workforce identity
    first; kubectl then reaches the private cluster through the gateway."""
    cfg = {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": context_name, "cluster": {"server": server}}],
        "contexts": [{"name": context_name,
                      "context": {"cluster": context_name, "user": context_name}}],
        "current-context": context_name,
        "users": [{"name": context_name, "user": {"exec": {
            "apiVersion": "client.authentication.k8s.io/v1beta1",
            "command": "gke-gcloud-auth-plugin",
            "provideClusterInfo": True,
            "installHint": ("Install the gke-gcloud-auth-plugin: "
                            "https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl"),
        }}}],
    }
    return yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)


def build_entra_oidc_kubeconfig(db: Session, cluster_id: str) -> str:
    """Return a token-free kubeconfig for real-identity access, authenticating as the
    USER's own Entra identity (not the dashboard's). EKS: the stored kubeconfig
    repointed at the API tunnel with the exec block replaced by ``kubectl oidc-login``
    (int128 kubelogin) against the shared Entra app. AKS: the existing kubelogin
    (Azure) kubeconfig repointed at the tunnel — already Entra. GKE: a **Connect
    Gateway** kubeconfig (NOT the tunnel) — the user's workforce identity reaches the
    private cluster through the gateway."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise K8sError(f"cluster {cluster_id} not found")
    if row.cloud == "gcp":
        from . import config_service, gcp_service
        project = _cfg("gcp_project_id")
        membership = (config_service.get(f"k8s_entra_fed_gke_{cluster_id}")
                      or _gke_name(f"k8s-{row.name}"))
        try:
            server = gcp_service.connect_gateway_server_url(membership, project, "global")
        except Exception as exc:
            raise K8sError(f"could not build the Connect Gateway URL: {exc}")
        return _connect_gateway_kubeconfig(row.name, server)
    local_port = int(_cfg("k8s_api_tunnel_local_port", "6443"))
    kubeconfig = _repoint_kubeconfig_to_tunnel(resolve_kubeconfig(db, cluster_id), local_port)
    if row.cloud == "azure":
        return kubeconfig  # AKS kubelogin exec is already the user's Entra identity
    return _entra_oidc_login_kubeconfig(kubeconfig, _entra_oidc_settings())


async def run_entra_federation(db: Session, *, cluster_id: str, job_id: str,
                               action: str = "enable") -> None:
    """Worker entry for a ``k8s_entra_federation`` job: enable/disable the cluster's
    Entra trust. The EKS associate is asynchronous on AWS's side (several minutes,
    cluster UPDATING) — poll to ACTIVE with a heartbeat so it isn't mistaken for a
    hang. Runs in the worker (no HTTP timeout)."""
    from . import job_service, aws_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    try:
        if action == "disable":
            await broadcast_progress(job_id, 20, "Disabling Entra federation…")
            await disable_entra_federation(db, cluster_id)
            job_service.set_completed(db, job_id)
            return
        await broadcast_progress(job_id, 15, "Enabling Entra federation…")
        res = await enable_entra_federation(db, cluster_id)
        if res.get("cloud") == "aws" and res.get("status") != "ACTIVE":
            name, region = res.get("eks_cluster"), res.get("region")
            await broadcast_progress(
                job_id, 40,
                "Associating the Entra OIDC provider — EKS makes this ACTIVE in a few minutes…")
            for i in range(60):  # ~10 min at 10s
                status = await asyncio.to_thread(aws_service.describe_eks_oidc_status, name, region)
                if status == "ACTIVE":
                    break
                await broadcast_progress(job_id, min(40 + i, 95),
                                         f"Waiting for the OIDC provider to become ACTIVE… ({status or 'pending'})")
                await asyncio.sleep(10)
            else:
                raise K8sError("EKS OIDC provider did not reach ACTIVE in time — check the "
                               "cluster's identity-provider config on AWS and retry")
        await broadcast_progress(job_id, 100, "Entra federation enabled.")
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("k8s Entra-federation job failed cluster=%s action=%s", cluster_id, action)
        return
    job_service.set_completed(db, job_id)


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


async def register_rancher_ui_web_jump(db: Session) -> dict:
    """Ensure a PRA **Web Jump** to the central Rancher UI exists — created ONCE
    and reused by every cluster's console (they all live in the same Rancher UI).
    Idempotent: returns the stored id if already provisioned. Requires the Rancher
    node running + PRA configured. OPT-IN (``rancher_ui_web_jump_enabled``): lets an
    operator whose IP isn't in ``rancher_allowed_source_cidrs`` reach the node's UI
    from the PRA representative console (brokered/recorded, no CIDR change).

    Re-ensures the dashboard-managed Jumpoint host and refreshes the node firewall
    on EVERY call (not just first provisioning): AWS/GCP jumpoint egress IPs are
    ephemeral, so a host reclaim/recreate changes the IP — re-syncing here keeps the
    firewall's jumpoint /32 current even when the Web Jump itself already exists."""
    from . import config_service, jumpoint_host_service, rancher_node_service, terraform_pra_service as pra
    server_url = _cfg("rancher_server_url")
    if not server_url:
        raise K8sError("Rancher node is not running (no rancher_server_url)")
    if not _pra_configured():
        raise K8sError("PRA is not configured (bt_api_host / bt_client_id / bt_jumpoint_name)")
    # Requirement 2: auto-manage the IP the Web Jump uses to reach the node. A Web
    # Jump connects THROUGH a Jumpoint host, so the source hitting the firewall is
    # that host's egress IP. Ensure the dashboard-managed Jumpoint is up, capture its
    # (possibly changed) egress IP, and refresh the firewall so its /32 is allowed —
    # BEFORE the reused early-return, so an ephemeral AWS/GCP jumpoint IP is re-synced
    # on every console open. Best-effort — a pre-existing operator Jumpoint can't be
    # auto-detected (manual CIDR then).
    try:
        await jumpoint_host_service.ensure_rancher_ui_jumpoint()
    except Exception as exc:
        logger.warning("Rancher UI web-jump: jumpoint egress capture failed (non-fatal): %s", exc)
    try:
        await rancher_node_service.refresh_rancher_firewall(db)
    except Exception as exc:
        logger.warning("Rancher UI web-jump: firewall refresh failed (non-fatal): %s", exc)

    existing = _cfg("rancher_ui_web_jump_id")
    if existing:
        return {"web_jump_id": existing, "reused": True}
    jump_group = _cfg("rancher_ui_jump_group") or _cfg("bt_jump_group_name")
    jumpoint = _cfg("rancher_ui_jumpoint_name") or _cfg("bt_jumpoint_name")
    result = await pra.provision_web_jump(
        name="rancher-ui", url=server_url,
        jump_group_name=jump_group, jumpoint_name=jumpoint,
        verify_certificate=config_service.get_bool("rancher_ui_verify_certificate", False))
    config_service.set("rancher_ui_web_jump_id", str(result.get("web_jump_id") or ""))
    if result.get("tf_state_json"):
        config_service.set("rancher_ui_web_jump_tfstate", result["tf_state_json"])
    return {"web_jump_id": result.get("web_jump_id"), "jump_group": jump_group,
            "jumpoint": jumpoint, "reused": False}


async def remove_rancher_ui_web_jump() -> None:
    """Destroy the Rancher-UI PRA Web Jump (best-effort) and clear its config.
    Called from Rancher node teardown."""
    from . import config_service, terraform_pra_service as pra
    state = _cfg("rancher_ui_web_jump_tfstate")
    if state:
        try:
            await pra.remove_web_jump(state)
        except Exception as exc:
            logger.warning("Rancher UI web-jump removal failed (non-fatal): %s", exc)
    config_service.set("rancher_ui_web_jump_id", "")
    config_service.set("rancher_ui_web_jump_tfstate", "")


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

    # Rancher UI reachability. The node is publicly reachable at its
    # source-restricted server-url, so by default we just hand back the direct
    # deep-link. When rancher_ui_web_jump_enabled is set, ALSO broker it via an
    # opt-in PRA Web Jump (created once, reused by every cluster's console) so an
    # operator not in the CIDR allowlist can reach it through the rep console. The
    # tunnel_type=k8s jump below stays for raw kubectl access.
    if row.mgmt_kind == "rancher" and _cfg("rancher_server_url"):
        server_url = _cfg("rancher_server_url")
        out["rancher_ui"] = {
            "console_deeplink": row.mgmt_endpoint or server_url,
            "server_url": server_url,
            "note": "Reach Rancher directly at its source-restricted server-url.",
        }
        if config_service.get_bool("rancher_ui_web_jump_enabled", False) and _pra_configured():
            try:
                wj = await register_rancher_ui_web_jump(db)
                out["rancher_ui"]["web_jump_id"] = wj.get("web_jump_id")
                out["rancher_ui"]["note"] = (
                    "Reach Rancher at its source-restricted server-url, or open the 'rancher-ui' "
                    "Web Jump from the BeyondTrust PRA representative console for brokered/recorded access.")
            except Exception as exc:
                logger.warning("Rancher UI web-jump provisioning failed for %s: %s", cluster_id, exc)
                out["rancher_ui_error"] = str(exc)

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
