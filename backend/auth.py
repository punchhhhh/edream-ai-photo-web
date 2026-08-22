"""认证核心:OAuth 身份解析(参照 ai-relay-broker)+ 服务端会话签发/校验。"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .models import OAuthSession, User
from .settings import settings


@dataclass(slots=True)
class Principal:
    sub: str
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Casdoor 的 JWT 里主体标识字段不稳定,按 sub → id → owner/name 兜底。"""
    sub = claims.get("sub") or claims.get("id")
    if not sub and claims.get("owner") and claims.get("name"):
        sub = f"{claims['owner']}/{claims['name']}"
    if not sub:
        raise HTTPException(status_code=401, detail="JWT 缺少可用的用户标识")
    return Principal(
        sub=str(sub),
        email=_opt_str(claims.get("email")),
        display_name=_opt_str(claims.get("displayName") or claims.get("name")),
        avatar_url=_opt_str(claims.get("avatar") or claims.get("picture")),
    )


def _opt_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def principal_from_userinfo(claims: dict[str, Any]) -> Principal:
    """OIDC userinfo 端点返回的标准声明,仍按宽容策略取主体标识。"""
    sub = _opt_str(claims.get("sub")) or _opt_str(claims.get("id"))
    if not sub and claims.get("owner") and claims.get("name"):
        sub = f"{claims['owner']}/{claims['name']}"
    if not sub:
        raise HTTPException(status_code=401, detail="userinfo 未返回可用的用户标识")
    return Principal(
        sub=sub,
        email=_opt_str(claims.get("email")),
        display_name=_opt_str(claims.get("name") or claims.get("preferred_username")),
        avatar_url=_opt_str(claims.get("picture") or claims.get("avatar")),
    )


# ---------------------------------------------------------------- 会话

SESSION_COOKIE = settings.session_cookie_name


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite 读回的 datetime 无时区,统一补成 UTC 再比较(PostgreSQL 不受影响)。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upsert_user_from_principal(session: Session, principal: Principal) -> User:
    """首次登录自动建用户,之后每次登录刷新资料。禁用用户拒绝进入。"""
    user = session.scalar(select(User).where(User.oauth_sub == principal.sub))
    if user is None:
        user = User(
            oauth_sub=principal.sub,
            email=principal.email,
            display_name=principal.display_name,
            avatar_url=principal.avatar_url,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号已被禁用")
    changed = False
    for field in ("email", "display_name", "avatar_url"):
        value = getattr(principal, field)
        if value and getattr(user, field) != value:
            setattr(user, field, value)
            changed = True
    if changed:
        session.commit()
        session.refresh(user)
    return user


def issue_session(session: Session, *, user_id: int, user_agent: str | None = None) -> str:
    """签发会话,返回应写入 Cookie 的原始 token(库里只存哈希)。"""
    token = secrets.token_urlsafe(32)
    now = _now()
    session.add(
        OAuthSession(
            token_hash=sha256(token.encode("utf-8")).hexdigest(),
            token_prefix=token[:8],
            user_id=user_id,
            user_agent=(user_agent or "")[:500] or None,
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
            last_seen_at=now,
        )
    )
    # 顺手清理已过期的会话(含已注销的),避免表无限膨胀
    session.execute(delete(OAuthSession).where(OAuthSession.expires_at < now))
    session.commit()
    return token


def resolve_session_user(session: Session, token: str) -> User | None:
    """按 Cookie 里的原始 token 找会话与用户;过期/注销/禁用一律视为未登录。"""
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    row = session.scalar(select(OAuthSession).where(OAuthSession.token_hash == token_hash))
    if row is None or row.revoked_at is not None or aware(row.expires_at) < _now():
        return None
    user = session.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    row.last_seen_at = _now()
    session.commit()
    return user


def revoke_session(session: Session, token: str | None) -> None:
    if not token:
        return
    session.execute(
        update(OAuthSession)
        .where(OAuthSession.token_hash == sha256(token.encode("utf-8")).hexdigest())
        .values(revoked_at=_now())
    )
    session.commit()
