"""Replay-certified production-route eligibility for the Kimi-v4 challenge.

The 16-item Kimi challenge is deliberately separate from ordinary LF-022
production admission.  A passing challenge remains provisional evidence until
this module binds its exact selection, terminal lineage, reviewed contract,
and production family matrix into one immutable route-eligibility record.

Certification and verification are offline.  They replay every persisted
challenge terminal through the strict requalification executor and never
create semantic labels or make generated variants training eligible.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import (
    LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT,
    LF022GOpenExecutionAdmission,
)
from leanfaith.generation.lf022_kimi_v4_requalification import (
    LF022KimiV4QualificationRecord,
    LF022KimiV4RequalificationError,
    run_verified_kimi_v4_requalification,
)
from leanfaith.generation.lf022_kimi_v4_selection import (
    LF022_KIMI_V4_SELECTION_ROOT,
    LF022KimiV4ChallengeContract,
    LF022KimiV4ChallengeSelection,
)
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022ProductionFamilyMatrix,
)
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

LF022_KIMI_V4_ELIGIBILITY_PATH = (
    f"{LF022_CANONICAL_EXECUTOR_OUTPUT_ROOT}/production_eligibility/moonshot_kimi_k2.json"
)


class LF022KimiV4EligibilityError(RuntimeError):
    """Kimi-v4 eligibility evidence failed closed."""


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or path.as_posix() != value
    ):
        raise LF022KimiV4EligibilityError(f"{label} is not a safe repository-relative path")
    return path


def _bound_path(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    label: str,
) -> Path:
    root = repo_root.resolve(strict=True)
    current = root
    for part in _safe_relative(binding.path, label=label).parts:
        current /= part
        if current.is_symlink():
            raise LF022KimiV4EligibilityError(f"{label} contains a symlinked component")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise LF022KimiV4EligibilityError(f"{label} is missing") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LF022KimiV4EligibilityError(f"{label} is not a repository-local file")
    observed = hash_file(resolved)
    if observed != binding.sha256:
        raise LF022KimiV4EligibilityError(
            f"{label} SHA-256 differs: {observed} != {binding.sha256}"
        )
    return resolved


def _binding(repo_root: Path, path: Path) -> LF022ArtifactBinding:
    root = repo_root.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise LF022KimiV4EligibilityError(f"eligibility artifact is missing or unsafe: {path}")
    try:
        relative = path.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise LF022KimiV4EligibilityError("eligibility artifact escapes repository") from exc
    return LF022ArtifactBinding(
        path=PurePosixPath(relative.as_posix()).as_posix(),
        sha256=hash_file(path),
    )


def _load_canonical[RecordT: StrictModel](
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
    model: type[RecordT],
    label: str,
) -> RecordT:
    raw = _bound_path(repo_root=repo_root, binding=binding, label=label).read_bytes()
    try:
        record = model.model_validate_json(raw)
    except ValueError as exc:
        raise LF022KimiV4EligibilityError(f"invalid {label}: {exc}") from exc
    canonical = canonical_json_bytes(record.model_dump(mode="json"))
    if raw not in {canonical, canonical + b"\n"}:
        raise LF022KimiV4EligibilityError(f"{label} is not canonical JSON")
    return record


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise LF022KimiV4EligibilityError("eligibility output cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise LF022KimiV4EligibilityError(f"existing Kimi-v4 eligibility differs: {path}")
        return
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022KimiV4EligibilityError(
                    f"concurrent Kimi-v4 eligibility differs: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


class LF022KimiV4ProductionEligibility(StrictModel):
    """One exact passing challenge authorizing only public provisional G_open."""

    schema_version: Literal[1] = 1
    eligibility_id: str = Field(pattern=id_pattern("lf022_kimi_v4_route_eligibility"))
    status: Literal["kimi_v4_challenge_replay_verified"]
    proposer_family_id: Literal["moonshot_kimi_k2"]
    model_id: Literal["moonshotai/Kimi-K2.7-Code"]
    deployment_id: Literal["moonshotai/Kimi-K2.7-Code"]
    canonical_family: Literal["moonshotai/kimi-k2"]
    provider_id: Literal["epfl_rcp"]
    catalog_snapshot_id: str = Field(pattern=id_pattern("lf022_provider_catalog"))
    route_snapshot_revision: str = Field(pattern=r"^rcp-catalog-sha256:[0-9a-f]{64}$")
    decoding_contract_id: Literal["kimi_k2_7_public_proposer_v4"]
    decoding_contract_hash: str = Field(pattern=HEX64_PATTERN)
    v4_contract: LF022ArtifactBinding
    v4_prompt: LF022ArtifactBinding
    selection_id: str = Field(pattern=id_pattern("lf022_kimi_v4_selection"))
    selection: LF022ArtifactBinding
    selection_code_tree_hash: str = Field(pattern=HEX64_PATTERN)
    selection_code_bundle: LF022ArtifactBinding
    qualification_id: str = Field(pattern=id_pattern("lf022_kimi_v4_qualification"))
    qualification: LF022ArtifactBinding
    qualification_status: Literal["passed"]
    qualification_terminal_count: Literal[16] = 16
    strict_parse_success_count: int = Field(ge=14, le=16, strict=True)
    replay_network_calls: Literal[0] = 0
    family_matrix_id: str = Field(pattern=id_pattern("lf022_family_matrix"))
    family_matrix: LF022ArtifactBinding
    judge_family_ids: tuple[str, ...] = Field(min_length=2)
    permitted_validator_family_ids: tuple[str, ...] = Field(min_length=1)
    heldout_eval_family_id: str
    heldout_eval_supervision_excluded: Literal[True] = True
    production_execution_scope: Literal["public_provisional_g_open"]
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    output_quality_tier: Literal["provisional"] = "provisional"
    outputs_unresolved: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    silver_promotion_enabled: Literal[False] = False
    gold_promotion_enabled: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        if tuple(sorted(set(self.judge_family_ids))) != self.judge_family_ids:
            raise ValueError("judge families must be sorted and unique")
        if tuple(sorted(set(self.permitted_validator_family_ids))) != (
            self.permitted_validator_family_ids
        ):
            raise ValueError("validator families must be sorted and unique")
        supervised = {
            self.proposer_family_id,
            *self.judge_family_ids,
            *self.permitted_validator_family_ids,
        }
        if self.proposer_family_id in self.judge_family_ids:
            raise ValueError("Kimi cannot judge its own generated variants")
        if self.proposer_family_id in self.permitted_validator_family_ids:
            raise ValueError("Kimi cannot validate its own generated variants")
        if self.heldout_eval_family_id in supervised:
            raise ValueError("held-out evaluator cannot supervise Kimi production")
        expected = make_id(
            "lf022_kimi_v4_route_eligibility",
            self.model_dump(mode="json", exclude={"eligibility_id"}),
        )
        if self.eligibility_id != expected:
            raise ValueError("eligibility_id does not match canonical Kimi-v4 evidence")
        return self


@dataclass(frozen=True, slots=True)
class CertifiedLF022KimiV4Route:
    """Persisted Kimi-v4 eligibility and its canonical path."""

    eligibility: LF022KimiV4ProductionEligibility
    eligibility_path: Path


def _load_frozen_selection(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
) -> LF022KimiV4ChallengeSelection:
    """Verify the frozen selection and its original semantics-bound files.

    The selection intentionally binds the clean code tree that executed the
    challenge.  Later admission code may evolve, so verification checks the
    archived bundle plus every explicitly semantics-bound implementation file
    rather than requiring the entire current worktree to equal that old tree.
    """

    selection = _load_canonical(
        repo_root=repo_root,
        binding=binding,
        model=LF022KimiV4ChallengeSelection,
        label="Kimi-v4 challenge selection",
    )
    expected_path = (
        PurePosixPath(LF022_KIMI_V4_SELECTION_ROOT)
        / f"{selection.selection_id.split(':', 1)[1]}.json"
    ).as_posix()
    if binding.path != expected_path:
        raise LF022KimiV4EligibilityError(
            "Kimi-v4 selection is outside its content-addressed registry"
        )
    lineage = selection.current_implementation
    bundle_path = _bound_path(
        repo_root=repo_root,
        binding=lineage.code_bundle,
        label="selection code bundle",
    )
    try:
        observed_bundle = validate_code_bundle(bundle_path, lineage.code_tree_hash)
    except (OSError, ValueError) as exc:
        raise LF022KimiV4EligibilityError(
            f"selection code bundle failed validation: {exc}"
        ) from exc
    if observed_bundle != lineage.code_bundle.sha256:
        raise LF022KimiV4EligibilityError("selection code-bundle hash differs")
    for item in lineage.files:
        _bound_path(
            repo_root=repo_root,
            binding=item.artifact,
            label=f"selection implementation {item.role}",
        )
    implementation_artifacts = tuple(item.artifact for item in lineage.files)
    if selection.v4_contract not in implementation_artifacts:
        raise LF022KimiV4EligibilityError("selection contract is outside bound implementation")
    if selection.v4_prompt not in implementation_artifacts:
        raise LF022KimiV4EligibilityError("selection prompt is outside bound implementation")
    return selection


def _load_contract(
    *, repo_root: Path, selection: LF022KimiV4ChallengeSelection
) -> LF022KimiV4ChallengeContract:
    path = _bound_path(
        repo_root=repo_root,
        binding=selection.v4_contract,
        label="Kimi-v4 reviewed contract",
    )
    try:
        mapping = dict(load_yaml_mapping(path))
        decoding = dict(cast(dict[str, object], mapping["decoding"]))
        decoding.update(schema_version=1, contract_id=mapping["contract_id"])
        mapping["decoding"] = decoding
        contract = LF022KimiV4ChallengeContract.model_validate(mapping)
    except (KeyError, TypeError, ValueError) as exc:
        raise LF022KimiV4EligibilityError(f"invalid Kimi-v4 reviewed contract: {exc}") from exc
    if hash_canonical(contract.model_dump(mode="json")) != selection.v4_contract_hash:
        raise LF022KimiV4EligibilityError("Kimi-v4 contract hash differs from selection")
    return contract


def _verify_matrix(
    *,
    repo_root: Path,
    binding: LF022ArtifactBinding,
) -> tuple[LF022ProductionFamilyMatrix, tuple[str, ...], tuple[str, ...]]:
    matrix = _load_canonical(
        repo_root=repo_root,
        binding=binding,
        model=LF022ProductionFamilyMatrix,
        label="Kimi-v4 production family matrix",
    )
    pin = matrix.pins_by_id.get("moonshot_kimi_k2")
    if (
        pin is None
        or pin.model_id != "moonshotai/Kimi-K2.7-Code"
        or pin.provider_deployment_id != "moonshotai/Kimi-K2.7-Code"
        or pin.canonical_family != "moonshotai/kimi-k2"
        or pin.provider_id != "epfl_rcp"
        or "moonshot_kimi_k2" not in matrix.proposer_family_ids
    ):
        raise LF022KimiV4EligibilityError("family matrix lacks the exact Kimi-v4 route")
    excluded = {"moonshot_kimi_k2", matrix.heldout_eval_family_id}
    judges = tuple(sorted(family for family in matrix.judge_family_ids if family not in excluded))
    validators = tuple(
        sorted(family for family in matrix.sci_validator_family_ids if family not in excluded)
    )
    if len(judges) < 2 or not validators:
        raise LF022KimiV4EligibilityError(
            "family matrix lacks independent Kimi judges or validators"
        )
    return matrix, judges, validators


def _replay_qualification(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    qualification_binding: LF022ArtifactBinding,
) -> LF022KimiV4QualificationRecord:
    qualification = _load_canonical(
        repo_root=repo_root,
        binding=qualification_binding,
        model=LF022KimiV4QualificationRecord,
        label="Kimi-v4 qualification",
    )
    expected_path = (
        PurePosixPath("data/lf022_kimi_v4_requalification/v1")
        / selection.selection_id.split(":", 1)[1]
        / "qualification.json"
    ).as_posix()
    if qualification_binding.path != expected_path:
        raise LF022KimiV4EligibilityError(
            "Kimi-v4 qualification is outside its selection-bound registry"
        )
    if qualification.selection_id != selection.selection_id:
        raise LF022KimiV4EligibilityError("qualification belongs to a different selection")
    try:
        replay = run_verified_kimi_v4_requalification(
            repo_root=repo_root,
            selection=selection,
            stage="replay",
            execute_public_requalification=False,
        )
    except (LF022KimiV4RequalificationError, OSError, ValueError) as exc:
        raise LF022KimiV4EligibilityError(f"Kimi-v4 exact offline replay rejected: {exc}") from exc
    if (
        replay.network_calls_this_run != 0
        or len(replay.terminals) != 16
        or replay.qualification is None
        or replay.qualification_path is None
        or replay.qualification != qualification
        or _binding(repo_root, replay.qualification_path) != qualification_binding
    ):
        raise LF022KimiV4EligibilityError(
            "Kimi-v4 qualification differs from exact zero-network replay"
        )
    if qualification.status != "passed":
        raise LF022KimiV4EligibilityError("Kimi-v4 challenge did not pass frozen criteria")
    return qualification


def _eligibility_payload(
    *,
    repo_root: Path,
    selection: LF022KimiV4ChallengeSelection,
    selection_binding: LF022ArtifactBinding,
    qualification: LF022KimiV4QualificationRecord,
    qualification_binding: LF022ArtifactBinding,
    matrix: LF022ProductionFamilyMatrix,
    matrix_binding: LF022ArtifactBinding,
    judges: tuple[str, ...],
    validators: tuple[str, ...],
) -> dict[str, object]:
    contract = _load_contract(repo_root=repo_root, selection=selection)
    historical_admission = _load_canonical(
        repo_root=repo_root,
        binding=selection.v3_admission,
        model=LF022GOpenExecutionAdmission,
        label="selection-bound Kimi-v3 admission",
    )
    route = historical_admission.route
    if (
        route.proposer_family_id != "moonshot_kimi_k2"
        or route.model_id != contract.model_id
        or route.deployment_id != contract.model_id
        or route.canonical_family != contract.canonical_family
        or route.provider_id != contract.provider
    ):
        raise LF022KimiV4EligibilityError(
            "Kimi-v4 qualification route differs from its selected historical lineage"
        )
    return {
        "schema_version": 1,
        "status": "kimi_v4_challenge_replay_verified",
        "proposer_family_id": contract.family_id,
        "model_id": contract.model_id,
        "deployment_id": contract.model_id,
        "canonical_family": contract.canonical_family,
        "provider_id": contract.provider,
        "catalog_snapshot_id": route.catalog_snapshot_id,
        "route_snapshot_revision": route.route_snapshot_revision,
        "decoding_contract_id": contract.contract_id,
        "decoding_contract_hash": hash_canonical(contract.decoding.model_dump(mode="json")),
        "v4_contract": selection.v4_contract.model_dump(mode="json"),
        "v4_prompt": selection.v4_prompt.model_dump(mode="json"),
        "selection_id": selection.selection_id,
        "selection": selection_binding.model_dump(mode="json"),
        "selection_code_tree_hash": selection.current_implementation.code_tree_hash,
        "selection_code_bundle": selection.current_implementation.code_bundle.model_dump(
            mode="json"
        ),
        "qualification_id": qualification.qualification_id,
        "qualification": qualification_binding.model_dump(mode="json"),
        "qualification_status": qualification.status,
        "qualification_terminal_count": len(qualification.terminals),
        "strict_parse_success_count": qualification.strict_parse_success_count,
        "replay_network_calls": 0,
        "family_matrix_id": matrix.matrix_id,
        "family_matrix": matrix_binding.model_dump(mode="json"),
        "judge_family_ids": list(judges),
        "permitted_validator_family_ids": list(validators),
        "heldout_eval_family_id": matrix.heldout_eval_family_id,
        "heldout_eval_supervision_excluded": True,
        "production_execution_scope": "public_provisional_g_open",
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "output_quality_tier": "provisional",
        "outputs_unresolved": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }


def certify_lf022_kimi_v4_production_eligibility(
    *,
    repo_root: Path,
    selection_binding: LF022ArtifactBinding,
    qualification_binding: LF022ArtifactBinding,
    family_matrix_binding: LF022ArtifactBinding,
) -> CertifiedLF022KimiV4Route:
    """Replay a passing 16-item challenge and persist route-only eligibility."""

    selection = _load_frozen_selection(repo_root=repo_root, binding=selection_binding)
    qualification = _replay_qualification(
        repo_root=repo_root,
        selection=selection,
        qualification_binding=qualification_binding,
    )
    matrix, judges, validators = _verify_matrix(
        repo_root=repo_root,
        binding=family_matrix_binding,
    )
    payload = _eligibility_payload(
        repo_root=repo_root,
        selection=selection,
        selection_binding=selection_binding,
        qualification=qualification,
        qualification_binding=qualification_binding,
        matrix=matrix,
        matrix_binding=family_matrix_binding,
        judges=judges,
        validators=validators,
    )
    eligibility = LF022KimiV4ProductionEligibility.model_validate(
        {
            **payload,
            "eligibility_id": make_id("lf022_kimi_v4_route_eligibility", payload),
        }
    )
    path = repo_root / LF022_KIMI_V4_ELIGIBILITY_PATH
    _write_immutable(
        path,
        canonical_json_bytes(eligibility.model_dump(mode="json")) + b"\n",
    )
    verified = verify_lf022_kimi_v4_production_eligibility(
        repo_root=repo_root,
        eligibility_binding=_binding(repo_root, path),
    )
    if verified != eligibility:
        raise LF022KimiV4EligibilityError("persisted Kimi-v4 eligibility replay differs")
    return CertifiedLF022KimiV4Route(eligibility=eligibility, eligibility_path=path)


def verify_lf022_kimi_v4_production_eligibility(
    *,
    repo_root: Path,
    eligibility_binding: LF022ArtifactBinding,
) -> LF022KimiV4ProductionEligibility:
    """Verify one exact Kimi-v4 route eligibility with zero network calls."""

    eligibility = _load_canonical(
        repo_root=repo_root,
        binding=eligibility_binding,
        model=LF022KimiV4ProductionEligibility,
        label="Kimi-v4 production eligibility",
    )
    if eligibility_binding.path != LF022_KIMI_V4_ELIGIBILITY_PATH:
        raise LF022KimiV4EligibilityError(
            "Kimi-v4 eligibility is outside the canonical family registry"
        )
    selection = _load_frozen_selection(repo_root=repo_root, binding=eligibility.selection)
    qualification = _replay_qualification(
        repo_root=repo_root,
        selection=selection,
        qualification_binding=eligibility.qualification,
    )
    matrix, judges, validators = _verify_matrix(
        repo_root=repo_root,
        binding=eligibility.family_matrix,
    )
    expected_payload = _eligibility_payload(
        repo_root=repo_root,
        selection=selection,
        selection_binding=eligibility.selection,
        qualification=qualification,
        qualification_binding=eligibility.qualification,
        matrix=matrix,
        matrix_binding=eligibility.family_matrix,
        judges=judges,
        validators=validators,
    )
    expected = LF022KimiV4ProductionEligibility.model_validate(
        {
            **expected_payload,
            "eligibility_id": make_id(
                "lf022_kimi_v4_route_eligibility",
                expected_payload,
            ),
        }
    )
    if eligibility != expected:
        raise LF022KimiV4EligibilityError(
            "Kimi-v4 eligibility differs from selection, challenge, contract, or matrix replay"
        )
    return eligibility


__all__ = [
    "LF022_KIMI_V4_ELIGIBILITY_PATH",
    "CertifiedLF022KimiV4Route",
    "LF022KimiV4EligibilityError",
    "LF022KimiV4ProductionEligibility",
    "certify_lf022_kimi_v4_production_eligibility",
    "verify_lf022_kimi_v4_production_eligibility",
]
