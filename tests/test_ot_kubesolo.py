"""The OT demo cell's KubeSolo runtime, as the bake script assembles it.

The cell is the only plant floor this repo ships, and KubeSolo is what it offers an
OT customer who cannot put a cluster on plant hardware — so the cell runs its
simulators as KubeSolo workloads rather than as docker containers. Nothing about that
can be checked at runtime here: the bake happens inside a cloud image builder and the
result boots in a subnet with no egress and no way in but PRA. These are the
structural rules that make it survivable, each one standing for a failure that would
otherwise only appear in front of a customer:

* Docker must be gone BEFORE KubeSolo is installed — its installer refuses a host
  that still carries Docker, so the wrong order fails every bake;
* the -offline KubeSolo build, because the default one pulls its images from a
  registry at first start and the cell has no route to one (and the bake would not
  notice: the BUILD VM has egress);
* the workload images travel as tarballs and are imported into KubeSolo's containerd,
  with imagePullPolicy: Never, so a missing image says "not in the local store"
  instead of looking like a blocked firewall;
* hostNetwork and Recreate on every workload: the PRA tunnels dial the node's own
  address, and a rolling update would deadlock on the host port it still holds;
* the cluster's identity is wiped at the end of the bake, or every cell would share
  one CA and carry a Node object named after the build VM.

Run: python tests/test_ot_kubesolo.py   (or under pytest)
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
    r"cat > /opt/ot-sim/kubesolo/ot-sim\.yaml <<EOF\n(.*?)\nEOF\n", _SRC, re.S)
_MANIFEST_PARTS = re.findall(
    r"cat >> /opt/ot-sim/kubesolo/ot-sim\.yaml <<'EOF'\n(.*?)\nEOF\n", _SRC, re.S)
_APPLY = re.search(r"cat > /opt/ot-sim/kubesolo/apply\.sh <<'EOF'\n(.*?)\nEOF\n",
                   _SRC, re.S)


def _manifest():
    assert _MANIFEST_BASE, "the bake writes no KubeSolo manifest"
    body = _MANIFEST_BASE.group(1).replace("$OT_FUXA_IMAGE", "frangoteam/fuxa:0.0.0")
    docs = list(yaml.safe_load_all("\n".join([body] + _MANIFEST_PARTS)))
    return [d for d in docs if d]


def _deployments():
    return [d for d in _manifest() if d.get("kind") == "Deployment"]


def test_kubesolo_is_the_default_runtime():
    """The demo cell exists to show the OT story, and KubeSolo is part of that story.
    An opt-in default would leave it exactly as undemonstrable as it was."""
    assert re.search(r'OT_RUNTIME:-kubesolo', _SRC), (
        "OT_RUNTIME must default to kubesolo — docker stays available as the fallback")


def test_docker_is_purged_before_kubesolo_is_installed():
    """KubeSolo's installer aborts on a host with docker on PATH, a docker.sock or an
    active docker service. Building the images needs Docker; keeping it does not.

    Checked against the CALL, not the definition: the installer is a function both
    roles share, so where its body sits in the file says nothing about ordering."""
    purge = _SRC.index("apt-get -y -q purge docker-ce")
    call = re.search(r"^install_kubesolo$", _SRC, re.M)
    assert call, "nothing calls install_kubesolo"
    assert purge < call.start(), (
        "Docker is purged after KubeSolo is installed — the installer would refuse "
        "the host and every bake would fail")


def test_the_broker_never_installs_docker_at_all():
    """The DMZ broker builds nothing, so the cleanest way to satisfy KubeSolo's
    prerequisite is to never create it: no Docker, nothing to purge, no window where
    the two container runtimes are both present."""
    docker_block = re.search(r'if \[ "\$OT_ROLE" = "cell" \]; then\n'
                             r'log "installing Docker Engine', _SRC)
    assert docker_block, (
        "the Docker install is not gated on the cell role — a broker bake would put "
        "Docker on a host whose whole job is to run KubeSolo")
    assert re.search(r"if command -v docker >/dev/null 2>&1; then\n\s*die ", _SRC), (
        "nothing checks the purge actually worked — a leftover docker binary fails "
        "the KubeSolo install with a message about the wrong thing")


def test_the_offline_kubesolo_build_is_the_one_that_gets_installed():
    """The default build pulls CoreDNS and friends from a registry at first start.
    The cell has no egress, and the bake VM does — so the failure would only ever
    appear on a deployed cell, as a cluster that never comes up."""
    install = re.search(r"curl -sfL https://get\.kubesolo\.io.*?"
                        r"rm -f /tmp/kubesolo-install\.sh", _SRC, re.S)
    assert install, "the bake does not install KubeSolo"
    block = install.group(0)
    assert "KUBESOLO_OFFLINE=true" in block, (
        "the KubeSolo install does not ask for the -offline build")
    assert "-o /tmp/kubesolo-install.sh" in block and "sh /tmp/kubesolo-install.sh" in block, (
        "the installer is piped into sh — a pipeline reports sh's status, so a failed "
        "download would install nothing and still look like a success")
    assert re.search(r"OT_KUBESOLO_VERSION:-v\d", _SRC), (
        "the KubeSolo version must be pinned — an unpinned edge distro makes two "
        "bakes of the same image behave differently")


def test_the_workload_images_are_exported_and_imported_not_pulled():
    assert "docker save" in _SRC, "the built images are never exported"
    for tarball in ("ot-plc-sim.tar", "fuxa.tar"):
        assert tarball in _SRC, (
            f"{tarball} is never written — there is no registry inside the cell")
    assert re.search(r"ctr --address .* images import", _SRC), (
        "nothing imports the exported images into KubeSolo's containerd")
    assert "install -m 0755 /usr/bin/ctr /usr/local/bin/ctr" in _SRC, (
        "ctr is not preserved before the Docker purge — the import would have no "
        "client, and apply.sh could not reload the images on a cell")
    assert _APPLY, "the bake writes no apply.sh"
    assert "images import" in _APPLY.group(1), (
        "apply.sh does not re-import the baked images — a cell whose containerd "
        "store is lost could never start its workloads again")


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
        f"the docker runtime serves {sorted(compose)} but the KubeSolo runtime "
        f"serves {sorted(manifest)} — one of them is a different cell")


def test_the_workloads_are_smoke_tested_by_listener_not_by_status():
    """`kubectl rollout status` returning is not the same claim as "the PLC answers",
    and the difference is the whole reason scripts/ot/verify_tunnels.py exists."""
    assert re.search(r"wait_for_port\(\)", _SRC), (
        "the KubeSolo runtime has no port probe — a Ready rollout would be taken as "
        "proof the plant answers")
    assert re.search(r'die "nothing answers on :\$_port', _SRC), (
        "a port that never answers must fail the BAKE, not ship an image that boots "
        "dead inside an air-gapped subnet")


def test_the_unit_waits_for_kubesolo_and_is_allowed_to_take_its_time():
    unit = re.search(r"cat > /etc/systemd/system/ot-sim\.service <<'EOF'\n(.*?)\nEOF\n"
                     r"fi\n", _SRC, re.S)
    assert unit, "the KubeSolo runtime installs no ot-sim unit"
    body = unit.group(1)
    assert "Requires=kubesolo.service" in body and "After=kubesolo.service" in body, (
        "ot-sim must order itself after KubeSolo, or first boot races the API")
    assert "ExecStart=/opt/ot-sim/kubesolo/apply.sh" in body
    timeout = re.search(r"TimeoutStartSec=(\d+)", body)
    assert timeout and int(timeout.group(1)) >= 600, (
        "a first boot mints the cluster's CA and imports ~a gigabyte of image "
        "tarballs; the default 90s timeout would kill it half way through")


def test_the_bake_resets_the_cluster_identity_but_keeps_the_image_store():
    """Same class of thing as the ssh host keys and machine-id the cleanup already
    drops. A baked cluster hands every cell the same CA and admin credential, and its
    Node object names the build VM — a hostname no cell will ever have."""
    reset = re.search(r"for _dir in /var/lib/kubesolo/\*; do(.*?)done", _SRC, re.S)
    assert reset, "the bake never resets KubeSolo's state"
    assert "*/containerd) continue" in reset.group(1), (
        "the reset wipes the image store too — every cell would then need a registry "
        "on first boot, and there is none inside the plant network")
    assert "rm -rf \"$_dir\"" in reset.group(1)


def test_the_cell_carries_the_clients_the_kubesolo_plays_expect():
    """examples/playbooks/kubesolo/ shells out to kubectl and helm because KubeSolo
    ships neither. A cell that lacks them cannot run the Entitle agent play, which is
    the second half of the story the cell exists to tell."""
    assert "/usr/local/bin/kubectl" in _SRC and "/usr/local/bin/helm" in _SRC, (
        "the bake installs no kubectl/helm on the cell")
    assert re.search(r"OT_HELM_VERSION:-v\d", _SRC), "the helm version must be pinned"


def test_apply_writes_a_kubeconfig_that_works_through_the_tunnel():
    """The rep reaches the API on 127.0.0.1 through a PRA protocol tunnel, while the
    certificate is issued to the node. Without tls-server-name the first kubectl
    command fails on a certificate error that reads like a broken tunnel."""
    body = _APPLY.group(1)
    assert "server: https://127.0.0.1:6443" in body
    assert "tls-server-name" in body


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
