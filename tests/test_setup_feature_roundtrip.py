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
                                         CostExplorerFeatureConfig,
                                         K8sManagementFeatureConfig,
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


def test_every_bound_panel_key_exists_on_some_feature_model():
    """The generalisation of the promote_runner_ test above, which was prefix-scoped and
    so never looked at any other panel — that blind spot is exactly how the Cost Explorer
    panel shipped an OCI budget input bound to a key no model declared. A key bound in the
    template but missing from its feature's model is silently DROPPED on save:
    patch_feature_config validates through the model first, pydantic ignores unknown extras
    by default, and the `k in payload` filter then intersects an already-stripped dict. The
    operator types a value, Save reports success, and nothing is written — while
    _read_feature (which iterates model_fields) leaves the input blank on every reload.

    Only ONE direction is asserted. bound - fields must be empty; fields - bound must not
    be, because plenty of model fields are deliberately env/DB-only (the ansible ECS
    cpu/memory knobs, the entitle agent KMS keys) and a handful more are bound through a
    different Alpine object than panelCfg (the entitle user-JIT panel's own `userJit`).

    Comparing against the UNION of all models rather than each panel's own model is safe
    here and needs no panel-block parsing: `enabled` is the only field name shared by more
    than one model, so no real config key is owned by two panels and the union check is
    equivalent to a per-panel one. It is also the check that matters — a key on the wrong
    panel's model still round-trips, a key on no model at all cannot.
    """
    html = open(_SETTINGS_HTML, encoding="utf-8").read()
    # The regex only sees dotted access. That is complete today, but would silently skip
    # anything written panelCfg["key"] — fail loudly instead of quietly under-checking.
    assert "panelCfg[" not in html, \
        "settings.html gained bracket-style panelCfg access; teach this test to read it"
    bound = set(re.findall(r"panelCfg\.([A-Za-z_][A-Za-z_0-9]*)", html))
    assert len(bound) > 200, f"only found {len(bound)} bound keys — did the regex break?"
    fields = {f for model in _FEATURE_MODELS.values() for f in model.model_fields}
    assert not (bound - fields), (
        "bound in settings.html but declared on no feature model, so the value is "
        f"discarded on save: {sorted(bound - fields)}")


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


def test_cost_explorer_budgets_round_trip():
    """All five budgets — the overall one plus one per cloud — must survive the cycle.

    These are the only ``float`` fields on any panel, and _read_feature coerces bool and
    int ONLY: a float field falls through to config_service.get, so an unset budget reads
    back as "" rather than as the model default. That "" is why the _blank_to_zero
    field_validator exists, and why every budget field has to be listed on it — a float
    field omitted from that validator reads back "" and then 422s the entire Cost Explorer
    panel on save, which is strictly worse than the dropped-value bug it replaces. This
    asserts the read shape and the re-validation together, so neither half can regress
    alone.
    """
    CONF.clear()
    data = _read_feature("cost_explorer", CostExplorerFeatureConfig)
    budgets = ("cost_monthly_budget", "cost_budget_aws", "cost_budget_azure",
               "cost_budget_gcp", "cost_budget_oci")
    for key in budgets:
        assert key in data, f"{key} is not on CostExplorerFeatureConfig — a form field " \
                            "bound to it would be silently dropped on save"
        assert data[key] == "", f"{key} read back as {data[key]!r}, not the unset \"\""
    # patch_feature_config does exactly this on save — it must not raise. Only passes
    # while _blank_to_zero covers every one of the five.
    assert CostExplorerFeatureConfig(**data).cost_budget_oci == 0.0


def test_cost_explorer_set_budget_is_preserved():
    """A configured per-cloud budget round-trips as that float. cost_service reads
    cost_budget_<cloud> for the per-cloud over/approaching alert, so a value that doesn't
    survive the save means the alert can never fire for that cloud."""
    CONF.clear()
    CONF["cost_budget_oci"] = "250.50"
    try:
        data = _read_feature("cost_explorer", CostExplorerFeatureConfig)
        assert CostExplorerFeatureConfig(**data).cost_budget_oci == 250.5
    finally:
        CONF.clear()


# The Rancher and Portainer management planes carry a deliberately parallel
# ``*_ui_<suffix>`` group for their optional PRA Web Jump. Suffixes that exist on one
# side and legitimately not the other:
_UI_SUFFIX_EXCLUSIONS = {
    # Runtime state written by the provisioner/teardown, never operator input — the
    # same reason resource_expiry_armed_at isn't a panel field.
    "web_jump_id", "web_jump_tfstate", "vault_account_id", "jumpoint_egress_ip",
    # rancher-only, and read by NOTHING: an sra_web_jump has no local listen port
    # (that concept belongs to protocol-tunnel jumps — k8s_api_tunnel_local_port).
    # Binding an input would let an operator set a value that changes nothing.
    "local_port",
}


