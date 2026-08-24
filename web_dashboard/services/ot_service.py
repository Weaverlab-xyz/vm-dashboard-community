"""OT (operational technology) demo features: protocol-tunnel presets and the
one-click OT demo cell orchestrator.

Two surfaces share this module:

* **Standalone OT protocol tunnels** — a thin, preset-carrying wrapper over
  ``terraform_pra_service.provision_api_tunnel`` (the generic ``tunnel_type="tcp"``
  jump the k8s API tunnel already uses). State lives in config_service keys
  (``ot_tunnel_{jump,state,meta}_<slug>``), mirroring the k8s API tunnel's
  ``k8s_api_tunnel_*`` precedent — no DB column. These tunnels hold a reference to
  the shared GCP gateway via ``active_standalone_tunnel_count()``, which
  ``jumpoint_host_service`` adds to its idle-teardown sum.

* **The OT demo cell** (job type ``ot_cell_deploy``, dispatched by ``jobs_worker``)
  — drives one ``queued`` ``gce_deploy`` child through ``gcp_vm_service.run`` (so
  the VM gets the Shell Jump, Password Safe onboarding, shared-gateway reference,
  expiry stamp and inventory row exactly as any GCE deploy does), then wires the
  OT layer on top: a Web Jump to the HMI and a protocol tunnel to the PLC port.
  Every wiring artifact is written into the CHILD's metadata the moment it exists
  (``ot_web_jump_tf_state`` / ``ot_tunnel_tf_state``), because the child row is
  the cell's inventory record: ``gcp_vm_service._run_destroy`` removes whatever of
  the wiring is present, so the Destroy button and the expiry reaper both clean
  the whole cell with no extra teardown path.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class OTError(Exception):
    """Invalid OT request (bad preset, duplicate tunnel, …)."""


class OTCellError(Exception):
    """A cell orchestration step failed. The message is written verbatim to the
    parent job's ``error_message`` — the ONLY field the failed-job page renders —
    so it must carry the remedy, not just the symptom."""


# ── Protocol presets ──────────────────────────────────────────────────────────
# Canonical TCP ports for the OT protocols a PRA protocol tunnel is most often
# demoed against. "custom" (any port) is accepted everywhere a preset key is.

OT_PORT_PRESETS = {
    "modbus":      {"port": 502,   "label": "Modbus TCP"},
    "opcua":       {"port": 4840,  "label": "OPC UA"},
    "dnp3":        {"port": 20000, "label": "DNP3"},
    "s7":          {"port": 102,   "label": "Siemens S7comm"},
    "ethernet-ip": {"port": 44818, "label": "EtherNet/IP"},
}

# A Web Jump renders headless Chromium ON the PRA gateway host. Below ~2 GB the
# renderer is OOM-killed and the session error is indistinguishable from a blocked
# firewall, so the cell deploy refuses to start against an undersized gateway.
MIN_WEB_JUMP_GATEWAY_MB = 2048


def resolve_ports(protocol: str, remote_port: Optional[int] = None,
                  local_port: Optional[int] = None) -> Tuple[int, int]:
    """(local_port, remote_port) for a preset key or ``custom``. The local (rep-side)
    port defaults to the remote port so the operator's Modbus/OPC-UA client config
    reads naturally (127.0.0.1:502 → plc:502)."""
    key = (protocol or "").strip().lower()
    if key == "custom":
        if not remote_port:
            raise OTError("protocol 'custom' requires remote_port")
        rp = int(remote_port)
    else:
        preset = OT_PORT_PRESETS.get(key)
        if not preset:
            raise OTError(
                f"unknown OT protocol '{protocol}' — one of "
                f"{', '.join(sorted(OT_PORT_PRESETS))} or 'custom'")
        rp = int(remote_port or preset["port"])
    lp = int(local_port or rp)
    return lp, rp


def tunnel_slug(name: str) -> str:
    """Config-key-safe slug for a tunnel name (same normalisation the PRA HCL uses)."""
    return re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower())


def _cfg(key: str) -> str:
    from ..config import settings
    from . import config_service
    return config_service.get(key) or str(getattr(settings, key, "") or "")


def resolve_jump_targets(jump_group: Optional[str], jumpoint_name: Optional[str]) -> Tuple[str, str]:
    """Resolve the PRA Jump Group / Jumpoint display names with the same fallback
    chain the GCE Shell Jump uses (override → gcp_* → bt_*)."""
    jg = ((jump_group or "").strip() or _cfg("gcp_bt_jump_group_name")
          or _cfg("bt_jump_group_name"))
    jp = ((jumpoint_name or "").strip() or _cfg("gcp_jumpoint_name")
          or _cfg("bt_jumpoint_name"))
    return jg, jp


def pra_preflight_problem() -> str:
    """"" when PRA is usable, else the remedy string for the failed-job page.
    Mirrors ``portainer_node_service._pra_configured`` (host + OAuth client +
    Jumpoint), which is the set every terraform PRA apply needs."""
    from . import config_service
    if not config_service.get_bool("pra_enabled"):
        return ("PRA integration is disabled (pra_enabled) — the OT cell exists to "
                "demo PRA-brokered access, so enable it in Settings → Integrations "
                "and redeploy. No VM was launched.")
    missing = [k for k in ("bt_api_host", "bt_client_id") if not _cfg(k)]
    _, jumpoint = resolve_jump_targets(None, None)
    if not jumpoint:
        missing.append("bt_jumpoint_name")
    if missing:
        return (f"PRA is not fully configured ({', '.join(missing)} missing) — set "
                "the PRA API host, OAuth client and Gateway name in Settings, then "
                "redeploy the cell. No VM was launched.")
    return ""


# ── Gateway sizing guard ──────────────────────────────────────────────────────

_KNOWN_MACHINE_MB = {
    "e2-micro": 1024, "e2-small": 2048, "e2-medium": 4096,
    "f1-micro": 614, "g1-small": 1740,
}

# Conservative per-vCPU minimums across GCE families (n1 is the smallest of each
# family class), so an unknown-generation type is judged pessimistically.
_FAMILY_MB_PER_VCPU = {"standard": 3840, "highmem": 6656, "highcpu": 900}


def gateway_mem_mb(machine_type: str) -> Optional[int]:
    """Approximate RAM for a GCE machine type; None = unknown (treated as OK,
    because refusing to deploy over a type this map hasn't met would be a worse
    failure than a documented risk)."""
    mt = (machine_type or "").strip().lower()
    if not mt:
        return None
    if mt in _KNOWN_MACHINE_MB:
        return _KNOWN_MACHINE_MB[mt]
    m = re.search(r"custom-(\d+)-(\d+)", mt)          # e2-custom-2-4096, n2-custom-…
    if m:
        return int(m.group(2))
    m = re.match(r"[a-z0-9]+-(standard|highmem|highcpu)-(\d+)$", mt)
    if m:
        return _FAMILY_MB_PER_VCPU[m.group(1)] * int(m.group(2))
    return None


def gateway_size_remedy(machine_type: str, gateway_name: str, source: str) -> str:
    """"" when the gateway can render a Web Jump, else the full remedy string."""
    mem = gateway_mem_mb(machine_type)
    if mem is None or mem >= MIN_WEB_JUMP_GATEWAY_MB:
        return ""
    return (
        f"Gateway sizing guard: {source} is {machine_type} (~{mem} MB RAM). A PRA "
        "Web Jump renders headless Chromium ON the gateway and is OOM-killed below "
        "2 GB — the resulting session failure looks identical to a blocked "
        "firewall. Set gcp_jumpoint_machine_type to e2-small (minimum) or "
        f"e2-medium (preferred) in Settings, delete the gateway VM {gateway_name} "
        "so the next deploy recreates it at the new size, then retry this cell. "
        "No VM was launched."
    )


async def gateway_size_problem(project_id: str, region: str) -> str:
    """Resolve the effective gateway machine type — the LIVE managed VM when it
    exists (a config change never resizes an existing gateway), else the config
    default — and return the remedy string when it is too small, "" otherwise."""
    from . import config_service, gcp_service, jumpoint_host_service as jhs
    name = jhs.managed_host_name("gcp")
    machine, source = "", ""
    try:
        zone = jhs._gcp_jumpoint_zone(region)
        for info in await gcp_service.describe_instances(project_id, zone, [name]):
            if info.get("machine_type") and info.get("status") not in ("", "UNKNOWN", "TERMINATED"):
                machine, source = info["machine_type"], f"the live gateway VM {name}"
                break
    except Exception as exc:  # noqa: BLE001 — the config fallback below still guards
        logger.debug("OT gateway guard: live lookup failed (%s) — using config", exc)
    if not machine:
        machine = config_service.get("gcp_jumpoint_machine_type") or "e2-micro"
        source = "gcp_jumpoint_machine_type (the configured gateway size)"
    return gateway_size_remedy(machine, name, source)


# ── Standalone OT protocol tunnels ────────────────────────────────────────────

def _tunnel_keys(slug: str) -> Tuple[str, str, str]:
    return (f"ot_tunnel_jump_{slug}", f"ot_tunnel_state_{slug}", f"ot_tunnel_meta_{slug}")


async def create_standalone_tunnel(*, name: str, hostname: str, protocol: str,
                                   remote_port: Optional[int], local_port: Optional[int],
                                   jump_group: Optional[str], jumpoint_name: Optional[str],
                                   region: str, created_by: str) -> dict:
    """Provision a generic-TCP PRA protocol tunnel to any OT endpoint and record it
    in config_service. The Jump Group + Jumpoint must already exist in PRA."""
    from . import config_service, terraform_pra_service as pra
    lp, rp = resolve_ports(protocol, remote_port, local_port)
    slug = tunnel_slug(name)
    if not slug:
        raise OTError("tunnel name must contain at least one letter or digit")
    jump_key, state_key, meta_key = _tunnel_keys(slug)
    # get_fresh: a tunnel deleted seconds ago must not block re-creation for the
    # config cache's 5s window, and a just-created one must be seen as a duplicate.
    if (config_service.get_fresh(jump_key) or "").strip():
        raise OTError(f"an OT tunnel named '{name}' already exists — delete it first "
                      "or pick another name")
    jg, jp = resolve_jump_targets(jump_group, jumpoint_name)
    if not (jg and jp):
        raise OTError("PRA Jump Group / Gateway are not configured "
                      "(bt_jump_group_name / bt_jumpoint_name)")
    # Best-effort, like the k8s API tunnel: the target may be reachable through an
    # operator-managed Jumpoint the dashboard doesn't run a host for.
    try:
        from . import jumpoint_host_service
        await jumpoint_host_service.ensure_jumpoint_host("gcp", region)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT tunnel: ensure jumpoint host failed (non-fatal): %s", exc)

    result = await pra.provision_api_tunnel(
        name=name, hostname=hostname, jump_group_name=jg, jumpoint_name=jp,
        local_port=lp, remote_port=rp, tag="OT",
        client_secret=config_service.get("bt_client_secret"),
    )
    config_service.set(jump_key, str(result.get("tunnel_jump_id") or ""))
    config_service.set(state_key, result.get("tf_state_json") or "")
    config_service.set(meta_key, json.dumps({
        "name": name, "hostname": hostname, "protocol": (protocol or "").lower(),
        "local_port": lp, "remote_port": rp,
        "created_by": created_by, "created_at": datetime.utcnow().isoformat() + "Z",
    }))
    logger.info("OT tunnel '%s' provisioned (jump id %s, %s -> %s:%s)",
                name, result.get("tunnel_jump_id"), lp, hostname, rp)
    return {"slug": slug, "tunnel_jump_id": str(result.get("tunnel_jump_id") or ""),
            "local_port": lp, "remote_port": rp}


async def delete_standalone_tunnel(slug: str) -> dict:
    """TF-destroy a standalone tunnel from its stored state and remove its config
    rows (``delete``, not blanking — a blanked row would still be enumerated)."""
    from . import config_service, terraform_pra_service as pra
    jump_key, state_key, meta_key = _tunnel_keys(tunnel_slug(slug))
    if not (config_service.get_fresh(jump_key) or "").strip():
        return {"ok": True, "removed": False}
    state = config_service.get_fresh(state_key)
    if state:
        try:
            await pra.remove_api_tunnel(state)
        except Exception as exc:  # noqa: BLE001 — mirror the k8s API tunnel: clear anyway
            logger.warning("OT tunnel %s: TF destroy failed (clearing keys anyway — "
                           "the jump item may need manual removal in PRA): %s", slug, exc)
    for key in (jump_key, state_key, meta_key):
        config_service.delete(key)
    return {"ok": True, "removed": True}


def list_standalone_tunnels() -> list:
    """All recorded standalone OT tunnels (from their ``ot_tunnel_meta_*`` rows)."""
    from . import config_service
    out = []
    for row in config_service.list_all():
        key = row.get("key") or ""
        if not key.startswith("ot_tunnel_meta_"):
            continue
        slug = key[len("ot_tunnel_meta_"):]
        jump_id = (config_service.get_fresh(f"ot_tunnel_jump_{slug}") or "").strip()
        if not jump_id:
            continue
        try:
            meta = json.loads(config_service.get_fresh(key) or "{}")
        except (ValueError, TypeError):
            meta = {}
        meta.update({"slug": slug, "tunnel_jump_id": jump_id})
        out.append(meta)
    return out


def active_standalone_tunnel_count() -> int:
    """Live standalone OT tunnels — a reference term in the shared GCP gateway's
    idle-teardown sum (``jumpoint_host_service``), so tearing down a cloud database
    can't reap the gateway from under a tunnel an operator is mid-session on.
    Deliberately NOT exception-swallowing: the caller's whole teardown pass is
    best-effort, and an error must mean "don't reap", never "count is zero"."""
    from . import config_service
    count = 0
    for row in config_service.list_all():
        key = row.get("key") or ""
        if key.startswith("ot_tunnel_jump_") and (config_service.get(key) or "").strip():
            count += 1
    return count


# ── The OT demo cell orchestrator (job type: ot_cell_deploy) ──────────────────

def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


async def run_cell_deploy(job_id: str, meta: dict) -> None:
    """Run one ``ot_cell_deploy`` job (deploy mode or rewire mode).

    Deploy mode: metadata carries ``children`` = [{job_id, instance_name, req}] —
    one queued ``gce_deploy`` child this parent drives — plus project/zone/region.
    Rewire mode: metadata carries ``rewire_child_job_id`` — re-run only the wiring
    steps whose ``*_tf_state`` is absent on an existing, completed cell."""
    from . import job_service
    db = _get_db_session()
    try:
        rewire_child = (meta.get("rewire_child_job_id") or "").strip()
        if rewire_child:
            await _run_rewire(db, job_id, rewire_child)
            return

        children = meta.get("children") or []
        child_id = (children[0].get("job_id") if children else "") or ""
        if not child_id:
            job_service.set_failed(db, job_id, "OT cell parent has no child VM job — "
                                               "deploy the cell again from the OT tab.")
            return
        project_id, region = meta["project_id"], meta.get("region") or ""

        problem = pra_preflight_problem()
        if problem:
            job_service.set_cancelled(db, child_id)
            job_service.set_failed(db, job_id, problem)
            return

        job_service.update_progress(db, job_id, 5,
                                    "Checking the PRA gateway size (a Web Jump needs ≥2 GB)…")
        remedy = await gateway_size_problem(project_id, region)
        if remedy:
            job_service.set_cancelled(db, child_id)
            job_service.set_failed(db, job_id, remedy)
            return

        child_row = job_service.get_job(db, child_id)
        if child_row is None:
            job_service.set_failed(db, job_id, f"child VM job {child_id} not found")
            return
        child_meta = child_row.metadata_dict

        job_service.update_progress(db, job_id, 12,
                                    f"Deploying the OT cell VM ({child_meta.get('instance_name')})…")
        # The child gets everything a normal GCE deploy gets — Shell Jump, Password
        # Safe onboarding, the shared-gateway reference (jumpoint_host_id), the
        # expiry stamp — because it IS a normal gce_deploy, just driven from here.
        # _run_deploy owns the child's terminal status; the only way run() can
        # RAISE is before the child ever leaves `queued` (e.g. a malformed stored
        # request), and a queued row nothing will drive again must be cancelled,
        # not abandoned — the reconciler skips queued by design.
        from . import gcp_vm_service
        try:
            await gcp_vm_service.run(child_id, "gce_deploy", child_meta)
        except Exception as exc:  # noqa: BLE001
            job_service.set_cancelled(db, child_id)
            raise OTCellError(
                f"The cell VM deploy could not start: {exc} — the VM job was "
                f"cancelled and nothing was created. Deploy a new cell.")

        db.expire_all()
        child_row = job_service.get_job(db, child_id)
        if child_row is None or child_row.status != "completed":
            err = (child_row.error_message if child_row else "") or f"see job {child_id}"
            job_service.set_failed(db, job_id,
                f"The cell VM deploy failed: {err} — nothing was wired. Fix the cause "
                f"and deploy a new cell (job {child_id} holds the VM detail).")
            return

        summary = await _wire_cell(db, job_id, child_id, child_row.metadata_dict)
        job_service.set_completed(db, job_id, summary)
    except OTCellError as exc:
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("ot_cell_deploy %s failed", job_id)
        job_service.set_failed(db, job_id, f"OT cell orchestration error: {exc}")
    finally:
        db.close()


async def _run_rewire(db, job_id: str, child_id: str) -> None:
    from . import job_service
    child = job_service.get_job(db, child_id)
    if child is None or child.job_type != "gce_deploy" or not child.metadata_dict.get("ot_cell"):
        job_service.set_failed(db, job_id, f"{child_id} is not an OT cell VM job — "
                                           "nothing to re-wire.")
        return
    cmeta = child.metadata_dict
    if cmeta.get("destroyed"):
        job_service.set_failed(db, job_id, "This cell has been destroyed — deploy a "
                                           "new one instead of re-wiring.")
        return
    if child.status != "completed":
        job_service.set_failed(db, job_id, f"The cell's VM job is {child.status} — "
                                           "re-wire applies only to a deployed cell.")
        return
    problem = pra_preflight_problem()
    if problem:
        job_service.set_failed(db, job_id, problem)
        return
    summary = await _wire_cell(db, job_id, child_id, cmeta)
    summary["rewired"] = True
    job_service.set_completed(db, job_id, summary)


async def _wire_cell(db, parent_id: str, child_id: str, cmeta: dict) -> dict:
    """Provision the OT access layer for a deployed cell VM, skipping any step whose
    Terraform state already exists (which is what makes the rewire path idempotent).
    Every artifact is persisted onto the CHILD's metadata before the next step runs,
    so a failure part-way leaves nothing untracked for the destroy path."""
    from . import config_service, job_service, terraform_pra_service as pra

    vm = cmeta.get("instance_name") or "ot-cell"
    ip = cmeta.get("private_ip") or cmeta.get("public_ip")
    if not ip:
        raise OTCellError(
            f"VM {vm} reported no IP address — it may have landed outside the expected "
            f"subnet. Destroy the cell and redeploy; job {child_id} has the VM detail.")

    ot = cmeta.get("ot_params") or {}
    hmi_port = int(ot.get("hmi_port") or 1881)
    protocol = (ot.get("protocol") or "modbus").lower()
    local_port, remote_port = resolve_ports(protocol, ot.get("plc_port"),
                                            ot.get("tunnel_local_port"))
    jump_group, jumpoint = resolve_jump_targets(ot.get("jump_group"), ot.get("jumpoint_name"))
    client_secret = config_service.get("bt_client_secret")
    rewire_hint = (f"Fix the cause, then use the cell's Re-wire button (POST "
                   f"/api/ot/cell/{child_id}/rewire) — it retries only the missing "
                   f"pieces. The VM was left running.")

    # The Web Jump and tunnel connect THROUGH the shared gateway host. The child
    # normally holds the host reference (jumpoint_host_id) from its own deploy; if
    # that ensure failed mid-deploy, repair it now — otherwise an idle-teardown
    # from an unrelated feature could reap the gateway from under this cell.
    if not (cmeta.get("jumpoint_host_id")
            or (cmeta.get("jumpoint_mode") == "paired" and cmeta.get("jumpoint_name"))):
        from . import jumpoint_host_service
        job_service.update_progress(db, parent_id, 78, "Ensuring the shared BeyondTrust Gateway host…")
        region = cmeta.get("region") or ""
        host = None
        try:
            host = await jumpoint_host_service.ensure_jumpoint_host("gcp", region)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OT cell %s: gateway ensure failed: %s", vm, exc)
        if not host:
            raise OTCellError(
                f"The cell VM {vm} is deployed, but the shared BeyondTrust Gateway "
                "host could not be started (check the GCP project and the gateway "
                f"deploy key, gcp_cloud_run_docker_deploy_key). {rewire_hint}")
        # Mirror _JumpointRef.record's shared shape — never jumpoint_name, which
        # would trigger the paired-delete branch in _run_destroy.
        job_service.update_metadata(db, child_id, {
            "jumpoint_mode": "shared", "jumpoint_host_id": host, "jumpoint_region": region})
        cmeta.update({"jumpoint_mode": "shared", "jumpoint_host_id": host,
                      "jumpoint_region": region})

    hmi_url = f"http://{ip}:{hmi_port}"
    if not cmeta.get("ot_web_jump_tf_state"):
        job_service.update_progress(db, parent_id, 84,
                                    f"Provisioning the PRA Web Jump to the HMI ({hmi_url})…")
        try:
            res = await pra.provision_web_jump(
                name=f"ot-{vm}-hmi", url=hmi_url, jump_group_name=jump_group,
                jumpoint_name=jumpoint, tag="OT", verify_certificate=False,
                client_secret=client_secret)
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} is deployed (Shell Jump / Password Safe as "
                f"selected), but the HMI Web Jump failed: {exc}. {rewire_hint}")
        wired = {"ot_web_jump_id": str(res.get("web_jump_id") or ""),
                 "ot_web_jump_tf_state": res.get("tf_state_json") or "",
                 "ot_hmi_url": hmi_url}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    if not cmeta.get("ot_tunnel_tf_state"):
        job_service.update_progress(
            db, parent_id, 92,
            f"Provisioning the PRA protocol tunnel ({protocol} → {ip}:{remote_port})…")
        try:
            res = await pra.provision_api_tunnel(
                name=f"ot-{vm}-{protocol}", hostname=ip, jump_group_name=jump_group,
                jumpoint_name=jumpoint, local_port=local_port, remote_port=remote_port,
                tag="OT", client_secret=client_secret)
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} and its HMI Web Jump are in place, but the "
                f"{protocol} protocol tunnel failed: {exc}. {rewire_hint}")
        wired = {"ot_tunnel_jump_id": str(res.get("tunnel_jump_id") or ""),
                 "ot_tunnel_tf_state": res.get("tf_state_json") or "",
                 "ot_tunnel_protocol": protocol,
                 "ot_tunnel_local_port": local_port,
                 "ot_tunnel_remote_port": remote_port}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    return {
        "vm_job_id": child_id,
        "instance_name": vm,
        "private_ip": ip,
        "hmi_url": cmeta.get("ot_hmi_url") or hmi_url,
        "web_jump_id": cmeta.get("ot_web_jump_id") or "",
        "tunnel_jump_id": cmeta.get("ot_tunnel_jump_id") or "",
        "tunnel_protocol": protocol,
        "tunnel_local_port": local_port,
        "tunnel_remote_port": remote_port,
        "shell_jump_id": cmeta.get("bt_shell_jump_id") or "",
    }
