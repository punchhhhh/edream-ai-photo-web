"""登录路由:Casdoor OAuth 授权码模式(浏览器跳转)+ dev 免登模式。"""

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..auth import (
    Principal,
    SESSION_COOKIE,
    aware,
    issue_session,
    principal_from_userinfo,
    resolve_session_user,
    revoke_session,
    upsert_user_from_principal,
)
from ..database import SessionLocal, get_db
from ..models import OAuthState, User
from ..schemas import AuthUserOut
from ..settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])


def _require_oauth_config() -> None:
    missing = [
        name
        for name, value in (
            ("OAUTH_AUTHORIZE_URL", settings.oauth_authorize_url),
            ("OAUTH_TOKEN_URL", settings.oauth_token_url),
            ("OAUTH_USERINFO_URL", settings.oauth_userinfo_url),
            ("OAUTH_CLIENT_ID", settings.oauth_client_id),
            ("OAUTH_CLIENT_SECRET", settings.oauth_client_secret),
        )
        if not value
    ]
    if missing:
        raise HTTPException(status_code=500, detail=f"OAuth 未配置完整,缺少:{', '.join(missing)}")


def _safe_next(raw: str | None) -> str:
    """登录后的回跳地址只允许站内相对路径,防开放重定向。"""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


def _redirect_uri(request: Request) -> str:
    return settings.oauth_redirect_uri or f"{str(request.base_url).rstrip('/')}/api/auth/callback"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


def get_current_user(
    request: Request,
    response: Response,
    x_casdoor_sub: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """所有 /api 业务接口的登录门槛:优先会话 Cookie;dev 模式额外支持 X-Casdoor-Sub 头。"""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user = resolve_session_user(db, token)
        if user is not None:
            return user
    if settings.auth_mode == "dev" and x_casdoor_sub:
        return upsert_user_from_principal(db, Principal(sub=x_casdoor_sub))
    raise HTTPException(status_code=401, detail="未登录,请先通过 OAuth 登录")


def _finalize_login(db: Session, principal: Principal, next_path: str, user_agent: str | None) -> RedirectResponse:
    user = upsert_user_from_principal(db, principal)
    token = issue_session(db, user_id=user.id, user_agent=user_agent)
    response = RedirectResponse(next_path)
    _set_session_cookie(response, token)
    return response


@router.get("/login")
def login(request: Request, next: str = "/"):
    next_path = _safe_next(next)
    if settings.auth_mode == "dev":
        # dev 模式直接给固定用户发会话,浏览器无感登录
        with SessionLocal() as db:
            return _finalize_login(
                db,
                Principal(sub=settings.dev_auth_sub, display_name="开发用户"),
                next_path,
                request.headers.get("user-agent"),
            )

    _require_oauth_config()
    state = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.oauth_state_ttl_minutes)
    with SessionLocal() as db:
        db.execute(delete(OAuthState).where(OAuthState.expires_at < datetime.now(timezone.utc)))
        db.add(OAuthState(state=state, next_path=next_path, expires_at=expires_at))
        db.commit()
    params = urlencode(
        {
            "client_id": settings.oauth_client_id,
            "response_type": "code",
            "redirect_uri": _redirect_uri(request),
            "scope": "openid profile email",
            "state": state,
        }
    )
    return RedirectResponse(f"{settings.oauth_authorize_url}?{params}")


@router.get("/callback")
def oauth_callback(request: Request, code: str | None = None, state: str | None = None):
    if not code or not state:
        raise HTTPException(status_code=400, detail="缺少 code 或 state 参数")
    _require_oauth_config()

    with SessionLocal() as db:
        row = db.get(OAuthState, state)
        if row is None or aware(row.expires_at) < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="state 无效或已过期,请重新登录")
        next_path = row.next_path or "/"
        db.delete(row)
        db.commit()

        # 授权码换 token,再用 userinfo 端点取身份(由 IdP 校验 token,不依赖 JWKS 本地验签)
        try:
            with httpx.Client(timeout=15) as client:
                token_resp = client.post(
                    settings.oauth_token_url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": settings.oauth_client_id,
                        "client_secret": settings.oauth_client_secret,
                        "code": code,
                        "redirect_uri": _redirect_uri(request),
                    },
                )
                if token_resp.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"换取 token 失败({token_resp.status_code}),请检查 OAuth 配置",
                    )
                token_json = token_resp.json()
                access_token = token_json.get("access_token")
                if not access_token:
                    raise HTTPException(status_code=502, detail="token 端点未返回 access_token")

                userinfo_resp = client.get(
                    settings.oauth_userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"访问 OAuth 服务失败:{e.__class__.__name__}") from e
        except ValueError:
            raise HTTPException(status_code=502, detail="token 端点返回了非 JSON 内容") from None

        if userinfo_resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"获取用户信息失败({userinfo_resp.status_code}),请检查 OAuth 应用配置",
            )
        try:
            userinfo = userinfo_resp.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="userinfo 端点返回了非 JSON 内容") from None

        principal = principal_from_userinfo(userinfo)
        return _finalize_login(db, principal, next_path, request.headers.get("user-agent"))


@router.get("/me", response_model=AuthUserOut)
def me(user: User = Depends(get_current_user)):
    return AuthUserOut(
        id=user.id,
        oauth_sub=user.oauth_sub,
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    revoke_session(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
