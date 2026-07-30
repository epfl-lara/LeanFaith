from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from leanfaith.cli import lf022_public_pool_operations as operations
from leanfaith.cli.app import app
from leanfaith.cli.lf022_public_pool_operations import (
    LF022ApprovedPublicSourcesFile,
    LF022PublicPoolOperationCode,
    LF022PublicPoolOperationError,
    LF022PublicPoolOperationRun,
    run_materialize_lf022_public_pool,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import FrozenRegistry
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022FamilyPin,
    LF022JSONLArtifactBinding,
    LF022ProductionFamilyMatrix,
    canonical_model_family,
    make_lf022_production_family_matrix,
)
from leanfaith.generation.lf022_public_pool import LF022PublicPoolCapacityError
from leanfaith.schemas.enums import ArtifactClass, DataStage
from leanfaith.schemas.manifest import CodeState, OutputManifest
from leanfaith.sources.mathlib_frame import (
    MathlibDomainAllocation,
    MathlibFileFrame,
    MathlibFrameMember,
    make_mathlib_frame_id,
)

NOW = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)
REVISION = "a" * 40
ROOT = Path(__file__).resolve().parents[2]


def test_checked_in_public_source_authorization_is_strict_and_non_executable() -> None:
    payload = yaml.safe_load(
        (ROOT / "configs/sources/lf022_public_sources_v1.yaml").read_text(encoding="utf-8")
    )
    authorization = LF022ApprovedPublicSourcesFile.model_validate(payload)

    assert authorization.public_sources_only is True
    assert authorization.network_execution_authorized is False
    assert authorization.semantic_labels_included is False
    assert len(authorization.approved_sources) == 1
    assert authorization.approved_sources[0].source == "mathlib"
    assert authorization.approved_sources[0].context_project_kind == "git"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes({"fixture": True}) + b"\n")


def _pin(family_id: str, model_id: str, digit: str) -> LF022FamilyPin:
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
        _pin("kimi", "moonshotai/Kimi-K2.7-Code", "1"),
        _pin("qwen", "Qwen/Qwen3.5-397B-A17B", "2"),
        _pin("glm", "zai-org/GLM-5", "3"),
        _pin("deepseek", "deepseek-ai/DeepSeek-V3.2", "4"),
        _pin("heldout", "anthropic/claude-opus-4.8", "5"),
    )
    return make_lf022_production_family_matrix(
        family_registry=registry,
        proposer_family_ids=("kimi", "qwen", "glm"),
        judge_family_ids=("kimi", "qwen", "glm", "deepseek"),
        sci_validator_family_ids=("kimi", "qwen", "glm", "deepseek"),
        heldout_eval_family_id="heldout",
    )


def _approved_sources() -> LF022ApprovedPublicSourcesFile:
    return LF022ApprovedPublicSourcesFile.model_validate(
        {
            "schema_version": 1,
            "approved_sources": [
                {
                    "schema_version": 1,
                    "source": "mathlib",
                    "source_revision": REVISION,
                    "license_id": "Apache-2.0",
                    "license_evidence_uri": (
                        f"https://github.com/leanprover-community/mathlib4/blob/{REVISION}/LICENSE"
                    ),
                    "approval_status": "approved_public_research_compatible",
                    "source_is_public": True,
                    "redistribution_allowed": True,
                    "external_transmission_allowed": True,
                    "context_project_kind": "mathlib",
                    "context_project_uri": "https://github.com/leanprover-community/mathlib4",
                    "context_project_registry_key": "mathlib",
                }
            ],
            "public_sources_only": True,
            "network_execution_authorized": False,
            "semantic_labels_included": False,
        }
    )


