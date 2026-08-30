"""Check fixed-sample implication-skeleton feasibility before fitting canaries.

This additive v3 precheck reuses the exact frozen 72/12/12 declaration sample,
runs the implication-aware Lean engine once, applies the unchanged public-only
screening boundaries to every typed candidate, and searches for one exact
24-row subset satisfying the preregistered split, family, and operation caps.
It does not launch an independent audit, fit a canary, access ``final_test``,
increase the sample, or authorize training.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

import leanfaith.corpus2.s1_public_negative_skeleton_implication_smoke as implication_smoke
import leanfaith.corpus2.s1_public_negative_skeleton_pilot as v1
import leanfaith.corpus2.s1_public_negative_skeleton_pilot_v2 as v2
from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.eval.m1_runtime import pack_pair
from leanfaith.representations.views import signature_near_dup_hash

METHOD_VERSION: Literal["s1_public_negative_skeleton_feasibility_v3"] = (
    "s1_public_negative_skeleton_feasibility_v3"
)
SOURCE_REVISION = v1.SOURCE_REVISION
EXPECTED_LAKE_VERSION = v1.EXPECTED_LAKE_VERSION
SELECTION_DOMAIN = v1.SELECTION_DOMAIN
SELECTION_QUOTAS = dict(v1.SELECTION_QUOTAS)
TARGET_TOTAL: Literal[24] = 24
MIN_DIAGNOSTIC_YIELD: Literal[4] = 4
MIN_N22_SHARE = v1.MIN_N22_SHARE
MAX_N21_SHARE = v1.MAX_N21_SHARE
MAX_OPERATION_SHARE = v1.MAX_OPERATION_SHARE
MIN_N22_COUNT = math.ceil(TARGET_TOTAL * MIN_N22_SHARE)
MAX_N21_COUNT = math.floor(TARGET_TOTAL * MAX_N21_SHARE)
MAX_OPERATION_COUNT = math.floor(TARGET_TOTAL * MAX_OPERATION_SHARE)
SOLVER_STATE_LIMIT: Literal[1_000_000] = 1_000_000

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_V2_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean"
_ENGINE_V3_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV3.lean"
_BASE_PILOT_V2_MODULE = Path(v2.__file__).resolve()
_BASE_IMPLICATION_SMOKE_MODULE = Path(implication_smoke.__file__).resolve()
_PILOT_V2_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/s1_public_negative_skeleton_pilot_v2_3d72e99_d568c8c"
)
_IMPLICATION_SMOKE_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/"
    "s1_public_negative_skeleton_implication_smoke_v1_4fd2c6a_d568c8c"
)
_PILOT_V2_MANIFEST_SHA256 = "4fd2c6a769d28d24322f7cedbfc5a2a01ef9edec5e2686eed74add1b914dbe44"
_PILOT_V2_SELECTION_SHA256 = "fd02051abc4902efee3238017052fafb717aaa5c285ab0d9e28010b1838d0809"
_IMPLICATION_SMOKE_MANIFEST_SHA256 = (
    "6d1de2b05fa7c8ab3e3f4fe043ad3d9a684c07fd53c3b78d14b1dcb25aa55200"
)
_INPUT_NAMES = (frozenset(v1._PRODUCTION_INPUTS) - {"negative_engine"}) | {
    "negative_engine_v2",
    "negative_engine_v3",
    "base_pilot_v2_module",
    "base_implication_smoke_module",
    "pilot_v2_manifest",
    "pilot_v2_selection",
    "implication_smoke_manifest",
}
_STATIC_OUTPUTS = frozenset(
    {
        "selection.jsonl",
        "declaration_names.txt",
        "primary_driver.lean",
        "primary.stdout.jsonl",
        "primary.stderr.txt",
        "primary.process.json",
        "candidate_pool.jsonl",
        "screened_candidates.jsonl",
        "exclusions.jsonl",
        "feasible_selection.jsonl",
        "summary.json",
    }
)
_OUTPUTS = _STATIC_OUTPUTS | {"manifest.json"}


class NegativeSkeletonFeasibilityError(RuntimeError):
    """A frozen input, engine row, screen, solver, or artifact replay differed."""


class FeasibilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_skeleton_feasibility_v3"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    tokenizer_root: Path
    inputs: dict[str, v1.FrozenInput]
    selection_quotas: dict[str, int] = Field(default_factory=lambda: dict(SELECTION_QUOTAS))
    target_total: Literal[24] = TARGET_TOTAL
    min_diagnostic_yield: Literal[4] = MIN_DIAGNOSTIC_YIELD
    min_n22_share: float = MIN_N22_SHARE
    max_n21_share: float = MAX_N21_SHARE
    max_operation_share: float = MAX_OPERATION_SHARE
    solver_state_limit: Literal[1_000_000] = SOLVER_STATE_LIMIT
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    expected_lake_version: str = EXPECTED_LAKE_VERSION
    mathlib_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = SOURCE_REVISION
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if set(self.inputs) != _INPUT_NAMES:
            raise ValueError("v3 feasibility must bind the exact frozen input set")
        if self.selection_quotas != SELECTION_QUOTAS:
            raise ValueError("v3 feasibility split quotas differ")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("v3 feasibility artifacts must be under /storage/milikic")
        return self


class EngineCandidateV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[3] = Field(alias="schemaVersion")
    kind: Literal["candidate"]
    record_kind: Literal["candidate"] = Field(alias="recordKind")
    status: Literal["ok"]
    declaration: str = Field(min_length=1)
    family: Literal["N21", "N22"]
    operation: str = Field(min_length=1)
    operation_kind: str = Field(alias="operationKind", min_length=1)
    site_path: str = Field(alias="sitePath", pattern=r"^/root-body(?:/(?:left|right|not))*$")
    source: str = Field(min_length=1)
    candidate: str = Field(min_length=1)
    source_type_hash: str = Field(alias="sourceTypeHash", pattern=r"^[0-9a-f]{64}$")
    candidate_type_hash: str = Field(alias="candidateTypeHash", pattern=r"^[0-9a-f]{64}$")
    evidence_class: Literal["N-SEP"] = Field(alias="evidenceClass")
    evidence: dict[str, object]
    witness: dict[str, object]
    candidate_elaborates: StrictBool = Field(alias="candidateElaborates")
    whole_type_def_eq: StrictBool = Field(alias="wholeTypeDefEq")
    axioms: Literal["none"]

    @model_validator(mode="after")
    def _valid_evidence(self) -> Self:
        if self.source == self.candidate:
            raise ValueError("v3 negative skeleton candidate is unchanged")
        if sha256_hex(self.source.encode()) != self.source_type_hash:
            raise ValueError("v3 negative skeleton source hash differs")
        if sha256_hex(self.candidate.encode()) != self.candidate_type_hash:
            raise ValueError("v3 negative skeleton candidate hash differs")
        if not self.candidate_elaborates or self.whole_type_def_eq:
            raise ValueError("v3 negative skeleton type checks differ")
        if self.operation != f"{self.operation_kind}:{self.site_path}":
            raise ValueError("v3 negative skeleton operation/path binding differs")
        if self.evidence != {
            "relation": "schemaInequivalence",
            "exactBooleanSkeleton": True,
            "deduplicatedAtoms": True,
            "fullTruthTableEnumerated": True,
            "implicationAware": True,
            "parameterTelescopePreserved": True,
            "rootInfluence": True,
            "separatorVerified": True,
            "contractScope": "abstract-propositional-schema",
        }:
            raise ValueError("v3 negative skeleton separator contract differs")
        atom_count = self.witness.get("atomCount")
        atom_hashes = self.witness.get("atomHashes")
        valuation = self.witness.get("valuation")
        if (
            not isinstance(atom_count, int)
            or isinstance(atom_count, bool)
            or not 1 <= atom_count <= 8
            or not isinstance(atom_hashes, list)
            or len(atom_hashes) != atom_count
            or not all(
                isinstance(value, str) and v1._HEX64.fullmatch(value) for value in atom_hashes
            )
            or not isinstance(valuation, list)
            or len(valuation) != atom_count
            or not all(isinstance(value, bool) for value in valuation)
            or self.witness.get("valuationSpaceSize") != 2**atom_count
            or not isinstance(self.witness.get("outerBinderCount"), int)
        ):
            raise ValueError("v3 negative skeleton valuation inventory differs")
        if self.witness.get("sourceValue") is self.witness.get("candidateValue"):
            raise ValueError("v3 negative skeleton valuation does not separate")
        return self


@dataclass(frozen=True, slots=True)
class SolverResult:
    status: Literal["passed", "failed", "indeterminate"]
    selected: tuple[EngineCandidateV3, ...]
    states_explored: int
    failed_state_count: int
    reason: str


class _SolverStateLimit(RuntimeError):
    pass


def production_config(output_root: Path) -> FeasibilityConfig:
    inputs = {
        name: v1.FrozenInput(path=path, sha256=digest)
        for name, (path, digest) in v1._PRODUCTION_INPUTS.items()
        if name != "negative_engine"
    }
    inputs.update(
        {
            "negative_engine_v2": v1.FrozenInput(
                path=_ENGINE_V2_PATH, sha256=hash_file(_ENGINE_V2_PATH)
            ),
            "negative_engine_v3": v1.FrozenInput(
                path=_ENGINE_V3_PATH, sha256=hash_file(_ENGINE_V3_PATH)
            ),
            "base_pilot_v2_module": v1.FrozenInput(
                path=_BASE_PILOT_V2_MODULE, sha256=hash_file(_BASE_PILOT_V2_MODULE)
            ),
            "base_implication_smoke_module": v1.FrozenInput(
                path=_BASE_IMPLICATION_SMOKE_MODULE,
                sha256=hash_file(_BASE_IMPLICATION_SMOKE_MODULE),
            ),
            "pilot_v2_manifest": v1.FrozenInput(
                path=_PILOT_V2_ROOT / "manifest.json", sha256=_PILOT_V2_MANIFEST_SHA256
            ),
            "pilot_v2_selection": v1.FrozenInput(
                path=_PILOT_V2_ROOT / "selection.jsonl", sha256=_PILOT_V2_SELECTION_SHA256
            ),
            "implication_smoke_manifest": v1.FrozenInput(
                path=_IMPLICATION_SMOKE_ROOT / "manifest.json",
                sha256=_IMPLICATION_SMOKE_MANIFEST_SHA256,
            ),
        }
    )
    return FeasibilityConfig(
        output_root=output_root,
        mathlib_root=v1._MATHLIB_ROOT,
        tokenizer_root=v1._TOKENIZER_ROOT,
        inputs=inputs,
    )


def verify_input_bindings(config: FeasibilityConfig) -> None:
    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise NegativeSkeletonFeasibilityError(f"unsafe or missing frozen input: {name}")
        if hash_file(binding.path) != binding.sha256:
            raise NegativeSkeletonFeasibilityError(f"frozen feasibility input differs: {name}")
    pilot = v1._read_json(config.inputs["pilot_v2_manifest"].path)
    pilot_outputs = pilot.get("outputs")
    if (
        pilot.get("status") != "completed"
        or pilot.get("summary", {}).get("pilot_gate_passed") is not False
        or not isinstance(pilot_outputs, Mapping)
        or not isinstance(pilot_outputs.get("selection.jsonl"), Mapping)
        or pilot_outputs["selection.jsonl"].get("sha256")
        != config.inputs["pilot_v2_selection"].sha256
        or pilot.get("negative_engine_v2_sha256") != config.inputs["negative_engine_v2"].sha256
        or pilot.get("execution", {}).get("final_test_accessed") is not False
        or pilot.get("execution", {}).get("training_launched") is not False
    ):
        raise NegativeSkeletonFeasibilityError("failed v2 pilot binding differs")
    smoke = v1._read_json(config.inputs["implication_smoke_manifest"].path)
    if (
        smoke.get("status") != "completed"
        or smoke.get("inputs", {}).get("pilot_v2_manifest", {}).get("sha256")
        != config.inputs["pilot_v2_manifest"].sha256
        or smoke.get("inputs", {}).get("pilot_v2_selection", {}).get("sha256")
        != config.inputs["pilot_v2_selection"].sha256
        or smoke.get("negative_engine_v2_sha256") != config.inputs["negative_engine_v2"].sha256
        or smoke.get("negative_engine_v3_sha256") != config.inputs["negative_engine_v3"].sha256
        or smoke.get("summary", {}).get("status") != "passed"
        or smoke.get("summary", {})
        .get("decision", {})
        .get("same_fixed_96_feasibility_precheck_authorized")
        is not True
        or smoke.get("execution", {}).get("final_test_accessed") is not False
        or smoke.get("execution", {}).get("training_launched") is not False
    ):
        raise NegativeSkeletonFeasibilityError("implication-smoke authorization differs")


def select_sources(config: FeasibilityConfig) -> tuple[v1.SourceRow, ...]:
    sources = v1.select_sources(cast(Any, config))
    expected = v1._jsonl_bytes(v1._selection_rows(sources))
    if config.inputs["pilot_v2_selection"].path.read_bytes() != expected:
        raise NegativeSkeletonFeasibilityError("fixed v2 selection replay differs")
    return sources


def _combined_engine(config: FeasibilityConfig) -> str:
    v2_body = implication_smoke._body(
        config.inputs["negative_engine_v2"].path,
        "import Lean",
        (
            "namespace LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
            "end LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
        ),
    )
    v3_body = implication_smoke._body(
        config.inputs["negative_engine_v3"].path,
        "import LeanFaith.Meta.NegativeSkeletonEngineV2",
        (
            "namespace LeanFaith.Meta.NegativeSkeletonEngineV3Helper",
            "lfNegativeSkeletonV3Batch",
            "lfAuditNegativeSkeletonV3",
            "end LeanFaith.Meta.NegativeSkeletonEngineV3Helper",
        ),
    )
    return v2_body + "\n" + v3_body


def render_primary_driver(config: FeasibilityConfig, names_path: Path) -> str:
    return (
        "import Mathlib\n\n"
        + _combined_engine(config)
        + "\nset_option maxHeartbeats 0 in\n"
        + f"lfNegativeSkeletonV3Batch {v1._lean_string(str(names_path))}\n"
    )


def _source_ordinals(sources: Sequence[v1.SourceRow]) -> dict[str, int]:
    return {source.declaration: ordinal for ordinal, source in enumerate(sources)}


def _candidate_key(
    candidate: EngineCandidateV3, ordinals: Mapping[str, int]
) -> tuple[int, str, str, str, str]:
    return (
        ordinals[candidate.declaration],
        candidate.family,
        candidate.operation_kind,
        candidate.operation,
        candidate.candidate_type_hash,
    )


def parse_primary(
    payload: bytes,
    sources: Sequence[v1.SourceRow],
) -> tuple[tuple[EngineCandidateV3, ...], tuple[dict[str, Any], ...]]:
    source_by_declaration = {source.declaration: source for source in sources}
    candidates: list[EngineCandidateV3] = []
    terminals: dict[str, dict[str, Any]] = {}
    batch: dict[str, Any] | None = None
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NegativeSkeletonFeasibilityError(
                f"primary stdout:{line_number}: invalid JSON: {exc}"
            ) from exc
        if (
            not isinstance(raw, dict)
            or v1._canonical_line(raw).rstrip(b"\n") != line
            or raw.get("schemaVersion") != 3
        ):
            raise NegativeSkeletonFeasibilityError("primary v3 engine row contract differs")
        row = cast(dict[str, Any], raw)
        kind = row.get("kind")
        if kind == "candidate":
            try:
                candidate = EngineCandidateV3.model_validate(row)
            except ValidationError as exc:
                raise NegativeSkeletonFeasibilityError(
                    f"primary stdout:{line_number}: invalid v3 candidate: {exc}"
                ) from exc
            source = source_by_declaration.get(candidate.declaration)
            if source is None or candidate.source != source.trainer.reference_headless:
                raise NegativeSkeletonFeasibilityError("v3 engine candidate/source join differs")
            candidates.append(candidate)
        elif kind == "terminal":
            declaration = row.get("declaration")
            if not isinstance(declaration, str) or declaration in terminals:
                raise NegativeSkeletonFeasibilityError("v3 engine terminal identity differs")
            terminals[declaration] = row
        elif kind == "batch":
            if batch is not None:
                raise NegativeSkeletonFeasibilityError("duplicate v3 engine batch terminal")
            batch = row
        else:
            raise NegativeSkeletonFeasibilityError("unknown primary v3 engine row kind")
    if set(terminals) != set(source_by_declaration):
        raise NegativeSkeletonFeasibilityError("v3 engine terminal declaration set differs")
    counts = Counter(candidate.declaration for candidate in candidates)
    for declaration, row in terminals.items():
        source = source_by_declaration[declaration]
        if (
            row.get("status") != "complete"
            or row.get("emittedCount") != counts[declaration]
            or row.get("sourceTypeHash") != sha256_hex(source.trainer.reference_headless.encode())
            or row.get("implicationAware") is not True
        ):
            raise NegativeSkeletonFeasibilityError("one or more v3 terminals differ")
    if (
        batch is None
        or batch.get("status") != "complete"
        or batch.get("declarationCount") != len(sources)
        or batch.get("completedCount") != len(sources)
        or batch.get("failedCount") != 0
    ):
        raise NegativeSkeletonFeasibilityError("v3 engine batch terminal differs")
    keys = [
        (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        )
        for candidate in candidates
    ]
    if len(keys) != len(set(keys)):
        raise NegativeSkeletonFeasibilityError("v3 engine emitted duplicate candidate keys")
    ordinals = _source_ordinals(sources)
    return (
        tuple(sorted(candidates, key=lambda row: _candidate_key(row, ordinals))),
        tuple(terminals[name] for name in sorted(terminals)),
    )


def _screen_candidates(
    config: FeasibilityConfig,
    candidates: Sequence[EngineCandidateV3],
    sources: Sequence[v1.SourceRow],
    tokenizer: Any,
) -> tuple[tuple[EngineCandidateV3, ...], tuple[dict[str, object], ...]]:
    source_by_declaration = {source.declaration: source for source in sources}
    blocklist = GoldenBlocklist.load(config.inputs["golden_blocklist"].path)
    screened: list[EngineCandidateV3] = []
    exclusions: list[dict[str, object]] = []
    for candidate in candidates:
        source = source_by_declaration[candidate.declaration]
        reference_near = signature_near_dup_hash(candidate.source)
        candidate_near = signature_near_dup_hash(candidate.candidate)
        reason: str | None = None
        if (
            reference_near in blocklist.near_dup_hashes
            or candidate_near in blocklist.near_dup_hashes
            or blocklist.problem_is_blocked(source.ancestry_id)
        ):
            reason = "golden_blocklist"
        elif reference_near == candidate_near:
            reason = "degenerate_near_identical_sides"
        forward = len(
            tokenizer.encode(
                pack_pair(candidate.source, candidate.candidate), add_special_tokens=True
            )
        )
        reverse = len(
            tokenizer.encode(
                pack_pair(candidate.candidate, candidate.source), add_special_tokens=True
            )
        )
        if not forward or not reverse:
            raise NegativeSkeletonFeasibilityError("tokenizer returned an empty packed pair")
        if forward > 1024 or reverse > 1024:
            reason = "overlength"
        if reason is None:
            screened.append(candidate)
        else:
            exclusions.append(
                {
                    "schema_version": 1,
                    "declaration": candidate.declaration,
                    "split": source.split,
                    "family": candidate.family,
                    "operation": candidate.operation,
                    "operation_kind": candidate.operation_kind,
                    "site_path": candidate.site_path,
                    "candidate_type_hash": candidate.candidate_type_hash,
                    "reason": reason,
                    "forward_tokens": forward,
                    "reverse_tokens": reverse,
                }
            )
    exclusions.sort(
        key=lambda row: (
            cast(str, row["declaration"]),
            cast(str, row["operation"]),
            cast(str, row["candidate_type_hash"]),
        )
    )
    return tuple(screened), tuple(exclusions)


def _pair_group(candidate: EngineCandidateV3) -> tuple[str, str]:
    return cast(
        tuple[str, str],
        tuple(
            sorted(
                (
                    signature_near_dup_hash(candidate.source),
                    signature_near_dup_hash(candidate.candidate),
                )
            )
        ),
    )


def solve_feasible_subset(
    candidates: Sequence[EngineCandidateV3],
    sources: Sequence[v1.SourceRow],
    *,
    state_limit: int = SOLVER_STATE_LIMIT,
) -> SolverResult:
    """Exhaustively search the frozen exact-24 feasibility contract."""

    source_by_declaration = {source.declaration: source for source in sources}
    options_by_declaration: dict[str, list[EngineCandidateV3]] = defaultdict(list)
    pair_declarations: dict[tuple[str, str], set[str]] = defaultdict(set)
    for candidate in candidates:
        if candidate.declaration not in source_by_declaration:
            raise NegativeSkeletonFeasibilityError("solver candidate is outside selection")
        options_by_declaration[candidate.declaration].append(candidate)
        pair_declarations[_pair_group(candidate)].add(candidate.declaration)
    duplicate_pair_ids = {
        pair: hash_canonical({"schema": "v3_feasibility_pair", "pair": pair})
        for pair, declarations in pair_declarations.items()
        if len(declarations) > 1
    }
    candidate_pair_id = {
        (
            candidate.declaration,
            candidate.operation,
            candidate.candidate_type_hash,
        ): duplicate_pair_ids.get(_pair_group(candidate))
        for candidate in candidates
    }
    split_rank = {"validation": 0, "test": 1, "train": 2}
    ordered_sources = tuple(
        sorted(
            (source for source in sources if options_by_declaration[source.declaration]),
            key=lambda source: (
                split_rank[source.split],
                len(options_by_declaration[source.declaration]),
                source.declaration,
            ),
        )
    )
    operation_kinds = tuple(sorted({candidate.operation_kind for candidate in candidates}))
    operation_index = {operation: index for index, operation in enumerate(operation_kinds)}
    option_groups = tuple(
        tuple(
            sorted(
                options_by_declaration[source.declaration],
                key=lambda candidate: (
                    0 if candidate.family == "N22" else 1,
                    candidate.operation_kind,
                    candidate.operation,
                    candidate.candidate_type_hash,
                ),
            )
        )
        for source in ordered_sources
    )
    length = len(ordered_sources)
    suffix_validation = [0] * (length + 1)
    suffix_test = [0] * (length + 1)
    suffix_n22 = [0] * (length + 1)
    for index in range(length - 1, -1, -1):
        source = ordered_sources[index]
        suffix_validation[index] = suffix_validation[index + 1] + int(source.split == "validation")
        suffix_test[index] = suffix_test[index + 1] + int(source.split == "test")
        suffix_n22[index] = suffix_n22[index + 1] + int(
            any(candidate.family == "N22" for candidate in option_groups[index])
        )

    failed_states: set[tuple[object, ...]] = set()
    explored = 0

    def search(
        index: int,
        selected: tuple[EngineCandidateV3, ...],
        validation_count: int,
        test_count: int,
        n22_count: int,
        operation_counts: tuple[int, ...],
        used_pair_ids: frozenset[str],
    ) -> tuple[EngineCandidateV3, ...] | None:
        nonlocal explored
        state: tuple[object, ...] = (
            index,
            len(selected),
            validation_count,
            test_count,
            n22_count,
            operation_counts,
            tuple(sorted(used_pair_ids)),
        )
        if state in failed_states:
            return None
        explored += 1
        if explored > state_limit:
            raise _SolverStateLimit
        selected_count = len(selected)
        if selected_count == TARGET_TOTAL:
            if (
                validation_count >= MIN_DIAGNOSTIC_YIELD
                and test_count >= MIN_DIAGNOSTIC_YIELD
                and n22_count >= MIN_N22_COUNT
                and max(operation_counts, default=0) <= MAX_OPERATION_COUNT
            ):
                return selected
            failed_states.add(state)
            return None
        needed = TARGET_TOTAL - selected_count
        remaining = length - index
        if (
            index == length
            or remaining < needed
            or validation_count + suffix_validation[index] < MIN_DIAGNOSTIC_YIELD
            or test_count + suffix_test[index] < MIN_DIAGNOSTIC_YIELD
            or n22_count + suffix_n22[index] < MIN_N22_COUNT
            or sum(MAX_OPERATION_COUNT - count for count in operation_counts) < needed
        ):
            failed_states.add(state)
            return None
        dynamically_eligible = sum(
            any(
                operation_counts[operation_index[candidate.operation_kind]] < MAX_OPERATION_COUNT
                for candidate in group
            )
            for group in option_groups[index:]
        )
        if dynamically_eligible < needed:
            failed_states.add(state)
            return None

        source = ordered_sources[index]
        ordered_options = sorted(
            option_groups[index],
            key=lambda candidate: (
                0 if candidate.family == "N22" else 1,
                operation_counts[operation_index[candidate.operation_kind]],
                candidate.operation_kind,
                candidate.operation,
                candidate.candidate_type_hash,
            ),
        )
        for candidate in ordered_options:
            operation_position = operation_index[candidate.operation_kind]
            if operation_counts[operation_position] >= MAX_OPERATION_COUNT:
                continue
            pair_id = candidate_pair_id[
                (candidate.declaration, candidate.operation, candidate.candidate_type_hash)
            ]
            if pair_id is not None and pair_id in used_pair_ids:
                continue
            next_operation_counts = list(operation_counts)
            next_operation_counts[operation_position] += 1
            result = search(
                index + 1,
                (*selected, candidate),
                validation_count + int(source.split == "validation"),
                test_count + int(source.split == "test"),
                n22_count + int(candidate.family == "N22"),
                tuple(next_operation_counts),
                used_pair_ids | ({pair_id} if pair_id is not None else set()),
            )
            if result is not None:
                return result
        result = search(
            index + 1,
            selected,
            validation_count,
            test_count,
            n22_count,
            operation_counts,
            used_pair_ids,
        )
        if result is not None:
            return result
        failed_states.add(state)
        return None

    try:
        selected = search(
            0,
            (),
            0,
            0,
            0,
            (0,) * len(operation_kinds),
            frozenset(),
        )
    except _SolverStateLimit:
        return SolverResult(
            status="indeterminate",
            selected=(),
            states_explored=explored,
            failed_state_count=len(failed_states),
            reason="solver_state_limit_exceeded",
        )
    if selected is None:
        return SolverResult(
            status="failed",
            selected=(),
            states_explored=explored,
            failed_state_count=len(failed_states),
            reason="no_exact_24_subset_satisfies_all_constraints",
        )
    ordinals = _source_ordinals(sources)
    return SolverResult(
        status="passed",
        selected=tuple(sorted(selected, key=lambda row: _candidate_key(row, ordinals))),
        states_explored=explored,
        failed_state_count=len(failed_states),
        reason="exact_24_subset_found",
    )


def _summary(
    config: FeasibilityConfig,
    sources: Sequence[v1.SourceRow],
    emitted: Sequence[EngineCandidateV3],
    screened: Sequence[EngineCandidateV3],
    exclusions: Sequence[Mapping[str, object]],
    solver: SolverResult,
) -> dict[str, object]:
    source_by_declaration = {source.declaration: source for source in sources}
    emitted_family = Counter(candidate.family for candidate in emitted)
    emitted_operations = Counter(candidate.operation_kind for candidate in emitted)
    screened_family = Counter(candidate.family for candidate in screened)
    screened_operations = Counter(candidate.operation_kind for candidate in screened)
    selected_family = Counter(candidate.family for candidate in solver.selected)
    selected_operations = Counter(candidate.operation_kind for candidate in solver.selected)
    selected_splits = Counter(
        source_by_declaration[candidate.declaration].split for candidate in solver.selected
    )
    selected_count = len(solver.selected)
    n22_share = selected_family["N22"] / selected_count if selected_count else 0.0
    n21_share = selected_family["N21"] / selected_count if selected_count else 0.0
    operation_share = (
        max(selected_operations.values(), default=0) / selected_count if selected_count else 0.0
    )
    pair_keys = [_pair_group(candidate) for candidate in solver.selected]
    gates: dict[str, dict[str, object]] = {
        "exact_target_yield": {
            "target": config.target_total,
            "observed": selected_count,
            "passed": selected_count == config.target_total,
        },
        "diagnostic_splits": {
            "minimum_validation": config.min_diagnostic_yield,
            "minimum_test": config.min_diagnostic_yield,
            "observed": dict(sorted(selected_splits.items())),
            "passed": selected_splits["validation"] >= config.min_diagnostic_yield
            and selected_splits["test"] >= config.min_diagnostic_yield,
        },
        "family_mix": {
            "minimum_n22_share": config.min_n22_share,
            "maximum_n21_share": config.max_n21_share,
            "observed_n22_share": n22_share,
            "observed_n21_share": n21_share,
            "passed": n22_share >= config.min_n22_share and n21_share <= config.max_n21_share,
        },
        "operation_cap": {
            "basis": "operation_kind",
            "maximum_share": config.max_operation_share,
            "maximum_integer_count_at_24": MAX_OPERATION_COUNT,
            "observed_maximum_share": operation_share,
            "passed": operation_share <= config.max_operation_share,
        },
        "one_per_declaration_and_unique_pair": {
            "selected_declarations": len({candidate.declaration for candidate in solver.selected}),
            "selected_pair_keys": len(set(pair_keys)),
            "passed": len({candidate.declaration for candidate in solver.selected})
            == selected_count
            and len(set(pair_keys)) == selected_count,
        },
    }
    feasibility_passed = solver.status == "passed" and all(
        cast(bool, gate["passed"]) for gate in gates.values()
    )
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": solver.status,
        "selection": {
            "domain": SELECTION_DOMAIN,
            "requested": len(sources),
            "quotas": config.selection_quotas,
            "selected_names_sha256": sha256_hex(
                "".join(f"{source.declaration}\n" for source in sources).encode()
            ),
            "identical_to_v2": True,
        },
        "candidate_inventory": {
            "engine_emitted": len(emitted),
            "screened_admissible": len(screened),
            "excluded": len(exclusions),
            "declarations_with_admissible_candidates": len(
                {candidate.declaration for candidate in screened}
            ),
            "emitted_family": dict(sorted(emitted_family.items())),
            "emitted_operation_kind": dict(sorted(emitted_operations.items())),
            "screened_family": dict(sorted(screened_family.items())),
            "screened_operation_kind": dict(sorted(screened_operations.items())),
        },
        "solver": {
            "contract": "exact_24_one_per_declaration_exhaustive_bounded_search",
            "state_limit": config.solver_state_limit,
            "states_explored": solver.states_explored,
            "failed_state_count": solver.failed_state_count,
            "reason": solver.reason,
        },
        "selected_counts": {
            "total": selected_count,
            "family": dict(sorted(selected_family.items())),
            "operation_kind": dict(sorted(selected_operations.items())),
            "split": dict(sorted(selected_splits.items())),
        },
        "gates": gates,
        "feasibility_gate_passed": feasibility_passed,
        "decision": {
            "fixed_subset_audit_and_canary_authorized": feasibility_passed,
            "canary_fitted_in_this_run": False,
            "independent_audit_run_in_this_run": False,
            "sample_size_increase_authorized": False,
            "scale_authorized": False,
            "training_authorized": False,
            "final_test_accessed": False,
        },
    }


def _staging_path(config: FeasibilityConfig) -> Path:
    return config.output_root.with_name(f".{config.output_root.name}.partial")


def _artifact_replay(
    config: FeasibilityConfig,
) -> tuple[
    tuple[v1.SourceRow, ...],
    tuple[EngineCandidateV3, ...],
    tuple[EngineCandidateV3, ...],
    tuple[dict[str, object], ...],
    SolverResult,
    dict[str, object],
]:
    sources = select_sources(config)
    selection_payload = v1._jsonl_bytes(v1._selection_rows(sources))
    if (config.output_root / "selection.jsonl").read_bytes() != selection_payload:
        raise NegativeSkeletonFeasibilityError("feasibility selection artifact differs")
    names_payload = "".join(f"{source.declaration}\n" for source in sources).encode()
    if (config.output_root / "declaration_names.txt").read_bytes() != names_payload:
        raise NegativeSkeletonFeasibilityError("feasibility declaration names differ")
    expected_driver = render_primary_driver(config, _staging_path(config) / "declaration_names.txt")
    if (config.output_root / "primary_driver.lean").read_text(encoding="utf-8") != expected_driver:
        raise NegativeSkeletonFeasibilityError("feasibility primary driver differs")
    primary = v1._process_from_artifact(cast(Any, config), stage="primary")
    emitted, _ = parse_primary(primary.stdout, sources)
    tokenizer = v1._load_tokenizer(cast(Any, config))
    screened, exclusions = _screen_candidates(config, emitted, sources, tokenizer)
    solver = solve_feasible_subset(screened, sources, state_limit=config.solver_state_limit)
    summary = _summary(config, sources, emitted, screened, exclusions, solver)
    return sources, emitted, screened, exclusions, solver, summary


def verify_feasibility(config: FeasibilityConfig) -> dict[str, Any]:
    """Replay the fixed selection, v3 pool, screens, and bounded solver."""

    verify_input_bindings(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise NegativeSkeletonFeasibilityError("feasibility root must be a non-symlink directory")
    observed = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed != _OUTPUTS:
        raise NegativeSkeletonFeasibilityError("feasibility output file set differs")
    manifest = v1._read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _STATIC_OUTPUTS:
        raise NegativeSkeletonFeasibilityError("feasibility manifest output inventory differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise NegativeSkeletonFeasibilityError(f"invalid output binding: {name}")
        path = config.output_root / name
        if (
            path.is_symlink()
            or raw_binding.get("path") != str(path)
            or raw_binding.get("sha256") != hash_file(path)
        ):
            raise NegativeSkeletonFeasibilityError(f"output binding differs: {name}")
    _, emitted, screened, exclusions, solver, summary = _artifact_replay(config)
    expected_payloads = {
        "candidate_pool.jsonl": v1._jsonl_bytes(
            candidate.model_dump(mode="json", by_alias=True) for candidate in emitted
        ),
        "screened_candidates.jsonl": v1._jsonl_bytes(
            candidate.model_dump(mode="json", by_alias=True) for candidate in screened
        ),
        "exclusions.jsonl": v1._jsonl_bytes(exclusions),
        "feasible_selection.jsonl": v1._jsonl_bytes(
            candidate.model_dump(mode="json", by_alias=True) for candidate in solver.selected
        ),
        "summary.json": v1._canonical_line(summary),
    }
    for name, payload in expected_payloads.items():
        if (config.output_root / name).read_bytes() != payload:
            raise NegativeSkeletonFeasibilityError(f"replayed output differs: {name}")
    if (
        manifest.get("status") != "completed"
        or manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("base_pilot_v2_module_sha256")
        != config.inputs["base_pilot_v2_module"].sha256
        or manifest.get("base_implication_smoke_module_sha256")
        != config.inputs["base_implication_smoke_module"].sha256
        or manifest.get("negative_engine_v2_sha256") != config.inputs["negative_engine_v2"].sha256
        or manifest.get("negative_engine_v3_sha256") != config.inputs["negative_engine_v3"].sha256
        or manifest.get("summary") != summary
        or manifest.get("privacy")
        != {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        }
        or manifest.get("execution", {}).get("audit_launched") is not False
        or manifest.get("execution", {}).get("canary_fitted") is not False
        or manifest.get("execution", {}).get("final_test_accessed") is not False
        or manifest.get("execution", {}).get("training_launched") is not False
    ):
        raise NegativeSkeletonFeasibilityError("feasibility manifest contract differs")
    return manifest


def materialize_feasibility(config: FeasibilityConfig) -> dict[str, Any]:
    """Run one timeout-bounded Lean pool and atomically freeze feasibility."""

    if config.output_root.exists():
        return verify_feasibility(config)
    verify_input_bindings(config)
    sources = select_sources(config)
    staging = _staging_path(config)
    if staging.exists():
        raise NegativeSkeletonFeasibilityError(f"stale feasibility staging root exists: {staging}")
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        v1._write_payload(staging / "selection.jsonl", v1._jsonl_bytes(v1._selection_rows(sources)))
        v1._write_payload(
            staging / "declaration_names.txt",
            "".join(f"{source.declaration}\n" for source in sources).encode(),
        )
        primary_driver = render_primary_driver(config, staging / "declaration_names.txt")
        v1._write_payload(staging / "primary_driver.lean", primary_driver.encode())
        primary = v1._run_lean(staging / "primary_driver.lean", cast(Any, config))
        v1._write_payload(staging / "primary.stdout.jsonl", primary.stdout)
        v1._write_payload(staging / "primary.stderr.txt", primary.stderr)
        v1._write_payload(
            staging / "primary.process.json",
            v1._canonical_line(v1._process_payload(primary, cast(Any, config), stage="primary")),
        )
        v1._validate_process(primary, cast(Any, config), stage="primary")
        emitted, _ = parse_primary(primary.stdout, sources)
        tokenizer = v1._load_tokenizer(cast(Any, config))
        screened, exclusions = _screen_candidates(config, emitted, sources, tokenizer)
        solver = solve_feasible_subset(screened, sources, state_limit=config.solver_state_limit)
        summary = _summary(config, sources, emitted, screened, exclusions, solver)
        payloads = {
            "candidate_pool.jsonl": v1._jsonl_bytes(
                candidate.model_dump(mode="json", by_alias=True) for candidate in emitted
            ),
            "screened_candidates.jsonl": v1._jsonl_bytes(
                candidate.model_dump(mode="json", by_alias=True) for candidate in screened
            ),
            "exclusions.jsonl": v1._jsonl_bytes(exclusions),
            "feasible_selection.jsonl": v1._jsonl_bytes(
                candidate.model_dump(mode="json", by_alias=True) for candidate in solver.selected
            ),
            "summary.json": v1._canonical_line(summary),
        }
        for name, payload in payloads.items():
            v1._write_payload(staging / name, payload)
        manifest = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "config_sha256": hash_canonical(config.model_dump(mode="json")),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "base_pilot_v2_module_sha256": config.inputs["base_pilot_v2_module"].sha256,
            "base_implication_smoke_module_sha256": config.inputs[
                "base_implication_smoke_module"
            ].sha256,
            "negative_engine_v2_sha256": config.inputs["negative_engine_v2"].sha256,
            "negative_engine_v3_sha256": config.inputs["negative_engine_v3"].sha256,
            "inputs": {
                name: {"path": str(binding.path), "sha256": binding.sha256}
                for name, binding in sorted(config.inputs.items())
            },
            "outputs": {
                name: {
                    "path": str(config.output_root / name),
                    "sha256": hash_file(staging / name),
                }
                for name in sorted(_STATIC_OUTPUTS)
            },
            "summary": summary,
            "privacy": {
                "public_only": True,
                "private_source_content": False,
                "external_transmission": False,
            },
            "execution": {
                "primary_lean_exit_code": primary.exit_code,
                "primary_timeout_seconds": config.timeout_seconds,
                "solver_state_limit": config.solver_state_limit,
                "audit_launched": False,
                "canary_fitted": False,
                "external_calls": False,
                "final_test_accessed": False,
                "training_launched": False,
            },
        }
        v1._write_payload(staging / "manifest.json", v1._canonical_line(manifest))
        os.replace(staging, config.output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verify_feasibility(config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("run-feasibility", "verify-feasibility", "show-selection")
    )
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = production_config(cast(Path, args.output_root))
    if args.command == "show-selection":
        print(json.dumps(v1._selection_rows(select_sources(config)), sort_keys=True))
        return 0
    manifest = (
        materialize_feasibility(config)
        if args.command == "run-feasibility"
        else verify_feasibility(config)
    )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
