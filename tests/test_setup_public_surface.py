"""The setup API's public surface: nothing a caller imports or patches may go missing.

web_dashboard/api/setup.py is being split into a package. ``main.py`` mounts
``setup.router``, ~30 tests import names from it or patch them on the module
(``monkeypatch.setattr(setup_api, "x", ...)``), so every name must stay importable from
``web_dashboard.api.setup`` whichever submodule defines it. Three checks:

  * **Frozen names.** Every top-level class, function and constant the module defined
    when it was split. Removing one is a decision (edit FROZEN), not an accident of a move.
  * **Swept uses.** Every ``from web_dashboard.api.setup import X``, ``from ..api import
    setup as s; s.X`` and ``setattr(s, "X", ...)`` anywhere in web_dashboard/ or tests/
    must resolve. Keeps itself current without touching this file.
  * **Route table.** ``setup.router``'s routes in registration order -- method, path,
    endpoint, status code and parameter signature. Route order decides which of two
    overlapping paths matches first, so it is part of the contract.

Run: python tests/test_setup_public_surface.py
"""
import ast
import inspect
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-setup-surface")

FROZEN = (
    "AWSSetup",
    "AdminSetup",
    "AdmissionControlFeatureConfig",
    "AnsibleFeatureConfig",
    "AwsRegionConfig",
    "AzureRegionConfig",
    "AzureSetup",
    "BrandLogoUpload",
    "BrandingConfig",
    "BtPlatformIdentityFeatureConfig",
    "CertLabFeatureConfig",
    "ChangeWindowsFeatureConfig",
    "CloudDatabaseFeatureConfig",
    "CloudFunctionsFeatureConfig",
    "CloudUnmanagedDiscoveryFeatureConfig",
    "CostExplorerFeatureConfig",
    "DirectoriesFeatureConfig",
    "EPMLFeatureConfig",
    "EntitleFeatureConfig",
    "FeaturesSetup",
    "GCPSetup",
    "GcpRegionConfig",
    "HeadlessImport",
    "HyperVFeatureConfig",
    "K8sManagementFeatureConfig",
    "MCPServerFeatureConfig",
    "MultiRegionFeatureConfig",
    "NotificationsFeatureConfig",
    "NutanixFeatureConfig",
    "OCISetup",
    "OidcFeatureConfig",
    "PRAFeatureConfig",
    "PasswordSafeFeatureConfig",
    "PersonaSetup",
    "PortainerFeatureConfig",
    "PovCloudFeatureConfig",
    "PovStorageSetup",
    "ProfileSetup",
    "ProxmoxFeatureConfig",
    "RegionConfigsPayload",
    "RemoteAgentsFeatureConfig",
    "ResourceExpiryFeatureConfig",
    "SetupPayload",
    "SkytapFeatureConfig",
    "SpiffeFeatureConfig",
    "SpireLabFeatureConfig",
    "SsoSetup",
    "VMSpendCapFeatureConfig",
    "VMwareFeatureConfig",
    "VSphereFeatureConfig",
    "VirtualDesktopsFeatureConfig",
    "WorkerFeatureConfig",
    "WorkloadCredentialsFeatureConfig",
    "XcpNgFeatureConfig",
    "_BRANDING_KEYS",
    "_CONFIG_DEPENDENT_CACHES",
    "_CONFIG_DEPENDENT_CACHE_PREFIXES",
    "_CONFIG_ONLY_FEATURES",
    "_DEFAULT_WHEN_BLANK",
    "_FEATURE_MODELS",
    "_LOGO_KEYS",
    "_POV_STORAGE_SPEC",
    "_PREVIEW_FLAGS",
    "_PREVIEW_FLAG_CONFIG",
    "_REGION_CONFIG_MODELS",
    "_apply_config",
    "_carry_legacy_wlc_identity",
    "_feature_to_cfg_key",
    "_guard_wlc_auth_mode",
    "_invalidate_data_caches",
    "_keep_stored",
    "_pov_storage_pairs",
    "_read_feature",
    "_region_config_cloud",
    "_region_configs_response",
    "_require_admin",
    "_save_region_configs",
    "_upsert_admin",
    "_write_feature",
    "complete_setup",
    "delete_branding_logo",
    "get_azure_regions",
    "get_branding",
    "get_branding_logo",
    "get_config",
    "get_feature_config",
    "get_preview_flags",
    "get_region_configs",
    "import_config",
    "logger",
    "patch_branding",
    "patch_feature_config",
    "patch_preview_flag",
    "put_azure_regions",
    "put_region_configs",
    "reconfigure",
    "router",
    "setup_status",
    "test_bt_platform_identity",
    "test_oidc_discovery",
    "test_pov_cloud_connection",
    "test_skytap_connection",
    "test_workload_idp",
    "upload_branding_logo",
)

