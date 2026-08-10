"""Password Safe registration for a cluster's PRA ServiceAccount token.

Today the token `k8s_service._mint_pra_sa_token` creates is vaulted in PRA and lives
forever. This module makes it a Password Safe **managed account** on the "Kubernetes
Service Account Token" custom plugin, so Password Safe rotates it on a schedule — and
registers a second managed account on the "PRA Vault Token" plugin so each rotation can
be mirrored into the PRA Vault copy a brokered session injects
(``services/k8s_token_sync.py`` drives that).

Two things about the plugin shape the whole design, and both are easy to get wrong:

* **The rotation sweep is label-scoped.** It deletes only Secrets carrying
  ``beyondtrust.com/managed-by=password-safe``. The dashboard's ``<sa>-token`` Secret
  has an annotation and no labels, so it is never swept and stays valid forever. That is
  not a broken PRA copy — it is a permanent, unrotatable, unaudited cluster-admin bearer
  token, which defeats the only reason to choose the revoking token mode. Registration
  therefore DELETES that Secret, and only after the PRA copy carries a
  Password-Safe-issued token.
* **Every token is bound to the ServiceAccount's uid.** Delete and recreate the SA and
  every token Password Safe has issued dies with it, which is why
  ``deregister_pra_tunnel`` skips its ServiceAccount revoke while a registration exists.

The operator owns the Password Safe side that holds credentials: the platform (import
the .psplugin) and the functional account (a cloud IAM identity per cloud, or a
bootstrap ServiceAccount for generic clusters). The dashboard only ever references it by
name, so no cloud secret passes through here.

See docs/design/k8s-sa-token-rotation.md.
"""
import asyncio
import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from ..database import Job, K8sCluster

logger = logging.getLogger(__name__)

VALID_PS_TOKEN_ACTIONS = ("register", "deregister", "rotate")
VALID_PS_TOKEN_MODES = ("longlived", "bound")

# The API server's own floor for a TokenRequest lifetime. The plugin's address parser
# only requires ttl > 0, but a cluster silently caps below this, so clamp what we build.
_MIN_BOUND_TTL = 600


class PSK8sTokenError(Exception):
    """Raised when Password Safe token registration cannot proceed."""


def _cfg(key: str, default: str = "") -> str:
    """config_service → settings → default. Fourth local copy of this resolver,
    matching ps_api_service / ps_resource_service / ps_vm_hook."""
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or default


def _cfg_bool(key: str, default: bool = False) -> bool:
    try:
        from . import config_service
        return config_service.get_bool(key, default)
    except Exception:
        from ..config import settings
        val = getattr(settings, key, default)
        return bool(val)


def _cfg_int(key: str, default: int) -> int:
    raw = str(_cfg(key, "")).strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def enabled() -> bool:
    return _cfg_bool("k8s_ps_token_rotation_enabled", False)


# ── cluster identity → the plugin's managed-system address ────────────────────

def _deploy_tf_variables(db: Session, row: K8sCluster) -> dict:
    """The ``-var`` set the cluster was provisioned with, from its deploy job.

    This is where the address parts that are not columns come from: the real cloud
    cluster name (``row.name`` is a dashboard label and is slugged before it reaches the
    cloud), the AKS resource group, and whether a GKE cluster is zonal. A registered
    cluster has no deploy job, so those callers must supply them on the request."""
    if not row.deploy_job_id:
        return {}
    job = db.query(Job).filter(Job.id == row.deploy_job_id).first()
    if job is None:
        return {}
    return (job.metadata_dict or {}).get("tf_variables") or {}


