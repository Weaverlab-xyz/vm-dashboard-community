"""Entra ID join for Windows servers on AWS and GCP, through Azure Arc.

Windows Server can only be Microsoft Entra joined from Azure. A server elsewhere gets
there by becoming an **Azure Arc-enabled server** first: the Connected Machine agent
(``azcmagent``) registers it as a ``Microsoft.HybridCompute/machines`` resource, and the
same ``AADLoginForWindows`` extension Azure VMs use is then installed on that resource.
The server becomes Entra joined, with no domain controller anywhere. Requirements
Microsoft sets: Windows Server 2025 or later with Desktop Experience, outbound 443 to
the Arc and Entra endpoints, and no Conditional Access.

**One follow-up job per server** (``windows_arc_join``), queued once the deploy has
completed — the same shape as the GCP agent join — because the onboarding play finds the
server's SSH key and administrator through that completed deploy job:

1. :func:`preflight` — the Arc resource providers are registered and the resource group
   exists. Each refusal names the command that fixes it; none is run.
2. :func:`onboard` — the built-in ``arc-onboard-windows.yml`` play runs over OpenSSH on
   the Config-Management runner for that cloud. It checks the OS, installs the agent and
   runs ``azcmagent connect --access-token``.
3. :func:`enable_entra_login` — ``AADLoginForWindows`` on the Arc machine, over ARM REST.
4. The configured Entra groups get Virtual Machine Administrator/User Login on it
   (``windows_server_hook.assign_login_roles``), exactly as on an Azure VM.

**The token.** The connect step needs an ARM credential on the guest. It is an access
token for the dashboard's own Azure identity, minted when the job runs (about an hour),
and it travels ONLY through the runner's secret channel — collect-from-dashboard or an
ephemeral store copy deleted after the run — and is scrubbed from the output. It is never
put in instance metadata (readable with ``compute.instances.get``), never sent as an SSM
parameter (kept in SSM's command history), and never written to either job. It does sit,
briefly, on the guest's ``azcmagent`` command line.

Results land on the DEPLOY job's metadata under the same keys an Azure Entra join uses
(``entra_join``, ``entra_role_assignments``, ``entra_error``), plus ``arc_machine_id``,
so destroy (:func:`teardown`) can remove what was made. A failure is a warning there,
never a failed deploy: the local administrator still reaches the server.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

JOB_TYPE = "windows_arc_join"
MODE = "arc"
PLAYBOOK = os.path.join(os.path.dirname(__file__), "builtin_playbooks",
                        "arc-onboard-windows.yml")
TOKEN_VAR = "arc_access_token"
SENTINEL = "VMDASH-ARC:"
API = "2024-07-10"
PROVIDERS = ("Microsoft.HybridCompute", "Microsoft.GuestConfiguration",
             "Microsoft.HybridConnectivity")
_ARM = "https://management.azure.com"
_EXTENSION = "AADLogin"
_EXTENSION_TIMEOUT_S = 900
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,53}$")
_SENTINEL_RE = re.compile(re.escape(SENTINEL) + r"(CONNECTED|UNSUPPORTED|FAILED)(.*)")

transport = None    # tests set an httpx.MockTransport


class ArcError(Exception):
    """An Arc join cannot proceed; the message says why and what to do."""


def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    from ..config import settings
    val = config_service.get(key)
    if val in (None, ""):
        val = getattr(settings, key, "")
    return default if val in (None, "") else str(val)


def resource_group() -> str:
    return _cfg("arc_resource_group") or _cfg("azure_resource_group")


def location() -> str:
    return _cfg("arc_location") or _cfg("azure_location", "eastus")


def requested(meta_or_req) -> bool:
    """Whether a deploy asked for an Arc Entra join. ``entra_join_mode`` decides when the
    request set it ("" is an explicit no); left unset (None), the
    ``windows_arc_entra_default`` setting does."""
    if isinstance(meta_or_req, dict):
        mode = meta_or_req.get("entra_join_mode")
    else:
        mode = getattr(meta_or_req, "entra_join_mode", None)
    if mode is None:
        from . import config_service
        return config_service.get_bool("windows_arc_entra_default", False)
    return mode == MODE


def deploy_problem(*, is_windows: bool, ad_directory_id: str = "", ssh: bool = True) -> str:
    """Why an Arc join cannot be attempted for this deploy, or "". Never stops a deploy."""
    if not is_windows:
        return "Entra join through Azure Arc applies to Windows images only; skipped"
    if ad_directory_id:
        return ("a server joins Entra ID through Arc or an Active Directory domain, not "
                "both; the Arc Entra join was skipped")
    if not ssh:
        return ("the Arc Entra join runs over OpenSSH, which is off for Windows servers "
                "(windows_ssh_enabled); skipped")
    return ""


def machine_id(subscription: str, name: str) -> str:
    return (f"/subscriptions/{subscription}/resourceGroups/{resource_group()}/providers/"
            f"Microsoft.HybridCompute/machines/{name}")


# ── ARM ───────────────────────────────────────────────────────────────────────

async def _token() -> str:
    from . import azure_service
    credential, _sub = await azure_service._ensure_creds()
    return await azure_service._arm_token(credential)


async def _arm(method: str, path: str, *, params: Optional[dict] = None,
               json: Optional[dict] = None) -> tuple:
    """``(status, body)`` for one ARM call with the dashboard's Azure identity."""
    import httpx
    token = await _token()
    kwargs = {"timeout": 60}
    if transport is not None:
        kwargs["transport"] = transport
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.request(method, f"{_ARM}{path}", params=params or {},
                                    json=json, headers={"Authorization": f"Bearer {token}"})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


