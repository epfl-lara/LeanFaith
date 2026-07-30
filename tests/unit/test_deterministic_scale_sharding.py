"""Deterministic source sharding, journal receipts, and merge-audit tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas import QualityTier, make_id
from leanfaith.schemas.manifest import CodeState, collect_code_state
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.transforms.scale_materializer import (
    DeterministicScaleArtifacts,
    DeterministicScaleConfig,
    DeterministicScaleError,
    DeterministicScaleManifest,
    DeterministicScaleRunSpec,
    ScaleDraftResult,
    ScaleFailure,
    ScaleQuarantineRecord,
    ScaleSourceShard,
    _build_journal_receipt,
    _build_lean_replay_audit,
    _canonical_model_bytes,
    _journal_receipt_path,
    _load_journal_receipt,
    _project_records,
    _representation_payload_hash,
    _role_ordered_replay_inputs,
    _root_component_shard_assignments,
    _run_spec_payload,
    _selection_key,
    _shard_set_spec_payload,
    _source_shard_path,
    _tree_hash,
    _validate_shard_execution_policy,
    _write_new_atomic,
    _write_partitions,
    run_deterministic_scale_materialization,
)
from leanfaith.transforms.scale_merge import (
    DeterministicScaleMergeArtifacts,
    _reject_cross_shard_semantic_leakage,
    _replay_shard_with_lean,
    _validate_projected_semantic_lineage,
    merge_deterministic_scale_shards,
)
from tests.unit.record_factories import representation_record, theorem_record
from tests.unit.test_deterministic_scale_materializer import _accepted_source_shard

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_generic_merge_fixtures_from_lean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic fake shard fixtures exercise merge logic, not a real Lean tree."""
    from leanfaith.transforms import scale_merge

    monkeypatch.setattr(
        scale_merge,
        "_replay_shard_with_lean",
        lambda **_: None,
    )


def _source(index: int, *, shared_root: str | None = None) -> TheoremRecord:
    theorem_id = make_id("thm", {"scale-shard-source": index})
    ancestry_id = make_id("anc", {"scale-shard-ancestry": index})
    root = shared_root or ancestry_id
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(root,),
        declaration_name=f"t{index}",
        declaration_full_name=f"Fixture.t{index}",
        proof_stripped_declaration=f"theorem t{index} : True := by sorry",
        statement_content_hash=hash_canonical({"statement": index}),
    )


def _make_run_spec(
    *,
    theorem_path: Path,
    representation_path: Path,
    support_paths: dict[str, Path],
    config_path: Path,
    config_hash: str,
    universe: tuple[TheoremRecord, ...],
    assignments: tuple[int, ...],
    shard_index: int,
    code: CodeState,
) -> DeterministicScaleRunSpec:
    selected_ids = tuple(
        theorem.theorem_id
        for theorem, assignment in zip(universe, assignments, strict=True)
        if assignment == shard_index
    )
    data: dict[str, object] = {
        "schema_version": 2,
        "artifact_kind": "deterministic_scale_run_spec",
        "theorem_input_path": str(theorem_path),
        "theorem_input_sha256": hash_file(theorem_path),
        "representation_input_path": str(representation_path),
        "representation_input_sha256": hash_file(representation_path),
        "source_inventory_manifest_path": str(support_paths["inventory"]),
        "source_inventory_manifest_sha256": hash_file(support_paths["inventory"]),
        "theorem_upstream_manifest_path": str(support_paths["theorem_upstream"]),
        "theorem_upstream_manifest_sha256": hash_file(support_paths["theorem_upstream"]),
        "representation_upstream_manifest_path": str(support_paths["representation_upstream"]),
        "representation_upstream_manifest_sha256": hash_file(
            support_paths["representation_upstream"]
        ),
        "config_path": str(config_path),
        "config_hash": config_hash,
        "registry_hash": "3" * 64,
        "benchmark_manifest_path": str(support_paths["benchmark"]),
        "benchmark_manifest_sha256": hash_file(support_paths["benchmark"]),
        "context_id": theorem_record().context_id,
        "context_record_sha256": "4" * 64,
        "project_dir": str(support_paths["project"]),
        "project_revision": "5" * 40,
        "project_tree_hash": "6" * 40,
        "code": code,
        "shard_assignment_scheme": "root_component_greedy_v1",
        "shard_count": 2,
        "shard_index": shard_index,
        "source_universe_theorem_ids": tuple(theorem.theorem_id for theorem in universe),
        "source_shard_assignments": assignments,
        "selected_source_theorem_ids": selected_ids,
        "max_sources": None,
    }
    preliminary = DeterministicScaleRunSpec.model_validate(
        {
            "run_spec_hash": "0" * 64,
            "shard_set_spec_hash": "0" * 64,
            **data,
        }
    ).model_dump(mode="json")
    shard_set_spec_hash = hash_canonical(_shard_set_spec_payload(preliminary))
    with_common_hash = DeterministicScaleRunSpec.model_validate(
        {
            "run_spec_hash": "0" * 64,
            "shard_set_spec_hash": shard_set_spec_hash,
            **data,
        }
    ).model_dump(mode="json")
    return DeterministicScaleRunSpec.model_validate(
        {
            "run_spec_hash": hash_canonical(_run_spec_payload(with_common_hash)),
            "shard_set_spec_hash": shard_set_spec_hash,
            **data,
        }
    )


