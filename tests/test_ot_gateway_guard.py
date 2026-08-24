"""The OT cell's gateway sizing guard (services/ot_service).

A PRA Web Jump renders headless Chromium ON the gateway host; below ~2 GB the
renderer is OOM-killed and the session error is indistinguishable from a blocked
firewall — and the GCP gateway's DEFAULT machine type is e2-micro (1 GB), so
without this guard the failure mode is the out-of-the-box experience. These tests
pin the guard's memory model and, critically, that the refusal message carries
the remedy: the failed-job page renders error_message and NOTHING else.

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
    assert ot.gateway_mem_mb("e2-micro") == 1024      # the config DEFAULT — too small
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
    for needle in ("gcp_jumpoint_machine_type", "e2-small", "e2-medium",
                   "clouddb-shared-jumpoint", "firewall", "No VM was launched"):
        assert needle in msg, f"remedy message lost {needle!r}: {msg}"


def test_adequate_and_unknown_types_pass():
    ot = _load()
    assert ot.gateway_size_remedy("e2-medium", "x", "config") == ""
    assert ot.gateway_size_remedy("e2-small", "x", "config") == ""
    # Unknown types must not block a deploy — the map can't know every family.
    assert ot.gateway_size_remedy("c4a-standard-1-exotic", "x", "config") == ""


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
