"""企业共创视频:模版管理、可见性、次数限制、企业主网关密钥解析、成员视频查看。"""

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.settings import settings


def _headers(sub: str) -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


def _apply_and_approve(client: TestClient, sub: str, suffix: str) -> int:
    applied = client.post(
        "/api/ops/v1/enterprise",
        json={
            "name": f"共创企业{suffix}",
            "credit_code": f"COCREATE-{suffix}",
            "contact_name": "联系人",
            "contact_phone": "",
            "contact_email": "",
            "description": "",
        },
        headers=_headers(sub),
    )
    assert applied.status_code == 200, applied.text
    enterprise_id = applied.json()["enterprise"]["id"]
    reviewed = client.post(
        f"/api/ops/v1/admin/enterprises/{enterprise_id}/review",
        json={"status": "approved", "reason": ""},
        headers=_headers("platform-admin"),
    )
    assert reviewed.status_code == 200, reviewed.text
    return enterprise_id


def _create_template(client: TestClient, sub: str, name: str = "品牌宣传模版", **overrides) -> dict:
    payload = {
        "name": name,
        "description": "统一品牌调性",
        "prompt": "画面需出现品牌 Logo,暖色调",
        "chat_model": "",
        "video_model": "seedance-1-lite",
        "video_provider": "video_generations",
        "duration": 5,
        "negative_prompt": "模糊",
        "is_active": True,
        "sort_order": 0,
    }
    payload.update(overrides)
    resp = client.post("/api/ops/v1/video-templates", json=payload, headers=_headers(sub))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture()
def gateway_ready(monkeypatch):
    """打开共创网关配置(仅本用例生效)。"""
    monkeypatch.setattr(settings, "new_api_base_url", "https://gateway.example.com")
    monkeypatch.setattr(settings, "new_api_internal_key_id", "key-test")
    monkeypatch.setattr(settings, "new_api_internal_key", "secret-test")


def test_template_crud_requires_owner() -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-x", "A")
        # 未加入企业的用户不能管理模版(上游已收敛为仅企业 Owner 可用)
        assert (
            client.post(
                "/api/ops/v1/video-templates",
                json={"name": "t", "video_model": "m"},
                headers=_headers("editor-x"),
            ).status_code
            == 403
        )
        # owner 可以创建并读取
        template = _create_template(client, "owner-x")
        assert template["enterprise_id"] > 0
        assert template["video_model"] == "seedance-1-lite"

        listed = client.get("/api/ops/v1/video-templates", headers=_headers("owner-x"))
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [template["id"]]

        updated = client.patch(
            f"/api/ops/v1/video-templates/{template['id']}",
            json={"is_active": False, "duration": 10},
            headers=_headers("owner-x"),
        )
        assert updated.status_code == 200
        assert updated.json()["is_active"] is False
        assert updated.json()["duration"] == 10

        assert (
            client.delete(
                f"/api/ops/v1/video-templates/{template['id']}",
                headers=_headers("owner-x"),
            ).status_code
            == 200
        )
        assert client.get("/api/ops/v1/video-templates", headers=_headers("owner-x")).json() == []


