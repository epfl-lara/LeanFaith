from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest
import yaml

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.datasets.denylist import FrozenBenchmark, FrozenRegistry
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022FamilyPin,
    LF022ProductionFamilyMatrix,
    canonical_model_family,
    make_lf022_production_family_matrix,
)
from leanfaith.generation.lf022_public_pool import (
    LF022ApprovedPublicSource,
    LF022PublicPoolAudit,
    LF022PublicPoolCapacityError,
    LF022PublicPoolError,
    MaterializedLF022PublicPool,
    materialize_lf022_public_pool,
)
from leanfaith.lean.project_registry import ContextPayload, build_context_record
from leanfaith.representations.views import representation_content_hash
from leanfaith.schemas.enums import ArtifactClass, DataStage, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import REPRESENTATION_PREFIX, make_id
from leanfaith.schemas.manifest import CodeState, OutputManifest
from leanfaith.schemas.source import make_source_ancestry_id
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord
from leanfaith.sources.mathlib_frame import (
    MathlibDomainAllocation,
    MathlibFileFrame,
    MathlibFrameMember,
    make_mathlib_frame_id,
)

NOW = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)
REVISION = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def test_public_frame_policy_has_explicit_generation_yield_requirement() -> None:
    policy = yaml.safe_load(
        (ROOT / "configs/sources/mathlib_public_lf022_frame_v1.yaml").read_text(encoding="utf-8")
    )
    targets = policy["capacity_targets"]
    pool = targets["frozen_public_source_pool_records"]
    pair_target = targets["confirmatory_training_pairs_per_arm"]
    negative_target = pair_target * targets["negative_fraction_per_training_arm"]
    largest_distribution_target = (
        negative_target
        * targets["largest_single_generated_distribution_fraction_of_negative_slots"]
    )
    required_outputs = targets["minimum_unique_valid_outputs_per_generated_distribution"]
    planned_tasks = targets["planned_source_tasks_per_generated_distribution"]

    assert pool == 15_000
    assert required_outputs == math.ceil(largest_distribution_target)
    assert planned_tasks == pool
    assert (
        required_outputs * targets["minimum_required_generation_yield_denominator"]
        <= planned_tasks * targets["minimum_required_generation_yield_numerator"]
    )
    assert targets["maximum_unique_variants_per_final_split_component_per_arm"] == 4


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_jsonl(path: Path, records: tuple[object, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(record) + b"\n" for record in records))


def _context() -> ContextRecord:
    payload = ContextPayload(
        environment_schema_version=1,
        project_uri="https://github.com/leanprover-community/mathlib4",
        project_revision=REVISION,
        lean_version="4.31.0",
        lean_interact_version="0.11.4",
        repl_revision="b" * 40,
        imports=("Mathlib",),
        header_text="import Mathlib",
    )
    return build_context_record(
        payload,
        project_kind="mathlib",
        project_registry_key="mathlib",
    )


def _theorem(index: int, context: ContextRecord, *, source: str = "mathlib") -> TheoremRecord:
    theorem_id = f"thm:{index + 1:064x}"
    source_file = f"Mathlib/Public/Fixture{index}.lean"
    declaration_full_name = f"Public.fixture_{index}"
    ancestry_id = make_source_ancestry_id(
        source=source,
        revision=REVISION,
        source_locator=source_file,
        declaration_full_name=declaration_full_name,
    )
    statement = f"theorem Public.fixture_{index} (n : Nat) : n = n"
    return TheoremRecord(
        schema_version=1,
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source=source,
        source_revision=REVISION,
        source_split="public_pool_fixture",
        source_record=source_file,
        source_file=source_file,
        context_id=context.context_id,
        declaration_kind="theorem",
        declaration_name=f"fixture_{index}",
        declaration_full_name=declaration_full_name,
        proof_stripped_declaration=statement,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES,
        statement_content_hash=hash_canonical({"statement": statement}),
        nl_source_link=f"https://example.test/mathlib/{index}",
        metadata={"transform_source_eligible": True},
    )


def _representation(index: int, theorem: TheoremRecord) -> RepresentationRecord:
    statuses = {
        "raw_proof_stripped": ViewStatus.OK,
        "headless": ViewStatus.OK,
        "signature_pp": ViewStatus.OK,
        "signature_explicit": ViewStatus.OK,
        "alpha_structural": ViewStatus.NOT_ATTEMPTED,
        "notation_light": ViewStatus.NOT_ATTEMPTED,
        "semantic_atoms": ViewStatus.OK,
        "operator_tree": ViewStatus.NOT_ATTEMPTED,
    }
    signature = f"theorem Public.fixture_{index} (n : Nat) : Eq Nat n n"
    raw_proof_stripped = theorem.proof_stripped_declaration
    headless = f"(n : Nat) : n = n -- fixture {index}"
    signature_pp = f"theorem Public.fixture_{index} (n : Nat) : n = n"
    alpha_fingerprint = hash_canonical({"signature": signature})
    views: dict[str, object] = {
        "raw_proof_stripped": raw_proof_stripped,
        "headless": headless,
        "signature_pp": signature_pp,
        "signature_explicit": signature,
        "semantic_atoms": ["Eq", "Nat"],
        "operator_tree": None,
        "alpha_identity_fingerprint": alpha_fingerprint,
    }
    return RepresentationRecord(
        schema_version=1,
        representation_id=make_id(
            REPRESENTATION_PREFIX,
            {
                "theorem_id": theorem.theorem_id,
                "normalization_version": "representation_v1",
            },
        ),
        theorem_id=theorem.theorem_id,
        normalization_version="representation_v1",
        context_id=theorem.context_id,
        raw_proof_stripped=raw_proof_stripped,
        headless=headless,
        signature_pp=signature_pp,
        signature_explicit=signature,
        semantic_atoms=("Eq", "Nat"),
        alpha_identity_fingerprint=alpha_fingerprint,
        view_status=statuses,
        content_hash=representation_content_hash(views),
        created_at=NOW,
    )


