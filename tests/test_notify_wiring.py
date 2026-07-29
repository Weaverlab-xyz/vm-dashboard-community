"""Structural invariants for outbound notification wiring.

Everything here is a property no unit test can observe, because each one is about how
the pieces are CONNECTED rather than what a function returns:

  * **the emit path must never touch the network.** A notification that can block or
    raise inside ``job_service.set_failed`` — 221 call sites — is a notification that
    can fail the job it is reporting on. The guarantee is that emit sites only INSERT,
    and the only way to check that is at the source level;
  * **every emit call must sit inside a try/except.** Same reason, one level down;
  * **``reconcile_stale_jobs`` must emit.** It writes ``failed`` inline rather than
    through ``set_failed``, so without an explicit hook the one failure an operator most
    needs told — the worker died mid-provision — is the one that stays silent;
  * **the warn latch must be a write, not a hope.** If ``expiry_warned_at`` isn't
    committed in the same function that emits, two sweeps warn twice;
  * **the drain loop must actually be launched.** A loop nobody starts is the silent
    version of the feature not existing;
  * **the drain path must not touch job rows.** A delivery failure marking a job failed
    would be catastrophic and is entirely plausible as a future edit.

Static and stdlib-only, like tests/test_expiry_wiring.py, so it runs without fastapi or
sqlalchemy installed.

Run: python tests/test_notify_wiring.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SVC = os.path.join(_ROOT, "web_dashboard", "services")
_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")
_JOBSVC = os.path.join(_SVC, "job_service.py")
_REAPER = os.path.join(_SVC, "expiry_reaper.py")
_NOTIFY = os.path.join(_SVC, "notification_service.py")
_POLICY = os.path.join(_SVC, "notify_policy.py")
_TRANSPORTS = os.path.join(_SVC, "notify_transports.py")
_SCANNER = os.path.join(_SVC, "notify_scanner.py")
_EXPIRY_POLICY = os.path.join(_SVC, "expiry_policy.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")
_SETTINGS_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_DASH_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "dashboard.html")

_NETWORK_NAMES = ("httpx", "requests", "smtplib", "urllib", "aiohttp")


def _src(path):
    return open(path, encoding="utf-8").read()


def _fn(path, name):
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {os.path.basename(path)}")


def _names(fn):
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


def _imported(path):
    """Every module name imported anywhere in the file, top-level or function-local."""
    out = set()
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
            for a in node.names:
                out.add(a.name)
    return out


def _called_names(path):
    """Every function name actually CALLED in a file.

    Deliberately not a substring scan: these modules discuss job_service.set_failed in
    their docstrings precisely because they must not call it, and a grep would flag the
    explanation as the violation.
    """
    out = set()
    for node in ast.walk(ast.parse(_src(path))):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            out.add(f.attr)
        elif isinstance(f, ast.Name):
            out.add(f.id)
    return out


def _calls_to(path, dotted):
    """Every ``x.y(...)`` call node matching ``"x.y"``, as a list of ast.Call."""
    obj, _, attr = dotted.partition(".")
    out = []
    for node in ast.walk(ast.parse(_src(path))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == obj):
            out.append(node)
    return out


# ── the emit path never touches the network ──────────────────────────────────

def test_job_service_never_imports_a_network_client():
    """THE invariant, in its strongest form. If job_service can't reach the network,
    no edit to it can make a webhook time out inside a job's terminal transition."""
    imported = _imported(_JOBSVC)
    clash = imported & set(_NETWORK_NAMES)
    assert not clash, (
        f"job_service imports {clash} — the emit path must only INSERT. "
        "Sending belongs in notification_service, which runs in the worker's drain loop.")


def test_the_reaper_never_imports_a_network_client():
    """Same guarantee for the sweep. A dead webhook must not be able to stall a pass
    that is in the middle of destroying infrastructure."""
    clash = _imported(_REAPER) & set(_NETWORK_NAMES)
    assert not clash, f"expiry_reaper imports {clash} — it must only enqueue"


def test_the_emit_helpers_cannot_raise():
    """Every emit site calls one of these, and both must swallow everything. A
    notification failing is never a reason for the underlying operation to fail."""
    for path, name in ((_JOBSVC, "notify_job_failed"), (_REAPER, "_notify")):
        fn = _fn(path, name)
        handlers = [h for node in ast.walk(fn) if isinstance(node, ast.Try)
                    for h in node.handlers]
        assert handlers, f"{name} has no try/except — it can propagate into its caller"
        assert any(h.type is None or (isinstance(h.type, ast.Name)
                                      and h.type.id in ("Exception", "BaseException"))
                   for h in handlers), \
            f"{name} does not catch Exception broadly"
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
        assert not raises, f"{name} contains an explicit raise"


