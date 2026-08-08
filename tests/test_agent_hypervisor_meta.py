"""The closed verb allowlist for agent-brokered hypervisor operations.

The design note this guards says the shape is "a closed verb allowlist plus scheduled
inventory sync — not a generic proxy, which is remote code execution with extra steps."
Everything here exists to keep that true after the module has been edited by someone who
never read the note.

The single most valuable assertion is
:func:`test_the_agent_never_reads_a_host_from_the_job_payload`: it AST-walks the agent's
handler and proves it cannot be pointed at an arbitrary endpoint, because there is no
field through which to ask.

Loaded by file path, stdlib only. Runs under pytest, or standalone:
    python tests/test_agent_hypervisor_meta.py
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_META = os.path.join(_ROOT, "web_dashboard", "services", "agent_hypervisor_meta.py")
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")

_spec = importlib.util.spec_from_file_location("agent_hypervisor_meta", _META)
ahm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ahm)


# ── The allowlist is closed ────────────────────────────────────────────────────

def test_metadata_keys_are_a_closed_set():
    meta = ahm.normalize({k: "x" for k in ahm.HYPERVISOR_META_KEYS} | {"host": "evil"})
    assert "host" not in ahm.hypervisor_kwargs(meta)
    assert set(ahm.hypervisor_kwargs(meta)) == set(ahm.HYPERVISOR_META_KEYS)


def test_no_field_can_carry_executable_content():
    """The same regex agent_job_meta's test uses, plus the display-name family.

    A display name is harmless in itself and there is still no reason for one to
    travel: the dashboard already knows what it called the connection, and a name on
    the wire is a free-form string looking for a use.
    """
    banned = re.compile(
        r"command|cmd|script|shell|exec|playbook|manifest|url|uri|path|file|image|"
        r"entrypoint|args|env|name|label|description|note", re.I)
    hits = [k for k in ahm.HYPERVISOR_META_KEYS if banned.search(k)]
    assert not hits, f"{hits} could carry executable content or free-form text"


def test_no_field_is_credential_shaped():
    banned = re.compile(r"pass|secret|token|credential|user|auth|key", re.I)
    hits = [k for k in ahm.HYPERVISOR_META_KEYS if banned.search(k)]
    assert not hits, f"{hits} looks credential-shaped — the agent holds the credential"


def test_there_is_no_host_or_port_field():
    """The heart of it. A job names a CONNECTION; the agent resolves the endpoint from
    its own file. A host field here would turn the allowlist into a proxy."""
    for banned in ("host", "hostname", "address", "ip", "port", "endpoint", "server"):
        assert banned not in ahm.HYPERVISOR_META_KEYS


# ── Phase gating ───────────────────────────────────────────────────────────────

def test_read_and_write_verbs_are_disjoint_and_complete():
    assert not set(ahm.READ_VERBS) & set(ahm.WRITE_VERBS)
    assert set(ahm.VALID_VERBS) == set(ahm.READ_VERBS) | set(ahm.WRITE_VERBS)


def test_the_verbs_needing_operator_supplied_input_are_absent():
    """clone/deploy/delete each need a name, a size or a network — a payload shape
    indistinguishable from a config file, and a config file is one step from a script."""
    for verb in ("clone", "deploy", "delete", "console", "exec", "migrate"):
        assert verb not in ahm.VALID_VERBS


def test_snapshot_is_allowed_but_carries_no_name_field():
    """Snapshot was held back precisely because a created thing needs a name, and a
    name is a free-form string. It is allowed now only because the name is DERIVED —
    so the absence of the field is the whole safety property, not an oversight.
    """
    assert "snapshot" in ahm.VALID_VERBS
    for banned in ("snapshot_name", "name", "label", "description"):
        assert banned not in ahm.HYPERVISOR_META_KEYS


def test_the_snapshot_name_is_derived_from_the_job_id():
    name = ahm.snapshot_name("a1b2c3d4-0000-1111-2222-333344445555")
    assert name.startswith(ahm.SNAPSHOT_NAME_PREFIX)
    assert "a1b2c3d4" in name
    # Traceable back to the job row that made it, which an operator-typed name is not.
    assert ahm.snapshot_name("x") != ahm.snapshot_name("y")


def test_the_snapshot_name_cannot_carry_anything_hostile():
    """The job id is ours, but this is the one string the agent hands to a hypervisor,
    so it is constrained rather than trusted."""
    for hostile in ("'; DROP TABLE vms; --", "../../etc/passwd", "a b\nc",
                    "$(whoami)", "<script>"):
        name = ahm.snapshot_name(hostile)
        assert re.fullmatch(r"dash-[A-Za-z0-9-]*", name), name
    assert len(ahm.snapshot_name("z" * 500)) <= len(ahm.SNAPSHOT_NAME_PREFIX) + 32


def test_the_agent_derives_the_snapshot_name_rather_than_reading_it():
    """The dashboard must not be able to choose it. If the agent ever read a name out
    of the payload, the field would have to exist — and then operator text reaches a
    hypervisor after all."""
    body = ast.unparse(_agent_function("_run_verb"))
    assert "_snapshot_name(job_id)" in body
    assert "snapshot_name" not in str(ahm.HYPERVISOR_META_KEYS)


def test_power_off_and_reset_are_separate_verbs_not_a_force_flag():
    # A boolean flag on a destructive verb gets defaulted wrong exactly once.
    assert "power_off" in ahm.VALID_VERBS and "power_reset" in ahm.VALID_VERBS
    assert "force" not in ahm.HYPERVISOR_META_KEYS


# ── Normalisation ──────────────────────────────────────────────────────────────

def test_an_unknown_verb_falls_back_rather_than_raising():
    assert ahm.normalize({"verb": "rm -rf"})["verb"] == "inventory_sync"
    assert ahm.normalize({"kind": "virtualbox"})["kind"] == "vsphere"
    assert ahm.normalize({"target_type": "wat"})["target_type"] == "vm"


def test_a_malformed_target_id_is_emptied_not_repaired():
    """A mangled hypervisor id names a DIFFERENT VM, so it is refused rather than
    sanitised — unlike an enum, where falling back is harmless."""
    assert ahm.normalize({"target_id": "vm-42"})["target_id"] == "vm-42"
    assert ahm.normalize({"target_id": "vm 42; rm -rf /"})["target_id"] == ""
    assert ahm.normalize({"target_id": "../../etc/passwd"})["target_id"] == ""
    assert ahm.normalize({"target_id": "x" * 200})["target_id"] == ""


def test_a_cursor_is_charset_and_length_bounded():
    assert ahm.normalize({"cursor": "500"})["cursor"] == "500"
    assert ahm.normalize({"cursor": "a" * 500})["cursor"] == ""
    assert ahm.normalize({"cursor": "<script>"})["cursor"] == ""


def test_page_size_and_timeout_are_clamped_both_ways():
    assert ahm.normalize({"page_size": 10 ** 6})["page_size"] == ahm.MAX_PAGE_SIZE
    assert ahm.normalize({"page_size": 0})["page_size"] == 1
    assert ahm.normalize({"timeout_s": 10 ** 6})["timeout_s"] == ahm.MAX_TIMEOUT_S
    assert ahm.normalize({"page_size": "nonsense"})["page_size"] == 250


def test_a_round_trip_preserves_every_declared_field():
    meta = ahm.normalize({"verb": "power_off", "connection_ref": "dc1-vcenter",
                          "connection_id": "abc-123", "kind": "proxmox",
                          "target_id": "101", "target_scope": "pve1",
                          "target_type": "qemu", "page_size": 10,
                          "cursor": "x1", "timeout_s": 30})
    out = ahm.hypervisor_kwargs(meta)
    for key in ahm.HYPERVISOR_META_KEYS:
        assert out[key] == meta[key], key


# ── The result projection ──────────────────────────────────────────────────────

def test_the_sync_projection_is_closed_and_sanitised():
    page = ahm.sync_page({"vms": [{"vm_id": "1", "name": "web\x1b[31m01",
                                   "power_state": "on", "vcpus": "4",
                                   "mem_mib": 8192, "password": "hunter2",
                                   "ip_addresses": ["10.0.0.1"], "extra": "x"}],
                          "next_cursor": "250", "complete": False, "scanned": 1})
    vm = page["vms"][0]
    assert set(vm) <= set(ahm.VM_KEYS)
    assert "hunter2" not in repr(page)
    assert "\x1b" not in vm["name"]
    assert vm["vcpus"] == 4
    assert page["next_cursor"] == "250" and page["complete"] is False


def test_a_hostile_cursor_in_a_result_is_dropped():
    # The cursor comes back from the agent and rides into the next job's metadata.
    page = ahm.sync_page({"vms": [], "next_cursor": "'; DROP TABLE jobs; --"})
    assert page["next_cursor"] == ""


def test_the_page_is_bounded():
    page = ahm.sync_page({"vms": [{"vm_id": str(i)} for i in range(5000)]})
    assert len(page["vms"]) == ahm.MAX_VMS_PER_PAGE


# ── The agent handler cannot be pointed anywhere ───────────────────────────────

def _agent_function(name: str):
    with open(_AGENT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _payload_readers():
    """Every function that could read the job payload, not just the entry point.

    Walking only ``run_hypervisor`` was enough when it did the work itself. It now
    delegates to ``_run_verb`` and the per-product implementations, so a test anchored
    on the entry point alone would still pass while a host field was read two frames
    down. Discovered exactly that way — the refactor silently narrowed this test.
    """
    with open(_AGENT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = re.compile(r"^(run_hypervisor|_run_verb|_sync_|_power_|_snapshot_)")
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and wanted.match(n.name)]


def test_the_agent_never_reads_a_host_from_the_job_payload():
    """The credential decision, in executable form.

    Nothing on the hypervisor path may read a host, a port or a credential out of the
    payload. If anything did, the dashboard could point an agent at an arbitrary
    endpoint — which is a proxy, and a proxy is remote code execution with extra steps.
    """
    nodes = _payload_readers()
    assert len(nodes) >= 10, f"the sweep only found {[n.name for n in nodes]}"
    banned = {"host", "hostname", "port", "url", "uri", "endpoint", "server",
              "username", "user", "password", "secret", "token", "command"}
    read = set()
    for node in nodes:
        read |= _payload_keys(node)
    leaked = read & banned
    assert not leaked, f"the hypervisor path reads {leaked} from the job payload"
    assert read <= set(ahm.HYPERVISOR_META_KEYS), (
        f"undeclared payload keys read: {read - set(ahm.HYPERVISOR_META_KEYS)}")


def _payload_keys(node):
    read = set()
    for child in ast.walk(node):
        # payload.get("x")
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == "get" and child.args
                and isinstance(child.args[0], ast.Constant)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "payload"):
            read.add(child.args[0].value)
        # payload["x"]
        if (isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name)
                and child.value.id == "payload"
                and isinstance(child.slice, ast.Constant)):
            read.add(child.slice.value)
    return read


def test_the_agent_dispatches_verbs_from_a_closed_dict():
    """Same argument as the handler table one level up: `getattr(self, verb)` would
    make every method on the module reachable by name."""
    with open(_AGENT, encoding="utf-8") as fh:
        source = fh.read()
    assert "_SYNC = {" in source and "_POWER_IMPL = {" in source
    node = _agent_function("run_hypervisor")
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            assert child.func.id != "getattr", "verb dispatch must not use getattr"


def test_the_agent_checks_policy_before_touching_a_connection():
    """Order matters: the policy grant is checked before the connections file is even
    read, so a refused verb never causes a credential to be loaded."""
    node = _agent_function("run_hypervisor")
    body = ast.unparse(node)
    assert body.index("check_verb") < body.index("HypervisorConnections.load")


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
