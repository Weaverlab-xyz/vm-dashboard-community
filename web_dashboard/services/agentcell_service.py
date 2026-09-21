"""The agent demo cell: a non-human principal, and the refusals that keep it honest.

**This module mints an authorization and records it. It does not create a VM, and it
does not bridge identity to authorization.** Both of those are deliberate, and both are
the interesting part of the design.

*Attached, never created.* The worker runs on a Linux VM this dashboard already
deployed, resolved through ``spire_lab_service.resolve_host`` — which re-derives the
host from completed deploy-job rows rather than trusting a supplied address, because
privileged playbooks against a host of the caller's choosing is not something this
should accept. The SPIRE lab made exactly this call for exactly this reason, and the
payoff is the same: the host keeps its auto-delete timer, its Password Safe onboarding
and its Destroy button, so this feature owns no teardown beyond revoking what it issued.

*Identity and authorization stay two things.* The worker attests itself to SPIRE and
gets an SVID — that is who it is, and it holds nothing. It calls ``/mcp`` with a
Personal Access Token — that is what it may do here, bounded by the token user's RBAC.
**Nothing mints one from the other**, because ``api/mcp_server`` takes a Bearer PAT and
has no mTLS path; the bridge would need the Password Safe SPIFFE SVID plugin, whose
configuration question ``spire_lab_service``'s own docstring records as unresolved.
Writing that bridge here would be betting on the answer, so the cell shows both halves
and names the gap.

The refusals below are the module's real content. They follow
``ot_service.in_plant_agent_problem``'s shape — a remedy string the route turns into a
400 — because a worker that installs and then cannot attest, or cannot call anything, has
cost a playbook run and a demo.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class AgentCellError(Exception):
    """An invalid agent-cell request. The message is rendered straight into a 400, so it
    carries the remedy rather than the symptom -- the contract ``ot_service.OTCellError``
    states for the failed-job page."""


# The worker's SPIFFE path under the trust domain. A path rather than a bare name so the
# ID reads as what it is in a log line, and fixed rather than configurable so the
# registration entry the playbook creates and the ID the worker expects cannot drift.
AGENT_SPIFFE_PATH = "/agent/mcp-reader"

# The PAT's lifetime, and the reason it is not optional. `expires_at` is nullable on the
# model (None = never expires, for a human's CI token), and a non-expiring token for a
# non-human principal is precisely what this cell argues against -- so the cell always
# sets one and `pat_expiry_problem` refuses to let it be turned off.
DEFAULT_PAT_HOURS = 8
MAX_PAT_HOURS = 72

# The two playbooks, in the order a cell runs them.
STAGES = ("agent-spiffe-entry", "agent-install")


def spiffe_id_for(trust_domain: str) -> str:
    """The SPIFFE ID this cell's worker will attest as."""
    td = (trust_domain or "").strip().strip("/")
    if not td:
        return ""
    return f"spiffe://{td}{AGENT_SPIFFE_PATH}"


def mcp_problem(mcp_enabled: bool) -> str:
    """Refuse a worker that would have nothing to call.

    The whole loop is one MCP tool call. With the server off, the install succeeds, the
    unit starts, and every poll fails against a 404 -- which reads as a broken worker
    rather than as a feature that was never turned on.
    """
    if mcp_enabled:
        return ""
    return ("The MCP server is off, so the worker would have nothing to call — every "
            "poll would 404 and read as a broken agent. Turn on **MCP Server** under "
            "Settings → Integrations first.")


def trust_domain_problem(trust_domain: str) -> str:
    """Refuse a cell with no trust domain to attest against.

    Not fatal to the worker -- it logs ``unattested`` and keeps polling -- but it is
    fatal to the demo, because the identity half becomes a claim rather than a fact. So
    it is refused at the door rather than discovered in a log line in front of an
    audience.
    """
    if (trust_domain or "").strip():
        return ""
    return ("No SPIRE trust domain. The worker would run, but it could not attest, so it "
            "would log `unattested` and the identity half of the demo would be an "
            "assertion. Stand up a SPIRE lab on this host first — Workload Lab → SPIRE.")


