import asyncio
import hashlib

from maxwell.config_validation import validate_config
from maxwell.daemon import request
from maxwell.store import Store


def config_digest(paths):
    return hashlib.sha256(paths.config.read_bytes()).hexdigest()


def apply_config(paths, on_check=None, *, force=False, skip_model_checks=False):
    if force and skip_model_checks:
        raise ValueError("--force and --skip-model-checks cannot be used together")
    try:
        digest = config_digest(paths)
    except OSError:
        digest = None
    cached_catalogs = {}
    if not force:
        try:
            cached = Store.cached_models(paths.db)
        except ValueError:
            pass
        else:
            cached_catalogs = {catalog["provider"]: catalog for catalog in cached["providers"]}
    report = validate_config(
        paths,
        on_check=on_check,
        discover=True,
        check_models=not skip_model_checks,
        cached_catalogs=cached_catalogs,
    )
    report["applied"] = False
    if "providers" not in report:
        return report

    def add(name, status, message):
        check = {"name": name, "status": status, "message": message}
        report["checks"].append(check)
        if on_check:
            on_check(dict(check))
        if status == "error":
            report["valid"] = False

    if skip_model_checks and not report["valid"]:
        return report

    try:
        unchanged = config_digest(paths) == digest
    except OSError:
        unchanged = False
    if not unchanged:
        add(
            "configuration",
            "error",
            "Configuration changed during apply; run `maxwell config apply` again",
        )
        return report
    paths.initialize()
    store = Store(paths.db)
    try:
        report["checked_at"] = store.apply_models(report, digest, reused_providers=cached_catalogs)
    finally:
        store.close()
    report["applied"] = True
    add(
        "database",
        "ok",
        "Configuration and model metadata saved",
    )
    if report["valid"] and paths.socket.exists():
        try:
            asyncio.run(request(paths, "reload_config", config_digest=digest))
        except (OSError, ValueError, TimeoutError):
            add(
                "daemon",
                "error",
                "Could not reload the daemon; restart it to use the applied configuration",
            )
        else:
            add("daemon", "ok", "Running daemon reloaded configuration")
    elif paths.socket.exists():
        add("daemon", "skipped", "Daemon configuration unchanged because apply has errors")
    return report
