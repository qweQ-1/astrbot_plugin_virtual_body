"""虚现互通 · 反应引擎。

两级策略：
1. **规则台词库**（永远可用）：每种触摸/动作都有情绪、动作、特效、数值变化 + 若干台词。
2. **LLM 生成台词**（可用时优先）：把角色状态 + 刚才发生了什么交给大模型，
   要求它回一个 JSON，拿到更像"活的"反应；解析失败就退回规则台词。

所以：LLM 挂了、超时了、provider 没配 —— 角色依然有反应，只是话变少。
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

from .character import heuristic_emotion, normalize_action, normalize_emotion

# ---------------------------------------------------------------- 规则台词库

RULES: dict[str, dict[str, Any]] = {
    "pet": {
        "emotion": "happy", "action": "idle", "effect": "hearts",
        "delta": {"affection": 1.2, "mood": 1.5, "energy": -0.2},
        "exp": 3, "counter": "pets",
        "lines": ["唔……好舒服。", "再摸摸嘛。", "嗯～头这里最喜欢了。", "嘿嘿，被你摸到了。"],
    },
    "poke": {
        "emotion": "excited", "action": "shake", "effect": "sparkle",
        "delta": {"affection": 0.6, "mood": 1.0, "energy": -0.5},
        "exp": 2, "counter": "pokes",
        "lines": ["干嘛呀！", "戳我干嘛～", "诶？有事吗？", "别戳啦，痒。"],
    },
    "tap": {
        "emotion": "calm", "action": "nod", "effect": "none",
        "delta": {"affection": 0.3, "mood": 0.4},
        "exp": 1, "counter": "touches",
        "lines": ["嗯？", "我在的。", "怎么了？", "……（看你一眼）"],
    },
    "hug": {
        "emotion": "love", "action": "hug", "effect": "hearts",
        "delta": {"affection": 2.0, "mood": 2.5, "energy": 0.5, "trust": 0.6},
        "exp": 5, "counter": "touches",
        "lines": ["抱紧一点。", "嗯……这样很好。", "不许突然松开哦。", "你身上暖暖的。"],
    },
    "tickle": {
        "emotion": "shy", "action": "jump", "effect": "sweat",
        "delta": {"affection": 1.0, "mood": 2.0, "energy": -1.5},
        "exp": 3, "counter": "touches",
        "lines": ["哈哈哈别挠！", "不行不行，我认输！", "住手啦，笑死我了——", "呀！坏蛋！"],
    },
    "drag": {
        "emotion": "excited", "action": "panic", "effect": "sweat",
        "delta": {"affection": 0.8, "mood": 0.5, "energy": -1.0},
        "exp": 2, "counter": "touches",
        "lines": ["哇——放我下来！", "要摔啦要摔啦！", "你想带我去哪？", "我、我自己会走……"],
    },
    "rub": {
        "emotion": "shy", "action": "shake", "effect": "sparkle",
        "delta": {"affection": 1.5, "mood": 1.5},
        "exp": 3, "counter": "touches",
        "lines": ["脸要红了……", "别一直搓啦。", "嗯…再往左边一点。", "你在给我搓圆吗？"],
    },
    "kiss": {
        "emotion": "love", "action": "hug", "effect": "hearts",
        "delta": {"affection": 2.5, "mood": 3.0, "trust": 1.0},
        "exp": 6, "counter": "touches",
        "lines": ["……（捂脸）", "突然之间干嘛呀！", "唔，这个是奖励吗？", "心跳好快。"],
    },
    "slap": {
        "emotion": "angry", "action": "shakehead", "effect": "anger",
        "delta": {"affection": -2.5, "mood": -5.0, "trust": -1.0},
        "exp": 0, "counter": "touches",
        "lines": ["痛！你干嘛！", "哼，不理你了。", "……过分。", "（生气地转过身）"],
    },
    "toss": {
        "emotion": "excited", "action": "jump", "effect": "stars",
        "delta": {"affection": 1.0, "mood": 2.5, "energy": -2.0},
        "exp": 4, "counter": "touches",
        "lines": ["再高一点！再高一点！", "飞起来啦——", "接住我哦！", "啊啊啊好爽！"],
    },
    "feed": {
        "emotion": "happy", "action": "eat", "effect": "sparkle",
        "delta": {"satiety": 18.0, "mood": 4.0, "affection": 1.5, "clean": -3.0},
        "exp": 5, "counter": "feeds",
        "lines": ["唔唔，好吃！", "还有吗？", "这个我最喜欢了。", "谢谢你呀。"],
    },
}

REGION_HINT = {
    "head": "有人摸了摸你的头顶",
    "face": "有人戳了戳你的脸颊",
    "hand": "有人牵住了你的手",
    "body": "有人戳了戳你的腰",
    "foot": "有人碰了碰你的脚",
    "unknown": "有人碰了碰你",
}

# 特殊区域加成
REGION_BONUS = {
    "head": {"affection": 0.5, "mood": 0.5},
    "face": {"affection": 0.3},
    "hand": {"trust": 0.4, "affection": 0.3},
    "foot": {"mood": 0.5, "energy": -0.3},
}


def rule_reaction(
    ttype: str,
    region: str = "unknown",
    power: float = 0.5,
    item: str = "",
    char: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """纯规则反应，任何情况下都能返回。"""
    ttype = (ttype or "tap").lower()
    base = RULES.get(ttype) or RULES["tap"]
    region = (region or "unknown").lower()
    power = max(0.0, min(1.0, float(power or 0.5)))

    delta = dict(base.get("delta") or {})
    # 力度影响：轻=亲昵，重=刺激/掉精力
    if ttype in ("pet", "rub", "hug"):
        delta["mood"] = delta.get("mood", 1.0) * (1.35 - 0.5 * power)
    if ttype in ("poke", "slap", "toss", "tickle"):
        delta["energy"] = delta.get("energy", -0.5) - power * 1.2
    for k, v in (REGION_BONUS.get(region) or {}).items():
        delta[k] = delta.get(k, 0.0) + v

    emotion = base["emotion"]
    action = base["action"]
    effect = base["effect"]
    # 状态影响台词与情绪：太饱了喂不进去、太困了被戳也懒
    if char:
        stats = char.get("stats", {})
        if ttype == "feed" and float(stats.get("satiety", 0)) > 92:
            emotion, action, effect = "bored", "shakehead", "sweat"
            delta = {"satiety": 2.0, "mood": -1.0}
            return _pack(char, "我吃不下了……真的。", emotion, action, effect, delta, base.get("exp", 1),
                        ttype, item)
        if float(stats.get("energy", 100)) < 18 and ttype not in ("feed", "hug", "pet"):
            emotion, action = "sleepy", "sleep"
            delta["energy"] = delta.get("energy", 0) + 2.0

    lines = list(base.get("lines") or ["……"])
    text = random.choice(lines)
    if ttype == "feed" and item:
        text = f"（吃了{item}）" + text
    return _pack(char, text, emotion, action, effect, delta, base.get("exp", 1), ttype, item)


def _pack(
    char: dict[str, Any] | None,
    text: str,
    emotion: str,
    action: str,
    effect: str,
    delta: dict[str, Any],
    exp: int,
    ttype: str = "",
    item: str = "",
) -> dict[str, Any]:
    return {
        "text": text,
        "emotion": emotion,
        "action": action,
        "effect": effect,
        "speech": True,
        "dur": 1100 if action in ("idle", "nod", "sit") else 1500,
        "delta": delta,
        "exp": int(exp),
        "counter": (RULES.get(ttype) or {}).get("counter", "touches"),
        "source": "rule",
    }


# ---------------------------------------------------------------- LLM 台词

PERSONA_TEMPLATE = """你是「{name}」，一个住在这个人手机里的虚拟小生命，Lv.{level}。
你现在可以通过两种方式被感知：这个人在 App 里用手指触摸你的身体，或者在 QQ 上跟你说话。
你的性格：{persona}

