"""Standalone CLI for audit-only deterministic provisional-pair combination."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from leanfaith.transforms.provisional_pair_combine import (
    ProvisionalPairCombineError,
    combine_provisional_pair_roots,
)

app = typer.Typer(
    add_completion=False,
    help="Audit and deduplicate completed deterministic-v2 provisional pair roots.",
)


@app.callback()
def root_command() -> None:
    """Audit-only deterministic provisional-pair operations."""


@app.command("combine")
def combine_command(
    materialization_roots: Annotated[
        list[Path],
        typer.Option(
            "--materialization-root",
            "-r",
            help="Completed deterministic-v2 root; repeat for every input root.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable audit-output directory."),
    ],
) -> None:
    """Combine completed roots without creating labels or training eligibility."""

    try:
        artifacts = combine_provisional_pair_roots(
            materialization_roots=materialization_roots,
            output_dir=output_dir,
        )
    except (OSError, ProvisionalPairCombineError) as exc:
        typer.echo(
            json.dumps(
                {
                    "operation": "combine_deterministic_provisional_pairs",
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
                "operation": "combine_deterministic_provisional_pairs",
                "status": "replayed" if artifacts.replayed else "combined",
                "manifest_path": str(artifacts.manifest_path),
                "combination_hash": artifacts.combination_hash,
                "gross_observation_count": artifacts.gross_count,
                "unique_pair_count": artifacts.unique_count,
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
