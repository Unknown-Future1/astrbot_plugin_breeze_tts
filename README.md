# 🐋 Breeze TTS 克隆语音 — AstrBot 插件

<p align="center">
  使用<strong>本地模型克隆的自定义音色</strong>，让你的<strong>大肥鱼</strong>进行语音回复。<br/>
  支持克隆音色、语速与音调调节，<strong>无需任何 API Key、不按字计费、不依赖外网</strong>。
</p>
<p align="center">
  <img src="https://img.shields.io/badge/version-1.3.5-blue" alt="version" />
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

![各量化版本的体积与显存占用](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@6b616361e8ba242607e83ddfc938468aaa8a0fa0/docs/model-vram.png)

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
| 📏 长度门限 | 防止过短语怪；长文本交给分段合成（单条防呆 600 字） |
| 🚫 屏蔽词 | 不适合朗读的内容自动跳过 |
| 🛠️ LLM 工具 | `clone_tts` 让模型主动说话（随插件启用，无单独开关） |
| 🖥️ **服务控制页** | 面板里选模型目录，一键启动/停止本地服务，看实时状态和日志 |
| ⌨️ **/tts 点播** | `/tts 要念的话` 手工合成；可加 `[语气]` 或 `语气\|` 自己定情绪 |
| 🛡️ 优雅降级 | 服务挂了只记日志，**绝不吞掉文字回复**，并补一句「嗓子哑了」 |
| 💤 **空闲自动休眠** | 最后一条语音后 N 分钟自动停服务释放显存；下次语音自动拉起（约 3 秒） |
| 🔥 **冷启动预热** | 服务离线时，在 LLM 思考期间后台拉起，隐藏约 3 秒启动耗时 |
| ✂️ **长回复分段** | 超过 30 字按句号切分、段间 350 ms、可以并发合成 |
| 🧠 **资源预检** | 服务启动前检查显存与内存（按量化自动算阈值），不足只发文字 |
| 🔁 并发退避 | 服务忙（409）自动重试 3 次（0.3/0.6/1.2 s） |

---

## 📋 前置要求

1. **AstrBot** ≥ v4.29.3 已安装并正常运行
2. **本地 Breeze TTS 2 服务**已部署（默认连 `127.0.0.1:9000`；没在跑也没关系，插件可以自动拉起）
3. **至少一个已保存的音色**（服务端 `voices` 目录里存在）
4. **无需外部程序** —— 语速/音调用内置的 numpy 实现（AstrBot 自带 numpy），装上就能用

### 部署本地模型

#### 最省事：直接让 DSH 代劳

![在 DSH 里让它下载项目、选量化版、配好 9000 端口](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@4061292250eec9661a6e0fe0b9489f003339f837/docs/dsh-deploy.png)

---

## 🚀 初步设置（装完插件 4 步）

1. **装插件**：AstrBot 面板 → 插件市场 → 搜索「Breeze TTS 克隆语音」（或 `astrbot_plugin_breeze_tts`），点安装、等它装完

   ![插件市场里搜索并安装](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@4061292250eec9661a6e0fe0b9489f003339f837/docs/market-install.png)

2. **填模型目录**：插件设置里「本地模型目录」填放刚刚下载完模型的那个文件夹，例如 `D:\Breeze-TTS-2.cpp`

   - 只填文件夹即可
   - 目录里有多个量化版本时，在「模型文件」里选一个；留空则自动取第一个

   > 还没填模型目录也不会报错：插件只发文字、语音静默跳过，日志里会提示去补配置。

3. **确认端口**：默认 `9000`，可以自己改，但不要填被其他服务占用的端口。

   ![① 保存后探测端口](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@33ceb9dfb76e9e916b6cb19a85915bb66210c17d/docs/start-1-detect.png)

   填好上面的内容后点**保存**，然后**移步到「服务控制」页启动服务**：

   - 刚保存完时，插件会先花几秒探测你设置的端口，所以期间服务页的状态还没出来，属正常

     ![② 自动拉起模型、等待就绪](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@33ceb9dfb76e9e916b6cb19a85915bb66210c17d/docs/start-2-loading.png)

     ![③ 服务已在线](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@33ceb9dfb76e9e916b6cb19a85915bb66210c17d/docs/start-3-online.png)

   - 启动服务

   - 就绪后状态变成**在线**，这一步才算完成

4. **设置音色**：默认自动使用服务端**第一个音色**，不设置也能出声，所以这一步可以先不管。
   想换音色、或者服务端还一个音色都没有时，看下面「如何设置音色」。

#### 🎙️ 如何设置音色

**已经有音色**：打开「服务控制」页，在「音色」下拉里选一个即可（选「自动」= 用服务端第一个）。选中立即生效，不用重启服务。

