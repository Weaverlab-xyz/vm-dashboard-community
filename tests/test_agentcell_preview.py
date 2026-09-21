"""The agent cell is a preview feature, and every reader of that fact must agree.

Five surfaces now: a router, a persona card set, a documentation page, the Workload Lab's
`Agent` tab and the home page's `Agent Cells` tile. The failure mode is partial adoption —
the router 404s while a card still reads `ready`, or a tile reports an honest zero for a
feature nobody can reach — which looks like a broken feature rather than a flag doing its
job.

**And one failure specific to this flag, which shipped: a toggle that reaches nothing.**
The tab lives on `/workload-lab`, whose route is gated on the DERIVED
`workload_lab_enabled`. Turning the cell on without either lab therefore left a router
serving behind a page that still 404'd, and the only surface was Swagger. So the flag is
a constituent of that derived one, and the tests below pin both halves of that: the tab
renders behind the flag, and the flag can reach the page.

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


def _schema():
    """Create the tables the DB-backed assertion below needs.

    ``feature_flags.flags()`` reads config through the database, and CI runs each test
    file as its own process against a checkout with no database at all. This file sorts
    early enough -- ``test_agentcell_*`` lands just after ``test_agent_*`` -- that it can
    be the first thing to touch config, at which point ``app_config`` does not exist yet
    and the read raises ``no such table``. It passed locally only because a populated
    database was already sitting in the working copy.

    Per-test rather than at module level, and for the reason
    ``test_workload_lab_governance._schema`` gives: almost every assertion here reads
    SOURCE and needs no app import, so importing the database at module scope would make
    the whole file unrunnable wherever the app's dependencies are missing -- and the
    tests that would then be skipped are the ones this file exists for.
    """
    import web_dashboard.database as d

    d.Base.metadata.create_all(bind=d.engine)
    return d


# -- declared, and off ---------------------------------------------------------

def test_the_flag_is_declared_and_defaults_off():
    from web_dashboard.config import settings
    assert hasattr(settings, _FLAG), f"config has no {_FLAG}"
    assert getattr(settings, _FLAG) is False, \
        "a preview feature that ships on is not a preview feature"


def test_feature_flags_resolves_it():
    _schema()
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


def test_the_tab_renders_behind_the_same_flag():
    """The surface. Both halves — the pill and the panel — or the tab bar grows an entry
    that shows nothing, which is worse than no tab at all."""
    shell = _read("web_dashboard", "templates", "workload_lab", "index.html")
    assert "{% if agentcell_enabled %}" in shell, \
        "the Workload Lab does not gate anything on the agent cell's flag"
    assert "{'slug': 'agent', 'label': 'Agent'}" in shell, "no Agent pill in the tab bar"
    assert 'workload_lab/_agent.html' in shell, "the Agent panel is never included"


def test_the_flag_can_actually_reach_the_page():
    """A preview toggle that turns a router on behind a page that still 404s is a switch
    an operator cannot act on, and Swagger is not a surface. This is the regression the
    tab was built to fix, so it is pinned rather than left to the template."""
    from web_dashboard.services.feature_flags import _DERIVED
    assert _FLAG in _DERIVED["workload_lab_enabled"], (
        f"{_FLAG} is not a constituent of workload_lab_enabled, so turning the agent "
        f"cell on leaves /workload-lab 404ing and the tab unreachable")


def test_joining_the_derived_flag_keeps_the_page_all_preview():
    """The constraint that makes the line above safe. `workload_lab_enabled` counts as
    preview only while EVERY constituent is a preview toggle — the moment one is not,
    tests/test_permission_catalog.py demands an RBAC scope for the page."""
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    from web_dashboard.services.feature_flags import _DERIVED
    not_preview = [f for f in _DERIVED["workload_lab_enabled"]
                   if f not in _PREVIEW_FLAGS]
    assert not not_preview, (
        f"{not_preview} are in workload_lab_enabled but have no Settings preview "
        f"toggle — the page stops resolving as all-preview")


def test_the_tab_partial_is_self_contained():
    """Markup and factory in the one file, which is what test_template_scripts.py and
    test_templates_parse.py require of any template naming an x-data helper. A script tag
    inside an `x-if` template is cloned rather than executed, so moving the factory out
    leaves a dead x-data and a blank panel."""
    partial = _read("web_dashboard", "templates", "workload_lab", "_agent.html")
    assert 'x-data="workloadAgentTab()"' in partial
    assert "function workloadAgentTab()" in partial, \
        "the partial names an x-data helper it does not define"


def test_the_tab_is_a_consumer_of_the_other_tabs():
    """The reason it is a tab rather than a page. If the cross-links go, the page is five
    unrelated features sharing a URL — and the argument that the other four are governed
    rather than merely demonstrated loses the one thing that holds their credentials."""
    partial = _read("web_dashboard", "templates", "workload_lab", "_agent.html")
    for slug in ("spire", "cloud", "kubernetes"):
        assert f"$dispatch('select-tab', '{slug}')" in partial, (
            f"the Agent tab does not point at the {slug} tab — it consumes what that tab "
            f"builds, and an operator who cannot get there has to go looking")


def test_the_tab_never_renders_a_field_nothing_writes():
    """`stages_done`, `stage_job_ids` and `error_message` exist on `AgentCell` and nothing
    writes any of them: the two playbooks are runs the OPERATOR makes, and the dashboard
    neither starts nor watches them. A panel bound to a permanently empty field reads as
    "nothing happened yet" on a cell where plenty did — the trap the model's own comment
    about `episode_request_id` records.
    """
    api = _read("web_dashboard", "api", "agentcell.py")
    for never_written in ("row.stages_done =", "row.stage_job_ids =",
                          "row.error_message ="):
        assert never_written not in api, (
            f"{never_written.strip()} is now written — the Agent tab suppresses the "
            f"surfaces for these fields precisely because nothing does, so give them a "
            f"panel in templates/workload_lab/_agent.html")
    partial = _read("web_dashboard", "templates", "workload_lab", "_agent.html")
    assert 'x-text="row.error_message"' not in partial, \
        "the tab renders an error field nothing ever fills"
    # `wired` is derived from stages_done, so it is permanently false. Rendering it only
    # when TRUE is what keeps a running agent from wearing a permanent accusation.
    assert 'x-show="row.wired"' in partial and 'x-show="!row.wired"' not in partial, (
        "the tab renders a NOT-wired state — `wired` is derived from stages_done, which "
        "nothing writes, so every agent would carry it forever")


def test_the_home_tile_names_the_flag():
    src = _read("web_dashboard", "templates", "dashboard.html")
    row = [ln for ln in src.splitlines() if "'agent_cells'" in ln]
    assert row, "the agent_cells tile is gone"
    assert "flag: 'agentcell'" in row[0], (
        "the agent_cells tile is not gated on the agent cell's flag, so it renders on "
        "an instance where the preview is off")


def test_the_tile_collector_reports_unavailable_when_the_preview_is_off():
    """A zero says "no agents yet" and invites the operator to go mint one, on a tab
    that is not rendered and behind a router that 404s."""
    src = _read("web_dashboard", "api", "dashboard.py")
    body = src.split("def _agent_cells():", 1)[1].split('_safe("agent_cells"', 1)[0]
    assert _FLAG in body, "the agent_cells collector never consults the preview flag"
    assert "_unavailable" in body, \
        "the collector returns a count rather than 'unavailable' when the preview is off"


def test_the_tile_needs_no_cloud_pages_guard():
    """The OT and network tiles pair their flag with `anyFlag: ['cloud_pages']` because
    every href they can produce is a cloud console a POV instance 404s. This tile's is
    the Workload Lab, and `agentcell_enabled` gates that page through _DERIVED — so the
    link resolves wherever the tile renders, and a second condition here would be one
    more thing to keep in step with the flag graph.

    Pinned because the copy-paste is the tempting move, and it would hide the tile on a
    POV instance for a reason that does not apply to it.
    """
    src = _read("web_dashboard", "templates", "dashboard.html")
    i = src.index("key: 'agent_cells'")
    row = src[i:src.index("}", i)]
    assert "cloud_pages" not in row, (
        "the agent_cells tile carries a cloud_pages guard; its href is /workload-lab, "
        "which is not a cloud console")
    from web_dashboard.services.feature_flags import _DERIVED
    assert _FLAG in _DERIVED["workload_lab_enabled"], (
        "without this the tile's href is not guaranteed to resolve, and the "
        "cloud_pages-style guard it does not have would actually be needed")


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
