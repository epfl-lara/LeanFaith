from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import leanfaith.generation.lf022_lean_check as checker
from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckError,
    check_lf022_provisional_candidates,
    parse_project_mappings,
)
from leanfaith.lean.leaninteract_backend import BackendSettings
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import ServerMode
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.variant import VariantRecord

REVISION = "a" * 40
CTX_A = "ctx:" + "1" * 64
CTX_B = "ctx:" + "2" * 64


def _variant(index: int, *, context_id: str) -> VariantRecord:
    statement = f"theorem proposed_{index} (n : Nat) : n = n"
    return VariantRecord(
        variant_id="var:" + f"{index:064x}",
        source_theorem_ids=("thm:" + f"{index:064x}",),
        source_representation_ids=("repr:" + f"{index:064x}",),
        context_id=context_id,
        generator_kind=GeneratorKind.LLM_PROPOSER,
        generator_id="test-proposer",
        generation_config_hash="f" * 64,
        seed=42,
        extracted_statement=statement,
        candidate_code_hash=sha256_hex(statement.encode()),
        intended_relation=IntendedRelation.NEAR_MISS,
        candidate_pool="G_open",
        validation_status=ValidationStatus.UNVALIDATED,
        quality_tier=QualityTier.PROVISIONAL,
        polarity_metadata=Polarity.NEGATIVE,
    )


def _write_task(
    root: Path,
    repo_root: Path,
    *,
    index: int,
    context_id: str,
    imports: list[str],
) -> Path:
    task_dir = root / "tasks" / f"{index:02x}" / f"{index:064x}"
    task_dir.mkdir(parents=True)
    variant = _variant(index, context_id=context_id)
    variants_path = task_dir / "provisional_variants.jsonl"
    variants_path.write_bytes(canonical_json_bytes(variant.model_dump(mode="json")) + b"\n")
    task = {
        "schema_version": 2,
        "source": {
            "source_id": "mathlib",
            "source_revision": REVISION,
            "context_id": context_id,
            "imports": imports,
        },
    }
    (task_dir / "task.json").write_bytes(canonical_json_bytes(task) + b"\n")
    terminal = {
        "status": "provisional_variants_created",
        "variants_artifact": variants_path.relative_to(repo_root).as_posix(),
        "variants_sha256": hash_file(variants_path),
        "provisional_variant_count": 1,
    }
    (task_dir / "terminal.json").write_bytes(canonical_json_bytes(terminal) + b"\n")
    return variants_path


