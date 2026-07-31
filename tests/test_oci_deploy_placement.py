"""Both OCI deploy endpoints precheck that the shape can launch the image *there*.

`LaunchInstance` answers three different mistakes with the same unattributed
``404 NotAuthorizedOrNotFound`` — no field named, about a second into the job:

  1. the shape isn't offered in that availability domain,
  2. the image doesn't support the shape,
  3. a genuine IAM policy denial.

The Packer build route has prechecked cases 1 and 2 since the builder shipped
(``oci_service.check_launch_placement``, pinned by tests/test_packer_oci.py). The
deploy routes did not, so ``POST /api/oci/deploy`` and ``POST /api/oci/bulk-deploy``
accepted a shape that cannot launch and reported it as that bare 404 from inside the
job. The trap is the default: ``VM.Standard.E2.1.Micro`` is the Always-Free AMD micro
and **E2 shapes exist only in the older OCI regions** — ``us-chicago-1`` offers none —
and the bulk modal hardcoded it.

Pinned here:
  * either route refuses an unlaunchable shape with HTTP 400 ``shape_not_launchable``
    and creates nothing;
  * the gate runs BEFORE the free-tier prompt, and ``acknowledge_charges`` cannot
    wave it through — it is not a charges warning;
  * a blank ``availability_domain`` is prechecked against the AD the runner will
    actually pick (``ads[0]``), not against the blank;
  * bulk checks EVERY image (the shape is request-level, the compatibility list is
    per image), deduped by OCID, and one bad item fails the whole batch — same
    all-or-nothing contract as the name-collision pre-flight;
  * the whole thing fails OPEN: an AD lookup that can't reach OCI must never be why
    a deploy is refused.

Follows the hermetic TestClient pattern from test_oci_cache_scope.py. Heavy cloud
deps (fastapi/oci/…) are only present in CI; when missing the file SKIPs cleanly so
the per-file runner stays green.

Run: python tests/test_oci_deploy_placement.py   (or under pytest)
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import oci
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
    from web_dashboard.services import admission_service
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"oci api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


CHICAGO = "us-chicago-1"
COMPARTMENT = "ocid1.compartment.oc1..aaaacompartment"
# us-chicago-1 has one AD; the deploy form's default is blank = "first available",
# so the precheck has to resolve it exactly as _launch_instance_sync does.
ADS = ["wYeM:US-CHICAGO-1-AD-1", "wYeM:US-CHICAGO-1-AD-2"]

FREE_MICRO = "VM.Standard.E2.1.Micro"     # Always-Free, absent from newer regions
PAID_FLEX = "VM.Standard.E5.Flex"         # offered in us-chicago-1, not free

IMG_X86 = "ocid1.image.oc1.us-chicago-1.aaaax86"
IMG_ARM = "ocid1.image.oc1.us-chicago-1.aaaaarm"

_CFG: dict = {}
_PLACEMENT_CALLS: list = []
_AD_CALLS: list = []
_JOBS: list = []
_UNLAUNCHABLE: set = set()     # image OCIDs the requested shape cannot launch
_AD_LOOKUP_FAILS: list = []    # non-empty → list_availability_domains raises


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = ["lab"]
    effective_permissions_dict: dict = {}


class _FakeJob:
    def __init__(self, n):
        self.id = f"job-{n}"


class _FakeWorkgroup:
    name = "lab"


def _reset():
    _CFG.clear()
    _CFG.update({
        "oci_tenancy_ocid":     "ocid1.tenancy.oc1..aaaatenancy",
        "oci_user_ocid":        "ocid1.user.oc1..aaaauser",
        "oci_private_key":      "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        "oci_region":           CHICAGO,
        "oci_compartment_ocid": COMPARTMENT,
        # Left at its default ("1" → on) so the ordering tests are meaningful: the
        # free-tier gate has to be live for "the placement gate comes first" to mean
        # anything.
    })
    _PLACEMENT_CALLS.clear()
    _AD_CALLS.clear()
    _JOBS.clear()
    _UNLAUNCHABLE.clear()
    _AD_LOOKUP_FAILS.clear()


def _install_stubs():
    oci._oci_cfg = lambda key, fallback="": _CFG.get(key) or fallback

    async def _fake_list_ads(compartment_id=""):
        _AD_CALLS.append(compartment_id)
        if _AD_LOOKUP_FAILS:
            raise RuntimeError("connection reset by peer")
        return list(ADS)

    async def _fake_check_placement(*, availability_domain, image_ocid, shape,
                                    compartment_id="", region=""):
        _PLACEMENT_CALLS.append({"ad": availability_domain, "image": image_ocid,
                                 "shape": shape, "compartment": compartment_id,
                                 "region": region})
        if image_ocid in _UNLAUNCHABLE:
            # Shaped like the real message (oci_service._check_launch_placement_sync):
            # it names the rejected shape, the placement, and what would work.
            raise oci.oci_service.OCIError(
                f"Shape {shape} cannot launch this image in {region} "
                f"({availability_domain}) — either the shape isn't offered there or "
                f"the image doesn't support it. OCI reports both as a bare 404 "
                f"NotAuthorizedOrNotFound. Usable shapes for this image here: "
                f"{PAID_FLEX}, VM.Standard3.Flex.")

    oci.oci_service.list_availability_domains = _fake_list_ads
    oci.oci_service.check_launch_placement = _fake_check_placement

    # Everything downstream of the gate: creating a job needs a real DB, and none of
    # it is what this file is about. _JOBS doubles as the "was anything created?"
    # assertion — the gate must refuse before the first create_job.
    def _fake_create_job(db, **kw):
        _JOBS.append(kw)
        return _FakeJob(len(_JOBS))

    oci.job_service.create_job = _fake_create_job
    oci.job_service.set_cloud_resource_id = lambda *a, **k: None
    oci.job_service.log_audit = lambda *a, **k: None
    oci.workgroup_service.get = lambda db, name: _FakeWorkgroup()
    # Reads Job rows for the free-tier envelope; zeros keep the arithmetic here
    # entirely about the request under test.
    oci._existing_freetier_usage = lambda db, exclude_job_id="": {
        "existing_amd_count": 0, "existing_a1_ocpus": 0.0, "existing_a1_memory_gb": 0.0}
    oci.deploy_batch.validate_name = lambda name, provider: name
    oci.deploy_batch.reject_name_collisions = lambda db, job_type, names: None
    oci.deploy_batch.expand_names = lambda base, count, provider: (
        [base] if count == 1 else [f"{base}-{i:02d}" for i in range(1, count + 1)])

    async def _no_admission(*a, **k):
        return None

    oci.deploy_batch.enforce_admission = _no_admission
    admission_service.enforce = lambda *a, **k: None


_app = FastAPI()
_app.include_router(oci.router)
_app.dependency_overrides[get_current_user] = lambda: _AdminUser()
_app.dependency_overrides[get_db] = lambda: object()
_client = TestClient(_app, raise_server_exceptions=False)


def _deploy(**over):
    body = {"image_ocid": IMG_X86, "image_name": "ol10-x86", "instance_name": "web-01",
            "shape": FREE_MICRO, "workgroup": "lab", "boot_volume_gb": 50}
    body.update(over)
    return _client.post("/api/oci/deploy", json=body)


def _bulk(items=None, **over):
    body = {"items": items if items is not None else [
                {"image_ocid": IMG_X86, "image_name": "ol10-x86", "instance_name": "web-01"}],
            "shape": FREE_MICRO, "workgroup": "lab", "boot_volume_gb": 50}
    body.update(over)
    return _client.post("/api/oci/bulk-deploy", json=body)


def _detail(resp):
    return resp.json().get("detail") or {}


# ── POST /api/oci/deploy ──────────────────────────────────────────────────────

def test_deploy_refuses_a_shape_that_cannot_launch_the_image():
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_X86)

    r = _deploy()
    assert r.status_code == 400, (r.status_code, r.text)
    detail = _detail(r)
    assert detail.get("code") == "shape_not_launchable", detail
    msg = detail.get("message") or ""
    assert FREE_MICRO in msg, "the error must name the rejected shape"
    assert ADS[0] in msg, "the error must name the placement it was checked against"
    assert PAID_FLEX in msg, "the error must list what would work"
    assert not _JOBS, "nothing may be created once the placement is refused"


def test_a_launchable_deploy_still_goes_through():
    _install_stubs()
    _reset()

    r = _deploy(shape=PAID_FLEX, ocpus=1, memory_gb=8, acknowledge_charges=True)
    assert r.status_code == 200, (r.status_code, r.text)
    assert len(_PLACEMENT_CALLS) == 1, "the precheck must actually run on the happy path"
    assert [j["job_type"] for j in _JOBS] == ["oci_deploy"]


def test_deploy_prechecks_the_ad_the_runner_will_actually_use():
    """A blank availability_domain means "first AD in the compartment" — resolved by
    oci_service._launch_instance_sync at launch time. Prechecking the blank would
    check nothing at all."""
    _install_stubs()
    _reset()

    assert _deploy(availability_domain="").status_code == 200
    assert _PLACEMENT_CALLS[0]["ad"] == ADS[0], "blank AD must resolve to the first AD"
    assert _AD_CALLS == [COMPARTMENT], "the AD list must be read for the right compartment"
    assert _PLACEMENT_CALLS[0]["compartment"] == COMPARTMENT
    assert _PLACEMENT_CALLS[0]["region"] == CHICAGO

    # An explicit AD is used verbatim, and costs no AD lookup.
    _PLACEMENT_CALLS.clear()
    _AD_CALLS.clear()
    assert _deploy(availability_domain=ADS[1]).status_code == 200
    assert _PLACEMENT_CALLS[0]["ad"] == ADS[1]
    assert _AD_CALLS == [], "an explicit AD needs no list_availability_domains call"


def test_the_placement_gate_precedes_the_free_tier_prompt():
    """A shape that cannot launch at all must not be waved through an "acknowledge
    charges" dialog first — the operator would tick the box and get the bare 404
    anyway. Same ordering the build route pins."""
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_X86)

    # PAID_FLEX is outside the Always-Free envelope, so BOTH gates would fire.
    r = _deploy(shape=PAID_FLEX, ocpus=4, memory_gb=32)
    assert r.status_code == 400, (r.status_code, r.text)
    assert _detail(r).get("code") == "shape_not_launchable", \
        "the free-tier prompt must not pre-empt the placement gate"


def test_acknowledge_charges_cannot_wave_an_unlaunchable_shape_through():
    """The placement gate is not a charges warning: there is nothing to acknowledge,
    the shape simply cannot launch this image here."""
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_X86)

    r = _deploy(shape=PAID_FLEX, ocpus=4, memory_gb=32, acknowledge_charges=True)
    assert r.status_code == 400, (r.status_code, r.text)
    assert _detail(r).get("code") == "shape_not_launchable"
    assert not _JOBS


def test_a_count_batch_is_gated_before_it_fans_out():
    """count > 1 fans out into a parent + N children, all sharing this request's
    shape/image/AD — so the check belongs ahead of the fan-out, not per child."""
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_X86)

    r = _deploy(count=3)
    assert r.status_code == 400, (r.status_code, r.text)
    assert _detail(r).get("code") == "shape_not_launchable"
    assert not _JOBS, "no children may be stranded queued behind a refused batch"
    # One check for the batch: same image, same shape, same AD for every child.
    assert len(_PLACEMENT_CALLS) == 1

    # …and a launchable batch still fans out. (acknowledge_charges because three free
    # micros is one more than the Always-Free envelope allows — that gate is the
    # subject of test_oci_freetier.py, not this file.)
    _reset()
    r = _deploy(count=3, acknowledge_charges=True)
    assert r.status_code == 200, (r.status_code, r.text)
    assert [j["job_type"] for j in _JOBS] == ["oci_deploy"] * 3 + ["oci_bulk_deploy"]


def test_the_precheck_fails_open_when_the_ad_lookup_breaks():
    """An OCI that can't be reached is not evidence the placement is wrong, and must
    never be the reason a deploy is refused."""
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_X86)      # would be refused if the check could run
    _AD_LOOKUP_FAILS.append(True)

    r = _deploy(availability_domain="")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _PLACEMENT_CALLS == [], "an unresolvable AD skips the check, it doesn't guess"
    assert len(_JOBS) == 1


def test_no_availability_domains_skips_the_check_rather_than_refusing():
    _install_stubs()
    _reset()

    async def _empty(compartment_id=""):
        _AD_CALLS.append(compartment_id)
        return []

    oci.oci_service.list_availability_domains = _empty
    _UNLAUNCHABLE.add(IMG_X86)
    assert _deploy(availability_domain="").status_code == 200
    assert _PLACEMENT_CALLS == []


# ── POST /api/oci/bulk-deploy ─────────────────────────────────────────────────

_THREE = [
    {"image_ocid": IMG_X86, "image_name": "ol10-x86", "instance_name": "web-01"},
    {"image_ocid": IMG_ARM, "image_name": "ol10-arm", "instance_name": "web-02"},
    {"image_ocid": IMG_X86, "image_name": "ol10-x86", "instance_name": "web-03"},
]


def test_bulk_deploy_refuses_the_whole_batch_when_one_image_cannot_launch():
    """Matches the name-collision pre-flight: all-or-nothing, before the first
    create_job. A partly-admitted batch would strand `queued` children that the
    runner can't claim and reconcile_stale_jobs won't touch."""
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_ARM)      # the x86 shape can't launch the aarch64 image

    r = _bulk(_THREE)
    assert r.status_code == 400, (r.status_code, r.text)
    detail = _detail(r)
    assert detail.get("code") == "shape_not_launchable", detail
    msg = detail.get("message") or ""
    assert "web-02" in msg, "the error must name the offending instance"
    assert "web-01:" not in msg and "web-03:" not in msg, \
        "only the failing items are the offenders"
    assert "1 of the 3" in msg, "say how much of the selection is affected"
    assert not _JOBS, "no child, and no parent, may be created"


def test_bulk_deploy_checks_every_image_and_dedupes_by_ocid():
    """The shape is request-level but each item carries its own image, and half the
    check (list_image_shape_compatibility_entries) is a property of the image — so
    one check for the batch would admit two items on a third's compatibility list."""
    _install_stubs()
    _reset()

    # acknowledge_charges: 3 × the free micro exceeds the 2-instance envelope, which
    # is the free-tier gate's business, not this file's.
    r = _bulk(_THREE, acknowledge_charges=True)
    assert r.status_code == 200, (r.status_code, r.text)
    checked = [c["image"] for c in _PLACEMENT_CALLS]
    assert sorted(checked) == sorted([IMG_X86, IMG_ARM]), \
        "every distinct image checked exactly once (two list calls each)"
    assert {c["shape"] for c in _PLACEMENT_CALLS} == {FREE_MICRO}
    assert {c["ad"] for c in _PLACEMENT_CALLS} == {ADS[0]}, "blank AD → first AD, once"
    assert _AD_CALLS == [COMPARTMENT], "the AD is resolved once for the batch"
    assert [j["job_type"] for j in _JOBS] == ["oci_deploy"] * 3 + ["oci_bulk_deploy"]


