"""
FastAPI application entry point for the VM CLI Web Dashboard.
"""
import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: F401
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import settings
from .logging_context import (
    LOG_FORMAT, install_log_correlation, new_request_id,
    reset_correlation_id, set_correlation_id,
)
from .database import SessionLocal, User, create_admin_user, init_db
from .services import cache_service
from .services import config_service, feature_flags
from .services import public_url

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs(settings.log_dir, exist_ok=True)

# Install the correlation-id LogRecord factory before basicConfig so every record
# carries `cid` for the %(cid)s field in LOG_FORMAT (see logging_context).
install_log_correlation()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(settings.log_dir, "api.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── Application lifecycle ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    logger.info("Infrastructure Management Dashboard starting up...")
    init_db()
    logger.info("Database initialised.")

    # Reconcile jobs orphaned by a prior restart (their in-process background task
    # died with the worker) so they don't linger as zombie 'running' rows and leave
    # their cluster/DB resource row stuck on 'provisioning'. Non-fatal.
    try:
        from .database import SessionLocal
        from .services import job_service
        _rdb = SessionLocal()
        try:
            n = job_service.reconcile_stale_jobs(_rdb)
            if n:
                logger.warning("Reconciled %d stale job(s) orphaned by a prior restart.", n)
        finally:
            _rdb.close()
    except Exception as exc:
        logger.warning("Stale-job reconcile failed (non-fatal): %s", exc)

    _bootstrap_first_run_admin()

    warmers = [
        asyncio.create_task(_warm_aws_amis(),               name="warm_aws_amis"),
        asyncio.create_task(_warm_aws_network_opts(),       name="warm_aws_network_opts"),
        asyncio.create_task(_warm_aws_instances(),          name="warm_aws_instances"),
    ]
    azure_configured = bool(
        config_service.get("azure_client_id") or settings.azure_client_id
    )
    if azure_configured:
        warmers += [
            asyncio.create_task(_warm_azure_images(),       name="warm_azure_images"),
            asyncio.create_task(_warm_azure_network_opts(), name="warm_azure_network_opts"),
        ]
    # Portainer warmer — always launched; each pass no-ops cleanly while the
    # feature is disabled or unconfigured, so enabling Portainer in Settings
    # starts the sweeps without an app restart.
    warmers.append(
        asyncio.create_task(_warm_portainer_containers(), name="warm_portainer_containers")
    )

    # Cloud-identity JIT sweeper (Phase 4a) — reconciles entitle_activations
    # against Entitle's view. Loop always launched; sweeper no-ops cleanly
    # when the master gate / sweep flag is off, so a runtime flag flip
    # activates the next pass without an app restart.
    warmers.append(
        asyncio.create_task(_ci_sweeper_loop(), name="ci_sweeper_loop")
    )

    # Auto-delete timer sweeper. Loop always launched; it no-ops while the feature is
    # off, so flipping it on in Settings activates the next pass without an app restart.
    warmers.append(
        asyncio.create_task(_hypervisor_sync_loop(), name="hypervisor_sync_loop")
    )
    warmers.append(
        asyncio.create_task(_expiry_sweeper_loop(), name="expiry_sweeper_loop")
    )
    # Cost-summary warmer — always launched; no-ops (no billable calls) while the
    # cost feature is off, so flipping the flag in Settings warms the next pass.
    warmers.append(
        asyncio.create_task(_warm_cost_summary(), name="warm_cost_summary")
    )
    # Dashboard tile snapshot — SECONDARY collector. dash-worker is the primary; this one
    # only takes over where there is no worker at all. See _warm_dashboard_stats.
    warmers.append(
        asyncio.create_task(_warm_dashboard_stats(), name="warm_dashboard_stats")
    )

    # Ephemeral-secret GC — reap any managed-account ephemeral cloud secrets a prior
    # run leaked (a crash between create and its finally-cleanup). No-op unless the
    # feature is enabled; runs off-thread so blocking cloud calls don't stall startup.
    async def _ephemeral_gc_startup():
        try:
            from .services import config_service as cs
            if not cs.get_bool("ansible_cloud_ephemeral_secrets_enabled"):
                return
            from .services import ephemeral_gc
            await asyncio.to_thread(ephemeral_gc.sweep)
        except Exception:
            logger.warning("startup ephemeral GC sweep failed (non-fatal)", exc_info=True)
    warmers.append(
        asyncio.create_task(_ephemeral_gc_startup(), name="ephemeral_gc_startup")
    )

    # The same shape, for a Password Safe credential a remote agent's job was holding when
    # the process died. The job runner sweeps on its own timer; this covers the deployment
    # where the app restarts and the runner does not, and it costs one query when no agent
    # connection uses Password Safe at all.
    async def _agent_credential_release_startup():
        try:
            from .services import agent_ps_credential_service
            db = SessionLocal()
            try:
                await asyncio.to_thread(agent_ps_credential_service.sweep, db)
            finally:
                db.close()
        except Exception:
            logger.warning("startup agent credential release failed (non-fatal)",
                           exc_info=True)
    warmers.append(
        asyncio.create_task(_agent_credential_release_startup(),
                            name="agent_credential_release_startup")
    )

    yield

    for task in warmers:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Shut down the thread-pool executor without blocking — avoids the
    # "executor did not finish joining threads within 300s" RuntimeWarning
    # that occurs when pwsh subprocesses are still running at reload/shutdown.
    loop = asyncio.get_running_loop()
    executor = getattr(loop, "_default_executor", None)
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)

    logger.info("Infrastructure Management Dashboard shutting down.")


def _bootstrap_first_run_admin() -> None:
    """Create the first admin account from FIRST_RUN_ADMIN_* env vars if the
    users table is empty. No-op when a password isn't supplied or users
    already exist — keeps prod clusters untouched."""
    if not settings.first_run_admin_password:
        return
    db = SessionLocal()
    try:
        if db.query(User).first() is not None:
            return
    finally:
        db.close()
    try:
        create_admin_user(
            settings.first_run_admin_username,
            settings.first_run_admin_password,
        )
        logger.info(
            "First-run admin '%s' created from FIRST_RUN_ADMIN_* env vars.",
            settings.first_run_admin_username,
        )
    except Exception as exc:
        logger.error("First-run admin bootstrap failed: %s", exc)


