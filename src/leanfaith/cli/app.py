"""LeanFaith command-line entry point."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Annotated

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


@app.command()
def doctor(
    root_dir: Annotated[
        Path | None,
        typer.Option(
            "--root",
            help="Repository root override (defaults to discovery from the cwd).",
        ),
    ] = None,
    write_lock_flag: Annotated[
        bool,
        typer.Option(
            "--write-lock",
            help="Write configs/environment.lock.yaml from pinned constants (Phase 0 task 8).",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite a divergent existing lock (ADR-0001 change)."),
    ] = False,
) -> None:
    """Check Python/LeanInteract/toolchain environment against the lock (LF-007)."""
    from leanfaith.cli.doctor import doctor_report_path, run_doctor, write_lock
    from leanfaith.config.paths import RepoPaths
    from leanfaith.schemas import (
        ArtifactClass,
        RunManifest,
        collect_code_state,
        new_run_id,
        run_manifest_path,
        write_manifest,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    lock_refused = False
    if write_lock_flag:
        changed, message = write_lock(paths, force=force)
        typer.echo(message)
        lock_refused = not changed and "--force" in message
    report = run_doctor(paths)
    report_hash = write_manifest(report, doctor_report_path(paths))

    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith doctor" + (" --write-lock" if write_lock_flag else ""),
        argv=("leanfaith", "doctor", *(["--write-lock"] if write_lock_flag else [])),
        code=collect_code_state(paths.root),
        output_hashes={str(doctor_report_path(paths).relative_to(paths.root)): report_hash},
        status_counts={
            "checks_passed": sum(1 for c in report.checks if c.passed),
            "checks_failed": sum(1 for c in report.checks if not c.passed),
            "warnings": len(report.warnings),
        },
        created_at=created_at,
    )
    write_manifest(manifest, run_manifest_path(paths, run_id))

    for check in report.checks:
        marker = "ok " if check.passed else "FAIL"
        typer.echo(f"[{marker}] {check.name}: {check.detail}")
    for warning in report.warnings:
        typer.echo(f"[warn] {warning}")
    if not report.ok or lock_refused:
        raise typer.Exit(code=1)


@app.command("probe-api")
def probe_api(
    root_dir: Annotated[
        Path | None,
        typer.Option(
            "--root",
            help="Repository root override (defaults to discovery from the cwd).",
        ),
    ] = None,
) -> None:
    """Verify the pinned LeanInteract API shape and write the A.8 artifacts (LF-006)."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.lean.api_probe import probe_report_paths, run_api_probe
    from leanfaith.schemas import (
        ArtifactClass,
        RunManifest,
        collect_code_state,
        new_run_id,
        run_manifest_path,
        write_manifest,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    report = run_api_probe()
    report_path, artifact_path = probe_report_paths(paths)
    report_hash = write_manifest(report, report_path)
    artifact_hash = write_manifest(report, artifact_path)

    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith probe-api",
        argv=("leanfaith", "probe-api"),
        code=collect_code_state(paths.root),
        environment={
            "lean_interact_version": report.installed_version,
            "leanfaith_version": __version__,
        },
        output_hashes={
            str(report_path.relative_to(paths.root)): report_hash,
            str(artifact_path.relative_to(paths.root)): artifact_hash,
        },
        status_counts={
            "symbols_present": sum(1 for s in report.symbols if s.present),
            "symbols_missing": sum(1 for s in report.symbols if not s.present),
            "caveats_passed": sum(1 for c in report.caveats if c.passed),
            "caveats_failed": sum(1 for c in report.caveats if not c.passed),
        },
        created_at=created_at,
    )
    write_manifest(manifest, run_manifest_path(paths, run_id))

    if not report.ok:
        typer.echo("LeanInteract API probe FAILED; see reports/compatibility/", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"LeanInteract {report.installed_version} API probe OK "
        f"({len(report.symbols)} symbols, {len(report.caveats)} caveats)"
    )


@app.command()
def probe(
    source: Annotated[
        str, typer.Argument(help="Source key from configs/sources/ (or 'all' for HF sources).")
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run configured live source probes and archive manifests/samples (LF-011)."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.schemas import (
        ArtifactClass,
        RunManifest,
        collect_code_state,
        new_run_id,
        run_manifest_path,
        write_manifest,
    )
    from leanfaith.sources import HFDatasetProber, archive_probe
    from leanfaith.sources.probe import RealHFClient, hf_probe_config_from_yaml

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    hf_sources = ("sft_classic", "sft_classic_numina", "lean_workbook", "proofnetverif")
    targets = hf_sources if source == "all" else (source,)
    client = RealHFClient()
    status_counts: dict[str, int] = {"accessible": 0, "blocked": 0}
    output_hashes: dict[str, str] = {}
    failed = False
    for name in targets:
        config = hf_probe_config_from_yaml(paths, name)
        outcome = archive_probe(HFDatasetProber(config, client).probe(), paths)
        if outcome.accessible:
            status_counts["accessible"] += 1
            assert outcome.sample_hash is not None
            output_hashes[f"data/source_manifests/{name}.json"] = outcome.sample_hash
            typer.echo(f"[ok     ] {name}: sample_rows={outcome.sample_row_count}")
        else:
            status_counts["blocked"] += 1
            failed = True
            typer.echo(f"[BLOCKED] {name}: {outcome.blocked_reason}", err=True)

    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.PRODUCTION,
        command=f"leanfaith probe {source}",
        argv=("leanfaith", "probe", source),
        code=collect_code_state(paths.root),
        output_hashes=output_hashes,
        status_counts=status_counts,
        created_at=created_at,
    )
    write_manifest(manifest, run_manifest_path(paths, run_id))
    if failed:
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
