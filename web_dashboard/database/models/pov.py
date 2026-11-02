"""Proof-of-value (POV) environment models."""
import json
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey, UniqueConstraint, Float

from ...utils.clock import utcnow
from .._engine import Base


class PovEnvironment(Base):
    """A customer POV environment on a lab platform.

    An OT demo cell can get away with using its child deploy job row as its record —
    it is one VM. A POV is N VMs the dashboard did not individually create, it lives
    for weeks, and later slices hang per-VM PAM artifacts off it, so it gets a real
    table like CloudDatabase and K8sCluster do.

    **There is deliberately no customer field.** A POV is identified by ``workgroup``
    (which RBAC, the inventory filters and the expiry exempt list already key off),
    ``created_by``, and an operator-chosen ``name`` slug. Adding a customer column
    later should be treated as a schema regression: a free-text "customer" box is
    where a company name eventually gets typed, and this database is not approved to
    hold it. See docs/profiles/pov/README.md.

    Every platform call is keyed on ``platform_environment_id``, **never on name**, so
    an SE may rename an environment on the platform to whatever their approvals allow
    and nothing here notices.
    """
    __tablename__ = "pov_environments"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    platform = Column(String(32), nullable=False)          # lab_platforms.VALID_PLATFORMS
    name = Column(String(255), nullable=False)             # operator-chosen slug
    # The platform's own id. A string on purpose: Skytap returns ids numeric in some
    # payloads and string in others, and 42 != "42" is a lookup that finds nothing.
    platform_environment_id = Column(String(64), nullable=True, index=True)
    template_id = Column(String(64), nullable=True)
    template_name = Column(String(255), nullable=True)
    region = Column(String(64), nullable=True)
    project_id = Column(String(64), nullable=True)

    # Dashboard-side lifecycle, distinct from the platform's runstate below.
    #   provisioning -> active -> destroying -> destroyed   (or failed at any point)
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    # The platform's view: running | suspended | stopped | halted | busy | ""
    runstate = Column(String(32), nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    # The provision job, so the UI can link to its Live Output.
    provision_job_id = Column(String(36), nullable=True, index=True)
    # A failed job surfaces only its error_message, so this carries the remedy too.
    error_message = Column(Text, nullable=True)

    # Slice 3+ fills these: the in-environment broker VM, its enrolled agent, the
    # Gateway and Password Safe Resource Broker installed on it per POV.
    broker_vm_id = Column(String(64), nullable=True)
    broker_agent_id = Column(String(36), nullable=True)
    gateway_name = Column(String(255), nullable=True)
    # Whether this POV's PRA vendor Group Policy requires TOTP (two_factor_type =
    # require_totp). NULL/False is PRA's default, "optional". See pov_vendor_access.
    pra_require_totp = Column(Boolean, nullable=True, default=False)
    # The PRA Gateway id, set ONLY when the dashboard created the Gateway itself
    # (pov_gateway.provision). It is the ownership marker teardown reads: a Gateway an
    # operator created and pasted the key for has no id here and is never deleted.
    pra_gateway_id = Column(String(64), nullable=True)
    ps_application_host_id = Column(Integer, nullable=True)
    # PRA <-> Password Safe, as last checked (services/pov_pra_ps_link): how many of this
    # POV's Password Safe accounts PRA can see, how many are linked to their jump item,
    # and when. COUNTS so the setup ladder reads them with no network call. NULL checked_at
    # means "never checked", which the ladder offers as Check rather than calling done.
    pra_ps_matched_count = Column(Integer, nullable=True)
    pra_ps_linked_count = Column(Integer, nullable=True)
    pra_ps_checked_at = Column(DateTime, nullable=True)

    # Which BeyondTrust tenant this POV is wired into, one FK per product. Three rather
    # than one because they are independent: PRA and Password Safe are per-customer,
    # Entitle is usually one shared tenant. See BeyondTrustTenant.
    #
    # ``ondelete="SET NULL"`` and the service refuses to delete a tenant a live POV still
    # references, so the constraint is the backstop and not the rule. Note SQLite does not
    # enforce foreign keys unless ``PRAGMA foreign_keys=ON`` per connection and nothing
    # here sets it — which is why bt_tenant_service validates every id it is handed rather
    # than trusting the column, exactly as ``agent_service.delete_agent`` does. The
    # constraint is also declared for ``create_all`` only: these three columns already
    # shipped bare, and the lightweight migrations in ``init_db`` add columns, never
    # constraints, so an existing table keeps them unconstrained and correct.
    pra_tenant_id = Column(String(36),
                           ForeignKey("beyondtrust_tenants.id", ondelete="SET NULL"),
                           index=True, nullable=True)
    ps_tenant_id = Column(String(36),
                          ForeignKey("beyondtrust_tenants.id", ondelete="SET NULL"),
                          index=True, nullable=True)
    entitle_tenant_id = Column(String(36),
                               ForeignKey("beyondtrust_tenants.id", ondelete="SET NULL"),
                               index=True, nullable=True)

    # Which use cases this POV tests -- the "Use cases chosen" step of its Setup tab. A
    # comma-separated subset of pra,password_safe,entitle (a Password-Safe-only POV, a
    # Total PASM POV of PRA + Password Safe, ...) plus a JSON list of card ids the SE
    # unticked. NULL products means nobody has chosen yet, which reads as "the whole
    # catalog", so every POV from before this landed keeps the checklist it had. See
    # services/pov_use_case_scope.
    use_case_products = Column(String(64), nullable=True)
    use_case_excluded = Column(Text, nullable=True)

    # The Entitle REST integration that mints this POV's accessors, registered against
    # THIS POV's own Entitle tenant. One per POV rather than one per instance, because the
    # asset is the POV: an instance-wide integration would have to name every customer's
    # environment as an asset in one tenant, which is the cross-tenant shape the registry
    # exists to prevent.
    #
    # The terraform state is what `deregister` destroys. Kept even when a destroy fails,
    # for the same reason the per-VM wire-up states are: clearing it optimistically is how
    # an integration in a customer's tenant becomes unreachable from here.
    accessor_integration_id = Column(String(64), nullable=True)
    accessor_tf_state = Column(Text, nullable=True)

    # The PRA Asset Group this POV's jump items live in, when it has one of its own.
    #
    # NULL means "use the tenant's appliance-wide Asset Group", which is what every POV
    # wired before this column existed does, and the fallback pov_wireup.tenant_override
    # still honours. It is a per-POV group at all because a PRA Group Policy grants access
    # BY ASSET GROUP: a vendor attached to the tenant-wide group would reach every POV on
    # that appliance, which is the whole reason the vendor feature could not just reuse it.
    #
    # The name and the id are both stored. The name is what terraform resolves through the
    # `sra_jump_group_list` data source when it builds jump items; the id is what the
    # Config API deletes and what proves a same-named group found later is OURS rather
    # than one an operator built by hand.
    pra_jump_group_name = Column(String(255), nullable=True)
    pra_jump_group_id = Column(String(36), nullable=True)

    # PRA Vendor Onboarding, per POV: one Group Policy scoped to the Asset Group above, and
    # one Vendor Group whose `default_policy` is that policy. Both are ids the appliance
    # assigned, held as text like every other foreign id on this table.
    #
    # Kept when a teardown fails, for the same reason accessor_tf_state is: clearing them
    # optimistically is how a vendor group in a customer's appliance becomes unreachable
    # from here, and a vendor group is a live door into their network.
    pra_vendor_policy_id = Column(String(36), nullable=True)
    pra_vendor_group_id = Column(String(36), nullable=True)
    # This dashboard's own copy of when the vendor group's users stop working. PRA takes a
    # DAY COUNT and enforces it on its side, so this is not a second clock — it is what
    # lets the row say "expires Friday" without a round trip, and what the reconcile sweep
    # reaps on. Kept for the same reason PovAccessor.expires_at is: an appliance that goes
    # unreachable, or a group somebody edits by hand, would otherwise leave standing
    # third-party access into a customer's estate with nothing here aware of it.
    pra_vendor_expires_at = Column(DateTime, nullable=True)

    # Slice 7: the customer-facing share link. The platform's own publish set is the
    # source of truth for whether the URL still works; these three record what THIS
    # dashboard published, so it can revoke exactly that one later.
    share_url = Column(Text, nullable=True)
    share_id = Column(String(64), nullable=True)
    # When the link dies. Stored rather than re-read from the platform because the POV
    # list renders it per row, and a share link with no expiry is the failure this slice
    # exists to prevent — see services/pov_share. NULL = no link published.
    share_expires_at = Column(DateTime, nullable=True)

    # What the reconcile sweep last read back off the platform. Written by
    # services/pov_reconcile and by nothing else, so a stale value is always "the sweep
    # has not run", never "some other writer disagreed".
    #
    # These exist because `runstate` above was, until the sweep landed, written ONLY at
    # provision and at an explicit power action. So `suspend_on_idle` — the single biggest
    # lever on lab-platform spend — would suspend a POV and this row would go on claiming
    # it was running forever. The page gates its Start/Suspend buttons on that value, so
    # the cost feature working correctly was exactly what broke the controls.
    platform_seen_at = Column(DateTime, nullable=True)
    # The platform's own throttle flag. On the row rather than only in the live read
    # because the managed table is where the buttons are, and "this will be slow" belongs
    # next to the button, not on the read-only table beside it.
    rate_limited = Column(Boolean, default=False, nullable=True)
    # The environment was not visible on the platform at the last sweep, AND a direct read
    # of it answered 404. Two signals rather than one on purpose: the listing is
    # project-scoped, so absence from it can mean "outside the configured project" rather
    # than "gone", and marking a live customer environment as missing on that evidence
    # would be the same false positive `verify` refuses to make.
    #
    # Never acted on automatically. It is a flag for a human, because the row still holds
    # the only record of what this POV was wired into.
    platform_missing = Column(Boolean, default=False, nullable=True)
    # The platform's idle timer, in seconds, as last read. Display-only today: it is set
    # at provision from the create form and there is currently no way to change it after,
    # which is worth being able to SEE before it is worth being able to edit.
    suspend_on_idle_seconds = Column(Integer, nullable=True)

    # The dashboard-driven suspend schedule, for platforms that have no idle timer of
    # their own (every public cloud). NULL throughout = no schedule, which is what every
    # existing row backfills to and what a Skytap POV keeps forever.
    #
    # A schedule rather than an inactivity timer because "idle" on a cloud has no honest
    # definition from outside the guest: every candidate signal — a PRA session, an agent
    # heartbeat, a page view — has a blind spot that either leaves a POV running all month
    # or suspends one mid-demo.
    suspend_at_local = Column(String(5), nullable=True)     # "HH:MM"
    resume_at_local = Column(String(5), nullable=True)      # "HH:MM"; NULL = never wake it
    schedule_timezone = Column(String(64), nullable=True)   # IANA, e.g. Europe/London
    # Seven characters, Monday first, '1' = the schedule applies that day.
    schedule_days = Column(String(7), nullable=True)
    # When the schedule was last evaluated for this row. **The schedule acts on a BOUNDARY
    # CROSSED since this timestamp, never on "is it currently outside working hours?"** —
    # which is what lets an SE start a POV by hand at 8pm for a demo and keep it, instead
    # of having it suspended again by the next sweep four minutes later. It is also what
    # makes a dashboard that was down for three hours do the right thing rather than the
    # last three things. NULL means "never evaluated": the first pass records the time and
    # acts on nothing, because an unbounded window has crossed every boundary there is.
    schedule_last_checked_at = Column(DateTime, nullable=True)
    # A ONE-OFF start, for a demo: "have it running by 14:00 Thursday". Naive UTC, the
    # moment to start the lab (the demo time minus a lead), not the demo time itself --
    # that is kept in extra_data["wake"] for display. NULL = nothing booked. Cleared by the
    # sweep the first time it is evaluated at or after this moment, whether or not that
    # pass had to act, so a booking fires at most once. Unlike the recurring schedule it
    # also applies on a platform with its own idle timer: it only ever STARTS a lab.
    wake_at = Column(DateTime, nullable=True)


    # The spend cap. NULL throughout = no cap, which is what every existing row backfills
    # to — so enabling this on an estate selects nothing by construction rather than by a
    # guard, exactly as `expires_at IS NULL` does.
    #
    # The auto-delete timer answers "how long may this POV live"; this answers the
    # question an operator on their own cloud account actually loses sleep over. A clock is
    # a poor proxy for money: the same fortnight is twenty dollars or two thousand
    # depending on what the template asked for.
    spend_cap_usd = Column(Float, nullable=True)
    # Accrued, not billed. Every reconcile pass adds `rate now × time since the last pass`.
    # A real bill lags a day and costs money to read, so a cap driven off one would report
    # a runaway rather than stop it — see services/spend_policy for the whole argument. It is
    # a LIST-PRICE estimate and everything user-facing says so.
    spend_estimate_usd = Column(Float, nullable=True, default=0.0)
    # When the accrual last ran. NULL = never measured, and the first pass records the time
    # and adds nothing: an unbounded interval would bill this POV for every hour since the
    # epoch. Same latch discipline as `schedule_last_checked_at`.
    spend_accrued_at = Column(DateTime, nullable=True)
    # Warn-once and act-once latches. Raising the cap clears BOTH, which is what makes
    # "give it another fifty dollars" work rather than leaving a POV permanently flagged.
    spend_warned_at = Column(DateTime, nullable=True)
    # Set when the cap suspended this POV, so the sweep does not re-suspend it on every
    # pass while an operator is deciding what to do.
    spend_capped_at = Column(DateTime, nullable=True)

    # The Password Safe functional accounts THIS dashboard minted for this POV, one per
    # guest OS. NULL means "none minted" — either the tenant names existing accounts (a
    # name there is used as-is and nothing is created) or the wire-up has not run.
    #
    # Recorded because deletion has to be exact: only an id written here is deleted at
    # teardown, which is what keeps a cleanup from removing the functional account a
    # customer's whole tenant uses. See services/pov_functional_account.
    ps_linux_functional_account_id = Column(Integer, nullable=True)
    ps_windows_functional_account_id = Column(Integer, nullable=True)

    # Slice 8: the auto-delete timer. NULL = never, exactly as elsewhere — so enabling
    # expiry on an existing estate selects zero rows.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)
    # The warning LADDER's latch. `expiry_warned_at` alone is warn-once, so with five
    # stages the first warning would burn it and the other four would never fire.
    # Holds the tightest stage already warned, in minutes; NULL = none yet.
    warned_stage_minutes = Column(Integer, nullable=True)

    extra_data = Column(Text, nullable=True)   # JSON string, like Job.extra_data

    @property
    def metadata_dict(self) -> dict:
        if not self.extra_data:
            return {}
        try:
            return json.loads(self.extra_data)
        except Exception:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, value: dict):
        self.extra_data = json.dumps(value)


