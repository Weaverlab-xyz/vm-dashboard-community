"""Unit tests for the Portainer migration bundle.

The bundle is the contract between the export CLI (runs on an operator's machine)
and the ``portainer_import`` job (runs in the app), so both halves import
``bundle.py`` and both are pinned here:

  * ``scrub`` — a bundle is a file an operator emails to themselves, so any
    credential-shaped field is dropped on the way OUT rather than trusted on the
    way in. Nested and list-nested cases included, because Portainer buries LDAP
    and OAuth secrets inside ``settings``.
  * ``build`` — endpoints are recorded as REFERENCE and never as importable data;
    that separation is the whole reason the import can't manufacture dead
    environments.
  * ``validate`` — a hand-edited bundle must fail with a sentence, not a KeyError
    halfway through writing to a live Portainer.

Pure module, no HTTP and no app imports. Runs under pytest or standalone:

    python tests/test_portainer_migrate_bundle.py
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.scripts.portainer_migrate import bundle as bundle_mod  # noqa: E402


def _sample(**over):
    data = {
        "users": [{"Id": 1, "Username": "admin", "Role": 1}],
        "teams": [{"Id": 1, "Name": "lab"}],
        "team_memberships": [{"Id": 1, "UserID": 1, "TeamID": 1, "Role": 2}],
        "registries": [],
        "stacks": [],
    }
    data.update(over)
    return data


# ── scrub ────────────────────────────────────────────────────────────────────

def test_scrub_drops_top_level_credentials():
    out = bundle_mod.scrub({"Username": "admin", "Password": "$2a$10$hash",
                            "Id": 1})
    assert out == {"Username": "admin", "Id": 1}


def test_scrub_is_case_and_underscore_insensitive():
    """Portainer's field casing is inconsistent across resources, and a bundle a
    human edited may use snake_case — neither may become a leak."""
    out = bundle_mod.scrub({"password": "x", "PASSWORD": "x", "PasswordHash": "x",
                            "access_token": "x", "AccessToken": "x",
                            "TLSCert": "x", "keep": "yes"})
    assert out == {"keep": "yes"}, out


def test_scrub_reaches_nested_and_listed_objects():
    """``settings`` hides the LDAP bind password one level down, and endpoint access
    policies are lists of objects."""
    src = {"LDAPSettings": {"Password": "secret", "AnonymousMode": True},
           "registries": [{"Name": "harbor", "Password": "secret", "Id": 3}],
           "deep": [[{"Secret": "no"}]]}
    out = bundle_mod.scrub(src)
    assert out["LDAPSettings"] == {"AnonymousMode": True}
    assert out["registries"] == [{"Name": "harbor", "Id": 3}]
    assert out["deep"] == [[{}]]
    assert "secret" not in json.dumps(out)


def test_scrub_does_not_mutate_its_input():
    src = {"Password": "secret", "Id": 1}
    bundle_mod.scrub(src)
    assert src == {"Password": "secret", "Id": 1}


def test_scrub_passes_scalars_through():
    assert bundle_mod.scrub("plain") == "plain"
    assert bundle_mod.scrub(7) == 7
    assert bundle_mod.scrub(None) is None


# ── build ────────────────────────────────────────────────────────────────────

def test_build_records_endpoints_as_reference_never_as_data():
    """The one structural guarantee: an endpoint can be SEEN in the bundle but can
    never be replayed, because a cloud node has no route to a LAN Docker socket."""
    doc = bundle_mod.build(
        source_url="https://localhost:9443", source_version="2.21.0",
        data=_sample(), reference={"endpoints": [
            {"Id": 1, "Name": "local", "URL": "unix:///var/run/docker.sock"}]})
    assert "endpoints" not in doc["data"], doc["data"].keys()
    assert doc["reference"]["endpoints"][0]["URL"] == "unix:///var/run/docker.sock"
    assert set(doc["data"]) == set(bundle_mod.SECTIONS)


def test_build_scrubs_secrets_out_of_both_halves():
    doc = bundle_mod.build(
        source_url="u", source_version="v",
        data=_sample(users=[{"Id": 1, "Username": "a", "Password": "LEAK"}]),
        reference={"settings": {"LDAPSettings": {"Password": "LEAK"}}})
    assert "LEAK" not in json.dumps(doc)


def test_build_counts_only_importable_sections():
    doc = bundle_mod.build(source_url="u", source_version="v", data=_sample(),
                           reference={"endpoints": [{"Id": 1}, {"Id": 2}]})
    assert doc["meta"]["counts"] == {"users": 1, "teams": 1, "team_memberships": 1,
                                     "registries": 0, "stacks": 0}


def test_build_normalizes_missing_sections_to_empty_lists():
    """An absent section must not become a null — the importer iterates these."""
    doc = bundle_mod.build(source_url="u", source_version="v",
                           data={"users": [{"Id": 1}]}, reference={})
    for name in bundle_mod.SECTIONS:
        assert isinstance(doc["data"][name], list), name


def test_build_always_explains_what_did_not_come_across():
    doc = bundle_mod.build(source_url="u", source_version="v", data=_sample(),
                           reference={})
    joined = " ".join(doc["not_migrated"]).lower()
    assert "edge agent" in joined, "the remedy for connections must be named"
    assert "password" in joined


def test_the_section_order_is_dependency_order():
    """Teams and users must precede memberships, and stacks come last because they
    need a live environment to deploy onto."""
    order = list(bundle_mod.SECTIONS)
    assert order.index("users") < order.index("team_memberships")
    assert order.index("teams") < order.index("team_memberships")
    assert order[-1] == "stacks"


# ── validate ─────────────────────────────────────────────────────────────────

def test_validate_accepts_a_built_bundle():
    doc = bundle_mod.build(source_url="u", source_version="v", data=_sample(),
                           reference={})
    assert bundle_mod.validate(doc) == []


def test_validate_rejects_a_foreign_schema():
    doc = bundle_mod.build(source_url="u", source_version="v", data=_sample(),
                           reference={})
    doc["schema"] = 99
    problems = bundle_mod.validate(doc)
    assert any("schema" in p for p in problems), problems


def test_validate_rejects_a_non_object_bundle():
    assert bundle_mod.validate([1, 2, 3])
    assert bundle_mod.validate("nope")


def test_validate_rejects_a_missing_data_object():
    assert any("'data'" in p for p in bundle_mod.validate({"schema": 1}))


def test_validate_rejects_a_section_of_the_wrong_shape():
    """A hand-edited bundle is the realistic source of this, and the importer would
    otherwise fail mid-replay with the target already half-written."""
    doc = bundle_mod.build(source_url="u", source_version="v", data=_sample(),
                           reference={})
    doc["data"]["users"] = {"not": "a list"}
    assert any("data.users must be a list" in p for p in bundle_mod.validate(doc))
    doc["data"]["users"] = ["a bare string"]
    assert any("data.users[0]" in p for p in bundle_mod.validate(doc))


def test_validate_rejects_an_entirely_empty_bundle():
    """The realistic case: a scratch Portainer the restore never actually landed in.
    Writing that would look like a successful export of nothing."""
    doc = bundle_mod.build(source_url="u", source_version="v", data={}, reference={})
    assert any("empty" in p for p in bundle_mod.validate(doc))


# ── write / read ─────────────────────────────────────────────────────────────

def test_write_round_trips_and_is_owner_only():
    doc = bundle_mod.build(source_url="u", source_version="v", data=_sample(),
                           reference={})
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "bundle.json")
        written = bundle_mod.write(path, doc)
        assert bundle_mod.read(written) == doc
        if os.name != "nt":
            # Windows does not honour POSIX modes; the mode is still requested at
            # creation so a WSL/Linux operator gets 0600.
            assert oct(os.stat(written).st_mode)[-3:] == "600"


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"PASS {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e!r}")
    print(f"\n{len(_tests) - _failures}/{len(_tests)} passed")
    sys.exit(1 if _failures else 0)
