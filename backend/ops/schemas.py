from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import AuthUserOut

EnterpriseStatus = Literal["pending", "approved", "rejected", "suspended", "archived"]


class EnterpriseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    credit_code: str
    contact_name: str
    contact_phone: str
    contact_email: str
    description: str
    status: str
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime


class EnterpriseApplyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    credit_code: str = Field(min_length=2, max_length=100)
    contact_name: str = Field(min_length=1, max_length=100)
    contact_phone: str = Field(default="", max_length=50)
    contact_email: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=1000)


class EnterpriseUpdateIn(EnterpriseApplyIn):
    pass


class EnterpriseReviewIn(BaseModel):
    status: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=1000)


class EnterpriseStatusIn(BaseModel):
    status: Literal["approved", "suspended", "archived"]
    reason: str = Field(default="", max_length=1000)


class QuotaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enterprise_id: int
    limit_bytes: int
    used_bytes: int
    reserved_bytes: int
    status: str


class UploadLimitsOut(BaseModel):
    batch_max_files: int
    batch_max_bytes: int
    image_max_bytes: int
    document_max_bytes: int
    source_max_bytes: int
    video_max_bytes: int


class QuotaUpdateIn(BaseModel):
    limit_bytes: int = Field(gt=0)


class MembershipOut(BaseModel):
    id: int
    enterprise_id: int
    user_id: int
    oauth_sub: str
    display_name: str | None
    email: str | None
    role: str
    status: str
    created_at: datetime


class EnterpriseEntryOut(BaseModel):
    enterprise_id: int
    enterprise_name: str
    active: bool
    entry_url: str | None
    created_at: datetime | None
    updated_at: datetime | None


class EnterpriseEntryResolveIn(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class EnterpriseBusinessContextOut(BaseModel):
    enterprise_id: int
    enterprise_name: str


class OpsProfileOut(BaseModel):
    user: AuthUserOut
    is_platform_admin: bool
    enterprise: EnterpriseOut | None
    membership: MembershipOut | None
    quota: QuotaOut | None
    upload_limits: UploadLimitsOut


class AssetTextCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    purpose: str
    text_content: str
    content_data: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=1000)


class AssetUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: str | None = None
    tags: list[str] | None = None
    description: str | None = Field(default=None, max_length=1000)
    text_content: str | None = None
    content_data: dict | None = None


class AssetVersionOut(BaseModel):
    id: int
    version_no: int
    original_filename: str | None
    mime_type: str
    text_content: str | None
    content_data: dict
    size_bytes: int
    checksum_sha256: str
    created_at: datetime


class AssetOut(BaseModel):
    id: int
    enterprise_id: int
    purpose: str
    content_type: str
    name: str
    tags: list[str]
    description: str
    status: str
    current_version: int
    version: AssetVersionOut
    content_url: str | None
    created_at: datetime
    updated_at: datetime


class PlatformAdminCreateIn(BaseModel):
    oauth_sub: str = Field(min_length=1, max_length=255)


class PlatformAdminOut(BaseModel):
    user_id: int
    oauth_sub: str
    display_name: str | None
    email: str | None
    source: Literal["database", "environment"]


class InternalMaterialSearchIn(BaseModel):
    enterprise_id: int
    purposes: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    asset_ids: list[int] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=500)


class InternalMaterialOut(BaseModel):
    asset_id: int
    version_id: int
    version_no: int
    enterprise_id: int
    purpose: str
    content_type: str
    name: str
    tags: list[str]
    description: str
    text_content: str | None
    content_data: dict
    mime_type: str
    size_bytes: int
    checksum_sha256: str
    content_url: str | None
