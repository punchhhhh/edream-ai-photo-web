"""视频生成后台管线:提交任务后由独立线程执行,前端轮询状态。

可靠性设计:
- 提交到网关拿到 task_id 后立即落库(on_submitted),进程重启凭它恢复轮询,不重复提交、不浪费已扣费的任务
- 轮询期间定期心跳(更新 updated_at),配合看门狗线程回收卡死任务,避免用户被单任务限制永久锁死
- 查询任务状态允许瞬时失败,由 AIClient.run_video 的连续失败计数兜底
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from .. import media, storage
from ..auth import aware
from ..database import SessionLocal
from ..models import Creation, ModelConfig, StylePreset
from ..settings import settings
from .ai_client import AICallError, AIClient

logger = logging.getLogger(__name__)

# 视频仍在进行中的状态;同一用户存在这些状态的任务时,后端拒绝新任务
ACTIVE_STATUSES = ("pending", "generating_video")

# 图生视频时参考图的 MIME 映射(按存储 key 后缀判断)
IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 心跳间隔(秒):轮询期间刷新 updated_at,证明线程还活着
HEARTBEAT_INTERVAL_SECONDS = 30.0
# 看门狗:超过 任务超时 + 该缓冲 仍无心跳的任务标记失败
STALE_BUFFER_SECONDS = 600.0
# 看门狗巡检间隔(秒)
SWEEPER_INTERVAL_SECONDS = 60.0


def recover_interrupted_creations() -> dict[str, int]:
    """服务重启后的恢复:
    - 已提交到网关(有 video_task_id)的任务恢复轮询,不再直接判死
    - 尚未提交成功的任务标记失败,避免并发限制把用户永久卡死
    """
    with SessionLocal() as session:
        stmt = select(Creation).where(Creation.status.in_(ACTIVE_STATUSES))
        if session.get_bind().dialect.name == "postgresql":
            # 多 worker 同时启动时各自认领不相交的任务集;SQLite(测试)不支持该语法
            stmt = stmt.with_for_update(skip_locked=True)
        rows = session.scalars(stmt).all()
        to_fail: list[int] = []
        to_resume: list[int] = []
        for creation in rows:
            if creation.video_task_id:
                to_resume.append(creation.id)
            else:
                creation.status = "failed"
                creation.error = "服务重启时任务尚未成功提交到网关,已终止;请重新提交"
                to_fail.append(creation.id)
        session.commit()
    for creation_id in to_resume:
        start_creation_thread(creation_id, resume=True)
    return {"resumed": len(to_resume), "failed": len(to_fail)}


def reap_stale_creations() -> int:
    """看门狗:回收长时间无心跳的生成中任务。"""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.video_timeout_seconds + STALE_BUFFER_SECONDS
    )
    with SessionLocal() as session:
        rows = session.scalars(select(Creation).where(Creation.status.in_(ACTIVE_STATUSES))).all()
        stale_ids = [c.id for c in rows if aware(c.updated_at) < cutoff]
        if not stale_ids:
            return 0
        session.execute(
            update(Creation)
            .where(Creation.id.in_(stale_ids))
            .values(
                status="failed",
                error="任务长时间无进展,已被系统终止;若网关侧仍在计费,请到网关后台核对",
            )
        )
        session.commit()
        return len(stale_ids)


def start_sweeper(stop_event: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop_event.wait(SWEEPER_INTERVAL_SECONDS):
            try:
                reap_stale_creations()
                # 共用一个轻量看门狗线程，避免为 Vlog 再维护一套生命周期。
                from .vlog_pipeline import reap_stale_vlogs

                reap_stale_vlogs()
            except Exception:  # noqa: BLE001 —— 看门狗自身不能倒
                logger.exception("reap stale creations failed")

    thread = threading.Thread(target=loop, daemon=True, name="creation-sweeper")
    thread.start()
    return thread


def start_creation_thread(creation_id: int, *, resume: bool = False) -> None:
    threading.Thread(
        target=_run, args=(creation_id, resume), daemon=True, name=f"creation-{creation_id}"
    ).start()


def _run(creation_id: int, resume: bool) -> None:
    with SessionLocal() as session:
        creation = session.get(Creation, creation_id)
        if creation is None:
            return
        try:
            _generate_video(session, creation, resume=resume)
        except AICallError as e:
            creation.error = str(e)
            _finish(session, creation, "failed")
        except Exception as e:  # noqa: BLE001 —— 后台线程兜底,任何异常都要落库
            logger.exception("creation %s pipeline crashed", creation_id)
            creation.error = f"内部错误:{e.__class__.__name__}: {e}"
            _finish(session, creation, "failed")


def _generate_video(session, creation: Creation, *, resume: bool = False) -> None:
    config = session.get(ModelConfig, creation.config_id) if creation.config_id else None
    if config is None:
        raise AICallError("模型配置不存在或已被删除,请重新选择配置")

    image_bytes = None
    image_mime = None
    if creation.image_path:
        if not media.is_safe_rel(creation.image_path):
            raise AICallError("图片文件路径不合法,请重新生成或上传后再提交")
        try:
            # 从存储层(本地磁盘/对象存储)读回参考图字节,转发给网关
            image_bytes = storage.read(creation.image_path)
        except FileNotFoundError:
            raise AICallError("图片文件不存在或已被清理,请重新生成或上传后再提交") from None
        image_mime = IMAGE_MIME_BY_EXT.get(Path(creation.image_path).suffix.lower(), "image/png")

    creation.status = "generating_video"
    creation.error = None
    session.commit()

    # 风格预设的负向提示词(正向画面语言已随拓展文本固化在 expanded_prompt 里)
    negative_prompt = ""
    if creation.style:
        preset = session.scalar(select(StylePreset).where(StylePreset.name == creation.style))
        if preset is not None:
            negative_prompt = preset.negative_prompt

    def persist_task_id(task_id: str) -> None:
        # 拿到远端任务号立即落库:此后进程重启也能恢复,不浪费已扣费的任务
        creation.video_task_id = task_id
        session.commit()

    last_heartbeat = [0.0]

    def heartbeat(_: str) -> None:
        now = time.monotonic()
        if now - last_heartbeat[0] < HEARTBEAT_INTERVAL_SECONDS:
            return
        last_heartbeat[0] = now
        try:
            creation.updated_at = datetime.now(timezone.utc)
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            logger.warning("heartbeat commit failed (creation %s)", creation.id, exc_info=True)

    with AIClient(config.base_url, config.api_key, provider=config.video_provider) as client:
        result = client.run_video(
            creation.video_model,
            creation.expanded_prompt or creation.input_text,
            image_bytes=image_bytes,
            image_mime=image_mime,
            negative_prompt=negative_prompt,
            duration=creation.duration,
            poll_interval=settings.video_poll_interval,
            timeout_seconds=settings.video_timeout_seconds,
            on_progress=heartbeat,
            on_submitted=persist_task_id,
            resume_task_id=creation.video_task_id if resume else None,
        )
        creation.video_task_id = result.get("task_id")

        url = result.get("url")
        content = result.get("content")
        if content:
            creation.video_path = storage.save_bytes("videos", content, ".mp4", user_id=creation.user_id)
            creation.video_url = None
        elif url:
            # 优先把远端视频拉回本地存储,失败则把远端链接存进 video_url 兜底
            try:
                creation.video_path = storage.save_bytes(
                    "videos", client.download(url), ".mp4", user_id=creation.user_id
                )
                creation.video_url = None
            except Exception:
                logger.warning("download remote video failed, keep remote url: %s", url, exc_info=True)
                creation.video_path = None
                creation.video_url = url
        else:
            raise AICallError("视频生成完成但没有可用的视频地址")

    _finish(session, creation, "completed")


def _finish(session, creation: Creation, status: str) -> None:
    for attempt in (1, 2):
        try:
            creation.status = status
            session.commit()
            return
        except SQLAlchemyError:
            session.rollback()
            logger.exception("持久化任务终态失败(creation %s,第 %s 次尝试)", creation.id, attempt)
    # 两次都失败:任务仍是进行中状态,交给看门狗按"无心跳超时"兜底回收
