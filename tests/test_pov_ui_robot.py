"""The POV's UI robot: what the dashboard refuses, what may not cross the wire, and the
agent's side of the same contract.

Pinned here (design: docs/profiles/pov/design/ui-robot.md):

  * **The envelope carries two scalars.** No image, no URL, no credential, no environment id.
  * **The logins are write-only and leave only through the sealed bundle**, derived from the
    job row. They are never in job metadata.
  * **The robot gets its own HostConfig constant**, with nothing in it from the job, and the
    SVID volume it mounts is named by policy, never by the job.
  * **Every refusal says nothing was queued.**

Uses a real SQLite database for the dashboard half; the agent half imports agent.py and
stubs the Docker Engine. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_ui_robot.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-ui-robot")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, agent_ui_robot_meta,  # noqa: E402
                                    bt_tenant_service, pov_broker, pov_env_service,
                                    pov_ui_robot)

_AGENT_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")
_spec = importlib.util.spec_from_file_location("agent_ui_robot_t", _AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _agent(db, *, version="2.18.0", reported=("agent_discover", "agent_ui_robot")):
    import datetime
    row, _code = agent_service.create_agent(db, name=_name("brk"), created_by="t")
    row.public_key = "fake-key"
    row.agent_version = version
    row.reported_job_types = json.dumps(list(reported))
    row.last_seen_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    db.commit()
    return row


def _tenant(db, kind):
    return bt_tenant_service.create(
        db, kind=kind, name=_name(kind.replace("_", "-")),
        base_url=f"{kind[:3]}.example.com/BeyondTrust/api/x",
        client_id="cid", secret="sekrit", created_by="t",
        options={"jump_group_name": "POV", "jumpoint_name": "gw"} if kind == "pra" else {})


def _env(db, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE, **kw)
    db.add(env)
    db.commit()
    return env


def _ready(db):
    """A POV with everything the installer task needs, so a test can take one thing away."""
    row = _agent(db)
    ps, pra = _tenant(db, "password_safe"), _tenant(db, "pra")
    env = _env(db, broker_agent_id=row.id, ps_tenant_id=ps["id"], pra_tenant_id=pra["id"])
    pov_ui_robot.set_credentials(env, {"ps_user": "svc-ps", "ps_password": "ps-pw-123456",
                                       "ps_totp_secret": "TOTPSECRET234567"})
    return env, row


def _refusal(fn):
    try:
        fn()
    except pov_ui_robot.UiRobotError as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


# ── what may cross the wire ──────────────────────────────────────────────────

def test_the_envelope_carries_two_scalars_and_no_string_of_substance():
    payload = agent_ui_robot_meta.envelope_payload(
        {"task": "installer", "timeout_s": 900, "image": "evil/image", "command": "rm -rf /",
         "environment_id": "env-1", "ps_password": "hunter2"})
    assert set(payload) == set(agent_ui_robot_meta.UI_ROBOT_META_KEYS)
    assert payload["task"] in agent_ui_robot_meta.VALID_TASKS
    assert isinstance(payload["timeout_s"], int)


def test_the_description_stays_off_the_envelope():
    meta = agent_ui_robot_meta.ui_robot_meta({"task": "installer"}, description="free text")
    assert meta["description"] == "free text"
    assert "description" not in agent_ui_robot_meta.envelope_payload(meta)


def test_an_unknown_task_falls_back_to_the_smaller_one_and_is_refused_at_queue_time():
    assert agent_ui_robot_meta.normalize({"task": "wipe"})["task"] == "installer"
    assert agent_ui_robot_meta.check({"task": "wipe"})


def test_the_timeout_is_clamped():
    assert agent_ui_robot_meta.normalize({"timeout_s": 1})["timeout_s"] == 60
    assert agent_ui_robot_meta.normalize({"timeout_s": 10 ** 9})["timeout_s"] == 3600


# ── the preflight ────────────────────────────────────────────────────────────

def test_a_ready_pov_queues_a_job_with_no_credential_in_it():
    db = d.SessionLocal()
    env, row = _ready(db)
    job = pov_ui_robot.queue(db, env, task="installer", created_by="tester")
    assert job.job_type == "agent_ui_robot"
    blob = repr(job.metadata_dict)
    for secret in ("ps-pw-123456", "TOTPSECRET234567", "svc-ps"):
        assert secret not in blob
    assert job.metadata_dict["environment_id"] == env.id
    assert job.metadata_dict["task"] == "installer"
    db.close()


def test_an_old_agent_is_refused_with_the_rebuild_remedy():
    db = d.SessionLocal()
    env, row = _ready(db)
    row.agent_version = "2.17.0"
    db.commit()
    text = _refusal(lambda: pov_ui_robot.preflight(db, env, "installer"))
    assert "Rebuild control node" in text and "Nothing was queued." in text
    db.close()


def test_a_policy_that_predates_the_grant_is_refused():
    db = d.SessionLocal()
    env, row = _ready(db)
    row.reported_job_types = json.dumps(["agent_discover"])
    db.commit()
    text = _refusal(lambda: pov_ui_robot.preflight(db, env, "installer"))
    assert "predates" in text and "Nothing was queued." in text
    db.close()


def test_a_pov_with_no_control_node_is_refused():
    db = d.SessionLocal()
    env = _env(db)
    text = _refusal(lambda: pov_ui_robot.preflight(db, env, "installer"))
    assert "Nothing was queued." in text
    db.close()


def test_missing_logins_are_named_and_never_echoed():
    db = d.SessionLocal()
    env, _row = _ready(db)
    pov_ui_robot.set_credentials(env, {"ps_password": ""})
    text = _refusal(lambda: pov_ui_robot.preflight(db, env, "installer"))
    assert "ps_password" in text and "Nothing was queued." in text
    assert "ps-pw-123456" not in text
    db.close()


def test_the_integration_needs_pra_and_the_ps_api_key():
    db = d.SessionLocal()
    env, _row = _ready(db)
    text = _refusal(lambda: pov_ui_robot.preflight(db, env, "integration"))
    for field in ("pra_user", "pra_password", "ps_api_key", "ps_runas_user"):
        assert field in text
    db.close()


def test_an_unknown_task_is_refused():
    db = d.SessionLocal()
    env, _row = _ready(db)
    assert "Nothing was queued." in _refusal(lambda: pov_ui_robot.preflight(db, env, ""))
    assert _refusal(lambda: pov_ui_robot.preflight(db, env, "wipe"))
    db.close()


# ── the logins ───────────────────────────────────────────────────────────────

def test_stored_logins_are_reported_as_flags_never_values():
    db = d.SessionLocal()
    env, _row = _ready(db)
    state = pov_ui_robot.credential_state(env)
    assert state["ps_user"] is True and state["pra_user"] is False
    assert all(isinstance(v, bool) for v in state.values())
    db.close()


def test_an_unknown_credential_field_is_refused_not_dropped():
    db = d.SessionLocal()
    env, _row = _ready(db)
    assert "bogus" in _refusal(lambda: pov_ui_robot.set_credentials(env, {"bogus": "x"}))
    db.close()


def test_teardown_clears_the_logins():
    from web_dashboard.services import pov_resource_broker
    db = d.SessionLocal()
    env, _row = _ready(db)
    pov_resource_broker.teardown(db, env)
    assert not any(pov_ui_robot.credential_state(env).values())
    db.close()


def test_the_installer_bundle_holds_ps_logins_only():
    db = d.SessionLocal()
    env, _row = _ready(db)
    job = pov_ui_robot.queue(db, env, task="installer", created_by="t")
    bundle, scrub = pov_ui_robot.bundle_for_job(db, job, "https://dash.example.com/")
    assert bundle["PS_PASSWORD"] == "ps-pw-123456"
    assert bundle["DASHBOARD_URL"] == "https://dash.example.com"
    assert bundle["POV_ENV_ID"] == env.id
    from urllib.parse import urlsplit
    assert urlsplit(bundle["PS_URL"]).hostname == "pas.example.com"
    assert urlsplit(bundle["PS_URL"]).scheme == "https"
    assert "/BeyondTrust" not in bundle["PS_URL"]
    assert not any(k.startswith("PRA_") for k in bundle)
    assert "ps-pw-123456" in scrub and "TOTPSECRET234567" in scrub
    db.close()


def test_the_integration_bundle_adds_pra_and_the_api_key():
    db = d.SessionLocal()
    env, _row = _ready(db)
    pov_ui_robot.set_credentials(env, {"pra_user": "svc-pra", "pra_password": "pra-pw-123456",
                                       "ps_api_key": "apikey-123456", "ps_runas_user": "runas"})
    job = pov_ui_robot.queue(db, env, task="integration", created_by="t")
    bundle, scrub = pov_ui_robot.bundle_for_job(db, job, "https://dash.example.com")
    assert bundle["PRA_PASSWORD"] == "pra-pw-123456"
    assert bundle["PS_API_KEY"] == "apikey-123456" and bundle["PS_RUNAS_USER"] == "runas"
    assert "apikey-123456" in scrub and "pra-pw-123456" in scrub
    db.close()


def test_the_bundle_is_derived_from_the_job_row_alone():
    """A job naming no POV, or a gone one, gets nothing, so a stolen agent identity cannot
    ask for another POV's logins."""
    db = d.SessionLocal()
    env, _row = _ready(db)
    job = pov_ui_robot.queue(db, env, task="installer", created_by="t")
    meta = dict(job.metadata_dict)
    meta["environment_id"] = ""
    job.metadata_dict = meta
    assert _refusal(lambda: pov_ui_robot.bundle_for_job(db, job, "https://x"))
    db.close()


