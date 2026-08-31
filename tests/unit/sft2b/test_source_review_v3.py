from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    stable_id,
)
from leanfaith.sft2b.source_review_v3 import (
    REVIEWED_FIELDS,
    AutomaticDispositionV3,
    HumanSourceReviewV3,
    ReviewedSourceSnapshotV3,
    SourceReviewContractError,
    SourceReviewPacketEntryV3,
    _field_hashes,
    build_review_packet,
    load_config,
    verify_completed_human_reviews,
    verify_review_packet,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG = _REPO_ROOT / "configs/sft2b/source_review_contract_v3.json"


def _snapshot() -> ReviewedSourceSnapshotV3:
    return ReviewedSourceSnapshotV3(
        nl_statement="For every natural number, adding zero preserves that number.",
        reference_proposition="∀ n : Nat, n + 0 = n",
        reference_theorem_id="test:add_zero",
        reference_declaration_name="test_add_zero",
        headless_signature="(n : Nat) : n + 0 = n",
        problem_identity="test::add_zero",
        compile_context=CompileContextRecord(
            source_context_id="ctx:" + "0" * 64,
            render_compile_context_id="ctx:" + "1" * 64,
            project_id="test",
            project_revision="2" * 40,
            project_path="/tmp/test-project",
            lean_version="v4.test",
            import_header="import Mathlib\n",
            source_context_path="context.json",
            source_context_sha256="3" * 64,
            helper_path="helper.lean",
            helper_sha256="4" * 64,
        ),
        provenance=SourceProvenance(
            source_family="public_research",
            source_url="https://example.test",
            source_revision="revision",
            source_path="Source.lean",
            source_file_sha256="5" * 64,
            manifest_path="manifest.json",
            manifest_sha256="6" * 64,
            source_recipe_sha256="7" * 64,
            license_card_value="Apache-2.0",
            redistribution_note="test-only fixture",
            nl_extraction_rule="test fixture",
            trusted_reference_basis="test fixture",
        ),
    )


def _packet() -> SourceReviewPacketEntryV3:
    snapshot = _snapshot()
    source_hash = hash_canonical(snapshot.model_dump(mode="json"))
    field_hashes = _field_hashes(snapshot)
    payload = {
        "schema_version": "sft2b_source_review_packet_entry_v3",
        "source_id": "sft2b_source:" + "8" * 64,
        "release_class": "library_mathlib",
        "required_reasons": ("deterministic_100_per_release_class",),
        "reviewed_fields": REVIEWED_FIELDS,
        "reviewed_source": snapshot.model_dump(mode="json"),
        "reviewed_field_sha256": field_hashes.model_dump(mode="json"),
        "reviewed_source_sha256": source_hash,
    }
    return SourceReviewPacketEntryV3(
        packet_entry_id=stable_id("sft2b_review_packet", payload),
        source_id="sft2b_source:" + "8" * 64,
        release_class="library_mathlib",
        required_reasons=("deterministic_100_per_release_class",),
        reviewed_fields=REVIEWED_FIELDS,
        reviewed_source=snapshot,
        reviewed_field_sha256=field_hashes,
        reviewed_source_sha256=source_hash,
    )


def test_automatic_disposition_is_explicitly_not_human_review() -> None:
    packet = _packet()
    automatic = AutomaticDispositionV3(
        source_id=packet.source_id,
        packet_entry_id=packet.packet_entry_id,
        reviewed_source_sha256=packet.reviewed_source_sha256,
        evidence_kind="deterministic_automatic_disposition",
        method="workbook_solution_or_answer_discourse_v1",
        flags=("solution_or_proof_header",),
        automatic_disposition="quarantine_solution_or_answer_discourse",
        automatic_rationale="A deterministic rule matched a solution header.",
        satisfies_human_review_contract=False,
    )
    assert automatic.satisfies_human_review_contract is False
    with pytest.raises(ValidationError):
        AutomaticDispositionV3.model_validate(
            {**automatic.model_dump(mode="json"), "satisfies_human_review_contract": True}
        )


def test_review_schema_rejects_nonhuman_kind_and_unbound_hashes() -> None:
    packet = _packet()
    timestamp = datetime.datetime(2026, 8, 31, 12, tzinfo=datetime.UTC)
    payload = {
        "packet_entry_id": packet.packet_entry_id,
        "source_id": packet.source_id,
        "reviewed_source_sha256": packet.reviewed_source_sha256,
        "reviewer_identity": "project-member:test-reviewer",
        "review_timestamp_utc": timestamp.isoformat(),
        "verdict": "admit_standalone_aligned",
    }
    row = {
        "schema_version": "sft2b_human_source_review_v3",
        "review_id": stable_id("sft2b_human_review", payload),
        "packet_entry_id": packet.packet_entry_id,
        "source_id": packet.source_id,
        "reviewed_fields": packet.reviewed_fields,
        "reviewed_field_sha256": packet.reviewed_field_sha256.model_dump(mode="json"),
        "reviewed_source_sha256": packet.reviewed_source_sha256,
        "reviewer_identity": "project-member:test-reviewer",
        "reviewer_kind": "human",
        "method": "manual_row_level_source_alignment_v1",
        "review_timestamp_utc": timestamp.isoformat(),
        "verdict": "admit_standalone_aligned",
        "rationale": "The standalone claim exactly matches the bound proposition.",
        "personally_reviewed_exact_fields": True,
    }
    assert HumanSourceReviewV3.model_validate(row).reviewer_kind == "human"
    with pytest.raises(ValidationError):
        HumanSourceReviewV3.model_validate({**row, "reviewer_kind": "model"})
    with pytest.raises(ValidationError):
        HumanSourceReviewV3.model_validate(
            {**row, "reviewed_source_sha256": sha256_hex(b"different source")}
        )


def test_review_timestamp_must_be_utc() -> None:
    packet = _packet()
    timestamp = datetime.datetime(2026, 8, 31, 12)
    payload = {
        "packet_entry_id": packet.packet_entry_id,
        "source_id": packet.source_id,
        "reviewed_source_sha256": packet.reviewed_source_sha256,
        "reviewer_identity": "project-member:test-reviewer",
        "review_timestamp_utc": timestamp.isoformat(),
        "verdict": "admit_standalone_aligned",
    }
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        HumanSourceReviewV3(
            review_id=stable_id("sft2b_human_review", payload),
            packet_entry_id=packet.packet_entry_id,
            source_id=packet.source_id,
            reviewed_fields=packet.reviewed_fields,
            reviewed_field_sha256=packet.reviewed_field_sha256,
            reviewed_source_sha256=packet.reviewed_source_sha256,
            reviewer_identity="project-member:test-reviewer",
            reviewer_kind="human",
            method="manual_row_level_source_alignment_v1",
            review_timestamp_utc=timestamp,
            verdict="admit_standalone_aligned",
            rationale="The standalone claim exactly matches the bound proposition.",
            personally_reviewed_exact_fields=True,
        )


def test_real_v2_packet_replays_exact_required_population(tmp_path: Path) -> None:
    config = load_config(_CONFIG)
    if not Path(config.source_bundle_path).is_dir():
        pytest.skip("private frozen v2 source bundle is unavailable on this host")
    packet_dir = tmp_path / "packet"
    manifest = build_review_packet(_CONFIG, packet_dir)
    assert manifest.packet_entry_count == 992
    assert manifest.workbook_hit_count == 293
    assert manifest.deterministic_sample_count == 700
    assert manifest.overlap_count == 1
    assert manifest.release_class_counts == {
        "lean_workbook": 392,
        "library_cslib": 100,
        "library_mathlib": 100,
        "library_physlib": 100,
        "numina_current_auto": 100,
        "numina_current_human": 100,
        "numina_legacy_owner": 100,
    }
    assert verify_review_packet(_CONFIG, packet_dir) == manifest
    automatic = [
        json.loads(line)
        for line in (packet_dir / "automatic_dispositions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(automatic) == 293
    assert all(row["satisfies_human_review_contract"] is False for row in automatic)
    with pytest.raises(SourceReviewContractError, match="completed human-review rows"):
        verify_completed_human_reviews(
            _CONFIG,
            packet_dir,
            tmp_path / "missing-human-reviews.jsonl",
        )


def test_packet_tampering_fails_closed(tmp_path: Path) -> None:
    config = load_config(_CONFIG)
    if not Path(config.source_bundle_path).is_dir():
        pytest.skip("private frozen v2 source bundle is unavailable on this host")
    packet_dir = tmp_path / "packet"
    build_review_packet(_CONFIG, packet_dir)
    packet_path = packet_dir / "review_packet.jsonl"
    lines = packet_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["reviewed_source"]["nl_statement"] += " tampered"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    packet_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises((SourceReviewContractError, ValidationError)):
        verify_review_packet(_CONFIG, packet_dir)
