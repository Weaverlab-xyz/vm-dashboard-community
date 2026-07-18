"""Multi-region support (Phase 2b): generalised per-region config resolver.

services/region_config.py was Azure-only; Phase 2b generalises it to a cloud-
parameterised resolver (aws/gcp/azure) backed by ``<cloud>_region_configs`` JSON
maps, keeping the exact backward-compat contract. This pins:

  * single-region installs (no ``<cloud>_region_configs``) resolve every field to
    the flat keys — bit-for-bit unchanged;
  * a region entry's value wins; a blank field falls back to its flat key;
  * the configured default region is never shadowed by a region entry;
  * secondary fallbacks (gcp jumpoint_subnetwork→subnetwork, db_network→network;
    azure vnet_resource_group→resource_group);
  * merge_region_fields merges field-by-field across regions without clobbering;
  * resolve_azure_region stays a thin wrapper;
  * setup.py's per-cloud region models stay in lock-step with region_fields(cloud)
    (drift guard), and the import-key namespace parser maps keys to the right cloud.

config_service / config are stubbed so no DB is needed.

Run: python tests/test_region_config.py   (or under pytest)
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub config_service (a mutable dict) + settings before importing region_config.
_CONF: dict = {}
_cfg_mod = types.ModuleType("web_dashboard.services.config_service")
_cfg_mod.get = lambda key, default="": _CONF.get(key, default)
_cfg_mod.set = lambda key, value: _CONF.__setitem__(key, value)
sys.modules["web_dashboard.services.config_service"] = _cfg_mod

_conf_mod = types.ModuleType("web_dashboard.config")


class _Settings:
    def __getattr__(self, _key):
        return ""


_conf_mod.settings = _Settings()
sys.modules["web_dashboard.config"] = _conf_mod

from web_dashboard.services import region_config as rc  # noqa: E402


def _reset():
    _CONF.clear()


# ── single-region: everything resolves to flat keys ──────────────────────────

def test_single_region_resolves_to_flat_keys():
    _reset()
    _CONF.update({
        "aws_region": "us-east-2",
        "aws_default_subnet_id": "subnet-aws",
        "ec2_ssh_key_secret": "aws-ssh",
        "aws_db_subnet_group_name": "aws-dbsg",
        "gcp_region": "us-central1",
        "gcp_subnetwork": "gcp-subnet",
        "gcp_db_network": "gcp-dbnet",
        "azure_location": "centralus",
        "azure_db_subnet_id": "az-db-subnet",
    })
    # No <cloud>_region_configs at all → flat values for every region.
    for region in ("us-east-2", "us-west-2"):
        r = rc.resolve_region("aws", region)
        assert r["default_subnet_id"] == "subnet-aws"
        assert r["ssh_key_secret"] == "aws-ssh"
        assert r["db_subnet_group_name"] == "aws-dbsg"
    assert rc.resolve_region("gcp", "europe-west1")["subnetwork"] == "gcp-subnet"
    assert rc.resolve_region("azure", "westus2")["db_subnet_id"] == "az-db-subnet"


# ── per-region override wins; blank field falls back to flat ──────────────────

def test_region_entry_overrides_and_per_field_fallback():
    _reset()
    _CONF.update({
        "aws_region": "us-east-2",
        "aws_default_subnet_id": "subnet-flat",
        "ec2_ssh_key_secret": "ssh-flat",
        "aws_region_configs": json.dumps({
            "us-west-2": {"default_subnet_id": "subnet-west"},  # only overrides subnet
        }),
    })
    r = rc.resolve_region("aws", "us-west-2")
    assert r["default_subnet_id"] == "subnet-west"   # region entry wins
    assert r["ssh_key_secret"] == "ssh-flat"         # blank field → flat fallback


def test_default_region_is_never_shadowed():
    _reset()
    _CONF.update({
        "aws_region": "us-east-2",
        "aws_default_subnet_id": "subnet-flat",
        # A stray entry for the default region must NOT override the flat value.
        "aws_region_configs": json.dumps({"us-east-2": {"default_subnet_id": "subnet-bad"}}),
    })
    assert rc.resolve_region("aws", "us-east-2")["default_subnet_id"] == "subnet-flat"
    assert rc.resolve_region("aws", "")["default_subnet_id"] == "subnet-flat"


# ── secondary fallbacks ───────────────────────────────────────────────────────

def test_secondary_fallbacks():
    _reset()
    _CONF.update({
        "gcp_region": "us-central1",
        "gcp_subnetwork": "vm-subnet",
        "gcp_network": "vm-net",
        # gcp_jumpoint_subnetwork + gcp_db_network unset → secondary fallbacks.
        "azure_location": "centralus",
        "azure_resource_group": "rg-flat",
        # azure_vnet_resource_group unset → falls back to resource_group.
    })
    g = rc.resolve_region("gcp", "europe-west1")
    assert g["jumpoint_subnetwork"] == "vm-subnet"   # → gcp_subnetwork
    assert g["db_network"] == "vm-net"               # → gcp_network
    a = rc.resolve_region("azure", "westus2")
    assert a["vnet_resource_group"] == "rg-flat"     # → azure_resource_group


# ── merge / save / load ───────────────────────────────────────────────────────

def test_merge_region_fields_no_clobber_across_regions():
    _reset()
    _CONF["aws_region"] = "us-east-2"
    rc.merge_region_fields("aws", {"us-west-2": {"default_subnet_id": "subnet-w"}})
    rc.merge_region_fields("aws", {"eu-west-1": {"ssh_key_secret": "ssh-eu"}})
    # A second field on an existing region also merges without dropping the first.
    rc.merge_region_fields("aws", {"us-west-2": {"ssh_key_secret": "ssh-w"}})
    loaded = rc.load_region_configs("aws")
    assert loaded["us-west-2"] == {"default_subnet_id": "subnet-w", "ssh_key_secret": "ssh-w"}
    assert loaded["eu-west-1"] == {"ssh_key_secret": "ssh-eu"}


def test_save_drops_blanks_and_unknown_fields():
    _reset()
    rc.save_region_configs("gcp", {
        "europe-west1": {"subnetwork": "net-eu", "bogus_field": "x", "zone": ""},
        "empty-region": {"zone": ""},   # no non-blank field → omitted
    })
    loaded = rc.load_region_configs("gcp")
    assert loaded == {"europe-west1": {"subnetwork": "net-eu"}}


# ── wrapper + metadata + errors ───────────────────────────────────────────────

def test_azure_wrapper_matches_resolve_region():
    _reset()
    _CONF.update({"azure_location": "centralus", "azure_resource_group": "rg-x"})
    assert rc.resolve_azure_region("westus2") == rc.resolve_region("azure", "westus2")


def test_region_fields_and_unknown_cloud():
    assert rc.region_fields("aws") == (
        "default_subnet_id", "default_security_group_id", "ssh_key_secret",
        "ssm_instance_profile", "db_subnet_group_name")
    assert "vnet_resource_group" in rc.region_fields("azure")
    for bad in ("oci", "bogus", ""):
        try:
            rc.resolve_region(bad, "us-east-1")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for cloud {bad!r}")


# ── setup.py drift guard + import-key namespace (skips without fastapi) ────────

def test_setup_models_match_region_fields_and_key_namespace():
    try:
        from web_dashboard.api import setup
    except Exception as exc:  # pragma: no cover — fastapi absent outside CI
        print(f"SKIP setup drift guard: {exc}")
        return
    for cloud, model in setup._REGION_CONFIG_MODELS.items():
        assert tuple(model.model_fields.keys()) == rc.region_fields(cloud), (
            f"{cloud} setup model fields drifted from region_fields({cloud})")
    # Dotted region-config namespace maps to the right cloud; flat keys don't match.
    assert setup._region_config_cloud("aws_region.us-west-2.default_subnet_id") == "aws"
    assert setup._region_config_cloud("gcp_region.europe-west1.subnetwork") == "gcp"
    assert setup._region_config_cloud("azure_region.westus2.resource_group") == "azure"
    assert setup._region_config_cloud("aws_region") is None          # flat default-region key
    assert setup._region_config_cloud("azure_region_configs") is None  # flat map key


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
