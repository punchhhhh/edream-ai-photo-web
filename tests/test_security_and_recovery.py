"""安全加固与可靠性恢复的行为测试(路径穿越拦截 / 魔数校验 / 重启恢复 / 看门狗 / 轮询容错)。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

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

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024


def _headers(sub: str) -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


@pytest.fixture()
def client(monkeypatch):
    # 拦掉后台线程,让任务停在 pending,便于验证恢复/看门狗逻辑
    monkeypatch.setattr("backend.routers.creations.start_creation_thread", lambda _id: None)
    from backend.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _create_config(client: TestClient, sub: str) -> dict:
    resp = client.post("/api/configs", json=CONFIG_PAYLOAD, headers=_headers(sub))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_creation(client: TestClient, sub: str, config_id: int, **overrides) -> object:
    payload = {
        "input_text": "黄昏的海边,一只橘猫追着浪花奔跑",
        "style": "电影质感",
        "expanded_prompt": "黄昏的海边,橘猫,浪花,电影质感",
        "image_source": "none",
        "config_id": config_id,
        "duration": 5,
    }
    payload.update(overrides)
    return client.post("/api/creations", json=payload, headers=_headers(sub))


# ---------------------------------------------------------------- 路径穿越拦截


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../.env",
        "/etc/passwd",
        "images/../../etc/passwd",
        "../media/videos/x.mp4",
        "media/videos/x.mp4",
        "images/a/b.png",
        "images/" + "a" * 300 + ".png",
        "images/复杂文件名.png",
    ],
)
def test_create_rejects_unsafe_image_paths(client: TestClient, bad_path: str) -> None:
    config = _create_config(client, "user-a")
    resp = _create_creation(
        client, "user-a", config["id"], image_source="uploaded", image_path=bad_path
    )
    assert resp.status_code == 422, f"{bad_path} 不应被接受"


def test_upload_then_create_roundtrip(client: TestClient) -> None:
    config = _create_config(client, "user-a")
    uploaded = client.post(
        "/api/upload", files={"file": ("a.png", PNG_BYTES, "image/png")}, headers=_headers("user-a")
    )
    assert uploaded.status_code == 200, uploaded.text
    image_path = uploaded.json()["image_path"]
    assert image_path.startswith("uploads/") and image_path.endswith(".png")
    assert uploaded.json()["url"].startswith("/api/media/")

    resp = _create_creation(
        client, "user-a", config["id"], image_source="uploaded", image_path=image_path
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 上传魔数校验


def test_upload_rejects_fake_image_bytes(client: TestClient) -> None:
    resp = client.post(
        "/api/upload",
        files={"file": ("evil.png", b"<html><script>bad()</script>", "image/png")},
        headers=_headers("user-a"),
    )
    assert resp.status_code == 422


def test_merged_rejects_non_video_bytes(client: TestClient) -> None:
    resp = client.post(
        "/api/creations/merged",
        files={"file": ("m.mp4", b"just some text bytes" * 64, "video/mp4")},
        data={"title": "假视频", "source_ids": "[]", "total_duration": "3"},
        headers=_headers("user-a"),
    )
    assert resp.status_code == 422


def test_merged_accepts_real_mp4(client: TestClient) -> None:
    resp = client.post(
        "/api/creations/merged",
        files={"file": ("m.mp4", MP4_BYTES, "video/mp4")},
        data={"title": "合成测试", "source_ids": "[]", "total_duration": "3"},
        headers=_headers("user-a"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["video_url"] and body["video_url"].startswith("/api/media/")
    assert body["video_url"].endswith(".mp4")


# ---------------------------------------------------------------- 重启恢复与看门狗


def test_recover_resumes_polled_tasks_and_fails_unsubmitted(client: TestClient, monkeypatch) -> None:
    from backend.database import SessionLocal
    from backend.models import Creation as CreationRow
    from backend.services import pipeline

    config_a = _create_config(client, "user-a")
    config_b = _create_config(client, "user-b")
    task_a = _create_creation(client, "user-a", config_a["id"]).json()
    task_b = _create_creation(client, "user-b", config_b["id"]).json()

    # 模拟重启前的状态:a 已提交到网关(有任务号),b 还没提交成功
    with SessionLocal() as db:
        row = db.get(CreationRow, task_a["id"])
        row.status = "generating_video"
        row.video_task_id = "task-live"
        db.commit()

    started: list[tuple[int, bool]] = []
    monkeypatch.setattr(pipeline, "start_creation_thread", lambda cid, resume=False: started.append((cid, resume)))
    result = pipeline.recover_interrupted_creations()

    assert result == {"resumed": 1, "failed": 1}
    assert started == [(task_a["id"], True)]
    recovered_b = client.get(f"/api/creations/{task_b['id']}", headers=_headers("user-b")).json()
    assert recovered_b["status"] == "failed"
    assert "重新提交" in recovered_b["error"]


def test_reap_marks_stale_tasks_failed(client: TestClient) -> None:
    from backend.database import SessionLocal
    from backend.models import Creation as CreationRow
    from backend.services.pipeline import reap_stale_creations

    config = _create_config(client, "user-a")
    task = _create_creation(client, "user-a", config["id"]).json()

    # 模拟线程已死:状态进行中,但心跳(updated_at)早已停止
    with SessionLocal() as db:
        row = db.get(CreationRow, task["id"])
        row.status = "generating_video"
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=99999)
        db.commit()

    assert reap_stale_creations() == 1
    body = client.get(f"/api/creations/{task['id']}", headers=_headers("user-a")).json()
    assert body["status"] == "failed"

    # 已终态的任务不会被重复回收
    assert reap_stale_creations() == 0


# ---------------------------------------------------------------- 轮询容错与重试限制


def test_run_video_tolerates_transient_poll_errors(monkeypatch) -> None:
    from backend.services.ai_client import AICallError, AIClient

    with AIClient("http://gw.test", "sk-test") as client:
        monkeypatch.setattr(client, "_submit_video", lambda *args: ("task-1", None))
        calls = {"poll": 0}

        def fake_poll(task_id: str):
            calls["poll"] += 1
            if calls["poll"] <= 2:
                raise AICallError("gateway 503")
            return "done", "https://cdn.example.test/v.mp4"

        monkeypatch.setattr(client, "_poll_video", fake_poll)
        submitted: list[str] = []
        result = client.run_video(
            "kling", "prompt", poll_interval=0, timeout_seconds=5, on_submitted=submitted.append
        )

    assert result["url"] == "https://cdn.example.test/v.mp4"
    assert submitted == ["task-1"]


def test_run_video_aborts_after_consecutive_poll_failures(monkeypatch) -> None:
    from backend.services.ai_client import AICallError, AIClient

    with AIClient("http://gw.test", "sk-test") as client:
        monkeypatch.setattr(client, "_submit_video", lambda *args: ("task-1", None))

        def fake_poll(task_id: str):
            raise AICallError("boom")

        monkeypatch.setattr(client, "_poll_video", fake_poll)
        with pytest.raises(AICallError) as exc_info:
            client.run_video("kling", "prompt", poll_interval=0, timeout_seconds=5, max_poll_errors=3)

    assert "连续失败" in str(exc_info.value)


def test_run_video_resume_skips_submit(monkeypatch) -> None:
    from backend.services.ai_client import AIClient

    with AIClient("http://gw.test", "sk-test") as client:
        monkeypatch.setattr(client, "_submit_video", lambda *args: pytest.fail("恢复模式不应重新提交"))
        monkeypatch.setattr(client, "_poll_video", lambda task_id: ("done", "https://cdn.example.test/v.mp4"))
        result = client.run_video("kling", "prompt", poll_interval=0, timeout_seconds=5, resume_task_id="old-task")

    assert result["url"] == "https://cdn.example.test/v.mp4"
    assert result["task_id"] == "old-task"


def test_submit_video_retries_only_on_parameter_errors(monkeypatch) -> None:
    from backend.services.ai_client import AICallError, AIClient

    with AIClient("http://gw.test", "sk-test") as client:
        calls = {"n": 0}

        def fail_500(method: str, path: str, **kw):
            calls["n"] += 1
            raise AICallError("模型服务返回 500", status_code=500)

        monkeypatch.setattr(client, "_request", fail_500)
        with pytest.raises(AICallError):
            client._submit_video("kling", "prompt", None, 5)
        assert calls["n"] == 1  # 5xx 不重发,避免网关重复受理

        def fail_400_then_ok(method: str, path: str, *, json=None, **kw):
            calls["n"] += 1
            if json is not None and "duration" in json:
                raise AICallError("模型服务返回 400", status_code=400)
            return {"id": "task-9"}

        monkeypatch.setattr(client, "_request", fail_400_then_ok)
        task_id, direct = client._submit_video("kling", "prompt", None, 5)
        assert (task_id, direct) == ("task-9", None)
        assert calls["n"] == 3  # 1 次 500 + 1 次 400 + 1 次降级重试成功
