"""Guard: the migration denylist keeps out what breaks a target, and nothing else.

Two failure modes, pulling in opposite directions, and both silent:

  * Too permissive — a runtime resource handle crosses, and the target believes
    it owns a Rancher node, a Web Jump, or an Entitle agent token that belongs
    to the source. It will show them in the UI and eventually reconcile them.
  * Too restrictive — a credential quietly doesn't migrate. This is why the
    tool must not filter against ``config.Settings``: several live config keys
    are declared nowhere in ``config.py`` and would vanish.

Needs no fastapi, no database and no network.

Runs under pytest, or standalone:  python tests/test_config_migrate_classify.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.scripts.config_migrate import classify

# Keys that exist only as app_config rows — written by the sandbox scripts through
# /api/setup/import and declared in no pydantic model. If the tool ever filters on
# a declared-key list instead of a denylist, these are what it silently drops, and
# the first two are the ones that turn a migration into an outage.
_UNDECLARED_BUT_LIVE = (
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_default_subnet_id",
    "aws_default_security_group_id",
    "secrets_backend",
    "bt_ecs_jumpoint_subnet_id",
    "gcp_runner_subnetwork",
    "gcp_k8s_pods_range_name",
)


def test_credentials_are_portable():
    """The whole point of the exercise: credentials must cross."""
    blocked = [k for k in _UNDECLARED_BUT_LIVE if classify.exclusion_reason(k, "v")]
    assert not blocked, (
        "these are real config keys and must migrate, but the denylist refuses "
        "them: " + ", ".join(blocked))


def test_unknown_keys_fail_open():
    """A key nobody has heard of migrates rather than being dropped.

    New config keys land in this repo continuously. A tool that only carries
    what it recognises would rot into uselessness one release at a time.
    """
    assert classify.exclusion_reason("some_key_added_next_month", "value") is None


def test_instance_identity_is_denied():
    for key in ("setup_complete", "public_base_url", "trusted_proxy_hosts",
                "agent_base_url", "webauthn_rp_id", "database_url", "jwt_secret_key"):
        assert classify.exclusion_reason(key, "v") == classify.INSTANCE_LOCAL, key


def test_denylist_is_matched_case_insensitively():
    """The app_config key is lowercase; the env var is not.

    ``public_base_url`` is the row in the database, ``PUBLIC_BASE_URL`` the
    environment variable for the same setting. A denylist written in either
    casing alone matches half the inputs, and the half it misses is the half
    that repoints the target's OAuth callbacks at the source.
    """
    for spelling in ("public_base_url", "PUBLIC_BASE_URL", "Public_Base_Url",
                     "trusted_proxy_hosts", "TRUSTED_PROXY_HOSTS"):
        assert classify.exclusion_reason(spelling, "v") == classify.INSTANCE_LOCAL, spelling


def test_runtime_resource_handles_are_denied():
    """Handles on live infrastructure the *source* provisioned."""
    for key in ("rancher_server_url", "rancher_api_token", "rancher_ui_web_jump_tfstate",
                "portainer_url", "portainer_pat", "portainer_ui_web_jump_id",
                "entitle_agent_token_ref", "entitle_rancher_tfstate",
                "resource_expiry_armed_at", "notify_last_scan_at"):
        assert classify.exclusion_reason(key, "v") == classify.RUNTIME_HANDLE, key


def test_per_resource_keys_are_denied():
    """Keys named after a cluster or cloud-database row id.

    Both of those tables deliberately stay behind, so every one of these is an
    orphan on arrival — and ``k8s_kubeconfig_*`` is cluster-admin on a live
    cluster while ``clouddb/*/admin`` is a database master password. Copying
    them grants a second dashboard standing access to infrastructure it cannot
    even display.
    """
    for key in ("k8s_kubeconfig_dec9136c-93d9-40e3-a4e1-fc90c433642d",
                "clouddb/6f1c2b70-1111-2222-3333-444455556666/admin",
                "k8s_api_tunnel_jump_abc123",
                "k8s_entra_group_abc123", "k8s_entra_group_role_abc123",
                "k8s_entra_fed_abc123", "k8s_entra_fed_eks_abc123",
                "k8s_entra_fed_gke_abc123", "k8s_impersonator_abc123",
                "entitle_agent_azure_client_id_abc123",
                "entitle_agent_azure_key_vault_name_abc123",
                "entitle_k8s_integration_id_abc123", "entitle_k8s_tfstate_abc123",
                "rancher_cluster_id_abc123", "rancher_manifest_url_abc123"):
        assert classify.exclusion_reason(key, "v") == classify.RUNTIME_HANDLE, key


def test_per_resource_prefixes_do_not_swallow_real_settings():
    """The prefixes are broad, so assert they stop where the panel keys start."""
    for key in ("entitle_k8s_user_prefix", "clouddb_ps_workgroup",
                "clouddb_ps_platform_postgres", "entra_rbac_group_id",
                "entra_rbac_group_role", "k8s_runner", "k8s_runner_image",
                "rancher_verify_tls", "rancher_ready_timeout_s"):
        assert classify.exclusion_reason(key, "v") is None, (
            f"{key} is a Settings panel key and must migrate")


def test_local_paths_are_denied():
    for key in ("vm_cli_wrapper_path", "ssh_key_file", "storage_local_path",
                "ova_search_path", "epml_rpm_path"):
        assert classify.exclusion_reason(key, "v") == classify.LOCAL_PATH, key


def test_on_prem_is_opt_in_not_forbidden():
    """Portable in form, unreachable in practice — so excluded, but recoverable."""
    for key in ("vmware_host", "proxmox_host", "hyperv_password", "storage_local_username"):
        assert classify.exclusion_reason(key, "v") == classify.ON_PREM, key
        assert classify.exclusion_reason(key, "v", include_on_prem=True) is None, key


def test_on_prem_flag_travels_with_its_connection_details():
    """The `*_enabled` flag is held back alongside the host/password it needs.

    Carrying `vmware_enabled=1` without `vmware_host` would light up a nav entry
    and a page backed by nothing — worse than the integration staying off. The
    two must be excluded and included as a unit.
    """
    for flag in ("vmware_enabled", "proxmox_enabled", "vsphere_enabled",
                 "hyperv_enabled", "nutanix_enabled", "xcpng_enabled"):
        assert classify.exclusion_reason(flag, "1") == classify.ON_PREM, flag
        assert classify.exclusion_reason(flag, "1", include_on_prem=True) is None, flag
    # Cloud-side feature flags have no such coupling and always migrate.
    for flag in ("cloud_database_enabled", "k8s_management_enabled",
                 "entitle_enabled", "password_safe_enabled", "pra_enabled",
                 "epml_enabled", "remote_agents_enabled"):
        assert classify.exclusion_reason(flag, "1") is None, flag


def test_app_written_keys_are_denied_but_operator_input_is_not():
    """The tier's rule is "does the application write it", not "is it in a panel".

    Both of these sit in Settings → Kubernetes and look like twins.
    ``rancher_bootstrap_password`` is only ever read, so it is ordinary config.
    ``rancher_admin_password`` is written back when the service auto-generates
    one — that is what ``rancher_admin_password_generated`` records — making it
    the live credential of a node that is not migrating.
    """
    assert classify.exclusion_reason("rancher_bootstrap_password", "hunter2") is None
    assert classify.exclusion_reason("rancher_admin_password", "x") == classify.RUNTIME_HANDLE
    assert classify.exclusion_reason("rancher_admin_password_generated", "1") == classify.RUNTIME_HANDLE


def test_dashboard_egress_addresses_are_instance_identity():
    """Auto-detected on deploy and written into a managed node's firewall.

    Carrying the source's opens a hole for a host that no longer calls, and
    leaves the target locked out of its own node until a deploy re-detects.
    """
    for key in ("rancher_dashboard_egress_cidr", "portainer_dashboard_egress_cidr"):
        assert classify.exclusion_reason(key, "1.2.3.4/32") == classify.INSTANCE_LOCAL, key


def test_masked_values_never_cross():
    """``get_all_public`` bullets four keys. Storing the bullets leaves a key that
    looks configured in the UI and fails at cloud-call time."""
    for key in sorted(classify.HTTP_MASKED_KEYS):
        assert classify.exclusion_reason(key, "•" * 8) == classify.MASKED, key
        # …but the same key with a real value is exactly what we want to carry.
        assert classify.exclusion_reason(key, "AKIAREAL") is None, key


def test_secret_pointers_are_not_treated_as_secrets():
    """``*_secret`` is often a *pointer* to a secret, and pointers must stay legible.

    ``ec2_ssh_key_secret`` names an AWS Secrets Manager entry;
    ``azure_ssh_keypair_secret_name`` names a Key Vault entry. Redacting them in
    the diff would hide the values an operator most needs to eyeball.
    """
    for key in ("ec2_ssh_key_secret", "azure_ssh_keypair_secret_name",
                "oci_ssh_key_secret", "entitle_ssh_private_key_ref"):
        assert not classify.is_secret(key), f"{key} is a pointer, not a secret"
    for key in ("bt_client_secret", "oidc_client_secret", "entitle_api_token",
                "portainer_pat", "storage_local_password", "gcp_service_account_json"):
        assert classify.is_secret(key), f"{key} should be redacted in operator output"


def test_registry_secrets_are_all_recognised():
    """Anything the app itself calls a secret must redact in the diff.

    Reads ``secret_hygiene.SECRET_REGISTRY`` rather than a copy of it, so a
    secret added there is covered here without anyone remembering to.
    """
    missed = [k for k, _d in classify.SECRET_REGISTRY if not classify.is_secret(k)]
    assert not missed, "in SECRET_REGISTRY but not redacted: " + ", ".join(missed)


def test_vault_references_are_recognised_and_parsed():
    """A vault reference is the ideal thing to migrate — the pointer moves and the
    secret never leaves the vault. Getting the *named-vault* form wrong is
    dangerous rather than merely broken: ``config_service._parse_ref`` falls back
    to legacy parsing for an unregistered id, so it resolves to the wrong secret
    instead of erroring.
    """
    assert classify.is_vault_reference("azure_kv://epml-pat")
    assert classify.is_vault_reference("aws_sm://dashboard/epml_pat")
    assert not classify.is_vault_reference("just-a-literal-value")
    assert not classify.is_vault_reference("")

    assert classify.vault_id_of("azure_kv://primary/epml-pat") == "primary"
    assert classify.vault_id_of("azure_kv://epml-pat") is None
    assert classify.vault_id_of("not-a-reference") is None


def test_vault_prefixes_track_the_service_registry():
    """The prefixes come from ``secret_hygiene.BACKEND_PREFIXES``, which
    ``config_service._EXT_PREFIXES`` must agree with. Assert the overlap rather
    than a hardcoded list so a fifth backend cannot be half-added."""
    from web_dashboard.services.config_service import _EXT_PREFIXES
    registry = {p for p in classify.BACKEND_PREFIXES.values() if p}
    assert registry == set(_EXT_PREFIXES), (
        f"secret_hygiene.BACKEND_PREFIXES {sorted(registry)} has drifted from "
        f"config_service._EXT_PREFIXES {sorted(_EXT_PREFIXES)}")


def test_partition_splits_without_mutating_values():
    config = {
        "azure_client_id": "abc",
        "public_base_url": "http://localhost:8001",
        "bt_client_secret": "azure_kv://primary/bt",
        "vmware_host": "10.0.0.5",
    }
    portable, excluded = classify.partition(config)
    assert set(portable) == {"azure_client_id", "bt_client_secret"}
    assert excluded == {"public_base_url": classify.INSTANCE_LOCAL,
                        "vmware_host": classify.ON_PREM}
    # A vault reference passes through as the reference, never resolved.
    assert portable["bt_client_secret"] == "azure_kv://primary/bt"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
