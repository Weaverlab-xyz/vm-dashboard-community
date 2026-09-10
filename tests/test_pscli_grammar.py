"""Every ps-cli invocation names a SERVICE and then a real verb.

ps-cli groups all its subcommands under a service — ``safes``, ``folders``, ``secrets``,
``settings`` — and argparse rejects a bare verb with ``invalid choice``. That failure is
a RUNTIME one, raised the first time that particular call is exercised, so a wrong argv
can sit in the tree for months looking perfectly reasonable.

It did. SIX calls in ``secrets_backend_service`` shipped without the service token:
``list-safes``, ``create-safe``, ``update-safe``, ``delete-safe``, ``create`` (a folder)
and ``delete`` (a folder). Nothing had ever created a Secrets Safe folder or safe from the
dashboard, so nothing ever ran them — until the SPIRE lab's folder pre-flight did, and
failed on a live tenant with a wall of argparse usage text. The sixth was found only when
this file learned to resolve an argv built in a variable; a literal-only sweep reported
five and looked complete. ``list_bt_folders``'s own docstring records an EARLIER
instance of the same bug (a bare ``list``), fixed in isolation without anyone checking its
neighbours.

``beyondtrust-bips-cli`` is **unpinned** in ``web_dashboard/requirements.txt``, so the CLI
can move under a rebuild. This file is the cheap standing check that argv still has the
shape ps-cli wants.

The verb sets below were read from ``ps-cli <service> -h`` (0.13.0), which lists a long
name and a short alias for each — ``list-safes`` / ``list``, ``create-folder`` / ``create``.
Both are valid. Adding a call with a new verb means adding it here, deliberately.

Static and stdlib-only — no app imports, no ps-cli, no tenant.
Runs under pytest or standalone:  python tests/test_pscli_grammar.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MODULE = os.path.join(_ROOT, "web_dashboard", "services", "secrets_backend_service.py")

# Verified against `ps-cli <service> -h`. Long form and short alias are both accepted.
VERBS = {
    "safes": {
        "create-safe", "create", "delete-safe", "delete", "get-safe", "get",
        "list-safes", "list", "update-safe", "update",
    },
    "folders": {
        "create-folder", "create", "delete-folder", "delete", "get-folder", "get",
        "import-csv", "import", "upload", "list-folders", "list", "move-folder", "move",
    },
    "secrets": {
        "create-secret", "create", "create-secret-share", "create-share",
        "delete-all-secret-shares", "delete-all-shares", "delete-secret", "delete",
        "delete-secret-share", "delete-share", "download-secret-file", "download",
        "get-secret", "get", "get-secret-shares", "shares", "list-secrets", "list",
        "move-secrets", "move", "update-secret", "update",
    },
    "settings": set(),
}


def _read(path=_MODULE) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _ps_run_argvs(src: str):
    """``(lineno, argv)`` for every ``_ps_run(...)`` call, plus lines it could not read.

    Two shapes are resolved, because both occur and the second is where the bug hid:

    * ``_ps_run(["folders", "list"])`` — a list literal, read directly;
    * ``args = ["safes", "create-safe", ...]`` then ``_ps_run(args)`` — resolved by
      finding that assignment in the enclosing function. ``create_bt_safe`` was stale
      for exactly as long as a literal-only sweep would have kept missing it.

    A list element that is a variable (``safe_id``, ``title``) becomes the sentinel
    ``<var>``: its value is irrelevant, and the two positions that matter — service and
    verb — are always string literals.
    """
    tree = ast.parse(src)

    def _argv_of(node):
        return [e.value if (isinstance(e, ast.Constant) and isinstance(e.value, str))
                else "<var>" for e in node.elts]

    found, dynamic = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Every `name = [...]` in this function, so a call can look one up.
        lists = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        lists[tgt.id] = _argv_of(node.value)
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_ps_run" and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.List):
                found.append((node.lineno, _argv_of(first)))
            elif isinstance(first, ast.Name) and first.id in lists:
                found.append((node.lineno, lists[first.id]))
            else:
                dynamic.append(node.lineno)
    return found, dynamic


def test_the_sweep_actually_finds_the_calls():
    """A sweep that silently matches nothing passes forever."""
    found, _ = _ps_run_argvs(_read())
    assert len(found) >= 10, f"expected the module's ps-cli calls; found {len(found)}"


def test_every_ps_run_argv_starts_with_a_service():
    """The bug this file exists for. ``["create", ...]`` is not a ps-cli command."""
    found, _ = _ps_run_argvs(_read())
    offenders = [(ln, argv) for ln, argv in found if argv[0] not in VERBS]
    assert not offenders, (
        "ps-cli argv must start with a service (%s):\n  %s"
        % (", ".join(sorted(VERBS)),
           "\n  ".join(f"line {ln}: {argv}" for ln, argv in offenders)))


def test_every_verb_is_one_the_service_actually_has():
    """Catches the other half: right service, verb that does not exist under it."""
    found, _ = _ps_run_argvs(_read())
    offenders = []
    for ln, argv in found:
        service = argv[0]
        if service not in VERBS or not VERBS[service]:
            continue
        if len(argv) < 2:
            offenders.append(f"line {ln}: {argv} names a service with no verb")
        elif argv[1] not in VERBS[service]:
            offenders.append(f"line {ln}: {service!r} has no verb {argv[1]!r}")
    assert not offenders, "unknown ps-cli verb:\n  " + "\n  ".join(offenders)


def test_every_ps_run_argv_is_a_readable_literal():
    """An argv assembled from a variable cannot be checked above, so it must not exist
    without someone deciding it should. If one is added deliberately, extend this file to
    understand it rather than deleting the guard."""
    _, dynamic = _ps_run_argvs(_read())
    assert not dynamic, (
        "ps-cli argv built dynamically at line(s) %s — the grammar guard cannot read it"
        % ", ".join(str(ln) for ln in dynamic))


def test_the_folder_calls_are_the_ones_the_spire_lab_needs():
    """The three the folder pre-flight walks. Named explicitly because these are the
    calls that had never been exercised, and the ones a future edit is most likely to
    'simplify' back to a bare verb."""
    found, _ = _ps_run_argvs(_read())
    argvs = [argv for _, argv in found]
    for expected in (["safes", "list-safes"],
                     ["folders", "list"],
                     ["folders", "create-folder", "-pid", "-n"]):
        head = expected[:2]
        match = next((a for a in argvs if a[:2] == head), None)
        assert match, f"no ps-cli call for {' '.join(head)}"
        for flag in expected[2:]:
            assert flag in match, f"{' '.join(head)} lost its {flag} argument: {match}"


def test_the_cli_dependency_is_still_unpinned_and_that_is_written_down():
    """Not a demand to pin it — a demand that the risk stays visible. If someone pins
    the package, this fails and they update the note in `_ps_run` that explains why this
    file exists."""
    reqs = _read(os.path.join(_ROOT, "web_dashboard", "requirements.txt"))
    line = next((l for l in reqs.splitlines()
                 if l.strip().startswith("beyondtrust-bips-cli")), "")
    assert line, "beyondtrust-bips-cli is no longer in requirements.txt"
    unpinned = line.strip() == "beyondtrust-bips-cli"
    note = "UNPINNED in requirements.txt" in _read()
    assert unpinned == note, (
        "requirements.txt says %r but _ps_run's docstring says unpinned=%s — one of "
        "them is now wrong" % (line.strip(), note))


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
