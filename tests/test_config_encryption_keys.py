"""The JWT key must be configured, and stored secrets can have a key of their own.

Before: with no JWT key configured, each process generated one at import. The two gunicorn
workers held different keys, so a login from one failed on the other; every restart made a
new one, so every stored secret became undecryptable; and ``_decrypt`` then returned the
CIPHERTEXT as if it were the value. The at-rest key was also derived from the JWT key, so
leaking the one (C1 in the audit read it straight off disk) leaked both.

Now:
  * the app and the job worker refuse to start without a JWT key;
  * ``CONFIG_ENCRYPTION_KEY`` (optional) encrypts stored secrets; rows under the legacy
    JWT-derived key still read, and are re-encrypted under the new key at boot;
  * a value that IS ciphertext and will not decrypt reads as "" and is logged, never as
    the ciphertext. A plain-text legacy row still reads as itself.

Runs standalone:
    python tests/test_config_encryption_keys.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="config-keys-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "config-keys-test-jwt")

from cryptography.fernet import InvalidToken  # noqa: E402

from web_dashboard.config import Settings, settings  # noqa: E402
from web_dashboard.database import (AppConfig, Base, NotificationEndpoint,  # noqa: E402
                                    SessionLocal, engine)
from web_dashboard.services import config_service as cs  # noqa: E402

Base.metadata.create_all(bind=engine)


class _Keys:
    """Set the two keys for one block, then put them back."""

    def __init__(self, jwt=None, enc=""):
        self.jwt, self.enc = jwt, enc

    def __enter__(self):
        self.saved = (settings.jwt_secret_key, settings.config_encryption_key)
        if self.jwt is not None:
            settings.jwt_secret_key = self.jwt
        settings.config_encryption_key = self.enc
        return self

    def __exit__(self, *exc):
        settings.jwt_secret_key, settings.config_encryption_key = self.saved


# ── the JWT key is required ───────────────────────────────────────────────────

def test_there_is_no_generated_default():
    saved = os.environ.pop("JWT_SECRET_KEY", None)
    try:
        assert Settings(_env_file=None).jwt_secret_key == ""
    finally:
        if saved is not None:
            os.environ["JWT_SECRET_KEY"] = saved


def test_a_server_process_refuses_to_start_without_one():
    with _Keys(jwt=""):
        try:
            settings.require_jwt_secret_key()
        except RuntimeError as exc:
            assert "JWT_SECRET_KEY" in str(exc) and "JWT_SECRET_KEY_FILE" in str(exc)
        else:
            raise AssertionError("started without a JWT key")
    settings.require_jwt_secret_key()  # configured: no error


def test_both_entry_points_check_before_touching_the_database():
    for rel, func in (("main.py", "async def lifespan("), ("jobs_worker.py", "def main(")):
        with open(os.path.join(_ROOT, "web_dashboard", rel), encoding="utf-8") as fh:
            body = fh.read().split(func, 1)[1]
        assert "require_jwt_secret_key()" in body, rel
        assert body.index("require_jwt_secret_key()") < body.index("init_db()"), rel


def test_the_encryption_key_loads_from_a_file():
    path = os.path.join(_TMPDIR, "enc_key")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("from-a-file\n")
    s = Settings(_env_file=None, jwt_secret_key="x", config_encryption_key_file=path)
    assert s.config_encryption_key == "from-a-file"


# ── decrypt never returns ciphertext ──────────────────────────────────────────

def test_plain_text_legacy_values_still_read_as_themselves():
    for plain in ("hunter2", "", "aws_sm://prod/db", "gAAAA-not-really"):
        assert cs._decrypt(plain) == plain


def test_ciphertext_under_another_key_reads_as_empty_not_as_ciphertext():
    with _Keys(jwt="the-old-key"):
        token = cs._encrypt("root-password")
    with _Keys(jwt="a-different-key"):
        assert cs._decrypt(token) == "", "ciphertext was handed back as the value"
        assert cs.decrypt_value(token) == ""


# ── the separate encryption key ───────────────────────────────────────────────

def test_without_the_new_key_nothing_changes():
    with _Keys(enc=""):
        token = cs._encrypt("v")
        assert cs._derive(settings.jwt_secret_key).decrypt(token.encode()) == b"v"


def test_with_the_new_key_new_values_are_under_it_and_old_ones_still_read():
    with _Keys(enc=""):
        old = cs._encrypt("written-before")
    with _Keys(enc="the-data-key"):
        assert cs._decrypt(old) == "written-before"
        new = cs._encrypt("written-after")
        assert cs._decrypt(new) == "written-after"
        try:
            cs._derive(settings.jwt_secret_key).decrypt(new.encode())
        except InvalidToken:
            pass
        else:
            raise AssertionError("a new value is still readable with the JWT-derived key")
        assert cs.decrypt_value_strict(new) == "written-after"


def test_boot_moves_long_lived_secrets_onto_the_new_key():
    db = SessionLocal()
    db.query(AppConfig).delete()
    db.query(NotificationEndpoint).delete()
    db.commit()
    with _Keys(enc=""):
        db.add(AppConfig(key="proxmox_password", value=cs._encrypt("pve-root")))
        db.add(AppConfig(key="plain_legacy", value="not-encrypted"))
        db.add(NotificationEndpoint(name="hook", url=cs.encrypt_value("https://hook/x"),
                                    secret=cs.encrypt_value("hmac-key")))
        db.commit()
        assert cs.reencrypt_stored_secrets(db) == 0, "no new key: nothing to move"

    with _Keys(enc="the-data-key"):
        assert cs.reencrypt_stored_secrets(db) == 3
        assert cs.reencrypt_stored_secrets(db) == 0, "a second boot has nothing left"
        legacy = cs._derive(settings.jwt_secret_key)
        row = db.query(AppConfig).filter(AppConfig.key == "proxmox_password").one()
        ep = db.query(NotificationEndpoint).one()
        for value in (row.value, ep.url, ep.secret):
            try:
                legacy.decrypt(value.encode())
            except InvalidToken:
                continue
            raise AssertionError("a moved value is still readable with the JWT-derived key")
        assert cs._decrypt(row.value) == "pve-root"
        assert cs.decrypt_value(ep.url) == "https://hook/x"
        plain = db.query(AppConfig).filter(AppConfig.key == "plain_legacy").one()
        assert plain.value == "not-encrypted", "a plain-text row is not touched"
    db.close()


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
