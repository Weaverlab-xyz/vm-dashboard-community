"""A platform-separator path may not be compared against a forward-slash literal.

`os.path.relpath()` and `Path.relative_to()` return the PLATFORM's separator, so on
Windows they yield `docs\\kubernetes.md`. Compare one of those against a `"docs/..."`
literal and the test silently never matches locally and always matches in CI.

This bit `tests/test_install_profile.py` (fixed in #823). It built its list with
`os.path.relpath` and asserted membership against forward-slash literals, so a Windows
run reported:

    FAIL test_the_page_header_still_names_the_stored_profile_value:
         docs/kubernetes.md no longer names its profile

while every page still carried its `**Profile:**` header. The sibling
`len(carriers) >= 20` assertion passed throughout -- a count does not care about
separators -- so only the three explicitly-named files tripped, which is exactly what
made it read as somebody's docs change rather than a platform artefact. It cost a round
trip to disprove, and it would have cost one again.

Same family as `tests/test_open_encoding.py` (cp1252 on an un-encoded `open()`): a
portability defect that CI can never see, because CI is only ever Linux. That asymmetry
is the reason it needs a test rather than a habit -- the one machine that could catch it
is the one machine nobody gates on.

DELIBERATELY NARROW. Only a genuine *comparison* is reported, never the mere use of
`relpath`. Four forms in this repo look like the bug and are not, and all four must stay
quiet or the guard gets disabled the first time somebody trips it on a false positive:

  * message text -- `offenders.append(f"{os.path.relpath(p, _ROOT)}: ...")`. ~20 sites.
    The assertion is on emptiness; the separator only reaches a human's eyes.
  * a lookup key built the same way on both sides -- `test_k8s_token_wiring.py` keys its
    allowlist with `os.path.join(...)`, so the key and the probe agree on every platform.
  * an `os.sep`-based substring test -- `test_playbook_samples.py` asks
    `os.sep + "windows" + os.sep in path` rather than `"/windows/" in path`.
  * a slash literal used to BUILD a path rather than to compare against one --
    `test_mobile_pages.py` feeds `"xcpng/index.html"` into `os.path.join`, and
    `os.path.join` accepts a forward slash on Windows.

Scope is `tests/` and `web_dashboard/`. `runners/` has no `relpath`/`relative_to` call at
all, so including it would only give the floor assertion something to be wrong about.

Runs under pytest, or standalone:  python tests/test_path_separator_portability.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TREES = ("tests", "web_dashboard")

# Calls returning a path fragment that carries the platform's separator.
_PATH_DERIVED = {"relpath", "relative_to"}
# ...and the calls that put it back to "/", which make the value portable again.
_NORMALISERS = {"replace", "as_posix"}


def _rel(path):
    # The idiom this whole file exists to enforce, applied to itself.
    return os.path.relpath(path, _ROOT).replace(os.sep, "/")


def _files():
    for tree in _TREES:
        for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, tree)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fname in sorted(filenames):
                if fname.endswith(".py"):
                    yield os.path.join(dirpath, fname)


def _called_attrs(node):
    """Every attribute-style call name appearing anywhere inside ``node``."""
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _is_path_derived(node):
    return bool(_called_attrs(node) & _PATH_DERIVED)


def _is_normalised(node):
    return bool(_called_attrs(node) & _NORMALISERS)


def _slash_str(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and "/" in node.value)


def _slash_literals(node):
    """A forward-slash string, or a tuple/list/set containing one."""
    if _slash_str(node):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_slash_str(e) for e in node.elts)
    return False


def _scope_nodes(scope):
    """Every node in one scope, NOT descending into a nested def or class.

    Per-scope is the whole reason this is quiet enough to ship: `rel` is bound in a
    dozen functions per file, and a flat module walk would happily pair one function's
    unnormalised `rel` with another's slash literal.
    """
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            out.append(child)
            walk(child)

    walk(scope)
    return out


def _scope_offenders(scope, rel, counts):
    """Bindings first, then comparisons against them."""
    nodes = _scope_nodes(scope)
    unnormalised, slashy, slash_keyed = set(), set(), set()

    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if _is_path_derived(node.value):
                counts["derived"] += 1
                # A comprehension counts: `[os.path.relpath(p, _ROOT) for p in ...]`
                # binds a COLLECTION of unnormalised fragments, which is the shape the
                # original bug had.
                if _is_normalised(node.value):
                    unnormalised.discard(name)
                else:
                    unnormalised.add(name)
            elif _slash_literals(node.value):
                slashy.add(name)
            elif isinstance(node.value, ast.Dict) and any(
                    _slash_str(k) for k in node.value.keys if k is not None):
                slash_keyed.add(name)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) \
                and _slash_literals(node.iter):
            # `for expected in ("docs/a.md", "docs/b.md"):`
            slashy.add(node.target.id)

    offenders = []
    for node in nodes:
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            if not isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                continue
            left, right = node.left, node.comparators[0]
            # Either side may hold the path: `rel == "a/b"` and `"a/b" in carriers`.
            for path_side, lit_side in ((left, right), (right, left)):
                if not (isinstance(path_side, ast.Name)
                        and path_side.id in unnormalised):
                    continue
                if _slash_literals(lit_side) or (isinstance(lit_side, ast.Name)
                                                 and lit_side.id in slashy):
                    offenders.append(
                        f"{rel}:{node.lineno}: `{path_side.id}` carries the platform "
                        f"separator and is compared against a forward-slash literal")
                    break
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id in slash_keyed and isinstance(node.slice, ast.Name) \
                and node.slice.id in unnormalised:
            offenders.append(
                f"{rel}:{node.lineno}: `{node.value.id}[{node.slice.id}]` looks up a "
                f"platform-separator path in a dict keyed by forward-slash literals")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id in slash_keyed and node.args \
                and isinstance(node.args[0], ast.Name) \
                and node.args[0].id in unnormalised:
            offenders.append(
                f"{rel}:{node.lineno}: `{node.func.value.id}.get({node.args[0].id})` "
                f"probes a dict keyed by forward-slash literals with a platform path")
    return offenders


def _check_source(src, rel="<inline>"):
    counts = {"derived": 0, "calls": 0}
    tree = ast.parse(src)
    offenders = _scope_offenders(tree, rel, counts)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders += _scope_offenders(node, rel, counts)
    return offenders, counts


# ── the sweep ────────────────────────────────────────────────────────────────

def test_no_platform_path_is_compared_to_a_forward_slash_literal():
    offenders, counts, files = [], {"derived": 0, "calls": 0}, 0
    for path in _files():
        rel = _rel(path)
        with open(path, "r", encoding="utf-8") as handle:
            src = handle.read()
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:      # a file that won't parse is a louder problem
            offenders.append(f"{rel}: does not parse: {exc}")
            continue
        files += 1
        offenders += _scope_offenders(tree, rel, counts)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                offenders += _scope_offenders(node, rel, counts)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in _PATH_DERIVED:
                counts["calls"] += 1

    # Two floors, because they fail for different reasons. `calls` catches the call
    # pattern drifting (a rename, or everything moving to pathlib); `derived` catches
    # the BINDING analysis silently matching nothing, which is what actually decides
    # whether a comparison can ever be reported. Measured 657 / 51 / 12 today.
    assert files >= 500, (
        f"expected to walk both trees, saw {files} files -- the walk may have stopped "
        "finding code rather than started passing")
    assert counts["calls"] >= 40, (
        f"only {counts['calls']} relpath/relative_to CALLS matched -- the pattern may "
        "have stopped matching rather than started passing")
    assert counts["derived"] >= 8, (
        f"only {counts['derived']} of those were bound to a name -- the binding "
        "analysis, which is what makes a comparison reportable, may have stopped "
        "matching rather than started passing")
    assert not offenders, (
        "these compare a platform-separator path against a forward-slash literal, so "
        "they pass on Linux CI and fail on Windows (or the reverse -- a guard that "
        "never matches). Normalise where the value is built:\n"
        "    rel = os.path.relpath(path, _ROOT).replace(os.sep, \"/\")\n"
        + "\n".join(offenders))


# ── the guard's own floor: it must still catch the bug it was written for ────

def test_the_sweep_catches_the_original_bug():
    """A sweep that has quietly stopped matching passes forever. This pins the exact
    shape from #823 -- a list comprehension of `relpath`, membership-tested against a
    tuple of forward-slash literals -- so the walk cannot rot into a no-op."""
    offenders, _ = _check_source(
        "def test_x():\n"
        "    carriers = [os.path.relpath(p, _ROOT) for p in _doc_files() if h in p]\n"
        "    for expected in ('docs/kubernetes.md', 'docs/databases.md'):\n"
        "        assert expected in carriers\n")
    assert offenders, "the sweep no longer catches the bug it was written for"
    assert "carriers" in offenders[0], offenders

    # ...and the direct equality form.
    direct, _ = _check_source(
        "def test_y():\n"
        "    rel = os.path.relpath(p, _ROOT)\n"
        "    assert rel == 'docs/kubernetes.md'\n")
    assert direct, "an `==` against a slash literal is not caught"


def test_the_sweep_stays_quiet_on_the_four_safe_forms():
    """The false positives that would get this test deleted rather than obeyed."""
    cases = {
        "message text only":
            "def t():\n"
            "    offenders = []\n"
            "    offenders.append(f'{os.path.relpath(p, _ROOT)}: bad')\n"
            "    assert not offenders\n",
        "both sides built with os.path.join":
            "def t():\n"
            "    allowed = {os.path.join('web_dashboard', 'x.py'): {'n'}}\n"
            "    rel = os.path.relpath(path, _ROOT)\n"
            "    if name in allowed.get(rel, set()):\n"
            "        pass\n",
        "os.sep substring test":
            "def t():\n"
            "    if os.sep + 'windows' + os.sep in path:\n"
            "        pass\n",
        "slash literal builds a path":
            "def t():\n"
            "    for rel in ('xcpng/index.html',):\n"
            "        src = _read(os.path.join(_TPL, rel))\n",
        "normalised at the binding":
            "def t():\n"
            "    rel = os.path.relpath(p, _ROOT).replace(os.sep, '/')\n"
            "    assert rel == 'docs/kubernetes.md'\n",
        "normalised with as_posix":
            "def t():\n"
            "    rel = p.relative_to(_ROOT).as_posix()\n"
            "    assert rel in ('docs/a.md', 'docs/b.md')\n",
    }
    for label, src in cases.items():
        offenders, _ = _check_source(src)
        assert not offenders, f"false positive on {label}: {offenders}"


def test_the_scope_is_per_function_not_per_module():
    """`rel` is bound in a dozen functions per file. A flat module walk would pair one
    function's unnormalised binding with another's slash literal and report a bug that
    does not exist -- which is how a sweep earns its way into an ignore list."""
    offenders, _ = _check_source(
        "def a():\n"
        "    rel = os.path.relpath(p, _ROOT)\n"
        "    return rel\n"
        "\n"
        "def b():\n"
        "    rel = 'docs/kubernetes.md'\n"
        "    assert rel == 'docs/kubernetes.md'\n")
    assert not offenders, f"scopes leaked across functions: {offenders}"


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