def _family_pin(family_id: str, model_id: str, digit: str) -> LF022FamilyPin:
    return LF022FamilyPin(
        family_id=family_id,
        model_id=model_id,
        canonical_family=canonical_model_family(model_id),
        pin_kind="exact_hf_checkpoint",
        checkpoint_revision=digit * 40,
        underlying_checkpoint_revision_status="exact",
    )


def _family_matrix() -> LF022ProductionFamilyMatrix:
    registry = (
        _family_pin("kimi", "moonshotai/Kimi-K2.7-Code", "1"),
        _family_pin("qwen", "Qwen/Qwen3.5-397B-A17B", "2"),
        _family_pin("glm", "zai-org/GLM-5", "3"),
        _family_pin("deepseek", "deepseek-ai/DeepSeek-V3.2", "4"),
        _family_pin("heldout", "anthropic/claude-opus-4.8", "5"),
    )
    return make_lf022_production_family_matrix(
        family_registry=registry,
        proposer_family_ids=("kimi", "qwen", "glm"),
        judge_family_ids=("kimi", "qwen", "glm", "deepseek"),
        sci_validator_family_ids=("kimi", "qwen", "glm", "deepseek"),
        heldout_eval_family_id="heldout",
    )


def _approval() -> LF022ApprovedPublicSource:
    return LF022ApprovedPublicSource(
        schema_version=1,
        source="mathlib",
        source_revision=REVISION,
        license_id="Apache-2.0",
        license_evidence_uri=(
            "https://github.com/leanprover-community/mathlib4/blob/master/LICENSE"
        ),
        approval_status="approved_public_research_compatible",
        source_is_public=True,
        redistribution_allowed=True,
        external_transmission_allowed=True,
        context_project_kind="mathlib",
        context_project_uri="https://github.com/leanprover-community/mathlib4",
        context_project_registry_key="mathlib",
    )


def _registry_binding(
    root: Path,
    *,
    row_ids: tuple[str, ...] = (),
) -> tuple[FrozenRegistry, LF022ArtifactBinding]:
    benchmarks = (
        (
            FrozenBenchmark(
                registry_key="fixture_benchmark",
                source_id="fixture",
                revision=REVISION,
                resolved=True,
                splits={"test": len(row_ids)},
                row_ids=tuple(sorted(row_ids)),
            ),
        )
        if row_ids
        else ()
    )
    registry = FrozenRegistry(
        schema_version=1,
        frozen_at=NOW,
        policy_version="benchmark_denylist_v1",
        benchmarks=benchmarks,
        representation_signatures_appended=True,
    )
    path = root / "data/benchmarks/active.json"
    _write_json(path, registry.model_dump(mode="json"))
    return (
        registry,
        LF022ArtifactBinding(
            path=str(path.relative_to(root)),
            sha256=hash_file(path),
        ),
    )


def _inputs(
    root: Path,
    theorems: tuple[TheoremRecord, ...],
    representations: tuple[RepresentationRecord, ...],
    context: ContextRecord,
) -> tuple[Path, Path, Path]:
    theorem_path = root / "inputs/theorems.jsonl"
    representation_path = root / "inputs/representations.jsonl"
    context_path = root / "inputs/contexts.jsonl"
    _write_jsonl(
        theorem_path,
        tuple(item.model_dump(mode="json") for item in theorems),
    )
    _write_jsonl(
        representation_path,
        tuple(item.model_dump(mode="json") for item in representations),
    )
    _write_jsonl(context_path, (context.model_dump(mode="json"),))
    return theorem_path, representation_path, context_path


