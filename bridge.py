"""虚现互通 · 内嵌 HTTP + WebSocket 服务（aiohttp）。

插件启动时在容器里监听 0.0.0.0:<port>（默认 8899），对外由 socat sidecar +
nginx 暴露成 https://samachat.de5.net/pet/ 。协议见 docs/PROTOCOL.md。

设计要点：
- 静态资源直接由本服务托管（同一来源，前端不用配 CORS；file:// 场景也保留了 CORS 头）。
- 所有 /api/* 除 hello/bind 外都要 Bearer token。
- 事件总线：引擎 emit 的事件 → 广播给所有 WebSocket 客户端；同时留一份环形缓冲，
  给 GET /api/events?since= 做降级轮询。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

WS_PING_INTERVAL = 25.0


class Bridge:
    def __init__(self, engine, store, *, host: str = "0.0.0.0", port: int = 8899,
                 webapp_dir: str | Path | None = None, token_required: bool = True,
                 logger=None):
        self.engine = engine
        self.store = store
        self.host = host
        self.port = int(port)
        self.webapp_dir = Path(webapp_dir) if webapp_dir else None
        self.token_required = token_required
        self.log = logger
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._clients: set[web.WebSocketResponse] = set()
        self._ping_task: asyncio.Task | None = None
        self._started_at = time.time()

    # ------------------------------------------------------------ 启动/停止

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/api/hello", self.h_hello)
        app.router.add_post("/api/bind", self.h_bind)
        app.router.add_get("/api/state", self.h_state)
        app.router.add_post("/api/touch", self.h_touch)
        app.router.add_post("/api/action", self.h_action)
        app.router.add_post("/api/chat", self.h_chat)
        app.router.add_get("/api/events", self.h_events)
        app.router.add_post("/api/device", self.h_device)
        app.router.add_post("/api/unbind", self.h_unbind)
        app.router.add_get("/ws", self.h_ws)
        app.router.add_get("/", self.h_static)
        app.router.add_get("/{tail:.*}", self.h_static)
        self.app = app
        return app

    async def start(self) -> None:
        if self.app is None:
            self.build_app()
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        if self.log:
            self.log.info(f"[virtual-body] 虚现互通服务已启动：http://{self.host}:{self.port}")
        self.store.subscribe(self._on_event)
        self._ping_task = asyncio.get_running_loop().create_task(self._heartbeat())

    async def stop(self) -> None:
        self.store.unsubscribe(self._on_event)
        for ws in list(self._clients):
            try:
                await ws.close(code=WSMsgType.CLOSE, message=b"server shutdown")
            except Exception:
                pass
        self._clients.clear()
        for task in (self._ping_task,):
            if task and not task.done():
                task.cancel()
        if self.runner:
            try:
                await self.runner.cleanup()
            except Exception:
                pass
        self.runner = None
        self.site = None
        if self.log:
            self.log.info("[virtual-body] 虚现互通服务已停止")

    # ------------------------------------------------------------ 中间件 / 工具

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=self._cors())
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            if self.log:
                self.log.warning(f"[virtual-body] {request.method} {request.path} 出错：{e!r}")
            resp = self._json({"ok": False, "error": "server_error", "message": str(e)}, status=500)
        if isinstance(resp, web.Response):
            resp.headers.update(self._cors())
        return resp

    @staticmethod
    def _cors() -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Max-Age": "600",
        }

    @staticmethod
    def _json(data: Any, status: int = 200) -> web.Response:
        return web.json_response(
            data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    def _token(self, request: web.Request) -> str:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.query.get("token", "").strip()

    def _auth(self, request: web.Request) -> dict[str, Any] | None:
        if not self.token_required:
            return {"qq": self.store.owner.get("qq") or "dev", "device": "dev"}
        return self.store.check_token(self._token(request))

    def _unauth(self) -> web.Response:
        return self._json(
            {"ok": False, "error": "unbound",
             "message": "还没有绑定，请先在 QQ 里发送「虚现 绑定码」"},
            status=401,
        )

    async def _body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------ 事件广播

    async def _on_event(self, ev: dict[str, Any]) -> None:
        payload = ev.get("payload") or {}
        frame: dict[str, Any] = {"t": ev.get("kind"), "id": ev.get("id"), "at": ev.get("at")}
        # say/notice 等帧把 payload 平铺，前端好写
        for k, v in payload.items():
            if k not in frame:
                frame[k] = v
        if not self._clients:
            return
        text = json.dumps(frame, ensure_ascii=False)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL)
            if not self._clients:
                continue
            ts = time.time()
            dead = []
            for ws in list(self._clients):
                try:
                    await ws.send_str(json.dumps({"t": "ping", "ts": ts}))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
            # 顺手落盘一次状态，保证断电也不丢
            try:
                await self.store.save()
            except Exception:
                pass

    # ------------------------------------------------------------ 静态前端

    async def h_static(self, request: web.Request) -> web.StreamResponse:
        if self.webapp_dir is None:
            return web.Response(text="virtual-body: webapp 目录未配置", content_type="text/plain")
        tail = request.match_info.get("tail", "") or ""
        if tail in ("", "/"):
            tail = "index.html"
        target = (self.webapp_dir / tail).resolve()
        try:
            target.relative_to(self.webapp_dir.resolve())
        except ValueError:
            return web.Response(status=403, text="forbidden")
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            # 单页应用：未知路径回落到 index.html
            target = self.webapp_dir / "index.html"
            if not target.exists():
                return web.Response(status=404, text="webapp/index.html 不存在")
        resp = web.FileResponse(target)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ------------------------------------------------------------ API

    async def h_hello(self, request: web.Request) -> web.Response:
        return self._json({"ok": True, "data": {
            "service": "virtual-body",
            "version": "1.0.1",
            "bound": bool(self.store.owner.get("qq")),
            "uptime": int(time.time() - self._started_at),
            "server_time": time.time(),
            "features": ["ws", "events_poll", "speech_bubble", "touch", "llm"],
        }})

    async def h_bind(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        ip = request.headers.get("X-Forwarded-For", request.remote or "?")
        if self.store.rate_limited(ip):
            return self._json({"ok": False, "error": "rate_limited",
                              "message": "尝试太频繁了，等 5 分钟再试"}, status=429)
        code = str(body.get("code") or "")
        ok, reason, qq = self.store.verify_code(code)
        if not ok:
            self.store.note_bind_fail()
            msg = {"bad_code": "绑定码错误或已过期", "expired": "绑定码已过期，重新发送「虚现 绑定码」"}.get(
                reason, "绑定失败")
            return self._json({"ok": False, "error": reason or "bad_code", "message": msg}, status=400)
        qq = qq or self.store.owner.get("qq", "")
        self.store.set_owner(qq)
        token = self.store.add_token(qq, str(body.get("device") or ""), str(body.get("platform") or ""))
        await self.store.save()
        await self.engine.announce("App 已连接 ✅", "info")
        if self.engine.send_qq and self.store.owner.get("umo"):
            await self.engine._qq_note(
                f"📱 设备「{body.get('device') or '未知设备'}」刚刚绑定了你的角色。"
                f"（发送「虚现 解绑」可以踢掉所有 App）", force=True)
        return self._json({"ok": True, "data": {
            "token": token,
            "qq": qq,
            "nickname": self.store.owner.get("nickname", ""),
            "character": self.engine.snapshot(),
        }})

    async def h_state(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return self._unauth()
        since = int(request.query.get("since", "0") or 0)
        return self._json({"ok": True, "data": {
            "character": self.engine.snapshot(),
            "bound": True,
            "muted": bool(self.store.settings.get("muted")),
            "owner_qq": self.store.owner.get("qq", ""),
            "nickname": self.store.owner.get("nickname", ""),
            "events": self.store.events_since(since) if since else self.store.data["events"][-20:],
            "last_id": self.store.last_event_id(),
            "server_time": time.time(),
        }})

    async def h_touch(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return self._unauth()
        body = await self._body(request)
        data = await self.engine.handle_touch(body)
        return self._json({"ok": True, "data": data})

    async def h_action(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return self._unauth()
        body = await self._body(request)
        data = await self.engine.handle_action(str(body.get("action") or ""), body.get("args") or {})
        if isinstance(data, dict) and data.get("ok") is False:
            return self._json({"ok": False, "error": data.get("error"), "message": data.get("message")},
                              status=400)
        return self._json({"ok": True, "data": data})

    async def h_chat(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return self._unauth()
        body = await self._body(request)
        data = await self.engine.handle_chat(str(body.get("text") or ""))
        return self._json({"ok": True, "data": data})

    async def h_events(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return self._unauth()
        since = int(request.query.get("since", "0") or 0)
        return self._json({"ok": True, "data": {
            "events": self.store.events_since(since),
            "last_id": self.store.last_event_id(),
            "character": self.engine.snapshot(),
        }})

    async def h_device(self, request: web.Request) -> web.Response:
        rec = self._auth(request)
        if not rec:
            return self._unauth()
        body = await self._body(request)
        if "muted" in body:
            self.store.settings["muted"] = bool(body["muted"])
        rec["device"] = str(body.get("device") or rec.get("device") or "")[:64]
        rec["platform"] = str(body.get("platform") or rec.get("platform") or "")[:16]
        rec["app_version"] = str(body.get("app_version") or "")[:16]
        self.store.touch_dirty()
        return self._json({"ok": True, "data": {"server_time": time.time()}})

    async def h_unbind(self, request: web.Request) -> web.Response:
        token = self._token(request)
        rec = self._auth(request)
        if not rec:
            return self._unauth()
        body = await self._body(request)
        if body.get("all"):
            n = self.store.revoke_tokens()
            await self.engine.announce(f"已解绑全部 {n} 台设备", "warn")
        else:
            self.store.data["tokens"].pop(token, None)
            await self.engine.announce("这台设备已解绑", "warn")
        await self.store.save()
        return self._json({"ok": True, "data": {"unbound": True}})

    # ------------------------------------------------------------ WebSocket

    async def h_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=1 << 20)
        await ws.prepare(request)
        if self.token_required and not self.store.check_token(self._token(request)):
            await ws.send_str(json.dumps(
                {"t": "notice", "level": "error", "text": "未绑定，请先在 QQ 里发送「虚现 绑定码」"},
                ensure_ascii=False))
            await ws.close(code=4001, message=b"unbound")
            return ws
        self._clients.add(ws)
        try:
            await ws.send_str(json.dumps({
                "t": "hello",
                "state": {"character": self.engine.snapshot(),
                          "muted": bool(self.store.settings.get("muted")),
                          "owner_qq": self.store.owner.get("qq", ""),
                          "nickname": self.store.owner.get("nickname", "")},
                "events": self.store.data["events"][-20:],
                "last_id": self.store.last_event_id(),
                "server_time": time.time(),
            }, ensure_ascii=False))
            await self.engine.announce(f"App 已连上（{len(self._clients)} 台在线）", "info")
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_ws_text(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
                elif msg.type == WSMsgType.CLOSE:
                    break
        finally:
            self._clients.discard(ws)
            try:
                self.store.touch_dirty()
            except Exception:
                pass
        return ws

    async def _on_ws_text(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        t = data.get("t")
        try:
            if t == "touch":
                res = await self.engine.handle_touch(data)
                await self._ws_reply(ws, {"t": "ack", "of": "touch", "data": res})
            elif t == "chat":
                res = await self.engine.handle_chat(str(data.get("text") or ""))
                await self._ws_reply(ws, {"t": "ack", "of": "chat", "data": res})
            elif t == "action":
                res = await self.engine.handle_action(str(data.get("action") or ""), data.get("args") or {})
                await self._ws_reply(ws, {"t": "ack", "of": "action", "data": res})
            elif t == "device":
                if "muted" in data:
                    self.store.settings["muted"] = bool(data["muted"])
                self.store.touch_dirty()
                await self._ws_reply(ws, {"t": "ack", "of": "device", "data": {"ok": True}})
            elif t == "ping":
                await ws.send_str(json.dumps({"t": "pong", "ts": time.time()}))
        except Exception as e:
            if self.log:
                self.log.warning(f"[virtual-body] WS 处理 {t} 失败：{e!r}")
            await self._ws_reply(ws, {"t": "error", "of": t, "message": str(e)})

    @staticmethod
    async def _ws_reply(ws: web.WebSocketResponse, obj: dict[str, Any]) -> None:
        try:
            await ws.send_str(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass
