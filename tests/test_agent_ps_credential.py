"""The Password Safe checkout/release lifecycle for a remote agent's job.

Acquiring a credential is the easy half. Releasing it correctly is where the bugs live, and
three of them are the reason this file exists:

  * **``ConflictOption=reuse`` means concurrent jobs on one account share a request id.**
    Releasing per job would check the request in — and, with rotation on, change the
    password — underneath a sibling job still authenticating with it. So the release is
    ref-counted, and a naive implementation passes every other test in this file.
  * **``reconcile_stale_jobs`` writes ``failed`` inline and never calls ``complete_job``.**
    A killed agent container therefore never reaches the completion hook, which makes the
    sweeper — not the hook — the authority. A release wired only into the hook leaks every
    request belonging to an agent that died, which is the case that matters most.
  * **Rotation must come after check-in, and happen once.** Rotating while the request is
    open can leave it holding a password that no longer works, which reads to the next
    reader as a wrong credential rather than as a rotation.

Password Safe itself is stubbed: what is under test is the ordering and the ref count, not
httpx. Runs under pytest, or standalone:
    python tests/test_agent_ps_credential.py
"""
import asyncio
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-ps-cred-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-ps-cred-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, engine
    from web_dashboard.services import (agent_ps_credential_service as svc,
                                        config_service,
                                        hypervisor_connection_service as hcs,
                                        job_service, ps_api_service)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

ACCOUNT = "4242"
CREDENTIAL = "PS!Released#Secret"


# A fresh request id per stub. The ref count is keyed on the request id — which is exactly
# right, since ConflictOption=reuse means a shared id really is a shared request — so two
# tests reusing one id would look to each other like concurrent jobs on the same account.
_NEXT_REQUEST_ID = [9000]


def _fresh_request_id() -> int:
    _NEXT_REQUEST_ID[0] += 1
    return _NEXT_REQUEST_ID[0]


class _PS:
    """Records what Password Safe was asked to do, in order."""

    def __init__(self, *, request_id=None, credential=CREDENTIAL, checkout_raises=None):
        request_id = _fresh_request_id() if request_id is None else request_id
        self.calls = []
        self.request_id = request_id
        self.credential = credential
        self.checkout_raises = checkout_raises

    def install(self):
        self._saved = {name: getattr(ps_api_service, name) for name in (
            "_client", "_sign_in", "_sign_out", "_checkout", "_checkin",
            "change_managed_account_password", "configured")}

        class _Client:
            async def aclose(self):
                pass

        async def _sign_in(_client):
            self.calls.append("sign_in")

        async def _sign_out(_client):
            self.calls.append("sign_out")

        async def _checkout(_client, account_id, *, duration_min, reason, system_id=0):
            self.calls.append(("checkout", int(account_id), duration_min))
            if self.checkout_raises:
                raise self.checkout_raises
            return self.request_id, self.credential

        async def _checkin(_client, request_id, reason=""):
            self.calls.append(("checkin", int(request_id)))

        async def _rotate(account_id):
            self.calls.append(("rotate", int(account_id)))

        ps_api_service._client = lambda: _Client()
        ps_api_service._sign_in = _sign_in
        ps_api_service._sign_out = _sign_out
        ps_api_service._checkout = _checkout
        ps_api_service._checkin = _checkin
        ps_api_service.change_managed_account_password = _rotate
        ps_api_service.configured = lambda: True
        return self

    def restore(self):
        for name, fn in self._saved.items():
            setattr(ps_api_service, name, fn)

    def __enter__(self):
        return self.install()

    def __exit__(self, *_exc):
        self.restore()
        return False

    @property
    def checkins(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == "checkin"]

    @property
    def rotations(self):
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == "rotate"]


def _row(agent_id="agt-1", ref="dc1-vcenter", account=ACCOUNT):
    db = SessionLocal()
    try:
        out = hcs.create(db, kind="vsphere", name=f"c-{uuid.uuid4().hex[:8]}",
                         created_by="t", agent_id=agent_id, agent_connection_name=ref,
                         secret_ref=f"{hcs.PS_ACCOUNT_PREFIX}{account}")
        return db.query(hcs.HypervisorConnection).filter_by(id=out["id"]).one()
    finally:
        db.close()


def _job(status="running", agent_id="agt-1"):
    db = SessionLocal()
    try:
        job = job_service.create_job(db, job_type="agent_hypervisor", created_by="t",
                                     agent_id=agent_id, metadata={"verb": "inventory_sync"})
        row = db.query(Job).filter(Job.id == job.id).first()
        row.status = status
        db.commit()
        return row.id
    finally:
        db.close()


def _reload(job_id):
    db = SessionLocal()
    try:
        return db, db.query(Job).filter(Job.id == job_id).first()
    finally:
        pass  # caller closes


def _set_status(job_id, status):
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id).update({"status": status})
        db.commit()
    finally:
        db.close()


# ── the reference scheme ──────────────────────────────────────────────────────

def test_ps_account_is_not_resolvable_through_config_service():
    """The load-bearing negative. The four prefixes in `config_service._EXT_PREFIXES` are
    stateless reads; this one opens a request somebody has to close. Registered there,
    every incidental `config_service.get()` would leak an open Password Safe request."""
    assert hcs.PS_ACCOUNT_PREFIX == "ps_account://"
    for prefix in config_service._EXT_PREFIXES:
        assert not prefix.startswith("ps_account"), \
            "ps_account:// must never be a generic config reference"
    assert not config_service.is_reference("ps_account://4242")


def test_the_generic_resolver_refuses_a_ps_account_row():
    """`resolve_agent_secret` must not treat a lifecycle reference as a value — that is how
    a request gets opened by a caller with no path to close it."""
    row = _row()
    assert hcs.is_ps_account(row) and hcs.ps_account_id(row) == ACCOUNT
    try:
        hcs.resolve_agent_secret(row)
    except hcs.HypervisorConnectionError as exc:
        assert "agent_ps_credential_service" in str(exc)
        return
    raise AssertionError("a ps_account:// row was resolved as a plain value")


def test_a_page_load_never_resolves_an_agent_rows_credential():
    """Every hypervisor router reaches `to_connection` before it checks `agent_id`, so a
    resolving `to_connection` would open a Password Safe request on every page view."""
    assert hcs.to_connection(_row()).secret == ""


def test_a_malformed_reference_is_refused_before_any_call():
    row = _row(account="not-a-number")
    job_id = _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            try:
                asyncio.run(svc.acquire(db, job, row))
            except svc.PSCredentialError as exc:
                assert "ps_account://12345" in str(exc)
            else:
                raise AssertionError("a malformed account id was accepted")
        finally:
            db.close()
    assert ps.calls == [], "nothing should have been called"


# ── acquire ───────────────────────────────────────────────────────────────────

def test_acquire_returns_the_credential_and_records_the_request():
    """The request id is recorded BEFORE the credential is returned: reversed, a crash
    between the two leaves an open request nothing points at, invisible to the sweeper."""
    row, job_id = _row(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            secret, source = asyncio.run(svc.acquire(db, job, row))
        finally:
            db.close()
    assert (secret, source) == (CREDENTIAL, "ps_account")
    assert ("checkout", int(ACCOUNT), 45) in ps.calls
    assert "sign_out" in ps.calls, "the session must be closed even on the happy path"

    db = SessionLocal()
    try:
        meta = db.query(Job).filter(Job.id == job_id).first().metadata_dict
        assert meta[svc.META_REQUEST_ID] == ps.request_id
        assert meta[svc.META_ACCOUNT_ID] == ACCOUNT
    finally:
        db.close()


def test_an_empty_credential_is_refused_and_released():
    """An empty password reaching a hypervisor reads as "wrong username or password", and
    retried on the sync schedule it risks locking the service account out. So it is refused
    here — and the request it opened is handed straight back rather than left for its whole
    duration."""
    row, job_id = _row(), _job()
    with _PS(credential="") as ps:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            try:
                asyncio.run(svc.acquire(db, job, row))
            except svc.PSCredentialError as exc:
                assert "empty credential" in str(exc)
            else:
                raise AssertionError("an empty credential was returned to the agent")
        finally:
            db.close()
    assert ps.checkins == [("checkin", ps.request_id)], "the opened request must be released"


def test_a_password_safe_refusal_carries_its_own_reason_through():
    """4031 vs 4034 vs 4035 is the only thing separating a missing grant from an awaiting
    approval from a concurrency cap, and a failed job renders this string and nothing else."""
    row, job_id = _row(), _job()
    boom = ps_api_service.PSApiError("Password Safe refused (403): {\"code\":4034} …")
    with _PS(checkout_raises=boom):
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            try:
                asyncio.run(svc.acquire(db, job, row))
            except svc.PSCredentialError as exc:
                assert "4034" in str(exc)
                return
            raise AssertionError("a Password Safe refusal was swallowed")
        finally:
            db.close()


# ── release: the ref count ────────────────────────────────────────────────────

def test_a_shared_request_is_released_only_by_the_last_job():
    """The one a naive implementation gets wrong. Two jobs, one account, one request id:
    releasing on the first completion would rotate the password underneath the second."""
    row = _row()
    first, second = _job(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            for job_id in (first, second):
                job = db.query(Job).filter(Job.id == job_id).first()
                asyncio.run(svc.acquire(db, job, row))
        finally:
            db.close()

        # First job finishes. The sibling is still running, so nothing may be released.
        _set_status(first, "completed")
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == first).first()
            assert asyncio.run(svc.release_for_job(db, job)) is False
        finally:
            db.close()
        assert ps.checkins == [], "released while a sibling job was still using it"
        assert ps.rotations == [], "ROTATED under a live sibling job"

        # ...and the finished job no longer claims it, so it cannot double-release later.
        db = SessionLocal()
        try:
            assert not (db.query(Job).filter(Job.id == first).first()
                        .metadata_dict.get(svc.META_REQUEST_ID))
        finally:
            db.close()

        # Now the second finishes: this one releases.
        _set_status(second, "completed")
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == second).first()
            assert asyncio.run(svc.release_for_job(db, job)) is True
        finally:
            db.close()
        assert ps.checkins == [("checkin", ps.request_id)]
        assert ps.rotations == [("rotate", int(ACCOUNT))]


