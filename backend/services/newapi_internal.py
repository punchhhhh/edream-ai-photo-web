"""new-api 内部接口客户端:按 Casdoor 用户标识取该用户的 system 网关密钥。

new-api 侧约定(internal 包):
- GET {base}/api/internal/user/api-keys?oidc_id=<casdoor sub>
- 请求头 X-Key-Id / X-Key 由平台管理员在 new-api 后台配置,本项目从环境变量读取
- 用户不存在时 new-api 会自动开户并发放不限额的 "system" 令牌
- 用户主动停用 system 令牌时响应里 token 为 null(视为未授权)
"""

import logging

import httpx

from ..settings import settings

logger = logging.getLogger(__name__)


class NewApiInternalError(RuntimeError):
    """内部接口不可用或配置缺失,信息可直接展示给用户。"""


def cocreation_gateway_ready() -> bool:
    return bool(
        settings.new_api_base_url.strip()
        and settings.new_api_internal_key_id.strip()
        and settings.new_api_internal_key.strip()
    )


def get_user_system_key(oidc_sub: str, *, deactivated_message: str | None = None) -> str:
    """返回该用户 system 令牌的密钥(自动带 sk- 前缀)。

    令牌被用户停用抛 NewApiInternalError;网络/配置问题同样抛错,由调用方决定兜底方式。
    deactivated_message 允许调用方按场景替换停用提示(共创/个人默认配置文案不同)。
    """
    base = settings.new_api_base_url.strip().rstrip("/")
    if not base or not settings.new_api_internal_key_id.strip() or not settings.new_api_internal_key.strip():
        raise NewApiInternalError("共创服务未配置(new-api 网关或内部密钥缺失),请联系平台管理员")
    url = f"{base}/api/internal/user/api-keys"
    try:
        resp = httpx.get(
            url,
            params={"oidc_id": oidc_sub},
            headers={
                "X-Key-Id": settings.new_api_internal_key_id.strip(),
                "X-Key": settings.new_api_internal_key.strip(),
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    except httpx.HTTPError as e:
        raise NewApiInternalError(f"请求共创网关失败({url}):{e.__class__.__name__}") from e
    if resp.status_code >= 400:
        raise NewApiInternalError(f"共创网关返回 {resp.status_code}:{resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        raise NewApiInternalError("共创网关响应不是合法 JSON") from None
    if not data.get("success"):
        raise NewApiInternalError(f"共创网关拒绝请求:{data.get('message') or '未知错误'}")
    token = data.get("data", {}).get("token")
    if not token or not token.get("key"):
        raise NewApiInternalError(
            deactivated_message or "企业主未授权共创服务(网关 system 令牌已停用),请联系企业管理员"
        )
    key = str(token["key"])
    return key if key.startswith("sk-") else f"sk-{key}"
