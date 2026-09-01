"""The BeyondTrust tenant registry: N named tenants replacing three singletons.

Slice 4 of the POV feature, and the reason the POV profile exists. A demo instance's
BeyondTrust tenant is the global singleton — ``bt_api_host``, ``pscli_api_url``,
``entitle_api_url`` — and that is right there, because there is exactly one. A POV
instance runs several POVs at once and each has its own PRA appliance and its own
Password Safe Cloud tenant, so "which tenant?" stops having one answer and the wrong
answer is silent: a POV onboarding into the demo tenant, or a demo VM into a customer's
Password Safe. Nothing errors; both paths "work".

This module is deliberately the same shape as ``hypervisor_connection_service``, which
solved the identical problem one layer down (one ``proxmox_host`` became N connections).
Same five-step :func:`resolve`, same ``secret_enc`` / ``secret_ref`` pair reusing
``config_service``'s Fernet, same ``is_default``, same one-time seed from the legacy keys.
Following it rather than inventing a second idiom is the point — an operator who has used
the Connections page already knows this one, and there is one secret-at-rest story in this
database instead of two.

**Step 4 of resolve is the compatibility contract.** With no row for a kind, the legacy
singletons answer. That is what makes this a non-breaking change for every existing
install, and it is why a demo instance can ignore this table entirely.

**One reader of the credential.** :func:`_resolve_secret` is the only function that turns
ciphertext or a vault reference into a value. Everything else passes rows around.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..database import BeyondTrustTenant, PovEnvironment
from . import config_service

logger = logging.getLogger(__name__)


class BTTenantError(Exception):
    """A refusal an operator can act on, not a stack trace."""


# The three BeyondTrust surfaces a POV is wired into. Kept as a tuple in the codebase's
# existing `VALID_*` idiom rather than an enum, because every other registry here is one.
VALID_KINDS = ("pra", "password_safe", "entitle")

LABELS = {
    "pra": "Privileged Remote Access",
    "password_safe": "Password Safe",
    "entitle": "Entitle",
}

# Non-secret per-kind extras allowed in `options`. Closed and pinned by a test, for the
# same reason `hypervisor_connection_service.OPTION_KEYS` is: this is a free-form JSON
# blob on a row that holds a credential, and "anything goes" is how a password ends up in
# one by accident.
OPTION_KEYS = {
    # The Jump Group and Gateway a POV's jump items are created in. Per-tenant because
    # they are names inside that appliance and mean nothing in another one.
    #
    # The KEY is still `jumpoint_name`, matching the `bt_jumpoint_name` setting it seeds
    # from. BeyondTrust renamed Jumpoint to Gateway and this codebase's prose follows,
    # but a persisted key is an identifier: renaming it reads a row nobody wrote, and the
    # symptom is a blank setting rather than an error. See tests/test_gateway_terminology.
    "pra": ("jump_group_name", "jumpoint_name"),
    # The Password Safe run-as user, plus what a POV VM needs to be onboarded as a
    # managed system. All three of the latter are names inside THAT tenant and mean
    # nothing in another one, which is why they belong here rather than in Settings.
    #
    # The functional account is split by guest OS because Password Safe derives the
    # managed system's PLATFORM from it (`fa["platform_id"]`, the rule ps_vm_hook
    # already follows) — one account cannot serve both a Linux and a Windows target.
    "password_safe": ("api_account_name", "workgroup",
                      "linux_functional_account", "windows_functional_account"),
    # What a POV VM needs to become an SSH ephemeral-accounts integration. Owner and
    # workflow are ids inside THAT tenant. The agent token names an Entitle agent running
    # inside the POV's network, and here it is the MANUAL answer only — a POV that
    # installed its own agent names that one instead, because a tenant is shared between
    # POVs and an agent is not. See pov_wireup.agent_token_name.
    #
    # `machine_identity_email` was here and is deliberately gone: every reader of it —
    # `cloud_identity_service`, `entitle_service`, `k8s_service` — reads the INSTANCE-wide
    # `entitle_machine_identity_email`, never the tenant's copy. A field an operator fills
    # in and nothing consumes is worse than an absent one, because it reads as configured.
    # Re-add it here only together with a caller that does `tenant.option(...)`.
    "entitle": ("owner_id", "workflow_id", "agent_token_name", "ssh_sudo_user"),
}

# Kinds whose credentials can be checked without side effects. PRA and Password Safe both
# have a token handshake this codebase already performs, so a Verify proves the credential
# and changes nothing.
#
# Entitle is absent deliberately rather than by omission. Everything the dashboard does
# with Entitle goes through the Terraform provider or POSTs an access request, and an
# access request is a *side effect* — there is no read this codebase already makes that
# would prove a bearer token, and inventing an endpoint to guess at is how a Verify starts
# reporting green for a token that does not work. So the UI shows no Verify for it and
# says why, which is the same "degrade visibly" rule ``lab_platforms.CAPABILITIES``
# follows. ``tests/test_bt_tenants.py`` pins this against what the verifier implements.
VERIFIABLE_KINDS = ("pra", "password_safe")

# What each option is CALLED, for the form. Without this the UI would render a field
# name from the key itself, which is the one place the Jumpoint/Gateway asymmetry above
# would leak back into prose — generated labels are still labels.
OPTION_LABELS = {
    "jump_group_name": "Jump Group name",
    "jumpoint_name": "Gateway name",
    "api_account_name": "Run-as user",
    "workgroup": "Workgroup",
    "linux_functional_account": "Functional account (Linux)",
    "windows_functional_account": "Functional account (Windows)",
    "owner_id": "Owner id",
    "workflow_id": "Workflow id",
    "agent_token_name": "Agent token name",
    "ssh_sudo_user": "SSH sudo user",
}

# What a blank field MEANS, for the options where blank is a real choice rather than an
# omission. Server-side and next to the labels, because the answer is a property of what
# the code does with the value and a hint hardcoded in the template is one that stops being
# true the next time this behaviour changes.
#
# The two functional accounts are the reason this exists: "Functional account (Linux)" with
# no hint reads as a required field naming something that, on a fresh POV tenant, does not
# exist yet — which is a question this form should answer rather than provoke.
OPTION_HINTS = {
    "linux_functional_account": "Leave blank and each POV creates its own, from the "
                                "login its Linux guests already use.",
    "windows_functional_account": "Leave blank and each POV creates its own, from the "
                                  "login its Windows guests already use.",
    # Same shape of answer, one product over. A POV that installed its own agent names
    # that one whatever is typed here, so the honest reading of blank is "the POV's own",
    # not "none" — and an SE who does not know that goes looking for an agent to deploy.
    "agent_token_name": "Only for an agent you deployed yourself. A POV that installs "
                        "its own names that one instead.",
}

# Options whose absence makes a tenant unusable rather than merely incomplete. Reported
# by `serialize` so the gap is visible on the row instead of surfacing inside a job.
REQUIRED_OPTIONS = {
    # `jumpoint_name` is NOT required, though it is allowed. A POV's jump items route
    # through the Gateway installed inside that environment (`env.gateway_name`, and
    # `pov_wireup.gateway_name` explains at length why the appliance-wide one cannot
    # work), so nothing in the POV path reads the tenant's copy. Requiring it made a
    # correctly configured tenant report a gap for a field with no consumer.
    "pra": ("jump_group_name",),
    # `workgroup` and the functional accounts are NOT required: a POV can carry a
    # Password Safe tenant for the Resource Broker's sake without ever onboarding its VMs,
    # and the per-VM path refuses with its own message when one is missing.
    "password_safe": ("api_account_name",),
    # Both are refused by `pov_wireup.entitle_tenant_ctx` before ANY integration is
    # created — the REST accessor adapter included, which needs no agent and no key. So
    # an Entitle tenant without them is unusable rather than merely incomplete, and the
    # row is where that should be visible.
    "entitle": ("owner_id", "workflow_id"),
}

# Same constraint as a POV's name, and for the same reason: this is where a company name
# would otherwise get typed. The appliance hostname in `base_url` already identifies the
# customer and cannot not — a connection target is unavoidable — but a free-text label is
# avoidable, so it is not free text. See the BeyondTrustTenant docstring.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

_SEED_MARK = "beyondtrust_tenants_seeded"


@dataclass(frozen=True)
class Tenant:
    """A resolved tenant: everything a caller needs, with the secret already resolved.

    Frozen and plain so a caller cannot write back through it. ``id`` is empty for the
    legacy-singleton fallback, which is the one Tenant that has no row behind it.
    """
    id: str
    kind: str
    name: str
    base_url: str
    client_id: str
    secret: str
    options: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)

    def option(self, key: str, default: str = "") -> str:
        return str(self.options.get(key) or default)

    @property
    def api_base(self) -> str:
        """``base_url`` normalised to the base each product's API is actually rooted at.

        Here rather than at the call sites because each of these rules already exists once
        — ``pra_api_service._host`` and ``ps_api_service._base_url`` — and a second copy
        that drifts would send a verified tenant's calls somewhere the verify never
        touched. ``tests/test_bt_tenants.py`` pins this against both originals, so the
        drift is a test failure rather than a support case.

        Password Safe is the one with a real trap: ``pscli`` configs store either the bare
        host or the full ``/BeyondTrust/api/public/v3`` path, and both are things an
        operator will paste.
        """
        host = (self.base_url or "").strip().rstrip("/")
        if not host:
            return ""
        if not host.lower().startswith("http"):
            host = f"https://{host}"
        if self.kind == "password_safe" and "/beyondtrust/api/public/" not in host.lower():
            host = f"{host}/BeyondTrust/api/public/v3"
        return host


# ── config helpers ────────────────────────────────────────────────────────────

def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val not in (None, ""):
        return str(val)
    return str(getattr(settings, key, "") or "")


def normalize(kind: str) -> str:
    k = (kind or "").strip().lower()
    if k not in VALID_KINDS:
        raise BTTenantError(
            f"unknown BeyondTrust tenant kind {kind!r}; expected one of "
            f"{', '.join(VALID_KINDS)}")
    return k


# Entitle's API is REGIONAL. `api.entitle.io`, `api.us.entitle.io` and
# `api.ca.entitle.io` are separate deployments serving different tenants — verified by
# their CSP headers, which name `app.entitle.io`, `us.entitle.io` and `ca.entitle.io`
# respectively. Listed here for the form's hint, not as an allowlist: BeyondTrust adds
# regions and a closed list would refuse a real one.
#
# **Every one of them answers 200 to an unauthenticated probe**, which is what makes this
# worth a constant and a paragraph. A tenant pointed at the wrong region does not fail at
# registration or at verify — it fails later, looking like a tenant that holds none of
# the customer's resources.
KNOWN_ENTITLE_REGIONS = ("https://api.entitle.io/v1",
                         "https://api.us.entitle.io/v1",
                         "https://api.ca.entitle.io/v1")


def default_base_url(kind: str) -> str:
    """**This install's** Entitle region, or ``""`` for the kinds that have no default.

    PRA and Password Safe are per-customer appliances and tenants — the hostname IS the
    customer, so there is nothing to default.

    Entitle is multi-tenant, so its host is not per customer either. It is per **region**,
    and this returns the region *this install* is configured for rather than a constant:
    an SE running POVs is almost always running them in their own region, so prefilling it
    saves the typing while leaving the field editable for the customer who is not.

    What it deliberately is NOT is an assertion that the value is right. See
    :data:`KNOWN_ENTITLE_REGIONS` — a wrong region is accepted by every host, so the form
    states which one it filled in rather than presenting it as settled.
    """
    kind = normalize(kind)
    if kind != "entitle":
        return ""
    return _cfg("entitle_api_url").strip()


# ── secrets ───────────────────────────────────────────────────────────────────

def _resolve_secret(row: BeyondTrustTenant) -> str:
    """The tenant's plaintext credential. The only place this happens.

    ``secret_ref`` wins when both are set, so an operator moving a tenant to an external
    backend does not have to clear the old ciphertext first — the same precedence
    ``hypervisor_connection_service`` uses, because a different one here would be a
    difference nobody could remember.
    """
    if row.secret_ref:
        try:
            return config_service.resolve_reference(row.secret_ref)
        except Exception as exc:  # noqa: BLE001
            raise BTTenantError(
                f"tenant {row.name!r}: its secret reference could not be resolved — "
                f"check the external secret backend.") from exc
    if row.secret_enc:
        return config_service.decrypt_value(row.secret_enc)
    return ""


# ── the legacy singletons ─────────────────────────────────────────────────────
#
# The keys each kind used before this table. One spec drives both the seed and resolve()'s
# step-4 fallback, so the row an install seeds and the tenant it would otherwise resolve
# cannot describe different things.

_SINGLETON_SPEC = {
    "pra": {
        "base_url": "bt_api_host",
        "client_id": "bt_client_id",
        "secret": "bt_client_secret",
        "options": {"jump_group_name": "bt_jump_group_name",
                    "jumpoint_name": "bt_jumpoint_name"},
    },
    "password_safe": {
        "base_url": "pscli_api_url",
        "client_id": "pscli_client_id",
        "secret": "pscli_client_secret",
        "options": {"api_account_name": "pscli_api_account_name"},
    },
    "entitle": {
        # Entitle is multi-tenant behind a REGIONAL API URL, so this one is whatever
        # region THIS install was configured for and the token is what usually differs.
        # No options: the machine identity is an instance-wide setting and stays one —
        # see OPTION_KEYS.
        "base_url": "entitle_api_url",
        "client_id": "",
        "secret": "entitle_api_token",
        "options": {},
    },
}


def _from_settings(kind: str) -> Tenant | None:
    """The legacy singleton for a kind, or None when it was never configured.

    ``base_url`` and the secret are what make a tenant usable at all, so both must be
    present. A half-configured singleton returns None rather than a Tenant that fails at
    the first call — the caller's "no tenant is configured" message is the useful one.
    """
    spec = _SINGLETON_SPEC[kind]
    base_url = _cfg(spec["base_url"])
    secret = _cfg(spec["secret"])
    if not base_url or not secret:
        return None
    return Tenant(
        id="", kind=kind, name="(configured)", base_url=base_url,
        client_id=_cfg(spec["client_id"]) if spec["client_id"] else "",
        secret=secret,
        options={k: _cfg(v) for k, v in spec["options"].items() if _cfg(v)},
    )


# ── resolution ────────────────────────────────────────────────────────────────

def to_tenant(row: BeyondTrustTenant) -> Tenant:
    return Tenant(
        id=row.id, kind=row.kind, name=row.name, base_url=row.base_url or "",
        client_id=row.client_id or "", secret=_resolve_secret(row),
        options=row.options_dict)


def resolve(db: Session, kind: str, tenant_id: str | None = None) -> Tenant:
    """The tenant a caller means, in five steps.

    1. an explicit ``tenant_id`` — the wrong kind, inactive, or missing is an **error**,
       never a silent fallback to something else. This is the step a POV uses, and
       falling back here is precisely how a customer's POV would onboard into the demo
       tenant without anything going wrong on the way;
    2. the ``is_default`` row for this kind;
    3. the only active row for this kind, if there is exactly one;
    4. **the legacy singleton config keys**, when the table holds no row for this kind at
       all. This is what makes the registry non-breaking, and it is why a demo instance
       never has to know this table exists. ``# COMPAT:`` in spirit — but unlike the
       hypervisor one it is not removable, because ``demo`` is a supported profile
       forever and those keys are its answer;
    5. otherwise an error naming the fix, because guessing between two customers' PRA
       appliances is very much worse than refusing.
    """
    kind = normalize(kind)

    if db is None:
        # No session: a script or a background helper nobody handed one. The legacy keys
        # are what such a caller could see before this table existed, which beats raising
        # an AttributeError into somebody's `except`.
        legacy = _from_settings(kind)
        if legacy is not None:
            return legacy
        raise BTTenantError(
            f"no {LABELS[kind]} tenant is configured and no database session was supplied")

    if tenant_id:
        row = db.query(BeyondTrustTenant).filter(
            BeyondTrustTenant.id == tenant_id).first()
        if row is None:
            raise BTTenantError("that BeyondTrust tenant no longer exists")
        if row.kind != kind:
            raise BTTenantError(
                f"tenant {row.name!r} is a {LABELS.get(row.kind, row.kind)} tenant, "
                f"not {LABELS[kind]}")
        if not row.is_active:
            raise BTTenantError(f"tenant {row.name!r} is disabled")
        return to_tenant(row)

    rows = db.query(BeyondTrustTenant).filter(
        BeyondTrustTenant.kind == kind,
        BeyondTrustTenant.is_active.is_(True)).all()
    if not rows:
        legacy = _from_settings(kind)
        if legacy is not None:
            return legacy
        raise BTTenantError(
            f"no {LABELS[kind]} tenant is configured — add one on the POV page")

    for row in rows:
        if row.is_default:
            return to_tenant(row)
    if len(rows) == 1:
        return to_tenant(rows[0])
    raise BTTenantError(
        f"{len(rows)} {LABELS[kind]} tenants are configured and none is the default — "
        f"choose one on the POV, or set a default")


def resolve_by_id(db: Session, tenant_id: str) -> Tenant:
    """One tenant, whatever kind it is.

    For the callers that already hold an id and do not care which product it belongs to —
    the Verify endpoint is the whole set. Every other caller knows the kind it wants and
    must go through :func:`resolve`, because "I asked for PRA and got Password Safe" is a
    mistake worth an error rather than a resolution.
    """
    row = _get(db, tenant_id)
    return resolve(db, row.kind, tenant_id)


# ── seeding ───────────────────────────────────────────────────────────────────

def seed_from_settings(db: Session, *, created_by: str = "setup") -> int:
    """Copy whatever singletons this install already has into rows. Once, ever.

    A **seed, not a sync**: after this runs the rows are the truth and editing the old
    config keys does nothing. That one-way-ness is the same promise the Connections page
    makes, and the same one its Settings panels carry a banner about — a second copy that
    keeps re-reading the singletons is how an operator edits a field and watches it have
    no effect, or worse, have an effect a week later.

    Idempotent through a marker key rather than through "is the table empty": an operator
    who seeds, deletes the seeded row on purpose and restarts must not have it come back.
    """
    if config_service.get_bool(_SEED_MARK, False):
        return 0

    made = 0
    for kind in VALID_KINDS:
        if db.query(BeyondTrustTenant).filter(BeyondTrustTenant.kind == kind).first():
            continue
        legacy = _from_settings(kind)
        if legacy is None:
            continue
        row = BeyondTrustTenant(
            kind=kind, name=kind.replace("_", "-") + "-default",
            base_url=legacy.base_url, client_id=legacy.client_id,
            secret_enc=config_service.encrypt_value(legacy.secret),
            options=json.dumps(legacy.options) if legacy.options else None,
            is_default=True, is_active=True, created_by=created_by)
        db.add(row)
        made += 1

    db.commit()
    config_service.set(_SEED_MARK, "1")
    if made:
        logger.info("BeyondTrust tenants: seeded %d row(s) from the legacy config keys",
                    made)
    return made


# ── CRUD ──────────────────────────────────────────────────────────────────────

def _clean_options(kind: str, raw) -> str | None:
    """Keep only this kind's allowlisted keys. Everything else is dropped silently.

    Silently on purpose: the allowlist is a safety property, and a caller that could
    learn which keys were rejected could enumerate the list. What matters to the operator
    is what the row *has*, which ``serialize`` reports.
    """
    if not isinstance(raw, dict):
        return None
    allowed = OPTION_KEYS.get(kind, ())
    kept = {k: str(v).strip() for k, v in raw.items()
            if k in allowed and str(v or "").strip()}
    return json.dumps(kept) if kept else None


def _get(db: Session, tenant_id: str) -> BeyondTrustTenant:
    row = db.query(BeyondTrustTenant).filter(BeyondTrustTenant.id == tenant_id).first()
    if row is None:
        raise BTTenantError("that BeyondTrust tenant no longer exists")
    return row


def list_tenants(db: Session, kind: str = "") -> list:
    q = db.query(BeyondTrustTenant)
    if kind:
        q = q.filter(BeyondTrustTenant.kind == normalize(kind))
    return q.order_by(BeyondTrustTenant.kind, BeyondTrustTenant.name).all()


def serialize(db: Session, row: BeyondTrustTenant) -> dict:
    """A tenant for the UI. **Never the secret**, and never a hint of its length.

    ``has_secret`` and ``secret_ref`` are what an operator needs — is one stored, and is
    it here or in a vault. ``missing_options`` is reported because a tenant with valid
    credentials and no Jump Group is configured-but-unusable, and finding that out inside
    a provision job is finding it out too late.
    """
    opts = row.options_dict
    return {
        "id": row.id,
        "kind": row.kind,
        "label": LABELS.get(row.kind, row.kind),
        "name": row.name,
        "base_url": row.base_url or "",
        "client_id": row.client_id or "",
        "has_secret": bool(row.secret_enc or row.secret_ref),
        "secret_ref": row.secret_ref or "",
        "options": opts,
        "missing_options": [k for k in REQUIRED_OPTIONS.get(row.kind, ())
                            if not str(opts.get(k) or "").strip()],
        "verifiable": row.kind in VERIFIABLE_KINDS,
        "is_default": bool(row.is_default),
        "is_active": bool(row.is_active),
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error or "",
        "in_use_by": environments_using(db, row.id),
        "created_by": row.created_by or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def environments_using(db: Session, tenant_id: str) -> int:
    """How many live POVs point at this tenant.

    Live: a destroyed POV's row is inventory, not a reference that has to keep working.
    Counting those would make a tenant undeletable forever after its first POV.
    """
    from . import pov_env_service
    return (db.query(PovEnvironment)
              .filter(PovEnvironment.status != pov_env_service.STATUS_DESTROYED)
              .filter((PovEnvironment.pra_tenant_id == tenant_id)
                      | (PovEnvironment.ps_tenant_id == tenant_id)
                      | (PovEnvironment.entitle_tenant_id == tenant_id))
              .count())


def create(db: Session, *, kind: str, name: str, created_by: str = "",
           base_url: str = "", client_id: str = "", secret: str = "",
           secret_ref: str = "", options=None, is_default: bool = False) -> dict:
    """Register a tenant.

    ``secret`` is encrypted here and never stored raw; ``secret_ref`` is stored as given
    and resolved per use. Passing both is a refusal rather than a precedence puzzle: the
    operator meant one of them and guessing which decides where a customer's credential
    lives.
    """
    kind = normalize(kind)
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise BTTenantError(
            "name must be 2-63 characters of lowercase letters, digits and hyphens, "
            "starting with a letter or digit — it is a label, not a company name")
    if db.query(BeyondTrustTenant).filter(BeyondTrustTenant.kind == kind,
                                          BeyondTrustTenant.name == name).first():
        raise BTTenantError(f"a {LABELS[kind]} tenant named {name!r} already exists")

    # A blank Entitle URL takes this install's region. The form always prefills, so blank
    # here means an API caller omitted it entirely — and the install's own region is a
    # better answer for that caller than a refusal. `update` refuses instead; see there.
    base_url = (base_url or "").strip() or default_base_url(kind)
    if not base_url:
        raise BTTenantError(f"a {LABELS[kind]} tenant needs its URL or appliance hostname")
    if secret and secret_ref:
        raise BTTenantError(
            "give either a secret or an external secret reference, not both")
    if secret_ref and not config_service.is_reference(secret_ref):
        raise BTTenantError(
            f"{secret_ref!r} is not an external secret reference "
            f"(aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://)")

    row = BeyondTrustTenant(
        kind=kind, name=name, base_url=base_url,
        client_id=(client_id or "").strip(),
        secret_enc=config_service.encrypt_value(secret) if secret else None,
        secret_ref=(secret_ref or "").strip() or None,
        options=_clean_options(kind, options),
        is_active=True, created_by=created_by)
    db.add(row)
    db.commit()
    db.refresh(row)
    if is_default:
        set_default(db, row.id)
        db.refresh(row)
    return serialize(db, row)


def update(db: Session, tenant_id: str, **fields) -> dict:
    """Patch a tenant. Absent keys are left alone; a blank secret KEEPS the stored one.

    "Blank means keep" is the rule every secret field in this dashboard's Settings uses,
    and it is the only safe one for a form that cannot render what is stored: the
    alternative is that saving an unrelated field wipes the credential.
    """
    row = _get(db, tenant_id)

    if "name" in fields:
        name = str(fields["name"] or "").strip().lower()
        if not _NAME_RE.match(name):
            raise BTTenantError(
                "name must be 2-63 characters of lowercase letters, digits and hyphens")
        clash = db.query(BeyondTrustTenant).filter(
            BeyondTrustTenant.kind == row.kind, BeyondTrustTenant.name == name,
            BeyondTrustTenant.id != row.id).first()
        if clash is not None:
            raise BTTenantError(
                f"a {LABELS[row.kind]} tenant named {name!r} already exists")
        row.name = name

    if "base_url" in fields:
        # Blank is a REFUSAL here, not "restore the default" — the opposite of what an
        # earlier draft did, and the difference matters because Entitle's URL turned out
        # to be regional. Substituting this install's region for a field somebody cleared
        # is how a customer's tenant silently moves to another deployment, and every
        # region answers 200 so nothing downstream would report it. `create` may still
        # default a blank, because there the operator never had a value to clear.
        base_url = str(fields["base_url"] or "").strip()
        if not base_url:
            raise BTTenantError(
                "a tenant needs its URL or appliance hostname. For Entitle that is the "
                "REGIONAL API base — " + ", ".join(KNOWN_ENTITLE_REGIONS) + " — and they "
                "are different deployments, so a blank is not something this can fill in "
                "for you.")
        row.base_url = base_url
    if "client_id" in fields:
        row.client_id = str(fields["client_id"] or "").strip()
    if "options" in fields:
        row.options = _clean_options(row.kind, fields["options"])
    if "is_active" in fields:
        row.is_active = bool(fields["is_active"])

    secret = str(fields.get("secret") or "")
    secret_ref = str(fields.get("secret_ref") or "").strip()
    if secret and secret_ref:
        raise BTTenantError(
            "give either a secret or an external secret reference, not both")
    if secret:
        row.secret_enc = config_service.encrypt_value(secret)
        row.secret_ref = None
    elif "secret_ref" in fields:
        if secret_ref and not config_service.is_reference(secret_ref):
            raise BTTenantError(
                f"{secret_ref!r} is not an external secret reference "
                f"(aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://)")
        row.secret_ref = secret_ref or None
        if secret_ref:
            # Moving to a vault clears the local copy. Leaving it would mean the
            # credential an operator believes they removed is still in this database.
            row.secret_enc = None

    # The credential changed or the target moved, so what Verify last said is about a
    # tenant that no longer exists. Stale-green is worse than unknown.
    if secret or secret_ref or "base_url" in fields or "client_id" in fields:
        row.last_checked_at = None
        row.last_ok_at = None
        row.last_error = None

    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    if fields.get("is_default"):
        set_default(db, row.id)
        db.refresh(row)
    return serialize(db, row)


def set_default(db: Session, tenant_id: str) -> dict:
    """Make this the default for its kind, clearing the previous one.

    Scoped to the kind: a default PRA tenant and a default Password Safe tenant are
    unrelated facts, and one flag across all three would let choosing a PRA appliance
    silently repoint Password Safe.
    """
    row = _get(db, tenant_id)
    db.query(BeyondTrustTenant).filter(
        BeyondTrustTenant.kind == row.kind,
        BeyondTrustTenant.id != row.id).update({BeyondTrustTenant.is_default: False},
                                               synchronize_session=False)
    row.is_default = True
    row.is_active = True
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return serialize(db, row)


def delete(db: Session, tenant_id: str) -> None:
    """Remove a tenant, unless a live POV still points at it.

    The FK is ``SET NULL``, so deleting one out from under a POV would not error — it
    would quietly blank that POV's tenant and the next wire-up would resolve the
    *default* instead. A POV silently changing which customer's appliance it targets is
    the exact failure this whole registry exists to prevent, so the refusal is here and
    not left to the constraint.
    """
    row = _get(db, tenant_id)
    in_use = environments_using(db, row.id)
    if in_use:
        raise BTTenantError(
            f"{in_use} POV environment(s) still reference {row.name!r}. Destroy them, or "
            f"point them at another tenant, before deleting it. Disabling it instead "
            f"stops it being chosen for anything new.")
    db.delete(row)
    db.commit()


def record_result(db: Session, tenant_id: str, *, error: str = "") -> dict:
    """Store what a Verify found. Success clears the error; failure keeps ``last_ok_at``.

    Keeping the previous success is deliberate: "worked an hour ago, fails now" and
    "never worked" are different problems, and collapsing them loses the more useful one.
    """
    row = _get(db, tenant_id)
    now = datetime.utcnow()
    row.last_checked_at = now
    if error:
        row.last_error = error[:2000]
    else:
        row.last_error = None
        row.last_ok_at = now
    db.commit()
    db.refresh(row)
    return serialize(db, row)


# ── validation for the POV provision path ─────────────────────────────────────

def validate_selection(db: Session, *, pra_tenant_id: str = "", ps_tenant_id: str = "",
                       entitle_tenant_id: str = "") -> dict:
    """Check the three ids a POV was created with, and hand back what to store.

    Every id is resolved **now**, at the request, rather than inside the provision job.
    A wrong tenant id that surfaces minutes later has already created an environment,
    and the operator has to destroy it to correct a dropdown.

    Blank is allowed and means "not chosen yet": a POV is provisioned before its wire-up
    slices run, and refusing to create one until all three are picked would make the
    registry a gate on a feature that does not need it yet.
    """
    chosen = {"pra_tenant_id": (pra_tenant_id or "").strip(),
              "ps_tenant_id": (ps_tenant_id or "").strip(),
              "entitle_tenant_id": (entitle_tenant_id or "").strip()}
    kinds = {"pra_tenant_id": "pra", "ps_tenant_id": "password_safe",
             "entitle_tenant_id": "entitle"}
    for column, value in chosen.items():
        if not value:
            continue
        # resolve() raises on a missing row, the wrong kind and an inactive one, with a
        # message naming which. Reusing it means the request and the job agree about what
        # a valid selection is rather than each having its own opinion.
        resolve(db, kinds[column], value)
    return chosen
