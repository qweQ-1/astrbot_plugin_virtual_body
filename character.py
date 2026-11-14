"""虚现互通 · 角色状态机。

角色是一组数值 + 一个当前情绪/动作。所有变化都走 ``apply_delta``，
保证数值永远被 clamp 在 0-100，并且离线也在缓慢衰减（``decay``）。
"""

from __future__ import annotations

import time
from typing import Any

STAT_KEYS = ("affection", "mood", "energy", "satiety", "clean", "trust")

EMOTIONS = (
    "happy", "calm", "shy", "sad", "angry", "sleepy",
    "hungry", "excited", "love", "bored",
)

ACTIONS = (
    "idle", "walk", "jump", "sit", "sleep", "eat", "wave",
    "hug", "spin", "shake", "dance", "think", "panic", "nod", "shakehead",
)

# 每种情绪对应的默认待机动作（没有显式动作时用）
EMOTION_DEFAULT_ACTION = {
    "happy": "idle",
    "calm": "idle",
    "shy": "shake",
    "sad": "sit",
    "angry": "shakehead",
    "sleepy": "sleep",
    "hungry": "sit",
    "excited": "jump",
    "love": "hug",
    "bored": "sit",
}

LEVEL_EXP_BASE = 120
LEVEL_EXP_STEP = 80


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


def exp_needed(level: int) -> int:
    return LEVEL_EXP_BASE + LEVEL_EXP_STEP * max(0, int(level) - 1)


