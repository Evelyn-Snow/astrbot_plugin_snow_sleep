"""机器人作息：00:00-07:00 进入休息，不回复、不处理、不被动读群消息。

实现为两层：
1. 事件守卫（最高优先级）：休息窗口内对非命令消息直接 stop_event，
   读空气、@ 唤醒、戳一戳、连续对话提升、图片处理全部不会触发；
2. LivingMemory 被动捕获同步：窗口内把 enable_full_group_capture 翻转为
   False（waking check 阶段的捕获早于事件拦截，需单独关闭），醒来恢复。

网页测试台（webchat）默认豁免，方便所有者随时调试。
"""

from __future__ import annotations

import asyncio
import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .library import in_sleep_window, is_command_text

NAME = "astrbot_plugin_snow_sleep"
_GUARD_PRIORITY = (1 << 31) - 2  # 仅次于 GCP 的命令标记器，先于一切消息处理


class SnowSleep(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self._sleep_applied = None  # None=未知, True/False=已同步的状态
        self._sync_task: asyncio.Task | None = None

    # ---------------- 配置 ----------------
    def _enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _in_window(self, now: datetime.datetime | None = None) -> bool:
        now = now or datetime.datetime.now()
        return in_sleep_window(now.hour * 60 + now.minute,
                               str(self.conf.get("sleep_start") or "00:00"),
                               str(self.conf.get("wake_time") or "07:00"))

    def _allow_commands(self) -> bool:
        return bool(self.conf.get("allow_commands", True))

    def _webchat_exempt(self) -> bool:
        return not bool(self.conf.get("include_webchat", False))

    # ---------------- LivingMemory 联动 ----------------
    def _set_lm_capture(self, value: bool) -> bool:
        """翻转 LivingMemory 的全群被动捕获（内存态，白天配置不受影响）。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_livingmemory")
            plugin = getattr(meta, "star_cls", None) if meta else None
            cm = getattr(plugin, "config_manager", None)
            raw = getattr(cm, "_config", None)
            section = raw.get("session_manager") if isinstance(raw, dict) else None
            if not isinstance(section, dict):
                return False
            section["enable_full_group_capture"] = value
            return True
        except Exception as exc:
            logger.warning(f"[{NAME}] 同步 LivingMemory 捕获开关失败: {exc}")
            return False

    def _apply_sleep_state(self, sleeping: bool) -> None:
        if not self.conf.get("suppress_memory_capture", True):
            return
        if self._sleep_applied == sleeping:
            return
        ok = self._set_lm_capture(not sleeping)
        self._sleep_applied = sleeping
        logger.info(
            f"[{NAME}] {'进入休息' if sleeping else '苏醒'}"
            f"（LivingMemory 被动捕获{'已暂停' if sleeping else '已恢复'}{'' if ok else '，同步失败'}）"
        )

    async def _sync_loop(self) -> None:
        while True:
            try:
                self._apply_sleep_state(self._enabled() and self._in_window())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[{NAME}] 作息同步失败: {exc}")
            await asyncio.sleep(60)

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        self._sleep_applied = None
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def terminate(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
            self._sync_task = None
        # 卸载/停用时恢复捕获，避免白天被动记忆失效。
        self._set_lm_capture(True)
        self._sleep_applied = None

    # ---------------- 事件守卫 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=_GUARD_PRIORITY)
    async def sleep_guard(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._in_window():
            return
        platform = event.get_platform_name()
        if platform == "webchat" and self._webchat_exempt():
            return
        if self._allow_commands() and is_command_text(event.message_str):
            return
        event.stop_event()
        logger.info(
            f"[{NAME}] 休息时间，已拦截 {platform} 消息"
            f"（发送者：{event.get_sender_name()}）"
        )

