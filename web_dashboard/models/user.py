"""User-related Pydantic schemas"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class UserLogin(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    workgroups: List[str] = []


class UserResponse(BaseModel):
    id: str
    username: str
    full_name: Optional[str]
    email: Optional[str] = None
    workgroups: List[str]
    is_active: bool
    is_admin: bool = False
    auth_provider: str = "local"
    mfa_required: bool = False
    permissions: Optional[dict] = None

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    workgroups: List[str]


class TokenData(BaseModel):
    username: Optional[str] = None


# ── MFA / FIDO2 schemas ─────────────────────────────────────────────────────

class PreAuthResponse(BaseModel):
    """Returned by /api/auth/login when the user has FIDO2 keys registered."""
    pre_auth_token: str
    mfa_required: bool = True


class MfaLoginRequest(BaseModel):
    """Body for POST /api/auth/login/mfa"""
    pre_auth_token: str
    assertion_response: dict  # raw WebAuthn PublicKeyCredential JSON from the browser


class Fido2RegisterBeginResponse(BaseModel):
    challenge_token: str
    options: dict  # PublicKeyCredentialCreationOptions (JSON-serialisable)


class Fido2AuthBeginResponse(BaseModel):
    challenge_token: str
    options: dict  # PublicKeyCredentialRequestOptions (JSON-serialisable)


class Fido2RegisterCompleteRequest(BaseModel):
    challenge_token: str
    device_name: str
    attestation_response: dict  # raw WebAuthn credential JSON from the browser


class Fido2CredentialResponse(BaseModel):
    id: str
    device_name: Optional[str]
    aaguid: Optional[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    is_active: bool

    class Config:
        from_attributes = True
