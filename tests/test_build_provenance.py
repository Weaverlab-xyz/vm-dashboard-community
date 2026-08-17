"""Build provenance for deployed cloud functions.

A deployed function runs privileged code from inside your network, so "what exactly
is running in there, and where did it come from?" has to stay answerable after the
image that produced it is gone.

Two properties matter most and are easy to get wrong in opposite directions:
provenance must never **fail a deploy**, and it must never **invent** a fact it does
not have. A confidently wrong commit is worse than an honest blank.
"""
import hashlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard.services import build_provenance as prov
from web_dashboard.services import cloud_function_package as pkg

_ENV_KEYS = ("DASHBOARD_GIT_SHA", "DASHBOARD_GIT_REF", "DASHBOARD_GIT_ORIGIN",
             "DASHBOARD_BUILT_AT")


def _clear_env():
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


# ── The source-tree hash: the layer that is always available ─────────────────

def test_the_tree_hash_is_stable_and_content_addressed():
    """Two calls agree, and it is derived from content — so two functions sharing
    the value are running the same handler code."""
    first = pkg.source_tree_sha256()
    second = pkg.source_tree_sha256()
    assert first == second
    assert len(first) == 64 and int(first, 16) >= 0


def test_the_tree_hash_changes_when_a_workload_changes():
    """The whole point: if the handler source moves, the value must move."""
    with tempfile.TemporaryDirectory() as tmp:
        for sub in ("fnruntime", "fnworkloads", "fnentry"):
            os.makedirs(os.path.join(tmp, sub))
            with io.open(os.path.join(tmp, sub, "__init__.py"), "w",
                         encoding="utf-8") as fh:
                fh.write("")
        target = os.path.join(tmp, "fnworkloads", "demo.py")
        with io.open(target, "w", encoding="utf-8") as fh:
            fh.write("NAME='demo'\n")
        before = pkg.source_tree_sha256(tmp)
        with io.open(target, "w", encoding="utf-8") as fh:
            fh.write("NAME='demo'  # changed\n")
        after = pkg.source_tree_sha256(tmp)
        assert before != after


def test_the_tree_hash_ignores_bytecode():
    """A __pycache__ left behind by an import must not change the answer."""
    with tempfile.TemporaryDirectory() as tmp:
        for sub in ("fnruntime", "fnworkloads", "fnentry"):
            os.makedirs(os.path.join(tmp, sub))
        with io.open(os.path.join(tmp, "fnworkloads", "demo.py"), "w",
                     encoding="utf-8") as fh:
            fh.write("NAME='demo'\n")
        before = pkg.source_tree_sha256(tmp)
        cache = os.path.join(tmp, "fnworkloads", "__pycache__")
        os.makedirs(cache)
        with io.open(os.path.join(cache, "demo.cpython-312.pyc"), "w",
                     encoding="utf-8") as fh:
            fh.write("junk")
        assert pkg.source_tree_sha256(tmp) == before


# ── Image metadata wins, and absence is honest ──────────────────────────────

def test_image_build_args_are_used_when_present():
    _clear_env()
    os.environ.update({"DASHBOARD_GIT_SHA": "a" * 40,
                       "DASHBOARD_GIT_REF": "main",
                       "DASHBOARD_GIT_ORIGIN": "https://example.com/repo.git",
                       "DASHBOARD_BUILT_AT": "2026-08-16T00:00:00Z"})
    try:
        got = prov.collect()
    finally:
        _clear_env()
    assert got["commit"] == "a" * 40
    assert got["ref"] == "main"
    assert got["source"] == "image"
    assert got["built_at"] == "2026-08-16T00:00:00Z"


def test_the_tree_hash_is_present_even_with_no_git_metadata_at_all():
    """An image built without the build args still answers 'what is running'."""
    _clear_env()
    got = prov.collect()
    assert got["tree_sha256"], "the always-available layer went missing"