async def _subscription() -> str:
    from . import azure_service
    return await azure_service.subscription_id()


async def preflight() -> str:
    """The subscription, after checking it can hold an Arc machine. Raises ArcError."""
    sub = await _subscription()
    if not sub:
        raise ArcError("no Azure subscription is configured (Settings → Azure)")
    missing = []
    for ns in PROVIDERS:
        status, body = await _arm("GET", f"/subscriptions/{sub}/providers/{ns}",
                                  params={"api-version": "2021-04-01"})
        if status == 200 and (body.get("registrationState") or "") != "Registered":
            missing.append(ns)
    if missing:
        raise ArcError("Azure Arc's resource providers are not registered in this "
                       "subscription — run: "
                       + "; ".join(f"az provider register --namespace {ns}" for ns in missing))
    rg = resource_group()
    if not rg:
        raise ArcError("no resource group for Arc machines (arc_resource_group or "
                       "azure_resource_group)")
    status, _ = await _arm("GET", f"/subscriptions/{sub}/resourcegroups/{rg}",
                           params={"api-version": "2021-04-01"})
    if status == 404:
        raise ArcError(f"the Arc resource group {rg} does not exist — create it, or set "
                       f"arc_resource_group")
    return sub


async def enable_entra_login(mid: str) -> dict:
    """Install AADLoginForWindows on an Arc machine and wait for it."""
    status, machine = await _arm("GET", mid, params={"api-version": API})
    if status != 200:
        raise ArcError(f"the Arc machine was not found after onboarding (HTTP {status})")
    loc = machine.get("location") or location()
    url = f"{mid}/extensions/{_EXTENSION}"
    body = {"location": loc, "properties": {
        "publisher": "Microsoft.Azure.ActiveDirectory", "type": "AADLoginForWindows",
        "typeHandlerVersion": "2.1.0.0", "autoUpgradeMinorVersion": True,
        # Required on Arc, even empty; Intune enrolment is not offered here.
        "settings": {"mdmId": ""}}}
    status, out = await _arm("PUT", url, params={"api-version": API}, json=body)
    if status not in (200, 201, 202):
        raise ArcError(f"installing AADLoginForWindows failed (HTTP {status}): "
                       f"{((out.get('error') or {}).get('message') or '')[:300]}")
    deadline = time.monotonic() + _EXTENSION_TIMEOUT_S
    while True:
        state = ((out.get("properties") or {}).get("provisioningState") or "")
        if state == "Succeeded":
            return {"extension": "AADLoginForWindows", "provisioning_state": state}
        if state in ("Failed", "Canceled"):
            detail = ((out.get("properties") or {}).get("instanceView") or {}).get("status") or {}
            raise ArcError(f"AADLoginForWindows ended {state}: "
                           f"{str(detail.get('message') or '')[:400]} — the server needs "
                           f"outbound 443 to enterpriseregistration.windows.net, "
                           f"login.microsoftonline.com and pas.windows.net")
        if time.monotonic() > deadline:
            raise ArcError(f"AADLoginForWindows did not finish within "
                           f"{_EXTENSION_TIMEOUT_S // 60} min")
        await asyncio.sleep(15)
        _status, out = await _arm("GET", url, params={"api-version": API})


