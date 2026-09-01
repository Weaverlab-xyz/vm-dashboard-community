"""The per-POV spend cap: accrual arithmetic, latches, and what must NOT happen.

The auto-delete timer answers "how long may this POV live?". This answers the question an
operator on their own cloud account actually loses sleep over — and the design rests on
one choice: **the number is accrued from list prices every sweep, never read off a bill.**
A bill lags a day, so a cap driven by one would report a runaway rather than stop it.

What is pinned here:

  * the first sweep accrues NOTHING, so enabling this selects nothing by construction;
  * storage accrues while the POV is suspended, because otherwise a POV left asleep for a
    month would never reach its cap — which would contradict everything else this feature
    says about suspend not stopping the bill;
  * a cloud with no price source accrues nothing and never acts, rather than reading
    "unknown" as "free";
  * warn-once and act-once latches, and that raising the cap clears both;
  * the cap SUSPENDS and never destroys, and defaults to warning only.

Pure policy: no database, no clock, no cloud.

Runs under pytest, or standalone:
    python tests/test_pov_spend.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-spend")

from web_dashboard.services import pov_cloud_cost as cost  # noqa: E402
from web_dashboard.services import pov_spend as spend  # noqa: E402

_UTC = timezone.utc
_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=_UTC)


class Row:
    def __init__(self, cap=None, spent=0.0, accrued_at=None, warned=None, capped=None):
        self.spend_cap_usd = cap
        self.spend_estimate_usd = spent
        self.spend_accrued_at = accrued_at
        self.spend_warned_at = warned
        self.spend_capped_at = capped


def _code(module_path: str, fn: str) -> str:
    """A function's executable source: no comments, no docstring.

    Same helper as the cloud-driver suites, for the same reason — this module documents
    the things it deliberately does not do, and a line-filtered scan would read the
    warning as the bug.
    """
    import ast
    src = open(os.path.join(_ROOT, module_path), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.unparse(s) for s in body).replace("'", '"')
    raise AssertionError(f"no function named {fn} in {module_path}")


# ── accrual ──────────────────────────────────────────────────────────────────

def test_the_first_sweep_accrues_nothing():
    """A NULL `accrued_at` means "never measured", and an unbounded interval would bill
    this POV for every hour since the epoch. Same rule the suspend schedule's latch and
    `expires_at IS NULL` both follow: enabling a feature selects nothing by construction
    rather than by a guard."""
    total, at, added = spend.accrue(None, None, 1.0, _T0)
    assert total == 0.0 and added == 0.0
    assert at == _T0, "the first sweep must still record the time"


def test_an_hour_at_a_dollar_accrues_a_dollar():
    total, at, added = spend.accrue(0.0, _T0, 1.0, _T0 + timedelta(hours=1))
    assert round(added, 6) == 1.0
    assert round(total, 6) == 1.0
    assert at == _T0 + timedelta(hours=1)


def test_accrual_adds_to_what_is_already_there():
    total, _at, _added = spend.accrue(10.0, _T0, 2.0, _T0 + timedelta(minutes=30))
    assert round(total, 4) == 11.0


def test_a_ten_minute_sweep_accrues_a_sixth_of_the_hourly_rate():
    """The real cadence — the reconcile pass runs every ten minutes."""
    _total, _at, added = spend.accrue(0.0, _T0, 6.0, _T0 + timedelta(minutes=10))
    assert round(added, 6) == 1.0


def test_no_price_source_accrues_nothing_but_still_moves_the_clock():
    """`None` is not zero. A cap that read "unknown" as "free" would never act on a cloud
    it cannot price — and leaving the clock behind would let a rate that appears later
    bill retroactively for the blind period."""
    total, at, added = spend.accrue(5.0, _T0, None, _T0 + timedelta(hours=3))
    assert total == 5.0 and added == 0.0
    assert at == _T0 + timedelta(hours=3), "the clock did not move on"


def test_a_long_outage_does_not_invent_an_unbounded_bill():
    """A dashboard down for days comes back and applies the CURRENT rate across the whole
    gap, which it cannot know was right — the POV may have been suspended for most of it.
    Capped, but generously: a POV that really did run really did cost that money."""
    _total, _at, added = spend.accrue(0.0, _T0, 1.0, _T0 + timedelta(days=30))
    assert added == spend.MAX_ACCRUAL_HOURS
    assert spend.MAX_ACCRUAL_HOURS >= 24, "the cap is tight enough to understate a real day"


def test_a_naive_timestamp_is_read_as_utc():
    """SQLAlchemy hands back naive datetimes. Treating one as local would shift every
    accrual by the container's timezone offset."""
    naive = _T0.replace(tzinfo=None)
    _total, _at, added = spend.accrue(0.0, naive, 1.0, _T0 + timedelta(hours=2))
    assert round(added, 6) == 2.0


