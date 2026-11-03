"""Identity and access models: users, credentials, tokens, OAuth clients, workload identities, SPIFFE."""
import json
import uuid
from typing import List
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, LargeBinary, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from ...utils.clock import utcnow
from .._engine import Base
from ..rbac import _ROLE_UNRESOLVED, merge_permission_maps, narrow_to_token_scope


class User(Base):
    """User model for authentication and authorization"""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=True)  # nullable for OAuth-only users
    full_name = Column(String(200))
    email = Column(String(200))
    workgroups = Column(Text)  # JSON array: ["Hydra", "Weaverlab"]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    # Auth provider fields
    auth_provider = Column(String(20), default="local", nullable=False)  # 'local' | 'azure_ad'
    oauth_subject = Column(String(255), nullable=True, unique=True)  # Azure AD oid claim
    mfa_required = Column(Boolean, default=False)  # True once first FIDO2 key is registered
    is_admin = Column(Boolean, default=False)       # Can manage users via /users page
    # Fine-grained permissions: JSON dict {"vms":["read","write"],"aws":["read"],...}
    # NULL = all permissions granted (backward compatible default).
    # This is the admin-set baseline (set via /users page or wizard).
    permissions = Column(Text, nullable=True)
    # Session-scoped permissions derived from OAuth group membership.
    # Re-computed on every OIDC login as the union of default_permissions
    # across matched oauth_group_mappings. Enables the Entitle user-JIT
    # flow (see docs/design/entitle-user-jit.md Phase 0): Entitle grants
    # the user a time-bound Entra group membership; next login picks the
    # union up here; effective_permissions_dict returns
    # union(permissions, session_permissions). Group expiry → next login
    # sees the group gone → matching permissions drop.
    session_permissions = Column(Text, nullable=True)

    # Entitle REST-granted permissions, and a SEPARATE column on purpose.
    # _complete_oauth_login overwrites session_permissions with the group-derived
    # union on every login — that overwrite is load-bearing, because it is how a
    # removed Entra group actually reduces access. Writing an Entitle grant there
    # would therefore be silently wiped at the user's next login.
    #
    # Keeping the two sources independent also means both can be live at once,
    # which is what makes migrating from Entra groups to the REST integration a
    # gradual change rather than a cutover.
    jit_permissions = Column(Text, nullable=True)

    # ── Access role (the fourth permission source) ───────────────────────────
    # The named, reusable permission map this user is assigned. TWO columns, and they are
    # always written together by `services/role_service.apply_role_to_user` -- the single
    # writer. Never set one without the other.
    #
    #   role_id           WHICH role, for the editor, the assignee list and the guard
    #   role_permissions  a materialised COPY of that role's map, unioned in below
    #
    # The copy is why there is no `relationship()` here, and that is a security decision
    # rather than a performance one. `effective_permissions_dict` is read on a DETACHED User
    # by `api/mcp_server` (see `_ROLE_UNRESOLVED` above), where a lazy load raises -- and
    # because an empty map means unrestricted, any `except: return {}` written around such a
    # load is a total authorization bypass. A plain column cannot fail that way.
    #
    # The copy is DERIVED, never authoritative: `role_service` fans a role edit out to every
    # assignee in the same transaction, and `role_service.reconcile` rewrites every row from
    # its role at boot. `permissions` above remains the column for per-user hand edits, which
    # is what makes a role an assignment plus an optional override rather than a replacement.
    # NO ForeignKey on either of these, and that is two decisions rather than an omission.
    #
    # First, it would be a CYCLE: `access_roles.created_by_user_id` points back here, so
    # declaring the reverse constraint makes `users` and `access_roles` mutually dependent.
    # SQLAlchemy cannot order that -- it warns "unresolvable cycles between tables" and then
    # IGNORES those constraints when sorting, which on a fresh PostgreSQL install means
    # CREATE TABLE users can run before access_roles exists and fail. SQLite accepts a
    # REFERENCES to a table that is not there yet, so the whole test suite is blind to it.
    #
    # Second, it would only ever exist on SOME installs. The retrofit ALTER in the
    # legacy list (schema_legacy.py) has no REFERENCES clause -- it cannot, without rewriting
    # the table -- so every upgraded install has no constraint here regardless. A constraint
    # that holds on new deployments and not on the ones with data in them is worse than
    # none: the bug it would catch surfaces only where nobody is looking for it.
    #
    # So referential integrity for a role assignment lives in `services/role_service`
    # instead, where it is testable: `delete` refuses while assigned, `--force` clears both
    # columns in one transaction, and `reconcile` clears a `role_id` whose role has gone.
    role_id = Column(String(36), nullable=True, index=True)
    role_permissions = Column(Text, nullable=True)

    # ── Persona (curation only) ──────────────────────────────────────────────
    # Which role's material the dashboard leads with for this user. NOT a permission:
    # services/personas may only reorder and surface, never gate, so neither column can
    # grant or deny anything. See services/personas.py for why that invariant is what
    # makes the whole layer safe.
    #
    # TWO columns, for exactly the reason jit_permissions is separate from
    # session_permissions below: `_complete_oauth_login` overwrites the group-derived one
    # on EVERY login -- that overwrite is load-bearing, because it is how removing someone
    # from an Entra group actually stops giving them that focus. An admin's deliberate
    # assignment written there would be silently wiped at the user's next login.
    #
    #   persona          an admin (or the user), in the dashboard -- never automatically
    #   session_persona  the OIDC group mapping -- rewritten every login
    #
    # `persona` wins when both are set: a specific decision about one person outranks a
    # rule about a group they happen to be in.
    persona = Column(String(32), nullable=True)
    session_persona = Column(String(32), nullable=True)

    # Non-NULL means this account is a POV ACCESSOR: a prospect's ephemeral login, bound
    # to exactly one POV environment and deleted when that POV is reaped. See
    # services/pov_accessor_service and PovAccessor below.
    #
    # It is a DENY marker, not a permission. The three columns above all GRANT, and
    # `effective_permissions_dict` returns {} when none of them says anything -- which
    # `require_permission` reads as UNRESTRICTED, for backward compatibility with the
    # pre-OIDC users that predate them. So an accessor created "with no permissions" would
    # hold every permission in the dashboard. The guard is therefore in
    # `api/auth.get_current_user` -- the one dependency every authenticated route resolves
    # through -- as a path ALLOWLIST, and it fires on this column being set rather than on
    # anything in the permission map.
    #
    # Duplicated from PovAccessor.environment_id deliberately: that guard runs on every
    # request and must not need a join.
    accessor_env_id = Column(String(36), nullable=True, index=True)

    # WHICH POVs this user may reach, as a JSON array of pov_environments.id. The `pov`
    # permission scope says what they may DO with a POV; this says which ones they can see
    # it on. NULL or [] means every POV the scope allows -- so an existing user is
    # unaffected, which is what makes the scope backfill honest.
    #
    # Deliberately NOT a key inside any of the three permission columns:
    # `session_permissions` is overwritten wholesale on every OIDC login (see
    # api/auth._complete_oauth_login), so a grant stored there would vanish the next time
    # the user signed in, intermittently and with nothing to point at.
    #
    # This is a NARROWING list, the opposite of accessor_env_id above. An accessor is
    # confined by a path allowlist and gets nothing else in the dashboard; a user with
    # pov_env_ids is an ordinary user whose POV pages happen to show one POV.
    pov_env_ids = Column(Text, nullable=True)

    # ── Service account (a workload principal, not a person) ─────────────────
    # True means this row is a WORKLOAD: it has no password, cannot sign in interactively,
    # can never be an administrator, and authenticates only with the OAuth 2.0
    # client_credentials grant (`api/oauth`) or a PAT. See services/service_accounts.
    #
    # It flips the empty-map default. Every other row reads "no permissions at all" as
    # UNRESTRICTED for the pre-OIDC users; a service account reads it as NOTHING, in
    # `effective_permissions_dict` below -- the same column-read guard `role_id` uses, so it
    # holds on the detached instance `api/mcp_server` reads. A workload created and then
    # forgotten must hold no access, not all of it.
    #
    # No DEFAULT in the retrofit ALTER for the PostgreSQL reason on `is_admin`; NULL reads
    # as False everywhere, which is every existing row.
    is_service_account = Column(Boolean, default=False, nullable=True)
    # When this account last completed an interactive sign-in: password (and the FIDO2
    # step when MFA applies) or SSO. Stamped when the session token is issued, never per
    # request -- a token used all afternoon is one login. NULL = never signed in since this
    # column existed. Read by services/pov_engagement to answer "has the customer actually
    # opened their POV login?", which a login being ISSUED does not.
    last_login_at = Column(DateTime, nullable=True)

    fido2_credentials = relationship("Fido2Credential", back_populates="user", cascade="all, delete-orphan")
    personal_access_tokens = relationship("PersonalAccessToken", back_populates="user", cascade="all, delete-orphan")

    @property
    def workgroups_list(self) -> List[str]:
        """Parse JSON workgroups into list"""
        if not self.workgroups:
            return []
        try:
            return json.loads(self.workgroups)
        except:
            return []

    @workgroups_list.setter
    def workgroups_list(self, value: List[str]):
        """Set workgroups from list. Names are lowercased to match the canonical
        form stored in the workgroups table. Old TitleCase rows continue to
        resolve via case-insensitive lookups in workgroup_service."""
        normalized = [v.lower() for v in (value or []) if isinstance(v, str)]
        self.workgroups = json.dumps(normalized)

    @property
    def pov_env_ids_list(self) -> List[str]:
        """POV environment ids this user is narrowed to. [] means "not narrowed"."""
        if not self.pov_env_ids:
            return []
        try:
            parsed = json.loads(self.pov_env_ids)
        except Exception:
            return []
        # A non-list here would make `env_id in scope` a substring test, the same trap the
        # permission map has with a bare string value.
        if not isinstance(parsed, list):
            return []
        return [v for v in parsed if isinstance(v, str)]

    @pov_env_ids_list.setter
    def pov_env_ids_list(self, value: List[str]):
        """Set from a list. Empty stores NULL, so "narrowed to nothing" is never
        expressible -- it would read as a user who can see no POV at all, which is what
        withholding the `pov` scope is for."""
        cleaned = [v.strip() for v in (value or []) if isinstance(v, str) and v.strip()]
        self.pov_env_ids = json.dumps(cleaned) if cleaned else None

    @property
    def permissions_dict(self) -> dict:
        """Parse JSON permissions into dict. Empty dict = no explicit permissions (treat as all)."""
        if not self.permissions:
            return {}
        try:
            return json.loads(self.permissions)
        except:
            return {}

    @permissions_dict.setter
    def permissions_dict(self, value: dict):
        """Set permissions from dict. Pass empty dict or None to restore full access."""
        self.permissions = json.dumps(value) if value else None

    @property
    def session_permissions_dict(self) -> dict:
        """Parse the session-scoped permissions JSON. Empty when no OIDC
        groups matched (or non-OIDC user). See effective_permissions_dict
        for the union with the admin-baseline."""
        if not self.session_permissions:
            return {}
        try:
            return json.loads(self.session_permissions)
        except Exception:
            return {}

    @session_permissions_dict.setter
    def session_permissions_dict(self, value: dict):
        self.session_permissions = json.dumps(value) if value else None

    @property
    def jit_permissions_dict(self) -> dict:
        """Permissions granted by an Entitle REST integration. See the column."""
        if not self.jit_permissions:
            return {}
        try:
            return json.loads(self.jit_permissions)
        except Exception:
            return {}

    @jit_permissions_dict.setter
    def jit_permissions_dict(self, value: dict):
        self.jit_permissions = json.dumps(value) if value else None

    @property
    def role_permissions_dict(self) -> dict:
        """The materialised copy of this user's access role. See the column.

        Note what this does NOT do: it never looks the role up. `role_id` is read on a
        detached instance in `api/mcp_server`, and a query here would raise there -- while
        the empty dict such a handler would return means UNRESTRICTED. The copy exists so
        that this is a plain JSON parse with no failure mode.
        """
        if not self.role_permissions:
            return {}
        try:
            return json.loads(self.role_permissions)
        except Exception:
            return {}

    @role_permissions_dict.setter
    def role_permissions_dict(self, value: dict):
        self.role_permissions = json.dumps(value) if value else None

    @property
    def effective_permissions_dict(self) -> dict:
        """Union of admin-baseline (permissions), group-derived
        (session_permissions) and Entitle-granted (jit_permissions).
        This is what require_permission() consults. Special key ``is_admin``
        (bool) is OR'd separately in is_effective_admin; everything else is
        treated as a list of levels per scope and union-merged.

        Empty dict grants nothing. It used to mean unrestricted; the one-time
        ``_materialise_empty_permission_maps`` wrote an explicit map for everyone
        who relied on that, so the rule could change without anyone losing access.

        FOUR sources now. An assigned access role (`role_permissions`) is unioned in
        alongside the other three, which is what makes a role an assignment PLUS an
        optional per-user override rather than a replacement for one.

        The guard below is the load-bearing part of that addition. "No permissions at all"
        means unrestricted, so a user who HOLDS a role but whose materialised copy is
        missing would be promoted to every permission in the dashboard -- the exact
        inversion of what assigning a restrictive role asks for. `role_id` being set is
        therefore answered with a non-empty map that grants nothing (`_ROLE_UNRESOLVED`),
        so the strict-allowlist branch runs instead. It is a column read, so it behaves the
        same on a detached instance as on a live one.
        """
        baseline = self.permissions_dict
        session = self.session_permissions_dict
        jit = self.jit_permissions_dict
        role = self.role_permissions_dict
        if not baseline and not session and not jit and not role:
            if self.role_id or self.is_service_account:
                return dict(_ROLE_UNRESOLVED)
            return {}
        merged = merge_permission_maps(role, baseline, session, jit)
        return narrow_to_token_scope(merged, getattr(self, "_token_scope", None))

    @property
    def is_effective_admin(self) -> bool:
        """True if the persistent is_admin flag, a current session_permissions row,
        a live Entitle grant, or an assigned access role confers admin.

        The role term is required rather than tidy: the built-in `administrator` role's
        map is exactly ``{"is_admin": true}``, so without it that user would hold a
        NON-EMPTY map naming no scopes -- i.e. a strict allowlist of nothing -- and be
        denied everything. `services/role_service` refuses ``is_admin`` on any role but
        that one frozen built-in, which is what keeps "can edit a role" from becoming
        "can mint an administrator".

        A service account is never an administrator, whatever any column says:
        `services/service_accounts` refuses to set it, and this refuses to read it, so a
        role edit or a hand-written row cannot turn a workload into one.
        """
        if self.is_service_account:
            return False
        if bool(self.is_admin):
            return True
        return (bool(self.session_permissions_dict.get("is_admin", False))
                or bool(self.jit_permissions_dict.get("is_admin", False))
                or bool(self.role_permissions_dict.get("is_admin", False)))


