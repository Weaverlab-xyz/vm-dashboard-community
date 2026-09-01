"""Cloud POV templates: the topology a cloud POV is built from, and its refusals.

On Skytap a template is something the platform holds. No public cloud has one, so the
dashboard holds it — and everything checkable has to be checked at save, because the
alternative is a template that stores cleanly and fails eleven minutes into a provision
with half a VPC already built.

What is pinned here:

  * every refusal the editor depends on, by the remedy it names;
  * that a template is not portable between clouds, and cannot be re-pointed;
  * that the environment id is derived from the POV name and nothing else, which is what
    makes a half-built environment reapable;
  * that resolving an image happens LATE, and refuses a region mismatch rather than
    letting RunInstances report it as a missing image.

No cloud calls: nothing in the template path may need credentials, because a template
outlives them and is legitimately written before its image is promoted.

Runs under pytest, or standalone:
    python tests/test_pov_cloud_templates.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-cloud-templates")

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)

from web_dashboard.database import (Base, PovCloudTemplate,  # noqa: E402
                                    PovCloudTemplateVM)
from web_dashboard.services import lab_platforms as lp  # noqa: E402
from web_dashboard.services import pov_cloud_env  # noqa: E402
from web_dashboard.services import pov_cloud_template_service as svc  # noqa: E402

_CLOUD = lp.CLOUD_PLATFORMS[0]


def _session():
    """A private in-memory database.

    Never the shared `vm_cli.db`: these tests write templates, and the suite's own
    isolation trap is that `tests/` share that file AND the developer's `.env`.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[PovCloudTemplate.__table__, PovCloudTemplateVM.__table__])
    return sessionmaker(bind=engine)()


def _vm(**over):
    base = {"name": "target-01", "role": "target", "os_family": "linux",
            "image_id": "ami-0abc", "instance_type": "t3.medium", "disk_gb": 30}
    base.update(over)
    return base


