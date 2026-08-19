"""End-to-end drive of ``agent.run_ansible`` against a stubbed Docker Engine.

The static assertions in ``test_agent_ansible_run.py`` pin shapes; this one pins
BEHAVIOUR, because the failure modes that matter here only appear when the handler
actually runs. Two real bugs were caught by exactly this harness and neither was
reachable statically: ``Cmd`` passed as a string where the Engine API needs an array,
and a cancel raising a policy refusal instead of letting ``execute`` report
"Cancelled by the operator".

What it drives: a Linux VM run, a Windows/WinRM run, a database run, a failed playbook,
a refused ``ansible_*`` var, an out-of-policy target, an out-of-policy port, and audit
mode. The Engine, the archive PUT and the log stream are stubbed; everything else — the
policy gate, the inventory rendering, the file layout, the credential mapping, the
container spec — is the real code.

Stdlib only, no app, no Docker. Standalone or under pytest:
    python tests/test_agent_ansible_e2e.py
"""

import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("agent", os.path.join(REPO, "runners", "agent", "agent.py"))
agent = importlib.util.module_from_spec(spec)
sys.modules["agent"] = agent
spec.loader.exec_module(agent)

POLICY = textwrap.dedent("""
    targets:
      - cidr: 10.20.0.0/24
        ports: [443]
    job_types: [agent_ansible]
    ansible:
      enabled: true
      vm_image: chrweav/ansible-winrm:latest
      db_image: chrweav/ansible-cloud:latest
      targets:
        - cidr: 127.0.0.0/8
          ports: [22, 5985]
        - cidr: 10.20.10.0/24
          ports: [22, 5985]
      max_runtime_minutes: 1
""")


def load_policy():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(POLICY)
        path = fh.name
    try:
        pol = agent.Policy.load(path)
    finally:
        os.unlink(path)
    # The default deny would block 127.0.0.0/8; drop it so the harness can use loopback as a
    # stand-in address without the policy refusing first. Everything else stays real.
    pol.deny = [n for n in pol.deny if str(n) != "127.0.0.0/8"]
    return pol


def frame(stream, payload):
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


class Engine:
    """Records every Engine call and replays a scripted container run."""

    def __init__(self, *, exit_code=0, lines=(b"PLAY [all]\n", b"ok: [target]\n"),
                 create_status=201, logs_status=200):
        self.calls = []
        self.created = None
        self.archives = []
        self.exit_code = exit_code
        self.lines = lines
        self.create_status = create_status
        self.logs_status = logs_status

    def engine(self, method, path, body=None, timeout=60.0, raw=False):
        self.calls.append((method, path.split("?")[0]))
        if path == "/containers/create":
            self.created = body
            return self.create_status, {"Id": "c0ffee123456"}
        if path.endswith("/start"):
            return 204, {}
        if path.endswith("/wait"):
            return 200, {"StatusCode": self.exit_code}
        if "/json" in path:
            return 200, {"State": {"OOMKilled": False, "ExitCode": self.exit_code}}
        if method == "DELETE":
            return 204, {}
        if "/kill" in path:
            return 204, {}
        return 200, {}

    # A stand-in for the streaming log reader: hands the agent the frames it would read.
    def stream(self, container, on_line, deadline):
        assert self.logs_status == 200, "logs_status != 200 should have raised"
        buf = bytearray()
        for ln in self.lines:
            buf += frame(1, ln)
        held = bytearray()
        for stream_id, payload in agent._demux_frames(buf):
            held += payload
            while True:
                nl = held.find(b"\n")
                if nl < 0:
                    break
                on_line(bytes(held[:nl]).decode())
                del held[:nl + 1]

    def archive_put(self, container, files):
        self.archives.append(files)
        # Prove the tar the real implementation would build is actually well-formed.
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w") as tar:
            seen = set()
            for p, content in files.items():
                parts = p.strip("/").split("/")[:-1]
                for i in range(len(parts)):
                    d = "/".join(parts[:i + 1])
                    if d in seen:
                        continue
                    seen.add(d)
                    info = tarfile.TarInfo(d)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o700
                    tar.addfile(info)
                info = tarfile.TarInfo(p.strip("/"))
                info.size = len(content)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(content))
        blob.seek(0)
        with tarfile.open(fileobj=blob) as tar:
            self.tar_names = tar.getnames()
            for m in tar.getmembers():
                if m.isfile():
                    assert m.mode == 0o600, f"{m.name} is not 0600"


class Dashboard:
    def __init__(self, bundle):
        self.bundle = bundle
        self.held = []

    def ansible_bundle(self, job_id, *, run_kind, transport, host, port):
        self.ref = agent.bundle_ref(run_kind=run_kind, transport=transport,
                                    host=host, port=port)
        return self.bundle, self.bundle.get("_scrub", [])


