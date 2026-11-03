# 这 3 个脚本：不用再复制到模型目录了

这 3 个脚本是用来**一键启动/停止本地 Breeze TTS 服务**的。
从 1.3.0 起，它们**可以原地留在插件目录里用**，只要告诉它模型在哪：

```bat
:: 把路径换成你自己的模型目录（放着 .gguf 和 breeze-server.exe 的那层）
start-breeze.cmd "D:\Breeze-TTS-2.cpp"
```

也可以不带参数、把它复制到模型目录里用（老办法仍然有效）。

> 最省事的办法其实是插件里的**「服务控制」页**：
> AstrBot 面板 → 插件 → Breeze TTS → 服务控制，选一次目录，
> 然后就有「启动 / 停止 / 重启 / 看日志 / 打开 WebUI」按钮了，连脚本都不用点。

---

## 📦 这 3 个文件是什么

| 文件 | 用途 |
| --- | --- |
| `start-breeze.cmd` | **双击这个**启动服务（会把参数原样转给 ps1） |
| `start-breeze.ps1` | 实际逻辑，被上面那个调用；也能单独跑 |
| `stop-breeze.cmd` | **双击这个**停止服务（可选传端口，更精准） |

---

## 🚀 三种用法

### 1) 带目录参数（推荐，不用复制）

```bat
cd /d <插件目录>\scripts
start-breeze.cmd "D:\Breeze-TTS-2.cpp"
```

`-Root` 具名写法等价：

```bat
start-breeze.cmd -Root "D:\Breeze-TTS-2.cpp"
```

也可以直接调 ps1（参数更多）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 "D:\Breeze-TTS-2.cpp"
```

### 2) 不带参数（老办法）

把 3 个脚本复制到模型目录，双击 `start-breeze.cmd`。
不带 `-Root` 时，脚本就以**自己所在的目录**为模型目录。

### 3) 让插件帮你起停

插件配置里填好「本地模型目录」，或到「服务控制」页点一次「浏览…」，
之后直接用页面按钮启动/停止 —— 后台运行、不弹控制台，关掉网页也继续跑。

---

## 🧭 脚本会在这个目录里找什么

给了 `-Root`（或第一个参数是路径）就按那个目录找，否则退回脚本自己的位置：

```
<模型目录>\
├── breeze-tts-2-q8_0.gguf                        ← 模型（两层内都能找到）
├── breeze-tts-2-v0.1.6-windows-x64-vulkan\        ← 服务程序（两层内都能找到）
│   └── breeze-server.exe
├── voice\                                         ← 音色目录（优先用这个）
│   ├── ds-voice.breeze
│   └── xiaojing-voice.breeze
└── logs\                                          ← 脚本自己写日志的地方
```

- **模型 / 服务程序**：在目录下两层内搜索，所以 `breeze-server.exe` 放在版本子目录里也没问题
- **音色目录**：优先 `<目录>\voice`；那里没有 `*.breeze` 就改用 `breeze-server.exe` 同目录
- **多个模型**：会列菜单让你选，并把选择记在 `last-model.txt`
- 目录不存在会直接报错，不会乱猜

---

## 🖥 启动后你看到什么

```
============================================
       Breeze TTS 2  local server
       port 9000  |  parallel 4
============================================

[1/5] 识别模型与服务程序
      ok  服务: .\breeze-tts-2-v0.1.6-windows-x64-vulkan\breeze-server.exe
      ok  模型: breeze-tts-2-q8_0.gguf
          3.32 GB  ≈6.6 GB 显存（估）
      ok  音色 2 个: ds-voice, xiaojing-voice

[2/5] 检查端口 9000
      ok  端口空闲

[3/5] 准备进程守护
      ok  Job Object 已建立：关窗口时服务会被系统自动回收

[4/5] 启动服务
      PID 12345
      ok  已纳入守护，关窗口即释放显存

[5/5] 等待服务就绪

============================================
      服务已就绪
      http://127.0.0.1:9000/
============================================

关闭本窗口 = 服务停止并释放显存
```

---

## 🔁 怎么停止

三种都行：

1. **关掉那个黑窗口** —— 服务被系统自动回收，显存立刻释放
2. **双击 `stop-breeze.cmd`** —— 结束 `breeze-server.exe`
3. **`stop-breeze.cmd 9000`** —— 只结束占着 9000 端口、且进程名是 `breeze-server` 的那个（更精准）

---

## ❓ 常见问题

### 双击 `start-breeze.cmd` 一闪而过

在**命令行**里跑一次，看清楚哪一步报错：

```bat
start-breeze.cmd "D:\你的模型目录"
```

### 提示「指定的目录不存在」

路径里空格没关系，但要用**英文引号**包住；网络路径请先用资源管理器打开一次。

### 提示「找不到 .gguf 模型文件」/「找不到 breeze-server.exe」

- 确认 `-Root` 指向的是**放 `.gguf` 的那一层**
- 服务程序可以在它的子目录里（两层内都能搜到）
- 模型文件名必须以 `.gguf` 结尾

### 提示「端口 9000 被占用」

- 是 `breeze-server` → 按 `Y`，脚本会结束旧的再启动（`-Force` 可免询问）
- 是**别的程序** → 脚本不会动它，换端口：

```bat
start-breeze.cmd -Root "D:\Breeze-TTS-2.cpp" -Port 9001
```

> ⚠️ 换端口后记得同步改插件配置的「本地服务端口」和「Breeze 服务地址」
> （例如 `http://127.0.0.1:9001`）。

### 想换量化版本（显存不够时）

把新的 `.gguf`（如 `q4_k_m`）也放进同一目录，再跑一次会列出所有模型让你选，
并记住上次的选择。

---

## 🔧 高级用法

```powershell
# 指定模型（只写文件名即可）
.\start-breeze.ps1 "D:\Breeze-TTS-2.cpp" -Model breeze-tts-2-q4_k_m.gguf

# 指定服务程序（有多个版本目录时）
.\start-breeze.ps1 "D:\Breeze-TTS-2.cpp" -Srv .\breeze-tts-2-v0.1.7-windows-x64-vulkan\breeze-server.exe

# 提高并发（默认 4；带语气指令的请求会占两个槽）
.\start-breeze.ps1 "D:\Breeze-TTS-2.cpp" -Parallel 6

# 不启网页界面
.\start-breeze.ps1 "D:\Breeze-TTS-2.cpp" -NoWebui

# 结尾不按回车（给别的程序/计划任务调用）
.\start-breeze.ps1 "D:\Breeze-TTS-2.cpp" -NoPause -Force

# 只看会执行什么命令，不真的启动
.\start-breeze.ps1 "D:\Breeze-TTS-2.cpp" -DryRun
```

---

## 📋 环境要求

- Windows 10 / 11
- PowerShell 5.1（系统自带）或更高
- **不需要管理员权限**
- 模型文件 `.gguf` 与服务程序 `breeze-server.exe` 已就位
