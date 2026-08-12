"""Offline tests for the bounded sequential Codex proposer v2 adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation import lf022_codex_proposer_scale as scale_module
from leanfaith.generation.lf022_batch import (
    LF022BatchTaskBinding,
    LF022PublicBatchManifest,
)
from leanfaith.generation.lf022_codex_proposer import LF022CodexProposerError
from leanfaith.generation.lf022_codex_proposer_scale import (
    LF022CodexProposerScaleManifest,
    load_lf022_codex_proposer_scale_config,
    run_lf022_codex_proposer_scale,
)
from leanfaith.schemas.ids import make_id
from tests.unit.test_lf022_codex_proposer import _batch_fixture, _config_fixture


def _expanded_batch_fixture(root: Path, count: int) -> tuple[Path, tuple[str, ...]]:
    path, _ = _batch_fixture(root)
    manifest = LF022PublicBatchManifest.model_validate_json(path.read_bytes())
    base = manifest.routes[0].tasks[0]
    tasks = tuple(
        LF022BatchTaskBinding(
            allocation_task_id=make_id("lf022_production_task", {"scale_fixture_index": index}),
            execution_task_id=make_id("lf022_execution_task", {"scale_fixture_index": index}),
            task=base.task,
        )
        for index in range(count)
    )
    tasks = tuple(sorted(tasks, key=lambda item: item.execution_task_id))
    route = manifest.routes[0].model_copy(update={"tasks": tasks})
    payload = manifest.model_dump(mode="json", exclude={"batch_id"})
    payload["routes"] = [route.model_dump(mode="json")]
    payload["total_task_count"] = count
    expanded = LF022PublicBatchManifest.model_validate(
        {**payload, "batch_id": make_id("lf022_public_batch", payload)}
    )
    path.write_bytes(canonical_json_bytes(expanded.model_dump(mode="json")))
    return path, tuple(item.execution_task_id for item in tasks)


def _scale_config_fixture(root: Path, *, task_limit: int) -> Path:
    delegate = _config_fixture(root)
    relative = delegate.relative_to(root).as_posix()
    config = {
        "schema_version": 2,
        "config_id": "lf022_codex_proposer_scale_v2",
        "status": "bounded_public_scale_only",
        "delegate_v1_config": {"path": relative, "sha256": hash_file(delegate)},
        "provider": "openai_codex_exec",
        "provider_slot": "lf022_codex_proposer_terra_v1",
        "model_family": "openai_codex",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "xhigh",
        "task_limit": task_limit,
        "hard_maximum_task_count": 64,
        "maximum_concurrency": 1,
        "execution_order": "manifest_order_sequential",
        "one_v1_invocation_per_task": True,
        "immutable_per_item_artifacts": True,
        "terminal_replay_required": True,
        "execute_requires_explicit_flag": True,
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "own_validator_allowed": False,
        "separate_family_validation_required": True,
        "validation_performed": False,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    path = root / "configs/generation/codex_proposer_scale_test.json"
    path.write_bytes(canonical_json_bytes(config))
    return path


class _FakeV1Runner:
    """Filesystem-backed fake preserving v1 terminal replay semantics."""

    def __init__(self, *, failed_task_ids: frozenset[str] = frozenset()) -> None:
        self.failed_task_ids = failed_task_ids
        self.delegate_calls: list[str] = []
        self.external_calls: list[str] = []

    def __call__(self, **kwargs: object) -> SimpleNamespace:
        task_ids = tuple(kwargs["execution_task_ids"])  # type: ignore[arg-type]
        assert len(task_ids) == 1
        task_id = str(task_ids[0])
        self.delegate_calls.append(task_id)
        output_root = Path(str(kwargs["output_root"]))
        item_id = make_id("lf022_codex_proposer_item", {"execution_task_id": task_id})
        terminal_id = make_id("lf022_codex_proposer_terminal", {"item_id": item_id})
        item_dir = output_root / "items" / item_id.rsplit(":", 1)[1]
        terminal_path = item_dir / "terminal.json"
        existed = terminal_path.is_file()
        status = (
            "process_failed" if task_id in self.failed_task_ids else "provisional_variants_created"
        )
        variant_count = 0 if task_id in self.failed_task_ids else 1
        terminal_payload = canonical_json_bytes(
            {
                "terminal_id": terminal_id,
                "item_id": item_id,
                "status": status,
                "provisional_variant_count": variant_count,
            }
        )
        if existed:
            assert terminal_path.read_bytes() == terminal_payload
        else:
            item_dir.mkdir(parents=True, exist_ok=True)
            terminal_path.write_bytes(terminal_payload)
            self.external_calls.append(task_id)
        manifest_path = output_root / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(
            canonical_json_bytes({"execution_task_id": task_id, "status": status})
        )
        terminal = SimpleNamespace(
            terminal_id=terminal_id,
            status=status,
            provisional_variant_count=variant_count,
        )
        prepared = SimpleNamespace(
            item=SimpleNamespace(item_id=item_id),
            item_directory=item_dir,
        )
        return SimpleNamespace(
            prepared=(prepared,),
            terminals=(terminal,),
            manifest=SimpleNamespace(),
            manifest_path=manifest_path,
            invoked_count=0 if existed else 1,
            reused_count=1 if existed else 0,
        )


def _run(
    root: Path,
    *,
    manifest_path: Path,
    config_path: Path,
    execution_task_ids: tuple[str, ...] = (),
    task_limit: int | None = None,
) -> scale_module.LF022CodexProposerScaleRunResult:
    return run_lf022_codex_proposer_scale(
        repo_root=root,
        config_path=config_path,
        batch_manifest_path=manifest_path,
        output_root=root / "data/codex_scale_output",
        execution_task_ids=execution_task_ids,
        task_limit=task_limit,
        execute_public_provisional=True,
        verify_cli_pin=False,
    )


def test_scale_runs_multiple_tasks_sequentially_and_isolates_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_ids = _expanded_batch_fixture(tmp_path, 3)
    config_path = _scale_config_fixture(tmp_path, task_limit=3)
    fake = _FakeV1Runner(failed_task_ids=frozenset({task_ids[1]}))
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    result = _run(
        tmp_path,
        manifest_path=manifest_path,
        config_path=config_path,
    )

    assert fake.delegate_calls == list(task_ids)
    assert fake.external_calls == list(task_ids)
    assert result.manifest is not None
    assert result.manifest.completed_count == 3
    assert result.manifest.invoked_count == 3
    assert result.manifest.reused_count == 0
    assert result.manifest.provisional_variant_count == 2
    assert result.manifest.status_counts == {
        "process_failed": 1,
        "provisional_variants_created": 2,
    }
    assert result.manifest.maximum_concurrency == 1
    assert result.manifest.separate_family_validation_required is True
    assert result.manifest.semantic_labels_created is False
    assert result.manifest.training_eligible is False


def test_scale_replay_reuses_the_exact_immutable_tranche(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_ids = _expanded_batch_fixture(tmp_path, 3)
    config_path = _scale_config_fixture(tmp_path, task_limit=3)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    first = _run(tmp_path, manifest_path=manifest_path, config_path=config_path)
    second = _run(
        tmp_path,
        manifest_path=manifest_path,
        config_path=config_path,
    )

    assert first.invoked_count == 3
    assert first.reused_count == 0
    assert second.invoked_count == 0
    assert second.reused_count == 3
    assert fake.external_calls == list(task_ids)
    assert fake.delegate_calls == [*task_ids, *task_ids]


def test_scale_root_cannot_exceed_64_by_changing_selection_between_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_ids = _expanded_batch_fixture(tmp_path, 65)
    config_path = _scale_config_fixture(tmp_path, task_limit=64)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    first = _run(
        tmp_path,
        manifest_path=manifest_path,
        config_path=config_path,
        execution_task_ids=task_ids[:64],
    )
    assert first.invoked_count == 64
    with pytest.raises(LF022CodexProposerError, match="immutable tranche conflict"):
        _run(
            tmp_path,
            manifest_path=manifest_path,
            config_path=config_path,
            execution_task_ids=task_ids[64:],
        )
    assert len(fake.external_calls) == 64


def test_scale_rejects_symlinked_v1_run_entry_even_with_expected_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_ids = _expanded_batch_fixture(tmp_path, 1)
    config_path = _scale_config_fixture(tmp_path, task_limit=1)
    output_root = tmp_path / "data/codex_scale_output"
    run_name = scale_module.sha256_hex(task_ids[0].encode("utf-8"))
    runs_root = output_root / "v1_runs"
    runs_root.mkdir(parents=True)
    target = output_root / "symlink-target"
    target.mkdir()
    (runs_root / run_name).symlink_to(target, target_is_directory=True)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    with pytest.raises(LF022CodexProposerError, match="unsafe v1 run entry"):
        _run(
            tmp_path,
            manifest_path=manifest_path,
            config_path=config_path,
        )
    assert fake.external_calls == []
    assert not (output_root / "tranche.json").exists()


def test_scale_rejects_nested_items_symlink_before_external_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_id = _batch_fixture(tmp_path)
    config_path = _scale_config_fixture(tmp_path, task_limit=1)
    output_root = tmp_path / "data/codex_scale_output"
    run_root = scale_module._delegate_run_root(output_root, task_id)
    run_root.mkdir(parents=True)
    escaped = tmp_path / "data/escaped-items"
    escaped.mkdir(parents=True)
    (run_root / "items").symlink_to(escaped, target_is_directory=True)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    with pytest.raises(LF022CodexProposerError, match="symlink entry"):
        _run(
            tmp_path,
            manifest_path=manifest_path,
            config_path=config_path,
        )

    assert fake.delegate_calls == []
    assert fake.external_calls == []
    assert tuple(escaped.iterdir()) == ()


@pytest.mark.parametrize(
    "kind",
    [
        "config",
        "config_parent",
        "manifest",
        "manifest_parent",
        "output",
        "output_parent",
    ],
)
def test_scale_rejects_direct_and_ancestor_symlink_paths(
    tmp_path: Path,
    kind: str,
) -> None:
    manifest_path, _ = _batch_fixture(tmp_path)
    config_path = _scale_config_fixture(tmp_path, task_limit=1)
    output_root = tmp_path / "data/codex_scale_output"
    if kind == "config":
        alias = tmp_path / "config-link.yaml"
        alias.symlink_to(config_path)
        config_path = alias
    elif kind == "config_parent":
        alias = tmp_path / "config-parent-link"
        alias.symlink_to(config_path.parent, target_is_directory=True)
        config_path = alias / config_path.name
    elif kind == "manifest":
        alias = tmp_path / "manifest-link.json"
        alias.symlink_to(manifest_path)
        manifest_path = alias
    elif kind == "manifest_parent":
        alias = tmp_path / "manifest-parent-link"
        alias.symlink_to(manifest_path.parent, target_is_directory=True)
        manifest_path = alias / manifest_path.name
    elif kind == "output":
        target = tmp_path / "real-output"
        target.mkdir()
        output_root.symlink_to(target, target_is_directory=True)
    else:
        target = tmp_path / "real-parent"
        target.mkdir()
        alias = tmp_path / "linked-parent"
        alias.symlink_to(target, target_is_directory=True)
        output_root = alias / "output"

    with pytest.raises(LF022CodexProposerError, match="symlink component"):
        run_lf022_codex_proposer_scale(
            repo_root=tmp_path,
            config_path=config_path,
            batch_manifest_path=manifest_path,
            output_root=output_root,
            execute_public_provisional=False,
            verify_cli_pin=False,
        )


def test_scale_rejects_duplicate_requested_ids_before_delegate_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_ids = _expanded_batch_fixture(tmp_path, 2)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    with pytest.raises(LF022CodexProposerError, match="must be unique"):
        _run(
            tmp_path,
            manifest_path=manifest_path,
            config_path=_scale_config_fixture(tmp_path, task_limit=2),
            execution_task_ids=(task_ids[0], task_ids[0]),
        )
    assert fake.delegate_calls == []


def test_scale_rejects_runtime_limit_above_configured_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _ = _expanded_batch_fixture(tmp_path, 3)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    with pytest.raises(LF022CodexProposerError, match="exceeds reviewed v2 bound"):
        _run(
            tmp_path,
            manifest_path=manifest_path,
            config_path=_scale_config_fixture(tmp_path, task_limit=2),
            task_limit=3,
        )
    assert fake.delegate_calls == []


def test_scale_rejects_more_than_hard_maximum_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, task_ids = _expanded_batch_fixture(tmp_path, 65)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)

    with pytest.raises(LF022CodexProposerError, match="exceeds effective task limit"):
        _run(
            tmp_path,
            manifest_path=manifest_path,
            config_path=_scale_config_fixture(tmp_path, task_limit=64),
            execution_task_ids=task_ids,
        )
    assert fake.delegate_calls == []


def test_scale_manifest_rejects_nonreconciling_status_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _ = _expanded_batch_fixture(tmp_path, 2)
    fake = _FakeV1Runner()
    monkeypatch.setattr(scale_module, "run_lf022_codex_proposer", fake)
    result = _run(
        tmp_path,
        manifest_path=manifest_path,
        config_path=_scale_config_fixture(tmp_path, task_limit=2),
    )
    assert result.manifest is not None
    payload = result.manifest.model_dump(mode="json")
    payload["status_counts"] = {"provisional_variants_created": 1}
    with pytest.raises(ValueError, match="status counts do not reconcile"):
        LF022CodexProposerScaleManifest.model_validate(payload)


def test_production_scale_config_replays_exact_v1_pin() -> None:
    root = Path(__file__).resolve().parents[2]
    loaded = load_lf022_codex_proposer_scale_config(
        root / "configs/generation/lf022_codex_proposer_scale_v2.yaml",
        repo_root=root,
    )
    assert loaded.config.task_limit == 64
    assert loaded.delegate.config.maximum_task_count == 1
    assert loaded.delegate.config.model == "gpt-5.6-terra"


def test_codex_proposer_scale_cli_requires_explicit_external_flag() -> None:
    result = CliRunner().invoke(app, ["propose-lf022-codex-scale", "--help"])
    assert result.exit_code == 0
    assert "--execute-public-pro" in result.stdout
    assert "--limit" in result.stdout
