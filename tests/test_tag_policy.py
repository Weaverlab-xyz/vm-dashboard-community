"""Tags, labels, attributes and categories: one shape, and who is allowed to change them.

Four properties matter here, roughly in order of how badly it would hurt to lose them.

1. **A protected key stays protected.** ``assert_editable`` is the only thing standing
   between a tag editor and the keys ``/costs``, RBAC visibility, POV teardown and the
   VDI pool all select on. ``test_every_load_bearing_key_the_estate_writes_is_protected``
   scans the modules that own those keys and fails if one grows a key this module has
   never heard of — the failure mode being a plausible-looking editor that silently
   lets somebody orphan a running resource from its teardown.
2. **Every native shape flattens.** Five producers hand over four different shapes. A
   shape this module does not recognise renders an empty chip list, which on a page
   looks exactly like "this VM has no tags" — no error, nothing in a log.
3. **A tag with no value is not a tag with an empty value.** Proxmox tags and vSphere
   categories are bare labels. Rendering ``prod=`` for one invents a value.
4. **Colour is stable.** ``tone_of`` must not use Python's randomised string hash, or
   one gunicorn worker serves a different palette from the next and a tag changes
   colour on refresh.

Pure, stdlib only, loaded by file path — no app import, no cloud SDK, no database.
Runs under pytest, or standalone:  python tests/test_tag_policy.py
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")
_PATH = os.path.join(_SERVICES, "tag_policy.py")
_spec = importlib.util.spec_from_file_location("tag_policy", _PATH)
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


# ── shapes ───────────────────────────────────────────────────────────────────

def test_a_dict_is_the_aws_azure_gcp_oci_shape():
    out = tp.normalise({"env": "prod"}, "aws")
    assert [(c["key"], c["value"]) for c in out] == [("env", "prod")]


def test_a_semicolon_string_is_the_proxmox_shape():
    out = tp.normalise("prod;web", "proxmox")
    assert [c["key"] for c in out] == ["prod", "web"]


def test_a_list_is_the_synced_hypervisor_cache_shape():
    out = tp.normalise(["dc", "core"], "vsphere")
    assert [c["key"] for c in out] == ["core", "dc"]


def test_a_boto3_key_value_list_flattens_too():
    out = tp.normalise([{"Key": "env", "Value": "prod"}], "aws")
    assert [(c["key"], c["value"]) for c in out] == [("env", "prod")]


def test_every_empty_shape_is_an_empty_list_not_an_error():
    for raw in (None, "", {}, [], 0, ";;", ["", None]):
        assert tp.normalise(raw, "aws") == [], repr(raw)


def test_an_unrecognised_shape_is_empty_rather_than_a_crash():
    """A page that renders no chips is recoverable; a 500 on the VM list is not."""
    assert tp.normalise(object(), "aws") == []


def test_a_valueless_tag_keeps_a_null_value_not_an_empty_string():
    """`prod=` would claim the hypervisor stored an empty value. It stored none."""
    assert tp.normalise("prod", "proxmox")[0]["value"] is None
    assert tp.normalise({"prod": ""}, "aws")[0]["value"] == ""


def test_a_non_string_value_survives_as_text():
    assert tp.normalise({"replicas": 3}, "aws")[0]["value"] == "3"


def test_two_spellings_of_one_key_render_once():
    """OCI freeform tags are case-sensitive, so both CAN be present. Rendering both
    would show two chips suggesting two different facts."""
    out = tp.normalise({"managed-by": "vm-dashboard", "Managed-By": "something"}, "oci")
    assert len(out) == 1


# ── classification ───────────────────────────────────────────────────────────

def test_all_three_spellings_of_managed_by_are_system():
    for key in ("managed-by", "ManagedBy", "managed_by", "MANAGED-BY"):
        assert tp.classify(key) == tp.CLASS_SYSTEM, key


def test_workgroup_is_identity_in_either_case():
    assert tp.classify("workgroup") == tp.CLASS_IDENTITY
    assert tp.classify("Workgroup") == tp.CLASS_IDENTITY


def test_an_operators_own_key_is_user():
    for key in ("env", "owner", "cost-center", "ticket"):
        assert tp.classify(key) == tp.CLASS_USER, key


def test_system_and_identity_chips_carry_no_tone():
    for key in ("managed-by", "workgroup"):
        assert tp.tone_of(key) is None, key


def test_the_pov_teardown_selector_is_system():
    assert tp.classify("povEnvironment") == tp.CLASS_SYSTEM


# ── colour ───────────────────────────────────────────────────────────────────

def test_a_tone_is_stable_and_in_range():
    for key in ("env", "owner", "team", "ticket", "cost-center"):
        first = tp.tone_of(key)
        assert first == tp.tone_of(key), key
        assert 0 <= first < tp.TONE_COUNT, key


def test_tone_does_not_use_pythons_randomised_string_hash():
    """The built-in hash() is salted per process (PYTHONHASHSEED), so a two-worker
    deploy would serve two different palettes and a tag would change colour on refresh.

    Pinned against the source because a single process cannot observe its own salt
    changing. Read as an AST rather than as text: the function's own docstring names
    the builtin in prose, and a substring scan trips over that — the same self-match
    tests/test_managed_by_tag_values.py is careful about.
    """
    with open(_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "tone_of")
    called = {c.func.id for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "hash" not in called, "tone_of must not call the built-in hash()"


def test_the_palette_matches_the_javascript_lookup_table():
    """TONE_COUNT indexes into TAG_TONES in static/js/app.js. If the two drift, a tone
    past the end of the table renders an unstyled chip."""
    app_js = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")
    with open(app_js, encoding="utf-8") as fh:
        src = fh.read()
    block = src[src.index("window.TAG_TONES"):]
    block = block[:block.index("];")]
    assert block.count("bg-") == tp.TONE_COUNT, (
        f"TAG_TONES has {block.count('bg-')} entries, TONE_COUNT is {tp.TONE_COUNT}")


def test_no_tone_borrows_a_colour_that_already_means_a_state():
    """Blue, green, red, yellow, amber and gray are job/power/expiry state across this
    app; indigo is the workgroup pill. A tag in any of them reads as a status."""
    app_js = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")
    with open(app_js, encoding="utf-8") as fh:
        src = fh.read()
    block = src[src.index("window.TAG_TONES"):]
    block = block[:block.index("];")]
    for reserved in ("blue", "green", "red", "yellow", "amber", "gray", "indigo"):
        assert f"bg-{reserved}-" not in block, f"TAG_TONES borrows bg-{reserved}-*"


# ── ordering ─────────────────────────────────────────────────────────────────

def test_chips_sort_system_then_identity_then_user_alphabetically():
    out = tp.normalise({"zebra": "1", "env": "2", "workgroup": "t", "managed-by": "d"},
                       "aws")
    assert [c["key"] for c in out] == ["managed-by", "workgroup", "env", "zebra"]


def test_the_order_is_stable_across_calls():
    """The cloud pages poll every 30s. Chips reshuffling between polls would make a
    table impossible to read."""
    raw = {"b": "1", "a": "2", "managed-by": "d", "c": "3"}
    assert tp.normalise(raw, "aws") == tp.normalise(raw, "aws")


# ── vocabulary ───────────────────────────────────────────────────────────────

def test_each_platform_is_named_by_the_word_it_uses():
    assert tp.noun("gcp") == "GCP label"
    assert tp.noun("oci") == "OCI freeform tag"
    assert tp.noun("passwordsafe") == "Password Safe attribute"
    assert tp.noun("nutanix") == "Nutanix category"


def test_an_unknown_source_falls_back_to_the_generic_word():
    assert tp.noun("") == "tag"
    assert tp.noun("something-new") == "tag"


def test_a_system_chips_tooltip_says_why_it_is_locked():
    chip = tp.normalise({"managed-by": "vm-dashboard"}, "aws")[0]
    assert chip["title"].startswith("AWS tag — ")
    assert "/costs" in chip["title"]


def test_a_user_chips_tooltip_is_just_the_noun():
    assert tp.normalise({"env": "prod"}, "gcp")[0]["title"] == "GCP label"


# ── the guard ────────────────────────────────────────────────────────────────

def test_a_free_form_key_is_editable():
    tp.assert_editable(["env", "owner", "cost-center"])   # must not raise


def test_every_protected_key_is_refused():
    for key in tp.PROTECTED_KEYS:
        try:
            tp.assert_editable([key])
        except tp.TagPolicyError:
            continue
        raise AssertionError(f"{key} was accepted")


def test_the_refusal_names_the_key_and_the_reason():
    """An operator who is refused here goes and does it by hand in the cloud console
    instead. The message is the only chance to tell them what that breaks."""
    try:
        tp.assert_editable(["workgroup"])
    except tp.TagPolicyError as exc:
        assert "workgroup" in str(exc)
        assert "who can see" in str(exc)
    else:
        raise AssertionError("workgroup was accepted")


def test_the_guard_is_case_insensitive():
    """AWS and Azure tag keys are case-sensitive, so `Managed-By` is a DIFFERENT key
    that this dashboard's own readers (unmanaged_vms, cost_service) would still match."""
    for key in ("Workgroup", "MANAGED-BY", "PovEnvironment", "Name"):
        try:
            tp.assert_editable([key])
        except tp.TagPolicyError:
            continue
        raise AssertionError(f"{key} was accepted")


