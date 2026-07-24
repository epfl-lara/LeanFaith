"""Hash-bound successor for the LF-021 one-problem RCP qualification.

Version 2 composes the tested v1 transport/lineage engine under an immutable
artifact envelope.  It binds the engine config and module, this wrapper, the
CLI, prompt, provider portfolio, and remote-generation policy before any
provider request.  The shared v1 execution records remain explicit; output
roots, preflights, and the qualification manifest are v2.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation import rcp_qualification_v1 as engine

_HEX64 = r"^[0-9a-f]{64}$"


class RCPQualificationV2Error(engine.RCPQualificationError):
    """The v2 envelope or one of its exact artifact bindings drifted."""


class RCPBoundArtifactV2(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        path = PurePosixPath(self.artifact)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("bound artifacts must be repository-relative")
        return self


class RCPV2Outputs(StrictModel):
    root: str
    preflight_root: str

    @model_validator(mode="after")
    def _relative(self) -> Self:
        for value in (self.root, self.preflight_root):
            path = PurePosixPath(value)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise ValueError("v2 output paths must be repository-relative")
        if "v2" not in PurePosixPath(self.root).parts:
            raise ValueError("v2 output root must contain an exact v2 path component")
        return self


class RCPV2ContaminationPolicy(StrictModel):
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_ids"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False
    allowed_use: Literal["supplemental_generator_candidates_only"]


class RCPQualificationV2Config(StrictModel):
    schema_version: Literal[2] = 2
    config_id: Literal["lf021_rcp_kimi_qualification_v2"]
    frozen_at: datetime.datetime
    status: Literal["qualification_ready"]
    artifact_class: Literal["qualification"] = "qualification"
    shared_execution_record_schema: Literal["lf021_rcp_qualification_records_v1"]
    engine_config: RCPBoundArtifactV2
    engine_module: RCPBoundArtifactV2
    wrapper_module: RCPBoundArtifactV2
    cli_script: RCPBoundArtifactV2
    prompt_template: RCPBoundArtifactV2
    provider_portfolio: RCPBoundArtifactV2
    remote_generation_policy: RCPBoundArtifactV2
    outputs: RCPV2Outputs
    primary_model_id: Literal["moonshotai/Kimi-K2.7-Code"]
    fallback_model_id: Literal["moonshotai/Kimi-K2.6"]
    primary_role: Literal["generator"]
    fallback_role: Literal["fallback_and_ablation_generator"]
    one_moonshot_diversity_family: Literal[True] = True
    bulk_execution_available: Literal[False] = False
    contamination: RCPV2ContaminationPolicy
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False

    @model_validator(mode="after")
    def _utc(self) -> Self:
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("v2 frozen_at must be UTC")
        return self


class RCPQualificationPreflightV2(StrictModel):
    schema_version: Literal[2] = 2
    artifact_kind: Literal["lf021_rcp_kimi_qualification_preflight_v2"]
    config_id: Literal["lf021_rcp_kimi_qualification_v2"]
    config_file_sha256: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    bound_artifact_hashes: dict[str, str]
    catalog: engine.RCPModelCatalogObservation
    engine_preflight: engine.RCPQualificationPreflight
    primary_model_id: Literal["moonshotai/Kimi-K2.7-Code"]
    fallback_model_id: Literal["moonshotai/Kimi-K2.6"]
    transport: Literal["remote_on_prem_epfl_rcp"]
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_ids"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False
    allowed_use: Literal["supplemental_generator_candidates_only"]
    provider_requests_created: Literal[0] = 0
    bulk_execution_available: Literal[False] = False
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False


class RCPQualificationManifestV2(StrictModel):
    schema_version: Literal[2] = 2
    manifest_id: str = Field(pattern=r"^rcp_qualification_manifest_v2:[0-9a-f]{64}$")
    artifact_kind: Literal["lf021_rcp_kimi_qualification_manifest_v2"]
    config_id: Literal["lf021_rcp_kimi_qualification_v2"]
    config_file_sha256: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    bound_artifact_hashes: dict[str, str]
    catalog_observation_id: str = Field(pattern=r"^rcp_catalog_observation:[0-9a-f]{64}$")
    catalog_raw_response_sha256: str = Field(pattern=_HEX64)
    output_directory: str
    invocation_sha256: str = Field(pattern=_HEX64)
    reference_blind_audit_sha256: str = Field(pattern=_HEX64)
    terminal_artifact: str
    terminal_sha256: str = Field(pattern=_HEX64)
    terminal_status: engine.RCPTerminalStatus
    attempt_count: int = Field(ge=1)
    model_id: str
    model_selection: Literal["primary", "fallback"]
    transport: Literal["remote_on_prem_epfl_rcp"]
    checkpoint_revision_status: Literal["unavailable_from_rcp_route_ids"]
    training_cutoff_status: Literal["unknown"]
    contamination_status: Literal["unknown"]
    unseen_claim_eligible: Literal[False] = False
    heldout_claim_eligible: Literal[False] = False
    evaluation_claim_eligible: Literal[False] = False
    allowed_use: Literal["supplemental_generator_candidates_only"]
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    gate_closed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "manifest_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        expected = "rcp_qualification_manifest_v2:" + hash_canonical(
            {"schema": "lf021_rcp_qualification_manifest_v2", **self.id_payload()}
        )
        if self.manifest_id != expected:
            raise ValueError("v2 qualification manifest ID differs")
        return self


@dataclass(frozen=True, slots=True)
class LoadedRCPQualificationV2:
    loaded_config: LoadedConfig[RCPQualificationV2Config]
    engine_loaded: engine.LoadedRCPQualification
    bound_artifact_hashes: dict[str, str]


@dataclass(frozen=True, slots=True)
class RCPQualificationRunV2:
    engine_run: engine.RCPQualificationRun
    manifest: RCPQualificationManifestV2
    manifest_path: Path


def _canonical_record_bytes(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _resolve_bound(
    repo_root: Path,
    binding: RCPBoundArtifactV2,
    *,
    label: str,
) -> Path:
    root = repo_root.resolve()
    path = root / binding.artifact
    if path.is_symlink() or not path.is_file():
        raise RCPQualificationV2Error(f"{label} is missing or unsafe")
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise RCPQualificationV2Error(f"{label} escapes repository root") from exc
    observed = hash_file(path)
    if observed != binding.sha256:
        raise RCPQualificationV2Error(f"{label} hash drift: {observed} != {binding.sha256}")
    return path


def _bound_hashes(config: RCPQualificationV2Config) -> dict[str, str]:
    return {
        "engine_config": config.engine_config.sha256,
        "engine_module": config.engine_module.sha256,
        "wrapper_module": config.wrapper_module.sha256,
        "cli_script": config.cli_script.sha256,
        "prompt_template": config.prompt_template.sha256,
        "provider_portfolio": config.provider_portfolio.sha256,
        "remote_generation_policy": config.remote_generation_policy.sha256,
    }


def load_rcp_qualification_v2(
    config_path: Path,
    *,
    repo_root: Path,
) -> LoadedRCPQualificationV2:
    loaded = load_config(config_path, RCPQualificationV2Config)
    config = loaded.config
    bindings = {
        "engine_config": config.engine_config,
        "engine_module": config.engine_module,
        "wrapper_module": config.wrapper_module,
        "cli_script": config.cli_script,
        "prompt_template": config.prompt_template,
        "provider_portfolio": config.provider_portfolio,
        "remote_generation_policy": config.remote_generation_policy,
    }
    resolved = {
        label: _resolve_bound(repo_root, binding, label=label)
        for label, binding in bindings.items()
    }
    engine_loaded_original = engine.load_rcp_qualification(
        resolved["engine_config"],
        repo_root=repo_root,
    )
    engine_config = engine_loaded_original.loaded_config.config
    if (
        engine_config.models.primary.model_id != config.primary_model_id
        or engine_config.models.fallback.model_id != config.fallback_model_id
        or engine_config.prompt.sha256 != config.prompt_template.sha256
        or engine_config.policy.bulk_execution_available
        or engine_config.models.primary.contamination_status != "unknown"
        or engine_config.models.fallback.contamination_status != "unknown"
    ):
        raise RCPQualificationV2Error("v2 wrapper and bound engine policy disagree")
    adapted_engine_config = engine_config.model_copy(
        update={
            "outputs": engine.RCPOutputConfig(
                root=config.outputs.root,
                preflight_root=config.outputs.preflight_root,
            )
        }
    )
    adapted_loaded_config = LoadedConfig(
        config=adapted_engine_config,
        path=config_path,
        raw=loaded.raw,
        # Every execution invocation binds the v2 semantic config, not the
        # delegated engine config.
        config_hash=loaded.config_hash,
    )
    adapted_engine_loaded = engine.LoadedRCPQualification(
        loaded_config=adapted_loaded_config,
        problem=engine_loaded_original.problem,
        reference_theorems=engine_loaded_original.reference_theorems,
        prompt_template_sha256=engine_loaded_original.prompt_template_sha256,
        rendered_prompt=engine_loaded_original.rendered_prompt,
        rendered_prompt_sha256=engine_loaded_original.rendered_prompt_sha256,
        reference_blind_audit=engine_loaded_original.reference_blind_audit,
    )
    return LoadedRCPQualificationV2(
        loaded_config=loaded,
        engine_loaded=adapted_engine_loaded,
        bound_artifact_hashes=_bound_hashes(config),
    )


def probe_rcp_catalog_v2(
    loaded: LoadedRCPQualificationV2,
    *,
    credentials: engine.RCPCredentials,
    transport: engine.RCPHTTPTransport,
    clock: object = None,
) -> engine.RCPModelCatalogObservation:
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return engine.probe_rcp_catalog(
        loaded.engine_loaded,
        credentials=credentials,
        transport=transport,
        **kwargs,  # type: ignore[arg-type]
    )


def write_rcp_preflight_v2(
    loaded: LoadedRCPQualificationV2,
    *,
    catalog: engine.RCPModelCatalogObservation,
    repo_root: Path,
) -> tuple[Path, str]:
    config = loaded.loaded_config.config
    engine_preflight = engine.build_rcp_preflight(
        loaded.engine_loaded,
        catalog=catalog,
        config_file_sha256=hash_file(loaded.loaded_config.path),
    )
    report = RCPQualificationPreflightV2(
        artifact_kind="lf021_rcp_kimi_qualification_preflight_v2",
        config_id=config.config_id,
        config_file_sha256=hash_file(loaded.loaded_config.path),
        config_hash=loaded.loaded_config.config_hash,
        bound_artifact_hashes=loaded.bound_artifact_hashes,
        catalog=catalog,
        engine_preflight=engine_preflight,
        primary_model_id=config.primary_model_id,
        fallback_model_id=config.fallback_model_id,
        transport="remote_on_prem_epfl_rcp",
        checkpoint_revision_status=config.contamination.checkpoint_revision_status,
        training_cutoff_status=config.contamination.training_cutoff_status,
        contamination_status=config.contamination.contamination_status,
        allowed_use=config.contamination.allowed_use,
    )
    suffix = catalog.observation_id.rsplit(":", 1)[-1]
    path = repo_root / config.outputs.preflight_root / f"{suffix}.json"
    digest = engine._persist_immutable(path, _canonical_record_bytes(report))
    return path, digest


def execute_one_rcp_qualification_v2(
    loaded: LoadedRCPQualificationV2,
    *,
    catalog: engine.RCPModelCatalogObservation,
    credentials: engine.RCPCredentials,
    repo_root: Path,
    model_selection: Literal["primary", "fallback"] = "primary",
    transport: engine.RCPHTTPTransport | None = None,
    clock: object = None,
    sleeper: object = None,
) -> RCPQualificationRunV2:
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    run = engine.execute_one_rcp_qualification(
        loaded.engine_loaded,
        catalog=catalog,
        credentials=credentials,
        repo_root=repo_root,
        model_selection=model_selection,
        transport=transport,
        **kwargs,  # type: ignore[arg-type]
    )
    config = loaded.loaded_config.config
    output_directory = str(run.output_directory.resolve().relative_to(repo_root.resolve()))
    terminal_artifact = str(run.terminal_path.resolve().relative_to(repo_root.resolve()))
    payload = {
        "schema_version": 2,
        "artifact_kind": "lf021_rcp_kimi_qualification_manifest_v2",
        "config_id": config.config_id,
        "config_file_sha256": hash_file(loaded.loaded_config.path),
        "config_hash": loaded.loaded_config.config_hash,
        "bound_artifact_hashes": loaded.bound_artifact_hashes,
        "catalog_observation_id": catalog.observation_id,
        "catalog_raw_response_sha256": catalog.raw_response_sha256,
        "output_directory": output_directory,
        "invocation_sha256": hash_file(run.output_directory / "invocation.json"),
        "reference_blind_audit_sha256": hash_file(
            run.output_directory / "reference_blind_audit.json"
        ),
        "terminal_artifact": terminal_artifact,
        "terminal_sha256": hash_file(run.terminal_path),
        "terminal_status": run.terminal.status.value,
        "attempt_count": len(run.attempt_paths),
        "model_id": run.terminal.model_id,
        "model_selection": run.terminal.model_selection,
        "transport": "remote_on_prem_epfl_rcp",
        "checkpoint_revision_status": config.contamination.checkpoint_revision_status,
        "training_cutoff_status": config.contamination.training_cutoff_status,
        "contamination_status": config.contamination.contamination_status,
        "unseen_claim_eligible": False,
        "heldout_claim_eligible": False,
        "evaluation_claim_eligible": False,
        "allowed_use": config.contamination.allowed_use,
        "reference_transmission_performed": False,
        "private_source_transmission_performed": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "gate_closed": False,
    }
    manifest_id = "rcp_qualification_manifest_v2:" + hash_canonical(
        {"schema": "lf021_rcp_qualification_manifest_v2", **payload}
    )
    manifest = RCPQualificationManifestV2.model_validate({"manifest_id": manifest_id, **payload})
    manifest_path = run.output_directory / "qualification_manifest_v2.json"
    engine._persist_immutable(manifest_path, _canonical_record_bytes(manifest))
    engine._secret_absent(run.output_directory, credentials.api_key)
    return RCPQualificationRunV2(
        engine_run=run,
        manifest=manifest,
        manifest_path=manifest_path,
    )
