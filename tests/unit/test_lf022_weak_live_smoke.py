"""One-pair, raw-first live weak-judge smoke with fake RCP transport only."""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.lf022_execution import LF022RCPRetryPolicy
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022FamilyPin,
    LF022ProviderDeployment,
    make_lf022_production_family_matrix,
    make_lf022_provider_catalog_snapshot,
)
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchSpec,
    prepare_lf022_weak_batch,
)
from leanfaith.generation.lf022_weak_live_smoke import (
    LF022WeakJudgeDecodingContract,
    LF022WeakJudgeRouteClaim,
    LF022WeakLiveSmokeConfig,
    LF022WeakLiveSmokeError,
    LF022WeakRuntimeCredentials,
    execute_lf022_weak_live_smoke,
    freeze_lf022_weak_live_smoke_inputs,
    prepare_lf022_weak_live_smoke,
    replay_lf022_weak_live_smoke,
)
from leanfaith.generation.rcp_provider import RCPTransportUnknownError, RCPWireResponse
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.manifest import CodeState
from tests.unit.test_lf022_weak_batch import KEY, _foundation, _json, _write

NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)
TREE = "e" * 64
COMMIT = "f" * 40
QWEN_MODEL = "Qwen/Qwen3.5-397B-A17B"
KIMI_MODEL = "moonshotai/Kimi-K2.7-Code"
DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Pro"


def _decoding(family: str) -> LF022WeakJudgeDecodingContract:
    common = {
        "top_k": None,
        "min_p": None,
        "presence_penalty": None,
        "repetition_penalty": None,
        "max_tokens": 8192,
        "seed": 42,
        "stream": False,
        "reasoning_effort": "high",
        "chat_template_enable_thinking": True,
    }
    if family == "moonshot_kimi_k2":
        return LF022WeakJudgeDecodingContract(
            contract_id="kimi_k2_7_weak_judge_smoke_v1",
            temperature=1.0,
            top_p=0.95,
            **common,
        )
    return LF022WeakJudgeDecodingContract(
        contract_id="deepseek_v4_weak_judge_smoke_v1",
        temperature=0.0,
        top_p=1.0,
        **common,
    )


def _inventory_id(values: dict[str, object]) -> str:
    payload = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "inventory_id",
            "records_artifact",
            "public_sample_artifact",
            "summary_artifact",
            "spec_sha256",
        }
    }
    return make_id("lf022_supervision_inventory", payload)


