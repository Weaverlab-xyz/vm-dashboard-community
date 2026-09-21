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

**This file had two blind spots of its own**, closed after a sweep for the certificate
lab went looking for an invented call and found instead that most of the tree was simply
unread. Both were the same mistake — assuming the shape the original bug happened to take
was the only shape it could take:

  * **One module.** It scanned ``secrets_backend_service`` alone, because that is where
    the six stale calls were. ``btapi_service`` has a *second* ``_ps_run`` funnel with
    nine calls under four more services, checked by nothing. See ``_MODULES``.
  * **One call shape.** It matched ``_ps_run(...)`` and nothing else, so a call that
    skips the funnel was invisible even in a scanned module — and
    ``btapi_service._get_ps_secret_sync`` is exactly that, a ``subprocess.run`` built
    around ``settings.pscli_executable``. See ``_direct_cli_argvs``.

Nothing was wrong in either place; that is the point. The guard was reporting on a tenth
of the surface and reading as though it covered all of it.

Static and stdlib-only — no app imports, no ps-cli, no tenant.
Runs under pytest or standalone:  python tests/test_pscli_grammar.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

def _svc(name):
    return os.path.join(_ROOT, "web_dashboard", "services", name)


# `_read()` defaults to this one: it owns the `_ps_run` docstring the unpinned-note test
# reads, and it is where the six stale calls lived.
_MODULE = _svc("secrets_backend_service.py")

# Every module that builds a ps-cli argv. There are TWO `_ps_run` funnels, not one —
# `btapi_service` has its own, and for a long time nothing checked it: this file scanned
# `secrets_backend_service` alone while nine calls next door (managed-systems,
# managed-accounts, requests, credentials, raw) went unread. Adding a third funnel means
# adding it here.
_MODULES = (_MODULE, _svc("btapi_service.py"))

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
    # Used by btapi_service. Same source: `ps-cli <service> -h` (0.13.0).
    "managed-systems": {
        "create-managed-system-by-asset", "create-by-asset",
        "create-managed-system-by-database-id", "create-by-database-id",
        "create-managed-system-by-workgroup", "create-by-workgroup-id",
        "delete-managed-system-by-id", "delete-by-id", "delete",
        "get-managed-system-by-asset", "get-by-asset",
        "get-managed-system-by-database-id", "get-by-database-id",
        "get-managed-system-by-functional-account-id", "get-by-functional-account-id",
        "get-managed-system-by-id", "get-by-id",
        "get-managed-system-by-workgroup-id", "get-by-workgroup-id",
        "list-managed-systems", "list", "list-systems",
        "update-managed-system-by-id", "update-by-id",
    },
    "managed-accounts": {
        "assign-attribute", "add-attribute", "change-credentials",
        "create-managed-account", "create", "delete-all-attributes",
        "delete-attribute", "delete-managed-account", "delete", "force-reset",
        "get-managed-account", "get", "list-accounts", "list-managed-accounts", "list",
        "list-managed-accounts-by-quick-rule", "list-by-quick-rule", "list-by-qr",
        "list-managed-accounts-by-smart-rule", "list-by-smart-rule", "list-by-sr",
        "test-credentials", "update-credentials", "update-managed-account", "update",
    },
    "requests": {
        "create-request", "create", "create-request-alias", "create-by-alias",
        "create-request-set", "create-request-sets", "get-request-set",
        "get-request-sets", "list-requests", "list", "put-request-approve",
        "approve-request", "put-request-checkin", "checkin-request", "put-request-deny",
        "deny", "request-rotate-on-checkin", "rotate-on-checkin",
        "terminate-user-request", "termination-by-user",
        "termination-managed-account-id", "termination-by-ma-id",
        "termination-managed-system-id", "termination-by-ms-id",
    },
    "credentials": {
        "get-credential-by-alias-id", "get-by-alias-id",
        "get-credential-by-managed-account-id", "get-by-managed-account-id",
        "get-credential-by-request-id", "get-by-request-id",
    },
    # `raw` escapes the grammar on purpose — its "verb" is an HTTP method and the rest is
    # a REST path, so it is the one service where argv shape says nothing about validity.
    "raw": {"GET", "POST", "PUT", "DELETE"},
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


# Global flags that sit BEFORE the service and swallow the token after them.
_VALUE_FLAGS = {"--format", "-f"}


def _is_cli_executable(elt) -> bool:
    """Whether this argv element is the ps-cli binary itself — either the literal, or
    ``settings.pscli_executable``, which is the same thing spelled indirectly."""
    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
        return elt.value == "ps-cli" or elt.value.endswith("/ps-cli")
    return isinstance(elt, ast.Attribute) and elt.attr == "pscli_executable"


