"""The Certificate Lab's per-cloud dispatch.

`tests/test_cert_lab_wiring.py` pins the wiring that fails late and quietly — the module
reaching the image, the job types, the reapable kind. This is the other half: the service
picking the right module, the right variables and the right backend name for the row's
cloud, which is what a second cloud actually costs.

It also covers the ids, which is where this feature's sharpest edge is: CAS never frees a
deleted pool's id, so an id derived from the CA's name alone makes the first REBUILD of
that name impossible for ever — and the identity the plugin authenticates as is unique per
project (GCP) or per account (AWS), so a shared constant collides between labs and with
the retry after a partial build. Plus the rollback that keeps a failed build from leaving
a live enrollment key behind.

The load-bearing one is `test_each_cloud_passes_only_variables_its_own_module_declares`.
Terraform treats an undeclared `-var` as a hard error before it touches anything, and
`_tf_variables` feeds the DESTROY as well as the apply — so a variable that leaked across
clouds would strand a CA that is already billing, which is the one failure this feature
cannot afford.

Runs under pytest or standalone:  python tests/test_cert_lab_clouds.py
"""
import asyncio
import contextlib
import os
import re
import sys
import types
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
    base = dict(id="7f3a9c1e-2b44-4d77-9a10-c0ffee123456",
                cloud="gcp", name="Demo", project="", location="", pool_id="",
                ca_arn="", backend="", enroll_account="", ca_chain_pem="",
                # NULL on a real row too: the build fills them in from the apply's
                # outputs, and a failure there leaves them None while the CA stands.
                ps_functional_account=None, ps_functional_account_id=None)
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
    assert row.ca_arn == "arn:aws:acm-pca:us-east-2:1:certificate-authority/x"
    assert row.location == "us-east-2"
    assert row.enroll_account == "AKIAEXAMPLE"
    assert row.pool_id == "", "an AWS build must not invent a pool id"


def test_a_gcp_build_still_records_the_pool_and_the_service_account():
    row = _row(cloud="gcp")
    cls._read_outputs(row, {"pool_id": "demo-pool", "location": "us-central1",
                            "ca_chain_pem": "-----BEGIN CERTIFICATE-----",
                            "service_account_email": "certauth@p.iam.gserviceaccount.com"})
    assert (row.pool_id, row.location) == ("demo-pool", "us-central1")
    # The whole value, not a suffix. `endswith("gserviceaccount.com")` would pass for
    # `evilgserviceaccount.com` too — weaker than it reads, and CodeQL flags the shape.
    assert row.enroll_account == "certauth@p.iam.gserviceaccount.com"
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
        # The name is the readable part; the suffix is what makes a REBUILD possible at
        # all (see test_a_pool_id_is_never_reusable_because_cas_never_frees_one).
        assert re.fullmatch(r"demo-pool-[0-9a-f]{6}", db.row.pool_id), db.row.pool_id
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


# ── the ids, which CAS never gives back ───────────────────────────────────────

def _provision_gcp(name, **kw):
    original = _with_timer(datetime.utcnow() + timedelta(hours=8))
    created = job_service.create_job
    job_service.create_job = lambda *a, **k: SimpleNamespace(id="job-1")
    try:
        db = _FakeDB()
        cls.provision(db, name=name, project="proj-1", created_by="t", cloud="gcp", **kw)
        return db.row
    finally:
        expiry_policy.default_expiry_for_kind = original
        job_service.create_job = created


def test_a_pool_id_is_never_reusable_because_cas_never_frees_one():
    """The one that matters here. CAS reserves a deleted pool's full name permanently:

        Error code 3, message: Previously used CaPool ids may not be reused.

    So an id derived from the CA's name ALONE can be built exactly once — every rebuild
    of that name is refused for good, in a feature whose whole point is that the CA gets
    destroyed. Two builds of one name must therefore ask for two different pools.
    """
    first, second = _provision_gcp("demo-pipeline"), _provision_gcp("demo-pipeline")
    assert first.pool_id != second.pool_id, first.pool_id
    for row in (first, second):
        assert row.pool_id.startswith("demo-pipeline-pool-"), row.pool_id


def test_a_generated_pool_id_and_the_ca_id_under_it_both_fit_cas_limits():
    """63 characters caps a pool id AND a certificate-authority id, and the module's CA
    id is `<pool>-root` — so a long name has to be cut short enough for both. Otherwise
    the apply fails on the CA after the pool exists, i.e. after it is billing."""
    row = _provision_gcp("a" * 200)
    assert len(row.pool_id) <= cls._POOL_ID_MAX, row.pool_id
    assert len(f"{row.pool_id}-root") <= cls._CAS_ID_MAX, row.pool_id
    assert cls._POOL_ID_RE.match(row.pool_id), row.pool_id


