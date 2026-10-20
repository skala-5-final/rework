from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from rework.config import get_settings
from rework.db.models import User, now
from rework.db.session import get_db

COOKIE = "rework_token"


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def create_token(user: User) -> str:
    cfg = get_settings()
    return jwt.encode({"sub": user.id, "tenant": user.tenant_id,
                       "exp": now() + timedelta(minutes=cfg.jwt_ttl_minutes)}, cfg.jwt_secret, algorithm="HS256")


@dataclass
class CurrentUser:
    id: str
    tenant_id: str
    username: str
    display_name: str


def _token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return request.cookies.get(COOKIE)


def current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    token = _token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다.")
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 만료되었거나 올바르지 않습니다.") from e
    user = db.get(User, claims["sub"])
    if user is None or user.tenant_id != claims["tenant"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다.")
    return CurrentUser(user.id, user.tenant_id, user.username, user.display_name)
