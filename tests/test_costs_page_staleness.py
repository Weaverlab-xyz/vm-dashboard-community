"""The costs page must distinguish a stale figure from a fresh one, and from no figure.

Three states now reach the template, where there used to be two:

  * ``status="ok"``, ``stale=false``  — a current figure
  * ``status="ok"``, ``stale=true``   — the last known figure; the cloud is throttled or
                                        unreachable, and ``note``/``as_of`` say so
  * ``status="unavailable"``          — this cloud has NEVER returned a figure

Rendering the middle one like the first is the failure this file guards: a rate-limited
Azure keeps its number on screen, and if the page does not say the number is old, nobody
can tell. Rendering it like the third is the bug being fixed — that is what the old code
did, and why one 429 blanked the tile for six hours.

Also pins the sequential fetch. ``Promise.all`` opened two connections, so ``gunicorn -w 2``
could accept them in different workers — two simultaneous Cost Management queries against
the one subscription Cost Management rate-limits on.

Pure text scan, no app import — same approach as tests/test_gateway_dashboard_tile.py.
Runs under pytest, or standalone:  python tests/test_costs_page_staleness.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_COSTS = os.path.join(_ROOT, "web_dashboard", "templates", "costs", "index.html")
_DASH = os.path.join(_ROOT, "web_dashboard", "templates", "dashboard.html")


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load_body(src):
    """The CODE of the page's load(), comments stripped.

    Two traps, both hit while writing this file. `budgetBadge(` also appears up in the
    markup, so the end marker has to be searched for after the start rather than from the
    top. And the code explains why it no longer uses Promise.all, which a plain substring
    scan reads as still using it — so drop the `//` lines before matching."""
    start = src.index("async load(")
    block = src[start:src.index("budgetBadge(", start)]
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in block.splitlines())


def test_the_two_cost_endpoints_are_not_fetched_concurrently():
    """One Refresh click must not become two simultaneous same-subscription queries."""
    block = _load_body(_src(_COSTS))
    assert "Promise.all" not in block, (
        "the costs page fetches summary and breakdown concurrently again — that is two "
        "Cost Management queries against one Azure subscription at once")
    assert block.count("await API.get('/api/costs/") == 2, (
        "both cost endpoints should still be fetched, just in sequence")


def test_the_page_renders_the_stale_state():
    src = _src(_COSTS)
    for token in ("c.stale", "as_of", "c.note", "asOf(", "staleClouds("):
        assert token in src, f"the costs page never references {token}"
    # The banner is what makes a stale figure legible without hunting per-cloud cards.
    assert 'x-show="staleClouds().length"' in src, "no page-level stale banner"


def test_unavailable_detail_is_still_gated_on_status():
    """`detail` now means "never had a figure", so it must not leak onto a cloud that is
    merely stale — that would put a 429 string next to a perfectly good number."""
    src = _src(_COSTS)
    found = 0
    for m in re.finditer(r'x-text="c\.detail"', src):
        found += 1
        # The guard sits either on the element itself or on the container it lives in
        # (the breakdown card wraps the whole unavailable block in one x-show), so look
        # back a short way rather than at the single tag.
        context = src[max(0, m.start() - 400):m.end()]
        assert "c.status !== 'ok'" in context, (
            f"c.detail rendered without a status guard near: "
            f"{src[max(0, m.start() - 120):m.end()].strip()[-160:]}")
    assert found >= 2, "the detail string is no longer rendered anywhere"


def test_stale_figures_are_visually_distinct():
    """Amber, not the normal text colour — a stale number that looks fresh is worse than
    no number, because nothing prompts anyone to check."""
    src = _src(_COSTS)
    assert "amber" in src
    assert re.search(r"c\.stale\s*\?\s*'text-amber", src), (
        "stale figures render in the same colour as fresh ones")


def test_the_dashboard_tile_surfaces_staleness():
    """The tile shows one cross-cloud total. If one cloud is serving a last-known-good
    figure, the total is not current and the tile has to say so.

    The tile moved server-side: the dashboard now reads every tile from
    /api/dashboard/stats instead of fetching /api/costs/summary itself, so the as-of and
    stale markers are built in api/dashboard.py::_cost_tile and rendered from the response.
    This used to slice `_fetchCost` out of dashboard.html; that fetcher is gone.
    """
    api = _src(os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py"))
    block = api[api.index("async def _cost_tile("):api.index("# ── the endpoint")]
    assert "oldest_as_of" in block and 'payload.get("stale")' in block, (
        "_cost_tile builds a possibly-stale total with no as-of or stale marker — the tile "
        "would read as current while one cloud serves a last-known-good figure")

    # And the page has to render it. `snapshotStale` is what marks the label.
    dash = _src(_DASH)
    assert "snapshotStale" in dash and "snapshotAsOf" in dash, (
        "the dashboard no longer renders the snapshot's staleness, so a stale total looks "
        "fresh")


def test_the_refresh_button_reports_a_declined_requery():
    """A forced refresh can be declined server-side (min interval, or a throttle cooldown).
    Silently doing nothing makes the button look broken and invites more clicking — which
    is the loop that produced the 429s in the first place."""
    src = _src(_COSTS)
    assert "asOfSignature(" in src
    assert "refreshed recently" in src.lower()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