def test_a_free_text_name_is_slugged_into_something_cas_accepts():
    """The build form's name is free text and the pool id is derived from it, so
    "Demo Pipeline (EU)" has to become legal here rather than a 400 mid-apply."""
    row = _provision_gcp("Demo Pipeline (EU)")
    assert cls._POOL_ID_RE.match(row.pool_id), row.pool_id
    assert row.pool_id.startswith("demo-pipeline-eu-pool-"), row.pool_id


def test_an_explicit_pool_id_is_honoured_as_typed_and_an_illegal_one_is_refused():
    """Honoured, because the id goes on to name the pool in every address built against
    this CA and silently mangling it would be worse. Refused when CAS could not accept
    it, at the click rather than part-way through the apply."""
    assert _provision_gcp("Demo", pool_id="Hand-Typed_Pool1").pool_id == "hand-typed_pool1"
    for bad in ("has spaces", "dots.are.out", "x" * (cls._POOL_ID_MAX + 1)):
        try:
            _provision_gcp("Demo", pool_id=bad)
        except cls.CertLabError as exc:
            assert str(cls._POOL_ID_MAX) in str(exc), "the refusal should name the cap"
        else:
            raise AssertionError(f"{bad!r} was accepted as a pool id")


def test_the_enrollment_identity_is_per_lab_on_both_clouds():
    """A service account id is unique per PROJECT and an IAM user name per ACCOUNT, so
    the modules' shared `certauth-plugin` default means the second lab collides at create
    with alreadyExists — and so does the retry after an apply that created the identity
    and then failed on the pool, which is what a reused pool id does."""
    one = _row(id="11111111-2222-3333-4444-555555555555")
    two = _row(id="66666666-7777-8888-9999-aaaaaaaaaaaa")
    assert cls._enroll_identity_id(one) != cls._enroll_identity_id(two)
    # Stable: `_tf_variables` feeds the DESTROY as well as the apply, so the teardown has
    # to name the same identity the apply created.
    assert cls._enroll_identity_id(one) == cls._enroll_identity_id(_row(id=one.id))
    for cloud, var in (("gcp", "service_account_id"), ("aws", "iam_user_name")):
        row = _row(cloud=cloud, id=one.id, project="p", location="us-central1",
                   pool_id="demo-pool-ab12cd")
        assert cls._tf_variables(row)[var] == cls._enroll_identity_id(one)


def test_the_generated_service_account_id_fits_gcps_own_rules():
    """6-30 characters, starting with a letter and ending alphanumeric. A longer or
    otherwise malformed id is an INVALID_ARGUMENT on the create, after the pool exists."""
    ident = cls._enroll_identity_id(_row(id="7f3a9c1e-2b44-4d77-9a10-c0ffee123456"))
    assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", ident), ident
    assert 6 <= len(ident) <= 30, ident


# ── the failure an operator has to read ───────────────────────────────────────

def test_a_reused_pool_id_is_explained_with_the_way_out():
    """The provider's text is a wall of plan output with the cause on one line near the
    bottom — and that is the line truncation cuts when it lands on the row."""
    row = _row(cloud="gcp", pool_id="demo-pipeline-pool")
    text = ("Error: Error waiting to create CaPool: Error code 3, message: Previously "
            "used CaPool ids may not be reused. A `CaPool` for `projects/x/locations/"
            "us-central1/caPools/demo-pipeline-pool` has previously been deleted")
    out = cls._explain_apply_failure(row, text)
    assert out.startswith("CAS has permanently reserved"), out[:80]
    assert "demo-pipeline-pool" in out.split("\n\n")[0], "the explanation names the pool"
    assert text in out, "the provider's own error is kept"


def test_an_identity_left_behind_is_explained_and_names_itself():
    row = _row(cloud="gcp", pool_id="demo-pool-ab12cd")
    text = ("Error creating service account: googleapi: Error 409: Service account "
            "certauth-7f3a9c1e already exists within project projects/x., alreadyExists")
    out = cls._explain_apply_failure(row, text)
    assert cls._enroll_identity_id(row) in out
    assert "Destroy this row" in out


def test_an_unrecognised_failure_is_passed_through_untouched():
    """Prefixing everything would push the provider's own words down the row's 2000
    characters for no gain."""
    text = "Error: googleapi: Error 403: Permission privateca.caPools.create denied"
    assert cls._explain_apply_failure(_row(cloud="gcp"), text) == text


# ── a failed build must not leave the cloud dirty ─────────────────────────────
#
# `terraform apply` failing is not a no-op: the provider keeps everything it finished
# before the error. Observed on a GCP build whose pool id had been used before — the
# enrollment service account AND its key were created, then the pool create was refused,
# so the attempt left a live credential behind and an identity id that then collided with
# the retry. Mirrors tests/test_k8s_provision_rollback.py.

