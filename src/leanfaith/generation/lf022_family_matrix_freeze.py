"""Offline, fail-closed LF-022 production-family matrix freeze.

This module turns already persisted model/catalog evidence into a versioned
``LF022ProductionFamilyMatrix`` consumed by the allocation-only LF-022
planner.  It deliberately has no provider client and grants no execution,
label, supervision, training, evaluation-independence, or Gate permission.

The original v1 allocation used GLM as its third proposer.  A versioned v2
allocation may replace that unavailable route with DeepSeek while preserving
all v1 artifacts byte-for-byte.  The prior DeepSeek judge parse failure remains
explicit evidence rather than being misrepresented as judge qualification.
OpenAI Codex remains wholly supervision-excluded as the proposed primary
evaluation family.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022FamilyPin,
    LF022ProductionFamilyMatrix,
    LF022ProviderCatalogSnapshot,
    LF022ProviderDeployment,
    canonical_model_family,
    make_lf022_production_family_matrix,
    make_lf022_provider_catalog_snapshot,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

_RCP_PROVIDER_ID = "epfl_rcp"
_CODEX_PROVIDER_ID = "openai_codex_exec"
_RCP_ACTIVE_MODELS = (
    "moonshotai/Kimi-K2.7-Code",
    "Qwen/Qwen3.5-397B-A17B",
    "zai-org/GLM-5.2",
    "deepseek-ai/DeepSeek-V4-Pro",
)
_PROPOSERS_V1 = ("moonshot_kimi_k2", "qwen3", "glm5")
_TRAINING_ROLE_FAMILIES = (
    "moonshot_kimi_k2",
    "qwen3",
    "glm5",
    "deepseek_v4",
)
_HELDOUT_FAMILY = "openai_codex"
_CODEX_REGISTRY_MODEL_ID = "openai/gpt-5.6-terra"
_CODEX_DEPLOYMENT_ID = "gpt-5.6-terra"
_ALTERNATIVES_NOT_COUNTED = (
    "moonshotai/Kimi-K2.6",
    "Qwen/Qwen3.6-35B-A3B",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "Qwen/Qwen3-VL-235B-A22B-Thinking",
)
_LOCAL_EXPECTED = (
    (
        "goedel_formalizer_v2_8b",
        "Goedel-LM/Goedel-Formalizer-V2-8B",
        "fe2d362d899601abe79d7d5e95eaa7fe9883a0cb",
    ),
    (
        "kimina_autoformalizer_7b",
        "AI-MO/Kimina-Autoformalizer-7B",
        "ddd47cb477d93b3ca990468e1c0d5ad6b60973dd",
    ),
    (
        "stepfun_formalizer_7b",
        "stepfun-ai/StepFun-Formalizer-7B",
        "fb0dc612761fecd64ebbc489c2a3417e9ea01968",
    ),
    (
        "reform_8b",
        "GuoxinChen/ReForm-8B",
        "1589c832cfad679a280b222e694b987a33befd26",
    ),
)


class LF022FamilyMatrixFreezeError(RuntimeError):
    """A bound input or immutable output failed closed."""


class FreezeArtifactBinding(StrictModel):
    """One exact repository-relative input or output."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _safe_path(self) -> Self:
        _validate_relative_path(self.path)
        return self


class FreezeOutputPaths(StrictModel):
    """Canonical output locations; outputs never contain credentials."""

    rcp_catalog: str
    codex_catalog: str
    family_matrix: str
    freeze_report: str

    @model_validator(mode="after")
    def _safe_unique_paths(self) -> Self:
        paths = [
            self.rcp_catalog,
            self.codex_catalog,
            self.family_matrix,
            self.freeze_report,
        ]
        for path in paths:
            _validate_relative_path(path)
        if len(paths) != len(set(paths)):
            raise ValueError("freeze output paths must be unique")
        return self


