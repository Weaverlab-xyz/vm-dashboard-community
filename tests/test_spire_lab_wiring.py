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


# ── a third party's exception text never reaches the caller ──────────────────

def test_a_broad_except_logs_the_real_error_and_returns_a_generic_one():
    """CodeQL ``py/stack-trace-exposure``, and the reason it matters here specifically.

    Both broad handlers in this router wrap a THIRD PARTY: a storage backend and a cloud
    SDK. Their exception strings carry request ids, subscription/project identifiers,
    bucket names and whole response bodies, and every route here is reachable by any
    ``cloud_function`` reader. So the rule the repo already follows (see
    ``api/config_mgmt``'s managed-account lookup and ``api/k8s``'s token status) is: log
    the real thing server-side, return a reason that names the server logs.

    Our OWN ``SpireLabError`` is exempt and deliberately returned verbatim — it is a
    message authored in this repo, which is the same exemption ``K8sError`` gets.
    """
    import ast as _ast
    src = _read("web_dashboard", "api", "spire_lab.py")
    tree = _ast.parse(src)
    checked = 0
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.ExceptHandler):
            continue
        # Only the broad ones. A named app error is the exempt case.
        if not (isinstance(node.type, _ast.Name) and node.type.id == "Exception"):
            continue
        checked += 1
        body = chr(10).join(_ast.unparse(stmt) for stmt in node.body)
        name = node.name or "exc"
        assert "logger." in body, (
            f"broad except at line {node.lineno} does not log the real error")
        assert f"str({name})" not in body, (
            f"broad except at line {node.lineno} returns str({name}) to the caller")
        # An f-string interpolation is the same leak spelled differently, and is the
        # form this actually shipped as before CodeQL caught it.
        for stmt in node.body:
            for sub in _ast.walk(stmt):
                if isinstance(sub, _ast.FormattedValue):
                    refs = {n.id for n in _ast.walk(sub) if isinstance(n, _ast.Name)}
                    # The logger call is allowed to interpolate; a response is not.
                    parent_is_log = "logger" in _ast.unparse(stmt)
                    assert not (name in refs and not parent_is_log), (
                        f"broad except at line {node.lineno} interpolates {name} into "
                        f"a value returned to the caller")
    assert checked >= 2, (
        f"expected the storage-listing and ACL handlers to be broad excepts; "
        f"found {checked}")


# ── the credential has somewhere to land, checked first ──────────────────────

