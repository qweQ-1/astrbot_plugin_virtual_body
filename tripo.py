"""虚现互通 · 图生 3D（Tripo3D 接入）

用户在 App 里传一张图 → 这里调 Tripo 的 `/v3/generation/image-to-model` →
轮询任务 → 把生成好的 glb 落到 webapp/uploads/ → 角色自动换上 3D 模型。

协议依据官方 SDK（tripo3d 1.0.0）：
- 上传：``POST /v3/files`` (multipart, 字段名 file) → ``{"file_token": "file_..."}``
- 建任务：``POST /v3/generation/image-to-model`` body ``{"input": <file_token|url>, "model": "..."}``
  → ``{"code":0,"data":{"task_id": "..."}}``
- 轮询：``GET /v3/tasks/{task_id}`` → ``data.status`` 为 queued/running/success/failed，
  成功时 ``data.output.pbr_model``（或 ``model``）是 glb 的下载地址。

没配 Key 时所有入口都返回明确的 ``no_api_key``，不会静默失败。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

BASE_URL = "https://openapi.tripo3d.ai"
DEFAULT_MODEL = "v2.5-20250123"
POLL_INTERVAL = 4.0
POLL_TIMEOUT = 300.0        # 单次生成最多等 5 分钟


class TripoError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TripoClient:
    """只用 aiohttp，不依赖官方 SDK（插件里不想多加一个包）。"""

    def __init__(self, api_key: str, *, logger=None, base_url: str = BASE_URL):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.log = logger

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _json(self, method: str, path: str, *, data=None, params=None) -> dict[str, Any]:
        import aiohttp

        url = self.base_url + path
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            req = {"headers": self._headers(), "params": params}
            if data is not None:
                req["json"] = data
            async with s.request(method, url, **req) as r:
                text = await r.text()
                try:
                    payload = json.loads(text)
                except Exception:
                    raise TripoError(f"http_{r.status}", f"服务返回的不是 JSON：{text[:200]}")
                if r.status >= 400:
                    msg = payload.get("message") or payload.get("msg") or text[:200]
                    raise TripoError(f"http_{r.status}", str(msg))
                # Tripo 统一信封：{"code": 0, "data": {...}}；code!=0 视为失败
                if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                    raise TripoError(f"api_{payload.get('code')}",
                                     str(payload.get("message") or "调用失败"))
                return payload.get("data", payload) if isinstance(payload, dict) else {}

    # ------------------------------------------------------------ 步骤

    async def upload(self, path: str | Path) -> str:
        """上传本地图片，返回 file_token。"""
        import aiohttp

        path = Path(path)
        form = aiohttp.FormData()
        with open(path, "rb") as fh:
            form.add_field("file", fh, filename=path.name,
                           content_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg")
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.base_url + "/v3/files",
                                  headers=self._headers(), data=form) as r:
                    text = await r.text()
                    try:
                        payload = json.loads(text)
                    except Exception:
                        raise TripoError(f"http_{r.status}", f"上传返回异常：{text[:200]}")
                    if r.status >= 400 or (isinstance(payload, dict) and payload.get("code") not in (None, 0)):
                        raise TripoError(f"http_{r.status}",
                                         str(payload.get("message") or text[:200]))
                    data = payload.get("data", payload) if isinstance(payload, dict) else {}
                    token = data.get("file_token")
                    if not token:
                        raise TripoError("no_token", f"上传响应里没有 file_token：{text[:200]}")
                    return str(token)

    async def create_image_task(self, *, file_token: str = "", image_url: str = "",
                                model: str = DEFAULT_MODEL) -> str:
        payload: dict[str, Any] = {"model": model, "texture": True, "pbr": True}
        if file_token:
            payload["input"] = file_token
        elif image_url:
            payload["input"] = image_url
        else:
            raise TripoError("no_input", "必须提供 file_token 或图片 URL")
        data = await self._json("POST", "/v3/generation/image-to-model", data=payload)
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise TripoError("no_task", f"没有拿到 task_id：{data}")
        return str(task_id)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v3/tasks/{task_id}")

    async def wait_task(self, task_id: str, *, timeout: float = POLL_TIMEOUT) -> dict[str, Any]:
        started = time.time()
        while True:
            data = await self.get_task(task_id)
            status = str(data.get("status") or "").lower()
            if status in ("success", "succeed", "succeeded", "finished"):
                return data
            if status in ("failed", "error", "cancelled", "canceled"):
                raise TripoError("task_failed",
                                 str(data.get("message") or "生成失败（模型服务返回 failed）"))
            if time.time() - started > timeout:
                raise TripoError("timeout", f"等了 {int(timeout)} 秒还没生成完，稍后重试")
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    def model_url_from(task: dict[str, Any]) -> str:
        """从任务结果里挑出 glb 地址（优先 pbr_model）。"""
        out = task.get("output") or {}
        for key in ("pbr_model", "model", "glb", "base_model"):
            v = out.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                return v["url"]
        # 有些版本把结果放在 pbr_model/model 的 dict 列表里
        for v in out.values():
            if isinstance(v, dict):
                u = v.get("url")
                if isinstance(u, str) and u.endswith((".glb", ".gltf")):
                    return u
        return ""

    async def download(self, url: str, dest: Path) -> int:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    raise TripoError(f"download_{r.status}", "模型下载失败")
                blob = await r.read()
        dest.write_bytes(blob)
        return len(blob)
