"""Unit tests for services/secret_scan.py (artefact secret scanning).

Pure functions, loaded by file path (stdlib only).
Runs under pytest, or standalone:  python tests/test_secret_scan.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "secret_scan.py")
_spec = importlib.util.spec_from_file_location("secret_scan", _PATH)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)


def _rules(findings):
    return {f["rule"] for f in findings}


# ── catches real secrets ─────────────────────────────────────────────────────

def test_flags_aws_access_key_id():
    f = ss.scan_text("env:\n  AWS_KEY: AKIAIOSFODNN7EXAMPLE\n")
    assert "aws_access_key_id" in _rules(f)
    assert f[0]["line"] == 2 and "…" in f[0]["match"]  # redacted


def test_flags_private_key_block():
    f = ss.scan_text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz...\n")
    assert "private_key" in _rules(f)


def test_flags_generic_password_assignment():
    f = ss.scan_text('db:\n  password: "hunter2hunter2"\n')
    assert "generic_secret_assignment" in _rules(f)


def test_flags_github_and_google_tokens():
    f = ss.scan_text("a: ghp_" + "a" * 36 + "\nb: AIza" + "b" * 35 + "\n")
    assert {"github_token", "google_api_key"} <= _rules(f)


def test_redaction_hides_the_value():
    f = ss.scan_text("password: superSecretValue123")
    assert f and "superSecretValue123" not in f[0]["match"]


# ── does NOT cry wolf ────────────────────────────────────────────────────────

def test_skips_templated_and_placeholder_values():
    text = (
        'a: "{{ vault_db_password }}"\n'
        "b: ${DB_PASSWORD}\n"
        "c: $DB_PASSWORD\n"
        "d: <your-password-here>\n"
        "e: changeme\n"
        "password: example\n"
    )
    assert ss.scan_text(text) == []


def test_skips_ansible_vault_encrypted_file():
    text = "$ANSIBLE_VAULT;1.1;AES256\n66386439...\n"
    assert ss.scan_text(text) == []


def test_dedupes_per_rule_per_line():
    # same rule twice on one line → one finding
    f = ss.scan_text("password: aaaaaaaa  password: bbbbbbbb")
    assert len([x for x in f if x["rule"] == "generic_secret_assignment"]) == 1


# ── scan_bytes: text vs binary ───────────────────────────────────────────────

def test_scan_bytes_scans_text_ext():
    f = ss.scan_bytes(b"password: hunter2hunter2\n", "play.yml")
    assert "generic_secret_assignment" in _rules(f)


def test_scan_bytes_skips_binary_extension():
    assert ss.scan_bytes(b"AKIAIOSFODNN7EXAMPLE", "agent.rpm") == []


def test_scan_bytes_skips_nul_binary():
    assert ss.scan_bytes(b"AKIAIOSFODNN7EXAMPLE\x00\x00binary", "x.yml") == []


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
