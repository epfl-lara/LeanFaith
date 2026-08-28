"""Golden-benchmark ingestion into canonical pair records (Track A1).

Loaders normalize four published human-labeled sources into one intermediate
row shape, then ``build_canonical_pairs`` merges rows that describe the same
(problem, reference, candidate) triple across datasets into a single
``GoldenPair`` carrying every dataset membership.

Provenance notes:
- EPLA / BEq / GTED labels are direct expert judgments (``expert_human``).
- ProofNetVerif only manually assessed predictions that typechecked;
  non-typechecking predictions were auto-labeled incorrect. The released data
  carries no typecheck column, so ``correct == False`` rows are a mix of
  human-judged-false and auto-false. Until a local compile pass tags
  ``candidate_compiles``, every False row is conservatively marked
  ``auto_typecheck_fail`` (erring toward distrust); True rows required a human
  decision and are ``expert_human``. ProofNetVerif is auxiliary-only either
  way (PLAN.md Track A1).

Problem identity: ``group_key = <source>::<bare exercise name, lowercased>``.
Book prefixes (``Rudin|``, ``Dummit-Foote.``) are stripped so the same
underlying problem joins across datasets; the rare same-name collision across
books over-merges, which is leakage-safe (both land in one bucket).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Literal

from leanfaith.config.models import StrictModel
from leanfaith.eval.schema import (
    EXPERT_DATASETS,
    DatasetMembership,
    GoldenDataset,
    GoldenPair,
    LabelProvenance,
    make_pair_id,
)
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.sources.proofnetverif import parse_row

#: Highest-priority membership decides the resolved label on (rare) conflicts;
#: the conflict is always flagged and conflicted pairs are excluded from
#: headline metrics.
_DATASET_PRIORITY: dict[str, int] = {
    "epla_minif2f": 0,
    "epla_proofnet": 0,
    "beq_o1": 1,
    "beq_rauto": 1,
    "gted_minif2f": 2,
    "gted_proofnet": 2,
    "proofnetverif": 3,
}

_PROOF_TAIL_FALLBACK = re.compile(r":=\s*(by\b[\s\S]*|sorry\s*)$")
_WS = re.compile(r"\s+")


class RawGoldenRow(StrictModel):
    """One benchmark row before cross-dataset merging."""

    dataset: GoldenDataset
    row_id: str
    problem_source: Literal["minif2f", "proofnet"]
    problem_name: str
    header: str
    reference_lean: str
    candidate_lean: str
    label: bool
    label_provenance: LabelProvenance
    candidate_compiles: bool | None = None
    generator_model: str | None = None


def normalize_problem_name(raw: str) -> str:
    """Bare exercise name: strip a ``Book|`` or ``Book.`` prefix, lowercase."""

    name = raw.strip()
    for separator in ("|",):
        if separator in name:
            name = name.split(separator, 1)[1]
    if "." in name and not name.split(".", 1)[0].isdigit():
        head, tail = name.split(".", 1)
        # ``Rudin.exercise_1_17`` → ``exercise_1_17``; keep names that merely
        # contain dots without a book-like head.
        if tail.startswith("exercise") or head[:1].isupper() or "-" in head:
            name = tail
    return name.lower()


def _headless(text: str) -> tuple[str, bool]:
    """§13.2 headless view with a flagged raw fallback."""

    normalized = normalize_headless(text)
    if normalized is not None:
        return normalized, False
    stripped = _PROOF_TAIL_FALLBACK.sub("", text)
    return _WS.sub(" ", stripped).strip(), True


def load_gted(gted_experiment_root: Path) -> list[RawGoldenRow]:
    """GTED human evaluation: 205 miniF2F + 93 ProofNet expert-labeled pairs."""

    rows: list[RawGoldenRow] = []
    for source, dataset in (("minif2f", "gted_minif2f"), ("proofnet", "gted_proofnet")):
        payload = json.loads((gted_experiment_root / source / "human_evaluation.json").read_text())
        for record in payload:
            name = str(record["name"])
            for index, sub in enumerate(record["sub_questions"]):
                rows.append(
                    RawGoldenRow(
                        dataset=dataset,  # type: ignore[arg-type]
                        row_id=f"{source}:{name}:{index}",
                        problem_source=source,  # type: ignore[arg-type]
                        problem_name=normalize_problem_name(name),
                        header=str(record.get("header", "")),
                        reference_lean=str(record["FL (Label)"]),
                        candidate_lean=str(sub["FL (Prediction)"]),
                        label=bool(sub["Human_Evaluation"]),
                        label_provenance="expert_human",
                        candidate_compiles=sub.get("compiler_check") == "success",
                        generator_model="herald",
                    )
                )
    return rows


def load_epla(minif2f_path: Path, proofnet_path: Path) -> list[RawGoldenRow]:
    """EPLA/ASSESS: 831 + 416 expert-labeled pairs; ``provability`` is the
    equivalence label."""

    rows: list[RawGoldenRow] = []
    for source, dataset, path in (
        ("minif2f", "epla_minif2f", minif2f_path),
        ("proofnet", "epla_proofnet", proofnet_path),
    ):
        payload = json.loads(path.read_text())
        for record in payload:
            name = str(record["name"])
            for index, sub in enumerate(record["sub_questions"]):
                rows.append(
                    RawGoldenRow(
                        dataset=dataset,  # type: ignore[arg-type]
                        row_id=f"{record['id']}:{index}",
                        problem_source=source,  # type: ignore[arg-type]
                        problem_name=normalize_problem_name(name),
                        header=str(record.get("header", "")),
                        reference_lean=str(record["FL (Label)"]),
                        candidate_lean=str(sub["FL (Prediction)"]),
                        label=bool(sub["provability"]),
                        label_provenance="expert_human",
                        candidate_compiles=sub.get("compiler_check") == "success",
                        generator_model=str(sub.get("tag")) if sub.get("tag") else None,
                    )
                )
    return rows


def load_proofnetverif(snapshot_dir: Path) -> list[RawGoldenRow]:
    """ProofNetVerif parquet splits via the existing §9.3 adapter."""

    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    rows: list[RawGoldenRow] = []
    for split, filename in (
        ("valid", "valid-00000-of-00001.parquet"),
        ("test", "test-00000-of-00001.parquet"),
    ):
        table = pq.read_table(snapshot_dir / "data" / filename)
        for index, raw in enumerate(table.to_pylist()):
            parsed = parse_row(raw, split=split)
            provenance: LabelProvenance = (
                "expert_human" if parsed.source_label else "auto_typecheck_fail"
            )
            rows.append(
                RawGoldenRow(
                    dataset="proofnetverif",
                    row_id=f"{split}:{index}:{parsed.problem_id}",
                    problem_source="proofnet",
                    problem_name=normalize_problem_name(parsed.problem_id),
                    header=parsed.lean_header,
                    reference_lean=parsed.reference_lean,
                    candidate_lean=parsed.candidate_lean,
                    label=parsed.source_label,
                    label_provenance=provenance,
                )
            )
    return rows


def load_beq_references(benchmark_jsonl: Path) -> dict[str, tuple[str, str]]:
    """``full_name`` → (reference ``formal_stmt``, ``header``) from the BEq
    repo's extended-ProofNet ``data/proofnet/benchmark.jsonl`` (374 rows)."""

    references: dict[str, tuple[str, str]] = {}
    with benchmark_jsonl.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            references[str(record["full_name"])] = (
                str(record["formal_stmt"]),
                str(record.get("header", "")),
            )
    return references