def build_address(*, cloud: str, region: str = "", cluster_name: str = "",
                  subscription_id: str = "", resource_group: str = "",
                  project_id: str = "", location: str = "", api_server: str = "",
                  mode: str = "longlived", ttl_seconds: int = 0,
                  namespace: str = "", extra_options: str = "") -> str:
    """The managed system address the "Kubernetes Service Account Token" plugin parses.

        eks;<region>;<cluster>
        aks;<subscriptionId>;<resourceGroup>;<cluster>
        gke;<projectId>;<location>;<cluster>
        k8s;<apiServerUrl>

    Options are appended in a stable order so the same inputs always produce the same
    address (the sync compares it, and an unstable one would look like drift).
    ``ps_resource_service._validate_k8ssa_dns_name`` is the authority on what parses —
    this only assembles.

    OCI/OKE and on-prem both go down the generic ``k8s;`` path: the plugin has no OCI
    provider, so there is nothing to gain from a fourth cloud branch."""
    c = (cloud or "").strip().lower()
    if c == "aws":
        if not (region and cluster_name):
            raise PSK8sTokenError(
                "an EKS address needs a region and the EKS cluster name")
        parts = ["eks", region, cluster_name]
    elif c == "azure":
        if not (subscription_id and resource_group and cluster_name):
            raise PSK8sTokenError(
                "an AKS address needs a subscription id, a resource group and the AKS "
                "cluster name — the resource group is not stored on the cluster row, so "
                "supply it on the request for a registered cluster")
        parts = ["aks", subscription_id, resource_group, cluster_name]
    elif c == "gcp":
        if not (project_id and location and cluster_name):
            raise PSK8sTokenError(
                "a GKE address needs a project id, a location and the GKE cluster name")
        parts = ["gke", project_id, location, cluster_name]
    else:
        if not api_server:
            raise PSK8sTokenError(
                "a generic (k8s;) address needs the API server URL — the cluster row has "
                "no api_server and none could be parsed from its kubeconfig")
        parts = ["k8s", api_server]

    mode = (mode or "longlived").strip().lower()
    if mode not in VALID_PS_TOKEN_MODES:
        raise PSK8sTokenError(
            f"unknown token mode {mode!r} (expected one of {', '.join(VALID_PS_TOKEN_MODES)})")
    parts.append(mode)
    if mode == "bound":
        parts.append(f"ttl={max(int(ttl_seconds or 0), _MIN_BOUND_TTL)}")
    if namespace:
        # So a Managed Account name given without a namespace prefix still resolves.
        parts.append(f"ns={namespace}")
    for opt in (extra_options or "").split(";"):
        if opt.strip():
            parts.append(opt.strip())
    return ";".join(parts)


def _address_for(db: Session, row: K8sCluster, *, mode: str, ttl_seconds: int,
                 namespace: str, cluster_name: str = "", resource_group: str = "",
                 location: str = "") -> str:
    """build_address with every part resolved from the row, its deploy job and config."""
    tf = _deploy_tf_variables(db, row)
    cloud = (row.cloud or "").strip().lower()
    name = cluster_name or tf.get("cluster_name") or _cfg(f"k8s_ps_cluster_name_{row.id}")
    if cloud == "gcp":
        # A zonal cluster's location is its ZONE and a regional cluster's is its region;
        # mixing them is the documented cause of a GKE 404 on the cluster lookup.
        loc = location or tf.get("zone") or tf.get("region") or row.region or ""
        return build_address(
            cloud=cloud, project_id=tf.get("project") or _cfg("gcp_project_id"),
            location=loc, cluster_name=name, mode=mode, ttl_seconds=ttl_seconds,
            namespace=namespace, extra_options=_cfg("k8s_ps_token_address_options"))
    if cloud == "azure":
        rg = (resource_group or tf.get("resource_group_name")
              or _cfg(f"k8s_ps_resource_group_{row.id}"))
        return build_address(
            cloud=cloud, subscription_id=_cfg("azure_subscription_id"),
            resource_group=rg, cluster_name=name,
            location=location or tf.get("location") or row.region or "",
            mode=mode, ttl_seconds=ttl_seconds, namespace=namespace,
            extra_options=_cfg("k8s_ps_token_address_options"))
    if cloud == "aws":
        return build_address(
            cloud=cloud, region=tf.get("region") or row.region or _cfg("aws_region"),
            cluster_name=name, mode=mode, ttl_seconds=ttl_seconds, namespace=namespace,
            extra_options=_cfg("k8s_ps_token_address_options"))
    return build_address(
        cloud=cloud, api_server=row.api_server or "", mode=mode,
        ttl_seconds=ttl_seconds, namespace=namespace,
        extra_options=_cfg("k8s_ps_token_address_options"))


# ── per-cluster state ─────────────────────────────────────────────────────────

