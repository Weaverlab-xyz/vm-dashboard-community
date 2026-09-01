"""The OCI POV driver: the security list, the shape suffix, and the compartment.

OCI is shaped like AWS — no environment object, so a teardown unpicks resource types in
dependency order. Three things are its own, and each fails in a way that names something
other than the cause:

  * **every VCN gets a default security list that allows SSH from anywhere**, so a POV
    placed on it would be a customer environment with port 22 open to the internet;
  * **a Flex shape is refused without an explicit OCPU count**, and the API's error names
    `shapeConfig` rather than the template field that produced it;
  * **`user_data` must be base64**, or the instance boots fine and never runs its
    bootstrap.

And one decision worth defending rather than assuming: a compartment is NOT used as the
environment, despite looking exactly like an Azure resource group.

No OCI SDK and no network: the parts needing SDK models are pinned at the source.

Runs under pytest, or standalone:
    python tests/test_pov_cloud_oci.py
"""
import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-cloud-oci")

from web_dashboard.services import lab_platforms as lp  # noqa: E402
from web_dashboard.services import pov_cloud_env  # noqa: E402
from web_dashboard.services import pov_cloud_oci as oci  # noqa: E402

_SRC = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_oci.py"),
            encoding="utf-8").read()


def _code(fn: str) -> str:
    """A function's executable source: no comments, and no docstring either.

    Through `ast`, for the reason the GCP and Azure suites give: this driver *documents*
    the calls it deliberately does not make, so a line-filtered scan reads the warning as
    the bug and then passes again the day somebody makes it.
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
            out = "\n".join(ast.unparse(stmt) for stmt in body)
            return out.replace("'", '"')
    raise AssertionError(f"no function named {fn} in the module under test")


def _refused(value) -> str:
    try:
        oci.parse_shape(value)
    except pov_cloud_env.CloudEnvError as exc:
        return str(exc)
    raise AssertionError(f"{value!r} was accepted as a shape")


# ── the registry entry ───────────────────────────────────────────────────────

def test_oci_is_a_built_cloud_with_an_adapter_and_a_driver():
    assert "oci" in lp.CLOUD_PLATFORMS
    assert "oci" in pov_cloud_env._DRIVER_MODULE
    adapter = lp.adapter("oci")
    for fn in lp.READ_CONTRACT:
        assert callable(getattr(adapter, fn, None)), f"oci adapter lacks {fn}"


def test_all_four_clouds_are_now_built():
    """The registry's claim from the first slice, finally exercised four times."""
    assert set(lp.CLOUD_PLATFORMS) == {"aws", "azure", "gcp", "oci"}
    for cloud in lp.CLOUD_PLATFORMS:
        caps = lp.capabilities(cloud)
        assert caps["idle_suspend"] is False, f"{cloud} claims a platform idle timer"
        assert caps["scheduled_suspend"] is True
        assert caps["share_link"] is False, f"{cloud} claims a share link"
        assert caps["bootstrap_injection"] == "cloud_init"


def test_oci_records_its_compartment_like_gcp_records_its_project():
    assert lp.supports("oci", "projects") is True
    assert isinstance(lp.adapter("oci").configured_project_id(), str)


def test_only_azure_mints_a_platform_login():
    """Azure's `os_profile` forces one at VM creation; the other three do not, so a POV
    there takes its login from the image and its Vault account."""
    assert lp.supports("azure", "stored_credentials") is True
    for cloud in ("aws", "gcp", "oci"):
        assert lp.supports(cloud, "stored_credentials") is False, f"{cloud} claims one"


# ── the default security list is the trap ────────────────────────────────────

def test_the_driver_creates_its_own_security_list():
    """Every VCN gets a default security list that allows SSH from 0.0.0.0/0. A POV on it
    would be a customer environment with port 22 open to the internet."""
    body = _code("_create_network_sync")
    assert "create_security_list" in body, "the driver uses the VCN's default rules"
    assert "security_list_ids=[security.id]" in body, (
        "the subnet does not attach the driver's own security list, so it falls back to "
        "the default — which allows SSH from anywhere")


