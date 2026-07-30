"""Regression: a feature config read by ``api.setup._read_feature`` must survive
the PATCH round-trip.

``patch_feature_config`` re-validates the WHOLE payload through the feature's
pydantic model (``AnsibleFeatureConfig(**payload)``), so every value
``_read_feature`` emits for an *unset* key has to be accepted by that model on the
way back. ``bool`` fields were coerced to real booleans; ``int`` fields were not —
an unset int read back as ``""`` (``config_service.get``'s default), which fails
int validation on save and silently 422s the entire Settings "Configure" panel
(the error renders off-screen at the top of the panel, so Save looks dead). This
pins the int round-trip so it can't regress.

Pure-Python: ``config_service`` is stubbed (no DB). Skips if fastapi/pydantic (the
app deps ``api.setup`` needs) aren't installed. Runs under pytest, or standalone:
    python tests/test_setup_feature_roundtrip.py
"""
import ast
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SETTINGS_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_STORAGE_API = os.path.join(_ROOT, "web_dashboard", "api", "storage.py")

# Simulated config store. Unset keys resolve to "" — exactly what the real
# config_service.get(key) returns for a key that was never written.
CONF = {}


def _install_config_stub():
    """Stub web_dashboard.services.config_service so _read_feature reads CONF
    instead of a real backend/DB (mirrors test_k8s_tf_vars.py)."""
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []  # mark as a package so the submodule import resolves
    sys.modules["web_dashboard.services"] = services
    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg


_install_config_stub()
try:
    from web_dashboard.api.setup import (_read_feature, _CONFIG_ONLY_FEATURES,
                                         _FEATURE_MODELS, AnsibleFeatureConfig,
                                         PortainerFeatureConfig,
                                         ResourceExpiryFeatureConfig)
