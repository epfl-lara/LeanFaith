"""Public LF-022 RCP smoke admission and fake-transport execution tests."""

from __future__ import annotations

import datetime
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.generation.lf022_rcp_smoke_v1 import (
    LF022RCPSmokeCatalogError,
    LF022RCPSmokeError,
    execute_public_smoke,
    extract_content_only,
    load_lf022_rcp_smoke,
    probe_and_write_smoke_preflight,
    replay_public_smoke,
    replay_public_smoke_failure,
)
from leanfaith.generation.rcp_qualification_v1 import (
    RCPCredentials,
    RCPHTTPResponse,
    RCPTransportError,
)
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.schemas.enums import ValidationStatus

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/lf022_rcp_public_smoke_v1.yaml"
CONFIG_V2 = ROOT / "configs/generation/lf022_rcp_public_smoke_v2.yaml"
CONFIG_V3 = ROOT / "configs/generation/lf022_rcp_public_smoke_v3.yaml"
FAILURE_V1 = (
    ROOT
    / "data/raw/llm_variants/lf022_rcp_public_smoke_v1"
    / "ba820b59f24090eaa93c8d205e732e7a5c78f9413ae78c325be5e8273a82734a"
    / "failure_manifest.json"
)
FAILURE_V2 = (
    ROOT
    / "data/raw/llm_variants/lf022_rcp_public_smoke_v2"
    / "f1ce60d318fa59c61da08302cfc33c03b3629e17e57854b810e385d426b35ce6"
    / "failure_manifest.json"
)
SUCCESS_V3 = (
    ROOT
    / "data/raw/llm_variants/lf022_rcp_public_smoke_v3"
    / "61e201acc254d89cb5e9686bd56a7f4e03c0ea2f8169ae39e22cc31be48a0589"
    / "manifest.json"
)
NOW = datetime.datetime(2026, 7, 28, 12, 0, tzinfo=datetime.UTC)


