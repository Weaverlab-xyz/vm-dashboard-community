"""What a POV leaves behind, and whether anybody can reach it.

The record was never the problem. ``run_env_destroy`` marks the row ``destroyed`` rather
than deleting it because it is "the inventory record of something that existed", and
``pov_use_cases.destroy_note`` deliberately keeps the checklist as that record's contents.
What was missing is that ``api/pov.list_managed`` filters destroyed rows out, so a finished
evaluation appeared nowhere and needed its raw uuid to see again — evidence that survived
teardown with no way to it, which is the same as losing it at the moment somebody wants it.

So the properties pinned here are about **reachability and honesty**:

  * **The archive route must be declared before ``/managed/{env_id}``**, or "archive" is
    captured as an environment id and the endpoint answers "No such POV environment". The
    third time this repo has met that trap and the third time it is pinned.
  * **The archive is a light projection, not ``_serialize``.** That builds five describes
    per row, each asking a question about a *living* environment — meaningless and a wasted
    query for a POV that no longer exists.
  * **``/summary`` serves a destroyed POV.** It is the endpoint written for after a POV is
    over; a status filter would leave it describing only the ones whose story is not
    finished.
  * **"They took part" means they ticked something.** A login issued and a login used are
    two different facts, and only the second is evidence. Conflating them would put a claim
    in a renewal conversation that the data does not support.
  * **Nothing here writes or reaches the network.** A summary that made platform calls
    would fail for exactly the POVs it exists to describe.

Runs under pytest, or standalone:
    python tests/test_pov_summary.py
"""
import ast
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-summary-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-summary")

_SVC = os.path.join(_ROOT, "web_dashboard", "services")
_API = os.path.join(_ROOT, "web_dashboard", "api", "pov.py")
_SERVICE = os.path.join(_SVC, "pov_summary.py")
_DETAIL = os.path.join(_ROOT, "web_dashboard", "templates", "pov", "detail.html")
_LIST = os.path.join(_ROOT, "web_dashboard", "templates", "pov", "index.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path, name=""):
    """Source with docstrings and comments dropped — these files argue for themselves at
    length, and an assertion about behaviour must not match the argument."""
    tree = ast.parse(_read(path))
    if name:
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name), None)
        assert node is not None, f"{os.path.basename(path)} has no {name}()"
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        return "\n".join(ast.unparse(s) for s in body)
    for node in ast.walk(tree):
        b = getattr(node, "body", None)
        if isinstance(b, list) and b and isinstance(b[0], ast.Expr) \
                and isinstance(b[0].value, ast.Constant) \
                and isinstance(b[0].value.value, str):
            node.body = b[1:]
    return ast.unparse(tree)


def _markup(path):
    src = _read(path)
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


try:
    from web_dashboard.database import (PovAccessor, PovEnvironment, PovEnvironmentVM,
                                        SessionLocal, init_db)
    from web_dashboard.services import pov_summary, pov_use_cases
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


_state = {}


