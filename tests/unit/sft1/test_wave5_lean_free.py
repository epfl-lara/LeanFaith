"""Lean-free Wave 5 compiler inventory contracts."""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.sft1.sprint.compiler_inventory import (
    FEATURE_VERSION,
    INVENTORY_SCHEMA_VERSION,
    LENGTH_STRATA_VERSION,
    NORMALIZATION_VERSION,
    AuditSampleSettings,
    CompilerInventoryError,
    CompilerProjectContext,
    CompilerRecordDraft,
    Cpt2ReleasePin,
    GoldenBlocklistHook,
    InventorySettings,
    build_compiler_record,
    build_inventory,
    extract_theorem_signature,
    iter_inventory_records,
    load_inventory_config,
    load_pinned_input_shards,
    normalize_lean_layout,
    reconstruct_source,
    signature_features,
)
from leanfaith.sft1.sprint.compiler_replay import (
    AUDIT_SCHEMA_VERSION,
    CHECKER_VERSION,
    DOWNSTREAM_MODE,
    EVIDENCE_MARKER,
    CompilerAuditRunner,
    CompilerAuditSettings,
    CompilerReplayError,
    CompilerTypedHookSpec,
    build_context_request,
    build_typed_descriptor_request,
    build_typed_wave4_selected_request,
    load_audit_sample,
    load_compiler_audit_config,
    parse_typed_descriptor_payloads,
    resolve_audit_sources,
)
from leanfaith.sft1.sprint.engine import EVIDENCE_MARKER as SPRINT_EVIDENCE_MARKER

ROOT = find_repo_root(Path(__file__))
CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave5_v1.yaml"
RELEASE_SCHEMA = pa.schema(
    [
        pa.field("theorem", pa.large_string(), nullable=False),
        pa.field("body", pa.large_string(), nullable=False),
        pa.field("label", pa.bool_(), nullable=False),
    ]
)


@dataclass(frozen=True, slots=True)
class TinyRelease:
    settings: InventorySettings
    rows: tuple[tuple[str, str, bool], ...]


