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
# So a worker cannot SPEND any of them without already holding a credential to fetch the
# credential -- which is the standing secret this whole cell argues against. Closing that
# needs an independent trust path, which is the SPIFFE bridge the agent cell already
# names as unbuilt. See docs/design/next-demo-cells.md section 5b.
#
# `cloud` is wired not because the worker can use it, but because its row carries a lease
# whose STATE is worth reporting beside the agent -- see `link_notes`.
LINKABLE_MECHANISMS = ("cloud",)

# Named here so the refusal can list them without claiming they are coming.
_UNWIRED_MECHANISMS = ("kubernetes", "certificates", "spire")


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
    if m in _UNWIRED_MECHANISMS:
        return (f"The {m} tab vaults its credential where a consumer needs a Password "
                "Safe client to reach it — another credential — so nothing here could "
                "report on it honestly. Only 'cloud' can be linked today; see "
                "docs/design/next-demo-cells.md §5b for what would have to exist first.")
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
