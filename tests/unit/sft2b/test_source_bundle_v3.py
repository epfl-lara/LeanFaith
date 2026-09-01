from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.source_bundle_v3 import (
    CORE_SELECTION_RULE,
    V2_RICH_MANIFEST_KEYS,
    ExternalHumanAttestationV3,
    SourceBundleV3Blocked,
    SourceBundleV3Error,
    _ProductionState,
    _secure_repo_pin,
    _validate_config,
    _verify_checksums,
    _verify_contained_attestation_pins,
    _write_release_files,
    canonical_source_line,
    plan_release,
    preflight_release,
    summarize_event_stream,
    verify_release,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_CONFIG = REPO_ROOT / "configs/sft2b/reform_diverse_full_sources_v3.json"
PRODUCTION_V2 = Path(
    "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/"
    "source_inputs/reform_diverse_full_v2"
)
PRODUCTION_REVIEW_PACKET = Path(
    "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/"
    "source_reviews/source_review_contract_v3_pending_human"
)


def _source(index: int) -> SourceRecord:
    nl = f"Standalone mathematical claim number {index}."
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


def _tiny_state_and_plan() -> tuple[_ProductionState, Any]:
    sources = tuple(_source(index) for index in range(1, 8))
    rows = {source.source_id: source for source in sources}
    one, two, three, four, five, six, seven = (source.source_id for source in sources)
    lines = {source.source_id: canonical_source_line(source) for source in sources}
    classes = {
        one: "library",
        two: "library",
        three: "workbook",
        four: "workbook",
        five: "legacy",
        six: "library",
        seven: "workbook",
    }
    state = _ProductionState(
        rows=rows,
        source_lines=lines,
        release_classes=classes,
        active_order=(one, two, three, four, five),
        core_ids=(one, two, three),
        tail_ids=(four, five),
        quarantine_ids=tuple(sorted((six, seven))),
        meta_ids=(one,),
        meta_evidence={one: "b" * 64},
        review_verdicts={
            three: "quarantine_solution_or_proof_fragment",
            six: "admit_standalone_aligned",
            seven: "quarantine_incomplete_or_nonstandalone",
        },
        review_evidence={three: "8" * 64, six: "9" * 64, seven: "a" * 64},
        reviews=(),
        mechanical_evidence={
            source.source_id: (
                "v2_workbook_automatic_disposition"
                if source.source_id in {six, seven}
                else "v2_source_selection_audit",
                sha256_hex(source.source_id.encode()),
            )
            for source in sources
        },
    )
    plan = plan_release(
        rows=state.rows,
        source_line_bytes=state.source_lines,
        release_class_by_id=state.release_classes,
        v2_active_order=state.active_order,
        v2_core_ids=state.core_ids,
        v2_tail_ids=state.tail_ids,
        v2_quarantine_ids=state.quarantine_ids,
        meta_quarantine_ids=state.meta_ids,
        review_verdicts=state.review_verdicts,
        review_evidence_sha256=state.review_evidence,
        meta_evidence_sha256=state.meta_evidence,
        target_core_count=3,
    )
    return state, plan


def _production_config() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(PRODUCTION_CONFIG.read_text(encoding="utf-8")))


