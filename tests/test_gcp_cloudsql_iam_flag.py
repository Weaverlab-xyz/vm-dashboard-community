"""Cloud SQL's IAM-database-authentication flag is spelled per ENGINE.

A live MySQL provision died at create time with::

    Error 404: The requested flag is either misspelled or unsupported by
    Cloud SQL., invalidFlagName

because both the MySQL terraform module and the runtime patch path sent
PostgreSQL's dotted ``cloudsql.iam_authentication``. MySQL's flag is
``cloudsql_iam_authentication`` (underscores). There is no engine-neutral spelling,
and the wrong one is not ignored — it rejects the whole create/patch, so the
instance never comes up at all.

Covered here:

  * ``gcp_service._sql_iam_auth_flag`` — the per-engine mapping, keyed off the
    Cloud SQL ``databaseVersion`` string the instance GET already returns;
  * ``_ensure_cloudsql_rotation_prereqs_sync`` — the flag it actually PATCHes for a
    MySQL vs a PostgreSQL instance, that "already on" is detected with the same
    spelling (so a re-registration is a no-op, not a restart), and that the
    existing databaseFlags list is merged rather than replaced;
  * the two terraform modules, read as text, so the module and the runtime path
    cannot drift apart again.

Runs under pytest, or standalone:  python tests/test_gcp_cloudsql_iam_flag.py
"""
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# gcp_service imports google.api_core.exceptions lazily inside helpers, but keep the
# stub so this file runs with no google libraries installed.
_g = sys.modules.setdefault("google", types.ModuleType("google"))
_gac = types.ModuleType("google.api_core")
_gace = types.ModuleType("google.api_core.exceptions")
_gace.NotFound = type("NotFound", (Exception,), {})
sys.modules["google.api_core"] = _gac
sys.modules["google.api_core.exceptions"] = _gace

from web_dashboard.services import gcp_service  # noqa: E402

MYSQL_FLAG = "cloudsql_iam_authentication"
PG_FLAG = "cloudsql.iam_authentication"


# ── Fake sqladmin session ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body=None, status=200):
        self._body = body if body is not None else {}
        self.status_code = status
        self.ok = status < 400
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise AssertionError(f"HTTP {self.status_code}")


class _FakeSession:
    """Serves instances.get, records instances.patch, and answers the operation poll
    as immediately DONE so the helper does not sleep."""

    def __init__(self, instance_body):
        self.instance_body = instance_body
        self.patches = []

    def get(self, url):
        if "/operations/" in url:
            return _Resp({"status": "DONE"})
        return _Resp(self.instance_body)

    def patch(self, url, json=None):
        self.patches.append(json)
        return _Resp({"name": "op-1"})


def _run(instance_body, *, iam_auth=True):
    sess = _FakeSession(instance_body)
    gcp_service._authed_session = lambda: sess
    out = gcp_service._ensure_cloudsql_rotation_prereqs_sync(
        "proj", "clouddb-abc", iam_auth=iam_auth)
    return sess, out


def _patched_flags(sess):
    assert sess.patches, "expected a patch"
    return sess.patches[-1]["settings"].get("databaseFlags")


def _flag_names(flags):
    return [f["name"] for f in (flags or [])]


# ── The mapping itself ───────────────────────────────────────────────────────────

def test_flag_name_is_underscored_for_mysql_and_dotted_for_postgres():
    assert gcp_service._sql_iam_auth_flag("MYSQL_8_4") == MYSQL_FLAG
    assert gcp_service._sql_iam_auth_flag("MYSQL_8_0") == MYSQL_FLAG
    assert gcp_service._sql_iam_auth_flag("POSTGRES_16") == PG_FLAG
    # Unknown/absent databaseVersion keeps the historical dotted default rather than
    # guessing MySQL — Postgres is the engine the dotted spelling is correct for.
    assert gcp_service._sql_iam_auth_flag("") == PG_FLAG
    assert gcp_service._sql_iam_auth_flag(None) == PG_FLAG


# ── What actually gets PATCHed ───────────────────────────────────────────────────

def test_mysql_instance_is_patched_with_the_underscored_flag():
    sess, _ = _run({"databaseVersion": "MYSQL_8_4", "settings": {}})
    names = _flag_names(_patched_flags(sess))
    assert names == [MYSQL_FLAG], names


def test_postgres_instance_is_patched_with_the_dotted_flag():
    sess, _ = _run({"databaseVersion": "POSTGRES_16", "settings": {}})
    names = _flag_names(_patched_flags(sess))
    assert names == [PG_FLAG], names


def test_mysql_flag_already_on_is_recognised_so_nothing_is_patched():
    # The Data API is already on too, so there is nothing left to do at all. Matching
    # with the wrong spelling here would re-patch (and restart) a healthy instance.
    body = {
        "databaseVersion": "MYSQL_8_4",
        "settings": {
            gcp_service._SQL_DATA_API_FIELD: gcp_service._SQL_DATA_API_ON,
            "databaseFlags": [{"name": MYSQL_FLAG, "value": "on"}],
        },
    }
    sess, out = _run(body)
    assert sess.patches == [], sess.patches
    assert out["patched"] is False


def test_existing_flags_are_merged_not_replaced():
    body = {
        "databaseVersion": "MYSQL_8_4",
        "settings": {"databaseFlags": [{"name": "max_connections", "value": "150"}]},
    }
    sess, _ = _run(body)
    names = _flag_names(_patched_flags(sess))
    assert "max_connections" in names, names
    assert MYSQL_FLAG in names, names


def test_sqlserver_style_call_with_iam_auth_off_never_sends_the_flag():
    # Cloud SQL for SQL Server has no IAM database auth; the caller passes iam_auth=False
    # and the patch must carry the Data API field only.
    sess, _ = _run({"databaseVersion": "SQLSERVER_2019_STANDARD", "settings": {}},
                   iam_auth=False)
    assert "databaseFlags" not in sess.patches[-1]["settings"], sess.patches
    assert gcp_service._SQL_DATA_API_FIELD in sess.patches[-1]["settings"]


# ── The terraform modules must agree ─────────────────────────────────────────────

def _flag_in_module(module: str) -> str:
    path = os.path.join(_ROOT, "terraform", module, "main.tf")
    src = open(path, encoding="utf-8").read()
    block = re.search(r'dynamic "database_flags".*?\n  }', src, re.S)
    assert block, f"no database_flags block in {module}"
    name = re.search(r'name\s*=\s*"([^"]+)"', block.group(0))
    assert name, f"no flag name in {module}"
    return name.group(1)


def test_terraform_modules_use_the_engine_correct_flag():
    assert _flag_in_module("db_gcp_mysql") == MYSQL_FLAG
    assert _flag_in_module("db_gcp_postgres") == PG_FLAG


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
    sys.exit(1 if failures else 0)