def test_the_secrets_folder_preflight_runs_before_the_acl():
    """Ordering is the whole value. The identity playbook is the LAST of four and writes
    into a folder it does not create, so a missing folder surfaces after the server is
    installed and seeded — as an error that reads like a credential fault. Checking first
    costs two list calls."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("async def run_provision(")[1].split(chr(10) + "def ")[0]
    assert "ensure_secret_folder(row)" in block
    folder_at = block.index("ensure_secret_folder(row)")
    acl_at = block.index("apply_ingress(")
    assert folder_at < acl_at, "the folder check must precede the cloud ACL"
    stage_at = block.index("_run_stage(")
    assert folder_at < stage_at, "the folder check must precede every playbook"


def test_the_folder_walk_has_exactly_one_implementation():
    """`cert_ps_service` had this first; the SPIRE lab needs the identical thing. Two
    copies would drift, and the two subtleties in it — re-reading the folder list each
    round, and matching on BOTH name and parent_id — are not the kind anyone reproduces
    correctly from memory.

    Read from the AST, not the text. The first version of this test matched the NAME
    anywhere in the file and so was satisfied by the docstring that merely *mentions* the
    helper — it passed against a module that had stopped calling it.
    """
    import ast as _ast

    def _referenced(module: str) -> set:
        """Every attribute/function name this module actually references in code."""
        tree = _ast.parse(_read("web_dashboard", "services", module))
        names = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, _ast.Name):
                names.add(node.id)
        return names

    secrets = _read("web_dashboard", "services", "secrets_backend_service.py")
    assert "def ensure_bt_folder_path(" in secrets

    for module in ("cert_ps_service.py", "spire_lab_service.py"):
        refs = _referenced(module)
        assert "ensure_bt_folder_path" in refs, (
            f"{module} does not CALL the shared walk (a docstring mentioning it is not "
            f"the same thing)")
        # The primitives the walk is built from belong to it alone. A caller reaching for
        # them is a caller growing a second copy.
        for primitive in ("create_bt_folder", "list_bt_folders", "list_bt_safes"):
            assert primitive not in refs, (
                f"{module} references {primitive} — that is the shared walk's job")


def test_the_safe_is_never_created_only_the_folders_under_it():
    """A safe carries its own ACL, and that ACL is half the access boundary on whatever
    lands inside. A safe appearing because an automation asked for one is a boundary
    nobody chose."""
    secrets = _read("web_dashboard", "services", "secrets_backend_service.py")
    block = secrets.split("def ensure_bt_folder_path(")[1].split(chr(10) + "def ")[0]
    assert "create_bt_safe" not in block
    # A missing safe is an error that names the ones the API user can actually see —
    # otherwise the operator cannot tell "wrong name" from "no access".
    assert "has no safe named" in block
    assert "Safes visible to the API user" in block


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


# ── the connection identity: chosen once, applied to every stage ─────────────

def test_the_stage_payload_reads_the_credential_off_the_row():
    """Not hard-coded empty, and not read from the request either. Every `vars_for`
    builder takes only the row + config so a RESUMED provision rebuilds an identical
    run; a credential read from anywhere else would change identity halfway through a
    build that had already failed once."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    # The whole builder, because the ref is resolved just above the payload class.
    meta_fn = svc.split("def _stage_meta(")[1].split("\nasync def ")[0]
    assert "managed_ref(row)" in meta_fn
    payload = meta_fn.split("class _Payload:")[1].split("return ansible_run_meta")[0]
    assert "row.ansible_secret_ssh_key_source" in payload
    assert "row.ansible_managed_become_self" in payload
    assert "row.login_user" in payload
    # The pre-existing hard-coded empties for the kinds this form does NOT offer must
    # stay empty rather than becoming undeclared -- run_meta would default them anyway,
    # but a bound-but-undeclared field is how a slot silently stops being sent.
    assert "secret_vars = None" in payload
    assert 'secret_become_source = ""' in payload
    assert 'epml_token_var = ""' in payload


def test_the_choice_is_not_written_into_the_parent_jobs_metadata():
    """The parent `spirelab_provision` job carries only pointers. A ref in there would
    be a second copy that could disagree with the row the stages actually read."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    block = svc.split("job = job_service.create_job(")[1].split(")")[0]
    for field in ("ansible_managed_account", "ansible_secret_ssh_key_source",
                  "managed_account", "login_user"):
        assert field not in block, f"{field} must not ride the parent job's metadata"


def test_the_managed_account_ref_is_imported_not_redeclared():
    """One definition, so its "pinned ids or a name" validator cannot drift. A second
    copy would present as a run checking out the wrong host's credential."""
    api = _read("web_dashboard", "api", "spire_lab.py")
    assert "from .config_mgmt import" in api and "ManagedAccountRef" in api
    assert "class ManagedAccountRef" not in api
    # ...and no cycle: config_mgmt must not reach back into this router.
    cm = _read("web_dashboard", "api", "config_mgmt.py")
    assert "spire_lab" not in cm


def test_the_build_route_applies_the_shared_credential_gate():
    api = _read("web_dashboard", "api", "spire_lab.py")
    build = api.split("def build_lab(")[1].split("\n@router.")[0]
    assert "check_permission(" in build
    assert "check_runner_capability(" in build
    assert "requires_ephemeral_store(" in build, (
        "an AWS/GCP lab dispatches to ECS / Cloud Run, where a just-in-time credential "
        "needs the ephemeral-store opt-in; an Azure lab on ACI does not")
    # The gate is consulted BEFORE the row is written: a refused build must leave no
    # inventory behind.
    assert build.index("check_permission(") < build.index("spire_lab_service.provision(")


