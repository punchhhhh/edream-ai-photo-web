"""服务端合成队列:纯函数滤镜图、提交端点、调度认领、worker 成功/失败/恢复路径。"""

import subprocess
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from backend import media
from backend.services import merge_queue, video_merge
from backend.services.video_merge import MediaProbe

MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024


def _headers(sub: str = "merge-user") -> dict[str, str]:
    return {"X-Casdoor-Sub": sub}


def _jpeg(color: tuple[int, int, int] = (90, 120, 150)) -> bytes:
    image = Image.new("RGB", (300, 500), color)
    from io import BytesIO

    output = BytesIO()
    image.save(output, "JPEG", quality=92)
    return output.getvalue()


@pytest.fixture()
def client(monkeypatch):
    # 关掉真实调度线程,认领逻辑在用例里同步调 _dispatch_once()
    def _dummy_dispatcher(stop_event):
        handle = threading.Thread(target=lambda: None, daemon=True)
        handle.start()
        return handle

    monkeypatch.setattr("backend.app.start_merge_dispatcher", _dummy_dispatcher)
    monkeypatch.setattr("backend.routers.vlogs.start_vlog_thread", lambda _id: None)
    from backend.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


class _SyncThread:
    """替身线程:同步执行 target,让调度认领的断言不依赖真实线程时序。"""

    def __init__(self, target=None, args=(), daemon=False, name=None):
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)

    def join(self, timeout=None) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_queue_state():
    yield
    with merge_queue._active_lock:
        merge_queue._processes.clear()
        merge_queue._active_count = 0


def _upload_image(client: TestClient) -> dict:
    response = client.post(
        "/api/vlogs/upload",
        files=[("files", ("photo.jpg", _jpeg(), "image/jpeg"))],
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload_video(client: TestClient) -> dict:
    response = client.post(
        "/api/vlogs/assets/video",
        files=[("file", ("clip.mp4", MP4_BYTES, "video/mp4"))],
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_local_project(client: TestClient, upload_image: dict, upload_video: dict, sub: str = "merge-user") -> dict:
    response = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "style": "写实纪录",
            "description": "服务端合成测试",
            "transition_style": "fade",
            "timeline_data": [
                {
                    "id": "motion-1",
                    "mode": "motion",
                    "image_path": upload_image["images"][0]["image_path"],
                    "duration": 4,
                    "motion_template": "kenburns_in",
                },
                {
                    "id": "video-1",
                    "mode": "video",
                    "source": "upload",
                    "asset_path": upload_video["asset_path"],
                    "duration": 6,
                    "name": "本地视频",
                    "mime": "video/mp4",
                },
            ],
        },
        headers=_headers(sub),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fake_render(monkeypatch, *, fail: bool = False, extra: int = 0):
    calls: list[dict] = []

    def fake_render_vlog(items, *, out_path, ratio, resolution, transition_style, on_progress=None, on_process=None):
        calls.append({
            "items": list(items),
            "ratio": ratio,
            "resolution": resolution,
            "transition_style": transition_style,
        })
        if fail:
            raise video_merge.MergeError("boom")
        out_path.write_bytes(MP4_BYTES + b"\x00" * extra)
        if on_progress is not None:
            on_progress(0.5)
            on_progress(1.0)
        return 12.0

    monkeypatch.setattr(video_merge, "render_vlog", fake_render_vlog)
    return calls


# ---------------------------------------------------------------- 纯函数


def _probe(duration: float = 5.0, has_audio: bool = True) -> MediaProbe:
    return MediaProbe(duration=duration, has_audio=has_audio, width=1280, height=720, video_codec="h264")


def test_output_size() -> None:
    assert video_merge.output_size("9:16") == (720, 1280)
    assert video_merge.output_size("16:9") == (1280, 720)
    assert video_merge.output_size("9:16", "1080p") == (1080, 1920)


def test_motion_expression_matches_frontend_templates() -> None:
    expressions = video_merge.motion_expression("kenburns_in", 120)
    assert expressions["z"] == "1+0.18*on/120"
    assert video_merge.motion_expression("kenburns_out", 120)["z"] == "max(1,1.18-0.18*on/120)"
    assert video_merge.motion_expression("pan_left", 90)["x"] == "(iw-iw/zoom)*(1-on/90)"
    assert video_merge.motion_expression("pan_right", 90)["x"] == "(iw-iw/zoom)*on/90"
    drift = video_merge.motion_expression("drift", 100)
    assert "sin(on/100*PI*2)" in drift["z"] and "cos(on/100*PI*2)" in drift["y"]


def test_build_motion_filter_uses_zoompan_two_pass_trick() -> None:
    filters = video_merge.build_motion_filter(0, "kenburns_in", 4.0, 720, 1280)
    assert "scale=1440:2560:force_original_aspect_ratio=increase" in filters
    assert (
        "zoompan=z='1+0.18*on/120':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=720x1280:fps=30" in filters
    )
    assert "trim=duration=4.000" in filters


def test_build_vlog_filters_two_sources_with_transition() -> None:
    filters, video_label, audio_label = video_merge.build_vlog_filters(
        [_probe(5.0), _probe(4.0, has_audio=False)], 720, 1280, "wipeleft"
    )
    joined = ";".join(filters)
    assert video_label == "vx1" and audio_label == "ax1"
    # 转场偏移 = 首段时长 - 0.6
    assert "xfade=transition=wipeleft:duration=0.6:offset=4.400[vx1]" in joined
    # 无声段补静音轨,再交叉淡化
    assert "anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:4.000[a1]" in joined
    assert "acrossfade=d=0.6:c1=tri:c2=tri[ax1]" in joined


def test_build_vlog_filters_single_source_has_no_transition() -> None:
    filters, video_label, audio_label = video_merge.build_vlog_filters([_probe()], 720, 1280, "fade")
    assert video_label == "v0"
    assert audio_label == "a0"
    assert all("xfade" not in item for item in filters)


# ---------------------------------------------------------------- 提交端点


def test_submit_merge_queues_and_rejects_duplicates(client: TestClient) -> None:
    project = _create_local_project(client, _upload_image(client), _upload_video(client))

    first = client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers())
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["merge_status"] == "queued"
    assert body["merge_progress"] == 0.0
    assert body["status"] == "ready_to_merge"

    second = client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers())
    assert second.status_code == 409

    # 排队中的项目仍占住"唯一活跃项目"名额
    payload_image = _upload_image(client)
    conflict = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "timeline_data": [{
                "id": "motion-2",
                "mode": "motion",
                "image_path": payload_image["images"][0]["image_path"],
                "duration": 4,
                "motion_template": "kenburns_in",
            }],
        },
        headers=_headers(),
    )
    assert conflict.status_code == 409