**还没有音色**：用「服务控制」页的**音色工作台**自己克隆一个——

1. 先把服务启动起来（页面右上角显示**在线**）
2. 在「克隆新音色」里选一段 **3~20 秒的 WAV**，填上它说的**每一个字**和名字
3. 点「克隆并保存」，新音色立刻出现在音色库和上面的「音色」下拉里

![克隆新音色](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@770f603195c43d0d31cc439861784fd69dba7da7/docs/service-studio.png)

---

## 🖥 服务页面（服务控制）

AstrBot 面板 → 插件 → **Breeze TTS 克隆语音** → 页面「服务控制」。

![音色库与克隆](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@770f603195c43d0d31cc439861784fd69dba7da7/docs/service-studio.png)

![试听](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@770f603195c43d0d31cc439861784fd69dba7da7/docs/service-preview.png)

### 服务状态

页面每 3 秒自动刷新一次，状态和进程信息不用手动点。

| 区域 | 内容 |
| --- | --- |
| 顶部 | 在线 / 离线、当前服务地址 |
| 状态卡片 | 端口、进程（PID）、当前模型 |
| 启动参数 | 目录里有多个 `.gguf` 时，选本次加载哪个 |
| 音色 | 服务端已加载的音色（含「自动」）；选中**立即生效**，不用重启服务 |
| 按钮 | 启动 / 关闭 / 重启；关闭会立刻释放显存 |

### 🎨 音色工作台（服务在线时才显示）

| 区块 | 怎么用 |
| --- | --- |
| 音色库 | 列出全部音色并标注 `使用中` / `已保存` / `仅内存`，每行可试听、改描述、删除 |
| 克隆新音色 | 选 **WAV** + 逐字转录 + 名字 → 保存；「听一下原音频」可以先确认素材没错 |
| 试听 | 文本 + 风格预设 / 自定义语气指令 + 语速 / 音调，选好音色点「合成试听」直接播放 |

> ⚠️ 克隆**只接受 WAV**：Breeze 的解码器只认 WAV，mp3 / m4a / flac 会被拒绝（先转成 24kHz 单声道 WAV 再传）。

> 💡 试听不会改插件设置；合成出来的音频可以用播放器右侧「⋮」下载。

> 💡 删除音色会同时移除内存与 `.breeze` 文件；文件被服务占用时会提示重启服务后再删。

> 🖥 需要模型可视化或原始克隆面板时，点页面底部「在浏览器中打开模型界面」，会用系统默认浏览器打开 Breeze 自带的 WebUI。

> [!TIP]
>
> - 服务是**后台进程**：关掉网页、重启 AstrBot 都不影响它继续跑
> - **模型目录、模型文件、服务端口、自动拉起**都在「插件设置」里，页面上不重复放输入框
> - 关掉「启用插件时自动拉起服务」后，重启 AstrBot 不会再拉起模型（把显存留给别的事）
> - 音色保存在「模型目录」下的音色文件夹里（`voices`，或已有的 `voice`），重启服务后自动加载
> - 服务的运行状态与日志保存在 `data/plugin_data/astrbot_plugin_breeze_tts/runtime/`（重装插件不会丢）
> - 想用脚本手动起停（不经过面板）：见 [`scripts/README.md`](scripts/README.md)

---

## 📖 使用方式

### -让 AI 自己开口

插件启用后，AI 就拥有 `clone_tts` 工具，会在「用户想听声音」时主动调用：

**实际效果：**

![QQ 里收到语音回复](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@6b616361e8ba242607e83ddfc938468aaa8a0fa0/docs/qq-effect.png)

**日志里能看到生成耗时（生成速度一并可见）：**

![生成速度日志](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@4061292250eec9661a6e0fe0b9489f003339f837/docs/log-speed.png)

### -手工/试听念一段话

```
/tts 今天天气不错，一起去海边吧
```

想自己定语气就加前缀，不加则交给 LLM 自动分析这句话的情绪：

```
/tts [温柔一点]今天天气不错      ← 方括号 + 内容，不加空格
/tts 温柔一点|今天天气不错       ← 竖线分隔（正文里别带竖线）
```

不受触发概率影响，走的是同一套合成链路（语速、音调、语气指令都生效）。
日志里会打印实际用的语气：`[BreezeTTS] 语气指令：… | 文本：…`

![QQ 里 /tts 的实际效果](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@4061292250eec9661a6e0fe0b9489f003339f837/docs/tts-effect.png)

![方括号写法的 /tts](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@f7ae45b/docs/tts-style-bracket.png)

