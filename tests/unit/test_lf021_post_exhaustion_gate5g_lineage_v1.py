from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.generation import extended_gate5g
from leanfaith.generation import post_exhaustion_collection_v6 as collection_v6
from leanfaith.generation import post_exhaustion_gate5g_lineage_v1 as subject
from leanfaith.generation import post_exhaustion_postprocess_v7 as postprocess_v7
from leanfaith.generation import research_collection_v5 as collection_v5
from leanfaith.generation import research_postprocess as postprocess_v1
from leanfaith.generation import research_postprocess_v6 as postprocess_v6
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GFamilyRevisionBinding,
    Gate5GObservationBinding,
    Gate5GReplayCertificateV1,
    Gate5GTrancheBindingV1,
)

ROOT = Path(__file__).resolve().parents[2]
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
ALGEBRA_S0_POSTPROCESS = Path(
    "data/raw/real_outputs/gate3_docstrings_operational_v1/v2/local_collection/"
    "3801b405ec8b7008f8c38f449189a52fe5e74bea3a98f5e3e0abdaa75edac62c/"
    "postprocess_v3/manifest.json"
)


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_legacy(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _binding(root: Path, path: Path) -> Gate5GArtifactBinding:
    return Gate5GArtifactBinding(
        artifact=str(path.relative_to(root)),
        sha256=hash_file(path),
    )


def _postprocess_binding(
    root: Path,
    path: Path,
) -> postprocess_v1.PostprocessArtifactBinding:
    return postprocess_v1.PostprocessArtifactBinding(
        artifact=str(path.relative_to(root)),
        sha256=hash_file(path),
    )


def _fixture_dependency(
    root: Path,
    path: Path,
) -> postprocess_v1.PostprocessArtifactBinding:
    if not path.exists():
        _write(
            path,
            {
                "fixture_artifact": str(path.relative_to(root)),
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
    return _postprocess_binding(root, path)


def _simple_manifest(root: Path) -> tuple[Path, Gate5GArtifactBinding]:
    terminal = root / "artifacts/terminal.json"
    _write(
        terminal,
        {
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        },
    )
    manifest = root / "artifacts/manifest.json"
    _write(
        manifest,
        {
            "terminal_artifacts": {
                str(terminal.relative_to(root)): hash_file(terminal),
            },
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        },
    )
    return manifest, _binding(root, manifest)


def test_real_algebra_s0_legacy_manifest_and_family_sessions_are_accepted() -> None:
    postprocess_path = ROOT / ALGEBRA_S0_POSTPROCESS
    postprocess_binding = _binding(ROOT, postprocess_path)
    postprocess_raw = subject._load_bound_registered_manifest_json(
        repo_root=ROOT,
        binding=postprocess_binding,
        label="algebra_s0 postprocess manifest",
        kind="postprocess",
    )
    postprocess = subject._project_registered_postprocess_manifest(
        postprocess_raw,
        expected_tranche_id="algebra_s0",
        label="algebra_s0 postprocess manifest",
    )
    collection_raw = subject._load_bound_registered_manifest_json(
        repo_root=ROOT,
        binding=postprocess.input_binding.collection_manifest,
        label="algebra_s0 collection manifest",
        kind="collection",
    )
    collection = subject._project_registered_collection_manifest(
        collection_raw,
        expected_tranche_id="algebra_s0",
        label="algebra_s0 collection manifest",
    )

    assert postprocess.manifest_id.startswith("research_postprocess_v3_manifest:")
    assert collection.manifest_id.startswith("research_collection_manifest_v2:")
    assert (
        tuple(
            item.family_id
            for item in subject._family_revisions(
                repo_root=ROOT,
                collection=collection,
            )
        )
        == FAMILIES
    )


def test_legacy_manifest_loader_rejects_reencoding_and_semantic_tamper(
    tmp_path: Path,
) -> None:
    source = ROOT / ALGEBRA_S0_POSTPROCESS
    exact = source.read_bytes()
    destination = tmp_path / "legacy/manifest.json"
    destination.parent.mkdir(parents=True)

    destination.write_bytes(exact)
    binding = _binding(tmp_path, destination)
    subject._load_bound_registered_manifest_json(
        repo_root=tmp_path,
        binding=binding,
        label="legacy postprocess manifest",
        kind="postprocess",
    )

    for mutated in (exact[:-1], exact[:-1] + b" \n", exact + b"\n"):
        destination.write_bytes(mutated)
        with pytest.raises(
            subject.PostExhaustionGate5GLineageError,
            match="exact legacy canonical-JSON-plus-LF",
        ):
            subject._load_bound_registered_manifest_json(
                repo_root=tmp_path,
                binding=_binding(tmp_path, destination),
                label="legacy postprocess manifest",
                kind="postprocess",
            )

    tampered = subject._json_object(exact, label="algebra_s0 fixture")
    tampered["terminal_invocations"] -= 1
    destination.write_bytes(canonical_json_bytes(tampered) + b"\n")
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="registered schema",
    ):
        subject._load_bound_registered_manifest_json(
            repo_root=tmp_path,
            binding=_binding(tmp_path, destination),
            label="legacy postprocess manifest",
            kind="postprocess",
        )


def test_binding_extraction_distinguishes_semantic_names_from_paths() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    bindings = subject._extract_bindings(
        {
            # Collection terminals use semantic component names here.  These
            # hashes are validated by the registered terminal schema, but the
            # keys are not repository paths and must not be traversed.
            "artifact_hashes": {
                "provider_request": digest_a,
                "provider_boundary": digest_b,
            },
            # Postprocess terminals and top-level manifests use actual paths.
            "output_artifact_hashes": {
                "reports/example.json": digest_a,
            },
        }
    )

    assert bindings == (
        Gate5GArtifactBinding(
            artifact="reports/example.json",
            sha256=digest_a,
        ),
    )


def test_explicit_absolute_content_addressed_binding_is_read_no_follow(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.bin"
    external.write_bytes(b"immutable external bytes")
    digest = hash_file(external)
    bindings = subject._extract_bindings(
        {
            "code_bundle": {
                "artifact": str(external),
                "sha256": digest,
                "location_kind": "absolute_content_addressed",
            }
        }
    )
    assert bindings == (Gate5GArtifactBinding(artifact=str(external), sha256=digest),)
    path, artifact, payload = subject._read_absolute_content_addressed_no_follow(
        artifact=bindings[0].artifact,
        expected_sha256=bindings[0].sha256,
        label="external test artifact",
    )
    assert path == external
    assert artifact == str(external)
    assert payload == b"immutable external bytes"

    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="lacks an explicit location kind",
    ):
        subject._extract_bindings({"artifact": str(external), "sha256": digest})
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="non-repository artifact path",
    ):
        subject._extract_bindings({"output_artifact_hashes": {str(external): digest}})
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="hash differs",
    ):
        subject._read_absolute_content_addressed_no_follow(
            artifact=str(external),
            expected_sha256="f" * 64,
            label="tampered external test artifact",
        )

    final_link = tmp_path / "final-link.bin"
    final_link.symlink_to(external)
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="missing, symlinked, or unreadable",
    ):
        subject._read_absolute_content_addressed_no_follow(
            artifact=str(final_link),
            expected_sha256=digest,
            label="symlinked external test artifact",
        )

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_file = real_parent / "artifact.bin"
    parent_file.write_bytes(b"parent artifact")
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="missing or symlinked component",
    ):
        subject._read_absolute_content_addressed_no_follow(
            artifact=str(parent_link / parent_file.name),
            expected_sha256=hash_file(parent_file),
            label="parent-symlinked external test artifact",
        )