except Exception as exc:  # pragma: no cover — skip if fastapi/pydantic/app deps missing
    try:
        import pytest
        pytest.skip(f"api.setup import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def test_unset_int_fields_round_trip():
    """An all-unset ansible config must re-validate through the model — the exact
    GET->PATCH path that silently 422'd before the int coercion fix."""
    CONF.clear()
    data = _read_feature("ansible", AnsibleFeatureConfig)
    # int fields come back as their model default (a real int), never "".
    assert data["ansible_ephemeral_secret_ttl_min"] == 30
    assert data["ansible_managed_request_duration_min"] == 60
    assert isinstance(data["ansible_ephemeral_secret_ttl_min"], int)
    # patch_feature_config does exactly this on save — it must not raise.
    AnsibleFeatureConfig(**data)


def test_set_int_field_is_preserved():
    """A configured int value round-trips as that int (not the default)."""
    CONF.clear()
    CONF["ansible_ephemeral_secret_ttl_min"] = "45"
    try:
        data = _read_feature("ansible", AnsibleFeatureConfig)
        assert data["ansible_ephemeral_secret_ttl_min"] == 45
        AnsibleFeatureConfig(**data)
    finally:
        CONF.clear()


def test_per_cloud_runner_image_keys_round_trip():
    """The per-cloud k8s runner image overrides exist and round-trip: blank when
    unset (they fall back to k8s_runner_image at resolve time), and a configured
    Azure override (e.g. an ACR mirror) survives the GET->PATCH cycle."""
    CONF.clear()
    data = _read_feature("ansible", AnsibleFeatureConfig)
    for k in ("k8s_runner_image_aws", "k8s_runner_image_azure", "k8s_runner_image_gcp"):
        assert k in data and data[k] == ""
    CONF["k8s_runner_image_azure"] = "myacr.azurecr.io/dtzar/helm-kubectl:latest"
    try:
        data = _read_feature("ansible", AnsibleFeatureConfig)
        assert data["k8s_runner_image_azure"] == "myacr.azurecr.io/dtzar/helm-kubectl:latest"
        AnsibleFeatureConfig(**data)
    finally:
        CONF.clear()


def _annotated_fields(path, class_name):
    """Field name -> annotation source for a pydantic model, read via ast so the
    module doesn't have to be importable (api.storage pulls in the cloud SDKs)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == class_name)
    return {s.target.id: ast.unparse(s.annotation) for s in cls.body
            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}


def test_promote_runner_fields_round_trip():
    """The promote runner's per-target-cloud config lives on this panel. Its numeric
    knobs (ECS cpu/memory, ACI cpu/memory_gb, OCI ocpus/memory_gbs) are typed ``float``
    or int-ish in config.py but MUST be ``str`` on the model: _read_feature only coerces
    bool/int, so a float field would read back as "" and 422 the whole panel on save —
    the same failure mode the int fields at the top of this file pin."""
    CONF.clear()
    data = _read_feature("ansible", AnsibleFeatureConfig)
    numeric = ("promote_runner_ecs_cpu", "promote_runner_ecs_memory",
               "promote_runner_azure_cpu", "promote_runner_azure_memory_gb",
               "promote_runner_oci_ocpus", "promote_runner_oci_memory_gbs")
    for key in numeric:
        assert AnsibleFeatureConfig.model_fields[key].annotation is str, \
            f"{key} must be str on the model (float/int breaks the unset round-trip)"
        assert data[key] == "", f"{key} read back as {data[key]!r}, not the unset \"\""
    # The one with no fallback anywhere: without a form field an AWS promote can't be
    # configured from the UI at all (the ECS task can't write the S3 staging object).
    assert "promote_runner_ecs_task_role_arn" in data
    AnsibleFeatureConfig(**data)

    CONF["promote_runner_ecs_task_role_arn"] = "arn:aws:iam::123456789012:role/promote"
    CONF["promote_runner_oci_ocpus"] = "4"
    try:
        data = _read_feature("ansible", AnsibleFeatureConfig)
        assert data["promote_runner_ecs_task_role_arn"].endswith("role/promote")
        assert data["promote_runner_oci_ocpus"] == "4"
        AnsibleFeatureConfig(**data)
    finally:
        CONF.clear()


def test_every_promote_runner_key_has_a_form_field():
    """A promote_runner_* key the API accepts but no form binds is dead config — the
    operator has to PATCH JSON to configure a promote. Pin that every key on the model
    is bound in the Remote Worker panel."""
    html = open(_SETTINGS_HTML, encoding="utf-8").read()
    bound = set(re.findall(r"panelCfg\.(promote_runner_[a-z_0-9]+)", html))
    model_keys = {k for k in AnsibleFeatureConfig.model_fields if k.startswith("promote_runner_")}
    assert model_keys, "the panel model lost its promote_runner_* fields"
    assert not (model_keys - bound), \
        f"no form field for: {sorted(model_keys - bound)}"
    assert not (bound - model_keys), \
        f"bound in settings.html but not on AnsibleFeatureConfig: {sorted(bound - model_keys)}"


def test_promote_runner_keys_match_the_storage_api():
    """Both surfaces write the same config_service keys — the panel (this model) and
    PATCH /api/storage/config (StorageConfigPatch), which scripted setups use. If they
    drift, a key is settable on one and invisible on the other."""
    patch_keys = {k for k in _annotated_fields(_STORAGE_API, "StorageConfigPatch")
                  if k.startswith("promote_runner_")}
    model_keys = {k for k in AnsibleFeatureConfig.model_fields if k.startswith("promote_runner_")}
    assert model_keys == patch_keys, (
        f"only on the panel: {sorted(model_keys - patch_keys)}; "
        f"only on /api/storage/config: {sorted(patch_keys - model_keys)}")
    # GET /api/storage/config lists its keys literally; an omission there means the
    # PATCH-able key never reaches the client that's about to PATCH the object back.
    storage_src = open(_STORAGE_API, encoding="utf-8").read()
    get_listed = set(re.findall(r'"(promote_runner_[a-z_0-9]+)"', storage_src))
    assert not (patch_keys - get_listed), \
        f"missing from GET /api/storage/config: {sorted(patch_keys - get_listed)}"


def test_portainer_managed_node_fields_round_trip():
    """The Portainer panel grew managed-node deploy knobs, including two int fields
    (ready timeout, boot disk). An all-unset read must return real ints — not "" —
    and re-validate, or saving the panel 422s the same way the ansible one did."""
    CONF.clear()
    data = _read_feature("portainer", PortainerFeatureConfig)
    assert data["portainer_ready_timeout_s"] == 300
    assert data["gcp_portainer_boot_disk_gb"] == 20
    assert isinstance(data["portainer_ready_timeout_s"], int)
    assert isinstance(data["gcp_portainer_boot_disk_gb"], int)
    # Bools must be real bools (an unset bool would otherwise render the toggle wrong).
    assert data["gcp_portainer_allow_open"] is False
    # String fields are NOT coerced to the model default — an unset one reads back as
    # "". That's fine here because portainer_node_service resolves every string knob as
    # `config_service.get(k) or settings.k`, so a blank config value falls through to
    # the settings default rather than deploying with an empty image/name.
    assert data["gcp_portainer_image"] == ""
    # patch_feature_config does exactly this on save — it must not raise.
    PortainerFeatureConfig(**data)


def test_portainer_set_int_is_preserved():
    """A configured int survives the GET->PATCH cycle as that int."""
    CONF.clear()
    CONF["portainer_ready_timeout_s"] = "600"
    try:
        data = _read_feature("portainer", PortainerFeatureConfig)
        assert data["portainer_ready_timeout_s"] == 600
        PortainerFeatureConfig(**data)
    finally:
        CONF.clear()


def test_resource_expiry_ints_round_trip():
    """The auto-delete panel has EIGHT int fields — the most exposed to the ""-doesn't-
    validate bug this file exists for. An unset read must return real ints and
    re-validate, or the whole panel 422s on save and the operator can't configure the
    feature at all."""
    CONF.clear()
    data = _read_feature("resource_expiry", ResourceExpiryFeatureConfig)
    expected = {
        "resource_expiry_default_hours": 0,
        "resource_expiry_extend_hours": 24,
        "resource_expiry_max_total_hours": 720,
        "resource_expiry_warn_hours": 24,
        "resource_expiry_grace_minutes": 30,
        "resource_expiry_sweep_interval_minutes": 30,
        "resource_expiry_sweep_retention_days": 7,
        "resource_expiry_max_per_pass": 10,
    }
    for key, want in expected.items():
        assert data[key] == want, f"{key}: {data[key]!r} != {want}"
        assert isinstance(data[key], int), f"{key} read back as {type(data[key])}"
    # patch_feature_config does exactly this on save — it must not raise.
    ResourceExpiryFeatureConfig(**data)


def test_resource_expiry_is_safe_by_default():
    """Two independent brakes, pinned so neither can be flipped by an innocent-looking
    default change: report-only is ON, and the default lifetime is 0 (= stamp nothing).
    Together they mean enabling the feature deletes nothing until an operator makes two
    more deliberate choices."""
    CONF.clear()
    data = _read_feature("resource_expiry", ResourceExpiryFeatureConfig)
    assert data["resource_expiry_dry_run"] is True
    assert data["resource_expiry_enforce"] is False
    assert data["resource_expiry_default_hours"] == 0
    assert data["resource_expiry_allow_never"] is False
    assert data["enabled"] is False
    # And the same defaults on the model itself, which is what a PATCH validates against.
    m = ResourceExpiryFeatureConfig()
    assert m.resource_expiry_dry_run is True and m.resource_expiry_enforce is False
    assert m.resource_expiry_default_hours == 0


def test_resource_expiry_runtime_state_is_not_editable():
    """armed_at and last_sweep are written by the reaper. If they were panel fields, an
    operator could reset the arming clock — the one delay that guarantees a review window
    between enabling the feature and destroying something — from the Settings page."""
    fields = set(ResourceExpiryFeatureConfig.model_fields)
    for runtime_key in ("resource_expiry_armed_at", "resource_expiry_last_sweep"):
        assert runtime_key not in fields, f"{runtime_key} must not be a panel field"


def test_resource_expiry_is_registered_with_a_real_toggle():
    """Registration is duplicated between _FEATURE_MODELS and settings.html and is easy
    to half-do. It must NOT be config-only: the feature needs its own on/off."""
    assert _FEATURE_MODELS.get("resource_expiry") is ResourceExpiryFeatureConfig
    assert "resource_expiry" not in _CONFIG_ONLY_FEATURES
    assert "enabled" in ResourceExpiryFeatureConfig.model_fields


def test_resource_expiry_set_int_is_preserved():
    CONF.clear()
    CONF["resource_expiry_default_hours"] = "72"
    try:
        data = _read_feature("resource_expiry", ResourceExpiryFeatureConfig)
        assert data["resource_expiry_default_hours"] == 72
        ResourceExpiryFeatureConfig(**data)
    finally:
        CONF.clear()


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