def _state_key(cluster_id: str) -> str:
    return f"ps_k8s_token_{cluster_id}"


def get_state(cluster_id: str) -> dict:
    """The registration's non-column state: the managed system ids and the scrubbed
    Terraform state that drives deregister. Churning/bulky fields stay out of the
    schema, matching the ``k8s_api_tunnel_state_{id}`` precedent."""
    try:
        from . import config_service
        raw = config_service.get(_state_key(cluster_id))
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(cluster_id: str, **updates) -> dict:
    state = get_state(cluster_id)
    state.update({k: v for k, v in updates.items() if v is not None})
    from . import config_service
    config_service.set(_state_key(cluster_id), json.dumps(state))
    return state


def _clear_state(cluster_id: str) -> None:
    try:
        from . import config_service
        config_service.delete(_state_key(cluster_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("clearing PS token state for %s failed: %s", cluster_id, exc)


def token_mode(cluster_id: str) -> str:
    return (_cfg(f"k8s_ps_token_mode_{cluster_id}")
            or get_state(cluster_id).get("token_mode")
            or _cfg("k8s_ps_token_mode", "longlived")).strip().lower()


# ── Password Safe lookups ─────────────────────────────────────────────────────

def _functional_account_name(cloud: str) -> str:
    c = (cloud or "").strip().lower()
    per_cloud = {"aws": "k8s_ps_functional_account_aws",
                 "azure": "k8s_ps_functional_account_azure",
                 "gcp": "k8s_ps_functional_account_gcp"}.get(c,
                                                             "k8s_ps_functional_account_local")
    return _cfg(per_cloud) or _cfg("k8s_ps_functional_account_local")


async def _resolve_functional_account(name: str, *platform_tokens) -> dict:
    """The functional account, with its platform sanity-checked.

    Reuses ``ps_vm_hook._platform_name_ok`` — an all-tokens substring match rather than a
    contiguous one, because a Password Safe admin renaming an imported plugin platform is
    a real event that once silently switched VM onboarding off ("Azure VM SSH Rotation" →
    "Azure Waagent VM SSH Rotation")."""
    if not name:
        raise PSK8sTokenError(
            "no Password Safe functional account is configured for this cloud — set "
            "k8s_ps_functional_account_{aws,azure,gcp,local}")
    from . import ps_api_service, ps_vm_hook
    fa = await ps_api_service.get_functional_account(name)
    pname = fa.get("platform_name") or ""
    if pname and not ps_vm_hook._platform_name_ok(pname, *platform_tokens):
        raise PSK8sTokenError(
            f"functional account {name!r} is on platform {pname!r}, which is not a "
            f"{' + '.join(platform_tokens)} platform — the managed system inherits the "
            f"functional account's platform, so this would onboard against the wrong plugin")
    return fa


def _workgroup() -> str:
    return _cfg("k8s_ps_workgroup") or _cfg("passwordsafe_workgroup")


# ── register ──────────────────────────────────────────────────────────────────

async def register(db: Session, cluster_id: str, *, job_id: str = "",
                   mode: Optional[str] = None, ttl_seconds: Optional[int] = None,
                   namespace: Optional[str] = None, service_account: Optional[str] = None,
                   cluster_name: str = "", resource_group: str = "", location: str = "",
                   functional_account: str = "",
                   change_on_register: Optional[bool] = None,
                   mirror_to_pra: Optional[bool] = None) -> dict:
    """Make the cluster's ServiceAccount token a Password Safe managed account.

    Ordered, and the order is the correctness argument:

      1. apply the rotator RBAC, so the functional account can actually rotate;
      2. create the managed system + account, seeded with the token the cluster is
         using right now — so Password Safe starts out holding a working credential
         instead of a placeholder it would have to rotate before anything works;
      3. create the "PRA Vault Token" mirror, also seeded, when there is a PRA Vault
         account to keep in sync. Before the rotation, because it is where the rotated
         value gets pushed;
      4. rotate once, which proves the whole path (functional-account credentials →
         cloud control plane → API server → RBAC → Secret create) at registration time
         rather than at 3am on the first scheduled rotation;
      5. mirror the new token into PRA — **fatal**, because stopping here leaves the
         token managed but not mirrored;
      6. delete the dashboard's ``<sa>-token`` Secret, which the plugin's label-scoped
         sweep would never remove.

    Idempotent: an already-registered cluster re-applies the RBAC and returns."""
    if not enabled():
        raise PSK8sTokenError(
            "Password Safe token rotation is disabled — enable k8s_ps_token_rotation_enabled")
    from . import k8s_service, ps_api_service, ps_resource_service
    from ..api.websocket import broadcast_progress

    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise PSK8sTokenError(f"cluster {cluster_id} not found")
    if not ps_api_service.configured():
        raise PSK8sTokenError(
            "Password Safe is not configured — set pscli_api_url, pscli_client_id, "
            "pscli_client_secret and pscli_api_account_name")

    warnings: list = []
    mode = (mode or token_mode(cluster_id)).strip().lower()
    if mode not in VALID_PS_TOKEN_MODES:
        raise PSK8sTokenError(f"unknown token mode {mode!r}")
    ttl = int(ttl_seconds or _cfg_int("k8s_ps_token_ttl_seconds", 3600))
    ns = namespace or _cfg("pra_k8s_namespace", "pra-access")
    sa = service_account or _cfg("pra_k8s_sa_name", "pra-access")
    account_name = f"{ns}/{sa}"

    if row.ps_token_account_id:
        note = await _apply_rbac(db, cluster_id, mode=mode, warnings=warnings)
        return {"already_registered": True, "managed_account_id": row.ps_token_account_id,
                "rbac": note, "warnings": warnings}

    address = _address_for(db, row, mode=mode, ttl_seconds=ttl, namespace=ns,
                           cluster_name=cluster_name, resource_group=resource_group,
                           location=location)
    ps_resource_service._validate_k8ssa_dns_name(address)

    # 1. RBAC first — a managed system whose functional account cannot rotate is worse
    #    than no managed system, because it looks registered.
    if job_id:
        await broadcast_progress(job_id, 15, "Applying the rotator RBAC…")
    rbac_note = await _apply_rbac(db, cluster_id, mode=mode, warnings=warnings)

    # 2. The token the cluster is using right now.
    if job_id:
        await broadcast_progress(job_id, 30, "Reading the current ServiceAccount token…")
    kubeconfig = k8s_service.resolve_kubeconfig(db, cluster_id)
    current, _src = await k8s_service._resolve_pra_sa_token(db, row, kubeconfig)

    fa = await _resolve_functional_account(
        functional_account or _functional_account_name(row.cloud),
        "kubernetes", "service account")
    platform_id = await ps_api_service.get_platform_id(
        _cfg("k8s_ps_token_platform", "Kubernetes Service Account Token"))
    workgroup_id = await ps_api_service.get_workgroup_id(_workgroup())

    if job_id:
        await broadcast_progress(job_id, 45, "Creating the Password Safe managed system…")
    reg = await ps_resource_service.register_managed_system(
        name=f"k8s-{row.name}", host_name=f"k8s-{row.name}",
        functional_account_id=fa["id"], platform_id=platform_id,
        workgroup_id=workgroup_id, ip_address="127.0.0.1", port=443,
        managed_account_name=account_name, method="k8ssa", dns_name=address,
        initial_password=current)
    row.ps_token_account_id = str(reg.get("managed_account_id") or "")
    _save_state(cluster_id, system_id=str(reg.get("managed_system_id") or ""),
                tf_state=reg.get("tf_state_json"), token_mode=mode, address=address,
                account_name=account_name)
    db.commit()

    # 3. The PRA Vault mirror — only meaningful when there IS a PRA Vault copy.
    mirror = (_cfg_bool("k8s_ps_pravault_mirror_enabled", True)
              if mirror_to_pra is None else bool(mirror_to_pra))
    vault_name = f"k8s-{row.name}-sa"
    mirrored = False
    if mirror and row.pra_vault_account_id:
        if job_id:
            await broadcast_progress(job_id, 60, "Registering the PRA Vault Token mirror…")
        try:
            mirrored = await _register_pravault_mirror(
                db, row, vault_account_name=vault_name, initial_password=current)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal on its own: the k8s account is still rotatable, PRA just stops
            # tracking. Mirrors provision_k8s_tunnel's best-effort vault half.
            warnings.append(f"PRA Vault Token mirror registration failed: {exc}")
            logger.warning("PS token: PRA mirror for %s failed: %s", cluster_id, exc)
    elif mirror and not row.pra_vault_account_id:
        warnings.append(
            "no PRA Vault account exists for this cluster (register the PRA k8s tunnel "
            "with credential injection first), so rotations will not be mirrored into PRA")
    del current

    # 4/5/6. Rotate, mirror the result, then retire the unsweepable Secret.
    rotated = False
    change = (_cfg_bool("k8s_ps_token_change_on_register", True)
              if change_on_register is None else bool(change_on_register))
    if change:
        if job_id:
            await broadcast_progress(job_id, 75, "Rotating the token once to prove the path…")
        await ps_api_service.change_managed_account_password(int(row.ps_token_account_id))
        rotated = True

    legacy_deleted = False
    if rotated and mirrored:
        if job_id:
            await broadcast_progress(job_id, 85, "Mirroring the new token into PRA Vault…")
        # FATAL on failure: the alternative is reporting success with the token managed
        # but not mirrored, which is exactly the hole this feature exists to close.
        await ps_api_service.rotate_pra_vault_token(
            source_account_id=int(row.ps_token_account_id),
            target_account_id=int(row.ps_pra_vault_account_id),
            duration_min=_cfg_int("k8s_ps_token_checkout_duration_min", 15),
            expect_target_platform=_cfg("k8s_ps_pravault_token_platform", "PRA Vault Token"))
        if _cfg_bool("k8s_ps_token_delete_legacy_secret", True):
            if job_id:
                await broadcast_progress(job_id, 92, "Removing the unmanaged token Secret…")
            try:
                await k8s_service.delete_legacy_pra_token_secret(db, cluster_id)
                legacy_deleted = True
            except Exception as exc:  # noqa: BLE001
                # Non-fatal, but say so plainly: the system works and carries a known
                # residual risk, which is different from the step not mattering.
                warnings.append(
                    f"could not delete the dashboard-minted {sa}-token Secret ({exc}) — it is "
                    f"NOT swept by the rotation plugin (which selects on its own labels), so "
                    f"it remains a cluster-admin token nothing rotates. Delete it by hand.")
    elif rotated and not mirrored:
        warnings.append(
            f"rotated, but with no PRA Vault mirror the {sa}-token Secret was left in place "
            f"so PRA keeps working — rotation will not revoke anything until the mirror exists")

    _save_state(cluster_id, rotated=rotated, legacy_secret_deleted=legacy_deleted,
                rbac=rbac_note)
    logger.info("PS token registration for cluster %s: account=%s mirror=%s rotated=%s "
                "legacy_deleted=%s", row.name, row.ps_token_account_id, mirrored, rotated,
                legacy_deleted)
    return {"managed_system_id": get_state(cluster_id).get("system_id"),
            "managed_account_id": row.ps_token_account_id,
            "pravault_account_id": row.ps_pra_vault_account_id or "",
            "address": address, "token_mode": mode, "rbac": rbac_note,
            "rotated": rotated, "pra_mirrored": mirrored,
            "legacy_secret_deleted": legacy_deleted, "warnings": warnings}


async def _apply_rbac(db: Session, cluster_id: str, *, mode: str, warnings: list) -> str:
    """Apply the rotator RBAC. Non-fatal: Password Safe's own Verify Functional Account
    names every missing verb and prints the ClusterRole, so a failure here is
    diagnosable, whereas refusing to register would leave nothing to verify."""
    if not _cfg_bool("k8s_ps_rotator_apply_rbac", True):
        return "skipped (k8s_ps_rotator_apply_rbac is off)"
    from . import k8s_service
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    subject = ""
    if row is not None and (row.cloud or "").lower() == "gcp" and not _cfg(
            "k8s_ps_rotator_gke_sa_email"):
        # GKE is the one cloud whose subject IS the functional account's own name.
        try:
            from . import ps_api_service
            fa = await ps_api_service.get_functional_account(
                _functional_account_name(row.cloud))
            subject = (fa.get("account_name") or "").split("impersonate:")[-1]
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"could not derive the GKE rotator subject: {exc}")
    if row is not None and (row.cloud or "").lower() == "aws":
        await _ensure_eks_access_entry(db, row, warnings)
    try:
        return await k8s_service.apply_ps_rotator_rbac(
            db, cluster_id, mode=mode, subject_name=subject)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"rotator RBAC apply failed: {exc}")
        logger.warning("PS token: rotator RBAC for %s failed: %s", cluster_id, exc)
        return f"failed: {exc}"


