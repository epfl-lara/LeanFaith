from __future__ import annotations

import base64
import concurrent.futures
import json
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

import leanfaith.sft2b.full_source_consumer as consumer_module
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.formalizer import FormalizerConfig, SlotSpec
from leanfaith.sft2b.full_source_consumer import (
    CORE_SHARD,
    FULL_PROFILE_NAME,
    PILOT_EVIDENCE_FILES,
    PILOT_OUTPUT_FILES,
    TAIL_SHARD,
    DetachedLaunch,
    FullSourceConsumerError,
    FullSourceConsumerSpec,
    PilotArtifactPin,
    ShardAuthorizationSpec,
    build_run_plan,
    inspect_detached_health,
    load_consumer_spec,
    main,
    run_integrated_executor,
    supervise_shard,
    verify_matched_500_gate,
)
from leanfaith.sft2b.schemas import (
    CandidateOrigin,
    CandidateRecord,
    CandidateSlot,
    CompileContextRecord,
    FormalizerAttempt,
    FormalizerInvalidAttemptView,
    FormalizerLineage,
    SourceProvenance,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.vllm_backend import (
    LoadedVllmBackend,
    StreamCompletion,
    VllmRequestMetrics,
    VllmRequestTerminal,
    inspect_vllm_sources_cache,
    load_vllm_spec,
    profile_endpoint,
    run_vllm_sources,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_V1_CONFIG = _REPO_ROOT / "configs/sft2b/reform_diverse_full_consumer_v1.json"
_V2_CONFIG = _REPO_ROOT / "configs/sft2b/reform_diverse_full_consumer_v2.json"
_MATCHED_CONFIG = _REPO_ROOT / "configs/sft2b/reform_32b_matched_500_pipeline_v1.json"
_VLLM_CONFIG = _REPO_ROOT / "configs/sft2b/reform_32b_vllm_v1.json"


def _source(index: int) -> SourceRecord:
    nl = f"Prove the standalone synthetic arithmetic statement numbered {index}."
    theorem_id = f"test:consumer-v2:{index}"
    revision = "1" * 40
    proposition = f"Nat.succ {index} > {index}"
    source_id = stable_id(
        "sft2b_source",
        {
            "reference_theorem_id": theorem_id,
            "nl_statement": nl,
            "source_revision": revision,
        },
    )
    return SourceRecord(
        source_id=source_id,
        nl_statement=nl,
        reference_theorem_id=theorem_id,
        reference_declaration_name=f"consumer_v2_{index}",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id=f"ctx:{index:064x}",
            render_compile_context_id=f"ctx:{index + 1000:064x}",
            project_id="test",
            project_revision="2" * 40,
            project_path="/tmp/test",
            lean_version="4.31.0",
            import_header="import Mathlib",
            source_context_path="context.json",
            source_context_sha256="3" * 64,
            helper_path="helper.lean",
            helper_sha256="4" * 64,
        ),
        provenance=SourceProvenance(
            source_family="new_audited",
            source_url="https://example.invalid/consumer-v2",
            source_revision=revision,
            source_path=f"rows/{index}.json",
            source_file_sha256="5" * 64,
            manifest_path="manifest.json",
            manifest_sha256="6" * 64,
            source_recipe_sha256="7" * 64,
            license_card_value="test-only",
            redistribution_note="private test fixture",
            nl_extraction_rule="fixture",
            trusted_reference_basis="fixture",
        ),
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=True,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))


def _pilot_input(root: Path) -> tuple[SourceRecord, ...]:
    root.mkdir()
    rows = tuple(_source(index) for index in range(500))
    _write_jsonl(root / "sources.jsonl", [row.model_dump(mode="json") for row in rows])
    source_config = json.loads(
        (_REPO_ROOT / "configs/sft2b/reform_matched_500_sources_v1.json").read_text()
    )
    placement_config = json.loads(
        (_REPO_ROOT / "configs/sft2b/reform_32b_placement_v1.json").read_text()
    )
    prompt_config = source_config["prompt"]
    tokenizer_config = source_config["tokenizer"]
    source_placement = source_config["placement"]
    prompt_template = (_REPO_ROOT / prompt_config["path"]).read_text()
    token_rows = [
        {
            "source_id": row.source_id,
            "prompt_sha256": sha256_hex(
                prompt_template.replace("{{NL}}", row.nl_statement).encode()
            ),
            "prompt_tokens": 967 if index == 499 else index % 966 + 1,
        }
        for index, row in enumerate(rows)
    ]
    _write_json(
        root / "prompt_token_counts.json",
        {
            "schema_version": "sft2b_prompt_token_counts_v1",
            "source_count": 500,
            "model_id": "GuoxinChen/ReForm-32B",
            "model_revision": "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8",
            "prompt_path": prompt_config["path"],
            "prompt_sha256": prompt_config["sha256"],
            "tokenizer_model_id": tokenizer_config["model_id"],
            "tokenizer_revision": tokenizer_config["revision"],
            "tokenizer_sha256": tokenizer_config["primary_sha256"],
            "maximum_prompt_tokens": 967,
            "max_new_tokens": 4096,
            "required_max_model_len": 5063,
            "rows": token_rows,
        },
    )
    _write_json(
        root / "source_manifest.json",
        {
            "source_config_path": "configs/sft2b/reform_matched_500_sources_v1.json",
            "source_config_sha256": hash_file(
                _REPO_ROOT / "configs/sft2b/reform_matched_500_sources_v1.json"
            ),
            "source_count": 500,
            "source_mix": {
                "selected": {
                    "library_docstring": 175,
                    "theorem_problem": 175,
                    "broader_public_synthetic": 100,
                    "specialist_high_difficulty": 50,
                }
            },
            "contamination": {
                "selected_exact_hits": 0,
                "selected_near_hits": 0,
                "selected_problem_identity_hits": 0,
                "selected_existing_301_hits": 0,
                "selected_internal_duplicates": 0,
                "shadowbench": "excluded_reference_free_test_only_126_rows",
            },
            "placement": {
                "path": source_placement["path"],
                "sha256": source_placement["sha256"],
                "model_id": source_placement["model_id"],
                "model_revision": "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8",
                "candidate_slots": source_placement["slots"],
                "decoding": placement_config["decoding"],
                "required_max_model_len": 5063,
            },
            "prompt": {**prompt_config, "observed_sha256": prompt_config["sha256"]},
            "tokenizer": tokenizer_config,
            "repr": {
                "freeze_commit": "176a783842c5a73b84413dfa8347670608b615d9",
                "spec_sha256": ("68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"),
                "implementation_set_sha256": (
                    "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
                ),
                "api_sha256": ("c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"),
            },
        },
    )
    covered = ("prompt_token_counts.json", "source_manifest.json", "sources.jsonl")
    (root / "SHA256SUMS").write_text(
        "".join(f"{hash_file(root / name)}  {name}\n" for name in covered),
        encoding="utf-8",
    )
    return rows


def _test_pipeline_config(tmp_path: Path, input_root: Path) -> tuple[Path, dict[str, object]]:
    raw = json.loads(_MATCHED_CONFIG.read_text(encoding="utf-8"))
    raw["input"]["files"] = {
        path.name: hash_file(path) for path in input_root.iterdir() if path.is_file()
    }
    path = tmp_path / "matched_pipeline.json"
    _write_json(path, raw)
    raw["_fixture_pipeline_path"] = str(path)
    return path, raw


