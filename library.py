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
