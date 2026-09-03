"""SQL Server connections encrypt, on every flavor, without a CA bundle to verify.

The bug this pins: ``pytds.connect`` derives its pre-login encryption flag from
``cafile`` alone, so passing ``None`` — which is what every caller did, ``FN_DB_CAFILE``
being set nowhere in this repo — advertises ENCRYPT_NOT_SUP. Azure SQL then answers
ENCRYPT_REQ and python-tds refuses; RDS and Cloud SQL answer ENCRYPT_OFF and
python-tds dives into ``establish_channel`` with no context and blows up. Either way
``db_grant``'s create_actor came back 500 "internal error" on the first real Entitle
grant, and no offline test noticed because dry run — the default — opens no
connection at all.

python-tds is faked here rather than installed: it is vendored into the function
package (cloud_function_package._WORKLOAD_VENDOR), not into the dashboard's own
requirements, so the *arguments* are what these tests can and should pin. Runs
entirely offline.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import tds


def _fake_pytds(openssl_available=True):
    """A stand-in for the vendored driver that follows the same branch it does:
    ``connect`` builds a context from ``cafile`` through ``tls.create_context``, so a
    test can see WHICH builder answered."""
    module = types.ModuleType("pytds")
    tls_module = types.ModuleType("pytds.tls")
    tls_module.OPENSSL_AVAILABLE = openssl_available
    tls_module.builder_calls = []

    def create_context(cafile):
        tls_module.builder_calls.append(cafile)
        return ("pytds-own-context", cafile)

    tls_module.create_context = create_context
    module.tls = tls_module
    module.calls = []

    def connect(**kwargs):
        # pytds only builds a context when cafile is truthy; that IS the flag.
        kwargs["tls_ctx"] = (module.tls.create_context(kwargs["cafile"])
                             if kwargs.get("cafile") else None)
        module.calls.append(kwargs)
        return kwargs

    module.connect = connect
    sys.modules["pytds"] = module
    sys.modules["pytds.tls"] = tls_module
    return module


def _trusting_sentinel(module):
    """Replace the real pyOpenSSL context builder — it is vendored per function, not
    installed here — so the ROUTING can be tested with no TLS stack present."""
    original = tds._trusting_context
    tds._trusting_context = lambda: ("trusting-context", module)
    return original


def _connect(cafile="", **overrides):
    if cafile:
        os.environ["FN_DB_CAFILE"] = cafile
    else:
        os.environ.pop("FN_DB_CAFILE", None)
    module = _fake_pytds()
    restore = _trusting_sentinel(module)
    try:
        kwargs = dict(host="sql.internal", port=1433, database="appdb",
                      user="dbadmin", password="s3cret", timeout=10)
        kwargs.update(overrides)
        tds.connect(**kwargs)
    finally:
        tds._trusting_context = restore
    return module


# ── The regression ───────────────────────────────────────────────────────────

def test_a_connection_asks_for_encryption_even_with_no_ca_bundle():
    """The whole bug in one assertion. A falsy cafile is python-tds's ONLY way of
    saying "do not encrypt", and no managed SQL Server here accepts that."""
    module = _connect()
    assert module.calls, "pytds.connect was never called"
    assert module.calls[0]["cafile"], module.calls[0]
    assert module.calls[0]["tls_ctx"] is not None, module.calls[0]


def test_the_context_used_is_the_non_verifying_one_not_pytds_own():
    """With no bundle there is nothing to verify against, so python-tds's builder —
    which loads verify locations and sets VERIFY_PEER — must not be the one that
    answers. Handing it a sentinel path would fail at load_verify_locations."""
    module = _connect()
    assert module.calls[0]["tls_ctx"][0] == "trusting-context", module.calls[0]
    assert module.tls.builder_calls == [], module.tls.builder_calls


def test_an_operator_supplied_bundle_goes_through_pytds_own_builder():
    """FN_DB_CAFILE is the opt-in to real verification; the override must stay
    narrow enough not to swallow it."""
    module = _connect(cafile="/var/task/rds-ca.pem")
    assert module.calls[0]["cafile"] == "/var/task/rds-ca.pem", module.calls[0]
    assert module.tls.builder_calls == ["/var/task/rds-ca.pem"], module.tls.builder_calls
    assert module.calls[0]["tls_ctx"][0] == "pytds-own-context", module.calls[0]


def test_the_connection_arguments_survive_the_indirection():
    """The workloads no longer call pytds themselves, so this is the only place the
    server/port/database mapping is stated."""
    module = _connect(host="db.example.net", port=1444, database="reporting",
                      user="admin", password="pw", timeout=7)
    call = module.calls[0]
    assert call["server"] == "db.example.net" and call["port"] == 1444, call
    assert call["database"] == "reporting" and call["user"] == "admin", call
    assert call["login_timeout"] == 7 and call["autocommit"] is True, call
    assert call["validate_host"] is False, call


def test_an_empty_database_connects_to_master():
    """Azure SQL's CREATE LOGIN runs in master, and cloud_db_sql_service emits that
    name explicitly — but a plan entry with no database must still land somewhere."""
    module = _connect(database="")
    assert module.calls[0]["database"] == "master", module.calls[0]


# ── The override itself ──────────────────────────────────────────────────────

def test_patching_pytds_is_idempotent():
    """A warm function connects many times. Wrapping the wrapper would nest a call
    per invocation and eventually recurse past the stack limit."""
    module = _fake_pytds()
    tds._install(module)
    once = module.tls.create_context
    for _ in range(50):
        tds._install(module)
    assert module.tls.create_context is once
    # And a real path still resolves to the original builder.
    assert module.tls.create_context("/ca.pem")[0] == "pytds-own-context"
    assert module.tls.builder_calls == ["/ca.pem"], module.tls.builder_calls


def test_a_package_built_without_pyopenssl_says_which_step_is_missing():
    """python-tds's own message names only pyOpenSSL. The actionable fact is that
    the image's vendor step and _WORKLOAD_VENDOR disagree."""
    os.environ.pop("FN_DB_CAFILE", None)
    module = _fake_pytds(openssl_available=False)
    try:
        tds.connect(host="sql.internal", port=1433, database="appdb",
                    user="dbadmin", password="pw", timeout=10)
    except RuntimeError as exc:
        assert "FN_VENDOR_DIR" in str(exc) and "_WORKLOAD_VENDOR" in str(exc), exc
    else:
        raise AssertionError("a package with no TLS stack connected anyway")
    assert module.calls == []