class PovEnvironmentVM(Base):
    """One VM inside a POV environment.

    Written from the platform read at provision time, and refreshed on demand. The
    PAM artifact columns are filled by the wire-up slice, each persisted the moment
    the artifact exists so teardown is exact rather than re-derived — a re-derived id
    is how you delete the wrong thing.
    """
    __tablename__ = "pov_environment_vms"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    environment_id = Column(String(36), index=True, nullable=False)
    platform_vm_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=True)
    # "linux" | "windows" | "" — blank rather than a wrong guess, because a confident
    # wrong answer sends a Windows VM down the Password-Safe-over-SSH path.
    os_family = Column(String(16), nullable=True)
    runstate = Column(String(32), nullable=True)
    private_ip = Column(String(64), nullable=True)
    published_services = Column(Text, nullable=True)   # JSON list

    @property
    def published_services_list(self) -> list:
        if not self.published_services:
            return []
        try:
            return json.loads(self.published_services)
        except Exception:
            return []

    @published_services_list.setter
    def published_services_list(self, value: list):
        self.published_services = json.dumps(value or [])

    # Slice 6 (PAM wire-up). One column per artifact, plus its terraform state.
    pra_jump_id = Column(String(64), nullable=True)
    pra_jump_tf_state = Column(Text, nullable=True)
    vault_account_id = Column(String(64), nullable=True)
    vault_tf_state = Column(Text, nullable=True)
    ps_managed_system_id = Column(String(64), nullable=True)
    ps_managed_account_id = Column(String(64), nullable=True)
    ps_registration_tf_state = Column(Text, nullable=True)
    entitle_integration_id = Column(String(64), nullable=True)
    entitle_tf_state = Column(Text, nullable=True)
    wiring_error = Column(Text, nullable=True)

    # Which of this guest's stored platform credentials its runs should use, when the
    # lab platform holds several and the guest-OS rule in `services/pov_credentials`
    # cannot separate them. A USERNAME, never a credential: the password is still read
    # live off the platform at the moment of every run and is still stored nowhere here.
    #
    # NULL throughout, and NULL is the normal state — it means "decide by guest OS",
    # which is right for every VM whose box holds the `root` or `administrator` the
    # template promised. So this backfills to the rule rather than to a refusal, and an
    # existing estate needs nothing typed into it.
    login_username = Column(String(104), nullable=True)

    # What an operator says this guest runs, when the lab platform's own read said
    # nothing. "linux" | "windows" | NULL.
    #
    # A SEPARATE column rather than a correction of `os_family`, because
    # `pov_env_service.refresh_vms` rewrites that one from the platform on every refresh
    # — a correction written there would survive exactly until the next read. Same reason
    # `login_username` above and the PAM artifact columns are their own columns.
    #
    # NULL is the normal state and means "believe the platform", so this backfills to
    # today's behaviour and an existing estate needs nothing typed into it. Blank still
    # means UNKNOWN everywhere downstream: this does not soften the "never guess an OS"
    # rule, it gives an operator the only way to END the blank that rule leaves behind.
    os_family_override = Column(String(16), nullable=True)

    # What this guest IS in a demo-cell sense, when it is one: "ot-sim" | "vyos" | NULL.
    # See services/pov_cell_roles.py. NULL is the normal state and means "an ordinary
    # target", so this backfills to today's wire-up exactly.
    #
    # On the VM row rather than read through from the template, because a Skytap POV has
    # no dashboard-side template to read it from — an operator says it per guest, the way
    # `os_family_override` is said. A cloud POV seeds it from its template VM of the same
    # name (`pov_cell_roles.seed_from_template`), once, and never overwrites an answer.
    cell_role = Column(String(32), nullable=True)
    # The PRA items a cell role adds beyond the base jump item, each with its own
    # Terraform state, as a JSON object keyed by item ("hmi", "tunnel:modbus", ...).
    # JSON rather than a column per item because the set differs per role and grows with
    # the protocol list; nothing filters on it. Written the moment each item exists, for
    # the reason every other artifact column here is.
    cell_artifacts = Column(Text, nullable=True)

    @property
    def cell_artifacts_dict(self) -> dict:
        if not self.cell_artifacts:
            return {}
        try:
            value = json.loads(self.cell_artifacts)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @cell_artifacts_dict.setter
    def cell_artifacts_dict(self, value: dict):
        self.cell_artifacts = json.dumps(value) if value else None

    @property
    def guest_os(self) -> str:
        """"linux" | "windows" | "" — the operator's answer first, else the platform's.

        The one place the POV feature answers "what does this guest run". Every selector
        and every wire-up path reads this rather than `os_family`, so an override reaches
        all of them at once; `os_family` stays the platform's own untouched answer, which
        is what the VMs tab shows beside the control.

        Normalised here so ~20 call sites stop repeating `(x or "").strip().lower()`.
        """
        return ((self.os_family_override or "").strip().lower()
                or (self.os_family or "").strip().lower())

    created_at = Column(DateTime, default=utcnow, nullable=False)


