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

**THERE IS DELIBERATELY NO CLOUD-MINT ROUTE HERE, and a reader will look for one next to
``/k8s-request``.** An agent linked to a ``cloud`` credential mints it itself, on the
host, with ``mcp_agent.py --cloud-episode``. Adding a route that called
``workload_credentials_service.generate`` on the worker's behalf would be easy, would
demo faster, and would destroy the property the whole mechanism exists for: the issuance
would land in Workload Credentials' audit log as *this dashboard* rather than as the
workload — the exact thing ``workload_cloud_service._run_issue``'s docstring says the
design removes. It would also spend money on a button. So the dashboard records the
link, reports the lease state, and mints nothing.
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
    AgentCellEpisodeRequest,
    AgentCellEpisodeResponse,
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
    """Every agent cell this caller may see, newest first.

    Visibility is ``agentcell_service.visible_to`` — workgroup OR creator — which the
    home page's `agent_cells` tile also reads, so the count and the list it links to
    cannot disagree.
    """
    from .gcp import _accessible_workgroups

    accessible = _accessible_workgroups(current_user)
    rows = db.query(AgentCell).order_by(AgentCell.created_at.desc()).all()
    agents = []
    for row in rows:
        if not agentcell_service.visible_to(row, accessible, current_user.username):
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
            episode_state=row.episode_state or "",
            episode_summary=agentcell_service.episode_summary(row),
            episode_started_at=_iso(row.episode_started_at),
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


