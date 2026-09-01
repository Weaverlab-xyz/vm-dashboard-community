"""The POV's Entitle agent — the refusals, the play, and the two silent mistakes.

This slice removes the one prerequisite ``docs/pov-instance.md`` said the dashboard could
not satisfy. What is pinned here is what would otherwise go wrong quietly:

  * **The token is per POV, never per tenant.** Two POVs for one customer share an Entitle
    tenant, and the tenant's ``agent_token_name`` can name only one agent. The second POV
    would create integrations pointed at the first one's network and *nothing would
    error* — Entitle creates them happily and the SSH connector simply cannot reach the
    host.
  * **The play text carries no secret.** It travels in the job row's ``asset_bytes_b64``,
    which is database metadata in plain text; the token rides ``secret_vars`` as a var
    NAME and is resolved into the sealed bundle.
  * **The chart's defaults do not fit a POV VM.** Three replicas at 1 CPU of requests each
    leaves the pods Pending forever, with no error anywhere but ``kubectl describe``.
  * **The Config-Management grant is per target, per port.** A Linux host gets 22 and a
    Windows one gets WinRM; granting the union would widen what may have a playbook
    applied to it as root.
  * **Teardown destroys the token.** Entitle refuses to mint a name it already holds and
    cannot read the value back, so a survivor wedges the next POV that derives the name.

Uses a real SQLite database. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_entitle_agent.py
"""
import asyncio
import base64
import json
import os
import sys
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-entitle-agent")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, bt_tenant_service,  # noqa: E402
                                    config_service, entitle_registration_service,
                                    pov_broker, pov_entitle_agent as ea, pov_env_service,
                                    pov_wireup)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _agent(db, *, version="2.4.0",
           reported=("agent_discover", "agent_gateway", "agent_ansible")):
    row, _code = agent_service.create_agent(db, name=_name("brk"), created_by="t")
    row.public_key = "k"
    row.agent_version = version
    row.reported_job_types = json.dumps(list(reported))
    row.last_seen_at = datetime.utcnow()
    db.commit()
    return row


def _tenant(db, **kw):
    kw.setdefault("name", _name("acme"))
    kw.setdefault("secret", "ent-api-key")
    kw.setdefault("options", {"owner_id": "own-1", "workflow_id": "wf-1",
                              "ssh_sudo_user": "btadmin"})
    return bt_tenant_service.create(db, kind="entitle", created_by="t", **kw)


def _env(db, **kw):
    kw.setdefault("name", _name("poc"))
    env = d.PovEnvironment(platform="skytap", platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE, **kw)
    db.add(env)
    db.commit()
    return env


def _vm(db, env, *, name="entitle", os_family="linux", ip="10.9.0.30"):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"),
                             name=name, os_family=os_family, private_ip=ip)
    db.add(row)
    db.commit()
    return row


def _ready(db):
    """A POV with everything the install needs, so a test can take one thing away."""
    agent = _agent(db)
    tenant = _tenant(db)
    env = _env(db, broker_agent_id=agent.id, entitle_tenant_id=tenant["id"])
    vm = _vm(db, env)
    return env, agent, vm, tenant


class _FakeMint:
    """Stands in for the Terraform apply. Records the ctx it was handed, because *which
    tenant the token was minted in* is the property that cannot be checked any other way
    and is the one that fails silently."""

    def __init__(self, token="tok-blob-abc"):
        self.token = token
        self.calls = []

    async def __call__(self, name, ctx=None):
        self.calls.append((name, ctx))
        return {"token": self.token, "tf_state_json": '{"resources": []}'}


def _patch_mint(monkey=None, token="tok-blob-abc"):
    fake = _FakeMint(token)
    entitle_registration_service.mint_agent_token = fake  # noqa: S3v — test double
    return fake


_REAL_MINT = entitle_registration_service.mint_agent_token


def _restore_mint():
    entitle_registration_service.mint_agent_token = _REAL_MINT


# ── the play ─────────────────────────────────────────────────────────────────

def test_the_play_carries_no_secret_because_it_lands_in_the_job_row():
    """`asset_bytes_b64` is database metadata in plain text. The token reaches the run as
    a var NAME resolved into the sealed bundle, exactly as the Resource Broker's key
    does."""
    play = ea.playbook_yaml()
    assert "{{ " + ea.TOKEN_VAR + " }}" in play
    for leak in ("ENTITLE_TOKEN=", "api_key", "BEGIN "):
        assert leak not in play


def test_the_play_is_valid_yaml_and_ascii():
    """ASCII because this text is written to a file by an agent on a guest whose locale
    nobody chose, and parsed by a runner that already has to be told about BOMs."""
    import yaml
    play = ea.playbook_yaml()
    assert play.isascii()
    parsed = yaml.safe_load(play)
    assert isinstance(parsed, list) and len(parsed) == 1
    assert parsed[0]["hosts"] == "all"


