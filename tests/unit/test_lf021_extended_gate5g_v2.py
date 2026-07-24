from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.paths import RepoPaths
from leanfaith.generation import extended_gate5g as module
from leanfaith.generation import gate5g as gate5g_v1
from leanfaith.generation import post_exhaustion_frame_v1 as frame_module
from leanfaith.generation import tranche_expansion as tranche_v1
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GObservationBinding,
    Gate5GScopeLimitations,
)
from leanfaith.schemas.gate5g_v2 import (
    ExtendedGate5GAuthorizationBindingV2,
    ExtendedGate5GInputBindingsV2,
    ExtendedGate5GLineageBindingsV2,
    ExtendedGate5GValidationReportV2,
)

ROOT = Path(__file__).resolve().parents[2]
_H = "a" * 64
_H2 = "b" * 64
_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)


def _write(root: Path, relative: str, content: bytes = b"{}\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _artifact(root: Path, relative: str, content: bytes = b"{}\n") -> Gate5GArtifactBinding:
    path = _write(root, relative, content)
    return Gate5GArtifactBinding(artifact=relative, sha256=hash_file(path))


def _tranche_artifact(
    root: Path,
    relative: str,
    content: bytes = b"{}\n",
) -> tranche_v1.ArtifactBinding:
    return tranche_v1.ArtifactBinding.model_validate(
        _artifact(root, relative, content).model_dump(mode="json")
    )


def _scope() -> Gate5GScopeLimitations:
    return Gate5GScopeLimitations(
        scalable_family_ids=_FAMILIES,
        three_family_collection_only=True,
        reduced_data_ablation=True,
        confirmatory_d4_d5_eligible=False,
        heldout_generator_claim_eligible=False,
        supplemental_qualifications_count_for_gate_credit=False,
        reduced_data_reasons=(
            "confirmatory_d4_d5_unavailable",
            "heldout_generator_claim_unavailable",
            "three_family_collection_only",
        ),
    )


def _observation(index: int) -> Gate5GObservationBinding:
    return Gate5GObservationBinding(
        artifact=f"reports/observation_{index}.json",
        sha256=f"{index + 1:064x}",
        manifest_id=f"manifest-{index}",
        tranche_id=f"tranche-{index}",
    )


def _authorization(
    *,
    order: int = 12,
) -> ExtendedGate5GAuthorizationBindingV2:
    return ExtendedGate5GAuthorizationBindingV2(
        authorization_id=("lf021_reviewed_extension_collection_authorization_v1:" + _H),
        authorization=Gate5GArtifactBinding(
            artifact="reports/authorization.json",
            sha256=_H,
        ),
        extension_decision_id="extension-decision",
        extension_decision=Gate5GArtifactBinding(
            artifact="reports/extension_decision.json",
            sha256=_H2,
        ),
        authorized_tranche_id=f"extension-{order}",
        authorized_tranche_order=order,
    )


def test_extended_lineage_schema_requires_contiguous_authorizations() -> None:
    common: dict[str, Any] = {
        "activation_v2_decision_id": "activation",
        "activation_v2_decision": {
            "artifact": "reports/activation.json",
            "sha256": _H,
        },
        "extension_stop_decision_id": "stop",
        "extension_stop_decision": {
            "artifact": "reports/stop.json",
            "sha256": _H,
        },
        "extension_policy": {
            "artifact": "configs/extension.yaml",
            "sha256": _H,
        },
        "extension_implementation": {
            "artifact": "src/extension.py",
            "sha256": _H,
        },
        "collection_authorization_policy": {
            "artifact": "configs/auth.yaml",
            "sha256": _H,
        },
        "collection_authorization_implementation": {
            "artifact": "src/auth.py",
            "sha256": _H,
        },
        "original_observation_count": 12,
        "extension_observation_count": 1,
        "observations": tuple(_observation(index) for index in range(13)),
        "lineage_manifest_id": "lf021_gate5g_lineage:" + _H,
        "lineage_manifest": {
            "artifact": "reports/lineage.json",
            "sha256": _H,
        },
    }
    ExtendedGate5GLineageBindingsV2(
        **common,
        authorizations=(_authorization(order=12),),
    )
    with pytest.raises(ValueError, match="orders are not contiguous"):
        ExtendedGate5GLineageBindingsV2(
            **common,
            authorizations=(_authorization(order=13),),
        )


def test_validation_schema_content_id_and_scope_are_coherent() -> None:
    lineage = ExtendedGate5GLineageBindingsV2(
        activation_v2_decision_id="activation",
        activation_v2_decision=Gate5GArtifactBinding(
            artifact="reports/activation.json",
            sha256=_H,
        ),
        extension_stop_decision_id="stop",
        extension_stop_decision=Gate5GArtifactBinding(
            artifact="reports/stop.json",
            sha256=_H,
        ),
        extension_policy=Gate5GArtifactBinding(
            artifact="configs/extension.yaml",
            sha256=_H,
        ),
        extension_implementation=Gate5GArtifactBinding(
            artifact="src/extension.py",
            sha256=_H,
        ),
        collection_authorization_policy=Gate5GArtifactBinding(
            artifact="configs/auth.yaml",
            sha256=_H,
        ),
        collection_authorization_implementation=Gate5GArtifactBinding(
            artifact="src/auth.py",
            sha256=_H,
        ),
        original_observation_count=12,
        extension_observation_count=1,
        observations=tuple(_observation(index) for index in range(13)),
        authorizations=(_authorization(),),
        lineage_manifest_id="lf021_gate5g_lineage:" + _H,
        lineage_manifest=Gate5GArtifactBinding(
            artifact="reports/lineage.json",
            sha256=_H,
        ),
    )
    artifact = Gate5GArtifactBinding(artifact="reports/input.json", sha256=_H)
    inputs: dict[str, Any] = dict.fromkeys(
        (
            "policy",
            "implementation",
            "prevalence_design_v3",
            "prevalence_design_v2",
            "prevalence_design_v1",
            "prevalence_design_v3_implementation",
            "frame_freeze_decision",
            "frame_materializer_policy",
            "frame_materializer_implementation",
            "population_manifest",
            "population_artifact",
            "frame",
            "sampling_seed_provenance",
            "sampling_seed",
            "sampling_seed_lock",
            "coverage_report",
            "phase_milestone",
        ),
        artifact,
    )
    inputs["lineage"] = lineage
    inputs["external_beacon_provenance"] = None
    input_record = ExtendedGate5GInputBindingsV2.model_validate(inputs)
    payload: dict[str, Any] = {
        "schema_version": 2,
        "validation_status": "ready_to_finalize",
        "input_bindings": input_record.model_dump(mode="json"),
        "prevalence_design_policy_id": "lf021_prevalence_design_v3",
        "frame_freeze_decision_id": ("lf021_extended_frame_freeze_decision_v1:" + _H),
        "frame_id": "lf021_extended_prevalence_frame_v1:" + _H,
        "frame_item_count": 240,
        "sampling_method": ("problem_aware_stratified_csprng_srs_without_replacement_v2"),
        "sampling_seed_sha256": _H,
        "sampling_seed_source": "os_csprng_secrets_token_bytes_256",
        "original_observation_count": 12,
        "extension_observation_count": 1,
        "observed_tranche_count": 13,
        "scalable_family_ids": _FAMILIES,
        "pool_ids": ("pool",),
        "source_proxies": ("proxy",),
        "family_item_counts": {
            "goedel_formalizer_v2_8b": 80,
            "kimina_autoformalizer_7b": 80,
            "stepfun_formalizer_7b": 80,
        },
        "pool_item_counts": {"pool": 240},
        "source_proxy_item_counts": {"proxy": 240},
        "strata": ({"stratum": "all", "population_size": 300, "sample_size": 240},),
        "scope_limitations": _scope().model_dump(mode="json"),
        "reform_applicability": {
            "applicable": False,
            "status": "not_applicable",
            "reason": "no ReForm family",
            "overlap_report": None,
        },
        "completed_checks": {"strict_replay": True},
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "remote_provider_content_used": False,
        "gate_5g_closed": False,
        "gate_5_closed": False,
    }
    validation_id = "lf021_extended_gate5g_validation_v2:" + hash_canonical(
        {"schema": "lf021_extended_gate5g_validation_v2", **payload}
    )
    record = ExtendedGate5GValidationReportV2.model_validate(
        {"validation_id": validation_id, **payload}
    )
    assert record.validation_id == validation_id
    with pytest.raises(ValueError, match="validation ID differs"):
        ExtendedGate5GValidationReportV2.model_validate(
            {"validation_id": "lf021_extended_gate5g_validation_v2:" + _H2, **payload}
        )


def test_checked_in_policy_replays_exact_bound_implementations() -> None:
    root = Path(__file__).resolve().parents[2]
    loaded = module.load_extended_gate5g_policy(
        root / "configs/generation/lf021_gate5g_finalizer_v2.yaml"
    )
    module._verify_policy_lineage(paths=RepoPaths(root), policy=loaded.config)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"transport": "openai"}, "non-local"),
        ({"external_transmission": True}, "external provider"),
        ({"same_claim": True}, "semantic label"),
        ({"relation": "equivalent"}, "semantic label"),
        ({"supervision_eligible": True}, "non-false"),
    ],
)
def test_label_and_remote_scan_fails_closed(
    payload: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(module.ExtendedGate5GFinalizationError, match=match):
        module._label_and_remote_scan(payload, label="test")


def test_config_plan_only_authorization_cannot_receive_gate_credit() -> None:
    record = SimpleNamespace(
        scientific_tranche_authorized=True,
        executable_collection_adapter_available=False,
        config_plan_adapter_only=True,
        family_pins=(SimpleNamespace(transport="local"),),
        model_dump=lambda **_: {
            "transport": "local",
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5_closed": False,
        },
    )
    binding = SimpleNamespace(
        model_dump=lambda **_: {
            "artifact": "reports/authorization.json",
            "sha256": _H,
        }
    )
    observation = SimpleNamespace(
        postprocess_manifest=SimpleNamespace(
            artifact="reports/missing-postprocess-v7.json",
            sha256=_H,
        ),
        manifest_id="research_postprocess_v7_manifest:" + _H,
        postprocess_schema_version=7,
    )
    verified = SimpleNamespace(
        collection_authorizations=SimpleNamespace(
            records=(record,),
            bindings=(binding,),
        ),
        verified_stop=SimpleNamespace(
            decision=SimpleNamespace(extension_observations=(observation,))
        ),
    )
    with pytest.raises(
        module.ExtendedGate5GFinalizationError,
        match="execution evidence is unavailable",
    ):
        module._verify_authorizations_are_local(RepoPaths(ROOT), cast(Any, verified))


def _minimal_verified(
    *,
    decision_path: Path,
    authorization_paths: tuple[Path, ...] = (),
    test_replay_only: bool = False,
) -> SimpleNamespace:
    decision = SimpleNamespace(
        policy_id="lf021_post_exhaustion_frame_materializer_v1",
        source_stop_action="preferred_eligible_stop",
        action="freeze_preferred_frame",
        next_tranche=None,
        coverage_deficits=(),
        original_observation_count=12,
        extension_observation_count=1,
        frame=SimpleNamespace(item_count=240, test_replay_only=test_replay_only),
        sampling_method="problem_aware_stratified_csprng_srs_without_replacement_v2",
        sampling_rank_algorithm="hmac_sha256_keyed_rank_v1",
        representative_family_ids=_FAMILIES,
        test_replay_only=test_replay_only,
    )
    decision.model_dump = lambda **_: {
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5_closed": False,
    }
    return SimpleNamespace(
        decision=decision,
        decision_path=decision_path,
        collection_authorizations=SimpleNamespace(paths=authorization_paths),
        seed_provenance=SimpleNamespace(
            test_replay_only=test_replay_only,
            source=(
                "test_replay_seed_256" if test_replay_only else "os_csprng_secrets_token_bytes_256"
            ),
        ),
    )


def test_strict_extended_replay_failure_never_writes_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(
        frame_module,
        "verify_extended_frame_freeze_v1",
        lambda **_: (_ for _ in ()).throw(frame_module.PostExhaustionFrameError("strict failure")),
    )
    with pytest.raises(module.ExtendedGate5GFinalizationError, match="strict failure"):
        module.validate_or_finalize_extended_gate5g(
            paths=RepoPaths(root),
            frame_freeze_decision_path=root / "missing-decision.json",
            collection_authorization_paths=(),
            lineage_manifest_path=root / "missing-lineage.json",
            coverage_report_path=root / "missing-coverage.md",
            phase_milestone_path=root / "missing-phase.md",
        )


def test_test_entropy_is_rejected_before_lineage_or_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    decision_path = tmp_path / "decision.json"
    decision_path.write_text("{}\n", encoding="utf-8")
    verified = _minimal_verified(
        decision_path=decision_path,
        test_replay_only=True,
    )
    monkeypatch.setattr(
        frame_module,
        "verify_extended_frame_freeze_v1",
        lambda **_: verified,
    )
    monkeypatch.setattr(
        module,
        "_verify_complete_lineage",
        lambda **_: pytest.fail("lineage must not run for test entropy"),
    )
    with pytest.raises(
        module.ExtendedGate5GFinalizationError,
        match="test/replay entropy",
    ):
        module.validate_or_finalize_extended_gate5g(
            paths=RepoPaths(root),
            frame_freeze_decision_path=decision_path,
            collection_authorization_paths=(),
            lineage_manifest_path=root / "missing-lineage.json",
            coverage_report_path=root / "missing-coverage.md",
            phase_milestone_path=root / "missing-phase.md",
        )


def test_authorization_path_mismatch_is_rejected_before_scientific_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    decision_path = tmp_path / "decision.json"
    decision_path.write_text("{}\n", encoding="utf-8")
    actual = tmp_path / "actual-auth.json"
    supplied = tmp_path / "supplied-auth.json"
    actual.write_text("{}\n", encoding="utf-8")
    supplied.write_text("{}\n", encoding="utf-8")
    verified = _minimal_verified(
        decision_path=decision_path,
        authorization_paths=(actual,),
    )
    monkeypatch.setattr(
        frame_module,
        "verify_extended_frame_freeze_v1",
        lambda **_: verified,
    )
    with pytest.raises(
        module.ExtendedGate5GFinalizationError,
        match="supplied authorization paths differ",
    ):
        module.validate_or_finalize_extended_gate5g(
            paths=RepoPaths(root),
            frame_freeze_decision_path=decision_path,
            collection_authorization_paths=(supplied,),
            lineage_manifest_path=root / "missing-lineage.json",
            coverage_report_path=root / "missing-coverage.md",
            phase_milestone_path=root / "missing-phase.md",
        )


def test_finalize_flag_and_date_are_an_explicit_pair() -> None:
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(module.ExtendedGate5GFinalizationError, match="requires a date"):
        module.validate_or_finalize_extended_gate5g(
            paths=RepoPaths(root),
            frame_freeze_decision_path=root / "unused.json",
            collection_authorization_paths=(),
            lineage_manifest_path=root / "unused.json",
            coverage_report_path=root / "unused.md",
            phase_milestone_path=root / "unused.md",
            finalize=True,
        )
    with pytest.raises(module.ExtendedGate5GFinalizationError, match="dry-run forbids"):
        module.validate_or_finalize_extended_gate5g(
            paths=RepoPaths(root),
            frame_freeze_decision_path=root / "unused.json",
            collection_authorization_paths=(),
            lineage_manifest_path=root / "unused.json",
            coverage_report_path=root / "unused.md",
            phase_milestone_path=root / "unused.md",
            finalized_date=datetime.date(2026, 7, 24),
        )


def test_hardened_publication_rejects_symlinked_validation_namespace(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    namespace = tmp_path / "reports/generation/lf021_extended_gate5g_finalization_v2"
    namespace.parent.mkdir(parents=True)
    namespace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        gate5g_v1.Gate5GFinalizationError,
        match=r"symlink|not trusted",
    ):
        gate5g_v1._write_immutable(
            namespace / "report.json",
            b"{}\n",
            repo_root=tmp_path,
            label="extended validation",
        )


def test_close_extended_gate5g_cli_routes_dry_run_without_touching_v1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_finalize(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            validation_report_path=(
                "reports/generation/lf021_extended_gate5g_finalization_v2/synthetic.json"
            ),
            validation_report_sha256=_H,
            gate_report=None,
            gate_report_path=None,
            gate_report_sha256=None,
        )

    monkeypatch.setattr(module, "validate_or_finalize_extended_gate5g", fake_finalize)
    auth_a = tmp_path / "reports/auth-a.json"
    auth_b = tmp_path / "reports/auth-b.json"
    result = CliRunner().invoke(
        app,
        [
            "close-extended-gate5g",
            "--root",
            str(tmp_path),
            "--frame-freeze-decision",
            "reports/frame.json",
            "--collection-authorization",
            str(auth_a),
            "--collection-authorization",
            str(auth_b),
            "--lineage-manifest",
            "reports/lineage.json",
            "--coverage-report",
            "reports/coverage.md",
            "--phase-milestone",
            "reports/phase.md",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "gate_5g_closed=false" in result.output
    assert captured["paths"] == RepoPaths(tmp_path)
    assert captured["frame_freeze_decision_path"] == tmp_path / "reports/frame.json"
    assert captured["collection_authorization_paths"] == (auth_a, auth_b)
    assert captured["lineage_manifest_path"] == tmp_path / "reports/lineage.json"
    assert captured["prevalence_design_policy_path"] == (
        tmp_path / "policies/lf021_prevalence_design_v3.yaml"
    )
    assert captured["policy_path"] == (
        tmp_path / "configs/generation/lf021_gate5g_finalizer_v2.yaml"
    )
    assert captured["finalize"] is False
    assert captured["finalized_date"] is None
    assert not (tmp_path / "reports/gates/gate_5g.json").exists()


def test_close_extended_gate5g_cli_rejects_unpaired_finalize_before_inputs(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "close-extended-gate5g",
            "--root",
            str(tmp_path),
            "--frame-freeze-decision",
            "missing-frame.json",
            "--collection-authorization",
            "missing-authorization.json",
            "--lineage-manifest",
            "missing-lineage.json",
            "--coverage-report",
            "missing-coverage.md",
            "--phase-milestone",
            "missing-phase.md",
            "--finalize",
        ],
    )
    assert result.exit_code == 2
    assert "requires a date" in result.output
    assert not (tmp_path / "reports/gates/gate_5g.json").exists()


def test_v1_and_extended_gate_commands_are_both_registered() -> None:
    help_result = CliRunner().invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "close-gate5g" in help_result.output
    assert "close-extended-gate5g" in help_result.output
