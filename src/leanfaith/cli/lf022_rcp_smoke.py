"""CLI orchestration for the public-only LF-022 RCP smoke."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.lf022_rcp_smoke_v1 import (
    LF022RCPSmokeFailureManifest,
    LF022RCPSmokeManifest,
    LF022RCPSmokePreflight,
    execute_public_smoke,
    load_lf022_rcp_smoke,
    probe_and_write_smoke_preflight,
    replay_public_smoke,
    replay_public_smoke_failure,
    resolve_smoke_credentials,
)
from leanfaith.generation.rcp_qualification_v1 import UrllibRCPTransport
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend


@dataclass(frozen=True, slots=True)
class LF022RCPSmokeCLIResult:
    mode: str
    preflight: LF022RCPSmokePreflight | None
    manifest: LF022RCPSmokeManifest | LF022RCPSmokeFailureManifest | None
    artifact_path: Path


def run_lf022_rcp_smoke(
    *,
    paths: RepoPaths,
    config_path: Path,
    execute_public_smoke_flag: bool,
    replay_manifest_path: Path | None,
    replay_failure_manifest_path: Path | None,
    mathlib_project_dir: Path | None,
) -> LF022RCPSmokeCLIResult:
    """Run offline replay, catalog-only preflight, or explicit live smoke."""

    loaded = load_lf022_rcp_smoke(config_path, repo_root=paths.root)
    selected_modes = sum(
        (
            execute_public_smoke_flag,
            replay_manifest_path is not None,
            replay_failure_manifest_path is not None,
        )
    )
    if selected_modes > 1:
        raise ValueError(
            "--execute-public-smoke, --replay-manifest, and "
            "--replay-failure-manifest are mutually exclusive"
        )
    if replay_manifest_path is not None:
        manifest = replay_public_smoke(
            loaded,
            manifest_path=replay_manifest_path,
            repo_root=paths.root,
        )
        return LF022RCPSmokeCLIResult(
            mode="replay",
            preflight=None,
            manifest=manifest,
            artifact_path=replay_manifest_path,
        )
    if replay_failure_manifest_path is not None:
        failure = replay_public_smoke_failure(
            loaded,
            failure_manifest_path=replay_failure_manifest_path,
            repo_root=paths.root,
        )
        return LF022RCPSmokeCLIResult(
            mode="failure-replay",
            preflight=None,
            manifest=failure,
            artifact_path=replay_failure_manifest_path,
        )

    credentials = resolve_smoke_credentials(loaded.loaded_config.config)
    transport = UrllibRCPTransport()
    preflight_run = probe_and_write_smoke_preflight(
        loaded,
        repo_root=paths.root,
        credentials=credentials,
        transport=transport,
    )
    if not execute_public_smoke_flag:
        return LF022RCPSmokeCLIResult(
            mode="preflight",
            preflight=preflight_run.preflight,
            manifest=None,
            artifact_path=preflight_run.preflight_path,
        )
    if mathlib_project_dir is None:
        raise ValueError("--mathlib-project-dir is required for explicit live smoke")
    context_fingerprint = loaded.problem.context_id.removeprefix("ctx:")
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=mathlib_project_dir,
            context_fingerprint=context_fingerprint,
            environment_schema_version=1,
            raw_response_dir=(
                paths.root / loaded.loaded_config.config.outputs.raw_root / "lean_raw_responses"
            ),
        )
    )
    try:
        execution = execute_public_smoke(
            loaded,
            preflight_run=preflight_run,
            repo_root=paths.root,
            credentials=credentials,
            transport=transport,
            lean_backend=backend,
            execute_public_smoke=True,
        )
    finally:
        backend.close()
    return LF022RCPSmokeCLIResult(
        mode="live",
        preflight=preflight_run.preflight,
        manifest=execution.manifest,
        artifact_path=execution.manifest_path,
    )


__all__ = ["LF022RCPSmokeCLIResult", "run_lf022_rcp_smoke"]