def _upstream_artifacts(
    root: Path,
    theorem_path: Path,
    representation_path: Path,
    context_path: Path,
) -> tuple[
    OutputManifest,
    LF022ArtifactBinding,
    OutputManifest,
    LF022ArtifactBinding,
    MathlibFileFrame,
    LF022ArtifactBinding,
]:
    theorem_documents = [
        json.loads(line) for line in theorem_path.read_text(encoding="utf-8").splitlines()
    ]
    theorems = tuple(
        TheoremRecord.model_validate(document.get("theorem", document))
        for document in theorem_documents
    )
    member_by_path: dict[str, MathlibFrameMember] = {}
    for theorem in theorems:
        relative_path = theorem.source_file or ""
        member_by_path.setdefault(
            relative_path,
            MathlibFrameMember(
                relative_path=relative_path,
                sha256=hash_canonical({"source_file": theorem.source_file}),
                domain="Public",
                selection_rank_sha256=hash_canonical({"rank": theorem.source_file}),
            ),
        )
    members = tuple(member_by_path[path] for path in sorted(member_by_path))
    frame_payload: dict[str, object] = {
        "schema_version": 1,
        "selection_algorithm": "mathlib_domain_progressive_proportional_hash_v1",
        "source": "mathlib",
        "revision": REVISION,
        "private_source": False,
        "release_eligible": True,
        "inventory_adapter_version": "mathlib_adapter_v1",
        "inventory_id": f"mathlib_repo_inventory_v1:{'4' * 64}",
        "inventory_file_count": len(members),
        "eligible_file_count": len(members),
        "excluded_file_count": 0,
        "excluded_domains": [],
        "target_file_count": len(members),
        "selected_file_count": len(members),
        "selection_seed_sha256": "5" * 64,
        "domain_allocations": [
            MathlibDomainAllocation(
                domain="Public",
                inventory_file_count=len(members),
                selected_file_count=len(members),
            ).model_dump(mode="json")
        ],
        "members": [member.model_dump(mode="json") for member in members],
    }
    frame = MathlibFileFrame.model_validate(
        {**frame_payload, "frame_id": make_mathlib_frame_id(frame_payload)}
    )
    frame_path = root / "inputs/mathlib_source_frame.json"
    frame_path.write_bytes(canonical_json_bytes(frame.model_dump(mode="json")) + b"\n")
    frame_binding = LF022ArtifactBinding(
        path=frame_path.relative_to(root).as_posix(),
        sha256=hash_file(frame_path),
    )
    theorem_binding_path = theorem_path.relative_to(root).as_posix()
    theorem_hash = hash_file(theorem_path)
    code = CodeState(
        git_revision="7" * 40,
        git_dirty=False,
        base_git_commit="7" * 40,
        code_tree_hash="8" * 64,
    )
    environment_hash = "9" * 64
    manifest = OutputManifest(
        stage=DataStage.ELABORATED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260728T000000Z_12345678",
        source="mathlib",
        source_revision=REVISION,
        config_hash="6" * 64,
        record_schema_version=1,
        row_count=len(theorems),
        output_partition_checksums={theorem_binding_path: theorem_hash},
        input_partition_checksums={frame_binding.path: frame_binding.sha256},
        file_checksums={theorem_binding_path: theorem_hash},
        context_hash=hash_file(context_path),
        environment_hash=environment_hash,
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=NOW,
    )
    manifest_path = root / "inputs/extraction_output_manifest.json"
    _write_json(manifest_path, manifest.model_dump(mode="json"))
    manifest_binding = LF022ArtifactBinding(
        path=manifest_path.relative_to(root).as_posix(),
        sha256=hash_file(manifest_path),
    )
    representation_binding_path = representation_path.relative_to(root).as_posix()
    representation_hash = hash_file(representation_path)
    context_ids = {theorem.context_id for theorem in theorems}
    assert len(context_ids) == 1
    representation_manifest = OutputManifest(
        stage=DataStage.REPRESENTED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260728T000000Z_87654321",
        source="mathlib_public_fixture",
        source_revision="from_theorem_partition",
        config_hash="a" * 64,
        record_schema_version=1,
        row_count=len(theorems),
        attempted_row_count=len(theorems),
        terminal_outcome_counts={"represented": len(theorems), "view_failures": 0},
        file_checksums={representation_binding_path: representation_hash},
        input_partition_checksums={theorem_binding_path: theorem_hash},
        output_partition_checksums={representation_binding_path: representation_hash},
        environment_hash=environment_hash,
        context_hash=hash_canonical({"context_id": next(iter(context_ids))}),
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=NOW,
    )
    representation_manifest_path = root / "inputs/representation_output_manifest.json"
    _write_json(
        representation_manifest_path,
        representation_manifest.model_dump(mode="json"),
    )
    representation_manifest_binding = LF022ArtifactBinding(
        path=representation_manifest_path.relative_to(root).as_posix(),
        sha256=hash_file(representation_manifest_path),
    )
    return (
        manifest,
        manifest_binding,
        representation_manifest,
        representation_manifest_binding,
        frame,
        frame_binding,
    )


