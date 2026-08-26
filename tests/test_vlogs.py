"""多图 Vlog 的上传、规划、模型调用和成片入库测试。"""

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

CONFIG_PAYLOAD = {
    "name": "Seedance 测试网关",
    "base_url": "https://gw.example.com/v1",
    "api_key": "sk-secret-key-abcdef",
    "chat_model": "",
    "image_model": "",
    "video_model": "doubao-seedance-2.0",
    "video_provider": "video_generations",
    "is_default": True,
}
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024


def _headers(sub: str = "vlog-user") -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


def _jpeg(width: int, height: int, color: tuple[int, int, int], *, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), color)
    output = BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    image.save(output, "JPEG", quality=92, exif=exif)
    return output.getvalue()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("backend.routers.vlogs.start_vlog_thread", lambda _id: None)
    from backend.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _config(client: TestClient, sub: str = "vlog-user") -> dict:
    response = client.post("/api/configs", json=CONFIG_PAYLOAD, headers=_headers(sub))
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client: TestClient, count: int = 9, sub: str = "vlog-user") -> dict:
    files = []
    for index in range(count):
        portrait = index >= count // 2
        width, height = ((300, 500) if portrait else (500, 300))
        color = (70 + index, 120 + index, 95 + index)
        files.append(("files", (f"image-{index}.jpg", _jpeg(width, height, color), "image/jpeg")))
    response = client.post("/api/vlogs/upload", files=files, headers=_headers(sub))
    assert response.status_code == 200, response.text
    return response.json()


