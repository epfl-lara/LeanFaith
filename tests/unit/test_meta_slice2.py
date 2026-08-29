from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import leanfaith.corpus2.meta_slice2 as meta_slice2_module
from leanfaith.config.hashing import hash_file
from leanfaith.corpus2.meta_slice2 import (
    ATTEMPT_CERTIFICATES_FILENAME,
    ATTEMPT_DRIVER_FILENAME,
    ATTEMPT_NAMES_FILENAME,
    ATTEMPT_PROCESS_FILENAME,
    ATTEMPT_RESULT_FILENAME,
    AUDIT_SHARD_SIZE,
    AUDIT_STDOUT_FILENAME,
    MANIFEST_FILENAME,
    METHOD_VERSION,
    PRIMARY_SHARD_SIZE,
    PRODUCTION_THEOREM_STORE,
    SELECTION_DOMAIN,
    STDOUT_FILENAME,
    ExecutionResult,
    MetaSlice2Config,
    MetaSlice2Error,
    production_config,
    run_meta_slice2,
    select_declarations,
    verify_meta_slice2,
)

_REVISION = "1" * 40


@dataclass(frozen=True)
class _FakeResponse:
    stdout: str
    returncode: int | None = 0
    timed_out: bool = False


_Handler = Callable[[Path, int], _FakeResponse]


