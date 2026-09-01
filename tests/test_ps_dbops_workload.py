"""ps_dbops — the Password Safe GCP ``cloud-run`` channel's DB-Ops service.

The workload is a credential-changing endpoint reached from outside the project, so
the properties worth pinning are the ones whose failure is silent:

  * the instance allowlist FAILS CLOSED, and an empty one is not "allow everything"
  * DDL quoting is per-engine and complete (T-SQL doubles ', MySQL binds parameters)
  * a MySQL account with no host qualifier is REFUSED, never defaulted
  * self-rotation uses USER(), not the parsed name
  * the v1 contract parser refuses every malformed shape rather than half-filling an
    operation, and maps each database failure onto the plan's own status/code table
  * no credential reaches a response body or a log line, structurally

Stdlib only — runs with nothing installed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request
from fnworkloads import ps_dbops

_ENV_KEYS = ("FN_DBOPS_ALLOWED_INSTANCES", "FN_DBOPS_AUDIENCE", "FN_DBOPS_CAPTURE",
             "FN_DBOPS_PATH", "FN_DBOPS_ALLOWED_INVOKERS")


def _reset_env(**overrides):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    for key, val in overrides.items():
        os.environ[key] = val


def _req(method="POST", path="/v1/credential-op", body=None, headers=None):
    return Request(method=method, path=path, headers=headers or {}, query={},
                   body=json.dumps(body or {}).encode() if body is not None else b"",
                   source="gcp")


def _ctx():
    return Context.from_env(workload="ps_dbops")


# One valid envelope per operation, per plan §2. Built as fixtures because most of
# these tests are about ONE field being wrong, and repeating the other nine obscures it.
_OP = {
    "contractVersion": 1,
    "requestId": "b7f0",
    "engine": "sqlserver",
    "operation": "verify",
    "instanceConnectionName": "proj:us-central1:mssql-core-01",
    "privateIp": "10.20.0.7",
    "port": 1433,
    "database": "master",
    "loginUser": "bt_rotator",
    "loginPassword": "rot-pw",
    "timeoutSeconds": 120,
}
_CHANGE = dict(_OP, operation="change", targetUser="app_login", newPassword="new-pw")
_SELF = dict(_OP, operation="change-self", targetUser="app_login",
             loginUser="app_login", loginPassword="old-pw",
             newPassword="new-pw", currentPassword="old-pw")


def _without(payload, *keys):
    """``payload`` minus ``keys`` — the shape of a request missing a required field."""
    return {k: v for k, v in payload.items() if k not in keys}


def _raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ps_dbops.DbOpsError as exc:
        return str(exc)
    raise AssertionError(f"{getattr(fn, '__name__', fn)} did not raise DbOpsError")


# ── Declarations the deploy path reads ────────────────────────────────────────

def test_declares_the_oidc_gate_and_is_not_an_entitle_adapter():
    # AUTH_MODE is what stops fnruntime.auth comparing a Google ID token against the
    # dashboard's shared secret and 401-ing every request.
    assert ps_dbops.AUTH_MODE == "gcp_oidc", ps_dbops.AUTH_MODE
    # And this one keeps "Register in Entitle" off a workload that does not serve
    # that contract — a live integration that can never resolve an asset is visible
    # only inside Entitle.
    assert ps_dbops.ENTITLE_ADAPTER is False


def test_nothing_is_required_env_and_that_is_deliberate():
    """The audience cannot be required — it is the service's own URL, which does not
    exist until the first apply has finished. The allowlist is not required either:
    standing the service up before the first database in a region is a legitimate
    order to work in, and an unset allowlist makes the service refuse every request
    and name the setting, rather than sit there inert. Requiring either would make
    the deploy impossible rather than safe."""
    assert ps_dbops.REQUIRED_ENV == (), ps_dbops.REQUIRED_ENV
    # …and the refusal is real, not a comment:
    _reset_env()
    assert _raises(ps_dbops.check_instance, "proj:us-east1:db-a")


# ── Instance admission ────────────────────────────────────────────────────────

def test_empty_allowlist_refuses_everything():
    _reset_env()
    message = _raises(ps_dbops.check_instance, "proj:us-east1:db-a")
    assert "FN_DBOPS_ALLOWED_INSTANCES" in message, message


def test_allowlist_admits_only_listed_instances():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="proj:us-east1:db-a,proj:us-east1:db-b")
    assert ps_dbops.check_instance("proj:us-east1:db-b") == "proj:us-east1:db-b"
    message = _raises(ps_dbops.check_instance, "proj:us-east1:db-c")
    assert "allowlist" in message, message


def test_wildcard_is_an_explicit_opt_out():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    assert ps_dbops.check_instance("other:europe-west1:db-z")


def test_malformed_connection_name_is_refused_before_the_allowlist():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    for bad in ("db-a", "proj:db-a", "proj:us-east1:db:a", "proj;us-east1;db"):
        message = _raises(ps_dbops.check_instance, bad)
        assert "project:region:instance" in message, (bad, message)


# ── Statement construction ────────────────────────────────────────────────────

def test_sqlserver_change_doubles_the_quote():
    """A password containing a single quote must not end the literal. T-SQL has no
    backslash escape, so doubling is the complete rule."""
    stmts = ps_dbops.change_password_statements(
        "sqlserver", principal="app", new_password="a'b--x")
    assert stmts == ["ALTER LOGIN [app] WITH PASSWORD = 'a''b--x';"], stmts


def test_sqlserver_bracket_in_a_principal_is_doubled():
    stmts = ps_dbops.change_password_statements(
        "sqlserver", principal="we]ird", new_password="pw")
    assert stmts[0].startswith("ALTER LOGIN [we]]ird]"), stmts


def test_sqlserver_self_rotation_uses_old_password():
    """The whole point of self-rotation on this channel: with OLD_PASSWORD the login
    alters itself and the functional account needs ALTER ANY LOGIN over nothing."""
    stmts = ps_dbops.change_password_statements(
        "sqlserver", principal="app", new_password="new", old_password="old")
    assert "OLD_PASSWORD = 'old'" in stmts[0], stmts


def test_mysql_binds_parameters_rather_than_interpolating():
    """MySQL's escaping depends on NO_BACKSLASH_ESCAPES, so a hand-rolled escape is
    wrong in one of the two modes. The driver's own binding is correct in both."""
    stmts = ps_dbops.change_password_statements(
        "mysql", principal="app@%", new_password="p'w\\x")
    assert stmts == [("ALTER USER %s@%s IDENTIFIED BY %s", ("app", "%", "p'w\\x"))], stmts


