"""
Azure Automation service — executes PowerShell runbooks on the on-prem Hybrid Runbook Worker.

Used when POWERSHELL_EXECUTION_MODE=automation (cloud deployment).
Provides the same interface as the local subprocess path in powershell.py so
callers need no changes:
  - execute(action, params) → dict
  - execute_streaming(action, params) → AsyncGenerator yielding progress/result events

All jobs target the "on-prem-powershell-workers" Hybrid Worker group so they
run on the on-prem Windows machine that has PowerShell + VMware access.
"""
import asyncio
import base64
import json
import logging
import uuid
from typing import AsyncGenerator

from azure.identity.aio import ManagedIdentityCredential
from azure.mgmt.automation.aio import AutomationClient
from azure.mgmt.automation.models import (
    JobCreateParameters,
    RunbookAssociationProperty,
)

from ..config import settings

logger = logging.getLogger(__name__)

# Terminal job statuses from Azure Automation
_TERMINAL_STATUSES = {"Completed", "Failed", "Stopped", "Suspended", "Disconnected"}

# Runbook name used for all VM / Images PowerShell wrapper calls
_VM_RUNBOOK = "Invoke-VMCLIWrapper"

# Runbook name for on-prem Ansible Docker runs
_ANSIBLE_RUNBOOK = "Invoke-AnsibleDocker"

# How often to poll job status / streams (seconds)
_POLL_INTERVAL = 3

# Hard timeout matching the local subprocess timeout (2 hours)
_JOB_TIMEOUT = 7200


def _automation_client() -> AutomationClient:
    """Return an AutomationClient using the user-assigned Managed Identity.
    AZURE_MI_CLIENT_ID is set to the user-assigned identity's client_id by Terraform
    so we target the same identity that holds the Automation Contributor role.
    """
    import os
    client_id = os.getenv("AZURE_MI_CLIENT_ID")
    credential = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    return AutomationClient(
        credential=credential,
        subscription_id=_get_subscription_id(),
    )


def _get_subscription_id() -> str:
    """Extract subscription ID from the automation account resource group's resource ID,
    or fall back to the ARM_SUBSCRIPTION_ID env var (set by Set-TerraformEnv.ps1 for local use)."""
    import os
    sub_id = os.getenv("ARM_SUBSCRIPTION_ID") or os.getenv("AZURE_SUBSCRIPTION_ID", "")
    if not sub_id:
        raise RuntimeError(
            "Cannot determine Azure subscription ID. "
            "Set AZURE_SUBSCRIPTION_ID in environment / Key Vault."
        )
    return sub_id


async def _create_job(
    client: AutomationClient,
    runbook_name: str,
    parameters: dict,
) -> str:
    """Create an Automation job targeting the Hybrid Worker group. Returns the job name (UUID)."""
    job_name = str(uuid.uuid4())
    await client.job.create(
        resource_group_name=settings.azure_automation_resource_group,
        automation_account_name=settings.azure_automation_account_name,
        job_name=job_name,
        parameters=JobCreateParameters(
            runbook=RunbookAssociationProperty(name=runbook_name),
            parameters={k: str(v) for k, v in parameters.items()},
            run_on=settings.azure_hybrid_worker_group,
        ),
    )
    logger.debug("Created Automation job %s (runbook=%s)", job_name, runbook_name)
    return job_name


async def _poll_until_done(client: AutomationClient, job_name: str) -> str:
    """Poll job status until terminal. Returns the final status string."""
    elapsed = 0
    while elapsed < _JOB_TIMEOUT:
        job = await client.job.get(
            resource_group_name=settings.azure_automation_resource_group,
            automation_account_name=settings.azure_automation_account_name,
            job_name=job_name,
        )
        status = getattr(job, "status", None) or "Unknown"
        if status in _TERMINAL_STATUSES:
            logger.debug("Job %s finished with status=%s", job_name, status)
            return status
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL
    raise TimeoutError(f"Automation job {job_name} did not complete within {_JOB_TIMEOUT}s")


async def _get_output(client: AutomationClient, job_name: str) -> str:
    """Retrieve the full stdout of a completed job via Output job streams.

    The list_by_job() API does NOT return stream_text — only the individual
    get() call does. So we list stream IDs first, then fetch each one.
    (job.get_output() is broken in azure-mgmt-automation 1.0.0: KeyError('IO'))
    """
    rg = settings.azure_automation_resource_group
    acct = settings.azure_automation_account_name
    lines: list[str] = []
    try:
        stream_ids: list[str] = []
        async for stream in client.job_stream.list_by_job(
            resource_group_name=rg,
            automation_account_name=acct,
            job_name=job_name,
            filter="properties/streamType eq 'Output'",
        ):
            sid = getattr(stream, "job_stream_id", None) or ""
            if sid:
                stream_ids.append(sid)

        for sid in stream_ids:
            detail = await client.job_stream.get(
                resource_group_name=rg,
                automation_account_name=acct,
                job_name=job_name,
                job_stream_id=sid,
            )
            text = getattr(detail, "stream_text", None) or ""
            if text.strip():
                lines.append(text.strip())
    except Exception as exc:
        logger.warning("Failed to retrieve job streams for %s: %s", job_name, exc)
    return "\n".join(lines)


