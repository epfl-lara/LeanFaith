"""Security-boundary tests for isolated LF-022 historical executor replay."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import leanfaith.generation.lf022_historical_replay as historical_replay
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.lf022_historical_replay import (
    LF022HistoricalReplayError,
    _copy_binding_closure,
    _json_values,
    _launch_historical_subprocess,
    run_lf022_historical_replay,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding

_TREE_HASH = "8" * 64
_BUNDLE_HASH = "7" * 64


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _historical_checkout(
    root: Path,
    *,
    leak_batch_module: bool = False,
    leak_lazy_module: bool = False,
) -> tuple[Path, str]:
    source = root / "src/leanfaith"
    _write(source / "__init__.py", "\n")
    _write(source / "config/__init__.py", "\n")
    _write(
        source / "config/hashing.py",
        """
import hashlib
from pathlib import Path

def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
""".lstrip(),
    )
    _write(source / "generation/__init__.py", "\n")
    _write(
        source / "generation/llm_variants.py",
        'HISTORICAL_MARKER = "admitted-old-code"\n',
    )
    lazy_file_override = (
        f"\n__file__ = {str((Path.cwd() / 'src/leanfaith/generation/llm_variants.py').resolve())!r}\n"
        if leak_lazy_module
        else ""
    )
    _write(
        source / "generation/lazy_loaded.py",
        'LAZY_HISTORICAL_MARKER = "admitted-lazy-code"\n' + lazy_file_override,
    )
    _write(
        source / "generation/lf022_production.py",
        """
from pydantic import BaseModel

class LF022ArtifactBinding(BaseModel):
    path: str
    sha256: str
""".lstrip(),
    )
    batch_file_override = (
        f"\n__file__ = {str((Path.cwd() / 'src/leanfaith/generation/lf022_batch.py').resolve())!r}\n"
        if leak_batch_module
        else ""
    )
    _write(
        source / "generation/lf022_batch.py",
        (
            """
import json
from pathlib import Path
from types import SimpleNamespace

from .llm_variants import HISTORICAL_MARKER

def load_lf022_public_batch(*, repo_root: Path, manifest_binding):
    assert HISTORICAL_MARKER == "admitted-old-code"
    payload = json.loads((repo_root / manifest_binding.path).read_text())
    task = SimpleNamespace(
        task=SimpleNamespace(execution_task_id=payload["execution_task_id"]),
        admission=SimpleNamespace(),
        verified=None,
        task_inputs=None,
    )
    return SimpleNamespace(executor_output_root=payload["executor_output_root"]), (task,)
""".lstrip()
            + batch_file_override
        ),
    )
    _write(
        source / "generation/lf022_executor.py",
        """
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .llm_variants import HISTORICAL_MARKER

def execute_lf022_g_open_task(*, repo_root: Path, task, **kwargs):
    from .lazy_loaded import LAZY_HISTORICAL_MARKER

    assert HISTORICAL_MARKER == "admitted-old-code"
    assert LAZY_HISTORICAL_MARKER == "admitted-lazy-code"
    assert "RCP_API_KEY" not in os.environ
    assert Path(os.environ["HOME"]).resolve() == repo_root.parent / "home"
    digest = task.execution_task_id.split(":", 1)[1]
    path = repo_root / "data/executor/tasks" / digest[:2] / digest / "terminal.json"
    terminal = SimpleNamespace(terminal_id=json.loads(path.read_text())["terminal_id"])
    return SimpleNamespace(
        replayed=True,
        network_calls_this_run=0,
        terminal=terminal,
        terminal_path=path,
    )
""".lstrip(),
    )
    _write(source / "schemas/__init__.py", "\n")
    _write(
        source / "schemas/manifest.py",
        f"""
from types import SimpleNamespace

def collect_code_state(root):
    return SimpleNamespace(code_tree_hash={_TREE_HASH!r}, git_dirty=False)
