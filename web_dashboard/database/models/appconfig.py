"""App configuration models: app config, brand assets, the schema marker ledger, the secret vault."""
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, LargeBinary

from ...utils.clock import utcnow
from .._engine import Base


class AppConfig(Base):
    """Encrypted key-value store for cloud credentials and feature flags.

    Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so that
    secrets at rest are protected even if someone reads the DB directly.
    Written by the setup wizard; consumed by config_service.get().

    The optional `workgroup` column lets prod-style multi-tenant deployments
    have per-workgroup overrides for the same key. NULL means "global";
    config_service.get() falls back to the NULL row when no workgroup-scoped
    row exists. Community installs leave `workgroup` NULL always and behave
    as before.
    """
    __tablename__ = "app_config"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)         # Fernet-encrypted
    workgroup = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class BrandAsset(Base):
    """An uploaded branding image -- today just the operator's logo.

    Its own table rather than an `app_config` row, for reasons that compound:
    `config_service` caches the WHOLE config table in every worker and reloads it from the
    database every 5 seconds, so a base64 logo there would be a per-worker resident string
    re-selected and Fernet-decrypted twelve times a minute forever, whether or not anybody
    is looking at a page. `get_all_public()` also returns every global row, which would
    quietly put the image in the settings payload. And a data URI inlined into the HTML
    cannot be cached by the browser at all, on every page, including /login.

    `slot` is the primary key, which is what makes "one global logo" a property of the
    schema instead of a rule the application has to remember. It leaves room for a
    `favicon` or `logo_dark` slot later without a second table.

    Deliberately NOT Fernet-encrypted, unlike every other sensitive column here. These
    bytes are served verbatim to anonymous browsers -- the sign-in page and the public docs
    shell both render the mark -- so they are by definition not a secret. Encrypting them
    would add ~37% to the stored size, put a decrypt on the pre-auth render path for every
    cache miss, and inherit the JWT_SECRET_KEY rotation hazard, whose symptom here would be
    a corrupt image rather than an error anybody could act on.

    `sha256` is the content hash and does double duty: it is the HTTP ETag and the last
    segment of the URL, so a new upload busts every browser cache immediately while the
    bytes themselves can be served `immutable`. Never add this table to a view that does
    `SELECT *` -- read it by primary key for the bytes, or project the metadata columns.

    No migration entry is needed: `create_all` makes new tables.
    """
    __tablename__ = "brand_asset"

    slot = Column(String(32), primary_key=True)        # "logo"
    content_type = Column(String(64), nullable=False)  # from the sniffer, never the client
    data = Column(LargeBinary, nullable=False)
    sha256 = Column(String(64), nullable=False)        # hex digest; the ETag
    byte_size = Column(Integer, nullable=False)
    # Nullable because an SVG need not declare pixel dimensions, and there is no image
    # library in requirements.txt to infer them. Only used to reserve the right aspect
    # ratio so the nav bar does not reflow when the image lands.
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    filename = Column(String(255), nullable=True)      # display only, in the settings card
    uploaded_by = Column(String(128), nullable=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


class SchemaMarker(Base):
    """"This one-time data migration has run." One row per migration, never deleted.

    Deliberately NOT a key in ``app_config``: those values are Fernet-encrypted with a key
    derived from JWT_SECRET_KEY, so rotating that secret would make the marker unreadable
    and re-run the migration. For a backfill that GRANTS permissions, re-running is not a
    harmless no-op — it would re-add a scope an administrator had deliberately removed.

    Most backfills in this file derive idempotency from the data instead (see
    ``job_service.backfill_audit_chain``, which checks whether any row is chained yet).
    That works when the end state is indistinguishable from "already done". It does not
    work here, because "this user has no `costs` scope" is both the pre-migration state and
    a legitimate post-migration choice.
    """
    __tablename__ = "schema_markers"

    key = Column(String(128), primary_key=True)
    applied_at = Column(DateTime, default=utcnow, nullable=False)
    detail = Column(Text, nullable=True)        # free-text: what it did, for support


class SecretVault(Base):
    """Registry of external secret-store endpoints the dashboard can resolve
    references against.

    Used by the multi-vault reference scheme: a config row with
    ``azure_kv://<vault-id>/<secret-name>`` looks up the vault row by
    (id, backend) and routes the read to that vault's endpoint with that
    vault's credentials. When the table is empty (community / fresh install)
    the legacy ``azure_kv://<secret-name>`` shape continues to resolve via
    the singleton config_service.get('azure_kv_*') keys, so behaviour is
    unchanged until an operator registers a vault.
    """
    __tablename__ = "secret_vaults"

    id = Column(String(64), primary_key=True)              # e.g. "primary", "tenant-alpha-eu"
    backend = Column(String(32), nullable=False)           # azure_kv | aws_sm | gcp_sm | bt_secrets_safe
    endpoint = Column(Text, nullable=False)                # e.g. https://my-vault.vault.azure.net
    credentials_ref = Column(Text, nullable=True)          # optional reference to creds (e.g. distinct SP per vault)
    workgroup = Column(String(64), nullable=True)          # if set, only this workgroup resolves here
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by_user_id = Column(String(36), nullable=True)