def run(*, bundle, payload, engine=None, mode="live"):
    eng = engine or Engine()
    pol = load_policy()
    lines = []
    agent.MODE = mode
    orig_engine, orig_arch, orig_stream, orig_resolve = (
        agent._engine, agent._archive_put, agent._stream_logs, agent._resolve_all)
    agent._engine = eng.engine
    agent._archive_put = eng.archive_put
    agent._stream_logs = eng.stream
    agent._resolve_all = lambda h: ["127.0.0.1"] if h == "loop.test" else orig_resolve(h)
    try:
        result = agent.run_ansible(payload, pol, lines.append, lambda: False,
                                   "job-1", Dashboard(bundle))
        return result, lines, eng
    finally:
        (agent._engine, agent._archive_put, agent._stream_logs,
         agent._resolve_all) = orig_engine, orig_arch, orig_stream, orig_resolve
        agent.MODE = "live"


VM_BUNDLE = {
    "run_kind": "vm", "transport": "ssh", "job_dir": "/opt/job",
    "playbook": "- hosts: all\n  tasks: []\n",
    "asset_name": "", "asset_b64": "",
    "extra_vars": {"role": "web"},
    "login_user": "svc", "login_password": "pw-1234",
    "become_password": "sudo-1234",
    "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabcdefgh\n-----END OPENSSH PRIVATE KEY-----\n",
    "winrm": {}, "env": {"PASSWORD_SAFE_CLIENT_SECRET": "ps-secret-1234"},
    "_scrub": ["pw-1234", "sudo-1234", "ps-secret-1234"],
}
VM_PAYLOAD = {"run_kind": "vm", "transport": "ssh", "target_host": "loop.test",
              "target_port": 22}

failures = 0


def check(label, cond, extra=""):
    global failures
    if cond:
        print(f"ok   {label}")
    else:
        failures += 1
        print(f"FAIL {label} {extra}")


# ── a successful VM run ───────────────────────────────────────────────────────
result, lines, eng = run(bundle=VM_BUNDLE, payload=VM_PAYLOAD)
check("a vm run returns exit 0", result == {"exit_code": 0}, result)
check("output is forwarded", any("PLAY [all]" in l for l in lines), lines)
check("policy resolution is reported", any("resolved to 127.0.0.1:22" in l for l in lines), lines)
check("Cmd is a list", isinstance(eng.created["Cmd"], list), eng.created.get("Cmd"))
check("Cmd goes through sh -c", eng.created["Cmd"][:2] == ["/bin/sh", "-c"], eng.created["Cmd"])
check("the command execs", eng.created["Cmd"][2].startswith("exec ansible-playbook"),
      eng.created["Cmd"][2][:60])
check("Entrypoint cleared", eng.created["Entrypoint"] == [], eng.created.get("Entrypoint"))
check("image comes from policy",
      eng.created["Image"] == "chrweav/ansible-winrm:latest", eng.created["Image"])
check("no bind mounts", eng.created["HostConfig"]["Binds"] == [])
check("read-only rootfs", eng.created["HostConfig"]["ReadonlyRootfs"] is True)
check("ANSIBLE_HOME relocated",
      any(e.startswith("ANSIBLE_HOME=/tmp/") for e in eng.created["Env"]), eng.created["Env"][:3])
check("bundle env reaches the container",
      any(e.startswith("PASSWORD_SAFE_CLIENT_SECRET=") for e in eng.created["Env"]))

files = eng.archives[0]
check("playbook written", "opt/job/playbook.yml" in files, sorted(files))
check("inventory written", "opt/job/inventory.json" in files, sorted(files))
check("key written", "opt/job/id_rsa" in files, sorted(files))
check("vars written", "opt/job/secret_vars.json" in files, sorted(files))
check("tar has parent dirs", "opt" in eng.tar_names and "opt/job" in eng.tar_names, eng.tar_names)

inv = json.loads(files["opt/job/inventory.json"])["all"]["hosts"]["target"]
check("inventory pins the resolved ip", inv["ansible_host"] == "127.0.0.1", inv)
check("inventory sets ssh", inv["ansible_connection"] == "ssh", inv)
check("inventory carries the login user", inv["ansible_user"] == "svc", inv)

pv = json.loads(files["opt/job/secret_vars.json"])
check("operator var present", pv.get("role") == "web", pv)
check("ssh password mapped", pv.get("ansible_ssh_pass") == "pw-1234", pv)
check("winrm password mapped", pv.get("ansible_password") == "pw-1234", pv)
check("become password mapped", pv.get("ansible_become_password") == "sudo-1234", pv)

check("wait comes after the delete-free happy path",
      ("POST", "/containers/c0ffee123456/wait") in eng.calls, eng.calls)
