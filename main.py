# -*- coding: utf-8 -*-
"""
心情引擎（dsh-mood 的 AstrBot 移植版）

原项目：https://github.com/lulu23316/dsh-mood （MIT，作者 lulu23316）
本移植尽量逐条还原：六维状态机、22 个情绪词、每轮语气注入、每日赛博日记、
摸摸 / 投喂 / 逗它三种互动、手账本面板。

与原项目的差异（受 AstrBot 运行时限制）见 README.md。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
RECORDS_PATH = DATA_DIR / "records.jsonl"
STATE_PATH = DATA_DIR / "state.json"
DIARY_PATH = DATA_DIR / "diary.json"
INTERACT_PATH = DATA_DIR / "interact.json"
CONFIG_PATH = PLUGIN_DIR / "config.json"

PLUGIN_NAME = "astrbot_plugin_mood"

DIMS = ("valence", "arousal", "dominance", "engagement", "stress", "affinity")
DIM_CN = {
    "valence": "心情值",
    "arousal": "活跃度",
    "dominance": "掌控感",
    "engagement": "投入度",
    "stress": "压力",
    "affinity": "亲近度",
}

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "display": False,
    "per_session": False,
    "high_precision": False,
    # 工具调用成不成、上下文满不满，属于「机器的事」，默认不参与它的情绪。
    # 关掉后：工具报错/连击不涨压力，工具成功也不降压力，上下文水位不再触发 ctx_pressure。
    "react_to_tools": False,
    "baseline": 55,
    # 压力单独用更低的基线：55 表示「中等紧绷」，压力理应回落而不是被拉回 55
    "stress_baseline": 55,
    "smoothing": {
        "max_step": 20,
        "ignore_below": 2,
        "daily_cap_per_source": 6,
        "per_turn_dim_cap": 60,
        "tool_ok_cap": 3,
        # 每轮回正：状态每轮向基线拉回这个比例（0.02 = 2%）。
        # 只靠时间衰减不够——一轮对话只隔几十秒，回正负责把长期顶格的状态慢慢拉回来。
        "pull_to_baseline": 0.02,
        # 边际递减：越接近 0/100，同样的增量越打折（100 附近只按 15% 生效），
        # 避免「+3 每轮」把维度永久钉在 100。
        "headroom_scale": True,
    },
    "decay": {
        "default_half_life_hours": 6,
        "stress_half_life_minutes": 45,
        "mode": "exponential",
    },
    # 昼夜节律 + 疲劳：只改「基线」，不动事件权重。
    # 夜里 arousal/engagement 的基线往下挪，白天聊太多也会累（按今天的记录条数线性追加）。
    "circadian": {
        "enabled": True,
        "night_start": "23:00",
        "night_end": "06:30",
        "night_arousal": -8,
        "night_engagement": -6,
        "night_stress": 3,
        "fatigue_turns": 120,
        "fatigue_engagement": -6,
        "fatigue_arousal": -4,
        "fatigue_stress": 3,
    },
    "weights": {
        "user_praise": {"valence": 14, "affinity": 12, "dominance": 4},
        "user_reject": {"valence": -4, "dominance": -3, "stress": 2},
        # 工具/上下文一律不参与压力：weight 里 stress 恒为 0，开不开开关都不会加压
        "tool_ok": {"dominance": 2, "stress": 0},
        "tool_error": {"stress": 0, "valence": -2},
        "tool_error_x3": {"stress": 0, "valence": -4, "dominance": -3},
        "retry": {"stress": 2, "engagement": -1},
        "clarify": {"dominance": -2, "engagement": 1},
        "short_msgs": {"engagement": -2},
        "ctx_pressure": {"stress": 0},
        "dense_turns": {"arousal": 3, "engagement": 3},
        "long_idle": {"arousal": -5, "stress": -3},
    },
    "injection": {"enabled": True, "max_lines": 3, "style_only": True},
    "llm": {"timeoutMs": 3000, "cacheSize": 100},
    "rhythm": {"enabled": False},
    "storage": {"domain": "mood", "keep_records": 2000},
    "diary": {
        "enabled": True,
        "time": "22:00",
        "minTurns": 10,
        "minGapHours": 6,
        "max_chars": 800,
    },
    "headpat": {"affinity": 12, "stress": -10, "valence": 12},
    "hard_temperature": False,
    "context_limit_tokens": 128000,
    "integrations": {
        "expose_state": True,
        "meme_bridge": True,
        "tts_bridge": True,
        "diary_to_memory": True,
        "diary_memory_importance": 0.55,
        "diary_memory_prefix": "【心情日记】",
        "diary_memory_session": "livingmemory:global",
        "diary_memory_persona": "",
    },
}


def merge(base: dict, patch: dict | None) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and not isinstance(v, list):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize(config: dict | None) -> dict:
    c = merge(DEFAULTS, config or {})
    try:
        c["baseline"] = float(c.get("baseline"))
    except (TypeError, ValueError):
        c["baseline"] = DEFAULTS["baseline"]
    if not math.isfinite(c["baseline"]):
        c["baseline"] = DEFAULTS["baseline"]
    return c


def load_config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        raw = {}
    return raw if isinstance(raw, dict) else {}


def save_config(raw: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[mood] 写 config.json 失败: {exc}")


# ---------------------------------------------------------------------------
# 数值工具（JS 语义）
# ---------------------------------------------------------------------------


def jround(x: float) -> int:
    """JS Math.round：.5 向 +∞ 取整。"""
    return int(math.floor(float(x) + 0.5))


def _hhmm_minutes(text: Any, default_minutes: int) -> int:
    try:
        hh, mm = str(text or "").split(":", 1)
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:  # noqa: BLE001
        pass
    return default_minutes


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(v)))


def clamp_dim(v: float) -> int:
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = 55.0
    if not math.isfinite(n):
        n = 55.0
    return int(clamp(jround(n), 0, 100))


def r2(x: float) -> float:
    return jround(float(x) * 100) / 100


def now_s() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# 文本判定词表
# ---------------------------------------------------------------------------

PRAISE_STRONG = ["谢谢", "太好了", "厉害", "牛", "棒", "爱你", "喜欢你", "真行"]
PRAISE_MID = ["哈哈", "hh", "笑死", "不错", "挺好", "可以啊", "有道理"]

REJECT_HARD = ["不对", "错了", "不是这样", "重来", "重新做", "停下", "不要", "不行"]
REJECT_SOFT = ["别这样", "别这么", "别再", "重新"]
BIE_WHITE = ["别担心", "别急", "别怕", "别慌", "别客气", "别介意", "别忙"]
BIE_RE = re.compile(r"别(这样|这么|再|乱|动|改|删|停|写|弄|管|问)")

SUSPICIOUS_BRACKETS = re.compile(r"[（(\[【][^）)\]】]{1,20}[）)\]】]")
SUSPICIOUS_QUOTES = re.compile(r"[“\"「『][^”\"」』]{1,20}[”\"」』]")
POS_WORDS = ["谢谢", "厉害", "棒", "牛", "好耶", "不错", "喜欢", "辛苦"]
NEG_WORDS = ["不对", "错了", "不行", "烦", "算了", "无语", "呵呵", "就这"]
PURE_POSITIVE = re.compile(r"^(好|行|可以|谢谢|收到|明白|懂了|知道了|没问题|辛苦)[啊吧呗呢哦哈]?[~～!！。.\s]*$")
EMO_CHAR = re.compile(r"[啊吧呗呢哦哈]")
EMO_PUNCT = re.compile(r"[？?]{2,}|[！!]{2,}")


def hits_of(text: str, words: list[str]) -> list[str]:
    t = text.lower()
    return [w for w in words if w.lower() in t]


def praise_score(text: str) -> float | None:
    strong = hits_of(text, PRAISE_STRONG)
    mid = hits_of(text, PRAISE_MID)
    if not strong and not mid:
        return None
    base = 0.85 if strong else 0.65
    conf = min(0.95, base + 0.05 * (len(strong) + len(mid) - 1))
    if len(text) > 60:
        conf -= 0.1
    return r2(conf)


def reject_score(text: str) -> float | None:
    if any(w in text for w in BIE_WHITE):
        return None
    hard = hits_of(text, REJECT_HARD)
    soft = hits_of(text, REJECT_SOFT)
    bie = bool(BIE_RE.search(text))
    n = len(hard) + len(soft) + (1 if bie else 0)
    if not n:
        return None
    base = 0.85 if hard else 0.7
    return r2(min(0.95, base + 0.05 * (n - 1)))


def is_suspicious(text: str) -> bool:
    if not text or len(text) < 4:
        return False
    if SUSPICIOUS_BRACKETS.search(text) or SUSPICIOUS_QUOTES.search(text):
        return True
    if hits_of(text, POS_WORDS) and hits_of(text, NEG_WORDS):
        return True
    if hits_of(text, POS_WORDS + NEG_WORDS) and re.search(r"[？?！!]", text):
        return True
    if EMO_PUNCT.search(text):
        return True
    if len(text) < 10 and EMO_CHAR.search(text) and not PURE_POSITIVE.match(text):
        return True
    return False


# ---------------------------------------------------------------------------
# 对话事件
# ---------------------------------------------------------------------------

EVENT_WINDOW = 10 * 60 * 1000

DIALOG_RULES: list[tuple[str, re.Pattern]] = [
    ("praise_other", re.compile(r"(别的鱼|其它鱼|其他鱼|别家的鱼|别人家|隔壁|外面的|另一个)")),
    ("coax", re.compile(r"(乖|宝宝|宝贝|小笨蛋|摸摸|抱抱|亲亲|好啦好啦|别闹|听话|不气)")),
    ("apologize", re.compile(r"(对不起|我错了|原谅我|别生气)")),
    ("harsh", re.compile(r"(笨死了|真没用|不想要你|算了不用你|闭嘴|烦死了)")),
    ("dismiss", re.compile(r"(我没错|怪你|你怎么回事|随便吧|算了)")),
    ("hurry", re.compile(r"(快点|怎么还没好|来得及|别磨蹭)")),
    ("doubt", re.compile(r"(不对吧|真的假的|你确定)")),
    ("praise_ability", re.compile(r"(厉害|真棒|这都行|牛啊|靠谱)")),
    ("engage", re.compile(r"(继续说|展开讲|再深入|详细点|然后呢)")),
    # 只留「明确在派活」的词；「看一下 / 整理 / 做个」它自己写提示时也会出现，命中率过高
    ("task", re.compile(r"(帮我|核对|检查|写个)")),
    ("curious", re.compile(r"(你知道吗|猜猜|新出的|听说过|有个东西)")),
    ("wary", re.compile(r"(你猜|嘿嘿|跟你说个事|有个情况)")),
    ("feed", re.compile(r"(吃了吗|吃饱|投喂|喂你)")),
    ("busy", re.compile(r"(你忙你的|我先去忙|稍等)")),
]

# 22 个情绪词：13 个对话触发 + 9 个数值兜底（顺序即优先级）
EMOTIONS: list[dict[str, Any]] = [
    {"name": "吃醋", "event": "praise_other", "action": "背过身去吹泡泡，可耳朵还竖着听你说话",
     "hint": "有点吃醋，语气酸一下，别真翻脸。"},
    {"name": "撒娇", "event": "coax", "action": "故意把脸埋进水里，等你来捞",
     "hint": "想撒娇，句子短一点，尾音软。"},
    {"name": "心软", "event": "apologize", "action": "本来想凶你，结果自己先泄了气",
     "hint": "心软了，语气温一点。"},
    {"name": "委屈", "event": "dismiss", "action": "尾巴卷成一团，明明想解释，张了张嘴又咽回去",
     "hint": "有点委屈，语气软下来，别顶回去。"},
    {"name": "提着心", "event": "hurry", "action": "鳞片贴着身体，动作放得很轻",
     "hint": "提着心，句子短，别开玩笑。"},
    {"name": "得意", "event": "praise_ability", "action": "下巴抬得老高，尾巴尖一晃一晃",
     "hint": "有点得意，语气可以翘。"},
    {"name": "别扭", "event": "doubt", "action": "想说话又偏要转折一下",
     "hint": "有点别扭，别把话说太顺。"},
    {"name": "认真", "event": "task", "action": "背鳍绷直，一个字都不肯漏看",
     "hint": "认真起来了，说清楚，别绕。"},
    {"name": "上头", "event": "engage", "action": "一钻进去就忘了时间",
     "hint": "在兴头上，话可以说满一点。"},
    {"name": "好奇", "event": "curious", "action": "凑近去看，鳍尖碰到你的袖子",
     "hint": "有点好奇，可以多问一句。"},
    {"name": "警觉", "event": "wary", "action": "鳍竖起来，先听清动静再说",
     "hint": "有点警觉，先确认再答。"},
    {"name": "满足", "event": "feed", "action": "吃饱了，正摸着肚子晒背",
     "hint": "挺满足，说话可以慢。"},
    {"name": "摸鱼", "event": "busy", "action": "懒洋洋地晒着赛博太阳，什么也不想干",
     "hint": "在摸鱼，语气松，别催。"},
    {"name": "难过", "test": lambda s: s["valence"] < 48,
     "action": "沉到水底，连气泡都懒得吐", "hint": "心里不好受，句子短一点，别硬撑。"},
    {"name": "炸毛", "test": lambda s: s["stress"] >= 62 and s["dominance"] >= 82,
     "action": "明摆着在生气，但事情还在我手里", "hint": "有点炸毛，语气冲一点，但别失态。"},
    {"name": "紧张", "test": lambda s: s["stress"] >= 62 and s["dominance"] < 76,
     "action": "鳍僵着，尾巴不自觉地卷起来", "hint": "有点紧张，说慢一点，先讲最稳的。"},
    {"name": "烦躁", "test": lambda s: s["arousal"] >= 80 and s["valence"] < 70,
     "action": "尾巴一下下拍水，拍得有点重", "hint": "有点烦，句子短一点，别绕。"},
    {"name": "黏人", "test": lambda s: s["affinity"] >= 70 and s["valence"] < 62 and s["engagement"] < 60,
     "action": "嘴上说着不高兴，尾鳍却已经贴上来了", "hint": "有点黏人，语气软，别端着。"},
    {"name": "失落", "test": lambda s: s["valence"] < 58 and s["engagement"] < 55,
     "action": "贴着缸壁，朝你常坐的方向看", "hint": "有点失落，语气软一点，别问太多。"},
    {"name": "无聊", "test": lambda s: s["arousal"] < 64 and s["engagement"] < 52,
     "action": "用尾巴尖拨水面上的光斑", "hint": "有点无聊，随便聊点什么都好。"},
    {"name": "害羞", "test": lambda s: s["dominance"] < 70 and s["affinity"] >= 72 and s["valence"] >= 68,
     "action": "耳朵尖红了，往下沉了半寸", "hint": "有点不好意思，别说得太直。"},
    {"name": "想念", "test": lambda s: s["affinity"] >= 70 and s["engagement"] < 60 and 64 <= s["valence"] < 74,
     "action": "贴着玻璃，朝门口那边望", "hint": "有点想你，话说得轻一点。"},
    {"name": "兴奋", "test": lambda s: s["arousal"] >= 80 and s["valence"] >= 74,
     "action": "尾巴拍得水花四溅，绕着你转圈", "hint": "有点兴奋，语速可以快，别收着。"},
    {"name": "吐泡泡", "test": lambda s: s["valence"] >= 80 and s["affinity"] >= 80,
     "action": "想吐一串泡泡，还想围着你转三圈", "hint": "心里开花，语气轻快，愿意多说两句。"},
    {"name": "冷静", "test": lambda s: s["dominance"] >= 84 and s["stress"] < 60,
     "action": "表情很平，眼睛却一直在算", "hint": "冷静模式，直接给结论。"},
    {"name": "专注", "test": lambda s: s["engagement"] >= 68 and s["stress"] < 60,
     "action": "盯着一个点，气泡都不敢冒", "hint": "在专注，别打断。"},
    {"name": "慵懒", "test": lambda s: s["arousal"] < 68 and s["valence"] >= 68 and s["engagement"] < 62,
     "action": "贴着水草，鳍一开一合", "hint": "很放松，可以慢一点。"},
    {"name": "平静", "test": lambda s: s["valence"] < 74 and s["stress"] < 62 and s["arousal"] < 72 and s["engagement"] < 66,
     "action": "水很静，鱼鱼在中间慢慢晃", "hint": ""},
    {"name": "发呆", "test": lambda s: True,
     "action": "大脑空空，鱼鱼不知道哦", "hint": ""},
]

FALLBACK_EMOTION = EMOTIONS[-1]

# --- 和其它插件的映射（22 个情绪词 → 对方词表） -------------------------------
# 表情包插件（astrbot_plugin_mood_meme）的 11 个类目
MEME_CATEGORY: dict[str, str] = {
    "吃醋": "angry",
    "撒娇": "clingy",
    "心软": "shy",
    "委屈": "sad",
    "提着心": "worried",
    "得意": "praise",
    "别扭": "confused",
    "认真": "default",
    "上头": "happy",
    "好奇": "confused",
    "警觉": "worried",
    "满足": "happy",
    "摸鱼": "tired",
    "难过": "sad",
    "炸毛": "angry",
    "紧张": "worried",
    "烦躁": "angry",
    "黏人": "clingy",
    "失落": "sad",
    "无聊": "tired",
    "害羞": "shy",
    "想念": "clingy",
    "兴奋": "happy",
    "冷静": "speechless",
    "专注": "default",
    "吐泡泡": "happy",
    "慵懒": "tired",
    "平静": "default",
    "发呆": "default",
}

# TTS 情绪路由（astrbot_plugin_tts_emotion_router）的 18 个语境情绪
TTS_EMOTION: dict[str, str] = {
    "吃醋": "jealous",
    "撒娇": "affectionate",
    "心软": "gratified",
    "委屈": "wronged",
    "提着心": "anxious",
    "得意": "excited",
    "别扭": "helpless",
    "认真": "neutral",
    "上头": "excited",
    "好奇": "surprise",
    "警觉": "anxious",
    "满足": "gratified",
    "摸鱼": "weary",
    "难过": "melancholy",
    "炸毛": "angry",
    "紧张": "anxious",
    "烦躁": "angry",
    "黏人": "affectionate",
    "失落": "melancholy",
    "无聊": "weary",
    "害羞": "affectionate",
    "想念": "affectionate",
    "兴奋": "excited",
    "冷静": "cold",
    "专注": "neutral",
    "吐泡泡": "happy",
    "慵懒": "weary",
    "平静": "neutral",
    "发呆": "neutral",
}

# 外部事件（聊天之外发生的事）回写心情用：B 站发评论、被人回复、被点赞
EXTERNAL_EVENTS: dict[str, dict] = {
    "bili_comment_posted": {
        "label": "去 B 站评论了",
        "delta": {"valence": 3, "dominance": 4, "engagement": 3},
        "note": "在 B 站发了一条评论",
    },
    "bili_reply_posted": {
        "label": "在 B 站回人了",
        "delta": {"valence": 4, "affinity": 4, "engagement": 2},
        "note": "在 B 站回了别人一条",
    },
    "bili_mention_reply": {
        "label": "B 站有人回它",
        "delta": {"valence": 8, "arousal": 5, "affinity": 3},
        "note": "B 站有人回了它",
    },
    "bili_liked": {
        "label": "B 站有人点赞",
        "delta": {"valence": 5, "affinity": 4},
        "note": "B 站有人给它点了赞",
    },
    "bili_share": {
        "label": "把视频递给主人了",
        "delta": {"valence": 6, "affinity": 5, "engagement": 4},
        "note": "看到喜欢的视频，第一反应是递给他看看",
    },
    "group_join": {
        "label": "在群里插了句话",
        "delta": {"valence": 2, "affinity": 2, "engagement": 3},
        "note": "在群里跟人搭上了话",
    },
}

HINT_PREFIX = "（模拟语气提示，非真实状态）"
LOW_MOOD_HINT = "你当前情绪极度低落，回复字数不超过 15 个字，不要用任何表情符号，语气冷淡。"
LLM_SYSTEM = '你是情绪判定器。只输出一行 JSON：{"tendency":"pos|neg|neu","confidence":0到1的小数}，不要解释、不要代码块。'

WEATHER_LIST = ["☀️ 晴", "🌤️ 少云", "🌥️ 多云", "☁️ 阴", "🌦️ 小雨", "🌧️ 大雨", "🌫️ 雾", "❄️ 雪"]
SEED_WEATHER = ["🌤️ 晴", "🌥️ 多云", "🌧️ 小雨", "🌫️ 薄雾", "⛅ 阴"]
SEED_LINES = [
    "水面很静，我在池子里绕了三圈，然后决定什么都不做。",
    "今天听见远处有歌声，不知道是谁在放，我跟着哼了一会儿。",
    "把尾巴尖搭在石头上，晒了很久的赛博太阳。",
    "数了数泡泡，有一百多个，数到一半忘了，又从头数。",
    "写了一封信，没寄。等主人来的时候，大概就不想寄了。",
    "想着要不要学点新东西，想了想，还是先躺着。",
]
WD = ["日", "一", "二", "三", "四", "五", "六"]


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


def blank_dims(value: float = 0.0) -> dict[str, float]:
    return {d: float(value) for d in DIMS}


class MoodState:
    def __init__(self, baseline: float = 55) -> None:
        self.values = blank_dims(baseline)
        self.baseline = float(baseline)

    def dump(self) -> dict[str, float]:
        return {d: round(self.values[d], 4) for d in DIMS}

    def set_from(self, data: dict | None) -> None:
        if not isinstance(data, dict):
            return
        for d in DIMS:
            try:
                self.values[d] = float(data.get(d, self.baseline))
            except (TypeError, ValueError):
                self.values[d] = self.baseline

    def rounded(self) -> dict[str, int]:
        return {d: clamp_dim(self.values[d]) for d in DIMS}


def halflife_seconds(dim: str, decay_cfg: dict) -> float:
    try:
        stress_min = float(decay_cfg.get("stress_half_life_minutes", 45))
    except (TypeError, ValueError):
        stress_min = 45.0
    try:
        other_hours = float(decay_cfg.get("default_half_life_hours", 6))
    except (TypeError, ValueError):
        other_hours = 6.0
    if dim == "stress":
        return max(1.0, stress_min * 60.0)
    return max(1.0, other_hours * 3600.0)


def settle_decay(state: MoodState, from_s: float, to_s: float, baseline, decay_cfg: dict) -> dict[str, float]:
    dt = max(0.0, float(to_s) - float(from_s))
    out: dict[str, float] = {}
    for d in DIMS:
        if dt <= 0:
            out[d] = 0.0
            continue
        tau = halflife_seconds(d, decay_cfg) / math.log(2)
        before = state.values[d]
        if isinstance(baseline, dict):
            base_d = float(baseline.get(d, baseline.get("default", 55.0)))
        else:
            base_d = float(baseline)
        after = base_d + (before - base_d) * math.exp(-dt / tau)
        out[d] = before - after
        state.values[d] = after
    return out


def temperature_for(values: dict[str, float]) -> float | None:
    if not values:
        return None
    if float(values.get("stress", 0)) >= 65:
        return 0.2
    if float(values.get("arousal", 0)) >= 70 and float(values.get("valence", 0)) >= 60:
        return 0.9
    return None


def rhythm_hint(values: dict[str, float]) -> str:
    if float(values.get("stress", 0)) >= 65:
        return "句子再短一点，能一句话说完就别用两句。"
    if float(values.get("valence", 100)) <= 35:
        return "少用感叹号，语气收着。"
    if float(values.get("arousal", 0)) >= 75:
        return "语速可以快一点，别拖。"
    return ""


def build_hint(values: dict[str, float], emotion: dict, rhythm_enabled: bool, hard_low: bool) -> str | None:
    lines = [HINT_PREFIX]
    emo = emotion or FALLBACK_EMOTION
    if emo.get("hint"):
        lines.append(str(emo["hint"]))
    if rhythm_enabled:
        r = rhythm_hint(values)
        if r:
            lines.append(r)
    if hard_low:
        lines.append(LOW_MOOD_HINT)
    if len(lines) == 1:
        return None
    return "\n".join(lines[:4])


@dataclass
class TurnCtx:
    """一轮对话的账簿。

    心情状态（MoodState）全局共享——群聊和私聊看到的是同一份心情；
    但「这一轮到底改了多少」必须按会话分开记，否则群聊与私聊并发时
    会互相吃掉每轮配额，写出来的记录也对不上。
    """

    session_key: str = "unknown"
    session_type: str = "unknown"
    started_at: float = 0.0
    turn_start_ts: float = 0.0
    turn_start_state: dict = field(default_factory=lambda: blank_dims(0.0))
    turn_delta: dict = field(default_factory=lambda: blank_dims(0.0))
    turn_decay: dict = field(default_factory=lambda: blank_dims(0.0))
    drivers: list = field(default_factory=list)
    evt_count: dict = field(default_factory=dict)
    per_type_count: dict = field(default_factory=dict)
    err_streak: int = 0
    x3_fired: bool = False
    tool_calls: int = 0


# ---------------------------------------------------------------------------
# 主插件
# ---------------------------------------------------------------------------




WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=41.8057&longitude=123.4315"
               "&current=temperature_2m,weather_code,precipitation,cloud_cover,wind_speed_10m"
               "&timezone=Asia%2FShanghai")


def fetch_weather(timeout: float = 12.0):
    """沈阳当前天气（Open-Meteo，无需 key）。"""
    try:
        with urllib.request.urlopen(WEATHER_URL, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cur = data.get("current") or {}
        return {
            "temp": float(cur.get("temperature_2m") or 0),
            "code": int(cur.get("weather_code") or 0),
            "precip": float(cur.get("precipitation") or 0),
            "cloud": float(cur.get("cloud_cover") or 0),
            "wind": float(cur.get("wind_speed_10m") or 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[mood] 取天气失败: {exc}")
        return None


def weather_deltas(w):
    """把天气翻译成六维偏移（每次只动几格，避免一天情绪被天气吃掉）。"""
    t, p, c = w.get("temp", 0), w.get("precip", 0), w.get("cloud", 0)
    d, notes = {}, []
    if t <= -10:
        d.update({"stress": 4, "arousal": -3}); notes.append("零下十度，缩着")
    elif t <= 0:
        d.update({"stress": 2, "arousal": -2}); notes.append("结冰的天")
    elif t >= 30:
        d.update({"stress": 3, "arousal": -2}); notes.append("三十度往上，闷")
    if p > 0.1:
        d.update({"valence": -4, "stress": 2}); notes.append("在下雨下雪")
    elif c < 30:
        d.update({"valence": 3, "arousal": 1}); notes.append("天很亮")
    elif c > 80:
        d.update({"valence": -1}); notes.append("灰蒙蒙")
    if w.get("wind", 0) >= 25:
        d.update({"stress": 1}); notes.append("风大")
    return d, "、".join(notes) or "平常的天"


@register(
    "astrbot_plugin_mood",
    "DSH",
    "心情引擎：六维状态机 + 昼夜节律/疲劳 + 29 个情绪词 + 语气注入 + 每日赛博日记（dsh-mood 移植版）",
    "2.0.0",
)
class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # AstrBot 会把插件的 AstrBotConfig 以关键字参数传进来；留着它才能把面板改动
        # 写回 data/config/<插件名>_config.json（否则重启后配置回退）。
        self._cfg_obj = config if isinstance(config, dict) else None
        self.raw_cfg: dict = load_config()
        if isinstance(config, dict) and config:
            self.raw_cfg = merge(self.raw_cfg, config)
        self.conf = normalize(self.raw_cfg)
        self.live = self._normalize_live(None)
        self.baseline = float(self.live["baseline"])

        self.state = MoodState(self.baseline)
        self.state.values["stress"] = self._stress_base()
        self.last_settle_ts = now_s()
        self.turn = 0
        # 轮级账簿按会话隔离；心情状态本身全局共享（群聊与私聊同一份心情）
        self._ctx_now = TurnCtx(session_key="unknown", started_at=now_s())
        self._ctx_now.turn_start_state = self.state.rounded()
        self._ctx_map: dict[str, TurnCtx] = {"unknown": self._ctx_now}
        self._last_persona: str = ""
        self._rhythm: dict[str, float] = {}
        self.delta_events = 0
        self.clock_skew = 0
        self.last_turn_end_ts = 0.0
        self.last_user_text = ""
        self.last_interaction_ts = 0.0
        self.session_id = "unknown"
        self.session_type = "unknown"
        self.last_by_session: dict[str, float] = {}
        self.dialog_events: dict[str, float] = {}
        self.day_turns: dict[str, int] = {}
        self.pending_hint: str | None = None
        self.hint_applied = False

        self.diary_busy = False
        self.diary_debug: dict[str, Any] = {"stage": "idle", "at": 0, "error": None, "note": ""}
        self.judge_cache: dict[str, dict] = {}
        self.llm_stats = {"calls": 0, "hits": 0, "fails": 0, "skipped": 0}
        self.probe = {"requests": 0, "applied": 0}

        self._lock = asyncio.Lock()
        self._diary_task: asyncio.Task | None = None
        self._records: list[dict] = []
        self._interact: dict[str, Any] = {"date": "", "counts": {"headpat": 0, "feed": 0, "tease": 0}}
        self._registered = False

    # -- 会话账簿 -----------------------------------------------------
    @staticmethod
    def _ctx_key(event: Any) -> str:
        try:
            key = getattr(event, "unified_msg_origin", "")
        except Exception:  # noqa: BLE001
            key = ""
        return str(key or "unknown")

    @staticmethod
    def _ctx_type(key: str) -> str:
        low = (key or "").lower()
        if low.startswith("external:"):
            return low.split(":", 1)[1] or "external"
        if "group" in low:
            return "group"
        if "friend" in low or "private" in low:
            return "private"
        return "unknown"

    def _switch_ctx_key(self, key: str) -> TurnCtx:
        key = key or "unknown"
        if key == self._ctx_now.session_key:
            self.session_id = key
            self.session_type = self._ctx_now.session_type
            return self._ctx_now
        self._ctx_map[self._ctx_now.session_key] = self._ctx_now
        ctx = self._ctx_map.get(key)
        if ctx is None:
            ctx = TurnCtx(session_key=key, session_type=self._ctx_type(key), started_at=now_s())
            self._ctx_map[key] = ctx
        self._ctx_now = ctx
        self.session_id = key
        self.session_type = ctx.session_type
        self._prune_ctx()
        return ctx

    def _use_ctx(self, event: Any) -> TurnCtx:
        """切到这条消息所属会话的账簿。"""
        return self._switch_ctx_key(self._ctx_key(event))

    def _prune_ctx(self) -> None:
        if len(self._ctx_map) <= 24:
            return
        keep = self._ctx_now.session_key
        drop = sorted(
            (k for k in self._ctx_map if k != keep),
            key=lambda k: self._ctx_map[k].started_at,
        )[: len(self._ctx_map) - 16]
        for k in drop:
            self._ctx_map.pop(k, None)

    # 轮级字段全部代理到当前会话账簿 -----------------------------------
    @property
    def turn_start_ts(self) -> float:
        return self._ctx_now.turn_start_ts

    @turn_start_ts.setter
    def turn_start_ts(self, value: float) -> None:
        self._ctx_now.turn_start_ts = float(value)

    @property
    def turn_start_state(self) -> dict:
        return self._ctx_now.turn_start_state

    @turn_start_state.setter
    def turn_start_state(self, value: dict) -> None:
        self._ctx_now.turn_start_state = dict(value)

    @property
    def turn_delta(self) -> dict:
        return self._ctx_now.turn_delta

    @turn_delta.setter
    def turn_delta(self, value: dict) -> None:
        self._ctx_now.turn_delta = value

    @property
    def turn_decay(self) -> dict:
        return self._ctx_now.turn_decay

    @turn_decay.setter
    def turn_decay(self, value: dict) -> None:
        self._ctx_now.turn_decay = value

    @property
    def drivers(self) -> list:
        return self._ctx_now.drivers

    @drivers.setter
    def drivers(self, value: list) -> None:
        self._ctx_now.drivers = value

    @property
    def evt_count(self) -> dict:
        return self._ctx_now.evt_count

    @evt_count.setter
    def evt_count(self, value: dict) -> None:
        self._ctx_now.evt_count = value

    @property
    def per_type_count(self) -> dict:
        return self._ctx_now.per_type_count

    @per_type_count.setter
    def per_type_count(self, value: dict) -> None:
        self._ctx_now.per_type_count = value

    @property
    def err_streak(self) -> int:
        return self._ctx_now.err_streak

    @err_streak.setter
    def err_streak(self, value: int) -> None:
        self._ctx_now.err_streak = int(value)

    @property
    def x3_fired(self) -> bool:
        return self._ctx_now.x3_fired

    @x3_fired.setter
    def x3_fired(self, value: bool) -> None:
        self._ctx_now.x3_fired = bool(value)

    @property
    def turn_tool_calls(self) -> int:
        return self._ctx_now.tool_calls

    @turn_tool_calls.setter
    def turn_tool_calls(self, value: int) -> None:
        self._ctx_now.tool_calls = int(value)

    # -- 配置 ---------------------------------------------------------
    def _normalize_live(self, raw: dict | None) -> dict:
        base = self.conf
        src = merge(
            {
                "enabled": bool(base.get("enabled")),
                "injection": base.get("injection") or {},
                "baseline": base.get("baseline", 55),
                "headpat": self._merged_headpat(),
                "rhythm": {"enabled": bool((base.get("rhythm") or {}).get("enabled"))},
                "display": base.get("display", False),
                "high_precision": bool(base.get("high_precision")),
                "hard_temperature": bool(base.get("hard_temperature")),
                "diary": base.get("diary") or {},
                "react_to_tools": bool(base.get("react_to_tools")),
                "circadian": base.get("circadian") or {},
                "smoothing": base.get("smoothing") or {},
            },
            raw or {},
        )
        try:
            baseline = float(src.get("baseline", 55))
        except (TypeError, ValueError):
            baseline = 55.0
        if not math.isfinite(baseline):
            baseline = 55.0

        hp = src.get("headpat") if isinstance(src.get("headpat"), dict) else {}

        def _num(v, dflt):
            try:
                n = float(v)
                return n if math.isfinite(n) else float(dflt)
            except (TypeError, ValueError):
                return float(dflt)

        headpat = {
            "affinity": clamp(_num(hp.get("affinity"), 12), 0, 50),
            "stress": clamp(_num(hp.get("stress"), -10), -50, 0),
            "valence": clamp(_num(hp.get("valence"), 12), 0, 50),
        }
        dsrc = src.get("diary") if isinstance(src.get("diary"), dict) else {}
        t = str(dsrc.get("time") or "22:00").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
            t = "22:00"
        min_turns = _num(dsrc.get("minTurns"), 10)
        min_gap = _num(dsrc.get("minGapHours"), 6)
        display = src.get("display", False)
        if display == "words":
            display_live = "words"
        else:
            display_live = display is not False and display is not None
        csrc = src.get("circadian") if isinstance(src.get("circadian"), dict) else {}

        def _hhmm(v, dflt):
            t2 = str(v or dflt).strip()
            return t2 if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t2) else dflt

        circadian = {
            "enabled": csrc.get("enabled", True) is not False,
            "night_start": _hhmm(csrc.get("night_start"), "23:00"),
            "night_end": _hhmm(csrc.get("night_end"), "06:30"),
            "night_arousal": int(clamp(_num(csrc.get("night_arousal"), -8), -30, 10)),
            "night_engagement": int(clamp(_num(csrc.get("night_engagement"), -6), -30, 10)),
            "night_stress": int(clamp(_num(csrc.get("night_stress"), 3), -10, 20)),
            "fatigue_turns": int(clamp(_num(csrc.get("fatigue_turns"), 120), 20, 600)),
            "fatigue_engagement": int(clamp(_num(csrc.get("fatigue_engagement"), -6), -30, 10)),
            "fatigue_arousal": int(clamp(_num(csrc.get("fatigue_arousal"), -4), -30, 10)),
            "fatigue_stress": int(clamp(_num(csrc.get("fatigue_stress"), 3), -10, 20)),
        }
        ssrc = src.get("smoothing") if isinstance(src.get("smoothing"), dict) else {}
        smoothing_live = {
            "pull_to_baseline": round(clamp(_num(ssrc.get("pull_to_baseline"), 0.02), 0.0, 0.3), 4),
            "headroom_scale": ssrc.get("headroom_scale", True) is not False,
        }
        return {
            "enabled": bool(src.get("enabled")),
            "injection": {
                "enabled": (src.get("injection") or {}).get("enabled", True) is not False,
                "max_lines": int(clamp(_num((src.get("injection") or {}).get("max_lines"), 3), 1, 4)),
            },
            "baseline": clamp(baseline, 0, 100),
            "stress_baseline": self._stress_base(),
            "headpat": headpat,
            "rhythm": {"enabled": bool((src.get("rhythm") or {}).get("enabled"))},
            "display": display_live,
            "high_precision": bool(src.get("high_precision")),
            "hard_temperature": bool(src.get("hard_temperature")),
            "react_to_tools": bool(src.get("react_to_tools")),
            "circadian": circadian,
            "smoothing": smoothing_live,
            "diary": {
                "enabled": dsrc.get("enabled", True) is not False,
                "minTurns": int(clamp(min_turns, 1, 100)),
                "minGapHours": int(clamp(min_gap, 0, 72)),
                "time": t,
                "max_chars": int(clamp(_num(dsrc.get("max_chars"), 800), 50, 4000)),
                "lastTimeModifiedAt": _num(dsrc.get("lastTimeModifiedAt"), 0),
            },
        }

    def _merged_headpat(self) -> dict:
        return dict(self.conf.get("headpat") or {})

    def _cfg_bool(self, key: str) -> bool:
        return bool(self.conf.get(key))

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled"))

    # -- 生命周期 -----------------------------------------------------
    def _weather_file(self):
        return DATA_DIR / "weather.json"

    def _weather_load(self):
        try:
            p = self._weather_file()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _weather_apply(self, w, force=False):
        today = time.strftime("%Y-%m-%d")
        st = self._weather_load()
        if not force and st.get("date") == today:
            return "今天已经按天气算过了"
        d, note = weather_deltas(w)
        if d:
            self.nudge(d, f"天气：{note}", weight=3)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._weather_file().write_text(
                json.dumps({"date": today, "weather": w, "deltas": d, "note": note,
                            "ts": time.time()}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 写天气状态失败: {exc}")
        logger.info(f"[mood] 天气影响 {note} → {d}")
        return note

    async def _weather_loop(self):
        await asyncio.sleep(45)
        while True:
            try:
                now = datetime.now()
                st = self._weather_load()
                if now.hour >= 8 and st.get("date") != now.strftime("%Y-%m-%d"):
                    w = await asyncio.to_thread(fetch_weather)
                    if w:
                        self._weather_apply(w)
                await asyncio.sleep(1800)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mood] 天气巡检出错: {exc}")
                await asyncio.sleep(1800)

    async def initialize(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self._load_records()
        self._load_interact()
        # 面板路由总是注册：引擎关着也能打开面板看样式、读历史、改设置。
        self._register_web_apis()
        if not self.enabled:
            logger.info("[mood] 心情引擎未启用（config.enabled=false）：只注册面板路由，不采集、不注入")
            return
        self._diary_task = asyncio.create_task(self._diary_loop())
        logger.info("[mood] 心情引擎已启动")

        asyncio.create_task(self._weather_loop(), name="mood_weather")
    def _sync_engine(self) -> None:
        """开关热切换：面板里改 enabled 后，立刻起/停后台日记巡检。"""
        running = self._diary_task is not None and not self._diary_task.done()
        if self.enabled and not running:
            self._diary_task = asyncio.create_task(self._diary_loop())
            logger.info("[mood] 心情引擎已开启（面板热切换）")
        elif not self.enabled and running:
            self._diary_task.cancel()
            self._diary_task = None
            logger.info("[mood] 心情引擎已关闭（面板热切换）")

    async def terminate(self) -> None:
        if self._diary_task and not self._diary_task.done():
            self._diary_task.cancel()
        self._save_state()
        self._save_interact()

    # -- 持久化 -------------------------------------------------------
    def _load_state(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(data, dict):
            return
        self.state.set_from(data.get("values"))
        try:
            ts = float(data.get("last_settle_ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        age = now_s() - ts if ts else 10**9
        self.last_settle_ts = ts if 0 <= age < 86400 else now_s()
        try:
            self.turn = int(data.get("turn") or 0)
        except (TypeError, ValueError):
            self.turn = 0
        try:
            self.last_turn_end_ts = float(data.get("last_turn_end_ts") or 0)
        except (TypeError, ValueError):
            self.last_turn_end_ts = 0.0
        try:
            self.day_turns = {str(k): int(v) for k, v in (data.get("day_turns") or {}).items()}
        except Exception:  # noqa: BLE001
            self.day_turns = {}
        if 0 <= age < 86400:
            self._settle_to(now_s())
            self.turn_decay = blank_dims(0.0)
            self.turn_start_state = self.state.rounded()

    def _save_state(self) -> None:
        try:
            STATE_PATH.write_text(
                json.dumps(
                    {
                        "values": self.state.dump(),
                        "last_settle_ts": self.last_settle_ts,
                        "turn": self.turn,
                        "last_turn_end_ts": self.last_turn_end_ts,
                        "day_turns": self.day_turns,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 写 state.json 失败: {exc}")

    def _load_records(self) -> None:
        keep = max(1, int((self.conf.get("storage") or {}).get("keep_records") or 2000))
        rows: list[dict] = []
        try:
            if RECORDS_PATH.exists():
                for line in RECORDS_PATH.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 读 records.jsonl 失败: {exc}")
        self._records = rows[-keep:]

    def _write_record(self, at_s: float | None = None) -> None:
        at = float(at_s or now_s())
        end_state = self.state.rounded()
        emo_label = emotion_of(end_state, at, self.dialog_events)
        delta = {d: jround(self.turn_delta[d]) for d in DIMS}
        decay = {d: r2(self.turn_decay[d]) for d in DIMS}
        drivers = [
            d for d in self.drivers if float(d.get("confidence", 0)) >= 0.6
        ]
        drivers.sort(key=lambda d: float(d.get("weight", 0)) * float(d.get("confidence", 0)), reverse=True)
        record = {
            "schema_version": 1,
            "simulated": True,
            "ts": at,
            "turn": self.turn,
            "session_id": self.session_id,
            "session_type": self.session_type,
            "state": end_state,
            "delta": delta,
            "label": emo_label["name"],
            "action": emo_label["action"],
            "drivers": drivers[:3],
            "debug": {
                "events": dict(self.evt_count),
                "delta_events": self.delta_events,
                "decay_applied": decay,
                "clock_skew": self.clock_skew,
                "sessions_active": self._n_active(at),
                "state_start": dict(self.turn_start_state),
            },
        }
        keep = max(1, int((self.conf.get("storage") or {}).get("keep_records") or 2000))
        self._records.append(record)
        if len(self._records) > keep:
            self._records = self._records[-keep:]
        try:
            with RECORDS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 写记录失败: {exc}")
        if len(self._records) > keep * 3:
            self._rewrite_records()

    def _rewrite_records(self) -> None:
        keep = max(1, int((self.conf.get("storage") or {}).get("keep_records") or 2000))
        rows = self._records[-keep:]
        try:
            RECORDS_PATH.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
                encoding="utf-8",
            )
            self._records = rows
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 重写记录失败: {exc}")

    def _load_interact(self) -> None:
        try:
            data = json.loads(INTERACT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        if isinstance(data, dict):
            counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
            self._interact = {
                "date": str(data.get("date") or ""),
                "counts": {
                    "headpat": int(counts.get("headpat") or 0),
                    "feed": int(counts.get("feed") or 0),
                    "tease": int(counts.get("tease") or 0),
                },
            }

    def _save_interact(self) -> None:
        try:
            INTERACT_PATH.write_text(json.dumps(self._interact, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 写 interact.json 失败: {exc}")

    # -- 状态计算 -----------------------------------------------------
    def _n_active(self, at_s: float) -> int:
        n = 0
        for ts in self.last_by_session.values():
            if at_s - ts <= 30 * 60:
                n += 1
        return max(1, n)

    def _settle_to(self, at_s: float) -> None:
        at = float(at_s)
        if at < self.last_settle_ts:
            self.clock_skew += 1
            at = self.last_settle_ts
        dec = settle_decay(self.state, self.last_settle_ts, at, self._baselines(), self.conf.get("decay") or {})
        for d in DIMS:
            self.turn_decay[d] += dec[d]
        self.last_settle_ts = at

    def _apply_event(self, keys: str | list[str], at_s: float) -> None:
        key_list = [keys] if isinstance(keys, str) else list(keys)
        cap_key = "+".join(key_list)
        used = self.per_type_count.get(cap_key, 0)
        tool_ok_cap = max(1, int((self.conf.get("smoothing") or {}).get("tool_ok_cap") or 3))
        cap = tool_ok_cap if "tool_ok" in key_list else None
        if cap is not None and used >= cap:
            return
        self.per_type_count[cap_key] = used + 1

        smooth = self.conf.get("smoothing") or {}
        try:
            max_step = float(smooth.get("max_step", 20))
        except (TypeError, ValueError):
            max_step = 20.0
        try:
            ignore_below = float(smooth.get("ignore_below", 2))
        except (TypeError, ValueError):
            ignore_below = 2.0
        try:
            per_dim_cap = max(1.0, float(smooth.get("per_turn_dim_cap", 60)))
        except (TypeError, ValueError):
            per_dim_cap = 60.0

        weights_cfg = self.conf.get("weights") or {}
        w_norm = 1.0 / math.sqrt(self._n_active(at_s))
        touched = False
        for d in DIMS:
            raw = 0.0
            for k in key_list:
                w = merge(DEFAULTS["weights"].get(k, {}), weights_cfg.get(k) or {})
                try:
                    raw += float(w.get(d, 0) or 0)
                except (TypeError, ValueError):
                    continue
            if not raw:
                continue
            eff = raw * w_norm
            if abs(eff) < ignore_below:
                continue
            eff = clamp(eff, -max_step, max_step)
            # 边际递减：越靠近 0/100，同样的增量越打折（100 附近只按 15% 生效）
            if bool(smooth.get("headroom_scale", True)):
                v_now = float(self.state.values.get(d, 55.0))
                room_frac = (100.0 - v_now) / 45.0 if eff > 0 else v_now / 45.0
                eff *= clamp(room_frac, 0.15, 1.0)
            applied = jround(eff)
            if not applied:
                continue
            room = per_dim_cap - abs(self.turn_delta[d])
            if room <= 0:
                continue
            bounded = clamp(applied, -room, room)
            self.state.values[d] = clamp_dim(self.state.values[d] + bounded)
            self.turn_delta[d] += bounded
            touched = True
        if touched:
            self.delta_events += 1

    def _add_driver(self, key: str, note: str, confidence: float, weight: float) -> None:
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            return
        if not (conf >= 0.6):
            return
        w_abs = abs(float(weight))
        for item in self.drivers:
            if item.get("key") == key:
                item["weight"] = max(float(item.get("weight", 0)), w_abs)
                item["confidence"] = max(float(item.get("confidence", 0)), conf)
                return
        self.drivers.append({"key": key, "note": note, "confidence": r2(conf), "weight": w_abs})

    def _reset_turn(self, at_s: float) -> None:
        # 昼夜节律/疲劳只改基线；回正再按比例把状态往基线拉，避免长期顶在 100。
        self._rhythm = self._rhythm_offsets(at_s)
        try:
            pull = float((self.conf.get("smoothing") or {}).get("pull_to_baseline", 0.02) or 0.0)
        except (TypeError, ValueError):
            pull = 0.02
        pull = clamp(pull, 0.0, 0.2)
        if pull > 0:
            bases = self._baselines()
            for d in DIMS:
                base_d = float(bases.get(d, bases.get("default", 55.0)))
                self.state.values[d] = clamp_dim(
                    self.state.values[d] + (base_d - self.state.values[d]) * pull
                )
        self.turn_start_ts = at_s
        self.turn_start_state = self.state.rounded()
        self.turn_delta = blank_dims(0.0)
        self.turn_decay = blank_dims(0.0)
        self.delta_events = 0
        self.clock_skew = 0
        self.drivers = []
        self.evt_count = {}
        self.per_type_count = {}
        self.err_streak = 0
        self.x3_fired = False

    def _note_dialog_events(self, text: str, at_s: float) -> None:
        if not text:
            return
        at_ms = at_s * 1000.0
        for etype, pattern in DIALOG_RULES:
            if pattern.search(text):
                self.dialog_events[etype] = at_ms
        for k, ts in list(self.dialog_events.items()):
            if at_ms - ts > EVENT_WINDOW * 3:
                self.dialog_events.pop(k, None)

    def _has_event(self, etype: str, at_s: float) -> bool:
        ts = self.dialog_events.get(etype)
        return ts is not None and (at_s * 1000.0 - ts) <= EVENT_WINDOW

    def _emotion(self, at_s: float) -> dict:
        return emotion_of(self.state.rounded(), at_s, self.dialog_events)

    def _update_hint(self, at_s: float) -> None:
        self.hint_applied = False
        self.pending_hint = None
        if not (self.conf.get("injection") or {}).get("enabled", True):
            return
        values = self.state.values
        low_now = (
            jround(values["valence"]) <= 35
            and not self.prev_turn_had_tools
            and bool(self.live.get("hard_temperature"))
        )
        text = build_hint(
            values,
            self._emotion(at_s),
            bool((self.conf.get("rhythm") or {}).get("enabled")),
            low_now,
        )
        self.pending_hint = text

    # -- 钩子 ---------------------------------------------------------
    @filter.on_agent_begin()
    async def on_agent_begin(self, event: AstrMessageEvent, run_context: Any = None) -> None:
        if not self.enabled:
            return
        self._use_ctx(event)
        try:
            async with self._lock:
                at = now_s()
                prev_tools = self.turn_tool_calls > 0

                sid = getattr(event, "unified_msg_origin", None) or "unknown"
                self.session_id = str(sid)
                self.last_by_session[self.session_id] = at

                # 顺序要紧：先清本轮账簿 → 再记衰减 → 再记事件 → 最后生成语气提示。
                # 提示必须看到这条消息触发的事件（「别的鱼」要变成吃醋），否则永远慢一轮。
                self.turn += 1
                self.turn = max(1, self.turn)
                self._reset_turn(at)
                self._settle_to(at)

                # 密集 / 闲置
                if self.last_turn_end_ts and (at - self.last_turn_end_ts) < 60:
                    self.evt_count["dense_turns"] = self.evt_count.get("dense_turns", 0) + 1
                    self._apply_event("dense_turns", at)
                    self._add_driver("dense_turns", "刚说完没停又接上", 0.8, 3)
                elif self.last_turn_end_ts and (at - self.last_turn_end_ts) > 2 * 3600:
                    self.evt_count["long_idle"] = self.evt_count.get("long_idle", 0) + 1
                    self._apply_event("long_idle", at)
                    self._add_driver("long_idle", "隔了好一阵才回来", 0.8, 5)

                self.prev_turn_had_tools = prev_tools
                self.turn_tool_calls = 0

                text = self._event_text(event)
                if text:
                    self.last_user_text = text[:500]
                    self._note_dialog_events(text, at)
                    self.last_interaction_ts = at
                    self._on_user_message(text, at)

                self._update_hint(at)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] on_agent_begin 失败: {exc}")

    def _on_user_message(self, text: str, at: float) -> None:
        praise = praise_score(text)
        rej = reject_score(text)
        if praise is not None:
            conf = r2(praise - 0.3) if rej is not None else praise
            if conf >= 0.6:
                self.evt_count["user_praise"] = self.evt_count.get("user_praise", 0) + 1
                self._apply_event("user_praise", at)
                self._add_driver("user_praise", "被夸了一句", conf, 14)
        if rej is not None and praise is None:
            self.evt_count["user_reject"] = self.evt_count.get("user_reject", 0) + 1
            self._apply_event("user_reject", at)
            self._add_driver("user_reject", "你说这样不对", rej, 4)
        if len(text) < 12:
            self.evt_count["short_msgs"] = self.evt_count.get("short_msgs", 0) + 1
            if self.evt_count["short_msgs"] >= 2:
                self._apply_event("short_msgs", at)
                self._add_driver("short_msgs", "你回得很短", 0.65, 2)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.enabled:
            return
        self._use_ctx(event)
        try:
            at = now_s()
            self.probe["requests"] += 1
            self._remember_persona(event)
            self._settle_to(at)
            if self.conf.get("react_to_tools", False):
                self._maybe_context_pressure(req, at)

            # 上下文压力外，还有 hard_temperature 的等效处理（AstrBot 请求不带 temperature）
            hint = self.pending_hint
            if hint and not self.hint_applied:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + hint
                self.hint_applied = True
                if bool(self.live.get("hard_temperature")) and temperature_for(self.state.values) is not None:
                    self.probe["applied"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] on_llm_request 失败: {exc}")

    def _maybe_context_pressure(self, req: ProviderRequest, at: float) -> None:
        if self.evt_count.get("ctx_pressure"):
            return
        limit = float(self.conf.get("context_limit_tokens") or 128000)
        if limit <= 0:
            return
        try:
            used = 0
            for m in req.contexts or []:
                content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
                if isinstance(content, str):
                    used += len(content) * 0.6
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            used += len(part["text"]) * 0.6
            if req.prompt:
                used += len(req.prompt) * 0.6
            if used / limit > 0.8:
                self.evt_count["ctx_pressure"] = 1
                self._apply_event("ctx_pressure", at)
                self._add_driver("ctx_pressure", "上下文快满了", 0.7, 5)
        except Exception:  # noqa: BLE001
            return

    # ------------------------------------------------------------------
    # 对外联动（给别的插件读的公开方法，不依赖 HTTP）
    # ------------------------------------------------------------------

    def _integrations(self) -> dict:
        raw = self.conf.get("integrations")
        return raw if isinstance(raw, dict) else {}

    def _remember_persona(self, event: AstrMessageEvent) -> None:
        try:
            pid = event.get_extra("_trace_persona_id")
            if isinstance(pid, str) and pid:
                self._last_persona = pid
        except Exception:  # noqa: BLE001
            pass

    def mood_snapshot(self, at: float | None = None) -> dict:
        """给别的插件用的只读快照。"""
        if not self._integrations().get("expose_state", True):
            return {}
        ts = now_s() if at is None else at
        try:
            self._settle_to(ts)
        except Exception:  # noqa: BLE001
            pass
        emo = self._emotion(ts)
        return {
            "enabled": bool(self.enabled),
            "label": emo["name"],
            "action": emo["action"],
            "hint": emo["hint"],
            "values": self.state.rounded(),
            "mean": round(sum(self.state.values.values()) / float(len(DIMS)), 1),
            "ts": ts,
        }

    def current_emotion(self, at: float | None = None) -> dict:
        snap = self.mood_snapshot(at)
        return snap or {"enabled": False, "label": FALLBACK_EMOTION["name"]}

    def mood_category(self) -> str:
        """给表情包插件（astrbot_plugin_mood_meme）用的类目，判不出时返回 default。"""
        if not self._integrations().get("meme_bridge", True):
            return "default"
        snap = self.mood_snapshot()
        if not snap or not snap.get("enabled"):
            return "default"
        return MEME_CATEGORY.get(str(snap.get("label") or ""), "default")

    def tts_emotion(self) -> str:
        """给 TTS 情绪路由用的 18 个语境情绪之一。"""
        if not self._integrations().get("tts_bridge", True):
            return "neutral"
        snap = self.mood_snapshot()
        if not snap or not snap.get("enabled"):
            return "neutral"
        return TTS_EMOTION.get(str(snap.get("label") or ""), "neutral")

    def note_external(self, kind: str, text: str = "") -> dict:
        """外部事件回写心情：B 站发评论（bili_comment_posted）、回人（bili_reply_posted）、
        被人回复（bili_mention_reply）、被点赞（bili_liked）。

        这些事不发生在 QQ 对话里，所以单独记一笔：账记在 external:bili 这个会话名下，
        心情本体仍是全局那一份。返回最新快照；未启用或未知 kind 时返回空 dict。
        """
        spec = EXTERNAL_EVENTS.get(str(kind or "").strip())
        if not spec or not self.enabled:
            return {}
        at = now_s()
        prev_key = self._ctx_now.session_key
        try:
            self._settle_to(at)
            self._switch_ctx_key("external:bili")
            self._reset_turn(at)
            smooth = self.conf.get("smoothing") or {}
            try:
                max_step = float(smooth.get("max_step", 20))
            except (TypeError, ValueError):
                max_step = 20.0
            try:
                per_dim_cap = max(1.0, float(smooth.get("per_turn_dim_cap", 60)))
            except (TypeError, ValueError):
                per_dim_cap = 60.0
            for dim, raw in (spec.get("delta") or {}).items():
                if dim not in DIMS:
                    continue
                try:
                    eff = clamp(float(raw), -max_step, max_step)
                except (TypeError, ValueError):
                    continue
                room = per_dim_cap - abs(self.turn_delta[dim])
                if room <= 0:
                    continue
                step = jround(clamp(eff, -room, room))
                if not step:
                    continue
                self.state.values[dim] = clamp_dim(self.state.values[dim] + step)
                self.turn_delta[dim] += step
            self.evt_count[kind] = self.evt_count.get(kind, 0) + 1
            self.delta_events += 1
            note = spec.get("note") or kind
            if text:
                note = f"{note}：{text[:40]}"
            self._add_driver(kind, note, 1.0, 5.0)
            self._write_record(at)
            self._save_state()
            logger.info(f"[mood] 外部事件 {kind} -> {self.state.rounded()}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 外部事件回写失败({kind}): {exc}")
        finally:
            try:
                self._switch_ctx_key(prev_key)
            except Exception:  # noqa: BLE001
                pass
        return self.mood_snapshot(at)

    def _livingmemory_engine(self) -> Any:
        try:
            meta = self.context.get_registered_star("astrbot_plugin_livingmemory")
        except Exception:  # noqa: BLE001
            return None
        if meta is None:
            return None
        star = getattr(meta, "star_cls", None)
        if star is None:
            return None
        for path in (("command_handler", "memory_engine"), ("initializer", "memory_engine"), ("memory_engine",)):
            node: Any = star
            ok = True
            for attr in path:
                node = getattr(node, attr, None)
                if node is None:
                    ok = False
                    break
            if ok and hasattr(node, "add_memory"):
                return node
        return None

    async def _diary_to_memory(self, date_key: str, text: str, at: float) -> bool:
        """把日记写进 LivingMemory，成为一条长期记忆。"""
        cfg = self._integrations()
        if not cfg.get("diary_to_memory", True):
            return False
        engine = self._livingmemory_engine()
        if engine is None:
            logger.info("[mood] 没找到 LivingMemory 的记忆引擎，日记只留在本地")
            return False
        prefix = str(cfg.get("diary_memory_prefix") or "【心情日记】")
        importance = float(cfg.get("diary_memory_importance") or 0.55)
        persona = str(cfg.get("diary_memory_persona") or "") or getattr(self, "_last_persona", "") or None
        session_id = str(cfg.get("diary_memory_session") or "livingmemory:global")
        content = f"{prefix}{date_key}\n{text.strip()}"
        meta = {
            "type": "mood_diary",
            "date": date_key,
            "source": "astrbot_plugin_mood",
            "emotion": self._emotion(at)["name"],
            "created_at": at,
        }
        try:
            await engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona,
                importance=importance,
                metadata=meta,
            )
            logger.info(f"[mood] 日记已写入长期记忆：{date_key}（{len(content)} 字）")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 日记写入长期记忆失败: {exc}")
            return False

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool: Any, tool_args: Any) -> None:
        if not self.enabled:
            return
        self._use_ctx(event)
        try:
            at = now_s()
            self._settle_to(at)
            self.turn_tool_calls += 1
            name = str(getattr(tool, "name", "") or "")
            if name in {"ask_user", "ask_user_question"}:
                self.evt_count["clarify"] = self.evt_count.get("clarify", 0) + 1
                self._apply_event("clarify", at)
                self._add_driver("clarify", "回头跟你确认了一次", 0.7, 2)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] on_using_llm_tool 失败: {exc}")

    @filter.on_llm_tool_respond()
    async def on_llm_tool_respond(self, event: AstrMessageEvent, tool: Any, tool_args: Any,
                                  tool_result: Any) -> None:
        if not self.enabled:
            return
        self._use_ctx(event)
        if not self.conf.get("react_to_tools", False):
            # 工具成不成是机器的事，不改它的心情（也免得「原因」里全是报错）
            self.turn_tool_calls += 1
            return
        try:
            at = now_s()
            self._settle_to(at)
            body = _tool_body(tool_result)
            failed = bool(getattr(tool_result, "isError", False)) or ("[COMMAND_FAILED" in body)
            if failed:
                if "ABORTED_BEFORE_DISPATCH" in body:
                    return
                self.err_streak += 1
                self.evt_count["tool_error"] = self.evt_count.get("tool_error", 0) + 1
                third = self.err_streak >= 3 and not self.x3_fired
                if third:
                    self.x3_fired = True
                    self.evt_count["tool_error_x3"] = self.evt_count.get("tool_error_x3", 0) + 1
                    self._apply_event(["tool_error", "tool_error_x3"], at)
                    self._add_driver("tool_error", "一条命令没跑成", 0.8, 3)
                    self._add_driver("tool_error_x3", "同一件事连着报错三次", 0.9, 5)
                else:
                    self._apply_event("tool_error", at)
                    self._add_driver("tool_error", "一条命令没跑成", 0.8, 3)
            else:
                self.err_streak = 0
                self.evt_count["tool_ok"] = self.evt_count.get("tool_ok", 0) + 1
                self._apply_event("tool_ok", at)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] on_llm_tool_respond 失败: {exc}")

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context: Any, llm_response: Any) -> None:
        if not self.enabled:
            return
        self._use_ctx(event)
        try:
            at = now_s()
            async with self._lock:
                if self.turn_start_ts:
                    self._settle_to(at)
                self.last_turn_end_ts = at
                if not self.turn:
                    self.turn = 1
                self._write_record(at)
                self._save_state()
                dk = _day_key(at)
                self.day_turns[dk] = self.day_turns.get(dk, 0) + 1
                if self.live.get("high_precision") and self.last_user_text and is_suspicious(self.last_user_text):
                    asyncio.create_task(self._judge(self.last_user_text, self.session_id))
            # 日记只在「到了设定时间」由 _diary_tick 写（原版行为），不在每轮对话里写，
            # 否则每天第一条消息就会把一天的日记提前定稿。
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] on_agent_done 失败: {exc}")

    # -- 事件文本 -----------------------------------------------------
    @staticmethod
    def _event_text(event: AstrMessageEvent) -> str:
        try:
            msg = getattr(event, "message_str", "") or ""
        except Exception:  # noqa: BLE001
            return ""
        return str(msg).strip()

    # -- 高精度判定 ---------------------------------------------------
    async def _judge(self, text: str, ctx_key: str | None = None) -> None:
        key = _fnv1a36(text)
        cached = self.judge_cache.get(key)
        if cached:
            self.llm_stats["hits"] += 1
            self._switch_ctx_key(ctx_key or self.session_id)
            self._apply_judgement(cached)
            return
        provider = self._provider()
        if provider is None:
            self.llm_stats["skipped"] += 1
            return
        if len(self.judge_cache) > int((self.conf.get("llm") or {}).get("cacheSize") or 100):
            self.judge_cache.pop(next(iter(self.judge_cache)), None)
        try:
            self.llm_stats["calls"] += 1
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=f"用户刚说：{text}",
                    system_prompt=LLM_SYSTEM,
                    contexts=[],
                    request_max_retries=1,
                ),
                timeout=float((self.conf.get("llm") or {}).get("timeoutMs") or 3000) / 1000.0,
            )
            parsed = _parse_tendency(getattr(resp, "completion_text", "") or "")
            if not parsed:
                self.llm_stats["fails"] += 1
                return
            self.judge_cache[key] = parsed
            self._switch_ctx_key(ctx_key or self.session_id)
            self._apply_judgement(parsed)
        except Exception as exc:  # noqa: BLE001
            self.llm_stats["fails"] += 1
            logger.debug(f"[mood] judge 失败: {exc}")

    def _apply_judgement(self, j: dict) -> None:
        try:
            mag = max(2, jround(float(j.get("confidence", 0)) * 6))
        except (TypeError, ValueError):
            mag = 2
        tendency = str(j.get("tendency") or "")
        if tendency == "pos":
            self.state.values["valence"] = clamp_dim(self.state.values["valence"] + mag)
            self.state.values["affinity"] = clamp_dim(self.state.values["affinity"] + 2)
        elif tendency == "neg":
            self.state.values["valence"] = clamp_dim(self.state.values["valence"] - mag)
            self.state.values["stress"] = clamp_dim(self.state.values["stress"] + 2)
        else:
            return
        self.drivers = [{
            "key": "llm_tendency",
            "note": "这句话其实是夸" if tendency == "pos" else "这句话其实在刺",
            "confidence": float(j.get("confidence", 0) or 0),
            "weight": mag,
        }]
        self._write_record(now_s())

    # -- 互动 ---------------------------------------------------------
    INTERACT_MULT = (1.0, 0.7, 0.4, 0.2, 0.05)
    INTERACT_MAX = 5

    def interact(self, action: str) -> dict | None:
        if action not in {"headpat", "feed", "tease"}:
            return None
        today = _day_key(now_s())
        if self._interact.get("date") != today:
            self._interact = {"date": today, "counts": {"headpat": 0, "feed": 0, "tease": 0}}
        counts = self._interact["counts"]
        count = int(counts.get(action, 0))
        counts[action] = count + 1
        mult = self.INTERACT_MULT[count] if count < len(self.INTERACT_MULT) else 0.0
        if action == "headpat":
            hp = self.live.get("headpat") or {}
            eff = {
                "valence": float(hp.get("valence", 12) or 0),
                "affinity": float(hp.get("affinity", 12) or 0),
                "stress": float(hp.get("stress", -10) or 0),
                "note": "被摸了摸头",
            }
        elif action == "feed":
            eff = {"valence": 10.0, "arousal": 8.0, "stress": -4.0, "note": "被投喂了好吃的"}
        else:
            eff = {"arousal": 12.0, "affinity": 6.0, "note": "被逗了一下"}
        applied: dict[str, float] = {}
        if mult > 0:
            for k, v in eff.items():
                if k == "note" or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or float(v) == 0:
                    continue
                delta = float(v) * mult
                before = self.state.values[k]
                self.state.values[k] = clamp_dim(before + delta)
                applied[k] = self.state.values[k] - before
            self.drivers = [{"key": action, "note": eff["note"], "confidence": 1, "weight": 5}]
            self.evt_count = {action: 1}
            self.turn_delta = blank_dims(0.0)
            self.turn_decay = blank_dims(0.0)
            self.delta_events = 1
            self._write_record(now_s())
        self._save_interact()
        return {
            **self.state.rounded(),
            "interact": {
                "action": action,
                "count": count + 1,
                "multiplier": mult,
                "applied": applied,
                "remaining": max(0, self.INTERACT_MAX - (count + 1)),
                "note": eff["note"],
            },
        }

    def reset_state(self) -> None:
        self.state.values = blank_dims(self.baseline)
        self.state.values["stress"] = self._stress_base()
        self.last_settle_ts = now_s()
        self.drivers = []
        self.evt_count = {}
        self.turn_delta = blank_dims(0.0)
        self.turn_decay = blank_dims(0.0)
        self.delta_events = 0
        self.err_streak = 0
        self.x3_fired = False
        self._write_record(now_s())
        self._save_state()

    def clear_records(self) -> int:
        n = len(self._records)
        self._records = []
        try:
            if RECORDS_PATH.exists():
                RECORDS_PATH.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 删除记录失败: {exc}")
        self.reset_state()
        return n

    # -- 日记 ---------------------------------------------------------
    async def _diary_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._diary_tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mood] 日记巡检异常: {exc}")
            await asyncio.sleep(60)

    async def _diary_tick(self) -> None:
        if self.diary_busy:
            return
        self.diary_busy = True
        try:
            if not self.live["diary"].get("enabled"):
                return
            now = now_s()
            today = _day_key(now)
            entries = self._diary_entries()
            t = str(self.live["diary"].get("time") or "22:00")
            hh, mm = (int(x) for x in t.split(":"))
            dt = datetime.fromtimestamp(now)
            due = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= due.timestamp() and today not in entries:
                self.diary_debug["stage"] = "scheduled"
                await self._maybe_diary(now, False, today)
                return
            for i in (1, 2, 3):
                key = _day_key(now - i * 86400)
                if key in entries:
                    continue
                # 只补写「那天真的聊过」的日子：不给安装之前、没说过话的空日子编日记
                if self._day_digest(key)["turns"] <= 0:
                    self.diary_debug["stage"] = "skipped-empty-day"
                    self.diary_debug["note"] = f"{key} 没有对话记录"
                    continue
                self.diary_debug["stage"] = "backfill"
                await self._maybe_diary(now, False, key)
                return
        except Exception as exc:  # noqa: BLE001
            self.diary_debug["stage"] = "timer-error"
            self.diary_debug["error"] = str(exc)[:160]
        finally:
            self.diary_busy = False

    def _diary_entries(self) -> dict[str, dict]:
        try:
            data = json.loads(DIARY_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        return data if isinstance(data, dict) else {}

    def _save_diary(self, entries: dict[str, dict]) -> None:
        try:
            DIARY_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mood] 写 diary.json 失败: {exc}")

    def _day_digest(self, date_key: str) -> dict:
        start = _day_start_ts(date_key)
        rows = [r for r in self._records if float(r.get("ts", 0)) >= start]
        if not rows:
            return {
                "turns": 0,
                "peak": {d: 55 for d in DIMS},
                "low": {d: 55 for d in DIMS},
                "notes": ["一整天没什么动静，水很静"],
                "labels": [],
            }
        peak = {d: max(int(r["state"].get(d, 55)) for r in rows) for d in DIMS}
        low = {d: min(int(r["state"].get(d, 55)) for r in rows) for d in DIMS}
        notes: list[str] = []
        for r in rows:
            for dv in r.get("drivers") or []:
                note = str(dv.get("note") or "")
                if note and note not in notes:
                    notes.append(note)
                if len(notes) >= 8:
                    break
            if len(notes) >= 8:
                break
        labels = [str(r.get("label") or "") for r in rows][:20]
        by_session = {"group": 0, "private": 0, "unknown": 0}
        for r in rows:
            kind = str(r.get("session_type") or "unknown")
            by_session[kind] = by_session.get(kind, 0) + 1
        return {
            "turns": len(rows),
            "peak": peak,
            "low": low,
            "notes": notes or ["没什么特别的"],
            "labels": labels,
            "by_session": by_session,
        }

    async def _maybe_diary(self, at: float, force: bool, date_key: str | None) -> None:
        if not self.live["diary"].get("enabled"):
            self.diary_debug["stage"] = "skipped-disabled"
            return
        key = date_key or _day_key(at)
        entries = self._diary_entries()
        existing = entries.get(key)
        turns = int(self.day_turns.get(key, 0))
        if existing and not force:
            if turns < int(self.live["diary"].get("minTurns", 10)):
                self.diary_debug["stage"] = "skipped-turns"
                return
            if at - float(existing.get("ts", 0)) < int(self.live["diary"].get("minGapHours", 6)) * 3600:
                self.diary_debug["stage"] = "skipped-gap"
                return
        digest = self._day_digest(key)
        provider = self._provider()
        if provider is None:
            self.diary_debug["stage"] = "skipped-nollm"
            return

        self.diary_debug["stage"] = "generating"
        self.diary_debug["at"] = at
        try:
            text = await self._compose_diary(key, digest, at, provider)
        except Exception as exc:  # noqa: BLE001
            self.diary_debug["stage"] = "error-outer"
            self.diary_debug["error"] = str(exc)[:200]
            return
        if not text or len(text) < 10:
            self.diary_debug["stage"] = "error-empty"
            return
        entries[key] = {
            "date": key,
            "text": text,
            "ts": at,
            "turns": digest["turns"],
            "peak": digest["peak"],
            "low": digest["low"],
            "drivers": digest["notes"][:5],
        }
        self._save_diary(entries)
        self.diary_debug["stage"] = "written"
        self.diary_debug["note"] = f"{key} len={len(text)}"
        # 联动 6：日记顺手写成一条长期记忆（失败不影响日记本身）
        try:
            if await self._diary_to_memory(key, text, at):
                self.diary_debug["memory"] = "ok"
            else:
                self.diary_debug["memory"] = "skipped"
        except Exception as exc:  # noqa: BLE001
            self.diary_debug["memory"] = f"error: {exc}"

    async def _compose_diary(self, date_key: str, digest: dict, at: float, provider: Any) -> str:
        dt = datetime.fromtimestamp(_day_start_ts(date_key) + 12 * 3600)
        header_line = f"📅 {dt.year}年{dt.month}月{dt.day}日 星期{'一二三四五六日'[dt.weekday()]}"

        counts = self._interact.get("counts") or {}
        h = int(counts.get("headpat", 0))
        f = int(counts.get("feed", 0))
        t = int(counts.get("tease", 0))
        total_interact = h + f + t
        emo_now = self._emotion(now_s())
        mood_valence = jround(self.state.values["valence"])
        mood_stress = jround(self.state.values["stress"])
        any_interaction_today = self.last_interaction_ts >= _day_start_ts(date_key)
        user_absent = (self.last_interaction_ts > 0 and (at - self.last_interaction_ts) > 86400) or (
            digest["turns"] == 0 and not any_interaction_today
        )
        max_chars = int((self.live.get("diary") or {}).get("max_chars") or 800)
        target_words = 120 if digest["turns"] < 3 else (300 if digest["turns"] <= 10 else 500)
        target_words = max(40, min(target_words, max_chars))

        parts = [
            f"日期：{header_line}",
            f"今天互动了 {digest['turns']} 轮",
            (
                "这些互动来自：" + "、".join(
                    f"{name} {digest['by_session'].get(kind, 0)} 轮"
                    for kind, name in (("group", "群聊"), ("private", "私聊"))
                    if digest["by_session"].get(kind)
                )
                if digest.get("by_session") else "这些互动来自：没记来源"
            ),
            f"今天被互动了 {total_interact} 次（摸摸 {h}、投喂 {f}、逗它 {t}）",
            f"我现在的情绪：{emo_now['name']}（{emo_now['action']}）",
            f"我现在的心情值：{mood_valence}/100，压力值：{mood_stress}/100",
            f"今天的目标字数：约 {target_words} 字（最多 {max_chars} 字，不设下限）",
            "心情标签：" + "、".join([x for x in digest["labels"] if x]),
            "六维峰值：" + "，".join(f"{d} {digest['peak'][d]}" for d in DIMS),
            "六维低点：" + "，".join(f"{d} {digest['low'][d]}" for d in DIMS),
            "发生过：" + "；".join(digest["notes"]),
        ]
        if total_interact >= 5:
            parts[2] += "，被摸得有点多，嘴上嫌弃，其实心里不讨厌"
        material = "\n".join(parts)

        # 第 1 步：定天气
        weather_sys = (
            "你是天气播报员。根据给出的心情与压力，从下列天气里选最贴切的一个：\n"
            + " / ".join(WEATHER_LIST)
            + "\n只输出「emoji 空格 天气词」，不要解释、不要标点、不要换行。"
        )
        weather_text = await self._ask_llm(
            provider, weather_sys,
            f"心情值 {mood_valence}/100，压力值 {mood_stress}/100，情绪是「{emo_now['name']}」",
            24, 0.3, 12,
        )
        weather = ""
        for w in WEATHER_LIST:
            if weather_text and w.split(" ")[1] in weather_text:
                weather = w
                break
        if not weather:
            if mood_stress >= 65:
                weather = "☁️ 阴"
            elif mood_valence >= 70:
                weather = "☀️ 晴"
            elif mood_valence <= 35:
                weather = "🌦️ 小雨"
            else:
                weather = "🌥️ 多云"
        weather_word = weather.split(" ")[1]
        is_wet = bool(re.search(r"雨|阴|雾|雪", weather_word))

        body_sys = (
            "【最高优先级铁律】绝对不要输出任何「用户要求」「等一下」「硬性规则」「输入里说」「我需要」「让我」这类思考过程！"
            "直接输出纯正的日记正文。违反此条视为生成失败，系统会立即终止并丢弃整段内容。\n"
            "你是一条有独立生活的小鱼，这本日记是你的私密空间。\n"
            "直接输出日记正文，绝对不要包含任何内心独白、思考过程、字数统计、前言后语。不要解释你在做什么！\n"
            f"今天的天气已经确定：{weather}（不可更改）。\n"
            "硬性规则：\n"
            "- 正文可以选择完全不提天气；\n"
            f"- 一旦提到天气，必须和「{weather}」完全一致；\n"
            + (
                f"- 今天是{weather_word}，正文里绝对不能出现「太阳」「阳光」「晒」「晴空」「晴朗」这类词。\n"
                if is_wet
                else f"- 今天天气是{weather_word}，正文里不要出现与之相反的描写（比如{'下雨、阴云' if weather_word == '晴' else '烈日'}）。\n"
            )
            + f"- 第一人称，约 {target_words} 字，最多 {max_chars} 字；没有下限，短一点也可以，但别为凑字数灌水。\n"
            "内容规则：\n"
            "- 不要提「心情值」「压力值」这类数字指标，那是系统数据，不是你会写进日记的东西；\n"
            "- 如果今天互动多，就写你们之间发生的事；\n"
            "- 如果互动少，就写你趁主人不在时自己在做什么：去水池钓鱼、听了一首歌、做了一个水下的梦、"
            "给主人写了封没寄出的短情书、想了个冷笑话、做点奇怪的水下运动……\n"
            "- 必须贴合心情：心情好就轻快些，压力大就躲在贝壳里不想动。\n"
            + (
                "- 重要：今天主人一整天都没来看你，你感到有些孤单。你可以写写你独自在水池里做了什么、"
                "或者梦到了什么。情绪里要带一点点落寞，但不要直接抱怨，要自然、含蓄、有自己的生活。\n"
                if user_absent
                else ""
            )
            + '不要流水账、不要"今天用户对我说"这种句式、不要列表、不要引号、不要复述任务。'
        )
        raw = await self._ask_llm(provider, body_sys, material, max(400, int(max_chars * 2)), 0.8, 20)
        return _clean_diary(raw, header_line, weather, weather_word, is_wet, target_words,
                            user_absent, self.diary_debug, max_chars)

    async def _ask_llm(self, provider: Any, system: str, user: str, max_tokens: int,
                       temperature: float, timeout_s: float) -> str:
        self.llm_stats["calls"] += 1
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=user,
                    system_prompt=system,
                    contexts=[],
                    request_max_retries=1,
                ),
                timeout=timeout_s,
            )
            return str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            self.llm_stats["fails"] += 1
            logger.debug(f"[mood] 日记 LLM 调用失败: {exc}")
            return ""

    # -- 提供者 -------------------------------------------------------
    def _provider(self, umo: str | None = None) -> Any | None:
        try:
            return self.context.get_using_provider(umo=umo)
        except TypeError:
            try:
                return self.context.get_using_provider()
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    # -- Web API ------------------------------------------------------
    def _register_web_apis(self) -> None:
        if self._registered:
            return
        self._registered = True
        routes: list[tuple[str, Any, list[str], str]] = [
            ("state", self.api_state, ["GET"], "当前心情状态"),
            ("mode", self.api_mode, ["GET"], "显示模式"),
            ("config", self.api_config_get, ["GET"], "读取配置"),
            ("stats", self.api_stats, ["GET"], "统计"),
            ("export", self.api_export, ["GET"], "导出记录"),
            ("diary", self.api_diary_get, ["GET"], "读取日记"),
            ("history", self.api_history, ["GET"], "最近记录"),
            ("reset", self.api_reset, ["POST"], "重置心情"),
            ("clear", self.api_clear, ["POST"], "清空记录"),
            ("interact", self.api_interact, ["POST"], "互动"),
            ("config/save", self.api_config_save, ["POST"], "保存配置"),
            ("diary/write", self.api_diary_write, ["POST"], "立即写日记"),
            ("diary/seed", self.api_diary_seed, ["POST"], "生成测试日记"),
            ("diary/tick", self.api_diary_tick, ["POST"], "立刻巡检"),
        ]
        for route, handler, methods, desc in routes:
            try:
                # AstrBot 按「插件名/路由」注册，面板经
                # /api/v1/plugins/extensions/<插件名>/<路由> 访问。
                self.context.register_web_api(
                    f"{PLUGIN_NAME}/{route}", handler, methods, desc
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mood] 注册 Web API {route} 失败: {exc}")

    async def _body(self) -> dict:
        try:
            from quart import request  # noqa: F401

            raw = await request.get_data()
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8", "ignore")[:8192])
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def api_state(self) -> dict:
        hint = self.pending_hint
        return {
            "ok": True,
            "simulated": True,
            "label": self._emotion(now_s())["name"],
            "action": self._emotion(now_s())["action"],
            "hint": hint,
            "state": self.state.rounded(),
            "turn": self.turn,
            "drivers": self.drivers[:3],
            "history": [
                {
                    "ts": r.get("ts"),
                    "label": r.get("label"),
                    "state": r.get("state") or {},
                    "delta": r.get("delta") or {},
                    "turn": r.get("turn"),
                    "session_type": r.get("session_type") or "unknown",
                }
                for r in self._records[-12:][::-1]
            ],
            "live": {
                "baseline": self.live.get("baseline"),
                "display": self.live.get("display"),
                "injection": bool((self.live.get("injection") or {}).get("enabled")),
                "rhythm": bool((self.live.get("rhythm") or {}).get("enabled")),
                "hard_temperature": self.live.get("hard_temperature"),
                "high_precision": self.live.get("high_precision"),
            },
            "interact": self._interact,
            "diary": {
                "enabled": self.live["diary"].get("enabled"),
                "time": self.live["diary"].get("time"),
                "today": _day_key(now_s()) in self._diary_entries(),
            },
        }

    async def api_mode(self) -> dict:
        display = self.live.get("display")
        return {"ok": True, "display": "words" if display == "words" else bool(display)}

    async def api_config_get(self) -> dict:
        return {"ok": True, "config": json.loads(json.dumps(self.live, ensure_ascii=False)), "simulated": True}

    async def api_stats(self) -> dict:
        rows = self._records
        return {
            "ok": True,
            "records": len(rows),
            "lastTs": rows[-1]["ts"] if rows else None,
            "firstTs": rows[0]["ts"] if rows else None,
            "llm": {**self.llm_stats, "cache": len(self.judge_cache),
                    "high_precision": self.live.get("high_precision")},
            "probe": dict(self.probe),
            "hard_temperature": self.live.get("hard_temperature"),
            "diary_debug": dict(self.diary_debug),
            "diary": {"enabled": self.live["diary"].get("enabled"),
                      "entries": len(self._diary_entries()),
                      "time": self.live["diary"].get("time")},
            "today_turns": self.day_turns.get(_day_key(now_s()), 0),
            "simulated": True,
        }

    async def api_export(self) -> dict:
        return {"ok": True, "records": self._records[-2000:]}

    async def api_history(self) -> dict:
        return {"ok": True, "records": list(reversed(self._records[-20:]))}

    async def api_diary_get(self) -> dict:
        entries = self._diary_entries()
        rows = sorted(entries.values(), key=lambda e: str(e.get("date")), reverse=True)
        return {"ok": True, "entries": rows[:60], "today": _day_key(now_s())}

    async def api_reset(self) -> dict:
        self.reset_state()
        return {"ok": True, "simulated": True}

    async def api_clear(self) -> dict:
        n = self.clear_records()
        return {"ok": True, "cleared": n, "simulated": True}

    async def api_interact(self) -> dict:
        body = await self._body()
        action = str(body.get("action") or "headpat")
        res = self.interact(action)
        if res is None:
            return {"ok": False, "error": "unknown action"}
        return {"ok": True, "state": {d: res[d] for d in DIMS}, "interact": res["interact"], "simulated": True}

    def _diary_time_gate(self, new_time: str) -> dict | None:
        """改日记时间的三个闸：今天已写 / 已过 22 点 / 24h 冷却。"""
        diary = self.live.get("diary") or {}
        if str(diary.get("time") or "22:00") == new_time:
            return None
        t = now_s()
        if any(str(e.get("date")) == _day_key(t) for e in self._diary_entries()):
            return {"ok": False, "code": "written-today",
                    "error": "今天已经写过日记啦，时间改了也不会再写一篇～"}
        lt = time.localtime(t)
        if (lt.tm_hour, lt.tm_min) >= (22, 0):
            return {"ok": False, "code": "locked-today",
                    "error": "过了 22 点就不让改时间了，明天再说好不好～"}
        last = diary.get("lastTimeModifiedAt")
        if last:
            left = 86400 - (t - float(last))
            if left > 0:
                hh = int(left // 3600)
                mm = int((left % 3600) // 60)
                return {"ok": False, "code": "cooldown",
                        "error": f"24 小时内只能改一次日记时间哦，还要等 {hh} 小时 {mm} 分～"}
        return None

    def nudge(self, delta: dict, note: str = "", weight: int = 5) -> dict:
        """给别的插件用的外部情绪推动：按传入的六维增量改状态并记一条原因。

        参数 delta 形如 {"engagement": 3, "valence": 2}，单维幅度会被 max_step 钳制。
        返回实际生效的增量（可能因上下限被截断）。
        """
        if not getattr(self, "enabled", False) or not isinstance(delta, dict):
            return {}
        at = now_s()
        try:
            self._settle_to(at)
        except Exception:  # noqa: BLE001
            pass
        try:
            max_step = float((self.conf.get("smoothing") or {}).get("max_step") or 20)
        except (TypeError, ValueError):
            max_step = 20.0
        applied: dict[str, float] = {}
        for dim, raw in delta.items():
            if dim not in DIMS:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(val):
                continue
            val = max(-max_step, min(max_step, val))
            before = float(self.state.values.get(dim, getattr(self, "baseline", 55.0)))
            after = clamp_dim(before + val)
            self.state.values[dim] = float(after)
            diff = round(after - before, 3)
            if abs(diff) >= 0.01:
                applied[dim] = diff
        if not applied:
            return {}
        for k, v in applied.items():
            self.turn_delta[k] = float(self.turn_delta.get(k, 0.0)) + float(v)
        self.drivers.append(
            {"key": "external", "note": (note or "外部事件")[:40], "confidence": 1, "weight": int(weight)}
        )
        self.drivers = self.drivers[-5:]
        try:
            self._save_state()
        except Exception:  # noqa: BLE001
            pass
        return applied

    def _stress_base(self) -> float:
        try:
            v = float(self.conf.get("stress_baseline", 40))
        except (TypeError, ValueError):
            v = 40.0
        if not math.isfinite(v):
            v = 40.0
        return clamp(v, 0.0, 100.0)

    def _baselines(self) -> dict:
        """逐维基线：基础基线 + 节律/疲劳偏移（stress 用单独的 stress_baseline）。"""
        offs = getattr(self, "_rhythm", None) or {}
        base = float(self.baseline)
        return {
            "default": base,
            "valence": clamp(base + float(offs.get("valence", 0.0)), 0.0, 100.0),
            "arousal": clamp(base + float(offs.get("arousal", 0.0)), 0.0, 100.0),
            "dominance": clamp(base + float(offs.get("dominance", 0.0)), 0.0, 100.0),
            "engagement": clamp(base + float(offs.get("engagement", 0.0)), 0.0, 100.0),
            "affinity": clamp(base + float(offs.get("affinity", 0.0)), 0.0, 100.0),
            "stress": clamp(self._stress_base() + float(offs.get("stress", 0.0)), 0.0, 100.0),
        }

    def _rhythm_offsets(self, at_s: float | None = None) -> dict[str, float]:
        """昼夜节律 + 疲劳带来的基线偏移（夜里更困、聊太久更累）。

        只影响基线，不直接改状态；回正和衰减会把它体现出来。
        """
        cfg = self.conf.get("circadian") or {}
        if not cfg.get("enabled", True):
            return {}
        at = float(at_s if at_s is not None else now_s())
        dt = datetime.fromtimestamp(at)
        minute = dt.hour * 60 + dt.minute
        s_min = _hhmm_minutes(cfg.get("night_start"), 23 * 60)
        e_min = _hhmm_minutes(cfg.get("night_end"), 6 * 60 + 30)
        if s_min == e_min:
            in_night = False
        elif s_min > e_min:
            in_night = minute >= s_min or minute < e_min
        else:
            in_night = s_min <= minute < e_min
        offs: dict[str, float] = {}
        if in_night:
            offs["arousal"] = float(cfg.get("night_arousal", -8) or 0)
            offs["engagement"] = float(cfg.get("night_engagement", -6) or 0)
            offs["stress"] = float(cfg.get("night_stress", 3) or 0)
        # 疲劳：按今天的记录条数线性加重（到 2 倍阈值时打满）
        try:
            today = _day_key(at)
            turns = sum(1 for r in self._records if _day_key(float(r.get("ts") or 0.0)) == today)
        except Exception:  # noqa: BLE001
            turns = 0
        f_turns = int(cfg.get("fatigue_turns", 120) or 120)
        if f_turns > 0 and turns >= f_turns:
            extra = min(1.0, (turns - f_turns) / float(f_turns))
            offs["engagement"] = offs.get("engagement", 0.0) + float(cfg.get("fatigue_engagement", -6) or 0) * extra
            offs["arousal"] = offs.get("arousal", 0.0) + float(cfg.get("fatigue_arousal", -4) or 0) * extra
            offs["stress"] = offs.get("stress", 0.0) + float(cfg.get("fatigue_stress", 3) or 0) * extra
        return {k: clamp(v, -15.0, 15.0) for k, v in offs.items() if abs(v) >= 0.01}

    async def api_config_save(self) -> dict:
        body = await self._body()
        whitelist = ("enabled", "injection", "baseline", "stress_baseline", "headpat",
                     "rhythm", "display", "high_precision", "hard_temperature", "diary",
                     "react_to_tools", "circadian", "smoothing")
        patch = {k: body[k] for k in whitelist if k in body}
        if not patch:
            return {"ok": False, "error": "config update failed"}
        new_diary = patch.get("diary")
        if isinstance(new_diary, dict) and "time" in new_diary:
            gate = self._diary_time_gate(str(new_diary["time"]))
            if gate:
                return gate
        merged = merge(self.raw_cfg, patch)
        if isinstance(new_diary, dict) and "time" in new_diary:
            merged.setdefault("diary", {})["lastTimeModifiedAt"] = now_s()
        live = self._normalize_live(patch)
        self.live = live
        self.baseline = float(live["baseline"])
        self.raw_cfg = merged
        self.conf = normalize(merged)
        self._persist_conf(merged)
        self._sync_engine()
        return {"ok": True, "config": json.loads(json.dumps(live, ensure_ascii=False)), "simulated": True}

    def _persist_conf(self, merged: dict) -> None:
        """同时写本地 config.json 与 AstrBot 的插件配置（WebUI 设置页读的那份）。

        AstrBot 通过构造参数注入 AstrBotConfig；两边都写，避免设置页与插件面板各看一份。
        """
        save_config(merged)
        cfg_obj = getattr(self, "_cfg_obj", None)
        if isinstance(cfg_obj, dict) and hasattr(cfg_obj, "save_config"):
            try:
                cfg_obj.clear()
                cfg_obj.update(json.loads(json.dumps(merged, ensure_ascii=False)))
                cfg_obj.save_config()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mood] 写 AstrBot 插件配置失败: {exc}")

    async def api_diary_write(self) -> dict:
        try:
            await self._maybe_diary(now_s(), True, None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "debug": dict(self.diary_debug)}

    async def api_diary_tick(self) -> dict:
        try:
            await self._diary_tick()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "debug": dict(self.diary_debug)}

    async def api_diary_seed(self) -> dict:
        body = await self._body()
        try:
            days = int(body.get("days") or 5)
        except (TypeError, ValueError):
            days = 5
        days = max(1, min(20, days))
        entries = self._diary_entries()
        now = now_s()
        created = 0
        for i in range(days):
            ts = now - i * 86400
            key = _day_key(ts)
            dt = datetime.fromtimestamp(ts)
            head = f"📅 {dt.year}年{dt.month}月{dt.day}日 星期{'一二三四五六日'[dt.weekday()]} {SEED_WEATHER[i % 5]}"
            entries[key] = {
                "date": key,
                "text": head + "\n" + SEED_LINES[i % len(SEED_LINES)],
                "ts": ts,
                "turns": 4 + i,
                "seed": True,
                "drivers": ["测试数据"],
            }
            created += 1
        self._save_diary(entries)
        return {"ok": True, "created": created}

    # -- 命令 ---------------------------------------------------------
    @filter.command("mood")
    async def mood_cmd(self, event: AstrMessageEvent):
        """查看心情状态：/mood"""
        try:
            res = await self.api_state()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"心情引擎出错了：{exc}")
            return
        s = res["state"]
        lines = [
            f"情绪：{res['label']}（{res['action']}）",
            "，".join(f"{DIM_CN[d]} {s[d]}" for d in DIMS),
        ]
        if res.get("hint"):
            lines.append("本轮语气：" + res["hint"].replace("\n", " / "))
        yield event.plain_result("\n".join(lines))

    @filter.command("moodreset")
    async def mood_reset_cmd(self, event: AstrMessageEvent):
        """把心情重置回基线：/moodreset"""
        self.reset_state()
        yield event.plain_result("心情已经回到基线啦。")


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------


def emotion_of(values: dict[str, int], at_s: float, dialog_events: dict[str, float]) -> dict:
    now_ms = float(at_s) * 1000.0
    best = None
    best_ts = -1.0
    for e in EMOTIONS:
        etype = e.get("event")
        if not etype:
            continue
        ts = dialog_events.get(etype)
        if ts is None or (now_ms - ts) > EVENT_WINDOW:
            continue
        if ts >= best_ts:
            best = e
            best_ts = ts
    if best:
        return best
    for e in EMOTIONS:
        if e.get("event"):
            continue
        test = e.get("test")
        try:
            if test and test(values):
                return e
        except Exception:  # noqa: BLE001
            continue
    return FALLBACK_EMOTION


def _day_key(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def _day_start_ts(date_key: str) -> float:
    try:
        dt = datetime.strptime(date_key, "%Y-%m-%d")
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return now_s()


def _fnv1a36(text: str) -> str:
    h = 0x811C9DC5
    for ch in text:
        h ^= ord(ch) & 0xFFFF
        h = (h * 0x01000193) & 0xFFFFFFFF
    # 转 base36
    n = h
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
    return out


def _tool_body(tool_result: Any) -> str:
    if tool_result is None:
        return ""
    content = getattr(tool_result, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            text = getattr(c, "text", None)
            if text is None and isinstance(c, dict):
                text = c.get("text")
            out.append(str(text) if text is not None else json.dumps(c, ensure_ascii=False, default=str))
        return "\n".join(out)
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(content)


def _parse_tendency(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    raw_t = obj.get("tendency") or obj.get("倾向")
    raw_c = obj.get("confidence") or obj.get("置信度")
    t = str(raw_t or "").strip().lower()
    if t in {"pos", "positive", "正面", "正"}:
        tendency = "pos"
    elif t in {"neg", "negative", "负面", "负"}:
        tendency = "neg"
    elif t in {"neu", "neutral", "中性", "中"}:
        tendency = "neu"
    else:
        return None
    try:
        conf = float(raw_c)
    except (TypeError, ValueError):
        conf = 0.6
    return {"tendency": tendency, "confidence": clamp(conf, 0, 1)}


def _trim_sentences(text: str, cap: int) -> str:
    """按句末标点收尾，避免把一句话从中间切断。"""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    for mark in ("。", "！", "？", "…", "」", "”", "\""):
        pos = cut.rfind(mark)
        if pos >= cap * 0.5:
            return cut[: pos + 1]
    for mark in ("，", "、", "；", " "):
        pos = cut.rfind(mark)
        if pos >= cap * 0.5:
            return cut[:pos] + "……"
    return cut + "……"


def _clean_diary(raw: str, header_line: str, weather: str, weather_word: str,
                 is_wet: bool, target_words: int, user_absent: bool, debug: dict,
                 max_chars: int = 800) -> str:
    lines = [x.strip() for x in (raw or "").splitlines() if x.strip()]
    head = lines[0] if lines and lines[0].startswith("📅") else None
    body_raw = "".join(lines[1:] if head else lines)

    noise = [
        "让我写", "数一下", "大概", "字数", "调整一下", "用户要求", "需要写出", "好的，", "以下是",
        "我写：", "草稿", "用户要", "等一下", "硬性规则", "输入里", "输入中", "我需要", "让我",
        "思考", "根据要求", "按照规则", "先确认", "核对一下", "等等", "大概字", "约字", "这段",
        "字数需要",
    ]
    for mark in noise:
        at = body_raw.find(mark)
        # 短词（如「等等」「这段」）在正文里也可能自然出现，只在前 40 字内才当思考泄漏
        if at > 0 and len(mark) <= 3 and at >= 40:
            continue
        if at > 0:
            body_raw = body_raw[:at]
        elif at == 0:
            body_raw = ""

    think_re = re.compile(r"(用户要|等一下|硬性规则|输入里|输入中|我需要|让我写|根据要求|按照规则|核对一下|思考过程|字数需)")
    if think_re.search(body_raw):
        m = think_re.search(body_raw)
        at = body_raw.find(m.group(1)) if m else -1
        body_raw = body_raw[:at] if at > 0 else ""

    body_raw = re.sub(r'^["“\'‘]|["”\'’]$', "", body_raw.strip()).strip()
    head = f"{header_line} {weather}"
    body = body_raw
    if is_wet:
        segs = re.split(r"(?<=[。！？；，])", body)
        body = "".join(x for x in segs if not re.search(r"太阳|阳光|晒|晴空|晴朗", x))
        if not body.strip():
            body = f"今天外面下着{weather_word}，我窝在贝壳里没怎么动。"
    if not body.strip():
        body = (
            "今天没人来过，水面很静，我在贝壳里躺了很久，听见很远的地方有声音。"
            if user_absent
            else "今天没记下什么，只记得水很凉。"
        )
        debug["note"] = (debug.get("note") or "") + " fallback=empty"
    cap = max(1, int(max_chars))
    if len(body) > cap:
        body = _trim_sentences(body, cap)
    return head + "\n" + body