class LF022FamilyMatrixFreezeConfig(StrictModel):
    """Evidence bindings and immutable output paths for the v1 freeze."""

    schema_version: Literal[1] = 1
    config_id: Literal[
        "lf022_production_family_matrix_freeze_v1",
        "lf022_production_family_matrix_freeze_v2",
    ]
    status: Literal["proposal_fail_closed_no_execution"]
    proposer_family_ids: tuple[str, str, str] = _PROPOSERS_V1
    rcp_catalog_wire: FreezeArtifactBinding
    remote_portfolio: FreezeArtifactBinding
    successful_lf022_smoke_config: FreezeArtifactBinding
    lf022_smoke_report: FreezeArtifactBinding
    failed_fourth_family_smoke_config: FreezeArtifactBinding
    codex_qualification: FreezeArtifactBinding
    local_generator_matrix: FreezeArtifactBinding
    outputs: FreezeOutputPaths
    route_execution_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    gate_credit_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _versioned_proposers(self) -> Self:
        expected = {
            "lf022_production_family_matrix_freeze_v1": _PROPOSERS_V1,
            "lf022_production_family_matrix_freeze_v2": (
                "moonshot_kimi_k2",
                "qwen3",
                "deepseek_v4",
            ),
        }[self.config_id]
        if self.proposer_family_ids != expected:
            raise ValueError("proposer families differ from the versioned freeze policy")
        return self


class InactiveLocalFamilyPin(StrictModel):
    """Exact local identity deliberately absent from LF-022 supervision roles."""

    family_id: str
    model_id: str
    checkpoint_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    role_status: Literal["inactive_not_lf022_judge_or_validator_qualified"]


class LF022FamilyMatrixFreezeChecks(StrictModel):
    """Mechanically replayed facts, not semantic-quality claims."""

    all_evidence_hashes_match: Literal[True] = True
    rcp_routes_present_in_bound_catalog: Literal[True] = True
    successful_smoke_identities_match: Literal[True] = True
    fourth_family_failure_preserved: Literal[True] = True
    codex_heldout_identity_matches: Literal[True] = True
    local_checkpoint_pins_match: Literal[True] = True
    canonical_families_are_unique: Literal[True] = True
    heldout_family_absent_from_supervision: Literal[True] = True
    no_same_family_fallback_counted_separately: Literal[True] = True
    no_provider_client_or_execution_binding_present: Literal[True] = True


class LF022FamilyMatrixFreezeReportContent(StrictModel):
    """Content-addressed, explicitly blocked freeze report."""

    schema_version: Literal[1] = 1
    report_kind: Literal["lf022_production_family_matrix_freeze"]
    status: Literal["BLOCKED_PENDING_FOURTH_SUPERVISION_FAMILY_QUALIFICATION"]
    config: FreezeArtifactBinding
    evidence: tuple[FreezeArtifactBinding, ...] = Field(min_length=7)
    rcp_catalog: LF022ArtifactBinding
    codex_catalog: LF022ArtifactBinding
    family_matrix: LF022ArtifactBinding
    family_matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    proposer_family_ids: tuple[str, str, str]
    judge_family_ids: tuple[str, str, str, str]
    sci_validator_family_ids: tuple[str, str, str, str]
    heldout_eval_family_id: Literal["openai_codex"]
    heldout_eval_supervision_excluded: Literal[True] = True
    inactive_local_families: tuple[
        InactiveLocalFamilyPin,
        InactiveLocalFamilyPin,
        InactiveLocalFamilyPin,
        InactiveLocalFamilyPin,
    ]
    same_family_or_mode_alternatives_not_counted: tuple[str, str, str, str]
    checks: LF022FamilyMatrixFreezeChecks
    blockers: tuple[str, ...] = Field(min_length=1)
    provider_calls_performed: Literal[0] = 0
    network_requests_performed: Literal[0] = 0
    route_execution_authorized: Literal[False] = False
    proposal_generation_authorized: Literal[False] = False
    judgment_generation_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_independence_claim_eligible: Literal[False] = False
    gate_credit_eligible: Literal[False] = False


