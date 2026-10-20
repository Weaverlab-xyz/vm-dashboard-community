"""Power-state attributes: keep a VM's Password Safe records in step with its power state.

When a power operation completes, apply the configured "online" or "offline" attribute to
the VM's managed system and its linked asset, and remove the other one — so a Smart Rule
keyed on that attribute (pause rotation on a stopped host, say) follows the VM without an
operator touching Password Safe.

**Hooked after completion, on every path, never before.** There is no single function all
power ops pass through: cloud ops finish in ``*_vm_service._run_power`` (which the suspend
sweeper, the spend cap and change-window schedules also enqueue into), directly-dialled
hypervisors in each router's ``_run_power_op``, agent-bound ones in ``api/agent.complete``,
and POV environments in ``pov_env_service.run_env_power``. Each calls :func:`after_power`
once its job row is terminal, so a stamp can never claim a power change that failed.

**Never allowed to fail a power job.** :func:`after_power` swallows everything; the outcome
is one Live Output line on the job, naming each record or the reason nothing was written.

**Nothing is created in the tenant.** The two AttributeIDs are picked in Settings from the
tenant's own vocabulary. A POV's guests live in the POV's OWN tenant, whose ids differ, so
there the configured ids are translated by NAME (type + value) and a tenant without that
pair is skipped and said so — see feedback "no silent tenant side effects".

**Address matching refuses to guess.** A VM the dashboard onboarded is found by the system
id it recorded. Anything else is matched by IP, and private addresses repeat across labs:
an IP that matches more than one managed system is skipped as ambiguous rather than
stamping somebody else's host offline.
"""
import functools
import logging
from typing import Optional

from ..config import settings
from . import cache_service, config_service, job_service, ps_api_service
from . import ps_attribute_catalog as pac

logger = logging.getLogger(__name__)

STATE_ONLINE = "online"
STATE_OFFLINE = "offline"

# Every power verb any path uses, classified. A verb that is not here is "no change" — and
# tests/test_ps_power_attrs.py fails when a router's BULK_OPS or the agent's WRITE_VERBS
# grows one this table does not name, so that default is always a decision.
STATE_OF_OP = {
    # cloud `action`, hypervisor `op`
    "start": STATE_ONLINE,
    "stop": STATE_OFFLINE,
    "shutdown": STATE_OFFLINE,
    # Paused, saved and suspended guests are unreachable — a rotation against one fails
    # exactly as against a stopped one, so for Password Safe they are offline.
    "pause": STATE_OFFLINE,
    "save": STATE_OFFLINE,
    "suspend": STATE_OFFLINE,
    "resume": STATE_ONLINE,
    "unpause": STATE_ONLINE,
    "reboot": "",
    "reset": "",
    "hard_reboot": "",
    # agent verbs
    "power_on": STATE_ONLINE,
    "power_off": STATE_OFFLINE,
    "power_reset": "",
    "restart": "",
    "snapshot": "",
    "set_tags": "",
    # POV runstates
    "running": STATE_ONLINE,
    "suspended": STATE_OFFLINE,
    "stopped": STATE_OFFLINE,
    "halted": STATE_OFFLINE,
}

# The same strings api/ps_attributes writes, so /audit's prefix filter groups a power
# stamp with every other attribute change. Spelled again rather than imported because a
# service importing from api/ is backwards; tests pin the two copies equal.
AUDIT_ASSIGN = "attributes.assign"
AUDIT_REMOVE = "attributes.remove"

_INDEX_CACHE = "ps_object_index"   # NOT under "ps_attributes": an attribute write
_VOCAB_CACHE = "ps_vocabulary"     # changes neither, so invalidating them would only cost


def state_of(op) -> str:
    return STATE_OF_OP.get(str(op or "").strip().lower(), "")


def enabled() -> bool:
    return (config_service.get_bool("password_safe_enabled", settings.password_safe_enabled)
            and config_service.get_bool("passwordsafe_power_attributes_enabled",
                                        settings.passwordsafe_power_attributes_enabled))


def configured_ids() -> dict:
    """``{state: attribute_id}`` from Settings, blanks included."""
    return {STATE_ONLINE: (config_service.get("passwordsafe_power_attr_online") or "").strip(),
            STATE_OFFLINE: (config_service.get("passwordsafe_power_attr_offline") or "").strip()}