def _claim(
    *,
    family: str,
    model: str,
    decoding: LF022WeakJudgeDecodingContract,
    production_catalog: Path,
    raw_catalog: Path,
    prompt_sha: str,
) -> LF022WeakJudgeRouteClaim:
    production_sha = hash_file(production_catalog)
    raw_sha = hash_file(raw_catalog)
    values: dict[str, object] = {
        "schema_version": 1,
        "role": "judge",
        "provider": "epfl_rcp",
        "model_id": model,
        "family_id": family,
        "production_matrix_revision": f"provider-deployment-snapshot:{production_sha}",
        "production_matrix_catalog_artifact": {
            "path": production_catalog.name,
            "sha256": production_sha,
        },
        "rcp_catalog_revision": f"rcp-catalog-sha256:{raw_sha}",
        "raw_rcp_catalog_artifact": {
            "path": raw_catalog.name,
            "sha256": raw_sha,
        },
        "decoding": decoding.model_dump(mode="json"),
        "judge_prompt_sha256": prompt_sha,
        "qualification_scope": "one_pair_four_cell_weak_judge_smoke",
        "smoke_route_admitted": True,
        "scale_judge_qualified": False,
        "private_source_content_allowed": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022WeakJudgeRouteClaim.model_validate(
        {
            **values,
            "claim_id": make_id("lf022_weak_judge_claim", values),
        }
    )


def _prepare_live_foundation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spec_path, batch_root, _ = _foundation(tmp_path, candidate_schema_version=3)
    spec = LF022WeakBatchSpec.model_validate_json(spec_path.read_bytes())

    candidate_path = Path(spec.candidate_records.path)
    candidate = LF022SupervisionCandidateRecord.model_validate_json(
        candidate_path.read_bytes().splitlines()[0]
    )
    candidate_values = candidate.model_dump(mode="json", exclude={"candidate_inventory_record_id"})
    candidate_values.update(
        {
            "proposer_family_id": "qwen3",
            "proposer_model": QWEN_MODEL,
        }
    )
    candidate = LF022SupervisionCandidateRecord.model_validate(
        {
            **candidate_values,
            "candidate_inventory_record_id": make_id(
                "lf022_supervision_candidate", candidate_values
            ),
        }
    )
    record_bytes = canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"
    candidate_sha = _write(candidate_path, record_bytes)

    manifest_path = Path(spec.candidate_manifest.path)
    manifest = LF022SupervisionCandidateManifest.model_validate_json(manifest_path.read_bytes())
    manifest_values = manifest.model_dump(mode="json")
    manifest_values.update(
        {
            "proposer_family_id": "qwen3",
            "proposer_model": QWEN_MODEL,
            "judge_a_family_id": "moonshot_kimi_k2",
            "judge_b_family_id": "deepseek_v4",
            "records_sha256": candidate_sha,
            "public_sample_sha256": candidate_sha,
        }
    )
    manifest_values["inventory_id"] = _inventory_id(manifest_values)
    manifest = LF022SupervisionCandidateManifest.model_validate(manifest_values)
    manifest_sha = _json(manifest_path, manifest.model_dump(mode="json"))

    production_catalog = tmp_path / "production_catalog.json"
    catalog = make_lf022_provider_catalog_snapshot(
        provider_id="epfl_rcp",
        deployments=tuple(
            LF022ProviderDeployment(model_id=model, deployment_id=model)
            for model in sorted((KIMI_MODEL, QWEN_MODEL, DEEPSEEK_MODEL))
        ),
    )
    production_sha = _json(production_catalog, catalog.model_dump(mode="json"))
    raw_catalog = tmp_path / "raw_models.json"
    _json(
        raw_catalog,
        {"data": [{"id": model} for model in (KIMI_MODEL, QWEN_MODEL, DEEPSEEK_MODEL)]},
    )

    def pin(family: str, model: str, provider: str = "epfl_rcp") -> LF022FamilyPin:
        return LF022FamilyPin(
            family_id=family,
            model_id=model,
            canonical_family=(
                "moonshotai/kimi-k2"
                if family == "moonshot_kimi_k2"
                else "qwen/qwen3"
                if family == "qwen3"
                else "deepseek-ai/deepseek-v4-pro"
                if family == "deepseek_v4"
                else "zai-org/glm-5.2"
                if family == "glm5"
                else "openai/gpt-5.6-terra"
            ),
            pin_kind="provider_deployment_snapshot",
            provider_id=provider,
            provider_deployment_id=model,
            provider_catalog_artifact=LF022ArtifactBinding(
                path="configs/test-production-catalog.json",
                sha256=production_sha,
            ),
            underlying_checkpoint_revision_status="provider_not_disclosed",
        )

    matrix = make_lf022_production_family_matrix(
        family_registry=(
            pin("moonshot_kimi_k2", KIMI_MODEL),
            pin("qwen3", QWEN_MODEL),
            pin("glm5", "zai-org/GLM-5.2"),
            pin("deepseek_v4", DEEPSEEK_MODEL),
            pin("openai_codex", "openai/gpt-5.6-terra", "openai_codex_exec"),
        ),
        proposer_family_ids=("moonshot_kimi_k2", "qwen3", "deepseek_v4"),
        judge_family_ids=("moonshot_kimi_k2", "deepseek_v4", "glm5"),
        sci_validator_family_ids=("moonshot_kimi_k2", "qwen3", "glm5", "deepseek_v4"),
        heldout_eval_family_id="openai_codex",
    )
    matrix_path = Path(spec.production_family_matrix.path)
    matrix_sha = _json(matrix_path, matrix.model_dump(mode="json"))
    revision = f"provider-deployment-snapshot:{production_sha}"
    altered = spec.model_copy(
        update={
            "candidate_manifest": BoundArtifact(path=str(manifest_path), sha256=manifest_sha),
            "candidate_records": BoundArtifact(path=str(candidate_path), sha256=candidate_sha),
            "production_family_matrix": BoundArtifact(path=str(matrix_path), sha256=matrix_sha),
            "judge_a": JudgeEndpointPin(
                provider_slot="judge_A",
                provider="epfl_rcp",
                model=KIMI_MODEL,
                family_id="moonshot_kimi_k2",
                revision=revision,
                decoding=_decoding("moonshot_kimi_k2").provider_decoding(),
            ),
            "judge_b": JudgeEndpointPin(
                provider_slot="judge_B",
                provider="epfl_rcp",
                model=DEEPSEEK_MODEL,
                family_id="deepseek_v4",
                revision=revision,
                decoding=_decoding("deepseek_v4").provider_decoding(),
            ),
        }
    )
    spec_sha = _json(spec_path, altered.model_dump(mode="json"))
    dispatches, weak_manifest = prepare_lf022_weak_batch(
        repo_root=Path.cwd(),
        spec_path=spec_path,
        expected_spec_sha256=spec_sha,
        randomization_key=KEY,
        output_dir=batch_root,
    )
    prompt_sha = dispatches[0].prompt_template_sha256
    claims: dict[str, Path] = {}
    for slot, family, model in (
        ("judge_A", "moonshot_kimi_k2", KIMI_MODEL),
        ("judge_B", "deepseek_v4", DEEPSEEK_MODEL),
    ):
        claim = _claim(
            family=family,
            model=model,
            decoding=_decoding(family),
            production_catalog=production_catalog,
            raw_catalog=raw_catalog,
            prompt_sha=prompt_sha,
        )
        claims[slot] = tmp_path / f"{slot}_claim.json"
        _json(claims[slot], claim.model_dump(mode="json"))

    bundle = tmp_path / "code_bundle.tar.gz"
    bundle_sha = _write(bundle, b"fixture-code-bundle")
    config = LF022WeakLiveSmokeConfig(
        parent_batch_id=weak_manifest.batch_id,
        parent_batch_spec_sha256=hash_file(batch_root / "batch_spec.json"),
        parent_dispatch_manifest_sha256=hash_file(batch_root / "dispatch_manifest.json"),
        parent_inventory_id=manifest.inventory_id,
        parent_candidate_manifest_sha256=hash_file(batch_root / "inputs/candidate_manifest.json"),
        parent_candidate_records_sha256=hash_file(batch_root / "inputs/candidate_records.jsonl"),
        production_family_matrix_sha256=hash_file(
            batch_root / "inputs/production_family_matrix.json"
        ),
        judge_a_claim=BoundArtifact(
            path=str(claims["judge_A"]), sha256=hash_file(claims["judge_A"])
        ),
        judge_b_claim=BoundArtifact(
            path=str(claims["judge_B"]), sha256=hash_file(claims["judge_B"])
        ),
        code_bundle=BoundArtifact(path=str(bundle), sha256=bundle_sha),
        code_tree_hash=TREE,
        producer_commit=COMMIT,
        retry_policy=LF022RCPRetryPolicy(
            max_attempts=1,
            request_timeout_seconds=60,
            base_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            retryable_http_statuses=(408, 429, 500, 502, 503, 504),
        ),
    )
    config_path = tmp_path / "live_config.json"
    config_sha = _json(config_path, config.model_dump(mode="json"))
    monkeypatch.setattr(
        "leanfaith.generation.lf022_weak_live_smoke.validate_code_bundle",
        lambda path, tree: hash_file(path),
    )
    monkeypatch.setattr(
        "leanfaith.generation.lf022_weak_live_smoke.collect_code_state",
        lambda root: CodeState(
            git_revision=COMMIT,
            git_dirty=False,
            code_tree_hash=TREE,
        ),
    )
    selector, admission = prepare_lf022_weak_live_smoke(
        repo_root=Path.cwd(),
        batch_root=batch_root,
        config_path=config_path,
        expected_config_sha256=config_sha,
    )
    admission_path = batch_root / "live_smoke/admission.json"
    return batch_root, admission_path, hash_file(admission_path), selector, admission


def _judge_output() -> str:
    return json.dumps(
        {
            "same_claim_answer": "not_same_claim",
            "relation": "unrelated",
            "A_implies_B": "no",
            "B_implies_A": "no",
            "error_types": ["E01"],
            "confidence": 0.9,
            "rationale": "The statements express materially different claims.",
            "needs_expert_review": False,
        }
    )


class FakeTransport:
    def __init__(
        self,
        model: str,
        failures: list[Exception] | None = None,
        *,
        output_text: str | None = None,
        returned_model: str | None = None,
    ) -> None:
        self.model = model
        self.failures = list(failures or [])
        self.output_text = output_text
        self.returned_model = returned_model
        self.calls = 0
        self.payloads: list[dict[str, object]] = []

    def post_json(self, **kwargs: object) -> RCPWireResponse:
        self.calls += 1
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        self.payloads.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        body = canonical_json_bytes(
            {
                "id": f"fake-{self.calls}",
                "model": self.returned_model or self.model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": self.output_text or _judge_output()},
                    }
                ],
                "usage": {"completion_tokens": 10, "prompt_tokens": 20},
            }
        )
        return RCPWireResponse(status_code=200, headers={}, body=body)