def _direct_cli_argvs(src: str):
    """``(lineno, argv)`` for every argv list that names ps-cli itself, with the
    executable and any global flags stripped so the service lands at ``[0]``.

    **The second blind spot.** The sweep above reads ``_ps_run(...)`` and nothing else,
    so a call that skips the funnel is invisible to it no matter which module it is in —
    and one does: ``btapi_service._get_ps_secret_sync`` builds
    ``[settings.pscli_executable, "--format", "json", "secrets", "get", ...]`` and hands
    it straight to ``subprocess.run``. It is correct today. Nothing would have said so.

    Every ``ast.List`` in the module is considered, not just call arguments, so a
    ``cmd = [...]`` assigned first and run later is caught too. A list that is only the
    funnel prefix (``[exe, "--format", "json"]``, completed by ``+ args`` elsewhere)
    strips to nothing and is skipped — the service it eventually gets is checked as a
    ``_ps_run`` argv instead.
    """
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.List) and node.elts
                and _is_cli_executable(node.elts[0])):
            continue
        argv = [e.value if (isinstance(e, ast.Constant) and isinstance(e.value, str))
                else "<var>" for e in node.elts[1:]]
        i = 0
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in _VALUE_FLAGS else 1
        argv = argv[i:]
        if argv:
            found.append((node.lineno, argv))
    return found


def _all_argvs():
    """Every checkable ps-cli argv in the tree, tagged with where it came from."""
    out = []
    for path in _MODULES:
        src, label = _read(path), os.path.basename(path)
        for ln, argv in _ps_run_argvs(src)[0]:
            out.append((f"{label}:{ln}", argv))
        for ln, argv in _direct_cli_argvs(src):
            out.append((f"{label}:{ln}", argv))
    return out


def test_the_sweep_actually_finds_the_calls():
    """A sweep that silently matches nothing passes forever."""
    found = _all_argvs()
    assert len(found) >= 19, f"expected both modules' ps-cli calls; found {len(found)}"


def test_the_sweep_reaches_past_the_first_module():
    """The first blind spot, asserted rather than trusted: widening ``_MODULES`` is only
    worth anything if every module in it actually yields calls. A path typo would leave
    this file quietly scanning one module again, which is the state it started in."""
    for path in _MODULES:
        src, label = _read(path), os.path.basename(path)
        n = len(_ps_run_argvs(src)[0]) + len(_direct_cli_argvs(src))
        assert n, f"{label} is listed in _MODULES but no ps-cli argv was read from it"


def test_the_direct_subprocess_sweep_finds_the_call_that_skips_the_funnel():
    """The second blind spot. ``btapi_service`` runs one ps-cli call through
    ``subprocess.run`` directly instead of its own ``_ps_run``; if this stops matching,
    either that call was refactored into the funnel (good — delete this) or the detector
    broke (bad — and every direct call silently stopped being checked)."""
    direct = _direct_cli_argvs(_read(_svc("btapi_service.py")))
    assert direct, ("no direct ps-cli subprocess argv found in btapi_service — the "
                    "detector or the call shape changed")
    assert any(argv[:2] == ["secrets", "get"] for _, argv in direct), (
        f"expected the direct `secrets get` call; got {[a for _, a in direct]}")


def test_every_ps_run_argv_starts_with_a_service():
    """The bug this file exists for. ``["create", ...]`` is not a ps-cli command."""
    offenders = [(w, argv) for w, argv in _all_argvs() if argv[0] not in VERBS]
    assert not offenders, (
        "ps-cli argv must start with a service (%s):\n  %s"
        % (", ".join(sorted(VERBS)),
           "\n  ".join(f"{w}: {argv}" for w, argv in offenders)))


def test_every_verb_is_one_the_service_actually_has():
    """Catches the other half: right service, verb that does not exist under it."""
    offenders = []
    for where, argv in _all_argvs():
        service = argv[0]
        if service not in VERBS or not VERBS[service]:
            continue
        if len(argv) < 2:
            offenders.append(f"{where}: {argv} names a service with no verb")
        elif argv[1] not in VERBS[service]:
            offenders.append(f"{where}: {service!r} has no verb {argv[1]!r}")
    assert not offenders, "unknown ps-cli verb:\n  " + "\n  ".join(offenders)


def test_every_ps_run_argv_is_a_readable_literal():
    """An argv assembled from a variable cannot be checked above, so it must not exist
    without someone deciding it should. If one is added deliberately, extend this file to
    understand it rather than deleting the guard."""
    dynamic = []
    for path in _MODULES:
        label = os.path.basename(path)
        dynamic += [f"{label}:{ln}" for ln in _ps_run_argvs(_read(path))[1]]
    assert not dynamic, (
        "ps-cli argv built dynamically at %s — the grammar guard cannot read it"
        % ", ".join(dynamic))


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