class PovUseCaseProgress(Base):
    """One use-case card, ticked off on one POV.

    A POV runs for weeks across many sessions, so "which of these have we actually shown
    them?" is state, not something to reconstruct from memory at the next call. The card
    catalog itself stays in ``services/personas`` -- this table holds only what somebody
    did with it.

    **A row exists only for a card somebody touched.** Absence means "not started", so
    adding a card to the registry needs no backfill and enabling this on an existing estate
    selects zero rows -- the same reading ``expires_at`` has everywhere else here. It also
    means a card RETIRED from the registry leaves a row behind, which is deliberate:
    ``services/pov_use_cases`` simply does not render it, because deleting an SE's history
    on a copy edit is a worse outcome than an invisible row.

    ``note`` and ``checked_by_kind`` ship before anything writes them. The prospect-facing
    accessor page is a later slice and will write both; putting the columns on the row now
    means that slice needs no migration, exactly as ``PovEnvironment`` carried its wire-up
    columns from slice 1.
    """
    __tablename__ = "pov_use_case_progress"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    environment_id = Column(String(36), index=True, nullable=False)
    # A UseCase.id from `pov_cards` or `pov_runbooks`, validated against those registries
    # before any write -- see pov_use_cases.set_state. An unvalidated column here would be
    # a free-text store.
    card_id = Column(String(64), nullable=False)
    # Which GROUP's list it came from -- a `pov_cards` product key, or a `pov_runbooks` key
    # once a POV is being run against a published procedure. Denormalised from the registry
    # so a card that later moves between groups keeps the answer it was ticked under, which
    # is what the SE actually remembers. The two registries are disjoint by card id, so one
    # column answers both without ambiguity.
    #
    # Was `persona`, back when the checklist was grouped by role. Nothing READS this
    # column -- `pov_summary` groups off the live registry -- and `set_state` rewrites it on
    # every save, so the rows written under the old grouping self-correct the next time
    # their card is touched. The rename is in the legacy list (schema_legacy.py).
    group_key = Column(String(32), nullable=False)
    # done | skipped. `skipped` is a real answer, not an absence: "we showed them and it
    # did not land" and "we never got to it" are different things to walk into a renewal
    # conversation with.
    state = Column(String(16), nullable=False, default="done")
    note = Column(Text, nullable=True)
    checked_by = Column(String(100), nullable=True)
    # se | accessor. The prospect's own ticks must be distinguishable from the SE's, or the
    # evidence a POV produces is just a list of boxes with no author.
    checked_by_kind = Column(String(16), nullable=False, default="se")
    checked_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("environment_id", "card_id",
                                       name="uq_pov_use_case_progress_env_card"),)


