"""Which config keys may cross between two dashboard instances.

**This is a denylist, and it has to be.** The tempting alternative — filter
against ``config.Settings.model_fields`` — silently drops your cloud
credentials. Plenty of live keys are declared nowhere in ``config.py`` and
exist only as ``app_config`` rows written by the sandbox scripts through
``/api/setup/import``: ``aws_access_key_id``, ``aws_secret_access_key``,
``aws_default_subnet_id``, ``secrets_backend``, the three
``<cloud>_region_configs`` blobs, and more. ``Settings`` is an annotation
source, not a filter.

So everything not explicitly denied is exported. The bias is deliberate: the
failure mode of fail-open is one extra key in a bundle the operator reads
before applying, while the failure mode of fail-closed is a credential that
quietly didn't migrate and a target that half-works.

Module-level imports stay stdlib-only (plus ``secret_hygiene``, which is itself
stdlib-only) so ``tests/test_config_migrate_classify.py`` runs with no fastapi,
no sqlalchemy and no database — matching how CI executes each test file as its
own process.
"""
from __future__ import annotations

# Re-exported so the diff's redaction and the "what didn't come across" report
# read from the real registry rather than a copy of it.
from ...services.secret_hygiene import BACKEND_PREFIXES, SECRET_REGISTRY

#: Exclusion reasons, in the order they are checked.
INSTANCE_LOCAL = "instance_local"
RUNTIME_HANDLE = "runtime_handle"
LOCAL_PATH = "local_path"
ON_PREM = "on_prem"
MASKED = "masked"


# ── Tier 1: instance identity ────────────────────────────────────────────────
#
# Keys that name *this* deployment. Copying them points the target at the
# source's origin, proxy, or database.
#
# Casing matters more than it looks. The ``app_config`` keys are lowercase
# ``public_base_url`` / ``trusted_proxy_hosts``; the SHOUTING spellings are the
# env-var names for the same settings. A denylist written in the env-var casing
# matches nothing and migrates both — which is how you end up with an Azure
# instance whose OAuth callbacks point at localhost and whose login throttle
# trusts a proxy that isn't there.

_INSTANCE_LOCAL = frozenset({
    # The target owns its own setup state; importing this would let a
    # half-applied bundle mark a virgin stack "configured".
    "setup_complete",
    # Origin identity. PUBLIC_BASE_URL pins OAuth callback URIs and the
    # remote-agent signing audience; TRUSTED_PROXY_HOSTS must be the literal IP
    # of *this* deployment's proxy (uvicorn 0.27 compares strings — a hostname
    # or CIDR silently never matches). See SECURITY.md.
    "public_base_url",
    "trusted_proxy_hosts",
    "notify_base_url",
    "azure_oauth_redirect_uri",
    # The agent signing audience. api/agent.py implements a whole conflict
    # detection path around this being pinned per-instance; copying it makes
    # every enrolled agent's signature fail to verify.
    "agent_base_url",
    # WebAuthn credentials are bound to the RP id, so these travelling would
    # invalidate the target's own security keys rather than carry the source's.
    "webauthn_rp_id",
    "webauthn_origin",
    "webauthn_rp_name",
    # Listener / transport / storage identity.
    "api_host",
    "api_port",
    "ssl_certfile",
    "ssl_keyfile",
    "cors_origins",
    "database_url",
    # Env-only by design: the pool budget is sized against the target's own
    # Postgres (an Azure B1ms Flexible Server caps at 50 connections).
    "db_pool_size",
    "db_max_overflow",
    # This dashboard's own public egress address, auto-detected and persisted on
    # deploy. It ends up in a managed node's firewall allow-list, so carrying the
    # source's would open a hole for a host that is no longer calling and leave
    # the target locked out of its own node until the next deploy re-detects.
    "rancher_dashboard_egress_cidr",
    "portainer_dashboard_egress_cidr",
    # Never stored here, but deny it explicitly so a hand-edited bundle cannot
    # smuggle in the key that decrypts everything.
    "jwt_secret_key",
})


# ── Tier 2: runtime resource handles ─────────────────────────────────────────
#
# The tier that actually bites. These are written back into ``app_config`` by
# the application itself and point at live infrastructure the *source* instance
# provisioned — a Rancher node, a Portainer node, Web Jump tfstate, Entitle
# agent tokens. Copying them makes the target believe it owns resources it has
# never seen: it will render them in the UI, hand out their credentials, and
# eventually try to reconcile or tear them down.

