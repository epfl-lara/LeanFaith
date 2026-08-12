from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.models.tokenizer_audit import (
    CandidateAuditConfig,
    FilePin,
    InputBinding,
    RuntimeVersions,
    SnapshotBinding,
    TokenizerAuditConfig,
    TokenizerAuditError,
    TokenizerAuditInputs,
    TokenizerAuditManifest,
    _build_payloads,
    _candidate_audit,
    _fragmentation,
    _fragmentation_payload,
    _load_inputs,
    _retention,
    _summary,
    _write_or_replay,
    load_tokenizer_audit_config,
    verify_tokenizer_audit,
)
from leanfaith.schemas.manifest import CodeState
from leanfaith.schemas.theorem import TheoremRecord


class _CharacterTokenizer:
    model_max_length = 8192
    is_fast = True

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        values = [ord(character) for character in text]
        return [1, *values, 2] if add_special_tokens else values

    def convert_ids_to_tokens(self, ids: Sequence[int]) -> list[str]:
        return [chr(value) for value in ids]

    def num_special_tokens_to_add(self, pair: bool = False) -> int:
        assert pair is False
        return 2

    def __len__(self) -> int:
        return 65536


def test_tokenizer_audit_cli_exposes_run_and_replay_commands() -> None:
    runner = CliRunner()

    run_help = runner.invoke(app, ["run-tokenizer-audit", "--help"])
    verify_help = runner.invoke(app, ["verify-tokenizer-audit", "--help"])

    assert run_help.exit_code == 0
    assert "--output-dir" in run_help.stdout
    assert "--config" in run_help.stdout
    assert verify_help.exit_code == 0
    assert "--replay" in verify_help.stdout

    section_run = runner.invoke(app, ["run-tokenizer-sections", "--help"])
    section_verify = runner.invoke(app, ["verify-tokenizer-sections", "--help"])
    assert section_run.exit_code == 0
    assert "--output-dir" in section_run.stdout
    assert "--config" in section_run.stdout
    assert section_verify.exit_code == 0
    assert "--output-dir" in section_verify.stdout


def _write_json(path: Path, value: object) -> str:
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    return hash_file(path)


def _write_jsonl(path: Path, values: list[object]) -> str:
    path.write_bytes(b"".join(canonical_json_bytes(value) + b"\n" for value in values))
    return hash_file(path)


def _toy_theorem(index: int, source: str) -> TheoremRecord:
    digest = f"{index + 1:064x}"
    ancestry = f"{index + 101:064x}"
    context = "ctx:" + "c" * 64
    declaration = f"t{index}"
    inline = None
    if source != "mathlib":
        inline = f"import Mathlib\ntheorem {declaration} : True := by sorry"
    return TheoremRecord.model_validate(
        {
            "theorem_id": "thm:" + digest,
            "ancestry_id": "anc:" + ancestry,
            "root_ancestry_ids": ["anc:" + ancestry],
            "source": source,
            "source_revision": "r",
            "context_id": context,
            "declaration_kind": "theorem",
            "declaration_name": declaration,
            "declaration_full_name": declaration,
            "proof_stripped_declaration": f"theorem {declaration} : True := by sorry",
            "inline_elaboration_source": inline,
            "is_proposition": True,
            "elaboration_status": "elaborates",
            "statement_content_hash": "d" * 64,
        }
    )


def _candidate(
    *,
    model_id: str,
    revision: str,
    snapshot: Path,
    native_max_length: int,
) -> CandidateAuditConfig:
    config_file = snapshot / "config.json"
    tokenizer_file = snapshot / "tokenizer_config.json"
    return CandidateAuditConfig(
        model_id=model_id,
        revision=revision,
        cache_snapshot=str(snapshot),
        branch="full_encoder",
        use_fast=True,
        native_max_length=native_max_length,
        files={
            "config.json": FilePin(
                sha256=hash_file(config_file), byte_count=config_file.stat().st_size
            ),
            "tokenizer_config.json": FilePin(
                sha256=hash_file(tokenizer_file), byte_count=tokenizer_file.stat().st_size
            ),
        },
    )