def test_the_seal_ref_matches_the_agent_build():
    src = pathlib.Path(_AGENT_PATH).read_text(encoding="utf-8")
    assert f'UI_ROBOT_SEAL_REF = "{pov_ui_robot.SEAL_REF}"' in src


# ── the broker's policy and bootstrap ────────────────────────────────────────

def test_the_policy_grants_the_robot_only_when_asked():
    plain = pov_broker.render_policy(["10.0.0.5"])
    assert "agent_ui_robot" not in plain and "ui_robot:" not in plain
    robot = pov_broker.render_policy(["10.0.0.5"], ui_robot=True)
    assert "  - agent_ui_robot" in robot
    assert f"  image: {pov_broker.UI_ROBOT_IMAGE}" in robot
    assert f"  svid_volume: {pov_broker.GUEST_JWT_VOLUME}" in robot
    assert robot.isascii()


def test_the_helper_mints_a_second_svid_for_the_token_endpoint():
    spire = {"server_address": "spire.example.com", "server_port": 8081,
             "trust_domain": "example.org", "join_token": "abcdef12-3456-7890",
             "bootstrap_pem": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"}
    block = pov_broker._render_spire_block(spire, "https://dash.example.com")
    assert 'jwt_audience="https://dash.example.com/api/agent/attest"' in block
    assert 'jwt_audience="https://dash.example.com/api/oauth/token"' in block
    assert 'jwt_svid_file_name="oauth.jwt"' in block


def test_the_robot_is_a_signed_published_image_and_a_granted_type():
    wf = pathlib.Path(_ROOT, ".github", "workflows", "publish-images.yml").read_text(
        encoding="utf-8")
    entry = wf.split("image: chrweav/ui-robot", 1)[1].split("- image:", 1)[0]
    assert "context: runners/ui-robot" in entry and "sign: true" in entry
    assert "agent_ui_robot" in agent_service.AGENT_JOB_TYPES
    assert "agent_ui_robot" not in pov_broker.BROKER_JOB_TYPES


# ── the agent's side ─────────────────────────────────────────────────────────

class _Policy:
    ui_robot_enabled = True
    ui_robot_image_name = "chrweav/ui-robot:test"
    ui_robot_svid_volume = "dashboard_agent_spiffe_jwt"
    ui_robot_network = "bridge"
    ui_robot_max_runtime_minutes = 30
    ansible_enabled = False
    ansible_network = "bridge"
    ansible_max_runtime_minutes = 20

    def ui_robot_image(self):
        return self.ui_robot_image_name


class _Engine:
    def __init__(self):
        self.created = None
        self.archived = None

    def __call__(self, method, path, body=None, timeout=60.0, raw=False):
        if path == "/containers/create":
            self.created = body
            return 201, {"Id": "c0ffee"}
        if path.endswith("/start"):
            return 204, {}
        if path.endswith("/wait"):
            return 200, {"StatusCode": 0}
        return 200, {}


def _run_robot(task="installer", bundle=None, mode="enforce"):
    engine = _Engine()
    originals = (agent._engine, agent._archive_put, agent._stream_logs, agent.MODE)
    agent._engine = engine
    agent._archive_put = lambda c, dest, files, owner=None: setattr(
        engine, "archived", (dest, dict(files), owner))
    agent._stream_logs = lambda *a, **k: None
    agent.MODE = mode
    fetched = []

    class _Dash:
        def ui_robot_bundle(self, job_id):
            fetched.append(job_id)
            return bundle if bundle is not None else {"ROBOT_TASK": task, "PS_PASSWORD": "x"}

    try:
        result = agent.run_ui_robot({"task": task, "timeout_s": 600}, _Policy(),
                                    lambda _m: None, lambda: False, "job-1", _Dash())
    finally:
        agent._engine, agent._archive_put, agent._stream_logs, agent.MODE = originals
    return engine, result, fetched


def test_the_robot_hostconfig_is_a_constant_a_browser_can_run_in():
    host = agent._UI_ROBOT_HOSTCONFIG
    assert host["Privileged"] is False and host["Binds"] == []
    assert host["CapDrop"] == ["ALL"] and host["ReadonlyRootfs"] is True
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["ShmSize"] >= 512 * 1024 * 1024, "Chromium crashes on a 64 MB /dev/shm"
    assert host["Memory"] > agent._ANSIBLE_MEMORY


def test_the_robot_spec_comes_from_policy_and_constants():
    engine, result, fetched = _run_robot()
    spec = engine.created
    assert spec["Image"] == "chrweav/ui-robot:test"
    host = spec["HostConfig"]
    assert host["NetworkMode"] == "bridge" and host["Privileged"] is False
    svid = [m for m in host["Mounts"] if m.get("Source")]
    assert svid == [{"Type": "volume", "Source": "dashboard_agent_spiffe_jwt",
                     "Target": "/run/spiffe/jwt", "ReadOnly": True}]
    assert spec["User"] == f"{agent._UI_ROBOT_UID}:{agent._UI_ROBOT_UID}"
    assert fetched == ["job-1"] and result == {"task": "installer", "ok": True}


def test_the_logins_are_a_file_not_the_environment():
    engine, _result, _f = _run_robot(bundle={"ROBOT_TASK": "installer", "PS_PASSWORD": "pw-zzz"})
    env = " ".join(engine.created["Env"])
    assert "pw-zzz" not in env and "PS_PASSWORD" not in env
    dest, files, owner = engine.archived
    assert dest == "/opt/job" and owner == (agent._UI_ROBOT_UID, agent._UI_ROBOT_UID)
    assert b"pw-zzz" in files["opt/job/bundle.json"]
    assert "ROBOT_BUNDLE_FILE=/opt/job/bundle.json" in engine.created["Env"]


def test_audit_mode_fetches_nothing_and_starts_nothing():
    engine, result, fetched = _run_robot(mode="audit")
    assert engine.created is None and fetched == [] and result["audit"] is True


def test_a_bundle_for_another_task_is_refused():
    try:
        _run_robot(task="installer", bundle={"ROBOT_TASK": "integration"})
    except agent.PolicyRefusal:
        return
    raise AssertionError("a bundle for a different task must be refused")


def test_an_unknown_task_never_reaches_the_dashboard():
    try:
        _run_robot(task="wipe")
    except agent.PolicyRefusal:
        return
    raise AssertionError("expected a refusal")


def test_the_policy_block_is_parsed_and_a_bad_volume_name_is_fatal():
    class P:
        pass
    pol = P()
    agent.parse_ui_robot(pol, {"enabled": True, "image": "i:1", "svid_volume": "vol_1"})
    assert pol.ui_robot_enabled and pol.ui_robot_svid_volume == "vol_1"
    for bad in ("/etc:/x", "a b", "../x", "v;rm"):
        try:
            agent.parse_ui_robot(P(), {"enabled": True, "image": "i", "svid_volume": bad})
        except agent.AgentFatal:
            continue
        raise AssertionError(f"{bad!r} must not be accepted as a volume name")


def test_the_robot_has_its_own_slot_and_never_runs_serially():
    original = getattr(agent.POLICY, "job_types", None) if agent.POLICY else None
    slots = agent.JobSlots(1, robot=True)
    assert slots.serial is False
    assert agent.JobSlots(1, robot=False).serial is True
    assert slots._kind("agent_ui_robot") == "robot"
    assert slots._kind("agent_discover") == "other"
    assert original is None or original == agent.POLICY.job_types


def test_the_robot_image_is_refused_when_policy_does_not_enable_it():
    pol = agent.Policy.__new__(agent.Policy)
    pol.ui_robot_enabled, pol.ui_robot_image_name = False, ""
    try:
        agent.Policy.ui_robot_image(pol)
    except agent.PolicyRefusal as exc:
        assert "Rebuild control node" in str(exc)
        return
    raise AssertionError("expected a refusal")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