![竖线写法的 /tts](https://gcore.jsdelivr.net/gh/Unknown-Future1/astrbot_plugin_breeze_tts@f7ae45b/docs/tts-style-pipe.png)

---

## 💤 空闲休眠与按需启动

服务默认**不常驻**：最后一条语音结束后，超过N分钟时（默认 5 分钟）没有新的语音合成，
就自动停止本地服务，释放约 3.4 GB 显存。

| 规则 | 说明 |
| --- | --- |
| 计时起点 | 最后一次合成结束 |
| 顺延 | 期间有新合成会顺延，但**最多只再顺延一个等待时间**（设 5 分钟 → 最长 10 分钟） |
| 到点仍在合成 | 等这次合成结束**立即停止** |
| 下次语音 | 自动拉起服务（约 3 秒），并在此前**在 LLM 思考期间提前预热** |
| 手动启动 | 面板点「启动/重启」后，计时从那一刻重新开始 |
| 资源不足 | 启动前检查显存与内存，不足时最多等 5 秒，仍不足只发文字 |

## ✂️ 长回复分段合成

回复超过 30 字时，插件会：

1. 优先在句号处切分（单句过长才退到逗号）
2. 每段不超过 30 字
3. 段与段之间插 350 ms 停顿
4. 默认并发 2 段合成，按原顺序拼接

| 情况 | 行为 |
| --- | --- |
| ≤30 字 | 整段合成，不受影响 |
| 30~60 字 | 切成 2 段并发，约快 30% |
| 超过 600 字 | 直接跳过语音（防呆） |
| 关闭分段 | 恢复整段合成；长文本可能超时，建议同时调小上限 |

> 分段切在标点上，普通陈述接缝较自然；带强烈情绪时句间音高可能跳 5~6 半音，属正常现象。

## 🔧 常见问题排查

### 服务页显示「已停止」，是不是坏了

不是。这是**空闲自动休眠**：超过 `idle_stop_minutes` 没有语音，服务会主动停止释放显存。
下次发语音会自动拉起（约 3 秒）。想让它常驻，把 `idle_stop_minutes` 设大即可。

### 这条消息为什么没有语音

按顺序检查：

1. 触发概率没中（`tts_probability`）
2. 文本太短（`min_length`）或太长（超 600 字防呆）
3. 命中屏蔽词
4. **显存/内存不足**：日志会写「资源不足…跳过本次语音」
5. 服务启动失败：日志会写「启动失败」

### 插件已启用但没有语音

1. 确认 Breeze 服务在跑：`curl http://127.0.0.1:9000/health` 应返回 `{"status":"ok",...}`；
   没跑就去「**服务控制**」页点「启动服务」
2. 确认日志里有 `[BreezeTTS] 已启用，服务在线`；若显示"连不上服务"，先启动服务或者重启服务，如果不行，关闭插件再开就行
3. 检查 `voice_id` 是否存在于 `GET /v1/voices` 列表里（拼错会 404）
4. 检查 `tts_probability` 是否过低、文本是否短于 `min_length` 或超过 600 字防呆上限

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
| `fail_reply_texts` | `list` | 4 条默认文案 | 合成失败时**随机**补发一句；留空则不提示 |

> `/tts` 指令默认开启，**不再出现在设置面板**。想关掉的话，手动往插件配置里加 `"enable_tts_command": false` 即可。

### 长回复分段合成（`split_synthesis` 分组）

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `split_enabled` | `bool` | `true` | 启用分段合成 |
| `split_min_chars` | `int` | `30` | 超过这个字数才分段 |
| `split_max_chars` | `int` | `30` | 每段最大字数（句号优先，单句过长退到逗号） |
| `split_gap_ms` | `int` | `350` | 段间停顿（毫秒） |
| `split_parallel` | `bool` | `true` | 启用并发优化（内部固定 2 段，服务槽不足时自动串行） |

### 本地服务（模型目录 / 一键起停）

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `model_dir` | `string` | `""` | 模型目录：放着 `*.gguf` 和 `breeze-server.exe` 的那层（也可直接粘 `.gguf` 文件路径） |
| `model_file` | `string` | `""` | 留空=自动用唯一的 `*.gguf`；目录里有多个量化版本时填文件名指定 |
| `service_port` | `int` | `9000` | **服务端口**：连接（`127.0.0.1:端口`）、一键启动、探测都用它。WebSocket/看板端口会自动占用「端口+1」（默认 9000 就占 9001），选端口时留出相邻一位 |
| `auto_start_service` | `bool` | `true` | **默认开启**：AstrBot 启动或改完设置保存时若探测不到服务，自动按上面的目录和端口拉起（后台窗口）；不想开机占显存就关掉 |
| `service_warmup` | `bool` | `false` | 启动时运行官方预热；**不推荐开启**（多等约 13 秒，首音只快约 0.2 秒） |
| `idle_stop_minutes` | `int` | `5` | 空闲多少分钟停止服务；整数，最低 1，推荐 5 |

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