def _write_ineligible_shard_output(
    *,
    output_dir: Path,
    spec: DeterministicScaleRunSpec,
    source_by_id: dict[str, TheoremRecord],
    config: DeterministicScaleConfig,
) -> None:
    run_spec_path = output_dir / "run_spec.json"
    _write_new_atomic(run_spec_path, _canonical_model_bytes(spec))
    journal_dir = output_dir / "journal"
    receipt_dir = output_dir / "journal_receipts"
    shards: list[ScaleSourceShard] = []
    previous_receipt_hash = "0" * 64
    for global_index, (theorem_id, assignment) in enumerate(
        zip(
            spec.source_universe_theorem_ids,
            spec.source_shard_assignments,
            strict=True,
        )
    ):
        if assignment != spec.shard_index:
            continue
        source = source_by_id[theorem_id]
        failure = ScaleFailure(
            stage="source_preflight",
            code="fixture_ineligible",
            detail="fixture terminal outcome",
            source_theorem_ids=(source.theorem_id,),
        )
        shard = ScaleSourceShard(
            run_spec_hash=spec.run_spec_hash,
            source_index=global_index,
            source_theorem_id=source.theorem_id,
            source_representation_id=None,
            source_status="ineligible",
            source_failure=failure,
        )
        shard_path = _source_shard_path(
            journal_dir,
            global_index,
            source.theorem_id,
        )
        _write_new_atomic(shard_path, _canonical_model_bytes(shard))
        receipt = _build_journal_receipt(
            shard=shard,
            shard_path=shard_path,
            previous_receipt_hash=previous_receipt_hash,
        )
        _write_new_atomic(
            _journal_receipt_path(receipt_dir, shard_path),
            _canonical_model_bytes(receipt),
        )
        previous_receipt_hash = receipt.receipt_hash
        shards.append(shard)

    projected = _project_records(shards)
    _, partition_hashes = _write_partitions(output_dir, projected)
    journal_count, journal_tree_hash = _tree_hash(journal_dir, "*.json")
    receipt_count, receipt_tree_hash = _tree_hash(receipt_dir, "*.json")
    raw_count, raw_tree_hash = _tree_hash(output_dir / "raw_lean_responses", "*")
    assignment_hash = hash_canonical(
        {
            "source_universe_theorem_ids": spec.source_universe_theorem_ids,
            "source_shard_assignments": spec.source_shard_assignments,
        }
    )
    manifest = DeterministicScaleManifest(
        run_spec_hash=spec.run_spec_hash,
        run_spec_sha256=hash_file(run_spec_path),
        shard_set_spec_hash=spec.shard_set_spec_hash,
        shard_count=spec.shard_count,
        shard_index=spec.shard_index,
        source_universe_count=len(spec.source_universe_theorem_ids),
        source_assignment_sha256=assignment_hash,
        source_count=len(shards),
        eligible_source_count=0,
        ineligible_source_count=len(shards),
        journal_shard_count=journal_count,
        rule_status_counts={},
        family_accepted_counts={},
        record_counts={name: len(records) for name, records in projected.items()},
        partition_sha256=dict(sorted(partition_hashes.items())),
        journal_tree_hash=journal_tree_hash,
        journal_receipt_count=receipt_count,
        journal_receipt_tree_hash=receipt_tree_hash,
        journal_chain_tip=previous_receipt_hash,
        raw_response_file_count=raw_count,
        raw_response_tree_hash=raw_tree_hash,
        created_at=config.record_timestamp_utc,
    )
    _write_new_atomic(output_dir / "manifest.json", _canonical_model_bytes(manifest))
    replay_audit = _build_lean_replay_audit(
        run_spec=spec,
        run_spec_path=run_spec_path,
        replayed_source_ids=spec.selected_source_theorem_ids,
        journal_tree_hash=journal_tree_hash,
        partition_sha256=partition_hashes,
        created_at=config.record_timestamp_utc,
    )
    _write_new_atomic(
        output_dir / "full_lean_replay_audit.json",
        _canonical_model_bytes(replay_audit),
    )


