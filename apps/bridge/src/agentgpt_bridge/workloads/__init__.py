"""Workload plugin registry."""

from __future__ import annotations

from .base import JobResult, JobStatus, JobSubmission, Workload, WorkloadInfo, WorkloadState
from .comfyui import ComfyUIWorkload
from .fake import FakeWorkload
from .openvoice import OpenVoiceWorkload

__all__ = [
    "ComfyUIWorkload",
    "FakeWorkload",
    "JobResult",
    "JobStatus",
    "JobSubmission",
    "OpenVoiceWorkload",
    "Workload",
    "WorkloadInfo",
    "WorkloadState",
]
