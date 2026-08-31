"""Guard: the image-export zone ladder must try the HUB BUCKET's region first.

The Daisy exporter writes the whole ~17 GB intermediate disk from its worker VM
to the destination (hub) bucket. So the worker's region — not the source image,
which is global — decides whether that write is free or crosses regions.

The ladder used to be anchored purely on the caller's preferred zone, which came
from the GCP/region config. A project whose configured zone lived in a different
region from its hub bucket therefore paid inter-region egress on EVERY export,
first attempt included, not just on a capacity fallback. The 2026-08-31 cost
audit priced it: 67 GB in one day across four us-east1 attempts against a
us-central1 hub = $1.32, which was 15% of that month's entire GCP bill.

Pure ordering only, so it needs neither a cloud account nor google-cloud-*.

Runs under pytest, or standalone:  python tests/test_gcp_export_zone_ladder.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services.gcp_service import _order_export_zones  # noqa: E402

# A realistic slice of UP zones across three US regions. us-east1 deliberately
# has no "-a" (it really doesn't) so the fixture can't accidentally pass by
# assuming a uniform "<region>-a" exists.
UP = [
    "us-central1-a", "us-central1-b", "us-central1-c",
    "us-east1-b", "us-east1-c", "us-east1-d",
    "us-west1-a", "us-west1-b",
]


def _region_of(z):
    return z.rsplit("-", 1)[0]


def test_hub_region_outranks_the_preferred_zone():
    """The exact audited case: config says us-east1, hub bucket is us-central1."""
    order = _order_export_zones(UP, "us-east1-b", hub_region="us-central1")
    assert _region_of(order[0]) == "us-central1", (
        f"first attempt must land in the hub's region, got {order[0]!r} "
        f"(ladder: {order}) — this is the $1.32/day egress bug")


def test_whole_hub_region_is_exhausted_before_leaving_it():
    """Capacity fallback should stay in-region while any zone there is untried:
    leaving early is what turns one blip into a cross-region transfer."""
    order = _order_export_zones(UP, "us-east1-b", hub_region="us-central1")
    hub_zones = [z for z in UP if _region_of(z) == "us-central1"]
    leading = order[:len(hub_zones)]
    assert sorted(leading) == sorted(hub_zones), (
        f"expected all of {hub_zones} before any other region, got {leading} "
        f"(ladder: {order})")


def test_preferred_zone_still_wins_inside_the_hub_region():
    """When the preferred zone is ALREADY in the hub region it keeps priority —
    the hub preference must not shuffle a deliberate zone choice."""
    order = _order_export_zones(UP, "us-central1-c", hub_region="us-central1")
    assert order[0] == "us-central1-c", f"ladder: {order}"


def test_other_regions_are_still_reachable_as_a_last_resort():
    """A whole-region outage must still find somewhere: the point of the ladder.
    Dropping this is the failure mode the hub preference could easily introduce."""
    order = _order_export_zones(UP, "us-east1-b", hub_region="us-central1")
    assert any(_region_of(z) not in ("us-central1",) for z in order), (
        f"no out-of-hub fallback left in the ladder: {order}")
    assert len(set(_region_of(z) for z in order)) >= 2, f"ladder: {order}"


def test_no_duplicate_zones():
    """Each entry is one build attempt; a repeat wastes a full ~17 GB export."""
    for pref, hub in (("us-east1-b", "us-central1"), ("us-central1-a", "us-central1"),
                      ("us-west1-a", ""), ("", "us-east1")):
        order = _order_export_zones(UP, pref, hub_region=hub)
        assert len(order) == len(set(order)), f"dupes for {pref!r}/{hub!r}: {order}"


def test_multi_region_hub_falls_back_to_preferred_zone_behaviour():
    """A multi-region ("US") or dual-region ("NAM4") bucket has no single region
    to match, so the ladder must degrade to the old preferred-zone ordering
    rather than emptying itself out."""
    for hub in ("US", "us", "NAM4", "nam4", ""):
        order = _order_export_zones(UP, "us-east1-b", hub_region=hub)
        assert order and order[0] == "us-east1-b", f"hub={hub!r} → ladder {order}"


def test_hub_region_is_matched_case_insensitively():
    """GCS reports bucket locations UPPERCASE ("US-CENTRAL1") while compute zones
    are lowercase. Comparing them raw silently disables the whole preference."""
    order = _order_export_zones(UP, "us-east1-b", hub_region="US-CENTRAL1")
    assert _region_of(order[0]) == "us-central1", (
        f"uppercase bucket location must still match: {order}")


def test_unenumerable_hub_does_not_break_the_ladder():
    """_gcs_bucket_region_sync returns "" when it cannot read the bucket; the
    export must still run."""
    order = _order_export_zones(UP, "us-west1-a", hub_region="")
    assert order[0] == "us-west1-a", f"ladder: {order}"


def test_limit_is_respected():
    order = _order_export_zones(UP, "us-east1-b", hub_region="us-central1", limit=3)
    assert len(order) == 3, f"ladder: {order}"


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
