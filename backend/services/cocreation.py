"""企业共创视频的业务规则:可见性判定、模版读取、企业主网关密钥解析、次数限制。"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Creation,
    Enterprise,
    EnterpriseMembership,
    EnterpriseVideoTemplate,
    ModelConfig,
    User,
)
from ..settings import settings
from . import newapi_internal
from .ai_client import AICallError
from .newapi_internal import NewApiInternalError

# 成员关系里视为"已启用"的状态(历史上曾用过 approved)
ACTIVE_MEMBERSHIP_STATUSES = {"active", "approved"}

# 网关配置是否齐全(转出自 newapi_internal,路由层统一从本模块取共创规则)
cocreation_gateway_ready = newapi_internal.cocreation_gateway_ready


@dataclass(frozen=True, slots=True)
class CoCreationContext:
    enterprise: Enterprise
    membership: EnterpriseMembership


def cocreation_context(db: Session, user_id: int) -> CoCreationContext | None:
    """当前用户是否可参与共创:企业认证通过且成员关系启用。"""
    membership = db.scalar(
        select(EnterpriseMembership)
        .where(
            EnterpriseMembership.user_id == user_id,
            EnterpriseMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES),
        )
        .order_by(EnterpriseMembership.id.desc())
    )
    if membership is None:
        return None
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        return None
    return CoCreationContext(enterprise=enterprise, membership=membership)


def list_templates(
    db: Session, enterprise_id: int, *, include_inactive: bool = False
) -> list[EnterpriseVideoTemplate]:
    conditions = [EnterpriseVideoTemplate.enterprise_id == enterprise_id]
    if not include_inactive:
        conditions.append(EnterpriseVideoTemplate.is_active.is_(True))
    return list(
        db.scalars(
            select(EnterpriseVideoTemplate)
            .where(*conditions)
            .order_by(EnterpriseVideoTemplate.sort_order, EnterpriseVideoTemplate.id)
        ).all()
    )


def get_template(db: Session, enterprise_id: int, template_id: int) -> EnterpriseVideoTemplate | None:
    template = db.get(EnterpriseVideoTemplate, template_id)
    if template is None or template.enterprise_id != enterprise_id:
        return None
    return template


def used_video_count(db: Session, user_id: int, enterprise_id: int) -> int:
    """已占用次数:该用户在该企业下非失败的共创任务数(生成中/已完成都算)。"""
    rows = db.scalars(
        select(Creation.id).where(
            Creation.user_id == user_id,
            Creation.enterprise_id == enterprise_id,
            Creation.status != "failed",
        )
    ).all()
    return len(rows)


def quota_left(db: Session, user_id: int, enterprise_id: int) -> int:
    return max(0, settings.cocreation_max_videos_per_user - used_video_count(db, user_id, enterprise_id))


def enterprise_gateway_credentials(db: Session, enterprise_id: int) -> tuple[str, str]:
    """返回 (网关 base_url, 企业主网关密钥)。

    共创内容用企业主的默认模型配置(如「一键 new-api 默认配置」落库的地址与密钥);
    企业主还没有默认配置时,再按其 Casdoor 标识实时从 new-api 内部接口取 system 密钥。
    密钥均不落新表、不回传前端;任何失败都转成 AICallError,
    生成管线与接口层可以直接落库/透出。
    """
    owner_membership = db.scalar(
        select(EnterpriseMembership).where(
            EnterpriseMembership.enterprise_id == enterprise_id,
            EnterpriseMembership.role == "owner",
            EnterpriseMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES),
        )
    )
    if owner_membership is None:
        raise AICallError("企业缺少启用的 Owner 账号,无法获取共创网关密钥")
    owner = db.get(User, owner_membership.user_id)
    if owner is None:
        raise AICallError("企业 Owner 账号不存在,无法获取共创网关密钥")
    # 默认位没有唯一约束(由 configs 路由收敛),防御性地取最新一条
    default_config = db.scalar(
        select(ModelConfig)
        .where(ModelConfig.user_id == owner.id, ModelConfig.is_default.is_(True))
        .order_by(ModelConfig.id.desc())
    )
    if default_config is not None:
        return default_config.base_url, default_config.api_key
    try:
        key = newapi_internal.get_user_system_key(owner.oauth_sub)
    except NewApiInternalError as e:
        raise AICallError(str(e)) from e
    return settings.new_api_base_url.strip(), key


def combine_prompt(template: EnterpriseVideoTemplate, user_text: str) -> str:
    """模版画面要求 + 成员创意,模版要求在前且不可被成员覆盖。"""
    parts = [template.prompt.strip(), user_text.strip()]
    return "\n".join(part for part in parts if part)
