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

def test_linkable_and_spendable_are_different_sets():
    """The distinction this change introduces, and the one that must not blur.

    `cloud` is linkable and NOT spendable — that tab returns its credential to nobody, so
    the link is accountability. `kubernetes` is both: the worker reaches Password Safe
    holding nothing, so it can genuinely request that token. A link that confers
    capability and one that confers only accountability must not read the same way back.
    """
    assert A.LINKABLE_MECHANISMS == ("cloud", "kubernetes"), \
        "the linkable set changed; the refusals below and the docs must change with it"
    assert A.SPENDABLE_MECHANISMS == ("kubernetes",)
    assert "cloud" not in A.SPENDABLE_MECHANISMS, \
        "cloud became spendable — that tab's credential is returned to nobody"
    for tab in A.LINKABLE_MECHANISMS:
        assert A.link_problem(tab) == ""


def test_the_stale_unreachable_reasoning_is_gone():
    """The refusal used to tell every unwired tab that a consumer "needs a Password Safe
    client to reach it — another credential". That stopped being true when the worker got
    `--token-source ps`: it reaches Password Safe holding nothing. A refusal repeating it
    would send an operator looking for a barrier that was removed two PRs ago."""
    for src in (A.link_problem("certificates"), A.link_problem("banana")):
        assert "another credential" not in src, \
            "a refusal still claims the worker would need a second credential"
    # The superseded conclusion may still APPEAR — the comment quotes it to record that
    # it was reversed, and that record is worth keeping. What it must never do is stand
    # alone as current. So wherever the phrase is, the correction has to be with it.
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "agentcell_service.py"))
    if "cannot SPEND any of them" in svc:
        para = svc[max(0, svc.index("cannot SPEND any of them") - 600):
                   svc.index("cannot SPEND any of them") + 600]
        assert "It did not" in para or "earlier version" in para.lower(), \
            ("the service states the superseded conclusion with nothing marking it as "
             "superseded — an operator reading it would believe it")


def test_certificates_is_refused_for_the_reason_that_is_actually_true():
    """A different retrieval path, not a structural barrier. PKCS#12 into Secrets Safe is
    not a managed-account password, and this worker has only the second."""
    msg = A.link_problem("certificates")
    assert msg, "certificates was accepted as linkable"
    assert "PKCS#12" in msg or "Secrets Safe" in msg, \
        "the certificates refusal does not name the real difference"


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

def test_the_page_distinguishes_accountability_from_capability():
    """This replaces an assertion that the page said a link "is not a consumption".
    That was true of every tab once and is now true of only one, so the flat claim had
    to go — but the distinction it protected matters MORE now, not less: a `cloud` link
    that read as capability, or a `kubernetes` link that read as a mere record, would
    both mislead."""
    doc = _read(_DOC)
    assert "accountability only" in doc and "a capability" in doc, \
        "the page does not distinguish a link that confers access from one that does not"
    assert "returned to nobody" in doc, \
        "the page never says why the cloud tab's credential cannot reach the worker"
    assert "cannot authorise its own access" in doc, \
        "the page does not state the beat the kubernetes link exists for"


def test_the_page_states_what_the_approval_does_not_gate():
    """The limit somebody will assume away. The approval gates RETRIEVAL; a token already
    released lives out its TTL, because rotation does not revoke."""
    doc = _read(_DOC)
    assert "gates retrieval, not use" in doc
    assert "rotation does not revoke" in doc.lower()
    assert "ServiceAccount" in doc, \
        "the page does not name the only hard kill switch"


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
