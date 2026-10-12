"""Workload credentials: is the plumbing that hands non-human workloads their
credentials working?

The Resources area already reports each SPIRE lab, workload token and workload cloud
credential by its recorded state and expiry. This area is what sits around them:

* **Workload Credentials leases** -- the dynamic credential the dashboard itself runs
  on, per cloud and purpose, where that cloud takes its credentials from Workload
  Credentials. A lease in failure backoff is a cloud the dashboard cannot reach.
* **Kubernetes ServiceAccount tokens in Password Safe** -- whether the vault holds a
  credential that works and the PRA Vault mirror is still linked to it (Password Safe's
  live answer, one read per cluster).
* **SPIRE labs** -- the k3s link, federation with the dashboard's own SPIRE server, the
  admin SVID's expiry and the trust bundle.
* **Agent cells** -- the cell's state and its agent's personal access token.
* **Integration tokens** -- personal access tokens that are in use and about to expire,
  by name only.

Everything but the Password Safe read is stored. Nothing here reads who holds which
account, group or role: that is Identity Security Insights' data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .. import core
from .resources import stored_check

logger = logging.getLogger(__name__)

SVID_WARN = timedelta(hours=24)
CELL_PAT_WARN = timedelta(hours=24)
PAT_WARN = timedelta(days=7)
PAT_IN_USE = timedelta(days=30)

_CLOUD_LABELS = {"aws": "AWS", "azure": "Azure"}
_GONE = {"deleted", "decommissioning", "revoked"}


def _until(when: datetime, now: datetime) -> str:
    left = when - now
    if left.total_seconds() < 3600:
        return f"{max(0, int(left.total_seconds() // 60))} min"
    if left < timedelta(hours=48):
        return f"{int(left.total_seconds() // 3600)} h"
    return f"{left.days} d"


# ── pure mappings ─────────────────────────────────────────────────────────────

def lease_check(row, *, now: datetime) -> dict:
    """One Workload Credentials lease. ``row`` has the ``WorkloadCredentialLease``
    columns, or is None when the lease has never been minted. Pure."""
    if row is None:
        return core.check("lease", "Lease", core.UNKNOWN, "Not issued yet.")
    failures = row.consecutive_failures or 0
    if failures and row.last_error:
        detail = (f"The last {failures} attempt(s) to issue it failed: "
                  f"{core.text(row.last_error, 250)}")
        if row.cooldown_until and now < row.cooldown_until:
            detail += f" Next attempt in {_until(row.cooldown_until, now)}."
        return core.check("lease", "Lease", core.FAIL, detail)
    if not row.payload or not row.expires_at:
        return core.check("lease", "Lease", core.UNKNOWN, "Not issued yet.")
    if row.expires_at <= now:
        return core.check("lease", "Lease", core.WARN,
                          f"Expired {core.ago(row.expires_at, now)} and not renewed yet.")
    return core.check("lease", "Lease", core.OK,
                      f"Issued {core.ago(row.issued_at, now)}; expires in "
                      f"{_until(row.expires_at, now)}." if row.issued_at else
                      f"Expires in {_until(row.expires_at, now)}.")


def token_sync_checks(status: Optional[dict]) -> list:
    """A cluster's ServiceAccount token registration, from
    ``ps_k8s_token_service.sync_status``. None means it could not be read. Pure."""
    if status is None:
        return [core.check("vault", "Password Safe", core.UNKNOWN,
                           "The sync state could not be read. See the dashboard log.")]
    if status.get("error"):
        # The service's own text, never the Password Safe response.
        return [core.check("vault", "Password Safe", core.UNKNOWN,
                           core.text(status["error"], 300))]
    out = [core.check(
        "vault", "Password Safe",
        core.OK if status.get("vault_has_real_credential") else core.FAIL,
        "The vault holds a token that was issued or rotated." if
        status.get("vault_has_real_credential") else
        "The vault holds the placeholder, not a working token. Repair the registration.")]
    if status.get("note"):
        out.append(core.check("pra", "PRA Vault mirror", core.WARN,
                              core.text(status["note"], 300)))
    elif status.get("linked"):
        last = status.get("subscriber_last_change") or ""
        out.append(core.check("pra", "PRA Vault mirror", core.OK,
                              "Linked" + (f"; last synced {last}." if last else ".")))
    else:
        out.append(core.check(
            "pra", "PRA Vault mirror", core.FAIL,
            "Not linked in Password Safe, so a rotation will not reach PRA."
            if status.get("parent_exists", True) else
            "The token account is gone from Password Safe."))
    return out


def spire_checks(lab, *, now: datetime) -> list:
    """The plumbing around one SPIRE lab, from its stored columns. Pure."""
    out = []
    ready = (lab.status or "").lower() == "available"
    if ready:
        exp = lab.admin_svid_expires_at
        if exp is None:
            out.append(core.check("svid", "Admin SVID", core.UNKNOWN, "No expiry recorded."))
        elif exp <= now:
            out.append(core.check("svid", "Admin SVID", core.FAIL,
                                  f"Expired {core.ago(exp, now)}."))
        else:
            out.append(core.check("svid", "Admin SVID",
                                  core.WARN if exp - now < SVID_WARN else core.OK,
                                  f"Expires in {_until(exp, now)}."))
        out.append(core.check("bundle", "Trust bundle",
                              core.OK if lab.trust_bundle_pem else core.WARN,
                              "Published." if lab.trust_bundle_pem else
                              "No trust bundle recorded, so relying parties cannot "
                              "verify this trust domain."))
    k8s = (lab.k8s_status or "").lower()
    if k8s == "failed":
        out.append(core.check("k8s", "k3s link", core.FAIL,
                              core.text(lab.k8s_error_message, 300) or "Linking failed."))
    elif k8s == "linked":
        out.append(core.check("k8s", "k3s link", core.OK, "Linked."))
    fed = (lab.federation_status or "").lower()
    if fed == "failed":
        out.append(core.check("federation", "Federation", core.FAIL,
                              core.text(lab.federation_error, 300) or "Federation failed."))
    elif fed == "federated":
        out.append(core.check("federation", "Federation", core.OK,
                              "Federated with the dashboard's SPIRE server."))
    if not ready:
        out.insert(0, stored_check(lab.status))
    return out


def agent_cell_checks(cell, *, now: datetime) -> list:
    """One agent cell: its state and its agent's token. Pure."""
    out = [stored_check(cell.status)]
    if cell.error_message:
        out.append(core.check("error", "Last error", core.FAIL,
                              core.text(cell.error_message, 300)))
    if cell.pat_revoked_at:
        out.append(core.check("pat", "Agent token", core.FAIL,
                              f"Revoked {core.ago(cell.pat_revoked_at, now)}; the agent "
                              f"cannot call the dashboard."))
    elif cell.pat_expires_at:
        exp = cell.pat_expires_at
        if exp <= now:
            out.append(core.check("pat", "Agent token", core.FAIL,
                                  f"Expired {core.ago(exp, now)}. Mint a fresh cell."))
        else:
            out.append(core.check("pat", "Agent token",
                                  core.WARN if exp - now < CELL_PAT_WARN else core.OK,
                                  f"Expires in {_until(exp, now)}."))
    return out