def _execute(
    *,
    batch_root: Path,
    admission_path: Path,
    admission_sha: str,
    transports: dict[str, FakeTransport],
    **kwargs: object,
):
    return execute_lf022_weak_live_smoke(
        repo_root=Path.cwd(),
        batch_root=batch_root,
        admission_path=admission_path,
        expected_admission_sha256=admission_sha,
        execute_public_provisional=True,
        credentials=LF022WeakRuntimeCredentials(
            base_url="https://inference.rcp.epfl.ch/v1",
            api_key="runtime-only-test-key",
        ),
        transports=transports,  # type: ignore[arg-type]
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def test_fresh_four_cell_smoke_and_offline_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, selector, _ = _prepare_live_foundation(
        tmp_path, monkeypatch
    )
    kimi = FakeTransport(KIMI_MODEL)
    deepseek = FakeTransport(DEEPSEEK_MODEL)
    terminals, manifest = _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={"judge_A": kimi, "judge_B": deepseek},
    )

    assert selector.eligible_candidate_count == 1
    assert len(terminals) == 4
    assert kimi.calls + deepseek.calls == 4
    assert manifest.status_counts == {"response_received": 4}
    assert manifest.parsed_evidence_count == 4
    assert manifest.supervision_eligible is False
    assert all(item.weak_terminal.call.supervision_eligible is False for item in terminals)
    assert all(item.weak_terminal.call.parse_status.value == "parsed" for item in terminals)
    first_messages = kimi.payloads[0]["messages"]
    assert isinstance(first_messages, list)
    assert [item["role"] for item in first_messages] == ["system", "user"]
    assert str(first_messages[1]["content"]).startswith("PROMPT_TEMPLATE_SHA256\n")

    replayed, replay_manifest = replay_lf022_weak_live_smoke(
        repo_root=Path.cwd(),
        batch_root=batch,
        admission_path=admission_path,
        expected_admission_sha256=admission_sha,
    )
    assert replayed == terminals
    assert replay_manifest == manifest
    assert kimi.calls + deepseek.calls == 4