def test_rancher_and_portainer_ui_panels_are_symmetrical():
    """The two Web-Jump groups must stay bound in lockstep. rancher_ui_web_jump_enabled
    was bound while its whole config group wasn't, so an admin could switch the broker ON
    from Settings and then had no way to say which Jump Group / Gateway / Vault group /
    Gateway cloud it used — env var or a hand-rolled PATCH only.

    test_every_bound_panel_key_exists_on_some_feature_model cannot catch this: it checks
    bound-implies-model, and here nothing was bound, so there was nothing for it to look
    at. Unbound-but-declared needs its own guard, and a whole-template version of it would
    be wrong — plenty of model fields are deliberately env-only.

    Direction is both ways here, unlike that sweep: the features are mirrors, so a suffix
    bound for one and missing for the other is a gap in whichever panel lacks it, not a
    judgement about which came first."""
    html = open(_SETTINGS_HTML, encoding="utf-8").read()
    bound = set(re.findall(r"panelCfg\.((?:rancher|portainer)_ui_[a-z_0-9]+)", html))
    suffixes = lambda prefix: {k[len(prefix):] for k in bound if k.startswith(prefix)}
    rancher, portainer = suffixes("rancher_ui_"), suffixes("portainer_ui_")
    assert rancher, "the k8s_management panel lost its rancher_ui_* bindings"
    assert portainer, "the portainer panel lost its portainer_ui_* bindings"
    only_portainer = portainer - rancher - _UI_SUFFIX_EXCLUSIONS
    only_rancher = rancher - portainer - _UI_SUFFIX_EXCLUSIONS
    assert not only_portainer, (
        "bound as portainer_ui_* but not rancher_ui_*: "
        f"{sorted(only_portainer)} — add the input to the k8s_management panel "
        "or list the suffix in _UI_SUFFIX_EXCLUSIONS with a reason")
    assert not only_rancher, (
        "bound as rancher_ui_* but not portainer_ui_*: "
        f"{sorted(only_rancher)} — add the input to the portainer panel "
        "or list the suffix in _UI_SUFFIX_EXCLUSIONS with a reason")


def test_bound_ui_keys_are_declared_on_the_model_their_prefix_names():
    """Not a narrower copy of test_every_bound_panel_key_exists_on_some_feature_model:
    that one compares against the UNION of every model, which is the right call for a
    whole-template sweep (it needs no panel-block parsing) but accepts a key declared on
    ANY model. Here the prefix names the owner, so this pins the stronger property — a
    ``rancher_ui_*`` key must be on K8sManagementFeatureConfig specifically.

    Worth pinning because these two groups differ only by prefix, which makes adding one
    to the other's model the easy copy-paste slip. The union sweep would pass; the panel
    binding it would still save nothing, since patch_feature_config validates against the
    model of the feature in the URL."""
    html = open(_SETTINGS_HTML, encoding="utf-8").read()
    for prefix, fields in (("rancher_ui_", set(K8sManagementFeatureConfig.model_fields)),
                           ("portainer_ui_", set(PortainerFeatureConfig.model_fields))):
        bound = set(re.findall(rf"panelCfg\.({prefix}[a-z_0-9]+)", html))
        assert bound and not (bound - fields), \
            f"bound in settings.html but not on the model: {sorted(bound - fields)}"


def test_rancher_ui_web_jump_fields_round_trip():
    """The newly-bound Rancher group must survive GET->PATCH. ``rancher_ui_local_port``
    is the int trap (unset must read 443, not ""), ``rancher_ui_verify_certificate`` the
    bool one — a float/Optional annotation on either 422s the whole k8s panel on save."""
    CONF.clear()
    data = _read_feature("k8s_management", K8sManagementFeatureConfig)
    assert data["rancher_ui_local_port"] == 443
    assert isinstance(data["rancher_ui_local_port"], int)
    assert data["rancher_ui_verify_certificate"] is False
    assert data["rancher_ui_web_jump_enabled"] is False
    # Strings fall back at resolve time (jump group → bt_jump_group_name etc.), so blank
    # is the correct unset read — but the key must be PRESENT or the input has nothing
    # to bind to and saves nothing.
    for key in ("rancher_ui_jump_group", "rancher_ui_jumpoint_name",
                "rancher_ui_vault_account_group_id"):
        assert data[key] == "", f"{key} read back as {data[key]!r}"
    assert data["rancher_ui_jumpoint_cloud"] == ""
    # patch_feature_config does exactly this on save — it must not raise.
    K8sManagementFeatureConfig(**data)


def test_rancher_ui_web_jump_values_are_preserved():
    """What the operator types in the panel comes back as typed — including the two
    keys that had no other UI surface at all (gateway cloud, verify certificate)."""
    CONF.clear()
    CONF.update({"rancher_ui_jump_group": "K8s Admins",
                 "rancher_ui_jumpoint_name": "bt-gateway-gcp",
                 "rancher_ui_vault_account_group_id": "42",
                 "rancher_ui_jumpoint_cloud": "aws",
                 "rancher_ui_verify_certificate": "1"})
    try:
        data = _read_feature("k8s_management", K8sManagementFeatureConfig)
        assert data["rancher_ui_jump_group"] == "K8s Admins"
        assert data["rancher_ui_jumpoint_name"] == "bt-gateway-gcp"
        assert data["rancher_ui_vault_account_group_id"] == "42"
        assert data["rancher_ui_jumpoint_cloud"] == "aws"
        assert data["rancher_ui_verify_certificate"] is True
        K8sManagementFeatureConfig(**data)
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
