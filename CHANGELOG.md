# 更新日志

本文件记录插件的重要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-11-02

**不再依赖 ffmpeg**，插件包从 63 MB 降到 1.6 MB（AstrBot Cloud 上限 16 MB）。

### 变更

- 语速与音量改为**纯 numpy 实现**，不再调用外部 `ffmpeg.exe`
  - 语速：WSOLA 波形相似叠加（参考 ffmpeg `af_atempo` 思路），**音调不变**
  - 音量：线性增益，`gain = 10 ** (dB / 20)`（对应 ffmpeg `af_volume`）
- 移除插件自带的 `bin/ffmpeg.exe`（61.5 MB）与 `ffmpeg_path` 配置项
- 后处理改为在事件循环外一次性完成，耗时从「起进程 50~100 ms」降到 **4~13 ms**
- 新增后处理日志：每次处理都记录效果、时长变化与耗时

### 修复

- 后处理失败时**保留原始音频**，绝不因调节失败而丢消息

### 移除

- `utils/ffmpeg_audio.py`（由 `utils/audio_ops.py` 取代）
- 配置项 `ffmpeg_path`

## [1.0.0] - 2026-10-31

首个版本。为 AstrBot 接入本地 Breeze TTS 2，把文字回复变成语音。

### 新增

- **本地推理**：对接 Breeze TTS 2（GGUF / Vulkan），无需 API Key、不按字计费
- **音色克隆**：使用服务端已保存的克隆音色（`voice_id` 可配）
- **精确语速控制**：`speed_ratio` 经 ffmpeg `atempo` 实现，正数更快、负数更慢
- **精确音量控制**：`loudness_rate` 经 ffmpeg `volume` 实现，封顶 ±6 dB（实测误差 0.00 dB）
- **自动语气指令**：合成前额外调一次 LLM 分析情绪并生成配音指令（可关）
- **三档风格预设**：`normal` / `gentle` / `whisper`
- **LLM 工具** `clone_tts`：让模型主动"开口说话"
- **并发退避**：服务忙（409）自动重试 3 次（0.3 / 0.6 / 1.2 秒）
- **优雅降级**：服务不可用时只记日志，绝不吞掉文字回复
- **启动自检**：检查服务连通性、ffmpeg 可用性、音色是否存在

### 已知限制

- 语速只能提高约 1.5 倍、降低到 0.5 倍（ffmpeg `atempo` 区间）
- 语气指令对"更快/更响"几乎无效——模型只会往"更慢/更轻"走
- 带语气指令（`cfg_scale > 1`）时服务端的并发槽消耗翻倍
