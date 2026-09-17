"""The POV use-case checklist: what a customer's evaluation is proving.

One registry of cards, grouped by the **product** each one demonstrates. A POV is bought
and scoped by product -- a row carries PRA, Password Safe and Entitle tenants
independently, and a Password-Safe-only evaluation is a normal shape -- so the product is
the only grouping that answers the two questions an SE actually asks of this page: "what
does this evaluation still owe the customer?" and "which of these can this POV even run?"

**These are not personas.** They used to be: the 32 cards lived in eight
``Persona.pov_use_cases`` tuples and rendered under role headings. That was wrong in a way
worth recording, because the mistake is easy to make again. A persona is a *presenting*
decision -- which role's story an SE leads an estate demo with, on a day they choose. A POV
instance is not presenting. It is one customer's evaluation of the products on their own
row, and the role of whoever happens to be in the room changes nothing about what has been
proved. So the role headings were sorting the customer's remaining work by an axis that
belonged to the other install profile, and ``services/personas`` is now what it says it is:
demo-profile curation, documented at ``docs/profiles/demo/personas``.

What survives from that arrangement, deliberately:

  * **The group shape.** ``{group, label, blurb, docs, use_cases}`` -- identical to
    :mod:`pov_runbooks`, which appends its own group to the same catalog. Every consumer
    (``pov_use_cases._groups``, the detail page's loop, ``pov_summary``) takes them without
    knowing which registry produced which, and that is the whole reason a runbook needed no
    changes to the page when it arrived.
  * **Never subtracting.** Every group and every card is present for every product mix. The
    mix decides each card's STATE and nothing else: a ``Password Safe``-only POV is served
    the PRA and Entitle groups too, marked ``out_of_scope`` to a card. Set that tenant on
    the POV row and they are simply in scope, with nothing to backfill or re-enable --
    which is the property that keeps this from being a second ``install_profile``.

    What the PAGES draw is theirs to decide, and all three of them drop the out-of-scope
    cards (``inScope()``, in each template). That is a presentation choice on top of a whole
    catalog, not a gate; this module must keep serving them either way, because the summary
    denominators and the runbook gating both read the state.
  * **Knowing nothing about the database.** The resolvers take a plain **products dict** of
    booleans; ``services/pov_use_cases`` is the module that knows about ``PovEnvironment``.
    Keeping the row out of here is what lets this module be imported from anywhere.

A card's group is **derived, never authored**: it is the first product the card declares,
or the environment group when it declares none. So there is no second field to keep in sync
with ``requires_products``, and an author moves a card between groups by reordering that
tuple -- which is also the field that decides whether the card is in scope at all, so the
two can never disagree. The four cards naming two products all lead with ``pra``, and all
four are PRA-first stories; a guard test holds that the ordering stays deliberate.
"""
from .personas import UseCase


# ── the products ─────────────────────────────────────────────────────────────
#
# The three BeyondTrust products a POV can be wired into, named as PRODUCTS rather than as
# flags. That is the whole distinction: `pra_enabled` is an instance-wide toggle, while
# `pra` here means "this POV has a PRA tenant on its row". Both are true on a POV instance
# running a Password-Safe-only POV, and only the second one answers the question a card
# asks. Sharing the flag vocabulary is how the two would silently become one.
POV_PRODUCTS = ("pra", "password_safe", "entitle")

_PRODUCT_LABELS = {
    "pra": "Privileged Remote Access",
    "password_safe": "Password Safe",
    "entitle": "Entitle",
}

# The artifact each product leaves on a POV once the wire-up has actually run. A POV can
# name a tenant and have nothing wired into it yet, and those are different answers: the
# first is "this POV does not include that product" (nothing to do), the second is "it does,
# and there is a button to press". `services/pov_use_cases.products_for` fills both halves.
_PRODUCT_ARTIFACT = {
    "pra": "wired",
    "password_safe": "onboarded",
    "entitle": "entitle_wired",
}

