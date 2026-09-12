"""Vlog 多片段生成管线：本地分组，Seedance 串行生成，浏览器负责最终合成。"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from .. import media, storage
from ..auth import aware
from ..database import SessionLocal
from ..models import ModelConfig, VlogClip, VlogProject
from ..settings import settings
from .ai_client import AICallError, AIClient

logger = logging.getLogger(__name__)

ACTIVE_VLOG_STATUSES = ("pending", "generating_video", "ready_to_merge")
IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
HEARTBEAT_INTERVAL_SECONDS = 30.0
STALE_BUFFER_SECONDS = 600.0
# 单个项目时间线的片段组上限,防止单条成片渲染时长与体积膨胀
MAX_VLOG_GROUPS = 20
# 上游视频下载重试次数;全部失败才退回远端链接(临时链接会过期,尽量落存储)
CLIP_DOWNLOAD_RETRIES = 3


def _download_clip_video(client: AIClient, url: str, clip_id: int) -> bytes | None:
    """把上游生成的视频拉回来入库;瞬时失败重试,全部失败返回 None。"""
    for attempt in range(1, CLIP_DOWNLOAD_RETRIES + 1):
        try:
            return client.download(url)
        except Exception:  # noqa: BLE001 —— 网关抖动不能直接让片段退回临时链接
            logger.warning(
                "download vlog clip failed (clip %s, attempt %s/%s)",
                clip_id,
                attempt,
                CLIP_DOWNLOAD_RETRIES,
                exc_info=True,
            )
            time.sleep(attempt * 2)
    return None


def _color_histogram(data: bytes) -> list[float]:
    with Image.open(BytesIO(data)) as source:
        histogram = source.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS).histogram()
    descriptor: list[float] = []
    for channel in range(3):
        values = histogram[channel * 256 : (channel + 1) * 256]
        descriptor.extend(sum(values[start : start + 16]) / 4096 for start in range(0, 256, 16))
    return descriptor


def plan_reference_groups(paths: list[str]) -> list[list[str]]:
    """按上传顺序分场景；边界在均匀切分附近选择颜色差异最大的位置。"""
    descriptors = [_color_histogram(storage.read(path)) for path in paths]
    distances = [
        math.sqrt(sum((a - b) ** 2 for a, b in zip(descriptors[i - 1], descriptors[i])))
        for i in range(1, len(paths))
    ]
    base_count = math.ceil(len(paths) / 3)
    abrupt_breaks = sum(distance >= 0.6 for distance in distances)
    group_count = min(5, len(paths), max(base_count, abrupt_breaks + 1))
    if group_count == 1:
        return [paths]

    boundaries: list[int] = []
    previous = 0
    for group_index in range(1, group_count):
        ideal = round(len(paths) * group_index / group_count)
        remaining_groups = group_count - group_index
        low = max(previous + 1, ideal - 1)
        high = min(len(paths) - remaining_groups, ideal + 1)
        boundary = max(range(low, high + 1), key=lambda value: distances[value - 1])
        boundaries.append(boundary)
        previous = boundary

    groups: list[list[str]] = []
    start = 0
    for boundary in boundaries + [len(paths)]:
        groups.append(paths[start:boundary])
        start = boundary
    return groups


def build_clip_prompt(
    *,
    sequence: int,
    style: str,
    style_description: str,
    description: str,
    ratio: str,
    duration: int,
    group_description: str = "",
) -> str:
    intent = group_description.strip() or description.strip() or "记录图片中的这次真实经历，不添加新的地点、人物或事件"
    phase = (
        "首段负责建立环境与人物关系，镜头从较宽的环境景别自然靠近主体"
        if sequence == 1
        else "承接上一段的运动方向与光线，从当前场景自然展开，并给下一段保留稳定的动作出口"
    )
    return (
        f"根据按时间顺序提供的多张参考图片，生成一段连续的真实旅行 Vlog。{phase}。"
        f"用户意图：{intent}。画面风格：{style or '写实纪录'}；{style_description}。"
        "保持参考图中的人物身份、服装、动物、环境、天气和时间一致；把相似照片理解为同一场景的连续观察，"
        "不要做照片轮播、拼贴、瞬间换脸或不合理形变。镜头运动克制，动作符合真实物理，前后帧可自然接续。"
        f"画幅 {ratio}，时长 {duration} 秒，720p，保留自然环境声，不要字幕、贴纸、Logo 或旁白。"
    )


def start_vlog_thread(project_id: int) -> None:
    threading.Thread(
        target=_run_vlog, args=(project_id,), daemon=True, name=f"vlog-{project_id}"
    ).start()


def _project_cancelled(session, project: VlogProject) -> bool:
    """放弃接口会把项目置为 cancelled；线程在每个片段前后复查，避免覆盖用户的决定。"""
    session.refresh(project)
    return project.status == "cancelled"


def _run_vlog(project_id: int) -> None:
    with SessionLocal() as session:
        project = session.get(VlogProject, project_id)
        if project is None:
            return
        clips = session.scalars(
            select(VlogClip).where(VlogClip.project_id == project.id).order_by(VlogClip.sequence)
        ).all()
        try:
            for clip in clips:
                if clip.status == "completed":
                    continue
                if _project_cancelled(session, project):
                    return
                _generate_clip(session, project, clip)
            if _project_cancelled(session, project):
                return
            project.status = "ready_to_merge"
            project.error = None
            session.commit()
        except AICallError as exc:
            clip.status = "failed"
            clip.error = str(exc)
            project.status = "failed"
            project.error = f"第 {clip.sequence} 段生成失败：{exc}"
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("vlog %s pipeline crashed", project_id)
            clip.status = "failed"
            clip.error = f"内部错误:{exc.__class__.__name__}: {exc}"
            project.status = "failed"
            project.error = clip.error
            session.commit()


def _generate_clip(session, project: VlogProject, clip: VlogClip) -> None:
    config = session.get(ModelConfig, project.config_id) if project.config_id else None
    if config is None:
        raise AICallError("模型配置不存在或已被删除，请重新创建 Vlog")

    image_inputs: list[tuple[bytes, str]] = []
    for path in clip.reference_paths:
        if not media.is_safe_rel(path):
            raise AICallError("参考图片路径不合法，请重新上传")
        try:
            data = storage.read(path)
        except FileNotFoundError:
            raise AICallError("参考图片已被清理，请重新上传") from None
        image_inputs.append((data, IMAGE_MIME_BY_EXT.get(Path(path).suffix.lower(), "image/jpeg")))

    project.status = "generating_video"
    project.error = None
    clip.status = "generating_video"
    clip.error = None
    session.commit()

    def persist_task_id(task_id: str) -> None:
        clip.video_task_id = task_id
        session.commit()

    last_heartbeat = [0.0]

    def heartbeat(_: str) -> None:
        now = time.monotonic()
        if now - last_heartbeat[0] < HEARTBEAT_INTERVAL_SECONDS:
            return
        last_heartbeat[0] = now
        try:
            timestamp = datetime.now(timezone.utc)
            project.updated_at = timestamp
            clip.updated_at = timestamp
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            logger.warning("vlog heartbeat commit failed (clip %s)", clip.id, exc_info=True)

    with AIClient(config.base_url, config.api_key, provider=config.video_provider) as client:
        result = client.run_video(
            project.video_model,
            clip.prompt,
            image_inputs=image_inputs,
            duration=clip.duration,
            ratio=project.ratio,
            resolution=project.resolution,
            generate_audio=True,
            poll_interval=settings.video_poll_interval,
            timeout_seconds=settings.video_timeout_seconds,
            on_progress=heartbeat,
            on_submitted=persist_task_id,
            resume_task_id=clip.video_task_id,
        )
        clip.video_task_id = result.get("task_id")
        url = result.get("url")
        content = result.get("content")
        if content:
            clip.video_path = storage.save_bytes(
                "videos", content, ".mp4", user_id=project.user_id
            )
            clip.video_url = None
        elif url:
            content = _download_clip_video(client, url, clip.id)
            if content is not None:
                clip.video_path = storage.save_bytes(
                    "videos", content, ".mp4", user_id=project.user_id
                )
                clip.video_url = None
            else:
                logger.warning("download vlog clip failed, keep remote url: %s", url)
                clip.video_path = None
                clip.video_url = url
        else:
            raise AICallError("视频生成完成但没有可用的视频地址")

    clip.status = "completed"
    clip.error = None
    session.commit()


def recover_interrupted_vlogs() -> dict[str, int]:
    """恢复已拿到远端任务号的片段；未确认提交的片段不重发，避免重复计费。"""
    resumed = failed = 0
    with SessionLocal() as session:
        projects = session.scalars(
            select(VlogProject).where(VlogProject.status.in_(("pending", "generating_video")))
        ).all()
        resume_ids: list[int] = []
        for project in projects:
            clip = session.scalars(
                select(VlogClip)
                .where(VlogClip.project_id == project.id, VlogClip.status != "completed")
                .order_by(VlogClip.sequence)
            ).first()
            if clip and clip.video_task_id:
                resume_ids.append(project.id)
                resumed += 1
            else:
                if clip:
                    clip.status = "failed"
                    clip.error = "服务重启时任务尚未确认提交，为避免重复计费已终止"
                project.status = "failed"
                project.error = "任务未确认提交，已终止；请手动重试一次"
                failed += 1
        session.commit()
    for project_id in resume_ids:
        start_vlog_thread(project_id)
    return {"resumed": resumed, "failed": failed}


def reap_stale_vlogs() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.video_timeout_seconds + STALE_BUFFER_SECONDS
    )
    with SessionLocal() as session:
        rows = session.scalars(
            select(VlogProject).where(VlogProject.status.in_(("pending", "generating_video")))
        ).all()
        stale_ids = [row.id for row in rows if aware(row.updated_at) < cutoff]
        if not stale_ids:
            return 0
        session.execute(
            update(VlogProject)
            .where(VlogProject.id.in_(stale_ids))
            .values(status="failed", error="任务长时间无进展，已终止；请核对网关账单后再重试")
        )
        session.execute(
            update(VlogClip)
            .where(
                VlogClip.project_id.in_(stale_ids),
                VlogClip.status.in_(("pending", "generating_video")),
            )
            .values(status="failed", error="任务长时间无进展，已终止")
        )
        session.commit()
        return len(stale_ids)