async def delete_machine(mid: str) -> None:
    status, out = await _arm("DELETE", mid, params={"api-version": API})
    if status not in (200, 202, 204, 404):
        raise ArcError(f"deleting the Arc machine failed (HTTP {status})")


# ── the onboarding play ───────────────────────────────────────────────────────

def play_vars(*, subscription: str, tenant: str, name: str, deploy_job_id: str) -> dict:
    return {"arc_subscription_id": subscription, "arc_tenant_id": tenant,
            "arc_resource_group": resource_group(), "arc_location": location(),
            "arc_resource_name": name,
            "arc_tags": f"managed-by=vm-dashboard,dashboard-job={deploy_job_id}"}


def render_playbook(variables: dict) -> str:
    """The built-in play with this server's NON-secret vars written in. The cloud
    runners take no extra vars, so they travel inside the play — the token never does."""
    import yaml
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    plays[0].setdefault("vars", {}).update(variables)
    return yaml.safe_dump(plays, sort_keys=False)


def parse_result(output: str) -> tuple:
    """``(state, detail)`` from the LAST sentinel line, or ``("", "")``."""
    found = _SENTINEL_RE.findall(output or "")
    if not found:
        return "", ""
    state, rest = found[-1]
    return state.lower(), rest.strip().strip('"').strip()


def host_from(detail: str) -> str:
    m = re.search(r"host=([A-Za-z0-9-]+)", detail or "")
    return m.group(1) if m else ""


async def onboard(db: Session, *, job_id: str, cloud: str, target: str, token: str,
                  variables: dict) -> tuple:
    """Run the onboarding play on ``target``. ``(exit_code, scrubbed_output)``.

    Reuses the Config-Management runner pieces rather than a second copy of them: the
    VM's own SSH key and administrator from its deploy job, and — for a cloud runner —
    the same credential routes a Password Safe checkout takes (collect-from-dashboard,
    else an ephemeral store secret reaped after the run)."""
    from . import ansible_local_run_service as alr, ansible_local_service, ansible_vm_cmd
    from . import runner_credential as rc
    win = alr.windows_target(alr._find_cloud_deploy_meta(db, cloud, target))
    if not win:
        raise ArcError(f"no completed Windows deploy is recorded for {target}")
    pem = await alr._resolve_cloud_ssh_key(db, cloud, target)
    if not pem:
        raise ArcError(f"no SSH key for {target} — the deploy's keypair secret is missing")
    playbook = render_playbook(variables)
    playbook_b64 = base64.b64encode(playbook.encode()).decode()
    secrets = {TOKEN_VAR: token}
    runner = _cfg(f"ansible_runner_{cloud}") or _cfg("ansible_runner") or "local"
    try:
        if runner == "local":
            output, code = await ansible_local_service.run_playbook(
                asset_b64=playbook_b64, target=target,
                extra_vars={**ansible_vm_cmd.WINDOWS_SSH_VARS,
                            "ansible_user": win.get("user") or ""},
                asset_name="arc-onboard-windows.yml", ssh_key_pem=pem,
                secret_extra_vars=secrets, ps_env=None, db=db)
            return code, alr._scrub_secrets(output, [token])
        if runner not in ("ecs", "gcp"):
            raise ArcError(f"the {runner} runner cannot reach a {cloud} server; set "
                           f"ansible_runner_{cloud}")
        if not rc.cloud_delivery_available(runner):
            raise ArcError(
                f"the {runner.upper()} runner has no way to receive the Arc token: turn on "
                f"collect-from-dashboard (runner credentials) or the ephemeral Secrets "
                f"Manager copy (ansible_cloud_ephemeral_secrets_enabled)")
        cleanup, fetch = [], {}
        entries, manifest = [], ""
        if rc.use_for(runner):
            fetch = alr._runner_fetch(db, runner, job_id, secrets)
        else:
            entries, manifest, cleanup = alr._add_ephemeral_managed_entries(
                runner, [], "", secrets, job_id)
        try:
            code, output = await alr._dispatch_cloud_runner(
                runner=runner, target_ip=target, ansible_user=win.get("user") or "",
                playbook_b64=playbook_b64, ssh_key_b64=base64.b64encode(pem.encode()).decode(),
                job_id=job_id, secret_entries=entries, manifest_b64=manifest,
                ps_env=None, runner_fetch=fetch or None, windows=True)
        finally:
            alr._delete_ephemeral(cleanup)
            if fetch:
                rc.revoke_for_job(db, job_id)
        scrub = [token] + ([fetch["token"]] if fetch.get("token") else [])
        return code, alr._scrub_secrets(output, scrub)
    finally:
        secrets.clear()


