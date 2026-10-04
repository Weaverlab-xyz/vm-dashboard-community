"""An ECS / Cloud Run Ansible runner collects its run's credential from the dashboard.

docs/design/dashboard-workload-identity.md, Slice 5 (services/runner_credential,
services/runner_fetch). Pinned:

  * the token is single-use, burnt even by a failed proof, and expires in ten minutes;
    the grant is gone after the run; only its hash is stored, the values encrypted;
  * ECS: the proof must be a presigned GetCallerIdentity to EXACTLY a regional STS host —
    checked before anything is sent — signing this run's binding, and STS must name the
    configured task role in the configured account;
  * Cloud Run: Google must verify an ID token for the configured service account, with
    a verified email and the binding audience;
  * the reply key is checked before the proof; the values come back sealed in
    agent_sealing's format and open with the container's own opener;
  * the container's SigV4 matches botocore's; the whole fetch round-trips into a 0600 vars
    file merged with what was there;
  * the token rides the per-run override, never the ECS task definition; the script does;
  * the route answers no-store, audits both outcomes, and never echoes a token or value.

Run: python tests/test_runner_credential.py   (or under pytest)
"""
import base64
import datetime as _dt
import json
import os
import stat
import sys
import tempfile
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="runner-cred-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-runner-credential")

try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.database import (AuditLog, Base, RunnerCredentialGrant,  # noqa: E402
                                    SessionLocal, engine, get_db)
from web_dashboard.services import (agent_sealing, agent_service,  # noqa: E402
                                    cloud_ansible_secrets, config_service,
                                    runner_credential as rc, runner_fetch as rf)

Base.metadata.create_all(bind=engine)

AUD = "https://agents.rc.test"
ROLE = "arn:aws:iam::123456789012:role/runners/vm-dashboard-ansible"
SA = "ansible-runner@proj.iam.gserviceaccount.com"
VALUES = {"ansible_password": "Pw!Managed#2026", "ansible_become_password": "Become#1"}


def _configure():
    config_service.set(agent_service.AUDIENCE_CONFIG, AUD)
    config_service.set(rc.SETTING, "1")
    config_service.set(rc.ECS_TASK_ROLE, ROLE)
    config_service.set(rc.GCP_RUNNER_SA, SA)


_configure()


def _issue(runner="ecs", job="job-1", now=None):
    db = SessionLocal()
    try:
        return rc.issue(db, job_id=job, runner=runner, values=VALUES, now=now)
    finally:
        db.close()


def _sts_ok(arn="arn:aws:sts::123456789012:assumed-role/vm-dashboard-ansible/task-1"):
    calls = []

    def post(url, headers, body):
        calls.append((url, headers, body))
        return 200, {"GetCallerIdentityResponse": {"GetCallerIdentityResult": {
            "Arn": arn, "Account": "123456789012"}}}
    post.calls = calls
    return post


def _ecs_proof(token, region="us-east-1"):
    return rf.sts_proof(region, "AKIDEXAMPLE", "secret", "session-tok", token)


def _redeem(token, proof, *, reply=None, sts=None, google=None, now=None):
    priv, pub = rf.keypair()
    db = SessionLocal()
    try:
        env, meta = rc.redeem(db, token=token, proof=proof, reply_key=reply or pub, now=now,
                              sts_post=sts or _sts_ok(),
                              google_verify=google or (lambda t, a: {}))
    finally:
        db.close()
    return priv, env, meta


def _refused(fn, why):
    try:
        fn()
    except rc.RunnerCredentialError as exc:
        assert why in str(exc), (why, str(exc))
        return
    raise AssertionError(f"accepted; expected a refusal naming {why!r}")


# ── the token ─────────────────────────────────────────────────────────────────

def test_ecs_round_trip_opens_with_the_containers_opener():
    token = _issue(job="job-ecs")
    priv, env, meta = _redeem(token, _ecs_proof(token))
    assert meta == {"job_id": "job-ecs", "runner": "ecs",
                    "identity": "arn:aws:sts::123456789012:assumed-role/vm-dashboard-ansible/task-1"}
    out = rf.open_sealed(priv, env, agent_id="runner:ecs", audience=AUD, job_id="job-ecs",
                         ref=rf.REF)
    assert json.loads(out) == VALUES


def test_only_the_hash_is_stored_and_the_values_are_encrypted():
    token = _issue(job="job-store")
    db = SessionLocal()
    try:
        g = db.query(RunnerCredentialGrant).filter(RunnerCredentialGrant.job_id == "job-store").one()
        assert g.token_hash != token and token not in g.token_hash
        assert "Pw!Managed" not in g.values_enc
    finally:
        db.close()


def test_single_use_and_a_failed_proof_still_burns_it():
    token = _issue()
    _redeem(token, _ecs_proof(token))
    _refused(lambda: _redeem(token, _ecs_proof(token)), "already used")
    token = _issue()
    _refused(lambda: _redeem(token, _ecs_proof(token), sts=_sts_ok(
        "arn:aws:sts::123456789012:assumed-role/someone-else/x")), "configured runner")
    _refused(lambda: _redeem(token, _ecs_proof(token)), "already used")


def test_an_expired_token_is_refused():
    token = _issue(now=datetime.utcnow() - timedelta(minutes=11))
    _refused(lambda: _redeem(token, _ecs_proof(token)), "expired")


def test_the_reply_key_is_checked_before_the_proof():
    token = _issue()
    sts = _sts_ok()
    _refused(lambda: _redeem(token, _ecs_proof(token), reply="not-a-key", sts=sts),
             "reply key")
    assert sts.calls == [], "nothing may be sent to STS for a request that cannot be sealed"


def test_revoke_and_sweep_remove_grants():
    _issue(job="job-rev")
    db = SessionLocal()
    try:
        assert rc.revoke_for_job(db, "job-rev") == 1
        _issue(job="job-old", now=datetime.utcnow() - timedelta(hours=1))
        assert rc.sweep(db) >= 1
        assert not db.query(RunnerCredentialGrant).filter(
            RunnerCredentialGrant.job_id.in_(["job-rev", "job-old"])).count()
    finally:
        db.close()


# ── ECS proof ─────────────────────────────────────────────────────────────────

def test_ecs_refuses_every_other_identity():
    for arn, why in (
            ("arn:aws:sts::123456789012:assumed-role/other-role/s", "configured runner"),
            ("arn:aws:sts::999999999999:assumed-role/vm-dashboard-ansible/s", "configured runner"),
            ("arn:aws:iam::123456789012:user/vm-dashboard-ansible", "configured runner")):
        token = _issue()
        _refused(lambda: _redeem(token, _ecs_proof(token), sts=_sts_ok(arn)), why)


def test_ecs_never_sends_the_proof_anywhere_but_a_regional_sts_endpoint():
    for url in ("https://evil.example/", "https://sts.us-east-1.amazonaws.com.evil.example/",
                "https://user@sts.us-east-1.amazonaws.com/", "https://sts.us-east-1.amazonaws.com:8443/",
                "http://sts.us-east-1.amazonaws.com/", "https://sts.amazonaws.com/",
                "https://sts.us-east-1.amazonaws.com/x", "https://sts.us-east-1.amazonaws.com/?a=b",
                "https://sts.US-EAST-1.amazonaws.com/"):
        token = _issue()
        proof = _ecs_proof(token)
        proof["url"] = url
        sts = _sts_ok()
        _refused(lambda: _redeem(token, proof, sts=sts), "regional STS endpoint")
        assert sts.calls == [], url