@router.get("/options")
def build_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """What the Agent tab's forms need, and an honest list of what is not ready yet.

    **``config_mgmt:write`` rather than a read scope, because of ``users``.** Every
    other field here is derivable from routes the caller can already reach; the
    candidate-user list is not, since ``/api/users`` is admin-only. Gating it on the
    permission that can actually mint an agent keeps the disclosure to callers who could
    obtain the same names by minting, and the tab degrades to read-only when it 403s.

    **Administrators are omitted rather than listed-and-disabled.** ``pat_user_problem``
    refuses them anyway, so showing them would add nothing but a roster of which
    accounts hold admin, handed to somebody who by definition does not.

    ``missing`` is the point of the route, as it is on the other tabs: every item on it
    is a failure that would otherwise surface as a 400 on submit, or — worse — as a
    worker that installs cleanly and then 404s every poll in front of an audience.
    """
    from ..config import settings
    from ..services import (cert_lab_service as cls, config_service, feature_flags,
                            spire_lab_service, workgroup_service,
                            workload_cloud_service as wcs,
                            workload_k8s_service as wks)
    from .cert_lab import _visible as _ca_visible
    from .spire_lab import _visible as _lab_visible

    missing = []

    mcp_on = feature_flags.enabled("mcp_server_enabled")
    problem = agentcell_service.mcp_problem(mcp_on)
    if problem:
        missing.append(problem)

    # The labs, filtered exactly as the SPIRE tab filters them — a lab this caller
    # cannot see on that tab must not become selectable here.
    labs = []
    for lab in db.query(SpireLab).order_by(SpireLab.created_at.desc()).all():
        if lab.status == "deleted" or not _lab_visible(lab, current_user):
            continue
        labs.append({
            "id": lab.id,
            "name": lab.name or "",
            "trust_domain": lab.trust_domain or "",
            "cloud": lab.cloud or "",
            "status": lab.status or "",
            "vm_name": lab.vm_name or "",
            "private_ip": lab.private_ip or "",
            "public_ip": lab.public_ip or "",
            # What the worker would attest as if this lab is chosen. Resolved here so the
            # form shows the same string the row will hold, rather than rebuilding
            # `spiffe://` + path in JavaScript where the two could drift.
            "spiffe_id": agentcell_service.spiffe_id_for(lab.trust_domain or ""),
        })
    if not labs:
        spire_on = feature_flags.enabled("spire_lab_enabled")
        missing.append(
            "no SPIRE trust domain — the worker would have nothing to attest to, so the "
            "identity half of the demo would be an assertion. "
            + ("Stand one up on the SPIRE tab first."
               if spire_on else
               "Turn on the SPIRE Lab preview under Settings → Features and stand one "
               "up, then come back."))

    if not feature_flags.enabled("ansible_enabled",
                                 config_service.get_bool("ansible_enabled", True)):
        missing.append(
            "ansible_enabled is off — minting still works, but the two playbooks that "
            "put the worker on the host are Ansible runs you make yourself, so there "
            "would be nothing to run them with.")

    # Candidate token users. Non-admin and active only; see the docstring.
    users = []
    for row in db.query(User).order_by(User.username.asc()).all():
        if not bool(getattr(row, "is_active", True)):
            continue
        if bool(getattr(row, "is_effective_admin", False)):
            continue
        users.append({"id": row.id, "username": row.username or "",
                      "full_name": getattr(row, "full_name", "") or ""})
    if not users:
        missing.append(
            "every active user on this instance is an administrator, and the cell "
            "refuses to mint against one — the token user IS the agent's blast radius. "
            "Create a narrow user for the agent first.")

    # ── What the agent can be made answerable for ────────────────────────────
    # The OTHER TABS' rows, which is what makes this tab a consumer rather than a fifth
    # lab. `linkable` is resolved here because the refusals live server-side: an
    # unrotated Kubernetes token holds a placeholder, and linking to it would promise
    # the agent something it cannot be given.
    credentials: dict = {m: [] for m in agentcell_service.LINKABLE_MECHANISMS}
    if wcs.enabled():
        for row in wcs.list_rows(db):
            # An identity mid-teardown will not mint again -- `start_decommission` says
            # that is precisely what retiring guarantees -- so a link to one promises a
            # credential the agent cannot get. Same shape as the unrotated-token and
            # unbuilt-CA refusals below and beside.
            retiring = (row.status or "") in ("retiring", "deleted")
            credentials["cloud"].append({
                "id": row.id, "name": row.name or "", "cloud": row.cloud or "",
                "detail": (row.dynamic_name or ""),
                "state": wcs.lease_state(row),
                "linkable": not retiring,
                "why": ("" if not retiring else
                        "This identity is being retired, which stops it minting "
                        "another credential. Register a new one on the Cloud tab."),
            })
    else:
        missing.append(
            "Workload Credentials is off, so the Cloud tab's identities cannot be "
            "listed here — and that link is the fourth demo, in which the agent mints "
            "its own short-lived cloud credential. Without it, that demo is "
            "unavailable; the other three are unaffected.")
    if wks.enabled():
        for row in wks.list_rows(db):
            rotated = bool(getattr(row, "rotated", False))
            credentials["kubernetes"].append({
                "id": row.id, "name": row.name or "",
                "cloud": row.cloud or "",
                "detail": " · ".join(x for x in (row.cluster_name or "",
                                                 row.ps_account_name or "") if x),
                "state": "ready" if rotated else "never rotated",
                "linkable": rotated,
                "why": ("" if rotated else
                        "Password Safe still holds the placeholder this token was "
                        "created with rather than a credential. Rotate it on the "
                        "Kubernetes tab first."),
            })
    else:
        missing.append(
            "the Kubernetes tab is unavailable (it needs Kubernetes management and "
            "Password Safe), so there is no token for an agent to request. That is the "
            "second demo — an agent that cannot authorise its own access — and without "
            "it only the revoke beat is available.")
    # The Certificate tab's CAs. Its own flag and its own visibility rule, both borrowed
    # rather than restated: a CA a caller cannot see on that tab must not become
    # selectable here, and `api/cert_lab._visible` is CREATOR-scoped for a non-admin
    # where the SPIRE and agent lists are workgroup-or-creator. Reimplementing the looser
    # one here would disclose rows that tab does not.
    if feature_flags.enabled("cert_lab_enabled", settings.cert_lab_enabled):
        for row in cls.list_labs(db):
            if (row.status or "") == "deleted" or not _ca_visible(row, current_user):
                continue
            available = (row.status or "") == "available"
            credentials["certificates"].append({
                "id": row.id, "name": row.name or "",
                "cloud": row.cloud or "",
                # "onboarded" only when the dashboard registered an identity against this
                # CA. Its ABSENCE is not rendered, because the row tracks the identity IT
                # created and an operator may have onboarded others by hand — saying "no
                # identity" would be a claim this row cannot make.
                "detail": " · ".join(x for x in (
                    row.project or "", row.location or "",
                    "onboarded" if row.ps_account_id else "") if x),
                "state": "ready" if available else (row.status or "unknown"),
                # The same shape as the unrotated Kubernetes token above, and the same
                # reasoning: the refusal lives server-side (see `link_agent`), so the
                # picker greys the row out rather than letting the operator discover it
                # as a 400. An identity against a CA that is not built yet produces a
                # managed system that fails every rotation, so there would be nothing
                # for the agent to request.
                "linkable": available,
                "why": ("" if available else
                        f"{row.name} is {row.status or 'unknown'}, not available. An "
                        "identity onboarded against a CA that is not built yet fails "
                        "every rotation, so the agent would have nothing to request. "
                        "Finish the build on the Certificates tab first."),
            })
    else:
        missing.append(
            "the Certificate Lab is off, so there is no certificate identity for an "
            "agent to request. That is the third demo — the credential nobody can take "
            "away — and it is the one that makes the other two worth having. Turn on "
            "the Certificate Lab preview under Settings → Features.")

    return {
        "clouds": list(spire_lab_service.PROVISIONING_CLOUDS),
        "hosts": spire_lab_service.deployed_hosts(db),
        "labs": labs,
        "users": users,
        # Through the same service /api/groups/workgroups reads, so the picker here and
        # the one on every other page cannot offer different names — but NARROWED to the
        # caller's own for a non-admin, which the global endpoint is not. Tagging a row
        # into a workgroup you are not in is a grant to a group you are not part of, and
        # it is almost always a misclick rather than an intent. `list_agents`' creator
        # term means such a row is still visible to whoever minted it, so this is about
        # not quietly handing an agent away rather than about not losing it.
        "workgroups": (workgroup_service.list_names(db)
                       if bool(getattr(current_user, "is_admin", False))
                       else sorted(current_user.workgroups_list or [])),
        "pat_hours": {"default": agentcell_service.DEFAULT_PAT_HOURS,
                      "max": agentcell_service.MAX_PAT_HOURS},
        "spiffe_path": agentcell_service.AGENT_SPIFFE_PATH,
        "credentials": credentials,
        # Which links are a CAPABILITY. The page must not render them alike: a
        # governance record that reads as a capability is this feature's headline
        # failure mode, and the reverse is the Kubernetes one.
        "spendable": list(agentcell_service.SPENDABLE_MECHANISMS),
        # And which of THOSE this dashboard can open an episode for, which is a
        # different question and the one the page's Request-access button belongs to.
        # `certificates` is spendable with no route here — its episode is one shot on
        # the host — so gating that button on `spendable` posts a certificate link at
        # the Kubernetes route, which misses and refuses it as a token that vanished.
        "episode_mechanisms": list(agentcell_service.EPISODE_MECHANISMS),
        "episode": {"default_minutes": agentcell_service.DEFAULT_DURATION_MINUTES,
                    "max_minutes": agentcell_service.MAX_WAIT_MINUTES * 2},
        "mcp_enabled": mcp_on,
        "missing": missing,
    }