class FakeAuditBackend:
    def __init__(self, settings: BackendSettings, factory: FakeAuditFactory) -> None:
        self.settings = settings
        self.factory = factory

    def _result(self, request: LeanRequest) -> LeanResult:
        self.factory.request_ids.append(request.request_id)
        status = self.factory.status
        if self.factory.crashes_remaining:
            self.factory.crashes_remaining -= 1
            status = LeanStatus.CRASH
        messages: tuple[dict[str, object], ...] = ()
        if status == LeanStatus.VALID:
            roots = json.loads(request.metadata["audit_root_ids"])
            names = json.loads(request.metadata["audit_qualified_names"])
            messages = tuple(
                {
                    "severity": "info",
                    "data": EVIDENCE_MARKER
                    + json.dumps(
                        {
                            "schema_version": AUDIT_SCHEMA_VERSION,
                            "checker_version": CHECKER_VERSION,
                            "root_id": root_id,
                            "requested_qualified_name": name,
                            "resolved_qualified_name": name,
                            "status": "compatible",
                            "taxonomy": "verified_local_theorem_proof",
                            "detail": "fake exact proof check",
                            "constant_kind": "theorem",
                            "type_expr_hash_u64": "1",
                            "proof_expr_hash_u64": "2",
                            "level_params": [],
                            "kernel_level_instantiation": "none",
                            "meta_checked": True,
                            "kernel_checked": True,
                            "source_proof_type_matches": True,
                            "closed_prop": True,
                            "environment_origin": "current_compilation_unit",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
                for root_id, name in zip(roots, names, strict=True)
            )
        return LeanResult(
            request_id=request.request_id,
            request_hash=hash_canonical(["fake-audit", request.code]),
            context_id=request.context_id,
            context_fingerprint=self.settings.context_fingerprint,
            status=status,
            messages=messages,
            elapsed_ms=7,
            infrastructure_error=("injected crash" if status == LeanStatus.CRASH else None),
        )

    def run(self, request: LeanRequest) -> LeanResult:
        return self._result(request)

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self._result(request) for request in requests]

    def close(self) -> None:
        self.factory.closes += 1


class FakeAuditFactory:
    def __init__(
        self,
        *,
        status: LeanStatus = LeanStatus.VALID,
        crashes_remaining: int = 0,
    ) -> None:
        self.status = status
        self.crashes_remaining = crashes_remaining
        self.constructions = 0
        self.closes = 0
        self.request_ids: list[str] = []

    def __call__(self, settings: BackendSettings) -> FakeAuditBackend:
        self.constructions += 1
        return FakeAuditBackend(settings, self)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_parquet(path: Path, rows: list[tuple[str, str, bool]]) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_arrays(
        [
            pa.array([row[0] for row in rows], type=pa.large_string()),
            pa.array([row[1] for row in rows], type=pa.large_string()),
            pa.array([row[2] for row in rows], type=pa.bool_()),
        ],
        schema=RELEASE_SCHEMA,
    )
    pq.write_table(table, path, compression="zstd")
    labels = {"true": sum(row[2] for row in rows), "false": sum(not row[2] for row in rows)}
    return {
        "file": path.name,
        "rows": len(rows),
        "labels": labels,
        "bytes": path.stat().st_size,
        "sha256": hash_file(path),
    }


def _tiny_release(tmp_path: Path) -> TinyRelease:
    release_root = tmp_path / "release"
    train_rows = [
        ("not a theorem at all", " malformed", False),
        ("import Mathlib\ntheorem Alpha (n : Nat) : n = n := ", " rfl", True),
        (
            "import Mathlib\ntheorem Alpha (n : Nat) : n = n := ",
            "\n  simpa using (rfl : (1 : Nat) = 1)",
            True,
        ),
        (
            "import Mathlib\ntheorem Beta /- layout only -/ (n : Nat) : n = n := ",
            " exact rfl",
            True,
        ),
        ('import Mathlib\ntheorem StringWide : "a  b" = "a  b" := ', " rfl", True),
        ('import Mathlib\ntheorem StringNarrow : "a b" = "a b" := ', " rfl", True),
        ("import Mathlib\ntheorem Blocked : True := ", " trivial", True),
    ]
    validation_rows = [
        (
            "import Mathlib\ntheorem Gamma (n : Nat)\n  : n = n := ",
            " exact rfl",
            True,
        ),
        (
            "import Mathlib\n"
            "theorem Helper : True := by trivial\n"
            "namespace «Odd Name»\n"
            "open Nat\n"
            "theorem Delta (n : Nat) : n = n := ",
            " exact rfl",
            True,
        ),
    ]
    train_receipt = _write_parquet(release_root / "train-00000-of-00001.parquet", train_rows)
    validation_receipt = _write_parquet(
        release_root / "validation-00000-of-00001.parquet", validation_rows
    )
    all_rows = (*train_rows, *validation_rows)
    valid_rows = [row for row in all_rows if row[2]]
    run_id = "d" * 64
    release_tree = "c" * 64
    data_commit = "b" * 40
    final_revision = "f" * 40
    source_revision = "a" * 40
    source_sha = "e" * 64
    manifest: dict[str, Any] = {
        "artifact_kind": "cpt2_full_release",
        "schema_version": "cpt2_theorem_body_label_v1",
        "scale_version": "cpt2_full_scale_v1",
        "run_id": run_id,
        "output_rows": len(all_rows),
        "output_labels": {
            "true": len(valid_rows),
            "false": len(all_rows) - len(valid_rows),
        },
        "source": {
            "repo_id": "example/compiler_data",
            "resolved_revision": source_revision,
            "parquet_path": "answer_data.parquet",
            "parquet_sha256": source_sha,
            "schema": [
                ["source_code", "large_string"],
                ["validation", "large_string"],
                ["isValid", "bool"],
            ],
        },
        "publication": {
            "destination": "example/cpt2",
            "data_commit": data_commit,
            "data_files_release_tree_sha256": release_tree,
        },
        "release": {
            "schema": [
                ["theorem", "large_string"],
                ["body", "large_string"],
                ["label", "bool"],
            ],
            "shards": [
                {
                    "part": 0,
                    "total_parts": 1,
                    "train": train_receipt,
                    "validation": validation_receipt,
                }
            ],
        },
        "splitter": {
            "method": "declaration_aware_v3",
            "round_trip_failures": 0,
            "scale_lean_rows": 0,
        },
        "training_started": False,
    }
    manifest_path = release_root / "manifest.json"
    _write_json(manifest_path, manifest)
    manifest_sha = hash_file(manifest_path)
    publication_receipt = {
        "artifact_kind": "cpt2_private_publication_receipt",
        "repo_id": "example/cpt2",
        "provenance_commit": final_revision,
        "data_commit": data_commit,
        "finalized_manifest_sha256": manifest_sha,
        "release_tree_sha256": release_tree,
        "private": True,
        "provenance_commit_is_head": True,
        "provenance_parent_is_data_commit": True,
        "remote_manifest_byte_identical": True,
        "local_remote_parquet_hashes_match": True,
        "parquet_lfs_hashes_unchanged_from_data_commit": True,
    }
    publication_path = tmp_path / "publication.json"
    _write_json(publication_path, publication_receipt)
    blocklist_path = tmp_path / "gold.json"
    _write_json(
        blocklist_path,
        {"version": ["golden_blocklist_v1"], "near_dup_hashes": [], "group_keys": []},
    )
    raw_exact = len({sha256_hex(row[0].encode("utf-8")) for row in valid_rows})
    settings = InventorySettings(
        release_root=release_root,
        manifest_path=manifest_path,
        publication_receipt_path=publication_path,
        output_root=tmp_path / "inventory-output",
        gold_blocklist_path=blocklist_path,
        gold_blocklist_sha256=hash_file(blocklist_path),
        pin=Cpt2ReleasePin(
            repo_id="example/cpt2",
            final_revision=final_revision,
            data_commit=data_commit,
            manifest_sha256=manifest_sha,
            publication_receipt_sha256=hash_file(publication_path),
            release_tree_sha256=release_tree,
            cpt2_run_id=run_id,
            source_repo_id="example/compiler_data",
            source_revision=source_revision,
            source_parquet_path="answer_data.parquet",
            source_parquet_sha256=source_sha,
            expected_release_rows=len(all_rows),
            expected_valid_rows=len(valid_rows),
            expected_valid_exact_prefixes=raw_exact,
        ),
        project=CompilerProjectContext(
            project_id="mathlib",
            project_revision="1" * 40,
            lean_version="v4.test",
            lean_interact_version="test",
            repl_revision="example/repl@test",
            checker_version="test_checker_v1",
        ),
        audit_sample=AuditSampleSettings(
            size=3,
            salt="tiny_audit_v1",
            features=("equality", "existential", "typeclass"),
            length_dimensions=("signature", "full_source"),
            include_context_frequency_strata=True,
            include_namespace_status_strata=True,
            include_context_complexity_strata=True,
        ),
        output_shards=3,
        batch_rows=2,
    )
    return TinyRelease(settings=settings, rows=all_rows)


def test_wave5_config_pins_exact_cpt2_release_and_zero_lean_contract() -> None:
    settings = load_inventory_config(CONFIG)
    assert settings.pin.repo_id == "Lemmy00/leanfaith-cpt2-proof-validity-v1"
    assert settings.pin.final_revision == "df99c186ce1841c806d8b2a194573dc0b73fed33"
    assert settings.pin.data_commit == "b6955dbf234d4ac294a6cf795c2e471126828d4c"
    assert (
        settings.pin.manifest_sha256
        == "0c9896c40f94194a5cfb4cda58f8e083a3587d5b76937619b539de377d996f10"
    )
    assert settings.pin.expected_valid_rows == 2_013_342
    assert settings.pin.expected_valid_exact_prefixes == 1_806_241
    assert settings.splits == ("train", "validation")
    assert settings.verify_input_file_hashes is True
    assert settings.audit_sample is not None and settings.audit_sample.size == 1000
    assert settings.config_sha256 == hash_file(CONFIG)


def test_exact_reconstruction_and_final_signature_selection() -> None:
    theorem = (
        "import Mathlib\n"
        "theorem helper : True := by trivial\n"
        "/- theorem decoy : False := by -/\n"
        "theorem Final.«odd  name» (n : Nat) : n = n := "
    )
    body = "\n  exact rfl"
    assert reconstruct_source(theorem, body) == theorem + "by" + body
    signature = extract_theorem_signature(theorem)
    assert signature.declaration_name == "Final.«odd  name»"
    assert signature.normalized_signature == "(n : Nat) : n = n"
    assert signature.context_prefix.endswith("/- theorem decoy : False := by -/\n")
    with_let = extract_theorem_signature("theorem WithLet : let n : Nat := 3; n = 3 := ")
    assert with_let.normalized_signature == ": let n : Nat := 3; n = 3"


def test_normalization_is_name_free_comment_free_and_quote_aware() -> None:
    alpha = extract_theorem_signature(
        'theorem Alpha /- nested /- x -/ comment -/ (s : String) : s = "a  -- /- b" := '
    )
    beta = extract_theorem_signature('lemma Beta\n  (s : String)\n  : s = "a  -- /- b" := ')
    changed_string = extract_theorem_signature('theorem Beta (s : String) : s = "a -- /- b" := ')
    assert alpha.normalized_signature == beta.normalized_signature
    assert alpha.normalized_signature != changed_string.normalized_signature
    assert normalize_lean_layout("(x : Char) : x = ' '") == "(x : Char) : x = ' '"
    assert normalize_lean_layout("(x : «A  B») : x = x") == "(x : «A  B») : x = x"


def test_record_has_content_locator_context_features_and_length_strata(tmp_path: Path) -> None:
    tiny = _tiny_release(tmp_path)
    shard = load_pinned_input_shards(tiny.settings)[0]
    theorem = (
        "import Mathlib\n"
        "set_option maxHeartbeats 0\n"
        "open scoped BigOperators\n"
        "universe u\n"
        "namespace Algebra_611064\n"
        "section local\n"
        "include u\n"
        "theorem Algebra_611064 {α : Type u} [Preorder α] (n : Nat) : "
        "(∃ i ∈ Finset.range n, i < n) → n ≤ n := "
    )
    draft = build_compiler_record(
        theorem=theorem,
        body=" trivial",
        row_index=7,
        shard=shard,
        pin=tiny.settings.pin,
        project=tiny.settings.project,
    )
    record = draft.record
    assert record["schema_version"] == INVENTORY_SCHEMA_VERSION
    assert record["source"]["row_index"] == 7  # type: ignore[index]
    assert record["source"]["shard_sha256"] == shard.sha256  # type: ignore[index]
    assert record["declaration"]["name"] == "Algebra_611064"  # type: ignore[index]
    assert (
        record["declaration"]["qualified_name_candidate"]  # type: ignore[index]
        == "Algebra_611064.Algebra_611064"
    )
    assert set(cast(list[str], record["features"])) >= {
        "strict_order",
        "non_strict_order",
        "bounded_quantifier",
        "existential",
        "implication",
        "membership",
        "universe",
        "typeclass",
    }
    context = record["context"]
    assert context["imports"] == ["Mathlib"]  # type: ignore[index]
    assert context["option_commands"] == ["set_option maxHeartbeats 0"]  # type: ignore[index]
    assert context["open_commands"] == ["open scoped BigOperators"]  # type: ignore[index]
    assert context["include_commands"] == ["include u"]  # type: ignore[index]
    assert context["namespace_stack"] == ["Algebra_611064"]  # type: ignore[index]
    assert record["lengths"]["full_source_characters"] == len(theorem + "by trivial")  # type: ignore[index]


def test_build_filters_false_before_parsing_dedups_and_resumes(tmp_path: Path) -> None:
    tiny = _tiny_release(tmp_path)

    def contamination_hook(draft: CompilerRecordDraft) -> str | None:
        record = draft.record
        declaration = cast(dict[str, object], record["declaration"])
        if declaration["name"] == "Blocked":
            return "test_contamination"
        return None

    first = build_inventory(
        tiny.settings,
        contamination_hook=contamination_hook,
        contamination_hook_id="test_contamination_v1",
        contamination_hook_sha256="1" * 64,
    )
    manifest_before = first.manifest_path.read_bytes()
    records = list(iter_inventory_records(tiny.settings.output_root))
    assert first.output_rows == 4
    assert len(records) == 4
    names = {record["declaration"]["name"] for record in records}
    assert "Alpha" in names
    assert "Blocked" not in names
    assert names & {"Beta", "Gamma"} == set()
    assert names >= {"Delta", "StringWide", "StringNarrow"}
    alpha = next(record for record in records if record["declaration"]["name"] == "Alpha")
    delta = next(record for record in records if record["declaration"]["name"] == "Delta")
    assert (
        alpha["hashes"]["normalized_signature_sha256"]
        == delta["hashes"]["normalized_signature_sha256"]
    )
    assert alpha["context"]["context_fingerprint"] != delta["context"]["context_fingerprint"]
    assert alpha["normalized_group_id"] != delta["normalized_group_id"]
    assert alpha["dedup"] == {
        "winner_exact_proof_count": 2,
        "normalized_exact_group_count": 3,
        "normalized_proof_count": 4,
    }
    assert alpha["source"]["split"] == "train"
    assert len(alpha["source_row_id"]) == 64
    assert len(alpha["inventory_record_sha256"]) == 64
    assert all("theorem" not in record and "body" not in record for record in records)

    manifest = json.loads(manifest_before)
    assert manifest["lean_calls"] == 0
    assert manifest["counts"]["input_rows"] == len(tiny.rows)
    assert manifest["counts"]["false_filtered_rows"] == 1
    assert manifest["counts"]["valid_rows"] == 8
    assert manifest["counts"]["accepted_rows"] == 7
    assert manifest["counts"]["raw_valid_exact_prefixes"] == 7
    assert manifest["counts"]["post_screen_exact_prefixes"] == 6
    assert manifest["counts"]["normalized_unique_contextual_signatures"] == 4
    assert manifest["counts"]["global_normalized_text_signatures"] == 3
    assert manifest["contamination"]["rejections"] == {"test_contamination": 1}
    assert manifest["contamination"]["additional_hook_id"] == "test_contamination_v1"
    assert manifest["contamination"]["additional_hook_sha256"] == "1" * 64
    assert len(manifest["output"]["shards"]) == 3
    assert manifest["audit_sample"]["rows"] == 3
    assert manifest["audit_sample"]["complete_population"] is False
    assert manifest["audit_sample"]["missing_required_context_cells"] == []
    assert manifest["audit_sample"]["population_namespace_status_counts"] == {
        "requires_lean_verification": 1,
        "simple_namespace_stack_v1": 3,
    }
    assert manifest["audit_sample"]["namespace_status_counts"].keys() == {
        "requires_lean_verification",
        "simple_namespace_stack_v1",
    }
    assert first.audit_sample_path is not None
    assert hash_file(first.audit_sample_path) == manifest["audit_sample"]["sha256"]
    run_spec = json.loads(
        (tiny.settings.output_root / "_state/run_spec.json").read_text(encoding="utf-8")
    )
    assert set(run_spec["semantic_dependency_sha256"]) == {
        "leanfaith.config.hashing",
        "leanfaith.cpt2.splitters",
        "leanfaith.representations.views",
    }
    for receipt in manifest["output"]["shards"]:
        output = tiny.settings.output_root / "inventory" / receipt["file"]
        assert hash_file(output) == receipt["sha256"]

    journal = tiny.settings.output_root / "_state/journal.jsonl"
    with journal.open("ab") as handle:
        handle.write(b'{"torn":')
    second = build_inventory(
        tiny.settings,
        contamination_hook=contamination_hook,
        contamination_hook_id="test_contamination_v1",
        contamination_hook_sha256="1" * 64,
    )
    assert second.resumed_input_shards == 2
    assert second.written_input_shards == 0
    assert second.resumed_output_shards == 3
    assert second.written_output_shards == 0
    assert second.manifest_path.read_bytes() == manifest_before
    with pytest.raises(CompilerInventoryError, match="run spec differs"):
        build_inventory(
            tiny.settings,
            contamination_hook=contamination_hook,
            contamination_hook_id="changed_contamination_v2",
            contamination_hook_sha256="1" * 64,
        )
    with pytest.raises(CompilerInventoryError, match="run spec differs"):
        build_inventory(
            tiny.settings,
            contamination_hook=contamination_hook,
            contamination_hook_id="test_contamination_v1",
            contamination_hook_sha256="2" * 64,
        )


def test_pin_and_input_hash_drift_fail_closed(tmp_path: Path) -> None:
    tiny = _tiny_release(tmp_path)
    wrong_pin = replace(tiny.settings.pin, manifest_sha256="0" * 64)
    with pytest.raises(CompilerInventoryError, match="manifest SHA-256 mismatch"):
        load_pinned_input_shards(replace(tiny.settings, pin=wrong_pin))

    shard = tiny.settings.release_root / "train-00000-of-00001.parquet"
    with shard.open("ab") as handle:
        handle.write(b"drift")
    with pytest.raises(CompilerInventoryError, match=r"shard .* SHA-256 mismatch"):
        build_inventory(tiny.settings)


def test_gold_hook_checks_normalized_signature_and_version(tmp_path: Path) -> None:
    tiny = _tiny_release(tmp_path)
    shard = load_pinned_input_shards(tiny.settings)[0]
    draft = build_compiler_record(
        theorem="import Mathlib\ntheorem Gold (n : Nat) : n = n := ",
        body=" rfl",
        row_index=0,
        shard=shard,
        pin=tiny.settings.pin,
        project=tiny.settings.project,
    )
    normalized_hash = draft.record["hashes"]["normalized_signature_sha256"]  # type: ignore[index]
    blocklist = tmp_path / "targeted-gold.json"
    _write_json(
        blocklist,
        {
            "version": ["golden_blocklist_v1"],
            "near_dup_hashes": [normalized_hash],
            "group_keys": [],
        },
    )
    hook = GoldenBlocklistHook.load(blocklist, hash_file(blocklist))
    assert hook(draft) == "gold_normalized_signature_hash"

    wrong_version = tmp_path / "wrong-version.json"
    _write_json(
        wrong_version,
        {"version": ["v2"], "near_dup_hashes": [], "group_keys": []},
    )
    with pytest.raises(CompilerInventoryError, match="version"):
        GoldenBlocklistHook.load(wrong_version, hash_file(wrong_version))


def test_completed_output_shard_corruption_fails_closed(tmp_path: Path) -> None:
    tiny = _tiny_release(tmp_path)
    result = build_inventory(tiny.settings)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    receipt = next(item for item in manifest["output"]["shards"] if item["rows"] > 0)
    path = tiny.settings.output_root / "inventory" / receipt["file"]
    with path.open("ab") as handle:
        handle.write(b"drift")
    with pytest.raises(CompilerInventoryError, match=r"output shard .* hash mismatch"):
        build_inventory(tiny.settings)


def test_versions_are_explicit_and_stable() -> None:
    assert NORMALIZATION_VERSION == "lean_name_free_quote_aware_layout_v1"
    assert FEATURE_VERSION == "sft1_compiler_signature_features_v1"
    assert LENGTH_STRATA_VERSION == "sft1_compiler_fixed_character_bins_v1"
    assert "equality" not in signature_features("(P Q : Prop) : P -> Q")


def test_inventory_module_has_no_lean_runtime_or_process_import() -> None:
    module_path = ROOT / "src/leanfaith/sft1/sprint/compiler_inventory.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    assert not {name for name in imports if name.startswith("leanfaith.lean")}
    assert "lean_interact" not in imports
    assert "subprocess" not in imports


def _tiny_audit_settings(tmp_path: Path) -> CompilerAuditSettings:
    tiny = _tiny_release(tmp_path / "cpt2")
    config_path = tmp_path / "fixture-wave5.yaml"
    config_path.write_text("fixture: sft1-wave5-audit\n", encoding="utf-8")
    inventory = replace(tiny.settings, config_sha256=hash_file(config_path))
    build_inventory(inventory)
    return CompilerAuditSettings(
        inventory=inventory,
        config_path=config_path,
        config_sha256=hash_file(config_path),
        output_root=tmp_path / "audit",
        project_dir=tmp_path / "unused-project",
        engine_path=ROOT / "LeanFaith/Meta/SFT1/Sprint.lean",
        resource_task="SFT1-WAVE5-AUDIT-TEST",
        lean_workers=2,
        lean_rss_claim_gib=40,
        memory_hard_limit_mb=24_576,
        request_timeout_seconds=30,
        context_request_max_roots=20,
        request_batch_size=8,
        retry_max_attempts=2,
        retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
        terminal_marker="complete.json",
        expected_rows=3,
        elab_async=False,
        isolate_incremental_commands=True,
        downstream_mode=DOWNSTREAM_MODE,
    )


def test_wave5_compiler_audit_config_is_bounded_and_receipt_only() -> None:
    settings = load_compiler_audit_config(CONFIG)
    assert settings.expected_rows == 1000
    assert settings.lean_workers == 2
    assert settings.lean_rss_claim_gib == 40
    assert settings.memory_hard_limit_mb == 24_576
    assert settings.retry_statuses == {
        LeanStatus.CRASH,
        LeanStatus.INTERNAL_ERROR,
        LeanStatus.TIMEOUT,
    }
    assert settings.elab_async is False
    assert settings.isolate_incremental_commands is True
    assert settings.downstream_mode == DOWNSTREAM_MODE


def test_audit_resolves_exact_source_and_places_context_before_theorem(tmp_path: Path) -> None:
    settings = _tiny_audit_settings(tmp_path)
    records = list(iter_inventory_records(settings.inventory.output_root))
    alpha = next(row for row in records if row["declaration"]["name"] == "Alpha")
    source = resolve_audit_sources(settings, [alpha])[0]
    prepared = build_context_request(
        [source],
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    code = cast(str, prepared.request.code)
    assert source.full_source in code
    assert code.index(source.context_prefix) < code.index(source.declaration_source)
    assert code.index(source.declaration_source) < code.index("run_meta do")
    assert "Meta.check rejected the proof" in code
    assert "Kernel.check env {} proof0" in code
    assert source.qualified_name == "Alpha"
    assert prepared.request.allow_sorry is False


def test_audit_run_resume_and_forced_replay_construct_no_second_backend(
    tmp_path: Path,
) -> None:
    settings = _tiny_audit_settings(tmp_path)
    sample = load_audit_sample(settings)
    assert len(sample) == 3
    factory = FakeAuditFactory()
    runner = CompilerAuditRunner(
        settings,
        backend_factory=factory,
        manage_resources=False,
        verify_project=False,
    )
    first = runner.run()
    assert first.roots == 3
    assert first.compatible + first.incompatible == 3
    assert first.incompatible >= 1
    assert first.lean_requests >= 1
    complete = json.loads(first.complete_path.read_text(encoding="utf-8"))
    assert complete["failure_taxonomy"]["unresolved_namespace_context"] >= 1
    assert complete["downstream"] == {
        "mode": DOWNSTREAM_MODE,
        "name_based_sprint_runner_compatible": False,
        "verified_roots_may_enter_core": False,
        "required_hook": (
            "pass each verified local theorem type/proof directly into existing Sprint "
            "Wave 3/4 logic inside its exact compilation request"
        ),
    }
    with (settings.output_root / "journal.jsonl").open("ab") as handle:
        handle.write(b'{"torn":')

    def explode(_settings: BackendSettings) -> FakeAuditBackend:
        raise AssertionError("completed resume must not construct a backend")

    resumed = CompilerAuditRunner(
        settings,
        backend_factory=explode,
        manage_resources=False,
        verify_project=False,
    ).run()
    assert resumed.cache_hits == 3
    assert resumed.lean_requests == 0
    replay = CompilerAuditRunner(
        settings,
        backend_factory=explode,
        manage_resources=False,
        verify_project=False,
    ).replay()
    assert replay["cache_hits"] == 3
    assert replay["lean_requests"] == 0
    assert replay["backend_constructed"] is False
    assert replay["resource_claimed"] is False


def test_audit_retries_only_infrastructure_and_not_deterministic_invalid(
    tmp_path: Path,
) -> None:
    retry_settings = _tiny_audit_settings(tmp_path / "retry")
    retry_factory = FakeAuditFactory(crashes_remaining=1)
    retry_result = CompilerAuditRunner(
        retry_settings,
        backend_factory=retry_factory,
        manage_resources=False,
        verify_project=False,
    ).run()
    assert retry_result.lean_requests >= 2
    assert retry_factory.constructions == 2

    invalid_settings = _tiny_audit_settings(tmp_path / "invalid")
    invalid_factory = FakeAuditFactory(status=LeanStatus.INVALID)
    invalid_result = CompilerAuditRunner(
        invalid_settings,
        backend_factory=invalid_factory,
        manage_resources=False,
        verify_project=False,
    ).run()
    assert invalid_result.incompatible == 3
    assert invalid_factory.constructions == 1
    assert max(Counter(invalid_factory.request_ids).values()) == 1


def test_audit_cache_drift_and_direct_leaninteract_import_fail_closed(tmp_path: Path) -> None:
    settings = _tiny_audit_settings(tmp_path)
    result = CompilerAuditRunner(
        settings,
        backend_factory=FakeAuditFactory(),
        manage_resources=False,
        verify_project=False,
    ).run()
    complete = json.loads(result.complete_path.read_text(encoding="utf-8"))
    first_cache = settings.output_root / "cache" / complete["cache_receipts"][0]["cache_key"][:2]
    first_cache /= complete["cache_receipts"][0]["cache_key"] + ".json"
    with first_cache.open("ab") as handle:
        handle.write(b"drift")
    with pytest.raises(CompilerReplayError, match="cannot read immutable audit cache"):
        CompilerAuditRunner(
            settings,
            backend_factory=FakeAuditFactory(),
            manage_resources=False,
            verify_project=False,
        ).replay()

    module_path = ROOT / "src/leanfaith/sft1/sprint/compiler_replay.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    direct_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    direct_imports.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not {name for name in direct_imports if name == "lean_interact"}
    assert "leanfaith.lean.leaninteract_backend" in direct_imports


def _typed_spec() -> CompilerTypedHookSpec:
    return CompilerTypedHookSpec(
        operations=("P18_SYMMETRIZE_EQUALITY_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"),
        orbit_operations=("ORBIT_WAVE4_N25_V1",),
        maximum_depth=3,
        maximum_variants_per_orbit=5,
        selection_salt="typed-hook-test-v1",
    )


def test_typed_hook_request_binds_exact_local_source_and_shared_engines(tmp_path: Path) -> None:
    settings = _tiny_audit_settings(tmp_path)
    records = list(iter_inventory_records(settings.inventory.output_root))
    alpha = next(row for row in records if row["declaration"]["name"] == "Alpha")
    source = resolve_audit_sources(settings, [alpha])[0]
    prepared = build_typed_descriptor_request(
        source,
        settings=settings,
        spec=_typed_spec(),
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    code = prepared.request.code
    assert isinstance(code, str)
    binding = json.loads(prepared.request.metadata["typed_hook_source_binding"])
    assert code.index("import Mathlib") < code.index("namespace LeanFaith.SFT1.Sprint")
    assert code.index("namespace LeanFaith.SFT1.Sprint") < code.index(source.declaration_source)
    assert code.index(source.declaration_source) < code.rindex("processCompilerRoot")
    assert "loadCompilerRootChecked" in code
    assert "buildWave4Descriptors root sop maximumDepth" in code
    assert binding["rootId"] == source.root_id
    assert binding["proofSourceSha256"] == source.inventory_record["hashes"]["body_sha256"]
    assert (
        binding["typeSourceSha256"]
        == source.inventory_record["hashes"]["normalized_signature_sha256"]
    )
    assert binding["declarationSourceSha256"] == sha256_hex(
        source.declaration_source.encode("utf-8")
    )
    assert prepared.request.allow_sorry is False
    assert prepared.phase == "descriptor"


def test_typed_selected_request_is_bounded_and_uses_shared_certificate_path(
    tmp_path: Path,
) -> None:
    settings = _tiny_audit_settings(tmp_path)
    records = list(iter_inventory_records(settings.inventory.output_root))
    alpha = next(row for row in records if row["declaration"]["name"] == "Alpha")
    source = resolve_audit_sources(settings, [alpha])[0]
    prepared = build_typed_wave4_selected_request(
        source,
        settings=settings,
        spec=_typed_spec(),
        operation_id="ORBIT_WAVE4_N25_V1",
        selected_indices=(4, 1),
        render_scope_id="sft1-wave5-typed-test",
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    code = prepared.request.code
    assert isinstance(code, str)
    assert "rebuildSelectedCompilerWave4Orbits" in code
    assert "emitSelectedCompilerWave4Report" in code
    assert (
        code[code.index(source.declaration_source) :].count("LeanFaith.GoalV1.emitClosedProp") == 8
    )
    assert prepared.selected_indices == (4, 1)
    assert prepared.operation_id == "ORBIT_WAVE4_N25_V1"
    with pytest.raises(CompilerReplayError, match="outside its bound"):
        build_typed_wave4_selected_request(
            source,
            settings=settings,
            spec=_typed_spec(),
            operation_id="ORBIT_WAVE4_N25_V1",
            selected_indices=tuple(range(6)),
            render_scope_id="sft1-wave5-typed-test",
            context_id="ctx:" + "1" * 64,
            timeout_seconds=30,
            run_id="2" * 64,
        )


def test_typed_descriptor_payload_parser_binds_root_and_every_orbit(tmp_path: Path) -> None:
    settings = _tiny_audit_settings(tmp_path)
    records = list(iter_inventory_records(settings.inventory.output_root))
    alpha = next(row for row in records if row["declaration"]["name"] == "Alpha")
    source = resolve_audit_sources(settings, [alpha])[0]
    binding = {"root_id": source.root_id}
    messages = [
        {
            "severity": "info",
            "data": SPRINT_EVIDENCE_MARKER
            + json.dumps(
                {
                    "kind": "compiler_root",
                    "root": source.qualified_name,
                    "root_status": "ok",
                    "compiler_source_binding": binding,
                }
            ),
        },
        {
            "severity": "info",
            "data": SPRINT_EVIDENCE_MARKER
            + json.dumps(
                {
                    "kind": "wave4_descriptor_root",
                    "root": source.qualified_name,
                    "operation_id": "ORBIT_WAVE4_N25_V1",
                    "status": "not_applicable",
                    "compiler_source_binding": binding,
                }
            ),
        },
    ]
    root, descriptors = parse_typed_descriptor_payloads(source, _typed_spec(), messages)
    assert root["root_status"] == "ok"
    assert descriptors["ORBIT_WAVE4_N25_V1"]["status"] == "not_applicable"
