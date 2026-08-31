from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.source_conservation_v3 import (
    ExplicitDeltaReasonV3,
    SourceConservationReceiptV3,
    build_conservation_events,
    summarize_conservation,
)


def _source(index: int) -> SourceRecord:
    nl = f"For every natural number n, example {index} states that n equals n."
    proposition = f"∀ n : Nat, n = n -- {index}"
    theorem_id = f"example:{index}"
    provenance = SourceProvenance(
        source_family="public_research",
        source_url="https://example.test/source",
        source_revision="revision",
        source_path=f"source-{index}.lean",
        source_file_sha256="0" * 64,
        manifest_path="manifest.json",
        manifest_sha256="1" * 64,
        source_recipe_sha256="2" * 64,
        license_card_value="Apache-2.0",
        redistribution_note="test",
        nl_extraction_rule="test",
        trusted_reference_basis="test",
    )
    return SourceRecord(
        source_id=stable_id(
            "sft2b_source",
            {
                "reference_theorem_id": theorem_id,
                "nl_statement": nl,
                "source_revision": provenance.source_revision,
            },
        ),
        nl_statement=nl,
        reference_theorem_id=theorem_id,
        reference_declaration_name=f"t{index}",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id="ctx:" + "3" * 64,
            render_compile_context_id="ctx:" + "4" * 64,
            project_id="mathlib",
            project_revision="5" * 40,
            project_path=str(Path("/tmp/mathlib")),
            lean_version="v4.31.0",
            import_header="import Mathlib\n",
            source_context_path="context.json",
            source_context_sha256="6" * 64,
            helper_path="helper.lean",
            helper_sha256="7" * 64,
        ),
        provenance=provenance,
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=True,
    )


def _reason(source: SourceRecord, direction: str, code: str) -> ExplicitDeltaReasonV3:
    return ExplicitDeltaReasonV3.model_validate(
        {
            "source_id": source.source_id,
            "direction": direction,
            "reason_code": code,
            "rationale": "Frozen test evidence explains this release delta.",
            "evidence_sha256": "8" * 64,
            "related_source_ids": [],
        }
    )


def test_conservation_accounts_for_quarantine_readmission_and_view_movement() -> None:
    one, two, three, four, five = (_source(index) for index in range(1, 6))
    events = build_conservation_events(
        v2_rows={row.source_id: row for row in (one, two, three, five)},
        v2_core_ids=(one.source_id, two.source_id),
        v2_quarantine_ids=(five.source_id,),
        v2_tail_ids=(three.source_id,),
        v3_rows={row.source_id: row for row in (one, two, three, four, five)},
        v3_core_ids=(three.source_id, four.source_id, five.source_id),
        v3_quarantine_ids=(two.source_id,),
        v3_tail_ids=(one.source_id,),
        delta_reasons=(
            _reason(one, "moved", "core_boundary_reselection"),
            _reason(two, "quarantined", "meta_instruction_quarantine"),
            _reason(three, "moved", "core_boundary_reselection"),
            _reason(four, "added", "newly_eligible_source"),
            _reason(five, "readmitted", "human_review_readmission"),
        ),
    )
    assert [event.source_id for event in events] == sorted(event.source_id for event in events)
    actions, reasons = summarize_conservation(events)
    assert actions == {
        "added": 1,
        "moved_core_to_tail": 1,
        "moved_tail_to_core": 1,
        "quarantined_from_core": 1,
        "readmitted_to_core": 1,
    }
    assert reasons == {
        "core_boundary_reselection": 2,
        "human_review_readmission": 1,
        "meta_instruction_quarantine": 1,
        "newly_eligible_source": 1,
    }


def test_conservation_rejects_unexplained_delta() -> None:
    one, two = _source(1), _source(2)
    with pytest.raises(ValueError, match="explicit reason"):
        build_conservation_events(
            v2_rows={one.source_id: one},
            v2_core_ids=(one.source_id,),
            v2_quarantine_ids=(),
            v2_tail_ids=(),
            v3_rows={two.source_id: two},
            v3_core_ids=(two.source_id,),
            v3_quarantine_ids=(),
            v3_tail_ids=(),
            delta_reasons=(),
        )


def test_conservation_rejects_stable_source_record_mutation() -> None:
    one = _source(1)
    mutated = one.model_copy(update={"nl_statement": one.nl_statement + " Changed."})
    with pytest.raises(ValueError, match="mutated"):
        build_conservation_events(
            v2_rows={one.source_id: one},
            v2_core_ids=(one.source_id,),
            v2_quarantine_ids=(),
            v2_tail_ids=(),
            v3_rows={one.source_id: mutated},
            v3_core_ids=(one.source_id,),
            v3_quarantine_ids=(),
            v3_tail_ids=(),
            delta_reasons=(),
        )


def test_conservation_receipt_rejects_self_inconsistent_summary_counts() -> None:
    payload = {
        "v2_sources_sha256": "0" * 64,
        "v2_core_view_sha256": "1" * 64,
        "v2_quarantine_view_sha256": "2" * 64,
        "v2_tail_view_sha256": "3" * 64,
        "v3_sources_sha256": "4" * 64,
        "v3_core_view_sha256": "5" * 64,
        "v3_quarantine_view_sha256": "6" * 64,
        "v3_tail_view_sha256": "7" * 64,
        "event_stream_sha256": "8" * 64,
        "event_count": 3,
        "v2_source_count": 3,
        "v3_source_count": 3,
        "action_counts": {"retained_core": 2},
        "reason_counts": {},
        "v2_partition_complete": True,
        "v3_partition_complete": True,
        "every_delta_explained": True,
    }
    with pytest.raises(ValueError, match="action counts"):
        SourceConservationReceiptV3.model_validate(payload)
