"""Retiring what a db_grant pairing leaves behind when its database is torn down.

`_stage_admin_secret` writes the database's master password into the cloud's own
secret store so the adapter function can resolve it. Nothing ever deleted it: on
2026-09-04 the lab GCP project held four orphaned `dashboard-clouddb-<uuid>-admin`
secrets while having zero Cloud SQL instances — a live DB admin password per
database that had ever been paired, outliving the database by any length of time.

What is pinned here is the teardown, and specifically the two ways it can go wrong
silently:

  * the REF. Each store mangles the key on the way in, so a delete addressed at the
    key this module passed to `write_sync` targets a name nothing was written under
    and succeeds at deleting nothing.
  * the SEVERITY. Absence is the normal outcome (most rows were never paired) and
    must be a no-op, but a real failure has to fail the job — a leftover credential
    is not something to log and move on from.

Imports cloud_db_adapter_service with a stubbed web_dashboard.config, and the REAL
secrets_backend_service, because reproducing the writer's mangling is the point.
Runs under pytest or standalone:
    python tests/test_clouddb_adapter_secret_retirement.py
"""
import asyncio
import os
import re
import sys
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, not gated on whether a dependency happens to be installed:
# gating on that made a previous suite pass locally and fail in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))
_stub("web_dashboard.services.job_service", create_job=None, set_running=None,
      set_completed=None, set_failed=None, update_progress=None)
_stub("web_dashboard.config",
      settings=types.SimpleNamespace(aws_region="us-east-1", gcp_project_id="lab-project"))

# CloudDatabase is imported for a type hint only; database.py opens an engine off
# settings.database_url and drags in bcrypt and the whole ORM, none of which the
# functions under test touch. Stubbed unconditionally, for the same reason the config
# above is: a test that behaves differently depending on what happens to be installed
# is one that passes locally and fails in CI.
_stub("web_dashboard.database", CloudDatabase=object, Job=object,
      CloudFunction=object, SecretVault=object, SessionLocal=None)

from web_dashboard.services import cloud_db_adapter_service as adapter  # noqa: E402
from web_dashboard.services import secrets_backend_service as backends  # noqa: E402

DB_ID = "abcdef12-3456-7890-abcd-ef1234567890"


class _Row:
    """A provisioned MySQL database — the common pairable shape."""

    def __init__(self, engine="mysql", cloud="gcp", **kw):
        self.id = DB_ID
        self.engine = engine
        self.cloud = cloud
        self.region = "us-east1"
        self.source = "provisioned"
        self.status = "available"
        self.db_name = "appdb"
        self.private_host = "db.internal"
        self.port = 3306
        self.entitle_integration_id = None
        for key, value in kw.items():
            setattr(self, key, value)


# ── The three SDKs' "it isn't there" ─────────────────────────────────────────
# Reproduced by shape, because that is what _already_gone matches on.

class NotFound(Exception):                     # google.api_core.exceptions.NotFound
    code = 404


class ResourceNotFoundError(Exception):        # azure.core.exceptions
    status_code = 404


class ClientError(Exception):                  # botocore
    def __init__(self, code="ResourceNotFoundException"):
        self.response = {"Error": {"Code": code, "Message": "Secrets Manager can't find it"}}
        super().__init__(str(self.response))


