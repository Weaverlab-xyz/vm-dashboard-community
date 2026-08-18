"""A power button on an agent-bound connection must enqueue an agent job.

An agent-bound connection stores no host and no credential — that is the whole design —
so calling the service layer for it cannot work: `hyperv_service._session()` raises
"has no host configured", and if a host were somehow present the dashboard has no route
to that network anyway. `hypervisor_deps.agent_power_job` exists to turn the button into
an `agent_hypervisor` job instead, and `api/hyperv.py` was the one router that never
called it. Its VMs listed fine (the page reads the synced cache) while every Start and
Stop went down the dead direct path, which is the worst shape a gap can have: the feature
looks present.

The second thing pinned here is the verb mapping, and it matters more than it looks.
`agent_hypervisor_meta.normalize()` falls an unrecognised verb back to `inventory_sync`
on purpose. So a router that maps one of its ops to a verb the allowlist does not contain
does not fail — it enqueues a SCAN, which succeeds, and the operator is told their power
operation completed while the VM never moved. Every value in every router's
`_AGENT_VERBS` therefore has to be a real write verb, and an op with no honest mapping
has to be refused instead of passed through.

AST only, no imports of the routers themselves (they pull in FastAPI and the hypervisor
SDKs). Runs under pytest, or standalone:
    python tests/test_hypervisor_power_routing.py
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API = os.path.join(_ROOT, "web_dashboard", "api")
_META = os.path.join(_ROOT, "web_dashboard", "services", "agent_hypervisor_meta.py")
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")
_SIBLING = os.path.join(_ROOT, "runners", "hypervisor", "run.py")

_spec = importlib.util.spec_from_file_location("agent_hypervisor_meta", _META)
ahm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ahm)

# The routers whose power buttons must reach an agent-bound connection. `nutanix` is
# deliberately NOT here: the agent refuses Nutanix power verbs (a v3 power change is a
# full spec PUT, not an action), so routing them to it would enqueue jobs that can only
# fail. test_nutanix_power_is_still_unimplemented_in_the_agent is the other half of that
# statement — if the agent ever grows the implementation, it fails and points here.
_AGENT_ROUTED = ("hyperv", "proxmox", "vsphere", "xcpng")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


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


def _agent_verbs(kind: str) -> dict:
    """The router's ``_AGENT_VERBS`` literal."""
    for node in _tree(os.path.join(_API, f"{kind}.py")).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_AGENT_VERBS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"api/{kind}.py has no _AGENT_VERBS")


# ── the button reaches the agent ───────────────────────────────────────────────

def test_every_agent_routed_power_endpoint_calls_agent_power_job():
    """The regression: api/hyperv.py imported conn_or_error and conn_in_task but not
    agent_power_job, so an agent-bound Hyper-V host had power buttons that could only
    fail."""
    missing = []
    for kind in _AGENT_ROUTED:
        handler = _function(_tree(os.path.join(_API, f"{kind}.py")), "_power_endpoint")
        if "agent_power_job" not in _calls(handler):
            missing.append(f"api/{kind}.py::_power_endpoint")
    assert not missing, ("power endpoint never offers the connection to the agent:\n  "
                         + "\n  ".join(missing))


def test_the_agent_branch_comes_before_the_direct_call():
    """Order, not just presence: `create_job` for the direct path must not run first, or
    an agent-bound connection gets a local job row as well as (or instead of) the agent
    one."""
    for kind in _AGENT_ROUTED:
        body = ast.unparse(_function(_tree(os.path.join(_API, f"{kind}.py")),
                                     "_power_endpoint"))
        assert body.index("agent_power_job") < body.index("create_job"), kind


# ── the verbs are real ─────────────────────────────────────────────────────────

def test_every_mapped_verb_is_a_real_write_verb():
    """A verb outside the allowlist is normalised to `inventory_sync`, so this mapping is
    the difference between a power operation and a scan reported as one."""
    bad = []
    for kind in _AGENT_ROUTED:
        for op, verb in _agent_verbs(kind).items():
            if verb not in ahm.WRITE_VERBS:
                bad.append(f"api/{kind}.py: {op!r} -> {verb!r}")
    assert not bad, ("mapped to a verb that is not in WRITE_VERBS, which normalize() "
                     "would silently turn into inventory_sync:\n  " + "\n  ".join(bad))


def test_an_unmapped_verb_would_become_an_inventory_sync():
    """The behaviour the guard exists for, stated once so the reason survives.

    `shutdown` is a plausible thing for a router to pass through — every one of these
    pages has a Shutdown button — and this is what it would do.
    """
    assert ahm.normalize({"verb": "shutdown"})["verb"] == "inventory_sync"
    assert "shutdown" not in ahm.VALID_VERBS


