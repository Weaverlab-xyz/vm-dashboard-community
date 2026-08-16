"""Ephemeral just-in-time database accounts — the SQL, per engine and flavor.

This is the crux of the Cloud Functions Phase 2 pilot. The whole reason Entitle's
native connectors cannot deliver these accounts is a SQL-shape problem:

  * its MySQL connector assigns persistent roles and never mints an account
  * its SQL Server connector's ephemeral accounts assume a server-level LOGIN plus
    USE, which is not how Azure SQL Database works

So the SQL is where the value is, and it is pure string-building — no driver, no
database, no cloud account needed to prove it.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard.services import cloud_db_sql_service as sql


def _flat(plan) -> str:
    """All statements in a plan, joined — for substring assertions."""
    return " ".join(stmt for _db, stmts in plan for stmt in stmts)


def _databases(plan) -> list:
    return [db for db, _stmts in plan]


# ── MySQL: the case Entitle cannot do at all ─────────────────────────────────

def test_mysql_grant_creates_the_account_and_scopes_it_to_one_database():
    plan = sql.grant_plan("mysql", username="jit_alice_ab12", password="Pw-1234",
                          database="appdb", role="read")
    assert _databases(plan) == ["appdb"], "mysql needs exactly one connection"
    body = _flat(plan)
    assert "CREATE USER 'jit_alice_ab12'@'%' IDENTIFIED BY 'Pw-1234';" in body
    assert "GRANT SELECT ON `appdb`.* TO 'jit_alice_ab12'@'%';" in body
    # Read must not smuggle in write.
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALL PRIVILEGES"):
        assert verb not in body, f"read grant leaked {verb}"


def test_mysql_readwrite_adds_dml_but_never_ddl_or_grant_option():
    body = _flat(sql.grant_plan("mysql", username="jit_bob_cd34", password="Pw-1234",
                                database="appdb", role="readwrite"))
    for verb in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert verb in body, verb
    for forbidden in ("ALL PRIVILEGES", "WITH GRANT OPTION", "DROP", "ALTER", "CREATE TABLE"):
        assert forbidden not in body, f"readwrite grant leaked {forbidden}"


def test_mysql_revoke_is_idempotent():
    """Entitle retries. A revoke that errors on an account that was never created
    leaves standing access behind — the one outcome this feature exists to stop."""
    body = _flat(sql.revoke_plan("mysql", username="jit_alice_ab12", database="appdb"))
    assert "DROP USER IF EXISTS" in body


def test_mysql_needs_no_flavor():
    """CREATE USER is identical on RDS, Flexible Server and Cloud SQL, so a flavor
    must neither be required nor change the output."""
    base = _flat(sql.grant_plan("mysql", username="jit_a_1", password="Pw-1",
                                database="appdb", role="read"))
    for flavor in ("", "rds", "azure_sql", "cloudsql"):
        got = _flat(sql.grant_plan("mysql", username="jit_a_1", password="Pw-1",
                                   database="appdb", role="read", flavor=flavor))
        assert got == base, flavor


# ── SQL Server: the flavor gap ───────────────────────────────────────────────

def test_azure_sql_splits_the_login_and_the_user_across_two_connections():
    """Azure SQL Database is a contained-database model: the login lives in master
    on the logical server, the user lives in the target database, and there is NO
    USE to bridge them. This is exactly why Entitle's stock connector fails here."""
    plan = sql.grant_plan("sqlserver", username="jit_alice_ab12", password="Pw-1234",
                          database="appdb", role="read", flavor="azure_sql")
    assert _databases(plan) == ["master", "appdb"], _databases(plan)

    master_stmts, db_stmts = plan[0][1], plan[1][1]
    assert any("CREATE LOGIN" in s for s in master_stmts)
    assert not any("CREATE USER" in s for s in master_stmts), \
        "the contained user must not be created on the master connection"
    assert any("CREATE USER" in s for s in db_stmts)
    assert any("db_datareader" in s for s in db_stmts)
    assert "USE " not in _flat(plan), "Azure SQL does not support USE between databases"