def _asdict_stats(stats: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in STAT_KEYS:
        try:
            out[k] = float((stats or {}).get(k, 0))
        except Exception:
            out[k] = 0.0
    return out


def apply_delta(char: dict[str, Any], delta: dict[str, Any] | None) -> dict[str, int]:
    """把 delta 应用到角色上，返回**实际**发生的整数变化。"""
    if not delta:
        return {}
    stats = char.setdefault("stats", {})
    actual: dict[str, int] = {}
    for k in STAT_KEYS:
        if k not in delta:
            continue
        try:
            d = float(delta[k])
        except Exception:
            continue
        if not d:
            continue
        cur = float(stats.get(k, 0))
        new = clamp(cur + d)
        stats[k] = round(new, 1)
        actual[k] = int(round(new - cur))
    char["updated_at"] = time.time()
    return actual


def add_exp(char: dict[str, Any], amount: int) -> bool:
    """加经验，返回是否升级。"""
    if amount <= 0:
        return False
    char["exp"] = int(char.get("exp", 0)) + int(amount)
    leveled = False
    while char["exp"] >= int(char.get("exp_next", exp_needed(char.get("level", 1)))):
        char["exp"] = int(char["exp"]) - int(char.get("exp_next", 120))
        char["level"] = int(char.get("level", 1)) + 1
        char["exp_next"] = exp_needed(char["level"])
        leveled = True
        # 升级奖励
        stats = char.setdefault("stats", {})
        for k, v in (("mood", 12), ("affection", 4), ("energy", 20), ("trust", 2)):
            stats[k] = round(clamp(float(stats.get(k, 0)) + v), 1)
    return leveled


def decay(char: dict[str, Any], now: float | None = None) -> dict[str, int]:
    """按离线时长做衰减；返回实际变化。"""
    now = now or time.time()
    last = float(char.get("last_active") or char.get("updated_at") or now)
    hours = max(0.0, (now - last) / 3600.0)
    if hours < 0.15:
        return {}
    sleeping = char.get("action") == "sleep" or char.get("emotion") == "sleepy"
    stats = char.setdefault("stats", {})
    delta: dict[str, float] = {
        "satiety": -1.6 * hours,
        "clean": -0.7 * hours,
        "energy": (5.0 if sleeping else -1.1) * hours,
        "mood": -0.9 * hours,
    }
    # 亲密度：超过 12 小时没互动才开始掉，且很慢（掉太多会很挫败）
    if hours > 12:
        delta["affection"] = -0.35 * (hours - 12)
    actual = apply_delta(char, delta)
    # 状态太差时情绪跟着变
    if float(stats.get("satiety", 100)) < 25:
        char["emotion"] = "hungry"
    elif float(stats.get("energy", 100)) < 20:
        char["emotion"] = "sleepy"
    elif float(stats.get("mood", 100)) < 25:
        char["emotion"] = "sad"
    elif float(stats.get("affection", 0)) >= 85 and char.get("emotion") in ("calm", "happy"):
        char["emotion"] = "love"
    char["last_active"] = now
    return actual


def mood_label(char: dict[str, Any]) -> str:
    m = float(char.get("stats", {}).get("mood", 50))
    if m >= 85:
        return "超级开心"
    if m >= 65:
        return "心情不错"
    if m >= 40:
        return "还凑合"
    if m >= 20:
        return "有点低落"
    return "非常难过"


def status_card(char: dict[str, Any], owner_qq: str = "") -> str:
    stats = _asdict_stats(char.get("stats"))
    bars = {
        "亲密度": stats["affection"],
        "心情": stats["mood"],
        "精力": stats["energy"],
        "饱食": stats["satiety"],
        "清洁": stats["clean"],
        "信任": stats["trust"],
    }

    def bar(v: float) -> str:
        filled = int(round(v / 10.0))
        return "█" * filled + "░" * (10 - filled)

    lines = [
        f"【{char.get('name', '小虚')}】 Lv.{char.get('level', 1)}  "
        f"{char.get('exp', 0)}/{char.get('exp_next', 120)} exp",
        f"情绪：{char.get('emotion', 'calm')}（{mood_label(char)}）　当前动作：{char.get('action', 'idle')}",
        "",
    ]
    for k, v in bars.items():
        lines.append(f"{k}  {bar(v)} {int(v)}")
    c = char.get("counters", {})
    lines += [
        "",
        f"摸头 {c.get('pets', 0)} 次 · 戳 {c.get('pokes', 0)} 次 · 投喂 {c.get('feeds', 0)} 次 "
        f"· 聊天 {c.get('chats', 0)} 次",
        f"装扮：{char.get('outfit', 'default')}　场景：{char.get('room', 'day')}",
    ]
    if not owner_qq:
        lines.append("（还没有 App 绑定过，发送「虚现 绑定码」去绑定）")
    return "\n".join(lines)


def heuristic_emotion(text: str, char: dict[str, Any]) -> str:
    """从一句话里猜情绪（LLM 不可用 / 只做轻量判断时用）。"""
    t = text or ""
    table = [
        (("哈哈", "嘻嘻", "开心", "好耶", "太棒", "喜欢", "爱", "嘿嘿", "哇"), "happy"),
        (("生气", "讨厌", "烦", "滚", "哼", "气死"), "angry"),
        (("难过", "累", "疲惫", "伤心", "哭", "呜呜", "孤独"), "sad"),
        (("害羞", "脸红", "别这样", "不好意"), "shy"),
        (("睡", "困", "晚安", "熬夜", "打哈欠"), "sleepy"),
        (("饿", "吃", "饭", "奶茶", "好吃"), "hungry"),
        (("！", "!!", "冲", "走起", "出发"), "excited"),
        (("想你", "陪我", "抱抱", "亲"), "love"),
        (("无聊", "没事", "随便", "哦"), "bored"),
    ]
    for keys, emo in table:
        if any(k in t for k in keys):
            return emo
    stats = _asdict_stats(char.get("stats"))
    if stats["energy"] < 25:
        return "sleepy"
    if stats["satiety"] < 25:
        return "hungry"
    if stats["mood"] >= 70:
        return "happy"
    if stats["mood"] <= 30:
        return "sad"
    return "calm"


def normalize_emotion(emotion: str | None, fallback: str = "calm") -> str:
    e = (emotion or "").strip().lower()
    aliases = {
        "joy": "happy", "angry_": "angry", "happy_": "happy", "shy_": "shy",
        "tired": "sleepy", "sleep": "sleepy", "hungry_": "hungry", "excited_": "excited",
        "love_": "love", "bored_": "bored", "normal": "calm", "neutral": "calm",
    }
    e = aliases.get(e, e)
    return e if e in EMOTIONS else fallback


def normalize_action(action: str | None, emotion: str = "calm") -> str:
    a = (action or "").strip().lower()
    if a in ACTIONS:
        return a
    return EMOTION_DEFAULT_ACTION.get(emotion, "idle")


def set_mood_pose(char: dict[str, Any], x: float | None = None, y: float | None = None) -> None:
    pose = char.setdefault("pose", {"x": 0.5, "y": 0.62, "facing": 1, "scale": 1.0})
    if x is not None:
        pose["x"] = round(clamp(float(x), 0.05, 0.95), 4)
    if y is not None:
        pose["y"] = round(clamp(float(y), 0.15, 0.95), 4)
    char["updated_at"] = time.time()
