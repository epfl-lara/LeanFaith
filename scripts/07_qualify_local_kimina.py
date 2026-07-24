#!/usr/bin/env python3
"""Run one fixture-only, no-Gate-credit local LF-021 qualification."""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import try_to_load_from_cache

from leanfaith.cli.collect_real_outputs import (
    _load_offline_fixture,
    _offline_context,
    _offline_problem,
    _offline_reference,
    _qualification_fixture_header_path,
)
from leanfaith.config.code_bundle import freeze_code_bundle
from leanfaith.config.hashing import hash_file
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.candidate_screening import CandidateScreeningIndex
from leanfaith.generation.invocation_failure import (
    InvocationCheckpointBinding,
    InvocationCodeBundleBinding,
    LocalQualificationInvocationFailure,
    LocalQualificationInvocationStage,
    persist_invocation_failure,
    redact_exception_message,
)
from leanfaith.generation.local_hf import (
    LocalHFGenerationRequest,
    LocalHFGenerationResult,
    LocalHFSequentialRuntime,
    TransformersCausalGenerator,
    TransformersLocalLoader,
)
from leanfaith.generation.local_qualification import (
    LocalQualificationFixtureConfig,
    QualificationCodeBundleBinding,
    QualificationScreeningInputFiles,
    QualificationStatus,
    build_local_qualification_formatter,
    load_local_qualification_config,
    make_runtime_binding,
    persist_local_qualification_bundle,
    preflight_local_qualification_fixture,
    run_local_qualification,
    verify_local_checkpoint_artifacts,
    verify_local_qualification_bundle,
)
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend


@dataclass(slots=True)
class _InvocationState:
    """Mutable launcher state that is never serialized as provider lineage."""

    stage: LocalQualificationInvocationStage
    model_execution_started: bool = False


@dataclass(frozen=True, slots=True)
class _TrackedRuntime:
    """Mark the exact boundary at which the local runtime is invoked."""

    delegate: LocalHFSequentialRuntime
    state: _InvocationState

    def generate(self, request: LocalHFGenerationRequest) -> LocalHFGenerationResult:
        self.state.stage = LocalQualificationInvocationStage.MODEL_EXECUTION
        self.state.model_execution_started = True
        return self.delegate.generate(request)


