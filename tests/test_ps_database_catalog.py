"""Unit tests for services/ps_database_catalog.py (pure Password Safe → candidate shaping).

Loaded by file path (stdlib only) — no config / FastAPI / httpx / Password Safe needed.
Runs under pytest, or standalone:  python tests/test_ps_database_catalog.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ps_database_catalog.py")
_spec = importlib.util.spec_from_file_location("ps_database_catalog", _PATH)
cat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cat)


# ── raw Password Safe shapes ────────────────────────────────────────────────────

def _platform(pid, name, short="", default_port=None):
    out = {"PlatformID": pid, "Name": name, "ShortName": short}
    if default_port is not None:
        out["DefaultPort"] = default_port
    return out


def _system(sid, name, platform_id, **kw):
    out = {"ManagedSystemID": sid, "SystemName": name, "PlatformID": platform_id}
    out.update(kw)
    return out


def _database(did, **kw):
    out = {"DatabaseID": did}
    out.update(kw)
    return out


def _account(aid, system_id, name="svc-dba", **kw):
    out = {"ManagedAccountID": aid, "SystemId": system_id, "AccountName": name}
    out.update(kw)
    return out


def _build(systems, platforms=(), databases=(), accounts=(), **kw):
    return cat.build_candidates(platforms=list(platforms), systems=list(systems),
                                databases=list(databases), accounts=list(accounts), **kw)


def _one(systems, platforms=(), databases=(), accounts=(), **kw):
    rows, _ = _build(systems, platforms, databases, accounts, **kw)
    assert len(rows) == 1, f"expected exactly one candidate, got {len(rows)}"
    return rows[0]


# ── engine_for_platform: the spellings Password Safe actually ships ─────────────

def test_sqlserver_spellings():
    for name in ("MS SQL Server", "Microsoft SQL Server", "MSSQL", "SQL Server",
                 "MSSQL Azure Run Command Plugin", "mssql SSM Custom Plugin"):
        assert cat.engine_for_platform(name) == "sqlserver", name


def test_postgres_spellings():
    for name in ("PostgreSQL", "Postgres", "psql SSM Custom Plugin", "Greenplum"):
        assert cat.engine_for_platform(name) == "postgres", name


def test_mysql_spellings_including_mariadb():
    for name in ("MySQL", "MariaDB", "MySQL SSM Custom Plugin"):
        assert cat.engine_for_platform(name) == "mysql", name


def test_oracle_spelling():
    assert cat.engine_for_platform("Oracle") == "oracle"
    assert cat.engine_for_platform("OraDB Internal") == "oracle"


def test_oracle_mysql_is_mysql_not_oracle():
    # Oracle owns MySQL. If the oracle rule were checked first this registers as an
    # Oracle database and every later connection uses the wrong driver.
    assert cat.engine_for_platform("Oracle MySQL") == "mysql"
    assert cat.engine_for_platform("MySQL by Oracle") == "mysql"


def test_mysql_azure_plugin_is_not_swallowed_by_a_sqlserver_needle():
    # The trap: sqlserver is checked first, and a careless "sql" needle would match
    # "MySQL Azure Run Command Plugin" (and every PostgreSQL spelling too).
    assert cat.engine_for_platform("MySQL Azure Run Command Plugin") == "mysql"
    assert cat.engine_for_platform("PostgreSQL Azure Run Command Plugin") == "postgres"


def test_short_name_is_consulted_too():
    assert cat.engine_for_platform("Some Vendor Thing", "postgres") == "postgres"


def test_unknown_platform_maps_to_nothing():
    assert cat.engine_for_platform("Windows Server") == ""
    assert cat.engine_for_platform("Cisco IOS") == ""
    assert cat.engine_for_platform("") == ""
    assert cat.engine_for_platform(None) == ""


# ── parse_platform_map / operator overrides ─────────────────────────────────────

def test_platform_map_override_wins_over_the_builtin_rules():
    extra = cat.parse_platform_map('{"Percona Server": "mysql"}')
    assert cat.engine_for_platform("Percona Server", extra=extra) == "mysql"
    # And it can correct a built-in answer, which is the point of an override.
    extra = cat.parse_platform_map('{"Oracle MySQL": "oracle"}')
    assert cat.engine_for_platform("Oracle MySQL", extra=extra) == "oracle"


def test_platform_map_tolerates_junk_without_raising():
    assert cat.parse_platform_map("") == {}
    assert cat.parse_platform_map(None) == {}
    assert cat.parse_platform_map("not json at all") == {}
    assert cat.parse_platform_map("[1, 2, 3]") == {}          # valid JSON, wrong shape
    assert cat.parse_platform_map('{"X": "cassandra"}') == {}  # engine we don't have
    assert cat.parse_platform_map('{"X": "MySQL"}') == {"x": "mysql"}


# ── inclusion: what is kept, what is dropped ────────────────────────────────────

def test_a_plain_vm_is_dropped_entirely():
    # No DatabaseID and a platform that is not a database engine: not a candidate,
    # and showing it would bury the real ones.
    rows, _ = _build([_system(1, "dc01", 9, DnsName="dc01.corp")],
                     platforms=[_platform(9, "Windows Server")])
    assert rows == []


def test_a_database_row_with_an_unmapped_platform_is_kept_but_ineligible():
    # Kept on purpose: "my database isn't in the list" is otherwise undiagnosable.
    row = _one([_system(1, "weird", 9, DnsName="w.corp", DatabaseID=100)],
               platforms=[_platform(9, "Sybase ASE")])
    assert row["eligible"] is False
    assert "Sybase ASE" in row["reason"]
    assert row["engine"] == ""


def test_a_custom_plugin_system_with_no_database_row_is_kept():
    # This dashboard's own onboarding creates these; they carry no Databases row,
    # so an EntityTypeID filter would silently drop exactly them.
    row = _one([_system(1, "pg-ssm", 7, DnsName="pg.corp")],
               platforms=[_platform(7, "psql SSM Custom Plugin")],
               accounts=[_account(50, 1)])
    assert row["eligible"] is True
    assert row["engine"] == "postgres"


# ── the four ineligibility reasons ──────────────────────────────────────────────

def test_ineligible_when_the_dashboard_already_manages_it():
    row = _one([_system(1, "pg-ours", 7, DnsName="pg.corp", DatabaseID=100)],
               platforms=[_platform(7, "psql SSM Custom Plugin")],
               accounts=[_account(50, 1)],
               dashboard_platforms=("psql SSM Custom Plugin",))
    assert row["eligible"] is False
    assert row["reason"] == cat.REASON_DASHBOARD_MANAGED


def test_ineligible_when_the_platform_maps_to_no_engine():
    row = _one([_system(1, "x", 9, DnsName="x.corp", DatabaseID=100)],
               platforms=[_platform(9, "Sybase ASE")],
               accounts=[_account(50, 1)])
    assert row["eligible"] is False
    assert row["reason"] == cat.REASON_NO_ENGINE.format(platform="Sybase ASE")


def test_ineligible_when_there_is_no_address_to_connect_to():
    row = _one([_system(1, "pg", 1, DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1)])
    assert row["eligible"] is False
    assert row["reason"] == cat.REASON_NO_HOST


def test_ineligible_when_no_account_is_requestable():
    # The whole point of sourcing accounts from the REQUESTABLE list: this is the
    # 4031/403 Requestor-role failure, caught before the import instead of hours
    # later on the first playbook run.
    row = _one([_system(1, "pg", 1, DnsName="pg.corp", DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")])
    assert row["eligible"] is False
    assert row["reason"] == cat.REASON_NO_ACCOUNT
    assert "Requestor" in row["reason"]


def test_a_complete_system_is_eligible_with_no_reason():
    row = _one([_system(1, "pg", 1, DnsName="pg.corp", DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1)])
    assert row["eligible"] is True
    assert row["reason"] == ""


# ── host preference and port precedence ─────────────────────────────────────────

def test_host_prefers_dns_then_hostname_then_ip():
    def host(**kw):
        return _one([_system(1, "s", 1, DatabaseID=100, **kw)],
                    platforms=[_platform(1, "PostgreSQL")])["host"]
    assert host(DnsName="a.corp", HostName="b", IPAddress="10.0.0.1") == "a.corp"
    assert host(HostName="b", IPAddress="10.0.0.1") == "b"
    assert host(IPAddress="10.0.0.1") == "10.0.0.1"


def test_port_precedence_chain():
    def port(system_kw=None, db_kw=None, default_port=None):
        systems = [_system(1, "s", 1, DnsName="h", DatabaseID=100, **(system_kw or {}))]
        return _one(systems,
                    platforms=[_platform(1, "PostgreSQL", default_port=default_port)],
                    databases=[_database(100, **(db_kw or {}))])["port"]
    assert port({"Port": 15432}, {"Port": 5433}, 5555) == 15432   # the system wins
    assert port(None, {"Port": 5433}, 5555) == 5433               # then the database
    assert port(None, None, 5555) == 5555                         # then the platform
    assert port(None, None, None) == 5432                         # then the engine default


def test_db_name_ignores_a_default_instance():
    def db_name(**db_kw):
        return _one([_system(1, "s", 1, DnsName="h", DatabaseID=100)],
                    platforms=[_platform(1, "MS SQL Server")],
                    databases=[_database(100, **db_kw)])["db_name"]
    assert db_name(InstanceName="APPDB", IsDefaultInstance=False) == "APPDB"
    assert db_name(InstanceName="MSSQLSERVER", IsDefaultInstance=True) == ""


# ── accounts ────────────────────────────────────────────────────────────────────

def test_accounts_are_grouped_onto_their_own_system_only():
    rows, _ = _build([_system(1, "a", 1, DnsName="a"), _system(2, "b", 1, DnsName="b")],
                     platforms=[_platform(1, "PostgreSQL")],
                     accounts=[_account(50, 1, "one"), _account(51, 2, "two")])
    by_id = {r["system_id"]: r for r in rows}
    assert [a["name"] for a in by_id[1]["accounts"]] == ["one"]
    assert [a["name"] for a in by_id[2]["accounts"]] == ["two"]


def test_plain_account_names_sort_ahead_of_scope_suffixed_ones():
    # The dropdown preselects the first entry; "svc-dba" is far more often the
    # intended login than the cloud-plugin "svc-dba;local".
    row = _one([_system(1, "s", 1, DnsName="h")],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1, "svc-dba;local"), _account(51, 1, "svc-dba")])
    assert [a["name"] for a in row["accounts"]] == ["svc-dba", "svc-dba;local"]


def test_ssh_key_flag_is_read_from_dss_auto_management():
    row = _one([_system(1, "s", 1, DnsName="h")],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1, DSSAutoManagementFlag=True)])
    assert row["accounts"][0]["uses_ssh_key"] is True


def test_alternate_id_field_names_are_accepted():
    # Password Safe field names vary by collection and API version.
    rows, _ = _build([{"SystemID": 1, "Name": "pg", "PlatformId": 1, "DnsName": "h",
                       "DatabaseId": 100}],
                     platforms=[{"PlatformId": 1, "Name": "PostgreSQL"}],
                     accounts=[{"AccountID": 50, "SystemID": 1, "Name": "svc"}])
    assert len(rows) == 1
    assert rows[0]["eligible"] is True
    assert rows[0]["accounts"][0]["account_id"] == 50


# ── text hygiene ────────────────────────────────────────────────────────────────

def test_clean_text_strips_control_characters_and_newlines():
    assert cat._clean_text("pg\x1b[31mprod") == "pg[31mprod"
    assert cat._clean_text("line\nbreak") == "linebreak"
    assert cat._clean_text("tab\there") == "tab\there"      # tab survives
    assert cat._clean_text("  spaced  ") == "spaced"
    assert cat._clean_text(None) == ""


def test_clean_text_truncates():
    assert len(cat._clean_text("x" * 10000)) == cat.MAX_TEXT


def test_hostile_names_are_sanitised_on_the_way_to_the_browser():
    # A managed-system name and an account name are typed by whoever onboarded the
    # system. They reach an operator's browser, and their terminal on copy.
    row = _one([_system(1, "pg\x1b]0;pwned\x07", 1, DnsName="h\nx")],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1, "svc\x1b[2J")])
    assert "\x1b" not in row["name"]
    assert "\n" not in row["host"]
    assert "\x1b" not in row["accounts"][0]["name"]


# ── the projection is closed ────────────────────────────────────────────────────

def test_no_password_safe_field_leaks_through_the_projection():
    row = _one([_system(1, "pg", 1, DnsName="h", DatabaseID=100,
                        Password="hunter2", FunctionalAccountID=9,
                        Description="internal", ChangeFrequencyDays=30)],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1, Password="hunter2", ApiEnabled=True)])
    assert set(row) <= set(cat.CANDIDATE_KEYS)
    assert set(row) == set(cat.CANDIDATE_KEYS)     # and the shape is stable for the UI
    for account in row["accounts"]:
        assert set(account) <= set(cat.ACCOUNT_KEYS)
    assert "hunter2" not in repr(row)


def test_already_imported_defaults_false_and_is_not_decided_here():
    row = _one([_system(1, "pg", 1, DnsName="h", DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1)])
    assert row["already_imported"] is False


# ── ordering and the cap ────────────────────────────────────────────────────────

def test_eligible_rows_sort_first():
    rows, _ = _build(
        [_system(1, "aaa-broken", 1, DatabaseID=100),          # no host → ineligible
         _system(2, "zzz-fine", 1, DnsName="z", DatabaseID=101)],
        platforms=[_platform(1, "PostgreSQL")],
        accounts=[_account(50, 1), _account(51, 2)])
    assert [r["system_id"] for r in rows] == [2, 1]


def test_the_cap_truncates_and_says_so():
    systems = [_system(i, f"pg{i:03d}", 1, DnsName=f"h{i}", DatabaseID=100 + i)
               for i in range(10)]
    rows, truncated = _build(systems, platforms=[_platform(1, "PostgreSQL")],
                             max_candidates=4)
    assert len(rows) == 4 and truncated is True
    rows, truncated = _build(systems, platforms=[_platform(1, "PostgreSQL")],
                             max_candidates=50)
    assert len(rows) == 10 and truncated is False


def test_empty_input_is_not_an_error():
    assert _build([]) == ([], False)
    assert cat.build_candidates(platforms=None, systems=None,
                                databases=None, accounts=None) == ([], False)


# ── import_request ──────────────────────────────────────────────────────────────

def test_import_request_matches_the_register_endpoint_shape():
    row = _one([_system(1, "pg", 1, DnsName="pg.corp", Port=5432, DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")],
               databases=[_database(100, InstanceName="app")],
               accounts=[_account(50, 1, "svc-dba")])
    req = cat.import_request(row, cloud="local", account_id=50)
    assert set(req) == {"engine", "cloud", "host", "port", "db_name",
                        "region", "instance_id", "managed_account"}
    assert req["engine"] == "postgres"
    assert req["cloud"] == "local"
    assert req["host"] == "pg.corp"
    assert req["port"] == 5432
    assert req["db_name"] == "app"
    assert set(req["managed_account"]) == {"system_id", "account_id",
                                           "account_name", "uses_ssh_key"}
    assert req["managed_account"] == {"system_id": 1, "account_id": 50,
                                      "account_name": "svc-dba", "uses_ssh_key": False}


def test_import_request_never_carries_a_region_or_instance_id():
    # instance_id is what inventory_service uses to build a display name — a
    # Password Safe id there makes every imported row read "postgres psms:42".
    row = _one([_system(1, "pg", 1, DnsName="pg.corp", DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1)])
    req = cat.import_request(row, cloud="aws", account_id=50)
    assert req["region"] == ""
    assert req["instance_id"] == ""


def test_import_request_uses_the_accounts_own_name_not_the_callers():
    row = _one([_system(1, "pg", 1, DnsName="pg.corp", DatabaseID=100)],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1, "svc-dba;local", DSSAutoManagementFlag=True)])
    req = cat.import_request(row, cloud="local", account_id=50)
    # The ";" suffix is significant downstream — it is stripped at ansible_user
    # time by managed_accounts.ssh_login_user, not here.
    assert req["managed_account"]["account_name"] == "svc-dba;local"
    assert req["managed_account"]["uses_ssh_key"] is True


def test_find_account_refuses_an_account_from_another_system():
    row = _one([_system(1, "pg", 1, DnsName="h")],
               platforms=[_platform(1, "PostgreSQL")],
               accounts=[_account(50, 1)])
    assert cat.find_account(row, 50)["account_id"] == 50
    assert cat.find_account(row, 999) == {}      # not on this system
    assert cat.find_account(row, None) == {}
    assert cat.find_account(row, "nonsense") == {}


# ── the duplicated constants must not drift ─────────────────────────────────────

def _service_literal(name):
    """Pull a constant's literal out of cloud_database_service.py as TEXT.

    Read rather than imported because that module pulls in SQLAlchemy, FastAPI and
    the config — the whole point of ps_database_catalog being stdlib-only is that
    this file runs without them.
    """
    import ast
    import re
    path = os.path.join(_ROOT, "web_dashboard", "services", "cloud_database_service.py")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    match = re.search(rf"^{re.escape(name)}\s*=\s*(\{{[^}}]*\}})", source, re.M)
    assert match, f"{name} not found in cloud_database_service.py — did it move or change shape?"
    return ast.literal_eval(match.group(1))


def test_engines_and_default_ports_match_cloud_database_service():
    # ps_database_catalog copies these because it may not import the service. A copy
    # that drifts sends an import to the wrong port, or offers an engine the
    # register endpoint will refuse, and nothing else would notice.
    assert set(cat.VALID_ENGINES) == _service_literal("VALID_ENGINES")
    assert cat._DEFAULT_PORTS == _service_literal("_DEFAULT_PORTS")


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