def _fixture():
    """One finished POV with a real evaluation on it, and one still running."""
    if _state:
        return _state
    init_db()
    db = SessionLocal()
    done_id, live_id = str(uuid.uuid4()), str(uuid.uuid4())
    db.add(PovEnvironment(id=done_id, platform="skytap", name="acme-pov",
                          status="destroyed", created_by="se@example",
                          created_at=datetime.utcnow() - timedelta(days=30),
                          ps_tenant_id="t-ps", pra_tenant_id="t-pra"))
    db.add(PovEnvironment(id=live_id, platform="skytap", name="live-pov",
                          status="active", created_by="se@example",
                          ps_tenant_id="t-ps"))
    db.add(PovEnvironmentVM(environment_id=done_id, platform_vm_id="vm-1",
                            name="linux-1", os_family="linux", private_ip="10.0.0.5",
                            pra_jump_id="j1", ps_managed_system_id="s1"))
    db.commit()

    env = db.query(PovEnvironment).filter(PovEnvironment.id == done_id).first()
    pov_use_cases.set_state(db, env, "pov-security-who-has-access", state="done",
                            by="se@example")
    pov_use_cases.set_state(db, env, "pov-hypervisor-rotate-root", state="done",
                            by="povguest_acme", by_kind=pov_use_cases.KIND_ACCESSOR,
                            note="this is the one that sold it")
    pov_use_cases.set_state(db, env, "pov-security-teardown-proof", state="skipped",
                            by="se@example", note="ran out of time")
    pov_use_cases.set_state(db, env, "pov-cloudops-reap", state="",
                            by="povguest_acme", by_kind=pov_use_cases.KIND_ACCESSOR,
                            note="could not get to this")
    db.add(PovAccessor(environment_id=done_id, username="povguest_acme_aaa",
                       user_id=None, source="manual", revoked_at=datetime.utcnow()))
    db.commit()
    db.close()
    _state.update(done=done_id, live=live_id)
    return _state


# ── reachability ─────────────────────────────────────────────────────────────

def test_the_archive_route_is_declared_before_the_parameterised_one():
    """The third time this repo meets this trap. Below it, "archive" is captured as an
    environment id and the endpoint answers "No such POV environment" — wrong, and silent."""
    src = _read(_API)
    assert src.index('@router.get("/managed/archive")') < \
        src.index('@router.get("/managed/{env_id}")'), \
        "/managed/archive is declared after /managed/{env_id} and would be shadowed"


def test_the_archive_lists_only_finished_povs_newest_first():
    s = _fixture()
    db = SessionLocal()
    out = pov_summary.archive(db)
    db.close()
    ids = [e["id"] for e in out["environments"]]
    assert s["done"] in ids, "a destroyed POV is missing from the archive"
    assert s["live"] not in ids, "the archive lists a POV that is still running"
    dates = [e["created_at"] for e in out["environments"] if e["created_at"]]
    assert dates == sorted(dates, reverse=True), "the archive is not newest first"


def test_the_archive_says_when_it_is_truncated():
    """A list silently cut is one an SE trusts and should not."""
    s = _fixture()
    db = SessionLocal()
    assert pov_summary.archive(db, limit=1)["truncated"] is True
    assert pov_summary.archive(db)["truncated"] is False
    db.close()
    code = _code(_SERVICE, "archive")
    assert "MAX_LIMIT" in code, "the limit is unbounded"


def test_the_archive_is_a_light_projection_not_the_serializer():
    """_serialize builds five describes per row, each asking a question about a LIVING
    environment. Every one is meaningless and a wasted query for a POV that is gone."""
    code = _code(_SERVICE, "archive")
    for heavy in ("_serialize", "pov_gateway", "pov_share", "pov_resource_broker",
                  "pov_wireup", "summary_for"):
        assert heavy not in code, f"the archive calls {heavy} per row"


def test_the_summary_serves_a_finished_pov():
    """It is the endpoint written for after a POV is over."""
    route = _code(_API, "get_summary")
    # `status_code=404` is not a status filter, so name the filter rather than the word.
    for filtering in ("STATUS_DESTROYED", "PovEnvironment.status", ".filter("):
        assert filtering not in route, \
            f"the summary endpoint filters the POV out with {filtering}"
    s = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["done"]).first()
    built = pov_summary.build(db, env)
    db.close()
    assert built["environment"]["finished"] is True
    assert built["environment"]["name"] == "acme-pov"


# ── honesty ──────────────────────────────────────────────────────────────────

