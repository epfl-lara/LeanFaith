"""Bounded host/GPU/vLLM telemetry for SFT2B throughput probes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated

from pydantic import Field

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.durable import atomic_write

_METRIC = re.compile(
    r"^(?P<name>vllm:(?:num_requests_running|num_requests_waiting))"
    r"(?:\{[^}]*\})?\s+(?P<value>-?[0-9.eE+]+)$"
)


class GpuTelemetry(StrictModel):
    index: Annotated[int, Field(ge=0)]
    uuid: Annotated[str, Field(min_length=1)]
    memory_used_mib: Annotated[int, Field(ge=0)]
    memory_total_mib: Annotated[int, Field(ge=1)]
    utilization_gpu_percent: Annotated[int, Field(ge=0, le=100)]
    power_draw_watts: Annotated[float, Field(ge=0.0)]


class TelemetrySample(StrictModel):
    schema_version: str = "sft2b_vllm_telemetry_sample_v1"
    monotonic_ns: Annotated[int, Field(ge=0)]
    unix_time_ns: Annotated[int, Field(ge=0)]
    gpus: tuple[GpuTelemetry, ...]
    requests_running: Annotated[float, Field(ge=0.0)] | None = None
    requests_waiting: Annotated[float, Field(ge=0.0)] | None = None
    server_process_tree_rss_bytes: Annotated[int, Field(ge=0)] | None = None
    system_ram_used_bytes: Annotated[int, Field(ge=0)]
    system_ram_available_bytes: Annotated[int, Field(ge=0)]


def _gpu_sample() -> tuple[GpuTelemetry, ...]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,memory.used,memory.total,utilization.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {completed.stderr.strip()}")
    values: list[GpuTelemetry] = []
    for line in completed.stdout.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 6:
            raise RuntimeError("nvidia-smi returned an unexpected column count")
        values.append(
            GpuTelemetry(
                index=int(fields[0]),
                uuid=fields[1],
                memory_used_mib=int(fields[2]),
                memory_total_mib=int(fields[3]),
                utilization_gpu_percent=int(fields[4]),
                power_draw_watts=float(fields[5]),
            )
        )
    if len(values) != 8:
        raise RuntimeError(f"expected eight GPUs, observed {len(values)}")
    return tuple(values)


def _vllm_metrics(endpoint_url: str) -> tuple[float | None, float | None]:
    suffix = "/v1/completions"
    if not endpoint_url.endswith(suffix):
        raise RuntimeError("vLLM endpoint URL has an unexpected suffix")
    url = endpoint_url[: -len(suffix)] + "/metrics"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            payload = response.read().decode("utf-8", errors="strict")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None, None
    running = 0.0
    waiting = 0.0
    saw_running = False
    saw_waiting = False
    for line in payload.splitlines():
        match = _METRIC.fullmatch(line.strip())
        if match is None:
            continue
        value = float(match.group("value"))
        if match.group("name") == "vllm:num_requests_running":
            running += value
            saw_running = True
        else:
            waiting += value
            saw_waiting = True
    return running if saw_running else None, waiting if saw_waiting else None


def _memory_sample() -> tuple[int, int]:
    fields: dict[str, int] = {}
    with Path("/proc/meminfo").open(encoding="utf-8") as handle:
        for line in handle:
            name, value = line.split(":", 1)
            fields[name] = int(value.strip().split()[0]) * 1024
    total = fields["MemTotal"]
    available = fields["MemAvailable"]
    return total - available, available


def _descendants(pid: int) -> set[int]:
    pending = [pid]
    found: set[int] = set()
    while pending:
        current = pending.pop()
        if current in found:
            continue
        proc = Path("/proc") / str(current)
        if not proc.exists():
            continue
        found.add(current)
        children_path = proc / "task" / str(current) / "children"
        try:
            children = [int(item) for item in children_path.read_text().split()]
        except (FileNotFoundError, PermissionError, ValueError):
            children = []
        pending.extend(children)
    return found


def _process_tree_rss(pid: int | None) -> int | None:
    if pid is None:
        return None
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    total = 0
    saw_process = False
    for child in _descendants(pid):
        try:
            fields = (Path("/proc") / str(child) / "statm").read_text().split()
            total += int(fields[1]) * page_size
            saw_process = True
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    return total if saw_process else None


def collect_sample(endpoint_url: str, server_pid: int | None) -> TelemetrySample:
    running, waiting = _vllm_metrics(endpoint_url)
    used, available = _memory_sample()
    return TelemetrySample(
        monotonic_ns=time.monotonic_ns(),
        unix_time_ns=time.time_ns(),
        gpus=_gpu_sample(),
        requests_running=running,
        requests_waiting=waiting,
        server_process_tree_rss_bytes=_process_tree_rss(server_pid),
        system_ram_used_bytes=used,
        system_ram_available_bytes=available,
    )


class TelemetryMonitor:
    """Sample bounded telemetry in one daemon thread around a request batch."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        interval_seconds: float,
        server_pid: int | None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.interval_seconds = interval_seconds
        self.server_pid = server_pid
        self.samples: list[TelemetrySample] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sft2b-vllm-telemetry", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append(collect_sample(self.endpoint_url, self.server_pid))
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(10.0, self.interval_seconds * 4))
        if self._thread.is_alive():
            raise RuntimeError("vLLM telemetry thread did not stop")
        if not self.samples:
            self.samples.append(collect_sample(self.endpoint_url, self.server_pid))

    def write(self, path: Path) -> None:
        payload = b"".join(
            canonical_json_bytes(sample.model_dump(mode="json")) + b"\n" for sample in self.samples
        )
        atomic_write(path, payload)

    def summary(self) -> dict[str, object]:
        gpu_indices = sorted({gpu.index for sample in self.samples for gpu in sample.gpus})
        peak_by_gpu = {
            str(index): {
                "memory_used_mib": max(
                    gpu.memory_used_mib
                    for sample in self.samples
                    for gpu in sample.gpus
                    if gpu.index == index
                ),
                "utilization_gpu_percent": max(
                    gpu.utilization_gpu_percent
                    for sample in self.samples
                    for gpu in sample.gpus
                    if gpu.index == index
                ),
                "power_draw_watts": max(
                    gpu.power_draw_watts
                    for sample in self.samples
                    for gpu in sample.gpus
                    if gpu.index == index
                ),
            }
            for index in gpu_indices
        }
        running = [
            item.requests_running for item in self.samples if item.requests_running is not None
        ]
        waiting = [
            item.requests_waiting for item in self.samples if item.requests_waiting is not None
        ]
        rss = [
            item.server_process_tree_rss_bytes
            for item in self.samples
            if item.server_process_tree_rss_bytes is not None
        ]
        return {
            "schema_version": "sft2b_vllm_telemetry_summary_v1",
            "samples": len(self.samples),
            "errors": list(self.errors),
            "peak_by_gpu": peak_by_gpu,
            "max_requests_running": max(running) if running else None,
            "max_requests_waiting": max(waiting) if waiting else None,
            "peak_server_process_tree_rss_bytes": max(rss) if rss else None,
            "peak_system_ram_used_bytes": max(item.system_ram_used_bytes for item in self.samples),
            "minimum_system_ram_available_bytes": min(
                item.system_ram_available_bytes for item in self.samples
            ),
        }


def load_samples(path: Path) -> tuple[TelemetrySample, ...]:
    rows: list[TelemetrySample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            rows.append(TelemetrySample.model_validate(json.loads(line)))
    return tuple(rows)
