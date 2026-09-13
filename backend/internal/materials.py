import hashlib
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..database import get_db
from ..models import Enterprise, EnterpriseAsset
from ..ops.catalog import validate_purpose
from ..ops.schemas import InternalMaterialOut, InternalMaterialSearchIn
from ..ops.service import audit, internal_material_out, version_for_asset
from ..settings import settings

router = APIRouter(prefix="/internal/v1", tags=["internal-materials"])


def require_internal_service(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> str:
    configured = settings.internal_service_tokens_set
    if not configured:
        raise HTTPException(503, "内部素材服务未配置")
    if not x_internal_token or not any(
        secrets.compare_digest(x_internal_token, token) for token in configured
    ):
        raise HTTPException(401, "内部服务凭据无效")
    return hashlib.sha256(x_internal_token.encode("utf-8")).hexdigest()[:12]


def _search(
    db: Session, payload: InternalMaterialSearchIn
) -> tuple[Enterprise, list[EnterpriseAsset]]:
    enterprise = db.get(Enterprise, payload.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        raise HTTPException(404, "企业不可用")
    for purpose in payload.purposes:
        validate_purpose(purpose)
    conditions = [
        EnterpriseAsset.enterprise_id == enterprise.id,
        EnterpriseAsset.status == "active",
    ]
    if payload.purposes:
        conditions.append(EnterpriseAsset.purpose.in_(payload.purposes))
    if payload.content_types:
        conditions.append(EnterpriseAsset.content_type.in_(payload.content_types))
    if payload.asset_ids:
        conditions.append(EnterpriseAsset.id.in_(payload.asset_ids))
    rows = list(
        db.scalars(
            select(EnterpriseAsset)
            .where(*conditions)
            .order_by(EnterpriseAsset.id.desc())
            .limit(payload.limit)
        ).all()
    )
    if payload.tags:
        rows = [row for row in rows if all(tag in (row.tags or []) for tag in payload.tags)]
    return enterprise, rows


@router.post("/materials/search", response_model=list[InternalMaterialOut])
def search_materials(
    payload: InternalMaterialSearchIn,
    db: Session = Depends(get_db),
    service_id: str = Depends(require_internal_service),
):
    enterprise, rows = _search(db, payload)
    audit(
        db,
        service_id=service_id,
        enterprise_id=enterprise.id,
        action="internal.material.search",
        resource_type="enterprise",
        resource_id=enterprise.id,
        details={"asset_ids": [row.id for row in rows], "purposes": payload.purposes},
    )
    db.commit()
    return [internal_material_out(db, row) for row in rows]


@router.post("/materials/batch-get", response_model=list[InternalMaterialOut])
def batch_get_materials(
    payload: InternalMaterialSearchIn,
    db: Session = Depends(get_db),
    service_id: str = Depends(require_internal_service),
):
    if not payload.asset_ids:
        raise HTTPException(422, "batch-get 必须提供 asset_ids")
    enterprise, rows = _search(db, payload)
    audit(
        db,
        service_id=service_id,
        enterprise_id=enterprise.id,
        action="internal.material.batch_get",
        resource_type="enterprise",
        resource_id=enterprise.id,
        details={"asset_ids": payload.asset_ids},
    )
    db.commit()
    return [internal_material_out(db, row) for row in rows]


@router.get("/enterprises/{enterprise_id}/material-profile")
def material_profile(
    enterprise_id: int,
    db: Session = Depends(get_db),
    service_id: str = Depends(require_internal_service),
):
    enterprise, rows = _search(db, InternalMaterialSearchIn(enterprise_id=enterprise_id))
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.purpose, []).append(
            internal_material_out(db, row).model_dump(mode="json")
        )
    audit(
        db,
        service_id=service_id,
        enterprise_id=enterprise.id,
        action="internal.material.profile",
        resource_type="enterprise",
        resource_id=enterprise.id,
        details={"count": len(rows)},
    )
    db.commit()
    return {"enterprise_id": enterprise.id, "materials": grouped}


@router.get(
    "/enterprises/{enterprise_id}/materials/{asset_id}/versions/{version_no}/content"
)
def get_material_content(
    enterprise_id: int,
    asset_id: int,
    version_no: int,
    db: Session = Depends(get_db),
    service_id: str = Depends(require_internal_service),
):
    asset = db.scalar(
        select(EnterpriseAsset).where(
            EnterpriseAsset.id == asset_id,
            EnterpriseAsset.enterprise_id == enterprise_id,
            EnterpriseAsset.status == "active",
        )
    )
    if asset is None:
        raise HTTPException(404, "素材不存在")
    enterprise = db.get(Enterprise, enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        raise HTTPException(404, "企业不可用")
    version = version_for_asset(db, asset, version_no)
    if not version.storage_key:
        raise HTTPException(404, "文字素材没有文件内容")
    audit(
        db,
        service_id=service_id,
        enterprise_id=enterprise.id,
        action="internal.material.download",
        resource_type="asset_version",
        resource_id=version.id,
        details={"asset_id": asset.id, "version": version_no},
    )
    db.commit()
    signed = storage.private_presigned_url(
        version.storage_key,
        content_type=version.mime_type,
    )
    if signed:
        return RedirectResponse(signed)
    path = storage.local_path(version.storage_key)
    if path is None or not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    return FileResponse(path, media_type=version.mime_type, filename=version.original_filename)