def test_the_sentinel_can_never_be_a_real_path():
    """It is compared by equality against whatever FN_DB_CAFILE holds, so a value an
    operator could plausibly set would silently disable verification."""
    assert "\0" in tds._TRUST_SERVER_CERT


def test_the_real_context_encrypts_without_verifying():
    """The one assertion that needs the actual TLS stack. Skipped where pyOpenSSL is
    not installed — it is vendored into the function package, not into the
    dashboard's requirements — so CI still runs everything above."""
    try:
        from OpenSSL import SSL
    except ImportError:
        print("     (skipped: pyOpenSSL is not installed here)")
        return
    context = tds._trusting_context()
    assert context.get_verify_mode() == SSL.VERIFY_NONE


# ── The workloads ────────────────────────────────────────────────────────────

def test_no_workload_opens_a_sql_server_connection_of_its_own():
    """db_grant and ps_dbops had the identical broken call. One copy, one fix."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "web_dashboard", "functions", "fnworkloads")
    offenders = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if stripped.startswith(("import pytds", "from pytds")):
                    offenders.append(f"{name}:{number}")
    assert offenders == [], offenders


def test_db_grant_encrypts_its_sql_server_connection():
    from fnworkloads import db_grant

    os.environ.pop("FN_DB_CAFILE", None)
    module = _fake_pytds()
    restore = _trusting_sentinel(module)
    try:
        db_grant._connect("sqlserver", host="sql.internal", port=1433,
                          database="appdb", user="dbadmin", password="pw")
    finally:
        tds._trusting_context = restore
    assert module.calls and module.calls[0]["cafile"], module.calls


def test_ps_dbops_encrypts_its_sql_server_connection():
    from fnworkloads import ps_dbops

    os.environ.pop("FN_DB_CAFILE", None)
    module = _fake_pytds()
    restore = _trusting_sentinel(module)
    try:
        ps_dbops._connect("sqlserver", host="sql.internal", port=1433,
                          database="appdb", user="dbadmin", password="pw")
    finally:
        tds._trusting_context = restore
    assert module.calls and module.calls[0]["cafile"], module.calls


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
