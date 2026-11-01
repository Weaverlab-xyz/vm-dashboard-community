"""Kubernetes work on a remote agent (2.12), agent side.

  * **The `k8s:` block fails closed.** Absent is off; an image not pinned by digest is
    fatal; the API endpoint is checked against `k8s.targets` with the shared deny list.
  * **The step set is closed.** A step names a kind and typed fields, and this agent builds
    every argv: an unknown kind, a bad name, a `--set` with a newline, a plain-HTTP repo or
    too many steps is refused, and no field reaches the shell unquoted.
  * **The order is run_ansible's.** Endpoint checked before anything is fetched; audit
    mode returns before the bundle; the sealed steps must be the kinds the signed envelope
    listed; the kubeconfig is a file in the job volume and its credentials are redacted.
  * **A `k8s` play** uses the k8s image and check, not the Ansible ones.

Standalone or under pytest:  python tests/test_agent_k8s_run.py
"""
import importlib.util
import os
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent = _load("agent", "runners", "agent", "agent.py")
sealing = _load("agent_sealing", "web_dashboard", "services", "agent_sealing.py")

IMG = "chrweav/ansible-cloud@sha256:" + "c" * 64
KUBECONFIG = ("contexts:\n- name: x\n  context: {cluster: c, user: u}\ncurrent-context: x\n"
              "clusters:\n- name: c\n  cluster:\n    server: https://abc.eks.amazonaws.com\n"
              "users:\n- name: u\n  user:\n    token: secret-token-123456\n"
              "    client-key-data: S0VZREFUQTEyMzQ1Njc4\n")



def _same_kubeconfig(written: bytes, original: str) -> bool:
    """The run gets the current context re-serialized (2.16), not the original bytes."""
    import yaml
    got, want = yaml.safe_load(written), yaml.safe_load(original)
    return all(got[k] == want[k] for k in ("clusters", "users", "contexts", "current-context"))


def _policy(extra: str = ""):
    doc = textwrap.dedent("""
        targets:
          - cidr: 10.40.0.0/16
            ports: [22]
        job_types: [agent_ansible, agent_k8s]
        ansible:
          enabled: true
          db_image: chrweav/ansible-cloud:latest
          targets:
            - cidr: 10.40.0.0/16
              ports: [22]
        """) + textwrap.dedent(extra)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(doc)
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


_K8S = f"""
k8s:
  enabled: true
  image: {IMG}
  targets:
    - cidr: 0.0.0.0/0
      ports: [443, 6443]
"""


