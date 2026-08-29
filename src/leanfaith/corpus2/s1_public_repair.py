"""Freeze and smoke-test the additive S1 public-data repair contract.

This module does not rerun Lean.  It binds the completed corpus-v1 and Meta
slice-2 artifacts, reconstructs the public corpus-v1 baseline, and proves that
one Meta candidate has an exact independently verified audit mate before it is
projected into the trainer and repair-provenance schemas.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.corpus2.build_v1 import (
    PROVENANCE_FIELDS,
    TRAINER_FIELDS,
    CorpusCandidate,
)
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.train2.trainer import TrainingRecord

METHOD_VERSION: Literal["s1_public_repair_contract_v1"] = "s1_public_repair_contract_v1"
META_SOURCE_KIND: Literal["meta_engine_slice2"] = "meta_engine_slice2"
META_SOURCE_REVISION: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = (
    "d568c8c09630de097a046763c17b9ea99f95f950"
)
PRODUCTION_CANDIDATE_INDEX = 0

_CORPUS_ROOT = Path("/storage/milikic/leanfaith/corpus2/v1_ed41471")
_META_ROOT = Path("/storage/milikic/leanfaith/meta_engine_slice2_6ace45e")
_BLOCKLIST_PATH = Path("/localhome/milikic/LeanFaith/data/benchmarks/golden_blocklist_v1.json")
_INPUT_NAMES = frozenset(
    {
        "corpus_manifest",
        "corpus_provenance",
        "corpus_train",
        "corpus_validation",
        "corpus_test",
        "golden_blocklist",
        "meta_manifest",
        "meta_summary",
        "meta_candidates",
        "meta_audits",
        "meta_declaration_names",
    }
)
_SPLIT_INPUT = {
    "train": "corpus_train",
    "validation": "corpus_validation",
    "test": "corpus_test",
}
_CORPUS_OUTPUT_INPUT = {
    "provenance_v1.jsonl": "corpus_provenance",
    "records_train_v1.jsonl": "corpus_train",
    "records_validation_v1.jsonl": "corpus_validation",
    "records_test_v1.jsonl": "corpus_test",
}
_META_OUTPUT_INPUT = {
    "summary.json": "meta_summary",
    "lean.stdout.jsonl": "meta_candidates",
    "audit.stdout.jsonl": "meta_audits",
    "declaration_names.txt": "meta_declaration_names",
}
_EVIDENCE_CLASS = {
    "P20": "P-DEF",
    "P21": "P-DEF",
    "P23": "P-SCHEMA",
    "P24": "P-SCHEMA",
}


class S1PublicRepairError(RuntimeError):
    """A frozen input, audit join, or smoke artifact failed closed."""


class FrozenInput(BaseModel):
    """One exact immutable input binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicBaselineCounts(BaseModel):
    """Expected public projection of corpus v1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(gt=0, strict=True)
    positive: int = Field(ge=0, strict=True)
    negative: int = Field(ge=0, strict=True)
    train: int = Field(ge=0, strict=True)
    validation: int = Field(ge=0, strict=True)
    test: int = Field(ge=0, strict=True)
    d3_rows: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.positive + self.negative != self.total:
            raise ValueError("public label counts must sum to total")
        if self.train + self.validation + self.test != self.total:
            raise ValueError("public split counts must sum to total")
        return self


class MetaPoolCounts(BaseModel):
    """Expected completed Meta-engine candidate/audit pool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: int = Field(gt=0, strict=True)
    audited: int = Field(gt=0, strict=True)
    selected_declarations: int = Field(gt=0, strict=True)
    successful_declarations: int = Field(gt=0, strict=True)
    family_counts: dict[str, int]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.candidates != self.audited:
            raise ValueError("every eligible Meta candidate must have an audit")
        if sum(self.family_counts.values()) != self.candidates:
            raise ValueError("Meta family counts must sum to candidate count")
        return self


