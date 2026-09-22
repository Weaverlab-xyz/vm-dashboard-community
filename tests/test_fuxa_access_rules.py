"""FUXA access rules — the decisions a JIT HMI grant turns on.

FUXA's permission model is a BITMASK, and the bit semantics are the one part of its
behaviour that has not been confirmed against a running instance. So the rules are
built to fail in the SAFE direction, and this file is what holds them there: the
worst outcome of a wrong reading must be a grant that does too little, never one
that hands out administrator.

Four properties, each standing for a failure FUXA itself reports as success:

* **no grant can produce administrator.** Bit 128, and the -1 / 255 sentinels FUXA
  treats as "all groups" — the seeded `admin` account carries -1.
* **the adapter touches only its own accounts.** The username prefix is the only
  thing between `delete_actor` and an operator's real HMI users.
* **`info` is always valid JSON.** FUXA parses it AFTER answering 200, and on a
  parse failure the user is silently absent from its in-memory map — able to sign
  in, 401 on everything, until a restart.
* **a role code is never quietly substituted.** An unknown code means Entitle's
  catalogue and this adapter have drifted, and defaulting would either grant the
  wrong thing or hide the drift forever.

Pure and stdlib-only; runs with nothing installed and reaches no HMI.

Run: python tests/test_fuxa_access_rules.py   (or under pytest)
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard.services import fuxa_access_rules as rules  # noqa: E402


# ── The administrator guard ──────────────────────────────────────────────────

def test_no_published_role_can_produce_administrator():
    """The property the whole module is arranged around."""
    for code in rules.role_codes():
        groups = rules.groups_for(code)
        assert not rules.is_admin_groups(groups), (code, groups)
        assert groups > 0, (code, groups)


def test_the_admin_bit_is_caught_in_either_half():
    """The value carries two bytes — the enabled set and the shown set — and the
    administrator bit is fatal in both. A guard that checked only the low byte would
    pass a value that shows every administrator view."""
    assert rules.is_admin_groups(rules.GROUP_ADMINISTRATOR)
    assert rules.is_admin_groups(rules.GROUP_ADMINISTRATOR << rules.SHOW_SHIFT)
    assert rules.is_admin_groups(rules.GROUP_VIEWER | rules.GROUP_ADMINISTRATOR)


def test_the_all_permissions_sentinels_are_refused():
    """FUXA's own adminGroups are -1 and 255, and the seeded admin carries -1."""
    for sentinel in (-1, 255, (255 << rules.SHOW_SHIFT) | 255):
        assert rules.is_admin_groups(sentinel), sentinel


def test_an_unparseable_groups_value_is_treated_as_dangerous():
    """"I cannot tell" must not read as "safe" — this guard decides whether a value
    is handed to FUXA."""
    for junk in (None, "", "admin", "0x80", [], {}):
        assert rules.is_admin_groups(junk), junk


def test_groups_for_refuses_a_table_edited_to_include_admin():
    """The assertion inside groups_for, which is what keeps the table honest when
    somebody adds a role to it."""
    original = rules.ROLE_OPTIONS
    try:
        rules.ROLE_OPTIONS = original + (
            {"code": "superuser", "display_name": "oops",
             "groups": rules.GROUP_ADMINISTRATOR, "permissions": ()},)
        try:
            rules.groups_for("superuser")
            raise AssertionError("an administrator role was mintable")
        except rules.FuxaRuleError as exc:
            assert "administrator" in str(exc).lower(), str(exc)
    finally:
        rules.ROLE_OPTIONS = original


def test_a_role_that_may_act_can_also_see():
    """Both halves come from the same bits. Enabling a control on a view the account
    cannot open presents as a broken HMI, not as a permission problem."""
    groups = rules.groups_for("operator")
    enabled = groups & 0xFF
    shown = (groups >> rules.SHOW_SHIFT) & 0xFF
    assert enabled == shown, (enabled, shown)
    assert enabled & rules.GROUP_OPERATOR and enabled & rules.GROUP_VIEWER


