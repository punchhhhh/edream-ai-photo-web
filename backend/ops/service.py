import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    EnterpriseAsset,
    EnterpriseAssetVersion,
    EnterpriseMembership,
    EnterpriseQuotaLedger,
    EnterpriseStorageQuota,
    User,
)
from ..settings import settings
from .catalog import PURPOSES
from .schemas import AssetOut, AssetVersionOut, InternalMaterialOut, MembershipOut


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def audit(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: int | str = "",
    enterprise_id: int | None = None,
    user: User | None = None,
    service_id: str = "",
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            enterprise_id=enterprise_id,
            actor_type="user" if user else "service",
            actor_id=user.oauth_sub if user else service_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            details=details or {},
        )
    )


def get_or_create_quota(
    db: Session, enterprise_id: int, *, updated_by_user_id: int | None = None
) -> EnterpriseStorageQuota:
    quota = db.scalar(
        select(EnterpriseStorageQuota).where(EnterpriseStorageQuota.enterprise_id == enterprise_id)
    )
    if quota is None:
        quota = EnterpriseStorageQuota(
            enterprise_id=enterprise_id,
            limit_bytes=settings.enterprise_default_quota_mb * 1024 * 1024,
            used_bytes=0,
            reserved_bytes=0,
            status="active",
            updated_by_user_id=updated_by_user_id,
        )
        db.add(quota)
        db.flush()
    return quota


def reserve_quota(db: Session, enterprise_id: int, size_bytes: int) -> None:
    quota = db.scalar(
        select(EnterpriseStorageQuota)
        .where(EnterpriseStorageQuota.enterprise_id == enterprise_id)
        .with_for_update()
    )
    if quota is None:
        quota = get_or_create_quota(db, enterprise_id)
    if quota.status != "active":
        raise HTTPException(403, "企业空间额度已停用")
    if quota.used_bytes + quota.reserved_bytes + size_bytes > quota.limit_bytes:
        remaining = max(0, quota.limit_bytes - quota.used_bytes - quota.reserved_bytes)
        raise HTTPException(422, f"企业空间不足，当前可用 {remaining} 字节")
    quota.reserved_bytes += size_bytes
    db.commit()


def release_reservation(db: Session, enterprise_id: int, size_bytes: int) -> None:
    quota = db.scalar(
        select(EnterpriseStorageQuota)
        .where(EnterpriseStorageQuota.enterprise_id == enterprise_id)
        .with_for_update()
    )
    if quota is not None:
        quota.reserved_bytes = max(0, quota.reserved_bytes - size_bytes)
        db.commit()


def finalize_upload(
    db: Session,
    *,
    enterprise_id: int,
    asset_id: int,
    size_bytes: int,
    user_id: int,
) -> None:
    quota = db.scalar(
        select(EnterpriseStorageQuota)
        .where(EnterpriseStorageQuota.enterprise_id == enterprise_id)
        .with_for_update()
    )
    if quota is None:
        raise RuntimeError("企业额度记录不存在")
    quota.reserved_bytes = max(0, quota.reserved_bytes - size_bytes)
    quota.used_bytes += size_bytes
    db.add(
        EnterpriseQuotaLedger(
            enterprise_id=enterprise_id,
            asset_id=asset_id,
            delta_bytes=size_bytes,
            reason="upload",
            created_by_user_id=user_id,
        )
    )


def release_asset_quota(
    db: Session,
    *,
    enterprise_id: int,
    asset_id: int,
    size_bytes: int,
    user_id: int,
) -> None:
    quota = db.scalar(
        select(EnterpriseStorageQuota)
        .where(EnterpriseStorageQuota.enterprise_id == enterprise_id)
        .with_for_update()
    )
    if quota is not None:
        quota.used_bytes = max(0, quota.used_bytes - size_bytes)
    if size_bytes:
        db.add(
            EnterpriseQuotaLedger(
                enterprise_id=enterprise_id,
                asset_id=asset_id,
                delta_bytes=-size_bytes,
                reason="delete",
                created_by_user_id=user_id,
            )
        )


def current_version(db: Session, asset: EnterpriseAsset) -> EnterpriseAssetVersion:
    row = db.scalar(
        select(EnterpriseAssetVersion).where(
            EnterpriseAssetVersion.asset_id == asset.id,
            EnterpriseAssetVersion.version_no == asset.current_version,
        )
    )
    if row is None:
        raise HTTPException(500, "素材版本数据不完整")
    return row


