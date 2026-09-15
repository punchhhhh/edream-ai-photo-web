from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
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


class StylePreset(Base):
    """风格预设:一段结构化的画面语言描述,影响 AI 拓展与生成参数(负向提示词/建议画幅)。

    全局内容表(非用户数据),启动时幂等播种默认风格;已存在的不覆盖,方便直接改库自定义。
    """

    __tablename__ = "style_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    # 画面语言要点:机位/镜头、光影、色调、质感、氛围,拼进拓展 prompt
    description: Mapped[str] = mapped_column(Text, default="")
    # 负向提示词:视频生成时传入(网关/模型不支持时由参数降级重试自动剔除)
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    # 该风格建议的首帧画幅
    image_size: Mapped[str] = mapped_column(String(20), default="1280x720")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Enterprise(Base):
    """企业认证主体。审核通过后企业 Owner 才能上传企业素材。"""

    __tablename__ = "enterprises"
    __table_args__ = (Index("uq_enterprises_credit_code", "credit_code", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    credit_code: Mapped[str] = mapped_column(String(100), default="", index=True)
    contact_name: Mapped[str] = mapped_column(String(100), default="")
    contact_phone: Mapped[str] = mapped_column(String(50), default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # pending / approved / rejected / suspended / archived
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseMembership(Base):
    """Casdoor Owner 与企业的一对一归属关系，企业账号本身不另设密码。"""

    __tablename__ = "enterprise_memberships"
    __table_args__ = (
        Index("uq_enterprise_membership_user_enterprise", "user_id", "enterprise_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # 一期只创建 owner；保留字符串字段兼容历史数据迁移。
    role: Mapped[str] = mapped_column(String(30), default="owner")
    # pending / active / disabled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseEntryToken(Base):
    """企业专属业务入口；Token 只定位企业，实际能力由临时 Grant 授予。"""

    __tablename__ = "enterprise_entry_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", ondelete="CASCADE"), unique=True, index=True
    )
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    # auto / manual
    approval_mode: Mapped[str] = mapped_column(String(20), default="auto")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grant_ttl_hours: Mapped[int] = mapped_column(Integer, default=24)
    video_limit: Mapped[int] = mapped_column(Integer, default=3)
    terms_version: Mapped[str] = mapped_column(String(40), default="2026-09-v1")
    privacy_version: Mapped[str] = mapped_column(String(40), default="2026-09-v1")
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseConsumerGrant(Base):
    """C 端用户使用企业共创能力的临时授权，不代表企业成员身份。"""

    __tablename__ = "enterprise_consumer_grants"
    __table_args__ = (
        Index(
            "uq_enterprise_consumer_grant_current",
            "entry_id",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'active')"),
            sqlite_where=text("status IN ('pending', 'active')"),
        ),
        Index("ix_enterprise_consumer_grant_enterprise_status", "enterprise_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", ondelete="CASCADE"), index=True
    )
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("enterprise_entry_tokens.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # pending / active / exhausted / expired / revoked / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    approval_mode: Mapped[str] = mapped_column(String(20), default="auto")
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    video_limit: Mapped[int] = mapped_column(Integer, default=3)
    video_used: Mapped[int] = mapped_column(Integer, default=0)
    terms_version: Mapped[str] = mapped_column(String(40), default="")
    privacy_version: Mapped[str] = mapped_column(String(40), default="")
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseCocreationUsageLedger(Base):
    """企业共创视频提交流水(含失败返还 refund);内容删除不会删除或回退本流水。"""

    __tablename__ = "enterprise_cocreation_usage_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", ondelete="CASCADE"), index=True
    )
    grant_id: Mapped[int] = mapped_column(
        ForeignKey("enterprise_consumer_grants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    creation_id: Mapped[int | None] = mapped_column(
        ForeignKey("creations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(30))
    amount: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="committed")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlatformUserRole(Base):
    """应用内平台角色；环境变量中的管理员仅用于首次引导。"""

    __tablename__ = "platform_user_roles"
    __table_args__ = (Index("uq_platform_user_role", "user_id", "role", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="platform_admin")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EnterpriseStorageQuota(Base):
    """企业对象存储额度；预占字段防止并发上传共同突破上限。"""

    __tablename__ = "enterprise_storage_quotas"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", ondelete="CASCADE"), unique=True, index=True
    )
    limit_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseAsset(Base):
    """企业素材逻辑记录；具体文字或文件内容保存在版本表。"""

    __tablename__ = "enterprise_assets"
    __table_args__ = (
        Index("ix_enterprise_assets_enterprise_purpose", "enterprise_id", "purpose"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id", ondelete="CASCADE"), index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 兼容早期运营草稿字段；新逻辑以 purpose/content_type 和版本表为准。
    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="")
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    content_type: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(200))
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    asset_path: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseAssetVersion(Base):
    """素材不可变版本；生成任务保存版本号和哈希即可稳定复现。"""

    __tablename__ = "enterprise_asset_versions"
    __table_args__ = (
        Index("uq_enterprise_asset_version", "asset_id", "version_no", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("enterprise_assets.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_data: Mapped[dict] = mapped_column(JSON, default=dict)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum_sha256: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EnterpriseQuotaLedger(Base):
    """企业空间变更流水，正数占用、负数释放。"""

    __tablename__ = "enterprise_quota_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("enterprise_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delta_bytes: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(50))
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """运营侧关键操作和内部素材读取审计。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int | None] = mapped_column(
        ForeignKey("enterprises.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(100), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EnterpriseVideoTemplate(Base):
    """企业共创视频模版:企业主/管理员配置,成员基于模版用企业主的网关密钥生成视频。

    网关调用参数(地址/密钥)不在模版里保存:生成时优先用企业主的默认模型配置,
    没有时按企业 Owner 的 Casdoor 标识从 new-api 内部接口实时取 system 密钥,
    模版只保存模型与提示词约束。
    """

    __tablename__ = "enterprise_video_templates"
    __table_args__ = (
        Index("ix_enterprise_video_templates_enterprise_active", "enterprise_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    # 给成员看的模版说明
    description: Mapped[str] = mapped_column(String(500), default="")
    # 模版固定的画面要求,生成时拼在成员创意前面
    prompt: Mapped[str] = mapped_column(Text, default="")
    # 互动剧本:首帧合成用的构图要求;为空时退化为用 prompt 合成
    first_frame_prompt: Mapped[str] = mapped_column(Text, default="")
    # 首帧合成用的图像模型;为空时用企业主默认配置的 image_model 或平台默认
    image_model: Mapped[str] = mapped_column(String(200), default="")
    # 可选:提供 AI 拓展能力(用企业主的网关密钥调用)
    chat_model: Mapped[str] = mapped_column(String(200), default="")
    video_model: Mapped[str] = mapped_column(String(200), default="")
    # video_generations = new-api 任务式接口;openai_videos = Sora 风格 /v1/videos
    video_provider: Mapped[str] = mapped_column(String(50), default="video_generations")
    duration: Mapped[int] = mapped_column(Integer, default=5)
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    # 成员出镜要求:none = 不出镜;required = 必须上传照片;optional = 可选上传
    member_photo: Mapped[str] = mapped_column(String(20), default="none")
    member_photo_hint: Mapped[str] = mapped_column(String(200), default="")
    # 首帧生成后是否需要成员确认再继续生成视频(企业按模版成熟度/预算选择)
    first_frame_confirm: Mapped[bool] = mapped_column(Boolean, default=True)
    # 预设的剧情选项,成员点选即用;不填则只展示自由输入
    interaction_options: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EnterpriseTemplateAsset(Base):
    """模版绑定的企业素材:character_reference = IP 形象参考图(进首帧合成);
    prompt_text = 特征文字(注入提示词);cover = 模版封面(成员端卡片展示)。
    绑定不锁版本,生成时取素材当前启用版本并在任务上做快照,便于追溯与复现。
    """

    __tablename__ = "enterprise_template_assets"
    __table_args__ = (
        Index("uq_template_asset", "template_id", "asset_id", "usage", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("enterprise_video_templates.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("enterprise_assets.id", ondelete="CASCADE"), index=True
    )
    # character_reference / prompt_text / cover
    usage: Mapped[str] = mapped_column(String(30))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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
    # 参考图的存储 key,形如 users/{uid}/uploads|images/xxx.png(历史数据可能是 images|uploads/xxx.png)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 共创附加参考图存储 key 列表:首帧(image_path)之外,随视频生成一起提交的中间/参考画面
    reference_image_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 成片的存储 key,形如 users/{uid}/videos/xxx.mp4
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 远端回退地址:本地/对象存储文件优先(播放地址由 video_path 在输出时推导);仅下载失败时存网关远端 URL
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

    # 企业共创任务:enterprise_id 非空表示基于企业模版、用企业主网关密钥生成
    enterprise_id: Mapped[int | None] = mapped_column(
        ForeignKey("enterprises.id", ondelete="SET NULL"), nullable=True, index=True
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("enterprise_video_templates.id", ondelete="SET NULL"), nullable=True
    )
    # 模版名快照,模版删除后历史记录仍可展示
    template_name: Mapped[str] = mapped_column(String(100), default="")
    # 共创素材快照:[{asset_id, version_id, usage, name}],记录本次生成实际使用的素材版本
    cocreation_materials: Mapped[list[dict]] = mapped_column(JSON, default=list)
    enterprise_grant_id: Mapped[int | None] = mapped_column(
        ForeignKey("enterprise_consumer_grants.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class VlogProject(Base):
    """多图 Vlog 项目；多个片段生成完成后由浏览器做最终转场合成。"""

    __tablename__ = "vlog_projects"
    __table_args__ = (
        Index(
            "uq_vlog_projects_user_active",
            "user_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'generating_video', 'ready_to_merge')"
            ),
            sqlite_where=text(
                "status IN ('pending', 'generating_video', 'ready_to_merge')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(String(50), default="写实纪录")
    image_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 完整时间线：AI 组、图片动效和本地/历史视频的可编辑来源。
    timeline_data: Mapped[list[dict]] = mapped_column(JSON, default=list)
    ratio: Mapped[str] = mapped_column(String(10), default="9:16")
    resolution: Mapped[str] = mapped_column(String(20), default="720p")
    target_duration: Mapped[int] = mapped_column(Integer, default=30)
    # 统一应用到每个场景边界的标准 xfade 模板
    transition_style: Mapped[str] = mapped_column(
        String(30), default="fade", server_default="fade"
    )
    # pending / generating_video / ready_to_merge / completed / failed / cancelled
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 服务端合成队列状态:None / queued / running / failed / done(项目本身保持 ready_to_merge,
    # 直到合成成功才转 completed,因此不触碰"唯一活跃项目"的部分唯一索引)
    merge_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    merge_progress: Mapped[float] = mapped_column(Float, default=0.0)
    merge_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_creation_id: Mapped[int | None] = mapped_column(
        ForeignKey("creations.id", ondelete="SET NULL"), nullable=True
    )

    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    config_name: Mapped[str] = mapped_column(String(100), default="")
    video_model: Mapped[str] = mapped_column(String(200), default="")
    video_provider: Mapped[str] = mapped_column(String(50), default="video_generations")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class VlogClip(Base):
    """Vlog 的单个 Seedance 片段，reference_paths 保留分组和顺序。"""

    __tablename__ = "vlog_clips"
    __table_args__ = (Index("uq_vlog_clips_project_sequence", "project_id", "sequence", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("vlog_projects.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    reference_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    prompt: Mapped[str] = mapped_column(Text)
    duration: Mapped[int] = mapped_column(Integer, default=15)
    # pending / generating_video / completed / failed
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
