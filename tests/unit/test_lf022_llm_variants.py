from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from leanfaith.generation.llm_variants import (
    DEFAULT_PROPOSER_TEMPLATE,
    PublicLeanVariantSource,
    VariantOutputErrorCode,
    VariantOutputParseError,
    VariantPromptError,
    VariantPromptErrorCode,
    VariantPromptRequest,
    materialize_provisional_variants,
    materialize_verified_provisional_variants,
    parse_variant_proposer_output,
    render_variant_proposer_prompt,
    variant_provider_input_ids,
)
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    ProviderError,
    ProviderIdentity,
    ProviderRequest,
    bridge_provider_result_to_generic_llm_lineage,
    persist_provider_request,
)
from leanfaith.schemas import IntendedRelation, LLMRole, ParseStatus, QualityTier, ValidationStatus


def _source(**overrides: object) -> PublicLeanVariantSource:
    values: dict[str, object] = {
        "source_theorem_id": "thm:" + "1" * 64,
        "source_representation_id": "repr:" + "2" * 64,
        "context_id": "ctx:" + "3" * 64,
        "imports": ("Mathlib",),
        "source_statement": "theorem source_identity (n : Nat) : n = n",
        "optional_natural_language": "Every natural number equals itself.",
        "source_id": "mathlib",
        "source_revision": "lean-4.31-compatible",
        "source_license": "Apache-2.0",
        "source_is_public": True,
        "external_transmission_allowed": True,
        "denylist_checked": True,
        "denylist_hits": (),
    }
    values.update(overrides)
    return PublicLeanVariantSource.model_validate(values)


def _request(**overrides: object) -> VariantPromptRequest:
    values: dict[str, object] = {
        "request_id": "lf022-fixture-1",
        "source": _source(),
        "proposal_count": 1,
        "requested_relations": (IntendedRelation.NEAR_MISS,),
        "requested_error_types": ("E17",),
        "requested_sci_categories": (),
        "generation_distribution": "G_open",
    }
    values.update(overrides)
    return VariantPromptRequest.model_validate(values)


def _raw(
    *,
    candidate: str = "theorem changed_bound (n : Nat) : n ≤ n + 1",
    relation: str = "near_miss",
    errors: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "variants": [
                {
                    "candidate_lean": candidate,
                    "intended_relation": relation,
                    "intended_error_types": errors if errors is not None else ["E17"],
                    "edit_summary": "Changed the bound.",
                    "confidence": 0.7,
                    "assumptions": [],
                    "potential_ambiguity": None,
                }
            ]
        },
        ensure_ascii=False,
    )


def test_proposer_prompt_is_canonical_and_hash_bound() -> None:
    rendered = render_variant_proposer_prompt(_request())
    assert rendered.template_id == "lean_variant"
    assert rendered.template_version == "v1"
    assert rendered.template_sha256 in rendered.text
    assert "{{" not in rendered.text
    assert rendered.render_sha256
    assert DEFAULT_PROPOSER_TEMPLATE.is_file()
    input_payload = json.loads(rendered.text.rstrip().rsplit("\n", maxsplit=1)[-1])
    assert input_payload["source_statement_id"] == "thm:" + "1" * 64
    assert input_payload["generation_distribution"] == "G_open"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"source_is_public": False}, VariantPromptErrorCode.PRIVATE_SOURCE),
        (
            {"external_transmission_allowed": False},
            VariantPromptErrorCode.EXTERNAL_TRANSMISSION_FORBIDDEN,
        ),
        ({"denylist_checked": False}, VariantPromptErrorCode.DENYLIST_NOT_CLEARED),
        ({"denylist_hits": ("FormalRx-Test",)}, VariantPromptErrorCode.DENYLIST_NOT_CLEARED),
    ],
)
def test_proposer_prompt_rejects_ineligible_source(
    overrides: dict[str, object],
    code: VariantPromptErrorCode,
) -> None:
    with pytest.raises(VariantPromptError) as caught:
        render_variant_proposer_prompt(_request(source=_source(**overrides)))
    assert caught.value.code is code


def test_proposer_parser_accepts_strict_json_and_materializes_only_provisional() -> None:
    request = _request()
    batch = parse_variant_proposer_output(_raw())
    variants = materialize_provisional_variants(
        batch=batch,
        request=request,
        proposer_family="moonshot_kimi_k2",
        proposer_model="moonshotai/Kimi-K2.7-Code",
        llm_call_id="call:" + "4" * 64,
        generation_config_hash="5" * 64,
        prompt_artifact="data/raw/lf022/prompt.json",
        raw_output_artifact="data/raw/lf022/response.json",
        seed=7,
    )
    assert len(variants) == 1
    variant = variants[0]
    assert variant.intended_relation is IntendedRelation.NEAR_MISS
    assert variant.quality_tier is QualityTier.PROVISIONAL
    assert variant.validation_status is ValidationStatus.UNVALIDATED
    assert variant.metadata["llm_call_id"] == "call:" + "4" * 64
    assert not hasattr(variant, "same_claim")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", VariantOutputErrorCode.EMPTY_OUTPUT),
        ("```json\n{}\n```", VariantOutputErrorCode.INVALID_JSON),
        ('{"variants":[]}', VariantOutputErrorCode.INVALID_SCHEMA),
        (
            '{"variants":[],"variants":[]}',
            VariantOutputErrorCode.INVALID_JSON,
        ),
        (
            '{"variants":[{"candidate_lean":"theorem t : True","intended_relation":'
            '"equivalent","intended_error_types":[],"edit_summary":"x","confidence":NaN,'
            '"assumptions":[],"potential_ambiguity":null}]}',
            VariantOutputErrorCode.INVALID_JSON,
        ),
        (_raw(relation="incomparable"), VariantOutputErrorCode.INVALID_SCHEMA),
        (_raw(errors=["E31"]), VariantOutputErrorCode.INVALID_SCHEMA),
        (
            _raw(candidate="theorem t : True := by trivial"),
            VariantOutputErrorCode.PROOF_BEARING_CANDIDATE,
        ),
        (
            _raw(candidate="def t : Nat := 1"),
            VariantOutputErrorCode.UNSUPPORTED_DECLARATION,
        ),
    ],
)
def test_proposer_parser_fails_closed(raw: str, code: VariantOutputErrorCode) -> None:
    with pytest.raises(VariantOutputParseError) as caught:
        parse_variant_proposer_output(raw)
    assert caught.value.code is code


