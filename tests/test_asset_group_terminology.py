"""PRA 26.1 renamed Jump Group to Asset Group; this pins how far that rename goes.

Same rule as tests/test_gateway_terminology.py, for the same reasons: **prose says Asset
Group, identifiers keep whatever BeyondTrust and the database already call them.**

  * **the vendor's own surface.** ``/api/config/v1/jump-group`` is the PRA Configuration
    API path and ``sra_jump_group_list`` / ``jump_group_id`` are the ``sra`` Terraform
    provider's schema. Renaming them is a 404 and HCL that does not plan.
  * **persisted names.** ``bt_jump_group_name`` is a row in ``app_config`` and a line in
    operators' ``.env`` files; ``pra_jump_group_name`` / ``pra_jump_group_id`` are columns;
    ``jump_group_name`` is a key inside stored tenant options; ``pov_env_jump_group_move``
    is a job type sitting in the jobs table. Renaming any of them reads a value nobody
    wrote — the failure is a blank, not an error.

The one place the old name is allowed in prose is the parenthetical that tells someone on
an older appliance what to look for ("called a Jump Group before PRA 26.1").

Run: python tests/test_asset_group_terminology.py   (or under pytest)
"""
import os
import re
import sys
from _pov_api_src import pov_api_source

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A standalone two-word phrase — not glued to identifier, path or hostname characters.
# The same rule the rename used, so the two cannot drift.
WORD = re.compile(r"(?<![A-Za-z0-9_\-/.])[Jj]ump [Gg]roups?(?![A-Za-z0-9_\-/])")
# "...a Jump Group before PRA 26.1" — the signpost for older appliances.
_SIGNPOST = re.compile(r"Jump Groups? before PRA 26\.1")

_TEXT_EXT = (".html", ".md", ".js")


def _walk(root, exts=_TEXT_EXT):
    for dp, _, files in os.walk(os.path.join(_ROOT, root)):
        for f in sorted(files):
            if f.endswith(exts):
                yield os.path.join(dp, f)


def _read(path):
    return open(path, encoding="utf-8").read()


def test_no_user_facing_prose_still_says_jump_group():
    offenders = []
    for root in ("web_dashboard/templates", "web_dashboard/static/js", "docs"):
        for p in _walk(root):
            text = _read(p)
            allowed = {m.start() for m in _SIGNPOST.finditer(text)}
            for m in WORD.finditer(text):
                if m.start() in allowed:
                    continue
                ls = text.rfind("\n", 0, m.start()) + 1
                offenders.append(f"{os.path.relpath(p, _ROOT)}: "
                                 f"{text[ls:m.end() + 40].strip()[:90]}")
    assert not offenders, (
        "user-facing text still says Jump Group:\n  " + "\n  ".join(offenders[:20]))


def test_python_messages_say_asset_group():
    """Job logs and refusals are what an SE reads a Wire up through."""
    for rel in ("web_dashboard/services/pov_wireup.py",
                "web_dashboard/services/pov_vendor_access.py",
                "web_dashboard/services/pra_vendor_api.py"):
        src = _read(os.path.join(_ROOT, rel))
        hits = [m.group(0) for m in WORD.finditer(src)]
        assert not hits, f"{rel} still says {hits[0]!r} in prose"


def test_the_vendor_surface_is_untouched():
    vendor = _read(os.path.join(_ROOT, "web_dashboard", "services", "pra_vendor_api.py"))
    assert '"/api/config/v1/jump-group"' in vendor, (
        "the PRA Config API path was renamed — the appliance still serves /jump-group")
    tf = _read(os.path.join(_ROOT, "web_dashboard", "services", "terraform_pra_service.py"))
    assert "sra_jump_group_list" in tf, "the sra provider data source was renamed"


def test_persisted_names_are_untouched():
    """Renaming one of these reads a key that was never written: a blank, not an error."""
    db = _read(os.path.join(_ROOT, "web_dashboard", "database.py"))
    for col in ("pra_jump_group_name", "pra_jump_group_id"):
        assert col in db, f"the {col} column was renamed"
    cfg = _read(os.path.join(_ROOT, "web_dashboard", "config.py"))
    assert "bt_jump_group_name" in cfg, "the bt_jump_group_name setting was renamed"
    tenants = _read(os.path.join(_ROOT, "web_dashboard", "services", "bt_tenant_service.py"))
    assert '"jump_group_name"' in tenants, "the stored tenant option key was renamed"
    wireup = pov_api_source()
    assert '"pov_env_jump_group_move"' in wireup, "a persisted job type was renamed"


def test_the_rename_rule_would_have_protected_these():
    for protected in ("bt_jump_group_name", "sra_jump_group_list", "/api/config/v1/jump-group",
                      "jump-group/move", "pra_jump_group_id", "Web-Jump groups"):
        assert not WORD.search(protected), f"the rename rule would have rewritten {protected!r}"
    for prose in ("the Jump Group", "Jump Groups", "a jump group.", "[Jump group]"):
        assert WORD.search(prose), f"the rename rule would have missed {prose!r}"


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
