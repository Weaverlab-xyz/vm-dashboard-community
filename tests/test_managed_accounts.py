"""Unit tests for services/managed_accounts.py (pure shaping + guard logic).

Loaded by file path (stdlib only) — no config / FastAPI / ps-cli needed.
Runs under pytest, or standalone:  python tests/test_managed_accounts.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "managed_accounts.py")
_spec = importlib.util.spec_from_file_location("managed_accounts", _PATH)
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)


# ── host_is_ip ──────────────────────────────────────────────────────────────────

def test_host_is_ip():
    assert ma.host_is_ip("10.0.0.5")
    assert ma.host_is_ip(" 192.168.1.1 ")   # trimmed
    assert not ma.host_is_ip("host.example.com")
    assert not ma.host_is_ip("")
    assert not ma.host_is_ip("dc01")


# ── lookup_args ─────────────────────────────────────────────────────────────────

def test_lookup_args_ip_only():
    # No name hint → match on IP alone (unchanged behaviour).
    assert ma.lookup_args("10.99.1.186") == ("10.99.1.186", "")
    assert ma.lookup_args(" 10.99.1.186 ") == ("10.99.1.186", "")


def test_lookup_args_ip_with_name_hint():
    # A cloud VM: keep the IP but pass the deploy name so a name-registered system
    # with a placeholder IP (AWS Systems Manager plugin) is still found.
    assert ma.lookup_args("10.99.1.186", "ubuntu24-1783462329") \
        == ("10.99.1.186", "ubuntu24-1783462329")


def test_lookup_args_non_ip_host_is_the_name():
    assert ma.lookup_args("dc01.shield.int") == ("", "dc01.shield.int")


def test_lookup_args_explicit_name_wins_over_non_ip_host():
    assert ma.lookup_args("some-host", "real-name") == ("", "real-name")


def test_lookup_args_trims_and_handles_empty():
    assert ma.lookup_args("", "") == ("", "")
    assert ma.lookup_args("  ", "  vm-1 ") == ("", "vm-1")


# ── ssh_login_user ──────────────────────────────────────────────────────────────

def test_ssh_login_user_plain_account_unchanged():
    assert ma.ssh_login_user("root") == "root"
    assert ma.ssh_login_user("svc-ansible") == "svc-ansible"


def test_ssh_login_user_strips_ssm_local_suffix():
    # AWS Systems Manager plugin IAM-user mode: "{user};local".
    assert ma.ssh_login_user("adminuser;local") == "adminuser"


def test_ssh_login_user_strips_ssm_arn_suffix():
    # AWS Systems Manager plugin EC2 mode: "{user};<AssumeRole ARN>".
    assert ma.ssh_login_user("ec2-user;arn:aws:iam::123456789012:role/PS-SSM") == "ec2-user"


def test_ssh_login_user_handles_empty_and_whitespace():
    assert ma.ssh_login_user("") == ""
    assert ma.ssh_login_user(None) == ""
    assert ma.ssh_login_user("  adminuser ; local ") == "adminuser"


# ── normalize_managed_systems ───────────────────────────────────────────────────

def test_normalize_locally_managed_account_ssh_and_password():
    systems = [{"ManagedSystemID": 5, "Name": "web01", "IPAddress": "10.0.0.5"}]
    accounts = {5: [
        {"ManagedAccountID": 45, "AccountName": "root", "DSSAutoManagementFlag": True},
        {"ManagedAccountID": 46, "AccountName": "deploy", "DSSAutoManagementFlag": False},
    ]}
    out = ma.normalize_managed_systems(systems, accounts)
    assert out == [{
        "system_id": 5, "name": "web01", "ip": "10.0.0.5",
        "accounts": [
            {"account_id": 45, "name": "root", "domain": "", "uses_ssh_key": True,
             "change_after_release": None},
            {"account_id": 46, "name": "deploy", "domain": "", "uses_ssh_key": False,
             "change_after_release": None},
        ],
    }]


def test_normalize_surfaces_change_after_release_flag():
    systems = [{"ManagedSystemID": 1, "Name": "x", "IPAddress": "1.1.1.1"}]
    accounts = {1: [
        {"ManagedAccountID": 1, "AccountName": "rotates",
         "ChangePasswordAfterAnyReleaseFlag": True},
        {"ManagedAccountID": 2, "AccountName": "static",
         "ChangePasswordAfterAnyReleaseFlag": False},
        {"ManagedAccountID": 3, "AccountName": "unknown"},   # flag absent
    ]}
    out = ma.normalize_managed_systems(systems, accounts)
    car = {a["name"]: a["change_after_release"] for a in out[0]["accounts"]}
    assert car == {"rotates": True, "static": False, "unknown": None}


def test_normalize_domain_linked_field_variants():
    # list-accounts fallback shape: SystemId / AccountId / DomainName, no DSS flag.
    systems = [{"SystemId": 9, "SystemName": "DC01", "IPAddress": "10.0.0.9"}]
    accounts = {9: [
        {"AccountId": 100, "AccountName": "svc-ansible", "DomainName": "SHIELD"},
    ]}
    out = ma.normalize_managed_systems(systems, accounts)
    assert out[0]["system_id"] == 9 and out[0]["name"] == "DC01"
    acct = out[0]["accounts"][0]
    assert acct == {"account_id": 100, "name": "svc-ansible",
                    "domain": "SHIELD", "uses_ssh_key": False,
                    "change_after_release": None}


def test_normalize_skips_systems_and_accounts_without_ids():
    systems = [{"Name": "nope", "IPAddress": "1.2.3.4"},          # no system id → skipped
               {"ManagedSystemID": 3, "Name": "ok", "IPAddress": "1.2.3.5"}]
    accounts = {3: [{"AccountName": "no-id"},                     # no account id → skipped
                    {"ManagedAccountID": 7, "AccountName": "yes"}]}
    out = ma.normalize_managed_systems(systems, accounts)
    assert len(out) == 1 and out[0]["system_id"] == 3
    assert [a["account_id"] for a in out[0]["accounts"]] == [7]


def test_normalize_system_with_no_accounts():
    out = ma.normalize_managed_systems(
        [{"ManagedSystemID": 1, "Name": "x", "IPAddress": "1.1.1.1"}], {})
    assert out == [{"system_id": 1, "name": "x", "ip": "1.1.1.1", "accounts": []}]


def test_normalize_empty():
    assert ma.normalize_managed_systems([], {}) == []
    assert ma.normalize_managed_systems(None, {}) == []


# ── requires_ephemeral_store ────────────────────────────────────────────────────

def test_requires_ephemeral_store_true_for_ecs_and_gcp():
    assert ma.requires_ephemeral_store(True, "ecs", True, True)
    assert ma.requires_ephemeral_store(True, "gcp", True, True)


def test_requires_ephemeral_store_false_for_aci():
    # ACI injects inline (secure_value) — managed accounts work there directly.
    assert not ma.requires_ephemeral_store(True, "aci", True, True)


def test_requires_ephemeral_store_false_when_no_managed():
    assert not ma.requires_ephemeral_store(False, "ecs", True, True)


def test_requires_ephemeral_store_false_for_local_runner():
    assert not ma.requires_ephemeral_store(True, "local", True, True)


def test_requires_ephemeral_store_false_when_not_adhoc_or_not_playbook():
    # group target (not adhoc) or non-playbook → falls back to local anyway
    assert not ma.requires_ephemeral_store(True, "ecs", False, True)
    assert not ma.requires_ephemeral_store(True, "ecs", True, False)


# ── find_account_by_name (per-host resolution for bulk runs) ────────────────────

def _systems(*specs):
    """normalize_managed_systems-shaped input. Each spec is (system_id, [accounts])
    where an account is (account_id, name) or (account_id, name, uses_ssh_key)."""
    out = []
    for sid, accounts in specs:
        out.append({
            "system_id": sid, "name": f"sys-{sid}", "ip": f"10.0.0.{sid}",
            "accounts": [
                {"account_id": a[0], "name": a[1], "domain": "",
                 "uses_ssh_key": (a[2] if len(a) > 2 else False),
                 "change_after_release": None}
                for a in accounts
            ],
        })
    return out


def test_find_account_by_exact_name():
    systems = _systems((7, [(1, "root"), (2, "svc-ansible")]))
    ref = ma.find_account_by_name(systems, "svc-ansible")
    assert ref == {"system_id": 7, "account_id": 2,
                   "account_name": "svc-ansible", "uses_ssh_key": False}


def test_find_account_matches_the_cloud_plugin_suffix_form():
    """AWS Systems Manager registers `{user};{suffix}`. An operator picking
    'adminuser' for a fleet must still match it."""
    systems = _systems((3, [(9, "adminuser;local")]))
    ref = ma.find_account_by_name(systems, "adminuser")
    assert ref is not None and ref["account_id"] == 9
    # The account's OWN name is returned — it becomes ansible_user, where the
    # suffix still has to be stripped by ssh_login_user at connection time.
    assert ref["account_name"] == "adminuser;local"


def test_find_account_is_case_insensitive():
    systems = _systems((1, [(4, "SVC-Ansible")]))
    assert ma.find_account_by_name(systems, "svc-ansible")["account_id"] == 4


def test_find_account_searches_every_system_for_the_host():
    """A host can resolve to more than one managed system (e.g. an IP match and a
    domain-linked one); the account may live on either."""
    systems = _systems((1, [(1, "root")]), (2, [(5, "svc-ansible")]))
    ref = ma.find_account_by_name(systems, "svc-ansible")
    assert ref["system_id"] == 2 and ref["account_id"] == 5


def test_find_account_carries_the_ssh_key_flag():
    """uses_ssh_key routes the checkout to -t dsskey and the credential to
    SSH_KEY_B64 instead of a password var — losing it breaks the connection."""
    systems = _systems((1, [(2, "svc-ansible", True)]))
    assert ma.find_account_by_name(systems, "svc-ansible")["uses_ssh_key"] is True


def test_find_account_returns_none_when_the_host_lacks_it():
    """This is the per-host failure the design accepts: that job fails, the rest of
    the batch is unaffected."""
    systems = _systems((1, [(1, "root")]))
    assert ma.find_account_by_name(systems, "svc-ansible") is None


def test_find_account_returns_none_for_a_blank_name_or_no_systems():
    assert ma.find_account_by_name(_systems((1, [(1, "root")])), "") is None
    assert ma.find_account_by_name(_systems((1, [(1, "root")])), "   ") is None
    assert ma.find_account_by_name([], "root") is None
    assert ma.find_account_by_name(None, "root") is None


def test_find_account_tolerates_a_system_with_no_accounts():
    systems = _systems((1, []), (2, [(3, "root")]))
    assert ma.find_account_by_name(systems, "root")["system_id"] == 2


# ── select_systems / narrow_by_ip / filter_by_ip ────────────────────────────────
#
# The batch counterpart of btapi_service's per-host lookup: one already-fetched
# estate, narrowed locally, so a 50-target bulk run does not re-list the estate 50
# times. narrow_by_ip is shared with that per-host path, so these also pin the
# precedence both of them rely on.

def _sys(sid, name, ip=""):
    return {"ManagedSystemID": sid, "Name": name, "IPAddress": ip}


_ESTATE = [
    _sys(1, "DC01", "10.0.0.10"),        # same name, different workgroup…
    _sys(2, "DC01", "10.0.0.11"),        # …as this one
    _sys(3, "web-01", "10.0.0.20"),
    _sys(4, "ssm-box", "127.0.0.1"),     # plugin-onboarded: placeholder IP
    _sys(5, "no-ip", None),
]


def test_select_systems_name_and_ip_agree_is_the_unambiguous_hit():
    got = ma.select_systems(_ESTATE, "10.0.0.11", "DC01")
    assert [s["ManagedSystemID"] for s in got] == [2]


def test_select_systems_lone_name_match_wins_without_an_ip_match():
    # The plugin-onboarded shape: registered by name, IP is a placeholder, and the
    # address we would connect on appears nowhere in Password Safe.
    got = ma.select_systems(_ESTATE, "10.99.1.7", "ssm-box")
    assert [s["ManagedSystemID"] for s in got] == [4]


def test_select_systems_returns_every_name_match_when_the_ip_cannot_disambiguate():
    # Ambiguity is surfaced, never silently resolved — picking one of these would be
    # connecting to a host in the wrong workgroup.
    got = ma.select_systems(_ESTATE, "10.0.0.99", "DC01")
    assert [s["ManagedSystemID"] for s in got] == [1, 2]


def test_select_systems_is_case_insensitive_on_the_name():
    assert [s["ManagedSystemID"] for s in ma.select_systems(_ESTATE, "", "WEB-01")] == [3]
    assert [s["ManagedSystemID"] for s in ma.select_systems(_ESTATE, "", " web-01 ")] == [3]


def test_select_systems_falls_back_to_the_ip_when_the_name_misses():
    got = ma.select_systems(_ESTATE, "10.0.0.20", "not-registered")
    assert [s["ManagedSystemID"] for s in got] == [3]


def test_select_systems_ip_only():
    assert [s["ManagedSystemID"] for s in ma.select_systems(_ESTATE, "10.0.0.10", "")] == [1]


def test_select_systems_empty_estate_and_no_match():
    assert ma.select_systems([], "10.0.0.1", "DC01") == []
    assert ma.select_systems(None, "10.0.0.1", "DC01") == []
    assert ma.select_systems(_ESTATE, "10.1.1.1", "nope") == []


def test_select_systems_reads_the_systemname_field_variant():
    estate = [{"ManagedSystemID": 9, "SystemName": "alt", "IPAddress": "10.0.0.9"}]
    assert [s["ManagedSystemID"] for s in ma.select_systems(estate, "", "alt")] == [9]


def test_narrow_by_ip_prefers_the_ip_match_else_hands_back_everything():
    cands = [_sys(1, "DC01", "10.0.0.10"), _sys(2, "DC01", "10.0.0.11")]
    assert [s["ManagedSystemID"] for s in ma.narrow_by_ip(cands, "10.0.0.11")] == [2]
    assert [s["ManagedSystemID"] for s in ma.narrow_by_ip(cands, "10.9.9.9")] == [1, 2]
    assert ma.narrow_by_ip([], "10.0.0.1") == []
    assert ma.narrow_by_ip(None, "10.0.0.1") == []


def test_filter_by_ip():
    assert [s["ManagedSystemID"] for s in ma.filter_by_ip(_ESTATE, "10.0.0.20")] == [3]
    assert ma.filter_by_ip(None, "10.0.0.20") == []


# ── suggest_account ─────────────────────────────────────────────────────────────
#
# A suggestion is shown, never silently applied. Tier 1 is load-bearing beyond
# convenience: matching the batch's chosen NAME guarantees the pre-selection equals
# what the name-only fallback would resolve to for that host, which is what makes
# leaving a row untouched safe.

def test_suggest_prefers_the_batch_default_name():
    systems = _systems((1, [(10, "root"), (11, "svc-ansible")]))
    ref, basis = ma.suggest_account(systems, default_name="svc-ansible")
    assert basis == ma.BASIS_DEFAULT_NAME
    assert (ref["system_id"], ref["account_id"]) == (1, 11)


def test_suggest_default_name_outranks_the_recorded_system():
    systems = _systems((1, [(10, "root")]), (2, [(20, "svc-ansible")]))
    ref, basis = ma.suggest_account(systems, default_name="svc-ansible", ps_system_id="1")
    assert basis == ma.BASIS_DEFAULT_NAME
    assert ref["system_id"] == 2


def test_suggest_falls_back_to_the_recorded_system():
    # The plugin-onboarded case: no name chosen yet, but registration recorded which
    # managed system this VM actually is.
    systems = _systems((1, [(10, "root")]), (2, [(20, "other")]))
    ref, basis = ma.suggest_account(systems, ps_system_id="2")
    assert basis == ma.BASIS_RECORDED_SYSTEM
    assert (ref["system_id"], ref["account_id"]) == (2, 20)


def test_suggest_declines_when_the_recorded_system_is_ambiguous():
    # Two accounts on the right system is not a basis for guessing between them.
    systems = _systems((1, [(10, "root"), (11, "admin")]))
    assert ma.suggest_account(systems, ps_system_id="1") == (None, "")


def test_suggest_takes_a_single_unambiguous_candidate():
    ref, basis = ma.suggest_account(_systems((7, [(70, "root")])))
    assert basis == ma.BASIS_ONLY_ACCOUNT
    assert (ref["system_id"], ref["account_id"]) == (7, 70)


def test_suggest_declines_when_there_is_nothing_to_go_on():
    assert ma.suggest_account(_systems((1, [(10, "a")]), (2, [(20, "b")]))) == (None, "")
    assert ma.suggest_account([]) == (None, "")
    assert ma.suggest_account(None) == (None, "")
    assert ma.suggest_account(_systems((1, []))) == (None, "")


def test_suggest_matches_the_cloud_plugin_suffix_form_via_tier_one():
    systems = _systems((1, [(10, "svc-ansible;local")]))
    ref, basis = ma.suggest_account(systems, default_name="svc-ansible")
    assert basis == ma.BASIS_DEFAULT_NAME
    # The account's OWN name travels, suffix included — it becomes ansible_user.
    assert ref["account_name"] == "svc-ansible;local"


def test_suggest_tolerates_a_recorded_system_not_in_the_list():
    ref, basis = ma.suggest_account(_systems((7, [(70, "root")])), ps_system_id="999")
    assert basis == ma.BASIS_ONLY_ACCOUNT      # falls through to tier 3


# ── pick_ref ────────────────────────────────────────────────────────────────────
#
# PRESENCE decides, not truthiness. Collapsing "mapped to None" into "absent" would
# make "no managed account for this host" silently mean "use the fleet account here",
# which is the bug the per-target map exists to fix.

_DEFAULT = {"account_name": "svc-fleet"}
_OWN = {"system_id": 7, "account_id": 8, "account_name": "svc-web01"}


def test_pick_ref_present_key_wins_over_the_default():
    assert ma.pick_ref({"job:a": _OWN}, "job:a", _DEFAULT) == _OWN


def test_pick_ref_absent_key_falls_back_to_the_default():
    assert ma.pick_ref({"job:a": _OWN}, "job:b", _DEFAULT) == _DEFAULT


def test_pick_ref_explicit_none_means_no_account_not_the_default():
    assert ma.pick_ref({"job:a": None}, "job:a", _DEFAULT) is None


def test_pick_ref_empty_map_is_the_default_for_every_target():
    assert ma.pick_ref({}, "job:a", _DEFAULT) == _DEFAULT
    assert ma.pick_ref(None, "job:a", _DEFAULT) == _DEFAULT


def test_pick_ref_default_may_itself_be_none():
    assert ma.pick_ref({}, "job:a", None) is None


# ── stray_ids ───────────────────────────────────────────────────────────────────

def test_stray_ids_flags_keys_outside_the_run():
    assert ma.stray_ids(["job:a", "job:z"], ["job:a", "job:b"]) == ["job:z"]


def test_stray_ids_is_empty_when_every_key_is_a_target():
    assert ma.stray_ids(["job:b", "job:a"], ["job:a", "job:b"]) == []
    assert ma.stray_ids([], ["job:a"]) == []
    assert ma.stray_ids(None, ["job:a"]) == []


def test_stray_ids_is_sorted_so_the_error_message_is_stable():
    assert ma.stray_ids(["z", "a", "m"], []) == ["a", "m", "z"]


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
