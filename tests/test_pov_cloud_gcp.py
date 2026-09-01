"""The GCP POV driver: label spelling, zone resolution, and the project boundary.

GCP sits between the other two clouds. Like AWS it has no native environment object, so
teardown unpicks resource types rather than deleting one group. Unlike either, two of its
API's own constraints cannot be met by the shared code:

  * **a GCE label key must be lowercase**, so `povEnvironment` is refused outright and the
    shared tag keys have to be mapped;
  * **only some GCE resources carry labels at all** — an Instance does, a Network,
    Subnetwork and Firewall do not — so the network layer is selected by NAME instead.

And one trap this codebase has already paid for once: `f"{region}-a"` is not a zone in
us-east1 or europe-west1, and GCE reports a nonexistent zone as a 403 that reads like a
permissions problem.

No GCP SDK and no network: the parts needing protobuf models are pinned at the source.

Runs under pytest, or standalone:
    python tests/test_pov_cloud_gcp.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-cloud-gcp")

from web_dashboard.services import lab_platforms as lp  # noqa: E402
from web_dashboard.services import pov_cloud_env  # noqa: E402
from web_dashboard.services import pov_cloud_gcp as gcp  # noqa: E402

_SRC = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_gcp.py"),
            encoding="utf-8").read()

# GCE's own rule for a label key: lowercase to start, then lowercase, digits, dashes and
# underscores. Capitals are refused outright.
import re  # noqa: E402
_LABEL_KEY_RE = re.compile(r"^[a-z]([-_a-z0-9]*)?$")
_LABEL_VALUE_RE = re.compile(r"^[-_a-z0-9]*$")


def _body(fn: str) -> str:
    return _SRC.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]


def _code(fn: str) -> str:
    """A function's executable source: no comments, and no docstring either.

    Parsed and unparsed through `ast` rather than filtered line by line. The first version
    stripped `#` comments only, and these drivers *document* the calls they deliberately
    do not make — "stop, not suspend", "user-data, not startup-script" — so three
    assertions read a docstring's warning as the bug it warns about. A prose-only test is
    worse than no test: it fails on correct code and goes green the day somebody deletes
    the comment.
    """
    import ast
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            # Quotes normalised to double, because `ast.unparse` emits single ones and an
            # assertion written in the source's own style would silently never match.
            out = "\n".join(ast.unparse(stmt) for stmt in body)
            return out.replace("'", '"')
    raise AssertionError(f"no function named {fn} in the module under test")


# ── the registry entry ───────────────────────────────────────────────────────

def test_gcp_is_a_built_cloud_with_an_adapter_and_a_driver():
    assert "gcp" in lp.CLOUD_PLATFORMS
    assert "gcp" in pov_cloud_env._DRIVER_MODULE
    adapter = lp.adapter("gcp")
    for fn in lp.READ_CONTRACT:
        assert callable(getattr(adapter, fn, None)), f"gcp adapter lacks {fn}"


def test_gcp_claims_projects_because_it_has_a_container_worth_recording():
    """A GCP project is a real boundary an environment is built INSIDE, so the POV row
    records which one and the teardown reads it back.

    Named against the two clouds whose answer is NO and is a decision — an AWS account and
    an Azure subscription are instance-wide settings, not a per-POV choice, so neither has
    anything for the create form to ask. Deliberately not "GCP is the only one": OCI's
    compartment is the same kind of boundary and claims it too, and a test asserting
    exclusivity would have had to be edited by the commit that added it.
    """
    assert lp.supports("gcp", "projects") is True
    assert isinstance(lp.adapter("gcp").configured_project_id(), str)
    for scoped_instance_wide in ("aws", "azure"):
        assert lp.supports(scoped_instance_wide, "projects") is False, (
            f"{scoped_instance_wide} claims projects, but its account or subscription is "
            f"an instance-wide setting rather than a per-POV choice")


def test_gcp_has_no_platform_login_and_no_share_link():
    assert lp.supports("gcp", "stored_credentials") is False
    assert lp.supports("gcp", "share_link") is False
    assert lp.supports("gcp", "idle_suspend") is False
    assert lp.supports("gcp", "scheduled_suspend") is True


# ── labels, in the only spelling GCE accepts ─────────────────────────────────

def test_every_label_key_and_value_is_one_gce_will_accept():
    """`povEnvironment` is refused by the API with an error naming the field rather than
    the tag, so the shared keys have to be mapped rather than passed through."""
    labels = gcp._labels("povenv-acme-eval", role="broker")
    assert labels, "no labels produced"
    for key, value in labels.items():
        assert _LABEL_KEY_RE.match(key), f"{key!r} is not a valid GCE label key"
        assert _LABEL_VALUE_RE.match(value), f"{value!r} is not a valid GCE label value"


def test_the_shared_tag_keys_would_not_have_been_accepted():
    """The reason the mapping exists, asserted rather than assumed — so nobody deletes it
    as redundant."""
    assert not _LABEL_KEY_RE.match(pov_cloud_env.TAG_ENVIRONMENT), (
        "the shared environment tag key is now lowercase, so the GCP mapping may be "
        "redundant — check before removing it")


def test_every_shared_tag_key_has_a_gcp_spelling():
    """A key with no mapping is silently dropped from `_labels`, which would leave the
    teardown filtering on something nothing carries."""
    for key in (pov_cloud_env.TAG_ENVIRONMENT, pov_cloud_env.TAG_MANAGED_BY,
                pov_cloud_env.TAG_ESTATE, pov_cloud_env.TAG_ROLE):
        assert key in gcp._LABEL_KEY, f"{key} has no GCE label spelling"


def test_the_environment_label_is_what_the_teardown_filters_on():
    env_id = "povenv-acme"
    assert gcp._label_filter(env_id) == f"labels.pov_environment={env_id}"
    assert gcp._labels(env_id)["pov_environment"] == env_id


def test_a_pov_resource_carries_the_estate_wide_managed_by_label_too():
    """`/costs` sums `managed-by=vm-dashboard` as the dashboard scope."""
    assert gcp._labels("povenv-x").get("managed-by") == "vm-dashboard"


# ── the zone trap ────────────────────────────────────────────────────────────

def test_the_zone_is_resolved_from_the_region_and_never_assembled():
    """us-east1 and europe-west1 start at `-b`. GCE reports a nonexistent zone as
    `403 Permission denied on 'locations/us-east1-a' (or it may not exist)`, which reads
    as a credentials problem — so this mistake does not look like itself."""
    body = _code("_zone_sync")
    assert "RegionsClient" in body, "the zone is not read from the region"
    for fn in ("_zone_sync", "_create_vms_sync", "_create_network_sync"):
        assert '-a"' not in _code(fn), f"{fn} assembles a zone name"
        assert "'-a'" not in _code(fn), f"{fn} assembles a zone name"


def test_the_subnet_and_the_instances_share_a_region_by_construction():
    """GCE rejects a cross-region pairing at insert time with 'Scope of the specified
    subnetwork doesn't match the scope of the instance', naming neither region — and on a
    multi-VM build the earlier instances already exist by then."""
    body = _code("_create_vms_sync")
    assert "_zone_sync(project, region)" in body, (
        "the zone is not derived from the same region the subnet was built in")


# ── the network layer is named, not labelled ─────────────────────────────────

def test_the_network_resources_are_selected_by_name_because_they_carry_no_labels():
    """A GCE Network, Subnetwork and Firewall have no labels field at all. Filtering them
    by label would match nothing and leave the network behind on every teardown."""
    body = _code("_delete_sync")
    assert "_resource_name(env_id" in body, \
        "the teardown looks the network layer up by something other than its name"
    for suffix in ("-fw", "-subnet", "-net"):
        assert suffix in body, f"the teardown never deletes the {suffix} resource"


def test_the_resource_names_are_derived_from_the_environment_id():
    assert gcp._resource_name("povenv-acme", "-net") == "povenv-acme-net"
    assert gcp._resource_name("povenv-acme", "-subnet") == "povenv-acme-subnet"


def test_a_pov_name_too_long_for_gce_is_refused_with_the_real_limit():
    """A GCE resource name is capped at 63 characters, and the POV name rule allows 63 on
    its own — so the network layer is where a long name breaks. Refused with a number
    rather than truncated, because truncation is how two POVs collide."""
    long_id = "povenv-" + ("a" * 60)
    try:
        gcp._resource_name(long_id, "-subnet")
    except pov_cloud_env.CloudEnvError as exc:
        assert "63" in str(exc) and "too long" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an over-long GCE resource name was accepted")


def test_a_teardown_is_not_stopped_by_a_name_it_cannot_build():
    """The refusal above must not strand the instances. If the network names cannot be
    derived, the VMs are still deleted and the rest is stepped over."""
    body = _code("_delete_sync")
    assert "except env.CloudEnvError" in body and "continue" in body, (
        "an un-derivable resource name aborts the teardown instead of being skipped")
    assert body.index("instances") < body.index("_resource_name"), \
        "the network is unpicked before the instances that hold addresses in it"


# ── power, and what suspend means here ───────────────────────────────────────

def test_suspending_stops_rather_than_using_gce_suspend():
    """GCE's `suspend` preserves RAM to disk and charges for that storage plus reserved
    resources. `stop` lands on TERMINATED, where only the disks bill — which is the state
    a suspend schedule is aiming at, despite the name."""
    body = _code("_power_sync")
    assert "instances.stop(" in body, "the suspend path does not stop the instances"
    assert ".suspend(" not in body, (
        "the suspend path calls GCE suspend, which keeps charging for reserved resources")


def test_terminated_reads_back_as_stopped():
    """GCE's word for a stopped instance is TERMINATED, which elsewhere in this codebase
    means gone. The mapping is what stops the POV page reporting a suspended environment
    as destroyed."""
    assert gcp._RUNSTATE["terminated"] == "stopped"
    assert gcp._RUNSTATE["running"] == "running"
    assert "terminating" in gcp._DEAD_STATES
    assert "terminated" not in gcp._DEAD_STATES, (
        "a stopped instance is being treated as gone, so every suspended POV would read "
        "as missing from the platform")


def test_rebuilding_the_broker_is_scoped_to_one_environment():
    """Two POVs can each have a `broker`."""
    body = _code("_remove_vms_sync")
    assert "_label_filter(env_id)" in body, "the delete is not scoped to the environment"
    assert "wanted" in body, "the delete is not scoped to the VM name"


# ── the project boundary ─────────────────────────────────────────────────────

def test_the_project_is_read_from_the_row_before_current_config():
    """`expiry_reaper` states the rule: a destroy aimed at the wrong project is the worst
    version of this bug. A POV built before the setting changed must still be destroyable
    in the project it actually went into."""
    body = _code("_project")
    assert "recorded_project" in body, "the project is taken from config, not the row"
    assert body.index("recorded_project") < body.index("configured_project_id"), \
        "current config is consulted before the recorded project"


def test_a_missing_project_is_refused_with_the_remedy():
    original = gcp.configured_project_id
    gcp.configured_project_id = lambda: ""
    try:
        gcp._project()
    except pov_cloud_env.CloudEnvError as exc:
        assert "Settings" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a blank project fell through")
    finally:
        gcp.configured_project_id = original


def test_reading_the_project_or_region_never_raises_on_a_missing_database():
    """These are read from the lab-platform registry, which the UI and the tests exercise
    constantly and sometimes before a database exists. `gcp_service._cfg` lets a config
    failure out, which is fine on the demo path and not here."""
    assert isinstance(gcp.default_region(), str)
    assert gcp.default_region(), "the region has no fallback"
    assert isinstance(gcp.configured_project_id(), str)


# ── bootstrap delivery ───────────────────────────────────────────────────────

def test_the_bootstrap_goes_in_user_data_not_startup_script():
    """`startup-script` is re-run by the guest agent on EVERY boot. The payload carries a
    single-use enrolment code, so a re-run would spend the rest of the POV's life failing
    to redeem a spent one. `user-data` is read by cloud-init, once."""
    assert gcp._USER_DATA_KEY == "user-data"
    body = _code("_create_vms_sync")
    assert "startup-script" not in body, (
        "the bootstrap is delivered as a startup script, which re-runs on every boot")


def test_only_the_broker_receives_the_bootstrap():
    """The driver must not decide WHICH VM is the broker.

    `pov_cloud_env.vm_specs` already puts the payload on the broker alone. A driver that
    re-derived that would be a second answer to one question, and two answers disagreeing
    is how a target ends up holding the enrolment code.
    """
    body = _code("_create_vms_sync")
    assert '"broker"' not in body, (
        "the driver decides for itself which VM is the broker instead of taking the "
        "payload the shared vm_specs already put on the spec")
    assert "user_data" in body, "the driver never reads the bootstrap at all"


def test_no_cloud_nat_is_created():
    """A Cloud NAT is a standing hourly charge before a byte moves. An ephemeral external
    address per VM is cheaper for a handful of VMs and exposes nothing, because GCP denies
    ingress by default and the one firewall rule allows only the subnet itself."""
    for fn in ("_create_network_sync", "_create_vms_sync"):
        assert "nat" not in _code(fn).lower().replace("one_to_one_nat", "").replace(
            "external nat", ""), f"{fn} creates a Cloud NAT"


def test_the_only_firewall_rule_is_the_environment_talking_to_itself():
    body = _code("_create_network_sync")
    assert 'direction="INGRESS"' in body
    assert "source_ranges=[sub_cidr]" in body, (
        "the firewall rule's source is something other than the POV's own subnet")
    assert "0.0.0.0/0" not in body, "the firewall opens the environment to the internet"


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
