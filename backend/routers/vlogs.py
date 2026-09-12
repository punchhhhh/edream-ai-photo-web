"""多图 Vlog：批量上传、动态多片段生成、手动重试与成片入库。"""

from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import media, storage
from ..database import get_db
from ..models import Creation, ModelConfig, StylePreset, User, VlogClip, VlogProject
from ..schemas import (
    VlogCreateIn,
    VlogClipOut,
    VlogImageOut,
    LocalVlogCreateIn,
    VlogPlanIn,
    VlogPlanClipOut,
    VlogPlanOut,
    VlogProjectOut,
    VlogUploadOut,
    VlogVideoAssetOut,
)
from ..services import merge_queue, video_merge
from ..services.vlog_pipeline import (
    ACTIVE_VLOG_STATUSES,
    MAX_VLOG_GROUPS,
    build_clip_prompt,
    plan_reference_groups,
    start_vlog_thread,
)
from ..services.vlog_transitions import normalize_vlog_transition
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
        timeline_data=_timeline_out(db, project.user_id, project.timeline_data),
        ratio=project.ratio,
        resolution=project.resolution,
        target_duration=project.target_duration,
        transition_style=project.transition_style,
        status=project.status,
        error=project.error,
        merge_status=project.merge_status,
        merge_progress=project.merge_progress or 0.0,
        merge_error=project.merge_error,
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


def _timeline_out(db: Session, user_id: int, timeline: list[dict]) -> list[dict]:
    """给持久化的素材路径补播放地址，浏览器不需要猜存储后端。"""
    history_ids = [item.get("creation_id") for item in timeline if item.get("source") == "history"]
    history = {
        row.id: row
        for row in db.scalars(
            select(Creation).where(
                Creation.user_id == user_id,
                Creation.id.in_([value for value in history_ids if isinstance(value, int)]),
            )
        )
    }
    result: list[dict] = []
    for item in timeline:
        output = dict(item)
        if isinstance(output.get("image_path"), str):
            output["image_url"] = storage.url(output["image_path"])
        if isinstance(output.get("asset_path"), str):
            output["video_url"] = storage.url(output["asset_path"])
        if output.get("source") == "history" and isinstance(output.get("creation_id"), int):
            creation = history.get(output["creation_id"])
            output["video_url"] = (storage.url(creation.video_path) or creation.video_url) if creation else None
            output["name"] = creation.input_text if creation else output.get("name", "历史视频")
        result.append(output)
    return result


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
    if not 1 <= len(files) <= 9:
        raise HTTPException(422, "一次请选择 1–9 张图片")

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


def _validate_video_path(user: User, path: str) -> None:
    if not media.is_safe_rel(path) or not path.startswith(f"users/{user.id}/videos/"):
        raise HTTPException(422, "视频不属于当前用户或路径不合法")
    if not storage.exists(path):
        raise HTTPException(422, "视频文件不存在，请重新上传")


def _normalize_timeline(db: Session, user: User, timeline: list[dict], *, allow_ai: bool = True) -> list[dict]:
    """只存可恢复的来源信息，不接受浏览器传入的 URL 或任意附加字段。"""
    result: list[dict] = []
    for position, item in enumerate(timeline, start=1):
        mode = item.get("mode") if isinstance(item, dict) else None
        group_id = item.get("id") if isinstance(item, dict) else None
        if mode not in ("ai", "motion", "video") or not isinstance(group_id, str) or not group_id:
            raise HTTPException(422, f"第 {position} 个时间线片段格式不合法")
        if mode == "ai":
            if not allow_ai:
                # 纯本地 Vlog 没有 AI 片段的生成管线，混入会产生无法合成的空片段。
                raise HTTPException(422, f"第 {position} 个片段是 AI 片段，纯本地 Vlog 不支持")
            result.append({
                "id": group_id,
                "mode": "ai",
                "description": str(item.get("description", ""))[:500],
            })
            continue
        if mode == "motion":
            image_path = item.get("image_path")
            if not isinstance(image_path, str):
                raise HTTPException(422, f"第 {position} 个图片动效缺少图片")
            _validate_image_paths(user, [image_path])
            duration = item.get("duration", 4)
            if not isinstance(duration, int) or not 3 <= duration <= 6:
                raise HTTPException(422, f"第 {position} 个图片动效时长不合法")
            template = item.get("motion_template", "kenburns_in")
            if template not in ("kenburns_in", "kenburns_out", "pan_left", "pan_right", "drift"):
                raise HTTPException(422, f"第 {position} 个图片动效模板不合法")
            result.append({"id": group_id, "mode": "motion", "image_path": image_path, "duration": duration, "motion_template": template})
            continue

        source = item.get("source")
        duration = item.get("duration", 5)
        if not isinstance(duration, (int, float)) or not 1 <= duration <= 3600:
            raise HTTPException(422, f"第 {position} 个视频时长不合法")
        if source == "history":
            creation_id = item.get("creation_id")
            creation = db.get(Creation, creation_id) if isinstance(creation_id, int) else None
            if creation is None or creation.user_id != user.id or not (creation.video_path or creation.video_url):
                raise HTTPException(422, f"第 {position} 个历史视频不可用")
            result.append({"id": group_id, "mode": "video", "source": "history", "creation_id": creation.id, "duration": duration})
            continue
        if source == "upload":
            asset_path = item.get("asset_path")
            if not isinstance(asset_path, str):
                raise HTTPException(422, f"第 {position} 个本地视频缺少已上传素材")
            _validate_video_path(user, asset_path)
            result.append({
                "id": group_id,
                "mode": "video",
                "source": "upload",
                "asset_path": asset_path,
                "duration": duration,
                "name": str(item.get("name", "本地视频"))[:200],
                "mime": str(item.get("mime", "video/mp4"))[:100],
            })
            continue
        raise HTTPException(422, f"第 {position} 个视频来源不合法")
    return result


