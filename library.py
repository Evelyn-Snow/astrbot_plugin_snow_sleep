"""作息窗口的纯逻辑判断（不依赖 AstrBot，可单独测试）。"""

from __future__ import annotations

import re

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    """解析 HH:MM 为 (小时, 分钟)；非法输入返回默认值。"""
    match = _HHMM.match(str(value or "").strip())
    if not match:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return default
    return hour, minute


def to_minutes(hhmm: tuple[int, int]) -> int:
    return hhmm[0] * 60 + hhmm[1]


def in_sleep_window(now_minutes: int, start: str, end: str) -> bool:
    """当前分钟数是否处于睡眠窗口（支持跨午夜，如 00:00-07:00）。"""
    start_m = to_minutes(parse_hhmm(start, (0, 0)))
    end_m = to_minutes(parse_hhmm(end, (7, 0)))
    now_m = max(0, min(1440, int(now_minutes)))
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


def is_command_text(text: str) -> bool:
    """是否为命令消息（命令在休息时间仍然放行）。"""
    return str(text or "").strip().startswith(("/", "！"))


def minutes_until(start: str, now_minutes: int) -> int:
    """距下一次到达 start（HH:MM）还有多少分钟（0..1439，支持跨午夜）。"""
    start_m = to_minutes(parse_hhmm(start, (0, 0)))
    now_m = max(0, min(1440, int(now_minutes)))
    return (start_m - now_m) % 1440


def in_goodnight_window(
    now_minutes: int, sleep_start: str, window_minutes: int
) -> bool:
    """临睡道别窗口：距休息开始不足 window_minutes 分钟（且尚未到达）。"""
    until = minutes_until(sleep_start, now_minutes)
    return 0 <= until < max(1, int(window_minutes))


def is_recently_active(last_ts: float | None, now_ts: float, active_minutes: int) -> bool:
    """对方是否仍在聊天（最近 active_minutes 分钟内有过私聊消息）。"""
    if not last_ts:
        return False
    return (now_ts - last_ts) <= max(1, int(active_minutes)) * 60


# ---------------- 虚拟人生（astrbot_plugin_virtual_life）联动 ----------------

_HHMM_LEN = re.compile(r"^\d{1,2}:\d{2}$")


def _hhmm_minutes(raw: str) -> int:
    hour, minute = str(raw or "0:0").split(":")[:2]
    value = int(hour) * 60 + int(minute)
    return max(0, min(1440, value))


def pick_plan(plans_data: dict, today: str, persona_id: str = "") -> dict | None:
    """从虚拟人生 plans.json 内容中选出今天的日程。

    plans_data 为该文件反序列化后的 dict（plans 键下是 "{日期}::{persona}" 条目）。
    指定 persona_id 时优先匹配；否则仅当今天恰好只有一个日程时采用。
    """
    plans = plans_data.get("plans") if isinstance(plans_data, dict) else None
    if not isinstance(plans, dict):
        return None
    matching = [
        value
        for key, value in plans.items()
        if isinstance(value, dict) and str(key).startswith(f"{today}::")
    ]
    if not matching:
        return None
    if persona_id:
        for plan in matching:
            if str(plan.get("persona_id", "")) == persona_id:
                return plan
    return matching[0] if len(matching) == 1 else None


def sleep_start_from_plan(plan: dict, persona_id: str = "") -> str | None:
    """从日程条目里找睡眠时段的开始时间（HH:MM），取最长的睡眠槽。

    与虚拟人生自带的 sleep exception 选择逻辑一致；找不到返回 None。
    """
    if not isinstance(plan, dict):
        return None
    if persona_id and str(plan.get("persona_id", "")) != persona_id:
        return None
    timeline = plan.get("timeline")
    if not isinstance(timeline, list):
        return None
    best, best_len = None, -1
    for item in timeline:
        if not isinstance(item, dict) or item.get("state") != "sleep":
            continue
        start, end = str(item.get("start") or ""), str(item.get("end") or "")
        if not _HHMM_LEN.match(start) or not _HHMM_LEN.match(end):
            continue
        length = _hhmm_minutes(end) - _hhmm_minutes(start)
        if length > best_len:
            best, best_len = start, length
    return best
