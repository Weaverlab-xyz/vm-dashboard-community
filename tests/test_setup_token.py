"""First-run setup needs the one-time setup token, so the first visitor cannot claim a server.

Before: on a fresh instance ``POST /api/setup/complete`` and ``POST /api/setup/import``
created the first administrator for anyone who asked -- whoever reached the port first
owned the dashboard. Now (services/setup_token.py) both need an ``X-Setup-Token`` header
matching ``SETUP_TOKEN`` from the environment, or else the random token the app writes to
``<data dir>/setup_token`` (mode 0600) and names -- by path only -- in its log. The file
is deleted once setup completes.

Runs standalone:
    python tests/test_setup_token.py
"""
import logging
import os
import stat
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="setup-token-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "setup-token-test-secret")
os.environ.pop("SETUP_TOKEN", None)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard import main  # noqa: E402
from web_dashboard.config import settings  # noqa: E402
from web_dashboard.database import Base, SessionLocal, User, engine  # noqa: E402
from web_dashboard.services import config_service, setup_token  # noqa: E402

Base.metadata.create_all(bind=engine)

TOKEN_FILE = os.path.join(_TMPDIR, "setup_token")
settings.setup_token = ""
settings.setup_token_file = TOKEN_FILE

WIZARD = {"admin": {"username": "first", "password": "a-long-password"},
          "profile": {"install_profile": "demo"}, "aws": {}, "azure": {},
          "features": {}}
IMPORT = {"admin_username": "first", "admin_password": "a-long-password",
          "config": {"aws_default_region": "eu-west-1"}}


def _fresh():
    """Back to a never-set-up instance with no token file."""
    config_service.delete("setup_complete")
    config_service._setup_complete = None
    db = SessionLocal()
    try:
        db.query(User).delete()
        db.commit()
    finally:
        db.close()
    if os.path.exists(TOKEN_FILE):
        os.unlink(TOKEN_FILE)
    setup_token._announced = False


def _users():
    db = SessionLocal()
    try:
        return db.query(User).count()
    finally:
        db.close()


def test_the_token_file_is_private_and_the_log_names_only_its_path():
    _fresh()
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger(setup_token.__name__).addHandler(handler)
    try:
        token = setup_token.ensure()
    finally:
        logging.getLogger(setup_token.__name__).removeHandler(handler)
    assert token and len(token) >= 24
    assert stat.S_IMODE(os.stat(TOKEN_FILE).st_mode) == 0o600
    logged = " ".join(r.getMessage() for r in records)
    assert TOKEN_FILE in logged and token not in logged, logged
    assert setup_token.ensure() == token, "a second call (another worker) made a new token"


def test_the_wizard_submit_needs_the_token():
    _fresh()
    c = TestClient(main.app)
    token = setup_token.ensure()
    for headers in ({}, {"X-Setup-Token": "guess"}, {"X-Setup-Token": token[:-1]}):
        r = c.post("/api/setup/complete", json=WIZARD, headers=headers)
        assert r.status_code == 403 and "setup token" in r.text, (headers, r.text)
    assert _users() == 0 and not config_service.is_setup_complete()
    r = c.post("/api/setup/complete", json=WIZARD, headers={"X-Setup-Token": token})
    assert r.status_code == 201, r.text
    assert config_service.is_setup_complete()
    assert not os.path.exists(TOKEN_FILE), "the token outlived setup"


def test_a_first_run_import_needs_the_token():
    _fresh()
    c = TestClient(main.app)
    token = setup_token.ensure()
    assert c.post("/api/setup/import", json=IMPORT).status_code == 403
    assert _users() == 0
    r = c.post("/api/setup/import", json=IMPORT, headers={"X-Setup-Token": token})
    assert r.status_code == 200, r.text
    assert not os.path.exists(TOKEN_FILE)
    # Afterwards the token means nothing: a merge needs an administrator.
    assert c.post("/api/setup/import", json=IMPORT,
                  headers={"X-Setup-Token": token}).status_code == 401


def test_setup_token_in_the_environment_wins_over_the_file():
    _fresh()
    file_token = setup_token.ensure()
    settings.setup_token = "from-the-environment"
    try:
        class _Req:
            def __init__(self, t):
                self.headers = {"X-Setup-Token": t}
        setup_token.require(_Req("from-the-environment"))
        try:
            setup_token.require(_Req(file_token))
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("the file's token was accepted while SETUP_TOKEN is set")
    finally:
        settings.setup_token = ""


def test_the_wizard_sends_the_token():
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "setup.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "headers['X-Setup-Token'] = this.setupToken.trim();" in src
    with open(os.path.join(_ROOT, "scripts", "ci", "browser_smoke.sh"), encoding="utf-8") as fh:
        assert 'X-Setup-Token: $setup_token' in fh.read()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