def _materialize(**kwargs: object) -> MaterializedLF022PublicPool:
    root = Path(kwargs["repo_root"])  # type: ignore[arg-type]
    theorem_path = Path(kwargs["theorem_records_path"])  # type: ignore[arg-type]
    representation_path = Path(kwargs["representation_records_path"])  # type: ignore[arg-type]
    context_path = Path(kwargs["context_records_path"])  # type: ignore[arg-type]
    supplied = tuple(
        kwargs.pop(name, None)
        for name in (
            "extraction_output_manifest",
            "extraction_output_manifest_binding",
            "representation_output_manifest",
            "representation_output_manifest_binding",
            "mathlib_source_frame",
            "mathlib_source_frame_binding",
        )
    )
    if all(value is None for value in supplied):
        (
            manifest,
            manifest_binding,
            representation_manifest,
            representation_manifest_binding,
            frame,
            frame_binding,
        ) = _upstream_artifacts(
            root,
            theorem_path,
            representation_path,
            context_path,
        )
    elif any(value is None for value in supplied):
        raise AssertionError("tests must supply all upstream extraction artifacts together")
    else:
        (
            manifest,
            manifest_binding,
            representation_manifest,
            representation_manifest_binding,
            frame,
            frame_binding,
        ) = supplied
    return materialize_lf022_public_pool(
        **kwargs,  # type: ignore[arg-type]
        extraction_output_manifest=manifest,  # type: ignore[arg-type]
        extraction_output_manifest_binding=manifest_binding,  # type: ignore[arg-type]
        representation_output_manifest=representation_manifest,  # type: ignore[arg-type]
        representation_output_manifest_binding=representation_manifest_binding,  # type: ignore[arg-type]
        mathlib_source_frame=frame,  # type: ignore[arg-type]
        mathlib_source_frame_binding=frame_binding,  # type: ignore[arg-type]
    )


def test_materializer_selects_exact_deterministic_non_executable_pool(
    tmp_path: Path,
) -> None:
    context = _context()
    theorems = tuple(_theorem(index, context) for index in range(4))
    representations = tuple(
        _representation(index, theorem) for index, theorem in enumerate(theorems)
    )
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        theorems,
        representations,
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)
    matrix = _family_matrix()

    def run() -> MaterializedLF022PublicPool:
        return _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=matrix,
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/lf022/public_pool"),
            requested_count=3,
            profile="diagnostic_scaffold",
        )

    first = run()
    second = run()

    assert first == second
    assert first.audit.selected_count == 3
    assert first.audit.eligible_count == 4
    assert first.audit.eligible_not_selected_count == 1
    assert first.audit.rejection_counts == {
        "private_source": 0,
        "unapproved_source": 0,
        "not_fully_elaborated_proposition": 0,
        "transform_source_ineligible": 0,
        "not_source_ancestry": 0,
        "ancestry_binding_mismatch": 0,
        "missing_representation": 0,
        "representation_binding_mismatch": 0,
        "representation_content_hash_mismatch": 0,
        "missing_or_mismatched_context": 0,
        "required_view_unavailable": 0,
        "unstable_source_locator": 0,
        "denylist_identifier_hit": 0,
        "denylist_content_hit": 0,
    }
    assert first.plan.unique_source_count == 3
    assert first.audit.eligible_unique_ancestry_count == 4
    assert first.audit.selected_unique_ancestry_count == 3
    assert len(first.plan.tasks) == 6
    assert first.plan.network_execution_authorized is False
    assert first.plan.semantic_labels_created is False
    assert all(task.executable is False for task in first.plan.tasks)
    assert all(task.semantic_label_created is False for task in first.plan.tasks)
    assert (
        first.audit.outputs.source_pool.record_count
        == first.audit.outputs.theorem_records.record_count
        == first.audit.outputs.representation_records.record_count
        == 3
    )
    for binding in first.audit.outputs.extraction_manifests.values():
        assert hash_file(tmp_path / binding.path) == binding.sha256
    assert hash_file(tmp_path / first.audit_binding.path) == first.audit_binding.sha256
    assert (
        LF022PublicPoolAudit.model_validate_json((tmp_path / first.audit_binding.path).read_bytes())
        == first.audit
    )


def test_materializer_accepts_canonical_extraction_theorem_envelopes(
    tmp_path: Path,
) -> None:
    context = _context()
    theorem = _theorem(0, context)
    representation = _representation(0, theorem)
    theorem_path = tmp_path / "inputs/theorems.jsonl"
    representation_path = tmp_path / "inputs/representations.jsonl"
    context_path = tmp_path / "inputs/contexts.jsonl"
    _write_jsonl(
        theorem_path,
        (
            {
                "theorem": theorem.model_dump(mode="json"),
                "representation": {"headless": representation.headless},
            },
        ),
    )
    _write_jsonl(
        representation_path,
        (representation.model_dump(mode="json"),),
    )
    _write_jsonl(context_path, (context.model_dump(mode="json"),))
    registry, registry_binding = _registry_binding(tmp_path)

    result = _materialize(
        repo_root=tmp_path,
        theorem_records_path=theorem_path,
        representation_records_path=representation_path,
        context_records_path=context_path,
        active_registry=registry,
        active_registry_binding=registry_binding,
        family_matrix=_family_matrix(),
        approved_sources=(_approval(),),
        output_directory=Path("artifacts/pool"),
        requested_count=1,
        profile="diagnostic_scaffold",
    )

    assert result.audit.selected_count == 1
    assert result.audit.input_theorems.sha256 == hash_file(theorem_path)