# ── Cloud-identity JIT sweeper loop (Phase 4a) ───────────────────────────────

async def _ci_sweeper_loop() -> None:
    """Background reconciliation of entitle_activations against Entitle's view.

    Sleep cadence comes from cloud_identity_sweep_interval_minutes (default
    60). Sweeper short-circuits when the master gate or sweep-enabled flag
    is off; loop is launched unconditionally so a runtime flag flip
    activates the next pass without an app restart.
    """
    from .database import SessionLocal
    from .services import cloud_identity_sweeper_service as ci_sweeper

    while True:
        try:
            db = SessionLocal()
            try:
                await asyncio.to_thread(ci_sweeper.sweep_once, db)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cloud-identity sweeper iteration failed: %s", exc)
        try:
            interval = ci_sweeper.sweep_interval_seconds()
        except Exception:
            interval = 60 * 60
        await asyncio.sleep(interval)


# ── Auto-delete timer sweeper loop ───────────────────────────────────────────

async def _expiry_sweeper_loop() -> None:
    """Enqueue one auto-delete (resource expiry) sweep per interval.

    This loop ONLY enqueues — it must never call ``sweep_once`` itself. The app runs
    under ``gunicorn -w 2``, so every task started here runs twice; letting
    ``jobs_worker._claim_one``'s ``UPDATE ... WHERE status='pending'`` rowcount decide the
    winner is what makes a pass single-flight across both app workers AND the three
    worker replicas, on SQLite as well as PostgreSQL. It also puts the destructive half of
    the feature in the worker process, where every other long/destructive operation runs,
    and gives each pass a job row on /jobs with Live Output and cancel.

    Cadence is read live each iteration so a Settings change takes effect on the next
    pass without an app restart (same contract as _ci_sweeper_loop).
    """
    from .database import SessionLocal
    from .services import expiry_policy, expiry_reaper

    while True:
        try:
            db = SessionLocal()
            try:
                await asyncio.to_thread(expiry_reaper.enqueue_sweep_if_due, db)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("auto-delete sweep enqueue failed: %s", exc)
        try:
            interval = expiry_policy.sweep_interval_seconds()
        except Exception:
            interval = 30 * 60
        await asyncio.sleep(interval)


# ── Hypervisor inventory sync loop ───────────────────────────────────────────

async def _hypervisor_sync_loop() -> None:
    """Enqueue one inventory_sync per due agent-bound hypervisor connection.

    Same shape and the same reasoning as _expiry_sweeper_loop: this ONLY enqueues.
    Under ``gunicorn -w 2`` every task here runs twice, and letting
    ``agent_service.lease_one``'s ``UPDATE ... WHERE status='queued' AND agent_id=:id``
    rowcount decide the winner is what makes a pass single-flight across both workers.
    It is emphatically not a jobs_worker handler — ``agent_hypervisor`` must stay
    disjoint from HANDLED_TYPES or the local worker would race the agent for the row.

    Always launched, and a no-op when remote agents are off or no connection is bound to
    one, so flipping the flag in Settings activates the next pass with no restart.
    """
    from .database import SessionLocal
    from .services import hypervisor_sync_service

    while True:
        try:
            db = SessionLocal()
            try:
                queued = await asyncio.to_thread(
                    hypervisor_sync_service.enqueue_due_syncs, db)
                if queued:
                    logger.info("queued %d hypervisor inventory sync(s)", queued)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("hypervisor sync enqueue failed: %s", exc)
        try:
            interval = max(60, int(config_service.get("hypervisor_sync_poll_seconds")
                                   or 300))
        except (TypeError, ValueError):
            interval = 300
        await asyncio.sleep(interval)


# ── Background cache warmers ──────────────────────────────────────────────────

# Every warmer below sources its fetcher AND its cache key from the api module that
# serves the same key. That is deliberate and load-bearing: a warmer holding its own
# copy of either one drifts silently. Both failure modes have happened here —
# a second copy of the AWS instance fetch dropped `region`/`workgroup`/`key_name`
# and blanked the list for every non-admin on each pass, and the network-options
# warmers wrote key_global() keys the key_param() readers never look at. Warm what
# the reader reads, by calling the reader's own code.
#
# tests/test_cache_warmer_parity.py pins warmer↔reader agreement.

async def _warm_loop(name: str, fetcher, key_fn, ttl: int) -> None:
    """Fetch → cache → sleep(ttl * 0.8) → repeat forever."""
    interval = int(ttl * 0.8)
    while True:
        try:
            data = await fetcher()
            await cache_service.set(key_fn(), data, ttl)
            logger.debug("cache warmed key=%s", key_fn())
        except Exception as exc:
            logger.warning("cache warmer %s failed: %s", name, exc)
        await asyncio.sleep(interval)


