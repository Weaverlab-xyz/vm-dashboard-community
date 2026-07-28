"""The gateway deploy form's region picker, and the placement it implies.

The Gateways tab used to take the region as free text. Typing a region that has no
per-region config set is not a validation error — it is worse than that. Every
regional id a gateway needs (subnet, security group, ECS cluster) resolves through
``region_config.resolve_region``, which falls each field back to the flat config
keys, so the host comes up **in the default region's network** while the row, the
job and the operator all say otherwise. GCP adds a second way to get there: the
gateway's subnet is derived from its *zone*, so a stale zone silently relocates the
host no matter which region was picked.

So three things are pinned here:

  * ``region_config.deployable_regions`` — what the picker may offer: the configured
    default region first, then every region with a config set of its own. This is
    also the list the Rancher/Portainer node pickers use, so it has one definition.
  * ``resolve_zone_for_region`` / ``_gcp_jumpoint_zone`` — a GCP gateway's zone
    follows the region it was asked for, and never inherits another region's zone;
    and ``_azure_gateway_location`` — an Azure gateway is built in the region asked
    for, not in whatever ``azure_location`` says. A **blank** region still resolves
    exactly as before, so single-region installs are untouched.
  * ``api/gateways._placement`` — the deploy endpoint validates the pair and refuses
    a zone outside the chosen region, rather than queueing a job that lands elsewhere.

Config-reading collaborators are replaced with spies (the house pattern from
test_compute_region_config.py), so no DB or cloud SDK is needed.

Run: python tests/test_gateway_region_picker.py   (or under pytest)
"""
import contextlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from web_dashboard.api import gateways
    from web_dashboard.services import jumpoint_host_service as jhs
    from web_dashboard.services import region_catalog, region_config
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
    saved = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


def _configs(mapping):
    """A ``load_region_configs`` stand-in that ignores the cloud."""
    return lambda cloud: dict(mapping)


# ── what the picker may offer ─────────────────────────────────────────────────

def test_deployable_regions_lists_the_default_first_then_configured_sets():
    """Default first is not cosmetic: the form preselects the first entry, and the
    default region is the one every flat config key already describes."""
    with _patched(region_catalog, default_region=lambda c: "us-east-2"), \
         _patched(region_config, load_region_configs=_configs(
             {"us-west-2": {"vpc_id": "vpc-w"}, "eu-west-1": {"vpc_id": "vpc-e"}})):
        assert region_config.deployable_regions("aws") == ["us-east-2", "eu-west-1", "us-west-2"]


def test_deployable_regions_never_repeats_the_default_region():
    """The sandbox writes a config set for the default region too."""
    with _patched(region_catalog, default_region=lambda c: "us-central1"), \
         _patched(region_config, load_region_configs=_configs(
             {"us-central1": {"zone": "us-central1-c"}, "us-east1": {"zone": "us-east1-b"}})):
        assert region_config.deployable_regions("gcp") == ["us-central1", "us-east1"]


def test_deployable_regions_on_a_single_region_install_is_just_the_default():
    """No ``<cloud>_region_configs`` at all — the picker offers the one region the
    install actually deploys to today, so nothing changes for it."""
    with _patched(region_catalog, default_region=lambda c: "centralus"), \
         _patched(region_config, load_region_configs=_configs({})):
        assert region_config.deployable_regions("azure") == ["centralus"]


def test_deployable_regions_normalises_what_it_offers():
    """Azure regions are stored space-free and lowercase; a hand-edited config set
    must not put 'West US 2' in a picker that posts it back as a region id."""
    with _patched(region_catalog, default_region=lambda c: "Central US"), \
         _patched(region_config, load_region_configs=_configs({"westus2": {}})):
        assert region_config.deployable_regions("azure") == ["centralus", "westus2"]


