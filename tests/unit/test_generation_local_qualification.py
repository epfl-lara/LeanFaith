"""One-model LF-021 qualification with an injectable no-download runtime."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

import leanfaith.generation.local_qualification as local_qualification
import leanfaith.generation.real_outputs as real_outputs
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.datasets.benchmark_signatures import (
    BENCHMARK_SIGNATURE_SCHEMA_VERSION,
    BENCHMARK_SIGNATURE_SELECTION_VERSION,
    BenchmarkSide,
    BenchmarkSignatureRecord,
    BenchmarkSignatureWorkManifest,
    BenchmarkViewStatus,
    build_benchmark_signature_artifact,
)
from leanfaith.datasets.denylist import (
    DenylistIndex,
    FrozenBenchmark,
    FrozenRegistry,
    RepresentationSignatureManifest,
)
from leanfaith.generation.candidate_screening import CandidateScreeningIndex
from leanfaith.generation.local_hf import (
    LocalHFGenerationRequest,
    LocalHFGenerationResult,
    LocalHFProviderCompatibility,
)
from leanfaith.generation.local_qualification import (
    LocalCheckpointFile,
    LocalCheckpointVerification,
    LocalQualificationBundleManifest,
    LocalQualificationFixturePreflight,
    LocalQualificationReplayError,
    QualificationCodeBundleBinding,
    QualificationScreeningInputFiles,
    QualificationStatus,
    RuntimeEnvironmentBinding,
    load_local_qualification_config,
    make_runtime_binding,
    persist_local_qualification_bundle,
    run_local_kimina_qualification,
    verify_local_qualification_bundle,
)
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.lean.extraction import PLACEHOLDER
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.pipeline import RepresentationBatch, RepresentationBatchResult
from leanfaith.schemas.enums import NLTrust, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import ANCESTRY_PREFIX, REPRESENTATION_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.nl_lean import ProblemPoolRecord, make_problem_record_id
from leanfaith.schemas.theorem import (
    CANONICAL_VIEW_NAMES,
    ContextRecord,
    RepresentationRecord,
    TheoremRecord,
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG = ROOT / "configs" / "generation" / "local_qualification_v1.yaml"
CONFIG_V2 = ROOT / "configs" / "generation" / "local_qualification_kimina_v2.yaml"
UTC = datetime.datetime(2026, 7, 23, 22, 0, tzinfo=datetime.UTC)
HEADER_ARTIFACT = "examples/lf021_offline_smoke_header_v1.lean"
FIXTURE_ARTIFACT = "examples/lf021_offline_smoke_v1.json"
HEADER = (ROOT / HEADER_ARTIFACT).read_text(encoding="utf-8")
CTX_FP = "0" * 64
CTX_ID = f"ctx:{CTX_FP}"
NAME = "lf021_kimina_identity"
STATEMENT = f"theorem {NAME} (n : Nat) : n = n"
FENCED = f"Check the claim.\n```lean4\n{STATEMENT} := by sorry\n```\n"
_CODE_TREE_HASH = "a" * 64
_CODE_BUNDLE_SOURCE = "tests/fixtures/lf021_synthetic_code_bundle.tar.gz"


class FakeLocalRuntime:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list[LocalHFGenerationRequest] = []

    def generate(self, request: LocalHFGenerationRequest) -> LocalHFGenerationResult:
        self.requests.append(request)
        output_hash = sha256_hex(self.output.encode("utf-8"))
        formatted_hash = sha256_hex(("<chat>" + request.prompt).encode("utf-8"))
        compatibility = LocalHFProviderCompatibility(
            model=request.pin.repo_id,
            revision=request.pin.revision,
            remote_code_authorized=False,
            private_source_content=False,
            execution_purpose=request.execution_purpose,
            output_hash=output_hash,
            formatted_prompt_hash=formatted_hash,
            prompt_formatter_id=request.prompt_formatter_id,
            decoding_hash=request.decoding.decoding_hash,
        )
        return LocalHFGenerationResult(
            request_hash=request.request_hash,
            formatted_prompt_hash=formatted_hash,
            raw_text=self.output,
            output_hash=output_hash,
            prompt_tokens=31,
            output_tokens=19,
            total_tokens=50,
            load_latency_ms=10,
            generation_latency_ms=20,
            unload_latency_ms=5,
            total_latency_ms=35,
            decoding=request.decoding,
            decoding_hash=request.decoding.decoding_hash,
            compatibility=compatibility,
        )


def _offset_position(source: str, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    return prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1])


class FakeLeanBackend:
    def __init__(self) -> None:
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        assert request.code is not None
        source = request.code
        start = source.index(f"theorem {NAME}")
        placeholder = source.index(" := by sorry", start)
        finish = len(source)
        decl_line, decl_column = _offset_position(source, start)
        finish_line, finish_column = _offset_position(source, finish)
        sig_start = start + len(f"theorem {NAME} ")
        sig_line, sig_column = _offset_position(source, sig_start)
        sig_finish_line, sig_finish_column = _offset_position(source, placeholder)
        declaration = {
            "name": NAME,
            "full_name": NAME,
            "kind": "theorem",
            "range": {
                "start": {"line": decl_line, "column": decl_column},
                "finish": {"line": finish_line, "column": finish_column},
            },
            "signature": {
                "pp": "(n : Nat) : n = n",
                "range": {
                    "start": {"line": sig_line, "column": sig_column},
                    "finish": {
                        "line": sig_finish_line,
                        "column": sig_finish_column,
                    },
                },
            },
            "type": {"pp": "n = n"},
        }
        return LeanResult(
            request_id=request.request_id,
            request_hash=sha256_hex(source.encode("utf-8")),
            context_id=request.context_id,
            context_fingerprint=CTX_FP,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=(declaration,),
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _context() -> ContextRecord:
    return ContextRecord(
        environment_schema_version=1,
        context_id=CTX_ID,
        context_fingerprint=CTX_FP,
        project_kind="local",
        project_uri="tests/lean_fixtures",
        project_revision="workspace",
        project_registry_key="fixtures",
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="fixture",
        imports=("LeanFaithFixtures.Basic",),
        header_text=HEADER,
        header_hash=sha256_hex(HEADER.encode("utf-8")),
    )


def _reference() -> TheoremRecord:
    theorem_id = make_id(THEOREM_PREFIX, {"lf021-local": "reference"})
    ancestry_id = make_id(ANCESTRY_PREFIX, {"lf021-local": "reference"})
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="public_fixture",
        source_revision="v1",
        source_record="identity",
        context_id=CTX_ID,
        declaration_kind="theorem",
        declaration_name="reference_identity",
        declaration_full_name="reference_identity",
        proof_stripped_declaration=("theorem reference_identity (n : Nat) : n = n := by sorry"),
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=sha256_hex(b"(n : Nat) : n = n"),
    )


def _problem(reference: TheoremRecord, *, private: bool = False) -> ProblemPoolRecord:
    record_id = make_problem_record_id(
        source="public_fixture",
        source_revision="v1",
        source_split="smoke",
        source_record_id="identity",
        problem_id="identity",
    )
    return ProblemPoolRecord(
        problem_record_id=record_id,
        problem_id="identity",
        problem_group="nl-problem:identity",
        source="public_fixture",
        source_revision="v1",
        source_split="smoke",
        source_record_id="identity",
        source_record_content_hash="1" * 64,
        nl_statement="For every natural number n, prove that n equals itself.",
        nl_trust=NLTrust.TRUSTED,
        nl_source_link="repo://tests/fixtures/lf021-local",
        context_id=CTX_ID,
        import_header_artifact=HEADER_ARTIFACT,
        import_header_hash=sha256_hex(HEADER.encode("utf-8")),
        reference_theorem_ids=(reference.theorem_id,),
        private_source_content=private,
        external_provider_eligible=not private,
        release_eligible=not private,
        eligibility="eligible",
        denylist_checked=True,
    )


def _representation(theorem_id: str) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for name in (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    ):
        statuses[name] = ViewStatus.OK
    return RepresentationRecord(
        representation_id=make_id(
            REPRESENTATION_PREFIX,
            {"theorem_id": theorem_id, "normalization_version": "repr_v2"},
        ),
        theorem_id=theorem_id,
        normalization_version="repr_v2",
        context_id=CTX_ID,
        raw_proof_stripped=STATEMENT + PLACEHOLDER,
        headless="(n : Nat) : n = n",
        signature_pp="(n : Nat) : n = n",
        signature_explicit="(n : Nat) : Eq Nat n n",
        semantic_atoms=("Eq", "Nat"),
        operator_tree={"kind": "const", "name": "Eq"},
        alpha_identity_fingerprint="d" * 64,
        view_status=statuses,
        content_hash=hash_canonical({"theorem_id": theorem_id, "views": "fixture"}),
        created_at=UTC,
    )


def _patch_representation(monkeypatch: pytest.MonkeyPatch) -> None:
    def build(
        backend: LeanInteractBackend,
        batch: RepresentationBatch,
        *,
        created_at: datetime.datetime,
    ) -> RepresentationBatchResult:
        del backend, created_at
        theorem_id = batch.ordered_theorem_inputs[0].theorem_id
        return RepresentationBatchResult((_representation(theorem_id),), ())

    monkeypatch.setattr(real_outputs, "build_representation_batch", build)


def _screening_index() -> CandidateScreeningIndex:
    registry, payloads = _screening_artifact_payloads()
    index = DenylistIndex(registry)
    binding = ProblemPoolDenylistBinding(
        index=index,
        manifest_path="tests/fixtures/lf021-screening/registry_manifest.json",
        manifest_sha256=sha256_hex(payloads["registry_manifest"]),
        active_registry_sha256=sha256_hex(payloads["active_registry"]),
        registry_content_hash=index.registry_content_hash,
    )
    return CandidateScreeningIndex(denylist=binding)


def _screening_artifact_payloads() -> tuple[FrozenRegistry, dict[str, bytes]]:
    """Build a complete, content-bound registry that cannot hit the smoke theorem."""

    unrelated_representation_hash = "c" * 64
    identity_registry_sha256 = "4" * 64
    statement_id = "5" * 64
    input_content_hash = "6" * 64
    registry = FrozenRegistry(
        frozen_at=UTC,
        benchmarks=(
            FrozenBenchmark(
                registry_key="lf021_unrelated_fixture",
                source_id="tests/fixtures/lf021-unrelated",
                revision="7" * 40,
                resolved=True,
                splits={"test": 1},
                row_ids=("unrelated:0",),
                nl_hashes=("8" * 64,),
                text_hashes=("9" * 64,),
                representation_hashes=(unrelated_representation_hash,),
            ),
        ),
        representation_signatures_appended=True,
    )
    record = BenchmarkSignatureRecord(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        statement_id=statement_id,
        input_content_hash=input_content_hash,
        registry_key="lf021_unrelated_fixture",
        source_id="tests/fixtures/lf021-unrelated",
        revision="7" * 40,
        split="test",
        row_id="unrelated:0",
        side=BenchmarkSide.CANDIDATE,
        context_id=CTX_ID,
        elaboration_status=LeanStatus.VALID.value,
        headless_hash=unrelated_representation_hash,
        signature_pp_hash=unrelated_representation_hash,
        signature_explicit_hash=unrelated_representation_hash,
        alpha_identity_fingerprint=unrelated_representation_hash,
        view_status={
            "headless": BenchmarkViewStatus.OK,
            "signature_pp": BenchmarkViewStatus.OK,
            "signature_explicit": BenchmarkViewStatus.OK,
            "alpha_identity_fingerprint": BenchmarkViewStatus.OK,
        },
    )
    detailed = build_benchmark_signature_artifact(
        identity_registry_sha256=identity_registry_sha256,
        context_id=CTX_ID,
        generated_at=UTC,
        input_checksums={"fixture": "a" * 64},
        records=(record,),
        failures=(),
    )
    work = BenchmarkSignatureWorkManifest(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        selection_version=BENCHMARK_SIGNATURE_SELECTION_VERSION,
        identity_registry_sha256=identity_registry_sha256,
        context_id=CTX_ID,
        generated_at=UTC,
        ordered_inputs=((statement_id, input_content_hash),),
    )
    active_payload = canonical_json_bytes(registry.model_dump(mode="json")) + b"\n"
    detailed_payload = canonical_json_bytes(detailed.model_dump(mode="json")) + b"\n"
    work_payload = canonical_json_bytes(work.model_dump(mode="json")) + b"\n"
    manifest = RepresentationSignatureManifest.model_validate(
        {
            "schema_version": 1,
            "artifact_kind": "benchmark_representation_signatures",
            "selection_version": detailed.selection_version,
            "normalization_version": detailed.normalization_version,
            "generated_at": UTC,
            "completed_at": UTC,
            "context_id": CTX_ID,
            "base_registry": {
                "path": "tests/fixtures/lf021-screening/base_registry.json",
                "sha256": identity_registry_sha256,
            },
            "active_registry": {
                "path": "tests/fixtures/lf021-screening/active_registry.json",
                "sha256": sha256_hex(active_payload),
            },
            "detailed_index": {
                "uri": "tests/fixtures/lf021-screening/detailed_index.json",
                "sha256": sha256_hex(detailed_payload),
                "required_for_preflight": True,
            },
            "input_manifest": {
                "uri": "tests/fixtures/lf021-screening/input_manifest.json",
                "sha256": sha256_hex(work_payload),
                "statement_count": 1,
            },
            "code_bundle": {
                "uri": "tests/fixtures/lf021-screening/code_bundle.tar.gz",
                "sha256": "b" * 64,
                "code_tree_hash": "d" * 64,
            },
            "accounting": {
                **detailed.accounting.model_dump(mode="json"),
                "failures": 0,
            },
            "resolved_benchmarks": {
                "lf021_unrelated_fixture": {
                    "statement_count": 1,
                    "representation_hash_count": 1,
                }
            },
            "unresolved_benchmark_policy": "protected_unknown_never_non_overlap",
            "missing_representation_policy": "protected_unknown_never_non_overlap",
        }
    )
    return registry, {
        "registry_manifest": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
        "active_registry": active_payload,
        "detailed_index": detailed_payload,
        "input_manifest": work_payload,
    }


def _screening_inputs(tmp_path: Path) -> QualificationScreeningInputFiles:
    directory = tmp_path / "screening-inputs"
    directory.mkdir(exist_ok=True)
    _, payloads = _screening_artifact_payloads()
    paths = {
        "registry_manifest": directory / "registry_manifest.json",
        "active_registry": directory / "active_registry.json",
        "detailed_index": directory / "detailed_index.json",
        "input_manifest": directory / "input_manifest.json",
    }
    for role, path in paths.items():
        path.write_bytes(payloads[role])
    return QualificationScreeningInputFiles(**paths)


def _code_bundle_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> QualificationCodeBundleBinding:
    bundle_path = tmp_path / "qualification-code-bundle.tar.gz"
    source = b"value = 1\n"
    manifest = {
        "schema_version": 1,
        "code_state": {
            "git_revision": "1" * 40,
            "git_dirty": False,
            "base_git_commit": "1" * 40,
            "code_tree_hash": _CODE_TREE_HASH,
            "tracked_diff_hash": "2" * 64,
            "untracked_files": [],
        },
        "files": [
            {
                "path": "source.py",
                "sha256": hashlib.sha256(source).hexdigest(),
                "mode": 0o644,
            }
        ],
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with (
        bundle_path.open("wb") as raw,
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

    original_safe_input = local_qualification._safe_regular_input

    def safe_input(repo_root: Path, value: str, *, label: str) -> Path:
        if value == _CODE_BUNDLE_SOURCE:
            return bundle_path
        return original_safe_input(repo_root, value, label=label)

    monkeypatch.setattr(local_qualification, "_safe_regular_input", safe_input)
    return QualificationCodeBundleBinding(
        source_artifact=_CODE_BUNDLE_SOURCE,
        sha256=hash_file(bundle_path),
        code_tree_hash=_CODE_TREE_HASH,
    )


def _runtime_binding() -> RuntimeEnvironmentBinding:
    return make_runtime_binding(
        repo_root=ROOT,
        environment_lock_artifact="uv.lock",
        torch_version="test-torch",
        transformers_version="test-transformers",
        driver_version="test-driver",
        device_name="fake-device",
        dtype="bfloat16",
    )


def _checkpoint_verification() -> LocalCheckpointVerification:
    model = load_local_qualification_config(CONFIG, repo_root=ROOT).config.active_model
    checkpoint = model.checkpoint_artifacts
    assert checkpoint is not None
    files = (
        LocalCheckpointFile(
            artifact="README.md",
            bytes=1,
            sha256=model.metadata_hashes.readme,
        ),
        LocalCheckpointFile(
            artifact="config.json",
            bytes=1,
            sha256=model.metadata_hashes.config,
        ),
        LocalCheckpointFile(
            artifact="tokenizer_config.json",
            bytes=1,
            sha256=model.metadata_hashes.tokenizer_config,
        ),
        LocalCheckpointFile(
            artifact="generation_config.json",
            bytes=1,
            sha256=model.metadata_hashes.generation_config,
        ),
        checkpoint.index,
        *checkpoint.shards,
        *checkpoint.auxiliary_files,
    )
    return LocalCheckpointVerification(
        model_repo_id=model.repo_id,
        model_revision=model.revision,
        snapshot_reference=f"unit://{model.repo_id}@{model.revision}",
        files=files,
        checkpoint_bytes=model.checkpoint_bytes,
    )


def _fixture_preflight(
    *,
    problem: ProblemPoolRecord,
    reference: TheoremRecord,
) -> LocalQualificationFixturePreflight:
    return LocalQualificationFixturePreflight(
        fixture_id="unit-fixture",
        fixture_sha256=hash_file(ROOT / FIXTURE_ARTIFACT),
        problem_record_id=problem.problem_record_id,
        reference_theorem_id=reference.theorem_id,
        reference_representation_id=_representation(reference.theorem_id).representation_id,
        context_id=CTX_ID,
        project_registry_key="fixtures",
        project_revision="workspace",
        import_header_artifact=HEADER_ARTIFACT,
        import_header_sha256=problem.import_header_hash,
        active_registry_hash=_screening_index().denylist.registry_content_hash,
    )


def test_fake_runtime_qualifies_and_replays_without_persisting_semantic_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    problem = _problem(reference)
    code_bundle = _code_bundle_binding(tmp_path, monkeypatch)

    first = run_local_kimina_qualification(
        loaded_config=loaded,
        runtime_binding=_runtime_binding(),
        runtime=FakeLocalRuntime(FENCED),
        problem=problem,
        expected_declaration_name=NAME,
        context=_context(),
        references=(reference,),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, FakeLeanBackend()),
        screening_index=_screening_index(),
        artifact_root=tmp_path,
        run_directory=tmp_path / "run-a",
        created_at=UTC,
        fixture_artifact=FIXTURE_ARTIFACT,
        fixture_preflight=_fixture_preflight(
            problem=problem,
            reference=reference,
        ),
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=code_bundle,
        screening_inputs=_screening_inputs(tmp_path),
    )
    second = run_local_kimina_qualification(
        loaded_config=loaded,
        runtime_binding=_runtime_binding(),
        runtime=FakeLocalRuntime(FENCED),
        problem=problem,
        expected_declaration_name=NAME,
        context=_context(),
        references=(reference,),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, FakeLeanBackend()),
        screening_index=_screening_index(),
        artifact_root=tmp_path,
        run_directory=tmp_path / "run-b",
        created_at=UTC,
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=code_bundle,
        screening_inputs=_screening_inputs(tmp_path),
    )

    assert first.terminal.status is QualificationStatus.QUALIFIED_SMOKE
    assert first.terminal.terminal_id == second.terminal.terminal_id
    assert first.terminal.qualifies_for_gate5g is False
    assert first.terminal.semantic_labels_created is False
    assert first.extracted is not None
    assert first.extracted.parsed.statement == STATEMENT
    assert first.admitted is not None
    assert all(pair.resolved_label_id is None for pair in first.admitted.pairs)
    assert first.admitted.nl_lean is not None
    assert first.admitted.nl_lean.resolved_label_id is None

    manifest = persist_local_qualification_bundle(
        first,
        run_directory=tmp_path / "run-a",
        artifact_root=tmp_path,
    )
    roles = {item.role for item in first.input_binding.archived_inputs}
    assert roles == {
        "benchmark_active_registry",
        "benchmark_detailed_index",
        "benchmark_input_manifest",
        "benchmark_registry_manifest",
        "checkpoint_verification",
        "code_bundle",
        "qualification_config",
        "prompt_template",
        "common_suffix",
        "parser_source",
        "runtime_adapter",
        "environment_lock",
        "fixture_source",
        "fixture_preflight",
        "import_header",
        "execution_input",
        "prior_candidate_index",
    }
    assert {f"input_{role}" for role in roles}.issubset(manifest.artifacts)
    assert not any(name.startswith("pair_") for name in manifest.artifacts)
    assert "nl_lean" not in manifest.artifacts
    terminal, call, attempt = verify_local_qualification_bundle(
        manifest,
        artifact_root=tmp_path,
        repo_root=tmp_path / "repository-does-not-exist",
        problem=problem,
    )
    assert terminal == first.terminal
    assert call == first.lineage.call
    assert attempt == first.lineage.attempt


def test_archived_input_tamper_and_symlink_fail_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    problem = _problem(reference)
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
        screening_index=_screening_index(),
        artifact_root=tmp_path,
        run_directory=tmp_path / "tamper",
        created_at=UTC,
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
        screening_inputs=_screening_inputs(tmp_path),
    )
    manifest = persist_local_qualification_bundle(
        result,
        run_directory=tmp_path / "tamper",
        artifact_root=tmp_path,
    )
    archived = result.input_binding.archived_inputs[0]
    archived_path = tmp_path / archived.archive_artifact
    original = archived_path.read_bytes()
    archived_path.write_bytes(original + b"tampered")
    with pytest.raises(LocalQualificationReplayError, match="bundle artifact drift"):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=tmp_path,
            repo_root=tmp_path / "absent",
            problem=problem,
        )

    archived_path.write_bytes(original)
    target = tmp_path / "same-bytes-target"
    target.write_bytes(original)
    archived_path.unlink()
    archived_path.symlink_to(target)
    with pytest.raises(LocalQualificationReplayError, match="symlink"):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=tmp_path,
            repo_root=tmp_path / "absent",
            problem=problem,
        )


def test_bundle_manifest_path_escape_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    problem = _problem(reference)
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
        screening_index=_screening_index(),
        artifact_root=tmp_path,
        run_directory=tmp_path / "escape",
        created_at=UTC,
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
        screening_inputs=_screening_inputs(tmp_path),
    )
    manifest = persist_local_qualification_bundle(
        result,
        run_directory=tmp_path / "escape",
        artifact_root=tmp_path,
    )
    artifacts = {**manifest.artifacts, "terminal": "../outside.json"}
    payload = {
        "schema": "lf021_local_qualification_bundle_v2",
        "terminal_id": manifest.terminal_id,
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
        "artifact_sha256": manifest.artifact_sha256,
    }
    escaped = LocalQualificationBundleManifest(
        bundle_id="local_qualification_bundle:" + hash_canonical(payload),
        terminal_id=manifest.terminal_id,
        artifacts=artifacts,
        artifact_sha256=manifest.artifact_sha256,
    )
    with pytest.raises(LocalQualificationReplayError, match="escapes artifact_root"):
        verify_local_qualification_bundle(
            escaped,
            artifact_root=tmp_path,
            repo_root=tmp_path / "absent",
            problem=problem,
        )


def test_archive_destination_symlink_rejected_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    runtime = FakeLocalRuntime(FENCED)
    outside = tmp_path / "outside"
    outside.mkdir()
    run_directory = tmp_path / "symlink-run"
    run_directory.mkdir()
    (run_directory / "qualification_inputs").symlink_to(outside)
    with pytest.raises(LocalQualificationReplayError, match="symlink"):
        run_local_kimina_qualification(
            loaded_config=loaded,
            runtime_binding=_runtime_binding(),
            runtime=runtime,
            problem=_problem(reference),
            expected_declaration_name=NAME,
            context=_context(),
            references=(reference,),
            registered_header=HEADER,
            backend=cast(LeanInteractBackend, FakeLeanBackend()),
            screening_index=_screening_index(),
            artifact_root=tmp_path,
            run_directory=run_directory,
            created_at=UTC,
            checkpoint_verification=_checkpoint_verification(),
            code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
            screening_inputs=_screening_inputs(tmp_path),
        )
    assert runtime.requests == []


def test_observed_unfenced_kimina_output_is_terminal_parse_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    output = f"import Mathlib\n\n{STATEMENT} := by sorry"
    result = run_local_kimina_qualification(
        loaded_config=loaded,
        runtime_binding=_runtime_binding(),
        runtime=FakeLocalRuntime(output),
        problem=_problem(reference),
        expected_declaration_name=NAME,
        context=_context(),
        references=(reference,),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, FakeLeanBackend()),
        screening_index=_screening_index(),
        artifact_root=tmp_path,
        run_directory=tmp_path / "unfenced",
        created_at=UTC,
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
        screening_inputs=_screening_inputs(tmp_path),
    )
    assert result.terminal.status is QualificationStatus.PARSE_FAILED
    assert result.terminal.error_code == "missing_final_fence"
    assert result.materialized is None
    assert result.provider_result.raw_response_path.is_file()


def test_v2_config_qualifies_exact_raw_kimina_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG_V2, repo_root=ROOT)
    reference = _reference()
    raw = f"{HEADER}\n\n{STATEMENT} := by sorry"
    result = run_local_kimina_qualification(
        loaded_config=loaded,
        runtime_binding=_runtime_binding(),
        runtime=FakeLocalRuntime(raw),
        problem=_problem(reference),
        expected_declaration_name=NAME,
        context=_context(),
        references=(reference,),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, FakeLeanBackend()),
        screening_index=_screening_index(),
        artifact_root=tmp_path,
        run_directory=tmp_path / "raw-v2",
        created_at=UTC,
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
        screening_inputs=_screening_inputs(tmp_path),
    )
    assert result.terminal.status is QualificationStatus.QUALIFIED_SMOKE
    assert result.terminal.parser_id == "lean_final_fence_or_raw_signature_v2"
    assert result.extracted is not None
    assert result.extracted.parsed.statement == STATEMENT


def test_missing_active_screening_never_admits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_representation(monkeypatch)
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    result = run_local_kimina_qualification(
        loaded_config=loaded,
        runtime_binding=_runtime_binding(),
        runtime=FakeLocalRuntime(FENCED),
        problem=_problem(reference),
        expected_declaration_name=NAME,
        context=_context(),
        references=(reference,),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, FakeLeanBackend()),
        screening_index=None,
        artifact_root=tmp_path,
        run_directory=tmp_path / "no-screen",
        created_at=UTC,
        checkpoint_verification=_checkpoint_verification(),
        code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
    )
    assert result.terminal.status is QualificationStatus.SCREENING_UNAVAILABLE
    assert result.screening is None
    assert result.admitted is None


def test_private_fixture_is_rejected_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = load_local_qualification_config(CONFIG, repo_root=ROOT)
    reference = _reference()
    runtime = FakeLocalRuntime(FENCED)
    with pytest.raises(Exception, match="private source content"):
        run_local_kimina_qualification(
            loaded_config=loaded,
            runtime_binding=_runtime_binding(),
            runtime=runtime,
            problem=_problem(reference, private=True),
            expected_declaration_name=NAME,
            context=_context(),
            references=(reference,),
            registered_header=HEADER,
            backend=cast(LeanInteractBackend, FakeLeanBackend()),
            screening_index=_screening_index(),
            artifact_root=tmp_path,
            run_directory=tmp_path / "private",
            created_at=UTC,
            checkpoint_verification=_checkpoint_verification(),
            code_bundle=_code_bundle_binding(tmp_path, monkeypatch),
        )
    assert runtime.requests == []