def test_operator_is_a_superset_of_viewer():
    viewer = rules.groups_for("viewer") & 0xFF
    operator = rules.groups_for("operator") & 0xFF
    assert viewer & operator == viewer, "operator cannot do everything viewer can"
    assert operator != viewer, "the two roles are indistinguishable"


# ── Role codes ───────────────────────────────────────────────────────────────

def test_an_unknown_role_code_raises_rather_than_defaulting():
    try:
        rules.role_option("engineer-plus")
        raise AssertionError("an unpublished role code was accepted")
    except rules.FuxaRuleError as exc:
        assert "engineer-plus" in str(exc)
        assert "viewer" in str(exc) and "operator" in str(exc), (
            "the refusal does not say what IS published")


def test_a_blank_role_code_is_the_least_privileged_one():
    assert rules.role_option("")["code"] == rules.DEFAULT_ROLE_CODE == "viewer"
    assert rules.groups_for("") == rules.groups_for("viewer")


def test_role_codes_round_trip_through_their_groups_value():
    for code in rules.role_codes():
        assert rules.role_code_for_groups(rules.groups_for(code)) == code


def test_an_unrecognised_groups_value_reports_nothing_rather_than_guessing():
    """An account an operator made by hand holds something this adapter cannot name,
    and Entitle reconciles against what it is told."""
    assert rules.role_code_for_groups(rules.GROUP_ENGINEER) == ""
    assert rules.role_code_for_groups(-1) == ""
    assert rules.role_code_for_groups("nonsense") == ""


def test_asset_role_options_are_all_available_without_a_catalogue():
    options = rules.asset_role_options()
    assert [o["code"] for o in options] == list(rules.role_codes())
    assert all(o["available"] for o in options)


def test_a_catalogue_disables_a_role_the_plant_does_not_have():
    """Disabled, not dropped: Entitle greys an unavailable role and drops an asset
    whose options all vanished, so a half-configured HMI must not disappear
    mid-POV."""
    options = rules.asset_role_options([{"id": "g1", "name": "Viewer"}])
    by_code = {o["code"]: o for o in options}
    assert by_code["viewer"]["available"] is True
    assert by_code["operator"]["available"] is False
    assert len(options) == len(rules.role_codes()), "an option was dropped, not disabled"


# ── Account names ────────────────────────────────────────────────────────────

def test_a_minted_name_traces_back_to_the_requester():
    name = rules.ephemeral_username("Vendor.Tech@acme.example", "a1b2c3d4e5f6a7b8")
    assert name.startswith("jit-"), name
    assert "vendor-tech-acme-example" in name, name
    assert rules.is_ephemeral_username(name)
    rules.validate_username(name)


def test_two_grants_to_one_person_do_not_collide():
    first = rules.ephemeral_username("a@b.c", "1111111111111111")
    second = rules.ephemeral_username("a@b.c", "2222222222222222")
    assert first != second


def test_a_name_survives_an_identity_that_is_all_punctuation():
    name = rules.ephemeral_username("!!!", "")
    assert rules.is_ephemeral_username(name)
    rules.validate_username(name)


def test_the_prefix_is_what_protects_the_operators_own_accounts():
    for theirs in ("admin", "operator1", "Admin", "", None, "jit", "xjit-a"):
        assert not rules.is_ephemeral_username(theirs), theirs
    assert rules.is_ephemeral_username("JIT-Someone-1234")  # case-insensitive


def test_a_name_too_long_or_malformed_is_refused():
    for bad in ("jit-" + "x" * 200, "admin", "jit-", "jit-a"):
        try:
            rules.validate_username(bad)
            raise AssertionError(f"accepted {bad!r}")
        except rules.FuxaRuleError:
            pass


def test_only_our_own_accounts_are_reported():
    users = [{"username": "admin"}, {"username": "jit-a-1"},
             {"username": "operator"}, {"username": "JIT-B-2"}, "not-a-dict"]
    got = [u["username"] for u in rules.ephemeral_users(users)]
    assert got == ["jit-a-1", "JIT-B-2"], got