def _refusal(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except svc.CloudTemplateError as exc:
        return str(exc)
    raise AssertionError("expected a CloudTemplateError, got none")


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_template_saves_with_its_vms_and_reads_back():
    db = _session()
    row = svc.create(db, cloud=_CLOUD, name="ps-eval", vms=[
        _vm(name="broker", role="broker"),
        _vm(name="win-target", os_family="windows", instance_type="t3.large"),
    ], description="Password Safe evaluation")
    out = svc.describe(db, row)
    assert out["cloud"] == _CLOUD
    assert out["name"] == "ps-eval"
    assert out["vm_count"] == 2
    assert out["broker_vm_name"] == "broker"
    assert [v["name"] for v in out["vms"]] == ["broker", "win-target"], \
        "VMs must come back in the order they were declared"


def test_a_blank_network_reports_what_it_will_actually_build():
    """The editor renders `effective_network_cidr`, so a template that leaves the field
    empty still shows the operator the range their POV will land in — rather than making
    them know the default."""
    db = _session()
    row = svc.create(db, cloud=_CLOUD, name="bare", vms=[_vm()])
    out = svc.describe(db, row)
    assert out["network_cidr"] == ""
    assert out["effective_network_cidr"] == pov_cloud_env.DEFAULT_NETWORK_CIDR


def test_editing_the_vm_list_replaces_it_wholesale():
    """A template's VM rows are a description with no identity — nothing references them.
    A diff would be machinery in exchange for nothing, and would silently keep a row the
    operator removed."""
    db = _session()
    row = svc.create(db, cloud=_CLOUD, name="two", vms=[_vm(name="a"), _vm(name="b")])
    svc.update(db, row, {"vms": [_vm(name="c")]})
    assert [v["name"] for v in svc.describe(db, row)["vms"]] == ["c"]
    assert db.query(PovCloudTemplateVM).count() == 1, "the old rows are still there"


def test_a_partial_update_leaves_the_vms_alone():
    db = _session()
    row = svc.create(db, cloud=_CLOUD, name="keep", vms=[_vm(name="a")])
    svc.update(db, row, {"description": "renamed only"})
    assert svc.describe(db, row)["vm_count"] == 1


# ── refusals at save, not at provision ───────────────────────────────────────

def test_two_broker_vms_are_refused():
    """Two brokers means two agents enrolled for one POV, each holding half the wire-up —
    which presents as an intermittently broken POV, not as a bad template."""
    db = _session()
    msg = _refusal(svc.create, db, cloud=_CLOUD, name="twobrokers", vms=[
        _vm(name="b1", role="broker"), _vm(name="b2", role="broker")])
    assert "broker" in msg.lower()


def test_a_template_with_no_vms_is_refused():
    db = _session()
    assert "at least one VM" in _refusal(svc.create, db, cloud=_CLOUD, name="empty",
                                         vms=[])


def test_a_vm_with_both_an_image_ref_and_a_literal_is_refused():
    db = _session()
    msg = _refusal(svc.create, db, cloud=_CLOUD, name="ambiguous",
                   vms=[_vm(image_ref="some-catalog-id", image_id="ami-0abc")])
    assert "both" in msg


def test_a_vm_with_neither_image_is_refused():
    db = _session()
    msg = _refusal(svc.create, db, cloud=_CLOUD, name="imageless",
                   vms=[_vm(image_id="")])
    assert "neither" in msg


def test_duplicate_vm_names_are_refused():
    """Names become resource tags and guest hostnames."""
    db = _session()
    msg = _refusal(svc.create, db, cloud=_CLOUD, name="dupes",
                   vms=[_vm(name="same"), _vm(name="same")])
    assert "same" in msg


def test_a_public_network_range_is_refused():
    db = _session()
    msg = _refusal(svc.create, db, cloud=_CLOUD, name="public", vms=[_vm()],
                   network_cidr="8.8.8.0/24")
    assert "public" in msg.lower()


def test_a_network_too_small_to_hold_a_pov_is_refused():
    db = _session()
    msg = _refusal(svc.create, db, cloud=_CLOUD, name="tiny", vms=[_vm()],
                   network_cidr="10.9.9.0/29")
    assert "/24" in msg


def test_an_unparseable_network_is_refused_before_a_row_exists():
    db = _session()
    _refusal(svc.create, db, cloud=_CLOUD, name="bad", vms=[_vm()],
             network_cidr="not-a-network")
    assert db.query(PovCloudTemplate).count() == 0, \
        "a refused template must leave no row behind"


def test_a_cloud_with_no_adapter_is_refused_by_name():
    """Named dynamically rather than hardcoded: `CLOUD_PLATFORMS` grows as drivers land,
    and a test naming the next one has to be edited by the commit that adds it."""
    db = _session()
    unsupported = next(c for c in ("gcp", "oci", "digitalocean", "nimbus")
                       if c not in lp.CLOUD_PLATFORMS)
    msg = _refusal(svc.create, db, cloud=unsupported, name="future", vms=[_vm()])
    assert _CLOUD in msg, "the refusal should say what IS supported"


def test_names_are_unique_per_cloud_not_globally():
    """An `aws` and an `azure` template describing the same POV are two rows by design;
    making one pick a different name would be a rename with no visible reason."""
    db = _session()
    svc.create(db, cloud=_CLOUD, name="shared", vms=[_vm()])
    assert "already exists" in _refusal(svc.create, db, cloud=_CLOUD, name="shared",
                                        vms=[_vm()])
    # The cross-cloud half cannot be exercised until a second adapter exists, so the
    # scoping is pinned at the source instead — a clash check that dropped the cloud
    # filter would pass every test above and only surface on the day Azure lands.
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "pov_cloud_template_service.py"), encoding="utf-8").read()
    body = src.split("def create(", 1)[1].split("\ndef ", 1)[0]
    assert "PovCloudTemplate.cloud == cloud" in body, \
        "the uniqueness check is not scoped to the cloud"


def test_a_template_cannot_be_re_pointed_at_another_cloud():
    """Its instance types and image ids mean nothing there, so the result would look
    saved and be unbuildable. Copying it is the honest version."""
    db = _session()
    row = svc.create(db, cloud=_CLOUD, name="pinned", vms=[_vm()])
    svc.update(db, row, {"cloud": "azure"})
    assert row.cloud == _CLOUD, "update honoured a cloud change it must ignore"


def test_deleting_a_template_takes_its_vm_rows_with_it():
    db = _session()
    row = svc.create(db, cloud=_CLOUD, name="doomed", vms=[_vm(name="a"), _vm(name="b")])
    svc.delete(db, row)
    assert db.query(PovCloudTemplate).count() == 0
    assert db.query(PovCloudTemplateVM).count() == 0, "orphaned VM rows left behind"


# ── the environment id ───────────────────────────────────────────────────────