# ── the job ───────────────────────────────────────────────────────────────────

def queue(db: Session, *, deploy_job_id: str, cloud: str, vm_name: str, target: str,
          created_by: str, workgroup: Optional[str] = None) -> str:
    """Queue the Arc join of a just-deployed server and record it on the deploy job."""
    from . import job_service
    job = job_service.create_job(db, JOB_TYPE, created_by or "system", workgroup=workgroup,
                                 metadata={"deploy_job_id": deploy_job_id, "cloud": cloud,
                                           "vm_name": vm_name, "target": target})
    job_service.update_metadata(db, deploy_job_id, {"entra_join_mode": MODE,
                                                    "arc_join_job_id": job.id})
    return job.id


async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """Worker entry point for ``windows_arc_join``."""
    from . import azure_service, job_service, windows_server_hook
    deploy_id = meta.get("deploy_job_id") or ""
    cloud, name, target = meta.get("cloud") or "", meta.get("vm_name") or "", meta.get("target") or ""
    job_service.set_running(db, job_id)
    record: dict = {}

    def fail(msg: str) -> None:
        record["entra_error"] = msg
        job_service.update_metadata(db, deploy_id, record)
        job_service.set_failed(db, job_id, msg)

    if not _NAME_RE.match(name or ""):
        return fail(f"{name!r} cannot be an Arc machine name")
    if not target:
        return fail("the deploy recorded no address to reach the server on")
    token = ""
    try:
        job_service.update_progress(db, job_id, 10, "Checking the Azure subscription…")
        sub = await preflight()
        tenant = (await azure_service.own_identity()).get("tid") or _cfg("azure_tenant_id")
        job_service.update_progress(db, job_id, 25, f"Onboarding {name} to Azure Arc…")
        token = await _token()
        code, output = await onboard(
            db, job_id=job_id, cloud=cloud, target=target, token=token,
            variables=play_vars(subscription=sub, tenant=tenant, name=name,
                                deploy_job_id=deploy_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("arc join %s failed before the extension: %s", job_id, exc)
        return fail(str(exc)[:1500])
    finally:
        token = ""
    state, detail = parse_result(output)
    if state != "connected":
        why = {"unsupported": "the server's OS cannot use Entra sign-in through Arc — it "
                              "needs Windows Server 2025 or later with Desktop Experience",
               "failed": "azcmagent connect failed — check outbound 443 from the server "
                         "to the Azure Arc endpoints"}.get(state, "the onboarding play "
                                                                  "ended without reporting")
        return fail(f"{why} ({detail or f'exit {code}'})\n\n{output[-3000:]}")
    mid = machine_id(sub, name)
    record.update(arc_machine_id=mid, arc_host=host_from(detail))
    job_service.update_metadata(db, deploy_id, record)
    try:
        job_service.update_progress(db, job_id, 70, "Installing AADLoginForWindows on the Arc machine…")
        record["entra_join"] = await enable_entra_login(mid)
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc)[:1500])
    await windows_server_hook.assign_login_roles(mid, record)
    job_service.update_metadata(db, deploy_id, record)
    job_service.set_completed(db, job_id, {"arc_machine_id": mid,
                                           "warnings": record.get("entra_role_errors") or []})


async def teardown(meta: dict, result: dict) -> None:
    """Remove the login roles this join created, then the Arc machine. Azure-side only,
    so it runs whatever order the instance itself goes in. The Entra DEVICE object is
    left: it is named by the guest's hostname, which can collide, so deleting one by name
    could remove someone else's — Entra's stale-device cleanup is the place for it."""
    from . import windows_server_hook
    mid = (meta or {}).get("arc_machine_id")
    if not mid:
        return
    await windows_server_hook.teardown_entra(meta, result)
    try:
        await delete_machine(mid)
        result["arc_machine_deleted"] = mid
    except Exception as exc:  # noqa: BLE001
        result["arc_error"] = f"the Arc machine {mid} was not deleted: {exc}"
    if meta.get("arc_host"):
        result["arc_note"] = (f"the Entra device {meta['arc_host']} is not deleted here; "
                              "remove it in Entra or let stale-device cleanup take it")
