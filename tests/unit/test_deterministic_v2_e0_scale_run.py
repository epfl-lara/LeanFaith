"""Persisted v2 E0 scale runner is exact, resumable, and provisional."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.v2_e0_runtime import build_v2_e0_runtime
from leanfaith.transforms.v2_e0_scale_run import V2E0ScaleRunError, run_v2_e0_scale
from tests.unit.test_deterministic_v2_e0_scale import (
    _BatchBackend,
    _install_representation_batch,
    _source,
)


def _write_inputs(
    root: Path,
    *,
    wrapped_theorems: bool = False,
) -> tuple[Path, Path, dict[str, RepresentationRecord]]:
    inputs = (_source("persistedFirst"), _source("persistedSecond"))
    theorem_path = root / "theorems.jsonl"
    representation_path = root / "representations.jsonl"
    theorem_path.write_text(
        "".join(
            (
                json.dumps(
                    {
                        "theorem": item.theorem.model_dump(mode="json"),
                        "representation": item.representation.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
                if wrapped_theorems
                else item.theorem.model_dump_json()
            )
            + "\n"
            for item in inputs
        ),
        encoding="utf-8",
    )
    representation_path.write_text(
        "".join(item.representation.model_dump_json() + "\n" for item in inputs),
        encoding="utf-8",
    )
    return (
        theorem_path,
        representation_path,
        {cast(str, item.theorem.declaration_full_name): item.representation for item in inputs},
    )


def test_persisted_scale_run_resumes_without_reexecuting_lean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    theorem_path, representation_path, source_by_name = _write_inputs(tmp_path)
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,))
    observed_names: list[str] = []
    _install_representation_batch(monkeypatch, source_by_name, observed_names)
    output_dir = tmp_path / "run"

    first = run_v2_e0_scale(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_e0_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=output_dir,
        batch_size=2,
        base_seed=41,
    )

    assert first.result_count == 4
    assert first.manifest_path.is_file()
    assert first.results_path.read_text(encoding="utf-8").count("\n") == 4
    assert len(backend.batches) == 2
    assert observed_names == ["persistedFirst", "persistedSecond"]
    before_batches = tuple(backend.batches)

    second = run_v2_e0_scale(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_e0_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=output_dir,
        batch_size=2,
        base_seed=41,
    )

    assert second == first
    assert tuple(backend.batches) == before_batches
    manifest = first.manifest_path.read_text(encoding="utf-8")
    assert '"resolved_label_count":0' in manifest
    assert '"promoted_item_count":0' in manifest
    assert '"training_eligible":false' in manifest


def test_persisted_scale_rejects_changed_order_against_existing_run_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    theorem_path, representation_path, source_by_name = _write_inputs(tmp_path)
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,))
    _install_representation_batch(monkeypatch, source_by_name, [])
    arguments = {
        "backend": cast(LeanInteractBackend, backend),
        "runtime": build_v2_e0_runtime(),
        "theorem_path": theorem_path,
        "representation_path": representation_path,
        "project_dir": tmp_path,
        "import_header": "import LeanFaithFixtures",
        "output_dir": tmp_path / "run",
        "batch_size": 2,
        "base_seed": 9,
    }
    run_v2_e0_scale(**arguments)
    lines = theorem_path.read_text(encoding="utf-8").splitlines(keepends=True)
    theorem_path.write_text("".join(reversed(lines)), encoding="utf-8")

    with pytest.raises(V2E0ScaleRunError, match="immutable artifact conflict"):
        run_v2_e0_scale(**arguments)


def test_persisted_scale_rejects_misaligned_partitions_before_lean(tmp_path: Path) -> None:
    theorem_path, representation_path, _ = _write_inputs(tmp_path)
    representation_path.write_text(
        representation_path.read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )
    backend = _BatchBackend(())

    with pytest.raises(V2E0ScaleRunError, match="partition counts differ"):
        run_v2_e0_scale(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e0_runtime(),
            theorem_path=theorem_path,
            representation_path=representation_path,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
            output_dir=tmp_path / "run",
        )

    assert backend.batches == []


def test_persisted_scale_accepts_canonical_wrapped_extraction_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    theorem_path, representation_path, source_by_name = _write_inputs(
        tmp_path,
        wrapped_theorems=True,
    )
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,))
    _install_representation_batch(monkeypatch, source_by_name, [])

    artifacts = run_v2_e0_scale(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_e0_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=tmp_path / "wrapped-run",
        max_sources=1,
    )

    assert artifacts.result_count == 2
