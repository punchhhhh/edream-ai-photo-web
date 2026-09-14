from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Enterprise, EnterpriseEntryToken, EnterpriseMembership, User
from ..ops.schemas import EnterpriseBusinessContextOut, EnterpriseEntryResolveIn
from ..ops.service import audit
from .auth import get_current_user

router = APIRouter(prefix="/enterprise-entry", tags=["enterprise-entry"])

# 成员关系里视为"已启用"的状态(与共创侧判定保持一致)
_ACTIVE_STATUSES = ("active", "approved")


def _context_of(enterprise: Enterprise) -> EnterpriseBusinessContextOut:
    return EnterpriseBusinessContextOut(
        enterprise_id=enterprise.id,
        enterprise_name=enterprise.name,
    )


def _business_context(db: Session, user: User) -> EnterpriseBusinessContextOut | None:
    """当前用户的企业业务身份:任意启用成员关系(Owner 或成员)对应的企业。"""
    membership = db.scalar(
        select(EnterpriseMembership)
        .where(
            EnterpriseMembership.user_id == user.id,
            EnterpriseMembership.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(EnterpriseMembership.id.desc())
    )
    if membership is None:
        return None
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        return None
    return _context_of(enterprise)


@router.get("/context", response_model=EnterpriseBusinessContextOut | None)
def get_enterprise_context(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _business_context(db, user)


@router.post("/resolve", response_model=EnterpriseBusinessContextOut)
def resolve_enterprise_entry(
    payload: EnterpriseEntryResolveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """解析企业入口 token。

    Owner 访问自己的入口 → 返回企业业务身份;入口开启自动加入时,任何登录
    用户访问即自动成为企业成员(role=member, status=active),无需企业二次
    确认,共创等成员能力随之可用;关闭自动加入时非本企业用户一律 403。
    """
    entry = db.scalar(
        select(EnterpriseEntryToken).where(
            EnterpriseEntryToken.token == payload.token,
            EnterpriseEntryToken.status == "active",
        )
    )
    if entry is None:
        raise HTTPException(404, "企业专属入口无效或已更新")
    enterprise = db.get(Enterprise, entry.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        raise HTTPException(404, "企业专属入口无效或已更新")

    membership = db.scalar(
        select(EnterpriseMembership).where(
            EnterpriseMembership.user_id == user.id,
            EnterpriseMembership.enterprise_id == enterprise.id,
        )
    )
    if membership is not None and membership.role == "owner":
        if membership.status not in _ACTIVE_STATUSES:
            raise HTTPException(403, "该企业专属入口不属于当前账号")
        audit(
            db,
            user=user,
            enterprise_id=enterprise.id,
            action="enterprise.entry.resolve",
            resource_type="enterprise_entry",
            resource_id=entry.id,
        )
        db.commit()
        return _context_of(enterprise)

    if entry.auto_join:
        # 幂等:没有关系则创建,被停用的旧关系重新启用
        if membership is None:
            membership = EnterpriseMembership(
                enterprise_id=enterprise.id,
                user_id=user.id,
                role="member",
                status="active",
            )
            db.add(membership)
            db.flush()
        elif membership.status not in _ACTIVE_STATUSES:
            membership.status = "active"
        audit(
            db,
            user=user,
            enterprise_id=enterprise.id,
            action="enterprise.entry.autojoin",
            resource_type="enterprise_membership",
            resource_id=membership.id,
        )
        db.commit()
        return _context_of(enterprise)

    raise HTTPException(403, "该企业专属入口不属于当前账号")
