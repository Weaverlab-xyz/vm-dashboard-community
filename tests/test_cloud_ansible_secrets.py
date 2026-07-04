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
