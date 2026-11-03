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

**模型相关内容请移步：** <https://github.com/HoppouAI/Breeze-TTS-2.cpp>

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
| ⌨️ **/tts 点播** | `/tts 要念的话` 手工合成，不受触发概率影响 |
| 🛡️ 优雅降级 | 服务挂了只记日志，**绝不吞掉文字回复**，并补一句「嗓子哑了」 |
| 🔁 并发退避 | 服务忙（409）自动重试 3 次（0.3/0.6/1.2 s） |

---

## 📋 前置要求

1. **AstrBot** ≥ v4.29.3 已安装并正常运行
2. **本地 Breeze TTS 2 服务**已部署（默认连 `127.0.0.1:9000`；没在跑也没关系，插件可以自动拉起）
3. **至少一个已保存的音色**（服务端 `voices` 目录里存在）
4. **无需外部程序** —— 语速/音调用内置的 numpy 实现（AstrBot 自带 numpy），装上就能用

### 部署模型与服务（一次性）

#### 最省事：直接让 DSH 代劳

![在 DSH 里让它下载项目、选量化版、配好 9000 端口](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@1c63257/docs/dsh-deploy.png)

---

## 🚀 初步设置（装完插件 4 步）

1. **装插件**：AstrBot 面板 → 插件市场 → 搜索「Breeze TTS 克隆语音」（或 `astrbot_plugin_breeze_tts`），点安装、等它装完

   ![插件市场里搜索并安装](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@1c63257/docs/market-install.png)

2. **填模型目录**：插件设置里「本地模型目录」填放刚刚下载完模型的那个文件夹，例如 `D:\Breeze-TTS-2.cpp`

   - 只填文件夹即可
   - 目录里有多个量化版本时，在「模型文件」里选一个；留空则自动取第一个

   > 还没填模型目录也不会报错：插件只发文字、语音静默跳过，日志里会提示去补配置。

3. **确认端口**：默认 `9000`，可以自己改，但不要填被其他服务占用的端口。

   填好上面的内容后点**保存**：面板会自动热重载插件；如果探测不到服务，就会**自动把模型拉起来**（首次约 10~15 秒），看服务页面是否显示在线。

   ![保存后服务显示在线](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@1c63257/docs/service-online.png)

4. **音色（可跳过）**：默认自动使用服务端**第一个音色**，不设置也能出声，所以这一步可以先不管。
   想换音色，或者服务端还一个音色都没有时，再照下面做一个新的：

#### 🎙️ 克隆音色（一次性）

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

## 🖥 服务页面（服务控制）

![服务控制页](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@1c63257/docs/service-page.png)

AstrBot 面板 → 插件 → **Breeze TTS 克隆语音** → 页面「服务控制」。

| 区域 | 内容 |
| --- | --- |
| 顶部状态 | 在线 / 离线、服务地址、文字说明 |
| 状态卡片 | 端口、进程（PID）、当前模型 |
| 启动参数 | 目录里有多个 `.gguf` 时，在这里选本次加载哪个 |
| 音色 | 服务端已加载的音色（含「自动」）；选中**立即生效**，不用重启服务、不用重载插件 |
| 按钮 | 启动服务 / 关闭服务 / 重启；关闭会立刻释放显存 |
| 模型可视化 | 内嵌 Breeze 的模型网页；服务在线时自动展开，离线时收起（也可手动折叠） |

> [!TIP]
>
> - 服务是**后台进程**：关掉网页、重启 AstrBot 都不影响它继续跑
> - **模型目录、模型文件、服务端口、自动拉起**都在「插件设置」里，页面上不重复放输入框
> - 关掉「启用插件时自动拉起服务」后，重启 AstrBot 不会再拉起模型（把显存留给别的事）
> - 从旧版本升级：服务地址现在只由「服务端口」决定；服务不在 9000 的话，把端口重填一次即可
> - 想用脚本手动起停（不经过面板）：见 [`scripts/README.md`](scripts/README.md)

---

## 📖 使用方式

### 手工/试听念一段话

```
/tts 今天天气不错，一起去海边吧
```

不受触发概率影响，走的是同一套合成链路（语速、音调、语气指令都生效）。

![QQ 里 /tts 的实际效果](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@1c63257/docs/tts-effect.png)

### 让 AI 自己开口

插件启用后，AI 就拥有 `clone_tts` 工具，会在「用户想听声音」时主动调用：

```json
{"text": "你好呀，我是溟月", "context_texts": "轻快亲切，语速稍慢"}
```

工具执行成功后，若 `enable_llm_response = false`，会通过**零宽空格**抑制重复文字 —— 群里只看到一条语音气泡。

**实际效果：**

![QQ 里收到语音回复](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@55f345f/docs/qq-effect.png)

**日志里能看到生成耗时（生成速度一并可见）：**

![生成速度日志](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@1c63257/docs/log-speed.png)

---

## 🔧 常见问题排查

### 插件已启用但没有语音

1. 确认 Breeze 服务在跑：`curl http://127.0.0.1:9000/health` 应返回 `{"status":"ok",...}`；
   没跑就去「**服务控制**」页点「启动服务」
2. 确认日志里有 `[BreezeTTS] 已启用，服务在线`；若显示"连不上服务"，先启动服务或者重启服务，如果不行，关闭插件再开就行
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
| `model_dir` | `string` | `""` | 模型目录：放着 `*.gguf` 和 `breeze-server.exe` 的那层（也可直接粘 `.gguf` 文件路径） |
| `model_file` | `string` | `""` | 留空=自动用唯一的 `*.gguf`；目录里有多个量化版本时填文件名指定 |
| `service_port` | `int` | `9000` | **服务端口**：连接（`127.0.0.1:端口`）、一键启动、探测都用它。WebSocket/看板端口会自动占用「端口+1」（默认 9000 就占 9001），选端口时留出相邻一位 |
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