check("container is removed", ("DELETE", "/containers/c0ffee123456") in eng.calls, eng.calls)

# ── a windows run ─────────────────────────────────────────────────────────────
win = dict(VM_BUNDLE, transport="winrm", winrm={"scheme": "http", "transport": "ntlm",
                                               "cert_validation": "ignore"},
           ssh_private_key="")
res, lines, eng = run(bundle=win, payload={"run_kind": "vm", "transport": "winrm",
                                           "target_host": "loop.test", "target_port": 5985})
inv = json.loads(eng.archives[0]["opt/job/inventory.json"])["all"]["hosts"]["target"]
check("winrm inventory", inv["ansible_connection"] == "winrm"
      and inv["ansible_winrm_transport"] == "ntlm", inv)
check("no key file when there is no key", "opt/job/id_rsa" not in eng.archives[0])
check("no --private-key when there is no key",
      "--private-key" not in eng.created["Cmd"][2], eng.created["Cmd"][2])

# ── a database run ────────────────────────────────────────────────────────────
db = {"run_kind": "database", "transport": "local", "playbook": "- hosts: localhost\n",
      "asset_name": "", "asset_b64": "", "extra_vars": {},
      "login_user": "", "login_password": "", "become_password": "",
      "ssh_private_key": "", "winrm": {}, "env": {},
      "db": {"db_engine": "postgres", "db_login_host": "db.lan",
             "db_login_password": "dbpw-1234", "db_login_user": "admin"}}
res, lines, eng = run(bundle=db, payload={"run_kind": "database", "transport": "local",
                                          "target_host": "loop.test", "target_port": 22})
check("database run uses the db image",
      eng.created["Image"] == "chrweav/ansible-cloud:latest", eng.created["Image"])
check("database run is a localhost play", "-c local" in eng.created["Cmd"][2]
      or "'localhost,'" in eng.created["Cmd"][2], eng.created["Cmd"][2])
check("no inventory file for a localhost play",
      "opt/job/inventory.json" not in eng.archives[0], sorted(eng.archives[0]))
dv = json.loads(eng.archives[0]["opt/job/secret_vars.json"])
check("db login vars reach the play", dv.get("db_login_host") == "db.lan", dv)

# ── failure paths ─────────────────────────────────────────────────────────────
try:
    run(bundle=VM_BUNDLE, payload=VM_PAYLOAD, engine=Engine(exit_code=2))
    check("a failed playbook raises", False, "no exception")
except agent.PolicyRefusal as e:
    check("a failed playbook raises and explains", "one or more hosts failed" in str(e), str(e))

try:
    run(bundle=dict(VM_BUNDLE, extra_vars={"ansible_connection": "local"}), payload=VM_PAYLOAD)
    check("an ansible_ extra var is refused", False, "accepted")
except agent.PolicyRefusal as e:
    check("an ansible_ extra var is refused", "refuses them" in str(e), str(e))

try:
    run(bundle=VM_BUNDLE, payload={"run_kind": "vm", "transport": "ssh",
                                   "target_host": "10.99.0.1", "target_port": 22})
    check("an out-of-policy target is refused", False, "accepted")
except agent.PolicyRefusal as e:
    check("an out-of-policy target is refused", "ansible.targets" in str(e), str(e))

try:
    run(bundle=VM_BUNDLE, payload={"run_kind": "vm", "transport": "ssh",
                                   "target_host": "loop.test", "target_port": 3389})
    check("an out-of-policy port is refused", False, "accepted")
except agent.PolicyRefusal as e:
    check("an out-of-policy port is refused", "3389" in str(e), str(e))

# audit mode must fetch nothing and run nothing
class NoFetch(Dashboard):
    def ansible_bundle(self, *a, **k):
        raise AssertionError("audit mode fetched the bundle")

eng = Engine()
pol = load_policy()
lines = []
agent.MODE = "audit"
oe, oa, os_, orr = agent._engine, agent._archive_put, agent._stream_logs, agent._resolve_all
agent._engine, agent._archive_put, agent._stream_logs = eng.engine, eng.archive_put, eng.stream
agent._resolve_all = lambda h: ["127.0.0.1"]
try:
    res = agent.run_ansible(VM_PAYLOAD, pol, lines.append, lambda: False, "j", NoFetch({}))
    check("audit mode executes nothing", res.get("audit") is True and eng.created is None, res)
    check("audit mode says so", any("AUDIT MODE" in l for l in lines), lines)
except AssertionError as e:
    check("audit mode executes nothing", False, str(e))
finally:
    agent._engine, agent._archive_put, agent._stream_logs, agent._resolve_all = oe, oa, os_, orr
    agent.MODE = "live"

print(f"\n{'ALL OK' if not failures else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
