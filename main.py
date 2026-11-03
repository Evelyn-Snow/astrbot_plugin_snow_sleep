"""小雪作息：00:00-07:00 进入休息，不回复、不处理、不被动读群消息。

实现为三层：
1. 事件守卫（最高优先级）：休息窗口内对非命令消息直接 stop_event，
   读空气、@ 唤醒、戳一戳、连续对话提升、图片处理全部不会触发；
2. LivingMemory 被动捕获同步：窗口内把 enable_full_group_capture 翻转为
   False（waking check 阶段的捕获早于事件拦截，需单独关闭），醒来恢复；
3. 临睡道别：休息开始前几分钟内仍在私聊的对象，会收到一句晚安道别。

网页测试台（webchat）默认豁免，方便所有者随时调试。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .library import (
    in_goodnight_window,
    in_sleep_window,
    is_command_text,
    is_recently_active,
    pick_plan,
    sleep_start_from_plan,
)

NAME = "astrbot_plugin_snow_sleep"
VLF_PLUGIN = "astrbot_plugin_virtual_life"
_GUARD_PRIORITY = (1 << 31) - 2  # 仅次于 GCP 的命令标记器，先于一切消息处理
_GOODNIGHT_FALLBACK = (
    "我先去睡啦，晚安，好梦～",
    "夜深了，我先下线休息咯，晚安！",
    "我去睡啦，你也别熬太晚，晚安～",
)


class SnowSleep(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self._sleep_applied = None  # None=未知, True/False=已同步的状态
        self._sync_task: asyncio.Task | None = None
        # umo -> (最后消息时间, 发送者昵称)：私聊活跃度，供临睡道别判断
        self._last_private: dict[str, tuple[float, str]] = {}
        self._goodnight_sent: dict[str, str] = {}  # umo -> 日期（每晚一次）
        # 虚拟人生联动：(读取时间, 当日虚拟休息点或 None)
        self._vlife_cache: tuple[float, str | None] = (0.0, None)
        self._vlife_logged = ""

    # ---------------- 配置 ----------------
    def _enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _vl_adapt(self) -> bool:
        return bool(self.conf.get("virtual_life_adapt", True))

    def _effective_sleep_start(self) -> str:
        """当晚实际休息点：配置值为基础；检测到虚拟人生且今天日程有睡眠槽时采用之。"""
        start = str(self.conf.get("sleep_start") or "00:00")
        if not self._vl_adapt():
            return start
        now = time.time()
        if now - self._vlife_cache[0] < 300:
            return self._vlife_cache[1] or start
        resolved = None
        try:
            if self.context.get_registered_star(VLF_PLUGIN):
                data_dir = Path(
                    str(StarTools.get_data_dir(VLF_PLUGIN))
                ) / "plans.json"
                with open(data_dir, encoding="utf-8") as f:
                    data = json.load(f)
                plan = pick_plan(
                    data, datetime.date.today().isoformat(), self._default_persona_name()
                )
                resolved = sleep_start_from_plan(plan or {}) if plan else None
        except FileNotFoundError:
            resolved = None
        except Exception as exc:
            logger.debug(f"[{NAME}] 虚拟作息读取失败：{exc}")
            resolved = None
        self._vlife_cache = (now, resolved)
        if resolved and resolved != start and self._vlife_logged != resolved:
            self._vlife_logged = resolved
            logger.info(
                f"[{NAME}] 虚拟作息适配：今日虚拟日程 {resolved} 休息"
                f"（配置值 {start}，晚安道别与守卫随之调整）"
            )
        return resolved or start

    def _default_persona_name(self) -> str:
        try:
            cfg = self.context.get_config()
            agent = (cfg.get("agent_runner") or {}).get("config") or {}
            # 人格 id 实际存在 agent_runner.config.persona（default_personality 仅运行时回填）
            return str(
                (agent.get("persona") or {}).get("persona_id")
                or (cfg.get("provider_settings") or {}).get("default_personality")
                or ""
            )
        except Exception:
            return ""

    def _in_window(self, now: datetime.datetime | None = None) -> bool:
        now = now or datetime.datetime.now()
        return in_sleep_window(
            now.hour * 60 + now.minute,
            self._effective_sleep_start(),
            str(self.conf.get("wake_time") or "07:00"),
        )

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
                await self._goodnight_check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[{NAME}] 作息同步失败: {exc}")
            await asyncio.sleep(60)

    # ---------------- 临睡道别 ----------------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def watch_private_activity(self, event: AstrMessageEvent):
        """记录私聊活跃度（休息中不记），供临睡晚安道别判断「正在和她聊天」。"""
        platform = event.get_platform_name()
        if platform == "webchat" and self._webchat_exempt():
            return
        if self._in_window():
            return
        umo = str(event.unified_msg_origin or "")
        if not umo:
            return
        try:
            name = str(event.get_sender_name() or "")
        except Exception:
            name = ""
        self._last_private[umo] = (time.time(), name)
        if len(self._last_private) > 100:
            oldest = min(self._last_private, key=lambda k: self._last_private[k][0])
            self._last_private.pop(oldest, None)

    async def _goodnight_check(self) -> None:
        if not self._enabled() or not bool(self.conf.get("goodnight_enabled", True)):
            return
        now = datetime.datetime.now()
        window = int(self.conf.get("goodnight_window_minutes", 5))
        if not in_goodnight_window(
            now.hour * 60 + now.minute,
            self._effective_sleep_start(),
            window,
        ):
            return
        today = now.strftime("%Y-%m-%d")
        active_minutes = int(self.conf.get("goodnight_active_minutes", 5))
        targets = [
            (umo, name)
            for umo, (ts, name) in self._last_private.items()
            if is_recently_active(ts, time.time(), active_minutes)
            and self._goodnight_sent.get(umo) != today
        ]
        if not targets:
            return
        text = await self._goodnight_text()
        sent = []
        for umo, name in targets:
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[Plain(text)])
                )
                self._goodnight_sent[umo] = today
                sent.append(name or umo)
            except Exception as exc:
                logger.warning(f"[{NAME}] 晚安道别发送失败（{umo}）：{exc}")
        if sent:
            logger.info(f"[{NAME}] 临睡晚安已发送：{'、'.join(sent)}")
        if len(self._goodnight_sent) > 50:
            for key in [k for k, v in self._goodnight_sent.items() if v != today]:
                self._goodnight_sent.pop(key, None)

    async def _goodnight_text(self) -> str:
        fallback = random.choice(_GOODNIGHT_FALLBACK)
        try:
            provider = await self.context.get_using_provider_async()
            if provider is None:
                return fallback
            result = await provider.text_chat(
                prompt=(
                    "马上要到你的休息时间了，你要去睡觉了。写一句发给正在和你聊天的"
                    "对方的临睡晚安道别：一两句话，口语自然带一点可爱，提到你要去睡了，"
                    "让对方也早点休息。只输出这句话本身，不要引号和解释。"
                ),
                system_prompt="用中文聊天口吻写一句道别，不要解释。",
            )
            text = str(getattr(result, "completion_text", "") or "").strip()
            return text[:100] if text else fallback
        except Exception as exc:
            logger.warning(f"[{NAME}] 晚安语生成失败，用兜底文案：{exc}")
            return fallback

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

