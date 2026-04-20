"""
FIDO2 / WebAuthn MFA endpoints.

Registration:
  POST /api/mfa/register/begin    — generate creation options
  POST /api/mfa/register/complete — verify attestation, store credential

Credential management (authenticated):
  GET    /api/mfa/credentials          — list registered devices
  DELETE /api/mfa/credentials/{id}     — remove a device
"""
import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db, Fido2Credential
from ..api.auth import get_current_user
from ..models.user import (
    Fido2RegisterBeginResponse,
    Fido2RegisterCompleteRequest,
    Fido2CredentialResponse,
)
from ..services.fido2_service import (
    fido2_server,
    store_fido2_challenge,
    fetch_fido2_challenge,
    creation_options_to_dict,
)

router = APIRouter(prefix="/api/mfa", tags=["mfa"])


# ---------------------------------------------------------------------------
# Registration — begin
# ---------------------------------------------------------------------------

@router.post("/register/begin", response_model=Fido2RegisterBeginResponse)
async def register_begin(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate WebAuthn creation options and store challenge in memory."""
    from fido2.webauthn import PublicKeyCredentialUserEntity, AttestedCredentialData

    # Reconstruct existing credentials so the authenticator excludes duplicates
    existing = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == current_user.id, Fido2Credential.is_active == True)
        .all()
    )
    exclude_credentials = [AttestedCredentialData(bytes(c.public_key)) for c in existing]

    user_entity = PublicKeyCredentialUserEntity(
        id=current_user.id.encode(),
        name=current_user.username,
        display_name=current_user.full_name or current_user.username,
    )

    options, state = fido2_server.register_begin(
        user=user_entity,
        credentials=exclude_credentials,
        user_verification="preferred",
    )

    # state is a plain str-keyed dict (challenge is base64url string, user_verification is str)
    challenge_token = store_fido2_challenge({"state": state, "user_id": current_user.id})

    return Fido2RegisterBeginResponse(
        challenge_token=challenge_token,
        options=creation_options_to_dict(options),
    )


# ---------------------------------------------------------------------------
# Registration — complete
# ---------------------------------------------------------------------------

@router.post("/register/complete", response_model=Fido2CredentialResponse)
async def register_complete(
    body: Fido2RegisterCompleteRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify attestation and store the new FIDO2 credential."""
    stored = fetch_fido2_challenge(body.challenge_token)
    if not stored or stored.get("user_id") != current_user.id:
        raise HTTPException(status_code=400, detail="Invalid or expired challenge token")

    try:
        # register_complete accepts a Mapping[str, Any] (our dict from the browser)
        auth_data = fido2_server.register_complete(
            state=stored["state"],
            response=body.attestation_response,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"FIDO2 registration failed: {exc}")

    cred_data = auth_data.credential_data
    if cred_data is None:
        raise HTTPException(status_code=400, detail="No credential data in attestation")

    # Store the full AttestedCredentialData bytes — reconstruct with AttestedCredentialData(bytes)
    credential_id_bytes = bytes(cred_data.credential_id)
    credential_data_bytes = bytes(cred_data)  # full AttestedCredentialData (includes pubkey)
    aaguid_str = str(cred_data.aaguid) if cred_data.aaguid else None
    initial_sign_count = auth_data.counter

    credential = Fido2Credential(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        credential_id=credential_id_bytes,
        public_key=credential_data_bytes,   # stores full AttestedCredentialData bytes
        sign_count=initial_sign_count,
        aaguid=aaguid_str,
        device_name=body.device_name or "Security Key",
        created_at=datetime.utcnow(),
    )
    db.add(credential)

    # Enforce MFA for this user going forward
    current_user.mfa_required = True
    db.commit()
    db.refresh(credential)

    return Fido2CredentialResponse(
        id=credential.id,
        device_name=credential.device_name,
        aaguid=credential.aaguid,
        created_at=credential.created_at,
        last_used_at=credential.last_used_at,
        is_active=credential.is_active,
    )


# ---------------------------------------------------------------------------
# Credential list
# ---------------------------------------------------------------------------

@router.get("/credentials", response_model=List[Fido2CredentialResponse])
async def list_credentials(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    creds = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == current_user.id, Fido2Credential.is_active == True)
        .order_by(Fido2Credential.created_at.desc())
        .all()
    )
    return [
        Fido2CredentialResponse(
            id=c.id,
            device_name=c.device_name,
            aaguid=c.aaguid,
            created_at=c.created_at,
            last_used_at=c.last_used_at,
            is_active=c.is_active,
        )
        for c in creds
    ]


# ---------------------------------------------------------------------------
# Delete credential
# ---------------------------------------------------------------------------

@router.delete("/credentials/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(Fido2Credential)
        .filter(
            Fido2Credential.id == credential_id,
            Fido2Credential.user_id == current_user.id,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    # Guard: don't let user delete their last active key (would lock them out)
    active_count = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == current_user.id, Fido2Credential.is_active == True)
        .count()
    )
    if active_count <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove your only security key — register a replacement first.",
        )

    cred.is_active = False
    db.commit()

    # Clear MFA requirement if somehow all credentials are gone
    remaining = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == current_user.id, Fido2Credential.is_active == True)
        .count()
    )
    if remaining == 0:
        current_user.mfa_required = False
        db.commit()
