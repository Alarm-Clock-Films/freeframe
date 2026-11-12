from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uuid
from typing import Optional
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.auth_service import decode_token, get_user_by_id
from ..models.user import User, UserStatus

bearer_scheme = HTTPBearer()
optional_bearer_scheme = HTTPBearer(auto_error=False)


def token_is_current(payload: dict, user: User) -> bool:
    """Whether a token was minted at the user's current token_version.

    The version is bumped when a password changes and when an account is
    deactivated, which ends every session signed before that. A token from
    before the claim existed counts as version 1, as /auth/refresh treats it.
    """
    return payload.get("ver", 1) == user.token_version


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    token = credentials.credentials
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = get_user_by_id(db, uuid.UUID(payload["sub"]))
    if not user or user.status == UserStatus.deactivated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or deactivated")
    # Checked here, not only on refresh: without it an access token signed
    # before a password change kept working until it expired.
    if not token_is_current(payload, user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user

def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Returns the authenticated user if a valid token is provided, None otherwise."""
    if not credentials:
        return None
    try:
        payload = decode_token(credentials.credentials)
        if not payload or payload.get("type") != "access":
            return None
        user = get_user_by_id(db, uuid.UUID(payload["sub"]))
        if not user or user.status == UserStatus.deactivated:
            return None
        if not token_is_current(payload, user):
            return None
        return user
    except Exception:
        return None

