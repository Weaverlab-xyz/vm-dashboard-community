"""The database layer's public surface: nothing a caller imports may go missing.

web_dashboard/database.py became a package. Callers (420 files) and ~40 test stubs key on
the module path ``web_dashboard.database``, so every name they use must still be there,
whichever submodule now defines it. Two checks:

  * **Frozen list.** The classes and functions the module defined when it was split.
    Removing one is a decision, made by editing this list, not an accident of a move.
  * **Swept imports.** Every ``from web_dashboard.database import X`` (and the relative
    spellings) anywhere in web_dashboard/ or tests/ must resolve. This one keeps itself
    current: a new caller of a new name is covered without touching this file.

Run: python tests/test_database_public_surface.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-database-surface")

FROZEN = (
    "AccessRole",
    "AgentCell",
    "AgentEnrollAttempt",
    "AgentHost",
    "AgentNonce",
    "AgentRelease",
    "AgentRollout",
    "AppConfig",
    "Approval",
    "AuditLog",
    "Base",
    "BeyondTrustTenant",
    "BrandAsset",
    "CertLab",
    "ChangeWindow",
    "CloudCostCache",
    "CloudDatabase",
    "CloudFunction",
    "ConfigApplyState",
    "ConfigMgmtRoute",
    "ContainerStateCache",
    "DashboardStatCache",
    "DirectoryHealth",
    "EntitleActivation",
    "EntitleBundleSet",
    "EntitleVirtualApp",
    "EphemeralState",
    "ExternalWorkloadIdentity",
    "FeedbackSubmission",
    "Fido2Credential",
    "Gateway",
    "HealthArea",
    "HealthCheck",
    "HypervisorConnection",
    "HypervisorVMCache",
    "Job",
    "JobLog",
    "JobSchedule",
    "K8sCluster",
    "LoginAttempt",
    "ManagedDirectory",
    "NotificationDelivery",
    "NotificationEndpoint",
    "OAuthClient",
    "OAuthGroupMapping",
    "PersonalAccessToken",
    "PovAccessor",
    "PovBlueprint",
    "PovCloudTemplate",
    "PovCloudTemplateVM",
    "PovEnvironment",
    "PovEnvironmentVM",
    "PovPlatformCache",
    "PovSignoff",
    "PovTemplateBuild",
    "PovUseCaseProgress",
    "PovVendorUser",
    "RegisteredImage",
    "RemoteAgent",
    "RunnerCredentialGrant",
    "SchemaMarker",
    "SecretVault",
    "SessionLocal",
    "SpiffeTrustDomain",
    "SpireAdminCall",
    "SpireAdminRelay",
    "SpireLab",
    "StorageAssetScope",
    "User",
    "VMWorkgroupOverride",
    "VirtualDesktop",
    "Workgroup",
    "WorkloadCloudCredential",
    "WorkloadCredentialLease",
    "WorkloadK8sToken",
    "_backfill_new_permission_scopes",
    "_commit_marked",
    "_create_all_sqlite",
    "_grant_presenter_own_pov",
    "_json_list",
    "_materialise_empty_permission_maps",
    "_narrow_presenter_to_own_povs",
    "_retire_unenforced_levels",
    "_strip_retired",
    "bcrypt_input",
    "check_password_length",
    "create_admin_user",
    "engine",
    "engine_url",
    "get_db",
    "get_password_hash",
    "init_db",
    "merge_permission_maps",
    "narrow_to_token_scope",
    "password_length_error",
    "pool_capacity",
    "verify_password",
)


FROZEN_TABLES = (
    "access_roles",
    "agent_cells",
    "agent_enroll_attempts",
    "agent_hosts",
    "agent_nonces",
    "agent_releases",
    "agent_rollouts",
    "app_config",
    "approvals",
    "audit_log",
    "beyondtrust_tenants",
    "brand_asset",
    "cert_labs",
    "change_windows",
    "cloud_cost_cache",
    "cloud_databases",
    "cloud_functions",
    "config_apply_state",
    "config_mgmt_routes",
    "container_state_cache",
    "dashboard_stat_cache",
    "directory_health",
    "entitle_activations",
    "entitle_bundle_sets",
    "entitle_virtual_apps",
    "ephemeral_state",
    "external_workload_identities",
    "feedback_submissions",
    "fido2_credentials",
    "gateways",
    "health_areas",
    "health_checks",
    "hypervisor_connections",
    "hypervisor_vm_cache",
    "job_logs",
    "job_schedules",
    "jobs",
    "k8s_clusters",
    "login_attempts",
    "managed_directories",
    "notification_deliveries",
    "notification_endpoints",
    "oauth_clients",
    "oauth_group_mappings",
    "personal_access_tokens",
    "pov_accessors",
    "pov_blueprints",
    "pov_cloud_template_vms",
    "pov_cloud_templates",
    "pov_environment_vms",
    "pov_environments",
    "pov_platform_cache",
    "pov_signoffs",
    "pov_template_builds",
    "pov_use_case_progress",
    "pov_vendor_users",
    "registered_images",
    "remote_agents",
    "runner_credential_grants",
    "schema_markers",
    "secret_vaults",
    "spiffe_trust_domains",
    "spire_admin_calls",
    "spire_admin_relays",
    "spire_labs",
    "storage_asset_scopes",
    "users",
    "virtual_desktops",
    "vm_workgroup_overrides",
    "workgroups",
    "workload_cloud_credentials",
    "workload_credential_lease",
    "workload_k8s_tokens",
)


def _module():
    import web_dashboard.database as database
    return database


def _importers():
    """``{name: ["path:line", ...]}`` for every name imported from the database module."""
    found = {}
    for top in ("web_dashboard", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "functions")]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
                if rel.startswith("web_dashboard/database/") or rel == "web_dashboard/database.py":
                    continue
                with open(path, encoding="utf-8") as fh:
                    try:
                        tree = ast.parse(fh.read(), filename=path)
                    except SyntaxError:
                        continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom):
                        continue
                    module = node.module or ""
                    is_db = (module == "web_dashboard.database"
                             or (node.level and module == "database")
                             or (node.level and module == "" and False))
                    if not is_db:
                        continue
                    for alias in node.names:
                        if alias.name != "*":
                            found.setdefault(alias.name, []).append(f"{rel}:{node.lineno}")
    return found


def test_the_frozen_public_names_are_still_exported():
    database = _module()
    missing = [n for n in FROZEN if not hasattr(database, n)]
    assert not missing, f"web_dashboard.database no longer exports: {missing}"


def test_every_imported_name_resolves():
    database = _module()
    importers = _importers()
    assert len(importers) > 50, f"only {len(importers)} imported names found -- did the sweep break?"
    missing = {n: where[:3] for n, where in importers.items() if not hasattr(database, n)}
    assert not missing, f"imported from web_dashboard.database but not defined there: {missing}"


def test_the_models_register_on_one_base():
    """create_all only sees mappers that were imported; a package must import them all.

    Compared as a set, so a model that falls out of ``__init__``'s imports is named."""
    database = _module()
    tables = set(database.Base.metadata.tables)
    assert tables >= set(FROZEN_TABLES), (
        f"tables no longer registered: {sorted(set(FROZEN_TABLES) - tables)}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
