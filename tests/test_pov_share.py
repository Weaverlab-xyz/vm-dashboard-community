"""The POV's customer-facing share link — the rules that keep a URL from outliving anyone.

Slice 7. This is the only slice whose output is opened by somebody OUTSIDE the account, so
what is pinned here is mostly what stops that URL being more open, or living longer, than
whoever created it intended:

  * **Always password-protected.** The password is generated, not asked for, so there is
    no field to leave blank — and the adapter refuses a blank one even if a future caller
    tries, because Skytap itself will happily publish an anonymous URL.
  * **Always time-limited.** There is no "never expires" path. An explicit request wins,
    then the POV's own auto-delete date, then the default — so a link can never outlive
    the environment it points at.
  * **Re-sharing revokes first.** Two live URLs and one stored id means the older one can
    never be revoked from here again.
  * **Revoke clears the row only AFTER the platform call succeeds**, because a cleared row
    with a live URL is a share nobody can find, let alone kill.
  * **The password never rides the list.** `describe` is called for every row on the page;
    revealing is its own endpoint, one row at a time, and audited.
  * **Destroy revokes first and never raises**, so a POV cannot be stranded over cleanup
    of something the environment delete removes anyway.

Uses a real SQLite database and a fake platform adapter. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_share.py
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-share")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (config_service, lab_platforms,  # noqa: E402
                                    pov_env_service, pov_share as sh)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _env(db, **kw):
    kw.setdefault("platform_environment_id", "sky-1")
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           status=pov_env_service.STATUS_ACTIVE, **kw)
    db.add(env)
    db.commit()
    return env


class _FakePlatform:
    """A stand-in adapter. Records what it was asked for so the assertions can be about
    the REQUEST rather than about our own bookkeeping."""

    def __init__(self, *, fail_create=False, fail_delete=False):
        self.created = []
        self.deleted = []
        self.fail_create = fail_create
        self.fail_delete = fail_delete

    async def create_share(self, env_id, password="", expires_at="", *, name=""):
        self.created.append({"env_id": env_id, "password": password,
                             "expires_at": expires_at, "name": name})
        if self.fail_create:
            raise RuntimeError("platform said no")
        n = len(self.created)
        return {"url": f"https://cloud.example/desktops/{n}", "id": f"ps-{n}",
                "expires_at": expires_at}

    async def delete_share(self, env_id, share_id):
        self.deleted.append({"env_id": env_id, "share_id": share_id})
        if self.fail_delete:
            raise RuntimeError("platform said no")


def _install(fake):
    """Swap the adapter resolver. Returns the original so the test can put it back —
    `lab_platforms.adapter` is imported by name in half the POV services, so patching the
    module attribute is what actually reaches them."""
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda _p: fake
    return original


def _restore(original):
    lab_platforms.adapter = original


# ── the password ─────────────────────────────────────────────────────────────

def test_the_password_is_generated_and_never_blank():
    """Skytap treats the password as optional. This does not: a blank field is how an
    anonymous URL into a lab full of PAM components gets published by accident."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        result = asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    sent = fake.created[0]["password"]
    assert sent and len(sent) >= 16, "a short or blank password reached the platform"
    assert result["password"] == sent
    db.close()


def test_the_password_alphabet_survives_being_read_aloud():
    """These get delivered down a phone line as often as by email."""
    for _ in range(200):
        pw = sh._generate_password()
        assert not (set(pw) & set("Il1O0")), f"ambiguous character in {pw!r}"


def test_the_password_is_stored_encrypted_and_readable_afterwards():
    """A link whose password cannot be re-read a day later is a link that gets recreated
    with a weaker one."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        result = asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    assert sh.reveal_password(env) == result["password"]
    # …and it is not sitting in the table in the clear.
    row = db.query(d.AppConfig).filter(
        d.AppConfig.key == sh.password_config_key(env.id)).first()
    assert row is not None and result["password"] not in (row.value or "")
    db.close()


def test_the_password_never_rides_the_row_serialization():
    """`describe` is called for every environment on the page. A secret that ships with a
    list is a secret in every browser cache and every screenshot of that page."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        result = asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    described = sh.describe(db, env)
    assert result["password"] not in str(described)
    assert described["has_share_password"] is True
    db.close()


# ── the expiry ───────────────────────────────────────────────────────────────