class FakeBackend:
    created: ClassVar[list[FakeBackend]] = []
    status_scripts: ClassVar[dict[str, list[LeanStatus]]] = {}

    def __init__(self, settings: BackendSettings) -> None:
        self.settings = settings
        self.batches: list[list[str]] = []
        self.reset_count = 0
        self.closed = False
        self.calls: dict[str, int] = {}
        self.created.append(self)

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        self.batches.append([request.request_id for request in requests])
        results: list[LeanResult] = []
        for request in requests:
            count = self.calls.get(request.request_id, 0)
            self.calls[request.request_id] = count + 1
            script = self.status_scripts.get(request.request_id, [LeanStatus.VALID_WITH_SORRY])
            status = script[min(count, len(script) - 1)]
            raw_path = self.settings.raw_response_dir / (
                sha256_hex(f"{request.request_id}:{count}".encode()) + ".json"
            )
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("{}", encoding="utf-8")
            declarations: tuple[dict[str, object], ...] = ()
            if status in {LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY}:
                assert request.code is not None
                marker = "theorem V_"
                name_start = request.code.index(marker) + len("theorem ")
                name_end = request.code.index(" ", name_start)
                declarations = (
                    {
                        "kind": "theorem",
                        "full_name": "LeanFaithLF022Check." + request.code[name_start:name_end],
                    },
                )
            results.append(
                LeanResult(
                    request_id=request.request_id,
                    request_hash=sha256_hex(request.request_id.encode()),
                    context_id=request.context_id,
                    context_fingerprint=request.context_id.removeprefix("ctx:"),
                    status=status,
                    declarations=declarations,
                    elapsed_ms=1,
                    raw_response_path=str(raw_path),
                    infrastructure_error="crash" if status is LeanStatus.CRASH else None,
                )
            )
        return results

    def reset_session(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_backend() -> None:
    FakeBackend.created = []
    FakeBackend.status_scripts = {}


def test_pool_check_groups_chunks_retries_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "data" / "lf022_execution"
    output_root = tmp_path / "data" / "checks"
    project_dir = tmp_path / "mathlib"
    project_dir.mkdir()
    variants = [
        _write_task(input_root, tmp_path, index=1, context_id=CTX_A, imports=["Mathlib"]),
        _write_task(input_root, tmp_path, index=2, context_id=CTX_A, imports=["Mathlib"]),
        _write_task(input_root, tmp_path, index=3, context_id=CTX_A, imports=["Mathlib"]),
        _write_task(
            input_root,
            tmp_path,
            index=4,
            context_id=CTX_B,
            imports=["Mathlib", "Mathlib.Algebra.Group.Basic"],
        ),
    ]
    before = {path: path.read_bytes() for path in variants}
    FakeBackend.status_scripts = {
        "lf022-lean-check-" + f"{2:064x}": [LeanStatus.INVALID],
        "lf022-lean-check-" + f"{3:064x}": [LeanStatus.CRASH, LeanStatus.VALID_WITH_SORRY],
    }
    monkeypatch.setattr(checker, "read_git_revision", lambda _path: REVISION)
    prepared: list[BackendSettings] = []

    result = check_lf022_provisional_candidates(
        repo_root=tmp_path,
        input_root=input_root,
        output_root=output_root,
        project_dirs={"mathlib": project_dir},
        workers=3,
        chunk_size=2,
        timeout_seconds=20,
        max_attempts=2,
        backend_factory=FakeBackend,
        prepare_environment=prepared.append,
    )

    assert result.executed_count == 4
    assert result.reused_count == 0
    assert [record.variant_id for record in result.records] == [
        "var:" + f"{index:064x}" for index in range(1, 5)
    ]
    assert [record.outcome for record in result.records] == [
        "elaborates_with_placeholder",
        "invalid",
        "elaborates_with_placeholder",
        "elaborates_with_placeholder",
    ]
    assert len(result.records[2].attempts) == 2
    assert len(prepared) == 1
    assert len(FakeBackend.created) == 2
    assert all(backend.settings.server_mode is ServerMode.POOL for backend in FakeBackend.created)
    assert all(backend.settings.workers == 3 for backend in FakeBackend.created)
    assert all(backend.settings.confirm_invalid_on_fresh_process for backend in FakeBackend.created)
    assert all(backend.settings.environment_is_prepared for backend in FakeBackend.created)
    assert all(backend.closed for backend in FakeBackend.created)
    assert any(backend.reset_count == 1 for backend in FakeBackend.created)
    assert {path: path.read_bytes() for path in variants} == before
    assert result.manifest.semantic_labels_created is False
    assert result.manifest.training_eligible is False

    FakeBackend.created = []
    prepared.clear()
    replay = check_lf022_provisional_candidates(
        repo_root=tmp_path,
        input_root=input_root,
        output_root=output_root,
        project_dirs={"mathlib": project_dir},
        workers=3,
        chunk_size=2,
        timeout_seconds=20,
        max_attempts=2,
        backend_factory=FakeBackend,
        prepare_environment=prepared.append,
    )
    assert replay.executed_count == 0
    assert replay.reused_count == 4
    assert not FakeBackend.created
    assert not prepared
    assert replay.manifest == result.manifest


def test_resume_rejects_mutated_source_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "data" / "lf022_execution"
    output_root = tmp_path / "data" / "checks"
    project_dir = tmp_path / "mathlib"
    project_dir.mkdir()
    variants_path = _write_task(
        input_root, tmp_path, index=1, context_id=CTX_A, imports=["Mathlib"]
    )
    monkeypatch.setattr(checker, "read_git_revision", lambda _path: REVISION)
    kwargs = {
        "repo_root": tmp_path,
        "input_root": input_root,
        "output_root": output_root,
        "project_dirs": {"mathlib": project_dir},
        "workers": 2,
        "chunk_size": 8,
        "timeout_seconds": 20,
        "backend_factory": FakeBackend,
        "prepare_environment": lambda _settings: None,
    }
    check_lf022_provisional_candidates(**kwargs)
    variant_payload = json.loads(variants_path.read_text(encoding="utf-8"))
    variant_payload["metadata"] = {"tampered": True}
    variants_path.write_bytes(canonical_json_bytes(variant_payload) + b"\n")
    terminal_path = variants_path.with_name("terminal.json")
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal["variants_sha256"] = hash_file(variants_path)
    terminal_path.write_bytes(canonical_json_bytes(terminal) + b"\n")
    with pytest.raises(LF022LeanCheckError, match="resume record no longer binds"):
        check_lf022_provisional_candidates(**kwargs)


def test_project_mapping_and_cli_help(tmp_path: Path) -> None:
    mapping = parse_project_mappings(["mathlib=mathlib4"], repo_root=tmp_path)
    assert mapping == {"mathlib": (tmp_path / "mathlib4").resolve()}
    with pytest.raises(LF022LeanCheckError, match="expected SOURCE_ID=PROJECT_DIR"):
        parse_project_mappings(["mathlib"], repo_root=tmp_path)
    result = CliRunner().invoke(app, ["check-lf022-provisional-lean", "--help"])
    assert result.exit_code == 0
    assert "LeanServerPool" in result.stdout
