from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.lf022 import run_lf022_validation
from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import ConfigError
from leanfaith.config.paths import RepoPaths
from leanfaith.generation.lf022_config import (
    ReplayKind,
    load_lf022_foundation_configs,
    validate_lf022_foundation,
)
from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    PrivateContentTransmissionError,
    ProviderIdentity,
    ProviderRequest,
    persist_provider_request,
)

ROOT = Path(__file__).resolve().parents[2]
VARIANTS_CONFIG = ROOT / "configs/generation/llm_variants_v1.yaml"
JUDGES_CONFIG = ROOT / "configs/judges/weak_supervision.yaml"
PROPOSER_PROMPT = ROOT / "prompts/proposers/lean_variant_v1.txt"
JUDGE_PROMPT = ROOT / "prompts/judges/lean_pair_blinded_v2.txt"


def _proposer_output() -> str:
    return json.dumps(
        {
            "variants": [
                {
                    "candidate_lean": "theorem replayed (n : Nat) : n ≤ n + 1",
                    "intended_relation": "near_miss",
                    "intended_error_types": ["E17"],
                    "edit_summary": "Changed the bound.",
                    "confidence": 0.7,
                    "assumptions": [],
                    "potential_ambiguity": None,
                }
            ]
        },
        ensure_ascii=False,
    )


def _judge_output() -> str:
    return json.dumps(
        {
            "same_claim_answer": "not_same_claim",
            "relation": "A_stronger",
            "A_implies_B": "yes",
            "B_implies_A": "no",
            "error_types": ["E17"],
            "confidence": 0.8,
            "rationale": "The visible statement A has a stricter claim.",
            "needs_expert_review": False,
        }
    )


def _persist_replay(
    tmp_path: Path,
    *,
    output: str,
    prompt_hash: str,
    private_source_content: bool = False,
) -> tuple[Path, Path]:
    identity = ProviderIdentity(
        provider="offline-fixture",
        model="fixture-model",
        revision="fixture-revision",
        transport="fixture",
    )
    request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=prompt_hash,
        rendered_prompt="Frozen replay prompt.",
        decoding={"temperature": 0.0},
        input_ids=("fixture-input",),
        private_source_content=private_source_content,
    )
    request_path = tmp_path / "request.json"
    persist_provider_request(request, request_path)
    raw_root = tmp_path / "raw"
    DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=raw_root,
        responses={request.request_hash: output},
    ).generate(request)
    return request_path, raw_root


def test_checked_in_lf022_foundation_is_typed_and_fail_closed() -> None:
    loaded = load_lf022_foundation_configs(
        paths=RepoPaths(root=ROOT),
        variants_path=VARIANTS_CONFIG,
        judges_path=JUDGES_CONFIG,
    )
    assert not loaded.variants.config.admission.live_calls_authorized
    assert not loaded.judges.config.admission.live_calls_authorized
    assert not loaded.variants.config.outputs.semantic_labels_created
    assert not loaded.judges.config.outputs.promoted_silver_write_enabled
    assert loaded.judges.config.aggregation.output_record == "WeakConsensusCandidateRecordV1"
    assert loaded.judges.config.prompt.template_version == "v2"
    assert loaded.proposer_prompt_sha256 == hash_file(PROPOSER_PROMPT)
    assert loaded.judge_prompt_sha256 == hash_file(JUDGE_PROMPT)


def test_typed_config_rejects_legacy_auto_promotion_record(tmp_path: Path) -> None:
    payload = yaml.safe_load(JUDGES_CONFIG.read_text(encoding="utf-8"))
    payload["aggregation"]["output_record"] = "SilverPromotionCandidateV1"
    config_path = tmp_path / "judges.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="WeakConsensusCandidateRecordV1"):
        load_lf022_foundation_configs(
            paths=RepoPaths(root=ROOT),
            variants_path=VARIANTS_CONFIG,
            judges_path=config_path,
        )


