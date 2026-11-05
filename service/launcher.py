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
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# 插件里统一用 AstrBot 的 logger；单独跑脚本（无 astrbot 环境）时才回退到内置 logging
try:
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("astrbot_plugin_breeze_tts.launcher")

# 子进程标志：不弹控制台窗口；独立进程组，Ctrl+C 不会连带杀掉
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

EXE_NAME = "breeze-server.exe"
GGUF_SUFFIX = ".gguf"
VOICES_DIR_NAME = "voices"          # 与 Breeze 自身默认一致（复数）
LEGACY_VOICES_DIR_NAME = "voice"    # 旧版本用的单数目录，启动时自动迁移

DEFAULT_PORT = 9000
DEFAULT_PARALLEL = 4

# 串行化「启动服务」：防止页面双击 / 自动拉起并发时叠出两个进程
_START_LOCK = threading.Lock()

# 找 exe / 模型时最多往下钻几层（够覆盖 "版本子目录" 这种布局）
_SEARCH_DEPTH = 2
# 找 exe/模型时跳过的目录（系统目录、缓存等）
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


def _list_processes(image_name: str) -> list[dict]:
    """列出指定映像名的进程（PID + 完整命令行）。仅 Windows。"""
    if not IS_WINDOWS:
        return []
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$ErrorActionPreference='SilentlyContinue'; "
        f"Get-CimInstance Win32_Process -Filter \"Name='{image_name}'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=25,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = (done.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows: list[dict] = []
    for item in data:
        try:
            pid = int(item.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid:
            rows.append({"pid": pid, "cmd": str(item.get("CommandLine") or "")})
    return rows


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
            encoding="mbcs",
            errors="replace",
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
            encoding="mbcs",
            errors="replace",
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
        data_dir: str = "",
    ):
        self.plugin_dir = Path(plugin_dir).resolve(strict=False)
        # 持久化数据放 data/plugin_data/<插件名>（AstrBot 规范）；没传就退回插件目录，便于单独跑脚本
        base = Path(data_dir).resolve(strict=False) if data_dir else self.plugin_dir
        self.runtime_dir = base / "runtime"
        self.log_dir = self.runtime_dir / "logs"
        self.log_file = self.log_dir / "server.log"
        self.state_file = self.runtime_dir / "service.json"
        # 旧位置的状态文件（插件目录下）在首次启动时迁过来，避免丢掉正在跑的服务 pid
        legacy_state = self.plugin_dir / "runtime" / "service.json"
        try:
            if legacy_state.exists() and not self.state_file.exists():
                self.runtime_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_state, self.state_file)
        except OSError:
            pass

        self.model_dir = _clean_path(model_dir)
        self.model_file = _clean_path(model_file)
        self.port = int(port or DEFAULT_PORT)
        self.parallel = max(1, int(parallel or DEFAULT_PARALLEL))
        self.webui = bool(webui)

        self._proc = None
        self._log_fh = None
        self._args_cache: tuple[float, dict] = (0.0, {})
        self._voices_cache: tuple[float, Path | None] = (0.0, None)

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
        path = _norm(self.model_dir)
        # 用户常把 .gguf 文件路径整条粘进来：自动退回它所在的文件夹
        if path is not None and path.suffix.lower() == GGUF_SUFFIX:
            return path.parent
        return path

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

    @staticmethod
    def _breeze_count(folder: Path) -> int:
        """目录里有多少个 .breeze（数不出来就当 0）。"""
        try:
            return sum(1 for _ in folder.glob("*.breeze"))
        except OSError:
            return 0

    def _detect_voices_dir(self, base: Path) -> Path | None:
        """在 base 及它的一层子目录里，找真正装着 .breeze 的文件夹。

        不猜目录名：谁的目录里有音色文件就认谁；名字恰好是 voices / voice 的优先。
        """
        candidates: list[tuple[Path, int]] = []
        for folder in [base, *[p for p in (base.iterdir() if base.is_dir() else []) if p.is_dir()]]:
            count = self._breeze_count(folder)
            if count:
                candidates.append((folder, count))
        if not candidates:
            return None
        names = {VOICES_DIR_NAME.lower(), LEGACY_VOICES_DIR_NAME.lower()}
        candidates.sort(
            key=lambda item: (
                0 if item[0].name.lower() in names else 1,  # 名字规范优先
                -item[1],                                     # 其次音色多的
                len(str(item[0])),
            )
        )
        return candidates[0][0]

    def voices_dir(self) -> Path | None:
        """音色目录：只在「模型目录」范围内找；服务在跑时听它的 --voices-dir（也必须在模型目录内）。"""
        root = self.root
        if not root:
            return None

        # 服务在跑时以它的 --voices-dir 为准，但必须落在模型目录里，否则忽略
        running = self.running_args().get("--voices-dir")
        if running:
            try:
                candidate = Path(running).resolve(strict=False)
                candidate.relative_to(root.resolve(strict=False))
                return candidate
            except (ValueError, OSError):
                logger.debug(f"[BreezeTTS] 服务的音色目录不在模型目录内，忽略：{running}")

        now = time.monotonic()
        stamp, cached = self._voices_cache
        if cached is not None and now - stamp < 10.0:
            return cached

        detected = self._detect_voices_dir(root)
        result = detected or (root / VOICES_DIR_NAME)
        self._voices_cache = (now, result)
        return result

    def migrate_voices_dir(self) -> str:
        """（可选，不自动调用）把旧版 <root>/voice 合并到 <root>/voices。

        - 新目录没有同名文件 → 直接搬
        - 有同名文件 → 把旧的改名为 <name>.legacy.breeze 再搬，绝不覆盖
        - 旧目录搬空后删掉
        """
        root = self.root
        if not root:
            return ""
        new = root / VOICES_DIR_NAME
        old = root / LEGACY_VOICES_DIR_NAME
        if not old.is_dir():
            return ""
        try:
            new.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        moved = renamed = 0
        for item in list(old.glob("*.breeze")):
            target = new / item.name
            try:
                if target.exists():
                    stem = item.stem
                    target = new / f"{stem}.legacy.breeze"
                    n = 2
                    while target.exists():
                        target = new / f"{stem}.legacy{n}.breeze"
                        n += 1
                    renamed += 1
                shutil.move(str(item), str(target))
                moved += 1
            except OSError:
                continue
        try:
            if not any(old.iterdir()):
                old.rmdir()
        except OSError:
            pass
        if not moved:
            return ""
        extra = f"，其中 {renamed} 个同名改名为 .legacy" if renamed else ""
        return f"已把音色目录合并到 {new}（{moved} 个{extra}）"

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

    def running_args(self, ttl: float = 10.0) -> dict:
        """运行中实例的启动参数（--voices-dir / --port 等），带短缓存。"""
        now = time.monotonic()
        stamp, cached = self._args_cache
        if now - stamp < ttl:
            return dict(cached)
        pid = self.tracked_pid() or self.port_owner()
        found: dict = {}
        if pid:
            for item in _list_processes(EXE_NAME):
                if item["pid"] != pid:
                    continue
                cmd = item["cmd"]
                for flag in ("--voices-dir", "--port", "--parallel"):
                    match = re.search(rf"{re.escape(flag)}\s+(\"[^\"]+\"|\S+)", cmd)
                    if match:
                        found[flag] = match.group(1).strip('"')
                break
        self._args_cache = (now, found)
        return dict(found)
    def voice_details(self) -> list[dict]:
        """音色明细：/v1/voices 全字段 + 文件是否存在（页面音色库用）。"""
        ok, data = self._get_json("/v1/voices")
        if not ok or not isinstance(data, list):
            return []
        folder = self.voices_dir()
        result: list[dict] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            vid = str(item["id"])
            result.append({
                "id": vid,
                "saved": bool(item.get("saved")),
                "seconds": round(float(item.get("seconds") or 0), 2),
                "frames": int(item.get("frames") or 0),
                "ref_text": str(item.get("ref_text") or ""),
                "description": str(item.get("description") or ""),
                "file": str(folder / f"{vid}.breeze") if folder else "",
                "file_exists": bool(folder and (folder / f"{vid}.breeze").exists()),
            })
        return result

    def remove_voice_file(self, voice_id: str) -> tuple[bool, str]:
        """删掉 <voices_dir>/<id>.breeze。服务可能占着文件，失败时返回原因。"""
        safe = str(voice_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", safe):
            return False, "音色名不合法"
        folder = self.voices_dir()
        if not folder:
            return False, "找不到音色目录"
        path = folder / f"{safe}.breeze"
        if not path.exists():
            return False, "文件不存在"
        try:
            path.unlink()
            return True, "已删除文件"
        except OSError as exc:
            return False, f"文件被占用（{type(exc).__name__}），重启服务后可删除"
    def _stale_pids(self, exe: Path) -> list[int]:
        """同 exe、同端口的残留 breeze-server。

        典型场景：上次启动卡在模型加载（还没绑端口），端口看起来是空的，
        再点一次启动就会再拉起一个，最后变成一堆抢显存的僵尸进程。
        """
        exe_text = str(exe).lower()
        pattern = re.compile(rf"--port\s+{self.port}(?:\s|$)")
        stale: list[int] = []
        for item in _list_processes(EXE_NAME):
            cmd = item["cmd"]
            low = cmd.lower()
            if exe_text and exe_text not in low:
                continue
            has_port = "--port" in low
            if has_port:
                if not pattern.search(low):
                    continue  # 别的端口，不管
            elif self.port != DEFAULT_PORT:
                continue  # 没写 --port 等价于默认 9000
            stale.append(item["pid"])
        return stale

    def _other_instances(self, exe: Path) -> list[dict]:
        """同 exe、但端口不是当前配置端口的实例（换端口后留下的旧实例）。"""
        exe_text = str(exe).lower()
        others: list[dict] = []
        for item in _list_processes(EXE_NAME):
            low = item["cmd"].lower()
            if exe_text and exe_text not in low:
                continue
            match = re.search(r"--port\s+(\d+)", low)
            port = int(match.group(1)) if match else DEFAULT_PORT
            if port == self.port:
                continue
            others.append({"pid": item["pid"], "port": port})
        return others

    def _kill_others(self, exe: Path, wait: float = 8.0) -> list[dict]:
        """清掉其他端口的同 exe 实例（插件默认只保留一个模型服务）。"""
        targets = self._other_instances(exe)
        killed = []
        for item in targets:
            if _kill_pid(item["pid"]):
                killed.append(item)
        if killed:
            deadline = time.monotonic() + max(1.0, wait)
            pids = [k["pid"] for k in killed]
            while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
                time.sleep(0.2)
        return killed
    def _kill_stale(self, exe: Path, wait: float = 6.0) -> list[int]:
        """清理同端口残留并等它们真正退出，返回被清理的 PID。"""
        killed = [pid for pid in self._stale_pids(exe) if _kill_pid(pid)]
        if not killed:
            return []
        deadline = time.monotonic() + max(1.0, wait)
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in killed):
            time.sleep(0.2)
        return killed

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
                encoding='utf-8',
                errors='replace',
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
        # 先看「我们配置的端口现在被谁占着」——它才是当前真正在服务的那个进程；
        # state 文件可能残留上一次（甚至测试）留下的 pid。
        owner = self.port_owner() or 0
        if owner and _pid_alive(owner) and _looks_like_breeze(owner):
            return owner
        pid = int(self._read_state().get("pid") or 0)
        if pid and _pid_alive(pid) and _looks_like_breeze(pid):
            return pid
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
            # 运行中进程实际用的 --voices-dir（改过启动参数后要重启服务才生效）
            "voices_dir_running": self.running_args().get("--voices-dir", ""),
            "api": self.base_url,
            "webui": f"http://127.0.0.1:{self.port}/" if self.webui else "",
            "sample_rate": (health or {}).get("sample_rate", 0),

            "log_tail": log_lines,
            "log_file": str(self.log_file),
        }

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
        # 串行执行：并发启动（页面双击 + 自动拉起）时不会叠出两个实例
        with _START_LOCK:
            return self._start_locked(model_hint, wait)

    def _start_locked(self, model_hint: str = "", wait: float = 25.0) -> dict:
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

        # 不自动迁移：用户可能一直用 voice（旧脚本建的），就顺着他用；只有两个都没有时才新建 voices
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

        # 同 exe + 同端口的残留（上次卡在加载没绑端口的僵尸）先清掉，
        # 否则会再拉起一个，变成一堆抢显存的进程
        cleaned = self._kill_stale(exe)
        others = self._kill_others(exe)
        notes = []
        if cleaned:
            notes.append(f"已清理同端口残留进程 {', '.join(map(str, cleaned))}")
        if others:
            notes.append(
                "已停止其他端口的旧实例 "
                + ", ".join(f"{o['port']}(PID {o['pid']})" for o in others)
            )
        cleaned_note = f"（{'；'.join(notes)}）" if notes else ""

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
                "message": (
                    f"已启动，但 {elapsed:.0f} 秒还没就绪"
                    f"（首次加载模型比较慢，稍等再看状态）{cleaned_note}"
                ),
            }
        return {
            "ok": True,
            "ready": True,
            "pid": self._proc.pid,
            "model": str(model),
            "elapsed": round(elapsed, 1),
            "cleaned": cleaned,
            "stopped_others": others,
            "message": f"服务已就绪（{elapsed:.1f} 秒）{cleaned_note}",
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
        # 卡在加载、没绑端口的残留 + 其他端口的旧实例，一并收掉（默认只留一个服务）
        exe = self.exe()
        if exe:
            for pid in self._stale_pids(exe):
                add(pid)
            for item in self._other_instances(exe):
                add(item["pid"])

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