class PovSignoff(Base):
    """Somebody confirming what a POV covered: the close of a technical evaluation.

    The checklist says what was ticked; this says that a named person -- the SE, or the
    customer through their accessor login -- looked at that list and agreed it was what
    they evaluated. It is evidence, so it is never deleted: a withdrawn sign-off keeps its
    row with ``revoked_at`` set, and the report shows both.

    ``coverage_json`` is a SNAPSHOT of what was covered at the moment of signing, and
    ``summary_hash`` its fingerprint (``services/pov_signoff``). A checklist ticked further
    after somebody signed is then visible as such -- the sign-off is "for an earlier
    version" -- rather than silently stretching to cover work it never saw.

    **No free-text column, and no customer field.** The same rule ``PovEnvironment``
    states: this database is not approved to hold a company name, and a comment box on a
    sign-off is exactly where one gets typed. ``signed_by`` is a dashboard username, which
    for an accessor is the generated ``pov_<slug>_<random>`` login, never their email.
    """
    __tablename__ = "pov_signoffs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    environment_id = Column(String(36), index=True, nullable=False)
    signed_by = Column(String(100), nullable=False)
    # se | accessor -- pov_use_cases.KIND_SE / KIND_ACCESSOR, as on the progress rows.
    signed_by_kind = Column(String(16), nullable=False, default="se")
    signed_at = Column(DateTime, default=utcnow, nullable=False)
    coverage_json = Column(Text, nullable=False)
    summary_hash = Column(String(64), nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(100), nullable=True)


