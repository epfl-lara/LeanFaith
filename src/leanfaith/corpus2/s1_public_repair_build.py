"""Build the capped, public-only S1 repair corpus from frozen artifacts.

The build reuses the completed Meta-engine run; it never invokes Lean.  It
keeps the public projection of corpus v1, admits at most four Meta rewrites per
mathlib declaration, caps the inherited negative-heavy recovered-judge source,
and applies the v2 family/mechanism/template ceilings to new Meta admissions.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.corpus2.build_v1 import (
    PROVENANCE_FIELDS,
    TRAINER_FIELDS,
    CorpusCandidate,
    CorpusV1Error,
    FinalRow,
    MergedPair,
    ScreenedCandidate,
    build_components,
    deduplicate_pairs,
    make_final_rows,
    quarantine_split_anchor_conflicts,
    run_lexical_canary,
    screen_candidates,
)
from leanfaith.corpus2.meta_slice2 import (
    production_config as meta_production_config,
)
from leanfaith.corpus2.meta_slice2 import verify_meta_slice2
from leanfaith.corpus2.s1_public_repair import (
    META_SOURCE_KIND,
    META_SOURCE_REVISION,
    FrozenInput,
    MetaAuditRow,
    MetaCandidateRow,
    RepairCaps,
    S1PublicRepairConfig,
    verify_public_baseline,
    verify_smoke,
)
from leanfaith.corpus2.s1_public_repair import (
    production_config as repair_production_config,
)
from leanfaith.train2.trainer import TrainingRecord

METHOD_VERSION: Literal["s1_public_repair_build_v1"] = "s1_public_repair_build_v1"
DEFAULT_SEED = 20260829
MAX_TOKENS: Literal[1024] = 1024
RECOVERED_SOURCE = "recovered_codex_judged_v1"
D3_SOURCE = "d3_codex_scale_v1"
EXPECTED_D3_ROWS = 146
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_OUTPUT_NAMES = frozenset(
    {
        "run_config.json",
        "records_train_v1.jsonl",
        "records_validation_v1.jsonl",
        "records_test_v1.jsonl",
        "provenance_v1.jsonl",
        "components_v1.jsonl",
        "exclusions_v1.jsonl",
        "cap_memberships_v1.jsonl",
        "selection_summary.json",
        "lexical_canary.json",
    }
)


class S1PublicRepairBuildError(RuntimeError):
    """The public repair selection, materialization, or replay failed closed."""


class _Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...


class S1PublicRepairBuildConfig(BaseModel):
    """Frozen full-build policy and direct input bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_repair_build_v1"] = METHOD_VERSION
    seed: int = Field(default=DEFAULT_SEED, ge=0, strict=True)
    max_tokens: Literal[1024] = MAX_TOKENS
    output_root: Path
    smoke_root: Path
    smoke_manifest: FrozenInput
    tokenizer_dir: Path
    tokenizer_files: dict[str, FrozenInput]
    caps: RepairCaps = Field(default_factory=RepairCaps)
    recovered_source_percent: Literal[20] = 20
    positive_share_min_percent: Literal[40] = 40
    positive_share_max_percent: Literal[60] = 60
    canary_epochs: int = Field(default=6, ge=1, strict=True)
    canary_learning_rate: float = Field(default=0.15, gt=0.0)
    canary_target_balanced_accuracy: float = Field(default=0.72, gt=0.5, lt=1.0)
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        expected_tokenizer = {
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        }
        if set(self.tokenizer_files) != expected_tokenizer:
            raise ValueError("full repair build must bind the exact tokenizer file set")
        if self.smoke_manifest.path != self.smoke_root / "manifest.json":
            raise ValueError("smoke manifest path must be rooted in smoke_root")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("full repair artifacts must be under /storage/milikic")
        return self


