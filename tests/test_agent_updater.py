"""The agent's updater (runners/agent-updater/updater.py): the one process on an agent host
that holds the Docker socket.

What each test pins:

  * **The signature comes first.** A request whose image does not verify is refused
    before anything is pulled; a digest that claims the wrong version is refused before
    the agent is touched.
  * **The successor is the same container**: mounts, user, groups, labels and networks
    carried over; only the image (and what the OLD image supplied) changes.
  * **It puts the old agent back** when the new one does not report ready, or exits.
  * **Restart** stops and starts the same container and waits for ready.
  * **The allowlist.** Only the agent's labelled container, only images from the
    policy's repository by digest, only the listed operations.

Runs under pytest, or standalone:  python tests/test_agent_updater.py
"""
import importlib.util
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "updater", os.path.join(_ROOT, "runners", "agent-updater", "updater.py"))
upd = importlib.util.module_from_spec(_spec)
sys.modules["updater"] = upd
_spec.loader.exec_module(upd)

REPO = "chrweav/dashboard-agent"
OLD = f"{REPO}@sha256:" + "1" * 64
NEW = f"{REPO}@sha256:" + "2" * 64
UPGRADES = {"repository": REPO, "verify": {"keyless": {"identity": "^x", "issuer": "y"}},
            "ready_timeout_s": 60, "allow_downgrade": False}


def _ctl():
    upd.CONTROL_DIR = tempfile.mkdtemp(prefix="upd-")


def _status():
    return upd.read_json("status.json") or {}


class FakeDocker:
    """In-memory stand-in for :class:`updater.DockerAPI`, recording every call."""

    def __init__(self, *, new_version="2.11.0", new_exits=False, ready_on_start=True):
        self.calls = []
        self.repository = REPO
        self.new_version = new_version
        self.new_exits = new_exits
        self.ready_on_start = ready_on_start
        self.containers = {"old": {
            "Id": "old" + "0" * 61, "Name": "/dashboard-agent", "Image": "sha256:" + "9" * 64,
            # Started by TAG, as a first upgrade usually is: the old image is still found.
            "Config": {"Image": f"{REPO}:latest", "User": "10001:10001", "Hostname": "old000000000",
                       "Env": ["PATH=/usr/bin", "DASHBOARD_URL=https://d"],
                       "Cmd": None, "Entrypoint": ["python", "/app/agent.py"],
                       "Labels": {"dashboard-agent.role": "agent",
                                  "com.docker.compose.service": "agent",
                                  "dashboard-agent.version": "2.10.0"}},
            "HostConfig": {"Binds": ["agent_state:/var/lib/dashboard-agent"],
                           "GroupAdd": ["990"], "ReadonlyRootfs": True,
                           "RestartPolicy": {"Name": "unless-stopped"}},
            "NetworkSettings": {"Networks": {"agent_default": {"Aliases": ["agent"],
                                                               "IPAddress": "172.18.0.2"}}},
            "State": {"Running": True, "Status": "running"}}}

    def _find(self, cid):
        for key, c in self.containers.items():
            if c["Id"] == cid or key == cid:
                return key, c
        raise upd.DockerError(f"no such container {cid}")

    def list_agents(self):
        self.calls.append(("list_agents",))
        return [{"Id": c["Id"], "Names": [c["Name"]]} for c in self.containers.values()]

    def inspect(self, cid):
        return self._find(cid)[1]

    def image_inspect(self, ref):
        self.calls.append(("image_inspect", ref))
        return {"Config": {"Labels": {"dashboard-agent.version": self.new_version}}}

    def image_inspect_id(self, image_id):
        self.calls.append(("image_inspect_id", image_id))
        return {"Config": {"Env": ["PATH=/usr/bin"], "Cmd": None,
                           "Entrypoint": ["python", "/app/agent.py"],
                           "Labels": {"dashboard-agent.version": "2.10.0"}}}

    def pull(self, ref):
        self.calls.append(("pull", ref))

    def create(self, name, body):
        self.calls.append(("create", name, body))
        self.containers["new"] = {"Id": "new" + "0" * 61, "Name": "/" + name,
                                  "Config": {"Image": body["Image"], "Labels": body["Labels"]},
                                  "State": {"Running": False, "Status": "created"}}
        return self.containers["new"]["Id"]

    def start(self, cid):
        self.calls.append(("start", cid[:3]))
        key, c = self._find(cid)
        c["State"] = {"Running": True, "Status": "running"}
        if key == "new" and self.new_exits:
            c["State"] = {"Running": False, "Status": "exited", "ExitCode": 1}
        req = upd.read_json("request.json") or {}
        if self.ready_on_start and not (key == "new" and self.new_exits):
            version = self.new_version if key == "new" else req.get("version")
            with open(os.path.join(upd.CONTROL_DIR, "ready"), "w", encoding="utf-8") as fh:
                json.dump({"job_id": req.get("job_id"), "version": version}, fh)

    def stop(self, cid, timeout=40):
        self.calls.append(("stop", cid[:3]))
        self._find(cid)[1]["State"] = {"Running": False, "Status": "exited", "ExitCode": 0}

    def rename(self, cid, name):
        self.calls.append(("rename", cid[:3], name))
        self._find(cid)[1]["Name"] = "/" + name

    def remove(self, cid):
        self.calls.append(("remove", cid[:3]))
        key, _ = self._find(cid)
        del self.containers[key]


