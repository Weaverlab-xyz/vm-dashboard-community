"""EPM for Linux: does the configured site answer, and are agent packages built?

The two reads the EPM-L page already makes (``epml_service.list_packages`` and
``get_build_status``), on a schedule. Neither changes anything on the site: listing
packages and reading the build flag are the side-effect-free pair -- ``trigger_build`` and
the installation token are never called from here.

EPM-L is configured once for the instance (site id plus a PAT, or the platform identity),
so this is one item, not one per tenant. An instance that never configured it reports
nothing rather than a failure, the same way ``bt_tenants`` reports nothing with no tenants.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import core

logger = logging.getLogger(__name__)


def item_from(*, packages: Optional[list], build: str = "", error: str = "") -> core.Item:
    """The site's health from what the two reads returned. Pure.

    ``error`` set means the API refused or did not answer; ``packages`` is then ignored.
    """
    checks = []
    if error:
        checks.append(core.check("api", "Site and API", core.FAIL, error))
        return _item(checks)
    checks.append(core.check("api", "Site and API", core.OK, "The EPM-L API answered."))
    count = len(packages or [])
    if count:
        checks.append(core.check("packages", "Agent packages", core.OK,
                                 f"{count} agent package(s) ready to download."))
    elif build == "building":
        checks.append(core.check("packages", "Agent packages", core.UNKNOWN,
                                 "A package build is running; check again when it ends."))
    else:
        checks.append(core.check(
            "packages", "Agent packages", core.WARN,
            "No agent packages are built, so a Linux guest cannot be enrolled. Build them "
            "from the EPM-L page."))
    return _item(checks)


def _item(checks: list) -> core.Item:
    return core.Item(object_id="epml:site", name="EPM for Linux site", kind="EPM-L",
                     href="/settings", checks=checks)


def configured() -> bool:
    """Whether a site and a credential are set, without making a call."""
    from ... import epml_service
    try:
        epml_service._get_api_base()
        epml_service._auth_headers()
        return True
    except epml_service.EpmlError:
        return False


async def collect(db) -> list:
    from ... import epml_service
    if not configured():
        return []
    try:
        packages = await epml_service.list_packages()
        build = ""
        if not packages:
            build = epml_service._extract_build_status(await epml_service.get_build_status())
        return [item_from(packages=packages, build=build)]
    except epml_service.EpmlError as exc:
        return [item_from(packages=None, error=core.text(exc, 400))]
    except Exception as exc:  # noqa: BLE001 - an unreachable site is a result, not a crash
        logger.warning("health: EPM-L read raised", exc_info=True)
        return [item_from(packages=None,
                          error=f"The EPM-L API did not answer ({type(exc).__name__}).")]