def test_finalized_manifest_rejects_missing_cell_state_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, admission = _prepare_live_foundation(
        tmp_path, monkeypatch
    )
    first_kimi = FakeTransport(KIMI_MODEL)
    first_deepseek = FakeTransport(DEEPSEEK_MODEL)
    _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={"judge_A": first_kimi, "judge_B": first_deepseek},
    )
    assert first_kimi.calls + first_deepseek.calls == 4
    assert (batch / "live_smoke/execution_manifest.json").is_file()

    missing_cell = admission.allowed_dispatch_cell_ids[0]
    (batch / "live_smoke/terminals" / f"{missing_cell}.json").unlink()
    shutil.rmtree(batch / "live_smoke/cells" / missing_cell)
    rerun_kimi = FakeTransport(KIMI_MODEL)
    rerun_deepseek = FakeTransport(DEEPSEEK_MODEL)

    with pytest.raises(LF022WeakLiveSmokeError, match="missing a committed per-cell terminal"):
        _execute(
            batch_root=batch,
            admission_path=admission_path,
            admission_sha=admission_sha,
            transports={"judge_A": rerun_kimi, "judge_B": rerun_deepseek},
        )
    assert rerun_kimi.calls + rerun_deepseek.calls == 0


def test_transport_unknown_one_cell_does_not_block_other_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    kimi = FakeTransport(
        KIMI_MODEL,
        failures=[RCPTransportUnknownError("unknown delivery")],
    )
    deepseek = FakeTransport(DEEPSEEK_MODEL)
    terminals, manifest = _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={"judge_A": kimi, "judge_B": deepseek},
    )

    assert len(terminals) == 4
    assert manifest.status_counts == {
        "response_received": 3,
        "transport_unknown": 1,
    }
    assert manifest.parsed_evidence_count == 3
    weak = (batch / "live_smoke/weak_consensus_candidates.jsonl").read_text()
    assert '"status":"incomplete"' in weak


