from __future__ import annotations

import json
from pathlib import Path

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.formalizer import FormalizerConfig, SlotSpec
from leanfaith.sft2b.schemas import (
    CandidateOrigin,
    CandidateSlot,
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.vllm_backend import (
    LoadedVllmBackend,
    StreamCompletion,
    build_vllm_serve_command,
    load_vllm_spec,
    run_vllm_profile,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_CONFIG_PATH = _REPO_ROOT / "configs/sft2b/reform_32b_vllm_v1.json"


def test_vllm_config_pins_exact_bf16_model_decoding_and_eight_gpu_topology() -> None:
    spec = load_vllm_spec(_REPO_ROOT, _CONFIG_PATH)
    placement = json.loads((_REPO_ROOT / spec.placement_config_path).read_text(encoding="utf-8"))

    assert spec.model_revision == "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8"
    assert spec.checkpoint_dtype == "bfloat16"
    assert spec.quantization is None
    assert spec.placement_config_sha256 == hash_file(_REPO_ROOT / spec.placement_config_path)
    assert placement["decoding"] == {
        "do_sample": True,
        "max_new_tokens": 4096,
        "temperature": 0.6,
        "top_k": 20,
        "top_p": 0.95,
        "repetition_penalty": 1.0,
        "use_cache": True,
    }
    smoke = spec.profiles["smoke_dp1_tp2"]
    assert smoke.data_parallel_size == 1
    assert smoke.tensor_parallel_size == 2
    assert smoke.max_model_len == 216 + 4096
    probe = spec.profiles["probe_dp4_tp2_c8"]
    assert probe.data_parallel_size == 4
    assert probe.tensor_parallel_size == 2
    assert probe.visible_devices == tuple(range(8))
    assert probe.concurrency == 8
    assert probe.max_model_len == 221 + 4096
    assert probe.prefix_caching is False


def _source() -> SourceRecord:
    nl = "Prove that truth is true."
    proposition = "True"
    revision = "1" * 40
    theorem_id = "test:true"
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
        reference_declaration_name="truth",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id=f"ctx:{'2' * 64}",
            render_compile_context_id=f"ctx:{'3' * 64}",
            project_id="test",
            project_revision="4" * 40,
            project_path="/tmp/test",
            lean_version="4.19.0",
            import_header="import Mathlib",
            source_context_path="context.json",
            source_context_sha256="5" * 64,
            helper_path="helper.lean",
            helper_sha256="6" * 64,
        ),
        provenance=SourceProvenance(
            source_family="new_audited",
            source_url="https://example.invalid/source",
            source_revision=revision,
            source_path="source.jsonl",
            source_file_sha256="7" * 64,
            manifest_path="manifest.json",
            manifest_sha256="8" * 64,
            source_recipe_sha256="9" * 64,
            license_card_value="test",
            redistribution_note="private test",
            nl_extraction_rule="test fixture",
            trusted_reference_basis="test fixture",
        ),
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=False,
    )


def _backend(tmp_path: Path, source: SourceRecord) -> LoadedVllmBackend:
    original = load_vllm_spec(_REPO_ROOT, _CONFIG_PATH)
    smoke = original.profiles["smoke_dp1_tp2"].model_copy(
        update={"source_ids": (source.source_id,), "max_model_len": 4103}
    )
    source_tokens = dict(original.source_prompt_tokens)
    for source_id in original.profiles["smoke_dp1_tp2"].source_ids:
        source_tokens.pop(source_id)
    source_tokens[source.source_id] = 7
    spec = original.model_copy(
        update={
            "staging_root": tmp_path / "staging",
            "source_prompt_tokens": source_tokens,
            "profiles": {
                "smoke_dp1_tp2": smoke,
                "probe_dp4_tp2_c8": original.profiles["probe_dp4_tp2_c8"],
            },
        }
    )
    release_root = tmp_path / original.portable_release.revision
    source_path = release_root / original.portable_release.smoke_sources_path
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source.model_dump_json() + "\n", encoding="utf-8")
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
        config_sha256="a" * 64,
        snapshot_binding_sha256=original.snapshot_binding_sha256,
    )
    return LoadedVllmBackend(
        spec=spec,
        config_path=_CONFIG_PATH,
        config_sha256=hash_file(_CONFIG_PATH),
        placement=placement,
        release_root=release_root,
    )


def test_vllm_request_replay_is_a_cache_hit_without_duplicate_generation(
    tmp_path: Path,
) -> None:
    source = _source()
    backend = _backend(tmp_path, source)
    calls = 0

    def transport(
        endpoint_url: str,
        payload: dict[str, object],
        request_key: str,
        timeout_seconds: float,
    ) -> StreamCompletion:
        nonlocal calls
        calls += 1
        assert endpoint_url.endswith("/v1/completions")
        assert payload["seed"] == 0
        assert payload["max_tokens"] == 4096
        assert payload["top_k"] == 20
        assert timeout_seconds > 0
        output = "reflection\n</think>\n```lean4\ntheorem sft2b_candidate : True := by sorry\n```\n"
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

    endpoint = "http://127.0.0.1:8101/v1/completions"
    first = run_vllm_profile(
        backend,
        profile_name="smoke_dp1_tp2",
        endpoint_url=endpoint,
        transport=transport,
    )
    replay = run_vllm_profile(
        backend,
        profile_name="smoke_dp1_tp2",
        endpoint_url=endpoint,
        transport=transport,
    )

    assert calls == 1
    assert first.model_calls == 1 and first.cache_hits == 0
    assert replay.model_calls == 0 and replay.cache_hits == 1
    assert replay.terminals == first.terminals
    assert replay.terminals[0].candidate is not None
    assert len((first.root / "journal/requests.jsonl").read_text().splitlines()) == 1


def test_vllm_launch_command_is_local_bf16_unquantized_and_profile_exact(
    tmp_path: Path,
) -> None:
    backend = _backend(tmp_path, _source())
    command = build_vllm_serve_command(backend, profile_name="smoke_dp1_tp2")
    rendered = " ".join(command)

    assert "--dtype bfloat16" in rendered
    assert "--data-parallel-size 1" in rendered
    assert "--tensor-parallel-size 2" in rendered
    assert "--max-model-len 4103" in rendered
    assert "--max-num-seqs 1" in rendered
    assert "--no-enable-prefix-caching" in command
    assert "--no-trust-remote-code" in command
    assert "--quantization" not in command