def _parse_result(output: str) -> dict:
    """Parse the final JSON result line from job output.

    The PowerShell wrapper writes one JSON object per line; the last JSON line
    is the result (same convention as the local subprocess path).
    """
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError(f"No JSON result found in Automation job output:\n{output[:500]}")


# ── Public API ─────────────────────────────────────────────────────────────────

async def execute(
    action: str,
    params: dict,
    runbook: str = _VM_RUNBOOK,
    raw_params: dict | None = None,
) -> dict:
    """Run a runbook on the Hybrid Worker and return the parsed JSON result.

    By default wraps action+params as {"Action": ..., "JsonParams": ...} for
    Invoke-VMCLIWrapper. Pass raw_params to supply different parameter names
    (e.g. for Invoke-AnsibleDocker which has PlaybookB64, Inventory, etc.).

    Equivalent to PowerShellService.execute() for the cloud execution path.
    """
    job_parameters = raw_params if raw_params is not None else {
        "Action": action,
        "JsonParams": base64.b64encode(json.dumps(params).encode()).decode(),
    }
    async with _automation_client() as client:
        job_name = await _create_job(client, runbook, job_parameters)
        status = await _poll_until_done(client, job_name)
        output = await _get_output(client, job_name)

    if status != "Completed":
        # Include any output for diagnostics
        raise RuntimeError(
            f"Automation job {job_name} ended with status={status}. "
            f"Output: {output[:500]}"
        )
    return _parse_result(output)


async def execute_streaming(
    action: str,
    params: dict,
    runbook: str = _VM_RUNBOOK,
) -> AsyncGenerator[dict, None]:
    """Run a runbook on the Hybrid Worker and yield progress events in real time.

    Yields dicts with the same shape as the local execute_streaming path:
      {"type": "progress", "pct": int, "message": str}
      {"type": "log", "line": str, "timestamp": str}
      {"type": "result", "data": dict}   ← final event

    Progress comes from Azure Automation Output job streams — each Output stream
    record that parses as JSON is forwarded as-is; non-JSON records are wrapped
    as log events.
    """
    seen_stream_ids: set[str] = set()

    async with _automation_client() as client:
        job_name = await _create_job(
            client,
            runbook,
            {"Action": action, "JsonParams": base64.b64encode(json.dumps(params).encode()).decode()},
        )

        elapsed = 0
        while elapsed < _JOB_TIMEOUT:
            # Fetch new Output streams since last poll
            # list_by_job doesn't return stream_text — must call get() per stream
            try:
                rg = settings.azure_automation_resource_group
                acct = settings.azure_automation_account_name
                new_sids: list[str] = []
                async for stream in client.job_stream.list_by_job(
                    resource_group_name=rg,
                    automation_account_name=acct,
                    job_name=job_name,
                    filter="properties/streamType eq 'Output'",
                ):
                    sid = getattr(stream, "job_stream_id", None) or ""
                    if sid and sid not in seen_stream_ids:
                        seen_stream_ids.add(sid)
                        new_sids.append(sid)

                for sid in new_sids:
                    detail = await client.job_stream.get(
                        resource_group_name=rg,
                        automation_account_name=acct,
                        job_name=job_name,
                        job_stream_id=sid,
                    )
                    text = (getattr(detail, "stream_text", None) or "").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                        if isinstance(event, dict) and "type" in event:
                            yield event
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # Non-JSON output → wrap as log event
                    yield {"type": "log", "line": text, "timestamp": ""}
            except Exception as exc:  # noqa: BLE001
                logger.debug("Stream poll error for job %s: %s", job_name, exc)

            # Check job status
            job = await client.job.get(
                resource_group_name=settings.azure_automation_resource_group,
                automation_account_name=settings.azure_automation_account_name,
                job_name=job_name,
            )
            status = getattr(job, "status", None) or "Unknown"
            if status in _TERMINAL_STATUSES:
                break

            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
        else:
            raise TimeoutError(f"Automation job {job_name} did not complete within {_JOB_TIMEOUT}s")

        output = await _get_output(client, job_name)

    if status != "Completed":
        raise RuntimeError(
            f"Automation job {job_name} ended with status={status}. "
            f"Output: {output[:500]}"
        )

    result_data = _parse_result(output)
    yield {"type": "result", "data": result_data}
