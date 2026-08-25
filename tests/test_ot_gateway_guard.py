"""The OT cell's gateway sizing guard (services/ot_service).

A PRA Web Jump renders headless Chromium ON the gateway host; below ~2 GB the
renderer is OOM-killed and the session error is indistinguishable from a blocked
firewall. config.py's default is e2-medium (4 GB), but the cloud-sandbox setup
scripts deliberately seed gcp_jumpoint_machine_type=e2-micro (1 GB) to keep
standing cost down — so on a sandbox install the failure mode is the
out-of-the-box experience, and this guard is what turns it into a refusal with a
remedy. These tests pin the guard's memory model and, critically, that the
refusal message carries the remedy: the failed-job page renders error_message
and NOTHING else.

Run: python tests/test_ot_gateway_guard.py   (or under pytest)
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")


def _load():
    spec = importlib.util.spec_from_file_location("ot_service_guard_under_test", _SVC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_known_types_and_the_2gb_threshold():
    ot = _load()
    assert ot.MIN_WEB_JUMP_GATEWAY_MB == 2048
    assert ot.gateway_mem_mb("e2-micro") == 1024      # the sandbox-seeded size — too small
    assert ot.gateway_mem_mb("e2-small") == 2048      # documented minimum
    assert ot.gateway_mem_mb("e2-medium") == 4096     # documented preference
    assert ot.gateway_mem_mb("E2-Micro ") == 1024     # case/space tolerant


def test_custom_and_family_types_parse_conservatively():
    ot = _load()
    assert ot.gateway_mem_mb("e2-custom-2-4096") == 4096
    assert ot.gateway_mem_mb("n2-custom-4-8192") == 8192
    assert ot.gateway_mem_mb("e2-standard-2") == 7680     # 3840/vCPU floor (n1)
    assert ot.gateway_mem_mb("n1-highcpu-2") == 1800      # correctly below 2 GB
    assert ot.gateway_mem_mb("weird-shape-9000") is None  # unknown → not blocked


def test_the_refusal_message_carries_the_remedy():
    ot = _load()
    msg = ot.gateway_size_remedy("e2-micro", "clouddb-shared-jumpoint", "the live gateway VM")
    # "Settings → Integrations → Privileged Remote Access" is load-bearing: the key had
    # no Settings control at all when this message first shipped, so operators went
    # looking for a field that did not exist. The panel input and this pointer landed
    # together; losing either recreates that dead end.
    for needle in ("gcp_jumpoint_machine_type", "e2-small", "e2-medium",
                   "clouddb-shared-jumpoint", "firewall", "No VM was launched",
                   "Settings → Integrations → Privileged Remote Access"):
        assert needle in msg, f"remedy message lost {needle!r}: {msg}"


def test_adequate_and_unknown_types_pass():
    ot = _load()
    assert ot.gateway_size_remedy("e2-medium", "x", "config") == ""
    assert ot.gateway_size_remedy("e2-small", "x", "config") == ""
    # Unknown types must not block a deploy — the map can't know every family.
    assert ot.gateway_size_remedy("c4a-standard-1-exotic", "x", "config") == ""


def test_the_guard_steps_aside_only_for_a_real_gateway_override():
    """The guard reasons about the DASHBOARD-MANAGED shared gateway. A cell wired
    through an operator-picked Gateway must not be refused on OUR config default
    (the sandbox seeds e2-micro!), but picking the default by name is no override."""
    ot = _load()
    ot._cfg = lambda key: {"gcp_jumpoint_name": "gcp-shared-gw"}.get(key, "")
    assert not ot.jumpoint_overridden({})
    assert not ot.jumpoint_overridden({"jumpoint_name": "   "})
    assert not ot.jumpoint_overridden({"jumpoint_name": "gcp-shared-gw"})
    assert ot.jumpoint_overridden({"jumpoint_name": "centralus-gw"})


def test_the_override_check_resolves_per_cloud_defaults():
    """Each cloud's cell compares against ITS OWN default chain (aws: bt_* only;
    azure: azure_jumpoint_name → bt_*) — comparing every cloud against the GCP
    default would misread 'the AWS default, picked by name' as an override and
    skip the guard exactly where it applies."""
    ot = _load()
    ot._cfg = lambda key: {"gcp_jumpoint_name": "gcp-gw",
                           "azure_jumpoint_name": "az-gw",
                           "bt_jumpoint_name": "aws-gw"}.get(key, "")
    assert not ot.jumpoint_overridden({"jumpoint_name": "aws-gw"}, "aws")
    assert ot.jumpoint_overridden({"jumpoint_name": "gcp-gw"}, "aws")
    assert not ot.jumpoint_overridden({"jumpoint_name": "az-gw"}, "azure")
    assert ot.jumpoint_overridden({"jumpoint_name": "aws-gw"}, "azure")
    # Azure falls back to bt_* when its own key is blank.
    ot._cfg = lambda key: {"bt_jumpoint_name": "shared-gw"}.get(key, "")
    assert not ot.jumpoint_overridden({"jumpoint_name": "shared-gw"}, "azure")


# ── AWS / Azure memory models (the cell went multi-cloud) ─────────────────────

def test_aws_types_and_the_2gb_threshold():
    ot = _load()
    assert ot.aws_gateway_mem_mb("t3.micro") == 1024     # too small
    assert ot.aws_gateway_mem_mb("t3.small") == 2048     # the creation default — minimum
    assert ot.aws_gateway_mem_mb("t3.medium") == 4096    # documented preference
    assert ot.aws_gateway_mem_mb(" T3.Small ") == 2048   # case/space tolerant
    # Size-suffix floor for unmapped families — conservative, like the GCE per-vCPU
    # floors: c-family .medium/.large are the smallest at 4 GB, every *.xlarge ≥8.
    assert ot.aws_gateway_mem_mb("m7i.large") == 4096
    assert ot.aws_gateway_mem_mb("c6i.2xlarge") == 8192
    assert ot.aws_gateway_mem_mb("weird-shape") is None  # unknown → not blocked


def test_azure_sizes_and_the_2gb_threshold():
    ot = _load()
    assert ot.azure_gateway_mem_mb("Standard_B1s") == 1024    # too small
    assert ot.azure_gateway_mem_mb("Standard_B1ms") == 2048   # documented minimum
    assert ot.azure_gateway_mem_mb("Standard_B2s") == 4096    # the creation default
    assert ot.azure_gateway_mem_mb(" standard_b2s ") == 4096  # case/space tolerant
    assert ot.azure_gateway_mem_mb("Standard_X99_v9") is None  # unknown → not blocked


def test_the_refusal_message_carries_the_remedy_per_cloud():
    """The failed-job page renders error_message and NOTHING else, so every cloud's
    refusal has to name ITS OWN size key, sizes and Settings location."""
    ot = _load()
    aws = ot.gateway_size_remedy("t3.micro", "dashboard-sandbox-jumpoint-host",
                                 "the live gateway host", cloud="aws")
    for needle in ("bt_ecs_host_instance_type", "t3.small", "t3.medium",
                   "dashboard-sandbox-jumpoint-host", "firewall", "No VM was launched",
                   "Settings → Integrations → Privileged Remote Access"):
        assert needle in aws, f"AWS remedy message lost {needle!r}: {aws}"
    az = ot.gateway_size_remedy("Standard_B1s", "clouddb-jumpoint",
                                "the live gateway VM", cloud="azure")
    for needle in ("azure_jumpoint_vm_size", "Standard_B1ms", "Standard_B2s",
                   "clouddb-jumpoint", "firewall", "No VM was launched",
                   "Settings → Integrations → Privileged Remote Access"):
        assert needle in az, f"Azure remedy message lost {needle!r}: {az}"


def test_adequate_aws_azure_sizes_pass():
    ot = _load()
    assert ot.gateway_size_remedy("t3.small", "x", "config", cloud="aws") == ""
    assert ot.gateway_size_remedy("t3.medium", "x", "config", cloud="aws") == ""
    assert ot.gateway_size_remedy("Standard_B1ms", "x", "config", cloud="azure") == ""
    assert ot.gateway_size_remedy("Standard_B2s", "x", "config", cloud="azure") == ""
    # Unknown types must not block a deploy on any cloud.
    assert ot.gateway_size_remedy("u-9tb1.metal", "x", "config", cloud="aws") == ""
    assert ot.gateway_size_remedy("Standard_NV6", "x", "config", cloud="azure") == ""


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