@dataclass(frozen=True, slots=True)
class MetaCapMetadata:
    """Selection dimensions retained outside the compact CorpusCandidate schema."""

    origin_id: str
    declaration: str
    family: str
    evidence_class: str
    operation: str
    source_site_hash: str
    candidate_key: tuple[str, str, str, str, str]

    @property
    def template_id(self) -> str:
        return hash_canonical(
            {
                "schema": "s1_public_repair_exact_template_v1",
                "family": self.family,
                "operation": self.operation,
                "source_site_hash": self.source_site_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class RatioMembership:
    rule: str
    member: str
    numerator: int
    denominator: int


def production_config(output_root: Path) -> S1PublicRepairBuildConfig:
    """Return the build contract pinned to the verified one-row smoke."""

    smoke_root = Path(
        "/storage/milikic/leanfaith/corpus2/s1_public_repair_smoke_v1_22386b7_9e2425f"
    )
    tokenizer_dir = Path("/storage/milikic/leanfaith/cpt/modernbert_lean_v1_run1")
    return S1PublicRepairBuildConfig(
        output_root=output_root,
        smoke_root=smoke_root,
        smoke_manifest=FrozenInput(
            path=smoke_root / "manifest.json",
            sha256="32f825b94d77ad578372537dfdc45a10c8a9dfbdeaeb9559ace3ae6687feaf49",
        ),
        tokenizer_dir=tokenizer_dir,
        tokenizer_files={
            "tokenizer.json": FrozenInput(
                path=tokenizer_dir / "tokenizer.json",
                sha256="c7a995f78d60cc3c253902f4b5becfe2f9d0b44f78e6e2f81a343a0cb71789e6",
            ),
            "tokenizer_config.json": FrozenInput(
                path=tokenizer_dir / "tokenizer_config.json",
                sha256="2966a59b9e9cf122279aec1249e22e5bc7ad8430c754e95031b13fd128d4e560",
            ),
            "special_tokens_map.json": FrozenInput(
                path=tokenizer_dir / "special_tokens_map.json",
                sha256="ea97ecdbcc73713039d8d64dbb05e3689495c96657fbd9a18f5bed381be81049",
            ),
        },
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1PublicRepairBuildError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise S1PublicRepairBuildError(f"expected a JSON object: {path}")
    return cast(dict[str, Any], value)


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise S1PublicRepairBuildError(f"{path}:{line_number}: expected a JSON object")
                yield line_number, cast(dict[str, Any], value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1PublicRepairBuildError(f"cannot read JSONL {path}: {exc}") from exc


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _jsonl_bytes(rows: Iterable[object]) -> bytes:
    return b"".join(_canonical_line(row) for row in rows)


def _verify_direct_bindings(config: S1PublicRepairBuildConfig) -> None:
    bindings = {"smoke_manifest": config.smoke_manifest, **config.tokenizer_files}
    for name, binding in sorted(bindings.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise S1PublicRepairBuildError(f"{name} must be a regular non-symlink file")
        observed = hash_file(binding.path)
        if observed != binding.sha256:
            raise S1PublicRepairBuildError(
                f"{name} hash differs: expected {binding.sha256}, observed {observed}"
            )


def _source_config(config: S1PublicRepairBuildConfig) -> S1PublicRepairConfig:
    source = repair_production_config(config.smoke_root)
    if source.caps != config.caps:
        raise S1PublicRepairBuildError("full-build caps differ from the one-row contract")
    return source


def verify_source_artifacts(
    config: S1PublicRepairBuildConfig,
) -> tuple[S1PublicRepairConfig, dict[str, object]]:
    """Replay the smoke, public projection, and Meta attempt/audit tree without Lean."""

    _verify_direct_bindings(config)
    source = _source_config(config)
    verify_smoke(source)
    baseline = verify_public_baseline(source)
    verify_meta_slice2(meta_production_config(source.inputs["meta_manifest"].path.parent))
    return source, baseline


def _load_public_candidates(source: S1PublicRepairConfig) -> list[CorpusCandidate]:
    trainers: dict[str, tuple[str, TrainingRecord]] = {}
    for split in ("train", "validation", "test"):
        path = source.inputs[f"corpus_{split}"].path
        for line_number, row in _iter_jsonl(path):
            if set(row) != TRAINER_FIELDS:
                raise S1PublicRepairBuildError(f"{path}:{line_number}: trainer fields differ")
            try:
                record = TrainingRecord.model_validate(row)
            except ValidationError as exc:
                raise S1PublicRepairBuildError(
                    f"{path}:{line_number}: invalid trainer record: {exc}"
                ) from exc
            if record.record_id in trainers:
                raise S1PublicRepairBuildError(f"duplicate trainer ID {record.record_id}")
            trainers[record.record_id] = (split, record)

    candidates: list[CorpusCandidate] = []
    path = source.inputs["corpus_provenance"].path
    for line_number, row in _iter_jsonl(path):
        if set(row) != PROVENANCE_FIELDS:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: provenance fields differ")
        if row["private_source_content"] is True:
            continue
        record_id = cast(str, row["record_id"])
        joined = trainers.get(record_id)
        if joined is None:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: trainer join is missing")
        split, record = joined
        if row["split"] != split or row["label"] is not record.label:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: trainer join differs")
        groups = tuple(sorted(cast(list[str], row["split_group_ids"])))
        families = tuple(sorted(cast(list[str], row["family_ids"])))
        provenance_ids = tuple(sorted(cast(list[str], row["provenance_ids"])))
        source_kinds = sorted(cast(list[str], row["source_kinds"]))
        for source_kind in source_kinds:
            origin_id = "s1_public_base:" + hash_canonical(
                {
                    "schema": "s1_public_base_origin_v1",
                    "record_id": record_id,
                    "source_kind": source_kind,
                }
            )
            candidates.append(
                CorpusCandidate(
                    origin_id=origin_id,
                    source_kind=source_kind,
                    reference_headless=record.reference_headless,
                    candidate_headless=record.candidate_headless,
                    label=record.label,
                    split_group_ids=groups,
                    family_ids=families,
                    provenance_ids=tuple(sorted({record_id, *provenance_ids})),
                    split_anchor=cast(Literal["train", "validation", "test"], split),
                    private_source_content=False,
                    redistribution_allowed=True,
                    external_transmission_allowed=True,
                    release_eligible=True,
                )
            )
    return candidates


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


def _load_meta_candidates(
    source: S1PublicRepairConfig,
) -> tuple[list[CorpusCandidate], dict[str, MetaCapMetadata]]:
    audits: dict[tuple[str, str, str, str, str], MetaAuditRow] = {}
    audit_path = source.inputs["meta_audits"].path
    for line_number, raw in _iter_jsonl(audit_path):
        try:
            audit = MetaAuditRow.model_validate(raw)
        except ValidationError as exc:
            raise S1PublicRepairBuildError(
                f"{audit_path}:{line_number}: invalid Meta audit: {exc}"
            ) from exc
        key = _audit_key(audit)
        if key in audits:
            raise S1PublicRepairBuildError("Meta audit keys are not unique")
        audits[key] = audit

    candidates: list[CorpusCandidate] = []
    metadata: dict[str, MetaCapMetadata] = {}
    observed_keys: set[tuple[str, str, str, str, str]] = set()
    candidate_path = source.inputs["meta_candidates"].path
    for line_number, raw in _iter_jsonl(candidate_path):
        if raw.get("recordKind") != "candidate":
            continue
        try:
            candidate = MetaCandidateRow.model_validate(raw)
        except ValidationError as exc:
            raise S1PublicRepairBuildError(
                f"{candidate_path}:{line_number}: invalid Meta candidate: {exc}"
            ) from exc
        key = _candidate_key(candidate)
        if key in observed_keys or key not in audits:
            raise S1PublicRepairBuildError("Meta candidate/audit key join is not one-to-one")
        observed_keys.add(key)
        source_site_hash = raw.get("sourceSiteHash")
        if not isinstance(source_site_hash, str) or _HEX64.fullmatch(source_site_hash) is None:
            raise S1PublicRepairBuildError("Meta candidate lacks a valid source-site hash")
        key_digest = hash_canonical(
            {"schema": "s1_public_repair_meta_key_v1", "candidate_key": list(key)}
        )
        origin_id = "meta_slice2_candidate:" + key_digest
        ancestry_id = "mathlib-declaration:" + hash_canonical(
            {
                "schema": "mathlib_declaration_ancestry_v1",
                "revision": META_SOURCE_REVISION,
                "declaration": candidate.declaration,
            }
        )
        primary_id = "meta_slice2_primary:" + hash_canonical(
            candidate.model_dump(mode="json", by_alias=True)
        )
        audit_id = "meta_slice2_audit:" + hash_canonical(
            audits[key].model_dump(mode="json", by_alias=True)
        )
        candidates.append(
            CorpusCandidate(
                origin_id=origin_id,
                source_kind=META_SOURCE_KIND,
                reference_headless=candidate.source,
                candidate_headless=candidate.candidate,
                label=True,
                split_group_ids=(ancestry_id,),
                family_ids=(candidate.family,),
                provenance_ids=tuple(sorted((primary_id, audit_id))),
                split_anchor=None,
                private_source_content=False,
                redistribution_allowed=True,
                external_transmission_allowed=False,
                release_eligible=True,
            )
        )
        metadata[origin_id] = MetaCapMetadata(
            origin_id=origin_id,
            declaration=candidate.declaration,
            family=candidate.family,
            evidence_class=candidate.evidence_class,
            operation=candidate.operation,
            source_site_hash=source_site_hash,
            candidate_key=key,
        )
    if observed_keys != set(audits) or len(candidates) != source.meta_pool.candidates:
        raise S1PublicRepairBuildError("full Meta candidate/audit sets differ")
    return candidates, metadata


def _exclusion(reason: str, payload: Mapping[str, object]) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "reason": reason,
        **dict(payload),
    }
    value["exclusion_id"] = "s1_public_repair_exclusion:" + hash_canonical(value)
    return value


def apply_meta_ancestry_cap(
    rows: Sequence[ScreenedCandidate],
    *,
    metadata: Mapping[str, MetaCapMetadata],
    seed: int,
    limit: int,
) -> tuple[list[ScreenedCandidate], list[dict[str, object]]]:
    """Keep at most ``limit`` screened direct Meta rewrites per declaration."""

    by_declaration: dict[str, list[ScreenedCandidate]] = defaultdict(list)
    retained: list[ScreenedCandidate] = []
    for row in rows:
        meta = metadata.get(row.candidate.origin_id)
        if meta is None:
            retained.append(row)
        else:
            by_declaration[meta.declaration].append(row)
    exclusions: list[dict[str, object]] = []
    for declaration, members in sorted(by_declaration.items()):
        ranked = sorted(
            members,
            key=lambda row: (
                hash_canonical(
                    {
                        "schema": "s1_public_repair_meta_ancestry_rank_v1",
                        "seed": seed,
                        "declaration": declaration,
                        "candidate_key": list(metadata[row.candidate.origin_id].candidate_key),
                    }
                ),
                row.candidate.origin_id,
            ),
        )
        retained.extend(ranked[:limit])
        for rank, row in enumerate(ranked[limit:], start=limit):
            exclusions.append(
                _exclusion(
                    "meta_source_ancestry_cap",
                    {
                        "origin_id": row.candidate.origin_id,
                        "declaration": declaration,
                        "limit": limit,
                        "rank": rank,
                    },
                )
            )
    return sorted(retained, key=lambda row: row.candidate.origin_id), exclusions


def _meta_for_row(
    row: MergedPair, metadata: Mapping[str, MetaCapMetadata]
) -> tuple[MetaCapMetadata, ...]:
    return tuple(
        sorted(
            (metadata[origin_id] for origin_id in row.origin_ids if origin_id in metadata),
            key=lambda item: item.origin_id,
        )
    )


def ratio_memberships_for_row(
    row: MergedPair,
    *,
    metadata: Mapping[str, MetaCapMetadata],
    config: S1PublicRepairBuildConfig,
) -> tuple[RatioMembership, ...]:
    """Return unique denominator-based cap memberships for one merged pair."""

    memberships: set[RatioMembership] = set()
    if RECOVERED_SOURCE in row.source_kinds and D3_SOURCE not in row.source_kinds:
        memberships.add(
            RatioMembership(
                rule="recovered_source",
                member=RECOVERED_SOURCE,
                numerator=config.recovered_source_percent,
                denominator=100,
            )
        )
    for meta in _meta_for_row(row, metadata):
        memberships.add(
            RatioMembership(
                rule="meta_family",
                member=meta.family,
                numerator=config.caps.family_percent,
                denominator=100,
            )
        )
        memberships.add(
            RatioMembership(
                rule="meta_mechanism",
                member=meta.evidence_class,
                numerator=config.caps.mechanism_superclass_percent,
                denominator=100,
            )
        )
        memberships.add(
            RatioMembership(
                rule="meta_exact_template",
                member=meta.template_id,
                numerator=config.caps.exact_template_percent,
                denominator=100,
            )
        )
        if meta.family == "P20":
            memberships.add(
                RatioMembership(
                    rule="meta_exact_rewrite_lemma",
                    member=meta.operation,
                    numerator=config.caps.exact_rewrite_lemma_per_mille,
                    denominator=1000,
                )
            )
    return tuple(sorted(memberships, key=lambda item: (item.rule, item.member)))


def apply_ratio_caps(
    rows: Sequence[MergedPair],
    *,
    metadata: Mapping[str, MetaCapMetadata],
    config: S1PublicRepairBuildConfig,
) -> tuple[list[MergedPair], list[dict[str, object]]]:
    """Reach a deterministic fixed point across source and new-Meta ratio caps."""

    selected = {row.pair_id: row for row in rows}
    exclusions: list[dict[str, object]] = []
    round_index = 0
    while selected:
        count = len(selected)
        by_membership: dict[RatioMembership, list[MergedPair]] = defaultdict(list)
        for row in selected.values():
            for membership in ratio_memberships_for_row(row, metadata=metadata, config=config):
                by_membership[membership].append(row)
        violations: list[tuple[int, int, RatioMembership]] = []
        for membership, members in by_membership.items():
            member_count = len(members)
            excess = membership.denominator * member_count - membership.numerator * count
            if excess > 0:
                violations.append((excess, member_count, membership))
        if not violations:
            break
        _, member_count, membership = sorted(
            violations,
            key=lambda item: (
                -item[0],
                -item[1],
                item[2].rule,
                item[2].member,
            ),
        )[0]
        excess = membership.denominator * member_count - membership.numerator * count
        drop_count = math.ceil(excess / (membership.denominator - membership.numerator))
        members = sorted(
            by_membership[membership],
            key=lambda row: (
                hash_canonical(
                    {
                        "schema": "s1_public_repair_ratio_cap_rank_v1",
                        "seed": config.seed,
                        "rule": membership.rule,
                        "member": membership.member,
                        "pair_key": list(row.pair_key),
                    }
                ),
                row.pair_id,
            ),
        )
        dropped = members[-drop_count:]
        if not dropped:
            raise S1PublicRepairBuildError("ratio-cap fixed point made no progress")
        for rank, row in enumerate(members):
            if row not in dropped:
                continue
            exclusions.append(
                _exclusion(
                    "repair_ratio_cap",
                    {
                        "pair_id": row.pair_id,
                        "pair_key": list(row.pair_key),
                        "origin_ids": list(row.origin_ids),
                        "rule": membership.rule,
                        "member": membership.member,
                        "numerator": membership.numerator,
                        "denominator": membership.denominator,
                        "round_index": round_index,
                        "member_count_before": member_count,
                        "corpus_count_before": count,
                        "member_rank": rank,
                    },
                )
            )
            del selected[row.pair_id]
        round_index += 1
    final = sorted(selected.values(), key=lambda row: row.pair_id)
    _verify_ratio_caps(final, metadata=metadata, config=config)
    return final, exclusions


def _verify_ratio_caps(
    rows: Sequence[MergedPair],
    *,
    metadata: Mapping[str, MetaCapMetadata],
    config: S1PublicRepairBuildConfig,
) -> None:
    counts: Counter[RatioMembership] = Counter()
    for row in rows:
        counts.update(ratio_memberships_for_row(row, metadata=metadata, config=config))
    for membership, count in counts.items():
        if membership.denominator * count > membership.numerator * len(rows):
            raise S1PublicRepairBuildError(
                f"ratio cap failed: {membership.rule}/{membership.member}"
            )


def _cap_membership_rows(
    final_rows: Sequence[FinalRow],
    *,
    merged_by_pair: Mapping[str, MergedPair],
    metadata: Mapping[str, MetaCapMetadata],
    config: S1PublicRepairBuildConfig,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for final in final_rows:
        pair_id = cast(str, final.provenance["pair_id"])
        merged = merged_by_pair[pair_id]
        meta_rows = _meta_for_row(merged, metadata)
        output.append(
            {
                "schema_version": 1,
                "record_id": final.trainer.record_id,
                "pair_id": pair_id,
                "meta_declarations": sorted({item.declaration for item in meta_rows}),
                "ratio_memberships": [
                    {
                        "rule": item.rule,
                        "member": item.member,
                        "numerator": item.numerator,
                        "denominator": item.denominator,
                    }
                    for item in ratio_memberships_for_row(merged, metadata=metadata, config=config)
                ],
            }
        )
    return sorted(output, key=lambda row: cast(str, row["record_id"]))


def _load_tokenizer(path: Path) -> _Tokenizer:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover
        raise S1PublicRepairBuildError(
            "repair build requires local-inference dependencies"
        ) from exc
    return cast(
        _Tokenizer,
        AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            str(path), local_files_only=True, trust_remote_code=False
        ),
    )


def _selection_summary(
    *,
    public_candidates: int,
    meta_candidates: int,
    screened: int,
    ancestry_retained: int,
    deduplicated: int,
    anchor_safe: int,
    final_rows: Sequence[FinalRow],
    exclusions: Sequence[Mapping[str, object]],
    canary: Mapping[str, object],
    config: S1PublicRepairBuildConfig,
) -> dict[str, object]:
    labels = Counter(row.trainer.label for row in final_rows)
    splits = Counter(row.split for row in final_rows)
    sources = Counter(
        source for row in final_rows for source in cast(list[str], row.provenance["source_kinds"])
    )
    families = Counter(
        family for row in final_rows for family in cast(list[str], row.provenance["family_ids"])
    )
    exclusion_counts = Counter(cast(str, row["reason"]) for row in exclusions)
    total = len(final_rows)
    positive_share = labels[True] / total
    balance_passed = (
        config.positive_share_min_percent * total
        <= 100 * labels[True]
        <= config.positive_share_max_percent * total
    )
    d3_retained = sources[D3_SOURCE]
    if d3_retained != EXPECTED_D3_ROWS:
        raise S1PublicRepairBuildError(
            f"D-3 retention differs: expected {EXPECTED_D3_ROWS}, observed {d3_retained}"
        )
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "stages": {
            "public_candidate_memberships": public_candidates,
            "meta_candidates": meta_candidates,
            "screened_candidate_memberships": screened,
            "after_meta_ancestry_cap": ancestry_retained,
            "deduplicated_pairs": deduplicated,
            "anchor_safe_pairs": anchor_safe,
            "retained_records": total,
        },
        "retained": {
            "label": {"false": labels[False], "true": labels[True]},
            "positive_share": positive_share,
            "split": dict(sorted(splits.items())),
            "source_memberships": dict(sorted(sources.items())),
            "family_memberships": dict(sorted(families.items())),
            "d3_rows": d3_retained,
            "private_records": sum(
                cast(bool, row.provenance["private_source_content"]) for row in final_rows
            ),
        },
        "exclusions": dict(sorted(exclusion_counts.items())),
        "gates": {
            "positive_share": {
                "minimum": config.positive_share_min_percent / 100,
                "maximum": config.positive_share_max_percent / 100,
                "observed": positive_share,
                "passed": balance_passed,
            },
            "d3_retention": {
                "expected": EXPECTED_D3_ROWS,
                "observed": d3_retained,
                "passed": True,
            },
            "diversity_caps": {"passed": True},
            "lexical_canary": {
                "target_balanced_accuracy_below": config.canary_target_balanced_accuracy,
                "passed": canary["target_met"],
            },
        },
        "training_gate_passed": balance_passed and cast(bool, canary["target_met"]),
    }


def assemble_repair(
    config: S1PublicRepairBuildConfig,
    *,
    tokenizer: _Tokenizer | None = None,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Replay source validation, selection, caps, splits, and canary in memory."""

    source, baseline = verify_source_artifacts(config)
    public_candidates = _load_public_candidates(source)
    meta_candidates, metadata = _load_meta_candidates(source)
    if tokenizer is None:
        tokenizer = _load_tokenizer(config.tokenizer_dir)
    blocklist = GoldenBlocklist.load(source.inputs["golden_blocklist"].path)
    try:
        screened, screen_exclusions = screen_candidates(
            [*public_candidates, *meta_candidates],
            blocklist=blocklist,
            tokenizer=tokenizer,
            max_tokens=config.max_tokens,
        )
        ancestry_safe, ancestry_exclusions = apply_meta_ancestry_cap(
            screened,
            metadata=metadata,
            seed=config.seed,
            limit=config.caps.direct_per_source_ancestry,
        )
        merged, component_seeds, conflict_exclusions = deduplicate_pairs(ancestry_safe)
        anchor_safe, component_seeds, anchor_exclusions = quarantine_split_anchor_conflicts(
            merged, component_seeds
        )
        item_components, components = build_components(component_seeds, seed=config.seed)
        capped, ratio_exclusions = apply_ratio_caps(
            anchor_safe,
            metadata=metadata,
            config=config,
        )
        final_rows = make_final_rows(
            capped,
            item_components=item_components,
            components=components,
        )
        canary = run_lexical_canary(
            final_rows,
            tokenizer=tokenizer,
            seed=config.seed,
            epochs=config.canary_epochs,
            learning_rate=config.canary_learning_rate,
            target=config.canary_target_balanced_accuracy,
        )
    except CorpusV1Error as exc:
        raise S1PublicRepairBuildError(str(exc)) from exc
    exclusions = sorted(
        [
            *screen_exclusions,
            *ancestry_exclusions,
            *conflict_exclusions,
            *anchor_exclusions,
            *ratio_exclusions,
        ],
        key=lambda row: cast(str, row["exclusion_id"]),
    )
    summary = _selection_summary(
        public_candidates=len(public_candidates),
        meta_candidates=len(meta_candidates),
        screened=len(screened),
        ancestry_retained=len(ancestry_safe),
        deduplicated=len(merged),
        anchor_safe=len(anchor_safe),
        final_rows=final_rows,
        exclusions=exclusions,
        canary=canary,
        config=config,
    )
    merged_by_pair = {row.pair_id: row for row in capped}
    cap_rows = _cap_membership_rows(
        final_rows,
        merged_by_pair=merged_by_pair,
        metadata=metadata,
        config=config,
    )
    payloads: dict[str, bytes] = {
        "run_config.json": _canonical_line(config.model_dump(mode="json")),
        "provenance_v1.jsonl": _jsonl_bytes(row.provenance for row in final_rows),
        "components_v1.jsonl": _jsonl_bytes(
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
        "exclusions_v1.jsonl": _jsonl_bytes(exclusions),
        "cap_memberships_v1.jsonl": _jsonl_bytes(cap_rows),
        "selection_summary.json": _canonical_line(summary),
        "lexical_canary.json": _canonical_line(canary),
    }
    for split in ("train", "validation", "test"):
        payloads[f"records_{split}_v1.jsonl"] = _jsonl_bytes(
            row.trainer.model_dump(mode="json") for row in final_rows if row.split == split
        )
    output_bindings = {
        name: {
            "path": str(config.output_root / name),
            "sha256": hash_canonical_bytes(payload),
        }
        for name, payload in sorted(payloads.items())
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "completed",
        "config_sha256": hash_canonical(config.model_dump(mode="json")),
        "implementation_module_sha256": hash_file(Path(__file__)),
        "smoke_manifest_sha256": config.smoke_manifest.sha256,
        "source_inputs": {
            name: {"path": str(binding.path), "sha256": binding.sha256}
            for name, binding in sorted(source.inputs.items())
        },
        "tokenizer_sha256": {
            name: binding.sha256 for name, binding in sorted(config.tokenizer_files.items())
        },
        "public_baseline": baseline,
        "selection_summary": summary,
        "outputs": output_bindings,
        "privacy": {
            "public_only": True,
            "private_source_content": False,
            "redistribution_allowed": True,
            "external_transmission": False,
            "release_eligible": True,
        },
        "execution": {
            "lean_reexecution": False,
            "external_calls": False,
            "final_test_accessed": False,
        },
    }
    payloads["manifest.json"] = _canonical_line(manifest)
    return payloads, manifest


def hash_canonical_bytes(payload: bytes) -> str:
    """Hash exact already-canonical artifact bytes."""

    import hashlib

    return hashlib.sha256(payload).hexdigest()


def materialize_repair(config: S1PublicRepairBuildConfig) -> dict[str, object]:
    """Atomically materialize, or verify, the full public repair corpus."""

    if config.output_root.exists():
        return verify_repair(config)
    payloads, manifest = assemble_repair(config)
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
    verified = verify_repair(config)
    if verified != manifest:
        raise S1PublicRepairBuildError("materialized manifest differs from verification")
    return verified


def _verify_cap_memberships(
    path: Path,
    *,
    record_ids: set[str],
) -> None:
    observed: set[str] = set()
    ratio_counts: Counter[RatioMembership] = Counter()
    declaration_counts: Counter[str] = Counter()
    for line_number, row in _iter_jsonl(path):
        if set(row) != {
            "schema_version",
            "record_id",
            "pair_id",
            "meta_declarations",
            "ratio_memberships",
        }:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: cap fields differ")
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or record_id in observed:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: cap record ID differs")
        observed.add(record_id)
        declarations = row.get("meta_declarations")
        memberships = row.get("ratio_memberships")
        if not isinstance(declarations, list) or not all(
            isinstance(value, str) for value in declarations
        ):
            raise S1PublicRepairBuildError(f"{path}:{line_number}: declarations differ")
        declaration_counts.update(cast(list[str], declarations))
        if not isinstance(memberships, list):
            raise S1PublicRepairBuildError(f"{path}:{line_number}: ratio memberships differ")
        for raw in memberships:
            if not isinstance(raw, dict):
                raise S1PublicRepairBuildError(f"{path}:{line_number}: ratio membership differs")
            try:
                membership = RatioMembership(
                    rule=cast(str, raw["rule"]),
                    member=cast(str, raw["member"]),
                    numerator=cast(int, raw["numerator"]),
                    denominator=cast(int, raw["denominator"]),
                )
            except (KeyError, TypeError) as exc:
                raise S1PublicRepairBuildError(
                    f"{path}:{line_number}: ratio membership differs"
                ) from exc
            ratio_counts[membership] += 1
    if observed != record_ids:
        raise S1PublicRepairBuildError("cap membership record set differs from trainers")
    if any(count > 4 for count in declaration_counts.values()):
        raise S1PublicRepairBuildError("Meta declaration ancestry cap differs")
    total = len(record_ids)
    for membership, count in ratio_counts.items():
        if membership.denominator * count > membership.numerator * total:
            raise S1PublicRepairBuildError(
                f"stored ratio cap failed: {membership.rule}/{membership.member}"
            )


def verify_repair(config: S1PublicRepairBuildConfig) -> dict[str, object]:
    """Verify source bindings, output hashes, schemas, splits, caps, and gates."""

    source, baseline = verify_source_artifacts(config)
    root = config.output_root
    if root.is_symlink() or not root.is_dir():
        raise S1PublicRepairBuildError("repair output root must be a non-symlink directory")
    observed_files = {path.name: path for path in root.iterdir()}
    if set(observed_files) != _OUTPUT_NAMES | {"manifest.json"} or any(
        path.is_symlink() or not path.is_file() for path in observed_files.values()
    ):
        raise S1PublicRepairBuildError("repair output file set differs")
    manifest = _read_json(root / "manifest.json")
    if manifest.get("status") != "completed" or manifest.get("method_version") != METHOD_VERSION:
        raise S1PublicRepairBuildError("repair manifest is not completed v1")
    if (
        manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("smoke_manifest_sha256") != config.smoke_manifest.sha256
        or manifest.get("public_baseline") != baseline
    ):
        raise S1PublicRepairBuildError("repair manifest bindings differ")
    expected_source_inputs = {
        name: {"path": str(binding.path), "sha256": binding.sha256}
        for name, binding in sorted(source.inputs.items())
    }
    if manifest.get("source_inputs") != expected_source_inputs:
        raise S1PublicRepairBuildError("repair source input bindings differ")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _OUTPUT_NAMES:
        raise S1PublicRepairBuildError("repair output inventory differs")
    for name in sorted(_OUTPUT_NAMES):
        binding = outputs[name]
        if not isinstance(binding, Mapping) or binding.get("path") != str(root / name):
            raise S1PublicRepairBuildError(f"repair output path differs: {name}")
        if binding.get("sha256") != hash_file(root / name):
            raise S1PublicRepairBuildError(f"repair output hash differs: {name}")

    trainers: dict[str, tuple[str, TrainingRecord]] = {}
    for split in ("train", "validation", "test"):
        path = root / f"records_{split}_v1.jsonl"
        for line_number, row in _iter_jsonl(path):
            if set(row) != TRAINER_FIELDS:
                raise S1PublicRepairBuildError(f"{path}:{line_number}: trainer fields differ")
            try:
                trainer = TrainingRecord.model_validate(row)
            except ValidationError as exc:
                raise S1PublicRepairBuildError(
                    f"{path}:{line_number}: invalid trainer row: {exc}"
                ) from exc
            if trainer.record_id in trainers:
                raise S1PublicRepairBuildError("trainer record crosses splits")
            trainers[trainer.record_id] = (split, trainer)

    provenance_ids: set[str] = set()
    group_splits: dict[str, str] = {}
    statement_splits: dict[str, str] = {}
    source_counts: Counter[str] = Counter()
    labels: Counter[bool] = Counter()
    path = root / "provenance_v1.jsonl"
    for line_number, row in _iter_jsonl(path):
        if set(row) != PROVENANCE_FIELDS:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: provenance fields differ")
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or record_id in provenance_ids:
            raise S1PublicRepairBuildError(f"{path}:{line_number}: provenance ID differs")
        provenance_ids.add(record_id)
        joined = trainers.get(record_id)
        if (
            joined is None
            or row.get("split") != joined[0]
            or row.get("label") is not joined[1].label
        ):
            raise S1PublicRepairBuildError(f"{path}:{line_number}: provenance join differs")
        if (
            row.get("private_source_content") is not False
            or row.get("redistribution_allowed") is not True
            or row.get("release_eligible") is not True
        ):
            raise S1PublicRepairBuildError(f"{path}:{line_number}: public policy differs")
        split = joined[0]
        for group in cast(list[str], row["component_group_ids"]):
            if group in group_splits and group_splits[group] != split:
                raise S1PublicRepairBuildError("ancestry group crosses output splits")
            group_splits[group] = split
        for statement in cast(list[str], row["component_statement_near_hashes"]):
            if statement in statement_splits and statement_splits[statement] != split:
                raise S1PublicRepairBuildError("statement identity crosses output splits")
            statement_splits[statement] = split
        source_counts.update(cast(list[str], row["source_kinds"]))
        labels[joined[1].label] += 1
    if provenance_ids != set(trainers):
        raise S1PublicRepairBuildError("trainer/provenance record sets differ")
    if source_counts[D3_SOURCE] != EXPECTED_D3_ROWS:
        raise S1PublicRepairBuildError("D-3 rows were not all retained")
    total = len(trainers)
    if not (
        config.positive_share_min_percent * total
        <= 100 * labels[True]
        <= config.positive_share_max_percent * total
    ):
        raise S1PublicRepairBuildError("repair label-balance gate failed")
    _verify_cap_memberships(root / "cap_memberships_v1.jsonl", record_ids=set(trainers))
    summary = _read_json(root / "selection_summary.json")
    if manifest.get("selection_summary") != summary:
        raise S1PublicRepairBuildError("selection summary differs from manifest")
    execution = manifest.get("execution")
    if execution != {
        "lean_reexecution": False,
        "external_calls": False,
        "final_test_accessed": False,
    }:
        raise S1PublicRepairBuildError("repair execution boundary differs")
    return cast(dict[str, object], manifest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = production_config(cast(Path, args.output_root))
    manifest = materialize_repair(config) if args.command == "build" else verify_repair(config)
    summary = cast(Mapping[str, object], manifest["selection_summary"])
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
