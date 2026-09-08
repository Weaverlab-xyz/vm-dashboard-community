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


# ── Gross vs net ──────────────────────────────────────────────────────────────

def test_the_card_shows_gross_beside_the_net_headline():
    """The headline is net. Without gross beside it, a credit expiring reads as growth —
    the false lead that consumed the GCP audit in cloud-cost-guardrails.md."""
    src = _src(_COSTS)
    assert "c.gross" in src and "c.credits" in src, (
        "the per-cloud card shows only net; a credit cliff is invisible on it")
    assert "summary.gross_mtd" in src, "the account total should split too"


def test_the_split_is_hidden_where_a_cloud_cannot_measure_it():
    """`gross: null` means "this API cannot separate credits" — Azure and OCI. Rendering
    that as a gross of zero would be a confident wrong answer, so the row must be gated on
    a non-null gross rather than on truthiness alone."""
    src = _src(_COSTS)
    assert "c.gross != null" in src, (
        "gate the split on gross being present, not on it being non-zero — 0.0 is a real "
        "measurement and null is not")
    assert "summary.gross_mtd != null" in src


# ── The unattributed breakdown ────────────────────────────────────────────────

def _unattributed_block(src, strip_comments=False):
    """The markup for the unattributed disclosure, from its comment to the notes list.

    ``strip_comments`` drops ``<!-- -->`` blocks, for the same reason ``_load_body``
    drops ``//`` lines: the markup EXPLAINS that it deliberately has no reclaim control,
    so a plain substring scan for "reclaim" finds the explanation and reports the thing
    it is documenting the absence of.
    """
    start = src.index("Spend carrying no managed-by tag")
    block = src[start:src.index('x-show="(c.notes || []).length"', start)]
    if strip_comments:
        block = re.sub(r"<!--.*?-->", "", block, flags=re.DOTALL)
        # The opening marker itself is inside the first comment, which the slice above
        # started partway through, so drop anything before the first real tag.
        block = block[block.index("<"):] if "<" in block else ""
    return block


def test_the_unattributed_row_lists_what_is_in_it():
    """The audit item: the number was already on the page, the list behind it was not."""
    block = _unattributed_block(_src(_COSTS))
    assert "scopeRows(c, 'unattributed')" in block, (
        "the unattributed row shows a total with nothing behind it")
    assert "s.service" in block and "s.amount" in block, (
        "the expanded rows should render service and amount, like the scope tables above")


def test_the_unattributed_list_offers_no_reclaim_action():
    """Read-only BY DESIGN, and the one thing the audit is emphatic about.

    A reclaim button here reverses the reapers' stated guard — they refuse to touch what
    they did not create — and by the cost-guardrails note's own finding, two of the three
    waste shapes are things a human built by hand. Deletion in this list is a per-resource
    proof obligation, which is exactly what a generic control cannot encode.
    """
    block = _unattributed_block(_src(_COSTS), strip_comments=True).lower()
    banned = ("reclaim", "destroy", "delete", "terminate", "release", "deallocate")
    found = [w for w in banned if w in block]
    assert not found, (
        f"the unattributed list grew an action verb {found} — this list is read-only; "
        "see docs/notes/feature-audit-2026-09.md and cloud-cost-guardrails.md")
    assert "confirm" not in block, "a confirmation prompt implies an action to confirm"


def test_a_cloud_that_cannot_list_the_remainder_gets_no_dead_control():
    """OCI derives its number by subtraction and has no rows behind it. Rendering a
    chevron there would be a control that does nothing when clicked."""
    block = _unattributed_block(_src(_COSTS))
    assert "scopeRows(c, 'unattributed').length &&" in block, (
        "the toggle should be gated on there being rows to show")
    assert 'x-show="scopeRows(c, \'unattributed\').length"' in block, (
        "the expander chevron should only render when the list is non-empty")


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
