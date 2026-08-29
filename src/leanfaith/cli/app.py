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
    merge_scale_shards_provisional: Annotated[
        bool,
        typer.Option(
            "--merge-scale-shards-provisional",
            help=(
                "Content-audit a complete producer shard set without the scientific "
                "second Lean replay; output is exploratory-only and gate-ineligible."
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
                materialize_scale,
                merge_scale_shards,
                merge_scale_shards_provisional,
                freeze_scale_inventory,
            )
        )
        > 1
    ):
        typer.echo(
            "--validate-only, --validate-positives, --validate-negatives, and "
            "--run-negative-pre-scale/"
            "--materialize-scale/--merge-scale-shards/"
            "--merge-scale-shards-provisional/"
            "--freeze-scale-inventory are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=2)
    if extraction_reuse_attestation is not None and not freeze_scale_inventory:
        typer.echo(
            "--extraction-reuse-attestation is supported only with --freeze-scale-inventory",
            err=True,
        )
        raise typer.Exit(code=2)
    if merge_scale_shards or merge_scale_shards_provisional:
        merge_flag = (
            "--merge-scale-shards" if merge_scale_shards else "--merge-scale-shards-provisional"
        )
        if report_path is not None:
            typer.echo(f"--report is not accepted with {merge_flag}", err=True)
            raise typer.Exit(code=2)
        if output_dir is None or not shard_output_dirs:
            typer.echo(
                f"{merge_flag} requires --output-dir and repeated --shard-output-dir",
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
                f"{merge_flag} does not accept " + ", ".join(forbidden),
                err=True,
            )
            raise typer.Exit(code=2)
        from leanfaith.transforms.scale_materializer import DeterministicScaleError
        from leanfaith.transforms.scale_merge import (
            merge_deterministic_scale_shards,
            merge_deterministic_scale_shards_provisional,
        )

        try:
            merge_function = (
                merge_deterministic_scale_shards
                if merge_scale_shards
                else merge_deterministic_scale_shards_provisional
            )
            merged_artifacts = merge_function(
                paths=paths,
                shard_output_dirs=shard_output_dirs,
                output_dir=output_dir,
            )
        except DeterministicScaleError as exc:
            typer.echo(f"deterministic scale shard merge FAILED ({merge_flag}): {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if merge_scale_shards:
            eligibility = "merge_replayed_with_lean=true training_eligible=false"
        else:
            eligibility = (
                "merge_replayed_with_lean=false exploratory_modeling_eligible=true "
                "training_eligible=false evaluation_eligible=false gate_credit=false"
            )
        typer.echo(
            "deterministic scale shard merge OK; "
            f"output={merged_artifacts.output_dir} "
            f"manifest={merged_artifacts.manifest_path} "
            f"manifest_sha256={merged_artifacts.manifest_sha256} "
            f"merged_manifest_hash={merged_artifacts.merged_manifest_hash} "
            "resolved_semantic_labels=0 promoted_items=0 output_tier=provisional "
            f"{eligibility}"
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
            "--run-negative-pre-scale/--materialize-scale",
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


@app.command("run-deterministic-v2-composition-smokes")
def run_deterministic_v2_composition_smokes_command(
    code_root: Annotated[
        Path,
        typer.Option(
            "--code-root",
            envvar="LEANFAITH_CODE_ROOT",
            help="Clean LeanFaith checkout whose source is used by every child.",
        ),
    ],
    expected_commit: Annotated[
        str,
        typer.Option(
            "--expected-commit",
            envvar="LEANFAITH_EXPECTED_COMMIT",
            help="Exact 40-hex clean code commit.",
        ),
    ],
    source_dir: Annotated[
        Path,
        typer.Option(
            "--source-dir",
            envvar="LEANFAITH_COMPOSITION_SMOKE_SOURCE",
            help="Exact 64-row composition smoke-source directory.",
        ),
    ],
    project_dir: Annotated[
        Path,
        typer.Option(
            "--project-dir",
            envvar="LEANFAITH_LEAN_PROJECT",
            help="Clean pinned Lean project used by LeanInteract.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            envvar="LEANFAITH_COMPOSITION_SMOKE_OUTPUT",
            help="Root for 13 family roots plus orchestration status/log/receipt.",
        ),
    ],
    reuse_roots: Annotated[
        list[str] | None,
        typer.Option(
            "--reuse-root",
            help="Explicit FAMILY=PATH completed schema-3 root; repeat as needed.",
        ),
    ] = None,
    reuse_root_commits: Annotated[
        list[str] | None,
        typer.Option(
            "--reuse-root-producer-commit",
            help="FAMILY=40_HEX producer attestation for each --reuse-root.",
        ),
    ] = None,
) -> None:
    """Run all 13 composition smokes serially with one unlimited-memory worker."""
    from leanfaith.transforms.composition_smoke_launcher import (
        CompositionSmokeLaunchError,
        run_composition_smokes,
    )

    def assignments(values: list[str] | None, *, label: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for value in values or ():
            family, separator, assigned = value.partition("=")
            if not separator or not family or not assigned or family in parsed:
                raise ValueError(f"invalid or duplicate {label} assignment: {value!r}")
            parsed[family] = assigned
        return parsed

    try:
        root_assignments = assignments(reuse_roots, label="reuse-root")
        commit_assignments = assignments(reuse_root_commits, label="reuse-root-producer-commit")
        receipt = run_composition_smokes(
            code_root=code_root,
            expected_commit=expected_commit,
            source_dir=source_dir,
            project_dir=project_dir,
            output_root=output_root,
            reused_roots={key: Path(value) for key, value in root_assignments.items()},
            reused_root_commits=commit_assignments,
        )
    except (CompositionSmokeLaunchError, OSError, ValueError) as exc:
        typer.echo(f"composition smoke launch FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "composition smoke launch complete; "
        f"families={len(receipt.roots)} receipt_id={receipt.receipt_id} "
        f"receipt={output_root.resolve() / 'orchestration/receipt.json'} "
        "workers=1 memory_hard_limit_mb=none processes_parallel=1 "
        "resolved_labels=0 promoted_items=0 training_eligible=false"
    )


@app.command("run-deterministic-v2-composition-full-scale")
def run_deterministic_v2_composition_full_scale_command(
    code_root: Annotated[
        Path,
        typer.Option(
            "--code-root",
            envvar="LEANFAITH_CODE_ROOT",
            help="Clean LeanFaith checkout used by every family process.",
        ),
    ],
    expected_commit: Annotated[
        str,
        typer.Option(
            "--expected-commit",
            envvar="LEANFAITH_EXPECTED_COMMIT",
            help="Exact 40-hex clean code commit.",
        ),
    ],
    seed_dir: Annotated[
        Path,
        typer.Option(
            "--seed-dir",
            envvar="LEANFAITH_COMPOSITION_FULL_SEED",
            help="Canonical 3,941-row composition seed directory.",
        ),
    ],
    project_dir: Annotated[
        Path,
        typer.Option(
            "--project-dir",
            envvar="LEANFAITH_LEAN_PROJECT",
            help="Clean pinned Lean project used by LeanInteract.",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            envvar="LEANFAITH_COMPOSITION_FULL_OUTPUT",
            help="Root for 13 full family roots plus orchestration artifacts.",
        ),
    ],
) -> None:
    """Run all 13 full composition families serially with one Lean worker."""
    from leanfaith.transforms.composition_full_launcher import (
        CompositionFullLaunchError,
        run_composition_full_scale,
    )

    try:
        receipt = run_composition_full_scale(
            code_root=code_root,
            expected_commit=expected_commit,
            seed_dir=seed_dir,
            project_dir=project_dir,
            output_root=output_root,
        )
    except (CompositionFullLaunchError, OSError, ValueError) as exc:
        typer.echo(f"composition full-scale launch FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "composition full-scale launch complete; "
        f"families={len(receipt.roots)} receipt_id={receipt.receipt_id} "
        f"receipt={output_root.resolve() / 'orchestration/receipt.json'} "
        "source_count=3941 workers=1 memory_hard_limit_mb=none "
        "processes_parallel=1 resolved_labels=0 promoted_items=0 "
        "training_eligible=false"
    )


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


@app.command("prepare-lf022-weak-batch")
def prepare_lf022_weak_batch_command(
    spec_path: Annotated[
        Path,
        typer.Option("--spec", help="Canonical offline weak-batch JSON spec."),
    ],
    spec_sha256: Annotated[
        str,
        typer.Option("--spec-sha256", help="Expected SHA-256 of --spec."),
    ],
    randomization_key_path: Annotated[
        Path,
        typer.Option(
            "--randomization-key-file",
            help="Binary file containing at least 32 bytes; only its hash is persisted.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Immutable prepared-batch artifact directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Prepare four blinded judge requests per pair; perform zero provider calls."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_weak_batch import (
        LF022WeakBatchError,
        prepare_lf022_weak_batch,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        records, manifest = prepare_lf022_weak_batch(
            repo_root=paths.root,
            spec_path=anchored(spec_path),
            expected_spec_sha256=spec_sha256,
            randomization_key=anchored(randomization_key_path).read_bytes(),
            output_dir=anchored(output_dir),
        )
    except (LF022WeakBatchError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 weak-batch preparation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"batch_id={manifest.batch_id} pairs={manifest.dispatch_pair_count} "
        f"cells={len(records)} provider_calls=0 semantic_labels_created=0 "
        "silver_records_created=0 training_eligible=false"
    )


@app.command("replay-finalize-lf022-weak-batch")
def replay_finalize_lf022_weak_batch_command(
    batch_root: Annotated[
        Path,
        typer.Option(
            "--batch-root",
            help="Prepared batch containing canonical raw responses under raw/<judge_slot>/.",
        ),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Replay persisted raw responses and finalize non-trainable weak evidence."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_weak_batch import (
        LF022WeakBatchError,
        finalize_lf022_weak_batch,
        replay_lf022_weak_batch,
    )
    from leanfaith.generation.providers import ProviderError

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    resolved = batch_root if batch_root.is_absolute() else paths.root / batch_root
    try:
        terminals, execution = replay_lf022_weak_batch(batch_root=resolved)
        evidence, candidates, finalization = finalize_lf022_weak_batch(batch_root=resolved)
    except (LF022WeakBatchError, ProviderError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 weak-batch replay rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"execution_id={execution.execution_id} finalization_id={finalization.finalization_id} "
        f"terminals={len(terminals)} evidence={len(evidence)} candidates={len(candidates)} "
        "provider_calls=0 semantic_labels_created=0 silver_records_created=0 "
        "training_eligible=false"
    )


@app.command("prepare-deterministic-composition-seeds")
def prepare_deterministic_composition_seeds_command(
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
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Prepare label-free E2 positives as deterministic second-hop seeds."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.composition_seed import (
        CompositionSeedError,
        prepare_deterministic_v2_composition_seeds,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = prepare_deterministic_v2_composition_seeds(
            combination_dir=anchored(combination_dir),
            materialization_roots=[anchored(path) for path in materialization_roots],
            output_dir=anchored(output_dir),
        )
    except (CompositionSeedError, OSError, ValueError) as exc:
        typer.echo(f"Deterministic composition-seed preparation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"seed_set_id={artifacts.seed_set_id} seeds={artifacts.seed_count} "
        f"status={'replayed' if artifacts.replayed else 'prepared'} "
        "semantic_labels_created=0 training_eligible=false"
    )


@app.command("audit-deterministic-composition-chains")
def audit_deterministic_composition_chains_command(
    seed_dir: Annotated[
        Path,
        typer.Option("--seed-dir", help="Completed immutable composition seed directory."),
    ],
    second_hop_roots: Annotated[
        list[Path],
        typer.Option(
            "--second-hop-root",
            "-r",
            help="Completed E2 or D0 second-hop root; repeat for every root.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable composition-chain directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Audit label-free P-to-P and P-to-N deterministic depth-two chains."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.composition_chain import (
        CompositionChainError,
        audit_deterministic_v2_composition_chains,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = audit_deterministic_v2_composition_chains(
            seed_dir=anchored(seed_dir),
            second_hop_roots=[anchored(path) for path in second_hop_roots],
            output_dir=anchored(output_dir),
        )
    except (CompositionChainError, OSError, ValueError) as exc:
        typer.echo(f"Deterministic composition-chain audit rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"chain_set_id={artifacts.chain_set_id} chains={artifacts.chain_count} "
        f"status={'replayed' if artifacts.replayed else 'audited'} "
        "semantic_labels_created=0 promoted_items=0 training_eligible=false "
        "evaluation_eligible=false gate_credit=false"
    )


@app.command("postprocess-deterministic-composition-unique-pairs")
def postprocess_deterministic_composition_unique_pairs_command(
    seed_dir: Annotated[
        Path,
        typer.Option("--seed-dir", help="Exact immutable composition seed directory."),
    ],
    chain_dir: Annotated[
        Path,
        typer.Option("--chain-dir", help="Exact immutable composition chain-v1 directory."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable unique-pair audit directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Deduplicate chain receipts and separate alpha returns from novel pairs."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.composition_unique_pairs import (
        CompositionUniquePairError,
        postprocess_deterministic_v2_composition_unique_pairs,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=anchored(seed_dir),
            chain_dir=anchored(chain_dir),
            output_dir=anchored(output_dir),
        )
    except (CompositionUniquePairError, OSError, ValueError) as exc:
        typer.echo(f"Deterministic composition unique-pair postprocess rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"unique_pair_set_id={artifacts.unique_pair_set_id} "
        f"gross_chains={artifacts.gross_chain_count} unique_pairs={artifacts.unique_pair_count} "
        f"status={'replayed' if artifacts.replayed else 'postprocessed'} "
        "semantic_labels_created=0 promoted_items=0 training_eligible=false "
        "evaluation_eligible=false gate_credit=false"
    )


@app.command("prepare-deterministic-composition-polarity-frontier")
def prepare_deterministic_composition_polarity_frontier_command(
    chain_dir: Annotated[
        Path,
        typer.Option("--chain-dir", help="Exact immutable depth-two chain directory."),
    ],
    unique_pair_dir: Annotated[
        Path,
        typer.Option("--unique-pair-dir", help="Exact immutable depth-two unique-pair directory."),
    ],
    second_hop_roots: Annotated[
        list[Path],
        typer.Option(
            "--second-hop-root",
            "-r",
            help="Receipt-bound E2/D0 root; repeat for every depth-two root.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable positive-only frontier directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze alpha-novel depth-two finals for a positive-only third hop."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.composition_polarity_frontier import (
        CompositionPolarityFrontierError,
        prepare_deterministic_v2_polarity_frontier,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = prepare_deterministic_v2_polarity_frontier(
            chain_dir=anchored(chain_dir),
            unique_pair_dir=anchored(unique_pair_dir),
            second_hop_roots=[anchored(path) for path in second_hop_roots],
            output_dir=anchored(output_dir),
        )
    except (CompositionPolarityFrontierError, OSError, ValueError) as exc:
        typer.echo(f"Deterministic composition frontier rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"frontier_set_id={artifacts.frontier_set_id} frontier={artifacts.frontier_count} "
        f"status={'replayed' if artifacts.replayed else 'prepared'} "
        "next_hop=E2_positive_only semantic_labels_created=0 promoted_items=0 "
        "training_eligible=false evaluation_eligible=false gate_credit=false"
    )


@app.command("audit-deterministic-composition-third-hop")
def audit_deterministic_composition_third_hop_command(
    frontier_dir: Annotated[
        Path,
        typer.Option("--frontier-dir", help="Exact immutable positive-only frontier directory."),
    ],
    unique_pair_dir: Annotated[
        Path,
        typer.Option(
            "--unique-pair-dir",
            help="Exact immutable unique-pair artifact cryptographically bound by the frontier.",
        ),
    ],
    seed_dir: Annotated[
        Path,
        typer.Option(
            "--seed-dir",
            help="Exact immutable depth-one seed inventory bound by the unique pairs.",
        ),
    ],
    chain_dir: Annotated[
        Path,
        typer.Option(
            "--chain-dir",
            help="Exact immutable depth-two chain receipt bound by the frontier.",
        ),
    ],
    third_hop_roots: Annotated[
        list[Path],
        typer.Option(
            "--third-hop-root",
            "-r",
            help="Completed frontier-bound E2 root; repeat exactly once for P14 through P18.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable depth-three audit directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Bind and deduplicate the five positive-only third-hop results."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.composition_third_hop import (
        CompositionThirdHopError,
        audit_deterministic_v2_composition_third_hop,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = audit_deterministic_v2_composition_third_hop(
            frontier_dir=anchored(frontier_dir),
            unique_pair_dir=anchored(unique_pair_dir),
            seed_dir=anchored(seed_dir),
            chain_dir=anchored(chain_dir),
            third_hop_roots=[anchored(path) for path in third_hop_roots],
            output_dir=anchored(output_dir),
        )
    except (CompositionThirdHopError, OSError, ValueError) as exc:
        typer.echo(f"Deterministic composition third-hop audit rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"third_hop_set_id={artifacts.third_hop_set_id} "
        f"gross_chains={artifacts.gross_chain_count} "
        f"unique_pairs={artifacts.unique_pair_count} "
        f"quarantined_chains={artifacts.quarantine_count} "
        f"status={'replayed' if artifacts.replayed else 'audited'} "
        "semantic_labels_created=0 promoted_items=0 training_eligible=false "
        "evaluation_eligible=false gate_credit=false"
    )


@app.command("export-deterministic-composition-receipt")
def export_deterministic_composition_receipt_command(
    full_run_root: Annotated[
        Path,
        typer.Option(
            "--full-run-root",
            help="Completed 13-family full-run root containing orchestration/receipt.json.",
        ),
    ],
    seed_dir: Annotated[
        Path,
        typer.Option("--seed-dir", help="Exact immutable composition seed directory."),
    ],
    source_theorems: Annotated[
        list[Path],
        typer.Option(
            "--source-theorems",
            help="Canonical original-source TheoremRecord JSONL; repeat for public/private inputs.",
        ),
    ],
    source_representations: Annotated[
        list[Path],
        typer.Option(
            "--source-representations",
            help=(
                "Canonical original-source RepresentationRecord JSONL; repeat for "
                "public/private inputs."
            ),
        ),
    ],
    chain_dir: Annotated[
        Path,
        typer.Option("--chain-dir", help="Immutable all-root composition-chain directory."),
    ],
    unique_pair_dir: Annotated[
        Path,
        typer.Option(
            "--unique-pair-dir", help="Immutable composition unique-pair audit directory."
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable provisional export directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Export a full receipt as provisional pairs plus cycles/conflicts report."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.composition_receipt_export import (
        CompositionReceiptExportError,
        export_deterministic_v2_composition_receipt,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = export_deterministic_v2_composition_receipt(
            full_run_root=anchored(full_run_root),
            seed_dir=anchored(seed_dir),
            source_theorems=[anchored(path) for path in source_theorems],
            source_representations=[anchored(path) for path in source_representations],
            chain_dir=anchored(chain_dir),
            unique_pair_dir=anchored(unique_pair_dir),
            output_dir=anchored(output_dir),
        )
    except (CompositionReceiptExportError, OSError, ValueError) as exc:
        typer.echo(f"Deterministic composition receipt export rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"export_set_id={artifacts.export_set_id} "
        f"provisional_inventory={artifacts.provisional_inventory_count} "
        f"cycles={artifacts.cycle_audit_count} "
        f"quarantine={artifacts.mixed_intention_quarantine_count} "
        f"status={'replayed' if artifacts.replayed else 'exported'} "
        "semantic_labels_created=0 promoted_items=0 training_eligible=false "
        "evaluation_eligible=false gate_credit=false"
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
                "to moonshot_kimi_k2, qwen3, glm5, or deepseek_v4."
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
            help="Frozen one-item proposer qualification admission JSON.",
        ),
    ],
    qualification_task_path: Annotated[
        Path,
        typer.Option(
            "--qualification-task",
            help="Frozen one-item proposer qualification task JSON.",
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
    batch_manifest: Annotated[
        Path | None,
        typer.Option(
            "--batch-manifest",
            help=(
                "Optional canonical LF-022 public batch manifest; when supplied, check only "
                "successful variants from its exact execution-task set."
            ),
        ),
    ] = None,
    postgen_selector: Annotated[
        Path | None,
        typer.Option(
            "--postgen-selector",
            help=(
                "Optional content-addressed terminal-only selector emitted by "
                "reconcile-lf022-postgen for safe incremental checking."
            ),
        ),
    ] = None,
    expected_postgen_selector_id: Annotated[
        str | None,
        typer.Option(
            "--expected-postgen-selector-id",
            help=(
                "Exact selector content ID verified before choosing the output directory; "
                "requires --postgen-selector."
            ),
        ),
    ] = None,
    codex_scale_manifest: Annotated[
        Path | None,
        typer.Option(
            "--codex-scale-manifest",
            help=(
                "Optional completed Codex proposer scale-v2 manifest; replay its exact "
                "tranche and v1 terminal lineages before pooled Lean checking."
            ),
        ),
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
            batch_manifest_path=(anchored(batch_manifest) if batch_manifest is not None else None),
            postgen_selector_path=(
                anchored(postgen_selector) if postgen_selector is not None else None
            ),
            expected_postgen_selector_id=expected_postgen_selector_id,
            codex_scale_manifest_path=(
                anchored(codex_scale_manifest) if codex_scale_manifest is not None else None
            ),
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


@app.command("propose-lf022-codex")
def propose_lf022_codex_command(
    batch_manifest: Annotated[
        Path,
        typer.Option(
            "--batch-manifest",
            help="Existing frozen public LF-022 batch manifest used only as a source subset.",
        ),
    ],
    task_id: Annotated[
        str,
        typer.Option(
            "--task-id",
            help="Exact frozen LF-022 execution-task ID to present to Codex.",
        ),
    ],
    config: Annotated[
        Path,
        typer.Option("--config", help="Pinned Codex proposer smoke configuration."),
    ] = Path("configs/generation/lf022_codex_proposer_smoke_v1.yaml"),
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Repository-local immutable/resumable Codex proposer artifact root.",
        ),
    ] = Path("data/lf022_codex_proposer/smoke_v1"),
    execute_public_provisional: Annotated[
        bool,
        typer.Option(
            "--execute-public-provisional",
            help=(
                "Explicitly authorize one public external Codex call. Without this flag, "
                "the command performs only network-free preparation."
            ),
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Propose one unvalidated public LF-022 variant with Codex exec."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_codex_proposer import (
        LF022CodexProposerError,
        run_lf022_codex_proposer,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_lf022_codex_proposer(
            repo_root=paths.root,
            config_path=anchored(config),
            batch_manifest_path=anchored(batch_manifest),
            execution_task_ids=(task_id,),
            output_root=anchored(output_root),
            execute_public_provisional=execute_public_provisional,
        )
    except (LF022CodexProposerError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 Codex proposer rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not execute_public_provisional:
        typer.echo(
            f"prepared={len(result.prepared)} item_id={result.prepared[0].item.item_id} "
            "external_calls=0 execute_requires_explicit_flag=true "
            "semantic_labels_created=0 supervision_eligible=false training_eligible=false"
        )
        return
    assert result.manifest is not None
    assert result.manifest_path is not None
    typer.echo(
        f"completed={result.manifest.completed_count} invoked={result.invoked_count} "
        f"reused={result.reused_count} statuses="
        f"{json.dumps(result.manifest.status_counts, sort_keys=True)} "
        f"manifest={result.manifest_path} outputs_provisional_only=true "
        "semantic_labels_created=0 supervision_eligible=false training_eligible=false "
        "evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("propose-lf022-codex-scale")
def propose_lf022_codex_scale_command(
    batch_manifest: Annotated[
        Path,
        typer.Option(
            "--batch-manifest",
            help="Existing frozen public LF-022 batch manifest.",
        ),
    ],
    task_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--task-id",
            help=(
                "Exact frozen execution-task ID; repeat to select an explicit ordered "
                "subset. If omitted, the bounded manifest prefix is selected."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            max=64,
            help="Optional lower runtime limit; cannot exceed the pinned v2 task limit.",
        ),
    ] = None,
    config: Annotated[
        Path,
        typer.Option("--config", help="Pinned bounded Codex proposer scale configuration."),
    ] = Path("configs/generation/lf022_codex_proposer_scale_v2.yaml"),
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Repository-local sequential/resumable v2 artifact root.",
        ),
    ] = Path("data/lf022_codex_proposer/scale_v2"),
    execute_public_provisional: Annotated[
        bool,
        typer.Option(
            "--execute-public-provisional",
            help=(
                "Explicitly authorize the bounded public-only sequential calls. "
                "Without this flag, preparation is network-free."
            ),
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run a bounded sequential tranche through the exact reviewed v1 proposer."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_codex_proposer import LF022CodexProposerError
    from leanfaith.generation.lf022_codex_proposer_scale import (
        run_lf022_codex_proposer_scale,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = run_lf022_codex_proposer_scale(
            repo_root=paths.root,
            config_path=anchored(config),
            batch_manifest_path=anchored(batch_manifest),
            output_root=anchored(output_root),
            execution_task_ids=tuple(task_ids or ()),
            task_limit=limit,
            execute_public_provisional=execute_public_provisional,
        )
    except (LF022CodexProposerError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 Codex proposer scale rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not execute_public_provisional:
        typer.echo(
            f"prepared={len(result.selected_execution_task_ids)} external_calls=0 "
            "maximum_concurrency=1 execute_requires_explicit_flag=true "
            "semantic_labels_created=0 supervision_eligible=false training_eligible=false"
        )
        return
    assert result.manifest is not None
    assert result.manifest_path is not None
    typer.echo(
        f"completed={result.manifest.completed_count} invoked={result.invoked_count} "
        f"reused={result.reused_count} statuses="
        f"{json.dumps(result.manifest.status_counts, sort_keys=True)} "
        f"manifest={result.manifest_path} sequential=true maximum_concurrency=1 "
        "outputs_provisional_only=true separate_family_validation_required=true "
        "semantic_labels_created=0 supervision_eligible=false training_eligible=false "
        "evaluation_eligible=false gate_credit_claimed=false"
    )


@app.command("summarize-lf022-codex-audit")
def summarize_lf022_codex_audit_command(
    checks_path: Annotated[
        Path,
        typer.Option("--checks", help="Exact checks.jsonl bound by the audit manifest."),
    ],
    audit_root: Annotated[
        Path,
        typer.Option("--audit-root", help="Completed immutable LF-022 Codex audit root."),
    ],
    output_json: Annotated[
        Path,
        typer.Option("--output-json", help="Destination for the verified JSON summary."),
    ] = Path("reports/generation/lf022_codex_audit_summary.json"),
    output_markdown: Annotated[
        Path,
        typer.Option("--output-markdown", help="Destination for the readable summary."),
    ] = Path("reports/generation/lf022_codex_audit_summary.md"),
    output_findings: Annotated[
        Path,
        typer.Option(
            "--output-findings",
            help="Destination for compact audit-only per-pair findings.",
        ),
    ] = Path("reports/generation/lf022_codex_audit_findings.jsonl"),
    parent_audit_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--parent-audit-root",
            help=(
                "Explicit immutable parent audit for copied completed items; "
                "repeat for composite audits."
            ),
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Verify and summarize a complete audit without creating labels."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_codex_audit import (
        LF022CodexAuditError,
        summarize_completed_lf022_codex_audit,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = summarize_completed_lf022_codex_audit(
            repo_root=paths.root,
            checks_path=anchored(checks_path),
            audit_root=anchored(audit_root),
            output_json_path=anchored(output_json),
            output_markdown_path=anchored(output_markdown),
            output_findings_path=anchored(output_findings),
            parent_audit_roots=tuple(anchored(path) for path in (parent_audit_root or [])),
        )
    except (LF022CodexAuditError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 Codex audit summary rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    summary = result.summary
    typer.echo(
        f"checks={summary.total_check_count} lean_valid={summary.lean_valid_check_count} "
        f"judged={summary.completed_judgment_count} "
        f"same_claim={summary.overall.same_claim_counts.get('same_claim', 0)} "
        f"not_same_claim={summary.overall.same_claim_counts.get('not_same_claim', 0)} "
        f"uncertain={summary.overall.same_claim_counts.get('uncertain', 0)} "
        f"summary={result.json_path} report={result.markdown_path} "
        f"findings={result.findings_path} "
        "audit_only=true human_labels_created=0 semantic_labels_created=0 "
        "silver_records_created=0 training_eligible=false evaluation_eligible=false "
        "gate_credit_claimed=false"
    )


@app.command("build-lf022-supervision-candidates")
def build_lf022_supervision_candidates_command(
    spec_path: Annotated[
        Path,
        typer.Option(
            "--spec",
            help=(
                "Canonical candidate-inventory spec. Schema v3 binds Lean-valid checks "
                "and may optionally bind a complete Codex diagnostic audit."
            ),
        ),
    ],
    spec_sha256: Annotated[
        str,
        typer.Option("--spec-sha256", help="Expected raw SHA-256 of --spec."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Immutable candidate-inventory output directory."),
    ],
    require_codex_diagnostic: Annotated[
        bool,
        typer.Option(
            "--require-codex-diagnostic",
            help=(
                "Fail unless the spec binds a complete replay-verified Codex diagnostic. "
                "This assertion never turns that diagnostic into a supervision vote."
            ),
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Inventory public Lean-valid pairs for later two-family judging."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_supervision_candidates import (
        LF022SupervisionCandidateError,
        build_lf022_supervision_candidate_inventory,
        write_lf022_supervision_candidate_inventory,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        records, manifest = build_lf022_supervision_candidate_inventory(
            repo_root=paths.root,
            spec_path=anchored(spec_path),
            expected_spec_sha256=spec_sha256,
        )
        diagnostic_status = manifest.codex_diagnostic_status or "complete"
        diagnostic_count = (
            manifest.codex_diagnostic_record_count
            if manifest.codex_diagnostic_record_count is not None
            else manifest.record_count
        )
        if require_codex_diagnostic and diagnostic_status != "complete":
            raise LF022SupervisionCandidateError(
                "--require-codex-diagnostic was set but the spec binds no Codex audit"
            )
        records_path, sample_path, summary_path, manifest_path = (
            write_lf022_supervision_candidate_inventory(
                output_dir=anchored(output_dir),
                records=records,
                manifest=manifest,
            )
        )
    except (LF022SupervisionCandidateError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 supervision candidate inventory rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"inventory_id={manifest.inventory_id} records={records_path} "
        f"public_sample={sample_path} summary={summary_path} manifest={manifest_path} "
        f"record_count={manifest.record_count} "
        f"dispatch_eligible_count={manifest.dispatch_eligible_count} "
        f"required_future_judge_call_count={manifest.required_future_judge_call_count} "
        f"codex_diagnostic_status={diagnostic_status} "
        f"codex_diagnostic_record_count={diagnostic_count} "
        "codex_weak_judge_votes=0 semantic_labels_created=0 silver_records_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
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


@app.command("reconcile-lf022-postgen")
def reconcile_lf022_postgen_command(
    manifest_path: Annotated[
        Path,
        typer.Option("--manifest", help="Frozen public LF-022 batch manifest JSON."),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Immutable reconciliation, retry-plan, and terminal-selector root.",
        ),
    ],
    require_offline_ready: Annotated[
        bool,
        typer.Option(
            "--require-offline-ready",
            help="Exit 3 after writing evidence when any task remains nonterminal.",
        ),
    ] = False,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Execution repository root override."),
    ] = None,
) -> None:
    """Partition terminal/error/missing tasks before LF-022 postprocessing."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_postgen_reconcile import (
        LF022PostgenReconciliationError,
        reconcile_lf022_postgen,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        result = reconcile_lf022_postgen(
            repo_root=paths.root,
            manifest_path=anchored(manifest_path),
            output_root=anchored(output_root),
        )
    except (LF022PostgenReconciliationError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 postgen reconciliation rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    report = result.reconciliation
    typer.echo(
        f"state={report.state} tasks={report.task_count} "
        f"terminal={len(report.terminal_task_ids)} "
        f"errors={len(report.error_task_ids)} missing={len(report.missing_task_ids)} "
        f"reconciliation={result.reconciliation_path} "
        f"retry_plan={result.retry_plan_path or 'none'} "
        f"terminal_selector={result.terminal_selector_path or 'none'} "
        "network_calls_this_run=0 semantic_labels_created=0 "
        "training_eligible=false evaluation_eligible=false gate_credit_claimed=false"
    )
    if require_offline_ready and report.state != "offline_ready":
        raise typer.Exit(code=3)


@app.command("verify-lf022-postgen-selector")
def verify_lf022_postgen_selector_command(
    selector_path: Annotated[
        Path,
        typer.Option("--selector", help="Immutable LF-022 postgen terminal selector JSON."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Execution repository root override."),
    ] = None,
) -> None:
    """Replay selected historical terminals and print the selector content ID.

    This verifies the frozen batch envelope plus every selected terminal's full
    persisted execution lineage.  It deliberately does not rerun the exhaustive
    current-policy source/admission audit for unselected batch tasks.
    """
    from leanfaith.config.paths import RepoPaths
    from leanfaith.generation.lf022_postgen_reconcile import (
        LF022PostgenReconciliationError,
        verify_lf022_postgen_terminal_selector_selected_only,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    anchored_selector = selector_path if selector_path.is_absolute() else paths.root / selector_path
    try:
        verified = verify_lf022_postgen_terminal_selector_selected_only(
            repo_root=paths.root,
            selector_path=anchored_selector,
        )
    except (LF022PostgenReconciliationError, OSError, ValueError) as exc:
        typer.echo(f"LF-022 postgen selector rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(verified.selector.selector_id)


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
    from leanfaith.transforms.v2_d0_n18_runtime import (
        V2D0N18Runtime,
        build_v2_d0_n18_runtime,
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
            | V2D0N18Runtime
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
        elif profile_id == "deterministic_v2_d0_n18_experimental":
            runtime = build_v2_d0_n18_runtime(paths.root, path=resolved_profile)
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
                workers=workers,
                memory_hard_limit_mb=memory_hard_limit_mb,
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


@app.command("materialize-deterministic-v2-e2-scale")
def materialize_deterministic_v2_e2_scale_command(
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
        typer.Option("--output-dir", help="Create-or-resume hash-bound E2 scale root."),
    ],
    profile_path: Annotated[
        Path,
        typer.Option("--profile", help="Exact experimental E2 execution profile."),
    ] = Path("configs/transformations/v2_e2_p15_experimental.yaml"),
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
    """Run/resume one LF-033 E2 profile through LeanServerPool."""
    import json
    from dataclasses import replace

    from leanfaith.config.paths import RepoPaths
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
    from leanfaith.lean.session_policy import ServerMode
    from leanfaith.schemas.theorem import TheoremRecord
    from leanfaith.transforms.v2_e2_runtime import build_v2_e2_runtime
    from leanfaith.transforms.v2_e2_scale_run import V2E2ScaleRunError, run_v2_e2_scale

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        resolved_theorems = anchored(theorem_path).resolve(strict=True)
        with resolved_theorems.open("rb") as handle:
            first_line = handle.readline()
        if not first_line.endswith(b"\n"):
            raise V2E2ScaleRunError("theorem partition is empty or lacks JSONL framing")
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
            artifacts = run_v2_e2_scale(
                backend=backend,
                runtime=build_v2_e2_runtime(
                    paths.root,
                    path=anchored(profile_path),
                ),
                theorem_path=resolved_theorems,
                representation_path=anchored(representation_path),
                project_dir=anchored(project_dir),
                import_header=import_header,
                output_dir=resolved_output,
                batch_size=batch_size,
                base_seed=base_seed,
                max_sources=max_sources,
                workers=workers,
                memory_hard_limit_mb=memory_hard_limit_mb,
            )
        finally:
            backend.close()
    except (OSError, ValueError, V2E2ScaleRunError) as exc:
        typer.echo(f"deterministic-v2 E2 scale rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic-v2 E2 scale complete; "
        f"results={artifacts.result_count} output={artifacts.output_dir} "
        f"manifest={artifacts.manifest_path} resolved_labels=0 promoted_items=0 "
        "training_eligible=false"
    )


@app.command("recover-deterministic-v2-e2-attempt")
def recover_deterministic_v2_e2_attempt_command(
    parent_root: Annotated[
        Path,
        typer.Option("--parent-root", help="Immutable E2 root containing one failed result."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable recovered root; must not exist."),
    ],
    target_line: Annotated[
        int,
        typer.Option("--target-line", min=1, help="Exact 1-based results.jsonl line."),
    ],
    import_header_path: Annotated[
        Path | None,
        typer.Option("--import-header", help="Exact import header used by the parent run."),
    ] = None,
    target_result_id: Annotated[
        str | None,
        typer.Option("--result-id", help="Exact candidate_infrastructure_error result ID."),
    ] = None,
    target_attempt_id: Annotated[
        str | None,
        typer.Option("--attempt-id", help="Exact failed transformation attempt ID."),
    ] = None,
    profile_path: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Optional exact profile; inferred from run spec by default.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Recover exactly one E2 infrastructure failure without mutating its parent."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.transforms.v2_e2_recovery import (
        V2E2RecoveryError,
        recover_v2_e2_attempt,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    try:
        artifacts = recover_v2_e2_attempt(
            parent_root=anchored(parent_root),
            output_dir=anchored(output_dir),
            repo_root=paths.root,
            import_header=(
                ""
                if import_header_path is None
                else anchored(import_header_path).read_text(encoding="utf-8")
            ),
            target_result_line_number=target_line,
            target_result_id=target_result_id,
            target_attempt_id=target_attempt_id,
            profile_path=(None if profile_path is None else anchored(profile_path)),
        )
    except (OSError, ValueError, V2E2RecoveryError) as exc:
        typer.echo(f"deterministic-v2 E2 recovery rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "deterministic-v2 E2 recovery complete; "
        f"output={artifacts.output_dir} replacement={artifacts.replacement_result_id} "
        f"receipt={artifacts.recovery_receipt_path} resolved_labels=0 promoted_items=0 "
        "training_eligible=false"
    )


@app.command("freeze-transform-source-subset")
def freeze_transform_source_subset_command(
    theorem_path: Annotated[
        Path,
        typer.Option(
            "--theorems",
            help="Aligned Gate-3 TheoremRecord JSONL, optionally using wrapped rows.",
        ),
    ],
    representation_path: Annotated[
        Path,
        typer.Option(
            "--representations",
            help="Aligned Gate-3 RepresentationRecord JSONL.",
        ),
    ],
    source: Annotated[
        str,
        typer.Option("--source", help="Exact TheoremRecord.source value to freeze."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="New immutable output directory, or an exact prior replay.",
        ),
    ],
) -> None:
    """Freeze a canonical source-only subset from aligned Gate-3 inputs."""
    from leanfaith.transforms.source_subset_freeze import (
        freeze_transform_source_subset,
    )

    try:
        artifacts = freeze_transform_source_subset(
            theorem_path=theorem_path,
            representation_path=representation_path,
            source=source,
            output_dir=output_dir,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"source-subset freeze rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "source-subset freeze complete; "
        f"source={artifacts.source} records={artifacts.record_count} "
        f"context={artifacts.context_id} replayed={str(artifacts.replayed).lower()} "
        f"manifest={artifacts.manifest_path}"
    )


@app.command("freeze-experimental-machine-supervision")
def freeze_experimental_machine_supervision_command(
    audit_dir: Annotated[
        Path,
        typer.Option(
            "--audit-dir",
            help="Frozen deterministic provisional-pair audit directory.",
        ),
    ],
    positive_seed_dir: Annotated[
        Path,
        typer.Option(
            "--positive-seed-dir",
            help="Frozen certificate-backed E2 composition-seed directory.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="New immutable corpus directory, or an exact prior replay.",
        ),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Pinned experimental-corpus selection policy.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Freeze the opt-in 2k machine-supervision learning corpus."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.datasets.experimental_machine_supervision import (
        ExperimentalMachineSupervisionError,
        freeze_experimental_machine_supervision,
        load_experimental_machine_supervision_config,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    selected_config = (
        paths.root / "configs/data/experimental_machine_supervision_mathlib_2k_v1.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        loaded = load_experimental_machine_supervision_config(selected_config)
        artifacts = freeze_experimental_machine_supervision(
            repo_root=paths.root,
            audit_dir=anchored(audit_dir),
            positive_seed_dir=anchored(positive_seed_dir),
            output_dir=anchored(output_dir),
            config=loaded.config,
            config_hash=loaded.config_hash,
        )
    except (OSError, ValueError, ExperimentalMachineSupervisionError) as exc:
        typer.echo(f"experimental machine-supervision freeze rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental machine-supervision corpus ready; "
        f"dataset={artifacts.dataset_id} records={artifacts.record_count} "
        f"replayed={str(artifacts.replayed).lower()} manifest={artifacts.manifest_path} "
        "scientific_training_eligible=false evaluation_eligible=false"
    )


@app.command("freeze-experimental-first-hop-projection")
def freeze_experimental_first_hop_projection_command(
    audit_dir: Annotated[
        Path,
        typer.Option(
            "--audit-dir",
            help="Frozen deterministic provisional-pair audit directory.",
        ),
    ],
    positive_seed_dir: Annotated[
        Path,
        typer.Option(
            "--positive-seed-dir",
            help="Frozen certificate-backed E2 composition-seed directory.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help=(
                "Absolute external directory for a new immutable projection, "
                "or an exact prior replay."
            ),
        ),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Pinned full first-hop projection policy."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Replay and freeze the complete deterministic first-hop inventory."""
    from leanfaith.config.loading import load_config
    from leanfaith.config.paths import RepoPaths
    from leanfaith.datasets.experimental_first_hop_projection import (
        ExperimentalFirstHopProjectionConfig,
        ExperimentalFirstHopProjectionError,
        freeze_experimental_first_hop_projection,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    selected_config = (
        paths.root / "configs/data/experimental_first_hop_projection_full_v1.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be an absolute path outside the repository")
        loaded = load_config(selected_config, ExperimentalFirstHopProjectionConfig)
        artifacts = freeze_experimental_first_hop_projection(
            repo_root=paths.root,
            audit_dir=anchored(audit_dir),
            positive_seed_dir=anchored(positive_seed_dir),
            output_dir=output_dir,
            config=loaded.config,
            config_hash=loaded.config_hash,
        )
    except (OSError, ValueError, ExperimentalFirstHopProjectionError) as exc:
        typer.echo(f"experimental first-hop projection rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental first-hop projection ready; "
        f"projection={artifacts.projection_id} inventory={artifacts.inventory_count} "
        f"selectable={artifacts.selectable_count} "
        f"replayed={str(artifacts.replayed).lower()} manifest={artifacts.manifest_path} "
        "semantic_labels_created=false scientific_training_eligible=false"
    )


@app.command("verify-experimental-first-hop-projection")
def verify_experimental_first_hop_projection_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen first-hop projection directory."),
    ],
    skip_external_inputs: Annotated[
        bool,
        typer.Option(
            "--skip-external-inputs",
            help="Verify projection bytes without re-reading external lineage inputs.",
        ),
    ] = False,
) -> None:
    """Verify a full first-hop projection without Lean or model calls."""
    from leanfaith.datasets.experimental_first_hop_projection import (
        ExperimentalFirstHopProjectionError,
        verify_experimental_first_hop_projection,
    )

    try:
        manifest = verify_experimental_first_hop_projection(
            output_dir,
            verify_external_inputs=not skip_external_inputs,
        )
    except (OSError, ValueError, ExperimentalFirstHopProjectionError) as exc:
        typer.echo(f"experimental first-hop verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental first-hop projection verified; "
        f"projection={manifest.projection_id} inventory={manifest.inventory_count} "
        f"selectable={manifest.selectable_count} "
        "semantic_labels_created=false scientific_training_eligible=false"
    )


@app.command("freeze-experimental-mixed-supervision")
def freeze_experimental_mixed_supervision_command(
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Absolute external directory for the new immutable mixed corpus.",
        ),
    ],
    first_hop_projection_dir: Annotated[
        Path,
        typer.Option(
            "--first-hop-projection-dir",
            help="Verified deterministic first-hop projection directory.",
        ),
    ],
    audit_spec_path: Annotated[
        Path,
        typer.Option(
            "--audit-spec",
            help=(
                "JSON file containing a list of LF-022 audit objects with name, "
                "repo_root, checks_path, audit_root, and optional parent_audit_roots."
            ),
        ),
    ],
    source_theorem_path: Annotated[
        list[Path],
        typer.Option(
            "--source-theorems",
            help="Canonical theorem JSONL partition; repeat as needed.",
        ),
    ],
    source_representation_path: Annotated[
        list[Path],
        typer.Option(
            "--source-representations",
            help="Canonical representation JSONL partition; repeat as needed.",
        ),
    ],
    composition_full_run_root: Annotated[
        Path | None,
        typer.Option(
            "--composition-full-run-root",
            help=(
                "Completed thirteen-family composition run; requires the other composition roots."
            ),
        ),
    ] = None,
    composition_seed_dir: Annotated[
        Path | None,
        typer.Option(
            "--composition-seed-dir",
            help="Immutable composition seed directory bound by the full run.",
        ),
    ] = None,
    composition_postprocess_root: Annotated[
        Path | None,
        typer.Option(
            "--composition-postprocess-root",
            help="Completed chains/unique_pairs/export composition postprocess root.",
        ),
    ] = None,
    composition_source_theorem_paths: Annotated[
        list[Path] | None,
        typer.Option(
            "--composition-source-theorems",
            help=(
                "Original source theorem partition bound by the composition export; "
                "repeat for every partition."
            ),
        ),
    ] = None,
    composition_source_representation_paths: Annotated[
        list[Path] | None,
        typer.Option(
            "--composition-source-representations",
            help=(
                "Original source representation partition bound by the composition export; "
                "repeat for every partition."
            ),
        ),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Pinned mixed-supervision policy."),
    ] = None,
    benchmark_manifest_path: Annotated[
        Path | None,
        typer.Option("--benchmark-manifest", help="Benchmark-registry manifest override."),
    ] = None,
    benchmark_manifest_sha256: Annotated[
        str | None,
        typer.Option(
            "--benchmark-manifest-sha256",
            help="Expected registry-manifest hash when authorization is not used.",
        ),
    ] = None,
    benchmark_authorization_path: Annotated[
        Path | None,
        typer.Option(
            "--benchmark-authorization",
            help="LF-016 authorization override for the active benchmark registry.",
        ),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Verify every source and freeze the first-hop plus LF-022 proxy corpus."""
    from typing import cast

    from pydantic import BaseModel, ConfigDict, Field

    from leanfaith.config.paths import RepoPaths
    from leanfaith.datasets.experimental_mixed_supervision import (
        ExperimentalMixedSupervisionError,
    )
    from leanfaith.datasets.experimental_mixed_supervision_orchestration import (
        ExperimentalCompositionSource,
        ExperimentalLF022AuditSource,
        freeze_experimental_mixed_supervision_from_artifacts,
    )

    class AuditSpec(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        name: str = Field(min_length=1)
        repo_root: Path
        checks_path: Path
        audit_root: Path
        parent_audit_roots: tuple[Path, ...] = ()

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path, *, base: Path | None = None) -> Path:
        if path.is_absolute():
            return path
        return (base or paths.root) / path

    selected_config = (
        paths.root / "configs/data/experimental_mixed_supervision_firsthop_lf022_v1.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be an absolute path outside the repository")
        raw_specs = json.loads(anchored(audit_spec_path).read_text(encoding="utf-8"))
        if not isinstance(raw_specs, list) or not raw_specs:
            raise ValueError("audit spec must be a nonempty JSON list")
        parsed_specs = tuple(AuditSpec.model_validate(value) for value in raw_specs)
        audits = tuple(
            ExperimentalLF022AuditSource(
                name=spec.name,
                repo_root=anchored(spec.repo_root),
                checks_path=anchored(spec.checks_path, base=anchored(spec.repo_root)),
                audit_root=anchored(spec.audit_root, base=anchored(spec.repo_root)),
                parent_audit_roots=tuple(
                    anchored(value, base=anchored(spec.repo_root))
                    for value in spec.parent_audit_roots
                ),
            )
            for spec in parsed_specs
        )
        composition_values = (
            composition_full_run_root,
            composition_seed_dir,
            composition_postprocess_root,
            composition_source_theorem_paths,
            composition_source_representation_paths,
        )
        if any(value is not None for value in composition_values) and not all(
            value is not None for value in composition_values
        ):
            raise ValueError(
                "composition roots and original source partitions must be provided together"
            )
        composition_source = (
            None
            if composition_full_run_root is None
            else ExperimentalCompositionSource(
                full_run_root=anchored(composition_full_run_root),
                seed_dir=anchored(cast(Path, composition_seed_dir)),
                postprocess_root=anchored(cast(Path, composition_postprocess_root)),
                source_theorem_paths=tuple(
                    anchored(path) for path in cast(list[Path], composition_source_theorem_paths)
                ),
                source_representation_paths=tuple(
                    anchored(path)
                    for path in cast(list[Path], composition_source_representation_paths)
                ),
            )
        )
        result = freeze_experimental_mixed_supervision_from_artifacts(
            repo_root=paths.root,
            output_dir=output_dir,
            config_path=selected_config,
            first_hop_projection_dir=anchored(first_hop_projection_dir),
            lf022_audits=audits,
            source_theorem_paths=tuple(anchored(path) for path in source_theorem_path),
            source_representation_paths=tuple(
                anchored(path) for path in source_representation_path
            ),
            composition_source=composition_source,
            benchmark_manifest_path=(
                None if benchmark_manifest_path is None else anchored(benchmark_manifest_path)
            ),
            benchmark_expected_manifest_sha256=benchmark_manifest_sha256,
            benchmark_authorization_path=(
                None
                if benchmark_authorization_path is None
                else anchored(benchmark_authorization_path)
            ),
        )
    except (OSError, ValueError, ExperimentalMixedSupervisionError) as exc:
        typer.echo(f"experimental mixed-supervision freeze rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    artifacts = result.artifacts
    typer.echo(
        "experimental mixed-supervision corpus ready; "
        f"dataset={artifacts.dataset_id} records={artifacts.record_count} "
        f"excluded={artifacts.exclusion_count} "
        f"first_hop_candidates={result.first_hop_candidate_count} "
        f"lf022_candidates={result.lf022_candidate_count} "
        f"lf022_judgments={result.lf022_judgment_count} "
        f"composition_export={result.composition_export_count} "
        f"composition_candidates={result.composition_candidate_count} "
        f"composition_exclusions={result.composition_exclusion_count} "
        f"replayed={str(artifacts.replayed).lower()} manifest={artifacts.manifest_path} "
        "proxy_only=true scientific_training_eligible=false evaluation_eligible=false"
    )


@app.command("verify-experimental-mixed-supervision")
def verify_experimental_mixed_supervision_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen mixed-supervision corpus directory."),
    ],
    skip_external_inputs: Annotated[
        bool,
        typer.Option(
            "--skip-external-inputs",
            help="Verify corpus bytes without re-reading external lineage inputs.",
        ),
    ] = False,
) -> None:
    """Verify mixed-corpus hashes, ancestry splits, and source bindings."""
    from leanfaith.datasets.experimental_mixed_supervision import (
        ExperimentalMixedSupervisionError,
        verify_experimental_mixed_supervision,
    )

    try:
        manifest = verify_experimental_mixed_supervision(
            output_dir,
            verify_external_inputs=not skip_external_inputs,
        )
    except (OSError, ValueError, ExperimentalMixedSupervisionError) as exc:
        typer.echo(f"experimental mixed-supervision verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental mixed-supervision corpus verified; "
        f"dataset={manifest.dataset_id} records={manifest.record_count} "
        f"excluded={manifest.exclusion_count} components={manifest.component_count} "
        "proxy_only=true scientific_training_eligible=false evaluation_eligible=false"
    )


@app.command("verify-experimental-machine-supervision")
def verify_experimental_machine_supervision_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen experimental-corpus directory."),
    ],
    skip_external_inputs: Annotated[
        bool,
        typer.Option(
            "--skip-external-inputs",
            help="Verify corpus bytes without re-reading its external lineage inputs.",
        ),
    ] = False,
) -> None:
    """Verify corpus hashes, quotas, ancestry splits, and opt-in policy."""
    from leanfaith.datasets.experimental_machine_supervision import (
        ExperimentalMachineSupervisionError,
        verify_experimental_machine_supervision,
    )

    try:
        manifest = verify_experimental_machine_supervision(
            output_dir,
            verify_external_inputs=not skip_external_inputs,
        )
    except (OSError, ValueError, ExperimentalMachineSupervisionError) as exc:
        typer.echo(f"experimental machine-supervision verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental machine-supervision corpus verified; "
        f"dataset={manifest.dataset_id} records={manifest.record_count} "
        "scientific_training_eligible=false evaluation_eligible=false"
    )


@app.command("run-experimental-scalar-learning-curve")
def run_experimental_scalar_learning_curve_command(
    dataset_dir: Annotated[
        Path,
        typer.Option("--dataset-dir", help="Frozen experimental machine-supervision corpus."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable diagnostic output, or exact replay."),
    ],
    allow_experimental_machine_supervision: Annotated[
        bool,
        typer.Option(
            "--allow-experimental-machine-supervision",
            help="Explicitly admit provisional machine intentions for this diagnostic only.",
        ),
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Pinned diagnostic learning-curve policy."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Fit the opt-in scalar pseudo-target curve; never make a semantic claim."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.experimental_scalar_learning_curve import (
        ExperimentalScalarLearningCurveError,
        load_experimental_scalar_learning_curve_config,
        run_experimental_scalar_learning_curve,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    selected_config = (
        paths.root / "configs/models/experimental_scalar_learning_curve_v1.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        loaded = load_experimental_scalar_learning_curve_config(selected_config)
        artifacts = run_experimental_scalar_learning_curve(
            repo_root=paths.root,
            dataset_dir=anchored(dataset_dir),
            output_dir=anchored(output_dir),
            config=loaded.config,
            config_hash=loaded.config_hash,
            allow_experimental_machine_supervision=allow_experimental_machine_supervision,
        )
    except (OSError, ValueError, ExperimentalScalarLearningCurveError) as exc:
        typer.echo(f"experimental scalar learning curve rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental scalar learning curve ready; "
        f"experiment={artifacts.experiment_id} models={artifacts.model_count} "
        f"predictions={artifacts.prediction_count} replayed={str(artifacts.replayed).lower()} "
        "semantic_prediction=false model_selection_eligible=false evaluation_eligible=false"
    )


@app.command("verify-experimental-scalar-learning-curve")
def verify_experimental_scalar_learning_curve_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen diagnostic learning-curve directory."),
    ],
    dataset_dir: Annotated[
        Path | None,
        typer.Option(
            "--dataset-dir",
            help="Dataset override; otherwise use the manifest-bound absolute path.",
        ),
    ] = None,
) -> None:
    """Verify diagnostic bytes, lineage, prefixes, predictions, and metrics."""
    from leanfaith.models.experimental_scalar_learning_curve import (
        ExperimentalScalarLearningCurveError,
        verify_experimental_scalar_learning_curve,
    )

    try:
        manifest = verify_experimental_scalar_learning_curve(
            output_dir,
            dataset_dir=dataset_dir,
        )
    except (OSError, ValueError, ExperimentalScalarLearningCurveError) as exc:
        typer.echo(f"experimental scalar learning-curve verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental scalar learning curve verified; "
        f"experiment={manifest.experiment_id} models={manifest.model_count} "
        f"predictions={manifest.prediction_count} "
        "semantic_prediction=false model_selection_eligible=false evaluation_eligible=false"
    )


@app.command("run-experimental-mixed-scalar-learning-curve")
def run_experimental_mixed_scalar_learning_curve_command(
    dataset_dir: Annotated[
        Path,
        typer.Option("--dataset-dir", help="Frozen experimental mixed-proxy corpus."),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable diagnostic output, or exact replay."),
    ],
    allow_experimental_mixed_supervision: Annotated[
        bool,
        typer.Option(
            "--allow-experimental-mixed-supervision",
            help="Explicitly admit mixed machine proxies for this diagnostic only.",
        ),
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Pinned mixed scalar learning-curve policy."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Fit the opt-in mixed-proxy scalar curve; never make a semantic claim."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.experimental_mixed_scalar_learning_curve import (
        ExperimentalMixedScalarLearningCurveError,
        load_experimental_mixed_scalar_learning_curve_config,
        run_experimental_mixed_scalar_learning_curve,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    selected_config = (
        paths.root / "configs/models/experimental_mixed_scalar_learning_curve_v2.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be an absolute path outside the repository")
        loaded = load_experimental_mixed_scalar_learning_curve_config(selected_config)
        artifacts = run_experimental_mixed_scalar_learning_curve(
            repo_root=paths.root,
            dataset_dir=anchored(dataset_dir),
            output_dir=output_dir,
            config=loaded.config,
            config_hash=loaded.config_hash,
            allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        )
    except (OSError, ValueError, ExperimentalMixedScalarLearningCurveError) as exc:
        typer.echo(f"experimental mixed scalar learning curve rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental mixed scalar learning curve ready; "
        f"experiment={artifacts.experiment_id} prefixes={artifacts.prefix_count} "
        f"models={artifacts.model_count} predictions={artifacts.prediction_count} "
        f"replayed={str(artifacts.replayed).lower()} proxy_only=true "
        "semantic_prediction=false scientific_training_eligible=false "
        "model_selection_eligible=false evaluation_eligible=false"
    )


@app.command("verify-experimental-mixed-scalar-learning-curve")
def verify_experimental_mixed_scalar_learning_curve_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen mixed scalar learning-curve directory."),
    ],
    dataset_dir: Annotated[
        Path | None,
        typer.Option(
            "--dataset-dir",
            help="Dataset override; otherwise use the manifest-bound absolute path.",
        ),
    ] = None,
    repository_root: Annotated[
        Path | None,
        typer.Option(
            "--repository-root",
            help=(
                "Clean code-equivalent repository override; otherwise use the "
                "manifest-bound producer path."
            ),
        ),
    ] = None,
) -> None:
    """Refit and verify mixed scalar bytes, prefixes, predictions, and metrics."""
    from leanfaith.models.experimental_mixed_scalar_learning_curve import (
        ExperimentalMixedScalarLearningCurveError,
        verify_experimental_mixed_scalar_learning_curve,
    )

    try:
        manifest = verify_experimental_mixed_scalar_learning_curve(
            output_dir,
            dataset_dir=dataset_dir,
            repository_root=repository_root,
        )
    except (OSError, ValueError, ExperimentalMixedScalarLearningCurveError) as exc:
        typer.echo(f"experimental mixed scalar verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "experimental mixed scalar learning curve verified; "
        f"experiment={manifest.experiment_id} prefixes={manifest.prefix_count} "
        f"models={manifest.model_count} predictions={manifest.prediction_count} "
        "proxy_only=true semantic_prediction=false scientific_training_eligible=false "
        "model_selection_eligible=false evaluation_eligible=false"
    )


@app.command("run-tokenizer-sections")
def run_tokenizer_sections_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Private immutable semantic-section output, or resume."),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Pinned tokenizer-section derivation configuration."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Run or resume the frozen Lean-meta semantic-section derivation."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.tokenizer_sections import (
        TokenizerSectionDerivationError,
        load_tokenizer_section_config,
        run_tokenizer_section_derivation,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    selected_config = (
        paths.root / "configs/models/tokenizer_sections_v1.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be an absolute private path")
        loaded = load_tokenizer_section_config(selected_config)
        manifest = run_tokenizer_section_derivation(
            repo_root=paths.root,
            output_dir=output_dir,
            config=loaded.config,
        )
    except (OSError, ValueError, TokenizerSectionDerivationError) as exc:
        typer.echo(f"tokenizer-section derivation rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "tokenizer sections ready; "
        f"derivation={manifest.derivation_id} records={manifest.record_count} "
        f"preflight={len(manifest.preflight_theorem_ids)} private=true release_eligible=false"
    )


@app.command("verify-tokenizer-sections")
def verify_tokenizer_sections_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen semantic-section output directory."),
    ],
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Verify section bytes, environment pins, ordering, and derivation identity."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.tokenizer_sections import (
        TokenizerSectionDerivationError,
        verify_tokenizer_section_derivation,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be an absolute private path")
        manifest = verify_tokenizer_section_derivation(output_dir, repo_root=paths.root)
    except (OSError, ValueError, TokenizerSectionDerivationError) as exc:
        typer.echo(f"tokenizer-section verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "tokenizer sections verified; "
        f"derivation={manifest.derivation_id} records={manifest.record_count} "
        "private=true release_eligible=false"
    )


@app.command("run-tokenizer-audit")
def run_tokenizer_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="New immutable tokenizer-audit output, or replay."),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Pinned tokenizer-audit configuration."),
    ] = None,
    root_dir: Annotated[
        Path | None,
        typer.Option("--root", help="Repository root override."),
    ] = None,
) -> None:
    """Audit the four pinned tokenizers; make no scientific winner selection."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.tokenizer_audit import (
        TokenizerAuditError,
        load_tokenizer_audit_config,
        run_tokenizer_audit,
    )

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)

    def anchored(path: Path) -> Path:
        return path if path.is_absolute() else paths.root / path

    selected_config = (
        paths.root / "configs/models/tokenizer_audit_v1.yaml"
        if config_path is None
        else anchored(config_path)
    )
    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be an absolute path outside the repository")
        loaded = load_tokenizer_audit_config(selected_config)
        artifacts = run_tokenizer_audit(
            repo_root=paths.root,
            output_dir=output_dir,
            config=loaded.config,
            config_hash=loaded.config_hash,
        )
    except (OSError, ValueError, TokenizerAuditError) as exc:
        typer.echo(f"tokenizer audit rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "tokenizer audit ready; "
        f"audit={artifacts.audit_id} selected_length={artifacts.selected_length} "
        f"eligible={','.join(artifacts.eligible_candidates)} "
        f"replayed={str(artifacts.replayed).lower()} scientific_winner_selected=false"
    )


@app.command("verify-tokenizer-audit")
def verify_tokenizer_audit_command(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Frozen tokenizer-audit output directory."),
    ],
    replay: Annotated[
        bool,
        typer.Option("--replay/--no-replay", help="Recompute every frozen output byte."),
    ] = True,
) -> None:
    """Verify tokenizer-audit hashes and, by default, exact computation replay."""
    from leanfaith.models.tokenizer_audit import TokenizerAuditError, verify_tokenizer_audit

    try:
        manifest = verify_tokenizer_audit(output_dir, replay=replay)
    except (OSError, ValueError, TokenizerAuditError) as exc:
        typer.echo(f"tokenizer audit verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "tokenizer audit verified; "
        f"audit={manifest.audit_id} selected_length={manifest.selected_length} "
        "scientific_winner_selected=false"
    )


@app.command("prepare-m0-proxy")
def prepare_m0_proxy_command(
    corpus_dir: Annotated[Path, typer.Option("--corpus-dir", help="Frozen mixed corpus.")],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Completed tokenizer audit.")
    ],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="New immutable M0 input output.")
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Frozen proxy-only M0 protocol.")
    ] = None,
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
    allow_experimental_mixed_supervision: Annotated[
        bool,
        typer.Option(
            "--allow-experimental-mixed-supervision",
            help="Acknowledge that labels are machine proxies, not semantic gold.",
        ),
    ] = False,
) -> None:
    """Prepare exact tokenizer-bound M0 inputs; never train or select a model."""
    from leanfaith.config.hashing import hash_file
    from leanfaith.config.paths import RepoPaths
    from leanfaith.datasets.experimental_mixed_supervision import (
        verify_experimental_mixed_supervision,
    )
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        M0ProxyRunBinding,
        load_experimental_m0_proxy_config,
        prepare_experimental_m0_proxy_inputs,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    selected_config = config_path or Path(
        "configs/models/experimental_m0_dual_encoder_proxy_v1.yaml"
    )
    if not selected_config.is_absolute():
        selected_config = paths.root / selected_config
    try:
        if (
            not corpus_dir.is_absolute()
            or not tokenizer_audit_dir.is_absolute()
            or not output_dir.is_absolute()
        ):
            raise ValueError("all M0 artifact paths must be absolute")
        protocol = load_experimental_m0_proxy_config(selected_config).config
        corpus = verify_experimental_mixed_supervision(corpus_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        binding = M0ProxyRunBinding(
            corpus_dir=str(corpus_dir.resolve()),
            dataset_id=corpus.dataset_id,
            corpus_manifest_sha256=hash_file(corpus_dir / "manifest.json"),
            tokenizer_audit_dir=str(tokenizer_audit_dir.resolve()),
            tokenizer_audit_id=audit.audit_id,
            tokenizer_audit_manifest_sha256=hash_file(tokenizer_audit_dir / "manifest.json"),
            tokenizer_audit_summary_sha256=hash_file(tokenizer_audit_dir / "summary.json"),
        )
        artifacts = prepare_experimental_m0_proxy_inputs(
            repository_root=paths.root,
            output_dir=output_dir,
            protocol=protocol,
            run_binding=binding,
            allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
        )
    except (OSError, ValueError, ExperimentalM0ProxyError) as exc:
        typer.echo(f"M0 proxy preparation rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M0 proxy inputs ready; artifact={artifacts.artifact_id} "
        f"records={artifacts.record_count} trainable={artifacts.training_record_count} "
        f"replayed={str(artifacts.replayed).lower()} semantic_prediction=false"
    )


@app.command("verify-m0-proxy")
def verify_m0_proxy_command(
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Frozen M0 input output.")],
) -> None:
    """Verify every byte and accounting invariant of prepared M0 proxy inputs."""
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        verify_experimental_m0_proxy_inputs,
    )

    try:
        if not output_dir.is_absolute():
            raise ValueError("--output-dir must be absolute")
        manifest = verify_experimental_m0_proxy_inputs(output_dir)
    except (OSError, ValueError, ExperimentalM0ProxyError) as exc:
        typer.echo(f"M0 proxy verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M0 proxy inputs verified; artifact={manifest.artifact_id} "
        f"records={manifest.record_count} semantic_prediction=false"
    )


@app.command("train-m0-proxy")
def train_m0_proxy_command(
    prepared_input_dir: Annotated[
        Path, typer.Option("--prepared-input-dir", help="Verified prepared M0 inputs.")
    ],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Exact completed tokenizer audit.")
    ],
    checkpoint_dir: Annotated[
        Path, typer.Option("--checkpoint-dir", help="Exact pinned local model snapshot.")
    ],
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Immutable training output.")],
    device: Annotated[
        str, typer.Option("--device", help="Torch device, for example cuda.")
    ] = "cpu",
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
    allow_experimental_mixed_supervision: Annotated[
        bool,
        typer.Option(
            "--allow-experimental-mixed-supervision",
            help="Acknowledge proxy-only non-scientific training.",
        ),
    ] = False,
) -> None:
    """Load exact trusted bytes and execute one proxy-only M0 epoch."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        bind_local_modernbert_checkpoint,
        train_m0_proxy_one_epoch,
        verify_experimental_m0_proxy_inputs,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        for path in (prepared_input_dir, tokenizer_audit_dir, checkpoint_dir, output_dir):
            if not path.is_absolute():
                raise ValueError("all M0 artifact paths must be absolute")
        prepared = verify_experimental_m0_proxy_inputs(prepared_input_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        snapshot = audit.snapshots[prepared.tokenizer_decision.candidate_key]
        checkpoint = bind_local_modernbert_checkpoint(
            checkpoint_dir, protocol=prepared.protocol, audited_tokenizer_snapshot=snapshot
        )
        artifacts = train_m0_proxy_one_epoch(
            repository_root=paths.root,
            prepared_input_dir=prepared_input_dir,
            output_dir=output_dir,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
            device=device,
        )
    except (KeyError, OSError, ValueError, ExperimentalM0ProxyError) as exc:
        typer.echo(f"M0 proxy training rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M0 proxy training ready; artifact={artifacts.artifact_id} "
        f"steps={artifacts.optimizer_steps} exposed={artifacts.examples_exposed} "
        f"replayed={str(artifacts.replayed).lower()} semantic_prediction=false"
    )


@app.command("verify-m0-training")
def verify_m0_training_command(
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Frozen M0 training output.")],
    prepared_input_dir: Annotated[
        Path, typer.Option("--prepared-input-dir", help="Exact prepared M0 inputs.")
    ],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Exact completed tokenizer audit.")
    ],
    checkpoint_dir: Annotated[
        Path, typer.Option("--checkpoint-dir", help="Exact pinned local model snapshot.")
    ],
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
) -> None:
    """Verify training bytes against exact code, inputs, tokenizer, and checkpoint."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        bind_local_modernbert_checkpoint,
        verify_experimental_m0_proxy_inputs,
        verify_m0_proxy_training,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    try:
        for path in (output_dir, prepared_input_dir, tokenizer_audit_dir, checkpoint_dir):
            if not path.is_absolute():
                raise ValueError("all M0 artifact paths must be absolute")
        prepared = verify_experimental_m0_proxy_inputs(prepared_input_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        snapshot = audit.snapshots[prepared.tokenizer_decision.candidate_key]
        checkpoint = bind_local_modernbert_checkpoint(
            checkpoint_dir, protocol=prepared.protocol, audited_tokenizer_snapshot=snapshot
        )
        manifest = verify_m0_proxy_training(
            output_dir,
            repository_root=paths.root,
            prepared_input_dir=prepared_input_dir,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
        )
    except (KeyError, OSError, ValueError, ExperimentalM0ProxyError) as exc:
        typer.echo(f"M0 training verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M0 proxy training verified; artifact={manifest.artifact_id} "
        f"steps={manifest.optimizer_steps} semantic_prediction=false"
    )


@app.command("train-m1-proxy")
def train_m1_proxy_command(
    prepared_input_dir: Annotated[
        Path, typer.Option("--prepared-input-dir", help="Verified frozen M0 proxy inputs.")
    ],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Exact completed tokenizer audit.")
    ],
    checkpoint_dir: Annotated[
        Path, typer.Option("--checkpoint-dir", help="Exact pinned local model snapshot.")
    ],
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Immutable M1 output.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Frozen M1 proxy protocol config.")
    ] = None,
    device: Annotated[
        str, typer.Option("--device", help="Torch device, for example cuda.")
    ] = "cpu",
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
    allow_experimental_mixed_supervision: Annotated[
        bool,
        typer.Option(
            "--allow-experimental-mixed-supervision",
            help="Acknowledge proxy-only non-scientific training.",
        ),
    ] = False,
) -> None:
    """Execute one packed cross-encoder epoch over exact M0 proxy inputs."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        bind_local_modernbert_checkpoint,
        verify_experimental_m0_proxy_inputs,
    )
    from leanfaith.models.m1_cross_encoder import (
        ExperimentalM1ProxyError,
        load_experimental_m1_proxy_config,
        train_m1_proxy_one_epoch,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    selected_config = config_path or Path(
        "configs/models/experimental_m1_cross_encoder_proxy_v1.yaml"
    )
    if not selected_config.is_absolute():
        selected_config = paths.root / selected_config
    try:
        for path in (prepared_input_dir, tokenizer_audit_dir, checkpoint_dir, output_dir):
            if not path.is_absolute():
                raise ValueError("all M1 artifact paths must be absolute")
        prepared = verify_experimental_m0_proxy_inputs(prepared_input_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        snapshot = audit.snapshots[prepared.tokenizer_decision.candidate_key]
        checkpoint = bind_local_modernbert_checkpoint(
            checkpoint_dir,
            protocol=prepared.protocol,
            audited_tokenizer_snapshot=snapshot,
        )
        protocol = load_experimental_m1_proxy_config(selected_config).config
        artifacts = train_m1_proxy_one_epoch(
            repository_root=paths.root,
            prepared_input_dir=prepared_input_dir,
            output_dir=output_dir,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
            allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
            device=device,
        )
    except (
        KeyError,
        OSError,
        ValueError,
        ExperimentalM0ProxyError,
        ExperimentalM1ProxyError,
    ) as exc:
        typer.echo(f"M1 proxy training rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M1 proxy training ready; artifact={artifacts.artifact_id} "
        f"steps={artifacts.optimizer_steps} exposed={artifacts.examples_exposed} "
        f"replayed={str(artifacts.replayed).lower()} semantic_prediction=false"
    )


@app.command("verify-m1-training")
def verify_m1_training_command(
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Frozen M1 output.")],
    prepared_input_dir: Annotated[
        Path, typer.Option("--prepared-input-dir", help="Exact frozen M0 proxy inputs.")
    ],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Exact completed tokenizer audit.")
    ],
    checkpoint_dir: Annotated[
        Path, typer.Option("--checkpoint-dir", help="Exact pinned local model snapshot.")
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Frozen M1 proxy protocol config.")
    ] = None,
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
) -> None:
    """Verify M1 bytes against exact code, inputs, tokenizer, and checkpoint."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        bind_local_modernbert_checkpoint,
        verify_experimental_m0_proxy_inputs,
    )
    from leanfaith.models.m1_cross_encoder import (
        ExperimentalM1ProxyError,
        load_experimental_m1_proxy_config,
        verify_m1_proxy_training,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    selected_config = config_path or Path(
        "configs/models/experimental_m1_cross_encoder_proxy_v1.yaml"
    )
    if not selected_config.is_absolute():
        selected_config = paths.root / selected_config
    try:
        for path in (output_dir, prepared_input_dir, tokenizer_audit_dir, checkpoint_dir):
            if not path.is_absolute():
                raise ValueError("all M1 artifact paths must be absolute")
        prepared = verify_experimental_m0_proxy_inputs(prepared_input_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        snapshot = audit.snapshots[prepared.tokenizer_decision.candidate_key]
        checkpoint = bind_local_modernbert_checkpoint(
            checkpoint_dir,
            protocol=prepared.protocol,
            audited_tokenizer_snapshot=snapshot,
        )
        protocol = load_experimental_m1_proxy_config(selected_config).config
        manifest = verify_m1_proxy_training(
            output_dir,
            repository_root=paths.root,
            prepared_input_dir=prepared_input_dir,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
        )
    except (
        KeyError,
        OSError,
        ValueError,
        ExperimentalM0ProxyError,
        ExperimentalM1ProxyError,
    ) as exc:
        typer.echo(f"M1 training verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M1 proxy training verified; artifact={manifest.artifact_id} "
        f"steps={manifest.optimizer_steps} semantic_prediction=false"
    )


@app.command("train-m2-proxy")
def train_m2_proxy_command(
    prepared_input_dir: Annotated[
        Path, typer.Option("--prepared-input-dir", help="Verified frozen M0 proxy inputs.")
    ],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Exact completed tokenizer audit.")
    ],
    checkpoint_dir: Annotated[
        Path, typer.Option("--checkpoint-dir", help="Exact pinned local model snapshot.")
    ],
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Immutable M2 output.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Frozen M2 proxy protocol config.")
    ] = None,
    device: Annotated[
        str, typer.Option("--device", help="Torch device, for example cuda.")
    ] = "cpu",
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
    allow_experimental_mixed_supervision: Annotated[
        bool,
        typer.Option(
            "--allow-experimental-mixed-supervision",
            help="Acknowledge proxy-only non-scientific training.",
        ),
    ] = False,
) -> None:
    """Execute M2's symmetric bidirectional matcher on exact proxy inputs."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        bind_local_modernbert_checkpoint,
        verify_experimental_m0_proxy_inputs,
    )
    from leanfaith.models.m2_bidirectional_matcher import (
        ExperimentalM2ProxyError,
        load_experimental_m2_proxy_config,
        train_m2_proxy_one_epoch,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    selected_config = config_path or Path(
        "configs/models/experimental_m2_bidirectional_matcher_proxy_v1.yaml"
    )
    if not selected_config.is_absolute():
        selected_config = paths.root / selected_config
    try:
        for path in (prepared_input_dir, tokenizer_audit_dir, checkpoint_dir, output_dir):
            if not path.is_absolute():
                raise ValueError("all M2 artifact paths must be absolute")
        prepared = verify_experimental_m0_proxy_inputs(prepared_input_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        snapshot = audit.snapshots[prepared.tokenizer_decision.candidate_key]
        checkpoint = bind_local_modernbert_checkpoint(
            checkpoint_dir,
            protocol=prepared.protocol,
            audited_tokenizer_snapshot=snapshot,
        )
        protocol = load_experimental_m2_proxy_config(selected_config).config
        artifacts = train_m2_proxy_one_epoch(
            repository_root=paths.root,
            prepared_input_dir=prepared_input_dir,
            output_dir=output_dir,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
            allow_experimental_mixed_supervision=allow_experimental_mixed_supervision,
            device=device,
        )
    except (
        KeyError,
        OSError,
        ValueError,
        ExperimentalM0ProxyError,
        ExperimentalM2ProxyError,
    ) as exc:
        typer.echo(f"M2 proxy training rejected: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M2 proxy training ready; artifact={artifacts.artifact_id} "
        f"steps={artifacts.optimizer_steps} exposed={artifacts.examples_exposed} "
        f"replayed={str(artifacts.replayed).lower()} swap_invariant=true "
        "relation_head=false semantic_prediction=false"
    )


@app.command("verify-m2-training")
def verify_m2_training_command(
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Frozen M2 output.")],
    prepared_input_dir: Annotated[
        Path, typer.Option("--prepared-input-dir", help="Exact frozen M0 proxy inputs.")
    ],
    tokenizer_audit_dir: Annotated[
        Path, typer.Option("--tokenizer-audit-dir", help="Exact completed tokenizer audit.")
    ],
    checkpoint_dir: Annotated[
        Path, typer.Option("--checkpoint-dir", help="Exact pinned local model snapshot.")
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Frozen M2 proxy protocol config.")
    ] = None,
    root_dir: Annotated[
        Path | None, typer.Option("--root", help="Repository root override.")
    ] = None,
) -> None:
    """Verify M2 bytes and swap invariance against every exact dependency."""
    from leanfaith.config.paths import RepoPaths
    from leanfaith.models.m0_dual_encoder import (
        ExperimentalM0ProxyError,
        bind_local_modernbert_checkpoint,
        verify_experimental_m0_proxy_inputs,
    )
    from leanfaith.models.m2_bidirectional_matcher import (
        ExperimentalM2ProxyError,
        load_experimental_m2_proxy_config,
        verify_m2_proxy_training,
    )
    from leanfaith.models.tokenizer_audit import verify_tokenizer_audit

    paths = RepoPaths.discover(root_dir) if root_dir is None else RepoPaths(root=root_dir)
    selected_config = config_path or Path(
        "configs/models/experimental_m2_bidirectional_matcher_proxy_v1.yaml"
    )
    if not selected_config.is_absolute():
        selected_config = paths.root / selected_config
    try:
        for path in (output_dir, prepared_input_dir, tokenizer_audit_dir, checkpoint_dir):
            if not path.is_absolute():
                raise ValueError("all M2 artifact paths must be absolute")
        prepared = verify_experimental_m0_proxy_inputs(prepared_input_dir)
        audit = verify_tokenizer_audit(tokenizer_audit_dir, replay=False)
        snapshot = audit.snapshots[prepared.tokenizer_decision.candidate_key]
        checkpoint = bind_local_modernbert_checkpoint(
            checkpoint_dir,
            protocol=prepared.protocol,
            audited_tokenizer_snapshot=snapshot,
        )
        protocol = load_experimental_m2_proxy_config(selected_config).config
        manifest = verify_m2_proxy_training(
            output_dir,
            repository_root=paths.root,
            prepared_input_dir=prepared_input_dir,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
        )
    except (
        KeyError,
        OSError,
        ValueError,
        ExperimentalM0ProxyError,
        ExperimentalM2ProxyError,
    ) as exc:
        typer.echo(f"M2 training verification failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"M2 proxy training verified; artifact={manifest.artifact_id} "
        f"steps={manifest.optimizer_steps} swap_invariant=true "
        "relation_head=false semantic_prediction=false"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
