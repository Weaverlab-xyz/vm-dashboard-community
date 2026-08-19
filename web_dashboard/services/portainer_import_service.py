"""Replay a Portainer migration bundle into the CONFIGURED Portainer.

Dispatched from ``jobs_worker`` as ``portainer_import``. The bundle is produced by
``web_dashboard.scripts.portainer_migrate`` on the operator's own machine, because
a Portainer ``.tar.gz`` backup is a BoltDB database that only Portainer can read
and that only restores into a *pristine* instance — which a managed node
deliberately never is (it initializes its admin at container start to dodge the
init-timeout lockout). See that package's docstring for the full reasoning.

This is a MERGE, not a restore. Every object is matched by name first and created
only if absent, so running the same bundle twice is a no-op rather than a pile of
duplicates. That is also why ``portainer_import`` is a singleton job type: two
concurrent imports would each ask "does this team exist yet", both get no, and both
create it.

Three deliberate refusals, each of which would otherwise produce something that
looks like success:

  * **Environments are never created.** The bundle records them under ``reference``
    precisely so they cannot be replayed: they address a local Docker socket or a
    LAN host that this node has no route to, and the node has no Docker socket of
    its own either. An imported environment could only ever be a dead one. Wiring
    a real one means an Edge agent.
  * **Imported users are never administrators.** ``portainer_service.create_user``
    accepts role 1, and a bundle is a hand-editable file that came from somewhere
    else — so the role is forced to STANDARD here and the downgrade is reported.
  * **Stacks are only deployed when the operator names a live environment.**
    ``deploy_stack`` actually creates containers; there is no "store the definition"
    call. Without a target there is nothing safe to do but skip and say so.
"""
import logging
import secrets
import string

from . import config_service, job_service, portainer_service
from ..scripts.portainer_migrate import bundle as bundle_mod

logger = logging.getLogger(__name__)

#: Generated password length for imported users. Portainer enforces a 12-char
#: minimum; this matches the node admin's own generated credential.
_PASSWORD_LEN = 24

#: Never created by an import. The managed node's admin already exists and the
#: dashboard holds its password — colliding with it is how you lose access to the
#: node. A name match would skip it anyway; this makes the intent explicit.
_RESERVED_USERNAMES = frozenset({"admin"})


def _generate_password() -> str:
    """A password for an imported user. Excludes shell/quote-hostile characters so a
    credential can be pasted into a terminal or a vault form without escaping."""
    alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+"
    return "".join(secrets.choice(alphabet) for _ in range(_PASSWORD_LEN))


def summarize(doc: dict) -> dict:
    """What an import WOULD touch, without calling Portainer. Pure.

    Used by the API to answer an upload immediately, so the operator sees the shape
    of the change before a job is queued.
    """
    data = (doc or {}).get("data") or {}
    counts = {name: len(data.get(name) or []) for name in bundle_mod.SECTIONS}
    reference = (doc or {}).get("reference") or {}
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "environments_seen": len(reference.get("endpoints") or []),
        "meta": (doc or {}).get("meta") or {},
        "not_migrated": (doc or {}).get("not_migrated") or [],
    }


async def _import_teams(db, job_id: str, teams: list, result: dict) -> dict:
    """Create missing teams. Returns ``{old_id: new_id}`` for the membership pass."""
    job_service.update_progress(db, job_id, 20, "Importing teams")
    mapping: dict = {}
    for team in teams:
        name = str(team.get("Name") or "").strip()
        if not name:
            result["skipped"].append("a team with no name")
            continue
        try:
            existing = await portainer_service.find_team(name)
            if existing:
                mapping[team.get("Id")] = existing.get("Id")
                result["matched"].append(f"team {name}")
                continue
            created = await portainer_service.create_team(name)
            mapping[team.get("Id")] = created.get("Id")
            result["created"].append(f"team {name}")
        except portainer_service.PortainerError as exc:
            result["failed"].append(f"team {name}: {exc}")
    return mapping


