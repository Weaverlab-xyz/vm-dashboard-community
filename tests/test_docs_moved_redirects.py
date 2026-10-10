"""A doc that moved still answers at its old URL, with a 301 to where it lives now.

Moving a page repoints every link in the repo -- the moves into scheduling/,
integrations/beyondtrust/ and ot-demo-cell/ touched 72 files -- but not a bookmark, a PRA
jump-item note or a link pasted into a ticket. Before ``docs_pages._MOVED`` those were a
bare 404, which a reader cannot tell apart from "this was deleted".

The map is a literal, so it can rot in two directions, and both are pinned here:

  * **A target that does not exist** redirects the reader to a 404, which is worse than
    the 404 it replaced because it looks deliberate.
  * **A source that exists again** is dead code at best. doc_page consults the map only
    after both real lookups miss, so the entry can never shadow the page -- but it means
    somebody moved the page back, or reused the name, and the entry now describes a move
    that did not happen.

Skips cleanly when fastapi isn't installed, like the other route tests here. The two
map-shape checks need only the module, and run whenever it imports.

Runs under pytest, or standalone:  python tests/test_docs_moved_redirects.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-docs-moved")

_DOCS = os.path.join(_ROOT, "docs")

try:
    from fastapi.testclient import TestClient
    from web_dashboard.main import app
    from web_dashboard.services import config_service
    from web_dashboard.api.docs_pages import _MOVED, _moved_to
except Exception as exc:  # fastapi / deps absent outside CI
    TestClient = None
    _IMPORT_ERR = exc


def _skip():
    if TestClient is None:
        print(f"  (skipped: {_IMPORT_ERR})")
        return True
    return False


def _resolves(route):
    """What doc_page would serve without the map: <route>.md, or a folder's README."""
    base = os.path.join(_DOCS, *route.split("/"))
    return os.path.isfile(base + ".md") or os.path.isfile(os.path.join(base, "README.md"))


def _client():
    c = TestClient(app)
    c.__enter__()
    config_service.set("setup_complete", "1")
    config_service._setup_complete = True
    return c


def test_every_target_exists():
    if _skip():
        return
    dead = sorted(f"{k} -> {v}" for k, v in _MOVED.items() if not _resolves(v))
    assert not dead, "these redirects land on a page that does not exist:\n  " + "\n  ".join(dead)


def test_no_source_exists_again():
    if _skip():
        return
    back = sorted(k for k in _MOVED
                  if _resolves(k) or os.path.isdir(os.path.join(_DOCS, *k.split("/"))))
    assert not back, ("these moved paths are live pages or folders again, so the entry "
                      "describes a move that no longer holds:\n  " + "\n  ".join(back))


def test_folder_keys_carry_the_rest_of_the_path():
    if _skip():
        return
    # A folder key carries the rest of the path: anything under operations' old
    # scheduling/ follows it, with no entry of its own.
    assert (_moved_to("scheduling/change-windows")
            == "operations/scheduling/change-windows")
    # Moved twice: each old address points straight at where the page lives now, never
    # at an intermediate one that is itself a redirect.
    assert (_moved_to("integrations/databases/password-safe-gcp")
            == "integrations/beyondtrust/password-safe/databases-gcp")
    assert (_moved_to("integrations/beyondtrust/databases")
            == "integrations/beyondtrust/password-safe/databases")
    # A prefix only counts at a path boundary: entitle must not swallow
    # entitle-dashboard-permissions, which has its own entry.
    assert (_moved_to("integrations/entitle-dashboard-permissions")
            == "integrations/beyondtrust/entitle-dashboard-permissions")
    assert _moved_to("integrations/entitlement") is None


def test_an_old_url_redirects():
    if _skip():
        return
    c = _client()
    for old, new in (("/docs/change-windows", "/docs/operations/scheduling/change-windows"),
                     ("/docs/integrations/password-safe",
                      "/docs/integrations/beyondtrust/password-safe"),
                     ("/docs/integrations/databases/password-safe-gcp",
                      "/docs/integrations/beyondtrust/password-safe/databases-gcp"),
                     ("/docs/integrations/beyondtrust/databases/password-safe",
                      "/docs/integrations/beyondtrust/password-safe/databases"),
                     ("/docs/integrations/spiffe", "/docs/workload-lab/spiffe")):
        r = c.get(old, follow_redirects=False)
        assert r.status_code == 301, f"{old}: {r.status_code}"
        assert r.headers["location"] == new, f"{old} -> {r.headers['location']}"
        assert c.get(new).status_code == 200, f"{new} does not render"


def test_live_pages_and_misses_are_unchanged():
    if _skip():
        return
    c = _client()
    for live in ("/docs/integrations/beyondtrust", "/docs/operations/scheduling",
                 "/docs/integrations/beyondtrust/password-safe/databases"):
        assert c.get(live, follow_redirects=False).status_code == 200, live
    assert c.get("/docs/no-such-page", follow_redirects=False).status_code == 404


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
