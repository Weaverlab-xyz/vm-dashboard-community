"""Demo-cell roles on a POV: what a guest adds to the wire-up because of what it IS.

The demo profile's cells (``ot_service``, ``netcell_service``) are not something a POV can
host as they stand. Each one deploys its own VMs, wires PRA against the install's global
``bt_*`` tenant and the shared Gateway host, and keeps its state on a Job row. A POV has
none of those: its VMs come from a template, its PRA tenant comes from the registry, its
Gateway and Jump Group are its own, and its state is ``PovEnvironmentVM`` rows torn down
by one ordered destroy. So a cell is not *ported* here. It is split into layers and each
layer goes where a POV already puts that kind of thing:

* **Topology** — the cell's image is a VM in a POV template (``PovCloudTemplateVM`` with
  a ``cell_role``, or a Skytap guest an operator marks).
* **Per-VM wiring beyond the default** — this module. ``pov_wireup.wire_vm`` builds the
  ordinary jump item every guest gets; :func:`wire_extras` adds what the role needs on
  top, against the SAME tenant, Gateway and Jump Group, and persists each item to the VM
  row the moment it exists.
* **Narrative** — the use-case cards in ``pov_cards``.

What a role deliberately does NOT carry over from the demo cell, and why:

* **The PRA Vault checkout** (``ot_service._wire_ps_checkout``). A POV already links its
  Password Safe accounts into PRA through ``pov_pra_ps_link``; a second path would be a
  second Vault account for the same credential.
* **The Purdue firewall and the DMZ broker.** A POV network is one CIDR today. The
  zoning, the Entitle plant agent and the function adapter are their own later slices,
  not something a per-VM hook can do.

Roles are Linux-only, both of them: the OT simulator image and VyOS are Debian-derived.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM
from . import terraform_pra_service

logger = logging.getLogger(__name__)

OT_SIM = "ot-sim"
VYOS = "vyos"
VALID_CELL_ROLES = (OT_SIM, VYOS)

LABELS = {
    OT_SIM: "OT simulator (FUXA HMI + PLC protocols)",
    VYOS: "Network device (VyOS)",
}

# The FUXA HMI's port in the ot-sim image (provisioners/ot/README.md). The demo cell
# lets an operator move it; a POV guest is built from the published image, so it is
# where the image puts it.
OT_HMI_PORT = 1881


class CellRoleError(Exception):
    """A cell role could not be set. The message names the remedy."""


def normalize(value) -> str | None:
    """``None`` for "no role", the role key otherwise, or a refusal."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw not in VALID_CELL_ROLES:
        raise CellRoleError(
            f"{raw!r} is not a cell role; use one of {', '.join(VALID_CELL_ROLES)}, or "
            f"leave it blank for an ordinary target")
    return raw


def check_os(cell_role: str | None, os_family: str) -> None:
    """Refuse a role on a guest that cannot be one. Both images are Linux."""
    if cell_role and (os_family or "").strip().lower() != "linux":
        raise CellRoleError(
            f"the {cell_role} role is a Linux image; a {os_family or 'unknown-OS'} guest "
            f"cannot play it")


def ot_protocols() -> list:
    """The PLC protocols an ot-sim guest gets one tunnel each for.

    Read off ``ot_service.OT_PORT_PRESETS`` rather than listed here, so the port a POV
    tunnels to is the port the demo cell tunnels to and the image serves — the table
    ``tests/test_ot_ports.py`` holds against the bake. The fieldbus protocols the image
    actually simulates (``cell`` and ``plc``); DNP3 is offered nowhere because nothing
    answers on it, and the k3s API is the cell's platform, not the plant.
    """
    from . import ot_service
    return [key for key, preset in ot_service.OT_PORT_PRESETS.items()
            if preset.get("cell") and preset.get("plc")]


