"""What the plant may honestly say about its one way out, and when that stops being true.

The OT demo cell's whole claim is *one narrow hole*. Three different rules satisfy the
deploy and they are not the same sentence:

  pinned    443/8080 to an address list an operator got from BeyondTrust.
  resolved  443/8080 to whatever the endpoint resolved to at wiring time — honest, but
            a snapshot, because no contractual range is published.
  open      443/8080 to 0.0.0.0/0 via `ot_dmz_egress_open_ports` — materially weaker.

Until this shipped the answer lived in a deploy-job summary that scrolls away and a
firewall-rule description nobody reads, so a demo could say the narrow sentence over
the wide rule and no one would know. And nothing noticed when the addresses moved
underneath a running cell: Re-wire repaired drift correctly, but the first symptom was
an agent that had quietly lost its channel while the card still read "agent installed".

Most of this is real unit tests -- `egress_claim` and `entitle_destination_drift` are
pure apart from two lookups, and those are monkeypatched here so the drift cases can
actually be driven rather than described.

Run: python tests/test_ot_egress_claim.py   (or under pytest)
"""
import ast
import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_OT = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "ot.py")
_PLAY = os.path.join(_ROOT, "examples", "playbooks", "kubesolo",
                     "entitle-agent-install.yml")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _ot():
    from web_dashboard.services import ot_service
    return ot_service


def _fn_body(path, name):
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


class _Patched:
    """Swap module attributes for the duration of a `with` block."""

    def __init__(self, mod, **values):
        self.mod, self.values, self.saved = mod, values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self.mod

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)
        return False


_PINNED = {"ot_entitle_destinations": ["203.0.113.10/32", "203.0.113.11/32"],
           "ot_entitle_destination_source": "ot_entitle_egress_cidrs"}
_RESOLVED = {"ot_entitle_destinations": ["203.0.113.10/32"],
             "ot_entitle_destination_source": "resolved agent.us.entitle.io at 2026-09-19T00:00:00Z"}
_OPEN = {"ot_entitle_destinations": ["0.0.0.0/0"],
         "ot_entitle_destination_source": "ot_dmz_egress_open_ports"}


# ── the claim ────────────────────────────────────────────────────────────────

def test_an_unrecorded_broker_makes_no_claim_at_all():
    """Absent evidence the card says nothing. Defaulting to the flattering answer is
    exactly the failure this whole thing exists to prevent."""
    claim = _ot().egress_claim({})
    assert claim["kind"] == "" and claim["text"] == ""


def test_the_open_hatch_is_named_as_such():
    ot = _ot()
    claim = ot.egress_claim(_OPEN)
    assert claim["kind"] == ot.EGRESS_KIND_OPEN
    assert "ANYWHERE" in claim["text"], (
        "the open-ports claim does not say the destination is unbounded — someone "
        "reading the card would believe the narrow sentence")
    assert "ot_dmz_egress_open_ports" in claim["text"], (
        "the claim does not name the setting that caused it, so nobody can turn it off")


def test_a_pinned_set_is_distinguishable_from_a_resolved_one():
    """The two look identical on the wire and are different promises: one is a firewall
    ticket, the other is a DNS answer with a timestamp on it."""
    ot = _ot()
    pinned, resolved = ot.egress_claim(_PINNED), ot.egress_claim(_RESOLVED)
    assert pinned["kind"] == ot.EGRESS_KIND_PINNED
    assert resolved["kind"] == ot.EGRESS_KIND_RESOLVED
    assert pinned["kind"] != resolved["kind"]
    assert "not a published contract" in resolved["text"], (
        "a resolved set is presented as if it were a contract")


def _records_via_update_metadata(fn_name, *keys):
    """True when `fn_name` passes every key to an update_metadata() call.

    The keys also get poked into the in-memory `bmeta` dict beside the real write, so a
    substring search over the function body passes happily when the PERSISTED write is
    gone -- which is the only half that the card and the drift check ever read. Asserted
    through the AST against the call itself for that reason.
    """
    src = _read(_OT)
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fn_name):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call)
                    and getattr(call.func, "attr", "") == "update_metadata"):
                continue
            written = ast.unparse(call)
            if all(k in written for k in keys):
                return True
        return False
    raise AssertionError(f"{fn_name}() not found")


def test_every_cloud_records_what_the_claim_is_read_from():
    """The card can only be honest if all three wiring paths PERSIST the same two keys.
    GCP was the one that did not -- its provenance lived in a job summary line and a
    firewall-rule description, both of which scroll away."""
    for fn in ("_wire_dmz_firewall", "_wire_zones_aws", "_wire_zones_azure"):
        assert _records_via_update_metadata(
            fn, "ot_entitle_destinations", "ot_entitle_destination_source"), (
            f"{fn} never writes the destination set and its provenance onto a job row — "
            f"the card would have nothing to read and the drift check nothing to compare")


def test_the_cells_api_reads_the_claim_off_the_broker():
    body = _fn_body(_API, "list_cells")
    assert "egress_claim(broker_meta)" in body, (
        "the claim is not read from the BROKER's row — the cell's own row never "
        "carried the destination set, so reading it there would always be empty")
    assert "entitle_destination_drift(broker_meta)" in body


# ── drift ────────────────────────────────────────────────────────────────────

def test_nothing_recorded_never_claims_drift():
    assert _ot().entitle_destination_drift({}) == ""


