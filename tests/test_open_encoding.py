"""Every text-mode ``open()`` must name its ``encoding``.

``open(path)`` with no ``encoding=`` decodes using the platform default, which is
``cp1252`` on Windows and ``UTF-8`` on Linux. So a test helper that reads a repo file
without saying ``encoding="utf-8"`` is green on CI and red on a developer laptop, for
reasons that have nothing to do with the code under test. Two ways it goes wrong:

  * **Hard failure.** 19 files under ``web_dashboard/`` are valid UTF-8 that ``cp1252``
    cannot decode at all — ``templates/azure/index.html`` and ``templates/gcp/index.html``
    among them. Reading one raises ``UnicodeDecodeError: 'charmap' codec can't decode
    byte 0x90``, which names a byte offset and no filename, and looks like nothing to do
    with the assertion that was actually being made.
  * **Silent corruption**, which is worse. ``web_dashboard/jobs_worker.py`` holds 51
    non-ASCII bytes that ``cp1252`` decodes *successfully* into mojibake. A test that
    greps the result for a string containing one of those characters simply stops
    matching, and reports a substantive-looking failure about the worker's dispatch
    table instead of an encoding problem.

All 27 sites across 12 files in ``tests/`` were fixed; this is the part that stops the
28th. There is no linter in CI, so — as with ``tests/test_no_undefined_names.py`` and
``tests/test_no_redefined_names.py`` — the sweep test IS the guard.

**Why AST and not grep.** A line-based grep for ``open(`` without ``encoding`` reports
false positives here, because several call sites wrap and put the keyword on a
continuation line — see ``tests/test_database_registration.py`` and
``tests/test_portainer_node_service.py``. Only a parse sees the whole call.

Two exclusions, without which the check is wrong rather than merely noisy:

  * **Binary modes.** ``open(p, "rb")`` / ``mode="wb"`` must NOT be asked for an
    encoding; passing one is a ``ValueError``. A mode that is not a literal constant is
    treated as text, deliberately — it cannot be proven binary, so it has to say so.
  * **``os.open``.** A raw file descriptor, which takes no ``encoding`` at any mode.
    Matching ``ast.Attribute`` with ``attr == "open"`` sweeps it up as a false positive;
    there is a real one at ``runners/agent/agent.py:369``. Other attribute opens are in
    scope on purpose: ``Path.open`` takes ``encoding`` and needs it just as much.

**Deliberately out of scope** — these are pre-existing and are NOT failures:

  * Runtime *writes* of generated content under ``web_dashboard/services/`` — Terraform
    HCL, kubeconfigs, Ansible inventories (``terraform.py``, ``ansible_local_service.py``,
    ``ansible_cloud_run_service.py``, ``k8s_service.py``). These emit ASCII the app just
    produced, into a scratch dir; they are not reads of repo files and are not what bit
    us. Excluded structurally rather than by name: the app-side sweep below covers text
    *reads* only.
  * ``web_dashboard/config.py:195``, which reads a JWT secret file the repo does not own,
    named in ``_APP_READ_EXEMPT``.

The boundary is drawn here, in the test, rather than by editing service code to satisfy
a test — changing how the app writes Terraform to keep a linter quiet would be the tail
wagging the dog.

Runs under pytest, or standalone:  python tests/test_open_encoding.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.join(_ROOT, "tests")
_APP = os.path.join(_ROOT, "web_dashboard")

_SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git"}

# Attribute-style openers that are not the text-mode builtin. `os.open` returns a raw
# fd and accepts no `encoding` in any mode, so requiring one of it is simply incorrect.
_NON_TEXT_OPENERS = {"os"}

# Reads of files this repo does not own, where utf-8 is not ours to assert.
_APP_READ_EXEMPT = {"web_dashboard/config.py"}  # :195, the JWT secret file


def _py_files(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _rel(path):
    return os.path.relpath(path, _ROOT).replace(os.sep, "/")


def _opener_name(call):
    """The opener being called, or None if this call is not an ``open`` of any kind.

    Returns ``"open"`` for the bare builtin and ``"<owner>.open"`` for the attribute
    form, so the caller can tell ``os.open`` from ``Path.open``.
    """
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        return "open"
    if isinstance(func, ast.Attribute) and func.attr == "open":
        owner = func.value
        if isinstance(owner, ast.Name):
            return f"{owner.id}.open"
        if isinstance(owner, ast.Attribute):
            return f"{owner.attr}.open"
        return "?.open"
    return None


def _mode_of(call):
    """The literal mode string, or None if absent or not a constant.

    None conflates "no mode given" (so text, the default 'r') with "mode is a
    variable". Both must declare an encoding, so they need not be told apart.
    """
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _is_binary(mode):
    return isinstance(mode, str) and "b" in mode


def _is_text_read(mode):
    """True for a text-mode read — no mode at all, or an explicit 'r'/'rt'."""
    if mode is None:
        return True
    if not isinstance(mode, str) or "b" in mode:
        return False
    return mode.strip() in ("r", "rt", "tr")


def _has_encoding(call):
    return any(kw.arg == "encoding" for kw in call.keywords)


def _open_calls(path):
    """Yield (lineno, opener, mode, has_encoding) for every open-ish call in a file."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        opener = _opener_name(node)
        if opener is None:
            continue
        if opener.rsplit(".", 1)[0] in _NON_TEXT_OPENERS and opener != "open":
            continue
        yield node.lineno, opener, _mode_of(node), _has_encoding(node)


