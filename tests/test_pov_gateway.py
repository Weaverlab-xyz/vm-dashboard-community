"""The POV's BeyondTrust Gateway — the refusals, and what may not cross the wire.

This is the first slice that uses both of the ones before it at once: an agent inside the
customer's environment, and a named PRA tenant to register into. So most of what is worth
pinning is what it refuses, and what it declines to carry:

  * **The deploy key is never in the job.** Not in the row's metadata, not in the signed
    envelope, and above all not in the lab platform's ``user_data`` — unlike an enrolment
    code it is neither single-use nor short-lived, because every node registered with it
    joins the same Gateway.
  * **The envelope carries no free-form string.** No image, no container name, no command.
    A hostile dashboard cannot name a thing for the agent to run, which is the same rule
    ``agent_job_meta`` enforces for discovery.
  * **An explicit tenant never falls back.** Installing a customer's Gateway into whatever
    tenant happens to be the default is the failure the registry exists to prevent.
  * **A removal needs less than an install.** A POV whose policy was narrowed, or whose
    key was cleared, must still be tearable-down.
  * **The status check asks whether a NODE is connected**, not whether the name exists —
    a Gateway is a cluster and PRA keeps the dead node when a broker VM is rebuilt.

Uses a real SQLite database. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_gateway.py
"""
import asyncio
import os
import pathlib
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-gateway")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_gateway_meta, agent_service,  # noqa: E402
                                    bt_tenant_service, pov_broker, pov_env_service,
                                    pov_gateway)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _agent(db, *, version="2.4.0", reported=("agent_discover", "agent_gateway"),
           seen=True):
    row, _code = agent_service.create_agent(db, name=_name("brk"), created_by="t")
    row.public_key = "fake-key"
    row.agent_version = version
    row.reported_job_types = __import__("json").dumps(list(reported))
    if seen:
        row.last_seen_at = __import__("datetime").datetime.utcnow()
    db.commit()
    return row


def _tenant(db):
    return bt_tenant_service.create(
        db, kind="pra", name=_name("pra"), base_url="acme.beyondtrustcloud.com",
        client_id="cid", secret="sekrit", created_by="t",
        options={"jump_group_name": "POV", "jumpoint_name": "gw"})


def _env(db, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE, **kw)
    db.add(env)
    db.commit()
    return env


def _ready(db):
    """A POV with everything the install needs, so a test can take one thing away."""
    agent = _agent(db)
    tenant = _tenant(db)
    env = _env(db, broker_agent_id=agent.id, pra_tenant_id=tenant["id"],
               gateway_name="pov-gw")
    pov_gateway.set_deploy_key(env, "dk-123456")
    return env, agent, tenant


# ── what may cross the wire ──────────────────────────────────────────────────

def test_the_envelope_carries_no_free_form_string():
    """A hostile dashboard must not be able to name a thing for the agent to run. Same
    rule agent_job_meta enforces for discovery, checked the same way."""
    payload = agent_gateway_meta.envelope_payload(
        {"gateway_action": "install", "timeout_s": 120,
         "image": "evil/image", "command": "rm -rf /", "container": "x"})
    assert set(payload) == set(agent_gateway_meta.GATEWAY_META_KEYS)
    assert payload["gateway_action"] in agent_gateway_meta.VALID_ACTIONS
    assert isinstance(payload["timeout_s"], int)
    for value in payload.values():
        assert not isinstance(value, str) or value in agent_gateway_meta.VALID_ACTIONS


def test_the_description_stays_off_the_envelope():
    """It is the one free-form string on the row, and it is for the Jobs page."""
    meta = agent_gateway_meta.gateway_meta({"gateway_action": "install"},
                                           description="anything at all")
    assert meta["description"] == "anything at all"
    assert "description" not in agent_gateway_meta.envelope_payload(meta)