class Fido2Credential(Base):
    """FIDO2/WebAuthn credential for MFA"""
    __tablename__ = "fido2_credentials"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id = Column(LargeBinary, nullable=False, unique=True)  # raw bytes from authenticator
    public_key = Column(LargeBinary, nullable=False)  # COSE-encoded public key
    sign_count = Column(Integer, default=0, nullable=False)
    aaguid = Column(String(36))  # authenticator device type GUID (informational)
    device_name = Column(String(100))  # user-provided label e.g. "YubiKey 5C"
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="fido2_credentials")


class PersonalAccessToken(Base):
    """Long-lived API tokens for machine-to-machine access (e.g. GitHub Actions)."""
    __tablename__ = "personal_access_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)           # e.g. "github-actions-prod"
    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256 hex of raw token
    created_at = Column(DateTime, default=utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)         # None = never expires
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="personal_access_tokens")


class OAuthClient(Base):
    """An OAuth 2.0 client_credentials client, bound to exactly one service account.

    The WORKLOAD's credential, where a PAT is a person's. Two things differ, and both are
    the point of it existing:

      * it is not a bearer credential for the API. The secret is exchanged at
        ``POST /api/oauth/token`` for an access token that lives minutes
        (``token_ttl_seconds``), so what crosses the wire on every call expires on its own;
      * the access token names this row (``client_id`` claim), and ``api/auth`` re-reads
        ``is_active`` on every request -- so deactivating a client ends every token it
        issued at once, not when the last one happens to expire.

    SECRET HASH ONLY, and a SHA-256 rather than bcrypt for the reason ``hash_pat`` gives: the
    secret is 256 bits of randomness, so there is nothing for a slow hash to protect, and
    the token endpoint must not be a CPU amplifier. Two hashes so a rotation has no gap:
    ``rotate`` moves the live one to ``previous_secret_hash`` and it keeps working until
    ``previous_expires_at``.
    """
    __tablename__ = "oauth_clients"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Public. Sent in the clear on every token request, so it carries no secret material.
    client_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    secret_hash = Column(String(64), nullable=False, index=True)
    # When the CURRENT secret stops being accepted. Never NULL from the dashboard: a
    # workload secret with no end date is the PAT problem again.
    secret_expires_at = Column(DateTime, nullable=True)
    previous_secret_hash = Column(String(64), nullable=True)
    previous_expires_at = Column(DateTime, nullable=True)
    token_ttl_seconds = Column(Integer, nullable=False, default=900)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    # How the client authenticates at the token endpoint. NULL/"secret" = the secret above.
    # "spiffe_jwt" = a JWT-SVID for `spiffe_id`, verified against that trust domain's keys
    # (services/spiffe_assertion) -- the client then has NO usable secret: `secret_hash`
    # holds the hash of a value that was generated and never shown, and
    # `authenticate_client` refuses secret auth for it regardless.
    auth_method = Column(String(20), nullable=True)
    spiffe_id = Column(String(500), nullable=True, index=True)


