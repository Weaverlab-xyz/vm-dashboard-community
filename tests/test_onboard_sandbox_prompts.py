"""onboard-sandbox.sh says what is missing when it cannot prompt for it.

With no terminal (CI, a pipe) every ``read`` prompt hits EOF and fails, and the script runs
under ``set -e``: it ended there, exit 1, with no message -- before the check right after
the prompt could say "The setup token is required". Each prompt now ends in ``|| true``,
so an empty answer reaches that check.

The script talks to a stub dashboard here: ``/api/setup/status`` answers as configured,
and every POST is recorded, so a run that should have stopped short is caught too.

Runs standalone:
    python tests/test_onboard_sandbox_prompts.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(_ROOT, "scripts", "sandbox", "Linux", "onboard-sandbox.sh")
_MISSING = [c for c in ("bash", "jq", "curl") if not shutil.which(c)]


class _Stub(BaseHTTPRequestHandler):
    complete = False
    posts: list = []

    def _json(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._json({"complete": _Stub.complete, "configured_keys": []})

    def do_POST(self):  # noqa: N802
        _Stub.posts.append(self.path)
        if self.path == "/api/auth/login":
            self._json({})                      # no access_token: the login "fails"
        else:
            self.send_response(500)
            self.end_headers()

    def log_message(self, *_a):
        pass


def _run(*extra, complete):
    _Stub.complete, _Stub.posts = complete, []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = tempfile.mkdtemp(prefix="onboard-prompts-")
    try:
        os.makedirs(os.path.join(state, "aws"))
        with open(os.path.join(state, "aws", "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"aws_default_region": "eu-west-1"}, fh)
        env = {k: v for k, v in os.environ.items() if k != "SETUP_TOKEN"}
        env["SANDBOX_STATE_DIR"] = state
        return subprocess.run(
            ["bash", SCRIPT, "--cloud", "aws", "--push-only",
             "--dashboard-url", f"http://127.0.0.1:{server.server_port}", *extra],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)
    finally:
        server.shutdown()
        shutil.rmtree(state, ignore_errors=True)


def test_a_first_run_without_a_setup_token_says_so():
    r = _run("--admin-user", "a", "--admin-pass", "p", complete=False)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "The setup token is required" in r.stderr, r.stderr
    assert _Stub.posts == [], f"posted without a setup token: {_Stub.posts}"


def test_a_rerun_without_credentials_says_the_login_failed():
    r = _run(complete=True)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "Login failed" in r.stderr, r.stderr
    assert "/api/setup/import" not in _Stub.posts


if __name__ == "__main__":
    if _MISSING:
        print(f"SKIP all: {', '.join(_MISSING)} not installed")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
