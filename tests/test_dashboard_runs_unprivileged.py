"""The dashboard image runs its app as an unprivileged user, not root.

The entrypoint starts as root only to adapt to the host (the Docker socket's group,
volumes earlier root releases wrote), then drops to `dashboard` (uid/gid 10001) with
setpriv before gunicorn or the worker start. CI's image job proves it at runtime: every
process in the booted container is uid 10001. These are the static halves, which a
contributor can break without building an image:

  * the image creates the user and the directories it writes, and ships the entrypoint;
  * the entrypoint ends in a setpriv to that user that keeps no capabilities, and every
    other path out of it is either "already unprivileged" or the explicit opt-out;
  * the Packer plugins are installed somewhere that user can read, not root's home.

The entrypoint itself needs root to exercise, which CI's test runner does not have; it was
exercised directly when written (socket groups root:<gid> and root:root, restart,
opt-out, started-as-non-root).

Runs under pytest, or standalone:
    python tests/test_dashboard_runs_unprivileged.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCKERFILE = os.path.join(_ROOT, "Dockerfile")
_ENTRYPOINT = os.path.join(_ROOT, "docker", "entrypoint.sh")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_image_creates_the_user_and_its_writable_dirs():
    src = _read(_DOCKERFILE)
    assert re.search(r"groupadd -g 10001 dashboard", src), "no dashboard group (gid 10001)"
    assert re.search(r"useradd -u 10001 -g dashboard\b", src), "no dashboard user (uid 10001)"
    for d in ("/app/terraform/deployments", "/app/packer/builds"):
        assert d in src, f"{d} is not created for the dashboard user"
    assert "COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh" in src
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in src


def test_the_image_does_not_pin_user_root_or_skip_the_entrypoint():
    """No USER line: the entrypoint must start as root to read the socket's group. A USER
    root would be harmless but misleading; a USER dashboard would skip the volume
    hand-over that upgraded installs need. Either is a deliberate change, made here."""
    users = re.findall(r"^USER\s+(\S+)", _read(_DOCKERFILE), re.MULTILINE)
    assert not users, f"the dashboard image sets USER {users}; see docker/entrypoint.sh"


def test_packer_plugins_live_outside_roots_home():
    src = _read(_DOCKERFILE)
    env = src.find("ENV PACKER_PLUGIN_PATH=")
    install = src.find('packer plugins install "github.com/')  # the command, not a comment
    assert env != -1, "PACKER_PLUGIN_PATH is not set, so plugins land in /root"
    assert env < install, "PACKER_PLUGIN_PATH is set after the plugins are installed"


def test_the_entrypoint_drops_to_the_app_user_with_no_capabilities():
    src = _read(_ENTRYPOINT)
    lines = [ln.strip() for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert lines[0].startswith("set -eu"), "the entrypoint must stop on the first error"
    last = lines[-1]
    assert last.startswith("exec setpriv "), f"the entrypoint does not end by dropping root: {last}"
    for flag in ('--reuid="$APP_USER"', '--regid="$APP_GID"', '--groups=',
                 "--inh-caps=-all", "--no-new-privs"):
        assert flag in last, f"the final setpriv lacks {flag}"
    assert "APP_USER=dashboard" in src


def test_every_other_exit_is_already_unprivileged_or_the_opt_out():
    """An `exec "$@"` that runs the command as root is the regression to catch. Exactly two
    are allowed: when the script was started as non-root, and the documented opt-out."""
    src = _read(_ENTRYPOINT)
    plain = [m.start() for m in re.finditer(r'^\s*exec "\$@"', src, re.MULTILINE)]
    assert len(plain) == 2, f"expected 2 plain execs (non-root start, opt-out), found {len(plain)}"
    first, second = plain
    assert 'if [ "$(id -u)" != "0" ]; then' in src[:first], "first plain exec is not the non-root path"
    assert 'DASHBOARD_RUN_AS_ROOT' in src[first:second], "second plain exec is not the opt-out"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