def _toy_config(tmp_path: Path, *, explicit: str = "∀ (x : Nat), x = x") -> TokenizerAuditConfig:
    revision = "a" * 40
    gate = tmp_path / "gate.json"
    theorem_partition = tmp_path / "theorems.jsonl"
    representation_manifest = tmp_path / "representation_manifest.json"
    representation_partition = tmp_path / "representations.jsonl"
    semantic_sections_manifest = tmp_path / "sections_manifest.json"
    semantic_sections_partition = tmp_path / "sections.jsonl"
    gate_hash = _write_json(gate, {"record_count": 1})
    theorem_hash = _write_jsonl(
        theorem_partition,
        [
            {
                "theorem": {
                    "theorem_id": "thm:one",
                    "source": "mathlib",
                    "metadata": {"conclusion_pp": "x = x"},
                }
            }
        ],
    )
    representation_manifest_hash = _write_json(representation_manifest, {"row_count": 1})
    representation_hash = _write_jsonl(
        representation_partition,
        [
            {
                "theorem_id": "thm:one",
                "normalization_version": "repr_v3",
                "headless": "(x : Nat) : x = x",
                "signature_explicit": explicit,
                "view_status": {"headless": "ok", "signature_explicit": "ok"},
            }
        ],
    )
    section_hash = _write_jsonl(
        semantic_sections_partition,
        [
            {
                "theorem_id": "thm:one",
                "source": "mathlib",
                "method_version": "lean_meta_tokenizer_sections_v1",
                "units": [
                    {
                        "ordinal": 0,
                        "kind": "ordinary_binder",
                        "binder_info": "default",
                        "domain_is_prop": False,
                        "text": explicit,
                    }
                ],
                "conclusion": "x = x",
            }
        ],
    )
    section_config = {
        "schema_version": 4,
        "profile_id": "test_sections",
        "method_version": "lean_meta_tokenizer_sections_v1",
        "theorem_partition": str(theorem_partition),
        "theorem_partition_sha256": theorem_hash,
        "expected_records": 1,
        "expected_per_source": {"mathlib": 1},
        "context_id": "ctx:" + "c" * 64,
        "import_header": "import Mathlib",
        "context_record_path": "data/context.json",
        "context_record_sha256": "1" * 64,
        "project_registry_key": "mathlib",
        "project_registry_path": "configs/projects/mathlib.yaml",
        "project_registry_sha256": "2" * 64,
        "environment_lock_path": "configs/environment.lock.yaml",
        "environment_lock_sha256": "3" * 64,
        "expected_project_revision": "4" * 40,
        "expected_lean_toolchain": "v4.31.0-rc1",
        "lean_toolchain_sha256": "5" * 64,
        "lake_manifest_sha256": "6" * 64,
        "project_dir": str(tmp_path),
        "raw_response_dir": str(tmp_path / "raw"),
        "workers": 1,
        "chunk_size": 1,
        "memory_hard_limit_mb": 16_384,
        "timeout_seconds": 30.0,
        "lean_num_threads": 1,
        "enable_incremental_optimization": True,
        "enable_parallel_elaboration": False,
        "isolate_incremental_commands": True,
        "confirm_invalid_on_fresh_process": True,
        "prepare_environment_once": True,
        "preflight_records_per_source": 0,
        "contains_private_source": True,
        "redistribution": False,
        "external_transmission": False,
        "release_eligible": False,
    }
    section_manifest_hash = _write_json(
        semantic_sections_manifest,
        {
            "schema_version": 4,
            "method_version": "lean_meta_tokenizer_sections_v1",
            "derivation_id": "tokenizer_sections:" + "7" * 64,
            "derivation_binding_sha256": "8" * 64,
            "config_hash": hash_canonical(section_config),
            "config": section_config,
            "repository_root": str(tmp_path),
            "theorem_partition_sha256": theorem_hash,
            "helper_sha256": "9" * 64,
            "environment": {
                "project_registry_key": "mathlib",
                "project_registry_sha256": "2" * 64,
                "environment_lock_sha256": "3" * 64,
                "context_record_sha256": "1" * 64,
                "project_revision": "4" * 40,
                "project_worktree_clean": True,
                "lean_toolchain": "v4.31.0-rc1",
                "lean_toolchain_sha256": "5" * 64,
                "lake_manifest_sha256": "6" * 64,
                "lean_interact_version": "0.11.4",
                "context_id": "ctx:" + "c" * 64,
            },
            "preflight_theorem_ids": [],
            "record_count": 1,
            "per_source": {"mathlib": 1},
            "context_id": "ctx:" + "c" * 64,
            "contains_private_source": True,
            "redistribution": False,
            "external_transmission": False,
            "release_eligible": False,
            "output_sha256": {"sections.jsonl": section_hash},
        },
    )
    candidates = {}
    for key in (
        "modernbert_base",
        "modernbert_large",
        "codet5p_220m_encoder",
        "deberta_v3_large",
    ):
        native = 512 if "codet5" in key or "deberta" in key else 8192
        snapshot = tmp_path / key / revision
        snapshot.mkdir(parents=True)
        _write_json(snapshot / "config.json", {"max_position_embeddings": native})
        _write_json(snapshot / "tokenizer_config.json", {"model_max_length": native})
        candidates[key] = _candidate(
            model_id=f"owner/{key}",
            revision=revision,
            snapshot=snapshot,
            native_max_length=native,
        )
    return TokenizerAuditConfig(
        schema_version=2,
        profile_id="test_audit",
        backbone_registry_path="configs/models/backbone_registry.yaml",
        backbone_registry_sha256="f" * 64,
        inputs=TokenizerAuditInputs(
            gate3_manifest=str(gate),
            gate3_manifest_sha256=gate_hash,
            theorem_partition=str(theorem_partition),
            theorem_partition_sha256=theorem_hash,
            representation_manifest=str(representation_manifest),
            representation_manifest_sha256=representation_manifest_hash,
            representation_partition=str(representation_partition),
            representation_partition_sha256=representation_hash,
            expected_records=1,
            expected_per_source={"mathlib": 1},
            expected_normalization_version="repr_v3",
            semantic_sections_status="frozen",
            semantic_sections_manifest=str(semantic_sections_manifest),
            semantic_sections_manifest_sha256=section_manifest_hash,
            semantic_sections_partition=str(semantic_sections_partition),
            semantic_sections_partition_sha256=section_hash,
            semantic_sections_method_version="lean_meta_tokenizer_sections_v1",
        ),
        candidates=candidates,
        budgets=(512, 1024),
        complete_semantics_fraction_at_512=0.99,
        representation_markers=("[HEADLESS]", "[SIGNATURE_EXPLICIT]"),
        section_budget_policy="conclusion_then_ordered_lean_sections_v2",
        unicode_symbols=("∀", "→"),
        maximum_namespace_piece_bins=10,
    )


