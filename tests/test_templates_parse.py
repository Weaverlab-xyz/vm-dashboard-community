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


def test_alpine_region_helpers_behave():
    """Run the node harness, which extracts each region helper from its template
    and exercises it. Skips when node isn't installed."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        print("   (skipped: node not installed)")
        return
    script = os.path.join(_ROOT, "tests", "template_helpers_check.js")
    proc = subprocess.run([shutil.which("node"), script],
                          capture_output=True, text=True)
    assert proc.returncode == 0, (
        "template helper checks failed:\n" + proc.stdout + proc.stderr)


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
