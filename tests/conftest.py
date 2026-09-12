"""测试环境:必须在导入 backend 之前把环境变量指向 SQLite 临时库,并启用 dev 免登模式。"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="edream-test-")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_tmpdir}/test.db"
os.environ["AUTH_MODE"] = "dev"
os.environ["DEV_AUTH_SUB"] = "dev-user"
os.environ["MEDIA_DIR"] = os.path.join(_tmpdir, "media")
# 测试一律走本地存储,避免读到开发者 .env 里的 STORAGE_BACKEND=cos 而写真实桶
os.environ["STORAGE_BACKEND"] = "local"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """每个测试用全新表,避免用例间数据串扰。"""
    from backend.database import Base, engine, init_db

    Base.metadata.drop_all(engine)
    init_db()
    yield
