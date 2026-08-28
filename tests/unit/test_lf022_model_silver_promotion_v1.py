"""Fail-closed tests for LF-022 model-adjudicated training silver."""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation import lf022_codex_audit as codex_audit
from leanfaith.generation import lf022_model_silver_promotion_v1 as promotion
from leanfaith.generation import lf022_sol_fable_batch_v1 as sol_fable
from leanfaith.generation.claude_fable_judge_v1 import load_claude_fable_judge_config
from leanfaith.generation.codex_sol_judge_v1 import load_codex_sol_judge_config
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditFinding,
    LF022CodexAuditInput,
    LF022CodexAuditSummary,
    LF022CodexAuditSummaryBucket,
)
from leanfaith.generation.lf022_model_silver_promotion_v1 import (
    DEFAULT_POLICY,
    LF022ModelSilverPromotionError,
    load_model_silver_promotion_policy_v1,
    promote_finalized_lf022_batch_to_model_silver_v1,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchError,
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    LF022WeakExecutionManifest,
    LF022WeakFinalizationManifest,
    _load_canonical_jsonl,
    _load_canonical_model,
    _load_prepared_batch,
    execute_or_resume_lf022_weak_batch,
    finalize_lf022_weak_batch,
    prepare_lf022_weak_batch,
)
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    ProviderIdentity,
)
from leanfaith.generation.weak_supervision import (
    PublicLeanJudgePair,
    make_swapped_presentations,
)
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import DefeqValue, EvidenceRecord, JudgmentValue
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.model_silver import (
    ModelAdjudicatedSilverPromotionManifestV1,
    ModelAdjudicatedSilverPromotionRecordV1,
    ModelAdjudicatedSilverRejectionV1,
)
from tests.unit.test_lf022_weak_batch import (
    KEY as FIXTURE_KEY,
)
from tests.unit.test_lf022_weak_batch import (
    _candidate_manifest_v3_without_codex,
    _candidate_v3_without_codex,
)

REPO_ROOT = Path(".").resolve()
FIXED_BATCH_ROOT = Path("/fixture/is/installed/by/module_fixture")
PORTABLE_REPO_ROOT = Path("/fixture/is/installed/by/module_fixture")
PORTABLE_POLICY_PATH = Path("/fixture/is/installed/by/module_fixture")


def _require_fixed_batch() -> None:
    required = (
        FIXED_BATCH_ROOT / "dispatch_manifest.json",
        FIXED_BATCH_ROOT / "execution_manifest.json",
        FIXED_BATCH_ROOT / "final/finalization_manifest.json",
    )
    assert all(path.is_file() for path in required), FIXED_BATCH_ROOT


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hash_file(path)


def _write_json(path: Path, value: object) -> str:
    return _write(path, canonical_json_bytes(value) + b"\n")


def _fixture_response(orientation: str) -> str:
    # Closed theorem implication votes are deliberately auxiliary metadata and
    # do not define the F1 relation.  Matching yes/yes votes reproduce the
    # reviewed live fixture while the canonical relation remains A_stronger.
    return json.dumps(
        {
            "same_claim_answer": "not_same_claim",
            "relation": "A_stronger" if orientation == "AB" else "B_stronger",
            "A_implies_B": "yes",
            "B_implies_A": "yes",
            "error_types": ["E01"],
            "confidence": 0.96,
            "rationale": "Equality is a stricter claim than non-strict inequality.",
            "needs_expert_review": False,
        }
    )


