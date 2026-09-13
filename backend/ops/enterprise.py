from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import storage
from ..database import get_db
from ..models import (
    Enterprise,
    EnterpriseAsset,
    EnterpriseAssetVersion,
    EnterpriseMembership,
    PlatformUserRole,
    User,
)
from ..routers.auth import get_current_user
from ..schemas import AuthUserOut
from ..settings import settings
from .access import (
    is_platform_admin,
    membership_for_user,
    require_enterprise,
    require_platform_admin,
)
from .catalog import describe_upload, normalize_tags, validate_purpose, validate_text
from .schemas import (
    AssetOut,
    AssetTextCreateIn,
    AssetUpdateIn,
    EnterpriseApplyIn,
    EnterpriseOut,
    EnterpriseReviewIn,
    EnterpriseStatusIn,
    EnterpriseUpdateIn,
    MemberCreateIn,
    MembershipOut,
    MemberUpdateIn,
    OpsProfileOut,
    PlatformAdminCreateIn,
    PlatformAdminOut,
    QuotaOut,
    QuotaUpdateIn,
    UploadLimitsOut,
)
from .service import (
    asset_for_enterprise,
    asset_out,
    audit,
    checksum_file,
    current_version,
    finalize_upload,
    get_or_create_quota,
    member_out,
    purpose_accepts_version,
    release_asset_quota,
    release_reservation,
    reserve_quota,
    version_for_asset,
)

router = APIRouter(prefix="/ops/v1", tags=["ops-enterprise"])
EDIT_ROLES = {"owner", "admin", "editor"}
ADMIN_ROLES = {"owner", "admin"}


def _auth_user_out(user: User) -> AuthUserOut:
    return AuthUserOut(
        id=user.id,
        oauth_sub=user.oauth_sub,
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


def _enterprise_out(enterprise: Enterprise | None) -> EnterpriseOut | None:
    return EnterpriseOut.model_validate(enterprise) if enterprise else None


def _duplicate_credit_code(
    db: Session, credit_code: str, *, exclude_enterprise_id: int | None = None
) -> bool:
    query = select(Enterprise.id).where(
        Enterprise.credit_code == credit_code,
    )
    if exclude_enterprise_id is not None:
        query = query.where(Enterprise.id != exclude_enterprise_id)
    return db.scalar(query) is not None


def _profile(db: Session, user: User) -> OpsProfileOut:
    membership = membership_for_user(db, user.id)
    enterprise = db.get(Enterprise, membership.enterprise_id) if membership else None
    quota = None
    if enterprise and enterprise.status in {"approved", "suspended"}:
        quota = get_or_create_quota(db, enterprise.id)
        db.commit()
    return OpsProfileOut(
        user=_auth_user_out(user),
        enterprise=_enterprise_out(enterprise),
        membership=member_out(db, membership) if membership else None,
        quota=QuotaOut.model_validate(quota) if quota else None,
        upload_limits=UploadLimitsOut(
            batch_max_files=settings.enterprise_batch_max_files,
            batch_max_bytes=settings.enterprise_batch_max_mb * 1024 * 1024,
            image_max_bytes=settings.enterprise_image_max_mb * 1024 * 1024,
            document_max_bytes=settings.enterprise_document_max_mb * 1024 * 1024,
            source_max_bytes=settings.enterprise_source_max_mb * 1024 * 1024,
            video_max_bytes=settings.enterprise_video_max_mb * 1024 * 1024,
        ),
        is_platform_admin=is_platform_admin(db, user),
    )


@router.get("/profile", response_model=OpsProfileOut)
def get_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _profile(db, user)


@router.post("/enterprise", response_model=OpsProfileOut)
def apply_enterprise(
    payload: EnterpriseApplyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing_membership = membership_for_user(db, user.id)
    if existing_membership is not None:
        existing_enterprise = db.get(Enterprise, existing_membership.enterprise_id)
        if existing_enterprise is None or existing_enterprise.status != "archived":
            raise HTTPException(409, "当前账号已有企业关系")
    credit_code = payload.credit_code.strip()
    if _duplicate_credit_code(db, credit_code):
        raise HTTPException(409, "统一社会信用代码已存在")
    enterprise = Enterprise(
        name=payload.name.strip(),
        credit_code=credit_code,
        contact_name=payload.contact_name.strip(),
        contact_phone=payload.contact_phone.strip(),
        contact_email=payload.contact_email.strip(),
        description=payload.description.strip(),
        status="pending",
        created_by_user_id=user.id,
    )
    db.add(enterprise)
    db.flush()
    db.add(
        EnterpriseMembership(
            enterprise_id=enterprise.id,
            user_id=user.id,
            role="owner",
            status="pending",
        )
    )
    audit(
        db,
        user=user,
        enterprise_id=enterprise.id,
        action="enterprise.apply",
        resource_type="enterprise",
        resource_id=enterprise.id,
    )
    db.commit()
    return _profile(db, user)


@router.put("/enterprise", response_model=OpsProfileOut)
def update_enterprise(
    payload: EnterpriseUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    membership = membership_for_user(db, user.id)
    if membership is None or membership.role != "owner":
        raise HTTPException(403, "仅企业 Owner 可修改企业资料")
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if enterprise is None or enterprise.status == "archived":
        raise HTTPException(404, "企业不存在")
    credit_code = payload.credit_code.strip()
    if _duplicate_credit_code(db, credit_code, exclude_enterprise_id=enterprise.id):
        raise HTTPException(409, "统一社会信用代码已存在")
    legal_changed = enterprise.name != payload.name.strip() or enterprise.credit_code != credit_code
    enterprise.name = payload.name.strip()
    enterprise.credit_code = credit_code
    enterprise.contact_name = payload.contact_name.strip()
    enterprise.contact_phone = payload.contact_phone.strip()
    enterprise.contact_email = payload.contact_email.strip()
    enterprise.description = payload.description.strip()
    if enterprise.status in {"rejected", "pending"} or legal_changed:
        enterprise.status = "pending"
        enterprise.rejection_reason = None
        membership.status = "pending"
    audit(
        db,
        user=user,
        enterprise_id=enterprise.id,
        action="enterprise.update",
        resource_type="enterprise",
        resource_id=enterprise.id,
        details={"requires_review": legal_changed},
    )
    db.commit()
    return _profile(db, user)


@router.delete("/enterprise")
def cancel_enterprise(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    membership = membership_for_user(db, user.id)
    if membership is None or membership.role != "owner":
        raise HTTPException(403, "仅企业 Owner 可撤销认证")
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if enterprise is None:
        raise HTTPException(404, "企业不存在")
    if enterprise.status not in {"pending", "rejected"}:
        raise HTTPException(409, "已通过认证的企业需由平台管理员归档")
    enterprise.status = "archived"
    membership.status = "disabled"
    audit(
        db,
        user=user,
        enterprise_id=enterprise.id,
        action="enterprise.cancel",
        resource_type="enterprise",
        resource_id=enterprise.id,
    )
    db.commit()
    return {"ok": True}


@router.get("/quota", response_model=QuotaOut)
def get_quota(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    context = require_enterprise(db, user)
    quota = get_or_create_quota(db, context.enterprise.id)
    db.commit()
    return QuotaOut.model_validate(quota)


@router.get("/members", response_model=list[MembershipOut])
def list_members(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    context = require_enterprise(db, user)
    rows = db.scalars(
        select(EnterpriseMembership)
        .where(EnterpriseMembership.enterprise_id == context.enterprise.id)
        .order_by(EnterpriseMembership.id)
    ).all()
    return [member_out(db, row) for row in rows]


@router.post("/members", response_model=MembershipOut)
def add_member(
    payload: MemberCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=ADMIN_ROLES)
    target = db.scalar(select(User).where(User.oauth_sub == payload.oauth_sub.strip()))
    if target is None:
        target = User(oauth_sub=payload.oauth_sub.strip(), status="active")
        db.add(target)
        db.flush()
    other = db.scalar(
        select(EnterpriseMembership).where(
            EnterpriseMembership.user_id == target.id,
            EnterpriseMembership.enterprise_id != context.enterprise.id,
            EnterpriseMembership.status.in_({"pending", "active", "approved"}),
        )
    )
    if other is not None:
        raise HTTPException(409, "该账号已属于其他企业")
    membership = db.scalar(
        select(EnterpriseMembership).where(
            EnterpriseMembership.user_id == target.id,
            EnterpriseMembership.enterprise_id == context.enterprise.id,
        )
    )
    if membership is None:
        membership = EnterpriseMembership(
            enterprise_id=context.enterprise.id,
            user_id=target.id,
            role=payload.role,
            status="active",
        )
        db.add(membership)
    else:
        membership.role = payload.role
        membership.status = "active"
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="member.add",
        resource_type="user",
        resource_id=target.id,
        details={"role": payload.role},
    )
    db.commit()
    db.refresh(membership)
    return member_out(db, membership)


@router.patch("/members/{membership_id}", response_model=MembershipOut)
def update_member(
    membership_id: int,
    payload: MemberUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=ADMIN_ROLES)
    membership = db.get(EnterpriseMembership, membership_id)
    if membership is None or membership.enterprise_id != context.enterprise.id:
        raise HTTPException(404, "企业成员不存在")
    if membership.role == "owner" and (payload.role not in {None, "owner"} or payload.status == "disabled"):
        raise HTTPException(409, "不能降级或停用企业 Owner")
    if payload.role is not None:
        membership.role = payload.role
    if payload.status is not None:
        membership.status = payload.status
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="member.update",
        resource_type="membership",
        resource_id=membership.id,
        details={"role": membership.role, "status": membership.status},
    )
    db.commit()
    db.refresh(membership)
    return member_out(db, membership)


@router.delete("/members/{membership_id}")
def delete_member(
    membership_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=ADMIN_ROLES)
    membership = db.get(EnterpriseMembership, membership_id)
    if membership is None or membership.enterprise_id != context.enterprise.id:
        raise HTTPException(404, "企业成员不存在")
    if membership.role == "owner":
        raise HTTPException(409, "不能移除企业 Owner")
    membership.status = "disabled"
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="member.disable",
        resource_type="membership",
        resource_id=membership.id,
    )
    db.commit()
    return {"ok": True}


@router.get("/assets", response_model=list[AssetOut])
def list_assets(
    purpose: str | None = None,
    content_type: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user)
    conditions = [
        EnterpriseAsset.enterprise_id == context.enterprise.id,
        EnterpriseAsset.status == "active",
    ]
    if purpose:
        validate_purpose(purpose)
        conditions.append(EnterpriseAsset.purpose == purpose)
    if content_type:
        conditions.append(EnterpriseAsset.content_type == content_type)
    if q:
        conditions.append(EnterpriseAsset.name.ilike(f"%{q.strip()}%"))
    rows = db.scalars(
        select(EnterpriseAsset).where(*conditions).order_by(EnterpriseAsset.id.desc())
    ).all()
    if tag:
        rows = [row for row in rows if tag in (row.tags or [])]
    return [asset_out(db, row) for row in rows]


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user)
    return asset_out(db, asset_for_enterprise(db, context.enterprise.id, asset_id))


@router.post("/assets/text", response_model=AssetOut)
def create_text_asset(
    payload: AssetTextCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    content = validate_text(payload.purpose, payload.text_content)
    asset = EnterpriseAsset(
        enterprise_id=context.enterprise.id,
        created_by_user_id=user.id,
        uploaded_by_user_id=user.id,
        kind="text",
        purpose=payload.purpose,
        content_type="text",
        name=payload.name.strip(),
        mime_type="text/plain",
        asset_path="",
        size_bytes=0,
        tags=normalize_tags(payload.tags),
        description=payload.description.strip(),
        current_version=1,
        status="active",
    )
    db.add(asset)
    db.flush()
    db.add(
        EnterpriseAssetVersion(
            asset_id=asset.id,
            version_no=1,
            mime_type="text/plain",
            text_content=content,
            content_data=payload.content_data,
            size_bytes=0,
            checksum_sha256="",
            status="active",
            created_by_user_id=user.id,
        )
    )
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="asset.create_text",
        resource_type="asset",
        resource_id=asset.id,
        details={"purpose": payload.purpose},
    )
    db.commit()
    db.refresh(asset)
    return asset_out(db, asset)


@router.post("/assets/file", response_model=AssetOut)
def create_file_asset(
    file: UploadFile = File(...),
    purpose: str = Form(...),
    name: str = Form(default=""),
    tags: str = Form(default=""),
    description: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    descriptor = describe_upload(file, purpose)
    normalized_tags = normalize_tags(tags)
    reserve_quota(db, context.enterprise.id, descriptor.size_bytes)
    asset = EnterpriseAsset(
        enterprise_id=context.enterprise.id,
        created_by_user_id=user.id,
        uploaded_by_user_id=user.id,
        kind=descriptor.storage_kind,
        purpose=purpose,
        content_type=descriptor.content_type,
        name=(name.strip() or descriptor.original_filename)[:200],
        mime_type=descriptor.mime_type,
        asset_path="",
        size_bytes=0,
        tags=normalized_tags,
        description=description.strip()[:1000],
        current_version=1,
        status="uploading",
    )
    try:
        db.add(asset)
        db.commit()
        db.refresh(asset)
        storage_key = storage.save_enterprise_seekable(
            descriptor.storage_kind,
            file.file,
            descriptor.extension,
            enterprise_id=context.enterprise.id,
            asset_id=asset.id,
            version=1,
            max_bytes=descriptor.max_bytes,
            content_type=descriptor.mime_type,
        )
        checksum = checksum_file(file.file)
        db.add(
            EnterpriseAssetVersion(
                asset_id=asset.id,
                version_no=1,
                original_filename=descriptor.original_filename,
                mime_type=descriptor.mime_type,
                storage_key=storage_key,
                size_bytes=descriptor.size_bytes,
                checksum_sha256=checksum,
                status="active",
                created_by_user_id=user.id,
            )
        )
        asset.status = "active"
        asset.asset_path = storage_key
        asset.size_bytes = descriptor.size_bytes
        finalize_upload(
            db,
            enterprise_id=context.enterprise.id,
            asset_id=asset.id,
            size_bytes=descriptor.size_bytes,
            user_id=user.id,
        )
        audit(
            db,
            user=user,
            enterprise_id=context.enterprise.id,
            action="asset.upload",
            resource_type="asset",
            resource_id=asset.id,
            details={"purpose": purpose, "size_bytes": descriptor.size_bytes},
        )
        db.commit()
    except Exception:
        db.rollback()
        saved_key = locals().get("storage_key")
        if saved_key:
            storage.delete(saved_key)
        if asset.id:
            failed = db.get(EnterpriseAsset, asset.id)
            if failed:
                failed.status = "failed"
                db.commit()
        release_reservation(db, context.enterprise.id, descriptor.size_bytes)
        raise
    db.refresh(asset)
    return asset_out(db, asset)


@router.post("/assets/files", response_model=list[AssetOut])
def create_file_assets(
    files: list[UploadFile] = File(...),
    purpose: str = Form(...),
    material_type: str = Form(...),
    tags: str = Form(default=""),
    description: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    if not files:
        raise HTTPException(422, "请至少选择一个文件")
    if len(files) > settings.enterprise_batch_max_files:
        raise HTTPException(422, f"每批最多上传 {settings.enterprise_batch_max_files} 个文件")
    if material_type not in {"image", "video", "document"}:
        raise HTTPException(422, "未知素材类型")

    descriptors = [describe_upload(file, purpose) for file in files]
    for descriptor in descriptors:
        descriptor_type = "image" if descriptor.content_type == "source" else descriptor.content_type
        if descriptor_type != material_type:
            raise HTTPException(422, "同一批次只能上传同类型文件")

    total_bytes = sum(descriptor.size_bytes for descriptor in descriptors)
    batch_max_bytes = settings.enterprise_batch_max_mb * 1024 * 1024
    if total_bytes > batch_max_bytes:
        raise HTTPException(
            422,
            f"单批文件总大小不能超过 {settings.enterprise_batch_max_mb}MB",
        )

    normalized_tags = normalize_tags(tags)
    reserve_quota(db, context.enterprise.id, total_bytes)
    assets: list[EnterpriseAsset] = []
    saved_keys: list[str] = []
    try:
        for descriptor in descriptors:
            assets.append(
                EnterpriseAsset(
                    enterprise_id=context.enterprise.id,
                    created_by_user_id=user.id,
                    uploaded_by_user_id=user.id,
                    kind=descriptor.storage_kind,
                    purpose=purpose,
                    content_type=descriptor.content_type,
                    name=(Path(descriptor.original_filename).stem or descriptor.original_filename)[:200],
                    mime_type=descriptor.mime_type,
                    asset_path="",
                    size_bytes=0,
                    tags=normalized_tags,
                    description=description.strip()[:1000],
                    current_version=1,
                    status="uploading",
                )
            )
        db.add_all(assets)
        db.commit()
        for asset in assets:
            db.refresh(asset)

        for file, descriptor, asset in zip(files, descriptors, assets, strict=True):
            storage_key = storage.save_enterprise_seekable(
                descriptor.storage_kind,
                file.file,
                descriptor.extension,
                enterprise_id=context.enterprise.id,
                asset_id=asset.id,
                version=1,
                max_bytes=descriptor.max_bytes,
                content_type=descriptor.mime_type,
            )
            saved_keys.append(storage_key)
            db.add(
                EnterpriseAssetVersion(
                    asset_id=asset.id,
                    version_no=1,
                    original_filename=descriptor.original_filename,
                    mime_type=descriptor.mime_type,
                    storage_key=storage_key,
                    size_bytes=descriptor.size_bytes,
                    checksum_sha256=checksum_file(file.file),
                    status="active",
                    created_by_user_id=user.id,
                )
            )
            asset.status = "active"
            asset.asset_path = storage_key
            asset.size_bytes = descriptor.size_bytes
            finalize_upload(
                db,
                enterprise_id=context.enterprise.id,
                asset_id=asset.id,
                size_bytes=descriptor.size_bytes,
                user_id=user.id,
            )
            audit(
                db,
                user=user,
                enterprise_id=context.enterprise.id,
                action="asset.upload",
                resource_type="asset",
                resource_id=asset.id,
                details={
                    "purpose": purpose,
                    "size_bytes": descriptor.size_bytes,
                    "batch_files": len(files),
                    "batch_total_bytes": total_bytes,
                },
            )
        db.commit()
    except Exception:
        db.rollback()
        for storage_key in saved_keys:
            storage.delete(storage_key)
        for asset in assets:
            if asset.id and (failed := db.get(EnterpriseAsset, asset.id)):
                failed.status = "failed"
        db.commit()
        release_reservation(db, context.enterprise.id, total_bytes)
        raise

    for asset in assets:
        db.refresh(asset)
    return [asset_out(db, asset) for asset in assets]


@router.patch("/assets/{asset_id}", response_model=AssetOut)
def update_asset(
    asset_id: int,
    payload: AssetUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    asset = asset_for_enterprise(db, context.enterprise.id, asset_id)
    version = current_version(db, asset)
    new_purpose = payload.purpose or asset.purpose
    validate_purpose(new_purpose)
    if not purpose_accepts_version(new_purpose, version):
        raise HTTPException(422, "当前素材格式不适用于新的用途分类")
    if payload.name is not None:
        asset.name = payload.name.strip()
    asset.purpose = new_purpose
    if payload.tags is not None:
        asset.tags = normalize_tags(payload.tags)
    if payload.description is not None:
        asset.description = payload.description.strip()
    if payload.text_content is not None or payload.content_data is not None:
        if asset.content_type != "text":
            raise HTTPException(422, "文件素材请使用替换文件接口")
        text_content = validate_text(new_purpose, payload.text_content or version.text_content or "")
        version.status = "superseded"
        asset.current_version += 1
        db.add(
            EnterpriseAssetVersion(
                asset_id=asset.id,
                version_no=asset.current_version,
                mime_type="text/plain",
                text_content=text_content,
                content_data=(
                    payload.content_data if payload.content_data is not None else version.content_data
                ),
                status="active",
                created_by_user_id=user.id,
            )
        )
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="asset.update",
        resource_type="asset",
        resource_id=asset.id,
        details={"version": asset.current_version},
    )
    db.commit()
    db.refresh(asset)
    return asset_out(db, asset)


@router.post("/assets/{asset_id}/file", response_model=AssetOut)
def replace_asset_file(
    asset_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    asset = asset_for_enterprise(db, context.enterprise.id, asset_id)
    if asset.content_type == "text":
        raise HTTPException(422, "文字素材请直接编辑文字内容")
    descriptor = describe_upload(file, asset.purpose)
    reserve_quota(db, context.enterprise.id, descriptor.size_bytes)
    new_version = asset.current_version + 1
    try:
        storage_key = storage.save_enterprise_seekable(
            descriptor.storage_kind,
            file.file,
            descriptor.extension,
            enterprise_id=context.enterprise.id,
            asset_id=asset.id,
            version=new_version,
            max_bytes=descriptor.max_bytes,
            content_type=descriptor.mime_type,
        )
        checksum = checksum_file(file.file)
        current_version(db, asset).status = "superseded"
        db.add(
            EnterpriseAssetVersion(
                asset_id=asset.id,
                version_no=new_version,
                original_filename=descriptor.original_filename,
                mime_type=descriptor.mime_type,
                storage_key=storage_key,
                size_bytes=descriptor.size_bytes,
                checksum_sha256=checksum,
                status="active",
                created_by_user_id=user.id,
            )
        )
        asset.current_version = new_version
        asset.content_type = descriptor.content_type
        asset.kind = descriptor.storage_kind
        asset.mime_type = descriptor.mime_type
        asset.asset_path = storage_key
        asset.size_bytes = descriptor.size_bytes
        finalize_upload(
            db,
            enterprise_id=context.enterprise.id,
            asset_id=asset.id,
            size_bytes=descriptor.size_bytes,
            user_id=user.id,
        )
        audit(
            db,
            user=user,
            enterprise_id=context.enterprise.id,
            action="asset.replace",
            resource_type="asset",
            resource_id=asset.id,
            details={"version": new_version, "size_bytes": descriptor.size_bytes},
        )
        db.commit()
    except Exception:
        db.rollback()
        saved_key = locals().get("storage_key")
        if saved_key:
            storage.delete(saved_key)
        release_reservation(db, context.enterprise.id, descriptor.size_bytes)
        raise
    db.refresh(asset)
    return asset_out(db, asset)


@router.get("/assets/{asset_id}/content")
def get_asset_content(
    asset_id: int,
    version: int | None = Query(default=None, ge=1),
    download: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user)
    asset = asset_for_enterprise(db, context.enterprise.id, asset_id)
    row = version_for_asset(db, asset, version)
    if not row.storage_key:
        raise HTTPException(404, "文字素材没有文件内容")
    signed = storage.private_presigned_url(
        row.storage_key,
        download_name=row.original_filename if download else None,
        content_type=row.mime_type,
    )
    if signed:
        return RedirectResponse(signed)
    path = storage.local_path(row.storage_key)
    if path is None or not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    if download:
        return FileResponse(
            path,
            media_type=row.mime_type,
            filename=row.original_filename,
            content_disposition_type="attachment",
        )
    return FileResponse(path, media_type=row.mime_type)


@router.delete("/assets/{asset_id}")
def delete_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    asset = asset_for_enterprise(db, context.enterprise.id, asset_id, include_deleted=True)
    if asset.status not in {"active", "deleting"}:
        raise HTTPException(404, "素材不存在")
    asset.status = "deleting"
    db.commit()
    versions = db.scalars(
        select(EnterpriseAssetVersion).where(
            EnterpriseAssetVersion.asset_id == asset.id,
            EnterpriseAssetVersion.status != "deleted",
        )
    ).all()
    failed = [row for row in versions if row.storage_key and not storage.delete(row.storage_key)]
    if failed:
        raise HTTPException(503, "对象存储删除失败，素材已停止使用，请稍后重试")
    released = sum(row.size_bytes for row in versions)
    for row in versions:
        row.status = "deleted"
    asset.status = "deleted"
    asset.deleted_at = datetime.now(timezone.utc)
    asset.asset_path = ""
    asset.size_bytes = 0
    release_asset_quota(
        db,
        enterprise_id=context.enterprise.id,
        asset_id=asset.id,
        size_bytes=released,
        user_id=user.id,
    )
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="asset.delete",
        resource_type="asset",
        resource_id=asset.id,
        details={"released_bytes": released},
    )
    db.commit()
    return {"ok": True}


@router.get("/admin/enterprises", response_model=list[EnterpriseOut])
def list_enterprises(
    status: str = "all",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    query = select(Enterprise).order_by(Enterprise.id.desc())
    if status != "all":
        query = query.where(Enterprise.status == status)
    return [EnterpriseOut.model_validate(row) for row in db.scalars(query).all()]


@router.post("/admin/enterprises/{enterprise_id}/review", response_model=EnterpriseOut)
def review_enterprise(
    enterprise_id: int,
    payload: EnterpriseReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    enterprise = db.get(Enterprise, enterprise_id)
    if enterprise is None or enterprise.status == "archived":
        raise HTTPException(404, "企业不存在")
    if payload.status == "rejected" and not payload.reason.strip():
        raise HTTPException(422, "驳回时必须填写原因")
    enterprise.status = payload.status
    enterprise.rejection_reason = payload.reason.strip() if payload.status == "rejected" else None
    enterprise.reviewed_by_user_id = user.id
    enterprise.reviewed_at = datetime.now(timezone.utc)
    memberships = db.scalars(
        select(EnterpriseMembership).where(EnterpriseMembership.enterprise_id == enterprise.id)
    ).all()
    for membership in memberships:
        membership.status = "active" if payload.status == "approved" else "pending"
    if payload.status == "approved":
        get_or_create_quota(db, enterprise.id, updated_by_user_id=user.id)
    audit(
        db,
        user=user,
        enterprise_id=enterprise.id,
        action=f"enterprise.{payload.status}",
        resource_type="enterprise",
        resource_id=enterprise.id,
        details={"reason": payload.reason.strip()},
    )
    db.commit()
    db.refresh(enterprise)
    return EnterpriseOut.model_validate(enterprise)


@router.patch("/admin/enterprises/{enterprise_id}/status", response_model=EnterpriseOut)
def update_enterprise_status(
    enterprise_id: int,
    payload: EnterpriseStatusIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    enterprise = db.get(Enterprise, enterprise_id)
    if enterprise is None:
        raise HTTPException(404, "企业不存在")
    if enterprise.status == "archived":
        raise HTTPException(409, "已归档企业不能恢复")
    enterprise.status = payload.status
    if payload.status == "archived":
        memberships = db.scalars(
            select(EnterpriseMembership).where(EnterpriseMembership.enterprise_id == enterprise.id)
        ).all()
        for membership in memberships:
            membership.status = "disabled"
        quota = get_or_create_quota(db, enterprise.id)
        quota.status = "disabled"
    audit(
        db,
        user=user,
        enterprise_id=enterprise.id,
        action=f"enterprise.status.{payload.status}",
        resource_type="enterprise",
        resource_id=enterprise.id,
        details={"reason": payload.reason.strip()},
    )
    db.commit()
    db.refresh(enterprise)
    return EnterpriseOut.model_validate(enterprise)


@router.get("/admin/enterprises/{enterprise_id}/quota", response_model=QuotaOut)
def admin_get_quota(
    enterprise_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    if db.get(Enterprise, enterprise_id) is None:
        raise HTTPException(404, "企业不存在")
    quota = get_or_create_quota(db, enterprise_id, updated_by_user_id=user.id)
    db.commit()
    return QuotaOut.model_validate(quota)


@router.put("/admin/enterprises/{enterprise_id}/quota", response_model=QuotaOut)
def admin_update_quota(
    enterprise_id: int,
    payload: QuotaUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    if db.get(Enterprise, enterprise_id) is None:
        raise HTTPException(404, "企业不存在")
    quota = get_or_create_quota(db, enterprise_id, updated_by_user_id=user.id)
    if payload.limit_bytes < quota.used_bytes + quota.reserved_bytes:
        raise HTTPException(422, "额度不能低于当前已使用和预占空间")
    old_limit = quota.limit_bytes
    quota.limit_bytes = payload.limit_bytes
    quota.updated_by_user_id = user.id
    audit(
        db,
        user=user,
        enterprise_id=enterprise_id,
        action="quota.update",
        resource_type="quota",
        resource_id=quota.id,
        details={"old_limit": old_limit, "new_limit": payload.limit_bytes},
    )
    db.commit()
    db.refresh(quota)
    return QuotaOut.model_validate(quota)


@router.get("/admin/platform-admins", response_model=list[PlatformAdminOut])
def list_platform_admins(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    require_platform_admin(db, user)
    result: list[PlatformAdminOut] = []
    seen: set[str] = set()
    for sub in sorted(settings.enterprise_admin_subs_set):
        target = db.scalar(select(User).where(User.oauth_sub == sub))
        result.append(
            PlatformAdminOut(
                user_id=target.id if target else 0,
                oauth_sub=sub,
                display_name=target.display_name if target else None,
                email=target.email if target else None,
                source="environment",
            )
        )
        seen.add(sub)
    rows = db.scalars(
        select(PlatformUserRole).where(
            PlatformUserRole.role == "platform_admin", PlatformUserRole.status == "active"
        )
    ).all()
    for row in rows:
        target = db.get(User, row.user_id)
        if target and target.oauth_sub not in seen:
            result.append(
                PlatformAdminOut(
                    user_id=target.id,
                    oauth_sub=target.oauth_sub,
                    display_name=target.display_name,
                    email=target.email,
                    source="database",
                )
            )
    return result


@router.post("/admin/platform-admins", response_model=PlatformAdminOut)
def add_platform_admin(
    payload: PlatformAdminCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    sub = payload.oauth_sub.strip()
    target = db.scalar(select(User).where(User.oauth_sub == sub))
    if target is None:
        target = User(oauth_sub=sub, status="active")
        db.add(target)
        db.flush()
    role = db.scalar(
        select(PlatformUserRole).where(
            PlatformUserRole.user_id == target.id, PlatformUserRole.role == "platform_admin"
        )
    )
    if role is None:
        role = PlatformUserRole(
            user_id=target.id,
            role="platform_admin",
            status="active",
            created_by_user_id=user.id,
        )
        db.add(role)
    else:
        role.status = "active"
    audit(
        db,
        user=user,
        action="platform_admin.add",
        resource_type="user",
        resource_id=target.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "该账号已经是平台管理员") from exc
    return PlatformAdminOut(
        user_id=target.id,
        oauth_sub=target.oauth_sub,
        display_name=target.display_name,
        email=target.email,
        source="database",
    )


@router.delete("/admin/platform-admins/{target_user_id}")
def delete_platform_admin(
    target_user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    require_platform_admin(db, user)
    target = db.get(User, target_user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    if target.oauth_sub in settings.enterprise_admin_subs_set:
        raise HTTPException(409, "环境变量引导管理员不能在页面中移除")
    role = db.scalar(
        select(PlatformUserRole).where(
            PlatformUserRole.user_id == target.id,
            PlatformUserRole.role == "platform_admin",
            PlatformUserRole.status == "active",
        )
    )
    if role is None:
        raise HTTPException(404, "平台管理员不存在")
    role.status = "disabled"
    audit(
        db,
        user=user,
        action="platform_admin.disable",
        resource_type="user",
        resource_id=target.id,
    )
    db.commit()
    return {"ok": True}
