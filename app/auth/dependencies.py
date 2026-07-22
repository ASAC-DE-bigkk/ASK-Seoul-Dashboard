"""FastAPI 인증 의존성."""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .models import AuthSession, User
from .security import ROLE_RANK
from .service import AccessService, DomainError


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.database.Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    state_user = getattr(request.state, "user", None)
    if state_user is None:
        raise DomainError(401, "authentication required", "로그인이 필요합니다.")
    user = db.get(User, state_user.id)
    if user is None or user.status != "active":
        raise DomainError(401, "authentication required", "로그인이 필요합니다.")
    return user


def current_session(
    request: Request, db: Session = Depends(get_db)
) -> AuthSession:
    state_session = getattr(request.state, "auth_session", None)
    if state_session is None:
        raise DomainError(401, "authentication required", "로그인이 필요합니다.")
    session = db.get(AuthSession, state_session.id)
    if session is None or session.revoked_at is not None:
        raise DomainError(401, "authentication required", "로그인이 필요합니다.")
    return session


def require_page(page_key: str):
    def dependency(
        user: User = Depends(current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not AccessService(db).can_access(user, page_key):
            raise DomainError(403, "forbidden", "이 영역에 접근할 권한이 없습니다.")
        return user

    return dependency


def require_operator(user: User = Depends(current_user)) -> User:
    if user.role not in {"operator", "admin"}:
        raise DomainError(403, "forbidden", "운영자 이상의 권한이 필요합니다.")
    return user


def require_member(user: User = Depends(current_user)) -> User:
    if ROLE_RANK.get(user.role, 0) < ROLE_RANK["member"]:
        raise DomainError(
            403,
            "forbidden",
            "일반회원 이상의 권한이 필요합니다.",
        )
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise DomainError(403, "forbidden", "최고관리자 권한이 필요합니다.")
    return user
