"""Synthetic, no-Lean tests for the Wave 5 proof-certified scale runner."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanStatus
from leanfaith.sft1.sprint import compiler_scale as scale_module
from leanfaith.sft1.sprint.compiler_inventory import (
    CompilerProjectContext,
    Cpt2ReleasePin,
    InputShard,
    InventorySettings,
)
from leanfaith.sft1.sprint.compiler_replay import (
    DOWNSTREAM_MODE,
    CompilerAuditSettings,
    CompilerAuditSource,
    CompilerTypedHookSpec,
)
from leanfaith.sft1.sprint.compiler_scale import (
    CompilerScaleError,
    CompilerScaleExecutor,
    CompilerScaleRootOutcome,
    CompilerScaleRunner,
    CompilerScaleSettings,
    _runtime_migration_required,
    _scale_chunks,
    build_eligible_selection,
)
from leanfaith.sft1.sprint.orbit import OrbitPolicy
from leanfaith.sft1.sprint.publish import PublishError, local_files
from leanfaith.sft1.sprint.screens import render_hash, unordered_pair_key
from leanfaith.sft1.sprint.square import (
    materialize_wave4_records,
    select_wave4_release_groups,
)

ROOT = find_repo_root(Path(__file__))
WAVE4_CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
ENGINE = ROOT / "LeanFaith/Meta/SFT1/Sprint.lean"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _inventory_record(index: int) -> dict[str, Any]:
    root_id = hash_canonical(["compiler-root", index])
    source_row_id = hash_canonical(["source-row", index])
    record: dict[str, Any] = {
        "schema_version": "sft1_cpt2_compiler_inventory_v1",
        "root_id": root_id,
        "source_row_id": source_row_id,
        "normalized_group_id": hash_canonical(["normalized", index]),
        "source": {
            "release_id": "a" * 64,
            "split": "train",
            "part": 0,
            "shard_file": "train-00000.parquet",
            "shard_sha256": "b" * 64,
            "row_index": index,
        },
        "hashes": {
            "theorem_sha256": hash_canonical(["theorem", index]),
            "body_sha256": hash_canonical(["body", index]),
            "full_source_sha256": hash_canonical(["source", index]),
            "exact_signature_sha256": hash_canonical(["exact", index]),
            "normalized_signature_sha256": hash_canonical(["signature", index]),
        },
        "declaration": {
            "kind": "theorem",
            "name": f"Root{index}",
            "name_is_rooted": False,
            "qualified_name_candidate": f"Root{index}",
            "qualified_name_status": "simple_namespace_stack_v1",
            "offset": 15,
            "assignment_offset": 38,
        },
        "context": {
            "context_sha256": hash_canonical(["context", index]),
            "context_fingerprint": hash_canonical(["context-fingerprint", index]),
            "project_fingerprint": "c" * 64,
            "imports": ["Mathlib"],
            "option_commands": [],
            "open_commands": [],
            "include_commands": [],
            "namespace_stack": [],
            "namespace_status": "simple_namespace_stack_v1",
            "preceding_declarations": 0,
            "context_complexity": "no_preceding_declarations",
        },
        "features": ["equality", "numeral"],
        "lengths": {
            "theorem_characters": 40,
            "body_characters": 4,
            "full_source_characters": 46,
            "signature_characters": 14,
            "theorem_stratum": "000_128",
            "body_stratum": "000_128",
            "full_source_stratum": "000_128",
            "signature_stratum": "000_128",
        },
        "dedup": {
            "winner_exact_proof_count": 1,
            "normalized_exact_group_count": 1,
            "normalized_proof_count": 1,
        },
    }
    record["inventory_record_sha256"] = hash_canonical(record)
    return record


def _settings(tmp_path: Path, *, roots: int = 3, roots_per_shard: int = 1) -> CompilerScaleSettings:
    inventory_root = tmp_path / "inventory"
    rows = [_inventory_record(index) for index in range(roots)]
    shard = inventory_root / "inventory" / "part-00000.jsonl"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    _write_json(
        inventory_root / "manifest.json",
        {
            "artifact_kind": "sft1_cpt2_compiler_inventory",
            "run_id": "d" * 64,
            "output": {
                "shards": [
                    {
                        "part": 0,
                        "file": shard.name,
                        "rows": roots,
                        "sha256": hash_file(shard),
                    }
                ]
            },
        },
    )
    blocklist = tmp_path / "gold.json"
    _write_json(blocklist, {"near_dup_hashes": [], "group_keys": []})
    manifest_stub = tmp_path / "cpt2-manifest.json"
    publication_stub = tmp_path / "cpt2-publication.json"
    _write_json(manifest_stub, {"fixture": True})
    _write_json(publication_stub, {"fixture": True})
    pin = Cpt2ReleasePin(
        repo_id="example/cpt2",
        final_revision="1" * 40,
        data_commit="2" * 40,
        manifest_sha256=hash_file(manifest_stub),
        publication_receipt_sha256=hash_file(publication_stub),
        release_tree_sha256="3" * 64,
        cpt2_run_id="4" * 64,
        source_repo_id="example/compiler",
        source_revision="5" * 40,
        source_parquet_path="source.parquet",
        source_parquet_sha256="6" * 64,
        expected_release_rows=roots,
        expected_valid_rows=roots,
        expected_valid_exact_prefixes=roots,
    )
    project = CompilerProjectContext(
        project_id="mathlib",
        project_revision="7" * 40,
        lean_version="v4.test",
        lean_interact_version="test",
        repl_revision="example/repl@test",
        checker_version="test-checker",
    )
    inventory = InventorySettings(
        release_root=tmp_path / "unused-release",
        manifest_path=manifest_stub,
        publication_receipt_path=publication_stub,
        output_root=inventory_root,
        gold_blocklist_path=blocklist,
        gold_blocklist_sha256=hash_file(blocklist),
        pin=pin,
        project=project,
        audit_sample=None,
        config_sha256="8" * 64,
        output_shards=1,
        batch_rows=8,
    )
    config_path = tmp_path / "wave5.yaml"
    config_path.write_text("fixture: wave5-scale\n", encoding="utf-8")
    audit = CompilerAuditSettings(
        inventory=inventory,
        config_path=config_path,
        config_sha256=hash_file(config_path),
        output_root=tmp_path / "audit",
        project_dir=tmp_path / "unused-project",
        engine_path=ENGINE,
        resource_task="SFT1-WAVE5-SCALE-TEST",
        lean_workers=2,
        lean_rss_claim_gib=40,
        memory_hard_limit_mb=24_576,
        request_timeout_seconds=30,
        context_request_max_roots=8,
        request_batch_size=8,
        retry_max_attempts=2,
        retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
        terminal_marker="complete.json",
        expected_rows=1,
        elab_async=False,
        isolate_incremental_commands=True,
        downstream_mode=DOWNSTREAM_MODE,
    )
    _write_json(
        audit.complete_path,
        {
            "artifact_kind": "sft1_wave5_compiler_context_audit_terminal",
            "run_id": "9" * 64,
            "status": "passed",
            "roots": 1,
            "compatible": 1,
            "incompatible": 0,
            "proof_contract": {
                "exact_prefix_plus_literal_by_plus_body": True,
                "source_label_true": True,
                "qualified_local_theorem_resolved": True,
                "meta_and_kernel_source_proof_checked": True,
            },
        },
    )
    return CompilerScaleSettings(
        audit=audit,
        wave4_config_path=WAVE4_CONFIG,
        output_root=tmp_path / "scale",
        typed_spec=CompilerTypedHookSpec(
            operations=("N31_DROP_REQUIRED_GUARD_PROOF_V1",),
            orbit_operations=("ORBIT_WAVE4_N31_V1",),
            maximum_depth=3,
            maximum_variants_per_orbit=5,
            selection_salt="fixture-typed-selection",
        ),
        root_ceiling=roots,
        maximum_release_rows=100,
        checkpoints=(100,),
        roots_per_shard=roots_per_shard,
        root_batch_size=1,
        selection_salt="fixture-scale-selection",
        projected_runtime_limit_hours=1000,
    )


def _audit_replay(settings: CompilerAuditSettings) -> Mapping[str, Any]:
    return {
        "run_id": json.loads(settings.complete_path.read_text())["run_id"],
        "roots_verified": settings.expected_rows,
        "lean_requests": 0,
        "backend_constructed": False,
        "resource_claimed": False,
    }


def _typed_gate_verifier(
    settings: CompilerAuditSettings,
    spec: CompilerTypedHookSpec,
    policy: OrbitPolicy,
) -> Mapping[str, Any]:
    del spec, policy
    return {
        "passed": True,
        "terminal_path": str(settings.output_root / "typed_certificate_gate/complete.json"),
        "terminal_sha256": "a" * 64,
        "audit_sample_sha256": "b" * 64,
        "manual_inspection_verdict": "synthetic_fixture_passed",
        "replay": {
            "forced_resume": True,
            "lean_requests": 0,
            "backend_constructed": False,
        },
    }


def _source_resolver(
    settings: CompilerAuditSettings, records: Sequence[Mapping[str, Any]]
) -> Sequence[CompilerAuditSource]:
    sources: list[CompilerAuditSource] = []
    for record in records:
        index = int(cast(Mapping[str, Any], record["source"])["row_index"])
        theorem = f"import Mathlib\ntheorem Root{index} : True := "
        body = " trivial"
        sources.append(
            CompilerAuditSource(
                inventory_record=record,
                shard=InputShard(
                    part=0,
                    total_parts=1,
                    split="train",
                    file="train-00000.parquet",
                    path=settings.inventory.release_root / "train-00000.parquet",
                    sha256="b" * 64,
                    rows=len(records),
                    valid_rows=len(records),
                ),
                theorem=theorem,
                body=body,
                full_source=theorem + "by" + body,
                context_prefix="import Mathlib\n",
                declaration_source=f"theorem Root{index} : True := by{body}",
                qualified_name=f"Root{index}",
            )
        )
    return sources


def _closure(
    root_id: str,
    *,
    text_seed: str,
    negative_operation: str = "N31_DROP_REQUIRED_GUARD_PROOF_V1",
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    operation_id = "ORBIT_WAVE4_N31_V1"
    if negative_operation == "N25_TOGGLE_EQ_NE_PROOF_V1":
        operation_id = "ORBIT_WAVE4_N25_V1"
    group_id = "variant:" + hash_canonical([root_id, text_seed, negative_operation])
    role_rows = (
        ("preserving_reference", True, f"⊢ A_{text_seed}", f"⊢ B_{text_seed}"),
        ("preserving_candidate", True, f"⊢ C_{text_seed}", f"⊢ D_{text_seed}"),
        ("negative_base", False, f"⊢ C_{text_seed}", f"⊢ B_{text_seed}"),
        ("negative_last", False, f"⊢ A_{text_seed}", f"⊢ D_{text_seed}"),
    )
    negative_evidence = {
        "boundary": {"checked": True},
        "separator": {"checked": True},
        "witnesses": [{"value": "0"}],
        "witness_checks": [{"checked": True}],
        "enumeration": {"complete": True},
    }
    replay = {"exact_operation_replayed": True, "checked": True}
    records: list[dict[str, Any]] = []
    logical: dict[str, str] = {}
    for role, label, reference, candidate in role_rows:
        pair_id = "pair:" + hash_canonical([root_id, role, text_seed, negative_operation])
        logical[role] = pair_id
        evidence: dict[str, Any] = {"role": role, "checked": True}
        if role in {"negative_base", "negative_last"}:
            evidence["negative_family_evidence"] = negative_evidence
        if role == "negative_last":
            evidence["negative_last_replay"] = replay
        sidecar: dict[str, Any] = {
            "pair_id": pair_id,
            "root_id": root_id,
            "root_name": f"Compiler.{root_id[:8]}",
            "operation_id": operation_id,
            "negative_operation": negative_operation,
            "mechanism": negative_operation.split("_", 1)[0],
            "row_kind": role,
            "closure_group_ids": [group_id],
            "evidence": evidence,
            "evidence_hash": hash_canonical(evidence),
            "implementation_commit": "f" * 40,
        }
        row = {"reference": reference, "candidate": candidate, "label": label}
        row_hash = hash_canonical(
            {
                "kind": "sft1_wave4_retained_row_v1",
                "row": row,
                "pair_id": pair_id,
                "evidence_hash": sidecar["evidence_hash"],
                "closure_group_ids": [group_id],
            }
        )
        records.append(
            {
                "row": row,
                "sidecar": sidecar,
                "unordered_pair_key": unordered_pair_key(
                    render_hash(reference), render_hash(candidate)
                ),
                "row_hash": row_hash,
                "label": label,
                "operation_id": operation_id,
                "root_name": sidecar["root_name"],
                "mechanism": sidecar["mechanism"],
            }
        )

    def digest(value: str) -> str:
        return hash_canonical([root_id, text_seed, value])

    mechanism = "N25" if negative_operation.startswith("N25") else "N31"
    group = {
        "schema_version": 1,
        "group_id": group_id,
        "root_id": root_id,
        "operation_id": operation_id,
        "negative_operation": negative_operation,
        "negative_mechanism": mechanism,
        "selection_hash": digest("selection"),
        "content_hash": digest("content"),
        "depth": 1,
        "reference_chain_hash": digest("reference-chain"),
        "candidate_chain_hash": digest("candidate-chain"),
        "reference_site_hash": digest("reference-site"),
        "candidate_site_hash": digest("candidate-site"),
        "reference_operation_chain": ["P18_SYMMETRIZE_EQUALITY_V1"],
        "candidate_operation_chain": ["P18_SYMMETRIZE_EQUALITY_V1"],
        "preserving_mechanism_chain": ["P18"],
        "preserving_superclass_chain": ["relation_symmetry"],
        "base_negative_evidence_hash": hash_canonical(negative_evidence),
        "negative_last_replay_hash": hash_canonical(replay),
        "logical_pair_ids": logical,
        "closure_certificate_hash": digest("closure"),
    }
    return tuple(records), (group,)


@dataclass
class _ExecutionState:
    calls: list[tuple[str, ...]]
    fail_on_call: int | None = None
    duplicate_text: bool = False
    negative_operations: tuple[str, ...] = ("N31_DROP_REQUIRED_GUARD_PROOF_V1",)


class _FakeExecutor(CompilerScaleExecutor):
    def __init__(self, state: _ExecutionState) -> None:
        self.state = state
        self.closed = False

    def execute_batch(
        self, sources: Sequence[CompilerAuditSource], *, run_id: str
    ) -> Sequence[CompilerScaleRootOutcome]:
        del run_id
        self.state.calls.append(tuple(source.root_id for source in sources))
        if self.state.fail_on_call == len(self.state.calls):
            raise RuntimeError("injected interruption")
        outcomes: list[CompilerScaleRootOutcome] = []
        for source in sources:
            seed = "duplicate" if self.state.duplicate_text else source.root_id[:8]
            source_index = int(
                cast(Mapping[str, Any], source.inventory_record["source"])["row_index"]
            )
            negative_operation = self.state.negative_operations[
                source_index % len(self.state.negative_operations)
            ]
            records, groups = _closure(
                source.root_id,
                text_seed=seed,
                negative_operation=negative_operation,
            )
            outcomes.append(
                CompilerScaleRootOutcome(
                    root_id=source.root_id,
                    status="retained",
                    taxonomy="proof_certified_wave4_closure",
                    records=records,
                    groups=groups,
                    proof_summary={
                        "source_proof_check": {
                            "meta_checked": True,
                            "kernel_checked": True,
                            "kernel_level_instantiation": "none",
                            "proof_expr_hash_u64": "42",
                        },
                        "engine_semantic_version": "test-engine-v1",
                    },
                    request_hashes=(hash_canonical(["request", source.root_id]),),
                    lean_requests=2,
                    lean_elapsed_ms=10,
                )
            )
        return outcomes

    def close(self) -> None:
        self.closed = True


def _runner(settings: CompilerScaleSettings, state: _ExecutionState) -> CompilerScaleRunner:
    return CompilerScaleRunner(
        settings,
        executor_factory=lambda: _FakeExecutor(state),
        audit_replay=_audit_replay,
        typed_gate_verifier=_typed_gate_verifier,
        source_resolver=_source_resolver,
        manage_resources=False,
    )


def _pass_checkpoint_screens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scale_module.shortcut_module,
        "run_screens_v3",
        lambda records: {
            "rows": len(records),
            "screens": [
                {"name": name, "passed": True}
                for name in ("candidate_only", "reference_only", "family_held_out")
            ],
            "passed": True,
        },
    )


def test_zero_lean_selection_is_stable_bounded_and_content_bound(tmp_path: Path) -> None:
    settings = _settings(tmp_path, roots=4)
    settings = replace(settings, root_ceiling=2)
    first = build_eligible_selection(settings)
    before = settings.selection_path.read_bytes()
    second = build_eligible_selection(settings)
    assert first == second
    assert len(first) == 2
    assert settings.selection_path.read_bytes() == before
    receipt = json.loads(settings.selection_receipt_path.read_text())
    assert receipt["lean_calls"] == 0
    assert receipt["selected_rows"] == 2


def test_scale_chunks_preserve_one_hundred_and_ten_thousand_pilots() -> None:
    chunks = _scale_chunks(tuple(range(10_300)), 256)
    cumulative: list[int] = []
    total = 0
    for chunk in chunks:
        assert len(chunk) <= 256
        total += len(chunk)
        cumulative.append(total)
    assert 1 in cumulative
    assert 100 in cumulative
    assert 10_000 in cumulative
    assert cumulative[-1] == 10_300


def test_failed_audit_blocks_selection_and_executor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    marker = json.loads(settings.audit.complete_path.read_text())
    marker["status"] = "failed"
    marker["compatible"] = 0
    marker["incompatible"] = 1
    _write_json(settings.audit.complete_path, marker)
    state = _ExecutionState(calls=[])
    with pytest.raises(CompilerScaleError, match="audit gate differs"):
        _runner(settings, state).run()
    assert state.calls == []
    assert not settings.selection_path.exists()


def test_interruption_resume_duplicate_suppression_and_zero_call_replay(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, roots=3, roots_per_shard=1)
    interrupted = _ExecutionState(calls=[], fail_on_call=2, duplicate_text=True)
    with pytest.raises(RuntimeError, match="injected interruption"):
        _runner(settings, interrupted).run()
    first_receipt = settings.output_root / "_state/shards/shard-00000.json"
    assert first_receipt.is_file()
    first_cache_count = len(tuple((settings.output_root / "cache/roots").rglob("*.json")))
    assert first_cache_count == 1

    resumed_state = _ExecutionState(calls=[], duplicate_text=True)
    result = _runner(settings, resumed_state).run()
    assert result.processed_roots == 3
    assert result.released_rows == 4
    assert result.release_shards == 3
    assert len(resumed_state.calls) == 2
    receipts = [
        json.loads(path.read_text())
        for path in sorted((settings.output_root / "_state/shards").glob("*.json"))
    ]
    assert receipts[0]["rows"] == 4
    assert sum(len(receipt["duplicate_dropped_roots"]) for receipt in receipts) == 2
    assert receipts[0]["pair_delta_balance_report"]["passed"] is True
    assert receipts[0]["pair_delta_balance_report"]["policy"] == (
        "whole_ancestry_closure_inverse_pair_delta_match_v1"
    )
    assert all(
        receipt["pair_delta_balance_report_sha256"]
        == hash_canonical(receipt["pair_delta_balance_report"])
        for receipt in receipts
    )
    rows_path = settings.output_root / "release/shard-0001/rows.jsonl"
    assert all(
        set(json.loads(line)) == {"reference", "candidate", "label"}
        for line in rows_path.read_text().splitlines()
    )

    def explode() -> CompilerScaleExecutor:
        raise AssertionError("completed replay must not construct an executor")

    replay = CompilerScaleRunner(
        settings,
        executor_factory=explode,
        audit_replay=_audit_replay,
        typed_gate_verifier=_typed_gate_verifier,
        source_resolver=lambda _settings, _records: (_ for _ in ()).throw(
            AssertionError("completed replay must not resolve source Parquet")
        ),
        manage_resources=False,
    ).replay()
    assert replay["lean_requests"] == 0
    assert replay["backend_constructed"] is False
    assert replay["cache_hits"] == 3
    manifest = json.loads((settings.output_root / "release/manifest.json").read_text())
    assert manifest["shards"] == [
        json.loads(path.read_text())
        for path in sorted((settings.output_root / "release").glob("shard-*/manifest.json"))
    ]
    assert all(shard["complete"] and shard["finalized"] for shard in manifest["shards"])
    with pytest.raises(PublishError, match=r"release_report\.json did not pass"):
        local_files(settings.output_root / "release")


def test_post_balance_n25_cap_falls_back_before_shard_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, roots=5, roots_per_shard=256)
    state = _ExecutionState(
        calls=[],
        negative_operations=(
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "N31_DROP_REQUIRED_GUARD_PROOF_V1",
            "N25_TOGGLE_EQ_NE_PROOF_V1",
        ),
    )
    original_selector = select_wave4_release_groups
    observed_caps: list[float] = []

    def simulate_post_balance_n25_dominance(
        materialized: Any,
        *,
        maximum_rows: int | None,
        n25_maximum_share: float,
        selection_salt: str,
        enforce_pair_delta_balance: bool = False,
    ) -> Any:
        observed_caps.append(n25_maximum_share)
        n25_groups = tuple(
            group
            for group in materialized.groups
            if group.operation_id == "N25_TOGGLE_EQ_NE_PROOF_V1"
        )
        if n25_maximum_share and n25_groups:
            n25_pair_ids = {pair_id for group in n25_groups for pair_id in group.row_ids}
            n25_only = materialize_wave4_records(
                [
                    record
                    for record in materialized.rows
                    if cast(Mapping[str, Any], record["sidecar"])["pair_id"] in n25_pair_ids
                ],
                [group.record for group in n25_groups],
            )
            return original_selector(
                n25_only,
                maximum_rows=maximum_rows,
                n25_maximum_share=1.0,
                selection_salt=selection_salt,
                enforce_pair_delta_balance=enforce_pair_delta_balance,
            )
        return original_selector(
            materialized,
            maximum_rows=maximum_rows,
            n25_maximum_share=n25_maximum_share,
            selection_salt=selection_salt,
            enforce_pair_delta_balance=enforce_pair_delta_balance,
        )

    monkeypatch.setattr(
        scale_module,
        "select_wave4_release_groups",
        simulate_post_balance_n25_dominance,
    )
    result = _runner(settings, state).run()
    receipts = [
        json.loads(path.read_text())
        for path in sorted((settings.output_root / "_state/shards").glob("*.json"))
    ]
    guard = receipts[1]["pair_delta_balance_report"]["post_balance_n25_guard"]
    assert result.released_rows == 16
    assert 0.0 in observed_caps
    assert guard["fallback"] == "drop_all_n25_then_rebalance"
    assert guard["initial_n25_rows"] == guard["initial_rows"] == 4
    assert guard["final_n25_rows"] == 0
    assert guard["passed"] is True
    assert receipts[1]["n25_rows"] == 0


def test_root_terminal_journal_uses_one_index_scan_and_batched_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(_settings(tmp_path, roots=3, roots_per_shard=256), root_batch_size=2)
    runner = _runner(settings, _ExecutionState(calls=[]))
    original_read = runner.journal.read
    read_count = 0

    def counted_read() -> Any:
        nonlocal read_count
        read_count += 1
        return original_read()

    appended_batches: list[tuple[Mapping[str, object], ...]] = []
    original_append_many = runner.journal.append_many

    def tracked_append_many(records: Sequence[Mapping[str, object]]) -> None:
        appended_batches.append(tuple(records))
        original_append_many(records)

    monkeypatch.setattr(runner.journal, "read", counted_read)
    monkeypatch.setattr(runner.journal, "append_many", tracked_append_many)
    runner.run()

    assert read_count == 1
    assert [len(batch) for batch in appended_batches] == [1, 2]
    assert all(event["event"] == "root_terminal" for batch in appended_batches for event in batch)


def test_orphan_cache_resume_preserves_lean_calls_and_honest_startup_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, roots=1)
    original = scale_module._write_release_shard
    monkeypatch.setattr(
        scale_module,
        "_write_release_shard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("after-cache crash")),
    )
    with pytest.raises(RuntimeError, match="after-cache crash"):
        _runner(settings, _ExecutionState(calls=[])).run()
    monkeypatch.setattr(scale_module, "_write_release_shard", original)
    resumed = _ExecutionState(calls=[])
    _runner(settings, resumed).run()
    milestone = json.loads(
        (settings.output_root / "_state/milestones/roots-0000000001.json").read_text()
    )
    assert resumed.calls == []
    assert milestone["lean_requests"] == 2
    assert milestone["cache_hits"] == 1
    assert milestone["cache_hit_rate"] == 1.0
    assert milestone["executor_construction_seconds"] >= 0
    assert milestone["first_lean_batch_wall_seconds"] >= 0
    assert "includes lazy Lean backend startup" in milestone["startup_measurement"]


def test_replay_fails_closed_on_cache_and_shard_tampering(tmp_path: Path) -> None:
    settings = _settings(tmp_path, roots=1)
    state = _ExecutionState(calls=[])
    runner = _runner(settings, state)
    result = runner.run()
    assert result.released_rows == 4
    cache_path = next((settings.output_root / "cache/roots").rglob("*.json"))
    original_cache = cache_path.read_bytes()
    with cache_path.open("ab") as handle:
        handle.write(b"{}")
    with pytest.raises((CompilerScaleError, json.JSONDecodeError)):
        runner.replay()
    cache_path.write_bytes(original_cache)

    rows_path = settings.output_root / "release/shard-0001/rows.jsonl"
    with rows_path.open("ab") as handle:
        handle.write(b'{"reference":"tampered","candidate":"x","label":false}\n')
    with pytest.raises(CompilerScaleError, match="rows hash differs"):
        runner.replay()


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("release/shard-0001/sidecars.jsonl", "sidecars hash differs"),
        ("release/shard-0001/closure_groups.jsonl", "groups hash differs"),
        ("release/shard-0001/manifest.json", "manifest hash differs"),
    ],
)
def test_replay_fails_closed_on_every_shard_artifact(
    tmp_path: Path, relative: str, message: str
) -> None:
    settings = _settings(tmp_path, roots=1)
    runner = _runner(settings, _ExecutionState(calls=[]))
    runner.run()
    target = settings.output_root / relative
    target.write_bytes(target.read_bytes() + b"{}\n")
    with pytest.raises(CompilerScaleError, match=message):
        runner.replay()


def test_replay_fails_closed_on_receipt_tamper_and_noncontiguous_later_shard(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, roots=3, roots_per_shard=1)
    runner = _runner(settings, _ExecutionState(calls=[]))
    runner.run()
    receipt = settings.output_root / "_state/shards/shard-00000.json"
    original = json.loads(receipt.read_text())
    tampered = dict(original)
    tampered["input_record_hashes"] = ["0" * 64]
    _write_json(receipt, tampered)
    with pytest.raises(CompilerScaleError, match="input_record_hashes"):
        runner.replay()
    _write_json(receipt, original)

    middle = settings.output_root / "_state/shards/shard-00001.json"
    middle.unlink()
    with pytest.raises(CompilerScaleError, match="not a contiguous prefix"):
        runner.replay()


def test_below_checkpoint_reports_are_explicitly_not_publishable(tmp_path: Path) -> None:
    settings = _settings(tmp_path, roots=1)
    result = _runner(settings, _ExecutionState(calls=[])).run()
    assert result.status == "complete_below_first_checkpoint"
    release = json.loads((settings.output_root / "release/release_report.json").read_text())
    integrity = json.loads((settings.output_root / "release/integrity_report.json").read_text())
    assert release["passed"] is False
    assert release["checks"]["first_checkpoint_reached"] is False
    assert integrity["passed"] is False
    assert integrity["issues"]
    assert any(value > 0 for value in integrity["issue_counts"].values())
    with pytest.raises(PublishError):
        local_files(settings.output_root / "release")


def test_publish_pending_is_manifest_last_private_fresh_and_parent_chained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pass_checkpoint_screens(monkeypatch)
    settings = replace(
        _settings(tmp_path, roots=2, roots_per_shard=1),
        checkpoints=(4,),
    )
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()

    calls: list[dict[str, Any]] = []
    revisions = iter(("2" * 40, "3" * 40, "4" * 40))

    def upload(
        *,
        repo_id: str,
        local_root: Path,
        files: Sequence[Path],
        remote_prefix: str,
        commit_message: str,
        expected_parent: str | None,
    ) -> tuple[str, str, Mapping[str, str]]:
        revision = next(revisions)
        parent = expected_parent or "1" * 40
        calls.append(
            {
                "repo_id": repo_id,
                "local_root": local_root,
                "files": tuple(path.name for path in files),
                "remote_prefix": remote_prefix,
                "commit_message": commit_message,
                "expected_parent": expected_parent,
            }
        )
        return (
            revision,
            parent,
            {f"{remote_prefix}/{path.name}": hash_file(path) for path in files},
        )

    published = runner.publish_pending(uploader=upload)
    assert len(published) == 3
    assert calls[0]["repo_id"] == "Lemmy00/leanfaith-sft1-deterministic-v1"
    assert calls[0]["remote_prefix"] == "wave5/compiler_core_v1/shards/shard-0001"
    assert calls[0]["files"][-1] == "manifest.json"
    assert calls[0]["expected_parent"] is None
    assert calls[1]["remote_prefix"].endswith("checkpoints/rows-0000000004")
    assert calls[1]["files"][-1] == "manifest.json"
    assert calls[1]["expected_parent"] == "2" * 40
    assert calls[2]["expected_parent"] == "3" * 40
    assert published[2]["parent_revision"] == published[1]["revision"]
    assert all(receipt["private"] is True for receipt in published)
    assert all(receipt["fresh_remote_verification"] is True for receipt in published)
    assert all(receipt["overwrite_performed"] is False for receipt in published)
    checkpoint = json.loads(
        (
            settings.output_root / "_state/publication_receipts/checkpoints/rows-0000000004.json"
        ).read_text()
    )
    assert checkpoint["revision"] == "3" * 40
    assert checkpoint["metadata_kind"] == "checkpoint"
    assert runner.publish_pending(uploader=upload) == ()


@pytest.mark.parametrize(
    "failed_signal", ["shortcut", "pair_delta", "pair_delta_cell", "useful_families"]
)
def test_checkpoint_publication_fails_closed_on_release_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_signal: str
) -> None:
    _pass_checkpoint_screens(monkeypatch)
    settings = replace(_settings(tmp_path, roots=1), checkpoints=(4,))
    if failed_signal == "useful_families":
        settings = replace(
            settings,
            typed_spec=replace(
                settings.typed_spec,
                operations=(
                    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
                    "N26_INCREMENT_BOUND_PROOF_V1",
                    "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
                ),
            ),
        )
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    if failed_signal == "shortcut":
        monkeypatch.setattr(
            scale_module.shortcut_module,
            "run_screens_v3",
            lambda _records: {"screens": [], "passed": False},
        )
    elif failed_signal == "pair_delta":
        monkeypatch.setattr(
            scale_module.shortcut_module,
            "pairwise_shortcut_diagnostics",
            lambda records: {
                "rows": len(records),
                "rules": {"leak": {}},
                "max_balanced_accuracy": 0.9,
            },
        )
    elif failed_signal == "pair_delta_cell":
        monkeypatch.setattr(
            scale_module,
            "wave3_pair_delta",
            lambda record: {"cell": "positive" if record["row"]["label"] else "negative"},
        )

    def upload(**kwargs: Any) -> tuple[str, str, Mapping[str, str]]:
        files = cast(Sequence[Path], kwargs["files"])
        prefix = str(kwargs["remote_prefix"])
        return (
            "2" * 40,
            "1" * 40,
            {f"{prefix}/{path.name}": hash_file(path) for path in files},
        )

    with pytest.raises(CompilerScaleError, match="checkpoint integrity gate failed"):
        runner.publish_pending(maximum_shards=1, uploader=upload)
    assert (settings.output_root / "_state/publication_receipts/shard-0001.json").is_file()
    assert not (
        settings.output_root / "_state/publication_receipts/checkpoints/rows-0000000004.json"
    ).exists()


def test_publish_pending_refuses_wrong_parent_without_writing_receipt(
    tmp_path: Path,
) -> None:
    settings = replace(
        _settings(tmp_path, roots=2, roots_per_shard=1),
        checkpoints=(8,),
    )
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    invocation = 0

    def upload(
        *,
        repo_id: str,
        local_root: Path,
        files: Sequence[Path],
        remote_prefix: str,
        commit_message: str,
        expected_parent: str | None,
    ) -> tuple[str, str, Mapping[str, str]]:
        nonlocal invocation
        del repo_id, local_root, commit_message
        invocation += 1
        revision = ("2" if invocation == 1 else "3") * 40
        parent = "1" * 40 if invocation == 1 else "f" * 40
        return (
            revision,
            parent,
            {f"{remote_prefix}/{path.name}": hash_file(path) for path in files},
        )

    runner.publish_pending(maximum_shards=1, uploader=upload)
    with pytest.raises(CompilerScaleError, match="not a direct child"):
        runner.publish_pending(maximum_shards=1, uploader=upload)
    assert not (settings.output_root / "_state/publication_receipts/shard-0002.json").exists()


def test_timeout_recovery_verifies_immutable_tree_without_reupload(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path, roots=1), checkpoints=(4,))
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    calls = 0

    def verify_existing(
        *,
        repo_id: str,
        local_root: Path,
        files: Sequence[Path],
        remote_prefix: str,
        commit_message: str,
        revision: str,
        parent_revision: str,
    ) -> Mapping[str, str]:
        nonlocal calls
        del repo_id, local_root, commit_message, revision, parent_revision
        calls += 1
        return {f"{remote_prefix}/{path.name}": hash_file(path) for path in files}

    receipt = runner.recover_incremental_publication(
        shard=1,
        revision="2" * 40,
        parent_revision="1" * 40,
        verifier=verify_existing,
    )
    assert calls == 1
    assert receipt["upload_performed"] is False
    assert receipt["verification_method"] == "immutable_hub_tree_digest_recovery"
    same = runner.recover_incremental_publication(
        shard=1,
        revision="2" * 40,
        parent_revision="1" * 40,
        verifier=lambda **_kwargs: pytest.fail("idempotent recovery must not access Hub"),
    )
    assert same == receipt


def test_checkpoint_timeout_recovery_is_manifest_last_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pass_checkpoint_screens(monkeypatch)
    settings = replace(_settings(tmp_path, roots=1), checkpoints=(4,))
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()

    def shard_verify(**kwargs: Any) -> Mapping[str, str]:
        return {
            f"{kwargs['remote_prefix']}/{path.name}": hash_file(path) for path in kwargs["files"]
        }

    runner.recover_incremental_publication(
        shard=1,
        revision="2" * 40,
        parent_revision="1" * 40,
        verifier=shard_verify,
    )
    calls = 0

    def metadata_verify(**kwargs: Any) -> Mapping[str, str]:
        nonlocal calls
        calls += 1
        files = cast(Sequence[Path], kwargs["files"])
        assert files[-1].name == "manifest.json"
        return {f"{kwargs['remote_prefix']}/{path.name}": hash_file(path) for path in files}

    receipt = runner.recover_metadata_publication(
        metadata_kind="checkpoint",
        checkpoint=4,
        revision="3" * 40,
        parent_revision="2" * 40,
        verifier=metadata_verify,
    )
    assert receipt["upload_performed"] is False
    assert receipt["immutable_tree_verification"] is True
    assert calls == 1
    same = runner.recover_metadata_publication(
        metadata_kind="checkpoint",
        checkpoint=4,
        revision="3" * 40,
        parent_revision="2" * 40,
        verifier=lambda **_kwargs: pytest.fail("idempotent recovery must not access Hub"),
    )
    assert same == receipt
    assert any(
        event.get("event") == "checkpoint_publication_receipt_recovered"
        for event in runner.journal.read()
    )
    report = settings.output_root / "release/checkpoints/rows-0000000004/release_report.json"
    report.write_bytes(report.read_bytes() + b" ")
    with pytest.raises(CompilerScaleError, match="immutable Wave 5 evidence differs"):
        runner.publish_pending(uploader=lambda **_kwargs: pytest.fail("must fail before upload"))


def test_final_aggregate_recovery_is_manifest_last_replayable_and_no_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pass_checkpoint_screens(monkeypatch)
    settings = replace(_settings(tmp_path, roots=1), checkpoints=(4,))
    original_aggregates = scale_module._release_aggregates

    def diverse_aggregates(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = original_aggregates(*args, **kwargs)
        value["negative_family_groups"] = {
            **value["negative_family_groups"],
            "N26_INCREMENT_BOUND_PROOF_V1": 1,
            "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1": 1,
        }
        return value

    monkeypatch.setattr(scale_module, "_release_aggregates", diverse_aggregates)
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    revisions = iter(("2" * 40, "3" * 40))

    def upload(**kwargs: Any) -> tuple[str, str, Mapping[str, str]]:
        revision = next(revisions)
        parent = kwargs["expected_parent"] or "1" * 40
        root = cast(Path, kwargs["local_root"])
        prefix = str(kwargs["remote_prefix"])
        files = cast(Sequence[Path], kwargs["files"])
        return (
            revision,
            parent,
            {f"{prefix}/{path.relative_to(root).as_posix()}": hash_file(path) for path in files},
        )

    runner.publish_pending(uploader=upload)
    with pytest.raises(
        CompilerScaleError, match="aggregate manifest/report publication is pending"
    ):
        runner.run()
    recovery_calls = 0

    def verify(**kwargs: Any) -> Mapping[str, str]:
        nonlocal recovery_calls
        recovery_calls += 1
        root = cast(Path, kwargs["local_root"])
        files = cast(Sequence[Path], kwargs["files"])
        assert files[-1].name == "manifest.json"
        return {
            f"{kwargs['remote_prefix']}/{path.relative_to(root).as_posix()}": hash_file(path)
            for path in files
        }

    aggregate = runner.recover_metadata_publication(
        metadata_kind="aggregate",
        revision="4" * 40,
        parent_revision="3" * 40,
        verifier=verify,
    )
    assert aggregate["upload_performed"] is False
    assert recovery_calls == 1
    assert runner.run().status == "complete"
    assert (
        runner.publish_pending(uploader=lambda **_kwargs: pytest.fail("no duplicate upload")) == ()
    )
    assert (
        runner.recover_metadata_publication(
            metadata_kind="aggregate",
            revision="4" * 40,
            parent_revision="3" * 40,
            verifier=lambda **_kwargs: pytest.fail("idempotent recovery must not access Hub"),
        )
        == aggregate
    )


def test_runtime_projection_is_telemetry_at_100_and_a_decision_at_10000(
    tmp_path: Path,
) -> None:
    settings = replace(
        _settings(tmp_path, roots=100, roots_per_shard=256),
        projected_runtime_limit_hours=1e-12,
    )
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    status = json.loads(settings.status_path.read_text())
    assert status["processed_roots"] == 100
    assert status["runtime_projection"]["exceeds_local_runtime_limit"] is True
    assert not settings.migration_required_path.exists()
    assert not settings.complete_path.exists()
    projection = {"exceeds_local_runtime_limit": True}
    assert not _runtime_migration_required(processed_roots=100, projection=projection)
    assert _runtime_migration_required(processed_roots=10_000, projection=projection)
    assert not _runtime_migration_required(
        processed_roots=10_000,
        projection={"exceeds_local_runtime_limit": False},
    )


def test_pilot_milestone_receipts_are_immutable_and_release_bound(tmp_path: Path) -> None:
    settings = _settings(tmp_path, roots=100, roots_per_shard=256)
    runner = _runner(settings, _ExecutionState(calls=[]))
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    required = {
        "processed_roots",
        "released_rows",
        "elapsed_seconds",
        "executor_construction_seconds",
        "first_lean_batch_wall_seconds",
        "startup_measurement",
        "roots_per_second",
        "rows_per_second",
        "cache_hits",
        "cache_hit_rate",
        "lean_requests",
        "failure_taxonomy",
        "peak_rss_bytes",
        "projected_total_hours",
        "decision",
        "completed_shards_sha256",
    }
    state_paths = [
        settings.output_root / f"_state/milestones/roots-{roots:010d}.json" for roots in (1, 100)
    ]
    for state_path in state_paths:
        release_path = settings.output_root / "release/milestones" / state_path.name
        payload = json.loads(state_path.read_text())
        assert required <= payload.keys()
        assert release_path.read_bytes() == state_path.read_bytes()
    before = [path.read_bytes() for path in state_paths]
    with pytest.raises(CompilerScaleError, match="pending incremental shard publications"):
        runner.run()
    assert [path.read_bytes() for path in state_paths] == before
    state_paths[-1].write_bytes(state_paths[-1].read_bytes() + b" ")
    with pytest.raises(CompilerScaleError, match="immutable Wave 5 evidence differs"):
        runner.run()


def test_settings_forbid_n19_and_more_than_two_workers(tmp_path: Path) -> None:
    settings = _settings(tmp_path, roots=1)
    with pytest.raises(ValueError, match="two Lean workers"):
        replace(settings, audit=replace(settings.audit, lean_workers=3))
    with pytest.raises(ValueError, match="25 percent"):
        replace(settings, n25_maximum_share=0.251)
