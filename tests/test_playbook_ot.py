"""The OT broker's plays (examples/playbooks/ot/) find their cluster, k3s first.

The OT demo's cell and DMZ broker run k3s. The same agent plays also serve a standalone
KubeSolo edge host (examples/playbooks/kubesolo/), so instead of hardcoding either
layout each play stats k3s's kubeconfig, falls back to KubeSolo's, and points every
kubectl and helm call at whichever it found. What is pinned here:

  * every play that talks to a cluster carries that discovery, and k3s wins when both
    are present — the broker is k3s, and a KubeSolo leftover must not redirect it;
  * the k3s path the plays look for is the one the bake writes, so a change to either
    side breaks this test instead of a broker install;
  * the filenames the dashboard queues by name are still here.

The shared play invariants (idempotency, absolute binary paths, the chart values) are in
tests/test_playbook_kubesolo.py, which covers this folder too.

Run: python tests/test_playbook_ot.py   (or under pytest)
"""
import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OT_DIR = os.path.join(_ROOT, "examples", "playbooks", "ot")
_BAKE = os.path.join(_ROOT, "provisioners", "ot", "ot-sim-debian.sh")

# The plays that run kubectl or helm. fuxa-admin-rotate.yml only speaks HTTP to the HMI.
_CLUSTER_PLAYS = ("entitle-agent-install.yml", "entitle-agent-uninstall.yml",
                  "openfaas-function-deploy.yml")


def _play(name):
    with open(os.path.join(_OT_DIR, name), encoding="utf-8") as handle:
        return yaml.safe_load(handle)[0]


def _flat_tasks(play):
    out = []

    def walk(items):
        for task in items or []:
            if not isinstance(task, dict):
                continue
            nested = [k for k in ("block", "rescue", "always") if k in task]
            for key in nested:
                walk(task[key])
            if not nested:
                out.append(task)

    walk(play.get("tasks"))
    return out


def test_the_services_find_the_plays_they_queue():
    """ot_service / ot_faas_service queue these by bare filename."""
    from web_dashboard.services import ot_faas_service, ot_service
    for name in (ot_service.ENTITLE_AGENT_PLAYBOOK, ot_faas_service.FAAS_DEPLOY_PLAYBOOK,
                 ot_faas_service.FUXA_ROTATE_PLAYBOOK):
        assert os.path.isfile(os.path.join(_OT_DIR, name)), (
            f"{name} is queued by the dashboard but is not in examples/playbooks/ot/")


def test_every_cluster_play_discovers_k3s_before_kubesolo():
    for name in _CLUSTER_PLAYS:
        tasks = _flat_tasks(_play(name))
        stats = [t["ansible.builtin.stat"]["path"] for t in tasks
                 if "ansible.builtin.stat" in t]
        assert "{{ k3s_kubeconfig }}" in stats, f"{name}: never looks for k3s"
        assert "{{ kubesolo_path }}/pki/admin/admin.kubeconfig" in stats, (
            f"{name}: never looks for KubeSolo")
        assert stats.index("{{ k3s_kubeconfig }}") < stats.index(
            "{{ kubesolo_path }}/pki/admin/admin.kubeconfig"), (
            f"{name}: KubeSolo is checked first — a leftover would redirect a k3s broker")
        facts = [t["ansible.builtin.set_fact"] for t in tasks
                 if "ansible.builtin.set_fact" in t and "kube_kubeconfig" in t[
                     "ansible.builtin.set_fact"]]
        assert facts, f"{name}: never sets kube_kubeconfig"
        chosen = str(facts[0]["kube_kubeconfig"])
        assert re.search(r"k3s_kubeconfig if _k3s_kc\.stat\.exists", chosen), (
            f"{name}: kube_kubeconfig does not prefer k3s: {chosen!r}")


def test_no_cluster_play_hardcodes_a_kubeconfig():
    """Every KUBECONFIG is the discovered one, so no task can talk to the wrong cluster."""
    for name in _CLUSTER_PLAYS:
        play = _play(name)
        text = open(os.path.join(_OT_DIR, name), encoding="utf-8").read()
        envs = [play.get("environment") or {}] + [
            t.get("environment") or {} for t in _flat_tasks(play)]
        for env in envs:
            if "KUBECONFIG" in env:
                assert "kube_kubeconfig" in env["KUBECONFIG"], (
                    f"{name}: KUBECONFIG is hardcoded to {env['KUBECONFIG']!r}")
        for line in re.findall(r"export KUBECONFIG=.*", text):
            assert "kube_kubeconfig" in line, f"{name}: {line}"


def test_the_k3s_path_is_the_one_the_bake_writes():
    bake = open(_BAKE, encoding="utf-8").read()
    match = re.search(r"^K3S_KUBECONFIG=(\S+)$", bake, re.M)
    assert match, "the bake no longer names k3s's kubeconfig"
    for name in _CLUSTER_PLAYS:
        default = _play(name)["vars"]["k3s_kubeconfig"]
        assert default == match.group(1), (
            f"{name} looks for k3s at {default}, the bake writes {match.group(1)}")


if __name__ == "__main__":
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    os.environ.setdefault("JWT_SECRET_KEY", "test-secret-playbook-ot")
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
