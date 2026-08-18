"""The Entitle DB forwarder is placed in the *database's* region, not the default one.

``entitle_db_proxy_service`` stands up a socat relay VM so the Entitle agent (in the
GKE VPC) can reach a private Cloud SQL instance. ``_resolve_placement`` used to read
the zone and network straight off the flat config keys::

    region = _cfg("gcp_region") or row.region or ""      # gcp_region is always set,
    zone   = _cfg("gcp_jumpoint_zone") or _cfg("gcp_zone") or f"{region}-b"
    network = _cfg("gcp_db_network") or _cfg("gcp_network") or "default"

— so ``row.region`` was unreachable (the sandbox always emits ``gcp_region``) and a DB
provisioned in a non-default region got a forwarder in the **default** region's zone,
on the **default** region's network. It came up healthy and had no route to the Cloud
SQL private IP it exists to reach. Same bug class as the one fixed in
``cloud_database_service._build_tf_variables``; the fix is the same resolver.

Pinned here:

  * a non-default-region DB gets *its* region's zone and db_network (region entry
    first), and the subnet self-link is built for that region;
  * a region with no config set of its own still never inherits another region's
    zone — it falls to ``<region>-b``, not ``gcp_zone``/``gcp_jumpoint_zone``;
  * the default region, and a row with no region at all, resolve to the flat keys
    exactly as before (single-region installs unchanged);
  * teardown deletes in the DB's zone *and* sweeps the pre-fix default zone, so the
    fix doesn't strand the forwarder an older release parked there.

config_service / config are stubbed (the test_region_config.py pattern) so the real
region_config resolution runs against a dict — no DB, no cloud SDK.

Run: PYTHONPATH=. python tests/test_entitle_db_proxy_placement.py   (or under pytest)
"""
import asyncio
import contextlib
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Stub config_service (a mutable dict) + settings before importing the services, so
# region_config resolves against the dict instead of the app database.
_CONF: dict = {}
_cfg_mod = types.ModuleType("web_dashboard.services.config_service")
_cfg_mod.get = lambda key, default="": _CONF.get(key, default)
_cfg_mod.set = lambda key, value: _CONF.__setitem__(key, value)
_cfg_mod.get_bool = lambda key, default=False: bool(_CONF.get(key, default))
sys.modules["web_dashboard.services.config_service"] = _cfg_mod

_conf_mod = types.ModuleType("web_dashboard.config")


class _Settings:
    def __getattr__(self, _key):
        return ""


_conf_mod.settings = _Settings()
sys.modules["web_dashboard.config"] = _conf_mod

try:
    import web_dashboard.services as _services_pkg
    from web_dashboard.services import entitle_db_proxy_service as edps
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


