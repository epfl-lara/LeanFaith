"""Offline exact-replay tests for the persisted LF-021 RCP v2 qualification."""

from __future__ import annotations

from pathlib import Path

from leanfaith.generation.rcp_qualification_verify_v2 import verify_rcp_qualification_v2

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/rcp_kimi_qualification_v2.yaml"
OUTPUT = (
    ROOT / "data/raw/real_outputs/rcp_kimi_qualification_v2/v2/"
    "7f5b578dd21cf696dd1222dcc176927383fc44e3b6fb10f7f96e2ff8c23dc497"
)
LEAN_RAW = (
    ROOT / "reports/generation/lf021_rcp_kimi_qualification_v2_lean_raw/"
    "f3ca7723f2d4433549c0226011105bbba26056532464e6573afee36b5b161b9e"
    ".d2da253d.json"
)


def test_exact_offline_replay_is_idempotent(tmp_path: Path) -> None:
    report = tmp_path / "verification.json"
    first = verify_rcp_qualification_v2(
        repo_root=ROOT,
        config_path=CONFIG,
        output_directory=OUTPUT,
        lean_raw_path=LEAN_RAW,
        report_path=report,
        credential="unit-test-secret-not-present",
    )
    second = verify_rcp_qualification_v2(
        repo_root=ROOT,
        config_path=CONFIG,
        output_directory=OUTPUT,
        lean_raw_path=LEAN_RAW,
        report_path=report,
        credential="unit-test-secret-not-present",
    )

    assert first.report_sha256 == second.report_sha256
    assert first.report.verification_id == second.report.verification_id
    assert first.report.provider_calls_performed == 0
    assert first.report.network_requests_performed == 0
    assert first.report.lean_operational_validation.operational_status == "valid_with_sorry"
    assert first.report.semantic_faithfulness_assessed is False
    assert first.report.semantic_labels_created is False
    assert first.report.gate_credit_claimed is False