def _copy_admission(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "lf022_rcp_public_smoke_v1.yaml"
    shutil.copy2(CONFIG, config_path)
    relative_paths = (
        "data/parsed/real_outputs/public_research_v1/one_example_preflight_v1/"
        "problem_pool_records.jsonl",
        "data/parsed/real_outputs/public_research_v1/one_example_preflight_v1/"
        "reference_theorems.jsonl",
        "data/parsed/real_outputs/public_research_v1/one_example_preflight_v1/"
        "reference_representations.jsonl",
        "examples/lf021_public_research_mathlib_header_v1.lean",
        "prompts/proposers/lean_variant_v1.txt",
        "prompts/judges/lean_pair_blinded_v1.txt",
    )
    for relative in relative_paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return config_path, tmp_path


def test_v2_is_a_distinct_admission_with_larger_forced_thinking_budgets() -> None:
    loaded = load_lf022_rcp_smoke(CONFIG_V2, repo_root=ROOT)
    config = loaded.loaded_config.config
    assert config.config_id == "lf022_rcp_public_smoke_v2"
    assert config.outputs.raw_root.endswith("_v2")
    assert config.outputs.preflight_root.endswith("_v2")
    assert config.providers.proposer.decoding is not None
    assert config.providers.judge_A.decoding is not None
    assert config.providers.judge_B.decoding is not None
    assert config.providers.proposer.decoding.max_tokens == 16_384
    assert config.providers.judge_A.decoding.max_tokens == 8_192
    assert config.providers.judge_B.decoding.max_tokens == 8_192


def test_v3_replaces_only_the_failed_judge_family() -> None:
    v2 = load_lf022_rcp_smoke(CONFIG_V2, repo_root=ROOT).loaded_config.config
    v3 = load_lf022_rcp_smoke(CONFIG_V3, repo_root=ROOT).loaded_config.config
    assert v3.config_id == "lf022_rcp_public_smoke_v3"
    assert v3.providers.proposer.model_id == v2.providers.proposer.model_id
    assert v3.providers.proposer.decoding == v2.providers.proposer.decoding
    assert v3.providers.judge_A.model_id == "Qwen/Qwen3.5-397B-A17B"
    assert v3.providers.judge_A.family_id == "qwen_3_5"
    assert v3.providers.judge_B == v2.providers.judge_B
    assert v3.outputs.raw_root.endswith("_v3")
    assert v3.outputs.preflight_root.endswith("_v3")


@pytest.mark.parametrize(
    ("config_path", "failure_path", "attempts"),
    ((CONFIG, FAILURE_V1, 1), (CONFIG_V2, FAILURE_V2, 2)),
)
def test_committed_failure_lineages_replay_typed_and_cli_reports_persisted_attempts(
    config_path: Path,
    failure_path: Path,
    attempts: int,
) -> None:
    loaded = load_lf022_rcp_smoke(config_path, repo_root=ROOT)
    failure = replay_public_smoke_failure(
        loaded,
        failure_manifest_path=failure_path,
        repo_root=ROOT,
    )
    assert failure.chat_completion_attempts == attempts

    result = CliRunner().invoke(
        app,
        [
            "lf022-rcp-smoke",
            "--root",
            str(ROOT),
            "--config",
            str(config_path),
            "--replay-failure-manifest",
            str(failure_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "network_calls_this_run=0" in result.output
    assert f"persisted_chat_completion_attempts={attempts}" in result.output
    assert "chat_calls=" not in result.output


def test_committed_v3_success_lineage_replays_with_consensus_binding() -> None:
    loaded = load_lf022_rcp_smoke(CONFIG_V3, repo_root=ROOT)
    replay = replay_public_smoke(
        loaded,
        manifest_path=SUCCESS_V3,
        repo_root=ROOT,
    )
    assert replay.weak_consensus_status == "candidate_consensus"
    assert replay.proposer_call_count + replay.judge_call_count == 5
    assert replay.semantic_labels_created is False
    assert replay.supervision_eligible is False
    assert replay.training_eligible is False
    assert replay.evaluation_eligible is False


class FakeTransport:
    def __init__(self, *, omit_model: str | None = None) -> None:
        self.get_count = 0
        self.post_count = 0
        self.payloads: list[Mapping[str, object]] = []
        self.omit_model = omit_model

    def get(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: int,
    ) -> RCPHTTPResponse:
        del api_key, timeout_seconds
        self.get_count += 1
        assert url == "https://inference.rcp.epfl.ch/v1/models"
        model_ids = (
            "moonshotai/Kimi-K2.7-Code",
            "deepseek-ai/DeepSeek-V4-Pro",
            "zai-org/GLM-5.2",
        )
        return RCPHTTPResponse(
            status_code=200,
            body=canonical_json_bytes(
                {
                    "object": "list",
                    "data": [{"id": model} for model in model_ids if model != self.omit_model],
                }
            ),
        )

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPHTTPResponse:
        del api_key, timeout_seconds
        self.post_count += 1
        self.payloads.append(payload)
        assert url == "https://inference.rcp.epfl.ch/v1/chat/completions"
        assert payload["reasoning_effort"] == "high"
        assert payload["chat_template_kwargs"] == {"enable_thinking": True}
        model = payload["model"]
        assert isinstance(model, str)
        if model == "moonshotai/Kimi-K2.7-Code":
            content = json.dumps(
                {
                    "variants": [
                        {
                            "candidate_lean": (
                                "theorem lf022_rcp_smoke_candidate {x : ℝ} "
                                "(hx : 0 ≤ x) : x - x ^ 3 / 7 ≤ Real.sin x"
                            ),
                            "intended_relation": "near_miss",
                            "intended_error_types": ["E21"],
                            "edit_summary": "Changed the cubic denominator.",
                            "confidence": 0.8,
                            "assumptions": [],
                            "potential_ambiguity": None,
                        }
                    ]
                }
            )
        else:
            content = json.dumps(
                {
                    "same_claim_answer": "uncertain",
                    "relation": None,
                    "A_implies_B": "unknown",
                    "B_implies_A": "unknown",
                    "error_types": [],
                    "confidence": 0.4,
                    "rationale": "This smoke response deliberately abstains.",
                    "needs_expert_review": True,
                }
            )
        return RCPHTTPResponse(
            status_code=200,
            body=canonical_json_bytes(
                {
                    "id": f"rcp-fixture-{self.post_count}",
                    "model": model,
                    "choices": [
                        {
                            "message": {
                                "content": content,
                                # A deliberately parseable but conflicting hidden
                                # object proves task parsing consumes content only.
                                "reasoning_content": '{"variants":[]}',
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                }
            ),
        )


class FakeLeanBackend:
    def __init__(self) -> None:
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        assert request.code is not None
        namespace = re.search(r"(?m)^namespace ([^\n]+)$", request.code)
        declaration = re.search(r"(?m)^theorem ([^\s:({\[]+)", request.code)
        assert namespace is not None
        assert declaration is not None
        fingerprint = request.context_id.removeprefix("ctx:")
        return LeanResult(
            request_id=request.request_id,
            request_hash="1" * 64,
            context_id=request.context_id,
            context_fingerprint=fingerprint,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=(
                {
                    "kind": "theorem",
                    "full_name": f"{namespace.group(1)}.{declaration.group(1)}",
                },
            ),
            sorries=({"kind": "sorry"},),
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _credentials() -> RCPCredentials:
    return RCPCredentials(
        base_url="https://inference.rcp.epfl.ch/v1",
        api_key="fixture-secret-never-persist",
    )


def _rewrite_self_consistent_failure_manifest(failure_path: Path, repo_root: Path) -> None:
    """Rehash a tampered partial run the way an artifact-only adversary could."""

    payload = json.loads(failure_path.read_text(encoding="utf-8"))
    run_root = failure_path.parent
    artifacts = [
        {
            "artifact": str(path.relative_to(repo_root)),
            "sha256": hash_file(path),
        }
        for path in sorted(run_root.rglob("*"))
        if path.is_file() and not path.is_symlink() and path != failure_path
    ]
    payload["artifacts"] = artifacts
    payload["chat_completion_attempts"] = sum(
        item["artifact"].endswith("/wire_request.json") for item in artifacts
    )
    payload["completed_call_count"] = sum(
        item["artifact"].endswith("/call_artifact.json") for item in artifacts
    )
    content = {key: value for key, value in payload.items() if key != "failure_id"}
    payload["failure_id"] = "lf022_rcp_smoke_failure:" + hash_canonical(content)
    failure_path.write_bytes(canonical_json_bytes(payload) + b"\n")


def test_preflight_is_catalog_only_and_fail_closed(tmp_path: Path) -> None:
    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    transport = FakeTransport()

    result = probe_and_write_smoke_preflight(
        loaded,
        repo_root=repo_root,
        credentials=_credentials(),
        transport=transport,
    )

    assert transport.get_count == 1
    assert transport.post_count == 0
    assert result.preflight.generation_distribution == "G_open"
    assert result.preflight.sci_conditioning_performed is False
    assert result.preflight.chat_completion_requests_performed == 0
    assert result.preflight.semantic_labels_created is False
    assert result.preflight.silver_records_created is False
    assert result.preflight.training_eligible is False
    assert result.preflight.evaluation_eligible is False
    assert result.preflight.gate_credit_claimed is False
    assert result.catalog_path.read_bytes().startswith(b"{")

    missing = FakeTransport(omit_model="zai-org/GLM-5.2")
    with pytest.raises(LF022RCPSmokeCatalogError, match="lacks exact admitted"):
        probe_and_write_smoke_preflight(
            loaded,
            repo_root=repo_root,
            credentials=_credentials(),
            transport=missing,
        )
    assert missing.post_count == 0


def test_content_only_parser_records_but_never_consumes_reasoning() -> None:
    visible = '{"same_claim_answer":"uncertain"}'
    body = canonical_json_bytes(
        {
            "id": "request-1",
            "model": "example/model",
            "choices": [
                {
                    "message": {
                        "content": visible,
                        "reasoning_content": '{"same_claim_answer":"same_claim"}',
                        "reasoning": "another hidden answer",
                    }
                }
            ],
            "usage": {"total_tokens": 9},
        }
    )

    content, metadata = extract_content_only(body, expected_model="example/model")

    assert content == visible
    assert metadata.reasoning_content_present
    assert metadata.reasoning_present
    assert metadata.reasoning_content_sha256 is not None
    assert metadata.usage == {"total_tokens": 9}


def test_explicit_fake_smoke_is_exactly_five_calls_and_replays(tmp_path: Path) -> None:
    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    transport = FakeTransport()
    preflight = probe_and_write_smoke_preflight(
        loaded,
        repo_root=repo_root,
        credentials=_credentials(),
        transport=transport,
    )
    backend = FakeLeanBackend()

    with pytest.raises(LF022RCPSmokeError, match="requires execute_public_smoke"):
        execute_public_smoke(
            loaded,
            preflight_run=preflight,
            repo_root=repo_root,
            credentials=_credentials(),
            transport=transport,
            lean_backend=backend,
            execute_public_smoke=False,
            clock=lambda: NOW,
        )
    assert transport.post_count == 0

    run = execute_public_smoke(
        loaded,
        preflight_run=preflight,
        repo_root=repo_root,
        credentials=_credentials(),
        transport=transport,
        lean_backend=backend,
        execute_public_smoke=True,
        clock=lambda: NOW,
    )

    assert transport.post_count == 5
    assert [payload["model"] for payload in transport.payloads] == [
        "moonshotai/Kimi-K2.7-Code",
        "deepseek-ai/DeepSeek-V4-Pro",
        "deepseek-ai/DeepSeek-V4-Pro",
        "zai-org/GLM-5.2",
        "zai-org/GLM-5.2",
    ]
    assert len(backend.requests) == 1
    assert run.variant.validation_status is ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    assert run.variant.metadata["artifact_class"] == "smoke"
    assert run.variant.metadata["training_eligible"] is False
    assert run.manifest.proposer_call_count == 1
    assert run.manifest.judge_call_count == 4
    assert run.manifest.primary_eval_judge_call_count == 0
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.silver_records_created is False
    assert run.manifest.supervision_eligible is False
    assert run.manifest.training_eligible is False
    assert run.manifest.evaluation_eligible is False
    assert run.manifest.gate_credit_claimed is False
    assert run.manifest.weak_consensus_status == "all_abstain"
    assert all(call.wire.reasoning_content_present for call in run.manifest.call_artifacts)
    for call in run.manifest.call_artifacts:
        wire_request = repo_root / call.wire_request_artifact
        wire_response = repo_root / call.wire_response_artifact
        assert wire_request.is_file()
        assert wire_response.is_file()
        assert (repo_root / call.llm_call_artifact).is_file()
        assert wire_request.stat().st_mode & 0o777 == 0o600
        assert wire_response.stat().st_mode & 0o777 == 0o600
        assert b"fixture-secret-never-persist" not in wire_request.read_bytes()
        assert b"fixture-secret-never-persist" not in wire_response.read_bytes()

    replay = replay_public_smoke(
        loaded,
        manifest_path=run.manifest_path,
        repo_root=repo_root,
    )
    assert replay == run.manifest

    original_manifest = run.manifest_path.read_bytes()
    forged_manifest = json.loads(original_manifest)
    forged_manifest["weak_consensus_status"] = "incomplete"
    run.manifest_path.write_bytes(canonical_json_bytes(forged_manifest) + b"\n")
    with pytest.raises(LF022RCPSmokeError, match="downstream artifact bindings differ"):
        replay_public_smoke(
            loaded,
            manifest_path=run.manifest_path,
            repo_root=repo_root,
        )
    run.manifest_path.write_bytes(original_manifest)

    first_wire_request = repo_root / run.manifest.call_artifacts[0].wire_request_artifact
    first_wire_request.write_bytes(b'{"tampered":true}\n')
    with pytest.raises(LF022RCPSmokeError, match="artifact hash differs"):
        replay_public_smoke(
            loaded,
            manifest_path=run.manifest_path,
            repo_root=repo_root,
        )


def test_server_credential_echo_is_rejected_before_response_persistence(
    tmp_path: Path,
) -> None:
    class CredentialEchoTransport(FakeTransport):
        def post_json(
            self,
            *,
            url: str,
            api_key: str,
            payload: Mapping[str, object],
            timeout_seconds: int,
        ) -> RCPHTTPResponse:
            response = super().post_json(
                url=url,
                api_key=api_key,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
            document = json.loads(response.body)
            document["credential_echo"] = f"Bearer {api_key}"
            return RCPHTTPResponse(
                status_code=response.status_code,
                body=canonical_json_bytes(document),
            )

    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    transport = CredentialEchoTransport()
    credentials = _credentials()
    preflight = probe_and_write_smoke_preflight(
        loaded,
        repo_root=repo_root,
        credentials=credentials,
        transport=transport,
    )

    with pytest.raises(LF022RCPSmokeError, match="credential material"):
        execute_public_smoke(
            loaded,
            preflight_run=preflight,
            repo_root=repo_root,
            credentials=credentials,
            transport=transport,
            lean_backend=FakeLeanBackend(),
            execute_public_smoke=True,
            clock=lambda: NOW,
        )

    run_root = next(path.parent for path in repo_root.rglob("failure_manifest.json"))
    assert not any(path.name == "wire_response.json" for path in run_root.rglob("*"))
    for path in run_root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert credentials.api_key.encode("utf-8") not in path.read_bytes()


def test_symlinked_smoke_output_roots_are_rejected_before_network(
    tmp_path: Path,
) -> None:
    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    config = loaded.loaded_config.config
    outside = tmp_path / "outside"
    outside.mkdir()
    preflight_root = repo_root / config.outputs.preflight_root
    preflight_root.parent.mkdir(parents=True, exist_ok=True)
    preflight_root.symlink_to(outside, target_is_directory=True)
    transport = FakeTransport()

    with pytest.raises(LF022RCPSmokeError, match="symlinked path component"):
        probe_and_write_smoke_preflight(
            loaded,
            repo_root=repo_root,
            credentials=_credentials(),
            transport=transport,
        )
    assert transport.get_count == 0
    assert not tuple(outside.iterdir())


def test_symlinked_smoke_run_directory_is_rejected_before_inference(
    tmp_path: Path,
) -> None:
    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    transport = FakeTransport()
    preflight = probe_and_write_smoke_preflight(
        loaded,
        repo_root=repo_root,
        credentials=_credentials(),
        transport=transport,
    )
    run_key = hash_canonical(
        {
            "schema": "lf022_rcp_public_smoke_run_v1",
            "config_hash": loaded.loaded_config.config_hash,
            "catalog_raw_response_sha256": preflight.preflight.catalog.raw_response_sha256,
            "problem_record_id": loaded.problem.problem_record_id,
        }
    )
    raw_root = repo_root / loaded.loaded_config.config.outputs.raw_root
    raw_root.mkdir(parents=True)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (raw_root / run_key).symlink_to(outside, target_is_directory=True)

    with pytest.raises(LF022RCPSmokeError, match="symlinked path component"):
        execute_public_smoke(
            loaded,
            preflight_run=preflight,
            repo_root=repo_root,
            credentials=_credentials(),
            transport=transport,
            lean_backend=FakeLeanBackend(),
            execute_public_smoke=True,
            clock=lambda: NOW,
        )
    assert transport.post_count == 0
    assert not tuple(outside.iterdir())


def test_live_execution_rejects_forged_preflight_binding(tmp_path: Path) -> None:
    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    transport = FakeTransport()
    preflight = probe_and_write_smoke_preflight(
        loaded,
        repo_root=repo_root,
        credentials=_credentials(),
        transport=transport,
    )
    forged = preflight.preflight.model_copy(update={"problem_record_id": "problem:" + "f" * 64})
    with pytest.raises(LF022RCPSmokeError, match="persisted preflight differs"):
        execute_public_smoke(
            loaded,
            preflight_run=replace(preflight, preflight=forged),
            repo_root=repo_root,
            credentials=_credentials(),
            transport=transport,
            lean_backend=FakeLeanBackend(),
            execute_public_smoke=True,
            clock=lambda: NOW,
        )
    assert transport.post_count == 0


def test_partial_failure_is_terminal_replayable_and_never_retried(tmp_path: Path) -> None:
    class FailingTransport(FakeTransport):
        def post_json(
            self,
            *,
            url: str,
            api_key: str,
            payload: Mapping[str, object],
            timeout_seconds: int,
        ) -> RCPHTTPResponse:
            if self.post_count == 2:
                self.post_count += 1
                raise RCPTransportError(
                    "fixture_timeout",
                    "fixture timeout",
                    retryable=True,
                )
            return super().post_json(
                url=url,
                api_key=api_key,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )

    config_path, repo_root = _copy_admission(tmp_path)
    loaded = load_lf022_rcp_smoke(config_path, repo_root=repo_root)
    transport = FailingTransport()
    preflight = probe_and_write_smoke_preflight(
        loaded,
        repo_root=repo_root,
        credentials=_credentials(),
        transport=transport,
    )
    with pytest.raises(RCPTransportError, match="fixture timeout"):
        execute_public_smoke(
            loaded,
            preflight_run=preflight,
            repo_root=repo_root,
            credentials=_credentials(),
            transport=transport,
            lean_backend=FakeLeanBackend(),
            execute_public_smoke=True,
            clock=lambda: NOW,
        )
    assert transport.post_count == 3
    failure_path = next(repo_root.rglob("failure_manifest.json"))
    failure = replay_public_smoke_failure(
        loaded,
        failure_manifest_path=failure_path,
        repo_root=repo_root,
    )
    assert failure.chat_completion_attempts == 3
    assert failure.completed_call_count == 2
    assert failure.training_eligible is False

    run_root = failure_path.parent
    lock_path = run_root / "run.lock"
    original_lock = lock_path.read_bytes()
    forged_lock = json.loads(original_lock)
    forged_lock["preflight_id"] = "lf022_rcp_preflight:" + "f" * 64
    lock_path.write_bytes(canonical_json_bytes(forged_lock) + b"\n")
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)
    with pytest.raises(LF022RCPSmokeError, match="preflight artifact is missing"):
        replay_public_smoke_failure(
            loaded,
            failure_manifest_path=failure_path,
            repo_root=repo_root,
        )
    lock_path.write_bytes(original_lock)
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)

    request_path = run_root / "calls/proposer/provider_request.json"
    original_request = request_path.read_bytes()
    request_path.write_bytes(original_request + b"\n")
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)
    with pytest.raises(LF022RCPSmokeError, match="provider request is invalid"):
        replay_public_smoke_failure(
            loaded,
            failure_manifest_path=failure_path,
            repo_root=repo_root,
        )
    request_path.write_bytes(original_request)
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)

    wire_path = run_root / "calls/proposer/wire_request.json"
    original_wire = wire_path.read_bytes()
    forged_wire = json.loads(original_wire)
    forged_wire["model"] = "forged/model"
    wire_path.write_bytes(canonical_json_bytes(forged_wire) + b"\n")
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)
    with pytest.raises(LF022RCPSmokeError, match="wire request differs"):
        replay_public_smoke_failure(
            loaded,
            failure_manifest_path=failure_path,
            repo_root=repo_root,
        )
    wire_path.write_bytes(original_wire)
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)

    lineage_path = run_root / "calls/proposer/llm_call.json"
    original_lineage = lineage_path.read_bytes()
    forged_lineage = json.loads(original_lineage)
    forged_lineage["parse_status"] = "parse_failed"
    forged_lineage["parsed_output"] = None
    lineage_path.write_bytes(canonical_json_bytes(forged_lineage) + b"\n")
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)
    with pytest.raises(LF022RCPSmokeError, match="parse-failure metadata differs"):
        replay_public_smoke_failure(
            loaded,
            failure_manifest_path=failure_path,
            repo_root=repo_root,
        )
    lineage_path.write_bytes(original_lineage)
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)

    variant_path = run_root / "variant.json"
    original_variant = variant_path.read_bytes()
    forged_variant = json.loads(original_variant)
    forged_variant["metadata"]["training_eligible"] = True
    variant_path.write_bytes(canonical_json_bytes(forged_variant) + b"\n")
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)
    with pytest.raises(LF022RCPSmokeError, match="partial variant quarantine differs"):
        replay_public_smoke_failure(
            loaded,
            failure_manifest_path=failure_path,
            repo_root=repo_root,
        )
    variant_path.write_bytes(original_variant)
    unexpected_path = run_root / "untyped.bin"
    unexpected_path.write_bytes(b"not a typed lineage artifact")
    _rewrite_self_consistent_failure_manifest(failure_path, repo_root)
    with pytest.raises(LF022RCPSmokeError, match="untyped or missing lineage artifacts"):
        replay_public_smoke_failure(
            loaded,
            failure_manifest_path=failure_path,
            repo_root=repo_root,
        )

    with pytest.raises(LF022RCPSmokeError, match="already claimed"):
        execute_public_smoke(
            loaded,
            preflight_run=preflight,
            repo_root=repo_root,
            credentials=_credentials(),
            transport=transport,
            lean_backend=FakeLeanBackend(),
            execute_public_smoke=True,
            clock=lambda: NOW,
        )
    assert transport.post_count == 3