def _single_public_pool_fixture(
    root: Path,
) -> tuple[
    dict[str, object], OutputManifest, LF022ArtifactBinding, MathlibFileFrame, LF022ArtifactBinding
]:
    context = _context()
    theorem = _theorem(0, context)
    theorem_path, representation_path, context_path = _inputs(
        root,
        (theorem,),
        (_representation(0, theorem),),
        context,
    )
    registry, registry_binding = _registry_binding(root)
    (
        manifest,
        manifest_binding,
        representation_manifest,
        representation_manifest_binding,
        frame,
        frame_binding,
    ) = _upstream_artifacts(
        root,
        theorem_path,
        representation_path,
        context_path,
    )
    kwargs: dict[str, object] = {
        "repo_root": root,
        "theorem_records_path": theorem_path,
        "representation_records_path": representation_path,
        "context_records_path": context_path,
        "representation_output_manifest": representation_manifest,
        "representation_output_manifest_binding": representation_manifest_binding,
        "active_registry": registry,
        "active_registry_binding": registry_binding,
        "family_matrix": _family_matrix(),
        "approved_sources": (_approval(),),
        "output_directory": Path("artifacts/pool"),
        "requested_count": 1,
        "profile": "diagnostic_scaffold",
    }
    return kwargs, manifest, manifest_binding, frame, frame_binding


def _persist_bound_json(
    root: Path,
    relative_path: str,
    value: object,
) -> LF022ArtifactBinding:
    path = root / relative_path
    _write_json(path, value)
    return LF022ArtifactBinding(path=relative_path, sha256=hash_file(path))


def test_materializer_rejects_extraction_manifest_with_wrong_theorem_hash(
    tmp_path: Path,
) -> None:
    kwargs, manifest, _, frame, frame_binding = _single_public_pool_fixture(tmp_path)
    bad_manifest = manifest.model_copy(
        update={
            "output_partition_checksums": {
                next(iter(manifest.output_partition_checksums)): "0" * 64
            }
        }
    )
    bad_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/bad_theorem_hash_manifest.json",
        bad_manifest.model_dump(mode="json"),
    )

    with pytest.raises(LF022PublicPoolError, match="exact theorem JSONL"):
        _materialize(
            **kwargs,
            extraction_output_manifest=bad_manifest,
            extraction_output_manifest_binding=bad_manifest_binding,
            mathlib_source_frame=frame,
            mathlib_source_frame_binding=frame_binding,
        )


def test_materializer_rejects_extraction_manifest_with_wrong_frame_hash(
    tmp_path: Path,
) -> None:
    kwargs, manifest, _, frame, frame_binding = _single_public_pool_fixture(tmp_path)
    bad_manifest = manifest.model_copy(
        update={
            "input_partition_checksums": {
                frame_binding.path: "0" * 64,
            }
        }
    )
    bad_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/bad_frame_hash_manifest.json",
        bad_manifest.model_dump(mode="json"),
    )

    with pytest.raises(LF022PublicPoolError, match="exact mathlib source frame"):
        _materialize(
            **kwargs,
            extraction_output_manifest=bad_manifest,
            extraction_output_manifest_binding=bad_manifest_binding,
            mathlib_source_frame=frame,
            mathlib_source_frame_binding=frame_binding,
        )


def test_materializer_rejects_representation_manifest_with_wrong_output_hash(
    tmp_path: Path,
) -> None:
    kwargs, manifest, manifest_binding, frame, frame_binding = _single_public_pool_fixture(tmp_path)
    representation_manifest = kwargs.pop("representation_output_manifest")
    kwargs.pop("representation_output_manifest_binding")
    assert isinstance(representation_manifest, OutputManifest)
    bad_representation_manifest = representation_manifest.model_copy(
        update={
            "output_partition_checksums": {
                next(iter(representation_manifest.output_partition_checksums)): "0" * 64
            }
        }
    )
    bad_representation_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/bad_representation_output_hash_manifest.json",
        bad_representation_manifest.model_dump(mode="json"),
    )

    with pytest.raises(LF022PublicPoolError, match="exact representation JSONL"):
        _materialize(
            **kwargs,
            extraction_output_manifest=manifest,
            extraction_output_manifest_binding=manifest_binding,
            representation_output_manifest=bad_representation_manifest,
            representation_output_manifest_binding=bad_representation_manifest_binding,
            mathlib_source_frame=frame,
            mathlib_source_frame_binding=frame_binding,
        )


