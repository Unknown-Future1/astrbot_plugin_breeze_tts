"""本地 Breeze TTS 2 服务进程管理。

纯标准库实现，不 import astrbot，方便单独跑脚本测试。

职责：
  · 由「模型目录」推断 breeze-server.exe / *.gguf / voice 目录
  · 拉起、停止、重启本地服务，做健康检查
  · 自动探测可能的模型目录（给设置页的「自动检测」用）
  · 收集服务日志尾巴
  · Windows 原生目录选择框（tkinter，可选）

服务由插件以子进程方式启动，不带控制台窗口；其输出写进
<插件目录>/runtime/logs/server.log，停止时按 PID 精准结束。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# 子进程标志：不弹控制台窗口；独立进程组，Ctrl+C 不会连带杀掉
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

EXE_NAME = "breeze-server.exe"
GGUF_SUFFIX = ".gguf"
VOICES_DIR_NAME = "voice"

DEFAULT_PORT = 9000
DEFAULT_PARALLEL = 4

# 找 exe / 模型时最多往下钻几层（够覆盖 "版本子目录" 这种布局）
_SEARCH_DEPTH = 2
# 自动探测时不该扫的目录
_SKIP_NAMES = {
    "$recycle.bin",
    "system volume information",
    "wpsystem",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    "node_modules",
    ".git",
    "__pycache__",
}

_EXE_HINTS = re.compile(r"breeze", re.I)

_pick_lock = threading.Lock()


# --------------------------------------------------------------------- 小工具


def _clean_path(raw) -> str:
    """去掉用户常带进来的引号和首尾空白。"""
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.strip()


def _norm(raw) -> Path | None:
    text = _clean_path(raw)
    if not text:
        return None
    try:
        return Path(os.path.expandvars(os.path.expanduser(text))).resolve(strict=False)
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（Windows 用 OpenProcess 查，避免 os.kill 误杀）。"""
    if not pid or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _pid_image(pid: int) -> str:
    """进程映像完整路径；拿不到返回空串（多为权限不足）。"""
    if not IS_WINDOWS or not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(handle)


def _looks_like_breeze(pid: int) -> bool:
    """这个 PID 是不是 breeze-server（拿不到映像名时按可信处理）。"""
    if not pid:
        return False
    image = _pid_image(pid)
    if not image:
        return True
    return bool(_EXE_HINTS.search(Path(image).name))


def _taskkill(pid: int) -> bool:
    if not pid:
        return False
    try:
        done = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _terminate_pid(pid: int) -> bool:
    """直接用 Win32 API 结束进程（taskkill 被拒/不存在时的备选方案）。"""
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(int(pid), 15)
            return True
        except OSError:
            return False
    PROCESS_TERMINATE = 0x0001
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, 1))
    except OSError:
        return False
    finally:
        k32.CloseHandle(handle)


def _kill_pid(pid: int) -> bool:
    """先 taskkill，失败再走 TerminateProcess。"""
    if _taskkill(pid):
        return True
    return _terminate_pid(pid)


