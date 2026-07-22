"""Approval registry for the human-in-the-loop intervention.

Maps approval_id -> asyncio.Future, keyed by approval_id so the runtime's
stdin dispatcher thread (which sees `run.approve` envelopes from the host)
can resolve futures created on the per-run event loop.

Thread-safety: `create` runs on the run's event loop thread; `resolve` runs
on the dispatcher thread. We bridge with `loop.call_soon_threadsafe` so the
Future's result is set on its own loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    """A single in-flight approval prompt."""

    run_id: str
    future: asyncio.Future[bool]
    prompt: str


@dataclass
class ApprovalRegistry:
    """Tracks pending approvals across all active runs.

    One instance per runtime process. Thread-safe enough for the two-thread
    split (run thread creates, dispatcher thread resolves).
    """

    _pending: dict[str, PendingApproval] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def create(
        self,
        approval_id: str,
        run_id: str,
        prompt: str,
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Future[bool]:
        """Register a new approval prompt. Returns a Future the caller awaits."""
        future: asyncio.Future[bool] = loop.create_future()
        async with self._lock:
            if approval_id in self._pending:
                # Should never happen — approval_ids are uuids. Fail closed.
                logger.warning("duplicate approval_id %s — denying both", approval_id)
                self._pending[approval_id].future.cancel()
            self._pending[approval_id] = PendingApproval(
                run_id=run_id, future=future, prompt=prompt
            )
        return future

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """Resolve a pending approval. Returns True if a matching approval existed.

        Safe to call from any thread — schedules the future resolution on the
        future's own loop.
        """
        pending = self._pending.pop(approval_id, None)
        if pending is None:
            return False
        # Schedule the future resolution on its own loop (the run thread's loop).
        loop = pending.future.get_loop()
        def _settle() -> None:
            if not pending.future.done():
                pending.future.set_result(approved)
        loop.call_soon_threadsafe(_settle)
        return True

    def cancel_run(self, run_id: str) -> int:
        """Deny all pending approvals for a run. Returns the count cancelled.

        Used when a run is cancelled — we don't want a dangling approval prompt
        after the user already hit Stop.
        """
        cancelled: list[PendingApproval] = []
        for approval_id, pending in list(self._pending.items()):
            if pending.run_id == run_id:
                cancelled.append(pending)
                self._pending.pop(approval_id, None)
        for pending in cancelled:
            loop = pending.future.get_loop()
            def _settle(f: asyncio.Future[bool] = pending.future) -> None:
                if not f.done():
                    f.set_result(False)
            loop.call_soon_threadsafe(_settle)
        return len(cancelled)

    def pending_count(self) -> int:
        """Diagnostic: how many approvals are currently outstanding."""
        return len(self._pending)
