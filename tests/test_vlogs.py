"""多图 Vlog 的上传、规划、模型调用和成片入库测试。"""

import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from backend.services.vlog_transitions import VLOG_TRANSITIONS

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


def test_latest_vlog_returns_empty_value_when_user_has_no_projects(client: TestClient) -> None:
    response = client.get("/api/vlogs/latest", headers=_headers("new-vlog-user"))

    assert response.status_code == 200, response.text
    assert response.json() is None


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
    assert project["transition_style"] == "fade"
    assert client.get("/api/vlogs/latest", headers=_headers()).json()["id"] == project["id"]


def test_create_vlog_accepts_nine_images_per_group_and_uses_group_description(client: TestClient) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogClip

    upload = _upload(client, count=9)
    config = _config(client)
    response = client.post(
        "/api/vlogs",
        json={
            "config_id": config["id"],
            "image_paths": [image["image_path"] for image in upload["images"]],
            "image_groups": [[image["image_path"] for image in upload["images"]]],
            "image_group_descriptions": ["镜头沿山路跟拍骑行者进入山谷"],
            "style": "写实纪录",
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    project = response.json()
    assert len(project["clips"]) == 1

    with SessionLocal() as db:
        clip = db.get(VlogClip, project["clips"][0]["id"])
        assert clip is not None
        assert "镜头沿山路跟拍骑行者进入山谷" in clip.prompt


def test_create_vlog_rejects_overlong_group_description(client: TestClient) -> None:
    upload = _upload(client, count=1)
    config = _config(client)
    path = upload["images"][0]["image_path"]
    response = client.post(
        "/api/vlogs",
        json={
            "config_id": config["id"],
            "image_paths": [path],
            "image_groups": [[path]],
            "image_group_descriptions": ["x" * 501],
            "style": "写实纪录",
        },
        headers=_headers(),
    )
    assert response.status_code == 422


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
    assert history[0]["vlog_project_id"] == project["id"]


def _upload_video_asset(client: TestClient, sub: str = "vlog-user") -> dict:
    response = client.post(
        "/api/vlogs/assets/video",
        files={"file": ("clip.mp4", MP4_BYTES, "video/mp4")},
        headers=_headers(sub),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_local_vlog(client: TestClient, timeline: list[dict], sub: str = "vlog-user") -> dict:
    response = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "style": "写实纪录",
            "description": "本地合成",
            "transition_style": "fade",
            "timeline_data": timeline,
        },
        headers=_headers(sub),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _complete_vlog(client: TestClient, project_id: int, sub: str = "vlog-user") -> dict:
    response = client.post(
        f"/api/vlogs/{project_id}/complete",
        files={"file": ("final.mp4", MP4_BYTES, "video/mp4")},
        data={"actual_duration": "9"},
        headers=_headers(sub),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_vlog_video_asset_rejects_non_video_content(client: TestClient) -> None:
    response = client.post(
        "/api/vlogs/assets/video",
        files={"file": ("fake.mp4", b"this is definitely not a video", "video/mp4")},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_create_local_vlog_roundtrips_timeline_and_completes(client: TestClient) -> None:
    upload = _upload(client, count=1)
    image_path = upload["images"][0]["image_path"]
    asset = _upload_video_asset(client)

    project = _create_local_vlog(client, [
        {"id": "g-1", "mode": "motion", "image_path": image_path, "duration": 4, "motion_template": "drift"},
        {"id": "g-2", "mode": "video", "source": "upload", "asset_path": asset["asset_path"], "duration": 8, "name": "骑行.mp4", "mime": "video/mp4"},
    ])

    assert project["status"] == "ready_to_merge"
    assert project["clips"] == []
    assert project["target_duration"] == 12
    assert project["image_paths"] == [image_path]
    timeline = project["timeline_data"]
    assert timeline[0]["image_url"].startswith("/api/media/")
    assert timeline[1]["video_url"].startswith("/api/media/")

    completed = _complete_vlog(client, project["id"])
    assert completed["status"] == "completed"
    history = client.get("/api/creations", headers=_headers()).json()
    assert history[0]["id"] == completed["final_creation_id"]
    assert history[0]["vlog_project_id"] == project["id"]


def test_local_vlog_accepts_history_video_and_derives_playback_url(client: TestClient) -> None:
    asset = _upload_video_asset(client)
    first = _create_local_vlog(client, [
        {"id": "v-1", "mode": "video", "source": "upload", "asset_path": asset["asset_path"], "duration": 6, "name": "clip.mp4", "mime": "video/mp4"},
    ])
    creation_id = _complete_vlog(client, first["id"])["final_creation_id"]

    second = _create_local_vlog(client, [
        {"id": "h-1", "mode": "video", "source": "history", "creation_id": creation_id, "duration": 5},
    ])

    timeline = second["timeline_data"]
    assert timeline[0]["video_url"].startswith("/api/media/")
    assert timeline[0]["name"]  # 由历史记录回填显示名


def test_local_vlog_rejects_ai_item_and_invalid_sources(client: TestClient) -> None:
    ai_item = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "style": "写实纪录",
            "description": "",
            "transition_style": "fade",
            "timeline_data": [{"id": "a-1", "mode": "ai", "description": "偷偷混入 AI 片段"}],
        },
        headers=_headers(),
    )
    assert ai_item.status_code == 422

    upload = _upload(client, count=1, sub="owner")
    foreign_asset = _upload_video_asset(client, sub="owner")
    responses = [
        # 引用他人上传的本地视频
        client.post(
            "/api/vlogs/local",
            json={
                "ratio": "9:16",
                "style": "写实纪录",
                "description": "",
                "transition_style": "fade",
                "timeline_data": [{"id": "v-1", "mode": "video", "source": "upload", "asset_path": foreign_asset["asset_path"], "duration": 5}],
            },
            headers=_headers("intruder"),
        ),
        # 引用他人的动效图片
        client.post(
            "/api/vlogs/local",
            json={
                "ratio": "9:16",
                "style": "写实纪录",
                "description": "",
                "transition_style": "fade",
                "timeline_data": [{"id": "m-1", "mode": "motion", "image_path": upload["images"][0]["image_path"], "duration": 4}],
            },
            headers=_headers("intruder"),
        ),
        # 未知来源与非法时长
        client.post(
            "/api/vlogs/local",
            json={
                "ratio": "9:16",
                "style": "写实纪录",
                "description": "",
                "transition_style": "fade",
                "timeline_data": [{"id": "v-2", "mode": "video", "source": "youtube", "duration": 5}],
            },
            headers=_headers(),
        ),
        client.post(
            "/api/vlogs/local",
            json={
                "ratio": "9:16",
                "style": "写实纪录",
                "description": "",
                "transition_style": "fade",
                "timeline_data": [{"id": "v-3", "mode": "video", "source": "upload", "asset_path": "x", "duration": 0}],
            },
            headers=_headers(),
        ),
    ]
    assert [response.status_code for response in responses] == [422, 422, 422, 422]


def test_local_vlog_blocked_while_another_vlog_is_active(client: TestClient) -> None:
    upload = _upload(client, count=2)
    config = _config(client)
    _create_project(client, upload, config["id"])  # pending 项目占用唯一名额
    asset = _upload_video_asset(client)

    response = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "style": "写实纪录",
            "description": "",
            "transition_style": "fade",
            "timeline_data": [{"id": "v-1", "mode": "video", "source": "upload", "asset_path": asset["asset_path"], "duration": 5}],
        },
        headers=_headers(),
    )
    assert response.status_code == 409


