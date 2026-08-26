"""多图 Vlog：批量上传、动态多片段生成、手动重试与成片入库。"""

from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import media, storage
from ..database import get_db
from ..models import Creation, ModelConfig, StylePreset, User, VlogClip, VlogProject
from ..schemas import (
    VlogCreateIn,
    VlogClipOut,
    VlogImageOut,
    VlogPlanIn,
    VlogPlanClipOut,
    VlogPlanOut,
    VlogProjectOut,
    VlogUploadOut,
)
from ..services.vlog_pipeline import (
    ACTIVE_VLOG_STATUSES,
    build_clip_prompt,
    plan_reference_groups,
    start_vlog_thread,
)
from ..settings import settings
from .auth import get_current_user

router = APIRouter(tags=["vlogs"])


def _get_project(db: Session, user: User, project_id: int) -> VlogProject:
    project = db.get(VlogProject, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(404, f"Vlog 项目 {project_id} 不存在")
    return project


def _project_out(db: Session, project: VlogProject) -> VlogProjectOut:
    clips = db.scalars(
        select(VlogClip).where(VlogClip.project_id == project.id).order_by(VlogClip.sequence)
    ).all()
    return VlogProjectOut(
        id=project.id,
        description=project.description,
        style=project.style,
        image_paths=project.image_paths,
        image_urls=[storage.url(path) or "" for path in project.image_paths],
        ratio=project.ratio,
        resolution=project.resolution,
        target_duration=project.target_duration,
        status=project.status,
        error=project.error,
        final_video_url=storage.url(project.final_video_path),
        final_creation_id=project.final_creation_id,
        config_name=project.config_name,
        video_model=project.video_model,
        clips=[
            VlogClipOut(
                id=clip.id,
                sequence=clip.sequence,
                reference_paths=clip.reference_paths,
                reference_urls=[storage.url(path) or "" for path in clip.reference_paths],
                duration=clip.duration,
                status=clip.status,
                error=clip.error,
                video_url=storage.url(clip.video_path) or clip.video_url,
                retry_count=clip.retry_count,
            )
            for clip in clips
        ],
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _normalize_image(data: bytes) -> tuple[bytes, int, int]:
    try:
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail(
                (settings.vlog_image_long_edge, settings.vlog_image_long_edge),
                Image.Resampling.LANCZOS,
            )
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                image = background.convert("RGB")
            else:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, "JPEG", quality=88, optimize=True, progressive=True)
            return output.getvalue(), image.width, image.height
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "文件内容不是可处理的 PNG / JPEG / WebP 图片") from exc


@router.post("/vlogs/upload", response_model=VlogUploadOut)
def upload_vlog_images(
    files: list[UploadFile] = File(...), user: User = Depends(get_current_user)
):
    if not 2 <= len(files) <= 9:
        raise HTTPException(422, "一次请选择 2–9 张图片")

    per_file_limit = settings.max_upload_mb * 1024 * 1024
    total_limit = settings.max_vlog_upload_total_mb * 1024 * 1024
    total = 0
    normalized: list[tuple[bytes, int, int]] = []
    allowed_types = {"image/png", "image/jpeg", "image/webp"}
    for file in files:
        if (file.content_type or "").lower() not in allowed_types:
            raise HTTPException(422, "Vlog 仅支持 PNG / JPEG / WebP 图片")
        data = file.file.read(per_file_limit + 1)
        if not data:
            raise HTTPException(422, "上传的图片为空")
        if len(data) > per_file_limit:
            raise HTTPException(422, f"单张图片不能超过 {settings.max_upload_mb}MB")
        total += len(data)
        if total > total_limit:
            raise HTTPException(422, f"图片总大小不能超过 {settings.max_vlog_upload_total_mb}MB")
        if media.sniff_image(data) not in (".png", ".jpg", ".webp"):
            raise HTTPException(422, "文件内容不是有效的 PNG / JPEG / WebP 图片")
        normalized.append(_normalize_image(data))

    saved: list[str] = []
    try:
        result: list[VlogImageOut] = []
        for order, (data, width, height) in enumerate(normalized):
            path = storage.save_bytes("uploads", data, ".jpg", user_id=user.id)
            saved.append(path)
            result.append(
                VlogImageOut(
                    image_path=path,
                    url=storage.url(path) or "",
                    width=width,
                    height=height,
                    order=order,
                )
            )
        groups = plan_reference_groups([item.image_path for item in result])
        durations = [5 if len(group) == 1 else 10 for group in groups]
        return VlogUploadOut(
            images=result,
            ratio=_infer_ratio([item.image_path for item in result]),
            target_duration=sum(durations),
            clips=[
                VlogPlanClipOut(reference_paths=group, duration=duration)
                for group, duration in zip(groups, durations)
            ],
        )
    except Exception:
        for path in saved:
            storage.delete(path)
        raise


