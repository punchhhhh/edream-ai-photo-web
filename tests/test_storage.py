"""存储层测试:本地后端行为 + COS 后端分发逻辑(桩掉腾讯云 SDK 客户端,无需真实依赖)。"""

import io

import pytest

from backend import storage
from backend.settings import settings


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data

    def get_stream(self):
        yield self._data


class _StubCOS:
    """模拟 cos-python-sdk-v5 CosS3Client 的最小接口,记录调用以便断言。"""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[str] = []

    def put_object(self, Bucket, Key, Body):
        # 真实 SDK 的 put_object 同时接受字节与文件流,统一读成字节再记录
        if hasattr(Body, "read"):
            Body.seek(0)
            Body = Body.read()
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": _FakeStream(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("not found")

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)

    def get_presigned_url(self, Method, Bucket, Key, Expired):
        return f"https://{Bucket}.cos.ap-test.myqcloud.com/{Key}?sign&expired={Expired}"


# ---------------------------------------------------------------- 本地后端


def test_local_backend_key_layout_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))

    key = storage.save_bytes("images", b"png-bytes", ".png", user_id=7)
    assert key.startswith("users/7/images/") and key.endswith(".png")
    assert storage.url(key) == f"/api/media/{key}"
    assert storage.read(key) == b"png-bytes"
    assert storage.exists(key)
    assert not storage.exists("users/7/images/not-exist.png")

    storage.delete(key)
    assert not storage.exists(key)


def test_local_backend_save_seekable_enforces_limit(tmp_path, monkeypatch):
    from backend.media import MediaTooLarge

    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))

    key = storage.save_seekable("videos", io.BytesIO(b"x" * 1024), ".mp4", user_id=3, max_bytes=4096)
    assert key.startswith("users/3/videos/")
    assert storage.read(key) == b"x" * 1024

    with pytest.raises(MediaTooLarge):
        storage.save_seekable("videos", io.BytesIO(b"x" * 8192), ".mp4", user_id=3, max_bytes=4096)


# ---------------------------------------------------------------- COS 后端(桩)


def _enable_cos(monkeypatch, stub):
    monkeypatch.setattr(settings, "storage_backend", "cos")
    monkeypatch.setattr(settings, "cos_bucket", "edream-1250000000")
    monkeypatch.setattr(settings, "cos_prefix", "edream")
    monkeypatch.setattr(storage, "cos_client", lambda: stub)


def test_cos_backend_upload_presign_read_delete(monkeypatch):
    stub = _StubCOS()
    _enable_cos(monkeypatch, stub)

    key = storage.save_bytes("videos", b"video-bytes", ".mp4", user_id=42)
    assert key.startswith("users/42/videos/")
    # 对象 key 必须带统一前缀、按用户组织
    assert stub.objects[("edream-1250000000", f"edream/{key}")] == b"video-bytes"

    # 访问地址是临时预签名链接,不落库、每次访问实时生成
    url = storage.url(key)
    assert url.startswith("https://edream-1250000000.cos.ap-test.myqcloud.com/edream/users/42/videos/")
    assert "expired=" in url

    assert storage.read(key) == b"video-bytes"
    assert storage.exists(key)
    assert not storage.exists("users/42/videos/missing.mp4")

    storage.delete(key)
    assert stub.deleted == [f"edream/{key}"]


def test_cos_backend_save_seekable_uploads_fileobj(monkeypatch):
    from backend.media import MediaTooLarge

    stub = _StubCOS()
    _enable_cos(monkeypatch, stub)

    payload = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048
    key = storage.save_seekable("videos", io.BytesIO(payload), ".mp4", user_id=1, max_bytes=4096)
    assert stub.objects[("edream-1250000000", f"edream/{key}")] == payload

    with pytest.raises(MediaTooLarge):
        storage.save_seekable("videos", io.BytesIO(b"x" * 8192), ".mp4", user_id=1, max_bytes=4096)


def test_assert_ready_rejects_incomplete_cos_config(monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "cos")
    monkeypatch.setattr(settings, "cos_region", "")
    monkeypatch.setattr(settings, "cos_bucket", "")
    monkeypatch.setattr(settings, "cos_secret_id", "")
    monkeypatch.setattr(settings, "cos_secret_key", "")
    with pytest.raises(RuntimeError, match="COS_REGION"):
        storage.assert_ready()


def test_assert_ready_rejects_unknown_backend(monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "s3")
    with pytest.raises(RuntimeError, match="STORAGE_BACKEND"):
        storage.assert_ready()


def test_local_backend_needs_no_cos_config():
    storage.assert_ready()  # local 模式直接放行