# What to run when the tenant is there and the artifact is not. Named per product because
# the Entitle half needs something the other two do not -- an SSH key on the POV row, which
# no other step can derive -- and "run the wire-up" alone would send an operator round a
# loop that keeps skipping the one thing they came for.
_PRODUCT_REMEDY = {
    "pra": "PRA jump items — run the wire-up",
    "password_safe": "Password Safe onboarding — needs a Resource Broker, then the wire-up",
    "entitle": "the Entitle integration — needs an SSH key on this POV, then the wire-up",
}

# Where a card sends an operator when it is unready. One destination, not a per-card field:
# every remedy above ends at the same tab, and a card that could name its own would
# eventually name one that does not exist.
_POV_ACTION_TAB = "#wired"


# ── the groups ───────────────────────────────────────────────────────────────

# The fourth group: cards that name no product at all. They are about the POV itself --
# who can reach it, what the two components inside the customer's network talk to, what
# teardown removes -- so they are always in scope, on every mix, and they are the ones a
# platform team and an auditor ask for rather than a product owner. Last in the order
# because a product group is what the evaluation was bought for.
ENVIRONMENT = "environment"

GROUPS = POV_PRODUCTS + (ENVIRONMENT,)

# A product group's label IS the product's label -- one spelling of "Password Safe", so a
# rename in the copy cannot leave the heading and the "Needs: ..." line disagreeing.
_GROUP_LABELS = {**_PRODUCT_LABELS, ENVIRONMENT: "The environment itself"}

_GROUP_BLURBS = {
    "pra": "Reaching a machine in the lab without a VPN, an inbound port or a password on "
           "anyone's screen.",
    "password_safe": "Bringing the lab's own accounts under management, and rotating them "
                     "without breaking what uses them.",
    "entitle": "Access that arrives when it is asked for and leaves on its own, so nothing "
               "in the environment is standing.",
    ENVIRONMENT: "The POV itself: who can reach it, what runs inside the customer's "
                 "network, and what is left when it is destroyed. These run on every POV.",
}

# One doc per group, and each one exists on disk (a guard test walks them). The product
# groups point at the integration page rather than at a POV page: an SE fielding "how does
# this actually work?" in the room needs the product, not the lab.
_GROUP_DOCS = {
    "pra": ("integrations/privileged-remote-access",),
    "password_safe": ("integrations/password-safe",),
    "entitle": ("integrations/entitle",),
    ENVIRONMENT: ("profiles/pov/lifecycle",),
}


def group_of(card: UseCase) -> str:
    """Which group ``card`` belongs to -- the first product it declares, else the
    environment group.

    Derived rather than stored, so ``requires_products`` is the single field that decides
    both a card's group and whether it is in scope. A separate ``group=`` would be a second
    place to edit, and the failure when they disagreed would be a card filed under a
    product it never mentions.
    """
    return card.requires_products[0] if card.requires_products else ENVIRONMENT


# ── the cards ─────────────────────────────────────────────────────────────────

