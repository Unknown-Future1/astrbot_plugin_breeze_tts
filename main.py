"""
astrbot_plugin_breeze_tts —— 用本地 Breeze TTS 2 模型把文字回复转成语音

功能概览：
  · 总开关 / 概率 / 长度门槛 / 屏蔽词
  · 装饰结果钩子（核心）
  · 语音说过后按需用零宽空格抑制文字（voice_silence_mode）
  · LLM 工具 clone_tts
  · 自动语气指令（多调一次 LLM 分析情绪）
  · numpy 实现语速（WSOLA）与音调（变调不变速），不依赖外部程序
  · /tts 手动点播 + 合成失败兜底提示
  · 「服务控制」页：选模型目录、一键启动/停止本地 breeze-server
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import subprocess
import time

from pydantic import Field
from pydantic.dataclasses import dataclass

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config import AstrBotConfig
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.filter.command import GreedyStr

from .service.gpu_probe import GpuProbe
from .service.launcher import IS_WINDOWS, ServiceManager
from .tts_api.breeze_api import BreezeTTSClient
from .utils import audio_ops
from .webapi import register_web_apis

ZERO_WIDTH = "\u200b"
# 各量化版本拉起服务大约需要的显存（MB，依据官方文档 + 实测）
QUANT_VRAM_MB = {
    "f16": 7000,
    "q8_0": 3420,
    "q6_k": 2920,
    "q4_k": 2420,
    "q3_k": 2100,
    "q2_k": 1800,
}
DEFAULT_VRAM_NEED_MB = 3420  # 识别不出时按 Q8_0 估
TTS_VRAM_MARGIN_MB = 512
# 服务本身还占约 3.8 GB 内存，留 512 余量
QUANT_RAM_MB = {
    "f16": 6500,
    "q8_0": 4200,
    "q6_k": 3700,
    "q4_k": 3200,
    "q3_k": 2900,
    "q2_k": 2500,
}
DEFAULT_RAM_NEED_MB = 4200

USAGE_TTS = (
    "用法：\n"
    "· /tts 要念的内容        （不写语气，自动分析情绪）\n"
    "· /tts [温柔一点]要念的内容\n"
    "· /tts 温柔一点|要念的内容 （正文里别带竖线）"
)

# 两种语气写法：[语气] 正文  或  语气|正文
_STYLE_BRACKET = re.compile(r"^\s*\[([^\[\]]{1,60})\]\s*(.+)$", re.S)
_STYLE_PIPE = re.compile(r"^\s*([^|\n]{1,60}?)\s*\|\s*(.+)$", re.S)


def split_style_prefix(raw: str) -> tuple[str, str]:
    """从 /tts 参数里拆出（正文, 语气）。没有语气时返回 (raw, "")。"""
    text = (raw or "").strip()
    for pattern in (_STYLE_BRACKET, _STYLE_PIPE):
        match = pattern.match(text)
        if match:
            style = match.group(1).strip()
            body = match.group(2).strip()
            if body:
                return body, style
    return text, ""


# 三档静态风格预设（提示词实测：只能更慢/更轻，做不到更快/更响）
STYLE_PRESETS = {
    "normal": "",
    "gentle": "语速慢一些，声音轻一点。",
    "whisper": "语速非常慢，一个字一个字地拖长，音量很小像说悄悄话。",
}

# 自动语气指令用的提示词（沿用原插件的思路，精简到适配本地模型）
AUTO_INSTRUCTION_PROMPT = (
    "你是一位精通语音合成（TTS）风格控制的提示词工程师。\n"
    "请分析下面这句话的语境、潜台词和情感流动，然后输出【一段中文配音指令】。\n"
    "要求：\n"
    "1. 只输出指令本身，一个字符串，不要任何分析过程、解释、引号或前缀；\n"
    "2. 用对配音演员说话的口吻，例如“语速稍微慢一点”“语气再温柔些”；\n"
    "3. 覆盖语速、情绪、语气、音量四个维度中真正需要变化的那些；\n"
    "4. 总长度控制在 30 字以内，宁短勿长。\n\n"
    "句子：{text}"
)


def _as_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


SENT_SPLIT = re.compile(r"(?<=[。！？!?])")
COMMA_SPLIT = re.compile(r"(?<=[，,、；;：:])")


def split_for_tts(text: str, max_chars: int = 30) -> list[str]:
    """按句末标点切分；单句过长退到逗号；无标点则按字数硬切。"""
    parts = [p for p in SENT_SPLIT.split(text or "") if p.strip()]
    segs: list[str] = []
    cur = ""
    for part in parts:
        pieces = [part]
        if len(part) > max_chars:
            pieces = [s for s in COMMA_SPLIT.split(part) if s.strip()]
        for piece in pieces:
            while len(piece) > max_chars:
                if cur.strip():
                    segs.append(cur)
                    cur = ""
                segs.append(piece[:max_chars])
                piece = piece[max_chars:]
            if cur and len(cur) + len(piece) <= max_chars:
                cur += piece
            else:
                if cur.strip():
                    segs.append(cur)
                cur = piece
    if cur.strip():
        segs.append(cur)
    return [s for s in segs if s.strip()]


def _group_get(cfg, key: str, default=None):
    """先读 split_synthesis 分组，再回退到扁平键（兼容旧配置）。"""
    try:
        sub = cfg.get("split_synthesis")
        if isinstance(sub, dict) and key in sub:
            return sub[key]
    except Exception:  # noqa: BLE001
        pass
    return cfg.get(key, default)


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return default


def _plugin_meta(fallback_version: str = "0.0.0", fallback_desc: str = "") -> tuple[str, str]:
    """从同目录 metadata.yaml 读 (desc, version)，避免和 @register 里写的不一致。"""
    desc, version = fallback_desc, fallback_version
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("desc:") and len(line) > 5:
                    desc = line.split(":", 1)[1].strip().strip("\"'")
                elif line.startswith("version:") and len(line) > 8:
                    version = line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return desc, version


_META_DESC, _META_VERSION = _plugin_meta()

@register(
    "astrbot_plugin_breeze_tts",
    "未知の未来",
    _META_DESC,
    _META_VERSION,
)
class BreezeTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config
        self.config = config

        self.enable_tts = _as_bool(cfg.get("enable_tts", True), True)
        self.tts_probability = _as_int(cfg.get("tts_probability", 80), 80, 0, 100)
        self.min_length = _as_int(cfg.get("min_length", 5), 5, 1, 1000)
        # 单条防呆上限：设置里不暴露，固定 600 字
        self.max_total_chars = 600
        self.blocked_words = [str(w) for w in (cfg.get("blocked_words") or []) if str(w).strip()]

        # 服务地址只由「服务端口」决定（127.0.0.1:端口），连接/启动/探测共用
        self.service_port = _as_int(cfg.get("service_port", 9000), 9000, 1, 65535)
        self.breeze_api = f"http://127.0.0.1:{self.service_port}"
        self.voice_id = str(cfg.get("voice_id") or "").strip()
        self.cfg_scale = _as_float(cfg.get("cfg_scale", 2.0), 2.0, 1.0, 4.0)
        self.seed = _as_int(cfg.get("seed", 42), 42, 0, 2**31 - 1)
        self.timeout = _as_int(cfg.get("timeout", 30), 30, 5, 300)

        self.enable_auto_instruction = _as_bool(cfg.get("enable_auto_instruction", True), True)
        self.llm_recognition = str(cfg.get("llm_recognition") or "").strip()
        self.style_preset = str(cfg.get("style_preset") or "normal").strip() or "normal"
        self.custom_instruction = str(cfg.get("custom_instruction") or "").strip()

        self.speed_ratio = _as_int(cfg.get("speed_ratio", 0), 0, -50, 100)
        self.pitch_shift = _as_int(cfg.get("pitch_shift", 0), 0, -12, 12)

        self.enable_tts_tool_minmax = _as_bool(cfg.get("enable_tts_tool_minmax", True), True)
        self.enable_llm_response = _as_bool(cfg.get("enable_llm_response", False), False)

        self.enable_tts_command = _as_bool(cfg.get("enable_tts_command", True), True)
        fail_reply = cfg.get("fail_reply_texts")
        if fail_reply is None:
            fail_reply = cfg.get("fail_reply_text")
        if isinstance(fail_reply, str):
            self.fail_reply_texts = [fail_reply] if fail_reply.strip() else []
        else:
            self.fail_reply_texts = [
                str(item).strip() for item in (fail_reply or []) if str(item).strip()
            ]

        # ---- 本地服务（模型目录 / 起停控制）----
        self.model_dir = str(cfg.get("model_dir") or "").strip()
        self.model_file = str(cfg.get("model_file") or "").strip()
        self.service_parallel = _as_int(cfg.get("service_parallel", 4), 4, 1, 16)
        self.service_warmup = _as_bool(cfg.get("service_warmup", False), False)
        self.idle_stop_minutes = _as_int(cfg.get("idle_stop_minutes", 5), 5, 1, 1440)
        # 检查间隔：最小值 30s，最大值动态跟随「空闲停止时间 × 60」
        self.split_enabled = _as_bool(_group_get(cfg, "split_enabled", True), True)
        self.split_min_chars = _as_int(
            _group_get(cfg, "split_min_chars", 30), 30, 10, 200
        )
        self.split_max_chars = _as_int(
            _group_get(cfg, "split_max_chars", 30), 30, 10, 200
        )
        self.split_gap_ms = _as_int(_group_get(cfg, "split_gap_ms", 350), 350, 0, 2000)
        self.split_parallel = _as_bool(_group_get(cfg, "split_parallel", True), True)
        self.auto_start_service = _as_bool(cfg.get("auto_start_service", True), True)
        self._auto_start_task = None
        # 持久化数据按 AstrBot 规范放 data/plugin_data/<插件名>；拿不到就退回插件目录
        try:
            data_dir = str(StarTools.get_data_dir(str(getattr(self, "name", "") or "") or None))
        except Exception:  # noqa: BLE001
            data_dir = ""
        self.service = ServiceManager(
            plugin_dir=os.path.dirname(os.path.abspath(__file__)),
            model_dir=self.model_dir,
            model_file=self.model_file,
            port=self.service_port,
            parallel=self.service_parallel,
            data_dir=data_dir,
            warmup=self.service_warmup,
        )
        self._inflight = 0
        self._last_use = 0.0
        self._prewarm_task = None
        self._idle_handle = None
        self._idle_warn_handle = None
        self._cycle_start = 0.0
        self._stop_pending = False
        # 串行化「启动/停止」与「合成开始」：避免空闲停止在合成刚起步时把服务杀掉
        self._service_gate = asyncio.Lock()
        self._idle_warned = False

        self.gpu = GpuProbe()
        self.client = BreezeTTSClient(
            api_base=self.breeze_api,
            voice=self.voice_id,
            cfg_scale=self.cfg_scale,
            seed=self.seed,
            timeout=self.timeout,
        )

        if not audio_ops._HAS_NUMPY and (self.speed_ratio or self.pitch_shift):
            logger.warning("[BreezeTTS] 环境缺少 numpy，语速/音调后处理将不生效")

        # clone_tts 工具随着插件启用，不再单独开关
        self.context.add_llm_tools(BreezeTTSTool(plugin=self))

        # 「服务控制」页的接口（选目录 / 起停服务）
        try:
            register_web_apis(self)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BreezeTTS] 服务控制接口注册失败：{exc}")

    async def initialize(self):
        """插件启用时：按配置的端口检查服务，并识别音色"""
        self._last_use = time.time()
        self._schedule_idle_stop()
        ok = await self.client.health()
        if not ok:
            if self.auto_start_service:
                logger.info("[BreezeTTS] 服务离线（可能处于空闲休眠），正在自动拉起…")
            else:
                logger.warning(
                    f"[BreezeTTS] 已启用，但连不上服务 {self.breeze_api}"
                    "（确认服务确实跑在这个端口，可在「服务端口」里改；"
                    "或到「服务控制」页点一下启动）；语音会静默跳过，文本回复不受影响"
                )
            if self.auto_start_service:
                # 保留引用，防止任务被垃圾回收（asyncio 只持弱引用）
                self._auto_start_task = asyncio.create_task(self._auto_start_service())
            return

        await self._refresh_voice()

        logger.info(f"[BreezeTTS] 已启用，服务在线：{self.breeze_api}，音色：{self.voice_id or '（未识别）'}")

    async def _refresh_voice(self) -> list[str]:
        """刷新服务端音色列表，顺便补齐没填的 voice_id。"""
        voices = await self.client.list_voices()
        if not self.voice_id:
            if voices:
                self.voice_id = voices[0]
                self.client.voice = voices[0]
                logger.info(
                    f"[BreezeTTS] 未配置 voice_id，自动使用第一个音色：{voices[0]}"
                    f"（服务端共 {len(voices)} 个：{', '.join(voices)}）"
                )
            else:
                logger.warning(
                    "[BreezeTTS] 服务端还没有任何音色，语音合成会失败；"
                    "请先按 README 的「克隆音色」步骤克隆一个"
                )
        elif voices and self.voice_id not in voices:
            logger.warning(
                f"[BreezeTTS] 音色 '{self.voice_id}' 不在服务端列表中，"
                f"现有音色：{', '.join(voices)}"
            )
        return voices

    async def _auto_start_service(self):
        """按配置自动拉起本地模型服务（异步，不阻塞插件加载）。"""
        if not IS_WINDOWS:
            logger.info("[BreezeTTS] 非 Windows 环境，跳过自动启动本地服务")
            return
        if not self.service.model_dir:
            logger.info("[BreezeTTS] 配置里没填「本地模型目录」，跳过自动启动")
            return
        try:
            loop = asyncio.get_running_loop()
            need_v = self.vram_need_mb() + TTS_VRAM_MARGIN_MB
            need_r = self.ram_need_mb()
            free_v = await loop.run_in_executor(None, self.free_vram_mb)
            free_r = await loop.run_in_executor(None, self.free_ram_mb)
            if (free_v is not None and free_v < need_v) or (
                free_r is not None and free_r < need_r
            ):
                logger.warning(
                    f"[BreezeTTS] 资源不足（显存 {free_v}/{need_v} MB，内存 {free_r}/{need_r} MB），"
                    f"暂不自动启动；下次语音请求会再检查"
                )
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BreezeTTS] 启动前检查失败，继续尝试启动：{exc}")
        logger.info("[BreezeTTS] 探测不到服务，按配置自动启动本地模型（首次加载约 5~10 秒）…")
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self.service.start)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BreezeTTS] 自动启动异常：{exc}")
            return
        if not result.get("ok"):
            logger.warning(f"[BreezeTTS] 自动启动失败：{result.get('message')}")
            return
        logger.info(f"[BreezeTTS] {result.get('message')}")
        self.breeze_api = f"http://127.0.0.1:{self.service_port}"
        self.client.api_base = self.breeze_api
        await self._refresh_voice()

    def _cancel_idle_timers(self) -> None:
        for handle in (self._idle_handle, self._idle_warn_handle):
            if handle is not None:
                handle.cancel()
        self._idle_handle = None
        self._idle_warn_handle = None

    def _arm_cycle(self) -> None:
        """记录本轮“服务开始被使用”的时间，用于硬上限。"""
        if not self._cycle_start:
            self._cycle_start = time.time()

    def _schedule_idle_stop(self) -> None:
        """软截止 = 最后活动 + 空闲分钟；硬上限 = 本轮开始 + 2×空闲分钟。"""
        self._cancel_idle_timers()
        if not self.idle_stop_minutes or not self._last_use:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        soft = self.idle_stop_minutes * 60
        start = self._cycle_start or self._last_use
        deadline = min(self._last_use + soft, start + soft * 2)
        delay = max(1, deadline - time.time())
        self._idle_warn_handle = loop.call_later(max(1, delay - 30), self._idle_warn)
        self._idle_handle = loop.call_later(delay, lambda: asyncio.create_task(self._idle_stop()))

    def _idle_warn(self) -> None:
        if self._inflight:
            return
        logger.info(
            f"[BreezeTTS] 空闲将满 {self.idle_stop_minutes} 分钟，约 30 秒后停止服务"
        )

    async def _idle_stop(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._service_gate:
            if self._inflight:
                self._stop_pending = True
                logger.info("[BreezeTTS] 到停止时间但仍在合成，等这次合成结束就停止")
                return
            await self._do_stop(loop)

    async def _do_stop(self, loop) -> None:
        if not await loop.run_in_executor(None, self.client_health):
            # 服务已经不在（可能被手动停掉）：清空计时状态，避免残留
            self._last_use = 0.0
            self._cycle_start = 0.0
            self._stop_pending = False
            self._cancel_idle_timers()
            return
        logger.info(f"[BreezeTTS] 空闲 {self.idle_stop_minutes} 分钟，停止服务释放显存")
        result = await loop.run_in_executor(None, self.service.stop)
        logger.info(f"[BreezeTTS] {result.get('message')}")
        self._last_use = 0.0
        self._cycle_start = 0.0
        self._stop_pending = False
        self._cancel_idle_timers()

    def client_health(self) -> bool:
        return bool(self.service.health(timeout=0.5)[0])

    async def terminate(self):
        self._cancel_idle_timers()
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()
        # 注意：插件停用/重载时故意不结束本地服务，避免「改个配置服务就断了」。
        # 需要停服务请用「服务控制」页的按钮。
        logger.info("[BreezeTTS] 插件已停用/卸载（本地服务保持运行，可在服务控制页手动停止）")

    def fail_reply(self, default: str = "") -> str:
        if self.fail_reply_texts:
            return random.choice(self.fail_reply_texts)
        return default

    def probability(self, percent: float) -> bool:
        p = max(0.0, min(100.0, float(percent)))
        return random.random() < (p / 100)

    def hit_blocked(self, text: str) -> bool:
        if not self.blocked_words:
            return False
        low = text.lower()
        return any(w.lower() in low for w in self.blocked_words)

    def cfg_for(self, instruction: str) -> float:
        """有语气指令时用配置的引导强度，没有时用 1.0 换速度。"""
        return self.cfg_scale if instruction else 1.0

    def preset_instruction(self) -> str:
        return STYLE_PRESETS.get(self.style_preset, "")

    def style_instruction(self, preset: str) -> str:
        """按预设 id 取指令文本（页面试听用；没传就回退当前设置）。"""
        return STYLE_PRESETS.get(preset or self.style_preset, "")

    async def _auto_instruction(self, event: AstrMessageEvent, text: str) -> str:
        """调一次 LLM 分析情绪，生成配音指令（失败则返回空）"""
        try:
            if self.llm_recognition:
                provider_id = self.llm_recognition
            else:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=AUTO_INSTRUCTION_PROMPT.format(text=text),
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            # 清掉模型可能加的引号/换行
            raw = re.sub(r'^["“”\'\s]+|["“”\'\s]+$', "", raw)
            raw = raw.replace("\n", " ").strip()
            if len(raw) > 80:
                raw = raw[:80]
            logger.debug(f"[BreezeTTS] 自动语气指令：{raw}")
            return raw
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[BreezeTTS] 自动语气指令生成失败，回退预设：{exc}")
            return ""

    async def resolve_instruction(self, event: AstrMessageEvent, text: str) -> str:
        """指令优先级：手写 > 自动 > 预设"""
        if self.custom_instruction:
            return self.custom_instruction
        if self.enable_auto_instruction:
            auto = await self._auto_instruction(event, text)
            if auto:
                return auto
        return self.preset_instruction()

    def _model_tag(self) -> str:
        """取模型文件名（小写）；没填就扫描目录里第一个 gguf。"""
        name = str(getattr(self, "model_file", "") or "").lower()
        if name:
            return name
        try:
            files = sorted(
                f.lower() for f in os.listdir(self.model_dir) if f.lower().endswith(".gguf")
            )
            return files[0] if files else ""
        except OSError:
            return ""

    def vram_need_mb(self) -> int:
        """按量化估算启动所需显存；识别不出按 Q8_0。"""
        name = self._model_tag()
        for key, need in QUANT_VRAM_MB.items():
            if key in name:
                return need
        return DEFAULT_VRAM_NEED_MB

    def ram_need_mb(self) -> int:
        """按量化估算启动所需内存；识别不出按 Q8_0。"""
        name = self._model_tag()
        for key, need in QUANT_RAM_MB.items():
            if key in name:
                return need
        return DEFAULT_RAM_NEED_MB

    def free_ram_mb(self) -> int | None:
        """读系统可用内存（物理与提交余量取小），读不到返回 None。"""
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            avail_phys = status.ullAvailPhys // (1024 * 1024)
            avail_commit = status.ullAvailPageFile // (1024 * 1024)
            return int(min(avail_phys, avail_commit))
        except Exception:  # noqa: BLE001
            return None

    def free_vram_mb(self) -> int | None:
        """读当前显卡剩余显存（MB）：优先 PDH（A/N 通吃），失败再退回 nvidia-smi。"""
        try:
            value = self.gpu.free_mb()
            if value is not None:
                return value
        except Exception:  # noqa: BLE001
            pass
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=0x08000000,
            )
            lines = [x.strip() for x in out.stdout.splitlines() if x.strip()]
            return int(lines[0]) if lines else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    async def _wait_resources(self) -> bool:
        """等待显存与内存都够；不足返回 False。"""
        loop = asyncio.get_running_loop()
        need_mb = self.vram_need_mb() + TTS_VRAM_MARGIN_MB
        ram_need = self.ram_need_mb()
        logger.info(
            f"[BreezeTTS] 预计启动需要 {need_mb} MB 显存、{ram_need} MB 内存"
            f"（模型：{self.model_file or '自动'}）"
        )

        free_mb = await loop.run_in_executor(None, self.free_vram_mb)
        free_ram = await loop.run_in_executor(None, self.free_ram_mb)
        waited = 0
        while (
            (free_mb is not None and free_mb < need_mb)
            or (free_ram is not None and free_ram < ram_need)
        ) and waited < 5:
            logger.info(
                f"[BreezeTTS] 资源不足（显存 {free_mb} MB/{need_mb} MB，"
                f"内存 {free_ram} MB/{ram_need} MB），等待其他程序释放…"
            )
            await asyncio.sleep(1)
            waited += 1
            free_mb = await loop.run_in_executor(None, self.free_vram_mb)
            free_ram = await loop.run_in_executor(None, self.free_ram_mb)

        if free_mb is not None and free_mb < need_mb:
            logger.warning(
                f"[BreezeTTS] 显存仍不足（可用 {free_mb} MB < {need_mb} MB），跳过本次语音"
            )
            return False
        if free_ram is not None and free_ram < ram_need:
            logger.warning(
                f"[BreezeTTS] 内存仍不足（可用 {free_ram} MB < {ram_need} MB），"
                f"跳过本次语音（避免合成超时）"
            )
            return False
        return True

    async def _start_service(self) -> bool:
        """启动服务，失败重试一次。调用方需持有门锁。"""
        loop = asyncio.get_running_loop()
        logger.info("[BreezeTTS] 服务离线，按需启动（约 2~3 秒）…")
        result = await loop.run_in_executor(None, self.service.start)
        if not result.get("ready"):
            logger.warning(
                f"[BreezeTTS] 启动失败：{result.get('message')}，1 秒后重试一次"
            )
            await asyncio.sleep(1.0)
            result = await loop.run_in_executor(None, self.service.start)
        if not result.get("ready"):
            logger.warning(f"[BreezeTTS] 重试仍失败：{result.get('message')}")
            return False
        self.breeze_api = f"http://127.0.0.1:{self.service_port}"
        self.client.api_base = self.breeze_api
        if not self.voice_id:
            await self._refresh_voice()
        return True

    async def begin_synth(self) -> bool:
        """确保服务在线并登记一次合成；失败返回 False。"""
        loop = asyncio.get_running_loop()
        async with self._service_gate:
            if not await loop.run_in_executor(None, self.client_health):
                if not self.auto_start_service:
                    logger.warning("[BreezeTTS] 服务离线，且未开启「自动启动」，跳过语音")
                    return False
                if not await self._wait_resources():
                    return False
                if not await self._start_service():
                    return False
                self.note_activity()
            self._inflight += 1
            return True

    def note_activity(self) -> None:
        """手动启动服务后也算一次“使用”，让空闲计时从零开始。"""
        self._last_use = time.time()
        self._arm_cycle()
        self._schedule_idle_stop()

    def end_synth(self) -> None:
        self._inflight = max(0, self._inflight - 1)
        self._last_use = time.time()
        if self._stop_pending:
            self._stop_pending = False
            try:
                asyncio.get_running_loop().create_task(self._idle_stop())
            except RuntimeError:
                pass
            return
        self._arm_cycle()
        self._schedule_idle_stop()

    def _split_concurrency(self) -> int:
        """分段并发数：开启并发且服务槽位足够（cfg 请求占 2 槽）时用 2，否则 1。"""
        if not self.split_parallel:
            return 1
        if self.service_parallel < 4:
            logger.info(
                f"[BreezeTTS] 服务并行槽只有 {self.service_parallel}，"
                f"分段改用串行，避免 409"
            )
            return 1
        return 2

    async def synth_wav(self, text: str, instruction: str, cfg: float):
        """按配置分段合成；短文本直接整段。"""
        segments = (
            split_for_tts(text, self.split_max_chars)
            if self.split_enabled and len(text) > self.split_min_chars
            else [text]
        )
        if len(segments) <= 1:
            return await self.client.synth(
                text=text,
                instructions=instruction or None,
                speed_ratio=self.speed_ratio,
                pitch_shift=self.pitch_shift,
                cfg_scale=cfg,
            )

        concurrency = self._split_concurrency()
        logger.info(
            f"[BreezeTTS] 分段合成：{len(segments)} 段，"
            f"段间 {self.split_gap_ms} ms，并发 {concurrency} → "
            f"{[len(s) for s in segments]}"
        )
        sem = asyncio.Semaphore(concurrency)

        async def _one(seg: str):
            async with sem:
                return await self.client.synth(
                    text=seg,
                    instructions=instruction or None,
                    speed_ratio=self.speed_ratio,
                    pitch_shift=self.pitch_shift,
                    cfg_scale=cfg,
                    pad_ms=0,
                    lead_ms=0,
                )

        parts = list(await asyncio.gather(*(_one(seg) for seg in segments)))
        if any(p is None for p in parts):
            return None
        return audio_ops.concat_wavs(parts, gap_ms=self.split_gap_ms)

    async def synthesize(
        self,
        event: AstrMessageEvent,
        text: str,
        instruction_override: str = "",
    ) -> str | None:
        """生成语音，返回 base64 字符串；失败返回 None（调用方降级）。

        instruction_override 非空时优先使用（/tts 指令手写的语气），
        否则按「手写配置 > LLM 自动分析 > 静态预设」解析。
        """
        # 惰性兜底 + 按需启动 + 登记合成（与空闲停止互斥）
        if not await self.begin_synth():
            return None

        if not self.voice_id:
            try:
                detected = await self.client.detect_voice()
                if detected:
                    self.voice_id = detected
                    logger.info(f"[BreezeTTS] 自动识别到音色：{detected}")
            except Exception:  # noqa: BLE001
                pass

        override = str(instruction_override or "").strip()
        instruction = (
            override if override else await self.resolve_instruction(event, text)
        )
        logger.info(
            f"[BreezeTTS] 语气指令：{instruction or '（无）'} | 文本：{text[:30]}"
        )
        try:
            wav = await self.synth_wav(text, instruction, self.cfg_for(instruction))
        finally:
            self.end_synth()
        if not wav:
            return None
        return self.client.to_base64(wav)

    @filter.on_llm_request()
    async def cold_start_prewarm_hook(self, event, req):
        """服务离线时，在 LLM 思考期间后台拉起服务。"""
        if not self.enable_tts or not self.auto_start_service:
            return
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, self.client_health):
            return
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return
        self._prewarm_task = asyncio.create_task(self._prewarm())

    async def _prewarm(self) -> None:
        """LLM 思考期间提前启动服务，省掉正式合成前的等待。"""
        loop = asyncio.get_running_loop()
        try:
            async with self._service_gate:
                if await loop.run_in_executor(None, self.client_health):
                    return
                if not await self._wait_resources():
                    return
                logger.info("[BreezeTTS] 检测到冷启动，提前拉起服务…")
                if await self._start_service():
                    self.note_activity()
                    logger.info("[BreezeTTS] 预热完成，服务已就绪")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[BreezeTTS] 预热失败：{exc}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        try:
            if not self.enable_tts:
                return

            result = event.get_result()
            if result is None or not result.chain:
                logger.debug("[BreezeTTS] 无结果，跳过")
                return

            # 本轮走过 clone_tts（保留文字开启时）：按工具文本统一出「文字 + 语音」，内容一致
            tool_text = str(event.get_extra("breeze_tool_text") or "").strip()
            if tool_text:
                task = event.get_extra("breeze_tool_task")
                if task is not None:
                    try:
                        wav = await task  # 工具里已经开跑，通常此时刚好拿结果
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[BreezeTTS] 工具后台合成失败：{exc}")
                        wav = None
                else:
                    instruction = str(
                        event.get_extra("breeze_tool_instruction") or ""
                    ).strip() or (self.custom_instruction or self.preset_instruction())
                    wav = await self.client.synth(
                        text=tool_text,
                        instructions=instruction or None,
                        speed_ratio=self.speed_ratio,
                        pitch_shift=self.pitch_shift,
                        cfg_scale=self.cfg_for(instruction),
                    )
                if wav:
                    result.chain = [
                        Comp.Plain(tool_text),
                        Comp.Record.fromBase64(self.client.to_base64(wav)),
                    ]
                    logger.info("[BreezeTTS] 已按工具文本统一发送「文字 + 语音」")
                else:
                    logger.warning("[BreezeTTS] 工具文本配音失败，只发文字")
                return

            # 只处理 LLM 产出（跳过指令返回等）
            if not result.is_llm_result():
                return

            text = "".join(
                c.text for c in result.chain if getattr(c, "text", "")
            ).strip()
            if not text:
                return
            # 「保留文字」关闭时会用零宽字符抹掉文字，这里别再给空内容配音
            if not text.replace(ZERO_WIDTH, "").strip():
                logger.debug("[BreezeTTS] 文字已被抹掉（只发语音模式），跳过装饰")
                return

            # 只保留一个防呆上限，正常长度交给分段合成
            if len(text) > self.max_total_chars:
                logger.debug(
                    f"[BreezeTTS] 文本 {len(text)} 字超过防呆上限 {self.max_total_chars}，跳过"
                )
                return
            if len(text) < self.min_length:
                logger.debug(
                    f"[BreezeTTS] 文本 {len(text)} 字低于下限 {self.min_length}，跳过"
                )
                return
            if self.hit_blocked(text):
                logger.debug("[BreezeTTS] 命中屏蔽词，跳过语音")
                return
            lucky = event.get_extra("breeze_tts_lucky")
            if lucky is None:
                lucky = self.probability(self.tts_probability)
                event.set_extra("breeze_tts_lucky", lucky)
            if not lucky:
                logger.debug("[BreezeTTS] 未命中概率，跳过语音")
                return

            logger.info(f"[BreezeTTS] 开始合成：{text[:30]}")
            audio_b64 = await self.synthesize(event, text)
            if not audio_b64:
                # 合成失败：文字照发，并按配置补一句提示（比如"嗓子哑了"）
                logger.warning("[BreezeTTS] 语音合成失败，本次只发文字")
                if self.fail_reply_texts:
                    try:
                        res = event.get_result()
                        if res is not None and res.chain:
                            res.chain.append(Comp.Plain(f"\n{self.fail_reply()}"))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[BreezeTTS] 追加失败提示出错：{exc}")
                return

            record = Comp.Record.fromBase64(audio_b64)
            if self.enable_llm_response:
                result.chain.append(record)
            else:
                result.chain = [record]

        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BreezeTTS] on_decorating_result 异常：{exc}")

    @filter.command("tts")
    async def cmd_tts(self, event: AstrMessageEvent, text: GreedyStr):
        """把一段文字念成语音：/tts 你好呀"""
        if not self.enable_tts_command:
            return

        raw = str(text or "").strip()
        if not raw:
            yield event.plain_result(USAGE_TTS)
            return

        content, style = split_style_prefix(raw)
        if not content:
            yield event.plain_result(USAGE_TTS)
            return

        if self.hit_blocked(content):
            yield event.plain_result("这句话里有不适合朗读的内容，换个说法吧")
            return

        if len(content) > self.max_total_chars:
            yield event.plain_result(
                f"太长了（{len(content)} 字），单次最多 {self.max_total_chars} 字"
            )
            return

        logger.info(
            f"[BreezeTTS] /tts 手工合成：{content[:30]}"
            f" | 手写语气：{style or '（走自动分析）'}"
        )
        audio_b64 = await self.synthesize(event, content, instruction_override=style)
        if not audio_b64:
            logger.warning("[BreezeTTS] /tts 合成失败")
            yield event.plain_result(self.fail_reply("（刚刚嗓子有点干,没说出话…稍后再试）"))
            return

        yield event.chain_result([Comp.Record.fromBase64(audio_b64)])

    @filter.on_llm_response()
    async def handle_silence(self, event: AstrMessageEvent, resp: LLMResponse):
        if not event.get_extra("breeze_voice_silence_mode"):
            return
        try:
            event.set_extra("breeze_voice_silence_mode", False)
            resp.completion_text = ZERO_WIDTH
            event.stop_event()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[BreezeTTS] 文字抑制失败：{exc}")


@dataclass
class BreezeTTSTool(FunctionTool[AstrAgentContext]):
    """让模型可以主动"开口说话"的工具"""

    name: str = "clone_tts"
    description: str = (
        "把文本转成语音发出去。当用户想听声音、让你说句话、或要求用语音回答时调用。"
        "text 是你要说的内容；context_texts 是你希望的说话风格（可选）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "需要转换为语音的文本",
                },
                "context_texts": {
                    "type": "string",
                    "description": (
                        "可选的语气指令，用自然语言描述希望的语气/语速/音量，"
                        "例如：'温柔一点，语速稍慢'。不需要就留空。"
                    ),
                },
            },
            "required": ["text"],
        }
    )

    plugin: object = Field(default=None, repr=False)

    async def _background_synth(self, text, instruction, cfg):
        if not await self.plugin.begin_synth():
            return None
        try:
            return await self.plugin.synth_wav(text, instruction, cfg)
        finally:
            self.plugin.end_synth()

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        try:
            return await self._call(context, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BreezeTTS] 工具执行异常：{exc}")
            return "语音合成失败"

    async def _call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if not self.plugin:
            return "插件未正确初始化"
        text = str(kwargs.get("text") or "").strip()
        if not text:
            return "文本不能为空"

        if self.plugin.enable_tts_tool_minmax:
            limit = getattr(self.plugin, "max_total_chars", 600)
            if len(text) > limit:
                return f"文本长度超过上限 {limit}，跳过语音合成"
            if len(text) < self.plugin.min_length:
                return f"文本长度低于下限 {self.plugin.min_length}，跳过语音合成"

        instruction = str(kwargs.get("context_texts") or "").strip()
        if not instruction:
            instruction = self.plugin.custom_instruction or self.plugin.preset_instruction()

        ev = context.context.event
        cfg = self.plugin.cfg_for(instruction)
        if self.plugin.enable_llm_response:
            # 后台先合成，装饰钩子直接取用，只合成一次
            ev.set_extra("breeze_tool_text", text)
            ev.set_extra("breeze_tool_instruction", instruction or "")
            ev.set_extra(
                "breeze_tool_task",
                asyncio.create_task(
                    self._background_synth(text, instruction, cfg)
                ),
            )
            return "SUCCESS"

        if not await self.plugin.begin_synth():
            return self.plugin.fail_reply("语音合成失败")
        try:
            wav = await self.plugin.synth_wav(text, instruction, cfg)
        finally:
            self.plugin.end_synth()
        if not wav:
            return self.plugin.fail_reply("语音合成失败")

        # chain_result() 是同步方法，实际发送交给 event.send()
        await ev.send(ev.chain_result([Comp.Record.fromBase64(self.plugin.client.to_base64(wav))]))
        ev.set_extra("breeze_voice_silence_mode", True)
        return "SUCCESS"