def planned(vm: PovEnvironmentVM) -> list:
    """The extra items this guest's role calls for, as ``(key, spec)`` pairs, in order."""
    if vm.cell_role != OT_SIM:
        return []
    from . import ot_service
    items = [("hmi", {"kind": "web_jump", "port": OT_HMI_PORT})]
    for protocol in ot_protocols():
        local_port, remote_port = ot_service.resolve_ports(protocol)
        items.append((f"tunnel:{protocol}", {"kind": "tunnel", "protocol": protocol,
                                             "local_port": local_port,
                                             "remote_port": remote_port}))
    return items


def has_artifacts(vm: PovEnvironmentVM) -> bool:
    """Whether this row holds any role item a teardown would have to remove."""
    return any((item or {}).get("tf_state") for item in vm.cell_artifacts_dict.values())


def entitle_skip_reason(vm: PovEnvironmentVM) -> str:
    """Why this guest is not registered in Entitle because of its role, or ""."""
    if vm.cell_role == VYOS:
        # The netcell refuses Entitle for the same reason (api/netcell.py): the SSH
        # ephemeral-accounts connector creates a Unix user with useradd, and VyOS
        # rebuilds its accounts from its own config on every commit, so the grant
        # would vanish the first time anyone changed the device.
        return ("VyOS keeps its accounts in its own config and rebuilds them on every "
                "commit, so an account Entitle mints over SSH would not survive")
    return ""


def describe(vm: PovEnvironmentVM) -> dict:
    """The role and its item counts, for the VMs tab and the Setup steps."""
    if not vm.cell_role:
        return {"cell_role": "", "cell_label": "", "cell_items": 0, "cell_wired": 0}
    items = planned(vm)
    have = vm.cell_artifacts_dict
    return {
        "cell_role": vm.cell_role,
        "cell_label": LABELS.get(vm.cell_role, vm.cell_role),
        "cell_items": len(items),
        "cell_wired": sum(1 for key, _ in items if (have.get(key) or {}).get("tf_state")),
    }


def _store(db: Session, vm: PovEnvironmentVM, key: str, entry: dict | None) -> None:
    """Persist one item (or its removal) immediately — see ``pov_wireup._record``."""
    current = vm.cell_artifacts_dict
    if entry is None:
        current.pop(key, None)
    else:
        current[key] = entry
    vm.cell_artifacts_dict = current
    db.commit()


async def wire_extras(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, *,
                      tenant: dict, gateway: str, tag: str) -> list:
    """Create the role's items that do not exist yet. Returns job-log lines.

    ``tenant`` is ``pov_wireup.tenant_override``'s dict and ``gateway`` its Gateway, the
    same two the base jump item was built with, so every item a POV owns is in one Jump
    Group behind one Gateway — which is what a vendor scoped to that group relies on.

    Idempotent per item: a re-run builds exactly what is missing. Best-effort per item,
    for the reason ``wire_vm`` gives: PRA accepts duplicates, so the record is the guard.
    """
    items = planned(vm)
    if not items:
        return []
    have = vm.cell_artifacts_dict
    label = f"{env.name}-{vm.name}"
    lines = []
    for key, spec in items:
        if (have.get(key) or {}).get("tf_state"):
            continue
        try:
            if spec["kind"] == "web_jump":
                url = f"http://{vm.private_ip}:{spec['port']}"
                result = await terraform_pra_service.provision_web_jump(
                    name=f"{label}-hmi", url=url,
                    jump_group_name=tenant["jump_group_name"], jumpoint_name=gateway,
                    tag=tag, verify_certificate=False, tenant=tenant["env"],
                    comments=f"POV {env.name}: FUXA HMI on {vm.name}")
                entry = {"kind": "web_jump", "url": url,
                         "id": str(result.get("web_jump_id") or "")}
                what = f"HMI Web Jump ({url})"
            else:
                protocol = spec["protocol"]
                result = await terraform_pra_service.provision_api_tunnel(
                    name=f"{label}-{protocol}", hostname=vm.private_ip,
                    jump_group_name=tenant["jump_group_name"], jumpoint_name=gateway,
                    local_port=spec["local_port"], remote_port=spec["remote_port"],
                    tag=tag, tenant=tenant["env"],
                    comments=f"POV {env.name}: {protocol} on {vm.name}")
                entry = {"kind": "tunnel", "protocol": protocol,
                         "local_port": spec["local_port"],
                         "remote_port": spec["remote_port"],
                         "id": str(result.get("tunnel_jump_id") or "")}
                what = f"{protocol} tunnel (:{spec['remote_port']})"
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: %s item %s on %s failed", env.id, vm.cell_role, key,
                           vm.name, exc_info=True)
            lines.append(f"{vm.name}: {key} FAILED — {exc}")
            continue

        entry["tf_state"] = result.get("tf_state_json") or ""
        _store(db, vm, key, entry)
        if not entry["tf_state"]:
            logger.error("POV %s: %s on %s was created but terraform returned no state",
                         env.id, key, vm.name)
            lines.append(f"{vm.name}: {what} {entry['id']} created but NO STATE — remove "
                         f"it in PRA by hand at teardown.")
        else:
            lines.append(f"{vm.name}: {what} {entry['id']} created.")
    return lines


async def unwire_extras(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, *,
                        tenant: dict) -> tuple[int, int]:
    """Destroy every role item this row holds. Returns ``(removed, problems)``.

    Against the tenant the items were created in, for the reason
    ``pov_wireup.unwire_jump_items`` gives. Each item is cleared only on success, so a
    re-run finishes what this one could not.
    """
    removed = problems = 0
    for key, entry in list(vm.cell_artifacts_dict.items()):
        state = (entry or {}).get("tf_state") or ""
        if not state:
            continue
        try:
            if entry.get("kind") == "web_jump":
                await terraform_pra_service.remove_web_jump(state, tenant=tenant["env"])
            else:
                await terraform_pra_service.remove_api_tunnel(state, tenant=tenant["env"])
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: removing %s on %s failed", env.id, key, vm.name,
                           exc_info=True)
            problems += 1
            continue
        _store(db, vm, key, None)
        removed += 1
    return removed, problems


def seed_from_template(db: Session, env: PovEnvironment) -> int:
    """Copy each cloud template VM's ``cell_role`` onto the POV VM of the same name.

    Only onto a row whose ``cell_role`` is still NULL — never over an answer, including
    an operator's deliberate blank, which is stored as "" for exactly this reason. Names
    match because a cloud driver tags each instance with its template VM's name and the
    adapter reads that tag back as the VM's name. A Skytap POV has no dashboard-side
    template, so this does nothing there. Returns how many rows it set.
    """
    from ..database import PovCloudTemplateVM
    from . import lab_platforms

    if (env.platform or "") not in lab_platforms.CLOUD_PLATFORMS or not env.template_id:
        return 0
    roles = {
        (row.name or "").strip().lower(): row.cell_role
        for row in db.query(PovCloudTemplateVM).filter(
            PovCloudTemplateVM.template_id == env.template_id).all()
        if row.cell_role
    }
    if not roles:
        return 0
    seeded = 0
    for vm in db.query(PovEnvironmentVM).filter(
            PovEnvironmentVM.environment_id == env.id).all():
        if vm.cell_role is None and (vm.name or "").strip().lower() in roles:
            vm.cell_role = roles[(vm.name or "").strip().lower()]
            seeded += 1
    if seeded:
        db.commit()
    return seeded


def set_role(db: Session, env: PovEnvironment, vm: PovEnvironmentVM, value) -> str:
    """An operator's answer for one guest. Returns a job-log line.

    Refused while the guest still holds role items: changing what a guest is with its
    Web Jump and tunnels still in PRA would leave them unreachable by any teardown that
    asks the new role what to remove. Blank is stored as "" rather than NULL so a later
    template seed does not put the role straight back.
    """
    role = normalize(value)
    check_os(role, vm.guest_os)
    if has_artifacts(vm) and role != vm.cell_role:
        raise CellRoleError(
            f"{vm.name} still has its {vm.cell_role} items in PRA, and a teardown asks "
            f"the role what to remove. They come out when this POV is destroyed; until "
            f"then the role stays as it is.")
    vm.cell_role = role or ""
    db.commit()
    return (f"{vm.name} is now {LABELS[role]}." if role
            else f"{vm.name} is now an ordinary target.")
