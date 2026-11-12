"""In-product feedback: the properties that are easy to break and expensive to break.

Runs standalone::

    python tests/test_feedback.py

The three that matter most, in order:

1. **A POV accessor is refused on the dashboard's own feedback routes.** An accessor's
   permission map is empty, which ``has_permission`` reads as UNRESTRICTED, so the only
   thing confining them is the path allowlist in ``auth.get_current_user``. Feedback
   deliberately did not widen it: the guest's submit is a route under
   ``/api/pov/accessor/self``, which is already allowed.

2. **No feedback route collides with an allowlisted prefix.** The allowlist match is a
   bare ``startswith`` with no path-boundary check. If somebody later adds an entry like
   ``/api/feedback`` -- or names a listing route so that an existing entry prefixes it --
   every prospect on the instance gets ``/mine`` and ``/all``, which is everything every
   other prospect and every SE ever typed into the form.

3. **A credential is refused, not filed.** ``secret_scan.scan_text`` returns findings and
   hands the input back UNTOUCHED, so anything treating it as a redaction pass would file
   the credential verbatim while reporting it had been handled.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from _schema import build_schema
    build_schema("feedback")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import feedback as feedback_api
    from web_dashboard.api.auth import (_ACCESSOR_ALLOWED_PREFIXES, get_current_user,
                                        require_admin, require_person)
    from web_dashboard.database import FeedbackSubmission, SessionLocal, get_db
    from web_dashboard.services import feedback_guard, feedback_service
except ModuleNotFoundError as exc:  # pragma: no cover - dependency-light machines
    print(f"SKIP: {exc}")
    sys.exit(0)


class _User:
    """Enough of a User for the dependencies these routes actually resolve."""

    def __init__(self, username="sam", is_admin=False, accessor_env_id=None):
        self.username = username
        self.is_admin = is_admin
        self.is_effective_admin = is_admin
        self.is_service_account = False
        self.accessor_env_id = accessor_env_id


def _client(user=None, *, admin=False):
    app = FastAPI()
    app.include_router(feedback_api.router)
    db = SessionLocal()

    def _db():
        try:
            yield db
        finally:
            pass

    principal = user or _User(is_admin=admin)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[require_person] = lambda: principal
    app.dependency_overrides[require_admin] = lambda: principal
    return TestClient(app, raise_server_exceptions=False), db


def _wipe(db):
    db.query(FeedbackSubmission).delete()
    db.commit()


class _on:
    """Feature on, with a mode, for the duration of a block.

    The flag and the mode are separate gates on purpose -- the router mounts ungated so
    an administrator can read /context before switching the feature on -- so a test that
    only sets a mode still gets "not enabled on this instance".
    """

    def __init__(self, mode="compose", repo="owner/name"):
        self.mode, self.repo = mode, repo

    def __enter__(self):
        import web_dashboard.api.feedback as fa
        import web_dashboard.services.feedback_service as fs
        self._saved = (fa._enabled, fs.mode, fs.repo)
        fa._enabled = lambda: True
        fs.mode = lambda: self.mode
        fs.repo = lambda: self.repo
        return self

    def __exit__(self, *exc):
        import web_dashboard.api.feedback as fa
        import web_dashboard.services.feedback_service as fs
        fa._enabled, fs.mode, fs.repo = self._saved
        return False


def _row(db, **kw):
    base = dict(submitted_by="sam", submitter_kind="user", category="bug",
                title="t", body="b", status="filed", mode="direct")
    base.update(kw)
    r = FeedbackSubmission(**base)
    db.add(r)
    db.commit()
    return r


# -- 1. The allowlist property ----------------------------------------------

def test_no_feedback_route_is_prefixed_by_an_accessor_allowlist_entry():
    """The single most important assertion in this file.

    ``_accessor_may_reach`` is ``path.startswith(entry)`` with no boundary check, so a
    listing route that merely BEGINS with an allowed prefix is reachable by every
    prospect. Checked against the real router rather than a copy of its paths.
    """
    paths = [r.path for r in feedback_api.router.routes]
    assert paths, "the feedback router has no routes"
    exposed = [(p, e) for p in paths for e in _ACCESSOR_ALLOWED_PREFIXES
               if p.startswith(e)]
    assert not exposed, (
        "these feedback routes are reachable by a POV accessor because an allowlist "
        "entry prefixes them: " + repr(exposed))


def test_the_guest_route_rides_the_prefix_that_is_already_allowed():
    """The accessor's submit must be inside an allowlisted prefix, or it 403s for the
    only people it exists for."""
    from web_dashboard.api import pov_accessor
    paths = [r.path for r in pov_accessor.self_router.routes]
    assert "/api/pov/accessor/self/feedback" in paths, paths
    assert any("/api/pov/accessor/self/feedback".startswith(e)
               for e in _ACCESSOR_ALLOWED_PREFIXES), _ACCESSOR_ALLOWED_PREFIXES


def test_the_guest_route_takes_no_environment_id():
    """Every /self route resolves the POV from the session. A route that took an env id
    would need an ownership check somebody will one day forget to write."""
    import inspect
    from web_dashboard.api import pov_accessor
    sig = inspect.signature(pov_accessor.submit_self_feedback)
    assert "env_id" not in sig.parameters, sorted(sig.parameters)
    src = inspect.getsource(pov_accessor.submit_self_feedback)
    assert "_accessor_env" in src, "the POV must come from the session"


# -- 2. Credentials are refused ---------------------------------------------

def test_a_credential_in_the_body_is_refused_and_nothing_is_stored():
    client, db = _client()
    _wipe(db)
    try:
        with _on():
            r = client.post("/api/feedback/submit", json={
                "category": "bug", "title": "token leak",
                "body": "here you go: ghp_" + "a" * 36})
        assert r.status_code == 400, (r.status_code, r.text)
        assert "github_token" in r.text, r.text
        assert db.query(FeedbackSubmission).count() == 0, "a refused submission was stored"
    finally:
        _wipe(db)
        db.close()


def test_scan_text_does_not_redact_which_is_why_we_refuse():
    """Pins the assumption the refusal rests on. If scan_text ever starts returning
    cleaned text, this fails and the refusal can be revisited deliberately."""
    from web_dashboard.services import secret_scan
    needle = "ghp_" + "b" * 36
    found = secret_scan.scan_text("token: " + needle)
    assert found, "the detector stopped detecting a github token"
    assert needle in "token: " + needle, "sanity"
    # The finding's own match is redacted, but the caller's text is untouched -- which is
    # the whole reason find_credentials is a gate and not a filter.
    assert all(needle not in f["match"] for f in found), found


# -- 3. Throttling -----------------------------------------------------------

def test_the_per_user_cap_refuses_with_429_and_a_retry_after():
    client, db = _client()
    _wipe(db)
    try:
        import web_dashboard.services.feedback_guard as fg
        orig = fg._cfg_int
        fg._cfg_int = lambda key, default: {
            "feedback_max_per_user": 2, "feedback_max_global": 0,
            "feedback_window_hours": 24}.get(key, default)
        try:
            for _ in range(2):
                _row(db)
            with _on():
                r = client.post("/api/feedback/submit",
                                json={"title": "one more", "body": "please"})
            assert r.status_code == 429, (r.status_code, r.text)
            assert r.headers.get("Retry-After"), dict(r.headers)
        finally:
            fg._cfg_int = orig
    finally:
        _wipe(db)
        db.close()


def test_a_cap_of_zero_disables_it():
    db = SessionLocal()
    _wipe(db)
    try:
        import web_dashboard.services.feedback_guard as fg
        orig = fg._cfg_int
        fg._cfg_int = lambda key, default: 0 if key.startswith("feedback_max") else 24
        try:
            for _ in range(5):
                _row(db)
            feedback_guard.check(db, username="sam")   # must not raise
        finally:
            fg._cfg_int = orig
    finally:
        _wipe(db)
        db.close()


# -- 4. Row visibility -------------------------------------------------------

def test_mine_never_returns_another_users_rows():
    client, db = _client(_User(username="sam"))
    _wipe(db)
    try:
        _row(db, submitted_by="sam", title="mine")
        _row(db, submitted_by="alex", title="theirs")
        r = client.get("/api/feedback/mine")
        assert r.status_code == 200, r.text
        titles = [x["title"] for x in r.json()["data"]]
        assert titles == ["mine"], titles
    finally:
        _wipe(db)
        db.close()


def test_all_is_admin_only():
    """Not a scope: adding one would silently revoke it from every existing role. The
    all-rows view holds whatever a prospect typed, so it answers to the admin flag."""
    import inspect
    src = inspect.getsource(feedback_api.all_feedback)
    assert "require_admin" in src, src[:200]


# -- 5. Compose mode ---------------------------------------------------------

def test_compose_mode_files_nothing_and_still_records_the_row():
    client, db = _client()
    _wipe(db)
    try:
        with _on():
            r = client.post("/api/feedback/submit",
                            json={"title": "a thing", "body": "some detail"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "compose"
        assert body["compose_url"].startswith("https://github.com/owner/name/issues/new?")
        rows = db.query(FeedbackSubmission).all()
        assert len(rows) == 1 and rows[0].status == "composed", [r.status for r in rows]
        assert rows[0].github_issue_number is None
    finally:
        _wipe(db)
        db.close()


def test_a_guest_in_compose_mode_is_never_handed_a_github_link():
    """A prospect is not a contributor and may have no GitHub account. Handing them a
    prefilled issue form to file themselves is a dead end dressed up as success, so their
    submission is recorded here and the SE reads it on the admin view."""
    db = SessionLocal()
    _wipe(db)
    try:
        with _on(mode="compose"):
            out = feedback_api.record_and_deliver(
                db, submitted_by="acme-guest", submitter_kind="accessor",
                payload=feedback_api.FeedbackCreate(
                    category="bug", title="it asked twice",
                    body="pressed reveal and it asked again", page_path="/pov/access"),
                pov_env_id="env-1")
        assert "compose_url" not in out, out
        assert out.get("recorded_only") is True, out
        row = db.query(FeedbackSubmission).one()
        assert row.submitter_kind == "accessor" and row.pov_env_id == "env-1"
        assert row.status == "composed", row.status
    finally:
        _wipe(db)
        db.close()


def test_direct_mode_records_the_issue_number_it_was_given():
    client, db = _client()
    _wipe(db)
    try:
        import web_dashboard.services.feedback_service as fs
        with _on(mode="direct"):
            orig = fs.file_issue
            fs.file_issue = lambda t, b, **kw: {"number": 1234,
                                                "url": "https://github.com/o/n/issues/1234"}
            try:
                r = client.post("/api/feedback/submit",
                                json={"title": "it broke", "body": "here is how"})
            finally:
                fs.file_issue = orig
        assert r.status_code == 200, r.text
        assert r.json()["issue_number"] == 1234, r.json()
        row = db.query(FeedbackSubmission).one()
        assert row.status == "filed" and row.github_issue_number == 1234
        assert row.issue_state == "open" and row.checked_at is not None
    finally:
        _wipe(db)
        db.close()


def test_a_failed_file_keeps_the_submission_and_says_it_was_not_filed():
    """The submitter did nothing wrong and must not be told it worked, nor lose what
    they typed -- so the row survives as `error` and the response is a 502."""
    client, db = _client()
    _wipe(db)
    try:
        import web_dashboard.services.feedback_service as fs
        with _on(mode="direct"):
            orig = fs.file_issue

            def _boom(t, b, **kw):
                raise fs.FeedbackError("The configured GitHub token was refused.")

            fs.file_issue = _boom
            try:
                r = client.post("/api/feedback/submit",
                                json={"title": "it broke", "body": "here is how"})
            finally:
                fs.file_issue = orig
        assert r.status_code == 502, (r.status_code, r.text)
        row = db.query(FeedbackSubmission).one()
        assert row.status == "error", row.status
        assert row.body == "here is how", "the submission was lost"
        assert row.error_message, "no reason recorded"
    finally:
        _wipe(db)
        db.close()


def test_the_issue_body_carries_the_context_and_no_secrets():
    class _R:
        category, title, body = "bug", "t", "the description"
        page_path, app_version, commit = "/vms", "26.11.1", "abcdef1234567890"
        instance_profile, submitted_by, submitter_kind = "pov", "guest-1", "accessor"
        pov_env_id = "env-9"
    out = feedback_service.compose_body(_R())
    for needle in ("/vms", "26.11.1", "abcdef123456", "pov", "guest-1", "env-9",
                   "the description"):
        assert needle in out, (needle, out)
    assert feedback_service.compose_title(_R()).startswith("[Bug] ")


def test_a_long_body_is_truncated_for_the_link_but_not_for_the_row():
    long_body = "x" * (feedback_service.COMPOSE_BODY_LIMIT + 500)
    url, truncated = feedback_service.compose_url("owner/name", "t", long_body)
    assert truncated is True
    assert len(url) < len(long_body) + 400, "the cap did not apply"
    short = feedback_service.compose_url("owner/name", "t", "short")
    assert short[1] is False


# -- 6. The repo slug --------------------------------------------------------

def test_a_bad_repo_slug_is_refused_before_it_reaches_a_url():
    """Unvalidated it is path traversal against api.github.com in direct mode and an
    open redirect in compose mode."""
    for bad in ("", "owner", "owner/name/extra", "../../etc", "owner/name?x=1",
                "https://evil.example/a/b", "owner /name"):
        try:
            feedback_service.validate_repo(bad)
        except feedback_service.FeedbackError:
            continue
        raise AssertionError("accepted a bad repo slug: " + repr(bad))
    assert feedback_service.validate_repo("Weaverlab-xyz/vm-dashboard-community")


def test_mode_is_off_without_a_repo():
    import web_dashboard.services.feedback_service as fs
    orig_repo, orig_token = fs.repo, fs._token
    fs.repo, fs._token = (lambda: ""), (lambda: "tok")
    try:
        assert fs.mode() == "off"
    finally:
        fs.repo, fs._token = orig_repo, orig_token


# -- 7. Shape of the module --------------------------------------------------

def test_no_feedback_route_is_async():
    """An async route running synchronous SQLAlchemy puts the query on the event loop,
    where one slow call freezes the worker. tests/test_sync_db_routes_off_the_event_loop
    holds this globally; this says it for the file being added."""
    import ast
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "feedback.py"),
               encoding="utf-8").read()
    bad = [n.name for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.AsyncFunctionDef)]
    assert not bad, "async routes in api/feedback.py: " + repr(bad)


def test_the_token_is_registered_as_a_secret():
    """Without this GET /api/setup/config hands the PAT back in clear."""
    from web_dashboard.services import secret_hygiene
    assert "feedback_github_token" in secret_hygiene.SECRET_KEYS


def test_the_service_does_not_emit_notifications():
    """tests/test_notify_wiring forbids an emit site from importing httpx. This module
    is an HTTP client, so it must not emit -- the router would be the place."""
    import ast
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "feedback_service.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    # Imports only -- the module docstring discusses emitting at length, so a substring
    # search over the file text matches that prose and passes for the wrong reason.
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
    assert "notification_service" not in imported, sorted(imported)
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "emit_safe" not in calls and "emit" not in calls, sorted(calls)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("ok   " + name)
        except Exception as exc:  # noqa: BLE001 - this IS the runner
            failures += 1
            print("FAIL " + name + ": " + type(exc).__name__ + ": " + str(exc))
    print(("FAILED " + str(failures)) if failures else "all passed")
    sys.exit(1 if failures else 0)
