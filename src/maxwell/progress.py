import asyncio
import time
from contextlib import asynccontextmanager


class ScanProgress:
    def __init__(self, job, callback, *, interval=None):
        self.job = job
        self.callback = callback
        self.interval = (
            interval if interval is not None else getattr(callback, "refresh_interval", 15)
        )
        self.started = time.monotonic()
        self.phase = "preparing repository"
        self.details = {}

    def emit(self, *, heartbeat=False):
        if self.callback:
            self.callback(
                {
                    **self.job,
                    **self.details,
                    "state": "progress",
                    "phase": self.phase,
                    "elapsed": int(time.monotonic() - self.started),
                    "heartbeat": heartbeat,
                }
            )

    def update(self, phase):
        if isinstance(phase, dict):
            self.phase = phase["phase"]
            self.details = {key: value for key, value in phase.items() if key != "phase"}
        else:
            self.phase = phase
            self.details = {}
        self.emit()

    async def heartbeat(self):
        while True:
            await asyncio.sleep(self.interval)
            self.emit(heartbeat=True)

    @asynccontextmanager
    async def watch(self):
        task = asyncio.create_task(self.heartbeat()) if self.callback else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
