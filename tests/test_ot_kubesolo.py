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
    assert re.search(r"command -v docker[^\n]*\n[^\n]*\[ -x ", _SRC), (
        "nothing checks the purge actually worked — a leftover docker binary fails "
        "the KubeSolo install with a message about the wrong thing")


def test_the_purge_check_asks_the_filesystem_not_the_shells_command_hash():
    """`command -v` alone cannot answer "is docker gone".

    Both dash and bash resolve it from the shell's command hash before they consult
    PATH, and neither stats the cached path (bash only does with `checkhash`, off by
    default). The bake runs docker a dozen times building and exporting the images,
    so by the time the purge finishes the hash still holds /usr/bin/docker and a bare
    `command -v` guard reports Docker present on a host it has just been removed
    from — a bake that fails for the one reason that is not true. The guard must drop
    the hash and then test the surviving path with -x."""
    guard = re.search(r"\n(hash -r[^\n]*\n(?:[^\n]*\n){0,4}?[^\n]*command -v docker"
                      r"(?:[^\n]*\n){0,3}?[^\n]*\[ -x [^\n]*\n)", _SRC)
    assert guard, (
        "the post-purge Docker guard does not clear the shell's command hash before "
        "asking, or does not confirm the answer against the filesystem — it would "
        "fail every cell bake on a host where the purge worked")


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


def test_the_identity_wipe_unmounts_before_it_deletes():
    """`systemctl stop kubesolo` does not undo kubelet's mounts. Every pod that ran
    leaves a tmpfs at .../volumes/kubernetes.io~projected/kube-api-access-* holding
    its service-account token, and those outlive the process — so the wipe hits
    `rm: cannot remove ...: Device or resource busy` and, under `set -eu`, kills the
    bake at the very last step with the image otherwise finished. Cost a 8m57s Azure
    cell bake 2026-09-25.

    Ordering is the test: unmounting after the delete is the same bug. So is
    unmounting shallowest-first, because a parent will not release while a child is
    still mounted."""
    stop = _SRC.index("systemctl stop kubesolo")
    delete = _SRC.index("for _dir in /var/lib/kubesolo/*; do", stop)
    window = _SRC[stop:delete]

    assert "umount" in window, (
        "nothing unmounts kubelet's pod volumes between stopping kubesolo and "
        "deleting its state directory — the delete cannot remove a mountpoint")
    assert "/proc/self/mounts" in window, (
        "the unmount does not consult the kernel's mount table, so it can only be "
        "guessing at paths that carry random pod UIDs")
    assert "sort -r" in window, (
        "the unmount is not deepest-first; a parent mount will not release while a "
        "child of it is still mounted")
    assert "umount -l" in window, (
        "no lazy-unmount fallback — one stubborn mount and the bake dies anyway, "
        "seconds before the host is generalized and captured")


def test_the_cell_carries_the_clients_the_kubesolo_plays_expect():
    """examples/playbooks/kubesolo/ shells out to kubectl and helm because KubeSolo
    ships neither. A cell that lacks them cannot run the Entitle agent play, which is
    the second half of the story the cell exists to tell."""
    assert "/usr/local/bin/kubectl" in _SRC and "/usr/local/bin/helm" in _SRC, (
        "the bake installs no kubectl/helm on the cell")
    assert re.search(r"OT_HELM_VERSION:-v\d", _SRC), "the helm version must be pinned"


def test_every_node_ready_wait_first_waits_for_the_node_to_exist():
    """`kubectl wait --for=condition=Ready node --all` is not a wait when the cluster
    has no Node object yet: it exits 1 immediately with "no matching resources found"
    and never reads --timeout. KubeSolo registers its node seconds after the installer
    writes the kubeconfig, and a cell mints a fresh node on every first boot — so both
    the bake and apply.sh must poll for the object's existence before waiting on its
    condition, or the race they exist to absorb becomes an instant hard failure."""
    loops = re.findall(r"while \[ ! -f \"\$KUBECONFIG\".*?\n *done\n", _SRC, re.S)
    # Three sites, and the count is the tripwire: install_kubesolo (the bake), the
    # cell's apply.sh, and the broker's ot-faas apply.sh. A fourth should be a
    # deliberate update to this number, not a silent inheritance — the whole point is
    # that every one of them polls for existence before it waits on a condition.
    assert len(loops) == 3, (
        "expected install_kubesolo's wait plus the cell's and the broker's apply.sh "
        f"— the KubeSolo readiness gate has moved (found {len(loops)})")
    for loop in loops:
        assert "kubectl get --raw /readyz" in loop, (
            "a kubeconfig on disk is not an API that answers")
        assert "kubectl get nodes -o name" in loop, (
            "this waits on node Ready without first waiting for a node to exist — "
            "that is an instant failure, not a wait")


def test_apply_writes_a_kubeconfig_that_works_through_the_tunnel():
    """The rep reaches the API on 127.0.0.1 through a PRA protocol tunnel, while the
    certificate is issued to the node. Without tls-server-name the first kubectl
    command fails on a certificate error that reads like a broken tunnel."""
    body = _APPLY.group(1)
    assert "server: https://127.0.0.1:6443" in body
    assert "tls-server-name" in body


def test_every_image_transfer_into_containerd_is_forced_local():
    """containerd 2.0 made ctr hand image imports, pulls and exports to the TRANSFER
    service, which is served over containerd.services.streaming.v1.Streaming --- an
    API KubeSolo's embedded containerd does not register. The cell's client is
    whatever Docker's containerd.io package ships on the day of the bake (2.x now),
    so the default path dies with "unknown service ...streaming.v1.Streaming" on a
    socket where listing images answers perfectly, and the same client re-imports on
    every boot. --local is the pre-2.0 path, over the content and images services
    KubeSolo does serve, and a no-op on the 1.7 client the broker pins. Comment lines
    are dropped first, so prose about the flag cannot satisfy the guard."""
    code = "\n".join(line for line in _SRC.splitlines()
                     if not line.lstrip().startswith("#"))
    calls = re.findall(r"images (import|pull|export)((?:\s+--?\S+)*)", code)
    assert calls, "nothing moves an image into KubeSolo's containerd"
    for verb, flags in calls:
        assert "--local" in flags.split(), (
            f"an `images {verb}` runs without --local: a 2.x ctr hands it to the "
            f"transfer service, and KubeSolo serves no streaming API for that — "
            f"the bake fails, or a cell fails the same way at boot")


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