def load_beq(beq_root: Path, benchmark_jsonl: Path) -> list[RawGoldenRow]:
    """BEq human equivalence: predictions keyed ``Book.exercise`` (=
    ``full_name``), labels keyed by the exact prediction string, references
    joined from the repo's own extended-ProofNet benchmark."""

    references = load_beq_references(benchmark_jsonl)
    rows: list[RawGoldenRow] = []
    for subdir, dataset in (("o1-generated", "beq_o1"), ("rautoformalizer-generated", "beq_rauto")):
        predictions = json.loads((beq_root / subdir / "autoformalization.json").read_text())
        labels = json.loads((beq_root / subdir / "labels.json").read_text())
        for problem_key, entries in predictions.items():
            if problem_key not in references:
                raise ValueError(f"BEq reference missing for {problem_key}")
            reference, header = references[problem_key]
            for index, entry in enumerate(entries):
                prediction = str(entry["formal_stmt_pred"])
                if prediction not in labels:
                    raise ValueError(f"BEq label missing for {problem_key}[{index}]")
                typecheck = entry.get("typecheck_result", {})
                rows.append(
                    RawGoldenRow(
                        dataset=dataset,  # type: ignore[arg-type]
                        row_id=f"{subdir}:{problem_key}:{index}",
                        problem_source="proofnet",
                        problem_name=normalize_problem_name(problem_key),
                        header=header,
                        reference_lean=reference,
                        candidate_lean=prediction,
                        label=bool(labels[prediction]),
                        label_provenance="expert_human",
                        candidate_compiles=bool(typecheck.get("is_success"))
                        if isinstance(typecheck, dict)
                        else None,
                        generator_model="o1" if subdir == "o1-generated" else "rautoformalizer",
                    )
                )
    return rows


