"""Every POV setup step names a guide, and the guide resolves.

Two holes meet here, and between them a dead link on the ladder is invisible:

``tests/test_app_docs_links.py`` walks ``web_dashboard/templates/`` for ``href="/docs/…"``
*literals*. The ladder's link is ``:href="s.docs"`` — an Alpine binding whose value is built
in Python — so that sweep cannot see it. ``tests/test_docs_anchors.py``'s bare-reference
sweep only matches paths ending ``.md``, and these deliberately do not carry the suffix
(``doc_page`` appends its own; a link that already has one 404s). So the paths in
``pov_setup_steps._STEP_DOCS`` are checked by nothing else in the suite.

The failure they would otherwise produce is the quiet kind: a presenter who is stuck on a
step clicks the one thing on the row offering to explain it and lands on a 404, or — worse,
because it looks like it worked — at the top of the right page with the fragment silently
dropped. Ten of the twelve paths carry a fragment, so the second is the likelier half.

``test_the_walkthrough_lists_every_step_in_order`` is here for a different reason. The docs
said the setup was nine steps for as long as it has had twelve: ``use_cases``,
``rb_installer`` and ``ot_adapter`` were added to ``STEPS`` and the page a first-time
presenter is pointed at went on describing the ladder they would not see. Nothing caught it
because nothing tied the prose to the tuple. This does, in the one place that still
enumerates them.

Runs under pytest, or standalone: python tests/test_pov_setup_step_docs.py
"""
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DOCS = os.path.join(_ROOT, "docs")
_WALKTHROUGH = os.path.join(_DOCS, "profiles", "pov", "first-pov.md")
_DETAIL = os.path.join(_ROOT, "web_dashboard", "templates", "pov", "detail.html")

# Loaded by path, like tests/test_pov_setup_steps.py: the module imports nothing, so this
# needs no app, no database and no stubs.
_SRC = os.path.join(_ROOT, "web_dashboard", "services", "pov_setup_steps.py")
_spec = importlib.util.spec_from_file_location("pov_setup_steps_docs_under_test", _SRC)
steps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(steps)

# Only the third-party half is probed. `web_dashboard.api.docs_pages` is first-party and is
# imported unguarded on purpose — a broad `except Exception` here would swallow a real
# breakage in it and report this file as "renderer unavailable". See
# tests/test_import_guard_narrowness.py.
try:
    import markdown  # noqa: F401  (presence check; the renderer imports it lazily)
    from web_dashboard.api.docs_pages import _render_markdown
except ModuleNotFoundError as exc:  # markdown / fastapi absent outside CI
    _render_markdown = None
    _IMPORT_ERR = exc

_ID = re.compile(r'id="([^"]+)"')
# A row of the walkthrough's step table: "| 7 | Resource Broker | … |".
_NUMBERED_ROW = re.compile(r"^\|\s*(\d+)\s*\|", re.M)


def _resolve(page):
    """The file ``doc_page`` would serve for ``/docs/<page>``, or None for a 404.

    Mirrors the route's folder fallback, which is what lets a value name a directory the
    way ``share`` would if it ever pointed at ``customer-access`` rather than a page in it.
    """
    for candidate in (os.path.join(_DOCS, f"{page}.md"),
                      os.path.join(_DOCS, page, "README.md")):
        if os.path.isfile(candidate):
            return candidate
    return None


