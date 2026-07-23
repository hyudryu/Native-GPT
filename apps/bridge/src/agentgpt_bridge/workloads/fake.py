"""Fake workload for tests and local development.

Simulates the full lifecycle (start/stop/idle/job) without a GPU. Produces
deterministic placeholder outputs so the desktop side can be tested end-to-end
without real ComfyUI or OpenVoice.
"""

from __future__ import annotations

import uuid
from typing import Any

from .base import JobResult, JobStatus, Workload, WorkloadState


class FakeWorkload(Workload):
    """A workload that needs no GPU — for tests and dry runs."""

    workload_id: str = "fake"
    display_name: str = "Fake Workload"
    description: str = "A fake workload for testing lifecycle and job submission."

    def __init__(self) -> None:
        super().__init__()
        self.start_call_count = 0
        self.stop_call_count = 0
        self.soft_idle_call_count = 0
        self.jobs_submitted: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.start_call_count += 1
        self.jobs_submitted = []
        self.state = WorkloadState.STARTING
        # Simulate near-instant startup.
        self.state = WorkloadState.READY
        self.last_health = 0.0
        self.touch()

    async def stop(self) -> None:
        self.stop_call_count += 1
        self.state = WorkloadState.STOPPING
        self.state = WorkloadState.STOPPED

    async def soft_idle(self) -> None:
        self.soft_idle_call_count += 1
        # Fake supports in-process "VRAM release" — just record it.

    async def submit_job(self, job: dict[str, Any]) -> JobResult:
        self.ensure_ready()
        self.jobs_submitted.append(job)
        self.state = WorkloadState.BUSY
        self.touch()
        job_id = str(uuid.uuid4())
        # Produce a deterministic fake output based on job kind.
        outputs = self._fake_outputs(job)
        self.state = WorkloadState.READY
        return JobResult(
            job_id=job_id,
            status=JobStatus.DONE,
            outputs=outputs,
            summary=f"fake job {job_id} completed",
        )

    def _fake_outputs(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        kind = job.get("output_kind") or job.get("kind") or "image"
        # For TTS, produce audio; for image gen, produce an image.
        if job.get("text") is not None:
            # Looks like a TTS job.
            return [
                {
                    "kind": "audio",
                    "mime_type": "audio/mpeg",
                    "bytes": b"FAKE_AUDIO_DATA",
                    "asset_token": f"fake-audio-{uuid.uuid4()}",
                }
            ]
        return [
            {
                "kind": kind,
                "mime_type": "image/png" if kind == "image" else "video/mp4",
                "bytes": b"FAKE_IMAGE_DATA",
                "asset_token": f"fake-{kind}-{uuid.uuid4()}",
            }
        ]
