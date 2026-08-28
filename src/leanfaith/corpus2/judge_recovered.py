"""Resumable Codex judgments for the recovered Lean-valid LF-022 pairs.

The input boundary is an immutable, public-only pair plan.  Provider calls see
only the blinded prompt projection, and every paid attempt is journaled before
the subprocess starts.  A missing terminal therefore requires an explicit
retry instead of risking an untracked duplicate charge.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import signal
import subprocess
import tempfile
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol, Self, cast

from pydantic import Field, StrictBool, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditInput,
    load_lean_valid_audit_inputs,
)
from leanfaith.generation.lf022_execution import LF022GOpenExecutionTask
from leanfaith.generation.lf022_lean_check import LF022LeanCheckRecord
from leanfaith.generation.weak_supervision import (
    DEFAULT_JUDGE_TEMPLATE,
    JudgeOutputParseError,
    JudgePresentation,
    JudgeResponse,
    make_swapped_presentations,
    parse_blinded_judge_output,
    remap_judgment_to_canonical_order,
    render_blinded_judge_prompt,
)
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.schemas.evidence import JudgmentValue
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.theorem import TheoremRecord

METHOD_VERSION: Literal["recovered_singlepass_codex_v1"] = "recovered_singlepass_codex_v1"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT: Literal["medium"] = "medium"
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_SEED = 20260828
PRODUCTION_PAIR_COUNT = 13_373
PILOT_PER_PROPOSER = 50
PRODUCTION_BATCH_SIZE = 500
PRODUCTION_AUDIT_SAMPLE_SIZE = 150
TRAINING_SOURCE: Literal["lf022_recovered_codex_judge_v1"] = "lf022_recovered_codex_judge_v1"

_THEOREM_STORE = Path(
    "/storage/milikic/leanfaith/immutable/extractions/"
    "mathlib_d568c8c_manifest_b1831204/theorems/mathlib.jsonl"
)
_THEOREM_STORE_SHA256 = "7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7"
_GOLDEN_BLOCKLIST_SHA256 = "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"

Proposer = Literal["qwen", "kimi"]
ResolutionStatus = Literal[
    "resolved_primary",
    "needs_reverse",
    "resolved_reverse",
    "resolved_agreement",
    "conflict",
    "unresolved_reverse",
]
CallStatus = Literal[
    "completed",
    "parse_failed",
    "process_failed",
    "timeout",
    "interrupted",
    "final_output_missing",
]

_PROPOSER_FAMILY: dict[Proposer, str] = {
    "qwen": "qwen3",
    "kimi": "moonshot_kimi_k2",
}


class RecoveredJudgeError(RuntimeError):
    """The recovered-judge artifact or execution contract was violated."""


def _headless(statement: str) -> str:
    value = normalize_headless(statement)
    if value is None:
        raise RecoveredJudgeError("recovered judge input is not a Lean declaration")
    return value


def _source_theorem_id(item: LF022CodexAuditInput) -> str:
    theorem_ids = tuple(value for value in item.pair.source_record_ids if value.startswith("thm:"))
    if len(theorem_ids) != 1:
        raise RecoveredJudgeError("judge pair must bind exactly one source theorem")
    return theorem_ids[0]


def _reverse_presentation(item: LF022CodexAuditInput) -> JudgePresentation:
    key = bytes.fromhex(
        hash_canonical(
            {
                "schema": "recovered_singlepass_codex_reverse_v1",
                "audit_item_id": item.audit_item_id,
                "pair_id": item.pair.pair_id,
            }
        )
    )
    return next(
        presentation
        for presentation in make_swapped_presentations(
            source=item.pair,
            judge_slot="judge_A",
            randomization_key=key,
        )
        if presentation.orientation == "BA"
    )


class RecoveredPlanRow(StrictModel):
    """One content-bound public pair, including both dispatch orientations."""

    schema_version: Literal[1] = 1
    plan_row_id: str = Field(pattern=id_pattern("recovered_judge_plan_row"))
    plan_index: int = Field(ge=0, strict=True)
    proposer: Proposer
    proposer_family_id: str = Field(min_length=1)
    source_git_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_checks_sha256: str = Field(pattern=HEX64_PATTERN)
    source_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    source_theorem_id: str = Field(pattern=id_pattern("thm"))
    group_key: str = Field(min_length=1)
    reference_headless: str = Field(min_length=1)
    candidate_headless: str = Field(min_length=1)
    audit_input: LF022CodexAuditInput
    primary_presentation: JudgePresentation
    reverse_presentation: JudgePresentation

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if self.proposer_family_id != _PROPOSER_FAMILY[self.proposer]:
            raise ValueError("proposer alias/family mismatch")
        if self.source_theorem_id != _source_theorem_id(self.audit_input):
            raise ValueError("source_theorem_id differs from the admitted pair")
        if self.group_key != self.source_theorem_id and not self.group_key.startswith("anc:"):
            raise ValueError("group_key must be a source theorem or root ancestry ID")
        if self.reference_headless != _headless(self.audit_input.pair.canonical_lean_a):
            raise ValueError("reference_headless differs from the canonical pair")
        if self.candidate_headless != _headless(self.audit_input.pair.canonical_lean_b):
            raise ValueError("candidate_headless differs from the canonical pair")
        if self.primary_presentation != self.audit_input.presentation:
            raise ValueError("primary presentation must be the admitted AB presentation")
        if self.primary_presentation.orientation != "AB":
            raise ValueError("primary presentation must have AB orientation")
        if self.reverse_presentation.orientation != "BA":
            raise ValueError("reverse presentation must have BA orientation")
        if self.reverse_presentation.pair_id != self.audit_input.pair.pair_id:
            raise ValueError("reverse presentation/pair mismatch")
        if (
            self.reverse_presentation.lean_a != self.audit_input.pair.canonical_lean_b
            or self.reverse_presentation.lean_b != self.audit_input.pair.canonical_lean_a
        ):
            raise ValueError("reverse presentation does not swap the canonical pair")
        if self.plan_row_id != _plan_row_id(self):
            raise ValueError("plan_row_id does not match row content")
        return self


def _plan_row_id(row: RecoveredPlanRow) -> str:
    return make_id(
        "recovered_judge_plan_row",
        row.model_dump(mode="json", exclude={"plan_row_id", "plan_index"}),
    )


def _make_plan_row(
    *,
    item: LF022CodexAuditInput,
    proposer: Proposer,
    plan_index: int,
    source_git_revision: str,
    source_checks_sha256: str,
    source_manifest_sha256: str,
    group_key: str | None = None,
) -> RecoveredPlanRow:
    theorem_id = _source_theorem_id(item)
    resolved_group_key = group_key or theorem_id
    values: dict[str, object] = {
        "schema_version": 1,
        "plan_index": plan_index,
        "proposer": proposer,
        "proposer_family_id": _PROPOSER_FAMILY[proposer],
        "source_git_revision": source_git_revision,
        "source_checks_sha256": source_checks_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "source_theorem_id": theorem_id,
        "group_key": resolved_group_key,
        "reference_headless": _headless(item.pair.canonical_lean_a),
        "candidate_headless": _headless(item.pair.canonical_lean_b),
        "audit_input": item,
        "primary_presentation": item.presentation,
        "reverse_presentation": _reverse_presentation(item),
    }
    temporary = RecoveredPlanRow.model_construct(
        schema_version=1,
        plan_row_id="",
        plan_index=plan_index,
        proposer=proposer,
        proposer_family_id=_PROPOSER_FAMILY[proposer],
        source_git_revision=source_git_revision,
        source_checks_sha256=source_checks_sha256,
        source_manifest_sha256=source_manifest_sha256,
        source_theorem_id=theorem_id,
        group_key=resolved_group_key,
        reference_headless=_headless(item.pair.canonical_lean_a),
        candidate_headless=_headless(item.pair.canonical_lean_b),
        audit_input=item,
        primary_presentation=item.presentation,
        reverse_presentation=cast(JudgePresentation, values["reverse_presentation"]),
    )
    return RecoveredPlanRow.model_validate({**values, "plan_row_id": _plan_row_id(temporary)})


class RecoveredPlan(StrictModel):
    """Balanced pilot rows plus the deterministically ordered remainder."""

    schema_version: Literal[1] = 1
    method_version: Literal["recovered_singlepass_codex_v1"] = METHOD_VERSION
    seed: int = Field(strict=True)
    theorem_store_sha256: str = Field(default="0" * 64, pattern=HEX64_PATTERN)
    source_collection_binding_sha256: str = Field(default="0" * 64, pattern=HEX64_PATTERN)
    rows: tuple[RecoveredPlanRow, ...]
    remaining_rows: tuple[RecoveredPlanRow, ...] = ()
    pilot_pair_count: int = Field(ge=1, strict=True)
    total_pair_count: int = Field(ge=1, strict=True)
    ordered_all_audit_item_ids: tuple[str, ...]
    ordered_all_audit_item_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    ordered_pilot_plan_row_ids_sha256: str = Field(pattern=HEX64_PATTERN)

    @property
    def execution_rows(self) -> tuple[RecoveredPlanRow, ...]:
        return self.rows + self.remaining_rows

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        execution_rows = self.execution_rows
        if len(self.rows) != self.pilot_pair_count:
            raise ValueError("pilot row count does not match pilot_pair_count")
        if len(execution_rows) != self.total_pair_count:
            raise ValueError("plan row count does not match total_pair_count")
        if [row.plan_index for row in execution_rows] != list(range(self.total_pair_count)):
            raise ValueError("plan indices must be contiguous")
        audit_ids = tuple(row.audit_input.audit_item_id for row in execution_rows)
        if audit_ids != self.ordered_all_audit_item_ids:
            raise ValueError("ordered audit IDs differ from execution rows")
        if len(set(audit_ids)) != len(audit_ids):
            raise ValueError("plan contains duplicate audit item IDs")
        if len({row.audit_input.pair.pair_id for row in execution_rows}) != len(execution_rows):
            raise ValueError("plan contains duplicate pair IDs")
        if self.ordered_all_audit_item_ids_sha256 != hash_canonical(list(audit_ids)):
            raise ValueError("ordered full-plan hash mismatch")
        pilot_ids = [row.plan_row_id for row in self.rows]
        if self.ordered_pilot_plan_row_ids_sha256 != hash_canonical(pilot_ids):
            raise ValueError("ordered pilot hash mismatch")
        return self


def _rank(item: LF022CodexAuditInput, *, proposer: Proposer, seed: int) -> str:
    return hash_canonical(
        {
            "schema": "recovered_singlepass_codex_order_v1",
            "seed": seed,
            "proposer": proposer,
            "audit_item_id": item.audit_item_id,
            "pair_id": item.pair.pair_id,
        }
    )


def build_pilot_plan(
    *,
    qwen_inputs: Sequence[LF022CodexAuditInput],
    kimi_inputs: Sequence[LF022CodexAuditInput],
    per_proposer: int = PILOT_PER_PROPOSER,
    expected_total: int | None = None,
    seed: int = DEFAULT_SEED,
    qwen_git_revision: str = "0" * 40,
    kimi_git_revision: str = "0" * 40,
    qwen_checks_sha256: str = "0" * 64,
    kimi_checks_sha256: str = "0" * 64,
    qwen_manifest_sha256: str = "0" * 64,
    kimi_manifest_sha256: str = "0" * 64,
    group_key_by_theorem: Mapping[str, str] | None = None,
    theorem_store_sha256: str = "0" * 64,
    source_collection_binding_sha256: str = "0" * 64,
) -> RecoveredPlan:
    """Select a deterministic balanced pilot while binding the full input order."""

    if per_proposer < 1:
        raise ValueError("per_proposer must be positive")
    if len(qwen_inputs) < per_proposer or len(kimi_inputs) < per_proposer:
        raise ValueError("each proposer must have at least per_proposer inputs")
    total = len(qwen_inputs) + len(kimi_inputs)
    if expected_total is not None and total != expected_total:
        raise ValueError(f"expected {expected_total} recovered pairs, observed {total}")
    all_items = (*qwen_inputs, *kimi_inputs)
    if len({item.audit_item_id for item in all_items}) != total:
        raise ValueError("recovered inputs contain duplicate audit item IDs")
    if len({item.pair.pair_id for item in all_items}) != total:
        raise ValueError("recovered inputs contain duplicate pair IDs")

    ranked: dict[Proposer, list[LF022CodexAuditInput]] = {
        "qwen": sorted(qwen_inputs, key=lambda item: _rank(item, proposer="qwen", seed=seed)),
        "kimi": sorted(kimi_inputs, key=lambda item: _rank(item, proposer="kimi", seed=seed)),
    }
    pilot_tagged: list[tuple[Proposer, LF022CodexAuditInput]] = [
        *(("qwen", item) for item in ranked["qwen"][:per_proposer]),
        *(("kimi", item) for item in ranked["kimi"][:per_proposer]),
    ]
    pilot_tagged.sort(key=lambda value: _rank(value[1], proposer=value[0], seed=seed))
    remaining_tagged: list[tuple[Proposer, LF022CodexAuditInput]] = [
        *(("qwen", item) for item in ranked["qwen"][per_proposer:]),
        *(("kimi", item) for item in ranked["kimi"][per_proposer:]),
    ]
    remaining_tagged.sort(key=lambda value: _rank(value[1], proposer=value[0], seed=seed))
    tagged = pilot_tagged + remaining_tagged

    revisions = {"qwen": qwen_git_revision, "kimi": kimi_git_revision}
    checks_hashes = {"qwen": qwen_checks_sha256, "kimi": kimi_checks_sha256}
    manifest_hashes = {"qwen": qwen_manifest_sha256, "kimi": kimi_manifest_sha256}
    all_rows = tuple(
        _make_plan_row(
            item=item,
            proposer=proposer,
            plan_index=index,
            source_git_revision=revisions[proposer],
            source_checks_sha256=checks_hashes[proposer],
            source_manifest_sha256=manifest_hashes[proposer],
            group_key=(
                group_key_by_theorem.get(_source_theorem_id(item))
                if group_key_by_theorem is not None
                else None
            ),
        )
        for index, (proposer, item) in enumerate(tagged)
    )
    pilot_count = 2 * per_proposer
    audit_ids = tuple(row.audit_input.audit_item_id for row in all_rows)
    values = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "seed": seed,
        "theorem_store_sha256": theorem_store_sha256,
        "source_collection_binding_sha256": source_collection_binding_sha256,
        "rows": all_rows[:pilot_count],
        "remaining_rows": all_rows[pilot_count:],
        "pilot_pair_count": pilot_count,
        "total_pair_count": total,
        "ordered_all_audit_item_ids": audit_ids,
        "ordered_all_audit_item_ids_sha256": hash_canonical(list(audit_ids)),
        "ordered_pilot_plan_row_ids_sha256": hash_canonical(
            [row.plan_row_id for row in all_rows[:pilot_count]]
        ),
    }
    return RecoveredPlan.model_validate(values)


@dataclass(frozen=True, slots=True)
class _ProductionSource:
    proposer: Proposer
    proposer_family_id: str
    repo_root: Path
    git_revision: str
    checks_path: Path
    checks_sha256: str
    manifest_path: Path
    manifest_sha256: str
    expected_valid_count: int

    def binding(self) -> dict[str, object]:
        return {
            "proposer": self.proposer,
            "proposer_family_id": self.proposer_family_id,
            "repo_root": str(self.repo_root),
            "git_revision": self.git_revision,
            "checks_path": str(self.checks_path),
            "checks_sha256": self.checks_sha256,
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "expected_valid_count": self.expected_valid_count,
        }


_QWEN_SOURCE = _ProductionSource(
    proposer="qwen",
    proposer_family_id="qwen3",
    repo_root=Path("/localhome/milikic/LeanFaith-rcp-5e672b9"),
    git_revision="5e672b987d65a876d953ea9757a81eedb9d03411",
    checks_path=Path(
        "/storage/milikic/leanfaith/lf022_lean_checks/"
        "qwen3_5_397b_full9207_v4/"
        "a6a1eeb6945cebc1c174b20ef4c1e169cf36d50bb8fc6fcb8abca9d66286c5c7/"
        "checks.jsonl"
    ),
    checks_sha256="9131a66130deb8680738206619059a07ed38c06856e7e5b4e2dacd3446e3b483",
    manifest_path=Path(
        "/storage/milikic/leanfaith/lf022_lean_checks/"
        "qwen3_5_397b_full9207_v4/"
        "a6a1eeb6945cebc1c174b20ef4c1e169cf36d50bb8fc6fcb8abca9d66286c5c7/"
        "manifest.json"
    ),
    manifest_sha256="035a55a9e55144b43ae26c30f47643f0bf71f8a8b24320f21d91c1dc4941b157",
    expected_valid_count=6_391,
)

_KIMI_SOURCE = _ProductionSource(
    proposer="kimi",
    proposer_family_id="moonshot_kimi_k2",
    repo_root=Path("/localhome/milikic/LeanFaith-kimi-641d13d"),
    git_revision="641d13d75e51306ebcff918fca37533d31c4ebf3",
    checks_path=Path(
        "/storage/milikic/leanfaith/lf022_lean_checks/"
        "kimi_v4_641d13d_full9207_v2/"
        "f24c623244de9502043712945c2b1c67378ef61c3e79970613364a0bd81f942f/"
        "checks.jsonl"
    ),
    checks_sha256="3ddd163f1580b3d4c45c78b7703c35bdbb3a877302fbf3fef3e95841f72a7297",
    manifest_path=Path(
        "/storage/milikic/leanfaith/lf022_lean_checks/"
        "kimi_v4_641d13d_full9207_v2/"
        "f24c623244de9502043712945c2b1c67378ef61c3e79970613364a0bd81f942f/"
        "manifest.json"
    ),
    manifest_sha256="3854703328751f547056099bc68667255d0caa11e753c4e1fd4fc287eb3b17c1",
    expected_valid_count=6_982,
)


def _git_revision(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or len(revision) != 40:
        raise RecoveredJudgeError(f"cannot resolve git revision for {repo_root}")
    return revision


def _resolve_source_artifact(path_text: str, repo_root: Path) -> Path:
    path = Path(path_text)
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _validate_proposer_bindings(
    *,
    source: _ProductionSource,
    inputs: Sequence[LF022CodexAuditInput],
) -> None:
    input_by_check = {item.lean_check_id: item for item in inputs}
    if len(input_by_check) != len(inputs):
        raise RecoveredJudgeError(f"duplicate admitted checks for {source.proposer}")
    seen: set[str] = set()
    for line_number, raw in enumerate(source.checks_path.read_bytes().splitlines(), start=1):
        try:
            check = LF022LeanCheckRecord.model_validate_json(raw)
        except ValueError as exc:
            raise RecoveredJudgeError(
                f"invalid {source.proposer} check line {line_number}: {exc}"
            ) from exc
        item = input_by_check.get(check.check_id)
        if item is None:
            continue
        artifact = _resolve_source_artifact(check.source_variant_artifact, source.repo_root)
        task_path = artifact.with_name("task.json")
        if hash_file(task_path) != item.source_task_sha256:
            raise RecoveredJudgeError(f"source-task hash mismatch: {task_path}")
        try:
            task = LF022GOpenExecutionTask.model_validate_json(task_path.read_bytes())
        except ValueError as exc:
            raise RecoveredJudgeError(f"invalid source task {task_path}: {exc}") from exc
        if task.allocation_task.proposer_family_id != source.proposer_family_id:
            raise RecoveredJudgeError(
                f"configured {source.proposer} collection contains proposer "
                f"{task.allocation_task.proposer_family_id!r}"
            )
        if task.source.source_theorem_id != _source_theorem_id(item):
            raise RecoveredJudgeError(f"source theorem mismatch: {task_path}")
        seen.add(check.check_id)
    missing = sorted(set(input_by_check).difference(seen))
    if missing:
        raise RecoveredJudgeError(
            f"{source.proposer} proposer verification missed {len(missing)} admitted checks"
        )


def _load_production_source(source: _ProductionSource) -> tuple[LF022CodexAuditInput, ...]:
    if _git_revision(source.repo_root) != source.git_revision:
        raise RecoveredJudgeError(f"historical worktree revision drift: {source.repo_root}")
    if hash_file(source.checks_path) != source.checks_sha256:
        raise RecoveredJudgeError(f"checks hash drift: {source.checks_path}")
    if hash_file(source.manifest_path) != source.manifest_sha256:
        raise RecoveredJudgeError(f"checks-manifest hash drift: {source.manifest_path}")
    inputs = load_lean_valid_audit_inputs(
        checks_path=source.checks_path,
        repo_root=source.repo_root,
    )
    if len(inputs) != source.expected_valid_count:
        raise RecoveredJudgeError(
            f"{source.proposer} expected {source.expected_valid_count} valid inputs, "
            f"observed {len(inputs)}"
        )
    _validate_proposer_bindings(source=source, inputs=inputs)
    return inputs


def _root_ancestry_map(theorem_ids: set[str]) -> dict[str, str]:
    if hash_file(_THEOREM_STORE) != _THEOREM_STORE_SHA256:
        raise RecoveredJudgeError("frozen theorem-store hash drift")
    roots: dict[str, str] = {}
    for line_number, raw in enumerate(_THEOREM_STORE.read_bytes().splitlines(), start=1):
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RecoveredJudgeError(f"invalid theorem-store line {line_number}") from exc
        if not isinstance(payload, dict):
            raise RecoveredJudgeError(f"non-object theorem-store line {line_number}")
        theorem_payload = cast(dict[str, object], payload).get("theorem")
        try:
            theorem = TheoremRecord.model_validate(theorem_payload)
        except ValueError as exc:
            raise RecoveredJudgeError(f"invalid theorem-store line {line_number}: {exc}") from exc
        if theorem.theorem_id not in theorem_ids:
            continue
        if len(theorem.root_ancestry_ids) != 1:
            raise RecoveredJudgeError(
                f"recovered source theorem has non-singleton roots: {theorem.theorem_id}"
            )
        roots[theorem.theorem_id] = theorem.root_ancestry_ids[0]
    missing = sorted(theorem_ids.difference(roots))
    if missing:
        raise RecoveredJudgeError(
            f"frozen theorem store misses {len(missing)} recovered source theorems"
        )
    return roots


def _validate_golden_blocklist(
    *,
    blocklist_path: Path,
    inputs: Sequence[LF022CodexAuditInput],
) -> None:
    """Hash-screen public pairs without opening any sealed benchmark text."""

    if hash_file(blocklist_path) != _GOLDEN_BLOCKLIST_SHA256:
        raise RecoveredJudgeError("golden blocklist hash drift")
    try:
        payload: object = json.loads(blocklist_path.read_bytes())
    except json.JSONDecodeError as exc:
        raise RecoveredJudgeError("golden blocklist is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RecoveredJudgeError("golden blocklist must be an object")
    values = cast(dict[str, object], payload)
    near_dup_values = values.get("near_dup_hashes")
    group_values = values.get("group_keys")
    if not isinstance(near_dup_values, list) or not all(
        isinstance(value, str) for value in near_dup_values
    ):
        raise RecoveredJudgeError("golden blocklist near-duplicate hashes are malformed")
    if not isinstance(group_values, list) or not all(
        isinstance(value, str) for value in group_values
    ):
        raise RecoveredJudgeError("golden blocklist group keys are malformed")
    near_dup_hashes = set(cast(list[str], near_dup_values))
    group_keys = set(cast(list[str], group_values))
    for item in inputs:
        reference = _headless(item.pair.canonical_lean_a)
        candidate = _headless(item.pair.canonical_lean_b)
        if (
            signature_near_dup_hash(reference) in near_dup_hashes
            or signature_near_dup_hash(candidate) in near_dup_hashes
            or any(value in group_keys for value in item.pair.source_record_ids)
        ):
            raise RecoveredJudgeError("recovered pair intersects the golden blocklist")


def _config_artifact_payload(config: RecoveredJudgeConfig) -> dict[str, object]:
    return config.model_dump(
        mode="json",
        exclude={"start_index", "count", "retry_incomplete_attempts", "max_workers"},
    )


def materialize_production_plan(
    *,
    repo_root: Path,
    output_root: Path,
) -> tuple[RecoveredJudgeConfig, RecoveredPlan]:
    """Freeze the exact 13,373 public pairs before any provider invocation."""

    sources = (_QWEN_SOURCE, _KIMI_SOURCE)
    collection_payload = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "sources": [source.binding() for source in sources],
        "theorem_store": {
            "path": str(_THEOREM_STORE),
            "sha256": _THEOREM_STORE_SHA256,
        },
        "golden_blocklist": {
            "path": str(repo_root / "data" / "benchmarks" / "golden_blocklist_v1.json"),
            "sha256": _GOLDEN_BLOCKLIST_SHA256,
            "sealed_text_opened": False,
        },
    }
    collection_bytes = canonical_json_bytes(collection_payload) + b"\n"
    collection_hash = sha256_hex(collection_bytes)
    qwen_inputs = _load_production_source(_QWEN_SOURCE)
    kimi_inputs = _load_production_source(_KIMI_SOURCE)
    _validate_golden_blocklist(
        blocklist_path=repo_root / "data" / "benchmarks" / "golden_blocklist_v1.json",
        inputs=(*qwen_inputs, *kimi_inputs),
    )
    theorem_ids = {_source_theorem_id(item) for item in (*qwen_inputs, *kimi_inputs)}
    roots = _root_ancestry_map(theorem_ids)
    plan = build_pilot_plan(
        qwen_inputs=qwen_inputs,
        kimi_inputs=kimi_inputs,
        per_proposer=PILOT_PER_PROPOSER,
        expected_total=PRODUCTION_PAIR_COUNT,
        seed=DEFAULT_SEED,
        qwen_git_revision=_QWEN_SOURCE.git_revision,
        kimi_git_revision=_KIMI_SOURCE.git_revision,
        qwen_checks_sha256=_QWEN_SOURCE.checks_sha256,
        kimi_checks_sha256=_KIMI_SOURCE.checks_sha256,
        qwen_manifest_sha256=_QWEN_SOURCE.manifest_sha256,
        kimi_manifest_sha256=_KIMI_SOURCE.manifest_sha256,
        group_key_by_theorem=roots,
        theorem_store_sha256=_THEOREM_STORE_SHA256,
        source_collection_binding_sha256=collection_hash,
    )
    prompt_path = repo_root / "prompts" / "judges" / "lean_pair_blinded_v2.txt"
    config = RecoveredJudgeConfig(
        repo_root=repo_root,
        output_root=output_root,
        implementation_git_revision=_git_revision(repo_root),
        implementation_module_sha256=hash_file(Path(__file__)),
        prompt_path=prompt_path,
        prompt_sha256=hash_file(prompt_path),
        expected_total=PRODUCTION_PAIR_COUNT,
        max_workers=1,
    )
    _write_immutable(output_root / "inputs" / "source_collections.json", collection_bytes)
    _persist_plan(output_root, plan)
    _schema_path(output_root)
    _write_immutable(
        output_root / "run_config.json",
        canonical_json_bytes(_config_artifact_payload(config)) + b"\n",
    )
    return config, plan


class RecoveredResolution(StrictModel):
    status: ResolutionStatus
    final_label: StrictBool | None
    escalated: bool
    primary: JudgmentValue | None
    reverse: JudgmentValue | None

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        resolved = self.status in {
            "resolved_primary",
            "resolved_reverse",
            "resolved_agreement",
        }
        if resolved != (self.final_label is not None):
            raise ValueError("resolution status/final label mismatch")
        if self.status == "resolved_primary" and self.escalated:
            raise ValueError("primary-only resolution cannot be escalated")
        if self.status != "resolved_primary" and not self.escalated:
            raise ValueError("non-primary resolution must be escalated")
        return self


def _as_response(value: JudgeResponse | str | None) -> JudgeResponse | None:
    if value is None or isinstance(value, JudgeResponse):
        return value
    try:
        return parse_blinded_judge_output(value)
    except JudgeOutputParseError:
        return None


def _binary_label(value: JudgmentValue, *, threshold: float) -> bool | None:
    if value.confidence is None or value.confidence < threshold:
        return None
    if value.answer == "same_claim":
        return True
    if value.answer == "not_same_claim":
        return False
    return None


def resolve_primary_and_optional_ba(
    primary_raw: JudgeResponse | str | None,
    ba_raw: JudgeResponse | str | None = None,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> RecoveredResolution:
    """Apply the frozen one-pass policy, remapping BA before comparison."""

    primary_response = _as_response(primary_raw)
    primary = primary_response.to_evidence_value() if primary_response is not None else None
    primary_label = (
        _binary_label(primary, threshold=confidence_threshold) if primary is not None else None
    )
    if primary_label is not None:
        return RecoveredResolution(
            status="resolved_primary",
            final_label=primary_label,
            escalated=False,
            primary=primary,
            reverse=None,
        )
    if ba_raw is None:
        return RecoveredResolution(
            status="needs_reverse",
            final_label=None,
            escalated=True,
            primary=primary,
            reverse=None,
        )

    reverse_response = _as_response(ba_raw)
    reverse = (
        remap_judgment_to_canonical_order(reverse_response.to_evidence_value(), orientation="BA")
        if reverse_response is not None
        else None
    )
    reverse_label = (
        _binary_label(reverse, threshold=confidence_threshold) if reverse is not None else None
    )
    if reverse_label is None:
        return RecoveredResolution(
            status="unresolved_reverse",
            final_label=None,
            escalated=True,
            primary=primary,
            reverse=reverse,
        )

    low_primary_label: bool | None = None
    if primary is not None:
        if primary.answer == "same_claim":
            low_primary_label = True
        elif primary.answer == "not_same_claim":
            low_primary_label = False
    if low_primary_label is not None:
        if low_primary_label != reverse_label:
            return RecoveredResolution(
                status="conflict",
                final_label=None,
                escalated=True,
                primary=primary,
                reverse=reverse,
            )
        status: ResolutionStatus = "resolved_agreement"
    else:
        status = "resolved_reverse"
    return RecoveredResolution(
        status=status,
        final_label=reverse_label,
        escalated=True,
        primary=primary,
        reverse=reverse,
    )


@dataclass(frozen=True, slots=True)
class JudgeProcessCapture:
    status: Literal["completed", "timeout", "interrupted"]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    final_message: bytes | None


def _judge_response_schema() -> dict[str, object]:
    schema = cast(dict[str, object], JudgeResponse.model_json_schema(by_alias=True))
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
        raise RecoveredJudgeError("JudgeResponse schema lacks string properties")
    schema["required"] = sorted(cast(dict[str, object], properties))
    schema["additionalProperties"] = False
    return schema


def _codex_argv(
    *,
    prompt: str,
    output_schema_path: Path,
    final_message_path: Path,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> list[str]:
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--disable",
        "shell_tool",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-c",
        'web_search="disabled"',
        "-c",
        'shell_environment_policy.inherit="none"',
        "--skip-git-repo-check",
        "--json",
        "--output-schema",
        str(output_schema_path),
        "-o",
        str(final_message_path),
        prompt,
    ]


class CodexJudgeExecutor:
    """Shell-free Codex CLI executor with a positional prompt and closed stdin."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        output_schema_path: Path | None = None,
        termination_grace_seconds: int = 10,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.output_schema_path = output_schema_path
        self.termination_grace_seconds = termination_grace_seconds
        self._active_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen[str]] = set()

    def cancel_active(self) -> None:
        """Terminate every active process group before dispatch can resume."""

        with self._active_lock:
            processes = tuple(self._active_processes)
        for process in processes:
            _terminate_process_group(process, self.termination_grace_seconds)

    def execute(self, *, prompt: str, cwd: Path, timeout_seconds: int) -> JudgeProcessCapture:
        cwd.mkdir(parents=True, exist_ok=True)
        schema_path = self.output_schema_path or (cwd / "judge_response.schema.json")
        schema_payload = canonical_json_bytes(_judge_response_schema())
        if schema_path.exists() and schema_path.read_bytes() != schema_payload:
            raise RecoveredJudgeError(f"output-schema conflict: {schema_path}")
        if not schema_path.exists():
            schema_path.write_bytes(schema_payload)
        final_path = cwd / "final_message.json"
        if final_path.exists():
            raise RecoveredJudgeError(f"final-message path is not fresh: {final_path}")
        command = _codex_argv(
            prompt=prompt,
            output_schema_path=schema_path,
            final_message_path=final_path,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with self._active_lock:
            self._active_processes.add(process)
        status: Literal["completed", "timeout", "interrupted"] = "completed"
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                status = "timeout"
                _terminate_process_group(process, self.termination_grace_seconds)
                stdout, stderr = process.communicate()
            except KeyboardInterrupt:
                _terminate_process_group(process, self.termination_grace_seconds)
                process.communicate()
                raise
        finally:
            with self._active_lock:
                self._active_processes.discard(process)
        final = final_path.read_bytes() if final_path.is_file() else None
        return JudgeProcessCapture(
            status,
            process.returncode,
            stdout.encode("utf-8"),
            stderr.encode("utf-8"),
            final,
        )


def _terminate_process_group(process: subprocess.Popen[str], grace_seconds: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace_seconds)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class _JudgeExecutor(Protocol):
    def execute(self, *, prompt: str, cwd: Path, timeout_seconds: int) -> object: ...


class RecoveredJudgeConfig(StrictModel):
    schema_version: Literal[1] = 1
    method_version: Literal["recovered_singlepass_codex_v1"] = METHOD_VERSION
    repo_root: Path
    output_root: Path
    implementation_git_revision: str = Field(default="0" * 40, pattern=r"^[0-9a-f]{40}$")
    implementation_module_sha256: str = Field(
        default_factory=lambda: hash_file(Path(__file__)), pattern=HEX64_PATTERN
    )
    prompt_path: Path = DEFAULT_JUDGE_TEMPLATE
    prompt_sha256: str = Field(
        default_factory=lambda: hash_file(DEFAULT_JUDGE_TEMPLATE), pattern=HEX64_PATTERN
    )
    model: str = DEFAULT_MODEL
    reasoning_effort: Literal["medium"] = DEFAULT_REASONING_EFFORT
    confidence_threshold: float = Field(default=DEFAULT_CONFIDENCE_THRESHOLD, ge=0.0, le=1.0)
    seed: int = Field(default=DEFAULT_SEED, strict=True)
    expected_total: int | None = Field(default=None, ge=1, strict=True)
    pilot_per_proposer: int = Field(default=PILOT_PER_PROPOSER, ge=1, strict=True)
    batch_size: int = Field(default=PRODUCTION_BATCH_SIZE, ge=1, strict=True)
    audit_sample_size: int = Field(default=PRODUCTION_AUDIT_SAMPLE_SIZE, ge=1, strict=True)
    timeout_seconds: int = Field(default=1800, ge=1, strict=True)
    max_workers: int = Field(default=1, ge=1, le=32, strict=True)
    start_index: int = Field(default=0, ge=0, strict=True)
    count: int | None = Field(default=None, ge=1, strict=True)
    retry_incomplete_attempts: bool = False
    max_attempts_per_orientation: int = Field(default=3, ge=1, le=10, strict=True)
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _frozen(self) -> Self:
        if self.prompt_path.is_file() and hash_file(self.prompt_path) != self.prompt_sha256:
            raise ValueError("judge prompt hash mismatch")
        if self.confidence_threshold != DEFAULT_CONFIDENCE_THRESHOLD:
            raise ValueError("recovered judge confidence threshold is frozen at 0.75")
        if self.enforce_storage_root:
            storage_root = Path("/storage/milikic").resolve()
            try:
                self.output_root.resolve().relative_to(storage_root)
            except ValueError as exc:
                raise ValueError("production artifacts must live below /storage/milikic") from exc
        return self


def _verify_implementation_binding(config: RecoveredJudgeConfig) -> None:
    if config.implementation_git_revision == "0" * 40:
        return
    if _git_revision(config.repo_root) != config.implementation_git_revision:
        raise RecoveredJudgeError("implementation git revision differs from frozen config")
    if hash_file(Path(__file__)) != config.implementation_module_sha256:
        raise RecoveredJudgeError("judge module bytes differ from frozen config")


class _AttemptRequest(StrictModel):
    schema_version: Literal[1] = 1
    plan_row_id: str = Field(pattern=id_pattern("recovered_judge_plan_row"))
    orientation: Literal["AB", "BA"]
    attempt_index: int = Field(ge=0, strict=True)
    model: str
    reasoning_effort: Literal["medium"]
    argv: tuple[str, ...]
    prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    output_schema_sha256: str = Field(pattern=HEX64_PATTERN)
    timeout_seconds: int = Field(ge=1, strict=True)
    stdin_mode: Literal["DEVNULL"] = "DEVNULL"


class _CallTerminal(StrictModel):
    schema_version: Literal[1] = 1
    plan_row_id: str = Field(pattern=id_pattern("recovered_judge_plan_row"))
    orientation: Literal["AB", "BA"]
    attempt_index: int = Field(ge=0, strict=True)
    status: CallStatus
    request_sha256: str = Field(pattern=HEX64_PATTERN)
    exit_code: int | None
    stdout_sha256: str = Field(pattern=HEX64_PATTERN)
    stderr_sha256: str = Field(pattern=HEX64_PATTERN)
    final_message_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    parsed_visible_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    canonical_response_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    error: str | None = None

    @model_validator(mode="after")
    def _status_contract(self) -> Self:
        if self.status == "completed":
            if (
                self.exit_code != 0
                or self.final_message_sha256 is None
                or self.parsed_visible_sha256 is None
                or self.canonical_response_sha256 is None
                or self.error is not None
            ):
                raise ValueError("completed call lacks successful parsed artifacts")
        elif self.status == "parse_failed":
            if self.exit_code != 0 or self.final_message_sha256 is None or self.error is None:
                raise ValueError("parse-failed call lacks raw output/error")
            if self.parsed_visible_sha256 is not None or self.canonical_response_sha256 is not None:
                raise ValueError("parse-failed call cannot bind parsed artifacts")
        elif self.parsed_visible_sha256 is not None or self.canonical_response_sha256 is not None:
            raise ValueError("nonsemantic call cannot bind parsed artifacts")
        return self


class RecoveredJudgment(StrictModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=id_pattern("recovered_judgment"))
    plan_row_id: str = Field(pattern=id_pattern("recovered_judge_plan_row"))
    plan_index: int = Field(ge=0, strict=True)
    proposer: Proposer
    proposer_family_id: str
    pair_id: str
    variant_id: str
    group_key: str
    status: ResolutionStatus
    final_label: StrictBool | None
    escalated: bool
    primary: JudgmentValue | None
    reverse: JudgmentValue | None
    primary_call_status: CallStatus
    reverse_call_status: CallStatus | None
    primary_terminal_sha256: str = Field(pattern=HEX64_PATTERN)
    reverse_terminal_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _id_and_label(self) -> Self:
        if self.record_id != _judgment_id(self):
            raise ValueError("recovered judgment ID mismatch")
        resolved = self.status in {
            "resolved_primary",
            "resolved_reverse",
            "resolved_agreement",
        }
        if resolved != (self.final_label is not None):
            raise ValueError("judgment status/final label mismatch")
        if (self.reverse_call_status is None) != (self.reverse_terminal_sha256 is None):
            raise ValueError("reverse status/terminal binding mismatch")
        if self.escalated != (self.reverse_call_status is not None):
            raise ValueError("escalation must bind exactly one reverse call")
        return self


def _judgment_id(record: RecoveredJudgment) -> str:
    return make_id(
        "recovered_judgment",
        record.model_dump(mode="json", exclude={"record_id"}),
    )


class RecoveredJudgeManifest(StrictModel):
    schema_version: Literal[1] = 1
    method_version: Literal["recovered_singlepass_codex_v1"] = METHOD_VERSION
    total_pair_count: int = Field(ge=1, strict=True)
    selected_count: int = Field(ge=0, strict=True)
    completed_count: int = Field(ge=0, strict=True)
    resolved_count: int = Field(ge=0, strict=True)
    unresolved_count: int = Field(ge=0, strict=True)
    incomplete_count: int = Field(ge=0, strict=True)
    escalated_count: int = Field(ge=0, strict=True)
    primary_attempt_count: int = Field(ge=0, strict=True)
    reverse_attempt_count: int = Field(ge=0, strict=True)
    total_request_count: int = Field(ge=0, strict=True)
    incomplete_journal_count: int = Field(ge=0, strict=True)
    invoked_count: int = Field(ge=0, strict=True)
    reused_count: int = Field(ge=0, strict=True)
    label_counts: dict[str, int]
    resolution_status_counts: dict[str, int]
    call_status_counts: dict[str, int]
    plan_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_sha256: str = Field(pattern=HEX64_PATTERN)
    model: str
    reasoning_effort: Literal["medium"]


@dataclass(frozen=True, slots=True)
class RecoveredJudgeRunResult:
    manifest: RecoveredJudgeManifest
    judgments: tuple[RecoveredJudgment, ...]


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    terminal: _CallTerminal
    terminal_sha256: str
    response: JudgeResponse | None
    invoked: bool


@dataclass(frozen=True, slots=True)
class _JobOutcome:
    judgment: RecoveredJudgment | None
    invoked_count: int
    reused: bool


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _write_immutable(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise RecoveredJudgeError(f"immutable artifact conflict: {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise RecoveredJudgeError(f"concurrent immutable conflict: {path}") from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _plan_bytes(plan: RecoveredPlan) -> bytes:
    return b"".join(_canonical_line(row) for row in plan.execution_rows)


def _persist_plan(output_root: Path, plan: RecoveredPlan) -> str:
    rows_path = output_root / "inputs" / "pair_plan.jsonl"
    rows_hash = _write_immutable(rows_path, _plan_bytes(plan))
    manifest = plan.model_dump(mode="json", exclude={"rows", "remaining_rows"})
    manifest["pair_plan_sha256"] = rows_hash
    _write_immutable(
        output_root / "inputs" / "pair_plan_manifest.json",
        canonical_json_bytes(manifest) + b"\n",
    )
    return rows_hash


def load_plan(output_root: Path) -> RecoveredPlan:
    manifest_path = output_root / "inputs" / "pair_plan_manifest.json"
    rows_path = output_root / "inputs" / "pair_plan.jsonl"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveredJudgeError(f"invalid pair-plan manifest: {exc}") from exc
    if hash_file(rows_path) != manifest.pop("pair_plan_sha256", None):
        raise RecoveredJudgeError("pair-plan artifact hash mismatch")
    rows = tuple(
        RecoveredPlanRow.model_validate_json(line)
        for line in rows_path.read_bytes().splitlines()
        if line
    )
    pilot_count_value = manifest.get("pilot_pair_count")
    if not isinstance(pilot_count_value, int):
        raise RecoveredJudgeError("pair-plan manifest lacks pilot_pair_count")
    pilot_count = pilot_count_value
    try:
        return RecoveredPlan.model_validate(
            {
                **manifest,
                "rows": rows[:pilot_count],
                "remaining_rows": rows[pilot_count:],
            }
        )
    except ValueError as exc:
        raise RecoveredJudgeError(f"invalid pair plan: {exc}") from exc


def _schema_path(output_root: Path) -> Path:
    payload = canonical_json_bytes(_judge_response_schema())
    digest = sha256_hex(payload)
    path = output_root / "schemas" / f"judge_response.{digest}.schema.json"
    _write_immutable(path, payload)
    return path


def _item_dir(output_root: Path, row: RecoveredPlanRow) -> Path:
    digest = row.plan_row_id.removeprefix("recovered_judge_plan_row:")
    return output_root / "items" / digest[:2] / digest


@contextmanager
def _job_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _cancel_dispatch(
    *,
    cancel_event: threading.Event,
    executor: _JudgeExecutor,
    futures: Iterable[Future[_JobOutcome]] = (),
) -> None:
    cancel_event.set()
    for future in futures:
        future.cancel()
    if isinstance(executor, CodexJudgeExecutor):
        executor.cancel_active()


def _attempt_dirs(call_dir: Path) -> tuple[Path, ...]:
    attempts_dir = call_dir / "attempts"
    if not attempts_dir.exists():
        return ()
    paths = tuple(sorted(path for path in attempts_dir.iterdir() if path.is_dir()))
    if [path.name for path in paths] != [f"{index:04d}" for index in range(len(paths))]:
        raise RecoveredJudgeError(f"non-contiguous attempt directories: {attempts_dir}")
    return paths


def _load_call_terminal(
    path: Path,
    row: RecoveredPlanRow,
    *,
    orientation: Literal["AB", "BA"],
    attempt_index: int,
) -> _CallTerminal:
    try:
        terminal = _CallTerminal.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RecoveredJudgeError(f"invalid call terminal {path}: {exc}") from exc
    if (
        terminal.plan_row_id != row.plan_row_id
        or terminal.orientation != orientation
        or terminal.attempt_index != attempt_index
    ):
        raise RecoveredJudgeError(f"terminal does not bind plan row: {path}")
    return terminal


def _verify_hash(path: Path, expected: str | None, *, label: str) -> None:
    if expected is None:
        if path.exists():
            raise RecoveredJudgeError(f"unexpected {label} artifact: {path}")
        return
    if path.is_symlink() or not path.is_file() or hash_file(path) != expected:
        raise RecoveredJudgeError(f"{label} hash mismatch: {path}")


def _verify_request_journal(
    *,
    config: RecoveredJudgeConfig,
    row: RecoveredPlanRow,
    presentation: JudgePresentation,
    orientation: Literal["AB", "BA"],
    schema_path: Path,
    attempt_dir: Path,
) -> tuple[_AttemptRequest, str]:
    try:
        attempt_index = int(attempt_dir.name)
    except ValueError as exc:
        raise RecoveredJudgeError(f"invalid attempt directory: {attempt_dir}") from exc
    request_path = attempt_dir / "request.json"
    if request_path.is_symlink() or not request_path.is_file():
        raise RecoveredJudgeError(f"missing regular request journal: {request_path}")
    try:
        request = _AttemptRequest.model_validate_json(request_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RecoveredJudgeError(f"invalid request journal: {request_path}: {exc}") from exc
    rendered = render_blinded_judge_prompt(presentation, template_path=config.prompt_path)
    final_path = attempt_dir / "final_message.json"
    expected_argv = tuple(
        _codex_argv(
            prompt=rendered.text,
            output_schema_path=schema_path,
            final_message_path=final_path,
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        )
    )
    if (
        request.plan_row_id != row.plan_row_id
        or request.orientation != orientation
        or request.attempt_index != attempt_index
        or request.model != config.model
        or request.reasoning_effort != config.reasoning_effort
        or request.argv != expected_argv
        or request.prompt_sha256 != rendered.render_sha256
        or request.output_schema_sha256 != hash_file(schema_path)
        or request.timeout_seconds != config.timeout_seconds
    ):
        raise RecoveredJudgeError(f"request journal differs from frozen invocation: {request_path}")
    return request, hash_file(request_path)


def _verify_attempt(
    *,
    config: RecoveredJudgeConfig,
    row: RecoveredPlanRow,
    presentation: JudgePresentation,
    orientation: Literal["AB", "BA"],
    schema_path: Path,
    attempt_dir: Path,
) -> _CallOutcome:
    request, request_hash = _verify_request_journal(
        config=config,
        row=row,
        presentation=presentation,
        orientation=orientation,
        schema_path=schema_path,
        attempt_dir=attempt_dir,
    )
    attempt_index = request.attempt_index
    terminal_path = attempt_dir / "terminal.json"
    terminal = _load_call_terminal(
        terminal_path,
        row,
        orientation=orientation,
        attempt_index=attempt_index,
    )
    if terminal.request_sha256 != request_hash:
        raise RecoveredJudgeError(f"terminal/request hash mismatch: {terminal_path}")
    final_path = attempt_dir / "final_message.json"
    _verify_hash(attempt_dir / "stdout.jsonl", terminal.stdout_sha256, label="stdout")
    _verify_hash(attempt_dir / "stderr.txt", terminal.stderr_sha256, label="stderr")
    _verify_hash(final_path, terminal.final_message_sha256, label="final message")

    parsed_path = attempt_dir / "parsed_visible.json"
    canonical_path = attempt_dir / "canonical_response.json"
    response: JudgeResponse | None = None
    if terminal.status in {"completed", "parse_failed"}:
        if terminal.final_message_sha256 is None:
            raise RecoveredJudgeError(f"semantic terminal lacks final message: {terminal_path}")
        final_bytes = final_path.read_bytes()
        try:
            decoded = final_bytes.decode("utf-8")
            response = parse_blinded_judge_output(decoded)
        except (UnicodeDecodeError, JudgeOutputParseError):
            if terminal.status != "parse_failed":
                raise RecoveredJudgeError(
                    f"completed terminal raw output no longer parses: {terminal_path}"
                ) from None
            _verify_hash(parsed_path, None, label="parsed visible")
            _verify_hash(canonical_path, None, label="canonical response")
            return _CallOutcome(
                terminal=terminal,
                terminal_sha256=hash_file(terminal_path),
                response=None,
                invoked=False,
            )
        if terminal.status != "completed":
            raise RecoveredJudgeError(f"parse-failed terminal now parses: {terminal_path}")
        expected_visible = _canonical_line(response)
        if parsed_path.read_bytes() != expected_visible:
            raise RecoveredJudgeError(f"parsed response replay mismatch: {parsed_path}")
        _verify_hash(parsed_path, terminal.parsed_visible_sha256, label="parsed visible")
        canonical = remap_judgment_to_canonical_order(
            response.to_evidence_value(), orientation=orientation
        )
        if canonical_path.read_bytes() != _canonical_line(canonical):
            raise RecoveredJudgeError(f"canonical response replay mismatch: {canonical_path}")
        _verify_hash(
            canonical_path,
            terminal.canonical_response_sha256,
            label="canonical response",
        )
    else:
        _verify_hash(parsed_path, None, label="parsed visible")
        _verify_hash(canonical_path, None, label="canonical response")
    return _CallOutcome(
        terminal=terminal,
        terminal_sha256=hash_file(terminal_path),
        response=response,
        invoked=False,
    )


def _normalize_capture(value: object) -> JudgeProcessCapture:
    if isinstance(value, JudgeProcessCapture):
        return value
    if isinstance(value, str):
        return JudgeProcessCapture("completed", 0, b"", b"", value.encode("utf-8"))
    if isinstance(value, bytes):
        return JudgeProcessCapture("completed", 0, b"", b"", value)
    raise RecoveredJudgeError(f"judge executor returned unsupported type: {type(value).__name__}")


def _call_orientation(
    *,
    config: RecoveredJudgeConfig,
    row: RecoveredPlanRow,
    presentation: JudgePresentation,
    orientation: Literal["AB", "BA"],
    schema_path: Path,
    executor: _JudgeExecutor,
    cancel_event: threading.Event,
) -> _CallOutcome:
    if cancel_event.is_set():
        raise InterruptedError("recovered judge run was cancelled")
    item_dir = _item_dir(config.output_root, row)
    call_dir = item_dir / ("primary_ab" if orientation == "AB" else "reverse_ba")
    attempts = _attempt_dirs(call_dir)
    if attempts:
        last = attempts[-1]
        terminal_path = last / "terminal.json"
        if not terminal_path.is_file():
            if not config.retry_incomplete_attempts:
                raise RecoveredJudgeError(
                    "incomplete request journal requires explicit audited retry"
                )
        else:
            verified = _verify_attempt(
                config=config,
                row=row,
                presentation=presentation,
                orientation=orientation,
                schema_path=schema_path,
                attempt_dir=last,
            )
            terminal = verified.terminal
            if terminal.status == "completed":
                return verified
            if terminal.status == "parse_failed":
                return verified
            if not config.retry_incomplete_attempts:
                return verified
        if len(attempts) >= config.max_attempts_per_orientation:
            raise RecoveredJudgeError("explicit retry exceeds max_attempts_per_orientation")

    rendered = render_blinded_judge_prompt(presentation, template_path=config.prompt_path)
    attempt_index = len(attempts)
    attempt_dir = call_dir / "attempts" / f"{attempt_index:04d}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    final_path = attempt_dir / "final_message.json"
    argv = _codex_argv(
        prompt=rendered.text,
        output_schema_path=schema_path,
        final_message_path=final_path,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
    )
    request = _AttemptRequest(
        plan_row_id=row.plan_row_id,
        orientation=orientation,
        attempt_index=attempt_index,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        argv=tuple(argv),
        prompt_sha256=rendered.render_sha256,
        output_schema_sha256=hash_file(schema_path),
        timeout_seconds=config.timeout_seconds,
    )
    request_hash = _write_immutable(attempt_dir / "request.json", _canonical_line(request))
    if cancel_event.is_set():
        raise InterruptedError("recovered judge run was cancelled after request journal")
    capture = _normalize_capture(
        executor.execute(
            prompt=rendered.text,
            cwd=attempt_dir,
            timeout_seconds=config.timeout_seconds,
        )
    )

    stdout_hash = _write_immutable(attempt_dir / "stdout.jsonl", capture.stdout)
    stderr_hash = _write_immutable(attempt_dir / "stderr.txt", capture.stderr)
    final_hash: str | None = None
    visible_hash: str | None = None
    canonical_hash: str | None = None
    response: JudgeResponse | None = None
    error: str | None = None
    status: CallStatus
    if capture.final_message is not None:
        final_hash = _write_immutable(final_path, capture.final_message)
    if capture.status != "completed":
        status = capture.status
    elif capture.exit_code != 0:
        status = "process_failed"
    elif capture.final_message is None:
        status = "final_output_missing"
    else:
        try:
            response = parse_blinded_judge_output(capture.final_message.decode("utf-8"))
        except (UnicodeDecodeError, JudgeOutputParseError) as exc:
            status = "parse_failed"
            error = f"{type(exc).__name__}: {exc}"
        else:
            status = "completed"
            visible_hash = _write_immutable(
                attempt_dir / "parsed_visible.json", _canonical_line(response)
            )
            canonical = remap_judgment_to_canonical_order(
                response.to_evidence_value(), orientation=orientation
            )
            canonical_hash = _write_immutable(
                attempt_dir / "canonical_response.json", _canonical_line(canonical)
            )
    terminal = _CallTerminal(
        plan_row_id=row.plan_row_id,
        orientation=orientation,
        attempt_index=attempt_index,
        status=status,
        request_sha256=request_hash,
        exit_code=capture.exit_code,
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
        final_message_sha256=final_hash,
        parsed_visible_sha256=visible_hash,
        canonical_response_sha256=canonical_hash,
        error=error,
    )
    terminal_hash = _write_immutable(attempt_dir / "terminal.json", _canonical_line(terminal))
    return _CallOutcome(
        terminal=terminal,
        terminal_sha256=terminal_hash,
        response=response,
        invoked=True,
    )


def _make_judgment(
    *,
    row: RecoveredPlanRow,
    resolution: RecoveredResolution,
    primary_status: CallStatus,
    reverse_status: CallStatus | None,
    primary_terminal_sha256: str,
    reverse_terminal_sha256: str | None,
) -> RecoveredJudgment:
    values: dict[str, object] = {
        "schema_version": 1,
        "plan_row_id": row.plan_row_id,
        "plan_index": row.plan_index,
        "proposer": row.proposer,
        "proposer_family_id": row.proposer_family_id,
        "pair_id": row.audit_input.pair.pair_id,
        "variant_id": row.audit_input.variant_id,
        "group_key": row.group_key,
        "status": resolution.status,
        "final_label": resolution.final_label,
        "escalated": resolution.escalated,
        "primary": resolution.primary,
        "reverse": resolution.reverse,
        "primary_call_status": primary_status,
        "reverse_call_status": reverse_status,
        "primary_terminal_sha256": primary_terminal_sha256,
        "reverse_terminal_sha256": reverse_terminal_sha256,
    }
    temporary = RecoveredJudgment.model_construct(
        schema_version=1,
        record_id="",
        plan_row_id=row.plan_row_id,
        plan_index=row.plan_index,
        proposer=row.proposer,
        proposer_family_id=row.proposer_family_id,
        pair_id=row.audit_input.pair.pair_id,
        variant_id=row.audit_input.variant_id,
        group_key=row.group_key,
        status=resolution.status,
        final_label=resolution.final_label,
        escalated=resolution.escalated,
        primary=resolution.primary,
        reverse=resolution.reverse,
        primary_call_status=primary_status,
        reverse_call_status=reverse_status,
        primary_terminal_sha256=primary_terminal_sha256,
        reverse_terminal_sha256=reverse_terminal_sha256,
    )
    return RecoveredJudgment.model_validate({**values, "record_id": _judgment_id(temporary)})


def _load_judgment(path: Path, row: RecoveredPlanRow) -> RecoveredJudgment:
    try:
        judgment = RecoveredJudgment.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RecoveredJudgeError(f"invalid judgment {path}: {exc}") from exc
    if judgment.plan_row_id != row.plan_row_id or judgment.plan_index != row.plan_index:
        raise RecoveredJudgeError(f"judgment does not bind plan row: {path}")
    return judgment


def _verify_judgment_lineage(
    *,
    config: RecoveredJudgeConfig,
    row: RecoveredPlanRow,
    judgment: RecoveredJudgment,
    schema_path: Path,
) -> None:
    item_dir = _item_dir(config.output_root, row)
    primary_attempts = _attempt_dirs(item_dir / "primary_ab")
    if not primary_attempts:
        raise RecoveredJudgeError("persisted judgment lacks a primary attempt")
    primary = _verify_attempt(
        config=config,
        row=row,
        presentation=row.primary_presentation,
        orientation="AB",
        schema_path=schema_path,
        attempt_dir=primary_attempts[-1],
    )
    if (
        primary.terminal_sha256 != judgment.primary_terminal_sha256
        or primary.terminal.status != judgment.primary_call_status
        or primary.terminal.status not in {"completed", "parse_failed"}
    ):
        raise RecoveredJudgeError("judgment/primary terminal binding mismatch")
    initial = resolve_primary_and_optional_ba(
        primary.response,
        confidence_threshold=config.confidence_threshold,
    )
    reverse_attempts = _attempt_dirs(item_dir / "reverse_ba")
    reverse: _CallOutcome | None = None
    if judgment.escalated:
        if not reverse_attempts:
            raise RecoveredJudgeError("escalated judgment lacks a reverse attempt")
        reverse = _verify_attempt(
            config=config,
            row=row,
            presentation=row.reverse_presentation,
            orientation="BA",
            schema_path=schema_path,
            attempt_dir=reverse_attempts[-1],
        )
        if (
            reverse.terminal_sha256 != judgment.reverse_terminal_sha256
            or reverse.terminal.status != judgment.reverse_call_status
            or reverse.terminal.status not in {"completed", "parse_failed"}
        ):
            raise RecoveredJudgeError("judgment/reverse terminal binding mismatch")
        if reverse.response is None:
            resolution = RecoveredResolution(
                status="unresolved_reverse",
                final_label=None,
                escalated=True,
                primary=initial.primary,
                reverse=None,
            )
        else:
            resolution = resolve_primary_and_optional_ba(
                primary.response,
                reverse.response,
                confidence_threshold=config.confidence_threshold,
            )
    else:
        if reverse_attempts:
            raise RecoveredJudgeError("primary-only judgment unexpectedly has a reverse attempt")
        resolution = initial
    if (
        resolution.status != judgment.status
        or resolution.final_label != judgment.final_label
        or resolution.escalated != judgment.escalated
        or resolution.primary != judgment.primary
        or resolution.reverse != judgment.reverse
    ):
        raise RecoveredJudgeError("judgment differs from replayed provider artifacts")


def _run_job(
    *,
    config: RecoveredJudgeConfig,
    row: RecoveredPlanRow,
    schema_path: Path,
    executor: _JudgeExecutor,
    cancel_event: threading.Event,
) -> _JobOutcome:
    if cancel_event.is_set():
        raise InterruptedError("recovered judge run was cancelled")
    item_dir = _item_dir(config.output_root, row)
    with _job_lock(item_dir / ".lock"):
        _write_immutable(item_dir / "input.json", _canonical_line(row))
        judgment_path = item_dir / "judgment.json"
        if judgment_path.is_file():
            judgment = _load_judgment(judgment_path, row)
            _verify_judgment_lineage(
                config=config,
                row=row,
                judgment=judgment,
                schema_path=schema_path,
            )
            return _JobOutcome(
                judgment=judgment,
                invoked_count=0,
                reused=True,
            )
        primary = _call_orientation(
            config=config,
            row=row,
            presentation=row.primary_presentation,
            orientation="AB",
            schema_path=schema_path,
            executor=executor,
            cancel_event=cancel_event,
        )
        invoked = int(primary.invoked)
        if primary.terminal.status not in {"completed", "parse_failed"}:
            return _JobOutcome(judgment=None, invoked_count=invoked, reused=False)
        initial = resolve_primary_and_optional_ba(
            primary.response,
            confidence_threshold=config.confidence_threshold,
        )
        reverse: _CallOutcome | None = None
        if initial.status == "needs_reverse":
            reverse = _call_orientation(
                config=config,
                row=row,
                presentation=row.reverse_presentation,
                orientation="BA",
                schema_path=schema_path,
                executor=executor,
                cancel_event=cancel_event,
            )
            invoked += int(reverse.invoked)
            if reverse.terminal.status not in {"completed", "parse_failed"}:
                return _JobOutcome(judgment=None, invoked_count=invoked, reused=False)
            if reverse.response is None:
                resolution = RecoveredResolution(
                    status="unresolved_reverse",
                    final_label=None,
                    escalated=True,
                    primary=initial.primary,
                    reverse=None,
                )
            else:
                resolution = resolve_primary_and_optional_ba(
                    primary.response,
                    reverse.response,
                    confidence_threshold=config.confidence_threshold,
                )
        else:
            resolution = initial
        judgment = _make_judgment(
            row=row,
            resolution=resolution,
            primary_status=primary.terminal.status,
            reverse_status=reverse.terminal.status if reverse is not None else None,
            primary_terminal_sha256=primary.terminal_sha256,
            reverse_terminal_sha256=(reverse.terminal_sha256 if reverse is not None else None),
        )
        _write_immutable(judgment_path, _canonical_line(judgment))
        return _JobOutcome(judgment=judgment, invoked_count=invoked, reused=False)


def _attempt_statuses(output_root: Path) -> Counter[str]:
    statuses: Counter[str] = Counter()
    for path in output_root.glob("items/*/*/*/attempts/*/terminal.json"):
        try:
            terminal = _CallTerminal.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise RecoveredJudgeError(f"invalid terminal during manifest rebuild: {path}") from exc
        statuses[terminal.status] += 1
    return statuses


def _build_manifest(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    selected_count: int,
    invoked_count: int,
    reused_count: int,
    plan_sha256: str,
) -> tuple[RecoveredJudgeManifest, tuple[RecoveredJudgment, ...]]:
    judgments: list[RecoveredJudgment] = []
    incomplete = 0
    for row in plan.execution_rows:
        item_dir = _item_dir(config.output_root, row)
        judgment_path = item_dir / "judgment.json"
        if judgment_path.is_file():
            judgments.append(_load_judgment(judgment_path, row))
        elif (item_dir / "input.json").is_file():
            incomplete += 1
    labels: Counter[str] = Counter(
        "unresolved" if item.final_label is None else str(item.final_label).lower()
        for item in judgments
    )
    resolution_statuses = Counter(item.status for item in judgments)
    attempt_statuses = _attempt_statuses(config.output_root)
    primary_attempt_count = len(
        list(config.output_root.glob("items/*/*/primary_ab/attempts/*/request.json"))
    )
    reverse_attempt_count = len(
        list(config.output_root.glob("items/*/*/reverse_ba/attempts/*/request.json"))
    )
    request_paths = list(config.output_root.glob("items/*/*/*/attempts/*/request.json"))
    incomplete_journal_count = sum(
        not (path.parent / "terminal.json").is_file() for path in request_paths
    )
    resolved = sum(item.final_label is not None for item in judgments)
    manifest = RecoveredJudgeManifest(
        total_pair_count=plan.total_pair_count,
        selected_count=selected_count,
        completed_count=len(judgments),
        resolved_count=resolved,
        unresolved_count=len(judgments) - resolved,
        incomplete_count=incomplete,
        escalated_count=sum(item.escalated for item in judgments),
        primary_attempt_count=primary_attempt_count,
        reverse_attempt_count=reverse_attempt_count,
        total_request_count=len(request_paths),
        incomplete_journal_count=incomplete_journal_count,
        invoked_count=invoked_count,
        reused_count=reused_count,
        label_counts=dict(sorted(labels.items())),
        resolution_status_counts=dict(sorted(resolution_statuses.items())),
        call_status_counts=dict(sorted(attempt_statuses.items())),
        plan_sha256=plan_sha256,
        prompt_sha256=config.prompt_sha256,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
    )
    return manifest, tuple(judgments)


def _batch_spans(*, plan: RecoveredPlan, batch_size: int) -> tuple[tuple[str, int, int], ...]:
    spans: list[tuple[str, int, int]] = [
        (f"pilot_{plan.pilot_pair_count}", 0, plan.pilot_pair_count)
    ]
    start = plan.pilot_pair_count
    batch_index = 1
    while start < plan.total_pair_count:
        end = min(plan.total_pair_count, start + batch_size)
        spans.append((f"batch_{batch_index:04d}", start, end))
        start = end
        batch_index += 1
    return tuple(spans)


def _write_completed_batch_summaries(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    selected_start: int,
    selected_end: int,
) -> None:
    rows = plan.execution_rows
    for name, start, end in _batch_spans(plan=plan, batch_size=config.batch_size):
        if start < selected_start or end > selected_end:
            continue
        judgments: list[RecoveredJudgment] = []
        for row in rows[start:end]:
            path = _item_dir(config.output_root, row) / "judgment.json"
            if not path.is_file():
                break
            judgments.append(_load_judgment(path, row))
        if len(judgments) != end - start:
            continue
        summary = _batch_summary_payload(
            config=config,
            plan=plan,
            name=name,
            start=start,
            end=end,
            judgments=judgments,
        )
        _write_immutable(
            config.output_root / "batches" / name / "summary.json",
            canonical_json_bytes(summary) + b"\n",
        )


def _batch_summary_payload(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    name: str,
    start: int,
    end: int,
    judgments: Sequence[RecoveredJudgment],
) -> dict[str, object]:
    labels = Counter(
        "unresolved" if item.final_label is None else str(item.final_label).lower()
        for item in judgments
    )
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "batch_name": name,
        "start_index": start,
        "end_index_exclusive": end,
        "pair_count": end - start,
        "completed_count": len(judgments),
        "resolved_count": sum(item.final_label is not None for item in judgments),
        "unresolved_count": sum(item.final_label is None for item in judgments),
        "escalated_count": sum(item.escalated for item in judgments),
        "escalation_rate": sum(item.escalated for item in judgments) / len(judgments),
        "label_counts": dict(sorted(labels.items())),
        "ordered_plan_row_ids_sha256": hash_canonical(
            [row.plan_row_id for row in plan.execution_rows[start:end]]
        ),
        "ordered_judgment_ids_sha256": hash_canonical([item.record_id for item in judgments]),
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "confidence_threshold": config.confidence_threshold,
    }


def _verify_pilot_barrier(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    schema_path: Path,
) -> None:
    name, start, end = _batch_spans(plan=plan, batch_size=config.batch_size)[0]
    judgments: list[RecoveredJudgment] = []
    for row in plan.execution_rows[start:end]:
        judgment_path = _item_dir(config.output_root, row) / "judgment.json"
        if not judgment_path.is_file():
            raise RecoveredJudgeError(
                "the exact balanced pilot must complete before production batches"
            )
        judgment = _load_judgment(judgment_path, row)
        _verify_judgment_lineage(
            config=config,
            row=row,
            judgment=judgment,
            schema_path=schema_path,
        )
        judgments.append(judgment)
    expected = (
        canonical_json_bytes(
            _batch_summary_payload(
                config=config,
                plan=plan,
                name=name,
                start=start,
                end=end,
                judgments=judgments,
            )
        )
        + b"\n"
    )
    summary_path = config.output_root / "batches" / name / "summary.json"
    if (
        summary_path.is_symlink()
        or not summary_path.is_file()
        or summary_path.read_bytes() != expected
    ):
        raise RecoveredJudgeError(
            "verified immutable pilot summary is required before production batches"
        )


def run_recovered_judge(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    executor: _JudgeExecutor | None = None,
) -> RecoveredJudgeRunResult:
    """Run or resume the configured plan slice and rebuild its durable manifest."""

    _verify_implementation_binding(config)
    if config.expected_total is not None and plan.total_pair_count != config.expected_total:
        raise RecoveredJudgeError("configured expected total differs from the pair plan")
    plan_sha = _persist_plan(config.output_root, plan)
    schema_path = _schema_path(config.output_root)
    config_payload = _config_artifact_payload(config)
    _write_immutable(
        config.output_root / "run_config.json", canonical_json_bytes(config_payload) + b"\n"
    )
    rows = plan.execution_rows
    end = len(rows) if config.count is None else min(len(rows), config.start_index + config.count)
    if config.start_index >= len(rows):
        raise RecoveredJudgeError("start_index is outside the pair plan")
    if config.expected_total == PRODUCTION_PAIR_COUNT and end > plan.pilot_pair_count:
        _verify_pilot_barrier(config=config, plan=plan, schema_path=schema_path)
    selected = rows[config.start_index : end]
    active_executor: _JudgeExecutor = executor or CodexJudgeExecutor(
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        output_schema_path=schema_path,
    )
    invoked_count = 0
    reused_count = 0
    cancel_event = threading.Event()

    def record_progress(index: int, outcome: _JobOutcome) -> None:
        nonlocal invoked_count, reused_count
        invoked_count += outcome.invoked_count
        reused_count += int(outcome.reused)
        if index % 50 == 0:
            partial, _ = _build_manifest(
                config=config,
                plan=plan,
                selected_count=len(selected),
                invoked_count=invoked_count,
                reused_count=reused_count,
                plan_sha256=plan_sha,
            )
            _write_atomic(
                config.output_root / "run_manifest.json",
                canonical_json_bytes(partial.model_dump(mode="json")) + b"\n",
            )

    with _sigterm_as_interrupt():
        if config.max_workers == 1:
            try:
                for index, row in enumerate(selected, start=1):
                    outcome = _run_job(
                        config=config,
                        row=row,
                        schema_path=schema_path,
                        executor=active_executor,
                        cancel_event=cancel_event,
                    )
                    record_progress(index, outcome)
            except BaseException:
                _cancel_dispatch(cancel_event=cancel_event, executor=active_executor)
                raise
        else:
            pool = ThreadPoolExecutor(max_workers=config.max_workers)
            pending: set[Future[_JobOutcome]] = set()
            row_iterator = iter(selected)

            def submit_next() -> bool:
                try:
                    row = next(row_iterator)
                except StopIteration:
                    return False
                pending.add(
                    pool.submit(
                        _run_job,
                        config=config,
                        row=row,
                        schema_path=schema_path,
                        executor=active_executor,
                        cancel_event=cancel_event,
                    )
                )
                return True

            for _ in range(config.max_workers):
                if not submit_next():
                    break
            completed = 0
            try:
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    completed_outcomes: list[_JobOutcome] = []
                    for future in done:
                        pending.remove(future)
                        completed_outcomes.append(future.result())
                    for outcome in completed_outcomes:
                        completed += 1
                        record_progress(completed, outcome)
                        submit_next()
            except BaseException:
                _cancel_dispatch(
                    cancel_event=cancel_event,
                    executor=active_executor,
                    futures=pending,
                )
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True, cancel_futures=False)

    manifest, judgments = _build_manifest(
        config=config,
        plan=plan,
        selected_count=len(selected),
        invoked_count=invoked_count,
        reused_count=reused_count,
        plan_sha256=plan_sha,
    )
    _write_atomic(
        config.output_root / "run_manifest.json",
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )
    _write_completed_batch_summaries(
        config=config,
        plan=plan,
        selected_start=config.start_index,
        selected_end=end,
    )
    return RecoveredJudgeRunResult(manifest=manifest, judgments=judgments)


class _AuditSampleLike(Protocol):
    @property
    def record_id(self) -> str: ...

    @property
    def proposer(self) -> str: ...

    @property
    def final_label(self) -> bool | None: ...

    @property
    def escalated(self) -> bool: ...


class RecoveredTrainingRecord(StrictModel):
    """Exact eight-field trainer row emitted only for resolved judgments."""

    record_id: str = Field(min_length=1)
    reference_headless: str = Field(min_length=1)
    candidate_headless: str = Field(min_length=1)
    label: StrictBool
    group_key: str = Field(min_length=1)
    family: str
    source: Literal["lf022_recovered_codex_judge_v1"] = TRAINING_SOURCE
    weight: float = Field(default=1.0, gt=0.0)


def deterministic_audit_sample[T: _AuditSampleLike](
    records: Sequence[T], *, sample_size: int, seed: int = DEFAULT_SEED
) -> tuple[T, ...]:
    """Round-robin deterministic samples across proposer/label/escalation strata."""

    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if sample_size > len(records):
        raise ValueError("sample_size cannot exceed record count")
    groups: dict[tuple[str, object, bool], list[T]] = defaultdict(list)
    for record in records:
        proposer = record.proposer
        label = record.final_label
        escalated = record.escalated
        record_id = record.record_id
        if not isinstance(proposer, str) or not isinstance(escalated, bool):
            raise ValueError("audit records require proposer and escalated fields")
        if label is not None and not isinstance(label, bool):
            raise ValueError("audit record final_label must be bool or null")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("audit records require a nonempty record_id")
        groups[(proposer, label, escalated)].append(record)
    for key, values in groups.items():
        values.sort(
            key=lambda record: hash_canonical(
                {
                    "schema": "recovered_judge_audit_sample_v1",
                    "seed": seed,
                    "stratum": [key[0], key[1], key[2]],
                    "record_id": record.record_id,
                }
            )
        )
    ordered_keys = sorted(groups, key=lambda key: (key[0], str(key[1]), key[2]))
    selected: list[T] = []
    depth = 0
    while len(selected) < sample_size:
        added = False
        for key in ordered_keys:
            values = groups[key]
            if depth < len(values):
                selected.append(values[depth])
                added = True
                if len(selected) == sample_size:
                    break
        if not added:
            raise RecoveredJudgeError("audit sample allocation exhausted unexpectedly")
        depth += 1
    return tuple(selected)


def _attempt_inventory(attempt_dir: Path) -> tuple[dict[str, object], ...]:
    inventory: list[dict[str, object]] = []
    for current_text, directory_names, file_names in os.walk(attempt_dir, followlinks=False):
        current = Path(current_text)
        for name in directory_names:
            path = current / name
            if path.is_symlink() or not path.is_dir():
                raise RecoveredJudgeError(f"attempt contains a linked directory: {path}")
        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise RecoveredJudgeError(f"attempt contains a non-regular file: {path}")
            inventory.append(
                {
                    "path": path.relative_to(attempt_dir).as_posix(),
                    "byte_count": path.stat().st_size,
                    "sha256": hash_file(path),
                }
            )
    return tuple(sorted(inventory, key=lambda item: cast(str, item["path"])))


def _build_attempt_ledger(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    schema_path: Path,
) -> tuple[tuple[dict[str, object], ...], Counter[str]]:
    ledger: list[dict[str, object]] = []
    statuses: Counter[str] = Counter()
    for row in plan.execution_rows:
        orientations: tuple[tuple[Literal["AB", "BA"], JudgePresentation, str], ...] = (
            ("AB", row.primary_presentation, "primary_ab"),
            ("BA", row.reverse_presentation, "reverse_ba"),
        )
        for orientation, presentation, directory_name in orientations:
            for attempt_dir in _attempt_dirs(_item_dir(config.output_root, row) / directory_name):
                request, request_sha = _verify_request_journal(
                    config=config,
                    row=row,
                    presentation=presentation,
                    orientation=orientation,
                    schema_path=schema_path,
                    attempt_dir=attempt_dir,
                )
                terminal_path = attempt_dir / "terminal.json"
                terminal_sha: str | None = None
                status: str
                if terminal_path.is_file():
                    outcome = _verify_attempt(
                        config=config,
                        row=row,
                        presentation=presentation,
                        orientation=orientation,
                        schema_path=schema_path,
                        attempt_dir=attempt_dir,
                    )
                    status = outcome.terminal.status
                    terminal_sha = outcome.terminal_sha256
                else:
                    status = "incomplete_journal"
                statuses[status] += 1
                inventory = _attempt_inventory(attempt_dir)
                ledger.append(
                    {
                        "schema_version": 1,
                        "plan_row_id": row.plan_row_id,
                        "plan_index": row.plan_index,
                        "orientation": orientation,
                        "attempt_index": request.attempt_index,
                        "status": status,
                        "ambiguous_paid_call": status == "incomplete_journal",
                        "request_sha256": request_sha,
                        "terminal_sha256": terminal_sha,
                        "artifact_inventory": list(inventory),
                        "artifact_inventory_sha256": hash_canonical(list(inventory)),
                    }
                )
    return tuple(ledger), statuses


def finalize_recovered_judge(
    *,
    config: RecoveredJudgeConfig,
    plan: RecoveredPlan,
    require_complete: bool = True,
) -> dict[str, object]:
    """Freeze ordered judgments, resolved trainer rows, and the audit sample."""

    _verify_implementation_binding(config)
    plan_path = config.output_root / "inputs" / "pair_plan.jsonl"
    if not plan_path.is_file():
        raise RecoveredJudgeError("pair plan must be materialized before finalization")
    manifest, judgments = _build_manifest(
        config=config,
        plan=plan,
        selected_count=plan.total_pair_count,
        invoked_count=0,
        reused_count=0,
        plan_sha256=hash_file(plan_path),
    )
    if require_complete and manifest.completed_count != plan.total_pair_count:
        raise RecoveredJudgeError(
            f"cannot finalize: {manifest.completed_count}/{plan.total_pair_count} "
            "judgments complete"
        )
    ordered = tuple(sorted(judgments, key=lambda item: item.plan_index))
    schema_path = _schema_path(config.output_root)
    rows_by_id = {row.plan_row_id: row for row in plan.execution_rows}
    for judgment in ordered:
        _verify_judgment_lineage(
            config=config,
            row=rows_by_id[judgment.plan_row_id],
            judgment=judgment,
            schema_path=schema_path,
        )
    attempt_ledger, attempt_statuses = _build_attempt_ledger(
        config=config,
        plan=plan,
        schema_path=schema_path,
    )
    attempt_ledger_path = config.output_root / "outputs" / "attempt_ledger.jsonl"
    _write_immutable(
        attempt_ledger_path,
        b"".join(canonical_json_bytes(item) + b"\n" for item in attempt_ledger),
    )
    response_artifact_set_sha256 = hash_canonical(
        {
            "attempt_ledger_sha256": hash_file(attempt_ledger_path),
            "judgment_terminals": [
                {
                    "record_id": item.record_id,
                    "primary_terminal_sha256": item.primary_terminal_sha256,
                    "reverse_terminal_sha256": item.reverse_terminal_sha256,
                }
                for item in ordered
            ],
        }
    )
    judgment_path = config.output_root / "outputs" / "judgments.jsonl"
    _write_immutable(judgment_path, b"".join(_canonical_line(item) for item in ordered))
    trainer_rows = tuple(
        RecoveredTrainingRecord(
            record_id=item.record_id,
            reference_headless=rows_by_id[item.plan_row_id].reference_headless,
            candidate_headless=rows_by_id[item.plan_row_id].candidate_headless,
            label=item.final_label,
            group_key=item.group_key,
            family=item.proposer_family_id,
            weight=1.0,
        )
        for item in ordered
        if item.final_label is not None
    )
    trainer_path = config.output_root / "outputs" / "trainer_records.jsonl"
    _write_immutable(trainer_path, b"".join(_canonical_line(item) for item in trainer_rows))

    sample_size = min(config.audit_sample_size, len(ordered))
    sample = deterministic_audit_sample(ordered, sample_size=sample_size, seed=config.seed)
    sample_rows = []
    for sample_index, judgment in enumerate(sample):
        row = rows_by_id[judgment.plan_row_id]
        sample_rows.append(
            {
                "schema_version": 1,
                "sample_index": sample_index,
                "record_id": judgment.record_id,
                "plan_row_id": row.plan_row_id,
                "proposer": row.proposer,
                "proposer_family_id": row.proposer_family_id,
                "pair_id": row.audit_input.pair.pair_id,
                "reference_headless": row.reference_headless,
                "candidate_headless": row.candidate_headless,
                "final_label": judgment.final_label,
                "status": judgment.status,
                "escalated": judgment.escalated,
                "primary": (
                    judgment.primary.model_dump(mode="json")
                    if judgment.primary is not None
                    else None
                ),
                "reverse": (
                    judgment.reverse.model_dump(mode="json")
                    if judgment.reverse is not None
                    else None
                ),
            }
        )
    sample_path = config.output_root / "outputs" / f"audit_sample_{sample_size}.jsonl"
    _write_immutable(
        sample_path,
        b"".join(canonical_json_bytes(item) + b"\n" for item in sample_rows),
    )
    sample_key = {
        "schema_version": 1,
        "method_version": "recovered_judge_audit_sample_v1",
        "seed": config.seed,
        "sample_size": sample_size,
        "strata": ["proposer", "final_label", "escalated"],
        "ordered_record_ids_sha256": hash_canonical([item.record_id for item in sample]),
        "source_judgments_sha256": hash_file(judgment_path),
    }
    sample_key_path = config.output_root / "outputs" / "audit_sample_key.json"
    _write_immutable(sample_key_path, canonical_json_bytes(sample_key) + b"\n")

    final = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": (
            "completed" if manifest.completed_count == plan.total_pair_count else "incomplete"
        ),
        "config_sha256": hash_file(config.output_root / "run_config.json"),
        "pair_plan_sha256": hash_file(plan_path),
        "prompt_sha256": config.prompt_sha256,
        "implementation_git_revision": config.implementation_git_revision,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "confidence_threshold": config.confidence_threshold,
        "response_artifact_set_sha256": response_artifact_set_sha256,
        "counts": {
            "planned": plan.total_pair_count,
            "judged": len(ordered),
            "resolved": len(trainer_rows),
            "unresolved": len(ordered) - len(trainer_rows),
            "escalated": sum(item.escalated for item in ordered),
            "audit_sample": sample_size,
            "provider_request_journals": len(attempt_ledger),
            "ambiguous_paid_calls": attempt_statuses["incomplete_journal"],
        },
        "attempt_status_counts": dict(sorted(attempt_statuses.items())),
        "escalation_rate": (
            sum(item.escalated for item in ordered) / len(ordered) if ordered else None
        ),
        "label_counts": manifest.label_counts,
        "resolution_status_counts": manifest.resolution_status_counts,
        "outputs": {
            "judgments": {
                "path": str(judgment_path),
                "sha256": hash_file(judgment_path),
            },
            "trainer_records": {
                "path": str(trainer_path),
                "sha256": hash_file(trainer_path),
            },
            "audit_sample": {
                "path": str(sample_path),
                "sha256": hash_file(sample_path),
            },
            "audit_sample_key": {
                "path": str(sample_key_path),
                "sha256": hash_file(sample_key_path),
            },
            "attempt_ledger": {
                "path": str(attempt_ledger_path),
                "sha256": hash_file(attempt_ledger_path),
            },
        },
    }
    final_path = config.output_root / "final_manifest.json"
    _write_immutable(final_path, canonical_json_bytes(final) + b"\n")
    return final


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser("materialize", help="freeze the production pair plan")
    materialize.add_argument("--output-root", type=Path, required=True)
    materialize.add_argument("--repo-root", type=Path, default=Path.cwd())
    for name in ("pilot", "run", "continue", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--output-root", type=Path, required=True)
        if name in {"pilot", "run", "continue"}:
            command.add_argument("--workers", type=int, default=1)
            command.add_argument("--retry-incomplete-attempts", action="store_true")
        if name == "run":
            command.add_argument("--start-index", type=int, required=True)
            command.add_argument("--count", type=int, required=True)
    return parser


def _load_base_config(output_root: Path) -> RecoveredJudgeConfig:
    config_path = output_root / "run_config.json"
    try:
        return RecoveredJudgeConfig.model_validate_json(config_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise RecoveredJudgeError(f"invalid run config: {config_path}: {exc}") from exc


def _invocation_config(
    base: RecoveredJudgeConfig,
    *,
    start_index: int,
    count: int,
    workers: int,
    retry_incomplete_attempts: bool,
) -> RecoveredJudgeConfig:
    return RecoveredJudgeConfig.model_validate(
        {
            **base.model_dump(mode="json"),
            "start_index": start_index,
            "count": count,
            "max_workers": workers,
            "retry_incomplete_attempts": retry_incomplete_attempts,
        }
    )


def main() -> None:
    args = _main_parser().parse_args()
    if args.command == "materialize":
        config, plan = materialize_production_plan(
            repo_root=args.repo_root.resolve(), output_root=args.output_root.resolve()
        )
        print(
            json.dumps(
                {
                    "completed_utc": _utc_now(),
                    "output_root": str(config.output_root),
                    "pair_count": plan.total_pair_count,
                    "pilot_pair_count": plan.pilot_pair_count,
                    "pair_plan_sha256": hash_file(
                        config.output_root / "inputs" / "pair_plan.jsonl"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    output_root = args.output_root.resolve()
    plan = load_plan(output_root)
    base = _load_base_config(output_root)
    if args.command == "finalize":
        print(json.dumps(finalize_recovered_judge(config=base, plan=plan), indent=2))
        return
    ranges: tuple[tuple[int, int], ...]
    if args.command == "pilot":
        spans = _batch_spans(plan=plan, batch_size=base.batch_size)
        _, start, end = spans[0]
        ranges = ((start, end),)
    elif args.command == "continue":
        ranges = tuple(
            (start, end)
            for _name, start, end in _batch_spans(plan=plan, batch_size=base.batch_size)[1:]
        )
    else:
        ranges = ((args.start_index, args.start_index + args.count),)

    for start, requested_end in ranges:
        end = min(requested_end, plan.total_pair_count)
        config = _invocation_config(
            base,
            start_index=start,
            count=end - start,
            workers=args.workers,
            retry_incomplete_attempts=args.retry_incomplete_attempts,
        )
        result = run_recovered_judge(config=config, plan=plan)
        print(
            json.dumps(
                {
                    "completed_utc": _utc_now(),
                    "start_index": start,
                    "end_index_exclusive": end,
                    **result.manifest.model_dump(mode="json"),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        missing = [
            row.plan_row_id
            for row in plan.execution_rows[start:end]
            if not (_item_dir(output_root, row) / "judgment.json").is_file()
        ]
        if missing:
            raise RecoveredJudgeError(
                f"batch remains incomplete after execution: {len(missing)} rows"
            )


if __name__ == "__main__":
    main()