def _fixture_shard_set(
    tmp_path: Path,
    *,
    code_by_shard: tuple[CodeState, CodeState] | None = None,
) -> tuple[tuple[Path, Path], Path, tuple[TheoremRecord, ...]]:
    sources = tuple(_source(index) for index in range(4))
    config_path = tmp_path / "deterministic_scale_unary_sharded_v1.yaml"
    config_path.write_bytes(
        (_ROOT / "configs/transformations/deterministic_scale_unary_sharded_v1.yaml").read_bytes()
    )
    loaded_config = load_config(config_path, DeterministicScaleConfig)
    theorem_path = tmp_path / "theorems.jsonl"
    theorem_path.write_bytes(b"".join(_canonical_model_bytes(source) for source in sources))
    representation_path = tmp_path / "representations.jsonl"
    representations = []
    for source in sources:
        representation = representation_record(
            representation_id=make_id(
                "repr",
                {
                    "theorem_id": source.theorem_id,
                    "normalization_version": "repr_v3",
                },
            ),
            theorem_id=source.theorem_id,
            normalization_version="repr_v3",
            context_id=source.context_id,
        )
        representations.append(
            representation.model_copy(
                update={"content_hash": _representation_payload_hash(representation)}
            )
        )
    representation_path.write_bytes(
        b"".join(_canonical_model_bytes(record) for record in representations)
    )
    support_paths = {
        name: tmp_path / f"{name}.json"
        for name in (
            "inventory",
            "theorem_upstream",
            "representation_upstream",
            "benchmark",
        )
    }
    for path in support_paths.values():
        path.write_bytes(b"{}\n")
    project = tmp_path / "project"
    project.mkdir()
    support_paths["project"] = project

    ordered = tuple(
        sorted(
            sources,
            key=lambda theorem: _selection_key(
                loaded_config.config.base_seed,
                theorem.theorem_id,
            ),
        )
    )
    assignments = _root_component_shard_assignments(ordered, shard_count=2)
    default_code = collect_code_state(_ROOT)
    codes = code_by_shard or (default_code, default_code)
    shard_dirs: list[Path] = []
    source_by_id = {source.theorem_id: source for source in ordered}
    for shard_index in range(2):
        spec = _make_run_spec(
            theorem_path=theorem_path,
            representation_path=representation_path,
            support_paths=support_paths,
            config_path=config_path,
            config_hash=loaded_config.config_hash,
            universe=ordered,
            assignments=assignments,
            shard_index=shard_index,
            code=codes[shard_index],
        )
        shard_dir = tmp_path / f"shard_{shard_index}"
        _write_ineligible_shard_output(
            output_dir=shard_dir,
            spec=spec,
            source_by_id=source_by_id,
            config=loaded_config.config,
        )
        shard_dirs.append(shard_dir)
    return (shard_dirs[0], shard_dirs[1]), theorem_path, ordered


def test_root_component_sharding_is_disjoint_complete_and_deterministic() -> None:
    shared_root = make_id("anc", {"shared": True})
    sources = (
        _source(0, shared_root=shared_root),
        _source(1),
        _source(2, shared_root=shared_root),
        _source(3),
    )

    first = _root_component_shard_assignments(sources, shard_count=2)
    second = _root_component_shard_assignments(sources, shard_count=2)

    assert first == second
    assert first[0] == first[2]
    assert set(first) == {0, 1}
    assert sum(first.count(index) for index in range(2)) == len(sources)


