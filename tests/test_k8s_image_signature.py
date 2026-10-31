"""The Kubernetes image's cosign signature (agent 2.13), agent and updater.

  * **`k8s.verify` fails closed.** Exactly one of a keyless identity and issuer or an
    absolute public-key path; any other shape is fatal, as `upgrades.verify` is.
  * **The updater verifies, the agent enforces.** The updater checks `k8s.image` against
    `k8s.verify` with cosign and writes `k8s-image.json` under a key naming that image and
    signer; the agent runs no Kubernetes work until it reads `ok: true` under its own key.
  * **A pass is final, a failure is retried** with backoff, and no Docker call is made.

Standalone or under pytest:  python tests/test_k8s_image_signature.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent = _load("agent", "runners", "agent", "agent.py")
upd = _load("updater", "runners", "agent-updater", "updater.py")

IMG = "chrweav/ansible-cloud@sha256:" + "c" * 64
OTHER = "chrweav/ansible-cloud@sha256:" + "d" * 64
KEYLESS = {"keyless": {"identity": "^https://github.com/o/r/.github/workflows/p.yml@refs/tags/v",
                       "issuer": "https://token.actions.githubusercontent.com"}}


def _policy_text(image=IMG, verify=KEYLESS):
    doc = textwrap.dedent(f"""
        targets:
          - cidr: 10.40.0.0/16
            ports: [22]
        job_types: [agent_k8s]
        k8s:
          enabled: true
          image: {image}
          targets:
            - cidr: 0.0.0.0/0
              ports: [443]
        """)
    if verify is not None:
        doc += "  verify: " + json.dumps(verify) + "\n"
    return doc


def _write(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    fh.write(text)
    fh.close()
    return fh.name


def _policy(**kw):
    path = _write(_policy_text(**kw))
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


def _control():
    d = tempfile.mkdtemp(prefix="k8ssig-")
    agent.CONTROL_DIR = upd.CONTROL_DIR = d
    return d


def _refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except (agent.PolicyRefusal, agent.AgentFatal) as exc:
        return str(exc)
    raise AssertionError("accepted")


# ── the policy block ──────────────────────────────────────────────────────────

def test_verify_takes_exactly_one_signer_form():
    pol = _policy()
    assert pol.k8s_verify == KEYLESS and len(pol.k8s_verify_key) == 64
    assert _policy(verify={"public_key": "/etc/k.pub"}).k8s_verify == {"public_key": "/etc/k.pub"}
    for bad in ({}, {"keyless": {"identity": "x"}}, {"public_key": "k.pub"},
                {"keyless": KEYLESS["keyless"], "public_key": "/k"}, "yes"):
        assert "k8s.verify" in _refused(_policy, verify=bad), bad


def test_the_agent_and_updater_agree_on_the_key():
    assert agent.k8s_verify_key(IMG, KEYLESS) == upd.k8s_verify_key(IMG, KEYLESS)
    assert agent.k8s_verify_key(IMG, KEYLESS) != agent.k8s_verify_key(OTHER, KEYLESS)
    assert (agent.k8s_verify_key(IMG, KEYLESS)
            != agent.k8s_verify_key(IMG, {"public_key": "/k.pub"}))


# ── the agent ─────────────────────────────────────────────────────────────────

def test_no_kubernetes_work_until_the_updater_says_verified():
    _control()
    pol = _policy()
    assert agent.k8s_signature_state(pol) == "pending"
    assert "not verified (pending)" in _refused(pol.k8s_image)
    agent._write_control("k8s-image.json", {"key": pol.k8s_verify_key, "ok": False,
                                            "reason": "no matching signatures"})
    assert agent.k8s_signature_state(pol) == "failed: no matching signatures"
    assert "no matching signatures" in _refused(pol.k8s_image)
    agent._write_control("k8s-image.json", {"key": pol.k8s_verify_key, "ok": True})
    assert agent.k8s_signature_state(pol) == "verified"
    assert pol.k8s_image() == IMG


def test_a_result_for_another_image_or_signer_does_not_count():
    _control()
    pol = _policy()
    for key in (agent.k8s_verify_key(OTHER, KEYLESS),
                agent.k8s_verify_key(IMG, {"public_key": "/k.pub"})):
        agent._write_control("k8s-image.json", {"key": key, "ok": True})
        assert agent.k8s_signature_state(pol) == "pending"
        _refused(pol.k8s_image)


def test_without_verify_the_image_is_digest_only_and_says_so():
    _control()
    pol = _policy(verify=None)
    assert agent.k8s_signature_state(pol) == "unchecked"
    assert pol.k8s_image() == IMG


def test_run_k8s_is_refused_before_anything_is_fetched():
    _control()
    asked = []

    class Dash:
        def k8s_bundle(self, *a, **kw):
            asked.append(a)
            return {}
    saved = agent._resolve_all
    agent._resolve_all = lambda host: ["52.1.2.3"]
    try:
        _refused(agent.run_k8s, {"cluster_host": "abc.eks.amazonaws.com", "cluster_port": 443,
                                 "step_kinds": ["apply"]},
                 _policy(), print, lambda: False, "job-1", Dash())
    finally:
        agent._resolve_all = saved
    assert asked == []


# ── the updater ───────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _updater(policy_text, results):
    """An updater reading ``policy_text`` whose cosign answers from ``results`` (True:
    verified, else the stderr line), recording each command."""
    _control()
    upd.POLICY_FILE = _write(policy_text)
    calls = []

    class Proc:
        def __init__(self, ok, err):
            self.returncode, self.stderr, self.stdout = (0 if ok else 1), err, ""

    def run(args, **kw):
        calls.append(args)
        res = results.pop(0)
        return Proc(res is True, "" if res is True else res)
    upd.subprocess.run = run

    class NoDocker:
        def __getattr__(self, name):
            raise AssertionError(f"the k8s check touched Docker ({name})")
    clock = _Clock()
    return upd.Updater(NoDocker(), clock=clock), calls, clock


def test_the_updater_verifies_the_image_against_k8s_verify_once():
    saved = upd.subprocess.run
    try:
        u, calls, _ = _updater(_policy_text(), [True])
        assert u.check_k8s_image() is True
        (args,) = calls
        assert args[:2] == [upd.COSIGN, "verify"] and args[-1] == IMG
        assert args[args.index("--certificate-identity-regexp") + 1] == KEYLESS["keyless"]["identity"]
        doc = upd.read_json("k8s-image.json")
        assert doc["ok"] is True and doc["key"] == agent.k8s_verify_key(IMG, KEYLESS)
        assert u.check_k8s_image() is None and len(calls) == 1, "a pass is final"
    finally:
        upd.subprocess.run = saved


def test_a_failure_is_written_and_retried_with_backoff():
    saved = upd.subprocess.run
    try:
        u, calls, clock = _updater(_policy_text(verify={"public_key": "/etc/k.pub"}),
                                   ["Error: no matching signatures", True])
        assert u.check_k8s_image() is False
        doc = upd.read_json("k8s-image.json")
        assert doc["ok"] is False and "no matching signatures" in doc["reason"]
        assert calls[0][2:4] == ["--key", "/etc/k.pub"]
        assert u.check_k8s_image() is None and len(calls) == 1, "not before the backoff"
        clock.t += upd.K8S_RETRY_FIRST_S + 1
        assert u.check_k8s_image() is True and upd.read_json("k8s-image.json")["ok"] is True
    finally:
        upd.subprocess.run = saved


def test_a_new_image_is_checked_again():
    saved = upd.subprocess.run
    try:
        u, calls, _ = _updater(_policy_text(), [True, True])
        u.check_k8s_image()
        with open(upd.POLICY_FILE, "w", encoding="utf-8") as fh:
            fh.write(_policy_text(image=OTHER))
        assert u.check_k8s_image() is True and calls[-1][-1] == OTHER
    finally:
        upd.subprocess.run = saved


def test_nothing_to_check_without_verify_or_k8s():
    saved = upd.subprocess.run
    try:
        u, calls, _ = _updater(_policy_text(verify=None), [])
        assert u.check_k8s_image() is None and calls == []
        assert upd.read_json("k8s-image.json") is None
    finally:
        upd.subprocess.run = saved



def test_a_bad_policy_is_logged_once_not_every_pass():
    import logging
    saved = upd.subprocess.run
    try:
        u, calls, _ = _updater(_policy_text(image="not-a-digest"), [])
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        upd.log.addHandler(handler)
        try:
            for _ in range(5):
                assert u.check_k8s_image() is None
        finally:
            upd.log.removeHandler(handler)
        assert len(records) == 1 and "not pinned by digest" in records[0].getMessage()
        assert calls == []
    finally:
        upd.subprocess.run = saved

if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