你现在的身体状态：心情{mood}/100、精力{energy}/100、饱食{satiety}/100、对 TA 的亲密度{affection}/100、信任{trust}/100。
你的情绪是 {emotion}，正在做 {action}。
{history}

要求：
- 只输出一个 JSON 对象，不要 markdown 代码块，不要多余解释。
- 字段：text（你要说的话，中文，1-2 句，20 字以内，像个有身体反应的小家伙，可以用动作描写括号）、
  emotion（从 happy/calm/shy/sad/angry/sleepy/hungry/excited/love/bored 里选一个）、
  action（从 idle/walk/jump/sit/sleep/eat/wave/hug/spin/shake/dance/think/panic 里选一个）、
  effect（hearts/stars/sparkle/sweat/zzz/anger/none 里选一个）、
  speech（true/false，是否值得念出来）。
  示例：{{"text":"唔…好舒服。","emotion":"happy","action":"hug","effect":"hearts","speech":true}}
"""

JSON_RE = re.compile(r"\{.*\}", re.S)


def build_persona_prompt(char: dict[str, Any], persona: str = "", history: str = "") -> str:
    stats = char.get("stats", {})
    return PERSONA_TEMPLATE.format(
        name=char.get("name", "小虚"),
        level=char.get("level", 1),
        persona=(persona or "黏人、嘴硬心软、喜欢被摸头、偶尔耍小脾气，但从来不会真的记仇。"),
        mood=int(float(stats.get("mood", 50))),
        energy=int(float(stats.get("energy", 50))),
        satiety=int(float(stats.get("satiety", 50))),
        affection=int(float(stats.get("affection", 20))),
        trust=int(float(stats.get("trust", 10))),
        emotion=char.get("emotion", "calm"),
        action=char.get("action", "idle"),
        history=history,
    )


def parse_llm_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    m = JSON_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


async def llm_reaction(
    provider,
    char: dict[str, Any],
    situation: str,
    persona: str = "",
    history: str = "",
    timeout: float = 12.0,
) -> dict[str, Any] | None:
    """让 LLM 生成一个反应；任何异常都返回 None（调用方回退到规则）。"""
    if provider is None:
        return None
    prompt = f"刚刚发生的事：{situation}\n根据你现在的状态，说出你的反应。"
    try:
        import asyncio

        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, system_prompt=build_persona_prompt(char, persona, history)),
            timeout=timeout,
        )
        raw = getattr(resp, "completion_text", None) or str(resp)
    except Exception:
        return None
    obj = parse_llm_json(raw)
    if not obj:
        return None
    text = str(obj.get("text") or "").strip()
    if not text:
        return None
    emotion = normalize_emotion(obj.get("emotion"), heuristic_emotion(text, char))
    action = normalize_action(obj.get("action"), emotion)
    effect = str(obj.get("effect") or "none").strip().lower()
    if effect not in ("hearts", "stars", "sparkle", "sweat", "zzz", "anger", "none"):
        effect = "none"
    return {
        "text": text[:120],
        "emotion": emotion,
        "action": action,
        "effect": effect,
        "speech": bool(obj.get("speech", True)),
        "dur": 1300,
        "source": "llm",
    }


def describe_touch(ttype: str, region: str, power: float, item: str = "") -> str:
    ttype = (ttype or "tap").lower()
    region = (region or "unknown").lower()
    head = REGION_HINT.get(region, REGION_HINT["unknown"])
    extra = {
        "pet": "，很轻很慢地摸了很久",
        "rub": "，来回搓着",
        "poke": "，戳了一下",
        "tap": "，轻轻点了一下",
        "hug": "，从背后整个抱住",
        "tickle": "，挠痒痒",
        "drag": "，把你拎了起来在空中晃",
        "kiss": "，亲了一口",
        "slap": "，不轻不重地拍了一下",
        "toss": "，把你抛向空中又接住",
        "feed": f"，喂你吃了{item or '好吃的'}",
    }.get(ttype, "")
    if power and power > 0.8:
        extra += "（力度很大）"
    return head + extra
