from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.formalizer import FormalizerConfig, SlotSpec
from leanfaith.sft2b.matched_500_pipeline import (
    Matched500PipelineError,
    Matched500PipelineSpec,
    VerifiedInput,
    _build_backend,
    load_pipeline_spec,
    verify_input_without_model,
)
from leanfaith.sft2b.schemas import (
    CandidateOrigin,
    CandidateSlot,
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)
from leanfaith.sft2b.vllm_backend import inspect_vllm_sources_cache, profile_endpoint

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_CONFIG = _REPO_ROOT / "configs/sft2b/reform_32b_matched_500_pipeline_v1.json"


def _source(index: int) -> SourceRecord:
    nl = f"Prove the standalone synthetic arithmetic statement numbered {index}."
    theorem_id = f"test:matched500:{index}"
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
        reference_declaration_name=f"matched500_{index}",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id=f"ctx:{index:064x}",
            render_compile_context_id=f"ctx:{index + 500:064x}",
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
            source_url="https://example.invalid/matched500",
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


def _write_fixture_bundle(root: Path) -> tuple[SourceRecord, ...]:
    rows = tuple(_source(index) for index in range(500))
    (root / "sources.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row.model_dump(mode="json")) + b"\n" for row in rows)
    )
    token_rows = [
        {
            "source_id": row.source_id,
            "prompt_sha256": f"{index:064x}",
            "prompt_tokens": 967 if index == 499 else index % 966 + 1,
        }
        for index, row in enumerate(rows)
    ]
    token_payload = {
        "schema_version": "sft2b_prompt_token_counts_v1",
        "source_count": 500,
        "model_id": "GuoxinChen/ReForm-32B",
        "model_revision": "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8",
        "maximum_prompt_tokens": 967,
        "required_max_model_len": 5063,
        "rows": token_rows,
    }
    (root / "prompt_token_counts.json").write_bytes(canonical_json_bytes(token_payload) + b"\n")
    manifest = {
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
            "model_revision": "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8",
            "required_max_model_len": 5063,
        },
        "repr": {
            "freeze_commit": "176a783842c5a73b84413dfa8347670608b615d9",
            "spec_sha256": "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
            "implementation_set_sha256": (
                "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
            ),
            "api_sha256": "c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d",
        },
    }
    (root / "source_manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    covered = ("prompt_token_counts.json", "source_manifest.json", "sources.jsonl")
    (root / "SHA256SUMS").write_text(
        "".join(f"{hash_file(root / name)}  {name}\n" for name in covered),
        encoding="utf-8",
    )
    return rows


def _verified_fixture(tmp_path: Path) -> tuple[VerifiedInput, Matched500PipelineSpec, str]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_fixture_bundle(bundle)
    original, config_hash = load_pipeline_spec(_REPO_ROOT, _CONFIG)
    files = {item.name: hash_file(item) for item in bundle.iterdir() if item.is_file()}
    spec = original.model_copy(update={"input": original.input.model_copy(update={"files": files})})
    verified = verify_input_without_model(_REPO_ROOT, spec=spec, bundle_root=bundle)
    return verified, spec, config_hash


def test_matched_500_config_pins_exact_hub_input_and_all_gpu_topology() -> None:
    spec, _ = load_pipeline_spec(_REPO_ROOT, _CONFIG)

    assert spec.input.revision == "08aa352a1e6c80f7c98f63070f0351ad39f8a272"
    assert spec.input.expected_rows == 500
    assert spec.generation.expected_requests == 2000
    assert spec.generation.visible_devices == tuple(range(8))
    assert spec.generation.data_parallel_size == 4
    assert spec.generation.tensor_parallel_size == 2
    assert spec.generation.concurrency == 64
    assert spec.generation.max_model_len == 5063
    assert spec.generation.slots == tuple(CandidateSlot)
    assert spec.generation.seeds == (0, 1, 2, 3)


def test_input_preflight_accepts_exact_500_and_rejects_tampering(tmp_path: Path) -> None:
    verified, spec, _ = _verified_fixture(tmp_path)

    assert len(verified.rows) == 500
    assert max(verified.prompt_tokens.values()) == 967
    (verified.root / "sources.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(Matched500PipelineError, match="input hash mismatch"):
        verify_input_without_model(_REPO_ROOT, spec=spec, bundle_root=verified.root)


def test_backend_prepares_exact_2000_cells_without_writing_or_calling_model(
    tmp_path: Path,
) -> None:
    verified, raw_spec, config_hash = _verified_fixture(tmp_path)
    spec = raw_spec
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
        model_id=spec.model.model_id,
        model_revision=spec.model.revision,
        origin=CandidateOrigin.REFORM_32B,
        staging_subdir="reform_32b",
        snapshot_path=tmp_path / spec.model.revision,
        snapshot_files={},
        prompt_path=prompt,
        prompt_sha256=hash_file(prompt),
        extraction_contract="final_theorem_signature_v1",
        slots=tuple(SlotSpec(slot=slot, seed=index) for index, slot in enumerate(CandidateSlot)),
        decoding=decoding,
        decoding_sha256=hash_canonical(decoding),
        dtype="bfloat16",
        device="cuda:0",
        trust_remote_code=False,
        local_files_only=True,
        staging_root=tmp_path,
        owner_session="test",
        config_sha256="8" * 64,
        snapshot_binding_sha256=spec.model.snapshot_binding_sha256,
    )
    backend = _build_backend(
        spec=spec,
        config_path=_CONFIG,
        config_hash=config_hash,
        placement=placement,
        verified_input=verified,
        work_root=tmp_path / "work",
    )
    endpoint = profile_endpoint(backend, "probe_dp4_tp2_c8")
    inspection = inspect_vllm_sources_cache(
        backend,
        profile_name="probe_dp4_tp2_c8",
        sources=verified.rows,
        endpoint_url=endpoint,
    )

    assert inspection.request_count == 2000
    assert len(inspection.missing_request_keys) == 2000
    assert len(set(inspection.missing_request_keys)) == 2000
    assert not inspection.root.exists()
