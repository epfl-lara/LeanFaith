from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.corpus2.meta_slice2 import (
    AUDIT_DRIVER_FILENAME,
    AUDIT_STDOUT_FILENAME,
    DRIVER_FILENAME,
    MANIFEST_FILENAME,
    NAMES_FILENAME,
    STDOUT_FILENAME,
    ExecutionResult,
    MetaSlice2Config,
    MetaSlice2Error,
    run_meta_slice2,
    select_declarations,
    verify_meta_slice2,
)

_REVISION = "1" * 40


@dataclass
class _FakeExecutor:
    outputs: list[str]
    calls: list[tuple[tuple[str, ...], Path, int, Path, Path]] = field(default_factory=list)

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> ExecutionResult:
        self.calls.append((command, cwd, timeout_seconds, stdout_path, stderr_path))
        stdout_path.write_text(self.outputs.pop(0), encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ExecutionResult(returncode=0, timed_out=False, elapsed_seconds=0.25)


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


def _fixture_config(tmp_path: Path, *, output_name: str = "output") -> MetaSlice2Config:
    extraction = tmp_path / "extraction"
    extraction.mkdir(exist_ok=True)
    theorem_store = extraction / "mathlib.jsonl"
    rows = [
        _theorem_row("Fixture.alpha"),
        _theorem_row("Fixture.beta"),
        _theorem_row("Fixture.gamma"),
        _theorem_row("Fixture.delta"),
        _theorem_row("Fixture.alpha"),
        _theorem_row("_private.Hidden.secret"),
        _theorem_row("_private.Hidden.ineligible", eligible=False),
        _theorem_row("Fixture.ineligible", eligible=False),
    ]
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
        sample_size=3,
        timeout_seconds=19,
        address_space_bytes=123_456,
        lean_memory_mb=321,
        enforce_production_bindings=False,
        enforce_storage_root=False,
        verify_mathlib_revision=False,
    )


def _candidate(
    declaration: str,
    *,
    corrupt_site_hash: bool = False,
    corrupt_residual_hash: bool = False,
) -> dict[str, object]:
    source = "True"
    candidate = "(fun x : Prop => x) True"
    source_site = "Fixture.wrapper True"
    candidate_site = "True"
    source_site_hash = hashlib.sha256(source_site.encode()).hexdigest()
    candidate_site_hash = hashlib.sha256(candidate_site.encode()).hexdigest()
    if corrupt_site_hash:
        source_site_hash = "0" * 64
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
            "residualHash": "f" * 64 if corrupt_residual_hash else candidate_site_hash,
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


def _p21_candidate(declaration: str, *, corrupt_direction: bool = False) -> dict[str, object]:
    row = _candidate(declaration)
    source_site_hash = str(row["sourceSiteHash"])
    candidate_site_hash = str(row["candidateSiteHash"])
    row.update(
        {
            "family": "P21",
            "operation": "betaIntroduce",
            "operationKind": "betaIntroduce",
            "evidence": {
                "relation": "definitionalEquality",
                "wholeTypeDefEqRequired": True,
                "redexKind": "beta",
                "redexCount": 1,
                "contextReconstructed": True,
            },
            "witness": {
                "sourceSiteHash": source_site_hash,
                "candidateSiteHash": candidate_site_hash,
                "residualHash": source_site_hash,
                "direction": "eliminate" if corrupt_direction else "introduce",
                "residualRule": "instantiate1",
                "captureFreeByKernelSubstitution": True,
            },
        }
    )
    return row


def _terminal(declaration: str, *, emitted: int) -> dict[str, object]:
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
        "discoveredCount": emitted,
        "pathCount": 4,
    }


def _output_with_candidate(
    config: MetaSlice2Config,
    candidate: dict[str, object],
) -> str:
    selection = select_declarations(config)
    rows: list[dict[str, object]] = [
        candidate,
        _terminal(selection.names[0], emitted=1),
    ]
    rows.extend(_terminal(name, emitted=0) for name in selection.names[1:])
    rows.append(
        {
            "schemaVersion": 2,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "namesFile": str((config.output_root / NAMES_FILENAME).resolve()),
            "declarationCount": len(selection.names),
            "completedCount": len(selection.names),
            "failedCount": 0,
        }
    )
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _primary_output(
    config: MetaSlice2Config,
    *,
    corrupt_site_hash: bool = False,
    corrupt_residual_hash: bool = False,
) -> str:
    declaration = select_declarations(config).names[0]
    return _output_with_candidate(
        config,
        _candidate(
            declaration,
            corrupt_site_hash=corrupt_site_hash,
            corrupt_residual_hash=corrupt_residual_hash,
        ),
    )


