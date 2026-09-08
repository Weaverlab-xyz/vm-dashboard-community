"""Wiring tests for the SPIRE Lab preview feature.

Text-and-registry checks rather than behaviour: every one of these pins a connection that
fails LATE and quietly if it is missed — a job type the worker will not claim, a
concurrency tier that DEADLOCKS a provision against its own children, an auto-delete kind
that silently never closes a port that mints identities.

No app imports, so it runs on a checkout without the requirements installed.
Runs under pytest or standalone:  python tests/test_spire_lab_wiring.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── the per-cloud backends actually exist ─────────────────────────────────────

def test_every_declared_cloud_has_an_ingress_primitive_behind_it():
    """`PROVISIONING_CLOUDS` is derived from `_HOST_BACKENDS`, so a cloud can only be
    offered if it has a backend. This pins the layer below that: the backend has to call
    a function that EXISTS. A name that does not is invisible to both the tests and the
    import — it raises only when an operator clicks Build, in a worker.
    """
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    assert "PROVISIONING_CLOUDS = tuple(c for c in VALID_CLOUDS if c in _HOST_BACKENDS)" in svc
    expected = {
        "azure": ("azure_service.py", "async def ensure_vm_inbound_rule("),
        "gcp": ("gcp_service.py", "async def ensure_instance_inbound_rule("),
        "aws": ("aws_service.py", "async def ensure_instance_inbound_rule("),
    }
    for cloud, (module, signature) in expected.items():
        assert f'"{cloud}": _' in svc, f"{cloud} has no entry in _HOST_BACKENDS"
        assert signature in _read("web_dashboard", "services", module), \
            f"{cloud}'s backend calls a function {module} does not define"


def test_every_cloud_fails_closed_on_an_empty_source_set():
    """An EMPTY source set must leave the port unreachable, not untouched. How differs
    per cloud — Azure deletes the NSG rule, GCP deletes the firewall rule, AWS revokes
    (an in-use security group cannot be deleted) — but `opened` is False either way, and
    both the teardown and the ACL button key off that rather than off the absence of an
    error."""
    az = _read("web_dashboard", "services", "azure_service.py")
    az_block = az.split("def _ensure_vm_inbound_rule_sync(")[1].split("\nasync def ")[0]
    assert "security_rules.begin_delete" in az_block
    assert '"opened": bool(source_cidrs)' in az_block

    gcp = _read("web_dashboard", "services", "gcp_service.py")
    gcp_block = gcp.split("def _ensure_instance_inbound_rule_sync(")[1].split("\nasync def ")[0]
    assert "if not source_cidrs:" in gcp_block
    assert "firewalls.delete(" in gcp_block
    assert '"opened": False' in gcp_block

    aws = _read("web_dashboard", "services", "aws_service.py")
    aws_block = aws.split("def _ensure_instance_inbound_rule_sync(")[1].split("\nasync def ")[0]
    assert "if not source_cidrs:" in aws_block
    assert "revoke_security_group_ingress" in aws_block
    assert '"opened": False' in aws_block


def test_the_gcp_rule_also_tags_the_instance():
    """A GCE firewall rule cannot name an instance — it selects by network tag. So
    opening a port is TWO writes, and a rule created without the tag is the failure that
    looks correct in the console and does nothing."""
    gcp = _read("web_dashboard", "services", "gcp_service.py")
    block = gcp.split("def _ensure_instance_inbound_rule_sync(")[1].split("\nasync def ")[0]
    assert "_ensure_instance_tag_sync(" in block
    assert "fw.target_tags = [tag]" in block
    # And the tag write must be a read-modify-write against the fingerprint, never a
    # blind set — a blind set drops every other tag the instance carries.
    tag_block = gcp.split("def _ensure_instance_tag_sync(")[1].split("\ndef ")[0]
    assert "fingerprint" in tag_block
    assert "items.append(tag)" in tag_block


def test_the_azure_rule_prefers_the_nic_nsg_over_the_subnet_one():
    """Azure evaluates the NIC's NSG before the subnet's, so a rule written to the wrong
    one exists and does nothing."""
    az = _read("web_dashboard", "services", "azure_service.py")
    block = az.split("def _ensure_vm_inbound_rule_sync(")[1].split("\nasync def ")[0]
    nic_at = block.index("nic.network_security_group")
    subnet_at = block.index("subnet.network_security_group")
    assert nic_at < subnet_at, "the subnet NSG must only be a fallback"


def test_the_lab_rule_name_is_not_the_managed_node_rule_name():
    """A VM can be both a SPIRE lab host and a Rancher/Portainer managed node. Sharing a
    rule name would have one feature's ingress silently overwrite the other's."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    assert 'RULE_NAME = "allow-spire-api"' in svc
    az = _read("web_dashboard", "services", "azure_service.py")
    assert '_NODE_RULE_NAME = "allow-mgmt"' in az, "sanity: the managed-node rule name"