def test_rds_uses_a_single_connection_with_use():
    plan = sql.grant_plan("sqlserver", username="jit_alice_ab12", password="Pw-1234",
                          database="appdb", role="read", flavor="rds")
    assert _databases(plan) == ["master"], "RDS SQL Server switches with USE"
    body = _flat(plan)
    assert "CREATE LOGIN" in body and "USE [appdb];" in body and "CREATE USER" in body


def test_cloudsql_behaves_like_rds_not_like_azure():
    rds = sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                         database="appdb", role="read", flavor="rds")
    cloudsql = sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                              database="appdb", role="read", flavor="cloudsql")
    assert _databases(rds) == _databases(cloudsql) == ["master"]
    assert _flat(rds) == _flat(cloudsql)


def test_sqlserver_defaults_to_rds_when_no_flavor_is_given():
    explicit = sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                              database="appdb", role="read", flavor="rds")
    default = sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                             database="appdb", role="read")
    assert _flat(default) == _flat(explicit)


def test_azure_sql_revoke_drops_the_user_before_the_login():
    """The login cannot be dropped while a database principal still maps to it."""
    plan = sql.revoke_plan("sqlserver", username="jit_alice_ab12",
                           database="appdb", flavor="azure_sql")
    assert _databases(plan) == ["appdb", "master"], _databases(plan)
    assert any("DROP USER" in s for s in plan[0][1]), plan[0]
    assert any("DROP LOGIN" in s for s in plan[1][1]), plan[1]


def test_sqlserver_revokes_are_existence_guarded():
    for flavor in ("rds", "azure_sql", "cloudsql"):
        body = _flat(sql.revoke_plan("sqlserver", username="jit_a_1",
                                     database="appdb", flavor=flavor))
        assert body.count("IF EXISTS") >= 2, (flavor, body)


def test_sqlserver_roles_map_to_fixed_database_roles():
    read = _flat(sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                                database="appdb", role="read", flavor="azure_sql"))
    write = _flat(sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                                 database="appdb", role="readwrite", flavor="azure_sql"))
    assert "db_datareader" in read and "db_datawriter" not in read
    assert "db_datawriter" in write
    for forbidden in ("db_owner", "sysadmin", "db_ddladmin", "CONTROL SERVER"):
        assert forbidden not in read and forbidden not in write


# ── Postgres (parity — already served by Entitle's native connector) ─────────

def test_postgres_revoke_reassigns_before_dropping():
    """Postgres refuses to drop a role that still owns objects, so a naive DROP
    ROLE fails on any account that was actually used."""
    body = _flat(sql.revoke_plan("postgres", username="jit_a_1", database="appdb"))
    order = [body.index(x) for x in ("REASSIGN OWNED BY", "DROP OWNED BY", "DROP ROLE")]
    assert order == sorted(order), f"wrong teardown order: {body}"
    assert "DROP ROLE IF EXISTS" in body


# ── Injection resistance ─────────────────────────────────────────────────────

def test_unsafe_identifiers_are_refused_not_escaped():
    """The module's contract is a hard allowlist, so nothing unvalidated can reach
    SQL. Escaping would be a second thing to get right."""
    evil = [
        "alice'; DROP TABLE users;--",
        'alice" OR "1"="1',
        "alice]; EXEC xp_cmdshell 'x';--",
        "alice`; SELECT 1;",
        "1alice",          # must start with a letter
        "a" * 64,          # too long
        "", "admin user", "admin-user",
    ]
    for engine, flavor in (("mysql", ""), ("sqlserver", "azure_sql"),
                           ("sqlserver", "rds"), ("postgres", "")):
        for name in evil:
            try:
                sql.grant_plan(engine, username=name, password="Pw-1",
                               database="appdb", role="read", flavor=flavor)
            except sql.CloudDbSqlError:
                pass
            else:
                raise AssertionError(f"{engine}/{flavor} accepted username {name!r}")


def test_unsafe_passwords_and_databases_are_refused():
    for bad_pw in ("pw'; DROP TABLE x;--", 'pw" OR 1=1', "pw with space", "pw`tick`", ""):
        try:
            sql.grant_plan("mysql", username="jit_a_1", password=bad_pw,
                           database="appdb", role="read")
        except sql.CloudDbSqlError:
            pass
        else:
            raise AssertionError(f"accepted password {bad_pw!r}")
    for bad_db in ("app db", "app;drop", "app`db`", "1db"):
        try:
            sql.grant_plan("mysql", username="jit_a_1", password="Pw-1",
                           database=bad_db, role="read")
        except sql.CloudDbSqlError:
            pass
        else:
            raise AssertionError(f"accepted database {bad_db!r}")


