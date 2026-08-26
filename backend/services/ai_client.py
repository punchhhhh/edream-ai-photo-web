"""OpenAI 兼容(new-api)模型调用客户端。

三类能力:
- expand_prompt: /v1/chat/completions 文本拓展
- generate_image: /v1/images/generations 文生图
- run_video: 视频生成,支持两种 provider
  * video_generations —— new-api 任务式接口(POST /v1/video/generations + 轮询),适用可灵/Vidu/PixVerse 等
  * openai_videos —— Sora 风格(POST /v1/videos + 轮询 + 下载 content)

以后切换自建模型服务时,保持 OpenAI 兼容协议即可直接复用。
"""

import base64
import json
import time
from typing import Any, Callable

import httpx

EXPAND_SYSTEM_PROMPT = """你是一位专业的 AI 视频提示词编剧。用户会给出一句简短的创意描述和期望的风格,请把它扩写成一段适合文生视频/图生视频模型使用的中文提示词。要求:
1. 忠实保留用户原意,可以补充细节,但不要引入与原意冲突的新剧情
2. 具体描述画面主体、动作、环境、光影、色调、镜头运动(推拉摇移等)与整体氛围
3. 控制在 80-160 字,连贯成段,不要分点、不要标题、不要输出任何解释性文字"""