class LF022FamilyMatrixFreezeReport(LF022FamilyMatrixFreezeReportContent):
    report_id: str = Field(pattern=id_pattern("lf022_family_matrix_freeze"))

    @model_validator(mode="after")
    def _content_addressed_and_fail_closed(self) -> Self:
        expected = make_id(
            "lf022_family_matrix_freeze",
            self.model_dump(mode="json", exclude={"report_id"}),
        )
        if self.report_id != expected:
            raise ValueError("report_id does not match canonical freeze-report content")
        heldout = self.heldout_eval_family_id
        if heldout in {
            *self.proposer_family_ids,
            *self.judge_family_ids,
            *self.sci_validator_family_ids,
        }:
            raise ValueError("held-out evaluation family appears in supervision roles")
        return self


class LF022FamilyMatrixFreezeBundle(StrictModel):
    """All deterministic outputs of one offline build."""

    rcp_catalog: LF022ProviderCatalogSnapshot
    codex_catalog: LF022ProviderCatalogSnapshot
    family_matrix: LF022ProductionFamilyMatrix
    report: LF022FamilyMatrixFreezeReport


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
    ):
        raise ValueError("artifact paths must be normalized repository-relative POSIX paths")


def _resolve_repo_path(repo_root: Path, relative_path: str, *, must_exist: bool) -> Path:
    _validate_relative_path(relative_path)
    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise LF022FamilyMatrixFreezeError(f"cannot resolve repository root: {repo_root}") from exc
    cursor = root
    parts = PurePosixPath(relative_path).parts
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise LF022FamilyMatrixFreezeError(f"artifact path contains a symlink: {relative_path}")
    if must_exist:
        try:
            resolved = cursor.resolve(strict=True)
        except OSError as exc:
            raise LF022FamilyMatrixFreezeError(
                f"bound artifact is missing: {relative_path}"
            ) from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise LF022FamilyMatrixFreezeError(
                f"bound artifact is not a regular repository file: {relative_path}"
            )
    else:
        cursor.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = cursor.parent.resolve(strict=True)
        if not resolved_parent.is_relative_to(root):
            raise LF022FamilyMatrixFreezeError(
                f"output path escapes the repository: {relative_path}"
            )
    return cursor


def _verify_binding(repo_root: Path, binding: FreezeArtifactBinding) -> Path:
    path = _resolve_repo_path(repo_root, binding.path, must_exist=True)
    if hash_file(path) != binding.sha256:
        raise LF022FamilyMatrixFreezeError(f"artifact hash mismatch: {binding.path}")
    return path


def _load_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise LF022FamilyMatrixFreezeError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise LF022FamilyMatrixFreezeError(f"JSON artifact root is not a mapping: {path}")
    return cast(dict[str, Any], value)


def _artifact_binding(path: str, model: StrictModel) -> LF022ArtifactBinding:
    payload = canonical_json_bytes(model.model_dump(mode="json"))
    return LF022ArtifactBinding(path=path, sha256=sha256(payload).hexdigest())


