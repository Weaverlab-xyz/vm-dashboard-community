"""The four cloud deploy pages build their shared blocks from one partial.

The PRA opt-in, its Asset group / Gateway / credential overrides, the batch PRA
opt-in and the workstream's post-deploy runs were once copied into the AWS, Azure,
GCP and OCI pages, and the copies drifted: three select styles, GCP and OCI's
summary offering a credential they had no field for, and the AWS batch form showing
its PRA options box with PRA off. They now live in
``web_dashboard/templates/partials/deploy_options.html``. This file pins:

  * every cloud page imports the partial and calls each macro, the PRA ones inside
    ``{% if pra_enabled %}``,
  * no page carries its own copy of the markup again,
  * a page shows the PRA credential picker exactly when its deploy request sends
    ``pra_credential_ref``,
  * the macros render with the form name they are given.

Templates are read as text and the partial rendered with bare Jinja; no app, no DB.

Run: python tests/test_cloud_deploy_partials.py   (or under pytest)
"""
import os
import re
import sys

import jinja2

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")
_PARTIAL = "partials/deploy_options.html"

# cloud -> (single form object, batch form object)
_PAGES = {
    "aws": ("deployForm", "deployForm"),   # AWS's batch form shares deployForm
    "azure": ("deployModal", "bulkModal"),
    "gcp": ("deployForm", "bulkModal"),
    "oci": ("deployForm", "bulkModal"),
}

# Text only the partial may carry; a page holding it has a copy again.
_OWNED = ("Add to BeyondTrust PRA", "Add each VM to BeyondTrust PRA",
          "Runs after deploy:", "BeyondTrust PRA options")


def _page(cloud):
    with open(os.path.join(_TEMPLATES, cloud, "index.html"), encoding="utf-8") as fh:
        return fh.read()


def _guarded(src, i):
    """Whether position i sits inside an open `{% if pra_enabled %}`."""
    return src.rfind("{% if pra_enabled %}", 0, i) > src.rfind("{% endif %}", 0, i)


def test_every_page_imports_the_partial():
    for cloud in _PAGES:
        assert re.search(r'\{%\s*from\s+"' + re.escape(_PARTIAL) + r'"\s+import', _page(cloud)), (
            f"{cloud}: does not import {_PARTIAL}")


def test_every_page_calls_each_macro_once_with_its_own_form():
    for cloud, (single, batch) in _PAGES.items():
        src = _page(cloud)
        for call in (f'{{{{ pra_opt_in("{single}"', f'{{{{ post_deploy_runs("{single}"',
                     f'{{{{ bulk_pra_opt_in("{batch}")'):
            assert src.count(call) == 1, f"{cloud}: expected one {call} … }}}}"


def test_the_pra_blocks_render_only_while_pra_is_enabled():
    for cloud, (single, batch) in _PAGES.items():
        src = _page(cloud)
        for call in (f'{{{{ pra_opt_in("{single}"', f'{{{{ bulk_pra_opt_in("{batch}")'):
            assert _guarded(src, src.index(call)), f"{cloud}: {call} is outside pra_enabled"
        for m in re.finditer(r"\{\{ pra_overrides\(", src):
            assert _guarded(src, m.start()), f"{cloud}: pra_overrides is outside pra_enabled"


def test_no_page_keeps_its_own_copy():
    for cloud in _PAGES:
        src = _page(cloud)
        for text in _OWNED:
            assert text not in src, (
                f"{cloud}: carries {text!r} itself — use {_PARTIAL} so the four pages "
                "cannot drift apart again")


def test_the_credential_picker_follows_what_the_request_sends():
    """Offering a PRA credential the deploy never sends is a field that does nothing;
    sending one with no way to pick it is dead state. Either way the two must agree."""
    for cloud, (single, _) in _PAGES.items():
        src = _page(cloud)
        call = re.search(r'\{\{ pra_opt_in\("' + single + r'"([^}]*)\}\}', src).group(1)
        offered = "credential=False" not in call
        sent = "pra_credential_ref:" in src and "pra_credential_ref ||" in src
        assert offered == sent, (
            f"{cloud}: the form {'offers' if offered else 'hides'} the PRA credential "
            f"picker but the request {'sends' if sent else 'does not send'} pra_credential_ref")
        if offered:
            # Without these the picker renders with an empty backend list.
            for needed in ("...secretPickerState()", "this.loadSecretBackends()",
                           "praCredBackend: ''"):
                assert needed in src, f"{cloud}: shows the PRA credential picker but lacks {needed}"


def _render(call):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES))
    tpl = env.from_string(
        '{% from "' + _PARTIAL + '" import pra_opt_in, bulk_pra_opt_in, pra_overrides, '
        'post_deploy_runs %}' + call)
    return tpl.render()


def test_the_macros_bind_the_form_they_are_given():
    html = _render('{{ pra_opt_in("myForm") }}')
    for field in ("enable_pra", "jump_group", "jumpoint_name", "praCredBackend",
                  "pra_credential_ref"):
        assert f'"myForm.{field}' in html, field
    assert ">Gateway</label>" in html and "and credential" in html

    html = _render('{{ pra_opt_in("myForm", credential=False) }}')
    assert "pra_credential_ref" not in html and "and credential" not in html

    assert 'x-model="b.enable_pra"' in _render('{{ bulk_pra_opt_in("b") }}')
    html = _render('{{ post_deploy_runs("f", class="mb-4") }}')
    assert 'x-model="f.skip_post_deploy"' in html and "px-3 py-2 mb-4" in html


def test_the_shared_overrides_box_can_follow_the_opt_in():
    html = _render('{{ pra_overrides("f", note="shared", show="f.enable_pra") }}')
    assert 'x-show="f.enable_pra"' in html and "— shared;" in html
    assert "pra_credential_ref" not in html


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