@pytest.mark.parametrize(
    ("kind", "prompt_path", "output"),
    [
        ("proposer", PROPOSER_PROMPT, _proposer_output()),
        ("judge", JUDGE_PROMPT, _judge_output()),
    ],
)
def test_network_free_replay_parses_without_semantic_outputs(
    tmp_path: Path,
    kind: ReplayKind,
    prompt_path: Path,
    output: str,
) -> None:
    request_path, raw_root = _persist_replay(
        tmp_path,
        output=output,
        prompt_hash=hash_file(prompt_path),
    )
    report = validate_lf022_foundation(
        paths=RepoPaths(root=ROOT),
        variants_path=VARIANTS_CONFIG,
        judges_path=JUDGES_CONFIG,
        replay_kind=kind,
        request_path=request_path,
        raw_response_root=raw_root,
    )
    assert report.replay is not None
    assert report.replay.parsed_item_count == 1
    assert not report.live_provider_calls_authorized
    assert not report.semantic_labels_created
    assert not report.silver_records_created


def test_replay_rejects_private_source_provider_artifact(tmp_path: Path) -> None:
    request_path, raw_root = _persist_replay(
        tmp_path,
        output=_proposer_output(),
        prompt_hash=hash_file(PROPOSER_PROMPT),
        private_source_content=True,
    )
    with pytest.raises(PrivateContentTransmissionError, match="private-source"):
        validate_lf022_foundation(
            paths=RepoPaths(root=ROOT),
            variants_path=VARIANTS_CONFIG,
            judges_path=JUDGES_CONFIG,
            replay_kind="proposer",
            request_path=request_path,
            raw_response_root=raw_root,
        )


def test_cli_validates_foundation_and_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        [
            "validate-lf022",
            "--root",
            str(ROOT),
            "--report",
            str(report_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "live_calls=0" in result.output
    assert "semantic_labels_created=0" in result.output
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "foundation_validated_no_live_calls"
    assert payload["replay"] is None


def test_cli_replaces_stale_foundation_report_with_current_effective_config_hashes(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    stale_payload = {
        "schema_version": 1,
        "status": "foundation_validated_no_live_calls",
        "variants_config_sha256": "0" * 64,
        "judges_config_sha256": "1" * 64,
        "proposer_prompt_sha256": "2" * 64,
        "judge_prompt_sha256": "3" * 64,
        "live_provider_calls_authorized": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "replay": None,
    }
    report_path.write_text(json.dumps(stale_payload) + "\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "validate-lf022",
            "--root",
            str(ROOT),
            "--report",
            str(report_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    loaded = load_lf022_foundation_configs(
        paths=RepoPaths(root=ROOT),
        variants_path=VARIANTS_CONFIG,
        judges_path=JUDGES_CONFIG,
    )
    assert payload["variants_config_sha256"] == loaded.variants.config_hash
    assert payload["judges_config_sha256"] == loaded.judges.config_hash
    assert payload["proposer_prompt_sha256"] == loaded.proposer_prompt_sha256
    assert payload["judge_prompt_sha256"] == loaded.judge_prompt_sha256
    assert payload != stale_payload


def test_cli_replays_hash_bound_proposer_response(tmp_path: Path) -> None:
    request_path, raw_root = _persist_replay(
        tmp_path,
        output=_proposer_output(),
        prompt_hash=hash_file(PROPOSER_PROMPT),
    )
    report_path = tmp_path / "replay-report.json"
    result = CliRunner().invoke(
        app,
        [
            "validate-lf022",
            "--root",
            str(ROOT),
            "--replay-kind",
            "proposer",
            "--request",
            str(request_path),
            "--raw-response-root",
            str(raw_root),
            "--report",
            str(report_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "replay=proposer" in result.output
    assert "parsed_items=1" in result.output
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["replay"]["replay_kind"] == "proposer"


def test_runner_requires_complete_replay_tuple(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be supplied together"):
        run_lf022_validation(
            paths=RepoPaths(root=ROOT),
            variants_config_path=VARIANTS_CONFIG,
            judges_config_path=JUDGES_CONFIG,
            report_path=tmp_path / "report.json",
            replay_kind="proposer",
        )
