"""The plant's function runtime, as the broker's bake assembles it.

The broker cell carries the Entitle agent; this adds the thing the agent CALLS — an
OpenFaaS function hosting an Entitle REST adapter, so the HTTP server that grants
access to a plant target sits in the plant and Entitle never needs a route in.

None of it is checkable at runtime here: the bake runs inside a cloud image builder
and the result boots in a subnet with no egress. So these are the structural rules
that make it survivable, each standing for a failure that would otherwise surface in
front of a customer:

* the broker must never install Docker — KubeSolo's installer refuses a host that
  carries it, and the cell already pays for that with a whole purge dance; so the one
  image the broker builds is built with buildah, which is daemonless;
* every image must be pre-loaded into KubeSolo's containerd AND named in images.txt,
  because the manifests pull nothing: a name containerd does not hold is a pod that
  dies ErrImageNeverPull on a host with no egress, which reads like a firewall block;
* ``imagePullPolicy: Never`` must survive into the RENDER, because this is the one
  setting whose failure the smoke test cannot catch — the build VM has egress, so
  ``Always`` works here and only fails later, on a cell;
* the chart must be trimmed to one pod, or it quietly outgrows a 2-vCPU broker that
  is already carrying the agent's 1Gi of requests;
* the adapter's CODE must not be baked. The bake channel is a single shell script, so
  a baked adapter would be a heredoc twin of ~45 KB of security-relevant Python; the
  image is a generic loader and the code arrives at wire time instead.

Run: python tests/test_ot_faas_bake.py   (or under pytest)
"""
import ast
import io
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_SCRIPT = os.path.join(_ROOT, "provisioners", "ot", "ot-sim-debian.sh")
_SRC = io.open(_SCRIPT, encoding="utf-8").read()


def _heredoc(marker: str, delimiter: str = "EOF") -> str:
    """The body of the heredoc written to ``marker``."""
    pattern = (re.escape(f"cat > {marker} <<") + r"'?" + re.escape(delimiter)
               + r"'?\n(.*?)\n" + re.escape(delimiter) + r"\n")
    found = re.search(pattern, _SRC, re.S)
    assert found, f"no heredoc writing {marker} (delimiter {delimiter})"
    return found.group(1)


def _faas_section() -> str:
    """Everything guarded by OT_FAAS = openfaas."""
    start = _SRC.index('if [ "$OT_FAAS" = "openfaas" ]; then')
    end = _SRC.index("fi   # OT_FAAS = openfaas")
    return _SRC[start:end]


