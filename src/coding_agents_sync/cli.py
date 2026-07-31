from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Annotated, Never

import typer

from .io import ManagedEntryConflict
from .patches import PatchError
from .runtime_config import NativeConfigError
from .sources import SourceSchemaError
from .sync import run_sync

CHECK_COMMANDS = (
    ("uv", "run", "--locked", "ruff", "format", "--check", "."),
    ("uv", "run", "--locked", "ruff", "check", "."),
    ("uv", "run", "--locked", "basedpyright"),
    ("uv", "run", "--locked", "pytest"),
)


def default_config_root() -> Path:
    if xdg_config_home := os.environ.get("XDG_CONFIG_HOME"):
        return Path(xdg_config_home).expanduser() / "coding-agents"
    return Path.home() / ".config" / "coding-agents"


def _fail(message: str, *, code: int = 1) -> Never:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


main = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Sync coding-agent canonical files into tool targets.",
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@main.command()
def sync(
    config_root: Annotated[
        Path | None,
        typer.Option(
            "--config-root",
            file_okay=False,
            dir_okay=True,
            help=(
                "Configuration source root. Defaults to "
                "$XDG_CONFIG_HOME/coding-agents or ~/.config/coding-agents."
            ),
        ),
    ] = None,
    legacy_config_root: Annotated[
        Path | None,
        typer.Option(
            "--base",
            file_okay=False,
            dir_okay=True,
            hidden=True,
        ),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            file_okay=False,
            dir_okay=True,
            help="Home directory for generated targets.",
        ),
    ] = None,
    check_mode: Annotated[
        bool,
        typer.Option("--check", help="Report managed drift without writing."),
    ] = False,
) -> None:
    if config_root is not None and legacy_config_root is not None:
        _fail("--config-root and --base are mutually exclusive", code=2)
    resolved_config_root = config_root or legacy_config_root or default_config_root()
    try:
        drift = run_sync(
            config_root=resolved_config_root,
            home=home or Path.home(),
            check=check_mode,
        )
    except (
        ManagedEntryConflict,
        NativeConfigError,
        PatchError,
        SourceSchemaError,
    ) as error:
        _fail(str(error))
    if drift:
        for path in drift:
            typer.echo(f"drift {path}", err=True)
        raise typer.Exit(code=1)


check = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Run the standard coding-agents-sync validation suite.",
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@check.command()
def run_checks(
    project_root: Annotated[
        Path | None,
        typer.Option(
            "--project-root",
            file_okay=False,
            dir_okay=True,
            help="Project root to validate.",
        ),
    ] = None,
) -> None:
    for command in CHECK_COMMANDS:
        try:
            subprocess.run(command, cwd=project_root or Path.cwd(), check=True)
        except subprocess.CalledProcessError as exc:
            rendered = shlex.join(command)
            _fail(f"{rendered} failed with exit code {exc.returncode}")


if __name__ == "__main__":
    main()
