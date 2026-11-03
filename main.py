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
from astrbot.core.star.filter.command import GreedyStr

from .service.launcher import IS_WINDOWS, ServiceManager
from .tts_api.breeze_api import BreezeTTSClient
from .utils import audio_ops
from .webapi import register_web_apis

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

        # ---- 开关与触发 ----
        self.enable_tts = _as_bool(cfg.get("enable_tts", True), True)
        self.tts_probability = _as_int(cfg.get("tts_probability", 80), 80, 0, 100)
        self.min_length = _as_int(cfg.get("min_length", 5), 5, 1, 1000)
        self.max_length = _as_int(cfg.get("max_length", 50), 50, 1, 5000)
        self.blocked_words = [str(w) for w in (cfg.get("blocked_words") or []) if str(w).strip()]

        # ---- 服务与音色 ----
        # 服务地址只由「服务端口」决定（127.0.0.1:端口），连接/启动/探测共用；
        # 兼容老配置：以前手填过完整地址（breeze_api）的话仍优先用它
        self.service_port = _as_int(cfg.get("service_port", 9000), 9000, 1, 65535)
        legacy_api = str(cfg.get("breeze_api") or "").strip()
        self.breeze_api = legacy_api or f"http://127.0.0.1:{self.service_port}"
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
        self.pitch_shift = _as_int(cfg.get("pitch_shift", 0), 0, -12, 12)

        # ---- LLM 工具与文字抑制 ----
        self.enable_tts_tool_minmax = _as_bool(cfg.get("enable_tts_tool_minmax", True), True)
        self.enable_llm_response = _as_bool(cfg.get("enable_llm_response", False), False)

        # ---- /tts 指令与失败兜底 ----
        self.enable_tts_command = _as_bool(cfg.get("enable_tts_command", True), True)
        self.fail_reply_text = str(cfg.get("fail_reply_text") or "").strip()

        # ---- 本地服务（模型目录 / 起停控制）----
        self.model_dir = str(cfg.get("model_dir") or "").strip()
        self.model_file = str(cfg.get("model_file") or "").strip()
        # 并发槽位不再是设置项：默认 4（老配置里手填过的值仍然尊重）
        self.service_parallel = _as_int(cfg.get("service_parallel", 4), 4, 1, 16)
        self.auto_start_service = _as_bool(cfg.get("auto_start_service", True), True)
        self._auto_start_task = None
        self.service = ServiceManager(
            plugin_dir=os.path.dirname(os.path.abspath(__file__)),
            model_dir=self.model_dir,
            model_file=self.model_file,
            port=self.service_port,
            parallel=self.service_parallel,
        )

        # ---- 客户端 ----
        self.client = BreezeTTSClient(
            api_base=self.breeze_api,
            voice=self.voice_id,
            cfg_scale=self.cfg_scale,
            seed=self.seed,
            timeout=self.timeout,
        )

        # ---- 启动自检（只记日志，不阻塞）----
        if not audio_ops._HAS_NUMPY and (self.speed_ratio or self.pitch_shift):
            logger.warning("[BreezeTTS] 环境缺少 numpy，语速/音调后处理将不生效")

        # clone_tts 工具随着插件启用，不再单独开关
        self.context.add_llm_tools(BreezeTTSTool(plugin=self))

        # 「服务控制」页的接口（选目录 / 起停服务）
        try:
            register_web_apis(self)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[BreezeTTS] 服务控制接口注册失败：{exc}")

    def _cfg(self, key: str, default=""):
        """读一条原始配置值（给设置页展示用，不做类型转换）。"""
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    async def initialize(self):
        """插件启用时：按配置的端口检查服务，并识别音色"""
        # ---- ① 服务连通性：直接连「服务端口」，不再四处探测 ----
        ok = await self.client.health()
        if not ok:
            logger.warning(
                f"[BreezeTTS] 已启用，但连不上服务 {self.breeze_api}"
                "（确认服务确实跑在这个端口，可在「服务端口」里改；"
                "或到「服务控制」页点一下启动）；语音会静默跳过，文本回复不受影响"
            )
            if self.auto_start_service:
                # 保留引用，防止任务被垃圾回收（asyncio 只持弱引用）
                self._auto_start_task = asyncio.create_task(self._auto_start_service())
            return

        # ---- ② 音色：没填就用服务端第一个 ----
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

    async def terminate(self):
        # 注意：插件停用/重载时故意不结束本地服务，避免「改个配置服务就断了」。
        # 需要停服务请用「服务控制」页的按钮或 /breeze stop。
        logger.info("[BreezeTTS] 插件已停用/卸载（本地服务保持运行，可在服务控制页手动停止）")

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
        # 惰性兜底：插件启用时服务可能还没起来，那时没识别到音色；
        # 这里再试一次，避免必须重载插件才能出声。
        if not self.voice_id:
            try:
                detected = await self.client.detect_voice()
                if detected:
                    self.voice_id = detected
                    logger.info(f"[BreezeTTS] 自动识别到音色：{detected}")
            except Exception:  # noqa: BLE001
                pass

        instruction = await self.resolve_instruction(event, text)
        # 没有指令时用 cfg=1 换速度；有指令时用配置值（默认 2.0）
        cfg = self.cfg_scale if instruction else 1.0

        wav = await self.client.synth(
            text=text,
            instructions=instruction or None,
            speed_ratio=self.speed_ratio,
            pitch_shift=self.pitch_shift,
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
                # 合成失败：文字照发，并按配置补一句提示（比如"嗓子哑了"）
                logger.warning("[BreezeTTS] 语音合成失败，本次只发文字")
                if self.fail_reply_text:
                    try:
                        res = event.get_result()
                        if res is not None and res.chain:
                            res.chain.append(Comp.Plain(f"\n{self.fail_reply_text}"))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[BreezeTTS] 追加失败提示出错：{exc}")
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

    # ------------------------------------------------------------ /tts 指令
    @filter.command("tts")
    async def cmd_tts(self, event: AstrMessageEvent, text: GreedyStr):
        """把一段文字念成语音：/tts 你好呀"""
        if not self.enable_tts_command:
            return

        content = str(text or "").strip()
        if not content:
            yield event.plain_result("用法：/tts 要念的内容")
            return

        if self.hit_blocked(content):
            yield event.plain_result("这句话里有不适合朗读的内容，换个说法吧")
            return

        if len(content) > self.max_length:
            yield event.plain_result(
                f"太长了（{len(content)} 字），单次最多 {self.max_length} 字"
            )
            return

        logger.info(f"[BreezeTTS] /tts 手工合成：{content[:30]}")
        audio_b64 = await self.synthesize(event, content)
        if not audio_b64:
            logger.warning("[BreezeTTS] /tts 合成失败")
            yield event.plain_result(self.fail_reply_text or "（刚刚嗓子有点干,没说出话…稍后再试）")
            return

        yield event.chain_result([Comp.Record.fromBase64(audio_b64)])

    # ------------------------------------------- 本地服务开关（仅管理员）
    @filter.command("breeze")
    async def cmd_breeze(self, event: AstrMessageEvent, action: GreedyStr):
        """本地模型服务开关：/breeze status | start | stop | restart"""
        if not event.is_admin():
            yield event.plain_result("只有管理员能操作本地服务")
            return

        cmd = str(action or "").strip().lower()
        loop = asyncio.get_running_loop()

        if cmd in ("", "status", "状态"):
            state = await loop.run_in_executor(None, self.service.status)
            yield event.plain_result(self._format_status(state))
            return

        if cmd in ("start", "启动", "开"):
            yield event.plain_result("正在启动本地模型…（首次加载约 5~10 秒）")
            result = await loop.run_in_executor(None, self.service.start)
            yield event.plain_result(str(result.get("message") or "没有输出"))
            if result.get("ok"):
                self.breeze_api = f"http://127.0.0.1:{self.service_port}"
                self.client.api_base = self.breeze_api
                await self._refresh_voice()
            return

        if cmd in ("stop", "停止", "关"):
            result = await loop.run_in_executor(None, self.service.stop)
            yield event.plain_result(str(result.get("message") or "没有输出"))
            return

        if cmd in ("restart", "重启"):
            yield event.plain_result("正在重启本地模型…")
            result = await loop.run_in_executor(None, self.service.restart)
            yield event.plain_result(str(result.get("message") or "没有输出"))
            return

        yield event.plain_result(
            "用法：/breeze status 看状态；/breeze start 启动；"
            "/breeze stop 停止；/breeze restart 重启"
        )

    @staticmethod
    def _format_status(state: dict) -> str:
        healthy = bool(state.get("healthy"))
        running = bool(state.get("running"))
        if healthy:
            head = "在线"
        elif running:
            head = "进程在，但 /health 没响应"
        else:
            head = "离线"
        lines = [
            "【Breeze TTS 本地服务】",
            f"状态：{head}",
            f"端口：{state.get('port')}（PID {state.get('pid') or '—'}）",
        ]
        if state.get("model_name"):
            lines.append(f"模型：{state['model_name']}（{state.get('model_size') or '?'}）")
        else:
            lines.append("模型：没找到 .gguf")
        voices = state.get("voices") or []
        if voices:
            shown = ", ".join(voices[:6]) + ("…" if len(voices) > 6 else "")
            lines.append(f"音色：{len(voices)} 个 → {shown}")
        if state.get("model_dir"):
            lines.append(f"目录：{state['model_dir']}")
        else:
            lines.append("目录：还没设置（去「服务控制」页选一个）")
        if not state.get("exe"):
            lines.append("⚠ 目录里没找到 breeze-server.exe")
        return "\n".join(lines)

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
            pitch_shift=self.plugin.pitch_shift,
            cfg_scale=cfg,
        )
        if not wav:
            return self.plugin.fail_reply_text or "语音合成失败"

        # chain_result() 是同步方法，返回 MessageEventResult；发送要交给 event.send()
        ev = context.context.event
        await ev.send(ev.chain_result([Comp.Record.fromBase64(self.plugin.client.to_base64(wav))]))

        if not self.plugin.enable_llm_response:
            ev.set_extra("breeze_voice_silence_mode", True)

        return "SUCCESS"