def test_sharded_policy_rejects_n10_and_requires_dedicated_global_pass() -> None:
    base = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config
    with pytest.raises(DeterministicScaleError, match="N10 cannot run inside source shards"):
        _validate_shard_execution_policy(base, shard_count=2)

    unary = load_config(
        _ROOT / "configs/transformations/deterministic_scale_unary_sharded_v1.yaml",
        DeterministicScaleConfig,
    ).config
    _validate_shard_execution_policy(unary, shard_count=2)

    n10 = load_config(
        _ROOT / "configs/transformations/deterministic_scale_n10_global_v1.yaml",
        DeterministicScaleConfig,
    ).config
    _validate_shard_execution_policy(n10, shard_count=1)
    with pytest.raises(DeterministicScaleError, match="N10 cannot run inside source shards"):
        _validate_shard_execution_policy(n10, shard_count=2)


@pytest.mark.parametrize("primary_is_lexically_first", [True, False])
def test_n10_replay_preserves_primary_and_donor_roles(
    primary_is_lexically_first: bool,
) -> None:
    lower = "thm:" + "0" * 64
    upper = "thm:" + "f" * 64
    primary_id, donor_id = (lower, upper) if primary_is_lexically_first else (upper, lower)
    primary = theorem_record(theorem_id=primary_id, declaration_full_name="Fixture.Primary")
    donor = theorem_record(theorem_id=donor_id, declaration_full_name="Fixture.Donor")
    primary_representation = representation_record(
        theorem_id=primary_id,
        representation_id=make_id(
            "repr",
            {"theorem_id": primary_id, "normalization_version": "repr_v3"},
        ),
    )
    donor_representation = representation_record(
        theorem_id=donor_id,
        representation_id=make_id(
            "repr",
            {"theorem_id": donor_id, "normalization_version": "repr_v3"},
        ),
    )

    sources, representations = _role_ordered_replay_inputs(
        rule_id="n10_nearby_theorem",
        primary=primary,
        primary_representation=primary_representation,
        donor_theorem_id=donor_id,
        theorem_by_id={primary_id: primary, donor_id: donor},
        representation_by_theorem={
            primary_id: primary_representation,
            donor_id: donor_representation,
        },
    )

    assert tuple(record.theorem_id for record in sources) == (primary_id, donor_id)
    assert tuple(record.theorem_id for record in representations) == (
        primary_id,
        donor_id,
    )


def test_merge_audits_complete_set_and_is_content_addressed(tmp_path: Path) -> None:
    shard_dirs, _, sources = _fixture_shard_set(tmp_path)

    first = merge_deterministic_scale_shards(
        paths=RepoPaths(root=_ROOT),
        shard_output_dirs=shard_dirs,
        output_dir=tmp_path / "merged",
    )
    second = merge_deterministic_scale_shards(
        paths=RepoPaths(root=_ROOT),
        shard_output_dirs=tuple(reversed(shard_dirs)),
        output_dir=tmp_path / "merged",
    )

    assert first.manifest_path == second.manifest_path
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.merged_manifest_hash in first.manifest_path.name
    assert sum(1 for _ in (tmp_path / "merged/partitions/failures.jsonl").open()) == len(sources)


def test_merge_requires_replay_accounting_audit(tmp_path: Path) -> None:
    shard_dirs, _, _ = _fixture_shard_set(tmp_path)
    (shard_dirs[0] / "full_lean_replay_audit.json").unlink()

    with pytest.raises(DeterministicScaleError, match="replay accounting audit"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=tmp_path / "merged",
        )


def test_merge_rejects_tampered_replay_audit(tmp_path: Path) -> None:
    shard_dirs, _, _ = _fixture_shard_set(tmp_path)
    replay_audit = shard_dirs[0] / "full_lean_replay_audit.json"
    replay_audit.write_bytes(replay_audit.read_bytes() + b" ")

    with pytest.raises(DeterministicScaleError, match="not canonical JSON"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=tmp_path / "merged",
        )