def _request(op="upgrade", version="2.11.0", digest=NEW.split("@")[1]):
    return {"op": op, "job_id": "job-1", "repository": REPO, "digest": digest,
            "version": version}


def _run(docker, request, *, signed=True):
    saved = upd.verify_signature

    def verify(upgrades, ref):
        docker.calls.append(("verify", ref))
        if not signed:
            raise upd.Refused("the image signature did not verify: no matching signatures")
    upd.verify_signature = verify
    with open(os.path.join(upd.CONTROL_DIR, "request.json"), "w", encoding="utf-8") as fh:
        json.dump(request, fh)
    clock = {"t": 0.0}
    try:
        return upd.Updater(docker, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                           clock=lambda: clock["t"]).handle(request, UPGRADES)
    finally:
        upd.verify_signature = saved


def _ops(docker):
    return [c[0] for c in docker.calls]


# ── the order of trust ────────────────────────────────────────────────────────

def test_an_unsigned_image_is_refused_before_anything_is_pulled():
    _ctl()
    docker = FakeDocker()
    assert _run(docker, _request(), signed=False) == "failed"
    assert _ops(docker) == ["verify"], docker.calls
    assert "signature did not verify" in _status()["reason"]


def test_a_digest_claiming_the_wrong_version_never_touches_the_agent():
    _ctl()
    docker = FakeDocker(new_version="2.10.5")
    assert _run(docker, _request(version="2.11.0")) == "failed"
    assert "stop" not in _ops(docker) and "labelled version" in _status()["reason"]


def test_a_request_outside_the_policy_is_refused():
    _ctl()
    docker = FakeDocker()
    bad = {**_request(), "repository": "evil/agent"}
    assert _run(docker, bad) == "failed" and _ops(docker) == []


# ── the swap ──────────────────────────────────────────────────────────────────

def test_the_successor_is_the_same_container_with_a_new_image():
    _ctl()
    docker = FakeDocker()
    assert _run(docker, _request()) == "done", _status()
    assert _ops(docker).index("verify") < _ops(docker).index("pull") < _ops(docker).index("stop")
    create = next(c for c in docker.calls if c[0] == "create")
    name, body = create[1], create[2]
    assert name == "dashboard-agent" and body["Image"] == NEW
    assert body["User"] == "10001:10001"
    assert body["HostConfig"]["Binds"] == ["agent_state:/var/lib/dashboard-agent"]
    assert body["HostConfig"]["GroupAdd"] == ["990"] and body["HostConfig"]["ReadonlyRootfs"]
    assert body["Labels"]["dashboard-agent.role"] == "agent"
    assert body["Labels"]["com.docker.compose.service"] == "agent"
    assert "dashboard-agent.version" not in body["Labels"], "the old image's label was pinned"
    assert body["Env"] == ["DASHBOARD_URL=https://d"], "the old image's PATH was pinned"
    assert "Entrypoint" not in body and "Hostname" not in body
    assert body["NetworkingConfig"]["EndpointsConfig"] == {"agent_default": {"Aliases": ["agent"]}}
    assert ("remove", "old") in docker.calls, "the previous container was left behind"
    assert list(docker.containers) == ["new"]


