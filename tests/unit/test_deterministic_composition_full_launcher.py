from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import leanfaith.transforms.composition_full_launcher as launcher
from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.transforms.composition_full_launcher import (
    CompositionFullLaunchError,
    CompositionFullLaunchSpec,
    FullFamilyPlan,
    FullRootReceipt,
    _family_command,
    run_composition_full_scale,
)
from leanfaith.transforms.composition_smoke_launcher import FAMILY_DEFINITIONS, _without_id


def _spec(tmp_path: Path) -> CompositionFullLaunchSpec:
    for name in ("code", "seed", "project", "output"):
        (tmp_path / name).mkdir()
    plans = tuple(
        FullFamilyPlan(
            family=item.key,
            run_kind=item.run_kind,
            profile_id=item.profile_id,
            profile_path=str(tmp_path / "code" / item.profile_name),
            profile_file_sha256="1" * 64,
            output_root=str(tmp_path / "output" / item.key),
        )
        for item in FAMILY_DEFINITIONS
    )
    payload: dict[str, object] = {
        "launch_id": f"detcomp_full_launch:{'0' * 64}",
        "code_root": str(tmp_path / "code"),
        "expected_commit": "a" * 40,
        "code_tree_hash": "2" * 64,
        "project_dir": str(tmp_path / "project"),
        "project_revision": "b" * 40,
        "project_tree": "c" * 40,
        "lean_toolchain_sha256": "3" * 64,
        "seed_dir": str(tmp_path / "seed"),
        "seed_manifest_sha256": "4" * 64,
        "seed_set_id": f"detcomp_seed_set:{'5' * 64}",
        "seed_partition_sha256": "6" * 64,
        "theorem_partition_sha256": "7" * 64,
        "representation_partition_sha256": "8" * 64,
        "output_root": str(tmp_path / "output"),
        "families": plans,
        "python_executable": "/python-test",
    }
    placeholder = CompositionFullLaunchSpec.model_construct(_fields_set=None, **payload)
    launch_id = "detcomp_full_launch:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "launch_id")
    )
    return CompositionFullLaunchSpec.model_validate({**payload, "launch_id": launch_id})


def _receipt(plan: FullFamilyPlan, log_path: Path, *, reused: bool) -> FullRootReceipt:
    return FullRootReceipt(
        family=plan.family,
        run_kind=plan.run_kind,
        profile_id=plan.profile_id,
        root_path=str(Path(plan.output_root).resolve()),
        reused=reused,
        root_binding_id=f"detprov_root:{plan.family.encode().hex():0<64}",
        root_tree_hash="9" * 64,
        run_spec_sha256="a" * 64,
        manifest_sha256="b" * 64,
        results_sha256="c" * 64,
        terminal_status_counts={"not_applicable": 3941},
        provisional_count=0,
        log_sha256=hash_file(log_path),
    )


class Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.active = 0
        self.max_active = 0

    def execute(
        self,
        *,
        family: str,
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        lock_path: Path,
    ) -> int:
        del cwd, lock_path
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((family, tuple(command)))
            Path(command[command.index("--output-dir") + 1]).mkdir(parents=True, exist_ok=True)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"{family}\n", encoding="utf-8")
            return 0
        finally:
            self.active -= 1

    def terminate(self) -> None:
        raise AssertionError("not expected")


def test_full_commands_are_fixed_to_safe_serial_settings(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    for plan in spec.families:
        command = _family_command(spec, plan)
        assert command[command.index("--workers") + 1] == "1"
        assert command[command.index("--batch-size") + 1] == "64"
        assert "--memory-hard-limit-mb" not in command
        assert "--max-sources" not in command
        assert "--resume" not in command


def test_full_launcher_runs_13_serially_and_writes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    executor = Executor()
    monkeypatch.setattr(launcher, "_build_spec", lambda **_kwargs: spec)

    def validate(**kwargs: object) -> FullRootReceipt:
        plan = kwargs["plan"]
        log_path = kwargs["log_path"]
        reused = kwargs["reused"]
        assert isinstance(plan, FullFamilyPlan)
        assert isinstance(log_path, Path)
        assert isinstance(reused, bool)
        return _receipt(plan, log_path, reused=reused)

    monkeypatch.setattr(launcher, "_validate_full_root", validate)
    receipt = run_composition_full_scale(
        code_root=tmp_path / "unused-code",
        expected_commit="a" * 40,
        seed_dir=tmp_path / "unused-seed",
        project_dir=tmp_path / "unused-project",
        output_root=Path(spec.output_root),
        process_executor=executor,
    )
    assert tuple(item[0] for item in executor.calls) == tuple(
        item.key for item in FAMILY_DEFINITIONS
    )
    assert executor.max_active == 1
    assert len(receipt.roots) == 13
    assert (Path(spec.output_root) / "orchestration/receipt.json").is_file()


def test_full_launcher_resumes_partial_existing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    Path(spec.families[0].output_root).mkdir(parents=True)
    monkeypatch.setattr(launcher, "_build_spec", lambda **_kwargs: spec)
    executor = Executor()

    def validate(**kwargs: object) -> FullRootReceipt:
        plan = kwargs["plan"]
        log_path = kwargs["log_path"]
        reused = kwargs["reused"]
        assert isinstance(plan, FullFamilyPlan)
        assert isinstance(log_path, Path)
        assert isinstance(reused, bool)
        if plan.family == "p14" and not executor.calls:
            raise CompositionFullLaunchError("partial")
        return _receipt(plan, log_path, reused=reused)

    monkeypatch.setattr(launcher, "_validate_full_root", validate)
    receipt = run_composition_full_scale(
        code_root=tmp_path / "unused-code",
        expected_commit="a" * 40,
        seed_dir=tmp_path / "unused-seed",
        project_dir=tmp_path / "unused-project",
        output_root=Path(spec.output_root),
        process_executor=executor,
    )
    assert tuple(item[0] for item in executor.calls) == tuple(
        item.key for item in FAMILY_DEFINITIONS
    )
    assert receipt.roots[0].family == "p14"
    assert receipt.roots[0].reused is False


def test_full_launcher_conflicting_partial_root_fails_through_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    Path(spec.families[0].output_root).mkdir(parents=True)
    monkeypatch.setattr(launcher, "_build_spec", lambda **_kwargs: spec)
    monkeypatch.setattr(
        launcher,
        "_validate_full_root",
        lambda **_kwargs: (_ for _ in ()).throw(CompositionFullLaunchError("conflict")),
    )

    class FailingExecutor(Executor):
        def execute(self, **kwargs: object) -> int:
            family = kwargs["family"]
            command = kwargs["command"]
            assert isinstance(family, str)
            assert isinstance(command, Sequence)
            self.calls.append((family, tuple(command)))
            return 1

    executor = FailingExecutor()
    with pytest.raises(CompositionFullLaunchError, match="exited 1"):
        run_composition_full_scale(
            code_root=tmp_path / "unused-code",
            expected_commit="a" * 40,
            seed_dir=tmp_path / "unused-seed",
            project_dir=tmp_path / "unused-project",
            output_root=Path(spec.output_root),
            process_executor=executor,
        )
    assert tuple(item[0] for item in executor.calls) == ("p14",)
