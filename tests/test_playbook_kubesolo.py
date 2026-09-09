"""Invariants for the KubeSolo playbooks in examples/playbooks/kubesolo/.

These plays drive the `kubesolo`, `kubectl` and `helm` CLIs rather than any collection,
because an SSH/VM target runs on the chrweav/ansible-winrm runner, which ships neither
kubernetes.core nor the helm and kubectl binaries — only chrweav/ansible-cloud does, and
that image is selected solely by a k8s/database target kind. Shelling out means Ansible
gives us no idempotency for free, so these assertions pin what we hand-rolled, the same
way tests/test_playbook_k3s.py does for k3s.

Unlike the k3s test, `_tasks` here FLATTENS block/rescue/always. entitle-agent-install.yml
wraps its install in a block so the values file is removed even when helm fails, and a
walker that only reads top-level tasks would silently exempt every command inside it.

Four assertions exist because the thing they pin already went wrong once, or would have:

`test_the_token_task_is_top_level` — tests/test_playbook_ps_lookup.py's no_log sweep
walks top-level tasks ONLY. The first draft of entitle-agent-install.yml had the
values-file task (the one task that touches the token) nested inside the install block,
where the sweep could not see it. It was caught, but only because the block wrapper
happened to serialise its children; move the task back down a level and the protection
silently disappears.

`test_single_node_defaults`, `test_datadog_is_off_at_both_switches` and
`test_platform_mode_is_native` — these are the findings the playbook exists to encode,
verified against chart 2.11.0 by rendering it. The chart defaults to THREE replicas with
no anti-affinity (so all three land on the one node and reserve 3 CPU / 3Gi of requests),
and `datadog.enabled: false` is already the chart default yet still injects a Datadog
sidecar into every pod — `datadog.sidecarLogs: false` is the actual switch. If someone
edits these values, the numbers in docs/kubesolo.md have to move with them.

Run: python tests/test_playbook_kubesolo.py   (or under pytest)
"""
import glob
import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KS_DIR = os.path.join(_ROOT, "examples", "playbooks", "kubesolo")
_PLAYBOOKS = sorted(glob.glob(os.path.join(_KS_DIR, "*.yml")))

_CMD_KEYS = ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell")

_EXPECTED = {
    "kubesolo-install.yml",
    "kubesolo-status.yml",
    "kubesolo-uninstall.yml",
    "entitle-agent-install.yml",
    "entitle-agent-uninstall.yml",
}

# Storage is a FLAT namespace — a run resolves an asset by bare filename, so a name
# collision anywhere under examples/playbooks/ would make the wrong play runnable.
_ALL_PLAYBOOKS = sorted(glob.glob(
    os.path.join(_ROOT, "examples", "playbooks", "**", "*.yml"), recursive=True))

_KUBECONFIG = "{{ kubesolo_path }}/pki/admin/admin.kubeconfig"
_VALUES_FILE = "/root/.entitle-agent-values.yaml"


def _rel(path):
    return os.path.relpath(path, _ROOT)


def _plays():
    for path in _PLAYBOOKS:
        for play in (yaml.safe_load(open(path, encoding="utf-8").read()) or []):
            yield path, play


def _tasks(play):
    """Every task in the play, descending into block/rescue/always."""
    out = []

    def walk(items):
        for task in (items or []):
            if not isinstance(task, dict):
                continue
            nested = False
            for key in ("block", "rescue", "always"):
                if key in task:
                    nested = True
                    walk(task[key])
            if not nested:
                out.append(task)

    walk(play.get("tasks"))
    return out


def _top_level_tasks(play):
    """Only what tests/test_playbook_ps_lookup.py's no_log sweep can see."""
    return [t for t in (play.get("tasks") or []) if isinstance(t, dict)]


def _command_of(task):
    """The command string for a command/shell task, else None."""
    for key in _CMD_KEYS:
        if key in task:
            val = task[key]
            if isinstance(val, dict):          # cmd:/argv: form
                return str(val.get("cmd") or val.get("argv") or "")
            return str(val)
    return None