def test_there_is_no_never_expires_path():
    """The failure mode of a POV share is that it outlives everyone's attention."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    assert fake.created[0]["expires_at"], "the platform was asked for an endless link"
    assert env.share_expires_at is not None
    db.close()


def test_the_povs_own_expiry_wins_over_the_default():
    """A live URL to an environment that was already reaped is a support call that starts
    with 'the link is broken' and ends nowhere near the truth."""
    db = d.SessionLocal()
    reap = datetime.utcnow() + timedelta(days=3)
    env = _env(db, expires_at=reap)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    assert env.share_expires_at == reap
    db.close()


def test_a_past_expiry_on_the_pov_falls_back_to_the_default():
    """An already-reaped date would otherwise publish a link that is dead on arrival."""
    db = d.SessionLocal()
    env = _env(db, expires_at=datetime.utcnow() - timedelta(days=1))
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    assert env.share_expires_at > datetime.utcnow()
    db.close()


def test_an_explicit_request_wins_over_both():
    db = d.SessionLocal()
    env = _env(db, expires_at=datetime.utcnow() + timedelta(days=30))
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env, days=2))
    finally:
        _restore(original)
    delta = env.share_expires_at - datetime.utcnow()
    assert timedelta(days=1, hours=23) < delta < timedelta(days=2, minutes=1)
    db.close()


def test_an_out_of_range_request_is_refused_not_clamped():
    """Silently shortening someone's link is the kind of help that gets discovered by a
    customer at the wrong moment."""
    db = d.SessionLocal()
    env = _env(db)
    for bad in (0, -1, sh.MAX_DAYS + 1):
        try:
            asyncio.run(sh.create(db, env, days=bad))
            raise AssertionError(f"{bad} days was accepted")
        except sh.ShareError as exc:
            assert str(sh.MAX_DAYS) in str(exc)
    db.close()


def test_an_expired_link_is_reported_rather_than_cleared():
    """Skytap enforces the expiry server-side, so the link is already dead. Clearing the
    row would delete the evidence that a link was ever shared."""
    db = d.SessionLocal()
    env = _env(db, share_url="https://cloud.example/d/1", share_id="ps-1",
               share_expires_at=datetime.utcnow() - timedelta(hours=1))
    assert sh.is_expired(env) is True
    described = sh.describe(db, env)
    assert described["share_expired"] is True and described["share_url"]
    db.close()


def test_a_row_with_no_link_is_not_expired():
    db = d.SessionLocal()
    env = _env(db)
    assert sh.is_expired(env) is False
    assert sh.describe(db, env)["share_expired"] is False
    db.close()


# ── re-sharing ───────────────────────────────────────────────────────────────

def test_resharing_revokes_the_previous_link_first():
    """Two live URLs and one stored id means the older one can never be revoked from
    here again — the orphan shape the tenant and gateway registries both learned to
    avoid."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
        first = env.share_id
        asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    assert fake.deleted == [{"env_id": "sky-1", "share_id": first}]
    assert env.share_id != first
    db.close()


def test_a_failed_revoke_does_not_block_resharing():
    """A POV whose old publish set was deleted in the platform's own UI could otherwise
    never be re-shared from here."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
        fake.fail_delete = True
        asyncio.run(sh.create(db, env))
    finally:
        _restore(original)
    assert env.share_url.endswith("/2")
    db.close()


def test_the_password_changes_on_every_share():
    """Re-sharing is what an SE does when a link went to the wrong person. Reusing the
    password would make that gesture meaningless."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        a = asyncio.run(sh.create(db, env))["password"]
        b = asyncio.run(sh.create(db, env))["password"]
    finally:
        _restore(original)
    assert a != b and sh.reveal_password(env) == b
    db.close()


# ── revoke ───────────────────────────────────────────────────────────────────

def test_revoke_clears_the_row_and_the_password():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
        asyncio.run(sh.revoke(db, env))
    finally:
        _restore(original)
    assert not env.share_url and not env.share_id and env.share_expires_at is None
    assert sh.reveal_password(env) == ""
    db.close()


