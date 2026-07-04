"""Unit tests for services/cloud_ansible_secrets.py (manifest + command snippet).

Pure, loaded by file path (stdlib only).
Runs under pytest, or standalone:  python tests/test_cloud_ansible_secrets.py
"""
import base64
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "cloud_ansible_secrets.py")
_spec = importlib.util.spec_from_file_location("cloud_ansible_secrets", _PATH)
cas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cas)


def test_build_manifest_assigns_env_names_and_encodes():
    env_names, manifest_b64 = cas.build_manifest(["db_password", "ansible_become_password"])
    assert env_names == ["DASH_SECRET_0", "DASH_SECRET_1"]
    entries = json.loads(base64.b64decode(manifest_b64))
    assert entries == [
        {"env": "DASH_SECRET_0", "var": "db_password"},
        {"env": "DASH_SECRET_1", "var": "ansible_become_password"},
    ]


def test_build_manifest_empty():
    env_names, manifest_b64 = cas.build_manifest([])
    assert env_names == [] and json.loads(base64.b64decode(manifest_b64)) == []


def test_manifest_round_trips_var_names():
    names = ["a", "weird.name", "x_y"]
    _, manifest_b64 = cas.build_manifest(names)
    entries = json.loads(base64.b64decode(manifest_b64))
    assert [e["var"] for e in entries] == names


def test_command_prefix_builds_vars_file_from_manifest_env():
    p = cas.command_prefix()
    assert p.strip().endswith("&&")
    assert cas.MANIFEST_ENV in p and cas.VARS_FILE in p
    # no literal secret in the snippet — only env references
    assert "os.environ" in p


def test_extra_vars_arg_points_at_vars_file():
    assert cas.extra_vars_arg().strip() == f"-e @{cas.VARS_FILE}"


def test_env_name_helper():
    assert cas.env_name(3) == "DASH_SECRET_3"


# ── Per-provider resolution ─────────────────────────────────────────────────────

def _fakes(store):
    """Build injected callables over a dict of {key_or_ref: raw_stored_value}.

    A registry key maps to a raw value that may itself be a reference; a reference
    string maps to its resolved plaintext. resolve/get return the plaintext.
    """
    resolved = {"aws_sm://db": "AWSVAL", "gcp_sm://db": "GCPVAL", "plainkey": "PLAINVAL",
                "aws_sm://kdb": "AWSVAL", "gcp_sm://kdb": "GCPVAL"}
    resolved.update({k: v for k, v in store.items()})

    def is_reference(s):
        return (s or "").startswith(("aws_sm://", "gcp_sm://", "azure_kv://", "bt_safe://"))

    def get_raw(key):        # registry key -> stored raw (may be a ref)
        return store.get(key, "")

    def get(key):            # registry key -> resolved plaintext
        raw = store.get(key, key)
        return resolved.get(raw, raw)

    def resolve_reference(ref):
        return resolved.get(ref, "")

    def parse_ref(raw, backend):
        prefix = {"aws_sm": "aws_sm://", "gcp_sm": "gcp_sm://"}[backend]
        return None, raw[len(prefix):]

    def aws_sm_arn(sref, vault_id):
        return f"arn:aws:secretsmanager:us-east-1:0:secret:{sref}-AbCdEf"

    return dict(is_reference=is_reference, get=get, get_raw=get_raw,
                resolve_reference=resolve_reference, parse_ref=parse_ref,
                aws_sm_arn=aws_sm_arn)


def test_bindings_named_then_become_ssh_excluded():
    b = cas.secret_bindings({"db_password": "aws_sm://db", "blank": "  "}, "aws_sm://sudo")
    assert b == [("db_password", "aws_sm://db"), ("ansible_become_password", "aws_sm://sudo")]


def test_resolve_ecs_requires_aws_sm_and_returns_arn():
    f = _fakes({})
    entries, manifest_b64, inline = cas.resolve_entries(
        "ecs", {"db_password": "aws_sm://db"}, "", **f)
    assert entries == [{"env": "DASH_SECRET_0",
                        "arn": "arn:aws:secretsmanager:us-east-1:0:secret:db-AbCdEf"}]
    assert inline == []  # ECS never resolves the value in-app
    assert [e["var"] for e in json.loads(base64.b64decode(manifest_b64))] == ["db_password"]


def test_resolve_ecs_registry_key_pointing_at_aws_sm():
    f = _fakes({"mykey": "aws_sm://kdb"})   # registry key whose raw value is a ref
    entries, _, _ = cas.resolve_entries("ecs", {"db_password": "mykey"}, "", **f)
    assert entries[0]["arn"].endswith("kdb-AbCdEf")


def test_resolve_ecs_rejects_non_aws_sm():
    f = _fakes({"mykey": "PLAINVAL"})       # literal, not a store ref
    try:
        cas.resolve_entries("ecs", {"db_password": "mykey"}, "", **f)
        assert False, "expected StoreMismatch"
    except cas.StoreMismatch as e:
        assert e.var == "db_password" and e.prefix == "aws_sm://"
        assert "AWS Secrets Manager" in str(e)


def test_resolve_gcp_requires_gcp_sm_and_returns_short_name():
    f = _fakes({})
    entries, _, inline = cas.resolve_entries("gcp", {}, "gcp_sm://db", **f)
    assert entries == [{"env": "DASH_SECRET_0", "secret_name": "db"}]
    assert inline == []


def test_resolve_gcp_rejects_aws_sm():
    f = _fakes({})
    try:
        cas.resolve_entries("gcp", {"p": "aws_sm://db"}, "", **f)
        assert False
    except cas.StoreMismatch as e:
        assert e.runner == "gcp"


def test_resolve_aci_inline_value_and_scrub_set():
    f = _fakes({})
    entries, _, inline = cas.resolve_entries(
        "aci", {"db_password": "aws_sm://db"}, "plainkey", **f)
    assert entries == [{"env": "DASH_SECRET_0", "value": "AWSVAL"},
                       {"env": "DASH_SECRET_1", "value": "PLAINVAL"}]
    assert inline == ["AWSVAL", "PLAINVAL"]  # both added to the scrub set


def test_resolve_empty_returns_nothing():
    f = _fakes({})
    assert cas.resolve_entries("ecs", {}, "", **f) == ([], "", [])


def test_validate_stores_noop_for_aci_and_local():
    f = _fakes({"mykey": "PLAINVAL"})
    cas.validate_stores("aci", {"p": "mykey"}, "",
                        is_reference=f["is_reference"], get_raw=f["get_raw"])
    cas.validate_stores("local", {"p": "mykey"}, "",
                        is_reference=f["is_reference"], get_raw=f["get_raw"])  # no raise


def test_validate_stores_raises_for_ecs_non_store():
    f = _fakes({"mykey": "PLAINVAL"})
    try:
        cas.validate_stores("ecs", {"p": "mykey"}, "",
                            is_reference=f["is_reference"], get_raw=f["get_raw"])
        assert False
    except cas.StoreMismatch as e:
        assert e.var == "p"


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
