"""The Certificate Lab's per-cloud dispatch.

`tests/test_cert_lab_wiring.py` pins the wiring that fails late and quietly — the module
reaching the image, the job types, the reapable kind. This is the other half: the service
picking the right module, the right variables and the right backend name for the row's
cloud, which is what a second cloud actually costs.

The load-bearing one is `test_each_cloud_passes_only_variables_its_own_module_declares`.
Terraform treats an undeclared `-var` as a hard error before it touches anything, and
`_tf_variables` feeds the DESTROY as well as the apply — so a variable that leaked across
clouds would strand a CA that is already billing, which is the one failure this feature
cannot afford.

Runs under pytest or standalone:  python tests/test_cert_lab_clouds.py
"""
import os
import re
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services import cert_lab_service as cls        # noqa: E402
from web_dashboard.services import expiry_policy, job_service      # noqa: E402


def _row(**kw):
    """A CertLab-shaped stand-in. The service reads attributes off the row and never
    queries through it, so a namespace is the whole fixture."""
    base = dict(cloud="gcp", name="Demo", project="", location="", pool_id="",
                ca_arn="", backend="", enroll_account="", ca_chain_pem="")
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeDB:
    """Enough Session for `provision`, which adds one row and creates one job."""
    def add(self, obj): self.row = obj
    def flush(self): pass
    def commit(self): pass
    def refresh(self, obj): pass


def _with_timer(value):
    """Swap the reaper's default-expiry answer. Returns the original to restore."""
    original = expiry_policy.default_expiry_for_kind
    expiry_policy.default_expiry_for_kind = lambda *a, **k: value
    return original


# ── the registry ──────────────────────────────────────────────────────────────

def test_every_cloud_with_a_module_has_a_backend_name_and_the_reverse():
    """`provision` indexes `_BACKENDS[cloud]` after `template_dir` has validated the
    cloud. Adding a module and forgetting the backend name would be a KeyError on a real
    build, not at import."""
    assert set(cls._TEMPLATE_DIRS) == set(cls._BACKENDS), (
        f"registries drifted: modules={sorted(cls._TEMPLATE_DIRS)} "
        f"backends={sorted(cls._BACKENDS)}")


def test_the_advertised_clouds_are_derived_from_the_modules_that_exist():
    """A cloud on the build form with no module behind it is the bug the audit item
    named. Derived, so the two cannot drift — as `vdesktop_service.PROVISIONING_CLOUDS`
    is derived from its seat backends."""
    assert cls.PROVISIONING_CLOUDS == tuple(sorted(cls._TEMPLATE_DIRS))
    for cloud in cls.PROVISIONING_CLOUDS:
        assert os.path.isdir(cls.template_dir(cloud)), cloud


def test_an_unbuilt_cloud_is_refused_and_the_message_names_what_is_built():
    try:
        cls.template_dir("azure")
    except cls.CertLabError as exc:
        for cloud in cls.PROVISIONING_CLOUDS:
            assert cloud in str(exc), f"the refusal does not mention {cloud}"
    else:
        raise AssertionError("azure resolved to a module")


# ── the variables ─────────────────────────────────────────────────────────────

def _declared_variables(cloud: str) -> set:
    """The `variable "x"` blocks the cloud's module declares — the same way
    `cloud_function_service._module_variables` reads them."""
    names = set()
    directory = cls.template_dir(cloud)
    for entry in sorted(os.listdir(directory)):
        if entry.endswith(".tf"):
            with open(os.path.join(directory, entry), encoding="utf-8") as fh:
                names.update(re.findall(r'^variable\s+"([^"]+)"', fh.read(), re.M))
    return names