_PRA_CARDS = (
    UseCase(
        id="pov-cloudops-three-layers",
        title="Three PAM layers on one lab VM",
        summary="Take one guest in this environment from “a machine with a password” "
                "to brokered access, a vaulted account and a grant that expires — the "
                "same three layers the demo estate shows, on the customer’s own lab.",
        target="#wired",
        minutes=12,
        docs="profiles/pov/wiring",
        requires_products=("pra", "password_safe"),
    ),
    UseCase(
        id="pov-devops-no-inbound",
        title="Reach the environment with no inbound firewall rule",
        summary="Every session into this lab arrives through a Gateway that only ever "
                "dials outward — no VPN, no port opened, nothing published.",
        target="#overview",
        minutes=8,
        docs="integrations/gateways",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-hypervisor-web-jump-console",
        title="Open a guest console with the password injected",
        summary="Reach a management console in the lab with the credential injected and "
                "the session recorded — the administrator does the work and never "
                "learns the password.",
        target="#wired",
        minutes=10,
        docs="integrations/privileged-remote-access",
        requires_products=("pra", "password_safe"),
    ),
    UseCase(
        id="pov-hypervisor-shell-jump-guest",
        title="Reach a guest on an isolated segment",
        summary="Shell Jump to a VM on the lab’s private network through the Gateway "
                "inside it — the machine has no route in and never needed one.",
        target="#wired",
        minutes=8,
        docs="integrations/privileged-remote-access",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-hypervisor-gateway",
        title="Where the Gateway sits, and why that is the whole story",
        summary="The egress-only path out of this environment, told against the live "
                "Gateway this POV installed rather than a diagram.",
        target="#overview",
        minutes=8,
        docs="integrations/gateways",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-itops-recorded-support",
        title="Help someone on a lab desktop, recorded",
        summary="Join a session on a guest desktop to support whoever is using it — "
                "recorded end to end, with no credential handed over.",
        target="#wired",
        minutes=10,
        docs="integrations/privileged-remote-access",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-ot-tunnel-to-device",
        title="Tunnel to a device with no VPN and no inbound rule",
        summary="Reach a machine on the lab’s process network over its own protocol "
                "through a brokered tunnel — the access path that does not require "
                "opening the plant network.",
        target="#wired",
        minutes=12,
        docs="integrations/privileged-remote-access",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-ot-vendor-jit",
        title="Two hours for the integrator, then the tunnel closes",
        summary="Give a third party access to one machine for a bounded window and "
                "watch the grant expire and the session end by itself — the flagship "
                "story, because vendor access is how plants get compromised.",
        target="#wired",
        minutes=15,
        docs="design/entitle-user-jit",
        requires_products=("pra", "entitle"),
    ),
    UseCase(
        id="pov-ot-credential-injection",
        title="The vendor uses a credential they never see",
        summary="The machine’s account is vaulted and injected at session launch, so "
                "a third party does real work without the password ever being on "
                "their screen or in their notes.",
        target="#wired",
        minutes=12,
        docs="integrations/password-safe",
        requires_products=("pra", "password_safe"),
    ),
    UseCase(
        id="pov-ot-egress-only",
        title="Nothing dials in — the architecture slide, live",
        summary="Trace the outbound-only path from the lab’s private segment to the "
                "appliance, against the Gateway this POV is actually using.",
        target="#overview",
        minutes=8,
        docs="integrations/gateways",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-dba-private-tunnel",
        title="A normal SQL client, a database with no way in",
        summary="Connect the tool the DBA already uses to a database on the lab’s "
                "private network, through a brokered tunnel rather than a bastion "
                "nobody patches.",
        target="#wired",
        minutes=10,
        docs="integrations/privileged-remote-access",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-security-session-record",
        title="Every session recorded, and where the recording lives",
        summary="Play back a session someone ran during this evaluation, and say "
                "plainly where the recording is held and who can reach it.",
        target="#wired",
        minutes=10,
        docs="integrations/privileged-remote-access",
        requires_products=("pra",),
    ),
    UseCase(
        id="pov-sre-private-api",
        title="Reach a private management API",
        summary="Point a normal client at a service in the lab that has no public "
                "endpoint, through a brokered tunnel instead of a jump box.",
        target="#wired",
        minutes=10,
        docs="integrations/privileged-remote-access",
        requires_products=("pra",),
    ),
)


