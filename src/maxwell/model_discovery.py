import asyncio
import json
import tempfile
from pathlib import Path

from maxwell import __version__
from maxwell.config import ModelSettings, Paths
from maxwell.provider import create_provider
from maxwell.provider_runtime import (
    codex_connection_overrides,
    native_settings,
    redact,
    runtime_environment,
)
from maxwell.repositories import terminate


class MetadataClient:
    def __init__(self, process):
        self.process = process
        self.size = 0
        self.stderr = b""

    async def capture_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr = (self.stderr + chunk)[-4000:]

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self):
        while line := await self.process.stdout.readline():
            self.size += len(line)
            if self.size > 8 * 1024 * 1024:
                raise ValueError("Model discovery output exceeded 8 MiB")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message
        code = await self.process.wait()
        raise ValueError(f"Model discovery exited with status {code}")

    async def codex_request(self, request_id, method, params):
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = (
                    error.get("message", "Request rejected")
                    if isinstance(error, dict)
                    else "Request rejected"
                )
                raise ValueError(str(detail))
            result = message.get("result")
            if not isinstance(result, dict):
                raise ValueError("Invalid model discovery response")
            return result

    async def codex_models(self):
        await self.codex_request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "maxwell", "version": __version__},
                "capabilities": {},
            },
        )
        await self.send({"method": "initialized", "params": {}})
        rows, cursors = [], set()
        cursor = None
        for request_id in range(2, 202):
            params = {"limit": 100, "includeHidden": True}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.codex_request(request_id, "model/list", params)
            data = result.get("data")
            if not isinstance(data, list):
                raise ValueError("Invalid Codex model list")
            rows.extend(data)
            cursor = result.get("nextCursor")
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Invalid Codex model pagination")
            cursors.add(cursor)
        raise ValueError("Codex model list exceeded 200 pages")

    async def claude_models(self):
        request_id = "maxwell-models"
        await self.send(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "initialize", "hooks": {}},
            }
        )
        while True:
            message = await self.receive()
            if message.get("type") == "control_request":
                raise ValueError("Claude discovery requested an unexpected client action")
            response = message.get("response", {})
            if (
                message.get("type") != "control_response"
                or not isinstance(response, dict)
                or response.get("request_id") != request_id
            ):
                continue
            if response.get("subtype") == "error":
                raise ValueError(str(response.get("error", "Claude discovery failed")))
            result = response.get("response", {})
            models = result.get("models") if isinstance(result, dict) else None
            if not isinstance(models, list):
                raise ValueError(
                    "Claude CLI did not return a model catalog; update the CLI or configure models"
                )
            return models


def metadata_arguments(settings, config, workspace, artifact, native, environment):
    if settings.adapter == "codex":
        overrides, secrets = codex_connection_overrides(settings, native, environment)
        overrides.extend(
            [
                "mcp_servers={}",
                "notify=[]",
                "features.hooks=false",
                "features.codex_hooks=false",
                "features.plugins=false",
                "features.apps=false",
                "features.multi_agent=false",
            ]
        )
        args = [settings.executable, "app-server", "--listen", "stdio://"]
        args.extend(argument for override in overrides for argument in ("-c", override))
        return args, secrets
    schema = artifact / "schema.json"
    schema.write_text("{}")
    provider = create_provider(
        Paths(artifact / "runtime"), config.model_copy(update={"provider": settings})
    )
    args = provider.arguments(workspace, artifact, schema, native)
    for flag in ("--model", "--json-schema", "--allowedTools"):
        index = args.index(flag)
        del args[index : index + 2]
    args[args.index("--tools") + 1] = ""
    args.extend(["--input-format", "stream-json"])
    return args, []


async def native_models(config, settings):
    secrets = []
    client = None
    try:
        with tempfile.TemporaryDirectory(prefix="maxwell-models-") as directory:
            root = Path(directory).resolve()
            workspace, artifact = root / "workspace", root / "artifacts"
            workspace.mkdir()
            artifact.mkdir()
            environment, native, secrets = runtime_environment(Paths(root / "runtime"), settings)
            args, extra = metadata_arguments(
                settings, config, workspace, artifact, native, environment
            )
            secrets.extend(extra)
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=workspace,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=2 * 1024 * 1024,
            )
            client = MetadataClient(process)
            capture = asyncio.create_task(client.capture_stderr())
            try:
                call = (
                    client.codex_models() if settings.adapter == "codex" else client.claude_models()
                )
                rows = await asyncio.wait_for(call, min(30, config.limits.scan_timeout_seconds))
                return redact_metadata(rows, config, secrets)
            finally:
                await terminate(process)
                if not capture.done():
                    capture.cancel()
                await asyncio.gather(capture, return_exceptions=True)
    except (ValueError, OSError, TimeoutError) as exc:
        detail = client.stderr.decode(errors="replace").strip() if client else ""
        message = "Model discovery timed out" if isinstance(exc, TimeoutError) else str(exc)
        if detail:
            message += f": {detail}"
        raise ValueError(redact(message, config, secrets)[:4000]) from None


