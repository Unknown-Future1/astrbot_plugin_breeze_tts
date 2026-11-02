"""
astrbot_plugin_breeze_tts —— 用本地 Breeze TTS 2 模型把文字回复转成语音

功能概览：
  · 总开关 / 概率 / 长度门槛 / 屏蔽词
  · 装饰结果钩子（核心）
  · 语音说过后按需用零宽空格抑制文字（voice_silence_mode）
  · LLM 工具 clone_tts
  · 自动语气指令（多调一次 LLM 分析情绪）
  · numpy 实现语速（WSOLA）/ 音量后处理，不依赖外部程序
"""

from __future__ import annotations

import random
import re

from pydantic import Field
from pydantic.dataclasses import dataclass

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config import AstrBotConfig
from astrbot.core.provider.entities import LLMResponse

from .tts_api.breeze_api import BreezeTTSClient
from .utils import audio_ops

ZERO_WIDTH = "\u200b"

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


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return default


@register(
    "astrbot_plugin_breeze_tts",
    "未知の未来",
    "使用本地部署Breeze TTS克隆的自定义音色，无需任何 API Key。让你的大肥鱼能进行语音回复，支持语音触发概率、语速与音量等功能调节。插件头像dsh画的hhh",
    "1.0.0",
)
class BreezeTTSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        cfg = config

        # ---- 开关与触发 ----
        self.enable_tts = _as_bool(cfg.get("enable_tts", True), True)
        self.tts_probability = _as_int(cfg.get("tts_probability", 80), 80, 0, 100)
        self.min_length = _as_int(cfg.get("min_length", 5), 5, 1, 1000)
        self.max_length = _as_int(cfg.get("max_length", 50), 50, 1, 5000)
        self.blocked_words = [str(w) for w in (cfg.get("blocked_words") or []) if str(w).strip()]

        # ---- 服务与音色 ----
        self.breeze_api = str(cfg.get("breeze_api") or "http://127.0.0.1:9000").strip()
        self.voice_id = str(cfg.get("voice_id") or "").strip()
        self.cfg_scale = _as_float(cfg.get("cfg_scale", 2.0), 2.0, 1.0, 4.0)
        self.seed = _as_int(cfg.get("seed", 42), 42, 0, 2**31 - 1)
        self.timeout = _as_int(cfg.get("timeout", 30), 30, 5, 300)

        # ---- 语气指令 ----
        self.enable_auto_instruction = _as_bool(cfg.get("enable_auto_instruction", True), True)
        self.llm_recognition = str(cfg.get("llm_recognition") or "").strip()
        self.style_preset = str(cfg.get("style_preset") or "normal").strip() or "normal"
        self.custom_instruction = str(cfg.get("custom_instruction") or "").strip()

        # ---- 数值控制 ----
        self.speed_ratio = _as_int(cfg.get("speed_ratio", 0), 0, -50, 100)
        self.loudness_rate = _as_int(cfg.get("loudness_rate", 0), 0, -100, 100)

        # ---- LLM 工具与文字抑制 ----
        self.enable_llm_tool = _as_bool(cfg.get("enable_llm_tool", True), True)
        self.enable_tts_tool_minmax = _as_bool(cfg.get("enable_tts_tool_minmax", True), True)
        self.enable_llm_response = _as_bool(cfg.get("enable_llm_response", False), False)

        # ---- 客户端 ----
        self.client = BreezeTTSClient(
            api_base=self.breeze_api,
            voice=self.voice_id,
            cfg_scale=self.cfg_scale,
            seed=self.seed,
            timeout=self.timeout,
        )

        # ---- 启动自检（只记日志，不阻塞）----
        if not self.voice_id:
            logger.warning("[BreezeTTS] voice_id 为空，语音合成会失败（404 未知音色）")
        if not audio_ops._HAS_NUMPY and (self.speed_ratio or self.loudness_rate):
            logger.warning("[BreezeTTS] 环境缺少 numpy，语速/音量后处理将不生效")

        if self.enable_llm_tool:
            self.context.add_llm_tools(BreezeTTSTool(plugin=self))

    async def initialize(self):
        ok = await self.client.health()
        if ok:
            logger.info(f"[BreezeTTS] 已启用，服务在线：{self.breeze_api}，音色：{self.voice_id}")
            # 音色存在性自检（拼错时给明确提示，而不是等 404）
            voices = await self.client.list_voices()
            if voices and self.voice_id and self.voice_id not in voices:
                logger.warning(
                    f"[BreezeTTS] 音色 '{self.voice_id}' 不在服务端列表中，"
                    f"现有音色：{', '.join(voices)}"
                )
        else:
            logger.warning(
                f"[BreezeTTS] 已启用，但连不上服务 {self.breeze_api}"
                "（请先运行 start-breeze.cmd）；语音会静默跳过，文本回复不受影响"
            )

    async def terminate(self):
        logger.info("[BreezeTTS] 插件已停用/卸载")

    # ------------------------------------------------------------------ 工具
    def probability(self, percent: float) -> bool:
        p = max(0.0, min(100.0, float(percent)))
        return random.random() < (p / 100)

    def hit_blocked(self, text: str) -> bool:
        if not self.blocked_words:
            return False
        low = text.lower()
        return any(w.lower() in low for w in self.blocked_words)

    def preset_instruction(self) -> str:
        return STYLE_PRESETS.get(self.style_preset, "")

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

    async def synthesize(self, event: AstrMessageEvent, text: str) -> str | None:
        """生成语音，返回 base64 字符串；失败返回 None（调用方降级）"""
        instruction = await self.resolve_instruction(event, text)
        # 没有指令时用 cfg=1 换速度；有指令时用配置值（默认 2.0）
        cfg = self.cfg_scale if instruction else 1.0

        wav = await self.client.synth(
            text=text,
            instructions=instruction or None,
            speed_ratio=self.speed_ratio,
            loudness_rate=self.loudness_rate,
            cfg_scale=cfg,
        )
        if not wav:
            return None
        return self.client.to_base64(wav)

    # ------------------------------------------------------- 核心钩子：装饰结果
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        try:
            if not self.enable_tts:
                return

            result = event.get_result()
            if result is None or not result.chain:
                logger.debug("[BreezeTTS] 无结果，跳过")
                return

            # 只处理 LLM 产出（跳过指令返回等）
            if not result.is_llm_result():
                return

            text_parts = [
                c.text for c in result.chain if hasattr(c, "text") and c.text
            ]
            if not text_parts:
                logger.debug("[BreezeTTS] 无文本，跳过")
                return

            text = "".join(text_parts).strip()
            if not text:
                return

            # 超长直接跳过（不截断，避免念出半句话）
            if len(text) > self.max_length:
                logger.debug(
                    f"[BreezeTTS] 文本 {len(text)} 字超过上限 {self.max_length}，跳过"
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
            if not self.probability(self.tts_probability):
                logger.debug("[BreezeTTS] 未命中概率，跳过语音")
                return

            logger.info(f"[BreezeTTS] 开始合成：{text[:30]}")
            audio_b64 = await self.synthesize(event, text)
            if not audio_b64:
                # 与原插件一致：只记日志，文本照发
                logger.warning("[BreezeTTS] 语音合成返回空数据，跳过本次语音回复")
                return

            result2 = event.get_result()
            if result2 is None:
                logger.warning("[BreezeTTS] 合成完成但 result 已失效，跳过")
                return

            record = Comp.Record.fromBase64(audio_b64)
            if self.enable_llm_response:
                # 文字 + 语音都发：把语音追加到原消息链后面
                result2.chain.append(record)
            else:
                # 只发语音：整条链替换，文字自然就没了
                #（注意：这条路径不需要 voice_silence_mode，替换链已经足够）
                result2.chain = [record]

        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BreezeTTS] on_decorating_result 异常：{exc}")

    # ------------------------------------------------ 钩子：抹掉重复的文字
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

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        text = str(kwargs.get("text") or "").strip()
        if not self.plugin:
            return "插件未正确初始化"
        if not self.plugin.enable_llm_tool:
            return "LLM 工具未启用"
        if not text:
            return "文本不能为空"

        if self.plugin.enable_tts_tool_minmax:
            if len(text) > self.plugin.max_length:
                return f"文本长度超过上限 {self.plugin.max_length}，跳过语音合成"
            if len(text) < self.plugin.min_length:
                return f"文本长度低于下限 {self.plugin.min_length}，跳过语音合成"

        instruction = str(kwargs.get("context_texts") or "").strip()
        if not instruction:
            instruction = self.plugin.custom_instruction or self.plugin.preset_instruction()

        cfg = self.plugin.cfg_scale if instruction else 1.0
        wav = await self.plugin.client.synth(
            text=text,
            instructions=instruction or None,
            speed_ratio=self.plugin.speed_ratio,
            loudness_rate=self.plugin.loudness_rate,
            cfg_scale=cfg,
        )
        if not wav:
            return "语音合成失败"

        # chain_result() 是同步方法，返回 MessageEventResult；发送要交给 event.send()
        ev = context.context.event
        await ev.send(ev.chain_result([Comp.Record.fromBase64(self.plugin.client.to_base64(wav))]))

        if not self.plugin.enable_llm_response:
            ev.set_extra("breeze_voice_silence_mode", True)

        return "SUCCESS"
