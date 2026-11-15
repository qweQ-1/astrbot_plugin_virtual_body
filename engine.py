"""虚现互通 · 核心引擎。

这里是「虚 ↔ 实」两条腿交汇的地方：

    App 触摸  → handle_touch() → 角色反应 → 推回 App（说话/动作/特效）
                                        └→ 播报到 QQ（节流）

    QQ 消息   → handle_qq_message() → 镜像到 App（角色听见了）
                                     └→ LLM 回复 → 角色在 App 里说出口 + 回复到 QQ

    App 说话  → handle_chat() → 角色回复 → 推回 App + 镜像到 QQ

引擎不直接依赖 AstrBot 的消息类：发 QQ 的动作由 ``send_qq`` 回调注入，
这样引擎可以脱离 AstrBot 单独跑单测。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from . import reactions as R
from .character import (
    add_exp,
    apply_delta,
    decay,
    heuristic_emotion,
    normalize_action,
    normalize_emotion,
    set_mood_pose,
    status_card,
)
from .store import Store

SendQQ = Callable[[str], Awaitable[bool]]


def _strip_json_tail(text: str) -> str:
    """去掉模型在句尾附带的 {...} JSON 片段，只留人说的话。"""
    import re as _re

    raw = (text or "").strip()
    if not raw:
        return ""
    # 只在 JSON 出现在末尾时裁剪，避免把台词里的花括号内容也删掉
    m = _re.search(r"\{\s*\"[a-z_]+\"\s*:.*?\}\s*$", raw, _re.S)
    if m:
        raw = raw[: m.start()].strip()
    return raw or text.strip()


class Engine:
    def __init__(
        self,
        store: Store,
        *,
        context=None,
        config: dict | None = None,
        logger=None,
        send_qq: SendQQ | None = None,
    ):
        self.store = store
        self.context = context
        self.cfg = config or {}
        self.log = logger
        self.send_qq = send_qq
        self._last_touch_llm: dict[str, float] = {}
        self._last_qq_broadcast = 0.0
        self._history: list[str] = []
        self._decay_task: asyncio.Task | None = None
        self._busy_lock = asyncio.Lock()
        # 最近一次解析到的 QQ 人格 id（仅用于显示/排查，不影响逻辑）
        self.active_persona_name: str = ""

    # ------------------------------------------------------------ 便捷属性

    @property
    def char(self) -> dict[str, Any]:
        return self.store.character

    def _persona(self) -> str:
        return str(self.cfg.get("persona", "") or "")

    def _dir_name(self) -> str:
        return str(self.cfg.get("owner_call", "") or "")

    def snapshot(self) -> dict[str, Any]:
        decay(self.char)
        return self.char

    def _remember(self, line: str) -> None:
        self._history.append(line)
        if len(self._history) > 16:
            del self._history[:-16]

    def _history_text(self) -> str:
        if not self._history:
            return ""
        return "最近发生的事：\n" + "\n".join(f"- {h}" for h in self._history[-8:])

    async def _provider(self):
        if self.context is None:
            return None
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    async def qq_persona_prompt(self) -> str:
        """取「QQ 那边正在用的人格」，让 App 里的对话和 QQ 完全一致。

        AstrBot 解析人格的唯一入口是 ``resolve_selected_persona``（会话级设置 >
        会话配置 > 全局默认）。这里传同一个 umo 去问它，拿到的就是 QQ 里生效的那一个；
        注入方式也照抄 AstrBot 自己（``# Persona Instructions`` 段落）。
        任何一步失败都只回落，不会影响对话本身。
        """
        ctx = self.context
        if ctx is None:
            return ""
        try:
            pm = getattr(ctx, "persona_manager", None)
            if pm is None:
                return ""
            umo = str(self.store.owner.get("umo") or "")
            provider_settings = {}
            try:
                provider_settings = self.cfg.get("provider_settings") or {}
            except Exception:
                provider_settings = {}
            if umo:
                persona_id, persona, _force, _webchat = await pm.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=None,
                    platform_name="aiocqhttp",
                    provider_settings=provider_settings,
                )
            else:
                # 房主还没在 QQ 里说过话 → 没有 umo，直接取全局默认人格
                # （就是 QQ 默认会用的那一个）
                persona_id, persona = "default", None
                try:
                    persona = await pm.get_default_persona_v3(None)
                    persona_id = getattr(persona, "persona_id", "default") if persona else "default"
                    if isinstance(persona, dict):
                        persona_id = persona.get("name") or "default"
                except Exception:
                    persona = None
            prompt = ""
            if persona is None:
                # 没匹配到人格（例如 umo 为空、或用的是系统默认）——这是正常情况，静默回落
                if self.log:
                    self.log.debug(f"[virtual-body] 未匹配到人格（persona_id={persona_id!r}），使用内置性格")
                return ""
            if isinstance(persona, dict):
                prompt = persona.get("prompt") or persona.get("system_prompt") or ""
            else:
                prompt = getattr(persona, "prompt", "") or ""
            if not prompt:
                return ""
            # 记录一下用的是哪个人格，方便前端显示 / 排查
            self.active_persona_name = str(persona_id or "")
            return f"\n# Persona Instructions\n\n{prompt}\n"
        except Exception as e:
            if self.log:
                self.log.debug(f"[virtual-body] 读取 QQ 人格失败（忽略）：{e}")
            return ""

    # ------------------------------------------------------------ 生命周期

    def start_background(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self._decay_task or self._decay_task.done():
            self._decay_task = loop.create_task(self._decay_loop())

    async def _decay_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                before = dict(self.char.get("stats", {}))
                delta = decay(self.char)
                if delta:
                    await self.store.emit("state", {"character": self.char, "patch": {"stats": before}})
                await self.store.save()
            except Exception as e:  # 后台任务绝不能死
                if self.log:
                    self.log.warning(f"[virtual-body] 衰减循环出错：{e}")
                await asyncio.sleep(60)

    # ------------------------------------------------------------ 反应输出

    async def _emit_say(
        self,
        text: str,
        emotion: str,
        action: str,
        effect: str = "none",
        *,
        source: str = "app",
        speech: bool = True,
        dur: int = 1300,
    ) -> None:
        c = self.char
        c["bubble"] = text
        c["emotion"] = normalize_emotion(emotion, c.get("emotion", "calm"))
        c["action"] = normalize_action(action, c["emotion"])
        c["last_active"] = time.time()
        c["updated_at"] = time.time()
        await self.store.emit(
            "say",
            {
                "text": text,
                "emotion": c["emotion"],
                "action": c["action"],
                "effect": effect,
                "speech": speech,
                "dur": dur,
                "source": source,
                "character": c,
            },
        )

    async def _emit_anim(self, action: str, emotion: str = "", effect: str = "none",
                         source: str = "app", dur: int = 1200) -> None:
        c = self.char
        c["action"] = normalize_action(action, c.get("emotion", "calm"))
        if emotion:
            c["emotion"] = normalize_emotion(emotion, c["emotion"])
        c["updated_at"] = time.time()
        await self.store.emit(
            "anim",
            {"action": c["action"], "emotion": c["emotion"], "effect": effect,
             "dur": dur, "source": source},
        )

    async def announce(self, text: str, level: str = "info") -> None:
        await self.store.emit("notice", {"level": level, "text": text})

    # ------------------------------------------------------------ 触摸（虚→实）

    async def handle_touch(self, payload: dict[str, Any], *, notify_qq: bool = True) -> dict[str, Any]:
        ttype = str(payload.get("type") or "tap").lower()
        region = str(payload.get("region") or "unknown").lower()
        try:
            power = float(payload.get("power", 0.5))
        except Exception:
            power = 0.5
        item = str(payload.get("item") or "")[:16]
        try:
            x = float(payload["x"]) if payload.get("x") is not None else None
            y = float(payload["y"]) if payload.get("y") is not None else None
        except Exception:
            x = y = None

        c = self.char
        expire_after = float(self.cfg.get("touch_llm_cooldown", 6.0) or 0)
        now = time.time()
        allow_llm = (now - self._last_touch_llm.get(ttype, 0.0)) >= expire_after

        reaction = R.rule_reaction(ttype, region, power, item, c)
        if allow_llm:
            self._last_touch_llm[ttype] = now
            llm = await R.llm_reaction(
                await self._provider(),
                c,
                R.describe_touch(ttype, region, power, item),
                self._persona(),
                self._history_text(),
                timeout=float(self.cfg.get("llm_timeout", 12.0) or 12.0),
            )
            if llm:
                llm["delta"] = reaction["delta"]
                llm["exp"] = reaction["exp"]
                llm["counter"] = reaction["counter"]
                reaction = llm

        delta = apply_delta(c, reaction.get("delta") or {})
        counter = reaction.get("counter") or "touches"
        counters = c.setdefault("counters", {})
        counters[counter] = int(counters.get(counter, 0)) + 1
        counters["touches"] = int(counters.get("touches", 0)) + 1
        if x is not None or y is not None:
            set_mood_pose(c, x, y)
        # 摸头/抱抱这类亲昵动作给经验
        leveled = add_exp(c, int(reaction.get("exp", 1)))
        c["last_active"] = now

        await self._emit_say(
            reaction["text"],
            reaction["emotion"],
            reaction["action"],
            reaction.get("effect", "none"),
            source="app",
            speech=bool(reaction.get("speech", True)),
            dur=int(reaction.get("dur", 1300)),
        )
        await self.store.emit(
            "touch",
            {"type": ttype, "region": region, "power": power, "item": item,
             "text": reaction["text"], "delta": delta},
        )
        if leveled:
            await self.store.emit(
                "levelup",
                {"level": c.get("level", 1), "text": f"{c.get('name', 'TA')} 升级到 Lv.{c.get('level', 1)} 了！"},
            )
            await self._qq_note(f"🎉 {c.get('name', 'TA')} 升到 Lv.{c.get('level', 1)} 了。", force=True)

        self._remember(f"你在 App 里被「{ttype}」了（{region}），你说：{reaction['text']}")

        qq_sent = False
        if notify_qq and bool(self.cfg.get("touch_to_qq", True)) and self.send_qq and self.store.owner.get("qq"):
            interval = float(self.cfg.get("touch_qq_interval", self.store.settings.get("touch_qq_interval", 30)) or 30)
            force = ttype in ("slap", "kiss", "feed", "hug") or leveled
            if force or (now - self._last_qq_broadcast) >= interval:
                who = self.store.owner.get("nickname") or "TA"
                qq_sent = await self._qq_note(
                    f"📱 {who} 在 App 里{R.describe_touch(ttype, region, power, item)}。\n"
                    f"{c.get('name', '小虚')}：「{reaction['text']}」\n"
                    f"（可以在 QQ 里直接回话，TA 会在 App 里听到你说的话）"
                )
                self._last_qq_broadcast = now
        self.store.touch_dirty()
        return {
            "reaction": {
                "text": reaction["text"],
                "emotion": c["emotion"],
                "action": c["action"],
                "effect": reaction.get("effect", "none"),
                "speech": bool(reaction.get("speech", True)),
                "dur": int(reaction.get("dur", 1300)),
                "source": reaction.get("source", "rule"),
            },
            "delta": delta,
            "character": c,
            "qq_sent": qq_sent,
        }

    # ------------------------------------------------------------ App 说话

    async def handle_chat(self, text: str, *, source: str = "app", mirror: bool = True) -> dict[str, Any]:
        text = (text or "").strip()[:500]
        if not text:
            return {"reply": "", "character": self.char, "qq_sent": False}
        c = self.char
        counters = c.setdefault("counters", {})
        counters["chats"] = int(counters.get("chats", 0)) + 1
        add_exp(c, 4)
        apply_delta(c, {"mood": 1.5, "affection": 0.8, "energy": -0.3})

        reply = ""
        reaction: dict[str, Any] | None = None
        provider = await self._provider()
        if provider is not None:
            # 关键：App 里的对话要与 QQ 完全一致 —— 同一个模型、同一份人格设定。
            # 人格由 AstrBot 的 persona_manager 解析（QQ 用什么，这里就用什么）。
            persona_block = await self.qq_persona_prompt()
            sys_prompt = persona_block or R.build_persona_prompt(c, self._persona(), self._history_text())
            sys_prompt += (
                "\n## 你现在有身体\n"
                f"你现在正以「{c.get('name', '小虚')}」的身份待在一部手机里，TA 可以直接看到你、摸到你。\n"
                f"你当前的心情 {int(float(c.get('stats', {}).get('mood', 50)))}/100，精力 "
                f"{int(float(c.get('stats', {}).get('energy', 50)))}/100，对 TA 的亲密度 "
                f"{int(float(c.get('stats', {}).get('affection', 20)))}/100。\n"
                f"{self._history_text()}\n"
                "这次不是 QQ 打字，而是 TA 就站在你面前跟你说话。"
                "先用一句话回应 TA（可以带括号里的动作描写），"
                "然后再单独给出一行 JSON，用来驱动你的表情和动作：\n"
                '{"emotion":"happy","action":"hug","effect":"hearts","speech":true}\n'
                "emotion 从 happy/calm/shy/sad/angry/sleepy/hungry/excited/love/bored 里选，"
                "action 从 idle/walk/jump/sit/sleep/eat/wave/hug/spin/shake/dance/think/panic 里选，"
                "effect 从 hearts/stars/sparkle/sweat/zzz/anger/none 里选。JSON 之前那句要是自然的话。"
            )
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=text, system_prompt=sys_prompt),
                    timeout=float(self.cfg.get("llm_timeout", 12.0) or 12.0),
                )
                raw = getattr(resp, "completion_text", None) or str(resp)
                obj = R.parse_llm_json(raw)
                if obj and str(obj.get("text") or "").strip():
                    reaction = {
                        "text": str(obj["text"]).strip()[:200],
                        "emotion": normalize_emotion(obj.get("emotion"), heuristic_emotion(text, c)),
                        "action": normalize_action(obj.get("action")),
                        "effect": str(obj.get("effect") or "none"),
                        "speech": bool(obj.get("speech", True)),
                    }
                    reply = reaction["text"]
                else:
                    # 模型没按 JSON 格式回（人格提示词可能让它只用自然语言）：
                    # 把整段文字当台词，情绪/动作按文字猜一个，不能把 reaction 留空。
                    reply = _strip_json_tail(raw)[:200]
                    if reply:
                        _emo = heuristic_emotion(reply, c)
                        reaction = {
                            "text": reply,
                            "emotion": _emo,
                            "action": normalize_action("nod", _emo),
                            "effect": "none",
                            "speech": True,
                        }
            except Exception as e:
                if self.log:
                    self.log.warning(f"[virtual-body] App 对话 LLM 失败：{e}")

        if not reply or not reaction:
            emo = heuristic_emotion(text, c)
            reply = {
                "sad": "……抱一下就好了。",
                "angry": "哼，我知道你只是嘴硬。",
                "sleepy": "唔……我有点困了，你说，我听着。",
                "hungry": "我有点饿了……不过你说吧。",
                "happy": "嗯嗯，我在听！",
                "love": "我在的，一直都在。",
            }.get(emo, "嗯，我听到了。")
            reaction = {"text": reply, "emotion": emo, "action": "nod", "effect": "none", "speech": True}

        await self._emit_say(
            reaction["text"], reaction["emotion"], reaction["action"],
            reaction.get("effect", "none"), source="app", speech=bool(reaction.get("speech", True)),
        )
        self._remember(f"TA 在 App 里对你说：{text}｜你回：{reply}")
        await self.store.emit("chat", {"text": text, "reply": reply, "from": source})

        qq_sent = False
        if mirror and bool(self.cfg.get("mirror_app_to_qq", True)) and self.send_qq and self.store.owner.get("qq"):
            who = self.store.owner.get("nickname") or "TA"
            qq_sent = await self._qq_note(f"📱 {who} 在 App 里对你说：{text}\n{c.get('name', '小虚')}：「{reply}」")
        self.store.touch_dirty()
        return {"reply": reply, "reaction": reaction, "character": c, "qq_sent": qq_sent}

    # ------------------------------------------------------------ App 指令

    async def handle_action(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        action = (action or "").strip().lower()
        c = self.char
        reaction: dict[str, Any] = {"text": "", "emotion": c.get("emotion", "calm"),
                                    "action": c.get("action", "idle"), "effect": "none", "speech": False}

        if action in ("say", "speak"):
            text = str(args.get("text") or "").strip()[:200]
            if text:
                reaction = {"text": text, "emotion": heuristic_emotion(text, c),
                            "action": "nod", "effect": "none", "speech": True}
                await self._emit_say(text, reaction["emotion"], reaction["action"], "none", source="app")
                self._remember(f"TA 让你说：{text}")
        elif action in ("walk", "move", "follow"):
            set_mood_pose(c, args.get("x"), args.get("y"))
            await self._emit_anim("walk", source="app")
        elif action in ("jump", "spin", "dance", "hug", "wave", "sit", "shake", "panic", "think"):
            effect = {"jump": "stars", "hug": "hearts", "dance": "sparkle", "spin": "sparkle"}.get(action, "none")
            await self._emit_anim(action, source="app", effect=effect)
        elif action == "sleep":
            await self._go_sleep()
            reaction = {"text": "那我先睡啦……晚安。", "emotion": "sleepy",
                        "action": "sleep", "effect": "zzz", "speech": True}
            await self._emit_say(reaction["text"], "sleepy", "sleep", "zzz", source="app")
        elif action == "wake":
            c["action"] = "idle"
            apply_delta(c, {"energy": 6, "mood": 2})
            await self._emit_say("唔……我醒了。", "sleepy", "idle", "none", source="app")
        elif action in ("eat", "feed"):
            item = str(args.get("item") or args.get("text") or "好吃的")[:16]
            res = await self.handle_touch({"type": "feed", "item": item})
            return res
        elif action == "dress":
            outfit = str(args.get("outfit") or "default")[:16]
            c["outfit"] = outfit
            await self._emit_say(f"换好了，{outfit} 这套怎么样？", "happy", "spin", "sparkle", source="app")
        elif action == "rename":
            name = str(args.get("name") or "").strip()[:12]
            if name:
                c["name"] = name
                await self._emit_say(f"好，那我叫{name}了。", "happy", "nod", "sparkle", source="app")
        elif action == "room":
            room = str(args.get("room") or "day")[:12]
            c["room"] = room
            await self.store.emit("state", {"character": c, "patch": {"room": room}})
        elif action in ("look", "appearance", "set_face"):
            # 切换外观来源：canvas（内置）｜image（上传立绘）｜model（glb 3D 模型）
            renderer = str(args.get("renderer") or "").strip().lower()
            url = str(args.get("url") or "").strip()[:512]
            if renderer not in ("canvas", "image", "model"):
                return {"ok": False, "error": "bad_renderer",
                        "message": "renderer 只能是 canvas / image / model"}
            if renderer != "canvas" and not url:
                return {"ok": False, "error": "no_url", "message": "切换外观需要提供 url"}
            c["renderer"] = renderer
            c["image_url"] = url if renderer == "image" else ""
            c["model_url"] = url if renderer == "model" else ""
            c["updated_at"] = time.time()
            label = {"canvas": "回到内置形象", "image": "换上你给的立绘", "model": "换上你给的 3D 模型"}[renderer]
            await self.store.emit("state", {"character": c,
                                            "patch": {"renderer": renderer, "url": url}})
            await self._emit_say(f"{label}，好看吗？", "happy", "spin", "sparkle", source="app")
        elif action == "mute":
            on = bool(args.get("on", True))
            self.store.settings["muted"] = on
            c["muted"] = on
            await self.store.emit("state", {"character": c, "patch": {"muted": on}})
        elif action == "play":
            apply_delta(c, {"mood": 6, "energy": -6, "satiety": -2, "affection": 1.5})
            add_exp(c, 6)
            await self._emit_say("耶——陪我玩！", "excited", "jump", "stars", source="app")
            self._remember("TA 陪你玩了一会儿，你很开心")
        else:
            return {"ok": False, "error": "unknown_action", "message": f"不认识的动作：{action}"}

        self.store.touch_dirty()
        return {"reaction": reaction, "character": c, "ok": True}

    async def _go_sleep(self) -> None:
        c = self.char
        c["action"] = "sleep"
        c["emotion"] = "sleepy"
        c["updated_at"] = time.time()
        await self.store.emit("anim", {"action": "sleep", "emotion": "sleepy",
                                       "effect": "zzz", "dur": 2000, "source": "app"})

    # ------------------------------------------------------------ QQ（实→虚）

    async def handle_qq_message(self, qq: str, nickname: str, text: str) -> None:
        """房主在 QQ 里说话了：镜像到 App，让角色"听见"。"""
        text = (text or "").strip()[:300]
        if not text:
            return
        c = self.char
        c["counters"]["chats"] = int(c.get("counters", {}).get("chats", 0)) + 1
        c["last_active"] = time.time()
        await self.store.emit(
            "qq",
            {"from": "owner", "nickname": nickname or "TA", "text": text,
             "say": "", "character": c},
        )
        self._remember(f"TA 在 QQ 里说：{text}")
        self.store.touch_dirty()

    async def handle_qq_reply(self, reply: str, *, emotion: str = "") -> dict[str, Any]:
        """LLM 在 QQ 里回复完了：把同一句话让角色在 App 里说出口。"""
        c = self.char
        reply = (reply or "").strip()
        if not reply:
            return {}
        emo = emotion or heuristic_emotion(reply, c)
        await self._emit_say(reply, emo, "nod" if emo not in ("excited", "angry") else "jump",
                             "sparkle" if emo in ("happy", "love", "excited") else "none",
                             source="qq", speech=not bool(self.store.settings.get("muted")))
        await self.store.emit("qq", {"from": "bot", "nickname": c.get("name", "小虚"), "text": reply,
                                     "say": reply, "character": c})
        self.store.touch_dirty()
        return {"say": reply, "emotion": emo}

    # ------------------------------------------------------------ 内部工具

    async def _qq_note(self, text: str, force: bool = False) -> bool:
        if not self.send_qq:
            return False
        if not self.store.owner.get("qq"):
            return False
        if bool(self.store.settings.get("muted")) and not force:
            pass  # 静音只影响 App 语音，不影响 QQ 播报
        try:
            return bool(await self.send_qq(text))
        except Exception as e:
            if self.log:
                self.log.warning(f"[virtual-body] 播报到 QQ 失败：{e}")
            return False

    def status_text(self) -> str:
        return status_card(self.char, self.store.owner.get("qq", ""))
