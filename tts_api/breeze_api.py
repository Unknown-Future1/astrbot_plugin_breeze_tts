"""Breeze TTS 本地服务 API 封装。

只做三件事：
  1. 调 OpenAI 兼容的 /v1/audio/speech 拿 WAV 字节
  2. 409 并发退避重试
  3. 健康探活

语速/音量的后处理在 utils/audio_ops.py 里（纯 numpy 实现，不依赖 ffmpeg）。
"""

from __future__ import annotations

import asyncio
import base64

import aiohttp

from astrbot.api import logger

from ..utils import audio_ops

DEFAULT_API = "http://127.0.0.1:9000"
DEFAULT_VOICE = "xiaojing-voice"


class BreezeTTSClient:
    """Breeze TTS HTTP 客户端"""

    def __init__(
        self,
        api_base: str = DEFAULT_API,
        voice: str = DEFAULT_VOICE,
        cfg_scale: float = 2.0,
        seed: int = 42,
        timeout: int = 30,
    ) -> None:
        self.api_base = (api_base or DEFAULT_API).rstrip("/")
        self.voice = voice
        self.cfg_scale = cfg_scale
        self.seed = seed
        self.timeout = timeout

    # ---------------------------------------------------------------- 探活
    async def health(self, timeout: float = 3.0) -> bool:
        """服务是否就绪（/health 返回 status=ok）"""
        try:
            to = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=to) as session:
                async with session.get(f"{self.api_base}/health") as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json(content_type=None)
                    return bool(data) and data.get("status") == "ok"
        except Exception:  # noqa: BLE001
            return False

    # ---------------------------------------------------------------- 生成
    async def synth(
        self,
        text: str,
        instructions: str | None = None,
        speed_ratio: int = 0,
        loudness_rate: int = 0,
        cfg_scale: float | None = None,
    ) -> bytes | None:
        """
        合成语音，返回处理后 WAV 的字节；失败返回 None（调用方负责降级）。

        cfg_scale: 覆盖默认引导强度。没有语气指令时传 1.0 可显著提速。
        """
        text = (text or "").strip()
        if not text:
            return None

        wav = await self._request_wav(text, instructions, cfg_scale)
        if wav is None:
            return None

        # 数值后处理（纯 numpy：WSOLA 时间伸缩 + 线性增益，不依赖 ffmpeg）
        if speed_ratio or loudness_rate:
            processed = audio_ops.process(wav, speed_ratio, loudness_rate)
            if processed is not None:
                wav = processed

        return wav

    async def _request_wav(
        self, text: str, instructions: str | None, cfg_scale: float | None = None
    ) -> bytes | None:
        """带 409 退避的实际请求"""
        payload = {
            "input": text,
            "response_format": "wav",
            "stream_format": "audio",
            "cfg_scale": self.cfg_scale if cfg_scale is None else cfg_scale,
            "seed": self.seed,
        }
        if self.voice:
            payload["voice"] = self.voice
        if instructions:
            payload["instructions"] = instructions

        delays = (0.3, 0.6, 1.2)  # 409 退避节奏（秒）
        attempts = len(delays) + 1

        for attempt in range(attempts):
            try:
                to = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(timeout=to) as session:
                    async with session.post(
                        f"{self.api_base}/v1/audio/speech", json=payload
                    ) as resp:
                        if resp.status == 200:
                            wav = await resp.read()
                            if not wav:
                                logger.warning("[BreezeTTS] 服务返回空音频")
                                return None
                            return wav

                        if resp.status == 409:
                            body = await resp.text()
                            if attempt < len(delays):
                                logger.debug(
                                    f"[BreezeTTS] 服务忙(409)，{delays[attempt]}s 后重试 "
                                    f"({attempt + 1}/{attempts}) {body[:80]}"
                                )
                                await asyncio.sleep(delays[attempt])
                                continue
                            logger.warning("[BreezeTTS] 服务持续繁忙(409)，放弃本次合成")
                            return None

                        body = await resp.text()
                        logger.error(
                            f"[BreezeTTS] 合成失败 HTTP {resp.status}: {body[:200]}"
                        )
                        return None

            except asyncio.TimeoutError:
                logger.error(f"[BreezeTTS] 请求超时（{self.timeout}s）")
                return None
            except aiohttp.ClientError as exc:
                logger.error(f"[BreezeTTS] 网络错误: {exc}")
                return None
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[BreezeTTS] 请求异常: {exc}")
                return None

        return None

    # ---------------------------------------------------------------- 工具
    async def list_voices(self) -> list[str]:
        """列出服务端已保存的音色 ID（用于启动自检）"""
        try:
            to = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=to) as session:
                async with session.get(f"{self.api_base}/v1/voices") as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
                    return [str(v.get("id")) for v in data if isinstance(v, dict)]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def to_base64(wav: bytes) -> str:
        """转成 AstrBot 的 Record.fromBase64 需要的字符串"""
        return base64.b64encode(wav).decode("utf-8")