@contextlib.contextmanager
def _patched(module, **attrs):
    """Swap module attributes for the duration of a test, then put them back."""
    missing = object()
    saved = {k: getattr(module, k, missing) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is missing:
                delattr(module, k)
            else:
                setattr(module, k, v)


class _Row:
    """The CloudDatabase fields _resolve_placement / teardown actually read."""

    def __init__(self, region):
        self.id = "abcd1234-0000-0000-0000-00000000beef"
        self.cloud = "gcp"
        self.region = region
        self.engine = "postgres"
        self.port = 5432
        self.private_host = "10.60.0.3"


def _reset(**extra):
    """The sandbox's single-region output: gcp_region is ALWAYS set — which is why
    the old ``_cfg("gcp_region") or row.region`` never reached ``row.region``."""
    _CONF.clear()
    _CONF.update({
        "gcp_project_id": "proj-1",
        "gcp_region": "us-central1",
        "gcp_zone": "us-central1-b",
        "gcp_db_network": "sandbox-vpc",
        "gcp_jumpoint_subnetwork": "jump-central",
    })
    _CONF.update(extra)


# ── the fix: placement follows the database's own region ──────────────────────

def test_forwarder_lands_in_the_databases_own_region():
    """A europe-west1 Cloud SQL instance gets a europe-west1 forwarder, on the
    network its own region's config set names — the region entry wins over every
    flat key."""
    _reset(gcp_region_configs=json.dumps({"europe-west1": {
        "zone": "europe-west1-c",
        "db_network": "sandbox-vpc-eu",
        "jumpoint_subnetwork": "jump-eu",
    }}))
    project, zone, network, subnetwork = edps._resolve_placement(_Row("europe-west1"))
    assert project == "proj-1"
    assert zone == "europe-west1-c"
    assert network == "sandbox-vpc-eu"
    # The subnet self-link is regional; it must name the DB's region, not us-central1.
    assert subnetwork == "projects/proj-1/regions/europe-west1/subnetworks/jump-eu"


def test_region_without_a_config_set_never_inherits_the_default_zone():
    """Every flat key points at us-central1 (including the gcp_jumpoint_zone
    override). A europe-west1 DB must still land in europe-west1 — the conventional
    ``<region>-b`` — because a zone from elsewhere silently relocates the VM and the
    subnet derived from it."""
    _reset(gcp_jumpoint_zone="us-central1-f")
    _project, zone, network, subnetwork = edps._resolve_placement(_Row("europe-west1"))
    assert zone == "europe-west1-b"
    assert subnetwork == "projects/proj-1/regions/europe-west1/subnetworks/jump-central"
    # No region entry → the flat db_network, which is also what
    # cloud_database_service._build_tf_variables gave the instance itself. Both sides
    # go through resolve_region, so the forwarder is on the DB's own network.
    assert network == "sandbox-vpc"


def test_row_region_wins_over_the_configured_default_region():
    """The regression in one line: gcp_region no longer decides where the VM goes."""
    _reset(gcp_region_configs=json.dumps({"us-west1": {"zone": "us-west1-a"}}))
    assert edps._resolve_placement(_Row("us-west1"))[1] == "us-west1-a"
    assert edps._resolve_placement(_Row("us-central1"))[1] == "us-central1-b"


# ── unchanged for single-region installs ──────────────────────────────────────

def test_default_region_database_resolves_to_the_flat_keys():
    _reset(gcp_region_configs=json.dumps({"europe-west1": {"zone": "europe-west1-c"}}))
    project, zone, network, subnetwork = edps._resolve_placement(_Row("us-central1"))
    assert (project, zone, network) == ("proj-1", "us-central1-b", "sandbox-vpc")
    assert subnetwork == "projects/proj-1/regions/us-central1/subnetworks/jump-central"


def test_row_without_a_region_falls_back_to_the_configured_default():
    """A registered DB may have no region recorded — gcp_region is the fallback."""
    _reset()
    for region in ("", None, "   "):
        assert edps._resolve_placement(_Row(region))[1] == "us-central1-b"


def test_network_keeps_its_final_default_fallback():
    """Nothing configured anywhere still yields GCE's auto network, as before."""
    _reset()
    del _CONF["gcp_db_network"]
    assert edps._resolve_placement(_Row("us-central1"))[2] == "default"


def test_jumpoint_zone_override_still_applies_inside_its_own_region():
    _reset(gcp_jumpoint_zone="us-central1-f")
    assert edps._resolve_placement(_Row("us-central1"))[1] == "us-central1-f"


# ── teardown sweeps the pre-fix zone too ──────────────────────────────────────

def _fake_gcp_service():
    deleted = []

    async def stop_gce_db_forwarder(project_id, zone, name):
        deleted.append((project_id, zone, name))

    return types.SimpleNamespace(stop_gce_db_forwarder=stop_gce_db_forwarder), deleted


def test_teardown_sweeps_the_databases_zone_and_the_legacy_default_zone():
    """A forwarder created before this fix is parked in the default region's zone
    under the same name. Deleting only the (now correct) DB-region zone would leave
    it running forever, so both are swept."""
    _reset(gcp_region_configs=json.dumps({"europe-west1": {"zone": "europe-west1-c"}}))
    fake, deleted = _fake_gcp_service()
    row = _Row("europe-west1")
    with _patched(_services_pkg, gcp_service=fake):
        asyncio.run(edps.teardown_db_forwarder(None, row))
    name = edps._forwarder_name(row.id)
    assert deleted == [("proj-1", "europe-west1-c", name), ("proj-1", "us-central1-b", name)]


def test_teardown_deletes_once_when_the_zone_is_the_default_one():
    _reset()
    fake, deleted = _fake_gcp_service()
    row = _Row("us-central1")
    with _patched(_services_pkg, gcp_service=fake):
        asyncio.run(edps.teardown_db_forwarder(None, row))
    assert deleted == [("proj-1", "us-central1-b", edps._forwarder_name(row.id))]


def test_teardown_is_a_noop_for_a_non_gcp_database():
    _reset()
    fake, deleted = _fake_gcp_service()
    row = _Row("us-east-2")
    row.cloud = "aws"
    with _patched(_services_pkg, gcp_service=fake):
        asyncio.run(edps.teardown_db_forwarder(None, row))
    assert deleted == []


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL: {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
