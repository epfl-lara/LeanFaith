from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    PrivateContentTransmissionError,
    ProviderIdentity,
    ProviderRequest,
    ReplayArtifactError,
    bridge_provider_result_to_generic_llm_lineage,
    persist_provider_request,
    verify_generic_llm_call_artifacts,
)
from leanfaith.schemas import LLMRole, ParseStatus, check_llm_call_attempt_lineage

NOW = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)


def _identity() -> ProviderIdentity:
    return ProviderIdentity(
        provider="fixture",
        model="fixture/critic",
        revision="sha-fixture",
        transport="fixture",
    )


def _request(*, private: bool = False) -> ProviderRequest:
    return ProviderRequest.create(
        identity=_identity(),
        prompt_template_hash="a" * 64,
        rendered_prompt="Return strict JSON.",
        decoding={"temperature": 0.0},
        input_ids=("pair:" + "1" * 64,),
        private_source_content=private,
    )


def test_generic_bridge_persists_and_verifies_proposer_lineage(tmp_path: Path) -> None:
    request = _request()
    request_path = tmp_path / "requests" / "request.json"
    request_hash = persist_provider_request(request, request_path)
    result = DeterministicFixtureProvider(
        identity=_identity(),
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: '{"variants":[]}'},
    ).generate(request)
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        role=LLMRole.PROPOSER,
        provider_slot="proposer_fixture",
        model_family="fixture_proposer",
        prompt_template_id="lean_variant",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.PARSED,
        parsed_output={"variants": []},
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=NOW,
        completed_at=NOW + datetime.timedelta(milliseconds=3),
        supervision_eligible=False,
    )
    assert lineage.call.role is LLMRole.PROPOSER
    assert lineage.call.request_artifact_sha256 == request_hash
    assert lineage.call.raw_response_sha256 == hash_file(result.raw_response_path)
    assert check_llm_call_attempt_lineage(lineage.call, (lineage.attempt,)) == []
    assert (
        verify_generic_llm_call_artifacts(
            call=lineage.call,
            expected_role=LLMRole.PROPOSER,
            expected_input_ids=request.input_ids,
            private_source_content=False,
            denylist_checked=True,
            denylist_hits=(),
            artifact_root=tmp_path,
        )
        == result.response
    )


def test_generic_bridge_retains_completed_parse_failure(tmp_path: Path) -> None:
    request = _request()
    request_path = tmp_path / "request.json"
    persist_provider_request(request, request_path)
    result = DeterministicFixtureProvider(
        identity=_identity(),
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "not-json"},
    ).generate(request)
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        role=LLMRole.JUDGE,
        provider_slot="judge_A",
        model_family="fixture_judge",
        prompt_template_id="lean_pair_blinded",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.PARSE_FAILED,
        parsed_output=None,
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=NOW,
        completed_at=NOW,
        supervision_eligible=True,
    )
    assert lineage.call.parse_status is ParseStatus.PARSE_FAILED
    assert lineage.call.parsed_output is None
    assert lineage.call.raw_output_artifact is not None


def test_generic_bridge_rejects_private_external_execution(tmp_path: Path) -> None:
    request = _request(private=True)
    request_path = tmp_path / "request.json"
    persist_provider_request(request, request_path)
    result = DeterministicFixtureProvider(
        identity=_identity(),
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "{}"},
    ).generate(request)
    with pytest.raises(PrivateContentTransmissionError, match="external"):
        bridge_provider_result_to_generic_llm_lineage(
            request=request,
            result=result,
            request_artifact_path=request_path,
            artifact_root=tmp_path,
            role=LLMRole.PROPOSER,
            provider_slot="proposer",
            model_family="fixture",
            prompt_template_id="lean_variant",
            prompt_template_version="v1",
            execution_mode="external",
            parse_status=ParseStatus.PARSED,
            parsed_output={},
            private_source_content=True,
            denylist_checked=True,
            denylist_hits=(),
            started_at=NOW,
            completed_at=NOW,
            supervision_eligible=False,
        )


def test_generic_verifier_detects_tampered_raw_artifact(tmp_path: Path) -> None:
    request = _request()
    request_path = tmp_path / "request.json"
    persist_provider_request(request, request_path)
    result = DeterministicFixtureProvider(
        identity=_identity(),
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "{}"},
    ).generate(request)
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        role=LLMRole.JUDGE,
        provider_slot="judge_A",
        model_family="fixture",
        prompt_template_id="lean_pair_blinded",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.PARSED,
        parsed_output={},
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=NOW,
        completed_at=NOW,
        supervision_eligible=True,
    )
    result.raw_response_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ReplayArtifactError):
        verify_generic_llm_call_artifacts(
            call=lineage.call,
            expected_role=LLMRole.JUDGE,
            expected_input_ids=request.input_ids,
            private_source_content=False,
            denylist_checked=True,
            denylist_hits=(),
            artifact_root=tmp_path,
        )
