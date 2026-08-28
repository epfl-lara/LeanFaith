"""Build the frozen corpus-v1 trainer partitions from the four Track-D inputs.

The builder deliberately keeps the eight-field trainer rows small.  Ancestry,
privacy, deduplication, and family-membership provenance live in separate,
content-addressed files and are replayed by :func:`verify_corpus_v1`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.corpus2.judge_recovered import RecoveredJudgment, RecoveredPlanRow
from leanfaith.datasets.experimental_mixed_supervision import ExperimentalMixedSupervisionRecord
from leanfaith.eval.m1_runtime import pack_pair
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.train2.trainer import TrainingRecord
from leanfaith.transforms.composition_third_hop import (
    DeterministicCompositionThirdHopPairRecord,
)

METHOD_VERSION = "corpus_v1_track_d_merge_v1"
DEFAULT_SEED = 20260828
MAX_TOKENS = 1024
TRAINER_FIELDS = frozenset(
    {
        "record_id",
        "reference_headless",
        "candidate_headless",
        "label",
        "group_key",
        "family",
        "source",
        "weight",
    }
)
PROVENANCE_FIELDS = frozenset(
    {
        "schema_version",
        "record_id",
        "pair_id",
        "pair_key",
        "reference_sha256",
        "candidate_sha256",
        "label",
        "group_key",
        "split",
        "split_group_ids",
        "component_group_ids",
        "component_statement_near_hashes",
        "family_ids",
        "origin_ids",
        "source_kinds",
        "provenance_ids",
        "forward_tokens",
        "reverse_tokens",
        "private_source_content",
        "redistribution_allowed",
        "external_transmission_allowed",
        "release_eligible",
    }
)
EXCLUSION_REASONS = frozenset(
    {
        "golden_blocklist",
        "degenerate_near_identical_sides",
        "overlength",
        "conflicting_labels",
        "split_anchor_component_conflict",
        "family_cap",
    }
)
SPLITS: tuple[Literal["train", "validation", "test"], ...] = (
    "train",
    "validation",
    "test",
)


class CorpusV1Error(RuntimeError):
    """A frozen input or corpus-v1 invariant failed closed."""


class _Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...


class FrozenFile(BaseModel):
    """One exact input file binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CorpusV1Config(BaseModel):
    """Complete reproducible corpus-v1 build configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["corpus_v1_track_d_merge_v1"] = "corpus_v1_track_d_merge_v1"
    seed: int = Field(default=DEFAULT_SEED, ge=0, strict=True)
    max_tokens: Literal[1024] = 1024
    family_cap_numerator: Literal[1] = 1
    family_cap_denominator: Literal[10] = 10
    train_percent: Literal[80] = 80
    validation_percent: Literal[10] = 10
    test_percent: Literal[10] = 10
    output_root: Path
    tokenizer_dir: Path
    tokenizer_files: dict[str, FrozenFile]
    inputs: dict[str, FrozenFile]
    enforce_storage_root: bool = True
    canary_epochs: int = Field(default=6, ge=1, strict=True)
    canary_learning_rate: float = Field(default=0.15, gt=0.0)
    canary_target_balanced_accuracy: float = Field(default=0.80, gt=0.5, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.train_percent + self.validation_percent + self.test_percent != 100:
            raise ValueError("split percentages must sum to 100")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("corpus-v1 artifacts must be under /storage/milikic")
        required_tokenizer = {
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        }
        if self.enforce_storage_root and set(self.tokenizer_files) != required_tokenizer:
            raise ValueError("tokenizer_files must bind the exact three tokenizer files")
        if self.enforce_storage_root and set(self.inputs) != _PRODUCTION_INPUT_NAMES:
            raise ValueError("inputs must bind exactly the frozen production input set")
        return self


@dataclass(frozen=True, slots=True)
class CorpusCandidate:
    """One source-specific pair before screening and cross-source deduplication."""

    origin_id: str
    source_kind: str
    reference_headless: str
    candidate_headless: str
    label: bool
    split_group_ids: tuple[str, ...]
    family_ids: tuple[str, ...]
    provenance_ids: tuple[str, ...]
    split_anchor: Literal["train", "validation", "test"] | None
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool


@dataclass(frozen=True, slots=True)
class ScreenedCandidate:
    candidate: CorpusCandidate
    reference_near_hash: str
    candidate_near_hash: str
    pair_key: tuple[str, str]
    forward_tokens: int
    reverse_tokens: int


@dataclass(frozen=True, slots=True)
class MergedPair:
    pair_id: str
    pair_key: tuple[str, str]
    reference_headless: str
    candidate_headless: str
    label: bool
    split_group_ids: tuple[str, ...]
    family_ids: tuple[str, ...]
    origin_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]
    provenance_ids: tuple[str, ...]
    split_anchors: tuple[str, ...]
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    forward_tokens: int
    reverse_tokens: int


@dataclass(frozen=True, slots=True)
class ComponentSeed:
    """One deduplicated pair's text-free contribution to the lineage graph."""

    pair_id: str
    pair_key: tuple[str, str]
    split_group_ids: tuple[str, ...]
    statement_near_hashes: tuple[str, ...]
    split_anchors: tuple[str, ...]
    origin_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LineageCluster:
    """One pre-cap connected component before split assignment."""

    component_id: str
    pair_ids: tuple[str, ...]
    split_group_ids: tuple[str, ...]
    statement_near_hashes: tuple[str, ...]
    split_anchors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Component:
    component_id: str
    split_group_ids: tuple[str, ...]
    statement_near_hashes: tuple[str, ...]
    split: Literal["train", "validation", "test"]
    split_anchors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalRow:
    trainer: TrainingRecord
    provenance: dict[str, Any]
    split: Literal["train", "validation", "test"]


_PRODUCTION_INPUT_NAMES = frozenset(
    {
        "blocklist",
        "v0_manifest",
        "v0_train",
        "v0_validation",
        "v0_test",
        "mixed_records",
        "depth_manifest",
        "depth_pairs",
        "depth_representations",
        "depth_theorems",
        "public_representations",
        "public_theorems",
        "private_representations",
        "private_theorems",
        "d3_manifest",
        "d3_job_plan",
        "d3_records",
        "d3_checks",
        "d3_trainer",
        "recovered_manifest",
        "recovered_plan",
        "recovered_judgments",
        "recovered_trainer",
    }
)


def _binding(path: str, digest: str) -> FrozenFile:
    return FrozenFile(path=Path(path), sha256=digest)