def test_the_summary_holds_only_what_somebody_touched():
    """A summary listing every untouched card would be the catalog again, and the catalog
    is not what happened."""
    s = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["done"]).first()
    built = pov_summary.build(db, env)
    db.close()
    assert {e["id"] for e in built["covered"]} == {
        "pov-security-who-has-access", "pov-hypervisor-rotate-root"}
    assert [e["id"] for e in built["skipped"]] == ["pov-security-teardown-proof"]
    # The note-only card is in `notes` and in neither verdict list — a comment is not a
    # verdict, which is the rule the accessor page writes by.
    note_ids = {e["id"] for e in built["notes"]}
    assert "pov-cloudops-reap" in note_ids
    assert not any(e["id"] == "pov-cloudops-reap"
                   for e in built["covered"] + built["skipped"])


def test_taking_part_means_they_ticked_something_not_that_they_were_given_a_login():
    """Two different facts, and only the second is evidence. Conflating them puts a claim
    in a renewal conversation that the data does not support."""
    s = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["done"]).first()
    built = pov_summary.build(db, env)
    assert built["customer"]["took_part"] is True
    assert built["customer"]["accessors_issued"] == 1
    assert built["customer"]["notes_from_customer"] == 2

    # A POV that issued a login nobody used claims nothing.
    other = PovEnvironment(id=str(uuid.uuid4()), platform="skytap", name="quiet-pov",
                           status="destroyed")
    db.add(other)
    db.commit()
    db.add(PovAccessor(environment_id=other.id, username="povguest_quiet_x",
                       source="manual"))
    db.commit()
    quiet = pov_summary.build(db, other)
    assert quiet["customer"]["accessors_issued"] == 1
    assert quiet["customer"]["took_part"] is False, \
        "a POV claims the customer took part because a login existed"
    db.close()


def test_the_archive_row_separates_a_login_issued_from_a_login_used():
    s = _fixture()
    db = SessionLocal()
    rows = {e["name"]: e for e in pov_summary.archive(db)["environments"]}
    db.close()
    acme = rows["acme-pov"]
    assert acme["accessors_issued"] == 1 and acme["by_customer"] == 2
    quiet = rows.get("quiet-pov")
    if quiet:
        assert quiet["accessors_issued"] == 1 and quiet["by_customer"] == 0

    page = _markup(_LIST)
    assert "ticked by them" in page and "unused" in page, \
        "the archive table collapses 'issued' and 'used' into one claim"


def test_a_role_with_nothing_in_scope_is_dropped_from_the_breakdown():
    """A Password-Safe-only evaluation has several, and "0 of 0" against each is noise in a
    document somebody reads once."""
    s = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["done"]).first()
    by_persona = pov_summary.build(db, env)["by_persona"]
    db.close()
    assert by_persona, "the breakdown is empty for a POV with two products"
    assert all(p["in_scope"] > 0 for p in by_persona), \
        "a role with nothing in scope is reported"
    assert all(p["done"] <= p["in_scope"] for p in by_persona)


def test_the_products_are_read_off_the_row_not_probed():
    """The only thing that could work for a destroyed POV, and the honest question for a
    live one: what the evaluation COVERED was decided when its tenants were chosen."""
    code = _code(_SERVICE, "_products")
    assert "pra_tenant_id" in code and "ps_tenant_id" in code
    for live in ("feature_flags", "adapter", "await"):
        assert live not in code, f"_products consults {live}"


def test_the_summary_never_writes_and_never_reaches_the_network():
    """It would fail for exactly the POVs it exists to describe."""
    code = _code(_SERVICE)
    for forbidden in ("db.commit", "db.add", "httpx", "requests.", "adapter(",
                      "lab_platforms"):
        assert forbidden not in code, f"pov_summary uses {forbidden}"


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_summary_tab_exists_and_the_archive_links_straight_to_it():
    detail = _markup(_DETAIL)
    block = detail.split("tabs: [", 1)[1].split("],", 1)[0]
    assert "'summary'" in block, "there is no Summary tab"
    assert "'/pov/' + a.id + '#summary'" in _markup(_LIST), \
        "the archive does not link a past POV to its own summary"


