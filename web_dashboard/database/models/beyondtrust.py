"""BeyondTrust tenant model."""
import json
import uuid
from sqlalchemy import Column, String, DateTime, Boolean, Text

from ...utils.clock import utcnow
from .._engine import Base


class BeyondTrustTenant(Base):
    """One BeyondTrust tenant a POV can be wired into.

    The reason the POV profile exists at all. A demo instance resolves its tenant from
    the global singletons — ``bt_api_host``, ``pscli_api_url``, ``entitle_api_url`` — and
    that is correct there, because there is exactly one. A POV instance runs several POVs
    at once and **each has its own PRA appliance and its own Password Safe Cloud tenant**,
    so "which tenant?" stops having one answer. See docs/profiles/pov/README.md.

    Modelled on ``HypervisorConnection``, which solved the same shape one layer down: N
    named rows replacing a singleton, credentials as Fernet ciphertext *or* an external
    reference, an ``is_default``, and a resolver that still falls back to the old config
    keys so nothing breaks on an install that has not seeded yet.

    **One row per product, not per customer.** ``PovEnvironment`` carries three separate
    FKs because the three are genuinely independent: a POV needs its own PRA appliance and
    its own Password Safe tenant, but Entitle is multi-tenant behind a REGIONAL API URL
    and is usually shared across every POV. A single row holding all three would force a
    duplicate Entitle credential per customer, and duplicated credentials rotate apart.

    **Why this is not the workgroup.** ``PovEnvironment.workgroup`` already exists and
    ``config_service`` already scopes values by it, so "just put the tenant on the
    workgroup" is the tempting shortcut. It is wrong: workgroup answers *who may see this*
    — RBAC, the inventory filters, the expiry exempt list all key off it — while a tenant
    answers *whose PRA appliance this is*. An SE running two customers' POVs inside one
    workgroup would get whichever tenant the workgroup named, silently, for both.

    **On customer data.** ``PovEnvironment`` deliberately has no customer field and adding
    one should be treated as a schema regression. This table is the one place that rule
    bends, and only exactly as far as it must: a PRA appliance is reached at a hostname
    like ``acme.beyondtrustcloud.com``, so the connection target inherently identifies
    who it belongs to and there is no way to hold one without that. A *connection target*
    you cannot avoid; a free-text "customer" box you can. ``name`` is therefore a
    constrained slug, like a POV's, and not somewhere to type a company.
    """
    __tablename__ = "beyondtrust_tenants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # pra | password_safe | entitle — bt_tenant_service.VALID_KINDS
    kind = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)               # operator slug, unique per kind

    # PRA appliance host, Password Safe URL, or the Entitle API base.
    base_url = Column(String(255), nullable=False, default="")
    # OAuth client id for PRA and Password Safe. Blank for Entitle, which is a bearer
    # token with no paired id — a column that is empty for one kind beats a second table.
    client_id = Column(String(255))

    # Exactly one of these carries the credential. Same rule and the same two mechanisms
    # as HypervisorConnection, so there is one secret-at-rest story in this database and
    # one rotation hazard rather than two.
    secret_enc = Column(Text)              # Fernet (config_service.encrypt_value)
    secret_ref = Column(String(256))       # aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://

    # Per-kind NON-SECRET extras, JSON. Allowlisted per kind by the service and pinned by
    # a test: this is a free-form blob on a row that holds credentials, which is exactly
    # where a password ends up by accident.
    options = Column(Text)

    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Set by the Verify action. Derived state would be better, but a live credential check
    # is an outbound round trip nobody wants on a page load, so this is the cached answer
    # and `last_checked_at` is what stops it being read as current.
    last_checked_at = Column(DateTime)
    last_ok_at = Column(DateTime)
    last_error = Column(Text)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    updated_at = Column(DateTime, default=utcnow)

    @property
    def options_dict(self) -> dict:
        if not self.options:
            return {}
        try:
            value = json.loads(self.options)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