def test_release_is_idempotent_under_two_workers():
    """The completion hook in one gunicorn worker and the sweeper in another race in
    practice. A double check-in would be harmless; a double ROTATION burns a password."""
    row, job_id = _row(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            asyncio.run(svc.acquire(db, job, row))
        finally:
            db.close()
        _set_status(job_id, "completed")
        for expected in (True, False, False):
            db = SessionLocal()
            try:
                job = db.query(Job).filter(Job.id == job_id).first()
                assert asyncio.run(svc.release_for_job(db, job)) is expected
            finally:
                db.close()
    assert len(ps.checkins) == 1 and len(ps.rotations) == 1


def test_rotation_happens_after_checkin_and_can_be_turned_off():
    row, job_id = _row(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            asyncio.run(svc.acquire(db, job, row))
        finally:
            db.close()
        _set_status(job_id, "completed")
        db = SessionLocal()
        try:
            asyncio.run(svc.release_for_job(
                db, db.query(Job).filter(Job.id == job_id).first()))
        finally:
            db.close()
        ordered = [c for c in ps.calls if c[0] in ("checkin", "rotate")]
        assert ordered == [("checkin", ps.request_id),
                           ("rotate", int(ACCOUNT))], ordered

    # Off: the request still closes, the password simply survives to PS's own schedule.
    config_service.set("agent_ps_rotate_on_release", "false")
    try:
        row2, job2 = _row(), _job()
        with _PS() as ps:
            db = SessionLocal()
            try:
                asyncio.run(svc.acquire(db, db.query(Job).filter(Job.id == job2).first(),
                                        row2))
            finally:
                db.close()
            _set_status(job2, "failed")
            db = SessionLocal()
            try:
                asyncio.run(svc.release_for_job(
                    db, db.query(Job).filter(Job.id == job2).first()))
            finally:
                db.close()
            assert ps.checkins and not ps.rotations
    finally:
        config_service.set("agent_ps_rotate_on_release", "true")


def test_a_job_that_holds_nothing_releases_nothing():
    job_id = _job(status="completed")
    with _PS() as ps:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            assert asyncio.run(svc.release_for_job(db, job)) is False
        finally:
            db.close()
    assert ps.calls == []


# ── release: the sweeper is the authority ─────────────────────────────────────

def test_the_sweeper_releases_a_job_the_reaper_failed_inline():
    """`reconcile_stale_jobs` writes `failed` without ever calling `complete_job`, so a
    killed agent never reaches the completion hook. This is the path that covers it — and
    the case that matters most, because it is the one where an agent host went away."""
    row, job_id = _row(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            asyncio.run(svc.acquire(db, db.query(Job).filter(Job.id == job_id).first(),
                                    row))
        finally:
            db.close()

        # Exactly what the reaper does: status flipped in place, no hook, no complete_job.
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            job.status = "failed"
            job.error_message = "Interrupted by an app restart (no heartbeat)"
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            assert svc.sweep(db) >= 1
            assert not (db.query(Job).filter(Job.id == job_id).first()
                        .metadata_dict.get(svc.META_REQUEST_ID))
        finally:
            db.close()
    assert ps.checkins == [("checkin", ps.request_id)]
    assert ps.rotations == [("rotate", int(ACCOUNT))]


def test_the_sweeper_leaves_a_running_job_alone():
    row, job_id = _row(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            asyncio.run(svc.acquire(db, db.query(Job).filter(Job.id == job_id).first(),
                                    row))
        finally:
            db.close()
        db = SessionLocal()
        try:
            svc.sweep(db)
        finally:
            db.close()
    assert ps.checkins == [], "released a credential a live job was still using"


def test_the_sweeper_survives_a_wedged_tenant():
    """One failing release must not stop the pass, and must never raise into the caller —
    the job runner calls this on the same timer it reaps stale jobs on."""
    row, job_id = _row(), _job()
    with _PS() as ps:
        db = SessionLocal()
        try:
            asyncio.run(svc.acquire(db, db.query(Job).filter(Job.id == job_id).first(),
                                    row))
        finally:
            db.close()
        _set_status(job_id, "completed")

        async def _boom(_client, _request_id, reason=""):
            raise RuntimeError("Password Safe is unreachable")
        ps_api_service._checkin = _boom

        db = SessionLocal()
        try:
            svc.sweep(db)   # must not raise, whatever Password Safe is doing
        finally:
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
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