def test_a_new_agent_that_never_reports_ready_is_rolled_back():
    _ctl()
    docker = FakeDocker(ready_on_start=False)
    assert _run(docker, _request()) == "rolled_back"
    assert "did not report ready" in _status()["reason"]
    assert list(docker.containers) == ["old"]
    old = docker.containers["old"]
    assert old["Name"] == "/dashboard-agent" and old["State"]["Running"]


def test_a_new_agent_that_exits_is_rolled_back_at_once():
    _ctl()
    docker = FakeDocker(new_exits=True)
    assert _run(docker, _request()) == "rolled_back"
    assert "exited (code 1)" in _status()["reason"]
    assert docker.containers["old"]["State"]["Running"]


def test_restart_stops_and_starts_the_same_container():
    _ctl()
    docker = FakeDocker()
    assert _run(docker, _request(op="restart", version="2.10.0")) == "done"
    assert ("stop", "old") in docker.calls and ("start", "old") in docker.calls
    assert "create" not in _ops(docker) and "pull" not in _ops(docker)


def test_a_restart_that_does_not_come_back_fails():
    _ctl()
    docker = FakeDocker(ready_on_start=False)
    assert _run(docker, _request(op="restart", version="2.10.0")) == "failed"


def test_a_request_is_acted_on_once():
    _ctl()
    docker = FakeDocker()
    with open(os.path.join(upd.CONTROL_DIR, "request.json"), "w", encoding="utf-8") as fh:
        json.dump(_request(), fh)
    upd.write_status("job-1", "done")
    assert upd.Updater(docker).tick() is None and docker.calls == []
    assert os.path.exists(os.path.join(upd.CONTROL_DIR, "updater.alive"))


# ── the allowlist ─────────────────────────────────────────────────────────────

def test_the_docker_client_exposes_only_the_allowlist():
    public = sorted(n for n in vars(upd.DockerAPI) if not n.startswith("_")
                    and callable(getattr(upd.DockerAPI, n)))
    assert public == sorted(upd.DockerAPI.ALLOWED), public


def test_the_docker_client_refuses_other_images_and_unlabelled_containers():
    api = upd.DockerAPI("/nonexistent.sock", repository=REPO)
    for ref in ("ubuntu@sha256:" + "3" * 64, f"{REPO}:latest", f"{REPO}@sha256:short"):
        try:
            api.pull(ref)
        except upd.Refused:
            continue
        raise AssertionError(f"{ref} was allowed")
    api._call = lambda method, path, body=None, ok=None: {"Config": {"Labels": {}}}
    try:
        api.stop("someone-else")
    except upd.Refused:
        pass
    else:
        raise AssertionError("stopped a container not labelled as the agent")
    try:
        api.create("x", {"Image": NEW, "Labels": {}})
    except upd.Refused:
        pass
    else:
        raise AssertionError("created an unlabelled container")


def test_cosign_is_asked_for_the_policys_identity_or_key():
    keyless = upd.cosign_args(UPGRADES, NEW)
    assert keyless[:2] == [upd.COSIGN, "verify"] and keyless[-1] == NEW
    assert "--certificate-identity-regexp" in keyless and "--certificate-oidc-issuer" in keyless
    keyed = upd.cosign_args({**UPGRADES, "verify": {"public_key": "/k.pub"}}, NEW)
    assert keyed[2:4] == ["--key", "/k.pub"]


def test_no_upgrades_block_means_every_request_is_refused():
    _ctl()
    docker = FakeDocker()
    state = upd.Updater(docker).handle(_request(), {})
    assert state == "failed" and "does not enable" in _status()["reason"] and docker.calls == []


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
