"""The Config-Management route MATCHER: normalisation, and which agent an address resolves to.

This is the half that decides who runs a playbook, and it is deliberately testable without
the app's dependencies installed — the service's module-level imports are stdlib only and
everything ORM-shaped is imported inside the function that needs it. That is not a detail:
on a developer box without sqlalchemy every database-backed test file in this repo prints
SKIP and exits 0, so logic that can only be reached through a Session is logic that is
covered on CI alone. :func:`test_the_matcher_imports_nothing_from_the_app` pins the property
this file depends on.

The CRUD half lives in tests/test_config_mgmt_routes.py, which does need a database.

Runs under pytest, or standalone:  python tests/test_config_mgmt_route_match.py
"""
import ast
import importlib.util
import ipaddress
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SVC_PATH = os.path.join(_ROOT, "web_dashboard", "services", "config_mgmt_route_service.py")

_spec = importlib.util.spec_from_file_location("cmr_match_under_test", _SVC_PATH)
cmr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmr)


def _route(agent: str, cidr: str) -> "cmr.Route":
    return cmr.Route(id="r-" + cidr, agent_id=agent,
                     network=ipaddress.ip_network(cidr), label="", cidr=cidr)


def _table(*pairs) -> "cmr.RouteTable":
    """A table built the way :func:`cmr.load_table` builds one, sort included — so these
    tests exercise the real ordering rather than a convenient one."""
    routes = [_route(agent, cidr) for agent, cidr in pairs]
    routes.sort(key=lambda r: (-r.network.prefixlen, r.network.version,
                               r.network.network_address))
    return cmr.RouteTable(tuple(routes))


# ── the property this file rests on ──────────────────────────────────────────

def test_the_matcher_imports_nothing_from_the_app():
    """Module-level imports must stay stdlib.

    Hoisting `from ..database import …` to the top would be a tidy-looking change that
    silently moves every test below onto CI only. The service says so in a comment; this
    is the assertion behind it.
    """
    with open(_SVC_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    offenders = []
    for node in tree.body:            # MODULE level only — nested imports are the point
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.split(".")[0] in ("sqlalchemy", "web_dashboard")]
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # a relative import reaches into the app
                offenders.append("." * node.level + (node.module or ""))
            elif (node.module or "").split(".")[0] in ("sqlalchemy", "web_dashboard"):
                offenders.append(node.module)
    assert not offenders, (
        f"config_mgmt_route_service gained module-level app imports {offenders} — the "
        f"matcher is no longer testable without the app's dependencies, so these tests "
        f"now skip instead of running")


# ── matching ─────────────────────────────────────────────────────────────────

def test_longest_prefix_wins():
    """A routing table, and for the same reason: a lab has a broad range with a narrower
    exception inside it, and the exception has to win or it cannot be expressed."""
    t = _table(("broad", "192.168.0.0/16"), ("narrow", "192.168.235.0/24"),
               ("one-host", "192.168.235.99/32"))
    assert t.executor_for("192.168.235.99") == "one-host"
    assert t.executor_for("192.168.235.50") == "narrow"
    assert t.executor_for("192.168.9.9") == "broad"


def test_resolution_is_independent_of_row_order():
    """Insertion order must not decide who runs a playbook. With the unique constraint on
    `cidr`, prefix length plus address is a TOTAL order, so there is no tie left to break
    by `created_at` — which would be invisible to the operator."""
    pairs = [("broad", "10.0.0.0/8"), ("mid", "10.1.0.0/16"), ("narrow", "10.1.2.0/24")]
    first = _table(*pairs).executor_for("10.1.2.3")
    second = _table(*reversed(pairs)).executor_for("10.1.2.3")
    assert first == second == "narrow"


def test_no_match_falls_back_to_the_brokering_agent():
    """THE BACKWARDS-COMPATIBILITY CONTRACT. An address no route covers resolves to the
    fallback the caller passed — the agent whose connection discovered the VM — so adding
    this feature changed nothing for an address nobody routed."""
    t = _table(("elsewhere", "192.168.235.0/24"))
    assert t.executor_for("10.0.0.5", fallback="broker") == "broker"


def test_an_empty_table_changes_nothing():
    """The state every install is in until an operator adds a row, and the one that has to
    behave exactly as the code did before this module existed."""
    assert cmr.EMPTY.executor_for("192.168.235.99", fallback="broker") == "broker"
    assert not cmr.EMPTY
    assert cmr.EMPTY.match_for("192.168.235.99") is None


