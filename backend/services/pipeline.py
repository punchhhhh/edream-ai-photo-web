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

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .. import media, storage
from ..auth import aware
from ..database import SessionLocal
from ..models import Creation, EnterpriseVideoTemplate, ModelConfig, StylePreset
from ..settings import settings
from .ai_client import AICallError, AIClient
from .enterprise_access import refund_video_grant

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
    - 尚未提交成功的任务标记失败(共创任务返还次数),避免并发限制把用户永久卡死
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
                creation.error = (
                    "服务重启时任务尚未成功提交到网关,已终止;本次共创次数已自动返还,请重新提交"
                    if creation.enterprise_grant_id
                    else "服务重启时任务尚未成功提交到网关,已终止;请重新提交"
                )
                _refund_if_cocreation(session, creation)
                to_fail.append(creation.id)
        session.commit()
    for creation_id in to_resume:
        start_creation_thread(creation_id, resume=True)
    return {"resumed": len(to_resume), "failed": len(to_fail)}


def reap_stale_creations() -> int:
    """看门狗:回收长时间无心跳的生成中任务(共创任务回收时返还次数)。"""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.video_timeout_seconds + STALE_BUFFER_SECONDS
    )
    with SessionLocal() as session:
        rows = session.scalars(select(Creation).where(Creation.status.in_(ACTIVE_STATUSES))).all()
        stale = [c for c in rows if aware(c.updated_at) < cutoff]
        if not stale:
            return 0
        for creation in stale:
            creation.status = "failed"
            creation.error = (
                "任务长时间无进展,已被系统终止;本次共创次数已自动返还"
                if creation.enterprise_grant_id
                else "任务长时间无进展,已被系统终止;若网关侧仍在计费,请到网关后台核对"
            )
            try:
                _refund_if_cocreation(session, creation)
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                logger.exception("reap stale creation failed (creation %s)", creation.id)
        return len(stale)


def start_sweeper(stop_event: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop_event.wait(SWEEPER_INTERVAL_SECONDS):
            try:
                reap_stale_creations()
                # 共用一个轻量看门狗线程，避免为 Vlog 再维护一套生命周期。
                from .vlog_pipeline import reap_stale_vlogs

                reap_stale_vlogs()
                from .merge_queue import reap_stale_merges

                reap_stale_merges()
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


def _resolve_gateway(session, creation: Creation) -> tuple[str, str, str]:
    """解析生成视频所需的网关参数,返回 (base_url, api_key, provider)。

    共创任务(有 template_id)用企业主的默认模型配置(无默认配置时实时取
    其 new-api system 密钥);普通任务用用户自己的模型配置。
    """
    if creation.template_id:
        from .cocreation import enterprise_gateway_credentials

        template = session.get(EnterpriseVideoTemplate, creation.template_id)
        if template is None or template.enterprise_id != creation.enterprise_id:
            raise AICallError("企业视频模版不存在或已被删除,无法继续生成")
        base_url, api_key = enterprise_gateway_credentials(session, template.enterprise_id)
        return base_url, api_key, template.video_provider or "video_generations"

    config = session.get(ModelConfig, creation.config_id) if creation.config_id else None
    if config is None:
        raise AICallError("模型配置不存在或已被删除,请重新选择配置")
    return config.base_url, config.api_key, config.video_provider or "video_generations"


def _generate_video(session, creation: Creation, *, resume: bool = False) -> None:
    base_url, api_key, provider = _resolve_gateway(session, creation)

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

    # 共创附加参考图:首帧(image_path)打头,后续参考图按提交顺序一起传给视频模型;
    # 有参考图时走多图通道,首帧作为视频起点,其余作为中间画面/参考
    image_inputs: list[tuple[bytes, str]] | None = None
    if creation.reference_image_paths:
        image_inputs = []
        if image_bytes is not None:
            image_inputs.append((image_bytes, image_mime or "image/png"))
        for ref_path in creation.reference_image_paths:
            if not media.is_safe_rel(ref_path):
                raise AICallError("参考图路径不合法,请重新上传后再提交")
            try:
                image_inputs.append(
                    (
                        storage.read(ref_path),
                        IMAGE_MIME_BY_EXT.get(Path(ref_path).suffix.lower(), "image/png"),
                    )
                )
            except FileNotFoundError:
                raise AICallError("参考图文件不存在或已被清理,请重新上传后再提交") from None

    creation.status = "generating_video"
    creation.error = None
    session.commit()

    # 风格预设的负向提示词(正向画面语言已随拓展文本固化在 expanded_prompt 里);
    # 共创任务用模版配置的负向提示词
    negative_prompt = ""
    if creation.template_id:
        template = session.get(EnterpriseVideoTemplate, creation.template_id)
        if template is not None:
            negative_prompt = template.negative_prompt
    elif creation.style:
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

    with AIClient(base_url, api_key, provider=provider) as client:
        result = client.run_video(
            creation.video_model,
            creation.expanded_prompt or creation.input_text,
            image_bytes=image_bytes,
            image_mime=image_mime,
            image_inputs=image_inputs,
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


def _refund_if_cocreation(session, creation: Creation) -> None:
    """共创任务失败终态时返还消耗的次数;个人任务没有授权,直接跳过。

    返还失败只记日志不抛出,保证任务终态(failed)总能落库;流水幂等,
    后续路径(看门狗/重启恢复)不会重复返还。
    """
    if not creation.enterprise_grant_id:
        return
    try:
        refund_video_grant(
            session,
            grant_id=creation.enterprise_grant_id,
            creation_id=creation.id,
        )
    except Exception:  # noqa: BLE001 —— 返还失败不能挡住任务终态落库
        logger.exception("refund cocreation grant failed (creation %s)", creation.id)


def _finish(session, creation: Creation, status: str) -> None:
    # 回滚会一并撤销 error/返还,重试循环里都要重放(返还按流水幂等,重复调用安全)
    error = creation.error
    for attempt in (1, 2):
        try:
            if status == "failed":
                _refund_if_cocreation(session, creation)
            creation.status = status
            creation.error = error
            session.commit()
            return
        except SQLAlchemyError:
            session.rollback()
            logger.exception("持久化任务终态失败(creation %s,第 %s 次尝试)", creation.id, attempt)
    # 两次都失败:任务仍是进行中状态,交给看门狗按"无心跳超时"兜底回收