def pat_expiry_problem(hours) -> str:
    """Refuse a token that outlives the demo, or never expires at all.

    The model allows ``expires_at = None``; this cell does not. That is the single
    strongest claim it makes -- a non-human principal whose authorization has no end is
    the status quo being argued against, and shipping a cell that could create one would
    undercut every card pointing at it.
    """
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return (f"{hours!r} is not a number of hours. The agent's token must expire; "
                f"pick something between 1 and {MAX_PAT_HOURS}.")
    if h < 1:
        return ("The agent's token must expire, and a non-positive lifetime would mean "
                "never. That is the arrangement this cell exists to argue against — "
                f"pick between 1 and {MAX_PAT_HOURS} hours.")
    if h > MAX_PAT_HOURS:
        return (f"{h} hours is longer than this cell will issue ({MAX_PAT_HOURS}). A "
                "demo credential that outlives the demo becomes a standing one; shorten "
                "it, or mint a fresh cell when you next present.")
    return ""


def pat_user_problem(is_admin: bool, username: str = "") -> str:
    """Refuse to mint an agent token for an administrator.

    **The refusal that matters most in this module.** An agent holding administrator is
    not a demonstration of scoped non-human access; it is the thing the demo warns about,
    wearing the demo's own badge. Every MCP tool resolves the token's user and applies
    that user's RBAC (``api/mcp_server._caller``), so the token user IS the agent's
    blast radius -- which makes picking an admin the one choice that silently makes the
    whole cell say the opposite of what it means to.
    """
    if not is_admin:
        return ""
    who = f" ({username})" if username else ""
    return (f"That account is an administrator{who}. Every MCP tool applies the token "
            "user's own permissions, so an agent minted against it would read the entire "
            "estate — which is the arrangement this cell argues against. Create a narrow "
            "user for the agent and mint the token against that.")


def host_problem(host_ref: str) -> str:
    """Refuse a request with no host to attach to.

    Only the shallow check lives here. The real one is
    ``spire_lab_service.resolve_host``, which re-derives the host against this
    dashboard's own deploy rows -- a name that looks fine here and resolves to nothing
    there is the case that matters, and it is that function's to answer.
    """
    if (host_ref or "").strip():
        return ""
    return ("No host. The worker attaches to a Linux VM this dashboard deployed — it "
            "does not create one — so name the VM running your SPIRE agent.")


def pat_expires_at(hours: int = DEFAULT_PAT_HOURS, now=None) -> datetime:
    """When the agent's token dies. Always a datetime, never None."""
    base = now or datetime.utcnow()
    return base + timedelta(hours=int(hours))


def pat_name_for(cell_name: str) -> str:
    """A token name that says what it belongs to, so the row in Settings → API Tokens is
    identifiable without opening anything. Revoking is the demo's closing beat, and
    hunting for which of six tokens to revoke is a bad thirty seconds."""
    slug = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in (cell_name or "agent").strip().lower())[:60]
    return f"agent-cell-{slug or 'agent'}"


