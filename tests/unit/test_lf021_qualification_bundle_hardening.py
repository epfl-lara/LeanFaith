"""Adversarial checks for the canonical LF-021 local qualification bundle."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

import leanfaith.generation.local_qualification as qualification
from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.generation.candidate_screening import PriorCandidateIdentity
from leanfaith.generation.local_qualification import (
    ArchivedQualificationInput,
    LocalCheckpointVerification,
    LocalQualificationBundleManifest,
    LocalQualificationReplayError,
    LocalQualificationRunResult,
    QualificationCodeBundleBinding,
    QualificationInputBinding,
    SmokeAdmissionDryRunReceipt,
    load_local_qualification_config,
    persist_local_qualification_bundle,
    run_local_kimina_qualification,
    verify_local_qualification_bundle,
)
from leanfaith.generation.real_outputs import (
    RealOutputCandidateOutcome,
    RealOutputOutcomeCode,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.schemas.llm import LLMAttemptRecord, LLMCallRecord
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantRecord
from tests.unit.test_generation_local_qualification import (
    CONFIG,
    FENCED,
    HEADER,
    NAME,
    ROOT,
    UTC,
    FakeLeanBackend,
    FakeLocalRuntime,
    _checkpoint_verification,
    _context,
    _fixture_preflight,
    _patch_representation,
    _problem,
    _reference,
    _runtime_binding,
    _screening_index,
    _screening_inputs,
)

_CODE_TREE_HASH = "a" * 64
_CODE_BUNDLE_SOURCE = "tests/fixtures/lf021_synthetic_code_bundle.tar.gz"


@dataclass(frozen=True, slots=True)
class HardenedBundle:
    result: LocalQualificationRunResult
    manifest: LocalQualificationBundleManifest
    problem: ProblemPoolRecord
    run_directory: Path
    artifact_root: Path


def _write_code_bundle(path: Path, *, code_tree_hash: str = _CODE_TREE_HASH) -> str:
    source = b"value = 1\n"
    source_hash = hashlib.sha256(source).hexdigest()
    manifest = {
        "schema_version": 1,
        "code_state": {
            "git_revision": "1" * 40,
            "git_dirty": False,
            "base_git_commit": "1" * 40,
            "code_tree_hash": code_tree_hash,
            "tracked_diff_hash": "2" * 64,
            "untracked_files": [],
        },
        "files": [
            {
                "path": "source.py",
                "sha256": source_hash,
                "mode": 0o644,
            }
        ],
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        manifest_info = tarfile.TarInfo("CODE_BUNDLE_MANIFEST.json")
        manifest_info.size = len(manifest_bytes)
        manifest_info.mode = 0o644
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        source_info = tarfile.TarInfo("source.py")
        source_info.size = len(source)
        source_info.mode = 0o644
        archive.addfile(source_info, io.BytesIO(source))
    return hash_file(path)


def _build_hardened_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> HardenedBundle:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    problem = _problem(reference)
    screening_index = _screening_index()
    screening_inputs = _screening_inputs(tmp_path)
    bundle_path = tmp_path / "qualification-code-bundle.tar.gz"
    bundle_hash = _write_code_bundle(bundle_path)
    code_binding = QualificationCodeBundleBinding(
        source_artifact=_CODE_BUNDLE_SOURCE,
        sha256=bundle_hash,
        code_tree_hash=_CODE_TREE_HASH,
    )

    original_safe_input = qualification._safe_regular_input

    def safe_input(repo_root: Path, value: str, *, label: str) -> Path:
        if value == _CODE_BUNDLE_SOURCE:
            return bundle_path
        return original_safe_input(repo_root, value, label=label)

    monkeypatch.setattr(qualification, "_safe_regular_input", safe_input)
    run_directory = tmp_path / "run"
    result = run_local_kimina_qualification(
        loaded_config=loaded,
        runtime_binding=_runtime_binding(),
        runtime=FakeLocalRuntime(FENCED),
        problem=problem,
        expected_declaration_name=NAME,
        context=_context(),
        references=(reference,),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, FakeLeanBackend()),
        screening_index=screening_index,
        artifact_root=tmp_path,
        run_directory=run_directory,
        created_at=UTC,
        fixture_artifact="examples/lf021_offline_smoke_v1.json",
        fixture_preflight=_fixture_preflight(problem=problem, reference=reference),
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=code_binding,
        screening_inputs=screening_inputs,
    )
    manifest = persist_local_qualification_bundle(
        result,
        run_directory=run_directory,
        artifact_root=tmp_path,
    )
    return HardenedBundle(result, manifest, problem, run_directory, tmp_path)


def _record(case: HardenedBundle, name: str, model_type: type[object]) -> object:
    path = case.artifact_root / case.manifest.artifacts[name]
    return model_type.model_validate_json(  # type: ignore[attr-defined]
        path.read_text(encoding="utf-8")
    )


def _manifest_v2(
    *,
    terminal_id: str,
    artifacts: dict[str, str],
    hashes: dict[str, str],
) -> LocalQualificationBundleManifest:
    payload = {
        "schema": "lf021_local_qualification_bundle_v2",
        "terminal_id": terminal_id,
        "artifact_class": "smoke",
        "qualifies_for_gate5g": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "release_eligible": False,
        "calibration_eligible": False,
        "model_selection_eligible": False,
        "scientific_evaluation_eligible": False,
        "artifacts": artifacts,
        "artifact_sha256": hashes,
    }
    return LocalQualificationBundleManifest(
        bundle_id="local_qualification_bundle:" + hash_canonical(payload),
        terminal_id=terminal_id,
        artifacts=artifacts,
        artifact_sha256=hashes,
    )


def _rewrite_archived_input(
    case: HardenedBundle,
    *,
    role: str,
    payload: bytes,
    input_updates: dict[str, object] | None = None,
) -> LocalQualificationBundleManifest:
    inputs_path = case.artifact_root / case.manifest.artifacts["qualification_inputs"]
    inputs = QualificationInputBinding.model_validate_json(inputs_path.read_text(encoding="utf-8"))
    digest = sha256_hex(payload)
    archive_path = case.run_directory / "qualification_inputs" / "sha256" / digest
    archive_path.write_bytes(payload)
    relative = str(archive_path.relative_to(case.artifact_root))
    archived: list[ArchivedQualificationInput] = []
    found = False
    for item in inputs.archived_inputs:
        if item.role != role:
            archived.append(item)
            continue
        found = True
        archived.append(
            item.model_copy(
                update={
                    "archive_artifact": relative,
                    "sha256": digest,
                    "byte_count": len(payload),
                }
            )
        )
    assert found, role
    document = inputs.model_dump(mode="json")
    document["archived_inputs"] = [item.model_dump(mode="json") for item in archived]
    document.update(input_updates or {})
    rewritten = QualificationInputBinding.model_validate(document)
    rewritten_bytes = canonical_json_bytes(rewritten.model_dump(mode="json")) + b"\n"
    inputs_path.write_bytes(rewritten_bytes)

    artifacts = dict(case.manifest.artifacts)
    hashes = dict(case.manifest.artifact_sha256)
    artifacts[f"input_{role}"] = relative
    hashes[f"input_{role}"] = digest
    hashes["qualification_inputs"] = sha256_hex(rewritten_bytes)
    return _manifest_v2(
        terminal_id=case.manifest.terminal_id,
        artifacts=artifacts,
        hashes=hashes,
    )


def test_canonical_bundle_binds_typed_non_admitting_receipt_and_all_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    result = case.result
    terminal = result.terminal
    receipt = result.admission_receipt

    assert isinstance(receipt, SmokeAdmissionDryRunReceipt)
    assert result.materialized is not None
    assert result.admitted is not None
    assert result.screening is not None
    assert terminal.admission_receipt_id == receipt.receipt_id
    assert terminal.materialization_outcome_id == result.materialized.outcome.outcome_id
    assert receipt.pending_outcome_id == result.materialized.outcome.outcome_id
    assert receipt.dry_run_admitted_outcome_id == result.admitted.outcome.outcome_id
    assert (
        result.materialized.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
    )
    assert result.materialized.outcome.semantic_pool_eligible is False
    assert result.materialized.outcome.pair_ids == ()
    assert result.materialized.outcome.nl_lean_id is None
    assert result.admitted.outcome.semantic_pool_eligible is True
    assert terminal.admitted_pair_ids == ()
    assert terminal.admitted_nl_lean_id is None

    expected_record_names = {
        "admission_receipt",
        "attempt",
        "call",
        "materialization_outcome",
        "qualification_inputs",
        "representation",
        "screening",
        "terminal",
        "theorem",
        "variant",
    }
    expected_input_roles = {
        "benchmark_active_registry",
        "benchmark_detailed_index",
        "benchmark_input_manifest",
        "benchmark_registry_manifest",
        "checkpoint_verification",
        "code_bundle",
        "common_suffix",
        "environment_lock",
        "execution_input",
        "fixture_preflight",
        "fixture_source",
        "import_header",
        "parser_source",
        "prior_candidate_index",
        "prompt_template",
        "qualification_config",
        "runtime_adapter",
    }
    assert set(case.manifest.artifacts) == expected_record_names | {
        f"input_{role}" for role in expected_input_roles
    }
    assert set(case.manifest.artifacts) == set(case.manifest.artifact_sha256)
    for name, artifact in case.manifest.artifacts.items():
        path = case.artifact_root / artifact
        assert path.is_file(), name
        assert not path.is_symlink(), name
        assert hash_file(path) == case.manifest.artifact_sha256[name], name

    outcome = cast(
        RealOutputCandidateOutcome,
        _record(case, "materialization_outcome", RealOutputCandidateOutcome),
    )
    persisted_receipt = cast(
        SmokeAdmissionDryRunReceipt,
        _record(case, "admission_receipt", SmokeAdmissionDryRunReceipt),
    )
    theorem = cast(TheoremRecord, _record(case, "theorem", TheoremRecord))
    representation = cast(
        RepresentationRecord,
        _record(case, "representation", RepresentationRecord),
    )
    variant = cast(VariantRecord, _record(case, "variant", VariantRecord))
    call = cast(LLMCallRecord, _record(case, "call", LLMCallRecord))
    attempt = cast(LLMAttemptRecord, _record(case, "attempt", LLMAttemptRecord))
    assert persisted_receipt == receipt
    assert outcome.outcome_id == receipt.pending_outcome_id
    assert theorem.theorem_id == terminal.candidate_theorem_id
    assert representation.representation_id == terminal.representation_id
    assert representation.theorem_id == theorem.theorem_id
    assert variant.derived_theorem_id == theorem.theorem_id
    assert variant.derived_representation_id == representation.representation_id
    assert call.call_id == terminal.llm_call_id
    assert attempt.attempt_id == terminal.llm_attempt_id

    replay_terminal, replay_call, replay_attempt = verify_local_qualification_bundle(
        case.manifest,
        artifact_root=case.artifact_root,
        repo_root=case.artifact_root / "working-tree-must-not-be-read",
        problem=case.problem,
    )
    assert replay_terminal == terminal
    assert replay_call == result.lineage.call
    assert replay_attempt == result.lineage.attempt


def test_canonical_smoke_bundle_is_hard_false_for_every_scientific_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    manifest = case.manifest
    receipt = case.result.admission_receipt
    assert receipt is not None

    for field in (
        "qualifies_for_gate5g",
        "semantic_labels_created",
        "supervision_eligible",
        "training_eligible",
        "release_eligible",
        "calibration_eligible",
        "model_selection_eligible",
        "scientific_evaluation_eligible",
    ):
        assert getattr(manifest, field) is False
    for field in (
        "qualifies_for_gate5g",
        "semantic_labels_created",
        "supervision_eligible",
        "training_eligible",
        "semantic_pool_eligible",
        "persistence_allowed",
    ):
        assert getattr(receipt, field) is False
    assert case.result.terminal.qualifies_for_gate5g is False
    assert case.result.terminal.semantic_labels_created is False
    assert case.result.terminal.supervision_eligible is False

    forbidden = ("pair", "nl_lean", "nllean", "label", "resolved_label")
    record_names = {name for name in manifest.artifacts if not name.startswith("input_")}
    assert all(
        not (
            name in forbidden
            or name.startswith("pair_")
            or name.startswith("nl_lean_")
            or name.startswith("nllean_")
            or name.startswith("label_")
            or name.startswith("resolved_label_")
        )
        for name in record_names
    )
    assert not any(
        path.name.startswith(("pair", "nl_lean", "nllean", "label"))
        for path in case.run_directory.glob("*.json")
    )

    for name in record_names:
        json.loads((case.artifact_root / manifest.artifacts[name]).read_text(encoding="utf-8"))


def test_replay_rejects_semantically_tampered_checkpoint_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    inputs_path = case.artifact_root / case.manifest.artifacts["qualification_inputs"]
    inputs = QualificationInputBinding.model_validate_json(inputs_path.read_text(encoding="utf-8"))
    checkpoint = inputs.checkpoint_verification
    assert checkpoint is not None
    files = list(checkpoint.files)
    files[0] = files[0].model_copy(update={"sha256": "f" * 64})
    tampered = LocalCheckpointVerification(
        model_repo_id=checkpoint.model_repo_id,
        model_revision=checkpoint.model_revision,
        snapshot_reference=checkpoint.snapshot_reference,
        files=tuple(files),
        checkpoint_bytes=checkpoint.checkpoint_bytes,
    )
    payload = canonical_json_bytes(tampered.model_dump(mode="json")) + b"\n"
    manifest = _rewrite_archived_input(
        case,
        role="checkpoint_verification",
        payload=payload,
        input_updates={"checkpoint_verification": tampered.model_dump(mode="json")},
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match=r"checkpoint verification.*active model|checkpoint.*config",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=case.artifact_root,
            repo_root=case.artifact_root / "absent",
            problem=case.problem,
        )


def test_replay_rejects_code_bundle_with_different_embedded_tree_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    inputs_path = case.artifact_root / case.manifest.artifacts["qualification_inputs"]
    inputs = QualificationInputBinding.model_validate_json(inputs_path.read_text(encoding="utf-8"))
    binding = inputs.code_bundle
    assert binding is not None
    wrong_bundle = tmp_path / "wrong-tree.tar.gz"
    wrong_hash = _write_code_bundle(wrong_bundle, code_tree_hash="b" * 64)
    manifest = _rewrite_archived_input(
        case,
        role="code_bundle",
        payload=wrong_bundle.read_bytes(),
        input_updates={
            "code_bundle": {
                **binding.model_dump(mode="json"),
                "sha256": wrong_hash,
            }
        },
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match="archived qualification code bundle is invalid",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=case.artifact_root,
            repo_root=case.artifact_root / "absent",
            problem=case.problem,
        )


def test_replay_recomputes_admission_instead_of_trusting_rehashed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    receipt = case.result.admission_receipt
    assert receipt is not None
    tampered_pair_ids = tuple(sorted({*receipt.dry_run_pair_ids, "pair:" + "f" * 64}))
    receipt_payload = receipt.id_payload()
    receipt_payload["dry_run_pair_ids"] = tampered_pair_ids
    tampered_receipt = SmokeAdmissionDryRunReceipt.model_validate(
        {
            **receipt.model_dump(mode="json"),
            "receipt_id": qualification._smoke_admission_receipt_id(receipt_payload),
            "dry_run_pair_ids": tampered_pair_ids,
        }
    )

    terminal = case.result.terminal
    terminal_payload = terminal.id_payload()
    terminal_payload["admission_receipt_id"] = tampered_receipt.receipt_id
    tampered_terminal = type(terminal).model_validate(
        {
            **terminal.model_dump(mode="json"),
            "terminal_id": qualification._terminal_id(terminal_payload),
            "admission_receipt_id": tampered_receipt.receipt_id,
        }
    )

    artifacts = dict(case.manifest.artifacts)
    hashes = dict(case.manifest.artifact_sha256)
    for name, record in (
        ("admission_receipt", tampered_receipt),
        ("terminal", tampered_terminal),
    ):
        payload = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
        (case.artifact_root / artifacts[name]).write_bytes(payload)
        hashes[name] = sha256_hex(payload)
    manifest = _manifest_v2(
        terminal_id=tampered_terminal.terminal_id,
        artifacts=artifacts,
        hashes=hashes,
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match="smoke admission receipt differs from fail-closed recomputation",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=case.artifact_root,
            repo_root=case.artifact_root / "absent",
            problem=case.problem,
        )


def test_replay_rejects_fully_rehashed_terminal_admission_timestamp_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    terminal = case.result.terminal
    assert terminal.admission_at is not None
    tampered_terminal = type(terminal).model_validate(
        {
            **terminal.model_dump(mode="json"),
            "admission_at": terminal.admission_at + datetime.timedelta(seconds=1),
        }
    )
    terminal_payload = canonical_json_bytes(tampered_terminal.model_dump(mode="json")) + b"\n"
    terminal_path = case.artifact_root / case.manifest.artifacts["terminal"]
    terminal_path.write_bytes(terminal_payload)
    hashes = {
        **case.manifest.artifact_sha256,
        "terminal": sha256_hex(terminal_payload),
    }
    manifest = _manifest_v2(
        terminal_id=tampered_terminal.terminal_id,
        artifacts=dict(case.manifest.artifacts),
        hashes=hashes,
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match="smoke admission receipt differs from pending materialization lineage",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=case.artifact_root,
            repo_root=case.artifact_root / "absent",
            problem=case.problem,
        )


def test_replay_rejects_fully_rehashed_execution_declaration_name_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    inputs = case.result.input_binding
    archived = {item.role: item for item in inputs.archived_inputs}
    execution_path = case.artifact_root / archived["execution_input"].archive_artifact
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    execution["expected_declaration_name"] = "different_declaration"
    payload = canonical_json_bytes(execution) + b"\n"
    manifest = _rewrite_archived_input(
        case,
        role="execution_input",
        payload=payload,
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match="archived execution context/header/declaration binding is inconsistent",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=case.artifact_root,
            repo_root=case.artifact_root / "absent",
            problem=case.problem,
        )


def test_replay_recomputes_prior_candidate_screening_instead_of_trusting_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_hardened_bundle(tmp_path, monkeypatch)
    representation = cast(
        RepresentationRecord,
        _record(case, "representation", RepresentationRecord),
    )
    assert representation.alpha_identity_fingerprint is not None
    prior = PriorCandidateIdentity(
        theorem_id="thm:" + "0" * 64,
        alpha_identity_fingerprint=representation.alpha_identity_fingerprint,
    )
    registry_hash = case.result.input_binding.screening_registry_hash
    assert registry_hash is not None
    payload = (
        canonical_json_bytes(
            {
                "schema": "lf021_prior_candidate_index_v1",
                "active_registry_hash": registry_hash,
                "prior_candidates": [prior.model_dump(mode="json")],
            }
        )
        + b"\n"
    )
    manifest = _rewrite_archived_input(
        case,
        role="prior_candidate_index",
        payload=payload,
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match="persisted candidate screening differs from fail-closed recomputation",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=case.artifact_root,
            repo_root=case.artifact_root / "absent",
            problem=case.problem,
        )
