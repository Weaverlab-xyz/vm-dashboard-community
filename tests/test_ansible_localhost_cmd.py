"""Unit test: ansible_localhost_cmd produces the exact in-container command for a
localhost Ansible run (k8s / cloud-database targets), shared verbatim by all four
runner backends — ECS / ACI / Cloud Run, and the local sibling container used for
on-prem clusters — plus the docker argv that wraps it locally.

Pins the security-relevant shape:
  - `-i 'localhost,' -c local` (no SSH `-i '<ip>,'`, no `--private-key`)
  - the DB conn-vars file is passed via `-e @/tmp/conn_vars.json` and chmod 600
  - the kubeconfig exports BOTH K8S_AUTH_KUBECONFIG and KUBECONFIG (helm needs the latter)
  - NO `set -x` (tracing would echo the base64 credential blobs into the logs)
  - the local runner passes every value by 0600 `--env-file`, never on the docker
    command line, where `ps` would expose the kubeconfig's client key

Pure module (no app deps) — imported directly from its file so the package's
heavier imports don't load. Runs under pytest, or standalone:
    python tests/test_ansible_localhost_cmd.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ansible_localhost_cmd.py")

_spec = importlib.util.spec_from_file_location("ansible_localhost_cmd", _MOD_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
build = _mod.build_localhost_command


def _common(cmd: str):
    assert cmd.startswith("set -e;"), cmd
    assert "set -x" not in cmd, "must never trace — would echo the credential blobs"
    assert 'printf %s "$PLAYBOOK_B64" | base64 -d > /tmp/playbook.yml' in cmd
    assert "ansible-playbook -i 'localhost,' -c local /tmp/playbook.yml" in cmd
    assert "--private-key" not in cmd and "-i '" + "127" not in cmd  # not an SSH run


def test_database_run_decodes_conn_vars_and_passes_extra_vars_file():
    cmd = build(with_conn_vars=True, with_kubeconfig=False)
    _common(cmd)
    assert 'printf %s "$CONN_VARS_B64" | base64 -d > /tmp/conn_vars.json' in cmd
    assert "chmod 600 /tmp/conn_vars.json" in cmd
    assert cmd.rstrip().endswith("-e @/tmp/conn_vars.json")
    # No kubeconfig material on a DB run.
    assert "KUBECONFIG" not in cmd


def test_kubernetes_run_exports_both_kubeconfig_env_names():
    cmd = build(with_conn_vars=False, with_kubeconfig=True)
    _common(cmd)
    assert 'printf %s "$KUBECONFIG_B64" | base64 -d > /tmp/kubeconfig' in cmd
    assert "chmod 600 /tmp/kubeconfig" in cmd
    assert "export K8S_AUTH_KUBECONFIG=/tmp/kubeconfig KUBECONFIG=/tmp/kubeconfig" in cmd
    # No DB conn-vars on a k8s run.
    assert "conn_vars.json" not in cmd
    assert cmd.rstrip().endswith("/tmp/playbook.yml")


def test_bare_run_only_decodes_playbook():
    cmd = build()
    _common(cmd)
    assert "conn_vars.json" not in cmd
    assert "KUBECONFIG" not in cmd
    assert cmd.rstrip().endswith("/tmp/playbook.yml")


# ── local runner argv (on-prem Kubernetes) ────────────────────────────────────

def test_local_argv_runs_the_same_command_as_the_cloud_runners():
    """The local branch must not grow its own command string — a divergence would
    only ever be found on a real on-prem cluster."""
    argv = _mod.build_local_docker_argv(
        image="chrweav/ansible-cloud:latest", env_file_path="/tmp/x/run_env",
        with_kubeconfig=True)
    assert argv[-3:-1] == ["sh", "-c"]
    assert argv[-1] == build(with_conn_vars=False, with_kubeconfig=True)


def test_local_argv_passes_every_value_by_env_file_never_on_the_command_line():
    """docker run arguments land in the HOST's process list, so `-e KUBECONFIG_B64=…`
    would expose the cluster-admin client key to any local user via `ps`. Only the
    0600 env-file path may appear."""
    argv = _mod.build_local_docker_argv(
        image="img", env_file_path="/tmp/x/run_env",
        with_conn_vars=True, with_kubeconfig=True)
    assert "--env-file" in argv
    assert argv[argv.index("--env-file") + 1] == "/tmp/x/run_env"
    # No `-e`/`--env` value-carrying flag at all.
    assert "-e" not in argv and "--env" not in argv
    joined = " ".join(argv)
    for name in ("PLAYBOOK_B64=", "KUBECONFIG_B64=", "CONN_VARS_B64=",
                 "PASSWORD_SAFE_", "PORTAINER_"):
        assert name not in joined, f"{name} must not reach the docker argv"


def test_local_argv_is_a_disposable_container_on_the_given_image():
    argv = _mod.build_local_docker_argv(image="myrepo/img:tag", env_file_path="/e")
    assert argv[:3] == ["docker", "run", "--rm"], "the run container must not persist"
    assert "myrepo/img:tag" in argv


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