def _create_project(client: TestClient, upload: dict, config_id: int, sub: str = "vlog-user") -> dict:
    response = client.post(
        "/api/vlogs",
        json={
            "config_id": config_id,
            "image_paths": [image["image_path"] for image in upload["images"]],
            "style": "写实纪录",
            "description": "记录这次同行",
        },
        headers=_headers(sub),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_normalizes_orientation_and_returns_exact_plan(client: TestClient) -> None:
    files = [
        ("files", ("rotated.jpg", _jpeg(1800, 1000, (80, 130, 100), orientation=6), "image/jpeg")),
        ("files", ("portrait.jpg", _jpeg(300, 500, (82, 132, 102)), "image/jpeg")),
    ]
    response = client.post("/api/vlogs/upload", files=files, headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["images"]) == 2
    assert body["images"][0]["height"] > body["images"][0]["width"]
    assert max(body["images"][0]["width"], body["images"][0]["height"]) == 1600
    assert body["ratio"] == "9:16"
    assert body["target_duration"] == sum(clip["duration"] for clip in body["clips"])
    assert len(body["clips"]) == 1


def test_nine_images_plan_three_scenes_and_variable_duration(client: TestClient) -> None:
    upload = _upload(client)
    config = _config(client)
    project = _create_project(client, upload, config["id"])

    assert upload["ratio"] == "9:16"
    assert len(upload["clips"]) == 3
    assert upload["target_duration"] == 30
    assert len(project["clips"]) == 3
    assert [clip["duration"] for clip in project["clips"]] == [10, 10, 10]
    assert project["target_duration"] == 30
    assert client.get("/api/vlogs/latest", headers=_headers()).json()["id"] == project["id"]


def test_plan_and_create_reject_another_users_images(client: TestClient) -> None:
    upload = _upload(client, count=2, sub="owner")
    paths = [image["image_path"] for image in upload["images"]]
    plan = client.post("/api/vlogs/plan", json={"image_paths": paths}, headers=_headers("intruder"))
    assert plan.status_code == 422

    config = _config(client, "intruder")
    create = client.post(
        "/api/vlogs",
        json={"config_id": config["id"], "image_paths": paths, "style": "写实纪录"},
        headers=_headers("intruder"),
    )
    assert create.status_code == 422


def test_seedance_multi_image_payload_contains_references_and_media_options(monkeypatch) -> None:
    from backend.services.ai_client import AIClient

    with AIClient("http://gw.test", "sk-test") as ai:
        captured: list[dict] = []

        def request(method: str, path: str, **kwargs):
            captured.append(kwargs["json"])
            return {"id": "task-vlog"}

        monkeypatch.setattr(ai, "_request", request)
        task_id, direct = ai._submit_video(
            "doubao-seedance-2.0",
            "prompt",
            None,
            10,
            image_inputs=[(b"one", "image/jpeg"), (b"two", "image/jpeg")],
            ratio="9:16",
            resolution="720p",
            generate_audio=True,
        )

    assert (task_id, direct) == ("task-vlog", None)
    assert len(captured) == 1
    body = captured[0]
    assert len(body["images"]) == 2
    assert len(body["metadata"]["content"]) == 2
    assert body["metadata"]["ratio"] == "9:16"
    assert body["metadata"]["resolution"] == "720p"
    assert body["metadata"]["generate_audio"] is True


def test_pipeline_generates_all_scenes_strictly_in_order(client: TestClient, monkeypatch) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogClip, VlogProject
    from backend.services import vlog_pipeline

    upload = _upload(client)
    config = _config(client)
    project = _create_project(client, upload, config["id"])
    calls: list[dict] = []

    class FakeAIClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run_video(self, model, prompt, **kwargs):
            calls.append(kwargs)
            kwargs["on_submitted"](f"task-{len(calls)}")
            return {"task_id": f"task-{len(calls)}", "content": MP4_BYTES}

    monkeypatch.setattr(vlog_pipeline, "AIClient", FakeAIClient)
    vlog_pipeline._run_vlog(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        clips = db.scalars(
            select(VlogClip).where(VlogClip.project_id == row.id).order_by(VlogClip.sequence)
        ).all()
        assert row.status == "ready_to_merge"
        assert [clip.status for clip in clips] == ["completed", "completed", "completed"]
        assert [clip.video_task_id for clip in clips] == ["task-1", "task-2", "task-3"]

    assert len(calls) == 3
    assert [call["duration"] for call in calls] == [10, 10, 10]
    assert all(call["ratio"] == "9:16" and call["resolution"] == "720p" for call in calls)
    assert all(call["generate_audio"] is True for call in calls)


def test_failed_scene_allows_only_one_manual_retry(client: TestClient) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogClip, VlogProject
    upload = _upload(client, count=4)
    config = _config(client)
    project = _create_project(client, upload, config["id"])
    clip_id = project["clips"][0]["id"]
    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        clip = db.get(VlogClip, clip_id)
        row.status = "failed"
        clip.status = "failed"
        db.commit()

    first = client.post(f"/api/vlogs/{project['id']}/clips/{clip_id}/retry", headers=_headers())
    assert first.status_code == 200, first.text
    assert first.json()["clips"][0]["retry_count"] == 1

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        clip = db.scalar(select(VlogClip).where(VlogClip.id == clip_id))
        row.status = "failed"
        clip.status = "failed"
        db.commit()
    second = client.post(f"/api/vlogs/{project['id']}/clips/{clip_id}/retry", headers=_headers())
    assert second.status_code == 409


def test_complete_vlog_creates_history_record(client: TestClient) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogClip, VlogProject
    upload = _upload(client, count=3)
    config = _config(client)
    project = _create_project(client, upload, config["id"])
    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.status = "ready_to_merge"
        for clip in db.scalars(select(VlogClip).where(VlogClip.project_id == row.id)):
            clip.status = "completed"
        db.commit()

    response = client.post(
        f"/api/vlogs/{project['id']}/complete",
        files={"file": ("vlog.mp4", MP4_BYTES, "video/mp4")},
        data={"actual_duration": "19"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["final_video_url"].startswith("/api/media/")
    assert body["final_creation_id"]

    history = client.get("/api/creations", headers=_headers()).json()
    assert history[0]["id"] == body["final_creation_id"]
    assert history[0]["image_source"] == "merged"
    assert history[0]["duration"] == 19
