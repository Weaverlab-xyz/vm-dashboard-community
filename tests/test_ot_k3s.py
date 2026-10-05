"""The OT demo cell's k3s runtime, as the bake script assembles it.

The cell runs its simulators as k3s workloads, with Docker kept on the host beside it:
k3s brings its own containerd and CNI and does not need the host to be Docker-free, and
not everything a plant (or this dashboard) runs is Kubernetes-native. Nothing about
that can be checked at runtime here: the bake happens inside a cloud image builder and
the result boots in a subnet with no egress and no way in but PRA. These are the
structural rules that make it survivable, each one standing for a failure that would
otherwise only appear in front of a customer:

* k3s is installed AIR-GAPPED — the binary and the release's image bundle are fetched
  at bake time and the installer is told to download nothing, because a k3s that pulls
  its own images at first start has no route to a registry on a cell (and the bake
  would not notice: the BUILD VM has egress);
* Docker STAYS on the cell — no purge — and the broker never gets Docker at all;
* the workload images travel as tarballs in k3s's air-gap image directory, which k3s
  imports on every start, with imagePullPolicy: Never, so a missing image says "not in
  the local store" instead of looking like a blocked firewall;
* every image move goes through `k3s ctr`, never a bare ctr: on the cell that name is
  Docker's client, which defaults to Docker's containerd — the wrong store, silently;
* hostNetwork and Recreate on every workload: the PRA tunnels dial the node's own
  address, and a rolling update would deadlock on the host port it still holds;
* the cluster's identity is wiped at the end of the bake, or every cell would share
  one CA and carry a Node object named after the build VM.

Run: python tests/test_ot_k3s.py   (or under pytest)
"""
import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIM = os.path.join(_ROOT, "provisioners", "ot", "ot-sim-debian.sh")
_SRC = open(_SIM, encoding="utf-8").read()

# The workload manifest, as the bake assembles it: one heredoc for the base (which
# interpolates the FUXA pin) plus one per optional simulator.
_MANIFEST_BASE = re.search(
    r"cat > /opt/ot-sim/k3s/ot-sim\.yaml <<EOF\n(.*?)\nEOF\n", _SRC, re.S)
_MANIFEST_PARTS = re.findall(
    r"cat >> /opt/ot-sim/k3s/ot-sim\.yaml <<'EOF'\n(.*?)\nEOF\n", _SRC, re.S)
_APPLY = re.search(r"cat > /opt/ot-sim/k3s/apply\.sh <<'EOF'\n(.*?)\nEOF\n",
                   _SRC, re.S)


def _manifest():
    assert _MANIFEST_BASE, "the bake writes no k3s manifest"
    body = _MANIFEST_BASE.group(1).replace("$OT_FUXA_IMAGE", "frangoteam/fuxa:0.0.0")
    docs = list(yaml.safe_load_all("\n".join([body] + _MANIFEST_PARTS)))
    return [d for d in docs if d]


def _deployments():
    return [d for d in _manifest() if d.get("kind") == "Deployment"]


def test_k3s_is_the_default_runtime_and_kubesolo_is_refused():
    """The demo cell exists to show a plant IPC carrying a real cluster. KubeSolo is
    refused by name rather than silently mapped: an operator asking for it is asking
    for an image this script no longer produces, and should be told what it does."""
    assert re.search(r'OT_RUNTIME:-k3s', _SRC), (
        "OT_RUNTIME must default to k3s — docker stays available as the fallback")
    assert re.search(r"^\s*kubesolo\) die ", _SRC, re.M), (
        "OT_RUNTIME=kubesolo is not refused with an explanation")


