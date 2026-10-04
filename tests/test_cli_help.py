import sys

import pytest

from maxwell.cli import execute, parser
from maxwell.config import Paths


def test_help_shows_nested_command_tree_and_global_options():
    help_text = parser().format_help()
    assert "├── config" in help_text
    assert "│   ├── init" in help_text and "│   └── apply" in help_text
    assert "├── models" in help_text and "│   └── list" in help_text
    assert "--home HOME" in help_text and "--json" in help_text
    assert "\033[" not in help_text


def test_tree_help_uses_terminal_colors_and_respects_no_color(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert "\033[1;36m" in parser().format_help()
    monkeypatch.setenv("NO_COLOR", "")
    assert "\033[" not in parser().format_help()


@pytest.mark.parametrize(
    "arguments", [["init"], ["config", "check"], ["config", "apply", "--offline"]]
)
def test_old_config_commands_are_removed(arguments):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(arguments)
    assert error.value.code == 2


def test_config_init_creates_bootstrap_files_and_refuses_overwrite(directory, capsys):
    paths = Paths(directory / "new-home")
    args = parser().parse_args(["--home", str(paths.root), "config", "init"])
    execute(args)
    assert "maxwell config apply" in capsys.readouterr().out
    assert paths.config.is_file() and paths.db.is_file()
    assert paths.config.stat().st_mode & 0o777 == 0o600
    before = paths.config.read_bytes()
    with pytest.raises(ValueError, match="not overwritten"):
        execute(args)
    assert paths.config.read_bytes() == before