_PASSWORD_SAFE_CARDS = (
    UseCase(
        id="pov-cloudops-agentless-onboard",
        title="Onboard a guest with nothing installed on it",
        summary="Bring a lab VM’s admin credential under management and rotate it "
                "without installing an agent on the guest — the Resource Broker "
                "inside the environment reaches it, so nothing on the machine changes.",
        target="#vms",
        minutes=10,
        docs="integrations/password-safe",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-devops-secretless-run",
        title="A playbook with no credential in it",
        summary="Run a playbook against a lab host that looks its own credential up as "
                "it executes — nothing in the repo, nothing in the inventory file, "
                "nothing on disk when it finishes.",
        target="#wired",
        minutes=12,
        docs="integrations/ansible",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-devops-broker-path",
        title="How the credential reaches a host you cannot route to",
        summary="Walk the path from the vault to a guest on a private lab network "
                "through the Resource Broker — the architecture question every "
                "rotation project stalls on, answered against something running.",
        target="#overview",
        minutes=8,
        docs="profiles/pov/design/resource-broker",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-hypervisor-rotate-root",
        title="Rotate the account six people share",
        summary="Bring a lab guest’s root or Administrator credential under management "
                "and rotate it — the account that has historically lived in a shared "
                "password manager.",
        target="#vms",
        minutes=10,
        docs="integrations/password-safe",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-itops-rotate-local-admin",
        title="Rotate a Windows guest’s local-admin password",
        summary="The shared local administrator password every machine has carried "
                "since imaging, brought under management and rotated per machine.",
        target="#vms",
        minutes=8,
        docs="integrations/password-safe",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-dba-onboard-db-account",
        title="Bring the database’s admin account under management",
        summary="Onboard the account the lab’s database runs on and rotate it — the "
                "credential that has never been changed because nobody was sure what "
                "would break.",
        target="#wired",
        minutes=12,
        docs="integrations/password-safe",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-dba-rotate-no-outage",
        title="Rotate it with the application still running",
        summary="Rotate an account something depends on and show the dependent keep "
                "working — the objection that stops most rotation projects, met on "
                "the customer’s own stack.",
        target="#vms",
        minutes=12,
        docs="integrations/password-safe",
        requires_products=("password_safe",),
    ),
    UseCase(
        id="pov-sre-token-rotation",
        title="Rotate the token a service is using",
        summary="The long-lived token in a CI system or a sidecar, rotated with the "
                "consumer picking up the new value — the non-human identity nobody "
                "rotates because nobody is sure what would break.",
        target="#wired",
        minutes=12,
        docs="integrations/password-safe",
        requires_products=("password_safe",),
    ),
)


_ENTITLE_CARDS = (
    UseCase(
        id="pov-cloudops-jit-vm",
        title="Two hours on one machine, then nothing",
        summary="Grant access to a single guest for the length of a task and watch the "
                "account disappear on its own — no standing administrator anywhere in "
                "the environment.",
        target="#wired",
        minutes=12,
        docs="design/entitle-user-jit",
        requires_products=("entitle",),
    ),
    UseCase(
        id="pov-devops-ephemeral-ssh",
        title="SSH accounts that exist only for the run",
        summary="A pipeline asks for an account on a lab host, gets it for the length "
                "of the job, and the account is destroyed on completion — so there is "
                "no build user to audit, rotate or forget about.",
        target="#wired",
        minutes=12,
        docs="design/entitle-user-jit",
        requires_products=("entitle",),
    ),
    UseCase(
        id="pov-itops-ask-for-elevation",
        title="Let the user ask for the one thing they need",
        summary="A request, an approval, and access to exactly one machine for exactly "
                "as long as was asked for — instead of a permanent group membership "
                "nobody reviews.",
        target="#wired",
        minutes=12,
        docs="design/entitle-user-jit",
        requires_products=("entitle",),
    ),
    UseCase(
        id="pov-dba-jit-grant",
        title="Request, approve, grant, expire",
        summary="An analyst asks for read access, an approver says yes, the grant "
                "appears and then removes itself — the full loop, on the customer’s "
                "own data, in about ten minutes.",
        target="#wired",
        minutes=12,
        docs="design/entitle-resource-registration",
        requires_products=("entitle",),
    ),
    UseCase(
        id="pov-security-no-standing-access",
        title="Prove nobody holds standing access",
        summary="Show the grant list empty between requests — access exists only while "
                "somebody asked for it, which is a different claim from “access is "
                "logged”.",
        target="#wired",
        minutes=10,
        docs="design/entitle-user-jit",
        requires_products=("entitle",),
    ),
    UseCase(
        id="pov-sre-incident-access",
        title="Access that lasts as long as the incident",
        summary="An engineer requests elevated access for the length of an incident "
                "and it removes itself afterwards — no permanent break-glass group "
                "that quietly becomes everyone’s baseline.",
        target="#wired",
        minutes=12,
        docs="design/entitle-user-jit",
        requires_products=("entitle",),
    ),
)


