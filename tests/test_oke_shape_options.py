"""OKE worker-shape resolution for the provision form.

OKE accepts only a *subset* of the compute shapes OCI offers, and the subset
differs by region and tenancy. A shape outside it is not rejected when the form is
submitted — it is rejected at node-pool creation, roughly ten minutes into the
apply, with the VCN, gateways, subnets and cluster already built and a rollback to
follow. `VM.Standard.E4.Flex` sat in the curated list in exactly that state, while
`VM.Standard.A2.Flex` (supported) was absent, so `provision_options("oci")` now
reads the live list (`oci ce node-pool-options get`) and keeps the curated list
only as the offline fallback.

Pinned here:

  * `oci_service._shape_sort_key` — free-tier first, bare metal last. The picker
    preselects "(module default)", but order is what an operator reads as the
    house default, and `BM.Standard.E5.192` is a 192-OCPU machine billed whole.
  * `provision_options("oci")` — serves the live list, falls back to the curated
    one when the lookup fails (the modal must still open with OCI unconfigured),
    and never lets the configured shape drop out of a strict `<select>`.
  * No hard gate: the live list is region- and tenancy-scoped, so a shape valid in
    another region must stay reachable through config.
  * The curated fallback itself — every entry a shape OKE actually takes.
  * The other clouds don't touch OCI at all.

Collaborators are swapped with spies (the house pattern from
test_gateway_region_picker.py), so no DB, no OCI SDK and no cloud account.

Run: python tests/test_oke_shape_options.py   (or under pytest)
"""
import asyncio
import contextlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from web_dashboard.services import k8s_service as svc
    from web_dashboard.services import oci_service
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# What the tenancy's us-chicago-1 node-pool-options actually returned on
# 2026-07-30 — the live shape of the data this code has to order and serve.
LIVE_SHAPES = ["BM.Standard.A1.160", "BM.Standard.E5.192", "BM.Standard3.64",
               "VM.Standard.A1.Flex", "VM.Standard.A2.Flex", "VM.Standard.E5.Flex",
               "VM.Standard3.Flex"]


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


def _no_config(key, default=""):
    """A `_cfg` stand-in: nothing configured (no DB, no settings)."""
    return ""


def _returns(shapes):
    """An `oke_node_pool_shapes` stand-in returning a fixed live list."""
    async def _fn(region: str = ""):
        return list(shapes)
    return _fn


def _raises(exc):
    async def _fn(region: str = ""):
        raise exc
    return _fn


def _options(cloud, **patches):
    """`provision_options` with the OCI collaborators swapped out. It reads *both*
    OKE lists for oci, so the version lookup is stubbed by default too — otherwise
    a shape test would reach for the network on the way past."""
    patches.setdefault("oke_cluster_versions", _returns(["v1.36.1"]))
    with _patched(svc, _cfg=patches.pop("_cfg", _no_config)), \
         _patched(oci_service, **patches):
        return asyncio.run(svc.provision_options(cloud))


# ── shape ordering ────────────────────────────────────────────────────────────

def test_free_tier_ampere_shape_leads():
    """A1.Flex is the Always-Free Ampere shape and the module's own default."""
    assert sorted(LIVE_SHAPES, key=oci_service._shape_sort_key)[0] == "VM.Standard.A1.Flex"


def test_bare_metal_sorts_last_not_first():
    """A plain sort() puts BM.Standard.A1.160 at the top of the picker — a
    192-OCPU-class machine billed whole, one click from a lab cluster."""
    ranked = sorted(LIVE_SHAPES, key=oci_service._shape_sort_key)
    assert [s for s in ranked if s.startswith("BM.")] == ranked[-3:]
    assert sorted(LIVE_SHAPES)[0].startswith("BM.")  # the trap being avoided


def test_flexible_vm_shapes_outrank_fixed_ones():
    """Flex shapes take an OCPU/memory config, so they fit a budget; fixed ones
    are take-it-or-leave-it."""
    ranked = sorted(LIVE_SHAPES + ["VM.Standard2.1"], key=oci_service._shape_sort_key)
    assert ranked.index("VM.Standard.E5.Flex") < ranked.index("VM.Standard2.1")