def test_a_failed_revoke_leaves_the_row_intact():
    """A cleared row with a live URL is a share nobody can find, let alone kill. So the
    platform call comes first and its failure is NOT swallowed."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
        fake.fail_delete = True
        try:
            asyncio.run(sh.revoke(db, env))
            raise AssertionError("a failed revoke reported success")
        except RuntimeError:
            pass
    finally:
        _restore(original)
    assert env.share_id, "the row was cleared while the link was still live"
    assert sh.reveal_password(env), "the password was cleared while the link was live"
    db.close()


def test_revoking_nothing_is_a_no_op():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.revoke(db, env))
    finally:
        _restore(original)
    assert fake.deleted == []
    db.close()


def test_a_share_id_with_no_environment_id_says_where_to_go():
    """Nothing here can revoke it, so the refusal has to name the place that can."""
    db = d.SessionLocal()
    env = _env(db, platform_environment_id=None, share_id="ps-9",
               share_url="https://cloud.example/d/9")
    try:
        asyncio.run(sh.revoke(db, env))
        raise AssertionError("a link with no environment id was silently 'revoked'")
    except sh.ShareError as exc:
        assert "by hand" in str(exc) or "own UI" in str(exc)
    db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_never_raises_and_says_what_was_left():
    """A destroy that stops here would strand a whole environment over cleanup of
    something the environment delete removes anyway."""
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
        fake.fail_delete = True
        note = asyncio.run(sh.teardown(db, env))
    finally:
        _restore(original)
    assert "could not be revoked" in note
    assert not env.share_id, "a destroyed POV was left advertising a link"
    assert sh.reveal_password(env) == ""
    db.close()


def test_teardown_is_quiet_when_it_works():
    db = d.SessionLocal()
    env = _env(db)
    fake = _FakePlatform()
    original = _install(fake)
    try:
        asyncio.run(sh.create(db, env))
        note = asyncio.run(sh.teardown(db, env))
    finally:
        _restore(original)
    assert note == ""
    db.close()


def test_teardown_with_no_link_still_clears_a_stray_password():
    """The password is written before the row. A create that died between the two leaves
    exactly this state, and nothing else would ever clean it up."""
    db = d.SessionLocal()
    env = _env(db)
    config_service.set(sh.password_config_key(env.id), "orphan")
    note = asyncio.run(sh.teardown(db, env))
    assert note == "" and sh.reveal_password(env) == ""
    db.close()


def test_the_destroy_path_revokes_the_share_before_anything_else():
    """The share link is the only artifact somebody outside the account can be holding,
    so the window where it still works is the one worth making shortest."""
    import inspect
    src = inspect.getsource(pov_env_service.run_env_destroy)
    order = [src.index(f"pov_{n}.teardown") for n in ("share", "wireup", "gateway")]
    assert order == sorted(order), "the share is no longer revoked first"


# ── capability gating ────────────────────────────────────────────────────────

def test_a_platform_without_share_links_is_refused_with_the_alternative():
    """'PRA only' in the UI, not a 500 from inside a job — the rule lab_platforms.supports
    was written for."""
    db = d.SessionLocal()
    env = _env(db)
    original_caps = dict(lab_platforms.CAPABILITIES["skytap"])
    lab_platforms.CAPABILITIES["skytap"]["share_link"] = False
    try:
        assert sh.describe(db, env)["shareable"] is False
        try:
            asyncio.run(sh.create(db, env))
            raise AssertionError("a platform with no share links published one")
        except sh.ShareError as exc:
            assert "PRA" in str(exc), "the refusal does not name the alternative"
    finally:
        lab_platforms.CAPABILITIES["skytap"] = original_caps
    db.close()


def test_an_unprovisioned_pov_is_refused():
    db = d.SessionLocal()
    env = _env(db, platform_environment_id=None)
    try:
        asyncio.run(sh.create(db, env))
        raise AssertionError("a POV with no platform environment was shared")
    except sh.ShareError as exc:
        assert "provision" in str(exc)
    db.close()


# ── the adapter's own refusal ────────────────────────────────────────────────

def test_the_adapter_refuses_a_blank_password_even_if_asked():
    """The service generates one, so this can only be reached by a future caller. It is
    the last place to stop an anonymous URL and it does not defer to the platform."""
    from web_dashboard.services import skytap_service
    try:
        asyncio.run(skytap_service.create_share("sky-1", ""))
        raise AssertionError("the adapter published an unauthenticated link")
    except skytap_service.SkytapError as exc:
        assert "password" in str(exc)


def test_the_adapter_prefers_the_desktops_url_over_the_api_self_reference():
    """`url` is an api.skytap.com address that answers 401 to a browser — handing it to a
    customer looks exactly like a broken link."""
    from web_dashboard.services import skytap_service
    out = skytap_service._share({"id": "ps-1", "url": "https://api.skytap.com/x",
                                 "desktops_url": "https://cloud.skytap.com/d/1"})
    assert out["url"] == "https://cloud.skytap.com/d/1"
    assert skytap_service._share({"id": "ps-2", "url": "https://api.skytap.com/x"})["url"] == ""


def test_the_write_contract_names_both_halves():
    """A create with no delete is how a link becomes unrevokable."""
    assert "create_share" in lab_platforms.WRITE_CONTRACT
    assert "delete_share" in lab_platforms.WRITE_CONTRACT
    from web_dashboard.services import skytap_service
    for fn in ("create_share", "delete_share"):
        assert callable(getattr(skytap_service, fn, None)), f"skytap has no {fn}"


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
