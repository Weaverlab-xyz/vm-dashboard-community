"""Unit tests for services/portainer_runner (the pure core).

Mirrors test_password_safe_runner.py — the two modules are the same shape (auto-inject
a configured connection into the Ansible runner as env vars, with one sensitive value
the caller must scrub).

Pure — loaded by file path (stdlib only). Runs under pytest, or standalone:
    python tests/test_portainer_runner.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "portainer_runner.py")
_spec = importlib.util.spec_from_file_location("portainer_runner", _PATH)
ptr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ptr)

_ENABLED = dict(enabled=True, url="https://1.2.3.4:9443", pat="tok", verify_ssl=True)


def test_full_env_when_configured():
    env = ptr.build_runner_env(**_ENABLED)
    assert env == {
        "PORTAINER_URL": "https://1.2.3.4:9443",
        "PORTAINER_PAT": "tok",
        "PORTAINER_VERIFY_SSL": "1",
    }
    # The caller appends this to its scrub set — it must be the token, not the URL.
    assert env[ptr.SECRET_KEY] == "tok"


def test_empty_when_disabled():
    assert ptr.build_runner_env(**{**_ENABLED, "enabled": False}) == {}


def test_empty_when_connection_incomplete():
    # "Do not inject" — a half-configured connection must not reach the runner.
    assert ptr.build_runner_env(**{**_ENABLED, "url": ""}) == {}
    assert ptr.build_runner_env(**{**_ENABLED, "pat": ""}) == {}
    # whitespace-only counts as missing
    assert ptr.build_runner_env(**{**_ENABLED, "pat": "   "}) == {}


def test_trailing_slash_stripped():
    # Playbooks build "{{ url }}/api/…", so a stored trailing slash would produce
    # a double slash in every request path.
    env = ptr.build_runner_env(**{**_ENABLED, "url": "https://1.2.3.4:9443/"})
    assert env["PORTAINER_URL"] == "https://1.2.3.4:9443"


def test_verify_ssl_is_stringified_both_ways():
    # A managed node serves a self-signed cert, so the deploy sets verify off; the
    # playbooks read this with `| bool`, which accepts "1"/"0".
    assert ptr.build_runner_env(**{**_ENABLED, "verify_ssl": False})["PORTAINER_VERIFY_SSL"] == "0"
    assert ptr.build_runner_env(**{**_ENABLED, "verify_ssl": True})["PORTAINER_VERIFY_SSL"] == "1"


def test_secret_key_matches_the_emitted_key():
    # Guards the scrub contract: callers do env.get(SECRET_KEY), so a rename of one
    # without the other would silently stop scrubbing the token from job output.
    assert ptr.SECRET_KEY in ptr.build_runner_env(**_ENABLED)


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"ok   {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e!r}")
    sys.exit(1 if _failures else 0)