def test_the_gate_owns_the_refusal_wording_and_the_api_layers_only_raise_it():
    """Each operator-facing sentence exists exactly ONCE, in the gate. Two copies is how
    two pages end up telling one operator different things about one Settings checkbox.
    """
    gate = _read("web_dashboard", "services", "ansible_run_gate.py")
    api = _read("web_dashboard", "api", "spire_lab.py")
    cm = _read("web_dashboard", "api", "config_mgmt.py")
    sentences = (
        "requires the 'secrets:use' permission.",
        "Managed-account checkout requires BeyondTrust Password Safe",
        "'Ephemeral cloud secrets' to be enabled in Settings",
        "GCP ephemeral secrets require 'gcp_ansible_runner_service_account'",
    )
    for s in sentences:
        assert gate.count(s) == 1, f"the gate should word {s!r} exactly once"
        assert s not in api, f"api/spire_lab re-words {s!r}"
        assert s not in cm, f"api/config_mgmt re-words {s!r}"


def test_the_gate_is_pure_and_loadable_without_the_app():
    """Stdlib only and no sibling imports -- the property that lets it be unit-tested by
    file path, the way managed_accounts and ansible_run_meta are. It takes
    `needs_ephemeral_store` as a BOOL for the same reason."""
    gate = _read("web_dashboard", "services", "ansible_run_gate.py")
    # Import STATEMENTS only -- the module explains the rule in prose, and matching the
    # explanation instead of the code is how this fires on the wrong thing.
    imports = [l.strip() for l in gate.splitlines()
               if l.startswith(("import ", "from "))]
    assert imports == ["from dataclasses import dataclass"], imports
    assert "raise HTTPException" not in gate
    assert "needs_ephemeral_store" in gate
    # It takes the ANSWER, never computes it: the call has to stay in api/config_mgmt,
    # where tests/test_database_registration pins its position.
    assert "requires_ephemeral_store(" not in gate


def test_the_new_columns_are_text_and_every_one_has_a_migration():
    """`spire_labs` had no migration entries at all, so the table has only ever arrived
    via create_all -- without these, an existing install gets the model attributes and
    not the columns. Text for the refs because a bt_safe:// ref has no bounded length
    (issue #830: SQLite enforces no VARCHAR width and PostgreSQL does)."""
    db = _read("web_dashboard", "database.py")
    cls = db.split("class SpireLab(Base):")[1].split("\nclass ")[0]
    for col in ("ansible_secret_ssh_key_source", "ansible_managed_account"):
        assert f"{col} = Column(Text" in cls, f"{col} should be Text, not VARCHAR(n)"
        assert f"ALTER TABLE spire_labs ADD COLUMN {col} TEXT" in db
    assert "ansible_managed_become_self = Column(Boolean" in cls
    assert "login_user = Column(String(104)" in cls
    assert "ALTER TABLE spire_labs ADD COLUMN login_user VARCHAR(104)" in db
    # A bare BOOLEAN: PostgreSQL rejects an integer default on a boolean column, the
    # per-statement savepoint rolls the ALTER back, and the column silently never
    # appears -- invisible to a SQLite run.
    assert "ADD COLUMN ansible_managed_become_self BOOLEAN\"" in db


