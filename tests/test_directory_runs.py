"""Config-Management runs against an on-premises directory, through its remote agent.

What these pin:

- ``directory`` is a run kind with exactly two transports, local (LDAP from the runner) and
  winrm (microsoft.ad on a Windows host), and must name its directory;
- the dashboard re-derives the target from the directory row: the agent must be the one
  the directory was registered with, an LDAP run aims at the directory's host:port, and a
  WinRM run at the chosen host on 5986; an LDAP directory cannot be run over WinRM;
- the inventory offers only on-prem, agent-bound directories as targets;
- the bundle carries the just-in-time dir_* vars, scrubs the bind password, and for WinRM
  logs on as that same account;
- the agent picks ``directory_image`` (falling back to ``db_image``), runs a localhost play
  for LDAP and a one-host WinRM inventory for AD, and passes only dir_* keys to the play.

Run: python tests/test_directory_runs.py   (or under pytest)
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dir-runs-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-directory-run-tests")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.database import (Base, ManagedDirectory, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine)
from web_dashboard.services import agent_ansible_meta as aam  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402

Base.metadata.create_all(bind=engine)

_spec = importlib.util.spec_from_file_location(
    "agent_runner_dirruns", os.path.join(_ROOT, "runners", "agent", "agent.py"))
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

ACCOUNT = {"system_id": 11, "account_id": 22, "account_name": "svc-ansible"}


def _agent(db, version="2.8.0"):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version=version, is_active=True,
                    public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdir",
                    last_seen_at=datetime.utcnow(), allowed_job_types=None,
                    created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    return a


def _dir(db, agent_id, **kw):
    args = dict(name="corp.example.com", provider="onprem_ad",
                host=f"dc-{uuid.uuid4().hex[:6]}.corp.example.com", agent_id=agent_id,
                managed_account=ACCOUNT, created_by="t")
    args.update(kw)
    return ds.register_onprem(db, **args)


class _Payload:
    def __init__(self, **kw):
        self.agent_id = kw.get("agent_id", "")
        self.target_kind = kw.get("target_kind", "directory")
        self.target_id = kw.get("target_id", "")
        self.connection_id = ""
        self.target = kw.get("target", "")
        self.transport = kw.get("transport", "")
        self.port = kw.get("port", 0)
        self.winrm_host = kw.get("winrm_host", "")


def _resolve(payload, db):
    from web_dashboard.api.config_mgmt import _resolve_agent_target
    return _resolve_agent_target(payload, db)


def _refused(fn, needle):
    try:
        fn()
    except HTTPException as e:
        assert needle in str(e.detail), e.detail
    else:
        raise AssertionError(f"accepted; expected a refusal mentioning {needle!r}")


# ── meta ──────────────────────────────────────────────────────────────────────

def test_meta_accepts_directory_with_two_transports():
    base = dict(run_kind="directory", target_host="dc01", target_port=636, target_id="d1")
    assert aam.check(dict(base, transport="local")) == ""
    assert aam.check(dict(base, transport="winrm", target_port=5986)) == ""
    assert "local or winrm" in aam.check(dict(base, transport="ssh"))
    assert "must name the directory" in aam.check(dict(base, transport="local", target_id=""))


# ── target resolution ────────────────────────────────────────────────────────

def test_ldap_run_aims_at_the_directory():
    db = SessionLocal()
    a = _agent(db)
    row = _dir(db, a.id, port=389, use_ldaps=False)
    out = _resolve(_Payload(agent_id=a.id, target_id=row.id), db)
    assert out["run_kind"] == "directory" and out["transport"] == "local"
    assert out["target_host"] == row.host and out["target_port"] == 389
    assert out["target_id"] == row.id
    db.close()


def test_winrm_run_aims_at_the_chosen_host_on_5986():
    db = SessionLocal()
    a = _agent(db)
    row = _dir(db, a.id)
    out = _resolve(_Payload(agent_id=a.id, target_id=row.id, transport="winrm",
                            winrm_host="mgmt01.corp.example.com"), db)
    assert out["transport"] == "winrm"
    assert out["target_host"] == "mgmt01.corp.example.com" and out["target_port"] == 5986
    out = _resolve(_Payload(agent_id=a.id, target_id=row.id, transport="winrm"), db)
    assert out["target_host"] == row.host
    db.close()


def test_resolution_refusals():
    db = SessionLocal()
    a, other = _agent(db), _agent(db)
    row = _dir(db, a.id)
    ldap = _dir(db, a.id, provider="ldap", name="ldap.example.com",
                base_dn="dc=example,dc=com", host=f"ldap-{uuid.uuid4().hex[:6]}")
    _refused(lambda: _resolve(_Payload(agent_id=other.id, target_id=row.id), db),
             "not reachable through agent")
    _refused(lambda: _resolve(_Payload(agent_id=a.id, target_id="nope"), db),
             "No such on-premises directory")
    _refused(lambda: _resolve(_Payload(agent_id=a.id, target_id=row.id, transport="ssh"), db),
             "transport 'local'")
    _refused(lambda: _resolve(_Payload(agent_id=a.id, target_id=ldap.id, transport="winrm"),
                              db), "WinRM runs are for Active Directory")
    old = _agent(db, version="2.7.0")
    row.agent_id = old.id
    db.commit()
    _refused(lambda: _resolve(_Payload(agent_id=old.id, target_id=row.id), db), "2.8")
    db.close()


def test_directory_run_needs_an_agent_and_the_directories_grant():
    import inspect
    from web_dashboard.api import config_mgmt
    src = inspect.getsource(config_mgmt.run_playbook)
    assert 'target_kind == "directory"' in src and "only through its remote agent" in src
    src = inspect.getsource(config_mgmt._run_agent_ansible)
    assert 'has_explicit_permission(\n            current_user, "directories", "write")' in src


# ── inventory ────────────────────────────────────────────────────────────────

def test_inventory_target_spec():
    from web_dashboard.services.inventory_service import _directory_item, _target_spec
    db = SessionLocal()
    a = _agent(db)
    row = _dir(db, a.id)
    spec = _target_spec(_directory_item(row))
    assert spec == {"target_kind": "directory", "target_id": row.id, "agent_id": a.id,
                    "target": row.host, "port": 636, "transport": "local"}
    cloud = ManagedDirectory(id=str(uuid.uuid4()), name="aws.example.com", cloud="aws",
                             provider="aws_managed_ad", status="available",
                             source="registered", created_by="t")
    assert isinstance(_target_spec(_directory_item(cloud)), str)
    db.close()


# ── bundle ───────────────────────────────────────────────────────────────────

class _Job:
    def __init__(self, meta):
        self.metadata_dict = meta


def _bundle(meta):
    from web_dashboard.services import agent_ansible_bundle as aab, btapi_service

    async def checkout(system_id, account_id, duration_min=60, **kw):
        return "req-1", "B1nd!Secret"

    async def playbook(asset, backend, prefetched_b64=""):
        return "- hosts: localhost\n  tasks: []\n", "", b""

    async def no_url(*a, **k):
        return ""

    saved = (btapi_service.get_ps_credential_with_request, aab._playbook_and_asset,
             aab._remote_fetch_url)
    btapi_service.get_ps_credential_with_request = checkout
    aab._playbook_and_asset, aab._remote_fetch_url = playbook, no_url
    try:
        db = SessionLocal()
        try:
            return asyncio.run(aab.build(db, job=_Job(meta), agent=None))
        finally:
            db.close()
    finally:
        (btapi_service.get_ps_credential_with_request, aab._playbook_and_asset,
         aab._remote_fetch_url) = saved


def test_bundle_carries_jit_vars_and_scrubs_the_password():
    db = SessionLocal()
    a = _agent(db)
    row = _dir(db, a.id)
    db.close()
    meta = {"run_kind": "directory", "transport": "local", "target_host": "dc01",
            "target_port": 636, "target_id": row.id, "asset": "p.yml", "extra_vars": {}}
    bundle, scrub = _bundle(meta)
    assert bundle["directory"]["dir_bind_dn"] == "svc-ansible@corp.example.com"
    assert bundle["directory"]["dir_bind_password"] == "B1nd!Secret"
    assert "B1nd!Secret" in scrub
    assert bundle["login_password"] == ""
    bundle, _ = _bundle(dict(meta, transport="winrm", target_port=5986))
    assert bundle["login_user"] == "svc-ansible@corp.example.com"
    assert bundle["login_password"] == "B1nd!Secret"
    assert bundle["winrm"]["scheme"] == "https"


# ── agent ────────────────────────────────────────────────────────────────────

def _policy(extra=""):
    doc = textwrap.dedent("""
        targets:
          - cidr: 10.20.0.0/24
        job_types: [agent_ansible]
        ansible:
          enabled: true
          vm_image: chrweav/ansible-winrm:latest
          db_image: chrweav/ansible-cloud:latest
          targets:
            - cidr: 10.20.10.0/24
              ports: [389, 636, 5986]
        """) + extra
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(doc)
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


def test_agent_image_falls_back_to_db_image():
    assert _policy().ansible_image("directory") == "chrweav/ansible-cloud:latest"
    pol = _policy("  directory_image: example/dir:1\n")
    assert pol.ansible_image("directory") == "example/dir:1"


def test_agent_argv_per_transport():
    local = agent._ansible_argv(run_kind="directory", transport="local", has_key=False,
                                has_vars=True)
    assert local[:5] == ["ansible-playbook", "-i", "localhost,", "-c", "local"]
    winrm = agent._ansible_argv(run_kind="directory", transport="winrm", has_key=False,
                                has_vars=True)
    assert f"{agent._JOB_DIR}/inventory.json" in winrm


class _Dash:
    def __init__(self, bundle):
        self.bundle = bundle

    def ansible_bundle(self, job_id, **kw):
        return self.bundle, []


def _run(transport, port):
    captured = {}

    def sibling(policy, *, image, files, env, command, emit, cancelled):
        captured.update(image=image, files=files, command=command)
        return 0

    bundle = {"run_kind": "directory", "transport": transport,
              "playbook": "- hosts: all\n  tasks: []\n", "extra_vars": {},
              "login_user": "svc@corp.example.com" if transport == "winrm" else "",
              "login_password": "B1nd!Secret" if transport == "winrm" else "",
              "winrm": {"scheme": "https", "transport": "ntlm", "cert_validation": "ignore"},
              "directory": {"dir_host": "dc01", "dir_bind_password": "B1nd!Secret",
                            "ansible_connection": "local"}}
    saved = agent._run_ansible_sibling, agent._resolve_all
    agent._run_ansible_sibling = sibling
    agent._resolve_all = lambda h: ["10.20.10.5"]
    try:
        agent.run_ansible({"run_kind": "directory", "transport": transport,
                           "target_host": "dc01", "target_port": port},
                          _policy(), lambda line: None, lambda: False, "j", _Dash(bundle))
    finally:
        agent._run_ansible_sibling, agent._resolve_all = saved
    return captured


def test_agent_runs_ldap_as_a_localhost_play():
    got = _run("local", 636)
    assert got["image"] == "chrweav/ansible-cloud:latest"
    assert "localhost," in got["command"]
    assert not any(k.endswith("inventory.json") for k in got["files"])
    play_vars = json.loads(got["files"]["opt/job/secret_vars.json"])
    assert play_vars["dir_bind_password"] == "B1nd!Secret"
    assert "ansible_connection" not in play_vars, "only dir_* keys may come from the bundle"


def test_agent_runs_ad_over_winrm_with_one_credential():
    got = _run("winrm", 5986)
    inv = json.loads(got["files"]["opt/job/inventory.json"])["all"]["hosts"]["target"]
    assert inv["ansible_connection"] == "winrm" and inv["ansible_port"] == 5986
    assert inv["ansible_winrm_scheme"] == "https"
    assert inv["ansible_user"] == "svc@corp.example.com"
    play_vars = json.loads(got["files"]["opt/job/secret_vars.json"])
    assert play_vars["ansible_password"] == "B1nd!Secret"


def test_agent_refuses_an_ssh_directory_run():
    try:
        _run("ssh", 636)
    except agent.PolicyRefusal as e:
        assert "local or winrm" in str(e)
    else:
        raise AssertionError("an ssh directory run was accepted")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