def test_a_user_lookup_folds_case():
    """FUXA's username is a primary key and its UI does not fold case, so a
    case-sensitive match would let two spellings exist and delete neither."""
    users = [{"username": "JIT-Alice-1", "groups": 1}]
    assert rules.match_user(users, "jit-alice-1")["groups"] == 1
    assert rules.match_user(users, "jit-bob-2") == {}
    assert rules.match_user(None, "x") == {}


# ── Passwords ────────────────────────────────────────────────────────────────

def test_a_generated_password_is_long_and_transcribable():
    password = rules.generate_password()
    assert len(password) == rules.PASSWORD_LENGTH >= 20
    # It is read off a screen and retyped, and it travels through JSON.
    for forbidden in ('"', "'", "\\", " ", "O", "l", "0", "1", "I"):
        assert forbidden not in password, f"{forbidden!r} in {password!r}"


def test_two_passwords_differ():
    assert rules.generate_password() != rules.generate_password()


# ── The `info` column ────────────────────────────────────────────────────────

def test_info_is_always_valid_json():
    """The trap: FUXA JSON-parses this AFTER answering 200, and a failure leaves the
    user absent from its in-memory map — signs in, then 401s on everything until a
    restart."""
    for roles in (None, [], ["g1"], ["g1", "g2"]):
        parsed = json.loads(rules.user_info(roles=roles))
        assert isinstance(parsed, dict)
        assert parsed["roles"] == [str(r) for r in (roles or [])]
        assert "start" in parsed and "languageId" in parsed


def test_info_preserves_the_keys_an_account_already_had():
    existing = json.dumps({"start": "/view/main", "languageId": "en", "other": 1})
    parsed = json.loads(rules.user_info(roles=["g1"], existing=existing))
    assert parsed["start"] == "/view/main" and parsed["languageId"] == "en"
    assert parsed["other"] == 1
    assert parsed["roles"] == ["g1"]


def test_an_unparseable_existing_info_is_replaced_not_propagated():
    """That state IS the bug above; writing a valid value over it is a repair."""
    parsed = json.loads(rules.user_info(roles=[], existing="{not json"))
    assert parsed["roles"] == [] and "start" in parsed


# ── The create payload ───────────────────────────────────────────────────────

def test_the_create_payload_is_a_single_object_with_every_field_fuxa_needs():
    body = rules.user_payload(username="jit-a-1", password="pw12345678",
                              role_code="operator")
    assert isinstance(body, dict), "a list is rejected by setUsers with a bare 400"
    assert set(body) == {"username", "fullname", "password", "groups", "info"}
    assert body["groups"] == rules.groups_for("operator")
    assert json.loads(body["info"])["roles"] == []
    assert body["password"] == "pw12345678", "the password must go in PLAINTEXT"


def test_the_create_payload_refuses_an_account_with_no_password():
    """A NULL password inserts a row that can never sign in — and FUXA reports the
    create as a success."""
    try:
        rules.user_payload(username="jit-a-1", password="  ", role_code="viewer")
        raise AssertionError("a passwordless account was accepted")
    except rules.FuxaRuleError as exc:
        assert "NULL password" in str(exc) or "no password" in str(exc), str(exc)


def test_the_create_payload_refuses_an_unsafe_username():
    try:
        rules.user_payload(username="admin", password="pw12345678",
                           role_code="viewer")
        raise AssertionError("a non-ephemeral username was accepted")
    except rules.FuxaRuleError as exc:
        assert "unsafe" in str(exc)


def test_the_create_payload_never_carries_an_admin_groups_value():
    for code in rules.role_codes():
        body = rules.user_payload(username="jit-a-1", password="pw12345678",
                                  role_code=code)
        assert not rules.is_admin_groups(body["groups"]), (code, body["groups"])


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