async def _ensure_eks_access_entry(db: Session, row: K8sCluster, warnings: list) -> None:
    """Map the functional account's IAM principal into the cluster.

    On EKS the rotator ClusterRoleBinding's ``User`` subject is whatever username the
    access entry maps the principal to — without the entry the binding matches nothing
    and the API server 401s, a failure invisible from inside the cluster. The principal
    ARN cannot be derived (mapping an access key id to an ARN needs
    ``sts:GetCallerIdentity`` *as that key*, and the secret lives only in Password
    Safe), so it comes from config; when unset, the exact CLI command goes in the
    warnings instead. Never touches aws-auth."""
    arn = _cfg("k8s_ps_rotator_eks_principal_arn")
    username = _cfg("k8s_ps_rotator_eks_username", "passwordsafe-rotator")
    tf = _deploy_tf_variables(db, row)
    eks_name = tf.get("cluster_name") or _cfg(f"k8s_ps_cluster_name_{row.id}") or row.name
    if not arn:
        warnings.append(
            "k8s_ps_rotator_eks_principal_arn is not set, so no EKS access entry was "
            "created — if the functional account's principal has none, create it: "
            f"aws eks create-access-entry --cluster-name {eks_name} "
            f"--principal-arn <arn> --type STANDARD --username {username}")
        return
    if not _cfg_bool("k8s_ps_rotator_eks_create_access_entry", True):
        return
    try:
        from . import aws_service
        out = await asyncio.to_thread(
            aws_service.create_eks_access_entry, eks_name,
            tf.get("region") or row.region or _cfg("aws_region"),
            principal_arn=arn, username=username)
        logger.info("PS token: EKS access entry for %s: %s", row.name, out)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"EKS access entry creation failed: {exc}")
        logger.warning("PS token: EKS access entry for %s failed: %s", row.id, exc)