def test_deployable_regions_handles_a_cloud_with_no_per_region_sets():
    """OCI carries a region but no config sets — asking must not raise, because the
    endpoint answers for every cloud in the catalog."""
    with _patched(region_catalog, default_region=lambda c: "us-ashburn-1"):
        assert region_config.deployable_regions("oci") == ["us-ashburn-1"]


# ── GCP: a region choice implies a zone ───────────────────────────────────────

def test_zone_in_region():
    assert region_config.zone_in_region("us-east1-b", "us-east1")
    assert not region_config.zone_in_region("us-central1-c", "us-east1")
    # A region is not a zone — GCE would reject it, and the caller would have to
    # find that out from the API instead of from the form.
    assert not region_config.zone_in_region("us-east1", "us-east1")
    assert not region_config.zone_in_region("garbage", "us-east1")
    assert not region_config.zone_in_region("", "us-east1")
    assert not region_config.zone_in_region("us-east1-b", "")


def test_resolve_zone_for_region_prefers_the_regions_own_zone():
    with _patched(region_config, resolve_region=lambda c, r: {"zone": "us-east1-b"}):
        assert region_config.resolve_zone_for_region("us-east1") == "us-east1-b"


def test_resolve_zone_for_region_never_inherits_another_regions_zone():
    """The bug this exists to stop: ``gcp_zone`` describes the default region, and
    handing it back for us-east1 would place the resource in us-central1."""
    with _patched(region_config, resolve_region=lambda c, r: {"zone": "us-central1-c"}):
        assert region_config.resolve_zone_for_region("us-east1") == "us-east1-b"


def test_resolve_zone_for_region_falls_back_to_the_conventional_zone():
    with _patched(region_config, resolve_region=lambda c, r: {"zone": ""}):
        assert region_config.resolve_zone_for_region("europe-west4") == "europe-west4-b"


def test_resolve_zone_for_region_with_no_region_is_the_configured_default():
    """Callers that never ask for a region keep the historical answer."""
    with _patched(region_catalog, default_zone=lambda: "us-central1-a"):
        assert region_config.resolve_zone_for_region("") == "us-central1-a"
        assert region_config.resolve_zone_for_region(None) == "us-central1-a"


# ── the gateway host's zone ───────────────────────────────────────────────────

def _cfg_stub(**cfg):
    return lambda key: cfg.get(key, "")


def test_gateway_zone_follows_the_requested_region():
    """A gateway asked for in us-east1 must not be built from the default region's
    zone — ``_gcp_jumpoint_subnetwork`` derives the subnet from the zone, so that is
    how a us-east1 gateway used to come up in us-central1."""
    with _patched(jhs, _cfg=_cfg_stub(gcp_zone="us-central1-c")), \
         _patched(region_config, resolve_region=lambda c, r: {"zone": "us-east1-b"}):
        assert jhs._gcp_jumpoint_zone("us-east1") == "us-east1-b"


def test_gateway_zone_ignores_an_override_from_another_region():
    """``gcp_jumpoint_zone`` is the gateway's default zone, not a global pin: it
    cannot override the region the operator picked."""
    with _patched(jhs, _cfg=_cfg_stub(gcp_jumpoint_zone="us-central1-f")), \
         _patched(region_config, resolve_region=lambda c, r: {"zone": ""}):
        assert jhs._gcp_jumpoint_zone("us-east1") == "us-east1-b"


def test_gateway_zone_honours_an_override_inside_the_region():
    with _patched(jhs, _cfg=_cfg_stub(gcp_jumpoint_zone="us-east1-d")), \
         _patched(region_config, resolve_region=lambda c, r: {"zone": "us-east1-b"}):
        assert jhs._gcp_jumpoint_zone("us-east1") == "us-east1-d"


def test_azure_gateway_location_prefers_the_requested_region():
    """``azure_location`` is set on every install, so reading it first made the
    region a decoration: the VM came up in the default region (and the teardown then
    looked for it in the wrong resource group)."""
    with _patched(jhs, _cfg=_cfg_stub(azure_location="centralus")):
        assert jhs._azure_gateway_location("westus2") == "westus2"
        assert jhs._azure_gateway_location("West US 2") == "westus2"


