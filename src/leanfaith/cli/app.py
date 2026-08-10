"""LeanFaith command-line entry point."""

from __future__ import annotations

import datetime
import json
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


@app.command("extract")
def extract_command(
    source: Annotated[str, typer.Argument(help="MVP source: mathlib or sft_classic")],
    input_path: Annotated[Path | None, typer.Option("--input")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    split: Annotated[str, typer.Option("--split")] = "train",
    row_offset: Annotated[int, typer.Option("--row-offset", min=0)] = 0,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 1,
    chunk_size: Annotated[int, typer.Option("--chunk-size", min=1)] = 500,
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option(
            "--memory-hard-limit-mb",
            min=256,
            help="Per-Lean-REPL Linux memory limit; recorded in the extraction manifest.",
        ),
    ] = None,
    code_bundle_path: Annotated[Path | None, typer.Option("--code-bundle")] = None,
    resume_work_dir: Annotated[Path | None, typer.Option("--resume-work-dir")] = None,
    mathlib_file_frame_path: Annotated[
        Path | None,
        typer.Option(
            "--mathlib-file-frame",
            help="Replay-verified public mathlib file-frame JSON; incompatible with --limit.",
        ),
    ] = None,
    mathlib_frame_selection_seed: Annotated[
        str | None,
        typer.Option(
            "--mathlib-frame-selection-seed",
            help="Exact non-secret seed used to freeze --mathlib-file-frame.",
        ),
    ] = None,
    mathlib_previous_file_frame_path: Annotated[
        Path | None,
        typer.Option(
            "--mathlib-previous-file-frame",
            help=(
                "Replay-verified smaller cumulative frame. When set, extract only "
                "the exact additive members in --mathlib-file-frame."
            ),
        ),
    ] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Extract theorem statements with exact provenance and terminal accounting."""
    from leanfaith.cli.pipeline import default_mathlib_checkout, run_extract
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    manifest, stats = run_extract(
        paths=paths,
        source=source,
        project_dir=project_dir or default_mathlib_checkout(),
        input_path=input_path,
        out_dir=out_dir or paths.data / "extracted",
        limit=limit,
        split=split,
        row_offset=row_offset,
        workers=workers,
        chunk_size=chunk_size,
        memory_hard_limit_mb=memory_hard_limit_mb,
        code_bundle_path=code_bundle_path,
        resume_work_dir=resume_work_dir,
        mathlib_file_frame_path=(
            mathlib_file_frame_path
            if mathlib_file_frame_path is None or mathlib_file_frame_path.is_absolute()
            else paths.root / mathlib_file_frame_path
        ),
        mathlib_frame_selection_seed=mathlib_frame_selection_seed,
        mathlib_previous_file_frame_path=(
            mathlib_previous_file_frame_path
            if mathlib_previous_file_frame_path is None
            or mathlib_previous_file_frame_path.is_absolute()
            else paths.root / mathlib_previous_file_frame_path
        ),
    )
    typer.echo(f"manifest={manifest}")
    typer.echo(json.dumps(stats, sort_keys=True))


@app.command("freeze-mathlib-file-frame")
def freeze_mathlib_file_frame_command(
    target_file_count: Annotated[int, typer.Option("--target-file-count", min=1)],
    selection_seed: Annotated[
        str,
        typer.Option("--selection-seed", help="Stable non-secret selection seed."),
    ],
    output_path: Annotated[Path, typer.Option("--output")],
    excluded_domains: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-domain",
            help=(
                "Repeatable explicit domain exclusion. Defaults to the six "
                "non-mathematical infrastructure roots used by LF-022."
            ),
        ),
    ] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir")] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Freeze a deterministic progressive public mathlib extraction frame."""
    from leanfaith.cli.pipeline import (
        default_mathlib_checkout,
        run_freeze_mathlib_file_frame,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    exclusions = tuple(
        sorted(
            excluded_domains
            if excluded_domains is not None
            else ("Deprecated", "Init", "Lean", "Tactic", "Testing", "Util")
        )
    )
    try:
        path, digest, summary = run_freeze_mathlib_file_frame(
            paths=paths,
            project_dir=project_dir or default_mathlib_checkout(),
            target_file_count=target_file_count,
            selection_seed=selection_seed,
            excluded_domains=exclusions,
            output_path=output_path if output_path.is_absolute() else paths.root / output_path,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Mathlib file-frame freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"frame={path} sha256={digest}")
    typer.echo(json.dumps(summary, sort_keys=True))


@app.command("freeze-code-bundle")
def freeze_code_bundle_command(
    output_dir: Annotated[Path, typer.Option("--out-dir")],
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Freeze the exact tracked and untracked code tree for a gate run."""
    from leanfaith.config.code_bundle import freeze_code_bundle
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    path, digest, state = freeze_code_bundle(paths.root, output_dir)
    typer.echo(
        json.dumps(
            {
                "path": str(path),
                "sha256": digest,
                "code_tree_hash": state.code_tree_hash,
                "git_revision": state.git_revision,
                "git_dirty": state.git_dirty,
            },
            sort_keys=True,
        )
    )


@app.command("freeze-benchmarks")
def freeze_benchmarks_command(
    proofnet_dir: Annotated[Path | None, typer.Option("--proofnet-dir")] = None,
    formalrx_jsonl: Annotated[Path | None, typer.Option("--formalrx-jsonl")] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Freeze exact external-benchmark IDs and content signatures."""
    from leanfaith.cli.pipeline import run_freeze_benchmarks
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    path, digest = run_freeze_benchmarks(
        paths=paths,
        proofnet_dir=proofnet_dir or paths.data / "parsed" / "sources" / "proofnetverif",
        formalrx_jsonl=formalrx_jsonl,
        frozen_at=datetime.datetime.now(tz=datetime.UTC),
    )
    typer.echo(f"frozen={path} sha256={digest}")


@app.command("append-benchmark-signatures")
def append_benchmark_signatures_command(
    formalrx_jsonl: Annotated[Path, typer.Option("--formalrx-jsonl")],
    code_bundle: Annotated[Path, typer.Option("--code-bundle")],
    identity_registry: Annotated[Path | None, typer.Option("--identity-registry")] = None,
    proofnet_dir: Annotated[Path | None, typer.Option("--proofnet-dir")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir")] = None,
    work_dir: Annotated[Path | None, typer.Option("--work-dir")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir")] = None,
    memory_hard_limit_mb: Annotated[
        int | None, typer.Option("--memory-hard-limit-mb", min=256)
    ] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Append Lean-derived hashes to a new versioned benchmark registry."""
    from leanfaith.cli.pipeline import build_mathlib_context, default_mathlib_checkout
    from leanfaith.config.code_bundle import validate_code_bundle
    from leanfaith.config.hashing import hash_file
    from leanfaith.config.paths import RepoPaths
    from leanfaith.datasets.benchmark_signatures import run_benchmark_signature_freeze
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
    from leanfaith.schemas import collect_code_state

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    project = project_dir or default_mathlib_checkout()
    identity = identity_registry or paths.data / "benchmarks" / "frozen_ids.json"
    proofnet = proofnet_dir or paths.data / "parsed" / "sources" / "proofnetverif"
    work = work_dir or paths.data / "work" / "benchmark_signatures_v1"
    output = out_dir or paths.data / "benchmarks"

    code_state = collect_code_state(paths.root)
    if code_state.code_tree_hash is None:
        raise ValueError("benchmark signature freeze requires a nonempty code_tree_hash")
    code_bundle_sha256 = validate_code_bundle(code_bundle, code_state.code_tree_hash)
    context, context_hash = build_mathlib_context(paths, project)
    environment_lock = paths.configs / "environment.lock.yaml"
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=project,
            context_fingerprint=context.context_fingerprint,
            environment_schema_version=context.environment_schema_version,
            raw_response_dir=work / "raw_responses",
            memory_hard_limit_mb=memory_hard_limit_mb,
        )
    )
    try:
        registry_path, registry_digest, index_path, index_digest, accounting = (
            run_benchmark_signature_freeze(
                backend,
                identity_registry_path=identity,
                proofnet_dir=proofnet,
                formalrx_jsonl=formalrx_jsonl,
                context_id=context.context_id,
                generated_at=datetime.datetime.now(tz=datetime.UTC),
                work_dir=work,
                output_dir=output,
                additional_input_checksums={
                    "code_bundle": code_bundle_sha256,
                    "context_record": context_hash,
                    "environment_lock": hash_file(environment_lock),
                },
            )
        )
    finally:
        backend.close()
    typer.echo(
        json.dumps(
            {
                "accounting": accounting.model_dump(mode="json"),
                "index": str(index_path),
                "index_sha256": index_digest,
                "registry": str(registry_path),
                "registry_sha256": registry_digest,
            },
            sort_keys=True,
        )
    )


@app.command("represent")
def represent_command(
    source: Annotated[str, typer.Argument(help="Source partition key")],
    theorem_jsonl: Annotated[Path, typer.Option("--input")],
    project_dir: Annotated[Path | None, typer.Option("--project-dir")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 1,
    chunk_size: Annotated[int, typer.Option("--chunk-size", min=1)] = 20,
    memory_hard_limit_mb: Annotated[
        int | None, typer.Option("--memory-hard-limit-mb", min=256)
    ] = None,
    code_bundle: Annotated[Path | None, typer.Option("--code-bundle")] = None,
    frozen_manifest: Annotated[Path | None, typer.Option("--frozen-manifest")] = None,
    resume_work_dir: Annotated[Path | None, typer.Option("--resume-work-dir")] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Build per-theorem representations with isolated failures."""
    from leanfaith.cli.pipeline import default_mathlib_checkout, run_represent
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    manifest, counts = run_represent(
        paths=paths,
        source=source,
        theorem_jsonl=theorem_jsonl,
        project_dir=project_dir or default_mathlib_checkout(),
        out_dir=out_dir or paths.data / "representations",
        limit=limit,
        workers=workers,
        chunk_size=chunk_size,
        memory_hard_limit_mb=memory_hard_limit_mb,
        code_bundle_path=code_bundle,
        frozen_manifest_path=frozen_manifest,
        resume_work_dir=resume_work_dir,
    )
    typer.echo(f"manifest={manifest}")
    typer.echo(json.dumps(counts, sort_keys=True))


@app.command("audit-extraction-regression")
def audit_extraction_regression_command(
    input_path: Annotated[Path, typer.Option("--input")],
    theorem_path: Annotated[Path, typer.Option("--theorems")],
    failure_path: Annotated[Path, typer.Option("--failures")],
    expected_path: Annotated[Path, typer.Option("--expected")],
) -> None:
    """Compare a Gate-2 extraction run to an immutable per-row expectation."""
    from leanfaith.lean.extraction_regression import validate_sft_classic_regression

    report = validate_sft_classic_regression(
        input_path=input_path,
        theorem_path=theorem_path,
        failure_path=failure_path,
        expected_path=expected_path,
    )
    typer.echo(
        json.dumps(
            {
                "ok": report.ok,
                "expected_rows": report.expected_rows,
                "observed_rows": report.observed_rows,
                "errors": report.errors,
            },
            sort_keys=True,
        )
    )
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("audit-extraction-replay")
def audit_extraction_replay_command(
    left_theorems: Annotated[Path, typer.Option("--left-theorems")],
    left_failures: Annotated[Path, typer.Option("--left-failures")],
    right_theorems: Annotated[Path, typer.Option("--right-theorems")],
    right_failures: Annotated[Path, typer.Option("--right-failures")],
) -> None:
    """Compare two Gate-2 runs for exact normalized terminal-outcome replay."""
    from leanfaith.lean.extraction_regression import compare_extraction_replays

    report = compare_extraction_replays(
        left_theorem_path=left_theorems,
        left_failure_path=left_failures,
        right_theorem_path=right_theorems,
        right_failure_path=right_failures,
    )
    typer.echo(
        json.dumps(
            {
                "ok": report.ok,
                "left_theorems": report.left_theorems,
                "right_theorems": report.right_theorems,
                "left_failures": report.left_failures,
                "right_failures": report.right_failures,
                "errors": report.errors,
            },
            sort_keys=True,
        )
    )
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("audit-gate2-scale")
def audit_gate2_scale_command(
    sample_path: Annotated[Path, typer.Option("--sample")],
    sample_manifest_path: Annotated[Path, typer.Option("--sample-manifest")],
    extraction_manifest_path: Annotated[Path, typer.Option("--extraction-manifest")],
    theorem_path: Annotated[Path, typer.Option("--theorems")],
    failure_path: Annotated[Path, typer.Option("--failures")],
    output_path: Annotated[Path | None, typer.Option("--out")] = None,
) -> None:
    """Audit exact Gate-2 denominator, provenance, accounting, and checksums."""
    from dataclasses import asdict

    from leanfaith.lean.extraction_regression import audit_gate2_scale

    report = audit_gate2_scale(
        sample_path=sample_path,
        sample_manifest_path=sample_manifest_path,
        extraction_manifest_path=extraction_manifest_path,
        theorem_path=theorem_path,
        failure_path=failure_path,
    )
    payload = asdict(report) | {"ok": report.ok}
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    typer.echo(json.dumps(payload, sort_keys=True))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("freeze-gate3-inputs")
def freeze_gate3_inputs_command(
    mathlib_jsonl: Annotated[Path, typer.Option("--mathlib")],
    sft_classic_jsonl: Annotated[Path, typer.Option("--sft-classic")],
    out_path: Annotated[Path, typer.Option("--out")],
    per_source: Annotated[int, typer.Option("--per-source", min=1)] = 5000,
) -> None:
    """Freeze the equal-source Gate-3 theorem manifest; fail if either stratum is short."""
    from leanfaith.cli.pipeline import run_freeze_gate3_inputs

    path, digest = run_freeze_gate3_inputs(
        mathlib_jsonl=mathlib_jsonl,
        sft_classic_jsonl=sft_classic_jsonl,
        out_path=out_path,
        per_source=per_source,
    )
    typer.echo(f"frozen={path} sha256={digest}")


@app.command("audit-representations")
def audit_representations_command(
    representation_jsonl: Annotated[Path, typer.Option("--representations")],
    theorem_jsonl: Annotated[Path, typer.Option("--theorems")],
    out_path: Annotated[Path, typer.Option("--out")],
    failure_jsonl: Annotated[Path | None, typer.Option("--failures")] = None,
    frozen_manifest: Annotated[Path | None, typer.Option("--frozen-manifest")] = None,
) -> None:
    """Run mechanical Gate-3 coverage, identity, and collision checks."""
    from leanfaith.cli.pipeline import run_audit_representations

    path, report = run_audit_representations(
        representation_jsonl=representation_jsonl,
        theorem_jsonl=theorem_jsonl,
        out_path=out_path,
        failure_jsonl=failure_jsonl,
        frozen_manifest_path=frozen_manifest,
    )
    typer.echo(f"report={path} sha256={report['report_hash']}")
    typer.echo(json.dumps({"mechanical_pass": report["mechanical_pass"]}, sort_keys=True))
    if not report["mechanical_pass"]:
        raise typer.Exit(code=1)


@app.command("audit-representation-replay")
def audit_representation_replay_command(
    left_path: Annotated[Path, typer.Option("--left")],
    right_path: Annotated[Path, typer.Option("--right")],
) -> None:
    """Compare two Gate-3 runs for exact representation identity/content replay."""
    from dataclasses import asdict

    from leanfaith.representations import compare_representation_replays

    report = compare_representation_replays(left_path, right_path)
    typer.echo(json.dumps(asdict(report) | {"ok": report.ok}, sort_keys=True))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("audit-alpha-invariance")
def audit_alpha_invariance_command(
    out_path: Annotated[Path, typer.Option("--out")],
    project_dir: Annotated[Path | None, typer.Option("--project-dir")] = None,
    cases: Annotated[int, typer.Option("--cases", min=1)] = 1000,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 1,
    chunk_size: Annotated[int, typer.Option("--chunk-size", min=1)] = 20,
    memory_hard_limit_mb: Annotated[
        int | None, typer.Option("--memory-hard-limit-mb", min=256)
    ] = None,
    code_bundle: Annotated[Path | None, typer.Option("--code-bundle")] = None,
    resume_work_dir: Annotated[Path | None, typer.Option("--resume-work-dir")] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Run the Gate-3 audit-only alpha-renaming property suite."""
    from leanfaith.cli.pipeline import (
        default_mathlib_checkout,
        run_alpha_invariance_audit,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    path, report = run_alpha_invariance_audit(
        paths=paths,
        project_dir=project_dir or default_mathlib_checkout(),
        out_path=out_path,
        cases=cases,
        workers=workers,
        chunk_size=chunk_size,
        memory_hard_limit_mb=memory_hard_limit_mb,
        code_bundle_path=code_bundle,
        resume_work_dir=resume_work_dir,
    )
    typer.echo(
        f"report={path} passed={report['passed']}/{report['cases']} "
        f"sha256={report['report_sha256']}"
    )
    if not report["mechanical_pass"]:
        raise typer.Exit(code=1)


@app.command("audit-representation-cross-path")
def audit_representation_cross_path_command(
    theorem_jsonl: Annotated[Path, typer.Option("--theorems")],
    representation_jsonl: Annotated[Path, typer.Option("--representations")],
    out_path: Annotated[Path, typer.Option("--out")],
    project_dir: Annotated[Path | None, typer.Option("--project-dir")] = None,
    cases: Annotated[int, typer.Option("--cases", min=1)] = 500,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 1,
    chunk_size: Annotated[int, typer.Option("--chunk-size", min=1)] = 20,
    memory_hard_limit_mb: Annotated[
        int | None, typer.Option("--memory-hard-limit-mb", min=256)
    ] = None,
    code_bundle: Annotated[Path | None, typer.Option("--code-bundle")] = None,
    resume_work_dir: Annotated[Path | None, typer.Option("--resume-work-dir")] = None,
    root_dir: Annotated[Path | None, typer.Option("--root")] = None,
) -> None:
    """Compare frozen mathlib declarations with audit-only exact-type inline aliases."""
    from leanfaith.cli.pipeline import default_mathlib_checkout, run_cross_path_audit
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    path, report = run_cross_path_audit(
        paths=paths,
        project_dir=project_dir or default_mathlib_checkout(),
        theorem_jsonl=theorem_jsonl,
        representation_jsonl=representation_jsonl,
        out_path=out_path,
        cases=cases,
        workers=workers,
        chunk_size=chunk_size,
        memory_hard_limit_mb=memory_hard_limit_mb,
        code_bundle_path=code_bundle,
        resume_work_dir=resume_work_dir,
    )
    typer.echo(
        f"report={path} passed={report['passed']}/{report['cases']} "
        f"sha256={report['report_sha256']}"
    )
    if not report["mechanical_pass"]:
        raise typer.Exit(code=1)


@app.command("close-representation-collision-audit")
def close_representation_collision_audit_command(
    mechanical_report: Annotated[Path, typer.Option("--audit-report")],
    review_jsonl: Annotated[Path, typer.Option("--reviews")],
    out_path: Annotated[Path, typer.Option("--out")],
) -> None:
    """Close the deterministic lossy-collision sample with terminal reviews."""
    from leanfaith.cli.pipeline import run_close_manual_collision_audit

    path, report = run_close_manual_collision_audit(
        mechanical_report_path=mechanical_report,
        review_jsonl=review_jsonl,
        out_path=out_path,
    )
    typer.echo(
        f"report={path} status={report['manual_audit_status']} sha256={report['report_sha256']}"
    )
    if not report["gate_pass"]:
        raise typer.Exit(code=1)


@app.command("sample-gate2")
def sample_gate2_command(
    input_path: Annotated[Path, typer.Option("--input")],
    output_path: Annotated[Path, typer.Option("--out")],
    manifest_path: Annotated[Path, typer.Option("--manifest")],
    sample_size: Annotated[int, typer.Option("--sample-size", min=1)] = 20000,
    split: Annotated[str, typer.Option("--split")] = "train",
) -> None:
    """Create the frozen pre-extraction Gate-2 sample from a full raw JSONL split."""
    from leanfaith.cli.pipeline import SFT_CLASSIC_REVISION
    from leanfaith.sources.gate2_sampling import sample_gate2_jsonl
    from leanfaith.sources.hf_sft_classic import DATASET_ID

    output, manifest = sample_gate2_jsonl(
        input_path=input_path,
        output_path=output_path,
        manifest_path=manifest_path,
        dataset_id=DATASET_ID,
        revision=SFT_CLASSIC_REVISION,
        split=split,
        sample_size=sample_size,
    )
    typer.echo(f"sample={output} manifest={manifest}")


@app.command("sample-gate2-arrow")
def sample_gate2_arrow_command(
    arrow_dir: Annotated[Path, typer.Option("--arrow-dir")],
    output_path: Annotated[Path, typer.Option("--out")],
    manifest_path: Annotated[Path, typer.Option("--manifest")],
    sample_size: Annotated[int, typer.Option("--sample-size", min=1)] = 20000,
    split: Annotated[str, typer.Option("--split")] = "train",
    expected_population_rows: Annotated[
        int | None, typer.Option("--expected-population-rows", min=1)
    ] = None,
) -> None:
    """Create Gate 2's sample directly from pinned Hugging Face Arrow shards."""
    from leanfaith.cli.pipeline import SFT_CLASSIC_REVISION
    from leanfaith.sources.gate2_sampling import sample_gate2_arrow_shards
    from leanfaith.sources.hf_sft_classic import DATASET_ID

    paths = sorted(arrow_dir.glob(f"sft_classic-{split}-*.arrow"))
    output, manifest = sample_gate2_arrow_shards(
        arrow_paths=paths,
        output_path=output_path,
        manifest_path=manifest_path,
        dataset_id=DATASET_ID,
        revision=SFT_CLASSIC_REVISION,
        split=split,
        sample_size=sample_size,
        expected_population_rows=expected_population_rows,
    )
    typer.echo(f"sample={output} manifest={manifest} shards={len(paths)}")


@app.command("generate-deterministic")
def generate_deterministic_command(
    validate_only: Annotated[
        bool,
        typer.Option(
            "--validate-only",
            help=(
                "Validate and freeze the LF-016 transformation framework without "
                "generating theorem variants."
            ),
        ),
    ] = False,
    validate_positives: Annotated[
        bool,
        typer.Option(
            "--validate-positives",
            help=(
                "Construct and hash-bind the LF-017 P01/P02/P04-lite "
                "implementations without generating training data."
            ),
        ),
    ] = False,
    validate_negatives: Annotated[
        bool,
        typer.Option(
            "--validate-negatives",
            help=(
                "Construct and hash-bind the LF-018 N01/N02/N03/N07/N10 "
                "implementations without generating training data."
            ),
        ),
    ] = False,
    run_negative_pre_scale: Annotated[
        bool,
        typer.Option(
            "--run-negative-pre-scale",
            help=(
                "Execute and persist the Lean-backed LF-018 five-family "
                "negative pre-scale audit slice."
            ),
        ),
    ] = False,
    run_smoke_vertical_slice: Annotated[
        bool,
        typer.Option(
            "--run-smoke-vertical-slice",
            help=(
                "Execute two immutable LF-019 smoke runs, verify deterministic "
                "semantic replay, and emit only smoke-ineligible artifacts."
            ),
        ),
    ] = False,
    materialize_scale: Annotated[
        bool,
        typer.Option(
            "--materialize-scale",
            help=(
                "Materialize provisional deterministic v1 candidates from an "
                "immutable theorem/repr_v3 inventory."
            ),
        ),
    ] = False,
    merge_scale_shards: Annotated[
        bool,
        typer.Option(
            "--merge-scale-shards",
            help=(
                "Audit a complete deterministic shard set and write one "
                "content-addressed merged manifest."
            ),
        ),
    ] = False,
    freeze_scale_inventory: Annotated[
        bool,
        typer.Option(
            "--freeze-scale-inventory",
            help=(
                "Validate and atomically freeze an authoritative theorem/repr_v3 "
                "inventory manifest for scientific materialization."
            ),
        ),
    ] = False,
    code_bundle: Annotated[
        Path | None,
        typer.Option(
            "--code-bundle",
            help="Optional content-addressed source bundle bound to LF-019 runs.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
    report_path: Annotated[
        Path | None,
        typer.Option("--report", help="Validation-report output override."),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="LF-018 pre-scale or scientific-scale output directory.",
        ),
    ] = None,
    theorem_jsonl: Annotated[
        Path | None,
        typer.Option("--theorems", help="Immutable source TheoremRecord JSONL."),
    ] = None,
    representation_jsonl: Annotated[
        Path | None,
        typer.Option("--representations", help="Matching repr_v3 RepresentationRecord JSONL."),
    ] = None,
    source_inventory_manifest: Annotated[
        Path | None,
        typer.Option(
            "--source-inventory-manifest",
            help=(
                "Authoritative manifest binding the exact theorem and repr_v3 source partitions."
            ),
        ),
    ] = None,
    theorem_upstream_manifest: Annotated[
        Path | None,
        typer.Option(
            "--theorem-upstream-manifest",
            help=(
                "Trusted extraction OutputManifest or frozen Gate-3 selection manifest "
                "binding the theorem partition."
            ),
        ),
    ] = None,
    representation_upstream_manifest: Annotated[
        Path | None,
        typer.Option(
            "--representation-upstream-manifest",
            help=(
                "Trusted representation OutputManifest binding both the repr_v3 "
                "partition and its theorem input."
            ),
        ),
    ] = None,
    extraction_reuse_attestation: Annotated[
        Path | None,
        typer.Option(
            "--extraction-reuse-attestation",
            help=(
                "Reviewed LF-022 attestation authorizing the exact "
                "content-addressed representation-output/theorem-input relocation."
            ),
        ),
    ] = None,
    project_dir: Annotated[
        Path | None,
        typer.Option("--project-dir", help="Pinned mathlib checkout for candidate validation."),
    ] = None,
    scale_config: Annotated[
        Path | None,
        typer.Option("--scale-config", help="Deterministic scale-policy YAML override."),
    ] = None,
    max_sources: Annotated[
        int | None,
        typer.Option("--max-sources", min=1, help="Deterministic source-prefix smoke/scale limit."),
    ] = None,
    shard_count: Annotated[
        int,
        typer.Option(
            "--shard-count",
            min=1,
            help=(
                "Number of deterministic root-component source shards. Values above "
                "one require the unary-only profile; N10 is a separate global pass."
            ),
        ),
    ] = 1,
    shard_index: Annotated[
        int,
        typer.Option(
            "--shard-index",
            min=0,
            help="Zero-based deterministic source shard to materialize.",
        ),
    ] = 0,
    shard_output_dirs: Annotated[
        list[Path] | None,
        typer.Option(
            "--shard-output-dir",
            help="Producer output directory; repeat once for every shard during merge.",
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume the exact hash-bound append-only scale journal."),
    ] = False,
    fast_resume: Annotated[
        bool,
        typer.Option(
            "--fast-resume",
            help=(
                "Retired unsafe compatibility flag. Scientific resume always performs "
                "full Lean-backed replay of completed source shards."
            ),
        ),
    ] = False,
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option("--memory-hard-limit-mb", min=256),
    ] = None,
) -> None:
    """Validate transformations or run the persisted LF-018 pre-scale slice."""
    from leanfaith.cli.transformations import (
        TransformationFrameworkValidationError,
        validate_transformation_framework,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    if (
        sum(
            (
                validate_only,
                validate_positives,
                validate_negatives,
                run_negative_pre_scale,
                run_smoke_vertical_slice,
                materialize_scale,
                merge_scale_shards,
                freeze_scale_inventory,
            )
        )
        > 1
    ):
        typer.echo(
            "--validate-only, --validate-positives, --validate-negatives, and "
            "--run-negative-pre-scale/--run-smoke-vertical-slice/"
            "--materialize-scale/--merge-scale-shards/"
            "--freeze-scale-inventory are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=2)
    if code_bundle is not None and not run_smoke_vertical_slice:
        typer.echo("--code-bundle is supported only with --run-smoke-vertical-slice", err=True)
        raise typer.Exit(code=2)
    if extraction_reuse_attestation is not None and not freeze_scale_inventory:
        typer.echo(
            "--extraction-reuse-attestation is supported only with --freeze-scale-inventory",
            err=True,
        )
        raise typer.Exit(code=2)
    if merge_scale_shards:
        if report_path is not None:
            typer.echo("--report is not accepted with --merge-scale-shards", err=True)
            raise typer.Exit(code=2)
        if output_dir is None or not shard_output_dirs:
            typer.echo(
                "--merge-scale-shards requires --output-dir and repeated --shard-output-dir",
                err=True,
            )
            raise typer.Exit(code=2)
        forbidden = [
            name
            for name, used in (
                ("--theorems", theorem_jsonl is not None),
                ("--representations", representation_jsonl is not None),
                ("--source-inventory-manifest", source_inventory_manifest is not None),
                ("--project-dir", project_dir is not None),
                ("--scale-config", scale_config is not None),
                ("--max-sources", max_sources is not None),
                ("--resume", resume),
                ("--fast-resume", fast_resume),
                ("--shard-count", shard_count != 1),
                ("--shard-index", shard_index != 0),
            )
            if used
        ]
        if forbidden:
            typer.echo(
                "--merge-scale-shards does not accept " + ", ".join(forbidden),
                err=True,
            )
            raise typer.Exit(code=2)
        from leanfaith.transforms.scale_materializer import DeterministicScaleError
        from leanfaith.transforms.scale_merge import merge_deterministic_scale_shards

        try:
            merged_artifacts = merge_deterministic_scale_shards(
                paths=paths,
                shard_output_dirs=shard_output_dirs,
                output_dir=output_dir,
            )
        except DeterministicScaleError as exc:
            typer.echo(f"deterministic scale shard merge FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            "deterministic scale shard merge OK; "
            f"output={merged_artifacts.output_dir} "
            f"manifest={merged_artifacts.manifest_path} "
            f"manifest_sha256={merged_artifacts.manifest_sha256} "
            f"merged_manifest_hash={merged_artifacts.merged_manifest_hash} "
            "resolved_semantic_labels=0 promoted_items=0 output_tier=provisional "
            "merge_replayed_with_lean=true training_eligible=false"
        )
        return
    if run_smoke_vertical_slice:
        if report_path is not None or output_dir is not None:
            typer.echo(
                "--report and --output-dir are not accepted for the paired LF-019 replay",
                err=True,
            )
            raise typer.Exit(code=2)
        from leanfaith.cli.smoke_vertical import (
            LF019SmokeError,
            run_lf019_smoke_replay,
        )

        try:
            replay = run_lf019_smoke_replay(
                paths=paths,
                code_bundle_path=code_bundle,
            )
        except LF019SmokeError as exc:
            typer.echo(
                "LF-019 smoke FAILED: "
                f"{exc}; report={exc.artifacts.report_path} "
                f"output_manifest={exc.artifacts.output_manifest_path} "
                f"run_manifest={exc.artifacts.run_manifest_path}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.echo(
            "LF-019 smoke OK; "
            f"run_a_report={replay.run_a.report_path} "
            f"run_b_report={replay.run_b.report_path} "
            f"semantic_fingerprint={replay.run_b.semantic_fingerprint} "
            f"gate_4g_closed={str(replay.run_b.gate_4g_closed).lower()} "
            "gate_4a_closed=false gate_4b_closed=false"
        )
        return

    if freeze_scale_inventory:
        missing = [
            name
            for name, value in (
                ("--theorems", theorem_jsonl),
                ("--representations", representation_jsonl),
                ("--source-inventory-manifest", source_inventory_manifest),
                ("--theorem-upstream-manifest", theorem_upstream_manifest),
                (
                    "--representation-upstream-manifest",
                    representation_upstream_manifest,
                ),
            )
            if value is None
        ]
        if missing:
            typer.echo(
                "--freeze-scale-inventory requires " + ", ".join(missing),
                err=True,
            )
            raise typer.Exit(code=2)
        from leanfaith.transforms.scale_materializer import (
            DeterministicScaleError,
            freeze_deterministic_scale_source_inventory,
        )

        assert theorem_jsonl is not None
        assert representation_jsonl is not None
        assert source_inventory_manifest is not None
        assert theorem_upstream_manifest is not None
        assert representation_upstream_manifest is not None
        try:
            frozen = freeze_deterministic_scale_source_inventory(
                repo_root=paths.root,
                theorem_jsonl=theorem_jsonl,
                representation_jsonl=representation_jsonl,
                theorem_upstream_manifest=theorem_upstream_manifest,
                representation_upstream_manifest=representation_upstream_manifest,
                relocation_attestation=extraction_reuse_attestation,
                manifest_path=source_inventory_manifest,
            )
        except DeterministicScaleError as exc:
            typer.echo(f"deterministic scale inventory freeze FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            "deterministic scale inventory freeze OK; "
            f"manifest={frozen.manifest_path} "
            f"manifest_sha256={frozen.manifest_sha256} "
            f"theorems={frozen.theorem_count} "
            f"representations={frozen.representation_count}"
        )
        return
    if materialize_scale:
        if report_path is not None:
            typer.echo("--report is not accepted with --materialize-scale", err=True)
            raise typer.Exit(code=2)
        if shard_output_dirs:
            typer.echo(
                "--shard-output-dir is supported only with --merge-scale-shards",
                err=True,
            )
            raise typer.Exit(code=2)
        if shard_index >= shard_count:
            typer.echo("--shard-index must be smaller than --shard-count", err=True)
            raise typer.Exit(code=2)
        if fast_resume:
            typer.echo(
                "--fast-resume is retired because receipt hashes are not scientific "
                "verification; use exact --resume Lean replay",
                err=True,
            )
            raise typer.Exit(code=2)
        missing = [
            name
            for name, value in (
                ("--theorems", theorem_jsonl),
                ("--representations", representation_jsonl),
                ("--source-inventory-manifest", source_inventory_manifest),
                ("--project-dir", project_dir),
                ("--output-dir", output_dir),
            )
            if value is None
        ]
        if missing:
            typer.echo(
                "--materialize-scale requires " + ", ".join(missing),
                err=True,
            )
            raise typer.Exit(code=2)
        from leanfaith.transforms.scale_materializer import (
            DeterministicScaleError,
            run_deterministic_scale_materialization,
        )

        assert theorem_jsonl is not None
        assert representation_jsonl is not None
        assert source_inventory_manifest is not None
        assert project_dir is not None
        assert output_dir is not None
        try:
            scale_artifacts = run_deterministic_scale_materialization(
                paths=paths,
                theorem_jsonl=theorem_jsonl,
                representation_jsonl=representation_jsonl,
                source_inventory_manifest=source_inventory_manifest,
                project_dir=project_dir,
                output_dir=output_dir,
                config_path=scale_config,
                max_sources=max_sources,
                shard_count=shard_count,
                shard_index=shard_index,
                resume=resume,
                fast_resume=fast_resume,
                memory_hard_limit_mb=memory_hard_limit_mb,
            )
        except DeterministicScaleError as exc:
            typer.echo(f"deterministic scale materialization FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            "deterministic scale materialization OK; "
            f"output={scale_artifacts.output_dir} "
            f"run_spec={scale_artifacts.run_spec_path} "
            f"manifest={scale_artifacts.manifest_path} "
            f"manifest_sha256={scale_artifacts.manifest_sha256} "
            "resolved_semantic_labels=0 promoted_items=0 output_tier=provisional"
        )
        return
    if run_negative_pre_scale:
        from leanfaith.cli.negative_pre_scale import (
            NegativePreScaleAuditError,
            run_negative_pre_scale_audit,
        )

        try:
            pre_scale_result = run_negative_pre_scale_audit(
                paths=paths,
                output_dir=output_dir,
                report_path=report_path,
            )
        except NegativePreScaleAuditError as exc:
            typer.echo(
                "LF-018 pre-scale FAILED: "
                f"{exc}; report={exc.artifacts.report_path} "
                f"output_manifest={exc.artifacts.output_manifest_path} "
                f"run_manifest={exc.artifacts.run_manifest_path}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"LF-018 pre-scale OK; output={pre_scale_result.output_dir} "
            f"report={pre_scale_result.report_path} "
            f"run_manifest={pre_scale_result.run_manifest_path} "
            "generated_drafts=5 generated_pairs=5 resolved_semantic_labels=0 "
            "promoted_items=0 gate_4g_closed=false"
        )
        return
    if validate_positives:
        from leanfaith.cli.positive_transformations import (
            PositiveRuleValidationError,
            validate_positive_rule_implementations,
        )

        try:
            positive_result = validate_positive_rule_implementations(
                paths=paths,
                report_path=report_path,
            )
        except PositiveRuleValidationError as exc:
            typer.echo(
                f"LF-017 validation FAILED: {exc}; failure_report={exc.report_path}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"LF-017 positives OK; report={positive_result.report_path} "
            f"run_manifest={positive_result.run_manifest_path} "
            "generated_drafts=0 resolved_semantic_labels=0"
        )
        return
    if validate_negatives:
        from leanfaith.cli.negative_transformations import (
            NegativeRuleValidationError,
            validate_negative_rule_implementations,
        )

        try:
            negative_result = validate_negative_rule_implementations(
                paths=paths,
                report_path=report_path,
            )
        except NegativeRuleValidationError as exc:
            typer.echo(
                f"LF-018 validation FAILED: {exc}; failure_report={exc.report_path}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"LF-018 negatives OK; report={negative_result.report_path} "
            f"run_manifest={negative_result.run_manifest_path} "
            "generated_drafts=0 generated_pairs=0 resolved_semantic_labels=0 "
            "promoted_items=0 gate_4g_closed=false"
        )
        return
    if not validate_only:
        typer.echo(
            "choose one deterministic action; "
            "use --validate-only, --validate-positives, --validate-negatives, or "
            "--run-negative-pre-scale/--run-smoke-vertical-slice/--materialize-scale",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        result = validate_transformation_framework(paths=paths, report_path=report_path)
    except TransformationFrameworkValidationError as exc:
        typer.echo(
            f"LF-016 validation FAILED: {exc}; failure_report={exc.report_path}",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"LF-016 framework OK; registry_snapshot={result.snapshot_path} "
        f"report={result.report_path} run_manifest={result.run_manifest_path} "
        "generated_drafts=0"
    )


@app.command("run-deterministic-shards")
def run_deterministic_shards_command(
    theorem_jsonl: Annotated[
        Path,
        typer.Option("--theorems", help="Immutable source TheoremRecord JSONL."),
    ],
    representation_jsonl: Annotated[
        Path,
        typer.Option(
            "--representations",
            help="Matching repr_v3 RepresentationRecord JSONL.",
        ),
    ],
    source_inventory_manifest: Annotated[
        Path,
        typer.Option(
            "--source-inventory-manifest",
            help="Authoritative deterministic source inventory manifest.",
        ),
    ],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", help="Pinned mathlib checkout."),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Parent for isolated shard directories plus logs/status.",
        ),
    ],
    shard_count: Annotated[
        int,
        typer.Option("--shard-count", min=1, help="Total immutable source shard count."),
    ],
    shard_indices: Annotated[
        list[int] | None,
        typer.Option(
            "--shard-index",
            min=0,
            help="Selected zero-based shard; repeat as needed (default: every shard).",
        ),
    ] = None,
    max_parallel: Annotated[
        int,
        typer.Option(
            "--max-parallel",
            min=1,
            help="Maximum independent shard processes to run concurrently.",
        ),
    ] = 1,
    resume_incomplete: Annotated[
        bool,
        typer.Option(
            "--resume-incomplete",
            help="Skip completed shards and exact-resume incomplete shard journals.",
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
    scale_config: Annotated[
        Path | None,
        typer.Option("--scale-config", help="Deterministic scale-policy YAML override."),
    ] = None,
    max_sources: Annotated[
        int | None,
        typer.Option("--max-sources", min=1, help="Shared source-universe prefix limit."),
    ] = None,
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option("--memory-hard-limit-mb", min=256),
    ] = None,
) -> None:
    """Run independent deterministic materializer shards with bounded concurrency."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.shard_launcher import (
        DeterministicShardLaunchError,
        run_deterministic_shards,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        summary = run_deterministic_shards(
            paths=paths,
            theorem_jsonl=theorem_jsonl,
            representation_jsonl=representation_jsonl,
            source_inventory_manifest=source_inventory_manifest,
            project_dir=project_dir,
            output_root=output_root,
            shard_count=shard_count,
            shard_indices=shard_indices,
            max_parallel=max_parallel,
            resume_incomplete=resume_incomplete,
            scale_config=scale_config,
            max_sources=max_sources,
            memory_hard_limit_mb=memory_hard_limit_mb,
        )
    except (DeterministicShardLaunchError, OSError, ValueError) as exc:
        typer.echo(f"deterministic shard launch FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic shard launch complete; "
        f"succeeded={len(summary.succeeded_shards)} "
        f"skipped_complete={len(summary.skipped_complete_shards)} "
        f"failed={len(summary.failed_shards)} "
        f"summary={output_root.resolve() / 'orchestration/latest_summary.json'}"
    )
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command("merge-deterministic-shards")
def merge_deterministic_shards_command(
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Completed run-deterministic-shards output root.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Content-addressed merged inventory output directory.",
        ),
    ],
    expected_shard_count: Annotated[
        int | None,
        typer.Option(
            "--expected-shard-count",
            min=1,
            help="Optional operator assertion for the complete shard count.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Discover, audit, replay, and atomically merge one complete shard run."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.scale_materializer import DeterministicScaleError
    from leanfaith.transforms.shard_merge import merge_deterministic_shard_run

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        merged = merge_deterministic_shard_run(
            paths=paths,
            output_root=output_root,
            output_dir=output_dir,
            expected_shard_count=expected_shard_count,
        )
    except (DeterministicScaleError, OSError, ValueError) as exc:
        typer.echo(f"deterministic shard-run merge FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic shard-run merge complete; "
        f"output={merged.output_dir} "
        f"manifest={merged.manifest_path} "
        f"manifest_sha256={merged.manifest_sha256} "
        f"merged_manifest_hash={merged.merged_manifest_hash} "
        "output_tier=provisional training_eligible=false"
    )


@app.command("combine-deterministic-scale-passes")
def combine_deterministic_scale_passes_command(
    unary_merged_output_dir: Annotated[
        Path,
        typer.Option(
            "--unary-merged-output",
            help="Content-addressed merged output for the unary deterministic pass.",
        ),
    ],
    n10_merged_output_dir: Annotated[
        Path,
        typer.Option(
            "--n10-merged-output",
            help="Content-addressed merged output for the global N10 pass.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Directory for the two-pass compatibility manifest.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Authorize one exact unary pass and one exact global-N10 pass together."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.scale_combine import combine_deterministic_scale_passes
    from leanfaith.transforms.scale_materializer import DeterministicScaleError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        artifacts = combine_deterministic_scale_passes(
            paths=paths,
            unary_merged_output_dir=unary_merged_output_dir,
            n10_merged_output_dir=n10_merged_output_dir,
            output_dir=output_dir,
        )
    except DeterministicScaleError as exc:
        typer.echo(f"deterministic two-pass combination FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic two-pass combination OK; "
        f"manifest={artifacts.manifest_path} "
        f"manifest_sha256={artifacts.manifest_sha256} "
        f"combined_manifest_hash={artifacts.combined_manifest_hash} "
        "scientific_pairing_eligible=true output_tier=provisional "
        "training_eligible=false"
    )


@app.command("close-gate4g")
def close_gate4g_command(
    run_a_report: Annotated[Path, typer.Option("--run-a-report")],
    run_b_report: Annotated[Path, typer.Option("--run-b-report")],
    phase_report: Annotated[Path | None, typer.Option("--phase-report")] = None,
    lf019_milestone: Annotated[
        Path | None,
        typer.Option("--lf019-milestone"),
    ] = None,
    output_path: Annotated[Path | None, typer.Option("--out")] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Bind two clean LF-019 replay runs and close only generation Gate 4G."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.gate4g import Gate4GFinalizationError, finalize_gate4g

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        result = finalize_gate4g(
            paths=paths,
            run_a_report_path=run_a_report,
            run_b_report_path=run_b_report,
            phase_report_path=phase_report,
            lf019_milestone_path=lf019_milestone,
            output_path=output_path,
        )
    except Gate4GFinalizationError as exc:
        typer.echo(f"Gate 4G finalization FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Gate 4G PASS; report={paths.root / result.report_path} "
        f"sha256={result.report_sha256} "
        "gate_4a_closed=false gate_4b_closed=false"
    )


@app.command("collect-evidence")
def collect_evidence_command(
    context_paths: Annotated[
        list[Path],
        typer.Option(
            "--contexts",
            help="Explicit ContextRecord JSONL; repeat for multiple partitions.",
        ),
    ],
    theorem_paths: Annotated[
        list[Path],
        typer.Option(
            "--theorems",
            help="Explicit TheoremRecord JSONL; repeat for source/candidate partitions.",
        ),
    ],
    representation_paths: Annotated[
        list[Path],
        typer.Option(
            "--representations",
            help=("Explicit RepresentationRecord JSONL; repeat for source/candidate partitions."),
        ),
    ],
    pair_path: Annotated[Path, typer.Option("--pairs", help="Explicit PairRecord JSONL.")],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", help="Pinned Lake project used by LeanInteract."),
    ],
    upstream_evidence_paths: Annotated[
        list[Path] | None,
        typer.Option(
            "--upstream-evidence",
            help=(
                "Canonical EvidenceRecord JSONL resolving preexisting pair links; "
                "repeat for multiple partitions."
            ),
        ),
    ] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir")] = None,
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir")] = None,
    artifact_dir: Annotated[Path | None, typer.Option("--artifact-dir")] = None,
    alignment_path: Annotated[
        Path | None,
        typer.Option(
            "--alignments",
            help="Optional explicit ClaimAlignmentSpec JSONL.",
        ),
    ] = None,
    artifact_class: Annotated[
        str,
        typer.Option(
            "--artifact-class",
            help="auto, production, smoke, or diagnostic; smoke inputs always stay smoke.",
        ),
    ] = "auto",
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option("--memory-hard-limit-mb", min=256),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Collect LF-020 symbolic evidence without creating semantic labels."""
    from leanfaith.cli.collect_evidence import (
        EvidenceCollectionInputError,
        run_collect_evidence,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        result = run_collect_evidence(
            paths=paths,
            context_paths=context_paths,
            theorem_paths=theorem_paths,
            representation_paths=representation_paths,
            pair_path=pair_path,
            project_dir=project_dir,
            upstream_evidence_paths=upstream_evidence_paths or (),
            out_dir=out_dir,
            cache_dir=cache_dir,
            artifact_dir=artifact_dir,
            alignment_path=alignment_path,
            artifact_class=artifact_class,
            memory_hard_limit_mb=memory_hard_limit_mb,
            limit=limit,
        )
    except EvidenceCollectionInputError as exc:
        typer.echo(f"LF-020 evidence input rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"LF-020 evidence complete; output={result.output_dir} "
        f"manifest={result.output_manifest_path} "
        f"run_manifest={result.run_manifest_path} "
        f"artifact_class={result.artifact_class.value} "
        f"pairs={result.pair_count} evidence={result.evidence_count} "
        f"failures={result.failure_count} cache_hits={result.cache_hits} "
        f"cache_misses={result.cache_misses} resolved_labels_created=0"
    )
    if result.failure_count:
        raise typer.Exit(code=1)


@app.command("collect-real-outputs")
def collect_real_outputs_command(
    validate_foundation: Annotated[
        bool,
        typer.Option(
            "--validate-foundation",
            help=(
                "Validate and hash-bind the disabled LF-021 foundation without "
                "making provider calls."
            ),
        ),
    ] = False,
    run_offline_smoke: Annotated[
        bool,
        typer.Option(
            "--run-offline-smoke",
            help=(
                "Run the one-example ADR-0005 deterministic fixture and byte replay; "
                "no network provider or semantic label is used."
            ),
        ),
    ] = False,
    report_path: Annotated[
        Path | None,
        typer.Option("--report", help="Override the foundation-validation report path."),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Immutable output directory for offline smoke."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Validate LF-021 foundations or run its authorized offline smoke."""
    from leanfaith.cli.collect_real_outputs import (
        LF021FoundationError,
        run_lf021_offline_smoke,
        validate_lf021_foundation,
    )
    from leanfaith.config.hashing import hash_file
    from leanfaith.config.paths import RepoPaths
    from leanfaith.schemas import (
        ArtifactClass,
        RunManifest,
        collect_code_state,
        new_run_id,
        run_manifest_path,
        write_manifest,
    )

    if validate_foundation and run_offline_smoke:
        typer.echo(
            "--validate-foundation and --run-offline-smoke are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=2)
    if not validate_foundation and not run_offline_smoke:
        typer.echo(
            "real-output execution is not authorized by the checked-in config; "
            "use --validate-foundation or the ADR-0005 --run-offline-smoke mode",
            err=True,
        )
        raise typer.Exit(code=2)

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    if run_offline_smoke:
        smoke_argv = ["leanfaith", "collect-real-outputs", "--run-offline-smoke"]
        if output_dir is not None:
            smoke_argv.extend(("--output-dir", str(output_dir)))
        if root_dir is not None:
            smoke_argv.extend(("--root", str(root_dir)))
        try:
            smoke = run_lf021_offline_smoke(
                paths,
                output_dir=output_dir,
                argv=tuple(smoke_argv),
            )
        except (LF021FoundationError, ValueError, OSError) as exc:
            typer.echo(f"LF-021 offline smoke FAILED: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            f"LF-021 offline smoke {'PASSED' if smoke.report.passed else 'FAILED'}; "
            f"output={smoke.output_dir} report={smoke.report_path} "
            f"run_manifest={smoke.run_manifest_path} network_calls_made=0 "
            f"semantic_labels_created=0 gate_5_closed=false"
        )
        if not smoke.report.passed:
            raise typer.Exit(code=1)
        return

    try:
        validation = validate_lf021_foundation(paths)
    except (LF021FoundationError, ValueError) as exc:
        typer.echo(f"LF-021 foundation validation FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    output_path = report_path or paths.reports / "generation" / "lf021_foundation_v1.json"
    if not output_path.is_absolute():
        output_path = paths.root / output_path
    try:
        output_key = str(output_path.resolve().relative_to(paths.root.resolve()))
    except ValueError as exc:
        typer.echo("LF-021 report path must stay inside the repository root", err=True)
        raise typer.Exit(code=2) from exc
    report_sha256 = write_manifest(validation.report, output_path)
    created_at = datetime.datetime.now(tz=datetime.UTC)
    run_id = new_run_id(created_at)
    config_paths = (
        paths.configs / "generation" / "problem_pool_v1.yaml",
        paths.configs / "generation" / "real_outputs_v1.yaml",
    )
    provider_path = paths.configs / "generation" / "providers.yaml"
    manifest = RunManifest(
        run_id=run_id,
        artifact_class=ArtifactClass.DIAGNOSTIC,
        command="leanfaith collect-real-outputs --validate-foundation",
        argv=("leanfaith", "collect-real-outputs", "--validate-foundation"),
        code=collect_code_state(paths.root),
        config_hashes={
            str(config_paths[0].relative_to(paths.root)): (
                validation.configs.problem_pool.config_hash
            ),
            str(config_paths[1].relative_to(paths.root)): (
                validation.configs.real_outputs.config_hash
            ),
        },
        input_hashes={
            str(provider_path.relative_to(paths.root)): hash_file(provider_path),
        },
        output_hashes={output_key: report_sha256},
        status_counts={
            "checks_passed": len(validation.report.checks),
            "checks_failed": 0,
            "provider_calls_made": 0,
            "semantic_labels_created": 0,
        },
        created_at=created_at,
        notes=(
            "LF-021 foundation validation only; provider/local/replay execution "
            "remains disabled pending the Phase-5 ADR."
        ),
    )
    manifest_path = run_manifest_path(paths, run_id)
    write_manifest(manifest, manifest_path)
    typer.echo(
        f"LF-021 foundation OK; report={output_path} "
        f"run_manifest={manifest_path} execution_authorized=false "
        "provider_calls=0 semantic_labels_created=0"
    )


@app.command("report-prevalence")
def report_prevalence_command(
    frame_decision_path: Annotated[
        Path,
        typer.Option(
            "--frame-decision",
            help="Verified randomized frame-freeze v3 decision.",
        ),
    ],
    adjudication_path: Annotated[
        Path,
        typer.Option(
            "--adjudications",
            help="Immutable prevalence-adjudication projection JSONL.",
        ),
    ],
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Canonical JSON report destination (divergent overwrite is rejected).",
        ),
    ],
    policy_path: Annotated[
        Path,
        typer.Option(
            "--prevalence-policy",
            help="Frozen frame-schema-3 prevalence design.",
        ),
    ] = Path("policies/lf021_prevalence_design_v2.yaml"),
    frame_freeze_policy_path: Annotated[
        Path,
        typer.Option(
            "--frame-freeze-policy",
            help="Frozen randomized frame-freeze v3 policy.",
        ),
    ] = Path("configs/generation/lf021_frame_freeze_v3.yaml"),
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Compute LF-021 design-weighted prevalence without changing labels or gates."""
    from leanfaith.cli.report_prevalence import run_report_prevalence
    from leanfaith.config.paths import RepoPaths
    from leanfaith.evaluation.prevalence import PrevalenceInputError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_report_prevalence(
            repo_root=paths.root,
            frame_decision_path=anchored(frame_decision_path),
            adjudication_path=anchored(adjudication_path),
            policy_path=anchored(policy_path),
            frame_freeze_policy_path=anchored(frame_freeze_policy_path),
            output_path=anchored(output_path),
        )
    except (OSError, ValueError, PrevalenceInputError) as exc:
        typer.echo(f"LF-021 prevalence report rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"LF-021 prevalence report written: {result.output_path} "
        f"sha256={result.output_sha256} labels_created=0 gate_5g_closed=false "
        "gate_5_closed=false"
    )


@app.command("provision-argilla-prevalence")
def provision_argilla_prevalence_command(
    authentication_key_path: Annotated[
        Path,
        typer.Option(
            "--authentication-key",
            help="Mode-0600 HMAC key authenticating both pre-response assignments.",
        ),
    ],
    endpoint: Annotated[
        str,
        typer.Option("--endpoint", help="Self-hosted HTTPS Argilla origin."),
    ],
    owner_api_key_env: Annotated[
        str,
        typer.Option(
            "--owner-api-key-env",
            help="Environment-variable name containing the Argilla owner key.",
        ),
    ],
    slot_1_assignment: Annotated[
        Path,
        typer.Option("--slot-1-assignment", help="Authenticated slot-1 assignment."),
    ],
    slot_2_assignment: Annotated[
        Path,
        typer.Option("--slot-2-assignment", help="Authenticated slot-2 assignment."),
    ],
    slot_1_bundle_manifest: Annotated[
        Path,
        typer.Option(
            "--slot-1-bundle-manifest",
            help="Exact frozen slot-1 public bundle manifest.",
        ),
    ],
    slot_2_bundle_manifest: Annotated[
        Path,
        typer.Option(
            "--slot-2-bundle-manifest",
            help="Exact frozen slot-2 public bundle manifest.",
        ),
    ],
    slot_1_workspace: Annotated[
        str,
        typer.Option("--slot-1-workspace", help="New isolated slot-1 workspace name."),
    ],
    slot_2_workspace: Annotated[
        str,
        typer.Option("--slot-2-workspace", help="New isolated slot-2 workspace name."),
    ],
    slot_1_dataset: Annotated[
        str,
        typer.Option("--slot-1-dataset", help="New slot-1 dataset name."),
    ],
    slot_2_dataset: Annotated[
        str,
        typer.Option("--slot-2-dataset", help="New slot-2 dataset name."),
    ],
    slot_1_annotator_backend_id: Annotated[
        str,
        typer.Option("--slot-1-annotator-id", help="Existing slot-1 Argilla user UUID."),
    ],
    slot_2_annotator_backend_id: Annotated[
        str,
        typer.Option("--slot-2-annotator-id", help="Existing slot-2 Argilla user UUID."),
    ],
    slot_1_api_key_env: Annotated[
        str,
        typer.Option(
            "--slot-1-api-key-env",
            help="Environment-variable name containing the slot-1 Argilla key.",
        ),
    ],
    slot_2_api_key_env: Annotated[
        str,
        typer.Option(
            "--slot-2-api-key-env",
            help="Environment-variable name containing the slot-2 Argilla key.",
        ),
    ],
    adjudication_workspace: Annotated[
        str,
        typer.Option(
            "--adjudication-workspace",
            help="New owner-only adjudication workspace name.",
        ),
    ],
    provisioned_at: Annotated[
        str,
        typer.Option("--provisioned-at", help="Explicit ISO-8601 UTC provisioning time."),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="New operator-chosen private root for all provisioning bindings.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Provision the two exact 240-item Argilla datasets without creating votes."""
    from leanfaith.cli.argilla_provisioning_operations import (
        ArgillaProvisioningOperationError,
        ArgillaProvisioningSlotInput,
        provision_argilla_prevalence_round,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        timestamp = datetime.datetime.fromisoformat(provisioned_at.replace("Z", "+00:00"))
        result = provision_argilla_prevalence_round(
            repo_root=paths.root,
            authentication_key_path=anchored(authentication_key_path),
            endpoint=endpoint,
            owner_api_key_env=owner_api_key_env,
            adjudication_workspace_name=adjudication_workspace,
            slot_inputs=(
                ArgillaProvisioningSlotInput(
                    annotator_slot="independent_annotator_1",
                    assignment_path=anchored(slot_1_assignment),
                    public_bundle_manifest_path=anchored(slot_1_bundle_manifest),
                    workspace_name=slot_1_workspace,
                    dataset_name=slot_1_dataset,
                    annotator_backend_id=slot_1_annotator_backend_id,
                    annotator_api_key_env=slot_1_api_key_env,
                ),
                ArgillaProvisioningSlotInput(
                    annotator_slot="independent_annotator_2",
                    assignment_path=anchored(slot_2_assignment),
                    public_bundle_manifest_path=anchored(slot_2_bundle_manifest),
                    workspace_name=slot_2_workspace,
                    dataset_name=slot_2_dataset,
                    annotator_backend_id=slot_2_annotator_backend_id,
                    annotator_api_key_env=slot_2_api_key_env,
                ),
            ),
            provisioned_at=timestamp,
            output_root=anchored(output_root),
        )
    except (ArgillaProvisioningOperationError, OSError, ValueError) as exc:
        typer.echo(f"Argilla prevalence provisioning rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"runtime_manifest_id={result.manifest.manifest_id} "
        f"runtime_manifest={result.manifest_path} output_root={result.output_root} "
        f"recovery_journal={result.recovery_journal_path} "
        "datasets=2 records_per_dataset=240 response_count=0 "
        "peer_isolation_verified=true semantic_labels_created=0 "
        "gold_labels_created=0 human_gold_eligible=false training_eligible=0"
    )


@app.command("cleanup-argilla-provisioning")
def cleanup_argilla_provisioning_command(
    recovery_journal: Annotated[
        Path,
        typer.Option(
            "--recovery-journal",
            help="Private crash-recovery journal emitted before remote provisioning.",
        ),
    ],
    owner_api_key_env: Annotated[
        str,
        typer.Option(
            "--owner-api-key-env",
            help="Environment-variable name containing the owner API key.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Verify and remove an unpublished Argilla crash-recovery attempt."""
    from leanfaith.cli.argilla_provisioning_operations import (
        ArgillaProvisioningOperationError,
        cleanup_argilla_provisioning_recovery,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    journal_path = (
        recovery_journal if recovery_journal.is_absolute() else paths.root / recovery_journal
    )
    try:
        result = cleanup_argilla_provisioning_recovery(
            journal_path=journal_path,
            owner_api_key_env=owner_api_key_env,
        )
    except (ArgillaProvisioningOperationError, OSError, ValueError) as exc:
        typer.echo(f"Argilla provisioning cleanup rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"state={result.journal.state} "
        f"deleted_datasets={result.deleted_dataset_count} "
        f"deleted_workspaces={result.deleted_workspace_count}"
    )


@app.command("write-argilla-backend-pin")
def write_argilla_backend_pin_command(
    endpoint: Annotated[
        str,
        typer.Option(
            "--endpoint",
            help="Pinned self-hosted HTTPS Argilla origin, without an API path.",
        ),
    ],
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Pinned Argilla workspace UUID."),
    ],
    dataset_id: Annotated[
        str,
        typer.Option("--dataset-id", help="Pinned Argilla dataset UUID."),
    ],
    annotator_id: Annotated[
        str,
        typer.Option("--annotator-id", help="Pinned Argilla annotator UUID."),
    ],
    api_key_env: Annotated[
        str,
        typer.Option(
            "--api-key-env",
            help="Environment-variable name containing the API key; never the key itself.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Private directory for the content-addressed backend pin.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Write one content-addressed Argilla backend identity pin without a secret."""
    from leanfaith.cli.argilla_operations import (
        ArgillaCliInputError,
        write_argilla_backend_pin,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    anchored_output = output_dir if output_dir.is_absolute() else paths.root / output_dir
    try:
        result = write_argilla_backend_pin(
            endpoint=endpoint,
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            annotator_id=annotator_id,
            api_key_env=api_key_env,
            output_dir=anchored_output,
        )
    except (ArgillaCliInputError, OSError, ValueError) as exc:
        typer.echo(f"Argilla backend pin rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"pin_id={result.pin.pin_id} path={result.path} sha256={result.sha256} "
        "api_key_persisted=false semantic_labels_created=0 "
        "human_gold_eligible=false training_eligible=0"
    )


@app.command("capture-argilla-responses")
def capture_argilla_responses_command(
    pin_path: Annotated[
        Path,
        typer.Option("--pin", help="Content-addressed Argilla backend pin JSON."),
    ],
    expected_manifest_path: Annotated[
        Path,
        typer.Option(
            "--expected-responses",
            help="Label-free expected-response identity manifest JSON.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Private output root for exact payloads and backend-origin receipts.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Capture exact submitted snapshots; do not create labels or training data."""
    from leanfaith.annotation_support.argilla_backend import ArgillaBackendError
    from leanfaith.cli.argilla_operations import (
        ArgillaCliInputError,
        capture_argilla_submitted_responses,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = capture_argilla_submitted_responses(
            pin_path=anchored(pin_path),
            expected_manifest_path=anchored(expected_manifest_path),
            output_root=anchored(output_root),
        )
    except (ArgillaBackendError, ArgillaCliInputError, OSError, ValueError) as exc:
        typer.echo(f"Argilla response capture rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"backend_origin_receipts={len(result.run.receipts)} "
        f"output_root={result.output_root} "
        f"pin_sha256={result.pin_sha256} "
        f"expected_manifest_sha256={result.expected_manifest_sha256} "
        f"capture_manifest_id={result.manifest.manifest_id} "
        f"capture_manifest_path={result.manifest_path} "
        "submitted_snapshot_only=true backend_immutability_verified=false "
        "project_logical_lock_included=false semantic_labels_created=0 "
        "gold_labels_created=0 human_gold_eligible=false training_eligible=0"
    )


@app.command("write-argilla-projection-binding")
def write_argilla_projection_binding_command(
    assignment_path: Annotated[
        Path,
        typer.Option(
            "--human-assignment",
            help="Private operator-authenticated assignment created before responses.",
        ),
    ],
    public_bundle_manifest_path: Annotated[
        Path,
        typer.Option(
            "--public-bundle-manifest",
            help="Exact public blinded-bundle manifest bound by the assignment.",
        ),
    ],
    pin_path: Annotated[
        Path,
        typer.Option("--pin", help="Content-addressed Argilla backend pin JSON."),
    ],
    mapping_path: Annotated[
        Path,
        typer.Option(
            "--record-mapping",
            help="Private label-free 240-item token-to-record allocation JSON.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Private root for the content-addressed pre-response binding.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze a pre-response Argilla record allocation without reading votes."""
    from leanfaith.cli.argilla_projection_operations import (
        ArgillaProjectionOperationError,
        write_argilla_projection_binding,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = write_argilla_projection_binding(
            repo_root=paths.root,
            assignment_path=anchored(assignment_path),
            public_bundle_manifest_path=anchored(public_bundle_manifest_path),
            pin_path=anchored(pin_path),
            mapping_path=anchored(mapping_path),
            output_root=anchored(output_root),
        )
    except (ArgillaProjectionOperationError, OSError, ValueError) as exc:
        typer.echo(f"Argilla projection binding rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"binding_manifest_id={result.manifest.manifest_id} "
        f"binding_path={result.path} sha256={result.sha256} "
        "pre_response_allocation_only=true response_values_included=false "
        "assignment_hmac_verified=false semantic_labels_created=0 "
        "gold_labels_created=0 human_gold_eligible=false training_eligible=0"
    )


@app.command("project-argilla-capture")
def project_argilla_capture_command(
    assignment_path: Annotated[
        Path,
        typer.Option(
            "--human-assignment",
            help="Private assignment bound before Argilla responses were observed.",
        ),
    ],
    pin_path: Annotated[
        Path,
        typer.Option("--pin", help="Content-addressed Argilla backend pin JSON."),
    ],
    binding_manifest_path: Annotated[
        Path,
        typer.Option(
            "--projection-binding",
            help="Private content-addressed pre-response record-allocation binding.",
        ),
    ],
    capture_root: Annotated[
        Path,
        typer.Option(
            "--capture-root",
            help="Private root created by capture-argilla-responses.",
        ),
    ],
    capture_manifest_path: Annotated[
        Path,
        typer.Option(
            "--capture-manifest",
            help="Content-addressed backend-origin capture manifest JSON.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Private root for canonical locked raw-vote JSONL and manifest.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Project reverified Argilla snapshots into immutable raw-vote JSONL."""
    from leanfaith.cli.argilla_projection_operations import (
        ArgillaProjectionOperationError,
        project_and_persist_argilla_capture,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = project_and_persist_argilla_capture(
            repo_root=paths.root,
            assignment_path=anchored(assignment_path),
            pin_path=anchored(pin_path),
            binding_manifest_path=anchored(binding_manifest_path),
            capture_root=anchored(capture_root),
            capture_manifest_path=anchored(capture_manifest_path),
            output_root=anchored(output_root),
        )
    except (ArgillaProjectionOperationError, OSError, ValueError) as exc:
        typer.echo(f"Argilla capture projection rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"projection_manifest_id={result.run.manifest.manifest_id} "
        f"capture_manifest_id={result.capture_manifest.manifest_id} "
        f"projection_manifest_path={result.manifest_path} "
        f"locked_responses_path={result.locked_responses_path} "
        f"response_count={result.run.manifest.response_count} "
        f"missing_item_count={result.run.manifest.missing_item_count} "
        f"complete={str(result.run.manifest.complete).lower()} "
        "backend_origin_verified=true submitted_snapshot_only=true "
        "assignment_hmac_verified=false import_logical_lock_created=false "
        "semantic_labels_created=0 gold_labels_created=0 "
        "human_gold_eligible=false training_eligible=0 gate_5_closed=false"
    )


@app.command("export-annotation")
def export_annotation_command(
    frame_path: Annotated[
        Path | None,
        typer.Option(
            "--frame",
            help="Exact frozen LF-021 prevalence frame; defaults to the registered frame.",
        ),
    ] = None,
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root",
            help=(
                "Operational annotation-export root; defaults to the ignored "
                "annotation/exports/lf021_prevalence_v1 directory."
            ),
        ),
    ] = None,
    randomization_key_paths: Annotated[
        list[Path] | None,
        typer.Option(
            "--randomization-key",
            help=(
                "Private binary randomization key; repeat exactly twice for an "
                "idempotent audited export. Omit to generate one-time CSPRNG keys."
            ),
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Create two independently randomized, reference-aware blinded bundles."""
    from leanfaith.annotation_support import AnnotationExportError, BlindingError
    from leanfaith.cli.export_annotation import (
        AnnotationExportInputError,
        run_export_annotation,
    )
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = run_export_annotation(
            paths=paths,
            frame_path=anchored(frame_path),
            output_root=anchored(output_root),
            randomization_key_paths=tuple(
                anchored(path) or path for path in (randomization_key_paths or [])
            ),
        )
    except (AnnotationExportError, AnnotationExportInputError, BlindingError, OSError) as exc:
        typer.echo(f"Annotation export rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    for bundle in result.bundles:
        typer.echo(
            f"{bundle.manifest.annotator_slot}: bundle={bundle.bundle_path} "
            f"manifest={bundle.manifest_path}"
        )
    typer.echo(
        f"private_linkage={result.private_linkage_path} "
        f"private_manifest={result.private_manifest_path} "
        "items_per_annotator=240 semantic_labels_created=0 gate_5_closed=false"
    )


@app.command("create-human-assignment")
def create_human_assignment_command(
    public_bundle_manifest_path: Annotated[
        Path,
        typer.Option("--bundle-manifest", help="Public manifest for one blinded slot."),
    ],
    private_linkage_manifest_path: Annotated[
        Path,
        typer.Option(
            "--private-linkage-manifest",
            help="Mode-0600 private linkage manifest from export-annotation.",
        ),
    ],
    authentication_key_path: Annotated[
        Path,
        typer.Option("--authentication-key", help="Mode-0600 LF-023 HMAC key."),
    ],
    round_id: Annotated[str, typer.Option("--round-id", help="Frozen annotation round ID.")],
    annotator_slot: Annotated[
        str,
        typer.Option("--annotator-slot", help="One registered independent annotator slot."),
    ],
    annotator_id: Annotated[
        str,
        typer.Option("--annotator-id", help="Pseudonymous annotator identifier."),
    ],
    annotator_principal_hash: Annotated[
        str,
        typer.Option(
            "--annotator-principal-hash",
            help=(
                "Operator-declared pseudonymous principal hash used to check stated "
                "annotator independence; the HMAC does not prove identity."
            ),
        ),
    ],
    backend_id: Annotated[
        str,
        typer.Option(
            "--backend-id",
            help="Registered backend: argilla, label_studio, or documented Streamlit fallback.",
        ),
    ],
    assigned_at: Annotated[
        str,
        typer.Option("--assigned-at", help="Explicit ISO-8601 UTC assignment timestamp."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="New mode-0600 immutable assignment JSON."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Authenticate one assignment before the human can create responses."""
    from leanfaith.annotation_support import AnnotationImportError, AnnotationOperationError
    from leanfaith.cli.annotation_operations import run_create_human_assignment
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_create_human_assignment(
            paths=paths,
            public_bundle_manifest_path=anchored(public_bundle_manifest_path),
            private_linkage_manifest_path=anchored(private_linkage_manifest_path),
            authentication_key_path=anchored(authentication_key_path),
            round_id=round_id,
            annotator_slot=annotator_slot,
            annotator_id=annotator_id,
            annotator_principal_hash=annotator_principal_hash,
            backend_id=backend_id,
            assigned_at=assigned_at,
            output_path=anchored(output_path),
        )
    except (AnnotationImportError, AnnotationOperationError, OSError, ValueError) as exc:
        typer.echo(f"Human assignment rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"assignment_id={result.assignment.assignment_id} path={result.path} "
        "response_not_yet_observed=true semantic_labels_created=0"
    )


@app.command("attest-human-submission")
def attest_human_submission_command(
    human_assignment_path: Annotated[
        Path,
        typer.Option("--human-assignment", help="Authenticated pre-response assignment."),
    ],
    response_path: Annotated[
        Path,
        typer.Option(
            "--responses",
            help="Mode-0600 exact frozen response-export snapshot JSONL.",
        ),
    ],
    authentication_key_path: Annotated[
        Path,
        typer.Option("--authentication-key", help="Mode-0600 LF-023 HMAC key."),
    ],
    backend_export_id: Annotated[
        str,
        typer.Option(
            "--backend-export-id",
            help="Immutable project snapshot identifier for the backend response export.",
        ),
    ],
    verifier_id: Annotated[
        str,
        typer.Option("--verifier-id", help="Trusted operator/verifier identifier."),
    ],
    attested_at: Annotated[
        str,
        typer.Option("--attested-at", help="Explicit ISO-8601 UTC attestation timestamp."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="New mode-0600 immutable attestation JSON."),
    ],
    confirm_operator_human_origin_assertion: Annotated[
        bool,
        typer.Option(
            "--confirm-operator-assertion",
            help=(
                "Required operator assertion about human origin; HMAC authenticates "
                "the assertion but does not independently prove it."
            ),
        ),
    ] = False,
    confirm_backend_export_locked: Annotated[
        bool,
        typer.Option(
            "--confirm-backend-export-locked",
            help=(
                "Required confirmation that the project-owned response-export snapshot "
                "is frozen; this does not assert backend-row immutability."
            ),
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Operator-attest one exact response export; create no semantic label."""
    from leanfaith.annotation_support import AnnotationImportError, AnnotationOperationError
    from leanfaith.cli.annotation_operations import run_attest_human_submission
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_attest_human_submission(
            paths=paths,
            human_assignment_path=anchored(human_assignment_path),
            response_path=anchored(response_path),
            authentication_key_path=anchored(authentication_key_path),
            backend_export_id=backend_export_id,
            verifier_id=verifier_id,
            attested_at=attested_at,
            confirm_operator_human_origin_assertion=(confirm_operator_human_origin_assertion),
            confirm_backend_export_locked=confirm_backend_export_locked,
            output_path=anchored(output_path),
        )
    except (AnnotationImportError, AnnotationOperationError, OSError, ValueError) as exc:
        typer.echo(f"Human submission attestation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"attestation_id={result.attestation.attestation_id} path={result.path} "
        "origin_assurance=operator_attested operator_attestation_verified=true "
        "backend_origin_verified=false human_gold_eligible=false semantic_labels_created=0"
    )


@app.command("import-annotation")
def import_annotation_command(
    public_bundle_manifest_path: Annotated[
        Path,
        typer.Option(
            "--bundle-manifest",
            help="Public blinded-bundle manifest for exactly one annotator slot.",
        ),
    ],
    private_linkage_manifest_path: Annotated[
        Path,
        typer.Option(
            "--private-linkage-manifest",
            help="Mode-0600 private linkage manifest created by export-annotation.",
        ),
    ],
    human_assignment_path: Annotated[
        Path,
        typer.Option(
            "--human-assignment",
            help="Mode-0600 authenticated assignment fixed before human responses.",
        ),
    ],
    human_submission_attestation_path: Annotated[
        Path,
        typer.Option(
            "--human-submission-attestation",
            help="Mode-0600 authenticated attestation binding the exact response export.",
        ),
    ],
    authentication_key_path: Annotated[
        Path,
        typer.Option(
            "--authentication-key",
            help="Mode-0600 private LF-023 HMAC key; never committed.",
        ),
    ],
    response_path: Annotated[
        Path,
        typer.Option(
            "--responses",
            help="Mode-0600 canonical JSONL of locked annotation responses.",
        ),
    ],
    output_root: Annotated[
        Path | None,
        typer.Option(
            "--output-root",
            help="Private import root; defaults under ignored data/human/pilot_raw/.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Import one locked blinded response set without adjudicating or promoting it."""
    from leanfaith.annotation_support import AnnotationImportError
    from leanfaith.cli.import_annotation import run_import_annotation
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = run_import_annotation(
            paths=paths,
            public_bundle_manifest_path=anchored(public_bundle_manifest_path)
            or public_bundle_manifest_path,
            private_linkage_manifest_path=anchored(private_linkage_manifest_path)
            or private_linkage_manifest_path,
            human_assignment_path=anchored(human_assignment_path) or human_assignment_path,
            human_submission_attestation_path=anchored(human_submission_attestation_path)
            or human_submission_attestation_path,
            authentication_key_path=anchored(authentication_key_path) or authentication_key_path,
            response_path=anchored(response_path) or response_path,
            output_root=anchored(output_root),
        )
    except (AnnotationImportError, OSError, ValueError) as exc:
        typer.echo(f"Annotation import rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"slot={result.manifest.annotator_slot} "
        f"responses={result.manifest.response_count}/240 "
        f"complete={str(result.manifest.complete).lower()} "
        f"manifest={result.manifest_path} "
        "adjudications_created=0 promoted_labels_created=0 gate_5_closed=false"
    )


@app.command("write-annotation-agreement")
def write_annotation_agreement_command(
    first_import_manifest_path: Annotated[
        Path,
        typer.Option("--first-import-manifest", help="First authenticated import manifest."),
    ],
    second_import_manifest_path: Annotated[
        Path,
        typer.Option("--second-import-manifest", help="Second authenticated import manifest."),
    ],
    authentication_key_path: Annotated[
        Path,
        typer.Option("--authentication-key", help="Mode-0600 LF-023 HMAC key."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="New mode-0600 immutable agreement artifact."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Reauthenticate two complete raw imports and write agreement statistics."""
    from leanfaith.annotation_support import (
        AnnotationAgreementError,
        AnnotationImportError,
        AnnotationOperationError,
    )
    from leanfaith.cli.annotation_operations import run_write_annotation_agreement
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_write_annotation_agreement(
            paths=paths,
            first_import_manifest_path=anchored(first_import_manifest_path),
            second_import_manifest_path=anchored(second_import_manifest_path),
            authentication_key_path=anchored(authentication_key_path),
            output_path=anchored(output_path),
        )
    except (
        AnnotationAgreementError,
        AnnotationImportError,
        AnnotationOperationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"Annotation agreement rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"report_id={result.artifact.report.report_id} path={result.path} "
        "raw_annotations_only=true resolved_labels_created=0"
    )


@app.command("write-adjudication-queue")
def write_adjudication_queue_command(
    first_import_manifest_path: Annotated[
        Path,
        typer.Option("--first-import-manifest", help="First authenticated import manifest."),
    ],
    second_import_manifest_path: Annotated[
        Path,
        typer.Option("--second-import-manifest", help="Second authenticated import manifest."),
    ],
    authentication_key_path: Annotated[
        Path,
        typer.Option("--authentication-key", help="Mode-0600 LF-023 HMAC key."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="New mode-0600 immutable routing artifact."),
    ],
    policy_trigger_set_path: Annotated[
        Path | None,
        typer.Option(
            "--policy-trigger-set",
            help="Optional mode-0600 canonical versioned trigger-set JSON.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Route raw disagreements to future humans; never adjudicate automatically."""
    from leanfaith.annotation_support import (
        AdjudicationRoutingError,
        AnnotationImportError,
        AnnotationOperationError,
    )
    from leanfaith.cli.annotation_operations import run_write_adjudication_queue
    from leanfaith.config.paths import RepoPaths

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_write_adjudication_queue(
            paths=paths,
            first_import_manifest_path=anchored(first_import_manifest_path),
            second_import_manifest_path=anchored(second_import_manifest_path),
            authentication_key_path=anchored(authentication_key_path),
            output_path=anchored(output_path),
            policy_trigger_set_path=(
                None if policy_trigger_set_path is None else anchored(policy_trigger_set_path)
            ),
        )
    except (
        AdjudicationRoutingError,
        AnnotationImportError,
        AnnotationOperationError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"Adjudication routing rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"queue_id={result.artifact.queue.queue_id} path={result.path} "
        f"routed={result.artifact.queue.routed_target_count} "
        "semantic_labels_created=0 adjudications_created=0"
    )


@app.command("validate-lf022")
def validate_lf022_command(
    variants_config_path: Annotated[
        Path,
        typer.Option(
            "--variants-config",
            help="Fail-closed LF-022 proposer foundation config.",
        ),
    ] = Path("configs/generation/llm_variants_v1.yaml"),
    judges_config_path: Annotated[
        Path,
        typer.Option(
            "--judges-config",
            help="Fail-closed LF-022 weak-supervision foundation config.",
        ),
    ] = Path("configs/judges/weak_supervision.yaml"),
    replay_kind: Annotated[
        str | None,
        typer.Option(
            "--replay-kind",
            help="Optional strict parser replay kind: proposer or judge.",
        ),
    ] = None,
    request_path: Annotated[
        Path | None,
        typer.Option(
            "--request",
            help="Canonical persisted ProviderRequest for a network-free replay.",
        ),
    ] = None,
    raw_response_root: Annotated[
        Path | None,
        typer.Option(
            "--raw-response-root",
            help="Immutable provider raw-response root matching --request.",
        ),
    ] = None,
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            help="Validation report path.",
        ),
    ] = Path("reports/milestones/lf022_foundation_validation.json"),
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Validate LF-022 configs or replay one response; never call a provider."""
    from leanfaith.cli.lf022 import run_lf022_validation
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_config import ReplayKind
    from leanfaith.generation.providers import ProviderError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    if replay_kind not in {None, "proposer", "judge"}:
        typer.echo("--replay-kind must be proposer or judge", err=True)
        raise typer.Exit(code=2)
    typed_replay_kind: ReplayKind | None
    if replay_kind == "proposer":
        typed_replay_kind = "proposer"
    elif replay_kind == "judge":
        typed_replay_kind = "judge"
    else:
        typed_replay_kind = None
    try:
        result = run_lf022_validation(
            paths=paths,
            variants_config_path=anchored(variants_config_path) or variants_config_path,
            judges_config_path=anchored(judges_config_path) or judges_config_path,
            report_path=anchored(report_path) or report_path,
            replay_kind=typed_replay_kind,
            request_path=anchored(request_path),
            raw_response_root=anchored(raw_response_root),
        )
    except (OSError, ProviderError, ValueError) as exc:
        typer.echo(f"LF-022 validation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    replay = result.report.replay
    replay_summary = (
        "replay=none"
        if replay is None
        else (
            f"replay={replay.replay_kind} "
            f"parsed_items={replay.parsed_item_count} "
            f"raw_sha256={replay.raw_response_sha256}"
        )
    )
    typer.echo(
        f"LF-022 foundation validated: report={result.report_path} "
        f"sha256={result.report_sha256} {replay_summary} "
        "live_calls=0 semantic_labels_created=0 silver_records_created=0"
    )


@app.command("freeze-lf022-family-matrix")
def freeze_lf022_family_matrix_command(
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Offline LF-022 production-family freeze configuration.",
        ),
    ] = Path("configs/generation/lf022_production_family_matrix_freeze_v1.yaml"),
    write: Annotated[
        bool,
        typer.Option(
            "--write",
            help="Immutably create the configured outputs; default is replay verification.",
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze or replay provider identities without calls, labels, or execution."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_family_matrix_freeze import (
        LF022FamilyMatrixFreezeError,
        verify_lf022_family_matrix_freeze,
        write_lf022_family_matrix_freeze,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    config = config_path if config_path.is_absolute() else paths.root / config_path
    try:
        bundle = (
            write_lf022_family_matrix_freeze(repo_root=paths.root, config_path=config)
            if write
            else verify_lf022_family_matrix_freeze(
                repo_root=paths.root,
                config_path=config,
            )
        )
    except (LF022FamilyMatrixFreezeError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 family-matrix freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status={bundle.report.status} matrix_id={bundle.family_matrix.matrix_id} "
        "network_requests=0 semantic_labels_created=0 training_eligible=false"
    )


@app.command("freeze-lf022-extraction-reuse-attestation")
def freeze_lf022_extraction_reuse_attestation_command(
    extraction_output_manifest: Annotated[
        Path,
        typer.Option("--extraction-manifest"),
    ],
    theorem_records: Annotated[Path, typer.Option("--theorems")],
    context_records: Annotated[Path, typer.Option("--contexts")],
    mathlib_source_frame: Annotated[Path, typer.Option("--mathlib-source-frame")],
    representation_output_manifest: Annotated[
        Path,
        typer.Option("--representation-manifest"),
    ],
    representation_records: Annotated[Path, typer.Option("--representations")],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Repository-local immutable attestation JSON.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze the one reviewed LF-022 extraction-reuse exception."""
    from leanfaith.config.hashing import hash_file
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_extraction_reuse import (
        LF022ExtractionReuseError,
        freeze_lf022_extraction_reuse_attestation,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        attestation = freeze_lf022_extraction_reuse_attestation(
            repo_root=paths.root,
            extraction_manifest_path=extraction_output_manifest,
            theorem_records_path=theorem_records,
            context_records_path=context_records,
            mathlib_source_frame_path=mathlib_source_frame,
            representation_manifest_path=representation_output_manifest,
            representation_records_path=representation_records,
            output_path=output,
        )
        output_path = output if output.is_absolute() else paths.root / output
        digest = hash_file(output_path.resolve(strict=True))
    except (LF022ExtractionReuseError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 extraction-reuse attestation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status=frozen attestation_id={attestation.attestation_id} sha256={digest} "
        "network_requests=0 semantic_labels_created=0 gate_credit=false"
    )


@app.command("materialize-lf022-public-pool")
def materialize_lf022_public_pool_command(
    theorem_records: Annotated[Path, typer.Option("--theorems")],
    representation_records: Annotated[Path, typer.Option("--representations")],
    context_records: Annotated[Path, typer.Option("--contexts")],
    extraction_output_manifest: Annotated[
        Path,
        typer.Option(
            "--extraction-manifest",
            help="Exact upstream extraction OutputManifest JSON.",
        ),
    ],
    representation_output_manifest: Annotated[
        Path,
        typer.Option(
            "--representation-manifest",
            help="Exact upstream representation OutputManifest JSON.",
        ),
    ],
    mathlib_source_frame: Annotated[
        Path,
        typer.Option(
            "--mathlib-source-frame",
            help="Exact canonical MathlibFileFrame JSON used by extraction.",
        ),
    ],
    active_registry: Annotated[
        Path,
        typer.Option("--active-registry", help="Frozen active benchmark registry JSON."),
    ],
    family_matrix: Annotated[
        Path,
        typer.Option("--family-matrix", help="Frozen LF-022 family-matrix JSON."),
    ] = Path("configs/generation/lf022_production_family_matrix_v1.json"),
    approved_sources: Annotated[
        Path,
        typer.Option("--approved-sources", help="Reviewed public-source authorization YAML/JSON."),
    ] = Path("configs/sources/lf022_public_sources_v1.yaml"),
    extraction_reuse_attestation: Annotated[
        Path | None,
        typer.Option(
            "--extraction-reuse-attestation",
            help=(
                "Optional exact reviewed attestation for the one approved "
                "old-extraction/new-representation provenance mismatch."
            ),
        ),
    ] = None,
    output_directory: Annotated[
        Path,
        typer.Option("--out-dir", help="Repository-local immutable output directory."),
    ] = Path("artifacts/generation/lf022_public_pool_v1"),
    requested_count: Annotated[
        int,
        typer.Option("--requested-count", min=1),
    ] = 15_000,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help=("diagnostic_scaffold, pilot_scaffold, or scientific_production_scaffold."),
        ),
    ] = "scientific_production_scaffold",
    diagnostic_proposer_family_id: Annotated[
        str | None,
        typer.Option(
            "--diagnostic-proposer-family",
            help=(
                "For a one-source diagnostic scaffold only, assign both tasks "
                "to moonshot_kimi_k2, qwen3, or glm5."
            ),
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Materialize a public, denylist-cleared, non-executable LF-022 pool."""
    from typing import cast

    from leanfaith.cli.lf022_public_pool_operations import (
        LF022PublicPoolOperationError,
        run_materialize_lf022_public_pool,
    )
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_production import LF022PlanProfile

    allowed_profiles = {
        "diagnostic_scaffold",
        "pilot_scaffold",
        "scientific_production_scaffold",
    }
    if profile not in allowed_profiles:
        typer.echo("--profile is not a canonical LF-022 plan profile", err=True)
        raise typer.Exit(code=2)
    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        result = run_materialize_lf022_public_pool(
            paths=paths,
            theorem_records_path=theorem_records,
            representation_records_path=representation_records,
            context_records_path=context_records,
            extraction_output_manifest_path=extraction_output_manifest,
            representation_output_manifest_path=representation_output_manifest,
            mathlib_source_frame_path=mathlib_source_frame,
            active_registry_path=active_registry,
            family_matrix_path=family_matrix,
            approved_sources_path=approved_sources,
            output_directory=output_directory,
            requested_count=requested_count,
            profile=cast(LF022PlanProfile, profile),
            diagnostic_proposer_family_id=diagnostic_proposer_family_id,
            extraction_reuse_attestation_path=extraction_reuse_attestation,
        )
    except LF022PublicPoolOperationError as exc:
        typer.echo(exc.failure.model_dump_json(), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(result.summary.model_dump_json())


@app.command("derive-lf022-diagnostic-subpool")
def derive_lf022_diagnostic_subpool_command(
    parent_pool_audit: Annotated[
        Path,
        typer.Option(
            "--parent-pool-audit",
            help="Exact immutable public-pool audit to subset.",
        ),
    ],
    proposer_family: Annotated[
        str,
        typer.Option("--proposer-family", help="qwen3 or glm5."),
    ],
    output_directory: Annotated[
        Path,
        typer.Option("--out-dir", help="Repository-local immutable output directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Derive one repr_v3 diagnostic source from an exact public pool offline."""
    from typing import cast

    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_diagnostic_subpool import (
        LF022DiagnosticProposerFamily,
        LF022DiagnosticSubpoolError,
        derive_lf022_diagnostic_subpool,
    )

    if proposer_family not in {"qwen3", "glm5"}:
        typer.echo("--proposer-family must be qwen3 or glm5", err=True)
        raise typer.Exit(code=2)
    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        result = derive_lf022_diagnostic_subpool(
            repo_root=paths.root,
            parent_pool_audit_path=parent_pool_audit,
            proposer_family_id=cast(LF022DiagnosticProposerFamily, proposer_family),
            output_directory=output_directory,
        )
    except (LF022DiagnosticSubpoolError, OSError, ValueError) as exc:
        typer.echo(
            json.dumps(
                {
                    "status": "error",
                    "operation": "derive_lf022_diagnostic_subpool",
                    "message": str(exc),
                    "network_execution_authorized": False,
                    "semantic_labels_created": False,
                    "training_eligible": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            err=True,
        )
        raise typer.Exit(code=2) from exc
    audit = result.materialized.audit
    typer.echo(
        json.dumps(
            {
                "status": "derived",
                "operation": "derive_lf022_diagnostic_subpool",
                "proposer_family_id": proposer_family,
                "parent_pool_audit_id": result.derivation.parent_pool_audit_id,
                "derivation_id": result.derivation.derivation_id,
                "audit_id": audit.audit_id,
                "plan_id": result.materialized.plan.manifest_id,
                "selected_count": audit.selected_count,
                "task_count": len(result.materialized.plan.tasks),
                "audit": result.materialized.audit_binding.model_dump(mode="json"),
                "network_execution_authorized": False,
                "outputs_unresolved": True,
                "semantic_labels_created": False,
                "training_eligible": False,
                "evaluation_eligible": False,
                "gate_credit_claimed": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@app.command("lf022-rcp-smoke")
def lf022_rcp_smoke_command(
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Exact public-only LF-022 RCP smoke admission.",
        ),
    ] = Path("configs/generation/lf022_rcp_public_smoke_v1.yaml"),
    execute_public_smoke: Annotated[
        bool,
        typer.Option(
            "--execute-public-smoke",
            help="Explicitly permit the capped one-proposer/four-judge live smoke.",
        ),
    ] = False,
    replay_manifest: Annotated[
        Path | None,
        typer.Option(
            "--replay-manifest",
            help="Verify one completed smoke locally; performs no network requests.",
        ),
    ] = None,
    replay_failure_manifest: Annotated[
        Path | None,
        typer.Option(
            "--replay-failure-manifest",
            help=("Verify one terminal partial smoke locally; performs no network requests."),
        ),
    ] = None,
    mathlib_project_dir: Annotated[
        Path | None,
        typer.Option(
            "--mathlib-project-dir",
            help="Pinned mathlib project used only for explicit live candidate validation.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Preflight by default; live inference requires an explicit smoke flag."""
    from leanfaith.cli.lf022_rcp_smoke import run_lf022_rcp_smoke
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_rcp_smoke_v1 import (
        LF022RCPSmokeError,
        LF022RCPSmokeFailureManifest,
        LF022RCPSmokeManifest,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = run_lf022_rcp_smoke(
            paths=paths,
            config_path=anchored(config_path) or config_path,
            execute_public_smoke_flag=execute_public_smoke,
            replay_manifest_path=anchored(replay_manifest),
            replay_failure_manifest_path=anchored(replay_failure_manifest),
            mathlib_project_dir=anchored(mathlib_project_dir),
        )
    except (LF022RCPSmokeError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 RCP smoke rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    network_calls_this_run = 6 if result.mode == "live" else 1 if result.mode == "preflight" else 0
    if isinstance(result.manifest, LF022RCPSmokeFailureManifest):
        persisted_chat_completion_attempts = result.manifest.chat_completion_attempts
    elif isinstance(result.manifest, LF022RCPSmokeManifest):
        persisted_chat_completion_attempts = (
            result.manifest.proposer_call_count + result.manifest.judge_call_count
        )
    else:
        persisted_chat_completion_attempts = 0
    typer.echo(
        f"LF-022 RCP smoke {result.mode}: artifact={result.artifact_path} "
        f"network_calls_this_run={network_calls_this_run} "
        f"persisted_chat_completion_attempts={persisted_chat_completion_attempts} "
        "semantic_labels_created=0 "
        "silver_records_created=0 supervision_eligible=false "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("run-lf022-public-provisional")
def run_lf022_public_provisional_command(
    admission_path: Annotated[
        Path,
        typer.Option(
            "--admission",
            help="Content-addressed public G_open execution admission JSON.",
        ),
    ],
    task_path: Annotated[
        Path,
        typer.Option(
            "--task",
            help="One allocation-bound public G_open execution task JSON.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Repository-local resumable task root.",
        ),
    ],
    execute_public_provisional: Annotated[
        bool,
        typer.Option(
            "--execute-public-provisional",
            help="Explicitly permit public proposer inference; omitted means offline preflight.",
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Preflight by default; collect only provisional G_open candidates when explicit."""
    from leanfaith.cli.lf022_executor import run_lf022_public_provisional
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_executor import LF022ExecutorError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_lf022_public_provisional(
            repo_root=paths.root,
            admission_path=anchored(admission_path),
            task_path=anchored(task_path),
            output_root=anchored(output_root),
            execute_public_provisional=execute_public_provisional,
        )
    except (LF022ExecutorError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 public provisional execution rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    mode = "replay" if result.replayed else "live" if result.terminal is not None else "preflight"
    typer.echo(
        f"mode={mode} preflight_id={result.preflight.preflight_id} "
        f"terminal_id={result.terminal.terminal_id if result.terminal else 'none'} "
        f"network_calls_this_run={result.network_calls_this_run} "
        f"provisional_variants="
        f"{result.terminal.provisional_variant_count if result.terminal else 0} "
        "semantic_labels_created=0 silver_records_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("certify-lf022-proposer-route")
def certify_lf022_proposer_route_command(
    qualification_admission_path: Annotated[
        Path,
        typer.Option(
            "--qualification-admission",
            help="Frozen one-item Qwen/GLM qualification admission JSON.",
        ),
    ],
    qualification_task_path: Annotated[
        Path,
        typer.Option(
            "--qualification-task",
            help="Frozen one-item Qwen/GLM qualification task JSON.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Replay completed live qualification and certify only route eligibility."""
    from leanfaith.cli.lf022_route_qualification import certify_proposer_route
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_route_qualification import (
        LF022RouteQualificationError,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = certify_proposer_route(
            repo_root=paths.root,
            qualification_admission_path=anchored(qualification_admission_path),
            qualification_task_path=anchored(qualification_task_path),
        )
    except (LF022RouteQualificationError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 proposer-route certification rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"family={result.eligibility.proposer_family_id} "
        f"model={result.eligibility.model_id} "
        f"eligibility_id={result.eligibility.eligibility_id} "
        f"eligibility={result.eligibility_path} network_calls_this_run=0 "
        "candidate_quality=provisional outputs_unresolved=true semantic_labels_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("check-lf022-provisional-lean")
def check_lf022_provisional_lean_command(
    project: Annotated[
        list[str],
        typer.Option(
            "--project",
            help="Repeat SOURCE_ID=PROJECT_DIR for every source present in the input.",
        ),
    ],
    input_root: Annotated[
        Path,
        typer.Option(
            "--input-root",
            help="Resumable LF-022 execution root containing tasks/*/*.",
        ),
    ] = Path("data/lf022_execution"),
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Separate resumable output root; source variants remain immutable.",
        ),
    ] = Path("data/lf022_lean_checks/v1"),
    workers: Annotated[
        int,
        typer.Option("--workers", min=1, help="Explicit LeanServerPool worker count."),
    ] = 4,
    chunk_size: Annotated[
        int,
        typer.Option("--chunk-size", min=1, help="Maximum independent requests per pool batch."),
    ] = 64,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0.001, help="Per-candidate Lean timeout."),
    ] = 120.0,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            min=1,
            help="Bounded attempts; only crash/internal-error/timeout outcomes retry.",
        ),
    ] = 2,
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option(
            "--memory-hard-limit-mb",
            min=1,
            help="Optional per-REPL Linux hard limit; omitted leaves LeanInteract default.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Deterministic prefix limit for a smoke run."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Lean-check provisional LF-022 variants with a resumable worker pool."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_lean_check import (
        LF022LeanCheckError,
        check_lf022_provisional_candidates,
        parse_project_mappings,
    )
    from leanfaith.lean.project_registry import load_environment_lock

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        mappings = parse_project_mappings(project, repo_root=paths.root)
        result = check_lf022_provisional_candidates(
            repo_root=paths.root,
            input_root=anchored(input_root),
            output_root=anchored(output_root),
            project_dirs=mappings,
            workers=workers,
            chunk_size=chunk_size,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            memory_hard_limit_mb=memory_hard_limit_mb,
            environment_schema_version=load_environment_lock(paths).environment_schema_version,
            limit=limit,
        )
    except (LF022LeanCheckError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 pooled Lean check rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"checked={result.manifest.record_count} executed={result.executed_count} "
        f"reused={result.reused_count} workers={workers} "
        f"outcomes={json.dumps(result.manifest.outcome_counts, sort_keys=True)} "
        f"manifest={result.manifest_path} semantic_labels_created=0 "
        "silver_records_created=0 training_eligible=false evaluation_eligible=false"
    )


@app.command("audit-lf022-codex")
def audit_lf022_codex_command(
    checks_path: Annotated[
        Path,
        typer.Option(
            "--checks",
            help="Canonical checks.jsonl from check-lf022-provisional-lean.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Separate immutable/resumable audit-only artifact root.",
        ),
    ] = Path("data/lf022_codex_audit/v1"),
    model: Annotated[
        str,
        typer.Option("--model", help="Codex judge model; defaults to gpt-5.6-sol."),
    ] = "gpt-5.6-sol",
    reasoning_effort: Annotated[
        str,
        typer.Option("--reasoning-effort", help="Codex reasoning effort."),
    ] = "xhigh",
    timeout_seconds: Annotated[
        int,
        typer.Option("--timeout-seconds", min=1, help="Timeout per one-pair invocation."),
    ] = 1800,
    termination_grace_seconds: Annotated[
        int,
        typer.Option(
            "--termination-grace-seconds",
            min=1,
            help="Grace period before a timed-out Codex process is killed.",
        ),
    ] = 10,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            min=1,
            help="Maximum immutable attempts per pair across resumptions.",
        ),
    ] = 2,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Audit only the first N Lean-valid pairs."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Judge Lean-valid public LF-022 pairs sequentially for audit only."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_codex_audit import (
        LF022CodexAuditError,
        audit_lean_valid_lf022_pairs,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = audit_lean_valid_lf022_pairs(
            repo_root=paths.root,
            checks_path=anchored(checks_path),
            output_root=anchored(output_root),
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            max_attempts=max_attempts,
            limit=limit,
        )
    except (LF022CodexAuditError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 Codex audit rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    manifest = result.manifest
    typer.echo(
        f"eligible={manifest.eligible_count} completed={manifest.completed_count} "
        f"invoked={manifest.invoked_count} reused={manifest.reused_count} "
        f"exhausted={manifest.exhausted_count} statuses="
        f"{json.dumps(manifest.attempt_status_counts, sort_keys=True)} "
        f"manifest={result.manifest_path} audit_only=true semantic_labels_created=0 "
        "silver_records_created=0 training_eligible=false evaluation_eligible=false "
        "gate_credit_claimed=false"
    )


@app.command("freeze-lf022-proposer-admission")
def freeze_lf022_proposer_admission_command(
    public_pool_audit_path: Annotated[
        Path,
        typer.Option(
            "--public-pool-audit",
            help="One-source family-specific diagnostic public-pool audit JSON.",
        ),
    ],
    proposer_family_id: Annotated[
        str,
        typer.Option(
            "--proposer-family",
            help="qwen3 or glm5; the Kimi-v3 route is archived.",
        ),
    ],
    code_bundle_path: Annotated[
        Path,
        typer.Option(
            "--code-bundle",
            help="Repository-local code bundle for the current exact code tree.",
        ),
    ],
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Repository-local immutable execution admission JSON.",
        ),
    ],
    provider_catalog_raw_path: Annotated[
        Path | None,
        typer.Option(
            "--provider-catalog-raw",
            help="Exact raw RCP catalog response; defaults to reviewed v3 smoke evidence.",
        ),
    ] = None,
    qualification_supersession_path: Annotated[
        Path | None,
        typer.Option(
            "--qualification-supersession",
            help="Replay-verified failed-v1 supersession authorizing a v2 qualification.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze and exact-replay one public diagnostic proposer admission offline."""
    from typing import cast

    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_admission_freeze import (
        LF022AdmissionFreezeError,
        LF022SupportedProposerFamily,
        freeze_lf022_diagnostic_execution_admission,
    )

    if proposer_family_id == "moonshot_kimi_k2":
        typer.echo(
            "Kimi-v3 admission is archived after the failed prefix-256 audit",
            err=True,
        )
        raise typer.Exit(code=2)
    allowed_families = {"qwen3", "glm5"}
    if proposer_family_id not in allowed_families:
        typer.echo("--proposer-family is not a supported public LF-022 proposer", err=True)
        raise typer.Exit(code=2)
    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = freeze_lf022_diagnostic_execution_admission(
            repo_root=paths.root,
            public_pool_audit_path=anchored(public_pool_audit_path) or public_pool_audit_path,
            proposer_family_id=cast(
                LF022SupportedProposerFamily,
                proposer_family_id,
            ),
            code_bundle_path=anchored(code_bundle_path) or code_bundle_path,
            output_path=anchored(output_path) or output_path,
            provider_catalog_raw_path=anchored(provider_catalog_raw_path),
            qualification_supersession_path=anchored(qualification_supersession_path),
        )
    except (LF022AdmissionFreezeError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 proposer admission freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status=frozen family={result.admission.route.proposer_family_id} "
        f"model={result.admission.route.model_id} "
        f"scope={result.admission.route.execution_scope} "
        f"admission_id={result.admission.admission_id} "
        f"admission={result.admission_path} network_calls_this_run=0 "
        "outputs_unresolved=true semantic_labels_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("freeze-lf022-scientific-kimi-admission")
def freeze_lf022_scientific_kimi_admission_command(
    public_pool_audit_path: Annotated[
        Path,
        typer.Option(
            "--public-pool-audit",
            help="Scientific production public-pool audit JSON.",
        ),
    ],
    code_bundle_path: Annotated[
        Path,
        typer.Option(
            "--code-bundle",
            help="Repository-local code bundle for the current exact code tree.",
        ),
    ],
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Repository-local immutable Kimi execution admission JSON.",
        ),
    ],
    provider_catalog_raw_path: Annotated[
        Path | None,
        typer.Option(
            "--provider-catalog-raw",
            help="Exact raw RCP catalog response; defaults to reviewed v3 smoke evidence.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Reject the archived Kimi-v3 scientific admission path."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_admission_freeze import (
        LF022AdmissionFreezeError,
        freeze_lf022_scientific_kimi_execution_admission,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = freeze_lf022_scientific_kimi_execution_admission(
            repo_root=paths.root,
            public_pool_audit_path=anchored(public_pool_audit_path) or public_pool_audit_path,
            code_bundle_path=anchored(code_bundle_path) or code_bundle_path,
            output_path=anchored(output_path) or output_path,
            provider_catalog_raw_path=anchored(provider_catalog_raw_path),
        )
    except (LF022AdmissionFreezeError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 scientific Kimi admission freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status=frozen family={result.admission.route.proposer_family_id} "
        f"model={result.admission.route.model_id} "
        f"scope={result.admission.route.execution_scope} "
        f"admission_id={result.admission.admission_id} "
        f"admission={result.admission_path} network_calls_this_run=0 "
        "outputs_unresolved=true semantic_labels_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("freeze-lf022-scientific-qualified-admission")
def freeze_lf022_scientific_qualified_admission_command(
    public_pool_audit_path: Annotated[
        Path,
        typer.Option(
            "--public-pool-audit",
            help="Scientific production public-pool audit JSON.",
        ),
    ],
    proposer_family_id: Annotated[
        str,
        typer.Option(
            "--proposer-family",
            help="Replay-qualified scientific proposer: qwen3 or glm5.",
        ),
    ],
    proposer_production_eligibility_path: Annotated[
        Path,
        typer.Option(
            "--proposer-production-eligibility",
            help="Canonical replay-verified v2 proposer eligibility JSON.",
        ),
    ],
    code_bundle_path: Annotated[
        Path,
        typer.Option(
            "--code-bundle",
            help="Repository-local code bundle for the current exact code tree.",
        ),
    ],
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Repository-local immutable scientific execution admission JSON.",
        ),
    ],
    provider_catalog_raw_path: Annotated[
        Path | None,
        typer.Option(
            "--provider-catalog-raw",
            help="Exact raw RCP catalog response; defaults to reviewed v3 smoke evidence.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze a replay-qualified Qwen/GLM route over the scientific public pool."""
    from typing import Literal, cast

    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_admission_freeze import (
        LF022AdmissionFreezeError,
        freeze_lf022_scientific_qualified_execution_admission,
    )

    if proposer_family_id not in {"qwen3", "glm5"}:
        typer.echo("--proposer-family must be qwen3 or glm5", err=True)
        raise typer.Exit(code=2)
    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path | None) -> Path | None:
        if path is None or path.is_absolute():
            return path
        return paths.root / path

    try:
        result = freeze_lf022_scientific_qualified_execution_admission(
            repo_root=paths.root,
            public_pool_audit_path=anchored(public_pool_audit_path) or public_pool_audit_path,
            proposer_family_id=cast(Literal["qwen3", "glm5"], proposer_family_id),
            proposer_production_eligibility_path=(
                anchored(proposer_production_eligibility_path)
                or proposer_production_eligibility_path
            ),
            code_bundle_path=anchored(code_bundle_path) or code_bundle_path,
            output_path=anchored(output_path) or output_path,
            provider_catalog_raw_path=anchored(provider_catalog_raw_path),
        )
    except (LF022AdmissionFreezeError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 scientific qualified admission freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status=frozen family={result.admission.route.proposer_family_id} "
        f"model={result.admission.route.model_id} "
        f"scope={result.admission.route.execution_scope} "
        f"admission_id={result.admission.admission_id} "
        f"admission={result.admission_path} network_calls_this_run=0 "
        "qualification_replay_verified=true outputs_unresolved=true "
        "semantic_labels_created=0 training_eligible=false evaluation_eligible=false "
        "gate_credit_claimed=false"
    )


@app.command("supersede-lf022-failed-qualification")
def supersede_lf022_failed_qualification_command(
    qualification_admission_path: Annotated[
        Path,
        typer.Option("--qualification-admission", help="Failed v1 qualification admission."),
    ],
    qualification_task_path: Annotated[
        Path,
        typer.Option("--qualification-task", help="Failed v1 qualification task."),
    ],
    next_decoding_contract_id: Annotated[
        str,
        typer.Option("--next-contract", help="Reviewed family-matched v2 recovery contract."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Replay a failed terminal and append one immutable retry authorization."""
    from leanfaith.cli.lf022_route_qualification import supersede_failed_qualification
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_route_qualification import (
        LF022RouteQualificationError,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = supersede_failed_qualification(
            repo_root=paths.root,
            qualification_admission_path=anchored(qualification_admission_path),
            qualification_task_path=anchored(qualification_task_path),
            next_decoding_contract_id=next_decoding_contract_id,
        )
    except (LF022RouteQualificationError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 qualification supersession rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status=failed_qualification_superseded "
        f"family={result.supersession.proposer_family_id} "
        f"previous_terminal={result.supersession.previous_terminal_status} "
        f"next_contract={result.supersession.next_decoding_contract_id} "
        f"supersession_id={result.supersession.supersession_id} "
        f"supersession={result.supersession_path} network_calls_this_run=0"
    )


@app.command("make-lf022-public-batch-request")
def make_lf022_public_batch_request_command(
    admission_path: Annotated[
        Path,
        typer.Option(
            "--admission",
            help="Canonical reviewed LF-022 execution admission JSON.",
        ),
    ],
    allocation_task_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--allocation-task-id",
            help="Allocation task ID to include; repeat in sorted order.",
        ),
    ] = None,
    allocation_offset: Annotated[
        int | None,
        typer.Option(
            "--allocation-offset",
            min=0,
            help="Zero-based offset in the admitted route's exact G_open plan order.",
        ),
    ] = None,
    allocation_limit: Annotated[
        int | None,
        typer.Option(
            "--allocation-limit",
            min=1,
            help="Exact number of consecutive G_open plan-order tasks to select.",
        ),
    ] = None,
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Repository-local immutable request JSON to create.",
        ),
    ] = Path("data/lf022_batch_request.json"),
    batch_directory: Annotated[
        str,
        typer.Option(
            "--batch-directory",
            help="Repository-relative directory for the later frozen batch.",
        ),
    ] = "data/lf022_batch",
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Verify one route and create a public-only batch request offline."""
    from leanfaith.cli.lf022_batch import create_public_batch_request
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_batch import LF022BatchError
    from leanfaith.generation.lf022_execution import LF022ExecutionError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = create_public_batch_request(
            repo_root=paths.root,
            admission_path=anchored(admission_path),
            allocation_task_ids=tuple(allocation_task_ids or ()),
            output_path=anchored(output_path),
            batch_directory=batch_directory,
            executor_output_root="data/lf022_execution",
            allocation_offset=allocation_offset,
            allocation_limit=allocation_limit,
        )
    except (LF022BatchError, LF022ExecutionError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 batch request creation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"request_id={result.request.request_id} "
        f"family={result.request.routes[0].proposer_family_id} "
        f"tasks={len(result.request.routes[0].allocation_task_ids)} "
        f"request={result.request_path} network_calls_this_run=0 "
        "semantic_labels_created=0 training_eligible=false "
        "evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("freeze-lf022-public-batch")
def freeze_lf022_public_batch_command(
    request_path: Annotated[
        Path,
        typer.Option(
            "--request",
            help="Content-addressed public batch freeze request JSON.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Replay reviewed admissions and freeze public-only LF-022 task JSON files."""
    from leanfaith.cli.lf022_batch import freeze_public_batch
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_batch import LF022BatchError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    anchored_request = request_path if request_path.is_absolute() else paths.root / request_path
    try:
        result = freeze_public_batch(
            repo_root=paths.root,
            request_path=anchored_request,
        )
    except (LF022BatchError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 public batch freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"batch_id={result.manifest.batch_id} "
        f"tasks={result.manifest.total_task_count} "
        f"manifest={result.manifest_path} "
        "network_calls_this_run=0 semantic_labels_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("run-lf022-public-batch")
def run_lf022_public_batch_command(
    manifest_path: Annotated[
        Path,
        typer.Option(
            "--manifest",
            help="Frozen public batch manifest JSON.",
        ),
    ],
    max_concurrency: Annotated[
        int,
        typer.Option(
            "--max-concurrency",
            min=1,
            max=8,
            help="Maximum concurrent task executions.",
        ),
    ] = 1,
    minimum_request_interval_seconds: Annotated[
        float,
        typer.Option(
            "--minimum-request-interval-seconds",
            min=0.0,
            max=300.0,
            help="Minimum start interval shared by all provider requests and retries.",
        ),
    ] = 0.0,
    execute_public_provisional: Annotated[
        bool,
        typer.Option(
            "--execute-public-provisional",
            help="Explicitly permit public provisional RCP calls; omitted is offline.",
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Preflight/replay a batch offline, or explicitly collect provisional candidates."""
    from leanfaith.cli.lf022_batch import run_public_batch
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_batch import LF022BatchError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    anchored_manifest = manifest_path if manifest_path.is_absolute() else paths.root / manifest_path
    try:
        result = run_public_batch(
            repo_root=paths.root,
            manifest_path=anchored_manifest,
            max_concurrency=max_concurrency,
            minimum_request_interval_seconds=minimum_request_interval_seconds,
            execute_public_provisional=execute_public_provisional,
        )
    except (LF022BatchError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 public batch run rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    report = result.report
    typer.echo(
        f"mode={report.mode} tasks={report.task_count} "
        f"preflight_only={report.preflight_only_count} "
        f"replayed_terminal={report.replayed_terminal_count} "
        f"new_terminal={report.new_terminal_count} "
        f"successful_terminal={report.successful_terminal_count or 0} "
        f"failed_terminal={report.failed_terminal_count or 0} "
        f"errors={report.error_count} "
        f"network_calls_this_run={report.network_calls_this_run} "
        f"report={result.report_path} semantic_labels_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )
    if report.error_count:
        raise typer.Exit(code=2)


@app.command("qa-lf022-prefix256")
def qa_lf022_prefix256_command(
    manifest_path: Annotated[
        Path,
        typer.Option(
            "--manifest",
            help="Frozen 256-task LF-022 public batch manifest JSON.",
        ),
    ],
    exact_offline_replay_report_path: Annotated[
        Path,
        typer.Option(
            "--offline-replay-report",
            help="Canonical report from the complete exact offline replay.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Repository-local immutable QA report and reviewer-bundle directory.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run fail-closed operational QA for one completed LF-022 prefix-256 batch."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_prefix256_qa import (
        LF022Prefix256QAError,
        run_lf022_prefix256_operational_qa,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_lf022_prefix256_operational_qa(
            repo_root=paths.root,
            manifest_path=anchored(manifest_path),
            exact_offline_replay_report_path=anchored(exact_offline_replay_report_path),
            output_dir=anchored(output_dir),
        )
    except (LF022Prefix256QAError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 prefix-256 operational QA rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    report = result.report
    typer.echo(
        f"qa_status={report.qa_status} tasks={report.task_count} "
        f"offline_replay={report.offline_replay_count} "
        f"successful_terminal={report.successful_terminal_count} "
        f"failed_terminal={report.failed_terminal_count} "
        f"network_calls_this_replay={report.network_calls_this_replay} "
        f"errors={report.orchestration_error_count} "
        f"review_sample={len(report.selected_task_ids)} "
        f"report={result.report_path} reviewer_bundle={result.reviewer_bundle_path} "
        "semantic_labels_created=0 training_eligible=false "
        "evaluation_eligible=false gate_credit_claimed=false"
    )
    if report.qa_status != "passed":
        raise typer.Exit(code=2)


@app.command("freeze-lf022-kimi-v4-challenge")
def freeze_lf022_kimi_v4_challenge_command(
    current_code_bundle: Annotated[
        Path,
        typer.Option(
            "--current-code-bundle",
            help="Validated full bundle of the clean current selection code tree.",
        ),
    ],
    v3_manifest: Annotated[
        Path,
        typer.Option("--v3-manifest", help="Exact immutable Kimi-v3 prefix-256 manifest."),
    ] = Path("data/lf022_kimi_scientific_cfdbb46/prefix_256/batch/batch_manifest.json"),
    exact_offline_replay_report: Annotated[
        Path,
        typer.Option(
            "--exact-offline-replay-report",
            help="Exact zero-network 256-terminal Kimi-v3 replay report.",
        ),
    ] = Path(
        "data/lf022_kimi_scientific_cfdbb46/prefix_256/batch/runs/"
        "9fc94ffe7c230634f961c6519bb5f70834de769afcbf5856affb4959117bf016.json"
    ),
    v4_contract: Annotated[
        Path,
        typer.Option("--v4-contract", help="Reviewed, still-unqualified Kimi-v4 contract."),
    ] = Path("configs/generation/lf022_kimi_k2_7_proposer_v4.yaml"),
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze the deterministic Kimi-v4 challenge without provider access."""
    from leanfaith.config.hashing import hash_file
    from leanfaith.config.paths import RepoPaths, RepoRootNotFoundError
    from leanfaith.generation.lf022_kimi_v4_selection import (
        LF022KimiV4SelectionError,
        freeze_lf022_kimi_v4_challenge_selection,
    )
    from leanfaith.generation.lf022_production import LF022ArtifactBinding

    try:
        paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
        root = paths.root.resolve(strict=True)

        def binding(path: Path, *, label: str) -> LF022ArtifactBinding:
            candidate = (path if path.is_absolute() else root / path).resolve(strict=True)
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(f"{label} must be inside the repository") from exc
            return LF022ArtifactBinding(path=relative, sha256=hash_file(candidate))

        frozen = freeze_lf022_kimi_v4_challenge_selection(
            repo_root=root,
            v3_manifest_binding=binding(v3_manifest, label="v3 manifest"),
            exact_offline_replay_report_binding=binding(
                exact_offline_replay_report,
                label="exact offline replay report",
            ),
            v4_contract_binding=binding(v4_contract, label="v4 contract"),
            current_code_bundle_binding=binding(
                current_code_bundle,
                label="current code bundle",
            ),
        )
    except (
        LF022KimiV4SelectionError,
        RepoRootNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"Kimi-v4 challenge freeze rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status={frozen.selection.status} selection_id={frozen.selection.selection_id} "
        f"path={frozen.selection_path} selected=16 capability_rank=0 "
        "network_requests=0 execution_admission_created=false promotion_enabled=false"
    )


@app.command("verify-lf022-kimi-v4-challenge")
def verify_lf022_kimi_v4_challenge_command(
    selection: Annotated[
        Path,
        typer.Option("--selection", help="Content-addressed Kimi-v4 challenge selection."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Replay a frozen Kimi-v4 challenge selection with zero network calls."""
    from leanfaith.config.hashing import hash_file
    from leanfaith.config.paths import RepoPaths, RepoRootNotFoundError
    from leanfaith.generation.lf022_kimi_v4_selection import (
        LF022KimiV4SelectionError,
        verify_lf022_kimi_v4_challenge_selection,
    )
    from leanfaith.generation.lf022_production import LF022ArtifactBinding

    try:
        paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
        root = paths.root.resolve(strict=True)
        candidate = (selection if selection.is_absolute() else root / selection).resolve(
            strict=True
        )
        relative = candidate.relative_to(root).as_posix()
        verified = verify_lf022_kimi_v4_challenge_selection(
            repo_root=root,
            selection_binding=LF022ArtifactBinding(
                path=relative,
                sha256=hash_file(candidate),
            ),
        )
    except (
        LF022KimiV4SelectionError,
        RepoRootNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        typer.echo(f"Kimi-v4 challenge replay rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"status={verified.status} selection_id={verified.selection_id} "
        "replayed_terminals=256 selected=16 network_requests=0 "
        "execution_admission_created=false promotion_enabled=false"
    )


@app.command("audit-training-readiness")
def audit_training_readiness_command(
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Frozen training-data readiness policy.",
        ),
    ] = Path("configs/models/training_data_readiness_v1.yaml"),
    reduced_data_ablation: Annotated[
        bool,
        typer.Option(
            "--reduced-data-ablation",
            help=(
                "Audit the explicitly named reduced-data mode; all label, split, "
                "source, cap, and gold-product requirements remain binding."
            ),
        ),
    ] = False,
    report_only: Annotated[
        bool,
        typer.Option(
            "--report-only",
            help="Write a NOT_READY report without returning a failing exit status.",
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Fail closed unless the frozen corpus is scientifically ready to train."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.data_readiness import (
        audit_training_data_readiness,
        load_training_data_readiness_policy,
        write_training_data_readiness_reports,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    policy_path = config_path if config_path.is_absolute() else paths.root / config_path
    try:
        loaded = load_training_data_readiness_policy(policy_path)
        report = audit_training_data_readiness(
            repo_root=paths.root,
            loaded_policy=loaded,
            reduced_data_ablation=reduced_data_ablation,
        )
        json_path = paths.root / loaded.config.reports.json_path
        markdown_path = paths.root / loaded.config.reports.markdown_path
        write_training_data_readiness_reports(
            report,
            json_path=json_path,
            markdown_path=markdown_path,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Training-readiness audit rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"status={report.status} audit_id={report.audit_id} "
        "prevalence_frame_adequate_for_annotation="
        f"{str(report.prevalence.frame_adequate_for_annotation).lower()} "
        f"human_terminal_labels={report.prevalence.human_terminal_label_count} "
        f"confirmatory_training_ready={str(report.training.confirmatory_ready).lower()} "
        f"report={json_path}"
    )
    if report.status == "NOT_READY" and not report_only:
        raise typer.Exit(code=3)


@app.command("close-gate5g")
def close_gate5g_command(
    frame_freeze_decision_path: Annotated[
        Path,
        typer.Option(
            "--frame-freeze-decision",
            help="Immutable, strictly replayable CSPRNG-bound v3 preferred-frame decision.",
        ),
    ],
    lineage_manifest_path: Annotated[
        Path,
        typer.Option(
            "--lineage-manifest",
            help="Content-addressed collection/postprocess/replay lineage manifest.",
        ),
    ],
    validated_manifest_path: Annotated[
        Path,
        typer.Option(
            "--validated-manifest",
            help="Final label-blind validated-real-outputs manifest.",
        ),
    ],
    coverage_report_path: Annotated[
        Path,
        typer.Option(
            "--coverage-report",
            help="Final generation-coverage report containing exact frame bindings.",
        ),
    ],
    phase_milestone_path: Annotated[
        Path,
        typer.Option(
            "--phase-milestone",
            help="Finalized Phase-5 milestone containing all prior report hashes.",
        ),
    ],
    prevalence_design_policy_path: Annotated[
        Path,
        typer.Option(
            "--prevalence-design-policy",
            help="Frozen frame-schema-3 prevalence design bound to its v1 base.",
        ),
    ] = Path("policies/lf021_prevalence_design_v2.yaml"),
    policy_path: Annotated[
        Path,
        typer.Option(
            "--policy",
            help="Frozen pre-label Gate-5G finalizer policy.",
        ),
    ] = Path("configs/generation/lf021_gate5g_finalizer_v1.yaml"),
    finalize: Annotated[
        bool,
        typer.Option(
            "--finalize",
            help=(
                "Explicitly write reports/gates/gate_5g.json after validation. "
                "Without this flag only a content-addressed dry-run report is written."
            ),
        ),
    ] = False,
    finalized_date: Annotated[
        str | None,
        typer.Option(
            "--finalized-date",
            help="Required with --finalize; forbidden during dry-run (YYYY-MM-DD).",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Validate or explicitly close the label-blind LF-021 generation gate."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.gate5g import validate_or_finalize_gate5g

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    parsed_date: datetime.date | None = None
    if finalized_date is not None:
        try:
            parsed_date = datetime.date.fromisoformat(finalized_date)
        except ValueError as exc:
            typer.echo("--finalized-date must be YYYY-MM-DD", err=True)
            raise typer.Exit(code=2) from exc
    try:
        result = validate_or_finalize_gate5g(
            paths=paths,
            frame_freeze_decision_path=anchored(frame_freeze_decision_path),
            lineage_manifest_path=anchored(lineage_manifest_path),
            validated_manifest_path=anchored(validated_manifest_path),
            coverage_report_path=anchored(coverage_report_path),
            phase_milestone_path=anchored(phase_milestone_path),
            prevalence_design_policy_path=anchored(prevalence_design_policy_path),
            policy_path=anchored(policy_path),
            finalize=finalize,
            finalized_date=parsed_date,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Gate 5G finalization rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"Gate 5G validation ready: {result.validation_report_path} "
        f"sha256={result.validation_report_sha256} "
        f"gate_5g_closed={str(result.gate_report is not None).lower()} "
        "semantic_labels_created=0 supervision_eligible=0 gate_5_closed=false"
    )
    if result.gate_report_path is not None:
        typer.echo(
            f"Gate 5G explicitly finalized: {result.gate_report_path} "
            f"sha256={result.gate_report_sha256}"
        )


@app.command("close-extended-gate5g")
def close_extended_gate5g_command(
    frame_freeze_decision_path: Annotated[
        Path,
        typer.Option(
            "--frame-freeze-decision",
            help=(
                "Immutable, strictly replayable production post-exhaustion "
                "preferred-frame decision."
            ),
        ),
    ],
    collection_authorization_paths: Annotated[
        list[Path],
        typer.Option(
            "--collection-authorization",
            help=(
                "Reviewed extension collection authorization in exact tranche "
                "prefix order; repeat once per extension tranche."
            ),
        ),
    ],
    lineage_manifest_path: Annotated[
        Path,
        typer.Option(
            "--lineage-manifest",
            help="Content-addressed mixed original/extension lineage manifest.",
        ),
    ],
    coverage_report_path: Annotated[
        Path,
        typer.Option(
            "--coverage-report",
            help="Final generation-coverage report containing exact extended-frame bindings.",
        ),
    ],
    phase_milestone_path: Annotated[
        Path,
        typer.Option(
            "--phase-milestone",
            help="Finalized Phase-5 milestone containing all prior extended-lineage hashes.",
        ),
    ],
    prevalence_design_policy_path: Annotated[
        Path,
        typer.Option(
            "--prevalence-design-policy",
            help="Frozen prevalence-design-v3 amendment bound to unchanged v2/v1.",
        ),
    ] = Path("policies/lf021_prevalence_design_v3.yaml"),
    policy_path: Annotated[
        Path,
        typer.Option(
            "--policy",
            help="Frozen post-exhaustion Gate-5G-v2 finalizer policy.",
        ),
    ] = Path("configs/generation/lf021_gate5g_finalizer_v2.yaml"),
    finalize: Annotated[
        bool,
        typer.Option(
            "--finalize",
            help=(
                "Explicitly write reports/gates/gate_5g.json after validation. "
                "Without this flag only a content-addressed dry-run report is written."
            ),
        ),
    ] = False,
    finalized_date: Annotated[
        str | None,
        typer.Option(
            "--finalized-date",
            help="Required with --finalize; forbidden during dry-run (YYYY-MM-DD).",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Validate or explicitly close Gate 5G over the extended frame lineage."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.extended_gate5g import (
        validate_or_finalize_extended_gate5g,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    parsed_date: datetime.date | None = None
    if finalized_date is not None:
        try:
            parsed_date = datetime.date.fromisoformat(finalized_date)
        except ValueError as exc:
            typer.echo("--finalized-date must be YYYY-MM-DD", err=True)
            raise typer.Exit(code=2) from exc
    try:
        result = validate_or_finalize_extended_gate5g(
            paths=paths,
            frame_freeze_decision_path=anchored(frame_freeze_decision_path),
            collection_authorization_paths=tuple(
                anchored(path) for path in collection_authorization_paths
            ),
            lineage_manifest_path=anchored(lineage_manifest_path),
            coverage_report_path=anchored(coverage_report_path),
            phase_milestone_path=anchored(phase_milestone_path),
            prevalence_design_policy_path=anchored(prevalence_design_policy_path),
            policy_path=anchored(policy_path),
            finalize=finalize,
            finalized_date=parsed_date,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Extended Gate 5G finalization rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"Extended Gate 5G validation ready: {result.validation_report_path} "
        f"sha256={result.validation_report_sha256} "
        f"gate_5g_closed={str(result.gate_report is not None).lower()} "
        "semantic_labels_created=0 supervision_eligible=0 gate_5_closed=false"
    )
    if result.gate_report_path is not None:
        typer.echo(
            f"Extended Gate 5G explicitly finalized: {result.gate_report_path} "
            f"sha256={result.gate_report_sha256}"
        )


@app.command("probe-deterministic-v2-coverage")
def probe_deterministic_v2_coverage_command(
    representations_path: Annotated[
        Path,
        typer.Option(
            "--representations",
            help="Immutable repr_v3 RepresentationRecord JSONL to inspect read-only.",
        ),
    ],
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            help="New canonical LF-031 coverage report path; existing files are rejected.",
        ),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="LF-031 v2 design config override (defaults to configs/transformations/v2.yaml).",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Report broad v2 design signals without Lean execution, drafts, or labels."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.v2_coverage import run_v2_coverage_probe

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        report, digest = run_v2_coverage_probe(
            representations_path=anchored(representations_path),
            output_path=anchored(output_path),
            repo_root=paths.root,
            config_path=None if config_path is None else anchored(config_path),
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"LF-031 deterministic-v2 coverage probe rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    total_hits = sum(item.theorem_hit_count for item in report.family_coverage)
    typer.echo(
        "LF-031 deterministic-v2 coverage probe OK; "
        f"report={anchored(output_path)} sha256={digest} "
        f"representations={report.representation_record_count} "
        f"family_signal_hits={total_hits} lean_requests=0 drafts=0 labels=0 "
        "interpretation=upper_bound_signal_not_applicability"
    )


@app.command("materialize-deterministic-v2-e0")
def materialize_deterministic_v2_e0_command(
    theorem_path: Annotated[
        Path,
        typer.Option("--theorem", help="One canonical TheoremRecord JSON object."),
    ],
    representation_path: Annotated[
        Path,
        typer.Option(
            "--representation",
            help="The source theorem's canonical RepresentationRecord JSON object.",
        ),
    ],
    rule_id: Annotated[
        str,
        typer.Option(
            "--rule-id",
            help="One rule enabled by the selected deterministic-v2 E0 profile.",
        ),
    ],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", help="Pinned Lean project used by LeanInteract."),
    ],
    output_path: Annotated[
        Path,
        typer.Option("--output", help="New create-only provisional result JSON path."),
    ],
    profile_path: Annotated[
        Path,
        typer.Option(
            "--profile",
            help="Versioned deterministic-v2 E0 execution profile.",
        ),
    ] = Path("configs/transformations/v2_e0_lf032_experimental.yaml"),
    import_header_path: Annotated[
        Path | None,
        typer.Option(
            "--import-header",
            help="Optional import-header text file for sources without inline/file context.",
        ),
    ] = None,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    raw_response_dir: Annotated[
        Path | None,
        typer.Option("--raw-response-dir", help="LeanInteract raw-response directory."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run one experimental LF-032 E0 candidate through LeanInteract."""
    from typing import cast

    from leanfaith.config.paths import RepoPaths
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
    from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
    from leanfaith.transforms.v2_e0_materializer import (
        V2E0MaterializationError,
        materialize_v2_e0_candidate,
        read_single_record,
        write_v2_e0_result,
    )
    from leanfaith.transforms.v2_e0_runtime import V2E0RuleId, build_v2_e0_runtime

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        theorem = cast(
            TheoremRecord,
            read_single_record(anchored(theorem_path), TheoremRecord),
        )
        representation = cast(
            RepresentationRecord,
            read_single_record(
                anchored(representation_path),
                RepresentationRecord,
            ),
        )
        import_header = (
            ""
            if import_header_path is None
            else anchored(import_header_path).read_text(encoding="utf-8")
        )
        runtime = build_v2_e0_runtime(paths.root, path=anchored(profile_path))
        if rule_id not in runtime.rule_ids:
            raise ValueError(
                f"rule {rule_id!r} is not enabled by profile {runtime.loaded.config.profile_id}"
            )
        raw_dir = (
            anchored(raw_response_dir)
            if raw_response_dir is not None
            else anchored(output_path).parent / "raw_lean"
        )
        backend = LeanInteractBackend(
            BackendSettings(
                project_dir=anchored(project_dir),
                context_fingerprint=theorem.context_id.removeprefix("ctx:"),
                environment_schema_version=1,
                raw_response_dir=raw_dir,
            )
        )
        try:
            result = materialize_v2_e0_candidate(
                backend=backend,
                runtime=runtime,
                theorem=theorem,
                representation=representation,
                rule_id=cast(V2E0RuleId, rule_id),
                seed=seed,
                project_dir=anchored(project_dir),
                import_header=import_header,
            )
        finally:
            backend.close()
        digest = write_v2_e0_result(result, anchored(output_path))
    except (OSError, ValueError, V2E0MaterializationError) as exc:
        typer.echo(f"deterministic-v2 E0 materialization rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic-v2 E0 materialization complete; "
        f"status={result.terminal_status} output={anchored(output_path)} "
        f"sha256={digest} resolved_labels=0 promoted_items=0 training_eligible=false"
    )


@app.command("materialize-deterministic-v2-e0-scale")
def materialize_deterministic_v2_e0_scale_command(
    theorem_path: Annotated[
        Path,
        typer.Option("--theorems", help="Canonical TheoremRecord JSONL partition."),
    ],
    representation_path: Annotated[
        Path,
        typer.Option(
            "--representations",
            help="Canonical RepresentationRecord JSONL partition aligned by theorem ID.",
        ),
    ],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", help="Pinned Lean project used by LeanInteract."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Create-or-resume hash-bound v2 scale root."),
    ],
    profile_path: Annotated[
        Path,
        typer.Option("--profile", help="Exact experimental E0 execution profile."),
    ] = Path("configs/transformations/v2_e0_lf032_experimental.yaml"),
    import_header_path: Annotated[
        Path | None,
        typer.Option("--import-header", help="Optional shared Lean import-header file."),
    ] = None,
    batch_size: Annotated[int, typer.Option("--batch-size", min=1)] = 128,
    max_sources: Annotated[
        int | None,
        typer.Option("--max-sources", min=1, help="Deterministic source-prefix limit."),
    ] = None,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 8,
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option("--memory-hard-limit-mb", min=1),
    ] = None,
    base_seed: Annotated[int, typer.Option("--base-seed")] = 0,
    raw_response_dir: Annotated[
        Path | None,
        typer.Option("--raw-response-dir", help="LeanInteract raw-response directory."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run/resume an experimental v2 E0 inventory through LeanServerPool."""
    import json
    from dataclasses import replace

    from leanfaith.config.paths import RepoPaths
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
    from leanfaith.lean.session_policy import ServerMode
    from leanfaith.schemas.theorem import TheoremRecord
    from leanfaith.transforms.v2_e0_runtime import build_v2_e0_runtime
    from leanfaith.transforms.v2_e0_scale_run import V2E0ScaleRunError, run_v2_e0_scale

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        resolved_theorems = anchored(theorem_path).resolve(strict=True)
        with resolved_theorems.open("rb") as handle:
            first_line = handle.readline()
        if not first_line.endswith(b"\n"):
            raise V2E0ScaleRunError("theorem partition is empty or lacks JSONL framing")
        first_payload = json.loads(first_line)
        if isinstance(first_payload, dict) and "theorem" in first_payload:
            first_payload = first_payload["theorem"]
        first_theorem = TheoremRecord.model_validate(first_payload)
        import_header = (
            ""
            if import_header_path is None
            else anchored(import_header_path).read_text(encoding="utf-8")
        )
        resolved_output = anchored(output_dir)
        raw_dir = (
            anchored(raw_response_dir)
            if raw_response_dir is not None
            else resolved_output / "raw_lean"
        )
        settings = BackendSettings(
            project_dir=anchored(project_dir),
            context_fingerprint=first_theorem.context_id.removeprefix("ctx:"),
            environment_schema_version=1,
            raw_response_dir=raw_dir,
            server_mode=ServerMode.POOL,
            workers=workers,
            memory_hard_limit_mb=memory_hard_limit_mb,
        )
        LeanInteractBackend.prepare_environment(settings)
        backend = LeanInteractBackend(replace(settings, environment_is_prepared=True))
        try:
            artifacts = run_v2_e0_scale(
                backend=backend,
                runtime=build_v2_e0_runtime(paths.root, path=anchored(profile_path)),
                theorem_path=resolved_theorems,
                representation_path=anchored(representation_path),
                project_dir=anchored(project_dir),
                import_header=import_header,
                output_dir=resolved_output,
                batch_size=batch_size,
                base_seed=base_seed,
                max_sources=max_sources,
            )
        finally:
            backend.close()
    except (OSError, ValueError, V2E0ScaleRunError) as exc:
        typer.echo(f"deterministic-v2 E0 scale rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic-v2 E0 scale complete; "
        f"results={artifacts.result_count} output={artifacts.output_dir} "
        f"manifest={artifacts.manifest_path} resolved_labels=0 promoted_items=0 "
        "training_eligible=false"
    )


@app.command("materialize-deterministic-v2-d0-scale")
def materialize_deterministic_v2_d0_scale_command(
    theorem_path: Annotated[
        Path,
        typer.Option("--theorems", help="Canonical TheoremRecord JSONL partition."),
    ],
    representation_path: Annotated[
        Path,
        typer.Option(
            "--representations",
            help="Canonical RepresentationRecord JSONL partition aligned by theorem ID.",
        ),
    ],
    project_dir: Annotated[
        Path,
        typer.Option("--project-dir", help="Pinned Lean project used by LeanInteract."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Create-or-resume hash-bound D0 scale root."),
    ],
    profile_path: Annotated[
        Path,
        typer.Option("--profile", help="Exact experimental D0 execution profile."),
    ] = Path("configs/transformations/v2_d0_n11_experimental.yaml"),
    import_header_path: Annotated[
        Path | None,
        typer.Option("--import-header", help="Optional shared Lean import-header file."),
    ] = None,
    batch_size: Annotated[int, typer.Option("--batch-size", min=1)] = 128,
    max_sources: Annotated[
        int | None,
        typer.Option("--max-sources", min=1, help="Deterministic source-prefix limit."),
    ] = None,
    workers: Annotated[int, typer.Option("--workers", min=1)] = 8,
    memory_hard_limit_mb: Annotated[
        int | None,
        typer.Option("--memory-hard-limit-mb", min=1),
    ] = None,
    base_seed: Annotated[int, typer.Option("--base-seed")] = 0,
    raw_response_dir: Annotated[
        Path | None,
        typer.Option("--raw-response-dir", help="LeanInteract raw-response directory."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run/resume one experimental LF-034 D0 inventory through LeanServerPool."""
    import json
    from dataclasses import replace

    from leanfaith.config.loading import load_yaml_mapping
    from leanfaith.config.paths import RepoPaths
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
    from leanfaith.lean.session_policy import ServerMode
    from leanfaith.schemas.theorem import TheoremRecord
    from leanfaith.transforms.v2_d0_n12_runtime import (
        V2D0N12Runtime,
        build_v2_d0_n12_runtime,
    )
    from leanfaith.transforms.v2_d0_n13_runtime import (
        V2D0N13Runtime,
        build_v2_d0_n13_runtime,
    )
    from leanfaith.transforms.v2_d0_n14_runtime import (
        V2D0N14Runtime,
        build_v2_d0_n14_runtime,
    )
    from leanfaith.transforms.v2_d0_n15_runtime import (
        V2D0N15Runtime,
        build_v2_d0_n15_runtime,
    )
    from leanfaith.transforms.v2_d0_n16_runtime import (
        V2D0N16Runtime,
        build_v2_d0_n16_runtime,
    )
    from leanfaith.transforms.v2_d0_n17_runtime import (
        V2D0N17Runtime,
        build_v2_d0_n17_runtime,
    )
    from leanfaith.transforms.v2_d0_runtime import V2D0Runtime, build_v2_d0_runtime
    from leanfaith.transforms.v2_d0_scale_run import V2D0ScaleRunError, run_v2_d0_scale

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        resolved_theorems = anchored(theorem_path).resolve(strict=True)
        with resolved_theorems.open("rb") as handle:
            first_line = handle.readline()
        if not first_line.endswith(b"\n"):
            raise V2D0ScaleRunError("theorem partition is empty or lacks JSONL framing")
        first_payload = json.loads(first_line)
        if isinstance(first_payload, dict) and "theorem" in first_payload:
            first_payload = first_payload["theorem"]
        first_theorem = TheoremRecord.model_validate(first_payload)
        import_header = (
            ""
            if import_header_path is None
            else anchored(import_header_path).read_text(encoding="utf-8")
        )
        resolved_profile = anchored(profile_path).resolve(strict=True)
        profile_id = load_yaml_mapping(resolved_profile).get("profile_id")
        runtime: (
            V2D0Runtime
            | V2D0N12Runtime
            | V2D0N13Runtime
            | V2D0N14Runtime
            | V2D0N15Runtime
            | V2D0N16Runtime
            | V2D0N17Runtime
        )
        if profile_id == "deterministic_v2_d0_n11_experimental":
            runtime = build_v2_d0_runtime(paths.root, path=resolved_profile)
        elif profile_id == "deterministic_v2_d0_n12_experimental":
            runtime = build_v2_d0_n12_runtime(paths.root, path=resolved_profile)
        elif profile_id == "deterministic_v2_d0_n13_experimental":
            runtime = build_v2_d0_n13_runtime(paths.root, path=resolved_profile)
        elif profile_id == "deterministic_v2_d0_n14_experimental":
            runtime = build_v2_d0_n14_runtime(paths.root, path=resolved_profile)
        elif profile_id == "deterministic_v2_d0_n15_experimental":
            runtime = build_v2_d0_n15_runtime(paths.root, path=resolved_profile)
        elif profile_id == "deterministic_v2_d0_n16_experimental":
            runtime = build_v2_d0_n16_runtime(paths.root, path=resolved_profile)
        elif profile_id == "deterministic_v2_d0_n17_experimental":
            runtime = build_v2_d0_n17_runtime(paths.root, path=resolved_profile)
        else:
            raise V2D0ScaleRunError(f"unsupported D0 profile_id: {profile_id!r}")
        resolved_output = anchored(output_dir)
        raw_dir = (
            anchored(raw_response_dir)
            if raw_response_dir is not None
            else resolved_output / "raw_lean"
        )
        settings = BackendSettings(
            project_dir=anchored(project_dir),
            context_fingerprint=first_theorem.context_id.removeprefix("ctx:"),
            environment_schema_version=1,
            raw_response_dir=raw_dir,
            server_mode=ServerMode.POOL,
            workers=workers,
            memory_hard_limit_mb=memory_hard_limit_mb,
        )
        LeanInteractBackend.prepare_environment(settings)
        backend = LeanInteractBackend(replace(settings, environment_is_prepared=True))
        try:
            artifacts = run_v2_d0_scale(
                backend=backend,
                runtime=runtime,
                theorem_path=resolved_theorems,
                representation_path=anchored(representation_path),
                project_dir=anchored(project_dir),
                import_header=import_header,
                output_dir=resolved_output,
                batch_size=batch_size,
                base_seed=base_seed,
                max_sources=max_sources,
            )
        finally:
            backend.close()
    except (OSError, ValueError, V2D0ScaleRunError) as exc:
        typer.echo(f"deterministic-v2 D0 scale rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic-v2 D0 scale complete; "
        f"results={artifacts.result_count} output={artifacts.output_dir} "
        f"manifest={artifacts.manifest_path} resolved_labels=0 promoted_items=0 "
        "training_eligible=false"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