class AICallError(RuntimeError):
    """模型调用失败,携带可直接展示给用户的错误信息。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def normalize_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")].rstrip("/")
    return u


def _snippet(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def _iter_strings(obj: Any, key: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v, key)
    elif isinstance(obj, str):
        yield key, obj


def extract_video_url(obj: Any) -> str | None:
    """从任意 JSON 结构里尽力找出视频文件 URL。"""
    strong, weak = [], []
    for key, s in _iter_strings(obj):
        if not s.startswith(("http://", "https://")):
            continue
        lk = key.lower()
        if any(ext in s.lower() for ext in (".mp4", ".webm", ".mov", ".m4v")):
            strong.append(s)
        elif "video" in lk and "url" in lk:
            weak.append(s)
    return strong[0] if strong else (weak[0] if weak else None)


_DONE = {"success", "succeeded", "succeed", "completed", "complete", "done", "finished", "end", "结束", "成功"}
_FAILED = {"failure", "failed", "fail", "error", "cancelled", "canceled", "timeout", "expired", "失败"}


def extract_task_status(obj: Any) -> str | None:
    """提取任务状态字符串,归一化成 processing / done / failed / None(未知)。"""
    for container in (obj, obj.get("data") if isinstance(obj, dict) else None, obj.get("task") if isinstance(obj, dict) else None):
        if not isinstance(container, dict):
            continue
        for key in ("status", "task_status", "state", "status_code"):
            v = container.get(key)
            if isinstance(v, str) and v.strip():
                lv = v.strip().lower()
                if lv in _DONE:
                    return "done"
                if lv in _FAILED:
                    return "failed"
                return "processing"
    return None


class AIClient:
    def __init__(self, base_url: str, api_key: str, provider: str = "video_generations"):
        self.base = normalize_base_url(base_url)
        if not self.base:
            raise AICallError("base_url 为空,请先在模型配置中填写 new-api 地址")
        self.provider = provider
        self.client = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=15.0),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "edream-ai-photo-web/0.1"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, *, timeout: float = 180.0, **kw) -> Any:
        url = f"{self.base}{path}"
        try:
            resp = self.client.request(method, url, timeout=timeout, **kw)
        except httpx.HTTPError as e:
            raise AICallError(f"请求模型服务失败({url}):{e.__class__.__name__}: {e}") from e
        if resp.status_code >= 400:
            raise AICallError(
                f"模型服务返回 {resp.status_code}({path}):{_snippet(resp.text)}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except json.JSONDecodeError:
            raise AICallError(f"模型服务响应不是合法 JSON({path}):{_snippet(resp.text)}") from None

    # ------------------------------------------------------------ 连接测试

    def list_models(self) -> list[str]:
        """拉取网关模型列表(不消耗 token),用于配置测试与模型名建议。"""
        data = self._request("GET", "/v1/models", timeout=30.0)
        ids = {s for key, s in _iter_strings(data) if key.lower() == "id" and s and not s.startswith("http")}
        return sorted(ids)

    # ------------------------------------------------------------ 文本拓展

    def expand_prompt(self, model: str, text: str, style: str = "", style_description: str = "") -> str:
        if not model:
            raise AICallError("当前配置未填写文本模型(chat_model),无法进行 AI 拓展")
        user_content = f"原始创意:{text.strip()}"
        if style:
            user_content += f"\n期望风格:{style}"
        if style_description:
            user_content += f"\n风格要点(务必融入画面描述,不要原样照抄):{style_description}"
        data = self._request(
            "POST",
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": EXPAND_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.7,
            },
            timeout=180.0,
        )
        try:
            content = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            raise AICallError(f"文本模型响应格式异常:{_snippet(json.dumps(data, ensure_ascii=False))}") from None
        if not content:
            raise AICallError("文本模型返回了空内容,请重试或更换模型")
        return content

    # ------------------------------------------------------------ 文生图

    def generate_image(self, model: str, prompt: str, size: str = "1024x1024") -> tuple[bytes, str]:
        """返回 (图片字节, 扩展名)。"""
        if not model:
            raise AICallError("当前配置未填写图片模型(image_model),无法生成图片")
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        }
        try:
            data = self._request("POST", "/v1/images/generations", json=body, timeout=300.0)
        except AICallError as e:
            # 仅参数类 4xx 才降级重试(部分网关不认 response_format/size);网络错误/5xx 时
            # 请求可能已被受理,重发会重复计费
            if e.status_code not in (400, 404, 422):
                raise
            body.pop("response_format", None)
            data = self._request("POST", "/v1/images/generations", json=body, timeout=300.0)
        try:
            item = data["data"][0]
        except (KeyError, IndexError, TypeError):
            raise AICallError(f"图片模型响应格式异常:{_snippet(json.dumps(data, ensure_ascii=False))}") from None

        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), ".png"
        if item.get("url"):
            return self.download(item["url"]), ".png"
        raise AICallError("图片模型响应中没有 b64_json 或 url 字段")

    def download(self, url: str, timeout: float = 600.0) -> bytes:
        try:
            resp = self.client.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as e:
            raise AICallError(f"下载文件失败({url}):{e.__class__.__name__}: {e}") from e

    # ------------------------------------------------------------ 视频生成

    def _image_data_url(self, data: bytes, mime: str | None = None) -> str:
        return f"data:{mime or 'image/png'};base64," + base64.b64encode(data).decode()

    def _submit_video(
        self,
        model: str,
        prompt: str,
        image_bytes: bytes | None,
        duration: int,
        image_mime: str = "image/png",
        negative_prompt: str = "",
        *,
        image_inputs: list[tuple[bytes, str]] | None = None,
        ratio: str | None = None,
        resolution: str | None = None,
        generate_audio: bool | None = None,
    ) -> tuple[str | None, str | None]:
        """提交视频任务,返回 (task_id, 已经完成的直链 URL)。"""
        if self.provider == "openai_videos":
            if image_bytes is not None:
                # Sora 风格接口用 multipart 传参考图
                try:
                    resp = self.client.post(
                        f"{self.base}/v1/videos",
                        data={"model": model, "prompt": prompt, "seconds": str(duration)},
                        files={"input_reference": ("reference.png", image_bytes, image_mime)},
                        timeout=180.0,
                    )
                    if resp.status_code >= 400:
                        raise AICallError(f"模型服务返回 {resp.status_code}(/v1/videos):{_snippet(resp.text)}")
                    data = resp.json()
                except httpx.HTTPError as e:
                    raise AICallError(f"请求模型服务失败(/v1/videos):{e.__class__.__name__}: {e}") from e
            else:
                data = self._request(
                    "POST",
                    "/v1/videos",
                    json={"model": model, "prompt": prompt, "seconds": duration},
                )
        else:
            body: dict[str, Any] = {"model": model, "prompt": prompt, "duration": duration}
            if negative_prompt:
                # 部分网关/模型(可灵/Vidu 等)支持负向提示词;不识别会 400,由下面的降级重试剔除
                body["negative_prompt"] = negative_prompt
            if image_inputs is not None:
                image_urls = [self._image_data_url(data, mime) for data, mime in image_inputs]
                metadata: dict[str, Any] = {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": url},
                            "role": "reference_image",
                        }
                        for url in image_urls
                    ],
                    # 兼容较早的 new-api Doubao 渠道实现。
                    "image_urls": image_urls,
                    "duration": duration,
                }
                if ratio:
                    metadata["ratio"] = ratio
                if resolution:
                    metadata["resolution"] = resolution
                if generate_audio is not None:
                    metadata["generate_audio"] = generate_audio
                body["images"] = image_urls
                body["metadata"] = metadata
            elif image_bytes is not None:
                body["image_url"] = self._image_data_url(image_bytes, image_mime)
            try:
                data = self._request("POST", "/v1/video/generations", json=body)
            except AICallError as e:
                # 仅参数类 4xx 才降级重试(去掉 duration/negative_prompt 等可选参数);其他错误重发
                # 可能造成网关重复受理、重复计费
                if e.status_code not in (400, 404, 422):
                    raise
                minimal: dict[str, Any] = {"model": model, "prompt": prompt}
                if image_inputs is not None:
                    # 已明确收到参数类错误才降级；保留全部参考图，只去掉兼容 metadata。
                    minimal["duration"] = duration
                    minimal["images"] = [
                        self._image_data_url(data, mime) for data, mime in image_inputs
                    ]
                elif image_bytes is not None:
                    minimal["image_url"] = self._image_data_url(image_bytes, image_mime)
                data = self._request("POST", "/v1/video/generations", json=minimal)

        # 某些渠道同步直接返回视频 URL
        direct = extract_video_url(data)
        if direct:
            return None, direct
        task_id = self._extract_task_id(data)
        if not task_id:
            raise AICallError(f"无法从视频任务响应中解析任务 ID:{_snippet(json.dumps(data, ensure_ascii=False))}")
        return task_id, None

    @staticmethod
    def _extract_task_id(data: Any) -> str | None:
        for container in (data, data.get("data") if isinstance(data, dict) else None):
            if not isinstance(container, dict):
                continue
            for key in ("id", "task_id", "taskId", "taskID", "video_id"):
                v = container.get(key)
                if isinstance(v, (str, int)) and str(v).strip():
                    return str(v)
        return None

    def _poll_video(self, task_id: str) -> tuple[str, str | None]:
        """查询一次任务状态,返回 (processing/done/failed, 视频 URL 或 None)。"""
        if self.provider == "openai_videos":
            data = self._request("GET", f"/v1/videos/{task_id}", timeout=60.0)
        else:
            data = self._request("GET", f"/v1/video/generations/{task_id}", timeout=60.0)
        status = extract_task_status(data)
        url = extract_video_url(data)
        if status == "done" or (status is None and url):
            return "done", url
        return status or "processing", url

    def run_video(
        self,
        model: str,
        prompt: str,
        *,
        image_bytes: bytes | None = None,
        image_mime: str = "image/png",
        image_inputs: list[tuple[bytes, str]] | None = None,
        negative_prompt: str = "",
        duration: int = 5,
        ratio: str | None = None,
        resolution: str | None = None,
        generate_audio: bool | None = None,
        poll_interval: float = 5.0,
        timeout_seconds: float = 900.0,
        on_progress: Callable[[str], None] | None = None,
        on_submitted: Callable[[str], None] | None = None,
        resume_task_id: str | None = None,
        max_poll_errors: int = 6,
    ) -> dict[str, Any]:
        """阻塞式跑完整个视频生成,返回 {url, content, task_id}。

        content 为视频文件字节(优先下载到本地);下载失败时只有 url。
        - on_submitted:提交成功拿到 task_id 后立即回调(用于落库,支撑进程重启后恢复)
        - resume_task_id:带上已提交任务的 ID,跳过提交直接轮询(重启恢复用)
        - negative_prompt:负向提示词(风格预设提供,仅 new-api 任务式接口会传)
        - 单次状态查询失败不致命(网络抖动/网关瞬时 5xx),连续 max_poll_errors 次才判失败
        """
        if not model:
            raise AICallError("当前配置未填写视频模型(video_model),无法生成视频")

        if resume_task_id:
            task_id = resume_task_id
        else:
            submit_options: dict[str, Any] = {}
            if image_inputs is not None:
                submit_options["image_inputs"] = image_inputs
            if ratio is not None:
                submit_options["ratio"] = ratio
            if resolution is not None:
                submit_options["resolution"] = resolution
            if generate_audio is not None:
                submit_options["generate_audio"] = generate_audio
            task_id, direct_url = self._submit_video(
                model,
                prompt,
                image_bytes,
                duration,
                image_mime,
                negative_prompt,
                **submit_options,
            )
            if direct_url:
                return {"url": direct_url, "task_id": None}
            if task_id and on_submitted:
                on_submitted(task_id)

        deadline = time.monotonic() + timeout_seconds
        poll_errors = 0
        while True:
            if time.monotonic() > deadline:
                raise AICallError(f"视频生成超时({int(timeout_seconds)} 秒),任务 ID:{task_id}")
            try:
                status, url = self._poll_video(task_id)
            except AICallError as e:
                poll_errors += 1
                if poll_errors >= max_poll_errors:
                    raise AICallError(
                        f"查询任务状态连续失败 {poll_errors} 次,已中止(任务 ID:{task_id};最后错误:{e})"
                    ) from e
                time.sleep(poll_interval)
                continue
            poll_errors = 0
            if on_progress:
                on_progress(f"任务 {task_id}:{'生成中' if status == 'processing' else status}")
            if status == "done":
                if self.provider == "openai_videos" and not url:
                    # Sora 风格:从 content 端点下载
                    try:
                        content = self.download(f"{self.base}/v1/videos/{task_id}/content")
                        return {"url": None, "content": content, "task_id": task_id}
                    except AICallError:
                        raise AICallError(f"视频已生成但下载失败,任务 ID:{task_id}") from None
                if url:
                    return {"url": url, "task_id": task_id}
                raise AICallError(f"任务已完成但未找到视频地址,任务 ID:{task_id}")
            if status == "failed":
                raise AICallError(f"视频生成失败,任务 ID:{task_id}(可在网关后台查看详情)")
            time.sleep(poll_interval)