async def _register_pravault_mirror(db: Session, row: K8sCluster, *,
                                    vault_account_name: str,
                                    initial_password: str) -> bool:
    """Register the PRA Vault account as a managed account on the "PRA Vault Token"
    plugin, so a rotation can be pushed into PRA through Password Safe.

    Reuses ``method="pravault"`` — the HCL shape is identical to the cloud-DB mirror
    (host_name = the PRA appliance URL, no dns_name, password-managed) and the plugin
    resolves the vault account by NAME, which is that method's documented contract."""
    from . import ps_api_service, ps_resource_service
    host = _cfg("bt_api_host").rstrip("/")
    if not host:
        raise PSK8sTokenError("bt_api_host is not configured (the PRA appliance URL)")
    if not host.lower().startswith("http"):
        host = f"https://{host}"
    fa = await _resolve_functional_account(
        _cfg("k8s_ps_pravault_functional_account"), "pra vault", "token")
    platform_id = await ps_api_service.get_platform_id(
        _cfg("k8s_ps_pravault_token_platform", "PRA Vault Token"))
    workgroup_id = await ps_api_service.get_workgroup_id(_workgroup())
    reg = await ps_resource_service.register_managed_system(
        name=f"k8s-{row.name}-pravault", host_name=host,
        functional_account_id=fa["id"], platform_id=platform_id,
        workgroup_id=workgroup_id, ip_address="127.0.0.1", port=443,
        managed_account_name=vault_account_name, method="pravault",
        initial_password=initial_password)
    row.ps_pra_vault_account_id = str(reg.get("managed_account_id") or "")
    _save_state(row.id, pravault_system_id=str(reg.get("managed_system_id") or ""),
                pravault_tf_state=reg.get("tf_state_json"),
                pravault_account_name=vault_account_name)
    db.commit()
    return bool(row.ps_pra_vault_account_id)