async def _import_users(db, job_id: str, users: list, result: dict) -> dict:
    """Create missing users with fresh generated passwords. Returns ``{old: new}``."""
    job_service.update_progress(db, job_id, 35, "Importing users")
    mapping: dict = {}
    for user in users:
        name = str(user.get("Username") or "").strip()
        if not name:
            result["skipped"].append("a user with no username")
            continue
        if name.lower() in _RESERVED_USERNAMES:
            result["skipped"].append(
                f"user {name} (reserved — this node's own admin)")
            continue
        try:
            existing = await portainer_service.find_user(name)
            if existing:
                mapping[user.get("Id")] = existing.get("Id")
                result["matched"].append(f"user {name}")
                continue
            # The role is NOT taken from the bundle: create_user would accept
            # administrator, and a bundle is hand-editable input from elsewhere.
            if int(user.get("Role") or 0) == portainer_service.USER_ROLE_ADMIN:
                result["notes"].append(
                    f"user {name} was an administrator in the source and was created "
                    f"as a standard user — promote it in Portainer if that is intended")
            password = _generate_password()
            created = await portainer_service.create_user(
                name, password, role=portainer_service.USER_ROLE_STANDARD)
            mapping[user.get("Id")] = created.get("Id")
            result["created"].append(f"user {name}")
            result["passwords"][name] = password
        except portainer_service.PortainerError as exc:
            result["failed"].append(f"user {name}: {exc}")
    return mapping


async def _import_memberships(db, job_id: str, memberships: list,
                              user_map: dict, team_map: dict, result: dict) -> None:
    """Join imported users to imported teams, translating both ids.

    A membership whose user or team never landed is skipped rather than guessed: the
    ids in the bundle belong to the SOURCE Portainer, so an untranslated id would
    address whatever unrelated object happens to hold that number here.
    """
    job_service.update_progress(db, job_id, 55, "Importing team memberships")
    if not memberships:
        return
    # Read what is already there ONCE. add_team_member is idempotent and returns the
    # existing row rather than raising, so it gives the caller no way to tell "created"
    # from "was already a member" — without this, a re-import reports memberships as
    # created and the operator can't see that nothing actually changed.
    try:
        seen = {(int(m.get("UserID") or -1), int(m.get("TeamID") or -1))
                for m in await portainer_service.list_team_memberships()}
    except portainer_service.PortainerError as exc:
        result["failed"].append(f"could not read existing team memberships: {exc}")
        return
    for m in memberships:
        old_user, old_team = m.get("UserID"), m.get("TeamID")
        new_user, new_team = user_map.get(old_user), team_map.get(old_team)
        if not new_user or not new_team:
            result["skipped"].append(
                f"membership user={old_user} team={old_team} (its user or team was "
                f"not imported, and a source id means nothing on this server)")
            continue
        pair = (int(new_user), int(new_team))
        if pair in seen:
            result["matched"].append(f"membership {new_user}->{new_team}")
            continue
        role = m.get("Role")
        if int(role or 0) not in (portainer_service.TEAM_ROLE_LEADER,
                                 portainer_service.TEAM_ROLE_MEMBER):
            role = portainer_service.TEAM_ROLE_MEMBER
        try:
            await portainer_service.add_team_member(new_user, new_team, role)
            result["created"].append(f"membership {new_user}->{new_team}")
            seen.add(pair)
        except portainer_service.PortainerError as exc:
            result["failed"].append(f"membership {old_user}->{old_team}: {exc}")


async def _import_registries(db, job_id: str, registries: list, result: dict) -> None:
    """Recreate registries by name + URL, WITHOUT credentials (they never travel)."""
    job_service.update_progress(db, job_id, 70, "Importing registries")
    for reg in registries:
        name = str(reg.get("Name") or "").strip()
        url = str(reg.get("URL") or "").strip()
        if not name:
            result["skipped"].append("a registry with no name")
            continue
        try:
            if await portainer_service.find_registry(name):
                result["matched"].append(f"registry {name}")
                continue
            await portainer_service.create_registry(
                name, url, registry_type=int(reg.get("Type") or 3))
            result["created"].append(f"registry {name}")
            if reg.get("Authentication"):
                # Say this per registry: a registry that looks configured but cannot
                # pull is the failure mode worth naming out loud.
                result["notes"].append(
                    f"registry {name} used authentication in the source; its password "
                    f"could not travel, so it was created unauthenticated — re-enter "
                    f"the credential in Portainer")
        except portainer_service.PortainerError as exc:
            result["failed"].append(f"registry {name}: {exc}")


