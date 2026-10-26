"""The remote agent's updater: upgrades or restarts the agent container beside it.

Runs next to a remote agent on the agent's own host (examples/remote-agent/
docker-compose.updater.yml). It is the only container there that holds the Docker
socket, and it does exactly one thing with it: replace the agent container — a newer
image, or the same one restarted — and put the old one back if the new one does not come
up. The agent itself never holds the socket and never decides what runs.

**It trusts neither the dashboard nor the agent.** It has no network listener and never
talks to the dashboard. Its one input is ``request.json`` in a volume it shares with the
agent, and before it pulls anything it verifies the requested image's cosign signature
against the identity pinned in the host's own ``policy.yaml`` (``upgrades.verify``). A
compromised dashboard, or a compromised agent, can therefore at most ask for a release the
project itself signed. Which Docker operations it may perform is a short allowlist
(:class:`DockerAPI`), on containers labelled ``dashboard-agent.role=agent`` and images
from ``upgrades.repository`` only.

The handoff, all files in CONTROL_DIR, each written atomically:

  * ``request.json`` — written by the agent: ``{op, job_id, digest, version, ...}``;
  * ``status.json``  — written here: ``{job_id, state, reason, at}``, ``state`` one of
    verifying, pulling, swapping, waiting_ready, done, rolled_back, failed;
  * ``ready``        — written by the (new) agent after its first successful lease;
  * ``updater.alive`` — touched here every loop, so the agent can say an updater is present.

Stdlib only, plus PyYAML for the policy and the ``cosign`` binary for verification.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import quote, urlencode

import yaml

UPDATER_VERSION = "1.0.0"
ROLE_LABEL = "dashboard-agent.role"
# The AGENT version an image carries — not the release tag, which numbers dashboard
# releases. Set by runners/agent/Dockerfile from AGENT_VERSION; checked so a digest cannot
# claim to be a version it is not.
VERSION_LABEL = "dashboard-agent.version"

CONTROL_DIR = os.environ.get("AGENT_CONTROL_DIR", "/var/lib/dashboard-agent-control")
POLICY_FILE = os.environ.get("AGENT_POLICY_FILE", "/etc/dashboard-agent/policy.yaml")
DOCKER_SOCKET = os.environ.get("UPDATER_DOCKER_SOCKET", "/var/run/docker.sock")
COSIGN = os.environ.get("UPDATER_COSIGN", "cosign")
# For a private or air-gapped registry only, e.g. "--allow-insecure-registry
# --insecure-ignore-tlog=true" with `public_key:` verification. It cannot change WHICH key
# or identity is required; that comes from policy.yaml.
COSIGN_EXTRA = os.environ.get("UPDATER_COSIGN_ARGS", "").split()
POLL_S = float(os.environ.get("UPDATER_POLL_INTERVAL", "2"))
STOP_TIMEOUT_S = int(os.environ.get("UPDATER_STOP_TIMEOUT", "40"))

log = logging.getLogger("updater")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_REPO_RE = re.compile(
    r"^(?:[a-z0-9.-]+(?::\d{1,5})?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")


class Refused(Exception):
    """A request this updater will not carry out; the reason is written to status.json."""


# ── policy ────────────────────────────────────────────────────────────────────

def load_upgrades(path: str = POLICY_FILE) -> dict:
    """The host's ``upgrades:`` block, validated the way the agent validates it.

    ``{}`` when upgrades are not enabled — then every request is refused."""
    try:
        with open(path, "rb") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise Refused(f"cannot read policy.yaml: {exc}")
    value = doc.get("upgrades") if isinstance(doc, dict) else None
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return {}
    repo = str(value.get("repository") or "").strip()
    verify = value.get("verify")
    if not _REPO_RE.match(repo) or not isinstance(verify, dict) or len(verify) != 1:
        raise Refused("policy.yaml `upgrades:` is malformed; the agent refuses it too")
    if "keyless" in verify:
        kl = verify["keyless"] if isinstance(verify["keyless"], dict) else {}
        identity, issuer = str(kl.get("identity") or ""), str(kl.get("issuer") or "")
        if not (identity and issuer):
            raise Refused("policy.yaml `upgrades.verify.keyless` needs identity and issuer")
        verify = {"keyless": {"identity": identity, "issuer": issuer}}
    elif "public_key" in verify:
        verify = {"public_key": str(verify["public_key"])}
    else:
        raise Refused("policy.yaml `upgrades.verify` names no verification")
    timeout = value.get("ready_timeout_s", 480)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 60 <= timeout <= 540:
        raise Refused("policy.yaml `upgrades.ready_timeout_s` is out of range")
    return {"repository": repo, "verify": verify, "ready_timeout_s": timeout,
            "allow_downgrade": value.get("allow_downgrade") is True}


def cosign_args(upgrades: dict, ref: str) -> list:
    """The ``cosign verify`` command for ``ref`` under this host's policy."""
    args = [COSIGN, "verify"]
    verify = upgrades["verify"]
    if "keyless" in verify:
        args += ["--certificate-identity-regexp", verify["keyless"]["identity"],
                 "--certificate-oidc-issuer", verify["keyless"]["issuer"]]
    else:
        args += ["--key", verify["public_key"]]
    return args + COSIGN_EXTRA + [ref]