def _code_only(text: str) -> str:
    """``text`` with whole-line comments removed.

    Every "must not appear" assertion below runs on this rather than the raw source,
    because the script EXPLAINS the things it must not do — the note on why buildah is
    used instead of Docker names Docker, and the loader's comment about path
    forwarding names Entitle. Asserting on the raw text makes each of those comments
    fail the test that the comment exists to explain.

    Whole-line comments only, deliberately: stripping trailing ``#`` would also cut
    inside strings, and for a negative assertion over-stripping is the dangerous
    direction — it turns a real violation into a pass.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


# ── The role and runtime gate ─────────────────────────────────────────────────

def test_the_runtime_is_broker_only_and_refuses_a_cell():
    assert 'if [ "$OT_ROLE" != "broker" ] && [ "$OT_FAAS" != "none" ]; then' in _SRC, (
        "a cell could be baked with a function runtime — the cell is the plant floor "
        "and runs simulators, not the adapters that grant access to them")


def test_an_unbuildable_runtime_is_refused_by_name_not_ignored():
    """`nuclio` and `deployment` are in the dashboard's dispatch table but this script
    cannot bake them. Silently treating them as 'openfaas' would bake the wrong thing
    and silently treating them as 'none' would bake a broker with no runtime; both
    surface only at wire time."""
    assert "nuclio|deployment)" in _SRC
    assert "planned runtime that this script cannot bake yet" in _SRC


def test_the_licence_ceiling_is_stated_where_the_runtime_is_chosen():
    """Not a footnote. OpenFaaS CE caps commercial use at one install per company for
    60 days and forbids installing it for a client or redistributing it — and this
    repo is public and ships to POV instances. An operator choosing the runtime has to
    meet that sentence here, not discover it at a customer."""
    for phrase in ("Community Edition", "60 days", "redistributing"):
        assert phrase in _SRC, f"the licensing note does not mention {phrase!r}"


# ── No Docker, ever ───────────────────────────────────────────────────────────

def test_the_broker_builds_its_image_without_docker():
    section = _faas_section()
    assert "buildah" in section, "the broker has no builder"
    code = _code_only(section)
    assert "docker build" not in code and "docker save" not in code, (
        "the broker reached for Docker — KubeSolo's installer refuses a host that "
        "carries it, and the cell's purge dance (iptables chains, docker0, a "
        "reinstalled iptables package) is the price of getting that wrong")
    assert "--isolation chroot" in section, (
        "buildah without --isolation chroot wants user namespaces, which vary by "
        "cloud image")
    assert "--storage-driver vfs" in section, (
        "the default storage driver wants kernel overlayfs or fuse-overlayfs; vfs "
        "costs a few seconds on one small image and works on any kernel")


def test_buildah_and_its_layer_store_are_purged_after_the_build():
    section = _faas_section()
    assert "apt-get -y -q purge buildah" in section, "buildah ships in the image"
    assert "rm -rf /var/lib/containers" in section, (
        "buildah's layer store (~150 MB of base-image layers) ships in the image")


def test_ctr_comes_from_a_pinned_release_not_from_a_package():
    body = _SRC[_SRC.index("install_ctr() {"):]
    body = body[:body.index("\n}\n")]
    assert "OT_CONTAINERD_VERSION" in body, "ctr is not pinned"
    assert "tar -xzf /tmp/containerd.tar.gz -C /tmp bin/ctr" in body, (
        "the whole containerd archive is extracted — that drops containerd and its "
        "shim into /usr/local/bin, where KubeSolo's own copies belong")
    assert "containerd.io" not in _code_only(body), (
        "installing the containerd.io package would put a second container runtime "
        "and a service on the host, which is what the cell has to purge")


# ── Images: pre-loaded, listed, and never pulled at boot ─────────────────────

def test_every_runtime_image_is_loaded_and_listed_and_proven():
    section = _faas_section()
    assert "images.txt" in section, "nothing records what containerd was given"
    assert "normalize_ref" in section, (
        "the recorded names are not normalised the way containerd stores them, so "
        "apply.sh's already-loaded check would never match")
    assert "images ls -q | grep -qx" in section, (
        "the import is not PROVEN — a name containerd does not hold is a pod that "
        "dies ErrImageNeverPull, and the bake would not notice")


def test_the_render_may_name_only_images_the_bake_loaded():
    """The check that catches a chart component quietly coming back.

    An enabled async queue or Prometheus shows up as an image nobody exported, and at
    boot that is a pod stuck ErrImageNeverPull rather than an obviously wrong pod
    count — so it is caught statically, against images.txt, before the manifest ships.
    """
    section = _faas_section()
    assert "the rendered manifest needs images this bake never loaded" in section
    assert "awk -v want=" in section, (
        "the membership test is not exact — matching a ref anywhere in the line would "
        "accept a tarball name as an image name")


def test_image_pull_policy_never_survives_into_the_render():
    """The one setting the smoke test CANNOT catch.

    The build VM has egress, so `imagePullPolicy: Always` pulls fine here and fails
    only later, on a cell, in an egress-less subnet — reported as ImagePullBackOff,
    which reads like a blocked firewall. So it is asserted on the rendered file.
    """
    section = _faas_section()
    assert "imagePullPolicy: Never" in section
    assert "normalising" in section and "imagePullPolicy: *Always" in section, (
        "a chart that renamed a values key would leave Always in the render and helm "
        "would say nothing")
    assert "survived normalisation" in section, (
        "the normalisation is not verified, so a sed that matched nothing would pass")


def test_no_new_image_or_chart_pin_is_floating():
    for var in ("OT_OPENFAAS_GATEWAY_IMAGE", "OT_OPENFAAS_NETES_IMAGE",
                "OT_FAAS_PYTHON_IMAGE", "OT_FAAS_IMAGE"):
        found = re.search(re.escape(var) + r'="\$\{' + re.escape(var) + r':-([^}]*)\}"',
                          _SRC)
        assert found, f"{var} has no default"
        assert not found.group(1).endswith(":latest"), f"{var} defaults to :latest"
        assert ":" in found.group(1).rsplit("/", 1)[-1], (
            f"{var} carries no explicit tag ({found.group(1)})")
    assert 'OT_OPENFAAS_CHART_VERSION:-1' in _SRC or \
           re.search(r'OT_OPENFAAS_CHART_VERSION="\$\{OT_OPENFAAS_CHART_VERSION:-[0-9]',
                     _SRC), "the chart version is not pinned to a number"
    assert "must be pinned to a version tag, not :latest" in _SRC


# ── The chart, trimmed ────────────────────────────────────────────────────────

def test_the_chart_is_rendered_at_bake_with_its_crds():
    section = _faas_section()
    assert "helm template" in section, (
        "the chart is installed at boot instead of rendered at bake, which leaves "
        "release state for a half-finished first boot to trip over")
    assert "--include-crds" in section, (
        "without the CRD the functions.openfaas.com type does not exist and the "
        "operator crash-loops on it")
    assert "the render carries no CRD" in section, (
        "--include-crds is passed but never verified, so a chart that stopped "
        "shipping CRDs would render quietly")


def test_both_namespaces_are_prepended_with_the_labels_faas_netes_looks_for():
    render = _heredoc('"$_rendered"')
    assert "name: openfaas" in render and "role: openfaas-system" in render
    assert "name: openfaas-fn" in render and 'openfaas: "1"' in render, (
        "faas-netes finds function namespaces by the openfaas=1 label; without it a "
        "Function object is accepted and never scheduled")


def test_the_trimmed_components_are_switched_off_and_the_cost_is_written_down():
    values = _heredoc('"$OT_FAAS_DIR/values.yaml"')
    assert "async: false" in values, "nats and the queue-worker are 2 of the 4 pods"
    for key in ("prometheus", "alertmanager", "basicAuthPlugin"):
        assert key in values, f"{key} is not switched off"
    assert "create: false" in values and "enabled: false" in values
    assert "basic_auth: true" in values, (
        "turning basic_auth off would leave /system/* open to everything on the "
        "cluster; it stays on and apply.sh mints a password no human holds")
    assert "operator:" in values and "create: true" in values, (
        "without operator mode a function is deployed by POSTing to the gateway with "
        "its credential, instead of by kubectl apply")
    # The cost of each `false` has to be stated, because discovering it live is worse.
    section = _faas_section()
    assert "reports zero invocations" in section, (
        "Prometheus is off and the consequence (no invocation counts, empty graphs) "
        "is not written down anywhere an operator will read it")


# ── The smoke test is the point ──────────────────────────────────────────────

def test_the_bake_requires_exactly_one_pod_in_the_openfaas_namespace():
    section = _faas_section()
    assert 'kubectl -n openfaas get pods --no-headers' in section
    assert 'if [ "$_pods" != "1" ]' in section, (
        "the pod count is not asserted, so a chart upgrade that re-enables NATS or "
        "Prometheus would ship — and its first symptom on a cell is pods Pending with "
        "the reason only in kubectl describe")


def test_the_bake_proves_the_whole_chain_through_a_throwaway_function():
    section = _faas_section()
    assert "crd/functions.openfaas.com" in section, "the CRD is never waited on"
    assert "ot-faas-selftest" in section, "nothing exercises a real Function object"
    assert "rollout status deploy/ot-faas-selftest" in section
    assert "the operator never created a Deployment" in section, (
        "a Function that is accepted but never reconciled looks like success")


def test_the_selftest_probes_from_a_pod_not_from_the_host():
    section = _faas_section()
    assert "kubectl -n openfaas-fn run" in section, (
        "the probe runs on the host, which is a different source address and a "
        "different answer — the caller will be the Entitle agent, also a pod")
    assert "gateway.openfaas.svc.cluster.local" in section, (
        "the probe does not go through cluster DNS, so it proves less than it looks")


def test_the_subpath_probe_is_what_makes_the_adapter_routable():
    """The routing question, answered at bake.

    An Entitle Remote Adapter distinguishes its operations by PATH — /give_access vs
    /revoke_access. If the gateway does not forward the residual path after
    /function/<name>, every operation lands on the function root and they become
    indistinguishable. That is a design problem, not a configuration one, so it has
    to fail the bake rather than surface at a customer.
    """
    section = _faas_section()
    assert "/function/ot-faas-selftest/get_assets" in section, "the sub-path is never probed"
    assert 'did NOT forward the sub-path' in section, (
        "a gateway that swallowed the sub-path would pass the probe, because the "
        "sentinel still comes back — the PATH it echoes is the assertion")


def test_the_bake_checks_the_operator_reconciles_a_deletion():
    section = _faas_section()
    assert "kubectl delete -f /tmp/ot-faas-selftest.yaml" in section
    assert "operator does not reconcile deletions" in section, (
        "a revoked adapter whose Deployment survives keeps running with the last "
        "package it was given")


# ── The unit, and the cleanup ────────────────────────────────────────────────

def test_the_unit_waits_for_kubesolo_and_is_allowed_to_take_its_time():
    unit = _heredoc("/etc/systemd/system/ot-faas.service")
    assert "Requires=kubesolo.service" in unit and "After=kubesolo.service" in unit
    found = re.search(r"TimeoutStartSec=(\d+)", unit)
    assert found and int(found.group(1)) >= 1200, (
        "a first boot mints the cluster CA and imports the baked tarballs; the "
        "default 90s would kill that half way through")
    assert "RemainAfterExit=yes" in unit, "a oneshot without this reads as failed"


def test_the_cleanup_stops_the_new_unit_before_wiping_the_cluster():
    assert "systemctl stop ot-faas.service" in _SRC, (
        "the identity reset stops ot-sim but not ot-faas, so a oneshot still mid-apply "
        "would be writing to the containerd store this block is about to prune")
    assert _SRC.index("systemctl stop ot-faas.service") < \
           _SRC.index("systemctl stop kubesolo"), (
        "ot-faas must stop before the API server it is talking to goes away")


def test_the_broker_apply_script_mints_the_basic_auth_secret_itself():
    apply = _heredoc('"$OT_FAAS_DIR/kubesolo/apply.sh"')
    assert "basic-auth" in apply, (
        "the chart generates this with a Helm HOOK, and hooks do not run through "
        "helm template — so without this the gateway has no secret to mount and "
        "never becomes ready")
    assert "/dev/urandom" in apply, "the password is not random"
    assert "rollout status deploy/gateway" in apply, (
        "a boot that only half-worked must say so in systemctl status")


# ── The loader, and its contract with the dashboard ──────────────────────────

def _bootstrap() -> str:
    return _heredoc('"$OT_FAAS_DIR/image/bootstrap.py"', "PYEOF")


def test_the_baked_loader_is_valid_python_and_carries_no_business_logic():
    body = _bootstrap()
    ast.parse(body)
    # It may know how to unpack and exec. It may not know anything about FUXA, Entitle
    # or HTTP routing — all of that arrives at wire time, which is the whole point.
    code = _code_only(body)
    for forbidden in ("fuxa", "entitle", "give_access", "create_actor", "Authorization"):
        assert forbidden.lower() not in code.lower(), (
            f"the baked loader mentions {forbidden!r} — business logic in the IMAGE "
            f"needs a re-bake to fix, which is what this design exists to avoid")


def test_the_loader_refuses_an_unverified_or_mismatched_package():
    body = _bootstrap()
    assert "OTFN_PKG_SHA256" in body
    assert "refusing to run an" in body, (
        "a package with no hash is accepted — it carries the code that mints "
        "credentials and it travels as one argv element with a kernel size limit, so "
        "a truncated payload is a real failure mode and a truncated zip can still "
        "extract something")
    assert "refusing to run it" in body, "a hash MISMATCH is not refused"


def test_the_loader_runs_the_entry_module_as_main():
    body = _bootstrap()
    assert 'runpy.run_module("openfaas_entry", run_name="__main__")' in body, (
        "without run_name=__main__ the shim is imported and the process exits at "
        "once, which presents as a pod that never becomes ready")


def test_the_loader_contract_matches_what_the_dashboard_builds():
    """The two halves of the wire-time contract, held together.

    The image is baked weeks before the package it runs is built, so every string
    they share is a contract: a rename on one side only is a pod that starts and then
    exits, with nothing in the logs about why.
    """
    from web_dashboard.services import cloud_function_package as pkg

    body = _bootstrap()
    entry_name = pkg._LAYOUT["openfaas"]["entry"][1]
    assert entry_name == "openfaas_entry.py", entry_name
    assert pkg.HANDLERS["openfaas"] == "openfaas_entry", pkg.HANDLERS["openfaas"]
    assert f'"{pkg.HANDLERS["openfaas"]}"' in body, (
        "the loader runs a module name the packager does not produce")
    # The port the Dockerfile tells of-watchdog to proxy to must be the one the shim
    # binds. The shim's default lives in the repo; the image's lives here.
    assert "upstream_url=http://127.0.0.1:5000" in _faas_section()
    assert "PORT = int(os.environ.get(\"OTFN_PORT\") or 5000)" in body
    shim_src = io.open(os.path.join(_ROOT, "web_dashboard", "functions", "fnentry",
                                    "openfaas_entry.py"), encoding="utf-8").read()
    assert "DEFAULT_PORT = 5000" in shim_src, (
        "the shim's default port and the image's upstream_url disagree, so of-watchdog "
        "would proxy to a port nothing is listening on")


def test_the_sentinel_echoes_the_path_the_bake_asserts_on():
    body = _bootstrap()
    assert "ot-faas-selftest-ok" in body, "the sentinel string moved"
    assert "ot-faas-selftest-ok" in _faas_section(), (
        "the bake greps for a different sentinel than the loader prints")
    assert '"path": "%s"' in body, (
        "the sentinel does not echo the request path, so the sub-path assertion above "
        "cannot tell a forwarded path from a swallowed one")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
