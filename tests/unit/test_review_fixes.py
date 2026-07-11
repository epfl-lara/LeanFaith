"""Regression tests for the consolidated review findings (codex + deep review).

Each test pins one confirmed defect from the LF-010-era adversarial review so
it can never silently return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config import (
    CanonicalizationError,
    ConfigError,
    StrictModel,
    canonical_json_bytes,
    hash_canonical,
    load_config,
    load_yaml_mapping,
)
from leanfaith.config.models import SecretRef
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.lean.response_normalization import classify_response, normalize_response
from leanfaith.lean.versions import LeanVersionError, parse_lean_version
from leanfaith.schemas import (
    FaithfulnessLevels,
    InvalidIdError,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    make_id,
)
from tests.unit.record_factories import resolved_label

_CTX = "ctx:" + "0" * 64

# --- labels: contradictory evidence rejected ---


def test_positive_label_with_refuted_direction_rejected() -> None:
    with pytest.raises(ValueError, match="refuted truth direction"):
        resolved_label(
            truth_A_implies_B=False,
            truth_B_implies_A=True,
            faithfulness_levels=FaithfulnessLevels(F1_same_claim=True, F2_truth_equivalent=False),
        )


def test_negative_label_with_refuted_direction_accepted() -> None:
    label = resolved_label(
        same_claim=False,
        resolution_outcome=ResolutionOutcome.NOT_SAME_CLAIM,
        relation=RelationLabel.A_STRONGER,
        truth_A_implies_B=True,
        truth_B_implies_A=False,
        faithfulness_levels=FaithfulnessLevels(F1_same_claim=False, F2_truth_equivalent=False),
        quality_tier=QualityTier.GOLD_COUNTEREXAMPLE,
        resolution_method="directional_proof_plus_separator",
        eval_eligibility=True,
    )
    assert label.faithfulness_levels.F2_truth_equivalent is False


# --- normalization: sorry spellings and root-goal separation ---


def _request(**overrides: object) -> LeanRequest:
    payload: dict[str, object] = {
        "request_id": "r",
        "context_id": _CTX,
        "code": "theorem t : True := trivial",
    }
    payload.update(overrides)
    return LeanRequest(**payload)  # type: ignore[arg-type]


def test_single_quote_sorry_diagnostic_recognized() -> None:
    raw = {
        "messages": [{"severity": "warning", "data": "declaration uses 'sorry'"}],
        "sorries": [{"goal": "⊢ True", "proof_state": 0}],
    }
    assert classify_response(raw, root_goals_requested=True) == LeanStatus.VALID_WITH_SORRY


def test_invalid_response_with_root_goals_gets_no_phantom_sorries() -> None:
    raw = {
        "messages": [{"severity": "error", "data": "Type mismatch"}],
        "sorries": [{"goal": "⊢ False", "proof_state": 0}],
    }
    result = normalize_response(
        _request(root_goals=True),
        raw,
        request_hash="h" * 64,
        context_fingerprint="0" * 64,
        elapsed_ms=1,
        raw_response_path=None,
    )
    assert result.status == LeanStatus.INVALID
    assert result.sorries == ()  # root-goal entries are not admissions
    assert result.root_goals == ("⊢ False",)


# --- versions: sentinel headroom ---


def test_implausible_rc_number_rejected() -> None:
    with pytest.raises(LeanVersionError, match="implausible"):
        parse_lean_version("v4.31.0-rc1000000")


# --- hashing: -0.0 normalization ---


def test_negative_zero_normalized() -> None:
    assert hash_canonical({"x": -0.0}) == hash_canonical({"x": 0.0})
    assert canonical_json_bytes({"x": -0.0}) == b'{"x":0.0}'


# --- ids: broadened machine-local guard ---


@pytest.mark.parametrize(
    "bad",
    [
        "/var/tmp/run/x.lean",
        "/storage/someone/data/x.jsonl",
        "/opt/data/lean/corpus.jsonl",  # full-string absolute path shape
        "C:\\Users\\someone\\data.lean",
    ],
)
def test_absolute_path_shapes_rejected(bad: str) -> None:
    with pytest.raises(InvalidIdError):
        make_id("thm", {"file": bad})


def test_machine_local_mapping_keys_rejected() -> None:
    local = str(Path.home() / "private")
    with pytest.raises(InvalidIdError, match="machine-local"):
        make_id("thm", {local: "x"})


def test_lean_code_with_slashes_still_allowed() -> None:
    # Lean code contains spaces, so the full-string path pattern never fires.
    assert make_id("thm", {"code": "/- header -/ theorem t : 1 / 2 = 0 := rfl"})
    assert make_id("thm", {"view": "a / b / c"})


# --- loader: recursive strictness + secret redaction ---


def test_yaml_set_tag_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("globs: !!set\n  ? A\n  ? B\n")
    with pytest.raises(ConfigError, match="unsupported YAML node type"):
        load_yaml_mapping(path)


def test_yaml_bare_date_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("probe_date: 2026-07-10\n")
    with pytest.raises(ConfigError, match="quote dates"):
        load_yaml_mapping(path)


def test_yaml_nested_non_string_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("outer:\n  1: x\n")
    with pytest.raises(ConfigError, match="non-string key"):
        load_yaml_mapping(path)


class _SecretConfig(StrictModel):
    token: SecretRef


def test_validation_error_never_echoes_input(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("token: hf_SUPERSECRETVALUE123\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, _SecretConfig)
    assert "hf_SUPERSECRETVALUE123" not in str(excinfo.value)


def test_datetime_still_rejected_in_canonical() -> None:
    import datetime

    with pytest.raises(CanonicalizationError):
        hash_canonical({"t": datetime.date(2026, 7, 10)})


# --- round 2: deep-review confirmed findings ---


def test_memory_error_maps_to_crash() -> None:
    from leanfaith.lean.response_normalization import status_for_exception

    assert status_for_exception(MemoryError("restart attempts exhausted")) == LeanStatus.CRASH


def test_trailing_newline_ids_rejected() -> None:
    from leanfaith.schemas import is_valid_id, parse_id

    tainted = "thm:" + "0" * 64 + "\n"
    assert not is_valid_id(tainted)
    with pytest.raises(InvalidIdError):
        parse_id(tainted)
    with pytest.raises(InvalidIdError, match="prefix"):
        make_id("thm\n", {"a": 1})


def test_toolchain_lock_coherence_enforced() -> None:
    from leanfaith.lean.project_registry import ToolchainLock, ToolchainMode

    with pytest.raises(ValueError, match="mixed"):
        ToolchainLock(
            mode=ToolchainMode.STABLE_V4_31_EXCEPTION,
            accepted_lean="v4.31.0-rc1",
            stable_v4_31_exception_adr="ADR-0001",
        )
    with pytest.raises(ValueError, match="ADR-0001"):
        ToolchainLock(mode=ToolchainMode.STABLE_V4_31_EXCEPTION, accepted_lean="v4.31.0")
    with pytest.raises(ValueError, match="in-range"):
        ToolchainLock(mode=ToolchainMode.ADVERTISED_RANGE, accepted_lean="v4.32.0-rc1")
    with pytest.raises(ValueError, match="must be null"):
        ToolchainLock(
            mode=ToolchainMode.ADVERTISED_RANGE,
            accepted_lean="v4.31.0-rc1",
            stable_v4_31_exception_adr="ADR-0001",
        )


def test_judgment_value_rejects_non_canonical_spellings() -> None:
    from leanfaith.schemas import JudgmentValue

    with pytest.raises(ValueError):
        JudgmentValue(answer="same claim")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        JudgmentValue(answer="same_claim", relation="A stronger")  # type: ignore[arg-type]
    value = JudgmentValue(answer="same_claim", relation="A_stronger")
    assert value.relation == "A_stronger"


def test_manifest_rejects_duplicate_keys_and_nan(tmp_path: Path) -> None:
    from leanfaith.schemas import ManifestError, RunManifest, read_manifest

    target = tmp_path / "m.json"
    target.write_text('{"a": 1, "a": 2}')
    with pytest.raises(ManifestError, match="duplicate JSON key"):
        read_manifest(target, RunManifest)
    target.write_text('{"schema_version": NaN}')
    with pytest.raises(ManifestError, match="non-finite JSON constant"):
        read_manifest(target, RunManifest)


def test_manifest_schema_version_mismatch_fails_closed(tmp_path: Path) -> None:
    import datetime

    from leanfaith.schemas import (
        ArtifactClass,
        CodeState,
        ManifestError,
        RunManifest,
        new_run_id,
        read_manifest,
        write_manifest,
    )

    now = datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC)
    manifest = RunManifest(
        run_id=new_run_id(now, nonce="deadbeef"),
        artifact_class=ArtifactClass.SMOKE,
        command="x",
        code=CodeState(git_revision="a" * 40, git_dirty=False),
        created_at=now,
    )
    target = tmp_path / "m.json"
    write_manifest(manifest, target)
    tampered = target.read_text().replace('"schema_version":1', '"schema_version":99')
    target.write_text(tampered)
    with pytest.raises(ManifestError, match="migrate explicitly"):
        read_manifest(target, RunManifest)


def test_run_manifest_rejects_non_finite_measurements() -> None:
    import datetime
    import math

    from leanfaith.schemas import ArtifactClass, CodeState, RunManifest, new_run_id

    now = datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC)
    with pytest.raises(ValueError, match="finite"):
        RunManifest(
            run_id=new_run_id(now, nonce="deadbeef"),
            artifact_class=ArtifactClass.SMOKE,
            command="x",
            code=CodeState(git_revision="a" * 40, git_dirty=False),
            measurements={"loss": math.nan},
            created_at=now,
        )


def test_symlinked_home_form_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "real_home"
    target.mkdir()
    link = tmp_path / "home_link"
    link.symlink_to(target)
    monkeypatch.setenv("HOME", str(link))
    # Both the symlinked and the resolved spellings must be rejected.
    for form in (str(link / "data" / "x.lean"), str(target / "data" / "x.lean")):
        with pytest.raises(InvalidIdError):
            make_id("thm", {"file": form})


def test_setup_error_status_for_broken_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend

    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
        )
    )

    def boom() -> object:
        raise RuntimeError("project build failed")

    monkeypatch.setattr(backend, "_ensure_server", boom)
    result = backend.run(_request())
    assert result.status == LeanStatus.SETUP_ERROR
    assert result.raw_response_path is not None


def test_auto_mode_falls_back_to_stable_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import leanfaith.lean.leaninteract_backend as backend_module
    from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
    from leanfaith.lean.session_policy import ServerMode

    class FakeStable:
        def __init__(self, config: object) -> None:
            self.config = config

    def raising_auto(config: object) -> object:
        raise RuntimeError("experimental server unavailable")

    monkeypatch.setattr(backend_module, "AutoLeanServer", raising_auto)
    monkeypatch.setattr(backend_module, "LeanServer", FakeStable)
    monkeypatch.setattr(backend_module.LeanInteractBackend, "_repl_config", lambda self: object())
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.AUTO,
        )
    )
    server = backend._ensure_server()
    assert isinstance(server, FakeStable)
    assert backend.auto_fallback_active


def test_label_link_detects_target_id_mismatch() -> None:
    from leanfaith.schemas import check_label_target_link
    from tests.unit.record_factories import LABEL_ID, THM_A, THM_B, pair_record

    other_pair = pair_record(
        pair_id=make_id("pair", {"different": True}),
        resolved_label_id=LABEL_ID,
    )
    label = resolved_label()  # targets the canonical PAIR_ID
    violations = check_label_target_link(label, other_pair)
    assert any("target_id" in violation for violation in violations)
    assert THM_A != THM_B  # silence unused-import style pruning


def test_doctor_memory_product_warning(tmp_path: Path) -> None:
    import subprocess

    from leanfaith.cli.doctor import run_doctor
    from leanfaith.config.paths import RepoPaths

    (tmp_path / "PLAN.md").write_text("plan\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "configs" / "projects").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "configs" / "environment.lock.yaml").write_text(
        "environment_schema_version: 1\n"
        "python:\n  version: '3.12'\n"
        "lean_interact:\n"
        "  package: lean-interact\n"
        "  version: 0.11.4\n"
        "  advertised_lean_min: v4.8.0-rc1\n"
        "  advertised_lean_max: v4.31.0-rc1\n"
        "  repl_fork: https://github.com/augustepoiroux/repl\n"
        "toolchain_lock:\n"
        "  mode: advertised_range\n"
        "  accepted_lean: v4.31.0-rc1\n"
        "lean_backend:\n"
        "  workers: 1000000\n"
        "  memory_hard_limit_mb: 1000000\n"
    )
    report = run_doctor(RepoPaths(root=tmp_path))
    assert any("exceeds detected RAM" in warning for warning in report.warnings)
