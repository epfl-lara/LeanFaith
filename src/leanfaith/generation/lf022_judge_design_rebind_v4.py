"""Content-preserving LF-022 v3 -> Sol/Fable judge-design freeze.

The two existing schema-v3 candidate inventories bind an older judge-family
allocation.  Rewriting those bytes in place would destroy the evidence chain.
This module instead verifies and copies the exact v3 inputs, wraps each source
record in a content-addressed v4 routing record, and freezes a two-partition
919-pair inventory with:

* ``gpt-5.6-sol`` / xhigh as ``judge_A``;
* ``claude-fable-5`` / max as ``judge_B``; and
* DeepSeek V4 reserved outside supervision as the evaluation family.

It performs no model call and creates no label.  In particular, any historical
Codex diagnostic embedded in a v3 source record remains diagnostic-only and is
not imported as a Sol weak-judge vote.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
)
from leanfaith.generation.lf022_weak_batch import _validate_candidate_inventory_records
from leanfaith.generation.weak_supervision import FamilySeparationMatrix, validate_family_separation
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id

JUDGE_REBIND_METHOD_VERSION: Literal["lf022_judge_design_rebind_v4"] = (
    "lf022_judge_design_rebind_v4"
)
_REQUIRED_CELLS = ("judge_A:AB", "judge_A:BA", "judge_B:AB", "judge_B:BA")


class LF022JudgeDesignRebindError(RuntimeError):
    """A source, routing, content, privacy, or immutable-output check failed."""


class RebindArtifact(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


class RebindSourcePartitionSpec(StrictModel):
    partition_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3"]
    expected_inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    expected_record_count: int = Field(ge=1, strict=True)
    manifest: RebindArtifact
    records: RebindArtifact


class RebindJudgeEndpoint(StrictModel):
    slot: Literal["judge_A", "judge_B"]
    provider: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: Literal["xhigh", "max"]
    client: str = Field(min_length=1)
    client_version: str = Field(min_length=1)
    client_binary_sha256: str = Field(pattern=HEX64_PATTERN)
    server_model_revision_status: Literal["unavailable_floating_provider_alias"]


class RebindHeldoutEndpoint(StrictModel):
    provider: Literal["epfl_rcp"]
    family_id: Literal["deepseek_v4"]
    model: Literal["deepseek-ai/DeepSeek-V4-Pro"]
    provider_catalog: RebindArtifact
    underlying_checkpoint_revision_status: Literal["provider_not_disclosed"]


class LF022JudgeDesignRebindSpecV4(StrictModel):
    schema_version: Literal[4] = 4
    method_version: Literal["lf022_judge_design_rebind_v4"] = JUDGE_REBIND_METHOD_VERSION
    config_id: Literal["lf022_sol_fable_public_rebind_v4"]
    collection_id: Literal["lf022_kimi_qwen_public_sol_fable_v4"]
    source_partitions: tuple[RebindSourcePartitionSpec, ...] = Field(min_length=2, max_length=2)
    judge_a: RebindJudgeEndpoint
    judge_b: RebindJudgeEndpoint
    primary_eval: RebindHeldoutEndpoint
    expected_record_count: int = Field(ge=1, strict=True)
    expected_proposer_counts: dict[str, int]
    forbidden_proposer_family_ids: tuple[Literal["deepseek_v4"], ...] = ("deepseek_v4",)
    require_unique_pair_ids: Literal[True] = True
    require_unique_judge_payloads: Literal[True] = True
    public_sources_only: Literal[True] = True
    private_source_content_forbidden: Literal[True] = True
    preserve_source_record_bytes: Literal[True] = True
    historical_codex_diagnostics_are_votes: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _routing(self) -> Self:
        if self.judge_a.slot != "judge_A" or self.judge_b.slot != "judge_B":
            raise ValueError("judge endpoints must occupy their named slots")
        if (
            self.judge_a.family_id != "openai_codex_sol"
            or self.judge_a.model != "gpt-5.6-sol"
            or self.judge_a.effort != "xhigh"
        ):
            raise ValueError("judge_A must be exact Sol/xhigh")
        if (
            self.judge_b.family_id != "anthropic_fable"
            or self.judge_b.model != "claude-fable-5"
            or self.judge_b.effort != "max"
        ):
            raise ValueError("judge_B must be exact Fable/max")
        partition_ids = [item.partition_id for item in self.source_partitions]
        proposer_ids = [item.proposer_family_id for item in self.source_partitions]
        if len(set(partition_ids)) != len(partition_ids) or list(partition_ids) != sorted(
            partition_ids
        ):
            raise ValueError("source partitions must be sorted and unique")
        if set(proposer_ids) != {"moonshot_kimi_k2", "qwen3"}:
            raise ValueError("v4 source partitions must be exactly Kimi and Qwen")
        if set(self.expected_proposer_counts) != set(proposer_ids):
            raise ValueError("expected proposer counts must cover the two source families")
        partition_counts = {
            item.proposer_family_id: item.expected_record_count for item in self.source_partitions
        }
        if (
            self.expected_proposer_counts != partition_counts
            or sum(self.expected_proposer_counts.values()) != self.expected_record_count
        ):
            raise ValueError("expected proposer and total counts must match source partitions")
        for proposer in proposer_ids:
            validate_family_separation(
                FamilySeparationMatrix(
                    proposer_family=proposer,
                    judge_a_family=self.judge_a.family_id,
                    judge_b_family=self.judge_b.family_id,
                    primary_eval_judge_family=self.primary_eval.family_id,
                )
            )
        return self


class ReboundSourceBindingV4(StrictModel):
    partition_id: str
    proposer_family_id: str
    source_inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    source_manifest_artifact: str
    source_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    source_records_artifact: str
    source_records_sha256: str = Field(pattern=HEX64_PATTERN)
    source_record_count: int = Field(ge=1, strict=True)
    source_bytes_preserved_exactly: Literal[True] = True


class LF022JudgeDesignRecordV4(StrictModel):
    """One source-v3 pair plus a new, non-labeling judge allocation."""

    schema_version: Literal[4] = 4
    method_version: Literal["lf022_judge_design_rebind_v4"] = JUDGE_REBIND_METHOD_VERSION
    record_id: str = Field(pattern=id_pattern("lf022_judge_design"))
    collection_id: Literal["lf022_kimi_qwen_public_sol_fable_v4"]
    source_partition_id: str
    source_inventory_id: str = Field(pattern=id_pattern("lf022_supervision_inventory"))
    source_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    source_records_sha256: str = Field(pattern=HEX64_PATTERN)
    source_record_line_sha256: str = Field(pattern=HEX64_PATTERN)
    source_candidate_inventory_record_id: str = Field(
        pattern=id_pattern("lf022_supervision_candidate")
    )
    source_record: LF022SupervisionCandidateRecord
    pair_id: str = Field(pattern=id_pattern("pair"))
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3"]
    proposer_model: str
    source_lineage_ids: tuple[str, ...] = Field(min_length=1)
    source_lineage_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_visible_payload_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_a_family_id: Literal["openai_codex_sol"] = "openai_codex_sol"
    judge_a_model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    judge_a_effort: Literal["xhigh"] = "xhigh"
    judge_b_family_id: Literal["anthropic_fable"] = "anthropic_fable"
    judge_b_model: Literal["claude-fable-5"] = "claude-fable-5"
    judge_b_effort: Literal["max"] = "max"
    primary_eval_family_id: Literal["deepseek_v4"] = "deepseek_v4"
    required_judgment_cells: tuple[str, ...]
    historical_codex_diagnostic_weak_vote: Literal[False] = False
    candidate_state: Literal["unresolved_awaiting_sol_fable_swapped_judging"]
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _preserved_and_unresolved(self) -> Self:
        source = self.source_record
        if source.schema_version != 3:
            raise ValueError("v4 routing records require source schema v3")
        if source.dispatch_status != "ready_for_two_family_judging":
            raise ValueError("v4 routes only canonical dispatch records")
        if (
            self.source_candidate_inventory_record_id != source.candidate_inventory_record_id
            or self.pair_id != source.pair_id
            or self.proposer_family_id != source.proposer_family_id
            or self.proposer_model != source.proposer_model
            or self.source_lineage_ids != source.pair.source_record_ids
            or self.source_lineage_sha256 != hash_canonical(list(source.pair.source_record_ids))
            or self.judge_visible_payload_sha256 != source.judge_visible_payload_sha256
        ):
            raise ValueError("v4 record differs from source identity, proposer, or lineage")
        if (
            not source.pair.source_is_public
            or source.pair.private_source_content
            or not source.pair.external_transmission_allowed
            or not source.pair.denylist_checked
            or source.pair.denylist_hits
        ):
            raise ValueError("v4 record contains private, forbidden, or denylisted content")
        if str(self.proposer_family_id) == self.primary_eval_family_id:
            raise ValueError("DeepSeek-origin input cannot use DeepSeek as held-out evaluator")
        validate_family_separation(
            FamilySeparationMatrix(
                proposer_family=self.proposer_family_id,
                judge_a_family=self.judge_a_family_id,
                judge_b_family=self.judge_b_family_id,
                primary_eval_judge_family=self.primary_eval_family_id,
            )
        )
        if self.required_judgment_cells != _REQUIRED_CELLS:
            raise ValueError("v4 record requires both families in both orientations")
        expected = make_id(
            "lf022_judge_design",
            self.model_dump(mode="json", exclude={"record_id"}),
        )
        if self.record_id != expected:
            raise ValueError("record_id differs from v4 content")
        return self


class LF022JudgeDesignManifestV4(StrictModel):
    schema_version: Literal[4] = 4
    method_version: Literal["lf022_judge_design_rebind_v4"] = JUDGE_REBIND_METHOD_VERSION
    inventory_id: str = Field(pattern=id_pattern("lf022_judge_design_inventory"))
    collection_id: Literal["lf022_kimi_qwen_public_sol_fable_v4"]
    spec_sha256: str = Field(pattern=HEX64_PATTERN)
    source_partitions: tuple[ReboundSourceBindingV4, ...] = Field(min_length=2, max_length=2)
    records_artifact: Literal["records.jsonl"] = "records.jsonl"
    records_sha256: str = Field(pattern=HEX64_PATTERN)
    record_count: int = Field(ge=1, strict=True)
    unique_pair_count: int = Field(ge=1, strict=True)
    unique_judge_visible_payload_count: int = Field(ge=1, strict=True)
    proposer_counts: dict[str, int]
    forbidden_proposer_count: Literal[0] = 0
    ordered_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    ordered_source_lineages_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_a_family_id: Literal["openai_codex_sol"] = "openai_codex_sol"
    judge_a_model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    judge_a_effort: Literal["xhigh"] = "xhigh"
    judge_b_family_id: Literal["anthropic_fable"] = "anthropic_fable"
    judge_b_model: Literal["claude-fable-5"] = "claude-fable-5"
    judge_b_effort: Literal["max"] = "max"
    primary_eval_family_id: Literal["deepseek_v4"] = "deepseek_v4"
    required_future_judge_call_count: int = Field(ge=4, strict=True)
    source_v3_bytes_unchanged: Literal[True] = True
    historical_codex_diagnostics_are_votes: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        if (
            set(self.proposer_counts) != {"moonshot_kimi_k2", "qwen3"}
            or sum(self.proposer_counts.values()) != self.record_count
        ):
            raise ValueError("v4 proposer counts differ from source corpus")
        if (
            sum(item.source_record_count for item in self.source_partitions) != self.record_count
            or self.unique_pair_count != self.record_count
            or self.unique_judge_visible_payload_count != self.record_count
            or self.required_future_judge_call_count != 4 * self.record_count
        ):
            raise ValueError("source partition counts do not reconcile")
        expected = make_id(
            "lf022_judge_design_inventory",
            self.model_dump(mode="json", exclude={"inventory_id", "spec_sha256"}),
        )
        if self.inventory_id != expected:
            raise ValueError("inventory_id differs from v4 manifest content")
        return self


@dataclass(frozen=True, slots=True)
class LoadedSourcePartition:
    spec: RebindSourcePartitionSpec
    manifest: LF022SupervisionCandidateManifest
    manifest_bytes: bytes
    records: tuple[LF022SupervisionCandidateRecord, ...]
    record_lines: tuple[bytes, ...]
    records_bytes: bytes


@dataclass(frozen=True, slots=True)
class JudgeDesignFreezeResult:
    records: tuple[LF022JudgeDesignRecordV4, ...]
    manifest: LF022JudgeDesignManifestV4
    output_dir: Path
    manifest_path: Path


def _safe_path(path: Path, *, label: str, allow_missing: bool) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise LF022JudgeDesignRebindError(f"{label} is missing: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022JudgeDesignRebindError(f"{label} contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise LF022JudgeDesignRebindError(f"{label} parent is not a directory: {current}")
    return absolute


def _bound_bytes(binding: RebindArtifact, *, label: str) -> bytes:
    path = _safe_path(Path(binding.path), label=label, allow_missing=False)
    if not path.is_file() or hash_file(path) != binding.sha256:
        raise LF022JudgeDesignRebindError(f"{label} differs from its frozen binding: {path}")
    return path.read_bytes()


def _canonical_jsonl(records: tuple[StrictModel, ...]) -> bytes:
    return b"".join(canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in records)


def _load_partition(source: RebindSourcePartitionSpec) -> LoadedSourcePartition:
    manifest_bytes = _bound_bytes(source.manifest, label=f"{source.partition_id} manifest")
    records_bytes = _bound_bytes(source.records, label=f"{source.partition_id} records")
    try:
        manifest = LF022SupervisionCandidateManifest.model_validate_json(manifest_bytes)
    except ValueError as exc:
        raise LF022JudgeDesignRebindError(
            f"invalid {source.partition_id} source manifest: {exc}"
        ) from exc
    records: list[LF022SupervisionCandidateRecord] = []
    lines = records_bytes.splitlines(keepends=True)
    for line_number, line in enumerate(lines, start=1):
        if not line.endswith(b"\n"):
            raise LF022JudgeDesignRebindError(
                f"{source.partition_id} record {line_number} lacks final newline"
            )
        try:
            record = LF022SupervisionCandidateRecord.model_validate_json(line)
        except ValueError as exc:
            raise LF022JudgeDesignRebindError(
                f"invalid {source.partition_id} record {line_number}: {exc}"
            ) from exc
        canonical = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
        if line != canonical:
            raise LF022JudgeDesignRebindError(
                f"{source.partition_id} record {line_number} is not canonical"
            )
        records.append(record)
    if (
        manifest.schema_version != 3
        or manifest.inventory_id != source.expected_inventory_id
        or manifest.proposer_family_id != source.proposer_family_id
        or manifest.record_count != source.expected_record_count
        or manifest.records_sha256 != source.records.sha256
        or len(records) != source.expected_record_count
    ):
        raise LF022JudgeDesignRebindError(
            f"{source.partition_id} source identity/count differs from the v4 spec"
        )
    try:
        _validate_candidate_inventory_records(manifest=manifest, candidates=tuple(records))
    except LF022JudgeDesignRebindError:
        raise
    except Exception as exc:
        raise LF022JudgeDesignRebindError(
            f"{source.partition_id} source inventory replay failed: {exc}"
        ) from exc
    return LoadedSourcePartition(
        spec=source,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        records=tuple(records),
        record_lines=tuple(lines),
        records_bytes=records_bytes,
    )


def _write_immutable(path: Path, payload: bytes, *, label: str) -> None:
    path = _safe_path(path, label=label, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(path, label=label, allow_missing=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022JudgeDesignRebindError(f"immutable {label} conflicts at {path}")
        return
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
                raise LF022JudgeDesignRebindError(
                    f"concurrent immutable {label} conflict at {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _record(
    *,
    spec: LF022JudgeDesignRebindSpecV4,
    source: LoadedSourcePartition,
    record: LF022SupervisionCandidateRecord,
    source_line: bytes,
) -> LF022JudgeDesignRecordV4:
    values: dict[str, object] = {
        "schema_version": 4,
        "method_version": JUDGE_REBIND_METHOD_VERSION,
        "collection_id": spec.collection_id,
        "source_partition_id": source.spec.partition_id,
        "source_inventory_id": source.manifest.inventory_id,
        "source_manifest_sha256": source.spec.manifest.sha256,
        "source_records_sha256": source.spec.records.sha256,
        "source_record_line_sha256": sha256_hex(source_line),
        "source_candidate_inventory_record_id": record.candidate_inventory_record_id,
        "source_record": record.model_dump(mode="json"),
        "pair_id": record.pair_id,
        "proposer_family_id": record.proposer_family_id,
        "proposer_model": record.proposer_model,
        "source_lineage_ids": list(record.pair.source_record_ids),
        "source_lineage_sha256": hash_canonical(list(record.pair.source_record_ids)),
        "judge_visible_payload_sha256": record.judge_visible_payload_sha256,
        "judge_a_family_id": spec.judge_a.family_id,
        "judge_a_model": spec.judge_a.model,
        "judge_a_effort": spec.judge_a.effort,
        "judge_b_family_id": spec.judge_b.family_id,
        "judge_b_model": spec.judge_b.model,
        "judge_b_effort": spec.judge_b.effort,
        "primary_eval_family_id": spec.primary_eval.family_id,
        "required_judgment_cells": list(_REQUIRED_CELLS),
        "historical_codex_diagnostic_weak_vote": False,
        "candidate_state": "unresolved_awaiting_sol_fable_swapped_judging",
        "semantic_labels_created": False,
        "human_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022JudgeDesignRecordV4.model_validate(
        {**values, "record_id": make_id("lf022_judge_design", values)}
    )


def _summary(manifest: LF022JudgeDesignManifestV4) -> bytes:
    return (
        "# LF-022 Sol/Fable judge-design inventory v4\n\n"
        "This is an offline, content-preserving routing freeze. It creates no semantic, "
        "human, silver, training, evaluation, or gate-credit label.\n\n"
        f"- Public unresolved pairs: {manifest.record_count}\n"
        f"- Kimi proposer pairs: {manifest.proposer_counts['moonshot_kimi_k2']}\n"
        f"- Qwen proposer pairs: {manifest.proposer_counts['qwen3']}\n"
        f"- DeepSeek-origin pairs: {manifest.forbidden_proposer_count}\n"
        f"- Required future calls: {manifest.required_future_judge_call_count}\n"
        f"- judge_A: `{manifest.judge_a_model}` / `{manifest.judge_a_effort}`\n"
        f"- judge_B: `{manifest.judge_b_model}` / `{manifest.judge_b_effort}`\n"
        f"- held-out evaluation family: `{manifest.primary_eval_family_id}`\n\n"
        "Every source v3 manifest and candidate JSONL file is copied byte-for-byte under "
        "`inputs/`. Historical Codex diagnostics remain diagnostic-only and contribute zero "
        "votes. Each pair still requires Sol AB/BA and Fable AB/BA.\n"
    ).encode()


def freeze_lf022_judge_design_v4(
    *,
    config_path: Path,
    output_dir: Path,
) -> JudgeDesignFreezeResult:
    """Verify, wrap, and immutably freeze the exact 919-pair public inventory."""

    loaded: LoadedConfig[LF022JudgeDesignRebindSpecV4] = load_config(
        config_path, LF022JudgeDesignRebindSpecV4
    )
    spec = loaded.config
    sources = tuple(_load_partition(item) for item in spec.source_partitions)
    all_records: list[LF022JudgeDesignRecordV4] = []
    for source in sources:
        if len(source.records) != len(source.record_lines):
            raise LF022JudgeDesignRebindError("source record/line counts differ")
        for record, line in zip(source.records, source.record_lines, strict=True):
            all_records.append(_record(spec=spec, source=source, record=record, source_line=line))
    all_records.sort(key=lambda item: (item.judge_visible_payload_sha256, item.pair_id))
    records = tuple(all_records)
    proposer_counts = dict(sorted(Counter(item.proposer_family_id for item in records).items()))
    pair_ids = [item.pair_id for item in records]
    payloads = [item.judge_visible_payload_sha256 for item in records]
    if (
        len(records) != spec.expected_record_count
        or proposer_counts != spec.expected_proposer_counts
        or len(set(pair_ids)) != len(records)
        or len(set(payloads)) != len(records)
    ):
        raise LF022JudgeDesignRebindError(
            "v4 combined record, proposer, pair, or payload counts differ from the freeze"
        )
    forbidden = {str(item) for item in spec.forbidden_proposer_family_ids}
    forbidden_count = sum(str(item.proposer_family_id) in forbidden for item in records)
    if forbidden_count:
        raise LF022JudgeDesignRebindError("DeepSeek-origin records cannot enter this route")

    output = _safe_path(output_dir, label="v4 output", allow_missing=True)
    output.mkdir(parents=True, exist_ok=True)
    source_bindings: list[ReboundSourceBindingV4] = []
    for source in sources:
        prefix = f"inputs/{source.spec.partition_id}"
        manifest_artifact = f"{prefix}/manifest.json"
        records_artifact = f"{prefix}/candidates.jsonl"
        _write_immutable(
            output / manifest_artifact,
            source.manifest_bytes,
            label=f"{source.spec.partition_id} source manifest copy",
        )
        _write_immutable(
            output / records_artifact,
            source.records_bytes,
            label=f"{source.spec.partition_id} source records copy",
        )
        source_bindings.append(
            ReboundSourceBindingV4(
                partition_id=source.spec.partition_id,
                proposer_family_id=source.spec.proposer_family_id,
                source_inventory_id=source.manifest.inventory_id,
                source_manifest_artifact=manifest_artifact,
                source_manifest_sha256=source.spec.manifest.sha256,
                source_records_artifact=records_artifact,
                source_records_sha256=source.spec.records.sha256,
                source_record_count=len(source.records),
                source_bytes_preserved_exactly=True,
            )
        )
    record_bytes = _canonical_jsonl(records)
    manifest_values: dict[str, object] = {
        "schema_version": 4,
        "method_version": JUDGE_REBIND_METHOD_VERSION,
        "collection_id": spec.collection_id,
        "source_partitions": [item.model_dump(mode="json") for item in source_bindings],
        "records_artifact": "records.jsonl",
        "records_sha256": sha256_hex(record_bytes),
        "record_count": len(records),
        "unique_pair_count": len(set(pair_ids)),
        "unique_judge_visible_payload_count": len(set(payloads)),
        "proposer_counts": proposer_counts,
        "forbidden_proposer_count": forbidden_count,
        "ordered_pair_ids_sha256": hash_canonical(pair_ids),
        "ordered_source_lineages_sha256": hash_canonical(
            [list(item.source_lineage_ids) for item in records]
        ),
        "judge_a_family_id": spec.judge_a.family_id,
        "judge_a_model": spec.judge_a.model,
        "judge_a_effort": spec.judge_a.effort,
        "judge_b_family_id": spec.judge_b.family_id,
        "judge_b_model": spec.judge_b.model,
        "judge_b_effort": spec.judge_b.effort,
        "primary_eval_family_id": spec.primary_eval.family_id,
        "required_future_judge_call_count": 4 * len(records),
        "source_v3_bytes_unchanged": True,
        "historical_codex_diagnostics_are_votes": False,
        "semantic_labels_created": False,
        "human_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    inventory_id = make_id("lf022_judge_design_inventory", manifest_values)
    manifest = LF022JudgeDesignManifestV4.model_validate(
        {
            **manifest_values,
            "spec_sha256": hash_file(config_path),
            "inventory_id": inventory_id,
        }
    )
    _write_immutable(output / "records.jsonl", record_bytes, label="v4 records")
    _write_immutable(
        output / "manifest.json",
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
        label="v4 manifest",
    )
    _write_immutable(output / "summary.md", _summary(manifest), label="v4 summary")
    return JudgeDesignFreezeResult(
        records=records,
        manifest=manifest,
        output_dir=output,
        manifest_path=output / "manifest.json",
    )


def verify_lf022_judge_design_v4(output_dir: Path) -> JudgeDesignFreezeResult:
    """Replay one frozen v4 directory without consulting the original paths."""

    output = _safe_path(output_dir, label="v4 replay root", allow_missing=False)
    manifest_path = output / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise LF022JudgeDesignRebindError("v4 manifest is missing or unsafe")
    try:
        manifest = LF022JudgeDesignManifestV4.model_validate_json(manifest_path.read_bytes())
    except ValueError as exc:
        raise LF022JudgeDesignRebindError(f"invalid v4 manifest: {exc}") from exc
    records_path = output / manifest.records_artifact
    if records_path.is_symlink() or not records_path.is_file():
        raise LF022JudgeDesignRebindError("v4 records are missing or unsafe")
    record_bytes = records_path.read_bytes()
    if sha256_hex(record_bytes) != manifest.records_sha256:
        raise LF022JudgeDesignRebindError("v4 records hash differs")
    records: list[LF022JudgeDesignRecordV4] = []
    for line_number, line in enumerate(record_bytes.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise LF022JudgeDesignRebindError(f"v4 line lacks newline: {line_number}")
        try:
            record = LF022JudgeDesignRecordV4.model_validate_json(line)
        except ValueError as exc:
            raise LF022JudgeDesignRebindError(f"invalid v4 line {line_number}: {exc}") from exc
        if canonical_json_bytes(record.model_dump(mode="json")) + b"\n" != line:
            raise LF022JudgeDesignRebindError(f"noncanonical v4 line: {line_number}")
        records.append(record)
    if len(records) != manifest.record_count:
        raise LF022JudgeDesignRebindError("v4 record count differs")
    for source in manifest.source_partitions:
        manifest_copy = output / source.source_manifest_artifact
        records_copy = output / source.source_records_artifact
        if (
            manifest_copy.is_symlink()
            or not manifest_copy.is_file()
            or hash_file(manifest_copy) != source.source_manifest_sha256
            or records_copy.is_symlink()
            or not records_copy.is_file()
            or hash_file(records_copy) != source.source_records_sha256
        ):
            raise LF022JudgeDesignRebindError(
                f"v3 source copy differs for partition {source.partition_id}"
            )
    summary_path = output / "summary.md"
    if summary_path.is_symlink() or not summary_path.is_file():
        raise LF022JudgeDesignRebindError("v4 summary is missing or unsafe")
    if summary_path.read_bytes() != _summary(manifest):
        raise LF022JudgeDesignRebindError("v4 summary does not replay")
    return JudgeDesignFreezeResult(
        records=tuple(records),
        manifest=manifest,
        output_dir=output,
        manifest_path=manifest_path,
    )


__all__ = [
    "JudgeDesignFreezeResult",
    "LF022JudgeDesignManifestV4",
    "LF022JudgeDesignRebindError",
    "LF022JudgeDesignRebindSpecV4",
    "LF022JudgeDesignRecordV4",
    "freeze_lf022_judge_design_v4",
    "verify_lf022_judge_design_v4",
]
