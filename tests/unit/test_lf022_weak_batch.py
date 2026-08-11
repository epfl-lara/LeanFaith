"""Raw-first, resumable LF-022 two-family weak-supervision batches."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022FamilyPin,
    LF022ProductionFamilyMatrix,
    canonical_model_family,
    make_lf022_production_family_matrix,
)
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
    PriorCodexDiagnostic,
    _judge_visible_payload_hash,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchError,
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    execute_or_resume_lf022_weak_batch,
    finalize_lf022_weak_batch,
    prepare_lf022_weak_batch,
    replay_lf022_weak_batch,
)
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    GenerationProvider,
    ProviderIdentity,
    ProviderRawResponse,
    ProviderRequest,
    ProviderResult,
    persist_provider_raw_response,
)
from leanfaith.generation.weak_supervision import PublicLeanJudgePair
from leanfaith.schemas.ids import make_id

NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)
KEY = b"lf022-weak-batch-test-random-key!!"
CATALOG_SHA = "d" * 64
REVISION = f"provider-deployment-snapshot:{CATALOG_SHA}"
QWEN_MODEL = "Qwen/Qwen3.5-397B-A17B"
DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Pro"


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hash_file(path)


def _json(path: Path, value: object) -> str:
    return _write(path, canonical_json_bytes(value) + b"\n")


def _pair() -> PublicLeanJudgePair:
    return PublicLeanJudgePair(
        pair_id="pair:" + "1" * 64,
        canonical_lean_a="theorem source (n : Nat) : n = n",
        canonical_lean_b="theorem candidate (n : Nat) : n ≤ n",
        optional_natural_language=None,
        source_record_ids=("thm:" + "2" * 64, "var:" + "3" * 64),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
    )


def _candidate() -> LF022SupervisionCandidateRecord:
    pair = _pair()
    values: dict[str, object] = {
        "schema_version": 2,
        "collection_id": "fixture",
        "pair_id": pair.pair_id,
        "variant_id": "var:" + "3" * 64,
        "lean_check_id": "lf022_lean_check:" + "4" * 64,
        "proposer_family_id": "moonshot_kimi_k2",
        "proposer_model": "moonshotai/Kimi-K2.7-Code",
        "pair": pair.model_dump(mode="json"),
        "pair_admission_sha256": pair.admission_sha256,
        "judge_visible_payload_sha256": _judge_visible_payload_hash(pair),
        "dispatch_status": "ready_for_two_family_judging",
        "canonical_dispatch_pair_id": pair.pair_id,
        "canonical_dispatch_audit_item_id": "lf022_codex_audit_item:" + "5" * 64,
        "required_judgment_cells": (
            "judge_A:AB",
            "judge_A:BA",
            "judge_B:AB",
            "judge_B:BA",
        ),
        "prior_codex_diagnostic": PriorCodexDiagnostic(
            audit_item_id="lf022_codex_audit_item:" + "5" * 64,
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            same_claim_answer="not_same_claim",
            relation="A_stronger",
            confidence=0.8,
            needs_expert_review=False,
            parsed_response_sha256="6" * 64,
        ).model_dump(mode="json"),
        "promotion_blockers": (
            "human_pilot_not_bound",
            "promotion_audit_missing",
            "silver_not_promoted",
            "swapped_order_judgments_missing",
            "two_family_judgments_missing",
        ),
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022SupervisionCandidateRecord.model_validate(
        {
            **values,
            "candidate_inventory_record_id": make_id("lf022_supervision_candidate", values),
        }
    )


def _candidate_manifest(record_bytes: bytes) -> LF022SupervisionCandidateManifest:
    record_hash = sha256_hex(record_bytes)
    values: dict[str, object] = {
        "schema_version": 2,
        "method_version": "lf022_supervision_candidate_inventory_v2",
        "collection_id": "fixture",
        "spec_sha256": "7" * 64,
        "checks_sha256": "8" * 64,
        "codex_audit_manifest_sha256": "9" * 64,
        "logical_input_binding_sha256": "a" * 64,
        "codex_response_artifact_set_sha256": "b" * 64,
        "proposer_family_id": "moonshot_kimi_k2",
        "proposer_model": "moonshotai/Kimi-K2.7-Code",
        "judge_a_family_id": "qwen3",
        "judge_b_family_id": "deepseek_v4",
        "primary_eval_judge_family_id": "openai_codex",
        "records_artifact": "candidates.jsonl",
        "records_sha256": record_hash,
        "public_sample_artifact": "public_sample.jsonl",
        "public_sample_sha256": record_hash,
        "public_sample_count": 1,
        "summary_artifact": "summary.md",
        "summary_sha256": "c" * 64,
        "record_count": 1,
        "unique_judge_visible_payload_count": 1,
        "exact_duplicate_record_count": 0,
        "dispatch_eligible_count": 1,
        "required_future_judge_call_count": 4,
        "codex_same_claim_counts": {"not_same_claim": 1},
        "dispatch_status_counts": {"ready_for_two_family_judging": 1},
        "codex_is_diagnostic_only": True,
        "two_family_judgments_completed": False,
        "human_pilot_bound": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    id_payload = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "records_artifact",
            "public_sample_artifact",
            "summary_artifact",
            "spec_sha256",
        }
    }
    return LF022SupervisionCandidateManifest.model_validate(
        {**values, "inventory_id": make_id("lf022_supervision_inventory", id_payload)}
    )


def _foundation(tmp_path: Path) -> tuple[Path, Path, str]:
    inputs = tmp_path / "inputs"
    candidate = _candidate()
    record_bytes = canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"
    records_path = inputs / "candidates.jsonl"
    records_sha = _write(records_path, record_bytes)
    manifest = _candidate_manifest(record_bytes)
    manifest_path = inputs / "manifest.json"
    manifest_sha = _json(manifest_path, manifest.model_dump(mode="json"))

    weak_config_path = Path("configs/judges/weak_supervision.yaml").resolve()
    weak_config_sha = hash_file(weak_config_path)

    def pin(family_id: str, model_id: str) -> LF022FamilyPin:
        return LF022FamilyPin(
            family_id=family_id,
            model_id=model_id,
            canonical_family=canonical_model_family(model_id),
            pin_kind="provider_deployment_snapshot",
            provider_id="fixture",
            provider_deployment_id=model_id,
            provider_catalog_artifact=LF022ArtifactBinding(
                path="configs/catalog.json", sha256=CATALOG_SHA
            ),
            underlying_checkpoint_revision_status="provider_not_disclosed",
        )

    matrix = make_lf022_production_family_matrix(
        family_registry=(
            pin("moonshot_kimi_k2", "moonshotai/Kimi-K2.7-Code"),
            pin("qwen3", QWEN_MODEL),
            pin("glm5", "zai-org/GLM-5.2"),
            pin("deepseek_v4", DEEPSEEK_MODEL),
            pin("openai_codex", "openai/gpt-5.6-terra"),
        ),
        proposer_family_ids=("moonshot_kimi_k2", "qwen3", "deepseek_v4"),
        judge_family_ids=("qwen3", "deepseek_v4", "glm5"),
        sci_validator_family_ids=("moonshot_kimi_k2", "qwen3", "glm5", "deepseek_v4"),
        heldout_eval_family_id="openai_codex",
    )
    matrix_path = inputs / "family_matrix.json"
    matrix_sha = _json(matrix_path, matrix.model_dump(mode="json"))
    spec = LF022WeakBatchSpec(
        batch_name="fixture",
        candidate_manifest=BoundArtifact(path=str(manifest_path), sha256=manifest_sha),
        candidate_records=BoundArtifact(path=str(records_path), sha256=records_sha),
        weak_supervision_config=BoundArtifact(path=str(weak_config_path), sha256=weak_config_sha),
        production_family_matrix=BoundArtifact(path=str(matrix_path), sha256=matrix_sha),
        randomization_key_sha256=sha256_hex(KEY),
        judge_a=JudgeEndpointPin(
            provider_slot="judge_A",
            provider="fixture",
            model=QWEN_MODEL,
            family_id="qwen3",
            revision=REVISION,
            decoding={"temperature": 0.0},
        ),
        judge_b=JudgeEndpointPin(
            provider_slot="judge_B",
            provider="fixture",
            model=DEEPSEEK_MODEL,
            family_id="deepseek_v4",
            revision=REVISION,
            decoding={"temperature": 0.0},
        ),
        primary_eval_family_id="openai_codex",
    )
    spec_path = inputs / "batch_spec.json"
    spec_sha = _json(spec_path, spec.model_dump(mode="json"))
    batch_root = tmp_path / "batch"
    return spec_path, batch_root, spec_sha


def _replace_matrix_roles(
    spec_path: Path,
    *,
    proposer_family_ids: tuple[str, ...],
    judge_family_ids: tuple[str, ...],
) -> str:
    spec = LF022WeakBatchSpec.model_validate_json(spec_path.read_bytes())
    matrix_path = Path(spec.production_family_matrix.path)
    matrix = LF022ProductionFamilyMatrix.model_validate_json(matrix_path.read_bytes())
    replaced = make_lf022_production_family_matrix(
        family_registry=matrix.family_registry,
        proposer_family_ids=proposer_family_ids,
        judge_family_ids=judge_family_ids,
        sci_validator_family_ids=matrix.sci_validator_family_ids,
        heldout_eval_family_id=matrix.heldout_eval_family_id,
    )
    matrix_sha = _json(matrix_path, replaced.model_dump(mode="json"))
    altered = spec.model_copy(
        update={"production_family_matrix": BoundArtifact(path=str(matrix_path), sha256=matrix_sha)}
    )
    return _json(spec_path, altered.model_dump(mode="json"))


def _response(*, orientation: str) -> str:
    relation = "A_stronger" if orientation == "AB" else "B_stronger"
    a_to_b = "yes" if orientation == "AB" else "no"
    b_to_a = "no" if orientation == "AB" else "yes"
    return json.dumps(
        {
            "same_claim_answer": "not_same_claim",
            "relation": relation,
            "A_implies_B": a_to_b,
            "B_implies_A": b_to_a,
            "error_types": ["E01"],
            "confidence": 0.9,
            "rationale": "The candidate weakens equality to an inequality.",
            "needs_expert_review": False,
        }
    )


def _prepare(tmp_path: Path):
    spec_path, batch_root, spec_sha = _foundation(tmp_path)
    records, manifest = prepare_lf022_weak_batch(
        repo_root=Path.cwd(),
        spec_path=spec_path,
        expected_spec_sha256=spec_sha,
        randomization_key=KEY,
        output_dir=batch_root,
    )
    return spec_path, batch_root, spec_sha, records, manifest


def _providers(batch_root: Path, records) -> tuple[dict[str, GenerationProvider], dict[str, Path]]:
    by_slot: dict[str, dict[str, str]] = {"judge_A": {}, "judge_B": {}}
    for record in records:
        by_slot[record.judge_slot][record.provider_request_hash] = _response(
            orientation=record.orientation
        )
    identities = {
        "judge_A": ProviderIdentity(
            provider="fixture",
            model=QWEN_MODEL,
            revision=REVISION,
            transport="fixture",
        ),
        "judge_B": ProviderIdentity(
            provider="fixture",
            model=DEEPSEEK_MODEL,
            revision=REVISION,
            transport="fixture",
        ),
    }
    roots = {
        "judge_A": batch_root / "raw/judge_A",
        "judge_B": batch_root / "raw/judge_B",
    }
    providers: dict[str, GenerationProvider] = {
        slot: DeterministicFixtureProvider(
            identity=identities[slot],
            raw_response_root=roots[slot],
            responses=by_slot[slot],
        )
        for slot in ("judge_A", "judge_B")
    }
    return providers, roots


def test_prepare_execute_finalize_stays_non_trainable(tmp_path: Path) -> None:
    _, batch_root, _, records, manifest = _prepare(tmp_path)
    assert len(records) == 4
    assert manifest.dispatch_pair_count == 1
    assert manifest.live_provider_calls_authorized is False
    assert {record.orientation for record in records} == {"AB", "BA"}
    assert {record.judge_slot for record in records} == {"judge_A", "judge_B"}

    providers, roots = _providers(batch_root, records)
    terminals, execution = execute_or_resume_lf022_weak_batch(
        batch_root=batch_root,
        providers=providers,  # type: ignore[arg-type]
        raw_response_roots=roots,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    evidence, candidates, finalization = finalize_lf022_weak_batch(batch_root=batch_root)
    assert len(terminals) == 4
    assert execution.parse_status_counts == {"parsed": 4}
    assert len(evidence) == 4
    assert len(candidates) == 1
    assert candidates[0].status == "candidate_consensus"
    assert candidates[0].consensus_value is not None
    assert candidates[0].consensus_value.relation == "A_stronger"
    assert candidates[0].semantic_label_created is False
    assert candidates[0].silver_promoted is False
    assert candidates[0].train_eligible is False
    assert finalization.training_eligible is False


def test_malformed_family_outputs_create_incomplete_candidate(tmp_path: Path) -> None:
    _, batch_root, _, records, _ = _prepare(tmp_path)
    providers, roots = _providers(batch_root, records)
    qwen = providers["judge_A"]
    assert isinstance(qwen, DeterministicFixtureProvider)
    for record in records:
        if record.judge_slot == "judge_A":
            qwen.responses[record.provider_request_hash] = "not-json"
    execute_or_resume_lf022_weak_batch(
        batch_root=batch_root,
        providers=providers,  # type: ignore[arg-type]
        raw_response_roots=roots,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    evidence, candidates, finalization = finalize_lf022_weak_batch(batch_root=batch_root)
    assert len(evidence) == 2
    assert candidates[0].status == "incomplete"
    assert candidates[0].consensus_value is None
    assert finalization.parse_status_counts == {"parse_failed": 2, "parsed": 2}
    assert len(tuple((batch_root / "raw/judge_A").rglob("*.json"))) == 2


class _CrashAfterRawOnce(DeterministicFixtureProvider):
    def __init__(self, provider: DeterministicFixtureProvider) -> None:
        super().__init__(
            identity=provider.identity,
            raw_response_root=provider.raw_response_root,
            responses=provider.responses,
        )
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            response = ProviderRawResponse.success(request, self.responses[request.request_hash])
            persist_provider_raw_response(self.raw_response_root, response)
            raise RuntimeError("fixture crash after raw persistence")
        return super().generate(request)


def test_resume_uses_raw_response_after_post_persistence_crash(tmp_path: Path) -> None:
    _, batch_root, _, records, _ = _prepare(tmp_path)
    providers, roots = _providers(batch_root, records)
    base = providers[records[0].judge_slot]
    assert isinstance(base, DeterministicFixtureProvider)
    crashing = _CrashAfterRawOnce(base)
    providers[records[0].judge_slot] = crashing
    with pytest.raises(RuntimeError, match="after raw persistence"):
        execute_or_resume_lf022_weak_batch(
            batch_root=batch_root,
            providers=providers,  # type: ignore[arg-type]
            raw_response_roots=roots,  # type: ignore[arg-type]
            now=lambda: NOW,
        )
    terminals, _ = execute_or_resume_lf022_weak_batch(
        batch_root=batch_root,
        providers=providers,  # type: ignore[arg-type]
        raw_response_roots=roots,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    assert len(terminals) == 4
    # The first request was replayed from raw; the wrapped provider was used
    # only for the other request belonging to its slot.
    assert crashing.calls == 2


def test_request_tampering_and_family_mismatch_fail_closed(tmp_path: Path) -> None:
    _, batch_root, _, records, _ = _prepare(tmp_path)
    providers, roots = _providers(batch_root, records)
    wrong = DeterministicFixtureProvider(
        identity=ProviderIdentity(
            provider="fixture",
            model="fixture/wrong",
            revision=REVISION,
            transport="fixture",
        ),
        raw_response_root=roots["judge_A"],
        responses={},
    )
    providers["judge_A"] = wrong
    with pytest.raises(LF022WeakBatchError, match="provider differs"):
        execute_or_resume_lf022_weak_batch(
            batch_root=batch_root,
            providers=providers,  # type: ignore[arg-type]
            raw_response_roots=roots,  # type: ignore[arg-type]
            now=lambda: NOW,
        )

    request_path = batch_root / records[0].request_artifact
    request_path.write_bytes(request_path.read_bytes() + b" ")
    with pytest.raises(LF022WeakBatchError, match="request artifact hash"):
        execute_or_resume_lf022_weak_batch(
            batch_root=batch_root,
            providers=providers,  # type: ignore[arg-type]
            raw_response_roots=roots,  # type: ignore[arg-type]
            now=lambda: NOW,
        )


def test_canonical_family_aliases_are_rejected_by_matrix_model(tmp_path: Path) -> None:
    spec_path, batch_root, _ = _foundation(tmp_path)
    spec = LF022WeakBatchSpec.model_validate_json(spec_path.read_bytes())
    matrix_path = Path(spec.production_family_matrix.path)
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    deepseek = next(
        item for item in matrix["family_registry"] if item["family_id"] == "deepseek_v4"
    )
    alias_model = "Qwen/Qwen3.6-35B-A3B"
    deepseek["model_id"] = alias_model
    deepseek["canonical_family"] = canonical_model_family(alias_model)
    deepseek["provider_deployment_id"] = alias_model
    matrix_sha = _json(matrix_path, matrix)
    altered = spec.model_copy(
        update={
            "production_family_matrix": BoundArtifact(path=str(matrix_path), sha256=matrix_sha),
            "judge_b": spec.judge_b.model_copy(update={"model": alias_model}),
        }
    )
    spec_sha = _json(spec_path, altered.model_dump(mode="json"))
    with pytest.raises(LF022WeakBatchError, match="unique canonical model families"):
        prepare_lf022_weak_batch(
            repo_root=Path.cwd(),
            spec_path=spec_path,
            expected_spec_sha256=spec_sha,
            randomization_key=KEY,
            output_dir=batch_root,
        )


def test_proposer_only_family_cannot_fill_judge_endpoint(tmp_path: Path) -> None:
    spec_path, batch_root, _ = _foundation(tmp_path)
    spec_sha = _replace_matrix_roles(
        spec_path,
        proposer_family_ids=("moonshot_kimi_k2", "qwen3", "deepseek_v4"),
        judge_family_ids=("moonshot_kimi_k2", "deepseek_v4", "glm5"),
    )
    with pytest.raises(LF022WeakBatchError, match="not admitted for the judge role: qwen3"):
        prepare_lf022_weak_batch(
            repo_root=Path.cwd(),
            spec_path=spec_path,
            expected_spec_sha256=spec_sha,
            randomization_key=KEY,
            output_dir=batch_root,
        )


def test_judge_only_family_cannot_supply_candidate_proposer(tmp_path: Path) -> None:
    spec_path, batch_root, _ = _foundation(tmp_path)
    spec_sha = _replace_matrix_roles(
        spec_path,
        proposer_family_ids=("qwen3", "deepseek_v4", "glm5"),
        judge_family_ids=("moonshot_kimi_k2", "qwen3", "deepseek_v4", "glm5"),
    )
    with pytest.raises(
        LF022WeakBatchError,
        match="candidate proposer is not admitted for the proposer role: moonshot_kimi_k2",
    ):
        prepare_lf022_weak_batch(
            repo_root=Path.cwd(),
            spec_path=spec_path,
            expected_spec_sha256=spec_sha,
            randomization_key=KEY,
            output_dir=batch_root,
        )


def test_replay_rejects_reconstructed_batch_with_judge_only_proposer(tmp_path: Path) -> None:
    _, batch_root, _, _, _ = _prepare(tmp_path)
    matrix_path = batch_root / "inputs/production_family_matrix.json"
    matrix = LF022ProductionFamilyMatrix.model_validate_json(matrix_path.read_bytes())
    reconstructed_matrix = make_lf022_production_family_matrix(
        family_registry=matrix.family_registry,
        proposer_family_ids=("qwen3", "deepseek_v4", "glm5"),
        judge_family_ids=matrix.judge_family_ids,
        sci_validator_family_ids=matrix.sci_validator_family_ids,
        heldout_eval_family_id=matrix.heldout_eval_family_id,
    )
    reconstructed_matrix_sha = _json(matrix_path, reconstructed_matrix.model_dump(mode="json"))

    manifest_path = batch_root / "dispatch_manifest.json"
    manifest = LF022WeakDispatchManifest.model_validate_json(manifest_path.read_bytes())
    manifest_values = manifest.model_dump(mode="json")
    manifest_values["production_family_matrix_sha256"] = reconstructed_matrix_sha
    manifest_values["batch_id"] = make_id(
        "lf022_weak_batch",
        {
            key: value
            for key, value in manifest_values.items()
            if key not in {"batch_id", "spec_sha256"}
        },
    )
    reconstructed_manifest = LF022WeakDispatchManifest.model_validate(manifest_values)
    _json(manifest_path, reconstructed_manifest.model_dump(mode="json"))

    with pytest.raises(
        LF022WeakBatchError,
        match="self-contained candidate proposer is not admitted for the proposer role",
    ):
        replay_lf022_weak_batch(batch_root=batch_root)


class _ForbiddenLocalProvider:
    def __init__(self, identity: ProviderIdentity) -> None:
        self.identity = identity
        self.called = False

    def generate(self, request: ProviderRequest) -> ProviderResult:
        self.called = True
        raise AssertionError(f"local provider was called for {request.request_hash}")


def test_offline_batch_rejects_local_provider_before_generate(tmp_path: Path) -> None:
    _, batch_root, _, records, _ = _prepare(tmp_path)
    providers, roots = _providers(batch_root, records)
    local = _ForbiddenLocalProvider(
        ProviderIdentity(
            provider="fixture",
            model=QWEN_MODEL,
            revision=REVISION,
            transport="local",
        )
    )
    providers["judge_A"] = local
    with pytest.raises(LF022WeakBatchError, match="only fixture or replay"):
        execute_or_resume_lf022_weak_batch(
            batch_root=batch_root,
            providers=providers,  # type: ignore[arg-type]
            raw_response_roots=roots,  # type: ignore[arg-type]
            now=lambda: NOW,
        )
    assert local.called is False


def test_cli_prepares_and_offline_replay_finalizes_existing_terminals(tmp_path: Path) -> None:
    spec_path, batch_root, spec_sha = _foundation(tmp_path)
    key_path = tmp_path / "randomization.key"
    key_path.write_bytes(KEY)
    runner = CliRunner()
    prepared = runner.invoke(
        app,
        [
            "prepare-lf022-weak-batch",
            "--spec",
            str(spec_path),
            "--spec-sha256",
            spec_sha,
            "--randomization-key-file",
            str(key_path),
            "--output-dir",
            str(batch_root),
            "--root",
            str(Path.cwd()),
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    from leanfaith.generation.lf022_weak_batch import _load_prepared_batch

    _, _, records, _ = _load_prepared_batch(batch_root)
    providers, roots = _providers(batch_root, records)
    execute_or_resume_lf022_weak_batch(
        batch_root=batch_root,
        providers=providers,  # type: ignore[arg-type]
        raw_response_roots=roots,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    replayed = runner.invoke(
        app,
        [
            "replay-finalize-lf022-weak-batch",
            "--batch-root",
            str(batch_root),
            "--root",
            str(Path.cwd()),
        ],
    )
    assert replayed.exit_code == 0, replayed.output
    assert "provider_calls=0" in replayed.output
    assert "training_eligible=false" in replayed.output