def test_merge_invokes_exact_lean_replay_for_every_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_merge

    shard_dirs, _, _ = _fixture_shard_set(tmp_path)
    replayed: list[Path] = []

    def record_replay(**kwargs: object) -> None:
        replayed.append(cast(Path, kwargs["output_dir"]))

    monkeypatch.setattr(scale_merge, "_replay_shard_with_lean", record_replay)
    merge_deterministic_scale_shards(
        paths=RepoPaths(root=_ROOT),
        shard_output_dirs=shard_dirs,
        output_dir=tmp_path / "merged",
    )

    assert replayed == list(shard_dirs)


def test_exact_merge_replay_requires_pinned_git_project(tmp_path: Path) -> None:
    shard_dirs, _, _ = _fixture_shard_set(tmp_path)
    spec = DeterministicScaleRunSpec.model_validate_json(
        (shard_dirs[0] / "run_spec.json").read_text(encoding="utf-8")
    )

    with pytest.raises(DeterministicScaleError, match="cannot bind clean Lean project"):
        _replay_shard_with_lean(
            paths=RepoPaths(root=_ROOT),
            output_dir=shard_dirs[0],
            spec=spec,
        )


def test_exact_merge_replay_calls_materializer_with_bound_run_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_merge

    shard_dirs, _, _ = _fixture_shard_set(tmp_path)
    output_dir = shard_dirs[0]
    spec = DeterministicScaleRunSpec.model_validate_json(
        (output_dir / "run_spec.json").read_text(encoding="utf-8")
    )
    clean_checks: list[tuple[Path, str | None, str | None]] = []
    materializer_kwargs: dict[str, object] = {}

    def fake_clean(
        project_dir: Path,
        *,
        expected_revision: str | None = None,
        expected_tree_hash: str | None = None,
    ) -> tuple[str, str]:
        clean_checks.append((project_dir, expected_revision, expected_tree_hash))
        return spec.project_revision, spec.project_tree_hash

    def fake_materialize(**kwargs: object) -> DeterministicScaleArtifacts:
        materializer_kwargs.update(kwargs)
        return DeterministicScaleArtifacts(
            output_dir=output_dir,
            run_spec_path=output_dir / "run_spec.json",
            manifest_path=output_dir / "manifest.json",
            manifest_sha256=hash_file(output_dir / "manifest.json"),
            partition_paths={},
        )

    monkeypatch.setattr(scale_merge, "_clean_project_tree_hash", fake_clean)
    monkeypatch.setattr(
        scale_merge,
        "run_deterministic_scale_materialization",
        fake_materialize,
    )
    _replay_shard_with_lean(
        paths=RepoPaths(root=_ROOT),
        output_dir=output_dir,
        spec=spec,
    )

    assert materializer_kwargs["resume"] is True
    assert materializer_kwargs["fast_resume"] is False
    assert materializer_kwargs["shard_count"] == spec.shard_count
    assert materializer_kwargs["shard_index"] == spec.shard_index
    assert materializer_kwargs["project_dir"] == Path(spec.project_dir)
    assert clean_checks == [
        (Path(spec.project_dir), spec.project_revision, spec.project_tree_hash),
        (Path(spec.project_dir), spec.project_revision, spec.project_tree_hash),
    ]


def test_materializer_api_rejects_retired_fast_resume_before_io(tmp_path: Path) -> None:
    with pytest.raises(DeterministicScaleError, match="fast resume is retired"):
        run_deterministic_scale_materialization(
            paths=RepoPaths(root=_ROOT),
            theorem_jsonl=tmp_path / "missing-theorems.jsonl",
            representation_jsonl=tmp_path / "missing-representations.jsonl",
            source_inventory_manifest=tmp_path / "missing-inventory.json",
            project_dir=tmp_path / "missing-project",
            output_dir=tmp_path / "output",
            resume=True,
            fast_resume=True,
        )


def test_merge_rejects_gap_overlap_and_journal_tampering(tmp_path: Path) -> None:
    shard_dirs, _, _ = _fixture_shard_set(tmp_path)
    with pytest.raises(DeterministicScaleError, match="every shard"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=(shard_dirs[0],),
            output_dir=tmp_path / "gap",
        )

    copied = tmp_path / "copied_shard_zero"
    shutil.copytree(shard_dirs[0], copied)
    with pytest.raises(DeterministicScaleError, match="indices"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=(shard_dirs[0], copied),
            output_dir=tmp_path / "overlap",
        )

    journal_path = next((shard_dirs[0] / "journal").glob("*.json"))
    journal_path.write_bytes(journal_path.read_bytes() + b" ")
    with pytest.raises(DeterministicScaleError, match="canonical JSON"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=tmp_path / "tampered",
        )