@contextlib.contextmanager
def _fake_terraform(*, apply_exc=None, destroy_exc=None, calls=None):
    """Swap the module attributes `run_provision_apply` actually reaches. Restored
    afterwards, since these are the real modules the rest of the suite imports."""
    calls = calls if calls is not None else []
    saved = (cls.terraform.apply, cls.terraform.destroy, cls.get_lab,
             job_service.set_running, job_service.set_completed, job_service.set_failed)
    ws_name = "web_dashboard.api.websocket"
    ws_prev = sys.modules.get(ws_name)

    async def _apply(deploy_dir, variables, template_dir=None, env=None, on_line=None):
        calls.append(("apply", deploy_dir, template_dir, dict(variables or {})))
        if apply_exc:
            raise apply_exc
        return {"pool_id": variables["pool_id"], "location": variables["location"],
                "ca_chain_pem": "-----BEGIN CERTIFICATE-----",
                "service_account_email": "certauth@p.iam.gserviceaccount.com"}

    async def _destroy(deploy_dir, variables=None, template_dir=None, env=None,
                       on_line=None):
        calls.append(("destroy", deploy_dir, template_dir, dict(variables or {})))
        if on_line:
            # The rollback's stream must not cancel-check: a cancelled apply is one of
            # the ways we get here, and aborting on the first line would leave behind
            # exactly the orphan the rollback exists to remove.
            await on_line("google_service_account.plugin: destroying...")
        if destroy_exc:
            raise destroy_exc

    ws = types.ModuleType(ws_name)

    async def _broadcast(job_id, pct, message, log_line=None):
        return None
    ws.broadcast_progress = _broadcast

    cls.terraform.apply, cls.terraform.destroy = _apply, _destroy
    job_service.set_running = lambda db, job_id: None
    job_service.set_completed = lambda db, job_id, result=None: calls.append(("completed",))
    job_service.set_failed = lambda db, job_id, error: calls.append(("failed", error))
    sys.modules[ws_name] = ws
    try:
        yield calls
    finally:
        (cls.terraform.apply, cls.terraform.destroy, cls.get_lab,
         job_service.set_running, job_service.set_completed,
         job_service.set_failed) = saved
        if ws_prev is None:
            sys.modules.pop(ws_name, None)
        else:
            sys.modules[ws_name] = ws_prev


def _drive_apply(*, apply_exc=None, destroy_exc=None):
    """Run the provision worker once. Returns (row, calls)."""
    row = _row(cloud="gcp", project="p", location="us-central1",
               pool_id="demo-pipeline-pool", status="provisioning",
               deploy_job_id="job-1", error_message=None, updated_at=None)
    with _fake_terraform(apply_exc=apply_exc, destroy_exc=destroy_exc) as calls:
        cls.get_lab = lambda db, lab_id: row
        asyncio.run(cls.run_provision_apply(_FakeDB(), lab_id=row.id, job_id="job-1"))
    return row, calls


def test_a_failed_apply_destroys_what_it_managed_to_create():
    row, calls = _drive_apply(apply_exc=RuntimeError(
        "Error waiting to create CaPool: Previously used CaPool ids may not be reused"))
    assert row.status == "failed"
    destroys = [c for c in calls if c[0] == "destroy"]
    assert len(destroys) == 1, calls
    # Same deploy dir, same module, same -var set as the apply — the destroy evaluates
    # the config too, and the state it acts on is the failed apply's own.
    apply_call = next(c for c in calls if c[0] == "apply")
    assert destroys[0][1:] == apply_call[1:], (destroys[0], apply_call)
    assert "[rollback] The partial build was destroyed" in row.error_message


def test_a_successful_apply_never_destroys_anything():
    row, calls = _drive_apply()
    assert row.status == "available"
    assert not [c for c in calls if c[0] == "destroy"], calls


def test_a_rollback_that_fails_says_so_without_hiding_the_real_error():
    """The apply error is the thing the operator needs to read, and a pool that is still
    live is still billing — so a broken rollback appends a warning rather than replacing
    the cause."""
    row, calls = _drive_apply(
        apply_exc=RuntimeError("Previously used CaPool ids may not be reused"),
        destroy_exc=RuntimeError("Error acquiring the state lock"))
    assert row.error_message.startswith("CAS has permanently reserved")
    assert "MANUAL CLEANUP REQUIRED" in row.error_message
    assert "Error acquiring the state lock" in row.error_message
    failed = next(c for c in calls if c[0] == "failed")
    assert "MANUAL CLEANUP REQUIRED" in failed[1]


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
