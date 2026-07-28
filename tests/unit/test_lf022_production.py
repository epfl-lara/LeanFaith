from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.datasets.denylist import DenylistIndex, FrozenRegistry
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022AuthorizedExtractionMember,
    LF022FamilyPin,
    LF022JSONLArtifactBinding,
    LF022ProductionAdmission,
    LF022ProductionArtifactSet,
    LF022ProductionFamilyMatrix,
    LF022ProductionPlanError,
    LF022ProviderDeployment,
    build_lf022_production_plan,
    canonical_model_family,
    lf022_source_locator_id,
    load_lf022_production_plan,
    make_lf022_authorized_extraction_manifest,
    make_lf022_benchmark_registry_manifest,
    make_lf022_denylist_clearance_record,
    make_lf022_production_admission,
    make_lf022_production_family_matrix,
    make_lf022_production_source_record,
    make_lf022_provider_catalog_snapshot,
    make_lf022_public_source_authorization,
    make_lf022_public_source_authorization_registry,
    write_lf022_production_plan,
)
from leanfaith.schemas.enums import ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord

REVISION = "a" * 40
NOW = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)


def _write_json(root: Path, relative: str, value: object) -> LF022ArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def _write_jsonl(
    root: Path,
    relative: str,
    values: list[object],
) -> LF022JSONLArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )
    return LF022JSONLArtifactBinding(
        path=relative,
        sha256=hash_file(path),
        record_count=len(values),
    )


def _pin(family_id: str, model_id: str, digit: str) -> LF022FamilyPin:
    return LF022FamilyPin(
        family_id=family_id,
        model_id=model_id,
        canonical_family=canonical_model_family(model_id),
        pin_kind="exact_hf_checkpoint",
        checkpoint_revision=digit * 40,
        underlying_checkpoint_revision_status="exact",
    )


def _matrix() -> LF022ProductionFamilyMatrix:
    registry = (
        _pin("kimi_k2", "moonshotai/Kimi-K2.7-Code", "1"),
        _pin("qwen3", "Qwen/Qwen3.5-397B-A17B", "2"),
        _pin("glm5", "zai-org/GLM-5", "3"),
        _pin("deepseek", "deepseek-ai/DeepSeek-V3.2", "4"),
        _pin("anthropic", "anthropic/claude-opus-4.8", "7"),
    )
    return make_lf022_production_family_matrix(
        family_registry=registry,
        proposer_family_ids=("kimi_k2", "qwen3", "glm5"),
        judge_family_ids=("kimi_k2", "qwen3", "glm5", "deepseek"),
        sci_validator_family_ids=("kimi_k2", "qwen3", "glm5", "deepseek"),
        heldout_eval_family_id="anthropic",
    )


def _context() -> ContextRecord:
    fingerprint = "c" * 64
    header = "import Mathlib"
    return ContextRecord(
        schema_version=1,
        environment_schema_version=1,
        context_id=f"ctx:{fingerprint}",
        context_fingerprint=fingerprint,
        project_kind="mathlib",
        project_uri="https://github.com/leanprover-community/mathlib4",
        project_revision=REVISION,
        project_registry_key="mathlib",
        lean_version="4.31.0",
        lean_interact_version="0.11.4",
        repl_revision="b" * 40,
        imports=("Mathlib",),
        header_text=header,
        header_hash=hash_canonical({"header": header}),
    )


def _theorem(
    index: int,
    context: ContextRecord,
    *,
    include_dataset_style_locator: bool = True,
) -> TheoremRecord:
    theorem_id = f"thm:{index + 1:064x}"
    ancestry_id = f"anc:{index + 1:064x}"
    statement = f"theorem public_fixture_{index} (n : Nat) : n = n"
    source_locator = hash_canonical({"source": "mathlib", "revision": REVISION, "index": index})
    return TheoremRecord(
        schema_version=1,
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="mathlib",
        source_revision=REVISION,
        source_split="public_lf022_fixture",
        source_record=f"Mathlib/PublicFixture{index}.lean",
        source_record_id=source_locator if include_dataset_style_locator else None,
        source_file=f"Mathlib/PublicFixture{index}.lean",
        context_id=context.context_id,
        declaration_kind="theorem",
        declaration_name=f"public_fixture_{index}",
        declaration_full_name=f"LeanFaith.Public.public_fixture_{index}",
        proof_stripped_declaration=statement,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES,
        statement_content_hash=hash_canonical({"statement": statement}),
        nl_source_link=f"https://github.com/leanprover-community/mathlib4/{index}",
    )