def test_the_chart_defaults_are_overridden_for_a_pov_sized_vm():
    """The chart asks for three replicas at 1 CPU / 1Gi of requests each. On any VM an SE
    would give a POV the pods stay Pending forever, and the only place that says so is
    `kubectl describe`."""
    assert ea.CHART_DEFAULTS["entitle_agent_replicas"] == 1
    assert ea.CHART_DEFAULTS["entitle_agent_cpu_request"] != "1000m"
    values = ea._values_yaml()
    # `enabled: false` selects the lightweight sidecar INSTEAD of the DaemonSet; it does
    # not turn logging off. The sidecar is a native sidecar needing Kubernetes 1.29+.
    assert "sidecarLogs: false" in values
    assert "kubernetes_secret_manager" in ea.CHART_DEFAULTS["entitle_agent_kms_type"]


def test_the_values_file_is_removed_even_when_the_install_fails():
    """The token is the one secret that lands on the guest. An `always` block, not a task
    after the install, because a failed helm run must not leave it there."""
    import yaml
    block = yaml.safe_load(ea.playbook_yaml())[0]["tasks"][1]
    assert any("absent" in json.dumps(t) for t in block["always"])
    assert block["rescue"], "a failure must print why the pods did not start"


def test_binaries_are_absolute_because_sudo_rewrites_the_path():
    """`secure_path` on a RHEL-family guest excludes /usr/local/bin, and a bare `helm`
    there is 'command not found' — which reads as a failed install."""
    play = ea.playbook_yaml()
    assert "/usr/local/bin/helm upgrade" in play
    assert "/usr/local/bin/k3s kubectl" in play


# ── the host ─────────────────────────────────────────────────────────────────

def test_a_windows_host_is_refused_by_name():
    db = d.SessionLocal()
    env, _a, _v, _t = _ready(db)
    _vm(db, env, name="win", os_family="windows", ip="10.9.0.40")
    ea.configure(db, env, vm_name="win")
    try:
        ea.select_host_vm(db, env)
        raise AssertionError("a Windows guest was accepted as a k3s host")
    except ea.EntitleAgentError as exc:
        assert "Linux" in str(exc)
    finally:
        db.close()


def test_an_unknown_os_is_refused_rather_than_guessed():
    db = d.SessionLocal()
    env, _a, _v, _t = _ready(db)
    _vm(db, env, name="mystery", os_family="", ip="10.9.0.41")
    ea.configure(db, env, vm_name="mystery")
    try:
        ea.select_host_vm(db, env)
        raise AssertionError("a VM with no reported OS was accepted")
    except ea.EntitleAgentError as exc:
        assert "reports its OS as nothing" in str(exc)
    finally:
        db.close()


def test_a_missing_host_names_what_it_found():
    db = d.SessionLocal()
    env, _a, _v, _t = _ready(db)
    ea.configure(db, env, vm_name="not-there")
    try:
        ea.select_host_vm(db, env)
        raise AssertionError("a name that matches nothing was accepted")
    except ea.EntitleAgentError as exc:
        assert "Found:" in str(exc) and "entitle" in str(exc)
    finally:
        db.close()


def test_the_broker_vm_is_excluded_from_the_guessed_target_list():
    """Not from a host somebody NAMED — from the list the dashboard guesses. k3s brings
    its own containerd and iptables rules, and the broker is the one guest whose job is to
    keep the agent channel this install runs over up."""
    db = d.SessionLocal()
    env, _a, _v, _t = _ready(db)
    broker = _vm(db, env, name=pov_broker.broker_vm_name(env), os_family="linux",
                 ip="10.9.0.50")
    ea.configure(db, env, vm_name="nothing-matches-this")
    targets = ea.linux_targets(db, env)
    assert broker.private_ip not in targets
    assert "10.9.0.30" in targets
    db.close()


# ── the token ────────────────────────────────────────────────────────────────

def test_the_token_is_minted_in_the_povs_tenant_not_the_installs():
    """The silent cross-tenant mistake. Minting into the configured tenant instead of the
    customer's would look exactly like success."""
    db = d.SessionLocal()
    fake = _patch_mint()
    try:
        env, _a, _v, tenant = _ready(db)
        row = db.query(d.BeyondTrustTenant).filter(
            d.BeyondTrustTenant.id == tenant["id"]).first()
        resolved = bt_tenant_service.to_tenant(row)
        asyncio.run(ea.ensure_token(db, env, resolved))
        assert len(fake.calls) == 1
        _name_arg, ctx = fake.calls[0]
        assert ctx is not None, "the mint fell back to the configured tenant"
        assert ctx.api_key == "ent-api-key"
    finally:
        _restore_mint()
        db.close()