class ExternalWorkloadIdentity(Base):
    """An identity at an external IdP that authenticates AS a service account.

    The workload keeps no dashboard secret at all: it presents an access token its own IdP
    issued (Entra, Okta, Keycloak -- client credentials, or a managed identity), and
    ``services/external_workload`` verifies it against that IdP's JWKS and resolves the
    service account mapped here.

    KEYED ON ``(issuer, subject)`` -- the token's ``sub`` -- AND NEVER ON ``azp``/``appid``.
    In a client-credentials token ``sub`` is the client's own identity (Entra: the service
    principal's object id; Okta: the client id; Keycloak: the client's service-account
    user). A PERSON signing in through the same app registration gets a token with the
    same ``azp`` and their OWN ``sub``, so a mapping on ``azp`` would let every user of that
    app act as the workload. ``expected_client`` is a second, optional check on top.
    """
    __tablename__ = "external_workload_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_external_workload_iss_sub"),)

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    issuer = Column(String(500), nullable=False)
    subject = Column(String(255), nullable=False)
    # If set, the token's azp / appid / client_id / cid must equal it as well.
    expected_client = Column(String(255), nullable=True)
    name = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)


class RunnerCredentialGrant(Base):
    """One run's managed-account credential, waiting for its ECS / Cloud Run task to
    collect it (services/runner_credential; docs/design/dashboard-workload-identity.md,
    Slice 5).

    The alternative this replaces copies the credential into AWS or GCP Secrets Manager
    for the task to read at launch. Here it stays in the dashboard, encrypted with the
    dashboard's own key, for at most ``expires_at`` — ten minutes — and leaves only
    SEALED to a key the task generated, after the task proves with its own cloud identity
    that it is the configured runner. A row is single-use (``redeemed_at``) and deleted
    when the run ends, redeemed or not. Only the token's SHA-256 is stored.
    """
    __tablename__ = "runner_credential_grants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    job_id = Column(String(36), nullable=False, index=True)
    runner = Column(String(16), nullable=False)          # ecs | gcp
    values_enc = Column(Text, nullable=False)            # config_service._encrypt(JSON)
    expires_at = Column(DateTime, nullable=False)
    redeemed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class SpiffeTrustDomain(Base):
    """Where the dashboard gets a SPIFFE trust domain's JWT-SVID signing keys.

    Two sources, and a row uses whichever it has -- the URL when set, else the bundle:

      * ``jwks_url`` -- fetched live and cached (SPIRE's OIDC Discovery Provider ``/keys``,
        or any JWKS endpoint), with ``ca_pem`` pinning its TLS when it is served by a
        private CA. Always current across SPIRE's key rotation;
      * ``bundle_json`` -- a stored SPIFFE bundle (``spire-server bundle show -format
        spiffe``), pasted or captured from a SPIRE lab row (``spire_lab_id``). It goes
        stale when SPIRE rotates its JWT keys, which is why ``bundle_captured_at`` is
        kept and shown.

    Only keys marked ``use: jwt-svid`` are used from a bundle; its X.509 roots are not
    signing keys for a JWT.
    """
    __tablename__ = "spiffe_trust_domains"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trust_domain = Column(String(255), nullable=False, unique=True, index=True)
    jwks_url = Column(String(500), nullable=True)
    ca_pem = Column(Text, nullable=True)
    # The name the JWKS URL's certificate is checked against, and the Host sent, when it
    # differs from the URL's host. A Workload Lab registers https://<ip>:8443/keys with
    # tls_server_name=oidc.<trust-domain>: that name is the provider's certificate SAN and
    # its `domains` entry, and it resolves only on the lab's own hosts.
    tls_server_name = Column(String(255), nullable=True)
    bundle_json = Column(Text, nullable=True)
    bundle_captured_at = Column(DateTime, nullable=True)
    spire_lab_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    updated_at = Column(DateTime, nullable=True)