def test_a_clock_that_went_backwards_accrues_nothing():
    total, at, added = spend.accrue(7.0, _T0, 1.0, _T0 - timedelta(hours=1))
    assert total == 7.0 and added == 0.0
    assert at == _T0, "a backwards clock must not rewind the latch"


def test_a_negative_rate_never_refunds():
    _total, _at, added = spend.accrue(0.0, _T0, -5.0, _T0 + timedelta(hours=1))
    assert added == 0.0


# ── storage is what makes a suspended POV reach its cap ──────────────────────

def _env(vms):
    return {"id": "povenv-x", "vms": vms}


def _vm(runstate="running", itype="t3.medium", disk=100):
    return {"runstate": runstate, "instance_type": itype, "disk_gb": disk,
            "os_family": "linux"}


def _rate(env, region, cloud):
    return asyncio.run(cost.rate_usd_per_hour(env, region, cloud))


def test_storage_accrues_while_every_vm_is_suspended():
    """The point of the whole feature. Compute stops when a POV is suspended and the disks
    do not — so a rate that counted only running instances would let a POV sleep for a
    month and never reach its cap, contradicting everything else the page says."""
    original = cost.storage_gb_month
    cost.storage_gb_month = lambda region: 0.08
    try:
        rate = _rate(_env([_vm("stopped"), _vm("stopped")]), "us-east-2", "aws")
    finally:
        cost.storage_gb_month = original
    assert rate is not None and rate > 0, "a suspended POV accrues nothing"
    # 200 GB at $0.08/GB-month over 730 hours.
    assert round(rate, 6) == round(200 * 0.08 / cost.HOURS_PER_MONTH, 6)


def test_compute_counts_only_for_running_vms():
    original_s, original_i = cost.storage_gb_month, cost.instance_hourly
    cost.storage_gb_month = lambda region: 0.0
    cost.instance_hourly = lambda region, itype, os_family="linux": 1.0
    try:
        both = _rate(_env([_vm("running"), _vm("running")]), "us-east-2", "aws")
        one = _rate(_env([_vm("running"), _vm("stopped")]), "us-east-2", "aws")
    finally:
        cost.storage_gb_month, cost.instance_hourly = original_s, original_i
    assert both == 2.0 and one == 1.0


def test_an_unpriced_cloud_returns_none_rather_than_zero():
    for cloud in ("skytap", "gcp", "oci"):
        if cost.priced(cloud):
            continue
        assert _rate(_env([_vm()]), "us-east-2", cloud) is None, (
            f"{cloud} reports a rate but has no price source, so a cap there would "
            f"accrue a confident zero and never act")


def test_a_priced_cloud_that_answers_nothing_is_unknown_not_free():
    """An environment with VMs and no price for any of them must read as UNKNOWN. A
    confident 0.0 would let a cap sit at zero for a whole evaluation and never act."""
    original_s, original_i = cost.storage_gb_month, cost.instance_hourly
    cost.storage_gb_month = lambda region: None
    cost.instance_hourly = lambda region, itype, os_family="linux": None
    try:
        assert _rate(_env([_vm("running")]), "us-east-2", "aws") is None
    finally:
        cost.storage_gb_month, cost.instance_hourly = original_s, original_i