class RepairCaps(BaseModel):
    """Frozen anti-shortcut ceilings inherited from PLAN.md/transform catalog v2."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family_percent: Literal[8] = 8
    mechanism_superclass_percent: Literal[15] = 15
    exact_template_percent: Literal[2] = 2
    neutral_wrapper_percent: Literal[1] = 1
    exact_rewrite_lemma_per_mille: Literal[5] = 5
    direct_per_source_ancestry: Literal[4] = 4
    composed_per_source_ancestry: Literal[4] = 4


class S1PublicRepairConfig(BaseModel):
    """Complete contract for the public-baseline and one-row Meta smoke."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_repair_contract_v1"] = METHOD_VERSION
    seed: int = Field(default=20260829, ge=0, strict=True)
    output_root: Path
    inputs: dict[str, FrozenInput]
    public_baseline: PublicBaselineCounts
    meta_pool: MetaPoolCounts
    caps: RepairCaps = Field(default_factory=RepairCaps)
    candidate_index: int = Field(default=PRODUCTION_CANDIDATE_INDEX, ge=0, strict=True)
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if set(self.inputs) != _INPUT_NAMES:
            raise ValueError("repair inputs must bind the exact frozen input set")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("repair artifacts must be under /storage/milikic")
        return self


class MetaCandidateRow(BaseModel):
    """Fields required to admit one already-validated Meta candidate."""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
    kind: Literal["candidate"]
    record_kind: Literal["candidate"] = Field(alias="recordKind")
    status: Literal["ok"]
    declaration: str = Field(min_length=1)
    family: Literal["P20", "P21", "P23", "P24"]
    evidence_class: Literal["P-DEF", "P-SCHEMA"] = Field(alias="evidenceClass")
    operation: str = Field(min_length=1)
    operation_kind: str = Field(alias="operationKind", min_length=1)
    site_path: str = Field(alias="sitePath", min_length=1)
    source: str = Field(min_length=1)
    source_pretty: str = Field(alias="sourcePretty", min_length=1)
    candidate: str = Field(min_length=1)
    candidate_pretty: str = Field(alias="candidatePretty", min_length=1)
    source_type_hash: str = Field(alias="sourceTypeHash", pattern=r"^[0-9a-f]{64}$")
    candidate_type_hash: str = Field(alias="candidateTypeHash", pattern=r"^[0-9a-f]{64}$")
    candidate_elaborates: StrictBool = Field(alias="candidateElaborates")
    whole_type_def_eq: StrictBool = Field(alias="wholeTypeDefEq")
    axioms: str = Field(min_length=1)
    evidence: dict[str, object]

    @model_validator(mode="after")
    def _valid_candidate(self) -> Self:
        if self.evidence_class != _EVIDENCE_CLASS[self.family]:
            raise ValueError("family/evidence class mismatch")
        if not self.site_path.startswith("/"):
            raise ValueError("site path must be absolute")
        if self.source != self.source_pretty or self.candidate != self.candidate_pretty:
            raise ValueError("pretty-text aliases differ")
        if self.source == self.candidate:
            raise ValueError("Meta candidate does not change the source")
        if sha256_hex(self.source.encode()) != self.source_type_hash:
            raise ValueError("source type hash differs")
        if sha256_hex(self.candidate.encode()) != self.candidate_type_hash:
            raise ValueError("candidate type hash differs")
        if not self.candidate_elaborates:
            raise ValueError("Meta candidate did not elaborate")
        if self.evidence_class == "P-DEF":
            if not self.whole_type_def_eq:
                raise ValueError("P-DEF candidate is not whole-type definitionally equal")
            if self.axioms != "none" or self.evidence.get("relation") != "definitionalEquality":
                raise ValueError("P-DEF evidence is malformed")
        elif self.axioms != "constructive":
            raise ValueError("P-SCHEMA evidence is malformed")
        return self


