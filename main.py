"""虚现互通 · AstrBot 插件主入口。

把 AstrBot 变成一个**有身体的虚拟生命**：
- 插件在容器里内嵌一个 HTTP + WebSocket 服务（bridge.py），
  手机 App / 网页从这里读到角色状态、把触觉事件送进来。
- 虚拟 ↔ 现实 两条腿都在 engine.py：
  * App 里摸头 → 角色有反应 → 同时把这件事播报到 QQ；
  * QQ 里说话 → 角色在 App 里听见并开口说出同一句回复。
- 本文件负责：QQ 指令面、房主识别、把 AstrBot 的 LLM 回复喂回 App。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .bridge import Bridge
from .engine import Engine
from .store import Store

PLUGIN_NAME = "astrbot_plugin_virtual_body"
PREFIXES = ("虚拟身体", "虚现", "vb", "VB")
DEFAULT_PORT = 8899

HELP = """🐾 虚现互通 · 指令表

【绑定 App】
虚现 绑定码           生成 6 位绑定码（10 分钟有效），在 App 里输入即可

【看它 / 摸它】
虚现 状态             状态卡（等级/心情/亲密/饱食/精力）
虚现 摸头 / 抱抱 / 戳戳 / 挠痒 / 亲亲 / 拍拍 / 抛
虚现 喂 <食物>        投喂（例：虚现 喂 草莓）
虚现 说 <内容>        让它在 App 里说出这句话
虚现 做 <动作>        让它做动作：jump/wave/spin/dance/hug/panic/think
虚现 表情 <情绪>      happy/calm/shy/sad/angry/sleepy/hungry/excited/love/bored

【安排它】
虚现 睡觉 / 起床
虚现 玩 / 散步 / 跳舞 / 打滚
虚现 来               把它叫到屏幕中间
虚现 名字 <新名字>    改名
虚现 换装 <装扮>      default/pajamas/sport/raincoat/hamster
虚现 场景 <场景>      day/night/rain/party/beach

【设置】
虚现 静音 / 开声       App 语音开关
虚现 在线              看当前连着的设备
虚现 重启服务           重启内嵌服务（端口占用时用）
虚现 解绑              作废所有 App 绑定
虚现 重置 确认         角色回到出厂状态

