"""Agent demo cell endpoints, gated behind ``agentcell_enabled`` at router-include time.

One non-human principal per row: a worker on a host this dashboard already deployed,
attested by a SPIRE trust domain, authorized by a Personal Access Token that expires and
can be revoked while somebody watches.

**This module issues an authorization and records it. It never stores one.** The raw PAT
is returned once, in the create response, exactly as ``api/tokens.create_token`` returns
it — the row keeps the id, the name and the expiry. ``services/agentcell_service`` argues
the rest of the design, including why identity and authorization stay two things.

The hashing is ``api/tokens``' own (`_generate_raw`, `hash_pat`) rather than a second
implementation: two ways of minting the same kind of token is two things to keep in step,
and the one that drifts is the one nobody is looking at. What this module cannot reuse is
``create_token`` itself, which mints for ``current_user`` — the whole point here is to
mint against a DIFFERENT, narrower user, which is also why the admin refusal exists.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import AgentCell, PersonalAccessToken, SpireLab, User, get_db
from ..models.agentcell import (
    AgentCellCreateRequest,
    AgentCellCreateResponse,
    AgentCellInfo,
    AgentCellLinkRequest,
    AgentCellLinkResponse,
    AgentCellListResponse,
)
from ..services import agentcell_service
from .auth import get_current_user, require_permission
from .tokens import _generate_raw, hash_pat

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agentcell", tags=["agentcell"])


def _iso(value) -> str:
    return value.isoformat() if value else ""


@router.post("/agent", response_model=AgentCellCreateResponse)
def create_agent(
    payload: AgentCellCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Mint an agent's authorization and record what was issued.

    Permission is ``config_mgmt:write`` rather than a scope of this cell's own: what this
    ultimately does is run two playbooks against a host, which is the config-management
    capability wearing a different form.
    """
    from ..services import feature_flags, spire_lab_service

    for problem in (agentcell_service.host_problem(payload.host_ref),
                    agentcell_service.mcp_problem(
                        feature_flags.enabled("mcp_server_enabled")),
                    agentcell_service.pat_expiry_problem(payload.pat_hours)):
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    lab = db.query(SpireLab).filter(SpireLab.id == payload.spire_lab_id).first()
    if not lab:
        raise HTTPException(status_code=404, detail="No such SPIRE lab.")
    problem = agentcell_service.trust_domain_problem(lab.trust_domain)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    # The host is re-derived, never trusted. A name that looks fine on the request and
    # resolves to nothing here is the case this call exists for.
    try:
        host = spire_lab_service.resolve_host(db, payload.cloud, payload.host_ref)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=(f"{payload.host_ref!r} is not a VM this dashboard deployed ({exc}). "
                    "The worker attaches to a host from this dashboard's own deploy "
                    "records — it does not create one, and it will not run playbooks "
                    "against an address it was simply handed."))

    agent_user = db.query(User).filter(User.id == payload.pat_user_id).first()
    if not agent_user:
        raise HTTPException(status_code=404, detail="No such user to mint the token against.")
    problem = agentcell_service.pat_user_problem(
        bool(getattr(agent_user, "is_effective_admin", False)), agent_user.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    raw = _generate_raw()
    expires_at = agentcell_service.pat_expires_at(payload.pat_hours)
    pat = PersonalAccessToken(
        user_id=agent_user.id,
        name=agentcell_service.pat_name_for(payload.name),
        token_hash=hash_pat(raw),
        expires_at=expires_at,
    )
    db.add(pat)
    db.flush()

    row = AgentCell(
        name=payload.name,
        status="provisioning",
        created_by=current_user.username,
        workgroup=payload.workgroup or None,
        host_deploy_job_id=host.get("deploy_job_id"),
        host_name=host.get("name"),
        cloud=payload.cloud,
        private_ip=host.get("private_ip"),
        public_ip=host.get("public_ip"),
        spire_lab_id=lab.id,
        trust_domain=lab.trust_domain,
        spiffe_id=agentcell_service.spiffe_id_for(lab.trust_domain),
        pat_id=pat.id,
        pat_name=pat.name,
        pat_user_id=agent_user.id,
        pat_expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info("agent cell %s created for host %s as %s (token %s, expires %s)",
                row.id, row.host_name, row.spiffe_id, pat.name, expires_at)
    return AgentCellCreateResponse(
        id=row.id,
        name=row.name,
        spiffe_id=row.spiffe_id or "",
        pat_name=row.pat_name or "",
        pat_expires_at=_iso(row.pat_expires_at),
        # Once. Carry it into agent-install.yml; nothing can recover it afterwards.
        token=raw,
        message=(f"Agent {row.name} recorded. Run the two playbooks in "
                 "examples/playbooks/agent/ against the host to install it."),
        notes=agentcell_service.deploy_notes(payload.pat_hours),
    )


@router.get("/agents", response_model=AgentCellListResponse)
def list_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every agent cell this caller may see, newest first."""
    from .gcp import _accessible_workgroups

    accessible = _accessible_workgroups(current_user)
    rows = db.query(AgentCell).order_by(AgentCell.created_at.desc()).all()
    agents = []
    for row in rows:
        if accessible is not None and (row.workgroup or "").lower() not in accessible:
            continue
        agents.append(AgentCellInfo(
            id=row.id,
            name=row.name or "",
            status=row.status or "",
            created_by=row.created_by or "",
            created_at=_iso(row.created_at),
            workgroup=row.workgroup or "",
            host_name=row.host_name or "",
            cloud=row.cloud or "",
            private_ip=row.private_ip or "",
            trust_domain=row.trust_domain or "",
            spiffe_id=row.spiffe_id or "",
            pat_name=row.pat_name or "",
            pat_expires_at=_iso(row.pat_expires_at),
            pat_revoked_at=_iso(row.pat_revoked_at),
            wired=agentcell_service.is_wired(row),
            stages_done=agentcell_service.stages_done(row),
            linked_mechanism=row.linked_mechanism or "",
            linked_credential_id=row.linked_credential_id or "",
            linked_summary=agentcell_service.link_summary(
                row.linked_mechanism or "", _lease_state(db, row)),
        ))
    return AgentCellListResponse(agents=agents)


def _lease_state(db: Session, row) -> str:
    """The linked credential's lease state, or "" when there is no link.

    Read through ``workload_cloud_service.lease_state`` rather than computed here, so
    "expired" keeps meaning what that function says it means: **the mechanism working**,
    not a fault. A second implementation would be a second opinion about that.
    """
    if (row.linked_mechanism or "") != "cloud" or not row.linked_credential_id:
        return ""
    try:
        from ..services import workload_cloud_service as wcs
        wl_row = wcs.get_row(db, row.linked_credential_id)
        return wcs.lease_state(wl_row) if wl_row else ""
    except Exception as exc:  # noqa: BLE001
        logger.info("agentcell: could not read the linked lease state (%s)", exc)
        return ""


@router.post("/agent/{agent_id}/link", response_model=AgentCellLinkResponse)
def link_agent(
    agent_id: str,
    payload: AgentCellLinkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Make an agent answerable for one Workload Lab credential.

    **This gives the worker nothing.** None of the lab's credentials can reach it — the
    Cloud tab's is returned to nobody and the other two are vaulted behind a Password
    Safe client — so what this records is accountability, not capability. The response
    leads with that rather than burying it, because a governance record that reads as a
    capability is the failure mode here.
    """
    row = db.query(AgentCell).filter(AgentCell.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No such agent cell.")

    for problem in (agentcell_service.link_problem(payload.mechanism),
                    agentcell_service.already_linked_problem(row)):
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    from ..services import workload_cloud_service as wcs
    wl_row = wcs.get_row(db, payload.credential_id)
    if not wl_row:
        raise HTTPException(status_code=404,
                            detail="No such Workload Lab cloud credential.")

    row.linked_mechanism = payload.mechanism.strip().lower()
    row.linked_credential_id = wl_row.id
    row.linked_at = datetime.utcnow()
    db.commit()
    db.refresh(row)

    logger.info("agent cell %s linked to %s credential %s by %s",
                row.id, row.linked_mechanism, wl_row.id, current_user.username)
    return AgentCellLinkResponse(
        id=row.id,
        mechanism=row.linked_mechanism,
        credential_id=row.linked_credential_id,
        summary=agentcell_service.link_summary(
            row.linked_mechanism, wcs.lease_state(wl_row)),
        notes=agentcell_service.link_notes(wl_row.cloud or "",
                                           wcs.revocable(wl_row.cloud or "")),
    )


@router.delete("/agent/{agent_id}/link")
def unlink_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Drop the link. Touches neither the agent nor the credential — the lab's row keeps
    its own lifecycle, and this only stops claiming a relationship between them."""
    row = db.query(AgentCell).filter(AgentCell.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No such agent cell.")
    was = row.linked_mechanism or ""
    row.linked_mechanism = None
    row.linked_credential_id = None
    row.linked_at = None
    db.commit()
    logger.info("agent cell %s unlinked from %s by %s", row.id, was or "nothing",
                current_user.username)
    return {"id": row.id, "unlinked": was,
            "message": (f"No longer answerable for its {was} credential."
                        if was else "Nothing was linked.")}


@router.delete("/agent/{agent_id}")
def revoke_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Revoke the agent's authorization and mark the row.

    **This revokes; it does not uninstall.** The worker keeps running and keeps
    attesting — its identity was never in question — and its very next poll is refused.
    That is the demo, and hiding it behind a teardown would remove the thing worth
    watching. The host is an ordinary VM, so destroying it reaps the worker whenever the
    operator chooses.
    """
    row = db.query(AgentCell).filter(AgentCell.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No such agent cell.")

    revoked = False
    if row.pat_id:
        pat = db.query(PersonalAccessToken).filter(
            PersonalAccessToken.id == row.pat_id).first()
        if pat and pat.is_active:
            pat.is_active = False
            revoked = True
    row.pat_revoked_at = datetime.utcnow()
    row.status = "revoked"
    db.commit()

    logger.info("agent cell %s revoked by %s (token cleared: %s)",
                row.id, current_user.username, revoked)
    return {
        "id": row.id,
        "revoked": revoked,
        "message": (f"{row.name}'s token is revoked. The worker is still attested as "
                    f"{row.spiffe_id or 'its SPIFFE ID'} — its next poll will be refused, "
                    "and the unit will stop. Watch `journalctl -u mcp-agent -f`."),
    }
