"""Password Safe registration for a cluster's PRA ServiceAccount token.

Left alone, the token `k8s_service._mint_pra_sa_token` creates is vaulted in PRA and
lives forever. This module makes it a Password Safe **managed account** on the
"Kubernetes Service Account Token" custom plugin, so Password Safe rotates it on a
schedule — and registers a second managed account on the "PRA Vault Token" plugin,
whose plugin writes a value into the PRA Vault account a brokered session injects.

**Password Safe keeps those two in step, not the dashboard.** Registration links them
with ``SyncedAccounts``: the PRA Vault account becomes a *subscriber* of the token
account, and a managed account and its subscribers always share an identical
credential, so every rotation reaches PRA with nothing running here on a schedule. An
earlier version of this feature polled ``LastChangeDate`` every 15 minutes and pushed
the value across itself; that was a reimplementation of a product primitive, and all of
its machinery — watermarks, backoff, parking, a rotation-loop circuit breaker — existed
only to manage failure modes the poll introduced.

Two things about the plugin shape the rest of the design, and both are easy to get wrong:

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
                   mirror_to_pra: Optional[bool] = None,
                   warnings: Optional[list] = None) -> dict:
    """Make the cluster's ServiceAccount token a Password Safe managed account.

    Ordered, and the order is the correctness argument:

      1. apply the rotator RBAC, so the functional account can actually rotate;
      2. create the managed system + account. The token the cluster is using right now is
         offered as the seed, but a bearer token is far longer than the 128 characters the
         create API accepts, so in practice it is dropped and the account starts out
         holding a placeholder — which is why step 5 is not optional (see below);
      3. create the "PRA Vault Token" subscriber, offered the same seed, when there is a
         PRA Vault account to keep in step;
      4. **link** the two with ``SyncedAccounts`` — fatal, and deliberately BEFORE the
         rotation. From here on Password Safe owns the sync; a failure at this point has
         changed nothing in the cluster, whereas rotating first and failing to link would
         leave PRA holding a value nothing will ever refresh;
      5. rotate once, which proves the whole path (functional-account credentials →
         cloud control plane → API server → RBAC → Secret create) at registration time
         rather than at 3am on the first scheduled rotation — and, because the link is
         already in place, proves propagation with it. When the seed at step 2 was dropped
         this is also the only thing that puts a real credential in the vault, so it runs
         even if ``change_on_register`` said not to;
      6. delete the dashboard's ``<sa>-token`` Secret, which the plugin's label-scoped
         sweep would never remove. Gated on the link, not on observing the propagation:
         Password Safe queues change operations, so there is nothing synchronous to
         observe, and the link is the guarantee that it converges.

    Idempotent: an already-registered cluster re-applies the RBAC and reconciles a
    missing sync link (see ``_reconcile_synced_link``) rather than returning early —
    step 2 commits ``ps_token_account_id`` before the fatal step 4, so a failed link
    leaves a row that looks registered and a pair that does not sync. That same failure
    also leaves the account holding the placeholder step 2 created it with, so the
    re-register rotates when the stored state shows neither a seed nor a completed
    rotation. Reconciling only the link would repair the plumbing around a credential
    that authenticates to nothing."""
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

    # Caller-owned when ``run`` supplies one: the returned dict is the only thing that
    # carries these, and a step raising means there is no returned dict. See ``run``.
    warnings = [] if warnings is None else warnings
    mode = (mode or token_mode(cluster_id)).strip().lower()
    if mode not in VALID_PS_TOKEN_MODES:
        raise PSK8sTokenError(f"unknown token mode {mode!r}")
    ttl = int(ttl_seconds or _cfg_int("k8s_ps_token_ttl_seconds", 3600))
    ns = namespace or _cfg("pra_k8s_namespace", "pra-access")
    sa = service_account or _cfg("pra_k8s_sa_name", "pra-access")
    account_name = f"{ns}/{sa}"

    if row.ps_token_account_id:
        note = await _apply_rbac(db, cluster_id, mode=mode, warnings=warnings)
        sync = await _reconcile_synced_link(row, job_id=job_id, warnings=warnings)
        # The other half of the recoverable half-state, and the reason this path cannot
        # just reconcile the link: step 2 commits the account id, but the seed is dropped
        # (a bearer token exceeds the create API's 128-character cap), so a run that died
        # before step 5 left the account holding its create-time placeholder — and
        # ``current_token`` would serve THAT to the PRA tunnel. Neither a seed nor a
        # completed rotation means nothing has ever put a real credential in the vault.
        st = get_state(cluster_id)
        refilled = False
        if not st.get("seeded") and not st.get("rotated"):
            if job_id:
                await broadcast_progress(
                    job_id, 80, "Rotating to replace the create-time placeholder…")
            await ps_api_service.change_managed_account_password(
                int(row.ps_token_account_id))
            refilled = True
            _save_state(cluster_id, rotated=True)
            warnings.append(
                "the managed account was still holding the placeholder it was created with "
                "(a ServiceAccount token is too long to seed, and no rotation had "
                "completed) — rotated so the vault holds a real credential")
        return {"already_registered": True, "managed_account_id": row.ps_token_account_id,
                "pravault_account_id": row.ps_pra_vault_account_id or "",
                "rbac": note, "pra_synced": sync["linked"], "relinked": sync["relinked"],
                "rotated": refilled, "warnings": warnings}

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
    # A bearer token is 800-1,200 characters and the create API caps Password at 128, so
    # this is False in practice — the vault holds a placeholder until step 5 rotates. Read
    # it rather than assuming either way: it is what makes that rotation mandatory.
    seeded = bool(reg.get("initial_password_seeded"))
    row.ps_token_account_id = str(reg.get("managed_account_id") or "")
    _save_state(cluster_id, system_id=str(reg.get("managed_system_id") or ""),
                tf_state=reg.get("tf_state_json"), token_mode=mode, address=address,
                account_name=account_name, seeded=seeded)
    db.commit()

    # 3. The PRA Vault subscriber — only meaningful when there IS a PRA Vault copy.
    mirror = (_cfg_bool("k8s_ps_pravault_mirror_enabled", True)
              if mirror_to_pra is None else bool(mirror_to_pra))
    vault_name = f"k8s-{row.name}-sa"
    pravault_registered = False
    if mirror and row.pra_vault_account_id:
        if job_id:
            await broadcast_progress(job_id, 55, "Registering the PRA Vault Token account…")
        try:
            pravault_registered = await _register_pravault_mirror(
                db, row, vault_account_name=vault_name, initial_password=current)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal on its own: the k8s account is still rotatable, PRA just stops
            # tracking. Mirrors provision_k8s_tunnel's best-effort vault half.
            warnings.append(f"PRA Vault Token account registration failed: {exc}")
            logger.warning("PS token: PRA mirror for %s failed: %s", cluster_id, exc)
    elif mirror and not row.pra_vault_account_id:
        warnings.append(
            "no PRA Vault account exists for this cluster (register the PRA k8s tunnel "
            "with credential injection first), so rotations will not reach PRA")
    del current

    # 4. Hand the sync to Password Safe. FATAL, and before the rotation on purpose:
    #    a failure here has changed nothing in the cluster, and reporting success with
    #    the token managed but unlinked is exactly the hole this feature exists to close.
    linked = False
    if pravault_registered:
        if job_id:
            await broadcast_progress(job_id, 70, "Syncing the PRA Vault account to the token…")
        link = await ps_api_service.link_synced_account(
            parent_account_id=int(row.ps_token_account_id),
            synced_account_id=int(row.ps_pra_vault_account_id),
            expect_subscriber_platform=_cfg("k8s_ps_pravault_token_platform",
                                            "PRA Vault Token"))
        if not link.get("confirmed"):
            raise PSK8sTokenError(
                f"Password Safe accepted the sync of account {row.ps_pra_vault_account_id} to "
                f"{row.ps_token_account_id} but the account is not in the parent's synced list "
                f"— refusing to continue, because rotations would not reach PRA.")
        linked = True
        _save_state(cluster_id, synced_to_parent=True)

    # 5. Rotate once. The link is already in place, so Password Safe propagates.
    rotated = False
    change = (_cfg_bool("k8s_ps_token_change_on_register", True)
              if change_on_register is None else bool(change_on_register))
    if not change and not seeded:
        # Not a preference we can honour: the seed was dropped, so Password Safe holds a
        # placeholder that authenticates to nothing, and ``current_token`` hands whatever
        # is in the vault to the PRA tunnel. Skipping the rotation would leave a
        # registration that reads as complete while serving a dead credential.
        change = True
        warnings.append(
            "rotated despite change_on_register=false: the ServiceAccount token is longer "
            "than the 128 characters the managed-account create API accepts, so it could "
            "not be seeded and only a rotation puts a real credential in the vault")
    if change:
        if job_id:
            await broadcast_progress(job_id, 80, "Rotating the token once to prove the path…")
        await ps_api_service.change_managed_account_password(int(row.ps_token_account_id))
        rotated = True

    # 6. Retire the Secret the plugin's label-scoped sweep would never remove.
    legacy_deleted = False
    if rotated and linked:
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
    elif rotated and not linked:
        warnings.append(
            f"rotated, but with no synced PRA Vault account the {sa}-token Secret was left in "
            f"place so PRA keeps working — rotation will not revoke anything until the sync "
            f"exists")

    _save_state(cluster_id, rotated=rotated, legacy_secret_deleted=legacy_deleted,
                rbac=rbac_note)
    logger.info("PS token registration for cluster %s: account=%s linked=%s rotated=%s "
                "legacy_deleted=%s", row.name, row.ps_token_account_id, linked, rotated,
                legacy_deleted)
    return {"managed_system_id": get_state(cluster_id).get("system_id"),
            "managed_account_id": row.ps_token_account_id,
            "pravault_account_id": row.ps_pra_vault_account_id or "",
            "address": address, "token_mode": mode, "rbac": rbac_note,
            "rotated": rotated, "pra_synced": linked,
            "legacy_secret_deleted": legacy_deleted, "warnings": warnings}


async def _reconcile_synced_link(row: K8sCluster, *, job_id: str = "",
                                 warnings: list) -> dict:
    """Re-create the ``SyncedAccounts`` link when an already-registered pair is unlinked.

    ``register`` commits ``ps_token_account_id`` at step 2, when it creates the managed
    system — two steps before the (deliberately fatal) link. So a failure at the link
    leaves the column set, both managed accounts created, and nothing syncing rotations
    into PRA. Without reconciling here, re-running the register would see the column,
    re-apply the RBAC and report success on a pair that still does not sync; the only
    recovery was to deregister and register again, which off-boards and re-creates two
    managed systems to fix one missing reference.

    The likeliest cause of that failure is a **403 on the link**, and the grant it needs
    is not settled: the REST reference says Password Safe *Account Management (Full
    control)*, ``ps-cli synced-accounts -h`` says *Role Management (Read/Write)*. Either
    way it is fixed tenant-side and then wants a retry — the one shape of failure most
    worth making retryable.

    Read first, then link: ``synced_account_status`` needs only *Read*, an already-linked
    pair must not be POSTed at on every idempotent re-register, and a POST against a live
    link is not documented as idempotent. A relink that is attempted and does not confirm
    is fatal, for the same reason step 4 is — reporting success on an unsynced pair is the
    hole this feature exists to close."""
    if not row.ps_pra_vault_account_id:
        # Nothing to link to. A re-register cannot repair this either: the PRA Vault
        # account is only created on a first registration, so say so plainly instead of
        # implying the re-run fixed something.
        warnings.append(
            "no PRA Vault Token account is registered for this cluster, so rotations do "
            "not reach PRA — re-registering cannot create one. Remove the Password Safe "
            "registration and register again (with a PRA k8s tunnel in place).")
        return {"linked": False, "relinked": False}

    from . import ps_api_service
    try:
        st = await ps_api_service.synced_account_status(
            parent_account_id=int(row.ps_token_account_id),
            synced_account_id=int(row.ps_pra_vault_account_id))
    except Exception as exc:  # noqa: BLE001
        # Don't guess in either direction: a transient read failure must not fail a
        # re-register that is otherwise fine, and blind-POSTing the link is not safe on a
        # pair that may already be live. The status panel reads this the same way, live.
        warnings.append(
            "could not read the synced-account state from Password Safe, so the PRA sync "
            "was left as it is — check the token rotation panel (details are in the "
            "server log)")
        logger.warning("PS token relink check for cluster %s failed: %s", row.id, exc)
        return {"linked": False, "relinked": False}
    if st.get("linked"):
        return {"linked": True, "relinked": False}

    if job_id:
        from ..api.websocket import broadcast_progress
        await broadcast_progress(job_id, 60, "Re-syncing the PRA Vault account…")
    # Same direction as step 4, and for the same reason: both path segments are plain
    # managed-account ids, so a swapped pair links happily and syncs BACKWARDS — pushing
    # the PRA Vault account's value onto the cluster's real token account.
    link = await ps_api_service.link_synced_account(
        parent_account_id=int(row.ps_token_account_id),
        synced_account_id=int(row.ps_pra_vault_account_id),
        expect_subscriber_platform=_cfg("k8s_ps_pravault_token_platform",
                                        "PRA Vault Token"))
    if not link.get("confirmed"):
        raise PSK8sTokenError(
            f"Password Safe accepted re-syncing account {row.ps_pra_vault_account_id} to "
            f"{row.ps_token_account_id} but the account is not in the parent's synced list "
            f"— refusing to report this repaired, because rotations still would not reach "
            f"PRA.")
    _save_state(row.id, synced_to_parent=True)
    logger.info("PS token: re-synced PRA Vault account %s to token account %s for "
                "cluster %s", row.ps_pra_vault_account_id, row.ps_token_account_id, row.id)
    return {"linked": True, "relinked": True}


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
    plugin, so Password Safe can drive the write into PRA itself.

    This only creates the account. What makes rotations reach PRA is the
    ``SyncedAccounts`` link the caller adds next — on its own, this account is just a
    second managed account that happens to hold the same value.

    Reuses ``method="pravault"`` — the HCL shape is identical to the cloud-DB mirror
    (password-managed, and the plugin resolves the vault account by NAME, which is that
    method's documented contract) with ONE difference, and it is load-bearing: the
    appliance URL goes in ``dns_name`` and a per-cluster label in ``host_name``.

    Password Safe names a workgroup-created managed system after its HostName, and the
    Terraform provider's ``passwordsafe_managed_account`` attaches to its system by NAME
    (it takes no system id). The cloud-DB and OT mirrors both put the appliance URL in
    host_name, so every PRA Vault managed system in the tenant shares one name — and
    since those two are on the same "PRA Vault Username Password" platform, the collision
    never showed. This one is on "PRA Vault Token". Measured live: the system was created
    on the Token platform, and the account was then created on the pre-existing cloud-DB
    Username Password system of the same name, which the SyncedAccounts platform guard
    refused to sync a cluster bearer token into.

    The plugin has to find the URL in DnsName for this to work. That is the same shape the
    ``k8ssa`` method already relies on (host_name is a human label there, the address rides
    dns_name) and matches the pravault branch's own note that the plugin walks the
    populated host fields in Password Safe's order — but it is worth confirming with one
    rotation the first time this runs against a tenant."""
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
    system_name = f"k8s-{row.name}-pravault"
    reg = await ps_resource_service.register_managed_system(
        name=system_name, host_name=system_name, dns_name=host,
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
    the rotation plugin would never sweep.

    The managed system id goes with the account id: ``POST Requests`` authorises the
    pair, not the account alone. Step 2 of ``register`` recorded it, so the common path
    spends no round trip re-reading what it already knows — and a state that predates
    the column falls back to the lookup in ``ps_api_service._checkout``."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not row.ps_token_account_id:
        raise PSK8sTokenError(
            f"cluster {cluster_id} has no Password Safe-managed ServiceAccount token")
    from . import ps_api_service
    system_id = str(get_state(cluster_id).get("system_id") or "").strip()
    return await ps_api_service.checkout_credential(
        int(row.ps_token_account_id),
        duration_min=_cfg_int("k8s_ps_token_checkout_duration_min", 15),
        system_id=int(system_id) if system_id.isdigit() else 0,
        reason=f"PRA Vault injection for cluster {row.name}")


# ── rotate on demand ──────────────────────────────────────────────────────────

async def rotate_now(db: Session, cluster_id: str, *, job_id: str = "",
                     warnings: Optional[list] = None) -> dict:
    """Ask Password Safe to rotate the token now.

    One call, because the synced-account link means PRA is updated by Password Safe as
    part of the same change — there is no second half for the dashboard to drive. The
    link is re-read rather than assumed: an admin can unlink in the Password Safe
    console, and a rotation on an unlinked pair silently leaves PRA holding the old
    value, which is worth saying in the job result rather than discovering later."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not row.ps_token_account_id:
        raise PSK8sTokenError(
            f"cluster {cluster_id} has no Password Safe-managed ServiceAccount token")
    from . import ps_api_service
    warnings = [] if warnings is None else warnings

    linked = False
    note = "no PRA Vault Token account is registered, so PRA will not be updated"
    if row.ps_pra_vault_account_id:
        st = await ps_api_service.synced_account_status(
            parent_account_id=int(row.ps_token_account_id),
            synced_account_id=int(row.ps_pra_vault_account_id))
        linked = bool(st.get("linked"))
        note = "" if linked else (
            f"managed account {row.ps_pra_vault_account_id} is NOT synced to "
            f"{row.ps_token_account_id} in Password Safe — this rotation will not reach PRA. "
            f"Re-register the cluster's token, or re-create the sync in Password Safe.")

    # Into the warnings as well as the return value, and before the rotation: the change
    # call is exactly what fails here (a 400 from Credentials/Change), and on that path
    # the return value never happens.
    if note:
        warnings.append(note)
    await ps_api_service.change_managed_account_password(int(row.ps_token_account_id))
    return {"rotated": True, "pra_synced": linked, "note": note, "warnings": warnings}


# ── deregister ────────────────────────────────────────────────────────────────

async def deregister(db: Session, cluster_id: str, *, job_id: str = "",
                     warnings: Optional[list] = None) -> dict:
    """Off-board both managed systems and drop the rotator RBAC (best-effort).

    Positional ``cluster_id`` so this can join ``k8s_service.run_decommission``'s
    deregister tuple, which calls each entry as ``(db, cluster_id)``.

    Teardown runs in reverse of registration: unlink the synced pair, then the PRA Vault
    account, then the token account. The unlink is first because a subscriber
    relationship is a reference between the two accounts — and because deleting a
    subscriber out from under a live link is the one ordering that could leave Password
    Safe rotating into an account that no longer exists."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None or not (row.ps_token_account_id or row.ps_pra_vault_account_id):
        return {"ok": True, "removed": False}
    from . import k8s_service, ps_api_service, ps_resource_service
    state = get_state(cluster_id)
    # This call's best-effort step failures ARE its warnings, so they share ``run``'s
    # list — off-boarding one managed system and then dying on the next must still
    # report the first.
    errors: list = [] if warnings is None else warnings

    if row.ps_token_account_id and row.ps_pra_vault_account_id:
        try:
            await ps_api_service.unlink_synced_account(
                parent_account_id=int(row.ps_token_account_id),
                synced_account_id=int(row.ps_pra_vault_account_id))
        except Exception as exc:  # noqa: BLE001 — best-effort, like every other step here
            errors.append(f"unsyncing the PRA Vault account: {exc}")
            logger.warning("PS token deregister (unlink) for %s failed: %s", cluster_id, exc)

    for label, key in (("PRA Vault Token account", "pravault_tf_state"),
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
        # Orphan cleanup, kept deliberately after the module that wrote it was deleted:
        # the poll-and-push sync shipped in v26.7.7, so a deployed instance can still
        # hold a `k8s_token_sync_<cluster_id>` row that nothing will ever read again.
        # Safe to drop once no instance predates the synced-accounts change.
        from . import config_service
        config_service.delete(f"k8s_token_sync_{cluster_id}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "removed": True, "errors": errors}


# ── status ────────────────────────────────────────────────────────────────────

async def sync_status(db: Session, cluster_id: str) -> dict:
    """Password Safe's live view of the pair, for the operator-facing modal.

    Deliberately not cached. The dashboard no longer participates in the sync, so the
    only thing it could cache is a claim about what was true at registration — and an
    admin unlinking the pair in the Password Safe console is precisely the case an
    operator opens this to diagnose."""
    row = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if row is None:
        raise PSK8sTokenError(f"cluster {cluster_id} not found")
    if not row.ps_token_account_id:
        return {"registered": False, "linked": False}
    out = {"registered": True,
           "managed_account_id": row.ps_token_account_id,
           "pravault_account_id": row.ps_pra_vault_account_id or "",
           "linked": False}
    if not row.ps_pra_vault_account_id:
        out["note"] = ("no PRA Vault Token account is registered, so rotations do not "
                       "reach PRA")
        return out
    from . import ps_api_service
    try:
        out.update(await ps_api_service.synced_account_status(
            parent_account_id=int(row.ps_token_account_id),
            synced_account_id=int(row.ps_pra_vault_account_id)))
    except Exception as exc:  # noqa: BLE001 — a status read must not 500 the modal
        # The detail goes to the log, never into the returned dict: this is served
        # straight to the browser by the ps-token/status endpoint, and a Password Safe
        # error carries its response body (CodeQL py/stack-trace-exposure). The
        # replacement still names the one cause worth naming, from our own text.
        logger.warning("PS token status read for cluster %s failed: %s", cluster_id, exc)
        out["error"] = (
            "could not read the sync state from Password Safe — check the server logs. "
            "The usual cause is the API identity lacking Password Safe Account Management "
            "(Full control), which the synced-account calls need.")
    return out


# ── worker entry ──────────────────────────────────────────────────────────────

def _failure_message(exc: Exception, warnings: list) -> str:
    """The failed job's ``error_message``: what raised, then what was already known.

    The warnings are not colour — the non-fatal steps put the *remedy* in them.
    ``_apply_rbac`` returns ``"failed: {exc}"`` as a note rather than raising, so an RBAC
    problem exists only in the warnings and the result dict; ``_ensure_eks_access_entry``
    writes the exact ``aws eks create-access-entry`` line there when
    ``k8s_ps_rotator_eks_principal_arn`` is unset.

    Diagnosed live on 2026-08-17 against EKS cluster k8s-aws-east: a register collected
    that access-entry line at step 1 and then failed at step 5 on a 400 from
    ``ManagedAccounts/{id}/Credentials/Change``, and because the warnings lived only in a
    return value that never happened, the operator saw the 400 alone and worked the IAM
    mapping out by hand. ``error_message`` is the field the job page renders for a failed
    job (``templates/jobs/detail.html``); the row's metadata reaches that page through
    one allowlisted view that serves ``agent_discover`` alone, and ``GET /api/jobs/{id}``
    is a fixed projection that omits it. So the prose copy goes here, where the operator
    reads it, and the structured copy goes to ``set_failed``'s result.
    """
    msg = str(exc) or exc.__class__.__name__
    if not warnings:
        return msg
    return "\n".join([msg, "", f"Collected before the failure ({len(warnings)}):",
                      *(f"  • {w}" for w in warnings)])


async def run(db: Session, *, cluster_id: str, job_id: str, action: str = "register",
              **kwargs) -> None:
    """``k8s_ps_token`` job handler. Owns its terminal state and never raises.

    The warnings list is owned HERE, not by the three entry points, and that is the
    whole point: each of them returns its warnings in its result dict, and an exception
    means there is no result dict — so every warning a run collected before a later step
    raised used to be discarded by the ``except`` below. Passing the list down leaves it
    in this frame, where the failure path can still read it (see ``_failure_message``)."""
    from . import job_service
    job_service.set_running(db, job_id)
    warnings: list = []
    try:
        if action == "register":
            result = await register(db, cluster_id, job_id=job_id, warnings=warnings,
                                    **kwargs)
        elif action == "deregister":
            result = await deregister(db, cluster_id, job_id=job_id, warnings=warnings)
        elif action == "rotate":
            result = await rotate_now(db, cluster_id, job_id=job_id, warnings=warnings)
        else:
            job_service.set_failed(
                db, job_id,
                f"unknown action {action!r} (expected one of "
                f"{', '.join(VALID_PS_TOKEN_ACTIONS)})")
            return
    except Exception as exc:
        job_service.set_failed(
            db, job_id, _failure_message(exc, warnings),
            {"ps_k8s_token": {"action": action, "failed": True, "warnings": warnings}})
        logger.exception("PS token job failed cluster=%s action=%s", cluster_id, action)
        return
    job_service.set_completed(db, job_id, {"ps_k8s_token": result})