# ── the worker will actually claim the jobs, in a tier that cannot deadlock ────

def test_every_job_type_is_handled_tiered_and_dispatched():
    worker = _read("web_dashboard", "jobs_worker.py")
    for job_type in ("spirelab_provision", "spirelab_decommission"):
        assert f'"{job_type}"' in worker, f"{job_type} missing from jobs_worker"
        assert f'elif job_type == "{job_type}":' in worker, f"{job_type} has no dispatch branch"
    heavy = worker.split("HEAVY_TYPES = (")[1].split("\n)")[0]
    medium = worker.split("MEDIUM_TYPES = (")[1].split("\n)")[0]
    light = worker.split("LIGHT_TYPES = (")[1].split("\n)")[0]
    for job_type in ("spirelab_provision", "spirelab_decommission"):
        tiers = [name for name, body in
                 (("heavy", heavy), ("medium", medium), ("light", light))
                 if f'"{job_type}"' in body]
        assert len(tiers) == 1, f"{job_type} is in {tiers}, expected exactly one tier"


def test_the_provision_is_light_because_it_waits_on_a_heavy_child():
    """**This is a deadlock constraint, not a judgement about duration.**

    `spirelab_provision` drives four `ansible_local` children and AWAITS each. Those are
    HEAVY, and the HEAVY cap can be 1 — so a parent holding a HEAVY slot while waiting on
    a HEAVY child would wait forever, taking the whole tier down with it. The parent runs
    no local process and streams no output; the children do.
    """
    worker = _read("web_dashboard", "jobs_worker.py")
    light = worker.split("LIGHT_TYPES = (")[1].split("\n)")[0]
    heavy = worker.split("HEAVY_TYPES = (")[1].split("\n)")[0]
    assert '"spirelab_provision"' in light
    assert '"ansible_local"' in heavy, "sanity: the child tier this must not share"
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    assert "await ansible_local_run_service.run(" in svc