def test_agent_power_job_refuses_a_verb_outside_the_allowlist():
    """The refusal lives in the shared helper, so no router can reintroduce the
    pass-through by forgetting to check."""
    node = _function(_tree(os.path.join(_API, "hypervisor_deps.py")), "agent_power_job")
    body = ast.unparse(node)
    assert "WRITE_VERBS" in body, "agent_power_job does not check the verb at all"
    assert body.index("WRITE_VERBS") < body.index("normalize"), \
        "the verb must be refused BEFORE normalize() can fall it back to inventory_sync"


# ── Hyper-V specifically ───────────────────────────────────────────────────────

def test_hyperv_maps_only_the_ops_the_sibling_runner_implements():
    """Hyper-V power runs in the sibling container over WinRM, and its script table is
    the real limit on what the page can offer."""
    with open(_SIBLING, "r", encoding="utf-8") as handle:
        sibling = ast.parse(handle.read(), filename=_SIBLING)
    implemented = None
    for node in sibling.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_PS_POWER" for t in node.targets):
            implemented = set(ast.literal_eval(node.value))
    assert implemented, "runners/hypervisor/run.py has no _PS_POWER table"
    mapped = set(_agent_verbs("hyperv").values())
    assert mapped <= implemented, f"{mapped - implemented} has no Hyper-V script"


def test_the_ops_hyperv_cannot_do_through_an_agent_are_refused_not_approximated():
    """`shutdown`, `pause`, `resume` and `save` have no agent verb. The handler must
    refuse them for an agent-bound connection — mapping `shutdown` onto `power_off`
    would turn "ask the guest to shut down" into a hard power cut."""
    verbs = _agent_verbs("hyperv")
    for op in ("shutdown", "pause", "resume", "save"):
        assert op not in verbs, f"{op!r} must not be mapped to an agent verb"
    body = ast.unparse(_function(_tree(os.path.join(_API, "hyperv.py")),
                                 "_power_endpoint"))
    assert "_AGENT_VERBS" in body and "501" in body, \
        "the handler must refuse an unmapped op instead of passing it to the agent"
    assert body.index("501") < body.index("agent_power_job"), \
        "the refusal has to come before the enqueue"


def test_every_hyperv_power_route_is_still_covered_one_way_or_the_other():
    """Each registered /power/<op> is either mapped to an agent verb or refused by the
    501 branch — nothing may fall between the two."""
    tree = _tree(os.path.join(_API, "hyperv.py"))
    routes = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_api_route" and node.args
                and isinstance(node.args[0], ast.Constant)):
            routes.add(node.args[0].value.rsplit("/", 1)[-1])
    assert routes == {"start", "shutdown", "stop", "restart", "pause", "resume", "save"}, \
        f"the Hyper-V power routes changed: {sorted(routes)}"
    assert set(_agent_verbs("hyperv")) <= routes, "a mapping for an op with no route"


def test_the_page_greys_exactly_the_ops_the_router_refuses():
    """Binding-to-model drift, in the direction that hurts: the template's `agentOps`
    decides which buttons are clickable on an agent-bound host, so a verb added to the
    router and not to the page stays dead, and one removed from the router leaves a
    button that 501s."""
    path = os.path.join(_ROOT, "web_dashboard", "templates", "hyperv", "index.html")
    with open(path, "r", encoding="utf-8") as handle:
        markup = handle.read()
    match = re.search(r"agentOps:\s*\[([^\]]*)\]", markup)
    assert match, "the Hyper-V page no longer declares agentOps"
    declared = {v.strip().strip("'\"") for v in match.group(1).split(",") if v.strip()}
    assert declared == set(_agent_verbs("hyperv")), \
        f"page offers {sorted(declared)}, router maps {sorted(_agent_verbs('hyperv'))}"
    assert "viaAgent: {{" in markup, "viaAgent must come from the server, not be guessed"


# ── the exclusion is honest ────────────────────────────────────────────────────

def test_nutanix_power_is_still_unimplemented_in_the_agent():
    """Why api/nutanix.py has no agent branch. If this starts failing, the agent has
    grown Nutanix power and the router should route to it."""
    node = _function(_tree(_AGENT), "_power_nutanix")
    body = ast.unparse(node)
    assert "PolicyRefusal" in body and "not implemented" in body, \
        "the agent implements Nutanix power now — api/nutanix.py should use it"
    tree = _tree(os.path.join(_API, "nutanix.py"))
    assert "agent_power_job" not in _calls(_function(tree, "_power_endpoint"))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