def verify_signature(upgrades: dict, ref: str) -> None:
    """Raise :class:`Refused` unless ``ref`` is signed by the policy's identity or key."""
    try:
        proc = subprocess.run(cosign_args(upgrades, ref), capture_output=True, text=True,
                              timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"could not run cosign: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise Refused(f"the image signature did not verify: {tail[0][:300]}")


# ── Docker, through an allowlist ──────────────────────────────────────────────

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 300):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class DockerError(Exception):
    pass


class DockerAPI:
    """The only Docker operations this process can perform.

    No exec, no arbitrary image, no container it did not find by the agent's role label:
    every method names its one endpoint, and the image methods accept only
    ``<upgrades.repository>@sha256:…``. ``ALLOWED`` is what the tests pin."""

    ALLOWED = ("list_agents", "inspect", "image_inspect", "image_inspect_id", "pull",
               "create", "start", "stop", "rename", "remove")

    def __init__(self, socket_path: str = DOCKER_SOCKET, repository: str = ""):
        self.socket_path = socket_path
        self.repository = repository

    def _call(self, method: str, path: str, body=None, *, ok=(200, 201, 204, 304)):
        conn = _UnixConnection(self.socket_path)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        if resp.status not in ok:
            raise DockerError(f"{method} {path.split('?')[0]}: HTTP {resp.status} "
                              f"{raw[:300].decode(errors='replace')}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            # /images/create streams one JSON object per line; the last carries any error.
            lines = [ln for ln in raw.decode(errors="replace").splitlines() if ln.strip()]
            last = json.loads(lines[-1]) if lines else {}
            if isinstance(last, dict) and last.get("error"):
                raise DockerError(f"pull failed: {last['error']}")
            return last

    def _image_ref(self, ref: str) -> str:
        repo, _, digest = ref.partition("@")
        if repo != self.repository or not _DIGEST_RE.match(digest):
            raise Refused(f"refusing image {ref}: only {self.repository}@sha256:… is allowed")
        return ref

    def _agent(self, cid: str) -> dict:
        info = self._call("GET", f"/containers/{quote(cid)}/json")
        if (info.get("Config", {}).get("Labels") or {}).get(ROLE_LABEL) != "agent":
            raise Refused(f"container {cid[:12]} is not labelled {ROLE_LABEL}=agent")
        return info

    def list_agents(self) -> list:
        filters = json.dumps({"label": [f"{ROLE_LABEL}=agent"]})
        return self._call("GET", "/containers/json?" + urlencode({"all": "1",
                                                                   "filters": filters}))

    def inspect(self, cid: str) -> dict:
        return self._agent(cid)

    def image_inspect(self, ref: str) -> dict:
        return self._call("GET", f"/images/{quote(self._image_ref(ref), safe='')}/json")

    def image_inspect_id(self, image_id: str) -> dict:
        """Read-only, by local image id (``sha256:…``): what the running agent's image
        supplied, so its defaults are not pinned onto the successor."""
        if not _DIGEST_RE.match(image_id or ""):
            raise Refused(f"refusing to inspect image {image_id!r}")
        return self._call("GET", f"/images/{quote(image_id, safe='')}/json")

    def pull(self, ref: str) -> None:
        self._call("POST", "/images/create?" + urlencode({"fromImage": self._image_ref(ref)}))

    def create(self, name: str, body: dict) -> str:
        if (body.get("Labels") or {}).get(ROLE_LABEL) != "agent":
            raise Refused(f"refusing to create a container not labelled {ROLE_LABEL}=agent")
        self._image_ref(body.get("Image") or "")
        return self._call("POST", "/containers/create?" + urlencode({"name": name}),
                          body)["Id"]

    def start(self, cid: str) -> None:
        self._agent(cid)
        self._call("POST", f"/containers/{quote(cid)}/start")

    def stop(self, cid: str, timeout: int = STOP_TIMEOUT_S) -> None:
        self._agent(cid)
        self._call("POST", f"/containers/{quote(cid)}/stop?t={int(timeout)}")

    def rename(self, cid: str, name: str) -> None:
        self._agent(cid)
        self._call("POST", f"/containers/{quote(cid)}/rename?" + urlencode({"name": name}))

    def remove(self, cid: str) -> None:
        self._agent(cid)
        self._call("DELETE", f"/containers/{quote(cid)}?force=1")


def successor_body(old: dict, old_image: dict, new_ref: str) -> dict:
    """The create body for the agent's successor: the old container's own settings — its
    mounts, user, group, read-only root, labels, restart policy, networks — with only the
    image changed. What the OLD IMAGE supplied (its Cmd, Entrypoint, WorkingDir, default
    Env) is dropped, so the new image's own defaults apply instead of being pinned to the
    previous release's."""
    cfg = dict(old.get("Config") or {})
    image_cfg = (old_image or {}).get("Config") or {}
    for key in ("Cmd", "Entrypoint", "WorkingDir"):
        if cfg.get(key) == image_cfg.get(key):
            cfg.pop(key, None)
    image_env = set(image_cfg.get("Env") or [])
    cfg["Env"] = [e for e in (cfg.get("Env") or []) if e not in image_env]
    if cfg.get("Hostname") == (old.get("Id") or "")[:12]:
        cfg.pop("Hostname", None)
    for key in ("Image", "Labels"):
        cfg.pop(key, None)
    labels = dict((old.get("Config") or {}).get("Labels") or {})
    labels.pop(VERSION_LABEL, None)
    networks = ((old.get("NetworkSettings") or {}).get("Networks") or {})
    endpoints = {name: {k: v for k, v in (ep or {}).items()
                        if k in ("Aliases", "IPAMConfig", "Links", "DriverOpts")}
                 for name, ep in networks.items()}
    return {**cfg, "Image": new_ref, "Labels": labels,
            "HostConfig": old.get("HostConfig") or {},
            "NetworkingConfig": {"EndpointsConfig": endpoints}}


# ── the handoff ───────────────────────────────────────────────────────────────

def _path(name: str) -> str:
    return os.path.join(CONTROL_DIR, name)


def read_json(name: str) -> Optional[dict]:
    try:
        with open(_path(name), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def write_status(job_id: str, state: str, reason: str = "") -> None:
    tmp = _path(".status.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"job_id": job_id, "state": state, "reason": reason,
                   "at": int(time.time()), "updater": UPDATER_VERSION}, fh)
    os.replace(tmp, _path("status.json"))
    log.info("job %s: %s%s", job_id[:8], state, f" — {reason}" if reason else "")


def touch_alive() -> None:
    with open(_path("updater.alive"), "w", encoding="utf-8") as fh:
        fh.write(str(int(time.time())))


class Updater:
    def __init__(self, docker: DockerAPI, *, sleep=time.sleep, clock=time.time):
        self.docker = docker
        self.sleep = sleep
        self.clock = clock

    def _the_agent(self) -> dict:
        running = [c for c in self.docker.list_agents()
                   if not any(n.endswith("-previous") for n in c.get("Names") or [])]
        if len(running) != 1:
            raise Refused(f"expected one container labelled {ROLE_LABEL}=agent, "
                          f"found {len(running)}")
        return self.docker.inspect(running[0]["Id"])

    def _wait_ready(self, job_id: str, version: str, timeout: int,
                    watch: Optional[str] = None) -> str:
        """"" once the agent wrote `ready` for this job at ``version``; else the reason."""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            ready = read_json("ready") or {}
            if ready.get("job_id") == job_id:
                if ready.get("version") == version:
                    return ""
                return f"the new agent came up as {ready.get('version')}, not {version}"
            if watch:
                info = self.docker.inspect(watch)
                state = info.get("State") or {}
                if not state.get("Running") and state.get("Status") in ("exited", "dead"):
                    return f"the new agent exited (code {state.get('ExitCode')})"
                # The successor inherits the restart policy, so one that crashes at start
                # is relaunched rather than left exited. A restart before `ready` is the
                # same failure; waiting out the timeout only delays the rollback.
                if state.get("Restarting") or int(info.get("RestartCount") or 0) > 0:
                    return (f"the new agent exited (code {state.get('ExitCode')}) and "
                            f"was restarting")
            self.sleep(POLL_S)
        return f"the new agent did not report ready within {timeout}s"

    def handle(self, request: dict, upgrades: dict) -> str:
        """Carry out one request. Returns the terminal state written."""
        job_id = str(request.get("job_id") or "")
        op = request.get("op")
        timeout = int(upgrades.get("ready_timeout_s") or 480)
        try:
            if not upgrades:
                raise Refused("policy.yaml does not enable `upgrades:`")
            if op == "restart":
                return self._restart(job_id, str(request.get("version") or ""), timeout)
            if op != "upgrade":
                raise Refused(f"unknown operation {op!r}")
            repo, digest = request.get("repository"), str(request.get("digest") or "")
            version = str(request.get("version") or "")
            if repo != upgrades["repository"] or not _DIGEST_RE.match(digest) \
                    or not _VERSION_RE.match(version):
                raise Refused("the request does not match policy.yaml (repository, digest "
                              "or version)")
            return self._upgrade(job_id, f"{repo}@{digest}", version, timeout, upgrades)
        except Refused as exc:
            write_status(job_id, "failed", str(exc))
            return "failed"
        except DockerError as exc:
            write_status(job_id, "failed", f"Docker refused: {exc}")
            return "failed"

    def _upgrade(self, job_id, ref, version, timeout, upgrades) -> str:
        write_status(job_id, "verifying")
        verify_signature(upgrades, ref)           # before anything is pulled
        write_status(job_id, "pulling")
        self.docker.pull(ref)
        labelled = ((self.docker.image_inspect(ref).get("Config") or {}).get("Labels")
                    or {}).get(VERSION_LABEL)
        if labelled != version:
            raise Refused(f"{ref} is labelled version {labelled!r}, not {version}")
        old = self._the_agent()
        old_id, name = old["Id"], old["Name"].lstrip("/")
        # By the container's image ID, which exists however it was started — by tag
        # (`:latest`, the usual first time) or by digest.
        try:
            old_image = self.docker.image_inspect_id(old.get("Image") or "")
        except (Refused, DockerError):
            old_image = {}
        body = successor_body(old, old_image, ref)
        write_status(job_id, "swapping")
        self.docker.stop(old_id)
        for c in self.docker.list_agents():
            if f"/{name}-previous" in (c.get("Names") or []):
                self.docker.remove(c["Id"])
        self.docker.rename(old_id, f"{name}-previous")
        try:
            new_id = self.docker.create(name, body)
            self.docker.start(new_id)
        except (DockerError, Refused) as exc:
            self._restore(old_id, name, None)
            write_status(job_id, "rolled_back", f"the new container would not start: {exc}")
            return "rolled_back"
        write_status(job_id, "waiting_ready")
        why = self._wait_ready(job_id, version, timeout, watch=new_id)
        if why:
            self._restore(old_id, name, new_id)
            write_status(job_id, "rolled_back", why)
            return "rolled_back"
        self.docker.remove(old_id)
        write_status(job_id, "done")
        return "done"

    def _restore(self, old_id: str, name: str, new_id: Optional[str]) -> None:
        if new_id:
            try:
                self.docker.stop(new_id, timeout=10)
            except DockerError:
                pass
            self.docker.remove(new_id)
        self.docker.rename(old_id, name)
        self.docker.start(old_id)

    def _restart(self, job_id: str, version: str, timeout: int) -> str:
        agent = self._the_agent()
        write_status(job_id, "swapping")
        self.docker.stop(agent["Id"])
        self.docker.start(agent["Id"])
        write_status(job_id, "waiting_ready")
        why = self._wait_ready(job_id, version, timeout, watch=agent["Id"])
        if why:
            write_status(job_id, "failed", why)
            return "failed"
        write_status(job_id, "done")
        return "done"

    def tick(self) -> Optional[str]:
        """One loop: say we are alive, and act on a request not yet acted on."""
        touch_alive()
        request = read_json("request.json")
        if not request or not request.get("job_id"):
            return None
        status = read_json("status.json") or {}
        if status.get("job_id") == request["job_id"]:
            return None                       # already acted on (or acting) on this one
        try:
            upgrades = load_upgrades()
        except Refused as exc:
            write_status(str(request["job_id"]), "failed", str(exc))
            return "failed"
        self.docker.repository = upgrades.get("repository", "")
        return self.handle(request, upgrades)


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    log.info("updater %s watching %s (policy %s, docker %s)", UPDATER_VERSION, CONTROL_DIR,
             POLICY_FILE, DOCKER_SOCKET)
    updater = Updater(DockerAPI())
    while True:
        try:
            updater.tick()
        except Exception:  # noqa: BLE001 — one bad pass must not end the watcher
            log.exception("updater pass failed")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