def _prepare_inputs(root: Path, *, approved_yaml: bool = False) -> dict[str, Path]:
    theorem = root / "inputs/theorems.jsonl"
    representation = root / "inputs/representations.jsonl"
    context = root / "inputs/contexts.jsonl"
    for path in (theorem, representation, context):
        _write_jsonl(path)
    frame_member = MathlibFrameMember(
        relative_path="Mathlib/Public/Fixture.lean",
        sha256=hash_canonical({"fixture": "source"}),
        domain="Public",
        selection_rank_sha256=hash_canonical({"fixture": "rank"}),
    )
    frame_payload: dict[str, object] = {
        "schema_version": 1,
        "selection_algorithm": "mathlib_domain_progressive_proportional_hash_v1",
        "source": "mathlib",
        "revision": REVISION,
        "private_source": False,
        "release_eligible": True,
        "inventory_adapter_version": "mathlib_adapter_v1",
        "inventory_id": f"mathlib_repo_inventory_v1:{'6' * 64}",
        "inventory_file_count": 1,
        "eligible_file_count": 1,
        "excluded_file_count": 0,
        "excluded_domains": [],
        "target_file_count": 1,
        "selected_file_count": 1,
        "selection_seed_sha256": "7" * 64,
        "domain_allocations": [
            MathlibDomainAllocation(
                domain="Public",
                inventory_file_count=1,
                selected_file_count=1,
            ).model_dump(mode="json")
        ],
        "members": [frame_member.model_dump(mode="json")],
    }
    frame = MathlibFileFrame.model_validate(
        {**frame_payload, "frame_id": make_mathlib_frame_id(frame_payload)}
    )
    frame_path = root / "inputs/mathlib_source_frame.json"
    frame_path.write_bytes(canonical_json_bytes(frame.model_dump(mode="json")) + b"\n")
    theorem_relative = theorem.relative_to(root).as_posix()
    frame_relative = frame_path.relative_to(root).as_posix()
    extraction_manifest = OutputManifest(
        stage=DataStage.ELABORATED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260728T000000Z_12345678",
        source="mathlib",
        source_revision=REVISION,
        config_hash="8" * 64,
        record_schema_version=1,
        row_count=1,
        file_checksums={theorem_relative: hash_file(theorem)},
        input_partition_checksums={frame_relative: hash_file(frame_path)},
        output_partition_checksums={theorem_relative: hash_file(theorem)},
        context_hash=hash_file(context),
        code=CodeState(git_revision="9" * 40, git_dirty=False),
        created_at=NOW,
    )
    extraction_manifest_path = root / "inputs/extraction_output_manifest.json"
    _write_json(extraction_manifest_path, extraction_manifest.model_dump(mode="json"))
    representation_relative = representation.relative_to(root).as_posix()
    representation_manifest = OutputManifest(
        stage=DataStage.REPRESENTED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260728T000000Z_87654321",
        source="mathlib_public_fixture",
        source_revision="from_theorem_partition",
        config_hash="a" * 64,
        record_schema_version=1,
        row_count=1,
        attempted_row_count=1,
        terminal_outcome_counts={"represented": 1, "view_failures": 0},
        file_checksums={representation_relative: hash_file(representation)},
        input_partition_checksums={theorem_relative: hash_file(theorem)},
        output_partition_checksums={representation_relative: hash_file(representation)},
        context_hash=hash_canonical({"context_id": "ctx:" + "0" * 64}),
        code=extraction_manifest.code,
        created_at=NOW,
    )
    representation_manifest_path = root / "inputs/representation_output_manifest.json"
    _write_json(
        representation_manifest_path,
        representation_manifest.model_dump(mode="json"),
    )
    registry = FrozenRegistry(
        frozen_at=NOW,
        benchmarks=(),
        representation_signatures_appended=True,
    )
    registry_path = root / "inputs/active_registry.json"
    matrix_path = root / "inputs/family_matrix.json"
    approved_path = root / (
        "inputs/approved_sources.yaml" if approved_yaml else "inputs/approved_sources.json"
    )
    _write_json(registry_path, registry.model_dump(mode="json"))
    matrix = _family_matrix()
    _write_json(matrix_path, matrix.model_dump(mode="json"))
    if approved_yaml:
        approved_path.write_text(
            "\n".join(
                [
                    "schema_version: 1",
                    "approved_sources:",
                    "  - schema_version: 1",
                    "    source: mathlib",
                    f"    source_revision: {REVISION}",
                    "    license_id: Apache-2.0",
                    "    license_evidence_uri: https://example.test/LICENSE",
                    "    approval_status: approved_public_research_compatible",
                    "    source_is_public: true",
                    "    redistribution_allowed: true",
                    "    external_transmission_allowed: true",
                    "    context_project_kind: mathlib",
                    "    context_project_uri: https://github.com/leanprover-community/mathlib4",
                    "    context_project_registry_key: mathlib",
                    "public_sources_only: true",
                    "network_execution_authorized: false",
                    "semantic_labels_included: false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        _write_json(approved_path, _approved_sources().model_dump(mode="json"))
    return {
        "theorem": theorem,
        "representation": representation,
        "context": context,
        "extraction_manifest": extraction_manifest_path,
        "representation_manifest": representation_manifest_path,
        "frame": frame_path,
        "registry": registry_path,
        "matrix": matrix_path,
        "approved": approved_path,
    }


def _jsonl_binding(root: Path, path: Path) -> LF022JSONLArtifactBinding:
    return LF022JSONLArtifactBinding(
        path=path.relative_to(root).as_posix(),
        sha256=hash_file(path),
        record_count=1,
    )


def _successful_materializer(
    *,
    mutate_after: Path | None = None,
) -> Any:
    def materialize(**kwargs: object) -> object:
        root = Path(kwargs["repo_root"])  # type: ignore[arg-type]
        output = Path(kwargs["output_directory"])  # type: ignore[arg-type]
        output = output if output.is_absolute() else root / output
        output.mkdir(parents=True, exist_ok=True)
        audit_path = output / "audit.json"
        audit_path.write_text("{}\n", encoding="utf-8")
        theorem_path = Path(kwargs["theorem_records_path"])  # type: ignore[arg-type]
        representation_path = Path(kwargs["representation_records_path"])  # type: ignore[arg-type]
        context_path = Path(kwargs["context_records_path"])  # type: ignore[arg-type]
        active_binding = kwargs["active_registry_binding"]
        extraction_manifest_binding = kwargs["extraction_output_manifest_binding"]
        representation_manifest_binding = kwargs["representation_output_manifest_binding"]
        source_frame_binding = kwargs["mathlib_source_frame_binding"]
        audit = SimpleNamespace(
            audit_id=f"lf022_public_pool_audit:{'1' * 64}",
            input_theorems=_jsonl_binding(root, theorem_path),
            input_representations=_jsonl_binding(root, representation_path),
            input_contexts=_jsonl_binding(root, context_path),
            input_extraction_output_manifest=extraction_manifest_binding,
            input_representation_output_manifest=representation_manifest_binding,
            input_mathlib_source_frame=source_frame_binding,
            active_benchmark_registry=active_binding,
            eligible_count=2,
            eligible_unique_ancestry_count=2,
            selected_count=1,
        )
        if mutate_after is not None:
            mutate_after.write_text('{"changed":true}\n', encoding="utf-8")
        admission = SimpleNamespace(
            admission_id=f"lf022_production_admission:{'2' * 64}",
            network_execution_authorized=False,
            semantic_labels_created=False,
        )
        plan = SimpleNamespace(
            manifest_id=f"lf022_production_plan:{'3' * 64}",
            network_execution_authorized=False,
            semantic_labels_created=False,
            execution_bindings_present=False,
        )
        return SimpleNamespace(
            audit=audit,
            audit_binding=LF022ArtifactBinding(
                path=audit_path.relative_to(root).as_posix(),
                sha256=hash_file(audit_path),
            ),
            admission=admission,
            plan=plan,
        )

    return materialize


def _run(
    root: Path,
    inputs: dict[str, Path],
    *,
    diagnostic_proposer_family_id: str | None = None,
    requested_count: int = 1,
    profile: str = "diagnostic_scaffold",
) -> LF022PublicPoolOperationRun:
    return run_materialize_lf022_public_pool(
        paths=RepoPaths(root=root),
        theorem_records_path=inputs["theorem"],
        representation_records_path=inputs["representation"],
        context_records_path=inputs["context"],
        extraction_output_manifest_path=inputs["extraction_manifest"],
        representation_output_manifest_path=inputs["representation_manifest"],
        mathlib_source_frame_path=inputs["frame"],
        active_registry_path=inputs["registry"],
        family_matrix_path=inputs["matrix"],
        approved_sources_path=inputs["approved"],
        output_directory=Path("artifacts/lf022/public_pool"),
        requested_count=requested_count,
        profile=profile,  # type: ignore[arg-type]
        diagnostic_proposer_family_id=diagnostic_proposer_family_id,
    )


@pytest.mark.parametrize("approved_yaml", [False, True])
def test_operation_loads_exact_inputs_and_returns_non_executable_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved_yaml: bool,
) -> None:
    inputs = _prepare_inputs(tmp_path, approved_yaml=approved_yaml)
    monkeypatch.setattr(
        operations,
        "materialize_lf022_public_pool",
        _successful_materializer(),
    )

    result = _run(tmp_path, inputs)

    assert result.summary.status == "materialized"
    assert result.summary.selected_count == 1
    assert result.summary.eligible_count == 2
    assert result.summary.network_execution_authorized is False
    assert result.summary.semantic_labels_created is False
    assert result.summary.non_executable_allocation_only is True
    assert result.summary.active_registry.sha256 == hash_file(inputs["registry"])


def test_operation_binds_supported_one_source_diagnostic_proposer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prepare_inputs(tmp_path)
    observed: dict[str, object] = {}

    def materialize(**kwargs: object) -> object:
        observed.update(kwargs)
        return _successful_materializer()(**kwargs)

    monkeypatch.setattr(operations, "materialize_lf022_public_pool", materialize)

    _run(
        tmp_path,
        inputs,
        diagnostic_proposer_family_id="qwen3",
    )

    assert observed["diagnostic_proposer_family_id"] == "qwen3"


@pytest.mark.parametrize(
    ("family_id", "profile", "requested_count"),
    (
        ("unsupported", "diagnostic_scaffold", 1),
        ("qwen3", "pilot_scaffold", 1),
        ("qwen3", "diagnostic_scaffold", 2),
    ),
)
def test_operation_rejects_invalid_diagnostic_proposer_scope_before_loading_inputs(
    tmp_path: Path,
    family_id: str,
    profile: str,
    requested_count: int,
) -> None:
    inputs = _prepare_inputs(tmp_path)
    with pytest.raises(LF022PublicPoolOperationError) as caught:
        _run(
            tmp_path,
            inputs,
            diagnostic_proposer_family_id=family_id,
            profile=profile,
            requested_count=requested_count,
        )

    assert caught.value.failure.code is LF022PublicPoolOperationCode.INVALID_REQUEST


def test_operation_rejects_duplicate_json_keys(
    tmp_path: Path,
) -> None:
    inputs = _prepare_inputs(tmp_path)
    inputs["registry"].write_text(
        (
            '{"schema_version":1,"schema_version":1,'
            f'"frozen_at":"{NOW.isoformat()}",'
            '"policy_version":"benchmark_denylist_v1","benchmarks":[],'
            '"representation_signatures_appended":true}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(LF022PublicPoolOperationError) as caught:
        _run(tmp_path, inputs)

    assert caught.value.failure.code is LF022PublicPoolOperationCode.INVALID_INPUT_SYNTAX
    assert "schema_version" not in caught.value.failure.message


def test_operation_rejects_duplicate_yaml_keys(
    tmp_path: Path,
) -> None:
    inputs = _prepare_inputs(tmp_path, approved_yaml=True)
    with inputs["approved"].open("a", encoding="utf-8") as handle:
        handle.write("public_sources_only: true\n")

    with pytest.raises(LF022PublicPoolOperationError) as caught:
        _run(tmp_path, inputs)

    assert caught.value.failure.code is LF022PublicPoolOperationCode.INVALID_INPUT_SYNTAX
    assert "public_sources_only" not in caught.value.failure.message


def test_operation_rejects_symlinked_input(
    tmp_path: Path,
) -> None:
    inputs = _prepare_inputs(tmp_path)
    target = inputs["matrix"]
    link = tmp_path / "inputs/family_matrix_link.json"
    link.symlink_to(target)
    inputs["matrix"] = link

    with pytest.raises(LF022PublicPoolOperationError) as caught:
        _run(tmp_path, inputs)

    assert caught.value.failure.code is LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH


def test_operation_rejects_input_hash_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prepare_inputs(tmp_path)
    monkeypatch.setattr(
        operations,
        "materialize_lf022_public_pool",
        _successful_materializer(mutate_after=inputs["theorem"]),
    )

    with pytest.raises(LF022PublicPoolOperationError) as caught:
        _run(tmp_path, inputs)

    assert caught.value.failure.code is LF022PublicPoolOperationCode.INPUT_HASH_DRIFT


def test_operation_normalizes_capacity_failure_without_rejection_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prepare_inputs(tmp_path)

    def insufficient(**_kwargs: object) -> object:
        raise LF022PublicPoolCapacityError(
            requested_count=10,
            eligible_count=3,
            eligible_unique_ancestry_count=2,
            rejection_counts={"private_source": 7},
        )

    monkeypatch.setattr(operations, "materialize_lf022_public_pool", insufficient)

    with pytest.raises(LF022PublicPoolOperationError) as caught:
        _run(tmp_path, inputs)

    failure = caught.value.failure
    assert failure.code is LF022PublicPoolOperationCode.INSUFFICIENT_CAPACITY
    assert failure.requested_count == 10
    assert failure.eligible_count == 3
    assert failure.eligible_unique_ancestry_count == 2
    assert "private_source" not in failure.message


def test_approved_source_file_rejects_executable_or_label_flags() -> None:
    payload = _approved_sources().model_dump(mode="json")
    payload["network_execution_authorized"] = True

    with pytest.raises(ValueError, match="network_execution_authorized"):
        LF022ApprovedPublicSourcesFile.model_validate(payload)


def test_public_pool_cli_help_exposes_exact_offline_inputs() -> None:
    result = CliRunner().invoke(
        app,
        ["materialize-lf022-public-pool", "--help"],
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert "--theorems" in result.output
    assert "--representations" in result.output
    assert "--contexts" in result.output
    assert "--extraction-manifest" in result.output
    assert "--representation-man" in result.output
    # Rich truncates long option names at the terminal edge.
    assert "--mathlib-source-fra" in result.output
    assert "--active-registry" in result.output
    assert "--requested-count" in result.output
    assert "--diagnostic-propose" in result.output
    assert "non-executable" in result.output