def test_each_cloud_passes_only_variables_its_own_module_declares():
    """The one that matters. Terraform refuses an undeclared -var outright, and
    `_tf_variables` feeds `terraform destroy` as well as the apply — so a leaked variable
    strands a CA that is already billing."""
    for cloud in cls.PROVISIONING_CLOUDS:
        # Every field populated, so each branch takes what it needs and a cloud added
        # without a `_tf_variables` branch of its own is caught here rather than by a
        # terraform error in a worker.
        row = _row(cloud=cloud, project="p", location="us-central1", pool_id="demo-pool")
        passed = set(cls._tf_variables(row))
        declared = _declared_variables(cloud)
        assert passed <= declared, (
            f"{cloud} passes variables its module does not declare: "
            f"{sorted(passed - declared)}")


def test_the_two_clouds_do_not_share_a_vocabulary_by_accident():
    """`pool_id` and `tier` are Google CAS concepts; `region` and `tags` are AWS's. The
    only thing both modules take is the subject's common name."""
    gcp = set(cls._tf_variables(_row(cloud="gcp", project="p", pool_id="d")))
    aws = set(cls._tf_variables(_row(cloud="aws", location="us-east-2")))
    assert gcp & aws == {"ca_common_name"}, sorted(gcp & aws)
    for cas_only in ("pool_id", "tier", "project", "labels"):
        assert cas_only not in aws, f"{cas_only} reached the AWS module"
    assert "region" not in gcp and "tags" not in gcp


# ── the outputs ───────────────────────────────────────────────────────────────

def test_an_aws_build_records_its_arn_and_invents_no_pool():
    """The modules deliberately emit different output names — an AWS module made to
    output a `service_account_email` holding an IAM key id would put a wrong word in the
    one field an operator reads back when a rotation fails."""
    row = _row(cloud="aws")
    cls._read_outputs(row, {"ca_arn": "arn:aws:acm-pca:us-east-2:1:certificate-authority/x",
                            "region": "us-east-2",
                            "ca_chain_pem": "-----BEGIN CERTIFICATE-----",
                            "enroll_access_key_id": "AKIAEXAMPLE"})
    assert row.ca_arn.startswith("arn:aws:acm-pca:")
    assert row.location == "us-east-2"
    assert row.enroll_account == "AKIAEXAMPLE"
    assert row.pool_id == "", "an AWS build must not invent a pool id"


def test_a_gcp_build_still_records_the_pool_and_the_service_account():
    row = _row(cloud="gcp")
    cls._read_outputs(row, {"pool_id": "demo-pool", "location": "us-central1",
                            "ca_chain_pem": "-----BEGIN CERTIFICATE-----",
                            "service_account_email": "certauth@p.iam.gserviceaccount.com"})
    assert (row.pool_id, row.location) == ("demo-pool", "us-central1")
    assert row.enroll_account.endswith("gserviceaccount.com")
    assert row.ca_arn == "", "a GCP build has no ARN"


# ── the cost guard ────────────────────────────────────────────────────────────

def test_an_aws_ca_is_refused_where_nothing_would_ever_take_it_down():
    """~$400/month standing, billed whether or not it issues anything. On an instance
    where the reaper would stamp no timer, building one creates a resource this dashboard
    will never destroy — so it is refused rather than created and hoped about."""
    original = _with_timer(None)
    try:
        cls.provision(_FakeDB(), name="Demo", project="", created_by="t", cloud="aws")
    except cls.CertLabError as exc:
        assert "400" in str(exc), "the refusal should say what it costs"
        assert "resource_expiry" in str(exc), "and which setting turns the timer on"
    else:
        raise AssertionError("an AWS CA was accepted with no timer")
    finally:
        expiry_policy.default_expiry_for_kind = original


def test_the_same_instance_still_builds_a_gcp_ca():
    """Deliberately not held to the AWS rule: at a twentieth of the cost the same trade
    does not hold, and tightening it would change behaviour somebody already has."""
    original = _with_timer(None)
    created = job_service.create_job
    job_service.create_job = lambda *a, **k: SimpleNamespace(id="job-1")
    try:
        db = _FakeDB()
        cls.provision(db, name="Demo", project="proj-1", created_by="t", cloud="gcp")
        assert db.row.expires_at is None
    finally:
        expiry_policy.default_expiry_for_kind = original
        job_service.create_job = created


