"""``init_db`` and its SQLite twin: create the tables, apply the ledger, seed the data.

Every name that lives in the package (``engine``, ``SessionLocal``, ``Base``,
``_DDL_LOCK_TIMEOUT_MS``, ``_is_sqlite`` and the RBAC backfills) is read through ``_pkg`` at
call time, never bound by ``from . import``. Tests rebind those names on
``web_dashboard.database`` (``_DDL_LOCK_TIMEOUT_MS`` to make a contended ALTER give up fast,
``SessionLocal`` to a fake) and the rebind has to reach this code.
"""
from sqlalchemy import text

from .. import database as _pkg


# How many times init_db re-runs create_all on SQLite after losing the race below.
_SQLITE_CREATE_ATTEMPTS = 5


def _create_all_sqlite(conn) -> None:
    """``create_all`` for SQLite, tolerating a second process creating tables meanwhile.

    PostgreSQL serializes concurrent init_db callers with an advisory lock; SQLite has
    none. The image's ``gunicorn -w 2`` boots two workers on the same fresh file, both
    pass create_all's "does it exist" check, and the slower one's CREATE TABLE fails
    with "table ... already exists" -- which aborts that worker's startup, and gunicorn
    then takes the whole container down. Re-running create_all re-checks, so it skips
    whatever the other worker created and makes only what is still missing.
    """
    import time as _time
    from sqlalchemy.exc import OperationalError
    for attempt in range(_SQLITE_CREATE_ATTEMPTS):
        try:
            _pkg.Base.metadata.create_all(bind=conn)
            return
        except OperationalError as exc:
            if "already exists" not in str(exc) or attempt == _SQLITE_CREATE_ATTEMPTS - 1:
                raise
            conn.rollback()
            _time.sleep(0.2 * (attempt + 1))