def test_crash_after_raw_wire_persistence_resumes_without_duplicate_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    kimi = FakeTransport(KIMI_MODEL)
    deepseek = FakeTransport(DEEPSEEK_MODEL)
    crashed = False

    def stop_once(_: str) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated process crash after raw wire persistence")

    with pytest.raises(RuntimeError, match="simulated process crash"):
        _execute(
            batch_root=batch,
            admission_path=admission_path,
            admission_sha=admission_sha,
            transports={"judge_A": kimi, "judge_B": deepseek},
            after_wire_response_persisted=stop_once,
        )
    assert kimi.calls + deepseek.calls == 1

    terminals, _ = _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={"judge_A": kimi, "judge_B": deepseek},
    )
    assert len(terminals) == 4
    assert kimi.calls + deepseek.calls == 4


def test_execution_rejects_admission_bound_batch_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    manifest_path = batch / "dispatch_manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    with pytest.raises((LF022WeakLiveSmokeError, ValueError), match=r"batch|canonical"):
        _execute(
            batch_root=batch,
            admission_path=admission_path,
            admission_sha=admission_sha,
            transports={
                "judge_A": FakeTransport(KIMI_MODEL),
                "judge_B": FakeTransport(DEEPSEEK_MODEL),
            },
        )


def test_historical_deepseek_malformed_json_is_incomplete_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    malformed = _judge_output().replace('"confidence": 0.9', '"confidence":电量0.99')
    kimi = FakeTransport(KIMI_MODEL)
    deepseek = FakeTransport(DEEPSEEK_MODEL, output_text=malformed)

    terminals, manifest = _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={"judge_A": kimi, "judge_B": deepseek},
    )

    assert kimi.calls == 2
    assert deepseek.calls == 2
    assert manifest.parsed_evidence_count == 2
    deepseek_terminals = [
        item for item in terminals if item.weak_terminal.call.model_family == "deepseek_v4"
    ]
    assert len(deepseek_terminals) == 2
    assert all(
        item.weak_terminal.call.parse_status.value == "parse_failed" for item in deepseek_terminals
    )
    weak = (batch / "live_smoke/weak_consensus_candidates.jsonl").read_text()
    assert '"status":"incomplete"' in weak
    assert '"train_eligible":false' in weak
    assert '"eval_eligible":false' in weak
    assert manifest.supervision_eligible is False


def test_replay_rejects_wire_body_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={
            "judge_A": FakeTransport(KIMI_MODEL),
            "judge_B": FakeTransport(DEEPSEEK_MODEL),
        },
    )
    body_path = sorted((batch / "live_smoke/cells").glob("*/wire_response.body"))[0]
    body_path.write_bytes(body_path.read_bytes() + b"tamper")

    with pytest.raises(LF022WeakLiveSmokeError, match=r"wire response.*hash"):
        replay_lf022_weak_live_smoke(
            repo_root=Path.cwd(),
            batch_root=batch,
            admission_path=admission_path,
            expected_admission_sha256=admission_sha,
        )


def test_completed_marker_without_response_fails_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, admission = _prepare_live_foundation(
        tmp_path, monkeypatch
    )
    first_cell = admission.allowed_dispatch_cell_ids[0]
    marker = batch / "live_smoke/cells" / first_cell / ".transport_completed"
    marker.parent.mkdir(parents=True)
    marker.write_text("completed\n", encoding="utf-8")
    kimi = FakeTransport(KIMI_MODEL)
    deepseek = FakeTransport(DEEPSEEK_MODEL)

    with pytest.raises(LF022WeakLiveSmokeError, match="without persisted response"):
        _execute(
            batch_root=batch,
            admission_path=admission_path,
            admission_sha=admission_sha,
            transports={"judge_A": kimi, "judge_B": deepseek},
        )
    assert kimi.calls + deepseek.calls == 0


def test_wrong_returned_model_is_terminal_and_other_cells_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    kimi = FakeTransport(KIMI_MODEL)
    deepseek = FakeTransport(DEEPSEEK_MODEL, returned_model="wrong/model")

    terminals, manifest = _execute(
        batch_root=batch,
        admission_path=admission_path,
        admission_sha=admission_sha,
        transports={"judge_A": kimi, "judge_B": deepseek},
    )

    assert len(terminals) == 4
    assert kimi.calls + deepseek.calls == 4
    assert manifest.status_counts == {
        "invalid_response": 2,
        "response_received": 2,
    }
    assert manifest.parsed_evidence_count == 2


