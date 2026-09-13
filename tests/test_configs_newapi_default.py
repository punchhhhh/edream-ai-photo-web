"""模型配置:一键 new-api 默认配置(按当前用户的 Casdoor 标识取网关 system 密钥)。"""

import pytest
from fastapi.testclient import TestClient

from backend.services import newapi_internal
from backend.settings import settings


def _headers(sub: str) -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


@pytest.fixture()
def gateway_ready(monkeypatch):
    monkeypatch.setattr(settings, "new_api_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "new_api_internal_key_id", "key-test")
    monkeypatch.setattr(settings, "new_api_internal_key", "secret-test")
    # 默认模型也显式固定,避免依赖本机 .env
    monkeypatch.setattr(settings, "new_api_default_chat_model", "chat-x")
    monkeypatch.setattr(settings, "new_api_default_image_model", "image-x")
    monkeypatch.setattr(settings, "new_api_default_video_model", "video-x")


def test_newapi_default_creates_config_and_takes_over_default(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    captured: dict[str, str] = {}

    def _fake_get_key(oidc_sub, *, deactivated_message=None):
        captured["oidc_sub"] = oidc_sub
        return "sk-system-key"

    monkeypatch.setattr(newapi_internal, "get_user_system_key", _fake_get_key)

    with TestClient(create_app()) as client:
        # 先手工建一个默认配置,验证一键配置会接管默认位
        manual = client.post(
            "/api/configs",
            json={"name": "手工配置", "base_url": "https://old.example.com", "api_key": "sk-old"},
            headers=_headers("user-a"),
        )
        assert manual.status_code == 200

        resp = client.post("/api/configs/newapi-default", headers=_headers("user-a"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "new-api 默认配置"
        assert body["base_url"] == "https://gateway.example.com"
        assert body["is_default"] is True
        # 平台默认模型随一键配置预填
        assert body["chat_model"] == "chat-x"
        assert body["image_model"] == "image-x"
        assert body["video_model"] == "video-x"
        # 密钥只在服务端流转:响应里没有明文,只有掩码
        assert "sk-system-key" not in resp.text
        assert "****" in body["api_key_masked"]
        assert captured["oidc_sub"] == "user-a"

        rows = client.get("/api/configs", headers=_headers("user-a")).json()
        defaults = [r for r in rows if r["is_default"]]
        assert len(defaults) == 1 and defaults[0]["id"] == body["id"]

        # 重复调用不重复建配置:已选模型保留,为空的模型按平台默认补齐
        client.put(
            f"/api/configs/{body['id']}",
            json={
                "name": "new-api 默认配置",
                "base_url": "https://gateway.example.com",
                "api_key": "",
                "chat_model": "my-chat",
                "image_model": "",
                "video_model": "",
            },
            headers=_headers("user-a"),
        )
        again = client.post("/api/configs/newapi-default", headers=_headers("user-a"))
        assert again.status_code == 200
        assert again.json()["id"] == body["id"]
        assert again.json()["chat_model"] == "my-chat"  # 用户已选,不被覆盖
        assert again.json()["image_model"] == "image-x"  # 为空,补默认
        assert again.json()["video_model"] == "video-x"
        assert again.json()["is_default"] is True

        # 各用户的配置互不可见:别人的一键配置不影响 user-a
        other = client.post("/api/configs/newapi-default", headers=_headers("user-b"))
        assert other.status_code == 200
        assert other.json()["id"] != body["id"]


def test_newapi_default_requires_gateway(monkeypatch) -> None:
    from backend.app import create_app

    monkeypatch.setattr(settings, "new_api_base_url", "")
    monkeypatch.setattr(settings, "new_api_internal_key_id", "")
    monkeypatch.setattr(settings, "new_api_internal_key", "")

    with TestClient(create_app()) as client:
        resp = client.post("/api/configs/newapi-default", headers=_headers("user-a"))
        assert resp.status_code == 502
        assert "new-api 网关未配置" in resp.json()["detail"]


def test_newapi_default_surfaces_gateway_errors(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    def _fake_get_key(oidc_sub, *, deactivated_message=None):
        raise newapi_internal.NewApiInternalError(deactivated_message or "boom")

    monkeypatch.setattr(newapi_internal, "get_user_system_key", _fake_get_key)

    with TestClient(create_app()) as client:
        resp = client.post("/api/configs/newapi-default", headers=_headers("user-a"))
        assert resp.status_code == 502
        # 停用令牌的场景使用个人化文案,而不是共创的"企业主"文案
        assert "system 令牌已停用" in resp.json()["detail"]
