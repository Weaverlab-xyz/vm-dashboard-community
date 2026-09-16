"""The RBAC page's workgroup pickers must FETCH their list, not receive an injection.

The Users and Groups tabs both offer a workgroup picker: a checkbox grid on Users, a
dropdown on the identity-provider group mappings. Both used to read
``{{ workgroups | tojson }}``, injected by the route as
``list(settings.workgroups.keys())``.

That was wrong twice over.

**It was empty.** ``settings.workgroups`` is a bootstrap seed — ``workgroup_service.
seed_if_empty`` reads it once, on first boot, and from then on the ``workgroups`` table
is authoritative. ``config.py`` declares it ``{}`` and nothing writes to it at runtime,
so on any install that seeded into the database (which is every install, since
``default`` is always seeded) both pickers rendered with no options. Quietly circular:
``/workgroups`` listed a full table, you could create a workgroup there, and then had no
way to put a user in one. Nothing errored — the API accepted workgroups fine, only the
two Jinja-injected pickers were starved.

**And injecting it at all was the wrong shape.** Every HTML route in this app is an
unauthenticated shell, so anything rendered into the template is readable by an anonymous
GET. ``_rbac_context`` spells out the distinction it draws: the permission *catalog* is a
static property of the build and safe to inject; the roles an operator has defined are
their configuration and are fetched from ``/api/roles`` with a token. A workgroup name is
configuration by that same test — so reading the table server-side would have fixed the
emptiness and kept the exposure.

Both tabs now fetch ``/api/groups/workgroups`` in ``init()``, beside the role picker.

Pure file reads plus one SQLite round trip.

Runs under pytest, or standalone:  python tests/test_workgroup_picker_injection.py
"""
import ast
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "wgpick.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

_TABS = ("web_dashboard/templates/rbac/_users.html",
         "web_dashboard/templates/rbac/_groups.html")
_ENDPOINT = "/api/groups/workgroups"
_CONTEXT_FN = "_rbac_context"


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _context_fn_node():
    for node in ast.parse(_read("web_dashboard/main.py")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _CONTEXT_FN:
            return node
    raise AssertionError(f"{_CONTEXT_FN} not found in web_dashboard/main.py")


def _context_keys():
    """The keys the function actually returns, from the AST rather than a text scan."""
    for sub in ast.walk(_context_fn_node()):
        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
            return {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError(f"{_CONTEXT_FN} no longer returns a dict literal")


def _context_code():
    """The function's code with every string literal blanked, so a fragment named in its
    docstring cannot be mistaken for code that uses it.

    That matters here specifically: the docstring now explains this very bug, so it
    mentions ``settings.workgroups`` in prose. A substring scan of the raw source would
    fail on the explanation of the fix.
    """
    node = _context_fn_node()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            sub.value = ""
    return ast.unparse(node)


def test_the_route_no_longer_injects_a_workgroup_list():
    """Covers BOTH halves: the seed dict is gone, and so is any server-side read of the
    table that would have replaced it."""
    keys = _context_keys()
    assert "workgroups" not in keys, (
        "the workgroup list is operator configuration and this template is anonymously "
        f"readable; the tabs must fetch it with a token. Context keys: {sorted(keys)}")
    # Must not pass by the context having been gutted.
    assert "permission_scopes" in keys and "initial_tab" in keys, sorted(keys)

    code = _context_code()
    for frag in ("settings.workgroups", "workgroup_service", "list_names"):
        assert frag not in code, (
            f"{frag} is read server-side again — that fixes the empty picker but keeps "
            f"the anonymous-read exposure")


def test_the_prose_stripper_actually_strips():
    """Guards the test above from passing vacuously. If ``_context_code`` returned the
    raw source, the docstring's own mention of settings.workgroups would fail the
    assertion; if it returned nothing, the assertion would pass for free."""
    code = _context_code()
    assert "permission_scopes" in code, "the stripper ate the code, not just the prose"
    assert "bootstrap seed" not in code, "the docstring survived the stripper"


def test_no_template_still_expects_the_injected_key():
    """A tab reading `{{ workgroups }}` against a route that no longer sends it renders
    an empty list with no error — the exact failure being fixed, in reverse."""
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(_ROOT, "web_dashboard", "templates")):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                markup = fh.read()
            if re.search(r"\{\{\s*workgroups\s*(\||\}\})", markup):
                offenders.append(os.path.relpath(path, _ROOT))
    assert not offenders, f"still reading an injected `workgroups`: {offenders}"


def test_both_tabs_fetch_the_list_in_init():
    for rel in _TABS:
        markup = _read(rel)
        assert "availableWorkgroups: []," in markup, (
            f"{rel}: must start empty and fill from the API")
        assert _ENDPOINT in markup, f"{rel}: never calls {_ENDPOINT}"
        # In init(), not on some later interaction: the Users tab renders a checkbox
        # matrix at first paint, and a list that arrives on first click is a grid that
        # looks broken.
        init = markup.split("async init()", 1)
        assert len(init) == 2, f"{rel}: no async init() to check"
        assert _ENDPOINT in init[1].split("},", 1)[0], (
            f"{rel}: fetches {_ENDPOINT} somewhere other than init()")


def test_the_fetch_fails_quietly():
    """An unreachable endpoint must leave the picker empty rather than break the tab —
    the same contract the role picker beside it already has."""
    for rel in _TABS:
        line = next(l for l in _read(rel).splitlines() if _ENDPOINT in l)
        assert "catch" in line, (
            f"{rel}: wrap it like the roles fetch: {line.strip()}")


def test_the_endpoint_is_readable_by_a_non_admin():
    """The tabs are admin-only, but the picker endpoint deliberately is not: the cloud,
    database and Kubernetes pages call the same one to offer a workgroup at create time.
    Tightening it to require_admin would empty every one of those selects."""
    src = _read("web_dashboard/api/groups.py")
    block = src.split('@router.get("/workgroups"', 1)
    assert len(block) == 2, "GET /api/groups/workgroups is gone — re-point this test"
    decorator = block[1].split("def ", 1)[0]
    assert "require_admin" not in decorator, (
        "making the picker endpoint admin-only would empty the workgroup select on "
        "every create form")


def test_the_live_source_is_actually_non_empty_on_a_stock_install():
    """The half a source assertion cannot cover: that what replaced the empty dict is
    itself populated. `seed_if_empty` always creates `default`."""
    try:
        import sqlalchemy  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - environmental
        print(f"   (skipped: {exc})")
        return
    from web_dashboard.database import Base, SessionLocal, engine
    from web_dashboard.services import workgroup_service as wgs

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        wgs.seed_if_empty(db)
        names = wgs.list_names(db)
        assert "default" in names, (
            f"seed_if_empty must always create `default`; got {names}")
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
