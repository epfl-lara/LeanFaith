"""Focused LF-020 evidence-collection CLI and artifact-boundary tests."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from leanfaith.cli import collect_evidence
from leanfaith.cli.app import app
from leanfaith.cli.collect_evidence import (
    EvidenceArtifactCatalog,
    EvidenceCacheCatalog,
    EvidenceCollectionArtifacts,
    EvidenceCollectionInputError,
    resolve_artifact_class,
    run_collect_evidence,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.evidence.config import load_evidence_configs
from leanfaith.evidence.pipeline import PairEvidenceResult
from leanfaith.lean.cache import (
    EvidenceCache,
    EvidenceCacheKey,
    compute_evidence_cache_key_hash,
)
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.schemas import (
    ArtifactClass,
    AuditValue,
    CodeState,
    DefeqValue,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceRecord,
    EvidenceTargetKind,
    OutputManifest,
    PairRecord,
    RunManifest,
    ViewStatus,
    make_id,
    read_manifest,
)
from leanfaith.schemas.migrations import CURRENT_RECORD_SCHEMA_VERSION
from tests.unit.record_factories import (
    ANC_A,
    ANC_B,
    THM_A,
    THM_B,
    UTC_NOW,
    context_record,
    pair_record,
    representation_record,
    theorem_record,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_jsonl(path: Path, records: tuple[Any, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records)
    )


def _inputs(tmp_path: Path, *, smoke: bool = True) -> dict[str, Path]:
    context = context_record(
        header_text="import LeanFaithFixtures",
        imports=("LeanFaithFixtures",),
    )
    metadata: dict[str, str | int | float | bool | None] = (
        {"artifact_class": "smoke"} if smoke else {}
    )
    theorem_a = theorem_record(
        theorem_id=THM_A,
        ancestry_id=ANC_A,
        root_ancestry_ids=(ANC_A,),
        declaration_name="evidence_a",
        declaration_full_name="evidence_a",
        proof_stripped_declaration="theorem evidence_a : True := by sorry",
        statement_content_hash="2" * 64,
        metadata=metadata,
    )
    theorem_b = theorem_record(
        theorem_id=THM_B,
        ancestry_id=ANC_B,
        root_ancestry_ids=(ANC_B,),
        declaration_name="evidence_b",
        declaration_full_name="evidence_b",
        proof_stripped_declaration="theorem evidence_b : True := by sorry",
        statement_content_hash="4" * 64,
        metadata=metadata,
    )
    view_status = representation_record().view_status | {
        "signature_explicit": ViewStatus.OK,
    }
    representation_a = representation_record(
        theorem_id=THM_A,
        signature_explicit="True",
        view_status=view_status,
    )
    representation_b = representation_record(
        representation_id=make_id("repr", {"theorem": THM_B, "version": "repr_v2"}),
        theorem_id=THM_B,
        signature_explicit="True",
        view_status=view_status,
        content_hash="5" * 64,
    )
    upstream_evidence_id = make_id(
        "ev",
        {"pair": pair_record().pair_id, "kind": "transformation_audit"},
    )
    pair = pair_record(
        split_group_ids=tuple(sorted((ANC_A, ANC_B))),
        evidence_ids=(upstream_evidence_id,),
        metadata=metadata,
    )
    upstream_evidence = EvidenceRecord(
        evidence_id=upstream_evidence_id,
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=pair.pair_id,
        kind=EvidenceKind.TRANSFORMATION_AUDIT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=AuditValue(checks={"fixture": True}, violation_codes=()),
        method_version="fixture_transform_audit_v1",
        config_hash="f" * 64,
        created_at=UTC_NOW,
        metadata=metadata,
    )
    paths = {
        "contexts": tmp_path / "input" / "contexts.jsonl",
        "theorems_a": tmp_path / "input" / "theorems_a.jsonl",
        "theorems_b": tmp_path / "input" / "theorems_b.jsonl",
        "representations_a": tmp_path / "input" / "representations_a.jsonl",
        "representations_b": tmp_path / "input" / "representations_b.jsonl",
        "pairs": tmp_path / "input" / "pairs.jsonl",
        "upstream_evidence": tmp_path / "input" / "upstream_evidence.jsonl",
    }
    _write_jsonl(paths["contexts"], (context,))
    _write_jsonl(paths["theorems_a"], (theorem_a,))
    _write_jsonl(paths["theorems_b"], (theorem_b,))
    _write_jsonl(paths["representations_a"], (representation_a,))
    _write_jsonl(paths["representations_b"], (representation_b,))
    _write_jsonl(paths["pairs"], (pair,))
    _write_jsonl(paths["upstream_evidence"], (upstream_evidence,))
    return paths


class _FakeBackend:
    closed = False

    def __init__(self, settings: object) -> None:
        self.settings = settings

    def close(self) -> None:
        self.closed = True


class _FakeCollector:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def collect_pair(self, **kwargs: object) -> PairEvidenceResult:
        pair = kwargs["pair"]
        assert isinstance(pair, PairRecord)
        evidence_id = make_id(
            "ev",
            {"pair": pair.pair_id, "kind": "defeq", "fixture": True},
        )
        evidence = EvidenceRecord(
            evidence_id=evidence_id,
            target_kind=EvidenceTargetKind.LEAN_PAIR,
            target_id=pair.pair_id,
            kind=EvidenceKind.DEFEQ,
            status=EvidenceExecutionStatus.SUCCESS,
            value=DefeqValue(outcome="equal"),
            method_version="defeq_fixture_v1",
            config_hash="a" * 64,
            created_at=UTC_NOW,
        )
        enriched = PairRecord.model_validate(
            {
                **pair.model_dump(mode="python"),
                "evidence_ids": tuple(sorted((*pair.evidence_ids, evidence_id))),
            }
        )
        return PairEvidenceResult(
            pair=enriched,
            evidence=(evidence,),
            cache_hits=0,
            cache_misses=0,
        )


def _patch_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepoPaths:
    (tmp_path / "configs").mkdir(parents=True)
    (tmp_path / "policies").mkdir(parents=True)
    (tmp_path / "configs/environment.lock.yaml").write_text("fixture: true\n", encoding="utf-8")
    (tmp_path / "policies/semantic_policy_v1.md").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    (tmp_path / "policies/evidence_policy_v1.yaml").write_text(
        "policy_version: fixture\n",
        encoding="utf-8",
    )
    real_configs = load_evidence_configs(RepoPaths(root=ROOT))
    monkeypatch.setattr(collect_evidence, "load_evidence_configs", lambda paths: real_configs)
    monkeypatch.setattr(collect_evidence, "LeanInteractBackend", _FakeBackend)
    monkeypatch.setattr(collect_evidence, "SymbolicEvidenceCollector", _FakeCollector)
    monkeypatch.setattr(
        collect_evidence,
        "collect_code_state",
        lambda root: CodeState(
            git_revision="1" * 40,
            git_dirty=False,
            base_git_commit="1" * 40,
            code_tree_hash="2" * 64,
            tracked_diff_hash="3" * 64,
        ),
    )
    return RepoPaths(root=tmp_path)


def _catalog_fixture(
    tmp_path: Path,
) -> tuple[RepoPaths, Path, dict[str, object], Path, Path]:
    paths = RepoPaths(root=tmp_path)
    context = context_record()
    representation_a = representation_record()
    representation_b = representation_record(
        representation_id=make_id("repr", {"catalog": "b"}),
        theorem_id=THM_B,
        content_hash="5" * 64,
    )
    key = EvidenceCacheKey(
        pair_id=pair_record().pair_id,
        theorem_a_id=THM_A,
        theorem_b_id=THM_B,
        theorem_a_statement_hash="2" * 64,
        theorem_b_statement_hash="4" * 64,
        representation_a_id=representation_a.representation_id,
        representation_b_id=representation_b.representation_id,
        representation_a_content_hash=representation_a.content_hash,
        representation_b_content_hash=representation_b.content_hash,
        representation_version=representation_a.normalization_version,
        context_id=context.context_id,
        context_fingerprint=context.context_fingerprint,
        environment_schema_version=context.environment_schema_version,
        environment_hash="6" * 64,
        evidence_kind=EvidenceKind.DEFEQ,
        evidence_direction="none",
        method_version="defeq_fixture_v1",
        timeout_seconds=1.0,
        config_hash="a" * 64,
        semantic_policy_version="semantic_policy_v1",
        semantic_policy_hash="7" * 64,
        lean_version=context.lean_version,
        lean_interact_version=context.lean_interact_version,
        repl_revision=context.repl_revision,
        project_revision=context.project_revision,
    )
    key_hash = compute_evidence_cache_key_hash(key)
    evidence_artifact = (
        tmp_path / "artifacts/evidence/lf020_symbolic_v1/run_fixture" / f"{key_hash}.json"
    )
    raw_response = (
        tmp_path / "data/evidence/lf020_symbolic_v1/run_fixture/raw_responses" / f"{key_hash}.json"
    )
    evidence_artifact.parent.mkdir(parents=True)
    raw_response.parent.mkdir(parents=True)
    evidence_artifact.write_text('{"artifact":true}\n', encoding="utf-8")
    raw_response.write_text('{"response":true}\n', encoding="utf-8")
    evidence_artifact_relative = evidence_artifact.relative_to(tmp_path).as_posix()
    raw_response_relative = raw_response.relative_to(tmp_path).as_posix()
    evidence = EvidenceRecord(
        evidence_id=make_id("ev", {"catalog": key_hash}),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=key.pair_id,
        kind=EvidenceKind.DEFEQ,
        status=EvidenceExecutionStatus.SUCCESS,
        value=DefeqValue(outcome="equal"),
        method_version=key.method_version,
        config_hash=key.config_hash,
        raw_artifact=evidence_artifact_relative,
        created_at=UTC_NOW,
        metadata={
            "cache_key": key_hash,
            "raw_artifact_sha256": hash_file(evidence_artifact),
        },
    )
    cache_dir = tmp_path / "cache"
    cache = EvidenceCache(cache_dir, artifact_root=tmp_path)
    entry = cache.put(
        key,
        evidence,
        generated_code_hash="8" * 64,
        lean_request_hashes=("9" * 64,),
        artifact_hashes={
            evidence_artifact_relative: hash_file(evidence_artifact),
            raw_response_relative: hash_file(raw_response),
        },
    )
    return paths, cache_dir, {entry.cache_key_hash: entry}, evidence_artifact, raw_response


def test_run_collect_evidence_propagates_smoke_and_emits_no_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    paths = _patch_runtime(tmp_path, monkeypatch)
    ticks = iter((1_000_000_000, 3_000_000_000))
    monkeypatch.setattr(collect_evidence, "_monotonic_ns", lambda: next(ticks))

    result = run_collect_evidence(
        paths=paths,
        context_paths=(inputs["contexts"],),
        theorem_paths=(inputs["theorems_a"], inputs["theorems_b"]),
        representation_paths=(
            inputs["representations_a"],
            inputs["representations_b"],
        ),
        pair_path=inputs["pairs"],
        project_dir=tmp_path / "project",
        upstream_evidence_paths=(inputs["upstream_evidence"],),
        out_dir=tmp_path / "output",
        cache_dir=tmp_path / "cache",
        artifact_dir=tmp_path / "artifacts",
        created_at=UTC_NOW,
    )

    assert result.artifact_class == ArtifactClass.SMOKE
    assert result.pair_count == 1
    assert result.evidence_count == 1
    assert result.failure_count == 0
    assert result.cache_misses == 0
    assert result.lean_request_attempts == 0
    assert result.retry_count == 0
    assert result.wall_elapsed_seconds == 2.0
    assert result.evidence_path.is_file()
    assert result.pair_path.is_file()
    assert result.failure_path.read_text(encoding="utf-8") == ""
    assert not (result.output_dir / "labels.jsonl").exists()
    assert result.output_manifest_path.is_file()
    assert result.run_manifest_path.is_file()
    assert result.artifact_catalog_path.is_file()
    assert result.cache_catalog_path.is_file()
    output_manifest = read_manifest(result.output_manifest_path, OutputManifest)
    upstream_key = inputs["upstream_evidence"].relative_to(tmp_path).as_posix()
    assert output_manifest.record_schema_version == CURRENT_RECORD_SCHEMA_VERSION
    assert upstream_key in output_manifest.input_partition_checksums
    assert output_manifest.terminal_outcome_counts["upstream_evidence_records"] == 1
    assert output_manifest.terminal_outcome_counts["evidence_jobs"] == 5
    assert output_manifest.terminal_outcome_counts["terminal_jobs_emitted"] == 1
    assert output_manifest.terminal_outcome_counts["axiom_audits"] == 0
    artifact_catalog_key = result.artifact_catalog_path.relative_to(tmp_path).as_posix()
    cache_catalog_key = result.cache_catalog_path.relative_to(tmp_path).as_posix()
    assert output_manifest.output_partition_checksums[artifact_catalog_key] == hash_file(
        result.artifact_catalog_path
    )
    assert output_manifest.output_partition_checksums[cache_catalog_key] == hash_file(
        result.cache_catalog_path
    )
    run_manifest = read_manifest(result.run_manifest_path, RunManifest)
    assert run_manifest.argv[:2] == ("leanfaith", "collect-evidence")
    assert "--upstream-evidence" in run_manifest.argv
    assert str(inputs["upstream_evidence"].resolve()) in run_manifest.argv
    assert "--out-dir" in run_manifest.argv
    assert str(result.output_dir) in run_manifest.argv
    assert "--cache-dir" in run_manifest.argv
    assert "--artifact-dir" in run_manifest.argv
    assert "--artifact-class" in run_manifest.argv
    assert "smoke" in run_manifest.argv
    assert "--root" in run_manifest.argv
    assert run_manifest.retry_count == 0
    assert run_manifest.measurements == {
        "artifact_catalog_entries": 0,
        "axiom_audits": 0,
        "cache_catalog_entries": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_puts": 0,
        "evidence_jobs": 5,
        "evidence_records": 1,
        "evidence_records_per_second": 0.5,
        "input_pairs": 1,
        "lean_backend_calls": 0,
        "lean_backend_elapsed_ms": 0,
        "lean_request_attempts": 0,
        "lean_unique_request_hashes": 0,
        "pairs_per_second": 0.5,
        "retries": 0,
        "terminal_jobs_emitted": 1,
        "wall_elapsed_seconds": 2.0,
    }
    artifact_catalog = EvidenceArtifactCatalog.model_validate_json(
        result.artifact_catalog_path.read_text(encoding="utf-8")
    )
    cache_catalog = EvidenceCacheCatalog.model_validate_json(
        result.cache_catalog_path.read_text(encoding="utf-8")
    )
    assert artifact_catalog.entries == ()
    assert cache_catalog.entries == ()
    enriched = collect_evidence._load_jsonl(result.pair_path, PairRecord)
    assert len(enriched[0].evidence_ids) == 2


def test_catalogs_bind_only_touched_cache_and_artifact_files(tmp_path: Path) -> None:
    paths, cache_dir, entries, evidence_artifact, raw_response = _catalog_fixture(tmp_path)
    output_dir = tmp_path / "output"
    (
        artifact_catalog_path,
        artifact_catalog_hash,
        artifact_catalog,
        cache_catalog_path,
        cache_catalog_hash,
        cache_catalog,
    ) = collect_evidence._write_evidence_catalogs(
        paths=paths,
        run_id="run_20260723T120000Z_deadbeef",
        output_dir=output_dir,
        cache_dir=cache_dir,
        cache_entries=entries,  # type: ignore[arg-type]
    )

    assert hash_file(artifact_catalog_path) == artifact_catalog_hash
    assert hash_file(cache_catalog_path) == cache_catalog_hash
    assert artifact_catalog_path.read_bytes().endswith(b"\n")
    assert cache_catalog_path.read_bytes().endswith(b"\n")
    assert [(entry.kind, entry.path, entry.sha256) for entry in artifact_catalog.entries] == [
        (
            "evidence_artifact",
            evidence_artifact.relative_to(tmp_path).as_posix(),
            hash_file(evidence_artifact),
        ),
        (
            "raw_response",
            raw_response.relative_to(tmp_path).as_posix(),
            hash_file(raw_response),
        ),
    ]
    assert len(cache_catalog.entries) == 1
    cache_entry = cache_catalog.entries[0]
    assert cache_entry.path == (
        f"v1/{cache_entry.cache_key_hash[:2]}/{cache_entry.cache_key_hash}.json"
    )
    assert cache_entry.sha256 == hash_file(cache_dir / cache_entry.path)


def test_artifact_catalog_fails_closed_on_tampering(tmp_path: Path) -> None:
    paths, cache_dir, entries, _evidence_artifact, raw_response = _catalog_fixture(tmp_path)
    raw_response.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(EvidenceCollectionInputError, match="artifact hash mismatch"):
        collect_evidence._write_evidence_catalogs(
            paths=paths,
            run_id="run_20260723T120000Z_deadbeef",
            output_dir=tmp_path / "output",
            cache_dir=cache_dir,
            cache_entries=entries,  # type: ignore[arg-type]
        )


def test_cache_catalog_fails_closed_on_missing_entry(tmp_path: Path) -> None:
    paths, cache_dir, entries, _evidence_artifact, _raw_response = _catalog_fixture(tmp_path)
    cache_key = next(iter(entries))
    (cache_dir / "v1" / cache_key[:2] / f"{cache_key}.json").unlink()

    with pytest.raises(EvidenceCollectionInputError, match="cache entry is missing"):
        collect_evidence._write_evidence_catalogs(
            paths=paths,
            run_id="run_20260723T120000Z_deadbeef",
            output_dir=tmp_path / "output",
            cache_dir=cache_dir,
            cache_entries=entries,  # type: ignore[arg-type]
        )


def test_cache_catalog_fails_closed_on_tampered_entry(tmp_path: Path) -> None:
    paths, cache_dir, entries, _evidence_artifact, _raw_response = _catalog_fixture(tmp_path)
    cache_key = next(iter(entries))
    cache_path = cache_dir / "v1" / cache_key[:2] / f"{cache_key}.json"
    cache_path.chmod(0o644)
    cache_path.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(EvidenceCollectionInputError, match="changed before cataloging"):
        collect_evidence._write_evidence_catalogs(
            paths=paths,
            run_id="run_20260723T120000Z_deadbeef",
            output_dir=tmp_path / "output",
            cache_dir=cache_dir,
            cache_entries=entries,  # type: ignore[arg-type]
        )


def test_measured_backend_counts_actual_retry_attempts() -> None:
    class Backend:
        def run(self, request: LeanRequest) -> LeanResult:
            return LeanResult(
                request_id=request.request_id,
                request_hash="a" * 64,
                context_id=request.context_id,
                context_fingerprint="b" * 64,
                status=LeanStatus.VALID,
                elapsed_ms=7,
            )

        def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
            return [self.run(request) for request in requests]

        def close(self) -> None:
            return None

    backend = collect_evidence._MeasuredLeanBackend(Backend())
    base = LeanRequest(
        request_id="measurement",
        context_id=f"ctx:{'b' * 64}",
        code="#check True",
    )
    backend.run(base)
    backend.run(
        LeanRequest(
            request_id=base.request_id,
            context_id=base.context_id,
            code=base.code,
            metadata={"attempt": "1"},
        )
    )

    assert backend.request_attempt_count == 2
    assert backend.retry_count == 1
    assert backend.elapsed_ms == 14
    assert backend.request_hashes == {"a" * 64}


def test_smoke_input_cannot_be_promoted_to_production(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    theorems = (
        *collect_evidence._load_jsonl(inputs["theorems_a"], theorem_record().__class__),
        *collect_evidence._load_jsonl(inputs["theorems_b"], theorem_record().__class__),
    )
    pairs = collect_evidence._load_jsonl(inputs["pairs"], PairRecord)

    with pytest.raises(EvidenceCollectionInputError, match="smoke input"):
        resolve_artifact_class(
            requested="production",
            theorems=theorems,
            pairs=pairs,
        )


def test_duplicate_ids_across_explicit_partitions_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    paths = _patch_runtime(tmp_path, monkeypatch)

    with pytest.raises(EvidenceCollectionInputError, match="duplicate theorem ID"):
        run_collect_evidence(
            paths=paths,
            context_paths=(inputs["contexts"],),
            theorem_paths=(inputs["theorems_a"], inputs["theorems_a"]),
            representation_paths=(
                inputs["representations_a"],
                inputs["representations_b"],
            ),
            pair_path=inputs["pairs"],
            project_dir=tmp_path / "project",
            upstream_evidence_paths=(inputs["upstream_evidence"],),
            out_dir=tmp_path / "output",
            created_at=UTC_NOW,
        )


def test_collect_evidence_cli_reports_evidence_only_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.cli import collect_evidence as module

    artifacts = EvidenceCollectionArtifacts(
        run_id="run_20260723T120000Z_deadbeef",
        artifact_class=ArtifactClass.SMOKE,
        output_dir=tmp_path / "output",
        evidence_path=tmp_path / "output/evidence.jsonl",
        pair_path=tmp_path / "output/pairs.jsonl",
        failure_path=tmp_path / "output/failures.jsonl",
        artifact_catalog_path=tmp_path / "output/artifact_catalog.json",
        cache_catalog_path=tmp_path / "output/cache_catalog.json",
        output_manifest_path=tmp_path / "output/manifest.json",
        run_manifest_path=tmp_path / "runs/run_20260723T120000Z_deadbeef/manifest.json",
        evidence_count=7,
        pair_count=1,
        failure_count=0,
        cache_hits=0,
        cache_misses=5,
        lean_request_attempts=9,
        retry_count=0,
        wall_elapsed_seconds=1.0,
    )
    monkeypatch.setattr(module, "run_collect_evidence", lambda **kwargs: artifacts)
    result = CliRunner().invoke(
        app,
        [
            "collect-evidence",
            "--contexts",
            str(tmp_path / "contexts.jsonl"),
            "--theorems",
            str(tmp_path / "theorems.jsonl"),
            "--representations",
            str(tmp_path / "representations.jsonl"),
            "--pairs",
            str(tmp_path / "pairs.jsonl"),
            "--project-dir",
            str(tmp_path / "project"),
            "--upstream-evidence",
            str(tmp_path / "upstream-evidence.jsonl"),
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "LF-020 evidence complete" in result.output
    assert "artifact_class=smoke" in result.output
    assert "resolved_labels_created=0" in result.output


def test_context_ids_are_required_to_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "contexts.jsonl"
    _write_jsonl(path, (context_record(), context_record()))
    loaded = collect_evidence._load_jsonl(path, context_record().__class__)

    with pytest.raises(EvidenceCollectionInputError, match="duplicate context ID"):
        collect_evidence._index_unique(
            loaded,
            id_field="context_id",
            record_kind="context",
        )


def test_preexisting_pair_evidence_must_be_explicitly_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    paths = _patch_runtime(tmp_path, monkeypatch)

    with pytest.raises(
        EvidenceCollectionInputError,
        match="pass their canonical records with --upstream-evidence",
    ):
        run_collect_evidence(
            paths=paths,
            context_paths=(inputs["contexts"],),
            theorem_paths=(inputs["theorems_a"], inputs["theorems_b"]),
            representation_paths=(
                inputs["representations_a"],
                inputs["representations_b"],
            ),
            pair_path=inputs["pairs"],
            project_dir=tmp_path / "project",
            out_dir=tmp_path / "output",
            created_at=UTC_NOW,
        )