def test_merge_rejects_changed_input_config_and_code(tmp_path: Path) -> None:
    input_case = tmp_path / "input_case"
    input_case.mkdir()
    shard_dirs, theorem_path, _ = _fixture_shard_set(input_case)
    theorem_path.write_bytes(theorem_path.read_bytes() + b"\n")
    with pytest.raises(DeterministicScaleError, match="theorem input changed"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=input_case / "merged",
        )

    config_case = tmp_path / "config_case"
    config_case.mkdir()
    shard_dirs, _, _ = _fixture_shard_set(config_case)
    config_path = config_case / "deterministic_scale_unary_sharded_v1.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "profile_version: 1.1.0",
            "profile_version: 1.1.1",
        ),
        encoding="utf-8",
    )
    with pytest.raises(DeterministicScaleError, match="config changed"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=config_case / "merged",
        )

    code_case = tmp_path / "code_case"
    code_case.mkdir()
    shard_dirs, _, _ = _fixture_shard_set(
        code_case,
        code_by_shard=(
            CodeState(
                git_revision="7" * 40,
                git_dirty=False,
                code_tree_hash="8" * 64,
            ),
            CodeState(
                git_revision="9" * 40,
                git_dirty=False,
                code_tree_hash="a" * 64,
            ),
        ),
    )
    with pytest.raises(DeterministicScaleError, match="identical input/config/code"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=code_case / "merged",
        )


def test_merge_rejects_partition_and_manifest_accounting_tampering(
    tmp_path: Path,
) -> None:
    partition_case = tmp_path / "partition_case"
    partition_case.mkdir()
    shard_dirs, _, _ = _fixture_shard_set(partition_case)
    failure_partition = shard_dirs[0] / "partitions/failures.jsonl"
    failure_partition.write_bytes(failure_partition.read_bytes() + b"\n")
    with pytest.raises(DeterministicScaleError, match="partition differs from journal"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=partition_case / "merged",
        )

    manifest_case = tmp_path / "manifest_case"
    manifest_case.mkdir()
    shard_dirs, _, _ = _fixture_shard_set(manifest_case)
    manifest_path = shard_dirs[0] / "manifest.json"
    manifest = DeterministicScaleManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    tampered = manifest.model_copy(
        update={"record_counts": {**manifest.record_counts, "failures": 999}}
    )
    manifest_path.write_bytes(_canonical_model_bytes(tampered))
    with pytest.raises(DeterministicScaleError, match="does not reconcile"):
        merge_deterministic_scale_shards(
            paths=RepoPaths(root=_ROOT),
            shard_output_dirs=shard_dirs,
            output_dir=manifest_case / "merged",
        )


def test_merge_rejects_duplicate_variant_pair_and_accidental_labels() -> None:
    _, _, accepted = _accepted_source_shard()
    clean = _project_records((accepted,))
    variant = clean["variants"][0]
    with pytest.raises(DeterministicScaleError, match="duplicate variant_id"):
        _reject_cross_shard_semantic_leakage({**clean, "variants": (variant, variant)})

    pair = clean["pairs"][0]
    with pytest.raises(DeterministicScaleError, match="duplicate pair_id"):
        _reject_cross_shard_semantic_leakage({**clean, "pairs": (pair, pair)})

    labeled_pair = pair.model_copy(
        update={"resolved_label_id": make_id("label", {"accidental": True})}
    )
    with pytest.raises(DeterministicScaleError, match="resolved semantic labels"):
        _reject_cross_shard_semantic_leakage({**clean, "pairs": (labeled_pair,)})

    promoted_variant = variant.model_copy(update={"quality_tier": QualityTier.GOLD_HUMAN})
    with pytest.raises(DeterministicScaleError, match="uniformly provisional"):
        _reject_cross_shard_semantic_leakage({**clean, "variants": (promoted_variant,)})

    attempt = clean["attempts"][0]
    with pytest.raises(DeterministicScaleError, match="duplicate attempt_id"):
        _reject_cross_shard_semantic_leakage({**clean, "attempts": (attempt, attempt)})

    draft = clean["drafts"][0]
    quarantine = ScaleQuarantineRecord(
        status="candidate_invalid",
        source_theorem_ids=draft.source_theorem_ids,
        rule_id=draft.rule_id,
        family_id=draft.family_id,
        polarity=variant.polarity_metadata,
        draft_id=draft.draft_id,
        candidate_code_hash=draft.candidate_code_hash,
        failure=ScaleFailure(
            stage="candidate_validation",
            code="fixture",
            detail="fixture",
            source_theorem_ids=draft.source_theorem_ids,
            rule_id=draft.rule_id,
            draft_id=draft.draft_id,
        ),
        candidate_content_redacted=False,
    )
    with pytest.raises(DeterministicScaleError, match="duplicate draft_id"):
        _reject_cross_shard_semantic_leakage({**clean, "quarantine": (quarantine, quarantine)})


