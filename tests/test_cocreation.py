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


def _entry_token(client: TestClient, owner_sub: str) -> str:
    from urllib.parse import parse_qs, urlparse

    created = client.post(
        "/api/ops/v1/enterprise-entry",
        headers={**_headers(owner_sub), "Origin": "http://127.0.0.1:5173"},
    )
    assert created.status_code == 200, created.text
    return parse_qs(urlparse(created.json()["entry_url"]).query)["enterprise_entry"][0]


def _grant_id(client: TestClient, owner_sub: str, user_sub: str | None = None) -> int:
    token = _entry_token(client, owner_sub)
    preview = client.get(f"/api/enterprise-entry/preview?token={token}")
    assert preview.status_code == 200, preview.text
    applied = client.post(
        "/api/enterprise-entry/apply",
        json={
            "token": token,
            "accepted": True,
            "terms_version": preview.json()["terms_version"],
            "privacy_version": preview.json()["privacy_version"],
        },
        headers=_headers(user_sub or owner_sub),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "active"
    return applied.json()["id"]


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
        assert "企业专属入口" in body["reason"]

        # 网关未配置时,即使企业已认证也不可用
        _apply_and_approve(client, "owner-y", "B")
        _create_template(client, "owner-y")
        grant_id = _grant_id(client, "owner-y")
        monkeypatch.setattr(settings, "new_api_base_url", "")
        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-y")
        )
        assert status.json()["available"] is False
        assert "共创服务" in status.json()["reason"]

        # 配置齐全 + 有模版 → 可用,并返回模版与配额
        monkeypatch.setattr(settings, "new_api_base_url", "https://gateway.example.com")
        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-y")
        )
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
        grant_id = _grant_id(client, "owner-tab")

        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-tab")
        )
        assert status.status_code == 200
        body = status.json()
        assert body["available"] is True
        assert [t["name"] for t in body["templates"]] == ["Owner模版"]

        # 不止能看到,Owner 也能直接提交共创视频(配额同样生效)
        created = client.post(
            "/api/cocreation/videos",
            json={
                "grant_id": grant_id,
                "template_id": body["templates"][0]["id"],
                "text": "Owner 的共创创意",
            },
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
        grant_id = _grant_id(client, "owner-z")

        created = client.post(
            "/api/cocreation/videos",
            json={
                "grant_id": grant_id,
                "template_id": template["id"],
                "text": "一只橘猫在海边奔跑",
            },
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
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-z")
        ).json()
        assert status_body["used"] == 1
        assert status_body["limit"] == 2

        # 任务完成后才能提交下一个(同用户同时只允许一个生成中任务)
        _finish_creation(body["id"], "completed")
        second = client.post(
            "/api/cocreation/videos",
            json={
                "template_id": template["id"],
                "grant_id": grant_id,
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
            json={"grant_id": grant_id, "template_id": template["id"], "text": "创意三"},
            headers=_headers("owner-z"),
        )
        assert third.status_code == 403
        assert "视频次数已用完" in third.json()["detail"]

        # 视频次数记在授权上，删除内容不返还；直接改库的终态不经过管线也不返还
        # (真实失败路径的自动返还见 test_failed_creation_refunds_quota)。
        deleted = client.delete(
            f"/api/creations/{body['id']}", headers=_headers("owner-z")
        )
        assert deleted.status_code == 200
        _finish_creation(second.json()["id"], "failed")
        retry = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "创意三"},
            headers=_headers("owner-z"),
        )
        assert retry.status_code == 403