def _representation(
    index: int,
    theorem: TheoremRecord,
    context: ContextRecord,
) -> RepresentationRecord:
    headless = f"(n : Nat) : n = n -- {index}"
    signature = f"theorem public_fixture_{index} (n : Nat) : Eq Nat n n"
    statuses = {
        "raw_proof_stripped": ViewStatus.OK,
        "headless": ViewStatus.OK,
        "signature_pp": ViewStatus.OK,
        "signature_explicit": ViewStatus.OK,
        "alpha_structural": ViewStatus.NOT_ATTEMPTED,
        "notation_light": ViewStatus.NOT_ATTEMPTED,
        "semantic_atoms": ViewStatus.OK,
        "operator_tree": ViewStatus.OK,
    }
    content = {"headless": headless, "signature": signature, "theorem_id": theorem.theorem_id}
    return RepresentationRecord(
        schema_version=1,
        representation_id=f"repr:{index + 101:064x}",
        theorem_id=theorem.theorem_id,
        normalization_version="representation_v1",
        context_id=context.context_id,
        raw_proof_stripped=theorem.proof_stripped_declaration,
        headless=headless,
        signature_pp=f"theorem public_fixture_{index} (n : Nat) : n = n",
        signature_explicit=signature,
        semantic_atoms=("Eq", "Nat"),
        operator_tree={"kind": "forall", "index": index},
        alpha_identity_fingerprint=hash_canonical({"alpha": content}),
        view_status=statuses,
        content_hash=hash_canonical(content),
        created_at=NOW,
    )


@dataclass(frozen=True)
class ProductionFixture:
    root: Path
    matrix: LF022ProductionFamilyMatrix
    artifacts: LF022ProductionArtifactSet
    admission: LF022ProductionAdmission
    theorems: tuple[TheoremRecord, ...]