def test_a_missing_commit_is_reported_as_unknown_not_guessed():
    """A confidently wrong commit is worse than an honest blank."""
    _clear_env()
    real_git = prov._git_provenance
    prov._git_provenance = lambda: {}
    try:
        got = prov.collect()
    finally:
        prov._git_provenance = real_git
    assert got["commit"] == ""
    assert got["source"] == "unknown"
    assert got["dirty"] is None, "unknown dirtiness must not be reported as clean"


def test_collect_never_raises_even_when_everything_fails():
    """Provenance is metadata. It must not be able to fail a deploy."""
    _clear_env()
    real_git, real_pkg = prov._git_provenance, pkg.source_tree_sha256

    def _boom(*a, **k):
        raise RuntimeError("boom")

    prov._git_provenance = _boom
    pkg.source_tree_sha256 = _boom
    try:
        got = prov.collect()
    finally:
        prov._git_provenance, pkg.source_tree_sha256 = real_git, real_pkg
    assert isinstance(got, dict) and got["tree_sha256"] == ""


def test_git_failures_are_swallowed():
    assert prov._git("definitely-not-a-git-subcommand") == ""


# ── Dirtiness ────────────────────────────────────────────────────────────────

def test_a_dirty_tree_is_recorded():
    """The most misleading thing provenance can omit: the running code does not
    match the commit it claims."""
    _clear_env()
    real = prov._git
    calls = {"status": " M web_dashboard/x.py"}
    prov._git = lambda *args: (calls["status"] if args[0] == "status"
                               else ("b" * 40 if args[:2] == ("rev-parse", "HEAD") else "x"))
    try:
        got = prov.collect()
    finally:
        prov._git = real
    assert got["dirty"] is True
    assert "dirty" in prov.describe(got)


# ── What reaches the function and the operator ──────────────────────────────

def test_the_function_environment_carries_provenance():
    """So a RUNNING function can report what it is, instead of you trusting the row."""
    env = prov.env_for_function({"commit": "c" * 40, "tree_sha256": "d" * 64})
    assert env["FN_SOURCE_COMMIT"] == "c" * 40
    assert env["FN_SOURCE_TREE"] == "d" * 64


def test_the_function_environment_omits_what_is_unknown():
    """An empty FN_SOURCE_COMMIT would look like a value; absence is clearer."""
    assert prov.env_for_function({"commit": "", "tree_sha256": ""}) == {}
    assert prov.env_for_function({}) == {}


def test_the_function_environment_carries_no_secret():
    env = prov.env_for_function(prov.collect())
    assert all(k.startswith("FN_SOURCE_") for k in env), env


def test_describe_is_readable_and_degrades():
    assert prov.describe({}) == "unknown"
    assert prov.describe({"commit": "", "tree_sha256": ""}) == "unknown"
    line = prov.describe({"commit": "e" * 40, "ref": "main", "tree_sha256": "f" * 64})
    assert "eeeeeeeeeeee" in line and "(main)" in line and "tree:" in line


def test_echo_diag_reports_what_it_is_running():
    """The verification path: ask the function, do not trust the record."""
    from web_dashboard import functions  # noqa: F401
    from fnruntime.contract import Context, Request
    from fnworkloads import echo_diag

    os.environ.update({"FN_SOURCE_COMMIT": "9" * 40, "FN_SOURCE_TREE": "8" * 64})
    try:
        resp = echo_diag.handle(
            Request(method="POST", path="/", headers={}, query={},
                    body=b'{"egress": false}', source="aws_function_url"),
            Context.from_env(workload="echo_diag"))
    finally:
        os.environ.pop("FN_SOURCE_COMMIT", None)
        os.environ.pop("FN_SOURCE_TREE", None)
    assert resp.body["provenance"]["source_commit"] == "9" * 40
    assert resp.body["provenance"]["source_tree"] == "8" * 64


def test_the_module_is_stdlib_only():
    """It is imported on the deploy path in every deployment shape."""
    import ast
    tree = ast.parse(io.open(prov.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"logging", "os", "subprocess", "__future__"}, imported


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