def test_a_hostname_or_empty_address_falls_back_rather_than_raising():
    """A VM with no synced address projects `ip: ""`, and a private_host is as often a
    name as an address. Neither is an error here: matching nothing sends the run to the
    discovering agent, which is the conservative answer and the one that keeps a VM with
    no address pointing at the agent that would give it one."""
    # Even a catch-all route cannot match something that is not an address, so every one
    # of these falls through to the broker rather than raising.
    for table in (_table(("routed", "0.0.0.0/0")), cmr.EMPTY):
        for value in ("", "   ", "db.internal.example", "not-an-ip", None):
            assert table.executor_for(value, fallback="broker") == "broker", \
                f"{value!r} did not fall back"
            assert table.match_for(value) is None


def test_v4_and_v6_never_cross():
    """One flat sorted list holds both families, which is only safe because `in` returns
    False across versions rather than raising."""
    t = _table(("four", "192.168.0.0/16"), ("six", "fd00::/8"))
    assert t.executor_for("192.168.1.1") == "four"
    assert t.executor_for("fd00::1") == "six"
    assert t.executor_for("2001:db8::1", fallback="broker") == "broker"


def test_a_default_route_is_allowed_but_loses_to_a_narrower_one():
    """`0.0.0.0/0` is a legitimate single-agent lab. It must not become a way to
    accidentally capture a segment somebody routed deliberately."""
    t = _table(("catch-all", "0.0.0.0/0"), ("specific", "192.168.235.0/24"))
    assert t.executor_for("192.168.235.99") == "specific"
    assert t.executor_for("8.8.8.8") == "catch-all"


def test_the_match_names_the_range_that_decided():
    """A refusal has to be able to say WHICH row sent the run elsewhere, or the operator
    cannot find it to change it."""
    t = _table(("agent-b", "192.168.235.0/24"))
    match = t.match_for("192.168.235.99")
    assert match is not None
    assert match.cidr == "192.168.235.0/24"
    assert match.agent_id == "agent-b"


# ── normalisation ────────────────────────────────────────────────────────────

def test_a_range_is_stored_normalised_and_trimmed():
    assert cmr.normalize_cidr("  192.168.235.0/24 ") == "192.168.235.0/24"
    assert cmr.normalize_cidr("fd00::/8") == "fd00::/8"


def test_a_bare_address_becomes_a_single_host_route():
    """Common enough for one lab VM that making the operator write /32 is pedantry."""
    assert cmr.normalize_cidr("192.168.235.99") == "192.168.235.99/32"
    assert cmr.normalize_cidr("fd00::1") == "fd00::1/128"


def test_a_host_bits_typo_is_refused_with_the_network_it_meant():
    """`192.168.235.1/24` is almost certainly the .0 network. Almost is not enough to
    assume, but it is enough to say so."""
    try:
        cmr.normalize_cidr("192.168.235.1/24")
    except cmr.ConfigRouteError as exc:
        assert "192.168.235.0/24" in str(exc), exc
        assert "192.168.235.1/32" in str(exc), \
            "the refusal should also offer the single-host form the operator may have meant"
    else:
        raise AssertionError("a host address with a /24 was accepted as a network")


def test_a_widening_prefix_is_refused_rather_than_normalised():
    """THE TYPO WITH CONSEQUENCES. `192.168.235.0/8` normalised with strict=False is
    `192.0.0.0/8` — silently handing one agent sixteen million addresses including other
    people's segments. It has to be an error, not a correction."""
    try:
        cmr.normalize_cidr("192.168.235.0/8")
    except cmr.ConfigRouteError as exc:
        assert "192.0.0.0/8" in str(exc), exc
    else:
        raise AssertionError("a /8 written against a /24 address was silently widened")


def test_a_range_every_agent_denies_is_refused():
    """Loopback and link-local are in the agent's permanent deny list, re-added at policy
    load whatever `ansible.targets` says. A route for one can only produce failing jobs, so
    it is refused where the operator is looking rather than in a job log."""
    for bad in ("127.0.0.0/8", "127.0.0.1", "169.254.169.254", "::1", "fe80::/10"):
        try:
            cmr.normalize_cidr(bad)
        except cmr.ConfigRouteError as exc:
            assert "deni" in str(exc).lower(), f"{bad}: {exc}"
        else:
            raise AssertionError(f"{bad} was accepted as a Config-Management target")


def test_malformed_input_is_refused_by_name():
    for bad in ("", "   ", "banana", "192.168.235.0/99", "192.168.235.0/-1", "1.2.3.4.5"):
        try:
            cmr.normalize_cidr(bad)
        except cmr.ConfigRouteError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_the_refusals_all_say_what_to_type_instead():
    """House rule for this codebase, and the reason these messages are long: a refusal an
    operator reads in a form has to name the fix, not just the fault."""
    for bad in ("", "banana", "192.168.235.1/24", "192.168.235.0/8", "127.0.0.0/8"):
        try:
            cmr.normalize_cidr(bad)
        except cmr.ConfigRouteError as exc:
            text = str(exc)
            assert any(hint in text for hint in ("192.168", "Enter", "did you mean")), \
                f"{bad!r} produced a refusal with no remedy in it: {text}"


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
