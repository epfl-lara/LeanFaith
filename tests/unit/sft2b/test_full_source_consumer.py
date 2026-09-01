from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.full_source_consumer import (
    CORE_SHARD,
    TAIL_SHARD,
    FullSourceConsumerError,
    FullSourceConsumerSpec,
    FullSourceJournal,
    Matched500GateSpec,
    ReleaseFilePin,
    SourceShardSpec,
    build_detached_launch,
    build_run_plan,
    compact_completed,
    load_consumer_spec,
    terminal_cache_path,
    verify_compaction,
    verify_matched_500_gate,
    verify_source_views,
    write_cached_terminal,
)
from leanfaith.sft2b.schemas import (
    CandidateSlot,
    CompileContextRecord,
    SourceProvenance,
    SourceRecord,
    stable_id,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_CONFIG = _REPO_ROOT / "configs/sft2b/reform_diverse_full_consumer_v1.json"


def _source(index: int) -> SourceRecord:
    nl = f"Prove the standalone full-source fixture statement numbered {index}."
    theorem_id = f"test:full-source:{index}"
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
        reference_declaration_name=f"full_source_{index}",
        reference_proposition=proposition,
        reference_proposition_sha256=sha256_hex(proposition.encode()),
        compile_context=CompileContextRecord(
            source_context_id=f"ctx:{index:064x}",
            render_compile_context_id=f"ctx:{index + 100:064x}",
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
            source_url="https://example.invalid/full-source",
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


def _pinned_fixture(
    tmp_path: Path, *, core_rows: int = 2, tail_rows: int = 1
) -> tuple[FullSourceConsumerSpec, Path, tuple[SourceRecord, ...]]:
    original, _ = load_consumer_spec(_CONFIG)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    rows = tuple(_source(index) for index in range(core_rows + tail_rows))
    (bundle / "sources.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row.model_dump(mode="json")) + b"\n" for row in rows)
    )
    core_ids = [item.source_id for item in rows[:core_rows]]
    tail_ids = [item.source_id for item in rows[core_rows:]]
    (bundle / "matched_50000_source_ids.json").write_bytes(
        canonical_json_bytes(
            {"schema_version": "test", "source_count": core_rows, "source_ids": core_ids}
        )
        + b"\n"
    )
    (bundle / "legacy_tail_source_ids.json").write_bytes(
        canonical_json_bytes(
            {"schema_version": "test", "source_count": tail_rows, "source_ids": tail_ids}
        )
        + b"\n"
    )
    (bundle / "source_manifest.json").write_text("{}\n", encoding="utf-8")
    covered = (
        "legacy_tail_source_ids.json",
        "matched_50000_source_ids.json",
        "source_manifest.json",
        "sources.jsonl",
    )
    (bundle / "SHA256SUMS").write_text(
        "".join(f"{hash_file(bundle / name)}  {name}\n" for name in covered), encoding="utf-8"
    )
    file_pins = tuple(
        ReleaseFilePin(path=item.path, sha256=hash_file(bundle / item.path))
        for item in original.input.files
    )
    core = SourceShardSpec(
        shard_id=CORE_SHARD,
        id_view_path="matched_50000_source_ids.json",
        id_view_sha256=hash_file(bundle / "matched_50000_source_ids.json"),
        expected_rows=core_rows,
    )
    tail = SourceShardSpec(
        shard_id=TAIL_SHARD,
        id_view_path="legacy_tail_source_ids.json",
        id_view_sha256=hash_file(bundle / "legacy_tail_source_ids.json"),
        expected_rows=tail_rows,
    )
    # Bypass only the production 50K cardinality for compact unit fixtures.  All
    # partition, Cartesian-product, cache, and resume contracts remain real.
    input_spec = original.input.model_copy(
        update={
            "revision": "8" * 40,
            "files": file_pins,
            "expected_source_rows": len(rows),
            "shards": (core, tail),
        }
    )
    spec = original.model_copy(update={"input": input_spec})
    return spec, bundle, rows


def test_checked_in_config_is_pinned_but_strictly_waiting() -> None:
    spec, _ = load_consumer_spec(_CONFIG)

    assert spec.status == "waiting_matched_500_report"
    assert spec.input.revision == "d0b961d2112d186009984242db674f2ad59905c7"
    assert spec.input.expected_source_rows == 54621
    assert spec.input.shards[0].expected_rows == 50000
    assert spec.input.shards[1].expected_rows == 4621
    assert spec.matched_500_gate.decision == "pending"
    with pytest.raises(FullSourceConsumerError, match="self-attested matched receipt"):
        verify_matched_500_gate(_REPO_ROOT, spec)


def test_source_views_are_disjoint_ordered_and_cover_release(tmp_path: Path) -> None:
    spec, bundle, rows = _pinned_fixture(tmp_path)
    verified = verify_source_views(spec, bundle_root=bundle)

    assert verified.source_ids == tuple(item.source_id for item in rows)
    assert verified.shard_source_ids[CORE_SHARD] == tuple(item.source_id for item in rows[:2])
    assert verified.shard_source_ids[TAIL_SHARD] == (rows[2].source_id,)

    tail = json.loads((bundle / "legacy_tail_source_ids.json").read_text())
    tail["source_ids"] = [rows[0].source_id]
    (bundle / "legacy_tail_source_ids.json").write_bytes(canonical_json_bytes(tail) + b"\n")
    with pytest.raises(FullSourceConsumerError, match="hash mismatch"):
        verify_source_views(spec, bundle_root=bundle)


def test_run_plan_is_content_addressed_complete_four_slot_product(tmp_path: Path) -> None:
    spec, bundle, _ = _pinned_fixture(tmp_path)
    verified = verify_source_views(spec, bundle_root=bundle)
    plan = build_run_plan(
        spec,
        config_sha256="9" * 64,
        shard_id=CORE_SHARD,
        source_ids=verified.shard_source_ids[CORE_SHARD],
    )

    assert len(plan.source_ids) == 2
    assert len(plan.cells) == 8
    assert tuple(item.slot for item in plan.cells[:4]) == tuple(CandidateSlot)
    assert tuple(item.seed for item in plan.cells[:4]) == (0, 1, 2, 3)
    assert len({item.cell_id for item in plan.cells}) == 8
    assert (
        build_run_plan(
            spec,
            config_sha256="9" * 64,
            shard_id=CORE_SHARD,
            source_ids=verified.shard_source_ids[CORE_SHARD],
        ).run_id
        == plan.run_id
    )
    path = terminal_cache_path(tmp_path / "cache", plan, plan.cells[0])
    assert plan.run_id in path.parts
    assert path.name == "terminal.json"


def test_journal_resume_suppresses_duplicates_and_compacts_only_when_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, bundle, _ = _pinned_fixture(tmp_path, core_rows=1, tail_rows=1)
    verified = verify_source_views(spec, bundle_root=bundle)
    plan = build_run_plan(
        spec,
        config_sha256="a" * 64,
        shard_id=CORE_SHARD,
        source_ids=verified.shard_source_ids[CORE_SHARD],
    )
    cache_root = tmp_path / "cache"
    journal = FullSourceJournal(
        tmp_path / "journal/requests.jsonl", plan=plan, cache_root=cache_root
    )

    first_path = write_cached_terminal(
        cache_root, plan, plan.cells[0], payload={"generated_text": "theorem candidate : True"}
    )
    assert journal.append_terminal(plan.cells[0], first_path)
    assert not journal.append_terminal(plan.cells[0], first_path)
    assert len(journal.missing_cells()) == 3
    with pytest.raises(FullSourceConsumerError, match="incomplete Cartesian product"):
        compact_completed(journal, tmp_path / "compacted.jsonl")

    for cell in journal.missing_cells():
        path = write_cached_terminal(
            cache_root, plan, cell, payload={"generated_text": f"candidate for {cell.slot}"}
        )
        assert journal.append_terminal(cell, path)
    result = compact_completed(journal, tmp_path / "compacted.jsonl")

    assert result.rows == 4
    assert result.sha256 == hash_file(result.path)
    assert compact_completed(journal, result.path) == result
    real_read_text = Path.read_text

    def forbid_compacted_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == result.path:
            raise AssertionError("compaction verification must stream")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_compacted_read_text)
    assert verify_compaction(plan, result.path, expected_sha256=result.sha256) == result


def test_legacy_self_attested_receipt_can_never_enable_tmux(tmp_path: Path) -> None:
    spec, bundle, _ = _pinned_fixture(tmp_path, core_rows=1, tail_rows=1)
    verified = verify_source_views(spec, bundle_root=bundle)
    plan = build_run_plan(
        spec,
        config_sha256="b" * 64,
        shard_id=CORE_SHARD,
        source_ids=verified.shard_source_ids[CORE_SHARD],
    )
    with pytest.raises(FullSourceConsumerError, match="self-attested matched receipt"):
        build_detached_launch(
            _REPO_ROOT,
            spec=spec,
            config_path=_CONFIG,
            bundle_root=bundle,
            plan=plan,
            run_root=tmp_path / "run",
        )

    receipt_path = tmp_path / "matched_500_receipt.json"
    receipt_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "sft2b_matched_500_runtime_quality_receipt_v1",
                "source_count": 500,
                "request_count": 2000,
                "complete_cartesian_product": True,
                "runtime_complete": True,
                "quality_review_complete": True,
                "quality_decision": "pass",
                "output_revision": "c" * 40,
                "runtime_report_sha256": "d" * 64,
                "quality_report_sha256": "e" * 64,
            }
        )
        + b"\n"
    )
    legacy_claim = spec.model_copy(
        update={
            "status": "scale_authorized",
            "matched_500_gate": Matched500GateSpec(
                receipt_path=str(receipt_path),
                receipt_sha256=hash_file(receipt_path),
                decision="pass",
                expected_sources=500,
                expected_requests=2000,
            ),
            "executor": spec.executor.model_copy(update={"argv": ("/usr/bin/true",)}),
        }
    )
    with pytest.raises(FullSourceConsumerError, match="self-attested matched receipt"):
        build_detached_launch(
            _REPO_ROOT,
            spec=legacy_claim,
            config_path=_CONFIG,
            bundle_root=bundle,
            plan=plan,
            run_root=tmp_path / "run",
        )
