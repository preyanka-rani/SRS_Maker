from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.envelope import ok
from app.core.errors import UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.database.models import AppUser
from app.domain.emailnorm import normalize_email
from app.database.session import get_session
from app.schemas.identity import TokenRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

# Verified against when the user does not exist, so timing does not reveal valid emails.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


@router.post("/token")
def login(body: TokenRequest, session: Session = Depends(get_session)) -> dict:
    user = session.execute(select(AppUser).where(AppUser.email == normalize_email(str(body.email)))).scalar_one_or_none()
    password_hash = user.password_hash if user else _DUMMY_HASH
    valid = verify_password(body.password, password_hash)
    if user is None or not valid or not user.is_active:
        raise UnauthorizedError("Invalid credentials")
    ttl = get_settings().jwt_ttl_minutes
    token = create_access_token(str(user.id), user.role.value)
    return ok(TokenResponse(access_token=token, expires_in=ttl * 60, role=user.role))
