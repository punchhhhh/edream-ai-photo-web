from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    _ensure_vlog_transition_schema()
    _ensure_enterprise_schema()
    _ensure_cocreation_schema()


def _ensure_vlog_transition_schema() -> None:
    """为当前轻量 create_all 初始化方式补齐已有库的 Vlog 转场字段。

    这里故意只使用两种数据库都支持的简单 DDL,不引入 Alembic,也不把本机路径
    或其他部署信息写进数据库。新库仍由 SQLAlchemy metadata 创建。
    """
    inspector = inspect(engine)
    if "vlog_projects" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("vlog_projects")}
    statements: list[str] = []
    if "transition_style" not in columns:
        statements.append(
            "ALTER TABLE vlog_projects ADD COLUMN transition_style VARCHAR(30) NOT NULL DEFAULT 'fade'"
        )
    if "timeline_data" not in columns:
        default = "'[]'::json" if engine.dialect.name == "postgresql" else "'[]'"
        statements.append(
            f"ALTER TABLE vlog_projects ADD COLUMN timeline_data JSON NOT NULL DEFAULT {default}"
        )
    if "merge_status" not in columns:
        statements.append("ALTER TABLE vlog_projects ADD COLUMN merge_status VARCHAR(20)")
    if "merge_progress" not in columns:
        statements.append(
            "ALTER TABLE vlog_projects ADD COLUMN merge_progress FLOAT NOT NULL DEFAULT 0"
        )
    if "merge_error" not in columns:
        statements.append("ALTER TABLE vlog_projects ADD COLUMN merge_error TEXT")
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _ensure_enterprise_schema() -> None:
    """补齐运营平台一期字段。这里只做可向前兼容的加列，复杂迁移后续交给 Alembic。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "enterprises" not in tables:
            return

        enterprise_columns = {column["name"] for column in inspector.get_columns("enterprises")}
        statements: list[str] = []
        should_backfill_asset_content_type = False
        if "rejection_reason" not in enterprise_columns:
            statements.append("ALTER TABLE enterprises ADD COLUMN rejection_reason TEXT")
        if "reviewed_by_user_id" not in enterprise_columns:
            statements.append("ALTER TABLE enterprises ADD COLUMN reviewed_by_user_id INTEGER")
        if "reviewed_at" not in enterprise_columns:
            statements.append("ALTER TABLE enterprises ADD COLUMN reviewed_at TIMESTAMP")

        if "enterprise_assets" in tables:
            asset_columns = {column["name"] for column in inspector.get_columns("enterprise_assets")}
            if "tags" not in asset_columns:
                default = "'[]'::json" if engine.dialect.name == "postgresql" else "'[]'"
                statements.append(f"ALTER TABLE enterprise_assets ADD COLUMN tags JSON NOT NULL DEFAULT {default}")
            if "description" not in asset_columns:
                statements.append("ALTER TABLE enterprise_assets ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            if "status" not in asset_columns:
                statements.append("ALTER TABLE enterprise_assets ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'")
            if "created_by_user_id" not in asset_columns:
                statements.append("ALTER TABLE enterprise_assets ADD COLUMN created_by_user_id INTEGER")
            if "purpose" not in asset_columns:
                statements.append(
                    "ALTER TABLE enterprise_assets ADD COLUMN purpose VARCHAR(40) NOT NULL DEFAULT 'brand_product'"
                )
            if "content_type" not in asset_columns:
                statements.append(
                    "ALTER TABLE enterprise_assets ADD COLUMN content_type VARCHAR(20) NOT NULL DEFAULT 'document'"
                )
                should_backfill_asset_content_type = "kind" in asset_columns
            if "current_version" not in asset_columns:
                statements.append(
                    "ALTER TABLE enterprise_assets ADD COLUMN current_version INTEGER NOT NULL DEFAULT 1"
                )
            if "deleted_at" not in asset_columns:
                statements.append("ALTER TABLE enterprise_assets ADD COLUMN deleted_at TIMESTAMP")

        for statement in statements:
            connection.execute(text(statement))

        if should_backfill_asset_content_type:
            connection.execute(
                text(
                    """
                    UPDATE enterprise_assets
                    SET content_type = CASE
                        WHEN LOWER(kind) IN ('image', 'images') THEN 'image'
                        WHEN LOWER(kind) IN ('video', 'videos') THEN 'video'
                        WHEN LOWER(kind) IN ('text', 'texts') THEN 'text'
                        WHEN LOWER(kind) IN ('source', 'sources') THEN 'source'
                        ELSE 'document'
                    END
                    """
                )
            )

        if "enterprise_assets" in tables:
            connection.execute(
                text(
                    """
                    UPDATE enterprise_assets
                    SET purpose = CASE
                        WHEN purpose = 'ip_persona' THEN 'ip_setting'
                        WHEN purpose IN ('brand_identity', 'product_material', 'scene_reference')
                            THEN 'brand_product'
                        ELSE purpose
                    END
                    WHERE purpose IN (
                        'ip_persona', 'brand_identity', 'product_material', 'scene_reference'
                    )
                    """
                )
            )

        # 早期草稿把已审核成员记为 approved；正式权限模型统一使用 active。
        if "enterprise_memberships" in tables:
            connection.execute(
                text("UPDATE enterprise_memberships SET status='active' WHERE status='approved'")
            )
            # 一期收敛为一个企业一个 Casdoor Owner，保留旧成员记录但停止授权。
            connection.execute(
                text(
                    "UPDATE enterprise_memberships SET status='disabled' "
                    "WHERE role != 'owner' AND status != 'disabled'"
                )
            )

        # 把早期单表素材提升为 v1，保证已有上传在升级后仍可读取。
        if "enterprise_assets" in tables and "enterprise_asset_versions" in tables:
            connection.execute(
                text(
                    """
                    INSERT INTO enterprise_asset_versions (
                        asset_id, version_no, original_filename, mime_type, storage_key,
                        text_content, content_data, size_bytes, checksum_sha256, status,
                        created_by_user_id, created_at
                    )
                    SELECT
                        a.id, 1, a.name, a.mime_type, NULLIF(a.asset_path, ''),
                        NULL, '{}', a.size_bytes, '', 'active',
                        COALESCE(a.created_by_user_id, a.uploaded_by_user_id), a.created_at
                    FROM enterprise_assets a
                    WHERE NOT EXISTS (
                        SELECT 1 FROM enterprise_asset_versions v WHERE v.asset_id = a.id
                    )
                    """
                )
            )

        # 已认证企业升级时补默认额度，已存在记录不会覆盖管理员配置。
        if "enterprise_storage_quotas" in tables:
            default_limit = settings.enterprise_default_quota_mb * 1024 * 1024
            connection.execute(
                text(
                    f"""
                    INSERT INTO enterprise_storage_quotas (
                        enterprise_id, limit_bytes, used_bytes, reserved_bytes, status,
                        created_at, updated_at
                    )
                    SELECT
                        e.id, {default_limit},
                        COALESCE((
                            SELECT SUM(v.size_bytes)
                            FROM enterprise_asset_versions v
                            JOIN enterprise_assets a ON a.id = v.asset_id
                            WHERE a.enterprise_id = e.id AND v.status != 'deleted'
                        ), 0),
                        0, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    FROM enterprises e
                    WHERE e.status IN ('approved', 'suspended')
                      AND NOT EXISTS (
                          SELECT 1 FROM enterprise_storage_quotas q WHERE q.enterprise_id = e.id
                      )
                    """
                )
            )


def _ensure_cocreation_schema() -> None:
    """补齐企业共创视频字段:creations 表加企业/模版标记列,模版表加互动剧本字段。"""
    inspector = inspect(engine)
    if "creations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("creations")}
    statements: list[str] = []
    if "enterprise_id" not in columns:
        statements.append("ALTER TABLE creations ADD COLUMN enterprise_id INTEGER")
    if "template_id" not in columns:
        statements.append("ALTER TABLE creations ADD COLUMN template_id INTEGER")
    if "template_name" not in columns:
        statements.append(
            "ALTER TABLE creations ADD COLUMN template_name VARCHAR(100) NOT NULL DEFAULT ''"
        )
    if "cocreation_materials" not in columns:
        default = "'[]'::json" if engine.dialect.name == "postgresql" else "'[]'"
        statements.append(
            f"ALTER TABLE creations ADD COLUMN cocreation_materials JSON NOT NULL DEFAULT {default}"
        )

    if "enterprise_video_templates" in inspector.get_table_names():
        template_columns = {
            column["name"] for column in inspector.get_columns("enterprise_video_templates")
        }
        if "first_frame_prompt" not in template_columns:
            statements.append("ALTER TABLE enterprise_video_templates ADD COLUMN first_frame_prompt TEXT NOT NULL DEFAULT ''")
        if "image_model" not in template_columns:
            statements.append(
                "ALTER TABLE enterprise_video_templates ADD COLUMN image_model VARCHAR(200) NOT NULL DEFAULT ''"
            )
        if "member_photo" not in template_columns:
            statements.append(
                "ALTER TABLE enterprise_video_templates ADD COLUMN member_photo VARCHAR(20) NOT NULL DEFAULT 'none'"
            )
        if "member_photo_hint" not in template_columns:
            statements.append(
                "ALTER TABLE enterprise_video_templates ADD COLUMN member_photo_hint VARCHAR(200) NOT NULL DEFAULT ''"
            )
        if "first_frame_confirm" not in template_columns:
            statements.append(
                "ALTER TABLE enterprise_video_templates ADD COLUMN first_frame_confirm BOOLEAN NOT NULL DEFAULT TRUE"
            )
        if "interaction_options" not in template_columns:
            default = "'[]'::json" if engine.dialect.name == "postgresql" else "'[]'"
            statements.append(
                f"ALTER TABLE enterprise_video_templates ADD COLUMN interaction_options JSON NOT NULL DEFAULT {default}"
            )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