def test_mysql_self_rotation_uses_user_function_not_the_name():
    """'app'@'%' and 'app'@'10.0.0.5' are different accounts. On a self-rotation the
    session already IS the right one, so naming it again could rotate the other."""
    stmts = ps_dbops.change_password_statements(
        "mysql", principal="app@%", new_password="new", old_password="old")
    assert stmts[0][0] == "ALTER USER USER() IDENTIFIED BY %s", stmts


def test_mysql_account_without_a_host_qualifier_is_refused():
    message = _raises(ps_dbops.change_password_statements, "mysql",
                      principal="app", new_password="pw")
    assert "host qualifier" in message, message


def test_postgres_quotes_both_halves():
    stmts = ps_dbops.change_password_statements(
        "postgres", principal='we"ird', new_password="a'b")
    assert stmts == ['ALTER ROLE "we""ird" WITH PASSWORD \'a\'\'b\';'], stmts


def test_control_characters_are_refused_in_names_and_passwords():
    _raises(ps_dbops.change_password_statements, "sqlserver",
            principal="ap\np", new_password="pw")
    _raises(ps_dbops.change_password_statements, "sqlserver",
            principal="app", new_password="p\x00w")


def test_no_password_character_allowlist():
    """Password Safe generates the password and its alphabet is the customer's
    policy. Rejecting an unusual character would mean refusing to apply a change
    Password Safe has already recorded — the split-brain this channel exists to
    avoid."""
    stmts = ps_dbops.change_password_statements(
        "sqlserver", principal="app", new_password="~!@#$%^&*()_+{}|:<>?`")
    assert "~!@#$%^&*()_+{}|:<>?`" in stmts[0], stmts


def test_unsupported_engine_is_named():
    message = _raises(ps_dbops.change_password_statements, "oracle",
                      principal="app", new_password="pw")
    assert "oracle" in message, message