async def _warm_scoped_loop(name: str, scope_fn, fetcher, key_fn, ttl: int) -> None:
    """Like _warm_loop, for caches keyed per region/location.

    Resolves the scope ONCE per pass and hands the same value to both the fetcher
    and the key builder, so the key can never describe a different region than the
    data stored under it. Re-resolving each pass is the point: the scope comes from
    config_service, so a Setup-wizard region change is picked up without a restart.
    """
    interval = int(ttl * 0.8)
    while True:
        try:
            scope = scope_fn()
            data = await fetcher(scope)
            key = key_fn(scope)
            await cache_service.set(key, data, ttl)
            logger.debug("cache warmed key=%s", key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cache warmer %s failed: %s", name, exc)
        await asyncio.sleep(interval)


async def _warm_cost_summary() -> None:
    """Pre-populate the cost tile + /costs page (account summary and the
    dashboard-managed breakdown). Skips the (billable, rate-limited) cloud calls while
    cost_explorer_enabled is off, so a runtime flag flip activates it on the next pass —
    and the endpoints self-populate on first load regardless.

    Calls exactly what /api/costs/{summary,breakdown} call. There is no cache key here
    and no second fetch path, so there is nothing for a warmer to drift onto — the
    failure mode the comment block above exists to prevent, closed structurally rather
    than by sharing a constant.

    Still runs in every gunicorn worker, as every warmer does, and that is now harmless:
    the first process to claim a cloud queries it and the others read its result out of
    the table. Before the durable cache this loop was 2 workers x 2 views = 4 Cost
    Management POSTs against one subscription at every container start."""
    from .services import cost_cache
    # De-burst the first pass. Both workers start within milliseconds of each other, and
    # while the claim lock makes a simultaneous start correct, it makes three of the four
    # callers wait out the cold-start poll for no reason.
    await asyncio.sleep(random.uniform(0, 10))
    while True:
        try:
            if config_service.get_bool("cost_explorer_enabled", settings.cost_explorer_enabled):
                await cost_cache.warm()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cache warmer cost_summary failed: %s", exc)
        await asyncio.sleep(cost_cache.warm_interval_seconds())


async def _warm_dashboard_stats() -> None:
    """Secondary collector for the dashboard tile snapshot.

    ``dash-worker`` is the PRIMARY: it runs the same loop as a peer of the job runner. This
    one exists for the installs that have no worker at all — a bare ``uvicorn``, a SQLite
    dev box, a ``docker-compose`` without the ``worker`` service — and it must be a no-op
    whenever the worker is doing its job.

    Two mechanisms, and the second is what makes the first safe:

      * the SAME advisory lock, lease and per-provider pacing as the worker, so a
        simultaneous pass is CORRECT rather than merely unlikely. That property is what
        tests/test_dashboard_stat_cache proves with two module loads standing in for two
        processes.
      * a DEFERENCE WINDOW: this loop only claims a tile older than
        ``dashboard_stats_stale_after_seconds`` — older than the worker would ever let one
        get. With a worker present every pass here claims nothing and costs one SELECT.
        Without one, it takes over within a single window.

    Deliberately NOT gated on a worker-liveness probe. The worker does publish a heartbeat
    (worker_policy / api.worker), and that is the right thing to show an operator — but a
    liveness signal that flaps would flap the collector, and the lock already makes
    concurrency correct rather than merely unlikely.

    Runs in BOTH gunicorn workers, as every warmer here does, and that is harmless for the
    same reason _warm_cost_summary is: whichever process wins the claim does the work and
    the other reads the row.
    """
    from .services import dashboard_collect, dashboard_stat_cache
    # De-burst: both gunicorn workers start within milliseconds of each other.
    await asyncio.sleep(random.uniform(0, 10))
    while True:
        try:
            await dashboard_collect.collect_once(
                min_age_s=dashboard_stat_cache.stale_after_seconds())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("dashboard stats fallback collector failed: %s", exc)
        await asyncio.sleep(dashboard_stat_cache.collect_interval_seconds())


async def _warm_aws_amis() -> None:
    from .api import aws as aws_api
    # Scoped, not flat: an AMI id only resolves in its own region, so this key now
    # carries the region (aws_api.amis_cache_key) exactly like network-options does.
    await _warm_scoped_loop(
        "aws_amis",
        scope_fn=aws_api._aws_region,
        fetcher=aws_api._fetch_amis,
        key_fn=aws_api.amis_cache_key,
        ttl=cache_service.TTL[aws_api.CACHE_KEY_AMIS],
    )


async def _warm_aws_network_opts() -> None:
    from .api import aws as aws_api
    from .services import aws_service
    await _warm_scoped_loop(
        "aws_network_opts",
        scope_fn=aws_api._aws_region,
        fetcher=aws_service.get_network_options,
        key_fn=aws_api.network_opts_cache_key,
        ttl=cache_service.TTL[aws_api.CACHE_KEY_NETWORK_OPTS],
    )


async def _warm_aws_instances() -> None:
    from .api import aws as aws_api
    from .database import SessionLocal

    async def _fetch():
        db = SessionLocal()
        try:
            return await aws_api._fetch_instances(db)
        finally:
            db.close()

    await _warm_loop(
        "aws_instances",
        fetcher=_fetch,
        key_fn=aws_api.instances_cache_key,
        ttl=cache_service.TTL[aws_api.CACHE_KEY_INSTANCES],
    )


async def _warm_azure_images() -> None:
    from .api import azure as azure_api
    # Scoped, not flat: under azure_region_configs each region resolves its own
    # gallery, so the key has to name the location the images came from.
    await _warm_scoped_loop(
        "azure_images",
        scope_fn=azure_api._loc,
        fetcher=azure_api._fetch_private_images,
        key_fn=azure_api.images_cache_key,
        ttl=cache_service.TTL[azure_api.CACHE_KEY_IMAGES],
    )


async def _warm_azure_network_opts() -> None:
    from .api import azure as azure_api
    await _warm_scoped_loop(
        "azure_network_opts",
        scope_fn=azure_api._loc,
        fetcher=azure_api._fetch_network_options,
        key_fn=azure_api.network_opts_cache_key,
        ttl=cache_service.TTL[azure_api.CACHE_KEY_NETWORK_OPTS],
    )


async def _warm_portainer_containers() -> None:
    """Periodically refresh Portainer container state into the DB cache."""
    from .database import SessionLocal
    from .services import container_inventory_service

    interval = 60  # seconds — matches portainer_service in-memory cache TTL
    while True:
        # Gate each pass on the live flag + a configured URL so the loop stays
        # quiet until Portainer is set up, and honors Settings changes live.
        enabled = config_service.get_bool("portainer_enabled", settings.portainer_enabled)
        configured = bool(config_service.get("portainer_url") or settings.portainer_url)
        if enabled and configured:
            db = SessionLocal()
            try:
                await container_inventory_service.populate_all(db)
            except Exception as exc:
                logger.warning("container warmer failed: %s", exc)
            finally:
                db.close()
        await asyncio.sleep(interval)


# ── Rate limiter ──────────────────────────────────────────────────────────────
#
# READ THIS BEFORE ASSUMING ANYTHING IS RATE LIMITED. `default_limits` only takes
# effect through `SlowAPIMiddleware`, which is deliberately NOT added: a blanket
# 60/minute per address would break the UI, which fires many API calls per page load.
# There are also no `@limiter.limit` decorators. So this limiter is currently inert and
# `settings.rate_limit_per_minute` does nothing — the object exists for the exception
# handler wiring below and for endpoints that opt in later.
#
# Brute-force protection on the one endpoint that actually needs it lives in
# `services/login_guard.py` instead, keyed on the USERNAME rather than the address —
# `get_remote_address` reads a value derived from X-Forwarded-For, which the default
# `trusted_proxy_hosts="*"` lets any client spoof and rotate per request.
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.rate_limit_per_minute}/minute"])


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=(
        "Self-hosted control plane for multi-cloud and on-prem infrastructure. "
        "Provision and manage cloud VMs, managed databases, containers and "
        "Kubernetes clusters across AWS, Azure, GCP and OCI — plus VMware, "
        "Hyper-V, Proxmox and Nutanix on-prem — with image build-and-promote, "
        "Ansible configuration management, pluggable secrets and storage "
        "backends, and layered privileged access through PRA, Password Safe "
        "and Entitle."
    ),
    lifespan=lifespan,
    # /docs is the repo documentation browser (api/docs_pages.py), so the API
    # explorer lives at /swagger. Both are served by custom routes below:
    # the schema itself requires authentication, which the built-ins can't do.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Trust X-Forwarded-Proto/X-Forwarded-For from a reverse proxy, so request.url.scheme