def test_failed_creation_refunds_quota(gateway_ready, monkeypatch) -> None:
    """任务失败自动返还共创次数:used 回落、因用尽而耗尽的授权恢复可用、可直接重试。"""
    from backend.app import create_app
    from backend.database import SessionLocal
    from backend.models import Creation
    from backend.services import newapi_internal
    from backend.services import pipeline as creation_pipeline
    from backend.services.ai_client import AICallError
    from backend.services.enterprise_access import refund_video_grant

    class _FailingVideoClient:
        def __init__(self, base_url, api_key, provider="video_generations"):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run_video(self, model, prompt, **kwargs):
            raise AICallError("网关生成失败")

    monkeypatch.setattr(creation_pipeline, "AIClient", _FailingVideoClient)
    monkeypatch.setattr(
        newapi_internal, "get_user_system_key", lambda oidc_id, **kwargs: "sk-system-key"
    )
    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )
    monkeypatch.setattr(settings, "cocreation_max_videos_per_user", 1)

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-refund", "S")
        template = _create_template(client, "owner-refund")
        grant_id = _grant_id(client, "owner-refund")

        created = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "会失败的创意"},
            headers=_headers("owner-refund"),
        )
        assert created.status_code == 200, created.text
        creation_id = created.json()["id"]

        # 提交即扣减:次数用尽,授权变为不可用
        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-refund")
        ).json()
        assert status["used"] == 1
        assert status["available"] is False

        # 走真实管线失败路径(_run → _finish("failed")),次数自动返还
        creation_pipeline._run(creation_id, resume=False)

        with SessionLocal() as db:
            assert db.get(Creation, creation_id).status == "failed"

        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-refund")
        ).json()
        assert status["used"] == 0
        assert status["available"] is True

        # 返还幂等:同一任务不会重复返还
        with SessionLocal() as db:
            assert (
                refund_video_grant(db, grant_id=grant_id, creation_id=creation_id) is False
            )

        # 次数回来后可以直接重试
        retry = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "再试一次"},
            headers=_headers("owner-refund"),
        )
        assert retry.status_code == 200, retry.text


def test_reap_stale_creations_refunds_cocreation(gateway_ready, monkeypatch) -> None:
    """看门狗回收无心跳的共创任务时同样返还次数。"""
    from datetime import datetime, timedelta, timezone

    from backend.app import create_app
    from backend.database import SessionLocal
    from backend.models import Creation
    from backend.services import pipeline as creation_pipeline

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-stale", "W")
        template = _create_template(client, "owner-stale")
        grant_id = _grant_id(client, "owner-stale")
        created = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "卡死的创意"},
            headers=_headers("owner-stale"),
        )
        assert created.status_code == 200, created.text

        # 模拟线程已死:心跳(updated_at)停在看门狗阈值之前
        with SessionLocal() as db:
            row = db.get(Creation, created.json()["id"])
            row.status = "generating_video"
            row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=99999)
            db.commit()

        assert creation_pipeline.reap_stale_creations() == 1

        with SessionLocal() as db:
            row = db.get(Creation, created.json()["id"])
            assert row.status == "failed"
            assert "返还" in row.error

        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-stale")
        ).json()
        assert status["used"] == 0
        assert status["available"] is True


def test_create_video_rejects_when_other_task_running(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-r", "D")
        template = _create_template(client, "owner-r")
        grant_id = _grant_id(client, "owner-r")
        first = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "第一条"},
            headers=_headers("owner-r"),
        )
        assert first.status_code == 200
        # 同用户同时只允许一个生成中任务(与个人创作共用约束)
        second = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "第二条"},
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

    def _fake_get_key(oidc_id: str, **kwargs) -> str:
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
        grant_id = _grant_id(client, "owner-g")
        created = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "创意"},
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
        grant_id = _grant_id(client, "owner-cfg")
        created = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "创意"},
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
        newapi_internal, "get_user_system_key", lambda oidc_id, **kwargs: "sk-system-key"
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


def test_newapi_model_catalog_uses_casdoor_owner_key(monkeypatch) -> None:
    from backend.services import newapi_internal

    monkeypatch.setattr(settings, "new_api_base_url", "https://gateway.example.com/")
    captured: dict[str, object] = {}

    def _fake_key(oidc_sub: str, **kwargs) -> str:
        captured["oidc_sub"] = oidc_sub
        return "sk-owner-system"

    def _fake_get(url: str, *, headers=None, **kwargs):
        captured.update({"url": url, "headers": headers})

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "data": [
                        {
                            "id": "deepseek-v4-flash",
                            "supported_endpoint_types": ["openai"],
                        },
                        {
                            "id": "gpt-image-2",
                            "supported_endpoint_types": ["openai"],
                        },
                        {
                            "id": "wan3_720p",
                            "supported_endpoint_types": ["openai-video"],
                        },
                        {
                            "id": "veo_3_1_fast",
                            "supported_endpoint_types": ["veo"],
                        },
                    ]
                }

        return _Resp()

    monkeypatch.setattr(newapi_internal, "get_user_system_key", _fake_key)
    monkeypatch.setattr(newapi_internal.httpx, "get", _fake_get)
    models = newapi_internal.list_user_models("enterprise-owner")

    assert captured["oidc_sub"] == "enterprise-owner"
    assert captured["url"] == "https://gateway.example.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-owner-system"
    by_id = {model.id: model for model in models}
    assert by_id["deepseek-v4-flash"].kind == "text"
    assert by_id["gpt-image-2"].kind == "image"
    assert by_id["wan3_720p"].kind == "video"
    assert by_id["veo_3_1_fast"].kind == "video"
    assert by_id["wan3_720p"].video_provider == "openai_videos"