def _play_of(name):
    path = os.path.join(_KS_DIR, name)
    return path, yaml.safe_load(open(path, encoding="utf-8").read())[0]


def _text_of(name):
    return open(os.path.join(_KS_DIR, name), encoding="utf-8").read()


def test_kubesolo_playbooks_exist():
    found = {os.path.basename(p) for p in _PLAYBOOKS}
    assert found == _EXPECTED, f"unexpected file set in examples/playbooks/kubesolo/: {found}"


def test_filenames_are_globally_unique():
    """A run picks an asset by bare filename out of a flat storage listing."""
    seen = {}
    for path in _ALL_PLAYBOOKS:
        name = os.path.basename(path)
        assert name not in seen, (
            f"duplicate playbook filename {name!r}: {_rel(path)} and {_rel(seen[name])}")
        seen[name] = path


def test_plays_match_the_linux_contract():
    """These configure hosts over SSH, like examples/playbooks/linux/ and k3s/."""
    for path, play in _plays():
        assert play.get("hosts") == "all", f"{_rel(path)}: hosts is not 'all'"
        assert play.get("become") is True, f"{_rel(path)}: become is not true"


def test_every_command_declares_changed_when():
    """Shelling out gives no change detection, so each command must say what a change
    means — or the play reports changed on every single run."""
    interesting = ("kubesolo", "kubectl", "helm", "get.kubesolo.io", "systemctl",
                   "update-ca-", "free -m", "du -sh")
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
    readonly = ("systemctl is-active", "kubectl get", "kubectl wait", "helm list",
                "helm status", "free -m", "du -sh", "kubectl describe")
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or not any(r in cmd for r in readonly):
                continue
            if task.get("changed_when") is not False:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "read-only probe not marked changed_when: false:\n  " + "\n  ".join(offenders)


def test_binaries_are_called_by_absolute_path():
    """RHEL's sudo secure_path excludes /usr/local/bin, so a bare `helm` works when you
    test it by hand and fails under become — the same reason pov_entitle_agent pins the
    absolute paths."""
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task) or ""
            for binary in ("helm", "kubectl"):
                for token in cmd.split():
                    if token == binary:
                        offenders.append(f"{_rel(path)}: {task.get('name')!r} calls bare {binary}")
    assert not offenders, "binary called without an absolute path:\n  " + "\n  ".join(offenders)


def test_uninstalls_are_gated_on_confirmation():
    """Both teardowns destroy something that is not recoverable from another node."""
    for name in ("kubesolo-uninstall.yml", "entitle-agent-uninstall.yml"):
        path, play = _play_of(name)
        assert play.get("vars", {}).get("confirm") is False, (
            f"{_rel(path)}: confirm must default to false")
        gated = [t for t in _tasks(play)
                 if "ansible.builtin.assert" in t and "confirm" in yaml.safe_dump(t)]
        assert gated, f"{_rel(path)}: no assert gating on confirm"


def test_kubeconfig_is_read_from_the_kubesolo_layout():
    """KubeSolo puts the admin kubeconfig under its state dir, not /etc/rancher."""
    for path, play in _plays():
        for task in _tasks(play):
            env = task.get("environment") or {}
            if "KUBECONFIG" not in env:
                continue
            assert env["KUBECONFIG"] == _KUBECONFIG, (
                f"{_rel(path)}: {task.get('name')!r} points KUBECONFIG at {env['KUBECONFIG']!r}")


# ── The token ────────────────────────────────────────────────────────────────

