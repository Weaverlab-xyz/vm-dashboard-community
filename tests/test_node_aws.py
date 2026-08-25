"""The managed Portainer / Rancher nodes on AWS.

GCE hands the whole container lifecycle to konlet: it reads a declaration at boot,
formats and mounts a persistent disk, and only *then* starts the container. EC2 has
no equivalent, so the same guarantees are reconstructed from user-data — and each one
that isn't reconstructed correctly fails quietly rather than loudly:

  * **Mount before container.** An existing EBS volume cannot be attached by
    ``run_instances`` (block-device mappings only create new ones), so the attach
    lands *after* user-data has started. Without the wait loop Portainer comes up
    against an unmounted ``/data``, writes its database to the root volume, and loses
    it on the next recreate — with nothing in any log to say so.
  * **Never reformat.** ``mkfs`` has to be conditional on the volume having no
    filesystem. An unconditional one destroys the only copy of the node's users,
    environments and settings.
  * **Quote the container's arguments.** A bcrypt hash is full of ``$``. Unquoted, the
    shell eats it and Portainer comes up rejecting the only password anyone has.
  * **Fail closed, per cloud.** GCE deletes the firewall rule; a security group in use
    cannot be deleted, so AWS revokes every rule instead. The observable contract —
    ``opened`` false, node unreachable — has to be the same either way.
  * **Own only what we own.** The node's ingress converges on its own ports; a rule
    somebody added on another port is theirs.
  * **Refuse, don't guess.** A region with no configured subnet must raise naming the
    key, not fall back to the default region's network while the job says otherwise.

No AWS account is needed: boto3 is never called — the pure helpers are exercised
directly and the placement's config reads are replaced with a dict-backed spy.

Run: python tests/test_node_aws.py   (or under pytest)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from web_dashboard.services import aws_service
    from web_dashboard.services import managed_node_service as mns
    from web_dashboard.services import rancher_api_runner
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_SPECS = (mns.RANCHER, mns.PORTAINER)


class _patched:
    """Swap module attributes for one test (config spies, region resolution)."""

    def __init__(self, **attrs):
        self.attrs = attrs
        self.saved = {}

    def __enter__(self):
        for dotted, value in self.attrs.items():
            mod_name, _, attr = dotted.rpartition("__")
            mod = {"cfg": mns.config_service, "rc": mns.region_config,
                   "cat": mns.region_catalog}[mod_name]
            self.saved[dotted] = (mod, attr, getattr(mod, attr))
            setattr(mod, attr, value)
        return self

    def __exit__(self, *exc):
        for mod, attr, old in self.saved.values():
            setattr(mod, attr, old)
        return False


def _cfg_spy(values):
    def get(key, default=None):
        return values.get(key, "")

    def get_bool(key, default=False):
        v = values.get(key)
        return default if v in (None, "") else str(v).lower() in ("1", "true", "yes", "on")
    return get, get_bool


def _aws_placement(spec, region_cfg, **cfg):
    get, get_bool = _cfg_spy(cfg)
    with _patched(cfg__get=get, cfg__get_bool=get_bool,
                  rc__resolve_region=lambda cloud, region: dict(region_cfg),
                  cat__default_region=lambda cloud: "us-east-2",
                  cat__normalize=lambda cloud, v: v):
        return mns.resolve_placement("aws", spec)


_GOOD_REGION = {"vpc_id": "vpc-123", "jumpoint_subnet_id": "subnet-abc"}


# ── user-data: the konlet guarantees, reconstructed ──────────────────────────

def test_the_container_starts_only_after_the_data_volume_is_mounted():
    ud = aws_service.container_node_user_data(
        "docker run -d --name portainer img", data_volume_id="vol-0abc")
    mount_at = ud.index("mount \"$DEV\"")
    run_at = ud.index("docker run")
    assert mount_at < run_at, (
        "the container is started before the data volume is mounted — Portainer would "
        "write its database to the root volume and lose it on the next recreate")


def test_the_user_data_waits_for_a_volume_attached_after_boot():
    ud = aws_service.container_node_user_data("docker run x", data_volume_id="vol-0abc")
    assert "for _ in $(seq 1" in ud, (
        "no wait loop — an existing EBS volume is attached after the instance is "
        "running, which is after user-data has already run")
    assert "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0abc" in ud, (
        "the volume is addressed by its requested device name, which is not what "
        "appears in /dev on a Nitro instance")


def test_a_volume_that_never_appears_refuses_to_start_the_container():
    ud = aws_service.container_node_user_data("docker run x", data_volume_id="vol-0abc")
    tail = ud[ud.index("else"):]
    assert "exit 1" in tail, (
        "user-data falls through to starting the container when the volume never "
        "arrived — a node that looks healthy and silently has no durable state")


def test_the_data_volume_is_never_reformatted_blindly():
    ud = aws_service.container_node_user_data("docker run x", data_volume_id="vol-0abc")
    assert "if ! blkid" in ud and "mkfs.ext4" in ud, (
        "mkfs is not gated on blkid — a redeploy would reformat the volume holding the "
        "node's only copy of its users, environments and settings")
    assert ud.index("blkid") < ud.index("mkfs.ext4")


def test_no_volume_means_no_mount_machinery_at_all():
    ud = aws_service.container_node_user_data("docker run x")
    assert "mkfs" not in ud and "blkid" not in ud, (
        "the ephemeral path carries mount logic it cannot use")
    assert ud.strip().endswith("docker run x")


def test_docker_is_started_before_the_container():
    ud = aws_service.container_node_user_data("docker run x")
    assert ud.index("systemctl enable --now docker") < ud.index("docker run x")


# ── the docker command ──────────────────────────────────────────────────────

def test_container_arguments_are_quoted_so_a_bcrypt_hash_survives():
    cmd = mns.docker_run_command(
        "portainer/portainer-ce:latest", name="portainer", ports=("9443",),
        args=["--admin-password", "$2y$05$abcDEF"])
    assert "'$2y$05$abcDEF'" in cmd, (
        "the bcrypt hash is unquoted, so the shell would expand it and Portainer would "
        f"reject the only password anybody has: {cmd}")


def test_environment_values_are_quoted_too():
    cmd = mns.docker_run_command("rancher/rancher", name="rancher",
                                 env={"CATTLE_BOOTSTRAP_PASSWORD": "p$ss w0rd"})
    assert "CATTLE_BOOTSTRAP_PASSWORD='p$ss w0rd'" in cmd, cmd


def test_the_container_restarts_with_the_host():
    """konlet's restartPolicy: Always. Without it a reboot leaves a running VM
    serving nothing, which reads as a firewall problem."""
    assert "--restart always" in mns.docker_run_command("img", name="x")


def test_rancher_runs_privileged_and_portainer_does_not():
    """Rancher's embedded components need it; the Portainer server administers REMOTE
    Docker hosts over the API, so it needs neither privilege nor a Docker socket."""
    r = mns.docker_run_command("rancher/rancher", name="rancher", privileged=True)
    p = mns.docker_run_command("portainer/portainer-ce", name="portainer")
    assert "--privileged" in r
    assert "--privileged" not in p
    assert "docker.sock" not in p, "the Portainer node must not mount a Docker socket"


def test_each_features_ports_are_published():
    r = mns.docker_run_command("i", name="r", ports=mns.RANCHER.ports)
    p = mns.docker_run_command("i", name="p", ports=mns.PORTAINER.ports)
    assert "-p 80:80" in r and "-p 443:443" in r
    assert "-p 9443:9443" in p and "-p 8000:8000" in p


# ── the security group ──────────────────────────────────────────────────────

def test_the_ingress_diff_ignores_rules_on_other_ports():
    sg = {"IpPermissions": [
        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
         "IpRanges": [{"CidrIp": "1.2.3.4/32"}]},
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
         "IpRanges": [{"CidrIp": "9.9.9.9/32"}]},
    ]}
    have = aws_service._current_node_ingress(sg, ("80", "443"))
    assert have == {(443, "1.2.3.4/32")}, (
        f"the diff claimed a rule on a port the node does not use, so converging would "
        f"revoke somebody else's access: {have}")


def test_a_port_range_is_not_mistaken_for_a_node_rule():
    """A wide range that merely CONTAINS the node's port is not the node's rule, and
    revoking it would close far more than intended."""
    sg = {"IpPermissions": [{"IpProtocol": "tcp", "FromPort": 1, "ToPort": 65535,
                             "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]}
    assert aws_service._current_node_ingress(sg, ("443",)) == set()


def test_non_tcp_rules_are_left_alone():
    sg = {"IpPermissions": [{"IpProtocol": "udp", "FromPort": 443, "ToPort": 443,
                             "IpRanges": [{"CidrIp": "1.2.3.4/32"}]}]}
    assert aws_service._current_node_ingress(sg, ("443",)) == set()


def test_ingress_permissions_group_every_cidr_under_its_port():
    perms = aws_service._node_ingress_permissions(
        {(443, "1.1.1.1/32"), (443, "2.2.2.2/32"), (80, "1.1.1.1/32")})
    by_port = {p["FromPort"]: [r["CidrIp"] for r in p["IpRanges"]] for p in perms}
    assert by_port == {80: ["1.1.1.1/32"], 443: ["1.1.1.1/32", "2.2.2.2/32"]}, by_port


# ── placement refuses rather than guessing ──────────────────────────────────

def test_a_region_with_no_subnet_refuses_and_names_the_key():
    for spec in _SPECS:
        try:
            _aws_placement(spec, {"vpc_id": "vpc-1"})
        except mns.ManagedNodeError as exc:
            assert "subnet" in str(exc), f"the error does not name what is missing: {exc}"
        else:
            raise AssertionError(
                f"{spec.feature} placed a node in a region with no subnet — it would "
                f"land in the default region's network while the job said otherwise")


def test_a_region_with_no_vpc_refuses_and_names_the_key():
    for spec in _SPECS:
        try:
            _aws_placement(spec, {"jumpoint_subnet_id": "subnet-1"})
        except mns.ManagedNodeError as exc:
            assert "vpc" in str(exc).lower(), exc
        else:
            raise AssertionError(f"{spec.feature} placed a node with no VPC for its "
                                 f"security group")


def test_the_runner_subnet_is_an_accepted_fallback_for_the_node_subnet():
    """The Gateway host already falls back to the sandbox's public runner subnet, and
    the node needs the same network shape — so it reuses that rather than demanding a
    second field the sandbox may not have emitted."""
    p = _aws_placement(mns.RANCHER, {"vpc_id": "vpc-1", "ecs_subnet_id": "subnet-run"})
    assert p["subnet_id"] == "subnet-run", p


def test_aws_placement_reports_no_zone_because_the_subnet_pins_it():
    p = _aws_placement(mns.RANCHER, _GOOD_REGION)
    assert p["zone"] == "", (
        "AWS placement invented an availability zone; the subnet already decides it, "
        "so a value here could only contradict the truth")


def test_aws_placement_carries_the_cloud_neutral_fields():
    p = _aws_placement(mns.PORTAINER, _GOOD_REGION)
    for key in ("cloud", "account", "region", "name", "firewall_name",
                "vpc_id", "subnet_id", "instance_type", "boot_disk_gb"):
        assert key in p, f"aws placement has no {key!r}"
    assert p["cloud"] == "aws"
    # The VPC is the honest truthiness check: without it the node cannot be placed.
    assert p["account"] == "vpc-123"
    assert p["firewall_name"] == f"{p['name']}-allow-mgmt"


def test_the_rancher_instance_type_default_has_enough_memory():
    """Rancher OOMs under 4 GB — the GCE side refuses e2-small for exactly this. A
    t3.small (2 GB) default would ship the same defect on AWS."""
    from web_dashboard.config import settings
    assert settings.aws_rancher_instance_type not in (
        "t2.micro", "t3.micro", "t2.small", "t3.small", "t3a.small"), (
        f"aws_rancher_instance_type={settings.aws_rancher_instance_type} is under 4 GB")


# ── the API runner follows the node ─────────────────────────────────────────

def test_an_aws_availability_zone_yields_its_region():
    """The two clouds spell a zone differently and neither split works on the other:
    'us-east-1a' rsplit on '-' would give 'us-east'."""
    from web_dashboard.services import config_service
    old = config_service.get
    config_service.get = lambda key, default=None: (
        "us-east-1a" if key == "aws_rancher_zone" else "")
    try:
        assert rancher_api_runner._node_region("aws") == "us-east-1"
    finally:
        config_service.get = old


def test_a_gcp_zone_still_yields_its_region():
    from web_dashboard.services import config_service
    old = config_service.get
    config_service.get = lambda key, default=None: (
        "us-east1-b" if key == "gcp_rancher_zone" else "")
    try:
        assert rancher_api_runner._node_region("gcp") == "us-east1"
    finally:
        config_service.get = old


def test_the_runner_backend_is_not_configured_separately():
    """It has to run inside the node's own network to reach its private address, so
    deriving it from the node's cloud is the only correct answer — a separate knob
    could be set to a cloud with no route."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "rancher_api_runner.py"), encoding="utf-8").read()
    assert "def _node_cloud()" in src
    body = src[src.index("async def _run("):]
    body = body[:body.index("\ndef ")]
    assert "_node_cloud()" in body, (
        "the runner picks its backend from something other than the node's cloud")
    for cloud in ('"gcp"', '"aws"'):
        assert cloud in body, f"the runner has no {cloud} backend"


# ── teardown ordering ───────────────────────────────────────────────────────

def test_teardown_revokes_before_reclaiming_the_group():
    """Revoking is what closes the node and must not wait on anything; the delete can
    only succeed once the terminating instance releases its ENI."""
    for module in ("rancher_node_service", "portainer_node_service"):
        src = open(os.path.join(_ROOT, "web_dashboard", "services", f"{module}.py"),
                   encoding="utf-8").read()
        body = src[src.index("async def _stop_node("):]
        body = body[:body.index("\nasync def ", 1)]
        assert "ensure_node_security_group" in body and "release_node_security_group" in body, (
            f"{module} does not reclaim the node's security group — an orphaned group "
            f"blocks a later VPC delete")
        assert body.index("ensure_node_security_group") < body.index(
            "release_node_security_group"), (
            f"{module} tries to delete the group before revoking its rules")


def test_the_data_volume_is_only_deleted_when_asked():
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_node_service.py"), encoding="utf-8").read()
    body = src[src.index("async def _stop_node("):]
    body = body[:body.index("\nasync def ", 1)]
    assert "if delete_data_disk and data_disk_name:" in body, (
        "the data volume is deleted on a routine teardown — that is the one part of a "
        "teardown that cannot be undone")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