def test_real_current_config_and_v2_preflight_reaches_authentic_human_gate(
    tmp_path: Path,
) -> None:
    if not PRODUCTION_V2.is_dir() or not PRODUCTION_REVIEW_PACKET.is_dir():
        pytest.skip("original-machine frozen v2/review-packet evidence is not mounted")
    output = tmp_path / "must-not-exist"
    with pytest.raises(SourceBundleV3Blocked, match="external human-attestation pins"):
        preflight_release(
            REPO_ROOT,
            config_path=PRODUCTION_CONFIG,
            v2_bundle_dir=PRODUCTION_V2,
            review_packet_dir=PRODUCTION_REVIEW_PACKET,
            output_dir=output,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        (("v2_evidence", "source_count"), 54_620, "v2 identity/counts"),
        (("meta_instruction_filter", "active_expected_rows"), 468, "meta-instruction"),
        (("human_review", "allow_model_substitution"), True, "no-model-substitution"),
        (("conservation", "expected_removals"), 1, "conservation"),
        (("publication", "private"), False, "publication"),
        (("generation_gate", "allow_core_generation"), True, "generation gates"),
    ),
)
def test_strict_config_invariants_reject_drift(
    path: tuple[str, str], value: object, message: str
) -> None:
    config = _production_config()
    cast_section = config[path[0]]
    assert isinstance(cast_section, dict)
    cast_section[path[1]] = value
    with pytest.raises(SourceBundleV3Error, match=message):
        _validate_config(REPO_ROOT, config)


@pytest.mark.parametrize(
    "substitution",
    (
        "/localhome/milikic/LeanFaith/src/leanfaith/sft2b/source_bundle_v3.py",
        "../LeanFaith/src/leanfaith/sft2b/source_bundle_v3.py",
    ),
)
def test_config_rejects_absolute_and_parent_code_pin_substitution(substitution: str) -> None:
    config = _production_config()
    config["builder"]["implementation_path"] = substitution
    with pytest.raises(SourceBundleV3Error, match="builder implementation"):
        _validate_config(REPO_ROOT, config)


def test_secure_repo_pin_rejects_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    expected = repo / "src/pkg/module.py"
    expected.parent.mkdir(parents=True)
    target = repo / "real.py"
    target.write_text("# real\n", encoding="utf-8")
    expected.symlink_to(target)
    with pytest.raises(SourceBundleV3Error, match="symlink"):
        _secure_repo_pin(
            repo,
            "src/pkg/module.py",
            expected_relative_path="src/pkg/module.py",
            imported_path=target,
            label="test module",
        )


def test_tiny_plan_is_class_aware_conserved_and_byte_stable() -> None:
    sources = tuple(_source(index) for index in range(1, 8))
    rows = {source.source_id: source for source in sources}
    one, two, three, four, five, six, seven = (source.source_id for source in sources)
    lines = {source.source_id: canonical_source_line(source) for source in sources}
    classes = {
        one: "library",
        two: "library",
        three: "workbook",
        four: "workbook",
        five: "legacy",
        six: "library",
        seven: "workbook",
    }
    plan = plan_release(
        rows=rows,
        source_line_bytes=lines,
        release_class_by_id=classes,
        v2_active_order=(one, two, three, four, five),
        v2_core_ids=(one, two, three),
        v2_tail_ids=(four, five),
        v2_quarantine_ids=(six, seven),
        meta_quarantine_ids=(one,),
        review_verdicts={
            three: "quarantine_solution_or_proof_fragment",
            six: "admit_standalone_aligned",
            seven: "quarantine_incomplete_or_nonstandalone",
        },
        review_evidence_sha256={three: "8" * 64, six: "9" * 64, seven: "a" * 64},
        meta_evidence_sha256={one: "b" * 64},
        target_core_count=3,
    )
    # Readmitted library source six stays in its class block and precedes Workbook/legacy backfill.
    assert plan.ordered_active_ids == (two, six, four, five)
    assert plan.core_ids == (two, six, four)
    assert plan.tail_ids == (five,)
    assert plan.quarantine_ids == tuple(sorted((one, three, seven)))
    assert plan.source_bytes == b"".join(lines[source_id] for source_id in (two, six, four, five))
    actions, reasons = summarize_event_stream(plan.event_stream)
    assert actions == plan.action_counts
    assert reasons == plan.reason_counts
    assert actions["quarantined_from_core"] == 2
    assert actions["moved_tail_to_core"] == 1
    assert actions["readmitted_to_core"] == 1
    assert actions["retained_quarantine"] == 1
    assert reasons["meta_instruction_quarantine"] == 1
    assert reasons["human_review_quarantine"] == 1
    assert reasons["human_review_readmission"] == 1
    assert reasons["core_boundary_reselection"] == 1
    assert reasons["dedup_displacement_addition"] == 0
    assert reasons["dedup_displacement_movement"] == 0


