"""Standalone CLI for immutable deterministic-v2 composition seed preparation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from leanfaith.transforms.composition_seed import (
    CompositionSeedError,
    prepare_deterministic_v2_composition_seeds,
)

app = typer.Typer(
    add_completion=False,
    help="Prepare clean certificate-backed E2 positives for a deterministic second hop.",
)


@app.callback()
def root_command() -> None:
    """Immutable deterministic-v2 composition seed operations."""


@app.command("prepare")
def prepare_command(
    combination_dir: Annotated[
        Path,
        typer.Option(
            "--combination-dir",
            help="Completed immutable provisional-pair combination directory.",
        ),
    ],
    materialization_roots: Annotated[
        list[Path],
        typer.Option(
            "--materialization-root",
            "-r",
            help="Bound completed materialization root; repeat for every combined root.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable seed-output directory."),
    ],
) -> None:
    """Admit only original-source, certificate-backed P14-P18 E2 positives."""

    try:
        artifacts = prepare_deterministic_v2_composition_seeds(
            combination_dir=combination_dir,
            materialization_roots=materialization_roots,
            output_dir=output_dir,
        )
    except (OSError, CompositionSeedError) as exc:
        typer.echo(
            json.dumps(
                {
                    "operation": "prepare_deterministic_v2_composition_seeds",
                    "status": "error",
                    "message": str(exc),
                    "semantic_labels_created": False,
                    "training_eligible": False,
                },
                sort_keys=True,
            ),
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "operation": "prepare_deterministic_v2_composition_seeds",
                "status": "replayed" if artifacts.replayed else "prepared",
                "manifest_path": str(artifacts.manifest_path),
                "seed_set_id": artifacts.seed_set_id,
                "seed_count": artifacts.seed_count,
                "semantic_labels_created": False,
                "training_eligible": False,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    """Run the standalone Typer application."""

    app()


if __name__ == "__main__":
    main()
