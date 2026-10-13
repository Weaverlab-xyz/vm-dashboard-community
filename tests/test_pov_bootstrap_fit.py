"""The broker's bootstrap fits the platform's user-data limit, least changed first.

Found live: a POV on Skytap failed at "could not hand the bootstrap" with a 422,
"Contents is too long (maximum is 16384 characters)". The script was already near the
limit (about half of it is comments for a human on the guest) and SPIRE's block pushed it
over; the policy grows with every VM, so a large enough POV was over without SPIRE too.

Pinned:

  * a script that fits is sent unchanged;
  * compacting drops comments and keeps every heredoc body byte for byte (they are files:
    the policy, SPIRE's configs, the trust bundle), the shebang and both markers;
  * the compressed form decodes back to exactly the compact script, keeps both markers
    once each for the guest runner, is ASCII, and really runs: output, and exit status
    so the runner retries a failure;
  * a realistic worst case fits Skytap's limit;
  * nothing fitting raises BrokerError naming both sizes;
  * ensure_broker fits the payload before the platform sees it, at the platform's limit.

Run: python tests/test_pov_bootstrap_fit.py   (or under pytest)
"""
import base64
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-fit-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-bootstrap-fit")

try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.services import lab_platforms, pov_broker  # noqa: E402

_PEM = ("-----BEGIN CERTIFICATE-----\n" + ("QUJD" * 16 + "\n") * 11
        + "-----END CERTIFICATE-----")
_SPIRE = {"server_address": "pov-agents.example.test", "server_port": 8091,
          "trust_domain": "pov.example.test", "join_token": "00000000-0000-0000-0000-000000000001",
          "bootstrap_pem": _PEM}


def _script(vms=8, spire=True):
    targets = [f"10.0.{i // 250}.{i % 250 + 1}" for i in range(vms)]
    return pov_broker.render_bootstrap(
        env_name="poc-fit", dashboard_url="https://pov-agents.example.test",
        enroll_code="agte_" + "x" * 40,
        policy_yaml=pov_broker.render_policy(targets, targets[: vms // 3], targets[vms // 3:]),
        images=(pov_broker.GATEWAY_IMAGE, pov_broker.ANSIBLE_VM_IMAGE),
        spire=_SPIRE if spire else None)


def _heredocs(script):
    """{terminator: body} for every heredoc in a script."""
    out, lines, i = {}, script.splitlines(), 0
    while i < len(lines):
        m = re.search(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?\s*$", lines[i])
        i += 1
        if not m:
            continue
        body = []
        while i < len(lines) and lines[i] != m.group(1):
            body.append(lines[i])
            i += 1
        out.setdefault(m.group(1), []).append("\n".join(body))
        i += 1
    return out


def _sh(script, *args):
    sh = shutil.which("sh")
    if not sh or os.name != "posix":
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="ascii",
                                     newline="\n") as fh:
        fh.write(script)
    try:
        return subprocess.run([sh, *args, fh.name], capture_output=True, text=True)
    finally:
        os.unlink(fh.name)


def test_a_script_that_fits_is_sent_unchanged():
    script = _script(vms=2, spire=False)
    payload, how = pov_broker.fit_bootstrap(script, 10 ** 6)
    assert how == "plain" and payload == script


def test_compacting_keeps_every_heredoc_and_both_markers():
    script = _script(vms=30)
    compact = pov_broker._compact(script)
    assert len(compact) < len(script) * 0.8, "comments are most of what goes"
    assert _heredocs(compact) == _heredocs(script), "a heredoc body changed"
    assert compact.startswith("#!/bin/sh\n")
    for marker in (pov_broker.BOOTSTRAP_BEGIN, pov_broker.BOOTSTRAP_END):
        assert compact.count(marker) == 1, marker
    assert not [ln for ln in compact.splitlines()
                if ln.strip().startswith("#") and not ln.startswith("#!")
                and ln not in (pov_broker.BOOTSTRAP_BEGIN, pov_broker.BOOTSTRAP_END)
                and not any(ln in b for bs in _heredocs(compact).values() for b in bs)]
    p = _sh(compact, "-n")
    if p is not None:
        assert p.returncode == 0, p.stderr[:300]


def test_the_compressed_form_decodes_to_the_compact_script():
    compact = pov_broker._compact(_script(vms=60))
    packed = pov_broker._compressed(compact)
    body = packed.split(f"<<'{pov_broker._GZ_EOF}'", 1)[1].split("\n", 1)[1]
    body = body.split(f"{pov_broker._GZ_EOF}\n", 1)[0]
    assert gzip.decompress(base64.b64decode(body)).decode("utf-8") == compact
    for marker in (pov_broker.BOOTSTRAP_BEGIN, pov_broker.BOOTSTRAP_END):
        assert packed.count(marker) == 1, marker
    assert packed.startswith("#!/bin/sh\n") and packed.rstrip().endswith(pov_broker.BOOTSTRAP_END)
    assert not [c for c in packed if ord(c) > 127]


def test_the_compressed_form_runs_and_passes_the_exit_status_on():
    ok = _sh(pov_broker._compressed("#!/bin/sh\necho inner-ran\nread x || true\necho after\n"))
    if ok is None:
        return  # no POSIX sh here; CI runs it
    assert ok.returncode == 0 and "inner-ran" in ok.stdout and "after" in ok.stdout, ok
    bad = _sh(pov_broker._compressed("#!/bin/sh\nexit 3\n"))
    assert bad.returncode == 3, "a failing bootstrap must fail, so the runner retries it"


def test_a_large_spire_pov_fits_skytap():
    limit = lab_platforms.capabilities("skytap")["bootstrap_max_chars"]
    assert limit == 16384
    for vms in (2, 8, 30, 60, 120):
        payload, how = pov_broker.fit_bootstrap(_script(vms=vms), limit)
        assert len(payload) <= limit, (vms, how, len(payload))


def test_nothing_fitting_names_both_sizes():
    try:
        pov_broker.fit_bootstrap(_script(vms=8), 500)
        raise AssertionError("an impossible limit was accepted")
    except pov_broker.BrokerError as exc:
        assert "500" in str(exc) and "characters" in str(exc)


def test_ensure_broker_fits_before_the_platform_sees_it():
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_broker.py"),
               encoding="utf-8").read()
    body = src.split("async def ensure_broker(", 1)[1]
    assert body.index("fit_bootstrap(payload") < body.index("inject_bootstrap(")
    assert body.index("fit_bootstrap(payload") < body.index("create_broker_vm(")
    assert '"bootstrap_max_chars"' in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fns.sort(key=lambda f: f.__code__.co_firstlineno)
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
