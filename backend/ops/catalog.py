"""企业素材一期固定用途、格式和大小规则。"""

from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .. import media
from ..settings import settings

PURPOSES = {
    "ip_setting": {
        "label": "IP 设定",
        "extensions": {".docx", ".pdf"},
        "accepts_text": True,
    },
    "ip_visual": {
        "label": "IP 形象",
        "extensions": {".jpg", ".png", ".webp", ".psd", ".mp4", ".mov", ".webm"},
    },
    "brand_product": {
        "label": "品牌与产品",
        "extensions": {".jpg", ".png", ".webp", ".pdf", ".docx", ".mp4", ".mov", ".webm"},
        "accepts_text": True,
    },
}

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".psd": "image/vnd.adobe.photoshop",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


@dataclass(frozen=True, slots=True)
class UploadDescriptor:
    original_filename: str
    extension: str
    content_type: str
    storage_kind: str
    mime_type: str
    size_bytes: int
    max_bytes: int


def validate_purpose(purpose: str) -> str:
    if purpose not in PURPOSES:
        raise HTTPException(422, "未知素材用途")
    return purpose


def validate_text(purpose: str, text_content: str) -> str:
    validate_purpose(purpose)
    if not PURPOSES[purpose].get("accepts_text"):
        raise HTTPException(422, "该用途不支持直接录入文字")
    value = text_content.strip()
    if not value:
        raise HTTPException(422, "文字内容不能为空")
    if len(value) > settings.enterprise_text_max_chars:
        raise HTTPException(422, f"文字内容不能超过 {settings.enterprise_text_max_chars} 字")
    return value


def describe_upload(file: UploadFile, purpose: str) -> UploadDescriptor:
    validate_purpose(purpose)
    original = Path(file.filename or "asset").name[:255]
    ext = Path(original).suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in PURPOSES[purpose]["extensions"]:
        if not PURPOSES[purpose]["extensions"]:
            raise HTTPException(422, f"{PURPOSES[purpose]['label']}仅支持直接录入文字")
        allowed = "、".join(sorted(PURPOSES[purpose]["extensions"]))
        raise HTTPException(422, f"{PURPOSES[purpose]['label']}仅支持：{allowed}")

    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size <= 0:
        raise HTTPException(422, "素材不能为空")
    head = file.file.read(16)
    file.file.seek(0)

    sniffed_image = media.sniff_image(head)
    if ext in {".jpg", ".png", ".webp"}:
        if sniffed_image != ext:
            raise HTTPException(422, "图片实际格式与文件扩展名不一致")
        content_type, storage_kind = "image", "images"
        max_bytes = settings.enterprise_image_max_mb * 1024 * 1024
    elif ext in {".mp4", ".mov", ".webm"}:
        if not media.sniff_video(head):
            raise HTTPException(422, "无法识别视频文件格式")
        content_type, storage_kind = "video", "videos"
        max_bytes = settings.enterprise_video_max_mb * 1024 * 1024
    elif ext == ".psd":
        if head[:4] != b"8BPS":
            raise HTTPException(422, "无法识别 PSD 文件格式")
        content_type, storage_kind = "source", "files"
        max_bytes = settings.enterprise_source_max_mb * 1024 * 1024
    else:
        if ext == ".pdf" and head[:4] != b"%PDF":
            raise HTTPException(422, "无法识别 PDF 文件格式")
        if ext == ".docx" and head[:2] != b"PK":
            raise HTTPException(422, "无法识别 DOCX 文件格式")
        content_type, storage_kind = "document", "files"
        max_bytes = settings.enterprise_document_max_mb * 1024 * 1024

    if size > max_bytes:
        raise HTTPException(422, f"文件不能超过 {max_bytes // 1024 // 1024}MB")
    return UploadDescriptor(
        original_filename=original,
        extension=ext,
        content_type=content_type,
        storage_kind=storage_kind,
        mime_type=MIME_BY_EXT[ext],
        size_bytes=size,
        max_bytes=max_bytes,
    )


def normalize_tags(tags: list[str] | str) -> list[str]:
    values = tags.split(",") if isinstance(tags, str) else tags
    result: list[str] = []
    for value in values:
        tag = value.strip()[:40]
        if tag and tag not in result:
            result.append(tag)
    if len(result) > 20:
        raise HTTPException(422, "标签最多 20 个")
    return result
