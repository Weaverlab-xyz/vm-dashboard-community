"""On-premises directories reached through a remote agent.

What these pin:

- register_onprem is refused without an active agent, without a Password Safe account,
  for an agent older than 2.8, for an unknown provider, and for a duplicate host:port;
- the row stores only ids and an account name — never a credential;
- an AD domain's base DN is derived when not given, and the bind identity becomes a UPN;
- directory_connection_vars checks the credential out just-in-time and returns the dir_*
  vars playbooks read, without writing anything back to the row.

Uses a real temp SQLite database; the Password Safe call is stubbed.

Run: python tests/test_onprem_directories.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="onprem-dir-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-onprem-directory-tests")

from web_dashboard.database import Base, ManagedDirectory, RemoteAgent, SessionLocal, engine  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402

Base.metadata.create_all(bind=engine)

ACCOUNT = {"system_id": 11, "account_id": 22, "account_name": "svc-ansible"}


def _agent(db, version="2.8.0", active=True, name=None):
    import uuid
    a = RemoteAgent(id=str(uuid.uuid4()), name=name or f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version=version, is_active=active)
    db.add(a)
    db.commit()
    return a


def _refuses(fn, needle):
    try:
        fn()
    except ds.DirectoryError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected a refusal mentioning {needle!r}")


def _reg(db, **kw):
    args = dict(name="corp.example.com", provider="onprem_ad", host="dc01.corp.example.com",
                agent_id="", managed_account=ACCOUNT, created_by="t")
    args.update(kw)
    return ds.register_onprem(db, **args)


def test_refusals():
    db = SessionLocal()
    good = _agent(db)
    old = _agent(db, version="2.7.3")
    retired = _agent(db, active=False)
    _refuses(lambda: _reg(db, agent_id="nope"), "not registered")
    _refuses(lambda: _reg(db, agent_id=retired.id), "not registered")
    _refuses(lambda: _reg(db, agent_id=old.id), "at least 2.8")
    _refuses(lambda: _reg(db, agent_id=good.id, managed_account={}), "Password Safe managed account")
    _refuses(lambda: _reg(db, agent_id=good.id, provider="nis"), "provider")
    _refuses(lambda: _reg(db, agent_id=good.id, host=""), "host is required")
    _refuses(lambda: _reg(db, agent_id=good.id, name="corp"), "not a usable AD domain")
    _refuses(lambda: _reg(db, agent_id=good.id, provider="ldap", name="ldap.example.com"),
             "base DN")
    db.close()


def test_register_ad_derives_base_dn_and_stores_no_secret():
    db = SessionLocal()
    a = _agent(db)
    row = _reg(db, agent_id=a.id, host="dc02.corp.example.com")
    assert row.cloud == "local" and row.source == "registered" and row.status == "available"
    assert row.base_dn == "DC=corp,DC=example,DC=com"
    assert row.port == 636 and row.use_ldaps is True
    assert row.credentials_ref.startswith("psmanaged:")
    assert "password" not in row.credentials_ref.lower()
    assert row.expires_at is None
    out = ds.to_dict(row)
    assert out["agent_id"] == a.id and out["bind_account"] == "svc-ansible"
    _refuses(lambda: _reg(db, agent_id=a.id, host="dc02.corp.example.com"), "already registered")
    db.close()


def test_register_ldap_uses_389_without_tls():
    db = SessionLocal()
    a = _agent(db)
    row = _reg(db, agent_id=a.id, provider="ldap", name="ldap.example.com",
               host="ldap1.example.com", base_dn="dc=example,dc=com", use_ldaps=False,
               managed_account={**ACCOUNT, "account_name": "cn=admin,dc=example,dc=com"})
    assert row.port == 389 and row.provider == "ldap"
    db.close()


def test_connection_vars_check_out_just_in_time():
    from web_dashboard.services import btapi_service
    db = SessionLocal()
    a = _agent(db)
    row = _reg(db, agent_id=a.id, host="dc03.corp.example.com", port=389, use_ldaps=False)
    calls = []

    async def checkout(system_id, account_id, duration_min=60, **kw):
        calls.append((system_id, account_id))
        return "req-1", "S3cret!bind"

    orig = btapi_service.get_ps_credential_with_request
    btapi_service.get_ps_credential_with_request = checkout
    try:
        v = asyncio.run(ds.directory_connection_vars(row))
    finally:
        btapi_service.get_ps_credential_with_request = orig
    assert calls == [(11, 22)]
    assert v["dir_bind_dn"] == "svc-ansible@corp.example.com"
    assert v["dir_bind_password"] == "S3cret!bind"
    assert v["dir_host"] == "dc03.corp.example.com" and v["dir_port"] == 389
    assert v["dir_use_ldaps"] is False and v["dir_domain"] == "corp.example.com"
    db.expire_all()
    fresh = db.query(ManagedDirectory).filter(ManagedDirectory.id == row.id).first()
    assert "S3cret" not in (fresh.credentials_ref or "")
    db.close()


def test_api_endpoint_needs_explicit_grant():
    import inspect
    from web_dashboard.api import directories as api
    src = inspect.getsource(api.register_onprem)
    assert 'require_explicit_permission("directories", "write")' in src


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
