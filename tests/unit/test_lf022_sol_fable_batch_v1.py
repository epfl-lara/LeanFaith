"""Offline tests for v4-to-Sol/Fable weak-batch authoring."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.generation import lf022_sol_fable_batch_v1 as sol_fable
from leanfaith.generation.lf022_sol_fable_batch_v1 import (
    LF022SolFableBatchError,
    prepare_lf022_sol_fable_batch_v1,
)
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateRecord,
    _judge_visible_payload_hash,
)
from leanfaith.schemas.ids import make_id

_ORIGINAL_VERIFY_V4 = sol_fable.verify_lf022_judge_design_v4

REPO_ROOT = Path(".").resolve()
V4_ROOT = Path(
    "/storage/milikic/leanfaith/lf022_judge_design/sol_fable_public_v4/"
    "e81d93c752d232da8847eb97db611dc0e31eaee0e4304de418ee5cfe21f9eb6a"
)
COMPLETED_SOL_FABLE_ROOT = Path("/storage/milikic/leanfaith/lf022_weak_batches")
KEY = b"lf022-sol-fable-offline-test-key-v1"
GENERAL_SOL_XHIGH_SUMMARY = REPO_ROOT / "reports/generation/lf022_codex_sol_xhigh_v2_summary.json"
KIMI_SOL_XHIGH_SUMMARY = Path(
    "/storage/milikic/leanfaith/lf022_codex_audits/"
    "kimi_v4_641d13d_prefix256_v1_summary/summary.json"
)
QWEN_SOL_XHIGH_SUMMARY = Path(
    "/storage/milikic/leanfaith/lf022_qwen_snapshot_1019_codex_audit_0e8d84c/summary.json"
)
INCREMENTAL_SOL_XHIGH_SUMMARY = Path(
    "/storage/milikic/leanfaith/"
    "lf022_qwen_incremental_1357_codex_audit_0b29_unlimited_v1/summary.json"
)
DELTA_SOL_XHIGH_SUMMARY = Path(
    "/storage/milikic/leanfaith/lf022_codex_audit_summaries/"
    "qwen3_5_397b_incremental/"
    "06866609_mem16g_delta150_sol_xhigh_f7b398af_v1/summary.json"
)
HISTORICAL_SOL_XHIGH_SUMMARIES = (
    DELTA_SOL_XHIGH_SUMMARY,
    GENERAL_SOL_XHIGH_SUMMARY,
    INCREMENTAL_SOL_XHIGH_SUMMARY,
    KIMI_SOL_XHIGH_SUMMARY,
    QWEN_SOL_XHIGH_SUMMARY,
)


def _require_v4() -> None:
    if not (V4_ROOT / "manifest.json").is_file():
        pytest.skip(f"fixed local LF-022 v4 artifact unavailable: {V4_ROOT}")
    if any(not path.is_file() for path in HISTORICAL_SOL_XHIGH_SUMMARIES):
        pytest.skip("fixed historical Sol/xhigh summaries are unavailable")
    if not COMPLETED_SOL_FABLE_ROOT.is_dir():
        pytest.skip("canonical completed Sol/Fable root is unavailable")


def _fresh_qwen_records(
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int = 1,
    mutate_payload: bool = True,
):
    """Inject content-valid fresh records solely to test post-exclusion authoring mechanics."""

    verified = _ORIGINAL_VERIFY_V4(V4_ROOT)
    templates = [
        record
        for record in verified.records
        if record.source_partition_id == "qwen_snapshot1019"
        and record.source_record.prior_codex_diagnostic is None
    ][:count]
    assert len(templates) == count
    fresh = []
    for index, template in enumerate(templates):
        pair_id = make_id("pair", {"schema": "fresh_test_pair_v1", "index": index})
        theorem_id = make_id("thm", {"schema": "fresh_test_theorem_v1", "index": index})
        source_pair_values = template.source_record.pair.model_dump(mode="json")
        source_pair_values.update(
            {
                "pair_id": pair_id,
                "source_record_ids": tuple(sorted((theorem_id, template.source_record.variant_id))),
            }
        )
        if mutate_payload:
            source_pair_values["canonical_lean_b"] = (
                f"{source_pair_values['canonical_lean_b']}\n-- offline-fresh-test-{index}"
            )
        source_pair = type(template.source_record.pair).model_validate(source_pair_values)
        source_item_id = make_id(
            "lf022_supervision_source",
            {
                "schema": "lf022_supervision_source_v1",
                "lean_check_id": template.source_record.lean_check_id,
                "variant_id": template.source_record.variant_id,
                "pair_id": pair_id,
                "pair_admission_sha256": source_pair.admission_sha256,
            },
        )
        source_values = template.source_record.model_dump(mode="json")
        source_values.update(
            {
                "pair_id": pair_id,
                "pair": source_pair.model_dump(mode="json"),
                "pair_admission_sha256": source_pair.admission_sha256,
                "judge_visible_payload_sha256": _judge_visible_payload_hash(source_pair),
                "canonical_dispatch_pair_id": pair_id,
                "source_candidate_item_id": source_item_id,
                "canonical_dispatch_source_item_id": source_item_id,
            }
        )
        source_values.pop("candidate_inventory_record_id")
        source_record = LF022SupervisionCandidateRecord.model_validate(
            {
                **source_values,
                "candidate_inventory_record_id": make_id(
                    "lf022_supervision_candidate", source_values
                ),
            }
        )
        source_line = canonical_json_bytes(source_record.model_dump(mode="json")) + b"\n"
        wrapper_values = template.model_dump(mode="json")
        wrapper_values.update(
            {
                "source_record": source_record.model_dump(mode="json"),
                "source_record_line_sha256": sha256_hex(source_line),
                "source_candidate_inventory_record_id": (
                    source_record.candidate_inventory_record_id
                ),
                "pair_id": pair_id,
                "source_lineage_ids": source_pair.source_record_ids,
                "source_lineage_sha256": hash_canonical(list(source_pair.source_record_ids)),
                "judge_visible_payload_sha256": _judge_visible_payload_hash(source_pair),
            }
        )
        wrapper_values.pop("record_id")
        fresh.append(
            type(template).model_validate(
                {
                    **wrapper_values,
                    "record_id": make_id("lf022_judge_design", wrapper_values),
                }
            )
        )
    rebound = replace(verified, records=tuple(fresh))
    monkeypatch.setattr(sol_fable, "verify_lf022_judge_design_v4", lambda _: rebound)
    return tuple(fresh)


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    partition: str = "qwen_snapshot1019",
):
    _require_v4()
    _fresh_qwen_records(monkeypatch)
    return prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id=partition,
        offset_pairs=0,
        limit_pairs=1,
        randomization_key=KEY,
        output_dir=tmp_path / "prepared",
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
    )


def test_completed_scan_precedes_new_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_v4()
    _fresh_qwen_records(monkeypatch)
    output = tmp_path / "prepared"
    original = sol_fable._completed_sol_fable_exclusion_ledger
    observed = False

    def checked_scan(*, registry, input_dir):  # type: ignore[no-untyped-def]
        nonlocal observed
        observed = True
        assert input_dir == output / "authoring" / "inputs"
        assert not output.exists()
        return original(registry=registry, input_dir=input_dir)

    monkeypatch.setattr(sol_fable, "_completed_sol_fable_exclusion_ledger", checked_scan)
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id="qwen_snapshot1019",
        offset_pairs=0,
        limit_pairs=1,
        randomization_key=KEY,
        output_dir=output,
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
    )
    assert observed
    assert result.authoring_path.is_file()


def test_one_pair_creates_exact_sol_and_fable_ab_ba_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _prepare(tmp_path, monkeypatch)

    assert result.authoring.selected_pair_count == 1
    assert result.authoring.dispatch_cell_count == 4
    assert result.authoring.unique_source_theorem_lineage_count == 1
    assert result.authoring.lineage_diversity_status == (
        "distinct_source_theorem_lineages_not_full_ancestry_certified"
    )
    assert result.authoring.schema_version == 4
    assert result.authoring.method_version == "lf022_sol_fable_batch_v4"
    assert result.authoring.source_v4_artifact_path == str(V4_ROOT)
    assert result.authoring.historical_sol_xhigh_pair_count == 1510
    assert result.authoring.historical_sol_xhigh_theorem_lineage_count == 1339
    assert result.authoring.historical_sol_xhigh_judge_visible_payload_count == 1487
    assert result.authoring.historical_sol_xhigh_exclusion_complete
    assert result.authoring.selected_pairs_absent_from_historical_sol_xhigh
    assert result.authoring.selected_theorem_lineages_absent_from_historical_sol_xhigh
    assert len(result.authoring.historical_sol_xhigh_corpora) == 5
    assert {binding.finding_count for binding in result.authoring.historical_sol_xhigh_corpora} == {
        150,
        201,
        493,
        718,
        975,
    }
    assert result.authoring.historical_sol_xhigh_registry_id == (
        "lf022_sol_history_registry:"
        "4a9c11e1a9636233677044d8c1aecd0392db1216883ac19de456d1e00ba05a5e"
    )
    copied_registry = (
        result.authoring_path.parent
        / "authoring/inputs"
        / result.authoring.historical_sol_xhigh_registry_artifact
    )
    assert hash_file(copied_registry) == result.authoring.historical_sol_xhigh_registry_sha256
    completed_ledger = (
        result.authoring_path.parent
        / "authoring/inputs"
        / result.authoring.completed_sol_fable_ledger_artifact
    )
    assert hash_file(completed_ledger) == result.authoring.completed_sol_fable_ledger_sha256
    assert result.authoring.completed_sol_fable_pair_ids_sha256 == hash_canonical(
        list(result.authoring.completed_sol_fable_pair_ids)
    )
    assert result.authoring.completed_sol_fable_judge_visible_payload_sha256s_sha256 == (
        hash_canonical(list(result.authoring.completed_sol_fable_judge_visible_payload_sha256s))
    )
    assert result.authoring.selected_pairs_absent_from_completed_sol_fable
    assert result.authoring.selected_theorem_lineages_absent_from_completed_sol_fable
    assert result.authoring.selected_payloads_absent_from_historical_sol_xhigh
    assert result.authoring.selected_payloads_absent_from_completed_sol_fable
    assert not (
        set(result.authoring.selected_pair_ids) & set(result.authoring.completed_sol_fable_pair_ids)
    )
    assert not (
        set(result.authoring.selected_source_theorem_lineage_ids)
        & set(result.authoring.excluded_historical_sol_theorem_lineage_ids)
    )
    assert not (
        set(result.authoring.selected_source_theorem_lineage_ids)
        & set(result.authoring.completed_sol_fable_theorem_lineage_ids)
    )
    assert not (
        set(result.authoring.selected_judge_visible_payload_sha256s)
        & set(result.authoring.excluded_historical_sol_judge_visible_payload_sha256s)
    )
    assert not (
        set(result.authoring.selected_judge_visible_payload_sha256s)
        & set(result.authoring.completed_sol_fable_judge_visible_payload_sha256s)
    )
    assert not (
        set(result.authoring.selected_pair_ids)
        & set(result.authoring.excluded_historical_sol_pair_ids)
    )
    assert result.authoring.selected_pair_ids == tuple(
        sorted({cell.pair_id for cell in result.dispatches})
    )
    assert result.authoring.randomization_key_persisted_in_bundle is False
    assert result.authoring.randomization_key_reconstruction_prerequisite == (
        "external_secret_bytes_matching_persisted_sha256_required"
    )
    assert result.authoring.regeneration_completeness == (
        "requires_bound_v4_artifact_and_external_randomization_key_bytes"
    )
    assert {
        (cell.judge_family_id, cell.judge_slot, cell.orientation) for cell in result.dispatches
    } == {
        ("openai_codex_sol", "judge_A", "AB"),
        ("openai_codex_sol", "judge_A", "BA"),
        ("anthropic_fable", "judge_B", "AB"),
        ("anthropic_fable", "judge_B", "BA"),
    }
    assert result.spec.primary_eval_family_id == "deepseek_v4"
    assert result.spec.judge_a.model == "openai/gpt-5.6-sol"
    assert result.spec.judge_a.revision == (
        "provider-deployment-snapshot:"
        "225c02955198c5ab9da6f8c0c0b56430c7e9c02c5fe3c28aef02f0bd7ffcdd88"
    )
    assert result.spec.judge_a.decoding["reasoning_effort"] == "xhigh"
    assert result.spec.judge_a.decoding["output_schema_sha256"] == (
        "9de1b73c98a5df344ac158f77ead4b1b6e118b4c2f5585335fd5a3bcf0dea4d4"
    )
    assert result.spec.judge_a.decoding["codex_cli_version"] == "codex-cli 0.144.1"
    assert result.spec.judge_a.decoding["shell_tool_disabled"] is True
    assert result.spec.judge_a.decoding["sandbox"] == "read-only"
    assert result.spec.judge_a.decoding["web_search"] == "disabled"
    assert result.spec.judge_b.model == "anthropic/claude-fable-5"
    assert result.spec.judge_b.revision == (
        "provider-deployment-snapshot:"
        "f913bc9dc00dc187c2f5429e7deddfe604372422dcff7193e881dc7ada8e3ce4"
    )
    assert result.spec.judge_b.decoding["effort"] == "max"
    assert result.spec.judge_b.decoding["output_schema_sha256"] == (
        "f043ad37e1dc98d8df5655f42aeb9140371bd69671f13fd3e9af07111014e1be"
    )
    assert result.spec.judge_b.decoding["safe_mode"] is True
    assert result.spec.judge_b.decoding["tools_disabled"] is True
    assert result.spec.judge_b.decoding["session_persistence"] is False
    assert not result.authoring.semantic_labels_created
    assert not result.authoring.training_eligible


def test_candidate_manifest_uses_truthful_seed_and_authoring_binds_final_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _prepare(tmp_path, monkeypatch)
    candidate_manifest = json.loads(
        (result.spec_path.parent / "inputs/candidate_manifest.json").read_text()
    )

    assert candidate_manifest["schema_version"] == 4
    assert candidate_manifest["method_version"] == ("lf022_supervision_candidate_inventory_v4")
    assert "spec_sha256" not in candidate_manifest
    assert candidate_manifest["selection_spec_seed_sha256"] == (
        result.authoring.candidate_manifest_selection_spec_seed_sha256
    )
    assert result.authoring.weak_batch_spec_sha256 == hash_file(result.spec_path)
    assert candidate_manifest["selection_spec_seed_sha256"] != hash_file(result.spec_path)


def test_selected_v3_bytes_and_source_hashes_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_v4()
    source_manifest = V4_ROOT / "inputs/qwen_snapshot1019/manifest.json"
    source_records = V4_ROOT / "inputs/qwen_snapshot1019/candidates.jsonl"
    before = {
        source_manifest: hash_file(source_manifest),
        source_records: hash_file(source_records),
    }

    fresh = _fresh_qwen_records(monkeypatch)
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id="qwen_snapshot1019",
        offset_pairs=0,
        limit_pairs=1,
        randomization_key=KEY,
        output_dir=tmp_path / "prepared",
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
    )

    after = {path: hash_file(path) for path in before}
    assert before == after
    selected_bytes = (result.spec_path.parent / "inputs/candidates.jsonl").read_bytes()
    assert selected_bytes == (
        canonical_json_bytes(fresh[0].source_record.model_dump(mode="json")) + b"\n"
    )
    assert result.authoring.selected_source_bytes_preserved


def test_authoring_is_an_exact_immutable_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _prepare(tmp_path, monkeypatch)
    second = _prepare(tmp_path, monkeypatch)

    assert second.authoring == first.authoring
    assert second.spec == first.spec
    assert second.dispatch_manifest == first.dispatch_manifest
    assert second.dispatches == first.dispatches
    assert hash_file(second.authoring_path) == hash_file(first.authoring_path)


def test_live_authorization_is_explicit_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_v4()
    _fresh_qwen_records(monkeypatch)
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id="qwen_snapshot1019",
        offset_pairs=0,
        limit_pairs=1,
        randomization_key=KEY,
        output_dir=tmp_path / "authorized",
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        authorize_live_provider_calls=True,
    )
    assert result.spec.execution_authorization == ("live_provider_calls_explicitly_authorized")
    assert result.spec.live_provider_calls_authorized
    assert result.dispatch_manifest.live_provider_calls_authorized
    assert result.authoring.execution_authorization == ("live_provider_calls_explicitly_authorized")
    assert result.authoring.live_provider_calls_authorized


@pytest.mark.parametrize("limit", [0, 65])
def test_pair_cap_is_enforced(tmp_path: Path, limit: int) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="limit_pairs"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=limit,
            randomization_key=KEY,
            output_dir=tmp_path / "prepared",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_offset_overflow_is_rejected(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="qwen_snapshot1019",
            offset_pairs=409,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "prepared",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_incomplete_historical_sol_corpus_set_is_rejected(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="complete registered corpus set"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "missing",
            # This was the formerly accepted, incomplete two-summary set.  It
            # omits the canonical 718-Qwen corpus and must fail before selection.
            historical_sol_xhigh_summary_paths=(
                GENERAL_SOL_XHIGH_SUMMARY,
                KIMI_SOL_XHIGH_SUMMARY,
            ),
        )


def test_former_three_corpus_registry_input_is_now_rejected(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="complete registered corpus set"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="qwen_snapshot1019",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "formerly-incomplete-three",
            historical_sol_xhigh_summary_paths=(
                GENERAL_SOL_XHIGH_SUMMARY,
                KIMI_SOL_XHIGH_SUMMARY,
                QWEN_SOL_XHIGH_SUMMARY,
            ),
        )


def test_canonical_completed_sol_fable_root_is_scanned_exactly(tmp_path: Path) -> None:
    _require_v4()
    _, registry = sol_fable._historical_sol_xhigh_registry(REPO_ROOT)
    ledger = sol_fable._completed_sol_fable_exclusion_ledger(
        registry=registry,
        input_dir=tmp_path / "inputs",
    )

    assert ledger.scanned_root == str(COMPLETED_SOL_FABLE_ROOT)
    assert len(ledger.completed_batches) >= 1
    assert len(ledger.excluded_pair_ids) >= 1
    assert len(ledger.excluded_theorem_lineage_ids) >= 1
    assert len(ledger.excluded_judge_visible_payload_sha256s) >= 1
    assert ledger.excluded_pair_ids_sha256 == hash_canonical(list(ledger.excluded_pair_ids))
    assert ledger.excluded_theorem_lineage_ids_sha256 == hash_canonical(
        list(ledger.excluded_theorem_lineage_ids)
    )
    assert ledger.excluded_judge_visible_payload_sha256s_sha256 == hash_canonical(
        list(ledger.excluded_judge_visible_payload_sha256s)
    )
    assert "sol_fable_live_smoke_qwen_n1_v3/batch/final/finalization_manifest.json" in {
        item.relative_finalization_artifact for item in ledger.completed_batches
    }


def test_executed_unfinalized_sol_fable_batch_fails_closed(tmp_path: Path) -> None:
    _require_v4()
    _, registry = sol_fable._historical_sol_xhigh_registry(REPO_ROOT)
    root = tmp_path / "completed"
    (root / "keys").mkdir(parents=True)
    (root / "prepared-only" / "batch").mkdir(parents=True)
    attempted_batch = root / "executed-partial" / "batch"
    attempted_batch.mkdir(parents=True)
    # Both production judge adapters persist this marker before invoking their
    # first external process. Provider outputs and run manifests may live in a
    # separate judge-run root, so the scanner must not depend on batch/raw.
    (attempted_batch / "execution_started.json").write_text("{}\n", encoding="utf-8")
    rebound = registry.model_copy(update={"completed_sol_fable_root": str(root)})

    with pytest.raises(LF022SolFableBatchError, match=r"executed .* is not finalized"):
        sol_fable._completed_sol_fable_exclusion_ledger(
            registry=rebound,
            input_dir=tmp_path / "inputs",
        )


def test_completed_scanner_binds_execution_to_dispatch_manifest(tmp_path: Path) -> None:
    _require_v4()
    _, registry = sol_fable._historical_sol_xhigh_registry(REPO_ROOT)
    root = tmp_path / "completed"
    root.mkdir()
    source = COMPLETED_SOL_FABLE_ROOT / "sol_fable_live_smoke_qwen_n1_v3"
    copied = root / "finalized"
    shutil.copytree(source, copied)
    dispatch = copied / "batch" / "dispatch_manifest.json"
    dispatch.write_bytes(dispatch.read_bytes() + b"\n")
    rebound = registry.model_copy(update={"completed_sol_fable_root": str(root)})

    with pytest.raises(LF022SolFableBatchError, match="dispatch differs from execution"):
        sol_fable._completed_sol_fable_exclusion_ledger(
            registry=rebound,
            input_dir=tmp_path / "inputs",
        )


def test_completed_scanner_binds_dispatch_to_source_candidates(tmp_path: Path) -> None:
    _require_v4()
    _, registry = sol_fable._historical_sol_xhigh_registry(REPO_ROOT)
    root = tmp_path / "completed"
    root.mkdir()
    source = COMPLETED_SOL_FABLE_ROOT / "sol_fable_live_smoke_qwen_n1_v3"
    copied = root / "finalized"
    shutil.copytree(source, copied)
    candidates = copied / "batch" / "inputs" / "candidate_records.jsonl"
    candidates.write_bytes(candidates.read_bytes() + b"\n")
    rebound = registry.model_copy(update={"completed_sol_fable_root": str(root)})

    with pytest.raises(LF022SolFableBatchError, match="source candidates differ from dispatch"):
        sol_fable._completed_sol_fable_exclusion_ledger(
            registry=rebound,
            input_dir=tmp_path / "inputs",
        )


def test_theorem_lineage_freshness_blocks_sibling_pair() -> None:
    _require_v4()
    verified = sol_fable.verify_lf022_judge_design_v4(V4_ROOT)
    record = next(
        item for item in verified.records if item.source_partition_id == "qwen_snapshot1019"
    )
    theorem_id = sol_fable._theorem_lineage(record)

    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        sol_fable._selected(
            (record,),
            partition_id="qwen_snapshot1019",
            offset_pairs=0,
            limit_pairs=1,
            excluded_pair_ids=(),
            excluded_theorem_lineage_ids=(theorem_id,),
            excluded_judge_visible_payload_sha256s=(),
        )


def test_judge_visible_payload_freshness_blocks_rekeyed_identical_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_v4()
    record = _fresh_qwen_records(monkeypatch, mutate_payload=False)[0]
    payload_hash = record.source_record.judge_visible_payload_sha256

    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        sol_fable._selected(
            (record,),
            partition_id="qwen_snapshot1019",
            offset_pairs=0,
            limit_pairs=1,
            excluded_pair_ids=(),
            excluded_theorem_lineage_ids=(),
            excluded_judge_visible_payload_sha256s=(payload_hash,),
        )


def test_freshness_validator_rejects_rekeyed_identical_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_v4()
    record = _fresh_qwen_records(monkeypatch, mutate_payload=False)[0]
    payload_hash = record.source_record.judge_visible_payload_sha256

    with pytest.raises(LF022SolFableBatchError, match="judge-visible content"):
        sol_fable._validate_selected_fresh(
            (record,),
            (),
            (),
            (payload_hash,),
        )


def test_reused_pair_is_rejected_by_freshness_validation() -> None:
    _require_v4()
    verified = sol_fable.verify_lf022_judge_design_v4(V4_ROOT)
    historical_pair_ids = {
        json.loads(line)["pair_id"]
        for summary_path in HISTORICAL_SOL_XHIGH_SUMMARIES
        for line in Path(json.loads(summary_path.read_text())["findings_artifact"])
        .read_text()
        .splitlines()
    }
    reused = next(record for record in verified.records if record.pair_id in historical_pair_ids)
    with pytest.raises(LF022SolFableBatchError, match="already has a historical Sol/xhigh"):
        sol_fable._validate_selected_fresh((reused,), tuple(sorted(historical_pair_ids)))


def test_kimi_partition_has_no_fresh_pair(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "kimi-exhausted",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_qwen_partition_has_no_fresh_pair_after_complete_718_exclusion(
    tmp_path: Path,
) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="qwen_snapshot1019",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "qwen-exhausted",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_qwen_partition_remains_homogeneous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _prepare(tmp_path, monkeypatch, partition="qwen_snapshot1019")
    assert result.authoring.proposer_family_id == "qwen3"
    assert {cell.proposer_family_id for cell in result.dispatches} == {"qwen3"}


def test_eight_pair_selection_has_distinct_theorem_lineages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_v4()
    _fresh_qwen_records(monkeypatch, count=8)
    partition = "qwen_snapshot1019"
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id=partition,
        offset_pairs=0,
        limit_pairs=8,
        randomization_key=KEY,
        output_dir=tmp_path / partition,
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
    )
    candidates = tuple(
        LF022SupervisionCandidateRecord.model_validate_json(line)
        for line in (result.spec_path.parent / "inputs/candidates.jsonl").read_bytes().splitlines()
    )
    theorem_ids = {
        lineage
        for candidate in candidates
        for lineage in candidate.pair.source_record_ids
        if lineage.startswith("thm:")
    }
    assert len(theorem_ids) == 8
    assert result.authoring.unique_source_theorem_lineage_count == 8


def test_symlinked_output_is_rejected(tmp_path: Path) -> None:
    _require_v4()
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(LF022SolFableBatchError, match="symlink"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=linked,
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )
