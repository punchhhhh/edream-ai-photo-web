"""认证与并发限制的核心行为测试(dev 模式 + X-Casdoor-Sub 头 + SQLite)。"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.auth import principal_from_claims
from backend.schemas import mask_secret

CONFIG_PAYLOAD = {
    "name": "测试网关",
    "base_url": "https://gw.example.com/v1",
    "api_key": "sk-secret-key-abcdef",
    "chat_model": "gpt-4o-mini",
    "image_model": "",
    "video_model": "kling-v1-master",
    "video_provider": "video_generations",
    "is_default": True,
}


def _headers(sub: str) -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


# ---------------------------------------------------------------- 纯单元

def test_principal_prefers_standard_subject() -> None:
    principal = principal_from_claims({"sub": "standard-sub", "id": "casdoor-id", "email": "u@example.test"})
    assert principal.sub == "standard-sub"
    assert principal.email == "u@example.test"


def test_principal_falls_back_to_casdoor_owner_and_name() -> None:
    principal = principal_from_claims({"owner": "edream", "name": "alice", "displayName": "Alice"})
    assert principal.sub == "edream/alice"
    assert principal.display_name == "Alice"


def test_principal_rejects_claims_without_subject() -> None:
    with pytest.raises(HTTPException) as exc_info:
        principal_from_claims({"email": "u@example.test"})
    assert exc_info.value.status_code == 401


def test_mask_secret_hides_middle() -> None:
    assert mask_secret("sk-secret-key-abcdef") == "sk-se****cdef"
    assert "secret" not in mask_secret("sk-secret-key-abcdef")


# ---------------------------------------------------------------- 接口集成

@pytest.fixture()
def client(monkeypatch):
    # 拦掉后台线程,让任务停在 pending,便于验证并发限制
    monkeypatch.setattr("backend.routers.creations.start_creation_thread", lambda _id: None)
    from backend.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _create_config(client: TestClient, sub: str) -> dict:
    resp = client.post("/api/configs", json=CONFIG_PAYLOAD, headers=_headers(sub))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_creation(client: TestClient, sub: str, config_id: int) -> object:
    return client.post(
        "/api/creations",
        json={
            "input_text": "黄昏的海边,一只橘猫追着浪花奔跑",
            "style": "电影质感",
            "expanded_prompt": "黄昏的海边,橘猫,浪花,电影质感",
            "image_source": "none",
            "config_id": config_id,
            "duration": 5,
        },
        headers=_headers(sub),
    )


def test_api_requires_login(client: TestClient) -> None:
    assert client.get("/api/configs").status_code == 401
    assert client.get("/api/creations").status_code == 401


def test_dev_login_sets_session_cookie(client: TestClient) -> None:
    resp = client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    assert "edream_session=" in resp.headers.get("set-cookie", "")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["oauth_sub"] == "dev-user"


def test_config_response_never_contains_api_key(client: TestClient) -> None:
    created = _create_config(client, "user-a")
    assert "api_key" not in created
    assert created["api_key_masked"]
    assert "secret" not in created["api_key_masked"]
    listed = client.get("/api/configs", headers=_headers("user-a")).json()
    assert all("api_key" not in c and c["api_key_masked"] for c in listed)


def test_update_with_blank_key_keeps_stored_key(client: TestClient) -> None:
    created = _create_config(client, "user-a")
    payload = {**CONFIG_PAYLOAD, "api_key": "", "name": "改名"}
    resp = client.put(f"/api/configs/{created['id']}", json=payload, headers=_headers("user-a"))
    assert resp.status_code == 200
    assert resp.json()["name"] == "改名"
    assert resp.json()["api_key_masked"] == created["api_key_masked"]


def test_configs_are_isolated_per_user(client: TestClient) -> None:
    _create_config(client, "user-a")
    assert client.get("/api/configs", headers=_headers("user-b")).json() == []


def test_second_generation_rejected_while_active(client: TestClient) -> None:
    config = _create_config(client, "user-a")
    first = _create_creation(client, "user-a", config["id"])
    assert first.status_code == 200
    assert first.json()["status"] == "pending"

    second = _create_creation(client, "user-a", config["id"])
    assert second.status_code == 409
    assert "生成中" in second.json()["detail"]

    # 其他用户不受影响
    other_config = _create_config(client, "user-b")
    assert _create_creation(client, "user-b", other_config["id"]).status_code == 200

    # 任务结束后(模拟完成后)可再次提交
    from backend.database import SessionLocal
    from backend.models import Creation

    with SessionLocal() as db:
        row = db.get(Creation, first.json()["id"])
        row.status = "completed"
        db.commit()
    assert _create_creation(client, "user-a", config["id"]).status_code == 200


def test_creations_are_isolated_per_user(client: TestClient) -> None:
    config_a = _create_config(client, "user-a")
    config_b = _create_config(client, "user-b")
    assert _create_creation(client, "user-a", config_a["id"]).status_code == 200
    assert _create_creation(client, "user-b", config_b["id"]).status_code == 200
    assert len(client.get("/api/creations", headers=_headers("user-a")).json()) == 1
    assert len(client.get("/api/creations", headers=_headers("user-b")).json()) == 1
    # 越权访问别人返回 404
    other_id = client.get("/api/creations", headers=_headers("user-b")).json()[0]["id"]
    assert client.get(f"/api/creations/{other_id}", headers=_headers("user-a")).status_code == 404


def test_config_test_requires_key_or_saved_config(client: TestClient) -> None:
    resp = client.post("/api/configs/test", json={"base_url": "https://x.com"}, headers=_headers("user-a"))
    assert resp.status_code == 422
    # 传别人的 config_id 也拿不到密钥
    created = _create_config(client, "user-a")
    resp = client.post(
        "/api/configs/test", json={"config_id": created["id"]}, headers=_headers("user-b")
    )
    assert resp.status_code == 404