def test_the_token_task_is_top_level():
    """tests/test_playbook_ps_lookup.py's no_log sweep walks TOP-LEVEL tasks only. The
    one task that writes the token must stay where that sweep can see it."""
    path, play = _play_of("entitle-agent-install.yml")
    # The same exemptions the sweep itself applies: a `when:` or an assert/debug that
    # merely names the variable is not an interpolation into a module.
    exempt = ("name", "when", "no_log", "register", "loop", "loop_control",
              "ansible.builtin.assert", "assert", "ansible.builtin.debug", "debug")
    writers = [t for t in _top_level_tasks(play)
               if "_entitle_agent_token" in yaml.safe_dump(
                   {k: v for k, v in t.items() if k not in exempt})]
    assert writers, (
        f"{_rel(path)}: no top-level task handles the token — did it move into the "
        "block? The no_log sweep cannot see it there.")
    for task in writers:
        assert task.get("no_log") is True, (
            f"{_rel(path)}: {task.get('name')!r} handles the token without no_log")


def test_the_values_file_is_private_and_always_removed():
    path, play = _play_of("entitle-agent-install.yml")
    writer = next(t for t in _tasks(play) if "ansible.builtin.copy" in t)
    assert writer["ansible.builtin.copy"].get("mode") == "0600", (
        f"{_rel(path)}: the values file must be 0600 — it holds the agent token")

    play_raw = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    always = []
    for task in (play_raw.get("tasks") or []):
        always.extend(task.get("always") or [])
    removes = [t for t in always
               if t.get("ansible.builtin.file", {}).get("state") == "absent"]
    assert removes, (
        f"{_rel(path)}: the values file must be removed in `always`, so a failed "
        "install does not leave the token on disk")


def test_helm_never_takes_the_token_on_the_command_line():
    """--set would put the token in argv, where `ps` shows it to every local user."""
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task) or ""
            if "helm upgrade" not in cmd:
                continue
            assert "--set" not in cmd, (
                f"{_rel(path)}: {task.get('name')!r} passes --set; use --values instead")
            assert "--values" in cmd, (
                f"{_rel(path)}: {task.get('name')!r} installs without a values file")


# ── The chart findings ───────────────────────────────────────────────────────

def test_single_node_defaults():
    """Chart 2.11.0 defaults to 3 replicas AND carries no anti-affinity, so all three
    schedule on the one node and reserve 3 CPU / 3Gi of requests before doing work."""
    _, play = _play_of("entitle-agent-install.yml")
    assert play["vars"]["entitle_agent_replicas"] == 1
    assert play["vars"]["entitle_agent_cpu_request"] != "1000m", (
        "1000m is the chart default and is too greedy for a single edge node")


def test_datadog_is_off_at_both_switches():
    """`datadog.enabled: false` is ALREADY the chart default and does not remove
    Datadog — with sidecarLogs true it injects a sidecar into every agent pod."""
    text = _text_of("entitle-agent-install.yml")
    assert "enabled: false" in text, "datadog.enabled must be pinned off"
    assert "sidecarLogs: false" in text, (
        "datadog.sidecarLogs: false is the switch that actually drops the sidecar; "
        "without it the pod carries three containers, not two")


def test_platform_mode_is_native():
    """The values.schema.json enum is gcp|aws|azure|native; native is the on-prem one."""
    assert "mode: native" in _text_of("entitle-agent-install.yml")


def test_defaults_match_the_pov_installer():
    """web_dashboard/services/pov_entitle_agent.py installs the same chart on k3s. The
    two encode the same hard-won resource and KMS choices, so they must not drift."""
    sys.path.insert(0, _ROOT)
    from web_dashboard.services import pov_entitle_agent  # noqa: PLC0415

    _, play = _play_of("entitle-agent-install.yml")
    shared = set(pov_entitle_agent.CHART_DEFAULTS) & set(play["vars"])
    assert len(shared) >= 8, f"only {len(shared)} shared keys — did the vars get renamed?"
    for key in sorted(shared):
        assert play["vars"][key] == pov_entitle_agent.CHART_DEFAULTS[key], (
            f"{key}: playbook has {play['vars'][key]!r}, "
            f"pov_entitle_agent has {pov_entitle_agent.CHART_DEFAULTS[key]!r}")


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