def test_committed_config_binds_exact_four_candidate_registry() -> None:
    loaded = load_tokenizer_audit_config(Path("configs/models/tokenizer_audit_v1.yaml"))

    assert set(loaded.config.candidates) == {
        "modernbert_base",
        "modernbert_large",
        "codet5p_220m_encoder",
        "deberta_v3_large",
    }
    assert loaded.config.inputs.expected_records == 10_000
    assert loaded.config.inputs.expected_per_source == {"mathlib": 5_000, "sft_classic": 5_000}
    assert loaded.config.budgets == (512, 1024)
    assert loaded.config.complete_semantics_fraction_at_512 == 0.99
    assert loaded.config.inputs.semantic_sections_status == "pending_derivation"
    assert loaded.config.schema_version == 2


def test_committed_section_config_binds_exact_environment_and_pool_preflight() -> None:
    from leanfaith.models.tokenizer_sections import (
        FROZEN_PROFILE_ID,
        load_tokenizer_section_config,
    )

    loaded = load_tokenizer_section_config(Path("configs/models/tokenizer_sections_v1.yaml"))

    assert loaded.config.profile_id == FROZEN_PROFILE_ID
    assert loaded.config.schema_version == 4
    assert loaded.config.expected_records == 10_000
    assert loaded.config.expected_per_source == {"mathlib": 5_000, "sft_classic": 5_000}
    assert loaded.config.workers == 4
    assert loaded.config.memory_hard_limit_mb == 16_384
    assert loaded.config.enable_incremental_optimization is True
    assert loaded.config.enable_parallel_elaboration is False
    assert loaded.config.isolate_incremental_commands is True
    assert loaded.config.confirm_invalid_on_fresh_process is True
    assert loaded.config.prepare_environment_once is True
    assert loaded.config.preflight_records_per_source == 2
    assert hash_file(Path(loaded.config.context_record_path)) == loaded.config.context_record_sha256
    assert (
        hash_file(Path(loaded.config.project_registry_path))
        == loaded.config.project_registry_sha256
    )
    assert (
        hash_file(Path(loaded.config.environment_lock_path))
        == loaded.config.environment_lock_sha256
    )


