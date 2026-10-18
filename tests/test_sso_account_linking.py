"""Single sign-on links an IdP identity to an account by its subject, never by email alone.

Before: an SSO login whose subject was unknown was matched to an existing account by email
-- any account, a local administrator included -- and the email came from a claim that most
IdPs let the user set (``email``, else ``preferred_username``/``upn``), with no
``email_verified`` check. Whoever could put an address on an IdP profile signed in as that
account. Now an email match links only when:

  * the IdP verified the address (OIDC ``email_verified`` on the ``email`` claim; Entra
    when signing in through one specific tenant);
  * the account is not already linked to a different subject;
  * the account is not a local password account -- unless an administrator turned on
    "Sign in with SSO" for it on the Users tab (``auth_provider = "sso_pending"``).

Runs standalone:
    python tests/test_sso_account_linking.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="sso-linking-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "sso-linking-test-secret")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.database import (AuditLog, Base, OAuthGroupMapping,  # noqa: E402
                                    SessionLocal, User, engine)
from web_dashboard.services import oidc_service  # noqa: E402

Base.metadata.create_all(bind=engine)


def _db():
    db = SessionLocal()
    if not db.query(OAuthGroupMapping).first():
        db.add(OAuthGroupMapping(entra_group_id="g-staff", display_name="staff",
                                 workgroup="default"))
        db.commit()
    return db


def _user(db, name, email, **cols):
    u = User(id=str(uuid.uuid4()), username=name, email=email, hashed_password="x", **cols)
    db.add(u)
    db.commit()
    return u


def _login(db, *, subject, email, verified, provider="oidc"):
    return auth._complete_oauth_login(db, subject=subject, email=email, display_name=email,
                                      groups=["g-staff"], provider=provider,
                                      email_verified=verified)


def _refused(resp):
    return resp.status_code == 302 and "error=sso_link_refused" in resp.headers["location"]


def test_a_local_account_is_never_taken_over_by_email():
    db = _db()
    admin = _user(db, "boss", "boss@example.com", is_admin=True)
    for verified in (False, True):
        assert _refused(_login(db, subject="attacker-sub", email="boss@example.com",
                               verified=verified)), verified
    db.refresh(admin)
    assert admin.oauth_subject is None and admin.auth_provider == "local"


def test_an_admin_opt_in_lets_the_next_verified_login_link():
    db = _db()
    u = _user(db, "pat", "pat@example.com", auth_provider=auth.SSO_PENDING)
    assert _refused(_login(db, subject="pat-sub", email="pat@example.com", verified=False)), (
        "an unverified email must not link even an opted-in account")
    resp = _login(db, subject="pat-sub", email="pat@example.com", verified=True)
    assert resp.status_code == 302 and "error=" not in resp.headers["location"], resp.headers
    db.refresh(u)
    assert u.oauth_subject == "pat-sub" and u.auth_provider == "oidc"
    assert db.query(AuditLog).filter(AuditLog.action == "sso_account_linked").count()


def test_once_linked_the_subject_is_what_signs_in():
    db = _db()
    _user(db, "lee", "lee@example.com", auth_provider="oidc", oauth_subject="lee-sub")
    ok = _login(db, subject="lee-sub", email="anything@else.example", verified=False)
    assert "error=" not in ok.headers["location"], "a known subject needs no email at all"
    assert _refused(_login(db, subject="other-sub", email="lee@example.com", verified=True)), (
        "a second identity with the same email took over a linked account")


def test_a_login_without_a_subject_is_refused():
    db = _db()
    assert _refused(_login(db, subject="", email="nobody@example.com", verified=True))


def test_a_new_identity_still_gets_a_new_account():
    db = _db()
    resp = _login(db, subject="new-sub", email="new@example.com", verified=False)
    assert "error=" not in resp.headers["location"], resp.headers
    assert db.query(User).filter(User.oauth_subject == "new-sub").one().email == "new@example.com"


def test_oidc_verification_needs_the_email_claim_itself():
    yes = {"email": "a@x.example", "email_verified": True}
    assert oidc_service.email_is_verified(yes, "a@x.example")
    for claims, email in (({"email": "a@x.example"}, "a@x.example"),
                          ({"email": "a@x.example", "email_verified": "true"}, "a@x.example"),
                          ({"preferred_username": "a@x.example", "email_verified": True},
                           "a@x.example")):
        assert not oidc_service.email_is_verified(claims, email), claims


def test_both_callbacks_pass_the_verification_signal():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "auth.py"), encoding="utf-8").read()
    assert "email_verified=oidc_service.email_is_verified(claims, email)" in src
    assert "email_verified=not _multi" in src


# ── the Users tab switch ──────────────────────────────────────────────────────

def test_the_switch_marks_a_local_account_and_is_refused_where_it_does_not_apply():
    from web_dashboard.api import users
    db = _db()
    admin = _user(db, "root2", "root2@example.com", is_admin=True)
    u = _user(db, "sam", "sam@example.com")
    users._set_sso_sign_in(db, admin, u, True)
    assert u.auth_provider == auth.SSO_PENDING
    users._set_sso_sign_in(db, admin, u, False)
    assert u.auth_provider == "local"
    for target, code in ((admin, 400),
                         (_user(db, "robot", "r@example.com", is_service_account=True), 400)):
        try:
            users._set_sso_sign_in(db, admin, target, True)
        except HTTPException as exc:
            assert exc.status_code == code
        else:
            raise AssertionError(f"switched {target.username}")
    linked = _user(db, "kim", "kim@example.com", auth_provider="oidc", oauth_subject="kim-sub")
    try:
        users._set_sso_sign_in(db, admin, linked, False)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("turned a linked SSO account back into a local one")


def test_password_login_refuses_an_opted_in_account():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "auth.py"), encoding="utf-8").read()
    assert 'if user and user.auth_provider != "local":' in src, (
        "password login no longer refuses non-local accounts, so the SSO switch would leave "
        "the password working")


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
