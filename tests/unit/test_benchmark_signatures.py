"""Phase-3 additive benchmark representation-signature registry."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_file
from leanfaith.datasets.benchmark_signatures import (
    BENCHMARK_SIGNATURE_INDEX_FILENAME,
    BENCHMARK_SIGNATURE_REGISTRY_FILENAME,
    BENCHMARK_SIGNATURE_SCHEMA_VERSION,
    BenchmarkSide,
    BenchmarkSignatureArtifact,
    BenchmarkSignatureRecord,
    BenchmarkStatementInput,
    BenchmarkViewStatus,
    build_benchmark_signature_artifact,
    build_benchmark_signature_record,
    load_resolved_benchmark_inputs,
    process_benchmark_signature_inputs,
    write_benchmark_signature_artifacts,
)
from leanfaith.datasets.denylist import (
    FrozenBenchmark,
    FrozenRegistry,
    lean_hash,
    load_frozen_registry,
    nl_hash,
    unresolved_benchmark,
    write_frozen_registry,
)
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus

_UTC = datetime.datetime(2026, 7, 18, tzinfo=datetime.UTC)
_FINGERPRINT = "1" * 64
_CONTEXT_ID = f"ctx:{_FINGERPRINT}"


def _result(
    request: LeanRequest,
    *,
    status: LeanStatus,
    declarations: tuple[dict[str, object], ...] = (),
    messages: tuple[dict[str, object], ...] = (),
) -> LeanResult:
    return LeanResult(
        request_id=request.request_id,
        request_hash="2" * 64,
        context_id=request.context_id,
        context_fingerprint=_FINGERPRINT,
        status=status,
        declarations=declarations,
        messages=messages,
    )


class _FakeBenchmarkBackend:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: LeanRequest) -> LeanResult:
        self.calls += 1
        assert request.code is not None
        if request.declarations:
            if "badStatement" in request.code:
                return _result(request, status=LeanStatus.INVALID)
            match = re.search(r"theorem\s+(\w+)\s+(:\s*True)\s*:=", request.code)
            assert match is not None
            line = request.code.count("\n", 0, match.start()) + 1
            line_start = request.code.rfind("\n", 0, match.start()) + 1
            start_column = match.start() - line_start
            signature_start = match.start(2) - line_start
            signature_finish = match.end(2) - line_start
            line_finish = request.code.find("\n", match.end())
            if line_finish < 0:
                line_finish = len(request.code)
            declaration = {
                "name": match.group(1),
                "full_name": match.group(1),
                "kind": "theorem",
                "range": {
                    "start": {"line": line, "column": start_column},
                    "finish": {"line": line, "column": line_finish - line_start},
                },
                "signature": {
                    "pp": match.group(2),
                    "range": {
                        "start": {"line": line, "column": signature_start},
                        "finish": {"line": line, "column": signature_finish},
                    },
                },
                "type": {"pp": "True"},
            }
            return _result(
                request,
                status=LeanStatus.VALID_WITH_SORRY,
                declarations=(declaration,),
            )
        match = re.search(r"#check @(\w+)", request.code)
        assert match is not None
        name = match.group(1)
        return _result(
            request,
            status=LeanStatus.VALID_WITH_SORRY,
            messages=(
                {"severity": "info", "data": f"@{name} : True"},
                {"severity": "info", "data": f"@{name} : True"},
                {
                    "severity": "info",
                    "data": (
                        'LFJSON {"name":"' + name + '","tree":{"k":"const","n":"True","us":"[]"}}'
                    ),
                },
            ),
        )

    def close(self) -> None:
        return None


def _input(name: str, statement: str, *, row_ordinal: int = 0) -> BenchmarkStatementInput:
    return BenchmarkStatementInput.create(
        registry_key="formalrx_test",
        source_id="LARK-Lab/FormalRx-Test",
        revision="a" * 40,
        split="test",
        row_id=name,
        row_ordinal=row_ordinal,
        side=BenchmarkSide.CANDIDATE,
        header="import Mathlib",
        statement=statement,
    )


def test_successful_statement_gets_all_lean_derived_hashes() -> None:
    backend = _FakeBenchmarkBackend()
    item = _input("ok", "theorem good : True := by sorry")

    record, failures = build_benchmark_signature_record(
        backend,
        item,
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert failures == ()
    assert record.elaboration_status == LeanStatus.VALID_WITH_SORRY.value
    assert record.theorem_id is not None
    assert all(status == BenchmarkViewStatus.OK for status in record.view_status.values())
    assert len(record.representation_hashes()) == 4


def test_successful_statement_preserves_nonzero_row_ordinal() -> None:
    item = _input("ordinal", "theorem good : True := by sorry", row_ordinal=37)

    record, failures = build_benchmark_signature_record(
        _FakeBenchmarkBackend(),
        item,
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert failures == ()
    assert record.row_ordinal == 37


class _FlakyExtractionBackend(_FakeBenchmarkBackend):
    def __init__(self, failures: tuple[object, ...]) -> None:
        super().__init__()
        self.failures = list(failures)
        self.extraction_attempts: list[str] = []

    def run(self, request: LeanRequest) -> LeanResult:
        if not request.declarations:
            return super().run(request)
        self.extraction_attempts.append(str(request.metadata.get("attempt")))
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            assert isinstance(failure, LeanStatus)
            return _result(request, status=failure)
        return super().run(request)


class _FlakyRepresentationBackend(_FakeBenchmarkBackend):
    def __init__(self, failure: object) -> None:
        super().__init__()
        self.failure = failure
        self.representation_attempts: list[str] = []

    def run(self, request: LeanRequest) -> LeanResult:
        if request.declarations:
            return super().run(request)
        self.representation_attempts.append(str(request.metadata.get("attempt")))
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            if isinstance(failure, BaseException):
                raise failure
            assert isinstance(failure, LeanStatus)
            return _result(request, status=failure)
        return super().run(request)


def test_extraction_backend_exception_retries_with_deterministic_lineage() -> None:
    backend = _FlakyExtractionBackend((RuntimeError("transient"),))

    record, failures = build_benchmark_signature_record(
        backend,
        _input("retry-extraction", "theorem good : True := by sorry"),
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert failures == ()
    assert record.elaboration_status == LeanStatus.VALID_WITH_SORRY.value
    assert backend.extraction_attempts == ["0", "1"]


@pytest.mark.parametrize("failure", [LeanStatus.TIMEOUT, TimeoutError("transient")])
def test_representation_infrastructure_failure_retries_only_failed_request(
    failure: object,
) -> None:
    backend = _FlakyRepresentationBackend(failure)

    record, failures = build_benchmark_signature_record(
        backend,
        _input("retry-representation", "theorem good : True := by sorry"),
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert failures == ()
    assert all(status == BenchmarkViewStatus.OK for status in record.view_status.values())
    assert backend.representation_attempts[:2] == ["0", "1"]


def test_exhausted_exception_retry_has_terminal_internal_status() -> None:
    backend = _FlakyExtractionBackend((RuntimeError("first"), RuntimeError("second")))

    record, failures = build_benchmark_signature_record(
        backend,
        _input("retry-exhausted", "theorem good : True := by sorry"),
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert backend.extraction_attempts == ["0", "1"]
    assert record.elaboration_status == LeanStatus.INTERNAL_ERROR.value
    assert record.failure_codes == ("source_internal_error",)
    assert failures[0].detail == "RuntimeError: second"


def test_semantic_invalid_result_is_not_retried() -> None:
    backend = _FlakyExtractionBackend((LeanStatus.INVALID,))

    record, failures = build_benchmark_signature_record(
        backend,
        _input("invalid-no-retry", "theorem good : True := by sorry"),
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert backend.extraction_attempts == ["0"]
    assert record.elaboration_status == LeanStatus.INVALID.value
    assert failures[0].code == "source_invalid"


def test_non_elaboration_is_explicit_and_not_hashed() -> None:
    backend = _FakeBenchmarkBackend()
    item = _input("bad", "badStatement")

    record, failures = build_benchmark_signature_record(
        backend,
        item,
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )

    assert record.elaboration_status == LeanStatus.INVALID.value
    assert record.representation_hashes() == ()
    assert record.failure_codes == ("source_invalid",)
    assert len(failures) == 1
    assert failures[0].statement_id == item.statement_id


def test_resume_is_bound_to_inputs_and_does_not_repeat_lean(tmp_path: Path) -> None:
    backend = _FakeBenchmarkBackend()
    inputs = (_input("ok", "theorem good : True := by sorry"),)
    first = process_benchmark_signature_inputs(
        backend,
        inputs,
        identity_registry_sha256="3" * 64,
        context_id=_CONTEXT_ID,
        created_at=_UTC,
        work_dir=tmp_path / "work",
    )
    calls_after_first = backend.calls
    second = process_benchmark_signature_inputs(
        backend,
        inputs,
        identity_registry_sha256="3" * 64,
        context_id=_CONTEXT_ID,
        created_at=_UTC,
        work_dir=tmp_path / "work",
    )

    assert first == second
    assert backend.calls == calls_after_first
    with pytest.raises(FileExistsError, match="different bytes"):
        process_benchmark_signature_inputs(
            backend,
            (_input("changed", "theorem changed : True := by sorry"),),
            identity_registry_sha256="3" * 64,
            context_id=_CONTEXT_ID,
            created_at=_UTC,
            work_dir=tmp_path / "work",
        )


def _registry() -> FrozenRegistry:
    return FrozenRegistry(
        frozen_at=_UTC,
        benchmarks=(
            FrozenBenchmark(
                registry_key="formalrx_test",
                source_id="LARK-Lab/FormalRx-Test",
                revision="a" * 40,
                resolved=True,
                splits={"test": 1},
                row_ids=("ok",),
                nl_hashes=("4" * 64,),
                text_hashes=("5" * 64,),
            ),
            unresolved_benchmark("rlm25", "unresolved"),
        ),
    )


def _artifact(record: BenchmarkSignatureRecord, registry_hash: str) -> BenchmarkSignatureArtifact:
    return build_benchmark_signature_artifact(
        identity_registry_sha256=registry_hash,
        context_id=_CONTEXT_ID,
        generated_at=_UTC,
        input_checksums={"input": "6" * 64},
        records=(record,),
        failures=(),
    )


def test_new_registry_is_additive_and_original_is_immutable(tmp_path: Path) -> None:
    identity_path = tmp_path / "frozen_ids.json"
    write_frozen_registry(_registry(), identity_path)
    original = identity_path.read_bytes()
    record, _ = build_benchmark_signature_record(
        _FakeBenchmarkBackend(),
        _input("ok", "theorem good : True := by sorry"),
        context_id=_CONTEXT_ID,
        created_at=_UTC,
    )
    artifact = _artifact(record, hash_file(identity_path))

    registry_path, registry_hash, index_path, index_hash = write_benchmark_signature_artifacts(
        identity_registry_path=identity_path,
        output_dir=tmp_path,
        artifact=artifact,
    )

    assert identity_path.read_bytes() == original
    assert registry_path.name == BENCHMARK_SIGNATURE_REGISTRY_FILENAME
    assert index_path.name == BENCHMARK_SIGNATURE_INDEX_FILENAME
    assert registry_hash == hash_file(registry_path)
    assert index_hash == hash_file(index_path)
    updated = load_frozen_registry(registry_path)
    assert updated.representation_signatures_appended
    formalrx = next(item for item in updated.benchmarks if item.registry_key == "formalrx_test")
    assert set(formalrx.representation_hashes) == set(record.representation_hashes())
    assert formalrx.nl_hashes == _registry().benchmarks[0].nl_hashes
    # Exact replay is idempotent; a changed versioned artifact cannot overwrite.
    write_benchmark_signature_artifacts(
        identity_registry_path=identity_path,
        output_dir=tmp_path,
        artifact=artifact,
    )
    changed = artifact.model_copy(update={"generated_at": _UTC + datetime.timedelta(seconds=1)})
    with pytest.raises(FileExistsError, match="different bytes"):
        write_benchmark_signature_artifacts(
            identity_registry_path=identity_path,
            output_dir=tmp_path,
            artifact=changed,
        )


def test_loader_matches_identity_freeze_and_ignores_labels(tmp_path: Path) -> None:
    proofnet_dir = tmp_path / "proofnet"
    proofnet_dir.mkdir()
    revision = "b" * 40
    (proofnet_dir / "manifest.json").write_text(
        json.dumps({"source_revision": revision}), encoding="utf-8"
    )
    proofnet_row = {
        "problem_id": "p1",
        "nl_statement": "proofnet problem",
        "lean_header": "import Mathlib",
        "reference_lean": "theorem r : True := by sorry",
        "candidate_lean": "theorem c : True := by sorry",
        "source_label": True,
    }
    (proofnet_dir / "test.jsonl").write_text(json.dumps(proofnet_row) + "\n", encoding="utf-8")
    formalrx_path = tmp_path / "formalrx.jsonl"
    formalrx_path.write_text(
        json.dumps(
            {
                "idx": "f1",
                "header": "import Mathlib",
                "informal_statement": "formalrx problem",
                "formal_statement": "theorem f : True := by sorry",
                "diagnosis": {"aligned": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    identity = FrozenRegistry(
        frozen_at=_UTC,
        benchmarks=(
            FrozenBenchmark(
                registry_key="proofnetverif",
                source_id="PAug/ProofNetVerif",
                revision=revision,
                resolved=True,
                splits={"test": 1},
                row_ids=("test:p1",),
                nl_hashes=(nl_hash("proofnet problem"),),
                text_hashes=tuple(
                    sorted(
                        {
                            lean_hash("theorem r : True := by sorry"),
                            lean_hash("theorem c : True := by sorry"),
                        }
                    )
                ),
            ),
            FrozenBenchmark(
                registry_key="formalrx_test",
                source_id="LARK-Lab/FormalRx-Test",
                revision="c" * 40,
                resolved=True,
                splits={"test": 1},
                row_ids=("f1",),
                nl_hashes=(nl_hash("formalrx problem"),),
                text_hashes=tuple(
                    sorted(
                        {
                            lean_hash("import Mathlib"),
                            lean_hash("theorem f : True := by sorry"),
                        }
                    )
                ),
            ),
        ),
    )
    identity_path = tmp_path / "frozen_ids.json"
    write_frozen_registry(identity, identity_path)

    inputs, checksums = load_resolved_benchmark_inputs(
        identity_registry_path=identity_path,
        proofnet_dir=proofnet_dir,
        formalrx_jsonl=formalrx_path,
    )

    assert len(inputs) == 3
    assert {item.side for item in inputs} == {BenchmarkSide.REFERENCE, BenchmarkSide.CANDIDATE}
    assert all("label" not in item.model_dump() for item in inputs)
    assert all("diagnosis" not in item.model_dump() for item in inputs)
    assert checksums[str(identity_path)] == hash_file(identity_path)


def test_artifact_indexes_hash_collisions_to_every_statement() -> None:
    base = BenchmarkSignatureRecord(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        statement_id="1" * 64,
        input_content_hash="2" * 64,
        registry_key="formalrx_test",
        source_id="source",
        revision="revision",
        split="test",
        row_id="one",
        side=BenchmarkSide.CANDIDATE,
        context_id=_CONTEXT_ID,
        elaboration_status=LeanStatus.VALID.value,
        headless_hash="3" * 64,
        view_status={
            "headless": BenchmarkViewStatus.OK,
            "signature_pp": BenchmarkViewStatus.NOT_ATTEMPTED,
            "signature_explicit": BenchmarkViewStatus.NOT_ATTEMPTED,
            "alpha_identity_fingerprint": BenchmarkViewStatus.NOT_ATTEMPTED,
        },
    )
    other = base.model_copy(
        update={"statement_id": "4" * 64, "input_content_hash": "5" * 64, "row_id": "two"}
    )
    artifact = build_benchmark_signature_artifact(
        identity_registry_sha256="6" * 64,
        context_id=_CONTEXT_ID,
        generated_at=_UTC,
        input_checksums={"input": "7" * 64},
        records=(other, base),
        failures=(),
    )
    assert artifact.retrieval_indexes["headless"]["3" * 64] == (
        "1" * 64,
        "4" * 64,
    )
    assert artifact.accounting.attempted == 2
    assert artifact.accounting.view_success["headless"] == 2


def test_cli_help_exposes_immutable_additive_inputs() -> None:
    result = CliRunner().invoke(app, ["append-benchmark-signatures", "--help"])
    assert result.exit_code == 0, result.output
    assert "--formalrx-jsonl" in result.output
    assert "--code-bundle" in result.output
    assert "--identity-registry" in result.output
    assert "new versioned benchmark registry" in result.output