def test_unknown_role_engine_and_flavor_are_refused():
    try:
        sql.grant_plan("mysql", username="jit_a_1", password="Pw-1",
                       database="appdb", role="db_owner")
    except sql.CloudDbSqlError:
        pass
    else:
        raise AssertionError("accepted an unknown role")
    try:
        sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                       database="appdb", role="read", flavor="managed_instance")
    except sql.CloudDbSqlError:
        pass
    else:
        raise AssertionError("accepted an unknown flavor")
    try:
        sql.grant_plan("mongodb", username="jit_a_1", password="Pw-1",
                       database="appdb", role="read")
    except sql.CloudDbSqlError:
        pass
    else:
        raise AssertionError("accepted an unknown engine")


def test_generated_passwords_pass_the_value_allowlist():
    """generate_password feeds straight into grant_plan, so the two must agree —
    a mismatch here fails every grant at runtime, not at test time."""
    for _ in range(200):
        pwd = sql.generate_password()
        sql.grant_plan("sqlserver", username="jit_a_1", password=pwd,
                       database="appdb", role="read", flavor="azure_sql")


def test_generated_passwords_satisfy_sql_server_complexity():
    """SQL Server enforces 3 of 4 character classes on CREATE LOGIN; a password
    that fails it is rejected by the server, not by us."""
    for _ in range(200):
        pwd = sql.generate_password()
        classes = sum([
            any(c.islower() for c in pwd), any(c.isupper() for c in pwd),
            any(c.isdigit() for c in pwd), any(c in "#-_" for c in pwd),
        ])
        assert classes >= 3, pwd
        assert len(pwd) >= 8


# ── Username derivation ──────────────────────────────────────────────────────

def test_ephemeral_username_is_traceable_and_safe():
    name = sql.ephemeral_username("alice@example.com", "AB12cd34EF56gh")
    assert name.startswith("jit_alice_example_com"), name
    assert sql._IDENT_RE.match(name), name
    # Two grants for the same person must not collide.
    other = sql.ephemeral_username("alice@example.com", "ZZ99")
    assert name != other


def test_ephemeral_username_survives_hostile_input():
    for prefix in ("", "'; DROP TABLE x;--", "!!!", "x" * 200, "123"):
        name = sql.ephemeral_username(prefix, "tok123")
        assert sql._IDENT_RE.match(name), (prefix, name)
        # And it must be usable as a real account name.
        sql.grant_plan("mysql", username=name, password="Pw-1",
                       database="appdb", role="read")


# ── The plan shape itself ────────────────────────────────────────────────────

def test_every_plan_entry_is_a_database_and_a_nonempty_statement_list():
    cases = [("mysql", ""), ("postgres", ""), ("sqlserver", "rds"),
             ("sqlserver", "azure_sql"), ("sqlserver", "cloudsql")]
    for engine, flavor in cases:
        for plan in (sql.grant_plan(engine, username="jit_a_1", password="Pw-1",
                                    database="appdb", role="read", flavor=flavor),
                     sql.revoke_plan(engine, username="jit_a_1",
                                     database="appdb", flavor=flavor)):
            assert plan, (engine, flavor)
            for db, stmts in plan:
                assert isinstance(db, str)
                assert stmts and all(isinstance(s, str) and s.strip() for s in stmts)
                assert all(s.rstrip().endswith(";") for s in stmts), (engine, stmts)


# ── The four Entitle operations ──────────────────────────────────────────────

def test_create_actor_grants_nothing():
    """Entitle calls create_actor before give_access. If the second call never
    arrives, what is left behind must be able to reach nothing."""
    for engine, flavor in (("mysql", ""), ("postgres", ""),
                           ("sqlserver", "rds"), ("sqlserver", "azure_sql")):
        body = _flat(sql.create_actor_plan(engine, username="jit_a_1", password="Pw-1",
                                           database="appdb", flavor=flavor))
        assert "CREATE" in body, (engine, flavor)
        for verb in ("GRANT ", "ALTER ROLE", "db_datareader"):
            assert verb not in body, f"{engine}/{flavor} create leaked {verb}: {body}"