def test_an_unknown_action_normalises_to_install_not_remove():
    """Falling back is deliberate so an old row stays actionable — and the direction
    matters: install is idempotent, whereas upgrading a typo to `remove` would delete a
    working Gateway."""
    assert agent_gateway_meta.normalize(
        {"gateway_action": "destroy"})["gateway_action"] == "install"
    assert agent_gateway_meta.check({"gateway_action": "destroy"})


def test_the_deploy_key_is_not_in_the_job_metadata():
    """It lands in the database and in the signed envelope, and a PRA deploy key is
    neither single-use nor short-lived."""
    db = d.SessionLocal()
    env, _agent_row, _t = _ready(db)
    job = pov_gateway.queue(db, env, created_by="tester")
    blob = repr(job.metadata_dict)
    assert "dk-123456" not in blob
    assert job.metadata_dict["environment_id"] == env.id
    db.close()


def test_the_seal_ref_matches_the_agent_build():
    """It is part of the sealed envelope's AAD, so a mismatch fails as 'did not
    authenticate' rather than as an obvious typo."""
    agent_src = (pathlib.Path(_ROOT) / "runners" / "agent" / "agent.py").read_text(
        encoding="utf-8")
    assert f'GATEWAY_SEAL_REF = "{pov_gateway.SEAL_REF}"' in agent_src
    assert f'GATEWAY_CONTAINER = "{pov_gateway.CONTAINER_NAME}"' in agent_src


def test_the_agent_build_is_new_enough_for_the_version_the_dashboard_demands():
    """The dashboard refuses to QUEUE below MIN_GATEWAY_VERSION, so shipping an agent
    below it would mean nobody could ever run this — the same pairing
    test_agent_ansible_run pins for Config Management."""
    agent_src = (pathlib.Path(_ROOT) / "runners" / "agent" / "agent.py").read_text(
        encoding="utf-8")
    version = agent_src.split('AGENT_VERSION = "', 1)[1].split('"', 1)[0]
    major, minor = (int(p) for p in version.split(".")[:2])
    assert (major, minor) >= agent_service.MIN_GATEWAY_VERSION, version


def test_the_agent_registers_the_handler_in_its_closed_table():
    """HANDLERS is a dict lookup precisely so a name off the wire cannot reach a function
    nobody listed. A verb the dashboard queues and the agent has never heard of leases and
    then fails on the far side."""
    agent_src = (pathlib.Path(_ROOT) / "runners" / "agent" / "agent.py").read_text(
        encoding="utf-8")
    assert '"agent_gateway": run_gateway' in agent_src


# ── preflight refusals ───────────────────────────────────────────────────────

def test_a_pov_with_no_broker_is_refused_with_the_button_to_press():
    db = d.SessionLocal()
    env = _env(db, gateway_name="gw")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("a POV with no broker was accepted")
    except pov_gateway.GatewayInstallError as exc:
        assert "Press Broker" in str(exc)
    finally:
        db.close()


def test_an_offline_broker_is_refused_before_a_job_is_queued():
    """The job would lease nowhere and sit queued, which reads as a dashboard fault."""
    db = d.SessionLocal()
    agent = _agent(db, seen=False)
    env = _env(db, broker_agent_id=agent.id, gateway_name="gw")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("an offline broker was accepted")
    except pov_gateway.GatewayInstallError as exc:
        assert "offline" in str(exc)
    finally:
        db.close()


def test_an_old_broker_is_refused_with_the_remedy_naming_the_button():
    """On a POV the policy is generated, so 'edit policy.yaml' would mean SSH into a
    customer's environment. The remedy is the Broker button."""
    db = d.SessionLocal()
    agent = _agent(db, version="2.3.1")
    env = _env(db, broker_agent_id=agent.id, gateway_name="gw")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("an old agent was accepted")
    except pov_gateway.GatewayInstallError as exc:
        assert "2.4" in str(exc) and "Broker" in str(exc)
    finally:
        db.close()


