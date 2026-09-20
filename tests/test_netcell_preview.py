"""The network cell is a preview feature, and every reader of that fact must agree.

A preview flag is only worth having if it is off by default and if *everything* that
surfaces the feature consults it. The failure mode is partial adoption: the router 404s
while the tab still renders, or the tile reports a confident zero for a feature whose
page cannot be reached. Both look like bugs in the feature rather than like a flag doing
its job.

So this file walks the readers one at a time:

  * **config** declares it, defaulting to off — a preview that ships on is not a preview;
  * **feature_flags** resolves it, so the template context processor carries it to every
    page (a flag Jinja cannot see fails closed and silently, which is bug #664's shape);
  * **setup._PREVIEW_FLAGS** lists it, which is what puts the toggle in Settings at all;
  * **main** gates the router on it;
  * **the GCP template** gates the tab button and the panel on it;
  * **the dashboard tile** names it, and its collector reports unavailable rather than a
    zero when it is off;
  * **every persona card** that deep-links into the tab requires it.

Runs under pytest, or standalone:
    python tests/test_netcell_preview.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-netcell-preview")

_FLAG = "netcell_enabled"
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# -- it is declared, and it is off ---------------------------------------------

def test_the_flag_is_declared_and_defaults_off():
    from web_dashboard.config import settings
    assert hasattr(settings, _FLAG), f"config has no {_FLAG}"
    assert getattr(settings, _FLAG) is False, \
        "a preview feature that ships on is not a preview feature"


def test_feature_flags_resolves_it():
    """Without this the template context processor never carries it, and every
    `{% if netcell_enabled %}` fails closed with no error — bug #664's shape."""
    from web_dashboard.services import feature_flags
    assert _FLAG in feature_flags.flags(), \
        f"{_FLAG} is not in feature_flags.flags(), so no template can see it"


def test_it_is_listed_as_a_preview_feature():
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    assert _FLAG in _PREVIEW_FLAGS, \
        f"{_FLAG} is not in _PREVIEW_FLAGS, so Settings renders no toggle for it"
    label, desc = _PREVIEW_FLAGS[_FLAG]
    assert label, "the preview entry has no label"
    assert "Preview" in desc, "the preview entry's description does not say it is a preview"


def test_the_preview_description_says_what_is_unproven():
    """A preview toggle with no stated reason is a toggle nobody can decide about."""
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    _, desc = _PREVIEW_FLAGS[_FLAG]
    assert "vyos-cell" in desc.lower() or "bake" in desc.lower(), \
        "the description never mentions the image the feature depends on"


# -- every reader consults it --------------------------------------------------

def test_the_router_is_gated_on_it():
    src = _read("web_dashboard", "main.py")
    m = re.search(r"netcell_api\.router,\s*dependencies=\[_feature_gate\(\"(\w+)\"\)\]", src)
    assert m, "the netcell router is not mounted behind a _feature_gate"
    assert m.group(1) == _FLAG, \
        f"the netcell router is gated on {m.group(1)!r}, not {_FLAG!r}"


def test_the_tab_and_the_panel_are_gated_on_it():
    src = _read("web_dashboard", "templates", "gcp", "index.html")
    assert src.count("{% if netcell_enabled %}") >= 2, (
        "the Network Cell tab button and its panel are not both gated on "
        f"{_FLAG} — with the preview off, one of them still renders against a router "
        "that 404s")


def test_the_tile_names_the_flag():
    src = _read("web_dashboard", "templates", "dashboard.html")
    row = [ln for ln in src.splitlines() if "'net_cells'" in ln]
    assert row, "the net_cells tile is gone"
    assert "flag: 'netcell'" in row[0], (
        "the net_cells tile is not gated on the netcell flag, so it renders on an "
        "instance where the preview is off")


def test_the_collector_reports_unavailable_when_the_preview_is_off():
    """A zero says 'no cells yet' and invites the operator to go make one, on a page
    whose tab is not rendered and whose router 404s."""
    src = _read("web_dashboard", "api", "dashboard.py")
    body = src.split("def _net_cells():", 1)[1].split("_safe(\"net_cells\"", 1)[0]
    assert _FLAG in body, "the net_cells collector never consults the preview flag"
    assert "_unavailable" in body, \
        "the collector returns a count rather than 'unavailable' when the preview is off"


def test_every_card_into_the_tab_requires_the_flag():
    from web_dashboard.services import personas as P
    missing = []
    for p in P.all_personas():
        for c in p.use_cases:
            if c.target.split("#")[-1] != "net" or "#" not in c.target:
                continue
            if _FLAG not in c.requires_flags:
                missing.append(f"{p.key}/{c.id}")
    assert not missing, (
        f"cards deep-linking into the netcell tab without requiring {_FLAG}: {missing}. "
        "They would render as ready on an instance where the tab does not exist.")


def test_the_flag_has_a_human_label_for_the_cards():
    """Every flag a card names needs a _FLAG_LABELS entry, or the 'Needs: …' copy shows
    the raw flag name."""
    from web_dashboard.services.personas import _FLAG_LABELS
    assert _FLAG in _FLAG_LABELS, f"_FLAG_LABELS has no entry for {_FLAG}"


# -- the docs say so -----------------------------------------------------------

def test_the_page_carries_a_preview_blockquote():
    doc = _read("docs", "profiles", "demo", "net-demo-cell.md")
    assert "> **Preview.**" in doc, (
        "net-demo-cell.md has no preview blockquote — the convention "
        "docs/virtual-desktops.md and docs/workload-lab.md both follow")
    assert "preview toggle in Settings" in doc, \
        "the preview blockquote never says how to turn the feature on"


def test_the_index_row_marks_it_preview():
    idx = _read("docs", "profiles", "demo", "README.md")
    row = [ln for ln in idx.splitlines() if "net-demo-cell.md" in ln]
    assert row, "the demo profile index no longer links the network cell"
    assert "Preview" in row[0], "the index row does not mark the feature as preview"


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
