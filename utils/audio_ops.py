"""
音频后处理：语速（时间伸缩）与音调（变调不变速）。

为什么不用 ffmpeg：
    插件只需要「WAV → 变速/变调 → WAV」这一条链路，
    而 ffmpeg 是 60 MB 级的多媒体全家桶，为了两个滤镜背 60 MB 不值。

实现原理：
    语速 —— WSOLA（Waveform Similarity Overlap-Add，波形相似叠加）。
        参考 ffmpeg 的 `af_atempo` 滤镜（libavfilter/af_atempo.c）思路：
        按固定分析步长取帧，在允许的偏移范围内找与上一帧最相似的片段，
        再用汉宁窗做重叠相加，从而在「不改变音调」的前提下伸缩时间轴。

    音调 —— 重采样改频率（对应 ffmpeg 的 `asetrate`），再用 WSOLA 把时长拉回来。

对外接口：
    process(wav_bytes, speed_ratio, pitch_shift) -> wav_bytes | None
"""

from __future__ import annotations

import struct

from astrbot.api import logger

# ----------------------------------------------------------------- 可选依赖
try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


# ----------------------------------------------------------------- 参数常量
WSOLA_FRAME = 1024   # 分析帧长（采样点），约 42 ms @ 24 kHz
WSOLA_TOLERANCE = 256  # 相似度搜索半径（互补搜索范围）
WSOLA_STEP = 8       # 搜索步长：越小越准、越慢


# ================================================================ WAV 读写