def test_a_broker_whose_policy_predates_the_grant_is_refused():
    """Covers what a version number cannot: a current agent enrolled before slice 5."""
    db = d.SessionLocal()
    agent = _agent(db, reported=("agent_discover",))
    env = _env(db, broker_agent_id=agent.id, gateway_name="gw")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("a policy without the grant was accepted")
    except pov_gateway.GatewayInstallError as exc:
        assert "policy.yaml predates" in str(exc) and "Broker" in str(exc)
    finally:
        db.close()


def test_a_pov_with_no_deploy_key_is_refused_naming_the_tenant():
    db = d.SessionLocal()
    agent = _agent(db)
    tenant = _tenant(db)
    env = _env(db, broker_agent_id=agent.id, pra_tenant_id=tenant["id"],
               gateway_name="gw")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("a POV with no key was accepted")
    except pov_gateway.GatewayInstallError as exc:
        assert tenant["name"] in str(exc)
    finally:
        db.close()


def test_a_pov_with_no_gateway_name_is_refused():
    db = d.SessionLocal()
    agent = _agent(db)
    tenant = _tenant(db)
    env = _env(db, broker_agent_id=agent.id, pra_tenant_id=tenant["id"])
    pov_gateway.set_deploy_key(env, "k")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("a POV with no Gateway name was accepted")
    except pov_gateway.GatewayInstallError as exc:
        assert "names no Gateway" in str(exc)
    finally:
        db.close()


def test_a_deleted_tenant_is_an_error_not_a_fall_back_to_the_default():
    """The failure the whole registry exists to prevent: a customer's Gateway installed
    into whatever tenant happened to be the default."""
    db = d.SessionLocal()
    agent = _agent(db)
    default = _tenant(db)
    bt_tenant_service.set_default(db, default["id"])
    env = _env(db, broker_agent_id=agent.id, pra_tenant_id="gone-tenant-id",
               gateway_name="gw")
    pov_gateway.set_deploy_key(env, "k")
    try:
        pov_gateway.preflight(db, env)
        raise AssertionError("a missing tenant fell back to the default")
    except pov_gateway.GatewayInstallError as exc:
        assert "no longer exists" in str(exc)
    finally:
        db.close()


# ── queueing ─────────────────────────────────────────────────────────────────

def test_the_job_is_queued_on_the_broker_agent_not_the_local_worker():
    """agent_id forces status=queued, which is what keeps the local runner's claim query
    from racing the agent's lease."""
    db = d.SessionLocal()
    env, agent, _t = _ready(db)
    job = pov_gateway.queue(db, env, created_by="tester")
    assert job.job_type == "agent_gateway"
    assert job.agent_id == agent.id
    assert job.status == "queued"
    db.close()


def test_a_removal_needs_less_than_an_install():
    """A POV whose key was cleared, or whose tenant was deleted, must still be
    tearable-down — that is the state that most needs tearing down."""
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id, gateway_name="gw")  # no tenant, no key
    job = pov_gateway.queue(db, env, action="remove", created_by="t")
    assert job.metadata_dict["gateway_action"] == "remove"
    db.close()


def test_the_agent_job_type_is_registered_and_not_run_locally():
    """AGENT_JOB_TYPES must stay disjoint from the local worker's HANDLED_TYPES, or the
    two executors race the same row."""
    from web_dashboard import jobs_worker
    assert "agent_gateway" in agent_service.AGENT_JOB_TYPES
    assert "agent_gateway" not in jobs_worker.HANDLED_TYPES


# ── the broker policy ────────────────────────────────────────────────────────

def test_the_generated_policy_grants_the_gateway_and_names_the_image():
    policy = pov_broker.render_policy(["10.0.0.5"])
    assert "- agent_gateway" in policy
    assert "gateway:" in policy and "privileged: true" in policy
    assert pov_broker.GATEWAY_IMAGE in policy