def build_canonical_pairs(rows: list[RawGoldenRow]) -> list[GoldenPair]:
    """Merge raw rows into canonical pairs with multi-dataset memberships."""

    grouped: dict[tuple[str, str, str], list[RawGoldenRow]] = defaultdict(list)
    headless_cache: dict[str, tuple[str, bool]] = {}

    def headless(text: str) -> tuple[str, bool]:
        if text not in headless_cache:
            headless_cache[text] = _headless(text)
        return headless_cache[text]

    for row in rows:
        group_key = f"{row.problem_source}::{row.problem_name}"
        reference_headless, _ = headless(row.reference_lean)
        candidate_headless, _ = headless(row.candidate_lean)
        key = (
            group_key,
            signature_near_dup_hash(reference_headless),
            signature_near_dup_hash(candidate_headless),
        )
        grouped[key].append(row)

    pairs: list[GoldenPair] = []
    for (group_key, reference_hash, candidate_hash), members in sorted(
        grouped.items(), key=lambda item: item[0]
    ):
        members = sorted(members, key=lambda row: (_DATASET_PRIORITY[row.dataset], row.row_id))
        primary = members[0]
        reference_headless, reference_fallback = headless(primary.reference_lean)
        candidate_headless, candidate_fallback = headless(primary.candidate_lean)
        expert_labels = {row.label for row in members if row.dataset in EXPERT_DATASETS}
        all_labels = {row.label for row in members}
        label_conflict = len(expert_labels) > 1 or (not expert_labels and len(all_labels) > 1)
        memberships = tuple(
            DatasetMembership(
                dataset=row.dataset,
                row_id=row.row_id,
                label=row.label,
                label_provenance=row.label_provenance,
                candidate_compiles=row.candidate_compiles,
                generator_model=row.generator_model,
            )
            for row in members
        )
        pairs.append(
            GoldenPair(
                pair_id=make_pair_id(group_key, reference_hash, candidate_hash),
                group_key=group_key,
                problem_source=primary.problem_source,
                problem_name=primary.problem_name,
                header=primary.header,
                reference_lean=primary.reference_lean,
                candidate_lean=primary.candidate_lean,
                reference_headless=reference_headless,
                candidate_headless=candidate_headless,
                reference_headless_fallback=reference_fallback,
                candidate_headless_fallback=candidate_fallback,
                memberships=memberships,
                label=primary.label,
                label_provenance=primary.label_provenance,
                label_conflict=label_conflict,
                partition="quarantine",
            )
        )
    return pairs