def _driver_version() -> str:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        value = result.stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        value = "unavailable"
    return value or "unavailable"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one public-fixture local qualification. This is smoke-only "
            "and never earns Gate 5G credit."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/generation/local_qualification_v1.yaml"),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("examples/lf021_offline_smoke_v1.json"),
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help=(
            "Pinned Lake checkout. Optional only for the original local "
            "offline fixture; required for Git-backed registry entries."
        ),
    )
    parser.add_argument(
        "--project-registry-key",
        choices=("fixtures", "mathlib"),
        help="Registry entry that must match the selected fixture.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate fixture/context/active-registry overlap without loading the model.",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def _checkpoint_snapshot(repo_id: str, revision: str) -> Path:
    cached = try_to_load_from_cache(
        repo_id,
        "model.safetensors.index.json",
        revision=revision,
    )
    if not isinstance(cached, str):
        raise SystemExit(
            f"pinned checkpoint is not fully available in the local cache: {repo_id}@{revision}"
        )
    # Cache snapshot files are symlinks into the blob store. Resolving the file
    # would discard the immutable snapshots/<revision> directory identity.
    snapshot = Path(cached).absolute().parent
    if snapshot.name != revision:
        raise SystemExit(f"resolved cache snapshot does not match pinned revision: {snapshot}")
    return snapshot


def _validate_configured_fixture_binding(
    *,
    configured: LocalQualificationFixtureConfig | None,
    repo_root: Path,
    fixture_path: Path,
    fixture_sha256: str,
    import_header_path: Path,
    project_registry_key: str,
) -> None:
    """Reject a CLI fixture that differs from a config-bound fixture."""

    if configured is None:
        return
    try:
        fixture_artifact = fixture_path.resolve().relative_to(repo_root.resolve()).as_posix()
        header_artifact = import_header_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise SystemExit("configured qualification fixtures must be inside the repository") from exc
    observed = {
        "fixture_artifact": fixture_artifact,
        "fixture_sha256": fixture_sha256,
        "import_header_artifact": header_artifact,
        "import_header_sha256": hash_file(import_header_path),
        "project_registry_key": project_registry_key,
    }
    expected = {
        "fixture_artifact": configured.fixture_artifact,
        "fixture_sha256": configured.fixture_sha256,
        "import_header_artifact": configured.import_header_artifact,
        "import_header_sha256": configured.import_header_sha256,
        "project_registry_key": configured.project_registry_key,
    }
    if observed != expected:
        raise SystemExit(
            "CLI qualification fixture/header/project binding differs from the loaded config"
        )


def main() -> int:
    args = _arguments()
    root = find_repo_root(Path.cwd())
    paths = RepoPaths(root)
    config_path = args.config if args.config.is_absolute() else root / args.config
    loaded = load_local_qualification_config(config_path, repo_root=root)
    config_file_sha256 = hash_file(config_path)
    try:
        config_artifact = str(config_path.resolve().relative_to(root.resolve()))
    except ValueError:
        config_artifact = f"file://{config_path.resolve()}"
    fixture_path = args.fixture if args.fixture.is_absolute() else root / args.fixture
    fixture = _load_offline_fixture(fixture_path)
    fixture_hash = hash_file(fixture_path)
    project_registry_key = args.project_registry_key or fixture.resolved_project_registry_key
    if project_registry_key != fixture.resolved_project_registry_key:
        raise SystemExit(
            "selected --project-registry-key differs from the fixture binding: "
            f"{project_registry_key!r} != {fixture.resolved_project_registry_key!r}"
        )
    import_header_path = _qualification_fixture_header_path(
        fixture,
        paths=paths,
    )
    _validate_configured_fixture_binding(
        configured=getattr(loaded.config, "qualification_fixture", None),
        repo_root=root,
        fixture_path=fixture_path,
        fixture_sha256=fixture_hash,
        import_header_path=import_header_path,
        project_registry_key=project_registry_key,
    )
    if args.project_dir is None:
        if project_registry_key != "fixtures":
            raise SystemExit("--project-dir is required for a Git-backed qualification fixture")
        fixture_project = root / "tests" / "lean_fixtures"
    else:
        fixture_project = (
            args.project_dir if args.project_dir.is_absolute() else root / args.project_dir
        )
    fixture_project = fixture_project.resolve()
    context = _offline_context(
        paths,
        project_dir=fixture_project,
        imports_text=fixture.imports,
        project_registry_key=project_registry_key,
    )
    reference = _offline_reference(
        fixture=fixture,
        fixture_hash=fixture_hash,
        context=context,
    )
    active = load_active_benchmark_registry(repo_root=root)
    denylist = ProblemPoolDenylistBinding.from_active_registry(active, repo_root=root)
    problem, _, _ = _offline_problem(
        fixture=fixture,
        fixture_hash=fixture_hash,
        fixture_path=fixture_path,
        import_header_path=import_header_path,
        paths=paths,
        context=context,
        reference=reference,
        denylist=denylist,
    )

    now = datetime.datetime.now(tz=datetime.UTC)
    run_prefix = loaded.config.active_model.family_id.removesuffix("_7b").removesuffix("_8b")
    run_name = now.strftime(f"{run_prefix}_%Y%m%dT%H%M%SZ")
    run_directory = (
        args.output_dir
        if args.output_dir is not None
        else root / "runs" / "lf021_local_qualification" / run_name
    )
    if not run_directory.is_absolute():
        run_directory = root / run_directory
    # From this point onward the run directory is allocated and every
    # exceptional exit must either have a normal terminal/bundle or an
    # immutable invocation-failure record.
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SystemExit(f"qualification output directory already exists: {run_directory}") from exc
    state = _InvocationState(stage=LocalQualificationInvocationStage.PREFLIGHT)
    checkpoint_binding: InvocationCheckpointBinding | None = None
    code_binding: InvocationCodeBundleBinding | None = None
    backend: LeanInteractBackend | None = None
    try:
        backend = LeanInteractBackend(
            BackendSettings(
                project_dir=fixture_project,
                context_fingerprint=context.context_fingerprint,
                environment_schema_version=context.environment_schema_version,
                raw_response_dir=run_directory / "lean_raw",
            )
        )
        screening_index = CandidateScreeningIndex(denylist=denylist)
        preflight = preflight_local_qualification_fixture(
            fixture_id=fixture.fixture_id,
            fixture_sha256=fixture_hash,
            import_header_artifact=str(import_header_path.resolve().relative_to(root.resolve())),
            problem=problem,
            reference=reference,
            context=context,
            registered_header=fixture.imports,
            backend=backend,
            screening_index=screening_index,
            created_at=now,
        )
        if args.preflight_only:
            state.stage = LocalQualificationInvocationStage.BACKEND_CLOSE
            backend.close()
            backend = None
            print(
                json.dumps(
                    {
                        **preflight.model_dump(mode="json"),
                        "qualification_config_id": loaded.config.config_id,
                        "qualification_config_hash": loaded.config_hash,
                        "model_repo_id": loaded.config.active_model.repo_id,
                        "model_revision": loaded.config.active_model.revision,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        state.stage = LocalQualificationInvocationStage.CHECKPOINT_VERIFICATION
        checkpoint_verification = verify_local_checkpoint_artifacts(
            loaded.config.active_model,
            snapshot_directory=_checkpoint_snapshot(
                loaded.config.active_model.repo_id,
                loaded.config.active_model.revision,
            ),
        )
        checkpoint_binding = InvocationCheckpointBinding(
            verification_hash=checkpoint_verification.verification_hash,
            model_repo_id=checkpoint_verification.model_repo_id,
            model_revision=checkpoint_verification.model_revision,
            snapshot_reference=checkpoint_verification.snapshot_reference,
            checkpoint_bytes=checkpoint_verification.checkpoint_bytes,
        )
        state.stage = LocalQualificationInvocationStage.CODE_BUNDLE_FREEZE
        code_bundle_path, code_bundle_sha256, code_state = freeze_code_bundle(
            root,
            run_directory / "code_bundle",
        )
        if code_state.code_tree_hash is None:
            raise RuntimeError("qualification code bundle lacks code_tree_hash")
        code_source_artifact = str(code_bundle_path.resolve().relative_to(root.resolve()))
        code_binding = InvocationCodeBundleBinding(
            source_artifact=code_source_artifact,
            sha256=code_bundle_sha256,
            code_tree_hash=code_state.code_tree_hash,
        )
        code_bundle = QualificationCodeBundleBinding(
            source_artifact=code_source_artifact,
            sha256=code_bundle_sha256,
            code_tree_hash=code_state.code_tree_hash,
        )
        state.stage = LocalQualificationInvocationStage.RUNTIME_INITIALIZATION
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        torch_version = str(getattr(torch, "__version__", "unknown"))
        transformers_version = str(getattr(transformers, "__version__", "unknown"))
        cuda = torch.__dict__["cuda"]
        device_name = str(cuda.get_device_name(0))
        runtime_binding = make_runtime_binding(
            repo_root=root,
            environment_lock_artifact="uv.lock",
            torch_version=torch_version,
            transformers_version=transformers_version,
            driver_version=_driver_version(),
            device_name=device_name,
            dtype="bfloat16",
        )
        formatter = build_local_qualification_formatter(loaded.config)
        runtime = _TrackedRuntime(
            delegate=LocalHFSequentialRuntime(
                loader=TransformersLocalLoader(),
                generator=TransformersCausalGenerator(),
                formatter=formatter,
            ),
            state=state,
        )
        state.stage = LocalQualificationInvocationStage.QUALIFICATION_PRE_PROVIDER
        result = run_local_qualification(
            loaded_config=loaded,
            runtime_binding=runtime_binding,
            runtime=runtime,
            problem=problem,
            expected_declaration_name=fixture.resolved_generated_declaration_name,
            context=context,
            references=(reference,),
            registered_header=fixture.imports,
            backend=backend,
            screening_index=screening_index,
            artifact_root=root,
            run_directory=run_directory,
            created_at=now,
            fixture_artifact=str(fixture_path.resolve().relative_to(root.resolve())),
            fixture_preflight=preflight,
            checkpoint_verification=checkpoint_verification,
            code_bundle=code_bundle,
            screening_inputs=QualificationScreeningInputFiles(
                registry_manifest=active.manifest_path,
                active_registry=active.active_registry_path,
                detailed_index=active.detailed_index_path,
                input_manifest=active.input_manifest_path,
            ),
        )
        state.stage = LocalQualificationInvocationStage.BACKEND_CLOSE
        backend.close()
        backend = None
        state.stage = LocalQualificationInvocationStage.BUNDLE_PERSISTENCE
        manifest = persist_local_qualification_bundle(
            result,
            run_directory=run_directory,
            artifact_root=root,
        )
        state.stage = LocalQualificationInvocationStage.REPLAY_VERIFICATION
        replay_terminal, replay_call, replay_attempt = verify_local_qualification_bundle(
            manifest,
            artifact_root=root,
            repo_root=root / ".lf021-replay-must-not-read-the-working-tree",
            problem=problem,
        )
        if (
            replay_terminal.terminal_id != result.terminal.terminal_id
            or replay_call.call_id != result.lineage.call.call_id
            or replay_attempt.attempt_id != result.lineage.attempt.attempt_id
        ):
            raise RuntimeError("persisted qualification replay differs from the executed result")
        print(
            json.dumps(
                {
                    "status": result.terminal.status.value,
                    "terminal_id": result.terminal.terminal_id,
                    "bundle_id": manifest.bundle_id,
                    "run_directory": str(run_directory.relative_to(root)),
                    "qualifies_for_gate5g": False,
                    "semantic_labels_created": False,
                    "replay_verified": True,
                    "error_code": result.terminal.error_code,
                    "error_detail": result.terminal.error_detail,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.terminal.status is QualificationStatus.QUALIFIED_SMOKE else 1
    except BaseException as exc:
        failure_stage = state.stage
        if backend is not None:
            try:
                state.stage = LocalQualificationInvocationStage.BACKEND_CLOSE
                backend.close()
            except BaseException as close_exc:
                exc.add_note(
                    "qualification backend close also failed: "
                    + redact_exception_message(str(close_exc))
                )
        # A completed normal terminal is already the authoritative accounting
        # record. Do not create a competing invocation-failure record after it
        # has been persisted, even if later bundle/replay work fails.
        if not (run_directory / "terminal.json").is_file():
            try:
                failed_at = datetime.datetime.now(tz=datetime.UTC)
                record = LocalQualificationInvocationFailure.create(
                    stage=failure_stage,
                    exception=exc,
                    invoked_at=now,
                    failed_at=failed_at,
                    qualification_config_id=loaded.config.config_id,
                    qualification_config_artifact=config_artifact,
                    qualification_config_file_sha256=config_file_sha256,
                    qualification_config_hash=loaded.config_hash,
                    model_family=loaded.config.active_model.family_id,
                    model_repo_id=loaded.config.active_model.repo_id,
                    model_revision=loaded.config.active_model.revision,
                    provider_slot=loaded.config.active_model.provider_slot,
                    checkpoint_binding=checkpoint_binding,
                    code_bundle_binding=code_binding,
                    model_execution_started=state.model_execution_started,
                )
                path, digest = persist_invocation_failure(
                    record,
                    run_directory=run_directory,
                    artifact_root=root,
                )
                exc.add_note(
                    f"invocation failure preserved at {path.relative_to(root)} (sha256={digest})"
                )
            except BaseException as accounting_exc:
                exc.add_note(
                    "invocation-failure accounting also failed: "
                    + redact_exception_message(str(accounting_exc))
                )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