def test_section_resume_items_and_work_identity_bind_helper_partition_and_environment(
    tmp_path: Path,
) -> None:
    from leanfaith.models.tokenizer_sections import (
        SectionEnvironmentBinding,
        SemanticSectionRecord,
        TokenizerSectionDerivationError,
        _derivation_binding,
        _load_item,
        _write_item,
    )

    environment = SectionEnvironmentBinding(
        project_registry_key="mathlib",
        project_registry_sha256="1" * 64,
        environment_lock_sha256="2" * 64,
        context_record_sha256="3" * 64,
        project_revision="4" * 40,
        project_worktree_clean=True,
        lean_toolchain="v4.31.0-rc1",
        lean_toolchain_sha256="5" * 64,
        lake_manifest_sha256="6" * 64,
        lean_interact_version="0.11.4",
        context_id="ctx:" + "c" * 64,
    )
    binding = _derivation_binding(
        config_hash="7" * 64,
        theorem_partition_sha256="8" * 64,
        helper_sha256="9" * 64,
        environment=environment,
    )
    changed_helper = _derivation_binding(
        config_hash="7" * 64,
        theorem_partition_sha256="8" * 64,
        helper_sha256="a" * 64,
        environment=environment,
    )
    assert changed_helper != binding

    theorem = _toy_theorem(0, "mathlib")
    record = SemanticSectionRecord(
        theorem_id=theorem.theorem_id,
        source=theorem.source,
        method_version="lean_meta_tokenizer_sections_v1",
        units=(),
        conclusion="True",
    )
    path = tmp_path / "item.json"
    _write_item(
        path,
        record,
        derivation_binding_sha256=binding,
        request_hash="b" * 64,
    )
    assert (
        _load_item(
            path,
            theorem=theorem,
            derivation_binding_sha256=binding,
            expected_request_hash="b" * 64,
        )
        == record
    )
    with pytest.raises(TokenizerSectionDerivationError, match="binding differs"):
        _load_item(
            path,
            theorem=theorem,
            derivation_binding_sha256=changed_helper,
            expected_request_hash="b" * 64,
        )


def test_section_preflight_is_exactly_two_records_per_source() -> None:
    from leanfaith.models.tokenizer_sections import (
        SectionDerivationConfig,
        _preflight_theorems,
        load_tokenizer_section_config,
    )

    raw = json.loads(
        json.dumps(
            load_tokenizer_section_config(
                Path("configs/models/tokenizer_sections_v1.yaml")
            ).config.model_dump(mode="json")
        )
    )
    raw["profile_id"] = "test"
    raw["theorem_partition"] = "/tmp/theorems.jsonl"
    raw["theorem_partition_sha256"] = "f" * 64
    raw["expected_records"] = 6
    raw["expected_per_source"] = {"mathlib": 3, "sft_classic": 3}
    config = SectionDerivationConfig.model_validate(raw)
    theorems = tuple(
        [_toy_theorem(i, "mathlib") for i in range(3)]
        + [_toy_theorem(i + 3, "sft_classic") for i in range(3)]
    )

    selected = _preflight_theorems(theorems, config)

    assert [theorem.source for theorem in selected] == [
        "mathlib",
        "mathlib",
        "sft_classic",
        "sft_classic",
    ]


def test_section_backend_freezes_incremental_command_isolation() -> None:
    from leanfaith.models.tokenizer_sections import (
        _backend_settings,
        load_tokenizer_section_config,
    )

    config = load_tokenizer_section_config(Path("configs/models/tokenizer_sections_v1.yaml")).config
    settings = _backend_settings(config)

    assert settings.enable_incremental_optimization is True
    assert settings.enable_parallel_elaboration is False
    assert settings.isolate_incremental_commands is True
    assert settings.confirm_invalid_on_fresh_process is True
    assert settings.environment_is_prepared is True
    assert settings.workers == 4
    assert settings.memory_hard_limit_mb == 16_384