def test_one_bad_key_refuses_the_whole_batch():
    """A partial write would leave the operator's own tags applied and the refusal
    reported, which reads as a total failure and is not one."""
    try:
        tp.assert_editable(["env", "managed-by"])
    except tp.TagPolicyError:
        return
    raise AssertionError("a batch containing managed-by was accepted")


def test_an_empty_batch_is_allowed():
    tp.assert_editable([])
    tp.assert_editable(None)


# ── the coverage scan ────────────────────────────────────────────────────────

# Modules that write a tag this dashboard later SELECTS on, and the constant in each
# that holds the key. Pure text inspection, so this needs no cloud SDK.
_OWNERS = {
    "unmanaged_vms.py": ("MANAGED_TAGS", "WORKGROUP_TAG_KEYS"),
    "pov_cloud_env.py": ("TAG_ENVIRONMENT", "TAG_MANAGED_BY", "TAG_ROLE"),
    "vdesktop_service.py": ("POOL_TAG",),
    "ephemeral_secrets.py": ("TAG_KEY",),
    "cost_service.py": ("_MANAGED_TAG_KEY",),
}

# A quoted string on the same line as one of those constants' assignments.
_ASSIGN = r'^\s*{name}\s*=\s*(.+)$'
_QUOTED = re.compile(r'''["']([^"']+)["']''')


