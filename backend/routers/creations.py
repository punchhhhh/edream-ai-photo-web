import json as _json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import media, storage
from ..database import get_db
from ..models import Creation, ModelConfig, StylePreset, User, VlogProject
from ..schemas import CreationIn, CreationOut, ExpandIn, ExpandOut, ImageGenIn, MediaOut
from ..settings import settings
from ..services.ai_client import AICallError, AIClient
from ..services.pipeline import ACTIVE_STATUSES, start_creation_thread
from .auth import get_current_user

router = APIRouter(tags=["creations"])


def _get_config(db: Session, user: User, config_id: int) -> ModelConfig:
    config = db.get(ModelConfig, config_id)
    if config is None or config.user_id != user.id:
        raise HTTPException(404, f"模型配置 {config_id} 不存在")
    return config


def _get_creation(db: Session, user: User, creation_id: int) -> Creation:
    creation = db.get(Creation, creation_id)
    if creation is None or creation.user_id != user.id:
        raise HTTPException(404, f"创作任务 {creation_id} 不存在")
    return creation


def _to_out(creation: Creation, vlog_project_id: int | None = None) -> CreationOut:
    data = {
        c.name: getattr(creation, c.name)
        for c in Creation.__table__.columns
        if c.name in CreationOut.model_fields
    }
    data["image_url"] = storage.url(creation.image_path)
    # 本地/对象存储的播放地址输出时推导;列里的 video_url 只作为"远端回退地址"(下载失败时保留网关链接)
    data["video_url"] = storage.url(creation.video_path) or creation.video_url
    data.pop("video_path", None)
    data["vlog_project_id"] = vlog_project_id
    return CreationOut(**data)


def _vlog_project_ids_for_creations(
    db: Session, user: User, creation_ids: list[int]
) -> dict[int, int]:
    if not creation_ids:
        return {}
    rows = db.execute(
        select(VlogProject.final_creation_id, VlogProject.id).where(
            VlogProject.user_id == user.id,
            VlogProject.final_creation_id.in_(creation_ids),
        )
    ).all()
    return {creation_id: project_id for creation_id, project_id in rows if creation_id is not None}


def _active_creation(db: Session, user: User) -> Creation | None:
    return db.scalars(
        select(Creation)
        .where(Creation.user_id == user.id, Creation.status.in_(ACTIVE_STATUSES))
        .order_by(Creation.id.desc())
    ).first()


# ---------------------------------------------------------------- 文本拓展

