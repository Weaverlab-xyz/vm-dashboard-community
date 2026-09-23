"""Matching dashboard resources to Password Safe objects, by id and by address.

The feature is "show me Password Safe attributes next to my infrastructure", and the whole
of it turns on a join. Five properties, in the order it would hurt to lose them.

1. **A placeholder address is never a match key.** Every cloud-native plugin onboarding
   writes ``IPAddress = "127.0.0.1"`` — Password Safe requires one on create. Accept it and
   every SSM/azurevm/gcpvm system in the tenant collides onto a single key, so one VM shows
   another VM's attributes. ``169.254.169.254`` is the same failure with the cloud metadata
   endpoint. Both are refused by PROPERTY (``is_loopback`` / ``is_link_local``), not by a
   denylist that ages.
2. **A packed locator is not an address.** ``DnsName`` on those same systems holds
   ``i-0abc123:us-east-1`` or ``<tenant>/<sub>/<rg>/<vm>``. Borrowing
   ``ps_database_catalog``'s ``DnsName > HostName > IPAddress`` order — which is a
   *connect-host* resolver — would make those match keys. They must yield nothing.
3. **Those systems must still match, via the id the dashboard recorded.** Tier 1 exists
   precisely because tier 2 cannot reach them; without it the column is empty for most of
   what this dashboard onboarded, which reads as broken rather than as "no match".
4. **An Asset and a ManagedSystem at one address are ONE host.** Password Safe holds both
   for the same machine routinely. If that trips the ambiguity state, ambiguity fires on
   the common case and the feature shows nothing.
5. **"Has no attributes" and "we did not look" are different.** They render the same way —
   an empty cell — unless the states are kept apart, and only one of them is worth acting on.

Pure, stdlib only, loaded by file path. Runs under pytest, or standalone:
    python tests/test_ps_attribute_catalog.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")


def _load(name):
    """Load a stdlib-only service module by path, satisfying its one relative import."""
    spec = importlib.util.spec_from_file_location(
        f"web_dashboard.services.{name}", os.path.join(_SERVICES, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ps_attribute_catalog does `from . import tag_policy`, so the package has to exist in
# sys.modules first. Both are stdlib-only, which is the property this file relies on.
import types  # noqa: E402

for _pkg in ("web_dashboard", "web_dashboard.services"):
    if _pkg not in sys.modules:
        module = types.ModuleType(_pkg)
        module.__path__ = [os.path.join(_ROOT, *_pkg.split(".")[1:])] or [_ROOT]
        sys.modules[_pkg] = module
sys.modules["web_dashboard.services"].__path__ = [_SERVICES]

tp = _load("tag_policy")
pac = _load("ps_attribute_catalog")


# ── fixtures mirroring what the two sides really look like ───────────────────

def _ssm_system(sid=7, name="web-1"):
    """A managed system as ps_vm_hook.py:193 creates one. No usable address ANYWHERE."""
    return {"ManagedSystemID": sid, "SystemName": name, "HostName": name,
            "DnsName": "i-0abc123:us-east-1", "IPAddress": "127.0.0.1"}


def _asset(aid=12, name="WEB-1", ip="10.0.0.4"):
    """An asset as discovery creates one — a real address."""
    return {"AssetID": aid, "AssetName": name, "IPAddress": ip}


# ── 1. placeholders are never keys ───────────────────────────────────────────

def test_the_onboarding_placeholder_is_never_a_match_key():
    """Every plugin-onboarded system carries 127.0.0.1. One key for all of them would
    show one VM another VM's attributes."""
    assert pac.canonical_ip("127.0.0.1") == ""
    assert pac.canonical_ip("127.1.2.3") == ""


def test_the_cloud_metadata_address_is_never_a_match_key():
    """169.254.169.254 turns up in scraped inventory and is the same address on every
    cloud VM in the estate."""
    assert pac.canonical_ip("169.254.169.254") == ""


def test_the_other_unusable_families_are_refused():
    for bad in ("0.0.0.0", "::1", "fe80::1", "224.0.0.1", "240.0.0.1"):
        assert pac.canonical_ip(bad) == "", bad