def _fixture(
    root: Path,
    *,
    count: int = 4,
    profile: str = "diagnostic_scaffold",
    legacy_mathlib_records: bool = False,
) -> ProductionFixture:
    matrix = _matrix()
    context = _context()
    theorems = tuple(
        _theorem(
            index,
            context,
            include_dataset_style_locator=not legacy_mathlib_records,
        )
        for index in range(count)
    )
    representations = tuple(
        _representation(index, theorem, context) for index, theorem in enumerate(theorems)
    )
    matrix_binding = _write_json(
        root,
        "data/lf022/family_matrix.json",
        matrix.model_dump(mode="json"),
    )

    extraction = make_lf022_authorized_extraction_manifest(
        source="mathlib",
        source_revision=REVISION,
        members=tuple(
            LF022AuthorizedExtractionMember(
                source_locator_id=lf022_source_locator_id(theorem),
                theorem_id=theorem.theorem_id,
                statement_content_hash=theorem.statement_content_hash,
            )
            for theorem in theorems
        ),
    )
    extraction_binding = _write_json(
        root,
        "data/lf022/extraction.json",
        extraction.model_dump(mode="json"),
    )
    source_authorization = make_lf022_public_source_authorization(
        source="mathlib",
        source_revision=REVISION,
        license_id="Apache-2.0",
        license_evidence_uri="https://github.com/leanprover-community/mathlib4/blob/master/LICENSE",
        extraction_manifest=extraction_binding,
    )
    source_registry = make_lf022_public_source_authorization_registry(
        policy_version="public_source_authorization_v1",
        authorizations=(source_authorization,),
    )
    source_registry_binding = _write_json(
        root,
        "data/lf022/public_source_registry.json",
        source_registry.model_dump(mode="json"),
    )

    active_registry = FrozenRegistry(
        schema_version=1,
        frozen_at=NOW,
        policy_version="benchmark_denylist_v1",
        benchmarks=(),
        representation_signatures_appended=True,
    )
    active_registry_binding = _write_json(
        root,
        "data/lf022/frozen_ids.json",
        active_registry.model_dump(mode="json"),
    )
    benchmark_manifest = make_lf022_benchmark_registry_manifest(
        policy_version="benchmark_denylist_v1",
        active_registry=active_registry_binding,
    )
    benchmark_manifest_binding = _write_json(
        root,
        "data/lf022/benchmark_registry_manifest.json",
        benchmark_manifest.model_dump(mode="json"),
    )
    denylist_index = DenylistIndex(active_registry)

    clearances = []
    source_records = []
    for theorem, representation in zip(theorems, representations, strict=True):
        source_locator_id = lf022_source_locator_id(theorem)
        clearance = make_lf022_denylist_clearance_record(
            benchmark_manifest_id=benchmark_manifest.manifest_id,
            active_registry_file_sha256=active_registry_binding.sha256,
            active_registry_content_hash=denylist_index.registry_content_hash,
            source_locator_id=source_locator_id,
            theorem_id=theorem.theorem_id,
            theorem_statement_content_hash=theorem.statement_content_hash,
            representation_id=representation.representation_id,
            representation_content_hash=representation.content_hash,
            identifier_hits=(),
            content_hits=(),
        )
        clearances.append(clearance)
        source_records.append(
            make_lf022_production_source_record(
                source_locator_id=source_locator_id,
                source=theorem.source,
                source_revision=theorem.source_revision,
                theorem_id=theorem.theorem_id,
                theorem_statement_content_hash=theorem.statement_content_hash,
                representation_id=representation.representation_id,
                representation_content_hash=representation.content_hash,
                normalization_version=representation.normalization_version,
                context_id=context.context_id,
                context_fingerprint=context.context_fingerprint,
                context_header_hash=context.header_hash,
                public_source_authorization_id=source_authorization.authorization_id,
                denylist_clearance_id=clearance.clearance_id,
            )
        )

    artifacts = LF022ProductionArtifactSet(
        family_matrix=matrix_binding,
        public_source_authorization_registry=source_registry_binding,
        benchmark_registry_manifest=benchmark_manifest_binding,
        active_benchmark_registry=active_registry_binding,
        denylist_clearance_records=_write_jsonl(
            root,
            "data/lf022/denylist_clearances.jsonl",
            [record.model_dump(mode="json") for record in clearances],
        ),
        source_pool=_write_jsonl(
            root,
            "data/lf022/source_pool.jsonl",
            [record.model_dump(mode="json") for record in source_records],
        ),
        theorem_records=_write_jsonl(
            root,
            "data/lf022/theorems.jsonl",
            [record.model_dump(mode="json") for record in theorems],
        ),
        representation_records=_write_jsonl(
            root,
            "data/lf022/representations.jsonl",
            [record.model_dump(mode="json") for record in representations],
        ),
        context_records=_write_jsonl(
            root,
            "data/lf022/contexts.jsonl",
            [context.model_dump(mode="json")],
        ),
    )
    admission = make_lf022_production_admission(
        family_matrix=matrix,
        artifacts=artifacts,
        profile=profile,  # type: ignore[arg-type]
    )
    return ProductionFixture(root, matrix, artifacts, admission, theorems)


def _replace_artifacts(
    fixture: ProductionFixture,
    **changes: LF022ArtifactBinding,
) -> LF022ProductionAdmission:
    values = fixture.artifacts.model_dump(mode="python")
    values.update(changes)
    artifacts = LF022ProductionArtifactSet.model_validate(values)
    return make_lf022_production_admission(
        family_matrix=fixture.matrix,
        artifacts=artifacts,
        profile=fixture.admission.profile,
    )


