from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.source_bundle_v3 import SourceBundleV3Error, canonical_source_line
from leanfaith.sft2b.source_bundle_v3_mechanical import (
    CONFIG_SCHEMA,
    EXPECTED_CORE_RELEASE_CLASS_COUNTS,
    MechanicalQuarantinedSourceV1,
    MechanicalState,
    _validate_config,
    plan_release,
    preflight_release,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = (
    REPO_ROOT / "configs/sft2b/reform_diverse_full_sources_v3_mechanical_conservative_v1.json"
)
V2_BUNDLE = Path(
    "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/"
    "source_inputs/reform_diverse_full_v2"
)


def _source(index: int) -> SourceRecord:
    nl = f"Standalone mathematical statement {index}."
    proposition = f"∀ n : Nat, n = n -- {index}"
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
    source_id = stable_id(
        "sft2b_source",
        {
            "reference_theorem_id": f"example:{index}",
            "nl_statement": nl,
            "source_revision": provenance.source_revision,
        },
    )
    return SourceRecord(
        source_id=source_id,
        nl_statement=nl,
        reference_theorem_id=f"example:{index}",
        reference_declaration_name=f"t{index}",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id="ctx:" + "3" * 64,
            render_compile_context_id="ctx:" + "4" * 64,
            project_id="mathlib",
            project_revision="5" * 40,
            project_path="/tmp/mathlib",
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


def _state() -> MechanicalState:
    sources = tuple(_source(index) for index in range(1, 8))
    one, two, three, four, five, six, seven = (source.source_id for source in sources)
    rows = {source.source_id: source for source in sources}
    return MechanicalState(
        rows=rows,
        source_lines={source.source_id: canonical_source_line(source) for source in sources},
        release_classes={source.source_id: "library_mathlib" for source in sources},
        domains={source.source_id: "algebra" for source in sources},
        active_order=(one, two, three, four, five, six),
        core_ids=(one, two, three, four),
        tail_ids=(five, six),
        prior_quarantine_ids=(seven,),
        meta_ids=(one,),
        workbook_ids=(four, seven),
        meta_evidence={one: "8" * 64},
        workbook_evidence={four: "9" * 64, seven: "a" * 64},
        mechanical_evidence={
            source.source_id: ("v2_source_selection_audit", "b" * 64) for source in sources
        },
    )


def _config() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CONFIG_PATH.read_text(encoding="utf-8")))


def test_one_source_mechanical_quarantine_is_canonical_and_not_review() -> None:
    source = _source(1)
    state = MechanicalState(
        rows={source.source_id: source},
        source_lines={source.source_id: canonical_source_line(source)},
        release_classes={source.source_id: "library_mathlib"},
        domains={source.source_id: "algebra"},
        active_order=(source.source_id,),
        core_ids=(source.source_id,),
        tail_ids=(),
        prior_quarantine_ids=(),
        meta_ids=(source.source_id,),
        workbook_ids=(),
        meta_evidence={source.source_id: "8" * 64},
        workbook_evidence={},
        mechanical_evidence={source.source_id: ("v2_source_selection_audit", "9" * 64)},
    )
    plan = plan_release(state, target_core_count=0)
    assert plan.ordered_active_ids == ()
    assert plan.quarantine_ids == (source.source_id,)
    assert plan.action_counts["quarantined_from_core"] == 1
    assert plan.reason_counts["meta_instruction_quarantine"] == 1
    row = MechanicalQuarantinedSourceV1(
        source=source,
        source_record_sha256=hash_canonical(source.model_dump(mode="json")),
        v2_view="core",
        terminal_basis="active_meta_instruction_filter_v2",
        evidence_sha256="8" * 64,
        semantic_or_human_review=False,
    )
    serialized = canonical_json_bytes(row.model_dump(mode="json")) + b"\n"
    assert b'"semantic_or_human_review":false' in serialized
    assert b"reviewer" not in serialized


def test_boundary_preserves_prior_core_then_backfills_prior_tail() -> None:
    state = _state()
    plan = plan_release(state, target_core_count=4)
    _, two, three, _, five, six, seven = state.rows
    assert plan.core_ids == (two, three, five, six)
    assert plan.tail_ids == ()
    assert plan.quarantine_ids == tuple(sorted((state.meta_ids[0], state.workbook_ids[0], seven)))
    assert plan.action_counts["moved_tail_to_core"] == 2
    assert plan.reason_counts["source_contract_correction"] == 1
    assert plan.source_bytes == b"".join(
        state.source_lines[source_id] for source_id in plan.core_ids
    )


def test_production_config_is_explicitly_mechanical_and_strict() -> None:
    config = _config()
    _validate_config(REPO_ROOT, config)
    assert config["schema_version"] == CONFIG_SCHEMA
    assert config["expected_core_release_class_counts"] == EXPECTED_CORE_RELEASE_CLASS_COUNTS
    assert config["mechanical_quarantine"]["review_records_used"] == 0
    assert config["mechanical_quarantine"]["human_or_model_review_used"] is False
    assert config["publication"]["requires_human_review_gate"] is False


def test_config_rejects_review_or_generation_drift() -> None:
    config = _config()
    config["mechanical_quarantine"]["human_or_model_review_used"] = True
    with pytest.raises(SourceBundleV3Error, match="mechanical quarantine"):
        _validate_config(REPO_ROOT, config)
    config = _config()
    config["generation_gate"]["allow_core_generation"] = True
    with pytest.raises(SourceBundleV3Error, match="generation"):
        _validate_config(REPO_ROOT, config)


def test_real_preflight_replays_v2_without_review_gate(tmp_path: Path) -> None:
    if not V2_BUNDLE.is_dir():
        pytest.skip("frozen v2 source bundle is not mounted")
    output = tmp_path / "must-not-exist"
    receipt = preflight_release(
        REPO_ROOT,
        config_path=CONFIG_PATH,
        v2_bundle_dir=V2_BUNDLE,
        output_dir=output,
    )
    assert receipt.source_universe_count == 54_906
    assert receipt.meta_instruction_count == 469
    assert receipt.workbook_heuristic_count == 293
    assert receipt.meta_workbook_overlap_count == 0
    assert receipt.review_record_count == 0
    assert receipt.release_gate_passed is True
    assert not output.exists()