class MetaAuditRow(BaseModel):
    """Independent reconstruction result joined to a Meta candidate key."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
    kind: Literal["audit"]
    record_kind: Literal["audit"] = Field(alias="recordKind")
    declaration: str = Field(min_length=1)
    family: Literal["P20", "P21", "P23", "P24"]
    operation: str = Field(min_length=1)
    site_path: str = Field(alias="sitePath", min_length=1)
    expected_candidate_type_hash: str = Field(
        alias="expectedCandidateTypeHash", pattern=r"^[0-9a-f]{64}$"
    )
    actual_candidate_type_hash: str = Field(
        alias="actualCandidateTypeHash", pattern=r"^[0-9a-f]{64}$"
    )
    verified: StrictBool
    inverse_fold_verified: StrictBool = Field(alias="inverseFoldVerified")
    status: Literal["verified"]
    reason: Literal["verified"]
    audit_mode: Literal["independent-site-reconstruction"] = Field(alias="auditMode")

    @model_validator(mode="after")
    def _valid_audit(self) -> Self:
        if self.actual_candidate_type_hash != self.expected_candidate_type_hash:
            raise ValueError("audit reconstructed a different candidate hash")
        if not self.verified:
            raise ValueError("audit is not verified")
        if self.inverse_fold_verified != (self.family == "P20"):
            raise ValueError("inverse-fold audit result does not match family")
        return self


class MetaRepairProvenance(BaseModel):
    """Frozen provenance emitted by the one-row admission smoke."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_repair_contract_v1"] = METHOD_VERSION
    record_id: str = Field(min_length=1)
    source_kind: Literal["meta_engine_slice2"] = META_SOURCE_KIND
    source_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = META_SOURCE_REVISION
    declaration: str = Field(min_length=1)
    family: Literal["P20", "P21", "P23", "P24"]
    evidence_class: Literal["P-DEF", "P-SCHEMA"]
    operation: str = Field(min_length=1)
    site_path: str = Field(min_length=1)
    candidate_key: tuple[str, str, str, str, str]
    candidate_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_group_ids: tuple[str, ...]
    origin_id: str = Field(min_length=1)
    provenance_ids: tuple[str, ...]
    audit_verified: Literal[True] = True
    audit_mode: Literal["independent-site-reconstruction"]
    private_source_content: Literal[False] = False
    redistribution_allowed: Literal[True] = True
    external_transmission_allowed: Literal[False] = False
    release_eligible: Literal[True] = True
    meta_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    meta_candidates_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    meta_audits_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MetaAdmission:
    """One joined Meta candidate in downstream corpus/trainer forms."""

    corpus_candidate: CorpusCandidate
    trainer_record: TrainingRecord
    provenance: MetaRepairProvenance