def init_db():
    """Initialize database — create all tables and run lightweight migrations.

    On PostgreSQL, multiple Gunicorn workers start concurrently and both call
    init_db().  A TRANSACTION-scoped advisory lock serializes them so only one worker
    runs the DDL at a time; the second worker proceeds after the first commits,
    at which point create_all's checkfirst logic skips existing tables. (It was
    session-level once; see the comment at the lock for why that wedged cold
    app+worker co-deploys and must not come back.)

    Every lock wait in here is bounded by _pkg._DDL_LOCK_TIMEOUT_MS, so a migration that
    cannot get its table lock is SKIPPED and retried next boot rather than queued --
    read the comment at the SET LOCAL before shortening or removing that, because an
    unbounded wait here takes the entire app down, not just this transaction.

    On PostgreSQL, a failed ALTER TABLE aborts the enclosing transaction — use
    savepoints per migration so a "column already exists" error doesn't prevent
    subsequent migrations from running.
    """
    with _pkg.engine.connect() as conn:
        if not _pkg._is_sqlite:
            # Transaction-scoped lock: serializes concurrent init_db callers (the
            # app's Gunicorn workers AND the jobs_worker container) and releases
            # when this transaction commits below. A *session*-level
            # pg_advisory_lock leaks here: QueuePool keeps the connection open
            # after this block, so the lock would be held for the life of the
            # process and wedge every other caller (seen as app workers blocked
            # forever acquiring 20260101 once the jobs_worker held it).
            conn.execute(text("SELECT pg_advisory_xact_lock(20260101)"))

            # Bound every lock wait this transaction makes. The advisory lock above
            # only serializes init_db against ITSELF; it says nothing about the row
            # and table locks the DDL below needs, and those are the ones that bite.
            #
            # `ALTER TABLE jobs ...` wants ACCESS EXCLUSIVE, so it queues behind any
            # open transaction touching `jobs` -- and the jobs_worker holds exactly
            # those, across network I/O, while it runs a job. The part that turns a
            # wait into an outage is that **PostgreSQL lock queues are FIFO: a
            # *pending* ACCESS EXCLUSIVE request blocks every LATER reader of that
            # table too.** So one queued ALTER stalls every `SELECT ... FROM jobs`
            # in the app, and because the routes serving those SELECTs are `async
            # def` running synchronous SQLAlchemy, the stall lands on the event loop
            # and takes the whole Gunicorn worker with it -- `/api/health` included.
            # That is the 2026-09-16 pov.weaverlab.app outage: every path timing out
            # at the 240s ingress limit, 0% CPU, and an idle database.
            #
            # With a bound, a contended migration fails fast instead: the
            # per-statement savepoint below rolls it back, the remaining migrations
            # still run, and the skipped one is retried on the next boot (they are
            # all idempotent "add column if missing" statements). Availability of the
            # whole app beats applying one additive column this boot.
            #
            # SET LOCAL, never a plain SET: LOCAL is scoped to this transaction and is
            # gone at the commit below. A plain SET would outlive it on this pooled
            # connection (QueuePool hands the same one out again) and silently put a
            # 4s lock-wait ceiling on every ordinary application query that later
            # borrowed it -- a far wider behaviour change than intended. LOCAL also
            # needs an open transaction to do anything, which is satisfied: the
            # advisory lock above started one.
            conn.execute(text(f"SET LOCAL lock_timeout = '{_pkg._DDL_LOCK_TIMEOUT_MS}ms'"))

        # Pass the connection so create_all runs inside the same transaction
        # (and the same advisory-lock session on PostgreSQL).
        if _pkg._is_sqlite:
            _pkg._create_all_sqlite(conn)
        else:
            _pkg.Base.metadata.create_all(bind=conn)

        # Which schema changes this database has already recorded (schema_migrations).
        # create_all has just made `schema_markers` if this is a new install.
        from .. import schema_migrations as _schema
        _done = _schema.applied(conn)

        # The legacy statement list, frozen in schema_legacy.py: once a database records
        # _schema.LEGACY_KEY it never reads the list again, so a new schema change is a
        # Migration in schema_migrations.MIGRATIONS instead.
        from .. import schema_legacy as _legacy
        # Migrations that never applied because of LOCK CONTENTION rather than because
        # the column was already there. Collected rather than raised: one contended
        # statement must not stop the other ~60 from applying. Each entry is
        # (sqlstate, statement) so the warning can say which of the two it was.
        _not_applied: list[tuple[str, str]] = []

        # Run only until this database has recorded that the list fully applied: on
        # PostgreSQL even a no-op statement takes ACCESS EXCLUSIVE on its table until the
        # commit below, which is the lock pile-up described above.
        _pending = [] if _schema.LEGACY_KEY in _done else _legacy.STATEMENTS
        for stmt in _pending:
            if _pkg._is_sqlite:
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception:
                    pass  # column already present
            else:
                # Use a savepoint per statement: on failure PostgreSQL puts the
                # transaction into an aborted state; rolling back to the savepoint
                # recovers it so the remaining migrations can still run.
                conn.execute(text("SAVEPOINT _mig"))
                try:
                    conn.execute(text(stmt))
                    conn.execute(text("RELEASE SAVEPOINT _mig"))
                except Exception as exc:  # noqa: BLE001 — see the docstring
                    conn.execute(text("ROLLBACK TO SAVEPOINT _mig"))
                    # Nearly every exception here is the expected "column already
                    # exists", which is why this block swallows by default. TWO codes
                    # must not be silent, because both mean the statement NEVER RAN --
                    # so the column is genuinely still missing and some later reader
                    # fails on it in a way that points nowhere near here:
                    #
                    #   55P03 lock_not_available  -- the SET LOCAL lock_timeout above
                    #                                gave up waiting for the table.
                    #   40P01 deadlock_detected   -- PostgreSQL picked THIS transaction
                    #                                as the victim of a lock cycle.
                    #
                    # The deadlock case is not theoretical and is nastier than the
                    # timeout, because the two are NOT symmetrical: a timeout is always
                    # ours, a deadlock kills one of the two participants and which one is
                    # PostgreSQL's choice. On 2026-09-25 a co-deployed jobs_worker
                    # claimed an `expiry_sweep` (holding `jobs`, then wanting
                    # `hypervisor_vm_cache`) while this transaction held
                    # `hypervisor_vm_cache` and wanted ACCESS EXCLUSIVE on `jobs`.
                    # PostgreSQL chose the sweep, so the migration applied and only a job
                    # died. Had it chosen init_db, the ALTER would have been rolled back
                    # to the savepoint and swallowed here with NO log line at all -- an
                    # UndefinedColumn from a random route days later, with nothing
                    # anywhere connecting it to a deploy.
                    #
                    # Note this transaction accumulates ACCESS EXCLUSIVE on every table
                    # it touches and holds them all until the commit below, INCLUDING for
                    # the no-op "column already exists" statements, which is what makes
                    # it a plausible deadlock partner in the first place.
                    _code = getattr(getattr(exc, "orig", None), "pgcode", None)
                    if _code in ("55P03", "40P01"):
                        _not_applied.append((_code, stmt))

        if _not_applied:
            # WARNING, not an exception: the app boots and serves, which is the whole
            # point of the timeout. But say exactly what was skipped and why, because
            # the eventual symptom is an UndefinedColumn error from a random route.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "init_db: %d migration(s) skipped -- lock contention with another "
                "transaction, usually the jobs_worker mid-job (55P03 = gave up after "
                "%dms waiting for the table; 40P01 = this transaction was chosen as a "
                "deadlock victim). They are idempotent and will be retried on the next "
                "boot; stopping the worker before the next deploy applies them. "
                "Skipped: %s",
                len(_not_applied), _pkg._DDL_LOCK_TIMEOUT_MS,
                "; ".join(f"[{code}] {stmt}" for code, stmt in _not_applied))

        # Record the list once everything it adds is present (or say what is not), then
        # apply the numbered migrations made since it was frozen.
        if _pending:
            _schema.finish_legacy(conn, _pkg.Base.metadata, _legacy.STATEMENTS, _not_applied)
        _schema.apply_numbered(conn)

        # On PostgreSQL this ends the txn → releases pg_advisory_xact_lock(20260101); on
        # SQLite it commits the ledger rows written just above.
        conn.commit()