def test_plan_is_deterministic_rotated_and_explicitly_non_executable(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    first = build_lf022_production_plan(
        repo_root=fixture.root,
        admission=fixture.admission,
        family_matrix=fixture.matrix,
    )
    second = build_lf022_production_plan(
        repo_root=fixture.root,
        admission=fixture.admission,
        family_matrix=fixture.matrix,
    )

    assert first == second
    assert first.scientific_status == "diagnostic_only"
    assert first.execution_bindings_present is False
    assert first.network_execution_authorized is False
    assert all(task.task_kind == "non_executable_allocation" for task in first.tasks)
    assert all(task.executable is False for task in first.tasks)
    sci_tasks = [task for task in first.tasks if task.distribution == "G_sci"]
    assert [task.proposer_family_id for task in sci_tasks] == [
        "kimi_k2",
        "qwen3",
        "glm5",
        "kimi_k2",
    ]
    assert [(task.judge_family_ids, task.sci_validator_family_id) for task in sci_tasks] == [
        (("qwen3", "glm5"), "deepseek"),
        (("glm5", "deepseek"), "kimi_k2"),
        (("deepseek", "kimi_k2"), "qwen3"),
        (("deepseek", "qwen3"), "glm5"),
    ]


def test_legacy_gate3_mathlib_theorems_without_source_record_id_are_admitted(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, count=2, legacy_mathlib_records=True)
    assert all(theorem.source_record_id is None for theorem in fixture.theorems)

    plan = build_lf022_production_plan(
        repo_root=fixture.root,
        admission=fixture.admission,
        family_matrix=fixture.matrix,
    )

    assert plan.unique_source_count == 2
    assert {task.source_locator_id for task in plan.tasks} == {
        lf022_source_locator_id(theorem) for theorem in fixture.theorems
    }


def test_git_declaration_locator_ignores_content_and_extraction_outputs() -> None:
    theorem = _theorem(0, _context(), include_dataset_style_locator=False).model_copy(
        update={"source_range": (20, 21), "declaration_ordinal": 17}
    )
    locator = lf022_source_locator_id(theorem)
    changed_extraction = theorem.model_copy(
        update={
            "proof_stripped_declaration": "theorem changed (n : Nat) : n = n",
            "statement_content_hash": "f" * 64,
            "source_range": (200, 240),
            "declaration_ordinal": 999,
            "declaration_kind": "lemma",
            "source_split": "changed_nonidentity_metadata",
        }
    )

    assert lf022_source_locator_id(changed_extraction) == locator
    assert changed_extraction.theorem_id == theorem.theorem_id
    assert changed_extraction.ancestry_id == theorem.ancestry_id
    assert (
        lf022_source_locator_id(
            theorem.model_copy(update={"source_file": "Mathlib/OtherFixture.lean"})
        )
        != locator
    )
    assert (
        lf022_source_locator_id(
            theorem.model_copy(update={"declaration_full_name": "LeanFaith.Public.other"})
        )
        != locator
    )
    assert (
        lf022_source_locator_id(theorem.model_copy(update={"source_revision": "b" * 40})) != locator
    )


def test_dataset_source_record_locator_is_used_verbatim() -> None:
    theorem = _theorem(0, _context())
    assert theorem.source_record_id is not None
    assert lf022_source_locator_id(theorem) == theorem.source_record_id


@pytest.mark.parametrize(
    "updates",
    (
        {"source_file": None},
        {"declaration_full_name": None},
        {"source_revision": "mutable-tag"},
    ),
)
def test_git_declaration_locator_fails_closed_without_immutable_identity(
    updates: dict[str, object],
) -> None:
    theorem = _theorem(0, _context(), include_dataset_style_locator=False).model_copy(
        update=updates
    )
    with pytest.raises(LF022ProductionPlanError, match="stable source locator"):
        lf022_source_locator_id(theorem)


def test_plan_canonical_writer_and_loader_round_trip(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, count=1)
    plan = build_lf022_production_plan(
        repo_root=fixture.root,
        admission=fixture.admission,
        family_matrix=fixture.matrix,
    )
    binding = write_lf022_production_plan(
        repo_root=tmp_path,
        relative_path="artifacts/lf022/production_plan.json",
        plan=plan,
    )
    assert (
        write_lf022_production_plan(
            repo_root=tmp_path,
            relative_path=binding.path,
            plan=plan,
        )
        == binding
    )
    output = tmp_path / binding.path
    assert output.read_bytes() == canonical_json_bytes(plan.model_dump(mode="json"))
    assert load_lf022_production_plan(repo_root=tmp_path, binding=binding) == plan

    output.write_bytes(output.read_bytes() + b"\n")
    with pytest.raises(LF022ProductionPlanError, match="not canonical JSON"):
        load_lf022_production_plan(
            repo_root=tmp_path,
            binding=LF022ArtifactBinding(path=binding.path, sha256=hash_file(output)),
        )


def test_kimi_26_and_27_cannot_count_as_distinct_families() -> None:
    kimi_26 = _pin("kimi_26", "moonshotai/Kimi-K2.6", "8")
    kimi_27 = _pin("kimi_27", "moonshotai/Kimi-K2.7-Code", "9")
    assert kimi_26.canonical_family == kimi_27.canonical_family == "moonshotai/kimi-k2"
    with pytest.raises(ValueError, match="unique canonical model families"):
        make_lf022_production_family_matrix(
            family_registry=(
                kimi_26,
                kimi_27,
                _pin("glm5", "zai-org/GLM-5", "3"),
                _pin("deepseek", "deepseek-ai/DeepSeek-V3.2", "4"),
                _pin("anthropic", "anthropic/claude-opus-4.8", "7"),
            ),
            proposer_family_ids=("kimi_26", "kimi_27", "glm5"),
            judge_family_ids=("kimi_26", "kimi_27", "glm5", "deepseek"),
            sci_validator_family_ids=("kimi_26", "kimi_27", "glm5", "deepseek"),
            heldout_eval_family_id="anthropic",
        )


@pytest.mark.parametrize(
    "source",
    (
        "formalmathatepfl/sft_classic",
        "sft_classic",
        "datasets/formalmathatepfl/sft_classic",
        "hf://formalmathatepfl/sft_classic@" + REVISION,
        "alternate-owner/sft_classic",
        "hf://alternate-owner/sft_classic@" + REVISION,
    ),
)
def test_private_sft_classic_source_spellings_fail_at_extraction_manifest(
    source: str,
) -> None:
    with pytest.raises(ValueError, match="private sft_classic"):
        make_lf022_authorized_extraction_manifest(
            source=source,
            source_revision=REVISION,
            members=(
                LF022AuthorizedExtractionMember(
                    source_locator_id="1" * 64,
                    theorem_id="thm:" + "2" * 64,
                    statement_content_hash="3" * 64,
                ),
            ),
        )


def test_public_sft_classic_numina_is_not_a_private_source_false_positive() -> None:
    manifest = make_lf022_authorized_extraction_manifest(
        source="formalmathatepfl/sft_classic_numina",
        source_revision=REVISION,
        members=(
            LF022AuthorizedExtractionMember(
                source_locator_id="1" * 64,
                theorem_id="thm:" + "2" * 64,
                statement_content_hash="3" * 64,
            ),
        ),
    )

    assert manifest.source == "formalmathatepfl/sft_classic_numina"


def test_rcp_provider_pin_binds_catalog_without_inventing_checkpoint_sha(
    tmp_path: Path,
) -> None:
    deployment = LF022ProviderDeployment(
        model_id="moonshotai/Kimi-K2.7-Code",
        deployment_id="rcp-prod-kimi-k2-7-code",
    )
    catalog = make_lf022_provider_catalog_snapshot(
        provider_id="rcp",
        deployments=(deployment,),
    )
    binding = _write_json(
        tmp_path,
        "data/lf022/rcp_catalog.json",
        catalog.model_dump(mode="json"),
    )
    pin = LF022FamilyPin(
        family_id="kimi_k2",
        model_id=deployment.model_id,
        canonical_family=canonical_model_family(deployment.model_id),
        pin_kind="provider_deployment_snapshot",
        provider_id="rcp",
        provider_deployment_id=deployment.deployment_id,
        provider_catalog_artifact=binding,
        underlying_checkpoint_revision_status="provider_not_disclosed",
    )
    assert pin.checkpoint_revision is None
    assert pin.provider_catalog_artifact == binding


def test_unregistered_public_source_cannot_self_assert_license(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, count=1)
    source_path = tmp_path / fixture.artifacts.source_pool.path
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    source_payload["source"] = "attacker/arbitrary-public-source"
    source_payload["source_revision"] = "f" * 40
    payload = {key: value for key, value in source_payload.items() if key != "admission_record_id"}
    source_payload["admission_record_id"] = make_id("lf022_source_admission", payload)
    source_binding = _write_jsonl(
        tmp_path,
        fixture.artifacts.source_pool.path,
        [source_payload],
    )
    admission = _replace_artifacts(fixture, source_pool=source_binding)
    with pytest.raises(LF022ProductionPlanError, match="absent from public authorization"):
        build_lf022_production_plan(
            repo_root=tmp_path,
            admission=admission,
            family_matrix=fixture.matrix,
        )


def test_forged_clearance_cannot_override_actual_active_registry(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, count=1)
    theorem = fixture.theorems[0]
    assert theorem.source_record_id is not None
    representation_payload = json.loads(
        (tmp_path / fixture.artifacts.representation_records.path).read_text(encoding="utf-8")
    )
    registry_path = tmp_path / fixture.artifacts.active_benchmark_registry.path
    registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    registry_payload["benchmarks"] = [
        {
            "registry_key": "protected_fixture",
            "source_id": "fixture",
            "revision": REVISION,
            "role": "evaluation_only",
            "resolved": True,
            "splits": {"test": 1},
            "row_ids": [theorem.theorem_id],
            "nl_hashes": [],
            "text_hashes": [],
            "representation_hashes": [],
            "resolution_plan": "",
        }
    ]
    active_binding = _write_json(
        tmp_path,
        fixture.artifacts.active_benchmark_registry.path,
        registry_payload,
    )
    manifest_path = tmp_path / fixture.artifacts.benchmark_registry_manifest.path
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["active_registry"] = active_binding.model_dump(mode="json")
    manifest_without_id = {
        key: value for key, value in manifest_payload.items() if key != "manifest_id"
    }
    manifest_payload["manifest_id"] = make_id("lf022_benchmark_registry", manifest_without_id)
    manifest_binding = _write_json(
        tmp_path,
        fixture.artifacts.benchmark_registry_manifest.path,
        manifest_payload,
    )
    forged_clearance = make_lf022_denylist_clearance_record(
        benchmark_manifest_id=manifest_payload["manifest_id"],
        active_registry_file_sha256=active_binding.sha256,
        active_registry_content_hash=DenylistIndex(
            FrozenRegistry.model_validate(registry_payload)
        ).registry_content_hash,
        source_locator_id=theorem.source_record_id,
        theorem_id=theorem.theorem_id,
        theorem_statement_content_hash=theorem.statement_content_hash,
        representation_id=representation_payload["representation_id"],
        representation_content_hash=representation_payload["content_hash"],
        # An attacker has updated every binding but still falsely claims clear.
        identifier_hits=(),
        content_hits=(),
    )
    clearance_binding = _write_jsonl(
        tmp_path,
        fixture.artifacts.denylist_clearance_records.path,
        [forged_clearance.model_dump(mode="json")],
    )
    source_path = tmp_path / fixture.artifacts.source_pool.path
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    source_payload["denylist_clearance_id"] = forged_clearance.clearance_id
    source_without_id = {
        key: value for key, value in source_payload.items() if key != "admission_record_id"
    }
    source_payload["admission_record_id"] = make_id(
        "lf022_source_admission",
        source_without_id,
    )
    source_binding = _write_jsonl(
        tmp_path,
        fixture.artifacts.source_pool.path,
        [source_payload],
    )
    admission = _replace_artifacts(
        fixture,
        active_benchmark_registry=active_binding,
        benchmark_registry_manifest=manifest_binding,
        denylist_clearance_records=clearance_binding,
        source_pool=source_binding,
    )
    with pytest.raises(LF022ProductionPlanError, match="checker output does not replay"):
        build_lf022_production_plan(
            repo_root=tmp_path,
            admission=admission,
            family_matrix=fixture.matrix,
        )


def test_scientific_profile_cannot_be_claimed_by_small_diagnostic_plan(
    tmp_path: Path,
) -> None:
    fixture = _fixture(
        tmp_path,
        count=12,
        profile="scientific_production_scaffold",
    )
    with pytest.raises(LF022ProductionPlanError, match="at least 10000"):
        build_lf022_production_plan(
            repo_root=tmp_path,
            admission=fixture.admission,
            family_matrix=fixture.matrix,
        )


def test_family_matrix_artifact_is_required_and_replayed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, count=1)
    matrix_path = tmp_path / fixture.artifacts.family_matrix.path
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    payload["proposer_family_ids"] = ["qwen3", "kimi_k2", "glm5"]
    without_id = {key: value for key, value in payload.items() if key != "matrix_id"}
    payload["matrix_id"] = make_id("lf022_family_matrix", without_id)
    binding = _write_json(tmp_path, fixture.artifacts.family_matrix.path, payload)
    admission = _replace_artifacts(fixture, family_matrix=binding)
    with pytest.raises(LF022ProductionPlanError, match="differs from bound artifact"):
        build_lf022_production_plan(
            repo_root=tmp_path,
            admission=admission,
            family_matrix=fixture.matrix,
        )


def test_duplicate_theorem_records_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, count=1)
    theorem = fixture.theorems[0].model_dump(mode="json")
    theorem_binding = _write_jsonl(
        tmp_path,
        fixture.artifacts.theorem_records.path,
        [theorem, theorem],
    )
    admission = _replace_artifacts(fixture, theorem_records=theorem_binding)
    with pytest.raises(LF022ProductionPlanError, match="duplicate theorem record"):
        build_lf022_production_plan(
            repo_root=tmp_path,
            admission=admission,
            family_matrix=fixture.matrix,
        )
