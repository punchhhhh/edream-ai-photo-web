"""企业共创视频:C 端用户凭临时授权使用企业模版和企业主网关密钥生成视频。

可见性:临时授权有效 + 企业认证通过 + 企业配置了启用模版 + 服务端已配置共创网关。
次数限制:按授权记录视频提交次数，内容删除或任务失败不返还。
互动剧本:模版可绑定企业 IP 形象参考图与特征文字;成员上传照片后先合成
「合拍首帧」,确认(或模版设为自动)后以首帧为起点图生视频。
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import media, storage
from ..database import get_db
from ..models import (
    Creation,
    Enterprise,
    EnterpriseConsumerGrant,
    EnterpriseVideoTemplate,
    User,
)
from ..schemas import (
    CoCreationExpandIn,
    CoCreationFirstFrameIn,
    CoCreationFirstFrameOut,
    CoCreationStatusOut,
    CoCreationTemplateOut,
    CoCreationVideoCreateIn,
    CreationOut,
    ExpandOut,
)
from ..services import cocreation, enterprise_access
from ..services.ai_client import AICallError, AIClient
from ..services.pipeline import ACTIVE_STATUSES, IMAGE_MIME_BY_EXT, start_creation_thread
from ..settings import settings
from .auth import get_current_user
from .creations import _to_out

router = APIRouter(tags=["cocreation"])


def _co_template_out(
    db: Session, template: EnterpriseVideoTemplate, grant_id: int
) -> CoCreationTemplateOut:
    """成员端模版视图:附带绑定素材的派生信息(封面地址、形象参考图数量)。"""
    bindings = cocreation.template_bindings(db, template.id)
    has_cover = any(b.binding.usage == "cover" for b in bindings)
    data = {
        c.name: getattr(template, c.name)
        for c in EnterpriseVideoTemplate.__table__.columns
        if c.name in CoCreationTemplateOut.model_fields
    }
    data["character_asset_count"] = sum(
        1 for b in bindings if b.binding.usage == "character_reference"
    )
    data["cover_url"] = (
        f"/api/cocreation/templates/{template.id}/cover?grant_id={grant_id}"
        if has_cover
        else None
    )
    data["can_expand"] = bool(template.chat_model)
    return CoCreationTemplateOut(**data)


def _status_out(
    db: Session, user: User, grant_id: int | None
) -> CoCreationStatusOut:
    if not cocreation.cocreation_gateway_ready():
        return CoCreationStatusOut(available=False, reason="平台尚未开启企业共创服务")
    if grant_id is None:
        return CoCreationStatusOut(
            available=False, reason="请从企业专属入口申请共创授权"
        )
    grant = db.get(EnterpriseConsumerGrant, grant_id)
    if grant is None or grant.user_id != user.id:
        return CoCreationStatusOut(available=False, reason="企业共创授权不存在")
    status = enterprise_access.refresh_grant_status(grant)
    enterprise = db.get(Enterprise, grant.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        return CoCreationStatusOut(available=False, reason="企业共创服务当前不可用")
    db.commit()
    templates = cocreation.list_templates(db, enterprise.id)
    if not templates:
        return CoCreationStatusOut(
            available=False,
            reason="企业还没有配置共创视频模版",
            grant_id=grant.id,
            enterprise_name=enterprise.name,
            expires_at=grant.expires_at,
            used=grant.video_used,
            limit=grant.video_limit,
        )
    reasons = {
        "pending": "共创授权正在等待企业确认",
        "expired": "本次共创授权已到期",
        "exhausted": "本次共创视频次数已用完",
        "revoked": "企业已撤销本次共创授权",
        "rejected": "企业未通过本次共创申请",
    }
    return CoCreationStatusOut(
        available=status == "active",
        reason=reasons.get(status, ""),
        templates=[_co_template_out(db, t, grant.id) for t in templates],
        used=grant.video_used,
        limit=grant.video_limit,
        grant_id=grant.id,
        enterprise_name=enterprise.name,
        expires_at=grant.expires_at,
    )


@router.get("/cocreation/status", response_model=CoCreationStatusOut)
def cocreation_status(
    grant_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _status_out(db, user, grant_id)


def _member_media_path(user: User, image_path: str | None) -> str | None:
    """校验成员提交的媒体 key:必须是本人目录下的图片文件且确实存在。"""
    if not image_path:
        return None
    if not media.is_safe_rel(image_path):
        raise HTTPException(422, "图片路径不合法")
    if not image_path.startswith(f"users/{user.id}/"):
        raise HTTPException(422, "只能使用你自己的图片")
    if Path(image_path).suffix.lower() not in IMAGE_MIME_BY_EXT:
        raise HTTPException(422, "只能使用图片文件")
    if not storage.exists(image_path):
        raise HTTPException(422, "图片文件不存在,请重新生成或上传")
    return image_path


@router.post("/cocreation/first-frame", response_model=CoCreationFirstFrameOut)
def cocreation_first_frame(
    payload: CoCreationFirstFrameIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """合拍首帧合成:成员照片 + 企业 IP 形象参考图 → 同框互动首帧(用企业网关密钥)。"""
    if not cocreation.cocreation_gateway_ready():
        raise HTTPException(403, "平台尚未开启企业共创服务")
    context = cocreation.cocreation_context(db, user, payload.grant_id)
    template = cocreation.get_template(db, context.enterprise.id, payload.template_id)
    if template is None or not template.is_active:
        raise HTTPException(404, "视频模版不存在或已停用")

    image_inputs: list[tuple[bytes, str]] = []
    member_path = _member_media_path(user, payload.member_photo_path)
    if member_path:
        image_inputs.append((
            storage.read(member_path),
            IMAGE_MIME_BY_EXT.get(Path(member_path).suffix.lower(), "image/png"),
        ))
    if template.member_photo == "required" and member_path is None:
        raise HTTPException(422, "本模版需要出镜,请先上传你的照片再合成画面")

    bindings = cocreation.template_bindings(db, template.id)
    character_refs = cocreation.character_reference_images(bindings)
    if not image_inputs and not character_refs:
        raise HTTPException(422, "请先上传照片,或联系企业为模版配置形象参考素材")
    image_inputs.extend(character_refs)

    # 首帧不占视频配额,但合成花企业网关额度:输入校验都过了才按用户按天计数
    limit = settings.cocreation_first_frame_daily_limit
    if limit > 0 and not cocreation.first_frame_allow(user.id, limit):
        raise HTTPException(429, f"今日首帧合成次数已用完(每天 {limit} 次),请明天再试")

    ip_features = cocreation.prompt_text_features(bindings)
    prompt = cocreation.first_frame_compose_prompt(template, payload.text or "", ip_features)
    base_url, api_key = cocreation.enterprise_gateway_credentials(db, context.enterprise.id)
    image_model = cocreation.resolve_image_model(db, template)
    with AIClient(base_url, api_key) as client:
        try:
            data, ext = client.compose_image(image_model, prompt, image_inputs, payload.size)
        except AICallError as e:
            raise HTTPException(502, str(e)) from e
    rel = storage.save_bytes("images", data, ext, user_id=user.id)
    return CoCreationFirstFrameOut(image_path=rel, url=storage.url(rel))


@router.get("/cocreation/templates/{template_id}/cover")
def cocreation_template_cover(
    template_id: int,
    grant_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """模版封面(企业素材不进公开媒体目录,由成员鉴权后按绑定读取)。"""
    context = cocreation.cocreation_context(db, user, grant_id)
    template = cocreation.get_template(db, context.enterprise.id, template_id)
    if template is None or not template.is_active:
        raise HTTPException(404, "视频模版不存在或已停用")
    cover = next(iter(cocreation.template_bindings(db, template.id, usage="cover")), None)
    if cover is None or not cover.version.storage_key:
        raise HTTPException(404, "模版未配置封面")
    signed = storage.private_presigned_url(
        cover.version.storage_key, content_type=cover.version.mime_type or "image/png"
    )
    if signed:
        return RedirectResponse(signed)
    path = storage.local_path(cover.version.storage_key)
    if path is None or not path.is_file():
        raise HTTPException(404, "封面文件不存在")
    return FileResponse(path, media_type=cover.version.mime_type or "image/png")


@router.post("/cocreation/expand", response_model=ExpandOut)
def cocreation_expand(
    payload: CoCreationExpandIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用企业主的网关密钥做 AI 文本拓展(模版配置了 chat_model 才可用)。"""
    context = cocreation.cocreation_context(db, user, payload.grant_id)
    template = cocreation.get_template(db, context.enterprise.id, payload.template_id)
    if template is None or not template.is_active:
        raise HTTPException(404, "视频模版不存在或已停用")
    if not template.chat_model:
        raise HTTPException(422, "当前模版未配置文本模型,不支持 AI 拓展")
    base_url, api_key = cocreation.enterprise_gateway_credentials(
        db, context.enterprise.id
    )
    # 模版画面要求作为风格要点交给 LLM,让拓展结果贴合企业模版调性
    with AIClient(base_url, api_key) as client:
        try:
            expanded = client.expand_prompt(
                template.chat_model,
                payload.text,
                style_description=template.prompt,
            )
        except AICallError as e:
            raise HTTPException(502, str(e)) from e
    return ExpandOut(expanded_prompt=expanded)