def test_the_environment_id_comes_from_the_pov_name_and_nothing_else():
    """This is what makes a half-built environment reapable: the tag is derivable from
    the row's own name, with no round trip to a platform that may never have answered."""
    assert pov_cloud_env.env_id_for("acme-eval") == "povenv-acme-eval"
    assert pov_cloud_env.env_id_for("  ACME-Eval ") == "povenv-acme-eval", \
        "the id must not depend on how the name was typed"


def test_a_name_that_would_make_a_bad_tag_is_refused():
    for bad in ("", "x", "-leading", "has spaces", "UPPER_SCORE", "a" * 80):
        try:
            pov_cloud_env.env_id_for(bad)
        except pov_cloud_env.CloudEnvError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a POV name")


def test_a_tag_scoped_delete_refuses_anything_that_is_not_an_environment_id():
    """The teardown selects by tag. Handed an arbitrary value it would select resources
    this dashboard does not own, so the prefix check is a safety boundary, not a format
    nicety."""
    import asyncio
    for bad in ("", "i-0123456789", "vpc-abc", "prod"):
        try:
            asyncio.run(pov_cloud_env.delete_environment(_CLOUD, bad))
        except pov_cloud_env.CloudEnvError as exc:
            assert "POV environment id" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"a tag-scoped delete accepted {bad!r}")


def test_the_subnet_is_carved_out_of_the_declared_network():
    assert pov_cloud_env.subnet_cidr("10.20.0.0/16") == "10.20.0.0/24"
    assert pov_cloud_env.subnet_cidr("192.168.5.0/24") == "192.168.5.0/24", \
        "a network already /24 or smaller is used as-is"


def test_every_resource_carries_the_environment_tag():
    tags = pov_cloud_env.base_tags("povenv-x")
    assert tags[pov_cloud_env.TAG_ENVIRONMENT] == "povenv-x"
    assert tags[pov_cloud_env.TAG_MANAGED_BY] == pov_cloud_env.MANAGED_BY, (
        "without a managed-by tag the 'everything on the platform' listing cannot tell a "
        "POV VPC from the customer's own")


def test_a_pov_resource_also_carries_the_estate_wide_managed_by_tag():
    """`/costs` sums `managed-by=vm-dashboard` as the dashboard scope. A billable
    dashboard-created resource missing it is invisible there — the bug behind the SSM
    interface endpoints that tests/test_managed_by_tag_values was written about. It
    matters here because a community user can point a demo instance and a POV instance at
    one account, and only the demo one has a cost page."""
    tags = pov_cloud_env.base_tags("povenv-x")
    assert tags.get("managed-by") == "vm-dashboard"
    assert pov_cloud_env.TAG_MANAGED_BY != "managed-by", (
        "the POV selector and the estate-wide tag must stay separate keys, or a "
        "tag-scoped POV teardown would select a demo instance's VMs")


# ── image resolution happens late, and refuses a region mismatch ─────────────

class _Row:
    def __init__(self, **kw):
        self.name = "vm"
        self.image_id = ""
        self.image_ref = ""
        self.__dict__.update(kw)


def test_a_literal_image_id_wins_and_needs_no_catalog():
    assert pov_cloud_env.resolve_image(_Row(image_id="ami-0abc"), _CLOUD,
                                       "us-east-2") == "ami-0abc"


def test_a_vm_naming_no_image_at_all_is_refused_with_the_remedy():
    try:
        pov_cloud_env.resolve_image(_Row(), _CLOUD, "us-east-2")
    except pov_cloud_env.CloudEnvError as exc:
        assert "edit the template" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a VM with no image resolved to something")