def _taskkill_name(name: str) -> bool:
    try:
        done = subprocess.run(
            ["taskkill", "/IM", name, "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _iter_subdirs(root: Path, max_depth: int):
    """广度优先列出 root 下（含自身）第 1..max_depth 层的目录。"""
    yield root
    level = [(root, 0)]
    while level:
        nxt = []
        for current, depth in level:
            if depth >= max_depth:
                continue
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if entry.name.lower() in _SKIP_NAMES:
                    continue
                child = Path(entry.path)
                yield child
                nxt.append((child, depth + 1))
        level = nxt


def _isdir(path) -> bool:
    """is_dir 的安全版：遇到权限/损坏的 reparse point 一律当不存在。"""
    try:
        return bool(path) and Path(path).is_dir()
    except (OSError, ValueError):
        return False


def _isfile(path) -> bool:
    try:
        return bool(path) and Path(path).is_file()
    except (OSError, ValueError):
        return False


def find_exe(root: Path | None, max_depth: int = _SEARCH_DEPTH) -> Path | None:
    """在 root 里找 breeze-server.exe（越浅越优先）。"""
    if not _isdir(root):
        return None
    best: tuple[int, Path] | None = None
    for folder in _iter_subdirs(root, max_depth):
        candidate = folder / EXE_NAME
        if not _isfile(candidate):
            continue
        depth = len(candidate.relative_to(root).parts) - 1
        if best is None or depth < best[0]:
            best = (depth, candidate)
    return best[1] if best else None


def find_models(root: Path | None, max_depth: int = _SEARCH_DEPTH) -> list[Path]:
    """在 root 里找 *.gguf，按层数、文件名排序。"""
    if not _isdir(root):
        return []
    found: list[tuple[int, str, Path]] = []
    for folder in _iter_subdirs(root, max_depth):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file() or not entry.name.lower().endswith(GGUF_SUFFIX):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            depth = len(path.relative_to(root).parts) - 1
            found.append((depth, path.name.lower(), path))
    found.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in found]


def _has_breeze(folder: Path) -> bool:
    """目录里有没有 *.breeze 音色文件。"""
    if not _isdir(folder):
        return False
    try:
        return any(folder.glob("*.breeze"))
    except OSError:
        return False


def pick_directory(initial: str = "") -> tuple[str, str]:
    """本机弹出原生目录选择框，返回 (路径, 错误)。必须在线程里调用。"""
    if not _pick_lock.acquire(blocking=False):
        return "", "已经有一个选择框在等待操作了，请先处理它"
    try:
        import tkinter  # noqa: PLC0415  延迟导入：无图形环境时更友好
        from tkinter import filedialog  # noqa: PLC0415

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tkinter.TclError:
            pass
        kwargs = {"title": "选择 Breeze TTS 2 所在目录（含 breeze-server.exe）"}
        start = _clean_path(initial)
        if start and _isdir(start):
            kwargs["initialdir"] = start
        chosen = filedialog.askdirectory(**kwargs)
        root.destroy()
        return (str(chosen or ""), "")
    except Exception as exc:  # noqa: BLE001  无 tkinter / 无桌面
        return "", f"打不开选择框（{type(exc).__name__}: {exc}），请直接把路径粘进输入框"
    finally:
        _pick_lock.release()


def _human_size(num: int) -> str:
    gb = num / (1024**3)
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{num / (1024**2):.0f} MB"


def _file_size(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return _human_size(path.stat().st_size)
    except OSError:
        return ""


# ----------------------------------------------------------------- 服务管理器


class ServiceManager:
    """本地 Breeze 服务的生命周期管理。"""

    def __init__(
        self,
        plugin_dir,
        model_dir: str = "",
        model_file: str = "",
        port: int = DEFAULT_PORT,
        parallel: int = DEFAULT_PARALLEL,
        webui: bool = True,
    ):
        self.plugin_dir = Path(plugin_dir).resolve(strict=False)
        self.runtime_dir = self.plugin_dir / "runtime"
        self.log_dir = self.runtime_dir / "logs"
        self.log_file = self.log_dir / "server.log"
        self.state_file = self.runtime_dir / "service.json"

        self.model_dir = _clean_path(model_dir)
        self.model_file = _clean_path(model_file)
        self.port = int(port or DEFAULT_PORT)
        self.parallel = max(1, int(parallel or DEFAULT_PARALLEL))
        self.webui = bool(webui)

        self._proc = None
        self._log_fh = None

    # ------------------------------------------------------------ 路径推断
    def clone(self, **overrides) -> "ServiceManager":
        """复制一个管理器（设置页拿未保存的目录做「检查」时用）。"""
        data = {
            "plugin_dir": self.plugin_dir,
            "model_dir": self.model_dir,
            "model_file": self.model_file,
            "port": self.port,
            "parallel": self.parallel,
            "webui": self.webui,
        }
        data.update(overrides)
        return ServiceManager(**data)

    @property
    def root(self) -> Path | None:
        return _norm(self.model_dir)

    def exe(self) -> Path | None:
        return find_exe(self.root)

    def models(self) -> list[Path]:
        return find_models(self.root)

    def model(self, hint: str = "") -> Path | None:
        """挑模型：显式 hint > 配置 model_file > last-model.txt > 唯一/第一个。"""
        hint = _clean_path(hint) or self.model_file
        root = self.root
        if hint:
            candidate = _norm(hint)
            if candidate and candidate.is_file():
                return candidate
            if root:
                for path in self.models():
                    if path.name.lower() == hint.lower() or str(path).lower() == hint.lower():
                        return path
        found = self.models()
        if not found:
            return None
        if len(found) == 1:
            return found[0]
        if root:
            remembered = root / "last-model.txt"
            try:
                last = remembered.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                last = ""
            for path in found:
                if path.name.lower() == last.lower():
                    return path
        return found[0]

    def voices_dir(self) -> Path | None:
        """音色目录：<root>/voice 优先；没有就退到 exe 同目录（服务默认位置）。"""
        root = self.root
        if not root:
            return None
        preferred = root / VOICES_DIR_NAME
        if _has_breeze(preferred):
            return preferred
        exe = self.exe()
        if exe and _has_breeze(exe.parent):
            return exe.parent
        return preferred

    def voices(self) -> list[dict]:
        ok, data = self._get_json("/v1/voices")
        if not ok or not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                result.append({"id": item["id"], "saved": bool(item.get("saved"))})
            elif isinstance(item, str):
                result.append({"id": item, "saved": False})
        return result

    # ------------------------------------------------------------ 健康检查
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # 健康检查必须直连本机：Windows 上 urllib 会自动读系统代理（注册表），
    # 用户开着 Clash 等代理时，探测 127.0.0.1 会被代理劫持导致全部超时
    _DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _get_json(self, path: str, timeout: float = 2.0):
        url = f"{self.base_url}{path}"
        try:
            with self._DIRECT_OPENER.open(url, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return True, json.loads(raw or "{}")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, None

    def health(self, timeout: float = 2.0) -> tuple[bool, dict]:
        ok, data = self._get_json("/health", timeout=timeout)
        if ok and isinstance(data, dict) and data.get("status") == "ok":
            return True, data
        return False, {}

    def port_owner(self) -> int | None:
        """谁在监听这个端口（用 netstat 解析，纯标准库）。"""
        if not IS_WINDOWS:
            return None
        try:
            done = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        pattern = re.compile(rf":{self.port}\s+\S+\s+.*?LISTENING\s+(\d+)", re.I)
        for line in (done.stdout or "").splitlines():
            match = pattern.search(line)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue
        return None

    # --------------------------------------------------------------- 状态
    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, payload: dict) -> None:
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _clear_state(self) -> None:
        try:
            self.state_file.unlink()
        except OSError:
            pass

    def tracked_pid(self) -> int:
        """当前在跑的 breeze-server 的 PID（0 = 没在跑）。"""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc.pid
        pid = int(self._read_state().get("pid") or 0)
        if pid and _pid_alive(pid) and _looks_like_breeze(pid):
            return pid
        owner = self.port_owner() or 0
        if owner and _pid_alive(owner) and _looks_like_breeze(owner):
            return owner
        return 0

    def status(self) -> dict:
        ok, health = self.health()
        pid = self.tracked_pid()
        exe = self.exe()
        model = self.model()
        models = self.models()
        voices_dir = self.voices_dir()
        voice_files = []
        if voices_dir and _isdir(voices_dir):
            try:
                voice_files = sorted(p.stem for p in voices_dir.glob("*.breeze"))
            except OSError:
                voice_files = []
        log_lines = self.log_tail(40)
        root = self.root
        return {
            "platform_ok": IS_WINDOWS,
            "running": bool(ok or pid),
            "healthy": bool(ok),
            "pid": pid,
            "port": self.port,
            "parallel": self.parallel,
            "model_dir": str(root) if root else "",
            "model_dir_exists": _isdir(root),
            "exe": str(exe) if exe else "",
            "exe_name": exe.name if exe else "",
            "model": str(model) if model else "",
            "model_name": model.name if model else "",
            "model_size": _file_size(model),
            "models": [
                {"name": p.name, "path": str(p), "size": _file_size(p)} for p in models
            ],
            "voices_dir": str(voices_dir) if voices_dir else "",
            "voice_files": voice_files,
            "voices": [v["id"] for v in self.voices()] if ok else [],
            "api": self.base_url,
            "webui": f"http://127.0.0.1:{self.port}/" if self.webui else "",
            "sample_rate": (health or {}).get("sample_rate", 0),
            "log_tail": log_lines,
            "log_file": str(self.log_file),
        }

    def validate(self) -> dict:
        """设置页「检查」按钮：逐条报告目录里能不能用。"""
        root = self.root
        exe = self.exe()
        models = self.models()
        voices_dir = self.voices_dir()
        checks = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add("目录存在", _isdir(root), str(root) if root else "还没填目录")
        add(
            "服务程序",
            bool(exe),
            str(exe) if exe else f"没找到 {EXE_NAME}（会在目录下两层内搜索）",
        )
        add(
            "模型文件",
            bool(models),
            models[0].name + (f" 等 {len(models)} 个" if len(models) > 1 else "")
            if models
            else "没找到 *.gguf",
        )
        if voices_dir:
            count = 0
            if _isdir(voices_dir):
                try:
                    count = len(list(voices_dir.glob("*.breeze")))
                except OSError:
                    count = 0
            add("音色目录", True, f"{voices_dir}（{count} 个音色）")
        else:
            add("音色目录", False, "目录没填，无法推断")

        owner = self.port_owner()
        if owner:
            image = _pid_image(owner)
            mine = _EXE_HINTS.search(Path(image).name or "")
            add(
                f"端口 {self.port}",
                True,
                f"被 PID {owner} 占用（{Path(image).name or '未知进程'}）"
                + ("" if mine else "，不是 breeze-server"),
            )
        else:
            add(f"端口 {self.port}", True, "空闲")
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # ------------------------------------------------------------ 启动/停止
    def _open_log(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_file.exists() and self.log_file.stat().st_size > 4 * 1024 * 1024:
                self.log_file.replace(self.log_file.with_suffix(".log.1"))
        except OSError:
            pass
        self._log_fh = open(self.log_file, "ab", buffering=0)  # noqa: SIM115
        return self._log_fh

    def start(self, model_hint: str = "", wait: float = 25.0) -> dict:
        if not IS_WINDOWS:
            return {"ok": False, "message": "只有 Windows 能管本地服务"}
        if self.health()[0]:
            return {
                "ok": True,
                "ready": True,
                "already": True,
                "pid": self.tracked_pid(),
                "message": f"服务已经在 {self.port} 端口跑着了",
            }

        exe = self.exe()
        if not exe:
            return {
                "ok": False,
                "message": f"没找到 {EXE_NAME}，先在「模型目录」里选对位置",
            }
        model = self.model(model_hint)
        if not model:
            return {"ok": False, "message": "没找到 .gguf 模型文件"}

        voices_dir = self.voices_dir()
        if voices_dir:
            try:
                voices_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                voices_dir = None

        args = [
            str(exe),
            str(model),
            "--port",
            str(self.port),
            "--parallel",
            str(self.parallel),
        ]
        if voices_dir:
            args += ["--voices-dir", str(voices_dir)]
        if self.webui:
            args.append("--webui")

        stale = self.port_owner()
        if stale:
            image = _pid_image(stale)
            if _EXE_HINTS.search(Path(image).name or ""):
                _taskkill(stale)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and self.port_owner():
                    time.sleep(0.3)
            else:
                return {
                    "ok": False,
                    "message": f"端口 {self.port} 被别的程序占着（PID {stale}），先换端口",
                }

        try:
            log_fh = self._open_log()
            self._proc = subprocess.Popen(
                args,
                cwd=str(exe.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except OSError as exc:
            return {"ok": False, "message": f"启动失败：{exc}"}

        self._write_state(
            {
                "pid": self._proc.pid,
                "port": self.port,
                "model": str(model),
                "exe": str(exe),
                "started_at": time.time(),
            }
        )

        started = time.monotonic()
        ready = False
        while time.monotonic() - started < max(3.0, wait):
            if self._proc.poll() is not None:
                break
            if self.health(timeout=1.0)[0]:
                ready = True
                break
            time.sleep(0.5)

        elapsed = time.monotonic() - started
        if not ready:
            code = self._proc.poll()
            tail = " / ".join(self.log_tail(4))
            if code is not None:
                return {
                    "ok": False,
                    "pid": self._proc.pid,
                    "message": f"进程启动后立刻退出了（退出码 {code}）。日志：{tail or '（空）'}",
                }
            return {
                "ok": True,
                "ready": False,
                "pid": self._proc.pid,
                "message": f"已启动，但 {elapsed:.0f} 秒还没就绪（首次加载模型比较慢，稍等再看状态）",
            }
        return {
            "ok": True,
            "ready": True,
            "pid": self._proc.pid,
            "model": str(model),
            "elapsed": round(elapsed, 1),
            "message": f"服务已就绪（{elapsed:.1f} 秒）",
        }

    def stop(self, wait: float = 12.0) -> dict:
        if not IS_WINDOWS:
            return {"ok": False, "message": "只有 Windows 能管本地服务"}
        pids: list[int] = []

        def add(pid) -> None:
            try:
                value = int(pid or 0)
            except (TypeError, ValueError):
                return
            if value > 0 and value not in pids:
                pids.append(value)

        if self._proc is not None and self._proc.poll() is None:
            add(self._proc.pid)
        add(self._read_state().get("pid"))
        add(self.port_owner())

        killed = []
        for pid in pids:
            if not _pid_alive(pid):
                continue
            if not _looks_like_breeze(pid):
                continue
            if _kill_pid(pid):
                killed.append(pid)

        deadline = time.monotonic() + max(3.0, wait)
        while time.monotonic() < deadline and self.health(timeout=1.0)[0]:
            time.sleep(0.4)

        if self.health(timeout=1.0)[0]:
            # 兜底：进程名全杀（和 stop-breeze.cmd 一个路子）
            if _taskkill_name(EXE_NAME):
                killed.append(-1)
                time.sleep(1.0)

        still = self.health(timeout=1.0)[0]
        self._proc = None
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
        self._clear_state()
        if still:
            return {
                "ok": False,
                "pids": killed,
                "message": (
                    "停止失败：端口还在响应。可能是权限不足（拒绝访问）——"
                    "试试以管理员身份运行 AstrBot，或直接双击脚本里的 stop-breeze.cmd"
                ),
            }
        if not killed:
            return {"ok": True, "pids": [], "message": "服务本来就没在运行"}
        return {"ok": True, "pids": killed, "message": f"已停止（PID {', '.join(map(str, [p for p in killed if p > 0])) or '进程名兜底'}），显存已释放"}

    def restart(self, model_hint: str = "", wait: float = 25.0) -> dict:
        self.stop()
        time.sleep(1.0)
        return self.start(model_hint=model_hint, wait=wait)

    # --------------------------------------------------------------- 日志
    def log_tail(self, lines: int = 40) -> list[str]:
        try:
            if not self.log_file.is_file():
                return []
            size = self.log_file.stat().st_size
            with open(self.log_file, "rb") as fh:
                if size > 16 * 1024:
                    fh.seek(-16 * 1024, os.SEEK_END)
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return []
        rows = [row.rstrip() for row in raw.splitlines() if row.strip()]
        return rows[-max(1, min(500, lines)):]

    # ----------------------------------------------------------- 自动探测
    def detect_roots(self, budget: float = 8.0) -> list[dict]:
        """扫一批常见位置，找含 breeze-server.exe 的目录。"""
        deadline = time.monotonic() + max(1.0, budget)
        seen: set[str] = set()
        hits: list[dict] = []

        def consider(path: Path | None) -> None:
            if path is None or time.monotonic() > deadline:
                return
            try:
                key = str(path).lower()
            except (TypeError, ValueError):
                return
            if key in seen or not _isdir(path):
                return
            seen.add(key)
            found_exe = find_exe(path, max_depth=_SEARCH_DEPTH)
            if not found_exe:
                return
            models = find_models(path, max_depth=_SEARCH_DEPTH)
            hits.append(
                {
                    "path": str(path),
                    "exe": str(found_exe),
                    "models": [p.name for p in models],
                    "score": (2 if models else 1),
                }
            )

        consider(self.root)
        extra = os.environ.get("BREEZE_TTS_HOME") or os.environ.get("BREEZE_HOME")
        if extra:
            consider(_norm(extra))
        home = Path.home()
        for name in ("Desktop", "Downloads", "Documents", "桌面", "下载", "文档"):
            consider(home / name)
        consider(home / "OneDrive" / "Desktop")

        # 各盘根目录的一层子目录（名字带 breeze/tts 的优先）
        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            probe = Path(f"{letter}:\\")
            try:
                if probe.exists():
                    drives.append(probe)
            except OSError:
                continue
        for drive in drives:
            if time.monotonic() > deadline:
                break
            try:
                children = [
                    Path(entry.path)
                    for entry in os.scandir(drive)
                    if entry.is_dir() and entry.name.lower() not in _SKIP_NAMES
                ]
            except OSError:
                continue
            children.sort(key=lambda p: (0 if _EXE_HINTS.search(p.name) else 1, p.name.lower()))
            for child in children:
                if time.monotonic() > deadline:
                    break
                consider(child)

        hits.sort(key=lambda item: (-item["score"], item["path"].lower()))
        for item in hits:
            item.pop("score", None)
        return hits


# --------------------------------------------------------------- 命令行测试


def _main(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Breeze TTS 本地服务管理（测试用）")
    parser.add_argument("action", choices=["status", "start", "stop", "restart", "detect", "validate", "log"])
    parser.add_argument("--root", default="", help="模型目录")
    parser.add_argument("--model", default="", help="指定 gguf 文件名")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    parser.add_argument("--lines", type=int, default=40)
    args = parser.parse_args(argv)

    manager = ServiceManager(
        plugin_dir=Path(__file__).resolve().parent.parent,
        model_dir=args.root,
        model_file=args.model,
        port=args.port,
        parallel=args.parallel,
    )
    if args.action == "status":
        print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
    elif args.action == "validate":
        print(json.dumps(manager.validate(), ensure_ascii=False, indent=2))
    elif args.action == "detect":
        print(json.dumps(manager.detect_roots(), ensure_ascii=False, indent=2))
    elif args.action == "log":
        print("\n".join(manager.log_tail(args.lines)))
    elif args.action == "start":
        print(json.dumps(manager.start(model_hint=args.model), ensure_ascii=False, indent=2))
    elif args.action == "stop":
        print(json.dumps(manager.stop(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(manager.restart(model_hint=args.model), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