def _keys_written_by(filename: str, names) -> set:
    with open(os.path.join(_SERVICES, filename), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    found = set()
    for name in names:
        pat = re.compile(_ASSIGN.format(name=re.escape(name)))
        for i, line in enumerate(lines):
            m = pat.match(line)
            if not m:
                continue
            # Take the assignment plus the few lines after it, so a tuple or dict
            # spread over several lines is caught whole.
            blob = "\n".join(lines[i:i + 8])
            # Stop at the first blank line — the next statement is not ours.
            blob = blob.split("\n\n")[0]
            found.update(_QUOTED.findall(blob))
    return found


# Values, not keys — these appear alongside the keys in the same tuples and must not be
# mistaken for keys the editor should protect.
_NOT_KEYS = {
    "vm-dashboard", "vm-cli-dashboard", "dashboard-sandbox",
    "ansible-managed-account", "povenv-",
}


def test_every_load_bearing_key_the_estate_writes_is_protected():
    """If a module grows a new selector key, this fails until tag_policy learns it.

    Without this, the editor keeps working and simply stops protecting the new key —
    a silent regression in exactly the guard that is the point of the module.
    """
    protected = {k.lower() for k in tp.PROTECTED_KEYS}
    missing = []
    for filename, names in _OWNERS.items():
        for literal in _keys_written_by(filename, names):
            if literal in _NOT_KEYS or literal.lower() in protected:
                continue
            missing.append(f"{filename}: {literal}")
    assert not missing, (
        "load-bearing tag keys this module does not protect:\n  " +
        "\n  ".join(sorted(missing)))


def test_the_scan_actually_finds_something():
    """A regex that silently matches nothing would make the test above pass forever."""
    found = _keys_written_by("unmanaged_vms.py", ("MANAGED_TAGS", "WORKGROUP_TAG_KEYS"))
    assert "managed-by" in found and "workgroup" in found, found


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