def test_create_actor_refuses_an_empty_password():
    """A login created with no password is a far worse hole than a rejected
    request — and the other three operations legitimately pass none."""
    for engine, flavor in (("mysql", ""), ("sqlserver", "azure_sql")):
        try:
            sql.create_actor_plan(engine, username="jit_a_1", password="",
                                  database="appdb", flavor=flavor)
        except sql.CloudDbSqlError:
            pass
        else:
            raise AssertionError(f"{engine} accepted an empty password")


def test_give_access_does_not_create_and_revoke_access_does_not_drop():
    """The account lifecycle belongs to create_actor/delete_actor. Mixing them
    would make Entitle unable to revoke one role without destroying the actor."""
    for engine, flavor in (("mysql", ""), ("postgres", ""),
                           ("sqlserver", "rds"), ("sqlserver", "azure_sql")):
        give = _flat(sql.give_access_plan(engine, username="jit_a_1",
                                          database="appdb", role="read", flavor=flavor))
        assert "CREATE USER" not in give and "CREATE LOGIN" not in give, (engine, give)
        take = _flat(sql.revoke_access_plan(engine, username="jit_a_1",
                                            database="appdb", role="read", flavor=flavor))
        assert "DROP USER" not in take and "DROP LOGIN" not in take, (engine, take)
        assert "DROP ROLE IF EXISTS" not in take, (engine, take)


def test_revoke_access_mirrors_give_access():
    """Whatever a grant hands out, the revoke must take back — a privilege granted
    and not listed in the revoke is a permanent leak."""
    for engine, flavor in (("mysql", ""), ("postgres", ""), ("sqlserver", "azure_sql")):
        for role in ("read", "readwrite"):
            give = _flat(sql.give_access_plan(engine, username="jit_a_1",
                                              database="appdb", role=role, flavor=flavor))
            take = _flat(sql.revoke_access_plan(engine, username="jit_a_1",
                                                database="appdb", role=role, flavor=flavor))
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE",
                              "CONNECT", "USAGE", "db_datareader", "db_datawriter"):
                if privilege in give:
                    assert privilege in take, \
                        f"{engine}/{flavor}/{role} grants {privilege} but never revokes it"


def test_delete_actor_matches_the_old_revoke_plan():
    """revoke_plan is now a composition; it must not have changed behaviour."""
    for engine, flavor in (("mysql", ""), ("postgres", ""),
                           ("sqlserver", "rds"), ("sqlserver", "azure_sql")):
        assert sql.revoke_plan(engine, username="jit_a_1", database="appdb",
                               flavor=flavor) == \
            sql.delete_actor_plan(engine, username="jit_a_1", database="appdb",
                                  flavor=flavor)


def test_grant_plan_is_exactly_create_plus_give():
    """The standalone path must be a composition, not a third implementation —
    otherwise the two copies of the SQL drift."""
    for engine, flavor in (("mysql", ""), ("postgres", ""),
                           ("sqlserver", "rds"), ("sqlserver", "azure_sql")):
        composed = _flat(
            sql.create_actor_plan(engine, username="jit_a_1", password="Pw-1",
                                  database="appdb", flavor=flavor)
            + sql.give_access_plan(engine, username="jit_a_1", database="appdb",
                                   role="read", flavor=flavor))
        assert _flat(sql.grant_plan(engine, username="jit_a_1", password="Pw-1",
                                    database="appdb", role="read",
                                    flavor=flavor)) == composed, (engine, flavor)


def test_composition_merges_adjacent_connections():
    """Each plan entry costs a real connection, and on Azure SQL that is a full TLS
    handshake. Composition must not turn two connections into three."""
    plan = sql.grant_plan("sqlserver", username="jit_a_1", password="Pw-1",
                          database="appdb", role="read", flavor="azure_sql")
    assert _databases(plan) == ["master", "appdb"], _databases(plan)
    # …and the statement order within a database is preserved.
    appdb = plan[1][1]
    assert appdb.index(next(s for s in appdb if "CREATE USER" in s)) < \
        appdb.index(next(s for s in appdb if "ALTER ROLE" in s))


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