@dataclass
class _FakeExecutor:
    handler: _Handler
    interrupt_at: int | None = None
    calls: list[tuple[tuple[str, ...], Path, int, Path, Path]] = field(default_factory=list)

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        process_state_path: Path,
        attempt_id: str,
    ) -> ExecutionResult:
        del process_state_path, attempt_id
        call_number = len(self.calls)
        self.calls.append((command, cwd, timeout_seconds, stdout_path, stderr_path))
        if self.interrupt_at == call_number:
            raise KeyboardInterrupt
        response = self.handler(stdout_path, call_number)
        stdout_path.write_text(response.stdout, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutionResult(
            returncode=response.returncode,
            timed_out=response.timed_out,
            elapsed_seconds=0.25,
        )


@dataclass
class _OrphaningExecutor:
    process: subprocess.Popen[bytes] | None = None
    reaper: threading.Thread | None = None

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        process_state_path: Path,
        attempt_id: str,
    ) -> ExecutionResult:
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        live_command = (
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            command[-1],
        )
        process = subprocess.Popen(
            live_command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.process = process
        process_group_id, start_ticks = meta_slice2_module._process_stat(process.pid)
        meta_slice2_module._write_json_atomic(
            process_state_path,
            meta_slice2_module._process_state_payload(
                attempt_id=attempt_id,
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                started_at=meta_slice2_module._utc_now(),
                phase="running",
                pid=process.pid,
                process_group_id=process_group_id,
                process_start_ticks=start_ticks,
                boot_id=meta_slice2_module._boot_id(),
                returncode=None,
                timed_out=False,
                interrupted=False,
                term_sent=False,
                kill_sent=False,
                group_gone=False,
            ),
        )
        self.reaper = threading.Thread(target=process.wait, daemon=True)
        self.reaper.start()
        raise KeyboardInterrupt


def _theorem_row(name: str, *, eligible: bool = True) -> dict[str, object]:
    return {
        "theorem": {
            "theorem_id": f"thm:{hashlib.sha256(name.encode()).hexdigest()}",
            "declaration_full_name": name,
            "source": "mathlib",
            "source_revision": _REVISION,
            "is_proposition": True,
            "elaboration_status": "elaborates",
            "metadata": {"transform_source_eligible": eligible},
        }
    }


def _fixture_config(
    tmp_path: Path,
    *,
    output_name: str = "output",
    sample_size: int = 3,
) -> MetaSlice2Config:
    extraction = tmp_path / "extraction"
    extraction.mkdir(exist_ok=True)
    theorem_store = extraction / "mathlib.jsonl"
    public_names = [f"Fixture.name{index:02d}" for index in range(max(sample_size + 2, 6))]
    rows = [*(_theorem_row(name) for name in public_names)]
    rows.extend(
        (
            _theorem_row(public_names[0]),
            _theorem_row("_private.Hidden.secret"),
            _theorem_row("_private.Hidden.ineligible", eligible=False),
            _theorem_row("Fixture.ineligible", eligible=False),
        )
    )
    theorem_store.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    theorem_hash = hash_file(theorem_store)
    extraction_manifest = extraction / "manifest.json"
    extraction_manifest.write_text(
        json.dumps(
            {
                "artifact_class": "production",
                "stage": "elaborated",
                "source": "mathlib",
                "source_revision": _REVISION,
                "row_count": len(rows),
                "output_partition_checksums": {"fixture/theorems/mathlib.jsonl": theorem_hash},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    engine = tmp_path / "TransformEngine.lean"
    engine.write_text(
        """import Lean
import Lean.Meta.Tactic.Delta
namespace LeanFaith.Meta.TransformEngineHelper
elab "lfTransformBatch " s:str : command => pure ()
elab "lfAuditTransform " d:str f:str o:str p:str h:str : command => pure ()
end LeanFaith.Meta.TransformEngineHelper
""",
        encoding="utf-8",
    )
    mathlib = tmp_path / "mathlib4"
    mathlib.mkdir(exist_ok=True)
    return MetaSlice2Config(
        output_root=tmp_path / output_name,
        theorem_store_path=theorem_store,
        theorem_store_sha256=theorem_hash,
        extraction_manifest_path=extraction_manifest,
        extraction_manifest_sha256=hash_file(extraction_manifest),
        transform_engine_path=engine,
        mathlib_project_path=mathlib,
        expected_source="mathlib",
        expected_source_revision=_REVISION,
        sample_size=sample_size,
        timeout_seconds=19,
        address_space_bytes=123_456,
        lean_memory_mb=321,
        enforce_production_bindings=False,
        enforce_storage_root=False,
        verify_mathlib_revision=False,
    )


def _candidate(declaration: str) -> dict[str, object]:
    source = "True"
    candidate = "(fun x : Prop => x) True"
    source_site = "Fixture.wrapper True"
    candidate_site = "True"
    source_site_hash = hashlib.sha256(source_site.encode()).hexdigest()
    candidate_site_hash = hashlib.sha256(candidate_site.encode()).hexdigest()
    return {
        "schemaVersion": 2,
        "kind": "candidate",
        "recordKind": "candidate",
        "declaration": declaration,
        "family": "P20",
        "operation": "unfold:Fixture.wrapper",
        "operationKind": "unfold",
        "sitePath": "/1",
        "binderDepth": 0,
        "nestedSite": True,
        "source": source,
        "candidate": candidate,
        "sourcePretty": source,
        "candidatePretty": candidate,
        "sourceSite": source_site,
        "candidateSite": candidate_site,
        "sourceTypeHash": hashlib.sha256(source.encode()).hexdigest(),
        "candidateTypeHash": hashlib.sha256(candidate.encode()).hexdigest(),
        "sourceSiteHash": source_site_hash,
        "candidateSiteHash": candidate_site_hash,
        "evidenceClass": "P-DEF",
        "evidence": {
            "relation": "definitionalEquality",
            "wholeTypeDefEqRequired": True,
            "deltaSteps": 1,
            "safeDefinition": True,
            "transparentDefinition": True,
            "typedSubterm": True,
            "contextReconstructed": True,
            "inverseFoldCertified": True,
        },
        "axioms": "none",
        "candidateElaborates": True,
        "wholeTypeDefEq": True,
        "witness": {
            "sourceSiteHash": source_site_hash,
            "candidateSiteHash": candidate_site_hash,
            "residualHash": candidate_site_hash,
            "constant": "Fixture.wrapper",
            "universeArguments": [],
            "arguments": ["True"],
            "argumentBinderInfo": ["default"],
            "argumentCount": 1,
            "reducibility": "reducible",
            "definitionSafety": "safe",
            "inverseOperation": "fold",
            "inverseUsesPreservedApplication": True,
            "foldSearch": False,
            "unfoldResidualStructuralMatch": True,
        },
        "status": "ok",
    }


def _terminal(declaration: str, *, emitted: int = 0) -> dict[str, object]:
    source = "True"
    return {
        "schemaVersion": 2,
        "kind": "terminal",
        "recordKind": "status",
        "declaration": declaration,
        "status": "complete",
        "candidateCount": emitted,
        "emittedCount": emitted,
        "duplicateCount": 0,
        "rejectedCount": 0,
        "source": source,
        "sourceTypeHash": hashlib.sha256(source.encode()).hexdigest(),
        "sourceTextRoundtripVerified": True,
        "discoveredCount": emitted,
        "pathCount": 4,
        "error": None,
    }


def _engine_terminal(declaration: str, status: str) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "kind": "terminal",
        "recordKind": "status",
        "declaration": declaration,
        "status": status,
        "candidateCount": 0,
        "emittedCount": 0,
        "duplicateCount": 0,
        "rejectedCount": 0,
        "error": "fixture failure" if status == "error" else None,
    }


def _source_text_rejected_terminal(declaration: str) -> dict[str, object]:
    source = "True"
    return {
        "schemaVersion": 2,
        "kind": "terminal",
        "recordKind": "status",
        "declaration": declaration,
        "status": "sourceTextRejected",
        "candidateCount": 0,
        "emittedCount": 0,
        "duplicateCount": 0,
        "rejectedCount": 0,
        "source": source,
        "sourceTypeHash": hashlib.sha256(source.encode()).hexdigest(),
        "sourceTextRoundtripVerified": False,
        "reasonCode": "source_pretty_roundtrip_mismatch",
        "error": None,
    }


def _primary_output(
    names_path: Path,
    *,
    candidate_declaration: str | None = None,
    candidate_declarations: frozenset[str] = frozenset(),
    engine_status: tuple[str, str] | None = None,
    source_text_rejected: str | None = None,
) -> str:
    names = tuple(line for line in names_path.read_text(encoding="utf-8").splitlines() if line)
    rows: list[dict[str, object]] = []
    failed_count = 0
    for declaration in names:
        if declaration == source_text_rejected:
            rows.append(_source_text_rejected_terminal(declaration))
            continue
        if engine_status is not None and declaration == engine_status[0]:
            rows.append(_engine_terminal(declaration, engine_status[1]))
            failed_count += 1
            continue
        emitted = int(declaration == candidate_declaration or declaration in candidate_declarations)
        if emitted:
            rows.append(_candidate(declaration))
        rows.append(_terminal(declaration, emitted=emitted))
    rows.append(
        {
            "schemaVersion": 2,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete" if failed_count == 0 else "partial",
            "namesFile": str(names_path.resolve()),
            "declarationCount": len(names),
            "completedCount": len(names) - failed_count,
            "failedCount": failed_count,
        }
    )
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _audit_output(certificates_path: Path) -> str:
    rows: list[dict[str, object]] = []
    for raw_line in certificates_path.read_text(encoding="utf-8").splitlines():
        certificate = json.loads(raw_line)
        expected_hash = certificate["candidate_type_hash"]
        rows.append(
            {
                "schemaVersion": 2,
                "kind": "audit",
                "recordKind": "audit",
                "declaration": certificate["declaration"],
                "family": certificate["family"],
                "operation": certificate["operation"],
                "sitePath": certificate["site_path"],
                "expectedCandidateTypeHash": expected_hash,
                "actualCandidateTypeHash": expected_hash,
                "verified": True,
                "inverseFoldVerified": certificate["family"] == "P20",
                "status": "verified",
                "reason": "verified",
                "auditMode": "independent-site-reconstruction",
            }
        )
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _success_handler(candidate_declaration: str | None = None) -> _Handler:
    def handle(stdout_path: Path, _call_number: int) -> _FakeResponse:
        attempt_dir = stdout_path.parent
        if attempt_dir.name.startswith("attempt-primary-"):
            return _FakeResponse(
                _primary_output(
                    attempt_dir / ATTEMPT_NAMES_FILENAME,
                    candidate_declaration=candidate_declaration,
                )
            )
        return _FakeResponse(_audit_output(attempt_dir / ATTEMPT_CERTIFICATES_FILENAME))

    return handle


def _result_files(config: MetaSlice2Config, stage: str) -> list[Path]:
    return sorted(
        (config.output_root / "shards").glob(f"attempt-{stage}-*/{ATTEMPT_RESULT_FILENAME}")
    )


def test_selector_is_unique_deterministic_and_selection_domain_is_unchanged(
    tmp_path: Path,
) -> None:
    config = _fixture_config(tmp_path)
    selection = select_declarations(config)
    eligible = {f"Fixture.name{index:02d}" for index in range(6)}
    expected = tuple(
        sorted(
            eligible,
            key=lambda name: (
                hashlib.sha256(
                    b"leanfaith_meta_slice2_yield_probe_v1\0" + name.encode()
                ).hexdigest(),
                name,
            ),
        )[:3]
    )
    assert SELECTION_DOMAIN == "leanfaith_meta_slice2_yield_probe_v1"
    assert METHOD_VERSION.endswith("_v3")
    assert PRIMARY_SHARD_SIZE == 20
    assert AUDIT_SHARD_SIZE == 100
    assert selection.names == expected
    assert selection.eligible_rows == 7
    assert selection.eligible_unique_names == 6
    assert selection.duplicate_eligible_names == 1
    assert selection.excluded_private == 2
    assert selection.excluded_transform_ineligible == 1


@pytest.mark.skipif(
    not PRODUCTION_THEOREM_STORE.is_file(),
    reason="frozen production extraction is unavailable",
)
def test_production_500_name_selection_hash_is_unchanged() -> None:
    config = production_config(Path("/storage/milikic/leanfaith/meta_slice2_selection_test"))
    selection = select_declarations(config)
    assert len(selection.names) == 500
    assert (
        hashlib.sha256("".join(f"{name}\n" for name in selection.names).encode()).hexdigest()
        == "1230b5bab24c2a55a4d3991f838aca8dab35adb75577c7eddd34d17b2f86f76c"
    )


def test_sharded_success_summary_manifest_and_replay(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    candidate_declaration = select_declarations(config).names[0]
    executor = _FakeExecutor(_success_handler(candidate_declaration))

    manifest = run_meta_slice2(config, executor=executor)
    summary = verify_meta_slice2(config)

    assert manifest["status"] == "completed"
    assert summary["selected_declaration_count"] == 3
    assert summary["terminal_declaration_count"] == 3
    assert summary["total_candidate_count"] == 1
    assert summary["per_family_counts"] == {"P20": 1}
    assert summary["declaration_coverage"] == {
        "with_candidate": 1,
        "without_candidate": 2,
        "share": 1 / 3,
    }
    assert summary["independent_audit"] == {
        "mode": "independent-site-reconstruction",
        "requested_count": 1,
        "verified_count": 1,
        "failed_count": 0,
        "coverage": 1.0,
    }
    assert summary["execution_attempts"] == {
        "total": 2,
        "by_stage": {"audit": 1, "primary": 1},
        "by_outcome": {"accepted": 2},
    }
    assert len(executor.calls) == 2
    for command, _, timeout, _, _ in executor.calls:
        assert command[:3] == ("/usr/bin/prlimit", "--as=123456", "--")
        assert command[3:8] == ("lake", "env", "lean", "-M321", "-j1")
        assert command[-1].endswith(ATTEMPT_DRIVER_FILENAME)
        assert timeout == 19
    attempt = json.loads(_result_files(config, "primary")[0].read_text())
    assert attempt["range"] == {"start": 0, "stop": 3}
    assert attempt["input"]["sha256"] == hash_file(
        _result_files(config, "primary")[0].parent / ATTEMPT_NAMES_FILENAME
    )
    assert set(attempt["artifacts"]) == {
        ATTEMPT_NAMES_FILENAME,
        ATTEMPT_DRIVER_FILENAME,
        "stdout.jsonl",
        "stderr.txt",
        "log.txt",
        ATTEMPT_PROCESS_FILENAME,
    }
    assert not list(config.output_root.rglob("*.partial"))


def test_invalid_parent_output_bisects_contiguous_range(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="bisect", sample_size=4)

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        attempt_dir = stdout_path.parent
        names_path = attempt_dir / ATTEMPT_NAMES_FILENAME
        names = names_path.read_text(encoding="utf-8").splitlines()
        if len(names) == 4:
            return _FakeResponse("not-json\n")
        return _FakeResponse(_primary_output(names_path))

    manifest = run_meta_slice2(config, executor=_FakeExecutor(handler))

    assert manifest["status"] == "completed"
    results = [json.loads(path.read_text()) for path in _result_files(config, "primary")]
    assert {(row["range"]["start"], row["range"]["stop"]) for row in results} == {
        (0, 4),
        (0, 2),
        (2, 4),
    }
    assert {row["outcome"] for row in results} == {"accepted", "invalid_output"}
    parent = next(row for row in results if row["range"] == {"start": 0, "stop": 4})
    for child in (row for row in results if row is not parent):
        assert child["parent_attempt_id"] == parent["attempt_id"]
    summary = verify_meta_slice2(config)
    assert summary["terminal_declaration_count"] == 4
    attempt_summary = summary["execution_attempts"]
    assert isinstance(attempt_summary, dict)
    assert attempt_summary["by_outcome"] == {
        "accepted": 2,
        "invalid_output": 1,
    }


def test_nonzero_exit_bisects_and_singleton_is_external_process_error(
    tmp_path: Path,
) -> None:
    config = _fixture_config(tmp_path, output_name="nonzero-bisect", sample_size=2)
    failed_declaration = select_declarations(config).names[0]

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        names_path = stdout_path.parent / ATTEMPT_NAMES_FILENAME
        names = names_path.read_text(encoding="utf-8").splitlines()
        if len(names) == 2 or names == [failed_declaration]:
            return _FakeResponse("partial", returncode=17)
        return _FakeResponse(_primary_output(names_path))

    manifest = run_meta_slice2(config, executor=_FakeExecutor(handler))
    summary = manifest["summary"]
    assert isinstance(summary, dict)
    assert summary["terminal_status_counts"] == {
        "complete": 1,
        "externalProcessError": 1,
    }
    runner_rejections = summary["runner_execution_rejections"]
    assert isinstance(runner_rejections, dict)
    disposition = runner_rejections["dispositions"][0]
    assert disposition["reason_code"] == "nonzero_exit"
    assert disposition["returncode"] == 17
    results = [json.loads(path.read_text()) for path in _result_files(config, "primary")]
    assert {(row["range"]["start"], row["range"]["stop"]) for row in results} == {
        (0, 2),
        (0, 1),
        (1, 2),
    }
    assert [row["outcome"] for row in results].count("nonzero_exit") == 2
    verify_meta_slice2(config)


def test_singleton_timeout_becomes_explicit_runner_disposition(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="singleton-timeout", sample_size=3)

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        attempt_dir = stdout_path.parent
        names_path = attempt_dir / ATTEMPT_NAMES_FILENAME
        names = names_path.read_text(encoding="utf-8").splitlines()
        if len(names) == 3:
            return _FakeResponse(
                _primary_output(names_path, candidate_declaration=names[1]),
                returncode=None,
                timed_out=True,
            )
        if len(names) == 1:
            return _FakeResponse("partial output", returncode=None, timed_out=True)
        return _FakeResponse(_primary_output(names_path))

    manifest = run_meta_slice2(config, executor=_FakeExecutor(handler))
    summary = manifest["summary"]
    assert isinstance(summary, dict)
    dispositions = summary["runner_execution_rejections"]
    assert dispositions["count"] == 1
    disposition = dispositions["dispositions"][0]
    assert disposition["reason_code"] == "timeout"
    assert disposition["timeout_seconds"] == 19
    assert disposition["timed_out"] is True
    assert disposition["returncode"] is None
    assert summary["terminal_status_counts"] == {
        "complete": 2,
        "externalTimeout": 1,
    }
    assert summary["declaration_coverage"]["without_candidate"] == 3
    assert summary["total_candidate_count"] == 0
    aggregate_rows = [
        json.loads(line) for line in (config.output_root / STDOUT_FILENAME).read_text().splitlines()
    ]
    runner_row = next(row for row in aggregate_rows if row.get("status") == "externalTimeout")
    assert runner_row["terminalOrigin"] == "runner"
    assert runner_row["reasonCode"] == "timeout"
    assert runner_row["candidateCount"] == runner_row["emittedCount"] == 0
    assert verify_meta_slice2(config) == summary


def test_singleton_invalid_output_maps_to_external_process_error(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="singleton-invalid", sample_size=1)
    manifest = run_meta_slice2(
        config,
        executor=_FakeExecutor(lambda _path, _call: _FakeResponse("not-json\n")),
    )
    summary = manifest["summary"]
    assert isinstance(summary, dict)
    assert summary["terminal_status_counts"] == {"externalProcessError": 1}
    runner_rejections = summary["runner_execution_rejections"]
    assert isinstance(runner_rejections, dict)
    disposition = runner_rejections["dispositions"][0]
    assert disposition["reason_code"] == "invalid_output"
    assert disposition["returncode"] == 0
    verify_meta_slice2(config)


def test_valid_partial_lean_batch_is_accounted_without_retry(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="lean-partial")
    failed = select_declarations(config).names[0]

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        names_path = stdout_path.parent / ATTEMPT_NAMES_FILENAME
        return _FakeResponse(_primary_output(names_path, engine_status=(failed, "notfound")))

    executor = _FakeExecutor(handler)
    manifest = run_meta_slice2(config, executor=executor)
    summary = manifest["summary"]
    assert isinstance(summary, dict)
    assert len(executor.calls) == 1
    assert summary["lean_engine_dispositions"] == {
        "count": 1,
        "dispositions": [{"declaration": failed, "status": "notfound", "error": None}],
    }
    assert summary["terminal_status_counts"] == {"complete": 2, "notfound": 1}
    assert summary["batch"]["status"] == "partial"
    verify_meta_slice2(config)


def test_source_text_rejection_remains_a_distinct_zero_yield_disposition(
    tmp_path: Path,
) -> None:
    config = _fixture_config(tmp_path, output_name="source-text-rejection")
    rejected = select_declarations(config).names[0]

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        return _FakeResponse(
            _primary_output(
                stdout_path.parent / ATTEMPT_NAMES_FILENAME,
                source_text_rejected=rejected,
            )
        )

    summary = run_meta_slice2(config, executor=_FakeExecutor(handler))["summary"]
    assert isinstance(summary, dict)
    assert summary["source_text_rejections"] == {
        "count": 1,
        "reason_code": "source_pretty_roundtrip_mismatch",
        "declarations": [rejected],
    }
    assert summary["runner_execution_rejections"] == {
        "count": 0,
        "dispositions": [],
    }
    assert summary["terminal_status_counts"] == {
        "complete": 2,
        "sourceTextRejected": 1,
    }
    verify_meta_slice2(config)


def test_resume_skips_accepted_primary_and_retries_interrupted_audit(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="resume")
    candidate_declaration = select_declarations(config).names[0]
    interrupted = _FakeExecutor(_success_handler(candidate_declaration), interrupt_at=1)

    with pytest.raises(KeyboardInterrupt):
        run_meta_slice2(config, executor=interrupted)

    manifest = json.loads((config.output_root / MANIFEST_FILENAME).read_text())
    assert manifest["status"] == "failure"
    resumed = _FakeExecutor(_success_handler(candidate_declaration))
    completed = run_meta_slice2(config, executor=resumed)
    assert completed["status"] == "completed"
    assert len(resumed.calls) == 1
    assert resumed.calls[0][3].parent.name.startswith("attempt-audit-")
    audit_results = [json.loads(path.read_text()) for path in _result_files(config, "audit")]
    assert [row["outcome"] for row in audit_results] == ["abandoned", "accepted"]
    assert [row["attempt_ordinal"] for row in audit_results] == [1, 2]

    never = _FakeExecutor(lambda _path, _call: pytest.fail("idempotent run executed Lean"))
    rerun = run_meta_slice2(config, executor=never)
    assert rerun["status"] == "completed"
    assert never.calls == []


def test_resume_rejects_tampered_accepted_attempt_before_execution(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="resume-tamper")
    candidate_declaration = select_declarations(config).names[0]
    interrupted = _FakeExecutor(_success_handler(candidate_declaration), interrupt_at=1)
    with pytest.raises(KeyboardInterrupt):
        run_meta_slice2(config, executor=interrupted)
    primary_stdout = _result_files(config, "primary")[0].parent / "stdout.jsonl"
    with primary_stdout.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    never = _FakeExecutor(lambda _path, _call: pytest.fail("tampered resume executed Lean"))
    with pytest.raises(MetaSlice2Error, match="output artifact drift"):
        run_meta_slice2(config, executor=never)
    assert never.calls == []


def test_resume_terminates_validated_orphan_process_group_before_retry(
    tmp_path: Path,
) -> None:
    config = _fixture_config(tmp_path, output_name="orphan-recovery", sample_size=1)
    orphaning = _OrphaningExecutor()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_meta_slice2(config, executor=orphaning)
        assert orphaning.process is not None
        assert orphaning.process.poll() is None

        resumed = run_meta_slice2(config, executor=_FakeExecutor(_success_handler()))
        assert resumed["status"] == "completed"
        assert orphaning.reaper is not None
        orphaning.reaper.join(timeout=2)
        assert orphaning.process.poll() is not None
        results = [json.loads(path.read_text()) for path in _result_files(config, "primary")]
        assert [row["outcome"] for row in results] == ["abandoned", "accepted"]
        recovered_state = json.loads(
            (_result_files(config, "primary")[0].parent / ATTEMPT_PROCESS_FILENAME).read_text()
        )
        assert recovered_state["phase"] == "recovered"
        assert recovered_state["term_sent"] is True
        assert recovered_state["group_gone"] is True
    finally:
        if orphaning.process is not None and orphaning.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(orphaning.process.pid, signal.SIGKILL)
            orphaning.process.wait(timeout=2)
        if orphaning.reaper is not None:
            orphaning.reaper.join(timeout=2)


def test_audit_singleton_failure_is_fatal_and_never_waived(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="audit-fatal")
    candidate_declaration = select_declarations(config).names[0]

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        attempt_dir = stdout_path.parent
        if attempt_dir.name.startswith("attempt-primary-"):
            return _FakeResponse(
                _primary_output(
                    attempt_dir / ATTEMPT_NAMES_FILENAME,
                    candidate_declaration=candidate_declaration,
                )
            )
        return _FakeResponse("", returncode=None, timed_out=True)

    with pytest.raises(MetaSlice2Error, match="independent audit singleton failed"):
        run_meta_slice2(config, executor=_FakeExecutor(handler))
    manifest = json.loads((config.output_root / MANIFEST_FILENAME).read_text())
    assert manifest["status"] == "failure"
    audit_result = json.loads(_result_files(config, "audit")[0].read_text())
    assert audit_result["outcome"] == "timeout"
    assert not (config.output_root / AUDIT_STDOUT_FILENAME).exists()


def test_audit_failure_bisects_and_reconstructs_all_candidates(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="audit-bisect", sample_size=2)
    declarations = frozenset(select_declarations(config).names)

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        attempt_dir = stdout_path.parent
        if attempt_dir.name.startswith("attempt-primary-"):
            return _FakeResponse(
                _primary_output(
                    attempt_dir / ATTEMPT_NAMES_FILENAME,
                    candidate_declarations=declarations,
                )
            )
        certificates_path = attempt_dir / ATTEMPT_CERTIFICATES_FILENAME
        certificate_count = len(certificates_path.read_text(encoding="utf-8").splitlines())
        if certificate_count == 2:
            return _FakeResponse("invalid audit output\n")
        return _FakeResponse(_audit_output(certificates_path))

    summary = run_meta_slice2(config, executor=_FakeExecutor(handler))["summary"]
    assert isinstance(summary, dict)
    assert summary["independent_audit"] == {
        "mode": "independent-site-reconstruction",
        "requested_count": 2,
        "verified_count": 2,
        "failed_count": 0,
        "coverage": 1.0,
    }
    audit_results = [json.loads(path.read_text()) for path in _result_files(config, "audit")]
    assert {(row["range"]["start"], row["range"]["stop"]) for row in audit_results} == {
        (0, 2),
        (0, 1),
        (1, 2),
    }
    assert [row["outcome"] for row in audit_results].count("invalid_output") == 1
    verify_meta_slice2(config)


def test_completed_recursive_artifact_tampering_is_detected(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="completed-tamper")
    run_meta_slice2(config, executor=_FakeExecutor(_success_handler()))
    result_path = _result_files(config, "primary")[0]
    result = json.loads(result_path.read_text())
    result["item_count"] = 999
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(MetaSlice2Error, match="output artifact inventory drifted"):
        verify_meta_slice2(config)


def test_untracked_root_artifact_is_never_blessed_or_resumed(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="rogue-artifact")

    def handler(stdout_path: Path, _call_number: int) -> _FakeResponse:
        (config.output_root / "rogue.txt").write_text("untracked", encoding="utf-8")
        return _FakeResponse(_primary_output(stdout_path.parent / ATTEMPT_NAMES_FILENAME))

    with pytest.raises(MetaSlice2Error, match="unexpected root output artifact"):
        run_meta_slice2(config, executor=_FakeExecutor(handler))
    never = _FakeExecutor(lambda _path, _call: pytest.fail("rogue resume executed Lean"))
    with pytest.raises(MetaSlice2Error, match="unexpected root output artifact"):
        run_meta_slice2(config, executor=never)
    assert never.calls == []


def test_final_self_verify_failure_leaves_a_resumable_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _fixture_config(tmp_path, output_name="self-verify-resume")
    real_verify = meta_slice2_module.verify_meta_slice2

    def fail_verify(_config: MetaSlice2Config) -> dict[str, object]:
        raise MetaSlice2Error("forced final replay failure")

    monkeypatch.setattr(meta_slice2_module, "verify_meta_slice2", fail_verify)
    with pytest.raises(MetaSlice2Error, match="forced final replay failure"):
        run_meta_slice2(config, executor=_FakeExecutor(_success_handler()))
    failed_manifest = json.loads((config.output_root / MANIFEST_FILENAME).read_text())
    assert failed_manifest["status"] == "failure"
    assert "completed_at" not in failed_manifest
    assert "summary" not in failed_manifest
    assert "shard_plan" not in failed_manifest

    monkeypatch.setattr(meta_slice2_module, "verify_meta_slice2", real_verify)
    never = _FakeExecutor(lambda _path, _call: pytest.fail("resume reran accepted shard"))
    resumed = run_meta_slice2(config, executor=never)
    assert resumed["status"] == "completed"
    assert never.calls == []


def test_run_lock_symlink_is_rejected(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="lock-symlink")
    config.output_root.mkdir()
    target = tmp_path / "lock-target"
    target.write_text("", encoding="utf-8")
    (config.output_root / ".run.lock").symlink_to(target)
    with pytest.raises(MetaSlice2Error, match="run lock must be a regular"):
        run_meta_slice2(config, executor=_FakeExecutor(_success_handler()))