def test_the_only_ingress_rule_is_the_environment_talking_to_itself():
    body = _code("_create_network_sync")
    assert "IngressSecurityRule" in body, "no ingress rule is declared at all"
    ingress = body.split("ingress_security_rules", 1)[1]
    assert "sub_cidr" in ingress, "the ingress rule's source is not the POV's own subnet"
    assert "0.0.0.0/0" not in ingress, "an ingress rule admits the whole internet"


def test_egress_is_open_because_everything_dials_out():
    """The agent polls, the Gateway reaches PRA, the Resource Broker reaches Password
    Safe. Nothing needs to reach in."""
    body = _code("_create_network_sync")
    assert "EgressSecurityRule" in body
    assert "0.0.0.0/0" in body.split("egress_security_rules", 1)[1]


def test_no_nat_gateway_is_created():
    """A NAT gateway is a standing charge before a byte moves. A public address per VM is
    cheaper for a handful and exposes nothing, because the security list admits only the
    POV's own subnet."""
    for fn in ("_create_network_sync", "_create_vms_sync"):
        assert "nat_gateway" not in _code(fn).lower(), f"{fn} creates a NAT gateway"
    assert "assign_public_ip=True" in _code("_create_vms_sync")


# ── shapes ───────────────────────────────────────────────────────────────────

def test_a_flex_shape_gets_a_size_it_would_otherwise_be_refused_without():
    """OCI rejects a Flex shape with no shape_config, naming `shapeConfig` rather than the
    template field that produced it."""
    shape, ocpus, memory = oci.parse_shape("VM.Standard.E4.Flex")
    assert shape == "VM.Standard.E4.Flex"
    assert ocpus and ocpus > 0 and memory and memory > 0


def test_a_fixed_shape_gets_no_shape_config():
    """Sending one for a fixed shape is an error, not a no-op."""
    shape, ocpus, memory = oci.parse_shape("VM.Standard2.2")
    assert shape == "VM.Standard2.2"
    assert ocpus is None and memory is None


def test_a_shape_can_be_sized_from_the_template_without_a_new_column():
    assert oci.parse_shape("VM.Standard.E4.Flex:4") == ("VM.Standard.E4.Flex", 4.0,
                                                        oci._FLEX_DEFAULT_MEMORY_GB)
    assert oci.parse_shape("VM.Standard.E4.Flex:4:32") == ("VM.Standard.E4.Flex", 4.0,
                                                           32.0)


def test_sizing_a_fixed_shape_is_refused_rather_than_ignored():
    """It means the author believed they were sizing something."""
    assert "fixed shape" in _refused("VM.Standard2.2:8")


def test_an_unreadable_shape_is_refused_with_the_format():
    assert "SHAPE:ocpus" in _refused("VM.Standard.E4.Flex:lots")
    assert "no CPU" in _refused("VM.Standard.E4.Flex:0")
    assert "no OCI shape" in _refused("")


def test_the_shape_reads_back_in_the_form_the_template_writes():
    """So an operator comparing the POV page to the template is not left translating a
    shape_config back into a suffix."""
    body = _code("_vm")
    assert "shape_config" in body and "ocpus" in body


# ── user_data ────────────────────────────────────────────────────────────────

def test_user_data_is_base64_because_the_sdk_does_not_encode_it():
    assert oci._user_data("") is None, "an empty payload must be omitted, not encoded"
    encoded = oci._user_data("#cloud-config\nruncmd: [echo, hi]\n")
    assert base64.b64decode(encoded).decode() == "#cloud-config\nruncmd: [echo, hi]\n"
    assert "_user_data(" in _code("_create_vms_sync"), \
        "the create path passes user_data without encoding it"


def test_only_the_broker_receives_the_bootstrap():
    """`pov_cloud_env.vm_specs` already put the payload on the broker alone. A driver that
    re-derived that would be a second answer to one question."""
    body = _code("_create_vms_sync")
    assert '"broker"' not in body, (
        "the driver decides for itself which VM is the broker instead of taking the "
        "payload the shared vm_specs already put on the spec")
    assert "user_data" in body


# ── availability domains, and the compartment ────────────────────────────────

