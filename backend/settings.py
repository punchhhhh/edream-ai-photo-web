from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg2://tailaogu@localhost:5432/edream_ai_photo"
    media_dir: str = str(BASE_DIR / "media")
    video_poll_interval: float = 5.0
    video_timeout_seconds: float = 900.0
    max_upload_mb: int = 20
    max_vlog_upload_total_mb: int = 90
    vlog_image_long_edge: int = 1600
    # 成片大小上限:服务端合成超限会自动压缩重试(先降码率再降分辨率);上传/回传接口共用
    max_video_upload_mb: int = 500

    # ---- 服务端合成(FFmpeg 任务队列) ----
    # 全局同时渲染的合成任务数(CPU 大户,限全局不限每用户)。
    # 并发计数与取消都在进程内:按单进程部署设计,多 uvicorn worker 会各自计数
    max_merge_workers: int = 1
    # 调度线程轮询排队任务的间隔(秒)
    merge_poll_interval: float = 2.0
    # 单个合成任务的硬超时(秒),超过且无心跳由看门狗回收
    merge_timeout_seconds: float = 1800.0

    # ---- 资产存储(腾讯云 COS) ----
    # local = 本地磁盘(MEDIA_DIR,开发/测试零依赖);cos = 腾讯云 COS(私有读写 + 临时预签名链接)
    storage_backend: str = "local"
    cos_region: str = ""  # 地域,如 ap-guangzhou
    cos_bucket: str = ""  # 存储桶完整名称(含 APPID,如 mybucket-1250000000)
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    # 对象 key 统一前缀(多应用共用桶时区分用),留空表示不加
    cos_prefix: str = "edream"
    # 临时预签名链接有效期(秒)
    cos_presign_expires_seconds: int = 3600

    # ---- 登录(Casdoor OAuth 授权码模式) ----
    # jwt = 走 Casdoor OAuth;dev = 免 OAuth,直接给 DEV_AUTH_SUB 建会话(本地开发用)
    auth_mode: str = "jwt"
    oauth_authorize_url: str = ""
    oauth_token_url: str = ""
    # 用 access_token 换用户信息的端点;由 IdP 校验 token,不依赖 JWKS 本地验签
    oauth_userinfo_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    # 留空时用请求的 base_url 拼 /api/auth/callback
    oauth_redirect_uri: str = ""
    # 登录 state 有效期(分钟)与会话有效期(小时)
    oauth_state_ttl_minutes: int = 10
    session_ttl_hours: int = 168
    session_cookie_name: str = "edream_session"
    session_cookie_secure: bool = False
    dev_auth_sub: str = "dev-user"

    # 跨域来源,逗号分隔;生产部署改为实际的前端域名
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