def test_status_visibility_rules(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        status = client.get("/api/cocreation/status", headers=_headers("member-y"))
        assert status.status_code == 200
        body = status.json()
        assert body["available"] is False
        assert "企业认证" in body["reason"]

        # 网关未配置时,即使企业已认证也不可用
        _apply_and_approve(client, "owner-y", "B")
        _create_template(client, "owner-y")
        monkeypatch.setattr(settings, "new_api_base_url", "")
        status = client.get("/api/cocreation/status", headers=_headers("owner-y"))
        assert status.json()["available"] is False
        assert "共创服务" in status.json()["reason"]

        # 配置齐全 + 有模版 → 可用,并返回模版与配额
        monkeypatch.setattr(settings, "new_api_base_url", "https://gateway.example.com")
        status = client.get("/api/cocreation/status", headers=_headers("owner-y"))
        body = status.json()
        assert body["available"] is True
        assert len(body["templates"]) == 1
        assert body["used"] == 0
        assert body["limit"] == settings.cocreation_max_videos_per_user


def test_owner_can_see_and_use_cocreation_tab(gateway_ready, monkeypatch) -> None:
    """企业主(Owner)在主应用也能看到并使用共创 tab:显隐只看企业与模版状态,不看角色。"""
    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    from backend.app import create_app

    with TestClient(create_app()) as client:
        enterprise_id = _apply_and_approve(client, "owner-tab", "T")
        _create_template(client, "owner-tab", name="Owner模版")

        status = client.get("/api/cocreation/status", headers=_headers("owner-tab"))
        assert status.status_code == 200
        body = status.json()
        assert body["available"] is True
        assert [t["name"] for t in body["templates"]] == ["Owner模版"]

        # 不止能看到,Owner 也能直接提交共创视频(配额同样生效)
        created = client.post(
            "/api/cocreation/videos",
            json={"template_id": body["templates"][0]["id"], "text": "Owner 的共创创意"},
            headers=_headers("owner-tab"),
        )
        assert created.status_code == 200, created.text
        assert created.json()["enterprise_id"] == enterprise_id


def _finish_creation(creation_id: int, status: str) -> None:
    """测试辅助:把任务推进到终态(管线被替换为空操作,状态不会自己流转)。"""
    from backend.database import SessionLocal
    from backend.models import Creation

    with SessionLocal() as db:
        row = db.get(Creation, creation_id)
        row.status = status
        db.commit()


def test_create_video_quota_and_snapshot(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    started: list[int] = []

    def _fake_start(creation_id: int, **kwargs):
        started.append(creation_id)

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", _fake_start
    )
    monkeypatch.setattr(settings, "cocreation_max_videos_per_user", 2)

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-z", "C")
        template = _create_template(client, "owner-z", name="模版C")

        created = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "一只橘猫在海边奔跑"},
            headers=_headers("owner-z"),
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["enterprise_id"] and body["template_id"] == template["id"]
        assert body["template_name"] == "模版C"
        assert body["video_model"] == "seedance-1-lite"
        assert body["duration"] == 5
        assert "企业共创 · 模版C" == body["config_name"]
        # 模版画面要求在前,成员创意在后
        assert body["expanded_prompt"] == "画面需出现品牌 Logo,暖色调\n一只橘猫在海边奔跑"
        assert started == [body["id"]]

        # 状态接口的已用次数同步 +1(生成中也计入)
        status_body = client.get(
            "/api/cocreation/status", headers=_headers("owner-z")
        ).json()
        assert status_body["used"] == 1
        assert status_body["limit"] == 2

        # 任务完成后才能提交下一个(同用户同时只允许一个生成中任务)
        _finish_creation(body["id"], "completed")
        second = client.post(
            "/api/cocreation/videos",
            json={
                "template_id": template["id"],
                "text": "创意二",
                "expanded_prompt": "AI 拓展后的完整提示词",
            },
            headers=_headers("owner-z"),
        )
        assert second.status_code == 200
        assert second.json()["expanded_prompt"] == (
            "画面需出现品牌 Logo,暖色调\nAI 拓展后的完整提示词"
        )
        _finish_creation(second.json()["id"], "completed")

        # 第 3 个:超出限额(限 2)被拒绝
        third = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "创意三"},
            headers=_headers("owner-z"),
        )
        assert third.status_code == 409
        assert "共创次数已用完" in third.json()["detail"]

        # 失败任务不计入配额,释放后可以再提交
        _finish_creation(second.json()["id"], "failed")
        retry = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "创意三"},
            headers=_headers("owner-z"),
        )
        assert retry.status_code == 200, retry.text


def test_create_video_rejects_when_other_task_running(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-r", "D")
        template = _create_template(client, "owner-r")
        first = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "第一条"},
            headers=_headers("owner-r"),
        )
        assert first.status_code == 200
        # 同用户同时只允许一个生成中任务(与个人创作共用约束)
        second = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "第二条"},
            headers=_headers("owner-r"),
        )
        assert second.status_code == 409
        assert "正在生成中" in second.json()["detail"]


def test_pipeline_resolves_owner_gateway_key(gateway_ready, monkeypatch) -> None:
    """共创任务生成时按企业 Owner 的 Casdoor 标识实时取 system 密钥。"""
    from backend.database import SessionLocal
    from backend.models import Creation, EnterpriseVideoTemplate
    from backend.services.ai_client import AICallError
    from backend.services import newapi_internal
    from backend.services.pipeline import _resolve_gateway

    captured: dict[str, str] = {}

    def _fake_get_key(oidc_id: str) -> str:
        captured["oidc_id"] = oidc_id
        return "sk-system-key"

    monkeypatch.setattr(newapi_internal, "get_user_system_key", _fake_get_key)
    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-g", "E")
        template = _create_template(client, "owner-g")
        created = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "创意"},
            headers=_headers("owner-g"),
        )
        assert created.status_code == 200
        creation_id = created.json()["id"]
        template_id = template["id"]

    with SessionLocal() as session:
        creation = session.get(Creation, creation_id)
        base_url, api_key, provider = _resolve_gateway(session, creation)
        assert base_url == "https://gateway.example.com"
        assert api_key == "sk-system-key"
        assert provider == "video_generations"
        assert captured["oidc_id"] == "owner-g"

        # 模版被删除后,进行中的任务给出可理解的失败原因
        session.delete(session.get(EnterpriseVideoTemplate, template_id))
        session.commit()
        session.refresh(creation)
        with pytest.raises(AICallError):
            _resolve_gateway(session, creation)