# Seed workgroups table on first boot. Imported here (not at module top)
    # to avoid a circular import: workgroup_service imports from database.
    from ..services import workgroup_service
    with _pkg.SessionLocal() as _seed_db:
        workgroup_service.seed_if_empty(_seed_db)
        # The built-in access roles, and then a repair pass over every user's
        # materialised copy of the role they hold. Data, not DDL, so outside the
        # advisory-locked transaction for the same reason as every seed below.
        #
        # Two calls and not one because they answer different questions: `seed_builtins`
        # inserts what is missing and never updates, while `reconcile` rewrites a copy that
        # has drifted from its role (a hand-edited database, a restore, an interrupted
        # deploy) and clears a `role_id` whose role is gone. Neither needs a schema marker:
        # the seed cannot re-grant because it skips the row it would touch, and reconcile
        # only ever writes what the role already says.
        # Before the seed and reconcile, not after: reconcile rewrites every user's copy of
        # their role FROM the role row, so the rows have to be clean first. Own try, so a
        # failure here cannot skip the seed that follows.
        try:
            _pkg._retire_unenforced_levels(_seed_db)
        except Exception:  # noqa: BLE001 — a migration must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "retired permission level cleanup skipped", exc_info=True)
        # Same placement and reason: reconcile below copies the role to its holders.
        try:
            _pkg._grant_presenter_own_pov(_seed_db)
        except Exception:  # noqa: BLE001 — a migration must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "POV Presenter own-POV grant skipped", exc_info=True)
        try:
            _pkg._grant_read_only_demo_read(_seed_db)
        except Exception:  # noqa: BLE001 — a migration must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Read-Only demo read grant skipped", exc_info=True)
        try:
            _pkg._narrow_presenter_to_own_povs(_seed_db)
        except Exception:  # noqa: BLE001 — a migration must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "POV Presenter own-only narrowing skipped", exc_info=True)
        # Every later change to a built-in's permissions, applied once each. Same placement
        # and reason as the one-off fix-ups above: reconcile copies the roles to holders.
        try:
            _pkg._apply_builtin_role_changes(_seed_db)
        except Exception:  # noqa: BLE001 — a migration must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "built-in role changes skipped", exc_info=True)
        try:
            from ..services import role_service
            role_service.seed_builtins(_seed_db)
            role_service.reconcile(_seed_db)
        except Exception:  # noqa: BLE001 — a seed must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "access role seed/reconcile skipped", exc_info=True)
        # Widen every explicit permission map to cover the scopes added when the catalog
        # grew from 14 entries to one-per-nav-section. Data, not DDL, and outside the
        # advisory-locked transaction for the same reason as the seeds below it.
        try:
            _pkg._backfill_new_permission_scopes(_seed_db)
        except Exception:  # noqa: BLE001 — a backfill must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "permission scope backfill skipped", exc_info=True)
        # After the scope backfill, so the maps it writes already carry the new scopes. A
        # failure here is logged loudly and retried at the next boot (no marker is written):
        # until it succeeds, users who relied on an empty map are denied, not widened.
        try:
            _pkg._materialise_empty_permission_maps(_seed_db)
        except Exception:  # noqa: BLE001 — a backfill must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).error(
                "empty-permission migration failed; users with no permission map are "
                "denied until it succeeds", exc_info=True)
        # Move stored secrets onto CONFIG_ENCRYPTION_KEY when one is set (a no-op otherwise).
        # Idempotent, so two processes booting together at worst both write a valid token.
        try:
            from ..services import config_service as _cs
            _cs.reencrypt_stored_secrets(_seed_db)
        except Exception:  # noqa: BLE001 — a backfill must never stop the app booting
            _seed_db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "stored-secret re-encryption skipped; the legacy key still reads them",
                exc_info=True)
        # Copy the legacy singleton hypervisor config into hypervisor_connections.
        # Here and not in the migration block above on purpose: this is a data seed,
        # not DDL, and it must stay OUTSIDE the advisory-locked transaction — a
        # session-level lock around it is the QueuePool leak that hung cold
        # app+worker co-deploys. The unique constraint arbitrates the race instead.
        try:
            from ..services import hypervisor_connection_service
            hypervisor_connection_service.seed_from_settings(_seed_db)
        except Exception:  # noqa: BLE001 — a seed must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "hypervisor connection seed skipped", exc_info=True)
        # Copy the legacy singleton BeyondTrust config into beyondtrust_tenants. Only
        # ever does anything on a POV instance in practice — a demo install keeps
        # resolving the singletons through bt_tenant_service.resolve's step-4 fallback and
        # never reads a row — but it is unconditional so that switching a demo install to
        # the POV profile finds its existing tenant already registered rather than an
        # empty page and three credentials to re-enter by hand.
        try:
            from ..services import bt_tenant_service
            bt_tenant_service.seed_from_settings(_seed_db)
        except Exception:  # noqa: BLE001 — a seed must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "BeyondTrust tenant seed skipped", exc_info=True)
        # Translate the retired `beyondtrust_enabled` flag into the three product flags
        # that replaced it. Writes app_config only, so it takes no db session — it lives
        # here because this is the block that runs after the schema is ready and outside
        # the advisory lock.
        try:
            from ..services import feature_flag_migration
            feature_flag_migration.seed_beyondtrust_split()
        except Exception:  # noqa: BLE001 — a seed must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "BeyondTrust flag split seed skipped", exc_info=True)

    # One-time: chain any pre-existing (pre-upgrade) audit rows so the whole
    # history is tamper-evident, not just entries written after this upgrade.
    # Guarded + advisory-locked inside the service; a no-op once done.
    from ..services import job_service
    with _pkg.SessionLocal() as _audit_db:
        try:
            n = job_service.backfill_audit_chain(_audit_db)
            if n:
                print(f"Audit chain: backfilled {n} pre-existing entr{'y' if n == 1 else 'ies'}.")
        except Exception as e:  # never block startup on backfill
            print(f"Audit chain backfill skipped: {e}")

    # One-time: move an existing chain onto the V2 hash form, which covers ip_address.
    # Verifies under the old form BEFORE rewriting and refuses a table that is already
    # broken — see job_service.rechain_audit_log for why re-blessing one would be worse
    # than leaving it alone.
    with _pkg.SessionLocal() as _rechain_db:
        try:
            r = job_service.rechain_audit_log(_rechain_db)
            if r.get("status") == "rechained":
                print(f"Audit chain: re-hashed {r['count']} entries onto chain v2.")
            elif r.get("status") == "refused":
                print("Audit chain: NOT migrated — the existing chain does not verify "
                      f"(first broken seq {r.get('first_broken_seq')}). Left untouched; "
                      "check GET /api/audit/verify.")
        except Exception as e:  # never block startup
            print(f"Audit chain migration skipped: {e}")

    # One-time: copy each provisioned cloud database's catalog out of its provisioning
    # job onto the row itself. Here rather than in the migration block above for the
    # reason spelled out at the hypervisor seed: this is data, not DDL, and it must stay
    # OUTSIDE the advisory-locked transaction. Convergent, so it takes no lock of its own.
    with _pkg.SessionLocal() as _dbname_db:
        try:
            # Inside the try, unlike the audit chain above: cloud_database_service pulls
            # in terraform + region_config, and a heavy import that fails must not be
            # the thing that stops the app booting either.
            from ..services import cloud_database_service as _clouddb
            n = _clouddb.backfill_provisioned_db_names(_dbname_db)
            if n:
                print(f"Cloud databases: backfilled {n} database name(s).")
        except Exception as e:  # never block startup on backfill
            print(f"Cloud database name backfill skipped: {e}")

    # One-time: classify pre-existing hypervisor jobs as check-in or operator work, so
    # /jobs can hide the syncs that are already there rather than only the ones written
    # from now on. Data, not DDL, so it stays outside the advisory lock above for the
    # same reason the two backfills before it do. Convergent, batched, and resumable.
    with _pkg.SessionLocal() as _checkin_db:
        try:
            n = job_service.backfill_job_checkins(_checkin_db)
            if n:
                print(f"Jobs: classified {n} pre-existing check-in row(s).")
        except Exception as e:  # never block startup on backfill
            print(f"Job check-in backfill skipped: {e}")

    print("Database initialized successfully!")
