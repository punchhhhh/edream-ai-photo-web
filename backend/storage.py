"""资产存储层:产物(图片/视频)按用户组织,统一 key,多后端分发。

- local(默认):本地磁盘(MEDIA_DIR),经 /api/media 静态服务,开发与测试零依赖
- cos:腾讯云 COS(私有读写),访问时实时生成临时预签名链接,不依赖任何外部地址

key 布局:users/{user_id}/{kind}/{filename}(kind ∈ images|uploads|videos)
数据库只存 key;历史数据的 {kind}/{filename} 布局同样兼容。
"""

import logging
import threading
import time
import uuid
from typing import BinaryIO

from . import media
from .settings import settings

logger = logging.getLogger(__name__)

KINDS = ("images", "uploads", "videos")
_CHUNK = 1024 * 1024


def build_key(user_id: int, kind: str, name: str) -> str:
    return f"users/{user_id}/{kind}/{name}"


def _new_name(ext: str) -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"


def use_cos() -> bool:
    return settings.storage_backend == "cos"


def assert_ready() -> None:
    """启动时校验:选了 cos 就必须配全、装好依赖,避免跑了半天才发现存不了。"""
    if settings.storage_backend == "local":
        return
    if not use_cos():
        raise RuntimeError(f"未知 STORAGE_BACKEND:{settings.storage_backend}(仅支持 local / cos)")
    missing = [
        name
        for name, value in (
            ("COS_REGION", settings.cos_region),
            ("COS_BUCKET", settings.cos_bucket),
            ("COS_SECRET_ID", settings.cos_secret_id),
            ("COS_SECRET_KEY", settings.cos_secret_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"STORAGE_BACKEND=cos 但缺少配置:{', '.join(missing)}")
    try:
        from qcloud_cos import CosConfig, CosS3Client  # noqa: F401
    except ImportError as e:
        raise RuntimeError("STORAGE_BACKEND=cos 需要安装 COS SDK:pip install '.[cos]'") from e


# ---------------------------------------------------------------- COS 客户端(懒加载单例)

_client_lock = threading.Lock()
_cos_client = None


def cos_client():
    with _client_lock:
        global _cos_client
        if _cos_client is None:
            from qcloud_cos import CosConfig, CosS3Client

            _cos_client = CosS3Client(
                CosConfig(
                    Region=settings.cos_region,
                    SecretId=settings.cos_secret_id,
                    SecretKey=settings.cos_secret_key,
                    Scheme="https",
                )
            )
        return _cos_client


def _full_key(key: str) -> str:
    prefix = settings.cos_prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


# ---------------------------------------------------------------- 对外操作


def save_bytes(kind: str, data: bytes, ext: str, *, user_id: int) -> str:
    """保存字节内容,返回存储 key。"""
    key = build_key(user_id, kind, _new_name(ext))
    if use_cos():
        cos_client().put_object(Bucket=settings.cos_bucket, Key=_full_key(key), Body=data)
    else:
        media.write_rel(key, (data,))
    return key


def save_seekable(
    kind: str, fileobj: BinaryIO, ext: str, *, user_id: int, max_bytes: int | None = None
) -> str:
    """保存可 seek 的文件对象(如 Starlette UploadFile),大文件不整读进内存。"""
    fileobj.seek(0, 2)
    size = fileobj.tell()
    fileobj.seek(0)
    if size == 0:
        raise media.MediaEmpty(kind)
    if max_bytes is not None and size > max_bytes:
        raise media.MediaTooLarge(str(size))
    key = build_key(user_id, kind, _new_name(ext))
    if use_cos():
        cos_client().upload_fileobj(Bucket=settings.cos_bucket, Key=_full_key(key), Body=fileobj)
    else:
        media.write_rel(key, iter(lambda: fileobj.read(_CHUNK), b""), max_bytes=max_bytes)
    return key


def read(key: str) -> bytes:
    if use_cos():
        resp = cos_client().get_object(Bucket=settings.cos_bucket, Key=_full_key(key))
        return b"".join(resp["Body"].get_stream())
    path = media.safe_abs_path(key)
    if path is None or not path.is_file():
        raise FileNotFoundError(key)
    return path.read_bytes()


def exists(key: str) -> bool:
    if use_cos():
        try:
            cos_client().head_object(Bucket=settings.cos_bucket, Key=_full_key(key))
            return True
        except Exception:  # noqa: BLE001 —— 不存在/无权限统一按"没有"处理
            return False
    path = media.safe_abs_path(key)
    return path is not None and path.is_file()


def url(key: str | None) -> str | None:
    """产物的访问地址:COS 生成临时预签名链接;本地走 /api/media。"""
    if not key:
        return None
    if use_cos():
        return cos_client().get_presigned_url(
            Method="GET",
            Bucket=settings.cos_bucket,
            Key=_full_key(key),
            Expired=settings.cos_presign_expires_seconds,
        )
    return media.media_url(key)


def delete(key: str | None) -> None:
    if not key or not media.is_safe_rel(key):
        return
    if use_cos():
        try:
            cos_client().delete_object(Bucket=settings.cos_bucket, Key=_full_key(key))
        except Exception:  # noqa: BLE001 —— 删除失败只记日志,不阻断业务
            logger.warning("delete cos object failed: %s", key, exc_info=True)
    else:
        media.remove_rel(key)
