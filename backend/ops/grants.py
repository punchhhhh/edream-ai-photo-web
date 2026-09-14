from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import EnterpriseConsumerGrant, EnterpriseEntryToken, User
from ..routers.auth import get_current_user
from ..services import enterprise_access
from .access import require_enterprise
from .schemas import EnterpriseConsumerGrantAdminOut, EnterpriseGrantActionIn
from .service import audit

router = APIRouter(prefix="/ops/v1/cocreation-grants", tags=["ops-cocreation-grants"])


def _grant_for_enterprise(
    db: Session, enterprise_id: int, grant_id: int
) -> EnterpriseConsumerGrant:
    grant = db.get(EnterpriseConsumerGrant, grant_id)
    if grant is None or grant.enterprise_id != enterprise_id:
        raise HTTPException(404, "共创授权不存在")
    return grant


def _out(
    grant: EnterpriseConsumerGrant, user: User, enterprise_name: str
) -> EnterpriseConsumerGrantAdminOut:
    return EnterpriseConsumerGrantAdminOut(
        id=grant.id,
        enterprise_id=grant.enterprise_id,
        enterprise_name=enterprise_name,
        entry_id=grant.entry_id,
        user_id=grant.user_id,
        oauth_sub=user.oauth_sub,
        display_name=user.display_name,
        email=user.email,
        status=grant.status,
        approval_mode=grant.approval_mode,
        expires_at=grant.expires_at,
        video_limit=grant.video_limit,
        video_used=grant.video_used,
        video_remaining=max(0, grant.video_limit - grant.video_used),
        terms_version=grant.terms_version,
        privacy_version=grant.privacy_version,
        consented_at=grant.consented_at,
        applied_at=grant.applied_at,
        approved_at=grant.approved_at,
        last_used_at=grant.last_used_at,
        decision_reason=grant.decision_reason,
    )


@router.get("", response_model=list[EnterpriseConsumerGrantAdminOut])
def list_cocreation_grants(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    context = require_enterprise(db, user, roles={"owner"})
    rows = db.execute(
        select(EnterpriseConsumerGrant, User)
        .join(User, User.id == EnterpriseConsumerGrant.user_id)
        .where(EnterpriseConsumerGrant.enterprise_id == context.enterprise.id)
        .order_by(EnterpriseConsumerGrant.id.desc())
    ).all()
    for grant, _ in rows:
        enterprise_access.refresh_grant_status(grant)
    db.commit()
    return [_out(grant, member, context.enterprise.name) for grant, member in rows]


@router.post("/{grant_id}/approve", response_model=EnterpriseConsumerGrantAdminOut)
def approve_cocreation_grant(
    grant_id: int,
    payload: EnterpriseGrantActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles={"owner"})
    grant = _grant_for_enterprise(db, context.enterprise.id, grant_id)
    entry = db.get(EnterpriseEntryToken, grant.entry_id)
    member = db.get(User, grant.user_id)
    if entry is None or member is None:
        raise HTTPException(404, "共创授权关联数据不存在")
    if grant.status != "pending":
        raise HTTPException(409, "只有待确认的申请可以通过")
    enterprise_access.activate_grant(
        grant,
        entry,
        approved_by_user_id=user.id,
        ttl_hours=payload.ttl_hours,
    )
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="enterprise.cocreation.approve",
        resource_type="enterprise_consumer_grant",
        resource_id=grant.id,
    )
    db.commit()
    db.refresh(grant)
    return _out(grant, member, context.enterprise.name)


@router.post("/{grant_id}/reject", response_model=EnterpriseConsumerGrantAdminOut)
def reject_cocreation_grant(
    grant_id: int,
    payload: EnterpriseGrantActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles={"owner"})
    grant = _grant_for_enterprise(db, context.enterprise.id, grant_id)
    member = db.get(User, grant.user_id)
    if member is None:
        raise HTTPException(404, "共创用户不存在")
    if grant.status != "pending":
        raise HTTPException(409, "只有待确认的申请可以拒绝")
    grant.status = "rejected"
    grant.decision_reason = payload.reason
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="enterprise.cocreation.reject",
        resource_type="enterprise_consumer_grant",
        resource_id=grant.id,
        details={"reason": payload.reason},
    )
    db.commit()
    db.refresh(grant)
    return _out(grant, member, context.enterprise.name)


@router.post("/{grant_id}/revoke", response_model=EnterpriseConsumerGrantAdminOut)
def revoke_cocreation_grant(
    grant_id: int,
    payload: EnterpriseGrantActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles={"owner"})
    grant = _grant_for_enterprise(db, context.enterprise.id, grant_id)
    member = db.get(User, grant.user_id)
    if member is None:
        raise HTTPException(404, "共创用户不存在")
    if grant.status not in {"pending", "active"}:
        raise HTTPException(409, "当前授权状态无法撤销")
    grant.status = "revoked"
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoked_by_user_id = user.id
    grant.decision_reason = payload.reason
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="enterprise.cocreation.revoke",
        resource_type="enterprise_consumer_grant",
        resource_id=grant.id,
        details={"reason": payload.reason},
    )
    db.commit()
    db.refresh(grant)
    return _out(grant, member, context.enterprise.name)


@router.post("/{grant_id}/renew", response_model=EnterpriseConsumerGrantAdminOut)
def renew_cocreation_grant(
    grant_id: int,
    payload: EnterpriseGrantActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles={"owner"})
    grant = _grant_for_enterprise(db, context.enterprise.id, grant_id)
    entry = db.get(EnterpriseEntryToken, grant.entry_id)
    member = db.get(User, grant.user_id)
    if entry is None or member is None:
        raise HTTPException(404, "共创授权关联数据不存在")
    if grant.status not in {"expired", "exhausted", "revoked"}:
        raise HTTPException(409, "当前授权无需续期")
    if (
        grant.terms_version != entry.terms_version
        or grant.privacy_version != entry.privacy_version
    ):
        raise HTTPException(409, "协议版本已更新，请用户从企业入口重新确认")
    current = enterprise_access.current_grant_for_entry_user(db, entry.id, member.id)
    if current is not None and current.id != grant.id and current.status in {"pending", "active"}:
        raise HTTPException(409, "该用户已有新的有效授权")
    grant.video_used = 0
    grant.revoked_at = None
    grant.revoked_by_user_id = None
    enterprise_access.activate_grant(
        grant,
        entry,
        approved_by_user_id=user.id,
        ttl_hours=payload.ttl_hours,
    )
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="enterprise.cocreation.renew",
        resource_type="enterprise_consumer_grant",
        resource_id=grant.id,
    )
    db.commit()
    db.refresh(grant)
    return _out(grant, member, context.enterprise.name)