class _Deleter:
    """Stands in for secrets_backend_service.delete_sync; records or raises."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, backend, ref):
        self.calls.append((backend, ref))
        if self.raises is not None:
            raise self.raises


def _retire(row, deleter):
    original = backends.delete_sync
    backends.delete_sync = deleter
    try:
        return adapter.retire_admin_secret(row)
    finally:
        backends.delete_sync = original


# ── The ref: the writer's own, not a hand-built prefix ───────────────────────

def test_each_cloud_maps_to_its_own_backend():
    """A row torn down against the wrong store deletes nothing and reports success."""
    assert adapter._secret_backend_for(_Row(cloud="aws")) == "aws_sm"
    assert adapter._secret_backend_for(_Row(cloud="azure")) == "azure_kv"
    assert adapter._secret_backend_for(_Row(cloud="gcp")) == "gcp_sm"


def test_the_ref_is_the_one_the_writer_returned_not_the_key():
    """Each store mangles the key on the way in. GCP is the one that bit: the key is
    ``clouddb-<id>-admin`` and what exists in Secret Manager is
    ``dashboard-clouddb-<id>-admin``, so a delete addressed at the key 404s forever
    while the credential stays live."""
    key = adapter.secret_key(_Row())
    assert key == f"clouddb-{DB_ID}-admin"

    assert backends.ref_for("gcp_sm", key) == f"dashboard-{key}"
    assert backends.ref_for("aws_sm", key) == f"dashboard/{key}"
    assert backends.ref_for("azure_kv", key) == key


def test_the_ref_tracks_the_writer_rather_than_a_second_spelling():
    """Derived through secrets_backend_service's own naming helpers, which are what
    the write path uses — so a prefix or separator change cannot leave the teardown
    addressing last month's names."""
    for backend, key in (("aws_sm", "clouddb-x-admin"),
                         ("azure_kv", "clouddb-x-admin"),
                         ("gcp_sm", "clouddb-x-admin")):
        assert backends.ref_for(backend, key) == backends._REF_FN[backend](key)
    assert backends._REF_FN["aws_sm"] is backends._aws_secret_name
    src = Path(_ROOT, "web_dashboard", "services", "secrets_backend_service.py").read_text(
        encoding="utf-8")
    writer = src.split("def write_aws_sm(", 1)[1].split("\ndef ", 1)[0]
    assert "_aws_secret_name(key)" in writer, "the writer stopped using the shared name"


def test_a_backend_that_mints_its_own_id_is_refused_not_guessed():
    """Secrets Safe / WLC hand back an id nothing local can reconstruct. Returning a
    plausible-looking wrong ref would delete someone else's secret, or nothing."""
    for backend in ("bt_secrets_safe", "wlc", "database", "nonsense"):
        try:
            backends.ref_for(backend, "k")
        except ValueError:
            continue
        raise AssertionError(f"{backend} returned a made-up ref")


# ── Which rows could ever have had one ───────────────────────────────────────

def test_a_paired_row_has_its_credential_deleted():
    deleter = _Deleter()
    ref = _retire(_Row(cloud="gcp"), deleter)
    assert deleter.calls == [("gcp_sm", f"dashboard-clouddb-{DB_ID}-admin")]
    assert ref == f"dashboard-clouddb-{DB_ID}-admin"


def test_a_row_that_could_never_have_been_paired_calls_no_cloud():
    """Postgres keeps the native connector and is never paired; a registered database
    has no admin credential to stage; an unknown cloud has no store. None of them can
    be holding a staged secret, so none of them reaches an SDK — which also keeps the
    common case (Postgres) free of a cloud call on every teardown."""
    for row in (_Row(engine="postgres"), _Row(source="registered"),
                _Row(cloud="oci"), _Row(engine="")):
        deleter = _Deleter(raises=AssertionError("should not have been called"))
        assert _retire(row, deleter) == ""
        assert deleter.calls == []


def test_sqlserver_is_retired_too():
    """The other engine that must use an adapter — and the one whose pairings are
    least likely to be remembered."""
    deleter = _Deleter()
    assert _retire(_Row(engine="sqlserver", cloud="aws"), deleter)
    assert deleter.calls == [("aws_sm", f"dashboard/clouddb-{DB_ID}-admin")]


# ── Absence vs failure ───────────────────────────────────────────────────────

def test_absence_is_a_clean_no_op_on_every_cloud():
    """Most rows were never paired, so "not found" is the NORMAL outcome. Failing on
    it would fail a teardown that had nothing to do."""
    for cloud, missing in (("gcp", NotFound("no such secret")),
                           ("azure", ResourceNotFoundError("SecretNotFound")),
                           ("aws", ClientError())):
        deleter = _Deleter(raises=missing)
        assert _retire(_Row(cloud=cloud), deleter) == "", cloud
        assert len(deleter.calls) == 1, cloud


def test_already_gone_does_not_swallow_a_real_refusal():
    """A 403 is not an absence. Crediting one would report a credential deleted that
    is still sitting there."""
    assert not adapter._already_gone(ClientError("AccessDeniedException"))
    assert not adapter._already_gone(RuntimeError("403 Forbidden"))
    assert not adapter._already_gone(ValueError("GCP project is not configured"))


def test_a_real_delete_failure_names_the_secret_and_says_what_it_is():
    """The message is the whole point: it is the only place an operator learns a
    database password was left behind, and which one to remove."""
    deleter = _Deleter(raises=RuntimeError("403 Permission denied on secret"))
    try:
        _retire(_Row(cloud="gcp"), deleter)
    except adapter.AdapterPairingError as exc:
        message = str(exc)
    else:
        raise AssertionError("a failed delete must not report success")
    assert f"dashboard-clouddb-{DB_ID}-admin" in message
    assert "database password" in message
    assert "by hand" in message
    assert "GCP Secret Manager" in message      # the store, as the Secrets page names it
    assert "403 Permission denied" in message   # and the cause, not just our summary


