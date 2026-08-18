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

The three inverted Shutdowns and the inverted Reboot were then repaired by adding the
verbs that express them — `shutdown` and `reboot` — rather than by re-pointing `restart`,
which a deployed agent already implements and a deployed policy.yaml already grants.
That is the second thing this file guards, and the reason it reaches into all three
maps at once: a verb the dashboard grants but an agent does not implement is the silent
scan again, so the allowlist here and the per-kind maps in `runners/` have to move in
the same commit.

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
    """The verb -> URL sub-path map inside the agent's ``_power_vsphere``.

    A dict literal local to the function rather than a module constant, so there is
    nothing to import even in principle. Reading it here is what lets this test pin the
    vSphere link of the chain — the one that hard-reset a VM.

    The values are sub-paths (``power?action=stop``, ``guest/power?action=shutdown``)
    rather than bare actions, because vCenter's graceful ops live on a different
    endpoint from its power ops and a bare action cannot say which one was used.
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

# A promise no allowlisted verb can keep. The op must therefore be refused — and the
# comment beside each one records the operation it *would* need, which is the
# specification whichever change adds that verb has to be built and checked against.
# That is not hypothetical: `shutdown` and `reboot` were both UNEXPRESSIBLE here, and
# the comments beside them are what the verbs that replaced them were written from.
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
    ("proxmox", "reboot"): "reboot",             # /status/reboot — a real endpoint, and
                                                 # NOT what `restart` resolves to here

    # vSphere entries are the sub-path under /api/vcenter/vm/{vm}, not the bare action,
    # because the graceful pair is on a different endpoint. `power` is the virtual power
    # button; `guest/power` asks the guest OS via VMware Tools. An entry of just
    # "shutdown" would have been satisfied by `power?action=shutdown`, which vCenter
    # does not accept — the table has to be able to tell the two endpoints apart.
    ("vsphere", "start"): "power?action=start",
    ("vsphere", "stop"): "power?action=stop",    # a hard power off, which is the op
    ("vsphere", "reset"): "power?action=reset",
    ("vsphere", "shutdown"): "guest/power?action=shutdown",
    ("vsphere", "suspend"): UNEXPRESSIBLE,       # needs power?action=suspend

    ("xcpng", "start"): "VM.start",
    ("xcpng", "stop"): "VM.hard_shutdown",
    ("xcpng", "shutdown"): "VM.clean_shutdown",
    ("xcpng", "reboot"): "VM.clean_reboot",
    ("xcpng", "hard_reboot"): "VM.hard_reboot",
    ("xcpng", "suspend"): UNEXPRESSIBLE,         # needs VM.suspend
    ("xcpng", "resume"): UNEXPRESSIBLE,          # needs VM.resume
    ("xcpng", "pause"): UNEXPRESSIBLE,           # needs VM.pause
    ("xcpng", "unpause"): UNEXPRESSIBLE,         # needs VM.unpause

    # Hyper-V runs through the sibling runner, so these are PowerShell, not URLs, and
    # the switches carry the meaning. Straight from Microsoft's cmdlet reference:
    #   Stop-VM, bare      "shuts down … through the guest operating system"
    #   Stop-VM -TurnOff   "equivalent to disconnecting the power"
    #   Stop-VM -Force     shuts down "regardless of any unsaved application data",
    #                      forcing it after five minutes — a DATA promise, not a prompt
    #                      setting, so the graceful op must not carry it
    #   Restart-VM         a "hard" restart, "like powering the computer down, then back
    #                      up again", with or without -Force (which only suppresses the
    #                      confirmation prompt) — hence `power_reset`, not `restart`
    #
    # Each one resolves the VM object with Get-VM first and passes it with -VM. That is
    # part of the promise, not formatting: -Id is a Get-VM parameter and nothing else, so
    # the `Start-VM -Id '<guid>'` these entries used to read could not bind at all. This
    # table was described as transcribed from Microsoft's cmdlet reference, but the call
    # form was copied from the code it exists to check, so it asserted the code equalled
    # itself and every Hyper-V power button failed on the host while this file was green.
    # The switch reasoning above was transcribed properly and was right all along; verify
    # a parameter EXISTS, not only that it means what you think.
    ("hyperv", "start"):
        "$vm = Get-VM -Id '{vm}' -EA Stop; Start-VM   -VM $vm -ErrorAction Stop",
    ("hyperv", "stop"):
        "$vm = Get-VM -Id '{vm}' -EA Stop; Stop-VM    -VM $vm -TurnOff -Force "
        "-ErrorAction Stop",
    ("hyperv", "shutdown"):
        "$vm = Get-VM -Id '{vm}' -EA Stop; Stop-VM    -VM $vm -ErrorAction Stop",
    ("hyperv", "restart"):
        "$vm = Get-VM -Id '{vm}' -EA Stop; Restart-VM -VM $vm -Force -ErrorAction Stop",
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

def test_shutdown_is_never_mapped_onto_a_reset_or_a_reboot():
    """The four inversions, named individually so no one of them can come back alone.

    Each Shutdown button now has the `shutdown` verb; what this pins is the thing that
    must not happen again — `shutdown` resolving to a reset or a reboot on any kind.
    Stated as the resolved operation rather than as `verb != "restart"`, because the
    fault was never the verb's name.
    """
    for kind in ("proxmox", "vsphere", "xcpng", "hyperv"):
        verb = ahm.agent_verb(kind, "shutdown")
        assert verb == "shutdown", (
            f"{kind} Shutdown maps to {verb!r}; the graceful verb is `shutdown`")
        got = _agent_operation(kind, verb)
        assert got == AGENT_OP[(kind, "shutdown")], got
        for wrong in ("reset", "reboot", "hard_reboot", "TurnOff"):
            assert wrong not in str(got), (
                f"{kind} Shutdown resolves to {got!r}, which is a {wrong} — this is the "
                f"bug the `shutdown` verb was added to fix, arriving from the other side")


def test_reboot_is_never_mapped_onto_a_shutdown():
    """The same fault mirrored: Proxmox Reboot gracefully shut the VM down, and left it
    off. Both Reboot buttons must resolve to something that brings the VM back up."""
    assert _agent_operation("proxmox", ahm.agent_verb("proxmox", "reboot")) == "reboot"
    # XCP-ng's Reboot still rides `restart`, because there `restart` IS the clean reboot
    # and re-pointing a correct, working button would only break it until every deployed
    # agent is re-pulled. What matters is where it lands, which is asserted either way.
    assert ahm.agent_verb("xcpng", "reboot") == "restart"
    assert _agent_operation("xcpng", "restart") == "VM.clean_reboot"


def test_hyperv_reboot_is_refused_by_the_agent_rather_than_the_verb_list():
    """The one gap that is not the dashboard's to close.

    Hyper-V has no graceful-reboot cmdlet — Restart-VM is the hard one — so `reboot` has
    no script. The runner's verb list would answer "unknown verb 'reboot'", which reads
    as an agent too old for the dashboard; the agent refuses first and says why instead.
    """
    assert "reboot" not in _literal(_SIBLING, "VALID_VERBS")
    assert "reboot" not in _literal(_SIBLING, "_PS_POWER")
    assert "reboot" not in ahm.PAGE_OPS["hyperv"], (
        "the Hyper-V page cannot offer Reboot: nothing implements it")
    body = ast.unparse(_function(_tree(_AGENT), "_run_verb"))
    assert "reboot" in body and "hyperv" in body, (
        "_run_verb no longer refuses `reboot` for Hyper-V, so it would reach the runner "
        "and come back as 'unknown verb'")


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
    """Names the refusals, so widening or narrowing the set is a deliberate diff.

    Every Shutdown button has come off this list, and Proxmox's Reboot with them. What
    is left is the suspend family — a suspend writes guest RAM to the datastore, so it
    is a storage operation wearing a power button's clothes, and no verb covers it.
    """
    refused = {kind: {op for op in _registered_ops(kind)
                      if ahm.agent_verb(kind, op) is None}
               for kind in _AGENT_ROUTED}
    assert refused == {
        "proxmox": set(),
        "vsphere": {"suspend"},
        "xcpng": {"suspend", "resume", "pause", "unpause"},
        "hyperv": {"pause", "resume", "save"},
    }, refused


def test_an_unmapped_verb_would_become_an_inventory_sync():
    """The behaviour the guard exists for, stated once so the reason survives.

    `suspend` is a plausible thing for a router to pass through — three of these pages
    have a Suspend button — and this is what it would do. `shutdown` used to be the
    example here, which is the point: the danger is not any particular word, it is that
    an unrecognised one is silently accepted.
    """
    assert ahm.normalize({"verb": "suspend"})["verb"] == "inventory_sync"
    assert ahm.normalize({"verb": "save"})["verb"] == "inventory_sync"
    assert "suspend" not in ahm.VALID_VERBS


# ── The refusal ───────────────────────────────────────────────────────────────

def test_agent_power_job_refuses_a_verb_outside_the_allowlist():
    """The refusal lives in the shared helper, so no router can reintroduce the
    pass-through by forgetting to check."""
    body = ast.unparse(_function(_tree(_DEPS), "agent_power_job"))
    assert "WRITE_VERBS" in body, "agent_power_job does not check the verb at all"
    assert body.index("WRITE_VERBS") < body.index("normalize"), \
        "the verb must be refused BEFORE normalize() can fall it back to inventory_sync"


def test_every_refused_op_names_the_buttons_that_do_work():
    """Every refusal an operator can actually reach, not a sample.

    The message is the whole diagnosis — they are looking at a page whose button just
    failed and cannot see any of this code — so it has to name the op, the kind, and
    what they can press instead.
    """
    for kind in _AGENT_ROUTED:
        for op in sorted(_registered_ops(kind)):
            if ahm.agent_verb(kind, op) is not None:
                continue
            reason = ahm.no_verb_reason(kind, op)
            assert op in reason and kind in reason, reason
            for available in ahm.PAGE_OPS[kind]:
                assert available in reason, (
                    f"{kind}/{op}: does not offer {available}: {reason}")
            assert f"Available here: {op}" not in reason, (
                f"{kind}/{op}: offers the very op it just refused: {reason}")


def test_no_stated_reason_outlives_the_gap_it_described():
    """A stale `_NO_EQUIVALENT` entry is a lie told at the worst moment.

    Each one names a substitution that would be wrong — "the nearest verb resolves to a
    HARD reset here". Once that op HAS a verb, the sentence is false, and it is read by
    an operator with no way to check it. This change emptied the table for exactly that
    reason, so the guard is what keeps the next one from being left behind.
    """
    stale = {(kind, op) for kind, op in ahm._NO_EQUIVALENT
             if ahm.agent_verb(kind, op) is not None}
    assert not stale, (
        f"{sorted(stale)} still carry a 'no equivalent' explanation but are mapped to a "
        f"verb now — the reason is no longer true, and it is what the operator reads")


def test_an_op_with_no_stated_reason_still_gets_a_usable_message():
    reason = ahm.no_verb_reason("xcpng", "pause")
    assert "pause" in reason and "no verb" in reason, reason
    assert "start" in reason, reason


def test_the_credential_gate_also_precedes_the_job():
    """Same rule for the dashboard-held-credential blockers as for an unmappable op.

    The failure being prevented is quiet and expensive: an agent too old to understand
    `dashboard_secret` does not error on it, it falls through to a local password the
    operator has just deleted and sends an empty one. The hypervisor answers "wrong
    username or password", which reads as a credential problem rather than a version
    problem — and on the sync schedule it retries until the service account locks out.
    """
    fn = _function(_tree(_DEPS), "agent_power_job")

    def _first(predicate):
        return min((n.lineno for n in ast.walk(fn) if predicate(n)), default=None)

    gated = _first(lambda n: isinstance(n, ast.Call)
                   and getattr(n.func, "attr", "") == "dashboard_secret_blockers")
    created = _first(lambda n: isinstance(n, ast.Call)
                     and getattr(n.func, "attr", "") == "create_job")
    assert gated, "agent_power_job no longer checks dashboard_secret_blockers"
    assert gated < created, (
        f"the credential gate (line {gated}) must run before the job is created "
        f"(line {created})")


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


# The guest-agent field each page's Shutdown button consults, and which is ABSENT from an
# agent-synced row — hypervisor_view_service drops live-only values rather than fabricate
# them, and test_hypervisor_view.py::test_live_only_fields_are_absent_rather_than_zero
# pins that it keeps doing so.
_GUEST_FIELD = {"vsphere": "tools_status", "hyperv": "integration_services_state",
                "xcpng": "tools_installed"}


def test_a_shutdown_button_is_not_gated_on_a_field_the_agent_never_syncs():
    """The second gate, which `agentOps` cannot open.

    Every Shutdown button has a guest-agent condition as well as `canOp`, and the field
    behind it is one an agent-bound row does not carry. Compared directly — `!vm.tools_
    installed`, `vm.tools_status === 'toolsOk'` — `undefined` reads as "no guest agent",
    so adding `shutdown` to PAGE_OPS and to `agentOps` would still leave the button dead
    on every agent-bound connection: the exact "feature looks present" shape this file
    exists for, arriving one gate further down.

    So the condition has to distinguish UNKNOWN from ABSENT, which is what the helper
    does. Pinned per page because each product names the field differently and only the
    page knows which one it means.
    """
    for kind, field in sorted(_GUEST_FIELD.items()):
        markup = _read(os.path.join(_TEMPLATES, kind, "index.html"))
        assert re.search(r"\n\s*guestToolsMaybeReady\(vm\)\s*\{", markup), (
            f"{kind} page has no guestToolsMaybeReady() — its Shutdown button is gated "
            f"on {field}, which an agent-synced row does not have")
        body = markup[markup.index("guestToolsMaybeReady(vm) {"):]
        body = body[:body.index("\n    },")]
        assert "undefined" in body and field in body, (
            f"{kind}: guestToolsMaybeReady must treat an absent {field} as unknown")
        # And no raw truthiness test may survive anywhere on the page, or the helper is
        # dead code sitting beside the gate it was written to replace. Both spellings:
        # `|| !vm.x` disables a button, `&& !vm.x` shows a "no guest tools" badge — and
        # on an agent-bound row the badge fires on every running VM, asserting something
        # the page never measured.
        for spelling in (f"|| !vm.{field}", f"&& !vm.{field}"):
            assert spelling not in markup, (
                f"{kind} page still treats an absent {field} as a negative: {spelling}")
        assert f"vm.{field} === 'toolsOk'\">" not in markup, (
            f"{kind} page still renders Shutdown only when {field} is positively known")


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

# Verbs the sibling runner does not carry, and why each one is a property of the
# transport rather than an unfinished job. Anything NOT listed here has to be present in
# the runner the moment it enters WRITE_VERBS, or an agent-bound Hyper-V connection gets
# its refusal from a place the operator has no reason to look.
_NOT_IN_SIBLING = {
    # Needs per-kind snapshot APIs the runner has no path to.
    "snapshot",
    # Hyper-V has no graceful-reboot cmdlet at all: Restart-VM is documented as a "hard"
    # restart, "like powering the computer down, then back up again", which is already
    # what `power_reset` runs. There is no second script to give `reboot`.
    "reboot",
}


def test_the_sibling_runner_implements_exactly_the_shared_verbs():
    """Hyper-V and bare ESXi go through `runners/hypervisor/run.py`, not the agent.

    It keeps its own verb list, so it is another copy of the same contract and drifts the
    same way — which is why the exclusions above have to be stated rather than inferred
    from whatever the runner happens to contain.
    """
    runner_verbs = set(_literal(_SIBLING, "VALID_VERBS"))
    ps_power = set(_literal(_SIBLING, "_PS_POWER"))
    expected = (set(ahm.WRITE_VERBS) - _NOT_IN_SIBLING) | set(ahm.READ_VERBS)
    assert runner_verbs == expected, (
        f"the sibling runner accepts {sorted(runner_verbs)} but the dashboard's "
        f"allowlist is {sorted(expected)}")
    assert ps_power == expected - set(ahm.READ_VERBS), (
        f"_PS_POWER implements {sorted(ps_power)}, which is not the runner's own "
        f"power-verb list {sorted(expected - set(ahm.READ_VERBS))}")
    assert _NOT_IN_SIBLING <= set(ahm.WRITE_VERBS), (
        f"{sorted(_NOT_IN_SIBLING - set(ahm.WRITE_VERBS))} is excused from the runner "
        f"but is not a verb any more — drop it from _NOT_IN_SIBLING")


def test_the_graceful_stop_is_the_same_call_on_both_hyperv_paths():
    """One button, one behaviour, either route — the drift that made Force Off graceful,
    arriving from the other side.

    `Stop-VM -Force` is not the prompt suppressor it is on `Restart-VM`: Microsoft
    documents it as shutting down "regardless of any unsaved application data", forcing
    it after five minutes. So it changes what Shutdown promises, and neither path may
    grow it while the other has not.
    """
    agent = _literal(_SIBLING, "_PS_POWER")["shutdown"]
    direct = _literal(os.path.join(_ROOT, "web_dashboard", "services",
                                   "hyperv_service.py"), "_POWER_OPS_PS")["shutdown"]
    for switch in ("-TurnOff", "-Force"):
        assert switch not in agent and switch not in direct, (
            f"{switch} is on a graceful Hyper-V shutdown: agent={agent!r} "
            f"direct={direct!r}")
    assert "Stop-VM" in agent and "Stop-VM" in direct


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
