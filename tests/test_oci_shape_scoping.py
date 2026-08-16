"""Unit tests: OCI shape lists are availability-domain AND image scoped.

The bug: ``_get_network_options_sync`` listed shapes for ``ads[0]`` only, and
``ListShapes`` genuinely honours ``availability_domain`` — OCI does not offer every
shape in every AD of a region. The deploy form and the Packer build form both let
the operator pick AD-2 or AD-3, so the picker offered shapes that cannot launch
there. Nothing else in the OCI path checks placement (there is no request-time or
job-runner shape gate), so a bad pick surfaces as an opaque LaunchInstance failure
minutes in — the picker is the only guard, which is why the scoping is pinned here.

The second axis is image compatibility: an Ampere shape can be offered in the AD
and still refuse an x86_64 image. That narrowing must fail OPEN — "compatibility
unknown" is not "nothing launches here", or an unreadable compatibility list empties
the picker for a perfectly launchable image.

The browser half (a @change on the AD select, and re-defaulting a shape the new
scope dropped) is pinned in tests/template_helpers_check.js.

oci_service is loaded under a synthetic package so its ``from . import
oci_freetier`` resolves without the app's dependency set; the oci SDK is faked, and
the SDK is only ever imported inside function bodies, so nothing needs it installed.
Runs under pytest, or standalone:  python tests/test_oci_shape_scoping.py
"""
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")