def test_emit_safe_is_what_the_call_sites_use():
    """``emit`` can raise; ``emit_safe`` is the one with the guarantee. A call site
    reaching past it would reintroduce exactly the risk this design removes."""
    for path in (_JOBSVC, _REAPER, _SCANNER):
        src = _src(path)
        assert "emit_safe" in src, f"{os.path.basename(path)} does not use emit_safe"
        bare = re.findall(r"notification_service\.emit\(", src)
        assert not bare, (
            f"{os.path.basename(path)} calls notification_service.emit directly; "
            "it must use emit_safe, which cannot raise")


def test_set_failed_emits_after_the_commit():
    """Ordering is load-bearing: the terminal status must already be committed, so the
    hook's rollback-on-error can never undo the transition it is reporting."""
    fn = _fn(_JOBSVC, "set_failed")
    body = ast.unparse(fn)
    assert "notify_job_failed" in body, "set_failed does not emit a notification"
    commit_at = body.index("db.commit()")
    notify_at = body.index("notify_job_failed")
    assert commit_at < notify_at, (
        "set_failed emits before committing — a failure in the hook could roll back "
        "the status write itself")


def test_reconcile_stale_jobs_emits():
    """It sets status='failed' inline rather than calling set_failed, so without this
    the "your worker died mid-provision" case notifies nobody. Exists purely so the
    bypass cannot silently reopen."""
    fn = _fn(_JOBSVC, "reconcile_stale_jobs")
    assert "notify_job_failed" in _names(fn), (
        "reconcile_stale_jobs writes failed inline and does not notify — a job killed "
        "by a restart would be the one failure nobody hears about")


# ── the auto-delete warning latch ────────────────────────────────────────────

def test_the_warn_latch_is_a_write_not_a_hope():
    """``expiry_warned_at`` has to be stamped AND committed in the same function that
    emits, or the next sweep — or a sibling worker — warns about the same resource
    again."""
    fn = _fn(_REAPER, "_warn_expiring")
    src = ast.unparse(fn)
    assert "expiry_warned_at" in src, "_warn_expiring never stamps the latch"
    assert "db.commit()" in src, "_warn_expiring stamps the latch without committing it"
    assert "_notify" in src, "_warn_expiring does not actually emit anything"


def test_the_latch_is_only_burned_when_something_was_queued():
    """A warning dropped because notifications are off, or no endpoint is configured,
    or storm control suppressed it, must be re-offered on the next sweep rather than
    marked as delivered forever."""
    src = ast.unparse(_fn(_REAPER, "_warn_expiring"))
    assert re.search(r"if not queued", src) or re.search(r"queued\s*:", src), (
        "_warn_expiring stamps expiry_warned_at unconditionally — a resource whose "
        "warning was never queued would never be warned about again")


def test_the_warn_pass_is_called_by_the_sweep():
    assert "_warn_expiring" in _names(_fn(_REAPER, "sweep_once")), (
        "sweep_once never calls _warn_expiring — the warning would never fire")


def test_expiry_policy_stays_pure():
    """expiry_policy is loaded by file path in its own test with no stubs beyond
    config_service. Reused here so a stray import added while wiring notifications
    fails loudly rather than at import time in production."""
    allowed = {"logging", "datetime", "timedelta", "typing", "Optional", "json",
               "config", "config_service", "settings"}
    extra = {m for m in _imported(_EXPIRY_POLICY)} - allowed
    assert not extra, f"expiry_policy grew impure imports: {extra}"


def test_notify_policy_is_pure():
    """Same contract for the notification policy: stdlib plus config_service, so it
    loads by file path in tests/test_notify_policy.py with no app installed."""
    allowed = {"hashlib", "logging", "dataclasses", "dataclass", "field",
               "datetime", "typing", "Optional", "json",
               "config", "config_service", "settings"}
    extra = _imported(_POLICY) - allowed
    assert not extra, f"notify_policy is no longer pure: {extra}"


# ── the drain loop ───────────────────────────────────────────────────────────

def test_the_drain_loop_is_actually_launched():
    """A drain loop nobody starts means every notification sits in the outbox forever
    while the UI cheerfully reports them as queued."""
    src = _src(_WORKER)
    assert "drain_loop" in src, "jobs_worker never references the notification drain loop"
    assert re.search(r"gather\(", src), (
        "jobs_worker.main no longer gathers the job runner with the drain loop")
    refs = _names(_fn(_WORKER, "_main"))
    assert "drain_loop" in refs and "_run_loop" in refs, (
        "_main must run both the job runner and the notification drain")
    assert "_main" in _names(_fn(_WORKER, "main"))


