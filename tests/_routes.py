"""Every route the app serves, flattened, for tests that walk the route table.

FastAPI up to 0.11x copied each included router's routes into ``app.routes``, so a test
could iterate it and see every path. Newer FastAPI keeps an included router as one
``_IncludedRouter`` entry and resolves its routes on demand, so ``app.routes`` holds only
the routes declared on the app itself (52 of ~590 here). A walk over it silently checks
almost nothing. ``fastapi.routing.iter_route_contexts`` is the supported way to see the
effective routes: each one carries the full prefixed ``path``, ``methods``, ``name`` and
``endpoint``, and proxies anything else (``dependant``, ``dependencies``) to the route as
mounted.

Not a test file (no ``test_`` prefix): imported by tests, run by none.
"""


def all_routes(app):
    """Every route as mounted: full path, including routers included into routers."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # FastAPI that still flattens app.routes itself
        return list(app.routes)
    return list(iter_route_contexts(app.routes))


def api_routes(app):
    """``all_routes`` narrowed to HTTP API routes (no mounts, websockets or static)."""
    from fastapi.routing import APIRoute
    return [r for r in all_routes(app) if isinstance(getattr(r, "original_route", r), APIRoute)]