# reflects "https" when the dashboard is reached through one.
#
# Pinned to loopback by default rather than "*". A wildcard lets any client that can
# reach the socket declare its own source address, and get_remote_address believes it —
# which is exactly the value the login throttle's per-address cap keys off. Set
# TRUSTED_PROXY_HOSTS to the proxy's literal IP when you put one in front; it must be a
# literal, because uvicorn 0.27 compares strings and understands neither hostnames nor
# CIDR.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_hosts)

_forwarded_auditor = public_url.ForwardedHeaderAuditor(settings.trusted_proxy_hosts)


@app.middleware("http")
async def warn_untrusted_forwarded_headers(request: Request, call_next):
    """Say something when a proxy header arrives from a peer we do not trust.

    The decision lives in ``public_url.ForwardedHeaderAuditor`` so it is testable
    without importing this module; all that happens here is plumbing.

    Runs OUTSIDE ProxyHeadersMiddleware. Starlette builds the stack so the
    most-recently-added middleware is outermost, and ProxyHeaders was added above, so
    ``request.client`` here is still the real transport peer rather than a rewritten
    one — which is the whole point, since the peer is what the operator has to name.
    """
    warning = _forwarded_auditor.check(
        request.client.host if request.client else "",
        "x-forwarded-proto" in request.headers or "x-forwarded-for" in request.headers,
    )
    if warning:
        logger.warning("%s", warning)
    return await call_next(request)


# ── Setup guard middleware ────────────────────────────────────────────────────
# Until the setup wizard has been completed, redirect all browser traffic to
# /setup.  API and static paths are exempt so the wizard itself can load.

from starlette.responses import RedirectResponse as _Redirect  # noqa: E402

_SETUP_BYPASS_PREFIXES = ("/setup", "/api/setup", "/static", "/api/health", "/api/features", "/api/secrets", "/api/storage")

# Machine callers that must never be handed a 302 to an HTML wizard. A remote agent
# polls this API in a loop with follow_redirects off; a redirect would arrive as an
# opaque non-JSON response it can only treat as a hard error. 503 + Retry-After says
# "not yet, come back" in the one vocabulary an HTTP client already understands.
# /api/entitle/rest is here for the same reason: Entitle calls it as a machine
# client and would read a 302-to-HTML as an integration failure rather than as
# "this dashboard has not finished its first-run setup".
_SETUP_503_PREFIXES = ("/api/agent", "/api/entitle/rest")

@app.middleware("http")
async def setup_guard(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _SETUP_BYPASS_PREFIXES):
        return await call_next(request)
    if not config_service.is_setup_complete():
        if any(path.startswith(p) for p in _SETUP_503_PREFIXES):
            return JSONResponse(
                {"detail": "Dashboard setup is not complete."},
                status_code=503, headers={"Retry-After": "60"},
            )
        return _Redirect("/setup", status_code=302)
    return await call_next(request)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_correlation(request: Request, call_next):
    """Tag every request's log lines with a short correlation id (honouring an
    inbound X-Request-ID), and echo it back as the X-Request-ID response header
    so a client/proxy can tie its record to the dashboard's."""
    rid = request.headers.get("x-request-id") or new_request_id()
    token = set_correlation_id(rid)
    try:
        response = await call_next(request)
    finally:
        reset_correlation_id(token)
    response.headers["X-Request-ID"] = rid
    return response


# ── Static files & templates ──────────────────────────────────────────────────

_base_dir = os.path.dirname(__file__)
_static_dir = os.path.join(_base_dir, "static")
_templates_dir = os.path.join(_base_dir, "templates")

if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

templates = Jinja2Templates(directory=_templates_dir)
templates.env.globals["app_env"] = settings.app_env


def _feature_flags() -> dict:
    """Raw ``*_enabled`` flags for the ~30 template responses below.

    Lives in services/feature_flags so the job worker can read the same map without
    importing this module (and with it the whole FastAPI app)."""
    return feature_flags.flags()


# ── Register API routers ──────────────────────────────────────────────────────
#
# Core routers are always included.
#
# Optional integration routers (VMware, Portainer, Ansible, Entitle) are also
# always registered but protected by a runtime dependency that checks the
# feature flag in config_service.  This means enabling a flag through the
# Settings → Integrations panel takes effect immediately — no restart needed.

