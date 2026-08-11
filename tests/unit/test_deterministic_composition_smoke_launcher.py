from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import leanfaith.transforms.composition_smoke_launcher as launcher
from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.transforms.composition_smoke_launcher import (
    FAMILY_DEFINITIONS,
    CompositionSmokeLaunchError,
    CompositionSmokeLaunchSpec,
    SmokeFamilyPlan,
    SmokeRootReceipt,
    _family_command,
    _without_id,
    run_composition_smokes,
)


def _spec(tmp_path: Path, *, reused_p14: bool = False) -> CompositionSmokeLaunchSpec:
    code = tmp_path / "code"
    source = tmp_path / "source"
    project = tmp_path / "project"
    output = tmp_path / "output"
    for path in (code, source, project, output):
        path.mkdir(exist_ok=True)
    plans = tuple(
        SmokeFamilyPlan(
            family=item.key,
            run_kind=item.run_kind,
            profile_id=item.profile_id,
            profile_path=str(code / item.profile_name),
            profile_file_sha256="1" * 64,
            output_root=str(
                tmp_path / "existing-p14" if reused_p14 and item.key == "p14" else output / item.key
            ),
            reuse_root=reused_p14 and item.key == "p14",
            producer_commit_attestation="a" * 40 if reused_p14 and item.key == "p14" else None,
        )
        for item in FAMILY_DEFINITIONS
    )
    payload: dict[str, object] = {
        "launch_id": f"detcomp_smoke_launch:{'0' * 64}",
        "code_root": str(code),
        "expected_commit": "a" * 40,
        "code_tree_hash": "2" * 64,
        "project_dir": str(project),
        "project_revision": "b" * 40,
        "project_tree": "c" * 40,
        "lean_toolchain_sha256": "3" * 64,
        "source_dir": str(source),
        "source_manifest_sha256": "4" * 64,
        "source_seed_set_id": f"detcomp_seed_set:{'5' * 64}",
        "theorem_partition_sha256": "6" * 64,
        "representation_partition_sha256": "7" * 64,
        "output_root": str(output),
        "families": plans,
        "python_executable": "/python-test",
    }
    placeholder = CompositionSmokeLaunchSpec.model_construct(_fields_set=None, **payload)
    identity = "detcomp_smoke_launch:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "launch_id")
    )
    return CompositionSmokeLaunchSpec.model_validate({**payload, "launch_id": identity})


def _root_receipt(plan: SmokeFamilyPlan, log_path: Path) -> SmokeRootReceipt:
    return SmokeRootReceipt(
        family=plan.family,
        run_kind=plan.run_kind,
        profile_id=plan.profile_id,
        root_path=str(Path(plan.output_root).resolve()),
        reused=plan.reuse_root,
        producer_commit_attestation=plan.producer_commit_attestation,
        root_binding_id=f"detprov_root:{plan.family.encode().hex():0<64}",
        root_tree_hash="8" * 64,
        run_spec_sha256="9" * 64,
        manifest_sha256="a" * 64,
        results_sha256="b" * 64,
        terminal_status_counts={"not_applicable": 64},
        provisional_count=0,
        log_sha256=hash_file(log_path),
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.active = 0
        self.maximum_active = 0

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
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            self.calls.append((family, tuple(command)))
            Path(command[command.index("--output-dir") + 1]).mkdir(parents=True)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"{family}\n", encoding="utf-8")
            return 0
        finally:
            self.active -= 1

    def terminate(self) -> None:
        raise AssertionError("not expected")


def test_registry_and_child_commands_fix_safe_execution_settings(tmp_path: Path) -> None:
    assert tuple(item.key for item in FAMILY_DEFINITIONS) == (
        "p14",
        "p18",
        "n18",
        "n11",
        "n12",
        "p15",
        "p16",
        "p17",
        "n13",
        "n14",
        "n15",
        "n16",
        "n17",
    )
    assert len({item.key for item in FAMILY_DEFINITIONS}) == 13
    spec = _spec(tmp_path)
    for plan in spec.families:
        command = _family_command(spec=spec, plan=plan)
        assert command[3] == (
            "materialize-deterministic-v2-e2-scale"
            if plan.run_kind == "e2"
            else "materialize-deterministic-v2-d0-scale"
        )
        assert command[command.index("--workers") + 1] == "1"
        assert command[command.index("--batch-size") + 1] == "64"
        assert "--memory-hard-limit-mb" not in command
        assert "--resume" not in command


def test_launcher_runs_all_families_serially_and_writes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    executor = RecordingExecutor()
    monkeypatch.setattr(launcher, "_build_spec", lambda **_kwargs: spec)

    def validate(**kwargs: object) -> SmokeRootReceipt:
        plan = kwargs["plan"]
        log_path = kwargs["log_path"]
        assert isinstance(plan, SmokeFamilyPlan)
        assert isinstance(log_path, Path)
        return _root_receipt(plan, log_path)

    monkeypatch.setattr(launcher, "_validate_root", validate)

    receipt = run_composition_smokes(
        code_root=tmp_path / "unused-code",
        expected_commit="a" * 40,
        source_dir=tmp_path / "unused-source",
        project_dir=tmp_path / "unused-project",
        output_root=Path(spec.output_root),
        process_executor=executor,
    )

    assert tuple(family for family, _ in executor.calls) == tuple(
        item.key for item in FAMILY_DEFINITIONS
    )
    assert executor.maximum_active == 1
    assert len(receipt.roots) == 13
    assert receipt.training_eligible is False
    assert (Path(spec.output_root) / "orchestration/receipt.json").is_file()


def test_launcher_registers_explicit_existing_p14_without_recomputation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path, reused_p14=True)
    Path(spec.families[0].output_root).mkdir()
    executor = RecordingExecutor()
    monkeypatch.setattr(launcher, "_build_spec", lambda **_kwargs: spec)

    def validate(**kwargs: object) -> SmokeRootReceipt:
        plan = kwargs["plan"]
        log_path = kwargs["log_path"]
        assert isinstance(plan, SmokeFamilyPlan)
        assert isinstance(log_path, Path)
        return _root_receipt(plan, log_path)

    monkeypatch.setattr(launcher, "_validate_root", validate)

    receipt = run_composition_smokes(
        code_root=tmp_path / "unused-code",
        expected_commit="a" * 40,
        source_dir=tmp_path / "unused-source",
        project_dir=tmp_path / "unused-project",
        output_root=Path(spec.output_root),
        process_executor=executor,
    )

    assert "p14" not in {family for family, _ in executor.calls}
    assert receipt.roots[0].family == "p14"
    assert receipt.roots[0].reused is True
    assert receipt.roots[0].producer_commit_attestation == "a" * 40


def test_launcher_rejects_existing_partial_root_instead_of_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    Path(spec.families[0].output_root).mkdir(parents=True)
    monkeypatch.setattr(launcher, "_build_spec", lambda **_kwargs: spec)
    monkeypatch.setattr(
        launcher,
        "_validate_root",
        lambda **_kwargs: (_ for _ in ()).throw(
            CompositionSmokeLaunchError("partial, different, or invalid")
        ),
    )
    executor = RecordingExecutor()

    with pytest.raises(CompositionSmokeLaunchError, match="partial"):
        run_composition_smokes(
            code_root=tmp_path / "unused-code",
            expected_commit="a" * 40,
            source_dir=tmp_path / "unused-source",
            project_dir=tmp_path / "unused-project",
            output_root=Path(spec.output_root),
            process_executor=executor,
        )
    assert executor.calls == []
