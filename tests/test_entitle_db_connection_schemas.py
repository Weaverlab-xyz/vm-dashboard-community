"""Entitle DB connector payloads, per engine.

Entitle validates ``connection_json`` against the connector's schema, and the key names
are NOT uniform across its DB connectors. Oracle used to fall into the postgres branch
(``user`` + an ``options`` object, no service name) — a payload no Oracle schema can
match. These pin the documented shapes and that an engine with no connector raises
instead of borrowing another engine's.

Oracle: docs.beyondtrust.com/entitle/docs/entitle-integration-oracle_database

Runs under pytest or standalone:  python tests/test_entitle_db_connection_schemas.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


CONF = {"entitle_owner_id": "owner-1", "entitle_workflow_id": "wf-1",
        "entitle_agent_token_name": "agent-1",
        "entitle_api_key": "key-1"}

_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)))

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace())

from web_dashboard.services import entitle_registration_service as ers  # noqa: E402


def _conn(engine, **kw):
    args = dict(engine=engine, host="10.0.0.5", port=1521, username="ADMIN",
                database="APPPDB", version="")
    args.update(kw)
    return ers._db_connection_json_hcl(**args)


def test_oracle_uses_its_own_key_names():
    hcl = _conn("oracle")
    assert 'username     = "ADMIN"' in hcl
    assert 'service_name = "APPPDB"' in hcl
    assert 'host         = "10.0.0.5"' in hcl
    assert 'port         = "1521"' in hcl
    assert "password     = var.db_password" in hcl
    # The postgres shape it used to borrow.
    assert "user     =" not in hcl
    assert "options" not in hcl


def test_oracle_protocol_is_sent_only_when_set():
    assert "protocol" not in _conn("oracle")
    assert 'protocol     = "tcps"' in _conn("oracle", version="tcps")


def test_postgres_shape_is_unchanged():
    hcl = _conn("postgres", port=5432, database="ignored")
    assert 'user     = "ADMIN"' in hcl
    assert "databases_constraints" in hcl
    assert "ignored" not in hcl


def test_an_engine_without_a_connector_raises_instead_of_borrowing_one():
    for engine in ("mongodb", "cassandra"):
        try:
            _conn(engine)
        except ers.EntitleRegistrationError:
            continue
        raise AssertionError(f"{engine} got another engine's connection_json")


def test_mongodb_cannot_register_as_a_db_connector():
    try:
        ers._generate_db_hcl(engine="mongodb", name="m", host="h", port=27017,
                             username="u", database="admin", version="", private=True)
    except ers.EntitleRegistrationError:
        return
    raise AssertionError("mongodb registered against a connector Entitle does not have")


def test_oracle_registers_with_ephemeral_accounts():
    hcl = ers._generate_db_hcl(engine="oracle", name="clouddb-x", host="h", port=1521,
                               username="ADMIN", database="APPPDB", version="",
                               private=True)
    assert 'application = { name = "oracle database" }' in hcl
    assert "allow_creating_accounts = true" in hcl


def test_mints_table():
    assert ers.db_connector_mints("postgres") is True
    assert ers.db_connector_mints("sqlserver") is True
    assert ers.db_connector_mints("oracle") is True
    assert ers.db_connector_mints("mysql") is False
    assert ers.db_connector_mints("mongodb") is False


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