from fastapi import Depends  # noqa: E402
from .api import auth, jobs, websocket, aws, azure, gcp, oci, packer, mfa, tokens, users, groups, setup, secrets, storage, images, regions as regions_api  # noqa: E402
from .api import cloud_databases  # noqa: E402
from .api import cloud_functions as cloud_functions_api  # noqa: E402
from .api import entitle_rest as entitle_rest_api  # noqa: E402
from .api import pra as pra_api  # noqa: E402
from .api import audit as audit_api  # noqa: E402
from .api import docs_pages  # noqa: E402
from .api import workgroups as workgroups_api  # noqa: E402
from .api import workgroup_overrides as workgroup_overrides_api  # noqa: E402
from .api import cloud_identity as cloud_identity_api  # noqa: E402
from .api import gateways as gateways_api  # noqa: E402
from .api import expiry as expiry_api  # noqa: E402
from .api import notifications as notifications_api  # noqa: E402
from .api import agent as agent_api  # noqa: E402
from .api import worker as worker_api  # noqa: E402
from .api.mcp_server import get_mcp_asgi_app  # noqa: E402


def _feature_gate(flag: str):
    """FastAPI dependency: 404 if the named feature flag is disabled."""
    def _check():
        if not config_service.get_bool(flag):
            raise HTTPException(
                status_code=404,
                detail=f"This integration is not enabled. "
                       f"Enable it in Settings → Integrations.",
            )
    return Depends(_check)


app.include_router(setup.router)
app.include_router(secrets.router)
app.include_router(cloud_identity_api.router)
app.include_router(storage.router)
app.include_router(images.router)
app.include_router(auth.router)
app.include_router(regions_api.router)
app.include_router(pra_api.router)
app.include_router(mfa.router)
app.include_router(tokens.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(workgroups_api.router)
app.include_router(workgroup_overrides_api.router)
app.include_router(jobs.router)
app.include_router(audit_api.router)
app.include_router(docs_pages.router)
# Remote on-prem agents. Gated: this is the only router that accepts requests from
# outside the dashboard's own trust domain, so it must be off unless asked for.
app.include_router(agent_api.router,
                   dependencies=[_feature_gate("remote_agents_enabled")])

# ── API explorer (/swagger) + authenticated schema ────────────────────────────
# FastAPI's built-ins are disabled above. The schema is the sensitive part — it
# enumerates every endpoint — so /openapi.json requires a bearer token. The HTML
# shells are public loaders that authenticate client-side like every other page
# (this app keeps its token in localStorage, so a browser navigation carries no
# Authorization header; Swagger UI attaches it via requestInterceptor).

from .api.auth import get_current_user as _docs_current_user  # noqa: E402


@app.get("/openapi.json", include_in_schema=False)
async def openapi_schema(current_user=Depends(_docs_current_user)):
    """The OpenAPI schema. Authenticated: it lists the full API surface."""
    return app.openapi()


def _custom_openapi():
    """Schema with the password grant removed.

    ``get_current_user`` depends on ``OAuth2PasswordBearer`` purely to extract the
    Authorization header, but declaring it that way makes Swagger's Authorize
    button collect a **username and password** — which is exactly the credential
    flow we don't want exercised from a docs page. Runtime behaviour is untouched;
    only the documented scheme changes, to plain bearer. The explorer attaches the
    session token itself (see templates/swagger.html), so nothing is lost.
    """
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes,
    )
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    for name, defn in list(schemes.items()):
        if defn.get("type") == "oauth2" and "password" in (defn.get("flows") or {}):
            schemes[name] = {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Session token (from SSO or local login) or a vmcli_ personal "
                    "access token. The API explorer attaches your session token "
                    "automatically."
                ),
            }
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


@app.get("/swagger", include_in_schema=False)
async def swagger_ui(request: Request):
    """API explorer. Authenticates client-side, then loads the schema with the
    stored bearer token attached."""
    return templates.TemplateResponse("swagger.html", {"request": request, **_feature_flags()})

app.include_router(websocket.router)
app.include_router(aws.router)
app.include_router(cloud_databases.router)
# Preview feature: the flag is owned by the Settings → Preview features card, and
# the router 404s entirely while it is off (the handlers also self-gate with 403,
# so a stale route can't leak either).
app.include_router(cloud_functions_api.router,
                   dependencies=[_feature_gate("cloud_functions_enabled")])
# The one Entitle adapter the dashboard hosts itself, because here the dashboard IS
# the target system. Gated by entitle_user_jit_enabled, and additionally closed
# (503) whenever entitle_rest_secret is unset — see the router's _require_secret.
app.include_router(entitle_rest_api.router,
                   dependencies=[_feature_gate("entitle_user_jit_enabled")])
app.include_router(azure.router)
app.include_router(gcp.router)
app.include_router(oci.router)
app.include_router(packer.router)

# MCP server — mounted as a sub-ASGI app so SSE streams pass through unmodified
app.mount("/mcp", get_mcp_asgi_app())

try:
    from .api import vms  # noqa: E402
    app.include_router(vms.router, dependencies=[_feature_gate("vmware_enabled")])
except ImportError as exc:
    logger.warning("API router 'vms' not loaded: %s", exc)

try:
    # Containers router exposes Portainer (gated per-call by the page UI when
    # portainer_enabled is false) plus ECS/ACI/Cloud Run endpoints that are
    # independent of Portainer — so don't gate the whole router on portainer.
    from .api import containers  # noqa: E402
    app.include_router(containers.router)
except ImportError as exc:
    logger.warning("API router 'containers' not loaded: %s", exc)

try:
    from .api import config_mgmt  # noqa: E402
    app.include_router(config_mgmt.router, dependencies=[_feature_gate("ansible_enabled")])
except ImportError as exc:
    logger.warning("API router 'config_mgmt' not loaded: %s", exc)


# Hypervisor connections. NOT behind any single hypervisor's feature gate: the page
# manages connections for all five kinds, and gating it on one of them would hide the
# others' connections whenever that one is off.
try:
    from .api import connections as connections_api  # noqa: E402
    app.include_router(connections_api.router)
except ImportError as exc:
    logger.warning("API router 'connections' not loaded: %s", exc)

try:
    from .api import proxmox  # noqa: E402
    app.include_router(proxmox.router, dependencies=[_feature_gate("proxmox_enabled")])
except ImportError as exc:
    logger.warning("API router 'proxmox' not loaded: %s", exc)

try:
    from .api import vsphere  # noqa: E402
    app.include_router(vsphere.router, dependencies=[_feature_gate("vsphere_enabled")])
