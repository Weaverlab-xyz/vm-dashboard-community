"""Invariants for the k3s playbooks in examples/playbooks/k3s/.

These plays drive the `k3s`/`kubectl` CLIs and the official install script rather than
any Kubernetes collection, because the VM runner image ships none and the modules
would need client libraries on the target. Shelling out means Ansible gives us no
idempotency for free, so these assertions pin what we hand-rolled:

  * every k3s/kubectl/installer command declares `changed_when` (or `creates`);
  * read-only probes use `changed_when: false`;
  * the plays match the linux/ contract (`hosts: all`, `become: true`);
  * `k3s-uninstall.yml` is gated on an explicit confirm var — it can destroy a cluster;
  * the node token and the kubeconfig never reach job output unguarded.

It also pins the kubeconfig rewrite, which is the one piece of real logic here: k3s
writes `server: https://127.0.0.1:6443`, and the dashboard registers a cluster by
parsing `clusters[].cluster.server`. Registering the loopback value would produce a
cluster nothing can reach, so the rewrite is exercised offline against a realistic
k3s kubeconfig.

The beyondtrust-module invariants (delegate_to / no_log) live in
test_playbook_ps_lookup.py — they apply to every playbook, not just these.

Run: python tests/test_playbook_k3s.py   (or under pytest)
"""
import base64
import glob
import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_K3S_DIR = os.path.join(_ROOT, "examples", "playbooks", "k3s")
_PLAYBOOKS = sorted(glob.glob(os.path.join(_K3S_DIR, "*.yml")))

_CMD_KEYS = ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell")

# A realistic k3s admin kubeconfig, as k3s writes it to /etc/rancher/k3s/k3s.yaml.
_K3S_KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- name: default
  cluster:
    server: https://127.0.0.1:6443
    certificate-authority-data: TEST_CA
contexts:
- name: default
  context:
    cluster: default
    user: default
current-context: default
users:
- name: default
  user:
    client-certificate-data: TEST_CRT
    client-key-data: TEST_KEY
"""


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


def test_k3s_playbooks_exist():
    assert _PLAYBOOKS, "no playbooks found in examples/playbooks/k3s/"


def test_plays_match_the_linux_contract():
    """These configure hosts over SSH, like examples/playbooks/linux/."""
    for path, play in _plays():
        assert play.get("hosts") == "all", f"{_rel(path)}: hosts is not 'all'"
        assert play.get("become") is True, f"{_rel(path)}: become is not true"


def test_every_command_declares_changed_when():
    """Shelling out gives no change detection, so each command must say what a change
    means — or the play reports changed on every single run."""
    interesting = ("k3s", "kubectl", "get.k3s.io", "systemctl", "ufw")
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or not any(i in cmd for i in interesting):
                continue
            if "changed_when" not in task and "creates" not in task:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "command without changed_when/creates:\n  " + "\n  ".join(offenders)


def test_probes_are_marked_unchanged():
    """State probes read, they don't mutate."""
    readonly = ("systemctl is-active", "k3s --version", "k3s kubectl get", "ufw status")
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or not any(r in cmd for r in readonly):
                continue
            if task.get("changed_when") is not False:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "read-only probe not marked changed_when: false:\n  " + "\n  ".join(offenders)


def test_uninstall_is_gated_on_confirmation():
    """k3s-uninstall.yml wipes cluster state — on a single-server cluster, all of it."""
    path = os.path.join(_K3S_DIR, "k3s-uninstall.yml")
    assert os.path.exists(path), "k3s-uninstall.yml is missing"
    play = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    assert (play.get("vars") or {}).get("confirm") is False, (
        "k3s-uninstall.yml must default confirm to false")
    found = 0
    for task in _tasks(play):
        cmd = _command_of(task)
        if cmd and "uninstall.sh" in cmd:
            found += 1
            when = yaml.safe_dump(task.get("when"))
            assert "confirm" in when, (
                f"the uninstall task {task.get('name')!r} is not gated on `confirm`")
    assert found, "no uninstall task found"


def test_join_installs_with_the_token_hidden():
    """k3s-join.yml passes the node token via the environment — that task must no_log."""
    path = os.path.join(_K3S_DIR, "k3s-join.yml")
    play = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    for task in _tasks(play):
        if "K3S_TOKEN" in yaml.safe_dump(task.get("environment") or {}):
            assert task.get("no_log") is True, (
                "the install task carries K3S_TOKEN in its environment and must no_log")
            return
    raise AssertionError("no task passing K3S_TOKEN found in k3s-join.yml")


def test_kubeconfig_rewrite_produces_a_registerable_document():
    """The rewrite must replace k3s's loopback server with the node's real address,
    preserve the CA and client credentials, and leave the document resolvable by the
    same logic k8s_service._parse_api_server uses at registration time."""
    try:
        from jinja2 import Environment
    except ModuleNotFoundError:                      # pragma: no cover
        print("SKIP: jinja2 unavailable")
        return

    play = yaml.safe_load(open(os.path.join(_K3S_DIR, "k3s-kubeconfig.yml"),
                               encoding="utf-8").read())[0]
    expr = None
    for task in _tasks(play):
        facts = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
        if "_kubeconfig" in facts:
            expr = facts["_kubeconfig"]
            break
    assert expr, "no task sets the _kubeconfig fact"

    env = Environment()
    env.filters["b64decode"] = lambda s: base64.b64decode(s).decode()
    env.filters["from_yaml"] = yaml.safe_load
    env.filters["to_nice_yaml"] = lambda o: yaml.safe_dump(o, default_flow_style=False)
    env.filters["combine"] = lambda a, b: {**a, **b}

    rendered = env.from_string(expr).render(
        kubeconfig_raw={"content": base64.b64encode(_K3S_KUBECONFIG.encode()).decode()},
        _api_addr="10.0.0.11", api_port=6443)

    doc = yaml.safe_load(rendered)
    assert isinstance(doc, dict), "the rewrite did not produce a YAML mapping"

    # Exactly what k8s_service._parse_api_server does: current-context → cluster → server.
    ctx = next(c for c in doc["contexts"] if c["name"] == doc["current-context"])
    cluster = next(c for c in doc["clusters"] if c["name"] == ctx["context"]["cluster"])
    assert cluster["cluster"]["server"] == "https://10.0.0.11:6443", (
        f"server was not rewritten: {cluster['cluster']['server']!r}")

    # Credentials must survive the round-trip, or the kubeconfig authenticates nothing.
    assert cluster["cluster"]["certificate-authority-data"] == "TEST_CA"
    assert doc["users"][0]["user"]["client-certificate-data"] == "TEST_CRT"
    assert doc["users"][0]["user"]["client-key-data"] == "TEST_KEY"


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