async def _cached(name: str, tenant_key: str, fetch):
    """One read per tenant per TTL per process, however many power jobs ask at once — a
    bulk stop of fifty VMs must not read the whole tenant fifty times. get_or_refresh
    coalesces concurrent misses on one key, which is what makes that true."""
    data, _at = await cache_service.get_or_refresh(
        cache_service.key_param(name, tenant=tenant_key),
        cache_service.TTL["ps_attributes"], fetch)
    return data


async def _index(tenant, tenant_key: str) -> Optional[dict]:
    async def fetch():
        raw = await ps_api_service.read_attribute_inventory(tenant=tenant)
        if not raw.get("reachable"):
            raise ps_api_service.PSApiError(raw.get("detail") or "Password Safe unreachable")
        return pac.build_index(raw["assets"]["rows"], raw["managed_systems"]["rows"])
    try:
        return await _cached(_INDEX_CACHE, tenant_key, fetch)
    except Exception:  # noqa: BLE001 — an id-only stamp still works without the index
        logger.warning("power attributes: could not read the Password Safe index",
                       exc_info=True)
        return None


async def _vocabulary(tenant, tenant_key: str) -> list:
    async def fetch():
        raw = await ps_api_service.read_attribute_vocabulary(tenant=tenant)
        if raw["state"] != ps_api_service.PROBE_OK:
            raise ps_api_service.PSApiError(raw.get("detail") or "vocabulary unreadable")
        return pac.build_vocabulary(raw["types"], raw["values_by_type"])
    return await _cached(_VOCAB_CACHE, tenant_key, fetch)


async def ids_for_tenant(tenant, tenant_key: str) -> tuple:
    """``({state: id}, skip_reason)``. The configured ids as-is for the default tenant;
    translated by type and value NAME for another one."""
    ids = configured_ids()
    if tenant is None:
        return ids, ""
    try:
        home = await _vocabulary(None, "default")
        there = await _vocabulary(tenant, tenant_key)
    except Exception:  # noqa: BLE001
        logger.warning("power attributes: vocabulary read failed", exc_info=True)
        return {}, ("the attribute vocabulary could not be read, so the configured values "
                    "could not be found in this POV's Password Safe")
    out = {}
    for state, attr_id in ids.items():
        if not attr_id:
            out[state] = ""
            continue
        type_row, value = pac.find_value(home, attr_id)
        if type_row is None:
            return {}, f"attribute {attr_id} is no longer in this dashboard's Password Safe"
        match = next((v["attribute_id"]
                      for t in there if t["name"].lower() == type_row["name"].lower()
                      and not t["read_only"]
                      for v in t["values"]
                      if v["value"].lower() == value["value"].lower()), "")
        if not match:
            return {}, (f"this POV's Password Safe has no assignable "
                        f"'{type_row['name']} = {value['value']}' — create it there to "
                        f"stamp these guests")
        out[state] = match
    return out, ""


def resolve_refs(resource: dict, index: Optional[dict]) -> tuple:
    """``(refs, skip_reason)`` — the records to write for one VM.

    A recorded system id is written even with no index (it is exact); the index only adds
    the linked asset. An address match that lands on more than one managed system is
    refused: those are different machines sharing a private IP, and stamping one offline
    because the other stopped is the failure this guards against.
    """
    system_id = str(resource.get("ps_system_id") or "").strip()
    if index is None:
        if system_id:
            return [f"{pac.KIND_SYSTEM}:{system_id}"], ""
        return [], "the Password Safe index could not be read, and this VM has no recorded system id"
    refs, basis = pac.match_refs({**resource, "kind": "vm"}, index)
    if not refs and system_id:
        refs, basis = [f"{pac.KIND_SYSTEM}:{system_id}"], pac.BASIS_ID
    if not refs:
        return [], "no Password Safe record matches this VM"
    if basis == pac.BASIS_IP:
        systems = [r for r in refs if r.startswith(pac.KIND_SYSTEM + ":")]
        assets = [r for r in refs if r.startswith(pac.KIND_ASSET + ":")]
        if len(systems) > 1 or (not systems and len(assets) > 1):
            return [], (f"its address matches {len(refs)} Password Safe records "
                        f"({', '.join(refs[:4])}) — skipped rather than guessed")
    return refs, ""


