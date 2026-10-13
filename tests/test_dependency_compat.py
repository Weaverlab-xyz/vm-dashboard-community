"""Behaviour that has to survive dependency upgrades Dependabot proposes.

What these pin:

- a bare ``postgresql://`` URL runs on psycopg2, the driver requirements.txt ships.
  SQLAlchemy 2.1 changed the default for that form to psycopg 3, which is not installed,
  and every documented URL is the bare form; a URL that names its driver is left alone;
- bcrypt sees at most 72 bytes of a password. bcrypt 4 dropped the rest silently and
  bcrypt 5 raises ValueError, so without the cut a long password at the login form is a
  500, and users whose long passwords were hashed by 4.x could no longer sign in;
- a NEW password over 72 bytes is refused wherever one is set, rather than accepted with
  its tail ignored.

Run: python tests/test_dependency_compat.py   (or under pytest)
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dep-compat-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dependency-compat")

import bcrypt  # noqa: E402

from web_dashboard import database  # noqa: E402


def test_a_bare_postgresql_url_is_pinned_to_psycopg2():
    url = database.engine_url("postgresql://u:p@db.example:5432/app?sslmode=require")
    assert url.drivername == "postgresql+psycopg2"
    assert (url.username, url.host, url.port, url.database) == ("u", "db.example", 5432, "app")
    assert url.query == {"sslmode": "require"}


def test_a_url_that_names_its_driver_is_left_alone():
    for raw in ("postgresql+psycopg://u@h/d", "postgresql+psycopg2://u@h/d",
                "sqlite:////tmp/x.db"):
        assert database.engine_url(raw).render_as_string(hide_password=False) == raw


def test_bcrypt_never_sees_more_than_72_bytes():
    assert database.bcrypt_input("a" * 100) == b"a" * 72
    assert database.bcrypt_input("short") == b"short"
    # Cut in bytes, not characters: a multi-byte character can be split, as bcrypt 4 did.
    assert len(database.bcrypt_input("é" * 50)) == 72


def test_a_long_password_hashes_verifies_and_never_raises():
    long_pw = "x" * 100
    h = database.get_password_hash(long_pw)
    assert database.verify_password(long_pw, h)
    assert not database.verify_password("y" * 100, h)


def test_a_hash_made_by_bcrypt_4_from_a_long_password_still_verifies():
    # bcrypt 4 hashed the first 72 bytes of whatever it was given.
    long_pw = "z" * 90
    legacy = bcrypt.hashpw(long_pw.encode()[:72], bcrypt.gensalt(4)).decode()
    assert database.verify_password(long_pw, legacy)


def test_the_portainer_admin_hash_cuts_at_72_bytes_too():
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "portainer_node_service.py"),
               encoding="utf-8").read()
    assert "raw = password.encode()[:72]" in src


def _source(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


def test_the_limit_counts_bytes_and_says_so():
    assert database.password_length_error("p" * 72) == ""
    assert database.password_length_error("") == ""
    assert "73 bytes" in database.password_length_error("p" * 73)
    # 19 characters, 76 bytes: the limit is bytes, because that is what bcrypt reads.
    assert "76 bytes" in database.password_length_error("\U0001F511" * 19)


def test_the_cli_refuses_a_new_password_over_72_bytes():
    try:
        database.create_admin_user("admin", "p" * 73)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "72 bytes" in str(exc)


def test_every_endpoint_that_sets_a_password_refuses_one_over_72_bytes():
    """A plain-string 400, not a Pydantic 422: the shared API helper and the setup
    wizard both show a string detail, and turn a 422's list into "HTTP 422"."""
    users = _source("web_dashboard", "api", "users.py")
    create = users[users.index("def create_user("):users.index("get_password_hash(body.password)")]
    assert "password_length_error(body.password)" in create
    update = users[users.index("def update_user("):]
    update = update[:update.index("user.hashed_password = get_password_hash(body.password)")]
    assert "password_length_error(body.password)" in update
    # The setup wizard, a reconfigure and a headless import all set the admin password
    # through _upsert_admin, which checks before it hashes.
    setup = _source("web_dashboard", "api", "setup.py")
    upsert = setup[setup.index("def _upsert_admin("):]
    upsert = upsert[:upsert.index("get_password_hash(password)")]
    assert "password_length_error(password)" in upsert


def test_upsert_admin_refuses_with_a_readable_400():
    from fastapi import HTTPException
    from web_dashboard.api.setup import _upsert_admin
    try:
        _upsert_admin("admin", "p" * 80)
        raise AssertionError("expected HTTPException")
    except HTTPException as exc:
        assert exc.status_code == 400 and "80 bytes" in exc.detail


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