class PovAccessor(Base):
    """One prospect's ephemeral login into one POV.

    The POV feature's first principal that is not somebody in the account. Everything else
    a POV publishes is a door into the LAB -- a share link, a jump item -- and this is a
    door into the DASHBOARD, which is why it is the artifact teardown removes first.

    The account itself is a row in ``users`` carrying ``accessor_env_id``, so login, MFA,
    the sign-in throttle and audit are the ones already here rather than a second
    implementation of each. This table is the binding and its provenance: which POV, who
    or what created it, when it dies.

    **Revoking DELETES the user row** and stamps ``revoked_at`` here. Deactivating instead
    would leave a username in ``users`` that every actor lookup in the codebase has to
    remember to skip -- and one that forgets is an escalation, not a cosmetic bug.
    """
    __tablename__ = "pov_accessors"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    environment_id = Column(String(36), index=True, nullable=False)
    # The users row while it exists. Kept after revoke as the record of what was deleted;
    # NOT a ForeignKey, because the row it names is deliberately gone by then.
    user_id = Column(String(36), index=True, nullable=True)
    username = Column(String(100), nullable=False)
    email = Column(String(200), nullable=True)
    full_name = Column(String(200), nullable=True)

    # "manual" (an SE minted it from the POV page) | "entitle" (create_actor did).
    source = Column(String(16), nullable=False, default="manual")
    # What Entitle called it, when Entitle made it. Ephemeral mode owns expiry on its side
    # and calls delete_actor; these let a delete_actor find the row it means.
    entitle_request_id = Column(String(64), nullable=True)
    entitle_actor_id = Column(String(128), nullable=True, index=True)

    # This dashboard's own clock, and it is not optional. Entitle owning expiry is not the
    # same as this instance knowing when to stop trusting a login: an integration that is
    # removed, misconfigured or simply never calls back would otherwise leave a working
    # credential behind forever. Clamped so an accessor can never outlive its POV.
    expires_at = Column(DateTime, index=True, nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoke_reason = Column(String(255), nullable=True)
    # The accessor's own last sign-in, copied here from the login (api/auth._stamp_login).
    # Kept on THIS row rather than read off `users` because revoking an accessor deletes
    # its user, and "did the customer ever open it?" is exactly what a finished POV's
    # record needs to answer. NULL = never signed in.
    last_login_at = Column(DateTime, nullable=True)


class PovVendorUser(Base):
    """One third party's login into one POV's lab, held in the customer's PRA appliance.

    The sibling of :class:`PovAccessor` and its opposite: an accessor is a door into THIS
    DASHBOARD, and a vendor user is a door into the customer's NETWORK. That is the whole
    difference, and it is why this is the artifact the destroy removes first.

    The account itself does not live here. PRA owns it — created under
    ``/api/config/v1/vendor/{id}/user``, expiring on the Vendor Group's own
    ``account_expiration`` clock, deleted with the group. This table is the binding and its
    provenance: which POV, who created it, what it was for, and the PRA id needed to delete
    exactly that one.

    ``expires_at`` is a COPY of what the appliance will enforce, not a second clock. PRA's
    ``VendorUser.account_expiration`` is read-only and derived from the group, so the
    dashboard cannot shorten one user's life — it can only tell an SE when the group's will
    end. Do not add a sweep that deletes on this column: it would be guessing at a date the
    appliance owns.
    """
    __tablename__ = "pov_vendor_users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    environment_id = Column(String(36), index=True, nullable=False)
    # What PRA called it. Recovered by re-reading the vendor group's user list after the
    # create, because POST /vendor/{id}/user answers 200 with no body.
    pra_vendor_user_id = Column(String(36), nullable=True)
    # Always carries the povvnd_ prefix; revoke refuses any name without it, so this path
    # can never be talked into deleting a vendor the customer added by hand.
    username = Column(String(64), nullable=False)
    email = Column(String(256), nullable=True)
    full_name = Column(String(200), nullable=True)

    expires_at = Column(DateTime, index=True, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoke_reason = Column(String(255), nullable=True)


class PovTemplateBuild(Base):
    """One run of the template builder: a lab-platform template this dashboard authored.

    A POV is a template instantiated whole, so until this table existed the whole feature
    was downstream of a catalogue nobody could author from here. The builder closes that:
    instantiate a base template, prepare the result (install the metadata runner the
    template contract requires), and save it back as a new template.

    **``build_environment_id`` is committed the moment the platform returns it**, before
    anything else in the job can fail. That is the same rule ``PovEnvironment`` follows and
    for the same reason: a scratch environment that exists on the platform and not in this
    database is the one failure mode nothing can clean up automatically — and unlike a POV,
    this one is *supposed* to be short-lived, so an orphan bills for weeks unnoticed.

    The row survives its environment on purpose. Once ``result_template_id`` is set the
    scratch environment is reaped and only the build record remains, which is what lets the
    page answer "where did this template come from?" long after.
    """
    __tablename__ = "pov_template_builds"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    platform = Column(String(32), nullable=False)      # lab_platforms.VALID_PLATFORMS
    # The name the finished template gets. Slug-constrained by the API for the same reason
    # PovEnvironment.name is: it reaches the platform's display name and job output.
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    base_template_id = Column(String(64), nullable=True)
    base_template_name = Column(String(255), nullable=True)
    project_id = Column(String(64), nullable=True)

    # The scratch environment. String for the same reason PovEnvironment's is: Skytap
    # returns ids numeric in some payloads and string in others, and 42 != "42" finds
    # nothing. NULL once it has been reaped.
    build_environment_id = Column(String(64), nullable=True, index=True)
    # Kept even after the environment is gone, so a finished row can still say which
    # environment produced it.
    build_environment_was = Column(String(64), nullable=True)

    broker_vm_name = Column(String(255), nullable=True)
    broker_vm_id = Column(String(64), nullable=True)

    result_template_id = Column(String(64), nullable=True, index=True)
    result_template_name = Column(String(255), nullable=True)

    #   building -> preparing -> baking -> ready      (or failed at any point)
    # `discarded` is the terminal state of a row whose scratch environment was reaped by
    # hand without a template ever being baked.
    status = Column(String(32), nullable=False, default="building", index=True)
    # The template-contract report, as a JSON list of {check, status, detail}. Persisted
    # rather than re-derived: it describes the environment at bake time, and that
    # environment is gone by the time anybody reads the row.
    contract_report = Column(Text, nullable=True)
    # How the runner got onto the broker VM: "ssh" | "skipped" | "failed" | "".
    # Distinct from `status` because a build that bakes with the runner NOT installed is a
    # success with a caveat, not a failure — the operator can still paste the script in.
    prepare_method = Column(String(16), nullable=True)
    prepare_detail = Column(Text, nullable=True)

    error_message = Column(Text, nullable=True)
    job_id = Column(String(36), nullable=True, index=True)
    # Leave the scratch environment up after a failure so it can be inspected by hand.
    # Off by default: a running environment bills, and the common case is a build nobody
    # is watching.
    keep_build_environment = Column(Boolean, default=False, nullable=False)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    @property
    def contract_list(self) -> list:
        if not self.contract_report:
            return []
        try:
            return json.loads(self.contract_report)
        except Exception:
            return []

    @contract_list.setter
    def contract_list(self, value: list):
        self.contract_report = json.dumps(value or [])


class PovBlueprint(Base):
    """A saved POV recipe: everything the create form asks, under one name.

    The POV create form has eleven fields, three of which are tenant references whose right
    answer is the same every time for a given kind of POV. A blueprint records one correct
    set so building the next POV is a choice rather than eleven, and so two SEs building
    "the same" POV actually build the same POV.

    It is deliberately **not** a second provision path. ``ProvisionRequest.blueprint_id``
    supplies defaults for fields the request does not set; the request still wins field by
    field, and everything after that is the one existing provision job. A blueprint that
    forked the flow would be a second place for the provision rules to drift.

    **There is no customer field here either**, for the reason ``PovEnvironment`` gives at
    length: a blueprint is a shape of POV, not a customer's POV.
    """
    __tablename__ = "pov_blueprints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False, index=True)   # operator-chosen slug
    description = Column(Text, nullable=True)
    platform = Column(String(32), nullable=False)

    # What to build from. `template_name` is denormalised so a blueprint still reads
    # sensibly when the template has been deleted from the platform — the id alone would
    # leave the page showing a number nobody recognises.
    template_id = Column(String(64), nullable=False)
    template_name = Column(String(255), nullable=True)
    project_id = Column(String(64), nullable=True)
    # Set when this blueprint was saved straight off a template the builder produced. Only
    # a provenance link; nothing keys on it.
    source_build_id = Column(String(36), nullable=True)

    broker_vm_name = Column(String(255), nullable=True)
    suspend_on_idle_seconds = Column(Integer, nullable=True)

    # The suspend schedule a POV from this blueprint starts with. Same four fields as
    # PovEnvironment, minus its evaluation latch — that is per-environment state, not part
    # of a recipe.
    suspend_at_local = Column(String(5), nullable=True)
    resume_at_local = Column(String(5), nullable=True)
    schedule_timezone = Column(String(64), nullable=True)
    schedule_days = Column(String(7), nullable=True)

    # The spend cap a POV from this blueprint starts with. NULL = no
    # cap, the same as everywhere else.
    spend_cap_usd = Column(Float, nullable=True)

    # Which BeyondTrust tenants a POV from this blueprint is wired into. Same three-FK
    # shape and the same ondelete as PovEnvironment — see the note there about SQLite not
    # enforcing FKs, which is why bt_tenant_service validates every id it is handed.
    pra_tenant_id = Column(String(36),
                           ForeignKey("beyondtrust_tenants.id", ondelete="SET NULL"),
                           index=True, nullable=True)
    ps_tenant_id = Column(String(36),
                          ForeignKey("beyondtrust_tenants.id", ondelete="SET NULL"),
                          index=True, nullable=True)
    entitle_tenant_id = Column(String(36),
                               ForeignKey("beyondtrust_tenants.id", ondelete="SET NULL"),
                               index=True, nullable=True)

    # The use cases a POV from this blueprint tests. Same two columns, same NULL meaning,
    # as PovEnvironment.use_case_products.
    use_case_products = Column(String(64), nullable=True)
    use_case_excluded = Column(Text, nullable=True)

    # The non-secret configuration for the two in-environment installs, pre-filled onto a
    # POV at provision time so the Gateway and Resource Broker panels open ready to press.
    #
    # **No secret is stored on a blueprint, and nothing here auto-runs.** Both installs go
    # through the broker agent, which does not exist until minutes after a provision, and
    # both need a credential that is per-tenant rather than per-recipe — a PRA deploy key
    # and the Password Safe installer key. Copying those into a saved recipe would spread
    # two secrets across every blueprint an SE ever makes, to save one paste. So a blueprint
    # carries the half that is safe to repeat and leaves the half that is not to the person
    # pressing the button, who by then is looking at the panel anyway.
    gateway_name = Column(String(255), nullable=True)
    rb_windows_vm_name = Column(String(255), nullable=True)
    rb_zone = Column(String(255), nullable=True)
    rb_asset = Column(String(255), nullable=True)

    # NULL means "use expiry_policy.pov_default_hours", not "never". A blueprint that could
    # silently mean never would be the one way to opt a POV out of the auto-delete timer
    # without saying so.
    expiry_hours = Column(Integer, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow,
                        nullable=False)


class PovCloudTemplate(Base):
    """A POV template on a public cloud: the topology, declared rather than captured.

    Skytap's template is a saved environment — disks and all — and creating from it is one
    call. No public cloud has that, so this table **is** the template: a named list of VMs
    to build, on a network to build them in. A cloud adapter's ``list_templates()`` reads
    these rows, which is what lets one create form, one blueprint table and one provision
    job serve both kinds of platform.

    **Declared, not captured, on purpose.** Baking N machine images (AMI / Managed Image /
    Machine Image / Custom Image) would be the faithful analogue, and it is slow, region-
    locked, cloud-specific, and a standing storage bill for every template anyone ever
    saves. A spec that references the image catalog costs nothing to keep and ports to the
    next cloud unchanged — and the catalog's standard images, built once per region by
    ``api/pov_images``, are what most templates reference. ``source_environment_id`` is
    here for the slice that adds per-template baking
    so that slice needs no migration — the convention every ``pov_*`` table has followed
    since slice 1.

    Not to be confused with :class:`PovBlueprint`, which is a saved set of *form answers*
    and names a template. This is the template.
    """
    __tablename__ = "pov_cloud_templates"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # The cloud this template builds on. A template is deliberately NOT portable between
    # clouds: an instance type and an image id are both cloud-specific, so `aws` and
    # `azure` versions of "the same" POV are two rows, and the create form only ever
    # offers the selected cloud's. Guessing a translation is how a build fails ten minutes
    # in with a shape error nobody can read.
    cloud = Column(String(16), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)   # operator-chosen slug
    description = Column(Text, nullable=True)

    # Where it builds. NULL = the instance's configured default region, resolved at
    # provision. Most templates do not care, and one pinned to a region the account has no
    # quota in is a failure minutes into a build rather than at the form.
    region = Column(String(64), nullable=True)

    # The environment's own private network, CIDR only. One network per POV rather than a
    # shared one: two customers' evaluations must not share a broadcast domain, and a
    # per-POV network is also what makes the tag-scoped teardown exact.
    network_cidr = Column(String(64), nullable=True)

    # Purdue zones: three subnets (enterprise, DMZ, plant) instead of one, each guest
    # placed by its cell role, with the flows between them limited. See
    # services/pov_zones.py. NULL/False builds the single subnet every template built
    # before this, so it backfills to today's topology.
    purdue_zones = Column(Boolean, nullable=True)

    # Provenance for the bake slice. Only a link; nothing keys on it.
    source_environment_id = Column(String(36), nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow,
                        nullable=False)


class PovCloudTemplateVM(Base):
    """One VM in a cloud POV template.

    A child table rather than a JSON column on the template, for the reason this file
    gives wherever the choice comes up: there is no portable JSON filter across SQLite and
    Postgres, and "which templates use this image?" is a question the bake slice and the
    image registry will both want to ask.
    """
    __tablename__ = "pov_cloud_template_vms"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    template_id = Column(String(36), index=True, nullable=False)
    name = Column(String(255), nullable=False)

    # "broker" | "target". At most ONE broker per template: it is the VM the dashboard
    # agent, the Gateway and the Resource Broker all land on, and the provision job builds
    # it LAST so its enrolment code is minted against a boot that is about to happen
    # rather than one that already timed out.
    role = Column(String(16), nullable=False, default="target")

    # A demo-cell role this VM plays, when it plays one: "ot-sim" | "vyos" | NULL. A
    # SEPARATE field from `role` on purpose — `role` decides build order and where the
    # agent lands, and a cell VM is still an ordinary target in both senses. Copied onto
    # the POV's VM row at refresh; see PovEnvironmentVM.cell_role.
    cell_role = Column(String(32), nullable=True)

    # "linux" | "windows". Required here, unlike PovEnvironmentVM where blank means "the
    # platform would not say" — the person writing a template knows, and the wire-up sends
    # a Windows VM down an entirely different path.
    os_family = Column(String(16), nullable=False, default="linux")

    # What to boot. Exactly one of these two is set, enforced by the service:
    #   image_ref - a registered_images.id. The per-cloud id is resolved from that row's
    #               `promotions` at provision time, so one template row survives a
    #               re-promote and a template can be written before the image exists in
    #               the target cloud.
    #   image_id  - a raw cloud image id (ami-…, a gallery version, a GCE image name) for
    #               when the catalog holds nothing and a literal is the honest answer.
    image_ref = Column(String(36), nullable=True)
    image_id = Column(String(255), nullable=True)

    instance_type = Column(String(64), nullable=False)
    disk_gb = Column(Integer, nullable=True)
    # Render and build order. The broker's position is decided by `role`, not by this.
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=utcnow, nullable=False)
