import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import media
from .database import init_db
from .routers import auth, configs, creations
from .schemas import HealthOut
from .services.pipeline import recover_interrupted_creations, start_sweeper
from .settings import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    media.ensure_dirs()
    recovered = recover_interrupted_creations()
    if recovered["resumed"] or recovered["failed"]:
        logger.info("startup recovery: %s", recovered)
    # 看门狗:回收心跳丢失的生成中任务,避免用户被单任务并发限制永久锁死
    stop_event = threading.Event()
    sweeper = start_sweeper(stop_event)
    try:
        yield
    finally:
        stop_event.set()
        sweeper.join(timeout=5)


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

    @app.get("/api/health", response_model=HealthOut)
    def health():
        return HealthOut(ok=True, media_dir=settings.media_dir)

    media.ensure_dirs()
    # 静态产物也挂在 /api 下:所有后端入口共用同一前缀,网关/反代只转发 /api 即可
    app.mount("/api/media", StaticFiles(directory=settings.media_dir), name="media")
    return app


app = create_app()