def pat_item(tokens: list, *, now: datetime) -> Optional[core.Item]:
    """Personal access tokens in use (used in the last 30 days), and which of them
    expire within a week. Names only: who owns a token is not reported. Pure over
    ``name``, ``expires_at``, ``last_used_at``, ``is_active``."""
    used = [t for t in tokens if t.is_active and t.last_used_at
            and now - t.last_used_at <= PAT_IN_USE
            and (t.expires_at is None or t.expires_at > now)]
    if not used:
        return None
    soon = sorted((t for t in used if t.expires_at and t.expires_at - now < PAT_WARN),
                  key=lambda t: t.expires_at)
    if soon:
        names = ", ".join(f"{core.text(t.name, 60)} ({_until(t.expires_at, now)})"
                          for t in soon[:10])
        check = core.check("expiry", "Expiry", core.WARN,
                           f"{len(soon)} token(s) in use expire within a week: {names}.")
    else:
        check = core.check("expiry", "Expiry", core.OK,
                           f"{len(used)} token(s) in use; none expire within a week.")
    return core.Item(object_id="pats", name="Integration tokens", kind="Access tokens",
                     href="/settings", checks=[check])


# ── collection ────────────────────────────────────────────────────────────────

async def _token_sync(db, cluster_id: str) -> Optional[dict]:
    from ... import ps_k8s_token_service
    try:
        return await ps_k8s_token_service.sync_status(db, cluster_id)
    except Exception as exc:  # noqa: BLE001 - one cluster must not stop the rest
        logger.warning("health: token sync status for cluster %s failed: %s",
                       cluster_id, exc)
        return None


async def collect(db) -> list:
    from ... import feature_flags, workload_credential_lease as wcl
    from ....database import (AgentCell, K8sCluster, PersonalAccessToken, SpireLab,
                              WorkloadCredentialLease)
    now = datetime.utcnow()
    items = []

    for cloud in wcl.CLOUDS:
        if not wcl.dynamic_enabled(cloud):
            continue
        for purpose in wcl.purposes_for(cloud):
            row = (db.query(WorkloadCredentialLease)
                   .filter(WorkloadCredentialLease.cloud == cloud,
                           WorkloadCredentialLease.purpose == purpose).first())
            items.append(core.Item(
                object_id=f"lease:{cloud}:{purpose}",
                name=f"{_CLOUD_LABELS.get(cloud, cloud)} {purpose} credential",
                kind="Workload Credentials lease", href="/settings",
                checks=[lease_check(row, now=now)]))

    if feature_flags.enabled("password_safe_enabled"):
        clusters = (db.query(K8sCluster).filter(K8sCluster.ps_token_account_id.isnot(None))
                    .order_by(K8sCluster.name).all())
        for c in clusters:
            items.append(core.Item(
                object_id=f"k8s-token:{c.id}", name=c.name,
                kind="ServiceAccount token", href="/k8s",
                checks=token_sync_checks(await _token_sync(db, c.id))))

    if feature_flags.enabled("spire_lab_enabled"):
        for lab in db.query(SpireLab).order_by(SpireLab.name).all():
            if (lab.status or "").lower() in _GONE:
                continue
            items.append(core.Item(object_id=f"spire:{lab.id}", name=lab.name,
                                   kind="SPIRE lab", href="/spire-lab",
                                   checks=spire_checks(lab, now=now)))

    if feature_flags.enabled("agentcell_enabled"):
        for cell in db.query(AgentCell).order_by(AgentCell.name).all():
            if (cell.status or "").lower() in _GONE:
                continue
            items.append(core.Item(object_id=f"agentcell:{cell.id}", name=cell.name,
                                   kind="Agent cell", href="/workload-lab",
                                   checks=agent_cell_checks(cell, now=now)))

    pats = pat_item(db.query(PersonalAccessToken)
                    .filter(PersonalAccessToken.is_active.is_(True)).all(), now=now)
    if pats:
        items.append(pats)
    return items