except ImportError as exc:
    logger.warning("API router 'vsphere' not loaded: %s", exc)

try:
    from .api import hyperv  # noqa: E402
    app.include_router(hyperv.router, dependencies=[_feature_gate("hyperv_enabled")])
except ImportError as exc:
    logger.warning("API router 'hyperv' not loaded: %s", exc)

try:
    from .api import nutanix  # noqa: E402
    app.include_router(nutanix.router, dependencies=[_feature_gate("nutanix_enabled")])
except ImportError as exc:
    logger.warning("API router 'nutanix' not loaded: %s", exc)

try:
    from .api import xcpng  # noqa: E402
    app.include_router(xcpng.router, dependencies=[_feature_gate("xcpng_enabled")])
except ImportError as exc:
    logger.warning("API router 'xcpng' not loaded: %s", exc)

try:
    from .api import epml  # noqa: E402
    app.include_router(epml.router, dependencies=[_feature_gate("epml_enabled")])
except ImportError as exc:
    logger.warning("API router 'epml' not loaded: %s", exc)

# Gateway hosts are a BeyondTrust PRA concept, so the routes follow the PRA flag — with
# it off there is nothing for a gateway to register with and the Gateways tab stays
# hidden. Deliberately NOT password_safe_enabled: a PRA-only deployment still needs
# gateways, and a Password Safe-only one has no use for them.
app.include_router(gateways_api.router,
                   dependencies=[_feature_gate("pra_enabled")])

try:
    # Virtual desktop management (Azure pools + PRA brokering). Gated on vdesktops_enabled.
    from .api import desktops  # noqa: E402
    app.include_router(desktops.router, dependencies=[_feature_gate("vdesktops_enabled")])
except ImportError as exc:
    logger.warning("API router 'desktops' not loaded: %s", exc)

try:
    # Kubernetes management (provision + register clusters). Gated on k8s_management_enabled.
    from .api import k8s as k8s_api  # noqa: E402
    app.include_router(k8s_api.router, dependencies=[_feature_gate("k8s_management_enabled")])
except ImportError as exc:
    logger.warning("API router 'k8s' not loaded: %s", exc)

try:
    # Cross-cloud cost (MTD spend tile). Gated on cost_explorer_enabled.
    from .api import costs  # noqa: E402
    app.include_router(costs.router, dependencies=[_feature_gate("cost_explorer_enabled")])
except ImportError as exc:
    logger.warning("API router 'costs' not loaded: %s", exc)

try:
    # Cross-provider deployment inventory. Always-on (like jobs); RBAC-filtered.
    from .api import inventory  # noqa: E402
    app.include_router(inventory.router)
except ImportError as exc:
    logger.warning("API router 'inventory' not loaded: %s", exc)

try:
    # The dashboard home page's one aggregate read. Always-on and NOT feature-gated: it
    # answers for whichever tiles this install has, and a tile with nothing collected
    # reports unavailable rather than 404ing the whole page.
    from .api import dashboard as dashboard_api  # noqa: E402
    app.include_router(dashboard_api.router)
except ImportError as exc:
    logger.warning("API router 'dashboard' not loaded: %s", exc)

# Auto-delete timer (extend/pin + sweeper surface). Not feature-gated at the router:
# GET /status has to answer "disabled" so the pages can hide the column, and the
# mutations refuse on their own when the feature is off.
app.include_router(expiry_api.router)

# Outbound notifications. Not feature-gated at the router either: an admin has to be
# able to add and test an endpoint *before* switching the feature on, and every route
# here is admin-only and harmless while it is off.
app.include_router(notifications_api.router)

# Job-worker concurrency readout. Read-only and admin-only; not feature-gated because the
# worker has no off switch — it is the process that runs every queued job.
app.include_router(worker_api.router)


