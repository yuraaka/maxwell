import asyncio
import time
from contextlib import asynccontextmanager


class ScanProgress:
    def __init__(self, job, callback, *, interval=15):
        self.job = job
        self.callback = callback
        self.interval = interval
        self.started = time.monotonic()
        self.phase = "preparing repository"

    def emit(self, *, heartbeat=False):
        if self.callback:
            self.callback(
                {
                    **self.job,
                    "state": "progress",
                    "phase": self.phase,
                    "elapsed": int(time.monotonic() - self.started),
                    "heartbeat": heartbeat,
                }
            )

    def update(self, phase):
        self.phase = phase
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
