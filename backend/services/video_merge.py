"""服务端 Vlog 合成:原生 FFmpeg 渲染,替代浏览器 FFmpeg.wasm。

滤镜图与参数从 frontend 的 ffmpegClient.ts 平移:0.6 秒转场、固定画幅统一、
zoompan 动效模板保持同一观感;素材探测从 wasm 日志正则升级为 ffprobe JSON。
进度经 `-progress pipe:1` 解析为 0~1,由调用方节流落库;进程句柄经 on_process
交给调用方登记,用户放弃项目时可 terminate。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
# 与前端时长预估共用:VlogPanel 的预计总时长也按该值扣减
VLOG_TRANSITION_SECONDS = 0.6
OUTPUT_FPS = 30

MOTION_TEMPLATES = ("kenburns_in", "kenburns_out", "pan_left", "pan_right", "drift")


class MergeError(RuntimeError):
    """合成失败,message 面向用户展示。"""


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None


ProgressCallback = Callable[[float], None]
ProcessCallback = Callable[[subprocess.Popen], None]


# ---------------------------------------------------------------- 探测


@dataclass
class MediaProbe:
    duration: float
    has_audio: bool
    width: int
    height: int
    video_codec: str


def probe_media(path: Path) -> MediaProbe:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MergeError(f"无法读取素材信息:{path.name}(可能不是有效的视频文件)")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MergeError(f"无法解析素材信息:{path.name}") from exc

    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    duration = 0.0
    format_info = info.get("format") or {}
    try:
        duration = float(format_info.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 and video:
        try:
            duration = float(video.get("duration", 0))
        except (TypeError, ValueError):
            duration = 0.0
    if duration <= 0:
        raise MergeError(f"无法确定素材时长:{path.name}")
    return MediaProbe(
        duration=duration,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        width=int(video.get("width", 0)) if video else 0,
        height=int(video.get("height", 0)) if video else 0,
        video_codec=str(video.get("codec_name", "")) if video else "",
    )


# ---------------------------------------------------------------- FFmpeg 执行


def _run_ffmpeg(
    args: list[str],
    *,
    total_seconds: float,
    on_progress: ProgressCallback | None = None,
    on_process: ProcessCallback | None = None,
) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats", "-y", "-progress", "pipe:1", *args]
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
        if on_process is not None:
            on_process(process)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                # 旧版 ffmpeg 的 out_time_ms 实际也是微秒
                if key not in ("out_time_us", "out_time_ms"):
                    continue
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                if total_seconds > 0 and on_progress is not None:
                    on_progress(min(1.0, max(0.0, seconds / total_seconds)))
        finally:
            if process.stdout is not None:
                process.stdout.close()
            returncode = process.wait()
        if on_process is not None:
            on_process(None)  # 进程已退出,通知调用方注销
        if returncode != 0:
            stderr_file.seek(0)
            lines = [
                line.strip()
                for line in stderr_file.read().decode(errors="replace").splitlines()
                if line.strip()
            ]
            raise MergeError(";".join(lines[-3:]) or f"FFmpeg 退出码 {returncode}")


# ---------------------------------------------------------------- 滤镜图构建(纯函数,便于单测)


def output_size(ratio: str, resolution: str = "720p") -> tuple[int, int]:
    base = 1080 if resolution == "1080p" else 720
    long_side = base * 16 // 9
    return (base, long_side) if ratio == "9:16" else (long_side, base)


def motion_expression(template: str, frames: int) -> dict[str, str]:
    """zoompan 表达式,与前端 ffmpegClient.ts 的 motionExpression 一致。"""
    progress = f"on/{frames}"
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if template == "kenburns_out":
        return {"z": f"max(1,1.18-0.18*{progress})", "x": center_x, "y": center_y}
    if template == "pan_left":
        return {"z": "1.08", "x": f"(iw-iw/zoom)*(1-{progress})", "y": center_y}
    if template == "pan_right":
        return {"z": "1.08", "x": f"(iw-iw/zoom)*{progress}", "y": center_y}
    if template == "drift":
        return {
            "z": f"1.05+0.03*sin({progress}*PI*2)",
            "x": f"(iw-iw/zoom)*(0.5+0.35*sin({progress}*PI*2))",
            "y": f"(ih-ih/zoom)*(0.5+0.35*cos({progress}*PI*2))",
        }
    return {"z": f"1+0.18*{progress}", "x": center_x, "y": center_y}


def build_motion_filter(index: int, template: str, duration: float, width: int, height: int) -> str:
    frames = max(1, round(duration * OUTPUT_FPS))
    motion = motion_expression(template, frames)
    base_width, base_height = width * 2, height * 2
    return (
        f"[{index}:v]scale={base_width}:{base_height}:force_original_aspect_ratio=increase,"
        f"crop={base_width}:{base_height},"
        f"zoompan=z='{motion['z']}':x='{motion['x']}':y='{motion['y']}':"
        f"d=1:s={width}x{height}:fps={OUTPUT_FPS},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
    )


def build_vlog_filters(
    probes: list[MediaProbe],
    width: int,
    height: int,
    transition_style: str,
    transition: float = VLOG_TRANSITION_SECONDS,
) -> tuple[list[str], str, str | None]:
    """Vlog 最终合成的 filter_complex;返回 (filters, 视频输出标签, 音频输出标签|None)。

    与 ffmpegClient.ts 的 mergeVlogClips 相同:先逐段统一画幅/帧率/像素格式,
    再做 0.6 秒 xfade 视频链与 acrossfade 音频链(无声段用 anullsrc 补静音)。
    """
    filters = [
        f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={OUTPUT_FPS},format=yuv420p[v{index}]"
        for index in range(len(probes))
    ]

    video_label = "v0"
    cumulative = probes[0].duration
    for index in range(1, len(probes)):
        output_label = f"vx{index}"
        offset = max(0.0, cumulative - transition * index)
        filters.append(
            f"[{video_label}][v{index}]xfade=transition={transition_style}:"
            f"duration={transition}:offset={offset:.3f}[{output_label}]"
        )
        video_label = output_label
        cumulative += probes[index].duration

    audio_label = None
    if any(probe.has_audio for probe in probes):
        for index, probe in enumerate(probes):
            if probe.has_audio:
                filters.append(
                    f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"atrim=0:{probe.duration:.3f},asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                filters.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{probe.duration:.3f}[a{index}]"
                )
        audio_label = "a0"
        for index in range(1, len(probes)):
            output_label = f"ax{index}"
            filters.append(f"[{audio_label}][a{index}]acrossfade=d={transition}:c1=tri:c2=tri[{output_label}]")
            audio_label = output_label
    return filters, video_label, audio_label


# ---------------------------------------------------------------- 渲染入口


@dataclass
class RenderItem:
    """一个时间线片段的本地素材:kind 为 video(成片素材)或 motion(单图动效)。"""

    kind: str
    path: Path
    duration: float = 0.0
    motion_template: str = "kenburns_in"


def render_motion_segment(
    image: Path,
    out_path: Path,
    *,
    template: str,
    duration: float,
    ratio: str,
    resolution: str = "720p",
    on_progress: ProgressCallback | None = None,
    on_process: ProcessCallback | None = None,
) -> None:
    width, height = output_size(ratio, resolution)
    filters = build_motion_filter(0, template, duration, width, height)
    _run_ffmpeg(
        [
            "-loop", "1", "-framerate", str(OUTPUT_FPS), "-t", f"{duration:.3f}", "-i", str(image),
            "-filter_complex", filters,
            "-map", "[v0]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out_path),
        ],
        total_seconds=duration,
        on_progress=on_progress,
        on_process=on_process,
    )


def compress_video(
    source: Path,
    out_path: Path,
    *,
    crf: int = 30,
    scale: float = 1.0,
    on_progress: ProgressCallback | None = None,
    on_process: ProcessCallback | None = None,
) -> None:
    """成片超限的兜底:降 CRF(可选缩分辨率)重编码,用画质换体积。"""
    probe = probe_media(source)
    args: list[str] = ["-i", str(source)]
    if scale != 1.0 and probe.width > 0 and probe.height > 0:
        # 缩放后取偶,避免 yuv420p 遇到奇数宽高报错
        width = max(2, int(probe.width * scale) // 2 * 2)
        height = max(2, int(probe.height * scale) // 2 * 2)
        args.extend(["-vf", f"scale={width}:{height}"])
    args.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)])
    if probe.has_audio:
        args.extend(["-c:a", "aac", "-b:a", "128k"])
    else:
        args.extend(["-an"])
    args.extend(["-movflags", "+faststart", str(out_path)])
    _run_ffmpeg(args, total_seconds=probe.duration, on_progress=on_progress, on_process=on_process)


def merge_segments(
    sources: list[Path],
    out_path: Path,
    *,
    ratio: str,
    resolution: str = "720p",
    transition_style: str,
    on_progress: ProgressCallback | None = None,
    on_process: ProcessCallback | None = None,
) -> float:
    """把若干视频段合成一条;返回预计成片时长(秒)。"""
    if not sources:
        raise MergeError("没有可合成的视频片段")
    probes = [probe_media(path) for path in sources]
    width, height = output_size(ratio, resolution)
    transition = VLOG_TRANSITION_SECONDS
    expected = sum(probe.duration for probe in probes) - transition * (len(probes) - 1)

    if len(sources) == 1 and sources[0].suffix.lower() == ".mp4" and probes[0].video_codec == "h264":
        # 单段 h264 mp4:流拷贝重封装即可,无损秒级
        _run_ffmpeg(
            ["-i", str(sources[0]), "-c", "copy", "-movflags", "+faststart", str(out_path)],
            total_seconds=probes[0].duration,
            on_progress=on_progress,
            on_process=on_process,
        )
        return probes[0].duration

    filters, video_label, audio_label = build_vlog_filters(probes, width, height, transition_style)
    args: list[str] = []
    for source in sources:
        args.extend(["-i", str(source)])
    args.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{video_label}]",
    ])
    if audio_label is not None:
        args.extend(["-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "160k"])
    args.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-movflags", "+faststart",
        str(out_path),
    ])
    _run_ffmpeg(
        args,
        total_seconds=expected,
        on_progress=on_progress,
        on_process=on_process,
    )
    return max(expected, 0.1)


def render_vlog(
    items: list[RenderItem],
    *,
    out_path: Path,
    ratio: str,
    resolution: str = "720p",
    transition_style: str,
    on_progress: ProgressCallback | None = None,
    on_process: ProcessCallback | None = None,
) -> float:
    """渲染完整时间线:先出图片动效段,再统一转场合成;返回预计成片时长(秒)。"""
    if not items:
        raise MergeError("时间线为空,没有可合成的内容")

    motion_count = sum(1 for item in items if item.kind == "motion")
    motion_done = 0
    segments: list[Path] = []
    for index, item in enumerate(items):
        if item.kind != "motion":
            segments.append(item.path)
            continue
        segment = out_path.parent / f"motion-{index:02d}.mp4"
        # 动效段渲染合计占前 45% 进度,最终合成占其余
        base = 0.45 * motion_done / motion_count
        step = 0.45 / motion_count
        render_motion_segment(
            item.path,
            segment,
            template=item.motion_template,
            duration=item.duration,
            ratio=ratio,
            resolution=resolution,
            on_progress=(lambda r, b=base, s=step: on_progress(b + s * r)) if on_progress else None,
            on_process=on_process,
        )
        motion_done += 1
        segments.append(segment)

    merge_base = 0.45 if motion_count else 0.0
    return merge_segments(
        segments,
        out_path,
        ratio=ratio,
        resolution=resolution,
        transition_style=transition_style,
        on_progress=(lambda r: on_progress(merge_base + (1 - merge_base) * r)) if on_progress else None,
        on_process=on_process,
    )
