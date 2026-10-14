import asyncio
import os

import pytest

from maxwell.config import Config, Paths
from maxwell.provider import create_provider


def test_scan_timeout_covers_blocked_prompt_delivery(directory):
    executable = directory / "blocked-provider"
    executable.write_text(
        f"#!{os.sys.executable}\n"
        "import os, time\n"
        "os.write(1, b'{\"type\":\"started\"}\\n')\n"
        "for _ in range(128):\n"
        "    os.write(1, b'x' * 8191 + b'\\n')\n"
        "time.sleep(60)\n"
    )
    executable.chmod(0o700)
    config = Config.model_validate(
        {
            "provider": {
                "executable": str(executable),
                "model": "fake-model",
                "api_key": "fake-key",
            },
            "limits": {"scan_timeout_seconds": 1},
        }
    )
    provider = create_provider(Paths(directory / "home"), config)
    artifact = directory / "artifact"
    artifact.mkdir()
    pids = []

    async def scan():
        async with asyncio.timeout(4):
            with pytest.raises(TimeoutError):
                await provider.scan(
                    directory, artifact, "x" * (2 * 1024 * 1024), pids.append
                )

    asyncio.run(scan())
    assert (artifact / "events.jsonl").read_text().startswith('{"type":"started"}\n')
    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