def test_retention_never_credits_partial_explicit_signature() -> None:
    from leanfaith.models.tokenizer_audit import _SemanticSectionsInput

    result = _retention(
        budget=45,
        full_length=700,
        tokenizer=_CharacterTokenizer(),
        sections=_SemanticSectionsInput.model_validate(
            {
                "theorem_id": "thm:one",
                "source": "mathlib",
                "method_version": "lean_meta_tokenizer_sections_v1",
                "units": [
                    {
                        "ordinal": 0,
                        "kind": "ordinary_binder",
                        "binder_info": "default",
                        "domain_is_prop": False,
                        "text": "(a_very_long_binder : Nat)",
                    }
                ],
                "conclusion": "True",
            }
        ),
    )

    assert result.conclusion_retained is True
    assert result.complete_binder_set_retained is False
    # Empty semantic sections are completely retained; the nonempty ordinary
    # binder section fails independently.
    assert result.complete_typeclass_binder_set_retained is True
    assert result.complete_hypothesis_set_retained is True
    assert result.complete_semantic_sections_retained is False
    assert result.full_bundle_retained is False


def test_frozen_input_loader_preserves_order_and_rejects_hash_drift(tmp_path: Path) -> None:
    config = _toy_config(tmp_path)

    theorems, representations, sections, bindings = _load_inputs(config)

    assert [item.theorem_id for item in theorems] == ["thm:one"]
    assert [item.theorem_id for item in representations] == ["thm:one"]
    assert [item.theorem_id for item in sections] == ["thm:one"]
    assert set(bindings) == {
        "gate3_manifest",
        "theorem_partition",
        "representation_manifest",
        "representation_partition",
        "semantic_sections_manifest",
        "semantic_sections_partition",
    }

    Path(config.inputs.theorem_partition).write_text("{}\n", encoding="utf-8")
    with pytest.raises(TokenizerAuditError, match="input hash differs"):
        _load_inputs(config)


def test_fragmentation_reports_unicode_and_qualified_constants() -> None:
    result = _fragmentation(
        candidate="toy",
        tokenizer=_CharacterTokenizer(),
        texts=("∀ x, Mathlib.Algebra.foo x → Nat.succ x",),
        symbols=("∀", "→"),
        maximum_namespace_piece_bins=10,
    )

    assert result.unicode_occurrences == 2
    assert result.unicode_weighted_mean_pieces == 1.0
    assert result.namespace_occurrences == 2
    assert result.namespace_unique == 2
    assert result.aggregate_only is True
    assert result.contains_private_source is True
    assert sum(item.unique_count for item in result.namespace_piece_histogram) == 2
    dumped = result.model_dump_json()
    assert "Mathlib.Algebra.foo" not in dumped
    assert "Nat.succ" not in dumped


def test_summary_selects_1024_without_selecting_scientific_winner(tmp_path: Path) -> None:
    # A 600-character explicit signature fails the complete-semantics rule at
    # 512 for every candidate but fits at 1,024 under the character tokenizer.
    config = _toy_config(tmp_path, explicit="∀ " + "x" * 600)
    theorems, representations, sections, bindings = _load_inputs(config)
    tokenizer = _CharacterTokenizer()
    records = []
    metadata = {}
    snapshots = {}
    tokenizers = {}
    for key, candidate in sorted(config.candidates.items()):
        snapshot = SnapshotBinding(
            model_id=candidate.model_id,
            revision=candidate.revision,
            path=candidate.cache_snapshot,
            use_fast=True,
            native_max_length=candidate.native_max_length,
            files=candidate.files,
            snapshot_content_hash=hashlib.sha256(key.encode()).hexdigest(),
        )
        candidate_records, _, tokenizer_class, is_fast, vocab_size, reported_max = _candidate_audit(
            key=key,
            candidate=candidate,
            binding=snapshot,
            tokenizer=tokenizer,
            theorems=theorems,
            representations=representations,
            semantic_sections=sections,
            budgets=config.budgets,
            symbols=config.unicode_symbols,
            maximum_namespace_piece_bins=config.maximum_namespace_piece_bins,
        )
        records.extend(candidate_records)
        snapshots[key] = snapshot
        tokenizers[key] = tokenizer
        metadata[key] = (tokenizer_class, is_fast, vocab_size, reported_max)

    summary = _summary(
        config=config,
        config_hash="b" * 64,
        code_tree_hash="c" * 64,
        snapshot_bindings=snapshots,
        tokenizers=tokenizers,
        theorems=theorems,
        records=records,
        fragmentation_payload=_fragmentation_payload([]),
        runtime=RuntimeVersions(
            python="3.12.0",
            transformers="1",
            tokenizers="1",
            sentencepiece=None,
            protobuf=None,
        ),
        input_bindings=bindings,
        tokenizer_metadata=metadata,
    )

    assert summary.selected_length == 1024
    assert summary.scientific_winner_selected is False
    assert summary.eligible_candidates == ("modernbert_base", "modernbert_large")
    assert summary.long_input_counts == {
        "codet5p_220m_encoder": 0,
        "deberta_v3_large": 0,
        "modernbert_base": 0,
        "modernbert_large": 0,
    }
    by_key = {item.candidate: item for item in summary.candidate_summaries}
    assert by_key["codet5p_220m_encoder"].eligible_at_selected_length is False
    assert by_key["modernbert_base"].eligible_at_selected_length is True