def test_semantic_lineage_audit_rejects_rehashed_bad_pair_split_group() -> None:
    source, source_representation, accepted = _accepted_source_shard()
    projected = _project_records((accepted,))
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config
    spec = DeterministicScaleRunSpec.model_construct(
        run_spec_hash="b" * 64,
        registry_hash="a" * 64,
        source_universe_theorem_ids=(source.theorem_id,),
    )
    _validate_projected_semantic_lineage(
        projected=projected,
        source_shards=(accepted,),
        source_theorems=(source,),
        source_representations=(source_representation,),
        spec=spec,
        config=config,
    )

    pair = projected["pairs"][0]
    bad_pair = pair.model_copy(
        update={"split_group_ids": (make_id("anc", {"malicious": "replacement"}),)}
    )
    with pytest.raises(DeterministicScaleError, match="pair identity/split lineage mismatch"):
        _validate_projected_semantic_lineage(
            projected={**projected, "pairs": (bad_pair,)},
            source_shards=(accepted,),
            source_theorems=(source,),
            source_representations=(source_representation,),
            spec=spec,
            config=config,
        )


@pytest.mark.parametrize("tamper", ["source", "candidate_hash"])
def test_semantic_lineage_audit_rejects_forged_quarantine_projection(
    tamper: str,
) -> None:
    source, source_representation, accepted = _accepted_source_shard()
    accepted_rule = accepted.rule_results[0]
    accepted_result = accepted_rule.draft_results[0]
    assert accepted_result.draft is not None
    failure = ScaleFailure(
        stage="candidate_validation",
        code="fixture_rejection",
        detail="fixture rejected draft",
        source_theorem_ids=accepted_rule.source_theorem_ids,
        rule_id=accepted_rule.rule_id,
        draft_id=accepted_result.draft.draft_id,
    )
    rejected_result = ScaleDraftResult(
        status="candidate_invalid",
        draft=accepted_result.draft,
        failure=failure,
    )
    rejected_rule = accepted_rule.model_copy(
        update={
            "status": "candidate_invalid",
            "draft_results": (rejected_result,),
        }
    )
    rejected_shard = accepted.model_copy(update={"rule_results": (rejected_rule,)})
    projected = _project_records((rejected_shard,))
    other_source = _source(999)
    other_representation = representation_record(
        representation_id=make_id(
            "repr",
            {
                "theorem_id": other_source.theorem_id,
                "normalization_version": "repr_v3",
            },
        ),
        theorem_id=other_source.theorem_id,
        normalization_version="repr_v3",
        context_id=other_source.context_id,
    )
    other_representation = other_representation.model_copy(
        update={"content_hash": _representation_payload_hash(other_representation)}
    )
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config
    spec = DeterministicScaleRunSpec.model_construct(
        run_spec_hash="b" * 64,
        registry_hash="a" * 64,
        source_universe_theorem_ids=(
            source.theorem_id,
            other_source.theorem_id,
        ),
    )
    record = projected["quarantine"][0]
    update: dict[str, object]
    if tamper == "source":
        update = {"source_theorem_ids": (other_source.theorem_id,)}
    else:
        update = {"candidate_code_hash": "c" * 64}
    forged = record.model_copy(update=update)

    with pytest.raises(DeterministicScaleError, match="exact owning outcome"):
        _validate_projected_semantic_lineage(
            projected={**projected, "quarantine": (forged,)},
            source_shards=(rejected_shard,),
            source_theorems=(source, other_source),
            source_representations=(source_representation, other_representation),
            spec=spec,
            config=config,
        )


