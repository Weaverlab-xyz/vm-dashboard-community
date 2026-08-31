"""ps_dbops — the Password Safe GCP ``cloud-run`` channel's DB-Ops service.

The workload is a credential-changing endpoint reached from outside the project, so
the properties worth pinning are the ones whose failure is silent:

  * the instance allowlist FAILS CLOSED, and an empty one is not "allow everything"
  * DDL quoting is per-engine and complete (T-SQL doubles ', MySQL binds parameters)
  * a MySQL account with no host qualifier is REFUSED, never defaulted
  * self-rotation uses USER(), not the parsed name
  * the unimplemented contract answers 501 and names the versions it serves — it
    does not fall through to an operation

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


# ── Routing and the unimplemented contract ────────────────────────────────────

def test_health_probe_touches_no_database_and_reports_the_contract():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="proj:us-east1:db-a")
    resp = ps_dbops.handle(_req(method="GET", path="/"), _ctx())
    assert resp.status == 200, resp
    assert resp.body["supported_contract_versions"] == [1], resp.body
    assert resp.body["contract_implemented"] is False, resp.body
    assert resp.body["allowed_instances"] == 1, resp.body


def test_credential_op_answers_501_with_the_versions_it_serves():
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    resp = ps_dbops.handle(_req(body={"anything": 1}), _ctx())
    assert resp.status == 501, resp
    assert resp.body["supported_contract_versions"] == [1], resp.body


def test_the_seam_never_falls_through_to_an_operation():
    """The one behaviour that must not regress when the parser is written: an
    unrecognised request must not reach _run with a half-filled dict."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*")
    try:
        ps_dbops._parse_credential_op({}, 1)
    except ps_dbops.ContractNotImplemented:
        return
    raise AssertionError("_parse_credential_op returned instead of raising")


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
    """DbOpsPath is a broker setting, so the service has to be able to follow it."""
    _reset_env(FN_DBOPS_ALLOWED_INSTANCES="*", FN_DBOPS_PATH="/custom/op")
    assert ps_dbops.handle(_req(path="/custom/op"), _ctx()).status == 501
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