def test_postgres_connect_names_the_reason_rather_than_crashing():
    """No Postgres driver is vendored — postgres has a channel that needs no service
    at all. The failure has to be a sentence, not a NameError three frames down."""
    message = _raises(ps_dbops._connect, "postgres", host="h", port=5432,
                      database="d", user="u", password="p")
    assert "data-api" in message, message


# ── Routing and the v1 contract ───────────────────────────────────────────────

def test_health_probe_touches_no_database_and_reports_the_contract():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="proj:us-east1:db-a")
    resp = ps_dbops.handle(_req(method="GET", path="/"), _ctx())
    assert resp.status == 200, resp
    assert resp.body["supported_contract_versions"] == [1], resp.body
    assert resp.body["contract_implemented"] is True, resp.body
    assert resp.body["operations"] == list(ps_dbops._OPERATIONS), resp.body
    assert resp.body["allowed_instances"] == 1, resp.body


def test_an_unserved_contract_version_is_501_not_400():
    """A version this build does not serve is not a malformed request — the address's
    ver= option exists precisely so one service can be asked for another one, and 501
    naming what it CAN serve is what makes that usable."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    resp = ps_dbops.handle(_req(body=dict(_OP, contractVersion=7)), _ctx())
    assert resp.status == 501, resp
    assert resp.body["supported_contract_versions"] == [1], resp.body
    assert resp.body["success"] is False, resp.body


def test_a_garbage_contract_version_is_400_not_501():
    """501 says "ask a different service"; this request is simply malformed."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    resp = ps_dbops.handle(_req(body=dict(_OP, contractVersion="nonsense")), _ctx())
    assert resp.status == 400, resp
    assert resp.body["code"] == "BAD_REQUEST", resp.body


def test_the_parser_never_half_fills_an_operation():
    """The behaviour that mattered while this was a seam and still matters now: every
    malformed shape has to raise, not return a dict with holes in it that _run then
    dereferences into a rotation."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    bad = (
        {},                                                     # empty body
        dict(_OP, operation="drop-database"),                   # unknown operation
        dict(_OP, operation=""),                                # no operation
        dict(_OP, engine="oracle"),                             # unknown engine
        _without(_OP, "instanceConnectionName"),                # no target
        dict(_OP, instanceConnectionName="not-a-connection"),   # malformed target
        _without(_OP, "privateIp"),                             # unresolvable address
        _without(_OP, "loginPassword"),                         # no credential
        dict(_OP, port="eighty"),                               # non-integer port
        _without(_CHANGE, "targetUser"),                        # nothing to change
        _without(_CHANGE, "newPassword"),                       # nothing to change it to
        _without(_SELF, "currentPassword"),                     # no OLD_PASSWORD
    )
    for payload in bad:
        try:
            ps_dbops._parse_credential_op(payload, 1)
        except ps_dbops.DbOpsError:
            continue
        raise AssertionError(f"parser accepted {payload!r}")


def test_postgres_is_refused_by_name_at_parse_time():
    """Not "no module named pg8000" three layers down: postgres has its own channel
    that needs no service at all, and the message says so."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    message = _raises(ps_dbops._parse_credential_op, dict(_OP, engine="postgres"), 1)
    assert "data-api" in message, message


def test_verify_authenticates_as_loginuser_not_targetuser():
    """Plan §4: verify is "open a connection as loginUser" and the connection IS the
    proof. Reading targetUser here would test the wrong credential and report a pass
    for an account nobody checked."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    parsed = ps_dbops._parse_credential_op(
        dict(_OP, loginUser="bt_rotator", loginPassword="rot-pw",
             targetUser="app_login"), 1)
    assert parsed["as_user"] == "bt_rotator", parsed
    assert parsed["as_password"] == "rot-pw", parsed


def test_change_self_defaults_the_login_to_the_target_and_refuses_a_mismatch():
    """ALTER LOGIN ... OLD_PASSWORD only works when the SESSION is that login, so the
    authenticating identity defaults to the target with its current password — and a
    request naming a different one is refused here rather than sent, because SQL Server
    answers it with a permission error and a permission error on a self-rotation is the
    most misleading failure this service could return."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    parsed = ps_dbops._parse_credential_op(_without(_SELF, "loginUser",
                                                    "loginPassword"), 1)
    assert parsed["as_user"] == parsed["principal"] == "app_login", parsed
    assert parsed["as_password"] == "old-pw", parsed
    assert parsed["old_password"] == "old-pw", parsed
    message = _raises(ps_dbops._parse_credential_op,
                      dict(_SELF, loginUser="someone_else"), 1)
    assert "change" in message, message