def test_docker_stays_on_the_cell():
    """The point of the move from KubeSolo: Docker and k3s coexist, so nothing purges
    Docker any more, and nothing removes docker0 or flushes its iptables chains."""
    code = "\n".join(line for line in _SRC.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "purge docker-ce" not in code, "the bake still purges Docker"
    assert "ip link delete docker0" not in code, "the bake still removes docker0"
    assert not re.search(r"iptables -t \"\$_table\" -F", code), (
        "the bake still flushes iptables — with Docker running that would break it")
    assert "systemctl enable docker" in code, "Docker is not left enabled on the cell"


def test_the_broker_never_installs_docker_at_all():
    """The DMZ broker runs the Entitle agent and nothing else, so it gets no engine."""
    docker_block = re.search(r'if \[ "\$OT_ROLE" = "cell" \]; then\n'
                             r'log "installing Docker Engine', _SRC)
    assert docker_block, (
        "the Docker install is not gated on the cell role — a broker bake would put "
        "an unused engine on the one host in the plant with a way out")


def test_k3s_is_installed_air_gapped_and_pinned():
    """k3s's own images (CoreDNS, pause, local-path) come from the release bundle, and
    the installer downloads nothing — the cell boots with no egress at all."""
    body = _SRC[_SRC.index("install_k3s() {"):]
    body = body[:body.index("\n}\n")]
    assert re.search(r"OT_K3S_VERSION:-v\d", _SRC), (
        "the k3s version must be pinned — two bakes of the same image must agree")
    assert "k3s-airgap-images-" in body and '"$K3S_IMAGES/' in body, (
        "the air-gap image bundle is not placed in k3s's image directory")
    assert "INSTALL_K3S_SKIP_DOWNLOAD=true" in body, (
        "the installer is allowed to download — it would fetch the binary at bake time "
        "only by luck, and nothing stops a later change pulling at first boot")
    assert "-o /tmp/k3s-install.sh" in body and "sh /tmp/k3s-install.sh" in body, (
        "the installer is piped into sh — a pipeline reports sh's status, so a failed "
        "download would install nothing and still look like a success")
    assert "%2B" in body, "the `+` in a k3s tag is not escaped in the release URL"


def test_k3s_runs_without_the_components_that_would_claim_host_ports():
    """traefik and servicelb both bind host ports on a machine whose ports are the
    plant's protocols; nothing here is published through either."""
    config = re.search(r"cat > /etc/rancher/k3s/config\.yaml <<'K3SEOF'\n(.*?)\nK3SEOF\n",
                       _SRC, re.S)
    assert config, "k3s is configured by flags rather than an inspectable config file"
    cfg = yaml.safe_load(config.group(1))
    assert {"traefik", "servicelb"} <= set(cfg.get("disable") or []), cfg
    assert str(cfg.get("write-kubeconfig-mode")) == "0600", (
        "the admin kubeconfig is cluster-admin and must stay root-only")


def test_pod_networking_is_proven_at_bake_beside_docker():
    """Docker sets FORWARD to DROP. hostNetwork workloads do not care, but CoreDNS and
    every non-host pod (OpenFaaS on the broker) do — so the bake waits for CoreDNS."""
    body = _SRC[_SRC.index("install_k3s() {"):]
    body = body[:body.index("\n}\n")]
    assert "rollout status deploy/coredns" in body, (
        "nothing proves pod networking works on a host that also runs Docker")


def test_the_workload_images_reach_k3s_as_tarballs_not_pulls():
    assert re.search(r'docker save ot-plc-sim:baked -o "\$K3S_IMAGES/ot-plc-sim\.tar"', _SRC)
    assert re.search(r'docker save "\$OT_FUXA_IMAGE" -o "\$K3S_IMAGES/fuxa\.tar"', _SRC)
    # Before k3s starts, so its first start imports them.
    assert _SRC.index('-o "$K3S_IMAGES/fuxa.tar"') < _SRC.index("\ninstall_k3s\n"), (
        "the tarballs land after k3s first starts, so they are not imported until a reboot")
    assert _APPLY, "the bake writes no apply.sh"
    assert "images import" in _APPLY.group(1), (
        "apply.sh cannot re-import a missing image — a cell whose containerd store is "
        "lost could never start its workloads again")


def test_every_image_move_goes_through_k3s_own_ctr():
    """A bare `ctr` on the cell is Docker's client, pointed at Docker's containerd."""
    code = "\n".join(line for line in _SRC.splitlines()
                     if not line.lstrip().startswith("#"))
    calls = re.findall(r"(\S+(?: \S+)*?) images (import|pull|export|ls)", code)
    assert calls, "nothing moves an image into k3s's containerd"
    for prefix, verb in calls:
        assert prefix.endswith(("$K3S_CTR", "$CTR")), (
            f"`images {verb}` runs through {prefix.split()[-1]!r}, not k3s's own ctr")
    assert 'CTR="k3s ctr --namespace k8s.io"' in code, "apply.sh's CTR is not k3s's"


def test_every_workload_pins_never_pull_host_network_and_recreate():
    deployments = _deployments()
    assert deployments, "the manifest declares no workloads"
    for dep in deployments:
        name = dep["metadata"]["name"]
        spec = dep["spec"]["template"]["spec"]
        assert dep["spec"].get("strategy", {}).get("type") == "Recreate", (
            f"{name}: a rolling update would start the new pod while the old one "
            f"still holds its host port, and the rollout would wedge")
        assert spec.get("hostNetwork") is True, (
            f"{name}: the PRA tunnels dial the node's own address, so the workload "
            f"has to listen there rather than behind the CNI")
        for container in spec["containers"]:
            assert container.get("imagePullPolicy") == "Never", (
                f"{name}: without imagePullPolicy Never a missing image becomes an "
                f"ImagePullBackOff, which in an air-gapped cell reads as a firewall "
                f"problem rather than as a missing image")
            for port in container.get("ports") or []:
                assert port.get("hostPort") == port.get("containerPort"), (
                    f"{name}: a remapped host port would make the PRA tunnel's "
                    f"remote half wrong even though the workload is fine")


def test_the_manifest_and_the_compose_stack_serve_the_same_ports():
    """Both runtimes have to present the same cell to PRA: the tunnels, the Web Jump
    and the Purdue allow-list are all written against these ports, and a cell baked
    either way must answer identically."""
    compose = {int(a) for a, b in re.findall(r'- "(\d+):(\d+)"', _SRC) if a == b}
    manifest = {int(p) for p in re.findall(r"hostPort:\s*(\d+)", _SRC)}
    assert compose == manifest, (
        f"the docker runtime serves {sorted(compose)} but the k3s runtime "
        f"serves {sorted(manifest)} — one of them is a different cell")


def test_the_workloads_are_smoke_tested_by_listener_not_by_status():
    """`kubectl rollout status` returning is not the same claim as "the PLC answers",
    and the difference is the whole reason scripts/ot/verify_tunnels.py exists."""
    assert re.search(r"wait_for_port\(\)", _SRC), (
        "the k3s runtime has no port probe — a Ready rollout would be taken as "
        "proof the plant answers")
    assert re.search(r'die "nothing answers on :\$_port', _SRC), (
        "a port that never answers must fail the BAKE, not ship an image that boots "
        "dead inside an air-gapped subnet")


def test_the_unit_waits_for_k3s_and_is_allowed_to_take_its_time():
    unit = re.search(r"cat > /etc/systemd/system/ot-sim\.service <<'EOF'\n(.*?)\nEOF\n"
                     r"fi\n", _SRC, re.S)
    assert unit, "the k3s runtime installs no ot-sim unit"
    body = unit.group(1)
    assert "Requires=k3s.service" in body and "After=k3s.service" in body, (
        "ot-sim must order itself after k3s, or first boot races the API")
    assert "ExecStart=/opt/ot-sim/k3s/apply.sh" in body
    timeout = re.search(r"TimeoutStartSec=(\d+)", body)
    assert timeout and int(timeout.group(1)) >= 600, (
        "a first boot mints the cluster's CA and imports ~a gigabyte of image "
        "tarballs; the default 90s timeout would kill it half way through")


def test_the_bake_resets_the_cluster_identity_but_keeps_the_images():
    """Same class of thing as the ssh host keys and machine-id the cleanup already
    drops. A baked cluster hands every cell the same CA and admin credential, and its
    Node object names the build VM — a hostname no cell will ever have."""
    cleanup = _SRC[_SRC.index("resetting the cluster's identity"):]
    assert 'rm -rf "$K3S_DATA/server"' in cleanup, (
        "the datastore, CA and tokens survive the bake")
    reset = re.search(r'for _dir in "\$K3S_DATA"/agent/\*; do(.*?)done', cleanup, re.S)
    assert reset, "the agent-side state is never reset"
    assert "*/containerd|*/images) continue" in reset.group(1), (
        "the reset wipes the images too — every cell would then need a registry on "
        "first boot, and there is none inside the plant network")
    assert "/etc/rancher/node" in cleanup, (
        "the node password survives, so every cell registers as the same node")
    assert cleanup.index("systemctl stop k3s") < cleanup.index('rm -rf "$K3S_DATA/server"')


def test_the_cell_carries_the_clients_the_plays_expect():
    """examples/playbooks/ot/ shells out to /usr/local/bin/kubectl and helm. k3s's
    installer links the first; helm is installed by the bake."""
    assert "install -m 0755 \"/tmp/linux-$OT_ARCH/helm\" /usr/local/bin/helm" in _SRC
    assert "ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl" in _SRC
    assert re.search(r"OT_HELM_VERSION:-v\d", _SRC), "the helm version must be pinned"


def test_every_node_ready_wait_first_waits_for_the_node_to_exist():
    """`kubectl wait --for=condition=Ready node --all` is not a wait when the cluster
    has no Node object yet: it exits 1 immediately with "no matching resources found"
    and never reads --timeout. k3s registers its node seconds after the installer
    returns, and a cell mints a fresh node on every first boot — so both
    the bake and apply.sh must poll for the object's existence before waiting on its
    condition, or the race they exist to absorb becomes an instant hard failure."""
    loops = re.findall(r"while \[ ! -f \"\$KUBECONFIG\".*?\n *done\n", _SRC, re.S)
    # Three sites, and the count is the tripwire: install_k3s (the bake), the
    # cell's apply.sh, and the broker's ot-faas apply.sh. A fourth should be a
    # deliberate update to this number, not a silent inheritance — the whole point is
    # that every one of them polls for existence before it waits on a condition.
    assert len(loops) == 3, (
        "expected install_k3s's wait plus the cell's and the broker's apply.sh "
        f"— the k3s readiness gate has moved (found {len(loops)})")
    for loop in loops:
        assert "kubectl get --raw /readyz" in loop, (
            "a kubeconfig on disk is not an API that answers")
        assert "kubectl get nodes -o name" in loop, (
            "this waits on node Ready without first waiting for a node to exist — "
            "that is an instant failure, not a wait")


def test_apply_writes_a_kubeconfig_that_works_through_the_tunnel():
    """The rep reaches the API on 127.0.0.1 through a PRA protocol tunnel. k3s's
    serving certificate names 127.0.0.1, so pinning the server line is all it takes;
    a tls-server-name for the node's address would now be a lie about what is needed."""
    body = _APPLY.group(1)
    assert "server: https://127.0.0.1:6443" in body
    assert "/var/lib/ot-sim/kubeconfig-via-tunnel.yaml" in body
    assert "chmod 0600 /var/lib/ot-sim/kubeconfig-via-tunnel.yaml" in body


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
