"""BeyondTrust renamed Jumpoint to Gateway; this pins how far that rename goes.

The user-facing half is done: labels, docs and job progress messages say Gateway. The
other half deliberately still says jumpoint, and that is the part worth a test — the
obvious next contribution is "finish the rename", and finishing it would break things
that are not ours to rename:

  * **the vendor's own surface.** `sra_jumpoint_list` and `jumpoint_id` are the
    BeyondTrust `sra` Terraform provider's schema, and `/api/config/v1/jumpoint` is
    their Config API path. Renaming those produces HCL that doesn't plan and a call
    that 404s.
  * **persisted config keys.** `bt_jumpoint_name`, `gcp_jumpoint_zone` and friends are
    rows in `app_config` and lines in operators' `.env` files. Renaming the constant
    silently stops reading the value that is already there — the failure is a blank
    setting, not an error.
  * **string data.** Dict keys and job metadata fields carrying `jumpoint` are read by
    name elsewhere. The Python rename only touched comments and *multi-word* strings
    for exactly this reason: a string with no spaces is an identifier in disguise.

So the rule is not "no more jumpoints". It is: prose says Gateway, identifiers keep
whatever BeyondTrust and the database already call them.

Run: python tests/test_gateway_terminology.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A standalone word — not glued to identifier, path or hostname characters. This is
# the same rule the rename used, so the two cannot drift.
WORD = re.compile(r"(?<![A-Za-z0-9_\-/.])[Jj]umpoints?(?![A-Za-z0-9_\-/])")

_TEXT_EXT = (".html", ".md", ".ps1", ".sh", ".yml", ".yaml", ".txt")


def _walk(root, exts=_TEXT_EXT):
    for dp, _, files in os.walk(os.path.join(_ROOT, root)):
        for f in sorted(files):
            if f.endswith(exts):
                yield os.path.join(dp, f)


def _read(path):
    return open(path, encoding="utf-8").read()


# ── the prose half is done ────────────────────────────────────────────────────

def test_no_user_facing_prose_still_says_jumpoint():
    offenders = []
    for root in ("web_dashboard/templates", "docs", "provisioners"):
        for p in _walk(root):
            for m in WORD.finditer(_read(p)):
                text = _read(p)
                ls = text.rfind("\n", 0, m.start()) + 1
                offenders.append(f"{os.path.relpath(p, _ROOT)}: {text[ls:m.end()+40].strip()[:90]}")
    assert not offenders, (
        "user-facing text still says Jumpoint:\n  " + "\n  ".join(offenders[:20]))


def test_job_progress_messages_say_gateway():
    """These are what an operator watches a deploy through."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py"))
    assert "Ensuring the shared BeyondTrust Gateway host" in src
    assert "Jumpoint host…" not in src


# ── the identifier half must NOT be renamed ───────────────────────────────────

def test_the_vendor_terraform_schema_is_untouched():
    """`sra_jumpoint_list` and `jumpoint_id` are the sra provider's field names. A
    rename here produces HCL that fails to plan, and nothing in this repo would catch
    it before a real provision."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services", "terraform_pra_service.py"))
    assert "sra_jumpoint_list" in src, "the sra provider data source was renamed"
    assert "jumpoint_id" in src, "the sra provider's jumpoint_id argument was renamed"


def test_the_pra_config_api_path_is_untouched():
    src = _read(os.path.join(_ROOT, "web_dashboard", "services", "pra_api_service.py"))
    assert "/api/config/v1/jumpoint" in src, (
        "the PRA Config API path was renamed — BeyondTrust still serves it at "
        "/jumpoint, so this would 404")


def test_persisted_config_keys_are_untouched():
    """Renaming one of these reads a key that was never written: the setting goes
    blank, and nothing raises."""
    keys = ("bt_jumpoint_name", "bt_ecs_jumpoint_subnet_id",
            "bt_ecs_jumpoint_security_group_id", "azure_jumpoint_subnet_id",
            "azure_jumpoint_vm_size", "gcp_jumpoint_name", "gcp_jumpoint_zone",
            "gcp_jumpoint_subnetwork", "rancher_ui_jumpoint_cloud",
            "portainer_ui_jumpoint_egress_ip")
    missing = []
    for key in keys:
        found = any(key in _read(p)
                    for p in _walk("web_dashboard", exts=(".py", ".html")))
        if not found:
            missing.append(key)
    assert not missing, f"persisted config keys were renamed: {missing}"


def test_the_managed_resource_names_are_untouched():
    """These name things that already exist in operators' clouds — an ECS cluster, a
    security group, a VM. Renaming the string orphans the resource."""
    for literal, where in (("bt-jumpoint", "docs"),
                           ("clouddb-jumpoint", "docs"),
                           ("dashboard-sandbox-jumpoint-sg", "docs")):
        found = any(literal in _read(p) for p in _walk(where))
        assert found, f"{literal!r} disappeared from {where} — it names a live resource"


def test_the_python_rename_left_code_tokens_alone():
    """The rename touched comments and multi-word strings only. A single-word string
    is a dict key or a config key, and renaming it changes data."""
    vd = _read(os.path.join(_ROOT, "web_dashboard", "services", "vdesktop_service.py"))
    assert '"jumpoint"' in vd, (
        "a single-word string key was renamed — whatever reads this dict still asks "
        "for 'jumpoint'")
    gcp = _read(os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py"))
    assert "_JumpointRef" in gcp, "a type name was renamed by the prose pass"
    assert "ensure_jumpoint_host" in gcp, "a function name was renamed by the prose pass"


def test_the_rename_rule_would_have_protected_these():
    """The regex above is the rename's own rule; prove it skips the shapes that must
    survive, so the two can't drift apart."""
    for protected in ("bt_jumpoint_name", "sra_jumpoint_list", "/api/config/v1/jumpoint",
                      "bt-jumpoint", "clouddb-shared-jumpoint", "gce-jumpoints",
                      "jumpoint_host_service", "jumpoint-subnet"):
        assert not WORD.search(protected), (
            f"the rename rule would have rewritten {protected!r}")
    for prose in ("the Jumpoint container", "one Jumpoint for all VMs",
                  "through a Jumpoint.", "[ACI Jumpoint]", "Jumpoints"):
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
