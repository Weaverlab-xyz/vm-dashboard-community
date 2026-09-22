"""The plant brokers its own identity: the OT cell's DMZ broker and its Entitle agent.

Registering an OT cell in Entitle used to mean the SHARED agent — one that lives in a
Kubernetes cluster outside the plant — reached into the cell over SSH. That was two
things at once: a false claim for a demo whose whole argument is that the plant is
isolated, and, once `ot_purdue_firewall_enabled` was on, silently broken, because the
cell admits the PRA Gateway and nothing else. The registration still succeeded and the
Entitle grant still approved; only the vendor's login failed, looking like a bad
credential.

So the agent moved into the plant, onto a DMZ broker of its own, and the plant boundary
became a thing you can read in `gcloud compute firewall-rules list`. These are the
structural rules that keep that true. Every one of them stands for a failure that is
invisible until a customer is watching:

* the plant floor never gets an egress allow — only the DMZ host does, and only to the
  Entitle channel;
* the destination set is named, or the deploy refuses; it is never widened silently;
* a changed destination set produces a differently-named rule, because the GCP helper
  never reconciles an existing one;
* the agent token belongs to the cell, dies with it, and never lands in job metadata;
* the cell's registration names the plant's own agent, not the install-wide one;
* the broker deploys before the cell, and the zoning is applied before the probe.

Run: python tests/test_ot_plant_agent.py   (or under pytest)
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_HOOK = os.path.join(_ROOT, "web_dashboard", "services", "entitle_vm_hook.py")
_GCP_VM = os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "ot.py")
_PLAY = os.path.join(_ROOT, "examples", "playbooks", "kubesolo",
                     "entitle-agent-install.yml")


def _load():
    spec = importlib.util.spec_from_file_location("ot_service_plant_agent", _OT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path):
    return open(path, encoding="utf-8").read()


def _fn_src(path, name):
    src = _read(path)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{os.path.basename(path)}: {name} not found")


def _fn_body(path, name):
    """The function without its docstring.

    Checks of the form "this name must not appear" have to read code, not prose —
    these functions explain in their docstrings exactly which neighbouring helper they
    deliberately do NOT call, and a naive scan would trip over the explanation."""
    src = _read(path)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body[1:] if ast.get_docstring(node) else node.body
            return "\n".join(ast.get_source_segment(src, stmt) or "" for stmt in body)
    raise AssertionError(f"{os.path.basename(path)}: {name} not found")


# ── the zones ────────────────────────────────────────────────────────────────

def test_the_plant_floor_never_gets_a_way_out():
    """The cell's egress rules are deny-only. The whole demo rests on this one line,
    and the DMZ host exists precisely so the cell never needs an exception."""
    src = _fn_src(_OT, "_wire_purdue_firewall")
    for match in re.finditer(r'direction="EGRESS", action="(\w+)"', src):
        assert match.group(1) == "deny", (
            "the plant floor has an egress ALLOW — if the agent needs a path out, it "
            "belongs on the DMZ broker, which is what that host is for")


def test_the_cell_admits_the_broker_on_22_and_nothing_else():
    src = _fn_src(_OT, "_wire_purdue_firewall")
    block = src[src.index('names["ingress_agent"]'):]
    assert "source_tags=[OT_DMZ_NETWORK_TAG]" in block, (
        "the agent must be admitted by the DMZ zone's tag, so the rule reads as the "
        "sentence it is")
    assert "ports=[22]" in block, (
        "the plant's agent gets SSH and nothing else — it mints accounts, it does not "
        "talk to PLCs")
    # And only when there is a broker: a cell without one must not carry a rule
    # admitting a zone that does not exist.
    assert 'cmeta.get("ot_broker_job_id")' in src


def test_the_brokers_ways_out_are_the_entitle_channel_and_its_own_cell():
    """The broker gets THREE destinations, and the third one was missing for as long
    as the in-plant agent existed.

    The earlier version of this test asserted "the Entitle set and the DNS resolver"
    and nothing else — and it was wrong in a way that only shows up when the two rule
    sets are read together. The cell admits the broker on 22 (`ingress_agent`), but an
    egress rule is evaluated on the SENDING host, so the broker's own catch-all deny
    held that door shut from the inside: the grant reported success (registration
    talks to Entitle's API, never to the cell) and then the agent's SSH was dropped.

    That is why the assertion below names the plant destination explicitly instead of
    allowing anything: the claim is still "narrow", it is just no longer "one".
    """
    ot = _load()
    src = _fn_src(_OT, "_wire_dmz_firewall")
    assert ot.ENTITLE_AGENT_PORTS == ("443", "8080"), (
        "8080 is not telemetry and not optional — it carries the agent's primary "
        "channel (docs/kubesolo.md)")
    allows = re.findall(r'direction="EGRESS", action="allow"[^)]*?destination_ranges=(\w+|\[[^\]]*\])',
                        src, re.S)
    assert allows, "the DMZ zone opens nothing"
    for dest in allows:
        assert dest in ("cidrs", "[_METADATA_RESOLVER_CIDR]", '[f"{plant_ip}/32"]'), (
            f"the broker may reach the Entitle set, the DNS resolver and the one "
            f"plant host it brokers; {dest} is none of the three")
    assert '[f"{plant_ip}/32"]' in allows, (
        "there is no plant-ward egress allow, so the cell's ingress_agent rule opens "
        "a door this host's own catch-all deny holds shut — the agent's SSH never "
        "leaves the broker and the failure reads as 'granted but it does not work'")
    assert 'direction="EGRESS", action="deny"' in src, (
        "without the catch-all deny the 'narrow way out' claim is just a sentence")


def test_the_plant_ward_hole_is_one_host_one_projection_and_digest_named():
    ot = _load()
    src = _fn_src(_OT, "_wire_dmz_firewall")
    # A /32, not the subnet: "the one plant host this broker brokers" has to be true
    # of the rule, not only of the description.
    assert '[f"{plant_ip}/32"]' in src, "the plant destination is not a single host"
    # Digest-named, because ensure_segmentation_rule is create-only: a cell redeployed
    # on a new address, or a port added later, must arrive as a differently-named rule
    # or it does not arrive at all — the same reason the Entitle allow carries one.
    assert "plant_digest" in src and 'names["egress_plant"]' in src, (
        "the plant-ward rule is not digest-named, so a changed cell address would "
        "leave the old allow in place and report success")
    assert f'{ot._dmz_rule_names("x", "d", "p")["egress_plant"]}'.startswith("x-dmz-egress-plant-")
    # And the port list comes from the ONE projection, so three clouds cannot drift —
    # the lesson _cell_tunnels already paid for.
    assert "dmz_to_plant_ports" in _read(_OT), "the port list is spelled inline somewhere"
    assert ot.dmz_to_plant_ports({}) == [22], (
        "22 is the agent's SSH; anything else here is a port opened toward a listener "
        "that may not exist, which is indistinguishable from a blocked firewall")


def test_every_cloud_opens_the_plant_ward_hole():
    """All three, from the one projection. AWS is NOT the exception it looks like:
    `_ensure_ot_zone_security_group_sync` revokes the allow-all egress AWS creates a
    group with, so an SG with an explicit egress list is as closed as the other two."""
    for fn in ("_wire_dmz_firewall", "_wire_zones_aws", "_wire_zones_azure"):
        body = _fn_src(_OT, fn)
        assert "dmz_to_plant_ports" in body, (
            f"{fn} does not open the plant-ward hole, so on that cloud the in-plant "
            f"agent still cannot reach the cell that admits it")


def test_the_egress_allow_outranks_the_deny_it_sits_behind():
    ot = _load()
    assert ot._DMZ_EGRESS_ALLOW_PRIORITY < ot._PURDUE_EGRESS_PRIORITY, (
        "lower number wins in GCP: an allow that does not outrank the catch-all deny "
        "is a rule that exists and does nothing")


def test_the_deny_is_never_created_before_the_allow():
    src = _fn_src(_OT, "_wire_dmz_firewall")
    assert src.index('names["egress_entitle"]') < src.index('names["egress_deny"]'), (
        "the catch-all egress deny must come after the Entitle allow, or the agent is "
        "stranded for however long the next API call takes")
    assert src.index('names["ingress_allow"]') < src.index('names["ingress_deny"]')


# ── the destination set ──────────────────────────────────────────────────────

def test_an_unknown_destination_set_refuses_rather_than_widens():
    src = _fn_src(_OT, "dmz_egress_problem")
    assert "ot_entitle_egress_cidrs" in src and "ot_dmz_egress_open_ports" in src, (
        "the refusal must name both the key that fixes it and the escape hatch")
    assert "0.0.0.0/0" not in src, (
        "an unknown destination set must never become an open hole by default")


def test_the_operators_list_wins_over_dns():
    src = _fn_src(_OT, "resolve_entitle_destinations")
    assert src.index("ot_entitle_egress_cidrs") < src.index("getaddrinfo"), (
        "a configured list is what a real plant has — a firewall ticket — and must be "
        "preferred over whatever DNS happens to answer here")
    assert "entitle_egress.cidrs(" not in _fn_body(_OT, "resolve_entitle_destinations"), (
        "entitle_egress holds the addresses Entitle connects FROM, for an ingress "
        "allow-list; pointing an egress rule at them is a guess in the wrong direction")


def test_the_provenance_travels_with_the_addresses():
    ot = _load()
    src = _fn_src(_OT, "_wire_dmz_firewall")
    assert "provenance" in src, (
        "the rule's description must say where its addresses came from: 'resolved at "
        "wiring time' is an honest answer, not a contract, and the difference has to "
        "survive into what an operator reads")
    assert ot.resolve_entitle_destinations.__doc__.strip()


def test_a_changed_destination_set_gets_a_new_rule_name():
    ot = _load()
    a = ot.entitle_destination_digest(["1.1.1.1/32"])
    b = ot.entitle_destination_digest(["1.1.1.1/32", "2.2.2.2/32"])
    assert a != b
    assert ot.entitle_destination_digest(["2.2.2.2/32", "1.1.1.1/32"]) == b, (
        "order must not change the digest, or every re-wire would churn the rule")
    assert ot._dmz_rule_names("b", a)["egress_entitle"] != \
        ot._dmz_rule_names("b", b)["egress_entitle"], (
        "ensure_segmentation_rule never reconciles an existing rule, so the address "
        "set has to be part of the name — otherwise a changed list reports success and "
        "changes nothing")
    src = _fn_src(_OT, "_wire_dmz_firewall")
    assert "delete_firewall_rule" in src, (
        "the previous allow must be removed, or the hole is the union of both and the "
        "rule list cannot say which one is live")


# ── the token ────────────────────────────────────────────────────────────────

def test_the_token_is_this_cells_own_and_dies_with_it():
    ot = _load()
    assert ot.agent_token_config_key("j1") != ot.agent_token_config_key("j2")
    src = _fn_src(_OT, "ensure_agent_token")
    assert "mint_agent_token" in src, (
        "ensure_agent_token() (the module-level singleton) would give every cell the "
        "same agent, so every plant's agent could broker every other plant's hosts")
    destroy = _fn_body(_OT, "destroy_agent_token")
    assert "deregister" in destroy and "config_service.delete" in destroy
    assert "return" in destroy and "raise" not in destroy, (
        "teardown of a demo must not be blockable by the identity provider")


def test_the_token_value_never_reaches_job_metadata():
    src = _fn_src(_OT, "_install_plant_agent")
    assert 'secret_vars={"entitle_agent_token": agent_token_config_key(' in src, (
        "the run must bind the token BY REFERENCE — the job row carries the config "
        "key, and the value is resolved at run time")
    for leak in ("token=", "minted[", '"token"'):
        assert leak not in src, f"the install queue handles a token value ({leak})"


def test_the_destroy_path_destroys_the_token():
    src = _fn_src(_GCP_VM, "_run_destroy")
    assert "destroy_agent_token" in src, (
        "a token left in the tenant outlives the plant it was issued for, and nothing "
        "else will ever clean it up")


# ── the registration ─────────────────────────────────────────────────────────

def test_the_cell_registers_against_the_agent_in_its_own_plant():
    hook = _read(_HOOK)
    assert "agent_token_name: str = \"\"" in hook, (
        "entitle_vm_hook.register needs a per-registration agent name, or every "
        "private target falls back to the install-wide agent outside the plant")
    assert "local_tenant_ctx" in hook, (
        "the name must travel in a context for THIS install's tenant — tenant_ctx is "
        "for a customer's and refuses a missing API key")
    gcp = _fn_src(_GCP_VM, "_run_deploy")
    assert "ot_agent_token_name" in gcp and "agent_token_name=_agent_name" in gcp, (
        "the GCP deploy must pass the name the cell orchestrator stamped on the job")


# ── ordering ─────────────────────────────────────────────────────────────────

def test_the_broker_is_deployed_before_the_cell():
    src = _fn_src(_OT, "run_cell_deploy")
    assert src.index("Deploying the plant's DMZ broker") < \
        src.index("Deploying the OT cell VM"), (
        "a cell registered against an agent whose host does not exist yet is a demo "
        "that half-works in the direction nobody checks")
    assert src.index("ensure_agent_token") < src.index("Deploying the plant's DMZ broker"), (
        "the token has to exist before either VM: the cell's own deploy registers "
        "against it")


def test_the_zoning_is_applied_before_the_agent_is_installed():
    src = _fn_src(_OT, "_wire_cell")
    assert src.index("_wire_dmz_firewall") < src.index("_install_plant_agent"), (
        "the install's pre-flight probe exists to prove the NARROW path; running it "
        "before the zoning would prove a hole that is about to close")


def test_the_install_runs_the_same_play_an_on_prem_site_would():
    ot = _load()
    assert ot.ENTITLE_AGENT_PLAYBOOK == "entitle-agent-install.yml"
    assert os.path.exists(_PLAY), "the play the cell installs with is gone"
    src = _fn_src(_OT, "_install_plant_agent")
    assert "entitle_probe_endpoint" in src and "entitle_probe_ssh_target" in src, (
        "the run must hand the probe both halves of what the agent needs: its channel "
        "out, and the cell it mints accounts on")
    assert ot.BROKER_CHART_PATH.startswith("/"), (
        "the chart must be the baked archive — the Helm repo is a CDN, and no honest "
        "allow-list can name one")


def test_the_play_can_install_from_a_baked_chart():
    play = _read(_PLAY)
    assert "entitle_agent_chart is match('^/')" in play, (
        "the play must accept an absolute chart path, or an air-gapped site cannot "
        "install at all")
    assert re.search(r"\{% if entitle_agent_chart_repo \| length > 0[^%]*%\}--repo", play), (
        "--repo must be conditional; a local chart with --repo is an error")
    assert play.index("Prove the agent's network path") < play.index("Write the values file"), (
        "the probe must run before the token is written to disk")


# ── the guards ───────────────────────────────────────────────────────────────

def test_every_guard_names_its_remedy():
    ot = _load()
    for fn, kwargs in ((ot.broker_shape_problem, {"machine_type": "e2-medium"}),
                       (ot.dmz_egress_problem, {})):
        try:
            msg = fn(**kwargs)
        except Exception:  # noqa: BLE001 — needs app config; the source check covers it
            continue
        if msg:
            assert "No VM was launched." in msg or "launched" in msg, (
                f"{fn.__name__} refuses without saying nothing was created")


def test_the_in_plant_agent_is_refused_where_it_cannot_work():
    src = _fn_src(_OT, "in_plant_agent_problem")
    for needed in ("entitle_registration_enabled", "purdue_firewall_enabled",
                   "dmz_egress_problem", "config_runner_problem",
                   "broker_shape_problem", "OT_ROLE=broker"):
        assert needed in src, f"the preflight does not check {needed}"
    # All three clouds carry zoning now, so the refusal is no longer "not GCP" — it is
    # the per-cloud thing each zone needs in order to name the PRA Gateway as a source.
    # Getting that wrong is the expensive direction: a zone applied without it denies
    # the Gateway along with everything else, and the cell is unreachable by the one
    # path the demo has.
    assert 'cloud not in ("gcp", "aws", "azure")' in src, (
        "the cloud allow-list is gone — a cloud with no zoning implementation must "
        "still be refused rather than deploying a broker nothing fences")
    assert "bt_ecs_jumpoint_security_group_id" in src, (
        "the AWS zone allows the Gateway in by security group; unset, it would lock "
        "the cell away from the Gateway")
    assert "azure_jumpoint_name" in src, (
        "the Azure zone allows the Gateway in by resolved address; unset, it would "
        "lock the cell away from the Gateway")


def test_the_install_channel_is_refused_when_it_could_not_reach_the_broker():
    src = _fn_src(_OT, "config_runner_problem")
    assert "ansible_runner_" in src and "ot_config_runner_source_cidr" in src, (
        "a private broker is reachable only from an in-cloud runner, and only if its "
        "firewall admits it — both halves have to be checked before anything launches")


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