def test_journal_receipt_rejects_shard_and_chain_tampering(tmp_path: Path) -> None:
    source = _source(0)
    shard = ScaleSourceShard(
        run_spec_hash="b" * 64,
        source_index=0,
        source_theorem_id=source.theorem_id,
        source_representation_id=None,
        source_status="ineligible",
        source_failure=ScaleFailure(
            stage="source_preflight",
            code="fixture",
            detail="fixture",
            source_theorem_ids=(source.theorem_id,),
        ),
    )
    shard_path = tmp_path / "journal/00000000-fixture.json"
    _write_new_atomic(shard_path, _canonical_model_bytes(shard))
    receipt = _build_journal_receipt(
        shard=shard,
        shard_path=shard_path,
        previous_receipt_hash="0" * 64,
    )
    receipt_path = tmp_path / "receipts/receipt.json"
    _write_new_atomic(receipt_path, _canonical_model_bytes(receipt))
    assert (
        _load_journal_receipt(
            path=receipt_path,
            shard=shard,
            shard_path=shard_path,
            previous_receipt_hash="0" * 64,
        )
        == receipt
    )

    with pytest.raises(DeterministicScaleError, match="bind the current shard/chain"):
        _load_journal_receipt(
            path=receipt_path,
            shard=shard,
            shard_path=shard_path,
            previous_receipt_hash="1" * 64,
        )


def test_scale_cli_forwards_sharding_and_exact_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_materializer

    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> DeterministicScaleArtifacts:
        seen.update(kwargs)
        output = tmp_path / "output"
        return DeterministicScaleArtifacts(
            output_dir=output,
            run_spec_path=output / "run_spec.json",
            manifest_path=output / "manifest.json",
            manifest_sha256="a" * 64,
            partition_paths={},
        )

    monkeypatch.setattr(
        scale_materializer,
        "run_deterministic_scale_materialization",
        fake_run,
    )
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--materialize-scale",
            "--root",
            str(tmp_path),
            "--theorems",
            str(tmp_path / "theorems.jsonl"),
            "--representations",
            str(tmp_path / "representations.jsonl"),
            "--source-inventory-manifest",
            str(tmp_path / "inventory.json"),
            "--project-dir",
            str(tmp_path / "mathlib"),
            "--output-dir",
            str(tmp_path / "output"),
            "--shard-count",
            "3",
            "--shard-index",
            "2",
            "--resume",
        ],
    )

    assert result.exit_code == 0
    assert seen["shard_count"] == 3
    assert seen["shard_index"] == 2
    assert seen["resume"] is True
    assert seen["fast_resume"] is False


def test_scale_cli_rejects_retired_fast_resume_even_with_resume(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--materialize-scale",
            "--root",
            str(tmp_path),
            "--theorems",
            str(tmp_path / "theorems.jsonl"),
            "--representations",
            str(tmp_path / "representations.jsonl"),
            "--source-inventory-manifest",
            str(tmp_path / "inventory.json"),
            "--project-dir",
            str(tmp_path / "mathlib"),
            "--output-dir",
            str(tmp_path / "output"),
            "--resume",
            "--fast-resume",
        ],
    )

    assert result.exit_code == 2
    assert "--fast-resume is retired" in result.output


def test_scale_merge_cli_accepts_repeated_shard_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_merge

    seen: dict[str, object] = {}

    def fake_merge(**kwargs: object) -> DeterministicScaleMergeArtifacts:
        seen.update(kwargs)
        output = tmp_path / "merged"
        return DeterministicScaleMergeArtifacts(
            output_dir=output,
            manifest_path=output / f"merged_manifest.{'a' * 64}.json",
            manifest_sha256="b" * 64,
            merged_manifest_hash="a" * 64,
            partition_paths={},
        )

    monkeypatch.setattr(scale_merge, "merge_deterministic_scale_shards", fake_merge)
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--merge-scale-shards",
            "--root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "merged"),
            "--shard-output-dir",
            str(tmp_path / "shard0"),
            "--shard-output-dir",
            str(tmp_path / "shard1"),
        ],
    )

    assert result.exit_code == 0
    assert seen["shard_output_dirs"] == [
        tmp_path / "shard0",
        tmp_path / "shard1",
    ]