def test_proposer_parser_rejects_duplicate_normalized_candidates() -> None:
    payload = json.loads(_raw())
    duplicate = dict(payload["variants"][0])
    duplicate["candidate_lean"] = " theorem   changed_bound (n : Nat) : n ≤ n + 1 "
    payload["variants"].append(duplicate)
    with pytest.raises(VariantOutputParseError) as caught:
        parse_variant_proposer_output(json.dumps(payload))
    assert caught.value.code is VariantOutputErrorCode.DUPLICATE_CANDIDATE


def test_materializer_rejects_proposal_count_and_relation_drift() -> None:
    with pytest.raises(VariantOutputParseError) as count_error:
        materialize_provisional_variants(
            batch=parse_variant_proposer_output(_raw()),
            request=_request(proposal_count=2),
            proposer_family="family",
            proposer_model="model",
            llm_call_id="call:" + "4" * 64,
            generation_config_hash="5" * 64,
            prompt_artifact="prompt.json",
            raw_output_artifact="raw.json",
            seed=None,
        )
    assert count_error.value.code is VariantOutputErrorCode.REQUEST_MISMATCH

    with pytest.raises(VariantOutputParseError) as relation_error:
        materialize_provisional_variants(
            batch=parse_variant_proposer_output(_raw(relation="equivalent", errors=[])),
            request=_request(),
            proposer_family="family",
            proposer_model="model",
            llm_call_id="call:" + "4" * 64,
            generation_config_hash="5" * 64,
            prompt_artifact="prompt.json",
            raw_output_artifact="raw.json",
            seed=None,
        )
    assert relation_error.value.code is VariantOutputErrorCode.REQUEST_MISMATCH


def test_sci_request_records_requested_not_validated_provenance() -> None:
    request = _request(
        generation_distribution="G_sci",
        requested_sci_categories=("S2.5",),
    )
    variant = materialize_provisional_variants(
        batch=parse_variant_proposer_output(_raw()),
        request=request,
        proposer_family="qwen3",
        proposer_model="Qwen/Qwen3.6-35B-A3B",
        llm_call_id="call:" + "6" * 64,
        generation_config_hash="7" * 64,
        prompt_artifact="prompt.json",
        raw_output_artifact="raw.json",
        seed=None,
    )[0]
    assert variant.formalrx_sci_requested == "S2.5"
    assert variant.formalrx_sci_validated is None
    assert variant.formalrx_sci_validation_status == "pending"
    assert variant.formalrx_sci_proposer_family == "qwen3"
    assert variant.formalrx_sci_validator_family is None


def test_sci_request_rejects_multiple_categories_that_cannot_be_persisted() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _request(
            generation_distribution="G_sci",
            requested_sci_categories=("S2.5", "S2.6"),
        )


def test_verified_materializer_rejects_unbound_and_accepts_hash_bound_lineage(
    tmp_path: Path,
) -> None:
    request = _request()
    rendered = render_variant_proposer_prompt(request)
    identity = ProviderIdentity(
        provider="fixture",
        model="fixture/proposer",
        revision="fixture-revision",
        transport="fixture",
    )
    provider_request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=rendered.template_sha256,
        rendered_prompt=rendered.text,
        decoding={"temperature": 0.0, "seed": 7},
        input_ids=variant_provider_input_ids(request),
        private_source_content=False,
    )
    request_path = tmp_path / "requests" / "request.json"
    persist_provider_request(provider_request, request_path)
    result = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={provider_request.request_hash: _raw()},
    ).generate(provider_request)
    parsed = parse_variant_proposer_output(_raw())
    config_hash = "5" * 64
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=provider_request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        role=LLMRole.PROPOSER,
        provider_slot="proposer_fixture",
        model_family="fixture_family",
        prompt_template_id="lean_variant",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.PARSED,
        parsed_output=parsed.model_dump(mode="json"),
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC),
        completed_at=datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC),
        supervision_eligible=False,
        metadata={"generation_config_hash": config_hash},
    )
    variants = materialize_verified_provisional_variants(
        request=request,
        call=lineage.call,
        artifact_root=tmp_path,
        generation_config_hash=config_hash,
    )
    assert variants[0].raw_output_artifact == lineage.call.raw_output_artifact
    assert variants[0].metadata["llm_call_id"] == lineage.call.call_id

    forged = lineage.call.model_copy(update={"metadata": {"generation_config_hash": "6" * 64}})
    with pytest.raises(ProviderError, match="generation config"):
        materialize_verified_provisional_variants(
            request=request,
            call=forged,
            artifact_root=tmp_path,
            generation_config_hash=config_hash,
        )
