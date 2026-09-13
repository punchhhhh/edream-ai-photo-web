from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Enterprise, EnterpriseMembership, PlatformUserRole, User
from ..settings import settings


@dataclass(frozen=True, slots=True)
class EnterpriseContext:
    enterprise: Enterprise
    membership: EnterpriseMembership


def is_platform_admin(db: Session, user: User) -> bool:
    if user.oauth_sub in settings.enterprise_admin_subs_set:
        return True
    return (
        db.scalar(
            select(PlatformUserRole.id).where(
                PlatformUserRole.user_id == user.id,
                PlatformUserRole.role == "platform_admin",
                PlatformUserRole.status == "active",
            )
        )
        is not None
    )


def require_platform_admin(db: Session, user: User) -> None:
    if not is_platform_admin(db, user):
        raise HTTPException(403, "仅平台管理员可执行该操作")


def membership_for_user(db: Session, user_id: int) -> EnterpriseMembership | None:
    return db.scalar(
        select(EnterpriseMembership)
        .where(EnterpriseMembership.user_id == user_id)
        .order_by(EnterpriseMembership.id.desc())
    )


def require_enterprise(
    db: Session,
    user: User,
    *,
    roles: set[str] | None = None,
) -> EnterpriseContext:
    membership = membership_for_user(db, user.id)
    if membership is None:
        raise HTTPException(403, "请先提交企业认证")
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if enterprise is None:
        raise HTTPException(404, "企业不存在")
    if enterprise.status != "approved" or membership.status not in {"active", "approved"}:
        raise HTTPException(403, "企业认证审核通过且账号启用后才可访问")
    if roles is not None and membership.role not in roles:
        raise HTTPException(403, "当前企业角色无权执行该操作")
    return EnterpriseContext(enterprise=enterprise, membership=membership)