ROUTES = (
    ("GET", "/api/setup/status", "setup_status", None, "request:Request"),
    ("GET", "/api/setup/config", "get_config", None, "request:Request"),
    ("POST", "/api/setup/complete", "complete_setup", 201, "payload:SetupPayload,request:Request,background_tasks:BackgroundTasks"),
    ("PUT", "/api/setup/config", "reconfigure", None, "payload:SetupPayload,request:Request,background_tasks:BackgroundTasks"),
    ("POST", "/api/setup/import", "import_config", None, "payload:HeadlessImport,request:Request,background_tasks:BackgroundTasks"),
    ("GET", "/api/setup/feature/{feature_name}", "get_feature_config", None, "feature_name:str,request:Request"),
    ("PATCH", "/api/setup/feature/{feature_name}", "patch_feature_config", None, "feature_name:str,payload:dict,request:Request"),
    ("POST", "/api/setup/bt-platform-identity/test", "test_bt_platform_identity", None, "request:Request"),
    ("POST", "/api/setup/oidc/test", "test_oidc_discovery", None, "request:Request"),
    ("POST", "/api/setup/oidc/workload-test", "test_workload_idp", None, "request:Request"),
    ("POST", "/api/setup/skytap/test", "test_skytap_connection", None, "request:Request"),
    ("POST", "/api/setup/pov-cloud/test", "test_pov_cloud_connection", None, "request:Request"),
    ("GET", "/api/setup/regions/{cloud}", "get_region_configs", None, "cloud:str,request:Request"),
    ("PUT", "/api/setup/regions/{cloud}", "put_region_configs", None, "cloud:str,payload:RegionConfigsPayload,request:Request"),
    ("GET", "/api/setup/azure-regions", "get_azure_regions", None, "request:Request"),
    ("PUT", "/api/setup/azure-regions", "put_azure_regions", None, "payload:RegionConfigsPayload,request:Request"),
    ("GET", "/api/setup/flags", "get_preview_flags", None, "request:Request"),
    ("PATCH", "/api/setup/flag/{key}", "patch_preview_flag", None, "key:str,payload:dict,request:Request"),
    ("GET", "/api/setup/branding", "get_branding", None, "request:Request"),
    ("PATCH", "/api/setup/branding", "patch_branding", None, "payload:BrandingConfig,request:Request"),
    ("POST", "/api/setup/branding/logo", "upload_branding_logo", 201, "payload:BrandLogoUpload,request:Request"),
    ("DELETE", "/api/setup/branding/logo", "delete_branding_logo", None, "request:Request"),
    ("GET", "/api/setup/branding/logo/{etag}", "get_branding_logo", None, "etag:str,request:Request"),
)


def _module():
    from web_dashboard.api import setup
    return setup


def _is_setup_module(module, level):
    return module == "web_dashboard.api.setup" or (level and module in ("api.setup", "setup"))


def _uses():
    """``{name: ["path:line", ...]}`` for every setup-module name a caller touches."""
    found = {}

    def add(name, rel, line):
        found.setdefault(name, []).append(f"{rel}:{line}")

    for top in ("web_dashboard", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "functions")]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
                if rel == "web_dashboard/api/setup.py" or rel.startswith("web_dashboard/api/setup/"):
                    continue
                with open(path, encoding="utf-8") as fh:
                    try:
                        tree = ast.parse(fh.read(), filename=path)
                    except SyntaxError:
                        continue
                aliases = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        if _is_setup_module(module, node.level):
                            for a in node.names:
                                if a.name != "*":
                                    add(a.name, rel, node.lineno)
                        elif module in ("web_dashboard.api", "api") or (node.level and module in ("api", "")):
                            for a in node.names:
                                if a.name == "setup":
                                    aliases.add(a.asname or a.name)
                    elif isinstance(node, ast.Import):
                        for a in node.names:
                            if a.name == "web_dashboard.api.setup" and a.asname:
                                aliases.add(a.asname)
                if not aliases:
                    continue
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                            and node.value.id in aliases):
                        add(node.attr, rel, node.lineno)
                    elif (isinstance(node, ast.Call) and len(node.args) >= 2
                          and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases
                          and isinstance(node.args[1], ast.Constant)
                          and isinstance(node.args[1].value, str)
                          and getattr(node.func, "attr", getattr(node.func, "id", "")) in
                          ("setattr", "getattr", "hasattr", "delattr")):
                        add(node.args[1].value, rel, node.lineno)
    return found


def test_the_frozen_public_names_are_still_exported():
    setup = _module()
    missing = [n for n in FROZEN if not hasattr(setup, n)]
    assert not missing, f"web_dashboard.api.setup no longer exports: {missing}"


def test_every_used_name_resolves():
    setup = _module()
    uses = _uses()
    assert len(uses) > 15, f"only {len(uses)} used names found -- did the sweep break?"
    missing = {n: where[:3] for n, where in uses.items() if not hasattr(setup, n)}
    assert not missing, f"used from web_dashboard.api.setup but not defined there: {missing}"


def test_the_route_table_is_unchanged():
    router = _module().router
    assert router.prefix == "/api/setup" and router.tags == ["setup"]
    assert not router.dependencies
    got = []
    for r in router.routes:
        sig = ",".join(f"{n}:{getattr(p.annotation, '__name__', str(p.annotation))}"
                       for n, p in inspect.signature(r.endpoint).parameters.items())
        got.append((",".join(sorted(r.methods)), r.path, r.endpoint.__name__, r.status_code, sig))
    assert got == list(ROUTES), (
        "setup.router's route table changed.\n  missing: "
        f"{[r for r in ROUTES if r not in got]}\n  extra: {[r for r in got if r not in ROUTES]}\n"
        "  (if only the order differs, the order is the bug)")


def test_main_still_mounts_the_router_by_attribute():
    """main.py includes ``setup.router`` -- the one name the app needs from the module."""
    with open(os.path.join(_ROOT, "web_dashboard", "main.py"), encoding="utf-8") as fh:
        text = fh.read()
    assert text.count("include_router(setup.router)") == 1


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