def _historical_fixture(root: Path) -> tuple[Path, str, Path]:
    audit_root = root / "historical" / "audit"
    manifest_path = audit_root / "manifest.json"
    manifest_sha = _write_json(manifest_path, {"portable_fixture": True})
    pair = PublicLeanJudgePair(
        pair_id=make_id("pair", {"portable_historical": True}),
        canonical_lean_a="theorem old_source (n : Nat) : n = n",
        canonical_lean_b="theorem old_candidate (n : Nat) : n + 0 = n",
        source_record_ids=("thm:" + "a" * 64, "var:" + "b" * 64),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    presentation = next(
        item
        for item in make_swapped_presentations(
            source=pair, judge_slot="judge_A", randomization_key=b"h" * 32
        )
        if item.orientation == "AB"
    )
    input_values: dict[str, object] = {
        "schema_version": 1,
        "audit_only": True,
        "lean_check_id": "lf022_lean_check:" + "c" * 64,
        "variant_id": "var:" + "b" * 64,
        "pair": pair,
        "presentation": presentation,
        "source_task_sha256": "d" * 64,
        "source_variant_artifact_sha256": "e" * 64,
        "source_variant_line_sha256": "f" * 64,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    audit_item_id = codex_audit._audit_item_id_values(
        lean_check_id=input_values["lean_check_id"],  # type: ignore[arg-type]
        variant_id=input_values["variant_id"],  # type: ignore[arg-type]
        pair=pair,
        presentation=presentation,
        source_task_sha256=input_values["source_task_sha256"],  # type: ignore[arg-type]
        source_variant_artifact_sha256=input_values["source_variant_artifact_sha256"],  # type: ignore[arg-type]
        source_variant_line_sha256=input_values["source_variant_line_sha256"],  # type: ignore[arg-type]
    )
    audit_input = LF022CodexAuditInput.model_validate(
        {**input_values, "audit_item_id": audit_item_id}
    )
    digest = audit_item_id.split(":", maxsplit=1)[1]
    _write_json(
        audit_root / "items" / digest[:2] / digest / "input.json",
        audit_input.model_dump(mode="json"),
    )
    finding_values: dict[str, object] = {
        "schema_version": 1,
        "audit_item_id": audit_item_id,
        "lean_check_id": audit_input.lean_check_id,
        "pair_id": pair.pair_id,
        "variant_id": audit_input.variant_id,
        "source_record_ids": pair.source_record_ids,
        "proposer_family_id": "qwen3",
        "same_claim_answer": "same_claim",
        "relation": "equivalent",
        "a_implies_b": "yes",
        "b_implies_a": "yes",
        "error_types": (),
        "confidence": 0.99,
        "needs_expert_review": False,
        "final_message_sha256": "1" * 64,
        "parsed_response_sha256": "2" * 64,
        "audit_only": True,
        "human_label": False,
        "semantic_label": False,
        "silver_record": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    finding = LF022CodexAuditFinding.model_validate(
        {
            **finding_values,
            "finding_id": make_id("lf022_codex_audit_finding", finding_values),
        }
    )
    findings_path = audit_root / "findings.jsonl"
    findings_sha = _write(
        findings_path, canonical_json_bytes(finding.model_dump(mode="json")) + b"\n"
    )
    bucket = LF022CodexAuditSummaryBucket(
        total_count=1,
        same_claim_counts={"same_claim": 1},
        relation_counts={"equivalent": 1},
        implication_counts={"A=yes,B=yes": 1},
        error_type_counts={},
        needs_expert_review_count=0,
        confidence_count=1,
        confidence_mean=0.99,
        confidence_min=0.99,
        confidence_max=0.99,
    )
    summary_values: dict[str, object] = {
        "schema_version": 1,
        "method_version": "lf022_codex_audit_summary_v1",
        "audit_manifest": str(manifest_path),
        "audit_manifest_sha256": manifest_sha,
        "audit_method_version": "lf022_codex_audit_v2",
        "checks_artifact": "portable-checks.jsonl",
        "checks_sha256": "3" * 64,
        "response_artifact_set_sha256": "4" * 64,
        "parent_audit_bindings": (),
        "findings_artifact": str(findings_path),
        "findings_sha256": findings_sha,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "total_check_count": 1,
        "lean_check_outcome_counts": {"elaborates": 1},
        "lean_valid_check_count": 1,
        "lean_invalid_check_count": 0,
        "completed_judgment_count": 1,
        "overall": bucket.model_dump(mode="json"),
        "by_proposer_family": {"qwen3": bucket.model_dump(mode="json")},
        "audit_only": True,
        "human_labels_created": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    summary_id_values = dict(summary_values)
    for key in ("audit_manifest", "checks_artifact", "findings_artifact", "parent_audit_bindings"):
        summary_id_values.pop(key)
    summary = LF022CodexAuditSummary.model_validate(
        {
            **summary_values,
            "summary_id": make_id("lf022_codex_audit_summary", summary_id_values),
        }
    )
    summary_path = root / "historical" / "summary.json"
    summary_sha = _write_json(summary_path, summary.model_dump(mode="json"))
    return summary_path, summary_sha, findings_path


def _build_self_contained_finalized_batch(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    inputs = root / "inputs"
    candidate = _candidate_v3_without_codex()
    record_bytes = canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"
    records_path = inputs / "candidates.jsonl"
    records_sha = _write(records_path, record_bytes)
    manifest = _candidate_manifest_v3_without_codex(record_bytes)
    manifest_values = manifest.model_dump(mode="json", exclude={"inventory_id"})
    manifest_values.update(
        {
            "judge_a_family_id": "openai_codex_sol",
            "judge_b_family_id": "anthropic_fable",
            "primary_eval_judge_family_id": "deepseek_v4",
        }
    )
    id_payload = {
        key: value
        for key, value in manifest_values.items()
        if key
        not in {
            "records_artifact",
            "public_sample_artifact",
            "summary_artifact",
            "spec_sha256",
        }
    }
    manifest = type(manifest).model_validate(
        {
            **manifest_values,
            "inventory_id": make_id("lf022_supervision_inventory", id_payload),
        }
    )
    manifest_path = inputs / "manifest.json"
    manifest_sha = _write_json(manifest_path, manifest.model_dump(mode="json"))

    weak_config = REPO_ROOT / "configs/judges/weak_supervision.yaml"
    family_matrix = REPO_ROOT / "configs/generation/lf022_sol_fable_family_matrix_v1.json"
    sol = load_codex_sol_judge_config(
        REPO_ROOT / "configs/generation/lf022_codex_sol_judge_v1.yaml"
    ).config
    fable = load_claude_fable_judge_config(
        REPO_ROOT / "configs/generation/lf022_claude_fable_judge_v1.yaml"
    ).config
    fable_decoding = {
        "effort": fable.effort,
        "system_prompt_sha256": fable.system_prompt_sha256,
        "output_schema_sha256": fable.output_schema_sha256,
        "claude_cli_version": fable.claude_cli_version,
        "claude_binary_sha256": fable.claude_binary_sha256,
        "structured_output": True,
        "safe_mode": True,
        "tools_disabled": True,
        "session_persistence": False,
    }
    spec = LF022WeakBatchSpec(
        batch_name="model-silver-portable-unit-fixture",
        candidate_manifest=BoundArtifact(path=str(manifest_path), sha256=manifest_sha),
        candidate_records=BoundArtifact(path=str(records_path), sha256=records_sha),
        weak_supervision_config=BoundArtifact(path=str(weak_config), sha256=hash_file(weak_config)),
        production_family_matrix=BoundArtifact(
            path=str(family_matrix), sha256=hash_file(family_matrix)
        ),
        randomization_key_sha256=sha256_hex(FIXTURE_KEY),
        judge_a=JudgeEndpointPin(
            provider_slot="judge_A",
            provider=sol.provider,
            model=sol.registry_model_id,
            family_id=sol.model_family,
            revision=sol.endpoint_revision,
            decoding=sol.endpoint_decoding,
        ),
        judge_b=JudgeEndpointPin(
            provider_slot="judge_B",
            provider=fable.provider,
            model=fable.registry_model_id,
            family_id=fable.model_family,
            revision=fable.endpoint_revision,
            decoding=fable_decoding,
        ),
        primary_eval_family_id="deepseek_v4",
    )
    spec_path = inputs / "batch_spec.json"
    spec_sha = _write_json(spec_path, spec.model_dump(mode="json"))
    batch_root = root / "bundle" / "batch"
    dispatches, _ = prepare_lf022_weak_batch(
        repo_root=REPO_ROOT,
        spec_path=spec_path,
        expected_spec_sha256=spec_sha,
        randomization_key=FIXTURE_KEY,
        output_dir=batch_root,
    )
    responses: dict[str, dict[str, str]] = {"judge_A": {}, "judge_B": {}}
    for dispatch in dispatches:
        responses[dispatch.judge_slot][dispatch.provider_request_hash] = _fixture_response(
            dispatch.orientation
        )
    identities = {
        "judge_A": ProviderIdentity(
            provider=sol.provider,
            model=sol.registry_model_id,
            revision=sol.endpoint_revision,
            transport="fixture",
        ),
        "judge_B": ProviderIdentity(
            provider=fable.provider,
            model=fable.registry_model_id,
            revision=fable.endpoint_revision,
            transport="fixture",
        ),
    }
    raw_roots = {slot: batch_root / "raw" / slot for slot in identities}
    providers = {
        slot: DeterministicFixtureProvider(
            identity=identities[slot],
            raw_response_root=raw_roots[slot],
            responses=responses[slot],
        )
        for slot in identities
    }
    execute_or_resume_lf022_weak_batch(
        batch_root=batch_root,
        providers=providers,  # type: ignore[arg-type]
        raw_response_roots=raw_roots,  # type: ignore[arg-type]
        now=lambda: datetime.datetime(2026, 8, 12, tzinfo=datetime.UTC),
    )
    _, weak_candidates, _ = finalize_lf022_weak_batch(batch_root=batch_root)
    assert len(weak_candidates) == 1
    assert weak_candidates[0].status == "candidate_consensus"
    summary_path, summary_sha, findings_path = _historical_fixture(root)
    corpus = sol_fable.HistoricalSolXhighCorpusPin(
        corpus_id="portable_fixture",
        canonical_summary_path=str(summary_path),
        summary_sha256=summary_sha,
        findings_sha256=hash_file(findings_path),
        finding_count=1,
        unique_pair_count=1,
    )
    registry_values: dict[str, object] = {
        "schema_version": 2,
        "method_version": "lf022_historical_sol_xhigh_registry_v2",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "expected_union_pair_count": 1,
        "expected_union_judge_visible_payload_count": 1,
        "completed_sol_fable_root": str(root / "completed"),
        "completed_sol_fable_scan_policy": "recursive_finalized_batches_fail_on_partial_v1",
        "corpora": (corpus.model_dump(mode="json"),),
    }
    (root / "completed").mkdir()
    registry_id_payload = {
        key: registry_values[key]
        for key in (
            "schema_version",
            "method_version",
            "model",
            "reasoning_effort",
            "expected_union_pair_count",
            "expected_union_judge_visible_payload_count",
            "completed_sol_fable_scan_policy",
        )
    }
    registry_id_payload["corpora"] = [
        corpus.model_dump(mode="json", exclude={"canonical_summary_path"})
    ]
    registry = sol_fable.HistoricalSolXhighRegistry.model_validate(
        {
            **registry_values,
            "registry_id": make_id("lf022_sol_history_registry", registry_id_payload),
        }
    )
    monkeypatch.setattr(
        sol_fable, "_REQUIRED_HISTORICAL_SOL_XHIGH_REGISTRY_ID", registry.registry_id
    )
    authoring_inputs = batch_root.parent / "authoring" / "inputs"
    bindings, historical_pairs, historical_theorems, historical_payloads = (
        sol_fable._historical_sol_xhigh_corpora(
            summary_paths=(summary_path,), input_dir=authoring_inputs, registry=registry
        )
    )
    copied_registry = authoring_inputs / "historical_sol_xhigh/registry.json"
    registry_sha = _write_json(copied_registry, registry.model_dump(mode="json"))
    ledger_values: dict[str, object] = {
        "schema_version": 1,
        "method_version": "lf022_completed_sol_fable_exclusion_v1",
        "scanned_root": str(root / "completed"),
        "scan_policy": "recursive_finalized_batches_fail_on_partial_v1",
        "completed_batches": (),
        "excluded_pair_ids": (),
        "excluded_pair_ids_sha256": promotion.hash_canonical([]),
        "excluded_theorem_lineage_ids": (),
        "excluded_theorem_lineage_ids_sha256": promotion.hash_canonical([]),
        "excluded_judge_visible_payload_sha256s": (),
        "excluded_judge_visible_payload_sha256s_sha256": promotion.hash_canonical([]),
    }
    ledger = sol_fable.CompletedSolFableExclusionLedger.model_validate(
        {
            **ledger_values,
            "ledger_id": make_id(
                "lf022_sol_fable_exclusion",
                {key: value for key, value in ledger_values.items() if key != "scanned_root"},
            ),
        }
    )
    ledger_path = authoring_inputs / "completed_sol_fable/ledger.json"
    ledger_sha = _write_json(ledger_path, ledger.model_dump(mode="json"))
    dispatch_manifest = _load_canonical_model(
        batch_root / "dispatch_manifest.json", LF022WeakDispatchManifest
    )
    assert isinstance(dispatch_manifest, LF022WeakDispatchManifest)
    source_line_sha = sha256_hex(record_bytes)
    theorem_id = next(
        value for value in candidate.pair.source_record_ids if value.startswith("thm:")
    )
    authoring_values: dict[str, object] = {
        "schema_version": 4,
        "method_version": "lf022_sol_fable_batch_v4",
        "source_v4_artifact_path": str(root / "portable-v4"),
        "source_v4_inventory_id": "lf022_judge_design_inventory:" + "7" * 64,
        "source_v4_manifest_sha256": "8" * 64,
        "source_v4_records_sha256": "9" * 64,
        "source_partition_id": "qwen_snapshot1019",
        "proposer_family_id": dispatch_manifest.proposer_family_id,
        "offset_pairs": 0,
        "excluded_historical_sol_pair_ids": historical_pairs,
        "excluded_historical_sol_pair_ids_sha256": promotion.hash_canonical(list(historical_pairs)),
        "excluded_historical_sol_theorem_lineage_ids": historical_theorems,
        "excluded_historical_sol_theorem_lineage_ids_sha256": promotion.hash_canonical(
            list(historical_theorems)
        ),
        "excluded_historical_sol_judge_visible_payload_sha256s": historical_payloads,
        "excluded_historical_sol_judge_visible_payload_sha256s_sha256": promotion.hash_canonical(
            list(historical_payloads)
        ),
        "historical_sol_xhigh_registry_id": registry.registry_id,
        "historical_sol_xhigh_registry_sha256": registry_sha,
        "historical_sol_xhigh_corpora": bindings,
        "historical_sol_xhigh_corpora_sha256": promotion.hash_canonical(
            [item.model_dump(mode="json") for item in bindings]
        ),
        "historical_sol_xhigh_pair_count": len(historical_pairs),
        "historical_sol_xhigh_theorem_lineage_count": len(historical_theorems),
        "historical_sol_xhigh_judge_visible_payload_count": len(historical_payloads),
        "completed_sol_fable_ledger_id": ledger.ledger_id,
        "completed_sol_fable_ledger_sha256": ledger_sha,
        "completed_sol_fable_pair_ids": (),
        "completed_sol_fable_pair_ids_sha256": promotion.hash_canonical([]),
        "completed_sol_fable_theorem_lineage_ids": (),
        "completed_sol_fable_theorem_lineage_ids_sha256": promotion.hash_canonical([]),
        "completed_sol_fable_judge_visible_payload_sha256s": (),
        "completed_sol_fable_judge_visible_payload_sha256s_sha256": promotion.hash_canonical([]),
        "selected_pair_count": 1,
        "unique_source_theorem_lineage_count": 1,
        "selected_pair_ids": (candidate.pair_id,),
        "selected_source_record_ids": (candidate.candidate_inventory_record_id,),
        "selected_source_theorem_lineage_ids": (theorem_id,),
        "selected_source_line_sha256s": (source_line_sha,),
        "selected_judge_visible_payload_sha256s": (candidate.judge_visible_payload_sha256,),
        "selected_source_theorem_lineage_ids_sha256": promotion.hash_canonical([theorem_id]),
        "selected_source_record_ids_sha256": promotion.hash_canonical(
            [candidate.candidate_inventory_record_id]
        ),
        "selected_source_line_sha256s_sha256": promotion.hash_canonical([source_line_sha]),
        "selected_judge_visible_payload_sha256s_sha256": promotion.hash_canonical(
            [candidate.judge_visible_payload_sha256]
        ),
        "selected_pair_ids_sha256": promotion.hash_canonical([candidate.pair_id]),
        "candidate_manifest_sha256": dispatch_manifest.candidate_manifest_sha256,
        "candidate_records_sha256": dispatch_manifest.candidate_records_sha256,
        "weak_config_sha256": dispatch_manifest.weak_supervision_config_sha256,
        "family_matrix_sha256": dispatch_manifest.production_family_matrix_sha256,
        "candidate_manifest_selection_spec_seed_sha256": "a" * 64,
        "randomization_key_sha256": dispatch_manifest.randomization_key_sha256,
        "weak_batch_spec_sha256": dispatch_manifest.spec_sha256,
        "dispatch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "weak_batch_id": dispatch_manifest.batch_id,
        "dispatch_cell_count": 4,
        "execution_authorization": "offline_fixture_or_replay_only",
        "live_provider_calls_authorized": False,
    }
    complete_authoring_values = sol_fable.SolFableBatchAuthoringManifest.model_construct(
        **authoring_values,
        authoring_id="lf022_sol_fable_authoring:" + "0" * 64,
    ).model_dump(mode="json", exclude={"authoring_id"})
    authoring = sol_fable.SolFableBatchAuthoringManifest.model_validate(
        {
            **complete_authoring_values,
            "authoring_id": make_id(
                "lf022_sol_fable_authoring",
                sol_fable._authoring_identity_payload(complete_authoring_values),
            ),
        }
    )
    _write_json(batch_root.parent / "authoring_manifest.json", authoring.model_dump(mode="json"))

    portable_repo = root / "repo"
    for relative in (
        "configs/generation/lf022_codex_sol_judge_v1.yaml",
        "configs/generation/lf022_claude_fable_judge_v1.yaml",
        "prompts/judges/lean_pair_blinded_v2.txt",
        "src/leanfaith/generation/weak_supervision.py",
    ):
        destination = portable_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
    registry_path = portable_repo / "configs/generation/portable_registry.json"
    _write_json(registry_path, registry.model_dump(mode="json"))
    policy_text = (REPO_ROOT / DEFAULT_POLICY).read_text(encoding="utf-8")
    policy_text = policy_text.replace(
        "historical_registry_path: configs/generation/lf022_historical_sol_xhigh_registry_v1.json",
        "historical_registry_path: configs/generation/portable_registry.json",
    ).replace(
        "historical_registry_sha256: cb36f06cbe0821196f95e1a432d79209dada5c3a43753a85a3edca937a4ae0b8",
        f"historical_registry_sha256: {registry_sha}",
    )
    policy_path = portable_repo / "configs/generation/policy.yaml"
    policy_path.write_text(policy_text, encoding="utf-8")
    return batch_root, portable_repo, policy_path


@pytest.fixture(scope="module", autouse=True)
def _portable_finalized_batch(
    tmp_path_factory: pytest.TempPathFactory,
):
    global FIXED_BATCH_ROOT, PORTABLE_REPO_ROOT, PORTABLE_POLICY_PATH
    patcher = pytest.MonkeyPatch()
    FIXED_BATCH_ROOT, PORTABLE_REPO_ROOT, PORTABLE_POLICY_PATH = (
        _build_self_contained_finalized_batch(
            tmp_path_factory.mktemp("model-silver-finalized-batch"), patcher
        )
    )
    yield
    patcher.undo()


def _evaluate_inputs() -> dict[str, Any]:
    _require_fixed_batch()
    loaded = load_model_silver_promotion_policy_v1(
        repo_root=PORTABLE_REPO_ROOT, policy_path=PORTABLE_POLICY_PATH
    )
    spec, dispatch_manifest, dispatches, candidates_by_id = _load_prepared_batch(FIXED_BATCH_ROOT)
    sol, fable = promotion._verify_registered_batch_endpoints(spec=spec, loaded=loaded)
    verified_authoring = promotion._verify_authoring_freshness(
        batch_root=FIXED_BATCH_ROOT,
        loaded_policy=loaded,
        dispatch_manifest=dispatch_manifest,
        candidates_by_id=candidates_by_id,
    )
    execution = _load_canonical_model(
        FIXED_BATCH_ROOT / "execution_manifest.json", LF022WeakExecutionManifest
    )
    finalization = _load_canonical_model(
        FIXED_BATCH_ROOT / "final/finalization_manifest.json",
        LF022WeakFinalizationManifest,
    )
    assert isinstance(execution, LF022WeakExecutionManifest)
    assert isinstance(finalization, LF022WeakFinalizationManifest)
    evidence, weak_candidates, replayed = finalize_lf022_weak_batch(batch_root=FIXED_BATCH_ROOT)
    assert replayed == finalization
    call_models = _load_canonical_jsonl(FIXED_BATCH_ROOT / "final/calls.jsonl", LLMCallRecord)
    calls = tuple(item for item in call_models if isinstance(item, LLMCallRecord))
    weak = weak_candidates[0]
    candidate_ids = {
        item.candidate_inventory_record_id for item in dispatches if item.pair_id == weak.pair_id
    }
    assert len(candidate_ids) == 1
    return {
        "pair_id": weak.pair_id,
        "weak_candidate": weak,
        "dispatches": dispatches,
        "calls": calls,
        "evidence": evidence,
        "source_candidate": candidates_by_id[next(iter(candidate_ids))],
        "dispatch_manifest": dispatch_manifest,
        "execution": execution,
        "finalization": finalization,
        "verified_authoring": verified_authoring,
        "loaded_policy": loaded,
        "sol_config_sha256": sol.sha256,
        "fable_config_sha256": fable.sha256,
        "dispatch_manifest_sha256": hash_file(FIXED_BATCH_ROOT / "dispatch_manifest.json"),
        "execution_manifest_sha256": hash_file(FIXED_BATCH_ROOT / "execution_manifest.json"),
        "finalization_manifest_sha256": hash_file(
            FIXED_BATCH_ROOT / "final/finalization_manifest.json"
        ),
    }


def _evaluate(**updates: Any):  # type: ignore[no-untyped-def]
    arguments = _evaluate_inputs()
    arguments.update(updates)
    return promotion._evaluate_pair(**arguments)


def test_fixed_finalized_batch_promotes_as_training_only_model_silver(
    tmp_path: Path,
) -> None:
    _require_fixed_batch()
    output = tmp_path / "promotion"

    result = promote_finalized_lf022_batch_to_model_silver_v1(
        repo_root=PORTABLE_REPO_ROOT,
        batch_root=FIXED_BATCH_ROOT,
        output_root=output,
        policy_path=PORTABLE_POLICY_PATH,
    )

    assert len(result.promotions) == 1
    assert not result.rejections
    record = result.promotions[0]
    assert record.same_claim is False
    assert record.relation.value == "A_stronger"
    assert record.minimum_self_reported_confidence == 0.96
    assert record.freshness_status == "verified_authoring_history_and_completed_ledger_v1"
    assert record.source_authoring_id.startswith("lf022_sol_fable_authoring:")
    assert record.historical_sol_xhigh_registry_id.startswith("lf022_sol_history_registry:")
    assert record.completed_sol_fable_ledger_id.startswith("lf022_sol_fable_exclusion:")
    assert record.source_theorem_lineage_id.startswith("thm:")
    assert record.train_eligibility
    assert not record.eval_eligibility
    assert not record.selection_eligibility
    assert not record.calibration_eligibility
    assert not record.human_gold_eligible
    assert record.human_adjudication_status == "not_performed"
    assert not record.resolved_label_created
    assert not record.gate_6_human_audit_claimed
    assert record.accepted_strong_evidence_ids == ()
    assert record.strong_evidence_conflict_status == "none_in_bound_evidence"
    assert [(cell.judge_slot, cell.orientation) for cell in record.cells] == [
        ("judge_A", "AB"),
        ("judge_A", "BA"),
        ("judge_B", "AB"),
        ("judge_B", "BA"),
    ]
    assert {(cell.a_implies_b, cell.b_implies_a) for cell in record.cells} == {("yes", "yes")}
    assert result.manifest.promotion_count == 1
    assert result.manifest.rejection_count == 0
    assert result.manifest.promotion_record_policy_train_eligible
    assert result.manifest.contains_train_eligible_records
    assert result.manifest.freshness_verified
    assert not result.manifest.eval_eligibility
    assert not result.manifest.selection_eligibility
    assert not result.manifest.calibration_eligibility
    assert not result.manifest.human_gold_eligible
    assert not result.manifest.resolved_label_created
    assert not result.manifest.gate_6_human_audit_claimed
    assert not (output / "rejections.jsonl").read_bytes()
    assert hash_file(output / "promotions.jsonl") == result.manifest.promotions_sha256
    assert hash_file(output / "rejections.jsonl") == result.manifest.rejections_sha256


def test_exact_replay_is_idempotent_and_output_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    _require_fixed_batch()
    output = tmp_path / "promotion"
    first = promote_finalized_lf022_batch_to_model_silver_v1(
        repo_root=PORTABLE_REPO_ROOT,
        batch_root=FIXED_BATCH_ROOT,
        output_root=output,
        policy_path=PORTABLE_POLICY_PATH,
    )
    second = promote_finalized_lf022_batch_to_model_silver_v1(
        repo_root=PORTABLE_REPO_ROOT,
        batch_root=FIXED_BATCH_ROOT,
        output_root=output,
        policy_path=PORTABLE_POLICY_PATH,
    )
    assert first.manifest == second.manifest

    (output / "promotions.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(LF022WeakBatchError, match="immutable silver promotions conflicts"):
        promote_finalized_lf022_batch_to_model_silver_v1(
            repo_root=PORTABLE_REPO_ROOT,
            batch_root=FIXED_BATCH_ROOT,
            output_root=output,
            policy_path=PORTABLE_POLICY_PATH,
        )


def test_missing_v4_authoring_proof_fails_closed_without_live_storage(tmp_path: Path) -> None:
    copied_bundle = tmp_path / "bundle"
    shutil.copytree(FIXED_BATCH_ROOT.parent, copied_bundle)
    copied_batch = copied_bundle / "batch"
    (copied_bundle / "authoring_manifest.json").unlink()
    spec, dispatch, _, candidates = _load_prepared_batch(copied_batch)
    loaded = load_model_silver_promotion_policy_v1(
        repo_root=PORTABLE_REPO_ROOT, policy_path=PORTABLE_POLICY_PATH
    )

    with pytest.raises(
        LF022ModelSilverPromotionError,
        match="invalid Sol/Fable v4 authoring manifest",
    ):
        promotion._verify_authoring_freshness(
            batch_root=copied_batch,
            loaded_policy=loaded,
            dispatch_manifest=dispatch,
            candidates_by_id=candidates,
        )

    assert spec.batch_name == "model-silver-portable-unit-fixture"


def test_source_artifact_tamper_fails_before_any_promotion_output(
    tmp_path: Path,
) -> None:
    _require_fixed_batch()
    copied_bundle = tmp_path / "bundle"
    shutil.copytree(FIXED_BATCH_ROOT.parent, copied_bundle)
    copied = copied_bundle / "batch"
    calls_path = copied / "final/calls.jsonl"
    calls_path.write_bytes(calls_path.read_bytes() + b"\n")
    output = tmp_path / "promotion"

    with pytest.raises(LF022WeakBatchError):
        promote_finalized_lf022_batch_to_model_silver_v1(
            repo_root=PORTABLE_REPO_ROOT,
            batch_root=copied,
            output_root=output,
            policy_path=PORTABLE_POLICY_PATH,
        )

    assert not output.exists()


def test_policy_binds_exact_parser_implementation(tmp_path: Path) -> None:
    source = (REPO_ROOT / DEFAULT_POLICY).read_text(encoding="utf-8")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        source.replace(
            "parser_implementation_sha256: b1b26aa336680e3b4f4fde0035fc536a259693e628c46b48f7d0b5aa2c532714",
            "parser_implementation_sha256: " + "f" * 64,
        ),
        encoding="utf-8",
    )

    with pytest.raises(LF022ModelSilverPromotionError, match="parser implementation hash"):
        load_model_silver_promotion_policy_v1(repo_root=REPO_ROOT, policy_path=policy_path)


def test_registered_endpoint_drift_fails_closed() -> None:
    arguments = _evaluate_inputs()
    spec, _, _, _ = _load_prepared_batch(FIXED_BATCH_ROOT)
    drifted_a = spec.judge_a.model_copy(
        update={"revision": "provider-deployment-snapshot:" + "0" * 64}
    )
    drifted_spec = spec.model_copy(update={"judge_a": drifted_a})

    with pytest.raises(LF022ModelSilverPromotionError, match="Sol endpoint differs"):
        promotion._verify_registered_batch_endpoints(
            spec=drifted_spec, loaded=arguments["loaded_policy"]
        )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("review", "source_judgment_requested_review"),
        ("low_confidence", "judgment_confidence_below_policy"),
        ("semantic_disagreement", "canonical_semantic_disagreement"),
        ("call_private", "call_private_source_content"),
        ("call_denylist", "call_denylist_not_clean"),
        ("call_revision", "registered_model_effort_schema_mismatch"),
        ("call_prompt", "registered_parser_prompt_binding_mismatch"),
        ("call_not_supervision", "call_not_supervision_eligible"),
        ("source_private", "source_private_content"),
        ("missing_cell", "exact_four_cells_missing_or_duplicate"),
        ("proposer_overlap", "proposer_judge_family_overlap"),
        ("heldout_overlap", "heldout_judge_family_overlap"),
    ],
)
def test_pair_level_policy_failures_emit_explicit_rejections(
    mutation: str, expected_reason: str
) -> None:
    arguments = _evaluate_inputs()
    if mutation in {"review", "low_confidence", "semantic_disagreement"}:
        evidence = list(arguments["evidence"])
        original = evidence[0]
        assert isinstance(original.value, JudgmentValue)
        updates: dict[str, object]
        if mutation == "review":
            updates = {"needs_expert_review": True}
        elif mutation == "low_confidence":
            updates = {"confidence": 0.1}
        else:
            updates = {"relation": "B_stronger"}
        evidence[0] = original.model_copy(
            update={"value": original.value.model_copy(update=updates)}
        )
        arguments["evidence"] = tuple(evidence)
    elif mutation in {
        "call_private",
        "call_denylist",
        "call_revision",
        "call_prompt",
        "call_not_supervision",
    }:
        calls = list(arguments["calls"])
        if mutation == "call_private":
            calls[0] = calls[0].model_copy(update={"private_source_content": True})
        elif mutation == "call_denylist":
            calls[0] = calls[0].model_copy(
                update={"denylist_checked": True, "denylist_hits": ("hit",)}
            )
        elif mutation == "call_revision":
            calls[0] = calls[0].model_copy(update={"model_revision": "drifted"})
        elif mutation == "call_prompt":
            calls[0] = calls[0].model_copy(update={"prompt_template_version": "v1"})
        else:
            calls[0] = calls[0].model_copy(update={"supervision_eligible": False})
        arguments["calls"] = tuple(calls)
    elif mutation == "missing_cell":
        arguments["dispatches"] = arguments["dispatches"][:-1]
    elif mutation == "source_private":
        candidate = arguments["source_candidate"]
        private_pair = candidate.pair.model_copy(
            update={
                "source_is_public": False,
                "private_source_content": True,
                "external_transmission_allowed": False,
            }
        )
        arguments["source_candidate"] = candidate.model_copy(update={"pair": private_pair})
    else:
        manifest = arguments["dispatch_manifest"]
        if mutation == "proposer_overlap":
            manifest = manifest.model_copy(update={"proposer_family_id": "openai_codex_sol"})
        else:
            manifest = manifest.model_copy(update={"primary_eval_family_id": "anthropic_fable"})
        arguments["dispatch_manifest"] = manifest

    result = promotion._evaluate_pair(**arguments)

    assert isinstance(result, ModelAdjudicatedSilverRejectionV1)
    assert expected_reason in result.reasons
    assert result.rejection_id.startswith("model_silver_rejection:")


def test_non_llm_bound_evidence_blocks_promotion_as_unresolved_strong_evidence() -> None:
    arguments = _evaluate_inputs()
    pair_id = arguments["pair_id"]
    strong = EvidenceRecord(
        evidence_id="ev:" + "0" * 64,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=pair_id,
        kind=EvidenceKind.DEFEQ,
        status=EvidenceExecutionStatus.SUCCESS,
        value=DefeqValue(outcome="not_equal"),
        method_version="offline-strong-evidence-fixture",
        created_at=datetime.datetime(2026, 8, 12, tzinfo=datetime.UTC),
    )
    arguments["evidence"] = (*arguments["evidence"], strong)

    result = promotion._evaluate_pair(**arguments)

    assert isinstance(result, ModelAdjudicatedSilverRejectionV1)
    assert "bound_non_llm_evidence_forbidden" in result.reasons
    assert "strong_evidence_conflict_not_resolved" in result.reasons


def test_schema_rejects_noncanonical_four_cell_implication_agreement() -> None:
    result = _evaluate()
    assert isinstance(result, ModelAdjudicatedSilverPromotionRecordV1)
    cells = list(result.cells)
    cells[0] = cells[0].model_copy(update={"a_implies_b": "no"})
    payload = result.model_dump(mode="json")
    payload["cells"] = [cell.model_dump(mode="json") for cell in cells]

    with pytest.raises(ValueError, match="agree exactly"):
        ModelAdjudicatedSilverPromotionRecordV1.model_validate(payload)


def test_zero_promotion_manifest_cannot_claim_train_eligible_records(tmp_path: Path) -> None:
    _require_fixed_batch()
    result = promote_finalized_lf022_batch_to_model_silver_v1(
        repo_root=PORTABLE_REPO_ROOT,
        batch_root=FIXED_BATCH_ROOT,
        output_root=tmp_path / "promotion",
        policy_path=PORTABLE_POLICY_PATH,
    )
    values = result.manifest.model_dump(mode="json", exclude={"manifest_id"})
    values.update(
        {
            "promotion_count": 0,
            "rejection_count": values["input_pair_count"],
            "rejection_reason_counts": {"fixture_rejection": values["input_pair_count"]},
            "contains_train_eligible_records": False,
        }
    )
    valid = ModelAdjudicatedSilverPromotionManifestV1.model_validate(
        {**values, "manifest_id": make_id("model_silver_manifest", values)}
    )
    assert not valid.contains_train_eligible_records

    invalid_values = {**values, "contains_train_eligible_records": True}
    with pytest.raises(ValueError, match="presence must match promotion count"):
        ModelAdjudicatedSilverPromotionManifestV1.model_validate(
            {
                **invalid_values,
                "manifest_id": make_id("model_silver_manifest", invalid_values),
            }
        )


def test_existing_output_with_unexpected_artifact_is_refused(tmp_path: Path) -> None:
    _require_fixed_batch()
    output = tmp_path / "promotion"
    output.mkdir()
    (output / "unexpected.txt").write_text("not part of the manifest", encoding="utf-8")

    with pytest.raises(LF022ModelSilverPromotionError, match="unexpected artifacts"):
        promote_finalized_lf022_batch_to_model_silver_v1(
            repo_root=PORTABLE_REPO_ROOT,
            batch_root=FIXED_BATCH_ROOT,
            output_root=output,
            policy_path=PORTABLE_POLICY_PATH,
        )

    assert not (output / "manifest.json").exists()


def test_manifest_is_canonical_and_does_not_claim_human_gate_credit(tmp_path: Path) -> None:
    _require_fixed_batch()
    result = promote_finalized_lf022_batch_to_model_silver_v1(
        repo_root=PORTABLE_REPO_ROOT,
        batch_root=FIXED_BATCH_ROOT,
        output_root=tmp_path / "promotion",
        policy_path=PORTABLE_POLICY_PATH,
    )
    stored = (result.output_root / "manifest.json").read_bytes()
    assert stored == canonical_json_bytes(result.manifest.model_dump(mode="json")) + b"\n"
    assert b"ResolvedLabel" not in stored
    assert b'"human_gold_eligible":true' not in stored
    assert b'"gate_6_human_audit_claimed":true' not in stored