_RUNTIME_HANDLES = frozenset({
    # Rancher management plane — provisioned node + its generated credentials.
    #
    # Note what is NOT here: `rancher_bootstrap_password` is only ever *read*
    # (`rancher_node_service` fails the deploy when it is unset) and is typed by
    # the operator in Settings → Kubernetes, so it is ordinary config and
    # migrates. `rancher_admin_password` looks like its twin but is written back
    # by the service when it auto-generates one — `rancher_admin_password_generated`
    # exists to record exactly that — which makes it the live credential of a
    # node that is staying behind. The distinction throughout this tier is
    # whether the application writes the key, not whether a panel shows it.
    "rancher_server_url",
    "rancher_internal_url",
    "rancher_api_token",
    "rancher_admin_password",
    "rancher_admin_password_generated",
    "rancher_ui_web_jump_id",
    "rancher_ui_web_jump_tfstate",
    "rancher_ui_vault_account_id",
    "rancher_ui_jumpoint_egress_ip",
    # Portainer management plane — same shape.
    "portainer_url",
    "portainer_pat",
    "portainer_admin_password",
    "portainer_admin_password_generated",
    "portainer_ui_web_jump_id",
    "portainer_ui_web_jump_tfstate",
    "portainer_ui_vault_account_id",
    "gcp_portainer_zone",
    # Entitle — minted agent tokens and the Terraform state behind them.
    "entitle_agent_token_ref",
    "entitle_agent_token_name",
    "entitle_agent_token_tf_state",
    "entitle_agent_cluster_id",
    "entitle_rancher_integration_id",
    "entitle_rancher_tfstate",
    # Sweeper / scanner bookkeeping. resource_expiry_armed_at in particular is
    # load-bearing: copying an old timestamp can arm the target's auto-delete
    # sweeper against resources it just learned about.
    "resource_expiry_armed_at",
    "resource_expiry_last_sweep",
    "notify_last_scan_at",
    "worker_runtime_status",
})


# Per-resource keys, named after the row id of a cluster or a cloud database:
# ``k8s_kubeconfig_<cluster_id>``, ``clouddb/<db_id>/admin``, and the Entra /
# Entitle / Rancher bindings that hang off the same ids.
#
# These are the sharpest edge in the whole store, for two compounding reasons.
# The tables they are keyed by — ``k8s_clusters`` and ``cloud_databases`` —
# deliberately never migrate, so on arrival every one of these is an orphan
# pointing at an id the target will never mint. And several are not bookkeeping
# but live credentials: ``k8s_kubeconfig_*`` is cluster-admin on a real cluster,
# ``clouddb/*/admin`` is a database master password. Copying them hands a second
# dashboard standing access to infrastructure it does not manage and cannot
# show, which is the worst of both.
#
# Matched by prefix because the ids are generated at runtime. Ordered
# shortest-first where prefixes nest (``k8s_entra_fed_`` already covers
# ``k8s_entra_fed_eks_`` / ``_gke_``).
_RUNTIME_HANDLE_PREFIXES = (
    "clouddb/",
    "k8s_kubeconfig_",
    "k8s_api_tunnel_jump_",
    "k8s_entra_group_",
    "k8s_entra_fed_",
    "k8s_impersonator_",
    "entitle_agent_azure_client_id_",
    "entitle_agent_azure_key_vault_name_",
    "entitle_k8s_integration_id_",
    "entitle_k8s_tfstate_",
    "rancher_cluster_id_",
    "rancher_manifest_url_",
)


# ── Tier 3: host filesystem paths ────────────────────────────────────────────
#
# Paths on the source host. The target is a different filesystem — on Azure
# Container Apps, one with no volume at all.

_LOCAL_PATHS = frozenset({
    "vm_cli_wrapper_path",
    "log_dir",
    "ssh_key_file",
    "ssh_host",
    "ova_search_path",
    "iso_source_path",
    "packer_work_root",
    "vmx_output_path",
    "ovf_tool_path",
    "storage_local_path",
    "clouddb_ps_azure_cert_path",
    "clouddb_ps_ssm_public_key_path",
    "epml_rpm_path",
    "epml_deb_path",
    "pathfinder_script_path",
})


# ── Tier 4: on-premises, unreachable from the target ─────────────────────────
#
# Portable in form, useless in practice: a cloud-hosted dashboard has no
# network route to a home-lab hypervisor or an SMB share. Excluded by default
# but recoverable with --include-on-prem, because an operator staging a future
# VPN or a like-for-like host does want them.
#
# The prefixes deliberately catch the `*_enabled` feature flag alongside the
# connection details, so the two move together. Carrying `vmware_enabled=1`
# without `vmware_host` would light up a nav entry and a page backed by nothing,
# which is a worse outcome than the integration simply staying off.

_ON_PREM_PREFIXES = (
    "vmware_",
    "proxmox_",
    "vsphere_",
    "hyperv_",
    "nutanix_",
    "xcpng_",
    "storage_local_",
)


# ── Secrets ──────────────────────────────────────────────────────────────────
#
# The codebase carries four non-identical secret-key lists (see
# docs/config-migration.md). Only config_service._SECRET_KEYS — the four below —
# drives masking in GET /api/setup/config, which is why an HTTP export cannot
# recover them and export-local exists.

#: Masked by ``config_service.get_all_public()``; an HTTP export sees bullets.
HTTP_MASKED_KEYS = frozenset({
    "aws_secret_access_key",
    "azure_client_secret",
    "azure_oauth_client_secret",
    "gcp_service_account_json",
})