def test_change_carries_no_old_password():
    """The OLD_PASSWORD form is what makes change-self work without ALTER ANY LOGIN.
    Leaking it into a plain change would add a precondition the operation does not
    have, and fail a rotation whenever Password Safe's stored value has drifted."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    parsed = ps_dbops._parse_credential_op(dict(_CHANGE, currentPassword="old-pw"), 1)
    assert parsed["old_password"] == "", parsed


def test_prehashed_is_422_not_400_and_the_hashed_form_is_not_built():
    """A well-formed request whose COMBINATION is refused. The plan ships the verifier
    flag off because a wrong salt length produces an ALTER that succeeds while leaving
    a login nobody can authenticate as — after Password Safe has recorded the new
    password as authoritative. So there is no flag here at all."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    resp = ps_dbops.handle(_req(body=dict(_CHANGE, passwordFormat="prehashed")), _ctx())
    assert resp.status == 422, resp
    assert resp.body["code"] == "UNSUPPORTED_COMBINATION", resp.body
    # And the statement form itself does not exist -- checked against the builders
    # rather than the whole module, whose prose explains why it does not.
    import inspect
    for fn in (ps_dbops.change_password_statements, ps_dbops.list_accounts_statement,
               ps_dbops.server_version_statement):
        assert "HASHED" not in inspect.getsource(fn), fn.__name__


