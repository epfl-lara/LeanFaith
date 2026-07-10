"""Golden check: examples/semantic_contract_v1.jsonl agrees with the schemas.

Every example's expected label fields must construct a valid ResolvedLabel
under the §11.8 invariants (with placeholder IDs), proving the semantic
contract file and the schema layer cannot drift apart silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.schemas import (
    FaithfulnessLevels,
    QualityTier,
    ResolvedLabel,
    SemanticLabelTargetKind,
    make_id,
)

_EXAMPLES = [
    json.loads(line)
    for line in (find_repo_root(Path(__file__).parent) / "examples" / "semantic_contract_v1.jsonl")
    .read_text(encoding="utf-8")
    .strip()
    .splitlines()
]


@pytest.mark.parametrize("example", _EXAMPLES, ids=[e["example_id"] for e in _EXAMPLES])
def test_contract_example_constructs_valid_label(example: dict[str, object]) -> None:
    expected = dict(example["expected"])  # type: ignore[arg-type]
    pair_id = make_id("pair", {"example": example["example_id"]})
    truths: dict[str, bool | None] = {"truth_A_implies_B": None, "truth_B_implies_A": None}
    levels = FaithfulnessLevels.model_validate(
        expected.get(
            "faithfulness_levels",
            {"F1_same_claim": expected["same_claim"]},
        )
    )
    if levels.F2_truth_equivalent is True:
        truths = {"truth_A_implies_B": True, "truth_B_implies_A": True}
    label = ResolvedLabel(
        label_id=make_id("lbl", {"example": example["example_id"]}),
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=pair_id,
        same_claim=expected["same_claim"],  # type: ignore[arg-type]
        resolution_outcome=expected["resolution_outcome"],  # type: ignore[arg-type]
        relation=expected["relation"],  # type: ignore[arg-type]
        faithfulness_levels=levels,
        error_types=tuple(expected.get("error_types", ())),  # type: ignore[arg-type]
        quality_tier=expected["quality_tier"],  # type: ignore[arg-type]
        resolution_method=expected.get("resolution_method"),  # type: ignore[arg-type]
        requires_adjudication=bool(expected["requires_adjudication"]),
        train_eligibility=expected["quality_tier"]
        not in (QualityTier.UNKNOWN.value, QualityTier.BENCHMARK.value)
        and expected["same_claim"] is not None,
        eval_eligibility=expected["quality_tier"] == QualityTier.GOLD_HUMAN.value,
        policy_version="semantic_policy_v1",
        **truths,
    )
    assert label.resolution_outcome.value == expected["resolution_outcome"]


def test_all_appendix_c_examples_present() -> None:
    ids = {e["example_id"] for e in _EXAMPLES}
    assert ids == {
        "C1_alpha_restatement",
        "C2_claim_erasure",
        "C3_missing_premise_tautology",
        "C4_strictness_under_alignment",
        "C5_extra_irrelevant_variable",
        "C6_wrong_domain",
        "C7_reference_defect_review_route",
        "C8_terminal_expert_ambiguity",
    }