def _sweep(base, only_reads, exempt=frozenset()):
    checked, offenders = 0, []
    for path in _py_files(base):
        rel = _rel(path)
        for lineno, opener, mode, has_enc in _open_calls(path):
            if _is_binary(mode):
                continue                      # encoding= would be a ValueError
            if only_reads and not _is_text_read(mode):
                continue
            if rel in exempt:
                continue
            checked += 1
            if not has_enc:
                offenders.append(
                    f"{rel}:{lineno} {opener}(mode={mode!r}) has no encoding= — it "
                    "decodes as cp1252 on Windows and utf-8 on Linux CI, so this "
                    "either raises UnicodeDecodeError or silently mojibakes, on one "
                    "platform only")
    return checked, offenders


def test_every_open_under_tests_declares_an_encoding():
    """The primary guard, over all of ``tests/`` — reads and writes alike.

    Writes are included because a test that writes cp1252 and reads back utf-8 is the
    same bug pointed the other way, and because a test fixture has no reason to want
    the platform default.
    """
    checked, offenders = _sweep(_TESTS, only_reads=False)
    assert checked >= 60, (
        f"expected to find plenty of open() calls across tests/, saw {checked} — the "
        "walk may have stopped matching rather than started passing")
    assert not offenders, (
        f"{len(offenders)} text-mode open() call(s) with no encoding=:\n  "
        + "\n  ".join(offenders))


def test_every_repo_file_read_in_the_app_declares_an_encoding():
    """The app half, scoped to text *reads* — see the module docstring.

    The floor is low on purpose: the app barely reads repo-owned text files, so this is
    mostly forward-looking. It is what catches the next ``open(template_path)`` added
    under ``web_dashboard/``, which would fail for Windows developers only.
    """
    checked, offenders = _sweep(_APP, only_reads=True, exempt=_APP_READ_EXEMPT)
    assert checked >= 1, (
        f"expected at least one in-scope text read under web_dashboard/, saw {checked} — "
        "the walk or the read/write split may have broken")
    assert not offenders, (
        f"{len(offenders)} text-mode read(s) of a repo file with no encoding=:\n  "
        + "\n  ".join(offenders))


def test_the_repo_really_does_hold_utf8_only_files():
    """Guard the premise. If nothing in the tree were cp1252-hostile, the sweep above
    would be busywork — so assert the hazard it exists for is real, and platform
    independently (decoding cp1252 explicitly, not via the locale default)."""
    hazards = []
    for dirpath, dirnames, filenames in os.walk(_APP):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith((".py", ".html", ".js", ".css")):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, "rb") as fh:
                raw = fh.read()
            try:
                raw.decode("cp1252")
                continue
            except UnicodeDecodeError:
                pass
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:        # pragma: no cover — not utf-8 either
                continue
            hazards.append(_rel(path))
    assert len(hazards) >= 5, (
        "expected several files that are valid utf-8 but undecodable as cp1252 — "
        f"found {len(hazards)}: {sorted(hazards)}")


def test_the_guard_catches_the_shape_it_was_written_for():
    """A check that cannot fail is worse than no check, because it reads as coverage.

    Also pins the two exclusions and the grep-vs-AST distinction, since each of the
    three is a way for this file to quietly stop meaning anything.
    """
    def offenders_in(src):
        tree = ast.parse(src)
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            opener = _opener_name(node)
            if opener is None:
                continue
            if opener.rsplit(".", 1)[0] in _NON_TEXT_OPENERS and opener != "open":
                continue
            mode = _mode_of(node)
            if _is_binary(mode) or _has_encoding(node):
                continue
            out.append((opener, mode))
        return out

    # The bug, in both the shapes it took in tests/.
    assert offenders_in("data = open(path).read()") == [("open", None)]
    assert offenders_in("open(p, 'w').close()") == [("open", "w")]

    # Fixed forms must be quiet — including the wrapped one a grep gets wrong. This is
    # the case that makes the AST non-negotiable.
    assert offenders_in("data = open(path, encoding='utf-8').read()") == []
    assert offenders_in("x = open(os.path.join(a, b),\n"
                        "         encoding='utf-8').read()") == []

    # Binary modes, positional and keyword: encoding= is invalid, so never demand it.
    assert offenders_in("open(p, 'rb')") == []
    assert offenders_in("open(p, mode='wb')") == []

    # os.open takes no encoding at all; Path.open does, and stays in scope.
    assert offenders_in("fd = os.open(t, os.O_WRONLY | os.O_CREAT, 0o600)") == []
    assert offenders_in("p.open(encoding='utf-8')") == []
    assert offenders_in("p.open()") == [("p.open", None)]

    # A non-constant mode cannot be proven binary, so it must still declare one.
    assert offenders_in("open(p, mode)") == [("open", None)]


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