def test_a_second_install_reuses_the_stored_token_rather_than_re_minting():
    """Entitle returns a token's value only at creation and refuses to re-mint a name it
    holds, so a second mint is unrecoverable rather than merely wasteful."""
    db = d.SessionLocal()
    fake = _patch_mint()
    try:
        env, _a, _v, tenant = _ready(db)
        row = db.query(d.BeyondTrustTenant).filter(
            d.BeyondTrustTenant.id == tenant["id"]).first()
        resolved = bt_tenant_service.to_tenant(row)
        first = asyncio.run(ea.ensure_token(db, env, resolved))
        second = asyncio.run(ea.ensure_token(db, env, resolved))
        assert first == second
        assert len(fake.calls) == 1
    finally:
        _restore_mint()
        db.close()


def test_the_mint_name_is_unique_per_pov_not_per_name():
    """Two POVs an SE called the same thing must not collide in one tenant — the second
    would be permanently unable to install, with a remedy that breaks the first."""
    db = d.SessionLocal()
    a = _env(db, name="poc-shared")
    b = _env(db, name="poc-shared")
    assert ea.mint_name(a) != ea.mint_name(b)
    assert ea.mint_name(a) == ea.mint_name(a)
    db.close()


def test_the_token_never_reaches_the_job_row():
    db = d.SessionLocal()
    fake = _patch_mint(token="tok-super-secret")
    try:
        env, _a, vm, _t = _ready(db)
        job = asyncio.run(ea.queue(db, env, created_by="t"))
        blob = json.dumps(job.metadata_dict)
        assert "tok-super-secret" not in blob
        assert job.metadata_dict["secret_vars"] == {
            ea.TOKEN_VAR: ea.token_config_key(env.id)}
        # And the play itself, which DOES ride in the row, names only the variable.
        play = base64.b64decode(job.metadata_dict["asset_bytes_b64"]).decode()
        assert "tok-super-secret" not in play
        assert ea.TOKEN_VAR in play
    finally:
        _restore_mint()
        db.close()


def test_the_job_is_an_ssh_run_against_the_named_host():
    db = d.SessionLocal()
    _patch_mint()
    try:
        env, agent, vm, _t = _ready(db)
        job = asyncio.run(ea.queue(db, env, created_by="t"))
        meta = job.metadata_dict
        assert job.job_type == "agent_ansible"
        assert job.agent_id == agent.id
        assert meta["transport"] == "ssh"
        assert meta["target_port"] == 22
        assert meta["target_host"] == vm.private_ip
        # The login comes from the LAB PLATFORM, read per run — so the row names ids and
        # stores no credential.
        assert meta["pov_environment_id"] == env.id
        assert meta["pov_vm_id"] == vm.platform_vm_id
    finally:
        _restore_mint()
        db.close()


# ── what the wire-up reads ───────────────────────────────────────────────────

def test_the_povs_own_agent_beats_the_tenants():
    """Two POVs for one customer share a tenant, and its `agent_token_name` can name only
    one agent. Whichever POV installed second would build integrations pointed at the
    first one's network, and Entitle would create them without complaint."""
    db = d.SessionLocal()
    _patch_mint()
    try:
        tenant_row = _tenant(db, options={"owner_id": "o", "workflow_id": "w",
                                          "ssh_sudo_user": "u",
                                          "agent_token_name": "somebody-elses-agent"})
        row = db.query(d.BeyondTrustTenant).filter(
            d.BeyondTrustTenant.id == tenant_row["id"]).first()
        resolved = bt_tenant_service.to_tenant(row)
        env = _env(db, entitle_tenant_id=tenant_row["id"])
        assert pov_wireup.agent_token_name(env, resolved) == "somebody-elses-agent"
        asyncio.run(ea.ensure_token(db, env, resolved))
        assert pov_wireup.agent_token_name(env, resolved) == ea.mint_name(env)
    finally:
        _restore_mint()
        db.close()


def test_the_wireup_refusal_names_the_button_now_that_one_exists():
    """An SE who reads only the old half of this sentence goes off to deploy Kubernetes by
    hand."""
    db = d.SessionLocal()
    try:
        tenant_row = _tenant(db, options={"owner_id": "o", "workflow_id": "w",
                                          "ssh_sudo_user": "u"})
        env = _env(db, entitle_tenant_id=tenant_row["id"])
        try:
            asyncio.run(pov_wireup.entitle_context(db, env))
            raise AssertionError("a POV with no agent was accepted")
        except pov_wireup.WireupError as exc:
            assert "Entitle agent" in str(exc)
            assert "does not install" not in str(exc)
    finally:
        db.close()


# ── preflight ────────────────────────────────────────────────────────────────