def test_planner_rejects_mutated_source_record_bytes() -> None:
    source = _source(1)
    with pytest.raises(SourceBundleV3Error, match="bytes"):
        plan_release(
            rows={source.source_id: source},
            source_line_bytes={source.source_id: canonical_source_line(source) + b" "},
            release_class_by_id={source.source_id: "library"},
            v2_active_order=(source.source_id,),
            v2_core_ids=(source.source_id,),
            v2_tail_ids=(),
            v2_quarantine_ids=(),
            meta_quarantine_ids=(),
            review_verdicts={},
            review_evidence_sha256={},
            meta_evidence_sha256={},
            target_core_count=1,
        )


class _SyntheticReviewReceipt:
    reviewer_identities = ("synthetic-reviewer-1",)

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "schema_version": "sft2b_human_review_verification_receipt_v3",
            "packet_sha256": "1" * 64,
            "reviews_sha256": "2" * 64,
            "review_count": 992,
            "reviewer_identities": ["synthetic-reviewer-1"],
            "verdict_counts": {"admit_standalone_aligned": 992},
            "escalation_count": 0,
            "schema_coverage_binding_passed": True,
            "authenticity_scope": "not_authenticated_by_schema_verifier",
        }


def _rewrite_checksum(bundle: Path, name: str) -> None:
    rows: dict[str, str] = {}
    for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, filename = line.partition("  ")
        rows[filename] = digest
    rows[name] = hash_file(bundle / name)
    (bundle / "SHA256SUMS").write_text(
        "".join(f"{digest}  {filename}\n" for filename, digest in sorted(rows.items())),
        encoding="utf-8",
    )


