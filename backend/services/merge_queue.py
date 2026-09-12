"""服务端合成队列:用户提交入库,调度线程轮询认领,全局并发上限执行 FFmpeg。

与生成管线(pipeline/vlog_pipeline)的关键差异:
- 提交只落库立即返回(merge_status='queued'),并发控制收拢在调度循环,一处管全局
- 合成是本地计算、无计费:重启恢复时 running 一律重新排队重跑,不像网关任务要防重复提交
- 项目在合成期间保持 ready_to_merge(占住"唯一活跃项目"索引名额),merge_status
  单独表达排队/运行/失败;成功才转 completed,失败可重新提交

可靠性:worker 定期把进度与 updated_at 心跳落库;看门狗(pipeline.start_sweeper 调用
reap_stale_merges)回收超时无心跳的 running 任务并 terminate 进程;用户放弃项目时
cancel_merge 终止进程,worker 完成前复查项目状态,已取消则丢弃产物。
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import media, storage
from ..auth import aware
from ..database import SessionLocal
from ..models import Creation, VlogClip, VlogProject
from ..settings import settings
from . import video_merge
from .video_merge import MergeError, RenderItem
from .vlog_pipeline import MAX_VLOG_GROUPS
from .vlog_transitions import normalize_vlog_transition

logger = logging.getLogger(__name__)

MERGE_QUEUED = "queued"
MERGE_RUNNING = "running"
MERGE_FAILED = "failed"
MERGE_DONE = "done"

# 心跳/进度落库的最小间隔(秒),避免 ffmpeg 进度回调打爆数据库
PROGRESS_COMMIT_INTERVAL = 2.0
# 压缩/上传等无进度回调阶段的节拍心跳间隔(秒)
MERGE_HEARTBEAT_SECONDS = 15.0
# 看门狗缓冲:超过 合成超时 + 该缓冲 仍无心跳判死
STALE_BUFFER_SECONDS = 600.0

_active_lock = threading.Lock()
_processes: dict[int, subprocess.Popen] = {}
_active_count = 0


def cancel_merge(project_id: int) -> None:
    """用户放弃项目时终止进行中的合成;排队中的任务由 worker 启动时自查跳过。"""
    with _active_lock:
        process = _processes.pop(project_id, None)
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:  # noqa: BLE001 —— 终止失败就强杀,不能让僵尸进程占着并发名额
        logger.warning("terminate merge %s failed, killing", project_id, exc_info=True)
        process.kill()


def cancel_all_merges() -> None:
    """进程退出时终止本进程内运行中的合成并重新排队。

    本地计算无计费,重跑安全;先改库再 terminate,worker 事后无论走成功还是失败
    路径,条件更新(merge_status==running)都不再命中,不会覆盖排队状态。
    """
    with _active_lock:
        processes = list(_processes.items())
        _processes.clear()
    if not processes:
        return
    try:
        with SessionLocal() as session:
            session.execute(
                update(VlogProject)
                .where(
                    VlogProject.id.in_([project_id for project_id, _ in processes]),
                    VlogProject.merge_status == MERGE_RUNNING,
                )
                .values(
                    merge_status=MERGE_QUEUED,
                    merge_progress=0.0,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except SQLAlchemyError:
        logger.warning("requeue merges on shutdown failed", exc_info=True)
    for project_id, process in processes:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            logger.warning("terminate merge %s on shutdown failed", project_id, exc_info=True)
            process.kill()


# ---------------------------------------------------------------- 提交侧辅助


def finalize_vlog_merge(db: Session, project: VlogProject, video_path: str, actual_duration: float) -> Creation:
    """成片入库:建 merged 历史记录 + 项目终态。complete 端点与后台 worker 共用。"""
    creation = Creation(
        user_id=project.user_id,
        input_text=project.description or f"{project.style} Vlog",
        style=project.style,
        expanded_prompt=f"Vlog {len(project.image_paths)} 张图片自然转场合成",
        image_source="merged",
        image_path=project.image_paths[0] if project.image_paths else None,
        video_path=video_path,
        duration=max(1, min(round(actual_duration), 3600)),
        status="completed",
        config_id=project.config_id,
        config_name=project.config_name,
        video_model=project.video_model,
    )
    db.add(creation)
    db.flush()
    project.final_video_path = video_path
    project.final_creation_id = creation.id
    project.status = "completed"
    project.error = None
    project.merge_status = MERGE_DONE
    project.merge_progress = 1.0
    project.merge_error = None
    return creation


# ---------------------------------------------------------------- 恢复与看门狗


def recover_merge_jobs() -> int:
    """服务重启时 running 的合成一律重新排队(本地计算重跑安全,产物在临时目录已丢失)。"""
    with SessionLocal() as session:
        result = session.execute(
            update(VlogProject)
            .where(VlogProject.merge_status == MERGE_RUNNING)
            .values(merge_status=MERGE_QUEUED, merge_progress=0.0, updated_at=datetime.now(timezone.utc))
        )
        session.commit()
        return result.rowcount


def reap_stale_merges() -> int:
    """看门狗:回收长时间无心跳的合成任务;项目保持 ready_to_merge,用户可直接重试。"""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.merge_timeout_seconds + STALE_BUFFER_SECONDS
    )
    with SessionLocal() as session:
        rows = session.scalars(
            select(VlogProject).where(VlogProject.merge_status == MERGE_RUNNING)
        ).all()
        stale_ids = [row.id for row in rows if aware(row.updated_at) < cutoff]
        if not stale_ids:
            return 0
        result = session.execute(
            update(VlogProject)
            .where(VlogProject.id.in_(stale_ids), VlogProject.merge_status == MERGE_RUNNING)
            .values(
                merge_status=MERGE_FAILED,
                merge_progress=0.0,
                merge_error="合成任务长时间无进展,已终止;请重新提交",
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    for project_id in stale_ids:
        cancel_merge(project_id)
    return result.rowcount


# ---------------------------------------------------------------- 调度循环


def start_merge_dispatcher(stop_event: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop_event.wait(settings.merge_poll_interval):
            try:
                _dispatch_once()
            except Exception:  # noqa: BLE001 —— 调度线程自身不能倒
                logger.exception("merge dispatch failed")

    thread = threading.Thread(target=loop, daemon=True, name="merge-dispatcher")
    thread.start()
    return thread


def _dispatch_once() -> None:
    global _active_count
    with _active_lock:
        slots = settings.max_merge_workers - _active_count
    if slots <= 0:
        return
    with SessionLocal() as session:
        candidates = session.scalars(
            select(VlogProject.id)
            .where(VlogProject.merge_status == MERGE_QUEUED, VlogProject.status == "ready_to_merge")
            .order_by(VlogProject.id)
            .limit(slots)
        ).all()
        claimed: list[int] = []
        for project_id in candidates:
            # 条件更新防重复认领(当前单调度线程,主要为未来多进程部署留保险)
            result = session.execute(
                update(VlogProject)
                .where(VlogProject.id == project_id, VlogProject.merge_status == MERGE_QUEUED)
                .values(merge_status=MERGE_RUNNING, merge_progress=0.0, updated_at=datetime.now(timezone.utc))
            )
            if result.rowcount:
                claimed.append(project_id)
        session.commit()

    for project_id in claimed:
        with _active_lock:
            _active_count += 1
        threading.Thread(
            target=_run_worker, args=(project_id,), daemon=True, name=f"merge-{project_id}"
        ).start()


# ---------------------------------------------------------------- worker


def _run_worker(project_id: int) -> None:
    global _active_count
    try:
        with SessionLocal() as session:
            project = session.get(VlogProject, project_id)
            if project is None:
                return
            if project.status == "cancelled":
                _reset_merge_state(session, project)
                return
            if project.status != "ready_to_merge" or project.merge_status != MERGE_RUNNING:
                return
            try:
                _render_and_store(session, project)
            except Exception as exc:  # noqa: BLE001 —— 任何失败都要落库,不能卡住名额
                logger.exception("merge %s failed", project_id)
                session.rollback()
                fresh = session.get(VlogProject, project_id)
                # 项目已取消/看门狗已判死时不再覆盖状态
                if fresh is not None and fresh.status == "ready_to_merge" and fresh.merge_status == MERGE_RUNNING:
                    fresh.merge_status = MERGE_FAILED
                    fresh.merge_progress = 0.0
                    fresh.merge_error = _user_message(exc)
                    session.commit()
    finally:
        with _active_lock:
            _processes.pop(project_id, None)
            _active_count -= 1


def _user_message(exc: Exception) -> str:
    if isinstance(exc, MergeError):
        return f"合成失败:{exc}"
    return f"合成失败:内部错误 {exc.__class__.__name__}: {exc}"


def _reset_merge_state(session: Session, project: VlogProject) -> None:
    project.merge_status = None
    project.merge_progress = 0.0
    project.merge_error = None
    session.commit()


def _save_final(
    session: Session,
    project: VlogProject,
    out_path: Path,
    tmp: Path,
    on_progress: Callable[[float], None],
    on_process: Callable[[subprocess.Popen | None], None],
) -> str:
    """成片入库;超过大小上限时自动压缩重试(先降码率,再降分辨率),仍超限才失败。

    压缩与上传阶段没有进度回调,期间由节拍线程定期心跳保活(渲染阶段的心跳由进度回调
    承担);心跳带 merge_status=running 条件,任务被看门狗回收或取消后自动失效。
    """
    stop_beat = threading.Event()

    def _beat() -> None:
        while not stop_beat.wait(MERGE_HEARTBEAT_SECONDS):
            try:
                with SessionLocal() as beat_session:
                    beat_session.execute(
                        update(VlogProject)
                        .where(
                            VlogProject.id == project.id,
                            VlogProject.merge_status == MERGE_RUNNING,
                        )
                        .values(updated_at=datetime.now(timezone.utc))
                    )
                    beat_session.commit()
            except SQLAlchemyError:
                logger.warning("merge heartbeat failed (project %s)", project.id, exc_info=True)

    threading.Thread(
        target=_beat, daemon=True, name=f"merge-beat-{project.id}"
    ).start()
    try:
        return _do_save_final(project, out_path, tmp, on_progress, on_process)
    finally:
        stop_beat.set()


def _do_save_final(
    project: VlogProject,
    out_path: Path,
    tmp: Path,
    on_progress: Callable[[float], None],
    on_process: Callable[[subprocess.Popen | None], None],
) -> str:
    max_bytes = settings.max_video_upload_mb * 1024 * 1024

    def save(path: Path) -> str:
        with path.open("rb") as handle:
            return storage.save_seekable(
                "videos", handle, ".mp4", user_id=project.user_id, max_bytes=max_bytes
            )

    try:
        return save(out_path)
    except media.MediaTooLarge:
        logger.info(
            "merge %s output exceeds %sMB, recompressing", project.id, settings.max_video_upload_mb
        )

    compressed = tmp / "final-compressed.mp4"
    attempts = ({"crf": 30, "scale": 1.0}, {"crf": 32, "scale": 0.5})
    for attempt in attempts:
        video_merge.compress_video(
            out_path, compressed, on_progress=on_progress, on_process=on_process, **attempt
        )
        try:
            return save(compressed)
        except media.MediaTooLarge:
            continue
    raise MergeError(
        f"成片压缩后仍超过 {settings.max_video_upload_mb}MB 上限,请减少片段组或缩短时长"
    )


def _render_and_store(session: Session, project: VlogProject) -> None:
    clips = session.scalars(
        select(VlogClip).where(VlogClip.project_id == project.id).order_by(VlogClip.sequence)
    ).all()
    with tempfile.TemporaryDirectory(prefix="edream-merge-") as tmp_name:
        tmp = Path(tmp_name)
        items = _resolve_items(session, project, clips, tmp)
        last_commit = [0.0]
        last_progress = [0.0]

        def on_progress(value: float) -> None:
            value = min(1.0, max(0.0, value))
            # 压缩兜底阶段进度从头计,展示上只增不减,避免百分比回跳
            if value <= last_progress[0]:
                return
            now = time.monotonic()
            if now - last_commit[0] < PROGRESS_COMMIT_INTERVAL and value < 1.0:
                return
            last_commit[0] = now
            last_progress[0] = value
            try:
                project.merge_progress = round(value, 3)
                project.updated_at = datetime.now(timezone.utc)
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                logger.warning("merge progress commit failed (project %s)", project.id, exc_info=True)

        out_path = tmp / "final.mp4"
        expected = video_merge.render_vlog(
            items,
            out_path=out_path,
            ratio=project.ratio,
            resolution=project.resolution,
            transition_style=normalize_vlog_transition(project.transition_style),
            on_progress=on_progress,
            on_process=lambda process: _register(project.id, process),
        )
        video_path = _save_final(
            session, project, out_path, tmp, on_progress, lambda process: _register(project.id, process)
        )

    # 完成前复查:渲染期间用户可能已放弃项目,产物直接丢弃
    session.refresh(project)
    if project.status == "cancelled":
        storage.delete(video_path)
        _reset_merge_state(session, project)
        return

    # 条件更新防竞态:看门狗若已判死(merge_status 不再是 running),丢弃产物不覆盖
    result = session.execute(
        update(VlogProject)
        .where(VlogProject.id == project.id, VlogProject.merge_status == MERGE_RUNNING)
        .values(merge_status=MERGE_DONE)
    )
    if result.rowcount == 0:
        storage.delete(video_path)
        return
    session.refresh(project)
    finalize_vlog_merge(session, project, video_path, expected)
    session.commit()


def _register(project_id: int, process: subprocess.Popen | None) -> None:
    with _active_lock:
        if process is None:
            _processes.pop(project_id, None)
        else:
            _processes[project_id] = process


# ---------------------------------------------------------------- 素材解析


def _validate_key(key: str, user_id: int, what: str) -> None:
    if not media.is_safe_rel(key) or not key.startswith(f"users/{user_id}/"):
        raise MergeError(f"{what}不属于当前用户或路径不合法")


def _materialize(key: str, dest: Path, user_id: int, what: str) -> Path:
    """把存储 key 变成本地文件:本地后端直接复用原路径,COS 流式拉到临时目录。"""
    _validate_key(key, user_id, what)
    if not storage.use_cos():
        path = media.safe_abs_path(key)
        if path is None or not path.is_file():
            raise MergeError(f"{what}已被清理,请重新选择素材")
        return path
    try:
        with dest.open("wb") as handle:
            storage.download_to(key, handle)
    except Exception as exc:  # noqa: BLE001
        raise MergeError(f"{what}下载失败,请稍后重试") from exc
    return dest


def _materialize_video(
    key: str | None, remote_url: str | None, dest: Path, user_id: int, what: str
) -> Path:
    if key:
        return _materialize(key, dest, user_id, what)
    if remote_url:
        # 深度防御:URL 来自网关/上游响应,仍限制 http(s),防配置异常注入 file:// 等
        if not remote_url.lower().startswith(("http://", "https://")):
            raise MergeError(f"{what}的来源链接不合法")
        try:
            with httpx.stream("GET", remote_url, timeout=120.0, follow_redirects=True) as response:
                response.raise_for_status()
                with dest.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        handle.write(chunk)
        except Exception as exc:  # noqa: BLE001
            raise MergeError(f"{what}下载失败,请稍后重试") from exc
        return dest
    raise MergeError(f"{what}缺少可用文件")


def _backfill_clip_asset(session: Session, project: VlogProject, clip: VlogClip, local: Path) -> None:
    """片段此前只存了上游临时链接:把拉回的素材落存储并回填 key,链接过期不再影响后续合成。"""
    try:
        with local.open("rb") as handle:
            clip.video_path = storage.save_seekable(
                "videos",
                handle,
                ".mp4",
                user_id=project.user_id,
                max_bytes=settings.max_video_upload_mb * 1024 * 1024,
            )
        clip.video_url = None
        session.commit()
        logger.info("backfilled vlog clip %s into storage: %s", clip.id, clip.video_path)
    except Exception:  # noqa: BLE001 —— 回填失败只影响下次,不能挡住本次合成
        session.rollback()
        logger.warning("backfill vlog clip %s failed", clip.id, exc_info=True)


def _resolve_items(
    session: Session, project: VlogProject, clips: list[VlogClip], tmp: Path
) -> list[RenderItem]:
    """按时间线顺序把每个片段组解析成本地素材;来源全部校验属主与路径安全。"""
    timeline = project.timeline_data or []
    if not timeline:
        raise MergeError("项目时间线为空,无法合成")
    if len(timeline) > MAX_VLOG_GROUPS:
        raise MergeError(f"时间线超过 {MAX_VLOG_GROUPS} 个片段组,无法合成")

    ai_clips = list(clips)
    ai_index = 0
    items: list[RenderItem] = []
    for position, entry in enumerate(timeline, start=1):
        if not isinstance(entry, dict):
            raise MergeError(f"第 {position} 段时间线格式不合法")
        mode = entry.get("mode")
        if mode == "ai":
            if ai_index >= len(ai_clips):
                raise MergeError("时间线中的 AI 片段数量与实际片段不一致")
            clip = ai_clips[ai_index]
            ai_index += 1
            if clip.status != "completed" or not (clip.video_path or clip.video_url):
                raise MergeError(f"第 {position} 段 AI 片段尚未就绪,无法合成")
            remote_only = clip.video_path is None and bool(clip.video_url)
            path = _materialize_video(
                clip.video_path,
                clip.video_url,
                tmp / f"ai-{position:02d}.mp4",
                project.user_id,
                f"第 {position} 段 AI 片段",
            )
            if remote_only:
                _backfill_clip_asset(session, project, clip, path)
            items.append(RenderItem(kind="video", path=path))
        elif mode == "motion":
            image_path = entry.get("image_path")
            duration = entry.get("duration", 4)
            template = entry.get("motion_template", "kenburns_in")
            if not isinstance(image_path, str):
                raise MergeError(f"第 {position} 段图片动效缺少图片")
            if not isinstance(duration, (int, float)) or not 1 <= duration <= 60:
                raise MergeError(f"第 {position} 段图片动效时长不合法")
            if template not in video_merge.MOTION_TEMPLATES:
                raise MergeError(f"第 {position} 段图片动效模板不合法")
            image = _materialize(
                image_path, tmp / f"motion-src-{position:02d}.jpg", project.user_id, f"第 {position} 段动效图片"
            )
            items.append(
                RenderItem(kind="motion", path=image, duration=float(duration), motion_template=template)
            )
        elif mode == "video":
            source = entry.get("source")
            if source == "history":
                creation = session.get(Creation, entry.get("creation_id"))
                if creation is None or creation.user_id != project.user_id:
                    raise MergeError(f"第 {position} 段历史视频不可用")
                path = _materialize_video(
                    creation.video_path,
                    creation.video_url,
                    tmp / f"video-{position:02d}.mp4",
                    project.user_id,
                    f"第 {position} 段历史视频",
                )
            elif source == "upload":
                asset_path = entry.get("asset_path")
                if not isinstance(asset_path, str):
                    raise MergeError(f"第 {position} 段本地视频缺少已上传素材")
                path = _materialize(
                    asset_path, tmp / f"video-{position:02d}.mp4", project.user_id, f"第 {position} 段本地视频"
                )
            else:
                raise MergeError(f"第 {position} 段视频来源不合法")
            items.append(RenderItem(kind="video", path=path))
        else:
            raise MergeError(f"第 {position} 段时间线类型不认识")
    if ai_index != len(ai_clips):
        raise MergeError("时间线中的 AI 片段数量与实际片段不一致")
    return items