def test_a_row_is_shaped_by_its_cloud():
    original = _with_timer(datetime.utcnow() + timedelta(hours=8))
    created = job_service.create_job
    job_service.create_job = lambda *a, **k: SimpleNamespace(id="job-1")
    try:
        db = _FakeDB()
        cls.provision(db, name="Demo", project="", created_by="t", cloud="aws")
        assert db.row.backend == "awspca"
        assert db.row.pool_id == ""
        assert db.row.location, "an AWS CA falls back to the configured region"

        db = _FakeDB()
        cls.provision(db, name="Demo", project="proj-1", created_by="t", cloud="gcp")
        assert db.row.backend == "gcpcas"
        assert db.row.pool_id == "demo-pool"
    finally:
        expiry_policy.default_expiry_for_kind = original
        job_service.create_job = created


def test_a_gcp_build_still_needs_its_project():
    original = _with_timer(datetime.utcnow() + timedelta(hours=8))
    try:
        cls.provision(_FakeDB(), name="Demo", project="", created_by="t", cloud="gcp")
    except cls.CertLabError as exc:
        assert "project" in str(exc)
    else:
        raise AssertionError("a CAS pool was accepted with no project")
    finally:
        expiry_policy.default_expiry_for_kind = original


# ── the address ───────────────────────────────────────────────────────────────

def test_an_awspca_address_is_built_from_the_arn():
    arn = "arn:aws:acm-pca:us-east-2:111122223333:certificate-authority/1a2b3c"
    address = cls.address_for(_row(cloud="aws", backend="awspca", ca_arn=arn,
                                   location="us-east-2"))
    assert address.startswith("awspca?")
    assert f"arn={arn}" in address
    assert "region=us-east-2" in address
    # `pool=` is a gcpcas key. The plugin's validator refuses it on an awspca address, so
    # a leak here would surface as a rejected profile rather than a wrong one.
    assert "pool=" not in address


def test_an_address_is_refused_before_the_arn_exists():
    """`arn=` is the one option an awspca address cannot be built without, and it is not
    known until the apply returns it — so a row still building has nothing to compose
    from. Saying so beats composing an address the plugin refuses at the first rotation."""
    try:
        cls.address_for(_row(cloud="aws", backend="awspca", ca_arn="", location="us-east-2"))
    except cls.CertLabError as exc:
        assert "ARN" in str(exc)
    else:
        raise AssertionError("an awspca address was composed with no ARN")


# ── progress ──────────────────────────────────────────────────────────────────

def test_each_cloud_has_milestones_matching_its_own_resource_names():
    """Matched against `google_*` needles, an AWS apply would sit at its starting
    percentage for the whole build, which reads as a hung job."""
    for cloud, line in (("aws", "aws_acmpca_certificate_authority.this: creating..."),
                        ("gcp", "google_privateca_ca_pool.this: creating...")):
        assert any(needle in line for needle, _, _ in cls._milestones(cloud)), cloud
        assert not any(needle in line for needle, _, _
                       in cls._milestones("aws" if cloud == "gcp" else "gcp")
                       if needle not in ("destroying", "destruction complete")), (
            f"{cloud}'s output matched the other cloud's needles")


def test_the_aws_needles_do_not_swallow_one_another():
    """`_job_stream` breaks on the first match, so an earlier needle that is a prefix of
    a later resource name would freeze the bar at the wrong step."""
    line = "aws_acmpca_certificate_authority_certificate.this: creating..."
    matched = [msg for needle, _, msg in cls._milestones("aws") if needle in line]
    assert matched == ["Activating the CA…"], matched


def test_teardown_milestones_are_shared_because_they_are_terraforms_own_words():
    for cloud in cls.PROVISIONING_CLOUDS:
        needles = [n for n, _, _ in cls._milestones(cloud)]
        assert "destroying" in needles and "destruction complete" in needles, cloud


def _run_tests():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERR  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_tests() else 0)
