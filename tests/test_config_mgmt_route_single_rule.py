"""ONE module decides which agent executes a Config-Management run, and it decides it
AFTER the target address is pinned.

Both halves of that sentence are invariants a future refactor could break without any
other test noticing, because breaking either one still produces working software — just
software with two answers to "who runs this", or with the routing table able to influence
which address a playbook is aimed at.

The picker (`/api/config-mgmt/agent-targets`) and the enqueue gate (`_resolve_agent_target`)
must agree, and they can only be relied on to agree if there is one implementation. The
gate then accepts exactly the agent the picker named — not "that one OR the brokering
agent", because an OR lets a caller name an agent with no network path and get a job that
leases, runs and times out, which is the bug this feature exists to remove.

Text-and-AST checks, no app imports, so it runs on a checkout without the requirements
installed. Runs under pytest or standalone:
    python tests/test_config_mgmt_route_single_rule.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SERVICE = os.path.join("web_dashboard", "services", "config_mgmt_route_service.py")
# Every module that CONSUMES the decision. None of them may re-implement any part of it.
_CONSUMERS = (
    os.path.join("web_dashboard", "api", "config_mgmt.py"),
    os.path.join("web_dashboard", "api", "connections.py"),
    os.path.join("web_dashboard", "services", "inventory_service.py"),
)


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _func_source(path: str, name: str) -> str:
    """The source of one function, by AST span — so a match inside it cannot be a match
    from a neighbour that happens to sit nearby."""
    text = _read(path)
    tree = ast.parse(text)
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"no function {name!r} in {path}")


def _py_files(root: str):
    for base, dirs, files in os.walk(os.path.join(_ROOT, root)):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                yield os.path.relpath(os.path.join(base, name), _ROOT)


# ── the ordering invariant ───────────────────────────────────────────────────

def test_the_executor_is_resolved_below_the_address_pin():
    """THE SECURITY PROPERTY, asserted as source order.

    `host = payload.target if payload.target in ips else ips[0]` is the anti-substitution
    control: the address a run targets must be one the discovering agent itself reported.
    The executor is then resolved FROM that pinned address, so the route table is an input
    to the AGENT decision and can never become an input to the ADDRESS decision.

    Hoisting the resolution above the pin would still work, and would silently let a
    caller's chosen address select which agent runs against it. Nothing else would fail.
    """
    body = _func_source(_CONSUMERS[0], "_resolve_agent_target")
    pin = body.find("host = payload.target")
    resolve = body.find("executor_for(")
    assert pin != -1, "the address pin in _resolve_agent_target changed shape"
    assert resolve != -1, "_resolve_agent_target no longer resolves an executor"
    assert pin < resolve, (
        "the executor is now resolved BEFORE the target address is pinned — the route "
        "table can influence which address a playbook is aimed at")


def test_the_gate_accepts_one_agent_not_a_set_of_them():
    """Guards against "helpfully" restoring the `or conn.agent_id` form.

    One correct answer per (connection, VM, address) is what makes the picker and the gate
    unable to disagree. A membership test would re-admit the agent that cannot reach the
    target.
    """
    body = _func_source(_CONSUMERS[0], "_resolve_agent_target")
    assert re.search(r"if\s+agent\.id\s*!=\s*expected\s*:", body), \
        "the executor check is no longer a plain equality against one resolved agent"
    # `conn.agent_id` may appear ONCE, as the fallback handed to the resolver. It may not
    # appear in a comparison against the run's agent: that is the old rule, and keeping it
    # alongside the resolver is exactly the "broker OR delegate" widening this refuses.
    assert not re.search(r"agent\.id\s*(==|!=)\s*\(?\s*conn\.agent_id", body), \
        "the brokering agent is compared against the run's agent again — an OR in disguise"
    assert not re.search(r"agent\.id\s*(not\s+)?in\s+[\(\[]", body), \
        "the executor check became a membership test, which admits more than one agent"


# ── one implementation ──────────────────────────────────────────────────────

def test_the_enqueue_gate_resolves_through_the_shared_module():
    body = _func_source(_CONSUMERS[0], "_resolve_agent_target")
    assert body.count("cmr.executor_for(") == 1, \
        "the gate should resolve the executor exactly once, through the shared module"
    assert "ConfigMgmtRoute" not in body, \
        "the gate queries the route table directly instead of going through the resolver"


def test_the_target_list_resolves_through_the_shared_module():
    """`_hv_item` is where a target's agent_id is decided for the picker, the bulk fan-out
    and `_target_spec` alike — all three read the same field, so this is the only place it
    can be decided without them drifting apart."""
    item = _func_source(_CONSUMERS[2], "_hv_item")
    assert "routes.executor_for(" in item, \
        "_hv_item no longer resolves the executing agent through the route table"
    assert "ConfigMgmtRoute" not in item


def test_the_route_table_is_loaded_once_per_collect_not_per_vm():
    """The static half of the N+1 guard. `_hv_item` is called once per VM and is pure, so
    a `load_table` inside it would be a query per row."""
    inv = _read(_CONSUMERS[2])
    assert inv.count("cmr.load_table(") == 1, \
        "inventory_service loads the route table more than once per collect()"
    item = _func_source(_CONSUMERS[2], "_hv_item")
    assert "load_table(" not in item, \
        "_hv_item loads the route table itself — that is one query per VM"
    items = _func_source(_CONSUMERS[2], "_hypervisor_items")
    assert "cmr.load_table(" in items, \
        "the bulk loader no longer loads the table where the other bulk lookups happen"


def test_only_the_resolver_module_knows_the_route_table():
    """The model may be referenced by its own definition and by the one module that owns
    it. Anywhere else is a second copy of the rule waiting to disagree."""
    allowed = {os.path.join("web_dashboard", "database.py"), _SERVICE}
    offenders = [p for p in _py_files("web_dashboard")
                 if "ConfigMgmtRoute" in _read(p) and p not in allowed]
    assert not offenders, \
        f"these modules query the route table directly instead of using the resolver: {offenders}"


def test_no_consumer_does_its_own_cidr_arithmetic():
    """`ipaddress` is used legitimately elsewhere in the app (GKE master ranges, POV
    networks, Password Safe address validation), so this is scoped to the modules that
    consume the routing decision. Any prefix arithmetic THERE is a reimplementation."""
    for path in _CONSUMERS:
        text = _read(path)
        for needle in ("ip_network", "prefixlen", "ip_address("):
            assert needle not in text, (
                f"{path} does its own address arithmetic ({needle}) — the matching rule "
                f"belongs only in config_mgmt_route_service")


def test_the_resolver_is_the_only_thing_the_consumers_import_for_it():
    for path in _CONSUMERS:
        assert "config_mgmt_route_service as cmr" in _read(path), \
            f"{path} no longer imports the shared resolver under the expected name"


# ── the run form gained no new axis ─────────────────────────────────────────

def test_the_run_form_still_sends_the_resolved_agent_and_offers_no_picker():
    """The design decision that made this feature cheap: the executor is a property of the
    target row, so the run form needed no new field. An agent picker there would be a
    second way to choose, and the gate would refuse whatever it chose that the picker
    did not."""
    page = _read("web_dashboard", "templates", "config-mgmt", "index.html")
    assert "agent_id: at.agent_id" in page, \
        "the run form no longer sends the agent the target list resolved"
    assert not re.search(r"""x-model=["']form\.agent_id["']""", page), \
        "the run form grew an agent picker — the executor is resolved server-side"


def test_the_guards_would_catch_the_changes_they_were_written_for():
    """A static test that cannot fail is worse than no test, because it reads as coverage.

    Both checks above are re-run here against synthetic sources that DO contain the
    regressions, to prove they discriminate rather than merely matching today's file.
    """
    reversed_order = (
        'def _resolve_agent_target(payload, db):\n'
        '    expected = cmr.executor_for(db, payload.target)\n'
        '    host = payload.target if payload.target in ips else ips[0]\n')
    assert reversed_order.find("host = payload.target") > reversed_order.find("executor_for("), \
        "the ordering comparison does not detect a resolution hoisted above the pin"

    widened = 'if agent.id != expected and agent.id != conn.agent_id:\n    raise\n'
    assert re.search(r"agent\.id\s*(==|!=)\s*\(?\s*conn\.agent_id", widened), \
        "the OR-in-disguise pattern is not detected"

    membership = 'if agent.id not in (expected, conn.agent_id):\n    raise\n'
    assert re.search(r"agent\.id\s*(not\s+)?in\s+[\(\[]", membership), \
        "the membership-test pattern is not detected"


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
