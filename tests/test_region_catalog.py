"""Multi-region support (Phase 2a): shared region catalog + validators.

services/region_catalog.py is the single source of truth for the per-cloud region
catalog (id + label), the region validators (one authoritative regex per cloud +
GCP zone), and default-region resolution. This pins:

  * validators accept canonical regions (incl. AWS GovCloud + multi-digit
    partitions, GCP zones, OCI) and reject junk;
  * the catalog-validity invariant — every catalogued region id passes its own
    cloud's validator (guards against typos in the hardcoded lists);
  * default-region resolution (config key → fallback) and resolve() semantics
    (blank → default, explicit valid wins, malformed → ValueError).

config_service / config are stubbed so no DB or app wiring is needed.

Run: python tests/test_region_catalog.py   (or under pytest)
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub the two lazy deps region_catalog reaches for (config_service + settings) so
# default_region()/resolve() work without a database.
_CONF: dict = {}
_cfg_mod = types.ModuleType("web_dashboard.services.config_service")
_cfg_mod.get = lambda key, default="": _CONF.get(key, default)
sys.modules["web_dashboard.services.config_service"] = _cfg_mod

_conf_mod = types.ModuleType("web_dashboard.config")


class _Settings:
    def __getattr__(self, _key):
        return ""


_conf_mod.settings = _Settings()
sys.modules["web_dashboard.config"] = _conf_mod

from web_dashboard.services import region_catalog as rc  # noqa: E402


# ── validators ────────────────────────────────────────────────────────────────

def test_validators_accept_canonical_regions():
    assert rc.validate("aws", "us-east-2")
    assert rc.validate("aws", "ap-southeast-3")     # multi-digit partition
    assert rc.validate("aws", "us-gov-west-1")      # GovCloud
    assert rc.validate("gcp", "us-central1")
    assert rc.validate("gcp", "australia-southeast1")
    assert rc.validate("azure", "eastus2")
    assert rc.validate("azure", "East US 2")        # normalised before matching
    assert rc.validate("oci", "us-ashburn-1")
    assert rc.validate("oci", "eu-frankfurt-1")


def test_validators_reject_junk():
    assert not rc.validate("aws", "bogus")
    assert not rc.validate("aws", "us_east_2")
    assert not rc.validate("gcp", "us-central")     # no region index
    assert not rc.validate("azure", "east/us")      # separator not allowed
    assert not rc.validate("oci", "ashburn")


def test_zone_validator_and_region_from_zone():
    assert rc.validate_zone("us-central1-a")
    assert rc.validate_zone("europe-west1-b")
    assert not rc.validate_zone("us-central1")       # region, not a zone
    assert not rc.validate_zone("us-central1-ab")
    assert rc.region_from_zone("us-central1-a") == "us-central1"
    assert rc.region_from_zone("europe-west1-b") == "europe-west1"


def test_normalize():
    assert rc.normalize("azure", "  East US 2 ") == "eastus2"
    assert rc.normalize("aws", " US-East-2 ") == "us-east-2"


# ── catalog-validity invariant ────────────────────────────────────────────────

def test_every_catalogued_region_is_valid_and_lists_nonempty():
    for cloud in rc.CLOUDS:
        ids = rc.region_ids(cloud)
        assert ids, f"{cloud} catalog is empty"
        for rid in ids:
            assert rc.validate(cloud, rid), f"{cloud} catalog id {rid!r} fails its own validator"
        # regions() carries id + label for each entry, in the same order.
        pairs = rc.regions(cloud)
        assert [p["id"] for p in pairs] == ids
        assert all(p["label"] for p in pairs), f"{cloud} has a blank label"


# ── default-region resolution + resolve() ─────────────────────────────────────

def test_default_region_honours_config_then_fallback():
    _CONF.clear()
    assert rc.default_region("aws") == "us-east-2"      # hardcoded fallback
    assert rc.default_region("azure") == "centralus"
    assert rc.default_region("gcp") == "us-central1"
    assert rc.default_region("oci") == "us-ashburn-1"
    _CONF["aws_region"] = "ap-south-1"
    _CONF["azure_location"] = "westeurope"
    assert rc.default_region("aws") == "ap-south-1"
    assert rc.default_region("azure") == "westeurope"
    _CONF.clear()


def test_resolve_semantics():
    _CONF.clear()
    _CONF["gcp_region"] = "europe-west4"
    assert rc.resolve("gcp", None) == "europe-west4"     # blank → configured default
    assert rc.resolve("gcp", "  ") == "europe-west4"
    assert rc.resolve("gcp", "asia-southeast1") == "asia-southeast1"  # explicit valid wins
    assert rc.resolve("azure", "West US 2") == "westus2"  # normalised
    for bad_cloud in ("bogus", ""):
        try:
            rc.resolve(bad_cloud, "us-east-1")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for cloud {bad_cloud!r}")
    try:
        rc.resolve("aws", "not-a-region!")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for malformed region")
    _CONF.clear()


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