@router.post("/expand", response_model=ExpandOut)
def expand_text(
    payload: ExpandIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    config = _get_config(db, user, payload.config_id)
    # 命中风格预设时,把结构化的画面语言要点一并交给 LLM,而不是只给一个风格词
    style_description = ""
    if payload.style:
        preset = db.scalar(select(StylePreset).where(StylePreset.name == payload.style))
        if preset is not None:
            style_description = preset.description
    with AIClient(config.base_url, config.api_key) as client:
        try:
            expanded = client.expand_prompt(
                config.chat_model, payload.text, payload.style, style_description=style_description
            )
        except AICallError as e:
            raise HTTPException(502, str(e)) from e
    return ExpandOut(expanded_prompt=expanded)


# ---------------------------------------------------------------- 图片

@router.post("/generate-image", response_model=MediaOut)
def generate_image(
    payload: ImageGenIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    config = _get_config(db, user, payload.config_id)
    with AIClient(config.base_url, config.api_key) as client:
        try:
            data, ext = client.generate_image(config.image_model, payload.prompt, payload.size)
        except AICallError as e:
            raise HTTPException(502, str(e)) from e
    rel = storage.save_bytes("images", data, ext, user_id=user.id)
    return MediaOut(image_path=rel, url=storage.url(rel))


@router.post("/upload", response_model=MediaOut)
def upload_image(file: UploadFile = File(...), user: User = Depends(get_current_user)):
    content_type = (file.content_type or "").lower()
    if content_type not in media.ALLOWED_IMAGE_TYPES:
        raise HTTPException(422, "仅支持 PNG / JPEG / WebP / GIF 图片")
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(422, f"图片不能超过 {settings.max_upload_mb}MB")
    if not data:
        raise HTTPException(422, "上传的图片为空")
    ext = media.sniff_image(data)
    if ext is None:
        raise HTTPException(422, "文件内容不是有效的图片(PNG / JPEG / WebP / GIF)")
    rel = storage.save_bytes("uploads", data, ext, user_id=user.id)
    return MediaOut(image_path=rel, url=storage.url(rel))


# ---------------------------------------------------------------- 创作任务

@router.post("/creations", response_model=CreationOut)
def create_creation(
    payload: CreationIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    config = _get_config(db, user, payload.config_id)
    if not config.video_model:
        raise HTTPException(422, "当前配置未填写视频模型(video_model),无法生成视频")
    if payload.image_source not in ("none", "generated", "uploaded"):
        raise HTTPException(422, "image_source 仅支持 none / generated / uploaded")
    if payload.image_source != "none":
        if not payload.image_path:
            raise HTTPException(422, "选择图片路径时必须先生成或上传图片")
        # 只接受本服务生成/上传接口返回的存储 key,拒绝 `..`/绝对路径等穿越写法
        if not media.is_safe_rel(payload.image_path):
            raise HTTPException(422, "图片路径不合法")
        if not storage.exists(payload.image_path):
            raise HTTPException(422, "图片文件不存在,请重新生成或上传")

    # 同一用户同时只允许一个生成中任务:应用层先拦一道
    active = _active_creation(db, user)
    if active is not None:
        raise HTTPException(
            409,
            f"已有视频正在生成中(任务 {active.id}),请等待完成或失败后再提交新任务",
        )

    creation = Creation(
        user_id=user.id,
        input_text=payload.input_text,
        style=payload.style,
        expanded_prompt=payload.expanded_prompt or payload.input_text,
        image_source=payload.image_source,
        image_path=payload.image_path if payload.image_source != "none" else None,
        duration=payload.duration,
        status="pending",
        config_id=config.id,
        config_name=config.name,
        chat_model=config.chat_model,
        image_model=config.image_model,
        video_model=config.video_model,
    )
    db.add(creation)
    try:
        db.commit()
    except IntegrityError:
        # 并发提交竞态由 (user_id, active) 部分唯一索引兜底
        db.rollback()
        raise HTTPException(409, "已有视频正在生成中,请勿重复提交") from None
    db.refresh(creation)

    start_creation_thread(creation.id)
    return _to_out(creation)


# ---------------------------------------------------------------- 浏览器合成入库

@router.post("/creations/merged", response_model=CreationOut)
def create_merged_creation(
    file: UploadFile = File(...),
    title: str = Form("剪辑合成"),
    source_ids: str = Form("[]"),
    total_duration: float = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接收浏览器端(FFmpeg.wasm)合成好的成片,入库并保存文件(流式,不整读进内存)。"""
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("video/"):
        raise HTTPException(422, "仅支持视频文件")
    limit = settings.max_video_upload_mb * 1024 * 1024

    # 先读头部按魔数校验(Content-Type 可伪造),再交给存储层(本地流式 / 对象存储直传)
    head = file.file.read(1024 * 1024)
    file.file.seek(0)
    if not head:
        raise HTTPException(422, "上传的视频为空")
    if not media.sniff_video(head):
        raise HTTPException(422, "文件内容不是有效的视频(mp4/webm)")
    try:
        video_rel = storage.save_seekable("videos", file.file, ".mp4", user_id=user.id, max_bytes=limit)
    except media.MediaTooLarge:
        raise HTTPException(422, f"合成成片不能超过 {settings.max_video_upload_mb}MB") from None
    except media.MediaEmpty:
        raise HTTPException(422, "上传的视频为空") from None
    try:
        source_id_list = [int(x) for x in _json.loads(source_ids)][:50]
    except (ValueError, TypeError):
        source_id_list = []

    creation = Creation(
        user_id=user.id,
        input_text=(title or "").strip()[:2000] or "剪辑合成",
        style="",
        expanded_prompt=f"浏览器端合成 · {len(source_id_list)} 段素材",
        image_source="merged",
        duration=max(0, min(round(total_duration), 3600)),
        status="completed",
        config_name="",
        chat_model="",
        image_model="",
        video_model="",
    )
    creation.video_path = video_rel
    db.add(creation)
    db.commit()
    db.refresh(creation)
    return _to_out(creation)


@router.get("/creations", response_model=list[CreationOut])
def list_creations(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    creations = db.scalars(
        select(Creation)
        .where(Creation.user_id == user.id)
        .order_by(Creation.id.desc())
        .limit(min(limit, 200))
    ).all()
    project_ids = _vlog_project_ids_for_creations(db, user, [creation.id for creation in creations])
    return [_to_out(c, project_ids.get(c.id)) for c in creations]


@router.get("/creations/{creation_id}", response_model=CreationOut)
def get_creation(
    creation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    creation = _get_creation(db, user, creation_id)
    project_ids = _vlog_project_ids_for_creations(db, user, [creation.id])
    return _to_out(creation, project_ids.get(creation.id))


@router.delete("/creations/{creation_id}")
def delete_creation(
    creation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    creation = _get_creation(db, user, creation_id)
    db.delete(creation)
    db.commit()
    storage.delete(creation.image_path)
    storage.delete(creation.video_path)
    return {"ok": True}
