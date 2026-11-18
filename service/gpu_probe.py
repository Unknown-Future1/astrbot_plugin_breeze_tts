"""Windows 显卡显存探测：只用系统自带能力，A/N 卡通吃。

- 总容量：注册表 `HardwareInformation.qwMemorySize`（64 位，A/N 通用）
- 已用：性能计数器 `\\GPU Adapter Memory(*)\\Dedicated Usage`（PDH，A/N 通用）
- 免费：总容量 - 已用 - 安全余量

PDH 首次读取要加载计数器提供者（约 0.3~0.4 秒），之后基本 0 开销；
读不到就返回 None，由调用方禁用监视（不影响主流程）。
"""

from __future__ import annotations

import time

from astrbot.api import logger

DEFAULT_MARGIN_MB = 0
REG_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
PDH_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"


def _read_capacity_mb() -> int:
    try:
        import winreg
    except ImportError:
        return 0
    best = 0
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_CLASS)
    except OSError:
        return 0
    idx = 0
    while True:
        try:
            name = winreg.EnumKey(root, idx)
        except OSError:
            break
        idx += 1
        try:
            with winreg.OpenKey(root, name) as sub:
                value, _ = winreg.QueryValueEx(sub, "HardwareInformation.qwMemorySize")
                best = max(best, int(value))
        except OSError:
            continue
    return int(best / 1024**2) if best else 0


class GpuProbe:
    """显存探测器；不可用时 available=False。"""

    def __init__(self, margin_mb: int = DEFAULT_MARGIN_MB) -> None:
        self.margin_mb = max(0, int(margin_mb))
        self.backend = "none"
        self.total_mb = 0
        self._pdh = None
        self._init()

    def _init(self) -> None:
        self.total_mb = _read_capacity_mb()
        try:
            import win32pdh  # noqa: F401

            self.backend = "windows-pdh"
            self._pdh = win32pdh
        except ImportError:
            self.backend = "none"
        if self.total_mb <= 0:
            self.backend = "none"
        if self.backend == "none":
            logger.info("[BreezeTTS] 显存探测不可用（非 Windows 或缺少计数器）")
        else:
            logger.info(
                f"[BreezeTTS] 显存探测就绪：backend={self.backend}，"
                f"总容量 {self.total_mb} MB，余量阈值 {self.margin_mb} MB"
            )

    @property
    def available(self) -> bool:
        return self.backend != "none" and self.total_mb > 0

    def used_mb(self) -> int | None:
        """已用显存（所有适配器取最大）；失败返回 None。"""
        if not self.available:
            return None
        try:
            query = self._pdh.OpenQuery()
            try:
                counter = self._pdh.AddCounter(query, PDH_PATH)
                self._pdh.CollectQueryData(query)
                data = self._pdh.GetFormattedCounterArray(
                    counter, self._pdh.PDH_FMT_LARGE
                )
            finally:
                self._pdh.CloseQuery(query)
            if not data:
                return None
            return int(max(data.values()) / 1024**2)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[BreezeTTS] 读取显存失败：{exc}")
            return None

    def free_mb(self) -> int | None:
        used = self.used_mb()
        if used is None:
            return None
        return max(0, self.total_mb - used - self.margin_mb)