#: The sentinel ``get_all_public`` substitutes. Matched by prefix rather than
#: equality because ``_write_feature`` uses the same two-bullet prefix test and
#: the run lengths differ between call sites.
MASK_SENTINEL_PREFIX = "••"

_REGISTRY_SECRETS = frozenset(k for k, _desc in SECRET_REGISTRY)

# Suffixes that mark a value as sensitive for *display* purposes. Deliberately
# not used for filtering — only to decide whether the diff prints a value.
_SECRET_SUFFIXES = (
    "_secret", "_password", "_token", "_api_key", "_pat",
    "_private_key", "_passphrase", "_service_account_json", "_deploy_key",
)

# The trap this list guards: a name ending in "_secret" is often a *pointer* to
# a secret rather than the secret itself, and pointers must migrate verbatim.
# ec2_ssh_key_secret names an AWS Secrets Manager entry; azure_ssh_keypair_secret_name
# names a Key Vault entry. Redacting them in the diff would hide the very value
# an operator needs to eyeball.
_POINTER_SUFFIXES = ("_secret_name", "_secret_title", "_key_secret", "_secret_ref", "_ref")


def is_secret(key: str) -> bool:
    """True when ``key``'s value should be redacted in operator-facing output.

    Errs toward redaction, but never for a pointer-to-a-secret — see
    ``_POINTER_SUFFIXES``.
    """
    k = (key or "").strip().lower()
    if not k:
        return False
    if k in _REGISTRY_SECRETS or k in HTTP_MASKED_KEYS:
        return True
    if k.endswith(_POINTER_SUFFIXES):
        return False
    return k.endswith(_SECRET_SUFFIXES)


def is_vault_reference(value: str) -> bool:
    """True when ``value`` is an external-vault pointer rather than a literal.

    These are the ideal thing to migrate: the reference moves, the secret never
    leaves the vault. Prefixes come from ``secret_hygiene.BACKEND_PREFIXES`` so
    this cannot drift from ``config_service._EXT_PREFIXES``.
    """
    v = (value or "").strip()
    return bool(v) and any(v.startswith(p) for p in BACKEND_PREFIXES.values() if p)


def vault_id_of(value: str) -> str | None:
    """The named-vault id in a multi-vault reference, or ``None``.

    ``azure_kv://primary/epml-pat`` → ``"primary"``; ``azure_kv://epml-pat`` →
    ``None``. Used for the pre-flight warning: ``config_service._parse_ref``
    falls back to legacy single-vault parsing when the id isn't a registered
    ``SecretVault``, so a reference whose vault row didn't migrate resolves to
    the wrong thing rather than erroring.
    """
    v = (value or "").strip()
    for prefix in BACKEND_PREFIXES.values():
        if prefix and v.startswith(prefix):
            rest = v[len(prefix):]
            return rest.split("/", 1)[0] if "/" in rest else None
    return None


def is_masked(value: object) -> bool:
    """True when ``value`` is the redaction sentinel rather than a real value.

    Importing one of these writes the literal bullet string into the target,
    where it *looks* configured in the UI and fails at cloud-call time.
    """
    return isinstance(value, str) and value.startswith(MASK_SENTINEL_PREFIX)


def exclusion_reason(key: str, value: object = "",
                     *, include_on_prem: bool = False) -> str | None:
    """Why ``key`` must not cross, or ``None`` when it may.

    Order matters only for the reported reason, not the outcome — a key in two
    tiers is excluded either way, and the first match is the more specific
    explanation.
    """
    k = (key or "").strip().lower()
    if not k:
        return INSTANCE_LOCAL
    if is_masked(value):
        return MASKED
    if k in _INSTANCE_LOCAL:
        return INSTANCE_LOCAL
    if k in _RUNTIME_HANDLES or k.startswith(_RUNTIME_HANDLE_PREFIXES):
        return RUNTIME_HANDLE
    if k in _LOCAL_PATHS:
        return LOCAL_PATH
    if not include_on_prem and k.startswith(_ON_PREM_PREFIXES):
        return ON_PREM
    return None


def partition(config: dict, *, include_on_prem: bool = False) -> tuple[dict, dict]:
    """Split a flat config map into ``(portable, excluded)``.

    ``portable`` maps key → value; ``excluded`` maps key → reason. Values are
    never mutated, so a vault reference passes through as the reference.
    """
    portable: dict = {}
    excluded: dict = {}
    for key, value in (config or {}).items():
        reason = exclusion_reason(key, value, include_on_prem=include_on_prem)
        if reason:
            excluded[key] = reason
        else:
            portable[key] = value
    return portable, excluded


def annotate(key: str, value: object) -> dict:
    """Per-key metadata for the bundle, so the file can be reviewed without
    reading every value: is it a secret, is it a vault pointer, was it masked."""
    return {
        "secret": is_secret(key),
        "vault_ref": is_vault_reference(value if isinstance(value, str) else ""),
        "masked": is_masked(value),
    }
