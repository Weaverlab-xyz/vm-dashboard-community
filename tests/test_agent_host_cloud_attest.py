"""Cloud node attestation for dashboard-deployed agents (dashboard_spire, agent_host_service).

  * A cloud uses its instance identity only when the operator ticked it (the CLI cannot
    list a SPIRE server's plugins); otherwise its hosts keep join tokens.
  * The selectors pinning a VM are exactly the strings the SPIRE 1.15 plugins emit, and a
    value that is not plain is refused rather than widened.
  * The bootstrap for a cloud attestor carries no join token anywhere, names the plugin
    (Azure with its tenant domain), keeps the node key on disk (every attestor is
    trust-on-first-use) and shares the host network for the metadata service.
  * The node becomes a node entry pinning the VM; forgetting it evicts whatever attested
    as that VM and deletes the entry. A join-token agent's node is left to remove_workload.

Runs under pytest, or standalone:  python tests/test_agent_host_cloud_attest.py
"""
import base64
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="cloud-attest-"), "t.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import agent_host_service as ahs  # noqa: E402
from web_dashboard.services import config_service, dashboard_spire  # noqa: E402

Base.metadata.create_all(bind=engine)
TD = "dash.example.com"
NODE = f"spiffe://{TD}/node/agent-1"


def _material(attestor, **extra):
    return {"mode": "spire", "join_token": "" if attestor != "join_token" else "JT-secret",
            "node_attestor": attestor, "node_spiffe_id": NODE, "trust_domain": TD,
            "bootstrap_pem": "-----BEGIN CERTIFICATE-----\n",
            "server_address": "spire.example.com", "server_port": 8081, **extra}


def _files(script):
    return {m.group(2): base64.b64decode(m.group(1)).decode()
            for m in re.finditer(r"echo (\S+) \| base64 -d > \"\$D/([^\"]+)\"", script)}


def _script(material):
    return ahs.bootstrap_script(url="https://dash.example.com",
                                policy=ahs.policy_yaml(["10.40.0.0/16"], 2, upgrades=True),
                                material=material)


# ── which clouds ──────────────────────────────────────────────────────────────

def test_a_cloud_attests_with_its_identity_only_once_ticked():
    try:
        for cloud in ("aws", "azure", "gcp"):
            assert dashboard_spire.cloud_attestor(cloud) == ""
        config_service.set("spire_cloud_attest_gcp", "true")
        assert dashboard_spire.cloud_attestor("gcp") == "gcp_iit"
        assert dashboard_spire.cloud_attestor("aws") == ""
        config_service.set("spire_cloud_attest_aws", "true")
        config_service.set("spire_cloud_attest_azure", "true")
        assert dashboard_spire.cloud_attestor("aws") == "aws_iid"
        assert dashboard_spire.cloud_attestor("azure") == "azure_imds"
        assert dashboard_spire.cloud_attestor("oci") == ""
    finally:
        for cloud in ("aws", "azure", "gcp"):
            config_service.set(f"spire_cloud_attest_{cloud}", "false")


# ── the selectors ─────────────────────────────────────────────────────────────

def test_the_selectors_are_the_plugins_own_strings():
    assert dashboard_spire.cloud_node_selectors("aws", instance_id="i-0abc123") == [
        "aws_iid:instance:id:i-0abc123"]
    assert dashboard_spire.cloud_node_selectors(
        "gcp", project="proj-1", zone="us-central1-a", name="vpc-a-agent") == [
        "gcp_iit:project-id:proj-1", "gcp_iit:zone:us-central1-a",
        "gcp_iit:instance-name:vpc-a-agent"]
    assert dashboard_spire.cloud_node_selectors(
        "azure", subscription="0000-11", resource_group="rg-agents", name="vpc-a-agent") == [
        "azure_imds:subscription-id:0000-11", "azure_imds:resource-group:rg-agents",
        "azure_imds:vm-name:vpc-a-agent"]


def test_a_value_that_is_not_plain_is_refused_not_widened():
    for bad in ("", "i-1:instance:id:i-2", "a b", "x" * 200):
        try:
            dashboard_spire.cloud_node_selectors("aws", instance_id=bad)
        except dashboard_spire.DashboardSpireError:
            continue
        raise AssertionError(f"{bad!r} was accepted")
    try:
        dashboard_spire.cloud_node_selectors("gcp", project="p", zone="", name="n")
    except dashboard_spire.DashboardSpireError:
        return
    raise AssertionError("a GCP VM was pinned without its zone")


# ── the bootstrap ─────────────────────────────────────────────────────────────