def test_private_addresses_are_kept_because_they_are_the_point():
    """Password Safe knows hosts by their private address; excluding RFC1918 leaves
    nothing to match on."""
    for good in ("10.0.0.4", "172.16.5.9", "192.168.1.20"):
        assert pac.canonical_ip(good) == good, good


def test_an_ipv4_mapped_v6_address_folds_onto_its_v4_form():
    """So a tenant storing one form and a page reporting the other still join."""
    assert pac.canonical_ip("::ffff:10.0.0.1") == "10.0.0.1"


def test_a_zero_padded_octet_is_refused_rather_than_guessed():
    """`010` is ambiguous between octal and decimal and that ambiguity is a known source
    of address-confusion bugs. A missed match is cheaper than a wrong one."""
    assert pac.canonical_ip("10.0.0.01") == ""


# ── 2. a packed locator is not an address ────────────────────────────────────

def test_the_packed_dnsname_forms_yield_no_addresses():
    """The regression that protects the whole design: these are the real DnsName values
    from ps_vm_hook.py 193/225/253. If any parsed, it would become a match key."""
    for locator in ("i-0abc123:us-east-1",
                    "11111111-2222-3333-4444-555555555555/sub/rg/vm",
                    "my-project/us-central1-a/web-1",
                    "host;rg;sub;tenant;a;b;c;d"):
        assert pac.canonical_ip(locator) == "", locator


def test_a_plugin_onboarded_system_has_no_usable_address_at_all():
    """Stated as its own assertion because it is the fact that forces tier 1 to exist."""
    assert pac.object_addresses(_ssm_system()) == []


def test_a_hostname_is_not_an_address():
    assert pac.canonical_ip("web-1.corp.example.com") == ""


def test_every_address_field_is_read_not_just_the_first():
    """A discovered asset has its address in IPAddress; an ssh-method system has one
    there too; some records put it in DnsName. Reading one field would miss two cases."""
    assert pac.object_addresses({"AssetID": 1, "DnsName": "10.1.2.3"}) == ["10.1.2.3"]
    assert pac.object_addresses({"AssetID": 1, "HostName": "10.1.2.4"}) == ["10.1.2.4"]


# ── 3. tier 1: the recorded id ───────────────────────────────────────────────

def test_a_plugin_onboarded_vm_matches_on_the_recorded_system_id():
    index = pac.build_index([], [_ssm_system(sid=7)])
    refs, basis = pac.match_refs({"ps_system_id": "7", "ips": []}, index)
    assert refs == ["managed_system:7"]
    assert basis == pac.BASIS_ID


def test_the_recorded_id_outranks_an_address():
    """The id is what the dashboard itself wrote at onboarding; an address that disagrees
    is at best a coincidence and at worst a reused RFC1918 range."""
    index = pac.build_index([_asset(aid=12, ip="10.0.0.4")], [_ssm_system(sid=7)])
    refs, basis = pac.match_refs({"ps_system_id": "7", "ips": ["10.0.0.4"]}, index)
    assert refs == ["managed_system:7"] and basis == pac.BASIS_ID


def test_an_id_that_names_nothing_falls_through_to_the_address():
    """A system deregistered in Password Safe but still recorded on the deploy job."""
    index = pac.build_index([_asset(aid=12, ip="10.0.0.4")], [])
    refs, basis = pac.match_refs({"ps_system_id": "999", "ips": ["10.0.0.4"]}, index)
    assert refs == ["asset:12"] and basis == pac.BASIS_IP


def test_an_asset_is_never_reachable_by_the_recorded_id():
    """`ps_managed_system_id` is a MANAGED SYSTEM id. Indexing assets under it would
    match an asset whose id happens to collide with a system's."""
    index = pac.build_index([_asset(aid=7)], [])
    assert index["by_id"] == {}


# ── 4. coalescing vs real ambiguity ──────────────────────────────────────────

