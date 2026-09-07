"""The command line.

The important one here is `test_every_command_builds`. Typer constructs each
command's parameters at registration time, and a mistake there is invisible
until the binary is run — no import fails, no test that only calls library code
notices. One command that fails to build takes down the whole CLI, including
`--help`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from emalia.cli import app

runner = CliRunner()

COMMANDS = ["version", "init", "check", "run", "once", "send", "inbox", "audit"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own credentials out of the CLI tests."""
    for key in list(os.environ):
        if key.startswith("EMALIA_"):
            monkeypatch.delenv(key, raising=False)


class TestCommandRegistration:
    def test_top_level_help_works(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in COMMANDS:
            assert command in result.output

    @pytest.mark.parametrize("command", COMMANDS)
    def test_every_command_builds(self, command: str) -> None:
        # Typer builds a command's parameters when it is registered. Sharing one
        # `typer.Option` instance across commands, or giving it a default that
        # the parameter also declares, fails here and nowhere else.
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output

    def test_version_prints_the_package_version(self) -> None:
        from emalia import __version__

        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output


class TestInit:
    def test_writes_both_starter_files(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "emalia.toml").exists()
        assert (tmp_path / ".env").exists()

    def test_does_not_clobber_existing_files(self, tmp_path: Path) -> None:
        (tmp_path / "emalia.toml").write_text("mine", encoding="utf-8")
        runner.invoke(app, ["init", str(tmp_path)])
        assert (tmp_path / "emalia.toml").read_text(encoding="utf-8") == "mine"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        (tmp_path / "emalia.toml").write_text("mine", encoding="utf-8")
        runner.invoke(app, ["init", str(tmp_path), "--force"])
        assert "instance_name" in (tmp_path / "emalia.toml").read_text(encoding="utf-8")

    def test_the_starter_config_is_loadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A starter file that does not parse would be a poor first impression.
        from emalia.config import EmaliaConfig

        runner.invoke(app, ["init", str(tmp_path)])
        monkeypatch.setenv("EMALIA_ADDRESS", "bot@example.com")
        monkeypatch.setenv("EMALIA_PASSWORD", "pw")
        monkeypatch.setenv("EMALIA_PROVIDER", "gmail")
        config = EmaliaConfig.load(tmp_path / "emalia.toml")
        assert config.policy.allowed_senders == ["you@example.com"]


class TestMissingCredentials:
    @pytest.mark.parametrize("command", ["check", "inbox", "audit"])
    def test_commands_needing_an_account_fail_cleanly(self, command: str) -> None:
        # No traceback, a message naming what to set, and a non-zero exit.
        result = runner.invoke(app, [command])
        assert result.exit_code != 0
        assert "EMALIA_ADDRESS" in result.output
        assert "Traceback" not in result.output
