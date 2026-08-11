from __future__ import annotations

import ctypes
import os
from dataclasses import asdict, dataclass
from typing import Any


GIB = 1024 ** 3


@dataclass(frozen=True)
class MemorySnapshot:
    commit_used_bytes: int
    commit_limit_bytes: int
    physical_available_bytes: int
    physical_total_bytes: int
    process_count: int = 0
    handle_count: int = 0

    @property
    def commit_free_bytes(self) -> int:
        return max(self.commit_limit_bytes - self.commit_used_bytes, 0)

    @property
    def commit_ratio(self) -> float:
        if self.commit_limit_bytes <= 0:
            return 0.0
        return self.commit_used_bytes / self.commit_limit_bytes

    def as_report(self) -> dict[str, Any]:
        report = asdict(self)
        report.update(
            {
                "commit_used_gb": round(self.commit_used_bytes / GIB, 2),
                "commit_limit_gb": round(self.commit_limit_bytes / GIB, 2),
                "commit_free_gb": round(self.commit_free_bytes / GIB, 2),
                "commit_percent": round(self.commit_ratio * 100, 1),
                "physical_available_gb": round(self.physical_available_bytes / GIB, 2),
                "physical_total_gb": round(self.physical_total_bytes / GIB, 2),
            }
        )
        return report


@dataclass(frozen=True)
class ResourcePreflightResult:
    ok: bool
    reason: str
    message: str
    snapshot: MemorySnapshot | None

    def as_report(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
            "snapshot": self.snapshot.as_report() if self.snapshot else None,
        }


class _PerformanceInformation(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_ulong),
        ("ProcessCount", ctypes.c_ulong),
        ("ThreadCount", ctypes.c_ulong),
    ]


def read_memory_snapshot() -> MemorySnapshot | None:
    """Read Windows commit and physical-memory counters without extra packages."""
    if os.name != "nt":
        return None
    info = _PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_performance_info = psapi.GetPerformanceInfo
    get_performance_info.argtypes = [ctypes.POINTER(_PerformanceInformation), ctypes.c_ulong]
    get_performance_info.restype = ctypes.c_int
    if not get_performance_info(ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    page_size = int(info.PageSize)
    return MemorySnapshot(
        commit_used_bytes=int(info.CommitTotal) * page_size,
        commit_limit_bytes=int(info.CommitLimit) * page_size,
        physical_available_bytes=int(info.PhysicalAvailable) * page_size,
        physical_total_bytes=int(info.PhysicalTotal) * page_size,
        process_count=int(info.ProcessCount),
        handle_count=int(info.HandleCount),
    )


def evaluate_cad_resources(
    snapshot: MemorySnapshot | None,
    *,
    max_commit_ratio: float = 0.90,
    min_commit_free_gb: float = 2.0,
    min_physical_free_gb: float = 1.0,
) -> ResourcePreflightResult:
    if snapshot is None:
        return ResourcePreflightResult(True, "resource_probe_unavailable", "Resource probe unavailable; execution allowed.", None)

    reasons: list[str] = []
    if snapshot.commit_ratio >= max_commit_ratio:
        reasons.append("commit_ratio_high")
    if snapshot.commit_free_bytes < int(min_commit_free_gb * GIB):
        reasons.append("commit_headroom_low")
    if snapshot.physical_available_bytes < int(min_physical_free_gb * GIB):
        reasons.append("physical_memory_low")
    if reasons:
        report = snapshot.as_report()
        return ResourcePreflightResult(
            False,
            "+".join(reasons),
            (
                "系统内存不足，已在启动 CAD 前阻断任务："
                f"提交内存 {report['commit_used_gb']}/{report['commit_limit_gb']} GB "
                f"({report['commit_percent']}%)，可用物理内存 {report['physical_available_gb']} GB。"
                "请关闭非 CAD 程序或重启 Windows 后重试。"
            ),
            snapshot,
        )
    return ResourcePreflightResult(True, "resources_available", "CAD resource preflight passed.", snapshot)


def cad_resource_preflight() -> dict[str, Any]:
    try:
        snapshot = read_memory_snapshot()
    except Exception as exc:
        return ResourcePreflightResult(
            True,
            "resource_probe_failed",
            f"Resource probe failed; execution allowed: {exc}",
            None,
        ).as_report()
    return evaluate_cad_resources(
        snapshot,
        max_commit_ratio=float(os.getenv("CAD_AGENT_MAX_COMMIT_RATIO", "0.90")),
        min_commit_free_gb=float(os.getenv("CAD_AGENT_MIN_COMMIT_FREE_GB", "2.0")),
        min_physical_free_gb=float(os.getenv("CAD_AGENT_MIN_PHYSICAL_FREE_GB", "1.0")),
    ).as_report()