def test_a_pov_with_no_entitle_tenant_is_refused_before_anything_is_minted():
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env)
    try:
        ea.preflight(db, env)
        raise AssertionError("a POV with no Entitle tenant was accepted")
    except ea.EntitleAgentError as exc:
        assert "names no Entitle tenant" in str(exc)
    finally:
        db.close()


def test_a_broker_that_may_not_run_config_management_is_refused_with_the_remedy():
    db = d.SessionLocal()
    agent = _agent(db, reported=("agent_discover",))
    tenant = _tenant(db)
    env = _env(db, broker_agent_id=agent.id, entitle_tenant_id=tenant["id"])
    _vm(db, env)
    try:
        ea.preflight(db, env)
        raise AssertionError("a broker with no ansible grant was accepted")
    except ea.EntitleAgentError as exc:
        assert "policy.yaml" in str(exc) and "Broker" in str(exc)
    finally:
        db.close()


# ── the policy grant ─────────────────────────────────────────────────────────

def test_ssh_and_winrm_targets_are_granted_their_own_ports():
    """Granting the union would let a playbook reach a Windows guest on 22 because a Linux
    one in the same POV needed it."""
    policy = pov_broker.render_policy(["10.0.0.1", "10.0.0.2"],
                                      ["10.0.0.1"], ["10.0.0.2"])
    # From `ansible:` onward. The discovery `targets:` block lists the same addresses with
    # the probe ports, and reading those would pass this test while the grant that
    # actually gates a playbook stayed wrong.
    lines = [ln.strip() for ln in policy.splitlines()]
    lines = lines[lines.index("ansible:"):]
    win = lines.index("- cidr: 10.0.0.1/32")
    lin = lines.index("- cidr: 10.0.0.2/32", win)
    assert "5985" in lines[win + 1] and "22" not in lines[win + 1]
    assert lines[lin + 1] == "ports: [22]"


def test_a_pov_with_only_a_linux_host_still_enables_config_management():
    """Before this slice `ansible:` was disabled unless the POV had a Windows guest, which
    would leave the Entitle agent install refused by a policy that fails closed."""
    policy = pov_broker.render_policy(["10.0.0.2"], [], ["10.0.0.2"])
    assert "enabled: true" in policy
    assert "ports: [22]" in policy


def test_a_pov_with_neither_still_renders_the_block_disabled():
    policy = pov_broker.render_policy(["10.0.0.2"], [], [])
    assert "ansible:" in policy
    assert "enabled: false" in policy


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_destroys_the_token_so_the_name_is_free_to_re_mint():
    db = d.SessionLocal()
    _patch_mint()
    seen = {}

    async def _fake_deregister(state, ctx=None):
        seen["state"] = state
        seen["ctx"] = ctx

    real = entitle_registration_service.deregister
    entitle_registration_service.deregister = _fake_deregister
    try:
        env, _a, _v, tenant = _ready(db)
        row = db.query(d.BeyondTrustTenant).filter(
            d.BeyondTrustTenant.id == tenant["id"]).first()
        asyncio.run(ea.ensure_token(db, env, bt_tenant_service.to_tenant(row)))
        line = asyncio.run(ea.teardown(db, env))
        assert seen.get("ctx") is not None
        assert "Destroyed" in line
        assert not ea.has_token(env)
        assert not ea.agent_token_name(env)
    finally:
        entitle_registration_service.deregister = real
        _restore_mint()
        db.close()


def test_a_failed_destroy_reports_the_leftover_rather_than_blocking_the_teardown():
    """A POV that cannot be destroyed because a customer's API was briefly down is worse
    than a token an operator has to retire by hand."""
    db = d.SessionLocal()
    _patch_mint()

    async def _boom(state, ctx=None):
        raise RuntimeError("entitle said no")

    real = entitle_registration_service.deregister
    entitle_registration_service.deregister = _boom
    try:
        env, _a, _v, tenant = _ready(db)
        row = db.query(d.BeyondTrustTenant).filter(
            d.BeyondTrustTenant.id == tenant["id"]).first()
        asyncio.run(ea.ensure_token(db, env, bt_tenant_service.to_tenant(row)))
        line = asyncio.run(ea.teardown(db, env))
        assert "Could not destroy" in line and "by hand" not in line
        assert "delete it in the Entitle tenant" in line
        # Local state still goes, or a retry would think a token it cannot reach is fine.
        assert not ea.has_token(env)
    finally:
        entitle_registration_service.deregister = real
        _restore_mint()
        db.close()


def test_teardown_is_a_no_op_on_a_pov_that_never_had_one():
    db = d.SessionLocal()
    env = _env(db)
    assert "No Entitle agent" in asyncio.run(ea.teardown(db, env))
    db.close()


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
