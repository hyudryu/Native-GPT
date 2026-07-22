"""Tests for the human-in-the-loop approval registry.

The registry lives across two threads: the per-run event loop thread (which
creates futures) and the stdin dispatcher thread (which resolves them). These
tests exercise both paths, plus the cancel-run cleanup.
"""

from __future__ import annotations

import asyncio
import threading

from agentgpt_runtime.approvals import ApprovalRegistry


def test_create_returns_future_that_resolves_approved() -> None:
    """A future created on a loop resolves to the approved value from another thread."""
    registry = ApprovalRegistry()
    result_holder: list[bool] = []
    loop_ready = threading.Event()
    test_done = threading.Event()

    def run_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main() -> None:
            future = await registry.create("a-1", "run-x", "approve?", loop)
            loop_ready.set()
            result_holder.append(await future)
            test_done.set()

        loop.run_until_complete(main())
        loop.close()

    t = threading.Thread(target=run_thread, daemon=True)
    t.start()
    assert loop_ready.wait(timeout=2.0), "loop did not start"
    # Resolve from the main thread (simulating the dispatcher).
    resolved = registry.resolve("a-1", approved=True)
    assert resolved is True
    assert test_done.wait(timeout=2.0), "future did not resolve"
    assert result_holder == [True]
    t.join(timeout=2.0)


def test_resolve_unknown_approval_id_returns_false() -> None:
    registry = ApprovalRegistry()
    assert registry.resolve("never-existed", approved=True) is False


def test_cancel_run_denies_all_pending_for_that_run() -> None:
    registry = ApprovalRegistry()
    results: list[bool] = []
    loop_ready = threading.Event()
    test_done = threading.Event()

    def run_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main() -> None:
            f1 = await registry.create("p-1", "run-a", "1?", loop)
            f2 = await registry.create("p-2", "run-a", "2?", loop)
            # Different run — must NOT be cancelled.
            f3 = await registry.create("p-3", "run-b", "3?", loop)
            loop_ready.set()
            results.append(await f1)
            results.append(await f2)
            results.append(await f3)
            test_done.set()

        loop.run_until_complete(main())
        loop.close()

    t = threading.Thread(target=run_thread, daemon=True)
    t.start()
    assert loop_ready.wait(timeout=2.0)
    # Cancel run-a; both its pending approvals resolve to False.
    cancelled_count = registry.cancel_run("run-a")
    assert cancelled_count == 2
    # Resolve run-b's approval normally.
    assert registry.resolve("p-3", approved=True) is True
    assert test_done.wait(timeout=2.0)
    assert results == [False, False, True]
    t.join(timeout=2.0)


def test_pending_count_tracks_outstanding() -> None:
    registry = ApprovalRegistry()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(registry.create("c-1", "r", "x?", loop))
        loop.run_until_complete(registry.create("c-2", "r", "y?", loop))
        assert registry.pending_count() == 2
        registry.resolve("c-1", True)
        # Pending count drops immediately because resolve() pops synchronously.
        assert registry.pending_count() == 1
    finally:
        loop.close()


def test_resolve_after_future_already_done_is_a_noop() -> None:
    """Resolving twice doesn't crash — the second call is a no-op."""
    registry = ApprovalRegistry()
    loop = asyncio.new_event_loop()
    try:
        future = loop.run_until_complete(registry.create("d-1", "r", "x?", loop))
        assert registry.resolve("d-1", True) is True
        # Pump the loop so the call_soon_threadsafe callback runs.
        loop.run_until_complete(asyncio.sleep(0.01))
        assert future.done() and future.result() is True
        # Second resolve: no matching id anymore.
        assert registry.resolve("d-1", False) is False
    finally:
        loop.close()
