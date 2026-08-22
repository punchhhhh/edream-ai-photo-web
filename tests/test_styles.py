"""风格预设测试:播种/列表接口、拓展 prompt 拼装、视频提交的负向提示词。"""

from fastapi.testclient import TestClient

from backend.services.ai_client import AIClient


def _headers(sub: str) -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


def _client() -> TestClient:
    from backend.app import create_app

    return TestClient(create_app())


def test_styles_seeded_and_ordered() -> None:
    with _client() as client:
        resp = client.get("/api/styles", headers=_headers("user-a"))
        assert resp.status_code == 200, resp.text
        styles = resp.json()
        assert len(styles) >= 10
        assert styles[0]["name"] == "电影质感"
        # 每个预设都带结构化画面语言与负向提示词,不再是光秃秃的风格词
        assert "胶片" in styles[0]["description"]
        assert styles[0]["negative_prompt"]
        sort_orders = [s["sort_order"] for s in styles]
        assert sort_orders == sorted(sort_orders)


def test_styles_seed_is_idempotent() -> None:
    from backend.style_presets import seed_default_styles

    with _client():
        pass  # lifespan 已播种一次
    assert seed_default_styles() == 0  # 再跑不重复插入


def test_expand_prompt_includes_style_description(monkeypatch) -> None:
    captured: dict = {}

    def fake_request(method: str, path: str, *, json=None, **kw):
        captured["json"] = json
        return {"choices": [{"message": {"content": "拓展后的提示词"}}]}

    with AIClient("http://gw.test", "sk-test") as client:
        monkeypatch.setattr(client, "_request", fake_request)
        out = client.expand_prompt(
            "gpt",
            "黄昏的海边,一只橘猫追着浪花奔跑",
            "电影质感",
            style_description="电影级摄影质感,浅景深虚化,35mm 胶片色彩科学",
        )

    assert out == "拓展后的提示词"
    user_content = captured["json"]["messages"][1]["content"]
    assert "期望风格:电影质感" in user_content
    assert "风格要点" in user_content and "浅景深虚化" in user_content


def test_submit_video_carries_negative_prompt(monkeypatch) -> None:
    bodies: list[dict] = []

    def fake_request(method: str, path: str, *, json=None, **kw):
        bodies.append(json)
        return {"id": "task-1"}

    with AIClient("http://gw.test", "sk-test") as client:
        monkeypatch.setattr(client, "_request", fake_request)
        task_id, direct = client._submit_video(
            "kling", "prompt", None, 5, negative_prompt="卡通,低画质"
        )

    assert (task_id, direct) == ("task-1", None)
    assert bodies[0]["negative_prompt"] == "卡通,低画质"

    # 网关不认 negative_prompt(400)时,降级重试的请求体不含该参数
    def fail_then_ok(method: str, path: str, *, json=None, **kw):
        bodies.append(json)
        if json is not None and "negative_prompt" in json:
            from backend.services.ai_client import AICallError

            raise AICallError("模型服务返回 400", status_code=400)
        return {"id": "task-2"}

    with AIClient("http://gw.test", "sk-test") as client:
        monkeypatch.setattr(client, "_request", fail_then_ok)
        task_id, _ = client._submit_video("kling", "prompt", None, 5, negative_prompt="卡通,低画质")

    assert task_id == "task-2"
    assert "negative_prompt" not in bodies[-1]
