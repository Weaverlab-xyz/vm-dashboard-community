"""Run a workstream's post-deploy assets on a VM it has just deployed.

A workstream can name Config-Management assets — a playbook, a script, a package — to run
on every VM deployed into it (``workgroup_service.post_deploy_runs``, administrator-only).
When a cloud deploy completes, its runner calls :func:`queue_runs`, which queues one
ordinary ``ansible_local`` job per asset: the same job ``POST /api/config-mgmt/run``
queues, built by the same ``ansible_run_meta.run_meta`` and aimed by the same
``inventory_service._target_spec`` a bulk run uses for ``job:<id>``. So the run reaches
the VM the way an operator's run would, with the key the VM was deployed with (the
runner looks up the deploy's own ``ssh_key_secret_override`` by address).

What it deliberately does NOT do:

* run anything itself, or block the deploy — the deploy is done; these are separate jobs
  on /jobs, scheduled ``DELAY_MINUTES`` out so sshd has come up;
* carry a secret — a post-deploy run has no secret_vars, become source or managed account,
  which is what lets it skip the secrets:use gate a run with any of those needs;
* run an asset kept on the dashboard's ``local`` storage against a cloud VM — the
  cloud-side runner cannot read it, the same refusal ``/run`` makes;
* fail the deploy — every problem is written onto the deploy row as
  ``post_deploy_error`` and logged.

A deploy can opt out with ``skip_post_deploy`` on its request.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Long enough for a fresh VM's sshd (and a Windows build's OpenSSH bootstrap) to come up;
# the job sits `pending` with a scheduled_for until then, claimable by nothing.
DELAY_MINUTES = 3


def _skipped(meta: dict) -> bool:
    return bool(meta.get("skip_post_deploy")
                or (meta.get("req") or {}).get("skip_post_deploy"))


def queue_runs(db: Session, deploy_job_id: str) -> list:
    """Queue the deploy's workstream post-deploy runs. Returns the new job ids (possibly
    none). Never raises: a deploy that succeeded stays succeeded."""
    try:
        return _queue(db, deploy_job_id)
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.exception("post-deploy runs failed for deploy %s", deploy_job_id)
        try:
            from . import job_service
            job_service.update_metadata(db, deploy_job_id,
                                        {"post_deploy_error": str(exc)[:500]})
        except Exception:  # noqa: BLE001
            pass
        return []


def _queue(db: Session, deploy_job_id: str) -> list:
    from . import (ansible_local_service, ansible_run_meta, inventory_service,
                   job_service, storage_service, workgroup_service)
    from ..utils.clock import utcnow

    deploy = job_service.get_job(db, deploy_job_id)
    if deploy is None or deploy.status != "completed":
        return []
    meta = deploy.metadata_dict or {}
    if _skipped(meta) or meta.get("post_deploy_jobs"):
        # Opted out, or already queued — a re-run of the hook must not double them.
        return []
    runs = workgroup_service.post_deploy_runs(deploy.workgroup, db)
    if not runs:
        return []
    if deploy.job_type not in inventory_service._VM_JOBS:
        return []

    spec = inventory_service._target_spec(inventory_service._vm_item(deploy))
    if isinstance(spec, str):
        job_service.update_metadata(db, deploy.id, {
            "post_deploy_error": f"post-deploy runs skipped: {spec}"})
        return []
    if spec.get("agent_id"):
        # A cloud deploy never lands behind a remote agent; refuse rather than guess.
        job_service.update_metadata(db, deploy.id, {
            "post_deploy_error": "post-deploy runs skipped: this VM is reached through a "
                                 "remote agent, which post-deploy runs do not use."})
        return []

    when = utcnow() + timedelta(minutes=DELAY_MINUTES)
    queued, problems = [], []
    for run in runs:
        asset = run["asset"]
        backend = run.get("asset_backend") or storage_service.active_backend()
        if backend == "local":
            problems.append(f"{asset}: kept on local storage, which the runner that reaches "
                            "cloud VMs cannot read; move it to a cloud backend")
            continue
        atype = ansible_local_service.asset_type(asset)
        payload = SimpleNamespace(asset=asset, target=spec["target"],
                                  cloud=spec.get("cloud") or "", extra_vars={})
        job = job_service.create_job(
            db,
            job_type="ansible_local",
            created_by=deploy.created_by,
            # Same row ownership as an operator's run (api/config_mgmt.run_playbook).
            workgroup="ansible",
            metadata={
                **ansible_run_meta.run_meta(
                    payload,
                    description=f"Ansible ({atype}): {asset} → {spec['target']} "
                                f"(post-deploy, workstream {deploy.workgroup})",
                    asset_backend=backend),
                "post_deploy_of": deploy.id,
            },
            batch_id=deploy.batch_id,
            scheduled_for=when,
        )
        queued.append(job.id)

    update = {"post_deploy_jobs": queued}
    if problems:
        update["post_deploy_error"] = "; ".join(problems)
    job_service.update_metadata(db, deploy.id, update)
    job_service.log_audit(
        db, deploy.created_by, "post_deploy_runs_queued",
        details={"deploy_job_id": deploy.id, "workstream": deploy.workgroup,
                 "assets": [r["asset"] for r in runs], "jobs": queued,
                 "skipped": problems})
    return queued
