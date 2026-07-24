"""LF-021 offline provider protocol, persistence, and privacy boundary."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    DisabledExternalProvider,
    FixtureResponseMissingError,
    GenerationProvider,
    PrivateContentTransmissionError,
    ProviderDisabledError,
    ProviderIdentity,
    ProviderIdentityMismatchError,
    ProviderRequest,
    RawResponseConflictError,
    ReplayArtifactError,
    ReplayProvider,
    bridge_provider_result_to_llm_lineage,
    create_provider_request_for_problem,
    load_provider_request,
    persist_provider_request,
    verify_llm_call_artifacts,
)
from leanfaith.schemas.enums import LLMCallStatus, NLTrust, ParseStatus
from leanfaith.schemas.ids import CONTEXT_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.llm import check_llm_call_attempt_lineage
from leanfaith.schemas.nl_lean import ProblemPoolRecord, make_problem_record_id

_TEMPLATE_HASH = "a" * 64
_UTC = datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC)


def _problem(*, private: bool = False, external: bool = True) -> ProblemPoolRecord:
    reference_id = make_id(THEOREM_PREFIX, {"provider": "reference"})
    fields: dict[str, object] = {
        "problem_id": "provider-problem",
        "problem_group": "nl-problem:provider-problem",
        "source": "public_fixture",
        "source_revision": "v1",
        "source_split": "smoke",
        "source_record_id": "row-1",
        "source_record_content_hash": "1" * 64,
        "nl_statement": "For every n, prove n equals itself.",
        "nl_trust": NLTrust.TRUSTED,
        "nl_source_link": "repo://provider-fixture",
        "context_id": make_id(CONTEXT_PREFIX, {"provider": "context"}),
        "import_header_artifact": "examples/provider-fixture.json",
        "import_header_hash": sha256_hex(b"import Lean"),
        "reference_theorem_ids": (reference_id,),
        "private_source_content": private,
        "external_provider_eligible": external and not private,
        "release_eligible": False,
        "eligibility": "eligible",
        "denylist_checked": True,
    }
    return ProblemPoolRecord(
        problem_record_id=make_problem_record_id(
            source=str(fields["source"]),
            source_revision=str(fields["source_revision"]),
            source_split=str(fields["source_split"]),
            source_record_id=str(fields["source_record_id"]),
            problem_id=str(fields["problem_id"]),
        ),
        **fields,
    )


def _identity(
    *,
    transport: str = "fixture",
    provider: str = "fixture_provider",
    model: str = "fixture/model",
    revision: str = "revision-1",
) -> ProviderIdentity:
    return ProviderIdentity(
        provider=provider,
        model=model,
        revision=revision,
        transport=transport,  # type: ignore[arg-type]
    )


def _request(
    identity: ProviderIdentity,
    *,
    rendered_prompt: str = "Formalize: 1 + 1 = 2",
    decoding: dict[str, str | int | float | bool | None] | None = None,
    private_source_content: bool = False,
    attempt_index: int = 0,
) -> ProviderRequest:
    return ProviderRequest.create(
        identity=identity,
        prompt_template_hash=_TEMPLATE_HASH,
        rendered_prompt=rendered_prompt,
        decoding=decoding or {"temperature": 0.0, "max_tokens": 128},
        input_ids=("problem:fixture",),
        private_source_content=private_source_content,
        attempt_index=attempt_index,
    )


def test_request_identity_binds_provider_model_revision_prompt_and_decoding() -> None:
    identity = _identity()
    original = _request(identity)
    repeated = _request(identity)

    assert original == repeated
    assert original.request_hash == repeated.request_hash
    variants = (
        _request(_identity(provider="different_provider")),
        _request(_identity(model="different/model")),
        _request(_identity(revision="revision-2")),
        ProviderRequest.create(
            identity=identity,
            prompt_template_hash="b" * 64,
            rendered_prompt=original.rendered_prompt,
            decoding=original.decoding,
            input_ids=original.input_ids,
        ),
        _request(identity, rendered_prompt="Formalize: 2 + 2 = 4"),
        _request(identity, decoding={"temperature": 0.5, "max_tokens": 128}),
    )
    assert all(item.request_hash != original.request_hash for item in variants)

    retry = _request(identity, attempt_index=1)
    assert retry.request_hash == original.request_hash
    assert retry.attempt_id != original.attempt_id
    assert retry.is_retry is True
    assert original.is_retry is False


def test_request_accepts_and_hashes_multi_token_decoding_values() -> None:
    identity = _identity()
    request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=_TEMPLATE_HASH,
        rendered_prompt="Formalize: 1 + 1 = 2",
        decoding={
            "eos_token_id": (151645, 151643),
            "pad_token_id": 151643,
        },
        input_ids=("problem:fixture",),
    )

    assert request.decoding["eos_token_id"] == (151645, 151643)
    assert ProviderRequest.model_validate_json(request.model_dump_json()) == request


def test_request_rejects_modified_binding_fields() -> None:
    request = _request(_identity())
    document = request.model_dump(mode="python")
    document["decoding_hash"] = "f" * 64

    with pytest.raises(ValidationError, match="decoding_hash"):
        ProviderRequest.model_validate(document)


def test_fixture_provider_persists_canonical_immutable_raw_response(
    tmp_path: Path,
) -> None:
    identity = _identity()
    request = _request(identity)
    provider = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "theorem generated : 1 + 1 = 2 := by decide"},
    )

    assert isinstance(provider, GenerationProvider)
    first = provider.generate(request)
    first_bytes = first.raw_response_path.read_bytes()
    second = provider.generate(request)

    assert first == second
    assert first.replayed is False
    assert first.response.request_hash == request.request_hash
    assert first.response.attempt_id == request.attempt_id
    assert first.response.prompt_template_hash == request.prompt_template_hash
    assert first.response.prompt_render_hash == request.prompt_render_hash
    assert first.response.decoding_hash == request.decoding_hash
    assert first.raw_response_sha256 == hash_file(first.raw_response_path)
    assert first_bytes.endswith(b"\n")
    assert first_bytes == (canonical_json_bytes(first.response.model_dump(mode="json")) + b"\n")


def test_fixture_attempt_paths_and_retry_identity_are_distinct(tmp_path: Path) -> None:
    identity = _identity()
    initial = _request(identity)
    retry = _request(identity, attempt_index=1)
    provider = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={initial.request_hash: "same deterministic output"},
    )

    initial_result = provider.generate(initial)
    retry_result = provider.generate(retry)

    assert initial_result.raw_response_path != retry_result.raw_response_path
    assert initial_result.response.request_hash == retry_result.response.request_hash
    assert retry_result.response.is_retry is True
    assert retry_result.response.attempt_index == 1


def test_fixture_missing_response_and_immutable_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    identity = _identity()
    request = _request(identity)
    empty = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={},
    )
    with pytest.raises(FixtureResponseMissingError, match="no fixture response"):
        empty.generate(request)

    first = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "first"},
    )
    first.generate(request)
    conflicting = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "second"},
    )
    with pytest.raises(RawResponseConflictError, match="different bytes"):
        conflicting.generate(request)


def test_replay_provider_reads_bound_response_without_network(tmp_path: Path) -> None:
    fixture_identity = _identity()
    request = _request(fixture_identity)
    fixture = DeterministicFixtureProvider(
        identity=fixture_identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "replay me"},
    )
    written = fixture.generate(request)
    replay = ReplayProvider(
        identity=_identity(transport="replay"),
        raw_response_root=tmp_path / "raw",
    )

    result = replay.generate(request)

    assert isinstance(replay, GenerationProvider)
    assert result.replayed is True
    assert result.response == written.response
    assert result.raw_response_sha256 == written.raw_response_sha256


def test_replay_rejects_missing_tampered_and_wrongly_bound_artifacts(
    tmp_path: Path,
) -> None:
    identity = _identity()
    request = _request(identity)
    root = tmp_path / "raw"
    replay = ReplayProvider(
        identity=_identity(transport="replay"),
        raw_response_root=root,
    )
    with pytest.raises(ReplayArtifactError, match="missing"):
        replay.generate(request)

    fixture = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=root,
        responses={request.request_hash: "original"},
    )
    written = fixture.generate(request)
    written.raw_response_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ReplayArtifactError, match="invalid raw response"):
        replay.generate(request)


def test_external_provider_is_disabled_and_rejects_private_content_first() -> None:
    identity = _identity(transport="external_disabled", provider="external")
    provider = DisabledExternalProvider(identity)

    with pytest.raises(ProviderDisabledError, match="disabled"):
        provider.generate(_request(identity))
    with pytest.raises(PrivateContentTransmissionError, match="private-source"):
        provider.generate(_request(identity, private_source_content=True))


def test_every_provider_rejects_identity_mismatch(tmp_path: Path) -> None:
    identity = _identity()
    request = _request(identity)
    mismatched = _identity(provider="other")

    fixture = DeterministicFixtureProvider(
        identity=mismatched,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "unused"},
    )
    with pytest.raises(ProviderIdentityMismatchError, match="does not match"):
        fixture.generate(request)

    replay = ReplayProvider(
        identity=_identity(transport="replay", provider="other"),
        raw_response_root=tmp_path / "raw",
    )
    with pytest.raises(ProviderIdentityMismatchError, match="does not match"):
        replay.generate(request)

    external = DisabledExternalProvider(_identity(transport="external_disabled", provider="other"))
    with pytest.raises(ProviderIdentityMismatchError, match="does not match"):
        external.generate(request)


def test_canonical_request_persistence_and_schema_v2_lineage_bridge(
    tmp_path: Path,
) -> None:
    problem = _problem()
    identity = _identity()
    request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=_TEMPLATE_HASH,
        rendered_prompt="Render the public fixture.",
        decoding={"temperature": 0.0, "seed": 0},
        input_ids=(problem.problem_record_id,),
        private_source_content=False,
    )
    request_path = tmp_path / "requests" / "request.json"
    request_sha256 = persist_provider_request(request, request_path)
    assert load_provider_request(request_path) == request
    assert request_sha256 == hash_file(request_path)

    provider = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "```lean4\ntheorem generated : True\n```\n"},
    )
    result = provider.generate(request)
    lineage = bridge_provider_result_to_llm_lineage(
        request=request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        problem=problem,
        provider_slot="offline_fixture",
        model_family="fixture-family",
        prompt_template_id="direct_autoformalize",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.PARSED,
        parsed_statement="theorem generated : True",
        started_at=_UTC,
        completed_at=_UTC + datetime.timedelta(milliseconds=7),
        metadata={"artifact_class": "smoke"},
    )

    assert lineage.call.schema_version == 2
    assert lineage.call.provider_request_hash == request.request_hash
    assert lineage.call.request_artifact_sha256 == request_sha256
    assert lineage.call.raw_response_sha256 == result.raw_response_sha256
    assert lineage.call.private_source_content is problem.private_source_content
    assert lineage.call.supervision_eligible is False
    assert lineage.attempt.provider_attempt_id == request.attempt_id
    assert lineage.attempt.raw_response_sha256 == result.raw_response_sha256
    assert check_llm_call_attempt_lineage(lineage.call, (lineage.attempt,)) == []
    assert (
        verify_llm_call_artifacts(
            call=lineage.call,
            problem=problem,
            artifact_root=tmp_path,
        )
        == result.response
    )


def test_problem_bound_request_enforces_privacy_before_provider_invocation() -> None:
    fixture_request = create_provider_request_for_problem(
        identity=_identity(),
        problem=_problem(private=True, external=False),
        prompt_template_hash=_TEMPLATE_HASH,
        rendered_prompt="Private local fixture.",
        decoding={"temperature": 0.0},
    )
    assert fixture_request.private_source_content is True

    with pytest.raises(PrivateContentTransmissionError, match="external provider request"):
        create_provider_request_for_problem(
            identity=_identity(transport="external_disabled"),
            problem=_problem(private=True, external=False),
            prompt_template_hash=_TEMPLATE_HASH,
            rendered_prompt="Must never be transmitted.",
            decoding={"temperature": 0.0},
        )
    with pytest.raises(PrivateContentTransmissionError, match="external provider request"):
        create_provider_request_for_problem(
            identity=_identity(transport="external_disabled"),
            problem=_problem(private=False, external=False),
            prompt_template_hash=_TEMPLATE_HASH,
            rendered_prompt="Not authorized for external transmission.",
            decoding={"temperature": 0.0},
        )


def test_single_attempt_empty_response_is_exhausted_not_completed(tmp_path: Path) -> None:
    problem = _problem()
    identity = _identity()
    request = create_provider_request_for_problem(
        identity=identity,
        problem=problem,
        prompt_template_hash=_TEMPLATE_HASH,
        rendered_prompt="Return one theorem.",
        decoding={"temperature": 0.0},
    )
    request_path = tmp_path / "request.json"
    persist_provider_request(request, request_path)
    result = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: ""},
    ).generate(request)
    lineage = bridge_provider_result_to_llm_lineage(
        request=request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        problem=problem,
        provider_slot="offline_fixture",
        model_family="fixture-family",
        prompt_template_id="direct_autoformalize",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.EMPTY,
        parsed_statement=None,
        started_at=_UTC,
        completed_at=_UTC,
    )
    assert lineage.call.terminal_status is LLMCallStatus.EXHAUSTED
    assert check_llm_call_attempt_lineage(lineage.call, (lineage.attempt,)) == []


def test_lineage_bridge_reloads_artifacts_and_enforces_privacy(tmp_path: Path) -> None:
    public_problem = _problem()
    identity = _identity()
    request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=_TEMPLATE_HASH,
        rendered_prompt="Render the public fixture.",
        decoding={"temperature": 0.0},
        input_ids=(public_problem.problem_record_id,),
        private_source_content=False,
    )
    request_path = tmp_path / "request.json"
    persist_provider_request(request, request_path)
    result = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={request.request_hash: "not parsed"},
    ).generate(request)

    request_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ReplayArtifactError, match="invalid provider request"):
        bridge_provider_result_to_llm_lineage(
            request=request,
            result=result,
            request_artifact_path=request_path,
            artifact_root=tmp_path,
            problem=public_problem,
            provider_slot="offline_fixture",
            model_family="fixture-family",
            prompt_template_id="direct_autoformalize",
            prompt_template_version="v1",
            execution_mode="replay",
            parse_status=ParseStatus.PARSE_FAILED,
            parsed_statement=None,
            started_at=_UTC,
            completed_at=_UTC,
        )

    request_path.unlink()
    persist_provider_request(request, request_path)
    with pytest.raises(PrivateContentTransmissionError, match="must exactly match"):
        bridge_provider_result_to_llm_lineage(
            request=request,
            result=result,
            request_artifact_path=request_path,
            artifact_root=tmp_path,
            problem=_problem(private=True, external=False),
            provider_slot="offline_fixture",
            model_family="fixture-family",
            prompt_template_id="direct_autoformalize",
            prompt_template_version="v1",
            execution_mode="replay",
            parse_status=ParseStatus.PARSE_FAILED,
            parsed_statement=None,
            started_at=_UTC,
            completed_at=_UTC,
        )
