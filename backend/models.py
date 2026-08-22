from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class User(Base):
    """OAuth 登录用户,oauth_sub 是 IdP(Casdoor)侧的稳定标识。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    oauth_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OAuthState(Base):
    """授权码流程的 state 防重放/CSRF,一次性,短有效期。"""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthSession(Base):
    """服务端会话。Cookie 存原始 token,库里只存 sha256 哈希。"""

    __tablename__ = "oauth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(8), default="")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelConfig(Base):
    """new-api 网关配置,后端按这里的配置调用模型。api_key 仅服务端使用,不回传前端。"""

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(String(500))
    api_key: Mapped[str] = mapped_column(String(500))
    chat_model: Mapped[str] = mapped_column(String(200), default="")
    image_model: Mapped[str] = mapped_column(String(200), default="")
    video_model: Mapped[str] = mapped_column(String(200), default="")
    # video_generations = new-api 任务式接口;openai_videos = Sora 风格 /v1/videos
    video_provider: Mapped[str] = mapped_column(String(50), default="video_generations")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Creation(Base):
    """一次视频生成任务的完整记录。"""

    __tablename__ = "creations"
    __table_args__ = (
        # 每个用户同时最多一个生成中任务(应用层 409 + 数据库层兜底防并发竞态)
        Index(
            "uq_creations_user_active",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'generating_video')"),
            sqlite_where=text("status IN ('pending', 'generating_video')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    input_text: Mapped[str] = mapped_column(Text)
    style: Mapped[str] = mapped_column(String(50), default="")
    expanded_prompt: Mapped[str] = mapped_column(Text, default="")
    # none = 纯文生视频;generated = 先生成图片;uploaded = 用户上传参考图;merged = 浏览器合成
    image_source: Mapped[str] = mapped_column(String(20), default="none")
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 远端回退地址:本地文件优先(播放地址由 video_path 在输出时推导);仅下载失败时存网关远端 URL
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int] = mapped_column(Integer, default=5)
    # pending / generating_video / completed / failed
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    # 生成时所用配置快照,方便以后换模型后追溯
    config_name: Mapped[str] = mapped_column(String(100), default="")
    chat_model: Mapped[str] = mapped_column(String(200), default="")
    image_model: Mapped[str] = mapped_column(String(200), default="")
    video_model: Mapped[str] = mapped_column(String(200), default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
