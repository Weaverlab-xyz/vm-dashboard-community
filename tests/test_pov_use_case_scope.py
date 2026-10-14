"""Choosing a POV's use cases: Password Safe only, PRA only, Total PASM, and so on.

``services/pov_use_case_scope`` is the one place the POV checklist SUBTRACTS. The tenant
mix still never does (tests/test_pov_use_cases pins that); a choice the SE made does. The
properties pinned here are the ones whose absence would be invisible:

  * **No choice means the whole catalog.** Every POV from before this landed has NULL in
    the column, and its checklist must not change.
  * **A combined card needs every product it names.** The PRA + Password Safe cards
    appear on a Total PASM POV and never on a Password-Safe-only one.
  * **Hidden means hidden.** An unticked card is gone from the payload and from the
    totals, and a write to it is refused -- unless the picker asks for ``all``, which
    flags it ``in_pov: false`` and still does not count it.
  * **Progress outlives a scope change.** Taking a card out and putting it back brings its
    tick back.

Runs under pytest, or standalone:
    python tests/test_pov_use_case_scope.py
"""
import os
import re
import sys
import tempfile
import types
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-scope-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-scope")

_API = os.path.join(_ROOT, "web_dashboard", "api", "pov.py")
_ACCESSOR_API = os.path.join(_ROOT, "web_dashboard", "api", "pov_accessor.py")
_DETAIL = os.path.join(_ROOT, "web_dashboard", "templates", "pov", "detail.html")
_LIST = os.path.join(_ROOT, "web_dashboard", "templates", "pov", "index.html")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")

# Probe the third-party deps by name; the first-party imports below are unguarded, so a
# broken one fails loudly rather than skipping (tests/test_import_guard_narrowness).
try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.database import (PovBlueprint, PovEnvironment,  # noqa: E402
                                    SessionLocal, init_db)
from web_dashboard.services import (pov_blueprint_service, pov_cards,  # noqa: E402
                                    pov_runbooks, pov_use_case_scope as S, pov_use_cases)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


_db = None


def _session():
    global _db
    if _db is None:
        init_db()
        _db = SessionLocal()
    return _db


def _env(products=None, excluded=()):
    """A POV with all three tenants, so a card's presence is decided by the scope alone."""
    db = _session()
    env = PovEnvironment(id=str(uuid.uuid4()), platform="skytap",
                         name=f"pov-{uuid.uuid4().hex[:8]}", status="active",
                         created_by="se@example", pra_tenant_id="t-pra",
                         ps_tenant_id="t-ps", entitle_tenant_id="t-ent")
    S.write(env, *S.validate(products, excluded))
    db.add(env)
    db.commit()
    return env


def _ids(payload):
    return [c["id"] for g in payload["groups"] for c in g["use_cases"]]


def _every_card():
    return ([c for g in pov_cards.GROUPS for c in pov_cards.cards(g)]
            + [c for r in pov_runbooks.all_runbooks() for c in r.use_cases])


def _combined():
    return [c for c in _every_card()
            if {"pra", "password_safe"} <= set(c.requires_products)]


# ── validation ───────────────────────────────────────────────────────────────

def test_validate_normalises_the_order_and_refuses_nonsense():
    assert S.validate(["password_safe", "pra"]) == (("pra", "password_safe"), ())
    assert S.validate(None) == (None, ())
    for bad in (["epm"], [], ["", " "]):
        try:
            S.validate(bad)
        except S.ScopeError:
            continue
        raise AssertionError(f"{bad!r} was accepted")
    try:
        S.validate(["pra"], ["no-such-card"])
    except S.ScopeError as exc:
        assert "no-such-card" in str(exc)
    else:
        raise AssertionError("an unknown card id was accepted")


def test_the_presets_cover_the_pov_types_an_se_names():
    keys = {p["key"]: set(p["products"]) for p in S.PRESETS}
    assert keys["password_safe"] == {"password_safe"}
    assert keys["pra"] == {"pra"}
    assert keys["pasm"] == {"pra", "password_safe"}, "Total PASM is PRA + Password Safe"
    assert S.preset_of(("password_safe", "pra")) == "pasm"
    assert "Total PASM" in S.label_of(("pra", "password_safe"))
    assert S.preset_of(("password_safe", "entitle")) == S.CUSTOM
    assert S.label_of(("password_safe", "entitle")) == "Password Safe + Entitle"
    assert S.preset_of(None) == ""


# ── the checklist ────────────────────────────────────────────────────────────

def test_no_choice_is_the_whole_catalog():
    env = _env()
    payload = pov_use_cases.describe(_session(), env)
    assert sorted(_ids(payload)) == sorted(c.id for c in _every_card())
    assert payload["scope"]["products"] is None and payload["scope"]["preset"] == ""


def test_a_password_safe_pov_tests_only_password_safe():
    payload = pov_use_cases.describe(_session(), _env(["password_safe"]))
    groups = {g["group"] for g in payload["groups"]}
    assert "pra" not in groups and "entitle" not in groups, groups
    assert "password_safe" in groups
    for g in payload["groups"]:
        for c in g["use_cases"]:
            assert set(c["products"]) <= {"password_safe"}, (g["group"], c["id"])
    for card in _combined():
        assert card.id not in _ids(payload), f"{card.id} needs PRA too"
    assert payload["summary"]["total"] == len(_ids(payload))


def test_total_pasm_brings_in_the_cards_that_need_both():
    assert _combined(), "the catalog has no PRA + Password Safe card to test with"
    payload = pov_use_cases.describe(_session(), _env(["pra", "password_safe"]))
    ids = _ids(payload)
    for card in _combined():
        assert card.id in ids, card.id
    assert "entitle" not in {g["group"] for g in payload["groups"]}
    assert payload["scope"]["preset"] == "pasm"