# ── The Workload Lab link ─────────────────────────────────────────────────────
# Which tabs an agent can be made ANSWERABLE FOR. Only `cloud` is wired, and the reason
# the other three are absent is structural rather than unfinished work:
#
#   * the **Cloud** tab's credential "is returned to nobody" (workload_cloud_service);
#   * the **Kubernetes** tab vaults its token where "the consumer is a program with a
#     Password Safe API client" (workload_k8s_service);
#   * the **Certificates** tab writes a PKCS#12 into Secrets Safe on the same principle.
#
# THAT REASONING HELD UNTIL THE WORKER BECAME THAT PROGRAM. An earlier version of this
# comment concluded that a worker "cannot SPEND any of them without already holding a
# credential to fetch the credential", and that closing the gap needed the SPIFFE bridge
# the cell names as unbuilt. It did not: the worker now reaches Password Safe with a
# workload identity brokered by Workload Credentials, holding nothing
# (`--token-source ps`, see examples/playbooks/agent/files/mcp_agent.py). So the
# Kubernetes tab's own sentence -- "the consumer is a program with a Password Safe API
# client" -- describes this worker, and `kubernetes` is linkable because the agent can
# genuinely request that token.
#
# `certificates` followed, and the refusal it used to carry was imprecise in a way worth
# recording: it said the tab "writes a PKCS#12 into Secrets Safe rather than a
# managed-account password". Half of that identity IS a managed-account password -- the
# PKCS#12 passphrase -- and the worker could always reach it. The gap was the BUNDLE
# alone, a Secrets Safe file secret. Secrets Safe is part of Password Safe, so the same
# client pair opens it; the worker now reads it with `ps-cli`, which is the path this
# repo already runs against a live tenant.
#
# `cloud` remains linkable but NOT spendable -- its credential is returned to nobody by
# design, so the link records a lease whose STATE is worth reporting beside the agent and
# nothing more. See `link_notes`.
#
# See docs/design/next-demo-cells.md sections 5b, 5d and 5e.
LINKABLE_MECHANISMS = ("cloud", "kubernetes", "certificates")

# Mechanisms a worker can actually SPEND, as opposed to merely be answerable for. The
# distinction is load-bearing: a link that confers capability and one that confers only
# accountability must not read the same way back to an operator.
SPENDABLE_MECHANISMS = ("kubernetes", "certificates")

# Named here so the refusal can list them without claiming they are coming.
_UNWIRED_MECHANISMS = ("spire",)


def link_problem(mechanism: str) -> str:
    """Refuse a link to a mechanism this cell cannot report on.

    The refusal distinguishes "not a tab" from "a tab whose credential no worker can
    reach", because those are different answers and the second one is the interesting
    one -- an operator who tries it is asking a reasonable question and deserves the
    structural reason rather than a validation error.
    """
    m = (mechanism or "").strip().lower()
    if m in LINKABLE_MECHANISMS:
        return ""
    if m == "spire":
        return ("The agent is already attested by SPIRE — that link is its SPIFFE ID, "
                "recorded when the cell was created, and it does not need a second one.")
    return (f"{mechanism!r} is not a Workload Lab mechanism. Linkable today: "
            f"{', '.join(LINKABLE_MECHANISMS)}.")


def already_linked_problem(row) -> str:
    """One link at a time.

    The constraint §5b records, and the reason it exists: an agent answerable for a
    cloud lease AND a cluster token AND a certificate would be the most
    over-credentialed principal in the estate, which is the arrangement this cell exists
    to argue against. Unlink before relinking, deliberately, so the widening is a
    decision somebody makes rather than an accumulation.
    """
    current = (getattr(row, "linked_mechanism", "") or "").strip()
    if not current:
        return ""
    return (f"This agent is already answerable for its {current} credential. Unlink that "
            "first — an agent accumulating credentials is the shape this demo argues "
            "against, so widening one is a decision rather than a default.")


def link_notes(cloud: str, revocable: bool) -> list:
    """What to say back when an agent is linked to a cloud credential.

    Leads with what the link is NOT, because the honest risk here is that a governance
    record reads as a capability. Then the revoke asymmetry, surfaced at link time rather
    than discovered when somebody tries to revoke in front of an audience.
    """
    notes = [
        "This records what the agent is answerable for. **It does not give the worker "
        "the credential** — the Cloud tab's credential is returned to nobody, by design, "
        "so nothing here can spend it.",
    ]
    if revocable:
        notes.append(
            f"{cloud} leases can be released early, so revoking one is observable in the "
            "lab's own record — though not, yet, in the agent's behaviour.")
    else:
        notes.append(
            f"{cloud} leases **cannot be revoked at all** — the TTL is the only control "
            "there is. That is the provider's limit, not this dashboard's, and it is "
            "worth saying out loud before somebody promises a revoke.")
    return notes


