"""Gate: every by-id route on /api/k8s and /api/databases checks ownership.

The bug this exists to prevent already shipped once. Ownership was enforced in the two
LIST endpoints and nowhere else, so `require_permission("k8s", "delete")` — which answers
"may you delete clusters at all" — was the only thing standing between a non-admin and
`DELETE /api/k8s/clusters/<someone-elses-id>`. Roughly thirty routes were reachable that
way, including three that hand back a **cluster-admin kubeconfig** and one that returns a
database's connection descriptor.

Why a source sweep rather than a router-level dependency, which is what `api/pov.py` uses
for its ~40 `{env_id}` routes: `GET /api/k8s/__phase1__` is deliberately unauthenticated
(it is the router-mounted health probe), and a router dependency that resolved a user
would start rejecting it. So the check is per-handler, which makes it a thing every future
route has to remember — and this file is what remembers instead of a person.

Pure stdlib `ast`, in the style of tests/test_no_redefined_names.py and
tests/test_import_guard_narrowness.py: no app import, no dependencies, so it cannot skip.

Runs under pytest, or standalone:  python tests/test_k8s_db_action_ownership.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (module, path parameter that names one row, minimum route count)
#
# The floors are not decoration. A sweep whose matcher silently stops matching scans
# nothing and passes — the lesson tests/test_inventory_service.py records. If a refactor
# genuinely removes routes, lower the number deliberately and say why in the commit.
_MODULES = (
    ("web_dashboard/api/k8s.py", "cluster_id", 24),
    ("web_dashboard/api/cloud_databases.py", "db_id", 5),
)

_GUARD = "_visible_or_404"

# An admin-only route needs no ownership check: an administrator passes every version of
# one. Encoded as an ALTERNATIVE rather than an allowlist of function names on purpose --
# downgrading such a route to a grantable scope (require_permission("k8s", "write")) must
# make this gate fire, which a list keyed on the name would not. Today this covers only
# the two PATCH .../workgroup retag endpoints.
_ADMIN_ONLY = "Depends(require_admin)"

# The creator comparison these routes used to open-code. Six copies existed across
# api/k8s.py, api/cloud_databases.py, api/dashboard.py and api/mcp_server.py; all six now
# delegate to inventory_service.row_visible_to. Listed as fragments because the six were
# not spelled identically, which is exactly how they drifted.
_OPEN_CODED = (
    'r.get("created_by") == current_user.username',
    'r.get("created_by") == user.username',
)
_NO_OPEN_CODING = (
    "web_dashboard/api/k8s.py",
    "web_dashboard/api/cloud_databases.py",
    "web_dashboard/api/dashboard.py",
    "web_dashboard/api/mcp_server.py",
)


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _by_id_handlers(src, param):
    """(name, body_source) for every top-level route handler naming ``param``.

    Underscore-prefixed functions are skipped: those are the module's own helpers,
    including the guard itself, which would otherwise have to guard itself."""
    tree = ast.parse(src)
    lines = src.splitlines()
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        args = [a.arg for a in node.args.args + node.args.kwonlyargs]
        if param not in args:
            continue
        out.append((node.name, "\n".join(lines[node.lineno - 1:node.end_lineno])))
    return out


def test_every_by_id_route_checks_ownership_or_is_admin_only():
    problems = []
    for rel, param, _floor in _MODULES:
        for name, body in _by_id_handlers(_read(rel), param):
            if _GUARD in body or _ADMIN_ONLY in body:
                continue
            problems.append(
                f"{rel}::{name} takes {param} but neither calls {_GUARD} nor is "
                f"admin-only")
    assert not problems, "unguarded by-id route(s): " + "; ".join(problems)


def test_most_by_id_routes_take_the_guard_not_the_admin_exemption():
    """Keeps the admin exemption from quietly becoming the normal way to pass.

    If a change makes most by-id routes admin-only, that is a far larger decision than
    this gate was written for and must not slip through as a green run."""
    for rel, param, _floor in _MODULES:
        handlers = _by_id_handlers(_read(rel), param)
        exempt = [n for n, b in handlers if _GUARD not in b and _ADMIN_ONLY in b]
        assert len(exempt) <= 2, (
            f"{rel}: {len(exempt)} by-id routes lean on the admin-only exemption "
            f"({exempt}); the ownership guard is meant to be the rule")


def test_the_sweep_is_actually_finding_routes():
    """Without this, a matcher that stops matching reports a clean run forever."""
    for rel, param, floor in _MODULES:
        found = _by_id_handlers(_read(rel), param)
        assert len(found) >= floor, (
            f"{rel}: found only {len(found)} handlers taking {param}, expected >= {floor}. "
            f"Either routes were removed (lower the floor on purpose) or the matcher "
            f"broke and this gate is now scanning nothing.")


def test_the_guard_is_defined_in_both_modules():
    for rel, _param, _floor in _MODULES:
        src = _read(rel)
        assert f"def {_GUARD}(" in src, f"{rel} has no {_GUARD} of its own"


def test_the_guard_answers_404_and_not_403():
    """404, matching api/spire_lab._visible_or_404, api/cert_lab._visible and
    api/auth.require_pov_env_access. The list endpoint HIDES a row the caller may not
    see, so a 403 here would confirm the existence of the very thing RBAC just denied and
    turn the id into something worth guessing."""
    for rel, _param, _floor in _MODULES:
        tree = ast.parse(_read(rel))
        lines = _read(rel).splitlines()
        body = None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _GUARD:
                body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        assert body, f"{rel}: no {_GUARD} to inspect"
        assert "status_code=404" in body, f"{rel}: {_GUARD} must answer 404"
        assert "status_code=403" not in body, (
            f"{rel}: {_GUARD} must not answer 403 — that leaks existence")


def test_the_creator_comparison_is_not_open_coded_anywhere():
    """One rule, one implementation. Six modules each carried their own copy of the
    creator comparison, which is how they came to disagree — api/dashboard.py's tile
    could count rows its own page would not list."""
    problems = []
    for rel in _NO_OPEN_CODING:
        src = _read(rel)
        for frag in _OPEN_CODED:
            if frag in src:
                problems.append(f"{rel} re-implements the rule: {frag}")
    assert not problems, (
        "creator-scoping must delegate to inventory_service.row_visible_to:\n  "
        + "\n  ".join(problems))


def test_the_open_coding_check_would_catch_a_regression():
    """Proves the fragment list above is not stale wording that can never match."""
    sample = 'rows = [r for r in rows if r.get("created_by") == current_user.username]'
    assert any(frag in sample for frag in _OPEN_CODED), (
        "the fragments no longer match the pattern they were written for")


def test_the_credential_returning_routes_guard_before_they_build():
    """Four routes hand back a working credential for the resource. For those the guard
    has to run BEFORE the document is constructed, not alongside the name lookup that
    used to follow it — otherwise a cluster-admin kubeconfig is assembled for a caller
    who is about to be refused, and any future 'return early on error' refactor leaks it.

    Asserted by position: the guard call must precede the builder call in the source."""
    cases = (
        ("web_dashboard/api/k8s.py", "build_api_tunnel_kubeconfig"),
        ("web_dashboard/api/k8s.py", "build_entra_oidc_kubeconfig"),
        ("web_dashboard/api/k8s.py", "console_url"),
        ("web_dashboard/api/cloud_databases.py", "connection_info"),
    )
    for rel, builder in cases:
        src = _read(rel)
        call = f"{builder}("
        assert call in src, f"{rel}: {builder} is gone — re-point this assertion"
        for name, body in _by_id_handlers(src, "cluster_id" if "k8s" in rel else "db_id"):
            if call not in body:
                continue
            g, b = body.index(_GUARD), body.index(call)
            assert g < b, (
                f"{rel}::{name} builds {builder} before checking ownership")


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
