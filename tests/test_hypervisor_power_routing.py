"""The chain from a power button to the operation a hypervisor actually performs.

Two things are pinned here, and the second was added after the first proved insufficient.

**The button reaches the agent at all.** An agent-bound connection stores no host and no
credential — that is the whole design — so calling the service layer for it cannot work:
`hyperv_service._session()` raises "has no host configured", and if a host were somehow
present the dashboard has no route to that network anyway. `agent_power_job` turns the
button into an `agent_hypervisor` job instead, and `api/hyperv.py` was once the one
router that never called it. Its VMs listed fine (the page reads the synced cache) while
every Start and Stop went down the dead direct path — the worst shape a gap can have,
because the feature looks present.

**The verb means what the button says.** This is the part that checking verbs against
`WRITE_VERBS` cannot do. A page op maps to an agent verb, and the agent resolves that
verb *against the hypervisor kind*:

    proxmox   restart      -> /status/shutdown        a graceful shutdown
    vsphere   restart      -> ?action=reset           a HARD reset
    xcpng     restart      -> VM.clean_reboot         a reboot
    hyperv    power_reset  -> Restart-VM -Force       a restart (sibling runner)

So `restart` is not one operation, and a table shared across routers cannot be correct
for more than one kind. One was nonetheless copied verbatim into proxmox.py, vsphere.py
and xcpng.py, mapping every page's `shutdown` onto `restart`. Every value in it was a
real write verb, so every allowlist check passed, and all three of these shipped:

  * agent-bound vCenter **Shutdown** hard-reset the guest — the data loss the operator
    pressed Shutdown to avoid;
  * agent-bound XCP-ng **Shutdown** cleanly rebooted it, so it came back up;
  * Proxmox **Reboot** gracefully shut it down and left it off — the same fault mirrored.

And a quieter one from the same table, which defaulted an unmapped op to itself:
`normalize()` falls an unrecognised verb back to `inventory_sync`, so vSphere `suspend`
and XCP-ng `suspend`/`resume`/`pause`/`unpause` each ran a **discovery scan that
completed green**. The operator clicked Suspend; the job said success; nothing was
suspended.

`AGENT_OP` below is the fix for that blind spot: it states, per (kind, op), the operation
the hypervisor must actually receive, transcribed from each product's own API rather than
derived from the maps under test.

AST and source reads only — no imports of the routers (FastAPI, hypervisor SDKs) or the
agent (requests, yaml, cryptography), so this file cannot skip on the machine where
someone edits one of these maps. Runs under pytest, or standalone:
    python tests/test_hypervisor_power_routing.py
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API = os.path.join(_ROOT, "web_dashboard", "api")
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")
_META = os.path.join(_ROOT, "web_dashboard", "services", "agent_hypervisor_meta.py")
_DEPS = os.path.join(_API, "hypervisor_deps.py")
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")
_SIBLING = os.path.join(_ROOT, "runners", "hypervisor", "run.py")

# agent_hypervisor_meta is stdlib-only by design, so it is the one module that can simply
# be loaded. Everything else here is read as source.
_spec = importlib.util.spec_from_file_location("agent_hypervisor_meta", _META)
ahm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ahm)

# The routers whose power buttons must reach an agent-bound connection. `nutanix` is
# deliberately NOT here: the agent refuses Nutanix power verbs (a v3 power change is a
# full spec PUT, not an action), so routing them to it would enqueue jobs that can only
# fail. test_nutanix_power_is_still_unimplemented_in_the_agent is the other half of that
# statement — if the agent ever grows the implementation, it fails and points here.
_AGENT_ROUTED = ("hyperv", "proxmox", "vsphere", "xcpng")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _tree(path: str) -> ast.Module:
    return ast.parse(_read(path), filename=path)


def _router(kind: str) -> str:
    return os.path.join(_API, f"{kind}.py")


def _function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"no function {name!r}")


def _calls(node) -> set:
    """Every function name called anywhere inside ``node``."""
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                names.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                names.add(child.func.attr)
    return names


def _literal(path: str, name: str):
    """A module-level literal (dict, tuple), read without importing the module."""
    for node in _tree(path).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {os.path.basename(path)}")


def _vsphere_actions() -> dict:
    """The verb -> ``?action=`` map inside the agent's ``_power_vsphere``.

    A dict literal local to the function rather than a module constant, so there is
    nothing to import even in principle. Reading it here is what lets this test pin the
    vSphere link of the chain — the one that hard-reset a VM.
    """
    for node in ast.walk(_tree(_AGENT)):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_power_vsphere"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Dict):
                continue
            if not all(isinstance(k, ast.Constant) for k in inner.keys):
                continue
            if "power_on" in [k.value for k in inner.keys]:
                return ast.literal_eval(inner)
    raise AssertionError("the verb -> action map in _power_vsphere was not found")


def _registered_ops(kind: str) -> set:
    """Every page op the router exposes as a /power/… route."""
    ops = set(re.findall(r'_power_endpoint\(\s*"([a-z_]+)"', _read(_router(kind))))
    assert ops, f"{kind}: no _power_endpoint(...) routes found — was it renamed?"
    return ops


def _agent_operation(kind: str, verb: str):
    """What the agent (or the sibling runner) would actually issue on `kind` for `verb`."""
    if kind == "vsphere":
        return _vsphere_actions().get(verb)
    if kind in ("hyperv", "esxi"):
        return _literal(_SIBLING, "_PS_POWER").get(verb)
    return (_literal(_AGENT, "_POWER").get(verb) or {}).get(kind)


# ── The button reaches the agent ──────────────────────────────────────────────

def test_every_agent_routed_power_endpoint_calls_agent_power_job():
    """The original regression: api/hyperv.py imported conn_or_error and conn_in_task but
    not agent_power_job, so an agent-bound Hyper-V host had power buttons that could only
    fail."""
    missing = []
    for kind in _AGENT_ROUTED:
        handler = _function(_tree(_router(kind)), "_power_endpoint")
        if "agent_power_job" not in _calls(handler):
            missing.append(f"api/{kind}.py::_power_endpoint")
    assert not missing, ("power endpoint never offers the connection to the agent:\n  "
                         + "\n  ".join(missing))


def test_the_agent_branch_comes_before_the_direct_call():
    """Order, not just presence: `create_job` for the direct path must not run first, or
    an agent-bound connection gets a local job row as well as (or instead of) the agent
    one."""
    for kind in _AGENT_ROUTED:
        body = ast.unparse(_function(_tree(_router(kind)), "_power_endpoint"))
        assert body.index("agent_power_job") < body.index("create_job"), kind


# ── The promise each button makes ─────────────────────────────────────────────

# A promise the five allowlisted verbs cannot keep. The op must therefore be refused —
# and the comment beside each one records the operation it *would* need, which is the
# specification a future `shutdown` verb has to be built and checked against.
UNEXPRESSIBLE = "«no verb in the allowlist expresses this»"

# (kind, page op) -> the operation the hypervisor must actually receive.
#
# Every op the four routers expose is listed, refused ones included, because the promise
# a button makes does not depend on whether we can currently keep it — and listing only
# the mapped ones is what would let this table go on agreeing with a mapping that lies.
#
# Transcribed by hand from each product's own API, not derived from the maps under test:
# a derived expectation asserts only that the code equals itself, which is exactly how
# `shutdown -> restart -> reset` shipped and survived a review that did check every verb
# against WRITE_VERBS. If an entry here looks like it needs changing, the question to
# settle first is which of the two — the button's label or the operation behind it — is
# now lying to the operator.
AGENT_OP = {
    ("proxmox", "start"): "start",
    ("proxmox", "stop"): "stop",                 # /status/stop, the force-stop
    ("proxmox", "shutdown"): "shutdown",         # /status/shutdown, graceful
    ("proxmox", "reboot"): UNEXPRESSIBLE,        # needs /status/reboot

    ("vsphere", "start"): "start",
    ("vsphere", "stop"): "stop",                 # ?action=stop, a hard power off
    ("vsphere", "reset"): "reset",
    ("vsphere", "shutdown"): UNEXPRESSIBLE,      # needs /guest/power?action=shutdown —
                                                 # a different endpoint, and VMware Tools
    ("vsphere", "suspend"): UNEXPRESSIBLE,       # needs ?action=suspend

    ("xcpng", "start"): "VM.start",
    ("xcpng", "stop"): "VM.hard_shutdown",
    ("xcpng", "reboot"): "VM.clean_reboot",
    ("xcpng", "hard_reboot"): "VM.hard_reboot",
    ("xcpng", "shutdown"): UNEXPRESSIBLE,        # needs VM.clean_shutdown
    ("xcpng", "suspend"): UNEXPRESSIBLE,         # needs VM.suspend
    ("xcpng", "resume"): UNEXPRESSIBLE,          # needs VM.resume
    ("xcpng", "pause"): UNEXPRESSIBLE,           # needs VM.pause
    ("xcpng", "unpause"): UNEXPRESSIBLE,         # needs VM.unpause

    # Hyper-V runs through the sibling runner, so these are PowerShell, not URLs.
    # `-Force` on Restart-VM only suppresses the confirmation prompt — which a
    # non-interactive WinRM session cannot answer — so it is the honest Restart here.
    ("hyperv", "start"): "Start-VM -Id '{vm}'",
    ("hyperv", "stop"): "Stop-VM -Id '{vm}' -TurnOff -Force",
    ("hyperv", "restart"): "Restart-VM -Id '{vm}' -Force",
    ("hyperv", "shutdown"): UNEXPRESSIBLE,       # needs Stop-VM with no -TurnOff
    ("hyperv", "pause"): UNEXPRESSIBLE,          # needs Suspend-VM
    ("hyperv", "resume"): UNEXPRESSIBLE,         # needs Resume-VM
    ("hyperv", "save"): UNEXPRESSIBLE,           # needs Save-VM
}


def test_every_op_either_reaches_its_promise_or_is_refused():
    """The assertion the shipped table failed, on every button the pages offer."""
    for (kind, op), expected in sorted(AGENT_OP.items()):
        verb = ahm.agent_verb(kind, op)
        if expected is UNEXPRESSIBLE:
            assert verb is None, (
                f"{kind} '{op}' is mapped to verb {verb!r}, which resolves to "
                f"{_agent_operation(kind, verb)!r} — no verb in the allowlist does what "
                f"this button says, so it has to be refused, not approximated onto a "
                f"neighbouring one")
            continue
        assert verb is not None, (
            f"{kind}/{op} is no longer mapped, but {expected!r} is still what its "
            f"button promises")
        got = _agent_operation(kind, verb)
        assert got == expected, (
            f"{kind} '{op}' maps to verb {verb!r}, which resolves to {got!r} — the "
            f"button promises {expected!r}")


def test_every_button_on_every_page_has_a_stated_promise():
    """Both directions, so neither a new route nor a new mapping can arrive without
    someone writing down what it is supposed to do."""
    for kind in _AGENT_ROUTED:
        for op in sorted(_registered_ops(kind)):
            assert (kind, op) in AGENT_OP, (
                f"{kind}.py exposes /power/{op} with no AGENT_OP entry saying what "
                f"operation it promises — state it, then check the agent agrees")
    for kind, table in sorted(ahm.PAGE_OPS.items()):
        for op in sorted(table):
            assert (kind, op) in AGENT_OP, (
                f"{kind}/{op} was added to PAGE_OPS without an AGENT_OP entry")
            assert op in _registered_ops(kind), (
                f"{kind}/{op} is mapped but no /power/{op} route exposes it")


# ── The inverted mappings, named ──────────────────────────────────────────────

def test_shutdown_is_not_mapped_onto_a_reset_or_a_reboot():
    """vSphere is the data-loss one: `reset` is a hard reset of a running guest."""
    assert ahm.agent_verb("vsphere", "shutdown") is None, (
        "vSphere has no graceful-shutdown verb — vCenter's is "
        "/guest/power?action=shutdown, not a power action — so mapping this button to "
        "anything available hard-resets the guest")
    assert ahm.agent_verb("xcpng", "shutdown") is None, (
        "XCP-ng's `restart` is VM.clean_reboot, which brings the VM back up")
    assert ahm.agent_verb("hyperv", "shutdown") is None, (
        "Hyper-V's only stop script is Stop-VM -TurnOff -Force, a hard power cut")
    # Proxmox is the one kind where `restart` really is a graceful shutdown, so its
    # Shutdown button is the single mapping that was right and must stay.
    assert ahm.agent_verb("proxmox", "shutdown") == "restart"


def test_reboot_is_not_mapped_onto_a_shutdown():
    """The same fault mirrored: Proxmox Reboot left the VM off."""
    assert ahm.agent_verb("proxmox", "reboot") is None, (
        "the agent's `restart` is Proxmox's /status/shutdown, so this reboots nothing "
        "and leaves the VM off; /status/reboot needs a verb of its own")
    # XCP-ng's Reboot is honest, because there `restart` is a clean reboot.
    assert ahm.agent_verb("xcpng", "reboot") == "restart"


# ── The silent scan ───────────────────────────────────────────────────────────

def test_no_page_op_can_degrade_into_an_inventory_scan():
    """The failure that reported success.

    Every op each router exposes is either mapped to a verb that survives `normalize()`,
    or unmapped — and unmapped has to mean refused. What it must never mean is passed
    through, because `normalize()` turns an unknown verb into `inventory_sync` and the
    operator gets a green discovery scan for a power click.
    """
    for kind in _AGENT_ROUTED:
        for op in sorted(_registered_ops(kind)):
            verb = ahm.agent_verb(kind, op)
            if verb is None:
                continue  # refused; see the 501 tests below
            assert verb in ahm.WRITE_VERBS, (
                f"{kind}/{op} maps to {verb!r}, which is not in WRITE_VERBS, so "
                f"normalize() would silently turn it into inventory_sync")
            assert ahm.normalize({"verb": verb})["verb"] == verb, (
                f"{kind}/{op} maps to {verb!r}, which normalize() rewrites to "
                f"{ahm.normalize({'verb': verb})['verb']!r}")


def test_the_ops_with_no_verb_are_the_ones_we_expect():
    """Names the refusals, so widening or narrowing the set is a deliberate diff."""
    refused = {kind: {op for op in _registered_ops(kind)
                      if ahm.agent_verb(kind, op) is None}
               for kind in _AGENT_ROUTED}
    assert refused == {
        "proxmox": {"reboot"},
        "vsphere": {"shutdown", "suspend"},
        "xcpng": {"shutdown", "suspend", "resume", "pause", "unpause"},
        "hyperv": {"shutdown", "pause", "resume", "save"},
    }, refused


def test_an_unmapped_verb_would_become_an_inventory_sync():
    """The behaviour the guard exists for, stated once so the reason survives.

    `shutdown` is a plausible thing for a router to pass through — every one of these
    pages has a Shutdown button — and this is what it would do.
    """
    assert ahm.normalize({"verb": "shutdown"})["verb"] == "inventory_sync"
    assert ahm.normalize({"verb": "suspend"})["verb"] == "inventory_sync"
    assert "shutdown" not in ahm.VALID_VERBS


# ── The refusal ───────────────────────────────────────────────────────────────

def test_agent_power_job_refuses_a_verb_outside_the_allowlist():
    """The refusal lives in the shared helper, so no router can reintroduce the
    pass-through by forgetting to check."""
    body = ast.unparse(_function(_tree(_DEPS), "agent_power_job"))
    assert "WRITE_VERBS" in body, "agent_power_job does not check the verb at all"
    assert body.index("WRITE_VERBS") < body.index("normalize"), \
        "the verb must be refused BEFORE normalize() can fall it back to inventory_sync"


def test_the_refusal_names_the_wrong_substitution_and_the_working_buttons():
    for kind, op in (("vsphere", "shutdown"), ("xcpng", "shutdown"),
                     ("proxmox", "reboot")):
        reason = ahm.no_verb_reason(kind, op)
        assert op in reason and kind in reason, reason
        # The substitution that would have been wrong is the whole diagnosis: without it
        # the operator's next move is to file the refusal as a bug.
        assert "restart" in reason, reason
        for available in ahm.PAGE_OPS[kind]:
            assert available in reason, f"{kind}/{op}: does not offer {available}: {reason}"
        assert f"Available here: {op}" not in reason, reason


def test_an_op_with_no_stated_reason_still_gets_a_usable_message():
    reason = ahm.no_verb_reason("xcpng", "pause")
    assert "pause" in reason and "no verb" in reason, reason
    assert "start" in reason, reason


def test_the_refusal_precedes_the_job_it_would_otherwise_create():
    """A refused op must leave nothing behind on /jobs.

    A job row created first and failed afterwards reads as an agent or permissions fault,
    which is the wrong diagnosis and the wrong place to look — and the job page renders
    only `error_message`, so there would be no metadata to correct it with.
    """
    fn = _function(_tree(_DEPS), "agent_power_job")

    def _first(predicate):
        return min((n.lineno for n in ast.walk(fn) if predicate(n)), default=None)

    resolved = _first(lambda n: isinstance(n, ast.Call)
                      and getattr(n.func, "attr", "") == "agent_verb")
    refused = _first(lambda n: isinstance(n, ast.Raise) and "501" in ast.unparse(n))
    created = _first(lambda n: isinstance(n, ast.Call)
                     and getattr(n.func, "attr", "") == "create_job")
    assert resolved, "agent_power_job no longer resolves the op through agent_verb"
    assert refused, "agent_power_job no longer refuses an unmappable op with a 501"
    assert created, "agent_power_job no longer creates a job — did create_job move?"
    assert resolved < refused < created, (
        f"the op must be resolved (line {resolved}) and refused (line {refused}) before "
        f"the job is created (line {created})")


def test_the_refusal_cannot_reach_a_connection_the_dashboard_dials_directly():
    """The op-to-verb translation must sit *after* the agent-bound early return.

    Every one of these ops works on a direct connection — the routers' background-task
    path implements all of them. Refusing before the early return would make this fix a
    far larger regression than the bug it repairs.
    """
    fn = _function(_tree(_DEPS), "agent_power_job")
    early = next((n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Return) and ast.unparse(n) == "return None"), None)
    resolved = min((n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                    and getattr(n.func, "attr", "") == "agent_verb"), default=None)
    assert early, "the `return None` for a non-agent connection is gone"
    assert resolved and early < resolved, (
        f"agent_verb is called at line {resolved}, before the non-agent early return at "
        f"line {early} — a direct connection would start getting 501s")


# ── One table, not one per router ─────────────────────────────────────────────

def test_the_routers_do_not_carry_their_own_op_to_verb_map():
    """The drift itself, since copying is what made one wrong table into three.

    Per-kind is the property that matters: a table inside `vsphere.py` cannot be shared,
    but it also cannot be reviewed beside its siblings — and those three were identical
    when only one of them could be right.
    """
    for kind in _AGENT_ROUTED:
        src = _read(_router(kind))
        assert "_AGENT_VERBS" not in src, (
            f"{kind}.py has its own verb table again — the map is per kind and belongs "
            f"in agent_hypervisor_meta.PAGE_OPS, where all four can be read together")
        # `restart` is both an agent verb and one of Hyper-V's own page ops, so a
        # router may legitimately name it as an op — exclude a router's own ops rather
        # than weakening the check for everyone.
        for verb in set(ahm.WRITE_VERBS) - _registered_ops(kind):
            assert f'"{verb}"' not in src and f"'{verb}'" not in src, (
                f"{kind}.py names the agent verb {verb!r} directly; routers pass the "
                f"page op and let agent_power_job translate it")


def test_the_routers_pass_the_page_op_rather_than_a_verb():
    for kind in _AGENT_ROUTED:
        assert re.search(r"agent_power_job\(\s*\n?\s*db,\s*conn,\s*op=op\b",
                         _read(_router(kind))), (
            f"{kind}.py must call agent_power_job(db, conn, op=op, …) so the per-kind "
            f"translation happens in one place")


def test_every_router_that_brokers_power_has_a_table_of_its_own():
    """Discovered from the api package, not from `_AGENT_ROUTED` above.

    `agent_verb` fails closed, so a fifth router wired to `agent_power_job` with no
    PAGE_OPS entry for its kind would 501 *every* button on that page — fail-closed, but
    a total loss of function that nothing else here would notice, because `_AGENT_ROUTED`
    would not know the router existed.
    """
    brokered = set()
    for name in sorted(os.listdir(_API)):
        if not name.endswith(".py"):
            continue
        src = _read(os.path.join(_API, name))
        if "agent_power_job(" in src and "def agent_power_job" not in src:
            brokered.add(name[:-3])
    assert brokered, "no router calls agent_power_job — was it renamed?"
    assert brokered == set(_AGENT_ROUTED), (
        f"the set of routers brokering agent power changed: {sorted(brokered)}")
    missing = brokered - set(ahm.PAGE_OPS)
    assert not missing, (
        f"{sorted(missing)} broker power through the agent but have no PAGE_OPS entry, "
        f"so every button on those pages would be refused with a 501")
    assert set(ahm.PAGE_OPS) <= brokered, (
        f"PAGE_OPS has entries for {sorted(set(ahm.PAGE_OPS) - brokered)}, which broker "
        f"no agent power ops")


# ── The page greys what the router refuses ────────────────────────────────────

def test_each_page_greys_exactly_the_ops_its_router_refuses():
    """Binding-to-model drift, in the direction that hurts: a page's `agentOps` decides
    which buttons are clickable on an agent-bound connection, so a verb added to
    PAGE_OPS and not to the page stays dead, and one removed leaves a button that 501s.
    """
    for kind in _AGENT_ROUTED:
        markup = _read(os.path.join(_TEMPLATES, kind, "index.html"))
        match = re.search(r"agentOps:\s*\[([^\]]*)\]", markup)
        assert match, f"the {kind} page no longer declares agentOps"
        declared = {v.strip().strip("'\"") for v in match.group(1).split(",") if v.strip()}
        assert declared == set(ahm.PAGE_OPS[kind]), (
            f"{kind} page offers {sorted(declared)}, PAGE_OPS maps "
            f"{sorted(ahm.PAGE_OPS[kind])}")
        assert "viaAgent: {{" in markup, (
            f"{kind}: viaAgent must come from the server, not be guessed")


def test_each_page_defines_the_helpers_its_bindings_call():
    """`canOp` and `opTitle` are referenced from markup, where a missing definition is a
    silent Alpine failure rather than an error."""
    for kind in _AGENT_ROUTED:
        markup = _read(os.path.join(_TEMPLATES, kind, "index.html"))
        for helper in ("canOp", "opTitle"):
            assert re.search(r"\n\s*" + helper + r"\(op\)\s*\{", markup), (
                f"{kind} page calls {helper}() from its markup but never defines it")
        # Methods, not getters: tests/template_helpers_check.js extracts by the
        # `name(args) {` shape and cannot see a getter.
        assert "get canOp" not in markup, f"{kind}: canOp must be a method, not a getter"


def test_the_refused_buttons_are_actually_wired_to_canop():
    """A helper nothing calls greys nothing. Each refused op with a button must consult
    `canOp`, or the operator clicks it and reads a 501 instead of seeing it disabled."""
    for kind in _AGENT_ROUTED:
        markup = _read(os.path.join(_TEMPLATES, kind, "index.html"))
        for op in sorted(set(_registered_ops(kind)) - set(ahm.PAGE_OPS[kind])):
            if f"powerOp(vm, '{op}')" not in markup:
                continue  # the route exists but this page has no button for it
            assert f"canOp('{op}')" in markup, (
                f"{kind} page has a {op!r} button that the router refuses for an "
                f"agent-bound connection, but it is not gated on canOp('{op}')")


# ── The sibling runner shares the allowlist, so it shares the gap ─────────────

def test_the_sibling_runner_implements_exactly_the_shared_verbs():
    """Hyper-V and bare ESXi go through `runners/hypervisor/run.py`, not the agent.

    It keeps its own verb list, so it is another copy of the same contract and drifts the
    same way. `shutdown` is absent from both sides today; whichever change adds it has to
    add it here too, or an agent-bound Hyper-V connection gets a refusal from a place the
    operator has no reason to look.
    """
    runner_verbs = set(_literal(_SIBLING, "VALID_VERBS"))
    ps_power = set(_literal(_SIBLING, "_PS_POWER"))
    # Snapshot is agent-only (it needs per-kind API support the sibling has no path to),
    # so the runner's list is the rest of the shared allowlist plus the read verb.
    expected = (set(ahm.WRITE_VERBS) - {"snapshot"}) | set(ahm.READ_VERBS)
    assert runner_verbs == expected, (
        f"the sibling runner accepts {sorted(runner_verbs)} but the dashboard's "
        f"allowlist is {sorted(expected)}")
    assert ps_power == expected - set(ahm.READ_VERBS), (
        f"_PS_POWER implements {sorted(ps_power)}, which is not the runner's own "
        f"power-verb list {sorted(expected - set(ahm.READ_VERBS))}")


def test_hyperv_maps_only_the_ops_the_sibling_runner_implements():
    """Hyper-V power runs in the sibling container over WinRM, and its script table is
    the real limit on what the page can offer."""
    implemented = set(_literal(_SIBLING, "_PS_POWER"))
    mapped = set(ahm.PAGE_OPS["hyperv"].values())
    assert mapped <= implemented, f"{mapped - implemented} has no Hyper-V script"


# ── The exclusion is honest ───────────────────────────────────────────────────

def test_nutanix_power_is_still_unimplemented_in_the_agent():
    """Why api/nutanix.py has no agent branch. If this starts failing, the agent has
    grown Nutanix power and the router should route to it."""
    body = ast.unparse(_function(_tree(_AGENT), "_power_nutanix"))
    assert "PolicyRefusal" in body and "not implemented" in body, \
        "the agent implements Nutanix power now — api/nutanix.py should use it"
    assert "agent_power_job" not in _calls(
        _function(_tree(_router("nutanix")), "_power_endpoint"))
    assert "nutanix" not in ahm.PAGE_OPS, \
        "a PAGE_OPS entry for nutanix implies a routing that does not exist"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
