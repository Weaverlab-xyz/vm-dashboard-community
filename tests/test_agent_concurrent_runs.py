"""One agent running several Config-Management jobs at once (agent 2.9, ``ansible.max_concurrent``).

What has to hold, and what each test pins:

  * **The default is the serial agent.** One slot runs every job inline on the main thread
    and leases with exactly the body an older agent sent, plus the advisory slot count.
  * **Only Config Management is concurrent.** ``agent_ansible`` jobs share the slots; every
    other job type keeps one slot of its own, because discovery, hypervisor pages, the
    Gateway and storage touch shared host state.
  * **The agent asks only for what it can start.** ``want_job_types`` names the types with
    a free slot, so the dashboard never hands it a job it would have to sit on.
  * **A finished job releases only its own credentials.** With one shared scrub set, the
    first run to end would unredact every other run's secrets.
  * **The policy key fails closed.** Not a whole number from 1 to 8 is fatal, never
    clamped.
  * **Windows over OpenSSH** gets the PowerShell shell from one typed bool.

Runs under pytest, or standalone:  python tests/test_agent_concurrent_runs.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")

_spec = importlib.util.spec_from_file_location("agent", _AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
sys.modules["agent"] = agent
_spec.loader.exec_module(agent)

_POLICY = """
    job_types: [agent_discover, agent_hypervisor, agent_ansible]
    targets:
      - cidr: 10.0.0.0/8
    ansible:
      enabled: true
      vm_image: chrweav/ansible-winrm:latest
      targets:
        - cidr: 10.40.0.0/16
          ports: [22]