def production_config(output_root: Path) -> CorpusV1Config:
    """Return the exact frozen queue-4 production configuration."""

    tokenizer_dir = Path("/storage/milikic/leanfaith/cpt/modernbert_lean_v1_run1")
    v0 = "/storage/milikic/leanfaith/corpus2/v0_from_mixed"
    mixed = (
        "/storage/milikic/leanfaith/experimental_mixed_supervision/"
        "firsthop_kimi_qwen1125_composition_f7b398af_v1"
    )
    depth = (
        "/storage/milikic/leanfaith/deterministic_v2/composition_third_hop_audits/"
        "frontier_084859ee_five_families_v2"
    )
    d3 = "/storage/milikic/leanfaith/lf023_llm_transforms/codex_scale_v1_f88931b"
    recovered = "/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba"
    return CorpusV1Config(
        output_root=output_root,
        tokenizer_dir=tokenizer_dir,
        tokenizer_files={
            "tokenizer.json": _binding(
                str(tokenizer_dir / "tokenizer.json"),
                "c7a995f78d60cc3c253902f4b5becfe2f9d0b44f78e6e2f81a343a0cb71789e6",
            ),
            "tokenizer_config.json": _binding(
                str(tokenizer_dir / "tokenizer_config.json"),
                "2966a59b9e9cf122279aec1249e22e5bc7ad8430c754e95031b13fd128d4e560",
            ),
            "special_tokens_map.json": _binding(
                str(tokenizer_dir / "special_tokens_map.json"),
                "ea97ecdbcc73713039d8d64dbb05e3689495c96657fbd9a18f5bed381be81049",
            ),
        },
        inputs={
            "blocklist": _binding(
                str(
                    Path(__file__).resolve().parents[3] / "data/benchmarks/golden_blocklist_v1.json"
                ),
                "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7",
            ),
            "v0_manifest": _binding(
                f"{v0}/corpus_v0_manifest.json",
                "8e7c010af90fd2b8a82df1710f296f7c6b6041979e8edc8ec5927b76b33ca03b",
            ),
            "v0_train": _binding(
                f"{v0}/records_train_v0.jsonl",
                "3f3a99fbd0d2bbb3feafe6ab0256dffb9f70099c995c8198146e1fdc469fa291",
            ),
            "v0_validation": _binding(
                f"{v0}/records_validation_v0.jsonl",
                "3b30ef4ecb40ad2e123113be3a0f99d6631f6929606d15db54410c923c697144",
            ),
            "v0_test": _binding(
                f"{v0}/records_test_v0.jsonl",
                "74b0af0aa6f0729be1214bfc568366c58529da2e303590c638809043a701de37",
            ),
            "mixed_records": _binding(
                f"{mixed}/records.jsonl",
                "cbb113c85c7fea00e0a53877d5f0a586db1c5399ea4107c0050c4ad443caccd1",
            ),
            "depth_manifest": _binding(
                f"{depth}/manifest.json",
                "7a0a07f1abdbf28c74a5fd07aa7fb392e18e8ffcc47d40f530398f659ec51fc7",
            ),
            "depth_pairs": _binding(
                f"{depth}/unique_pairs.jsonl",
                "1156d72c2077f210099e61e00dd18803a99922398a5ca49577be2db3856fd37c",
            ),
            "depth_representations": _binding(
                f"{depth}/representations.jsonl",
                "1638e00e8fe424c0773c25d04b5e386a4f4ab0f29acb2fa70f55b2abb1dae545",
            ),
            "depth_theorems": _binding(
                f"{depth}/theorems.jsonl",
                "a016de724eed23862bce1d78a2fb152946ae97c35ca28caacecbd14aa82a151d",
            ),
            "public_representations": _binding(
                "/storage/milikic/leanfaith/scale_dc29fe6d4038/public_mathlib_repr_v3/run_a/records/mathlib.jsonl",
                "c799f54c60d3eb3f45a0fa473231ba991e871b7de440c65b037436721037e505",
            ),
            "public_theorems": _binding(
                "/storage/milikic/leanfaith/immutable/extractions/mathlib_d568c8c_manifest_b1831204/theorems/mathlib.jsonl",
                "7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7",
            ),
            "private_representations": _binding(
                "/storage/milikic/leanfaith/gate3/frozen/source_subsets/sft_classic_v1/representations.jsonl",
                "c63bf8e2706d4fc3fff430bee920cb0c575b2947023a4141a4d0384f747cad24",
            ),
            "private_theorems": _binding(
                "/storage/milikic/leanfaith/gate3/frozen/source_subsets/sft_classic_v1/theorems.jsonl",
                "3241ea0ff7f7e80a27ea6deafe680043c8ac8e782db049dcc551c50441115c30",
            ),
            "d3_manifest": _binding(
                f"{d3}/run_manifest.json",
                "4e1dd75ff2c3f6eaec88b73fbd81a7589dcc12398feccf97435c824a3f512075",
            ),
            "d3_job_plan": _binding(
                f"{d3}/job_plan.jsonl",
                "26770ee4ec163ea1d9bf6a8e2e3f0bfe84ad04615015c4751669967abb477e39",
            ),
            "d3_records": _binding(
                f"{d3}/records.jsonl",
                "8503d6307374fb58643c0bbdd382338761332321aa587e0d99590c8862305a74",
            ),
            "d3_checks": _binding(
                f"{d3}/lean_checks.jsonl",
                "a8a34cd67f8a55b2df3e73ab1796eea51e8edd099a14e5746f0b7b886aa14f23",
            ),
            "d3_trainer": _binding(
                f"{d3}/trainer_records.jsonl",
                "95ba0a0ab5d18f560dfa6beeb1b012bbf74c8fdb6d95a3cb99e8179d4e54a532",
            ),
            "recovered_manifest": _binding(
                f"{recovered}/final_manifest.json",
                "19a9d814823245f300c9c386514c9f4281322b0939d51a23ab13228df9cc0d1b",
            ),
            "recovered_plan": _binding(
                f"{recovered}/inputs/pair_plan.jsonl",
                "1746aa6b95476712f858db196138f5f18a938126b90e7de18881fb5c72056fe4",
            ),
            "recovered_judgments": _binding(
                f"{recovered}/outputs/judgments.jsonl",
                "2a6ef8c170a20e38047b3fbe6d1b842fb51abb0d0049552aa3f4bfac57b06025",
            ),
            "recovered_trainer": _binding(
                f"{recovered}/outputs/trainer_records.jsonl",
                "5de1f904904da6fa204a446e65c58d137a59a6a21d5afa15eb1ad24dbf3bf2f1",
            ),
        },
    )


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _sha_text(value: str) -> str:
    return sha256_hex(value.encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusV1Error(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusV1Error(f"expected a JSON object: {path}")
    return cast(dict[str, Any], value)


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise CorpusV1Error(f"{path}:{line_number}: expected JSON object")
                yield line_number, cast(dict[str, Any], value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusV1Error(f"cannot read JSONL {path}: {exc}") from exc


def _verify_binding(binding: FrozenFile) -> None:
    if not binding.path.is_file():
        raise CorpusV1Error(f"missing frozen input: {binding.path}")
    observed = hash_file(binding.path)
    if observed != binding.sha256:
        raise CorpusV1Error(
            f"frozen input hash mismatch for {binding.path}: {observed} != {binding.sha256}"
        )


def verify_input_bindings(config: CorpusV1Config) -> None:
    """Rehash every configured source and tokenizer artifact."""

    for name in sorted(config.inputs):
        _verify_binding(config.inputs[name])
    for name in sorted(config.tokenizer_files):
        binding = config.tokenizer_files[name]
        if binding.path != config.tokenizer_dir / name:
            raise CorpusV1Error(f"tokenizer binding path mismatch: {name}")
        _verify_binding(binding)


def _validate_candidate(row: CorpusCandidate) -> CorpusCandidate:
    for name, value in (
        ("origin_id", row.origin_id),
        ("source_kind", row.source_kind),
        ("reference_headless", row.reference_headless),
        ("candidate_headless", row.candidate_headless),
    ):
        if not value.strip() or "\x00" in value:
            raise CorpusV1Error(f"candidate {name} must be safe nonempty text")
    if row.split_group_ids != tuple(sorted(set(row.split_group_ids))) or not row.split_group_ids:
        raise CorpusV1Error("candidate split_group_ids must be nonempty, sorted, and unique")
    if row.family_ids != tuple(sorted(set(row.family_ids))) or not row.family_ids:
        raise CorpusV1Error("candidate family_ids must be nonempty, sorted, and unique")
    if row.provenance_ids != tuple(sorted(set(row.provenance_ids))) or not row.provenance_ids:
        raise CorpusV1Error("candidate provenance_ids must be nonempty, sorted, and unique")
    if row.private_source_content and row.external_transmission_allowed:
        raise CorpusV1Error("private candidate cannot be externally transmissible")
    if row.release_eligible and (row.private_source_content or not row.redistribution_allowed):
        raise CorpusV1Error("candidate release policy is incoherent")
    return row


def _load_training_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, payload in _iter_jsonl(path):
        if set(payload) != TRAINER_FIELDS:
            raise CorpusV1Error(
                f"{path}:{line_number}: trainer row fields differ from the frozen schema"
            )
        try:
            record = TrainingRecord.model_validate(payload)
        except ValueError as exc:
            raise CorpusV1Error(f"{path}:{line_number}: invalid trainer row: {exc}") from exc
        if record.record_id in seen:
            raise CorpusV1Error(f"{path}:{line_number}: duplicate record_id {record.record_id}")
        seen.add(record.record_id)
        rows.append(record.model_dump(mode="json"))
    return rows


def _load_v0_candidates(config: CorpusV1Config) -> list[CorpusCandidate]:
    manifest = _read_json(config.inputs["v0_manifest"].path)
    if manifest.get("command") != "corpus2_from_mixed_v0":
        raise CorpusV1Error("v0 manifest has the wrong command")
    mixed_binding = cast(Mapping[str, Any], manifest.get("mixed_corpus"))
    block_binding = cast(Mapping[str, Any], manifest.get("blocklist"))
    if mixed_binding.get("sha256") != config.inputs["mixed_records"].sha256:
        raise CorpusV1Error("v0 manifest does not bind the configured mixed corpus")
    if block_binding.get("sha256") != config.inputs["blocklist"].sha256:
        raise CorpusV1Error("v0 manifest does not bind the configured golden blocklist")
    outputs = cast(Mapping[str, Any], manifest.get("outputs"))
    for split in SPLITS:
        bound = cast(Mapping[str, Any], outputs.get(split))
        if bound.get("sha256") != config.inputs[f"v0_{split}"].sha256:
            raise CorpusV1Error(f"v0 manifest output binding differs for {split}")

    mixed: dict[str, ExperimentalMixedSupervisionRecord] = {}
    for line_number, payload in _iter_jsonl(config.inputs["mixed_records"].path):
        try:
            row = ExperimentalMixedSupervisionRecord.model_validate(payload)
        except ValueError as exc:
            raise CorpusV1Error(
                f"mixed records line {line_number} fails its strict schema: {exc}"
            ) from exc
        if row.record_id in mixed:
            raise CorpusV1Error(f"mixed corpus repeats {row.record_id}")
        mixed[row.record_id] = row

    candidates: list[CorpusCandidate] = []
    seen: set[str] = set()
    for split in SPLITS:
        rows = _load_training_rows(config.inputs[f"v0_{split}"].path)
        for payload in rows:
            record_id = cast(str, payload["record_id"])
            if record_id in seen:
                raise CorpusV1Error(f"v0 outputs repeat {record_id}")
            seen.add(record_id)
            raw = mixed.get(record_id)
            if raw is None:
                raise CorpusV1Error(f"v0 row lacks an exact mixed-corpus join: {record_id}")
            expected = {
                "reference_headless": raw.source.headless,
                "candidate_headless": raw.candidate.headless,
                "label": raw.pseudo_target == "same_claim",
                "group_key": raw.split_component_id,
                "family": "+".join(raw.family_ids) or None,
                "source": raw.pseudo_target_basis,
                "weight": None,
            }
            if any(payload[name] != value for name, value in expected.items()):
                raise CorpusV1Error(f"v0 row is not the exact frozen projection: {record_id}")
            if raw.split != split:
                raise CorpusV1Error(f"v0 row is stored in the wrong split file: {record_id}")
            signal_ids = tuple(signal.signal_id for signal in raw.signals)
            candidates.append(
                _validate_candidate(
                    CorpusCandidate(
                        origin_id=record_id,
                        source_kind="v0_mixed_proxy",
                        reference_headless=raw.source.headless,
                        candidate_headless=raw.candidate.headless,
                        label=raw.pseudo_target == "same_claim",
                        split_group_ids=raw.split_group_ids,
                        family_ids=raw.family_ids,
                        provenance_ids=tuple(sorted({record_id, *signal_ids})),
                        split_anchor=split,
                        private_source_content=raw.private_source_content,
                        redistribution_allowed=raw.redistribution_allowed,
                        external_transmission_allowed=raw.external_transmission_allowed,
                        release_eligible=raw.release_eligible,
                    )
                )
            )
    counts = cast(Mapping[str, Any], manifest.get("counts"))
    if any(
        int(counts.get(split, -1)) != sum(row.split_anchor == split for row in candidates)
        for split in SPLITS
    ):
        raise CorpusV1Error("v0 manifest counts do not reconcile with its rows")
    return candidates


def _unwrap_record(payload: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in payload:
        return payload
    value = payload[key]
    if not isinstance(value, dict):
        raise CorpusV1Error(f"enveloped {key} record is not an object")
    return cast(dict[str, Any], value)


def _load_representations(
    path: Path,
    needed: set[str],
    *,
    envelope_key: str | None = None,
) -> dict[str, RepresentationRecord]:
    output: dict[str, RepresentationRecord] = {}
    for line_number, payload in _iter_jsonl(path):
        raw = _unwrap_record(payload, envelope_key) if envelope_key is not None else payload
        record_id = raw.get("representation_id")
        if record_id not in needed:
            continue
        try:
            row = RepresentationRecord.model_validate(raw)
        except ValueError as exc:
            raise CorpusV1Error(f"{path}:{line_number}: invalid representation: {exc}") from exc
        if row.representation_id in output:
            raise CorpusV1Error(f"representation input repeats {row.representation_id}")
        output[row.representation_id] = row
    return output


def _load_theorems(
    path: Path,
    needed: set[str],
    *,
    envelope_key: str | None = None,
) -> dict[str, TheoremRecord]:
    output: dict[str, TheoremRecord] = {}
    for line_number, payload in _iter_jsonl(path):
        raw = _unwrap_record(payload, envelope_key) if envelope_key is not None else payload
        record_id = raw.get("theorem_id")
        if record_id not in needed:
            continue
        try:
            row = TheoremRecord.model_validate(raw)
        except ValueError as exc:
            raise CorpusV1Error(f"{path}:{line_number}: invalid theorem: {exc}") from exc
        if row.theorem_id in output:
            raise CorpusV1Error(f"theorem input repeats {row.theorem_id}")
        output[row.theorem_id] = row
    return output


def _merge_disjoint[T](left: Mapping[str, T], right: Mapping[str, T], *, kind: str) -> dict[str, T]:
    overlap = set(left) & set(right)
    if overlap:
        raise CorpusV1Error(f"{kind} occurs in both public and private inputs: {min(overlap)}")
    return {**left, **right}


def _load_depth_candidates(config: CorpusV1Config) -> list[CorpusCandidate]:
    manifest = _read_json(config.inputs["depth_manifest"].path)
    expected_hashes = {
        "unique_output_sha256": config.inputs["depth_pairs"].sha256,
        "representation_output_sha256": config.inputs["depth_representations"].sha256,
        "theorem_output_sha256": config.inputs["depth_theorems"].sha256,
    }
    if manifest.get("method_version") != "deterministic_v2_composition_third_hop_v2" or any(
        manifest.get(name) != digest for name, digest in expected_hashes.items()
    ):
        raise CorpusV1Error("depth-3 manifest does not bind its configured outputs")

    pairs: list[DeterministicCompositionThirdHopPairRecord] = []
    for line_number, payload in _iter_jsonl(config.inputs["depth_pairs"].path):
        try:
            pairs.append(DeterministicCompositionThirdHopPairRecord.model_validate(payload))
        except ValueError as exc:
            raise CorpusV1Error(f"depth pair line {line_number} is invalid: {exc}") from exc
    if len(pairs) != manifest.get("unique_pair_count"):
        raise CorpusV1Error("depth-3 pair count differs from its manifest")
    original_rep_ids = {row.original_source_representation_id for row in pairs}
    original_theorem_ids = {row.original_source_theorem_id for row in pairs}
    final_rep_ids = {row.selected_final_representation_id for row in pairs}
    final_theorem_ids = {row.selected_final_theorem_id for row in pairs}

    final_reps = _load_representations(config.inputs["depth_representations"].path, final_rep_ids)
    final_theorems = _load_theorems(config.inputs["depth_theorems"].path, final_theorem_ids)
    public_reps = _load_representations(
        config.inputs["public_representations"].path, original_rep_ids
    )
    private_reps = _load_representations(
        config.inputs["private_representations"].path, original_rep_ids
    )
    public_theorems = _load_theorems(
        config.inputs["public_theorems"].path, original_theorem_ids, envelope_key="theorem"
    )
    private_theorems = _load_theorems(
        config.inputs["private_theorems"].path,
        original_theorem_ids,
        envelope_key="theorem",
    )
    original_reps = _merge_disjoint(public_reps, private_reps, kind="representation")
    original_theorems = _merge_disjoint(public_theorems, private_theorems, kind="theorem")
    if (
        set(original_reps) != original_rep_ids
        or set(original_theorems) != original_theorem_ids
        or set(final_reps) != final_rep_ids
        or set(final_theorems) != final_theorem_ids
    ):
        raise CorpusV1Error("depth-3 source/final joins are incomplete")

    candidates: list[CorpusCandidate] = []
    for pair in pairs:
        source_rep = original_reps[pair.original_source_representation_id]
        source_theorem = original_theorems[pair.original_source_theorem_id]
        final_rep = final_reps[pair.selected_final_representation_id]
        final_theorem = final_theorems[pair.selected_final_theorem_id]
        roots = pair.root_ancestry_ids
        if (
            source_rep.theorem_id != source_theorem.theorem_id
            or final_rep.theorem_id != final_theorem.theorem_id
            or source_rep.context_id != pair.context_id
            or source_theorem.context_id != pair.context_id
            or final_rep.context_id != pair.context_id
            or final_theorem.context_id != pair.context_id
            or source_theorem.root_ancestry_ids != roots
            or final_theorem.root_ancestry_ids != roots
            or source_theorem.statement_content_hash != pair.original_source_statement_content_hash
            or source_rep.alpha_identity_fingerprint
            != pair.original_source_alpha_identity_fingerprint
            or final_rep.alpha_identity_fingerprint != pair.final_alpha_identity_fingerprint
            or source_rep.headless is None
            or final_rep.headless is None
        ):
            raise CorpusV1Error(f"depth-3 lineage join differs for {pair.pair_id}")
        label = pair.semantic_negative_hop_count == 0
        expected_intention = "equivalent_candidate" if label else "near_miss_candidate"
        if pair.preserved_intention != expected_intention:
            raise CorpusV1Error(f"depth-3 polarity differs for {pair.pair_id}")
        private = source_rep.representation_id in private_reps
        candidates.append(
            _validate_candidate(
                CorpusCandidate(
                    origin_id=pair.pair_id,
                    source_kind="deterministic_depth3_v2",
                    reference_headless=source_rep.headless,
                    candidate_headless=final_rep.headless,
                    label=label,
                    split_group_ids=roots,
                    family_ids=pair.depth_three_sequences,
                    provenance_ids=tuple(
                        sorted(
                            {
                                pair.pair_id,
                                pair.original_source_theorem_id,
                                pair.original_source_representation_id,
                                pair.selected_final_theorem_id,
                                pair.selected_final_representation_id,
                                *pair.chain_ids,
                            }
                        )
                    ),
                    split_anchor=None,
                    private_source_content=private,
                    redistribution_allowed=not private,
                    external_transmission_allowed=not private,
                    release_eligible=not private,
                )
            )
        )
    return candidates


def _load_recovered_candidates(config: CorpusV1Config) -> list[CorpusCandidate]:
    manifest = _read_json(config.inputs["recovered_manifest"].path)
    outputs = cast(Mapping[str, Any], manifest.get("outputs"))
    trainer_output = cast(Mapping[str, Any], outputs.get("trainer_records"))
    judgment_output = cast(Mapping[str, Any], outputs.get("judgments"))
    if (
        manifest.get("status") != "completed"
        or manifest.get("method_version") != "recovered_singlepass_codex_v1"
        or manifest.get("pair_plan_sha256") != config.inputs["recovered_plan"].sha256
        or trainer_output.get("sha256") != config.inputs["recovered_trainer"].sha256
        or judgment_output.get("sha256") != config.inputs["recovered_judgments"].sha256
    ):
        raise CorpusV1Error("recovered judge manifest is incomplete or has different outputs")

    plan: dict[str, RecoveredPlanRow] = {}
    for line_number, payload in _iter_jsonl(config.inputs["recovered_plan"].path):
        try:
            row = RecoveredPlanRow.model_validate(payload)
        except ValueError as exc:
            raise CorpusV1Error(f"recovered plan line {line_number} is invalid: {exc}") from exc
        if row.plan_row_id in plan:
            raise CorpusV1Error(f"recovered plan repeats {row.plan_row_id}")
        plan[row.plan_row_id] = row

    judgments: dict[str, RecoveredJudgment] = {}
    for line_number, payload in _iter_jsonl(config.inputs["recovered_judgments"].path):
        try:
            judgment_row = RecoveredJudgment.model_validate(payload)
        except ValueError as exc:
            raise CorpusV1Error(f"recovered judgment line {line_number} is invalid: {exc}") from exc
        if judgment_row.record_id in judgments:
            raise CorpusV1Error(f"recovered judgments repeat {judgment_row.record_id}")
        judgments[judgment_row.record_id] = judgment_row
    if len(judgments) != int(cast(Mapping[str, Any], manifest["counts"])["judged"]):
        raise CorpusV1Error("recovered judgment count differs from its final manifest")

    rows = _load_training_rows(config.inputs["recovered_trainer"].path)
    source_theorem_ids = {
        plan[judgments[cast(str, row["record_id"])].plan_row_id].source_theorem_id
        for row in rows
        if cast(str, row["record_id"]) in judgments
        and judgments[cast(str, row["record_id"])].plan_row_id in plan
    }
    public_theorems = _load_theorems(
        config.inputs["public_theorems"].path,
        source_theorem_ids,
        envelope_key="theorem",
    )
    if set(public_theorems) != source_theorem_ids:
        raise CorpusV1Error("recovered source theorem join is incomplete")

    candidates: list[CorpusCandidate] = []
    for payload in rows:
        record_id = cast(str, payload["record_id"])
        judgment = judgments.get(record_id)
        if judgment is None or judgment.final_label is None:
            raise CorpusV1Error(f"recovered trainer row is not a resolved judgment: {record_id}")
        plan_row = plan.get(judgment.plan_row_id)
        if plan_row is None:
            raise CorpusV1Error(f"recovered judgment lacks a plan row: {record_id}")
        theorem = public_theorems[plan_row.source_theorem_id]
        if (
            len(theorem.root_ancestry_ids) != 1
            or plan_row.group_key != theorem.root_ancestry_ids[0]
        ):
            raise CorpusV1Error(f"recovered plan ancestry differs from source theorem: {record_id}")
        expected = {
            "reference_headless": plan_row.reference_headless,
            "candidate_headless": plan_row.candidate_headless,
            "label": judgment.final_label,
            "group_key": plan_row.group_key,
            "family": plan_row.proposer_family_id,
            "source": "lf022_recovered_codex_judge_v1",
            "weight": 1.0,
        }
        if any(payload[name] != value for name, value in expected.items()):
            raise CorpusV1Error(f"recovered trainer projection differs: {record_id}")
        candidates.append(
            _validate_candidate(
                CorpusCandidate(
                    origin_id=record_id,
                    source_kind="recovered_codex_judged_v1",
                    reference_headless=plan_row.reference_headless,
                    candidate_headless=plan_row.candidate_headless,
                    label=judgment.final_label,
                    split_group_ids=(plan_row.group_key,),
                    family_ids=(plan_row.proposer_family_id,),
                    provenance_ids=tuple(
                        sorted(
                            {
                                record_id,
                                judgment.plan_row_id,
                                plan_row.audit_input.pair.pair_id,
                                plan_row.audit_input.variant_id,
                            }
                        )
                    ),
                    split_anchor=None,
                    private_source_content=False,
                    redistribution_allowed=True,
                    external_transmission_allowed=True,
                    release_eligible=True,
                )
            )
        )
    resolved = int(cast(Mapping[str, Any], manifest["counts"])["resolved"])
    if len(candidates) != resolved:
        raise CorpusV1Error("recovered trainer count differs from resolved manifest count")
    return candidates


def _d3_record_id(*, theorem_id: str, candidate: str, family: str, label: bool) -> str:
    identity = {
        "schema": "d3_trainer_record_v1",
        "theorem_id": theorem_id,
        "candidate_sha256": _sha_text(candidate),
        "family": family,
        "label": label,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "d3:" + hashlib.sha256(encoded).hexdigest()


def _validate_d3_job_join(
    *,
    index: int,
    job: Mapping[str, Any],
    record: Mapping[str, Any],
    check: Mapping[str, Any],
) -> str:
    """Bind one frozen D-3 job, generation, and Lean-check without text ambiguity."""

    for field in (
        "index",
        "source_statement",
        "statement_hash",
        "statement_id",
        "assigned_family",
        "direction",
        "prompt_sha256",
        "provider",
    ):
        if record.get(field) != job.get(field):
            raise CorpusV1Error(f"D-3 job/record field differs at index {index}: {field}")
    source = job.get("source_statement")
    candidate = record.get("rewritten_statement")
    if (
        job.get("index") != index
        or not isinstance(job.get("job_id"), str)
        or not isinstance(job.get("theorem_id"), str)
        or not isinstance(job.get("group_key"), str)
        or not isinstance(job.get("statement_id"), str)
        or not isinstance(job.get("assigned_family"), str)
        or not isinstance(source, str)
        or not isinstance(candidate, str)
        or record.get("transformation") != job.get("assigned_family")
        or check.get("job_id") != job.get("job_id")
        or check.get("candidate_sha256") != _sha_text(candidate)
        or check.get("candidate_near_dup_hash") != signature_near_dup_hash(candidate)
    ):
        raise CorpusV1Error(f"D-3 job/record/check identity differs at index {index}")
    return candidate


def _validate_d3_source_join(
    *,
    index: int,
    job: Mapping[str, Any],
    representation: RepresentationRecord,
    theorem: TheoremRecord,
) -> None:
    if (
        representation.representation_id != job.get("statement_id")
        or representation.theorem_id != theorem.theorem_id
        or theorem.theorem_id != job.get("theorem_id")
        or representation.context_id != theorem.context_id
        or representation.headless != job.get("source_statement")
        or representation.content_hash != job.get("statement_hash")
        or theorem.root_ancestry_ids != (job.get("group_key"),)
    ):
        raise CorpusV1Error(f"D-3 source representation/theorem differs at index {index}")


def _load_d3_candidates(config: CorpusV1Config) -> list[CorpusCandidate]:
    manifest = _read_json(config.inputs["d3_manifest"].path)
    outputs = cast(Mapping[str, Any], manifest.get("outputs"))
    for output_name, binding_name in (
        ("job_plan", "d3_job_plan"),
        ("records", "d3_records"),
        ("lean_checks", "d3_checks"),
        ("trainer_records", "d3_trainer"),
    ):
        output = cast(Mapping[str, Any], outputs.get(output_name))
        if (
            output.get("path") != str(config.inputs[binding_name].path)
            or output.get("sha256") != config.inputs[binding_name].sha256
        ):
            raise CorpusV1Error(f"D-3 manifest output differs: {output_name}")
    input_hashes = cast(Mapping[str, Any], manifest.get("input_sha256"))
    if (
        manifest.get("status") != "completed"
        or manifest.get("private_source_content") is not False
        or manifest.get("public_source_only") is not True
        or manifest.get("job_plan_sha256") != config.inputs["d3_job_plan"].sha256
        or input_hashes.get("golden_blocklist") != config.inputs["blocklist"].sha256
        or input_hashes.get("representations") != config.inputs["public_representations"].sha256
        or input_hashes.get("theorems") != config.inputs["public_theorems"].sha256
    ):
        raise CorpusV1Error("D-3 manifest is incomplete or not public-only")

    jobs: dict[int, dict[str, Any]] = {}
    jobs_by_id: dict[str, dict[str, Any]] = {}
    for line_number, row in _iter_jsonl(config.inputs["d3_job_plan"].path):
        index = row.get("index")
        job_id = row.get("job_id")
        if not isinstance(index, int) or not isinstance(job_id, str):
            raise CorpusV1Error(f"D-3 job plan line {line_number} lacks identity")
        if index in jobs or job_id in jobs_by_id:
            raise CorpusV1Error("D-3 job plan repeats an identity")
        jobs[index] = row
        jobs_by_id[job_id] = row
    generated: dict[int, dict[str, Any]] = {}
    for line_number, row in _iter_jsonl(config.inputs["d3_records"].path):
        index = row.get("index")
        if not isinstance(index, int) or index in generated:
            raise CorpusV1Error(f"D-3 record line {line_number} has invalid identity")
        generated[index] = row
    checks: dict[str, dict[str, Any]] = {}
    for line_number, row in _iter_jsonl(config.inputs["d3_checks"].path):
        job_id = row.get("job_id")
        if not isinstance(job_id, str) or job_id in checks:
            raise CorpusV1Error(f"D-3 check line {line_number} has invalid identity")
        checks[job_id] = row
    trainers = _load_training_rows(config.inputs["d3_trainer"].path)
    trainer_by_id = {cast(str, row["record_id"]): row for row in trainers}
    if (
        set(generated) != set(jobs)
        or set(checks) != set(jobs_by_id)
        or manifest.get("count") != len(jobs)
    ):
        raise CorpusV1Error("D-3 job/generation/check identity sets differ")

    source_representation_ids = {cast(str, job["statement_id"]) for job in jobs.values()}
    source_theorem_ids = {cast(str, job["theorem_id"]) for job in jobs.values()}
    source_representations = _load_representations(
        config.inputs["public_representations"].path,
        source_representation_ids,
    )
    source_theorems = _load_theorems(
        config.inputs["public_theorems"].path,
        source_theorem_ids,
        envelope_key="theorem",
    )
    if (
        set(source_representations) != source_representation_ids
        or set(source_theorems) != source_theorem_ids
    ):
        raise CorpusV1Error("D-3 public source joins are incomplete")

    expected_rows: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for index in sorted(jobs):
        job = jobs[index]
        record = generated.get(index)
        check = checks.get(cast(str, job["job_id"]))
        if record is None or check is None:
            raise CorpusV1Error("D-3 generation/check join is incomplete")
        candidate = _validate_d3_job_join(
            index=index,
            job=job,
            record=record,
            check=check,
        )
        source_representation = source_representations[cast(str, job["statement_id"])]
        source_theorem = source_theorems[cast(str, job["theorem_id"])]
        _validate_d3_source_join(
            index=index,
            job=job,
            representation=source_representation,
            theorem=source_theorem,
        )
        admitted = (
            record.get("parse_ok") is True
            and record.get("label_matches_direction") is True
            and record.get("family_matches_assignment") is True
            and record.get("rewrite_changed") is True
            and check.get("status") == "valid"
            and check.get("candidate_blocked") is False
            and record.get("intended_label") in {"consistent", "inconsistent"}
        )
        if not admitted:
            continue
        family = cast(str, job["assigned_family"])
        label = record["intended_label"] == "consistent"
        record_id = _d3_record_id(
            theorem_id=cast(str, job["theorem_id"]),
            candidate=candidate,
            family=family,
            label=label,
        )
        expected = {
            "record_id": record_id,
            "reference_headless": job["source_statement"],
            "candidate_headless": candidate,
            "label": label,
            "group_key": job["group_key"],
            "family": family,
            "source": "d3_codex_scale_v1",
            "weight": 1.0,
        }
        if record_id in expected_rows:
            raise CorpusV1Error(f"D-3 admitted rows collide on record ID: {record_id}")
        expected_rows[record_id] = (expected, job)
    if set(trainer_by_id) != set(expected_rows):
        raise CorpusV1Error("D-3 trainer IDs differ from admitted generation/check rows")
    trainer_manifest = cast(Mapping[str, Any], manifest.get("trainer"))
    observed_families = Counter(cast(str, row["family"]) for row in trainers)
    observed_labels = Counter(str(row["label"]).lower() for row in trainers)
    if (
        trainer_manifest.get("record_count") != len(trainers)
        or trainer_manifest.get("family_counts") != dict(sorted(observed_families.items()))
        or trainer_manifest.get("label_counts") != dict(sorted(observed_labels.items()))
    ):
        raise CorpusV1Error("D-3 trainer counts differ from its manifest")

    candidates: list[CorpusCandidate] = []
    for record_id in sorted(expected_rows):
        expected, job = expected_rows[record_id]
        if trainer_by_id[record_id] != expected:
            raise CorpusV1Error(f"D-3 trainer projection differs: {record_id}")
        theorem = source_theorems[cast(str, job["theorem_id"])]
        group_key = cast(str, job["group_key"])
        if len(theorem.root_ancestry_ids) != 1 or theorem.root_ancestry_ids[0] != group_key:
            raise CorpusV1Error(f"D-3 ancestry differs from source theorem: {record_id}")
        candidates.append(
            _validate_candidate(
                CorpusCandidate(
                    origin_id=record_id,
                    source_kind="d3_codex_scale_v1",
                    reference_headless=cast(str, expected["reference_headless"]),
                    candidate_headless=cast(str, expected["candidate_headless"]),
                    label=cast(bool, expected["label"]),
                    split_group_ids=(group_key,),
                    family_ids=(cast(str, expected["family"]),),
                    provenance_ids=tuple(
                        sorted(
                            {
                                record_id,
                                cast(str, job["job_id"]),
                                cast(str, job["statement_id"]),
                                cast(str, job["theorem_id"]),
                            }
                        )
                    ),
                    split_anchor=None,
                    private_source_content=False,
                    redistribution_allowed=True,
                    external_transmission_allowed=True,
                    release_eligible=True,
                )
            )
        )
    return candidates


def load_production_candidates(config: CorpusV1Config) -> list[CorpusCandidate]:
    """Load and strictly join all four frozen corpus sources."""

    candidates = [
        *_load_v0_candidates(config),
        *_load_depth_candidates(config),
        *_load_recovered_candidates(config),
        *_load_d3_candidates(config),
    ]
    origin_ids = [row.origin_id for row in candidates]
    if len(set(origin_ids)) != len(origin_ids):
        raise CorpusV1Error("source loaders produced duplicate origin IDs")
    return candidates


def _exclusion(reason: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "reason": reason,
        **dict(payload),
    }
    values["exclusion_id"] = "corpus_v1_exclusion:" + hash_canonical(values)
    return values


def screen_candidates(
    candidates: Sequence[CorpusCandidate],
    *,
    blocklist: GoldenBlocklist,
    tokenizer: _Tokenizer,
    max_tokens: int = MAX_TOKENS,
) -> tuple[list[ScreenedCandidate], list[dict[str, Any]]]:
    """Apply current golden screening and the bidirectional packed-token limit."""

    screened: list[ScreenedCandidate] = []
    exclusions: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: item.origin_id):
        _validate_candidate(candidate)
        reference_hash = signature_near_dup_hash(candidate.reference_headless)
        candidate_hash = signature_near_dup_hash(candidate.candidate_headless)
        pair_key = tuple(sorted((reference_hash, candidate_hash)))
        if (
            reference_hash in blocklist.near_dup_hashes
            or candidate_hash in blocklist.near_dup_hashes
            or any(blocklist.problem_is_blocked(group) for group in candidate.split_group_ids)
        ):
            exclusions.append(
                _exclusion(
                    "golden_blocklist",
                    {
                        "origin_ids": [candidate.origin_id],
                        "source_kinds": [candidate.source_kind],
                        "pair_key": list(pair_key),
                    },
                )
            )
            continue
        if reference_hash == candidate_hash:
            exclusions.append(
                _exclusion(
                    "degenerate_near_identical_sides",
                    {
                        "origin_ids": [candidate.origin_id],
                        "source_kinds": [candidate.source_kind],
                        "pair_key": list(pair_key),
                    },
                )
            )
            continue
        forward = len(
            tokenizer.encode(
                pack_pair(candidate.reference_headless, candidate.candidate_headless),
                add_special_tokens=True,
            )
        )
        reverse = len(
            tokenizer.encode(
                pack_pair(candidate.candidate_headless, candidate.reference_headless),
                add_special_tokens=True,
            )
        )
        if not forward or not reverse:
            raise CorpusV1Error(f"tokenizer returned an empty packed pair: {candidate.origin_id}")
        if forward > max_tokens or reverse > max_tokens:
            exclusions.append(
                _exclusion(
                    "overlength",
                    {
                        "origin_ids": [candidate.origin_id],
                        "source_kinds": [candidate.source_kind],
                        "pair_key": list(pair_key),
                        "forward_tokens": forward,
                        "reverse_tokens": reverse,
                        "max_tokens": max_tokens,
                    },
                )
            )
            continue
        screened.append(
            ScreenedCandidate(
                candidate=candidate,
                reference_near_hash=reference_hash,
                candidate_near_hash=candidate_hash,
                pair_key=cast(tuple[str, str], pair_key),
                forward_tokens=forward,
                reverse_tokens=reverse,
            )
        )
    return screened, exclusions


def deduplicate_pairs(
    candidates: Sequence[ScreenedCandidate],
) -> tuple[
    list[MergedPair],
    dict[str, ComponentSeed],
    list[dict[str, Any]],
]:
    """Deduplicate unordered near-signature pairs and quarantine label conflicts."""

    grouped: dict[tuple[str, str], list[ScreenedCandidate]] = defaultdict(list)
    for row in candidates:
        grouped[row.pair_key].append(row)
    merged: list[MergedPair] = []
    component_seeds: dict[str, ComponentSeed] = {}
    exclusions: list[dict[str, Any]] = []
    for pair_key in sorted(grouped):
        group = grouped[pair_key]
        pair_id = "corpus_v1_pair:" + hash_canonical(
            {"schema": "corpus_v1_unordered_pair_v1", "pair_key": list(pair_key)}
        )
        groups = tuple(
            sorted({value for item in group for value in item.candidate.split_group_ids})
        )
        anchors = tuple(
            sorted(
                {
                    item.candidate.split_anchor
                    for item in group
                    if item.candidate.split_anchor is not None
                }
            )
        )
        labels = {item.candidate.label for item in group}
        origin_ids = tuple(sorted(item.candidate.origin_id for item in group))
        source_kinds = tuple(sorted({item.candidate.source_kind for item in group}))
        component_seeds[pair_id] = ComponentSeed(
            pair_id=pair_id,
            pair_key=pair_key,
            split_group_ids=groups,
            statement_near_hashes=pair_key,
            split_anchors=anchors,
            origin_ids=origin_ids,
            source_kinds=source_kinds,
        )
        if len(labels) != 1:
            exclusions.append(
                _exclusion(
                    "conflicting_labels",
                    {
                        "pair_id": pair_id,
                        "pair_key": list(pair_key),
                        "origin_ids": list(origin_ids),
                        "source_kinds": list(source_kinds),
                        "split_group_ids": list(groups),
                        "split_anchors": list(anchors),
                        "observed_labels": sorted(labels),
                        "label_by_origin": [
                            {
                                "origin_id": item.candidate.origin_id,
                                "source_kind": item.candidate.source_kind,
                                "label": item.candidate.label,
                            }
                            for item in sorted(group, key=lambda value: value.candidate.origin_id)
                        ],
                    },
                )
            )
            continue
        representative = min(
            group,
            key=lambda item: (
                _sha_text(item.candidate.reference_headless),
                _sha_text(item.candidate.candidate_headless),
                item.candidate.origin_id,
            ),
        )
        merged.append(
            MergedPair(
                pair_id=pair_id,
                pair_key=pair_key,
                reference_headless=representative.candidate.reference_headless,
                candidate_headless=representative.candidate.candidate_headless,
                label=labels.pop(),
                split_group_ids=groups,
                family_ids=tuple(
                    sorted({value for item in group for value in item.candidate.family_ids})
                ),
                origin_ids=origin_ids,
                source_kinds=source_kinds,
                provenance_ids=tuple(
                    sorted({value for item in group for value in item.candidate.provenance_ids})
                ),
                split_anchors=anchors,
                private_source_content=any(item.candidate.private_source_content for item in group),
                redistribution_allowed=all(item.candidate.redistribution_allowed for item in group),
                external_transmission_allowed=all(
                    item.candidate.external_transmission_allowed for item in group
                ),
                release_eligible=all(item.candidate.release_eligible for item in group),
                forward_tokens=representative.forward_tokens,
                reverse_tokens=representative.reverse_tokens,
            )
        )
    return merged, component_seeds, exclusions


def _lineage_clusters(
    component_seeds: Mapping[str, ComponentSeed],
) -> tuple[dict[str, str], list[LineageCluster]]:
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(value: tuple[str, str]) -> tuple[str, str]:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            successor = parent[value]
            parent[value] = root
            value = successor
        return root

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        low, high = sorted((left_root, right_root))
        parent[high] = low

    for item_id, item in sorted(component_seeds.items()):
        if item.pair_id != item_id:
            raise CorpusV1Error(f"component seed identity differs: {item_id}")
        if not item.split_group_ids or item.split_group_ids != tuple(
            sorted(set(item.split_group_ids))
        ):
            raise CorpusV1Error(f"component seed has invalid ancestry groups: {item_id}")
        if (
            not item.statement_near_hashes
            or item.statement_near_hashes != tuple(sorted(set(item.statement_near_hashes)))
            or item.statement_near_hashes != item.pair_key
        ):
            raise CorpusV1Error(f"component seed has invalid statement identities: {item_id}")
        if item.split_anchors != tuple(sorted(set(item.split_anchors))) or any(
            anchor not in SPLITS for anchor in item.split_anchors
        ):
            raise CorpusV1Error(f"component seed has invalid split anchors: {item_id}")
        nodes = [
            *(("group", group) for group in item.split_group_ids),
            *(("statement", statement) for statement in item.statement_near_hashes),
        ]
        for node in nodes:
            parent.setdefault(node, node)
        for node in nodes[1:]:
            union(nodes[0], node)

    members: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for node in parent:
        members[find(node)].add(node)
    component_by_root: dict[tuple[str, str], str] = {}
    groups_by_component: dict[str, tuple[str, ...]] = {}
    statements_by_component: dict[str, tuple[str, ...]] = {}
    for root, member_nodes in members.items():
        groups = tuple(sorted(value for kind, value in member_nodes if kind == "group"))
        statements = tuple(sorted(value for kind, value in member_nodes if kind == "statement"))
        if not groups or not statements:
            raise CorpusV1Error("lineage component lacks a group or statement identity")
        component_id = "corpus_v1_component:" + hash_canonical(
            {
                "schema": "corpus_v1_group_statement_component_v1",
                "split_group_ids": list(groups),
                "statement_near_hashes": list(statements),
            }
        )
        component_by_root[root] = component_id
        groups_by_component[component_id] = groups
        statements_by_component[component_id] = statements
    item_components = {
        item_id: component_by_root[find(("group", item.split_group_ids[0]))]
        for item_id, item in component_seeds.items()
    }
    pair_ids: dict[str, set[str]] = defaultdict(set)
    anchors: dict[str, set[str]] = defaultdict(set)
    for item_id, item in component_seeds.items():
        component_id = item_components[item_id]
        pair_ids[component_id].add(item_id)
        anchors[component_id].update(item.split_anchors)
    clusters = [
        LineageCluster(
            component_id=component_id,
            pair_ids=tuple(sorted(pair_ids[component_id])),
            split_group_ids=groups_by_component[component_id],
            statement_near_hashes=statements_by_component[component_id],
            split_anchors=tuple(sorted(anchors[component_id])),
        )
        for component_id in sorted(groups_by_component)
    ]
    return item_components, clusters


def quarantine_split_anchor_conflicts(
    rows: Sequence[MergedPair],
    component_seeds: Mapping[str, ComponentSeed],
) -> tuple[list[MergedPair], dict[str, ComponentSeed], list[dict[str, Any]]]:
    """Quarantine every complete pre-cap component that crosses frozen splits."""

    _, clusters = _lineage_clusters(component_seeds)
    conflicts = [cluster for cluster in clusters if len(cluster.split_anchors) > 1]
    conflicting_ids = {pair_id for cluster in conflicts for pair_id in cluster.pair_ids}
    retained = sorted(
        (row for row in rows if row.pair_id not in conflicting_ids),
        key=lambda item: item.pair_id,
    )
    retained_seeds = {
        pair_id: seed
        for pair_id, seed in sorted(component_seeds.items())
        if pair_id not in conflicting_ids
    }
    exclusions: list[dict[str, Any]] = []
    for cluster in conflicts:
        for pair_id in cluster.pair_ids:
            item = component_seeds[pair_id]
            exclusions.append(
                _exclusion(
                    "split_anchor_component_conflict",
                    {
                        "pair_id": item.pair_id,
                        "pair_key": list(item.pair_key),
                        "origin_ids": list(item.origin_ids),
                        "source_kinds": list(item.source_kinds),
                        "split_group_ids": list(item.split_group_ids),
                        "statement_near_hashes": list(item.statement_near_hashes),
                        "split_anchors": list(item.split_anchors),
                        "conflict_component_id": cluster.component_id,
                        "conflict_component_pair_ids": list(cluster.pair_ids),
                        "conflict_component_group_ids": list(cluster.split_group_ids),
                        "conflict_component_statement_near_hashes": list(
                            cluster.statement_near_hashes
                        ),
                        "conflict_component_split_anchors": list(cluster.split_anchors),
                    },
                )
            )
    _, replay_clusters = _lineage_clusters(retained_seeds)
    if any(len(cluster.split_anchors) > 1 for cluster in replay_clusters):
        raise CorpusV1Error("split-anchor component quarantine did not reach a safe graph")
    return retained, retained_seeds, exclusions


def _unanchored_component_split(
    component_id: str, seed: int
) -> Literal["train", "validation", "test"]:
    bucket = (
        int(
            hash_canonical(
                {
                    "schema": "corpus_v1_component_split_v1",
                    "seed": seed,
                    "component_id": component_id,
                }
            )[:8],
            16,
        )
        % 100
    )
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def build_components(
    component_seeds: Mapping[str, ComponentSeed],
    *,
    seed: int,
) -> tuple[dict[str, str], list[Component]]:
    """Union pre-cap ancestry groups and shared statements, preserving v0 anchors."""

    item_components, clusters = _lineage_clusters(component_seeds)
    components: list[Component] = []
    for cluster in clusters:
        anchors = cluster.split_anchors
        if len(anchors) > 1:
            raise CorpusV1Error(
                f"ancestry component crosses frozen v0 splits: {cluster.component_id} {anchors}"
            )
        if anchors:
            split = cast(Literal["train", "validation", "test"], anchors[0])
        else:
            split = _unanchored_component_split(cluster.component_id, seed)
        components.append(
            Component(
                component_id=cluster.component_id,
                split_group_ids=cluster.split_group_ids,
                statement_near_hashes=cluster.statement_near_hashes,
                split=split,
                split_anchors=anchors,
            )
        )
    return item_components, components


def apply_family_cap(
    rows: Sequence[MergedPair],
    *,
    seed: int = DEFAULT_SEED,
) -> tuple[list[MergedPair], list[dict[str, Any]]]:
    """Apply the deterministic stored-membership 10% denominator fixed point."""

    selected = {row.pair_id: row for row in rows}
    exclusions: list[dict[str, Any]] = []
    round_index = 0
    while selected:
        count = len(selected)
        limit = count // 10
        family_counts = Counter(family for row in selected.values() for family in row.family_ids)
        violating = [
            (family_count - limit, family_count, family)
            for family, family_count in family_counts.items()
            if 10 * family_count > count
        ]
        if not violating:
            break
        _, family_count, family = sorted(violating, key=lambda item: (-item[0], -item[1], item[2]))[
            0
        ]
        other_count = count - family_count
        keep_limit = other_count // 9
        members = sorted(
            (row for row in selected.values() if family in row.family_ids),
            key=lambda row: (
                hash_canonical(
                    {
                        "schema": "corpus_v1_family_cap_rank_v1",
                        "seed": seed,
                        "family": family,
                        "pair_key": list(row.pair_key),
                    }
                ),
                row.pair_id,
            ),
        )
        dropped = members[keep_limit:]
        if not dropped:
            raise CorpusV1Error("family cap fixed point made no progress")
        for rank, row in enumerate(members):
            if rank < keep_limit:
                continue
            exclusions.append(
                _exclusion(
                    "family_cap",
                    {
                        "pair_id": row.pair_id,
                        "pair_key": list(row.pair_key),
                        "origin_ids": list(row.origin_ids),
                        "source_kinds": list(row.source_kinds),
                        "trigger_family": family,
                        "round_index": round_index,
                        "family_count_before": family_count,
                        "corpus_count_before": count,
                        "keep_limit": keep_limit,
                        "family_rank": rank,
                    },
                )
            )
            del selected[row.pair_id]
        round_index += 1
    final = sorted(selected.values(), key=lambda row: row.pair_id)
    counts = Counter(family for row in final for family in row.family_ids)
    if any(10 * value > len(final) for value in counts.values()):
        raise CorpusV1Error("family cap did not reach a valid fixed point")
    return final, exclusions


def make_final_rows(
    rows: Sequence[MergedPair],
    *,
    item_components: Mapping[str, str],
    components: Sequence[Component],
) -> list[FinalRow]:
    component_by_id = {component.component_id: component for component in components}
    output: list[FinalRow] = []
    for row in rows:
        component = component_by_id[item_components[row.pair_id]]
        identity = {
            "schema": "corpus_v1_trainer_record_v1",
            "pair_id": row.pair_id,
            "reference_sha256": _sha_text(row.reference_headless),
            "candidate_sha256": _sha_text(row.candidate_headless),
            "label": row.label,
            "component_id": component.component_id,
            "origin_ids": list(row.origin_ids),
        }
        record_id = "corpus_v1:" + hash_canonical(identity)
        trainer = TrainingRecord(
            record_id=record_id,
            reference_headless=row.reference_headless,
            candidate_headless=row.candidate_headless,
            label=row.label,
            group_key=component.component_id,
            family="+".join(row.family_ids),
            source="+".join(row.source_kinds),
            weight=1.0,
        )
        provenance: dict[str, Any] = {
            "schema_version": 1,
            "record_id": record_id,
            "pair_id": row.pair_id,
            "pair_key": list(row.pair_key),
            "reference_sha256": _sha_text(row.reference_headless),
            "candidate_sha256": _sha_text(row.candidate_headless),
            "label": row.label,
            "group_key": component.component_id,
            "split": component.split,
            "split_group_ids": list(row.split_group_ids),
            "component_group_ids": list(component.split_group_ids),
            "component_statement_near_hashes": list(component.statement_near_hashes),
            "family_ids": list(row.family_ids),
            "origin_ids": list(row.origin_ids),
            "source_kinds": list(row.source_kinds),
            "provenance_ids": list(row.provenance_ids),
            "forward_tokens": row.forward_tokens,
            "reverse_tokens": row.reverse_tokens,
            "private_source_content": row.private_source_content,
            "redistribution_allowed": row.redistribution_allowed,
            "external_transmission_allowed": row.external_transmission_allowed,
            "release_eligible": row.release_eligible,
        }
        output.append(FinalRow(trainer=trainer, provenance=provenance, split=component.split))
    output.sort(key=lambda item: item.trainer.record_id)
    return output


def _bow_features(
    tokenizer: _Tokenizer,
    reference: str,
    candidate: str,
    *,
    swap: bool,
) -> dict[int, float]:
    left, right = (candidate, reference) if swap else (reference, candidate)
    left_ids = tokenizer.encode(left, add_special_tokens=False)
    right_ids = tokenizer.encode(right, add_special_tokens=False)
    if any(not isinstance(value, int) or value < 0 for value in (*left_ids, *right_ids)):
        raise CorpusV1Error("canary tokenizer returned an invalid token ID")
    counts: Counter[int] = Counter(2 * value for value in left_ids)
    counts.update(2 * value + 1 for value in right_ids)
    return {key: math.log1p(value) for key, value in counts.items()}


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        exp = math.exp(-min(value, 40.0))
        return 1.0 / (1.0 + exp)
    exp = math.exp(max(value, -40.0))
    return exp / (1.0 + exp)


def _balanced_metrics(labels: Sequence[bool], probabilities: Sequence[float]) -> dict[str, Any]:
    if len(labels) != len(probabilities) or not labels:
        raise CorpusV1Error("canary metric inputs are empty or misaligned")
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        raise CorpusV1Error("canary diagnostic split must contain both labels")
    tp = tn = fp = fn = 0
    for label, probability in zip(labels, probabilities, strict=True):
        prediction = probability >= 0.5
        if label and prediction:
            tp += 1
        elif label:
            fn += 1
        elif prediction:
            fp += 1
        else:
            tn += 1
    return {
        "record_count": len(labels),
        "positive_count": positives,
        "negative_count": negatives,
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "balanced_accuracy": 0.5 * (tp / positives + tn / negatives),
        "accuracy": (tp + tn) / len(labels),
    }


def run_lexical_canary(
    rows: Sequence[FinalRow],
    *,
    tokenizer: _Tokenizer,
    seed: int,
    epochs: int,
    learning_rate: float,
    target: float,
) -> dict[str, Any]:
    """Fit a deterministic sparse bag-of-token logistic shortcut diagnostic."""

    by_split = {split: [row for row in rows if row.split == split] for split in SPLITS}
    train = by_split["train"]
    label_counts = Counter(row.trainer.label for row in train)
    if not train or set(label_counts) != {False, True}:
        raise CorpusV1Error("lexical canary training split must contain both labels")
    class_weights = {label: len(train) / (2.0 * label_counts[label]) for label in (False, True)}
    weights: dict[int, float] = {}
    accumulators: dict[int, float] = {}
    bias = 0.0
    bias_accumulator = 1e-12
    for epoch in range(epochs):
        ordered = sorted(
            train,
            key=lambda row: hash_canonical(
                {
                    "schema": "corpus_v1_lexical_canary_order_v1",
                    "seed": seed,
                    "epoch": epoch,
                    "record_id": row.trainer.record_id,
                }
            ),
        )
        for row in ordered:
            for swap in (False, True):
                features = _bow_features(
                    tokenizer,
                    row.trainer.reference_headless,
                    row.trainer.candidate_headless,
                    swap=swap,
                )
                logit = bias + math.fsum(
                    weights.get(key, 0.0) * value for key, value in features.items()
                )
                gradient = (_sigmoid(logit) - float(row.trainer.label)) * class_weights[
                    row.trainer.label
                ]
                bias_accumulator += gradient * gradient
                bias -= learning_rate * gradient / math.sqrt(bias_accumulator)
                for key, value in features.items():
                    feature_gradient = gradient * value
                    accumulator = accumulators.get(key, 1e-12) + feature_gradient * feature_gradient
                    accumulators[key] = accumulator
                    weights[key] = weights.get(key, 0.0) - (
                        learning_rate * feature_gradient / math.sqrt(accumulator)
                    )

    diagnostics: dict[str, Any] = {}
    for split in ("validation", "test"):
        labels: list[bool] = []
        probabilities: list[float] = []
        for row in by_split[split]:
            directional: list[float] = []
            for swap in (False, True):
                features = _bow_features(
                    tokenizer,
                    row.trainer.reference_headless,
                    row.trainer.candidate_headless,
                    swap=swap,
                )
                directional.append(
                    _sigmoid(
                        bias
                        + math.fsum(
                            weights.get(key, 0.0) * value for key, value in features.items()
                        )
                    )
                )
            labels.append(row.trainer.label)
            probabilities.append(math.fsum(directional) / 2.0)
        diagnostics[split] = _balanced_metrics(labels, probabilities)
    maximum = max(
        cast(float, diagnostics[split]["balanced_accuracy"]) for split in ("validation", "test")
    )
    return {
        "schema_version": 1,
        "method_version": "modernbert_token_bow_logistic_canary_v1",
        "seed": seed,
        "training_split": "train",
        "diagnostic_splits": ["validation", "test"],
        "swap_augmented_training": True,
        "class_balance": "inverse_frequency",
        "optimizer": "deterministic_online_adagrad",
        "zero_initialization": True,
        "epochs": epochs,
        "learning_rate": learning_rate,
        "decision_threshold": 0.5,
        "training_record_count": len(train),
        "training_record_ids_sha256": hash_canonical(
            sorted(row.trainer.record_id for row in train)
        ),
        "nonzero_feature_count": len(weights),
        "diagnostics": diagnostics,
        "target_balanced_accuracy_below": target,
        "target_met": maximum < target,
    }


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _write_jsonl(path: Path, rows: Iterable[object]) -> None:
    path.write_bytes(b"".join(_canonical_line(row) for row in rows))


def _flat_directories_match(left: Path, right: Path) -> bool:
    if not left.is_dir() or not right.is_dir():
        return False
    left_files = {path.name: path for path in left.iterdir()}
    right_files = {path.name: path for path in right.iterdir()}
    if set(left_files) != set(right_files):
        return False
    return all(
        left_files[name].is_file()
        and not left_files[name].is_symlink()
        and right_files[name].is_file()
        and not right_files[name].is_symlink()
        and left_files[name].stat().st_size == right_files[name].stat().st_size
        and hash_file(left_files[name]) == hash_file(right_files[name])
        for name in left_files
    )


def materialize_candidates(
    config: CorpusV1Config,
    candidates: Sequence[CorpusCandidate],
    *,
    blocklist: GoldenBlocklist,
    tokenizer: _Tokenizer,
) -> dict[str, Any]:
    """Assemble candidates and atomically materialize the complete corpus-v1 root."""

    screened, screen_exclusions = screen_candidates(
        candidates,
        blocklist=blocklist,
        tokenizer=tokenizer,
        max_tokens=config.max_tokens,
    )
    merged, component_seeds, conflict_exclusions = deduplicate_pairs(screened)
    anchor_safe, component_seeds, anchor_exclusions = quarantine_split_anchor_conflicts(
        merged, component_seeds
    )
    item_components, components = build_components(component_seeds, seed=config.seed)
    capped, cap_exclusions = apply_family_cap(anchor_safe, seed=config.seed)
    final_rows = make_final_rows(
        capped,
        item_components=item_components,
        components=components,
    )
    if not final_rows:
        raise CorpusV1Error("corpus-v1 assembly produced no trainer rows")
    canary = run_lexical_canary(
        final_rows,
        tokenizer=tokenizer,
        seed=config.seed,
        epochs=config.canary_epochs,
        learning_rate=config.canary_learning_rate,
        target=config.canary_target_balanced_accuracy,
    )
    exclusions = sorted(
        [
            *screen_exclusions,
            *conflict_exclusions,
            *anchor_exclusions,
            *cap_exclusions,
        ],
        key=lambda row: cast(str, row["exclusion_id"]),
    )

    output_root = config.output_root.resolve()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        run_config_path = staging / "run_config.json"
        run_config_path.write_bytes(_canonical_line(config.model_dump(mode="json")))
        for split in SPLITS:
            _write_jsonl(
                staging / f"records_{split}_v1.jsonl",
                (row.trainer.model_dump(mode="json") for row in final_rows if row.split == split),
            )
        _write_jsonl(staging / "provenance_v1.jsonl", (row.provenance for row in final_rows))
        _write_jsonl(
            staging / "components_v1.jsonl",
            (
                {
                    "schema_version": 1,
                    "component_id": component.component_id,
                    "split_group_ids": list(component.split_group_ids),
                    "statement_near_hashes": list(component.statement_near_hashes),
                    "split": component.split,
                    "split_anchors": list(component.split_anchors),
                }
                for component in components
            ),
        )
        _write_jsonl(staging / "exclusions_v1.jsonl", exclusions)
        (staging / "lexical_canary.json").write_bytes(_canonical_line(canary))
        output_names = [
            "run_config.json",
            *(f"records_{split}_v1.jsonl" for split in SPLITS),
            "provenance_v1.jsonl",
            "components_v1.jsonl",
            "exclusions_v1.jsonl",
            "lexical_canary.json",
        ]
        output_bindings = {
            name: {"path": str(output_root / name), "sha256": hash_file(staging / name)}
            for name in output_names
        }
        split_counts = Counter(row.split for row in final_rows)
        label_counts = Counter(str(row.trainer.label).lower() for row in final_rows)
        source_counts = Counter(
            source
            for row in final_rows
            for source in cast(list[str], row.provenance["source_kinds"])
        )
        family_counts = Counter(
            family for row in final_rows for family in cast(list[str], row.provenance["family_ids"])
        )
        exclusion_counts = Counter(cast(str, row["reason"]) for row in exclusions)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "git_revision": _git_revision(),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "config_sha256": hash_file(run_config_path),
            "seed": config.seed,
            "input_sha256": {
                name: binding.sha256 for name, binding in sorted(config.inputs.items())
            },
            "tokenizer_sha256": {
                name: binding.sha256 for name, binding in sorted(config.tokenizer_files.items())
            },
            "policies": {
                "pair_deduplication": "unordered_signature_near_dup_hash_pair_v1",
                "conflicting_labels": "quarantine_entire_pair_keep_lineage_seed",
                "frozen_split_anchor_conflicts": (
                    "quarantine_entire_multianchor_component_then_rebuild_v1"
                ),
                "ancestry_components": "pre_cap_group_and_statement_union_with_v0_anchors_v1",
                "family_cap": "stored_membership_denominator_fixed_point_10pct_v1",
                "token_limit": "both_orientations_at_most_1024_v1",
                "split": "frozen_anchor_else_seeded_80_10_10_v1",
            },
            "objective_authorized_overrides": [
                {
                    "scope": "deterministic_depth3_v2",
                    "authorization": "queue_4_depth_composed_polarity",
                    "input_quality_tier": "provisional",
                    "input_training_eligible": False,
                    "label_rule": "semantic_negative_hop_count_equals_zero",
                }
            ],
            "counts": {
                "input_candidates": len(candidates),
                "screened_candidates": len(screened),
                "deduplicated_pairs_before_conflict": len(component_seeds) + len(anchor_exclusions),
                "dedup_duplicate_excess": len(screened)
                - len(component_seeds)
                - len(anchor_exclusions),
                "conflict_free_pairs_before_anchor_quarantine": len(merged),
                "lineage_seed_pairs_after_anchor_quarantine": len(component_seeds),
                "anchor_safe_pairs_before_cap": len(anchor_safe),
                "anchor_conflict_component_count": len(
                    {cast(str, row["conflict_component_id"]) for row in anchor_exclusions}
                ),
                "retained_records": len(final_rows),
                "component_count": len(components),
                "active_component_count": len({row.trainer.group_key for row in final_rows}),
                "private_records": sum(
                    cast(bool, row.provenance["private_source_content"]) for row in final_rows
                ),
                "split": dict(sorted(split_counts.items())),
                "label": dict(sorted(label_counts.items())),
                "source_memberships": dict(sorted(source_counts.items())),
                "family_memberships": dict(sorted(family_counts.items())),
                "exclusions": dict(sorted(exclusion_counts.items())),
            },
            "private_source_content": any(
                cast(bool, row.provenance["private_source_content"]) for row in final_rows
            ),
            "redistribution_allowed": all(
                cast(bool, row.provenance["redistribution_allowed"]) for row in final_rows
            ),
            "external_transmission_allowed": all(
                cast(bool, row.provenance["external_transmission_allowed"]) for row in final_rows
            ),
            "release_eligible": all(
                cast(bool, row.provenance["release_eligible"]) for row in final_rows
            ),
            "lexical_canary": {
                "target_met": canary["target_met"],
                "validation_balanced_accuracy": canary["diagnostics"]["validation"][
                    "balanced_accuracy"
                ],
                "test_balanced_accuracy": canary["diagnostics"]["test"]["balanced_accuracy"],
            },
            "outputs": output_bindings,
        }
        (staging / "corpus_v1_manifest.json").write_bytes(_canonical_line(manifest))
        if output_root.exists():
            verify_corpus_v1(
                output_root,
                tokenizer=tokenizer,
                blocklist=blocklist,
            )
            if not _flat_directories_match(staging, output_root):
                raise CorpusV1Error(
                    f"existing corpus-v1 output differs from deterministic replay: {output_root}"
                )
            shutil.rmtree(staging)
            return manifest
        os.replace(staging, output_root)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _load_tokenizer(path: Path) -> _Tokenizer:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - production environment contract
        raise CorpusV1Error(
            "corpus-v1 tokenization requires the local-inference dependency group"
        ) from exc
    return cast(
        _Tokenizer,
        AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            str(path), local_files_only=True, trust_remote_code=False
        ),
    )


def build_production_corpus(config: CorpusV1Config) -> dict[str, Any]:
    """Verify every frozen input, load all sources, and materialize corpus-v1."""

    verify_input_bindings(config)
    candidates = load_production_candidates(config)
    tokenizer = _load_tokenizer(config.tokenizer_dir)
    blocklist = GoldenBlocklist.load(config.inputs["blocklist"].path)
    return materialize_candidates(
        config,
        candidates,
        blocklist=blocklist,
        tokenizer=tokenizer,
    )


def verify_corpus_v1(
    output_root: Path,
    *,
    tokenizer: _Tokenizer | None = None,
    blocklist: GoldenBlocklist | None = None,
    verify_inputs: bool = False,
) -> dict[str, Any]:
    """Replay output hashes, trainer schema, caps, tokens, and ancestry splits."""

    root = output_root.resolve()
    manifest_path = root / "corpus_v1_manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("status") != "completed" or manifest.get("method_version") != METHOD_VERSION:
        raise CorpusV1Error("corpus-v1 manifest is not a completed v1 artifact")
    config_payload = _read_json(root / "run_config.json")
    try:
        config = CorpusV1Config.model_validate(config_payload)
    except ValueError as exc:
        raise CorpusV1Error(f"invalid corpus-v1 run config: {exc}") from exc
    if config.output_root.resolve() != root:
        raise CorpusV1Error("run config output_root differs from the verified directory")
    if manifest.get("config_sha256") != hash_file(root / "run_config.json"):
        raise CorpusV1Error("run config hash differs from the corpus manifest")
    if (
        manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("seed") != config.seed
        or manifest.get("input_sha256")
        != {name: binding.sha256 for name, binding in sorted(config.inputs.items())}
        or manifest.get("tokenizer_sha256")
        != {name: binding.sha256 for name, binding in sorted(config.tokenizer_files.items())}
    ):
        raise CorpusV1Error("manifest config or implementation binding differs")
    if manifest.get("policies") != {
        "pair_deduplication": "unordered_signature_near_dup_hash_pair_v1",
        "conflicting_labels": "quarantine_entire_pair_keep_lineage_seed",
        "frozen_split_anchor_conflicts": (
            "quarantine_entire_multianchor_component_then_rebuild_v1"
        ),
        "ancestry_components": "pre_cap_group_and_statement_union_with_v0_anchors_v1",
        "family_cap": "stored_membership_denominator_fixed_point_10pct_v1",
        "token_limit": "both_orientations_at_most_1024_v1",
        "split": "frozen_anchor_else_seeded_80_10_10_v1",
    } or manifest.get("objective_authorized_overrides") != [
        {
            "scope": "deterministic_depth3_v2",
            "authorization": "queue_4_depth_composed_polarity",
            "input_quality_tier": "provisional",
            "input_training_eligible": False,
            "label_rule": "semantic_negative_hop_count_equals_zero",
        }
    ]:
        raise CorpusV1Error("corpus-v1 policy contract differs")
    if verify_inputs:
        verify_input_bindings(config)
    outputs = cast(Mapping[str, Any], manifest.get("outputs"))
    expected_names = {
        "run_config.json",
        *(f"records_{split}_v1.jsonl" for split in SPLITS),
        "provenance_v1.jsonl",
        "components_v1.jsonl",
        "exclusions_v1.jsonl",
        "lexical_canary.json",
    }
    if set(outputs) != expected_names:
        raise CorpusV1Error("corpus manifest output set differs")
    observed_entries = {path.name: path for path in root.iterdir()}
    if set(observed_entries) != expected_names | {"corpus_v1_manifest.json"} or any(
        not path.is_file() or path.is_symlink() for path in observed_entries.values()
    ):
        raise CorpusV1Error("corpus output root contains missing or unexpected files")
    for name in sorted(expected_names):
        binding = cast(Mapping[str, Any], outputs[name])
        if binding.get("path") != str(root / name) or binding.get("sha256") != hash_file(
            root / name
        ):
            raise CorpusV1Error(f"corpus output binding differs: {name}")

    if tokenizer is None:
        tokenizer = _load_tokenizer(config.tokenizer_dir)
    if blocklist is None:
        block_binding = config.inputs.get("blocklist")
        if block_binding is None:
            raise CorpusV1Error("verification requires a blocklist")
        blocklist = GoldenBlocklist.load(block_binding.path)

    trainers: dict[str, tuple[Literal["train", "validation", "test"], dict[str, Any]]] = {}
    for output_split in SPLITS:
        for payload in _load_training_rows(root / f"records_{output_split}_v1.jsonl"):
            record_id = cast(str, payload["record_id"])
            if record_id in trainers:
                raise CorpusV1Error(f"trainer record crosses output splits: {record_id}")
            trainers[record_id] = (output_split, payload)
    provenance: dict[str, dict[str, Any]] = {}
    for line_number, payload in _iter_jsonl(root / "provenance_v1.jsonl"):
        provenance_record_id = payload.get("record_id")
        if (
            set(payload) != PROVENANCE_FIELDS
            or payload.get("schema_version") != 1
            or not isinstance(provenance_record_id, str)
            or provenance_record_id in provenance
        ):
            raise CorpusV1Error(f"provenance line {line_number} has invalid identity")
        provenance[provenance_record_id] = payload
    if set(trainers) != set(provenance):
        raise CorpusV1Error("trainer/provenance record IDs differ")

    exclusion_counts: Counter[str] = Counter()
    exclusion_ids: set[str] = set()
    exclusion_rows: list[dict[str, Any]] = []
    for line_number, payload in _iter_jsonl(root / "exclusions_v1.jsonl"):
        exclusion_id = payload.get("exclusion_id")
        reason = payload.get("reason")
        identity = dict(payload)
        identity.pop("exclusion_id", None)
        if (
            payload.get("schema_version") != 1
            or not isinstance(exclusion_id, str)
            or exclusion_id in exclusion_ids
            or reason not in EXCLUSION_REASONS
            or exclusion_id != "corpus_v1_exclusion:" + hash_canonical(identity)
            or any(
                field in payload
                for field in (
                    "reference_headless",
                    "candidate_headless",
                    "source_statement",
                    "rewritten_statement",
                )
            )
        ):
            raise CorpusV1Error(f"exclusions line {line_number} is invalid")
        exclusion_ids.add(exclusion_id)
        exclusion_counts[cast(str, reason)] += 1
        exclusion_rows.append(payload)

    components: dict[str, dict[str, Any]] = {}
    group_owner: dict[str, str] = {}
    statement_owner: dict[str, str] = {}
    for line_number, payload in _iter_jsonl(root / "components_v1.jsonl"):
        component_id = payload.get("component_id")
        groups = payload.get("split_group_ids")
        statements = payload.get("statement_near_hashes")
        component_split = payload.get("split")
        anchors = payload.get("split_anchors")
        if (
            set(payload)
            != {
                "schema_version",
                "component_id",
                "split_group_ids",
                "statement_near_hashes",
                "split",
                "split_anchors",
            }
            or payload.get("schema_version") != 1
            or not isinstance(component_id, str)
            or component_id in components
            or not isinstance(groups, list)
            or not groups
            or groups != sorted(set(groups))
            or not all(isinstance(value, str) for value in groups)
            or not isinstance(statements, list)
            or not statements
            or statements != sorted(set(statements))
            or not all(
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
                for value in statements
            )
            or component_split not in SPLITS
            or not isinstance(anchors, list)
            or len(anchors) > 1
            or any(value not in SPLITS for value in anchors)
            or (anchors and anchors[0] != component_split)
        ):
            raise CorpusV1Error(f"components line {line_number} is invalid")
        expected_id = "corpus_v1_component:" + hash_canonical(
            {
                "schema": "corpus_v1_group_statement_component_v1",
                "split_group_ids": groups,
                "statement_near_hashes": statements,
            }
        )
        if component_id != expected_id:
            raise CorpusV1Error(f"component ID differs from its groups: {component_id}")
        expected_split = (
            cast(Literal["train", "validation", "test"], anchors[0])
            if anchors
            else _unanchored_component_split(component_id, config.seed)
        )
        if component_split != expected_split:
            raise CorpusV1Error(
                f"component split differs from deterministic assignment: {component_id}"
            )
        for group in cast(list[str], groups):
            if group in group_owner:
                raise CorpusV1Error(f"ancestry group crosses components: {group}")
            group_owner[group] = component_id
        for statement in cast(list[str], statements):
            if statement in statement_owner:
                raise CorpusV1Error(f"statement identity crosses components: {statement}")
            statement_owner[statement] = component_id
        components[component_id] = payload

    family_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    seen_pair_keys: set[tuple[str, str]] = set()
    active_component_ids: set[str] = set()
    private_count = 0
    all_redistributable = True
    all_external = True
    all_release_eligible = True
    if not trainers:
        raise CorpusV1Error("verified corpus contains no trainer records")
    for record_id in sorted(trainers):
        split, payload = trainers[record_id]
        meta = provenance[record_id]
        component_id = cast(str, payload["group_key"])
        component = components.get(component_id)
        if component is None:
            raise CorpusV1Error(f"trainer row lacks an ancestry component: {record_id}")
        families = meta.get("family_ids")
        split_groups = meta.get("split_group_ids")
        origin_ids = meta.get("origin_ids")
        source_kinds = meta.get("source_kinds")
        provenance_ids = meta.get("provenance_ids")
        pair_id = meta.get("pair_id")
        if (
            meta.get("split") != split
            or component.get("split") != split
            or meta.get("group_key") != component_id
            or meta.get("label") != payload["label"]
            or meta.get("component_group_ids") != component.get("split_group_ids")
            or meta.get("component_statement_near_hashes") != component.get("statement_near_hashes")
            or not isinstance(families, list)
            or not families
            or families != sorted(set(families))
            or not all(isinstance(value, str) for value in families)
            or payload["family"] != "+".join(cast(list[str], families))
            or not isinstance(split_groups, list)
            or not split_groups
            or split_groups != sorted(set(split_groups))
            or not all(isinstance(value, str) for value in split_groups)
            or not set(split_groups).issubset(set(cast(list[str], component["split_group_ids"])))
            or not isinstance(origin_ids, list)
            or not origin_ids
            or origin_ids != sorted(set(origin_ids))
            or not all(isinstance(value, str) for value in origin_ids)
            or not isinstance(source_kinds, list)
            or not source_kinds
            or source_kinds != sorted(set(source_kinds))
            or not all(isinstance(value, str) for value in source_kinds)
            or payload["source"] != "+".join(cast(list[str], source_kinds))
            or payload["weight"] != 1.0
            or not isinstance(provenance_ids, list)
            or not provenance_ids
            or provenance_ids != sorted(set(provenance_ids))
            or not all(isinstance(value, str) for value in provenance_ids)
            or not isinstance(pair_id, str)
        ):
            raise CorpusV1Error(f"trainer/provenance/component join differs: {record_id}")
        reference = cast(str, payload["reference_headless"])
        candidate = cast(str, payload["candidate_headless"])
        reference_hash = signature_near_dup_hash(reference)
        candidate_hash = signature_near_dup_hash(candidate)
        pair_key = sorted((reference_hash, candidate_hash))
        pair_key_tuple = cast(tuple[str, str], tuple(pair_key))
        expected_pair_id = "corpus_v1_pair:" + hash_canonical(
            {"schema": "corpus_v1_unordered_pair_v1", "pair_key": pair_key}
        )
        expected_record_id = "corpus_v1:" + hash_canonical(
            {
                "schema": "corpus_v1_trainer_record_v1",
                "pair_id": expected_pair_id,
                "reference_sha256": _sha_text(reference),
                "candidate_sha256": _sha_text(candidate),
                "label": payload["label"],
                "component_id": component_id,
                "origin_ids": origin_ids,
            }
        )
        if (
            meta.get("pair_key") != pair_key
            or reference_hash == candidate_hash
            or pair_key_tuple in seen_pair_keys
            or pair_id != expected_pair_id
            or record_id != expected_record_id
            or not set(pair_key).issubset(set(cast(list[str], component["statement_near_hashes"])))
            or meta.get("reference_sha256") != _sha_text(reference)
            or meta.get("candidate_sha256") != _sha_text(candidate)
            or reference_hash in blocklist.near_dup_hashes
            or candidate_hash in blocklist.near_dup_hashes
            or any(blocklist.problem_is_blocked(value) for value in cast(list[str], split_groups))
        ):
            raise CorpusV1Error(f"trainer row fails hash/blocklist replay: {record_id}")
        seen_pair_keys.add(pair_key_tuple)
        forward = len(tokenizer.encode(pack_pair(reference, candidate), add_special_tokens=True))
        reverse = len(tokenizer.encode(pack_pair(candidate, reference), add_special_tokens=True))
        if (
            forward > config.max_tokens
            or reverse > config.max_tokens
            or meta.get("forward_tokens") != forward
            or meta.get("reverse_tokens") != reverse
        ):
            raise CorpusV1Error(f"trainer row fails token replay: {record_id}")
        private = meta.get("private_source_content")
        external = meta.get("external_transmission_allowed")
        redistribute = meta.get("redistribution_allowed")
        release = meta.get("release_eligible")
        if (
            not isinstance(private, bool)
            or not isinstance(external, bool)
            or not isinstance(redistribute, bool)
            or not isinstance(release, bool)
            or (private and external)
            or (release and (private or not redistribute))
        ):
            raise CorpusV1Error(f"trainer row has incoherent source policy: {record_id}")
        private_count += int(private)
        family_counts.update(cast(list[str], families))
        source_counts.update(cast(list[str], source_kinds))
        split_counts[split] += 1
        label_counts[str(payload["label"]).lower()] += 1
        active_component_ids.add(component_id)
        all_redistributable = all_redistributable and redistribute
        all_external = all_external and external
        all_release_eligible = all_release_eligible and release
    if any(10 * count > len(trainers) for count in family_counts.values()):
        raise CorpusV1Error("verified trainer records violate the 10% family cap")

    counts = cast(Mapping[str, Any], manifest.get("counts"))
    pair_ids_by_reason: dict[str, set[str]] = defaultdict(set)
    anchor_component_ids: set[str] = set()
    for row in exclusion_rows:
        reason = cast(str, row["reason"])
        if reason in {"conflicting_labels", "split_anchor_component_conflict", "family_cap"}:
            excluded_pair_id = row.get("pair_id")
            if (
                not isinstance(excluded_pair_id, str)
                or excluded_pair_id in pair_ids_by_reason[reason]
            ):
                raise CorpusV1Error(f"exclusion reason repeats or lacks pair identity: {reason}")
            pair_ids_by_reason[reason].add(excluded_pair_id)
        if reason == "split_anchor_component_conflict":
            conflict_component_id = row.get("conflict_component_id")
            if not isinstance(conflict_component_id, str):
                raise CorpusV1Error("anchor-conflict exclusion lacks a component identity")
            anchor_component_ids.add(conflict_component_id)
    screen_exclusion_count = sum(
        exclusion_counts[reason]
        for reason in (
            "golden_blocklist",
            "degenerate_near_identical_sides",
            "overlength",
        )
    )
    deduplicated_count = counts.get("deduplicated_pairs_before_conflict")
    conflict_free_count = counts.get("conflict_free_pairs_before_anchor_quarantine")
    lineage_seed_count = counts.get("lineage_seed_pairs_after_anchor_quarantine")
    anchor_safe_count = counts.get("anchor_safe_pairs_before_cap")
    if not all(
        isinstance(value, int)
        for value in (
            deduplicated_count,
            conflict_free_count,
            lineage_seed_count,
            anchor_safe_count,
        )
    ):
        raise CorpusV1Error("corpus manifest pipeline counts are not integers")
    removed_conflict_free = len(
        pair_ids_by_reason["split_anchor_component_conflict"]
        - pair_ids_by_reason["conflicting_labels"]
    )
    if (
        counts.get("retained_records") != len(trainers)
        or counts.get("component_count") != len(components)
        or counts.get("active_component_count") != len(active_component_ids)
        or counts.get("private_records") != private_count
        or counts.get("split") != dict(sorted(split_counts.items()))
        or counts.get("label") != dict(sorted(label_counts.items()))
        or counts.get("source_memberships") != dict(sorted(source_counts.items()))
        or counts.get("family_memberships") != dict(sorted(family_counts.items()))
        or counts.get("exclusions") != dict(sorted(exclusion_counts.items()))
        or counts.get("input_candidates")
        != counts.get("screened_candidates", -1) + screen_exclusion_count
        or counts.get("dedup_duplicate_excess")
        != counts.get("screened_candidates", -1) - cast(int, deduplicated_count)
        or cast(int, deduplicated_count)
        != cast(int, conflict_free_count) + exclusion_counts["conflicting_labels"]
        or cast(int, lineage_seed_count)
        != cast(int, deduplicated_count) - exclusion_counts["split_anchor_component_conflict"]
        or cast(int, anchor_safe_count) != cast(int, conflict_free_count) - removed_conflict_free
        or len(trainers) != cast(int, anchor_safe_count) - exclusion_counts["family_cap"]
        or counts.get("anchor_conflict_component_count") != len(anchor_component_ids)
        or manifest.get("private_source_content") != (private_count > 0)
        or manifest.get("redistribution_allowed") != all_redistributable
        or manifest.get("external_transmission_allowed") != all_external
        or manifest.get("release_eligible") != all_release_eligible
    ):
        raise CorpusV1Error("corpus manifest counts do not replay")
    canary = _read_json(root / "lexical_canary.json")
    diagnostics = cast(Mapping[str, Any], canary.get("diagnostics"))
    canary_summary = cast(Mapping[str, Any], manifest.get("lexical_canary"))
    if (
        canary.get("schema_version") != 1
        or canary.get("method_version") != "modernbert_token_bow_logistic_canary_v1"
        or canary.get("seed") != config.seed
        or canary.get("training_split") != "train"
        or canary.get("diagnostic_splits") != ["validation", "test"]
        or canary.get("epochs") != config.canary_epochs
        or canary.get("learning_rate") != config.canary_learning_rate
        or canary.get("target_balanced_accuracy_below") != config.canary_target_balanced_accuracy
        or canary.get("training_record_count") != split_counts["train"]
        or canary.get("training_record_ids_sha256")
        != hash_canonical(
            sorted(record_id for record_id, (split, _) in trainers.items() if split == "train")
        )
        or set(diagnostics) != {"validation", "test"}
        or canary_summary.get("target_met") != canary.get("target_met")
        or canary_summary.get("validation_balanced_accuracy")
        != cast(Mapping[str, Any], diagnostics.get("validation")).get("balanced_accuracy")
        or canary_summary.get("test_balanced_accuracy")
        != cast(Mapping[str, Any], diagnostics.get("test")).get("balanced_accuracy")
    ):
        raise CorpusV1Error("lexical canary contract differs")
    replay_balanced_accuracies: dict[str, float] = {}
    for diagnostic_split in ("validation", "test"):
        diagnostic = cast(Mapping[str, Any], diagnostics[diagnostic_split])
        labels = [
            cast(bool, payload["label"])
            for split, payload in trainers.values()
            if split == diagnostic_split
        ]
        positives = sum(labels)
        negatives = len(labels) - positives
        tp = diagnostic.get("true_positive")
        tn = diagnostic.get("true_negative")
        fp = diagnostic.get("false_positive")
        fn = diagnostic.get("false_negative")
        if (
            not all(isinstance(value, int) and value >= 0 for value in (tp, tn, fp, fn))
            or diagnostic.get("record_count") != len(labels)
            or diagnostic.get("positive_count") != positives
            or diagnostic.get("negative_count") != negatives
            or cast(int, tp) + cast(int, fn) != positives
            or cast(int, tn) + cast(int, fp) != negatives
        ):
            raise CorpusV1Error(f"lexical canary counts differ for {diagnostic_split}")
        expected_accuracy = (cast(int, tp) + cast(int, tn)) / len(labels)
        expected_balanced_accuracy = 0.5 * (cast(int, tp) / positives + cast(int, tn) / negatives)
        if (
            diagnostic.get("accuracy") != expected_accuracy
            or diagnostic.get("balanced_accuracy") != expected_balanced_accuracy
        ):
            raise CorpusV1Error(f"lexical canary metrics differ for {diagnostic_split}")
        replay_balanced_accuracies[diagnostic_split] = expected_balanced_accuracy
    if canary.get("target_met") != (
        max(replay_balanced_accuracies.values()) < config.canary_target_balanced_accuracy
    ):
        raise CorpusV1Error("lexical canary target result differs")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    materialize = subparsers.add_parser("materialize", help="build the frozen production corpus")
    materialize.add_argument("--output-root", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="verify one materialized corpus")
    verify.add_argument("--output-root", type=Path, required=True)
    verify.add_argument("--verify-inputs", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    output_root = cast(Path, args.output_root)
    if args.command == "materialize":
        result = build_production_corpus(production_config(output_root))
    else:
        result = verify_corpus_v1(
            output_root,
            verify_inputs=bool(args.verify_inputs),
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
