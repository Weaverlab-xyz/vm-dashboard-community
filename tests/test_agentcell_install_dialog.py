"""The Agent tab's Install dialog writes commands that actually run.

It did not. The dialog passed `spiffe_id` / `agent_host` to agent-spiffe-entry.yml, which
requires `trust_domain` and `agent_node_id` and fails its first assert without them; and
`mcp_url` / `agent_pat` to agent-install.yml, which reads `agent_mcp_url` and
`agent_token` -- and the MCP URL it built was `/mcp`, where clients must use `/mcp/sse`.
Nothing noticed, because nothing compared the dialog with the playbooks. This does:

  * every `-e name=` the dialog emits is a variable its playbook declares (the drift guard);
  * the entry play reaches the SPIRE CLI through `spire_cli_prefix`, so it works against a
    docker or k8s lab, not only a VM one (run for real against SPIRE 1.15.3: the entry is
    created with the right parent and selector, and a prefixed re-run is a no-op);
  * the list API gives the dialog the facts it needs -- token mode, client id, the SPIRE
    host and CLI prefix, and the one node in the lab that runs a SPIRE agent.

Standalone:  python tests/test_agentcell_install_dialog.py
"""
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-install-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agent-install")

try:
    import fastapi  # noqa: F401 -- the optional third-party deps, probed by name
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.database import (AgentCell, Base, OAuthClient, SessionLocal,  # noqa: E402
                                    SpireLab, engine)
from web_dashboard.api import agentcell as api  # noqa: E402

Base.metadata.create_all(bind=engine)

_AGENT = os.path.join(_ROOT, "examples", "playbooks", "agent")
_PAGE = os.path.join(_ROOT, "web_dashboard", "templates", "workload_lab", "_agent.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _play(name):
    return yaml.safe_load(_read(os.path.join(_AGENT, name)))[0]


def _declared(name) -> set:
    """A play's variables: its `vars` block plus the ones its header documents as
    extra_vars (the required ones have no default, so they only appear there)."""
    src = _read(os.path.join(_AGENT, name))
    header = set(re.findall(r"^#\s+([a-z_]+):", src, re.M))
    return set(_play(name).get("vars", {})) | header


def _dialog_method(name) -> str:
    page = _read(_PAGE)
    i = page.index(f"    {name}() {{")
    return page[i:page.index("\n    },", i)]


def test_every_variable_the_dialog_passes_is_one_its_playbook_reads():
    for method, play in (("entryCommand", "agent-spiffe-entry.yml"),
                         ("installCommand", "agent-install.yml")):
        body = _dialog_method(method)
        passed = set(re.findall(r"-e \"?([a-z_]+)=", body))
        assert passed, f"{method} passes no variables -- the scan found nothing"
        unknown = passed - _declared(play)
        assert not unknown, f"{method} passes {sorted(unknown)}, which {play} never reads"


def test_the_dialog_passes_what_each_playbook_requires():
    entry = _dialog_method("entryCommand")
    for required in ("trust_domain", "agent_node_id"):
        assert f"-e {required}=" in entry, f"agent-spiffe-entry.yml asserts {required}"
    install = _dialog_method("installCommand")
    assert "-e agent_mcp_url=" in install and "/mcp/sse" in install, (
        "the worker is an SSE client; /mcp itself answers a redirect")
    assert "-e agent_token=" in install, "the play reads agent_token, not agent_pat"
    assert "agent_token_source=spiffe" in install and "agent_oauth_client_id=" in install, (
        "an SVID-bound agent holds no token; the dialog must say how it authenticates")


def test_the_entry_play_reaches_the_cli_through_the_labs_prefix():
    play = _play("agent-spiffe-entry.yml")
    assert play["vars"]["spire_cli_prefix"] == "", "vm-mode labs need no prefix"
    create = next(t for t in play["tasks"] if t.get("name") == "Create the registration entry")
    cmd = create["ansible.builtin.command"]
    assert isinstance(cmd, str) and cmd.startswith("{{ spire_cli_prefix }}{{ spire_bin }}"), (
        "a docker or k8s lab has no spire-server binary on the host")


def _lab(**kw):
    base = dict(id=str(uuid.uuid4()), name="lab", trust_domain="Install.Test", cloud="azure",
                bind_port=8081, status="available", public_ip="203.0.113.5",
                private_ip="10.0.0.5", deployment_mode="docker", k8s_status="linked",
                k8s_private_ip="10.0.0.9", k8s_public_ip="203.0.113.9")
    base.update(kw)
    return SpireLab(**base)


def _cell(lab, **kw):
    base = dict(id=str(uuid.uuid4()), name="agent", status="ready", spire_lab_id=lab.id,
                trust_domain=lab.trust_domain, private_ip="10.0.0.9")
    base.update(kw)
    return AgentCell(**base)


def test_the_list_gives_the_dialog_the_facts_it_needs():
    db = SessionLocal()
    try:
        lab = _lab()
        client = OAuthClient(id=str(uuid.uuid4()), client_id="vmsa_x", name="c",
                             spiffe_id="spiffe://install.test/agent/mcp-reader",
                             user_id=str(uuid.uuid4()), secret_hash="0" * 64, is_active=True)
        cell = _cell(lab, oauth_client_id=client.id)
        db.add_all([lab, client, cell])
        db.commit()
        facts = api._install_facts(db, cell)
        assert facts["token_mode"] == "spiffe" and facts["client_id"] == "vmsa_x"
        assert facts["spire_host"] == "203.0.113.5"
        assert facts["spire_cli_prefix"] == "docker exec spire-server "
        assert facts["agent_node_id"] == "spiffe://install.test/node/k3s-01"
        assert facts["worker_on_node"] is True

        pat_cell = _cell(lab, private_ip="10.0.0.77")
        unlinked = _lab(deployment_mode="vm", k8s_status=None)
        vm_cell = _cell(unlinked)
        db.add_all([pat_cell, unlinked, vm_cell])
        db.commit()
        facts = api._install_facts(db, pat_cell)
        assert facts["token_mode"] == "pat" and facts["client_id"] == ""
        assert facts["worker_on_node"] is False, "not the node that runs the SPIRE agent"
        facts = api._install_facts(db, vm_cell)
        assert facts["spire_cli_prefix"] == "" and facts["agent_node_id"] == ""
    finally:
        db.close()


def test_the_facts_carry_nothing_secret():
    from web_dashboard.models.agentcell import AgentCellInfo
    added = {"token_mode", "client_id", "spire_host", "spire_cli_prefix",
             "agent_node_id", "agent_node_host", "worker_on_node"}
    assert added <= set(AgentCellInfo.model_fields)
    assert not any("secret" in f or f == "token" for f in added)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
