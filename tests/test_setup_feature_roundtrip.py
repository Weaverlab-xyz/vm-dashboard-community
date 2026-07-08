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
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
    from web_dashboard.api.setup import _read_feature, AnsibleFeatureConfig
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


if __name__ == "__main__":
    test_unset_int_fields_round_trip()
    test_set_int_field_is_preserved()
    test_per_cloud_runner_image_keys_round_trip()
    print("ok")