def _parse_wav(data: bytes) -> tuple[list, int]:
    """
    解析 WAV 字节流，返回 (采样列表, 采样率)。

    只做插件真正会遇到的情况：PCM 16bit，以及 8/24/32bit 的兜底转换。
    参考 ffmpeg 的 wav demuxer 行为：按 chunk 逐个跳过，直到找到 fmt / data。
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是合法的 WAV 数据（缺少 RIFF/WAVE 头）")

    pos = 12
    fmt = None
    payload = None

    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        body_start = pos + 8
        body_end = min(body_start + chunk_size, len(data))

        if chunk_id == b"fmt ":
            fmt = data[body_start:body_end]
        elif chunk_id == b"data":
            payload = data[body_start:body_end]
            break

        # chunk 按 2 字节对齐
        pos = body_start + chunk_size + (chunk_size % 2)

    if fmt is None or payload is None:
        raise ValueError("WAV 缺少 fmt 或 data 块")

    audio_format, channels, sample_rate = struct.unpack_from("<HHI", fmt, 0)
    bits = struct.unpack_from("<H", fmt, 14)[0]

    if audio_format not in (1, 3):  # 1 = PCM, 3 = IEEE float
        raise ValueError(f"不支持的 WAV 编码格式 {audio_format}")

    if channels != 1:
        raise ValueError(f"只支持单声道，收到 {channels} 声道")

    samples = _decode_samples(payload, bits, audio_format)
    return samples, sample_rate


def _decode_samples(payload: bytes, bits: int, audio_format: int) -> list:
    """把原始字节解码成 -1.0 ~ 1.0 之间的浮点采样。"""
    if audio_format == 3:  # float
        if bits == 32:
            count = len(payload) // 4
            return list(struct.unpack_from(f"<{count}f", payload, 0))
        raise ValueError(f"只支持 32bit 浮点 WAV，收到 {bits}bit")

    if bits == 16:
        count = len(payload) // 2
        return [v / 32768.0 for v in struct.unpack_from(f"<{count}h", payload, 0)]
    if bits == 8:
        return [(v - 128) / 128.0 for v in payload]
    if bits == 24:
        out = []
        for i in range(0, len(payload) - 2, 3):
            v = int.from_bytes(payload[i:i + 3], "little", signed=True)
            out.append(v / 8388608.0)
        return out
    if bits == 32:
        count = len(payload) // 4
        return [v / 2147483648.0 for v in struct.unpack_from(f"<{count}i", payload, 0)]

    raise ValueError(f"不支持的位深 {bits}bit")


def _build_wav(samples, sample_rate: int) -> bytes:
    """把浮点采样打包成 PCM 16bit 单声道 WAV 字节流。"""
    if _HAS_NUMPY:
        arr = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm = (arr * 32767.0).astype("<i2").tobytes()
    else:
        pcm = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
        )

    channels = 1
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)

    header = b"RIFF"
    header += struct.pack("<I", 36 + data_size)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                          byte_rate, block_align, bits)
    header += b"data"
    header += struct.pack("<I", data_size)
    return header + pcm


# ================================================================ WSOLA 变速

def _wsola(x, factor: float):
    """
    时间伸缩：输出长度 ≈ 输入长度 × factor。

    factor > 1 → 变慢（变长）；factor < 1 → 变快（变短）。
    音调保持不变（这是与"直接重采样"的本质区别）。

    对应 ffmpeg 的 af_atempo.c：
        - 分析步长固定、合成步长按 factor 缩放
        - 每帧在 ±tolerance 内做相似度搜索，取相关度最高的位置
        - 汉宁窗重叠相加，最后按窗函数累加和归一化
    """
    n = x.shape[0]
    frame = WSOLA_FRAME
    hop_a = frame // 2
    hop_s = int(round(hop_a * factor))

    if n < frame * 2 or hop_s < 1:
        return x  # 太短，直接原样返回

    window = np.hanning(frame).astype(np.float32)
    out_len = int(n * factor) + frame
    out = np.zeros(out_len, dtype=np.float32)
    weight = np.zeros(out_len, dtype=np.float32)

    pos_a = 0
    pos_s = 0
    first = True

    while pos_a + frame < n and pos_s + frame < out_len:
        if first:
            # 第一帧无需搜索，直接放
            seg = x[pos_a:pos_a + frame]
            first = False
        else:
            # 在输出已有内容的尾部作为参考，找输入里最相似的片段
            ref = out[pos_s:pos_s + frame]
            best_score = -1e18
            best_shift = 0
            for shift in range(-WSOLA_TOLERANCE, WSOLA_TOLERANCE + 1, WSOLA_STEP):
                p = pos_a + shift
                if p < 0 or p + frame >= n:
                    continue
                score = float(np.dot(x[p:p + frame], ref))
                if score > best_score:
                    best_score = score
                    best_shift = shift
            p = pos_a + best_shift
            p = max(0, min(p, n - frame - 1))
            seg = x[p:p + frame]

        out[pos_s:pos_s + frame] += seg * window
        weight[pos_s:pos_s + frame] += window
        pos_a += hop_a
        pos_s += hop_s

    valid = weight > 1e-6
    out[valid] /= weight[valid]

    target = int(round(n * factor))
    out = out[:target]

    # 响度补偿：重叠相加时相邻帧会有相位抵消，压缩得越狠电平掉得越多
    # （实测 ×2.0 时约 -0.8 dB）。这里按输入 RMS 还原整体电平，
    # 保证「变速不改响度」。
    try:
        src_rms = float(np.sqrt(np.mean(x ** 2)))
        out_rms = float(np.sqrt(np.mean(out ** 2)))
        if src_rms > 1e-6 and out_rms > 1e-6:
            gain = src_rms / out_rms
            out = out * float(max(0.5, min(2.0, gain)))
    except Exception:  # noqa: BLE001
        pass

    return out


def speed_factor(speed_ratio: int) -> float:
    """
    把配置里的 speed_ratio 换算成「时长倍率」（WSOLA 的 factor）。

    约定：正数 = 更快，负数 = 更慢（与用户直觉一致）。
    例：+30 → 播放速度 1.3 倍 → 时长缩到 1/1.3 ≈ 0.77 倍
        -30 → 播放速度 0.7 倍 → 时长拉到 1/0.7 ≈ 1.43 倍
    """
    ratio = max(-50, min(100, int(speed_ratio or 0)))
    playback = 1.0 + ratio / 100.0          # 播放速度倍率
    playback = max(0.5, min(2.0, playback))
    return 1.0 / playback                    # 时长倍率




# ================================================================ 音调（变调不变速）

PITCH_SEMITONE_LIMIT = 12   # 最多 ±12 个半音（一个八度）


def _resample(x, ratio: float):
    """
    线性插值重采样。

    ratio > 1 → 采样变稀 → 时长缩短、音调升高（像快放）
    ratio < 1 → 采样变密 → 时长变长、音调降低（像慢放）
    """
    n = x.shape[0]
    out_len = int(round(n / ratio))
    if out_len < 1 or n < 2:
        return x
    dst_idx = np.arange(out_len, dtype=np.float64) * ratio
    np.clip(dst_idx, 0, n - 1, out=dst_idx)
    src_idx = np.arange(n, dtype=np.float64)
    return np.interp(dst_idx, src_idx, x).astype(np.float32)


def pitch_ratio(semitones: int) -> float:
    """
    半音数 → 频率倍率。

    每 12 个半音翻一倍：+12 → 2.0，-12 → 0.5，+7 → 1.498（纯五度）。
    """
    n = max(-PITCH_SEMITONE_LIMIT, min(PITCH_SEMITONE_LIMIT, int(semitones or 0)))
    return 2.0 ** (n / 12.0)


def apply_pitch(x, semitones: int):
    """
    变调但**不改时长**（对应 ffmpeg 的 `asetrate` + `atempo` 组合）。

    做法：
        ① 重采样  → 音调变化、时长也变了（那不是我们要的）
        ② WSOLA 拉伸回原时长 → 音调保持在新的高度

    副作用：重采样会连共振峰一起搬，±12 个半音内可接受，
    超过就会有"花栗鼠 / 低沉怪"的感觉（这也是所有纯变速变调方案的共性）。
    """
    if not semitones:
        return x

    ratio = pitch_ratio(semitones)
    if abs(ratio - 1.0) < 0.001:
        return x

    shifted = _resample(x, ratio)
    if shifted.shape[0] < 2:
        return x

    # 重采样后时长变成 len/ratio，用 factor=ratio 拉回原长度
    restored = _wsola(shifted, ratio)

    # 长度对齐到原始长度，避免尾部多/少几十个采样
    n = x.shape[0]
    if restored.shape[0] > n:
        restored = restored[:n]
    elif restored.shape[0] < n:
        restored = np.pad(restored, (0, n - restored.shape[0]))
    return restored


# ================================================================ 调度入口

def _safe_int(value, default: int = 0) -> int:
    """把配置值安全地转成 int（可能是 str / float / None）。"""
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        logger.warning(f"[BreezeTTS] 后处理参数无法解析为数字：{value!r}，按 0 处理")
        return default


def process(wav_bytes: bytes, speed_ratio: int = 0,
            pitch_shift: int = 0) -> bytes | None:
    """
    对一段 WAV 做语速 / 音调后处理。

    参数：
        wav_bytes      : 输入 WAV 字节（单声道，24 kHz 为主）
        speed_ratio    : -50 ~ 100，正数更快、负数更慢；0 表示不变
        pitch_shift    : -12 ~ +12 半音，正数升高、负数降低；0 表示不变

    返回：
        处理后的 WAV 字节；如果没有需要处理的项，原样返回输入；
        出错时返回 None（调用方保留原始音频，绝不能因此丢消息）。
    """
    if not wav_bytes:
        return None

    # 防御：配置可能是字符串（"30"）或浮点（30.0），统一转成 int
    speed_ratio = _safe_int(speed_ratio)
    pitch_shift = _safe_int(pitch_shift)

    need_speed = speed_ratio != 0
    need_pitch = pitch_shift != 0
    if not need_speed and not need_pitch:
        return wav_bytes

    if not _HAS_NUMPY:
        logger.warning("[BreezeTTS] 环境缺少 numpy，跳过语速/音调后处理")
        return wav_bytes

    try:
        import time as _time
        started = _time.perf_counter()

        samples, sample_rate = _parse_wav(wav_bytes)
        arr = np.asarray(samples, dtype=np.float32)
        original_seconds = arr.shape[0] / float(sample_rate or 24000)

        notes = []

        # ---- 音调（变调不变速）：先做，因为它只改频率不改时长 ----
        if need_pitch:
            arr = apply_pitch(arr, pitch_shift)
            ratio = pitch_ratio(pitch_shift)
            notes.append(f"音调{pitch_shift:+d}半音（×{ratio:.3f}）")

        # ---- 语速（时间伸缩，音调不变）----
        if need_speed:
            factor = speed_factor(speed_ratio)
            if abs(factor - 1.0) > 0.005:
                arr = _wsola(arr, factor)
                notes.append(f"语速{speed_ratio:+d}（时长×{factor:.2f}）")
            else:
                notes.append(f"语速{speed_ratio:+d}（改变过小，忽略）")


        out = _build_wav(arr, sample_rate)
        elapsed_ms = (_time.perf_counter() - started) * 1000
        new_seconds = arr.shape[0] / float(sample_rate or 24000)

        logger.info(
            f"[BreezeTTS] 后处理完成：{', '.join(notes)}"
            f" | {original_seconds:.2f}s → {new_seconds:.2f}s | 耗时 {elapsed_ms:.0f} ms"
        )
        return out

    except ValueError as exc:
        # 数据本身不合法（不是单声道、位深不支持…）：保留原音频
        logger.warning(f"[BreezeTTS] WAV 后处理跳过：{exc}")
        return wav_bytes
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[BreezeTTS] WAV 后处理异常：{exc}")
        return wav_bytes


def describe_effects(speed_ratio: int = 0, pitch_shift: int = 0) -> str:
    """给日志/自检用：把当前配置翻译成一句话。"""
    parts = []
    if speed_ratio:
        parts.append(f"语速×{1.0 / speed_factor(speed_ratio):.2f}")
    if pitch_shift:
        parts.append(f"音调{pitch_shift:+d}半音")
    return "、".join(parts) if parts else "无后处理"
