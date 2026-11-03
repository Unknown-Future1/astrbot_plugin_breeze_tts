# 🐋 Breeze TTS 克隆语音 — AstrBot 插件

<p align="center">
  使用<strong>本地模型克隆的自定义音色</strong>，让你的<strong>大肥鱼</strong>进行语音回复。<br/>
  支持克隆音色、语速与音调调节，<strong>无需任何 API Key、不按字计费、不依赖外网</strong>。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.2.0-blue" alt="version" />
  <img src="https://img.shields.io/badge/AstrBot-%E2%89%A5%204.29.3-orange" alt="astrbot" />
  <img src="https://img.shields.io/badge/backend-Breeze%20TTS%202-4f9cf9" alt="backend" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license" />
</p>

---

## 本插件不依赖 AstrBot 的 TTS 功能

不需要在 AstrBot 里配置任何 TTS 服务提供商。只要装好插件、部署 Breeze 模型、把本地 Breeze 服务跑起来、克隆完音色，填上音色 ID 就能用。

---

## 为什么选 Breeze TTS 2

- **体积小**：Q4_K 仅 2.4 GB，最大的 F16 也才 5.9 GB
- **显存友好**：Q4_K 约 3 GB 显存就能跑，6 GB 显卡也带得动
- **效果可观**：中英双语、音色克隆、语气导演、文字造音色，一套齐全
- **完全本地**：无 API Key、不按字计费、断网也能用

![各量化版本的体积与显存占用](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/model-vram.png)

> 📌 上图来自官方文档，标的是**模型本身**的显存占用。
> **实测提醒**：本机（独显模式）跑 Q8_0 时 `nvidia-smi` 显示约 **6.5 GB** ——
> 多出来的部分是壁纸引擎、桌面合成等系统级占用，不是模型本身吃掉的。
> 显存吃紧、或要和 ComfyUI 共存时，建议选 Q6_K / Q4_K。

**模型地址：** <https://github.com/HoppouAI/Breeze-TTS-2.cpp>

---

## ✨ 功能特性

| 特性 | 说明 |
| --- | --- |
| 🆓 **完全免费** | 本地推理，无 API Key、不按字计费、不依赖外网 |
| 🎙️ 音色克隆 | 3~10 秒参考音频 + 准确转写即可复刻音色 |
| 🎭 语气控制 | LLM 分析情绪自动生成配音指令，也可手写 |
| ⚡ **精确语速** | 内置 WSOLA 时间伸缩，音调不变，实测误差 0%（不是靠提示词"约一下"） |
| 🎚️ **音调调节** | ±12 半音，**变调不改时长**；FFT 实测频率误差 < 0.6% |
| 🎲 概率触发 | 0~100% 可调，控制语音出现频率 |
| 📏 长度门限 | 防止过短语怪、过长等太久 |
| 🚫 屏蔽词 | 不适合朗读的内容自动跳过 |
| 🛠️ LLM 工具 | `clone_tts` 让模型主动说话（随插件启用，无单独开关） |
| 🖥️ **服务控制页** | 面板里选模型目录，一键启动/停止本地服务，看实时状态和日志 |
| ⌨️ **/tts 点播** | `/tts 要念的话` 手工合成；`/breeze start`、`/breeze stop` 管理员可远程起停服务 |
| 🛡️ 优雅降级 | 服务挂了只记日志，**绝不吞掉文字回复**，并补一句「嗓子哑了」 |
| 🔁 并发退避 | 服务忙（409）自动重试 3 次（0.3/0.6/1.2 s） |

---

## 📋 前置要求

1. **AstrBot** ≥ v4.29.3 已安装并正常运行
2. **本地 Breeze TTS 2 服务**已部署并运行（默认连 `127.0.0.1:9000`，端口可在设置里改）
3. **至少一个已保存的音色**（服务端 `voices` 目录里存在）
4. **无需外部程序** —— 语速/音调用内置的 numpy 实现（AstrBot 自带 numpy），装上就能用

### 部署模型与服务（一次性）

#### 最省事：直接让 DSH 代劳

![在 DSH 里让它下载项目、选量化版、配好 9000 端口](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/dsh-deploy.png)

---

## 🛠 启动本地模型服务

### 首选：插件里的「服务控制」页

AstrBot 面板 → 插件 → **Breeze TTS 克隆语音** → 页面 **「服务控制」**：

1. 点「浏览…」选一次 **模型目录**（放着 `*.gguf` 和 `breeze-server.exe` 的那一层），
   点「保存并应用」
2. 之后就是一排按钮：**启动服务 / 停止服务 / 重启**，外加实时状态和日志

页面上能看到端口、PID、当前模型、音色数量、服务日志尾巴，也能直接点开 WebUI。

> [!TIP]
> - 服务是**后台进程**，关掉网页也不会停，重启 AstrBot 也不受影响
> - 「**随插件自动拉起**」开关与设置里的同名选项联动：**关**=立刻停止服务并禁止自动拉起（显存释放，失败会回滚设置）；**开**=保存后自动把服务带起来
> - 目录里有哪些 `.gguf` 会列出来，多个量化版本可以下拉选