def test_tiny_staged_bundle_fresh_verify_and_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, plan = _tiny_state_and_plan()
    repo = tmp_path / "repo"
    repo.mkdir()
    reviews = tmp_path / "completed_reviews.jsonl"
    reviews.write_text("{}\n", encoding="utf-8")
    attestation = tmp_path / "attestation.json"
    attestation.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "sft2b_external_human_review_attestation_v3",
                "completed_reviews_sha256": hash_file(reviews),
                "reviewer_identities": ["synthetic-reviewer-1"],
                "attestor_identity": "synthetic-accountable-owner",
                "attested_at_utc": "2026-08-31T00:00:00Z",
                "attestation_scope": ("out_of_band_accountable_not_cryptographic_authentication"),
                "statement": (
                    "The named human reviewers personally reviewed the exact hash-bound "
                    "source fields; this is an accountable out-of-band attestation, not "
                    "cryptographic authentication."
                ),
            }
        )
        + b"\n"
    )
    v2_config = repo / "v2.json"
    v2_config.write_text("{}\n", encoding="utf-8")
    v2 = tmp_path / "v2"
    v2.mkdir()
    for name in (
        "sources.jsonl",
        "matched_50000_source_ids.json",
        "workbook_quarantine.jsonl",
        "legacy_tail_source_ids.json",
        "source_audit.jsonl",
        "library_docstring_corrections.jsonl",
        "semantic_alignment_audit.jsonl",
    ):
        (v2 / name).write_text(f"{name}\n", encoding="utf-8")
    frozen_manifest = {
        key: {"synthetic_fixture_only": True, "field": key} for key in V2_RICH_MANIFEST_KEYS
    }
    (v2 / "source_manifest.json").write_bytes(canonical_json_bytes(frozen_manifest) + b"\n")
    config = {
        "schema_version": "sft2b_reform_diverse_full_sources_v3",
        "matched_view_rows": 3,
        "v2_evidence": {
            "file_sha256": {
                name: hash_file(v2 / name)
                for name in (
                    "source_audit.jsonl",
                    "library_docstring_corrections.jsonl",
                    "semantic_alignment_audit.jsonl",
                    "source_manifest.json",
                )
            }
        },
        "v2_source_config": {"path": str(v2_config), "sha256": hash_file(v2_config)},
        "human_review": {
            "contract_path": str(repo / "review.json"),
            "completed_reviews_path": str(reviews),
            "completed_reviews_sha256": hash_file(reviews),
            "allowed_reviewer_identities": ["synthetic-reviewer-1"],
            "external_human_attestation_path": str(attestation),
            "external_human_attestation_sha256": hash_file(attestation),
            "allowed_attestor_identities": ["synthetic-accountable-owner"],
        },
        "conservation": {
            "expected_v2_universe_count": len(state.rows),
            "allow_new_sources": False,
            "allow_removed_sources": False,
            "expected_additions": 0,
            "expected_removals": 0,
            "expected_dedup_displacements": 0,
            "expected_dedup_displacement_movements": 0,
            "core_selection_rule": CORE_SELECTION_RULE,
        },
    }
    config_path = repo / "v3.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    packet = tmp_path / "packet"
    packet.mkdir()
    for name in (
        "automatic_dispositions.jsonl",
        "review_packet.jsonl",
        "review_packet_manifest.json",
        "SHA256SUMS",
    ):
        (packet / name).write_text(f"{name}\n", encoding="utf-8")
    prompt_payload = (
        canonical_json_bytes(
            {
                "schema_version": "sft2b_prompt_token_counts_v3",
                "source_count": len(plan.ordered_active_ids),
                "maximum_prompt_tokens": 10,
                "required_max_model_len": 4106,
                "rows": [],
            }
        )
        + b"\n"
    )
    monkeypatch.setattr(
        "leanfaith.sft2b.source_bundle_v3._prompt_counts",
        lambda *_args, **_kwargs: (prompt_payload, 10, 4106),
    )
    monkeypatch.setattr(
        "leanfaith.sft2b.source_bundle_v3.verify_completed_human_reviews",
        lambda *_args, **_kwargs: _SyntheticReviewReceipt(),
    )
    staged = tmp_path / "staged"
    _write_release_files(
        repo,
        config_path=config_path,
        config=config,
        v2_bundle_dir=v2,
        review_packet_dir=packet,
        state=state,
        plan=plan,
        output_dir=staged,
    )
    _verify_checksums(staged)
    staged_again = tmp_path / "staged-again"
    _write_release_files(
        repo,
        config_path=config_path,
        config=config,
        v2_bundle_dir=v2,
        review_packet_dir=packet,
        state=state,
        plan=plan,
        output_dir=staged_again,
    )
    assert {path.name: path.read_bytes() for path in staged.iterdir() if path.is_file()} == {
        path.name: path.read_bytes() for path in staged_again.iterdir() if path.is_file()
    }

    monkeypatch.setattr(
        "leanfaith.sft2b.source_bundle_v3._verify_static_inputs", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "leanfaith.sft2b.source_bundle_v3._verify_contained_review_evidence",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "leanfaith.sft2b.source_bundle_v3._load_production_state",
        lambda *_args, **_kwargs: state,
    )
    fresh = tmp_path / "fresh"
    shutil.copytree(staged, fresh)
    verify_release(
        repo,
        config_path=config_path,
        v2_bundle_dir=v2,
        review_packet_dir=packet,
        bundle_dir=fresh,
    )

    unexpected = tmp_path / "unexpected"
    shutil.copytree(staged, unexpected)
    (unexpected / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(SourceBundleV3Error, match="file set"):
        _verify_checksums(unexpected)

    manifest_bad = tmp_path / "manifest-bad"
    shutil.copytree(staged, manifest_bad)
    manifest = json.loads((manifest_bad / "source_manifest.json").read_text(encoding="utf-8"))
    manifest["source_count"] = 999
    (manifest_bad / "source_manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    _rewrite_checksum(manifest_bad, "source_manifest.json")
    with pytest.raises(SourceBundleV3Error, match="manifest counts"):
        verify_release(
            repo,
            config_path=config_path,
            v2_bundle_dir=v2,
            review_packet_dir=packet,
            bundle_dir=manifest_bad,
        )

    mechanical_bad = tmp_path / "mechanical-bad"
    shutil.copytree(staged, mechanical_bad)
    mechanical_path = mechanical_bad / "source_mechanical_evidence.jsonl"
    first_line = mechanical_path.read_bytes().splitlines(keepends=True)[0]
    mechanical_path.write_bytes(mechanical_path.read_bytes() + first_line)
    mechanical_manifest = json.loads(
        (mechanical_bad / "source_manifest.json").read_text(encoding="utf-8")
    )
    mechanical_manifest["data_files"]["source_mechanical_evidence.jsonl"]["sha256"] = hash_file(
        mechanical_path
    )
    (mechanical_bad / "source_manifest.json").write_bytes(
        canonical_json_bytes(mechanical_manifest) + b"\n"
    )
    _rewrite_checksum(mechanical_bad, "source_mechanical_evidence.jsonl")
    _rewrite_checksum(mechanical_bad, "source_manifest.json")
    with pytest.raises(SourceBundleV3Error, match="duplicate rows"):
        verify_release(
            repo,
            config_path=config_path,
            v2_bundle_dir=v2,
            review_packet_dir=packet,
            bundle_dir=mechanical_bad,
        )


def test_contained_attestation_mutation_is_rejected(tmp_path: Path) -> None:
    reviews = tmp_path / "human_reviews.jsonl"
    reviews.write_text("{}\n", encoding="utf-8")
    reviews_hash = hash_file(reviews)
    attestation = tmp_path / "external_human_attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "schema_version": "sft2b_external_human_review_attestation_v3",
                "completed_reviews_sha256": reviews_hash,
                "reviewer_identities": ["synthetic-reviewer-1"],
                "attestor_identity": "synthetic-accountable-owner",
                "attested_at_utc": "2026-08-31T00:00:00Z",
                "attestation_scope": ("out_of_band_accountable_not_cryptographic_authentication"),
                "statement": (
                    "The named human reviewers personally reviewed the exact hash-bound "
                    "source fields; this is an accountable out-of-band attestation, not "
                    "cryptographic authentication."
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    config = {
        "human_review": {
            "completed_reviews_sha256": reviews_hash,
            "allowed_reviewer_identities": ["synthetic-reviewer-1"],
            "external_human_attestation_sha256": hash_file(attestation),
            "allowed_attestor_identities": ["synthetic-accountable-owner"],
        }
    }
    assert _verify_contained_attestation_pins(config, tmp_path) == (
        reviews_hash,
        ("synthetic-reviewer-1",),
    )
    attestation.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SourceBundleV3Error, match="hash mismatch"):
        _verify_contained_attestation_pins(config, tmp_path)


def test_external_attestation_requires_timezone_aware_utc() -> None:
    payload = {
        "schema_version": "sft2b_external_human_review_attestation_v3",
        "completed_reviews_sha256": "1" * 64,
        "reviewer_identities": ["synthetic-reviewer-1"],
        "attestor_identity": "synthetic-accountable-owner",
        "attested_at_utc": "2026-08-31T01:00:00+01:00",
        "attestation_scope": "out_of_band_accountable_not_cryptographic_authentication",
        "statement": (
            "The named human reviewers personally reviewed the exact hash-bound source fields; "
            "this is an accountable out-of-band attestation, not cryptographic authentication."
        ),
    }
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        ExternalHumanAttestationV3.model_validate(payload)