def test_a_cloud_attestor_bootstrap_carries_no_token_and_keeps_the_key_on_disk():
    for attestor, extra in (("aws_iid", {}), ("gcp_iit", {}),
                            ("azure_imds", {"tenant_domain": "contoso.onmicrosoft.com"})):
        script = _script(_material(attestor, **extra))
        files = _files(script)
        conf = files["spire/conf/agent.conf"]
        assert f'NodeAttestor "{attestor}"' in conf, (attestor, conf)
        assert "join_token" not in conf and "JT-secret" not in script
        assert "spire/join.token" not in files and "SPIRE_JOIN_TOKEN" not in script
        assert "-expandEnv" not in script
        assert 'KeyManager "disk"' in conf
        spire_line = next(x for x in script.splitlines() if "--name dashboard-agent-spire" in x)
        assert "--network host" in spire_line, "the metadata service is out of reach"
        if attestor == "azure_imds":
            assert 'tenant_domain = "contoso.onmicrosoft.com"' in conf


def test_the_join_token_bootstrap_is_unchanged():
    script = _script(_material("join_token"))
    files = _files(script)
    assert files["spire/join.token"] == "JT-secret"
    assert 'NodeAttestor "join_token"' in files["spire/conf/agent.conf"]
    spire_line = next(x for x in script.splitlines() if "--name dashboard-agent-spire" in x)
    assert "--network host" not in spire_line and "-expandEnv" in spire_line


def test_azure_without_a_plain_tenant_domain_is_refused():
    for domain in ("", 'x" }} plugins {{ evil'):
        try:
            _script(_material("azure_imds", tenant_domain=domain))
        except ahs.AgentHostError:
            continue
        raise AssertionError(f"{domain!r} was accepted")


# ── node entries and eviction ─────────────────────────────────────────────────

class _Spire:
    """The spire-server CLI as JSON: records argv, holds one node entry."""
    def __init__(self, node_selectors=None, agents=None):
        self.calls = []
        self.node_selectors = node_selectors
        self.agents = agents or []

    def json(self, *args, **kw):
        self.calls.append(args)
        if args[:2] == ("entry", "show"):
            if args[3] == NODE and self.node_selectors is not None:
                return {"entries": [{"id": "node-entry", "selectors": [
                    {"type": s.split(":", 1)[0], "value": s.split(":", 1)[1]}
                    for s in self.node_selectors]}]}
            return {"entries": []}
        if args[:2] == ("agent", "list"):
            return {"agents": [{"id": {"trust_domain": TD, "path": p}} for p in self.agents]}
        return {"results": [{"status": {"code": 0}}]}

    def cli(self, *args, **kw):
        self.calls.append(args)
        return ""

    def __enter__(self):
        self._saved = dashboard_spire._json, dashboard_spire._cli
        dashboard_spire._json, dashboard_spire._cli = self.json, self.cli
        return self

    def __exit__(self, *exc):
        dashboard_spire._json, dashboard_spire._cli = self._saved


def test_registering_replaces_the_node_entry_with_one_pinning_the_vm():
    sels = dashboard_spire.cloud_node_selectors("aws", instance_id="i-0new")
    with _Spire(node_selectors=["aws_iid:instance:id:i-0old"]) as spire:
        dashboard_spire.register_cloud_node(NODE, sels)
    assert ("entry", "delete", "-entryID", "node-entry") in spire.calls
    create = [c for c in spire.calls if c[:2] == ("entry", "create")]
    assert create == [("entry", "create", "-node", "-spiffeID", NODE,
                       "-selector", "aws_iid:instance:id:i-0new")]


def test_forgetting_evicts_what_attested_as_the_vm_then_drops_the_entry():
    sels = ["gcp_iit:project-id:p", "gcp_iit:zone:z", "gcp_iit:instance-name:n"]
    attested = "/spire/agent/gcp_iit/p/5551234"
    with _Spire(node_selectors=sels, agents=[attested]) as spire:
        assert dashboard_spire.forget_cloud_node(NODE) == []
    listing = [c for c in spire.calls if c[:2] == ("agent", "list")][0]
    assert [listing[i + 1] for i, a in enumerate(listing) if a == "-selector"] == sels
    assert ("agent", "evict", "-spiffeID", f"spiffe://{TD}{attested}") in spire.calls
    assert ("entry", "delete", "-entryID", "node-entry") in spire.calls


def test_a_join_token_node_is_not_a_node_entry_and_is_left_alone():
    with _Spire(node_selectors=None) as spire:
        assert dashboard_spire.forget_cloud_node(NODE) == []
    assert not [c for c in spire.calls if c[:2] in (("agent", "list"), ("agent", "evict"),
                                                     ("entry", "delete"))]


def test_revoking_a_cloud_attested_agent_evicts_its_vm_too():
    sels = ["aws_iid:instance:id:i-0abc"]
    attested = "/spire/agent/aws_iid/111122223333/us-east-2/i-0abc"
    with _Spire(node_selectors=sels, agents=[attested]) as spire:
        problems = dashboard_spire.forget_agent("agent-1", f"spiffe://{TD}/agent/agent-1")
    assert problems == [], problems
    assert ("agent", "evict", "-spiffeID", f"spiffe://{TD}{attested}") in spire.calls


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