def test_replay_certificate_is_independently_recomputed_and_idempotent(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _simple_manifest(tmp_path)
    output = tmp_path / "reports/lineage"
    certificate, path, binding = subject.seal_gate5g_replay_certificate_v1(
        repo_root=tmp_path,
        manifest=manifest,
        tranche_id="algebra_s6",
        kind="postprocess",
        expected_record_count=1,
        output_root=output,
    )
    assert (
        subject.verify_gate5g_replay_certificate_v1_exact(
            repo_root=tmp_path,
            certificate_binding=binding,
            manifest=manifest,
            tranche_id="algebra_s6",
            kind="postprocess",
            expected_record_count=1,
        )
        == certificate
    )
    rerun = subject.seal_gate5g_replay_certificate_v1(
        repo_root=tmp_path,
        manifest=manifest,
        tranche_id="algebra_s6",
        kind="postprocess",
        expected_record_count=1,
        output_root=output,
    )
    assert rerun[0] == certificate
    assert rerun[1] == path
    assert path.read_bytes() == canonical_json_bytes(certificate.model_dump(mode="json"))
    assert hash_file(manifest_path) == manifest.sha256


def test_hand_authored_equal_hash_assertion_is_rejected(tmp_path: Path) -> None:
    _, manifest = _simple_manifest(tmp_path)
    forged = Gate5GReplayCertificateV1(
        report_kind="lf021_collection_replay_certificate_v1",
        tranche_id="algebra_s6",
        manifest=manifest,
        replayed=True,
        byte_identical=True,
        first_tree_sha256="f" * 64,
        replay_tree_sha256="f" * 64,
        expected_record_count=1,
        replay_record_count=1,
    )
    path = tmp_path / "forged.json"
    _write(path, forged.model_dump(mode="json"))
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="not independently reproduced",
    ):
        subject.verify_gate5g_replay_certificate_v1_exact(
            repo_root=tmp_path,
            certificate_binding=_binding(tmp_path, path),
            manifest=manifest,
            tranche_id="algebra_s6",
            kind="collection",
            expected_record_count=1,
        )