def test_materializer_rejects_representation_manifest_with_wrong_input_hash(
    tmp_path: Path,
) -> None:
    kwargs, manifest, manifest_binding, frame, frame_binding = _single_public_pool_fixture(tmp_path)
    representation_manifest = kwargs.pop("representation_output_manifest")
    kwargs.pop("representation_output_manifest_binding")
    assert isinstance(representation_manifest, OutputManifest)
    bad_representation_manifest = representation_manifest.model_copy(
        update={
            "input_partition_checksums": {
                next(iter(representation_manifest.input_partition_checksums)): "0" * 64
            }
        }
    )
    bad_representation_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/bad_representation_input_hash_manifest.json",
        bad_representation_manifest.model_dump(mode="json"),
    )

    with pytest.raises(LF022PublicPoolError, match="exact extraction theorem JSONL"):
        _materialize(
            **kwargs,
            extraction_output_manifest=manifest,
            extraction_output_manifest_binding=manifest_binding,
            representation_output_manifest=bad_representation_manifest,
            representation_output_manifest_binding=bad_representation_manifest_binding,
            mathlib_source_frame=frame,
            mathlib_source_frame_binding=frame_binding,
        )


def test_materializer_rejects_representation_run_from_different_code_tree(
    tmp_path: Path,
) -> None:
    kwargs, manifest, manifest_binding, frame, frame_binding = _single_public_pool_fixture(tmp_path)
    representation_manifest = kwargs.pop("representation_output_manifest")
    kwargs.pop("representation_output_manifest_binding")
    assert isinstance(representation_manifest, OutputManifest)
    different_code = representation_manifest.code.model_copy(update={"code_tree_hash": "d" * 64})
    bad_representation_manifest = representation_manifest.model_copy(
        update={
            "code_tree_hash": "d" * 64,
            "code": different_code,
        }
    )
    bad_representation_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/different_code_representation_manifest.json",
        bad_representation_manifest.model_dump(mode="json"),
    )

    with pytest.raises(
        LF022PublicPoolError,
        match="environment/code provenance differs",
    ):
        _materialize(
            **kwargs,
            extraction_output_manifest=manifest,
            extraction_output_manifest_binding=manifest_binding,
            representation_output_manifest=bad_representation_manifest,
            representation_output_manifest_binding=bad_representation_manifest_binding,
            mathlib_source_frame=frame,
            mathlib_source_frame_binding=frame_binding,
        )


def test_materializer_rejects_theorem_outside_bound_mathlib_frame(
    tmp_path: Path,
) -> None:
    kwargs, manifest, _, frame, _ = _single_public_pool_fixture(tmp_path)
    payload = frame.model_dump(mode="python", exclude={"frame_id"})
    payload["members"][0]["relative_path"] = "Mathlib/Public/Other.lean"
    changed_frame = MathlibFileFrame.model_validate(
        {**payload, "frame_id": make_mathlib_frame_id(payload)}
    )
    changed_frame_path = tmp_path / "inputs/changed_mathlib_source_frame.json"
    changed_frame_path.write_bytes(
        canonical_json_bytes(changed_frame.model_dump(mode="json")) + b"\n"
    )
    changed_frame_binding = LF022ArtifactBinding(
        path=changed_frame_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(changed_frame_path),
    )
    changed_manifest = manifest.model_copy(
        update={
            "input_partition_checksums": {
                changed_frame_binding.path: changed_frame_binding.sha256,
            }
        }
    )
    changed_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/changed_frame_manifest.json",
        changed_manifest.model_dump(mode="json"),
    )

    with pytest.raises(LF022PublicPoolError, match="outside the bound mathlib source frame"):
        _materialize(
            **kwargs,
            extraction_output_manifest=changed_manifest,
            extraction_output_manifest_binding=changed_manifest_binding,
            mathlib_source_frame=changed_frame,
            mathlib_source_frame_binding=changed_frame_binding,
        )


def test_scientific_pool_requires_environment_and_code_tree_hashes(
    tmp_path: Path,
) -> None:
    kwargs, manifest, _, frame, frame_binding = _single_public_pool_fixture(tmp_path)
    kwargs["profile"] = "scientific_production_scaffold"
    kwargs["requested_count"] = 15_000
    incomplete_manifest = manifest.model_copy(
        update={
            "environment_hash": None,
            "code_tree_hash": None,
            "code": manifest.code.model_copy(update={"code_tree_hash": None}),
        }
    )
    incomplete_manifest_binding = _persist_bound_json(
        tmp_path,
        "inputs/incomplete_provenance_manifest.json",
        incomplete_manifest.model_dump(mode="json"),
    )

    with pytest.raises(
        LF022PublicPoolError,
        match="exact environment and code-tree hashes",
    ):
        _materialize(
            **kwargs,
            extraction_output_manifest=incomplete_manifest,
            extraction_output_manifest_binding=incomplete_manifest_binding,
            mathlib_source_frame=frame,
            mathlib_source_frame_binding=frame_binding,
        )


