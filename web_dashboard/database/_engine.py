"""The engine, the session factory and the declarative base: the one place that binds them.

Every model module imports ``Base`` from here, and ``web_dashboard.database`` re-exports
all of it. ``create_engine`` runs at import and reads ``settings.database_url`` and the pool
settings, so this module is imported before any connection exists; nothing here connects.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from ..config import settings

# Create database engine.
#
# SQLite → NullPool: connections are just file handles so pooling adds no benefit, and
# a bounded pool is exhausted when several long-running background jobs hold sessions
# open simultaneously (each job + each broadcast_progress call takes one slot). NullPool
# creates and closes a fresh connection per Session, eliminating the timeout entirely.
#
# Postgres → QueuePool, EXPLICITLY sized. The library defaults (pool_size=5,
# max_overflow=10 = 15 max) were adequate only while the job worker ran ONE job at a time
# and the concurrency lived in `replicas`, each replica being its own process with its own
# pool. jobs_worker now runs several jobs in ONE pool, and a job is not one connection:
# _dispatch holds a session for the job's whole duration, several services open their own
# (aws_vm_service, azure_vm_service, gcp_vm_service, oci_vm_service, packer_build_service,
# ansible_local_run_service, vdesktop_service, image_promote_service), and every heartbeat
# beat, every job_service.cancel_check and every streamed terraform output line via
# api.websocket.broadcast_progress opens a transient one.
#
# pool_pre_ping / pool_recycle are not about concurrency and are worth having on their own:
# Azure Postgres Flexible Server and the load balancer in front of it close idle
# connections after minutes, and a pooled-but-dead connection surfaces as an InterfaceError
# in the middle of a job rather than at checkout. A two-hour image-export poller is exposed
# to that at concurrency 1.
#
# Budget, per PROCESS: pool_size + max_overflow. The app runs `gunicorn -w 2` → 2 pools;
# the worker → 1 per replica. So one deployment can hold 3 x (size + overflow) — 30 at the
# defaults — and that whole figure multiplies by the replica count. Keep it under
# (max_connections - 20), the 20 covering the server's management sessions plus
# superuser_reserved_connections. Verify with `SHOW max_connections;`: Azure Burstable
# B1ms is 50, which is why the defaults are 5 + 5 and jobs_worker._limits clamps
# concurrency to 3 there; B2s and every General Purpose tier are 429+.
_is_sqlite = "sqlite" in settings.database_url


def engine_url(raw: str):
    """``raw`` with a bare ``postgresql://`` pinned to the psycopg2 driver.

    psycopg2 is the driver this app ships (requirements.txt), but a URL with no driver
    named leaves the choice to SQLAlchemy, and 2.1 changed its default to psycopg 3.
    Every documented URL is the bare form (the compose files, the CI job, the docs), so
    without this, upgrading SQLAlchemy fails every Postgres install at boot with
    ``No module named 'psycopg'``. A URL that names its driver is left as it is.
    """
    from sqlalchemy.engine import make_url
    url = make_url(raw)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg2")
    return url


_pool_kwargs = {} if _is_sqlite else {
    "pool_size":     settings.db_pool_size,
    "max_overflow":  settings.db_max_overflow,
    "pool_timeout":  settings.db_pool_timeout_s,
    "pool_pre_ping": True,
    "pool_recycle":  settings.db_pool_recycle_s,
}
engine = create_engine(
    engine_url(settings.database_url),
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    poolclass=NullPool if _is_sqlite else QueuePool,
    echo=False,
    **_pool_kwargs,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()