def test_the_allowlist_still_gates_the_parsed_request():
    """The parser is now the first thing that touches the target, so the fail-closed
    boundary has to live inside it — not only in the operations it hands off to."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="proj:us-east1:other")
    message = _raises(ps_dbops._parse_credential_op, _OP, 1)
    assert "allowlist" in message, message


def test_timeout_seconds_is_clamped_not_trusted():
    assert ps_dbops.connect_timeout(0) == ps_dbops._CONNECT_TIMEOUT
    assert ps_dbops.connect_timeout("nonsense") == ps_dbops._CONNECT_TIMEOUT
    assert ps_dbops.connect_timeout(120) == 120
    assert ps_dbops.connect_timeout(1) == ps_dbops._MIN_TIMEOUT
    assert ps_dbops.connect_timeout(86400) == ps_dbops._MAX_TIMEOUT


# ── The plan's status/code table ──────────────────────────────────────────────

def test_a_rejected_credential_is_401_not_a_generic_failure():
    """Plan §2 calls this "a legitimate Verify 'false', not an infrastructure failure".
    Collapsing it into a 500 would make every wrong password look like a broken VPC."""
    exc = Exception("Login failed for user 'app_login'.")
    exc.number = 18456
    status, code, _ = ps_dbops._classify(exc)
    assert (status, code) == (401, "DB_AUTH_FAILED")


def test_each_documented_failure_maps_to_its_own_code():
    for number, expected in ((15007, (404, "PRINCIPAL_NOT_FOUND")),
                             (1396, (404, "PRINCIPAL_NOT_FOUND")),
                             (1142, (403, "DB_PERMISSION_DENIED")),
                             (15118, (409, "POLICY_REJECTED")),
                             (1819, (409, "POLICY_REJECTED")),
                             (1045, (401, "DB_AUTH_FAILED"))):
        exc = Exception("boom")
        exc.number = number
        status, code, _ = ps_dbops._classify(exc)
        assert (status, code) == expected, (number, status, code)


def test_sql_servers_ambiguous_15151_says_it_is_ambiguous():
    """SQL Server reports "does not exist or you do not have permission" as ONE error
    and refuses to distinguish them, so that an unprivileged caller cannot enumerate
    logins. Picking 403 silently would send an operator hunting a missing login."""
    exc = Exception("Cannot alter the login 'app_login', because it does not exist "
                    "or you do not have permission.")
    exc.number = 15151
    status, code, detail = ps_dbops._classify(exc)
    assert (status, code) == (403, "DB_PERMISSION_DENIED")
    assert "does not distinguish" in detail, detail
    assert "CustomerDbRootRole" in detail, detail


def test_a_timeout_is_504_and_an_unreachable_instance_is_502():
    assert ps_dbops._classify(Exception("Login timeout expired"))[0] == 504
    assert ps_dbops._classify(OSError("connection refused"))[0] == 502
    assert ps_dbops._classify(Exception("getaddrinfo failed"))[0] == 502


def test_an_unmapped_failure_does_not_borrow_a_documented_code():
    """Forcing an unrecognised error into the table would send the plugin down a branch
    the failure does not justify. An unknown code degrades to "it failed", which is the
    only true statement available."""
    status, code, _ = ps_dbops._classify(Exception("something entirely new"))
    assert status == 500 and code == "DB_ERROR", (status, code)


def test_the_error_number_is_read_from_whichever_attribute_the_driver_used():
    for attr in ("number", "msg_no", "errno", "code"):
        exc = Exception("boom")
        setattr(exc, attr, 18456)
        assert ps_dbops._error_number(exc) == 18456, attr
    # pymysql puts it in args[0]
    assert ps_dbops._error_number(Exception(1045, "Access denied")) == 1045
    assert ps_dbops._error_number(Exception("no number here")) == 0


# ── Nothing credential-shaped leaves this service ─────────────────────────────

def test_a_driver_error_that_echoes_the_password_is_scrubbed():
    """Structural, not best-effort (plan §8). The drivers are third-party and an error
    message embedding the value it rejected is exactly the thing discovered after a
    month of it being in Cloud Logging. The passwords are known here, so this is a
    substring replace rather than a pattern guess."""
    detail = ps_dbops._scrub("password 'Sup3rSecret!' was rejected",
                             ["Sup3rSecret!", ""])
    assert "Sup3rSecret" not in detail, detail
    assert "***" in detail, detail
    # A short value is left alone: replacing "abc" everywhere would corrupt the very
    # message an operator needs, and a 3-character password is not the risk here.
    assert ps_dbops._scrub("error at abc", ["abc"]) == "error at abc"


def test_no_response_body_carries_a_credential():
    """Every refusal path, not just the happy one. A parse-time refusal has no
    statement to report, so it carries no statementKind — but it must still never echo
    a password back at the caller."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    for body in (dict(_CHANGE, passwordFormat="prehashed"),
                 _without(_CHANGE, "targetUser"),
                 dict(_CHANGE, instanceConnectionName="nope"),
                 dict(_SELF, loginUser="someone_else")):
        rendered = json.dumps(ps_dbops.handle(_req(body=body), _ctx()).body)
        assert "new-pw" not in rendered, rendered
        assert "rot-pw" not in rendered, rendered
        assert "old-pw" not in rendered, rendered


def test_a_change_reports_the_statement_kind_and_never_the_statement():
    """statementKind is what makes a failure diagnosable without putting the statement
    -- which carries the new password -- in a response body or a log line."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    calls = {}

    def _fake_change(target, **kw):
        calls.update(kw)
        return 1

    real = ps_dbops.change_password
    ps_dbops.change_password = _fake_change
    try:
        resp = ps_dbops.handle(_req(body=_CHANGE), _ctx())
    finally:
        ps_dbops.change_password = real
    assert resp.status == 200, resp
    assert resp.body["success"] is True, resp.body
    assert resp.body["code"] == "OK", resp.body
    assert resp.body["statementKind"] == "ALTER_LOGIN", resp.body
    assert resp.body["statementsExecuted"] == 1, resp.body
    # The operation really was handed the parsed identities, not the target's own.
    assert calls["as_user"] == "bt_rotator" and calls["principal"] == "app_login", calls
    assert "new-pw" not in json.dumps(resp.body), resp.body


def test_a_driver_failure_becomes_the_plans_code_not_a_stack_trace():
    """An unclassified driver exception escaping would be rendered by the runtime as a
    500 with a traceback, and a traceback out of a credential-handling path is the one
    thing the plan's security section rules out."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")

    def _boom(target, **kw):
        exc = Exception("Login failed for user 'bt_rotator'.")
        exc.number = 18456
        raise exc

    real = ps_dbops.change_password
    ps_dbops.change_password = _boom
    try:
        resp = ps_dbops.handle(_req(body=_CHANGE), _ctx())
    finally:
        ps_dbops.change_password = real
    assert resp.status == 401, resp
    assert resp.body["code"] == "DB_AUTH_FAILED", resp.body
    assert resp.body["success"] is False, resp.body
    assert resp.body["statementKind"] == "ALTER_LOGIN", resp.body


