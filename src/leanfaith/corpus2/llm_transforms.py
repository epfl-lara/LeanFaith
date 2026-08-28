"""Track D-3: LLM transform generation with trusted self-labels.

Prompts an external LLM (codex / claude / lemex) to rewrite a headless Lean 4
statement with an explicitly chosen consistency-PRESERVING or consistency-BREAKING
transformation drawn from the TRANSFORM_CATALOG_V2 taxonomy, and trusts the model's
self-label (subject to later typecheck + stratified audits, per PLAN.md Track D-3).

New-regime reproducibility: no attestation. Pilot runs emit a plain manifest; the
Codex-only scale path additionally freezes per-job source/family/prompt bindings,
journals each call for resume, Lean-checks every parsed rewrite, and emits trainer rows.

GOLD RULE (enforced in code, see :func:`build_fewshots`): few-shot examples may come
ONLY from ``partition == "golden_train"`` pairs of the canonical golden pairs file.
Pairs from any other partition (dev / final_test / quarantine) are dropped at load
time and never rendered into a prompt.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.representations.views import signature_near_dup_hash

GOLDEN_PAIRS_PATH = Path("/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl")
MATHLIB_REPRESENTATIONS_PATH = Path("data/representations/mathlib.jsonl")
DEFAULT_OUTPUT_ROOT = Path("/storage/milikic/leanfaith/lf023_llm_transforms/pilot_v1")
FULL_MATHLIB_REPRESENTATIONS_PATH = Path(
    "/storage/milikic/leanfaith/scale_dc29fe6d4038/"
    "public_mathlib_repr_v3/run_a/records/mathlib.jsonl"
)
FULL_MATHLIB_THEOREMS_PATH = Path(
    "/storage/milikic/leanfaith/immutable/extractions/"
    "mathlib_d568c8c_manifest_b1831204/theorems/mathlib.jsonl"
)
GOLDEN_PARTITION_PATH = Path("data/benchmarks/golden_partition_v1.json")
GOLDEN_BLOCKLIST_PATH = Path("data/benchmarks/golden_blocklist_v1.json")
MATHLIB_PROJECT_PATH = Path("/storage/milikic/leanfaith/mathlib4")
FULL_MATHLIB_REVISION = "d568c8c09630de097a046763c17b9ea99f95f950"
DEFAULT_SCALE_OUTPUT_ROOT = Path("/storage/milikic/leanfaith/lf023_llm_transforms/codex_scale_v1")

FEWSHOT_PARTITION = "golden_train"
FEWSHOT_PROVENANCE = "expert_human"
DIRECTIONS = ("preserve", "break")
PROVIDERS = ("codex", "claude", "lemex")
PROVIDER_TIMEOUT_SECONDS = 240
MIN_STATEMENT_CHARS = 60
MAX_STATEMENT_CHARS = 600
SCALE_STATEMENT_COUNT = 200
SCALE_CODEX_WORKERS = 4
SCALE_CODEX_TIMEOUT_SECONDS = 600
CODEX_MODEL = "gpt-5.6-sol"
LEAN_MEMORY_MB = 24_576
LEAN_BATCH_SIZE = 25
LEAN_TIMEOUT_SECONDS = 600

_PRESERVE_MENU = (
    "currying/uncurrying of hypotheses (`A -> B -> C` <-> `A /\\ B -> C`)",
    "contrapositive (`A -> B` <-> `not B -> not A`)",
    "De Morgan rewrites and double-negation insertion/removal",
    "quantifier motion over independent binders (`A -> forall x, B x` <-> `forall x, A -> B x`)",
    "definitional fold/unfold of a transparent definition",
    "associativity/commutativity (AC) rewrites backed by standard lemmas",
    "extensionality expansion (funext / set ext) of an equality",
    "iff decomposition (`A <-> B` <-> `(A -> B) /\\ (B -> A)`)",
)

_BREAK_MENU = (
    "negate exactly one atom that influences the claim",
    "swap a logical connective (e.g. `/\\` <-> `\\/`, `->` <-> `<->`)",
    "drop or weaken a hypothesis that the claim actually depends on",
    "flip an inequality direction (`<` <-> `>`, `<=` <-> `>=`, or `<` <-> `<=` where it matters)",
    "perturb a numeric constant or exponent with real semantic effect",
    "swap quantifier scopes (`forall x, exists y` <-> `exists y, forall x`)",
)


@dataclass(frozen=True, slots=True)
class FamilySpec:
    """One explicit Track D-3 family assignment from the v2 catalog."""

    family_id: str
    direction: str
    instruction: str


PRESERVE_FAMILIES = tuple(
    FamilySpec(family_id, "preserve", instruction)
    for family_id, instruction in zip(
        ("P23", "P27", "P29", "P31", "P20", "P32", "P36", "P28"),
        _PRESERVE_MENU,
        strict=True,
    )
)
BREAK_FAMILIES = tuple(
    FamilySpec(family_id, "break", instruction)
    for family_id, instruction in zip(
        ("N21", "N22", "N24", "N25", "N26", "N23"),
        _BREAK_MENU,
        strict=True,
    )
)
FAMILIES_BY_DIRECTION = {
    "preserve": PRESERVE_FAMILIES,
    "break": BREAK_FAMILIES,
}

_JSON_SCHEMA_LINE = (
    '{"rewritten_statement": str, "intended_label": "consistent"|"inconsistent", '
    '"transformation": str (short family name), "reasoning": str (<=60 words), '
    '"confidence": float 0-1}'
)

REQUIRED_OUTPUT_KEYS = (
    "rewritten_statement",
    "intended_label",
    "transformation",
    "reasoning",
    "confidence",
)


class GoldRuleViolation(RuntimeError):
    """Raised when a few-shot candidate is not a golden_train pair."""


@dataclass(frozen=True)
class FewShot:
    """One rendered few-shot calibration example (golden_train only)."""

    pair_id: str
    original: str
    rewritten: str
    verdict: str  # "consistent" | "inconsistent"

    def render(self) -> str:
        return f"ORIGINAL: {self.original}\nREWRITTEN: {self.rewritten}\nVERDICT: {self.verdict}"


@dataclass(frozen=True)
class SourceStatement:
    """One source statement sampled from the representations file."""

    statement_id: str
    content_hash: str
    headless: str
    theorem_id: str = ""
    group_key: str = ""
    source_file: str = ""
    source_range_start: int | None = None


@dataclass
class ProviderCall:
    """Result of one external-LLM subprocess invocation."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    fallback_used: bool = False
    elapsed_seconds: float = 0.0
    invocation_error: str | None = None


@dataclass
class TransformRecord:
    """One pilot record: provider x source statement x direction."""

    provider: str
    index: int
    statement_id: str
    statement_hash: str
    source_statement: str
    direction: str
    assigned_family: str | None
    prompt_sha256: str
    raw_stdout_path: str
    returncode: int | None
    timed_out: bool
    fallback_used: bool
    parse_ok: bool
    parse_error: str | None = None
    rewritten_statement: str | None = None
    intended_label: str | None = None
    transformation: str | None = None
    reasoning: str | None = None
    confidence: float | None = None
    label_matches_direction: bool | None = None
    family_matches_assignment: bool | None = None
    rewrite_changed: bool | None = None
    invocation_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# Few-shots (GOLD RULE enforced here)
# ---------------------------------------------------------------------------


