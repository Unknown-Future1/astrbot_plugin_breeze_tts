# ⚠️ 先读我 —— 这 3 个脚本必须移动后才能用

本目录里的 3 个脚本是用来**一键启动/停止本地 Breeze TTS 服务**的，
**不能放在插件目录里直接运行**，必须复制到**模型所在的文件夹**。

---

## 📦 这 3 个文件是什么

| 文件 | 用途 |
| --- | --- |
| `start-breeze.cmd` | **双击这个**启动服务 |
| `start-breeze.ps1` | 实际逻辑（被上面那个调用，不需要单独双击） |
| `stop-breeze.cmd` | **双击这个**停止服务 |

---

## 🚚 移动到哪里

**移到和 `*.gguf` 模型文件同一个文件夹。**

以本插件作者的目录为例：

```
<你的模型目录>\
├── breeze-tts-2-q8_0.gguf                          ← 模型（已经在）
├── breeze-tts-2-v0.1.6-windows-x64-vulkan\          ← 服务程序（已经在）
│   └── breeze-server.exe
├── voice\                                           ← 音色目录（已经在）
│   ├── ds-voice.breeze
│   └── xiaojing-voice.breeze
│
├── start-breeze.cmd      ← ★ 把这 3 个放这里
├── start-breeze.ps1      ← ★
└── stop-breeze.cmd       ← ★
```

> 也就是说：**这 3 个脚本要和 `.gguf` 文件平级**。

---

## 🧭 为什么要移动（不是作者偷懒）

脚本靠"**自己所在的那个文件夹**"来寻找模型和服务程序：

```powershell
$Root = $PSScriptRoot                     # = 脚本自己所在的目录
$Model = 在 $Root 里找 *.gguf
$Srv   = 在 $Root 里找 breeze-server.exe
```

所以：

- 放在 `<你的模型目录>\` → 能找到模型 ✅
- 放在插件目录 → 那个目录里没有 `.gguf` → 报错退出 ❌

**移动脚本 = 告诉它"要去哪里找模型"，比在脚本里写死路径更通用**
（换量化版本、换服务版本都不用改脚本）。

---

## ✅ 操作步骤

1. 找到你的模型目录（`.gguf` 文件所在的那个文件夹）
2. 把本目录里的 **3 个文件**一起复制过去
   - 注意：`start-breeze.ps1` **也要复制**，`start-breeze.cmd` 需要它
3. 双击 `start-breeze.cmd`

启动成功后会自动打开网页 <http://127.0.0.1:9000/>，控制台窗口**必须保持打开**。

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

两种都行：

1. **关掉那个黑窗口** —— 服务会被系统自动回收，显存立刻释放
2. **双击 `stop-breeze.cmd`** —— 强制结束 `breeze-server.exe`

---

## ❓ 常见问题

### 双击 `start-breeze.cmd` 一闪而过

多半是脚本找不到模型或服务程序。请在**命令行窗口**里运行，看清楚哪一步报错：

```powershell
cd /d 你的模型目录
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1
```

### 提示「找不到 .gguf 模型文件」

- 确认脚本和 `.gguf` 在**同一层**
- 确认模型文件名以 `.gguf` 结尾

### 提示「端口 9000 被占用」

- 如果是 `breeze-server` → 按提示输入 `Y`，脚本会结束旧的再启动
- 如果是**别的程序** → 脚本不会动它，请自行换端口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 -Port 9001
```

> ⚠️ 换端口后记得同步修改 AstrBot 里插件配置的 `breeze_api`
> （例如 `http://127.0.0.1:9001`）。

### 页面打不开

确认启动窗口里出现了「服务已就绪」。如果卡在 `[5/5] 等待服务就绪`，
看 `logs\server.err.log` 里的错误。

### 想换量化版本（显存不够时）

把新的 `.gguf`（如 `q4_k_m`）也放进这个目录，再次运行时脚本会**列出所有模型让你选**，
并记住上次的选择。

---

## 🔧 高级用法

```powershell
# 指定模型（只写文件名即可）
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 -Model breeze-tts-2-q4_k_m.gguf

# 指定服务程序（有多个版本目录时）
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 -Srv .\breeze-tts-2-v0.1.7-windows-x64-vulkan\breeze-server.exe

# 提高并发（默认 4；带语气指令的请求会占两个槽）
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 -Parallel 6

# 不启网页界面
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 -NoWebui

# 只看会执行什么命令，不真的启动
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-breeze.ps1 -DryRun
```

---

## 📋 环境要求

- Windows 10 / 11
- PowerShell 5.1（系统自带）或更高
- **不需要管理员权限**
- 模型文件 `.gguf` 与服务程序 `breeze-server.exe` 已就位
