"""Exact E2 infrastructure recovery preserves the immutable parent lineage."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.lean.leaninteract_backend import (
    BackendExecutionBinding,
    BackendSettings,
    LeanInteractBackend,
)
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations import NORMALIZATION_VERSION, TheoremForRepresentation
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.provisional_pair_combine import (
    ProvisionalPairCombineError,
    combine_provisional_pair_roots,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_e2_materializer import (
    V2E2MaterializationResult,
    build_v2_e2_result,
)
from leanfaith.transforms.v2_e2_p18_runtime import build_v2_e2_p18_runtime
from leanfaith.transforms.v2_e2_recovery import V2E2RecoveryError, recover_v2_e2_attempt
from leanfaith.transforms.v2_e2_recovery_schema import build_recovery_receipt
from leanfaith.transforms.v2_e2_scale_run import V2E2ScaleRunManifest, run_v2_e2_scale
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend
from tests.unit.test_deterministic_v2_p18 import _records, _root

_REPO = Path(__file__).resolve().parents[2]
_IMPORT = "import LeanFaithFixtures"


def _line(value: object) -> bytes:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return canonical_json_bytes(payload) + b"\n"


def _install_representations(
    monkeypatch: pytest.MonkeyPatch,
    source_representations: dict[str, RepresentationRecord],
) -> None:
    import leanfaith.transforms.v2_e2_scale as module

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        output: list[RepresentationRecord] = []
        for item in inputs:
            source = source_representations[item.full_name]
            candidate = source.model_copy(
                update={
                    "representation_id": "repr:" + item.theorem_id.removeprefix("thm:"),
                    "theorem_id": item.theorem_id,
                    "normalization_version": NORMALIZATION_VERSION,
                    "raw_proof_stripped": item.proof_stripped,
                    "headless": "(x y : Nat) : y = x",
                    "signature_pp": "(x y : Nat) : y = x",
                    "signature_explicit": "∀ (x y : Nat), Eq y x",
                    "semantic_atoms": semantic_atoms(_root(swapped=True)),
                    "operator_tree": operator_tree(_root(swapped=True)),
                    "content_hash": "0" * 64,
                }
            )
            output.append(
                candidate.model_copy(
                    update={"content_hash": _representation_payload_hash(candidate)}
                )
            )
        return output

    monkeypatch.setattr(module, "build_representations", fake_build)


def _make_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    records = []
    by_name: dict[str, RepresentationRecord] = {}
    for index in range(2):
        name = f"p18_recovery_{index}"
        source = f"theorem {name} (x y : Nat) : x = y := by sorry"
        theorem, representation = _records(source, name, _root())
        theorem = theorem.model_copy(
            update={
                "declaration_name": name,
                "declaration_full_name": name,
                "inline_elaboration_source": f"{_IMPORT}\n{source}",
            }
        )
        records.append((theorem, representation))
        by_name[name] = representation
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_bytes(b"".join(_line(theorem) for theorem, _ in records))
    representation_path.write_bytes(
        b"".join(_line(representation) for _, representation in records)
    )
    _install_representations(monkeypatch, by_name)
    parent = tmp_path / "parent"
    run_v2_e2_scale(
        backend=cast(
            LeanInteractBackend,
            _BatchBackend(
                (LeanStatus.VALID_WITH_SORRY, LeanStatus.VALID_WITH_SORRY),
                workers=1,
            ),
        ),
        runtime=build_v2_e2_p18_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=project,
        import_header=_IMPORT,
        output_dir=parent,
        batch_size=2,
        base_seed=18,
        workers=1,
    )
    _mark_infrastructure(parent, result_index=0)
    return parent


def _mark_infrastructure(parent: Path, *, result_index: int) -> None:
    results = [
        V2E2MaterializationResult.model_validate_json(line)
        for line in (parent / "results.jsonl").read_text().splitlines()
    ]
    original = results[result_index]
    assert original.draft is not None
    results[result_index] = build_v2_e2_result(
        profile_id=original.profile_id,
        profile_config_hash=original.profile_config_hash,
        rule_id=original.rule_id,
        terminal_status="candidate_infrastructure_error",
        attempt=original.attempt,
        draft=original.draft,
        failure_codes=("lean_timeout",),
        resolved_label_count=0,
        promoted_item_count=0,
        training_eligible=False,
    )
    payload = b"".join(_line(result) for result in results)
    journal = parent / "journal" / "batch_000000.jsonl"
    journal.write_bytes(payload)
    (parent / "results.jsonl").write_bytes(payload)
    status_counts = Counter(item.terminal_status for item in results)
    family_counts = Counter(f"{item.rule_id}:{item.terminal_status}" for item in results)
    manifest = V2E2ScaleRunManifest(
        run_spec_sha256=hash_file(parent / "run_spec.json"),
        batch_count=1,
        result_count=2,
        terminal_status_counts=dict(sorted(status_counts.items())),
        family_status_counts=dict(sorted(family_counts.items())),
        journal_tree_hash=hash_canonical([(journal.name, hash_file(journal))]),
        results_sha256=hash_file(parent / "results.jsonl"),
    )
    (parent / "manifest.json").write_bytes(_line(manifest))


class _RecoveryBackend:
    def __init__(self, settings: BackendSettings, statuses: Sequence[LeanStatus]) -> None:
        self.settings = settings
        self.statuses = list(statuses)
        self.run_count = 0
        self.reset_count = 0
        self.closed = False
        self.execution_binding = BackendExecutionBinding(
            server_mode=settings.server_mode,
            workers=settings.workers,
            memory_hard_limit_mb=settings.memory_hard_limit_mb,
        )

    def run(self, request: LeanRequest) -> LeanResult:
        return self.run_batch((request,))[0]

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        assert len(requests) == 1
        request = requests[0]
        if request.metadata.get("artifact_kind") == "v2_e2_candidate":
            assert request.timeout_seconds == 600.0
            assert request.metadata["attempt"] == str(self.run_count)
        status = self.statuses[self.run_count]
        request_hash = f"{self.run_count + 1:064x}"
        raw_dir = self.settings.raw_response_dir
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw = raw_dir / f"attempt-{self.run_count}.json"
        raw.write_text(
            json.dumps(
                {
                    "request": {
                        "request_id": request.request_id,
                        "context_id": request.context_id,
                        "allow_sorry": request.allow_sorry,
                        "timeout_seconds": request.timeout_seconds,
                    },
                    "transport_isolation": {
                        "attempt": str(request.metadata["attempt"]),
                    },
                    "request_hash": request_hash,
                    "status": status.value,
                }
            ),
            encoding="utf-8",
        )
        self.run_count += 1
        return [
            LeanResult(
                request_id=request.request_id,
                request_hash=request_hash,
                context_id=request.context_id,
                context_fingerprint=request.context_id.removeprefix("ctx:"),
                status=status,
                raw_response_path=str(raw),
            )
        ]

    def reset_session(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        self.closed = True


def _toolchain(_project: Path) -> tuple[str, str, str]:
    return (
        "leanprover/lean4:v4.31.0-rc1",
        "v4.31.0-rc1",
        "Lean (version 4.31.0-rc1, synthetic-test)",
    )


def test_exact_recovery_changes_only_target_and_combiner_accepts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = _make_parent(monkeypatch, tmp_path)
    parent_raw = parent / "raw_lean" / "attempt-0.json"
    parent_raw.parent.mkdir(parents=True, exist_ok=True)
    parent_raw.write_text('{"status":"original-timeout"}', encoding="utf-8")
    parent_raw_hash = hash_file(parent_raw)
    original_parent_hash = hash_file(parent / "results.jsonl")
    parent_lines = (parent / "results.jsonl").read_bytes().splitlines(keepends=True)
    failed = V2E2MaterializationResult.model_validate_json(parent_lines[0])
    created: list[_RecoveryBackend] = []

    def factory(settings: BackendSettings) -> LeanInteractBackend:
        backend = _RecoveryBackend(
            settings,
            (LeanStatus.TIMEOUT, LeanStatus.VALID_WITH_SORRY),
        )
        created.append(backend)
        return cast(LeanInteractBackend, backend)

    output = tmp_path / "recovered"
    artifacts = recover_v2_e2_attempt(
        parent_root=parent,
        output_dir=output,
        repo_root=_REPO,
        import_header=_IMPORT,
        target_result_line_number=1,
        target_result_id=failed.result_id,
        target_attempt_id=failed.attempt.attempt_id,
        backend_factory=factory,
        toolchain_probe=_toolchain,
    )

    assert artifacts.output_dir == output
    assert hash_file(parent / "results.jsonl") == original_parent_hash
    assert hash_file(parent_raw) == parent_raw_hash
    assert hash_file(output / "raw_lean" / "attempt-0.json") == parent_raw_hash
    output_lines = artifacts.results_path.read_bytes().splitlines(keepends=True)
    assert output_lines[1] == parent_lines[1]
    assert output_lines[0] != parent_lines[0]
    recovered = V2E2MaterializationResult.model_validate_json(output_lines[0])
    assert recovered.terminal_status != "candidate_infrastructure_error"
    assert created[0].run_count == 2
    assert created[0].reset_count == 1
    assert created[0].closed is True
    assert (output / "run_spec.json").read_bytes() == (parent / "run_spec.json").read_bytes()
    receipt = json.loads((output / "recovery_receipt.json").read_text())
    assert len(receipt["lean_attempts"]) == 2
    assert [attempt["status"] for attempt in receipt["lean_attempts"]] == [
        "timeout",
        "valid_with_sorry",
    ]
    for attempt in receipt["lean_attempts"]:
        raw_path = output / attempt["raw_response_relative_path"]
        assert hash_file(raw_path) == attempt["raw_response_sha256"]
    assert ".recovery-" in receipt["lean_attempts"][0]["raw_response_relative_path"]

    combined = combine_provisional_pair_roots(
        materialization_roots=(output,),
        output_dir=tmp_path / "combined",
    )
    assert combined.gross_count == 0

    raw_tampered = tmp_path / "recovered-raw-tampered"
    shutil.copytree(output, raw_tampered)
    raw_receipt = json.loads((raw_tampered / "recovery_receipt.json").read_text())
    raw_path = raw_tampered / raw_receipt["lean_attempts"][0]["raw_response_relative_path"]
    raw_path.write_bytes(raw_path.read_bytes() + b"tamper")
    with pytest.raises(ProvisionalPairCombineError, match=r"output root tree|raw response"):
        combine_provisional_pair_roots(
            materialization_roots=(raw_tampered,),
            output_dir=tmp_path / "combined-raw-tampered",
        )

    status_tampered = tmp_path / "recovered-status-tampered"
    shutil.copytree(output, status_tampered)
    status_payload = json.loads((status_tampered / "recovery_receipt.json").read_text())
    status_payload.pop("recovery_receipt_id")
    status_payload["lean_attempts"][-1]["status"] = "timeout"
    candidate_pipeline = [
        item
        for item in status_payload["pipeline_attempts"]
        if item["stage"] == "candidate_validation"
    ]
    candidate_pipeline[-1]["status"] = "timeout"
    status_receipt = build_recovery_receipt(**status_payload)
    (status_tampered / "recovery_receipt.json").write_bytes(_line(status_receipt))
    with pytest.raises(ProvisionalPairCombineError, match="materialized recovery"):
        combine_provisional_pair_roots(
            materialization_roots=(status_tampered,),
            output_dir=tmp_path / "combined-status-tampered",
        )

    request_tampered = tmp_path / "recovered-request-tampered"
    shutil.copytree(output, request_tampered)
    request_payload = json.loads((request_tampered / "recovery_receipt.json").read_text())
    request_payload.pop("recovery_receipt_id")
    forged_hash = "f" * 64
    request_payload["lean_attempts"][0]["request_hash"] = forged_hash
    first_candidate = next(
        item
        for item in request_payload["pipeline_attempts"]
        if item["stage"] == "candidate_validation"
    )
    first_candidate["request_hash"] = forged_hash
    request_receipt = build_recovery_receipt(**request_payload)
    (request_tampered / "recovery_receipt.json").write_bytes(_line(request_receipt))
    with pytest.raises(ProvisionalPairCombineError, match="differs from its raw request"):
        combine_provisional_pair_roots(
            materialization_roots=(request_tampered,),
            output_dir=tmp_path / "combined-request-tampered",
        )

    tampered = output / "recovery_receipt.json"
    tampered.write_bytes(tampered.read_bytes().replace(b"v4.31.0-rc1", b"v4.30.0    ", 1))
    with pytest.raises(ProvisionalPairCombineError):
        combine_provisional_pair_roots(
            materialization_roots=(output,),
            output_dir=tmp_path / "combined-tampered",
        )


def test_repeated_infrastructure_failure_publishes_no_recovered_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = _make_parent(monkeypatch, tmp_path)
    failed = V2E2MaterializationResult.model_validate_json(
        (parent / "results.jsonl").read_text().splitlines()[0]
    )

    def factory(settings: BackendSettings) -> LeanInteractBackend:
        return cast(
            LeanInteractBackend,
            _RecoveryBackend(settings, (LeanStatus.TIMEOUT, LeanStatus.CRASH)),
        )

    output = tmp_path / "still-failed"
    with pytest.raises(V2E2RecoveryError, match="remained an infrastructure failure"):
        recover_v2_e2_attempt(
            parent_root=parent,
            output_dir=output,
            repo_root=_REPO,
            import_header=_IMPORT,
            target_result_line_number=1,
            target_result_id=failed.result_id,
            backend_factory=factory,
            toolchain_probe=_toolchain,
        )
    assert not output.exists()
    failures = tuple(tmp_path.glob(".still-failed.failed-*"))
    assert len(failures) == 1
    assert (failures[0] / "failure_receipt.json").is_file()


def test_representation_infrastructure_failure_publishes_no_recovered_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_e2_scale as scale_module

    real_build_representations = scale_module.build_representations
    parent = _make_parent(monkeypatch, tmp_path)
    monkeypatch.setattr(scale_module, "build_representations", real_build_representations)
    failed = V2E2MaterializationResult.model_validate_json(
        (parent / "results.jsonl").read_text().splitlines()[0]
    )
    created: list[_RecoveryBackend] = []

    def factory(settings: BackendSettings) -> LeanInteractBackend:
        backend = _RecoveryBackend(
            settings,
            (LeanStatus.VALID_WITH_SORRY, *(LeanStatus.TIMEOUT for _ in range(16))),
        )
        created.append(backend)
        return cast(LeanInteractBackend, backend)

    output = tmp_path / "representation-still-failed"
    with pytest.raises(V2E2RecoveryError, match="representation remained an infrastructure"):
        recover_v2_e2_attempt(
            parent_root=parent,
            output_dir=output,
            repo_root=_REPO,
            import_header=_IMPORT,
            target_result_line_number=1,
            target_result_id=failed.result_id,
            backend_factory=factory,
            toolchain_probe=_toolchain,
        )
    assert not output.exists()
    assert created[0].reset_count >= 1
    failures = tuple(tmp_path.glob(".representation-still-failed.failed-*"))
    assert len(failures) == 1
    failure_payload = json.loads((failures[0] / "failure_receipt.json").read_text())
    assert failure_payload["failed_stage"] == "candidate_representation"


def test_malformed_backend_raw_response_cannot_publish_a_recovered_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = _make_parent(monkeypatch, tmp_path)
    failed = V2E2MaterializationResult.model_validate_json(
        (parent / "results.jsonl").read_text().splitlines()[0]
    )

    class MismatchedRawBackend(_RecoveryBackend):
        def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
            results = super().run_batch(requests)
            raw_path = Path(cast(str, results[0].raw_response_path))
            raw_payload = json.loads(raw_path.read_text())
            raw_payload["request_hash"] = "e" * 64
            raw_path.write_text(json.dumps(raw_payload), encoding="utf-8")
            return results

    output = tmp_path / "malformed-raw-output"
    with pytest.raises(V2E2RecoveryError, match="failed final combiner validation"):
        recover_v2_e2_attempt(
            parent_root=parent,
            output_dir=output,
            repo_root=_REPO,
            import_header=_IMPORT,
            target_result_line_number=1,
            target_result_id=failed.result_id,
            backend_factory=lambda settings: cast(
                LeanInteractBackend,
                MismatchedRawBackend(settings, (LeanStatus.VALID_WITH_SORRY,)),
            ),
            toolchain_probe=_toolchain,
        )
    assert not output.exists()


def test_default_combiner_still_rejects_unrecovered_infrastructure_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = _make_parent(monkeypatch, tmp_path)
    with pytest.raises(ProvisionalPairCombineError, match="infrastructure-error"):
        combine_provisional_pair_roots(
            materialization_roots=(parent,),
            output_dir=tmp_path / "combined-parent",
        )


def test_recovery_rejects_wrong_selector_nested_output_and_second_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = _make_parent(monkeypatch, tmp_path)
    failed = V2E2MaterializationResult.model_validate_json(
        (parent / "results.jsonl").read_text().splitlines()[0]
    )

    with pytest.raises(V2E2RecoveryError, match="cannot be nested"):
        recover_v2_e2_attempt(
            parent_root=parent,
            output_dir=parent / "nested",
            repo_root=_REPO,
            import_header=_IMPORT,
            target_result_line_number=1,
            target_result_id=failed.result_id,
            backend_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
            toolchain_probe=_toolchain,
        )
    with pytest.raises(V2E2RecoveryError, match="not unique at the requested line"):
        recover_v2_e2_attempt(
            parent_root=parent,
            output_dir=tmp_path / "wrong-line",
            repo_root=_REPO,
            import_header=_IMPORT,
            target_result_line_number=2,
            target_result_id=failed.result_id,
            backend_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
            toolchain_probe=_toolchain,
        )

    _mark_infrastructure(parent, result_index=1)
    with pytest.raises(V2E2RecoveryError, match="failed full validation"):
        recover_v2_e2_attempt(
            parent_root=parent,
            output_dir=tmp_path / "second-infrastructure",
            repo_root=_REPO,
            import_header=_IMPORT,
            target_result_line_number=1,
            target_result_id=failed.result_id,
            backend_factory=lambda _settings: (_ for _ in ()).throw(AssertionError()),
            toolchain_probe=_toolchain,
        )
