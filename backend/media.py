"""媒体文件辅助:魔数嗅探、相对路径白名单、本地原子落盘。

相对路径(存储 key)布局:users/{user_id}/{kind}/{filename};历史数据可能是 {kind}/{filename}。
本地后端落在 MEDIA_DIR 下经 /api/media 静态服务;对象存储后端见 backend/storage.py。
"""

import re
from collections.abc import Iterable
from pathlib import Path

from .settings import settings

ALLOWED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

# 相对路径白名单:可选 users/{uid} 前缀 + 三个子目录之一的单层文件名,拒绝 `..`/绝对路径等穿越
_SAFE_REL = re.compile(r"(?:users/\d{1,10}/)?(?:images|uploads|videos)/[A-Za-z0-9._-]{1,150}")


class MediaTooLarge(ValueError):
    """写入内容超过大小上限。"""


class MediaEmpty(ValueError):
    """写入内容为空。"""


def media_root() -> Path:
    return Path(settings.media_dir)


def ensure_dirs() -> None:
    for sub in ("images", "videos", "uploads"):
        (media_root() / sub).mkdir(parents=True, exist_ok=True)


def is_safe_rel(rel: str | None) -> bool:
    return bool(rel) and _SAFE_REL.fullmatch(rel) is not None


def safe_abs_path(rel: str | None) -> Path | None:
    """解析(可能是客户端传入的)相对路径;不合法返回 None,调用方据此拒绝。"""
    return media_root() / rel if is_safe_rel(rel) else None


def write_rel(rel: str, chunks: Iterable[bytes], *, max_bytes: int | None = None) -> str:
    """按完整相对路径原子写入本地媒体目录(先写 .tmp 再 rename),返回 rel。"""
    final = media_root() / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    size = 0
    try:
        with tmp.open("wb") as f:
            for chunk in chunks:
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise MediaTooLarge(str(size))
                f.write(chunk)
        if size == 0:
            raise MediaEmpty(rel)
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)
    return rel


def sniff_image(data: bytes) -> str | None:
    """按魔数识别图片格式并返回扩展名;识别不了返回 None。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def sniff_video(head: bytes) -> bool:
    """按魔数判断是否视频:mp4/mov 的 ftyp box 或 webm/mkv 的 EBML 头。"""
    return head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3"


def media_url(rel: str | None) -> str | None:
    """本地后端的产物访问地址。统一挂在 /api 前缀下,反向代理只需按 /api 一条规则转发。"""
    return f"/api/media/{rel}" if rel else None


def remove_rel(rel: str | None) -> None:
    if not rel:
        return
    path = safe_abs_path(rel)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