# ── HTML pages ────────────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page(request: Request):
    """First-run setup wizard. Accessible without authentication."""
    return templates.TemplateResponse("setup.html", {"request": request})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    """Serve the dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request, **_feature_flags()})


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    # Surface the generic-OIDC button only when an issuer is configured, so a
    # default install doesn't show a button that 501s.
    try:
        from .services import oidc_service
        oidc_enabled, oidc_label = oidc_service.is_configured(), oidc_service.provider_label()
    except Exception:
        oidc_enabled, oidc_label = False, "SSO"
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "oidc_enabled": oidc_enabled, "oidc_label": oidc_label},
    )


@app.get("/vms", response_class=HTMLResponse, include_in_schema=False)
async def vms_page(request: Request):
    if not config_service.get_bool("vmware_enabled", settings.vmware_enabled):
        raise HTTPException(status_code=404, detail="VMware integration is disabled")
    # Always agent-bound, and structurally so: `workstation` is in
    # hypervisor_connection_service.AGENT_ONLY_KINDS because the dashboard has no
    # transport to vmrest under any configuration. Deliberately NOT taken from
    # _hypervisor_page_host, which returns via_agent=False in its except branch — that
    # would ungrey every button on an install whose connection failed to resolve.
    return templates.TemplateResponse(
        "vms/list.html",
        {"request": request, "via_agent": True, **_feature_flags()})


def _hypervisor_page_host(kind: str) -> dict:
    """The endpoint a hypervisor page is actually talking to, for its header.

    These pages have no connection picker, so they use the default connection — and
    they used to display the singleton `*_host` config key instead, which on a
    multi-connection install is simply the wrong host. Resolve the same connection the
    API will use, and name it, so the header stops disagreeing with the data below it.

    Best-effort: an unconfigured or unreachable-to-resolve kind renders a blank host
    rather than 500ing a page whose real content loads over the API anyway.
    """
    from .database import HypervisorConnection, SessionLocal
    from .services import hypervisor_connection_service as hcs
    try:
        with SessionLocal() as db:
            conn = hcs.resolve(db, kind)
        # An agent-bound connection has no host here by design — the agent holds it —
        # so show the agent-side name rather than an empty span.
        # last_sync_at drives the "showing the last synced inventory" banner. It lives
        # on the connection rather than in the VM response so the list endpoints keep a
        # single shape — see hypervisor_view_service.synced_rows.
        row = db.query(HypervisorConnection).filter(
            HypervisorConnection.id == conn.id).first() if conn.id else None
        synced_at = row.last_sync_at.isoformat() if (row and row.last_sync_at) else ""
        return {"host": conn.host or conn.agent_connection_name or "",
                "connection_name": conn.name,
                "via_agent": conn.via_agent,
                "synced_at": synced_at}
    except Exception:  # noqa: BLE001
        return {"host": "", "connection_name": "", "via_agent": False, "synced_at": ""}


@app.get("/connections", response_class=HTMLResponse, include_in_schema=False)
async def connections_page(request: Request):
    """Hypervisor connections. Reachable whenever ANY hypervisor integration is on —
    it is the one place their credentials now live."""
    flags = _feature_flags()
    if not any(flags.get(f"{k}_enabled") for k in
               ("proxmox", "vsphere", "hyperv", "nutanix", "xcpng", "vmware")):
        raise HTTPException(status_code=404, detail="No hypervisor integration is enabled")
    return templates.TemplateResponse("connections/index.html", {"request": request, **flags})


@app.get("/proxmox", response_class=HTMLResponse, include_in_schema=False)
async def proxmox_page(request: Request):
    if not config_service.get_bool("proxmox_enabled", settings.proxmox_enabled):
        raise HTTPException(status_code=404, detail="Proxmox integration is disabled")
    conn = _hypervisor_page_host("proxmox")
    return templates.TemplateResponse(
        "proxmox/index.html",
        {"request": request, "connection_name": conn["connection_name"],
         "via_agent": conn["via_agent"], "synced_at": conn["synced_at"],
         **_feature_flags()})


@app.get("/vsphere", response_class=HTMLResponse, include_in_schema=False)
async def vsphere_page(request: Request):
    if not config_service.get_bool("vsphere_enabled", settings.vsphere_enabled):
        raise HTTPException(status_code=404, detail="vSphere integration is disabled")
    conn = _hypervisor_page_host("vsphere")
    return templates.TemplateResponse(
        "vsphere/index.html",
        {"request": request, "connection_name": conn["connection_name"],
         "via_agent": conn["via_agent"], "synced_at": conn["synced_at"],
         **_feature_flags()})


@app.get("/hyperv", response_class=HTMLResponse, include_in_schema=False)
async def hyperv_page(request: Request):
    if not config_service.get_bool("hyperv_enabled", settings.hyperv_enabled):
        raise HTTPException(status_code=404, detail="Hyper-V integration is disabled")
    conn = _hypervisor_page_host("hyperv")
    return templates.TemplateResponse(
        "hyperv/index.html",
        {"request": request, "hyperv_host": conn["host"],
         "connection_name": conn["connection_name"], "via_agent": conn["via_agent"],
         "synced_at": conn["synced_at"],
         **_feature_flags()},
    )


@app.get("/nutanix", response_class=HTMLResponse, include_in_schema=False)
async def nutanix_page(request: Request):
    if not config_service.get_bool("nutanix_enabled", settings.nutanix_enabled):
        raise HTTPException(status_code=404, detail="Nutanix integration is disabled")
    conn = _hypervisor_page_host("nutanix")
    return templates.TemplateResponse(
        "nutanix/index.html",
        {"request": request, "nutanix_host": conn["host"],
         "connection_name": conn["connection_name"], "via_agent": conn["via_agent"],
         "synced_at": conn["synced_at"],
         **_feature_flags()},
    )


@app.get("/xcpng", response_class=HTMLResponse, include_in_schema=False)
async def xcpng_page(request: Request):
    if not config_service.get_bool("xcpng_enabled", settings.xcpng_enabled):
        raise HTTPException(status_code=404, detail="XCP-ng integration is disabled")
    conn = _hypervisor_page_host("xcpng")
    return templates.TemplateResponse(
        "xcpng/index.html",
        {"request": request, "xcpng_host": conn["host"],
         "connection_name": conn["connection_name"], "via_agent": conn["via_agent"],
         "synced_at": conn["synced_at"],
         **_feature_flags()},
    )


@app.get("/config-mgmt", response_class=HTMLResponse, include_in_schema=False)
async def config_mgmt_page(request: Request):
    if not config_service.get_bool("ansible_enabled", settings.ansible_enabled):
        raise HTTPException(status_code=404, detail="Ansible integration is disabled")
    return templates.TemplateResponse("config-mgmt/index.html", {"request": request, **_feature_flags()})


@app.get("/containers", response_class=HTMLResponse, include_in_schema=False)
async def containers_page(request: Request):
    # Always accessible: surfaces On-Premises (Portainer), AWS ECS, Azure ACI,
    # GCP Cloud Run. Each tab self-gates on its own configuration.
    portainer_enabled = config_service.get_bool("portainer_enabled", settings.portainer_enabled)
    return templates.TemplateResponse(
        "containers/index.html",
        {"request": request, "portainer_enabled": portainer_enabled, **_feature_flags()},
    )


@app.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs_page(request: Request):
    return templates.TemplateResponse("jobs/list.html", {"request": request, **_feature_flags()})


@app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
async def inventory_page(request: Request):
    """Cross-provider deployment inventory (read-only aggregation of DB records)."""
    return templates.TemplateResponse("inventory/list.html", {"request": request, **_feature_flags()})


@app.get("/costs", response_class=HTMLResponse, include_in_schema=False)
async def costs_page(request: Request):
    """Cloud cost page: account-total summary + dashboard-managed spend breakdown.
    Nav-gated on cost_explorer_enabled (+ admin); the /api/costs/* routes are
    admin-only and feature-gated."""
    return templates.TemplateResponse("costs/index.html", {"request": request, **_feature_flags()})


@app.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def job_detail_page(request: Request, job_id: str):
    return templates.TemplateResponse("jobs/detail.html", {"request": request, "job_id": job_id, **_feature_flags()})


@app.get("/aws", response_class=HTMLResponse, include_in_schema=False)
async def aws_page(request: Request):
    return templates.TemplateResponse("aws/index.html", {"request": request, **_feature_flags()})


@app.get("/azure", response_class=HTMLResponse, include_in_schema=False)
async def azure_page(request: Request):
    location = config_service.get("azure_location") or settings.azure_location
    return templates.TemplateResponse("azure/index.html", {"request": request, "default_location": location, **_feature_flags()})


@app.get("/gcp", response_class=HTMLResponse, include_in_schema=False)
async def gcp_page(request: Request):
    return templates.TemplateResponse("gcp/index.html", {"request": request, **_feature_flags()})


@app.get("/oci", response_class=HTMLResponse, include_in_schema=False)
async def oci_page(request: Request):
    return templates.TemplateResponse("oci/index.html", {"request": request, **_feature_flags()})


@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request, **_feature_flags()})


@app.get("/secrets", response_class=HTMLResponse, include_in_schema=False)
async def secrets_page(request: Request):
    return templates.TemplateResponse("secrets/index.html", {"request": request, **_feature_flags()})


@app.get("/storage", response_class=HTMLResponse, include_in_schema=False)
async def storage_page(request: Request):
    return templates.TemplateResponse("storage/index.html", {"request": request, **_feature_flags()})


@app.get("/images", response_class=HTMLResponse, include_in_schema=False)
async def images_page(request: Request):
    return templates.TemplateResponse("images/index.html", {"request": request, **_feature_flags()})


@app.get("/desktops", response_class=HTMLResponse, include_in_schema=False)
async def desktops_page(request: Request):
    """Virtual-desktop management page. Nav-gated on vdesktops_enabled;
    the /api/desktops router is feature-gated."""
    return templates.TemplateResponse("desktops/index.html", {"request": request, **_feature_flags()})


@app.get("/databases", response_class=HTMLResponse, include_in_schema=False)
async def databases_page(request: Request):
    """Cloud database infrastructure page. Nav-gated on cloud_database_enabled;
    the /api/databases router self-gates per call. PostgreSQL/MySQL/SQL Server
    are live across AWS/Azure/GCP."""
    return templates.TemplateResponse("databases/index.html", {"request": request, **_feature_flags()})


@app.get("/functions", response_class=HTMLResponse, include_in_schema=False)
async def functions_page(request: Request):
    """Cloud Functions page (preview). Nav-gated on cloud_functions_enabled; the
    /api/functions router is feature-gated."""
    return templates.TemplateResponse("functions/index.html", {"request": request, **_feature_flags()})


@app.get("/k8s", response_class=HTMLResponse, include_in_schema=False)
async def k8s_page(request: Request):
    """Kubernetes management page — Phase 3a. Nav-gated on k8s_management_enabled;
    the /api/k8s router is feature-gated."""
    return templates.TemplateResponse("k8s/index.html", {"request": request, **_feature_flags()})


@app.get("/agents", response_class=HTMLResponse, include_in_schema=False)
async def agents_page(request: Request):
    """Remote on-prem agents. Nav-gated on remote_agents_enabled (+ admin); the
    /api/agent router is feature-gated and its operator half is admin-only."""
    return templates.TemplateResponse("agents/index.html", {"request": request, **_feature_flags()})


@app.get("/users", response_class=HTMLResponse, include_in_schema=False)
async def users_page(request: Request):
    return templates.TemplateResponse(
        "users/list.html",
        {
            "request": request,
            "workgroups": list(settings.workgroups.keys()),
            # Inject the backend permission catalog so the assignment grid
            # can't drift from api/auth.py (was hard-coded in the template).
            "permission_scopes": auth.PERMISSION_SCOPES,
            "permission_levels": auth.PERMISSION_LEVELS,
        },
    )


@app.get("/groups", response_class=HTMLResponse, include_in_schema=False)
async def groups_page(request: Request):
    return templates.TemplateResponse(
        "groups/index.html",
        {
            "request": request,
            "workgroups": list(settings.workgroups.keys()),
            # Inject the backend permission catalog so the assignment grid
            # can't drift from api/auth.py (was hard-coded in the template).
            "permission_scopes": auth.PERMISSION_SCOPES,
            "permission_levels": auth.PERMISSION_LEVELS,
        },
    )


@app.get("/workgroups", response_class=HTMLResponse, include_in_schema=False)
async def workgroups_page(request: Request):
    return templates.TemplateResponse("workgroups/index.html", {"request": request})


# ── Health / diagnostic ───────────────────────────────────────────────────────

@app.get("/api/health", tags=["health"])
async def health():
    """Quick health check."""
    return {"status": "ok", "version": settings.api_version}


@app.get("/api/features", tags=["health"])
async def features():
    """Expose the enabled feature set to the frontend (reads from config_service
    so wizard changes are reflected immediately without a restart).

    A thin delegate: services/dashboard_collect reads the same map to decide which tiles
    to collect, and it must agree with what the page renders."""
    return feature_flags.feature_map()


@app.get("/api/cache/status", tags=["health"])
async def cache_status():
    """Return metadata for all cached keys (debug / admin).

    Three stores, deliberately: the in-memory one below is per-process and dies with the
    container, while `cost_cache` and `dashboard_stats` rows are shared across every worker
    and survive a rebuild. Comparing `fetched_at` across replicas is how you confirm those
    two are actually shared rather than silently per-worker.

    `dashboard_stats` is also the only place the tile collector is visible at all: it runs
    in dash-worker, writes no job rows, and logs only on failure. An empty list here on a
    running install means no collector is reaching the database."""
    entries = await cache_service.all_entries()
    from .services import cost_cache, dashboard_stat_cache
    db = SessionLocal()
    try:
        cost_rows = cost_cache.snapshot(db)
    except Exception as exc:  # noqa: BLE001 — a health endpoint must not 500 on a detail
        logger.warning("cache status: cost cache snapshot failed: %s", exc)
        cost_rows = []
    try:
        stat_rows = dashboard_stat_cache.snapshot(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache status: dashboard stats snapshot failed: %s", exc)
        stat_rows = []
    finally:
        db.close()
    return {
        "cache_type": "in-memory",
        "entry_count": len(entries),
        "entries": entries,
        "cost_cache": cost_rows,
        "dashboard_stats": stat_rows,
    }