def version_for_asset(
    db: Session, asset: EnterpriseAsset, version_no: int | None = None
) -> EnterpriseAssetVersion:
    target = version_no or asset.current_version
    row = db.scalar(
        select(EnterpriseAssetVersion).where(
            EnterpriseAssetVersion.asset_id == asset.id,
            EnterpriseAssetVersion.version_no == target,
            EnterpriseAssetVersion.status != "deleted",
        )
    )
    if row is None:
        raise HTTPException(404, "素材版本不存在")
    return row


def asset_for_enterprise(
    db: Session, enterprise_id: int, asset_id: int, *, include_deleted: bool = False
) -> EnterpriseAsset:
    conditions = [
        EnterpriseAsset.id == asset_id,
        EnterpriseAsset.enterprise_id == enterprise_id,
    ]
    if not include_deleted:
        conditions.append(EnterpriseAsset.status == "active")
    asset = db.scalar(select(EnterpriseAsset).where(*conditions))
    if asset is None:
        raise HTTPException(404, "素材不存在")
    return asset


def checksum_file(fileobj) -> str:
    digest = hashlib.sha256()
    fileobj.seek(0)
    while chunk := fileobj.read(1024 * 1024):
        digest.update(chunk)
    fileobj.seek(0)
    return digest.hexdigest()


def member_out(db: Session, membership: EnterpriseMembership) -> MembershipOut:
    user = db.get(User, membership.user_id)
    if user is None:
        raise HTTPException(500, "企业成员用户不存在")
    return MembershipOut(
        id=membership.id,
        enterprise_id=membership.enterprise_id,
        user_id=membership.user_id,
        oauth_sub=user.oauth_sub,
        display_name=user.display_name,
        email=user.email,
        role=membership.role,
        status=membership.status,
        created_at=membership.created_at,
    )


def asset_out(db: Session, asset: EnterpriseAsset) -> AssetOut:
    version = current_version(db, asset)
    content_url = None
    if version.storage_key:
        content_url = f"/api/ops/v1/assets/{asset.id}/content?version={version.version_no}"
    return AssetOut(
        id=asset.id,
        enterprise_id=asset.enterprise_id,
        purpose=asset.purpose,
        content_type=asset.content_type,
        name=asset.name,
        tags=asset.tags or [],
        description=asset.description,
        status=asset.status,
        current_version=asset.current_version,
        version=AssetVersionOut(
            id=version.id,
            version_no=version.version_no,
            original_filename=version.original_filename,
            mime_type=version.mime_type,
            text_content=version.text_content,
            content_data=version.content_data or {},
            size_bytes=version.size_bytes,
            checksum_sha256=version.checksum_sha256,
            created_at=version.created_at,
        ),
        content_url=content_url,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def internal_material_out(
    db: Session, asset: EnterpriseAsset, *, version_no: int | None = None
) -> InternalMaterialOut:
    version = version_for_asset(db, asset, version_no)
    content_url = None
    if version.storage_key:
        content_url = (
            f"/api/internal/v1/materials/{asset.id}/versions/{version.version_no}/content"
        )
    return InternalMaterialOut(
        asset_id=asset.id,
        version_id=version.id,
        version_no=version.version_no,
        enterprise_id=asset.enterprise_id,
        purpose=asset.purpose,
        content_type=asset.content_type,
        name=asset.name,
        tags=asset.tags or [],
        description=asset.description,
        text_content=version.text_content,
        content_data=version.content_data or {},
        mime_type=version.mime_type,
        size_bytes=version.size_bytes,
        checksum_sha256=version.checksum_sha256,
        content_url=content_url,
    )


def purpose_accepts_version(purpose: str, version: EnterpriseAssetVersion) -> bool:
    if version.text_content is not None:
        return bool(PURPOSES.get(purpose, {}).get("accepts_text"))
    suffix = Path(version.original_filename or "").suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    return suffix in PURPOSES.get(purpose, {}).get("extensions", set())


def total_asset_bytes(db: Session, asset_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(EnterpriseAssetVersion.size_bytes), 0)).where(
                EnterpriseAssetVersion.asset_id == asset_id,
                EnterpriseAssetVersion.status != "deleted",
            )
        )
        or 0
    )