# ── read the current value ────────────────────────────────────────────────────

async def current_token(db: Session, cluster_id: str) -> str:
    """The cluster's ServiceAccount token, checked out of Password Safe.

    Called by ``k8s_service._resolve_pra_sa_token`` when a registration exists, so the
    PRA tunnel vaults the value Password Safe holds instead of minting a second token
    the rotation plugin would never sweep."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not row.ps_token_account_id:
        raise PSK8sTokenError(
            f"cluster {cluster_id} has no Password Safe-managed ServiceAccount token")
    from . import ps_api_service
    return await ps_api_service.checkout_credential(
        int(row.ps_token_account_id),
        duration_min=_cfg_int("k8s_ps_token_checkout_duration_min", 15),
        reason=f"PRA Vault injection for cluster {row.name}")


# ── rotate on demand ──────────────────────────────────────────────────────────

async def rotate_now(db: Session, cluster_id: str, *, job_id: str = "") -> dict:
    """Ask Password Safe to rotate the token, then mirror the result into PRA in the
    same job — an operator-initiated rotation should not leave PRA stale until the next
    sweep."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not row.ps_token_account_id:
        raise PSK8sTokenError(
            f"cluster {cluster_id} has no Password Safe-managed ServiceAccount token")
    from . import ps_api_service
    await ps_api_service.change_managed_account_password(int(row.ps_token_account_id))
    if not row.ps_pra_vault_account_id:
        return {"rotated": True, "pra_synced": False,
                "note": "no PRA Vault Token mirror is registered, so PRA was not updated"}
    from . import k8s_token_sync
    synced = await k8s_token_sync.sync_cluster(db, cluster_id, force=True)
    return {"rotated": True, "pra_synced": bool(synced.get("pushed")), **synced}