def k8s_link_notes(profile: str, account_name: str, namespace: str) -> list:
    """What to say back when an agent is linked to a Kubernetes token.

    The opposite risk to `link_notes`. There the danger is a governance record reading as
    a capability; here it IS a capability, so the danger is an operator assuming the
    approval gates more than it does. Both limits come straight from
    ``workload_k8s_service``'s own docstring rather than being softened on the way out.
    """
    where = f" in {namespace}" if namespace else ""
    return [
        f"This agent can now **request** the `{account_name or profile}` token"
        f"{where} — it holds nothing until Password Safe releases one, and every "
        "retrieval is a recorded request with a duration and a reason.",
        "**The approval gates retrieval, not use.** In bound mode rotation does not "
        "revoke: a token the agent has already been given lives out its TTL whatever "
        "happens next. Deleting the ServiceAccount is the only hard kill.",
        "**The vault still cannot tell who retrieved.** The agent holds no standing "
        "credential to ask with, so what reaches Password Safe is not transferable — "
        "but anyone who can retrieve is the workload, as far as this mechanism can "
        "tell. That is the axis the SPIRE path wins on and this one does not.",
    ]


# ── The cluster-access episode ────────────────────────────────────────────────
#
# One bounded request: ask, wait for whatever the access policy requires, probe, release.
# The states are named rather than boolean because "waiting" and "denied" are different
# answers an operator needs to tell apart, and because WAITING IS THE DEMO -- an agent
# that cannot authorise its own access to a cluster is the argument, so the state that
# says so has to be visible rather than inferred from an absence.
# WHICH OF THESE THE ROW ACTUALLY HOLDS, because the split is not obvious and pretending
# otherwise would make the row look live when it is not.
#
# The dashboard writes two: an operator opens an episode, and an operator (or the worker's
# operator) closes it. Everything between happens on the host, and **the worker has no way
# to report it back** -- its PAT belongs to a non-admin user by construction, and these
# endpoints need `config_mgmt:write`. Giving the worker that authority to file status
# updates would hand a non-human principal more than the cell argues it should have, which
# is a bad trade for a progress bar.
#
# So the journal is the truth for what happened, and the row answers only "is something
# out right now". The worker states are named here because `episode_summary` renders them
# when one is quoted back -- not because the row will hold them.
ROW_STATES = ("requested", "released")
WORKER_STATES = ("waiting", "approved", "probed", "denied", "expired")
EPISODE_STATES = ROW_STATES + WORKER_STATES
# Terminal states: the slot is back and a new episode may start.
EPISODE_CLOSED = ("released", "denied", "expired")

# How long an episode may sit waiting before it gives the slot back. An abandoned request
# holds the account's concurrent-request slot for its WHOLE duration, so the next attempt
# fails on the cap (Password Safe code 4035) reporting a cap problem instead of the
# approval it was actually waiting on -- the confusion `ps_api_service._request_credential`
# documents. Giving up is therefore part of the design, not a timeout bolted on.
MAX_WAIT_MINUTES = 30
DEFAULT_DURATION_MINUTES = 15


def episode_problem(row) -> str:
    """Refuse a second episode while one is open.

    Same reasoning as `already_linked_problem` one level down: an agent holding two open
    requests against the same account trips the concurrent cap, and the failure arrives
    as a cap error on the *next* attempt rather than here where the cause is legible.
    """
    state = (getattr(row, "episode_state", "") or "").strip()
    if not state or state in EPISODE_CLOSED:
        return ""
    return (f"This agent already has a cluster-access request open ({state}). Let it "
            "finish or release it first — two open requests against one account trip "
            "Password Safe's concurrent-request cap, and that failure reports the cap "
            "rather than the reason.")


def episode_link_problem(row) -> str:
    """Refuse an episode on an agent linked to nothing, or to something unspendable."""
    mechanism = (getattr(row, "linked_mechanism", "") or "").strip().lower()
    if not mechanism:
        return ("This agent is not linked to a Workload Lab credential. Link it to a "
                "Kubernetes token first — the link is what says which identity it may "
                "ask for.")
    if mechanism not in SPENDABLE_MECHANISMS:
        return (f"This agent is answerable for a {mechanism} credential, which no worker "
                "can spend — that tab returns its credential to nobody. Only "
                f"{', '.join(SPENDABLE_MECHANISMS)} can be requested.")
    return ""


