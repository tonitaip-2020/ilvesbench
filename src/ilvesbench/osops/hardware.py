from __future__ import annotations

import ctypes
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import shutil

from ilvesbench.config import DockerConfig
from ilvesbench.models import HardwareSnapshot

from .subprocesses import SubprocessRunner


class HardwareInspector:
    def __init__(self, docker: DockerConfig, runner: SubprocessRunner | None = None) -> None:
        self._docker = docker
        self._runner = runner or SubprocessRunner()

    def collect(self, root_path: str | Path = ".") -> HardwareSnapshot:
        target = Path(root_path).resolve()
        host_raw = self._collect_host_raw()
        container_snapshot = self._collect_container_snapshot()

        if container_snapshot is not None and self._docker.prefer_container_snapshot:
            container_snapshot.raw["host_snapshot"] = host_raw
            return container_snapshot

        disk = shutil.disk_usage(target)
        raw: dict[str, object] = {"host_snapshot": host_raw}
        if container_snapshot is not None:
            raw["docker_snapshot"] = container_snapshot.raw
        return HardwareSnapshot(
            collected_at=datetime.now(UTC).isoformat(),
            scope="host",
            platform=platform.platform(),
            cpu_count=os.cpu_count() or 0,
            architecture=platform.machine(),
            memory_total_bytes=self._memory_total_bytes(),
            disk_total_bytes=disk.total,
            disk_free_bytes=disk.free,
            container_name=None,
            raw=raw,
        )

    def _collect_host_raw(self) -> dict[str, str]:
        raw: dict[str, str] = {}
        for command in (["uname", "-a"], ["lscpu"], ["free", "-b"], ["lsblk", "-b"]):
            result = self._safe_run(command)
            if result:
                raw[" ".join(command)] = result
        return raw

    def _collect_container_snapshot(self) -> HardwareSnapshot | None:
        container_name = (self._docker.postgres_container_name or "").strip()
        if not container_name or self._runner.which("docker") is None:
            return None

        inspect_result = self._runner.run(["docker", "inspect", container_name], timeout=15)
        if inspect_result.returncode != 0:
            return None

        try:
            payload = json.loads(inspect_result.stdout)
            info = payload[0]
        except Exception:
            return None

        host_config = info.get("HostConfig", {})
        config = info.get("Config", {})
        state = info.get("State", {})

        nano_cpus = int(host_config.get("NanoCpus") or 0)
        cpu_quota = int(host_config.get("CpuQuota") or 0)
        cpu_period = int(host_config.get("CpuPeriod") or 0)
        if nano_cpus > 0:
            cpu_count: int | None = max(1, nano_cpus // 1_000_000_000)
        elif cpu_quota > 0 and cpu_period > 0:
            cpu_count = max(1, cpu_quota // cpu_period)
        else:
            cpu_count = None

        memory_limit = int(host_config.get("Memory") or 0) or None
        raw: dict[str, object] = {
            "docker_inspect": info,
            "container_state": {
                "status": state.get("Status"),
                "running": state.get("Running"),
                "started_at": state.get("StartedAt"),
            },
            "limits": {
                "nano_cpus": nano_cpus,
                "cpu_quota": cpu_quota,
                "cpu_period": cpu_period,
                "memory_bytes": memory_limit,
            },
            "container_config": {
                "image": config.get("Image"),
                "hostname": config.get("Hostname"),
            },
        }

        stats_result = self._runner.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", container_name],
            timeout=15,
        )
        if stats_result.returncode == 0 and stats_result.stdout.strip():
            raw["docker_stats"] = stats_result.stdout.strip()

        return HardwareSnapshot(
            collected_at=datetime.now(UTC).isoformat(),
            scope="docker_container",
            platform="docker-container",
            cpu_count=cpu_count,
            architecture="container-runtime",
            memory_total_bytes=memory_limit,
            disk_total_bytes=None,
            disk_free_bytes=None,
            container_name=container_name,
            raw=raw,
        )

    def _memory_total_bytes(self) -> int | None:
        system = platform.system().lower()
        if system == "linux":
            meminfo = Path("/proc/meminfo")
            if meminfo.exists():
                for line in meminfo.read_text(encoding="utf-8").splitlines():
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        return int(parts[1]) * 1024
        if system == "darwin":
            output = self._safe_run(["sysctl", "-n", "hw.memsize"])
            if output and output.strip().isdigit():
                return int(output.strip())
        if system == "windows":
            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        return None

    def _safe_run(self, args: list[str]) -> str | None:
        if self._runner.which(args[0]) is None:
            return None
        result = self._runner.run(args, timeout=10)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
