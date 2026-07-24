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
    )
    typer.echo(f"manifest={manifest}")
    typer.echo(json.dumps(stats, sort_keys=True))


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
            help="LF-018 pre-scale output-directory override.",
        ),
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
            )
        )
        > 1
    ):
        typer.echo(
            "--validate-only, --validate-positives, --validate-negatives, and "
            "--run-negative-pre-scale/--run-smoke-vertical-slice are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=2)
    if code_bundle is not None and not run_smoke_vertical_slice:
        typer.echo("--code-bundle is supported only with --run-smoke-vertical-slice", err=True)
        raise typer.Exit(code=2)
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
            "--run-negative-pre-scale/--run-smoke-vertical-slice",
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
