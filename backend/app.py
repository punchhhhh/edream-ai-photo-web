import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import media, storage
from .database import init_db
from .routers import auth, configs, creations, styles, vlogs
from .schemas import HealthOut
from .services import merge_queue, video_merge
from .services.merge_queue import recover_merge_jobs, start_merge_dispatcher
from .services.pipeline import recover_interrupted_creations, start_sweeper
from .services.vlog_pipeline import recover_interrupted_vlogs
from .settings import settings
from .style_presets import seed_default_styles

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.assert_ready()
    init_db()
    seeded = seed_default_styles()
    if seeded:
        logger.info("seeded %s default style presets", seeded)
    media.ensure_dirs()
    recovered = recover_interrupted_creations()
    if recovered["resumed"] or recovered["failed"]:
        logger.info("startup recovery: %s", recovered)
    recovered_vlogs = recover_interrupted_vlogs()
    if recovered_vlogs["resumed"] or recovered_vlogs["failed"]:
        logger.info("startup vlog recovery: %s", recovered_vlogs)
    requeued_merges = recover_merge_jobs()
    if requeued_merges:
        logger.info("startup merge recovery: requeued %s", requeued_merges)
    if not video_merge.ffmpeg_available():
        logger.warning(
            "FFmpeg/ffprobe 未安装,服务端合成不可用(提交合成会返回 503);安装后重启即可恢复"
        )
    # 看门狗:回收心跳丢失的生成中任务,避免用户被单任务并发限制永久锁死
    stop_event = threading.Event()
    sweeper = start_sweeper(stop_event)
    # 合成调度:轮询排队任务,按全局并发上限认领执行
    dispatcher = start_merge_dispatcher(stop_event)
    try:
        yield
    finally:
        stop_event.set()
        sweeper.join(timeout=5)
        dispatcher.join(timeout=5)
        # 先停调度再收尾:终止运行中的 ffmpeg 并把任务重新排队,避免孤儿进程与残留临时目录
        merge_queue.cancel_all_merges()


def create_app() -> FastAPI:
    app = FastAPI(title="eDream AI Photo Web", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix="/api")
    app.include_router(configs.router, prefix="/api")
    app.include_router(creations.router, prefix="/api")
    app.include_router(styles.router, prefix="/api")
    app.include_router(vlogs.router, prefix="/api")

    @app.get("/api/health", response_model=HealthOut)
    def health():
        return HealthOut(ok=True, media_dir=settings.media_dir)

    media.ensure_dirs()
    # 静态产物也挂在 /api 下:所有后端入口共用同一前缀,网关/反代只转发 /api 即可
    app.mount("/api/media", StaticFiles(directory=settings.media_dir), name="media")
    return app


app = create_app()