"""


def _policy(extra: str = ""):
    doc = textwrap.dedent(_POLICY) + textwrap.indent(textwrap.dedent(extra), "  ")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(doc)
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


def _with_policy(pol):
    agent.POLICY = pol
    return pol


# ── the policy key ────────────────────────────────────────────────────────────

def test_the_default_is_one_slot():
    assert _policy().ansible_max_concurrent == 1


def test_a_whole_number_in_range_is_taken():
    assert _policy("max_concurrent: 4\n").ansible_max_concurrent == 4
    assert _policy("max_concurrent: 8\n").ansible_max_concurrent == 8


def test_anything_else_is_fatal_not_clamped():
    for bad in ("0", "9", "50", "'3'", "true", "2.5", "-1"):
        try:
            _policy(f"max_concurrent: {bad}\n")
        except agent.AgentFatal as exc:
            assert "max_concurrent" in str(exc)
        else:
            raise AssertionError(f"max_concurrent: {bad} loaded")


# ── the slots ─────────────────────────────────────────────────────────────────

def test_one_slot_runs_inline_and_asks_for_everything():
    _with_policy(_policy())
    slots = agent.JobSlots(1)
    assert slots.serial
    ran_on = []
    slots.run(lambda: ran_on.append(threading.current_thread()), "agent_ansible")
    assert ran_on == [threading.current_thread()], "a serial agent ran a job off-thread"
    assert slots.wanted() == {"agent_discover", "agent_hypervisor", "agent_ansible"}


def test_config_runs_share_the_slots_and_everything_else_queues_alone():
    _with_policy(_policy("max_concurrent: 2\n"))
    slots = agent.JobSlots(2)
    release = threading.Event()
    running = []

    def _job(name):
        def _fn():
            running.append(name)
            release.wait(5)
        return _fn

    slots.run(_job("a1"), "agent_ansible")
    assert "agent_ansible" in slots.wanted()
    slots.run(_job("a2"), "agent_ansible")
    # Both ansible slots taken: only the serial types are wanted now.
    assert slots.wanted() == {"agent_discover", "agent_hypervisor"}
    slots.run(_job("d1"), "agent_discover")
    assert slots.wanted() == set(), "a slot was offered while all were busy"
    deadline = time.time() + 5
    while len(running) < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert sorted(running) == ["a1", "a2", "d1"], "the three did not run at once"
    release.set()
    slots.drain(5)
    assert slots.draining
    assert all(not t.is_alive() for t in slots._threads), "drain returned with jobs running"


def test_an_idle_agent_always_leases_so_it_stays_online():
    """The lease is also the heartbeat. With no job types granted in policy.yaml, wanted()
    is empty — and an agent that skipped its lease then would read as offline, with
    nothing running to say otherwise. Only a BUSY agent may skip it."""
    _with_policy(_policy("max_concurrent: 2\n"))
    slots = agent.JobSlots(2)
    agent.POLICY.job_types = set()
    try:
        assert slots.wanted() == set()
        assert not slots.busy()
    finally:
        _with_policy(_policy("max_concurrent: 2\n"))
    src = open(_AGENT_PATH, encoding="utf-8").read()
    loop = src[src.index("slots = JobSlots(POLICY.ansible_max_concurrent)"):]
    loop = loop[:loop.index("slots.drain(")]
    assert "and slots.busy()" in loop, "the main loop skips its lease while idle"


def test_a_freed_slot_is_offered_again():
    _with_policy(_policy("max_concurrent: 2\n"))
    slots = agent.JobSlots(2)
    done = threading.Event()
    slots.run(lambda: done.wait(5), "agent_ansible")
    slots.run(lambda: None, "agent_ansible")
    deadline = time.time() + 5
    while slots._running["ansible"] > 1 and time.time() < deadline:
        time.sleep(0.01)
    assert "agent_ansible" in slots.wanted()
    done.set()
    slots.drain(5)


def test_the_lease_body_names_the_free_types_only_when_concurrent():
    _with_policy(_policy("max_concurrent: 3\n"))
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
    dash.lease({"agent_ansible"})
    assert "want_job_types" not in sent[0]
    assert sent[0]["ansible_slots"] == 3
    assert sent[1]["want_job_types"] == ["agent_ansible"]


# ── credentials stay scoped to their job ──────────────────────────────────────

def test_a_finished_job_releases_only_its_own_credentials():
    dash = agent.Dashboard("https://dash.example.com")
    results = {}

    def _job(job_id, secret, finish: threading.Event, held: threading.Event):
        agent._JOB_CONTEXT.job_id = job_id
        dash.hold_secret(secret)
        held.set()
        finish.wait(5)
        results[job_id] = dash.redact(f"leak {secret}")
        dash.release_secrets(job_id)

    fin_a, fin_b = threading.Event(), threading.Event()
    held_a, held_b = threading.Event(), threading.Event()
    ta = threading.Thread(target=_job, args=("job-a", "secret-aaaa", fin_a, held_a))
    tb = threading.Thread(target=_job, args=("job-b", "secret-bbbb", fin_b, held_b))
    ta.start()
    tb.start()
    held_a.wait(5)
    held_b.wait(5)
    fin_a.set()
    ta.join(5)
    # Job a has ended; b is still running and its secret must still be scrubbed.
    assert dash.redact("leak secret-bbbb") == "leak ***redacted***"
    fin_b.set()
    tb.join(5)
    assert results["job-a"] == "leak ***redacted***"
    assert results["job-b"] == "leak ***redacted***"
    assert dash.redact("secret-bbbb") == "secret-bbbb", "a released secret was kept"


def test_a_value_two_jobs_share_stays_held_until_both_end():
    dash = agent.Dashboard("https://dash.example.com")
    for job_id in ("job-a", "job-b"):
        agent._JOB_CONTEXT.job_id = job_id
        dash.hold_secret("shared-key")
    dash.release_secrets("job-a")
    assert dash.redact("shared-key") == "***redacted***"
    dash.release_secrets("job-b")
    assert dash.redact("shared-key") == "shared-key"
    agent._JOB_CONTEXT.job_id = ""


def test_release_with_no_job_still_clears_everything():
    """The old contract, which test doubles and the serial path rely on."""
    dash = agent.Dashboard("https://dash.example.com")
    agent._JOB_CONTEXT.job_id = "job-a"
    dash.hold_secret("secret-aaaa")
    agent._JOB_CONTEXT.job_id = ""
    dash.release_secrets()
    assert dash.redact("secret-aaaa") == "secret-aaaa"


def test_each_worker_thread_gets_its_own_http_session():
    dash = agent.Dashboard("https://dash.example.com")
    seen = []
    t = threading.Thread(target=lambda: seen.extend([dash._http(), dash._http()]))
    t.start()
    t.join(5)
    assert dash._http() is dash.session
    assert seen[0] is seen[1] and seen[0] is not dash.session
    assert seen[0].verify == dash.session.verify


# ── Windows over OpenSSH ──────────────────────────────────────────────────────

def test_a_windows_server_over_ssh_gets_the_powershell_shell():
    inv = json.loads(agent._vm_inventory(ip="10.40.1.15", port=22, transport="ssh",
                                         login_user="Administrator", winrm={},
                                         windows_ssh=True))
    host = inv["all"]["hosts"]["target"]
    assert host["ansible_connection"] == "ssh" and host["ansible_shell_type"] == "powershell"
    linux = json.loads(agent._vm_inventory(ip="10.40.1.16", port=22, transport="ssh",
                                           login_user="ec2-user", winrm={}))
    assert "ansible_shell_type" not in linux["all"]["hosts"]["target"]


def test_the_shell_matches_the_dashboards_windows_ssh_vars():
    sys.path.insert(0, _ROOT)
    from web_dashboard.services.ansible_vm_cmd import WINDOWS_SSH_VARS
    inv = json.loads(agent._vm_inventory(ip="10.40.1.15", port=22, transport="ssh",
                                         login_user="", winrm={}, windows_ssh=True))
    host = inv["all"]["hosts"]["target"]
    for key, value in WINDOWS_SSH_VARS.items():
        assert host[key] == value


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