def _pilot_evidence_bundle(
    root: Path,
    *,
    rows: tuple[SourceRecord, ...],
    pipeline_raw: dict[str, object],
    pipeline_hash: str,
    corrupt_first_request_key: bool = False,
    invalid_ordinals: frozenset[int] = frozenset(),
) -> str:
    root.mkdir()
    model = pipeline_raw["model"]
    generation = pipeline_raw["generation"]
    assert isinstance(model, dict) and isinstance(generation, dict)
    placement = json.loads((_REPO_ROOT / "configs/sft2b/reform_32b_placement_v1.json").read_text())
    decoding = placement["decoding"]
    decoding_sha = hash_canonical(decoding)
    prompt_template = (_REPO_ROOT / "prompts/sft2b/reform_8b_generation_theorem_v1.md").read_text()
    slots = tuple(CandidateSlot(item) for item in generation["slots"])
    run_id = stable_id(
        "sft2b_vllm_run",
        {
            "profile_id": generation["profile_id"],
            "backend_config_sha256": pipeline_hash,
            "model_revision": model["revision"],
            "snapshot_binding_sha256": model["snapshot_binding_sha256"],
            "source_ids": tuple(row.source_id for row in rows),
            "slots": slots,
        },
    )
    attempts: list[object] = []
    candidates: list[object] = []
    invalid_attempts: list[object] = []
    metrics_rows: list[object] = []
    terminal_rows: list[object] = []
    raw_rows: list[object] = []
    journal_rows: list[object] = []
    request_keys: list[str] = []
    for source_index, source in enumerate(rows):
        prompt = prompt_template.replace("{{NL}}", source.nl_statement)
        prompt_sha = sha256_hex(prompt.encode())
        prompt_tokens = 967 if source_index == 499 else source_index % 966 + 1
        for slot_index, slot in enumerate(CandidateSlot):
            ordinal = source_index * 4 + slot_index
            request_key = hash_canonical(
                {
                    "schema_version": "sft2b_vllm_request_key_v1",
                    "profile_id": generation["profile_id"],
                    "backend_config_sha256": pipeline_hash,
                    "source_id": source.source_id,
                    "slot": slot,
                    "seed": slot_index,
                    "model_revision": model["revision"],
                    "snapshot_binding_sha256": model["snapshot_binding_sha256"],
                    "prompt_input_sha256": prompt_sha,
                    "prompt_template_sha256": placement["prompt_sha256"],
                    "decoding_sha256": decoding_sha,
                }
            )
            if corrupt_first_request_key and ordinal == 0:
                request_key = hash_canonical(
                    {"schema_version": "self_consistent_but_wrong_fixture_v1"}
                )
            attempt_id = stable_id(
                "sft2b_formalizer_attempt",
                {"request_key": request_key, "provider": "local_vllm_openai"},
            )
            lineage = FormalizerLineage(
                origin=CandidateOrigin.REFORM_32B,
                provider="local_vllm_openai",
                model_id=str(model["model_id"]),
                model_revision=str(model["revision"]),
                prompt_sha256=str(placement["prompt_sha256"]),
                decoding_sha256=decoding_sha,
                seed=slot_index,
                upstream_call_id=attempt_id,
                upstream_generation_config_sha256=pipeline_hash,
            )
            signature = "True"
            signature_hash = sha256_hex(signature.encode())
            candidate_id = stable_id(
                "sft2b_candidate",
                {
                    "source_id": source.source_id,
                    "slot": slot,
                    "signature_sha256": signature_hash,
                    "source_context_id": source.compile_context.source_context_id,
                    "lineage": lineage.model_dump(mode="json"),
                },
            )
            candidate = CandidateRecord(
                candidate_id=candidate_id,
                source_id=source.source_id,
                slot=slot,
                raw_proof_free_signature=signature,
                signature_sha256=signature_hash,
                source_context_id=source.compile_context.source_context_id,
                lineage=lineage,
            )
            is_invalid = ordinal in invalid_ordinals
            output = (
                "This response does not contain a theorem declaration."
                if is_invalid
                else f"```lean4\ntheorem fixture_{ordinal} : True := by sorry\n```"
            )
            _proposition, extraction_failure = consumer_module.extract_candidate(
                output,
                extraction_contract="final_theorem_signature_v1",
            )
            output_sha = sha256_hex(output.encode())
            response_id = f"cmpl-{ordinal}"
            response = (
                "data: "
                + json.dumps(
                    {
                        "id": response_id,
                        "model": model["served_model_name"],
                        "choices": [{"text": output, "finish_reason": "stop"}],
                    },
                    separators=(",", ":"),
                )
                + "\n\n"
                + "data: "
                + json.dumps(
                    {
                        "id": response_id,
                        "model": model["served_model_name"],
                        "choices": [],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": 5,
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            response_sha = sha256_hex(response)
            attempt = FormalizerAttempt(
                attempt_id=attempt_id,
                source_id=source.source_id,
                slot=slot,
                lineage=lineage,
                prompt_input_sha256=prompt_sha,
                raw_output_path=f"/scratch/fixture/{request_key}/raw_output.txt",
                raw_output_sha256=output_sha,
                extraction_status="invalid" if is_invalid else "candidate",
                candidate_id=None if is_invalid else candidate_id,
                failure_class="formalizer_output_contract" if is_invalid else None,
                failure_detail=str(extraction_failure) if is_invalid else None,
                elapsed_ms=50,
                prompt_tokens=prompt_tokens,
                completion_tokens=5,
                peak_cuda_allocated_bytes=0,
                peak_cuda_reserved_bytes=0,
                torch_version="test",
                transformers_version="test",
            )
            request_payload = {
                "model": model["served_model_name"],
                "prompt": prompt,
                "n": 1,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": decoding["max_new_tokens"],
                "temperature": decoding["temperature"],
                "top_k": decoding["top_k"],
                "top_p": decoding["top_p"],
                "repetition_penalty": decoding["repetition_penalty"],
                "seed": slot_index,
            }
            metrics = VllmRequestMetrics(
                request_key=request_key,
                attempt_id=attempt_id,
                source_id=source.source_id,
                slot=slot,
                profile_id=str(generation["profile_id"]),
                endpoint_url="http://127.0.0.1:8102/v1/completions",
                request_payload_sha256=hash_canonical(request_payload),
                response_id=response_id,
                response_request_id=request_key,
                raw_response_path=f"/scratch/fixture/{request_key}/raw_response.sse",
                raw_response_sha256=response_sha,
                raw_output_path=f"/scratch/fixture/{request_key}/raw_output.txt",
                raw_output_sha256=output_sha,
                elapsed_ms=50,
                time_to_first_token_ms=5,
                prompt_tokens=prompt_tokens,
                completion_tokens=5,
                finish_reason="stop",
                http_status=200,
                vllm_version="0.12.0",
            )
            request_artifact = {
                "schema_version": "sft2b_vllm_request_v1",
                "request_key": request_key,
                "attempt_id": attempt_id,
                "profile_id": generation["profile_id"],
                "source_id": source.source_id,
                "slot": slot,
                "seed": slot_index,
                "endpoint_url": "http://127.0.0.1:8102/v1/completions",
                "payload": request_payload,
            }
            started_artifact = {
                "schema_version": "sft2b_vllm_request_started_v1",
                "request_key": request_key,
            }
            artifact_sha256 = {
                "request": sha256_hex(canonical_json_bytes(request_artifact) + b"\n"),
                "request_started": sha256_hex(canonical_json_bytes(started_artifact) + b"\n"),
                "raw_response": response_sha,
                "raw_output": output_sha,
                "attempt": sha256_hex(
                    canonical_json_bytes(attempt.model_dump(mode="json")) + b"\n"
                ),
                "metrics": sha256_hex(
                    canonical_json_bytes(metrics.model_dump(mode="json")) + b"\n"
                ),
            }
            if not is_invalid:
                artifact_sha256["candidate"] = sha256_hex(
                    canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"
                )
            terminal = VllmRequestTerminal(
                request_key=request_key,
                attempt=attempt,
                candidate=None if is_invalid else candidate,
                metrics=metrics,
                artifact_sha256=artifact_sha256,
            )
            terminal_sha = sha256_hex(
                canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n"
            )
            attempts.append(attempt.model_dump(mode="json"))
            if is_invalid:
                invalid_attempts.append(
                    FormalizerInvalidAttemptView(
                        attempt_id=attempt_id,
                        source_id=source.source_id,
                        slot=slot,
                        validity_label=False,
                        failure_class="formalizer_output_contract",
                        failure_detail=str(extraction_failure),
                        raw_output_sha256=output_sha,
                    ).model_dump(mode="json")
                )
            else:
                candidates.append(candidate.model_dump(mode="json"))
            metrics_rows.append(metrics.model_dump(mode="json"))
            terminal_rows.append(terminal.model_dump(mode="json"))
            raw_rows.append(
                {
                    "schema_version": "sft2b_raw_generation_v1",
                    "request_key": request_key,
                    "attempt_id": attempt_id,
                    "source_id": source.source_id,
                    "slot": slot,
                    "response_id": metrics.response_id,
                    "raw_output": output,
                    "raw_output_sha256": output_sha,
                    "raw_response_base64": base64.b64encode(response).decode(),
                    "raw_response_sha256": response_sha,
                }
            )
            journal_rows.append(
                {
                    "schema_version": "sft2b_vllm_journal_event_v1",
                    "sequence": ordinal,
                    "request_key": request_key,
                    "attempt_id": attempt_id,
                    "source_id": source.source_id,
                    "slot": slot,
                    "terminal_path": f"/scratch/fixture/{request_key}/terminal.json",
                    "terminal_sha256": terminal_sha,
                }
            )
            request_keys.append(request_key)
    _write_jsonl(root / "formalizer_attempts.jsonl", attempts)
    _write_jsonl(root / "candidates.jsonl", candidates)
    _write_jsonl(root / "formalizer_invalid_attempts.jsonl", invalid_attempts)
    _write_jsonl(root / "request_metrics.jsonl", metrics_rows)
    _write_jsonl(root / "request_terminals.jsonl", terminal_rows)
    _write_jsonl(root / "raw_generations.jsonl", raw_rows)
    _write_jsonl(root / "requests_journal.jsonl", journal_rows)
    first_unix_ns = 1_800_000_000_000_000_000
    gpu_inventory = [
        {
            "index": index,
            "name": "NVIDIA A100-SXM4-80GB",
            "uuid": f"GPU-{index}",
            "memory_total_mib": 81_920,
            "memory_used_mib": 1_000 + index,
        }
        for index in range(8)
    ]
    telemetry_rows = [
        {
            "schema_version": "sft2b_vllm_telemetry_sample_v1",
            "monotonic_ns": sample_index + 1,
            "unix_time_ns": first_unix_ns + sample_index * 50_000_000_000,
            "gpus": [
                {
                    "index": index,
                    "uuid": f"GPU-{index}",
                    "memory_used_mib": (sample_index + 1) * 1_000 + index,
                    "memory_total_mib": 81_920,
                    "utilization_gpu_percent": 10 if sample_index == 0 else 90,
                    "power_draw_watts": 100.0 if sample_index == 0 else 300.0,
                }
                for index in range(8)
            ],
            "requests_running": 1.0 if sample_index == 0 else 64.0,
            "requests_waiting": 4.0 if sample_index == 0 else 0.0,
            "server_process_tree_rss_bytes": (sample_index + 1) * 1_000_000,
            "system_ram_used_bytes": (sample_index + 2) * 1_000_000,
            "system_ram_available_bytes": (4 - sample_index) * 1_000_000,
        }
        for sample_index in range(2)
    ]
    _write_jsonl(root / "telemetry.jsonl", telemetry_rows)
    (root / "vllm_server.log").write_text(
        "INFO: Started server process [12345]\n"
        "INFO: Application startup complete; vLLM API server ready\n",
        encoding="utf-8",
    )
    telemetry_summary = {
        "schema_version": "sft2b_vllm_telemetry_summary_v1",
        "samples": 2,
        "errors": [],
        "peak_by_gpu": {
            str(index): {
                "memory_used_mib": 2_000 + index,
                "utilization_gpu_percent": 90,
                "power_draw_watts": 300.0,
            }
            for index in range(8)
        },
        "max_requests_running": 64.0,
        "max_requests_waiting": 4.0,
        "peak_server_process_tree_rss_bytes": 2_000_000,
        "peak_system_ram_used_bytes": 3_000_000,
        "minimum_system_ram_available_bytes": 3_000_000,
    }
    runtime_versions = {
        "vllm": "0.12.0",
        "torch": "test",
        "transformers": "test",
        "huggingface-hub": "test",
        "flash-attn": "not-installed",
    }
    server_observation = {
        "cache_complete_at_start": False,
        "health_status": 200,
        "models_status": 200,
        "model_ids": [model["served_model_name"]],
        "telemetry": telemetry_summary,
        "runtime_versions": runtime_versions,
    }
    input_spec = pipeline_raw["input"]
    assert isinstance(input_spec, dict)
    manifest = {
        "schema_version": "sft2b_reform_32b_matched_500_generation_manifest_v1",
        "run_id": run_id,
        "git_commit": "0c4056a64333d8b3979bdf2b95dcd96159072b2e",
        "pipeline_config_path": pipeline_raw["_fixture_pipeline_path"],
        "pipeline_config_sha256": pipeline_hash,
        "input": {
            "repo_id": input_spec["repo_id"],
            "revision": input_spec["revision"],
            "path": input_spec["path"],
            "files": input_spec["files"],
            "source_manifest_sha256": input_spec["files"]["source_manifest.json"],
        },
        "model": {
            "model_id": model["model_id"],
            "revision": model["revision"],
            "snapshot_binding_sha256": model["snapshot_binding_sha256"],
            "checkpoint_dtype": "bfloat16",
            "quantization": None,
        },
        "generation": generation,
        "source_ids": [row.source_id for row in rows],
        "request_keys": request_keys,
        "candidate_ids": [row["candidate_id"] for row in attempts],
        "counts": {
            "sources": 500,
            "attempts": 2000,
            "candidates": len(candidates),
            "formalizer_invalid": len(invalid_attempts),
            "metrics": 2000,
            "terminals": 2000,
            "raw_generations": 2000,
            "lean_calls": 0,
            "judge_calls": 0,
            "core_rows": 0,
            "semantic_labels": 0,
        },
        "runtime_versions": runtime_versions,
        "gpu_inventory": gpu_inventory,
        "server_observation": server_observation,
        "tokens": {
            "prompt": 4 * sum(967 if index == 499 else index % 966 + 1 for index in range(500)),
            "completion": 10_000,
            "maximum_prompt": 967,
            "max_model_len": 5063,
        },
        "repr": pipeline_raw["repr"],
        "routing": {
            "candidate": "candidates.jsonl; validity and semantics not yet established",
            "core": "absent until Lean validity and three blinded votes",
            "formalizer_invalid": ("formalizer_invalid_attempts.jsonl; never semantic false"),
        },
        "forbidden_stages_executed": [],
    }
    _write_json(root / "generation_manifest.json", manifest)
    covered = sorted(set(PILOT_OUTPUT_FILES).difference({"SHA256SUMS"}))
    (root / "SHA256SUMS").write_text(
        "".join(f"{hash_file(root / name)}  {name}\n" for name in covered), encoding="utf-8"
    )
    request_keys_sha = hash_canonical(tuple(request_keys))
    output_hashes = {name: hash_file(root / name) for name in PILOT_OUTPUT_FILES}
    failure_taxonomy = {"candidate": len(candidates)}
    if invalid_attempts:
        failure_taxonomy["formalizer_output_contract"] = len(invalid_attempts)
    wall_ms = 100_000
    prompt_token_total = 4 * sum(967 if index == 499 else index % 966 + 1 for index in range(500))
    _write_json(
        root / "runtime_report.json",
        {
            "schema_version": "sft2b_matched_500_runtime_report_v1",
            "run_id": run_id,
            "source_count": 500,
            "request_count": 2000,
            "request_keys_sha256": request_keys_sha,
            "output_manifest_sha256": hash_file(root / "generation_manifest.json"),
            "request_metrics_sha256": hash_file(root / "request_metrics.jsonl"),
            "requests_journal_sha256": hash_file(root / "requests_journal.jsonl"),
            "telemetry_sha256": hash_file(root / "telemetry.jsonl"),
            "server_log_sha256": hash_file(root / "vllm_server.log"),
            "telemetry_samples": 2,
            "telemetry_first_unix_ns": first_unix_ns,
            "telemetry_last_unix_ns": first_unix_ns + 50_000_000_000,
            "telemetry_summary_sha256": hash_canonical(telemetry_summary),
            "server_observation_sha256": hash_canonical(server_observation),
            "runtime_versions_sha256": hash_canonical(runtime_versions),
            "gpu_inventory_sha256": hash_canonical(tuple(gpu_inventory)),
            "server_pid": 12345,
            "wall_time_ms": wall_ms,
            "prompt_tokens": prompt_token_total,
            "completion_tokens": 10_000,
            "requests_per_second": 20.0,
            "output_tokens_per_second": 100.0,
            "failure_taxonomy": failure_taxonomy,
        },
    )
    _write_json(
        root / "quality_report.json",
        {
            "schema_version": "sft2b_matched_500_quality_acceptance_v2",
            "run_id": run_id,
            "source_count": 500,
            "request_count": 2000,
            "observed_partial_evidence_binding_sha256": "0" * 64,
            "quality_metrics_sha256": "0" * 64,
            "reviewed_by": "human:unit-test",
            "reviewed_at": "2026-08-31T00:00:00Z",
            "rationale": "Synthetic strict-gate fixture exercises explicit acceptance binding.",
            "decision": "accept_as_pilot_evidence",
        },
    )
    _write_json(
        root / "replay_report.json",
        {
            "schema_version": "sft2b_matched_500_replay_report_v1",
            "run_id": run_id,
            "request_count": 2000,
            "request_keys_sha256": request_keys_sha,
            "model_calls": 0,
            "cache_hits": 2000,
            "complete_cartesian_product": True,
            "deterministic_output_sha256": output_hashes,
        },
    )
    _write_json(
        root / "server_shutdown.json",
        {
            "schema_version": "sft2b_matched_500_server_shutdown_v1",
            "run_id": run_id,
            "server_started": True,
            "server_pid": 12345,
            "server_log_sha256": hash_file(root / "vllm_server.log"),
            "telemetry_sha256": hash_file(root / "telemetry.jsonl"),
            "server_observation_sha256": hash_canonical(server_observation),
            "stopped": True,
            "clean_shutdown": True,
            "kill_escalated": False,
            "return_code": -15,
            "process_absent_after_shutdown": True,
        },
    )
    _write_json(
        root / "resource_claim.json",
        {
            "schema_version": "sft2b_matched_500_resource_claim_v1",
            "run_id": run_id,
            "reservation_root": "/scratch/milikic/data/leanfaith/value_first/host_reservations",
            "task": "SFT2B",
            "lean_workers": 0,
            "lean_rss_gib": 0.0,
            "gpu": True,
            "pid": 23456,
            "owner_session": "matched-500-test",
            "hostname": "fixture-host",
            "worktree": "/fixture/LeanFaith",
            "created_at": "2026-08-31T00:00:00Z",
        },
    )
    _write_json(
        root / "resource_release.json",
        {
            "schema_version": "sft2b_matched_500_resource_release_v1",
            "run_id": run_id,
            "task": "SFT2B",
            "reservation_root": "/scratch/milikic/data/leanfaith/value_first/host_reservations",
            "claim_acquired": True,
            "claim_artifact_path": "resource_claim.json",
            "claim_sha256": hash_file(root / "resource_claim.json"),
            "supervisor_pid": 23456,
            "released": True,
            "active_task_claims_after_release": 0,
        },
    )
    assert {path.name for path in root.iterdir()} == set(PILOT_EVIDENCE_FILES)
    return run_id


def _authorized_spec(source_count: int = 1) -> FullSourceConsumerSpec:
    pending, _ = load_consumer_spec(_V2_CONFIG)
    file_pins = tuple(item.model_copy(update={"sha256": "a" * 64}) for item in pending.input.files)
    core = pending.input.shards[0].model_copy(
        update={"id_view_sha256": "b" * 64, "expected_rows": source_count}
    )
    tail = pending.input.shards[1].model_copy(
        update={"id_view_sha256": "c" * 64, "expected_rows": 1}
    )
    input_spec = pending.input.model_copy(
        update={
            "revision": "1" * 40,
            "files": file_pins,
            "expected_source_rows": source_count + 1,
            "shards": (core, tail),
        }
    )
    authorization = ShardAuthorizationSpec(
        frozen=True,
        core_enabled=True,
        tail_enabled=False,
        authorized_by="unit-test",
        authorized_at="2026-08-31T00:00:00Z",
        pilot_evidence_binding_sha256="f" * 64,
    )
    return pending.model_copy(
        update={
            "status": "scale_authorized",
            "input": input_spec,
            "executor": pending.executor.model_copy(update={"max_model_len": 4103}),
            "authorization": authorization,
        }
    )


def _bind_fixture_quality_acceptance(
    pending: FullSourceConsumerSpec,
    *,
    pipeline_path: Path,
    input_root: Path,
    artifacts: Path,
) -> consumer_module.ObservedPilotReceipt:
    partial_gate = pending.matched_500_gate.model_copy(
        update={
            "evidence_state": "outputs_frozen_incomplete_receipts",
            "pipeline_config_path": str(pipeline_path),
            "pipeline_config_sha256": hash_file(pipeline_path),
            "artifact_files": tuple(
                PilotArtifactPin(path=name, sha256=hash_file(artifacts / name))
                for name in PILOT_OUTPUT_FILES
            ),
        }
    )
    receipt = consumer_module.verify_observed_pilot(
        _REPO_ROOT,
        pending.model_copy(update={"matched_500_gate": partial_gate}),
        artifact_root=artifacts,
        pilot_input_root=input_root,
    )
    quality = json.loads((artifacts / "quality_report.json").read_text())
    quality["observed_partial_evidence_binding_sha256"] = receipt.evidence_binding_sha256
    quality["quality_metrics_sha256"] = hash_canonical(
        receipt.quality_metrics.model_dump(mode="json")
    )
    _write_json(artifacts / "quality_report.json", quality)
    return receipt


def _backend(tmp_path: Path, source: SourceRecord) -> LoadedVllmBackend:
    original = load_vllm_spec(_REPO_ROOT, _VLLM_CONFIG)
    smoke = original.profiles["smoke_dp1_tp2"].model_copy(
        update={"source_ids": (source.source_id,), "max_model_len": 4103}
    )
    full = original.profiles[FULL_PROFILE_NAME].model_copy(
        update={
            "source_ids": (source.source_id,),
            "slots": tuple(CandidateSlot),
            "max_model_len": 4103,
            "concurrency": 4,
        }
    )
    spec = original.model_copy(
        update={
            "staging_root": tmp_path / "vllm-cache",
            "source_prompt_tokens": {source.source_id: 7},
            "profiles": {"smoke_dp1_tp2": smoke, FULL_PROFILE_NAME: full},
        }
    )
    prompt = _REPO_ROOT / "prompts/sft2b/reform_8b_generation_theorem_v1.md"
    decoding: dict[str, object] = {
        "do_sample": True,
        "max_new_tokens": 4096,
        "temperature": 0.6,
        "top_k": 20,
        "top_p": 0.95,
        "repetition_penalty": 1.0,
        "use_cache": True,
    }
    placement = FormalizerConfig(
        model_id=original.model_id,
        model_revision=original.model_revision,
        origin=CandidateOrigin.REFORM_32B,
        staging_subdir="reform_32b",
        snapshot_path=tmp_path / original.model_revision,
        snapshot_files={},
        prompt_path=prompt,
        prompt_sha256=hash_file(prompt),
        extraction_contract="final_theorem_signature_v1",
        slots=(
            SlotSpec(slot=CandidateSlot.SLOT_0, seed=0),
            SlotSpec(slot=CandidateSlot.SLOT_1, seed=1),
            SlotSpec(slot=CandidateSlot.SLOT_2, seed=2),
            SlotSpec(slot=CandidateSlot.SLOT_3, seed=3),
        ),
        decoding=decoding,
        decoding_sha256=hash_canonical(decoding),
        dtype="bfloat16",
        device="cuda:0",
        trust_remote_code=False,
        local_files_only=True,
        staging_root=tmp_path,
        owner_session="test",
        config_sha256="e" * 64,
        snapshot_binding_sha256=original.snapshot_binding_sha256,
    )
    return LoadedVllmBackend(
        spec=spec,
        config_path=_V2_CONFIG,
        config_sha256="9" * 64,
        placement=placement,
        release_root=tmp_path,
    )


def test_additive_a100_config_is_active_but_has_no_authorization_or_run_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec, _ = load_consumer_spec(_V2_CONFIG)

    assert (
        hash_file(_V1_CONFIG) == "38a687041d6fdbab1d7ee09076b0961c0d4787666cbad2115912f474a40b7d36"
    )
    assert spec.status == "active"
    assert spec.input.path_prefix == "source_inputs/reform_diverse_full_v3"
    assert spec.runtime.host_profile == "eight_a100_scratch"
    assert str(spec.runtime.scratch_root) == "/scratch/milikic/data/leanfaith"
    assert spec.executor.visible_devices == tuple(range(8))
    assert not spec.authorization.frozen
    assert not spec.authorization.core_enabled and not spec.authorization.tail_enabled
    assert main(["preflight", "--config", str(_V2_CONFIG)]) == 0
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["run_id"] is None
    assert preflight["run_id_deferred"] is True
    assert preflight["pilot_evidence_verified"] is False
    assert preflight["launch_authorized"] is False


def test_core_and_tail_authorization_are_separate_and_final_run_id_is_post_freeze() -> None:
    pending, _ = load_consumer_spec(_V2_CONFIG)
    source = _source(0)
    with pytest.raises(FullSourceConsumerError, match="run ID is deferred"):
        build_run_plan(
            pending,
            config_sha256="9" * 64,
            shard_id=CORE_SHARD,
            source_ids=(source.source_id,),
        )

    authorized = _authorized_spec()
    core = build_run_plan(
        authorized,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    assert core.run_id.startswith("sft2b_full_reform_run:")
    with pytest.raises(FullSourceConsumerError, match="tail is not independently authorized"):
        build_run_plan(
            authorized,
            config_sha256="9" * 64,
            shard_id=TAIL_SHARD,
            source_ids=(source.source_id,),
        )
    raw = authorized.model_dump(mode="json")
    raw["authorization"]["tail_enabled"] = True
    with pytest.raises(ValueError, match="never be authorized together"):
        FullSourceConsumerSpec.model_validate(raw)


def test_frozen_authorization_refuses_pending_release_and_model_length_pins() -> None:
    pending, _ = load_consumer_spec(_V2_CONFIG)
    raw = pending.model_dump(mode="json")
    raw["status"] = "scale_authorized"
    raw["authorization"] = {
        "frozen": True,
        "core_enabled": True,
        "tail_enabled": False,
        "authorized_by": "unit-test",
        "authorized_at": "2026-08-31T00:00:00Z",
        "pilot_evidence_binding_sha256": "f" * 64,
    }
    with pytest.raises(ValueError, match="immutable full-release pin"):
        FullSourceConsumerSpec.model_validate(raw)

    raw["input"]["revision"] = "1" * 40
    raw["input"]["expected_source_rows"] = 50_001
    for item in raw["input"]["files"]:
        item["sha256"] = "a" * 64
    raw["input"]["shards"][0]["id_view_sha256"] = "b" * 64
    raw["input"]["shards"][1]["id_view_sha256"] = "c" * 64
    raw["input"]["shards"][1]["expected_rows"] = 1
    with pytest.raises(ValueError, match="model/context pins"):
        FullSourceConsumerSpec.model_validate(raw)

    raw["executor"]["max_model_len"] = 4103
    raw["model"]["snapshot_binding_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="complete matched-500 artifact evidence"):
        FullSourceConsumerSpec.model_validate(raw)


def test_frozen_preflight_verifies_pilot_artifacts_before_authorizing_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorized = _authorized_spec()
    monkeypatch.setattr(
        consumer_module,
        "load_consumer_spec",
        lambda _path: (authorized, "9" * 64),
    )
    with pytest.raises(
        FullSourceConsumerError, match="matched-500 artifact evidence is still pending"
    ):
        main(
            [
                "preflight",
                "--config",
                str(_V2_CONFIG),
                "--bundle-root",
                str(tmp_path / "bundle"),
            ]
        )


def test_actual_matched_artifacts_are_cross_verified_and_tampering_fails(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "pilot-input"
    rows = _pilot_input(input_root)
    pipeline_path, pipeline_raw = _test_pipeline_config(tmp_path, input_root)
    artifacts = tmp_path / "pilot-artifacts"
    expected_run = _pilot_evidence_bundle(
        artifacts,
        rows=rows,
        pipeline_raw=pipeline_raw,
        pipeline_hash=hash_file(pipeline_path),
    )
    pending, _ = load_consumer_spec(_V2_CONFIG)
    partial_receipt = _bind_fixture_quality_acceptance(
        pending,
        pipeline_path=pipeline_path,
        input_root=input_root,
        artifacts=artifacts,
    )
    gate = pending.matched_500_gate.model_copy(
        update={
            "evidence_state": "artifacts_frozen",
            "artifact_root": artifacts,
            "pilot_input_root": input_root,
            "pipeline_config_path": str(pipeline_path),
            "pipeline_config_sha256": hash_file(pipeline_path),
            "artifact_files": tuple(
                PilotArtifactPin(path=name, sha256=hash_file(artifacts / name))
                for name in PILOT_EVIDENCE_FILES
            ),
        }
    )
    spec = pending.model_copy(update={"matched_500_gate": gate})
    evidence = verify_matched_500_gate(_REPO_ROOT, spec)

    assert evidence.run_id == expected_run
    assert len(evidence.source_ids) == 500
    assert len(evidence.request_keys) == 2000
    assert evidence.failure_taxonomy == {"candidate": 2000}
    assert len(evidence.evidence_binding_sha256) == 64
    assert partial_receipt.quality_metrics.selection_mix == {
        "library_docstring": 175,
        "theorem_problem": 175,
        "broader_public_synthetic": 100,
        "specialist_high_difficulty": 50,
    }
    assert partial_receipt.quality_metrics.provenance_origin_mix == {"new_audited": 500}

    runtime = json.loads((artifacts / "runtime_report.json").read_text())
    runtime["completion_tokens"] += 1
    _write_json(artifacts / "runtime_report.json", runtime)
    with pytest.raises(FullSourceConsumerError, match="evidence hash mismatch"):
        verify_matched_500_gate(_REPO_ROOT, spec)


def test_pilot_raw_rows_stream_in_order_and_cover_candidate_and_invalid_maps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "pilot-input"
    rows = _pilot_input(input_root)
    pipeline_path, pipeline_raw = _test_pipeline_config(tmp_path, input_root)
    artifacts = tmp_path / "pilot-artifacts"
    expected_run = _pilot_evidence_bundle(
        artifacts,
        rows=rows,
        pipeline_raw=pipeline_raw,
        pipeline_hash=hash_file(pipeline_path),
        invalid_ordinals=frozenset({17}),
    )
    pending, _ = load_consumer_spec(_V2_CONFIG)
    _bind_fixture_quality_acceptance(
        pending,
        pipeline_path=pipeline_path,
        input_root=input_root,
        artifacts=artifacts,
    )
    gate = pending.matched_500_gate.model_copy(
        update={
            "evidence_state": "artifacts_frozen",
            "artifact_root": artifacts,
            "pilot_input_root": input_root,
            "pipeline_config_path": str(pipeline_path),
            "pipeline_config_sha256": hash_file(pipeline_path),
            "artifact_files": tuple(
                PilotArtifactPin(path=name, sha256=hash_file(artifacts / name))
                for name in PILOT_EVIDENCE_FILES
            ),
        }
    )
    real_read_text = Path.read_text

    def forbid_whole_raw_read(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "raw_generations.jsonl":
            raise AssertionError("raw generations must be verified as a stream")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_whole_raw_read)
    evidence = verify_matched_500_gate(
        _REPO_ROOT,
        pending.model_copy(update={"matched_500_gate": gate}),
    )
    assert evidence.run_id == expected_run
    assert evidence.failure_taxonomy == {
        "candidate": 1999,
        "formalizer_output_contract": 1,
    }


def test_sse_replay_requires_exactly_one_terminal_done_last() -> None:
    event = (
        b'data: {"id":"cmpl-1","model":"reform-32b-80e9d9d83998",'
        b'"choices":[{"text":"x","finish_reason":"stop"}]}\n\n'
    )
    usage = (
        b'data: {"id":"cmpl-1","model":"reform-32b-80e9d9d83998",'
        b'"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
    )
    valid = event + usage + b"data: [DONE]\n\n"
    replayed = consumer_module._replay_sse(valid)
    assert replayed.response_id == "cmpl-1" and replayed.output_text == "x"
    with pytest.raises(FullSourceConsumerError, match="after its terminal DONE"):
        consumer_module._replay_sse(valid + b"data: [DONE]\n\n")
    with pytest.raises(FullSourceConsumerError, match="after its terminal DONE"):
        consumer_module._replay_sse(valid + event)


def test_self_consistent_but_noncanonical_pilot_request_key_is_rejected(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "pilot-input"
    rows = _pilot_input(input_root)
    pipeline_path, pipeline_raw = _test_pipeline_config(tmp_path, input_root)
    artifacts = tmp_path / "wrong-request-key-artifacts"
    _pilot_evidence_bundle(
        artifacts,
        rows=rows,
        pipeline_raw=pipeline_raw,
        pipeline_hash=hash_file(pipeline_path),
        corrupt_first_request_key=True,
    )
    pending, _ = load_consumer_spec(_V2_CONFIG)
    gate = pending.matched_500_gate.model_copy(
        update={
            "evidence_state": "artifacts_frozen",
            "artifact_root": artifacts,
            "pilot_input_root": input_root,
            "pipeline_config_path": str(pipeline_path),
            "pipeline_config_sha256": hash_file(pipeline_path),
            "artifact_files": tuple(
                PilotArtifactPin(path=name, sha256=hash_file(artifacts / name))
                for name in PILOT_EVIDENCE_FILES
            ),
        }
    )
    with pytest.raises(FullSourceConsumerError, match="exact frozen recomputation"):
        verify_matched_500_gate(
            _REPO_ROOT,
            pending.model_copy(update={"matched_500_gate": gate}),
        )


def test_pilot_runtime_rejects_one_line_telemetry_and_server_log(tmp_path: Path) -> None:
    input_root = tmp_path / "pilot-input"
    rows = _pilot_input(input_root)
    pipeline_path, pipeline_raw = _test_pipeline_config(tmp_path, input_root)
    artifacts = tmp_path / "pilot-artifacts"
    _pilot_evidence_bundle(
        artifacts,
        rows=rows,
        pipeline_raw=pipeline_raw,
        pipeline_hash=hash_file(pipeline_path),
    )
    pipeline, _ = consumer_module.load_pipeline_spec(_REPO_ROOT, pipeline_path)
    manifest = json.loads((artifacts / "generation_manifest.json").read_text())
    original_telemetry = (artifacts / "telemetry.jsonl").read_bytes()
    first_sample = original_telemetry.splitlines(keepends=True)[0]
    (artifacts / "telemetry.jsonl").write_bytes(first_sample)
    with pytest.raises(FullSourceConsumerError, match="multi-sample timeline"):
        consumer_module._verify_pilot_runtime_artifacts(
            artifact_root=artifacts,
            pipeline=pipeline,
            manifest=manifest,
        )

    (artifacts / "telemetry.jsonl").write_bytes(original_telemetry)
    (artifacts / "vllm_server.log").write_text("ready\n", encoding="utf-8")
    with pytest.raises(FullSourceConsumerError, match="substantive readiness"):
        consumer_module._verify_pilot_runtime_artifacts(
            artifact_root=artifacts,
            pipeline=pipeline,
            manifest=manifest,
        )


def test_pilot_gate_rejects_fake_claim_even_when_its_hash_is_pinned(tmp_path: Path) -> None:
    input_root = tmp_path / "pilot-input"
    rows = _pilot_input(input_root)
    pipeline_path, pipeline_raw = _test_pipeline_config(tmp_path, input_root)
    artifacts = tmp_path / "pilot-artifacts"
    _pilot_evidence_bundle(
        artifacts,
        rows=rows,
        pipeline_raw=pipeline_raw,
        pipeline_hash=hash_file(pipeline_path),
    )
    _write_json(artifacts / "resource_claim.json", {"fixture": True})
    release = json.loads((artifacts / "resource_release.json").read_text())
    release["claim_sha256"] = hash_file(artifacts / "resource_claim.json")
    _write_json(artifacts / "resource_release.json", release)
    pending, _ = load_consumer_spec(_V2_CONFIG)
    _bind_fixture_quality_acceptance(
        pending,
        pipeline_path=pipeline_path,
        input_root=input_root,
        artifacts=artifacts,
    )
    gate = pending.matched_500_gate.model_copy(
        update={
            "evidence_state": "artifacts_frozen",
            "artifact_root": artifacts,
            "pilot_input_root": input_root,
            "pipeline_config_path": str(pipeline_path),
            "pipeline_config_sha256": hash_file(pipeline_path),
            "artifact_files": tuple(
                PilotArtifactPin(path=name, sha256=hash_file(artifacts / name))
                for name in PILOT_EVIDENCE_FILES
            ),
        }
    )
    with pytest.raises(FullSourceConsumerError, match="evidence schema failed"):
        verify_matched_500_gate(
            _REPO_ROOT,
            pending.model_copy(update={"matched_500_gate": gate}),
        )


def test_crash_after_provider_terminals_restarts_without_duplicate_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("leanfaith.sft2b.vllm_backend.metadata.version", lambda _name: "test")
    source = _source(0)
    backend = _backend(tmp_path, source)
    spec = _authorized_spec()
    plan = build_run_plan(
        spec,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    endpoint = profile_endpoint(backend, FULL_PROFILE_NAME)
    calls = 0

    def transport(
        endpoint_url: str,
        payload: dict[str, object],
        request_key: str,
        timeout_seconds: float,
    ) -> StreamCompletion:
        nonlocal calls
        calls += 1
        assert endpoint_url == endpoint and timeout_seconds > 0
        output = "reflection\n</think>\n```lean4\ntheorem generated : True\n```\n"
        return StreamCompletion(
            raw_response=b"data: {}\n\ndata: [DONE]\n\n",
            output_text=output,
            response_id=f"cmpl-{request_key[:12]}",
            response_request_id=request_key,
            prompt_tokens=7,
            completion_tokens=12,
            finish_reason="stop",
            elapsed_ms=42,
            time_to_first_token_ms=7,
            http_status=200,
        )

    first = run_vllm_sources(
        backend,
        profile_name=FULL_PROFILE_NAME,
        sources=(source,),
        endpoint_url=endpoint,
        transport=transport,
    )
    assert first.model_calls == 4 and calls == 4
    # Simulated process crash here: provider terminals exist, but the consumer
    # journal and compaction have not run.
    compacted = run_integrated_executor(
        spec=spec,
        backend=backend,
        sources=(source,),
        plan=plan,
        cache_root=tmp_path / "consumer-cache",
        run_root=tmp_path / "runs",
        transport=transport,
    )
    assert calls == 4
    assert compacted.rows == 4
    assert compacted.sha256 == hash_file(compacted.path)

    replay = run_integrated_executor(
        spec=spec,
        backend=backend,
        sources=(source,),
        plan=plan,
        cache_root=tmp_path / "consumer-cache",
        run_root=tmp_path / "runs",
        transport=transport,
    )
    assert replay == compacted
    assert calls == 4


def test_complete_provider_cache_before_runtime_receipt_recovers_without_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("leanfaith.sft2b.vllm_backend.metadata.version", lambda _name: "test")
    source = _source(0)
    backend = _backend(tmp_path, source)
    spec = _authorized_spec()
    plan = build_run_plan(
        spec,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    endpoint = profile_endpoint(backend, FULL_PROFILE_NAME)
    calls = 0

    def transport(
        _endpoint_url: str,
        _payload: dict[str, object],
        request_key: str,
        _timeout_seconds: float,
    ) -> StreamCompletion:
        nonlocal calls
        calls += 1
        return StreamCompletion(
            raw_response=b"data: {}\n\ndata: [DONE]\n\n",
            output_text="```lean4\ntheorem generated : True\n```\n",
            response_id=f"cmpl-{request_key[:12]}",
            response_request_id=request_key,
            prompt_tokens=7,
            completion_tokens=8,
            finish_reason="stop",
            elapsed_ms=5,
            time_to_first_token_ms=1,
            http_status=200,
        )

    first = run_vllm_sources(
        backend,
        profile_name=FULL_PROFILE_NAME,
        sources=(source,),
        endpoint_url=endpoint,
        transport=transport,
    )
    assert first.model_calls == 4 and calls == 4

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("complete cache must not start vLLM or call a provider")

    monkeypatch.setattr("leanfaith.sft2b.full_source_consumer.stream_openai_completion", forbidden)
    monkeypatch.setattr("leanfaith.sft2b.full_source_consumer.subprocess.Popen", forbidden)
    compacted = run_integrated_executor(
        spec=spec,
        backend=backend,
        sources=(source,),
        plan=plan,
        cache_root=tmp_path / "consumer-cache",
        run_root=tmp_path / "runs",
        transport=None,
    )
    assert compacted.rows == 4 and calls == 4
    recovery = json.loads(
        (tmp_path / "runs" / CORE_SHARD / plan.run_id / "cache_recovery.json").read_text()
    )
    assert recovery["provider_calls"] == 0
    assert recovery["cache_hits"] == 4
    assert recovery["clean_shutdown_evidence"] is False
    assert recovery["authorization_evidence"] is False


def test_supervisor_complete_replay_never_reclaims_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(0)
    original = _authorized_spec()
    runtime = original.runtime.model_copy(
        update={"run_root": tmp_path / "runs", "cache_root": tmp_path / "cache"}
    )
    spec = original.model_copy(update={"runtime": runtime})
    config_sha = "9" * 64
    plan = build_run_plan(
        spec,
        config_sha256=config_sha,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    output = runtime.run_root / CORE_SHARD / plan.run_id / "outputs/request_terminals.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text("already compacted\n", encoding="utf-8")
    launch_nonce = "d" * 64
    launched_unix_ns = 1
    _write_json(
        output.parents[1] / "launch_status.json",
        {
            "schema_version": "sft2b_full_source_launch_status_v1",
            "run_id": plan.run_id,
            "shard_id": CORE_SHARD,
            "launch_nonce": launch_nonce,
            "launched_unix_ns": launched_unix_ns,
            "state": "launch_pending",
            "supervisor_pid": None,
        },
    )
    compacted = consumer_module.CompactionResult(
        path=output,
        rows=4,
        sha256=hash_file(output),
    )
    backend = _backend(tmp_path, source)
    monkeypatch.setattr(
        consumer_module,
        "verify_matched_500_gate",
        lambda *_args, **_kwargs: SimpleNamespace(
            evidence_binding_sha256=spec.authorization.pilot_evidence_binding_sha256
        ),
    )
    monkeypatch.setattr(
        consumer_module,
        "verify_source_views",
        lambda *_args, **_kwargs: SimpleNamespace(
            rows=(source,),
            shard_source_ids={CORE_SHARD: (source.source_id,), TAIL_SHARD: ()},
        ),
    )
    monkeypatch.setattr(
        consumer_module,
        "build_integrated_vllm_backend",
        lambda *_args, **_kwargs: (backend, (source,)),
    )
    monkeypatch.setattr(
        consumer_module,
        "_inspect_scalable_cache",
        lambda *_args, **_kwargs: consumer_module._ScalableCacheInspection(
            run_id="sft2b_vllm_run:" + "a" * 64,
            root=tmp_path / "provider",
            request_count=4,
            cached_terminals=4,
            missing_requests=0,
            ambiguous_request_keys=(),
        ),
    )
    monkeypatch.setattr(consumer_module, "list_reservations", lambda *_args, **_kwargs: [])

    def forbidden_claim(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("complete replay must not claim GPUs")

    monkeypatch.setattr(consumer_module, "claim_resources", forbidden_claim)
    monkeypatch.setattr(
        consumer_module,
        "run_integrated_executor",
        lambda *_args, **_kwargs: compacted,
    )
    assert (
        supervise_shard(
            _REPO_ROOT,
            spec=spec,
            config_path=_V2_CONFIG,
            config_sha256=config_sha,
            bundle_root=tmp_path / "bundle",
            shard_id=CORE_SHARD,
            run_root=runtime.run_root,
            launch_nonce=launch_nonce,
            launched_unix_ns=launched_unix_ns,
        )
        == 2
    )
    status = json.loads(
        (runtime.run_root / CORE_SHARD / plan.run_id / "launch_status.json").read_text()
    )
    assert status["state"] == "recovered_unattested"
    assert status["manual_reconciliation_required"] is True


def test_integrated_executor_bounds_inflight_futures_and_indexes_journal_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("leanfaith.sft2b.vllm_backend.metadata.version", lambda _name: "test")
    source = _source(0)
    original = _backend(tmp_path, source)
    profiles = dict(original.spec.profiles)
    profiles[FULL_PROFILE_NAME] = profiles[FULL_PROFILE_NAME].model_copy(update={"concurrency": 2})
    backend = LoadedVllmBackend(
        spec=original.spec.model_copy(update={"profiles": profiles}),
        config_path=original.config_path,
        config_sha256=original.config_sha256,
        placement=original.placement,
        release_root=original.release_root,
    )
    spec = _authorized_spec()
    plan = build_run_plan(
        spec,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    calls = 0
    pending_sizes: list[int] = []

    def transport(
        _endpoint_url: str,
        _payload: dict[str, object],
        request_key: str,
        _timeout_seconds: float,
    ) -> StreamCompletion:
        nonlocal calls
        calls += 1
        return StreamCompletion(
            raw_response=b"data: {}\n\ndata: [DONE]\n\n",
            output_text="```lean4\ntheorem generated : True\n```\n",
            response_id=f"cmpl-{request_key[:12]}",
            response_request_id=request_key,
            prompt_tokens=7,
            completion_tokens=8,
            finish_reason="stop",
            elapsed_ms=5,
            time_to_first_token_ms=1,
            http_status=200,
        )

    real_wait = concurrent.futures.wait

    def observed_wait(
        futures: Iterable[concurrent.futures.Future[object]],
        timeout: float | None = None,
        return_when: str = concurrent.futures.ALL_COMPLETED,
    ) -> tuple[
        set[concurrent.futures.Future[object]],
        set[concurrent.futures.Future[object]],
    ]:
        materialized = tuple(futures)
        pending_sizes.append(len(materialized))
        result = real_wait(materialized, timeout=timeout, return_when=return_when)
        return set(result.done), set(result.not_done)

    monkeypatch.setattr(
        "leanfaith.sft2b.full_source_consumer.concurrent.futures.wait", observed_wait
    )
    compacted = run_integrated_executor(
        spec=spec,
        backend=backend,
        sources=(source,),
        plan=plan,
        cache_root=tmp_path / "consumer-cache",
        run_root=tmp_path / "runs",
        transport=transport,
    )

    assert calls == 4 and compacted.rows == 4
    assert pending_sizes and max(pending_sizes) <= 2
    provider_journal = next((tmp_path / "vllm-cache").rglob("journal/requests.jsonl"))
    assert len(provider_journal.read_text().splitlines()) == 4


def test_ambiguous_started_provider_call_fails_closed_without_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("leanfaith.sft2b.vllm_backend.metadata.version", lambda _name: "test")
    source = _source(0)
    backend = _backend(tmp_path, source)
    spec = _authorized_spec()
    plan = build_run_plan(
        spec,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    endpoint = profile_endpoint(backend, FULL_PROFILE_NAME)
    inspection = inspect_vllm_sources_cache(
        backend,
        profile_name=FULL_PROFILE_NAME,
        sources=(source,),
        endpoint_url=endpoint,
    )
    ambiguous_key = inspection.missing_request_keys[0]
    started = inspection.root / "requests" / ambiguous_key / "request_started.json"
    started.parent.mkdir(parents=True)
    _write_json(
        started,
        {
            "schema_version": "sft2b_vllm_request_started_v1",
            "request_key": ambiguous_key,
        },
    )
    calls = 0

    def transport(
        _endpoint_url: str,
        _payload: dict[str, object],
        _request_key: str,
        _timeout_seconds: float,
    ) -> StreamCompletion:
        nonlocal calls
        calls += 1
        raise AssertionError("ambiguous provider call must never be repeated")

    with pytest.raises(Exception, match="ambiguous in-flight vLLM request"):
        run_integrated_executor(
            spec=spec,
            backend=backend,
            sources=(source,),
            plan=plan,
            cache_root=tmp_path / "consumer-cache",
            run_root=tmp_path / "runs",
            transport=transport,
        )
    assert calls == 0


def test_detached_health_requires_actual_durable_advancement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_journal = tmp_path / "provider" / "journal" / "requests.jsonl"
    provider_journal.parent.mkdir(parents=True)
    provider_journal.write_text("", encoding="utf-8")
    journal = tmp_path / "consumer" / "requests.jsonl"
    journal.parent.mkdir()
    journal.write_text("", encoding="utf-8")
    output = tmp_path / "outputs" / "request_terminals.jsonl"
    output.parent.mkdir()
    output.write_text("", encoding="utf-8")
    status = tmp_path / "launch_status.json"
    run_id = stable_id("sft2b_full_reform_run", {"fixture": "health"})
    launch_nonce = "e" * 64
    launched_unix_ns = 1
    _write_json(
        status,
        {
            "schema_version": "sft2b_full_source_launch_status_v1",
            "run_id": run_id,
            "shard_id": CORE_SHARD,
            "launch_nonce": launch_nonce,
            "launched_unix_ns": launched_unix_ns,
            "state": "worker_started",
            "supervisor_pid": 6789,
            "progress_artifacts": [
                {"path": str(provider_journal), "kind": "bytes", "baseline": 0},
                {"path": str(journal), "kind": "bytes", "baseline": 0},
                {"path": str(output), "kind": "bytes", "baseline": 0},
            ],
        },
    )
    launch = DetachedLaunch(
        session_name="leanfaith-sft2b-full-v3-test",
        command=("tmux",),
        status_path=status,
        log_path=tmp_path / "consumer.log",
        run_id=run_id,
        shard_id=CORE_SHARD,
        launch_nonce=launch_nonce,
        launched_unix_ns=launched_unix_ns,
        provider_journal_path=provider_journal,
        consumer_journal_path=journal,
        compacted_output_path=output,
    )

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="12345\n")

    monkeypatch.setattr("leanfaith.sft2b.full_source_consumer.subprocess.run", fake_run)
    monkeypatch.setattr(
        "leanfaith.sft2b.full_source_consumer._process_descends_from",
        lambda **_kwargs: True,
    )
    before = inspect_detached_health(launch)
    assert before.state == "worker_started"
    assert not before.durable_advancement and not before.healthy

    journal.write_text('{"sequence":0}\n', encoding="utf-8")
    status_payload = json.loads(status.read_text())
    status_payload["durable_checkpoint"] = {
        "schema_version": "sft2b_full_source_durable_checkpoint_v1",
        "sequence": 1,
        "provider_journal_bytes": 0,
        "consumer_journal_bytes": journal.stat().st_size,
        "compacted_output_bytes": 0,
    }
    _write_json(status, status_payload)
    after = inspect_detached_health(launch)
    assert after.durable_advancement and after.healthy


def test_runtime_session_receipt_opens_and_hashes_eight_gpu_telemetry(
    tmp_path: Path,
) -> None:
    source = _source(0)
    plan = build_run_plan(
        _authorized_spec(),
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    session_id = "123456789-9876"
    session_root = tmp_path / CORE_SHARD / plan.run_id / "runtime_sessions" / session_id
    session_root.mkdir(parents=True)
    server_log = session_root / "vllm_server.log"
    server_log.write_text(
        "INFO: Started server process [12345]\n"
        "INFO: Application startup complete; vLLM API server ready\n"
    )
    telemetry = session_root / "telemetry.jsonl"
    samples = [
        {
            "schema_version": "sft2b_vllm_telemetry_sample_v1",
            "monotonic_ns": index + 1,
            "unix_time_ns": 1_000_000_000 + index,
            "gpus": [
                {
                    "index": gpu_index,
                    "uuid": f"GPU-{gpu_index}",
                    "memory_used_mib": 60_000 + index,
                    "memory_total_mib": 81_920,
                    "utilization_gpu_percent": 95,
                    "power_draw_watts": 300.0,
                }
                for gpu_index in range(8)
            ],
            "requests_running": 64.0,
            "requests_waiting": 4.0,
            "server_process_tree_rss_bytes": 1_000_000 + index,
            "system_ram_used_bytes": 2_000_000 + index,
            "system_ram_available_bytes": 3_000_000 - index,
        }
        for index in range(2)
    ]
    _write_jsonl(telemetry, samples)
    shutdown = session_root / "server_shutdown.json"
    _write_json(
        shutdown,
        {
            "schema_version": "sft2b_full_source_server_shutdown_v1",
            "run_id": plan.run_id,
            "session_id": session_id,
            "server_pid": 12345,
            "server_observation": {
                "health_status": 200,
                "models_status": 200,
                "model_ids": ["reform-32b-80e9d9d83998"],
            },
            "stopped": True,
            "return_code": -15,
            "clean_shutdown": True,
            "kill_escalated": False,
            "process_absent_after_shutdown": True,
        },
    )
    summary = {
        "schema_version": "sft2b_vllm_telemetry_summary_v1",
        "samples": 2,
        "errors": [],
        "peak_by_gpu": {
            str(index): {
                "memory_used_mib": 60_001,
                "utilization_gpu_percent": 95,
                "power_draw_watts": 300.0,
            }
            for index in range(8)
        },
        "max_requests_running": 64.0,
        "max_requests_waiting": 4.0,
        "peak_server_process_tree_rss_bytes": 1_000_001,
        "peak_system_ram_used_bytes": 2_000_001,
        "minimum_system_ram_available_bytes": 2_999_999,
    }
    claim_path = tmp_path / CORE_SHARD / plan.run_id / "resource_sessions/test/claim.json"
    claim_path.parent.mkdir(parents=True)
    _write_json(claim_path, {"fixture": "resource claim"})
    consumer_module._append_runtime_session_start(
        run_root=tmp_path,
        plan=plan,
        session_id=session_id,
        server_pid=12345,
        served_model_name="reform-32b-80e9d9d83998",
        started_unix_ns=999_999_999,
        backend_config_sha256="9" * 64,
        claim_path=claim_path,
    )
    receipt = consumer_module._append_runtime_session(
        run_root=tmp_path,
        plan=plan,
        session_id=session_id,
        server_pid=12345,
        served_model_name="reform-32b-80e9d9d83998",
        started_unix_ns=999_999_999,
        server_log_path=server_log,
        telemetry_path=telemetry,
        telemetry_summary=summary,
        shutdown_path=shutdown,
    )

    assert receipt.telemetry_sha256 == hash_file(telemetry)
    assert consumer_module._verify_runtime_sessions(tmp_path, plan, require_nonempty=True) == (
        receipt,
    )
    telemetry.write_text(telemetry.read_text() + "\n")
    with pytest.raises(FullSourceConsumerError, match="telemetry artifact drifted"):
        consumer_module._verify_runtime_sessions(tmp_path, plan, require_nonempty=True)


def test_two_session_claim_release_receipts_are_append_only_and_fully_verified(
    tmp_path: Path,
) -> None:
    spec = _authorized_spec()
    source = _source(0)
    plan = build_run_plan(
        spec,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=(source.source_id,),
    )
    shard_root = tmp_path / CORE_SHARD / plan.run_id
    for sequence, pid in enumerate((100, 101)):
        claim_id = f"{sequence + 1}000-{pid}"
        nonce = f"{sequence + 1:064x}"
        claim_path = consumer_module._append_resource_record(
            shard_root=shard_root,
            kind="claim",
            claim_id=claim_id,
            payload={
                "schema_version": "sft2b_full_source_resource_claim_v2",
                "run_id": plan.run_id,
                "reservation_root": str(spec.runtime.reservation_root),
                "launch_nonce": nonce,
                "launched_unix_ns": sequence + 1,
                "reservation": {
                    "task": "SFT2B",
                    "lean_workers": 0,
                    "lean_rss_gib": 0.0,
                    "gpu": True,
                    "pid": pid,
                    "owner_session": f"fixture; run_id={plan.run_id}",
                    "hostname": "a100-fixture",
                    "worktree": str(_REPO_ROOT),
                    "created_at": f"2026-08-31T00:00:0{sequence}Z",
                },
            },
        )
        consumer_module._append_resource_record(
            shard_root=shard_root,
            kind="release",
            claim_id=claim_id,
            payload={
                "schema_version": "sft2b_full_source_resource_release_v2",
                "run_id": plan.run_id,
                "task": "SFT2B",
                "launch_nonce": nonce,
                "launched_unix_ns": sequence + 1,
                "claim_artifact_path": str(claim_path),
                "claim_sha256": hash_file(claim_path),
                "supervisor_pid": pid,
                "released": True,
                "active_task_claims_after_release": 0,
            },
        )

    consumer_module._verify_full_resource_release(shard_root, spec=spec, plan=plan)
    assert len((shard_root / "resource_claims.jsonl").read_text().splitlines()) == 2
    assert len((shard_root / "resource_releases.jsonl").read_text().splitlines()) == 2
