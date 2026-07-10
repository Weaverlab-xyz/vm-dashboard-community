"""
FastAPI application entry point for the VM CLI Web Dashboard.
"""
import asyncio
import logging
import os
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
from .services import config_service

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

    # Cost-summary warmer — always launched; no-ops (no billable calls) while the
    # cost feature is off, so flipping the flag in Settings warms the next pass.
    warmers.append(
        asyncio.create_task(_warm_cost_summary(), name="warm_cost_summary")
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


# ── Background cache warmers ──────────────────────────────────────────────────

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


async def _warm_cost_summary() -> None:
    """Pre-populate the cost tile + /costs page (account summary and the
    dashboard-managed breakdown). Skips the (billable) cloud calls while
    cost_explorer_enabled is off, so a runtime flag flip activates it on the next
    pass — and the endpoints self-populate on first load regardless."""
    from .services import cost_service
    ttl = cache_service.TTL["cost_summary"]
    interval = int(ttl * 0.8)
    while True:
        try:
            if config_service.get_bool("cost_explorer_enabled", settings.cost_explorer_enabled):
                summary = await cost_service.get_cost_summary()
                await cache_service.set(cache_service.key_global("cost_summary"), summary, ttl)
                breakdown = await cost_service.get_cost_breakdown()
                await cache_service.set(
                    cache_service.key_global("cost_breakdown"), breakdown,
                    cache_service.TTL["cost_breakdown"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cache warmer cost_summary failed: %s", exc)
        await asyncio.sleep(interval)


async def _warm_aws_amis() -> None:
    from .services import aws_service
    await _warm_loop(
        "aws_amis",
        fetcher=lambda: aws_service.list_amis(settings.aws_region),
        key_fn=lambda: cache_service.key_global("aws_amis"),
        ttl=cache_service.TTL["aws_amis"],
    )


async def _warm_aws_network_opts() -> None:
    from .services import aws_service
    await _warm_loop(
        "aws_network_opts",
        fetcher=lambda: aws_service.get_network_options(settings.aws_region),
        key_fn=lambda: cache_service.key_global("aws_network_opts"),
        ttl=cache_service.TTL["aws_network_opts"],
    )


async def _warm_aws_instances() -> None:
    from .database import SessionLocal, Job
    from .services import aws_service

    async def _fetch():
        db = SessionLocal()
        try:
            deploy_jobs = (
                db.query(Job)
                .filter(Job.job_type == "ec2_deploy", Job.status == "completed")
                .all()
            )
            instance_ids = [
                job.metadata_dict.get("instance_id")
                for job in deploy_jobs
                if not job.metadata_dict.get("destroyed")
                and job.metadata_dict.get("instance_id")
            ]
            if not instance_ids:
                return []
            live = await aws_service.describe_instances(settings.aws_region, instance_ids)
            job_by_instance = {
                job.metadata_dict.get("instance_id"): job
                for job in deploy_jobs
                if job.metadata_dict.get("instance_id")
            }
            result = []
            for inst in live:
                iid = inst.get("instance_id")
                j = job_by_instance.get(iid)
                result.append({**inst, "job_id": j.id if j else None, "deployed_by": j.created_by if j else None})
            return result
        finally:
            db.close()

    await _warm_loop(
        "aws_instances",
        fetcher=_fetch,
        key_fn=lambda: cache_service.key_global("aws_instances"),
        ttl=cache_service.TTL["aws_instances"],
    )


def _live_cfg(key: str) -> str:
    """Return the live config_service value, falling back to startup settings."""
    from .services import config_service
    return config_service.get(key) or getattr(settings, key, "")


async def _warm_azure_images() -> None:
    from .services import azure_service
    await _warm_loop(
        "azure_images",
        fetcher=lambda: azure_service.list_private_images(
            _live_cfg("azure_shared_image_gallery"),
            _live_cfg("azure_gallery_resource_group"),
            _live_cfg("azure_resource_group"),
        ),
        key_fn=lambda: cache_service.key_global("azure_images"),
        ttl=cache_service.TTL["azure_images"],
    )


async def _warm_azure_network_opts() -> None:
    from .services import azure_service
    await _warm_loop(
        "azure_network_opts",
        fetcher=lambda: azure_service.get_network_options(
            _live_cfg("azure_location"),
            _live_cfg("azure_vnet_resource_group"),
            _live_cfg("azure_resource_group"),
        ),
        key_fn=lambda: cache_service.key_global("azure_network_opts"),
        ttl=cache_service.TTL["azure_network_opts"],
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

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.rate_limit_per_minute}/minute"])


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description="Web-based dashboard for managing VMware Workstation VMs via browser.",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Trust X-Forwarded-Proto/X-Forwarded-For from the Container Apps / reverse proxy.
# This makes request.url.scheme reflect "https" when accessed through the proxy.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


# ── Setup guard middleware ────────────────────────────────────────────────────
# Until the setup wizard has been completed, redirect all browser traffic to
# /setup.  API and static paths are exempt so the wizard itself can load.

from starlette.responses import RedirectResponse as _Redirect  # noqa: E402

_SETUP_BYPASS_PREFIXES = ("/setup", "/api/setup", "/static", "/api/health", "/api/features", "/api/secrets", "/api/storage")

@app.middleware("http")
async def setup_guard(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in _SETUP_BYPASS_PREFIXES):
        return await call_next(request)
    if not config_service.is_setup_complete():
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
    """Read feature flags from config_service (DB) with env-var fallback.
    Called per-request so wizard changes are visible without a restart."""
    return {
        "vmware_enabled":       config_service.get_bool("vmware_enabled",        settings.vmware_enabled),
        "portainer_enabled":    config_service.get_bool("portainer_enabled",     settings.portainer_enabled),
        "ansible_enabled":      config_service.get_bool("ansible_enabled",       settings.ansible_enabled),
        "entitle_enabled":      config_service.get_bool("entitle_enabled",       settings.entitle_enabled),
        "beyondtrust_enabled":  config_service.get_bool("beyondtrust_enabled",   settings.beyondtrust_enabled),
        "proxmox_enabled":      config_service.get_bool("proxmox_enabled",       settings.proxmox_enabled),
        "vsphere_enabled":      config_service.get_bool("vsphere_enabled",       settings.vsphere_enabled),
        "hyperv_enabled":       config_service.get_bool("hyperv_enabled",        settings.hyperv_enabled),
        "nutanix_enabled":      config_service.get_bool("nutanix_enabled",       settings.nutanix_enabled),
        "xcpng_enabled":        config_service.get_bool("xcpng_enabled",         settings.xcpng_enabled),
        "vdesktops_enabled":    config_service.get_bool("vdesktops_enabled",     settings.vdesktops_enabled),
        "cloud_database_enabled": config_service.get_bool("cloud_database_enabled", settings.cloud_database_enabled),
        "entitle_registration_enabled": config_service.get_bool("entitle_registration_enabled", settings.entitle_registration_enabled),
        "k8s_management_enabled": config_service.get_bool("k8s_management_enabled", settings.k8s_management_enabled),
        "cost_explorer_enabled": config_service.get_bool("cost_explorer_enabled", settings.cost_explorer_enabled),
        "admission_control_enabled": config_service.get_bool("admission_control_enabled", settings.admission_control_enabled),
        # Entitle user-JIT Phase 4 UI affordances — surfaces the
        # "Request access" nav link + portal URL when both are configured.
        "entitle_user_jit_enabled":   config_service.get_bool("entitle_user_jit_enabled", settings.entitle_user_jit_enabled),
        "entitle_request_portal_url": config_service.get("entitle_request_portal_url",   settings.entitle_request_portal_url),
    }


# ── Register API routers ──────────────────────────────────────────────────────
#
# Core routers are always included.
#
# Optional integration routers (VMware, Portainer, Ansible, Entitle) are also
# always registered but protected by a runtime dependency that checks the
# feature flag in config_service.  This means enabling a flag through the
# Settings → Integrations panel takes effect immediately — no restart needed.

from fastapi import Depends  # noqa: E402
from .api import auth, jobs, websocket, aws, azure, gcp, packer, mfa, tokens, users, groups, setup, secrets, storage, images  # noqa: E402
from .api import cloud_databases  # noqa: E402
from .api import audit as audit_api  # noqa: E402
from .api import docs_pages  # noqa: E402
from .api import workgroups as workgroups_api  # noqa: E402
from .api import workgroup_overrides as workgroup_overrides_api  # noqa: E402
from .api import cloud_identity as cloud_identity_api  # noqa: E402
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
app.include_router(mfa.router)
app.include_router(tokens.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(workgroups_api.router)
app.include_router(workgroup_overrides_api.router)
app.include_router(jobs.router)
app.include_router(audit_api.router)
app.include_router(docs_pages.router)
app.include_router(websocket.router)
app.include_router(aws.router)
app.include_router(cloud_databases.router)
app.include_router(azure.router)
app.include_router(gcp.router)
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
    app.include_router(epml.router, dependencies=[_feature_gate("beyondtrust_enabled")])
except ImportError as exc:
    logger.warning("API router 'epml' not loaded: %s", exc)

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
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/vms", response_class=HTMLResponse, include_in_schema=False)
async def vms_page(request: Request):
    if not config_service.get_bool("vmware_enabled", settings.vmware_enabled):
        raise HTTPException(status_code=404, detail="VMware integration is disabled")
    return templates.TemplateResponse("vms/list.html", {"request": request, **_feature_flags()})


@app.get("/proxmox", response_class=HTMLResponse, include_in_schema=False)
async def proxmox_page(request: Request):
    if not config_service.get_bool("proxmox_enabled", settings.proxmox_enabled):
        raise HTTPException(status_code=404, detail="Proxmox integration is disabled")
    return templates.TemplateResponse("proxmox/index.html", {"request": request, **_feature_flags()})


@app.get("/vsphere", response_class=HTMLResponse, include_in_schema=False)
async def vsphere_page(request: Request):
    if not config_service.get_bool("vsphere_enabled", settings.vsphere_enabled):
        raise HTTPException(status_code=404, detail="vSphere integration is disabled")
    return templates.TemplateResponse("vsphere/index.html", {"request": request, **_feature_flags()})


@app.get("/hyperv", response_class=HTMLResponse, include_in_schema=False)
async def hyperv_page(request: Request):
    if not config_service.get_bool("hyperv_enabled", settings.hyperv_enabled):
        raise HTTPException(status_code=404, detail="Hyper-V integration is disabled")
    host = config_service.get("hyperv_host") or settings.hyperv_host
    return templates.TemplateResponse(
        "hyperv/index.html",
        {"request": request, "hyperv_host": host, **_feature_flags()},
    )


@app.get("/nutanix", response_class=HTMLResponse, include_in_schema=False)
async def nutanix_page(request: Request):
    if not config_service.get_bool("nutanix_enabled", settings.nutanix_enabled):
        raise HTTPException(status_code=404, detail="Nutanix integration is disabled")
    host = config_service.get("nutanix_host") or settings.nutanix_host
    return templates.TemplateResponse(
        "nutanix/index.html",
        {"request": request, "nutanix_host": host, **_feature_flags()},
    )


@app.get("/xcpng", response_class=HTMLResponse, include_in_schema=False)
async def xcpng_page(request: Request):
    if not config_service.get_bool("xcpng_enabled", settings.xcpng_enabled):
        raise HTTPException(status_code=404, detail="XCP-ng integration is disabled")
    host = config_service.get("xcpng_host") or settings.xcpng_host
    return templates.TemplateResponse(
        "xcpng/index.html",
        {"request": request, "xcpng_host": host, **_feature_flags()},
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


@app.get("/k8s", response_class=HTMLResponse, include_in_schema=False)
async def k8s_page(request: Request):
    """Kubernetes management page — Phase 3a. Nav-gated on k8s_management_enabled;
    the /api/k8s router is feature-gated."""
    return templates.TemplateResponse("k8s/index.html", {"request": request, **_feature_flags()})


@app.get("/users", response_class=HTMLResponse, include_in_schema=False)
async def users_page(request: Request):
    return templates.TemplateResponse(
        "users/list.html",
        {"request": request, "workgroups": list(settings.workgroups.keys())},
    )


@app.get("/groups", response_class=HTMLResponse, include_in_schema=False)
async def groups_page(request: Request):
    return templates.TemplateResponse(
        "groups/index.html",
        {"request": request, "workgroups": list(settings.workgroups.keys())},
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
    so wizard changes are reflected immediately without a restart)."""
    flags = _feature_flags()
    # AWS/Azure/GCP aren't gated by a feature flag — they're "configured" iff
    # credentials are present. The dashboard uses these to hide tiles on bare installs.
    aws_configured = bool(
        config_service.get("aws_access_key_id")
        or os.environ.get("AWS_ACCESS_KEY_ID", "")
    )
    azure_configured = bool(
        (config_service.get("azure_client_id") or settings.azure_client_id)
        and (config_service.get("azure_subscription_id") or settings.azure_subscription_id)
    )
    gcp_configured = bool(config_service.get("gcp_project_id") or settings.gcp_project_id)
    # Portainer needs both the toggle AND a URL — enabled-but-unconfigured should
    # hide the dashboard tile rather than show a permanently "unavailable" one.
    portainer_configured = flags["portainer_enabled"] and bool(
        config_service.get("portainer_url") or settings.portainer_url
    )
    return {
        "vmware":       flags["vmware_enabled"],
        "beyondtrust":  flags["beyondtrust_enabled"],
        "portainer":    flags["portainer_enabled"],
        # Distinct from the enabled toggle: the dashboard tile hides unless
        # Portainer is both enabled AND has a URL configured.
        "portainer_configured": portainer_configured,
        "ansible":      flags["ansible_enabled"],
        "entitle":      flags["entitle_enabled"],
        "aws":          aws_configured,
        "azure":        azure_configured,
        "gcp":          gcp_configured,
        "proxmox":      flags["proxmox_enabled"],
        "vsphere":      flags["vsphere_enabled"],
        "hyperv":       flags["hyperv_enabled"],
        "nutanix":      flags["nutanix_enabled"],
        "xcpng":        flags["xcpng_enabled"],
        "cost":         flags["cost_explorer_enabled"],
        "admission":    flags["admission_control_enabled"],
        "cloud_database": flags["cloud_database_enabled"],
    }


@app.get("/api/cache/status", tags=["health"])
async def cache_status():
    """Return metadata for all cached keys (debug / admin)."""
    entries = await cache_service.all_entries()
    return {
        "cache_type": "in-memory",
        "entry_count": len(entries),
        "entries": entries,
    }


@app.get("/api/health/powershell", tags=["health"])
async def health_powershell():
    """Test connectivity to the PowerShell wrapper."""
    from .services import powershell
    try:
        result = await powershell.execute("health_check", {})
        return {"status": "ok", "details": result}
    except powershell.PowerShellError as e:
        # Log the real error server-side; return a generic detail (a raw exception
        # string here would leak internal detail — CodeQL py/stack-trace-exposure).
        logger.warning("powershell health check failed: %s", e)
        return JSONResponse(status_code=503,
                            content={"status": "error", "detail": "PowerShell wrapper unavailable"})

