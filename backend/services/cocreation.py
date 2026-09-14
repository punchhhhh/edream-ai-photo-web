"""企业共创视频的业务规则:可见性判定、模版读取、企业主网关密钥解析、次数限制、素材绑定解析。"""

import logging
import threading
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..models import (
    Creation,
    Enterprise,
    EnterpriseAsset,
    EnterpriseAssetVersion,
    EnterpriseMembership,
    EnterpriseTemplateAsset,
    EnterpriseVideoTemplate,
    ModelConfig,
    User,
)
from ..settings import settings
from . import newapi_internal
from .ai_client import AICallError
from .newapi_internal import NewApiInternalError

logger = logging.getLogger(__name__)

# 成员关系里视为"已启用"的状态(历史上曾用过 approved)
ACTIVE_MEMBERSHIP_STATUSES = {"active", "approved"}

# 网关配置是否齐全(转出自 newapi_internal,路由层统一从本模块取共创规则)
cocreation_gateway_ready = newapi_internal.cocreation_gateway_ready

# 首帧合成最多带的企业形象参考图张数(受多参考图模型的输入上限约束)
MAX_CHARACTER_REFERENCES = 3
# 注入提示词的特征文字总长度上限,防止企业素材把 prompt 撑爆
MAX_PROMPT_TEXT_CHARS = 2000

# 首帧合成按用户按天计数的进程内计数器;多进程部署时每进程各一份,总量上界 = 限额 × 进程数
_first_frame_counters: dict[int, tuple[str, int]] = {}
_first_frame_lock = threading.Lock()


def first_frame_allow(user_id: int, limit: int) -> bool:
    """首帧合成的每日次数闸门:首帧不占视频配额但花企业网关额度,单独限流。"""
    today = date.today().isoformat()
    with _first_frame_lock:
        day, count = _first_frame_counters.get(user_id, (today, 0))
        if day != today:
            count = 0
        if count >= limit:
            return False
        _first_frame_counters[user_id] = (today, count + 1)
        return True


@dataclass(frozen=True, slots=True)
class TemplateBinding:
    """模版绑定 + 素材 + 其当前启用版本;生成时统一从这里取内容。"""

    binding: EnterpriseTemplateAsset
    asset: EnterpriseAsset
    version: EnterpriseAssetVersion


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


def combine_prompt(
    template: EnterpriseVideoTemplate, user_text: str, ip_features: str = ""
) -> str:
    """模版画面要求 + IP 特征文字 + 成员创意;模版要求在前且不可被成员覆盖。"""
    parts = [template.prompt.strip()]
    if ip_features.strip():
        parts.append(f"品牌形象特征(必须严格遵守,不得改变外形/配色/服饰):\n{ip_features.strip()}")
    parts.append(user_text.strip())
    return "\n".join(part for part in parts if part)


def template_bindings(
    db: Session, template_id: int, usage: str | None = None
) -> list[TemplateBinding]:
    """取模版绑定的启用素材及其当前版本(绑定不锁版本,生成时统一解析)。"""
    conditions = [EnterpriseTemplateAsset.template_id == template_id]
    if usage:
        conditions.append(EnterpriseTemplateAsset.usage == usage)
    rows = db.execute(
        select(EnterpriseTemplateAsset, EnterpriseAsset, EnterpriseAssetVersion)
        .join(EnterpriseAsset, EnterpriseAsset.id == EnterpriseTemplateAsset.asset_id)
        .join(
            EnterpriseAssetVersion,
            EnterpriseAssetVersion.asset_id == EnterpriseAsset.id,
        )
        .where(
            *conditions,
            EnterpriseAsset.status == "active",
            EnterpriseAssetVersion.version_no == EnterpriseAsset.current_version,
            EnterpriseAssetVersion.status != "deleted",
        )
        .order_by(EnterpriseTemplateAsset.sort_order, EnterpriseTemplateAsset.id)
    ).all()
    return [TemplateBinding(binding=b, asset=a, version=v) for b, a, v in rows]


def prompt_text_features(bindings: list[TemplateBinding]) -> str:
    """把 prompt_text 绑定的文字素材合并为一段特征描述(带总量上限)。"""
    chunks: list[str] = []
    for item in bindings:
        if item.binding.usage != "prompt_text":
            continue
        text = (item.version.text_content or "").strip()
        if text:
            chunks.append(f"【{item.asset.name}】{text}")
        if sum(len(c) for c in chunks) >= MAX_PROMPT_TEXT_CHARS:
            break
    return "\n".join(chunks)[:MAX_PROMPT_TEXT_CHARS]


def character_reference_images(
    bindings: list[TemplateBinding],
) -> list[tuple[bytes, str]]:
    """读取 character_reference 绑定的图片字节(最多 MAX_CHARACTER_REFERENCES 张)。"""
    result: list[tuple[bytes, str]] = []
    for item in bindings:
        if item.binding.usage != "character_reference":
            continue
        if not item.version.storage_key:
            continue
        try:
            data = storage.read(item.version.storage_key)
        except Exception:  # noqa: BLE001 —— 单张参考图读不出来跳过,不拦整个合成
            logger.warning(
                "read character reference failed: %s", item.version.storage_key, exc_info=True
            )
            continue
        result.append((data, item.version.mime_type or "image/png"))
        if len(result) >= MAX_CHARACTER_REFERENCES:
            break
    return result


def material_snapshot(db: Session, template_id: int) -> list[dict]:
    """生成任务用的素材版本快照:记录实际用到的 asset/version,便于追溯与复现。"""
    return [
        {
            "asset_id": item.asset.id,
            "version_id": item.version.id,
            "usage": item.binding.usage,
            "name": item.asset.name,
        }
        for item in template_bindings(db, template_id)
        if item.binding.usage in ("character_reference", "prompt_text")
    ]


def resolve_image_model(db: Session, template: EnterpriseVideoTemplate) -> str:
    """首帧合成用的图像模型:模版指定 > 企业主默认配置 > 平台默认。"""
    if template.image_model:
        return template.image_model
    owner_membership = db.scalar(
        select(EnterpriseMembership).where(
            EnterpriseMembership.enterprise_id == template.enterprise_id,
            EnterpriseMembership.role == "owner",
            EnterpriseMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES),
        )
    )
    if owner_membership is not None:
        default_config = db.scalar(
            select(ModelConfig)
            .where(ModelConfig.user_id == owner_membership.user_id, ModelConfig.is_default.is_(True))
            .order_by(ModelConfig.id.desc())
        )
        if default_config is not None and default_config.image_model:
            return default_config.image_model
    return settings.new_api_default_image_model


FIRST_FRAME_GUARDRAIL = (
    "合成要求:参考图人物与形象必须同框出现、有自然互动;"
    "严格保持参考图中人物的面部特征和企业形象的体型/配色/服饰等关键特征不变;"
    "构图完整、光影统一,输出为视频首帧。"
)


def first_frame_compose_prompt(template: EnterpriseVideoTemplate, user_text: str, ip_features: str) -> str:
    """首帧合成 prompt:模版构图剧本在前,成员创意在后,末尾加一致性护栏。"""
    scene = (template.first_frame_prompt or template.prompt).strip()
    parts = [part for part in (scene, user_text.strip()) if part]
    if ip_features.strip():
        parts.append(f"品牌形象特征(必须严格遵守):{ip_features.strip()}")
    parts.append(FIRST_FRAME_GUARDRAIL)
    return "\n".join(parts)
