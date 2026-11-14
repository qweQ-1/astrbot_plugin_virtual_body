"""虚现互通 · 离线自检（不依赖 AstrBot，直接验证引擎/状态机/协议数据结构）。

跑法：python3 /var/minis/shared/virtual-body/tests/selftest.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 插件源码就在仓库根目录，而模块之间用相对导入（它们是包的一部分），
# 所以这里造一个临时目录放一个同名软链接，把整个仓库当成包来 import。
_SHIM = tempfile.mkdtemp(prefix="vb-pkg-")
_LINK = os.path.join(_SHIM, "astrbot_plugin_virtual_body")
if not os.path.exists(_LINK):
    os.symlink(ROOT, _LINK)
sys.path.insert(0, _SHIM)

from astrbot_plugin_virtual_body import reactions as R          # noqa: E402
from astrbot_plugin_virtual_body.character import (            # noqa: E402
    add_exp, apply_delta, decay, heuristic_emotion, status_card,
)
from astrbot_plugin_virtual_body.engine import Engine          # noqa: E402
from astrbot_plugin_virtual_body.store import Store, default_state  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra and not cond else ""))


async def main():
    tmp = tempfile.mkdtemp(prefix="vbtest-")
    try:
        print("\n[1] Store：绑定码 / token / 状态持久化")
        st = Store(tmp)
        st.load()
        check("初始状态可加载", st.character["name"] == "小虚")
        code = st.make_code("10001")
        check("绑定码是 6 位数字", len(code) == 6 and code.isdigit(), code)
        ok, reason, qq = st.verify_code(code)
        check("绑定码可校验", ok and qq == "10001", f"{ok} {reason} {qq}")
        ok2, reason2, _ = st.verify_code(code)
        check("绑定码一次性（复用失败）", (not ok2) and reason2 == "bad_code", reason2)
        code2 = st.make_code("10001", ttl=-1)
        ok3, reason3, _ = st.verify_code(code2)
        check("过期绑定码被拒", (not ok3) and reason3 == "expired", reason3)
        tok = st.add_token("10001", "iPhone", "ios")
        check("token 可校验", bool(st.check_token(tok)))
        st.set_owner("10001", "aiocqhttp:private:10001", "阿蓝")
        await st.save()
        st2 = Store(tmp)
        st2.load()
        check("落盘后可恢复 token", bool(st2.check_token(tok)))
        check("落盘后可恢复房主", st2.owner.get("qq") == "10001")
        check("counters/stats 补全", "clean" in st2.character["stats"] and "chats" in st2.character["counters"])
        check("解绑作废 token", st2.revoke_tokens() == 1 and not st2.check_token(tok))

        print("\n[2] Character：数值 clamp / 升级 / 衰减")
        c = default_state()["character"]
        d = apply_delta(c, {"mood": 500, "affection": -999})
        check("上限 clamp 100", c["stats"]["mood"] == 100, str(c["stats"]["mood"]))
        check("下限 clamp 0", c["stats"]["affection"] == 0, str(c["stats"]["affection"]))
        check("delta 返回真实变化", d.get("affection") == -20, str(d))
        c["exp"] = 0
        lv = add_exp(c, 400)
        check("经验足够会升级", lv and c["level"] > 1, f"Lv.{c['level']}")
        c["stats"]["satiety"] = 30
        c["last_active"] = time.time() - 3600 * 24
        before = c["stats"]["satiety"]
        decay(c)
        check("离线一天饱食下降", c["stats"]["satiety"] < before, f"{before} → {c['stats']['satiety']}")
        check("饿到低值会变 hungry", c["stats"]["satiety"] < 25 and c["emotion"] == "hungry", c["emotion"])
        card = status_card(c, "10001")
        check("状态卡含关键字段", "亲密度" in card and "Lv." in card)

        print("\n[3] Reactions：规则反应覆盖所有触摸类型")
        for t in ("pet", "poke", "tap", "hug", "tickle", "drag", "rub", "kiss", "slap", "toss", "feed"):
            r = R.rule_reaction(t, "head", 0.5, "草莓", c)
            check(f"{t} 有台词/情绪/动作",
                  bool(r["text"]) and r["emotion"] and r["action"] and isinstance(r["delta"], dict))
        r_slap = R.rule_reaction("slap", "body", 0.5, "", c)
        check("拍打会掉亲密/心情", r_slap["delta"].get("affection", 0) < 0 and r_slap["delta"].get("mood", 0) < 0)
        r_feed = R.rule_reaction("feed", "face", 0.5, "小鱼干", c)
        check("投喂涨饱食", r_feed["delta"].get("satiety", 0) > 0)
        check("描述文字可读", "摸了" in R.describe_touch("pet", "head", 0.5))
        check("情绪启发式", heuristic_emotion("哈哈哈哈", c) == "happy",
              heuristic_emotion("哈哈哈哈", c))
        check("LLM JSON 解析", (R.parse_llm_json('废话```{"text":"嗨","emotion":"happy"}```') or {}).get("text") == "嗨")
        check("坏 JSON 返回 None", R.parse_llm_json("没有 json") is None)

        print("\n[4] Engine：触摸 → 反应 → 事件 → QQ 播报")
        st3 = Store(tempfile.mkdtemp(prefix="vbtest2-"))
        st3.load()
        st3.set_owner("10001", "aiocqhttp:private:10001", "阿蓝")
        st3.add_token("10001", "iPhone", "ios")
        sent = []

        async def fake_send(text):
            sent.append(text)
            return True

        en = Engine(st3, context=None, config={"touch_to_qq": True, "touch_qq_interval": 0,
                                              "mirror_app_to_qq": True}, logger=None, send_qq=fake_send)
        out = await en.handle_touch({"type": "pet", "region": "head", "x": 0.4, "y": 0.5, "power": 0.6})
        check("触摸返回 reaction/character/delta",
              all(k in out for k in ("reaction", "character", "delta")))
        check("触摸后亲密度上升", st3.character["stats"]["affection"] > 20)
        check("触摸计数 +1", st3.character["counters"]["pets"] == 1)
        check("姿态被更新", abs(st3.character["pose"]["x"] - 0.4) < 0.01)
        check("产生了 say 事件", any(e["kind"] == "say" for e in st3.data["events"]))
        check("触摸播报到了 QQ", any("📱" in s for s in sent), str(sent[:1]))
        check("气泡文字已写入角色", bool(st3.character["bubble"]))

        sent.clear()
        ev_before = len(st3.data["events"])
        out2 = await en.handle_touch({"type": "pet", "region": "head"}, notify_qq=True)
        check("QQ 来源的触摸不重复播报", not any("📱" in s for s in sent), str(sent))

        print("\n[5] Engine：App 说话 / QQ 说话 双向互通")
        sent.clear()
        out3 = await en.handle_chat("今天有点累")
        check("App 说话有回复", bool(out3["reply"]), out3["reply"])
        check("App 说话镜像到 QQ", any("在 App 里对你说" in s for s in sent), str(sent[:1]))
        check("chat 计数 +1", st3.character["counters"]["chats"] >= 1)
        sent.clear()
        await en.handle_qq_message("10001", "阿蓝", "在吗")
        check("QQ 消息产生 qq 事件", any(e["kind"] == "qq" for e in st3.data["events"]))
        await en.handle_qq_reply("在的呀，刚在打盹。")
        check("QQ 回复会在 App 里说出口",
              any(e["kind"] == "say" and "打盹" in json.dumps(e, ensure_ascii=False) for e in st3.data["events"]))

        print("\n[6] Engine：动作面")
        for act, args in (("say", {"text": "你好"}), ("jump", {}), ("sleep", {}), ("wake", {}),
                          ("dance", {}), ("dress", {"outfit": "pajamas"}), ("rename", {"name": "阿豆"}),
                          ("room", {"room": "night"}), ("play", {})):
            res = await en.handle_action(act, args)
            check(f"动作 {act} 正常", isinstance(res, dict) and res.get("ok") is not False, str(res)[:80])
        check("改名生效", st3.character["name"] == "阿豆", st3.character["name"])
        check("换装生效", st3.character["outfit"] == "pajamas")
        check("场景生效", st3.character["room"] == "night")
        bad = await en.handle_action("bogus")
        check("未知动作被拒", bad.get("ok") is False)

        print("\n[7] 协议字段完整性（前端依赖）")
        snap = en.snapshot()
        need = {"id", "name", "level", "exp", "exp_next", "stats", "emotion", "action",
                "pose", "outfit", "room", "bubble", "counters"}
        check("snapshot 字段齐全", need.issubset(set(snap.keys())), str(need - set(snap.keys())))
        check("stats 六项齐全",
              set(snap["stats"]) >= {"affection", "mood", "energy", "satiety", "clean", "trust"})
        ev = st3.data["events"][-1]
        check("事件有 id/at/kind/payload", {"id", "at", "kind", "payload"} <= set(ev.keys()))

        print("\n[8] 事件环形缓冲")
        st4 = Store(tempfile.mkdtemp(prefix="vbtest3-"))
        st4.load()
        for i in range(260):
            st4.push_event("say", {"text": str(i)})
        check("事件上限 200", len(st4.data["events"]) == 200, str(len(st4.data["events"])))
        check("events_since 正确", len(st4.events_since(st4.data["events"][0]["id"])) == 199)
        check("last_event_id 正确", st4.last_event_id() == 260)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== 通过 {len(PASS)} / 失败 {len(FAIL)} ====")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
