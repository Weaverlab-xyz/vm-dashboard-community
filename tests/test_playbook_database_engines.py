"""The Oracle and MongoDB database samples in examples/playbooks/database/.

There is no official Oracle Database collection, so oracle-create-user.yml carries its
work as a Python script run by the runner's interpreter over python-oracledb. That script
is code nobody else tests, so this extracts it from the YAML and RUNS it against a stub
``oracledb`` that records what was executed — create on a new user, alter on an existing
one, refusal of an unsafe name or grant.

The MongoDB samples are module calls; for those this pins the connection wiring every
task needs (TLS, the auth database, the replica set, atlas_auth) and that the user play
refuses an Atlas target, which denies createUser over the wire.

Runs under pytest or standalone:  python tests/test_playbook_database_engines.py
"""
import contextlib
import io
import os
import sys
import types

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_ROOT, "examples", "playbooks", "database")


def _tasks(name):
    with open(os.path.join(_DIR, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)[0]["tasks"]


def _task(name, module):
    for task in _tasks(name):
        if module in task:
            return task
    raise AssertionError(f"{name}: no {module} task")


# ── Oracle: run the embedded script ──────────────────────────────────────────

class _Cursor:
    def __init__(self, log, exists):
        self.log, self.exists = log, exists

    def execute(self, sql, **binds):
        self.log.append((sql, binds))

    def fetchone(self):
        return (1 if self.exists else 0,)


class _Conn:
    def __init__(self, log, exists):
        self.log, self.exists = log, exists

    def cursor(self):
        return _Cursor(self.log, self.exists)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_oracle(env_over=None, exists=False):
    task = _task("oracle-create-user.yml", "ansible.builtin.command")
    script = task["ansible.builtin.command"]["argv"][2]
    env = {"ORA_HOST": "db.example", "ORA_PORT": "1521", "ORA_SERVICE": "ORAABCDE",
           "ORA_USER": "dbadmin", "ORA_PASSWORD": "Adm1n", "ORA_TLS": "False",
           "ORA_NEW_USER": "appuser", "ORA_NEW_PASSWORD": "Pw-1#x",
           "ORA_GRANTS": "CREATE SESSION"}
    env.update(env_over or {})
    log, seen = [], {}

    def _connect(**kw):
        seen.update(kw)
        return _Conn(log, exists)

    stub = types.ModuleType("oracledb")
    stub.connect = _connect
    saved_mod, saved_env = sys.modules.get("oracledb"), dict(os.environ)
    sys.modules["oracledb"] = stub
    os.environ.update(env)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(script, "oracle-create-user.yml", "exec"), {})
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        if saved_mod is None:
            sys.modules.pop("oracledb", None)
        else:
            sys.modules["oracledb"] = saved_mod
    return log, seen, out.getvalue().strip()


def test_a_new_oracle_user_is_created_and_granted():
    log, seen, out = _run_oracle()
    assert seen["dsn"] == "tcp://db.example:1521/ORAABCDE"
    assert seen["user"] == "dbadmin" and seen["password"] == "Adm1n"
    assert log[0] == ("SELECT COUNT(*) FROM all_users WHERE username = :u", {"u": "APPUSER"})
    assert log[1][0] == 'CREATE USER appuser IDENTIFIED BY "Pw-1#x"'
    assert log[2][0] == "GRANT CREATE SESSION TO appuser"
    assert out == "CREATED"


def test_an_existing_oracle_user_is_reset_not_recreated():
    log, _seen, out = _run_oracle(exists=True)
    assert log[1][0] == 'ALTER USER appuser IDENTIFIED BY "Pw-1#x"'
    assert out == "UPDATED"


def test_tls_switches_the_descriptor_to_tcps():
    _log, seen, _out = _run_oracle({"ORA_TLS": "True"})
    assert seen["dsn"].startswith("tcps://")


def test_unsafe_names_passwords_and_grants_are_refused_in_the_script_too():
    # The play validates first; the script re-checks, because it is the code that
    # builds the DDL and must not depend on a caller having asserted anything.
    for over in ({"ORA_NEW_USER": "x; DROP USER y"}, {"ORA_NEW_PASSWORD": 'a"b'},
                 {"ORA_GRANTS": "DBA; DROP USER y"}):
        try:
            _run_oracle(over)
        except AssertionError:
            continue
        raise AssertionError(f"accepted {over}")


def test_the_oracle_task_hides_its_environment():
    task = _task("oracle-create-user.yml", "ansible.builtin.command")
    assert task.get("no_log") is True
    env = task["environment"]
    assert env["ORA_PASSWORD"] == "{{ db_login_password }}"
    assert env["ORA_SERVICE"] == "{{ db_name }}"


# ── MongoDB: connection wiring + the Atlas refusal ───────────────────────────

def test_every_mongodb_task_carries_the_connection_wiring():
    for name, module in (("mongodb-create-user.yml", "community.mongodb.mongodb_user"),
                         ("mongodb-create-database.yml", "community.mongodb.mongodb_index")):
        task = _task(name, module)
        args = task[module]
        assert task.get("no_log") is True, name
        assert "db_auth_source" in args["login_database"], name
        assert "db_tls" in args["tls"], name
        assert "db_replica_set" in args["replica_set"], name
        assert "atlas" in args["atlas_auth"], name
        assert args["strict_compatibility"] is False, name


def test_the_user_play_refuses_atlas_before_touching_the_database():
    tasks = _tasks("mongodb-create-user.yml")
    first = tasks[0]
    assert "ansible.builtin.assert" in first
    assert any("atlas" in str(cond) for cond in first["ansible.builtin.assert"]["that"])
    module_at = next(i for i, t in enumerate(tasks) if "community.mongodb.mongodb_user" in t)
    assert module_at > 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
