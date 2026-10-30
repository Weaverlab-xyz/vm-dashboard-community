"""Agent 2.10 lifecycle control: ``agent_upgrade`` / ``agent_restart`` on the agent side.

The dashboard may only choose WHICH signed release runs; the host decides whether any may.
What each test pins:

  * **The opt-in is the host's.** ``upgrades:`` in policy.yaml, validated strictly: one
    verify mode, a plain repository, a ready timeout under the stale-job limit.
  * **The agent checks the request** before handing it on: the policy's repository only,
    by digest, no downgrade unless allowed.
  * **The handoff.** No updater → refused; a refusal from the updater → the job fails; a
    ``docker stop`` → handed off, not completed.
  * **The successor finishes the job**, and a rolled-back predecessor fails it.
  * **SIGTERM drains a serial agent too**, so a swap never kills a running job.

Runs under pytest, or standalone:  python tests/test_agent_control_jobs.py
"""
import importlib.util
import json
import os
import signal
import sys
import tempfile
import textwrap
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")
_spec = importlib.util.spec_from_file_location("agent_ctl", _AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
sys.modules["agent_ctl"] = agent
_spec.loader.exec_module(agent)

DIGEST = "sha256:" + "a" * 64
KEYLESS = {"keyless": {"identity": "^https://github.com/o/r/.github/workflows/p.yml@refs/tags/v",
                       "issuer": "https://token.actions.githubusercontent.com"}}


def _policy(extra: str = ""):
    doc = "targets:\n  - cidr: 10.0.0.0/8\njob_types: [agent_discover]\n" + textwrap.dedent(extra)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(doc)
    try:
        return agent.Policy.load(fh.name)
    finally:
        os.unlink(fh.name)


UPGRADES = """
upgrades:
  enabled: true
  repository: chrweav/dashboard-agent
  verify:
    keyless:
      identity: "^https://github.com/o/r/.github/workflows/p.yml@refs/tags/v"
      issuer: "https://token.actions.githubusercontent.com"
"""


def _control_dir():
    agent.CONTROL_DIR = tempfile.mkdtemp(prefix="ctl-")
    return agent.CONTROL_DIR


class _Dash:
    def __init__(self):
        self.completed = []
        self.beats = []

    def complete(self, job_id, *, status, result=None, error=""):
        self.completed.append((job_id, status, result or {}, error))

    def heartbeat(self, job_id, pct, message):
        self.beats.append((job_id, message))
        return False

    def logs(self, job_id, lines):
        return None

    def redact(self, text):
        return text

    def release_secrets(self, job_id=None):
        return None


# ── the policy block ──────────────────────────────────────────────────────────

def test_upgrades_are_off_unless_the_host_enables_them():
    assert _policy().upgrades == {}
    assert _policy("upgrades:\n  enabled: false\n").upgrades == {}
    up = _policy(UPGRADES).upgrades
    assert up["repository"] == "chrweav/dashboard-agent" and up["verify"] == KEYLESS
    assert up["allow_downgrade"] is False and up["ready_timeout_s"] == 480


def test_a_public_key_or_a_private_registry_is_accepted():
    up = agent.parse_upgrades({"enabled": True, "repository": "registry.local:5000/agent",
                               "verify": {"public_key": "/etc/dashboard-agent/cosign.pub"}})
    assert up["verify"] == {"public_key": "/etc/dashboard-agent/cosign.pub"}


def test_a_malformed_block_is_fatal_not_ignored():
    bad = [
        {"enabled": True, "repository": "chrweav/dashboard-agent:latest", "verify": KEYLESS},
        {"enabled": True, "repository": "chrweav/dashboard-agent",
         "verify": {**KEYLESS, "public_key": "/k.pub"}},
        {"enabled": True, "repository": "chrweav/dashboard-agent", "verify": {}},
        {"enabled": True, "repository": "chrweav/dashboard-agent",
         "verify": {"keyless": {"identity": "x"}}},
        {"enabled": True, "repository": "chrweav/dashboard-agent",
         "verify": {"public_key": "relative.pub"}},
        {"enabled": True, "repository": "chrweav/dashboard-agent", "verify": KEYLESS,
         "ready_timeout_s": 900},
        "yes",
    ]
    for value in bad:
        try:
            agent.parse_upgrades(value)
        except agent.AgentFatal:
            continue
        raise AssertionError(f"{value!r} was accepted")


# ── the agent's own check ─────────────────────────────────────────────────────

def test_the_agent_accepts_only_its_repository_by_digest_and_no_downgrade():
    up = agent.parse_upgrades({"enabled": True, "repository": "chrweav/dashboard-agent",
                               "verify": KEYLESS})
    ok = agent.check_control_request("agent_upgrade", {"repository": "chrweav/dashboard-agent",
                                                       "digest": DIGEST, "version": "2.15.0"}, up)
    assert ok == {"op": "upgrade", "repository": "chrweav/dashboard-agent", "digest": DIGEST,
                  "version": "2.15.0"}
    for payload in ({"repository": "evil/agent", "digest": DIGEST, "version": "2.15.0"},
                    {"repository": "chrweav/dashboard-agent", "digest": "latest",
                     "version": "2.15.0"},
                    {"repository": "chrweav/dashboard-agent", "digest": DIGEST,
                     "version": "2.9.0"},
                    {"repository": "chrweav/dashboard-agent", "digest": DIGEST,
                     "version": "next"}):
        try:
            agent.check_control_request("agent_upgrade", payload, up)
        except agent.PolicyRefusal:
            continue
        raise AssertionError(f"{payload} was accepted")
    up["allow_downgrade"] = True
    agent.check_control_request("agent_upgrade", {"repository": "chrweav/dashboard-agent",
                                                  "digest": DIGEST, "version": "2.9.0"}, up)
    assert agent.check_control_request("agent_restart", {}, up) == {
        "op": "restart", "version": agent.AGENT_VERSION}


def test_a_control_job_is_refused_without_the_hosts_opt_in():
    dash = _Dash()
    agent.execute(dash, _policy(), {"job_id": "j1", "job_type": "agent_restart", "payload": {}})
    assert dash.completed[0][1] == "failed" and "upgrades" in dash.completed[0][3]


# ── the handoff ───────────────────────────────────────────────────────────────

def _alive():
    with open(os.path.join(agent.CONTROL_DIR, "updater.alive"), "w", encoding="utf-8") as fh:
        fh.write("1")


def test_no_updater_means_no_upgrade():
    _control_dir()
    pol = _policy(UPGRADES)
    try:
        agent.run_control("agent_restart", {}, pol, lambda m: None, lambda: False, "j1", _Dash())
    except agent.RunFailure as exc:
        assert "No updater" in str(exc)
    else:
        raise AssertionError("ran with no updater")
    assert not os.path.exists(os.path.join(agent.CONTROL_DIR, "request.json"))


def test_a_refusal_from_the_updater_fails_the_job():
    _control_dir()
    _alive()
    pol = _policy(UPGRADES)
    agent.SLOTS = agent.JobSlots(1)

    def updater():
        for _ in range(100):
            req = agent._read_control("request.json")
            if req:
                agent._write_control("status.json", {"job_id": req["job_id"],
                                                     "state": "failed",
                                                     "reason": "the image signature did not verify"})
                return
            time.sleep(0.05)
    threading.Thread(target=updater, daemon=True).start()
    saved = agent.time.sleep
    agent.time.sleep = lambda s: saved(0.05)
    try:
        agent.run_control("agent_upgrade", {"repository": "chrweav/dashboard-agent",
                                            "digest": DIGEST, "version": "2.15.0"},
                          pol, lambda m: None, lambda: False, "j2", _Dash())
    except agent.RunFailure as exc:
        assert "signature did not verify" in str(exc)
    else:
        raise AssertionError("an updater refusal did not fail the job")
    finally:
        agent.time.sleep = saved
    assert not agent.SLOTS.hold, "a refused handoff left the agent holding off new work"
    agent.SLOTS = None


def test_a_docker_stop_hands_the_job_off_without_completing_it():
    _control_dir()
    _alive()
    pol = _policy(UPGRADES)
    agent.SLOTS = agent.JobSlots(1)
    saved = agent.time.sleep

    def sleep(seconds):
        agent.SLOTS.draining = True        # what the SIGTERM handler does
        saved(0.01)
    agent.time.sleep = sleep
    dash = _Dash()
    try:
        agent.execute(dash, pol, {"job_id": "j3", "job_type": "agent_upgrade",
                                  "payload": {"repository": "chrweav/dashboard-agent",
                                              "digest": DIGEST, "version": "2.15.0"}})
    finally:
        agent.time.sleep = saved
        agent.SLOTS = None
    assert dash.completed == [], "a handed-off job was completed by the agent being replaced"
    req = agent._read_control("request.json")
    assert req["op"] == "upgrade" and req["job_id"] == "j3" and req["digest"] == DIGEST


# ── the successor ─────────────────────────────────────────────────────────────

def _pending(op="upgrade", version="2.10.0", job_id="j4", **extra):
    agent._write_control("request.json", {"op": op, "job_id": job_id, "version": version,
                                          "digest": DIGEST, "requested_at": int(time.time()),
                                          **extra})


def test_the_successor_completes_the_job_and_reports_ready():
    _control_dir()
    _pending(version=agent.AGENT_VERSION)
    agent._write_control("status.json", {"job_id": "j4", "state": "waiting_ready"})
    dash = _Dash()
    assert agent.finish_pending_control(dash) == "j4"
    assert dash.completed == [("j4", "completed", {"version": agent.AGENT_VERSION,
                                                   "outcome": "upgraded"}, "")]
    ready = agent._read_control("ready")
    assert ready["job_id"] == "j4" and ready["version"] == agent.AGENT_VERSION
    assert agent._read_control("request.json") is None
    assert agent.finish_pending_control(dash) is None, "settled twice"


def test_a_rolled_back_predecessor_fails_the_job_with_the_reason():
    _control_dir()
    _pending(version="2.15.0")
    agent._write_control("status.json", {"job_id": "j4", "state": "rolled_back",
                                         "reason": "the new agent exited (code 1)"})
    dash = _Dash()
    agent.finish_pending_control(dash)
    assert dash.completed[0][1] == "failed" and "exited (code 1)" in dash.completed[0][3]
    assert agent._read_control("ready") is None, "a rolled-back agent claimed to be ready"


def test_the_wrong_version_coming_up_fails_the_job():
    _control_dir()
    _pending(version="2.99.0")
    dash = _Dash()
    agent.finish_pending_control(dash)
    assert dash.completed[0][1] == "failed" and "came up as" in dash.completed[0][3]


def test_a_stale_leftover_request_is_discarded():
    _control_dir()
    _pending(requested_at=int(time.time()) - 9000)
    dash = _Dash()
    assert agent.finish_pending_control(dash) is None and dash.completed == []


# ── slots and signals ─────────────────────────────────────────────────────────

def test_quiesce_waits_for_other_jobs_and_holds_new_leases():
    slots = agent.JobSlots(2)
    agent.POLICY = _policy("ansible:\n  enabled: true\n  vm_image: x\n  max_concurrent: 2\n"
                           "  targets:\n    - cidr: 10.0.0.0/8\n      ports: [22]\n")
    release = threading.Event()
    slots.run(lambda: release.wait(5), "agent_ansible")
    assert slots.quiesce(0.2) is False and slots.hold
    release.set()
    slots.drain(5)
    assert slots.quiesce(1) is True


def test_a_concurrent_agent_asks_for_control_jobs_only_when_opted_in():
    ansible = ("ansible:\n  enabled: true\n  vm_image: x\n  max_concurrent: 2\n"
               "  targets:\n    - cidr: 10.0.0.0/8\n      ports: [22]\n")
    slots = agent.JobSlots(2)
    agent.POLICY = _policy(ansible)
    assert not set(agent.CONTROL_JOB_TYPES) & slots.wanted()
    agent.POLICY = _policy(ansible + UPGRADES)
    assert set(agent.CONTROL_JOB_TYPES) <= slots.wanted()
    # The serial slot busy: no control job either, it would have to wait for it.
    release = threading.Event()
    slots.run(lambda: release.wait(5), "agent_discover")
    try:
        assert not set(agent.CONTROL_JOB_TYPES) & slots.wanted()
    finally:
        release.set()
        slots.drain(5)


def test_sigterm_drains_a_serial_agent_too():
    slots = agent.JobSlots(1)
    before = signal.getsignal(signal.SIGTERM)
    try:
        agent._install_drain(slots)
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not before and callable(handler)
        handler(signal.SIGTERM, None)
        assert slots.draining
    finally:
        signal.signal(signal.SIGTERM, before)


def test_the_lease_reports_whether_this_host_can_be_managed():
    _control_dir()
    agent.POLICY = _policy(UPGRADES)
    sent = []

    class _Resp:
        status_code = 200
        content = b'{"job": null}'

        def json(self):
            return {"job": None}

        def raise_for_status(self):
            return None
    dash = agent.Dashboard("https://dash.example.com")
    dash._request = lambda method, path, payload, **kw: (sent.append(payload), _Resp())[1]
    dash.lease()
    assert sent[0]["control"] == {"upgrades_enabled": True, "updater_present": False,
                                  "k8s_signature": "off"}
    _alive()
    dash.lease()
    assert sent[1]["control"]["updater_present"] is True


def test_the_agent_version_is_at_least_2_10():
    # 2.10 introduced the control handlers; later builds keep them.
    assert agent._version_tuple(agent.AGENT_VERSION) >= (2, 10, 0)
    src = open(os.path.join(_ROOT, "runners", "agent", "Dockerfile"), encoding="utf-8").read()
    assert "LABEL dashboard-agent.version=$AGENT_VERSION" in src


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
