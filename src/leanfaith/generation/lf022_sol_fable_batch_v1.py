"""Prepare bounded Sol/Fable AB+BA weak batches from the frozen v4 inventory."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.claude_fable_judge_v1 import (
    FABLE_FAMILY,
    FABLE_PROVIDER,
    load_claude_fable_judge_config,
)
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditFinding,
    LF022CodexAuditSummary,
)
from leanfaith.generation.lf022_judge_design_rebind_v4 import (
    LF022JudgeDesignRecordV4,
    verify_lf022_judge_design_v4,
)
from leanfaith.generation.lf022_production import LF022ProductionFamilyMatrix
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    _render_summary,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    LF022WeakDispatchRecord,
    _load_canonical_jsonl,
    _load_canonical_model,
    _validate_candidate_inventory_records,
    _validate_family_pins,
    _validate_weak_config,
    prepare_lf022_weak_batch,
)
from leanfaith.generation.providers import DecodingValue
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

SOL_FABLE_BATCH_METHOD_VERSION: Literal["lf022_sol_fable_batch_v3"] = "lf022_sol_fable_batch_v3"
_MAX_PAIRS = 64
_SOL_PROVIDER: Literal["openai_codex_exec"] = "openai_codex_exec"
_SOL_FAMILY: Literal["openai_codex_sol"] = "openai_codex_sol"
_SOL_MODEL: Literal["openai/gpt-5.6-sol"] = "openai/gpt-5.6-sol"
_SOL_EFFORT: Literal["xhigh"] = "xhigh"
_HELDOUT_FAMILY: Literal["deepseek_v4"] = "deepseek_v4"
_FAMILY_MATRIX = Path("configs/generation/lf022_sol_fable_family_matrix_v1.json")
_WEAK_CONFIG = Path("configs/judges/weak_supervision.yaml")
_FABLE_CONFIG = Path("configs/generation/lf022_claude_fable_judge_v1.yaml")
_REQUIRED_HISTORICAL_SOL_XHIGH_SUMMARY_SHA256S = frozenset(
    {
        "4f82f1b00f4f5dd4cd04b3c3c72946d37c54512f657d451d6d16dd33b7fe6d5c",
        "321e1ae56fdd637e3a064b2ccbb072485ee85972a6b3def37e3db15fda3f0bec",
    }
)


class LF022SolFableBatchError(RuntimeError):
    """A v4 selection, family, immutable artifact, or preparation invariant failed."""


class HistoricalSolXhighCorpusBinding(StrictModel):
    """One complete, immutable historical Sol/xhigh findings corpus."""

    summary_id: str = Field(pattern=id_pattern("lf022_codex_audit_summary"))
    summary_sha256: str = Field(pattern=HEX64_PATTERN)
    findings_sha256: str = Field(pattern=HEX64_PATTERN)
    finding_count: int = Field(ge=1, strict=True)
    unique_pair_count: int = Field(ge=1, strict=True)
    pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["xhigh"] = "xhigh"
    copied_summary_artifact: str = Field(min_length=1)
    copied_findings_artifact: str = Field(min_length=1)


class SolFableBatchAuthoringManifest(StrictModel):
    schema_version: Literal[3] = 3
    method_version: Literal["lf022_sol_fable_batch_v3"] = SOL_FABLE_BATCH_METHOD_VERSION
    authoring_id: str = Field(pattern=id_pattern("lf022_sol_fable_authoring"))
    source_v4_artifact_path: str = Field(min_length=1)
    source_v4_inventory_id: str = Field(pattern=id_pattern("lf022_judge_design_inventory"))
    source_v4_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    source_v4_records_sha256: str = Field(pattern=HEX64_PATTERN)
    source_partition_id: str
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3"]
    selection_method: Literal[
        "deterministic_theorem_lineage_hash_with_exhaustive_sol_xhigh_exclusions_v3"
    ] = "deterministic_theorem_lineage_hash_with_exhaustive_sol_xhigh_exclusions_v3"
    lineage_diversity_status: Literal[
        "distinct_source_theorem_lineages_not_full_ancestry_certified"
    ] = "distinct_source_theorem_lineages_not_full_ancestry_certified"
    offset_pairs: int = Field(ge=0, strict=True)
    excluded_historical_sol_pair_ids: tuple[str, ...]
    excluded_historical_sol_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    historical_sol_xhigh_corpora: tuple[HistoricalSolXhighCorpusBinding, ...] = Field(min_length=1)
    historical_sol_xhigh_corpora_sha256: str = Field(pattern=HEX64_PATTERN)
    historical_sol_xhigh_pair_count: int = Field(ge=1, strict=True)
    historical_sol_xhigh_exclusion_complete: Literal[True] = True
    selected_pairs_absent_from_historical_sol_xhigh: Literal[True] = True
    selected_pair_count: int = Field(ge=1, le=64, strict=True)
    unique_source_theorem_lineage_count: int = Field(ge=1, le=64, strict=True)
    selected_pair_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_record_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_theorem_lineage_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_line_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_source_record_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_source_line_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_source_bytes_preserved: Literal[True] = True
    candidate_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    weak_config_sha256: str = Field(pattern=HEX64_PATTERN)
    family_matrix_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_manifest_selection_spec_seed_sha256: str = Field(pattern=HEX64_PATTERN)
    randomization_key_sha256: str = Field(pattern=HEX64_PATTERN)
    randomization_key_persisted_in_bundle: Literal[False] = False
    randomization_key_reconstruction_prerequisite: Literal[
        "external_secret_bytes_matching_persisted_sha256_required"
    ] = "external_secret_bytes_matching_persisted_sha256_required"
    regeneration_completeness: Literal[
        "requires_bound_v4_artifact_and_external_randomization_key_bytes"
    ] = "requires_bound_v4_artifact_and_external_randomization_key_bytes"
    weak_batch_spec_sha256: str = Field(pattern=HEX64_PATTERN)
    dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    weak_batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_cell_count: int = Field(ge=4, le=256, strict=True)
    judge_a_family_id: Literal["openai_codex_sol"] = _SOL_FAMILY
    judge_b_family_id: Literal["anthropic_fable"] = FABLE_FAMILY
    primary_eval_family_id: Literal["deepseek_v4"] = _HELDOUT_FAMILY
    execution_authorization: Literal[
        "offline_fixture_or_replay_only",
        "live_provider_calls_explicitly_authorized",
    ]
    live_provider_calls_authorized: bool = Field(strict=True)
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        if self.dispatch_cell_count != 4 * self.selected_pair_count:
            raise ValueError("authoring must produce four cells per selected pair")
        if self.unique_source_theorem_lineage_count != self.selected_pair_count:
            raise ValueError("every selected pair must have a distinct theorem lineage")
        explicit_lists = (
            self.selected_pair_ids,
            self.selected_source_record_ids,
            self.selected_source_theorem_lineage_ids,
            self.selected_source_line_sha256s,
        )
        if any(len(values) != self.selected_pair_count for values in explicit_lists):
            raise ValueError("explicit selected identities must cover every selected pair")
        if len(set(self.selected_pair_ids)) != self.selected_pair_count:
            raise ValueError("selected pair IDs must be unique")
        if len(set(self.selected_source_theorem_lineage_ids)) != self.selected_pair_count:
            raise ValueError("selected theorem lineage IDs must be unique")
        if tuple(sorted(set(self.excluded_historical_sol_pair_ids))) != (
            self.excluded_historical_sol_pair_ids
        ):
            raise ValueError("historical Sol pair exclusions must be sorted and unique")
        if self.historical_sol_xhigh_pair_count != len(self.excluded_historical_sol_pair_ids):
            raise ValueError("historical Sol pair count differs from exhaustive exclusion list")
        if len({item.summary_sha256 for item in self.historical_sol_xhigh_corpora}) != len(
            self.historical_sol_xhigh_corpora
        ):
            raise ValueError("historical Sol/xhigh corpora must be unique")
        expected_hashes = (
            (
                self.excluded_historical_sol_pair_ids_sha256,
                hash_canonical(list(self.excluded_historical_sol_pair_ids)),
            ),
            (
                self.historical_sol_xhigh_corpora_sha256,
                hash_canonical(
                    [item.model_dump(mode="json") for item in self.historical_sol_xhigh_corpora]
                ),
            ),
            (self.selected_pair_ids_sha256, hash_canonical(list(self.selected_pair_ids))),
            (
                self.selected_source_record_ids_sha256,
                hash_canonical(list(self.selected_source_record_ids)),
            ),
            (
                self.selected_source_theorem_lineage_ids_sha256,
                hash_canonical(list(self.selected_source_theorem_lineage_ids)),
            ),
            (
                self.selected_source_line_sha256s_sha256,
                hash_canonical(list(self.selected_source_line_sha256s)),
            ),
        )
        if any(observed != expected for observed, expected in expected_hashes):
            raise ValueError("explicit selected or excluded identity hash differs")
        if set(self.selected_pair_ids) & set(self.excluded_historical_sol_pair_ids):
            raise ValueError("selected pairs overlap historical Sol exclusions")
        expected = make_id(
            "lf022_sol_fable_authoring",
            self.model_dump(mode="json", exclude={"authoring_id", "source_v4_artifact_path"}),
        )
        if self.authoring_id != expected:
            raise ValueError("authoring_id differs from content")
        return self


@dataclass(frozen=True, slots=True)
class SolFablePreparedBatch:
    authoring: SolFableBatchAuthoringManifest
    authoring_path: Path
    spec: LF022WeakBatchSpec
    spec_path: Path
    dispatches: tuple[LF022WeakDispatchRecord, ...]
    dispatch_manifest: LF022WeakDispatchManifest
    batch_root: Path


def _safe(path: Path, *, label: str, allow_missing: bool) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise LF022SolFableBatchError(f"{label} is missing: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022SolFableBatchError(f"{label} contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise LF022SolFableBatchError(f"{label} parent is not a directory: {current}")
    return absolute


def _immutable(path: Path, payload: bytes, *, label: str) -> str:
    path = _safe(path, label=label, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path, label=label, allow_missing=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022SolFableBatchError(f"immutable {label} conflicts at {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022SolFableBatchError(
                    f"concurrent immutable {label} conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _matrix(repo_root: Path) -> tuple[Path, LF022ProductionFamilyMatrix]:
    path = _safe(repo_root / _FAMILY_MATRIX, label="Sol/Fable family matrix", allow_missing=False)
    matrix_model = _load_canonical_model(path, LF022ProductionFamilyMatrix)
    assert isinstance(matrix_model, LF022ProductionFamilyMatrix)
    matrix = matrix_model
    if (
        matrix.matrix_id
        != "lf022_family_matrix:377b624fddfc76cf4714d77a238479fa03b48cfb4a2a7820035cb1fc19247e09"
        or matrix.judge_family_ids != (_SOL_FAMILY, FABLE_FAMILY)
        or matrix.heldout_eval_family_id != _HELDOUT_FAMILY
    ):
        raise LF022SolFableBatchError("Sol/Fable family matrix differs from reviewed roles")
    for pin in matrix.family_registry:
        binding = pin.provider_catalog_artifact
        if binding is None:
            continue
        artifact = _safe(
            repo_root / binding.path,
            label=f"{pin.family_id} catalog",
            allow_missing=False,
        )
        if not artifact.is_file() or hash_file(artifact) != binding.sha256:
            raise LF022SolFableBatchError(f"provider catalog differs for {pin.family_id}")
    return path, matrix


def _historical_sol_xhigh_corpora(
    *,
    summary_paths: tuple[Path, ...],
    input_dir: Path,
) -> tuple[tuple[HistoricalSolXhighCorpusBinding, ...], tuple[str, ...]]:
    """Verify and copy the complete reviewed historical Sol/xhigh finding inputs."""

    safe_summaries = tuple(
        _safe(path, label="historical Sol/xhigh summary", allow_missing=False)
        for path in summary_paths
    )
    observed_summary_hashes = [hash_file(path) for path in safe_summaries]
    if (
        len(observed_summary_hashes) != len(_REQUIRED_HISTORICAL_SOL_XHIGH_SUMMARY_SHA256S)
        or frozenset(observed_summary_hashes) != _REQUIRED_HISTORICAL_SOL_XHIGH_SUMMARY_SHA256S
    ):
        raise LF022SolFableBatchError(
            "historical Sol/xhigh summaries are not the exhaustive reviewed corpus set"
        )

    bindings: list[HistoricalSolXhighCorpusBinding] = []
    all_pair_ids: set[str] = set()
    for summary_sha256, summary_path in sorted(
        zip(observed_summary_hashes, safe_summaries, strict=True)
    ):
        raw_summary = summary_path.read_bytes()

        def duplicate_free(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValueError(f"duplicate JSON key {key!r}")
                result[key] = value
            return result

        def reject_nonfinite(value: str) -> float:
            raise ValueError(f"non-finite number {value!r}")

        try:
            summary_payload = json.loads(
                raw_summary.decode("utf-8"),
                object_pairs_hook=duplicate_free,
                parse_constant=reject_nonfinite,
            )
            if raw_summary != canonical_json_bytes(summary_payload) + b"\n":
                raise ValueError("summary is not canonical under its original schema")
            summary = LF022CodexAuditSummary.model_validate(summary_payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LF022SolFableBatchError(
                f"invalid historical Sol/xhigh summary {summary_path}: {exc}"
            ) from exc
        if summary.model != "gpt-5.6-sol" or summary.reasoning_effort != "xhigh":
            raise LF022SolFableBatchError("historical finding corpus is not Sol/xhigh")
        findings_path = _safe(
            Path(summary.findings_artifact),
            label="historical Sol/xhigh findings",
            allow_missing=False,
        )
        if hash_file(findings_path) != summary.findings_sha256:
            raise LF022SolFableBatchError("historical findings hash differs from its summary")
        finding_models = _load_canonical_jsonl(findings_path, LF022CodexAuditFinding)
        findings = tuple(
            item for item in finding_models if isinstance(item, LF022CodexAuditFinding)
        )
        if len(findings) != summary.completed_judgment_count:
            raise LF022SolFableBatchError("historical findings count differs from its summary")
        pair_ids = tuple(sorted(item.pair_id for item in findings))
        if len(set(pair_ids)) != len(pair_ids):
            raise LF022SolFableBatchError("historical Sol/xhigh findings repeat one pair")
        all_pair_ids.update(pair_ids)
        stem = summary_sha256[:16]
        copied_summary = f"historical_sol_xhigh/{stem}.summary.json"
        copied_findings = f"historical_sol_xhigh/{stem}.findings.jsonl"
        _immutable(
            input_dir / copied_summary,
            summary_path.read_bytes(),
            label="historical Sol/xhigh summary copy",
        )
        _immutable(
            input_dir / copied_findings,
            findings_path.read_bytes(),
            label="historical Sol/xhigh findings copy",
        )
        bindings.append(
            HistoricalSolXhighCorpusBinding(
                summary_id=summary.summary_id,
                summary_sha256=summary_sha256,
                findings_sha256=summary.findings_sha256,
                finding_count=len(findings),
                unique_pair_count=len(pair_ids),
                pair_ids_sha256=hash_canonical(list(pair_ids)),
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                copied_summary_artifact=copied_summary,
                copied_findings_artifact=copied_findings,
            )
        )
    return tuple(bindings), tuple(sorted(all_pair_ids))


def _validate_selected_fresh(
    selected: tuple[LF022JudgeDesignRecordV4, ...],
    historical_pair_ids: tuple[str, ...],
) -> None:
    overlap = sorted({item.pair_id for item in selected} & set(historical_pair_ids))
    if overlap:
        raise LF022SolFableBatchError(
            "selected pair already has a historical Sol/xhigh finding: " + ", ".join(overlap)
        )


def _selected(
    records: tuple[LF022JudgeDesignRecordV4, ...],
    *,
    partition_id: str,
    offset_pairs: int,
    limit_pairs: int,
    excluded_pair_ids: tuple[str, ...],
) -> tuple[LF022JudgeDesignRecordV4, ...]:
    if offset_pairs < 0:
        raise LF022SolFableBatchError("offset_pairs must be nonnegative")
    if limit_pairs < 1 or limit_pairs > _MAX_PAIRS:
        raise LF022SolFableBatchError(f"limit_pairs must be within 1..{_MAX_PAIRS}")
    partition = tuple(item for item in records if item.source_partition_id == partition_id)
    if not partition:
        raise LF022SolFableBatchError(f"unknown or empty v4 partition: {partition_id}")
    excluded = tuple(sorted(set(excluded_pair_ids)))
    if excluded != excluded_pair_ids:
        raise LF022SolFableBatchError("excluded_pair_ids must be sorted and unique")
    admitted_partition = tuple(item for item in partition if item.pair_id not in set(excluded))
    by_theorem: dict[str, LF022JudgeDesignRecordV4] = {}
    for item in admitted_partition:
        theorem_ids = tuple(
            lineage for lineage in item.source_lineage_ids if lineage.startswith("thm:")
        )
        if len(theorem_ids) != 1:
            raise LF022SolFableBatchError(
                "every v4 record must carry exactly one source theorem lineage"
            )
        theorem_id = theorem_ids[0]
        previous = by_theorem.get(theorem_id)
        if previous is None or hash_canonical(
            {
                "schema": "lf022_lineage_selection_order_v1",
                "partition_id": partition_id,
                "theorem_id": theorem_id,
                "pair_id": item.pair_id,
            }
        ) < hash_canonical(
            {
                "schema": "lf022_lineage_selection_order_v1",
                "partition_id": partition_id,
                "theorem_id": theorem_id,
                "pair_id": previous.pair_id,
            }
        ):
            by_theorem[theorem_id] = item
    ordered = tuple(
        item
        for _, item in sorted(
            by_theorem.items(),
            key=lambda pair: hash_canonical(
                {
                    "schema": "lf022_lineage_selection_order_v1",
                    "partition_id": partition_id,
                    "theorem_id": pair[0],
                }
            ),
        )
    )
    selected = ordered[offset_pairs : offset_pairs + limit_pairs]
    if len(selected) != limit_pairs:
        raise LF022SolFableBatchError(
            "requested bounded selection exceeds distinct theorem-lineage count"
        )
    if len({item.proposer_family_id for item in selected}) != 1:
        raise LF022SolFableBatchError("one prepared batch must have one proposer family")
    selected_theorems = {
        lineage
        for item in selected
        for lineage in item.source_lineage_ids
        if lineage.startswith("thm:")
    }
    if len(selected_theorems) != len(selected):
        raise LF022SolFableBatchError("selected pairs do not have distinct theorem lineages")
    _validate_selected_fresh(selected, excluded)
    return selected


def _theorem_lineage(item: LF022JudgeDesignRecordV4) -> str:
    theorem_ids = tuple(
        lineage for lineage in item.source_lineage_ids if lineage.startswith("thm:")
    )
    if len(theorem_ids) != 1:
        raise LF022SolFableBatchError(
            "every selected record must carry exactly one source theorem lineage"
        )
    return theorem_ids[0]


def _derived_candidate_manifest(
    *,
    source_manifest: LF022SupervisionCandidateManifest,
    selected: tuple[LF022JudgeDesignRecordV4, ...],
    records_bytes: bytes,
    selection_spec_seed_sha256: str,
) -> tuple[LF022SupervisionCandidateManifest, bytes, bytes]:
    records = tuple(item.source_record for item in selected)
    sample = records[:10]
    sample_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in sample
    )
    status_counts = dict(sorted(Counter(item.dispatch_status for item in records).items()))
    values: dict[str, object] = {
        "schema_version": 4,
        "method_version": "lf022_supervision_candidate_inventory_v4",
        "collection_id": source_manifest.collection_id,
        "selection_spec_seed_sha256": selection_spec_seed_sha256,
        "checks_sha256": source_manifest.checks_sha256,
        "lean_check_manifest_sha256": source_manifest.lean_check_manifest_sha256,
        "codex_audit_manifest_sha256": None,
        "logical_input_binding_sha256": hash_canonical(
            {
                "schema": "lf022_sol_fable_candidate_selection_v1",
                "source_inventory_id": source_manifest.inventory_id,
                "selected_source_record_ids": [
                    item.source_candidate_inventory_record_id for item in selected
                ],
                "selected_line_sha256s": [item.source_record_line_sha256 for item in selected],
                "judge_a_family_id": _SOL_FAMILY,
                "judge_b_family_id": FABLE_FAMILY,
                "primary_eval_judge_family_id": _HELDOUT_FAMILY,
            }
        ),
        "codex_response_artifact_set_sha256": None,
        "proposer_family_id": records[0].proposer_family_id,
        "proposer_model": records[0].proposer_model,
        "judge_a_family_id": _SOL_FAMILY,
        "judge_b_family_id": FABLE_FAMILY,
        "primary_eval_judge_family_id": _HELDOUT_FAMILY,
        "records_artifact": "candidates.jsonl",
        "records_sha256": sha256_hex(records_bytes),
        "public_sample_artifact": "public_sample.jsonl",
        "public_sample_sha256": sha256_hex(sample_bytes),
        "public_sample_count": len(sample),
        "summary_artifact": "summary.md",
        "summary_sha256": "0" * 64,
        "record_count": len(records),
        "unique_judge_visible_payload_count": len(records),
        "exact_duplicate_record_count": 0,
        "dispatch_eligible_count": len(records),
        "required_future_judge_call_count": 4 * len(records),
        "codex_diagnostic_status": "absent",
        "codex_diagnostic_record_count": 0,
        "codex_same_claim_counts": {},
        "dispatch_status_counts": status_counts,
        "codex_is_diagnostic_only": True,
        "two_family_judgments_completed": False,
        "human_pilot_bound": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    summary_bytes = _render_summary(values)
    values["summary_sha256"] = sha256_hex(summary_bytes)
    identity = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "inventory_id",
            "records_artifact",
            "public_sample_artifact",
            "summary_artifact",
            "spec_sha256",
            "selection_spec_seed_sha256",
        }
    }
    manifest = LF022SupervisionCandidateManifest.model_validate(
        {
            **values,
            "inventory_id": make_id("lf022_supervision_inventory", identity),
        }
    )
    _validate_candidate_inventory_records(manifest=manifest, candidates=records)
    if _render_summary(manifest.model_dump(mode="json")) != summary_bytes:
        raise LF022SolFableBatchError("derived candidate summary does not replay")
    return manifest, sample_bytes, summary_bytes


def _revision(matrix: LF022ProductionFamilyMatrix, family_id: str) -> str:
    pin = matrix.pins_by_id[family_id]
    assert pin.provider_catalog_artifact is not None
    return f"provider-deployment-snapshot:{pin.provider_catalog_artifact.sha256}"


def prepare_lf022_sol_fable_batch_v1(
    *,
    repo_root: Path,
    v4_root: Path,
    source_partition_id: str,
    offset_pairs: int,
    limit_pairs: int,
    randomization_key: bytes,
    output_dir: Path,
    historical_sol_xhigh_summary_paths: tuple[Path, ...],
    authorize_live_provider_calls: bool = False,
) -> SolFablePreparedBatch:
    """Prepare one immutable, bounded, four-cell-per-pair batch offline."""

    if len(randomization_key) < 32:
        raise LF022SolFableBatchError("randomization key must contain at least 32 bytes")
    repo = _safe(repo_root, label="repository root", allow_missing=False)
    output = _safe(output_dir, label="Sol/Fable output", allow_missing=True)
    source_v4_artifact = _safe(
        v4_root, label="source v4 judge-design artifact", allow_missing=False
    )
    verified = verify_lf022_judge_design_v4(source_v4_artifact)
    input_dir = output / "authoring" / "inputs"
    historical_corpora, exclusions = _historical_sol_xhigh_corpora(
        summary_paths=historical_sol_xhigh_summary_paths,
        input_dir=input_dir,
    )
    selected = _selected(
        verified.records,
        partition_id=source_partition_id,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
        excluded_pair_ids=exclusions,
    )
    source_binding = next(
        item
        for item in verified.manifest.source_partitions
        if item.partition_id == source_partition_id
    )
    source_manifest_path = verified.output_dir / source_binding.source_manifest_artifact
    source_manifest_model = _load_canonical_model(
        source_manifest_path, LF022SupervisionCandidateManifest
    )
    assert isinstance(source_manifest_model, LF022SupervisionCandidateManifest)
    source_manifest = source_manifest_model
    matrix_path, matrix = _matrix(repo)
    weak_config_path = _safe(repo / _WEAK_CONFIG, label="weak config", allow_missing=False)
    _validate_weak_config(weak_config_path)
    fable_config = load_claude_fable_judge_config(repo / _FABLE_CONFIG).config

    selected_lines: list[bytes] = []
    for item in selected:
        line = canonical_json_bytes(item.source_record.model_dump(mode="json")) + b"\n"
        if sha256_hex(line) != item.source_record_line_sha256:
            raise LF022SolFableBatchError("selected v3 source bytes do not replay")
        selected_lines.append(line)
    records_bytes = b"".join(selected_lines)
    spec_seed = hash_canonical(
        {
            "source_v4_inventory_id": verified.manifest.inventory_id,
            "source_partition_id": source_partition_id,
            "offset_pairs": offset_pairs,
            "excluded_historical_sol_pair_ids": list(exclusions),
            "historical_sol_xhigh_corpora": [
                item.model_dump(mode="json") for item in historical_corpora
            ],
            "selected_pair_ids": [item.pair_id for item in selected],
            "family_matrix_sha256": hash_file(matrix_path),
            "weak_config_sha256": hash_file(weak_config_path),
        }
    )
    candidate_manifest, sample_bytes, summary_bytes = _derived_candidate_manifest(
        source_manifest=source_manifest,
        selected=selected,
        records_bytes=records_bytes,
        selection_spec_seed_sha256=spec_seed,
    )
    candidate_manifest_path = input_dir / "candidate_manifest.json"
    candidate_records_path = input_dir / "candidates.jsonl"
    _immutable(
        candidate_manifest_path,
        canonical_json_bytes(candidate_manifest.model_dump(mode="json")) + b"\n",
        label="selected candidate manifest",
    )
    _immutable(candidate_records_path, records_bytes, label="selected candidate records")
    _immutable(input_dir / "public_sample.jsonl", sample_bytes, label="candidate sample")
    _immutable(input_dir / "summary.md", summary_bytes, label="candidate summary")
    matrix_copy = input_dir / "family_matrix.json"
    weak_copy = input_dir / "weak_supervision.yaml"
    _immutable(matrix_copy, matrix_path.read_bytes(), label="family matrix")
    _immutable(weak_copy, weak_config_path.read_bytes(), label="weak config")

    def bound(path: Path) -> BoundArtifact:
        return BoundArtifact(path=str(path), sha256=hash_file(path))

    sol_decoding = cast(
        dict[str, DecodingValue],
        {
            "reasoning_effort": _SOL_EFFORT,
            "structured_output": True,
            "system_prompt_sha256": (
                "4c1f6de9fb14818e7432a0b9bdac2ca9ef784cccfd2f73ebc8774a4d0d6588bf"
            ),
            "output_schema_sha256": (
                "9de1b73c98a5df344ac158f77ead4b1b6e118b4c2f5585335fd5a3bcf0dea4d4"
            ),
            "codex_cli_version": "codex-cli 0.144.1",
            "codex_binary_sha256": (
                "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
            ),
            "shell_tool_disabled": True,
            "sandbox": "read-only",
            "web_search": "disabled",
        },
    )
    fable_decoding = cast(
        dict[str, DecodingValue],
        {
            "effort": fable_config.effort,
            "system_prompt_sha256": fable_config.system_prompt_sha256,
            "output_schema_sha256": fable_config.output_schema_sha256,
            "claude_cli_version": fable_config.claude_cli_version,
            "claude_binary_sha256": fable_config.claude_binary_sha256,
            "structured_output": True,
            "safe_mode": True,
            "tools_disabled": True,
            "session_persistence": False,
        },
    )
    spec = LF022WeakBatchSpec(
        batch_name=(f"sol_fable_{source_partition_id}_offset{offset_pairs}_n{limit_pairs}_v3"),
        candidate_manifest=bound(candidate_manifest_path),
        candidate_records=bound(candidate_records_path),
        weak_supervision_config=bound(weak_copy),
        production_family_matrix=bound(matrix_copy),
        randomization_key_sha256=sha256_hex(randomization_key),
        judge_a=JudgeEndpointPin(
            provider_slot="judge_A",
            provider=_SOL_PROVIDER,
            model=_SOL_MODEL,
            family_id=_SOL_FAMILY,
            revision=_revision(matrix, _SOL_FAMILY),
            decoding=sol_decoding,
        ),
        judge_b=JudgeEndpointPin(
            provider_slot="judge_B",
            provider=FABLE_PROVIDER,
            model=fable_config.registry_model_id,
            family_id=FABLE_FAMILY,
            revision=_revision(matrix, FABLE_FAMILY),
            decoding=fable_decoding,
        ),
        primary_eval_family_id=_HELDOUT_FAMILY,
        execution_authorization=(
            "live_provider_calls_explicitly_authorized"
            if authorize_live_provider_calls
            else "offline_fixture_or_replay_only"
        ),
        live_provider_calls_authorized=authorize_live_provider_calls,
    )
    _validate_family_pins(spec, matrix_copy)
    spec_path = output / "authoring" / "weak_batch_spec.json"
    spec_sha = _immutable(
        spec_path,
        canonical_json_bytes(spec.model_dump(mode="json")) + b"\n",
        label="weak batch spec",
    )
    batch_root = output / "batch"
    dispatches, dispatch_manifest = prepare_lf022_weak_batch(
        repo_root=repo,
        spec_path=spec_path,
        expected_spec_sha256=spec_sha,
        randomization_key=randomization_key,
        output_dir=batch_root,
    )
    authoring_values: dict[str, object] = {
        "schema_version": 3,
        "method_version": SOL_FABLE_BATCH_METHOD_VERSION,
        "source_v4_artifact_path": str(source_v4_artifact),
        "source_v4_inventory_id": verified.manifest.inventory_id,
        "source_v4_manifest_sha256": hash_file(verified.manifest_path),
        "source_v4_records_sha256": verified.manifest.records_sha256,
        "source_partition_id": source_partition_id,
        "proposer_family_id": selected[0].proposer_family_id,
        "selection_method": (
            "deterministic_theorem_lineage_hash_with_exhaustive_sol_xhigh_exclusions_v3"
        ),
        "lineage_diversity_status": (
            "distinct_source_theorem_lineages_not_full_ancestry_certified"
        ),
        "offset_pairs": offset_pairs,
        "excluded_historical_sol_pair_ids": exclusions,
        "excluded_historical_sol_pair_ids_sha256": hash_canonical(list(exclusions)),
        "historical_sol_xhigh_corpora": [
            item.model_dump(mode="json") for item in historical_corpora
        ],
        "historical_sol_xhigh_corpora_sha256": hash_canonical(
            [item.model_dump(mode="json") for item in historical_corpora]
        ),
        "historical_sol_xhigh_pair_count": len(exclusions),
        "historical_sol_xhigh_exclusion_complete": True,
        "selected_pairs_absent_from_historical_sol_xhigh": True,
        "selected_pair_count": len(selected),
        "unique_source_theorem_lineage_count": len({_theorem_lineage(item) for item in selected}),
        "selected_pair_ids": tuple(item.pair_id for item in selected),
        "selected_source_record_ids": tuple(
            item.source_candidate_inventory_record_id for item in selected
        ),
        "selected_source_theorem_lineage_ids": tuple(_theorem_lineage(item) for item in selected),
        "selected_source_line_sha256s": tuple(item.source_record_line_sha256 for item in selected),
        "selected_source_theorem_lineage_ids_sha256": hash_canonical(
            [_theorem_lineage(item) for item in selected]
        ),
        "selected_source_record_ids_sha256": hash_canonical(
            [item.source_candidate_inventory_record_id for item in selected]
        ),
        "selected_source_line_sha256s_sha256": hash_canonical(
            [item.source_record_line_sha256 for item in selected]
        ),
        "selected_pair_ids_sha256": hash_canonical([item.pair_id for item in selected]),
        "selected_source_bytes_preserved": True,
        "candidate_manifest_sha256": hash_file(candidate_manifest_path),
        "candidate_records_sha256": hash_file(candidate_records_path),
        "weak_config_sha256": hash_file(weak_copy),
        "family_matrix_sha256": hash_file(matrix_copy),
        "candidate_manifest_selection_spec_seed_sha256": spec_seed,
        "randomization_key_sha256": sha256_hex(randomization_key),
        "randomization_key_persisted_in_bundle": False,
        "randomization_key_reconstruction_prerequisite": (
            "external_secret_bytes_matching_persisted_sha256_required"
        ),
        "regeneration_completeness": (
            "requires_bound_v4_artifact_and_external_randomization_key_bytes"
        ),
        "weak_batch_spec_sha256": spec_sha,
        "dispatch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "weak_batch_id": dispatch_manifest.batch_id,
        "dispatch_cell_count": len(dispatches),
        "judge_a_family_id": _SOL_FAMILY,
        "judge_b_family_id": FABLE_FAMILY,
        "primary_eval_family_id": _HELDOUT_FAMILY,
        "execution_authorization": spec.execution_authorization,
        "live_provider_calls_authorized": spec.live_provider_calls_authorized,
        "semantic_labels_created": False,
        "human_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    authoring = SolFableBatchAuthoringManifest.model_validate(
        {
            **authoring_values,
            "authoring_id": make_id(
                "lf022_sol_fable_authoring",
                {
                    key: value
                    for key, value in authoring_values.items()
                    if key != "source_v4_artifact_path"
                },
            ),
        }
    )
    authoring_path = output / "authoring_manifest.json"
    _immutable(
        authoring_path,
        canonical_json_bytes(authoring.model_dump(mode="json")) + b"\n",
        label="authoring manifest",
    )
    return SolFablePreparedBatch(
        authoring=authoring,
        authoring_path=authoring_path,
        spec=spec,
        spec_path=spec_path,
        dispatches=dispatches,
        dispatch_manifest=dispatch_manifest,
        batch_root=batch_root,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--v4-root", type=Path, required=True)
    parser.add_argument("--source-partition", required=True)
    parser.add_argument("--offset-pairs", type=int, default=0)
    parser.add_argument("--limit-pairs", type=int, required=True)
    parser.add_argument("--randomization-key-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--historical-sol-xhigh-summary",
        type=Path,
        action="append",
        required=True,
        help="Complete reviewed Sol/xhigh summary; repeat for every required corpus.",
    )
    parser.add_argument("--authorize-live-provider-calls", action="store_true")
    arguments = parser.parse_args()
    key_path = _safe(
        arguments.randomization_key_file,
        label="randomization key file",
        allow_missing=False,
    )
    if not key_path.is_file():
        raise LF022SolFableBatchError("randomization key is not a regular file")
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=arguments.repo_root,
        v4_root=arguments.v4_root,
        source_partition_id=arguments.source_partition,
        offset_pairs=arguments.offset_pairs,
        limit_pairs=arguments.limit_pairs,
        randomization_key=key_path.read_bytes(),
        output_dir=arguments.output_dir,
        historical_sol_xhigh_summary_paths=tuple(arguments.historical_sol_xhigh_summary),
        authorize_live_provider_calls=arguments.authorize_live_provider_calls,
    )
    print(result.authoring.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "SOL_FABLE_BATCH_METHOD_VERSION",
    "HistoricalSolXhighCorpusBinding",
    "LF022SolFableBatchError",
    "SolFableBatchAuthoringManifest",
    "SolFablePreparedBatch",
    "prepare_lf022_sol_fable_batch_v1",
]