def test_create_vlog_rejects_unknown_transition(client: TestClient) -> None:
    upload = _upload(client, count=2)
    config = _config(client)
    response = client.post(
        "/api/vlogs",
        json={
            "config_id": config["id"],
            "image_paths": [image["image_path"] for image in upload["images"]],
            "style": "写实纪录",
            "transition_style": "star-wipe",
        },
        headers=_headers(),
    )
    assert response.status_code == 422
    assert "转场" in response.json()["detail"]


def test_complete_vlog_rejects_incomplete_clips(client: TestClient) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogProject

    upload = _upload(client, count=2)
    config = _config(client)
    project = _create_project(client, upload, config["id"])
    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.status = "ready_to_merge"
        db.commit()

    response = client.post(
        f"/api/vlogs/{project['id']}/complete",
        files={"file": ("vlog.mp4", MP4_BYTES, "video/mp4")},
        headers=_headers(),
    )
    assert response.status_code == 409


def test_abandon_ready_to_merge_unblocks_new_project(client: TestClient) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogProject

    upload = _upload(client, count=2)
    config = _config(client)
    project = _create_project(client, upload, config["id"])
    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.status = "ready_to_merge"
        db.commit()

    response = client.post(f"/api/vlogs/{project['id']}/abandon", headers=_headers())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"

    # 放弃后释放「唯一活跃项目」名额，可以立即新建
    created = client.post(
        "/api/vlogs",
        json={
            "config_id": config["id"],
            "image_paths": [image["image_path"] for image in upload["images"]],
            "style": "写实纪录",
        },
        headers=_headers(),
    )
    assert created.status_code == 200, created.text

    # 已结束的项目不能重复放弃
    again = client.post(f"/api/vlogs/{project['id']}/abandon", headers=_headers())
    assert again.status_code == 409


def test_pipeline_respects_cancelled_project(client: TestClient, monkeypatch) -> None:
    from backend.database import SessionLocal
    from backend.models import VlogClip, VlogProject
    from backend.services import vlog_pipeline

    upload = _upload(client, count=4)
    config = _config(client)
    project = _create_project(client, upload, config["id"])
    calls: list[int] = []

    class FakeAIClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run_video(self, model, prompt, **kwargs):
            calls.append(1)
            kwargs["on_submitted"](f"task-{len(calls)}")
            # 模拟放弃接口在片段生成期间把项目置为 cancelled
            with SessionLocal() as db:
                row = db.get(VlogProject, project["id"])
                row.status = "cancelled"
                db.commit()
            return {"task_id": f"task-{len(calls)}", "content": MP4_BYTES}

    monkeypatch.setattr(vlog_pipeline, "AIClient", FakeAIClient)
    vlog_pipeline._run_vlog(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        clips = db.scalars(
            select(VlogClip).where(VlogClip.project_id == project["id"]).order_by(VlogClip.sequence)
        ).all()
        assert row.status == "cancelled"
        assert [clip.status for clip in clips] == ["completed", "pending"]
    assert len(calls) == 1


def test_transition_catalog_matches_frontend() -> None:
    """转场清单在前后端各维护一份，防止两边漂移。"""
    source = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    match = re.search(r"export const VLOG_TRANSITIONS: VlogTransitionOption\[\] = \[(.*?)\n\]", source, re.S)
    assert match, "frontend/src/types.ts 中找不到 VLOG_TRANSITIONS 定义"
    frontend_keys = re.findall(r"key: '(\w+)'", match.group(1))
    assert tuple(frontend_keys) == tuple(item.key for item in VLOG_TRANSITIONS)
