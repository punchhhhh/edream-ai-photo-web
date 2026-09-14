"""企业后台 · 共创视频管理:模版 CRUD(含互动剧本与素材绑定)与成员共创视频查看。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Creation,
    EnterpriseAsset,
    EnterpriseAssetVersion,
    EnterpriseTemplateAsset,
    EnterpriseVideoTemplate,
    User,
)
from ..routers.auth import get_current_user
from .. import storage
from .access import require_enterprise
from .enterprise import EDIT_ROLES
from .schemas import (
    CoCreationVideoOut,
    NewApiModelOut,
    NewApiModelsOut,
    TemplateAssetIn,
    TemplateAssetOut,
    VideoTemplateCreateIn,
    VideoTemplateOut,
    VideoTemplateUpdateIn,
)
from ..services import cocreation, newapi_internal
from ..services.newapi_internal import NewApiInternalError
from ..settings import settings
from .service import audit

router = APIRouter(prefix="/ops/v1", tags=["ops-cocreation"])


@router.get("/new-api-models", response_model=NewApiModelsOut)
def list_new_api_models(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """用企业 Owner 当前 Casdoor 主体读取其 new-api 可用模型。"""
    require_enterprise(db, user, roles={"owner"})
    try:
        models = newapi_internal.list_user_models(user.oauth_sub)
    except NewApiInternalError as e:
        raise HTTPException(502, str(e)) from e
    return NewApiModelsOut(
        models=[
            NewApiModelOut(
                id=model.id,
                kind=model.kind,
                endpoint_types=list(model.endpoint_types),
                video_provider=model.video_provider,
            )
            for model in models
        ],
        default_chat_model=settings.new_api_default_chat_model.strip(),
        default_image_model=settings.new_api_default_image_model.strip(),
        default_video_model=settings.new_api_default_video_model.strip(),
    )


def _template_for_enterprise(
    db: Session, enterprise_id: int, template_id: int
) -> EnterpriseVideoTemplate:
    template = db.get(EnterpriseVideoTemplate, template_id)
    if template is None or template.enterprise_id != enterprise_id:
        raise HTTPException(404, "视频模版不存在")
    return template


def _current_version(db: Session, asset: EnterpriseAsset) -> EnterpriseAssetVersion | None:
    return db.scalar(
        select(EnterpriseAssetVersion).where(
            EnterpriseAssetVersion.asset_id == asset.id,
            EnterpriseAssetVersion.version_no == asset.current_version,
            EnterpriseAssetVersion.status != "deleted",
        )
    )


def _apply_template_assets(
    db: Session, enterprise_id: int, template: EnterpriseVideoTemplate, assets: list[TemplateAssetIn]
) -> None:
    """整体替换模版的素材绑定;校验素材归属/类型与用途匹配、数量与合成用量对齐。"""
    seen: set[tuple[int, str]] = set()
    usages: dict[str, int] = {"cover": 0, "character_reference": 0}
    for item in assets:
        if (item.asset_id, item.usage) in seen:
            raise HTTPException(422, f"素材 {item.asset_id} 重复绑定了同一用途")
        seen.add((item.asset_id, item.usage))
        asset = db.get(EnterpriseAsset, item.asset_id)
        if asset is None or asset.enterprise_id != enterprise_id or asset.status != "active":
            raise HTTPException(422, f"素材 {item.asset_id} 不存在或不可用")
        version = _current_version(db, asset)
        if item.usage in ("character_reference", "cover"):
            if asset.content_type != "image" or version is None or not version.storage_key:
                raise HTTPException(422, f"素材「{asset.name}」不是可用的图片素材,不能作为形象参考或封面")
        elif item.usage == "prompt_text":
            if asset.content_type != "text" or version is None or not (version.text_content or "").strip():
                raise HTTPException(422, f"素材「{asset.name}」没有文字内容,不能注入提示词")
        usages[item.usage] = usages.get(item.usage, 0) + 1
    if usages["cover"] > 1:
        raise HTTPException(422, "封面素材只能绑定一个")
    if usages["character_reference"] > cocreation.MAX_CHARACTER_REFERENCES:
        raise HTTPException(
            422,
            f"形象参考图最多绑定 {cocreation.MAX_CHARACTER_REFERENCES} 张"
            f"(首帧合成时受多参考图模型输入上限约束)",
        )

    db.execute(
        EnterpriseTemplateAsset.__table__.delete().where(
            EnterpriseTemplateAsset.template_id == template.id
        )
    )
    for item in assets:
        db.add(
            EnterpriseTemplateAsset(
                template_id=template.id,
                asset_id=item.asset_id,
                usage=item.usage,
                sort_order=item.sort_order,
            )
        )


def _template_out(db: Session, template: EnterpriseVideoTemplate) -> VideoTemplateOut:
    rows = db.execute(
        select(EnterpriseTemplateAsset, EnterpriseAsset)
        .join(EnterpriseAsset, EnterpriseAsset.id == EnterpriseTemplateAsset.asset_id)
        .where(EnterpriseTemplateAsset.template_id == template.id)
        .order_by(EnterpriseTemplateAsset.usage, EnterpriseTemplateAsset.sort_order, EnterpriseTemplateAsset.id)
    ).all()
    assets = [
        TemplateAssetOut(
            asset_id=binding.asset_id,
            asset_name=asset.name,
            usage=binding.usage,
            sort_order=binding.sort_order,
            content_type=asset.content_type,
            purpose=asset.purpose,
            mime_type=asset.mime_type,
        )
        for binding, asset in rows
    ]
    cover_url = next(
        (
            f"/api/ops/v1/assets/{binding.asset_id}/content"
            for binding, _ in rows
            if binding.usage == "cover"
        ),
        None,
    )
    data = {
        c.name: getattr(template, c.name)
        for c in EnterpriseVideoTemplate.__table__.columns
        if c.name in VideoTemplateOut.model_fields
    }
    return VideoTemplateOut(**data, assets=assets, cover_url=cover_url)


@router.get("/video-templates", response_model=list[VideoTemplateOut])
def list_video_templates(
    db: Session = Depends(get_db), user=Depends(get_current_user)
):
    context = require_enterprise(db, user)
    templates = cocreation.list_templates(db, context.enterprise.id, include_inactive=True)
    return [_template_out(db, t) for t in templates]


@router.post("/video-templates", response_model=VideoTemplateOut)
def create_video_template(
    payload: VideoTemplateCreateIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    fields = payload.model_dump(exclude={"assets"})
    template = EnterpriseVideoTemplate(
        enterprise_id=context.enterprise.id,
        created_by_user_id=user.id,
        **fields,
    )
    db.add(template)
    db.flush()
    _apply_template_assets(db, context.enterprise.id, template, payload.assets)
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="video_template.create",
        resource_type="video_template",
        resource_id=template.id,
        details={
            "name": payload.name,
            "video_model": payload.video_model,
            "assets": [a.asset_id for a in payload.assets],
        },
    )
    db.commit()
    db.refresh(template)
    return _template_out(db, template)


@router.patch("/video-templates/{template_id}", response_model=VideoTemplateOut)
def update_video_template(
    template_id: int,
    payload: VideoTemplateUpdateIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    template = _template_for_enterprise(db, context.enterprise.id, template_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"assets"})
    for field, value in changes.items():
        setattr(template, field, value)
    if payload.assets is not None:
        _apply_template_assets(db, context.enterprise.id, template, payload.assets)
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="video_template.update",
        resource_type="video_template",
        resource_id=template.id,
        details={
            "fields": sorted(changes),
            "assets_updated": payload.assets is not None,
        },
    )
    db.commit()
    db.refresh(template)
    return _template_out(db, template)


@router.delete("/video-templates/{template_id}")
def delete_video_template(
    template_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    template = _template_for_enterprise(db, context.enterprise.id, template_id)
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="video_template.delete",
        resource_type="video_template",
        resource_id=template.id,
        details={"name": template.name},
    )
    db.delete(template)
    db.commit()
    return {"ok": True}


@router.get("/cocreation/videos", response_model=list[CoCreationVideoOut])
def list_cocreation_videos(
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """企业主/管理员查看本企业全部成员的共创视频。"""
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    rows = db.execute(
        select(Creation, User)
        .join(User, User.id == Creation.user_id)
        .where(
            Creation.enterprise_id == context.enterprise.id,
            Creation.template_id.isnot(None),
        )
        .order_by(Creation.id.desc())
        .limit(min(limit, 500))
    ).all()
    result: list[CoCreationVideoOut] = []
    for creation, creator in rows:
        result.append(
            CoCreationVideoOut(
                id=creation.id,
                creator_id=creator.id,
                creator_sub=creator.oauth_sub,
                creator_name=creator.display_name,
                template_id=creation.template_id,
                template_name=creation.template_name,
                input_text=creation.input_text,
                video_model=creation.video_model,
                duration=creation.duration,
                status=creation.status,
                error=creation.error,
                # 播放地址输出时推导;video_path 为空时回退到远端链接
                video_url=(storage.url(creation.video_path) if creation.video_path else None)
                or creation.video_url,
                created_at=creation.created_at,
            )
        )
    return result
