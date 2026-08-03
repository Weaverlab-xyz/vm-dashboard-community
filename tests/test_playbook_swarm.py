"""Invariants for the Docker Swarm playbooks in examples/playbooks/swarm/.

These plays drive the `docker` CLI rather than `community.docker`, because that
collection is not in the runner image's documented set AND its modules would need the
Docker SDK for Python on every target (which linux/install-docker.yml does not
install). Shelling out means Ansible gives us no idempotency for free, so these
assertions pin what we hand-rolled instead:

  * every docker command declares `changed_when` (or `creates`) — otherwise a
    read-only probe reports "changed" on every run and the plays are never idempotent;
  * the plays match the linux/ contract (`hosts: all`, `become: true`), since they
    configure hosts over SSH;
  * `swarm-leave.yml` is gated on an explicit confirm var — it can destroy a cluster;
  * no `community.docker` module usage creeps back in. That's the constraint most
    likely to be "helpfully" undone by someone who doesn't know why it's there.

The beyondtrust-module invariants (delegate_to / no_log) live in
test_playbook_ps_lookup.py instead — they apply to every playbook, not just these.

Run: python tests/test_playbook_swarm.py   (or under pytest)
"""
import glob
import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SWARM_DIR = os.path.join(_ROOT, "examples", "playbooks", "swarm")
_PLAYBOOKS = sorted(glob.glob(os.path.join(_SWARM_DIR, "*.yml")))

# Modules that would drag in the Docker SDK for Python on the target.
_FORBIDDEN = ("community.docker", "docker_swarm", "docker_stack", "docker_node",
              "docker_container", "docker_swarm_service")

_CMD_KEYS = ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell")


def _rel(path):
    return os.path.relpath(path, _ROOT)


def _plays():
    for path in _PLAYBOOKS:
        for play in (yaml.safe_load(open(path, encoding="utf-8").read()) or []):
            yield path, play


def _tasks(play):
    return play.get("tasks") or []


def _command_of(task):
    """The command string for a command/shell task, else None."""
    for key in _CMD_KEYS:
        if key in task:
            val = task[key]
            if isinstance(val, dict):          # cmd:/argv: form
                return str(val.get("cmd") or val.get("argv") or "")
            return str(val)
    return None


def test_swarm_playbooks_exist():
    assert _PLAYBOOKS, "no playbooks found in examples/playbooks/swarm/"


def test_plays_match_the_linux_contract():
    """These configure hosts over SSH, like examples/playbooks/linux/."""
    for path, play in _plays():
        assert play.get("hosts") == "all", f"{_rel(path)}: hosts is not 'all'"
        assert play.get("become") is True, f"{_rel(path)}: become is not true"


def test_every_docker_command_declares_changed_when():
    """Shelling out gives no change detection, so each command must say what a
    change means — or the play reports changed on every single run."""
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or "docker" not in cmd:
                continue
            if "changed_when" not in task and "creates" not in task:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "docker command without changed_when/creates:\n  " + "\n  ".join(offenders)


def test_probes_are_marked_unchanged():
    """`docker info`/`ls`/`--version` read state; they must be changed_when: false."""
    readonly = ("docker info", "docker node ls", "docker service ls", "docker stack ls",
                "docker stack services", "docker --version", "docker swarm join-token")
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or not any(r in cmd for r in readonly):
                continue
            if task.get("changed_when") is not False:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "read-only docker probe not marked changed_when: false:\n  " + "\n  ".join(offenders)


def test_no_community_docker_usage():
    for path in _PLAYBOOKS:
        text = open(path, encoding="utf-8").read()
        for bad in _FORBIDDEN:
            assert bad not in text, (
                f"{_rel(path)} references {bad!r}. The runner image's documented "
                f"collection set does not include community.docker, and its modules "
                f"need the Docker SDK for Python on the target — use the docker CLI.")


def test_leave_is_gated_on_confirmation():
    """swarm-leave.yml can destroy a whole cluster (last manager) — it must refuse
    to act without an explicit opt-in."""
    path = os.path.join(_SWARM_DIR, "swarm-leave.yml")
    assert os.path.exists(path), "swarm-leave.yml is missing"
    play = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    assert (play.get("vars") or {}).get("confirm") is False, (
        "swarm-leave.yml must default confirm to false")
    # the destructive command must be conditioned on it
    for task in _tasks(play):
        cmd = _command_of(task)
        if cmd and "docker swarm leave" in cmd:
            when = yaml.safe_dump(task.get("when"))
            assert "confirm" in when, (
                "the `docker swarm leave` task is not gated on `confirm`")
            break
    else:
        raise AssertionError("no `docker swarm leave` task found")


def test_join_token_is_not_echoed():
    """The join token is a cluster credential. swarm-join.yml puts it on a command
    line, so that task must no_log."""
    path = os.path.join(_SWARM_DIR, "swarm-join.yml")
    play = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    for task in _tasks(play):
        cmd = _command_of(task)
        if cmd and "docker swarm join " in cmd:
            assert task.get("no_log") is True, (
                "the `docker swarm join` task passes the token on the command line "
                "and must set no_log: true")
            return
    raise AssertionError("no `docker swarm join` task found in swarm-join.yml")


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
            print(f"FAIL {_t.__name__}: {_e}")
    sys.exit(1 if _failures else 0)