def test_config_rejects_noncanonical_candidate_set(tmp_path: Path) -> None:
    config = _toy_config(tmp_path)
    value = config.model_dump(mode="json")
    del value["candidates"]["deberta_v3_large"]

    with pytest.raises(ValueError, match="candidates must be exactly"):
        TokenizerAuditConfig.model_validate(value)


def test_frozen_profile_rejects_gameable_denominator_and_threshold(tmp_path: Path) -> None:
    config = _toy_config(tmp_path)
    value = config.model_dump(mode="json")
    value["profile_id"] = "gate3_repr_v3_backbone_tokenizer_audit_v1"
    value["inputs"]["expected_records"] = 9_999
    value["inputs"]["expected_per_source"] = {"mathlib": 9_999}
    with pytest.raises(ValueError, match="exactly 10,000"):
        TokenizerAuditConfig.model_validate(value)

    value["inputs"]["expected_records"] = 10_000
    value["inputs"]["expected_per_source"] = {"mathlib": 5_000, "sft_classic": 5_000}
    value["complete_semantics_fraction_at_512"] = 0.98
    with pytest.raises(ValueError, match=r"exact 0\.99"):
        TokenizerAuditConfig.model_validate(value)


def test_manifest_rejects_nonexact_output_hash_keys(tmp_path: Path) -> None:
    config = _toy_config(tmp_path)
    payload = {
        "schema_version": 2,
        "audit_id": "tokenizer_audit:" + "a" * 64,
        "profile_id": config.profile_id,
        "config_hash": "b" * 64,
        "config": config.model_dump(mode="json"),
        "code": {
            "git_revision": "c" * 40,
            "git_dirty": False,
            "base_git_commit": "c" * 40,
            "code_tree_hash": "d" * 64,
            "tracked_diff_hash": "e" * 64,
            "untracked_files": [],
        },
        "repository_root": str(tmp_path),
        "runtime": {
            "python": "3.12",
            "transformers": "1",
            "tokenizers": "1",
            "sentencepiece": None,
            "protobuf": None,
        },
        "inputs": {},
        "snapshots": {},
        "selected_length": 512,
        "scientific_winner_selected": False,
        "contains_private_source": True,
        "redistribution": False,
        "external_transmission": False,
        "release_eligible": False,
        "output_sha256": {
            "fragmentation.json": "f" * 64,
            "records.jsonl": "f" * 64,
            "summary.json": "f" * 64,
            "unexpected.json": "f" * 64,
        },
    }
    with pytest.raises(ValueError, match="keys must be exactly"):
        TokenizerAuditManifest.model_validate(payload)


def test_end_to_end_static_replay_privacy_and_coordinated_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import leanfaith.models.tokenizer_audit as module

    config = _toy_config(tmp_path)
    registry = tmp_path / "registry.yaml"
    registry.write_text("candidates: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_verify_backbone_registry",
        lambda _root, _config: InputBinding(
            path=str(registry),
            sha256=hash_file(registry),
            byte_count=registry.stat().st_size,
        ),
    )
    monkeypatch.setattr(module, "_load_tokenizer", lambda _binding: _CharacterTokenizer())
    runtime = RuntimeVersions(
        python="3.12.0",
        transformers="1",
        tokenizers="1",
        sentencepiece=None,
        protobuf=None,
    )
    monkeypatch.setattr(module, "_runtime_versions", lambda: runtime)
    code = CodeState(
        git_revision="a" * 40,
        git_dirty=False,
        base_git_commit="a" * 40,
        code_tree_hash="b" * 64,
        tracked_diff_hash="c" * 64,
        untracked_files=(),
    )
    config_hash = hash_canonical(config.model_dump(mode="json"))
    payloads, _summary_value = _build_payloads(
        repo_root=tmp_path,
        config=config,
        config_hash=config_hash,
        code=code,
    )
    output = tmp_path / "audit"
    assert _write_or_replay(output, payloads) is False
    assert _write_or_replay(output, payloads) is True
    manifest = verify_tokenizer_audit(output, replay=False)
    assert manifest.runtime == runtime

    fragmentation_text = (output / "fragmentation.json").read_text(encoding="utf-8")
    assert '"aggregate_only":true' in fragmentation_text
    assert '"contains_private_source":true' in fragmentation_text
    assert "Mathlib.Algebra.foo" not in fragmentation_text

    fragmentation = json.loads(fragmentation_text)
    fragmentation["candidates"][0]["namespace_occurrences"] += 1
    (output / "fragmentation.json").write_bytes(canonical_json_bytes(fragmentation) + b"\n")
    manifest_payload = json.loads((output / "manifest.json").read_bytes())
    manifest_payload["output_sha256"]["fragmentation.json"] = hash_file(
        output / "fragmentation.json"
    )
    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest_payload) + b"\n")
    with pytest.raises(TokenizerAuditError, match="summary differs from manifest"):
        verify_tokenizer_audit(output, replay=False)