def _audit_output(config: MetaSlice2Config) -> str:
    declaration = select_declarations(config).names[0]
    candidate = _candidate(declaration)
    candidate_hash = str(candidate["candidateTypeHash"])
    return (
        json.dumps(
            {
                "schemaVersion": 2,
                "kind": "audit",
                "recordKind": "audit",
                "declaration": declaration,
                "family": "P20",
                "operation": "unfold:Fixture.wrapper",
                "sitePath": "/1",
                "expectedCandidateTypeHash": candidate_hash,
                "actualCandidateTypeHash": candidate_hash,
                "verified": True,
                "inverseFoldVerified": True,
                "status": "verified",
                "reason": "verified",
                "auditMode": "independent-site-reconstruction",
            },
            sort_keys=True,
        )
        + "\n"
    )


def _zero_candidate_output(config: MetaSlice2Config) -> str:
    selection = select_declarations(config)
    rows = [_terminal(name, emitted=0) for name in selection.names]
    rows.append(
        {
            "schemaVersion": 2,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "namesFile": str((config.output_root / NAMES_FILENAME).resolve()),
            "declarationCount": len(selection.names),
            "completedCount": len(selection.names),
            "failedCount": 0,
        }
    )
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _dishonest_terminal_output(config: MetaSlice2Config) -> str:
    selection = select_declarations(config)
    first, *rest = selection.names
    rows: list[dict[str, object]] = [
        {
            "schemaVersion": 2,
            "kind": "terminal",
            "recordKind": "status",
            "declaration": first,
            "status": "notfound",
            "candidateCount": 0,
            "emittedCount": 0,
            "duplicateCount": 0,
            "rejectedCount": 0,
            "notfound": True,
        }
    ]
    rows.extend(_terminal(name, emitted=0) for name in rest)
    rows.append(
        {
            "schemaVersion": 2,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "namesFile": str((config.output_root / NAMES_FILENAME).resolve()),
            "declarationCount": len(selection.names),
            "completedCount": len(selection.names),
            "failedCount": 0,
        }
    )
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _late_candidate_output(config: MetaSlice2Config) -> str:
    selection = select_declarations(config)
    rows: list[dict[str, object]] = [
        _terminal(selection.names[0], emitted=1),
        _candidate(selection.names[0]),
    ]
    rows.extend(_terminal(name, emitted=0) for name in selection.names[1:])
    rows.append(
        {
            "schemaVersion": 2,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "namesFile": str((config.output_root / NAMES_FILENAME).resolve()),
            "declarationCount": len(selection.names),
            "completedCount": len(selection.names),
            "failedCount": 0,
        }
    )
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def test_selector_is_unique_deterministic_and_private_first(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    selection = select_declarations(config)
    eligible = {"Fixture.alpha", "Fixture.beta", "Fixture.gamma", "Fixture.delta"}
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
    assert selection.names == expected
    assert selection.theorem_rows == 8
    assert selection.eligible_rows == 5
    assert selection.eligible_unique_names == 4
    assert selection.duplicate_eligible_names == 1
    assert selection.excluded_private == 2
    assert selection.excluded_transform_ineligible == 1


def test_run_and_verify_with_independent_fake_audit(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    executor = _FakeExecutor([_primary_output(config), _audit_output(config)])

    manifest = run_meta_slice2(config, executor=executor)

    assert manifest["status"] == "completed"
    summary = verify_meta_slice2(config)
    assert summary["total_candidate_count"] == 1
    assert summary["per_family_counts"] == {"P20": 1}
    assert summary["per_operation_counts"] == {"unfold": 1}
    assert summary["nested_candidates"] == {"count": 1, "share": 1.0}
    assert summary["declaration_coverage"] == {
        "with_candidate": 1,
        "without_candidate": 2,
        "share": 1 / 3,
    }
    assert summary["candidate_count_distribution"] == {
        "mean": 1 / 3,
        "median": 0.0,
        "p95": 1,
        "max": 1,
    }
    assert summary["independent_audit"] == {
        "mode": "independent-site-reconstruction",
        "requested_count": 1,
        "verified_count": 1,
        "failed_count": 0,
        "coverage": 1.0,
    }
    assert len(executor.calls) == 2
    primary_command = executor.calls[0][0]
    audit_command = executor.calls[1][0]
    assert primary_command[:3] == ("/usr/bin/prlimit", "--as=123456", "--")
    assert primary_command[3:8] == ("lake", "env", "lean", "-M321", "-j1")
    assert primary_command[-1].endswith(DRIVER_FILENAME)
    assert audit_command[-1].endswith(AUDIT_DRIVER_FILENAME)
    assert executor.calls[0][2] == executor.calls[1][2] == 19
    assert (config.output_root / AUDIT_STDOUT_FILENAME).is_file()
    audit_driver_text = (config.output_root / AUDIT_DRIVER_FILENAME).read_text()
    audit_commands = [
        line for line in audit_driver_text.splitlines() if line.startswith("lfAuditTransform ")
    ]
    certificate = _candidate(select_declarations(config).names[0])
    assert audit_commands == [
        "lfAuditTransform "
        f"{json.dumps(certificate['declaration'])} "
        '"P20" "unfold:Fixture.wrapper" "/1" '
        f"{json.dumps(certificate['candidateTypeHash'])}"
    ]
    assert not list(config.output_root.glob("*.partial"))


def test_bad_local_site_hash_fails_closed_and_records_failure(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="bad-hash")
    executor = _FakeExecutor([_primary_output(config, corrupt_site_hash=True)])

    with pytest.raises(MetaSlice2Error, match="sourceSiteHash failed independent"):
        run_meta_slice2(config, executor=executor)

    manifest = json.loads((config.output_root / MANIFEST_FILENAME).read_text())
    assert manifest["status"] == "failure"
    assert manifest["failure"]["type"] == "MetaSlice2Error"
    assert (config.output_root / STDOUT_FILENAME).is_file()
    assert not (config.output_root / AUDIT_STDOUT_FILENAME).exists()


def test_bad_p20_residual_certificate_fails_closed(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="bad-p20-residual")
    executor = _FakeExecutor([_primary_output(config, corrupt_residual_hash=True)])

    with pytest.raises(MetaSlice2Error, match="inverse-fold certificate"):
        run_meta_slice2(config, executor=executor)


def test_bad_p21_operation_witness_fails_closed(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="bad-p21-witness")
    declaration = select_declarations(config).names[0]
    stdout = _output_with_candidate(
        config,
        _p21_candidate(declaration, corrupt_direction=True),
    )
    executor = _FakeExecutor([stdout])

    with pytest.raises(MetaSlice2Error, match="beta/zeta residual certificate"):
        run_meta_slice2(config, executor=executor)


def test_zero_candidate_probe_still_runs_vacuous_independent_audit(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="zero")
    executor = _FakeExecutor([_zero_candidate_output(config), ""])

    manifest = run_meta_slice2(config, executor=executor)

    summary = manifest["summary"]
    assert isinstance(summary, dict)
    assert summary["total_candidate_count"] == 0
    assert summary["independent_audit"] == {
        "mode": "independent-site-reconstruction",
        "requested_count": 0,
        "verified_count": 0,
        "failed_count": 0,
        "coverage": 1.0,
    }
    assert len(executor.calls) == 2
    audit_driver = (config.output_root / AUDIT_DRIVER_FILENAME).read_text()
    assert 'elab "lfAuditTransform ' in audit_driver
    assert not audit_driver.rstrip().endswith('lfAuditTransform "" "" "" "" ""')


def test_batch_counts_must_reconcile_with_terminal_statuses(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="dishonest-batch")
    executor = _FakeExecutor([_dishonest_terminal_output(config)])

    with pytest.raises(MetaSlice2Error, match="contradict per-declaration terminal"):
        run_meta_slice2(config, executor=executor)

    manifest = json.loads((config.output_root / MANIFEST_FILENAME).read_text())
    assert manifest["status"] == "failure"


def test_candidate_after_its_terminal_fails_closed(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="late-candidate")
    executor = _FakeExecutor([_late_candidate_output(config)])

    with pytest.raises(MetaSlice2Error, match="after its declaration terminal"):
        run_meta_slice2(config, executor=executor)


def test_completed_output_tampering_is_detected(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, output_name="tamper")
    executor = _FakeExecutor([_primary_output(config), _audit_output(config)])
    run_meta_slice2(config, executor=executor)

    with (config.output_root / STDOUT_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(MetaSlice2Error, match="output artifact drift"):
        verify_meta_slice2(config)