### 也支持脚本（不用再复制到模型目录）

| 文件 | 用途 |
| --- | --- |
| `start-breeze.cmd` | 双击启动服务；也接受目录参数 |
| `start-breeze.ps1` | 实际逻辑，被上面那个调用 |
| `stop-breeze.cmd` | 双击停止服务；`stop-breeze.cmd 9000` 只停占着 9000 的那个 |

从 1.3.0 起它们**可以原地留在插件目录里用**，只要把模型目录当参数传进去：

```bat
:: 路径换成你的模型目录
start-breeze.cmd "H:\Breeze-TTS-2.cpp"
```

脚本会在这个目录里找：

```
<模型目录>\
├── breeze-tts-2-q8_0.gguf                            # 模型权重（两层内都能找到）
├── breeze-tts-2-v0.1.6-windows-x64-vulkan\           # 预编译服务（两层内都能找到）
│   └── breeze-server.exe
└── voice\                                            # 音色目录（--voices-dir 指向这里）
    └── my-voice.breeze
```

| 脚本能力 | 说明 |
| --- | --- |
| 目录定位 | `-Root "路径"` 或第一个位置参数；不给就用脚本自己所在目录 |
| 自动识别 | `*.gguf`（多个时弹菜单并记住选择）、`breeze-server.exe`（子目录也行） |
| 音色目录 | 优先 `<目录>\voice`，没有音色就用服务程序同目录 |
| 端口冲突 | 只清理自家的 `breeze-server`，别人的程序不动 |
| 关窗即停 | Job Object 守护，窗口一关系统回收进程，显存立刻释放 |

更多细节见 [`scripts/README.md`](scripts/README.md)。

服务起来后可以打开模型可视化网页（`http://127.0.0.1:9000/`），「服务控制」页上也有直达按钮：

![模型可视化网页](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/webui.png)

---

## 🎙️ 克隆音色（一次性）

对着 DSH 说这句话，它会帮你转码、上传、注册成音色：

![让 DSH 帮你克隆音色](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/dsh-clone.png)

三个占位符怎么填：

| 占位符 | 填什么 | 注意 |
| --- | --- | --- |
| `【何处】` | 音频文件路径 | **模型只接受 WAV**；mp3 / m4a 等要先转码（在 DSH 里说一句就会帮你转成 24kHz 单声道 WAV） |
| `【音色名】` | 音色的名字 | **只能用英文 / 数字 / `-` / `_`** |
| `【…所有字】` | 音频里人物说的每一个字 | **必须逐字一致**，错一个字韵律就跑偏 |

> 💡 参考音频建议 **3~10 秒**、单人、无背景噪声。
> Breeze 没有语音识别能力，**原文必须你自己提供**。

音色会保存为 `<name>.breeze`（约 800 字节/秒参考音频），**存入 `--voices-dir` 指定的 `voice\` 文件夹中**，重启服务后自动加载。

---

## ⚙️ 配置说明

安装后在 AstrBot 管理面板的 **插件配置** 中找到 `Breeze TTS 克隆语音`。

### 常用参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_tts` | `bool` | `true` | 总开关，关闭后插件完全不介入 |
| `tts_probability` | `int` | `80` | 触发概率 0~100，`0`=永不触发，`100`=必定触发 |
| `min_length` | `int` | `5` | 最短合成字数，短于它不合成 |
| `max_length` | `int` | `50` | 最长合成字数，**超过直接跳过**（不截断，避免半句话） |
| `blocked_words` | `list` | `[]` | 命中任一屏蔽词则跳过语音，文字照发 |
| `voice_id` | `string` | `""` | 音色名。**留空自动用服务端第一个音色**；有多个时建议明确填写 |
| `style_preset` | `string` | `normal` | `normal` / `gentle` / `whisper` 三档静态预设 |
| `speed_ratio` | `int` | `0` | **正数=更快，负数=更慢**，范围 -50~100（±50 即 0.5/1.5 倍） |
| `pitch_shift` | `int` | `0` | **音调（半音）**，-12 ~ +12；正数升高、负数降低，**时长不变** |

### 进阶参数（一般不用改）

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `cfg_scale` | `float` | `2.0` | 引导强度 1.0~4.0，**只在有语气指令时生效** |
| `seed` | `int` | `42` | 随机种子，固定则同文本生成同音频 |
| `timeout` | `int` | `30` | 单次请求超时（秒） |
| `enable_auto_instruction` | `bool` | `true` | 每次合成前多调一次 LLM 分析情绪生成配音指令 |
| `llm_recognition` | `string` | `""` | 生成指令所用模型，留空=当前会话模型 |
| `custom_instruction` | `text` | `""` | 手写指令，**优先级最高**，填了就用它 |
| `enable_tts_tool_minmax` | `bool` | `true` | 工具调用是否也受长度限制 |
| `enable_llm_response` | `bool` | `false` | `false`=群里只看到语音；`true`=文字+语音都发 |
| `fail_reply_text` | `string` | `刚刚嗓子有点干,没说出话…等会再说` | 合成失败时补发的提示；**留空则不提示** |