async def stamp(db, *, state: str, resource: dict, job_id: str = "", actor: str = "system",
                tenant=None, tenant_key: str = "default") -> tuple:
    """Write one VM's power state. Returns ``(outcomes, skip_reason)``.

    ``outcomes`` is one ``{"ref", "action", "ok", "error"}`` per write attempted. Raises
    nothing the caller has to handle beyond what :func:`after_power` already swallows.
    """
    ids, reason = await ids_for_tenant(tenant, tenant_key)
    if reason:
        return [], reason
    target, opposite = ids.get(state, ""), ids.get(
        STATE_OFFLINE if state == STATE_ONLINE else STATE_ONLINE, "")
    if not target and not opposite:
        return [], f"no attribute is configured for {state}"

    refs, reason = resolve_refs(resource, await _index(tenant, tenant_key))
    if reason:
        return [], reason

    outcomes = []
    for ref in refs:
        kind, _, object_id = ref.partition(":")
        writer = (ps_api_service.set_asset_attribute if kind == pac.KIND_ASSET
                  else ps_api_service.set_managed_system_attribute)
        # Apply first, then remove: a record briefly carrying both is harmless, one
        # carrying neither would read as "unknown" to a rule in between.
        for attr_id, assign in ((target, True), (opposite, False)):
            if not attr_id:
                continue
            try:
                await writer(object_id, attr_id, assign=assign, tenant=tenant)
            except Exception as exc:  # noqa: BLE001 — one record must not end the run
                logger.warning("power attributes: %s %s on %s failed",
                               "assign" if assign else "remove", attr_id, ref, exc_info=True)
                outcomes.append({"ref": ref, "action": "assign" if assign else "remove",
                                 "ok": False,
                                 "error": (str(exc) if isinstance(exc, ps_api_service.PSApiError)
                                           else "see the dashboard log")})
                continue
            outcomes.append({"ref": ref, "action": "assign" if assign else "remove",
                             "ok": True, "error": ""})
            # After the call, never before — a row claiming a write the tenant refused is
            # worse than no row.
            job_service.log_audit(
                db, actor or "system", AUDIT_ASSIGN if assign else AUDIT_REMOVE,
                target_vm=ref,
                details={"attribute_id": attr_id, "trigger": "power", "state": state,
                         "job_id": job_id, "tenant": tenant_key})
    if any(o["ok"] for o in outcomes):
        # Process-local, like the cache: the web process re-reads on its own TTL when the
        # write happened in the jobs worker.
        await cache_service.invalidate_prefix("ps_attributes")
    return outcomes, ""


def _summary(state: str, outcomes: list, reason: str) -> str:
    label = "Online" if state == STATE_ONLINE else "Offline"
    if reason:
        return f"Password Safe: {label} attribute not applied — {reason}."
    applied = sorted({o["ref"] for o in outcomes if o["ok"] and o["action"] == "assign"})
    failed = [f"{o['action']} on {o['ref']}: {o['error']}" for o in outcomes if not o["ok"]]
    line = f"Password Safe: {label} attribute applied to {', '.join(applied) or 'nothing'}"
    if failed:
        line += f"; FAILED {'; '.join(failed)}"
    return line + "."


async def after_power(db, job_id: str, op, resource: Optional[dict], *, actor: str = "",
                      tenant=None, tenant_key: str = "default") -> Optional[dict]:
    """The hook every power path calls once its job is terminal. Never raises.

    Returns ``None`` when it did nothing at all (feature off, a verb that changes no power
    state), otherwise ``{"outcomes", "reason"}`` — also written to the job's Live Output.
    """
    try:
        state = state_of(op)
        if not state or not enabled():
            return None
        if tenant is None and not ps_api_service.configured():
            return None
        if not resource:
            outcomes, reason = [], "this VM has no recorded Password Safe system and no known address"
        else:
            outcomes, reason = await stamp(db, state=state, resource=resource, job_id=job_id,
                                           actor=actor, tenant=tenant, tenant_key=tenant_key)
        if job_id:
            job_service.append_job_log(db, job_id, _summary(state, outcomes, reason))
        return {"outcomes": outcomes, "reason": reason}
    except Exception:  # noqa: BLE001 — a finished power job must never fail on this
        logger.exception("power attributes: stamping after job %s failed", job_id)
        return None


# ── resources, per path ──────────────────────────────────────────────────────

def resource_for_deploy(db, deploy_job_id) -> Optional[dict]:
    """``{ps_system_id, ips}`` from the deploy Job a power job names, or None for a VM the
    dashboard did not deploy. The same extraction /inventory uses."""
    if not deploy_job_id:
        return None
    from ..database import Job
    from . import inventory_service
    deploy = db.query(Job).filter(Job.id == deploy_job_id).first()
    if deploy is None or deploy.job_type not in inventory_service._VM_JOBS:
        return None
    item = inventory_service._vm_item(deploy)
    return {"ps_system_id": item.get("ps_system_id", ""), "ips": item.get("ips") or []}


