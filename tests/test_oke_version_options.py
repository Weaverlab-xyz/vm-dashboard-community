"""OKE Kubernetes-version resolution for the provision form.

OKE is the one cloud here that *retires* Kubernetes versions and then rejects
them outright — `400 InvalidParameter, Invalid kubernetesVersion` — and it does
so part-way through the apply, after the VCN, gateways and subnets are already
built. A curated version list is therefore not a cosmetic default on this cloud:
once it ages out, every provision fails and rolls back. OKE also happens to be
the one cloud with a live version-discovery API, so `provision_options("oci")`
reads it and only falls back to `K8S_VERSIONS["oci"]` when OCI is unconfigured.

Pinned here:

  * `oci_service._version_sort_key` — newest-first means *numeric* order.
    Sorting the strings puts v1.33.9 above v1.33.10, which would hand the module
    a stale patch version while looking correct.
  * `provision_options("oci")` — serves the live list, falls back to the curated
    one when the lookup fails (the modal must still open with OCI unconfigured),
    and never lets the configured value drop out of a strict `<select>`.
  * The curated fallback itself — v-prefixed patch format, newest first. AKS/EKS
    /GKE take a bare `1.36`; OKE needs `v1.36.1`, and the wrong format fails the
    same way a retired version does.
  * The other clouds don't touch OCI at all.

Collaborators are swapped with spies (the house pattern from
test_gateway_region_picker.py), so no DB, no OCI SDK and no cloud account.

Run: python tests/test_oke_version_options.py   (or under pytest)
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


def _returns(versions):
    """An `oke_cluster_versions` stand-in returning a fixed live list."""
    async def _fn(region: str = ""):
        return list(versions)
    return _fn


def _raises(exc):
    async def _fn(region: str = ""):
        raise exc
    return _fn


def _options(cloud, **patches):
    with _patched(svc, _cfg=patches.pop("_cfg", _no_config)), \
         _patched(oci_service, **patches):
        return asyncio.run(svc.provision_options(cloud))


# ── numeric version ordering ──────────────────────────────────────────────────

def test_version_sort_key_orders_patch_numbers_numerically():
    """v1.33.10 is newer than v1.33.9 — the reason for a key instead of sort()."""
    versions = ["v1.33.9", "v1.33.10", "v1.33.1"]
    assert sorted(versions, key=oci_service._version_sort_key, reverse=True) == [
        "v1.33.10", "v1.33.9", "v1.33.1"]
    # And a plain string sort is exactly the trap being avoided.
    assert sorted(versions, reverse=True)[0] == "v1.33.9"


def test_version_sort_key_orders_across_minors_and_tolerates_junk():
    versions = ["v1.9.7", "v1.36.1", "v1.35.2", "vX.Y.Z"]
    ranked = sorted(versions, key=oci_service._version_sort_key, reverse=True)
    assert ranked[0] == "v1.36.1"        # not "v1.9.7" (string sort would pick it)
    assert ranked[-1] == "vX.Y.Z"        # unparseable sorts last, never crashes


# ── provision_options: live list, with a fallback ─────────────────────────────

def test_oci_serves_the_live_version_list():
    live = ["v1.36.1", "v1.35.2", "v1.34.2"]
    out = _options("oci", oke_cluster_versions=_returns(live))
    assert out["k8s_versions"] == live


def test_oci_falls_back_to_the_curated_list_when_the_lookup_fails():
    """OCI unconfigured must not break the modal — it opens on the curated list."""
    out = _options("oci", oke_cluster_versions=_raises(oci_service.OCIError("not configured")))
    assert out["k8s_versions"] == svc.K8S_VERSIONS["oci"]


def test_oci_falls_back_when_the_live_list_comes_back_empty():
    out = _options("oci", oke_cluster_versions=_returns([]))
    assert out["k8s_versions"] == svc.K8S_VERSIONS["oci"]


def test_configured_version_survives_the_live_list_and_leads():
    """A strict <select> can never exclude the configured value, live list or not."""
    def _cfg(key, default=""):
        return "v1.33.1" if key == "oci_oke_k8s_version" else ""

    out = _options("oci", _cfg=_cfg, oke_cluster_versions=_returns(["v1.36.1", "v1.35.2"]))
    assert out["k8s_versions"] == ["v1.33.1", "v1.36.1", "v1.35.2"]


def test_other_clouds_never_call_the_oci_api():
    def _boom(region: str = ""):
        raise AssertionError("provision_options must not call OCI for a non-oci cloud")

    for cloud in ("aws", "azure", "gcp"):
        out = _options(cloud, oke_cluster_versions=_boom)
        assert out["k8s_versions"] == svc.K8S_VERSIONS[cloud]


# ── the curated fallback itself ───────────────────────────────────────────────

def test_curated_oci_versions_use_okes_v_prefixed_patch_format():
    """A bare "1.36" is rejected by OKE the same way a retired version is."""
    for v in svc.K8S_VERSIONS["oci"]:
        assert v.startswith("v"), f"{v} is missing OKE's v prefix"
        assert len(v.lstrip("v").split(".")) == 3, f"{v} is not a full patch version"


def test_curated_oci_versions_are_newest_first():
    """The picker preselects nothing, but order is what an operator reads as current."""
    versions = svc.K8S_VERSIONS["oci"]
    assert versions == sorted(versions, key=oci_service._version_sort_key, reverse=True)


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
