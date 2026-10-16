import asyncio

import pytest

from maxwell.progress import ScanProgress


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_silent_operation_emits_heartbeat_and_stops_on_every_exit(outcome):
    async def scenario():
        events = []
        received = asyncio.Event()

        def callback(event):
            events.append(event)
            if event["heartbeat"]:
                received.set()

        progress = ScanProgress({"repository": "acme/repo"}, callback, interval=0.01)

        async def operation():
            async with progress.watch():
                progress.update("agent running")
                await asyncio.wait_for(received.wait(), timeout=1)
                if outcome == "failed":
                    raise ValueError("failed")
                if outcome == "cancelled":
                    await asyncio.Event().wait()

        task = asyncio.create_task(operation())
        await asyncio.wait_for(received.wait(), timeout=1)
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failed":
            with pytest.raises(ValueError, match="failed"):
                await task
        else:
            await task
        assert events[0]["heartbeat"] is False
        assert events[-1]["heartbeat"] is True
        assert all(event["phase"] == "agent running" for event in events)
        assert all(event["elapsed"] >= 0 for event in events)
        count = len(events)
        await asyncio.sleep(0.03)
        assert len(events) == count
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


def test_json_mode_creates_no_heartbeat_task():
    async def scenario():
        progress = ScanProgress({"repository": "acme/repo"}, None)
        async with progress.watch():
            progress.update("agent running")
            assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


def test_heartbeat_tracks_current_phase_and_total_elapsed(monkeypatch):
    now = [100]
    monkeypatch.setattr("maxwell.progress.time.monotonic", lambda: now[0])
    events = []
    progress = ScanProgress({"repository": "acme/repo"}, events.append)
    progress.update("agent running")
    now[0] = 175
    progress.update("publishing results to GitHub")
    progress.emit(heartbeat=True)
    assert events[-1]["phase"] == "publishing results to GitHub"
    assert events[-1]["elapsed"] == 75


def test_progress_uses_terminal_refresh_and_preserves_measurement_fraction():
    class Callback:
        refresh_interval = 0.12

        def __init__(self):
            self.events = []

        def __call__(self, event):
            self.events.append(event)

    callback = Callback()
    progress = ScanProgress({"repository": "acme/repo"}, callback)
    assert progress.interval == 0.12
    progress.update({"phase": "test run 1/2 · 3/10 tests", "stage_fraction": 0.325})
    progress.emit(heartbeat=True)
    assert callback.events[-1]["stage_fraction"] == 0.325
    assert callback.events[-1]["phase"] == "test run 1/2 · 3/10 tests"
    progress.update("publishing measurement")
    assert "stage_fraction" not in callback.events[-1]
