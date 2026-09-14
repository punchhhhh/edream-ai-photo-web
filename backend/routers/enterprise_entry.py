from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Enterprise, EnterpriseEntryToken, EnterpriseMembership, User
from ..ops.schemas import (
    EnterpriseBusinessContextOut,
    EnterpriseConsumerGrantOut,
    EnterpriseEntryApplyIn,
    EnterpriseEntryContextOut,
    EnterpriseEntryPreviewOut,
)
from ..ops.service import audit
from ..services import enterprise_access
from .auth import get_current_user

router = APIRouter(prefix="/enterprise-entry", tags=["enterprise-entry"])
_ACTIVE_STATUSES = ("active", "approved")


def _context_of(enterprise: Enterprise) -> EnterpriseBusinessContextOut:
    return EnterpriseBusinessContextOut(
        enterprise_id=enterprise.id,
        enterprise_name=enterprise.name,
    )


def _owner_business_context(
    db: Session, user: User
) -> EnterpriseBusinessContextOut | None:
    membership = db.scalar(
        select(EnterpriseMembership)
        .where(
            EnterpriseMembership.user_id == user.id,
            EnterpriseMembership.role == "owner",
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


def _entry_by_token(db: Session, token: str) -> tuple[EnterpriseEntryToken, Enterprise]:
    entry = db.scalar(
        select(EnterpriseEntryToken).where(EnterpriseEntryToken.token == token)
    )
    if entry is None or not enterprise_access.entry_is_available(entry):
        raise HTTPException(404, "企业专属入口无效或已更新")
    enterprise = db.get(Enterprise, entry.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        raise HTTPException(404, "企业专属入口无效或已更新")
    return entry, enterprise


def _preview_of(
    entry: EnterpriseEntryToken, enterprise: Enterprise
) -> EnterpriseEntryPreviewOut:
    return EnterpriseEntryPreviewOut(
        enterprise_id=enterprise.id,
        enterprise_name=enterprise.name,
        enterprise_description=enterprise.description or "",
        approval_mode=entry.approval_mode,
        grant_ttl_hours=entry.grant_ttl_hours,
        video_limit=entry.video_limit,
        terms_version=entry.terms_version,
        privacy_version=entry.privacy_version,
    )


def _grant_out(grant, enterprise: Enterprise) -> EnterpriseConsumerGrantOut:
    return EnterpriseConsumerGrantOut(
        id=grant.id,
        enterprise_id=grant.enterprise_id,
        enterprise_name=enterprise.name,
        entry_id=grant.entry_id,
        user_id=grant.user_id,
        status=grant.status,
        approval_mode=grant.approval_mode,
        expires_at=grant.expires_at,
        video_limit=grant.video_limit,
        video_used=grant.video_used,
        video_remaining=max(0, grant.video_limit - grant.video_used),
        terms_version=grant.terms_version,
        privacy_version=grant.privacy_version,
        consented_at=grant.consented_at,
    )


@router.get("/preview", response_model=EnterpriseEntryPreviewOut)
def preview_enterprise_entry(token: str, db: Session = Depends(get_db)):
    """公开展示企业入口信息，不创建账号关系或消耗额度。"""
    entry, enterprise = _entry_by_token(db, token)
    return _preview_of(entry, enterprise)


@router.get("/context", response_model=EnterpriseBusinessContextOut | None)
def get_enterprise_context(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """只返回企业 Owner 的运营身份，C 端授权不是企业成员身份。"""
    return _owner_business_context(db, user)


@router.get("/access-context", response_model=EnterpriseEntryContextOut)
def get_enterprise_access_context(
    token: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry, enterprise = _entry_by_token(db, token)
    grant = enterprise_access.current_grant_for_entry_user(db, entry.id, user.id)
    if grant is not None:
        db.commit()
        db.refresh(grant)
    return EnterpriseEntryContextOut(
        preview=_preview_of(entry, enterprise),
        grant=_grant_out(grant, enterprise) if grant is not None else None,
    )


@router.post("/apply", response_model=EnterpriseConsumerGrantOut)
def apply_enterprise_entry(
    payload: EnterpriseEntryApplyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry, enterprise = _entry_by_token(db, payload.token)
    grant = enterprise_access.create_grant(
        db,
        entry=entry,
        user=user,
        accepted=payload.accepted,
        terms_version=payload.terms_version,
        privacy_version=payload.privacy_version,
    )
    audit(
        db,
        user=user,
        enterprise_id=enterprise.id,
        action="enterprise.cocreation.apply",
        resource_type="enterprise_consumer_grant",
        resource_id=grant.id,
        details={
            "approval_mode": grant.approval_mode,
            "terms_version": grant.terms_version,
            "privacy_version": grant.privacy_version,
        },
    )
    db.commit()
    db.refresh(grant)
    return _grant_out(grant, enterprise)
