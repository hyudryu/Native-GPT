"""Workload plugin protocol.

Each workload is a controller that manages a GPU subprocess: start on demand,
stop after idle, unload/release VRAM when idle, and submit jobs. ComfyUI and
OpenVoice are two implementations; a FakeWorkload is used for tests.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class WorkloadState(StrEnum):
    """Lifecycle state of a workload."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    ERROR = "error"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class JobResult:
    """A completed job's output."""

    job_id: str
    status: JobStatus
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    summary: str = ""


class JobSubmission(BaseModel):
    """Inbound job request (validated by the workload)."""

    model_config = {"extra": "allow"}


class WorkloadInfo(BaseModel):
    """Public-facing workload description for /health and /workloads."""

    id: str
    name: str
    state: str
    healthy: bool
    version: str | None = None
    description: str = ""


class Workload(abc.ABC):
    """Base class for a managed GPU workload.

    Lifecycle:
        start()  -> state transitions stopped -> starting -> ready
        stop()   -> state transitions * -> stopping -> stopped
        soft_idle() -> release VRAM without stopping (if supported)
        submit_job(job) -> busy -> ready
    """

    workload_id: str = ""
    display_name: str = ""
    description: str = ""

    def __init__(self) -> None:
        self.state: WorkloadState = WorkloadState.STOPPED
        self.last_used: float = 0.0
        self.last_health: float = 0.0
        self.error_message: str | None = None
        self.soft_idle_seconds: float = 300.0  # 5 min default
        self.hard_idle_seconds: float = 900.0  # 15 min default

    @property
    def healthy(self) -> bool:
        """Whether the workload is healthy and ready to accept jobs."""
        return self.state in (WorkloadState.READY, WorkloadState.BUSY)

    def info(self) -> WorkloadInfo:
        return WorkloadInfo(
            id=self.workload_id,
            name=self.display_name,
            state=self.state.value,
            healthy=self.healthy,
            description=self.description,
        )

    def touch(self) -> None:
        """Record that the workload was just used."""
        self.last_used = time.monotonic()

    def idle_for(self) -> float:
        """Seconds since the workload was last used."""
        if self.last_used == 0.0:
            return float("inf")
        return time.monotonic() - self.last_used

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the workload (spawn the GPU subprocess)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the workload (kill the subprocess, release all VRAM)."""

    async def soft_idle(self) -> None:  # noqa: B027
        """Release VRAM without stopping the process (if supported).

        Override in workloads that support in-process VRAM release (e.g.
        ComfyUI's POST /free). The default implementation is a no-op.
        """
        # Base no-op: workloads without an unload API override this to call
        # stop() (e.g. OpenVoice tears down its worker process).

    @abc.abstractmethod
    async def submit_job(self, job: dict[str, Any]) -> JobResult:
        """Submit a job and return the result. The workload must be started."""

    async def health_check(self) -> bool:
        """Check if the underlying process is alive and responsive."""
        return self.state in (WorkloadState.READY, WorkloadState.BUSY)

    def ensure_ready(self) -> None:
        """Raise if the workload isn't ready to accept a job."""
        if self.state == WorkloadState.STOPPED:
            raise RuntimeError(f"{self.workload_id} is stopped; call start() first")
        if self.state == WorkloadState.ERROR:
            raise RuntimeError(f"{self.workload_id} is in error state: {self.error_message}")