💡 直接跟我聊天就行 —— 你说的话会让它在 App 里开口回答。"""

# 触摸指令 → (类型, 部位)
TOUCH_CMD: dict[str, tuple[str, str]] = {
    "摸头": ("pet", "head"), "摸摸": ("pet", "head"), "拍头": ("pet", "head"), "摸摸头": ("pet", "head"),
    "抱抱": ("hug", "body"), "抱": ("hug", "body"), "抱一下": ("hug", "body"),
    "戳戳": ("poke", "body"), "戳": ("poke", "body"), "戳脸": ("poke", "face"),
    "挠痒": ("tickle", "body"), "挠": ("tickle", "body"), "挠痒痒": ("tickle", "body"),
    "亲亲": ("kiss", "face"), "亲": ("kiss", "face"),
    "拍拍": ("slap", "body"), "打": ("slap", "body"), "打屁股": ("slap", "body"),
    "抛": ("toss", "body"), "举高高": ("toss", "body"),
    "搓": ("rub", "head"), "揉": ("rub", "head"),
    "牵手": ("hug", "hand"), "捏": ("rub", "face"),
}

ACTION_CMD: dict[str, str] = {
    "跳": "jump", "跳一下": "jump", "挥手": "wave", "转圈": "spin", "跳舞": "dance",
    "坐下": "sit", "坐": "sit", "打滚": "spin", "思考": "think", "慌张": "panic",
}

EMOTIONS = ("happy", "calm", "shy", "sad", "angry", "sleepy", "hungry", "excited", "love", "bored")
OUTFITS = ("default", "pajamas", "sport", "raincoat", "hamster")
ROOMS = ("day", "night", "rain", "party", "beach")


class VirtualBodyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg: dict[str, Any] = dict(config or {})
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir)
        self.store.load()
        self.engine = Engine(
            self.store,
            context=context,
            config=self.cfg,
            logger=logger,
            send_qq=self._send_qq,
        )
        self.bridge: Bridge | None = None
        self._server_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        self.engine.start_background()
        await self._start_bridge()

    async def terminate(self) -> None:
        try:
            if self.bridge:
                await self.bridge.stop()
        except Exception as e:
            logger.warning(f"[virtual-body] 停止服务失败：{e}")
        try:
            await self.store.save()
        except Exception:
            pass

    async def _start_bridge(self) -> bool:
        if self.bridge is not None:
            return True
        port = int(self.cfg.get("port", DEFAULT_PORT) or DEFAULT_PORT)
        host = str(self.cfg.get("host", "0.0.0.0") or "0.0.0.0")
        self.bridge = Bridge(
            self.engine,
            self.store,
            host=host,
            port=port,
            webapp_dir=Path(__file__).parent / "webapp",
            data_dir=self.data_dir,
            token_required=bool(self.cfg.get("token_required", True)),
            logger=logger,
        )
        try:
            await self.bridge.start()
            logger.info(f"[virtual-body] 虚现互通已就绪：{host}:{port}（App 基址见配置 public_base）")
            return True
        except Exception as e:
            logger.error(f"[virtual-body] 服务启动失败（端口 {port} 可能被占用）：{e}")
            self.bridge = None
            return False

    async def _restart_bridge(self) -> bool:
        if self.bridge:
            try:
                await self.bridge.stop()
            except Exception:
                pass
            self.bridge = None
        return await self._start_bridge()

    # ------------------------------------------------------------ 发 QQ

    async def _send_qq(self, text: str) -> bool:
        umo = str(self.store.owner.get("umo") or "")
        if not umo:
            return False
        try:
            ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
            return bool(ok)
        except Exception as e:
            logger.warning(f"[virtual-body] 主动发 QQ 失败（umo={umo}）：{e}")
            return False

    # ------------------------------------------------------------ 指令工具

    @staticmethod
    def _strip_prefix(text: str) -> str:
        t = (text or "").strip().lstrip("/#！!")
        for p in PREFIXES:
            if t.startswith(p):
                return t[len(p):].strip()
        return t

    @staticmethod
    def _is_command(text: str) -> bool:
        t = (text or "").strip().lstrip("/#！!")
        return any(t.startswith(p) for p in PREFIXES)

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        owner = str(self.store.owner.get("qq") or "")
        qq = str(event.get_sender_id() or "")
        if not owner:
            return True
        if qq == owner:
            return True
        wl = [str(x) for x in (self.cfg.get("whitelist") or [])]
        if qq in wl:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _capture_owner(self, event: AstrMessageEvent) -> None:
        """记录房主的 QQ / 会话 / 昵称；私聊会话优先（播报默认走私聊）。"""
        qq = str(event.get_sender_id() or "")
        if not qq:
            return
        owner = self.store.owner
        is_private = False
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            pass
        umo = event.unified_msg_origin
        if not owner.get("umo"):
            owner["umo"] = umo
        elif is_private:
            owner["umo"] = umo
        owner["qq"] = owner.get("qq") or qq
        if not owner.get("nickname"):
            try:
                owner["nickname"] = event.get_sender_name() or ""
            except Exception:
                pass
        self.store.touch_dirty()

    # ------------------------------------------------------------ 指令入口

    @filter.command("虚现", alias={"虚拟身体", "vb"})
    async def vb_cmd(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """虚现 <子命令> —— 虚现互通总入口（发送「虚现 帮助」看全部指令）。"""
        try:
            text = (event.message_str or "").strip()
            body = self._strip_prefix(text)
            parts = body.split(maxsplit=1)
            sub = parts[0] if parts and parts[0] else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
            reply = await self._dispatch(event, sub, rest)
        except Exception as e:
            logger.exception(f"[virtual-body] 指令处理异常：{e}")
            reply = f"出错了：{e}"
        if reply:
            yield event.plain_result(reply)
        event.should_call_llm(False)
        event.stop_event()

    async def _dispatch(self, event: AstrMessageEvent, sub: str, rest: str) -> str:
        st = self.store
        en = self.engine
        qq = str(event.get_sender_id() or "")

        if sub in ("", "帮助", "help", "?", "？"):
            return HELP

        # ---- 绑定码：谁先绑定谁就是房主 ----
        if sub in ("绑定码", "bind", "绑定"):
            if st.owner.get("qq") and qq != st.owner["qq"] and not self._is_owner(event):
                return "这个角色已经有主人了。"
            if not st.owner.get("qq"):
                st.set_owner(qq, "", "")
                self._capture_owner(event)
            code = st.make_code(qq, ttl=int(self.cfg.get("code_ttl", 600) or 600))
            base = str(self.cfg.get("public_base", "") or "")
            lines = [
                f"🔑 App 绑定码：{code}",
                "10 分钟内有效，一次一码。",
                f"在 App 的绑定页填这 6 位数字即可。" + (f"\n地址：{base}" if base else ""),
            ]
            if self.bridge is None:
                lines.append("⚠️ 内嵌服务当前没起来，先发「虚现 重启服务」。")
            return "\n".join(lines)

        # ---- 只读信息 ----
        if sub in ("状态", "status"):
            card = en.status_text()
            n = len(self.bridge._clients) if self.bridge else 0
            if not self._is_owner(event):
                card = f"（你）\n{card}" if False else card
            return card + (f"\n\n📱 当前 App 在线：{n} 台" if n else "\n\n📱 当前没有 App 在线")

        if sub in ("在线", "设备", "apps"):
            if not self._is_owner(event):
                return "只有房主能看设备列表。"
            tokens = st.data.get("tokens", {})
            if not tokens:
                return "还没有 App 绑定过。发送「虚现 绑定码」去绑定。"
            lines = [f"📱 已绑定设备 {len(tokens)} 台："]
            for i, (tk, rec) in enumerate(tokens.items(), 1):
                lines.append(
                    f"{i}. {rec.get('device') or '未知设备'}（{rec.get('platform') or '?'}）"
                    f"　最后活跃：{self._ago(rec.get('last_seen'))}"
                )
            return "\n".join(lines)

        # ---- 以下都是有身体的操作，需要权限 ----
        if not self._is_owner(event):
            return "只有房主（或其白名单）能让它做事哦。"

        # 触摸类
        if sub in TOUCH_CMD:
            ttype, region = TOUCH_CMD[sub]
            data = await en.handle_touch({"type": ttype, "region": region, "power": 0.5},
                                         notify_qq=False)
            r = data["reaction"]
            return f"（你{('' if sub == '摸头' else sub)}）{r['text']}"

        if sub in ("喂", "投喂", "吃"):
            item = rest or "好吃的"
            data = await en.handle_touch({"type": "feed", "item": item, "region": "face"},
                                         notify_qq=False)
            return data["reaction"]["text"]

        # 说话 / 动作
        if sub in ("说", "说话"):
            if not rest:
                return "要说什么？例：虚现 说 我回来啦"
            await en.handle_action("say", {"text": rest})
            return f"（{en.char.get('name', '小虚')}在 App 里说了）{rest}"

        if sub in ("做", "动作"):
            act = ACTION_CMD.get(rest, rest)
            if act not in ("jump", "wave", "spin", "dance", "walk", "sit", "sleep", "hug", "panic", "think", "shake"):
                return "动作可选：跳 / 挥手 / 转圈 / 跳舞 / 坐下 / 打滚 / 思考 / 慌张"
            await en.handle_action(act)
            return f"（它{rest or act}了）"

        if sub in ("表情", "情绪"):
            emo = rest.strip().lower()
            if emo not in EMOTIONS:
                return "情绪可选：" + " / ".join(EMOTIONS)
            en.char["emotion"] = emo
            await st.emit("state", {"character": en.char, "patch": {"emotion": emo}})
            return f"（表情切到 {emo}）"

        if sub in ("来", "过来", "过来吧", "来我身边"):
            await en.handle_action("follow", {"x": 0.5, "y": 0.62})
            emo = en.char.get("emotion", "happy")
            en.char["emotion"] = "happy" if emo in ("sad", "angry", "bored") else emo
            await st.emit("say", {"text": "我来了～", "emotion": en.char["emotion"],
                                  "action": "walk", "effect": "sparkle", "source": "qq"})
            return "它朝屏幕中间走过来了。"

        if sub in ("睡觉", "睡", "晚安"):
            await en.handle_action("sleep")
            return "它蜷起来睡了……（精力会慢慢恢复）"

        if sub in ("起床", "醒来", "早上好"):
            await en.handle_action("wake")
            return "它揉着眼睛醒了。"

        if sub in ("玩", "陪我玩"):
            await en.handle_action("play")
            return "它开心地蹦了起来。"

        if sub in ("散步", "走走"):
            en.char["pose"]["x"] = 0.2
            en.char["pose"]["facing"] = 1
            await en.handle_action("walk")
            from .character import apply_delta

            apply_delta(en.char, {"mood": 3, "energy": -3, "satiety": -1})
            return "它去散步了，心情好了一点。"

        if sub in ("跳舞", "打滚"):
            act = "dance" if sub == "跳舞" else "spin"
            await en.handle_action(act)
            return "它转了两圈。"

        # 外观
        if sub in ("名字", "改名"):
            name = rest.strip()[:12]
            if not name:
                return f"现在叫「{en.char.get('name', '小虚')}」。用法：虚现 名字 阿豆"
            await en.handle_action("rename", {"name": name})
            return f"好，它以后叫「{name}」了。"

        if sub == "换装":
            outfit = rest.strip().lower()
            if outfit not in OUTFITS:
                return "装扮可选：" + " / ".join(OUTFITS)
            await en.handle_action("dress", {"outfit": outfit})
            return f"换上了 {outfit}。"

        if sub == "场景":
            room = rest.strip().lower()
            if room not in ROOMS:
                return "场景可选：" + " / ".join(ROOMS)
            await en.handle_action("room", {"room": room})
            return f"场景切到 {room}。"

        # 设置
        if sub == "静音":
            st.settings["muted"] = True
            await st.emit("state", {"character": en.char, "patch": {"muted": True}})
            return "已静音（App 里它只打字不出声）。"
        if sub in ("开声", "解除静音"):
            st.settings["muted"] = False
            await st.emit("state", {"character": en.char, "patch": {"muted": False}})
            return "已开声。"

        if sub in ("解绑", "踢掉"):
            n = st.revoke_tokens()
            await st.emit("notice", {"level": "warn", "text": "已被房主解绑"})
            return f"已作废 {n} 台设备的绑定。"

        if sub == "重启服务":
            ok = await self._restart_bridge()
            return "服务已重启 ✅" if ok else "服务启动失败，看日志里 [virtual-body] 的错误。"

        if sub in ("重置", "初始化"):
            if rest.strip() not in ("确认", "confirm", "yes"):
                return "这会清空等级、亲密度、装扮和所有绑定，发「虚现 重置 确认」才会执行。"
            from .store import default_state

            fresh = default_state()
            keep = {"umo": st.owner.get("umo"), "qq": st.owner.get("qq"),
                    "nickname": st.owner.get("nickname")}
            fresh["owner"].update({k: v for k, v in keep.items() if v})
            st.data = fresh
            await st.save()
            await st.emit("notice", {"level": "warn", "text": "角色已重置"})
            return "重置完成，它变回刚出生的样子了。"

        return f"没听懂「{sub}」。发「虚现 帮助」看全部指令。"

    @staticmethod
    def _ago(ts: Any) -> str:
        import time

        try:
            d = max(0.0, time.time() - float(ts or 0))
        except Exception:
            return "未知"
        if d < 60:
            return "刚刚"
        if d < 3600:
            return f"{int(d // 60)} 分钟前"
        if d < 86400:
            return f"{int(d // 3600)} 小时前"
        return f"{int(d // 86400)} 天前"

    # ------------------------------------------------------------ 普通消息：实→虚

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """房主的普通对话都镜像到 App，让角色"听见"，并让它在 App 里开口。

        注意：这里**不回复 QQ**，QQ 侧的回复交给 AstrBot 原本的对话链路，
        我们只是在 LLM 出结果后（on_llm_response）把同一句话送到 App。
        """
        try:
            if event.is_stopped():
                return
            text = (event.message_str or "").strip()
            if self._is_command(text):
                return
            qq = str(event.get_sender_id() or "")
            owner = str(self.store.owner.get("qq") or "")
            if not owner or qq != owner or not text:
                return
            self._capture_owner(event)
            name = ""
            try:
                name = event.get_sender_name() or ""
            except Exception:
                pass
            await self.engine.handle_qq_message(qq, name, text)
            # 私聊里房主随便说话 → 让 AstrBot 正常回话（回复内容会被同步到 App）
            if bool(self.cfg.get("force_reply_private", True)):
                try:
                    if event.is_private_chat() and not event.is_wake_up():
                        event.should_call_llm(True)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[virtual-body] 消息镜像失败：{e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp) -> None:
        """LLM 在 QQ 里说了什么，就让角色在 App 里说出同一句话。"""
        try:
            qq = str(event.get_sender_id() or "")
            if qq != str(self.store.owner.get("qq") or ""):
                return
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return
            await self.engine.handle_qq_reply(text)
        except Exception as e:
            logger.debug(f"[virtual-body] 同步回复到 App 失败：{e}")