@router.post("/cocreation/videos", response_model=CreationOut)
def create_cocreation_video(
    payload: CoCreationVideoCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not cocreation.cocreation_gateway_ready():
        raise HTTPException(403, "平台尚未开启企业共创服务")
    context = cocreation.cocreation_context(db, user, payload.grant_id)
    template = cocreation.get_template(db, context.enterprise.id, payload.template_id)
    if template is None or not template.is_active:
        raise HTTPException(404, "视频模版不存在或已停用")
    if not template.video_model:
        raise HTTPException(422, "当前模版未配置视频模型,请联系企业管理员")

    first_frame = _member_media_path(user, payload.first_frame_path)
    if template.member_photo == "required" and first_frame is None:
        raise HTTPException(422, "本模版需要出镜,请先上传照片并合成合拍画面")

    # 与个人创作共用"同用户同时只有一个生成中任务"的约束
    active = db.scalars(
        select(Creation)
        .where(Creation.user_id == user.id, Creation.status.in_(ACTIVE_STATUSES))
        .order_by(Creation.id.desc())
    ).first()
    if active is not None:
        raise HTTPException(
            409,
            f"已有视频正在生成中(任务 {active.id}),请等待完成或失败后再提交新任务",
        )

    # 模版画面要求 + IP 特征文字 + 成员创意;素材版本快照落库,便于事后追溯
    ip_features = cocreation.prompt_text_features(
        cocreation.template_bindings(db, template.id)
    )
    expanded = (payload.expanded_prompt or payload.text).strip()
    creation = Creation(
        user_id=user.id,
        input_text=payload.text.strip(),
        style="",
        expanded_prompt=cocreation.combine_prompt(template, expanded, ip_features),
        image_source="generated" if first_frame else "none",
        image_path=first_frame,
        duration=template.duration,
        status="pending",
        config_name=f"企业共创 · {template.name}",
        chat_model=template.chat_model,
        image_model=template.image_model if first_frame else "",
        video_model=template.video_model,
        enterprise_id=context.enterprise.id,
        template_id=template.id,
        template_name=template.name,
        cocreation_materials=cocreation.material_snapshot(db, template.id),
        enterprise_grant_id=context.grant.id,
    )
    db.add(creation)
    try:
        db.flush()
        enterprise_access.consume_video_grant(
            db,
            user=user,
            grant_id=context.grant.id,
            creation_id=creation.id,
        )
        db.commit()
    except IntegrityError:
        # 并发提交竞态由 (user_id, active) 部分唯一索引兜底
        db.rollback()
        raise HTTPException(409, "已有视频正在生成中,请勿重复提交") from None
    db.refresh(creation)

    start_creation_thread(creation.id)
    return _to_out(creation)