def test_delivery_is_not_a_job_type():
    """Deliberate: each worker runs one job at a time, so a delivery in the queue would
    park an alert behind a 30-minute terraform apply — and the apply behind a stalled
    POST. It also avoids a notify job's own completion re-entering the emit hook."""
    src = _src(_WORKER)
    assert '"notify"' not in src and '"notify_dispatch"' not in src, (
        "delivery has been made a job type; see this test's docstring for why it isn't")


def test_the_drain_path_never_touches_job_rows():
    """A failed webhook must never mark a job failed. Entirely plausible as a future
    edit, catastrophic if it lands."""
    called = _called_names(_NOTIFY)
    forbidden = called & {"set_failed", "set_completed", "set_running", "update_progress"}
    assert not forbidden, (
        f"notification_service calls {forbidden} — a delivery outcome must never be "
        "written onto a job row")


def test_the_delivery_table_has_a_unique_dedupe_constraint():
    """This constraint — not an in-process set — is what makes an event fire once
    across two gunicorn workers and three worker replicas."""
    src = _src(_DB)
    assert "notification_deliveries" in src, "the deliveries table is not declared"
    assert re.search(r'UniqueConstraint\(\s*["\']dedupe_key["\']', src), (
        "notification_deliveries has no UNIQUE on dedupe_key — nothing prevents five "
        "processes from each sending the same alert")


def test_an_integrity_error_on_the_dedupe_key_is_absorbed():
    """Losing the race is the constraint doing its job, not an error to propagate into
    whatever was being reported."""
    src = ast.unparse(_fn(_NOTIFY, "_insert"))
    assert "IntegrityError" in src, (
        "_insert does not handle IntegrityError — a lost dedupe race would raise into "
        "the emit site")
    assert "rollback" in src


def test_no_new_advisory_lock_id_is_taken():
    """20260101/2/3 are init_db, the audit chain and the expiry enqueue. The notify path
    has no cross-process read-modify-write to serialise, so taking one would add a
    deadlock surface for nothing."""
    for path in (_NOTIFY, _POLICY, _SCANNER, _TRANSPORTS):
        ids = set(re.findall(r"pg_advisory_xact_lock\((\d+)\)", _src(path)))
        ids |= set(re.findall(r"_LOCK_ID\s*=\s*(\d+)", _src(path)))
        assert not ids, (
            f"{os.path.basename(path)} takes advisory lock(s) {ids}; if this is now "
            "needed, pick an unused id and extend test_expiry_wiring's collision scan")


# ── dependency direction ─────────────────────────────────────────────────────

def test_the_notification_services_do_not_import_from_the_api_layer():
    """These run in the worker, which never builds a FastAPI app. An ``..api`` import
    would drag routers — and fastapi itself — into the worker process."""
    for path in (_NOTIFY, _POLICY, _TRANSPORTS, _SCANNER):
        src = _src(path)
        assert "from ..api" not in src and "from web_dashboard.api" not in src, (
            f"{os.path.basename(path)} imports from the api layer")


def test_the_scanner_uses_service_collectors_not_routers():
    """The gathering for secret staleness and config drift was lifted out of the
    routers precisely so the worker could call it. This pins the lift."""
    src = _src(_SCANNER)
    assert "secret_hygiene.collect" in src, (
        "the scanner does not use secret_hygiene.collect — it must not re-implement "
        "the gathering that api/secrets.py used to own")
    assert "config_drift.collect" in src