def test_materializer_persists_machine_readable_rejections(tmp_path: Path) -> None:
    context = _context()
    theorems = tuple(_theorem(index, context) for index in range(4))
    representations = [_representation(index, theorem) for index, theorem in enumerate(theorems)]
    ineligible = theorems[1].model_copy(update={"metadata": {"transform_source_eligible": False}})
    bad_view_payload = representations[2].model_dump(mode="python")
    bad_view_payload["signature_explicit"] = None
    bad_view_payload["view_status"]["signature_explicit"] = ViewStatus.FAILED
    bad_view_payload["content_hash"] = representation_content_hash(
        {
            "raw_proof_stripped": bad_view_payload["raw_proof_stripped"],
            "headless": bad_view_payload["headless"],
            "signature_pp": bad_view_payload["signature_pp"],
            "signature_explicit": None,
            "semantic_atoms": list(bad_view_payload["semantic_atoms"]),
            "operator_tree": bad_view_payload["operator_tree"],
            "alpha_identity_fingerprint": bad_view_payload["alpha_identity_fingerprint"],
        }
    )
    bad_view = RepresentationRecord.model_validate(bad_view_payload)
    adjusted_theorems = (theorems[0], ineligible, theorems[2], theorems[3])
    adjusted_representations = (
        representations[0],
        representations[1],
        bad_view,
        representations[3],
    )
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        adjusted_theorems,
        adjusted_representations,
        context,
    )
    registry, registry_binding = _registry_binding(
        tmp_path,
        row_ids=(theorems[3].theorem_id,),
    )

    result = _materialize(
        repo_root=tmp_path,
        theorem_records_path=theorem_path,
        representation_records_path=representation_path,
        context_records_path=context_path,
        active_registry=registry,
        active_registry_binding=registry_binding,
        family_matrix=_family_matrix(),
        approved_sources=(_approval(),),
        output_directory=Path("artifacts/pool"),
        requested_count=1,
        profile="diagnostic_scaffold",
    )

    assert result.audit.eligible_count == 1
    assert result.audit.rejection_counts["transform_source_ineligible"] == 1
    assert result.audit.rejection_counts["required_view_unavailable"] == 1
    assert result.audit.rejection_counts["denylist_identifier_hit"] == 1
    assert sum(result.audit.rejection_counts.values()) == 3


def test_materializer_fails_before_output_when_requested_count_is_unavailable(
    tmp_path: Path,
) -> None:
    context = _context()
    theorem = _theorem(0, context)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (_representation(0, theorem),),
        context,
    )
    registry, registry_binding = _registry_binding(
        tmp_path,
        row_ids=(theorem.theorem_id,),
    )

    with pytest.raises(
        LF022PublicPoolCapacityError,
        match="only 0 distinct source ancestries",
    ) as error:
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/must_not_exist"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )

    assert error.value.requested_count == 1
    assert error.value.eligible_count == 0
    assert error.value.eligible_unique_ancestry_count == 0
    assert error.value.rejection_counts["denylist_identifier_hit"] == 1
    assert not (tmp_path / "artifacts/must_not_exist").exists()


def test_materializer_rejects_duplicate_source_locators(tmp_path: Path) -> None:
    context = _context()
    first = _theorem(0, context)
    second_payload = _theorem(1, context).model_dump(mode="python")
    second_payload.update(
        {
            "source_file": first.source_file,
            "declaration_full_name": first.declaration_full_name,
        }
    )
    second = TheoremRecord.model_validate(second_payload)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (first, second),
        (_representation(0, first), _representation(1, second)),
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(LF022PublicPoolError, match="duplicate source locator"):
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )


def test_materializer_recomputes_root_ancestry_before_counting_capacity(
    tmp_path: Path,
) -> None:
    context = _context()
    first = _theorem(0, context)
    second = _theorem(1, context).model_copy(
        update={
            "ancestry_id": first.ancestry_id,
            "root_ancestry_ids": first.root_ancestry_ids,
        }
    )
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (first, second),
        (_representation(0, first), _representation(1, second)),
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(
        LF022PublicPoolCapacityError,
        match=r"only 1 distinct source ancestries \(1 theorem records\)",
    ) as error:
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=2,
            profile="diagnostic_scaffold",
        )

    assert error.value.eligible_count == 1
    assert error.value.eligible_unique_ancestry_count == 1
    assert error.value.rejection_counts["ancestry_binding_mismatch"] == 1
    assert not (tmp_path / "artifacts/pool").exists()


def test_materializer_rejects_context_source_revision_mismatch(tmp_path: Path) -> None:
    context = _context()
    theorem = _theorem(0, context)
    mismatched_context = context.model_copy(update={"project_revision": "b" * 40})
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (_representation(0, theorem),),
        mismatched_context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(LF022PublicPoolCapacityError) as error:
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )

    assert error.value.rejection_counts["missing_or_mismatched_context"] == 1
    assert not (tmp_path / "artifacts/pool").exists()


def test_materializer_rejects_context_project_uri_mismatch(tmp_path: Path) -> None:
    context = _context()
    theorem = _theorem(0, context)
    payload = ContextPayload(
        environment_schema_version=context.environment_schema_version,
        project_uri="https://example.invalid/not-mathlib",
        project_revision=context.project_revision,
        lean_version=context.lean_version,
        lean_interact_version=context.lean_interact_version,
        repl_revision=context.repl_revision,
        imports=context.imports,
        header_text=context.header_text,
    )
    mismatched_context = build_context_record(
        payload,
        project_kind=context.project_kind,
        project_registry_key=context.project_registry_key,
    )
    mismatched_theorem = theorem.model_copy(update={"context_id": mismatched_context.context_id})
    mismatched_representation = _representation(0, mismatched_theorem)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (mismatched_theorem,),
        (mismatched_representation,),
        mismatched_context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(LF022PublicPoolCapacityError) as error:
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )

    assert error.value.rejection_counts["missing_or_mismatched_context"] == 1
    assert not (tmp_path / "artifacts/pool").exists()


