"""Wiring tests for the shared collapsible section (templates/partials/collapsible.html).

Storage's backend sections, the Configuration run's "Use a secret" section and the
Settings panels' sub-sections each carried their own copy of the same header button,
status pills and rotating chevron. They now share one macro, and the one rule that
matters most lives there: THE BODY IS x-show, NEVER x-if. A collapsed section's inputs
must stay in the DOM and stay bound, or a form submitted with that section closed
silently drops every field in it -- the Configuration run's secret variables, become
password and SSH key are exactly that case.

Not every collapsible is on the macro, deliberately: the Portainer cards carry action
buttons beside the toggle, /use-cases rows are an x-for with a blurb and a count, and the
inventory, costs and agents expanders are row or menu toggles rather than form sections.

Text-and-render checks, no app imports. Runs under pytest or standalone:
    python tests/test_collapsible_contract.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")
_MIGRATED = ("storage/index.html", "config-mgmt/index.html", "settings.html")

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:  # jinja2 absent -> the render checks skip, the text checks still run
    Environment = None


def _read(rel: str) -> str:
    with open(os.path.join(_TEMPLATES, rel), encoding="utf-8") as fh:
        return fh.read()


def _render(src: str) -> str:
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    return env.from_string('{% from "partials/collapsible.html" import collapsible %}' + src).render()


def test_the_body_is_x_show_never_x_if():
    macro = _read("partials/collapsible.html")
    body = macro.split("{% macro collapsible", 1)[1]
    assert 'x-if' not in body.split("{% endmacro %}")[0], (
        "the collapsible body uses x-if, so a closed section's inputs leave the DOM and a "
        "submitted form drops them")
    if Environment is None:
        print("   (render check skipped: jinja2 not installed)")
        return
    html = _render('{% call collapsible("open", "T") %}<input x-model="f.a">{% endcall %}')
    assert '<div x-show="open" x-cloak>' in html and 'x-model="f.a"' in html, html


def test_the_header_reports_its_state_and_toggles_by_default():
    if Environment is None:
        print("   (skipped: jinja2 not installed)")
        return
    html = _render('{% call collapsible("panelOpen.x", "T") %}{% endcall %}')
    assert '@click="panelOpen.x = !panelOpen.x"' in html, html
    assert ":aria-expanded=\"(panelOpen.x) ? 'true' : 'false'\"" in html, html
    html = _render('{% call collapsible("o === 1", "T", toggle="go(1)") %}{% endcall %}')
    assert '@click="go(1)"' in html, html


def test_a_badge_without_show_is_always_visible():
    if Environment is None:
        print("   (skipped: jinja2 not installed)")
        return
    html = _render('{% call collapsible("o", "T", badges=[{"label": "via agent", "tone": "sky"},'
                   ' {"show": "x.active", "label": "active", "tone": "blue"}]) %}{% endcall %}')
    fixed = re.search(r"<span([^>]*)>via agent</span>", html)
    live = re.search(r"<span([^>]*)>active</span>", html)
    assert fixed and "x-show" not in fixed.group(1) and "text-sky-700" in fixed.group(1), html
    assert live and 'x-show="x.active"' in live.group(1) and "text-blue-700" in live.group(1), html


def test_the_migrated_pages_use_the_macro_and_roll_none_of_their_own():
    failures = []
    for rel in _MIGRATED:
        src = _read(rel)
        if 'import collapsible' not in src or "{% call collapsible(" not in src:
            failures.append(f"{rel}: does not use the collapsible macro")
        # The hand-rolled header's tell: a button binding aria-expanded. Every one of these
        # pages' collapsibles goes through the macro now.
        for m in re.finditer(r":aria-expanded=\"([^\"]*)\"", src):
            line = src.count("\n", 0, m.start()) + 1
            failures.append(f"{rel}:{line} hand-written collapsible header ({m.group(1)[:40]})")
    assert not failures, "\n  ".join([""] + failures)


def test_each_call_closes():
    for rel in _MIGRATED:
        src = _read(rel)
        assert src.count("{% call collapsible(") == src.count("{% endcall %}"), rel


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok   " + name)