def episode_duration_problem(minutes) -> int:
    """Clamp the request duration, returning the value to use.

    Not a refusal: a duration outside the sane band is a slider in the wrong place rather
    than an error worth stopping for. But it is clamped rather than honoured, because the
    duration is how long the slot stays held if nobody approves.
    """
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_DURATION_MINUTES
    return max(5, min(m, MAX_WAIT_MINUTES * 2))


def episode_reason(agent_name: str, spiffe_id: str = "") -> str:
    """What Password Safe records as the reason for the request.

    Names the agent AND its SPIFFE ID, because the audit row is the one place a human
    approving this can see WHAT is asking. A reason of "automated" would make the
    approval a rubber stamp, which is the opposite of the point.
    """
    who = (spiffe_id or "").strip() or (agent_name or "agent")
    return f"mcp-agent cluster access — {who}"


def episode_summary(row) -> str:
    """One clause for the agent's row, honouring what each state actually means."""
    state = (getattr(row, "episode_state", "") or "").strip()
    if not state:
        return "no cluster-access request"
    if state == "waiting":
        return "waiting for approval — the agent cannot authorise its own access"
    if state == "denied":
        return "request denied — the mechanism working, not a fault"
    if state == "expired":
        return "request expired unapproved — the slot was given back"
    if state == "released":
        return "access released — the request was checked back in"
    return f"cluster access: {state}"


def cert_link_notes(account_name: str, bundle_title: str, cn: str = "") -> list:
    """What to say back when an agent is linked to a certificate identity.

    The third control surface, and the one whose limit is easiest to assume away. The
    other two links warn about what the agent *can* do; this one warns about what taking
    it away *cannot* do.
    """
    who = cn or account_name or "this identity"
    return [
        f"This agent can now request **both halves** of `{who}` — the PKCS#12 "
        f"passphrase from the managed account, and the bundle from Secrets Safe "
        f"(`{bundle_title}`). Neither is usable without the other, which is the point "
        "of the split.",
        "**Revoking this certificate will not stop the agent.** The consumer checks "
        "neither CRL nor OCSP — `docs/workload-lab/certificates.md` states that as a "
        "deliberate design position, with short lifetimes as the mitigation. The agent "
        "stops when the certificate **expires**, not when somebody takes it away.",
        "That is the opposite of the PAT, and deliberately so. Say it out loud: this is "
        "the shape that shows why a revocable credential and an approval gate are worth "
        "having.",
    ]


def link_summary(mechanism: str, lease_state: str) -> str:
    """One clause for the agent's row. `lease_state` comes from
    ``workload_cloud_service.lease_state``, whose docstring is worth honouring here:
    **an expired lease is the mechanism working**, so it must not render as a fault."""
    if not mechanism:
        return ""
    if lease_state == "expired":
        return f"{mechanism}: credential expired — the mechanism working, not a fault"
    if lease_state == "live":
        return f"{mechanism}: credential live"
    return f"{mechanism}: no credential issued yet"


def stages_done(row) -> list:
    return [s for s in ((getattr(row, "stages_done", "") or "").split(",")) if s]


def is_wired(row) -> bool:
    """Both playbooks ran. A cell with only the entry has an identity nothing uses; one
    with only the install has a worker that logs ``unattested``."""
    return set(STAGES).issubset(set(stages_done(row)))


def deploy_notes(hours: int = DEFAULT_PAT_HOURS) -> list:
    """What the form says back, so the shape of the demo is read before it is run."""
    return [
        f"The agent's token expires in {hours}h and can be revoked at any time from "
        "Settings → API Tokens. Revoking it mid-run is the demo.",
        "Its identity (a SPIFFE SVID) and its authorization (this token) are two "
        "separate things — the SVID does not authenticate to /mcp. The worker names both "
        "in every log line so the gap stays visible.",
        "The worker attaches to a VM you already deployed. Destroying that VM reaps the "
        "worker with it; this cell adds no teardown of its own beyond revoking the token.",
    ]