def redact_metadata(value, config, secrets):
    if isinstance(value, str):
        return redact(value, config, secrets)
    if isinstance(value, list):
        return [redact_metadata(item, config, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_metadata(item, config, secrets) for key, item in value.items()}
    return value


def model_row(row, adapter):
    if not isinstance(row, dict):
        raise ValueError("Invalid model catalog entry")
    model = row.get("model", row.get("id")) if adapter == "codex" else row.get("value")
    if (
        not isinstance(model, str)
        or not model
        or model.startswith("-")
        or any(character.isspace() or not character.isprintable() for character in model)
    ):
        raise ValueError("Invalid model identifier in discovery response")
    efforts = (
        row.get("supportedReasoningEfforts")
        if adapter == "codex"
        else row.get("supportedEffortLevels")
    )
    if efforts is not None:
        if not isinstance(efforts, list):
            raise ValueError("Invalid effort metadata in discovery response")
        efforts = [
            entry.get("reasoningEffort") if isinstance(entry, dict) else entry for entry in efforts
        ]
        if not all(
            isinstance(effort, str)
            and effort.isascii()
            and effort.replace("_", "").replace("-", "").isalnum()
            for effort in efforts
        ):
            raise ValueError("Invalid effort metadata in discovery response")
        efforts = list(dict.fromkeys(efforts))
    elif row.get("supportsEffort") is False:
        efforts = []
    default = row.get("defaultReasoningEffort") if adapter == "codex" else row.get("defaultEffort")
    try:
        ModelSettings.model_validate({"effort": default, "efforts": efforts})
    except ValueError:
        raise ValueError("Invalid effort metadata in discovery response") from None
    display = row.get("displayName", model)
    display = (
        "".join(character for character in display if character.isprintable())
        if isinstance(display, str)
        else model
    )
    return {
        "model": model,
        "name": display,
        "efforts": efforts,
        "default_effort": default,
        "resolved_model": row.get("resolvedModel"),
        "source": "native",
        "access": "unverified",
    }


async def discover_models(config, provider=None, on_provider=None):
    names = (
        [config.provider_name(provider)] if provider is not None else list(config.all_providers())
    )
    results = []
    for name in names:
        settings = config.all_providers()[name]
        result = {"provider": name, "adapter": settings.adapter, "models": []}
        models = {}
        try:
            rows = await native_models(config, settings)
            for row in rows:
                entry = model_row(row, settings.adapter)
                models[entry["model"]] = entry
        except ValueError as exc:
            result["error"] = config.redact(str(exc))
        if settings.home is not None:
            try:
                native, _ = native_settings(settings)
            except ValueError:
                native = {}
            default_model = native.get("model")
            if settings.adapter == "claude" and not default_model:
                default_model = (
                    native.get("env", {}).get("ANTHROPIC_MODEL")
                    if isinstance(native.get("env", {}), dict)
                    else None
                )
            if isinstance(default_model, str) and default_model and default_model not in models:
                try:
                    entry = model_row({"model": default_model}, "codex")
                except ValueError as exc:
                    result.setdefault("error", str(exc))
                else:
                    entry["source"] = "native settings"
                    models[default_model] = entry
        configured = dict(settings.models)
        if settings.model and settings.model not in configured:
            configured[settings.model] = ModelSettings.model_validate(
                settings.model_dump(include=set(ModelSettings.model_fields))
            )
        for entry in models.values():
            if settings.effort is not None:
                entry["default_effort"] = settings.effort
                entry["source"] = "native + config"
            if settings.efforts is not None:
                entry["efforts"] = settings.efforts
                entry["source"] = "native + config"
        for model, options in configured.items():
            entry = models.setdefault(
                model,
                {
                    "model": model,
                    "name": model,
                    "efforts": None,
                    "default_effort": None,
                    "resolved_model": None,
                    "source": "config",
                    "access": "unverified",
                },
            )
            if options.efforts is not None:
                entry["efforts"] = options.efforts
                if entry["source"] in ("native", "native settings"):
                    entry["source"] += " + config"
            if options.effort is not None:
                entry["default_effort"] = options.effort
                if entry["source"] in ("native", "native settings"):
                    entry["source"] += " + config"
        for model, entry in sorted(models.items()):
            entry["selectable"] = not settings.models or model in settings.models
            entry["configured"] = model in configured
            result["models"].append(entry)
        results.append(result)
        if on_provider:
            on_provider(result)
    return {"providers": results}