def _logger_calls(body):
    """Every ``logger.<level>(...)`` in ``body``, each as one whole statement."""
    out = []
    for start in (m.end() for m in re.finditer(r"logger\.\w+\(", body)):
        depth, i = 1, start
        while depth and i < len(body):
            depth += {"(": 1, ")": -1}.get(body[i], 0)
            i += 1
        out.append(body[start:i - 1])
    return out


def test_the_ref_is_never_written_to_the_log_from_this_module():
    """CodeQL alerts 124/125 on the first cut of this. `secret_key` and
    `_SECRET_BACKEND` are name-based sensitive sources, so anything descending from
    them trips py/clear-text-logging-sensitive-data at a logging sink — the query reads
    the NAME of the secret as the secret. Naming the value for what it is, and logging
    it only where it does not descend from such a name, is the same answer the
    functional account's regional Secret Manager entry already took; a `# nosec` there
    would have wasted a query that is right to be suspicious.

    Nothing is lost by the omission, which is why this is a guard and not a compromise:
    the ref reaches the log through the caller (as `resource_id`) and reaches the
    operator through the raised message. Both are asserted elsewhere in this file."""
    src = Path(_ROOT, "web_dashboard", "services", "cloud_db_adapter_service.py").read_text(
        encoding="utf-8")
    body = src.split("def retire_admin_secret(", 1)[1].split("async def retire_adapter(", 1)[0]
    calls = _logger_calls(body)
    assert calls, "the guard stopped finding the logger calls it exists to check"
    for call in calls:
        # Whole statements, not lines: these calls wrap, and a regression that put the
        # ref back on a continuation line is exactly what a per-line check would miss.
        for tainted in ("ref", "key", "backend"):
            found = re.search(rf"[(,]\s*{tainted}\b", call)
            assert found is None, (
                f"logs a CodeQL-sensitive value: {' '.join(call.split())}")


def test_an_underivable_ref_still_names_something_addressable():
    """If the ref cannot even be built, the operator still gets the key — an error
    saying a password was left behind but not which one is one they cannot act on."""
    original = backends.ref_for
    backends.ref_for = lambda backend, key: (_ for _ in ()).throw(ValueError("no project"))
    try:
        adapter.retire_admin_secret(_Row(cloud="gcp"))
    except adapter.AdapterPairingError as exc:
        assert f"clouddb-{DB_ID}-admin" in str(exc)
        assert "database password" in str(exc)
    else:
        raise AssertionError("an underivable ref must not report success")
    finally:
        backends.ref_for = original


# ── The adapter function that read it ────────────────────────────────────────

class _Fn:
    def __init__(self, name, status="available", entitle_integration_id=None):
        self.id = "fn-1"
        self.name = name
        self.status = status
        self.entitle_integration_id = entitle_integration_id


class _DB:
    def refresh(self, obj):
        pass


class _FakeFunctions(types.ModuleType):
    """Stands in for cloud_function_service: records the teardown calls and replays
    the outcome each one leaves on the row (both real entry points report through the
    job rather than by raising, which is why the row is what gets checked)."""

    def __init__(self, fn=None, destroy_status="deleted", deregisters=True):
        super().__init__("web_dashboard.services.cloud_function_service")
        self.fn = fn
        self.destroy_status = destroy_status
        self.deregisters = deregisters
        self.calls = []

    def normalize_name(self, raw):
        return raw.replace("_", "-").lower()

    def find_by_names(self, db, names, workload=""):
        self.calls.append(("find", sorted(names), workload))
        names = list(names)
        if self.fn is None or self.fn.name not in names:
            return {}
        return {self.fn.name: self.fn}

    def start_entitle_register(self, db, fn_id, action="register", created_by=""):
        self.calls.append(("entitle", fn_id, action))
        return {"job_id": "job-e"}

    async def run_entitle_register(self, db, *, fn_id, job_id, action="register"):
        if self.deregisters:
            self.fn.entitle_integration_id = None

    def start_decommission(self, db, fn_id, created_by=""):
        self.calls.append(("destroy", fn_id))
        return {"job_id": "job-d"}

    async def run_decommission(self, db, *, fn_id, job_id):
        self.fn.status = self.destroy_status


def _retire_adapter(row, functions):
    key = "web_dashboard.services.cloud_function_service"
    original = sys.modules.get(key)
    sys.modules[key] = functions
    try:
        return asyncio.run(adapter.retire_adapter(_DB(), row))
    finally:
        if original is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = original


def test_the_adapter_function_is_destroyed_with_its_database():
    """A function pointed at a destroyed database is billable, can only ever fail, and
    still holds the IAM binding on the credential above."""
    row = _Row()
    functions = _FakeFunctions(_Fn(f"jit-mysql-{DB_ID[:8]}"))
    assert _retire_adapter(row, functions) == "fn-1"
    assert ("destroy", "fn-1") in functions.calls
    assert functions.calls[0] == ("find", [f"jit-mysql-{DB_ID[:8]}"], adapter.ADAPTER_WORKLOAD)


def test_a_never_paired_row_has_no_adapter_and_destroys_nothing():
    row = _Row()
    functions = _FakeFunctions(fn=None)
    assert _retire_adapter(row, functions) == ""
    assert [c for c in functions.calls if c[0] != "find"] == []


def test_the_entitle_integration_goes_first_and_never_skips_the_destroy():
    """Entitle is the outward-facing half — left behind it stays in the catalogue and
    errors on every grant. But the function costs money either way, so a failed
    deregistration must not leave it running."""
    functions = _FakeFunctions(_Fn(f"jit-mysql-{DB_ID[:8]}", entitle_integration_id="int-9"),
                               deregisters=False)
    try:
        _retire_adapter(_Row(), functions)
    except adapter.AdapterPairingError as exc:
        assert "int-9" in str(exc)
    else:
        raise AssertionError("a stuck Entitle integration must be reported")
    kinds = [c[0] for c in functions.calls]
    assert kinds.index("entitle") < kinds.index("destroy"), kinds


def test_an_undestroyed_adapter_is_reported_rather_than_assumed_gone():
    functions = _FakeFunctions(_Fn(f"jit-mysql-{DB_ID[:8]}"), destroy_status="failed")
    try:
        _retire_adapter(_Row(), functions)
    except adapter.AdapterPairingError as exc:
        assert f"jit-mysql-{DB_ID[:8]}" in str(exc)
        assert "Cloud Functions page" in str(exc)
    else:
        raise AssertionError("a function still standing must not report success")


# ── How run_decommission grades each leftover ────────────────────────────────

def _decommission_body():
    src = Path(_ROOT, "web_dashboard", "services", "cloud_database_service.py").read_text(
        encoding="utf-8")
    return src.split("async def run_decommission(", 1)[1].split("\ndef ", 1)[0]


def test_the_teardown_runs_both_retirements():
    body = _decommission_body()
    assert "retire_admin_secret" in body, "the staged credential is leaking again"
    assert "retire_adapter" in body, "the adapter function is being orphaned again"


def test_a_leftover_credential_fails_the_job_and_a_leftover_function_warns():
    """The split is the decision, so it is what gets pinned. A live database password
    nothing will retry is worth failing over; a billable function with its own Delete
    button on the Cloud Functions page is not worth wedging the database row at
    `failed` — and quietly downgrading the credential to a warning would put this leak
    straight back."""
    body = _decommission_body()
    credential = body.split("retire_admin_secret", 1)[1]
    assert credential.index("errors.append") < credential.index("warnings.append"), \
        "a leftover admin credential must fail the job"
    function = body.split("retire_adapter", 1)[1]
    assert function.index("warnings.append") < function.index("errors.append"), \
        "a leftover adapter function must not wedge the database row at failed"


def test_the_blocking_delete_runs_off_the_event_loop():
    """Three cloud SDKs, all synchronous; called inline they stall every other request
    the worker is serving for the length of the teardown."""
    assert "_to_thread(cloud_db_adapter_service.retire_admin_secret" in _decommission_body()


def test_the_credential_goes_after_the_function_that_reads_it():
    """On GCP the function's Terraform holds the accessor binding on the secret, and
    the function is its reader — the same order the functional account's regional
    secret follows."""
    body = _decommission_body()
    assert body.index("retire_adapter") < body.index("retire_admin_secret")
    # And both after the instance destroy, not instead of it.
    assert body.index("Destroying the database instance") < body.index("retire_adapter")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
