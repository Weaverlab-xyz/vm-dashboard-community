"""Published runbooks a POV can be run against, as tickable checklists.

The fourth axis, and the one that is not about this dashboard at all.

``install_profile`` gates, ``personas`` curates by role, and ``pov_use_cases`` answers
"can I run this on THIS POV?". None of them answers the question an SE working from a
**published procedure** has in front of them: *where am I in the document?* A POV is run
against a real runbook over several sessions — BeyondTrust's own Skytap Password Safe POC
step-by-step is 75 pages and twenty use cases — and the thing an SE loses track of is
which of those twenty they have actually shown.

So a runbook is a group of cards like a persona is, and deliberately **not** a persona:

  * A persona is a **role** — it presets feature flags, orders dashboard tiles, pins nav
    items, and appears in the setup wizard and the lens picker. "Password Safe POC
    runbook" is not a job title, and putting it there would offer an SE a role they are
    not, in four places that have nothing to do with a POV.
  * A persona also owes ``docs/personas/<key>.md`` a section per demo card, and demo cards
    target the demo estate — pages a POV instance masks. A runbook has no demo half.

What it DOES share is the card shape and every consumer: :class:`personas.UseCase` and
:func:`personas.describe_pov_card` are reused verbatim, so a runbook card renders,
resolves its product mix, withholds its target when out of scope, and records progress
through exactly the code a persona card does.

Two properties carried over from ``pov_use_cases``, for the same reasons:

  * **It never subtracts.** Every card is present for every product mix; a card whose
    product this POV does not include renders ``out_of_scope``. So the checklist always
    mirrors the document — an SE who cannot find use case 15 in the UI would go looking
    for it in the runbook, and finding it marked out of scope is the answer.
  * **The registry is the allowlist for writes.** :func:`find_card` is what
    ``pov_use_cases.set_state`` consults, so an unknown id is refused rather than stored.

**The cards mirror the runbook, including the parts the runbook has not finished.** Seven
of its twenty use cases are unwritten, unQA'd, or personal notes, and their card says so.
That is more useful than omitting them: a card that reads "the runbook's own steps for
this are incomplete" stops an SE promising a demo they cannot give, where a missing card
just reads as a gap in this dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import personas
from .personas import UseCase


@dataclass(frozen=True)
class Runbook:
    """One published procedure, and the use cases it teaches.

    ``source`` is where the document actually lives, spelled out rather than linked from
    the copy: these runbooks are internal Confluence pages whose attachments move, and an
    SE who cannot find the page needs the space and page id, not a dead hyperlink.
    """
    key: str
    label: str
    blurb: str
    source: str
    docs: tuple = ()
    use_cases: tuple = ()


# ── BeyondTrust's Skytap Password Safe Cloud POC ──────────────────────────────
#
# Confluence SELab page 870514897, rev 7.0 (2026-09-02), validated by its author against
# PWS SaaS 26.2.0.1427. The card list is that document's own numbering, so an SE can work
# down the page and tick as they go -- 11A and 11B are separate cards because the runbook
# separates them, and 8/9/10 stay separate for the same reason even though they are one
# pattern shown three times.
#
# Every card is a thing DEMONSTRATED to a customer. The runbook's setup steps 1-11 are not
# cards: they are a precondition for all twenty, they are done once, and an SE who has not
# finished them has nothing to tick.

_PS_POC_SKYTAP = Runbook(
    key="ps-poc-skytap",
    label="Password Safe Cloud POC (Skytap)",
    blurb="BeyondTrust's own step-by-step, use case by use case — so a POC that runs "
          "across several sessions and more than one person knows where it got to.",
    source="Confluence SELab page 870514897, rev 7.0 — "
           "“Using Skytap for a Password Safe Cloud POC: Step-by-Step”",
    docs=("pov-ps-runbook",),
    use_cases=(
        UseCase(
            id="pspoc-uc1-onboard-systems",
            title="UC1 · Automatic onboarding of systems",
            summary="Asset Smart Rules take the discovered hosts under management on their "
                    "own — the demo the whole runbook builds towards, and the reason "
                    "step 10's authenticated scan gathers so much.",
            target="#wired",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc2-shared-windows-domain",
            title="UC2 · Shared Windows domain accounts",
            summary="Managed Account Smart Rules bring the SharedAdmin domain accounts "
                    "under management and grant them to the requestor group, then a "
                    "brokered RDP session proves the credential is injected, never seen.",
            target="#wired",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc3-monitor-live",
            title="UC3 · Monitoring live sessions",
            summary="An approver watches the live session, locks it, unlocks it, and "
                    "terminates it. Narrated in the Password Safe console — there is "
                    "nothing to provision, and it runs off UC2's still-open session.",
            target="#wired",
            minutes=10,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc4-review-recorded",
            title="UC4 · Reviewing recorded sessions",
            summary="Playback, events, event search and Mark as Reviewed on the session "
                    "UC3 just terminated. Console narration, no setup.",
            target="#wired",
            minutes=10,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc5-linux-shared-admin",
            title="UC5 · Linux shared admin accounts",
            summary="The same shape as UC2 on the Linux guests, including the first "
                    "rotation of a password nobody in the room knows.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc6-windows-dedicated",
            title="UC6 · Windows 1:1 mapped dedicated admin accounts",
            summary="Each admin gets their own account, mapped to their own identity by "
                    "Smart Rule rather than by hand — the answer to “how does this scale”.",
            target="#wired",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc7-linux-dedicated",
            title="UC7 · Linux 1:1 mapped dedicated admin accounts",
            summary="UC6 on the Linux guests. Worth running both: the mapping rule differs, "
                    "and a customer with a mixed estate asks about exactly that.",
            target="#wired",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc8-windows-domain-admin-approval",
            title="UC8 · Windows domain admin, approval required",
            summary="The same account under the 24x7-Manual access policy, so a request "
                    "waits for an approver. Needs the requestor/approver group split from "
                    "the runbook's step 11.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc9-windows-local-admin-approval",
            title="UC9 · Windows local admin, approval required",
            summary="The local Administrator on a member server, approval-gated. The "
                    "account a customer is usually most nervous about.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc10-linux-root-approval",
            title="UC10 · Linux root, approval required",
            summary="root under approval, rotated on check-in. The Linux counterpart to "
                    "UC9 and the one that closes the “what about root” question.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc11a-service-accounts",
            title="UC11A · Windows service accounts (FileZilla)",
            summary="Rotate the credential a Windows service runs under and let Password "
                    "Safe restart the service, so a rotation does not take the app down. "
                    "The discovery scan already knows which services run as what.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc11b-scheduled-tasks",
            title="UC11B · Windows scheduled-task credentials",
            summary="The same for a scheduled task's stored credential — the other half of "
                    "what the scan enumerated, and the pair customers most often say they "
                    "cannot rotate today.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc12-ssms-app-session",
            title="UC12 · Application session via SQL Server Management Studio",
            summary="A published application session into SSMS with the credential injected. "
                    "**The runbook is incomplete here** — it needs SQL Server and SSMS "
                    "installed on App01, which its own step 3 leaves unwritten. Confirm "
                    "before promising it.",
            target="#vms",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc13-web-application",
            title="UC13 · Web application (AWS or Gmail)",
            summary="Credential injection into a browser session through the AutoIT "
                    "Universal App. **Not QA'd by the runbook since 2022**, and it needs "
                    "shared AWS and Gmail accounts that do not exist. Treat as unproven.",
            target="#vms",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc14-bring-your-own-tool",
            title="UC14 · Bring your own tool (MobaXterm)",
            summary="A customer's own SSH client driven by a Direct Connect shortcut. "
                    "**The runbook never wrote this one up** — PuTTY ships on the "
                    "workstation, so it is demonstrable, just not scripted.",
            target="#vms",
            minutes=10,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc15-dr-pws-cache",
            title="UC15 · Disaster recovery via Password Safe Cache",
            summary="Break-glass checkout when the tenant is unreachable. **Personal notes "
                    "only in the runbook**, and it needs a Cache01 guest that is not in "
                    "template Part 1 — so the lab cannot show it as shipped.",
            target="#vms",
            minutes=25,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc16-api-resource-kit",
            title="UC16 · API access via the Password Safe Resource Kit",
            summary="A scripted checkout against an API registration. **Unwritten in the "
                    "runbook**, which notes it overlaps UC15's API setup — do that one "
                    "first and this is mostly done.",
            target="#wired",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc17-workforce-passwords",
            title="UC17 · Workforce Passwords",
            summary="A requestor stores their own web credentials in Secrets Safe and logs "
                    "in through the browser extension. **Outline only in the runbook**, "
                    "though it is short: grant the feature, then demonstrate.",
            target="#access",
            minutes=15,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc18-smart-rule-scale",
            title="UC18 · Smart Rules at scale — new servers and users, no rule changes",
            summary="Add the Part 2 guests, run SetupMoreUsers.ps1 on the DC, re-scan, and "
                    "watch the existing rules onboard everything with nothing edited. The "
                    "runbook's strongest demo, and the one that answers “does this scale”.",
            target="#vms",
            minutes=25,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc19-ssh-key-rotation",
            title="UC19 · SSH key management and rotation",
            summary="Password Safe managing and rotating an SSH key rather than a password. "
                    "**The runbook points at another repository instead of documenting it**, "
                    "and it needs a key-based Linux account seeded in the template.",
            target="#wired",
            minutes=20,
            requires_products=("password_safe",),
        ),
        UseCase(
            id="pspoc-uc20-pws-pra-integration",
            title="UC20 · Password Safe / PRA integration",
            summary="Password Safe accounts discovered inside PRA, so a user works from the "
                    "PRA console and off VPN. New in rev 7.0 and **marked work-in-progress "
                    "by its author**; it needs Builder to enable tunnel and web jump, a "
                    "Gateway on the Windows broker, and Jump Clients on the guests — none "
                    "of which is what this dashboard's jump-item wire-up does.",
            target="#wired",
            minutes=45,
            requires_products=("pra", "password_safe"),
        ),
    ),
)


VALID_RUNBOOKS = ("ps-poc-skytap",)

_RUNBOOKS = {r.key: r for r in (_PS_POC_SKYTAP,)}


def all_runbooks() -> tuple:
    """Every runbook, in declaration order."""
    return tuple(_RUNBOOKS[k] for k in VALID_RUNBOOKS)


def get(key: str) -> Runbook | None:
    return _RUNBOOKS.get((key or "").strip())


# ── the API shape ────────────────────────────────────────────────────────────
#
# Identical to `personas.describe_pov`, including the group's identity living under a key
# called `persona`. That name is reused deliberately rather than added alongside: the POV
# detail page keys its group loop on it, `pov_summary` reads it, and
# `PovUseCaseProgress.persona` stores it -- so the field means "which GROUP this card came
# from", and a second name for the same thing would be three consumers to teach.

def describe(key: str, env_id: str, products: dict) -> dict:
    """One runbook's cards for one POV. Unknown key yields an empty group, never None."""
    runbook = get(key)
    if runbook is None:
        return {"persona": "", "label": "", "blurb": "", "docs": [], "use_cases": []}
    return {
        "persona": runbook.key,
        "label": runbook.label,
        # The source goes in the blurb rather than a field of its own: every consumer
        # already renders a blurb, and "which document is this" is the first thing an SE
        # opening somebody else's POV wants to know.
        "blurb": f"{runbook.blurb} Source: {runbook.source}.",
        "docs": [f"/docs/{d}" for d in runbook.docs],
        "use_cases": [personas.describe_pov_card(c, env_id, products)
                      for c in runbook.use_cases],
    }


def catalog(env_id: str, products: dict) -> list:
    """Every runbook's cards for one POV, in declaration order."""
    return [describe(r.key, env_id, products) for r in all_runbooks()]


def find_card(card_id: str) -> tuple:
    """``(runbook_key, UseCase)`` for a runbook card id, or ``("", None)``.

    The write allowlist, and the reason ``pov_use_cases.set_state`` consults this as well
    as ``personas.find_pov_card``: the two registries are disjoint by id, and a card id
    that matches neither must be refused rather than stored.
    """
    target = (card_id or "").strip()
    if not target:
        return "", None
    for runbook in all_runbooks():
        for card in runbook.use_cases:
            if card.id == target:
                return runbook.key, card
    return "", None
