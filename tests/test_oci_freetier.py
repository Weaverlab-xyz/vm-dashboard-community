"""Unit tests: services/oci_freetier — the Always-Free warn-and-confirm envelope.

``instance_count`` had been a parameter with no caller since the module was written.
Count-based deploys make it load-bearing: three Always-Free micros is one more than
the tier allows even though each one on its own is free, and if the multiplication is
wrong the gate stays silent and the user gets a bill.

The interesting cases are all the ones where a per-instance-legal selection becomes
illegal in aggregate, which is precisely what nothing exercised before.

Loaded by file path — the module is deliberately import-free so it tests without the
oci SDK. Runs under pytest, or standalone:  python tests/test_oci_freetier.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "oci_freetier.py")

_spec = importlib.util.spec_from_file_location("oci_freetier", _PATH)
ft = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ft)


def _amd(**kw):
    kw.setdefault("shape", ft.FREE_AMD_SHAPE)
    kw.setdefault("boot_volume_gb", ft.FREE_BOOT_VOLUME_GB)
    return ft.evaluate(**kw)


def _a1(**kw):
    kw.setdefault("shape", ft.FREE_A1_SHAPE)
    kw.setdefault("boot_volume_gb", ft.FREE_BOOT_VOLUME_GB)
    return ft.evaluate(**kw)


# ── the count multiplier ─────────────────────────────────────────────────────

def test_one_free_micro_is_within_the_envelope():
    within, warnings = _amd(instance_count=1)
    assert within is True, warnings


def test_the_amd_instance_cap_counts_the_batch():
    """Each micro is free; FREE_AMD_MAX_INSTANCES+1 of them is not."""
    within, warnings = _amd(instance_count=ft.FREE_AMD_MAX_INSTANCES)
    assert within is True, warnings

    within, warnings = _amd(instance_count=ft.FREE_AMD_MAX_INSTANCES + 1)
    assert within is False
    assert any("instances to" in w for w in warnings), warnings


def test_the_amd_cap_folds_in_what_is_already_deployed():
    """A batch of 2 is fine from zero and over the line with 1 already running."""
    assert _amd(instance_count=2, existing_amd_count=0)[0] is True
    assert _amd(instance_count=2, existing_amd_count=1)[0] is False


def test_a1_ocpus_and_memory_multiply_by_the_count():
    # 1 OCPU / 6 GB each: two fit the sustained budget exactly, three do not.
    assert _a1(ocpus=1, memory_gb=6, instance_count=2)[0] is True

    within, warnings = _a1(ocpus=1, memory_gb=6, instance_count=3)
    assert within is False
    assert any("OCPUs would total 3" in w for w in warnings), warnings
    assert any("memory would total 18" in w for w in warnings), warnings


def test_boot_storage_is_pooled_across_the_batch():
    """50 GB is the free default per VM, but the 200 GB pool is account-wide, so a
    fifth default-sized VM exceeds it while each one alone looks free.

    Uses A1 with a deliberately tiny per-VM footprint so the only budget in play is
    block storage — the AMD shape caps at 2 instances and would trip that rule first."""
    per_vm = ft.FREE_BOOT_VOLUME_GB
    fits = ft.FREE_BLOCK_STORAGE_GB // per_vm
    tiny = dict(ocpus=0.25, memory_gb=1, boot_volume_gb=per_vm)

    assert _a1(instance_count=fits, **tiny)[0] is True

    within, warnings = _a1(instance_count=fits + 1, **tiny)
    assert within is False
    assert any("free block-storage allotment" in w for w in warnings), warnings


def test_count_defaults_to_one_and_tolerates_junk():
    """The default is what every pre-count caller relied on; None/0 must not divide the
    envelope by zero or wrap to a huge batch."""
    assert _amd()[0] is True
    assert _amd(instance_count=None)[0] is True
    assert _amd(instance_count=0)[0] is True


def test_a_non_free_shape_still_warns_regardless_of_count():
    within, warnings = ft.evaluate(shape="VM.Standard3.Flex", instance_count=1)
    assert within is False
    assert any("not an Always-Free shape" in w for w in warnings), warnings


def test_within_is_exactly_the_absence_of_warnings():
    """The endpoint gates on `not within`, and the form renders `warnings` — if those
    two ever disagree the user gets a block with nothing to read, or advice with no
    block."""
    for count in range(1, 6):
        within, warnings = _amd(instance_count=count)
        assert within == (not warnings), (count, within, warnings)


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