def test_nothing_in_the_template_path_reaches_a_cloud():
    """A template outlives the credentials, and writing one before its image is promoted
    is legitimate. A cloud call here would make both impossible."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "pov_cloud_template_service.py"), encoding="utf-8").read()
    for banned in ("boto3", "aws_service", "pov_cloud_aws", "import httpx"):
        assert banned not in src, (
            f"pov_cloud_template_service reaches {banned}; saving a template must not "
            f"need credentials")

# ── the broker VM is built last, and that ordering is the feature ────────────

class _TplRow:
    name = "t"


def _vm_rows(*specs):
    class R:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    return [R(name=s.get("name"), role=s.get("role", "target"),
              os_family="linux", image_id="ami-0abc", image_ref="",
              instance_type="t3.medium", disk_gb=30) for s in specs]


def test_the_initial_create_leaves_the_broker_vm_out():
    """The policy the broker boots with names the TARGETS' addresses, and those do not
    exist until the targets do. Including the broker here would mean either a policy that
    grants nothing or a second boot to fix it."""
    rows = _vm_rows({"name": "broker", "role": "broker"}, {"name": "web01"})
    specs = pov_cloud_env.vm_specs(_TplRow(), rows, _CLOUD, "us-east-2")
    assert [s["name"] for s in specs] == ["web01"]


def test_the_broker_pass_builds_only_the_broker_and_carries_the_payload():
    rows = _vm_rows({"name": "broker", "role": "broker"}, {"name": "web01"})
    specs = pov_cloud_env.vm_specs(_TplRow(), rows, _CLOUD, "us-east-2",
                                   roles=("broker",), bootstrap="PAYLOAD")
    assert [s["name"] for s in specs] == ["broker"]
    assert specs[0]["user_data"] == "PAYLOAD"


def test_no_target_ever_receives_the_bootstrap():
    """A target with the enrolment code in its user-data would enrol a second agent for
    the POV, each holding half the wire-up."""
    rows = _vm_rows({"name": "broker", "role": "broker"}, {"name": "web01"})
    specs = pov_cloud_env.vm_specs(_TplRow(), rows, _CLOUD, "us-east-2",
                                   roles=("target", "broker"), bootstrap="PAYLOAD")
    for spec in specs:
        assert (spec["user_data"] == "PAYLOAD") == (spec["role"] == "broker"), spec


def test_a_template_of_nothing_but_a_broker_is_refused_at_build():
    """It would build an environment with a broker and nothing to broker for."""
    import asyncio
    db = _session()
    # A region on the template, so the refusal is reached without `default_region()`
    # asking config_service for one — this session holds the template tables only.
    row = svc.create(db, cloud=_CLOUD, name="brokeronly", region="us-east-2",
                     vms=[_vm(name="broker", role="broker")])
    original = pov_cloud_env.load_template
    pov_cloud_env.load_template = lambda tid, cloud: (row, svc.vms_of(db, row.id))
    try:
        asyncio.run(pov_cloud_env.create_environment(_CLOUD, row.id, "some-pov"))
    except pov_cloud_env.CloudEnvError as exc:
        assert "target" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an all-broker template built an environment")
    finally:
        pov_cloud_env.load_template = original


def test_the_broker_install_branches_on_the_mechanism_and_never_injects_on_a_cloud():
    """Cloud-init runs user-data on FIRST boot. Handing a payload to an instance that is
    already up does nothing at all, silently — which is why `cloud_init` is its own enum
    value rather than a second name for `metadata`."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_broker.py"),
               encoding="utf-8").read()
    body = src.split("async def ensure_broker(", 1)[1].split("\nasync def ", 1)[0]
    assert 'mechanism == "metadata"' in body, "ensure_broker does not branch"
    assert "create_broker_vm(" in body, "the cloud path never builds a broker VM"
    inject_at = body.index("inject_bootstrap")
    branch_at = body.index('if mechanism == "metadata":')
    assert branch_at < inject_at, (
        "inject_bootstrap is reached before the mechanism branch, so a cloud POV would "
        "be handed a payload nothing executes")


