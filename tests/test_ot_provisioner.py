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


def _embedded_python(path):
    """(filename, source) for every .py the script writes via a quoted heredoc."""
    return re.findall(r"cat > \S*?/([\w.]+\.py) <<'EOF'\n(.*?)\nEOF\n",
                      _read(path), re.S)


def test_the_embedded_python_compiles():
    # These sources are written by a shell heredoc, so nothing else type-checks or
    # even parses them: a stray unescaped character ships an image whose container
    # dies at boot, inside an air-gapped subnet, with the bake long finished.
    for path in _SCRIPTS:
        for name, src in _embedded_python(path):
            try:
                compile(src, name, "exec")
            except SyntaxError as exc:
                raise AssertionError(
                    f"{os.path.basename(path)}: embedded {name} does not compile: {exc}")


def test_every_baked_sim_is_smoke_tested():
    # The bake's contract is "a container that will not start fails the BAKE".
    # A sim added to the compose file but not to the smoke list breaks it quietly.
    for path in _SCRIPTS:
        src = _read(path)
        if "OT_SMOKE_CONTAINERS" not in src:
            continue
        containers = set(re.findall(r"container_name:\s*(\S+)", src))
        smoked = set()
        for assignment in re.findall(r'OT_SMOKE_CONTAINERS="([^"]*)"', src):
            smoked |= {w for w in assignment.split() if not w.startswith("$")}
        missing = containers - smoked
        assert not missing, (
            f"{os.path.basename(path)}: {sorted(missing)} are in the compose stack but "
            f"never smoke-tested — a dead one would only surface in an air-gapped subnet")


def test_cpppo_is_not_pinned_to_the_broken_4x_series():
    # cpppo < 5 rewrites code objects at import and raises
    # "code() argument 13 must be str, not int" on Python 3.11+. The sim image is
    # python:3.12-slim, so a 4.x pin fails every bake at the smoke test.
    for path in _SCRIPTS:
        src = _read(path)
        for pin in re.findall(r"OT_CPPPO_VERSION:-(\d+)\.", src):
            assert int(pin) >= 5, (
                f"{os.path.basename(path)}: cpppo {pin}.x cannot run on Python 3.11+ "
                f"— pin 5.x or newer")


def test_the_new_pins_are_explicit_too():
    for path in _SCRIPTS:
        src = _read(path)
        for name, var in (("asyncua", "OT_ASYNCUA_VERSION"),
                          ("cpppo", "OT_CPPPO_VERSION")):
            if name not in src:
                continue
            assert (re.search(rf"{name}==\d", src)
                    or re.search(rf"{var}:-\d", src)), (
                f"{os.path.basename(path)}: {name} must be ==-pinned (literally or via "
                f"a digit-defaulted {var})")


def test_the_fuxa_seed_never_fails_the_bake():
    # The seed is a convenience: FUXA's project format is version-coupled, so a
    # future image that rejects it must leave the operator with today's behaviour
    # (wire it by hand), not a failed 15-minute bake.
    for path in _SCRIPTS:
        src = _read(path)
        if "fuxa_seed.py" not in src:
            continue
        m = re.search(r"if docker run[^\n]*fuxa_seed[^\n]*\n(.*?)\nfi\n", src, re.S)
        if m is None:
            m = re.search(r"(if docker run.*?\nfi\n)", src, re.S)
        assert m, f"{os.path.basename(path)}: the FUXA seed is not in an if/else guard"
        assert "die " not in m.group(0), (
            f"{os.path.basename(path)}: the FUXA seed calls die — a convenience must "
            f"not fail the bake")
        assert "WARNING" in m.group(0), (
            f"{os.path.basename(path)}: a skipped FUXA seed must say so loudly, or the "
            f"operator opens an empty HMI mid-demo with no idea why")


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
