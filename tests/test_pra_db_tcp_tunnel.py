"""The raw-TCP DB tunnel (Oracle, MongoDB) — PRA's generic sra_protocol_tunnel_jump.

Oracle shipped with ``tunnel_type = "tcp"`` and nothing else, which is not a tunnel: a
tcp protocol tunnel is a PORT FORWARD and PRA needs ``tunnel_definitions`` +
``tunnel_listen_address`` to know what to forward (the k8s API tunnel, the one tcp
tunnel known to work live, always sent both). These pin that shape, and that the
protocol-aware tunnels (SQL Server's ``mssql``, the dedicated postgres/mysql
resources) are unchanged.

When the beyondtrust/sra provider ships the PRA 26.3 Oracle/MongoDB tunnel resources,
_DB_TUNNEL_RESOURCE / _DB_TUNNEL_TYPE change and these tests move with them.

Runs under pytest or standalone:  python tests/test_pra_db_tcp_tunnel.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import terraform_pra_service as pra  # noqa: E402


def _hcl(engine, **kw):
    kw.setdefault("username", "dbadmin")
    kw.setdefault("database", "app_db")
    return pra._generate_db_tunnel_hcl(
        engine=engine, name="clouddb-abc123", hostname="10.0.0.5",
        jump_group_name="centralus", jumpoint_name="GCP Run", tag="clouddb", **kw)


def test_oracle_and_mongodb_are_tcp_port_forwards():
    for engine, port in (("oracle", 1521), ("mongodb", 27017)):
        hcl = _hcl(engine, port=port)
        assert 'resource "sra_protocol_tunnel_jump"' in hcl, engine
        assert 'tunnel_type   = "tcp"' in hcl, engine
        assert f'tunnel_definitions    = "{port};{port}"' in hcl, engine
        assert 'tunnel_listen_address = "127.0.0.1"' in hcl, engine


def test_a_tcp_tunnel_falls_back_to_the_engine_default_port():
    assert 'tunnel_definitions    = "1521;1521"' in _hcl("oracle")
    assert 'tunnel_definitions    = "27017;27017"' in _hcl("mongodb")


def test_a_custom_port_is_forwarded_as_given():
    assert 'tunnel_definitions    = "1522;1522"' in _hcl("oracle", port=1522)


def test_a_tcp_tunnel_sends_no_login_fields():
    # username/database are what the protocol-aware tunnels log in with; a byte
    # forward has no use for them.
    hcl = _hcl("oracle", port=1521)
    assert "username      =" not in hcl
    assert "database      =" not in hcl


def test_sqlserver_keeps_its_protocol_aware_tunnel():
    hcl = _hcl("sqlserver", port=1433)
    assert 'tunnel_type   = "mssql"' in hcl
    assert 'username      = "dbadmin"' in hcl
    assert 'database      = "app_db"' in hcl
    assert "tunnel_definitions" not in hcl


def test_dedicated_resources_take_no_tunnel_type_or_definitions():
    for engine, resource in (("postgres", "sra_postgresql_tunnel_jump"),
                             ("mysql", "sra_my_sql_tunnel_jump")):
        hcl = _hcl(engine, port=5432)
        assert f'resource "{resource}"' in hcl
        assert "tunnel_type" not in hcl
        assert "tunnel_definitions" not in hcl


def test_the_vault_account_still_rides_a_tcp_tunnel():
    hcl = _hcl("mongodb", port=27017, vault_account_name="clouddb-abc123-admin",
               vault_username="dbadmin")
    assert 'resource "sra_vault_username_password_account" "db_admin"' in hcl
    assert 'username    = "dbadmin"' in hcl


def test_destroy_resolves_the_engine_from_the_states_tunnel_type():
    # Several engines share the generic resource, so the resource type alone used to
    # resolve to whichever engine was listed last — a SQL Server tunnel was rebuilt as
    # a tcp one at destroy time.
    f = pra._engine_from_tunnel_state
    assert f("sra_protocol_tunnel_jump", {"tunnel_type": "mssql"}) == "sqlserver"
    assert pra._DB_TUNNEL_TYPE[f("sra_protocol_tunnel_jump", {"tunnel_type": "tcp"})] == "tcp"
    assert f("sra_postgresql_tunnel_jump", {}) == "postgres"
    assert f("sra_my_sql_tunnel_jump", {}) == "mysql"
    assert f("sra_vault_username_password_account", {}) is None


def test_every_engine_has_a_tunnel():
    for engine in ("postgres", "mysql", "sqlserver", "oracle", "mongodb"):
        assert engine in pra._DB_TUNNEL_RESOURCE, engine


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
