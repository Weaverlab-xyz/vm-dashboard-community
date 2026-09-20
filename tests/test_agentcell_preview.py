"""The agent cell is a preview feature, and every reader of that fact must agree.

Smaller than tests/test_netcell_preview.py because this cell has no tab and no tile —
its surfaces are a router, a persona card set and a page. The failure mode is the same
though, and it is partial adoption: the router 404s while a card still reads `ready`,
which looks like a broken feature rather than a flag doing its job.

Also pins the one thing specific to this cell: **its preview flag is its own, not
`mcp_server_enabled`.** The MCP server is a shipped feature an operator may legitimately
want on by itself; the cell is the unproven thing. Conflating them would mean turning on
the agent cell as a side effect of turning on an AI client integration.

Runs under pytest, or standalone:
    python tests/test_agentcell_preview.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-preview")

_FLAG = "agentcell_enabled"


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# -- declared, and off ---------------------------------------------------------

def test_the_flag_is_declared_and_defaults_off():
    from web_dashboard.config import settings
    assert hasattr(settings, _FLAG), f"config has no {_FLAG}"
    assert getattr(settings, _FLAG) is False, \
        "a preview feature that ships on is not a preview feature"


def test_feature_flags_resolves_it():
    from web_dashboard.services import feature_flags
    assert _FLAG in feature_flags.flags(), \
        f"{_FLAG} is not in feature_flags.flags(), so no reader can see it"


def test_it_is_listed_as_a_preview_feature():
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    assert _FLAG in _PREVIEW_FLAGS, \
        f"{_FLAG} is not in _PREVIEW_FLAGS, so Settings renders no toggle for it"
    label, desc = _PREVIEW_FLAGS[_FLAG]
    assert label and "Preview" in desc


def test_the_description_names_what_is_unproven_and_the_admin_refusal():
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    _, desc = _PREVIEW_FLAGS[_FLAG]
    assert "SPIRE" in desc, "the description never mentions the trust domain it needs"
    assert "administrator" in desc, (
        "the description never mentions that an administrator is refused — the one "
        "behaviour an operator most needs to know before minting an agent")


# -- the readers ---------------------------------------------------------------

def test_the_router_is_gated_on_its_own_flag():
    src = _read("web_dashboard", "main.py")
    m = re.search(r"agentcell_api\.router,\s*\n?\s*dependencies=\[_feature_gate\(\"(\w+)\"\)\]",
                  src)
    assert m, "the agentcell router is not mounted behind a _feature_gate"
    assert m.group(1) == _FLAG, (
        f"the agentcell router is gated on {m.group(1)!r}. It must be its own preview "
        "flag: the MCP server is a shipped feature somebody may want on by itself, and "
        "the cell is the unproven thing")


def test_the_flagship_card_requires_it():
    from web_dashboard.services import personas as P
    card = next((c for p in P.all_personas() for c in p.use_cases
                 if c.id == "aiops-revoke-mid-task"), None)
    assert card, "the aiops revoke card is gone"
    assert _FLAG in card.requires_flags, (
        "the card pointing at the agent cell does not require its preview flag, so it "
        "would read as ready on an instance where the router 404s")


def test_the_flag_has_a_human_label():
    from web_dashboard.services.personas import _FLAG_LABELS
    assert _FLAG in _FLAG_LABELS, f"_FLAG_LABELS has no entry for {_FLAG}"


# -- the docs say so -----------------------------------------------------------

def test_the_page_carries_a_preview_blockquote():
    doc = _read("docs", "profiles", "demo", "agent-demo-cell.md")
    assert "> **Preview.**" in doc, "agent-demo-cell.md has no preview blockquote"
    assert "preview\n> toggle in Settings" in doc or "preview toggle in Settings" in doc, \
        "the preview blockquote never says how to turn the feature on"


def test_the_page_states_the_svid_to_pat_gap():
    """The honest claim this whole feature rests on. If it disappears, the page starts
    implying an identity-to-authorization bridge that does not exist."""
    doc = _read("docs", "profiles", "demo", "agent-demo-cell.md")
    assert "does not authenticate" in doc, \
        "the page no longer states that the SVID does not authenticate to /mcp"
    assert "SPIFFE SVID" in doc and "unresolved" in doc, \
        "the page no longer names what would have to be answered to close the gap"


def test_the_index_row_marks_it_preview():
    idx = _read("docs", "profiles", "demo", "README.md")
    row = [ln for ln in idx.splitlines() if "agent-demo-cell.md" in ln]
    assert row, "the demo profile index no longer links the agent cell"
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