def test_the_bootstrap_mounts_the_docker_socket():
    """The Gateway is a sibling container, so the broker's agent needs the socket. That
    is root on the broker VM and is a deliberate line, not an inherited default."""
    script = pov_broker.render_bootstrap(
        env_name="p", dashboard_url="https://x.test", enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert "/var/run/docker.sock:/var/run/docker.sock" in script


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_clears_the_stored_key_even_when_the_removal_cannot_be_queued():
    """The container goes with the environment either way; a customer's credential left
    behind in this database is the part that actually matters."""
    db = d.SessionLocal()
    env = _env(db, gateway_name="gw")           # no broker, so nothing can be queued
    pov_gateway.set_deploy_key(env, "leftover")
    assert pov_gateway.has_deploy_key(env)
    pov_gateway.teardown(db, env)
    assert not pov_gateway.has_deploy_key(env)
    assert env.gateway_name is None
    db.close()


def test_teardown_queues_a_removal_when_there_is_a_broker():
    db = d.SessionLocal()
    env, agent, _t = _ready(db)
    line = pov_gateway.teardown(db, env)
    assert "Queued removal" in line
    job = (db.query(d.Job).filter(d.Job.agent_id == agent.id,
                                 d.Job.job_type == "agent_gateway").first())
    assert job is not None and job.metadata_dict["gateway_action"] == "remove"
    db.close()


# ── status ───────────────────────────────────────────────────────────────────

def _with_gateways(rows):
    """Swap the PRA read for a canned answer."""
    from web_dashboard.services import pra_tenant_api

    async def fake(tenant):
        return rows
    original = pra_tenant_api.list_gateways
    pra_tenant_api.list_gateways = fake
    return original


def _restore_gateways(original):
    from web_dashboard.services import pra_tenant_api
    pra_tenant_api.list_gateways = original


def test_status_asks_whether_a_node_is_connected_not_whether_the_name_exists():
    """A Gateway is a cluster. Rebuilding a broker VM adds a node and PRA parks the dead
    one, so the name is present either way."""
    db = d.SessionLocal()
    env, _a, _t = _ready(db)
    original = _with_gateways([{"id": 1, "name": "pov-gw", "connected": False,
                                "nodes": 2}])
    try:
        got = asyncio.run(pov_gateway.status(db, env))
    finally:
        _restore_gateways(original)
        db.close()
    assert got["state"] == "disconnected"
    assert got["nodes"] == 2
    assert "dead node" in got["detail"]


def test_an_unreported_connected_field_is_unknown_never_disconnected():
    """Which field an appliance reports varies by version, and telling an operator their
    Gateway is down because we did not recognise a key is worse than saying nothing."""
    db = d.SessionLocal()
    env, _a, _t = _ready(db)
    original = _with_gateways([{"id": 1, "name": "pov-gw", "connected": None, "nodes": 1}])
    try:
        got = asyncio.run(pov_gateway.status(db, env))
    finally:
        _restore_gateways(original)
        db.close()
    assert got["state"] == "unknown"


def test_a_name_pra_does_not_know_is_missing_and_says_which_tenant():
    db = d.SessionLocal()
    env, _a, tenant = _ready(db)
    original = _with_gateways([{"id": 1, "name": "something-else", "connected": True,
                                "nodes": 1}])
    try:
        got = asyncio.run(pov_gateway.status(db, env))
    finally:
        _restore_gateways(original)
        db.close()
    assert got["state"] == "missing"
    assert tenant["name"] in got["detail"]


def test_a_pov_with_no_gateway_name_has_no_status_to_report():
    db = d.SessionLocal()
    env = _env(db)
    got = asyncio.run(pov_gateway.status(db, env))
    db.close()
    assert got["state"] == "none"


# ── the describe used by the list endpoint ───────────────────────────────────

def test_describe_makes_no_network_call_and_never_leaks_the_key():
    """One live appliance read per row would make the POV page as slow as the slowest
    customer's network."""
    db = d.SessionLocal()
    env, _a, _t = _ready(db)
    original = _with_gateways([])   # any call would return the wrong shape and blow up
    try:
        got = pov_gateway.describe(db, env)
    finally:
        _restore_gateways(original)
        db.close()
    assert got == {"gateway_name": "pov-gw", "gateway_has_key": True,
                   "gateway_ready": True}
    assert "dk-123456" not in repr(got)


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