def test_the_stage_children_are_queued_so_the_worker_cannot_double_run_them():
    """The runner claims on `status='pending'` under a handled type. A stage child
    created `pending` would be claimed and run a SECOND time, concurrently with the
    parent that is already running it — two `ansible-playbook` processes against one
    host."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("async def _run_stage(")[1].split("\nasync def ")[0]
    assert 'status="queued"' in block
    assert '"ansible_local"' in block


def test_stage_metadata_goes_through_the_run_meta_allowlist():
    """`ansible_run_meta.RUN_META_KEYS` is the closed allowlist that keeps a credential
    out of the jobs table. A second hand-rolled metadata dict beside it would be a second
    place for one to leak."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    assert "ansible_run_meta.run_meta(" in svc
    assert "from . import ansible_run_meta" in svc


# ── the four playbooks, in order, and staged where a run can fetch them ───────

def test_the_stages_are_the_four_playbooks_in_the_documented_order():
    """Order is not cosmetic: every stage after the first asserts the server is up, and
    the identity mint needs the seeded entries' parent to exist."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("STAGES = (")[1].split("\n)")[0]
    order = [a for a in ("spire-server-install.yml", "spire-open-ports.yml",
                         "spire-seed-entries.yml", "spire-admin-identity.yml")]
    positions = [block.index(a) for a in order]
    assert positions == sorted(positions), f"stages out of order: {block}"
    for asset in order:
        assert os.path.exists(os.path.join(_ROOT, "examples", "playbooks", "spire", asset))


def test_a_failed_stage_stops_the_sequence():
    """Continuing past a failure turns one legible Ansible error into four, because every
    later stage asserts the server is up."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("async def run_provision(")[1].split("\ndef ")[0]
    assert 'if status != "completed":' in block
    assert "raise SpireLabError(" in block


def test_the_options_route_reports_unstaged_playbooks():
    """A run fetches assets BY FILENAME from the storage backend and never from
    examples/. An un-uploaded playbook is a mid-provision failure otherwise."""
    api = _read("web_dashboard", "api", "spire_lab.py")
    assert "list_assets_in(" in api
    assert "STAGE_ASSETS" in api


# ── both gates carry the same source set ─────────────────────────────────────

def test_the_host_firewall_gets_the_same_cidrs_as_the_cloud_acl():
    """A closed cloud ACL and a closed host firewall present IDENTICALLY — a gRPC timeout
    on Verify Functional Account. A lab where one gate is narrower than the other is the
    hardest version of this to debug, so they are fed from one place."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    ports = svc.split("def _ports_vars(")[1].split("\ndef ")[0]
    assert "_row_cidrs(row)" in ports
    assert "spire_source_cidrs" in ports
    # And the playbook has to accept the list form, or the extra_var is silently ignored.
    pb = _read("examples", "playbooks", "spire", "spire-open-ports.yml")
    assert "spire_source_cidrs: []" in pb
    assert "_sources" in pb


def test_a_blank_source_set_opens_nothing_and_says_so():
    """Blank must not mean "open to the world": 8081 is an API that mints identities, so
    an unstated source set can only mean "nobody new". It also must not be silent, because
    it is the first thing to check when the plugin later times out."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("async def run_provision(")[1].split("\ndef ")[0]
    assert "if cidrs:" in block
    assert "spire_lab_source_cidrs is " in block, "the blank case must explain itself"


# ── the credential never comes back through the job log ──────────────────────

def test_the_public_artifacts_come_from_secrets_safe_not_from_the_log():
    """There is no output-as-value channel in this runner — a job's "output" IS a captured
    log — so the playbook publishes the two public values as text secrets and the service
    READS them. Parsing them back out of the log would mean parsing Ansible's callback
    formatting."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    assert "read_bt_secrets_safe(" in svc
    pb = _read("examples", "playbooks", "spire", "spire-admin-identity.yml")
    assert "title: trust-bundle-pem" in pb
    assert "title: admin-svid-expires" in pb
    # Every secrets_create in that play must be no_log — the OAuth client secret is in
    # the module args, and a failed task dumps its arguments.
    for chunk in pb.split("beyondtrust.secrets_safe.secrets_create:")[1:]:
        task = chunk.split("\n    - name:")[0]
        assert "no_log: true" in task, f"a secrets_create without no_log:\n{task[:300]}"


def test_the_service_never_reads_the_credential_only_names_it():
    """The functional account's DSS-key field is the protected place built for the
    PKCS#12. A dashboard that read it would be a second copy in a place with no such
    protection."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    reader = svc.split("def read_public_artifacts(")[1].split("\ndef ")[0]
    assert 'refs["bundle"]' in reader and 'refs["expires"]' in reader
    assert '"pfx"' not in reader and '"passphrase"' not in reader


# ── the auto-delete timer reaches the trust domain ───────────────────────────

def test_spirelab_is_a_reapable_kind_with_an_idle_state_and_a_teardown():
    policy = _read("web_dashboard", "services", "expiry_policy.py")
    assert '"spirelab"' in policy.split("REAPABLE_KINDS = (")[1].split(")")[0]
    assert '"spirelab": frozenset({"available"})' in policy
    states = policy.split('"spirelab": frozenset({')[1].split("})")[0]
    assert "failed" not in states, "a half-built lab needs a human, not a race"

    reaper = _read("web_dashboard", "services", "expiry_reaper.py")
    assert 'elif kind == "spirelab":' in reaper
    assert "spire_lab_service.start_decommission" in reaper
    assert "SpireLab" in reaper


def test_the_inventory_emits_a_spirelab_row_so_the_sweep_can_find_it():
    inv = _read("web_dashboard", "services", "inventory_service.py")
    assert "def _spirelab_item(" in inv
    assert '"kind": "spirelab"' in inv
    assert "items.append(_spirelab_item(row))" in inv
    # Unconditional, like the Certificate Lab and POV rows: turning the feature off hides
    # the page, it does not close tcp/8081.
    assert "db.query(SpireLab)" in inv


def test_the_timer_is_stamped_at_provision_because_null_means_never():
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    assert "expiry_policy.default_expiry_for_kind(INVENTORY_KIND)" in svc


def test_teardown_clears_the_timer_in_the_same_transaction():
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("def start_decommission(")[1].split("\nasync def ")[0]
    assert "row.expires_at = None" in block


def test_a_failed_teardown_does_not_re_arm_the_timer():
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    tail = svc.split("async def run_decommission(")[1].split("except Exception as exc:")[1]
    assert "expires_at" not in tail


def test_the_teardown_closes_the_port_and_leaves_the_vm_alone():
    """Destroying somebody's host because a lab expired is a bigger surprise than leaving
    a server nothing can reach — and the VM has its own timer and its own Destroy."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("async def run_decommission(")[1]
    assert "apply_ingress(placement, [row.bind_port], [])" in block
    assert "terminate" not in block, "the teardown must not destroy the host VM"
    assert '"vm_destroyed": False' in block


# ── the preview flag ──────────────────────────────────────────────────────────

def test_it_ships_as_a_preview_flag_with_a_config_panel():
    setup = _read("web_dashboard", "api", "setup.py")
    flags = setup.split("_PREVIEW_FLAGS = {")[1].split("\n}")[0]
    assert '"spire_lab_enabled"' in flags
    assert '"cert_lab_enabled"' in flags, "sanity: the reference preview flag"
    assert '"spire_lab_enabled": "spire_lab"' in setup
    assert '"spire_lab": SpireLabFeatureConfig' in setup
    assert '"spire_lab"' in setup.split("_CONFIG_ONLY_FEATURES = {")[1].split("}")[0]


def test_it_is_demo_only_for_the_tenancy_reason():
    """The administrative credential is written into Secrets Safe through the global
    pscli_* singletons, so on a POV instance it would land in the wrong customer's
    tenant — exactly the Certificate Lab's argument."""
    flags = _read("web_dashboard", "services", "feature_flags.py")
    demo = flags.split("_DEMO_ONLY = (")[1].split("\n)")[0]
    assert '"spire_lab_enabled"' in demo
    assert '"cert_lab_enabled"' in demo, "sanity: the reference demo-only flag"


def test_the_flag_gates_the_router_the_page_and_the_nav():
    main = _read("web_dashboard", "main.py")
    assert "app.include_router(spire_lab_api.router," in main
    assert main.count('_feature_gate("spire_lab_enabled")') >= 2, "router AND page"
    assert '@app.get("/spire-lab"' in main
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    assert "{% if spire_lab_enabled %}" in nav
    flags = _read("web_dashboard", "services", "feature_flags.py")
    assert '"spire_lab_enabled"' in flags


def test_the_page_carries_the_preview_badge_and_never_bare_fetches():
    page = _read("web_dashboard", "templates", "spire_lab", "index.html")
    assert ">Preview</span>" in page
    # Every call goes through window.API, which attaches the bearer token. A bare fetch
    # would be anonymous and 401 -- there is no auth cookie on this app. Comment lines
    # are stripped first: the page explains that rule in a comment, and matching the
    # explanation instead of the code is how this assertion fires on the wrong thing.
    code = chr(10).join(l for l in page.splitlines()
                        if not l.strip().startswith("//"))
    assert "fetch(" not in code
    assert "API.get(" in code and "API.post(" in code and "API.del(" in code


def test_the_page_shows_the_discovery_count_not_just_success():
    """"Discovery succeeded" is not the assertion. The plugin once shipped with discovery
    filtering on the MINTABLE prefix, which narrowed the inventory to 2 accounts while
    the action still reported success."""
    page = _read("web_dashboard", "templates", "spire_lab", "index.html")
    assert "discovery_expected" in page
    assert "entries_seeded" in page


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