def test_replay_rejects_tamper_and_symlinked_input_or_output(tmp_path: Path) -> None:
    manifest_path, manifest = _simple_manifest(tmp_path)
    certificate, _, binding = subject.seal_gate5g_replay_certificate_v1(
        repo_root=tmp_path,
        manifest=manifest,
        tranche_id="algebra_s6",
        kind="collection",
        expected_record_count=1,
        output_root=tmp_path / "reports/lineage",
    )
    assert certificate.byte_identical
    terminal = tmp_path / "artifacts/terminal.json"
    terminal.write_bytes(b"tampered\n")
    with pytest.raises(subject.PostExhaustionGate5GLineageError, match="hash differs"):
        subject.verify_gate5g_replay_certificate_v1_exact(
            repo_root=tmp_path,
            certificate_binding=binding,
            manifest=manifest,
            tranche_id="algebra_s6",
            kind="collection",
            expected_record_count=1,
        )

    symlink = tmp_path / "manifest-link.json"
    symlink.symlink_to(manifest_path)
    with pytest.raises(subject.PostExhaustionGate5GLineageError, match="symlinked"):
        subject.seal_gate5g_replay_certificate_v1(
            repo_root=tmp_path,
            manifest=Gate5GArtifactBinding(
                artifact=str(symlink.relative_to(tmp_path)),
                sha256=manifest.sha256,
            ),
            tranche_id="algebra_s6",
            kind="collection",
            expected_record_count=1,
            output_root=tmp_path / "other",
        )

    clean_root = tmp_path / "clean"
    _, clean_manifest = _simple_manifest(clean_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    unsafe = clean_root / "reports"
    unsafe.symlink_to(outside, target_is_directory=True)
    with pytest.raises(subject.PostExhaustionGate5GLineageError, match="trusted"):
        subject.seal_gate5g_replay_certificate_v1(
            repo_root=clean_root,
            manifest=clean_manifest,
            tranche_id="algebra_s6",
            kind="collection",
            expected_record_count=1,
            output_root=unsafe / "lineage",
        )


def _make_tranche(
    *,
    root: Path,
    output_root: Path,
    index: int,
) -> Gate5GTrancheBindingV1:
    tranche_id = f"original_s{index}" if index < 12 else "extension_s0"
    base = root / "artifacts" / tranche_id
    expected_invocations = len(FAMILIES)
    seed_count_by_family = dict.fromkeys(FAMILIES, 1)
    invocation_ids = tuple(
        f"invocation:{index:02d}:{family_index}" for family_index in range(expected_invocations)
    )
    revisions: list[Gate5GFamilyRevisionBinding] = []
    session_hashes: dict[str, str] = {}
    for family_index, family_id in enumerate(FAMILIES):
        session_id = f"session:{index:02d}:{family_index}"
        start = base / "families" / family_id / "family_session_start.json"
        end = base / "families" / family_id / "family_session_end.json"
        _write_legacy(
            start,
            {
                "schema_version": 1,
                "family_id": family_id,
                "family_session_id": session_id,
                "model_repo_id": f"models/{family_id}",
                "model_revision": f"{index + family_index + 1:040x}",
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        _write_legacy(
            end,
            {
                "schema_version": 1,
                "family_id": family_id,
                "family_session_id": session_id,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        start_binding = _binding(root, start)
        end_binding = _binding(root, end)
        session_hashes[start_binding.artifact] = start_binding.sha256
        session_hashes[end_binding.artifact] = end_binding.sha256
        revisions.append(
            Gate5GFamilyRevisionBinding(
                family_id=family_id,
                model_repo_id=f"models/{family_id}",
                model_revision=f"{index + family_index + 1:040x}",
                session_start=start_binding,
                session_end=end_binding,
            )
        )

    collection_terminals: dict[str, str] = {}
    for family_index, family_id in enumerate(FAMILIES):
        collection_terminal = base / "collection" / f"{family_index:02d}.json"
        _write(
            collection_terminal,
            {
                "artifact_class": "research",
                "family_id": family_id,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        collection_terminals[str(collection_terminal.relative_to(root))] = hash_file(
            collection_terminal
        )
    collection_terminals = dict(sorted(collection_terminals.items()))
    session_hashes = dict(sorted(session_hashes.items()))

    collection_manifest = base / "collection_manifest.json"
    if index < 12:
        plan_id = "research_collection_plan_v5:" + f"{index + 1:064x}"
        collection_payload: dict[str, Any] = {
            "schema_version": 5,
            "plan_id": plan_id,
            "plan_hash": f"{index + 101:064x}",
            "tranche_id": tranche_id,
            "pool_dialect": "gate3_algebra_operational_v1",
            "overlap_schema": "lf021_research_family_overlap_v2",
            "expansion_decision_id": f"fixture_expansion_decision:{index}",
            "expansion_decision_sha256": f"{index + 201:064x}",
            "expansion_policy_id": "fixture_expansion_policy_v1",
            "expansion_policy_sha256": f"{index + 301:064x}",
            "shared_execution_record_schema": "lf021_research_execution_records_v1",
            "actual_collection_performed": True,
            "problem_count": 1,
            "family_count": 3,
            "seed_count_by_family": seed_count_by_family,
            "expected_candidate_count": expected_invocations,
            "terminal_candidate_count": expected_invocations,
            "status_counts": {"completed": expected_invocations},
            "successful_family_count": 3,
            "terminal_artifact_hashes": collection_terminals,
            "family_session_artifact_hashes": session_hashes,
            "semantic_labels_created": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        collection_id = "research_collection_manifest_v5:" + hash_canonical(
            {
                "schema": "lf021_research_collection_manifest_v5",
                **collection_payload,
            }
        )
        collection_model: (
            collection_v5.ResearchCollectionManifestV5
            | collection_v6.PostExhaustionCollectionManifestV6
        ) = collection_v5.ResearchCollectionManifestV5.model_validate(
            {"manifest_id": collection_id, **collection_payload}
        )
    else:
        execution_config = _fixture_dependency(
            root,
            base / "dependencies" / "execution_config.json",
        )
        execution_config_id = "lf021_post_exhaustion_execution_config_v6:" + f"{index + 1:064x}"
        collection_payload = {
            "schema_version": 6,
            "execution_config_id": execution_config_id,
            "execution_config_hash": f"{index + 101:064x}",
            "execution_config": {
                "artifact": execution_config.artifact,
                "sha256": execution_config.sha256,
            },
            "authorization_id": f"fixture_authorization:{index}",
            "extension_decision_id": f"fixture_extension_decision:{index}",
            "planning_config_id": f"fixture_planning_config:{index}",
            "planning_plan_id": f"fixture_planning_plan:{index}",
            "planning_plan_hash": f"{index + 201:064x}",
            "tranche_id": tranche_id,
            "tranche_order": 12,
            "pool_id": "pool_a",
            "pool_dialect": "gate3_algebra_operational_v1",
            "shared_execution_record_schema": "lf021_research_execution_records_v1",
            "actual_collection_performed": True,
            "problem_count": 1,
            "family_count": 3,
            "seed_count_by_family": seed_count_by_family,
            "expected_candidate_count": expected_invocations,
            "terminal_candidate_count": expected_invocations,
            "status_counts": {"completed": expected_invocations},
            "successful_family_count": 3,
            "terminal_artifact_hashes": collection_terminals,
            "family_session_artifact_hashes": session_hashes,
            "semantic_labels_inspected": False,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        collection_id = "lf021_post_exhaustion_collection_manifest_v6:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_collection_manifest_v6",
                **collection_payload,
            }
        )
        collection_model = collection_v6.PostExhaustionCollectionManifestV6.model_validate(
            {"manifest_id": collection_id, **collection_payload}
        )
    _write_legacy(
        collection_manifest,
        collection_model.model_dump(mode="json"),
    )
    collection_binding = _binding(root, collection_manifest)

    postprocess_terminals: dict[str, str] = {}
    family_reports: dict[str, str] = {}
    for family_index, family_id in enumerate(FAMILIES):
        postprocess_terminal = base / "postprocess" / "terminals" / f"{family_index:02d}.json"
        _write(
            postprocess_terminal,
            {
                "artifact_class": "research",
                "family_id": family_id,
                "semantic_labels_created": False,
                "supervision_eligible": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        postprocess_terminals[str(postprocess_terminal.relative_to(root))] = hash_file(
            postprocess_terminal
        )
        family_report = base / "postprocess" / "families" / f"{family_index:02d}.json"
        _write(
            family_report,
            {
                "artifact_class": "research",
                "family_id": family_id,
                "semantic_labels_created": False,
                "supervision_eligible": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        family_reports[str(family_report.relative_to(root))] = hash_file(family_report)
    postprocess_terminals = dict(sorted(postprocess_terminals.items()))
    family_reports = dict(sorted(family_reports.items()))

    generic_dependency = _fixture_dependency(
        root,
        base / "dependencies" / "generic.json",
    )
    collection_manifest_input = postprocess_v1.PostprocessArtifactBinding(
        artifact=collection_binding.artifact,
        sha256=collection_binding.sha256,
    )
    collector_v5 = _fixture_dependency(
        root,
        root / "src/leanfaith/generation/research_collection_v5.py",
    )
    collector_v6 = _fixture_dependency(
        root,
        root / "src/leanfaith/generation/post_exhaustion_collection_v6.py",
    )
    shared_postprocess = _fixture_dependency(
        root,
        root / "src/leanfaith/generation/research_postprocess_v3.py",
    )
    problem_record_ids = (f"problem:{index:02d}",)
    raw_collection_artifacts = {invocation_id: {} for invocation_id in invocation_ids}
    parser_bindings = {
        family_id: generic_dependency.model_dump(mode="json") for family_id in FAMILIES
    }

    postprocess_manifest = base / "postprocess_manifest.json"
    common_input: dict[str, Any] = {
        "collection_plan": generic_dependency.model_dump(mode="json"),
        "collection_manifest": collection_manifest_input.model_dump(mode="json"),
        "collection_plan_id": (
            collection_payload["plan_id"] if index < 12 else collection_payload["planning_plan_id"]
        ),
        "collection_plan_hash": (
            collection_payload["plan_hash"]
            if index < 12
            else collection_payload["planning_plan_hash"]
        ),
        "collection_manifest_id": collection_id,
        "collection_terminal_artifacts": collection_terminals,
        "collection_family_session_artifacts": session_hashes,
        "raw_collection_artifacts_by_invocation": raw_collection_artifacts,
        "problem_pool_manifest": generic_dependency.model_dump(mode="json"),
        "problem_pool_records": generic_dependency.model_dump(mode="json"),
        "context": generic_dependency.model_dump(mode="json"),
        "import_header": generic_dependency.model_dump(mode="json"),
        "source_matrix": generic_dependency.model_dump(mode="json"),
        "reference_theorems": generic_dependency.model_dump(mode="json"),
        "reference_representations": generic_dependency.model_dump(mode="json"),
        "active_registry_artifacts": {
            "active_registry": generic_dependency.model_dump(mode="json")
        },
        "active_registry_content_hash": generic_dependency.sha256,
        "primary_parser_implementations": parser_bindings,
        "recovery_implementation": generic_dependency.model_dump(mode="json"),
        "shared_processing_implementation": shared_postprocess.model_dump(mode="json"),
        "implementation": generic_dependency.model_dump(mode="json"),
        "problem_count": 1,
        "family_count": 3,
        "seed_count_by_family": seed_count_by_family,
        "expected_invocations": expected_invocations,
        "problem_record_ids": problem_record_ids,
        "invocation_ids": invocation_ids,
        "family_ids": FAMILIES,
    }
    if index < 12:
        input_binding: (
            postprocess_v6.ResearchPostprocessV6InputBinding
            | postprocess_v7.PostExhaustionPostprocessInputBindingV7
        ) = postprocess_v6.ResearchPostprocessV6InputBinding.model_validate(
            {
                "schema_version": 6,
                "tranche_id": tranche_id,
                "pool_dialect": "gate3_algebra_operational_v1",
                "pool_source": "mathlib_gate3_docstrings_operational_v1",
                "pool_manifest_artifact_kind": (
                    "lf021_gate3_docstrings_operational_problem_pool_v1"
                ),
                "collection_config": generic_dependency.model_dump(mode="json"),
                "collector_implementation": collector_v5.model_dump(mode="json"),
                **common_input,
            }
        )
        postprocess_payload: dict[str, Any] = {
            "schema_version": 6,
            "input_binding": input_binding.model_dump(mode="json"),
            "input_binding_hash": input_binding.binding_hash,
            "shared_processing_input_binding_hash": (
                input_binding.shared_processing_input_binding_hash
            ),
            "tranche_id": tranche_id,
            "pool_dialect": "gate3_algebra_operational_v1",
            "pool_source": "mathlib_gate3_docstrings_operational_v1",
            "problem_count": 1,
            "family_count": 3,
            "seed_count_by_family": seed_count_by_family,
            "expected_invocations": expected_invocations,
            "terminal_invocations": expected_invocations,
            "status_counts": {"admitted_unresolved": expected_invocations},
            "recovery_status_counts": {"not_needed": expected_invocations},
            "terminal_artifacts": postprocess_terminals,
            "family_report_artifacts": family_reports,
            "admitted_pair_count": expected_invocations,
            "admitted_nl_lean_count": expected_invocations,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        postprocess_id = "research_postprocess_v6_manifest:" + hash_canonical(
            {
                "schema": "lf021_research_postprocess_manifest_v6",
                **postprocess_payload,
            }
        )
        postprocess_model: (
            postprocess_v6.ResearchPostprocessV6Manifest
            | postprocess_v7.PostExhaustionPostprocessManifestV7
        ) = postprocess_v6.ResearchPostprocessV6Manifest.model_validate(
            {"manifest_id": postprocess_id, **postprocess_payload}
        )
    else:
        input_binding = postprocess_v7.PostExhaustionPostprocessInputBindingV7.model_validate(
            {
                "schema_version": 7,
                "tranche_id": tranche_id,
                "tranche_order": 12,
                "pool_id": "pool_a",
                "pool_dialect": "gate3_algebra_operational_v1",
                "pool_source": "mathlib_gate3_docstrings_operational_v1",
                "pool_manifest_artifact_kind": (
                    "lf021_gate3_docstrings_operational_problem_pool_v1"
                ),
                "extension_authorization": generic_dependency.model_dump(mode="json"),
                "extension_authorization_id": collection_payload["authorization_id"],
                "extension_decision": generic_dependency.model_dump(mode="json"),
                "extension_decision_id": collection_payload["extension_decision_id"],
                "planning_config": generic_dependency.model_dump(mode="json"),
                "planning_config_id": collection_payload["planning_config_id"],
                "execution_config": collection_payload["execution_config"],
                "execution_config_id": collection_payload["execution_config_id"],
                "execution_config_hash": collection_payload["execution_config_hash"],
                "collector_implementation": collector_v6.model_dump(mode="json"),
                **common_input,
            }
        )
        postprocess_payload = {
            "schema_version": 7,
            "input_binding": input_binding.model_dump(mode="json"),
            "input_binding_hash": input_binding.binding_hash,
            "shared_processing_input_binding_hash": (
                input_binding.shared_processing_input_binding_hash
            ),
            "tranche_id": tranche_id,
            "tranche_order": 12,
            "pool_id": "pool_a",
            "pool_dialect": "gate3_algebra_operational_v1",
            "pool_source": "mathlib_gate3_docstrings_operational_v1",
            "problem_count": 1,
            "family_count": 3,
            "seed_count_by_family": seed_count_by_family,
            "expected_invocations": expected_invocations,
            "terminal_invocations": expected_invocations,
            "status_counts": {"admitted_unresolved": expected_invocations},
            "recovery_status_counts": {"not_needed": expected_invocations},
            "terminal_artifacts": postprocess_terminals,
            "family_report_artifacts": family_reports,
            "admitted_pair_count": expected_invocations,
            "admitted_nl_lean_count": expected_invocations,
            "semantic_labels_inspected": False,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        postprocess_id = "research_postprocess_v7_manifest:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_postprocess_manifest_v7",
                **postprocess_payload,
            }
        )
        postprocess_model = postprocess_v7.PostExhaustionPostprocessManifestV7.model_validate(
            {"manifest_id": postprocess_id, **postprocess_payload}
        )
    _write_legacy(
        postprocess_manifest,
        postprocess_model.model_dump(mode="json"),
    )
    postprocess_binding = _binding(root, postprocess_manifest)

    overlap_parts: dict[str, Gate5GArtifactBinding] = {}
    for family_id in FAMILIES:
        part = base / "overlap" / f"{family_id}.json"
        _write(
            part,
            {
                "family_id": family_id,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        overlap_parts[family_id] = _binding(root, part)
    overlap = base / "overlap_manifest.json"
    _write(
        overlap,
        {
            "family_count": 3,
            "family_artifacts": {
                key: value.model_dump(mode="json") for key, value in sorted(overlap_parts.items())
            },
            "semantic_labels_created": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        },
    )

    _, _, collection_replay = subject.seal_gate5g_replay_certificate_v1(
        repo_root=root,
        manifest=collection_binding,
        tranche_id=tranche_id,
        kind="collection",
        expected_record_count=expected_invocations,
        output_root=output_root,
    )
    _, _, postprocess_replay = subject.seal_gate5g_replay_certificate_v1(
        repo_root=root,
        manifest=postprocess_binding,
        tranche_id=tranche_id,
        kind="postprocess",
        expected_record_count=expected_invocations,
        output_root=output_root,
    )
    return Gate5GTrancheBindingV1(
        tranche_id=tranche_id,
        collection_manifest=collection_binding,
        postprocess_manifest=Gate5GObservationBinding(
            artifact=postprocess_binding.artifact,
            sha256=postprocess_binding.sha256,
            manifest_id=postprocess_id,
            tranche_id=tranche_id,
        ),
        collection_replay=collection_replay,
        postprocess_replay=postprocess_replay,
        family_ids=FAMILIES,
        family_revisions=tuple(revisions),
        overlap_manifest=_binding(root, overlap),
        pool_ids=("pool_a",),
        source_proxies=("public/source",),
        expected_invocations=expected_invocations,
        collection_terminal_count=expected_invocations,
        postprocess_terminal_count=expected_invocations,
        benchmark_clear_compiling_count=expected_invocations,
    )


def test_mixed_12_plus_extension_lineage_round_trips_real_gate_verifier(
    tmp_path: Path,
) -> None:
    output = tmp_path / "reports/lineage"
    tranches = tuple(
        _make_tranche(root=tmp_path, output_root=output, index=index) for index in range(13)
    )
    observations = tuple(
        SimpleNamespace(
            tranche_id=item.tranche_id,
            manifest_id=item.postprocess_manifest.manifest_id,
            postprocess_manifest=item.postprocess_manifest,
        )
        for item in tranches
    )
    decision = SimpleNamespace(observations=observations)
    lineage, path = subject.publish_mixed_gate5g_lineage_v1(
        repo_root=tmp_path,
        decision=decision,
        tranches=tranches,
        required_families=FAMILIES,
        output_root=output,
    )
    assert len(lineage.tranches) == 13
    assert lineage.tranches[-1].tranche_id == "extension_s0"
    assert path.read_bytes() == canonical_json_bytes(lineage.model_dump(mode="json"))
    assert subject.publish_mixed_gate5g_lineage_v1(
        repo_root=tmp_path,
        decision=decision,
        tranches=tranches,
        required_families=FAMILIES,
        output_root=output,
    ) == (lineage, path)
    verified_lineage, verified_binding = extended_gate5g._verify_complete_lineage(
        paths=RepoPaths(tmp_path),
        lineage_manifest_path=path,
        verified=SimpleNamespace(decision=decision),
        required_families=FAMILIES,
    )
    assert verified_lineage == lineage
    assert verified_binding == _binding(tmp_path, path)


def test_extended_gate_policy_pins_exact_replay_implementation() -> None:
    loaded = extended_gate5g.load_extended_gate5g_policy(
        ROOT / "configs/generation/lf021_gate5g_finalizer_v2.yaml"
    )
    extended_gate5g._verify_policy_lineage(
        paths=RepoPaths(ROOT),
        policy=loaded.config,
    )
    assert (
        loaded.config.lineage_builder_implementation.artifact
        == "src/leanfaith/generation/post_exhaustion_gate5g_lineage_v1.py"
    )


def test_spec_is_content_addressed_and_requires_executing_builder() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "report_kind": "lf021_post_exhaustion_gate5g_lineage_spec_v1",
        "builder_implementation": {
            "artifact": "src/leanfaith/generation/post_exhaustion_gate5g_lineage_v1.py",
            "sha256": hash_file(
                ROOT / "src/leanfaith/generation/post_exhaustion_gate5g_lineage_v1.py"
            ),
        },
        "frame_policy": {
            "artifact": "configs/generation/lf021_post_exhaustion_frame_v1.yaml",
            "sha256": "1" * 64,
        },
        "frame_decision": {
            "artifact": "reports/decision.json",
            "sha256": "2" * 64,
        },
        "pool_overlaps": [
            {
                "pool_id": "pool_a",
                "overlap_manifest": {
                    "artifact": "reports/overlap.json",
                    "sha256": "3" * 64,
                },
            }
        ],
        "required_original_observation_count": 12,
        "minimum_extension_observation_count": 1,
        "maximum_extension_observation_count": 4,
        "required_family_ids": FAMILIES,
        "production_only": True,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    spec_id = "lf021_post_exhaustion_gate5g_lineage_spec_v1:" + hash_canonical(
        {"schema": "lf021_post_exhaustion_gate5g_lineage_spec_v1", **payload}
    )
    spec = subject.PostExhaustionGate5GLineageSpecV1.model_validate({"spec_id": spec_id, **payload})
    assert spec.spec_id == spec_id
    with pytest.raises(ValueError, match="ID differs"):
        subject.PostExhaustionGate5GLineageSpecV1.model_validate(
            {"spec_id": "lf021_post_exhaustion_gate5g_lineage_spec_v1:" + "f" * 64, **payload}
        )


def _canonical_spec_fixture(
    *,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_replay_only: bool = False,
) -> Path:
    builder = root / "src/leanfaith/generation/post_exhaustion_gate5g_lineage_v1.py"
    builder.parent.mkdir(parents=True)
    shutil.copy2(Path(subject.__file__).resolve(), builder)
    monkeypatch.setattr(subject, "__file__", str(builder))

    policy = root / "configs/generation/lf021_post_exhaustion_frame_v1.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text("schema_version: 1\n", encoding="utf-8")
    decision_path = root / "reports/frame/decision.json"
    _write(decision_path, {"frame": "synthetic"})

    for pool_id, relative in subject._CANONICAL_POOL_OVERLAPS:
        family_bindings: dict[str, Gate5GArtifactBinding] = {}
        for family_id in FAMILIES:
            family_path = root / "reports/overlap_parts" / pool_id / f"{family_id}.json"
            _write(
                family_path,
                {
                    "family_id": family_id,
                    "semantic_labels_created": False,
                    "gate_5g_credit_claimed": False,
                    "gate_5_closed": False,
                },
            )
            family_bindings[family_id] = _binding(root, family_path)
        overlap_path = root / relative
        _write(
            overlap_path,
            {
                "family_artifacts": {
                    key: value.model_dump(mode="json")
                    for key, value in sorted(family_bindings.items())
                },
                "family_count": 3,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        # Checked-in overlap bundle manifests are strict JSON with a terminal
        # newline; only the newly published lineage spec is canonical-byte exact.
        overlap_path.write_bytes(overlap_path.read_bytes() + b"\n")

    decision = SimpleNamespace(
        test_replay_only=test_replay_only,
        frame=SimpleNamespace(test_replay_only=test_replay_only, item_count=240),
        source_stop_action="preferred_eligible_stop",
        action="freeze_preferred_frame",
        next_tranche=None,
        coverage_deficits=(),
        original_observation_count=12,
        extension_observation_count=1,
        observations=tuple(object() for _ in range(13)),
    )
    monkeypatch.setattr(
        subject.frame_v1,
        "verify_extended_frame_freeze_v1",
        lambda **_: SimpleNamespace(
            decision=decision,
            decision_path=decision_path,
        ),
    )
    return decision_path


def test_canonical_spec_factory_is_exact_idempotent_and_loadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    decision = _canonical_spec_fixture(root=tmp_path, monkeypatch=monkeypatch)
    first = subject.publish_post_exhaustion_gate5g_lineage_spec_v1(
        repo_root=tmp_path,
        frame_decision_path=decision,
    )
    second = subject.publish_post_exhaustion_gate5g_lineage_spec_v1(
        repo_root=tmp_path,
        frame_decision_path=decision,
    )
    assert second == first
    assert first.spec_path.parent == (
        tmp_path / "reports/generation/lf021_post_exhaustion_gate5g_lineage_specs_v1"
    )
    expected = canonical_json_bytes(first.spec.model_dump(mode="json"))
    assert first.spec_path.read_bytes() == expected
    assert not expected.endswith(b"\n")
    assert first.spec_binding.sha256 == hash_file(first.spec_path)
    assert (
        subject.load_post_exhaustion_gate5g_lineage_spec_v1(
            repo_root=tmp_path,
            spec_path=first.spec_path,
        )
        == first.spec
    )
    assert tuple(item.pool_id for item in first.spec.pool_overlaps) == (
        "algebra_gate3_docstrings_v1",
        "cross_domain_docstrings_v1",
    )


def test_canonical_spec_factory_rejects_test_entropy_without_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    decision = _canonical_spec_fixture(
        root=tmp_path,
        monkeypatch=monkeypatch,
        test_replay_only=True,
    )
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match="production preferred frame",
    ):
        subject.publish_post_exhaustion_gate5g_lineage_spec_v1(
            repo_root=tmp_path,
            frame_decision_path=decision,
        )
    assert not (
        tmp_path / "reports/generation/lf021_post_exhaustion_gate5g_lineage_specs_v1"
    ).exists()


def test_canonical_spec_factory_rejects_symlinked_publication_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    decision = _canonical_spec_fixture(root=tmp_path, monkeypatch=monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    namespace = tmp_path / "reports/generation/lf021_post_exhaustion_gate5g_lineage_specs_v1"
    namespace.parent.mkdir(parents=True, exist_ok=True)
    namespace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        subject.PostExhaustionGate5GLineageError,
        match=r"symlink|trusted",
    ):
        subject.publish_post_exhaustion_gate5g_lineage_spec_v1(
            repo_root=tmp_path,
            frame_decision_path=decision,
        )
    assert tuple(outside.iterdir()) == ()


def test_prepare_spec_cli_forbids_output_override(tmp_path: Path) -> None:
    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts/35_build_post_exhaustion_gate5g_lineage_v1.py"),
            "--root",
            str(ROOT),
            "--prepare-spec",
            "--frame-decision",
            "missing.json",
            "--output-root",
            "forbidden",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--prepare-spec forbids --spec and --output-root" in result.stderr
    assert not (tmp_path / "reports").exists()
