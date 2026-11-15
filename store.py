"""虚现互通 · 持久化与绑定管理。

所有运行数据都落在 AstrBot 的 plugin_data 目录下（插件重装不丢）：
- state.json   角色状态、房主、token、绑定码、设置、事件环形缓冲
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import time
from pathlib import Path
from typing import Any

MAX_EVENTS = 200


def _now() -> float:
    return time.time()


def default_character(name: str = "小虚") -> dict[str, Any]:
    ts = _now()
    return {
        "id": "vb1",
        "name": name,
        "level": 1,
        "exp": 0,
        "exp_next": 120,
        "stats": {
            "affection": 20,
            "mood": 70,
            "energy": 80,
            "satiety": 70,
            "clean": 90,
            "trust": 10,
        },
        "emotion": "calm",
        "action": "idle",
        "pose": {"x": 0.5, "y": 0.62, "facing": 1, "scale": 1.0},
        "outfit": "default",
        "room": "day",
        # 角色外观来源：canvas（内置 2D）｜image（上传的立绘）｜model（glb/gltf 3D 模型）
        "renderer": "canvas",
        # 立绘/模型地址（相对 /pet/ 的路径，或完整 http(s) URL）
        "image_url": "",
        "model_url": "",
        "bubble": "",
        "counters": {"pets": 0, "pokes": 0, "feeds": 0, "chats": 0, "touches": 0},
        "last_active": ts,
        "created_at": ts,
        "updated_at": ts,
    }


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "character": default_character(),
        "owner": {"qq": "", "umo": "", "nickname": ""},
        "tokens": {},
        "bind_codes": {},
        "settings": {
            "muted": False,
            "mirror_app_to_qq": True,
            "touch_to_qq": True,
            "touch_qq_interval": 30,
            "auto_reply_private": True,
            "whitelist": [],
        },
        "ei": 1,          # next event id
        "events": [],
        "bind_fails": [],
        "saved_at": _now(),
    }


class Store:
    """线程/协程安全的 JSON 状态仓库（aiohttp 与 AstrBot 主循环共用）。"""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self._lock = asyncio.Lock()
        self._dirty = False
        self._save_task: asyncio.Task | None = None
        self.data: dict[str, Any] = default_state()
        self._subscribers: list[Any] = []   # callable(event) -> awaitable

    # ---------- 生命周期 ----------

    def load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                base = default_state()
                base.update(raw or {})
                # 逐层补全，避免旧版本缺字段
                base["character"] = {**default_character(), **(raw.get("character") or {})}
                base["character"]["stats"] = {
                    **default_character()["stats"],
                    **((raw.get("character") or {}).get("stats") or {}),
                }
                base["character"]["counters"] = {
                    **default_character()["counters"],
                    **((raw.get("character") or {}).get("counters") or {}),
                }
                # 2.0 新增的外观字段：旧状态里没有就补默认值
                for _k in ("renderer", "image_url", "model_url"):
                    if _k not in base["character"]:
                        base["character"][_k] = default_character()[_k]
                base["settings"] = {**default_state()["settings"], **(raw.get("settings") or {})}
                base["owner"] = {**default_state()["owner"], **(raw.get("owner") or {})}
                self.data = base
            except Exception:
                # 状态文件坏了也要能起来，先备份再重置
                try:
                    self.path.replace(self.path.with_suffix(".bak.json"))
                except Exception:
                    pass
                self.data = default_state()

    async def save(self) -> None:
        async with self._lock:
            self.data["saved_at"] = _now()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False

    def touch_dirty(self) -> None:
        """标记需要保存，2 秒防抖后落盘。"""
        self._dirty = True
        if self._save_task and not self._save_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_task = loop.create_task(self._debounced_save())

    async def _debounced_save(self) -> None:
        await asyncio.sleep(2)
        if self._dirty:
            try:
                await self.save()
            except Exception:
                pass

    # ---------- 事件总线 ----------

    def subscribe(self, cb) -> None:
        if cb not in self._subscribers:
            self._subscribers.append(cb)

    def unsubscribe(self, cb) -> None:
        if cb in self._subscribers:
            self._subscribers.remove(cb)

    def push_event(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        ev = {
            "id": self.data["ei"],
            "at": _now(),
            "kind": kind,
            "payload": payload or {},
        }
        self.data["ei"] = ev["id"] + 1
        evs = self.data["events"]
        evs.append(ev)
        if len(evs) > MAX_EVENTS:
            del evs[:-MAX_EVENTS]
        self.touch_dirty()
        return ev

    async def emit(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """写入事件并广播给所有 WebSocket 订阅者。"""
        ev = self.push_event(kind, payload)
        for cb in list(self._subscribers):
            try:
                res = cb(ev)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        return ev

    def events_since(self, since_id: int) -> list[dict[str, Any]]:
        return [e for e in self.data["events"] if e["id"] > since_id]

    def last_event_id(self) -> int:
        return int(self.data["ei"]) - 1

    # ---------- 绑定 ----------

    def make_code(self, qq: str, ttl: int = 600) -> str:
        codes = self.data["bind_codes"]
        # 清掉过期
        now = _now()
        for k in [k for k, v in codes.items() if v.get("expire_at", 0) < now]:
            codes.pop(k, None)
        code = "".join(random.choice("0123456789") for _ in range(6))
        codes[code] = {"qq": str(qq), "expire_at": now + ttl, "created_at": now}
        self.touch_dirty()
        return code

    def verify_code(self, code: str) -> tuple[bool, str, str]:
        """返回 (是否成功, 失败原因, qq)。"""
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        now = _now()
        rec = self.data["bind_codes"].get(code)
        if not rec:
            return False, "bad_code", ""
        if rec.get("expire_at", 0) < now:
            self.data["bind_codes"].pop(code, None)
            self.touch_dirty()
            return False, "expired", ""
        qq = str(rec.get("qq") or "")
        # 房主认领：第一次绑定谁就是谁的
        owner = self.data["owner"]
        if owner.get("qq") and qq and owner["qq"] != qq:
            return False, "bad_code", ""
        self.data["bind_codes"].pop(code, None)
        self.touch_dirty()
        return True, "", qq

    def add_token(self, qq: str, device: str, platform: str) -> str:
        token = secrets.token_hex(16)
        self.data["tokens"][token] = {
            "qq": str(qq),
            "device": str(device or "")[:64],
            "platform": str(platform or "")[:16],
            "created_at": _now(),
            "last_seen": _now(),
        }
        self.touch_dirty()
        return token

    def check_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        rec = self.data["tokens"].get(token)
        if rec:
            rec["last_seen"] = _now()
            self.touch_dirty()
        return rec

    def revoke_tokens(self) -> int:
        n = len(self.data["tokens"])
        self.data["tokens"] = {}
        self.touch_dirty()
        return n

    def rate_limited(self, ip: str, limit: int = 10, window: int = 300) -> bool:
        now = _now()
        fails = [t for t in self.data["bind_fails"] if now - t < window]
        self.data["bind_fails"] = fails
        return len(fails) >= limit

    def note_bind_fail(self) -> None:
        self.data["bind_fails"].append(_now())
        self.data["bind_fails"] = self.data["bind_fails"][-50:]
        self.touch_dirty()

    # ---------- 便捷访问 ----------

    @property
    def character(self) -> dict[str, Any]:
        return self.data["character"]

    @property
    def owner(self) -> dict[str, Any]:
        return self.data["owner"]

    @property
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    def set_owner(self, qq: str, umo: str = "", nickname: str = "") -> None:
        owner = self.data["owner"]
        if qq:
            owner["qq"] = str(qq)
        if umo:
            owner["umo"] = str(umo)
        if nickname:
            owner["nickname"] = str(nickname)
        self.touch_dirty()

    def bound(self) -> bool:
        return bool(self.data["tokens"]) and bool(self.data["owner"].get("qq"))