def _infer_ratio(paths: list[str]) -> str:
    portrait = 0
    landscape = 0
    for path in paths:
        with Image.open(BytesIO(storage.read(path))) as image:
            if image.height > image.width:
                portrait += 1
            elif image.width > image.height:
                landscape += 1
    return "9:16" if portrait > landscape else "16:9"


def _validate_image_paths(user: User, paths: list[str]) -> None:
    if len(set(paths)) != len(paths):
        raise HTTPException(422, "请勿重复选择同一张图片")
    owner_prefix = f"users/{user.id}/uploads/"
    for path in paths:
        if not media.is_safe_rel(path) or not path.startswith(owner_prefix):
            raise HTTPException(422, "图片不属于当前用户或路径不合法")
        if not storage.exists(path):
            raise HTTPException(422, "图片文件不存在，请重新上传")


def _plan(paths: list[str]) -> VlogPlanOut:
    groups = plan_reference_groups(paths)
    durations = [5 if len(group) == 1 else 10 for group in groups]
    return VlogPlanOut(
        ratio=_infer_ratio(paths),
        target_duration=sum(durations),
        clips=[
            VlogPlanClipOut(reference_paths=group, duration=duration)
            for group, duration in zip(groups, durations)
        ],
    )


@router.post("/vlogs/plan", response_model=VlogPlanOut)
def plan_vlog(payload: VlogPlanIn, user: User = Depends(get_current_user)):
    _validate_image_paths(user, payload.image_paths)
    return _plan(payload.image_paths)