class SpireAdminCall(Base):
    """One ``spire-server`` CLI call, relayed through the database to a process that can
    reach the dashboard's SPIRE server (services/spire_relay).

    On a managed container runtime the SPIRE server is a sidecar of the APP, and its admin
    socket is a volume only that pod can see — the worker is a separate deployment, and no
    platform shares a volume between two of them. Everything the worker asks the server
    for (its own JWT-SVIDs, a Packer build assertion, a Workload Lab federation) goes in a
    row here; an app process claims it with the same ``UPDATE … WHERE status='pending'``
    rowcount the job queue uses, runs it, and writes the result back. The requester reads
    it and deletes the row.

    ``stdin`` and both output columns are Fernet-encrypted (config_service.encrypt_value):
    the output of ``token generate`` and ``jwt mint`` is a credential.
    """
    __tablename__ = "spire_admin_calls"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # The CLI arguments after `spire-server`, as a JSON list. Built by code, never by a
    # request; the server side still refuses a subcommand outside its allow-list.
    args_json = Column(Text, nullable=False)
    stdin_enc = Column(Text, nullable=True)
    timeout_s = Column(Integer, nullable=False, default=30)
    # pending -> running -> done. Indexed because the server polls on it.
    status = Column(String(16), nullable=False, default="pending", index=True)
    returncode = Column(Integer, nullable=True)
    stdout_enc = Column(Text, nullable=True)
    stderr_enc = Column(Text, nullable=True)
    requested_by = Column(String(255), nullable=True)
    served_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    claimed_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class SpireAdminRelay(Base):
    """A process currently serving :class:`SpireAdminCall` rows, and when it last said so.

    A requester checks for a fresh row BEFORE it queues a call, so an install with no
    SPIRE server at all fails at once with a sentence, rather than after a full timeout in
    every background loop that asks.
    """
    __tablename__ = "spire_admin_relays"

    # host:pid — one row per serving process.
    id = Column(String(255), primary_key=True)
    transport = Column(String(16), nullable=False)
    seen_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class LoginAttempt(Base):
    """One FAILED password login. Successes are not recorded here — a successful login
    deletes the username's rows, so the table only ever holds the evidence of failure.

    In the database rather than in a process, because gunicorn runs two workers: an
    in-memory counter would give an attacker double the allowance and reset it on every
    redeploy. Same reasoning as the job claim and the notification outbox.

    Doubles as the only record that a brute-force attempt happened at all — before this
    existed, a failed login left no trace anywhere in the system.
    """
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Case-folded by login_guard.normalize_username, so `Admin` and `admin` cannot each
    # get their own budget. Recorded whether or not the account exists — a throttle that
    # engaged only for real users would answer "does this user exist?".
    username = Column(String(150), nullable=False, index=True)
    ip = Column(String(45), nullable=False, default="", index=True)
    # Indexed because every query is a range scan on it, in both the check and the sweep.
    attempted_at = Column(DateTime, default=utcnow, nullable=False, index=True)