_ENVIRONMENT_CARDS = (
    UseCase(
        id="pov-cloudops-reap",
        title="The whole POV disappears on the date you set",
        summary="Give this environment an expiry and show what teardown removes — the "
                "integrations, the accounts, the jump items and the lab itself. The "
                "answer to “what happens to our data when the evaluation ends?”",
        target="#overview",
        minutes=8,
        docs="auto-delete-timer",
    ),
    UseCase(
        id="pov-itops-share-desktop",
        title="Hand the customer their own way in",
        summary="Publish a password-protected, expiring link onto this environment’s "
                "desktops, so the evaluation continues when nobody from your side is "
                "on the call.",
        target="#share",
        minutes=6,
        docs="profiles/pov/customer-access",
    ),
    UseCase(
        id="pov-security-who-has-access",
        title="Who has access to this environment, right now",
        summary="Every privileged path into the POV in one view, with what created "
                "each one — the question an auditor opens with, answered without a "
                "spreadsheet.",
        target="#wired",
        minutes=8,
        docs="profiles/pov/wiring",
    ),
    UseCase(
        id="pov-security-teardown-proof",
        title="What teardown removes, in the order it removes it",
        summary="Walk the destroy path — accessors and links first, then the customer’s "
                "own appliance objects, then the lab. The evidence that an evaluation "
                "leaves nothing behind.",
        target="#overview",
        minutes=8,
        docs="profiles/pov/customer-access",
    ),
    UseCase(
        id="pov-sre-inside-components",
        title="The two things that run inside the customer’s network",
        summary="The Gateway and the Resource Broker, what each one talks to, and why "
                "neither needs anything dialling in — the review a platform team will "
                "ask for before any of this ships.",
        target="#overview",
        minutes=10,
        docs="profiles/pov/design/resource-broker",
    ),
)


_CARDS = {
    "pra": _PRA_CARDS,
    "password_safe": _PASSWORD_SAFE_CARDS,
    "entitle": _ENTITLE_CARDS,
    ENVIRONMENT: _ENVIRONMENT_CARDS,
}


def cards(group: str = "") -> tuple:
    """The cards in one group, or every card in group order when ``group`` is empty."""
    if group:
        return _CARDS.get(group, ())
    return tuple(c for g in GROUPS for c in _CARDS[g])


# ── card readiness ───────────────────────────────────────────────────────────