def test_shape_ordering_is_total_and_tolerates_junk():
    for junk in ("", "   ", "weird-shape"):
        assert isinstance(oci_service._shape_sort_key(junk), tuple)
    assert len(sorted(LIVE_SHAPES + ["", "x"], key=oci_service._shape_sort_key)) == 9


# ── provision_options: live list, with a fallback ─────────────────────────────

def test_oci_serves_the_live_shape_list():
    out = _options("oci", oke_node_pool_shapes=_returns(LIVE_SHAPES))
    assert out["node_instance_types"] == LIVE_SHAPES


def test_the_live_list_supplies_a2_and_drops_e4():
    """The two bugs this exists to fix, stated as one assertion."""
    out = _options("oci", oke_node_pool_shapes=_returns(LIVE_SHAPES))
    assert "VM.Standard.A2.Flex" in out["node_instance_types"]
    assert "VM.Standard.E4.Flex" not in out["node_instance_types"]


def test_oci_falls_back_to_the_curated_list_when_the_lookup_fails():
    """OCI unconfigured must not break the modal — it opens on the curated list."""
    out = _options("oci", oke_node_pool_shapes=_raises(oci_service.OCIError("not configured")))
    assert out["node_instance_types"] == svc.K8S_NODE_TYPES["oci"]


def test_oci_falls_back_when_the_live_list_comes_back_empty():
    out = _options("oci", oke_node_pool_shapes=_returns([]))
    assert out["node_instance_types"] == svc.K8S_NODE_TYPES["oci"]


def test_configured_shape_survives_the_live_list_and_leads():
    """A strict <select> can never exclude the configured value, live list or not.
    This is what keeps a shape valid in another region reachable — the live list is
    scoped to one region and tenancy, so it is a picker, not a gate."""
    def _cfg(key, default=""):
        return "VM.Standard.E4.Flex" if key == "oci_oke_node_shape" else ""

    out = _options("oci", _cfg=_cfg, oke_node_pool_shapes=_returns(LIVE_SHAPES))
    assert out["node_instance_types"] == ["VM.Standard.E4.Flex"] + LIVE_SHAPES


def test_configured_shape_is_not_duplicated_when_the_live_list_has_it():
    def _cfg(key, default=""):
        return "VM.Standard.A2.Flex" if key == "oci_oke_node_shape" else ""

    out = _options("oci", _cfg=_cfg, oke_node_pool_shapes=_returns(LIVE_SHAPES))
    assert out["node_instance_types"].count("VM.Standard.A2.Flex") == 1
    assert out["node_instance_types"][0] == "VM.Standard.A2.Flex"


def test_other_clouds_never_call_the_oci_api():
    def _boom(region: str = ""):
        raise AssertionError("provision_options must not call OCI for a non-oci cloud")

    for cloud in ("aws", "azure", "gcp"):
        out = _options(cloud, oke_node_pool_shapes=_boom, oke_cluster_versions=_boom)
        assert out["node_instance_types"] == svc.K8S_NODE_TYPES[cloud]


# ── the curated fallback itself ───────────────────────────────────────────────

def test_curated_oci_shapes_are_all_supported_by_oke():
    """The fallback is what an air-gapped/unconfigured form offers, so a shape OKE
    rejects is as bad here as in the live list. Checked against the tenancy's
    live node-pool-options (us-chicago-1, 2026-07-30)."""
    for shape in svc.K8S_NODE_TYPES["oci"]:
        assert shape in LIVE_SHAPES, f"{shape} is not an OKE-supported shape"


def test_curated_oci_shapes_lead_with_the_free_tier_shape():
    shapes = svc.K8S_NODE_TYPES["oci"]
    assert shapes[0] == "VM.Standard.A1.Flex"
    assert shapes == sorted(shapes, key=oci_service._shape_sort_key)


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
    sys.exit(1 if failures else 0)