@router.post("/vlogs", response_model=VlogProjectOut)
def create_vlog(
    payload: VlogCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    config = db.get(ModelConfig, payload.config_id)
    if config is None or config.user_id != user.id:
        raise HTTPException(404, f"模型配置 {payload.config_id} 不存在")
    if "seedance-2" not in config.video_model.lower():
        raise HTTPException(422, "Vlog 当前需要选择 Seedance 2.0 视频模型")
    if config.video_provider != "video_generations":
        raise HTTPException(422, "Seedance 2.0 Vlog 需要 video_generations 接口")
    _validate_image_paths(user, payload.image_paths)

    active = db.scalars(
        select(VlogProject)
        .where(VlogProject.user_id == user.id, VlogProject.status.in_(ACTIVE_VLOG_STATUSES))
        .order_by(VlogProject.id.desc())
    ).first()
    if active is not None:
        raise HTTPException(409, f"已有 Vlog 正在处理（项目 {active.id}）")

    style = payload.style.strip() or "写实纪录"
    preset = db.scalar(select(StylePreset).where(StylePreset.name == style))
    style_description = preset.description if preset else "自然光、真实质感、克制运镜"
    ratio = _infer_ratio(payload.image_paths)
    groups = plan_reference_groups(payload.image_paths)
    target_duration = sum(5 if len(group) == 1 else 10 for group in groups)
    project = VlogProject(
        user_id=user.id,
        description=payload.description.strip(),
        style=style,
        image_paths=payload.image_paths,
        ratio=ratio,
        resolution="720p",
        target_duration=target_duration,
        status="pending",
        config_id=config.id,
        config_name=config.name,
        video_model=config.video_model,
        video_provider=config.video_provider,
    )
    db.add(project)
    db.flush()
    for sequence, paths in enumerate(groups, start=1):
        clip_duration = 5 if len(paths) == 1 else 10
        db.add(
            VlogClip(
                project_id=project.id,
                sequence=sequence,
                reference_paths=paths,
                prompt=build_clip_prompt(
                    sequence=sequence,
                    style=style,
                    style_description=style_description,
                    description=project.description,
                    ratio=ratio,
                    duration=clip_duration,
                ),
                duration=clip_duration,
                status="pending",
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "已有 Vlog 正在处理，请勿重复提交") from None
    db.refresh(project)
    start_vlog_thread(project.id)
    return _project_out(db, project)


@router.get("/vlogs/latest", response_model=VlogProjectOut)
def get_latest_vlog(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = db.scalars(
        select(VlogProject)
        .where(VlogProject.user_id == user.id)
        .order_by(VlogProject.id.desc())
    ).first()
    if project is None:
        raise HTTPException(404, "暂无 Vlog 项目")
    return _project_out(db, project)


@router.get("/vlogs/{project_id}", response_model=VlogProjectOut)
def get_vlog(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _project_out(db, _get_project(db, user, project_id))


@router.post("/vlogs/{project_id}/clips/{clip_id}/retry", response_model=VlogProjectOut)
def retry_vlog_clip(
    project_id: int,
    clip_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _get_project(db, user, project_id)
    clip = db.get(VlogClip, clip_id)
    if clip is None or clip.project_id != project.id:
        raise HTTPException(404, f"Vlog 片段 {clip_id} 不存在")
    if clip.status != "failed":
        raise HTTPException(409, "只有失败的片段可以重试")
    if clip.retry_count >= 1:
        raise HTTPException(409, "该片段已使用过一次手动重试")
    other_active = db.scalars(
        select(VlogProject).where(
            VlogProject.user_id == user.id,
            VlogProject.id != project.id,
            VlogProject.status.in_(ACTIVE_VLOG_STATUSES),
        )
    ).first()
    if other_active is not None:
        raise HTTPException(409, f"已有 Vlog 正在处理（项目 {other_active.id}）")

    clip.retry_count += 1
    clip.status = "pending"
    clip.error = None
    clip.video_task_id = None
    clip.video_path = None
    clip.video_url = None
    project.status = "pending"
    project.error = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "已有 Vlog 正在处理") from None
    start_vlog_thread(project.id)
    return _project_out(db, project)


@router.post("/vlogs/{project_id}/complete", response_model=VlogProjectOut)
def complete_vlog(
    project_id: int,
    file: UploadFile = File(...),
    actual_duration: int = Form(29),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _get_project(db, user, project_id)
    if project.status != "ready_to_merge":
        raise HTTPException(409, "两个片段尚未全部生成完成")
    if not (file.content_type or "").lower().startswith("video/"):
        raise HTTPException(422, "仅支持视频文件")
    head = file.file.read(1024 * 1024)
    file.file.seek(0)
    if not head or not media.sniff_video(head):
        raise HTTPException(422, "文件内容不是有效的视频(mp4/webm)")
    try:
        video_path = storage.save_seekable(
            "videos",
            file.file,
            ".mp4",
            user_id=user.id,
            max_bytes=settings.max_video_upload_mb * 1024 * 1024,
        )
    except media.MediaTooLarge:
        raise HTTPException(422, f"Vlog 成片不能超过 {settings.max_video_upload_mb}MB") from None

    creation = Creation(
        user_id=user.id,
        input_text=project.description or f"{project.style} Vlog",
        style=project.style,
        expanded_prompt=f"Vlog {len(project.image_paths)} 张图片自然转场合成",
        image_source="merged",
        image_path=project.image_paths[0] if project.image_paths else None,
        video_path=video_path,
        duration=max(1, min(actual_duration, 3600)),
        status="completed",
        config_id=project.config_id,
        config_name=project.config_name,
        video_model=project.video_model,
    )
    db.add(creation)
    try:
        db.flush()
        project.final_video_path = video_path
        project.final_creation_id = creation.id
        project.status = "completed"
        project.error = None
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(video_path)
        raise
    db.refresh(project)
    return _project_out(db, project)
