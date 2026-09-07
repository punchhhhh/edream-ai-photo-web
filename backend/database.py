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
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