""".lstrip(),
    )

    task_id = f"lf022_execution_task:{'1' * 64}"
    terminal_id = f"lf022_execution_terminal:{'2' * 64}"
    manifest_path = root / "data/batch/batch_manifest.json"
    manifest = {
        "execution_task_id": task_id,
        "executor_output_root": "data/executor",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    digest = task_id.split(":", 1)[1]
    terminal_path = root / "data/executor/tasks" / digest[:2] / digest / "terminal.json"
    terminal_path.parent.mkdir(parents=True, exist_ok=True)
    terminal_path.write_bytes(canonical_json_bytes({"terminal_id": terminal_id}) + b"\n")
    return manifest_path, task_id


def test_historical_subprocess_uses_only_admitted_modules_and_strips_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical_root = tmp_path / "historical"
    manifest_path, task_id = _historical_checkout(historical_root)
    monkeypatch.setenv("RCP_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd() / "src"))

    result = _launch_historical_subprocess(
        historical_root=historical_root,
        current_root=Path.cwd(),
        manifest_binding=LF022ArtifactBinding(
            path=manifest_path.relative_to(historical_root).as_posix(),
            sha256=hash_file(manifest_path),
        ),
        code_tree_hash=_TREE_HASH,
        code_bundle_sha256=_BUNDLE_HASH,
        task_count=1,
    )

    assert result.code_tree_hash == _TREE_HASH
    assert result.network_calls_performed == 0
    assert result.terminal_bindings[0].execution_task_id == task_id
    module = next(
        item
        for item in result.module_bindings
        if item.module_name == "leanfaith.generation.llm_variants"
    )
    assert module.path == "src/leanfaith/generation/llm_variants.py"
    assert module.sha256 == hash_file(historical_root / module.path)
    lazy_module = next(
        item
        for item in result.module_bindings
        if item.module_name == "leanfaith.generation.lazy_loaded"
    )
    assert lazy_module.path == "src/leanfaith/generation/lazy_loaded.py"
    assert lazy_module.sha256 == hash_file(historical_root / lazy_module.path)


def test_historical_subprocess_rejects_module_origin_leakage(tmp_path: Path) -> None:
    historical_root = tmp_path / "historical"
    manifest_path, _ = _historical_checkout(historical_root, leak_batch_module=True)

    with pytest.raises(LF022HistoricalReplayError, match="outside admitted source"):
        _launch_historical_subprocess(
            historical_root=historical_root,
            current_root=Path.cwd(),
            manifest_binding=LF022ArtifactBinding(
                path=manifest_path.relative_to(historical_root).as_posix(),
                sha256=hash_file(manifest_path),
            ),
            code_tree_hash=_TREE_HASH,
            code_bundle_sha256=_BUNDLE_HASH,
            task_count=1,
        )


def test_historical_subprocess_rejects_lazy_module_origin_leakage(tmp_path: Path) -> None:
    historical_root = tmp_path / "historical"
    manifest_path, _ = _historical_checkout(historical_root, leak_lazy_module=True)

    with pytest.raises(LF022HistoricalReplayError, match="outside admitted source"):
        _launch_historical_subprocess(
            historical_root=historical_root,
            current_root=Path.cwd(),
            manifest_binding=LF022ArtifactBinding(
                path=manifest_path.relative_to(historical_root).as_posix(),
                sha256=hash_file(manifest_path),
            ),
            code_tree_hash=_TREE_HASH,
            code_bundle_sha256=_BUNDLE_HASH,
            task_count=1,
        )


def _loaded_task(bundle: LF022ArtifactBinding) -> tuple[Any, ...]:
    admission = SimpleNamespace(
        admission_id=f"lf022_execution_admission:{'3' * 64}",
        code_tree_hash=_TREE_HASH,
        artifacts=SimpleNamespace(code_bundle=bundle),
    )
    task = SimpleNamespace(execution_task_id=f"lf022_execution_task:{'4' * 64}")
    return (SimpleNamespace(admission=admission, task=task),)


def test_jsonl_binding_scan_streams_records_lazily(tmp_path: Path) -> None:
    artifact = tmp_path / "bound.jsonl"
    artifact.write_text('{"index": 1}\nnot-json\n', encoding="utf-8")

    values = _json_values(artifact)

    assert next(values) == {"index": 1}
    with pytest.raises(LF022HistoricalReplayError, match="bound JSON artifact is invalid"):
        next(values)


def test_jsonl_binding_scan_does_not_use_whole_file_read_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "bound.jsonl"
    artifact.write_text('{"index": 1}\n{"index": 2}\n', encoding="utf-8")

    def reject_read_text(*args: object, **kwargs: object) -> str:
        raise AssertionError("JSONL closure scan must not read the whole file")

    def reject_read_bytes(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("JSONL closure scan must not read the whole file")

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    assert tuple(_json_values(artifact)) == ({"index": 1}, {"index": 2})


def test_binding_closure_discovers_children_from_streamed_jsonl(tmp_path: Path) -> None:
    source = tmp_path / "source"
    historical = tmp_path / "historical"
    historical.mkdir()
    first = source / "data/children/first.txt"
    second = source / "data/children/second.txt"
    _write(first, "first\n")
    _write(second, "second\n")
    records = source / "data/records.jsonl"
    first_binding = {
        "path": first.relative_to(source).as_posix(),
        "sha256": hash_file(first),
    }
    records.write_bytes(
        canonical_json_bytes(first_binding)
        + b"\n"
        + canonical_json_bytes(
            {"path": second.relative_to(source).as_posix(), "sha256": hash_file(second)}
        )
        + b"\n"
        + canonical_json_bytes(first_binding)
        + b"\n"
    )
    manifest = source / "data/manifest.json"
    manifest.write_bytes(
        canonical_json_bytes(
            {
                "records": {
                    "path": records.relative_to(source).as_posix(),
                    "sha256": hash_file(records),
                }
            }
        )
    )

    _copy_binding_closure(
        source_root=source,
        historical_root=historical,
        initial=(
            LF022ArtifactBinding(
                path=manifest.relative_to(source).as_posix(),
                sha256=hash_file(manifest),
            ),
        ),
    )

    assert (historical / first.relative_to(source)).read_bytes() == b"first\n"
    assert (historical / second.relative_to(source)).read_bytes() == b"second\n"


def test_binding_closure_rejects_conflicting_duplicate_discovered_in_jsonl(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    historical = tmp_path / "historical"
    historical.mkdir()
    child = source / "data/child.txt"
    _write(child, "child\n")
    child_path = child.relative_to(source).as_posix()
    records = source / "data/records.jsonl"
    records.write_bytes(
        canonical_json_bytes({"path": child_path, "sha256": hash_file(child)})
        + b"\n"
        + canonical_json_bytes({"path": child_path, "sha256": "0" * 64})
        + b"\n"
    )
    manifest = source / "data/manifest.json"
    manifest.write_bytes(
        canonical_json_bytes(
            {
                "records": {
                    "path": records.relative_to(source).as_posix(),
                    "sha256": hash_file(records),
                }
            }
        )
    )

    with pytest.raises(LF022HistoricalReplayError, match="conflicting hashes"):
        _copy_binding_closure(
            source_root=source,
            historical_root=historical,
            initial=(
                LF022ArtifactBinding(
                    path=manifest.relative_to(source).as_posix(),
                    sha256=hash_file(manifest),
                ),
            ),
        )


def test_binding_closure_duplicate_bindings_do_not_consume_unique_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    historical = tmp_path / "historical"
    historical.mkdir()
    child = source / "data/child.txt"
    _write(child, "child\n")
    binding = (
        canonical_json_bytes(
            {"path": child.relative_to(source).as_posix(), "sha256": hash_file(child)}
        )
        + b"\n"
    )
    records = source / "data/records.jsonl"
    records.parent.mkdir(parents=True, exist_ok=True)
    records.write_bytes(binding * 2_000)
    manifest = source / "data/manifest.json"
    manifest.write_bytes(
        canonical_json_bytes(
            {
                "records": {
                    "path": records.relative_to(source).as_posix(),
                    "sha256": hash_file(records),
                }
            }
        )
    )
    monkeypatch.setattr(historical_replay, "_ARTIFACT_BINDING_CLOSURE_LIMIT", 3)

    _copy_binding_closure(
        source_root=source,
        historical_root=historical,
        initial=(
            LF022ArtifactBinding(
                path=manifest.relative_to(source).as_posix(),
                sha256=hash_file(manifest),
            ),
        ),
    )

    assert (historical / child.relative_to(source)).read_bytes() == b"child\n"


def test_binding_closure_limit_rejects_extra_unique_before_child_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    historical = tmp_path / "historical"
    historical.mkdir()
    first = source / "data/first.txt"
    second = source / "data/second.txt"
    _write(first, "first\n")
    _write(second, "second\n")
    first_binding = (
        canonical_json_bytes(
            {"path": first.relative_to(source).as_posix(), "sha256": hash_file(first)}
        )
        + b"\n"
    )
    second_binding = (
        canonical_json_bytes(
            {"path": second.relative_to(source).as_posix(), "sha256": hash_file(second)}
        )
        + b"\n"
    )
    records = source / "data/records.jsonl"
    records.write_bytes(first_binding * 2_000 + second_binding)
    manifest = source / "data/manifest.json"
    manifest.write_bytes(
        canonical_json_bytes(
            {
                "records": {
                    "path": records.relative_to(source).as_posix(),
                    "sha256": hash_file(records),
                }
            }
        )
    )
    monkeypatch.setattr(historical_replay, "_ARTIFACT_BINDING_CLOSURE_LIMIT", 3)

    with pytest.raises(LF022HistoricalReplayError, match="closure exceeds safety limit"):
        _copy_binding_closure(
            source_root=source,
            historical_root=historical,
            initial=(
                LF022ArtifactBinding(
                    path=manifest.relative_to(source).as_posix(),
                    sha256=hash_file(manifest),
                ),
            ),
        )

    assert not (historical / first.relative_to(source)).exists()
    assert not (historical / second.relative_to(source)).exists()


def test_historical_replay_rejects_tampered_bundle_binding(tmp_path: Path) -> None:
    bundle_path = tmp_path / "artifacts/code_bundle.tar.gz"
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(b"tampered")
    binding = LF022ArtifactBinding(
        path=bundle_path.relative_to(tmp_path).as_posix(),
        sha256="0" * 64,
    )

    with pytest.raises(LF022HistoricalReplayError, match="admission binding"):
        run_lf022_historical_replay(
            repo_root=tmp_path,
            manifest_binding=LF022ArtifactBinding(
                path="data/batch.json",
                sha256="1" * 64,
            ),
            loaded_tasks=cast(Any, _loaded_task(binding)),
            executor_output_root="data/executor",
        )


def test_historical_replay_rejects_bundle_path_traversal(tmp_path: Path) -> None:
    bundle_path = tmp_path / "artifacts/malicious.tar.gz"
    bundle_path.parent.mkdir(parents=True)
    with tarfile.open(bundle_path, mode="w:gz") as archive:
        member = tarfile.TarInfo("../escape.py")
        payload = b"escape = True\n"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    binding = LF022ArtifactBinding(
        path=bundle_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(bundle_path),
    )

    with pytest.raises(LF022HistoricalReplayError, match="historical code bundle rejected"):
        run_lf022_historical_replay(
            repo_root=tmp_path,
            manifest_binding=LF022ArtifactBinding(
                path="data/batch.json",
                sha256="1" * 64,
            ),
            loaded_tasks=cast(Any, _loaded_task(binding)),
            executor_output_root="data/executor",
        )