@router.post("/vlogs/assets/video", response_model=VlogVideoAssetOut)
def upload_vlog_video_asset(
    file: UploadFile = File(...), user: User = Depends(get_current_user)
):
    if not (file.content_type or "").lower().startswith("video/"):
        raise HTTPException(422, "仅支持视频文件")
    head = file.file.read(1024 * 1024)
    file.file.seek(0)
    if not head or not media.sniff_video(head):
        raise HTTPException(422, "文件内容不是有效的视频(mp4/webm)")
    ext = ".webm" if head.startswith(b"\x1a\x45\xdf\xa3") else ".mp4"
    try:
        path = storage.save_seekable(
            "videos", file.file, ext, user_id=user.id, max_bytes=settings.max_video_upload_mb * 1024 * 1024
        )
    except media.MediaTooLarge:
        raise HTTPException(422, f"视频不能超过 {settings.max_video_upload_mb}MB") from None
    return VlogVideoAssetOut(asset_path=path, url=storage.url(path) or "")


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


@router.post("/vlogs/local", response_model=VlogProjectOut)
def create_local_vlog(
    payload: LocalVlogCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    active = db.scalars(
        select(VlogProject)
        .where(VlogProject.user_id == user.id, VlogProject.status.in_(ACTIVE_VLOG_STATUSES))
        .order_by(VlogProject.id.desc())
    ).first()
    if active is not None:
        raise HTTPException(409, f"已有 Vlog 正在处理（项目 {active.id}）")
    try:
        transition_style = normalize_vlog_transition(payload.transition_style)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    timeline = _normalize_timeline(db, user, payload.timeline_data, allow_ai=False)
    image_paths = [item["image_path"] for item in timeline if item["mode"] == "motion"]
    target_duration = round(sum(float(item.get("duration", 5)) for item in timeline))
    project = VlogProject(
        user_id=user.id,
        description=payload.description.strip(),
        style=payload.style.strip() or "写实纪录",
        image_paths=image_paths,
        timeline_data=timeline,
        ratio=payload.ratio,
        resolution="720p",
        target_duration=max(1, target_duration),
        transition_style=transition_style,
        status="ready_to_merge",
    )
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "已有 Vlog 正在处理，请勿重复提交") from None
    db.refresh(project)
    return _project_out(db, project)


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
    try:
        transition_style = normalize_vlog_transition(payload.transition_style)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    preset = db.scalar(select(StylePreset).where(StylePreset.name == style))
    style_description = preset.description if preset else "自然光、真实质感、克制运镜"
    ratio = payload.ratio or _infer_ratio(payload.image_paths)
    groups = payload.image_groups or plan_reference_groups(payload.image_paths)
    if len(groups) > MAX_VLOG_GROUPS:
        raise HTTPException(422, f"最多支持 {MAX_VLOG_GROUPS} 个 AI 片段组，请合并或删减")
    if payload.image_groups is not None:
        flattened = [path for group in payload.image_groups for path in group]
        if flattened != payload.image_paths:
            raise HTTPException(422, "图片分组必须覆盖全部图片且保持时间线顺序")
        if any(not 1 <= len(group) <= 9 for group in payload.image_groups):
            raise HTTPException(422, "每个 AI 图生视频组需要 1–9 张图片")
        if not payload.image_groups:
            raise HTTPException(422, "至少需要一个 AI 图生视频组")
        if payload.image_group_descriptions is not None and len(payload.image_group_descriptions) != len(payload.image_groups):
            raise HTTPException(422, "每个 AI 图生视频组都需要对应一段描述")
    elif payload.image_group_descriptions is not None:
        raise HTTPException(422, "没有图片分组时不能传入组描述")
    group_descriptions = payload.image_group_descriptions or [""] * len(groups)
    target_duration = sum(5 if len(group) == 1 else 10 for group in groups)
    timeline = _normalize_timeline(
        db,
        user,
        payload.timeline_data
        or [
            {"id": f"ai-{sequence}", "mode": "ai", "description": group_descriptions[sequence - 1]}
            for sequence in range(1, len(groups) + 1)
        ],
    )
    if sum(item["mode"] == "ai" for item in timeline) != len(groups):
        raise HTTPException(422, "时间线中的 AI 片段数量必须与图片分组一致")
    project = VlogProject(
        user_id=user.id,
        description=payload.description.strip(),
        style=style,
        image_paths=payload.image_paths,
        timeline_data=timeline,
        ratio=ratio,
        resolution="720p",
        target_duration=target_duration,
        transition_style=transition_style,
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
                    group_description=group_descriptions[sequence - 1],
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


@router.get("/vlogs/latest", response_model=VlogProjectOut | None)
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
        # 空历史是首次使用时的正常状态，不应被浏览器记录为请求错误。
        return None
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


def _require_mergeable(db: Session, project: VlogProject) -> None:
    """合成前置校验:项目就绪、片段齐全、时间线非空。"""
    if project.status != "ready_to_merge":
        raise HTTPException(409, "片段尚未全部生成完成")
    clips = db.scalars(select(VlogClip).where(VlogClip.project_id == project.id)).all()
    if any(clip.status != "completed" for clip in clips):
        raise HTTPException(409, "片段尚未全部生成完成")
    if not project.timeline_data:
        raise HTTPException(409, "项目时间线为空,无法合成")


@router.post("/vlogs/{project_id}/merge", response_model=VlogProjectOut)
def submit_vlog_merge(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """提交服务端合成:只入队立即返回,调度线程按全局并发上限认领执行。"""
    project = _get_project(db, user, project_id)
    _require_mergeable(db, project)
    if project.merge_status in (merge_queue.MERGE_QUEUED, merge_queue.MERGE_RUNNING):
        state = "排队中" if project.merge_status == merge_queue.MERGE_QUEUED else "合成中"
        raise HTTPException(409, f"该项目已在合成({state}),请勿重复提交")
    if not video_merge.ffmpeg_available():
        raise HTTPException(503, "服务端未安装 FFmpeg,暂时无法合成;请联系管理员")

    project.merge_status = merge_queue.MERGE_QUEUED
    project.merge_progress = 0.0
    project.merge_error = None
    db.commit()
    db.refresh(project)
    return _project_out(db, project)


@router.post("/vlogs/{project_id}/complete", response_model=VlogProjectOut)
def complete_vlog(
    project_id: int,
    file: UploadFile = File(...),
    actual_duration: float = Form(29),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """接收浏览器(FFmpeg.wasm)合成好的成片;前端已切换到服务端合成,保留兼容旧客户端。"""
    project = _get_project(db, user, project_id)
    _require_mergeable(db, project)
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

    try:
        merge_queue.finalize_vlog_merge(db, project, video_path, actual_duration)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(video_path)
        raise
    db.refresh(project)
    return _project_out(db, project)


@router.post("/vlogs/{project_id}/abandon", response_model=VlogProjectOut)
def abandon_vlog(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用户主动放弃未完成的项目；放弃后不再占用「唯一活跃项目」名额，可以新建。"""
    project = _get_project(db, user, project_id)
    if project.status not in ACTIVE_VLOG_STATUSES:
        raise HTTPException(409, "项目已结束，无需放弃")
    # 服务端合成一并取消:排队中的由 worker 启动时自查跳过,运行中的 terminate 进程
    if project.merge_status in (merge_queue.MERGE_QUEUED, merge_queue.MERGE_RUNNING):
        merge_queue.cancel_merge(project.id)
        project.merge_status = None
        project.merge_progress = 0.0
        project.merge_error = None
    project.status = "cancelled"
    project.error = "用户已放弃该项目"
    db.execute(
        update(VlogClip)
        .where(
            VlogClip.project_id == project.id,
            VlogClip.status.in_(("pending", "generating_video")),
        )
        .values(status="cancelled", error="项目已放弃")
    )
    db.commit()
    db.refresh(project)
    return _project_out(db, project)
