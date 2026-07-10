"""LeanFaith command-line entry point."""

from __future__ import annotations

import typer

from leanfaith import __version__

app = typer.Typer(
    name="leanfaith",
    help="LeanFaith: a calibrated faithfulness metric for Lean 4 autoformalization.",
    no_args_is_help=True,
)


@app.callback()
def root() -> None:
    """LeanFaith pipeline commands; see PLAN.md §7.2 for the phase-to-command map."""


@app.command()
def version() -> None:
    """Print the installed LeanFaith version."""
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
