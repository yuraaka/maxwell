import argparse
import json
import sqlite3

from argcomplete import shellcode
from argcomplete.completers import DirectoriesCompleter, SuppressCompleter

from maxwell.config import Paths


def read_rows(paths, sql):
    if not paths.db.exists():
        return []
    try:
        connection = sqlite3.connect(paths.db.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return []


def context(parsed_args):
    paths = Paths(getattr(parsed_args, "home", None))
    try:
        config = paths.load()
    except (ValueError, OSError):
        config = None
    catalogs = {}
    for (data,) in read_rows(paths, "SELECT data FROM model_catalogs"):
        try:
            catalog = json.loads(data)
            entries = catalog["models"]
            if not isinstance(entries, list):
                continue
            catalogs[catalog["provider"]] = [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("model"), str)
                and (
                    entry.get("efforts") is None
                    or (
                        isinstance(entry["efforts"], list)
                        and all(isinstance(value, str) for value in entry["efforts"])
                    )
                )
                and (
                    entry.get("default_effort") is None or isinstance(entry["default_effort"], str)
                )
            ]
        except (ValueError, KeyError, TypeError):
            continue
    return config, catalogs


def providers(prefix, parsed_args, **kwargs):
    config, _ = context(parsed_args)
    return sorted(config.all_providers()) if config else []


def selected_providers(config, parsed_args):
    if config is None:
        return {}
    configured = config.all_providers()
    selected = getattr(parsed_args, "provider", None)
    if selected is None:
        return configured
    return {selected: configured[selected]} if selected in configured else {}


def model_names(settings, catalog):
    if settings.models:
        return set(settings.models)
    names = {entry["model"] for entry in catalog if entry.get("access") != "unavailable"}
    if settings.model:
        names.add(settings.model)
    return names


def models(prefix, parsed_args, **kwargs):
    config, catalogs = context(parsed_args)
    return sorted(
        {
            model
            for name, settings in selected_providers(config, parsed_args).items()
            for model in model_names(settings, catalogs.get(name, []))
        }
    )


def efforts(prefix, parsed_args, **kwargs):
    config, catalogs = context(parsed_args)
    choices = {"default"}
    selected = getattr(parsed_args, "model", None)
    for name, settings in selected_providers(config, parsed_args).items():
        catalog = catalogs.get(name, [])
        names = {selected} if selected else model_names(settings, catalog)
        if not names and settings.efforts:
            choices.update(settings.efforts)
        for model in names:
            try:
                configured = settings.for_model(model)
            except ValueError:
                continue
            if configured.efforts is not None:
                choices.update(configured.efforts)
                continue
            metadata = next((entry for entry in catalog if entry["model"] == model), {})
            choices.update(metadata.get("efforts") or [])
            if configured.effort or metadata.get("default_effort"):
                choices.add(configured.effort or metadata["default_effort"])
    return sorted(choices)


def repositories(prefix, parsed_args, **kwargs):
    return [
        row[0]
        for row in read_rows(
            Paths(getattr(parsed_args, "home", None)), "SELECT name FROM repositories ORDER BY name"
        )
    ]


def accounts(prefix, parsed_args, **kwargs):
    config, _ = context(parsed_args)
    return ["default", *sorted(config.github.accounts)] if config else []


def configure_completion(parser):
    completers = {
        "provider": providers,
        "model": models,
        "effort": efforts,
        "repository": repositories,
        "account": accounts,
        "home": DirectoriesCompleter(),
        "directory": DirectoriesCompleter(),
        "issue": SuppressCompleter(),
        "limit": SuppressCompleter(),
    }
    for action in parser._actions:
        if action.dest in completers:
            action.completer = completers[action.dest]
        if isinstance(action, argparse._SubParsersAction):
            for command in action.choices.values():
                configure_completion(command)


def bash_completion():
    return shellcode(["maxwell"], shell="bash")
