"""Smoke test: every Jinja template parses, and the Alpine helpers the region
filters depend on are actually defined in the page that references them.

Template edits are otherwise unverified in this repo — a typo'd x-for or a
filtered() that names a helper nobody defined fails silently in the browser.
This catches the two cheap classes of that: unparseable Jinja, and an x-for
bound to an undefined function.

Runs under pytest, or standalone:  python tests/test_templates_parse.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")

try:
    from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
except ImportError:  # jinja2 absent → skip rather than fail the per-file runner
    Environment = None


def _template_files():
    for root, _dirs, files in os.walk(_TEMPLATES):
        for f in files:
            if f.endswith(".html"):
                full = os.path.join(root, f)
                yield os.path.relpath(full, _TEMPLATES).replace("\\", "/"), full


def test_all_templates_parse():
    if Environment is None:
        print("   (skipped: jinja2 not installed)")
        return
    env = Environment(loader=FileSystemLoader(_TEMPLATES))
    failures = []
    for rel, full in _template_files():
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        try:
            env.parse(src, filename=rel)
        except TemplateSyntaxError as e:
            failures.append(f"{rel}:{e.lineno}: {e.message}")
    assert not failures, "Jinja parse errors:\n  " + "\n  ".join(failures)


# x-for="<var> in <helper>()" — the helper must be defined somewhere in the
# same file, as `helper(` (method shorthand) or `helper:` / `helper =`.
_XFOR_CALL = re.compile(r'x-for="\s*\w+\s+in\s+([A-Za-z_$][\w$]*)\s*\(')


def test_x_for_helpers_are_defined_in_their_page():
    failures = []
    for rel, full in _template_files():
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        for helper in set(_XFOR_CALL.findall(src)):
            defined = (
                re.search(r'\b' + re.escape(helper) + r'\s*\(', src) is not None
                and len(re.findall(r'\b' + re.escape(helper) + r'\s*\(', src)) > 1
            ) or re.search(r'\b' + re.escape(helper) + r'\s*[:=]', src) is not None
            if not defined:
                failures.append(f"{rel}: x-for calls {helper}() but it is never defined")
    assert not failures, "Undefined Alpine helpers:\n  " + "\n  ".join(failures)


# `{{ x | tojson }}` sitting inside a DOUBLE-quoted HTML attribute.
_TOJSON_IN_DQ_ATTR = re.compile(r'=\s*"[^"]*\{\{[^{}]*\|\s*tojson[^{}]*\}\}')


def test_tojson_never_sits_in_a_double_quoted_attribute():
    """|tojson is safe in a <script> body and in a SINGLE-quoted attribute. It is not
    safe in a double-quoted one.

    Jinja escapes <, >, & and ' on the way out, which is what makes it script-safe --
    but it leaves " alone, so `x-data="f({{ slugs | tojson }})"` renders as

        x-data="f([" certificates", ...

    and the browser ends the attribute at the array's first quote. Alpine gets the
    truncated `f([`, throws, and never initialises the component -- so x-cloak is never
    lifted and the page renders its header and nothing else. No failing request, no
    server error, and every other template test passes: test_all_templates_parse sees
    valid Jinja, and test_every_x_data_component_is_defined_somewhere still finds the
    function name inside the truncated text.

    This shipped on the Workload Lab container and left all four tabs blank.
    """
    failures = []
    for rel, full in _template_files():
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        for i, line in enumerate(src.splitlines(), 1):
            if _TOJSON_IN_DQ_ATTR.search(line):
                failures.append(f"{rel}:{i}: |tojson in a double-quoted attribute "
                                f"-- single-quote it: {line.strip()[:90]}")
    assert not failures, ("Unquotable tojson:\n  " +
                          "\n  ".join(failures))


def test_region_filter_pages_define_their_helpers():
    """The Phase-3 region filters specifically: each page that renders a region
    <select> must define the matching distinct-values + filter helpers."""
    expected = {
        "inventory/list.html": ["regions", "filtered"],
        "aws/index.html": ["regions", "filteredInstances"],
        "gcp/index.html": ["regions", "filteredInstances"],
        "k8s/index.html": ["regions", "filteredClusters"],
        "azure/index.html": ["vmLocations", "filteredVms"],
        "databases/index.html": ["regions", "filteredDatabases"],
    }
    failures = []
    for rel, helpers in expected.items():
        full = os.path.join(_TEMPLATES, *rel.split("/"))
        if not os.path.exists(full):
            failures.append(f"{rel}: template missing")
            continue
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        for h in helpers:
            if not re.search(r'\b' + re.escape(h) + r'\s*\(\s*\)\s*\{', src):
                failures.append(f"{rel}: helper {h}() not defined")
    assert not failures, "Missing region-filter helpers:\n  " + "\n  ".join(failures)


def test_inventory_column_count_matches_both_colspans():
    """The Expires column is Jinja-gated, and the loading + empty rows each carry their
    own colspan. A new <th> without updating BOTH leaves those two rows misaligned — the
    single most likely mistake when adding a column here, and nothing else catches it.

    Rendered twice, with the flag on and off, because the whole point of the conditional
    is that both states have to be right.
    """
    from jinja2 import Environment
    full = os.path.join(_TEMPLATES, "inventory", "list.html")
    with open(full, encoding="utf-8") as fh:
        src = fh.read()
    thead = re.search(r"<thead>.*?</thead>", src, re.S)
    assert thead, "inventory/list.html has no <thead>"

    env = Environment()
    # TWO Jinja-gated columns now — Expires and Password Safe — so the combinations are a
    # 2x2 MATRIX, not a pair. Rendering one flag and leaving the other undefined would
    # have Jinja resolve it to falsey, so the test would quietly check a single
    # combination and pass while three others were misaligned.
    for expiry in (True, False):
        for ps in (True, False):
            flags = {"resource_expiry_enabled": expiry, "password_safe_enabled": ps}
            # Render only the fragments that carry the count, so this needs no base
            # template or Alpine runtime.
            n_th = len(re.findall(
                r"<th\b", env.from_string(thead.group(0)).render(**flags)))
            colspans = {
                int(env.from_string("{{ " + expr + " }}").render(**flags))
                for expr in re.findall(r'colspan="\{\{([^}]+)\}\}"', src)
            }
            # Any literal colspans in the table must match too.
            colspans |= {int(v) for v in re.findall(r'colspan="(\d+)"', src)}
            assert colspans == {n_th}, (
                f"{flags}: {n_th} <th> but colspan(s) {sorted(colspans)}")


def test_pov_ladder_colspan_matches_the_managed_table():
    """The setup ladder is a full-width second row, so its colspan must equal the number
    of columns above it.

    Same failure as the inventory test above and the same reason nothing else catches it:
    a new <th> in that table leaves the ladder row short, which does not error -- it just
    renders misaligned. The managed table's <thead> is the only one in the file with a
    Broker column, which is how it is told from the "all environments" and "Past POVs"
    tables beside it.
    """
    full = os.path.join(_TEMPLATES, "pov", "index.html")
    with open(full, encoding="utf-8") as fh:
        src = fh.read()

    theads = [m.group(0) for m in re.finditer(r"<thead\b.*?</thead>", src, re.S)]
    managed = [t for t in theads if ">Broker<" in t]
    assert len(managed) == 1, (
        f"expected exactly one <thead> carrying a Broker column, found {len(managed)}")
    n_th = len(re.findall(r"<th\b", managed[0]))

    spans = {int(v) for v in re.findall(r':colspan="(\d+)"', src)}
    spans |= {int(v) for v in re.findall(r'colspan="(\d+)"', src)}
    assert spans == {n_th}, (
        f"the managed POV table has {n_th} columns but the ladder row spans "
        f"{sorted(spans)}")


def _run_node(script, label):
    """Run a tests/*.js harness in its own node process. Skips when node isn't
    installed (it is on the CI runner)."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        print("   (skipped: node not installed)")
        return
    proc = subprocess.run([shutil.which("node"), os.path.join(_ROOT, "tests", script)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, (
        label + " failed:\n" + proc.stdout + proc.stderr)


def test_alpine_region_helpers_behave():
    """Run the node harness, which extracts each region helper from its template
    and exercises it."""
    _run_node("template_helpers_check.js", "template helper checks")


def test_toast_carries_the_request_access_link():
    """Entitle user-JIT Phase 4: the request-access deep link has to survive from the
    403 body all the way to the toast object the renderer reads. Its own harness
    because it stubs fetch/Alpine, which the helper checks must not inherit."""
    _run_node("toast_request_access_check.js", "toast deep-link checks")


def test_api_errors_carry_their_policy_reasons():
    """A guardrail refusal must reach the operator with its reason, and a
    change-window refusal with its offer.

    `API.request` understood only a string `detail` or `detail.message`, but the
    admission gate answers `{error, reasons: [...]}` — so every policy denial in the
    product surfaced as a bare `HTTP 403` and the operator was never told which rule
    refused them. Same class of bug as the toast deep link above: produced,
    serialised, and dropped one hop before the screen."""
    _run_node("api_error_reasons_check.js", "API error reason checks")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