def test_an_asset_and_a_system_at_one_address_are_one_host():
    """The NORMAL case. If this trips ambiguity, ambiguity fires constantly."""
    index = pac.build_index([_asset(aid=12, ip="10.0.0.4")],
                            [{"ManagedSystemID": 8, "SystemName": "s", "IPAddress": "10.0.0.4"}])
    out = pac.match({"ips": ["10.0.0.4"]}, index,
                    {"asset:12": [{"AttributeTypeID": 3, "ShortName": "High"}],
                     "managed_system:8": []}, types=_TYPES)
    assert out["state"] == pac.STATE_OK
    assert {o["kind"] for o in out["objects"]} == {pac.KIND_ASSET, pac.KIND_SYSTEM}
    assert [c["key"] for c in out["attributes"]] == ["Criticality"]


def test_two_assets_at_one_address_are_ambiguous_and_show_no_chips():
    """In Phase 2 a wrong guess here becomes a write against the wrong Asset."""
    index = pac.build_index([_asset(1, "A", "10.0.0.4"), _asset(2, "B", "10.0.0.4")], [])
    out = pac.match({"ips": ["10.0.0.4"]}, index, {"asset:1": [{"AttributeTypeID": 3, "ShortName": "High"}]})
    assert out["state"] == pac.STATE_AMBIGUOUS
    assert out["attributes"] == []
    assert "A" in out["detail"] and "B" in out["detail"]


def test_one_record_matching_two_vms_is_reported_not_refused():
    """Legitimate — two VPCs, or a destroy-and-redeploy. Phase 2 needs to refuse a write
    on it, and deriving that later would mean re-doing the join."""
    index = pac.build_index([_asset(9, "C", "10.0.0.5")], [])
    out = pac.match({"ips": ["10.0.0.5"]}, index, {"asset:9": []}, {"asset:9": 2})
    assert out["shared_with"] == 1
    assert out["state"] == pac.STATE_MATCHED_EMPTY


# ── 5. the states an empty cell would otherwise conflate ─────────────────────

def test_matched_with_no_attributes_is_not_the_same_as_unmatched():
    index = pac.build_index([_asset(9, "C", "10.0.0.5")], [])
    assert pac.match({"ips": ["10.0.0.5"]}, index, {"asset:9": []})["state"] == pac.STATE_MATCHED_EMPTY
    assert pac.match({"ips": ["10.9.9.9"]}, index, {})["state"] == pac.STATE_UNMATCHED


def test_matched_but_not_read_is_its_own_state():
    """Over the per-pass fetch cap. Rendering it as "no attributes" would be a claim the
    code has no basis for."""
    index = pac.build_index([_asset(9, "C", "10.0.0.5")], [])
    for mapping in ({}, {"asset:9": None}):
        assert pac.match({"ips": ["10.0.0.5"]}, index,
                         mapping)["state"] == pac.STATE_NOT_FETCHED


def test_a_row_with_no_address_and_no_id_is_unmatched_not_an_error():
    """Agent-synced Nutanix and XCP-ng report no IPs at all."""
    index = pac.build_index([_asset()], [])
    assert pac.match({"ips": [], "ps_system_id": ""}, index, {})["state"] == pac.STATE_UNMATCHED


def test_the_basis_is_recorded_so_an_operator_can_tell_the_joins_apart():
    index = pac.build_index([_asset(12, "W", "10.0.0.4")], [_ssm_system(7)])
    assert pac.match({"ps_system_id": "7"}, index, {"managed_system:7": []})["basis"] == pac.BASIS_ID
    assert pac.match({"ips": ["10.0.0.4"]}, index, {"asset:12": []})["basis"] == pac.BASIS_IP


# ── shaping ──────────────────────────────────────────────────────────────────

# BeyondInsight's real shape, captured from a live tenant 2026-09-23. The ORIENTATION is
# the whole point of these: ShortName is the VALUE, and the type is the KEY.
_TYPES = pac.type_names([{"AttributeTypeID": 3, "Name": "Criticality"},
                         {"AttributeTypeID": 10000, "Name": "Status"}])
_ATTR_ONLINE = {"AttributeID": 10000, "AttributeTypeID": 10000, "ShortName": "Online",
                "LongName": "Online", "ValueInt": 0}
_ESC = chr(27)   # built, not written as a literal — an editing tool eats one level