def verify_golden_partition_freeze(
    pairs_path: Path,
    partition_manifest_path: Path,
) -> dict[str, str]:
    """Bind the few-shot file to the frozen canonical partition manifest."""
    raw = json.loads(partition_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != "golden_partition_v1":
        raise GoldRuleViolation("partition manifest is not golden_partition_v1")
    expected_hash = raw.get("canonical_pairs_sha256")
    actual_hash = _sha256_file(pairs_path)
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise GoldRuleViolation(
            f"golden pairs hash {actual_hash} does not match frozen hash {expected_hash!r}"
        )
    raw_groups = raw.get("group_partitions")
    if not isinstance(raw_groups, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_groups.items()
    ):
        raise GoldRuleViolation("partition manifest has invalid group_partitions")
    return {str(key): str(value) for key, value in raw_groups.items()}


def load_golden_train_pairs(pairs_path: Path) -> list[dict[str, Any]]:
    """Load ONLY partition=="golden_train" pairs from the canonical golden file.

    Every other partition (dev / final_test / quarantine) is discarded the moment
    its partition field is read; its statement text is never retained.
    """
    kept: list[dict[str, Any]] = []
    with pairs_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row: dict[str, Any] = json.loads(line)
            if row.get("partition") != FEWSHOT_PARTITION:
                continue  # never retain dev/final_test/quarantine content
            kept.append(row)
    for row in kept:
        if row.get("partition") != FEWSHOT_PARTITION:  # defense in depth
            raise GoldRuleViolation(
                f"non-{FEWSHOT_PARTITION} pair leaked into few-shot pool: {row.get('pair_id')}"
            )
    return kept


def build_fewshots(
    pairs_path: Path,
    seed: int,
    k_pos: int = 3,
    k_neg: int = 3,
    partition_manifest_path: Path | None = None,
) -> list[FewShot]:
    """Deterministically sample k_pos consistent + k_neg inconsistent few-shots.

    Eligible pairs: partition=="golden_train" AND label_conflict==False AND
    label_provenance=="expert_human". Sampling is seeded and order-independent
    (pools are sorted by pair_id before sampling).
    """
    frozen_groups = (
        verify_golden_partition_freeze(pairs_path, partition_manifest_path)
        if partition_manifest_path is not None
        else None
    )
    pool = load_golden_train_pairs(pairs_path)
    if frozen_groups is not None:
        for row in pool:
            group_key = row.get("group_key")
            if not isinstance(group_key, str) or frozen_groups.get(group_key) != FEWSHOT_PARTITION:
                raise GoldRuleViolation(
                    f"pair {row.get('pair_id')} is not bound to a frozen golden_train group"
                )
    eligible = [
        row
        for row in pool
        if row.get("label_conflict") is False and row.get("label_provenance") == FEWSHOT_PROVENANCE
    ]
    positives = sorted(
        (r for r in eligible if r.get("label") is True), key=lambda r: str(r["pair_id"])
    )
    negatives = sorted(
        (r for r in eligible if r.get("label") is False), key=lambda r: str(r["pair_id"])
    )
    if len(positives) < k_pos or len(negatives) < k_neg:
        raise ValueError(
            f"not enough eligible golden_train pairs: {len(positives)} positive "
            f"(need {k_pos}), {len(negatives)} negative (need {k_neg})"
        )
    rng = random.Random(seed)
    chosen = rng.sample(positives, k_pos) + rng.sample(negatives, k_neg)

    fewshots: list[FewShot] = []
    for row in chosen:
        if row.get("partition") != FEWSHOT_PARTITION:  # GOLD RULE assertion
            raise GoldRuleViolation(
                f"few-shot pair {row.get('pair_id')} has partition "
                f"{row.get('partition')!r}, expected {FEWSHOT_PARTITION!r}"
            )
        fewshots.append(
            FewShot(
                pair_id=str(row["pair_id"]),
                original=str(row["reference_headless"]),
                rewritten=str(row["candidate_headless"]),
                verdict="consistent" if row["label"] is True else "inconsistent",
            )
        )
    return fewshots


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_prompt(
    source_statement: str,
    direction: str,
    fewshots: Sequence[FewShot],
    assigned_family: FamilySpec | None = None,
) -> str:
    """Build the full instruction prompt for one rewrite request."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    if assigned_family is not None and assigned_family.direction != direction:
        raise ValueError(
            f"family {assigned_family.family_id} has direction {assigned_family.direction!r}, "
            f"not {direction!r}"
        )
    menu: tuple[str, ...]
    if direction == "preserve":
        goal = (
            "rewrite it so the mathematical claim is PROVABLY EQUIVALENT to the original "
            "(a consistency-PRESERVING transformation). The rewrite must state the same "
            "mathematical fact, only expressed differently."
        )
        menu_title = "Choose ONE transformation from this consistency-preserving menu:"
        menu = _PRESERVE_MENU
        label_instruction = 'Your intended_label MUST be "consistent".'
    else:
        goal = (
            "rewrite it so the mathematical claim is SUBTLY DIFFERENT from the original "
            "(a consistency-BREAKING transformation). The rewrite must NOT be equivalent "
            "to the original, but the change must be semantic and non-obvious — not a "
            "mere renaming or reformatting."
        )
        menu_title = "Choose ONE transformation from this consistency-breaking menu:"
        menu = _BREAK_MENU
        label_instruction = 'Your intended_label MUST be "inconsistent".'

    if assigned_family is None:
        family_instruction = f"{menu_title}\n" + "\n".join(f"- {item}" for item in menu)
        transformation_requirement = "Name the one family you applied in `transformation`."
        family_action_requirement = "Apply exactly one transformation family from the menu."
    else:
        family_instruction = (
            "ASSIGNED TRANSFORMATION (apply this family only):\n"
            f"- {assigned_family.family_id}: {assigned_family.instruction}"
        )
        transformation_requirement = (
            f'The `transformation` value MUST be exactly "{assigned_family.family_id}". '
            "If the assigned family truly has no valid application site, return the original "
            "statement unchanged; it will be rejected rather than silently reassigned."
        )
        family_action_requirement = (
            "Apply exactly the assigned transformation family and no other semantic transformation."
        )
    fewshot_blocks = "\n\n".join(shot.render() for shot in fewshots)

    return f"""You are an expert in Lean 4 and mathlib. Use only the text supplied in this
prompt. Do not call tools, inspect files, browse, or run commands.

TASK: You are given a headless Lean 4 theorem statement (binders and goal, no `theorem`
keyword, no name, no proof). Your job is to {goal}

{family_instruction}

REQUIREMENTS:
- The rewritten statement must remain a well-typed, headless Lean 4 statement in
  mathlib style (same shape: binders followed by `:` and the goal). It must elaborate
  against the supplied source context (built on `import Mathlib`) in the pinned checkout.
- Keep it self-contained: do not invent constants that do not exist in mathlib.
- {family_action_requirement}
- {transformation_requirement}
- {label_instruction}

CALIBRATION EXAMPLES: The following are real, expert-judged statement pairs showing
what "consistent" (same mathematical claim) and "inconsistent" (different claim) mean.
They are judged examples for calibrating the verdict semantics — NOT demonstrations of
the transformations in the menu.

{fewshot_blocks}

INPUT STATEMENT:
{source_statement}

OUTPUT FORMAT: After any reasoning, the LAST line of your reply must be a single
strict JSON object, exactly this schema, no markdown fence, no trailing text:
{_JSON_SCHEMA_LINE}"""


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _iter_json_candidates(text: str) -> list[dict[str, Any]]:
    """Yield JSON objects decodable at any '{' position, scanning from the end."""
    decoder = json.JSONDecoder()
    # Strip markdown fences so `raw_decode` sees clean JSON tails as well.
    cleaned = text.replace("```json", "\n").replace("```", "\n")
    found: list[dict[str, Any]] = []
    for source in (text, cleaned):
        starts = [i for i, ch in enumerate(source) if ch == "{"]
        for start in reversed(starts):
            try:
                obj, _ = decoder.raw_decode(source[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                found.append(obj)
        if found:
            break
    return found


def parse_llm_output(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the last valid transform-JSON object from provider stdout.

    Returns ``(parsed, None)`` on success or ``(None, error)`` on failure.
    Robust to markdown fences and trailing/leading prose; prefers the last
    JSON object that carries the required schema keys.
    """
    candidates = _iter_json_candidates(stdout)
    if not candidates:
        return None, "no JSON object found in stdout"

    schema_hits = [c for c in candidates if all(k in c for k in REQUIRED_OUTPUT_KEYS)]
    if not schema_hits:
        return None, (
            "JSON found but missing required keys; saw keys: "
            + ", ".join(sorted(candidates[0].keys()))
        )
    obj = schema_hits[0]  # candidates are ordered last-first

    if not isinstance(obj["rewritten_statement"], str) or not obj["rewritten_statement"].strip():
        return None, "rewritten_statement missing or not a non-empty string"
    if obj["intended_label"] not in ("consistent", "inconsistent"):
        return None, f"invalid intended_label: {obj['intended_label']!r}"
    if not isinstance(obj["transformation"], str):
        return None, "transformation is not a string"
    if not isinstance(obj["reasoning"], str):
        return None, "reasoning is not a string"
    conf = obj["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, int | float):
        return None, f"confidence is not a number: {conf!r}"
    conf_f = float(conf)
    if not 0.0 <= conf_f <= 1.0:
        return None, f"confidence out of [0,1]: {conf_f}"
    obj["confidence"] = conf_f
    return obj, None


def expected_label(direction: str) -> str:
    return "consistent" if direction == "preserve" else "inconsistent"


# ---------------------------------------------------------------------------
# Source statement sampling
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePoolStats:
    """Mechanical accounting for the full public-source projection."""

    theorem_rows: int
    representation_rows: int
    joined_rows: int
    headless_ok_rows: int
    length_eligible_rows: int
    transform_eligible_rows: int
    blocked_source_rows: int
    duplicate_source_rows: int
    eligible_unique_rows: int


@dataclass(frozen=True, slots=True)
class _TheoremSourceInfo:
    theorem_id: str
    group_key: str
    source_file: str
    source_range_start: int
    transform_source_eligible: bool


def _source_selection_rank(statement: SourceStatement, seed: int) -> str:
    payload = {
        "schema": "d3_source_selection_rank_v1",
        "seed": seed,
        "group_key": statement.group_key,
        "theorem_id": statement.theorem_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_scale_source_statements(
    reprs_path: Path,
    theorems_path: Path,
    blocklist_path: Path,
    seed: int,
    *,
    expected_source_revision: str | None = FULL_MATHLIB_REVISION,
) -> tuple[list[SourceStatement], SourcePoolStats]:
    """Project the full public repr store and join each row to its ancestry root."""
    blocklist = GoldenBlocklist.load(blocklist_path)
    theorem_info: dict[str, _TheoremSourceInfo] = {}
    theorem_rows = 0
    with theorems_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            theorem_rows += 1
            outer = json.loads(line)
            theorem = outer.get("theorem")
            if not isinstance(theorem, dict):
                raise ValueError(f"theorem row {theorem_rows} lacks nested theorem object")
            theorem_id = theorem.get("theorem_id")
            roots = theorem.get("root_ancestry_ids")
            parents = theorem.get("parent_theorem_ids")
            metadata = theorem.get("metadata")
            source_range = theorem.get("source_range")
            source_file = theorem.get("source_file")
            if not isinstance(theorem_id, str) or not theorem_id:
                raise ValueError(f"theorem row {theorem_rows} has invalid theorem_id")
            if theorem_id in theorem_info:
                raise ValueError(f"duplicate theorem_id in source extraction: {theorem_id}")
            if theorem.get("source") != "mathlib":
                raise ValueError(
                    f"D-3 external generation accepts only public mathlib: {theorem_id}"
                )
            if (
                expected_source_revision is not None
                and theorem.get("source_revision") != expected_source_revision
            ):
                raise ValueError(f"unexpected mathlib revision for {theorem_id}")
            if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], str):
                raise ValueError(f"source theorem must have one ancestry root: {theorem_id}")
            if parents != []:
                raise ValueError(f"D-3 source theorem is not a root: {theorem_id}")
            if not isinstance(metadata, dict):
                raise ValueError(f"source theorem lacks metadata: {theorem_id}")
            if (
                not isinstance(source_range, list)
                or len(source_range) != 2
                or not all(isinstance(value, int) for value in source_range)
                or source_range[0] <= 0
                or source_range[1] < source_range[0]
            ):
                raise ValueError(f"source theorem lacks a valid source range: {theorem_id}")
            if not isinstance(source_file, str) or not source_file:
                raise ValueError(f"source theorem lacks a source file: {theorem_id}")
            theorem_info[theorem_id] = _TheoremSourceInfo(
                theorem_id=theorem_id,
                group_key=roots[0],
                source_file=source_file,
                source_range_start=source_range[0],
                transform_source_eligible=metadata.get("transform_source_eligible") is True,
            )

    representation_rows = 0
    joined_rows = 0
    headless_ok_rows = 0
    length_eligible_rows = 0
    transform_eligible_rows = 0
    blocked_source_rows = 0
    candidates_by_near_dup: dict[str, SourceStatement] = {}
    duplicate_source_rows = 0
    seen_representation_theorem_ids: set[str] = set()
    with reprs_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            representation_rows += 1
            row = json.loads(line)
            theorem_id = row.get("theorem_id")
            info = theorem_info.get(theorem_id) if isinstance(theorem_id, str) else None
            if info is None:
                continue
            if theorem_id in seen_representation_theorem_ids:
                raise ValueError(f"duplicate theorem_id in representation store: {theorem_id}")
            seen_representation_theorem_ids.add(theorem_id)
            joined_rows += 1
            headless = row.get("headless")
            view_status = row.get("view_status")
            if (
                not isinstance(headless, str)
                or not isinstance(view_status, dict)
                or view_status.get("headless") != "ok"
            ):
                continue
            headless_ok_rows += 1
            if not MIN_STATEMENT_CHARS <= len(headless) <= MAX_STATEMENT_CHARS:
                continue
            length_eligible_rows += 1
            if not info.transform_source_eligible:
                continue
            transform_eligible_rows += 1
            near_dup = signature_near_dup_hash(headless)
            if near_dup in blocklist.near_dup_hashes or blocklist.problem_is_blocked(
                info.group_key
            ):
                blocked_source_rows += 1
                continue
            representation_id = row.get("representation_id")
            content_hash = row.get("content_hash")
            if not isinstance(representation_id, str) or not representation_id:
                raise ValueError(f"representation has blank id: {theorem_id}")
            if not isinstance(content_hash, str) or not content_hash:
                raise ValueError(f"representation has blank content hash: {theorem_id}")
            statement = SourceStatement(
                statement_id=representation_id,
                content_hash=content_hash,
                headless=headless,
                theorem_id=info.theorem_id,
                group_key=info.group_key,
                source_file=info.source_file,
                source_range_start=info.source_range_start,
            )
            existing = candidates_by_near_dup.get(near_dup)
            if existing is None:
                candidates_by_near_dup[near_dup] = statement
            else:
                duplicate_source_rows += 1
                if _source_selection_rank(statement, seed) < _source_selection_rank(existing, seed):
                    candidates_by_near_dup[near_dup] = statement

    if (
        joined_rows != representation_rows
        or joined_rows != theorem_rows
        or seen_representation_theorem_ids != set(theorem_info)
    ):
        raise ValueError(
            "representation/theorem join is not total: "
            f"representations={representation_rows}, theorems={theorem_rows}, joined={joined_rows}"
        )
    pool = sorted(
        candidates_by_near_dup.values(),
        key=lambda statement: (_source_selection_rank(statement, seed), statement.statement_id),
    )
    stats = SourcePoolStats(
        theorem_rows=theorem_rows,
        representation_rows=representation_rows,
        joined_rows=joined_rows,
        headless_ok_rows=headless_ok_rows,
        length_eligible_rows=length_eligible_rows,
        transform_eligible_rows=transform_eligible_rows,
        blocked_source_rows=blocked_source_rows,
        duplicate_source_rows=duplicate_source_rows,
        eligible_unique_rows=len(pool),
    )
    return pool, stats


def load_source_statements(reprs_path: Path) -> list[SourceStatement]:
    """Load headless statements in the [MIN, MAX] char range from a representations file."""
    out: list[SourceStatement] = []
    with reprs_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            headless = row.get("headless")
            if not isinstance(headless, str):
                continue
            if not MIN_STATEMENT_CHARS <= len(headless) <= MAX_STATEMENT_CHARS:
                continue
            out.append(
                SourceStatement(
                    statement_id=str(row.get("representation_id", "")),
                    content_hash=str(row.get("content_hash", "")),
                    headless=headless,
                )
            )
    return out


def sample_source_statements(
    reprs_path: Path,
    seed: int,
    n_per_provider: int,
    providers: Sequence[str] = PROVIDERS,
) -> dict[str, list[SourceStatement]]:
    """Deterministic seeded sample: DIFFERENT statements per provider, no overlap."""
    pool = sorted(load_source_statements(reprs_path), key=lambda s: s.statement_id)
    total = n_per_provider * len(providers)
    if len(pool) < total:
        raise ValueError(
            f"only {len(pool)} statements in range "
            f"[{MIN_STATEMENT_CHARS},{MAX_STATEMENT_CHARS}] chars, need {total}"
        )
    rng = random.Random(seed)
    picked = rng.sample(pool, total)
    return {
        provider: picked[i * n_per_provider : (i + 1) * n_per_provider]
        for i, provider in enumerate(providers)
    }


def direction_for_index(index: int) -> str:
    """Alternate preserve/break per index within a provider (even=preserve)."""
    return DIRECTIONS[index % 2]


def family_for_index(index: int) -> FamilySpec:
    """Assign one family deterministically while balancing each direction's menu."""
    if index < 0:
        raise ValueError("index must be non-negative")
    direction = direction_for_index(index)
    menu = FAMILIES_BY_DIRECTION[direction]
    return menu[(index // len(DIRECTIONS)) % len(menu)]


# ---------------------------------------------------------------------------
# Provider invocation
# ---------------------------------------------------------------------------


def provider_command(provider: str, prompt: str, claude_model: str | None) -> list[str]:
    if provider == "codex":
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
            CODEX_MODEL,
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            'web_search="disabled"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "--skip-git-repo-check",
            prompt,
        ]
    if provider == "claude":
        cmd = ["claude", "-p", prompt]
        if claude_model is not None:
            cmd += ["--model", claude_model]
        return cmd
    if provider == "lemex":
        return [
            "lemex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'model_reasoning_effort="high"',
            "--skip-git-repo-check",
            prompt,
        ]
    raise ValueError(f"unknown provider: {provider!r}")


def run_provider(
    provider: str,
    prompt: str,
    timeout: int = PROVIDER_TIMEOUT_SECONDS,
    cwd: str = "/tmp",
    claude_model: str | None = "claude-opus-5",
) -> ProviderCall:
    """Invoke one provider CLI via subprocess; capture stdout. Never raises on failure.

    For claude, a bad --model id falls back to the CLI's default model once.
    """

    def _run(cmd: list[str]) -> ProviderCall:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ProviderCall(
                proc.returncode,
                proc.stdout,
                proc.stderr,
                elapsed_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return ProviderCall(
                None,
                stdout,
                stderr,
                timed_out=True,
                elapsed_seconds=time.monotonic() - started,
            )
        except OSError as exc:
            return ProviderCall(
                None,
                "",
                "",
                elapsed_seconds=time.monotonic() - started,
                invocation_error=f"{type(exc).__name__}: {exc}",
            )

    call = _run(provider_command(provider, prompt, claude_model))
    if (
        provider == "claude"
        and claude_model is not None
        and not call.timed_out
        and call.returncode not in (0, None)
    ):
        fallback = _run(provider_command(provider, prompt, None))
        fallback.fallback_used = True
        return fallback
    return call


# ---------------------------------------------------------------------------
# Pilot run
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_rev(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except OSError:
        return "unknown"


def _git_is_clean(repo_root: Path) -> bool:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and not proc.stdout.strip()


def make_record(
    provider: str,
    index: int,
    statement: SourceStatement,
    direction: str,
    prompt: str,
    call: ProviderCall,
    raw_stdout_path: Path,
    assigned_family: FamilySpec | None = None,
) -> TransformRecord:
    """Assemble one pilot record from a completed provider call."""
    record = TransformRecord(
        provider=provider,
        index=index,
        statement_id=statement.statement_id,
        statement_hash=statement.content_hash,
        source_statement=statement.headless,
        direction=direction,
        assigned_family=assigned_family.family_id if assigned_family is not None else None,
        prompt_sha256=_sha256_text(prompt),
        raw_stdout_path=str(raw_stdout_path),
        returncode=call.returncode,
        timed_out=call.timed_out,
        fallback_used=call.fallback_used,
        parse_ok=False,
        invocation_error=call.invocation_error,
    )
    if call.invocation_error is not None:
        record.parse_error = f"provider invocation failed: {call.invocation_error}"
        return record
    if call.timed_out:
        record.parse_error = "provider timed out"
        return record
    if call.returncode != 0:
        record.parse_error = f"provider exited with return code {call.returncode}"
        return record
    parsed, error = parse_llm_output(call.stdout)
    if parsed is None:
        record.parse_error = error
        return record
    record.parse_ok = True
    record.rewritten_statement = str(parsed["rewritten_statement"])
    record.intended_label = str(parsed["intended_label"])
    record.transformation = str(parsed["transformation"])
    record.reasoning = str(parsed["reasoning"])
    record.confidence = float(parsed["confidence"])
    record.label_matches_direction = record.intended_label == expected_label(direction)
    record.family_matches_assignment = (
        assigned_family is None or record.transformation == assigned_family.family_id
    )
    record.rewrite_changed = " ".join(record.rewritten_statement.split()) != " ".join(
        record.source_statement.split()
    )
    return record


def provider_stats(records: Sequence[TransformRecord]) -> dict[str, Any]:
    """Aggregate per-provider pilot statistics."""
    parsed = [r for r in records if r.parse_ok]
    label_dist: dict[str, int] = {}
    for r in parsed:
        assert r.intended_label is not None
        label_dist[r.intended_label] = label_dist.get(r.intended_label, 0) + 1
    confidences = [r.confidence for r in parsed if r.confidence is not None]
    deltas = [
        len(r.rewritten_statement) - len(r.source_statement)
        for r in parsed
        if r.rewritten_statement is not None
    ]
    return {
        "n": len(records),
        "provider_processes_started": sum(r.invocation_error is None for r in records),
        "provider_invocation_errors": sum(r.invocation_error is not None for r in records),
        "provider_exit_zero": sum(r.returncode == 0 for r in records),
        "provider_nonzero_exit": sum(
            r.returncode is not None and r.returncode != 0 for r in records
        ),
        "parse_ok": len(parsed),
        "timeouts": sum(1 for r in records if r.timed_out),
        "label_matches_direction": sum(1 for r in parsed if r.label_matches_direction),
        "intended_label_distribution": label_dist,
        "mean_confidence": round(float(statistics.mean(confidences)), 4) if confidences else None,
        "mean_rewritten_length_delta": (
            round(float(statistics.mean(deltas)), 1) if deltas else None
        ),
    }


@dataclass
class PilotConfig:
    """Configuration for one pilot run."""

    pairs_path: Path = GOLDEN_PAIRS_PATH
    reprs_path: Path = MATHLIB_REPRESENTATIONS_PATH
    output_root: Path = DEFAULT_OUTPUT_ROOT
    seed: int = 20260828
    n_per_provider: int = 10
    k_pos: int = 3
    k_neg: int = 3
    providers: tuple[str, ...] = PROVIDERS
    claude_model: str | None = "claude-opus-5"
    max_workers: int = 6
    timeout: int = PROVIDER_TIMEOUT_SECONDS
    repo_root: Path = field(default_factory=Path.cwd)


def run_pilot(config: PilotConfig) -> dict[str, Any]:
    """Execute the full pilot: sample, prompt, invoke providers, write outputs."""
    raw_dir = config.output_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    fewshots = build_fewshots(config.pairs_path, config.seed, config.k_pos, config.k_neg)
    samples = sample_source_statements(
        config.reprs_path, config.seed, config.n_per_provider, config.providers
    )

    jobs: list[tuple[str, int, SourceStatement, str, FamilySpec, str]] = []
    for provider in config.providers:
        for index, statement in enumerate(samples[provider]):
            direction = direction_for_index(index)
            family = family_for_index(index)
            prompt = build_prompt(statement.headless, direction, fewshots, family)
            jobs.append((provider, index, statement, direction, family, prompt))

    def _execute(
        job: tuple[str, int, SourceStatement, str, FamilySpec, str],
    ) -> TransformRecord:
        provider, index, statement, direction, family, prompt = job
        call = run_provider(
            provider, prompt, timeout=config.timeout, claude_model=config.claude_model
        )
        stdout_path = raw_dir / f"{provider}_{index:02d}.stdout.txt"
        stdout_path.write_text(call.stdout, encoding="utf-8")
        stderr_path = raw_dir / f"{provider}_{index:02d}.stderr.txt"
        stderr_path.write_text(call.stderr, encoding="utf-8")
        return make_record(
            provider,
            index,
            statement,
            direction,
            prompt,
            call,
            stdout_path,
            family,
        )

    with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
        records = list(pool.map(_execute, jobs))
    records.sort(key=lambda r: (r.provider, r.index))

    records_path = config.output_root / "records.jsonl"
    with records_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")

    manifest: dict[str, Any] = {
        "track": "D-3 LLM transforms pilot",
        "regime": "new (plain run manifest; no attestation)",
        "git_rev": _git_rev(config.repo_root),
        "seed": config.seed,
        "config": {
            "pairs_path": str(config.pairs_path),
            "pairs_sha256": _sha256_file(config.pairs_path),
            "reprs_path": str(config.reprs_path),
            "reprs_sha256": _sha256_file(config.reprs_path),
            "n_per_provider": config.n_per_provider,
            "k_pos": config.k_pos,
            "k_neg": config.k_neg,
            "providers": list(config.providers),
            "claude_model": config.claude_model,
            "timeout_seconds": config.timeout,
            "statement_char_range": [MIN_STATEMENT_CHARS, MAX_STATEMENT_CHARS],
            "direction_policy": "alternating preserve/break per index (even=preserve)",
        },
        "fewshot_pair_ids": [shot.pair_id for shot in fewshots],
        "fewshot_partition": FEWSHOT_PARTITION,
        "records_path": str(records_path),
        "per_provider_stats": {
            provider: provider_stats([r for r in records if r.provider == provider])
            for provider in config.providers
        },
    }
    manifest_path = config.output_root / "pilot_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# Codex-only D-3 scale run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScaleJob:
    """One frozen source/family/prompt binding for the 200-call run."""

    job_id: str
    index: int
    statement: SourceStatement
    direction: str
    family: FamilySpec
    prompt: str
    prompt_sha256: str
    family_heuristic_matched: bool

    def plan_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "job_id": self.job_id,
            "provider": "codex",
            "index": self.index,
            "statement_id": self.statement.statement_id,
            "statement_hash": self.statement.content_hash,
            "theorem_id": self.statement.theorem_id,
            "group_key": self.statement.group_key,
            "source_file": self.statement.source_file,
            "source_range_start": self.statement.source_range_start,
            "source_statement": self.statement.headless,
            "direction": self.direction,
            "assigned_family": self.family.family_id,
            "family_instruction": self.family.instruction,
            "family_heuristic_matched": self.family_heuristic_matched,
            "prompt_sha256": self.prompt_sha256,
        }


@dataclass(frozen=True, slots=True)
class LeanCheckResult:
    """One resumable elaboration result for a generated rewrite."""

    job_id: str
    candidate_sha256: str | None
    status: str
    candidate_near_dup_hash: str | None = None
    candidate_blocked: bool = False
    returncode: int | None = None
    timed_out: bool = False
    batch_source_path: str | None = None
    batch_source_sha256: str | None = None
    memory_hard_limit_mb: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScaleConfig:
    """Configuration for the Codex-only, Lean-checked D-3 scale run."""

    pairs_path: Path = GOLDEN_PAIRS_PATH
    partition_manifest_path: Path = GOLDEN_PARTITION_PATH
    reprs_path: Path = FULL_MATHLIB_REPRESENTATIONS_PATH
    theorems_path: Path = FULL_MATHLIB_THEOREMS_PATH
    blocklist_path: Path = GOLDEN_BLOCKLIST_PATH
    output_root: Path = DEFAULT_SCALE_OUTPUT_ROOT
    mathlib_project: Path = MATHLIB_PROJECT_PATH
    seed: int = 20260828
    count: int = SCALE_STATEMENT_COUNT
    k_pos: int = 3
    k_neg: int = 3
    max_workers: int = SCALE_CODEX_WORKERS
    timeout: int = SCALE_CODEX_TIMEOUT_SECONDS
    lean_batch_size: int = LEAN_BATCH_SIZE
    lean_memory_mb: int = LEAN_MEMORY_MB
    lean_timeout: int = LEAN_TIMEOUT_SECONDS
    repo_root: Path = field(default_factory=Path.cwd)
    expected_source_revision: str | None = FULL_MATHLIB_REVISION
    enforce_storage_root: bool = True
    retry_incomplete_attempts: bool = False


def _family_likely_applicable(family_id: str, statement: str) -> bool:
    arrows = statement.count("→") + statement.count("->")
    if family_id == "P23":
        adjacent_named_hypotheses = re.search(r"\(\s*h\w*\s*:[^()\n]+\)\s*\(\s*h\w*\s*:", statement)
        return adjacent_named_hypotheses is not None
    if family_id == "P27":
        return arrows >= 1
    if family_id == "P29":
        return any(
            token in statement
            for token in ("¬", "∧", "∨", "→", "↔", "not ")  # noqa: RUF001
        )
    if family_id == "P31":
        return arrows >= 1 or any(token in statement for token in ("∀", "∃", "forall "))
    if family_id == "P20":
        return True
    if family_id == "P32":
        return any(
            token in statement
            for token in (" + ", " * ", " ∧ ", " ∨ ", " ∪ ", " ∩ ")  # noqa: RUF001
        )
    if family_id == "P36":
        return " = " in statement or "Set " in statement or "FunLike" in statement
    if family_id == "P28":
        return "↔" in statement or "<->" in statement
    if family_id == "N21":
        return True
    if family_id == "N22":
        return any(
            token in statement
            for token in ("∧", "∨", "→", "↔", "/\\", "\\/")  # noqa: RUF001
        )
    if family_id == "N24":
        return arrows >= 1 or statement.lstrip().startswith(("(", "[", "{"))
    if family_id == "N25":
        return any(token in statement for token in (" < ", " > ", " ≤ ", " ≥ ", " <= ", " >= "))
    if family_id == "N26":
        return any(character.isdigit() for character in statement)
    if family_id == "N23":
        universal = "∀" in statement or "forall " in statement
        existential = "∃" in statement or "exists " in statement
        return universal and existential
    raise ValueError(f"unknown D-3 family: {family_id}")


def build_scale_jobs(
    pool: Sequence[SourceStatement],
    fewshots: Sequence[FewShot],
    count: int,
) -> list[ScaleJob]:
    """Select distinct full-store sources and bind one explicit family to each."""
    if count <= 0:
        raise ValueError("scale count must be positive")
    if len(pool) < count:
        raise ValueError(f"full source pool has {len(pool)} rows, need {count}")
    used: set[str] = set()
    jobs: list[ScaleJob] = []
    for index in range(count):
        family = family_for_index(index)
        statement = next(
            (
                candidate
                for candidate in pool
                if candidate.statement_id not in used
                and _family_likely_applicable(family.family_id, candidate.headless)
            ),
            None,
        )
        heuristic_matched = statement is not None
        if statement is None:
            statement = next(candidate for candidate in pool if candidate.statement_id not in used)
        used.add(statement.statement_id)
        direction = family.direction
        prompt = build_prompt(statement.headless, direction, fewshots, family)
        prompt_hash = _sha256_text(prompt)
        binding = {
            "schema": "d3_scale_job_v1",
            "index": index,
            "statement_id": statement.statement_id,
            "direction": direction,
            "assigned_family": family.family_id,
            "prompt_sha256": prompt_hash,
        }
        binding_hash = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        jobs.append(
            ScaleJob(
                job_id=f"d3_codex_{index:04d}_{binding_hash[:16]}",
                index=index,
                statement=statement,
                direction=direction,
                family=family,
                prompt=prompt,
                prompt_sha256=prompt_hash,
                family_heuristic_matched=heuristic_matched,
            )
        )
    return jobs


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _canonical_jsonl_bytes(rows: Sequence[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(row) for row in rows)


def _write_immutable(path: Path, payload: bytes) -> None:
    """Create a content-bound artifact once; an identical replay is allowed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ValueError(f"immutable artifact conflict: {path}") from None
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object: {path}")
    return raw


def _codex_cli_version() -> str:
    try:
        process = subprocess.run(
            ["codex", "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return process.stdout.strip() if process.returncode == 0 else "unknown"


def _load_scale_terminal(path: Path, job: ScaleJob) -> TransformRecord:
    terminal = _read_json_object(path)
    if (
        terminal.get("job_id") != job.job_id
        or terminal.get("prompt_sha256") != job.prompt_sha256
        or terminal.get("statement_id") != job.statement.statement_id
        or terminal.get("assigned_family") != job.family.family_id
    ):
        raise ValueError(f"terminal binding mismatch: {path}")
    for kind in ("stdout", "stderr"):
        artifact_path = Path(str(terminal.get(f"raw_{kind}_path", "")))
        expected_hash = terminal.get(f"raw_{kind}_sha256")
        if not artifact_path.is_file() or _sha256_file(artifact_path) != expected_hash:
            raise ValueError(f"terminal {kind} artifact mismatch: {path}")
    record = terminal.get("record")
    if not isinstance(record, dict):
        raise ValueError(f"terminal lacks record: {path}")
    loaded = TransformRecord(**record)
    if (
        loaded.provider != "codex"
        or loaded.index != job.index
        or loaded.statement_id != job.statement.statement_id
        or loaded.statement_hash != job.statement.content_hash
        or loaded.source_statement != job.statement.headless
        or loaded.direction != job.direction
        or loaded.assigned_family != job.family.family_id
        or loaded.prompt_sha256 != job.prompt_sha256
        or loaded.raw_stdout_path != terminal.get("raw_stdout_path")
    ):
        raise ValueError(f"terminal record mismatch: {path}")
    return loaded


def _execute_scale_job(config: ScaleConfig, job: ScaleJob) -> tuple[TransformRecord, bool]:
    item_dir = config.output_root / "items" / job.job_id
    item_dir.mkdir(parents=True, exist_ok=True)
    claim_path = item_dir / "claim.lock"
    with claim_path.open("a+b") as claim:
        fcntl.flock(claim.fileno(), fcntl.LOCK_EX)
        return _execute_scale_job_locked(config, job, item_dir)


def _execute_scale_job_locked(
    config: ScaleConfig,
    job: ScaleJob,
    item_dir: Path,
) -> tuple[TransformRecord, bool]:
    terminal_path = item_dir / "terminal.json"
    if terminal_path.exists():
        return _load_scale_terminal(terminal_path, job), False
    prompt_path = item_dir / "prompt.txt"
    request_path = item_dir / "request.json"
    _write_immutable(prompt_path, job.prompt.encode("utf-8"))
    _write_immutable(request_path, _canonical_json_bytes(job.plan_json()))
    attempts_dir = item_dir / "attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    existing_attempts = sorted(
        int(path.name) for path in attempts_dir.iterdir() if path.is_dir() and path.name.isdigit()
    )
    if existing_attempts and not config.retry_incomplete_attempts:
        raise ValueError(
            f"job {job.job_id} has an incomplete provider attempt; refusing an implicit "
            "duplicate paid call (resume with retry_incomplete_attempts only after audit)"
        )
    attempt_index = (existing_attempts[-1] + 1) if existing_attempts else 0
    attempt_dir = attempts_dir / f"{attempt_index:03d}"
    stdout_path = attempt_dir / "stdout.txt"
    stderr_path = attempt_dir / "stderr.txt"
    attempt_request = {
        "schema_version": 1,
        "job_id": job.job_id,
        "attempt_index": attempt_index,
        "prompt_sha256": job.prompt_sha256,
        "started_utc": datetime.now(UTC).isoformat(),
    }
    _write_immutable(attempt_dir / "request.json", _canonical_json_bytes(attempt_request))
    call = run_provider(
        "codex",
        job.prompt,
        timeout=config.timeout,
        cwd="/tmp",
        claude_model=None,
    )
    stdout_bytes = call.stdout.encode("utf-8")
    stderr_bytes = call.stderr.encode("utf-8")
    _write_immutable(stdout_path, stdout_bytes)
    _write_immutable(stderr_path, stderr_bytes)
    record = make_record(
        "codex",
        job.index,
        job.statement,
        job.direction,
        job.prompt,
        call,
        stdout_path,
        job.family,
    )
    terminal = {
        "schema_version": 1,
        "job_id": job.job_id,
        "provider": "codex",
        "statement_id": job.statement.statement_id,
        "assigned_family": job.family.family_id,
        "prompt_sha256": job.prompt_sha256,
        "completed_utc": datetime.now(UTC).isoformat(),
        "attempt_index": attempt_index,
        "provider_call": {
            "returncode": call.returncode,
            "timed_out": call.timed_out,
            "fallback_used": call.fallback_used,
            "elapsed_seconds": call.elapsed_seconds,
            "invocation_error": call.invocation_error,
        },
        "raw_stdout_path": str(stdout_path),
        "raw_stdout_sha256": _sha256_bytes(stdout_bytes),
        "raw_stderr_path": str(stderr_path),
        "raw_stderr_sha256": _sha256_bytes(stderr_bytes),
        "record": record.to_json(),
    }
    _write_immutable(terminal_path, _canonical_json_bytes(terminal))
    return record, True


def _load_lean_terminal(
    config: ScaleConfig,
    path: Path,
    job: ScaleJob,
    record: TransformRecord,
) -> LeanCheckResult:
    raw = _read_json_object(path)
    candidate_hash = (
        _sha256_text(record.rewritten_statement) if record.rewritten_statement is not None else None
    )
    if raw.get("job_id") != job.job_id or raw.get("candidate_sha256") != candidate_hash:
        raise ValueError(f"Lean terminal binding mismatch: {path}")
    result = LeanCheckResult(**raw)
    if result.status not in {"valid", "invalid", "not_generated", "infrastructure_error"}:
        raise ValueError(f"unknown Lean terminal status: {path}")
    if result.status != "not_generated":
        source_path = Path(result.batch_source_path or "")
        assert record.rewritten_statement is not None
        expected_declaration = _lean_declaration_header(job, record.rewritten_statement)
        expected_context_sha256: str | None = None
        if job.statement.source_range_start is not None:
            expected_candidate = _LeanCandidate(
                job=job,
                statement=record.rewritten_statement,
                candidate_sha256=candidate_hash or "",
                near_dup_hash=signature_near_dup_hash(record.rewritten_statement),
                candidate_blocked=False,
            )
            expected_context_sha256 = _sha256_bytes(
                _lean_source_bytes(config, [expected_candidate])
            )
        if (
            result.batch_source_path is None
            or result.batch_source_sha256 is None
            or not source_path.is_file()
            or _sha256_file(source_path) != result.batch_source_sha256
            or (
                expected_context_sha256 is not None
                and result.batch_source_sha256 != expected_context_sha256
            )
            or expected_declaration not in source_path.read_text(encoding="utf-8")
            or result.memory_hard_limit_mb != config.lean_memory_mb
        ):
            raise ValueError(f"Lean terminal source/config mismatch: {path}")
    for artifact_path, expected_hash in (
        (result.stdout_path, result.stdout_sha256),
        (result.stderr_path, result.stderr_sha256),
    ):
        if artifact_path is None and expected_hash is None:
            continue
        if (
            artifact_path is None
            or expected_hash is None
            or not Path(artifact_path).is_file()
            or _sha256_file(Path(artifact_path)) != expected_hash
        ):
            raise ValueError(f"Lean terminal artifact mismatch: {path}")
    return result


def _write_lean_terminal(config: ScaleConfig, result: LeanCheckResult) -> None:
    path = config.output_root / "lean" / "items" / result.job_id / "terminal.json"
    _write_immutable(path, _canonical_json_bytes(result.to_json()))


@dataclass(frozen=True, slots=True)
class _LeanCandidate:
    job: ScaleJob
    statement: str
    candidate_sha256: str
    near_dup_hash: str
    candidate_blocked: bool


@dataclass(frozen=True, slots=True)
class _LeanBatchCapture:
    returncode: int | None
    timed_out: bool
    invocation_error: str | None
    source_path: Path
    source_sha256: str
    memory_hard_limit_mb: int
    stdout_path: Path
    stderr_path: Path
    stdout_sha256: str
    stderr_sha256: str


def _lean_theorem_name(job: ScaleJob) -> str:
    digest = hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()[:16]
    alpha_digest = digest.translate(str.maketrans("0123456789", "ghijklmnop"))
    return f"LeanFaithDThree_{alpha_digest}"


def _lean_declaration_header(job: ScaleJob, statement: str) -> str:
    return f"theorem {_lean_theorem_name(job)} {statement} := by"


def _lean_source_bytes(
    config: ScaleConfig,
    candidates: Sequence[_LeanCandidate],
) -> bytes:
    contextual = [
        candidate
        for candidate in candidates
        if candidate.job.statement.source_range_start is not None
    ]
    if contextual:
        if len(candidates) != 1:
            raise ValueError("contextual D-3 Lean checks must contain exactly one candidate")
        candidate = contextual[0]
        relative_source = Path(candidate.job.statement.source_file)
        if relative_source.is_absolute() or ".." in relative_source.parts:
            raise ValueError(f"invalid mathlib source path: {relative_source}")
        source_path = (config.mathlib_project / relative_source).resolve()
        if not source_path.is_relative_to(config.mathlib_project.resolve()):
            raise ValueError(f"mathlib source escapes the project: {source_path}")
        source_lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
        start = candidate.job.statement.source_range_start
        assert start is not None
        if start > len(source_lines) + 1:
            raise ValueError(f"source range starts past EOF: {source_path}:{start}")
        prefix = "".join(source_lines[: start - 1])
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        source = (
            prefix
            + "\n"
            + _lean_declaration_header(candidate.job, candidate.statement)
            + "\n  sorry\n"
        )
        return source.encode("utf-8")
    declarations = ["import Mathlib", ""]
    for candidate in candidates:
        declarations.extend(
            [
                _lean_declaration_header(candidate.job, candidate.statement),
                "  sorry",
                "",
            ]
        )
    return "\n".join(declarations).encode("utf-8")


def _validate_scale_source_contexts(
    config: ScaleConfig,
    jobs: Sequence[ScaleJob],
) -> dict[str, int]:
    """Fail before provider calls if a frozen source context cannot be reconstructed."""
    total_bytes = 0
    source_files: set[str] = set()
    for job in jobs:
        statement = job.statement.headless
        candidate = _LeanCandidate(
            job=job,
            statement=statement,
            candidate_sha256=_sha256_text(statement),
            near_dup_hash=signature_near_dup_hash(statement),
            candidate_blocked=False,
        )
        total_bytes += len(_lean_source_bytes(config, [candidate]))
        source_files.add(job.statement.source_file)
    return {
        "validated_jobs": len(jobs),
        "source_files": len(source_files),
        "total_reconstructed_bytes": total_bytes,
    }


def _validate_mathlib_checkout(config: ScaleConfig) -> tuple[str, bool]:
    revision = _git_rev(config.mathlib_project)
    clean = _git_is_clean(config.mathlib_project)
    if config.enforce_storage_root:
        if config.expected_source_revision is None:
            raise ValueError("production D-3 scale requires a pinned mathlib source revision")
        if revision != config.expected_source_revision:
            raise ValueError(
                "mathlib checkout revision mismatch: "
                f"expected {config.expected_source_revision}, got {revision}"
            )
        if not clean:
            raise ValueError("production D-3 scale requires a clean mathlib checkout")
    return revision, clean


def _execute_lean_batch(
    config: ScaleConfig,
    candidates: Sequence[_LeanCandidate],
) -> _LeanBatchCapture:
    binding = "\n".join(
        f"{candidate.job.job_id}:{candidate.candidate_sha256}" for candidate in candidates
    )
    batch_id = hashlib.sha256(binding.encode("utf-8")).hexdigest()[:20]
    attempt_dir = config.output_root / "lean" / "batches" / batch_id / f"attempt_{time.time_ns()}"
    source_path = attempt_dir / "batch.lean"
    stdout_path = attempt_dir / "stdout.txt"
    stderr_path = attempt_dir / "stderr.txt"
    source_bytes = _lean_source_bytes(config, candidates)
    _write_immutable(source_path, source_bytes)
    returncode: int | None = None
    timed_out = False
    invocation_error: str | None = None
    stdout = ""
    stderr = ""
    try:
        process = subprocess.run(
            [
                "lake",
                "env",
                "lean",
                "-M",
                str(config.lean_memory_mb),
                str(source_path),
            ],
            cwd=config.mathlib_project,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=config.lean_timeout,
        )
        returncode = process.returncode
        stdout = process.stdout
        stderr = process.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        invocation_error = f"{type(exc).__name__}: {exc}"
    stdout_bytes = stdout.encode("utf-8")
    stderr_bytes = stderr.encode("utf-8")
    _write_immutable(stdout_path, stdout_bytes)
    _write_immutable(stderr_path, stderr_bytes)
    return _LeanBatchCapture(
        returncode=returncode,
        timed_out=timed_out,
        invocation_error=invocation_error,
        source_path=source_path,
        source_sha256=_sha256_bytes(source_bytes),
        memory_hard_limit_mb=config.lean_memory_mb,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_sha256=_sha256_bytes(stdout_bytes),
        stderr_sha256=_sha256_bytes(stderr_bytes),
    )


def _lean_result_from_capture(
    candidate: _LeanCandidate,
    capture: _LeanBatchCapture,
    status: str,
    detail: str | None,
) -> LeanCheckResult:
    return LeanCheckResult(
        job_id=candidate.job.job_id,
        candidate_sha256=candidate.candidate_sha256,
        status=status,
        candidate_near_dup_hash=candidate.near_dup_hash,
        candidate_blocked=candidate.candidate_blocked,
        returncode=capture.returncode,
        timed_out=capture.timed_out,
        batch_source_path=str(capture.source_path),
        batch_source_sha256=capture.source_sha256,
        memory_hard_limit_mb=capture.memory_hard_limit_mb,
        stdout_path=str(capture.stdout_path),
        stderr_path=str(capture.stderr_path),
        stdout_sha256=capture.stdout_sha256,
        stderr_sha256=capture.stderr_sha256,
        detail=detail,
    )


def _check_lean_candidates_recursive(
    config: ScaleConfig,
    candidates: Sequence[_LeanCandidate],
) -> list[LeanCheckResult]:
    if len(candidates) > 1 and any(
        candidate.job.statement.source_range_start is not None for candidate in candidates
    ):
        return [
            result
            for candidate in candidates
            for result in _check_lean_candidates_recursive(config, [candidate])
        ]
    capture = _execute_lean_batch(config, candidates)
    if capture.returncode == 0:
        return [
            _lean_result_from_capture(candidate, capture, "valid", None) for candidate in candidates
        ]
    if capture.invocation_error is not None:
        return [
            _lean_result_from_capture(
                candidate, capture, "infrastructure_error", capture.invocation_error
            )
            for candidate in candidates
        ]
    if len(candidates) > 1:
        midpoint = len(candidates) // 2
        return _check_lean_candidates_recursive(
            config, candidates[:midpoint]
        ) + _check_lean_candidates_recursive(config, candidates[midpoint:])
    status = "infrastructure_error" if capture.timed_out else "invalid"
    detail = "Lean check timed out" if capture.timed_out else "Lean elaboration failed"
    return [_lean_result_from_capture(candidates[0], capture, status, detail)]


def run_lean_checks(
    config: ScaleConfig,
    jobs: Sequence[ScaleJob],
    records: Sequence[TransformRecord],
) -> tuple[list[LeanCheckResult], int]:
    """Lean-check every parsed rewrite, with per-record resume terminals."""
    lean_root = config.output_root / "lean"
    lean_root.mkdir(parents=True, exist_ok=True)
    with (lean_root / "claim.lock").open("a+b") as claim:
        fcntl.flock(claim.fileno(), fcntl.LOCK_EX)
        return _run_lean_checks_locked(config, jobs, records)


def _run_lean_checks_locked(
    config: ScaleConfig,
    jobs: Sequence[ScaleJob],
    records: Sequence[TransformRecord],
) -> tuple[list[LeanCheckResult], int]:
    blocklist = GoldenBlocklist.load(config.blocklist_path)
    record_by_index = {record.index: record for record in records}
    results: dict[str, LeanCheckResult] = {}
    pending: list[_LeanCandidate] = []
    reused = 0
    for job in jobs:
        record = record_by_index[job.index]
        terminal_path = config.output_root / "lean" / "items" / job.job_id / "terminal.json"
        if terminal_path.exists():
            results[job.job_id] = _load_lean_terminal(config, terminal_path, job, record)
            reused += 1
            continue
        if not record.parse_ok or record.rewritten_statement is None:
            result = LeanCheckResult(
                job_id=job.job_id,
                candidate_sha256=None,
                status="not_generated",
                detail=record.parse_error or "no parsed rewrite",
            )
            _write_lean_terminal(config, result)
            results[job.job_id] = result
            continue
        candidate_hash = _sha256_text(record.rewritten_statement)
        near_dup = signature_near_dup_hash(record.rewritten_statement)
        pending.append(
            _LeanCandidate(
                job=job,
                statement=record.rewritten_statement,
                candidate_sha256=candidate_hash,
                near_dup_hash=near_dup,
                candidate_blocked=near_dup in blocklist.near_dup_hashes,
            )
        )
    for offset in range(0, len(pending), config.lean_batch_size):
        batch = pending[offset : offset + config.lean_batch_size]
        for result in _check_lean_candidates_recursive(config, batch):
            _write_lean_terminal(config, result)
            results[result.job_id] = result
    return [results[job.job_id] for job in jobs], reused


def build_trainer_records(
    jobs: Sequence[ScaleJob],
    records: Sequence[TransformRecord],
    lean_results: Sequence[LeanCheckResult],
) -> list[dict[str, Any]]:
    """Project admitted D-3 records into the frozen trainer schema."""
    from leanfaith.train2.trainer import TrainingRecord

    record_by_index = {record.index: record for record in records}
    lean_by_job = {result.job_id: result for result in lean_results}
    output: list[dict[str, Any]] = []
    for job in jobs:
        record = record_by_index[job.index]
        check = lean_by_job[job.job_id]
        if not (
            record.parse_ok
            and record.label_matches_direction is True
            and record.family_matches_assignment is True
            and record.rewrite_changed is True
            and check.status == "valid"
            and not check.candidate_blocked
            and record.rewritten_statement is not None
            and record.intended_label in {"consistent", "inconsistent"}
        ):
            continue
        label = record.intended_label == "consistent"
        identity = {
            "schema": "d3_trainer_record_v1",
            "theorem_id": job.statement.theorem_id,
            "candidate_sha256": _sha256_text(record.rewritten_statement),
            "family": job.family.family_id,
            "label": label,
        }
        record_id = (
            "d3:"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        )
        row = {
            "record_id": record_id,
            "reference_headless": job.statement.headless,
            "candidate_headless": record.rewritten_statement,
            "label": label,
            "group_key": job.statement.group_key,
            "family": job.family.family_id,
            "source": "d3_codex_scale_v1",
            "weight": 1.0,
        }
        validated = TrainingRecord.model_validate(row)
        output.append(validated.model_dump(mode="json"))
    return output


def run_scale(config: ScaleConfig) -> dict[str, Any]:
    """Run/resume the frozen 200-call Codex generation and Lean-check pipeline."""
    if config.enforce_storage_root and not config.output_root.resolve().is_relative_to(
        Path("/storage/milikic")
    ):
        raise ValueError("D-3 scale data artifacts must be under /storage/milikic")
    if config.enforce_storage_root and config.count != SCALE_STATEMENT_COUNT:
        raise ValueError(
            f"production D-3 scale requires exactly {SCALE_STATEMENT_COUNT} jobs, "
            f"got {config.count}"
        )
    if config.max_workers <= 0 or config.lean_batch_size <= 0:
        raise ValueError("worker and Lean batch counts must be positive")
    mathlib_git_rev, mathlib_git_clean = _validate_mathlib_checkout(config)
    config.output_root.mkdir(parents=True, exist_ok=True)
    fewshots = build_fewshots(
        config.pairs_path,
        config.seed,
        config.k_pos,
        config.k_neg,
        config.partition_manifest_path,
    )
    pool, source_stats = load_scale_source_statements(
        config.reprs_path,
        config.theorems_path,
        config.blocklist_path,
        config.seed,
        expected_source_revision=config.expected_source_revision,
    )
    jobs = build_scale_jobs(pool, fewshots, config.count)
    source_context_preflight = _validate_scale_source_contexts(config, jobs)
    plan_rows = [job.plan_json() for job in jobs]
    plan_bytes = _canonical_jsonl_bytes(plan_rows)
    job_plan_path = config.output_root / "job_plan.jsonl"
    input_hashes = {
        "golden_pairs": _sha256_file(config.pairs_path),
        "golden_partition": _sha256_file(config.partition_manifest_path),
        "golden_blocklist": _sha256_file(config.blocklist_path),
        "representations": _sha256_file(config.reprs_path),
        "theorems": _sha256_file(config.theorems_path),
    }
    command_template = provider_command("codex", "<PROMPT>", None)
    run_config = {
        "schema_version": 1,
        "track": "D-3 Codex transforms scale v1",
        "git_rev": _git_rev(config.repo_root),
        "seed": config.seed,
        "provider": "codex",
        "model": CODEX_MODEL,
        "codex_cli_version": _codex_cli_version(),
        "provider_command_template": command_template,
        "reasoning_effort": "high",
        "stdin_policy": "subprocess.DEVNULL",
        "public_source_only": True,
        "private_source_content": False,
        "count": config.count,
        "max_workers": config.max_workers,
        "timeout_seconds": config.timeout,
        "fewshot_partition": FEWSHOT_PARTITION,
        "fewshot_pair_ids": [shot.pair_id for shot in fewshots],
        "input_paths": {
            "golden_pairs": str(config.pairs_path),
            "golden_partition": str(config.partition_manifest_path),
            "golden_blocklist": str(config.blocklist_path),
            "representations": str(config.reprs_path),
            "theorems": str(config.theorems_path),
        },
        "input_sha256": input_hashes,
        "source_revision": config.expected_source_revision,
        "source_pool_stats": asdict(source_stats),
        "job_plan_sha256": _sha256_bytes(plan_bytes),
        "planned_family_counts": dict(
            sorted(Counter(job.family.family_id for job in jobs).items())
        ),
        "family_assignment_policy": (
            "direction=index mod 2; family=(index//2) mod direction-menu length"
        ),
        "mathlib_project": str(config.mathlib_project),
        "mathlib_git_rev": mathlib_git_rev,
        "mathlib_git_clean": mathlib_git_clean,
        "source_context_preflight": source_context_preflight,
        "lean_check": {
            "launcher": "lake env lean",
            "source_context_policy": (
                "one generated declaration per pinned source-file prefix ending immediately "
                "before the source theorem"
            ),
            "memory_hard_limit_mb": config.lean_memory_mb,
            "lean_cli_memory_args": ["-M", str(config.lean_memory_mb)],
            "batch_size": config.lean_batch_size,
            "effective_context_batch_size": 1,
            "timeout_seconds": config.lean_timeout,
        },
    }
    _write_immutable(config.output_root / "run_config.json", _canonical_json_bytes(run_config))
    _write_immutable(job_plan_path, plan_bytes)

    terminal_paths = {
        job.job_id: config.output_root / "items" / job.job_id / "terminal.json" for job in jobs
    }
    reused_generation = sum(path.exists() for path in terminal_paths.values())
    records_by_index: dict[int, TransformRecord] = {}
    pending_jobs = [job for job in jobs if not terminal_paths[job.job_id].exists()]
    for job in jobs:
        if terminal_paths[job.job_id].exists():
            records_by_index[job.index] = _load_scale_terminal(terminal_paths[job.job_id], job)
    progress_path = config.output_root / "progress.json"
    attempted_here_indexes: set[int] = set()
    with ThreadPoolExecutor(max_workers=config.max_workers) as pool_executor:
        future_jobs = {
            pool_executor.submit(_execute_scale_job, config, job): job for job in pending_jobs
        }
        for future in as_completed(future_jobs):
            job = future_jobs[future]
            record, attempted_here = future.result()
            records_by_index[job.index] = record
            if attempted_here:
                attempted_here_indexes.add(job.index)
            progress = {
                "requested": len(jobs),
                "completed": len(records_by_index),
                "reused_at_start": reused_generation,
                "updated_utc": datetime.now(UTC).isoformat(),
            }
            _atomic_replace(progress_path, _canonical_json_bytes(progress))
            print(
                f"D-3 generation {len(records_by_index)}/{len(jobs)}: {job.job_id}",
                flush=True,
            )
    records = [records_by_index[job.index] for job in jobs]
    records_path = config.output_root / "records.jsonl"
    records_bytes = _canonical_jsonl_bytes([record.to_json() for record in records])
    _atomic_replace(records_path, records_bytes)

    lean_results, reused_lean = run_lean_checks(config, jobs, records)
    lean_path = config.output_root / "lean_checks.jsonl"
    lean_bytes = _canonical_jsonl_bytes([result.to_json() for result in lean_results])
    _atomic_replace(lean_path, lean_bytes)
    trainer_rows = build_trainer_records(jobs, records, lean_results)
    trainer_path = config.output_root / "trainer_records.jsonl"
    trainer_bytes = _canonical_jsonl_bytes(trainer_rows)
    _atomic_replace(trainer_path, trainer_bytes)

    generation_stats = provider_stats(records)
    generation_stats.update(
        {
            "family_matches_assignment": sum(
                record.family_matches_assignment is True for record in records
            ),
            "rewrite_changed": sum(record.rewrite_changed is True for record in records),
        }
    )
    lean_counts = dict(sorted(Counter(result.status for result in lean_results).items()))
    new_processes_started = sum(
        record.index in attempted_here_indexes and record.invocation_error is None
        for record in records
    )
    attempt_counts = {
        job.job_id: len(
            [
                path
                for path in (config.output_root / "items" / job.job_id / "attempts").iterdir()
                if path.is_dir() and path.name.isdigit()
            ]
        )
        for job in jobs
    }
    provider_attempt_journals = sum(attempt_counts.values())
    parsed_by_index = {record.index for record in records if record.parse_ok}
    lean_by_index = {job.index: result for job, result in zip(jobs, lean_results, strict=True)}
    unaccounted_parsed = sum(
        lean_by_index[index].status == "not_generated" for index in parsed_by_index
    )
    blocked_reasons: list[str] = []
    if generation_stats["provider_processes_started"] != len(jobs):
        blocked_reasons.append("not every frozen job started a Codex provider process")
    if generation_stats["parse_ok"] == 0:
        blocked_reasons.append("no Codex response produced a parsed rewrite")
    if unaccounted_parsed:
        blocked_reasons.append("one or more parsed rewrites lack a Lean outcome")
    if lean_counts.get("infrastructure_error", 0):
        blocked_reasons.append("one or more Lean checks ended in infrastructure_error")
    manifest = {
        **run_config,
        "status": "blocked" if blocked_reasons else "completed",
        "blocked_reason": "; ".join(blocked_reasons) if blocked_reasons else None,
        "completed_utc": datetime.now(UTC).isoformat(),
        "generation": {
            **generation_stats,
            "jobs_attempted_this_invocation": len(attempted_here_indexes),
            "calls_executed_this_invocation": new_processes_started,
            "provider_attempt_journals": provider_attempt_journals,
            "jobs_with_retry_attempts": sum(count > 1 for count in attempt_counts.values()),
            "extra_attempt_journals": provider_attempt_journals - len(jobs),
            "terminals_reused_at_start": reused_generation,
        },
        "lean": {
            "outcomes": lean_counts,
            "parsed_rewrites_unaccounted": unaccounted_parsed,
            "terminals_reused_at_start": reused_lean,
            "candidate_blocked": sum(result.candidate_blocked for result in lean_results),
        },
        "trainer": {
            "record_count": len(trainer_rows),
            "label_counts": dict(
                sorted(Counter(str(row["label"]).lower() for row in trainer_rows).items())
            ),
            "family_counts": dict(
                sorted(Counter(str(row["family"]) for row in trainer_rows).items())
            ),
        },
        "outputs": {
            "job_plan": {"path": str(job_plan_path), "sha256": _sha256_bytes(plan_bytes)},
            "records": {"path": str(records_path), "sha256": _sha256_bytes(records_bytes)},
            "lean_checks": {"path": str(lean_path), "sha256": _sha256_bytes(lean_bytes)},
            "trainer_records": {
                "path": str(trainer_path),
                "sha256": _sha256_bytes(trainer_bytes),
            },
        },
    }
    manifest_path = config.output_root / "run_manifest.json"
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_replace(manifest_path, manifest_bytes)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", action="store_true", help="run the Codex-only D-3 scale flow")
    parser.add_argument("--pairs-path", type=Path, default=GOLDEN_PAIRS_PATH)
    parser.add_argument("--partition-manifest", type=Path, default=GOLDEN_PARTITION_PATH)
    parser.add_argument("--reprs-path", type=Path)
    parser.add_argument("--theorems-path", type=Path, default=FULL_MATHLIB_THEOREMS_PATH)
    parser.add_argument("--blocklist-path", type=Path, default=GOLDEN_BLOCKLIST_PATH)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--mathlib-project", type=Path, default=MATHLIB_PROJECT_PATH)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--n-per-provider", type=int, default=10)
    parser.add_argument("--scale-count", type=int, default=SCALE_STATEMENT_COUNT)
    parser.add_argument("--providers", nargs="+", default=list(PROVIDERS))
    parser.add_argument("--claude-model", default="claude-opus-5")
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--lean-batch-size", type=int, default=LEAN_BATCH_SIZE)
    parser.add_argument("--lean-memory-mb", type=int, default=LEAN_MEMORY_MB)
    parser.add_argument("--lean-timeout", type=int, default=LEAN_TIMEOUT_SECONDS)
    parser.add_argument("--retry-incomplete-attempts", action="store_true")
    args = parser.parse_args(argv)

    if args.scale:
        scale_config = ScaleConfig(
            pairs_path=args.pairs_path,
            partition_manifest_path=args.partition_manifest,
            reprs_path=args.reprs_path or FULL_MATHLIB_REPRESENTATIONS_PATH,
            theorems_path=args.theorems_path,
            blocklist_path=args.blocklist_path,
            output_root=args.output_root or DEFAULT_SCALE_OUTPUT_ROOT,
            mathlib_project=args.mathlib_project,
            seed=args.seed,
            count=args.scale_count,
            max_workers=args.max_workers or SCALE_CODEX_WORKERS,
            timeout=args.timeout or SCALE_CODEX_TIMEOUT_SECONDS,
            lean_batch_size=args.lean_batch_size,
            lean_memory_mb=args.lean_memory_mb,
            lean_timeout=args.lean_timeout,
            retry_incomplete_attempts=args.retry_incomplete_attempts,
        )
        manifest = run_scale(scale_config)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "generation": manifest["generation"],
                    "lean": manifest["lean"],
                    "trainer": manifest["trainer"],
                },
                indent=2,
            )
        )
        return 0 if manifest["status"] == "completed" else 1

    config = PilotConfig(
        pairs_path=args.pairs_path,
        reprs_path=args.reprs_path or MATHLIB_REPRESENTATIONS_PATH,
        output_root=args.output_root or DEFAULT_OUTPUT_ROOT,
        seed=args.seed,
        n_per_provider=args.n_per_provider,
        providers=tuple(args.providers),
        claude_model=args.claude_model,
        max_workers=args.max_workers or 6,
        timeout=args.timeout or PROVIDER_TIMEOUT_SECONDS,
    )
    manifest = run_pilot(config)
    print(json.dumps(manifest["per_provider_stats"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
