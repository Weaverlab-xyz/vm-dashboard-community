"""Four ways an OT cell leaked or lied, and the guards that close them.

Each of these shipped, and each is silent in the same way: the deploy goes green and
the damage surfaces somewhere nobody is looking.

1. **A failed cell's broker was invisible.** The broker deploys FIRST, so the common
   failure is "broker up, cell failed" — and Clear, the only exit a failed cell has,
   looked at the cell's row alone. Clearing retired the card while a VM carrying a
   working Entitle agent kept running and kept billing.
2. **The agent token outlived a failed deploy.** It is minted BEFORE either VM, and the
   destroy runner that collects it only ever runs for a deploy that completed.
3. **A `routing: v0` tenant was accepted.** Those agents pull from ghcr.io and
   gcr.io/datadoghq, which no narrow allow-list can name, so the agent reached
   CrashLoopBackOff inside a subnet with no egress to debug from.
4. **A docker-baked cell could be given the KubeSolo tunnel.** Nothing listens on 6443
   there, and a tunnel to a dead port fails exactly like a blocked firewall — which
   this feature's own docs call the most expensive kind of demo failure.

Two of the four are pure functions, so those are real unit tests rather than source
reads. The other two are structural, because what they assert is an ordering and a
call inside a FastAPI endpoint.

Run: python tests/test_ot_cell_guards.py   (or under pytest)
"""
import ast
import base64
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "ot.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load_ot():
    """ot_service as a PACKAGE module, not a standalone file load.

    tests/test_ot_ports.py loads it by path for isolation, which works there because
    nothing it calls reaches sideways. Two of the functions here do —
    `entitle_agent_endpoint` does `from . import entitle_egress` — and a path load has
    no parent package for that to resolve against. The real import is also what the
    runtime does, which is the behaviour these tests are about.
    """
    from web_dashboard.services import ot_service
    return ot_service


def _fn_body(path, name):
    """One function's CODE, docstring stripped — see tests/test_ot_multicloud_zones."""
    src = _read(path)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


def _token(**fields):
    """A token blob shaped like Entitle's: base64 of JSON (docs/kubesolo.md)."""
    return base64.b64encode(json.dumps(fields).encode()).decode()


# ── 3. the tenant's own account of what it needs ─────────────────────────────

def test_a_v0_tenant_is_refused_with_the_reason():
    ot = _load_ot()
    problem = ot.agent_token_egress_problem(_token(routing="v0", platform="us"))
    assert problem, "a v0 token was accepted"
    # Matched as an anchored pattern with the dots escaped, not as `"ghcr.io" in problem`:
    # a bare hostname substring test is what CodeQL flags as incomplete URL
    # sanitization (py/incomplete-url-substring-sanitization), and escaping the dots is
    # what satisfies its sibling rule for hostname regexes.
    assert re.search(r"\bghcr\.io\b", problem), (
        "the refusal does not say WHY v0 cannot work — the operator needs to know it is "
        "the registry, not the endpoint, so they can ask for the right thing")
    assert "No VM was launched" in problem


def test_a_v1_tenant_in_the_expected_region_passes():
    ot = _load_ot()
    region = ot.entitle_agent_endpoint().split(".")[1]
    assert ot.agent_token_egress_problem(_token(routing="v1", platform=region)) == ""


def test_a_token_from_another_region_is_refused():
    """The quiet one. entitle_egress.region() derives the region from entitle_api_url and
    FALLS BACK to a default when that URL is a proxy or a bare host — so a tenant can be
    on eu while the firewall hole points at agent.us.entitle.io, and nothing says so."""
    ot = _load_ot()
    region = ot.entitle_agent_endpoint().split(".")[1]
    other = "eu" if region != "eu" else "ca"
    problem = ot.agent_token_egress_problem(_token(routing="v1", platform=other))
    assert problem, f"a token for '{other}' was accepted while the hole is drawn for '{region}'"
    assert other in problem and "entitle_api_url" in problem, (
        "the refusal names neither the region it found nor the key that sets it")


def test_an_unreadable_token_is_not_refused():
    """Refusing on a parse failure would make a format change at Entitle's end break
    every deploy. The pre-install probe still catches a path that does not work."""
    ot = _load_ot()
    for bad in ("", "not-base64-at-all!!", base64.b64encode(b"[]").decode(),
                base64.b64encode(b"not json").decode()):
        assert ot.agent_token_egress_problem(bad) == "", f"refused on {bad!r}"