def test_bulk_deploy_placement_gate_precedes_the_free_tier_prompt():
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_ARM)

    # 3 × a non-free shape: the free-tier gate would fire on this selection too.
    r = _bulk(_THREE, shape=PAID_FLEX, ocpus=4, memory_gb=32)
    assert r.status_code == 400, (r.status_code, r.text)
    assert _detail(r).get("code") == "shape_not_launchable"
    assert not _JOBS


def test_a_single_item_bulk_reads_as_one_sentence():
    """No "1 of the 1 instances" preamble when the selection is one image."""
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.add(IMG_X86)

    r = _bulk()
    assert r.status_code == 400, (r.status_code, r.text)
    msg = _detail(r).get("message") or ""
    assert msg.startswith("web-01: "), msg
    assert "1 of the 1" not in msg


def test_bulk_deploy_fails_open_when_the_ad_lookup_breaks():
    _install_stubs()
    _reset()
    _UNLAUNCHABLE.update({IMG_X86, IMG_ARM})
    _AD_LOOKUP_FAILS.append(True)

    r = _bulk(_THREE, acknowledge_charges=True)
    assert r.status_code == 200, (r.status_code, r.text)
    assert _PLACEMENT_CALLS == []
    assert len(_JOBS) == 4, "3 children + 1 parent"


# ── source-level pins (mirrors test_packer_oci.py's entry-point test) ─────────

def _func_body(src: str, name: str) -> str:
    m = re.search(rf"async def {name}\(.*?\n(.*?)(?=\n@router\.|\nasync def |\Z)",
                  src, re.S)
    assert m, f"{name} not found in api/oci.py"
    return m.group(1)


def test_both_deploy_routes_precheck_before_the_free_tier_prompt():
    """The behavioural tests above cover today's ordering; this pins the shape of the
    code so a later edit can't reintroduce "prompt about charges, then refuse"."""
    with open(os.path.join(_ROOT, "web_dashboard", "api", "oci.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert '"code": "shape_not_launchable"' in src
    for route in ("deploy_instance", "bulk_deploy_instances"):
        body = _func_body(src, route)
        assert "_placement_problems(" in body, f"{route} does not precheck the placement"
        assert "oci_freetier.evaluate(" in body, f"{route} lost its free-tier gate"
        assert body.index("_placement_problems(") < body.index("oci_freetier.evaluate("), \
            f"{route}: the placement gate must precede the free-tier prompt"
    # The service call itself stays the one in oci_service, shared with the build path.
    assert "oci_service.check_launch_placement(" in src


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