def card_state(card: UseCase, products: dict) -> tuple:
    """``(state, needs)`` for one card, against one POV's product mix.

    Three states, and deliberately **not** the three words ``personas._card_state`` uses:

      ``ready``         this POV has every product the card names, and each one's artifact
                        actually exists on it
      ``needs_wiring``  the tenant is set and the artifact is not -- actionable, so the
                        card keeps a link to the tab with the button on it
      ``out_of_scope``  this POV has no tenant for that product at all

    ``out_of_scope`` is NOT ``masked``. Masked means this INSTANCE's profile refuses the
    feature and no operator can change that. Out of scope means this CUSTOMER's POV was
    deliberately not wired into that product -- a Password-Safe-only evaluation is a normal,
    correct shape, and borrowing the word "masked" for it would make the whole page read as
    a misconfiguration.

    Absence beats unwired when a card names two products: a card needing PRA and Password
    Safe on a POV with no PRA tenant cannot be run at all, so reporting "run the wire-up"
    would send an operator to a button that will skip the half they came for.
    """
    absent = []
    unwired = []

    for product in card.requires_products:
        if not products.get(product):
            absent.append(_PRODUCT_LABELS.get(product, product))
        elif not products.get(_PRODUCT_ARTIFACT.get(product, ""), False):
            unwired.append(_PRODUCT_REMEDY.get(product,
                                               _PRODUCT_LABELS.get(product, product)))

    if absent:
        return "out_of_scope", tuple(absent)
    if unwired:
        return "needs_wiring", tuple(unwired)
    return "ready", ()


def describe_card(card: UseCase, env_id: str, products: dict) -> dict:
    """One POV card as the API serves it. An out-of-scope card carries **no href at all.**

    The registry holds a fragment and this joins it to the POV -- so ``/pov/<id>`` is
    spelled in exactly one place, and a card can never carry a path to an environment that
    is not the one being described. :mod:`pov_runbooks` renders its own cards through this
    function for the same reason: two spellings of that join is two chances to leak a POV
    id into another POV's page.
    """
    state, needs = card_state(card, products)
    return {
        "id": card.id,
        "title": card.title,
        "summary": card.summary,
        # Withheld rather than dimmed, for the same reason personas.describe_card withholds
        # a masked target: a link the client only styles as inert is one stray middle-click
        # from taking somebody to a tab that has nothing on it for this POV.
        "target": f"/pov/{env_id}{card.target}" if state != "out_of_scope" else "",
        "minutes": card.minutes,
        "docs": f"/docs/{card.docs}" if card.docs else "",
        "state": state,
        "needs": list(needs),
        # The products this card is about. Still shipped even though the group now names
        # one of them: a card can require two, and the second is what the page needs to
        # explain an out-of-scope state that its heading does not account for.
        "products": list(card.requires_products),
        # Only the ACTIONABLE state gets one. An out-of-scope card has nowhere useful to
        # send an operator -- the fix is a tenant on the POV row, which is a decision about
        # the evaluation rather than a button on a tab.
        "action_link": (f"/pov/{env_id}{_POV_ACTION_TAB}"
                        if state == "needs_wiring" else ""),
    }


# ── the catalog ──────────────────────────────────────────────────────────────

def describe(group: str, env_id: str, products: dict) -> dict:
    """One group's cards for one POV. Unknown group yields an empty group, never None."""
    if group not in _CARDS:
        return {"group": "", "label": "", "blurb": "", "docs": [], "use_cases": []}
    return {
        "group": group,
        "label": _GROUP_LABELS[group],
        "blurb": _GROUP_BLURBS[group],
        "docs": [f"/docs/{d}" for d in _GROUP_DOCS[group]],
        "use_cases": [describe_card(c, env_id, products) for c in _CARDS[group]],
    }


def catalog(env_id: str, products: dict) -> list:
    """Every group's cards for one POV, in :data:`GROUPS` order.

    Complete for every product mix -- see the module docstring on why the mix decides a
    card's state and never its presence.
    """
    return [describe(g, env_id, products) for g in GROUPS]


def find_card(card_id: str) -> tuple:
    """``(group, UseCase)`` for a card id, or ``("", None)``.

    The registry is the allowlist for anything that WRITES a card id. Without this a
    progress table accepts any string a client sends and becomes a free-text store nobody
    can render -- and the rows outlive the mistake, because progress is deliberately never
    deleted on a copy edit. :mod:`pov_runbooks` carries the second half of that allowlist.
    """
    target = (card_id or "").strip()
    if not target:
        return "", None
    for group in GROUPS:
        for card in _CARDS[group]:
            if card.id == target:
                return group, card
    return "", None