def test_the_priced_clouds_are_the_built_ones_not_the_planned_ones():
    """Same discipline as `lab_platforms.CLOUD_PLATFORMS`: a cloud listed without a
    working lookup would offer a cap that silently never accrues."""
    from web_dashboard.services import lab_platforms as lp
    for cloud in cost.PRICED_CLOUDS:
        assert cloud in lp.CLOUD_PLATFORMS, f"{cloud} is priced but is not a POV cloud"
    assert cost.no_price_reason("gcp"), "an unpriced cloud has no reason to show"
    assert "Skytap" in cost.no_price_reason("skytap"), \
        "Skytap's reason should say it bills the lab, not the VM"


# ── Azure pricing: tiers, and the SKUs that must not be picked ───────────────

def _with_azure_items(items):
    """Stand in for the Retail Prices API. No network, no auth, no cache carry-over."""
    original = cost._azure_query
    cost._prices.clear()
    cost._azure_query = lambda f: items
    return original


def _restore_azure(original):
    cost._azure_query = original
    cost._prices.clear()


def test_azure_is_priced_and_needs_no_credentials():
    """The Retail Prices API is public. That is the whole reason Azure is the second cloud
    to get a price source rather than the last — AWS's needs an IAM permission an
    EC2-scoped key does not have."""
    assert cost.priced("azure") is True
    body = _code(os.path.join("web_dashboard", "services", "pov_cloud_cost.py"),
                 "_azure_query")
    assert "httpx.get" in body
    for banned in ("credential", "_oci_config", "_aws_kwargs", "_ensure_creds", "token"):
        assert banned not in body.lower(), f"the Azure price lookup reaches for {banned}"


def test_a_disk_is_billed_by_the_tier_it_rounds_up_into():
    """Azure bills a managed disk by SIZE TIER, not per GB. A 30 GB and a 32 GB Standard
    SSD cost exactly the same; a 33 GB one costs roughly double. Multiplying a per-GB
    figure would be wrong in both directions."""
    assert cost.azure_disk_sku(30) == "E4 LRS", "a 30 GB disk is billed as a 32 GiB E4"
    assert cost.azure_disk_sku(32) == "E4 LRS"
    assert cost.azure_disk_sku(33) == "E6 LRS", "33 GB rounds UP to the next tier"
    assert cost.azure_disk_sku(1) == "E1 LRS"
    assert cost.azure_disk_sku(1024) == "E30 LRS"


def test_a_disk_with_no_tier_has_no_price_rather_than_a_guess():
    assert cost.azure_disk_sku(0) == ""
    assert cost.azure_disk_sku(-5) == ""
    assert cost.azure_disk_sku(99_999_999) == "", "a disk past the last tier was guessed"


def test_the_tiers_are_ordered_and_never_duplicated():
    ceilings = [c for c, _sku in cost._AZURE_SSD_TIERS]
    assert ceilings == sorted(ceilings), (
        "the tier table is out of order, so a small disk could match a large tier")
    assert len(set(ceilings)) == len(ceilings), "a duplicate ceiling makes one tier dead"