def test_the_extracted_collectors_exist_and_the_routers_use_them():
    """If a route stops delegating, the router and the scanner drift apart and start
    reporting different numbers for the same condition."""
    sh = _src(os.path.join(_SVC, "secret_hygiene.py"))
    cd = _src(os.path.join(_SVC, "config_drift.py"))
    assert "def collect(" in sh and "async def collect(" in cd
    assert "secret_hygiene.collect" in _src(
        os.path.join(_ROOT, "web_dashboard", "api", "secrets.py"))
    assert "config_drift.collect" in _src(
        os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py"))


def test_the_pure_modules_keep_module_level_imports_stdlib_only():
    """tests/test_secret_hygiene.py and tests/test_config_drift.py load these by file
    path with NO stubs at all, so a top-level ``from ..database import …`` breaks them.
    collect() keeps its imports function-local for exactly this reason."""
    for name in ("secret_hygiene.py", "config_drift.py"):
        tree = ast.parse(_src(os.path.join(_SVC, name)))
        for node in tree.body:                     # top level only
            if isinstance(node, ast.ImportFrom):
                assert not (node.level or 0), (
                    f"{name} has a top-level relative import — it must stay loadable "
                    "by file path with no package context")


# ── secrets never leave the process ──────────────────────────────────────────

def test_the_endpoint_url_and_secret_are_never_returned_by_the_api():
    """A Slack or Teams webhook URL is a bearer credential: whoever has it can post to
    the channel. The public shape carries a hint and a boolean."""
    src = ast.unparse(_fn(_NOTIFY, "endpoint_public"))
    assert "url_hint" in src and "redact_url" in src
    assert "has_secret" in src
    assert "endpoint_secret" not in src, "endpoint_public exposes the decrypted secret"
    assert not re.search(r'["\']url["\']\s*:\s*endpoint_url', src), (
        "endpoint_public returns the full URL")


def test_the_stored_url_and_secret_are_encrypted():
    src = _src(_NOTIFY)
    assert "encrypt_value" in src, "endpoint URLs are stored in plain text"
    assert "decrypt_value" in src


def test_the_delivery_log_does_not_carry_the_endpoint_url():
    """The payload is stored for reproducibility; the credential is in the URL and the
    signature header, and neither belongs in a row the UI renders."""
    src = ast.unparse(_fn(_NOTIFY, "delivery_public"))
    assert "endpoint_url" not in src and "endpoint_secret" not in src


def test_every_http_client_passes_verify_and_trust_env():
    """httpx verifies against certifi and ignores SSL_CERT_FILE / REQUESTS_CA_BUNDLE,
    both of which the image sets and the corp-CA overlay mounts over. Without an
    explicit bundle, every Slack/Teams POST fails behind a TLS-inspecting proxy while
    everything else in the same container works."""
    clients = _calls_to(_TRANSPORTS, "httpx.AsyncClient")
    assert clients, "no httpx client found in notify_transports"
    for call in clients:
        kw = {k.arg: k.value for k in call.keywords}
        assert "verify" in kw, "an httpx client omits verify= (see this test's docstring)"
        assert "trust_env" in kw, "an httpx client omits trust_env= (corp proxy)"
        assert isinstance(kw.get("follow_redirects"), ast.Constant) \
            and kw["follow_redirects"].value is False, (
            "a signed body must not be replayable to a redirect target")


def test_the_signature_is_computed_over_posted_bytes():
    """Signing one serialisation and posting another with ``json=`` is the classic HMAC
    bug: every receiver's verification fails and nobody can tell why."""
    src = ast.unparse(_fn(_TRANSPORTS, "post"))
    assert "content=raw" in src.replace(" ", ""), (
        "post() must send the exact signed bytes via content=")
    assert "json=" not in src, "post() uses json=, which re-serialises past the signature"


# ── the feature is off, and quiet, by default ────────────────────────────────

def test_the_feature_ships_off_and_in_dry_run():
    """Off is not the only brake: enabling notifications against a live estate could
    produce hundreds of messages in the first pass."""
    cfg = _src(os.path.join(_ROOT, "web_dashboard", "config.py"))
    assert re.search(r"notifications_enabled:\s*bool\s*=\s*False", cfg)
    assert re.search(r"notify_dry_run:\s*bool\s*=\s*True", cfg)
    setup = _src(os.path.join(_ROOT, "web_dashboard", "api", "setup.py"))
    assert "NotificationsFeatureConfig" in setup
    assert '"notifications":' in setup, "the panel is not registered in _FEATURE_MODELS"


def test_no_new_runtime_dependency_was_added():
    """CONTRIBUTING is explicit that dependencies are a cost. stdlib plus the already
    pinned httpx covers all three payload formats."""
    reqs = _src(os.path.join(_ROOT, "web_dashboard", "requirements.txt")).lower()
    for banned in ("aiosmtplib", "slack_sdk", "slack-sdk", "pymsteams", "tenacity",
                   "apprise"):
        assert banned not in reqs, f"{banned} was added to requirements.txt"


# ── the docs and the UI no longer lie ────────────────────────────────────────

def test_the_ui_no_longer_claims_there_is_no_notification():
    """Two places asserted, in operator-visible copy and in a load-bearing comment,
    that this feature did not exist. Both become false the moment this ships."""
    for path in (_SETTINGS_HTML, _DASH_HTML):
        src = _src(path)
        assert "There is no email or chat notification" not in src, (
            f"{os.path.basename(path)} still tells the operator notifications do not exist")
        assert "there is no email or chat notification" not in src


def test_the_settings_panel_is_registered_in_the_ui():
    src = _src(_SETTINGS_HTML)
    assert "key: 'notifications'" in src, "no entry in the integrations list"
    assert "panel?.key === 'notifications'" in src, "no slide-over panel block"


def test_the_feature_is_documented():
    doc = os.path.join(_ROOT, "docs", "notifications.md")
    assert os.path.isfile(doc), "docs/notifications.md is missing"
    src = _src(doc).lower()
    assert "outbound" in src, (
        "the doc must say outbound only — docs/saas-roadmap.md reserves the inbound "
        "webhook endpoint for the hosted edition")
    # The three Teams facts that otherwise generate support tickets.
    assert "power automate" in src
    assert "202" in src
    assert "at-least-once" in src


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