def test_a_verify_returns_the_server_version():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    real = ps_dbops.verify_credential
    ps_dbops.verify_credential = lambda target, **kw: "Microsoft SQL Server 2022"
    try:
        resp = ps_dbops.handle(_req(body=_OP), _ctx())
    finally:
        ps_dbops.verify_credential = real
    assert resp.status == 200, resp
    assert resp.body["serverVersion"] == "Microsoft SQL Server 2022", resp.body
    assert resp.body["statementKind"] == "CONNECT", resp.body


def test_list_accounts_returns_names_the_plugin_can_rotate():
    """On MySQL an account IS user@host — returning bare names would hand back
    accounts that cannot be rotated, which is the round-trip the plugin depends on."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    assert "sys.sql_logins" in ps_dbops.list_accounts_statement("sqlserver")
    assert "##%" in ps_dbops.list_accounts_statement("sqlserver"), \
        "internal ##MS_*## logins are not rotatable accounts"
    assert "cloudsqlsa" in ps_dbops.list_accounts_statement("sqlserver"), \
        "Google's own management login is not a rotatable account"
    assert "mysql.user" in ps_dbops.list_accounts_statement("mysql")
    assert "host" in ps_dbops.list_accounts_statement("mysql")


def test_every_response_carries_the_plans_envelope():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    resp = ps_dbops.handle(_req(body=dict(_OP, operation="nope")), _ctx())
    for key in ("contractVersion", "requestId", "success", "code", "detail",
                "elapsedMs"):
        assert key in resp.body, (key, resp.body)
    assert resp.body["contractVersion"] == 1, resp.body


def test_the_request_id_is_echoed_for_log_correlation():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    resp = ps_dbops.handle(_req(body=dict(_OP, requestId="b7f0abc", operation="nope")),
                           _ctx())
    assert resp.body["requestId"] == "b7f0abc", resp.body


def test_contract_version_is_read_from_the_body_not_assumed():
    assert ps_dbops._contract_version({}) == 1
    assert ps_dbops._contract_version({"contractVersion": 2}) == 2
    assert ps_dbops._contract_version({"ver": "3"}) == 3
    # A garbage version must not silently become 1 and get served.
    assert ps_dbops._contract_version({"ver": "nonsense"}) == -1


def test_wrong_method_and_wrong_path_are_distinguishable():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    assert ps_dbops.handle(_req(method="GET", path="/v1/credential-op"),
                           _ctx()).status == 405
    resp = ps_dbops.handle(_req(path="/v1/nope"), _ctx())
    assert resp.status == 404, resp
    # Named, because a 404 reached over a custom audience is otherwise
    # indistinguishable from the audience pointing at a different service entirely.
    assert "/v1/credential-op" in resp.body["error"], resp.body


def test_the_contract_path_is_overridable():
    """DbOpsPath is a broker setting, so the service has to be able to follow it. The
    body is deliberately empty: what is under test is which path is SERVED, and a 400
    from the parser proves the request reached it."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*", FN_DBOPS_PATH="/custom/op")
    assert ps_dbops.handle(_req(path="/custom/op"), _ctx()).status == 400
    assert ps_dbops.handle(_req(path="/v1/credential-op"), _ctx()).status == 404


def test_capture_never_logs_the_authorization_header():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*", FN_DBOPS_CAPTURE="1")
    import io
    import contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        ps_dbops.handle(_req(body={"password": "hunter2", "user": "app"},
                             headers={"authorization": "Bearer eyJhbGciOi.SECRET.sig"}),
                        _ctx())
    written = buffer.getvalue()
    assert "dbops_contract_capture" in written, written
    assert "SECRET" not in written, written
    assert "hunter2" not in written, written
    # …but the non-credential fields ARE captured, or the whole exercise is pointless.
    assert '"user": "app"' in written, written


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