def test_materializer_rejects_stale_representation_content_hash(tmp_path: Path) -> None:
    context = _context()
    theorem = _theorem(0, context)
    representation = _representation(0, theorem).model_copy(update={"headless": "(n : Nat) : True"})
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (representation,),
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(LF022PublicPoolCapacityError) as error:
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )

    assert error.value.rejection_counts["representation_content_hash_mismatch"] == 1
    assert not (tmp_path / "artifacts/pool").exists()


def test_materializer_rejects_representation_with_different_theorem_source(
    tmp_path: Path,
) -> None:
    context = _context()
    theorem = _theorem(0, context)
    representation = _representation(0, theorem)
    changed_raw = theorem.proof_stripped_declaration.replace("n = n", "True")
    payload = representation.model_dump(mode="python")
    payload["raw_proof_stripped"] = changed_raw
    payload["content_hash"] = representation_content_hash(
        {
            "raw_proof_stripped": changed_raw,
            "headless": representation.headless,
            "signature_pp": representation.signature_pp,
            "signature_explicit": representation.signature_explicit,
            "semantic_atoms": list(representation.semantic_atoms or ()),
            "operator_tree": representation.operator_tree,
            "alpha_identity_fingerprint": representation.alpha_identity_fingerprint,
        }
    )
    mismatched = RepresentationRecord.model_validate(payload)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (mismatched,),
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(LF022PublicPoolCapacityError) as error:
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )

    assert error.value.rejection_counts["representation_binding_mismatch"] == 1


def test_private_sft_classic_cannot_be_approved_or_materialized(
    tmp_path: Path,
) -> None:
    for source in (
        "formalmathatepfl/sft_classic",
        "hf://formalmathatepfl/sft_classic@private",
    ):
        with pytest.raises(ValueError, match="private sft_classic"):
            LF022ApprovedPublicSource(
                schema_version=1,
                source=source,
                source_revision=REVISION,
                license_id="unknown",
                license_evidence_uri="internal://not-public",
                approval_status="approved_public_research_compatible",
                source_is_public=True,
                redistribution_allowed=True,
                external_transmission_allowed=True,
                context_project_kind="mathlib",
                context_project_uri="https://github.com/leanprover-community/mathlib4",
                context_project_registry_key="mathlib",
            )


def test_registry_binding_must_match_supplied_registry(tmp_path: Path) -> None:
    context = _context()
    theorem = _theorem(0, context)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (_representation(0, theorem),),
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)
    different_registry = registry.model_copy(update={"policy_version": "different_policy"})

    with pytest.raises(
        LF022PublicPoolError,
        match="differs from its exact binding",
    ):
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=different_registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )


def test_output_directory_cannot_escape_repository(tmp_path: Path) -> None:
    context = _context()
    theorem = _theorem(0, context)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (_representation(0, theorem),),
        context,
    )
    registry, registry_binding = _registry_binding(tmp_path)

    with pytest.raises(LF022PublicPoolError, match="normalized path"):
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("../escape"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )


def test_denylist_content_hash_is_screened_before_selection(tmp_path: Path) -> None:
    context = _context()
    theorem = _theorem(0, context)
    representation = _representation(0, theorem)
    theorem_path, representation_path, context_path = _inputs(
        tmp_path,
        (theorem,),
        (representation,),
        context,
    )
    assert representation.signature_explicit is not None
    protected_hash = sha256_hex(representation.signature_explicit.encode("utf-8"))
    benchmark = FrozenBenchmark(
        registry_key="fixture_benchmark",
        source_id="fixture",
        revision=REVISION,
        resolved=True,
        splits={"test": 1},
        representation_hashes=(protected_hash,),
    )
    registry = FrozenRegistry(
        schema_version=1,
        frozen_at=NOW,
        policy_version="benchmark_denylist_v1",
        benchmarks=(benchmark,),
        representation_signatures_appended=True,
    )
    registry_path = tmp_path / "data/benchmarks/active.json"
    _write_json(registry_path, registry.model_dump(mode="json"))
    registry_binding = LF022ArtifactBinding(
        path=str(registry_path.relative_to(tmp_path)),
        sha256=hash_file(registry_path),
    )

    with pytest.raises(LF022PublicPoolError, match=r"denylist_content_hit.*1"):
        _materialize(
            repo_root=tmp_path,
            theorem_records_path=theorem_path,
            representation_records_path=representation_path,
            context_records_path=context_path,
            active_registry=registry,
            active_registry_binding=registry_binding,
            family_matrix=_family_matrix(),
            approved_sources=(_approval(),),
            output_directory=Path("artifacts/pool"),
            requested_count=1,
            profile="diagnostic_scaffold",
        )