def _load_oci_service():
    """Import services/oci_service.py under a synthetic parent package.

    A plain spec_from_file_location can't execute it — the module-level
    ``from . import oci_freetier`` needs a package whose __path__ holds the real
    services directory. config_service is imported lazily inside _cfg, so it never
    has to exist."""
    pkg = types.ModuleType("_ocipkg")
    pkg.__path__ = [_SERVICES]
    sys.modules["_ocipkg"] = pkg
    spec = importlib.util.spec_from_file_location(
        "_ocipkg.oci_service", os.path.join(_SERVICES, "oci_service.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ocipkg.oci_service"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── a fake oci SDK ───────────────────────────────────────────────────────────

class _Shape:
    def __init__(self, shape, ocpus=None, memory_in_gbs=None):
        self.shape = shape
        self.ocpus = ocpus
        self.memory_in_gbs = memory_in_gbs
        self.ocpu_options = None


class _CompatEntry:
    def __init__(self, shape):
        self.shape = shape


class _AD:
    def __init__(self, name):
        self.name = name


# Two ADs offering DIFFERENT shapes — the whole point. AD-1 has the Ampere flex
# shape, AD-2 does not; a picker pinned to AD-1 offers A1.Flex in AD-2 regardless.
_SHAPES_BY_AD = {
    "Uocm:PHX-AD-1": ["VM.Standard.E2.1.Micro", "VM.Standard.A1.Flex", "VM.Standard.E4.Flex"],
    "Uocm:PHX-AD-2": ["VM.Standard.E2.1.Micro", "VM.Standard.E4.Flex"],
    "Uocm:PHX-AD-3": ["VM.Standard.A1.Flex"],
}
# An x86_64 image: bootable on the AMD/Intel shapes, never on Ampere.
_X86_COMPAT = ["VM.Standard.E2.1.Micro", "VM.Standard.E4.Flex"]
_ARM_COMPAT = ["VM.Standard.A1.Flex"]


class _Calls:
    """Records what the fake SDK was asked for, so a test can assert the AD was
    actually passed through rather than inferring it from the result."""
    def __init__(self):
        self.list_shapes_ads = []
        self.compat_images = []


def _install_fake_oci(calls, compat=None, compat_raises=False):
    """Register a fake ``oci`` module. ``compat`` is the image's compatibility list;
    None means the image declares none (the fail-open case)."""
    oci = types.ModuleType("oci")

    class ComputeClient:
        def __init__(self, cfg):
            pass

        def list_shapes(self, compartment_id=None, availability_domain=None, **kw):
            calls.list_shapes_ads.append(availability_domain)
            return [_Shape(s) for s in _SHAPES_BY_AD.get(availability_domain or "", [])]

        def list_image_shape_compatibility_entries(self, image_id=None, **kw):
            calls.compat_images.append(image_id)
            if compat_raises:
                raise RuntimeError("ServiceError: NotAuthorizedOrNotFound")
            return [_CompatEntry(s) for s in (compat or [])]

    class IdentityClient:
        def __init__(self, cfg):
            pass

        def list_availability_domains(self, compartment_id=None, **kw):
            return types.SimpleNamespace(
                data=[_AD(n) for n in sorted(_SHAPES_BY_AD)])

    class VirtualNetworkClient:
        """Subnets are not what this file is about, but they share the response — a
        client that raises would bury the assertions in warning noise."""
        def __init__(self, cfg):
            pass

        def list_subnets(self, **kw):
            return []

    oci.core = types.SimpleNamespace(ComputeClient=ComputeClient,
                                     VirtualNetworkClient=VirtualNetworkClient)
    oci.identity = types.SimpleNamespace(IdentityClient=IdentityClient)

    def _list_all(fn, **kw):
        return types.SimpleNamespace(data=fn(**kw))

    oci.pagination = types.SimpleNamespace(list_call_get_all_results=_list_all)
    oci.config = types.SimpleNamespace(validate_config=lambda cfg: None)
    sys.modules["oci"] = oci
    return oci


svc = _load_oci_service()
# _oci_config() would read stored credentials through config_service; the fake SDK
# ignores the dict entirely, so short-circuit it rather than stub a config store.
svc._oci_config = lambda: {"fake": True}
svc._cfg = lambda key: ""


def _shape_names(rows):
    return [r["shape"] for r in rows]


# ── ListShapes is called for the AD asked for, not always the first ──────────

def test_list_shapes_passes_the_availability_domain_through():
    calls = _Calls()
    _install_fake_oci(calls)
    rows = svc._list_shapes_sync("ocid1.compartment..a", "Uocm:PHX-AD-2")
    assert calls.list_shapes_ads == ["Uocm:PHX-AD-2"], calls.list_shapes_ads
    assert "VM.Standard.A1.Flex" not in _shape_names(rows), _shape_names(rows)


def test_network_options_scopes_shapes_to_the_requested_ad():
    """The regression. AD-2 does not offer A1.Flex; before the fix the response
    carried AD-1's list no matter which AD the form had selected."""
    calls = _Calls()
    _install_fake_oci(calls)
    opts = svc._get_network_options_sync(
        "ocid1.compartment..a", "", availability_domain="Uocm:PHX-AD-2")
    assert calls.list_shapes_ads == ["Uocm:PHX-AD-2"], calls.list_shapes_ads
    assert _shape_names(opts["shapes"]) == ["VM.Standard.E2.1.Micro", "VM.Standard.E4.Flex"], \
        _shape_names(opts["shapes"])
    # AD-3 offers a disjoint set — proof the AD is doing the selecting.
    opts3 = svc._get_network_options_sync(
        "ocid1.compartment..a", "", availability_domain="Uocm:PHX-AD-3")
    assert _shape_names(opts3["shapes"]) == ["VM.Standard.A1.Flex"], _shape_names(opts3["shapes"])


def test_network_options_falls_back_to_the_first_ad_when_none_is_requested():
    """A blank AD has to resolve to the first one — that is also what a blank
    availability_domain resolves to at launch time, so the picker and the launch
    agree instead of describing different ADs."""
    calls = _Calls()
    _install_fake_oci(calls)
    opts = svc._get_network_options_sync("ocid1.compartment..a", "")
    assert calls.list_shapes_ads == ["Uocm:PHX-AD-1"], calls.list_shapes_ads
    assert opts["availability_domain"] == "Uocm:PHX-AD-1"


def test_network_options_echoes_the_scope_it_resolved():
    """A caller that sent a blank AD has no other way to learn which one it got."""
    calls = _Calls()
    _install_fake_oci(calls, compat=_X86_COMPAT)
    opts = svc._get_network_options_sync(
        "ocid1.compartment..a", "", availability_domain="Uocm:PHX-AD-2",
        image_ocid="ocid1.image..x86")
    assert opts["availability_domain"] == "Uocm:PHX-AD-2"
    assert opts["image_ocid"] == "ocid1.image..x86"


# ── the second axis: which shapes the image can actually boot ────────────────

def test_launchable_shape_rows_narrow_to_the_images_compatible_shapes():
    """AD-1 offers A1.Flex and an x86_64 image can be offered it — and will fail at
    launch. Intersecting with the image's compatibility list is what removes it."""
    calls = _Calls()
    _install_fake_oci(calls, compat=_X86_COMPAT)
    rows = svc._launchable_shape_rows_sync(
        "ocid1.compartment..a", "Uocm:PHX-AD-1", "ocid1.image..x86")
    assert _shape_names(rows) == ["VM.Standard.E2.1.Micro", "VM.Standard.E4.Flex"], _shape_names(rows)
    assert calls.compat_images == ["ocid1.image..x86"], calls.compat_images


def test_launchable_shape_rows_carry_the_metadata_the_form_renders():
    """Bare names (what _launchable_shapes_sync returns for the launch gate) are not
    enough here: the picker renders the Always-Free flag and switches its OCPU/memory
    inputs on is_flexible."""
    calls = _Calls()
    _install_fake_oci(calls, compat=_X86_COMPAT)
    rows = svc._launchable_shape_rows_sync(
        "ocid1.compartment..a", "Uocm:PHX-AD-1", "ocid1.image..x86")
    assert all({"shape", "is_flexible", "free_tier"} <= set(r) for r in rows), rows


def test_launchable_shape_rows_skip_the_compat_call_without_an_image():
    calls = _Calls()
    _install_fake_oci(calls, compat=_X86_COMPAT)
    rows = svc._launchable_shape_rows_sync("ocid1.compartment..a", "Uocm:PHX-AD-1", "")
    assert calls.compat_images == [], calls.compat_images
    assert len(rows) == 3, _shape_names(rows)


# ── failing open: "unknown" must never present as "nothing launches here" ────
#
# These mirror _check_launch_placement_sync's fail-open conditions on purpose (see
# tests/test_packer_oci.py::test_the_precheck_fails_open_when_the_lookup_breaks).
# The picker must never offer LESS than the gate accepts, or the form refuses a
# shape the API would have taken — and in the empty-intersection case that means
# every OCI deploy in the tenancy, with an empty dropdown and nothing to explain it.

def test_unreadable_compatibility_leaves_the_ad_list_unnarrowed():
    """A tenancy whose policy blocks ListImageShapeCompatibilityEntries must still
    get a usable picker."""
    calls = _Calls()
    _install_fake_oci(calls, compat_raises=True)
    rows = svc._launchable_shape_rows_sync(
        "ocid1.compartment..a", "Uocm:PHX-AD-1", "ocid1.image..x86")
    assert len(rows) == 3, _shape_names(rows)


def test_an_image_declaring_no_compatibility_entries_is_not_narrowed_to_nothing():
    calls = _Calls()
    _install_fake_oci(calls, compat=[])
    rows = svc._launchable_shape_rows_sync(
        "ocid1.compartment..a", "Uocm:PHX-AD-1", "ocid1.image..bare")
    assert len(rows) == 3, _shape_names(rows)


def test_an_empty_intersection_falls_open_exactly_as_the_launch_gate_does():
    """AD-2 offers no Ampere shape and an Arm image boots on nothing else, so the
    intersection is empty. ``check_launch_placement`` reads empty as "the lookup told
    us nothing useful" and allows the launch; the picker has to agree, or the form
    shows an empty dropdown for a shape the API would happily accept."""
    calls = _Calls()
    _install_fake_oci(calls, compat=_ARM_COMPAT)
    rows = svc._launchable_shape_rows_sync(
        "ocid1.compartment..a", "Uocm:PHX-AD-2", "ocid1.image..arm")
    assert len(rows) == 2, _shape_names(rows)
    # The gate reaches the same verdict on the same inputs — pinned together so the
    # two can't drift into disagreeing about one placement.
    svc._check_launch_placement_sync(
        availability_domain="Uocm:PHX-AD-2", image_ocid="ocid1.image..arm",
        shape="VM.Standard.E2.1.Micro", compartment_id="ocid1.compartment..a")


def test_the_picker_offers_only_shapes_the_launch_gate_accepts():
    """The invariant that makes this worth having: everything in the dropdown clears
    the gate. A picker filtering on a different rule than the check enforces is how a
    form ends up offering a shape the API rejects."""
    calls = _Calls()
    _install_fake_oci(calls, compat=_X86_COMPAT)
    rows = svc._launchable_shape_rows_sync(
        "ocid1.compartment..a", "Uocm:PHX-AD-1", "ocid1.image..x86")
    for row in rows:
        svc._check_launch_placement_sync(
            availability_domain="Uocm:PHX-AD-1", image_ocid="ocid1.image..x86",
            shape=row["shape"], compartment_id="ocid1.compartment..a")
    # ...and the shape it filtered out is one the gate really does refuse.
    try:
        svc._check_launch_placement_sync(
            availability_domain="Uocm:PHX-AD-1", image_ocid="ocid1.image..x86",
            shape="VM.Standard.A1.Flex", compartment_id="ocid1.compartment..a")
    except svc.OCIError:
        pass
    else:
        raise AssertionError("the gate accepted an Ampere shape for an x86 image")


def test_network_options_survives_a_shape_listing_failure():
    """Every other key still has to arrive — the form reads free_tier and the AD list
    from the same response, and losing them would blank the whole page."""
    calls = _Calls()
    oci = _install_fake_oci(calls)

    class Boom:
        def __init__(self, cfg):
            pass

        def list_shapes(self, **kw):
            raise RuntimeError("ServiceError: 404 NotAuthorizedOrNotFound")

    oci.core.ComputeClient = Boom
    opts = svc._get_network_options_sync(
        "ocid1.compartment..a", "", availability_domain="Uocm:PHX-AD-2")
    assert opts["shapes"] == []
    assert opts["availability_domains"] == sorted(_SHAPES_BY_AD)
    assert "free_tier" in opts


# ── the endpoint's cache key has to carry the scope ──────────────────────────

def test_the_network_options_cache_key_is_scoped_by_ad_and_image():
    """A cache key that ignores the scope is the bug wearing a different hat: AD-2's
    request gets served the list built for AD-1. Asserted on the source because
    importing api/oci.py needs the whole app; the key builder itself is real."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "oci.py"), encoding="utf-8").read()
    assert 'key_global("oci_network_opts")' not in src, \
        "network-options is back on an unscoped cache key"
    assert 'ad=availability_domain' in src and 'image=image_ocid' in src, \
        "the network-options cache key no longer carries the AD/image scope"

    spec = importlib.util.spec_from_file_location(
        "cache_service_probe", os.path.join(_SERVICES, "cache_service.py"))
    cache = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cache)
    keys = {cache.key_param("oci_network_opts", region="us-phoenix-1", compartment="c",
                            ad=ad, image=img)
            for ad, img in [("Uocm:PHX-AD-1", ""), ("Uocm:PHX-AD-2", ""),
                            ("Uocm:PHX-AD-1", "ocid1.image..a")]}
    assert len(keys) == 3, keys
    # The AD/image dimensions must NARROW the region+compartment key added by #498,
    # never displace it — a region change has to keep invalidating the entry.
    assert all("region=us-phoenix-1" in k and "compartment=c" in k for k in keys), keys


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