@router.post("/agent/{agent_id}/link", response_model=AgentCellLinkResponse)
def link_agent(
    agent_id: str,
    payload: AgentCellLinkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Make an agent answerable for one Workload Lab credential.

    **All three confer a capability now, and they differ in the failure mode the notes
    have to pre-empt** — so the response leads with a different sentence per tab rather
    than letting the operator assume:

      * ``cloud`` is the one that **mints**, and the two things nobody thinks to ask are
        that it BILLS and that this dashboard does not decide what the credential may
        do. It reaches Workload Credentials directly — no vault, no approval — and like
        ``certificates`` it has **no episode route here**: the run is
        ``mcp_agent.py --cloud-episode`` on the host.
      * ``kubernetes`` is a **capability**: the worker reaches Password Safe holding
        nothing, so it can request that token. The notes lead with what the approval
        does and does not gate, because assuming it gates use is the failure mode here.
      * ``certificates`` is a capability too, and the one whose limit is easiest to
        assume away: the notes lead with what taking it away **cannot** do, because
        nothing on that path checks a CRL or OCSP. It also has **no episode route
        here** — that episode is one shot on the host — so the page must not offer a
        Request-access button for it (see ``EPISODE_MECHANISMS``).
    """
    row = db.query(AgentCell).filter(AgentCell.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No such agent cell.")

    for problem in (agentcell_service.link_problem(payload.mechanism),
                    agentcell_service.already_linked_problem(row)):
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    mechanism = payload.mechanism.strip().lower()
    if mechanism == "kubernetes":
        from ..services import workload_k8s_service as wks
        wl_row = wks.get_row(db, payload.credential_id)
        if not wl_row:
            raise HTTPException(
                status_code=404, detail="No such Workload Lab Kubernetes token.")
        # A token whose first rotation has not completed holds a placeholder, not a
        # credential -- `WorkloadK8sToken.rotated`'s own comment says that state is
        # "indistinguishable from success on the page". Linking to it would promise the
        # agent something it cannot be given.
        if not getattr(wl_row, "rotated", False):
            raise HTTPException(
                status_code=400,
                detail="That token has never completed a rotation, so Password Safe "
                       "still holds the placeholder it was created with rather than a "
                       "credential. Rotate it first.")
        summary = agentcell_service.link_summary(mechanism, "live")
        notes = agentcell_service.k8s_link_notes(
            wl_row.profile or "", wl_row.ps_account_name or "", wl_row.namespace or "")
    elif mechanism == "certificates":
        from ..services import cert_lab_service as cls
        wl_row = cls.get_lab(db, payload.credential_id)
        if not wl_row:
            raise HTTPException(
                status_code=404, detail="No such certificate authority.")
        # An identity onboarded against a CA that is not built yet produces a managed
        # system that fails every rotation -- cert_lab_service.start_ps_register refuses
        # for exactly this reason, and linking to one would promise the agent a
        # certificate that cannot be issued.
        if (wl_row.status or "") != "available":
            raise HTTPException(
                status_code=400,
                detail=f"{wl_row.name} is {wl_row.status or 'unknown'}, not available. "
                       "An identity against a CA that is not built yet fails every "
                       "rotation, so there would be nothing for the agent to request.")
        summary = agentcell_service.link_summary(mechanism, "live")
        notes = agentcell_service.cert_link_notes(
            payload.account_name or "", payload.bundle_title or "",
            payload.expect_cn or "")
    else:
        from ..services import workload_cloud_service as wcs
        wl_row = wcs.get_row(db, payload.credential_id)
        if not wl_row:
            raise HTTPException(status_code=404,
                                detail="No such Workload Lab cloud credential.")
        # The third refusal of the same shape as its two siblings above: an identity
        # that cannot produce a credential must not be linked to an agent that will be
        # told it can mint one. `start_decommission` is explicit that what retiring
        # guarantees is exactly this -- "this identity will not mint another credential".
        if (wl_row.status or "") in ("retiring", "deleted"):
            raise HTTPException(
                status_code=400,
                detail=f"{wl_row.name} is being retired, so it will not mint another "
                       "credential — the agent would have nothing to ask for. Register "
                       "a new identity on the Cloud tab.")
        summary = agentcell_service.link_summary(mechanism, wcs.lease_state(wl_row))
        notes = agentcell_service.link_notes(
            wl_row.cloud or "", wcs.revocable(wl_row.cloud or ""),
            dynamic_name=wl_row.dynamic_name or "",
            folder=wl_row.dynamic_folder or "")
        # There is no button for this episode, so the link hands over the command. The
        # two payload fields are identifiers and neither is stored — the same rule the
        # certificate link's three fields follow.
        notes.append(
            "There is no Request button for this one — it is a run on the host: "
            "`" + agentcell_service.cloud_episode_command(
                wl_row.cloud or "", wl_row.dynamic_name or "",
                wl_row.dynamic_folder or "", payload.cloud_scope or "",
                payload.cloud_deny_probe or "") + "`. `journalctl -u mcp-agent` is the "
            "record; this dashboard neither opens nor watches it.")

    row.linked_mechanism = mechanism
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
        summary=summary,
        notes=notes,
    )


@router.post("/agent/{agent_id}/k8s-request",
             response_model=AgentCellEpisodeResponse)
def request_cluster_access(
    agent_id: str,
    payload: AgentCellEpisodeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Open one bounded cluster-access episode for a linked agent.

    **This opens a request; it does not hand anything over.** Password Safe decides
    whether to release, and where the access policy requires approval it holds the
    request for a person — which is the beat this exists for. The response says which
    state the request landed in and never carries a credential.

    The dashboard records that an episode is open. The **worker** is what waits on it,
    retrieves when released, probes the cluster and checks back in — this endpoint does
    not fetch on the worker's behalf, because a credential that came through here would
    have travelled through a process the agent does not control.

    **The row does not track the worker's progress**, and it is worth being plain about
    why rather than letting the field look live: the worker cannot call back. Its PAT
    belongs to a non-admin user, and this route needs ``config_mgmt:write`` — giving a
    non-human principal that so it could file status updates would hand it more authority
    than the cell argues for. ``journalctl -u mcp-agent`` is where the episode happens.
    """
    row = db.query(AgentCell).filter(AgentCell.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No such agent cell.")

    for problem in (agentcell_service.episode_link_problem(row),
                    agentcell_service.cluster_episode_mechanism_problem(row),
                    agentcell_service.episode_problem(row)):
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    from ..services import workload_k8s_service as wks
    wl_row = wks.get_row(db, row.linked_credential_id or "")
    if not wl_row:
        raise HTTPException(
            status_code=404,
            detail="The linked Kubernetes token no longer exists. Unlink and relink.")

    minutes = agentcell_service.episode_duration_problem(payload.duration_minutes)
    row.episode_state = "requested"
    row.episode_started_at = datetime.utcnow()
    row.episode_released_at = None
    db.commit()
    db.refresh(row)

    logger.info("agent cell %s opened a cluster-access episode on %s (%s min) by %s",
                row.id, wl_row.id, minutes, current_user.username)
    return AgentCellEpisodeResponse(
        id=row.id,
        state=row.episode_state,
        summary=agentcell_service.episode_summary(row),
        notes=[
            f"The worker will ask Password Safe for `{wl_row.ps_account_name or ''}` "
            f"with a {minutes}-minute window and the reason "
            f"`{agentcell_service.episode_reason(row.name or '', row.spiffe_id or '')}`.",
            "**If the access policy requires approval, the agent waits** — and says so "
            "on every poll. It cannot approve its own request; that is the point.",
            "**The approval gates retrieval, not use.** A token already released lives "
            "out its TTL whatever happens next — rotation does not revoke it, and only "
            "deleting the ServiceAccount does.",
        ],
    )


@router.delete("/agent/{agent_id}/k8s-request")
def release_cluster_access(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("config_mgmt", "write")),
):
    """Close an open episode from this side.

    **Releasing the record is not releasing the token.** If Password Safe already
    released one, it lives out its TTL — this marks the episode closed and frees the
    agent to open another. Say so rather than letting "release" read as a revoke.
    """
    row = db.query(AgentCell).filter(AgentCell.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No such agent cell.")
    was = (row.episode_state or "").strip()
    if not was or was in agentcell_service.EPISODE_CLOSED:
        return {"id": row.id, "released": "", "message": "No episode was open."}
    row.episode_state = "released"
    row.episode_released_at = datetime.utcnow()
    db.commit()
    logger.info("agent cell %s cluster-access episode closed from %s by %s",
                row.id, was, current_user.username)
    return {
        "id": row.id,
        "released": was,
        "message": ("Episode closed. If a token was already released it lives out its "
                    "TTL — this frees the request slot, it does not revoke anything."),
    }


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
    # An open episode is refused rather than silently orphaned: unlinking mid-wait would
    # leave a request holding the account's concurrent slot for an identity this agent is
    # no longer answerable for, and nothing would be left pointing at it.
    problem = agentcell_service.episode_problem(row)
    if problem:
        raise HTTPException(
            status_code=400,
            detail=problem + " Unlinking now would orphan it against an account this "
                             "agent would no longer be answerable for.")
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
