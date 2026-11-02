# Breeze TTS 2 启动器（通用版：自动识别模型与服务程序）
#
# 支持任意量化版本（q8_0 / q6_k / q4_k_m / f16 ...）与任意服务版本目录。
#
# 特性：
#   1) Job Object（KILL_ON_JOB_CLOSE）—— 窗口一关，breeze-server 必被系统回收
#   2) 启动时自动清理残留 —— 端口被上一次的 breeze-server 占着时，询问后可结束
#   3) 自动发现 *.gguf 与 breeze-server.exe（目录里有多个模型时让你选，并记住上次选择）
#   4) finally 兜底 —— 正常退出 / Ctrl+C 也会杀掉子进程
#
# 用法：
#   start-breeze.ps1                          # 自动识别
#   start-breeze.ps1 -Model q4_k_m.gguf       # 指定模型（文件名或完整路径）
#   start-breeze.ps1 -Parallel 2              # 指定并发槽
#   start-breeze.ps1 -Port 9001               # 换端口
#   start-breeze.ps1 -NoWebui                 # 不启 WebUI
#   start-breeze.ps1 -DryRun                  # 只打印将要执行的命令，不启动

param(
    [switch]$Force,          # 端口被自家进程占用时不再询问，直接结束
    [int]$Port = 9000,
    [string]$Model = "",     # gguf 文件（可只写文件名）
    [string]$Srv = "",       # breeze-server.exe 路径
    [int]$Parallel = 4,
    [switch]$NoWebui,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$Root      = $PSScriptRoot
$VoicesDir = Join-Path $Root 'voice'
$Parallel_ = $Parallel
$LogDir    = Join-Path $Root 'logs'
$LogOut    = Join-Path $LogDir 'server.out.log'
$LogErr    = Join-Path $LogDir 'server.err.log'
$LastModel = Join-Path $Root 'last-model.txt'

$jobHandle = [IntPtr]::Zero
$proc      = $null

# ============================================================ 工具函数

function Test-Service([int]$P) {
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$P/health" -TimeoutSec 2 -ErrorAction Stop
        return ($r.status -eq 'ok')
    } catch { return $false }
}

# 不用 Select-Object -First 1 提前中断管道（$ErrorActionPreference='Stop' 下会抛异常被吞）
function Get-PortOwner([int]$P) {
    $conns = @()
    try { $conns = @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue) } catch { }
    if ($conns.Count -gt 0) {
        $procId = [int]$conns[0].OwningProcess
        $pr = Get-Process -Id $procId -ErrorAction SilentlyContinue
        return [pscustomobject]@{
            PID = $procId
            Name = $(if ($pr) { $pr.ProcessName } else { '?' })
            Path = $(if ($pr) { $pr.Path } else { '?' })
        }
    }
    try {
        $lines = @(netstat -ano 2>$null | Select-String ":$P\s+.*LISTENING")
        if ($lines.Count -gt 0) {
            $fields = @(($lines[0].Line -split '\s+') | Where-Object { $_ -ne '' })
            $procId = [int]$fields[-1]
            $pr = Get-Process -Id $procId -ErrorAction SilentlyContinue
            return [pscustomobject]@{
                PID = $procId
                Name = $(if ($pr) { $pr.ProcessName } else { '?' })
                Path = $(if ($pr) { $pr.Path } else { '?' })
            }
        }
    } catch { }
    return $null
}

function Stop-Breeze {
    $targets = @(Get-Process -Name 'breeze-server' -ErrorAction SilentlyContinue)
    if ($targets.Count -eq 0) { return $true }
    foreach ($t in $targets) {
        Write-Host "        结束进程 PID $($t.Id)" -ForegroundColor DarkGray
        try { $t.Kill() } catch { }
    }
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Milliseconds 300
        if (-not (Get-PortOwner $Port)) { return $true }
    }
    return $false
}

# 估算显存占用（按量化档位粗估，仅供参考）
function Get-VramHint([string]$Name, [long]$Bytes) {
    $gb = $Bytes / 1GB
    $factor = 1.9
    if ($Name -match '(?i)f16|fp16')      { $factor = 1.25 }
    elseif ($Name -match '(?i)q8')        { $factor = 2.0 }
    elseif ($Name -match '(?i)q6')        { $factor = 1.9 }
    elseif ($Name -match '(?i)q5')        { $factor = 1.8 }
    elseif ($Name -match '(?i)q4')        { $factor = 1.6 }
    elseif ($Name -match '(?i)q3')        { $factor = 1.5 }
    return [math]::Round($gb * $factor, 1)
}