> `/tts` 指令默认开启，**不再出现在设置面板**。想关掉的话，手动往插件配置里加 `"enable_tts_command": false` 即可。

### 本地服务（模型目录 / 一键起停）

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `model_dir` | `string` | `""` | 模型目录：放着 `*.gguf` 和 `breeze-server.exe` 的那层。到「服务控制」页「浏览…」选一次即可 |
| `model_file` | `string` | `""` | 留空=自动用唯一的 `*.gguf`；目录里有多个量化版本时填文件名指定 |
| `service_port` | `int` | `9000` | **服务端口**：连接（`127.0.0.1:端口`）、一键启动、探测都用它。WebSocket/看板端口会自动占用「端口+1」（默认 9000 就占 9001），选端口时留出相邻一位 |
| `service_parallel` | `int` | `4` | 传给 `--parallel`，显存紧张就改 2 |
| `auto_start_service` | `bool` | `true` | **默认开启**：AstrBot 启动或改完设置保存时若探测不到服务，自动按上面的目录和端口拉起（后台窗口）；不想开机占显存就关掉 |

### 语气指令的优先级

```
自定义指令 custom_instruction（填了就用，最高）
      ↓ 否则
自动指令（LLM 分析这句话的情绪，默认开启）
      ↓ 否则
静态预设 style_preset（normal / gentle / whisper）
```

**没有指令时自动用 `cfg_scale = 1.0`**（最快）；有指令时才用配置值（默认 2.0）。

---

## 📖 使用方式

### 手工念一段话

```
/tts 今天天气不错，一起去海边吧
```

不受触发概率影响，走的是同一套合成链路（语速、音调、语气指令都生效）。

### 在群里管服务（仅管理员）

```
/breeze status     看服务状态（端口、PID、模型、音色数）
/breeze start      启动本地模型
/breeze stop       停止服务并释放显存
/breeze restart    重启
```

按钮坏了还有个能用的入口 —— 出门在外也能让机器人自己把模型拉起来。

### 让 AI 自己开口

插件启用后，AI 就拥有 `clone_tts` 工具，会在「用户想听声音」时主动调用：

```json
{"text": "你好呀，我是溟月", "context_texts": "轻快亲切，语速稍慢"}
```

工具执行成功后，若 `enable_llm_response = false`，会通过**零宽空格**抑制重复文字 —— 群里只看到一条语音气泡。

**实际效果：**

![QQ 里收到语音回复](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/qq-effect.png)

**日志里能看到生成耗时（生成速度一并可见）：**

![生成速度日志](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/log-speed.png)

---

## 🔧 常见问题排查

### 插件已启用但没有语音

1. 确认 Breeze 服务在跑：`curl http://127.0.0.1:9000/health` 应返回 `{"status":"ok",...}`；
   没跑就去「**服务控制**」页点「启动服务」（或 `/breeze start`）
2. 确认日志里有 `[BreezeTTS] 已启用，服务在线`；若显示"连不上服务"，先启动服务
3. 检查 `voice_id` 是否存在于 `GET /v1/voices` 列表里（拼错会 404）
4. 检查 `tts_probability` 是否过低、文本长度是否超出 `min_length`~`max_length`

### 日志报 409

```
[BreezeTTS] 服务持续繁忙(409)，放弃本次合成
```

服务并发槽被打满。**启动服务时把 `--parallel` 调到 4 或更高**（带 guidance 的流占两个槽）。

### 语速/音调不起作用

日志里会有：

```
[BreezeTTS] 环境缺少 numpy，语速/音调后处理将不生效
```

AstrBot 默认自带 numpy，正常情况不会出现；若真出现，重装 AstrBot 的运行环境即可。

语音生成完全不受影响，只是这两项调节失效。

### 克隆出来不像原声

1. 参考音频必须是 **WAV**（mp3 会被拒）
2. `ref_text` 必须与音频内容**逐字一致**，错一个字韵律就会跑偏
3. 参考音频建议 3~10 秒、单人、无背景噪声

### 音色列表里看不到自己的音色

服务启动时**没有带 `--voices-dir`**，默认只读工作目录下的 `voices/`。
用附带的启动脚本（已固定 `--voices-dir`），或手动补上参数。

---

## 🔗 相关链接

- 📘 [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- 🐙 [Breeze-TTS-2.cpp（本地推理）](https://github.com/HoppouAI/Breeze-TTS-2.cpp)
- 🤗 [BreezeBlue/breeze-tts-2（模型权重）](https://huggingface.co/BreezeBlue/breeze-tts-2)
- ⚠️ **模型权重为非商用许可**，个人使用没问题，商用需自行确认授权

---

## 📄 许可证

本插件代码采用 [MIT License](LICENSE) 开源。

> **注意**：MIT 只覆盖本插件的代码。
> Breeze TTS 2 的**模型权重**使用 BreezeBlue Research 非商用许可，与本项目许可证无关。