def _rendered_ids(page):
    """Anchor ids for a page as the dashboard actually renders it.

    Rendered rather than derived: ``docs_pages`` wires a GitHub-compatible slugger into the
    renderer, and re-implementing it here would be a second spelling of the rule that could
    agree with itself while disagreeing with the page a reader lands on.
    """
    with open(_resolve(page), encoding="utf-8") as fh:
        return set(_ID.findall(_render_markdown(fh.read())))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_every_step_names_a_guide():
    """A step with no guide renders a row that explains itself to nobody.

    Same shape as the NEEDS check in tests/test_pov_setup_steps.py: the map is keyed by
    step, so a step added later fails here rather than quietly serving "".
    """
    assert set(steps._STEP_DOCS) == set(steps.STEP_KEYS), (
        "_STEP_DOCS has drifted from STEPS: "
        f"missing {sorted(set(steps.STEP_KEYS) - set(steps._STEP_DOCS))}, "
        f"unknown {sorted(set(steps._STEP_DOCS) - set(steps.STEP_KEYS))}")
    for key, value in steps._STEP_DOCS.items():
        assert value and value.strip(), f"{key} names an empty guide"
        assert not value.startswith("/docs/"), (
            f"{key}: store the bare path; describe() adds the /docs/ prefix")
        assert not value.partition("#")[0].endswith(".md"), (
            f"{key}: doc_page appends .md itself, so a path carrying one 404s")


def test_every_step_guide_resolves_to_a_page():
    broken = sorted(f"{key} -> {value}" for key, value in steps._STEP_DOCS.items()
                    if _resolve(value.partition("#")[0]) is None)
    assert not broken, "setup steps pointing at a page that does not exist:\n" + "\n".join(broken)


def test_every_step_guide_fragment_lands_on_a_real_heading():
    """The half that catches a heading reword, which breaks nothing visibly."""
    assert _render_markdown is not None, (
        f"the docs renderer is unavailable, so fragments cannot be checked ({_IMPORT_ERR})")
    ids = {}
    broken = []
    for key, value in steps._STEP_DOCS.items():
        page, _, frag = value.partition("#")
        if not frag:
            continue
        if page not in ids:
            ids[page] = _rendered_ids(page)
        if frag not in ids[page]:
            broken.append(f"{key} -> {value}")
    assert not broken, (
        "setup steps pointing at a heading that is not on the page:\n" + "\n".join(broken))


def test_the_ladder_payload_carries_the_guide_url():
    """`describe` is what the three ladder surfaces read, so the prefix belongs to it."""
    result = steps.describe({"status": "active", "vm_count": 2, "wired_count": 2})
    assert result["steps"], "the ladder served no steps"
    for step in result["steps"]:
        assert step["docs"].startswith("/docs/"), step
        assert not step["docs"].endswith(".md"), step


def test_the_setup_tab_renders_the_guide_link():
    """Without this the map can resolve perfectly while nothing puts it on the page."""
    body = _read(_DETAIL)
    ladder = body.partition("env.setup.steps")[2]
    assert ladder, "could not find the setup ladder's loop in pov/detail.html"
    assert ':href="s.docs"' in ladder, (
        "the Setup tab's step rows no longer render the per-step guide link")


def test_the_walkthrough_lists_every_step_in_order():
    """The page a first-time presenter is sent to must name the real ladder.

    Asserted positively — each label, in order — rather than by forbidding a stale phrase.
    A sweep that bans a string has to contain it, which is how such a test trips over
    itself; and a ban would not have caught this anyway, because the page was not wrong
    about a word, it was missing three steps.
    """
    body = _read(_WALKTHROUGH)
    cursor = 0
    for key, label, _fn in steps.STEPS:
        found = body.find(label, cursor)
        assert found != -1, (
            f"{key}: the walkthrough does not list {label!r} after the step before it. "
            f"docs/profiles/pov/first-pov.md is the one place that enumerates the ladder.")
        cursor = found + len(label)
    rows = _NUMBERED_ROW.findall(body)
    assert len(rows) == len(steps.STEPS), (
        f"the walkthrough's step table has {len(rows)} numbered rows, "
        f"STEPS has {len(steps.STEPS)}")
    assert rows == [str(n + 1) for n in range(len(steps.STEPS))], (
        f"the walkthrough's step table is misnumbered: {rows}")


if __name__ == "__main__":
    if _render_markdown is None:
        print(f"SKIP all: renderer unavailable ({_IMPORT_ERR})")
        sys.exit(0)
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