def test_a_pinned_rule_that_still_matches_is_quiet():
    ot = _ot()
    with _Patched(ot,
                  dmz_egress_open_ports=lambda: False,
                  _current_destinations_cached=lambda: (
                      list(_PINNED["ot_entitle_destinations"]), "ot_entitle_egress_cidrs")):
        assert ot.entitle_destination_drift(_PINNED) == ""


def test_addresses_moving_under_a_running_cell_is_reported():
    ot = _ot()
    with _Patched(ot,
                  dmz_egress_open_ports=lambda: False,
                  _current_destinations_cached=lambda: (["198.51.100.7/32"], "x")):
        note = ot.entitle_destination_drift(_PINNED)
    assert note, "the set changed and nothing said so"
    assert "Re-wire" in note, "the report does not name the remedy"


def test_a_failed_lookup_never_cries_wolf():
    """An unanswerable question is not evidence of drift, and a false alarm at a demo
    costs more than the thing it was warning about."""
    ot = _ot()
    with _Patched(ot,
                  dmz_egress_open_ports=lambda: False,
                  _current_destinations_cached=lambda: ([], "")):
        assert ot.entitle_destination_drift(_PINNED) == ""


def test_the_hatch_being_turned_off_under_an_open_rule_is_drift():
    """The rule is still wide open while the setting says it should not be — the
    dangerous direction, because the card would otherwise keep reporting `open` as if
    that were still deliberate."""
    ot = _ot()
    with _Patched(ot, dmz_egress_open_ports=lambda: False):
        note = ot.entitle_destination_drift(_OPEN)
    assert note and "Re-wire" in note


def test_the_hatch_being_turned_on_under_a_narrow_rule_is_drift():
    ot = _ot()
    with _Patched(ot, dmz_egress_open_ports=lambda: True):
        note = ot.entitle_destination_drift(_PINNED)
    assert note and "Re-wire" in note


def test_an_open_rule_with_the_hatch_still_on_is_quiet():
    ot = _ot()
    with _Patched(ot, dmz_egress_open_ports=lambda: True):
        assert ot.entitle_destination_drift(_OPEN) == ""


def test_the_drift_lookup_is_cached_and_the_wiring_path_is_not():
    """The cells list calls this per request and the answer is a DNS lookup, so it is
    cached. The WIRING path must never be: a rule drawn from a five-minute-old answer
    is the drift this is trying to detect."""
    body = _fn_body(_OT, "entitle_destination_drift")
    assert "_current_destinations_cached" in body
    for fn in ("_wire_dmz_firewall", "_wire_zones_aws", "_wire_zones_azure",
               "_entitle_egress_targets"):
        assert "_current_destinations_cached" not in _fn_body(_OT, fn), (
            f"{fn} draws its rule from the CACHED answer — it must resolve fresh")


# ── the probe, on demand ─────────────────────────────────────────────────────

def _play():
    return yaml.safe_load(_read(_PLAY))[0]


def test_a_probe_only_run_stops_before_anything_touches_the_cluster():
    """What makes it safe to run against a broker with a healthy agent on it."""
    tasks = _play()["tasks"]
    ends = [i for i, t in enumerate(tasks)
            if (t.get("ansible.builtin.meta") == "end_play"
                and "entitle_probe_only" in str(t.get("when", "")))]
    assert len(ends) == 1, f"expected exactly one probe-only end_play, found {len(ends)}"
    installs = [i for i, t in enumerate(tasks)
                if "Install the Entitle agent" in (t.get("name") or "")
                or "Write the values file" in (t.get("name") or "")]
    assert installs, "the install tasks are gone from the play"
    assert ends[0] < min(installs), (
        "the probe-only run ends AFTER work that changes the cluster — it would "
        "reinstall the agent every time someone asked whether the boundary holds")


def test_a_probe_only_run_needs_neither_a_token_nor_a_chart():
    """So it can answer the question on a broker whose agent never installed, which is
    exactly when the answer matters most."""
    tasks = _play()["tasks"]
    validate = next(t for t in tasks if "Validate required vars" in (t.get("name") or ""))
    conditions = " ".join(str(c) for c in validate["ansible.builtin.assert"]["that"])
    assert conditions.count("entitle_probe_only") >= 2, (
        "the token and chart assertions are not both relaxed for a probe-only run")


def test_the_probe_is_declared_off_by_default():
    assert _play()["vars"]["entitle_probe_only"] is False, (
        "probe-only defaults on — every agent install would stop before installing")


def test_the_queued_probe_asks_for_no_credential():
    body = _fn_body(_OT, "queue_egress_probe")
    assert "'entitle_probe_only': True" in body or '"entitle_probe_only": True' in body
    assert "secret_vars={}" in body, (
        "the probe requests a secret it does not need — not asking is the difference "
        "between a check you run freely and one you think twice about")
    assert "agent_token_config_key" not in body


def test_the_probe_refuses_a_cell_with_no_broker():
    body = _fn_body(_OT, "queue_egress_probe")
    assert "ot_broker_job_id" in body and "OTError" in body, (
        "a cell with no broker has no plant boundary to probe, and should be told so "
        "rather than queueing a run against nothing")


def test_the_probe_endpoint_needs_a_deployed_cell_and_write_permission():
    body = _fn_body(_API, "probe_cell_egress")
    assert 'require_permission' in _read(_API)
    assert "queue_egress_probe" in body
    assert "destroyed" in body and "completed" in body, (
        "the endpoint would queue a run against a destroyed or half-deployed cell")


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