def test_an_unticked_card_is_hidden_and_not_counted():
    full = pov_use_cases.describe(_session(), _env(["password_safe"]))
    card_id = next(c["id"] for g in full["groups"] if g["group"] == "password_safe"
                   for c in g["use_cases"])
    env = _env(["password_safe"], [card_id])
    payload = pov_use_cases.describe(_session(), env)
    assert card_id not in _ids(payload)
    assert payload["summary"]["total"] == full["summary"]["total"] - 1
    picker = pov_use_cases.describe(_session(), env, include_excluded=True)
    flagged = {c["id"]: c["in_pov"] for g in picker["groups"] for c in g["use_cases"]}
    assert flagged[card_id] is False
    assert picker["summary"] == payload["summary"], "the picker must not change the counts"


def test_a_hidden_card_cannot_be_ticked_and_its_progress_comes_back():
    db = _session()
    env = _env(["pra", "password_safe"])
    card = _combined()[0]
    pov_use_cases.set_state(db, env, card.id, state="done", note="shown", by="se")
    # Down to Password Safe only: the combined card is hidden and refuses writes.
    S.write(env, *S.validate(["password_safe"]))
    db.commit()
    assert card.id not in _ids(pov_use_cases.describe(db, env))
    try:
        pov_use_cases.set_state(db, env, card.id, state="done", by="se")
    except pov_use_cases.UseCaseError as exc:
        assert "Setup tab" in str(exc)
    else:
        raise AssertionError("a hidden card was ticked")
    # Back to Total PASM: the tick and the note were never deleted.
    S.write(env, *S.validate(["pra", "password_safe"]))
    db.commit()
    back = {c["id"]: c for g in pov_use_cases.describe(db, env)["groups"]
            for c in g["use_cases"]}
    assert back[card.id]["progress"]["state"] == "done"
    assert back[card.id]["progress"]["note"] == "shown"


# ── blueprints ───────────────────────────────────────────────────────────────

def test_a_blueprint_fills_the_scope_only_when_the_request_chose_none():
    bp = PovBlueprint(name="ps-only")
    S.write(bp, ("password_safe",), ())
    out = pov_blueprint_service.apply_to(bp, types.SimpleNamespace(use_case_products=None))
    assert out["use_case_products"] == ["password_safe"]
    out = pov_blueprint_service.apply_to(bp, types.SimpleNamespace(use_case_products=["pra"]))
    assert "use_case_products" not in out, "the request wins"
    assert pov_blueprint_service.serialize(bp)["use_case_scope"]["preset"] == "password_safe"


# ── the API, through the real router ─────────────────────────────────────────

class _Operator:
    """An admin SE, shaped like tests/test_pov_api's: the routes are gated on `pov`."""
    username = "tester"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}
    accessor_env_id = None
    pov_env_ids_list = []


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard import database as d
    from web_dashboard.api import pov as pov_api
    from web_dashboard.api.auth import get_current_user

    def _db():
        db = d.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(pov_api.router)
    app.dependency_overrides[get_current_user] = lambda: _Operator()
    app.dependency_overrides[d.get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def test_the_scope_route_saves_refuses_and_404s():
    client = _client()
    env = _env()
    url = f"/api/pov/managed/{env.id}/use-case-scope"
    res = client.put(url, json={"products": ["password_safe"], "excluded": []})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"]["preset"] == "password_safe"
    assert any(c["in_pov"] is False for g in body["groups"] for c in g["use_cases"]), \
        "the save answers with the picker's view, hidden cards included"
    for bad in ({"products": ["epm"]}, {"products": []},
                {"products": ["pra"], "excluded": ["no-such-card"]}):
        res = client.put(url, json=bad)
        assert res.status_code == 400, (bad, res.status_code, res.text)
    res = client.put(f"/api/pov/managed/{uuid.uuid4()}/use-case-scope",
                     json={"products": ["pra"]})
    assert res.status_code == 404, res.text
    # The ordinary read hides what the picker's read flags.
    plain = client.get(f"/api/pov/managed/{env.id}/use-cases").json()
    every = client.get(f"/api/pov/managed/{env.id}/use-cases?all=1").json()
    assert len(_ids(every)) > len(_ids(plain))
    assert all(c["in_pov"] for g in plain["groups"] for c in g["use_cases"])


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_scope_route_is_owner_gated_and_404s_like_the_tenants_route():
    src = _read(_API)
    m = re.search(r'@router\.put\("/managed/\{env_id\}/use-case-scope", '
                  r'dependencies=_POV_WRITE_OWN\)\s*\ndef set_use_case_scope', src)
    assert m, "PUT /managed/{env_id}/use-case-scope is missing or not owner-gated"
    body = src[m.end():].split("\n@router", 1)[0]
    assert "status_code=404" in body and "status_code=400" in body


def test_the_customer_page_never_asks_for_the_hidden_cards():
    src = _read(_ACCESSOR_API) + _read(os.path.join(
        _ROOT, "web_dashboard", "services", "pov_accessor_service.py"))
    assert "include_excluded" not in src


def test_both_pages_read_the_one_preset_list():
    assert 'templates.env.globals["pov_use_case_presets"]' in _read(_MAIN)
    for path in (_DETAIL, _LIST):
        assert "{{ pov_use_case_presets | tojson }}" in _read(path), path
    detail = _read(_DETAIL)
    assert "use-cases?all=1" in detail and "use-case-scope" in detail


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