def _load_and_verify_config(
    repo_root: Path,
    config_path: Path,
) -> LoadedConfig[LF022FamilyMatrixFreezeConfig]:
    try:
        relative = config_path.resolve(strict=True).relative_to(repo_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise LF022FamilyMatrixFreezeError(
            "freeze config must be an existing file beneath the repository root"
        ) from exc
    config_components = (
        repo_root / PurePosixPath(*relative.parts[:index])
        for index in range(1, len(relative.parts) + 1)
    )
    if any(part.is_symlink() for part in config_components):
        raise LF022FamilyMatrixFreezeError("freeze config path cannot contain symlinks")
    return load_config(config_path, LF022FamilyMatrixFreezeConfig)


def _verify_successful_smoke(
    *,
    config: dict[str, Any],
    report: dict[str, Any],
) -> None:
    providers = cast(dict[str, Any], config.get("providers"))
    proposer = cast(dict[str, Any], providers.get("proposer"))
    judge_a = cast(dict[str, Any], providers.get("judge_A"))
    judge_b = cast(dict[str, Any], providers.get("judge_B"))
    heldout = cast(dict[str, Any], providers.get("primary_eval_judge"))
    observed = (
        proposer.get("model_id"),
        judge_a.get("model_id"),
        judge_b.get("model_id"),
        heldout.get("model_id"),
    )
    expected = (
        _RCP_ACTIVE_MODELS[0],
        _RCP_ACTIVE_MODELS[1],
        _RCP_ACTIVE_MODELS[2],
        _CODEX_DEPLOYMENT_ID,
    )
    if observed != expected or heldout.get("enabled_for_this_smoke") is not False:
        raise LF022FamilyMatrixFreezeError("successful LF-022 smoke identities drifted")
    success = cast(dict[str, Any], report.get("verified_success"))
    if (
        success.get("status") != "success"
        or success.get("proposer") != _RCP_ACTIVE_MODELS[0]
        or [item.get("model") for item in cast(list[dict[str, Any]], success.get("judge_calls"))]
        != [_RCP_ACTIVE_MODELS[1], _RCP_ACTIVE_MODELS[2]]
        or success.get("primary_evaluation_judge_calls") != 0
    ):
        raise LF022FamilyMatrixFreezeError("successful LF-022 smoke report drifted")


def _verify_failed_fourth_family(
    *,
    config: dict[str, Any],
    report: dict[str, Any],
) -> None:
    providers = cast(dict[str, Any], config.get("providers"))
    judge_a = cast(dict[str, Any], providers.get("judge_A"))
    if judge_a.get("model_id") != _RCP_ACTIVE_MODELS[3]:
        raise LF022FamilyMatrixFreezeError("fourth-family smoke identity drifted")
    attempts = cast(list[dict[str, Any]], report.get("terminal_attempts"))
    if not any(
        item.get("config_file") == "configs/generation/lf022_rcp_public_smoke_v2.yaml"
        and item.get("status") == "terminal_failure"
        and "strict parsing rejected" in str(item.get("diagnosis", ""))
        for item in attempts
    ):
        raise LF022FamilyMatrixFreezeError(
            "fourth-family structured-output failure is not preserved"
        )


def _load_local_pins(path: Path) -> tuple[InactiveLocalFamilyPin, ...]:
    matrix = load_yaml_mapping(path)
    observed: list[tuple[str, str, str]] = []
    for item in cast(list[dict[str, Any]], matrix.get("families")):
        observed.append(
            (
                cast(str, item.get("family_id")),
                cast(str, item.get("model")),
                cast(str, item.get("revision")),
            )
        )
    heldout = cast(dict[str, Any], matrix.get("heldout"))
    observed.append(
        (
            cast(str, heldout.get("family_id")),
            cast(str, heldout.get("model")),
            cast(str, heldout.get("revision")),
        )
    )
    if tuple(observed) != _LOCAL_EXPECTED:
        raise LF022FamilyMatrixFreezeError("pinned local generator identities drifted")
    return tuple(
        InactiveLocalFamilyPin(
            family_id=family_id,
            model_id=model_id,
            checkpoint_revision=revision,
            role_status="inactive_not_lf022_judge_or_validator_qualified",
        )
        for family_id, model_id, revision in observed
    )


def build_lf022_family_matrix_freeze(
    *,
    repo_root: Path,
    config_path: Path,
) -> LF022FamilyMatrixFreezeBundle:
    """Replay exact evidence and build a non-executable, blocked matrix freeze."""

    loaded = _load_and_verify_config(repo_root, config_path)
    cfg = loaded.config
    bindings = (
        cfg.rcp_catalog_wire,
        cfg.remote_portfolio,
        cfg.successful_lf022_smoke_config,
        cfg.lf022_smoke_report,
        cfg.failed_fourth_family_smoke_config,
        cfg.codex_qualification,
        cfg.local_generator_matrix,
    )
    paths = {binding.path: _verify_binding(repo_root, binding) for binding in bindings}

    raw_catalog = _load_json_mapping(paths[cfg.rcp_catalog_wire.path])
    rows = raw_catalog.get("data")
    if not isinstance(rows, list):
        raise LF022FamilyMatrixFreezeError("RCP catalog has no data list")
    catalog_ids = [item.get("id") for item in rows if isinstance(item, dict)]
    if len(catalog_ids) != len(set(catalog_ids)):
        raise LF022FamilyMatrixFreezeError("RCP catalog model IDs are not unique")
    missing = set(_RCP_ACTIVE_MODELS) - set(catalog_ids)
    if missing:
        raise LF022FamilyMatrixFreezeError(
            f"required RCP deployments are absent: {sorted(missing)}"
        )

    successful_config = load_yaml_mapping(paths[cfg.successful_lf022_smoke_config.path])
    smoke_report = _load_json_mapping(paths[cfg.lf022_smoke_report.path])
    _verify_successful_smoke(config=successful_config, report=smoke_report)
    failed_config = load_yaml_mapping(paths[cfg.failed_fourth_family_smoke_config.path])
    _verify_failed_fourth_family(config=failed_config, report=smoke_report)

    remote_portfolio = load_yaml_mapping(paths[cfg.remote_portfolio.path])
    if (
        remote_portfolio.get("status") != "prospective_fail_disabled"
        or cast(dict[str, Any], remote_portfolio.get("global_guards")).get(
            "route_execution_authorized"
        )
        is not False
    ):
        raise LF022FamilyMatrixFreezeError("bound remote portfolio is not fail-disabled")

    codex = _load_json_mapping(paths[cfg.codex_qualification.path])
    if (
        codex.get("provider") != _CODEX_PROVIDER_ID
        or codex.get("model_family") != _HELDOUT_FAMILY
        or codex.get("model") != _CODEX_DEPLOYMENT_ID
        or codex.get("immutable_model_revision_available") is not False
        or codex.get("evaluation_judge_eligible_if_used_for_supervision") is not False
    ):
        raise LF022FamilyMatrixFreezeError("Codex held-out qualification identity drifted")

    inactive_local = _load_local_pins(paths[cfg.local_generator_matrix.path])

    rcp_catalog = make_lf022_provider_catalog_snapshot(
        provider_id=_RCP_PROVIDER_ID,
        deployments=tuple(
            LF022ProviderDeployment(model_id=model_id, deployment_id=model_id)
            for model_id in _RCP_ACTIVE_MODELS
        ),
    )
    codex_catalog = make_lf022_provider_catalog_snapshot(
        provider_id=_CODEX_PROVIDER_ID,
        deployments=(
            LF022ProviderDeployment(
                model_id=_CODEX_REGISTRY_MODEL_ID,
                deployment_id=_CODEX_DEPLOYMENT_ID,
            ),
        ),
    )
    rcp_catalog_binding = _artifact_binding(cfg.outputs.rcp_catalog, rcp_catalog)
    codex_catalog_binding = _artifact_binding(cfg.outputs.codex_catalog, codex_catalog)

    family_registry = (
        LF022FamilyPin(
            family_id="moonshot_kimi_k2",
            model_id=_RCP_ACTIVE_MODELS[0],
            canonical_family=canonical_model_family(_RCP_ACTIVE_MODELS[0]),
            pin_kind="provider_deployment_snapshot",
            provider_id=_RCP_PROVIDER_ID,
            provider_deployment_id=_RCP_ACTIVE_MODELS[0],
            provider_catalog_artifact=rcp_catalog_binding,
            underlying_checkpoint_revision_status="provider_not_disclosed",
        ),
        LF022FamilyPin(
            family_id="qwen3",
            model_id=_RCP_ACTIVE_MODELS[1],
            canonical_family=canonical_model_family(_RCP_ACTIVE_MODELS[1]),
            pin_kind="provider_deployment_snapshot",
            provider_id=_RCP_PROVIDER_ID,
            provider_deployment_id=_RCP_ACTIVE_MODELS[1],
            provider_catalog_artifact=rcp_catalog_binding,
            underlying_checkpoint_revision_status="provider_not_disclosed",
        ),
        LF022FamilyPin(
            family_id="glm5",
            model_id=_RCP_ACTIVE_MODELS[2],
            canonical_family=canonical_model_family(_RCP_ACTIVE_MODELS[2]),
            pin_kind="provider_deployment_snapshot",
            provider_id=_RCP_PROVIDER_ID,
            provider_deployment_id=_RCP_ACTIVE_MODELS[2],
            provider_catalog_artifact=rcp_catalog_binding,
            underlying_checkpoint_revision_status="provider_not_disclosed",
        ),
        LF022FamilyPin(
            family_id="deepseek_v4",
            model_id=_RCP_ACTIVE_MODELS[3],
            canonical_family=canonical_model_family(_RCP_ACTIVE_MODELS[3]),
            pin_kind="provider_deployment_snapshot",
            provider_id=_RCP_PROVIDER_ID,
            provider_deployment_id=_RCP_ACTIVE_MODELS[3],
            provider_catalog_artifact=rcp_catalog_binding,
            underlying_checkpoint_revision_status="provider_not_disclosed",
        ),
        LF022FamilyPin(
            family_id=_HELDOUT_FAMILY,
            model_id=_CODEX_REGISTRY_MODEL_ID,
            canonical_family=canonical_model_family(_CODEX_REGISTRY_MODEL_ID),
            pin_kind="provider_deployment_snapshot",
            provider_id=_CODEX_PROVIDER_ID,
            provider_deployment_id=_CODEX_DEPLOYMENT_ID,
            provider_catalog_artifact=codex_catalog_binding,
            underlying_checkpoint_revision_status="provider_not_disclosed",
        ),
    )
    matrix = make_lf022_production_family_matrix(
        family_registry=family_registry,
        proposer_family_ids=cfg.proposer_family_ids,
        judge_family_ids=_TRAINING_ROLE_FAMILIES,
        sci_validator_family_ids=_TRAINING_ROLE_FAMILIES,
        heldout_eval_family_id=_HELDOUT_FAMILY,
    )
    matrix_binding = _artifact_binding(cfg.outputs.family_matrix, matrix)

    config_relative = config_path.resolve(strict=True).relative_to(repo_root.resolve(strict=True))
    config_binding = FreezeArtifactBinding(
        path=config_relative.as_posix(),
        sha256=hash_file(config_path),
    )
    report_payload: dict[str, object] = {
        "schema_version": 1,
        "report_kind": "lf022_production_family_matrix_freeze",
        "status": "BLOCKED_PENDING_FOURTH_SUPERVISION_FAMILY_QUALIFICATION",
        "config": config_binding.model_dump(mode="json"),
        "evidence": [binding.model_dump(mode="json") for binding in bindings],
        "rcp_catalog": rcp_catalog_binding.model_dump(mode="json"),
        "codex_catalog": codex_catalog_binding.model_dump(mode="json"),
        "family_matrix": matrix_binding.model_dump(mode="json"),
        "family_matrix_id": matrix.matrix_id,
        "proposer_family_ids": list(cfg.proposer_family_ids),
        "judge_family_ids": list(_TRAINING_ROLE_FAMILIES),
        "sci_validator_family_ids": list(_TRAINING_ROLE_FAMILIES),
        "heldout_eval_family_id": _HELDOUT_FAMILY,
        "heldout_eval_supervision_excluded": True,
        "inactive_local_families": [pin.model_dump(mode="json") for pin in inactive_local],
        "same_family_or_mode_alternatives_not_counted": list(_ALTERNATIVES_NOT_COUNTED),
        "checks": LF022FamilyMatrixFreezeChecks().model_dump(mode="json"),
        "blockers": [
            (
                "DeepSeek-V4-Pro is the required fourth distinct supervision family, "
                "but its only LF-022 judge smoke failed strict structured-output parsing; "
                "a separately reviewed successful qualification is required."
                if cfg.config_id == "lf022_production_family_matrix_freeze_v1"
                else "DeepSeek-V4-Pro has only judge-role HTTP transport evidence ending in "
                "strict parse failure; proposer and judge roles require separate exact "
                "qualifications before any generated output can become supervision."
            ),
            (
                "The exact RCP checkpoint revisions are provider-undisclosed; the freeze "
                "binds only the exact persisted catalog snapshot and deployment selectors."
            ),
            (
                "Gate 5 human adjudication and a separately versioned route-execution "
                "admission remain absent; this identity matrix cannot authorize calls."
            ),
        ],
        "provider_calls_performed": 0,
        "network_requests_performed": 0,
        "route_execution_authorized": False,
        "proposal_generation_authorized": False,
        "judgment_generation_authorized": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_independence_claim_eligible": False,
        "gate_credit_eligible": False,
    }
    content = LF022FamilyMatrixFreezeReportContent.model_validate(report_payload)
    report = LF022FamilyMatrixFreezeReport.model_validate(
        {
            **report_payload,
            "report_id": make_id(
                "lf022_family_matrix_freeze",
                content.model_dump(mode="json"),
            ),
        }
    )
    return LF022FamilyMatrixFreezeBundle(
        rcp_catalog=rcp_catalog,
        codex_catalog=codex_catalog,
        family_matrix=matrix,
        report=report,
    )


def _write_immutable_model(
    *,
    repo_root: Path,
    relative_path: str,
    model: StrictModel,
) -> LF022ArtifactBinding:
    output = _resolve_repo_path(repo_root, relative_path, must_exist=False)
    payload = canonical_json_bytes(model.model_dump(mode="json"))
    if output.exists():
        if not output.is_file() or output.read_bytes() != payload:
            raise LF022FamilyMatrixFreezeError(
                f"immutable freeze output already differs: {relative_path}"
            )
    else:
        try:
            with output.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            if not output.is_file() or output.read_bytes() != payload:
                raise LF022FamilyMatrixFreezeError(
                    f"concurrent freeze output differs: {relative_path}"
                ) from None
    return LF022ArtifactBinding(path=relative_path, sha256=hash_file(output))


def write_lf022_family_matrix_freeze(
    *,
    repo_root: Path,
    config_path: Path,
) -> LF022FamilyMatrixFreezeBundle:
    """Build and immutably write all four canonical freeze artifacts."""

    bundle = build_lf022_family_matrix_freeze(
        repo_root=repo_root,
        config_path=config_path,
    )
    cfg = load_config(config_path, LF022FamilyMatrixFreezeConfig).config
    written = (
        _write_immutable_model(
            repo_root=repo_root,
            relative_path=cfg.outputs.rcp_catalog,
            model=bundle.rcp_catalog,
        ),
        _write_immutable_model(
            repo_root=repo_root,
            relative_path=cfg.outputs.codex_catalog,
            model=bundle.codex_catalog,
        ),
        _write_immutable_model(
            repo_root=repo_root,
            relative_path=cfg.outputs.family_matrix,
            model=bundle.family_matrix,
        ),
        _write_immutable_model(
            repo_root=repo_root,
            relative_path=cfg.outputs.freeze_report,
            model=bundle.report,
        ),
    )
    expected = (
        bundle.report.rcp_catalog,
        bundle.report.codex_catalog,
        bundle.report.family_matrix,
    )
    if written[:3] != expected:
        raise LF022FamilyMatrixFreezeError("written output bindings differ from freeze report")
    return bundle


def verify_lf022_family_matrix_freeze(
    *,
    repo_root: Path,
    config_path: Path,
) -> LF022FamilyMatrixFreezeBundle:
    """Rebuild offline and require every persisted artifact to be byte-identical."""

    bundle = build_lf022_family_matrix_freeze(
        repo_root=repo_root,
        config_path=config_path,
    )
    cfg = load_config(config_path, LF022FamilyMatrixFreezeConfig).config
    for relative_path, model in (
        (cfg.outputs.rcp_catalog, bundle.rcp_catalog),
        (cfg.outputs.codex_catalog, bundle.codex_catalog),
        (cfg.outputs.family_matrix, bundle.family_matrix),
        (cfg.outputs.freeze_report, bundle.report),
    ):
        path = _resolve_repo_path(repo_root, relative_path, must_exist=True)
        expected = canonical_json_bytes(model.model_dump(mode="json"))
        if path.read_bytes() != expected:
            raise LF022FamilyMatrixFreezeError(
                f"persisted freeze artifact differs from replay: {relative_path}"
            )
    return bundle


__all__ = [
    "FreezeArtifactBinding",
    "LF022FamilyMatrixFreezeBundle",
    "LF022FamilyMatrixFreezeConfig",
    "LF022FamilyMatrixFreezeError",
    "LF022FamilyMatrixFreezeReport",
    "build_lf022_family_matrix_freeze",
    "verify_lf022_family_matrix_freeze",
    "write_lf022_family_matrix_freeze",
]
