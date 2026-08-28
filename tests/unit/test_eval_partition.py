"""Leakage and determinism properties of the golden partition (Track A1)."""

from __future__ import annotations

from leanfaith.eval.ingest import (
    RawGoldenRow,
    build_canonical_pairs,
    normalize_problem_name,
)
from leanfaith.eval.partition import assign_partitions, build_blocklist
from leanfaith.eval.schema import GoldenPair


def _row(
    dataset: str,
    problem: str,
    candidate: str,
    *,
    label: bool = True,
    reference: str | None = None,
    source: str = "proofnet",
) -> RawGoldenRow:
    return RawGoldenRow(
        dataset=dataset,  # type: ignore[arg-type]
        row_id=f"{dataset}:{problem}:{candidate[:12]}",
        problem_source=source,  # type: ignore[arg-type]
        problem_name=problem,
        header="import Mathlib\n",
        reference_lean=reference or f"theorem {problem} : 1 + 1 = 2 := by sorry",
        candidate_lean=candidate,
        label=label,
        label_provenance="expert_human",
    )


def _pairs(count: int = 40) -> list[GoldenPair]:
    rows: list[RawGoldenRow] = []
    for index in range(count):
        problem = f"exercise_{index}"
        rows.append(
            _row(
                "epla_proofnet",
                problem,
                f"theorem t{index} : {index} + 0 = {index} := by sorry",
                label=index % 3 != 0,
            )
        )
        if index % 4 == 0:
            rows.append(
                _row(
                    "gted_proofnet",
                    problem,
                    f"theorem g{index} : 0 + {index} = {index} := by sorry",
                    label=index % 2 == 0,
                )
            )
        if index % 5 == 0:
            rows.append(
                _row(
                    "beq_o1",
                    problem,
                    f"theorem b{index} : {index} = {index} := by sorry",
                )
            )
        if index % 2 == 0:
            rows.append(
                _row(
                    "proofnetverif",
                    problem,
                    f"theorem p{index} : {index} * 1 = {index} := by sorry",
                    label=False,
                )
            )
    return build_canonical_pairs(rows)


def test_no_group_crosses_buckets() -> None:
    result = assign_partitions(_pairs(), seed=7)
    seen: dict[str, set[str]] = {}
    for pair in result.pairs:
        bucket = result.group_partitions[pair.group_key]
        seen.setdefault(pair.group_key, set()).add(bucket)
        # A pair either follows its group or is quarantined (PNV-only rule).
        assert pair.partition in {bucket, "quarantine"}
    assert all(len(buckets) == 1 for buckets in seen.values())


def test_beq_groups_all_in_final_test() -> None:
    result = assign_partitions(_pairs(), seed=7)
    for pair in result.pairs:
        if any(m.dataset in {"beq_o1", "beq_rauto"} for m in pair.memberships):
            assert result.group_partitions[pair.group_key] == "final_test"
            assert pair.partition == "final_test"


def test_pnv_only_pairs_never_in_final_test() -> None:
    result = assign_partitions(_pairs(), seed=7)
    for pair in result.pairs:
        if all(m.dataset == "proofnetverif" for m in pair.memberships):
            assert pair.partition != "final_test"


def test_partition_is_deterministic_and_seed_sensitive() -> None:
    pairs = _pairs()
    first = assign_partitions(pairs, seed=11)
    second = assign_partitions(pairs, seed=11)
    assert first.group_partitions == second.group_partitions
    third = assign_partitions(pairs, seed=12)
    # Different seeds may reassign non-forced groups (tie-breaks); forced BEq
    # groups must not move.
    for group, bucket in third.group_partitions.items():
        if first.group_partitions[group] == "final_test" and bucket != "final_test":
            assert not any(
                m.dataset in {"beq_o1", "beq_rauto"}
                for pair in third.pairs
                if pair.group_key == group
                for m in pair.memberships
            )


def test_cross_dataset_merge_and_conflict_flag() -> None:
    shared_candidate = "theorem shared : 2 + 2 = 4 := by sorry"
    shared_reference = "theorem exercise_x : 2 + 2 = 4 := by sorry"
    rows = [
        _row(
            "epla_proofnet", "exercise_x", shared_candidate, label=True, reference=shared_reference
        ),
        _row(
            "gted_proofnet", "exercise_x", shared_candidate, label=False, reference=shared_reference
        ),
    ]
    pairs = build_canonical_pairs(rows)
    assert len(pairs) == 1
    assert {m.dataset for m in pairs[0].memberships} == {"epla_proofnet", "gted_proofnet"}
    assert pairs[0].label_conflict is True
    # EPLA has priority in label resolution.
    assert pairs[0].label is True


def test_blocklist_covers_every_side() -> None:
    pairs = _pairs(10)
    blocklist = build_blocklist(pairs)
    assert len(blocklist["near_dup_hashes"]) >= len(pairs)
    assert all(len(digest) == 64 for digest in blocklist["near_dup_hashes"])


def test_normalize_problem_name() -> None:
    assert normalize_problem_name("Rudin|exercise_4_5a") == "exercise_4_5a"
    assert normalize_problem_name("Dummit-Foote.exercise_9_4_9") == "exercise_9_4_9"
    assert normalize_problem_name("Putnam.exercise_2020_b5") == "exercise_2020_b5"
    assert normalize_problem_name("aime_1983_p1") == "aime_1983_p1"
    assert normalize_problem_name("exercise_1_19a") == "exercise_1_19a"