def test_ecs_proof_must_be_bound_to_this_token_and_be_get_caller_identity():
    token, other = _issue(), _issue()
    _refused(lambda: _redeem(token, _ecs_proof(other)), "different token")
    token = _issue()
    proof = _ecs_proof(token)
    proof["body"] = "Action=AssumeRole&Version=2011-06-15"
    _refused(lambda: _redeem(token, proof), "GetCallerIdentity")
    token = _issue()
    proof = _ecs_proof(token)
    signed = proof["headers"]["authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert signed.endswith(rf.BINDING_HEADER), signed   # sorted, so it is the last one
    proof["headers"]["authorization"] = proof["headers"]["authorization"].replace(
        ";" + rf.BINDING_HEADER, "")
    _refused(lambda: _redeem(token, proof), "binding")


def test_only_signing_headers_are_forwarded_to_sts():
    token = _issue()
    proof = _ecs_proof(token)
    proof["headers"]["x-forwarded-for"] = "1.2.3.4"
    proof["headers"]["host"] = "evil.example"
    sts = _sts_ok()
    _redeem(token, proof, sts=sts)
    (_, headers, body), = sts.calls
    assert set(headers) <= {"authorization", "content-type", "x-amz-date",
                            "x-amz-security-token", rf.BINDING_HEADER, "accept"}
    assert body == rf.STS_BODY


def test_the_containers_sigv4_matches_botocore():
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
    except ModuleNotFoundError:
        return
    when = _dt.datetime(2026, 10, 4, 12, 0, 0, tzinfo=_dt.timezone.utc)
    proof = rf.sts_proof("eu-west-2", "AKIDEXAMPLE", "wJalrXUtnFEMI", "tok", "the-token", now=when)
    req = AWSRequest(method="POST", url=proof["url"], data=rf.STS_BODY,
                     headers={k: v for k, v in proof["headers"].items()
                              if k in ("content-type", rf.BINDING_HEADER)})
    import unittest.mock as m
    with m.patch("botocore.auth.datetime") as dt:
        dt.datetime.utcnow.return_value = when.replace(tzinfo=None)
        SigV4Auth(Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI", "tok"), "sts",
                  "eu-west-2").add_auth(req)
    theirs = req.headers["Authorization"].split("Signature=")[1]
    ours = proof["headers"]["authorization"].split("Signature=")[1]
    assert theirs == ours, "the container's SigV4 must be the real thing"


# ── Cloud Run proof ───────────────────────────────────────────────────────────

def _google(claims_for):
    def verify(raw, aud):
        return claims_for(aud)
    return verify


def test_gcp_round_trip_and_refusals():
    token = _issue(runner="gcp", job="job-gcp")
    want = rf.gcp_audience(rc.callback_url(), token)
    priv, env, meta = _redeem(token, {"id_token": "a.b.c"}, google=_google(
        lambda aud: {"aud": aud, "email": SA, "email_verified": True}))
    assert aud_ok(want, token) and meta["identity"] == SA
    assert json.loads(rf.open_sealed(priv, env, agent_id="runner:gcp", audience=AUD,
                                     job_id="job-gcp", ref=rf.REF)) == VALUES
    for claims, why in (
            (lambda aud: {"aud": aud, "email": "x@p.iam.gserviceaccount.com",
                          "email_verified": True}, "configured runner"),
            (lambda aud: {"aud": aud, "email": SA, "email_verified": False}, "configured runner"),
            (lambda aud: {"aud": aud + "x", "email": SA, "email_verified": True}, "different token")):
        token = _issue(runner="gcp")
        _refused(lambda: _redeem(token, {"id_token": "a.b.c"}, google=_google(claims)), why)
    token = _issue(runner="gcp")
    _refused(lambda: _redeem(token, {"id_token": "a.b.c"}, google=lambda r, a: (_ for _ in ()).throw(
        ValueError("bad signature"))), "did not verify")


def aud_ok(want, token):
    return want.endswith("?binding=" + rf.binding(token)) and want.startswith(AUD + rc.ROUTE)


# ── the container side ────────────────────────────────────────────────────────

def test_the_containers_opener_is_agent_sealings_format():
    priv, pub = rf.keypair()
    env = agent_sealing.seal(pub, "s3cret", agent_id="runner:ecs", audience=AUD, job_id="j",
                             ref=rf.REF)
    assert rf.open_sealed(priv, env, agent_id="runner:ecs", audience=AUD, job_id="j",
                          ref=rf.REF) == "s3cret"
    assert rf.seal_aad(agent_id="a", audience="b", epk="c", job_id="d", ref="e") == \
        agent_sealing.seal_aad(agent_id="a", audience="b", epk="c", job_id="d", ref="e")
    assert (rf.SEAL_INFO, rf.SEAL_VERSION, rf.SEAL_ALG) == (
        agent_sealing.SEAL_INFO, agent_sealing.SEAL_VERSION, agent_sealing.SEAL_ALG)
    try:
        rf.open_sealed(priv, env, agent_id="runner:gcp", audience=AUD, job_id="j", ref=rf.REF)
        raise AssertionError("opened under the wrong context")
    except Exception as exc:  # noqa: BLE001
        assert "opened under" not in str(exc)


def test_the_whole_fetch_round_trips_into_a_0600_vars_file():
    """runner_fetch.main against the real redeem, with requests stubbed to route there."""
    token = _issue(job="job-main")
    vars_file = os.path.join(tempfile.mkdtemp(), "vars.json")
    with open(vars_file, "w", encoding="utf-8") as fh:
        json.dump({"from_manifest": "kept"}, fh)

    class Resp:
        def __init__(self, code, doc):
            self.status_code, self._doc = code, doc

        def json(self):
            return self._doc

    fake = types.ModuleType("requests")
    fake.get = lambda url, timeout=5: Resp(200, {
        "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "secret", "Token": "t"})

    def post(url, json=None, timeout=30):
        assert url == rc.callback_url()
        db = SessionLocal()
        try:
            env, _ = rc.redeem(db, token=json["token"], proof=json["proof"],
                               reply_key=json["reply_key"], sts_post=_sts_ok())
        finally:
            db.close()
        return Resp(200, {"sealed": env})
    fake.post = post
    saved = sys.modules.get("requests")
    sys.modules["requests"] = fake
    try:
        rc_ = rf.main({"RUNNER_CREDENTIAL_URL": rc.callback_url(), "RUNNER_CREDENTIAL_TOKEN": token,
                       "RUNNER_CREDENTIAL_PLATFORM": "ecs", "RUNNER_CREDENTIAL_JOB": "job-main",
                       "RUNNER_CREDENTIAL_AUDIENCE": AUD, "RUNNER_CREDENTIAL_STS_REGION": "us-east-1",
                       "RUNNER_CREDENTIAL_VARS_FILE": vars_file,
                       "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/creds"} | {})
    finally:
        if saved is not None:
            sys.modules["requests"] = saved
    assert rc_ == 0
    with open(vars_file, encoding="utf-8") as fh:
        assert json.load(fh) == {"from_manifest": "kept", **VALUES}
    assert stat.S_IMODE(os.stat(vars_file).st_mode) == 0o600


def test_a_refused_collection_exits_non_zero_without_printing_the_token():
    import contextlib
    import io

    class Resp:
        status_code = 403

        def json(self):
            return {"detail": "this runner token is unknown, expired or already used"}
    fake = types.ModuleType("requests")
    fake.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no creds needed"))
    fake.post = lambda *a, **k: Resp()
    saved = sys.modules.get("requests")
    sys.modules["requests"] = fake
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = rf.main({"RUNNER_CREDENTIAL_URL": "https://x", "RUNNER_CREDENTIAL_TOKEN": "TOK-xyz",
                            "RUNNER_CREDENTIAL_PLATFORM": "gcp", "RUNNER_CREDENTIAL_JOB": "j",
                            "RUNNER_CREDENTIAL_AUDIENCE": AUD, "RUNNER_CREDENTIAL_VARS_FILE": "/dev/null"})
    except Exception:  # noqa: BLE001 — the gcp proof fetch uses fake.get, which raises
        code = 2
    finally:
        if saved is not None:
            sys.modules["requests"] = saved
    assert code != 0 and "TOK-xyz" not in err.getvalue()


# ── the run wiring ────────────────────────────────────────────────────────────

def test_the_token_rides_the_override_and_the_script_the_task_definition():
    from web_dashboard.services import ansible_local_run_service as svc, aws_service
    db = SessionLocal()
    try:
        fetch = svc._runner_fetch(db, "ecs", "job-wire", dict(VALUES))
    finally:
        db.close()
    captured = {}

    class ECS:
        def register_task_definition(self, **kw):
            captured["td"] = kw
            return {"taskDefinition": {"taskDefinitionArn": "arn:td"}}

        def create_cluster(self, **kw):
            pass

        def run_task(self, **kw):
            captured["run"] = kw
            return {"tasks": [{"taskArn": "arn:task/abc"}]}

        def describe_tasks(self, **kw):
            return {"tasks": [{"lastStatus": "STOPPED",
                               "containers": [{"name": "ansible", "exitCode": 0}]}]}

    class Logs:
        def create_log_group(self, **kw):
            pass

        def get_log_events(self, **kw):
            return {"events": []}

    saved = (aws_service._get_ecs, aws_service.boto3.client)
    aws_service._get_ecs = lambda region: ECS()
    aws_service.boto3.client = lambda *a, **k: Logs()
    try:
        aws_service._run_ecs_ansible_sync(
            "us-east-1", "c", "fam", "img", "256", "512", "subnet", [], "arn:exec",
            "10.0.0.1", "ubuntu", "cGxheQ==", "c3No", "job-wire", None, "", None,
            ROLE, fetch)
    finally:
        aws_service._get_ecs, aws_service.boto3.client = saved
    container = captured["td"]["containerDefinitions"][0]
    td_env = {e["name"]: e["value"] for e in container["environment"]}
    override = {e["name"]: e["value"] for e in
                captured["run"]["overrides"]["containerOverrides"][0]["environment"]}
    assert fetch["token"] not in json.dumps(captured["td"]), "the token must not be on the task def"
    assert override["RUNNER_CREDENTIAL_TOKEN"] == fetch["token"]
    assert td_env[cloud_ansible_secrets.FETCH_ENV] == fetch["script_b64"]
    assert captured["td"]["taskRoleArn"] == ROLE
    cmd = container["command"][2]
    assert cloud_ansible_secrets.fetch_prefix() in cmd and cloud_ansible_secrets.extra_vars_arg() in cmd
    assert "Pw!Managed" not in json.dumps(captured)
    assert base64.b64decode(fetch["script_b64"]).decode() == open(rf.__file__, encoding="utf-8").read()


def test_the_problem_check_and_the_gate_helper():
    _configure()
    assert rc.problem("ecs") == "" and rc.problem("gcp") == "" and rc.use_for("ecs")
    config_service.set(rc.ECS_TASK_ROLE, "not-an-arn")
    assert "task role" in rc.problem("ecs") and not rc.use_for("ecs") and rc.use_for("gcp")
    config_service.set("ansible_cloud_ephemeral_secrets_enabled", "")
    assert rc.cloud_delivery_available("gcp") and not rc.cloud_delivery_available("ecs")
    config_service.set(rc.SETTING, "")
    assert not rc.cloud_delivery_available("gcp")
    _configure()


# ── the route ─────────────────────────────────────────────────────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import agent as agent_api
    app = FastAPI()
    app.include_router(agent_api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_the_route_is_no_store_audited_and_echoes_nothing():
    _configure()
    client = _client()
    token = _issue(job="job-route")
    _, pub = rf.keypair()
    # redeem's sts_post default is bound at definition time, so wrap redeem itself.
    import web_dashboard.services.runner_credential as mod
    orig = mod.redeem
    try:

        def redeem(db, **kw):
            return orig(db, sts_post=_sts_ok(), **kw)
        mod.redeem = redeem
        r = client.post("/api/agent/runner-credential",
                        json={"token": token, "proof": _ecs_proof(token), "reply_key": pub})
        r2 = client.post("/api/agent/runner-credential",
                         json={"token": token, "proof": _ecs_proof(token), "reply_key": pub})
    finally:
        mod.redeem = orig
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store"
    assert token not in r.text and "Pw!Managed" not in r.text
    assert r2.status_code == 403 and token not in r2.text
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action.like("agent.runner_credential%")).all()
        blob = " ".join(f"{x.action} {x.details}" for x in rows)
    finally:
        db.close()
    assert "agent.runner_credential " in blob + " " and "refused" in blob
    assert token not in blob and "Pw!Managed" not in blob


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