def test_the_customers_words_render_as_text():
    """Prose typed by somebody outside the account, shown back on an operator's page."""
    detail = _markup(_DETAIL)
    assert 'x-text="n.note"' in detail, "the summary does not show the notes"
    assert "x-html" not in detail


def test_the_takeaway_is_copied_rather_than_downloaded():
    """What an SE does with this is paste it into a renewal note; a file on disk is one
    more step to the same place."""
    src = _read(_DETAIL)
    assert "summaryMarkdown()" in src and "copySummary()" in src
    assert "download" not in src.split("summaryMarkdown()", 1)[1][:2000], \
        "the summary is offered as a download"
    markdown = src.split("summaryMarkdown() {", 1)[1].split("\n      },", 1)[0]
    assert "took_part" in markdown, "the export drops whether the customer took part"
    assert "r.note" in markdown, "the export drops what they said"


# ── the runbook axis reaches the same consumers ──────────────────────────────
#
# `pov_runbooks` is a SECOND card registry appended by `pov_use_cases._groups`. It emits
# the persona group shape on purpose, so the detail page's group loop, `_summarize` and
# `by_persona` below take it without knowing which registry a group came from. These pin
# that it actually arrives -- a registry nothing appends is a file with tests.


def _runbook_card():
    from web_dashboard.services import pov_runbooks
    return pov_runbooks.get("ps-poc-skytap").use_cases[0].id


def test_the_runbook_group_reaches_the_catalog():
    from web_dashboard.services import personas, pov_runbooks
    st = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == st["live"]).first()
    groups = pov_use_cases.describe(db, env)["groups"]
    keys = [g["persona"] for g in groups]
    assert keys == list(personas.VALID_PERSONAS) + list(pov_runbooks.VALID_RUNBOOKS),         f"the catalog drops, reorders or duplicates a group: {keys}"
    db.close()


def test_the_runbook_cards_are_counted_in_the_summary_total():
    """`_summarize` counts only in-scope cards. The live POV names a Password Safe tenant,
    so the runbook's cards are in scope and its denominator has to include them."""
    from web_dashboard.services import pov_runbooks
    st = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == st["live"]).first()
    total = pov_use_cases.describe(db, env)["summary"]["total"]
    runbook_cards = sum(len(r.use_cases) for r in pov_runbooks.all_runbooks())
    assert total >= runbook_cards,         f"summary total {total} is below the {runbook_cards} runbook cards alone"
    db.close()


def test_a_runbook_card_can_be_ticked_and_records_its_runbook():
    """`set_state` consults both registries. The `persona` column stores whichever group
    the card came from, which is what makes a runbook tick renderable later."""
    st = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == st["live"]).first()
    card = _runbook_card()
    row = pov_use_cases.set_state(db, env, card, state="done", by="se@example",
                                  note="shown in session 1")
    assert row["state"] == "done" and row["note"] == "shown in session 1"

    found = next(c for g in pov_use_cases.describe(db, env)["groups"]
                 for c in g["use_cases"] if c["id"] == card)
    assert found["progress"]["state"] == "done"

    from web_dashboard.database import PovUseCaseProgress
    stored = (db.query(PovUseCaseProgress)
                .filter(PovUseCaseProgress.environment_id == env.id,
                        PovUseCaseProgress.card_id == card).first())
    assert stored.persona == "ps-poc-skytap",         f"the tick was filed under {stored.persona!r}, not its runbook"

    assert pov_use_cases.clear(db, env, card) is True
    db.close()


def test_a_card_id_in_neither_registry_is_still_refused():
    """The two registries together are the allowlist. Widening `set_state` to consult a
    second one must not turn it into a free-text store."""
    st = _fixture()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == st["live"]).first()
    try:
        pov_use_cases.set_state(db, env, "pspoc-not-a-real-card", state="done")
        raise AssertionError("an unknown card id was accepted")
    except pov_use_cases.UseCaseError as exc:
        assert "no POV use case" in str(exc)
    finally:
        db.close()


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
