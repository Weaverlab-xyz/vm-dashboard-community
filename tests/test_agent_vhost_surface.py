"""What the remote-agent vhost actually publishes, checked against what it claims to.

``docker-compose.agent.yml`` puts Caddy in front of the dashboard and splits it in two:
one hostname reachable from wherever the agents live, serving the agent protocol, and
one internal hostname serving everything else. The gateway selects by path prefix, so
the split exists only insofar as the prefix says so.

That is two copies of one rule — the matcher in ``examples/remote-agent/Caddyfile`` and
the prefixes in ``web_dashboard/api/agent.py`` — and they drifted. The matcher read
``/api/agent*``, with no slash, which is a prefix match on the raw string; the operator
console's routes sat on the same ``/api/agent`` prefix; so ``POST
/api/agent/{id}/enrollment-code`` and ``DELETE /api/agent/{id}`` were published on the
internet-facing vhost while both files' comments stated in plain words that they were
not. Bearer-gated, so never an open door — but minting enrolment codes and revoking
agents are not operations to leave on the hostile side of a wall you built on purpose,
and a comment that is wrong is worse than no comment.

So this test holds one copy of the rule: it **parses the matcher out of the real
Caddyfile** and checks it against the app's real route table.

  * Nothing reachable through that matcher may require a human permission. A route that
    takes ``require_permission``/``require_explicit_permission`` is an operator route by
    construction — there is no operator on an agent poll.
  * Everything the agent protocol needs must still be reachable through it, or the split
    stops being a security property and starts being an outage.
  * No OTHER router may creep under the matcher either. ``/api/agentcell`` is the live
    example of how easily that happens: it never shared a router with any of this, and
    the old no-slash matcher published all of it.

Imports the whole app, because the question is about the whole app's surface and not
about one module's routers. Skips only when the third-party deps are absent. Runs under
pytest, or standalone:
    python tests/test_agent_vhost_surface.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agent-vhost-surface")

_CADDYFILE = os.path.join(_ROOT, "examples", "remote-agent", "Caddyfile")
_COMPOSE = os.path.join(_ROOT, "docker-compose.agent.yml")

# Probe the optional third-party packages by name, then import the app UNGUARDED. A
# wider guard here would turn "web_dashboard.main is broken" into a silent skip, and this
# file's whole job is to notice things that are silently not what they claim to be.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi.routing import APIRoute  # noqa: E402
from web_dashboard.main import app  # noqa: E402


# ── reading the gateway config ────────────────────────────────────────────────

def _proxied_matchers():
    """Every path matcher on a `handle` block in the Caddyfile that proxies to the app.

    Deliberately ignores comment lines. The Caddyfile's comments have to be able to
    name the wrong matcher in order to explain why it is wrong, and a parser that read
    them would fail this test on its own documentation.
    """
    matchers, pending, depth = [], None, 0
    for raw in open(_CADDYFILE, encoding="utf-8"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # `handle /path/* {` — capture the matchers, then look for a reverse_proxy
        # inside the block before deciding it is proxied to the app at all.
        m = re.match(r"handle\s+(.*?)\s*\{$", line)
        if m and m.group(1):
            pending, depth = m.group(1).split(), 1
            continue
        if pending is None:
            continue
        depth += line.count("{") - line.count("}")
        if line.startswith("reverse_proxy"):
            matchers.extend(pending)
        if depth <= 0:
            pending = None
    return matchers


def _matches(matcher, path):
    """Caddy path matcher semantics, for the two forms this file uses.

    A trailing ``*`` is a prefix match on the raw path; anything else is exact (Caddy
    also treats a bare prefix as matching the path with a trailing slash, which does not
    change any answer here). Notably ``/api/agent*`` matches ``/api/agents`` — that is
    the entire bug this module exists to stop coming back.
    """
    if matcher.endswith("*"):
        return path.startswith(matcher[:-1])
    return path == matcher


def _api_routes():
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _permission_gates(route):
    """The human-permission dependencies guarding a route, as ``scope:level`` strings.

    Reads the tags ``api/auth._tag`` attaches to both permission factories, rather than
    a function name — both factories return a closure called ``_check``.
    """
    gates = []
    for dep in route.dependant.dependencies:
        fn = dep.call
        scope = getattr(fn, "permission_scope", None)
        if scope:
            gates.append(f"{scope}:{getattr(fn, 'permission_level', '?')}")
    return gates


def _proxied_routes():
    matchers = _proxied_matchers()
    return [r for r in _api_routes()
            if any(_matches(m, r.path) for m in matchers)], matchers


# ── the invariants ────────────────────────────────────────────────────────────

def test_the_caddyfile_still_proxies_something():
    """A guard on the parser itself: every assertion below is vacuously true if this
    file stops finding the matcher, which is exactly what a Caddyfile reformat would
    do."""
    matchers = _proxied_matchers()
    assert matchers, f"no proxied `handle` matcher found in {_CADDYFILE}"
    assert matchers == ["/api/agent/*"], (
        f"the agent vhost proxies {matchers}. If that is deliberate, the comments in "
        f"the Caddyfile and docker-compose.agent.yml describe the old set and need "
        f"updating too — they are the reason this test exists.")


def test_no_route_needing_a_human_permission_is_published_on_the_agent_vhost():
    """The property the vhost split is FOR.

    A route with a permission dependency is an operator route: there is no operator
    session on an agent poll, so the only callers it can have are humans on the console
    — which is the side of the wall it belongs on. Minting an enrolment code and
    revoking an agent are the two that were on the wrong side.
    """
    routes, matchers = _proxied_routes()
    offenders = [f"{sorted(r.methods)} {r.path} ({', '.join(_permission_gates(r))})"
                 for r in routes if _permission_gates(r)]
    assert not offenders, (
        f"{matchers} publishes {len(offenders)} route(s) that require a human "
        f"permission, on the vhost reachable from wherever the agents live:\n  "
        + "\n  ".join(offenders)
        + "\nMove them to a prefix the matcher does not reach (the operator half of "
          "api/agent.py lives on /api/agents), or narrow the matcher.")


def test_the_agent_protocol_is_still_reachable():
    """The other direction. A matcher narrow enough to publish nothing would pass the
    test above and break every agent in the field, with the failure showing up as
    enrolment hanging rather than as anything that names a proxy."""
    routes, matchers = _proxied_routes()
    published = {r.path for r in routes}
    required = {"/api/agent/enroll", "/api/agent/lease",
                "/api/agent/jobs/{job_id}/heartbeat",
                "/api/agent/jobs/{job_id}/logs",
                "/api/agent/jobs/{job_id}/complete",
                "/api/agent/jobs/{job_id}/secret",
                "/api/agent/jobs/{job_id}/gateway-key",
                "/api/agent/jobs/{job_id}/ansible-bundle"}
    missing = sorted(required - published)
    assert not missing, (
        f"{matchers} does not reach {missing}. The agent calls these — see "
        f"runners/agent/agent.py — so an agent behind this gateway would get the "
        f"vhost's catch-all 404 instead.")


def test_only_the_agent_protocol_router_is_published():
    """Nothing else may drift under the matcher.

    /api/agentcell is the worked example: a different feature, a different router, a
    console API with no signature scheme anywhere in it — and `/api/agent*` published
    every route on it, because prefix matching does not care about word boundaries.
    """
    routes, matchers = _proxied_routes()
    strays = sorted({r.path for r in routes if not r.path.startswith("/api/agent/")})
    assert not strays, f"{matchers} also publishes {strays}"


def test_the_operator_half_is_off_the_vhost_and_still_gated():
    """The routes this split moved, pinned by name.

    Both halves of the fix have to hold. Off the vhost is not a substitute for the
    permission check — an internal hostname is a smaller blast radius, not a closed
    door — so this asserts they kept their gates as well as their new prefix.
    """
    matchers = _proxied_matchers()
    moved = {"/api/agents", "/api/agents/audience", "/api/agents/{agent_id}",
             "/api/agents/{agent_id}/enrollment-code",
             "/api/agents/{agent_id}/discover",
             "/api/agents/{agent_id}/record"}
    by_path = {}
    for r in _api_routes():
        by_path.setdefault(r.path, []).append(r)

    for path in sorted(moved):
        assert path in by_path, f"{path} is not routed — did the operator half move again?"
        assert not any(_matches(m, path) for m in matchers), (
            f"{path} is reachable through {matchers} on the agent vhost")
        for route in by_path[path]:
            gates = _permission_gates(route)
            assert gates, f"{sorted(route.methods)} {path} has no permission gate"
            assert all(g.startswith("agents:") for g in gates), (
                f"{sorted(route.methods)} {path} is gated on {gates}, not agents:*")


def test_the_config_files_do_not_claim_the_old_matcher():
    """The comments are half the deliverable here — the original report was that the
    code was defensible and the prose was not. Both files must name the matcher that is
    actually in force.
    """
    for path in (_CADDYFILE, _COMPOSE):
        text = open(path, encoding="utf-8").read()
        assert "/api/agent/*" in text, (
            f"{os.path.basename(path)} never names the matcher it documents")
        # The no-slash form may still APPEAR — both files explain why it is wrong — but
        # only ever as prose about the trap, never as the directive itself.
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            assert "/api/agent*" not in code, (
                f"{os.path.basename(path)} uses the no-slash matcher in a directive: "
                f"{line.strip()}")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