def test_the_cloud_runner_refuses_a_run_with_no_usable_credential():
    """An EMPTY key file is worse than no key file: every cloud runner writes
    /tmp/ssh_key unconditionally and always passes --private-key, so a blank one fails
    as `Permission denied (publickey)` -- which reads as a credential the host rejected
    rather than one that was never found."""
    svc = _read("web_dashboard", "services", "ansible_local_run_service.py")
    guard = svc.split("_pw_vars = (")[1].split("ssh_key_b64 = base64")[0]
    assert "_abandon(" in guard, "the guard must fail the job, not fall through"
    # "no key AND no password": a managed PASSWORD account legitimately has no key.
    assert "ansible_ssh_pass" in guard and "ansible_password" in guard
    assert "not ssh_key_pem and not _has_password" in guard
    # A NAMED key secret that resolved to nothing is a different, earlier failure --
    # otherwise the run silently uses a different credential than was chosen.
    assert "if secret_ssh_key_source and not secret_ssh_pem:" in svc
    # Both giving-up paths hand back anything already checked out: a Password Safe
    # request runs for 60 minutes, so a play that never started must not hold one.
    abandon = svc.split("async def _abandon(")[1].split("if secret_ssh_key_source")[0]
    assert "checkin_ps_request" in abandon and "set_failed" in abandon


def test_the_deploy_job_match_covers_both_recorded_addresses():
    """`(public or private) == ip` short-circuits to the public address, so a run aimed
    at a VM's PRIVATE ip never matched a deploy job that also recorded a public one --
    no build key found, silent fallback to the global key, `Permission denied`."""
    svc = _read("web_dashboard", "services", "ansible_local_run_service.py")
    assert 'ip in (meta.get("public_ip"), meta.get("private_ip"))' in svc
    assert '(meta.get("public_ip") or meta.get("private_ip")) ==' not in svc


def test_one_password_safe_request_id_is_recorded_once():
    """"Also use for sudo" checks the same account out twice; Password Safe returns the
    already-open request (ConflictOption=reuse) rather than opening a second one, so the
    same id arrives twice and would misreport how many requests the run opened."""
    creds = _read("web_dashboard", "services", "ansible_credentials.py")
    assert "dict.fromkeys(out.request_ids)" in creds


def test_the_build_form_reloads_the_account_list_when_the_host_changes():
    """Both ids are scoped to ONE managed system, so a key left over from another host
    would check out that host's credential and connect to this one."""
    page = _read("web_dashboard", "templates", "spire_lab", "index.html")
    assert "onHostChange()" in page
    assert 'x-model="form.host" @change="onHostChange()"' in page
    assert 'form.host = \'\'; onHostChange()' in page          # the cloud select too
    assert "/api/config-mgmt/managed-accounts" in page
    assert "/api/config-mgmt/secret-options" in page
    # The account key is cleared before the reload, never after.
    hc = page.split("async onHostChange()")[1].split("async ")[0]
    assert hc.index("resetCredential()") < hc.index("managed-accounts")


def test_the_build_form_has_one_shape_used_by_both_initialisers():
    """Two drifted initialisers is how a field ends up undefined on the SECOND build of
    a session -- `form` is rebuilt from scratch every time the modal opens."""
    page = _read("web_dashboard", "templates", "spire_lab", "index.html")
    literal = page.split("form: {")[1].split("},")[0]
    blank = page.split("blankForm(cloud) {")[1].split("},")[0]
    for key in ("secret_ssh_key_source", "managed_become_self", "login_user"):
        assert key in literal, f"{key} missing from the data object's form"
        assert key in blank, f"{key} missing from blankForm()"
    # The DEFINITION, not the @click in the markup above it.
    assert "this.blankForm(" in page.split("openBuild() {")[1].split("},")[0]


def test_the_two_credential_pickers_are_mutually_exclusive_in_the_form():
    """The server refuses both -- the API is the boundary -- but an operator should not
    be able to compose a request it will reject."""
    page = _read("web_dashboard", "templates", "spire_lab", "index.html")
    assert ':disabled="!!form.secret_ssh_key_source"' in page
    assert ':disabled="!!managedAccountKey"' in page
    # A DSS account has a key, not a password, so there is nothing to check out for sudo.
    assert "selectedAccountUsesSshKey" in page


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
