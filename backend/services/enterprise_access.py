"""企业与 C 端用户的临时共创授权。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    Enterprise,
    EnterpriseCocreationUsageLedger,
    EnterpriseConsumerGrant,
    EnterpriseEntryToken,
    User,
)


ACTIVE_GRANT_STATUS = "active"


@dataclass(frozen=True, slots=True)
class GrantContext:
    enterprise: Enterprise
    grant: EnterpriseConsumerGrant


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def entry_is_available(entry: EnterpriseEntryToken, now: datetime | None = None) -> bool:
    current = now or utc_now()
    return entry.status == "active" and (
        entry.expires_at is None or aware(entry.expires_at) > current
    )


def refresh_grant_status(
    grant: EnterpriseConsumerGrant, now: datetime | None = None
) -> str:
    current = now or utc_now()
    if grant.status == ACTIVE_GRANT_STATUS:
        if grant.expires_at is not None and aware(grant.expires_at) <= current:
            grant.status = "expired"
        elif grant.video_used >= grant.video_limit:
            grant.status = "exhausted"
    return grant.status


def current_grant_for_entry_user(
    db: Session, entry_id: int, user_id: int
) -> EnterpriseConsumerGrant | None:
    grants = db.scalars(
        select(EnterpriseConsumerGrant)
        .where(
            EnterpriseConsumerGrant.entry_id == entry_id,
            EnterpriseConsumerGrant.user_id == user_id,
        )
        .order_by(EnterpriseConsumerGrant.id.desc())
    ).all()
    for grant in grants:
        refresh_grant_status(grant)
        if grant.status in {"pending", "active"}:
            return grant
    return grants[0] if grants else None


def require_grant(
    db: Session,
    user: User,
    grant_id: int | None,
    *,
    lock: bool = False,
) -> GrantContext:
    if grant_id is None:
        raise HTTPException(403, "请先通过企业入口申请共创授权")
    query = select(EnterpriseConsumerGrant).where(
        EnterpriseConsumerGrant.id == grant_id,
        EnterpriseConsumerGrant.user_id == user.id,
    )
    if lock:
        query = query.with_for_update()
    grant = db.scalar(query)
    if grant is None:
        raise HTTPException(404, "企业共创授权不存在")
    status = refresh_grant_status(grant)
    if status != ACTIVE_GRANT_STATUS:
        messages = {
            "pending": "共创授权正在等待企业确认",
            "expired": "本次共创授权已到期",
            "exhausted": "本次共创视频次数已用完",
            "revoked": "企业已撤销本次共创授权",
            "rejected": "企业未通过本次共创申请",
        }
        raise HTTPException(403, messages.get(status, "企业共创授权当前不可用"))
    enterprise = db.get(Enterprise, grant.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        raise HTTPException(403, "企业共创服务当前不可用")
    return GrantContext(enterprise=enterprise, grant=grant)


def create_grant(
    db: Session,
    *,
    entry: EnterpriseEntryToken,
    user: User,
    accepted: bool,
    terms_version: str,
    privacy_version: str,
) -> EnterpriseConsumerGrant:
    existing = current_grant_for_entry_user(db, entry.id, user.id)
    if existing is not None and existing.status in {"pending", "active"}:
        return existing
    if not accepted:
        raise HTTPException(422, "请先阅读并同意共创服务说明与隐私说明")
    if terms_version != entry.terms_version or privacy_version != entry.privacy_version:
        raise HTTPException(409, "服务说明或隐私说明已更新，请重新确认")

    now = utc_now()
    active = entry.approval_mode == "auto"
    grant = EnterpriseConsumerGrant(
        enterprise_id=entry.enterprise_id,
        entry_id=entry.id,
        user_id=user.id,
        status="active" if active else "pending",
        approval_mode=entry.approval_mode,
        approved_at=now if active else None,
        valid_from=now if active else None,
        expires_at=now + timedelta(hours=entry.grant_ttl_hours) if active else None,
        video_limit=entry.video_limit,
        terms_version=terms_version,
        privacy_version=privacy_version,
        consented_at=now,
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = current_grant_for_entry_user(db, entry.id, user.id)
        if existing is None or existing.status not in {"pending", "active"}:
            raise
        return existing
    return grant


def activate_grant(
    grant: EnterpriseConsumerGrant,
    entry: EnterpriseEntryToken,
    *,
    approved_by_user_id: int | None = None,
    ttl_hours: int | None = None,
) -> None:
    now = utc_now()
    grant.status = "active"
    grant.approved_at = now
    grant.approved_by_user_id = approved_by_user_id
    grant.valid_from = now
    grant.expires_at = now + timedelta(hours=ttl_hours or entry.grant_ttl_hours)
    grant.decision_reason = ""


def consume_video_grant(
    db: Session,
    *,
    user: User,
    grant_id: int,
    creation_id: int,
) -> GrantContext:
    context = require_grant(db, user, grant_id, lock=True)
    grant = context.grant
    if grant.video_used >= grant.video_limit:
        grant.status = "exhausted"
        raise HTTPException(409, "本次共创视频次数已用完")
    grant.video_used += 1
    grant.last_used_at = utc_now()
    if grant.video_used >= grant.video_limit:
        grant.status = "exhausted"
    db.add(
        EnterpriseCocreationUsageLedger(
            enterprise_id=grant.enterprise_id,
            grant_id=grant.id,
            user_id=user.id,
            creation_id=creation_id,
            operation_type="video",
            amount=1,
            status="committed",
        )
    )
    return context
