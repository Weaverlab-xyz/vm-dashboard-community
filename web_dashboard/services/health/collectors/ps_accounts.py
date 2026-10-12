"""Password Safe: is every account the dashboard onboarded still there, and rotating?

One credential-free bulk read (``ps_api_service.get_managed_account_states``) across every
managed account the dashboard recorded an id for: database accounts, Kubernetes
ServiceAccount tokens, directory administrators, certificate-lab and SPIRE-lab accounts,
workload tokens. It reads LastChangeDate and NextChangeDate and never checks anything out.

A Password Safe that does not answer raises, and the runner records that against the
whole area: "Password Safe is down" must not read as "every account was deleted".
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .. import core

# A rotation a day late is a scheduler that has not got to it; a week late is a
# rotation that is not happening.
OVERDUE_WARN = timedelta(days=1)
OVERDUE_FAIL = timedelta(days=7)


def _accounts(db) -> list:
    """``[(object_id, name, kind, href, account_id)]`` for every onboarded account."""
    from ....database import (CertLab, CloudDatabase, K8sCluster, ManagedDirectory,
                              SpireLab, WorkloadK8sToken)
    out = []

    def add(rows, prefix, kind, href, name_of, *columns):
        for row in rows:
            if getattr(row, "status", "") in ("deleted", "decommissioning"):
                continue
            for column, what in columns:
                aid = str(getattr(row, column, "") or "").strip()
                if aid.isdigit():
                    label = name_of(row) + (f" ({what})" if what else "")
                    out.append((f"{prefix}:{row.id}:{column}", label, kind, href, aid))

    add(db.query(CloudDatabase).all(), "clouddb", "Database account", "/databases",
        lambda r: f"{r.engine} {r.instance_id or r.private_host or r.id[:8]}",
        ("ps_managed_account_id", ""))
    add(db.query(K8sCluster).all(), "k8s", "Kubernetes token", "/k8s",
        lambda r: r.name, ("ps_token_account_id", "ServiceAccount token"),
        ("ps_pra_vault_account_id", "PRA Vault mirror"))
    add(db.query(ManagedDirectory).all(), "directory", "Directory administrator",
        "/directories", lambda r: r.name, ("ps_account_id", ""))
    add(db.query(CertLab).all(), "certlab", "Certificate lab account", "/workload-lab",
        lambda r: r.name, ("ps_account_id", ""))
    add(db.query(SpireLab).all(), "spirelab", "SPIRE lab account", "/workload-lab",
        lambda r: r.name, ("ps_account_id", ""))
    add(db.query(WorkloadK8sToken).all(), "workloadk8s", "Workload token", "/workload-lab",
        lambda r: f"{r.namespace}/{r.service_account}", ("ps_account_id", ""))
    return out


def account_checks(state: dict, *, now: Optional[datetime] = None) -> list:
    """Checks for one account's state. ``{}`` means Password Safe has no such account.
    Pure."""
    now = now or datetime.utcnow()
    if not state:
        return [core.check("exists", "In Password Safe", core.FAIL,
                           "The managed account no longer exists in Password Safe, so "
                           "nothing is rotating this credential.")]
    last = core.parse_when(state.get("last_change_date"))
    due = core.parse_when(state.get("next_change_date"))
    if due and now - due > OVERDUE_FAIL:
        status = core.FAIL
        detail = f"Rotation was due {core.ago(due, now)} and has not happened."
    elif due and now - due > OVERDUE_WARN:
        status = core.WARN
        detail = f"Rotation was due {core.ago(due, now)}."
    elif not last:
        status = core.WARN
        detail = "Password Safe has never rotated this account."
    else:
        status = core.OK
        detail = f"Last rotated {core.ago(last, now)}" + (
            f"; next due {due:%Y-%m-%d}." if due else ".")
    return [core.check("rotation", "Rotation", status, detail)]


async def collect(db) -> list:
    from ... import ps_api_service
    accounts = _accounts(db)
    if not accounts:
        return []
    states = await ps_api_service.get_managed_account_states([a[4] for a in accounts])
    now = datetime.utcnow()
    return [core.Item(object_id=oid, name=name, kind=kind, href=href,
                      checks=account_checks(states.get(aid) or {}, now=now))
            for oid, name, kind, href, aid in accounts]