# ── deregister ────────────────────────────────────────────────────────────────

async def deregister(db: Session, cluster_id: str, *, job_id: str = "") -> dict:
    """Off-board both managed systems and drop the rotator RBAC (best-effort).

    Positional ``cluster_id`` so this can join ``k8s_service.run_decommission``'s
    deregister tuple, which calls each entry as ``(db, cluster_id)``.

    The mirror goes first: a managed system that references a functional account blocks
    that account's deletion, and destroying in registration order keeps the same
    ordering rule the cloud-DB teardown follows."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not (row.ps_token_account_id or row.ps_pra_vault_account_id):
        return {"ok": True, "removed": False}
    from . import k8s_service, ps_resource_service
    state = get_state(cluster_id)
    errors: list = []

    for label, key in (("PRA Vault Token mirror", "pravault_tf_state"),
                       ("Kubernetes token managed system", "tf_state")):
        tf_state = state.get(key)
        if not tf_state:
            continue
        try:
            await ps_resource_service.deregister(tf_state)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
            logger.warning("PS token deregister (%s) for %s failed: %s",
                           label, cluster_id, exc)

    try:
        await k8s_service.remove_ps_rotator_rbac(
            db, cluster_id, mode=state.get("token_mode") or "longlived")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"rotator RBAC: {exc}")

    row.ps_token_account_id = None
    row.ps_pra_vault_account_id = None
    db.commit()
    _clear_state(cluster_id)
    try:
        from . import config_service
        config_service.delete(f"k8s_token_sync_{cluster_id}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "removed": True, "errors": errors}


# ── worker entry ──────────────────────────────────────────────────────────────

async def run(db: Session, *, cluster_id: str, job_id: str, action: str = "register",
              **kwargs) -> None:
    """``k8s_ps_token`` job handler. Owns its terminal state and never raises."""
    from . import job_service
    job_service.set_running(db, job_id)
    try:
        if action == "register":
            result = await register(db, cluster_id, job_id=job_id, **kwargs)
        elif action == "deregister":
            result = await deregister(db, cluster_id, job_id=job_id)
        elif action == "rotate":
            result = await rotate_now(db, cluster_id, job_id=job_id)
        else:
            job_service.set_failed(
                db, job_id,
                f"unknown action {action!r} (expected one of "
                f"{', '.join(VALID_PS_TOKEN_ACTIONS)})")
            return
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("PS token job failed cluster=%s action=%s", cluster_id, action)
        return
    job_service.set_completed(db, job_id, {"ps_k8s_token": result})