def test_private_section_paths_use_exact_modes_and_reject_symlinks(tmp_path: Path) -> None:
    from leanfaith.models.tokenizer_sections import (
        TokenizerSectionDerivationError,
        _normalize_private_tree,
        _private_directory,
        _write_private_file,
    )

    private = _private_directory(tmp_path / "private", create=True)
    item = private / "item.json"
    _write_private_file(item, b"{}\n")
    assert private.stat().st_mode & 0o777 == 0o700
    assert item.stat().st_mode & 0o777 == 0o600

    target = tmp_path / "target"
    target.mkdir()
    symlink = private / "escape"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(TokenizerSectionDerivationError, match="contains symlink"):
        _normalize_private_tree(private)


def test_section_run_rejects_raw_response_symlink_before_backend_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted raw-response link must be rejected before LeanInteract exists."""

    import leanfaith.models.tokenizer_sections as module

    loaded = module.load_tokenizer_section_config(Path("configs/models/tokenizer_sections_v1.yaml"))
    raw = loaded.config.model_dump(mode="json")
    raw.update(
        {
            "profile_id": "test-preexisting-raw-link",
            "theorem_partition": str(tmp_path / "theorems.jsonl"),
            "theorem_partition_sha256": "f" * 64,
            "expected_records": 1,
            "expected_per_source": {"mathlib": 1},
            "raw_response_dir": str(tmp_path / "raw"),
            "workers": 1,
            "preflight_records_per_source": 0,
        }
    )
    config = module.SectionDerivationConfig.model_validate(raw)
    theorem = _toy_theorem(0, "mathlib")
    (tmp_path / "raw").mkdir()
    target = tmp_path / "target"
    target.write_text("do not follow", encoding="utf-8")
    (tmp_path / "raw" / "planted.json").symlink_to(target)

    monkeypatch.setenv("LEAN_NUM_THREADS", "1")
    environment = module.SectionEnvironmentBinding(
        project_registry_key="mathlib",
        project_registry_sha256="1" * 64,
        environment_lock_sha256="2" * 64,
        context_record_sha256="3" * 64,
        project_revision="4" * 40,
        project_worktree_clean=True,
        lean_toolchain="v4.31.0-rc1",
        lean_toolchain_sha256="5" * 64,
        lake_manifest_sha256="6" * 64,
        lean_interact_version="0.11.4",
        context_id=config.context_id,
    )
    monkeypatch.setattr(module, "_verify_environment", lambda _repo, _config: environment)
    monkeypatch.setattr(module, "_theorems", lambda _config: [theorem])
    monkeypatch.setattr(module, "_helper", lambda _repo: ("helper", "a" * 64))

    backend_started = False

    def reject_backend(_settings: object) -> None:
        nonlocal backend_started
        backend_started = True
        raise AssertionError("backend must not start")

    monkeypatch.setattr(module, "LeanInteractBackend", reject_backend)

    with pytest.raises(module.TokenizerSectionDerivationError, match="contains symlink"):
        module.run_tokenizer_section_derivation(
            repo_root=Path(".").resolve(),
            output_dir=tmp_path / "output" / "frozen",
            config=config,
        )
    assert backend_started is False
    assert target.read_text(encoding="utf-8") == "do not follow"