async def _import_stacks(db, job_id: str, stacks: list, endpoint_id: int,
                         result: dict) -> None:
    """Deploy stacks onto ``endpoint_id``, or skip every one of them.

    There is no "save the definition without running it" call in Portainer's API —
    ``deploy_stack`` creates containers. So this needs a real, reachable environment,
    which is exactly what a fresh managed node does not have until an Edge agent is
    registered.
    """
    if not stacks:
        return
    if not endpoint_id:
        result["skipped"].append(
            f"{len(stacks)} stack(s) — no target environment was chosen. Deploying a "
            f"stack creates containers, so it needs a live environment; register one "
            f"(Edge agent) and import again with it selected.")
        return
    job_service.update_progress(db, job_id, 85, "Deploying stacks")
    for stack in stacks:
        name = str(stack.get("Name") or "").strip()
        compose = stack.get("StackFileContent") or ""
        if not name:
            result["skipped"].append("a stack with no name")
            continue
        if not compose.strip():
            result["skipped"].append(
                f"stack {name} (its compose file was unreadable at export time)")
            continue
        env = [{"key": e.get("name"), "value": e.get("value")}
               for e in (stack.get("Env") or []) if e.get("name")]
        try:
            await portainer_service.deploy_stack(endpoint_id, name, compose, env)
            result["created"].append(f"stack {name}")
        except portainer_service.PortainerError as exc:
            result["failed"].append(f"stack {name}: {exc}")


async def run_import(db, *, job_id: str, meta: dict) -> None:
    """Replay a bundle into the configured Portainer.

    Owns its own ``set_completed`` / ``set_failed`` and does not raise, matching the
    handler contract every other ``jobs_worker`` service function follows.
    """
    try:
        job_service.set_running(db, job_id)

        if not config_service.get("portainer_url"):
            job_service.set_failed(
                db, job_id,
                "No Portainer server is configured. Deploy a managed node, or set the "
                "URL and API token in Settings -> Containers, then import again.")
            return

        doc = meta.get("bundle")
        if not isinstance(doc, dict):
            job_service.set_failed(
                db, job_id, "The import job carries no bundle to replay.")
            return
        problems = bundle_mod.validate(doc)
        if problems:
            # Validate again here even though the upload route already did: the job
            # may have been queued by an older build, and a half-applied import is
            # much worse than a rejected one.
            job_service.set_failed(
                db, job_id,
                "This bundle cannot be imported: " + "; ".join(problems))
            return

        data = doc.get("data") or {}
        result = {"created": [], "matched": [], "skipped": [], "failed": [],
                  "notes": [], "passwords": {}}

        job_service.update_progress(db, job_id, 10, "Reading the bundle")
        team_map = await _import_teams(db, job_id, data.get("teams") or [], result)
        user_map = await _import_users(db, job_id, data.get("users") or [], result)
        await _import_memberships(db, job_id, data.get("team_memberships") or [],
                                  user_map, team_map, result)
        await _import_registries(db, job_id, data.get("registries") or [], result)

        try:
            endpoint_id = int(meta.get("endpoint_id") or 0)
        except (TypeError, ValueError):
            endpoint_id = 0
        await _import_stacks(db, job_id, data.get("stacks") or [], endpoint_id, result)

        # Environments are never created — see the module docstring. Recorded as a
        # note so the job result is honest about the one thing an import cannot do.
        seen = len(((doc.get("reference") or {}).get("endpoints")) or [])
        if seen:
            result["notes"].append(
                f"{seen} environment connection(s) were recorded in the bundle but not "
                f"imported: they address a local Docker socket or a LAN host this node "
                f"cannot reach. Register an Edge agent instead.")

        completion = {
            "created": result["created"],
            "matched": result["matched"],
            "skipped": result["skipped"],
            "notes": result["notes"],
            "counts": {"created": len(result["created"]),
                       "matched": len(result["matched"]),
                       "skipped": len(result["skipped"]),
                       "failed": len(result["failed"])},
            "source": (doc.get("meta") or {}).get("source_url") or "",
        }
        # Generated credentials are the one thing an operator MUST capture from this
        # job, since Portainer will not show them again.
        if result["passwords"]:
            completion["generated_passwords"] = result["passwords"]

        if result["failed"]:
            # A partial import is the normal shape of failure here — every object is
            # independent — so the failures go in the message (the job page renders
            # error_message only) and the successes are kept in the result metadata.
            completion["failed"] = result["failed"]
            job_service.set_failed(
                db, job_id,
                f"Imported {len(result['created'])} object(s), matched "
                f"{len(result['matched'])} that already existed, and {len(result['failed'])} "
                f"failed: " + "; ".join(result["failed"][:10])
                + ("; ..." if len(result["failed"]) > 10 else ""),
                result=completion)
            return

        job_service.set_completed(db, job_id, completion)
    except Exception as exc:
        logger.exception("Portainer bundle import failed")
        job_service.set_failed(db, job_id, str(exc))