def test_an_attribute_value_cannot_replay_ansi_into_a_browser():
    """Attribute values are operator-typed text from another product."""
    chips = pac.to_chips(
        [{"AttributeTypeID": 3, "ShortName": _ESC + "[31mred" + _ESC + "[0m"}], _TYPES)
    assert _ESC not in chips[0]["value"]


def test_the_type_is_the_key_and_the_short_name_is_the_value():
    """The correction live inspection forced, and the reason this test file exists.

    An attribute row carries only its ``AttributeTypeID``; ``GET AttributeTypes`` says
    10000 is "Status" and ``ShortName`` says the value is "Online". Read the other way
    round it renders ``Online=`` — an empty value, and no sign of the type, which is the
    half a Smart Rule actually filters on.
    """
    chips = pac.to_chips([_ATTR_ONLINE], _TYPES)
    assert len(chips) == 1
    assert chips[0]["key"] == "Status" and chips[0]["value"] == "Online"


def test_attributes_render_as_password_safe_chips():
    """tag_policy already knows Password Safe calls these attributes, so there is no
    second chip renderer and the noun is right without a lookup table here."""
    chips = pac.to_chips([{"AttributeTypeID": 3, "ShortName": "High"}], _TYPES)
    assert chips[0]["key"] == "Criticality" and chips[0]["value"] == "High"
    assert "attribute" in chips[0]["title"].lower()


def test_an_unknown_type_shows_the_value_rather_than_vanishing():
    """A vocabulary gap should cost the label, not the fact that the attribute is set."""
    chips = pac.to_chips([{"AttributeTypeID": 999, "ShortName": "Online"}], _TYPES)
    assert [c["key"] for c in chips] == ["Online"]
    assert chips[0]["value"] == ""


def test_type_names_indexes_the_vocabulary_by_id():
    assert _TYPES["3"] == "Criticality"
    assert pac.type_names([{"Name": "no id"}, {"AttributeTypeID": 1}, "junk"]) == {}


def test_a_valueless_attribute_row_is_dropped_rather_than_rendered_blank():
    assert pac.to_chips([{"AttributeTypeID": 3}, {"ShortName": ""}], _TYPES) == []


def test_the_attribute_count_is_capped():
    rows = [{"AttributeTypeID": 3, "ShortName": f"v{i}"}
            for i in range(pac.MAX_ATTRIBUTES + 20)]
    assert len(pac.to_chips(rows, _TYPES)) <= pac.MAX_ATTRIBUTES


def test_the_matched_payload_is_a_closed_shape():
    index = pac.build_index([_asset()], [])
    out = pac.match({"ips": ["10.0.0.4"]}, index, {"asset:12": []})
    assert set(out) == set(pac.MATCH_KEYS)
    for obj in out["objects"]:
        assert set(obj) == set(pac.MATCHED_OBJECT_KEYS)


# ── orphans ──────────────────────────────────────────────────────────────────

def test_unmatched_lists_only_records_nothing_claimed():
    index = pac.build_index([_asset(1, "KEPT", "10.0.0.4"), _asset(2, "GONE", "10.0.9.9")], [])
    names = [o["name"] for o in pac.unmatched(index, {"asset:1"})]
    assert names == ["GONE"]


def test_unmatched_is_capped():
    index = pac.build_index([_asset(i, f"A{i}", f"10.0.1.{i}") for i in range(1, 30)], [])
    assert len(pac.unmatched(index, set(), cap=5)) == 5


# ── the index ────────────────────────────────────────────────────────────────

def test_a_row_without_an_id_is_skipped_rather_than_keyed_on_none():
    index = pac.build_index([{"AssetName": "no id", "IPAddress": "10.0.0.7"}], [])
    assert index["objects"] == {}


def test_a_duplicate_object_is_indexed_once():
    index = pac.build_index([_asset(), _asset()], [])
    assert len(index["objects"]) == 1


def test_a_non_dict_row_does_not_break_the_index():
    """The rows come from another product's API; one malformed entry must not cost the
    whole page."""
    index = pac.build_index(["nonsense", None, _asset()], [])
    assert len(index["objects"]) == 1


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