def test_azure_gateway_location_falls_back_for_the_managed_host():
    """The managed gateway is ensured without a region — that must still resolve."""
    with _patched(jhs, _cfg=_cfg_stub(azure_location="centralus")):
        assert jhs._azure_gateway_location("") == "centralus"


def test_gateway_zone_without_a_region_keeps_the_historical_chain():
    """The managed gateway is ensured with no region on single-region installs. That
    path must resolve exactly as it did: override, then gcp_zone, then blank."""
    with _patched(jhs, _cfg=_cfg_stub(gcp_jumpoint_zone="us-west1-a", gcp_zone="us-central1-c")):
        assert jhs._gcp_jumpoint_zone("") == "us-west1-a"
    with _patched(jhs, _cfg=_cfg_stub(gcp_zone="us-central1-c")):
        assert jhs._gcp_jumpoint_zone("") == "us-central1-c"
    with _patched(jhs, _cfg=_cfg_stub()):
        assert jhs._gcp_jumpoint_zone("") == ""


# ── what the deploy endpoint accepts ──────────────────────────────────────────

def _req(cloud, region="", zone=""):
    return gateways.DeployGatewayRequest(cloud=cloud, region=region, zone=zone, name="gw-01")


def _placement_error(cloud, region="", zone=""):
    try:
        gateways._placement(_req(cloud, region, zone))
    except Exception as exc:  # HTTPException
        return getattr(exc, "detail", str(exc))
    return None


def test_placement_normalises_the_pair():
    assert gateways._placement(_req("aws", " US-East-2 ")) == ("us-east-2", "")
    assert gateways._placement(_req("gcp", "us-east1", "US-East1-B")) == ("us-east1", "us-east1-b")
    assert gateways._placement(_req("azure", "West US 2")) == ("westus2", "")


def test_placement_leaves_a_blank_region_blank():
    """Blank means 'the configured default', resolved at deploy time — not an error."""
    assert gateways._placement(_req("aws")) == ("", "")


def test_placement_rejects_a_malformed_region():
    detail = _placement_error("aws", "us_east_2")
    assert detail and "region" in detail.lower(), detail


def test_placement_rejects_a_zone_outside_the_region():
    """The check that makes the picker mean something: the subnet follows the zone."""
    detail = _placement_error("gcp", "us-east1", "us-central1-c")
    assert detail and "us-east1" in detail, detail


def test_placement_rejects_a_malformed_zone():
    detail = _placement_error("gcp", "us-east1", "us-east1")
    assert detail and "zone" in detail.lower(), detail


def test_placement_drops_a_zone_for_the_clouds_that_have_none():
    """Only GCP places the host in a zone; carrying one on an AWS row would be a
    lie the teardown might read back."""
    assert gateways._placement(_req("aws", "us-east-2", "us-east-2a")) == ("us-east-2", "")


# ── the form is actually wired to the picker ──────────────────────────────────

def _template():
    return open(os.path.join(_ROOT, "web_dashboard", "templates", "containers",
                             "index.html"), encoding="utf-8").read()


def test_the_form_binds_the_region_to_a_picker_not_a_text_box():
    src = _template()
    form = src[src.index("BeyondTrust Gateways"):src.index("<!-- Gateway list -->")]
    assert 'x-model="gwForm.region"' in form, "the gateway form lost its region binding"
    assert 'type="text" x-model="gwForm.region"' not in form, (
        "the region is free text again — a region with no config set puts the gateway "
        "on the default region's network")
    assert 'x-for="r in gwRegions"' in form, "the region select is not fed by gwRegions"


def test_the_picker_is_fed_by_the_configured_regions():
    src = _template()
    assert "/api/regions?cloud=" in src, "the form no longer asks for the region list"
    assert "data.configured" in src, (
        "the picker reads something other than the configured region sets — the full "
        "catalog would offer regions with no subnet")


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
