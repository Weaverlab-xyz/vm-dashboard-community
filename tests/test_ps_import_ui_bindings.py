"""Static checks on the Import-from-Password-Safe modal in databases/index.html.

In the spirit of test_pra_picker_bindings.py: the markup and the Alpine state object
live in one file with nothing type-checking the join between them, so a renamed key
is a silently dead binding. Pure text analysis — no browser, no server.

Runs under pytest, or standalone:  python tests/test_ps_import_ui_bindings.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "web_dashboard", "templates", "databases", "index.html")

with open(_PAGE, encoding="utf-8") as fh:
    HTML = fh.read()


def _state_block():
    """The literal returned by databasesPage(), up to the first method definition."""
    start = HTML.index("function databasesPage()")
    end = HTML.index("async init()", start)
    return HTML[start:end]


STATE = _state_block()


# imp + a capital: matches every state key and method (impLoading, impSelectable),
# and none of the prose in the modal copy ("Importing", "importable", "Imported").
_IMP = r"\bimp[A-Z][A-Za-z0-9_]*"


def _declared_keys():
    # Keys share lines ("impEnabled: true, impConfigured: true"), so this must not be
    # line-anchored.
    return set(re.findall(_IMP + r"(?=\s*:)", STATE))


def _referenced_keys():
    """Every impXxx the markup reads, excluding method calls."""
    markup = HTML[:HTML.index("{% block scripts %}")]
    names = set(re.findall(_IMP, markup))
    return {n for n in names if not re.search(rf"\b{n}\s*\(", markup)}


def _methods():
    # `async submitImport() {` as well as `impToggle(c) {`.
    return set(re.findall(r"^\s*(?:async\s+)?(" + _IMP.lstrip(r"\b") +
                          r"|submitImport|openImport|loadImportCandidates)\s*\(",
                          HTML, re.M))


# ── the state object and the markup agree ──────────────────────────────────────

def test_the_sweep_actually_sees_the_modal():
    """Guard against the checks below passing because they found nothing.

    Every assertion here is 'no mismatches', which a broken regex satisfies
    trivially. Same reason test_cache_key_scoping.py opens with its own sweep check.
    """
    assert len(_declared_keys()) >= 12, f"only found {sorted(_declared_keys())}"
    assert len(_referenced_keys()) >= 8, f"only found {sorted(_referenced_keys())}"
    assert {"impSelectable", "impToggleAll", "submitImport", "openImport"} <= _methods()


def test_every_state_key_the_markup_reads_is_declared():
    missing = sorted(_referenced_keys() - _declared_keys())
    assert not missing, f"markup reads undeclared state: {missing}"


def test_every_method_the_markup_calls_is_defined():
    markup = HTML[:HTML.index("{% block scripts %}")]
    called = set(re.findall(r"\b(imp[A-Z][A-Za-z0-9_]*|submitImport|openImport)\s*\(", markup))
    missing = sorted(called - _methods())
    assert not missing, f"markup calls undefined methods: {missing}"


def test_the_import_state_is_prefixed_and_adds_no_options_object():
    # tests/test_pra_picker_bindings.py matches every `options: {…}` literal in this
    # file and asserts it declares `gateways:`. A second object named `options` here
    # would break that, which is why everything is imXxx.
    assert len(re.findall(r"(?<![\w.])options\s*:\s*\{", HTML)) == 1
    assert _declared_keys(), "no imp* state was declared at all"


# ── eligibility stays the server's answer ──────────────────────────────────────

def test_the_row_checkbox_is_disabled_from_the_server_supplied_flag():
    assert ':disabled="!impSelectable(c)"' in HTML
    assert ':title="impDisabledReason(c)"' in HTML


def test_impSelectable_consults_the_servers_eligible_flag():
    """The assertion that matters.

    If someone reimplements eligibility client-side — say by checking
    `c.accounts.length` — the modal starts offering rows the import endpoint
    refuses, and the operator gets a per-item failure instead of a greyed row with
    a reason. impSelectable must read the server's `eligible` and nothing else.
    """
    body = re.search(r"impSelectable\(c\)\s*\{([^}]*)\}", HTML).group(1)
    assert "c.eligible" in body, "impSelectable must use the server's eligible flag"
    assert "already_imported" in body
    for invented in ("accounts.length", "c.engine", "c.host", "c.port"):
        assert invented not in body, (
            f"impSelectable re-derives eligibility from {invented} — that belongs "
            "in ps_database_catalog, server-side")


def test_the_disabled_reason_comes_from_the_server():
    body = re.search(r"impDisabledReason\(c\)\s*\{(.*?)\n    \}", HTML, re.S).group(1)
    assert "c.reason" in body


def test_select_all_only_offers_selectable_rows():
    assert "impToggleAll($event.target.checked)" in HTML
    body = re.search(r"impToggleAll\(checked\)\s*\{(.*?)\n    \}", HTML, re.S).group(1)
    assert "impSelectableShown()" in body, "select-all must not select ineligible rows"


# ── the request the modal posts ────────────────────────────────────────────────

def test_the_import_posts_ids_only():
    body = re.search(r"async submitImport\(\)\s*\{(.*?)\n    \},", HTML, re.S).group(1)
    assert "'/api/databases/ps-import'" in body
    assert "system_id:" in body and "account_id:" in body
    # No host/port/account name may be assembled client-side — the server re-resolves
    # every system_id from its own read, and that is the whole injection boundary.
    for banned in ("host:", "port:", "account_name:", "engine:"):
        assert banned not in body, f"submitImport must not send {banned}"


def test_there_is_no_colon_packed_account_key_in_the_import_path():
    # The register modal packs `system:account:name` into one string; the import
    # sends numbers, so the "an account name may contain a colon" class is gone.
    body = re.search(r"async submitImport\(\)\s*\{(.*?)\n    \},", HTML, re.S).group(1)
    assert ".split(':')" not in body


def test_partial_failure_keeps_the_modal_open():
    body = re.search(r"async submitImport\(\)\s*\{(.*?)\n    \},", HTML, re.S).group(1)
    ok_branch = body[body.index("if (!(r.failed"):]
    # showImport = false appears only in the all-succeeded branch.
    assert ok_branch.count("this.showImport = false") == 1
    else_branch = ok_branch[ok_branch.index("} else {"):]
    assert "this.showImport = false" not in else_branch, (
        "closing on a partial failure hides the only place the per-row reasons show")
    assert "loadImportCandidates()" in else_branch, "must re-annotate already_imported"


# ── gating ─────────────────────────────────────────────────────────────────────

def test_the_button_and_modal_sit_inside_the_beyondtrust_gate():
    gates = [m.start() for m in re.finditer(r"\{% if beyondtrust_enabled %\}", HTML)]
    ends = [m.start() for m in re.finditer(r"\{% endif %\}", HTML)]
    button = HTML.index('@click="openImport()"')
    modal = HTML.index('x-show="showImport"')
    for pos, what in ((button, "the toolbar button"), (modal, "the modal")):
        opened = [g for g in gates if g < pos]
        closed = [e for e in ends if e < pos]
        assert opened and len(opened) > len(closed), \
            f"{what} is not inside a beyondtrust_enabled block"


def test_the_page_still_defines_the_helpers_other_tests_pin():
    # test_templates_parse.py asserts these exist for this page.
    for helper in ("regions()", "filteredDatabases()"):
        assert helper.rstrip("()") + "(" in HTML


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
    sys.exit(1 if failures else 0)