def test_submit_merge_requires_mergeable_state(client: TestClient) -> None:
    upload_image = _upload_image(client)
    project = _create_local_project(client, upload_image, _upload_video(client))
    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.status = "completed"
        db.commit()

    response = client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers())
    assert response.status_code == 409


def test_submit_merge_without_ffmpeg_returns_503(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(video_merge, "ffmpeg_available", lambda: False)
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    response = client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers())
    assert response.status_code == 503
    assert "FFmpeg" in response.json()["detail"]


def test_local_project_over_20_groups_fails_at_merge_with_clear_error(client: TestClient, monkeypatch) -> None:
    """20 组是业务上限:创建仍放行(兼容旧数据上限 36),合成时给出明确的失败原因。"""
    calls = _fake_render(monkeypatch)
    upload = _upload_image(client)
    image_path = upload["images"][0]["image_path"]
    response = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "timeline_data": [
                {
                    "id": f"motion-{index}",
                    "mode": "motion",
                    "image_path": image_path,
                    "duration": 3,
                    "motion_template": "kenburns_in",
                }
                for index in range(1, 22)
            ],
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    project = response.json()
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    merge_queue._run_worker(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.merge_status == "failed"
        assert "20 个片段组" in row.merge_error
    assert calls == []


# ---------------------------------------------------------------- 调度与 worker


def test_dispatch_respects_global_concurrency(client: TestClient, monkeypatch) -> None:
    from types import SimpleNamespace

    from backend.settings import settings

    monkeypatch.setattr(settings, "max_merge_workers", 1)
    monkeypatch.setattr(merge_queue, "threading", SimpleNamespace(Thread=_SyncThread))
    seen: list[int] = []
    monkeypatch.setattr(merge_queue, "_run_worker", lambda pid: seen.append(pid))

    # 两个用户各排一个队,全局上限 1 一次只认领一个
    first = _create_local_project(client, _upload_image(client), _upload_video(client))
    other_image = client.post(
        "/api/vlogs/upload",
        files=[("files", ("other.jpg", _jpeg((10, 20, 30)), "image/jpeg"))],
        headers=_headers("merge-user-2"),
    ).json()
    other_video = client.post(
        "/api/vlogs/assets/video",
        files=[("file", ("other.mp4", MP4_BYTES, "video/mp4"))],
        headers=_headers("merge-user-2"),
    ).json()
    second = _create_local_project(client, other_image, other_video, sub="merge-user-2")
    assert client.post(f"/api/vlogs/{first['id']}/merge", headers=_headers()).status_code == 200
    assert client.post(f"/api/vlogs/{second['id']}/merge", headers=_headers("merge-user-2")).status_code == 200

    merge_queue._dispatch_once()
    assert len(seen) == 1

    # running 任务占住名额,其余继续排队
    merge_queue._dispatch_once()
    assert len(seen) == 1

    # worker 结束释放名额后,剩余任务被认领
    with merge_queue._active_lock:
        merge_queue._active_count = 0
    merge_queue._dispatch_once()
    assert len(seen) == 2

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        statuses = {
            row.id: row.merge_status
            for row in db.scalars(select(VlogProject)).all()
        }
    assert statuses[first["id"]] == "running"
    assert statuses[second["id"]] == "running"


def test_worker_completes_and_finalizes(client: TestClient, monkeypatch) -> None:
    calls = _fake_render(monkeypatch)
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    merge_queue._run_worker(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.status == "completed"
        assert row.merge_status == "done"
        assert row.merge_progress == 1.0
        assert row.merge_error is None
        assert row.final_video_path and row.final_creation_id

    assert len(calls) == 1
    items = calls[0]["items"]
    assert [item.kind for item in items] == ["motion", "video"]
    assert items[0].duration == 4.0 and items[0].motion_template == "kenburns_in"
    assert calls[0]["ratio"] == "9:16" and calls[0]["transition_style"] == "fade"

    detail = client.get(f"/api/vlogs/{project['id']}", headers=_headers()).json()
    assert detail["merge_status"] == "done"
    assert detail["final_video_url"]

    history = client.get("/api/creations", headers=_headers()).json()
    merged = [item for item in history if item["id"] == detail["final_creation_id"]]
    assert merged and merged[0]["image_source"] == "merged"
    assert merged[0]["duration"] == 12


def test_worker_failure_marks_merge_failed_and_allows_retry(client: TestClient, monkeypatch) -> None:
    _fake_render(monkeypatch, fail=True)
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    merge_queue._run_worker(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.status == "ready_to_merge"
        assert row.merge_status == "failed"
        assert "boom" in row.merge_error

    # 失败后可以重新提交
    again = client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers())
    assert again.status_code == 200
    assert again.json()["merge_status"] == "queued"


def _run_submitted_worker(client: TestClient, project: dict) -> None:
    """提交合成后,把项目置为 running 并同步跑一遍 worker(测试里不依赖真实线程)。"""
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    merge_queue._run_worker(project["id"])


def test_merge_compresses_when_output_too_large(client: TestClient, monkeypatch) -> None:
    compress_calls: list[dict] = []

    def fake_compress(source, out_path, *, crf=30, scale=1.0, on_progress=None, on_process=None):
        compress_calls.append({"crf": crf, "scale": scale})
        out_path.write_bytes(MP4_BYTES)
        if on_progress is not None:
            on_progress(1.0)

    monkeypatch.setattr(video_merge, "compress_video", fake_compress)
    from backend.settings import settings

    monkeypatch.setattr(settings, "max_video_upload_mb", 1)
    _fake_render(monkeypatch, extra=2 * 1024 * 1024)
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    _run_submitted_worker(client, project)

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.merge_status == "done", row.merge_error
        assert row.final_video_path
    # 首次压缩只降码率,不动分辨率
    assert compress_calls == [{"crf": 30, "scale": 1.0}]


def test_merge_fails_when_compressed_still_too_large(client: TestClient, monkeypatch) -> None:
    def fake_compress(source, out_path, *, crf=30, scale=1.0, on_progress=None, on_process=None):
        out_path.write_bytes(MP4_BYTES + b"\x00" * (2 * 1024 * 1024))

    monkeypatch.setattr(video_merge, "compress_video", fake_compress)
    from backend.settings import settings

    monkeypatch.setattr(settings, "max_video_upload_mb", 1)
    _fake_render(monkeypatch, extra=2 * 1024 * 1024)
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    _run_submitted_worker(client, project)

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.merge_status == "failed"
        assert "压缩" in row.merge_error
        assert row.final_video_path is None


def test_worker_skips_cancelled_project(client: TestClient, monkeypatch) -> None:
    _fake_render(monkeypatch)
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    abandon = client.post(f"/api/vlogs/{project['id']}/abandon", headers=_headers())
    assert abandon.status_code == 200
    assert abandon.json()["status"] == "cancelled"
    assert abandon.json()["merge_status"] is None

    merge_queue._run_worker(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.status == "cancelled"
        assert row.merge_status is None
        assert row.final_video_path is None


def test_cancel_all_merges_requeues_and_terminates(client: TestClient) -> None:
    """进程退出收尾:终止 ffmpeg 进程,任务重新排队,worker 事后不会覆盖排队状态。"""
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    terminated: list[bool] = []

    class _FakeProcess:
        def terminate(self) -> None:
            terminated.append(True)

        def wait(self, timeout=None) -> int:
            return 0

        def kill(self) -> None:
            return None

    with merge_queue._active_lock:
        merge_queue._processes[project["id"]] = _FakeProcess()

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    merge_queue.cancel_all_merges()

    assert terminated == [True]
    with merge_queue._active_lock:
        assert merge_queue._processes == {}
    with SessionLocal() as db:
        assert db.get(VlogProject, project["id"]).merge_status == "queued"


def test_recover_merge_jobs_requeues_running(client: TestClient) -> None:
    project = _create_local_project(client, _upload_image(client), _upload_video(client))
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        row.merge_progress = 0.4
        db.commit()

    assert merge_queue.recover_merge_jobs() == 1

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.merge_status == "queued"
        assert row.merge_progress == 0.0
    assert merge_queue.recover_merge_jobs() == 0


def test_reap_stale_merges_only_touches_stale(client: TestClient) -> None:
    from datetime import datetime, timedelta, timezone

    from backend.database import SessionLocal
    from backend.models import VlogProject

    stale = _create_local_project(client, _upload_image(client), _upload_video(client))
    fresh_image = client.post(
        "/api/vlogs/upload",
        files=[("files", ("fresh.jpg", _jpeg((200, 100, 50)), "image/jpeg"))],
        headers=_headers("merge-user-3"),
    ).json()
    fresh_project = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "16:9",
            "timeline_data": [{
                "id": "motion-1",
                "mode": "motion",
                "image_path": fresh_image["images"][0]["image_path"],
                "duration": 3,
                "motion_template": "drift",
            }],
        },
        headers=_headers("merge-user-3"),
    ).json()

    with SessionLocal() as db:
        stale_row = db.get(VlogProject, stale["id"])
        stale_row.merge_status = "running"
        stale_row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
        fresh_row = db.get(VlogProject, fresh_project["id"])
        fresh_row.merge_status = "running"
        db.commit()

    assert merge_queue.reap_stale_merges() == 1

    with SessionLocal() as db:
        assert db.get(VlogProject, stale["id"]).merge_status == "failed"
        assert "重新提交" in db.get(VlogProject, stale["id"]).merge_error
        assert db.get(VlogProject, fresh_project["id"]).merge_status == "running"


# ---------------------------------------------------------------- 真实 FFmpeg 集成(本机无 ffmpeg 时跳过)

requires_ffmpeg = pytest.mark.skipif(
    not video_merge.ffmpeg_available(), reason="本机未安装 FFmpeg/ffprobe"
)


def _make_clip(path, seconds: float, size: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )


@requires_ffmpeg
def test_real_ffmpeg_renders_motion_and_transition_merge(client: TestClient, tmp_path) -> None:
    clip_a, clip_b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _make_clip(clip_a, 2.0, "320x480")
    _make_clip(clip_b, 2.0, "640x360")

    image_upload = _upload_image(client)
    uploads = []
    for clip in (clip_a, clip_b):
        response = client.post(
            "/api/vlogs/assets/video",
            files=[("file", (clip.name, clip.read_bytes(), "video/mp4"))],
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        uploads.append(response.json())

    response = client.post(
        "/api/vlogs/local",
        json={
            "ratio": "9:16",
            "style": "写实纪录",
            "description": "真实渲染集成",
            "transition_style": "wipeleft",
            "timeline_data": [
                {
                    "id": "video-1",
                    "mode": "video",
                    "source": "upload",
                    "asset_path": uploads[0]["asset_path"],
                    "duration": 2,
                    "name": "素材A",
                    "mime": "video/mp4",
                },
                {
                    "id": "motion-1",
                    "mode": "motion",
                    "image_path": image_upload["images"][0]["image_path"],
                    "duration": 3,
                    "motion_template": "pan_right",
                },
                {
                    "id": "video-2",
                    "mode": "video",
                    "source": "upload",
                    "asset_path": uploads[1]["asset_path"],
                    "duration": 2,
                    "name": "素材B",
                    "mime": "video/mp4",
                },
            ],
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    project = response.json()
    assert client.post(f"/api/vlogs/{project['id']}/merge", headers=_headers()).status_code == 200

    from backend.database import SessionLocal
    from backend.models import VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.merge_status = "running"
        db.commit()

    merge_queue._run_worker(project["id"])

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.merge_status == "done", row.merge_error
        assert row.status == "completed"

    probe = video_merge.probe_media(media.safe_abs_path(row.final_video_path))
    # 2s + 3s + 2s - 2 个 0.6s 转场 = 5.8s;720p 竖幅 h264
    assert 5.5 <= probe.duration <= 6.1
    assert (probe.width, probe.height) == (720, 1280)
    assert probe.video_codec == "h264"


@requires_ffmpeg
def test_real_compress_video_halves_resolution(tmp_path) -> None:
    source = tmp_path / "src.mp4"
    _make_clip(source, 2.0, "320x480")
    compressed = tmp_path / "compressed.mp4"
    video_merge.compress_video(source, compressed, crf=30, scale=0.5)
    probe = video_merge.probe_media(compressed)
    assert (probe.width, probe.height) == (160, 240)
    assert 1.8 <= probe.duration <= 2.2
    assert probe.video_codec == "h264"


class _FakeStreamResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int | None = None):
        return iter([self._payload])


class _FakeStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return _FakeStreamResponse(self._payload)

    def __exit__(self, *args):
        return False


def test_merge_backfills_remote_only_ai_clip_into_storage(client: TestClient, monkeypatch) -> None:
    """只存了上游临时链接的 AI 片段,合成时把素材落对象存储并回填 key。"""
    _fake_render(monkeypatch)
    monkeypatch.setattr(
        merge_queue.httpx,
        "stream",
        lambda *_args, **_kwargs: _FakeStream(MP4_BYTES),
    )
    project = _create_local_project(client, _upload_image(client), _upload_video(client))

    from backend.database import SessionLocal
    from backend.models import VlogClip, VlogProject

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        row.timeline_data = [{"id": "ai-1", "mode": "ai", "description": "回填测试"}]
        db.add(
            VlogClip(
                project_id=row.id,
                sequence=1,
                duration=5,
                status="completed",
                prompt="回填测试",
                video_path=None,
                video_url="https://upstream.example.com/clip.mp4",
            )
        )
        db.commit()
        user_id = row.user_id

    _run_submitted_worker(client, project)

    with SessionLocal() as db:
        row = db.get(VlogProject, project["id"])
        assert row.merge_status == "done", row.merge_error
        clip = db.scalars(select(VlogClip).where(VlogClip.project_id == row.id)).one()
        assert clip.video_path and clip.video_path.startswith(f"users/{user_id}/videos/")
        assert clip.video_url is None