def test_owner_can_fetch_newapi_model_catalog(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app
    from backend.services import newapi_internal

    captured: dict[str, str] = {}

    def _fake_models(oidc_sub: str):
        captured["oidc_sub"] = oidc_sub
        return [
            newapi_internal.AvailableModel(
                id="wan3_720p",
                kind="video",
                endpoint_types=("openai-video",),
                video_provider="openai_videos",
            )
        ]

    monkeypatch.setattr(newapi_internal, "list_user_models", _fake_models)
    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-models", "MODELS")
        response = client.get(
            "/api/ops/v1/new-api-models", headers=_headers("owner-models")
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert captured["oidc_sub"] == "owner-models"
        assert body["models"] == [
            {
                "id": "wan3_720p",
                "kind": "video",
                "endpoint_types": ["openai-video"],
                "video_provider": "openai_videos",
            }
        ]
        assert body["default_video_model"] == settings.new_api_default_video_model

        forbidden = client.get(
            "/api/ops/v1/new-api-models", headers=_headers("someone-else")
        )
        assert forbidden.status_code == 403


def test_video_template_provider_is_inferred_from_model(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app
    from backend.services import newapi_internal

    monkeypatch.setattr(
        newapi_internal,
        "list_user_models",
        lambda _sub: [
            newapi_internal.AvailableModel(
                id="wan3_720p",
                kind="video",
                endpoint_types=("openai-video",),
                video_provider="openai_videos",
            )
        ],
    )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-provider", "PROVIDER")
        created = _create_template(
            client,
            "owner-provider",
            video_model="wan3_720p",
            video_provider="video_generations",
        )
        assert created["video_provider"] == "openai_videos"

        updated = client.patch(
            f"/api/ops/v1/video-templates/{created['id']}",
            json={"video_provider": "video_generations"},
            headers=_headers("owner-provider"),
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["video_provider"] == "openai_videos"


def test_owner_sees_enterprise_videos(gateway_ready, monkeypatch) -> None:
    from backend.app import create_app

    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-v", "F")
        template = _create_template(client, "owner-v", name="模版F")
        grant_id = _grant_id(client, "owner-v")
        created = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "企业的作品"},
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


# ---------------------------------------------------------------- 互动剧本与素材绑定


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"c" * 64


def _upload_image_asset(
    client: TestClient, sub: str, name: str = "IP形象图.png"
) -> dict:
    resp = client.post(
        "/api/ops/v1/assets/file",
        data={"purpose": "ip_visual", "name": name},
        files={"file": (name, _png_bytes(), "image/png")},
        headers=_headers(sub),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_text_asset(client: TestClient, sub: str) -> dict:
    resp = client.post(
        "/api/ops/v1/assets/text",
        json={
            "name": "IP特征卡",
            "purpose": "ip_setting",
            "text_content": "橙色小猫IP,戴蓝色围巾,圆眼睛",
            "tags": [],
            "description": "",
        },
        headers=_headers(sub),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_template_interaction_fields_and_assets(gateway_ready) -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-b1", "K")
        image_asset = _upload_image_asset(client, "owner-b1")
        text_asset = _create_text_asset(client, "owner-b1")

        template = _create_template(
            client,
            "owner-b1",
            member_photo="required",
            member_photo_hint="请上传正脸照",
            first_frame_prompt="咖啡馆门口与 IP 合影",
            interaction_options=["一起比心", "跳一段开工舞"],
            assets=[
                {"asset_id": image_asset["id"], "usage": "character_reference"},
                {"asset_id": image_asset["id"], "usage": "cover"},
                {"asset_id": text_asset["id"], "usage": "prompt_text"},
            ],
        )
        grant_id = _grant_id(client, "owner-b1")
        assert template["member_photo"] == "required"
        assert template["first_frame_confirm"] is True
        assert template["interaction_options"] == ["一起比心", "跳一段开工舞"]
        usages = {(a["asset_id"], a["usage"]) for a in template["assets"]}
        assert (image_asset["id"], "character_reference") in usages
        assert (text_asset["id"], "prompt_text") in usages
        assert template["cover_url"] == f"/api/ops/v1/assets/{image_asset['id']}/content"

        # 成员端状态接口带派生信息:封面地址、形象参考图数量
        status = client.get(
            f"/api/cocreation/status?grant_id={grant_id}", headers=_headers("owner-b1")
        ).json()
        row = status["templates"][0]
        assert row["character_asset_count"] == 1
        assert row["member_photo"] == "required"
        assert row["member_photo_hint"] == "请上传正脸照"
        assert row["cover_url"] == (
            f"/api/cocreation/templates/{template['id']}/cover?grant_id={grant_id}"
        )

        # 封面由成员鉴权后按绑定读取企业素材
        cover = client.get(row["cover_url"], headers=_headers("owner-b1"))
        assert cover.status_code == 200
        assert cover.content == _png_bytes()
        # 非本企业成员不能读
        assert client.get(row["cover_url"], headers=_headers("stranger-x")).status_code == 404

        # 文字素材不能绑成形象参考
        resp = client.post(
            "/api/ops/v1/video-templates",
            json={
                "name": "坏模版",
                "video_model": "m",
                "assets": [{"asset_id": text_asset["id"], "usage": "character_reference"}],
            },
            headers=_headers("owner-b1"),
        )
        assert resp.status_code == 422

        # 更新整体替换绑定
        updated = client.patch(
            f"/api/ops/v1/video-templates/{template['id']}",
            json={"assets": []},
            headers=_headers("owner-b1"),
        )
        assert updated.status_code == 200
        assert updated.json()["assets"] == []
        assert updated.json()["cover_url"] is None


def test_first_frame_compose_and_video_submit(gateway_ready, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_compose(self, model, prompt, image_inputs, size="1280x720"):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["inputs"] = image_inputs
        return _png_bytes(), ".png"

    monkeypatch.setattr(
        "backend.services.ai_client.AIClient.compose_image", _fake_compose
    )
    monkeypatch.setattr(
        "backend.routers.cocreation.start_creation_thread", lambda *a, **k: None
    )
    # owner 无默认模型配置时按 Casdoor 标识实时取 system 密钥,测试里 mock 掉
    from backend.services import newapi_internal

    monkeypatch.setattr(
        newapi_internal, "get_user_system_key", lambda oidc_id, **kwargs: "sk-system-key"
    )

    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-ff", "L")
        image_asset = _upload_image_asset(client, "owner-ff")
        text_asset = _create_text_asset(client, "owner-ff")
        template = _create_template(
            client,
            "owner-ff",
            name="合拍模版",
            first_frame_prompt="海边与 IP 合影",
            image_model="gpt-image-2",
            member_photo="required",
            assets=[
                {"asset_id": image_asset["id"], "usage": "character_reference"},
                {"asset_id": text_asset["id"], "usage": "prompt_text"},
            ],
        )
        grant_id = _grant_id(client, "owner-ff")

        # 出镜模版未合成首帧直接提交会被拦
        early = client.post(
            "/api/cocreation/videos",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "创意"},
            headers=_headers("owner-ff"),
        )
        assert early.status_code == 422
        assert "出镜" in early.json()["detail"]

        # 未上传照片不能合成首帧
        no_photo = client.post(
            "/api/cocreation/first-frame",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "打招呼"},
            headers=_headers("owner-ff"),
        )
        assert no_photo.status_code == 422

        # 上传成员照片后合成首帧:成员照片 + IP 参考图一起进模型
        photo = client.post(
            "/api/upload",
            files={"file": ("me.png", _png_bytes(), "image/png")},
            headers=_headers("owner-ff"),
        )
        assert photo.status_code == 200, photo.text
        frame = client.post(
            "/api/cocreation/first-frame",
            json={
                "template_id": template["id"],
                "grant_id": grant_id,
                "text": "打招呼",
                "member_photo_path": photo.json()["image_path"],
            },
            headers=_headers("owner-ff"),
        )
        assert frame.status_code == 200, frame.text
        frame_path = frame.json()["image_path"]
        assert frame_path.startswith("users/")
        assert len(captured["inputs"]) == 2
        assert captured["model"] == "gpt-image-2"
        assert "海边与 IP 合影" in captured["prompt"]
        assert "橙色小猫IP" in captured["prompt"]

        # 不能引用别人的图片
        stolen = client.post(
            "/api/cocreation/videos",
            json={
                "template_id": template["id"],
                "grant_id": grant_id,
                "text": "创意",
                "first_frame_path": "users/99999/images/x.png",
            },
            headers=_headers("owner-ff"),
        )
        assert stolen.status_code == 422

        created = client.post(
            "/api/cocreation/videos",
            json={
                "template_id": template["id"],
                "grant_id": grant_id,
                "text": "和 IP 打招呼",
                "first_frame_path": frame_path,
            },
            headers=_headers("owner-ff"),
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["image_source"] == "generated"
        assert body["image_path"] == frame_path
        assert body["image_url"]
        # 模版画面要求在最前,IP 特征文字注入,成员创意最后
        assert body["expanded_prompt"].startswith("画面需出现品牌 Logo,暖色调")
        assert "橙色小猫IP" in body["expanded_prompt"]
        assert body["expanded_prompt"].endswith("和 IP 打招呼")

        # 素材版本快照落库,便于追溯
        from backend.database import SessionLocal
        from backend.models import Creation

        with SessionLocal() as db:
            row = db.get(Creation, body["id"])
            assert row.cocreation_materials
            assert {item["usage"] for item in row.cocreation_materials} == {
                "character_reference",
                "prompt_text",
            }


def test_first_frame_guardrails(gateway_ready, monkeypatch) -> None:
    """首帧合成的输入校验与每日限流:非法画幅/非图片路径 422,超每日限额 429。"""
    monkeypatch.setattr(
        "backend.services.ai_client.AIClient.compose_image",
        lambda self, model, prompt, image_inputs, size="1280x720": (_png_bytes(), ".png"),
    )
    # owner 无默认模型配置时按 Casdoor 标识实时取 system 密钥,测试里 mock 掉
    from backend.services import newapi_internal

    monkeypatch.setattr(
        newapi_internal, "get_user_system_key", lambda oidc_id, **kwargs: "sk-system-key"
    )
    monkeypatch.setattr(settings, "cocreation_first_frame_daily_limit", 2)

    from backend.services import cocreation as cocreation_service

    cocreation_service._first_frame_counters.clear()

    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-rl", "M")
        image_asset = _upload_image_asset(client, "owner-rl")
        template = _create_template(
            client,
            "owner-rl",
            assets=[{"asset_id": image_asset["id"], "usage": "character_reference"}],
        )
        grant_id = _grant_id(client, "owner-rl")

        # 画幅只认 宽x高 数字格式
        bad_size = client.post(
            "/api/cocreation/first-frame",
            json={"grant_id": grant_id, "template_id": template["id"], "size": "biggest"},
            headers=_headers("owner-rl"),
        )
        assert bad_size.status_code == 422

        # 本人目录下也只能引用图片文件,视频等非图片路径被拒
        photo = client.post(
            "/api/upload",
            files={"file": ("me.png", _png_bytes(), "image/png")},
            headers=_headers("owner-rl"),
        )
        assert photo.status_code == 200, photo.text
        own_prefix = photo.json()["image_path"].rsplit("/uploads/", 1)[0]
        bad_ext = client.post(
            "/api/cocreation/first-frame",
            json={
                "template_id": template["id"],
                "grant_id": grant_id,
                "member_photo_path": f"{own_prefix}/videos/fake.mp4",
            },
            headers=_headers("owner-rl"),
        )
        assert bad_ext.status_code == 422
        assert "图片" in bad_ext.json()["detail"]

        # 输入校验失败的请求不占每日额度;额度用尽后 429
        for _ in range(2):
            ok = client.post(
                "/api/cocreation/first-frame",
                json={"grant_id": grant_id, "template_id": template["id"], "text": "打招呼"},
                headers=_headers("owner-rl"),
            )
            assert ok.status_code == 200, ok.text
        limited = client.post(
            "/api/cocreation/first-frame",
            json={"grant_id": grant_id, "template_id": template["id"], "text": "打招呼"},
            headers=_headers("owner-rl"),
        )
        assert limited.status_code == 429


def test_template_character_reference_binding_cap(gateway_ready) -> None:
    """形象参考图绑定数超过首帧合成的模型输入上限时,保存模版直接 422。"""
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-cap", "N")
        assets = [
            _upload_image_asset(client, "owner-cap", name=f"IP形象{i}.png") for i in range(4)
        ]
        resp = client.post(
            "/api/ops/v1/video-templates",
            json={
                "name": "超量模版",
                "video_model": "m",
                "assets": [
                    {"asset_id": a["id"], "usage": "character_reference"} for a in assets
                ],
            },
            headers=_headers("owner-cap"),
        )
        assert resp.status_code == 422
        assert "形象参考图" in resp.json()["detail"]


def test_entry_creates_temporary_grant_without_membership(gateway_ready) -> None:
    """入口访客确认说明后获得临时授权，但不会成为企业成员。"""
    from sqlalchemy import select

    from backend.app import create_app
    from backend.database import SessionLocal
    from backend.models import EnterpriseMembership, User

    def _membership(sub: str, enterprise_id: int) -> EnterpriseMembership | None:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.oauth_sub == sub))
            assert user is not None
            return db.scalar(
                select(EnterpriseMembership).where(
                    EnterpriseMembership.user_id == user.id,
                    EnterpriseMembership.enterprise_id == enterprise_id,
                )
            )

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-aj", "O")
        _create_template(client, "owner-aj")
        token = _entry_token(client, "owner-aj")

        preview = client.get(f"/api/enterprise-entry/preview?token={token}")
        assert preview.status_code == 200
        assert preview.json()["enterprise_name"] == "共创企业O"
        assert preview.json()["video_limit"] == settings.cocreation_max_videos_per_user

        # 必须确认当前版本的服务与隐私说明。
        denied = client.post(
            "/api/enterprise-entry/apply",
            json={
                "token": token,
                "accepted": False,
                "terms_version": preview.json()["terms_version"],
                "privacy_version": preview.json()["privacy_version"],
            },
            headers=_headers("visitor-x"),
        )
        assert denied.status_code == 422

        applied = client.post(
            "/api/enterprise-entry/apply",
            json={
                "token": token,
                "accepted": True,
                "terms_version": preview.json()["terms_version"],
                "privacy_version": preview.json()["privacy_version"],
            },
            headers=_headers("visitor-x"),
        )
        assert applied.status_code == 200, applied.text
        grant = applied.json()
        assert grant["status"] == "active"
        assert grant["video_remaining"] == settings.cocreation_max_videos_per_user
        enterprise_id = grant["enterprise_id"]
        assert _membership("visitor-x", enterprise_id) is None

        access = client.get(
            f"/api/enterprise-entry/access-context?token={token}",
            headers=_headers("visitor-x"),
        )
        assert access.status_code == 200
        assert access.json()["grant"]["id"] == grant["id"]

        status = client.get(
            f"/api/cocreation/status?grant_id={grant['id']}",
            headers=_headers("visitor-x"),
        ).json()
        assert status["available"] is True
        assert status["templates"]

        # 授权不能被其他登录用户冒用。
        other_status = client.get(
            f"/api/cocreation/status?grant_id={grant['id']}",
            headers=_headers("visitor-y"),
        ).json()
        assert other_status["available"] is False

        # 有效期内重复申请幂等；企业侧可以查看并撤销。
        again = client.post(
            "/api/enterprise-entry/apply",
            json={
                "token": token,
                "accepted": True,
                "terms_version": preview.json()["terms_version"],
                "privacy_version": preview.json()["privacy_version"],
            },
            headers=_headers("visitor-x"),
        )
        assert again.json()["id"] == grant["id"]

        listed = client.get(
            "/api/ops/v1/cocreation-grants", headers=_headers("owner-aj")
        )
        assert listed.status_code == 200
        assert listed.json()[0]["oauth_sub"] == "visitor-x"
        revoked = client.post(
            f"/api/ops/v1/cocreation-grants/{grant['id']}/revoke",
            json={"reason": "活动结束"},
            headers=_headers("owner-aj"),
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        assert client.get(
            f"/api/cocreation/status?grant_id={grant['id']}",
            headers=_headers("visitor-x"),
        ).json()["available"] is False

        # 撤销后用户重新确认会生成新授权记录，历史仍保留。
        reapplied = client.post(
            "/api/enterprise-entry/apply",
            json={
                "token": token,
                "accepted": True,
                "terms_version": preview.json()["terms_version"],
                "privacy_version": preview.json()["privacy_version"],
            },
            headers=_headers("visitor-x"),
        )
        assert reapplied.status_code == 200
        assert reapplied.json()["id"] != grant["id"]
        assert _membership("visitor-x", enterprise_id) is None

        # 非 Owner 不能读取或管理企业授权列表。
        assert client.patch(
            "/api/ops/v1/enterprise-entry",
            json={"video_limit": 9},
            headers=_headers("visitor-x"),
        ).status_code == 403
        disabled = client.delete("/api/ops/v1/enterprise-entry", headers=_headers("owner-aj"))
        assert disabled.json()["active"] is False
        assert client.get(f"/api/enterprise-entry/preview?token={token}").status_code == 404


def test_expired_grant_requires_new_consent_period(gateway_ready) -> None:
    from datetime import datetime, timedelta, timezone

    from backend.app import create_app
    from backend.database import SessionLocal
    from backend.models import EnterpriseConsumerGrant

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-expire", "P")
        _create_template(client, "owner-expire")
        token = _entry_token(client, "owner-expire")
        preview = client.get(f"/api/enterprise-entry/preview?token={token}").json()
        first = client.post(
            "/api/enterprise-entry/apply",
            json={
                "token": token,
                "accepted": True,
                "terms_version": preview["terms_version"],
                "privacy_version": preview["privacy_version"],
            },
            headers=_headers("visitor-expire"),
        ).json()

        with SessionLocal() as db:
            grant = db.get(EnterpriseConsumerGrant, first["id"])
            grant.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.commit()

        status = client.get(
            f"/api/cocreation/status?grant_id={first['id']}",
            headers=_headers("visitor-expire"),
        ).json()
        assert status["available"] is False
        assert "到期" in status["reason"]
        blocked = client.post(
            "/api/cocreation/videos",
            json={"grant_id": first["id"], "template_id": 1, "text": "过期请求"},
            headers=_headers("visitor-expire"),
        )
        assert blocked.status_code == 403

        renewed = client.post(
            "/api/enterprise-entry/apply",
            json={
                "token": token,
                "accepted": True,
                "terms_version": preview["terms_version"],
                "privacy_version": preview["privacy_version"],
            },
            headers=_headers("visitor-expire"),
        )
        assert renewed.status_code == 200
        assert renewed.json()["status"] == "active"
        assert renewed.json()["id"] != first["id"]


def test_grant_cannot_cross_enterprise_templates(gateway_ready) -> None:
    from backend.app import create_app

    with TestClient(create_app()) as client:
        _apply_and_approve(client, "owner-one", "Q1")
        template_one = _create_template(client, "owner-one")
        grant_one = _grant_id(client, "owner-one", "shared-visitor")

        _apply_and_approve(client, "owner-two", "Q2")
        template_two = _create_template(client, "owner-two")
        grant_two = _grant_id(client, "owner-two", "shared-visitor")

        assert template_one["id"] != template_two["id"]
        crossed = client.post(
            "/api/cocreation/videos",
            json={
                "grant_id": grant_one,
                "template_id": template_two["id"],
                "text": "尝试跨企业使用模版",
            },
            headers=_headers("shared-visitor"),
        )
        assert crossed.status_code == 404
        assert client.get(
            f"/api/cocreation/templates/{template_one['id']}/cover?grant_id={grant_two}",
            headers=_headers("shared-visitor"),
        ).status_code == 404
