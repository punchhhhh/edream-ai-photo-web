from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VIDEO_PROVIDERS = ("video_generations", "openai_videos")


def mask_secret(secret: str) -> str:
    """密钥掩码:仅保留首尾少量字符,任何接口都不回传完整密钥。"""
    if len(secret) <= 8:
        return secret[:2] + "****"
    return f"{secret[:5]}****{secret[-4:]}"


class AuthUserOut(BaseModel):
    id: int
    oauth_sub: str
    email: str | None
    display_name: str | None
    avatar_url: str | None


class ModelConfigIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=500)
    chat_model: str = ""
    image_model: str = ""
    video_model: str = ""
    video_provider: str = "video_generations"
    is_default: bool = False


class ModelConfigUpdateIn(BaseModel):
    """编辑配置:api_key 留空表示保留原密钥。"""

    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)
    chat_model: str = ""
    image_model: str = ""
    video_model: str = ""
    video_provider: str = "video_generations"
    is_default: bool = False


class ModelConfigOut(BaseModel):
    """api_key 永不出现在响应里,只回传掩码。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    api_key_masked: str
    chat_model: str
    image_model: str
    video_model: str
    video_provider: str
    is_default: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_config(cls, config) -> "ModelConfigOut":
        return cls(
            id=config.id,
            name=config.name,
            base_url=config.base_url,
            api_key_masked=mask_secret(config.api_key),
            chat_model=config.chat_model,
            image_model=config.image_model,
            video_model=config.video_model,
            video_provider=config.video_provider,
            is_default=config.is_default,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )


class ConfigTestIn(BaseModel):
    """测试连接:传 config_id 用存储密钥测;否则必须填 base_url + api_key(用户本次输入)。"""

    config_id: int | None = None
    base_url: str = ""
    api_key: str = ""
    chat_model: str = ""
    image_model: str = ""
    video_model: str = ""


class ExpandIn(BaseModel):
    config_id: int
    text: str = Field(min_length=1, max_length=2000)
    style: str = ""


class ExpandOut(BaseModel):
    expanded_prompt: str


class ImageGenIn(BaseModel):
    config_id: int
    prompt: str = Field(min_length=1, max_length=4000)
    size: str = "1024x1024"


class MediaOut(BaseModel):
    image_path: str
    url: str


class VlogImageOut(BaseModel):
    image_path: str
    url: str
    width: int
    height: int
    order: int


class VlogPlanClipOut(BaseModel):
    reference_paths: list[str]
    duration: int


class VlogPlanIn(BaseModel):
    image_paths: list[str] = Field(min_length=1, max_length=9)


class VlogPlanOut(BaseModel):
    ratio: str
    target_duration: int
    clips: list[VlogPlanClipOut]


class VlogUploadOut(BaseModel):
    images: list[VlogImageOut]
    ratio: str
    target_duration: int
    clips: list[VlogPlanClipOut]


class VlogCreateIn(BaseModel):
    config_id: int
    # 手动组装时每个 AI 组最多 4 张图，允许多个组加入同一个 Vlog。
    image_paths: list[str] = Field(min_length=1, max_length=36)
    # 前端手动维护的 AI 片段组。未传时兼容旧客户端，继续按图片自动规划。
    image_groups: list[list[str]] | None = None
    # 未传时兼容旧客户端，继续按图片方向推断画幅。
    ratio: Literal["9:16", "16:9"] | None = None
    style: str = Field(default="写实纪录", max_length=50)
    description: str = Field(default="", max_length=500)
    transition_style: str = Field(default="fade", max_length=30)


class VlogClipOut(BaseModel):
    id: int
    sequence: int
    reference_paths: list[str]
    reference_urls: list[str]
    duration: int
    status: str
    error: str | None
    video_url: str | None
    retry_count: int


class VlogProjectOut(BaseModel):
    id: int
    description: str
    style: str
    image_paths: list[str]
    image_urls: list[str]
    ratio: str
    resolution: str
    target_duration: int
    transition_style: str
    status: str
    error: str | None
    final_video_url: str | None
    final_creation_id: int | None
    config_name: str
    video_model: str
    clips: list[VlogClipOut]
    created_at: datetime
    updated_at: datetime


class CreationIn(BaseModel):
    input_text: str = Field(min_length=1, max_length=2000)
    style: str = ""
    expanded_prompt: str = ""
    image_source: str = "none"
    image_path: str | None = None
    config_id: int
    duration: int = Field(default=5, ge=1, le=60)


class CreationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    input_text: str
    style: str
    expanded_prompt: str
    image_source: str
    image_path: str | None
    image_url: str | None
    video_url: str | None
    duration: int
    status: str
    error: str | None
    config_name: str
    chat_model: str
    image_model: str
    video_model: str
    created_at: datetime
    updated_at: datetime


class StyleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    negative_prompt: str
    image_size: str
    sort_order: int


class ConfigTestOut(BaseModel):
    ok: bool
    models: list[str] = []
    found: dict[str, bool | None] = {}
    note: str = ""


class HealthOut(BaseModel):
    ok: bool
    media_dir: str