def production_config(output_root: Path) -> S1PublicRepairConfig:
    """Return the exact contract bound to the completed queue artifacts."""

    inputs = {
        "corpus_manifest": FrozenInput(
            path=_CORPUS_ROOT / "corpus_v1_manifest.json",
            sha256="22386b7127c80fab6ce70df722ecc155ee3a3520971515ebefee6cb438a20a01",
        ),
        "corpus_provenance": FrozenInput(
            path=_CORPUS_ROOT / "provenance_v1.jsonl",
            sha256="cac85660e8803e151864b7f723fe6a06c4b578539f76db0ef1594607773ff979",
        ),
        "corpus_train": FrozenInput(
            path=_CORPUS_ROOT / "records_train_v1.jsonl",
            sha256="51ad67e42d5d350be0219ff26142e24ac1b7f8dfbfc652a1355430e46f5d6c4b",
        ),
        "corpus_validation": FrozenInput(
            path=_CORPUS_ROOT / "records_validation_v1.jsonl",
            sha256="a5939fee4df3363fec1c3285623ca18509c549fbf65e73f2ec9a741af5505470",
        ),
        "corpus_test": FrozenInput(
            path=_CORPUS_ROOT / "records_test_v1.jsonl",
            sha256="7424eb1afa8f6bbb28bbfebdc3bb16b082c2dbfe327b11e93fdf990ce220d917",
        ),
        "golden_blocklist": FrozenInput(
            path=_BLOCKLIST_PATH,
            sha256="8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7",
        ),
        "meta_manifest": FrozenInput(
            path=_META_ROOT / "manifest.json",
            sha256="9e2425f17a44fa2005d2856c290b2f551a19e46c8a10bf0ae9888875ab311fe0",
        ),
        "meta_summary": FrozenInput(
            path=_META_ROOT / "summary.json",
            sha256="497e5a17a7f8875ed2241aef4d4a26a84992073c07626766a458d61a93908093",
        ),
        "meta_candidates": FrozenInput(
            path=_META_ROOT / "lean.stdout.jsonl",
            sha256="61acf7436e03025a173249360915833dcbba7527d1b2504e10235445922a59f8",
        ),
        "meta_audits": FrozenInput(
            path=_META_ROOT / "audit.stdout.jsonl",
            sha256="ae77075722c1942e018a0402b8e8700d0d86e6ee6484dd35168cec81e92e6957",
        ),
        "meta_declaration_names": FrozenInput(
            path=_META_ROOT / "declaration_names.txt",
            sha256="1230b5bab24c2a55a4d3991f838aca8dab35adb75577c7eddd34d17b2f86f76c",
        ),
    }
    return S1PublicRepairConfig(
        output_root=output_root,
        inputs=inputs,
        public_baseline=PublicBaselineCounts(
            total=9585,
            positive=2351,
            negative=7234,
            train=7645,
            validation=942,
            test=998,
            d3_rows=146,
        ),
        meta_pool=MetaPoolCounts(
            candidates=16138,
            audited=16138,
            selected_declarations=500,
            successful_declarations=393,
            family_counts={"P20": 7813, "P21": 8078, "P23": 102, "P24": 145},
        ),
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1PublicRepairError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise S1PublicRepairError(f"expected a JSON object: {path}")
    return cast(dict[str, Any], value)


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise S1PublicRepairError(f"{path}:{line_number}: expected a JSON object")
                yield line_number, cast(dict[str, Any], value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1PublicRepairError(f"cannot read JSONL {path}: {exc}") from exc


def verify_input_bindings(config: S1PublicRepairConfig) -> None:
    """Verify every frozen file before opening structured content."""

    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise S1PublicRepairError(f"{name} must be a regular non-symlink file")
        actual = hash_file(binding.path)
        if actual != binding.sha256:
            raise S1PublicRepairError(
                f"{name} hash differs: expected {binding.sha256}, observed {actual}"
            )


def _require_manifest_output(
    manifest: Mapping[str, object],
    *,
    output_name: str,
    binding: FrozenInput,
) -> None:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise S1PublicRepairError("source manifest lacks an output inventory")
    output = outputs.get(output_name)
    if not isinstance(output, Mapping):
        raise S1PublicRepairError(f"source manifest lacks output {output_name}")
    if output.get("path") != str(binding.path) or output.get("sha256") != binding.sha256:
        raise S1PublicRepairError(f"source manifest binding differs for {output_name}")


def verify_public_baseline(config: S1PublicRepairConfig) -> dict[str, object]:
    """Replay the public projection and exact trainer/provenance join."""

    manifest = _read_json(config.inputs["corpus_manifest"].path)
    if manifest.get("status") != "completed" or manifest.get("schema_version") != 1:
        raise S1PublicRepairError("corpus-v1 manifest is not completed schema v1")
    for output_name, input_name in _CORPUS_OUTPUT_INPUT.items():
        _require_manifest_output(
            manifest,
            output_name=output_name,
            binding=config.inputs[input_name],
        )

    records: dict[str, tuple[str, TrainingRecord]] = {}
    for split, input_name in _SPLIT_INPUT.items():
        path = config.inputs[input_name].path
        for line_number, row in _iter_jsonl(path):
            if set(row) != TRAINER_FIELDS:
                raise S1PublicRepairError(f"{path}:{line_number}: trainer fields differ")
            try:
                record = TrainingRecord.model_validate(row)
            except ValidationError as exc:
                raise S1PublicRepairError(
                    f"{path}:{line_number}: invalid trainer record: {exc}"
                ) from exc
            if record.record_id in records:
                raise S1PublicRepairError(f"duplicate trainer record ID {record.record_id}")
            records[record.record_id] = (split, record)

    labels: Counter[bool] = Counter()
    splits: Counter[str] = Counter()
    public_ids: set[str] = set()
    d3_rows = 0
    path = config.inputs["corpus_provenance"].path
    for line_number, row in _iter_jsonl(path):
        if set(row) != PROVENANCE_FIELDS:
            raise S1PublicRepairError(f"{path}:{line_number}: provenance fields differ")
        if row.get("private_source_content") is True:
            continue
        if any(
            row.get(name) is not True
            for name in (
                "redistribution_allowed",
                "external_transmission_allowed",
                "release_eligible",
            )
        ):
            raise S1PublicRepairError(f"{path}:{line_number}: public release policy differs")
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or record_id in public_ids:
            raise S1PublicRepairError(f"{path}:{line_number}: invalid public record identity")
        public_ids.add(record_id)
        joined = records.get(record_id)
        if joined is None:
            raise S1PublicRepairError(f"{path}:{line_number}: public trainer join is missing")
        split, record = joined
        if row.get("split") != split or row.get("label") is not record.label:
            raise S1PublicRepairError(f"{path}:{line_number}: trainer/provenance join differs")
        if row.get("group_key") != record.group_key:
            raise S1PublicRepairError(f"{path}:{line_number}: group key differs")
        source_kinds = row.get("source_kinds")
        if not isinstance(source_kinds, list) or not all(
            isinstance(value, str) for value in source_kinds
        ):
            raise S1PublicRepairError(f"{path}:{line_number}: source kinds are invalid")
        if "d3_codex_scale_v1" in source_kinds:
            d3_rows += 1
        labels[record.label] += 1
        splits[split] += 1

    observed = PublicBaselineCounts(
        total=len(public_ids),
        positive=labels[True],
        negative=labels[False],
        train=splits["train"],
        validation=splits["validation"],
        test=splits["test"],
        d3_rows=d3_rows,
    )
    if observed != config.public_baseline:
        raise S1PublicRepairError(
            "public corpus-v1 projection differs: "
            f"expected {config.public_baseline.model_dump()}, observed {observed.model_dump()}"
        )
    return observed.model_dump(mode="json")


def verify_meta_pool(config: S1PublicRepairConfig) -> dict[str, object]:
    """Verify the completed public Meta run's root manifest and count contract."""

    manifest = _read_json(config.inputs["meta_manifest"].path)
    if (
        manifest.get("status") != "completed"
        or manifest.get("schema_version") != 2
        or manifest.get("method_version") != "meta_engine_slice2_yield_probe_v4"
    ):
        raise S1PublicRepairError("Meta manifest is not the completed slice-2 v4 run")
    expected_privacy = {
        "public_only": True,
        "private_source_content": False,
        "external_transmission": False,
    }
    if manifest.get("privacy") != expected_privacy:
        raise S1PublicRepairError("Meta manifest is not public-only")
    meta_config = manifest.get("config")
    if not isinstance(meta_config, Mapping) or any(
        meta_config.get(key) != value for key, value in expected_privacy.items()
    ):
        raise S1PublicRepairError("Meta run config privacy differs")
    for output_name, input_name in _META_OUTPUT_INPUT.items():
        _require_manifest_output(
            manifest,
            output_name=output_name,
            binding=config.inputs[input_name],
        )
    summary = _read_json(config.inputs["meta_summary"].path)
    if manifest.get("summary") != summary:
        raise S1PublicRepairError("Meta summary differs from its completed manifest")
    audit = summary.get("independent_audit")
    family_counts = summary.get("per_family_counts")
    observed = MetaPoolCounts(
        candidates=cast(int, summary.get("total_candidate_count")),
        audited=cast(int, audit.get("verified_count") if isinstance(audit, Mapping) else None),
        selected_declarations=cast(int, summary.get("selected_declaration_count")),
        successful_declarations=cast(int, summary.get("successful_declaration_count")),
        family_counts=cast(dict[str, int], family_counts),
    )
    if observed != config.meta_pool:
        raise S1PublicRepairError(
            "Meta pool counts differ: "
            f"expected {config.meta_pool.model_dump()}, observed {observed.model_dump()}"
        )
    selection = manifest.get("selection")
    if (
        not isinstance(selection, Mapping)
        or selection.get("selected_names_sha256") != config.inputs["meta_declaration_names"].sha256
    ):
        raise S1PublicRepairError("Meta declaration selection binding differs")
    return observed.model_dump(mode="json")


def _candidate_key(row: MetaCandidateRow) -> tuple[str, str, str, str, str]:
    return (
        row.declaration,
        row.family,
        row.operation,
        row.site_path,
        row.candidate_type_hash,
    )


def _audit_key(row: MetaAuditRow) -> tuple[str, str, str, str, str]:
    return (
        row.declaration,
        row.family,
        row.operation,
        row.site_path,
        row.expected_candidate_type_hash,
    )


def _select_candidate(path: Path, index: int) -> MetaCandidateRow:
    candidate_number = 0
    for line_number, row in _iter_jsonl(path):
        if row.get("recordKind") != "candidate":
            continue
        if candidate_number != index:
            candidate_number += 1
            continue
        try:
            return MetaCandidateRow.model_validate(row)
        except ValidationError as exc:
            raise S1PublicRepairError(
                f"{path}:{line_number}: selected Meta candidate is invalid: {exc}"
            ) from exc
    raise S1PublicRepairError(f"Meta candidate index {index} is out of range")


def _join_audit(path: Path, key: tuple[str, str, str, str, str]) -> MetaAuditRow:
    matched: MetaAuditRow | None = None
    for line_number, row in _iter_jsonl(path):
        try:
            audit = MetaAuditRow.model_validate(row)
        except ValidationError as exc:
            raise S1PublicRepairError(f"{path}:{line_number}: invalid Meta audit: {exc}") from exc
        if _audit_key(audit) != key:
            continue
        if matched is not None:
            raise S1PublicRepairError("selected Meta candidate has duplicate audit rows")
        matched = audit
    if matched is None:
        raise S1PublicRepairError("selected Meta candidate lacks an exact verified audit row")
    return matched


def convert_one_verified_meta(config: S1PublicRepairConfig) -> MetaAdmission:
    """Verify both source contracts and project one exactly audited Meta pair."""

    verify_input_bindings(config)
    verify_public_baseline(config)
    verify_meta_pool(config)
    candidate = _select_candidate(config.inputs["meta_candidates"].path, config.candidate_index)
    key = _candidate_key(candidate)
    audit = _join_audit(config.inputs["meta_audits"].path, key)

    blocklist = GoldenBlocklist.load(config.inputs["golden_blocklist"].path)
    reference_near_hash = signature_near_dup_hash(candidate.source)
    candidate_near_hash = signature_near_dup_hash(candidate.candidate)
    if reference_near_hash in blocklist.near_dup_hashes:
        raise S1PublicRepairError("selected Meta source collides with the golden blocklist")
    if candidate_near_hash in blocklist.near_dup_hashes:
        raise S1PublicRepairError("selected Meta candidate collides with the golden blocklist")
    if reference_near_hash == candidate_near_hash:
        raise S1PublicRepairError("selected Meta pair is near-signature degenerate")

    key_payload = list(key)
    key_digest = hash_canonical(
        {"schema": "s1_public_repair_meta_key_v1", "candidate_key": key_payload}
    )
    ancestry_id = "mathlib-declaration:" + hash_canonical(
        {
            "schema": "mathlib_declaration_ancestry_v1",
            "revision": META_SOURCE_REVISION,
            "declaration": candidate.declaration,
        }
    )
    origin_id = "meta_slice2_candidate:" + key_digest
    candidate_provenance_id = "meta_slice2_primary:" + hash_canonical(
        candidate.model_dump(mode="json", by_alias=True)
    )
    audit_provenance_id = "meta_slice2_audit:" + hash_canonical(
        audit.model_dump(mode="json", by_alias=True)
    )
    record_id = "s1_public_repair:" + hash_canonical(
        {
            "schema": "s1_public_repair_trainer_projection_v1",
            "candidate_key": key_payload,
            "meta_manifest_sha256": config.inputs["meta_manifest"].sha256,
        }
    )
    corpus_candidate = CorpusCandidate(
        origin_id=origin_id,
        source_kind=META_SOURCE_KIND,
        reference_headless=candidate.source,
        candidate_headless=candidate.candidate,
        label=True,
        split_group_ids=(ancestry_id,),
        family_ids=(candidate.family,),
        provenance_ids=(candidate_provenance_id, audit_provenance_id),
        split_anchor=None,
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=False,
        release_eligible=True,
    )
    trainer_record = TrainingRecord(
        record_id=record_id,
        reference_headless=candidate.source,
        candidate_headless=candidate.candidate,
        label=True,
        group_key=ancestry_id,
        family=candidate.family,
        source=META_SOURCE_KIND,
        weight=1.0,
    )
    provenance = MetaRepairProvenance(
        record_id=record_id,
        declaration=candidate.declaration,
        family=candidate.family,
        evidence_class=candidate.evidence_class,
        operation=candidate.operation,
        site_path=candidate.site_path,
        candidate_key=key,
        candidate_key_sha256=key_digest,
        reference_sha256=candidate.source_type_hash,
        candidate_sha256=candidate.candidate_type_hash,
        split_group_ids=(ancestry_id,),
        origin_id=origin_id,
        provenance_ids=(candidate_provenance_id, audit_provenance_id),
        audit_mode=audit.audit_mode,
        meta_manifest_sha256=config.inputs["meta_manifest"].sha256,
        meta_candidates_sha256=config.inputs["meta_candidates"].sha256,
        meta_audits_sha256=config.inputs["meta_audits"].sha256,
    )
    return MetaAdmission(
        corpus_candidate=corpus_candidate,
        trainer_record=trainer_record,
        provenance=provenance,
    )


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _smoke_payloads(config: S1PublicRepairConfig) -> dict[str, bytes]:
    admission = convert_one_verified_meta(config)
    trainer = admission.trainer_record.model_dump(mode="json")
    if set(trainer) != TRAINER_FIELDS:
        raise S1PublicRepairError("smoke trainer projection fields differ")
    provenance = admission.provenance.model_dump(mode="json")
    payloads = {
        "trainer_record.jsonl": _canonical_line(trainer),
        "provenance.jsonl": _canonical_line(provenance),
    }
    output_bindings = {
        name: {
            "path": str(config.output_root / name),
            "sha256": sha256_hex(payload),
        }
        for name, payload in sorted(payloads.items())
    }
    manifest = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "completed",
        "candidate_index": config.candidate_index,
        "candidate_key": list(admission.provenance.candidate_key),
        "candidate_key_sha256": admission.provenance.candidate_key_sha256,
        "config_sha256": hash_canonical(config.model_dump(mode="json")),
        "implementation_module_sha256": hash_file(Path(__file__)),
        "inputs": {
            name: {"path": str(binding.path), "sha256": binding.sha256}
            for name, binding in sorted(config.inputs.items())
        },
        "public_baseline": config.public_baseline.model_dump(mode="json"),
        "meta_pool": config.meta_pool.model_dump(mode="json"),
        "caps": config.caps.model_dump(mode="json"),
        "outputs": output_bindings,
        "privacy": {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        },
        "execution": {
            "lean_reexecution": False,
            "external_calls": False,
            "final_test_accessed": False,
        },
        "counts": {"trainer_records": 1, "provenance_records": 1, "verified_audits": 1},
    }
    payloads["manifest.json"] = _canonical_line(manifest)
    return payloads


def verify_smoke(config: S1PublicRepairConfig) -> dict[str, Any]:
    """Replay the one-row conversion and require byte-identical smoke outputs."""

    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise S1PublicRepairError("smoke output root must be a non-symlink directory")
    expected = _smoke_payloads(config)
    observed_names = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed_names != set(expected):
        raise S1PublicRepairError("smoke output file set differs")
    for name, payload in expected.items():
        path = config.output_root / name
        if path.is_symlink() or path.read_bytes() != payload:
            raise S1PublicRepairError(f"smoke output differs: {name}")
    return _read_json(config.output_root / "manifest.json")


def materialize_smoke(config: S1PublicRepairConfig) -> dict[str, Any]:
    """Atomically emit or idempotently verify the one-row conversion smoke."""

    if config.output_root.exists():
        return verify_smoke(config)
    payloads = _smoke_payloads(config)
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{config.output_root.name}.",
            suffix=".partial",
            dir=config.output_root.parent,
        )
    )
    try:
        for name, payload in payloads.items():
            path = staging / name
            path.write_bytes(payload)
            os.chmod(path, 0o600)
        os.replace(staging, config.output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verify_smoke(config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run-smoke", "verify-smoke"))
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = production_config(cast(Path, args.output_root))
    manifest = materialize_smoke(config) if args.command == "run-smoke" else verify_smoke(config)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