def _refused(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except (agent.PolicyRefusal, agent.AgentFatal) as exc:
        return str(exc)
    raise AssertionError("accepted")


# ── the policy block ──────────────────────────────────────────────────────────

def test_no_block_means_no_kubernetes_work():
    pol = _policy()
    assert pol.k8s_enabled is False
    assert "does not enable Kubernetes work" in _refused(pol.k8s_image)
    assert "does not enable" in _refused(pol.check_k8s, "52.1.2.3", 443)


def test_the_image_must_be_pinned_by_digest():
    msg = _refused(_policy, _K8S.replace(IMG, "chrweav/ansible-cloud:latest"))
    assert "pinned by digest" in msg
    assert _policy(_K8S).k8s_image() == IMG


def test_the_api_endpoint_is_checked_with_the_deny_list_in_front():
    pol = _policy(_K8S)
    pol.check_k8s("52.1.2.3", 443)
    pol.check_k8s("52.1.2.3", 6443)
    assert "does not allow" in _refused(pol.check_k8s, "52.1.2.3", 22)
    assert "denied" in _refused(pol.check_k8s, "169.254.169.254", 443)
    assert "denied" in _refused(pol.check_k8s, "127.0.0.1", 443)


# ── the closed step set ───────────────────────────────────────────────────────

def test_each_step_kind_builds_its_own_argv():
    files = {}
    cmd = agent.k8s_command([
        {"kind": "apply", "manifest": "kind: Namespace"},
        {"kind": "delete", "manifest": "kind: Secret"},
        {"kind": "helm_upgrade", "release": "entitle-agent", "chart": "entitle-agent",
         "namespace": "entitle", "repo": "https://charts.entitle.io", "version": "1.2.3",
         "set": ["agent.token=abc; rm -rf /"], "values": "a: 1", "wait": True,
         "timeout": "10m", "create_namespace": True, "best_effort": True},
        {"kind": "helm_uninstall", "release": "eso", "namespace": "external-secrets"},
    ], files)
    assert "kubectl apply -f /opt/job/step1.yaml" in cmd
    assert "kubectl delete --ignore-not-found -f /opt/job/step2.yaml" in cmd
    assert ("helm upgrade --install entitle-agent entitle-agent --namespace entitle "
            "--repo https://charts.entitle.io --version 1.2.3 --create-namespace --wait "
            "--timeout 10m --set 'agent.token=abc; rm -rf /' --values "
            "/opt/job/step3.values.yaml") in cmd
    assert f"|| echo {agent.K8S_BEST_EFFORT_MARKER} step=3" in cmd
    assert "helm uninstall eso --namespace external-secrets" in cmd
    assert set(files) == {"opt/job/step1.yaml", "opt/job/step2.yaml",
                          "opt/job/step3.values.yaml"}


def test_anything_outside_the_set_is_refused():
    bad = [
        [{"kind": "exec", "command": "id"}],
        [{"kind": "helm_upgrade", "release": "--post-renderer", "chart": "c",
          "namespace": "n"}],
        [{"kind": "helm_upgrade", "release": "r", "chart": "-x", "namespace": "n"}],
        [{"kind": "helm_upgrade", "release": "r", "chart": "c", "namespace": "n",
          "repo": "http://charts.example.com"}],
        [{"kind": "helm_upgrade", "release": "r", "chart": "c", "namespace": "n",
          "set": ["a=b\n--post-renderer=/bin/sh"]}],
        [{"kind": "helm_upgrade", "release": "r", "chart": "c", "namespace": "n",
          "set": ["--kubeconfig=/etc/x"]}],
        [{"kind": "helm_uninstall", "release": "r", "namespace": "Bad_NS"}],
        [{"kind": "apply", "manifest": ""}],
        [{"kind": "apply", "manifest": "x"}] * (agent.K8S_MAX_STEPS + 1),
        [],
    ]
    for steps in bad:
        _refused(agent.k8s_command, steps, {})


# ── the handler ───────────────────────────────────────────────────────────────

class _Dash:
    def __init__(self, steps):
        self.steps, self.asked = steps, []

    def k8s_bundle(self, job_id, *, host, port):
        self.asked.append((job_id, host, port))
        return {"kubeconfig": KUBECONFIG, "steps": self.steps}


def _run(payload, dash, *, code=0, mode="enforce"):
    seen = {}
    saved = agent._run_ansible_sibling, agent._resolve_all, agent.MODE

    def sibling(policy, **kw):
        seen.update(kw)
        return code
    agent._run_ansible_sibling = sibling
    agent._resolve_all = lambda host: ["52.1.2.3"]
    agent.MODE = mode
    try:
        return agent.run_k8s(payload, _policy(_K8S), print, lambda: False, "job-1", dash), seen
    finally:
        agent._run_ansible_sibling, agent._resolve_all, agent.MODE = saved


_PAYLOAD = {"cluster_host": "abc.eks.amazonaws.com", "cluster_port": 443,
            "step_kinds": ["apply"]}


def test_a_run_writes_the_kubeconfig_and_runs_the_steps_in_the_k8s_sibling():
    dash = _Dash([{"kind": "apply", "manifest": "kind: Namespace"}])
    result, seen = _run(_PAYLOAD, dash)
    assert result == {"exit_code": 0, "steps": 1}
    assert dash.asked == [("job-1", "abc.eks.amazonaws.com", 443)]
    assert seen["kind"] == "k8s" and seen["image"] == IMG
    assert _same_kubeconfig(seen["files"]["opt/job/kubeconfig"], KUBECONFIG)
    assert seen["env"]["KUBECONFIG"] == "/opt/job/kubeconfig"
    assert "kubectl apply" in seen["command"]


def test_sealed_steps_must_be_the_kinds_the_envelope_listed():
    dash = _Dash([{"kind": "delete", "manifest": "kind: Namespace"}])
    try:
        _run(_PAYLOAD, dash)
    except agent.PolicyRefusal as exc:
        assert "not the ones the signed job listed" in str(exc)
    else:
        raise AssertionError("ran steps the envelope did not list")


def test_audit_mode_fetches_nothing():
    dash = _Dash([{"kind": "apply", "manifest": "x"}])
    result, seen = _run(_PAYLOAD, dash, mode="audit")
    assert result["audit"] is True and dash.asked == [] and seen == {}


def test_a_failing_step_fails_the_job():
    try:
        _run(_PAYLOAD, _Dash([{"kind": "apply", "manifest": "x"}]), code=1)
    except agent.RunFailure as exc:
        assert "exited 1" in str(exc)
    else:
        raise AssertionError("a failed step completed")


def test_an_envelope_with_unknown_kinds_is_refused_before_anything():
    dash = _Dash([])
    for payload in ({**_PAYLOAD, "step_kinds": ["exec"]}, {**_PAYLOAD, "cluster_host": ""}):
        try:
            _run(payload, dash)
        except agent.PolicyRefusal:
            continue
        raise AssertionError(f"{payload} was accepted")
    assert dash.asked == []


def test_the_kubeconfigs_credentials_are_redacted():
    assert set(agent.kubeconfig_secrets(KUBECONFIG)) == {"secret-token-123456",
                                                         "S0VZREFUQTEyMzQ1Njc4"}
    assert agent.kubeconfig_secrets("not: [yaml") == []


def test_the_seal_ref_matches_the_dashboards():
    for host, port in (("ABC.eks.amazonaws.com.", 443), ("10.0.0.1", "6443")):
        assert agent.k8s_ref(host=host, port=port) == sealing.k8s_ref(host=host, port=port)


# ── a k8s play ────────────────────────────────────────────────────────────────

def test_a_k8s_play_uses_the_k8s_image_check_and_kubeconfig():
    class Dash:
        def ansible_bundle(self, job_id, **kw):
            return {"playbook": "- hosts: localhost\n", "kubeconfig": KUBECONFIG}, []
    seen = {}
    saved = agent._run_ansible_sibling, agent._resolve_all

    def sibling(policy, **kw):
        seen.update(kw)
        return 0
    agent._run_ansible_sibling = sibling
    agent._resolve_all = lambda host: ["52.1.2.3"]
    try:
        agent.run_ansible({"run_kind": "k8s", "transport": "local",
                           "target_host": "abc.eks.amazonaws.com", "target_port": 443},
                          _policy(_K8S), print, lambda: False, "job-2", Dash())
    finally:
        agent._run_ansible_sibling, agent._resolve_all = saved
    assert seen["image"] == IMG and seen["kind"] == "k8s"
    assert _same_kubeconfig(seen["files"]["opt/job/kubeconfig"], KUBECONFIG)
    assert seen["env"]["K8S_AUTH_KUBECONFIG"] == "/opt/job/kubeconfig"
    assert "-i localhost," in seen["command"]
    # Without the block a k8s play is refused, even with Config Management on.
    agent._resolve_all = lambda host: ["52.1.2.3"]
    try:
        _refused(agent.run_ansible, {"run_kind": "k8s", "transport": "local",
                                     "target_host": "abc", "target_port": 443},
                 _policy(), print, lambda: False, "job-3", Dash())
    finally:
        agent._resolve_all = saved[1]


def test_the_version_is_2_12():
    assert agent._version_tuple(agent.AGENT_VERSION) >= (2, 12, 0)
    assert agent.HANDLERS["agent_k8s"] is agent.run_k8s



# ── the API server is pinned to the checked address (2.15) ────────────────────
# ── and only the current context reaches the run (2.16) ───────────────────────

import yaml as _yaml  # noqa: E402

_KC = ("clusters:\n- name: c\n  cluster:\n    server: https://ABC.eks.amazonaws.com\n"
       "users:\n- name: u\n  user:\n    token: secret-token-123456\n"
       "contexts:\n- name: x\n  context: {cluster: c, user: u}\ncurrent-context: x\n")

# A pasted ~/.kube/config: the signed cluster is current, another sits beside it.
_KC_TWO = ("clusters:\n- name: c\n  cluster:\n    server: https://ABC.eks.amazonaws.com\n"
           "- name: d\n  cluster:\n    server: https://other.example.com\n"
           "users:\n- name: u\n  user:\n    token: secret-token-123456\n"
           "- name: v\n  user:\n    token: other-token-654321\n"
           "contexts:\n- name: x\n  context: {cluster: c, user: u}\n"
           "- name: y\n  context: {cluster: d, user: v}\ncurrent-context: x\n")


def test_a_kubernetes_run_pins_the_api_name_to_the_checked_address():
    _, pin = agent._k8s_scope(_KC, "abc.eks.amazonaws.com", 443, "52.1.2.3")
    assert pin == ("abc.eks.amazonaws.com", "52.1.2.3")
    ip_kc = _KC.replace("ABC.eks.amazonaws.com", "52.1.2.3")
    assert agent._k8s_scope(ip_kc, "52.1.2.3", 443, "52.1.2.3")[1] is None, "an address needs none"


def test_a_multi_cluster_kubeconfig_reaches_the_run_as_its_current_context_alone():
    scoped, pin = agent._k8s_scope(_KC_TWO, "abc.eks.amazonaws.com", 443, "52.1.2.3")
    doc = _yaml.safe_load(scoped)
    assert [c["name"] for c in doc["clusters"]] == ["c"]
    assert [u["name"] for u in doc["users"]] == ["u"]
    assert [c["name"] for c in doc["contexts"]] == ["x"] and doc["current-context"] == "x"
    assert "other-token" not in scoped and "other.example.com" not in scoped
    assert pin == ("abc.eks.amazonaws.com", "52.1.2.3")


def test_a_single_context_needs_no_current_context():
    kc = _KC.replace("current-context: x\n", "")
    assert _yaml.safe_load(agent._k8s_scope(kc, "abc.eks.amazonaws.com", 443,
                                            "52.1.2.3")[0])["current-context"] == "x"


def test_a_kubeconfig_whose_current_context_is_another_server_is_refused():
    for kc in (_KC.replace("ABC.eks", "evil.eks"),
               _KC.replace(".com\n", ".com:6443\n", 1),
               _KC.replace("https://", "http://"),
               _KC_TWO.replace("current-context: x", "current-context: y"),
               _KC_TWO.replace("current-context: x\n", ""),    # two contexts, none current
               _KC.replace("{cluster: c,", "{cluster: missing,"),
               _KC.replace("user: u}", "user: missing}"),
               "users: []\n", "{not yaml", "- a list\n"):
        _refused(agent._k8s_scope, kc, "abc.eks.amazonaws.com", 443, "52.1.2.3")


def test_run_k8s_passes_the_pin_to_the_sibling():
    dash = _Dash([{"kind": "apply", "manifest": "kind: Namespace"}])
    dash.k8s_bundle = lambda job_id, **kw: {"kubeconfig": _KC_TWO, "steps": dash.steps}
    result, seen = _run(_PAYLOAD, dash)
    assert result["exit_code"] == 0
    assert seen["pin"] == ("abc.eks.amazonaws.com", "52.1.2.3")
    written = seen["files"]["opt/job/kubeconfig"].decode()
    assert "other.example.com" not in written, "only the current context reaches the run"

if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
