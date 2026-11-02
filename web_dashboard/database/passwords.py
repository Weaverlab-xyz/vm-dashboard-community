"""Password hashing for the database layer: bcrypt, with the 72-byte rule in one place.

Pure functions, no engine and no models, so this module imports nothing from the rest of
the layer. ``web_dashboard.database`` re-exports every name here; callers keep importing
them from there.
"""
from typing import Optional

import bcrypt as _bcrypt

# bcrypt only ever reads the first 72 bytes of a password. bcrypt 4 dropped the rest
# silently; bcrypt 5 raises ValueError instead. Every stored hash of a longer password was
# made from its first 72 bytes, so cutting there keeps those users able to sign in, and
# keeps a long password at the login form a wrong-password answer rather than a 500.
BCRYPT_MAX_BYTES = 72


def password_length_error(password: str) -> str:
    """Why ``password`` is too long to set, or ``""`` when it is fine.

    For a password being SET. Verifying keeps cutting at 72 instead (bcrypt_input), so a
    long password hashed by bcrypt 4 still works; a new one is refused rather than having
    its tail ignored without anyone knowing. Counts bytes: é is two, an emoji four.
    """
    size = len((password or "").encode("utf-8"))
    if size > BCRYPT_MAX_BYTES:
        return (f"Passwords can be at most {BCRYPT_MAX_BYTES} bytes (bcrypt reads no "
                f"further); this one is {size} bytes.")
    return ""


def check_password_length(password: str) -> str:
    """:func:`password_length_error` as a ValueError, for callers outside a request."""
    error = password_length_error(password)
    if error:
        raise ValueError(error)
    return password


def bcrypt_input(password: str) -> bytes:
    """The bytes bcrypt actually hashes: the UTF-8 password, cut at 72 bytes."""
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """Verify password against bcrypt hash. Returns False for OAuth-only users (no hash)."""
    if not hashed_password:
        return False
    return _bcrypt.checkpw(
        bcrypt_input(plain_password),
        hashed_password.encode("utf-8") if isinstance(hashed_password, str) else hashed_password,
    )


def get_password_hash(password: str) -> str:
    """Hash a password with bcrypt"""
    return _bcrypt.hashpw(bcrypt_input(password), _bcrypt.gensalt()).decode("utf-8")
