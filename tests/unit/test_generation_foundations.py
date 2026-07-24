"""LF-021 strict config and persistent lineage foundations."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config import ConfigError, load_config, load_yaml_mapping
from leanfaith.config.hashing import hash_file
from leanfaith.generation.config import (
    ProblemPoolConfig,
    RealOutputsConfig,
    load_generation_foundation_configs,
)
from leanfaith.schemas.enums import (
    LLMAttemptStatus,
    LLMCallStatus,
    LLMRole,
    NLTrust,
    ParseStatus,
)
from leanfaith.schemas.ids import CONTEXT_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    check_llm_call_attempt_lineage,
    make_llm_attempt_id,
    make_llm_call_id,
)
from leanfaith.schemas.nl_lean import (
    NLPLeanRecord,
    ProblemPoolRecord,
    check_nl_lean_problem_link,
    make_problem_record_id,
)

ROOT = Path(__file__).resolve().parents[2]
UTC_NOW = datetime.datetime(2026, 7, 23, 12, 0, tzinfo=datetime.UTC)
UTC_DONE = UTC_NOW + datetime.timedelta(milliseconds=50)
CONTEXT_ID = make_id(CONTEXT_PREFIX, {"generation": "fixture"})
REFERENCE_ID = make_id(THEOREM_PREFIX, {"generation": "reference"})


def _problem(**overrides: object) -> ProblemPoolRecord:
    fields: dict[str, object] = {
        "problem_id": "source-problem-1",
        "problem_group": "grp:source-problem-1",
        "source": "public_fixture",
        "source_revision": "fixture-revision-1",
        "source_split": "train",
        "source_record_id": "row-1",
        "source_record_content_hash": "1" * 64,
        "nl_statement": "For every natural number n, n + 0 = n.",
        "nl_trust": NLTrust.TRUSTED,
        "nl_source_link": "fixture://public/row-1",
        "context_id": CONTEXT_ID,
        "import_header_artifact": "artifacts/generation/headers/row-1.lean",
        "import_header_hash": "2" * 64,
        "reference_theorem_ids": (REFERENCE_ID,),
        "private_source_content": False,
        "external_provider_eligible": False,
        "release_eligible": True,
        "eligibility": "eligible",
        "denylist_checked": True,
    }
    fields.update(overrides)
    fields.setdefault(
        "problem_record_id",
        make_problem_record_id(
            source=str(fields["source"]),
            source_revision=str(fields["source_revision"]),
            source_split=str(fields["source_split"]),
            source_record_id=str(fields["source_record_id"]),
            problem_id=str(fields["problem_id"]),
        ),
    )
    return ProblemPoolRecord.model_validate(fields)


def _call_payload(problem: ProblemPoolRecord) -> dict[str, object]:
    decoding = {"temperature": 0.0, "seed": 7}
    call_id = make_llm_call_id(
        provider="fixture",
        provider_slot="offline_fixture",
        model="fixture-model",
        model_family="fixture-family",
        model_revision="fixture-model-r1",
        role=LLMRole.AUTOFORMALIZER,
        problem_record_id=problem.problem_record_id,
        prompt_template_hash="3" * 64,
        prompt_render_hash="4" * 64,
        input_ids=(problem.problem_record_id,),
        decoding=decoding,
    )
    attempt_id = make_llm_attempt_id(call_id, 0)
    return {
        "schema_version": 2,
        "call_id": call_id,
        "provider": "fixture",
        "provider_slot": "offline_fixture",
        "model": "fixture-model",
        "model_family": "fixture-family",
        "role": LLMRole.AUTOFORMALIZER,
        "model_revision": "fixture-model-r1",
        "request_date": UTC_NOW,
        "started_at": UTC_NOW,
        "completed_at": UTC_DONE,
        "execution_mode": "replay",
        "prompt_template_id": "direct_autoformalization",
        "prompt_template_version": "v1",
        "prompt_template_hash": "3" * 64,
        "prompt_render_hash": "4" * 64,
        "request_artifact": "data/raw/real_outputs/call/request.json",
        "input_ids": (problem.problem_record_id,),
        "decoding": decoding,
        "raw_output_artifact": "data/raw/real_outputs/call/attempt_0.json",
        "parsed_output": {"lean_statement": "theorem generated : True := by trivial"},
        "parse_status": ParseStatus.PARSED,
        "retry_count": 0,
        "tokens": {"input": 12, "output": 9},
        "supervision_eligible": True,
        "private_source_content": False,
        "denylist_checked": True,
        "problem_record_id": problem.problem_record_id,
        "problem_id": problem.problem_id,
        "problem_group": problem.problem_group,
        "terminal_status": LLMCallStatus.COMPLETED,
        "attempt_ids": (attempt_id,),
        "latency_ms": 50,
        "provider_request_hash": "5" * 64,
        "request_artifact_sha256": "6" * 64,
        "raw_response_sha256": "7" * 64,
    }


def _attempt(call_id: str, **overrides: object) -> LLMAttemptRecord:
    fields: dict[str, object] = {
        "attempt_id": make_llm_attempt_id(call_id, 0),
        "call_id": call_id,
        "attempt_index": 0,
        "execution_mode": "replay",
        "started_at": UTC_NOW,
        "completed_at": UTC_DONE,
        "request_artifact": "data/raw/real_outputs/call/request.json",
        "raw_response_artifact": "data/raw/real_outputs/call/attempt_0.json",
        "status": LLMAttemptStatus.RESPONSE_RECEIVED,
        "retryable": False,
        "latency_ms": 50,
        "tokens": {"input": 12, "output": 9},
        "provider_request_hash": "5" * 64,
        "provider_attempt_id": "provider-attempt:" + "8" * 64,
        "request_artifact_sha256": "6" * 64,
        "raw_response_sha256": "7" * 64,
    }
    fields.update(overrides)
    return LLMAttemptRecord.model_validate(fields)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_ready_foundation(tmp_path: Path) -> dict[str, Path]:
    source_path = tmp_path / "configs/sources/public_fixture.yaml"
    authorization = {
        "source_revision": "source-r1",
        "license_id": "CC-BY-4.0",
        "private_source": False,
        "external_transmission": True,
        "release_eligible": True,
    }
    _write_json(
        source_path,
        {
            "source": "public_fixture",
            "probe": {"resolved_revision": "source-r1"},
            "lf021_authorization": authorization,
        },
    )
    profile_path = tmp_path / "configs/sources/public_replication.yaml"
    _write_json(profile_path, {"profile": "fixture"})
    benchmark_path = tmp_path / "data/benchmarks/manifests/representation_signatures_v1.json"
    _write_json(benchmark_path, {"manifest": "fixture"})
    prompt_path = tmp_path / "prompts/direct_v1.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("{{PROMPT_TEMPLATE_SHA256}}\n{{PROBLEM_JSON}}\n", encoding="utf-8")

    problem_path = tmp_path / "configs/generation/problem_pool_v1.yaml"
    _write_json(
        problem_path,
        {
            "schema_version": 1,
            "config_id": "problem_pool_v1",
            "status": "ready",
            "selection_seed": "fixture",
            "sources": [
                {
                    "source": "public_fixture",
                    "source_config": "configs/sources/public_fixture.yaml",
                    "source_config_sha256": hash_file(source_path),
                    "authorization": authorization,
                    "enabled": True,
                    "private_source": False,
                    "external_provider_eligible": True,
                    "allowed_trust": ["trusted"],
                    "require_reference_theorem": True,
                }
            ],
            "active_benchmark_registry_manifest": (
                "data/benchmarks/manifests/representation_signatures_v1.json"
            ),
            "active_benchmark_registry_manifest_sha256": hash_file(benchmark_path),
            "benchmark_preflight_required": True,
            "normalized_nl_exact_dedup": True,
            "near_duplicate": {
                "status": "frozen",
                "method": "supplied_group_ids",
                "method_version": "v1",
                "threshold": 1.0,
            },
            "private_source_external_transmission": False,
            "public_replication_profile": "configs/sources/public_replication.yaml",
            "outputs": {
                "records": "data/parsed/real_outputs/problem_pool_v1.jsonl",
                "failures": "data/parsed/real_outputs/problem_pool_failures_v1.jsonl",
                "manifest": "data/parsed/real_outputs/problem_pool_manifest_v1.json",
                "coverage_report": "reports/generation_coverage.md",
            },
        },
    )

    provider_path = tmp_path / "configs/generation/providers.yaml"
    _write_json(
        provider_path,
        {
            "config_version": "providers_v1",
            "plan_sections": ["17.2"],
            "status": "ready",
            "rules": {"fixture": "frozen"},
            "api_key_env_convention": "LEANFAITH_API_KEY_<PROVIDER>",
            "slots": {
                "generator_a": {
                    "role": "fixture generator",
                    "family_constraint": "fixture family",
                    "exact_model": "fixture/model",
                    "revision": "fixture-revision",
                    "status": "enabled",
                    "family": "fixture-family",
                    "transport": "replay",
                    "slot_kind": "generator",
                    "allowed_sources": ["public_fixture"],
                }
            },
            "resolution_blockers": [],
        },
    )

    real_path = tmp_path / "configs/generation/real_outputs_v1.yaml"
    _write_json(
        real_path,
        {
            "schema_version": 1,
            "config_id": "real_outputs_v1",
            "status": "ready",
            "problem_pool_config": "configs/generation/problem_pool_v1.yaml",
            "provider_registry": "configs/generation/providers.yaml",
            "generation_enabled": True,
            "execution": {
                "external_provider_calls_enabled": False,
                "local_provider_calls_enabled": False,
                "replay_import_enabled": True,
                "allowed_provider_slots": ["generator_a"],
            },
            "prompt": {
                "status": "frozen",
                "template_artifact": "prompts/direct_v1.txt",
                "template_version": "v1",
                "template_sha256": hash_file(prompt_path),
                "parser_version": "direct_autoformalization_v1",
                "strict_machine_parse": True,
            },
            "retry": {
                "max_attempts": 1,
                "retry_statuses": [],
                "append_only_attempt_artifacts": True,
            },
            "family_policy": {
                "full_track_successful_families": 1,
                "supervision_eligible_families": 1,
                "heldout_families": 0,
                "heldout_family": None,
            },
            "safety": {
                "private_source_external_transmission": False,
                "failed_attempts_retained": True,
                "noncompiling_outputs_semantic_pool_eligible": False,
                "semantic_labels_created": False,
            },
            "outputs": {
                "raw": "data/raw/real_outputs/",
                "parsed": "data/parsed/real_outputs/",
                "validated": "data/real_outputs/validated/",
                "manifest": "data/real_outputs/validated/manifest_v1.json",
            },
        },
    )
    return {
        "source": source_path,
        "provider": provider_path,
        "prompt": prompt_path,
        "real": real_path,
    }


def test_checked_in_generation_configs_are_strict_and_fail_closed() -> None:
    loaded = load_generation_foundation_configs(ROOT)
    assert loaded.problem_pool.config.status == "disabled_until_phase_5_adr"
    assert not any(source.enabled for source in loaded.problem_pool.config.sources)
    real = loaded.real_outputs.config
    assert real.status == "disabled_until_phase_5_adr"
    assert real.generation_enabled is False
    assert real.execution.external_provider_calls_enabled is False
    assert real.execution.local_provider_calls_enabled is False
    assert real.execution.replay_import_enabled is False
    assert real.execution.allowed_provider_slots == ()
    assert real.family_policy.full_track_successful_families == 4
    assert len(loaded.problem_pool.config_hash) == 64
    assert len(loaded.real_outputs.config_hash) == 64


def test_problem_pool_config_rejects_enabled_source_while_disabled() -> None:
    payload = load_yaml_mapping(ROOT / "configs/generation/problem_pool_v1.yaml")
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["enabled"] = True
    with pytest.raises(ValidationError, match="enabled problem-pool"):
        ProblemPoolConfig.model_validate(payload)


def test_real_output_config_rejects_external_enablement_while_disabled() -> None:
    payload = load_yaml_mapping(ROOT / "configs/generation/real_outputs_v1.yaml")
    execution = payload["execution"]
    assert isinstance(execution, dict)
    execution["external_provider_calls_enabled"] = True
    with pytest.raises(ValidationError, match="fail closed"):
        RealOutputsConfig.model_validate(payload)


def test_generation_config_unknown_key_fails(tmp_path: Path) -> None:
    payload = (ROOT / "configs/generation/problem_pool_v1.yaml").read_text(encoding="utf-8")
    path = tmp_path / "problem_pool.yaml"
    path.write_text(payload + "\nunexpected: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unexpected"):
        load_config(path, ProblemPoolConfig)


def test_ready_generation_configs_are_cryptographically_cross_checked(
    tmp_path: Path,
) -> None:
    _write_ready_foundation(tmp_path)
    loaded = load_generation_foundation_configs(tmp_path)
    slot = loaded.provider_registry.config.slots["generator_a"]
    assert slot.exact_model == "fixture/model"
    assert slot.revision == "fixture-revision"
    assert slot.transport == "replay"
    assert loaded.problem_pool.config.sources[0].authorization is not None


def test_ready_generation_rejects_unknown_provider_slot(tmp_path: Path) -> None:
    paths = _write_ready_foundation(tmp_path)
    payload = json.loads(paths["real"].read_text(encoding="utf-8"))
    payload["execution"]["allowed_provider_slots"] = ["missing"]
    _write_json(paths["real"], payload)
    with pytest.raises(ValueError, match="unknown provider slots"):
        load_generation_foundation_configs(tmp_path)


def test_ready_generation_rejects_provider_transport_mismatch(tmp_path: Path) -> None:
    paths = _write_ready_foundation(tmp_path)
    payload = json.loads(paths["provider"].read_text(encoding="utf-8"))
    payload["slots"]["generator_a"]["transport"] = "local"
    _write_json(paths["provider"], payload)
    with pytest.raises(ValueError, match="disabled transport"):
        load_generation_foundation_configs(tmp_path)


def test_ready_generation_rejects_unpinned_or_unresolved_provider(
    tmp_path: Path,
) -> None:
    paths = _write_ready_foundation(tmp_path)
    payload = json.loads(paths["provider"].read_text(encoding="utf-8"))
    payload["slots"]["generator_a"]["revision"] = None
    payload["slots"]["generator_a"]["unresolved"] = "not actually ready"
    _write_json(paths["provider"], payload)
    with pytest.raises(ConfigError, match="revision"):
        load_generation_foundation_configs(tmp_path)


def test_ready_generation_rejects_tampered_prompt_or_source(tmp_path: Path) -> None:
    paths = _write_ready_foundation(tmp_path)
    paths["prompt"].write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="prompt SHA-256 mismatch"):
        load_generation_foundation_configs(tmp_path)

    paths = _write_ready_foundation(tmp_path)
    source = json.loads(paths["source"].read_text(encoding="utf-8"))
    source["lf021_authorization"]["license_id"] = "spoofed"
    _write_json(paths["source"], source)
    with pytest.raises(ValueError, match="source config SHA-256 mismatch"):
        load_generation_foundation_configs(tmp_path)


def test_ready_generation_binds_named_heldout_family(tmp_path: Path) -> None:
    paths = _write_ready_foundation(tmp_path)
    payload = json.loads(paths["real"].read_text(encoding="utf-8"))
    payload["family_policy"] = {
        "full_track_successful_families": 2,
        "supervision_eligible_families": 1,
        "heldout_families": 1,
        "heldout_family": "missing-family",
    }
    _write_json(paths["real"], payload)
    with pytest.raises(ValueError, match="held-out family"):
        load_generation_foundation_configs(tmp_path)


def test_problem_record_has_deterministic_identity_and_no_label() -> None:
    record = _problem()
    assert record.problem_record_id.startswith("problem:")
    assert record.eligibility == "eligible"
    dumped = record.model_dump(mode="json")
    assert "same_claim" not in dumped
    assert "relation" not in dumped


def test_private_problem_cannot_be_external_or_release_eligible() -> None:
    with pytest.raises(ValidationError, match="private-source"):
        _problem(
            private_source_content=True,
            external_provider_eligible=True,
            release_eligible=False,
        )
    with pytest.raises(ValidationError, match="private-source"):
        _problem(
            private_source_content=True,
            external_provider_eligible=False,
            release_eligible=True,
        )


def test_excluded_problem_requires_reason_and_cannot_be_external() -> None:
    excluded = _problem(
        eligibility="excluded",
        exclusion_reasons=("benchmark_overlap",),
        external_provider_eligible=False,
    )
    assert excluded.exclusion_reasons == ("benchmark_overlap",)
    with pytest.raises(ValidationError, match="exclusion reason"):
        _problem(eligibility="excluded", external_provider_eligible=False)


def test_schema_v1_llm_call_remains_readable() -> None:
    record = LLMCallRecord(
        call_id=make_id("call", {"legacy": 1}),
        provider="legacy",
        model="legacy-model",
        model_family="legacy-family",
        role=LLMRole.JUDGE,
        request_date=UTC_NOW,
        prompt_template_hash="5" * 64,
        prompt_render_hash="6" * 64,
        parse_status=ParseStatus.PARSED,
        parsed_output={"answer": "same_claim"},
        supervision_eligible=True,
        private_source_content=False,
        denylist_checked=True,
    )
    assert record.schema_version == 1
    assert record.attempt_ids == ()


def test_schema_v2_call_and_attempt_lineage_round_trip() -> None:
    problem = _problem()
    call = LLMCallRecord.model_validate(_call_payload(problem))
    attempt = _attempt(call.call_id)
    assert check_llm_call_attempt_lineage(call, (attempt,)) == []
    reloaded_call = LLMCallRecord.model_validate_json(call.model_dump_json())
    reloaded_attempt = LLMAttemptRecord.model_validate_json(attempt.model_dump_json())
    assert reloaded_call == call
    assert reloaded_attempt == attempt


def test_schema_v2_autoformalizer_call_requires_problem_lineage() -> None:
    payload = _call_payload(_problem())
    payload["problem_record_id"] = None
    with pytest.raises(ValidationError, match="problem_record_id"):
        LLMCallRecord.model_validate(payload)


def test_schema_v2_private_external_call_is_rejected_even_with_legacy_approval() -> None:
    payload = _call_payload(_problem())
    payload["execution_mode"] = "external"
    payload["private_source_content"] = True
    payload["external_api_approval"] = "legacy-approval-must-not-override-revision-4.1"
    with pytest.raises(ValidationError, match="forbids external-provider"):
        LLMCallRecord.model_validate(payload)


def test_attempt_ids_and_call_ids_are_verified() -> None:
    problem = _problem()
    payload = _call_payload(problem)
    payload["call_id"] = make_id("call", {"wrong": True})
    with pytest.raises(ValidationError, match="logical request payload"):
        LLMCallRecord.model_validate(payload)
    good_call = LLMCallRecord.model_validate(_call_payload(problem))
    with pytest.raises(ValidationError, match="attempt_id"):
        _attempt(good_call.call_id, attempt_id=make_id("call_attempt", {"wrong": True}))


def test_attempt_failure_requires_error_code() -> None:
    call = LLMCallRecord.model_validate(_call_payload(_problem()))
    with pytest.raises(ValidationError, match="requires error_code"):
        _attempt(
            call.call_id,
            status=LLMAttemptStatus.TIMEOUT,
            raw_response_artifact=None,
            retryable=False,
        )


def test_cross_record_checker_detects_attempt_call_mismatch() -> None:
    call = LLMCallRecord.model_validate(_call_payload(_problem()))
    other_call_id = make_id("call", {"other": True})
    other_attempt = _attempt(
        other_call_id,
        attempt_id=make_llm_attempt_id(other_call_id, 0),
    )
    violations = check_llm_call_attempt_lineage(call, (other_attempt,))
    assert "attempt_ids_do_not_match_ordered_attempts" in violations
    assert "attempt_call_id_mismatch" in violations


def test_nl_lean_schema_v2_links_problem_record() -> None:
    problem = _problem()
    record = NLPLeanRecord(
        schema_version=2,
        nl_lean_id=make_id("nllean", {"generated": 1}),
        problem_record_id=problem.problem_record_id,
        problem_id=problem.problem_id,
        problem_group=problem.problem_group,
        source=problem.source,
        source_revision=problem.source_revision,
        nl_statement=problem.nl_statement,
        nl_trust=problem.nl_trust,
        candidate_theorem_id=make_id(THEOREM_PREFIX, {"candidate": 1}),
        reference_theorem_ids=problem.reference_theorem_ids,
        split_group_ids=(problem.problem_group,),
    )
    assert check_nl_lean_problem_link(record, problem) == []


def test_nl_lean_schema_v1_remains_backward_compatible() -> None:
    record = NLPLeanRecord(
        nl_lean_id=make_id("nllean", {"legacy": 1}),
        problem_id="legacy",
        problem_group="grp:legacy",
        source="legacy",
        source_revision="v1",
        nl_statement="Legacy problem.",
        nl_trust=NLTrust.UNCERTAIN,
        candidate_theorem_id=make_id(THEOREM_PREFIX, {"legacy-candidate": 1}),
        split_group_ids=("grp:legacy",),
    )
    assert record.schema_version == 1
    assert record.problem_record_id is None
