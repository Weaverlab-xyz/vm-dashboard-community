"""The agent cell's refusals, and the one that matters most.

The cell installs a non-human principal and mints its authorization. Every mistake it can
catch up front is one that would otherwise surface as a worker that installed fine and
then meant something other than what the demo claims — which is the worst kind, because
the page looks complete either way.

The rot this prevents, in order of how badly it bites:

  * **An agent token minted against an administrator.** Every MCP tool applies the token
    user's RBAC, so the token user IS the agent's blast radius. An admin-scoped agent is
    not a demonstration of scoped non-human access; it is the thing the demo warns about,
    wearing the demo's badge. If this guard ever returns "" for an admin, the cell argues
    the opposite of its point while looking healthy.
  * **A token that never expires.** The model allows `expires_at = None` — that is right
    for a human's CI token and wrong for this. The cell must never be able to mint one.
  * **A worker with nothing to call, or nothing to attest against.** Both install
    cleanly and fail only in a log line, during the demo.
  * **A remedy decaying into a symptom.** These strings are rendered straight into a 400.

Runs under pytest, or standalone:
    python tests/test_agentcell_guards.py
"""
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-guards")

from web_dashboard.services import agentcell_service as A  # noqa: E402


# -- the admin refusal ---------------------------------------------------------

def test_an_administrator_is_refused_as_the_agents_token_user():
    assert A.pat_user_problem(True, "root"), \
        "the cell would mint an agent token against an administrator"


def test_a_narrow_user_is_accepted():
    assert A.pat_user_problem(False, "agent-reader") == ""


def test_the_admin_refusal_explains_the_blast_radius():
    """"That account is an administrator" is a fact. What an SE needs is why it ruins the
    demo — the token user IS what the agent can reach."""
    msg = A.pat_user_problem(True, "root")
    assert "permissions" in msg or "RBAC" in msg, \
        "the admin refusal never says that the token user's permissions bound the agent"
    assert "root" in msg, "the refusal does not name the account it is refusing"


# -- the expiry refusal --------------------------------------------------------

def test_a_token_that_never_expires_cannot_be_minted():
    for never in (0, -1, -24):
        assert A.pat_expiry_problem(never), f"{never}h was accepted as a lifetime"


def test_a_lifetime_longer_than_the_ceiling_is_refused():
    assert A.pat_expiry_problem(A.MAX_PAT_HOURS + 1)
    assert A.pat_expiry_problem(A.MAX_PAT_HOURS) == "", \
        "the ceiling itself is refused; it should be the last accepted value"


def test_a_non_numeric_lifetime_is_refused_rather_than_crashing():
    for junk in (None, "", "eight", object()):
        assert A.pat_expiry_problem(junk), f"{junk!r} did not produce a refusal"


def test_the_expiry_is_always_a_datetime():
    """The model allows None. This cell must never produce one."""
    out = A.pat_expires_at(1)
    assert isinstance(out, datetime), "pat_expires_at returned something that is not a time"
    assert out > datetime.utcnow(), "the expiry is not in the future"


# -- the worker would be meaningless ------------------------------------------

def test_a_worker_with_nothing_to_call_is_refused():
    assert A.mcp_problem(False), "the cell would install a worker with the MCP server off"
    assert A.mcp_problem(True) == ""


def test_the_mcp_refusal_says_where_to_turn_it_on():
    assert "Settings" in A.mcp_problem(False), \
        "the MCP refusal does not say where to enable the server"


def test_a_cell_with_no_trust_domain_is_refused():
    for blank in ("", "   ", None):
        assert A.trust_domain_problem(blank), f"{blank!r} was accepted as a trust domain"
    assert A.trust_domain_problem("weaverlab.test") == ""


def test_the_trust_domain_refusal_explains_what_would_be_lost():
    msg = A.trust_domain_problem("")
    assert "unattested" in msg, \
        "the refusal never says the worker would log `unattested` — which is the point"


def test_a_request_with_no_host_is_refused():
    assert A.host_problem(""), "a cell with no host was accepted"
    assert A.host_problem("agent-host-01") == ""


def test_the_host_refusal_says_the_cell_attaches_rather_than_creates():
    msg = A.host_problem("")
    assert "does not create" in msg or "deployed" in msg, \
        "the host refusal does not convey that the cell attaches to an existing VM"


# -- the identity it will claim ------------------------------------------------

def test_the_spiffe_id_is_derived_from_the_trust_domain():
    assert A.spiffe_id_for("weaverlab.test") == \
        f"spiffe://weaverlab.test{A.AGENT_SPIFFE_PATH}"


def test_the_spiffe_id_tolerates_a_stray_slash():
    assert A.spiffe_id_for("weaverlab.test/") == A.spiffe_id_for("weaverlab.test")


def test_no_trust_domain_yields_no_spiffe_id():
    assert A.spiffe_id_for("") == "", \
        "a blank trust domain produced a SPIFFE ID, which would be a lie in a log line"


# -- the token's name, and wiring ---------------------------------------------

def test_the_token_name_identifies_the_cell_it_belongs_to():
    """Revoking is the demo's closing beat; hunting for which of six tokens to revoke is
    a bad thirty seconds."""
    name = A.pat_name_for("Nightly Reader")
    assert name.startswith("agent-cell-"), f"unexpected token name: {name}"
    assert "nightly" in name


def test_the_token_name_survives_punctuation():
    name = A.pat_name_for("agent #1 (demo)")
    assert " " not in name and "#" not in name and "(" not in name, \
        f"the token name carries characters that make it awkward to match: {name}"


def test_a_cell_is_wired_only_when_both_playbooks_ran():
    class Row:
        stages_done = ""
    r = Row()
    assert not A.is_wired(r)
    r.stages_done = "agent-install"
    assert not A.is_wired(r), \
        "a cell with only the install is wired; its worker would log `unattested`"
    r.stages_done = ",".join(A.STAGES)
    assert A.is_wired(r)


def test_the_notes_state_the_two_credentials_and_the_revoke():
    notes = " ".join(A.deploy_notes()).lower()
    assert "revok" in notes, "the notes never mention revoking, which is the demo"
    assert "spiffe" in notes or "identity" in notes, \
        "the notes never distinguish identity from authorization"


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