def resource_for_power_job(db, job_id: str) -> Optional[dict]:
    from ..database import Job
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        return None
    return resource_for_deploy(db, (job.metadata_dict or {}).get("deploy_job_id"))


def resource_for_hypervisor(db, connection_id: str, vm_id) -> Optional[dict]:
    """``{ips}`` from the synced inventory cache — last-known, so a stopped guest still has
    the address it was matched by. None when the cache has no row or no address."""
    from ..database import HypervisorVMCache
    import json
    if not connection_id or vm_id in (None, ""):
        return None
    row = db.query(HypervisorVMCache).filter(
        HypervisorVMCache.connection_id == connection_id,
        HypervisorVMCache.vm_id == str(vm_id)).first()
    if row is None:
        return None
    try:
        ips = pac.addr_list(*(json.loads(row.ip_addresses or "[]") or []))
    except ValueError:
        ips = []
    return {"ips": ips} if ips else None


def _never_raises(fn):
    """The path hooks run inside a power job's own try block, after it is terminal. An
    exception escaping one would land in that block's ``set_failed`` and turn a power op
    that worked red — so the hooks are sealed here rather than trusted at each call site."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("power attributes: %s failed", fn.__name__)
            return None
    return wrapper


@_never_raises
async def after_cloud_power(db, job_id: str, action: str) -> None:
    """The cloud runners' hook: resource from the deploy Job the power job names."""
    if not state_of(action) or not enabled():
        return
    from ..database import Job
    job = db.query(Job).filter(Job.id == job_id).first()
    await after_power(db, job_id, action, resource_for_power_job(db, job_id),
                      actor=getattr(job, "created_by", "") or "system")


@_never_raises
async def after_hypervisor_power(db, job_id: str, op, connection_id: str, vm_id) -> None:
    if not state_of(op) or not enabled():
        return
    from ..database import Job
    job = db.query(Job).filter(Job.id == job_id).first()
    await after_power(db, job_id, op, resource_for_hypervisor(db, connection_id, vm_id),
                      actor=getattr(job, "created_by", "") or "system")


@_never_raises
async def after_agent_job(db, job, applied: str) -> None:
    """``agent_hypervisor`` completion. Completed power verbs only: a failed op may not
    have moved the VM, and claiming it did is the thing this module must never do.
    ``applied`` is what ``agent_service.complete_job`` returned — an oversized result is
    turned into a failure there, so the agent's own "completed" is not the answer."""
    if getattr(job, "job_type", "") != "agent_hypervisor" or applied != "completed":
        return
    meta = job.metadata_dict or {}
    verb = meta.get("verb")
    if not state_of(verb) or not enabled():
        return
    connection_id = str(meta.get("connection_id") or job.cloud_resource_id or "")
    await after_power(db, job.id, verb,
                      resource_for_hypervisor(db, connection_id, meta.get("target_id")),
                      actor=job.created_by or "system")


@_never_raises
async def after_pov_power(db, job_id: str, env, runstate: str) -> None:
    """Every wired guest of one POV environment, in that POV's own tenant."""
    state = state_of(runstate)
    if not state or not enabled():
        return
    try:
        from ..database import PovEnvironmentVM
        from . import bt_tenant_service
        if not getattr(env, "ps_tenant_id", None):
            return  # no Password Safe on this POV, so nothing was ever wired
        t = bt_tenant_service.resolve(db, "password_safe", env.ps_tenant_id)
        tenant = ps_api_service.tenant_creds(t.base_url, t.client_id, t.secret)
        vms = db.query(PovEnvironmentVM).filter(
            PovEnvironmentVM.environment_id == env.id).all()
    except Exception:  # noqa: BLE001
        logger.exception("power attributes: could not resolve POV %s", getattr(env, "id", ""))
        return
    for vm in vms:
        resource = {"ps_system_id": vm.ps_managed_system_id or "",
                    "ips": pac.addr_list(vm.private_ip)}
        if not resource["ps_system_id"] and not resource["ips"]:
            continue
        result = await after_power(db, "", runstate, resource,
                                   actor="system", tenant=tenant,
                                   tenant_key=f"pov:{env.ps_tenant_id}")
        if result is not None and job_id:
            job_service.append_job_log(
                db, job_id, f"{vm.name or vm.platform_vm_id}: " +
                _summary(state, result["outcomes"], result["reason"]))
