"""Hygiene rules for the OT provisioner scripts (provisioners/ot/*.sh).

Auto-discovering like tests/test_compose_samples.py: any script added to
provisioners/ot/ is covered without editing this file. The rules encode why the
baked-image substrate was chosen at all:

* pinned versions, never ``:latest`` — a floating tag makes two bakes of the
  "same" image behave differently, which in an air-gapped demo cell is
  undebuggable;
* ``set -eu`` + POSIX shebang — the bake must fail loudly, and Azure's builder
  forces /bin/sh regardless of shebang;
* the pulls-happen-at-bake-time contract (mirror + smoke test) stays present.

Run: python tests/test_ot_provisioner.py   (or under pytest)
"""
import glob
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = sorted(glob.glob(os.path.join(_ROOT, "provisioners", "ot", "*.sh")))


def _read(path):
    return open(path, encoding="utf-8").read()


def test_there_is_at_least_one_ot_provisioner():
    assert _SCRIPTS, "provisioners/ot/ holds no .sh scripts"


def test_posix_shebang_and_fail_fast():
    for path in _SCRIPTS:
        src = _read(path)
        assert src.startswith("#!/bin/sh"), f"{os.path.basename(path)}: shebang must be #!/bin/sh"
        assert "set -eu" in src, f"{os.path.basename(path)}: missing set -eu"


def test_no_floating_latest_tags():
    """No image may be USED at :latest. Lines that exist to reject :latest (the
    scripts' own guards, `die`/comment lines) are not references."""
    for path in _SCRIPTS:
        offenders = [
            ln.strip() for ln in _read(path).splitlines()
            if ":latest" in ln
            and not ln.lstrip().startswith("#")
            and "die " not in ln and "die\t" not in ln
        ]
        assert not offenders, (
            f"{os.path.basename(path)} references a :latest image — pin a version; "
            f"a floating tag makes bakes unreproducible: {offenders}")


def test_the_pins_are_explicit():
    for path in _SCRIPTS:
        src = _read(path)
        if "pymodbus" in src:
            # Either a literal ==-pin, or an env override whose DEFAULT is a pin.
            assert (re.search(r"pymodbus==\d", src)
                    or re.search(r"OT_PYMODBUS_VERSION:-\d", src)), (
                f"{os.path.basename(path)}: pymodbus must be ==-pinned "
                "(literally or via a digit-defaulted OT_PYMODBUS_VERSION)")
        assert re.search(r"frangoteam/fuxa:\d", src) or "fuxa" not in src.lower(), (
            f"{os.path.basename(path)}: the FUXA default must be a version tag")


def test_the_zero_runtime_egress_contract_holds():
    """The whole point of the baked image: pulls and the smoke test happen at bake
    time, so the deployed VM boots the stack with no outbound internet."""
    for path in _SCRIPTS:
        src = _read(path)
        assert re.search(r'"registry-mirrors"\s*:\s*\[\s*"https://mirror\.gcr\.io"', src), (
            f"{os.path.basename(path)}: registry mirror dropped from the docker "
            "daemon config — bakes become hostage to Docker Hub anonymous rate limits")
        assert "compose" in src and "up -d" in src, (
            f"{os.path.basename(path)}: the bake-time smoke test is gone — a dead "
            "stack would only be discovered inside an air-gapped subnet")


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
