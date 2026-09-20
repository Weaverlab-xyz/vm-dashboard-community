"""A link records accountability. It must never become a consumption.

An agent can be made *answerable for* one Workload Lab credential. That is a governance
record, and the failure mode is that it starts reading — or behaving — like a capability.
It cannot be one: the Cloud tab's credential is returned to nobody, and the Kubernetes
and Certificate tabs vault theirs where a consumer needs a Password Safe client to reach
them. A worker able to fetch any of those would already be holding the standing secret
this whole cell argues against.

So the assertions here are mostly about what the link does NOT do:

  * **No credential crosses into the agent.** Not onto the row, not into the response.
  * **The refusal for an unwired tab names the structural reason**, rather than reading
    as work somebody forgot to finish — an operator who tries `kubernetes` is asking a
    reasonable question.
  * **One link at a time.** An agent answerable for three credentials is the shape the
    cell argues against, so widening is a decision rather than an accumulation.
  * **An expired lease is the mechanism working.** `workload_cloud_service.lease_state`
    exists to keep "expired" and "broken" apart; rendering the first as the second would
    make a correctly-behaving identity look faulty most of the time.

Runs under pytest, or standalone:
    python tests/test_agentcell_link.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-link")

_API = os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")
_DOC = os.path.join(_ROOT, "docs", "profiles", "demo", "agent-demo-cell.md")

from web_dashboard.services import agentcell_service as A  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    src = re.sub(r'"""[\s\S]*?"""', "", _read(path))
    return "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))


# -- what can be linked, and what the refusals say -----------------------------

def test_only_cloud_is_linkable_today():
    assert A.LINKABLE_MECHANISMS == ("cloud",), \
        "the linkable set changed; the refusals below and the docs must change with it"
    assert A.link_problem("cloud") == ""


def test_an_unwired_tab_is_refused_with_the_structural_reason():
    """Not "coming soon". An operator trying this is asking a reasonable question."""
    for tab in ("kubernetes", "certificates"):
        msg = A.link_problem(tab)
        assert msg, f"{tab} was accepted as linkable"
        assert "Password Safe" in msg, \
            f"the {tab} refusal does not say why no worker can reach that credential"


def test_spire_is_refused_because_it_is_already_the_agents_identity():
    msg = A.link_problem("spire")
    assert msg and "SPIFFE" in msg, \
        "the spire refusal does not explain that the agent is already attested"


def test_nonsense_is_refused_and_lists_what_is_linkable():
    msg = A.link_problem("banana")
    assert msg and "cloud" in msg


# -- one at a time -------------------------------------------------------------

def test_an_agent_with_a_link_refuses_a_second():
    class Row:
        linked_mechanism = "cloud"
    msg = A.already_linked_problem(Row())
    assert msg, "an agent was allowed to accumulate a second credential link"
    assert "Unlink" in msg, "the refusal does not say how to proceed deliberately"


def test_an_unlinked_agent_is_accepted():
    class Row:
        linked_mechanism = None
    assert A.already_linked_problem(Row()) == ""


# -- the notes lead with what the link is not ----------------------------------

def test_the_notes_say_the_worker_gets_nothing():
    notes = A.link_notes("aws", revocable=False)
    assert notes, "linking says nothing back"
    first = notes[0].lower()
    assert "does not give" in first or "not give the worker" in first, (
        "the first note does not lead with the fact that the worker gets no credential — "
        "which is the one thing most likely to be misread")


def test_a_non_revocable_cloud_says_so_plainly():
    notes = " ".join(A.link_notes("aws", revocable=False)).lower()
    assert "cannot be revoked" in notes, \
        "an unrevocable lease does not say so; somebody would promise a revoke"
    assert "ttl" in notes, "the note never says what the only remaining control is"


def test_a_revocable_cloud_does_not_overclaim():
    notes = " ".join(A.link_notes("azure", revocable=True)).lower()
    assert "cannot be revoked" not in notes
    assert "not, yet, in the agent" in notes or "not in the agent" in notes, (
        "the revocable note implies the agent's behaviour changes on revoke, which it "
        "does not — nothing consumes the credential")


# -- an expired lease is the mechanism working ---------------------------------

def test_an_expired_lease_does_not_read_as_a_fault():
    out = A.link_summary("cloud", "expired").lower()
    assert "expired" in out
    assert "working" in out, (
        "an expired lease renders as a bare failure. workload_cloud_service.lease_state "
        "exists to keep 'expired' and 'broken' apart, and this is where that is shown")


def test_the_other_lease_states_render():
    assert "live" in A.link_summary("cloud", "live")
    assert A.link_summary("", "live") == "", "an unlinked agent claims a summary"


# -- nothing secret moves ------------------------------------------------------

def test_the_row_gains_no_credential_column():
    block = _read(_DB).split("class AgentCell(Base):", 1)[1].split("\nclass ", 1)[0]
    cols = set(re.findall(r"^\s{4}(\w+)\s*=\s*Column", block, re.M))
    for required in ("linked_mechanism", "linked_credential_id", "linked_at"):
        assert required in cols, f"AgentCell no longer records {required!r}"
    for banned in ("linked_credential", "linked_secret", "lease_value", "credential"):
        assert banned not in cols, f"AgentCell has a {banned!r} column"


def test_the_link_route_never_reads_a_credential_value():
    body = _code(_API).split("def link_agent(", 1)[1].split("def unlink_agent(", 1)[0]
    for banned in ("values", "secret", "credential_value", "get_credential"):
        assert banned not in body, \
            f"the link route touches {banned!r} — it must move no credential"
    assert "lease_state" in body, "the link route does not report the lease's state"


def test_unlinking_touches_neither_side():
    # Bounded at the next def. An unbounded slice runs to end of file and picks up
    # revoke_agent, whose whole job IS to revoke -- the first draft of this test tripped
    # on exactly that.
    body = _code(_API).split("def unlink_agent(", 1)[1].split("\n@router.", 1)[0]
    for banned in ("revoke", "start_revoke", "terminate"):
        assert banned not in body, (
            f"unlinking touches {banned!r}. It should drop the association only — the "
            "lab's row keeps its own lifecycle")


def test_the_api_is_reachable_and_paired():
    import warnings
    warnings.filterwarnings("ignore")
    from web_dashboard.main import app
    paths = {r.path for r in app.routes if "agentcell" in getattr(r, "path", "")}
    assert "/api/agentcell/agent/{agent_id}/link" in paths, "no link route is mounted"


# -- the page says it too ------------------------------------------------------

def test_the_page_states_that_a_link_is_not_a_consumption():
    doc = _read(_DOC)
    assert "not a consumption" in doc, \
        "the cell page does not state that a link gives the worker nothing"
    assert "returned to nobody" in doc, \
        "the page never says why no Workload Lab credential can reach the worker"


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
