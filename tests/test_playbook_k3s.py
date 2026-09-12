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


# ── SPIFFE authentication ────────────────────────────────────────────────────
# k3s-spiffe-auth.yml edits the API server's authentication configuration, which on a
# single-node cluster is the one change that can leave no API to fix itself from. It has
# never been run against a live cluster, so these pin the parts that decide whether it
# comes back. Reasoning: docs/design/workload-k8s-short-lived-token.md.

def _spiffe_play():
    path = os.path.join(_K3S_DIR, "k3s-spiffe-auth.yml")
    assert os.path.exists(path), "k3s-spiffe-auth.yml is missing"
    return yaml.safe_load(open(path, encoding="utf-8").read())[0]


def _resolved(play, value):
    """`value` with any bare `{{ var }}` replaced from the play's own vars.

    The dests in this play are variables (`{{ auth_config }}`), not literal paths, so
    matching on the raw string finds nothing and every assertion below would pass
    vacuously by never running.
    """
    out = str(value)
    for name, val in (play.get("vars") or {}).items():
        out = out.replace("{{ %s }}" % name, str(val)).replace("{{%s}}" % name, str(val))
    return out


def _copy_task(play, needle):
    """The copy task whose (variable-resolved) dest contains `needle`."""
    for task in _tasks(play):
        copy = task.get("ansible.builtin.copy") or {}
        if needle in _resolved(play, copy.get("dest", "")):
            return task
    raise AssertionError(f"no copy task writing a dest containing {needle!r}")


def test_the_apiserver_flag_goes_in_a_dropin_not_config_yaml():
    """config.yaml already carries whatever k3s-server-init.yml or an operator put there.
    k3s merges config.yaml.d/*, so a drop-in adds the flag without clobbering those — and
    deleting the one file is the whole recovery path if the API server will not restart."""
    play = _spiffe_play()
    dropin = play["vars"]["dropin"]
    assert "config.yaml.d/" in dropin, (
        f"the flag is written to {dropin!r}. Editing config.yaml directly discards flags "
        "set elsewhere, and leaves no single file to delete to undo this")
    task = _copy_task(play, "config.yaml.d")
    assert "kube-apiserver-arg" in task["ansible.builtin.copy"]["content"]
    assert "authentication-config=" in task["ansible.builtin.copy"]["content"]


def test_the_dropin_directory_exists_before_the_dropin_is_written():
    """copy does not create intermediate directories, and config.yaml.d does not exist on a
    stock k3s host — so writing the drop-in first fails the first run on every clean node."""
    play = _spiffe_play()
    names = [t.get("name") for t in _tasks(play)]
    mkdir = next(i for i, t in enumerate(_tasks(play))
                 if "config.yaml.d" in _resolved(play, (t.get("ansible.builtin.file") or {}).get("path", "")))
    write = next(i for i, t in enumerate(_tasks(play))
                 if "config.yaml.d" in _resolved(play, (t.get("ansible.builtin.copy") or {}).get("dest", "")))
    assert mkdir < write, (
        f"{names[write]!r} writes the drop-in before {names[mkdir]!r} creates its directory")


def test_the_authentication_config_is_the_ga_api_version():
    """Structured authentication is GA in 1.34 as apiserver.config.k8s.io/v1. A v1 document
    on an older API server is rejected and the API server does not start."""
    play = _spiffe_play()
    content = _copy_task(play, "spiffe-auth-config.yaml")["ansible.builtin.copy"]["content"]
    # Matched as a whole LINE, not a substring: "…k8s.io/v1" is a substring of
    # "…k8s.io/v1beta1", so `in content` would happily accept the beta version this test
    # exists to reject. Caught by mutation-testing the assertion itself.
    lines = [ln.strip() for ln in content.splitlines()]
    assert "apiVersion: apiserver.config.k8s.io/v1" in lines, (
        "the AuthenticationConfiguration is not at the GA apiVersion; found "
        + repr(next((ln for ln in lines if ln.startswith("apiVersion:")), None)))
    assert "kind: AuthenticationConfiguration" in lines
    guard = yaml.safe_dump([t for t in _tasks(play) if "assert" in yaml.safe_dump(t)])
    assert "34" in guard, (
        "nothing refuses an older Kubernetes. Writing a v1 config to a pre-1.34 API server "
        "stops it, and on a single-node cluster there is then no API to repair it through")


