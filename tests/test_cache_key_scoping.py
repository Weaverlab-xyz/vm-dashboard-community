"""Static guard: a dimension-scoped cache key is never read, written, or invalidated
through the unscoped ``key_global`` form.

This is the trap the per-cloud scope tests can't cover on their own. When a cache key
gains a dimension — ``vmcli:aws_amis`` → ``vmcli:aws_amis:region=us-east-2`` — every
site still spelling the old key keeps working *silently*:

  * ``invalidate(key_global("aws_amis"))`` deletes a key nobody writes. It returns
    None either way, so a mutation route reports success and the stale list survives.
  * A cache *warmer* keyed on the old name fills a key nobody reads. It logs "cache
    warmed" and every page load still pays a live cloud fetch.
  * ``_CONFIG_DEPENDENT_CACHES`` in api/setup.py clears by exact key; a scoped name
    left in that tuple instead of ``_CONFIG_DEPENDENT_CACHE_PREFIXES`` means a wizard
    save flushes nothing.

None of those raise, fail a type check, or show up in a diff of the file that moved the
key — which is why this is a source-level sweep rather than a behavioural test. Same
shape as tests/test_no_undefined_names.py: cheap, total, and it imports nothing from
web_dashboard, so it runs even where the cloud-SDK suites skip.

Uses ``ast`` rather than regex because the routers name their keys through
``CACHE_KEY_* = "…"`` constants, so the string never appears at the call site.

Run: python tests/test_cache_key_scoping.py   (or under pytest)
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web_dashboard")

# Caches whose payload varies with a mutable config dimension, and the dimension each
# one is keyed on. Add a name here the moment you add the dimension — that is what
# makes the sweep notice the sites you forgot.
SCOPED_CACHES = {
    "aws_amis":           "region",
    "aws_network_opts":   "region",
    "azure_images":       "location",
    "azure_network_opts": "location",
    "gcp_custom_images":  "project",
    "gcp_instances":      "project",
    "gcp_network_opts":   "region",
    "oci_images":         "region",
    "oci_network_opts":   "region",
    "oci_instances":      "region",
    "ps_db_candidates":   "workgroup",
    # Assets on a share reached through a remote agent. The dimension is which SHARE, and
    # it takes three values to say so: two dashboards could point at the same agent, or
    # one agent at two shares, or one share at two subdirectories. An unscoped key would
    # serve the wrong filenames for a full TTL to a page with no way to know.
    "agent_storage_list": "share",
}

# Names whose key is built by a helper that takes the cache name as a *parameter*, so
# no call site mentions the name and the value can't be resolved statically. Maps
# name → (module whose helper must pass the dimension, dimension).
VIA_HELPER = {
    "oci_images":       ("web_dashboard/api/oci.py", "region"),
    "oci_network_opts": ("web_dashboard/api/oci.py", "region"),
    "oci_instances":    ("web_dashboard/api/oci.py", "region"),
}

# Genuinely global: their payload varies with nothing an operator can change at
# runtime. Listed so the distinction is a decision on the record rather than an
# oversight. aws_instances / azure_vms are dashboard-wide inventories whose fetchers
# iterate every region a deploy job recorded, so they span regions by construction;
# the public GCP image projects are fixed constants in gcp_service.py.
INTENTIONALLY_GLOBAL = {"aws_instances", "azure_vms", "gcp_public_images_"}

KEY_BUILDERS = {"key_global", "key_param"}


def _rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def _modules():
    """(relpath, parsed AST) for every module under web_dashboard/."""
    out = []
    for dirpath, dirnames, filenames in os.walk(WEB):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".venv"}]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            with open(full, "r", encoding="utf-8") as fh:
                src = fh.read()
            try:
                out.append((_rel(full), ast.parse(src, filename=fn)))
            except SyntaxError as exc:  # pragma: no cover
                raise AssertionError(f"{_rel(full)} does not parse: {exc}")
    return out


def _string_constants(tree):
    """Module-level ``NAME = "value"`` map, so key_param(CACHE_KEY_AMIS, …) resolves."""
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _callee(node):
    """Bare name of the function being called, or ''."""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _collect():
    """Every key-builder / invalidate call in web_dashboard, resolved where possible.

    Returns (builds, exact_invalidates, wildcards):
      builds            [(path, lineno, builder, name, {kwargs})]  — name resolved
      exact_invalidates [(path, lineno, name)]  — invalidate(key_global(name))
      wildcards         {path: [{kwargs}, …]}   — key_param(<unresolvable>, **kw)
    """
    builds, exact_invalidates, wildcards = [], [], {}
    for path, tree in _modules():
        consts = _string_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name_of = _callee(node)

            # invalidate(<key_global(...)>) — the silent no-op for a scoped key.
            if name_of == "invalidate" and node.args:
                inner = node.args[0]
                if isinstance(inner, ast.Call) and _callee(inner) == "key_global" and inner.args:
                    arg = inner.args[0]
                    resolved = (arg.value if isinstance(arg, ast.Constant)
                                else consts.get(arg.id) if isinstance(arg, ast.Name) else None)
                    if isinstance(resolved, str):
                        exact_invalidates.append((path, node.lineno, resolved))

            if name_of not in KEY_BUILDERS or not node.args:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            arg = node.args[0]
            resolved = None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                resolved = arg.value
            elif isinstance(arg, ast.Name):
                resolved = consts.get(arg.id)   # None when it's a function parameter
            if resolved is None:
                # f-string (gcp_public_images_{os_filter}) or a name-taking helper.
                if name_of == "key_param":
                    wildcards.setdefault(path, []).append(kwargs)
                continue
            builds.append((path, node.lineno, name_of, resolved, kwargs))
    return builds, exact_invalidates, wildcards


_BUILDS, _EXACT_INVALIDATES, _WILDCARDS = _collect()


def test_the_sweep_actually_sees_the_call_sites():
    """Guard the guard: if the AST walk silently found nothing, every assertion below
    would pass vacuously."""
    assert len(_BUILDS) > 20, f"only found {len(_BUILDS)} key-builder calls — walk broken?"
    names = {n for _, _, _, n, _ in _BUILDS}
    assert "aws_amis" in names, "aws_amis not resolved — is CACHE_KEY_AMIS still a constant?"
    assert _WILDCARDS, "no name-taking key_param helper found — oci._cache_key gone?"


def test_scoped_caches_never_use_key_global():
    """The core sweep: a scoped name inside ``key_global(...)`` is a bug at every kind
    of call site — read, write, warmer key_fn, or invalidate."""
    offenders = [f"{p}:{ln}: key_global(\"{n}\")"
                 for p, ln, builder, n, _ in _BUILDS
                 if builder == "key_global" and n in SCOPED_CACHES]
    assert not offenders, (
        "these cache keys are scoped by a config dimension but are still addressed "
        "through the unscoped key_global form, so they silently never match:\n  "
        + "\n  ".join(offenders))


def test_scoped_caches_are_invalidated_by_prefix():
    """``invalidate_prefix(name)`` matches ``vmcli:<name>:`` — the scoped shape.
    ``invalidate(key_global(name))`` matches only the bare ``vmcli:<name>``, so for a
    scoped key it deletes nothing and reports no error."""
    offenders = [f"{p}:{ln}: invalidate(key_global(\"{n}\"))"
                 for p, ln, n in _EXACT_INVALIDATES if n in SCOPED_CACHES]
    assert not offenders, (
        "exact-key invalidate of a scoped cache — this is a silent no-op, use "
        "cache_service.invalidate_prefix(<name>):\n  " + "\n  ".join(offenders))


def test_every_scoped_cache_is_keyed_on_its_declared_dimension():
    """Each scoped name must actually be given its dimension: either directly via
    ``key_param(name, <dimension>=…)``, or — for names built by a helper that takes the
    name as a parameter — by that helper passing the dimension."""
    missing = []
    for name, dimension in SCOPED_CACHES.items():
        if any(builder == "key_param" and n == name and dimension in kwargs
               for _, _, builder, n, kwargs in _BUILDS):
            continue
        helper = VIA_HELPER.get(name)
        if helper:
            path, dim = helper
            if any(dim in kwargs for kwargs in _WILDCARDS.get(path, [])):
                continue
            missing.append(f"{name}: helper in {path} does not pass {dim}=")
            continue
        missing.append(f'{name}: expected key_param("{name}", {dimension}=…)')
    assert not missing, (
        "declared scoped but never keyed on that dimension:\n  " + "\n  ".join(missing))


def test_scoped_caches_are_in_the_prefix_invalidation_tuple():
    """``api/setup.py`` splits wizard-save invalidation by key shape. A scoped name in
    the exact-key tuple is cleared with ``invalidate(key_global(name))`` — a no-op — so
    it must sit in ``_CONFIG_DEPENDENT_CACHE_PREFIXES`` instead."""
    path = os.path.join(WEB, "api", "setup.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="setup.py")

    tuples = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("_CONFIG_DEPENDENT"):
                tuples[target.id] = [e.value for e in node.value.elts
                                     if isinstance(e, ast.Constant)]

    exact = set(tuples.get("_CONFIG_DEPENDENT_CACHES", []))
    prefixes = set(tuples.get("_CONFIG_DEPENDENT_CACHE_PREFIXES", []))
    assert exact or prefixes, "could not read the setup.py invalidation tuples"
    assert not (exact & prefixes), f"a name must be in exactly one tuple: {exact & prefixes}"

    wrong = sorted(exact & set(SCOPED_CACHES))
    assert not wrong, (
        f"scoped caches sitting in the exact-key tuple, where clearing them is a "
        f"no-op — move to _CONFIG_DEPENDENT_CACHE_PREFIXES: {wrong}")

    # And the flat ones must not be cleared by prefix, which would never match them.
    wrong_way = sorted(prefixes & INTENTIONALLY_GLOBAL)
    assert not wrong_way, (
        f"global caches in the prefix tuple — invalidate_prefix never matches "
        f"vmcli:<name>: {wrong_way}")


def test_intentionally_global_caches_are_still_global():
    """The other half of the decision: these must NOT have quietly gained a dimension
    without being moved into SCOPED_CACHES, or their invalidates start no-oping."""
    overlap = INTENTIONALLY_GLOBAL & set(SCOPED_CACHES)
    assert not overlap, f"a cache cannot be both global and scoped: {sorted(overlap)}"

    offenders = [f"{p}:{ln}: key_param(\"{n}\", {sorted(kwargs)})"
                 for p, ln, builder, n, kwargs in _BUILDS
                 if builder == "key_param" and n in INTENTIONALLY_GLOBAL and kwargs]
    assert not offenders, (
        "declared global but now built with dimensions — move it into SCOPED_CACHES "
        "and switch its invalidates to invalidate_prefix:\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