def test_pipeline_prefers_owner_default_config(gateway_ready, monkeypatch) -> None:
    """共创任务优先用企业主的默认模型配置;默认位取消后才回退实时取 system 密钥。"""
    from backend.database import SessionLocal
    from backend.models import Creation, ModelConfig
    from backend.services import newapi_internal
    from backend.services.pipeline import _resolve_gateway

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-cfg", "H")
        template = _create_template(client, "owner-cfg")
        created = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "创意"},
            headers=_headers("owner-cfg"),
        )
        assert created.status_code == 200
        creation_id = created.json()["id"]

        # 企业主建的第一个模型配置自动占住默认位(一键默认配置同理)
        config = client.post(
            "/api/configs",
            json={
                "name": "企业主默认网关",
                "base_url": "https://owner-gateway.example.com",
                "api_key": "sk-owner-default",
            },
            headers=_headers("owner-cfg"),
        )
        assert config.status_code == 200, config.text
        assert config.json()["is_default"] is True

    def _fail_get_key(oidc_id: str) -> str:
        raise AssertionError("企业主已有默认配置时不应再实时取 system 密钥")

    monkeypatch.setattr(newapi_internal, "get_user_system_key", _fail_get_key)
    config_id = config.json()["id"]
    with SessionLocal() as session:
        creation = session.get(Creation, creation_id)
        base_url, api_key, provider = _resolve_gateway(session, creation)
        assert base_url == "https://owner-gateway.example.com"
        assert api_key == "sk-owner-default"
        assert provider == "video_generations"

        # 默认位取消后回退到实时取 system 密钥路径
        session.get(ModelConfig, config_id).is_default = False
        session.commit()

    monkeypatch.setattr(
        newapi_internal, "get_user_system_key", lambda oidc_id: "sk-system-key"
    )
    with SessionLocal() as session:
        creation = session.get(Creation, creation_id)
        base_url, api_key, provider = _resolve_gateway(session, creation)
        assert base_url == "https://gateway.example.com"
        assert api_key == "sk-system-key"
        assert provider == "video_generations"


def test_newapi_internal_client(monkeypatch) -> None:
    from backend.services import newapi_internal

    # 本机 .env 可能已配置网关,显式清空保证"未配置"分支可测
    monkeypatch.setattr(settings, "new_api_base_url", "")
    monkeypatch.setattr(settings, "new_api_internal_key_id", "")
    monkeypatch.setattr(settings, "new_api_internal_key", "")

    assert newapi_internal.cocreation_gateway_ready() is False
    with pytest.raises(newapi_internal.NewApiInternalError):
        newapi_internal.get_user_system_key("someone")

    monkeypatch.setattr(settings, "new_api_base_url", "https://gateway.example.com/")
    monkeypatch.setattr(settings, "new_api_internal_key_id", "key-test")
    monkeypatch.setattr(settings, "new_api_internal_key", "secret-test")
    assert newapi_internal.cocreation_gateway_ready() is True

    calls: dict[str, object] = {}

    def _fake_get(url: str, *, params=None, headers=None, timeout=None):
        calls.update({"url": url, "params": params, "headers": headers})

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "success": True,
                    "data": {
                        "created_user": False,
                        "token": {"key": "abc123", "status": 1, "name": "system"},
                    },
                }

        return _Resp()

    monkeypatch.setattr(newapi_internal.httpx, "get", _fake_get)
    key = newapi_internal.get_user_system_key("owner-casdoor-id")
    assert key == "sk-abc123"
    assert calls["url"] == "https://gateway.example.com/api/internal/user/api-keys"
    assert calls["params"] == {"oidc_id": "owner-casdoor-id"}
    assert calls["headers"]["X-Key-Id"] == "key-test"

    # 用户停用 system 令牌:token 为 null,视为未授权
    def _fake_get_disabled(url: str, **kwargs):
        class _Resp:
            status_code = 200

            def json(self):
                return {"success": True, "data": {"token": None}}

        return _Resp()

    monkeypatch.setattr(newapi_internal.httpx, "get", _fake_get_disabled)
    with pytest.raises(newapi_internal.NewApiInternalError, match="未授权"):
        newapi_internal.get_user_system_key("owner-casdoor-id")

    # 网络层异常(HTTPStatusError 也是 HTTPError 子类)统一转成可读错误
    def _fake_get_error(url: str, **kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(newapi_internal.httpx, "get", _fake_get_error)
    with pytest.raises(newapi_internal.NewApiInternalError, match="请求共创网关失败"):
        newapi_internal.get_user_system_key("owner-casdoor-id")


def test_owner_sees_enterprise_videos(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-v", "F")
        template = _create_template(client, "owner-v", name="模版F")
        created = client.post(
            "/api/cocreation/videos",
            json={"template_id": template["id"], "text": "企业的作品"},
            headers=_headers("owner-v"),
        )
        assert created.status_code == 200

        rows = client.get("/api/ops/v1/cocreation/videos", headers=_headers("owner-v"))
        assert rows.status_code == 200
        data = rows.json()
        assert len(data) == 1
        row = data[0]
        assert row["creator_sub"] == "owner-v"
        assert row["template_name"] == "模版F"
        assert row["input_text"] == "企业的作品"
        assert row["status"] == "pending"

        # 其他企业看不到这些视频
        _apply_and_approve(client, "owner-other", "G")
        other = client.get(
            "/api/ops/v1/cocreation/videos", headers=_headers("owner-other")
        )
        assert other.json() == []