def test_the_username_claim_carries_the_mandatory_prefix():
    """Kubernetes requires a prefix on any username claim that is not `email`, and whatever
    is chosen becomes part of every RBAC subject naming this identity."""
    play = _spiffe_play()
    content = _copy_task(play, "spiffe-auth-config.yaml")["ansible.builtin.copy"]["content"]
    assert "claim: sub" in content, "the username must map from sub, which holds the SPIFFE ID"
    assert "prefix:" in content, "a username claim other than email needs a prefix"
    assert play["vars"]["username_prefix"], "username_prefix defaults to empty"
    rbac = _copy_task(play, "spiffe-rbac.yaml")["ansible.builtin.copy"]["content"]
    assert "_rbac_user" in rbac, (
        "the binding does not use the prefixed username, so it names an identity the API "
        "server never produces")


def test_the_audience_is_checked_and_not_merely_declared():
    """The audience is the only thing stopping a JWT-SVID minted for another relying party
    being replayed at this API server."""
    play = _spiffe_play()
    content = _copy_task(play, "spiffe-auth-config.yaml")["ansible.builtin.copy"]["content"]
    assert "audiences:" in content, "the issuer declares no audience"
    assert "claim: aud" in content, "no claimValidationRule pins the audience"


def test_the_workloads_kubeconfig_holds_no_credential():
    """The entire point: it names a command, and the command asks the Workload API. A token
    or a client certificate in this file would make the demonstration prove the opposite of
    what it claims."""
    play = _spiffe_play()
    content = _copy_task(play, ".kube/config")["ansible.builtin.copy"]["content"]
    # Matched as whole LINES, exactly as the AuthenticationConfiguration above is, and for
    # the same reason twice over: `"exec:" in content` is satisfied by "notexec:", and
    # "…k8s.io/v1" is a substring of "…k8s.io/v1beta1". Both halves of the substring form
    # passed mutations that broke precisely what they claim to pin. Quotes are dropped from
    # the lines rather than written into the literal so the play may quote the value or not.
    #
    # It also keeps CodeQL off the line. A bare dotted hostname in a positive membership test
    # is how a URL allow-list check is written, which is what
    # py/incomplete-url-substring-sanitization flags -- correctly in general, since a
    # substring is a weak way to validate a URL. It is not what this line does, but the fix
    # is to stop writing the shape rather than to suppress the query.
    lines = [ln.strip().replace('"', "") for ln in content.splitlines()]
    assert "exec:" in lines, "the user entry runs no exec credential plugin"
    assert "apiVersion: client.authentication.k8s.io/v1" in lines, (
        "the exec plugin does not name the client-go credential API at its GA version; found "
        + repr([ln for ln in lines if ln.startswith("apiVersion:")]))
    for banned in ("token:", "client-certificate-data:", "client-key-data:", "password:"):
        assert banned not in content, (
            f"the workload kubeconfig contains {banned!r} — it must carry no credential at all")


def test_the_kubeconfig_directory_exists_before_the_kubeconfig():
    play = _spiffe_play()
    tasks = _tasks(play)
    names = [t.get("name") for t in tasks]
    mkdir = next(i for i, t in enumerate(tasks)
                 if ".kube" in _resolved(play, (t.get("ansible.builtin.file") or {}).get("path", "")))
    write = next(i for i, t in enumerate(tasks)
                 if ".kube/config" in _resolved(play, (t.get("ansible.builtin.copy") or {}).get("dest", "")))
    assert mkdir < write, (
        f"{names[write]!r} writes the kubeconfig before {names[mkdir]!r} creates ~/.kube")


def test_the_recovery_path_is_documented_in_the_play():
    """This play can stop the API server. The way back has to be in the file, not only in
    the design note — whoever is staring at a dead cluster is reading the play."""
    src = open(os.path.join(_K3S_DIR, "k3s-spiffe-auth.yml"), encoding="utf-8").read()
    assert "config.yaml.d" in src and "systemctl restart k3s" in src, \
        "the play does not say how to undo itself"


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