def test_a_spot_price_is_never_taken():
    """Spot and Low Priority are cheaper and would understate a cap — the one direction an
    estimate behind a cap must not err."""
    original = _with_azure_items([
        {"meterName": "D2s v3 Spot", "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.01},
        {"meterName": "D2s v3", "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.10},
    ])
    try:
        assert cost._azure_instance_hourly("eastus", "Standard_D2s_v3", "linux") == 0.10
    finally:
        _restore_azure(original)


def test_a_low_priority_price_is_never_taken():
    original = _with_azure_items([
        {"meterName": "D2s v3 Low Priority",
         "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.02},
        {"meterName": "D2s v3", "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.10},
    ])
    try:
        assert cost._azure_instance_hourly("eastus", "Standard_D2s_v3", "linux") == 0.10
    finally:
        _restore_azure(original)


def test_windows_and_linux_are_told_apart_by_the_product_name():
    """A Windows SKU carries the licence and costs more. The two differ only by a
    substring of `productName`, so taking the cheaper one would bill a Windows POV at
    Linux rates."""
    items = [
        {"meterName": "D2s v3", "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.10},
        {"meterName": "D2s v3", "productName": "Virtual Machines Dsv3 Series Windows",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.28},
    ]
    original = _with_azure_items(items)
    try:
        assert cost._azure_instance_hourly("eastus", "Standard_D2s_v3", "linux") == 0.10
        cost._prices.clear()
        assert cost._azure_instance_hourly("eastus", "Standard_D2s_v3", "windows") == 0.28
    finally:
        _restore_azure(original)


def test_a_price_that_is_not_per_hour_is_ignored():
    original = _with_azure_items([
        {"meterName": "D2s v3", "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Month", "retailPrice": 70.0},
    ])
    try:
        assert cost._azure_instance_hourly("eastus", "Standard_D2s_v3", "linux") is None
    finally:
        _restore_azure(original)


def test_a_disk_price_must_be_a_standard_ssd_managed_disk():
    """The same skuName appears against other products in the catalogue."""
    original = _with_azure_items([
        {"productName": "Premium SSD Managed Disks", "retailPrice": 9.99},
        {"productName": "Standard SSD Managed Disks", "retailPrice": 2.40},
    ])
    try:
        assert cost._azure_disk_monthly("eastus", 30) == 2.40
    finally:
        _restore_azure(original)


def test_an_azure_environment_prices_compute_and_its_disks():
    original = _with_azure_items([
        {"meterName": "D2s v3", "productName": "Virtual Machines Dsv3 Series",
         "unitOfMeasure": "1 Hour", "retailPrice": 0.10},
    ])
    disk_original = cost._azure_disk_monthly
    cost._azure_disk_monthly = lambda region, gb: 2.40
    try:
        running = _rate(_env([_vm("running", "Standard_D2s_v3", 30)]), "eastus", "azure")
        stopped = _rate(_env([_vm("stopped", "Standard_D2s_v3", 30)]), "eastus", "azure")
    finally:
        cost._azure_disk_monthly = disk_original
        _restore_azure(original)
    disk_hourly = 2.40 / cost.HOURS_PER_MONTH
    assert round(running, 6) == round(0.10 + disk_hourly, 6)
    assert round(stopped, 6) == round(disk_hourly, 6), \
        "a suspended Azure POV must still accrue its disk"


def test_the_price_table_matches_the_disk_the_azure_driver_actually_creates():
    """A cross-module invariant with no other guard. The E-series tiers priced here are
    Standard SSD; if the driver were changed to Premium the lookup would keep answering,
    with a number roughly a third of the real one — an estimate quietly wrong in the
    direction a cap must not err."""
    from web_dashboard.services import pov_cloud_azure
    assert pov_cloud_azure._DISK_SKU == "StandardSSD_LRS", (
        f"the driver now creates {pov_cloud_azure._DISK_SKU} disks, but the price table "
        f"here still prices Standard SSD (E-series) tiers")
    assert all(sku.startswith("E") for _c, sku in cost._AZURE_SSD_TIERS)


def test_azure_needs_no_region_name_map_the_way_aws_does():
    """AWS's Pricing API wants a human region NAME this module keeps a map of, so an
    unmapped region has no answer. Azure's takes the region id directly, so every region
    is in scope and a cap works the day a new one opens."""
    assert cost._priceable("azure", "some-new-region-1") is True
    assert cost._priceable("aws", "some-new-region-1") is False


def test_the_price_lookups_run_off_the_event_loop():
    """Memoised for six hours, so this is a dict read on all but the first pass after a
    restart — but that first pass is real HTTP per distinct shape, and doing it inline
    would block the worker's loop and every job queued behind it."""
    path = os.path.join("web_dashboard", "services", "pov_cloud_cost.py")
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    for fn in ("rate_usd_per_hour", "estimate"):
        assert f"async def {fn}(" in src, f"{fn} is not async"
        assert "cloud_executor.run" in _code(path, fn), f"{fn} does its lookups on the loop"


# ── GCP pricing: cores and memory, priced separately ─────────────────────────

def _sku(description, group, price, region="us-central1", usage="OnDemand"):
    """One Cloud Billing Catalog SKU, in the shape the real API returns."""
    units = int(price)
    return {
        "description": description,
        "category": {"resourceGroup": group, "usageType": usage},
        "serviceRegions": [region],
        "pricingInfo": [{"pricingExpression": {"tieredRates": [
            {"unitPrice": {"units": str(units),
                           "nanos": int(round((price - units) * 1e9))}}]}}],
    }


def _gcp_index(skus, region="us-central1"):
    """Build the index the way `_gcp_catalog` does, without the HTTP or the cache."""
    index = {"cpu": {}, "ram": {}, "disk": None}
    for sku in skus:
        cost._gcp_index_sku(index, sku, region)
    return index


def test_gcp_is_priced():
    assert cost.priced("gcp") is True
    assert cost._priceable("gcp", "us-central1") is True


def test_a_machine_type_is_cores_plus_memory_not_one_sku():
    """GCE does not price a machine type. `n2-standard-4` is four units of "N2 Instance
    Core" plus sixteen of "N2 Instance Ram", so the shape's vCPU and memory have to come
    from the Compute API before the catalogue can say anything."""
    index = _gcp_index([
        _sku("N2 Instance Core running in Americas", "CPU", 0.031611),
        _sku("N2 Instance Ram running in Americas", "RAM", 0.004237),
    ])
    assert index["cpu"]["N2"] == 0.031611
    assert index["ram"]["N2"] == 0.004237

    original_c, original_s = cost._gcp_catalog, cost._gcp_machine_spec
    cost._gcp_catalog = lambda region: index
    cost._gcp_machine_spec = lambda region, mt: (4, 16.0)
    try:
        price = cost._gcp_instance_hourly("us-central1", "n2-standard-4", "linux")
    finally:
        cost._gcp_catalog, cost._gcp_machine_spec = original_c, original_s
    assert round(price, 6) == round(4 * 0.031611 + 16.0 * 0.004237, 6)


def test_n2d_is_never_read_as_n2():
    """The family lives ONLY in the SKU description. A prefix match without the space
    would price an N2D VM off N2 rates, which is a wrong number rather than no number —
    the failure mode this whole module is arranged to avoid."""
    index = _gcp_index([
        _sku("N2D Instance Core running in Americas", "CPU", 0.027),
        _sku("N2D Instance Ram running in Americas", "RAM", 0.0036),
    ])
    assert "N2" not in index["cpu"], "N2D was indexed as N2"
    assert index["cpu"]["N2D"] == 0.027


def test_preemptible_and_committed_skus_are_never_indexed():
    """Preemptible is cheaper and would understate a cap; a commitment rate is not what an
    un-discounted POV pays."""
    index = _gcp_index([
        _sku("N2 Instance Core running in Americas", "CPU", 0.007, usage="Preemptible"),
        _sku("Commitment v1: N2 Cpu in Americas", "CPU", 0.019, usage="Commit1Yr"),
    ])
    assert index["cpu"] == {}, f"a discounted SKU was indexed: {index['cpu']}"


def test_a_sku_from_another_region_is_never_indexed():
    index = _gcp_index([
        _sku("N2 Instance Core running in Europe", "CPU", 0.035, region="europe-west1"),
    ])
    assert index["cpu"] == {}, "a SKU from another region was priced as local"


def test_the_disk_sku_is_the_one_the_driver_actually_creates():
    """`pov_cloud_gcp` creates pd-balanced. The catalogue calls that "Balanced PD
    Capacity", and matching the wrong description would price a different disk class."""
    from web_dashboard.services import pov_cloud_gcp
    assert pov_cloud_gcp._DISK_TYPE == "pd-balanced", (
        f"the driver now creates {pov_cloud_gcp._DISK_TYPE} disks, but the catalogue "
        f"match here still looks for Balanced PD Capacity")
    index = _gcp_index([
        _sku("SSD backed PD Capacity", "SSD", 0.170),
        _sku("Balanced PD Capacity in Americas", "SSD", 0.100),
    ])
    assert index["disk"] == 0.100


def test_a_zero_price_tier_is_skipped():
    """Promotional and free tiers appear first in `tieredRates`."""
    sku = _sku("N2 Instance Core running in Americas", "CPU", 0.031611)
    sku["pricingInfo"][0]["pricingExpression"]["tieredRates"].insert(
        0, {"unitPrice": {"units": "0", "nanos": 0}})
    assert round(cost._gcp_sku_price(sku), 6) == 0.031611


def test_a_custom_machine_type_has_no_price_rather_than_a_guess():
    assert cost._gcp_instance_hourly("us-central1", "custom-4-16384", "linux") is None
    assert cost._gcp_instance_hourly("us-central1", "", "linux") is None


def test_a_family_the_catalogue_never_mentioned_has_no_price():
    original_c, original_s = cost._gcp_catalog, cost._gcp_machine_spec
    cost._gcp_catalog = lambda region: {"cpu": {}, "ram": {}, "disk": None}
    cost._gcp_machine_spec = lambda region, mt: (4, 16.0)
    try:
        assert cost._gcp_instance_hourly("us-central1", "z9-standard-4", "linux") is None
    finally:
        cost._gcp_catalog, cost._gcp_machine_spec = original_c, original_s


def test_the_catalogue_walk_is_bounded():
    """There is no server-side filter, so this lists every Compute Engine SKU. A paging
    bug on either side must not turn one price lookup into an unbounded download."""
    assert cost._GCP_MAX_PAGES <= 10
    body = _code(os.path.join("web_dashboard", "services", "pov_cloud_cost.py"),
                 "_gcp_catalog")
    assert "_GCP_MAX_PAGES" in body, "the catalogue walk has no page ceiling"
    assert "nextPageToken" in body


def test_an_empty_catalogue_is_not_cached_as_an_answer():
    """Caching a failed read would pin it for six hours — long enough that an operator who
    enables the billing API sees no change and concludes it did not help."""
    body = _code(os.path.join("web_dashboard", "services", "pov_cloud_cost.py"),
                 "_gcp_catalog")
    guarded = body.split("except Exception", 1)[1]
    # The GUARD itself, not merely a `return` — the tail of the function has one anyway,
    # so searching for that alone passed with the guard deleted. Verified by deleting it.
    assert 'if not index["cpu"]' in guarded, (
        "a failed catalogue read is cached as an answer, pinning the failure for six "
        "hours — long enough that an operator who enables the billing API sees no change "
        "and concludes it did not help")
    assert guarded.index("return index") < guarded.index("_prices[key]"), \
        "the early return comes after the cache write, so it never runs"


def test_a_gcp_environment_prices_compute_and_its_disks():
    original_c, original_s = cost._gcp_catalog, cost._gcp_machine_spec
    cost._gcp_catalog = lambda region: {"cpu": {"N2": 0.03}, "ram": {"N2": 0.004},
                                        "disk": 0.10}
    cost._gcp_machine_spec = lambda region, mt: (2, 8.0)
    try:
        running = _rate(_env([_vm("running", "n2-standard-2", 30)]), "us-central1", "gcp")
        stopped = _rate(_env([_vm("stopped", "n2-standard-2", 30)]), "us-central1", "gcp")
    finally:
        cost._gcp_catalog, cost._gcp_machine_spec = original_c, original_s
    disk_hourly = 30 * 0.10 / cost.HOURS_PER_MONTH
    assert round(running, 6) == round(2 * 0.03 + 8.0 * 0.004 + disk_hourly, 6)
    assert round(stopped, 6) == round(disk_hourly, 6), \
        "a suspended GCP POV must still accrue its disk"


def test_the_gcp_reason_names_the_api_to_enable():
    """Unlike AWS's, GCP's catalogue needs no IAM role — but the API has to be on, and an
    unenabled one answers 403, which reads like a permissions problem.

    Asserted on the service name and the instruction separately, rather than on the whole
    hostname. A dotted host in a membership test is the shape of a URL allow-list check,
    and CodeQL's `py/incomplete-url-substring-sanitization` flags it — correctly in
    general, since a substring is a weak way to validate a URL. It is not what this line
    does, but the fix is to stop writing the shape rather than to suppress the query.
    """
    reason = cost._no_answer_reason("gcp", "us-central1")
    assert "cloudbilling" in reason, "the reason does not name the API"
    assert "enabled on the project" in reason, "the reason does not say what to do"
    assert "IAM" in reason, (
        "the reason should say no extra role is needed, or an operator goes looking for "
        "a permission that does not exist")


# ── the thresholds and their latches ─────────────────────────────────────────

def test_nothing_happens_without_a_cap():
    assert spend.state(Row(cap=None, spent=9999.0)) == ""
    assert spend.state(Row(cap=0, spent=9999.0)) == ""


def test_a_warning_fires_once_at_the_threshold():
    assert spend.state(Row(cap=100.0, spent=79.0), warn_at_percent=80) == ""
    assert spend.state(Row(cap=100.0, spent=80.0), warn_at_percent=80) == "warn"
    already = Row(cap=100.0, spent=85.0, warned=_T0)
    assert spend.state(already, warn_at_percent=80) == "", "the warning repeated"


def test_the_cap_fires_once_and_outranks_the_warning():
    assert spend.state(Row(cap=100.0, spent=100.0)) == "cap"
    assert spend.state(Row(cap=100.0, spent=250.0)) == "cap"
    assert spend.state(Row(cap=100.0, spent=100.0, capped=_T0)) == "", "it re-fired"


def test_a_row_over_its_cap_reports_cap_even_if_it_was_only_warned():
    """The warning latch must not swallow the cap. A POV that warned at 80% and then blew
    past the cap between two sweeps still has to be acted on."""
    assert spend.state(Row(cap=100.0, spent=120.0, warned=_T0)) == "cap"


def test_the_warning_threshold_is_clamped_to_something_meaningful():
    """Below 10% every POV warns immediately and the warning stops being read; above 99%
    the warning and the cap arrive together, which is not a warning."""
    assert spend.warn_percent(0) == spend.MIN_WARN_PERCENT
    assert spend.warn_percent(150) == spend.MAX_WARN_PERCENT
    assert spend.warn_percent(None) == spend.DEFAULT_WARN_PERCENT
    assert spend.warn_percent("nonsense") == spend.DEFAULT_WARN_PERCENT


# ── validation and the action ────────────────────────────────────────────────

def _refused(value) -> str:
    try:
        spend.validate_cap(value)
    except spend.SpendError as exc:
        return str(exc)
    raise AssertionError(f"{value!r} was accepted as a cap")


def test_a_blank_or_zero_cap_means_no_cap():
    for empty in (None, "", 0, 0.0):
        assert spend.validate_cap(empty) is None


def test_a_cap_too_small_to_mean_anything_is_refused():
    assert "within the hour" in _refused(0.5)
    assert "negative" in _refused(-10)
    assert "not an amount" in _refused("lots")


def test_a_valid_cap_rounds_to_cents():
    assert spend.validate_cap(250) == 250.0
    assert spend.validate_cap("99.999") == 100.0


def test_the_default_action_is_warn_and_an_unknown_one_falls_back_to_it():
    """The figure is a list-price ESTIMATE. One that suspended a live customer demo on its
    first outing would be the last time anybody trusted it — and an unrecognised config
    value must make the feature do LESS, never more."""
    from web_dashboard.config import settings
    assert settings.pov_spend_cap_action == spend.ACTION_WARN
    assert spend.normalize_action("") == spend.ACTION_WARN
    assert spend.normalize_action("destroy") == spend.ACTION_WARN
    assert spend.normalize_action("SUSPEND") == spend.ACTION_SUSPEND


def test_the_default_cap_is_zero_so_enabling_nothing_changes_nothing():
    from web_dashboard.config import settings
    assert settings.pov_spend_cap_default_usd == 0.0


# ── the sweep's own guarantees ───────────────────────────────────────────────

_RECONCILE = os.path.join("web_dashboard", "services", "pov_reconcile.py")


def test_the_cap_suspends_and_never_destroys():
    """Reversible in one click, which is what lets this feature exist without the
    auto-delete timer's two arming clocks and dry-run mode."""
    body = _code(_RECONCILE, "sweep_spend")
    assert '"pov_env_power"' in body, "the sweep does not enqueue a power job"
    assert "destroy" not in body.lower(), "the spend cap can destroy a POV"
    assert '"stopped"' in body


def test_the_sweep_only_enqueues_and_never_calls_a_cloud():
    body = _code(_RECONCILE, "sweep_spend")
    for banned in ("set_runstate", "boto3", "pov_cloud_aws", "_to_thread"):
        assert banned not in body, f"the spend sweep reaches {banned} directly"


def test_a_deferred_suspension_does_not_latch():
    """If a power job is already in flight the cap has NOT been acted on. Latching there
    would let a POV sail past its cap because something unrelated was mid-flight."""
    body = _code(_RECONCILE, "sweep_spend")
    deferred = body.split("_power_job_in_flight", 1)[1].split("continue", 1)[0]
    assert "spend_capped_at = None" in deferred, \
        "the cap latches even when the suspension was deferred"


def test_accrual_uses_the_read_the_pass_already_made():
    """No extra platform call, and no billing API — the environment dict the reconcile
    loop fetched already carries every VM's shape, disk and runstate."""
    body = _code(_RECONCILE, "_accrue_spend")
    assert "rate_usd_per_hour(raw" in body, \
        "the accrual re-fetches instead of using the pass's own read"
    assert "cost_explorer" not in body and "get_cost_and_usage" not in body


def test_the_spend_sweep_runs_after_the_schedule():
    """A POV that hits its cap in the same pass a schedule would have resumed it must end
    up stopped: the cap is the stronger statement of the two."""
    body = _code(_RECONCILE, "run_reconcile")
    assert body.index("sweep_schedules") < body.index("sweep_spend"), \
        "the spend cap runs before the schedule, so a resume could outrank a cap"


def test_raising_the_cap_clears_both_latches():
    """Otherwise "give it another fifty dollars" leaves the row permanently flagged and
    the cap unable to fire again."""
    body = _code(os.path.join("web_dashboard", "api", "pov.py"), "set_spend_cap")
    assert "spend_warned_at = None" in body and "spend_capped_at = None" in body
    assert "cap > previous" in body, "the latches clear on any edit, not only on a raise"
    assert "spend_estimate_usd" not in body, (
        "editing the cap resets the accrued total, which would make the cap mean "
        "'another $X from now' rather than what the page promises")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
