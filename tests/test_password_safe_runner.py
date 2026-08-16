"""Unit tests for services/password_safe_runner (the pure core).

Pure — loaded by file path (stdlib only). Runs under pytest, or standalone:
    python tests/test_password_safe_runner.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "password_safe_runner.py")
_spec = importlib.util.spec_from_file_location("password_safe_runner", _PATH)
psr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(psr)

_ENABLED = dict(enabled=True, api_url_raw="https://ps.example.com",
                client_id="cid", client_secret="csecret")


def test_full_env_when_configured():
    env = psr.build_runner_env(**_ENABLED)
    assert env == {
        "PASSWORD_SAFE_API_URL": "https://ps.example.com/BeyondTrust/api/public/v3",
        "PASSWORD_SAFE_CLIENT_ID": "cid",
        "PASSWORD_SAFE_CLIENT_SECRET": "csecret",
    }
    assert env[psr.SECRET_KEY] == "csecret"


def test_empty_when_disabled():
    assert psr.build_runner_env(**{**_ENABLED, "enabled": False}) == {}


def test_empty_when_any_cred_missing():
    assert psr.build_runner_env(**{**_ENABLED, "api_url_raw": ""}) == {}
    assert psr.build_runner_env(**{**_ENABLED, "client_id": ""}) == {}
    assert psr.build_runner_env(**{**_ENABLED, "client_secret": ""}) == {}
    # whitespace-only counts as missing
    assert psr.build_runner_env(**{**_ENABLED, "client_secret": "   "}) == {}


def test_none_creds_are_safe():
    assert psr.build_runner_env(enabled=True, api_url_raw=None,
                                client_id=None, client_secret=None) == {}


def test_api_url_normalization():
    # bare host → https + public-API path appended
    assert psr._normalize_api_url("ps.example.com") == \
        "https://ps.example.com/BeyondTrust/api/public/v3"
    # trailing slash trimmed
    assert psr._normalize_api_url("https://ps.example.com/") == \
        "https://ps.example.com/BeyondTrust/api/public/v3"
    # already-full path is left as-is (case-insensitive match)
    full = "https://ps.example.com/BeyondTrust/api/public/v3"
    assert psr._normalize_api_url(full) == full
    assert psr._normalize_api_url("") == ""


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