def test_rebuilding_the_broker_is_scoped_to_one_environment():
    """Two POVs can each have a VM called `broker`. Selecting by name alone would
    terminate a live customer's agent host to make room for somebody else's."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_aws.py"),
               encoding="utf-8").read()
    body = src.split("def _remove_vms_sync(", 1)[1].split("\nasync def ", 1)[0]
    assert "_env_filter(env_id)" in body, "the terminate is not scoped to the environment"
    assert "TAG_NAME" in body, "the terminate is not scoped to the VM name"

# ── the footprint, and the orphan question the page exists for ───────────────

def _env(env_id, vms):
    return {"id": env_id, "vm_count": len(vms), "runstate": "running",
            "region": "us-east-2", "vms": vms}


def _cvm(runstate="running", disk=30, itype="t3.medium"):
    return {"id": "i-1", "name": "x", "os_family": "linux", "runstate": runstate,
            "private_ip": "10.20.0.5", "instance_type": itype, "disk_gb": disk,
            "published_services": []}


def test_the_footprint_counts_stopped_vms_and_their_disks():
    """A suspended POV is not a free POV. Counting only running VMs would let the page
    imply an environment costs nothing overnight, which is the single most expensive
    misunderstanding this feature can create."""
    from web_dashboard.services import pov_cloud_cost as cost
    out = cost.footprint([_env("povenv-a", [_cvm(), _cvm("stopped"), _cvm("stopped")])])
    assert out["vms"] == 3
    assert out["vms_running"] == 1
    assert out["disk_gb"] == 90, "stopped VMs' disks still bill and must still be counted"


def test_the_footprint_lists_the_shapes_so_an_oversized_template_is_visible():
    from web_dashboard.services import pov_cloud_cost as cost
    out = cost.footprint([_env("povenv-a", [_cvm(itype="m5.4xlarge"), _cvm()])])
    assert out["instance_types"] == ["m5.4xlarge", "t3.medium"]


def test_an_estimate_is_refused_rather_than_guessed_when_there_is_no_price():
    """A hardcoded price table goes stale silently and reports a number somebody plans
    around. No answer, with the reason, is the smaller lie."""
    import asyncio
    from web_dashboard.services import pov_cloud_cost as cost
    # Async since the price lookups moved onto the cloud executors — a memoised read on
    # every pass but the first, which is real HTTP and must not block the worker's loop.
    out = asyncio.run(cost.estimate([_env("povenv-a", [_cvm()])], "moon-base-1"))
    assert out["available"] is False
    assert "moon-base-1" in out["reason"]


def test_the_overview_counts_orphans_in_the_footprint():
    """An orphan costs exactly what a tracked POV costs. A total that excluded it would
    understate the bill by the part nobody is watching — which is the part this page was
    added to surface."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "pov_cloud.py"),
               encoding="utf-8").read()
    body = src.split("async def overview(", 1)[1]
    assert "footprint(known + orphans)" in body, \
        "the footprint excludes orphans, so the page understates what is billing"
    assert "estimate(known + orphans" in body


def test_a_destroyed_pov_row_does_not_hide_an_orphan():
    """If the teardown left something behind, the row says `destroyed` and the cloud says
    otherwise. Matching against destroyed rows would file that as a tracked environment —
    exactly the case where somebody needs to be told."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "pov_cloud.py"),
               encoding="utf-8").read()
    body = src.split("async def overview(", 1)[1]
    assert "STATUS_DESTROYED" in body, \
        "the row lookup does not exclude destroyed POVs"


def test_the_cloud_page_creates_nothing():
    """A POV instance may hold cloud credentials now. The reason /aws stays 404 there is
    that its deploys resolve the global BeyondTrust tenant, and a deploy control on this
    page would reopen exactly that hole."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "pov_cloud.py"),
               encoding="utf-8").read()
    for verb in ("@router.post", "@router.delete", "@router.put", "@router.patch"):
        assert verb not in src, f"the POV cloud view exposes {verb}"
    page = open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "cloud.html"),
                encoding="utf-8").read()
    for banned in ("method: 'POST'", 'method: "POST"', "method: 'DELETE'"):
        assert banned not in page, f"the POV cloud page issues a {banned}"


def test_the_cloud_page_sends_its_bearer_token():
    """The app sets no cookie and the JWT lives in localStorage, so a bare fetch() is an
    ANONYMOUS request. The whole POV page was 401'd for six calls once, for this."""
    page = open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "cloud.html"),
                encoding="utf-8").read()
    assert "Authorization" in page and "Bearer" in page
    assert page.count("await fetch(") == 1, \
        "more than one raw fetch on the page — one of them is not going through apiFetch"

def test_a_network_with_no_instances_left_is_still_found():
    """The orphan the page most needs to show: a teardown that terminated the VMs and then
    failed on the VPC. Grouping environments from the instances alone would render that
    state invisible — and it is the one where somebody has to go and finish the job."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_aws.py"),
               encoding="utf-8").read()
    listing = src.split("async def list_environments(", 1)[1].split("\ndef ", 1)[0]
    assert "_network_env_ids_sync" in listing, (
        "list_environments groups on instances only, so an environment whose VMs are gone "
        "but whose network survives never appears")
    read = src.split("async def read_environment(", 1)[1].split("\nasync def ", 1)[0]
    assert "_network_env_ids_sync" in read, (
        "read_environment answers None when the instances are gone, which would let the "
        "reconcile flag a POV as missing while its VPC is still billing")


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
