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
    name = sql.ephemeral_username("alice@example.com", "AB12cd34EF56gh", "postgres")
    assert name.startswith("jit_alice_example_com"), name
    assert sql._IDENT_RE.match(name), name
    # Two grants for the same person must not collide.
    other = sql.ephemeral_username("alice@example.com", "ZZ99", "postgres")
    assert name != other


def test_ephemeral_username_fits_the_engine_it_is_for():
    """MySQL stores account names in a char(32); Postgres allows 63 and SQL Server
    128. One blind cap of 63 is what broke every live MySQL grant on 2026-08-31 —
    ``jit_`` + a 24-char slug + ``_`` + a 12-char token is 41 characters, so MySQL
    rejected the CREATE USER outright (1470) and pymysql surfaced it as a bare
    OperationalError.
    """
    for engine in ("mysql", "postgres", "sqlserver"):
        name = sql.ephemeral_username("karen.walker@weaverlab.xyz", "6a95b8ad0000",
                                      engine)
        assert len(name) <= sql.max_identifier_length(engine), (engine, name)
        # Short enough to store, and still says whose account it is.
        assert name.startswith("jit_karen"), (engine, name)
        # Usable, which is the property the length is FOR.
        sql.grant_plan(engine, username=name, password="Pw-1", database="appdb",
                       role="read")


def test_an_unknown_engine_gets_the_SMALLEST_limit():
    """A name that is too short is ugly; a name that is too long does not exist. So
    the default must be the tightest engine, never the loosest."""
    name = sql.ephemeral_username("karen.walker@weaverlab.xyz", "6a95b8ad0000")
    assert len(name) <= min(sql._MAX_IDENT.values()), name
    assert len(name) <= sql.max_identifier_length("mysql")


def test_the_collision_suffix_is_never_what_gets_clipped():
    """Two grants colliding is a correctness bug; a clipped slug is a legibility
    one. When the budget is tight the slug pays, not the token."""
    a = sql.ephemeral_username("karen.walker@weaverlab.xyz", "aaaaaaaaaaaa", "mysql")
    b = sql.ephemeral_username("karen.walker@weaverlab.xyz", "bbbbbbbbbbbb", "mysql")
    assert a != b
    assert a.endswith("aaaaaaaaaaaa") and b.endswith("bbbbbbbbbbbb")


def test_a_name_longer_than_the_engine_allows_is_refused_not_attempted():
    """A CloudDbSqlError is a 400 on the route that asked for it. Letting the driver
    refuse it instead is the 500 that took a Cloud Logging session to diagnose."""
    too_long = "jit_karen_walker_weaverlab_x_6a95b8ad0000"   # the live failure, verbatim
    assert len(too_long) == 41
    for builder in (
        lambda u: sql.create_actor_plan("mysql", username=u, password="Pw-1",
                                        database="appdb"),
        lambda u: sql.give_access_plan("mysql", username=u, database="appdb"),
        lambda u: sql.delete_actor_plan("mysql", username=u, database="appdb"),
        lambda u: sql.grant_plan("mysql", username=u, password="Pw-1",
                                 database="appdb", role="readwrite"),
    ):
        try:
            builder(too_long)
        except sql.CloudDbSqlError as exc:
            assert "32" in str(exc) and "41" in str(exc), exc
        else:
            raise AssertionError("a 41-character MySQL account name was accepted")
    # The same name is fine where the engine actually allows it.
    sql.create_actor_plan("postgres", username=too_long, password="Pw-1",
                          database="appdb")


def test_ephemeral_username_survives_hostile_input():
    for engine in ("mysql", "postgres", "sqlserver"):
        for prefix in ("", "'; DROP TABLE x;--", "!!!", "x" * 200, "123"):
            name = sql.ephemeral_username(prefix, "tok123", engine)
            assert sql._IDENT_RE.match(name), (prefix, name)
            assert len(name) <= sql.max_identifier_length(engine), (prefix, name)
            # And it must be usable as a real account name.
            sql.grant_plan(engine, username=name, password="Pw-1",
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


def test_readwrite_can_actually_read_on_every_engine():
    """SQL Server's fixed database roles do not nest: db_datawriter grants INSERT,
    UPDATE and DELETE and no SELECT. A readwrite account holding only that role can
    write rows it cannot read back — and db_grant advertises readwrite to Entitle as
    SELECT, INSERT, UPDATE, DELETE, so the adapter would be granting less than it
    said it did."""
    for engine, flavor in (("mysql", ""), ("postgres", ""),
                           ("sqlserver", "rds"), ("sqlserver", "azure_sql")):
        give = _flat(sql.give_access_plan(engine, username="jit_a_1",
                                          database="appdb", role="readwrite",
                                          flavor=flavor))
        reads = "SELECT" in give or "db_datareader" in give
        assert reads, f"{engine}/{flavor} readwrite grants no read: {give}"


def test_revoking_a_role_the_account_never_held_is_a_no_op():
    """An account granted readwrite before that mapped to two roles holds only
    db_datawriter, and its revoke now names db_datareader too. Unguarded, that
    DROP MEMBER fails the whole revoke and leaves the access it does hold in place."""
    take = _flat(sql.revoke_access_plan("sqlserver", username="jit_a_1",
                                        database="appdb", role="readwrite",
                                        flavor="azure_sql"))
    for name in ("db_datareader", "db_datawriter"):
        assert f"IS_ROLEMEMBER('{name}'" in take, (name, take)


def test_read_grants_only_read():
    """The other half: readwrite gaining a role must not hand write access to the
    read code."""
    for engine, flavor in (("mysql", ""), ("postgres", ""),
                           ("sqlserver", "rds"), ("sqlserver", "azure_sql")):
        give = _flat(sql.give_access_plan(engine, username="jit_a_1",
                                          database="appdb", role="read",
                                          flavor=flavor))
        for write in ("INSERT", "UPDATE", "DELETE", "db_datawriter"):
            assert write not in give, f"{engine}/{flavor} read grants {write}: {give}"


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