def test_offline_freeze_authors_exact_claims_and_config_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, _, _, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    output = tmp_path / "frozen_live_smoke"

    first = freeze_lf022_weak_live_smoke_inputs(
        repo_root=tmp_path,
        batch_root=batch,
        production_catalog_path=tmp_path / "production_catalog.json",
        raw_rcp_catalog_path=tmp_path / "raw_models.json",
        code_bundle_path=tmp_path / "code_bundle.tar.gz",
        output_dir=output,
    )
    second = freeze_lf022_weak_live_smoke_inputs(
        repo_root=tmp_path,
        batch_root=batch,
        production_catalog_path=tmp_path / "production_catalog.json",
        raw_rcp_catalog_path=tmp_path / "raw_models.json",
        code_bundle_path=tmp_path / "code_bundle.tar.gz",
        output_dir=output,
    )

    assert first == second
    assert hash_file(first.config_path) == first.config_sha256
    config = LF022WeakLiveSmokeConfig.model_validate_json(first.config_path.read_bytes())
    assert config.parent_candidate_records_sha256 == hash_file(
        batch / "inputs/candidate_records.jsonl"
    )
    judge_a = LF022WeakJudgeRouteClaim.model_validate_json(first.judge_a_claim_path.read_bytes())
    judge_b = LF022WeakJudgeRouteClaim.model_validate_json(first.judge_b_claim_path.read_bytes())
    assert (judge_a.family_id, judge_a.model_id) == (
        "moonshot_kimi_k2",
        KIMI_MODEL,
    )
    assert (judge_b.family_id, judge_b.model_id) == (
        "deepseek_v4",
        DEEPSEEK_MODEL,
    )
    assert judge_a.scale_judge_qualified is False
    assert judge_b.scale_judge_qualified is False


def test_symlinked_batch_root_is_rejected_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, admission_path, admission_sha, _, _ = _prepare_live_foundation(tmp_path, monkeypatch)
    linked_batch = tmp_path / "linked_batch"
    linked_batch.symlink_to(batch, target_is_directory=True)
    kimi = FakeTransport(KIMI_MODEL)
    deepseek = FakeTransport(DEEPSEEK_MODEL)

    with pytest.raises(LF022WeakLiveSmokeError, match="symlink component"):
        _execute(
            batch_root=linked_batch,
            admission_path=admission_path,
            admission_sha=admission_sha,
            transports={"judge_A": kimi, "judge_B": deepseek},
        )
    assert kimi.calls + deepseek.calls == 0


def test_config_frozen_for_one_parent_batch_cannot_prepare_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    _prepare_live_foundation(first_root, monkeypatch)
    second_batch, _, _, _, _ = _prepare_live_foundation(second_root, monkeypatch)
    first_config = first_root / "live_config.json"

    with pytest.raises(LF022WeakLiveSmokeError, match="prepared parent batch"):
        prepare_lf022_weak_live_smoke(
            repo_root=Path.cwd(),
            batch_root=second_batch,
            config_path=first_config,
            expected_config_sha256=hash_file(first_config),
        )


def test_offline_freeze_rejects_non_qwen_parent_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path, batch_root, _ = _foundation(tmp_path, candidate_schema_version=3)
    prepare_lf022_weak_batch(
        repo_root=Path.cwd(),
        spec_path=spec_path,
        expected_spec_sha256=hash_file(spec_path),
        randomization_key=KEY,
        output_dir=batch_root,
    )
    bundle = tmp_path / "code_bundle.tar.gz"
    _write(bundle, b"fixture-code-bundle")
    monkeypatch.setattr(
        "leanfaith.generation.lf022_weak_live_smoke.validate_code_bundle",
        lambda path, tree: hash_file(path),
    )
    monkeypatch.setattr(
        "leanfaith.generation.lf022_weak_live_smoke.collect_code_state",
        lambda root: CodeState(
            git_revision=COMMIT,
            git_dirty=False,
            code_tree_hash=TREE,
        ),
    )

    with pytest.raises(LF022WeakLiveSmokeError, match="exact Qwen"):
        freeze_lf022_weak_live_smoke_inputs(
            repo_root=tmp_path,
            batch_root=batch_root,
            production_catalog_path=tmp_path / "unused-production.json",
            raw_rcp_catalog_path=tmp_path / "unused-raw.json",
            code_bundle_path=bundle,
            output_dir=tmp_path / "frozen",
        )