def test_the_token_is_checked_between_the_mint_and_the_first_launch():
    """Order is the whole value. Checked after the VMs, this is a teardown; checked
    before the mint, there is no token to read."""
    body = _fn_body(_OT, "run_cell_deploy")
    mint = body.find("ensure_agent_token")
    check = body.find("agent_token_egress_problem")
    assert mint != -1 and check != -1, "the mint or the check is gone from run_cell_deploy"
    assert mint < check, "the token is checked before it is minted"
    tail = body[check:check + 600]
    assert "destroy_agent_token" in tail, (
        "a refused token is left live in the tenant — nothing else will collect it, "
        "because the destroy runner only runs for a deploy that completed")
    assert "set_cancelled" in tail, "the children are not cancelled on a refusal"


# ── 4. a tunnel needs a listener ─────────────────────────────────────────────

def test_a_docker_cell_may_not_be_given_the_cluster_tunnel():
    ot = _load_ot()
    problem = ot.cell_runtime_problem(["modbus", "kubesolo"], "docker")
    assert problem, "a docker-baked cell was allowed to broker :6443"
    assert "6443" in problem and "No VM was launched" in problem


def test_a_docker_cell_keeps_every_fieldbus_protocol():
    """The guard is about the CLUSTER's endpoints, not about the runtime being lesser —
    docker cells serve every PLC protocol exactly as KubeSolo ones do."""
    ot = _load_ot()
    assert ot.cell_runtime_problem(ot.plc_protocols(), "docker") == ""


def test_the_kubesolo_runtime_may_broker_anything_the_image_serves():
    ot = _load_ot()
    assert ot.cell_runtime_problem(ot.cell_protocols(), "kubesolo") == ""


def test_an_unknown_runtime_is_refused_rather_than_assumed():
    ot = _load_ot()
    assert ot.cell_runtime_problem(["modbus"], "podman")


def test_the_runtime_only_presets_come_from_the_table():
    """Derived, not hardcoded: a platform endpoint added to OT_PORT_PRESETS later is
    covered the day it is added, without anyone remembering this guard exists."""
    ot = _load_ot()
    assert "kubesolo" in ot.runtime_only_presets()
    assert not set(ot.runtime_only_presets()) & set(ot.plc_protocols()), (
        "a fieldbus protocol is being treated as a cluster endpoint")
    body = _fn_body(_OT, "runtime_only_presets")
    assert "OT_PORT_PRESETS" in body, "the list is no longer derived from the table"


def test_every_cell_endpoint_applies_the_runtime_guard():
    src = _read(_API)
    assert src.count("cell_runtime_problem") == 3, (
        f"expected the guard on all three cloud endpoints, found "
        f"{src.count('cell_runtime_problem')}")
    assert src.count('"runtime":           payload.runtime') == 3, (
        "the runtime is not recorded on every cell, so a re-wire could not know it")


# ── 1 + 2. Clear is the only exit a failed cell has ──────────────────────────

def test_clear_refuses_while_the_broker_is_still_running():
    body = _fn_body(_API, "clear_cell")
    assert "ot_broker_job_id" in body, "Clear never looks for the cell's broker"
    # The CALL, not the variable: asserting on the name `broker_alive` passes happily
    # when the probe has been replaced by a constant, which is exactly the shape a
    # careless refactor leaves behind.
    probe = "cell_resource_alive(cloud, broker_meta)"
    assert probe in body, (
        f"the broker's VM is never actually probed ({probe} is missing) — Clear would "
        f"retire the card while the broker keeps running and keeps billing")
    assert "broker_alive is True" in body, (
        "nothing branches on the broker being alive, so the probe's answer is discarded")
    at = body.find(probe)
    assert body.find("update_metadata", at) > at, (
        "the broker is probed after the record is already retired")
    assert "409" in body


def test_clear_retires_the_broker_row_too():
    body = _fn_body(_API, "clear_cell")
    assert body.count("'destroyed': True") >= 2 or body.count('"destroyed": True') >= 2, (
        "only one row is marked destroyed — the broker's card would survive its cell")


def test_clear_destroys_the_plants_agent_token():
    """The token is minted before either VM exists, so a deploy that never completed
    leaves one live in the tenant with nothing pointing at it."""
    body = _fn_body(_API, "clear_cell")
    assert "destroy_agent_token" in body, (
        "Clear leaves the plant's Entitle agent token in the tenant")
    assert "token_note" in body, (
        "a failed token destroy is not reported back — it would vanish silently")


# ── the broker reaps with its cell ───────────────────────────────────────────

def test_the_broker_expires_with_its_cell():
    src = _read(_API)
    assert src.count("expires_at=child.expires_at") == 3, (
        f"expected the broker on all three clouds to inherit the cell's expiry, found "
        f"{src.count('expires_at=child.expires_at')} — on its own clock, whichever "
        f"reaped first would leave the other useless")


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
    print(f"{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