def test_the_availability_domain_is_listed_never_assembled():
    """An AD is named `Uocm:PHX-AD-1` — a tenancy-specific prefix and a region code — so
    there is no string to build, and many regions have exactly one."""
    body = _code("_availability_domain_sync")
    assert "list_availability_domains" in body
    assert "-AD-" not in body, "the driver assembles an availability domain name"


def test_the_compartment_is_read_from_the_row_before_current_config():
    """`expiry_reaper` states the rule: a destroy aimed at the wrong project is the worst
    version of this bug. A compartment is exactly that kind of boundary."""
    body = _code("_compartment")
    assert "recorded_project" in body, "the compartment is taken from config, not the row"
    assert body.index("recorded_project") < body.index("configured_project_id"), \
        "current config is consulted before the recorded compartment"


def test_a_missing_compartment_is_refused_with_the_remedy():
    original = oci.configured_project_id
    oci.configured_project_id = lambda: ""
    try:
        oci._compartment()
    except pov_cloud_env.CloudEnvError as exc:
        assert "Settings" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a blank compartment fell through")
    finally:
        oci.configured_project_id = original


def test_a_compartment_is_not_created_per_pov():
    """The obvious analogy to an Azure resource group, and the wrong one: creating a
    compartment needs tenancy-level IAM, deleting one requires it to be empty first, the
    delete is asynchronous and slow, and the name stays reserved afterwards."""
    assert "create_compartment" not in _SRC, (
        "the driver creates a compartment per POV; its delete semantics make that worse "
        "than tagging, not better")
    assert "delete_compartment" not in _SRC


def test_reading_the_compartment_or_region_never_raises_on_a_missing_database():
    """These are read from the lab-platform registry, which the UI and the tests exercise
    constantly and sometimes before a database exists."""
    assert isinstance(oci.default_region(), str) and oci.default_region()
    assert isinstance(oci.configured_project_id(), str)


# ── power and teardown ───────────────────────────────────────────────────────

def test_suspending_asks_the_guest_first():
    """SOFTSTOP asks the guest to shut down and falls back to a hard stop. A POV somebody
    resumes next morning should not have been pulled out at the cord every night."""
    body = _code("_power_sync")
    assert "SOFTSTOP" in body, "the suspend path hard-stops the guests"
    assert "START" in body


def test_the_teardown_deletes_the_subnet_before_what_it_references():
    """OCI refuses to delete a route table or security list a subnet still points at, and
    refuses the VCN while any of them remain."""
    body = _code("_delete_sync")
    # Anchored on the DELETE CALLS, not the step labels: "vcn" also appears in the
    # `vcns = ...` lookup above the loop, which made the first version of this test read
    # the order backwards and fail on correct code.
    calls = ("delete_subnet", "delete_route_table", "delete_security_list",
             "delete_internet_gateway", "delete_vcn")
    for call in calls:
        assert call in body, f"the teardown never calls {call}"
    order = [body.index(c) for c in calls]
    assert order == sorted(order), (
        f"teardown steps are out of dependency order: {list(zip(calls, order))}")


def test_the_teardown_waits_for_the_instances_to_go():
    """An instance holds its VNIC well past the terminate call returning, and OCI will not
    delete a subnet with a VNIC in it."""
    body = _code("_terminate_instances_sync")
    assert "wait_until" in body and "TERMINATED" in body


def test_every_teardown_step_is_scoped_to_the_environment_tag():
    body = _code("_delete_sync")
    assert "_mine(" in body, "a teardown step selects something wider than this POV"
    assert "TAG_ENVIRONMENT" in _code("_mine") or "TAG_ENVIRONMENT" in body


def test_rebuilding_the_broker_is_scoped_to_one_environment():
    """Two POVs can each have a `broker`."""
    body = _code("_terminate_instances_sync")
    assert "_of_env(inst, env_id)" in body, "the delete is not scoped to the environment"
    assert "names" in body, "the delete is not scoped to the VM name"


def test_a_vcn_with_no_instances_left_is_not_reported_as_gone():
    """That state is a teardown which removed the machines and then failed."""
    body = _code("_read_environment_sync")
    assert "list_vcns" in body, \
        "the read answers None on an empty environment, so the reconcile would mark a POV "\
        "missing while its network still bills"


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
