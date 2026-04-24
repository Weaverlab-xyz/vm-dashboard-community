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
from .database import SessionLocal, User, create_admin_user, init_db
from .services import cache_service
from .services import config_service

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs(settings.log_dir, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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

    _bootstrap_first_run_admin()

    warmers = [
        asyncio.create_task(_warm_aws_amis(),               name="warm_aws_amis"),
        asyncio.create_task(_warm_aws_network_opts(),       name="warm_aws_network_opts"),
        asyncio.create_task(_warm_aws_instances(),          name="warm_aws_instances"),

        asyncio.create_task(_warm_azure_images(),           name="warm_azure_images"),
        asyncio.create_task(_warm_azure_network_opts(),     name="warm_azure_network_opts"),
    ]
    if settings.portainer_enabled:
        warmers.append(
            asyncio.create_task(_warm_portainer_containers(), name="warm_portainer_containers")
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


async def _warm_azure_images() -> None:
    from .services import azure_service
    await _warm_loop(
        "azure_images",
        fetcher=lambda: azure_service.list_private_images(
            settings.azure_shared_image_gallery,
            settings.azure_gallery_resource_group,
            settings.azure_resource_group,
        ),
        key_fn=lambda: cache_service.key_global("azure_images"),
        ttl=cache_service.TTL["azure_images"],
    )


async def _warm_azure_network_opts() -> None:
    from .services import azure_service
    await _warm_loop(
        "azure_network_opts",
        fetcher=lambda: azure_service.get_network_options(
            settings.azure_location,
            settings.azure_vnet_resource_group,
            settings.azure_resource_group,
        ),
        key_fn=lambda: cache_service.key_global("azure_network_opts"),
        ttl=cache_service.TTL["azure_network_opts"],
    )


async def _warm_portainer_containers() -> None:
    """Periodically refresh all Portainer container state into the DB cache."""
    from .database import SessionLocal
    from .services import container_inventory_service

    interval = 60  # seconds — matches portainer_service in-memory cache TTL
    while True:
        db = SessionLocal()
        try:
            await container_inventory_service.populate_all_workgroups(
                db, list(settings.workgroups.keys())
            )
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

_SETUP_BYPASS_PREFIXES = ("/setup", "/api/setup", "/static", "/api/health", "/api/features")

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
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["*"],
)


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
from .api import auth, jobs, websocket, aws, azure, mfa, tokens, users, groups, setup  # noqa: E402
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
app.include_router(auth.router)
app.include_router(mfa.router)
app.include_router(tokens.router)
app.include_router(users.router)
app.include_router(groups.router)
app.include_router(jobs.router)
app.include_router(websocket.router)
app.include_router(aws.router)
app.include_router(azure.router)

# MCP server — mounted as a sub-ASGI app so SSE streams pass through unmodified
app.mount("/mcp", get_mcp_asgi_app())

try:
    from .api import vms  # noqa: E402
    app.include_router(vms.router, dependencies=[_feature_gate("vmware_enabled")])
except ImportError:
    pass

try:
    from .api import containers  # noqa: E402
    app.include_router(containers.router, dependencies=[_feature_gate("portainer_enabled")])
except ImportError:
    pass

try:
    from .api import config_mgmt  # noqa: E402
    app.include_router(config_mgmt.router, dependencies=[_feature_gate("ansible_enabled")])
except ImportError:
    pass

try:
    from .api import approvals  # noqa: E402
    app.include_router(approvals.router, dependencies=[_feature_gate("entitle_enabled")])
except ImportError:
    pass


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


@app.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs_page(request: Request):
    return templates.TemplateResponse("jobs/list.html", {"request": request, **_feature_flags()})


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


@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request, **_feature_flags()})


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
    return {
        "vmware":       flags["vmware_enabled"],
        "beyondtrust":  flags["beyondtrust_enabled"],
        "portainer":    flags["portainer_enabled"],
        "ansible":      flags["ansible_enabled"],
        "entitle":      flags["entitle_enabled"],
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
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})

