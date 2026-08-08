"""Guard: exported region config survives the importer, and doesn't clobber.

Two separate hazards live here, and neither raises.

**Silent drop.** ``/api/setup/import`` validates each
``<cloud>_region.<region>.<field>`` key against that cloud's pydantic model and
discards anything it doesn't recognise, logging only. A field the exporter emits
but the model rejects produces a target that looks multi-region and isn't — the
same failure ``tests/test_sandbox_region_keys.py`` exists to catch for the
sandbox scripts, arriving by a second route.

**Silent clobber.** The flat ``<cloud>_region_configs`` blob does round-trip
through the importer, but that write is replace-on-save. Shipping the blob to a
target that already has region config of its own deletes every entry the source
doesn't share, and returns success. The exporter must emit dotted keys, which
route to ``merge_region_fields`` instead.

Runs under pytest, or standalone:  python tests/test_config_migrate_regions.py
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.api.setup import _REGION_CONFIG_MODELS, _region_config_cloud
from web_dashboard.scripts.config_migrate import regions
from web_dashboard.services.region_config import REGION_CONFIG_CLOUDS, region_fields


def _sample(cloud: str, region: str) -> dict:
    """Every field the resolver knows for ``cloud``, with placeholder values."""
    return {field: f"{region}-{field}" for field in region_fields(cloud)}


def test_every_emitted_key_is_accepted_by_the_importer():
    """The exporter and the import validator must agree, field for field.

    Mirrors the parser in ``api/setup.import_config``: split on ``.`` twice,
    then check ``parts[2] in model.model_fields``.
    """
    region_map = {cloud: {"testregion": _sample(cloud, "testregion")}
                  for cloud in REGION_CONFIG_CLOUDS}
    keys, dropped = regions.to_import_keys(region_map)
    assert not dropped, f"exporter emitted fields it knows are unimportable: {dropped}"

    failures = []
    for dotted in keys:
        cloud = _region_config_cloud(dotted)
        assert cloud, f"{dotted} does not match the importer's region namespace"
        parts = dotted.split(".", 2)
        if len(parts) != 3 or not parts[1]:
            failures.append(f"{dotted}: malformed shape")
        elif parts[2] not in _REGION_CONFIG_MODELS[cloud].model_fields:
            failures.append(f"{dotted}: /api/setup/import would drop {parts[2]!r}")
    assert not failures, "Unimportable region keys:\n  " + "\n  ".join(failures)


def test_resolver_and_import_model_agree():
    """``region_fields`` drives the export; the pydantic model gates the import.

    A field in one and not the other is invisible at both ends: the exporter
    happily emits it and the importer happily discards it.
    """
    for cloud in REGION_CONFIG_CLOUDS:
        resolver = set(region_fields(cloud))
        model = set(_REGION_CONFIG_MODELS[cloud].model_fields)
        assert resolver == model, (
            f"{cloud}: region_config knows {sorted(resolver - model)} that the import "
            f"model rejects; the model declares {sorted(model - resolver)} that the "
            f"resolver never reads")


def test_blob_is_exploded_not_shipped_whole():
    """The clobber guard. After extraction the flat key must be gone."""
    blob = json.dumps({"eastus2": {"resource_group": "rg-e2"},
                       "westus2": {"resource_group": "rg-w2"}})
    config = {"azure_location": "eastus2", "azure_region_configs": blob, "other": "x"}

    remaining, region_map, malformed = regions.extract(config)
    assert "azure_region_configs" not in remaining, (
        "the flat blob survived extraction — importing it is replace-on-save and "
        "would delete region sets that exist only on the target")
    assert remaining == {"azure_location": "eastus2", "other": "x"}
    assert not malformed
    assert sorted(region_map["azure"]) == ["eastus2", "westus2"]

    keys, _dropped = regions.to_import_keys(region_map)
    assert keys["azure_region.eastus2.resource_group"] == "rg-e2"
    assert keys["azure_region.westus2.resource_group"] == "rg-w2"


def test_round_trip_preserves_every_field():
    """extract → to_import_keys loses nothing for any cloud."""
    for cloud in REGION_CONFIG_CLOUDS:
        original = {"r1": _sample(cloud, "r1"), "r2": _sample(cloud, "r2")}
        config = {regions.BLOB_KEYS[cloud]: json.dumps(original)}

        _remaining, region_map, _malformed = regions.extract(config)
        keys, dropped = regions.to_import_keys(region_map)
        assert not dropped, f"{cloud}: {dropped}"

        rebuilt: dict = {}
        for dotted, value in keys.items():
            _prefix, region, field = dotted.split(".", 2)
            rebuilt.setdefault(region, {})[field] = value
        assert rebuilt == original, f"{cloud}: round trip lost or altered fields"


def test_unknown_field_is_reported_not_silently_dropped():
    """A stale field in the source blob is named, not swallowed."""
    blob = json.dumps({"eastus2": {"resource_group": "rg", "retired_field": "x"}})
    _remaining, region_map, _malformed = regions.extract({"azure_region_configs": blob})
    keys, dropped = regions.to_import_keys(region_map)
    assert "azure_region.eastus2.resource_group" in keys
    assert dropped == ["azure_region.eastus2.retired_field"]


def test_malformed_blob_is_reported_and_not_carried():
    """An unparseable blob is already inert on the source (load_region_configs
    logs and falls back to flat keys). Carrying it would propagate the breakage;
    dropping it quietly would hide it."""
    _remaining, region_map, malformed = regions.extract(
        {"gcp_region_configs": "{not json"})
    assert not region_map
    assert malformed == ["gcp_region_configs"]

    # An empty map is not a malformation and needs no warning.
    _remaining, _rm, malformed = regions.extract({"gcp_region_configs": "{}"})
    assert not malformed


def test_blank_values_are_omitted():
    """``merge_region_fields`` skips blanks; sending them only pads the diff."""
    blob = json.dumps({"eastus2": {"resource_group": "rg", "db_subnet_id": "  "}})
    _remaining, region_map, _malformed = regions.extract({"azure_region_configs": blob})
    keys, _dropped = regions.to_import_keys(region_map)
    assert "azure_region.eastus2.db_subnet_id" not in keys
    assert keys["azure_region.eastus2.resource_group"] == "rg"


def test_replace_mode_reproduces_the_blob_key():
    """``--regions replace`` is the deliberate mirror, and must emit the flat key
    the replace-on-save path reads."""
    blob = json.dumps({"eastus2": {"resource_group": "rg-e2"}})
    _remaining, region_map, _malformed = regions.extract({"azure_region_configs": blob})
    out = regions.to_replace_keys(region_map)
    assert list(out) == ["azure_region_configs"]
    assert json.loads(out["azure_region_configs"]) == {"eastus2": {"resource_group": "rg-e2"}}
    # And it must NOT match the dotted namespace, or it would be re-routed.
    assert _region_config_cloud("azure_region_configs") is None


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
