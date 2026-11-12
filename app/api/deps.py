"""Auth and project-scoped authorization dependencies."""
import uuid
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import decode_access_token
from app.database.models import AppUser, Project, ProjectMember, UserRole
from app.database.session import get_session

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str
    role: UserRole


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> CurrentUser:
    if creds is None:
        raise UnauthorizedError("Missing bearer token")
    claims = decode_access_token(creds.credentials)
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Invalid token subject") from exc
    user = session.get(AppUser, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive")
    return CurrentUser(id=user.id, email=user.email, role=user.role)


def require_roles(*roles: UserRole):
    def checker(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role not in roles:
            raise ForbiddenError("Insufficient role")
        return user

    return checker


def get_member_project(session: Session, user: CurrentUser, project_id: uuid.UUID) -> Project:
    """Return the project if the user may see it. Non-members get 404, not 403,
    so project existence is never confirmed to outsiders."""
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found")
    if user.role == UserRole.admin:
        return project
    is_member = session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
        )
    ).first()
    if is_member is None:
        raise NotFoundError("Project not found")
    return project