# ---- 自动发现服务程序 ----
function Resolve-ServerExe([string]$Given) {
    if ($Given) {
        $p = if (Test-Path $Given) { (Resolve-Path $Given).Path } else { Join-Path $Root $Given }
        if (Test-Path $p) { return $p }
        return $null
    }
    $hits = @(Get-ChildItem $Root -Recurse -Filter 'breeze-server.exe' -File -ErrorAction SilentlyContinue)
    if ($hits.Count -eq 0) { return $null }
    return ($hits | Sort-Object LastWriteTime -Descending)[0].FullName
}

# ---- 自动发现模型文件 ----
function Resolve-ModelFile([string]$Given) {
    if ($Given) {
        $p = if (Test-Path $Given) { (Resolve-Path $Given).Path } else { Join-Path $Root $Given }
        if (Test-Path $p) { return $p }
        return $null
    }

    $ggufs = @(Get-ChildItem $Root -Filter '*.gguf' -File -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($ggufs.Count -eq 0) { return $null }
    if ($ggufs.Count -eq 1) { return $ggufs[0].FullName }

    # 多个模型：菜单 + 记住上次
    $last = ''
    if (Test-Path $LastModel) { $last = (Get-Content $LastModel -Raw -ErrorAction SilentlyContinue).Trim() }

    Write-Host '        检测到多个模型：' -ForegroundColor Cyan
    for ($i = 0; $i -lt $ggufs.Count; $i++) {
        $mark = if ($ggufs[$i].Name -eq $last) { '   <- 上次使用' } else { '' }
        Write-Host ("          [{0}] {1,-34} {2,7:N2} GB  ≈{3} GB 显存{4}" -f `
            ($i + 1), $ggufs[$i].Name, ($ggufs[$i].Length / 1GB), (Get-VramHint $ggufs[$i].Name $ggufs[$i].Length), $mark)
    }

    $defaultIdx = 1
    for ($i = 0; $i -lt $ggufs.Count; $i++) { if ($ggufs[$i].Name -eq $last) { $defaultIdx = $i + 1 } }

    $ans = Read-Host "        请选择 [1-$($ggufs.Count)]，回车用默认 $defaultIdx"
    $idx = $defaultIdx
    if (-not [string]::IsNullOrWhiteSpace($ans)) {
        $parsed = 0
        if ([int]::TryParse($ans, [ref]$parsed) -and $parsed -ge 1 -and $parsed -le $ggufs.Count) { $idx = $parsed }
    }
    try { Set-Content -Path $LastModel -Value $ggufs[$idx - 1].Name -Encoding UTF8 } catch { }
    return $ggufs[$idx - 1].FullName
}

# ============================================================ Job Object

$JobSource = @'
using System;
using System.Runtime.InteropServices;

public static class BreezeJob
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr hJob, int infoClass, IntPtr lpInfo, uint cbInfo);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public Int64 PerProcessUserTimeLimit;
        public Int64 PerJobUserTimeLimit;
        public UInt32 LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public UInt32 ActiveProcessLimit;
        public IntPtr Affinity;
        public UInt32 PriorityClass;
        public UInt32 SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public UInt64 ReadOperationCount;
        public UInt64 WriteOperationCount;
        public UInt64 OtherOperationCount;
        public UInt64 ReadTransferCount;
        public UInt64 WriteTransferCount;
        public UInt64 OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    public static IntPtr CreateKillOnCloseJob()
    {
        IntPtr hJob = CreateJobObject(IntPtr.Zero, null);
        if (hJob == IntPtr.Zero) { return IntPtr.Zero; }

        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        int len = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr ptr = Marshal.AllocHGlobal(len);
        try
        {
            Marshal.StructureToPtr(info, ptr, false);
            if (!SetInformationJobObject(hJob, JobObjectExtendedLimitInformation, ptr, (uint)len))
            {
                return IntPtr.Zero;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(ptr);
        }
        return hJob;
    }

    public static bool Assign(IntPtr hJob, IntPtr hProcess)
    {
        return AssignProcessToJobObject(hJob, hProcess);
    }
}
'@

# ============================================================ 主流程

try {
    Write-Host ''
    Write-Host '  ============================================' -ForegroundColor Cyan
    Write-Host '         Breeze TTS 2  local server' -ForegroundColor Cyan
    Write-Host "         port $Port  |  parallel $Parallel_" -ForegroundColor DarkCyan
    Write-Host '  ============================================' -ForegroundColor Cyan
    Write-Host ''

    # --- 1/5 自动识别文件 ---
    Write-Host '  [1/5] 识别模型与服务程序' -ForegroundColor Yellow

    $SrvPath = Resolve-ServerExe $Srv
    if (-not $SrvPath) {
        Write-Host '        x  找不到 breeze-server.exe' -ForegroundColor Red
        Write-Host "           已搜索: $Root" -ForegroundColor DarkGray
        if (-not $DryRun) { $null = Read-Host '  按回车键退出...' }
        exit 1
    }
    Write-Host ("        ok  服务: {0}" -f $SrvPath.Replace($Root, '.')) -ForegroundColor Green

    $ModelPath = Resolve-ModelFile $Model
    if (-not $ModelPath) {
        Write-Host '        x  找不到 .gguf 模型文件' -ForegroundColor Red
        Write-Host "           已搜索: $Root" -ForegroundColor DarkGray
        if (-not $DryRun) { $null = Read-Host '  按回车键退出...' }
        exit 1
    }
    $mi = Get-Item $ModelPath
    Write-Host ("        ok  模型: {0}" -f $mi.Name) -ForegroundColor Green
    Write-Host ("            {0:N2} GB  ≈{1} GB 显存（估）" -f ($mi.Length / 1GB), (Get-VramHint $mi.Name $mi.Length)) -ForegroundColor DarkGray

    if (-not (Test-Path $VoicesDir)) {
        if (-not $DryRun) { New-Item -ItemType Directory -Path $VoicesDir -Force | Out-Null }
        Write-Host '        +  音色目录不存在，已创建' -ForegroundColor DarkYellow
    }
    $voices = @(Get-ChildItem $VoicesDir -Filter '*.breeze' -ErrorAction SilentlyContinue)
    Write-Host ("        ok  音色 {0} 个: {1}" -f $voices.Count, (($voices | ForEach-Object { $_.BaseName }) -join ', ')) -ForegroundColor Green

    $webuiArgs = if ($NoWebui) { @() } else { @('--webui') }
    $argList = @($ModelPath, '--port', $Port, '--parallel', $Parallel_, '--voices-dir', $VoicesDir) + $webuiArgs

    if ($DryRun) {
        Write-Host ''
        Write-Host '  [DryRun] 将要执行：' -ForegroundColor Cyan
        Write-Host ('        "{0}" {1}' -f $SrvPath, ($argList -join ' ')) -ForegroundColor White
        Write-Host ''
        exit 0
    }

    # --- 2/5 端口检查 ---
    Write-Host ''
    Write-Host "  [2/5] 检查端口 $Port" -ForegroundColor Yellow
    $owner = Get-PortOwner $Port
    if ($owner) {
        Write-Host "        端口被占用: PID $($owner.PID)  $($owner.Name)" -ForegroundColor DarkYellow
        Write-Host "        路径: $($owner.Path)" -ForegroundColor DarkGray
        if ($owner.Name -eq 'breeze-server') {
            Write-Host '        这是上一次留下的 breeze-server 进程。' -ForegroundColor DarkYellow
            $go = [bool]$Force
            if (-not $Force) {
                $ans = Read-Host '        是否结束它并重新启动？[Y/n]'
                if ($ans -eq '' -or $ans -match '^(y|Y|yes|是)') { $go = $true }
            }
            if ($go) {
                if (Stop-Breeze) { Write-Host '        ok  已结束，端口已释放' -ForegroundColor Green }
                else {
                    Write-Host '        x  无法结束，请手动处理' -ForegroundColor Red
                    $null = Read-Host '  按回车键退出...'
                    exit 1
                }
            } else {
                Write-Host '  已取消。' -ForegroundColor DarkGray
                $null = Read-Host '  按回车键退出...'
                exit 0
            }
        } else {
            Write-Host '        x  占用者是别的程序，本脚本不会动它' -ForegroundColor Red
            $null = Read-Host '  按回车键退出...'
            exit 1
        }
    } else {
        Write-Host '        ok  端口空闲' -ForegroundColor Green
    }

    # --- 3/5 Job Object ---
    Write-Host ''
    Write-Host '  [3/5] 准备进程守护' -ForegroundColor Yellow
    try {
        Add-Type -TypeDefinition $JobSource -Language CSharp -ErrorAction Stop
        $jobHandle = [BreezeJob]::CreateKillOnCloseJob()
        if ($jobHandle -ne [IntPtr]::Zero) {
            Write-Host '        ok  Job Object 已建立：关窗口时服务会被系统自动回收' -ForegroundColor Green
        } else {
            Write-Host '        !  创建失败，改用退出时兜底清理' -ForegroundColor DarkYellow
        }
    } catch {
        Write-Host "        !  不可用（$($_.Exception.Message)），改用兜底清理" -ForegroundColor DarkYellow
    }

    # --- 4/5 启动 ---
    Write-Host ''
    Write-Host '  [4/5] 启动服务（首次加载模型约 5~10 秒）' -ForegroundColor Yellow
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

    $proc = Start-Process -FilePath $SrvPath -ArgumentList $argList `
        -WorkingDirectory (Split-Path $SrvPath -Parent) -NoNewWindow -PassThru `
        -RedirectStandardOutput $LogOut -RedirectStandardError $LogErr

    Write-Host "        PID $($proc.Id)" -ForegroundColor DarkGray

    if ($jobHandle -ne [IntPtr]::Zero) {
        try {
            if ([BreezeJob]::Assign($jobHandle, $proc.Handle)) {
                Write-Host '        ok  已纳入守护，关窗口即释放显存' -ForegroundColor Green
            } else {
                Write-Host '        !  纳入守护失败，改用兜底清理' -ForegroundColor DarkYellow
            }
        } catch {
            Write-Host "        !  纳入守护异常: $($_.Exception.Message)" -ForegroundColor DarkYellow
        }
    }

    # --- 5/5 等待就绪 ---
    Write-Host ''
    Write-Host '  [5/5] 等待服务就绪' -ForegroundColor Yellow
    $ready = $false
    for ($i = 1; $i -le 90; $i++) {
        Start-Sleep -Seconds 1
        if ($proc.HasExited) {
            Write-Host "        x  进程提前退出，退出码 $($proc.ExitCode)" -ForegroundColor Red
            Write-Host "        日志: $LogErr" -ForegroundColor DarkGray
            Get-Content $LogErr -Tail 10 -ErrorAction SilentlyContinue | ForEach-Object { "        $_" }
            break
        }
        if (Test-Service $Port) { $ready = $true; break }
        if ($i % 5 -eq 0) { Write-Host "        ... 已等待 $i 秒" -ForegroundColor DarkGray }
    }

    if ($ready) {
        # 读一下实际占用显存
        Write-Host ''
        Write-Host '  ============================================' -ForegroundColor Green
        Write-Host '         服务已就绪' -ForegroundColor Green
        Write-Host "         http://127.0.0.1:$Port/" -ForegroundColor Green
        Write-Host ("         模型: {0}" -f $mi.Name) -ForegroundColor Green
        Write-Host '  ============================================' -ForegroundColor Green
        Write-Host ''
        Write-Host '  关闭本窗口 = 服务停止并释放显存' -ForegroundColor Cyan
        Write-Host ''
        try { Start-Process "http://127.0.0.1:$Port/" } catch { }
    } else {
        Write-Host ''
        Write-Host '  服务未能在限时内就绪，请查看 logs\ 下的日志。' -ForegroundColor Red
    }

    Write-Host '  ---- 服务运行中（日志写入 logs\）----' -ForegroundColor DarkGray
    try {
        $proc.WaitForExit()
        Write-Host "  服务已退出，退出码 $($proc.ExitCode)" -ForegroundColor DarkYellow
    } catch {
        Write-Host '  等待被中断' -ForegroundColor DarkYellow
    }
}
finally {
    if ($proc -and -not $proc.HasExited) {
        Write-Host ''
        Write-Host '  正在停止服务...' -ForegroundColor Yellow
        try { $proc.Kill() } catch { }
        try { $null = $proc.WaitForExit(5000) } catch { }
        Write-Host '  服务已停止' -ForegroundColor Green
    }
    if ($Host.Name -eq 'ConsoleHost' -and -not $DryRun) {
        Write-Host ''
        Write-Host '  按回车键关闭窗口...' -ForegroundColor DarkGray
        $null = Read-Host
    }
}
