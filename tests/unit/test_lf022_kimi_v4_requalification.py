"""Fail-closed tests for the gated Kimi-v4 live requalification executor."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.generation.lf022_execution import LF022GOpenExecutionTask
from leanfaith.generation.lf022_kimi_v4_requalification import (
    KimiV4RuntimeCredentials,
    LF022KimiV4RequalificationError,
    run_verified_kimi_v4_requalification,
)
from leanfaith.generation.lf022_kimi_v4_selection import (
    LF022KimiV4ChallengeContract,
    LF022KimiV4ChallengeSelection,
    LF022KimiV4SelectedChallengeItem,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding, LF022ProductionTask
from leanfaith.generation.llm_variants import PublicLeanVariantSource
from leanfaith.generation.rcp_provider import RCPTransportUnknownError, RCPWireResponse
from leanfaith.schemas.enums import IntendedRelation
from leanfaith.schemas.ids import make_id

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)


class FakeTransport:
    def __init__(self, responses: list[RCPWireResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def post_json(self, **_: object) -> RCPWireResponse:
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _write(root: Path, relative: str, payload: bytes) -> LF022ArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return LF022ArtifactBinding(path=relative, sha256=hash_file(path))


def _production_task(index: int, source: PublicLeanVariantSource) -> LF022ProductionTask:
    payload: dict[str, object] = {
        "schema_version": 2,
        "task_kind": "non_executable_allocation",
        "admission_record_id": f"lf022_source_admission:{index + 1:064x}",
        "source_locator_id": f"{index + 1:064x}",
        "theorem_id": source.source_theorem_id,
        "representation_id": source.source_representation_id,
        "context_id": source.context_id,
        "distribution": "G_open",
        "proposer_family_id": "moonshot_kimi_k2",
        "judge_family_ids": ["qwen3", "glm5"],
        "sci_validator_family_id": None,
        "heldout_eval_family_id": "heldout_family",
        "heldout_eval_supervision_excluded": True,
        "execution_binding_id": None,
        "executable": False,
        "network_execution_authorized": False,
        "semantic_label_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
    }
    return LF022ProductionTask.model_validate(
        {**payload, "task_id": make_id("lf022_production_task", payload)}
    )


def _execution_task(index: int) -> LF022GOpenExecutionTask:
    source = PublicLeanVariantSource(
        source_theorem_id=f"thm:{index + 1:064x}",
        source_representation_id=f"repr:{index + 1:064x}",
        context_id=f"ctx:{index + 1:064x}",
        imports=("Mathlib",),
        source_statement=f"theorem source_{index} (n : Nat) : n = n",
        optional_natural_language=None,
        source_id="mathlib",
        source_revision="fixture-revision",
        source_license="Apache-2.0",
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )
    allocation = _production_task(index, source)
    payload: dict[str, object] = {
        "schema_version": 2,
        "execution_admission_id": f"lf022_execution_admission:{'a' * 64}",
        "allocation_plan_id": f"lf022_production_plan:{'b' * 64}",
        "allocation_task": allocation.model_dump(mode="json"),
        "normalization_version": "repr_v3",
        "source": source.model_dump(mode="json"),
        "proposal_count": 1,
        "requested_relations": [IntendedRelation.NEAR_MISS.value],
        "distribution": "G_open",
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "source_statement_version": "named_signature_v2",
    }
    return LF022GOpenExecutionTask.model_validate(
        {**payload, "execution_task_id": make_id("lf022_execution_task", payload)}
    )


def _fixture(root: Path) -> LF022KimiV4ChallengeSelection:
    prompt_binding = _write(
        root,
        "prompts/proposers/lean_variant_v2.txt",
        (ROOT / "prompts/proposers/lean_variant_v2.txt").read_bytes(),
    )
    config_binding = _write(
        root,
        "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml",
        (ROOT / "configs/generation/lf022_kimi_k2_7_proposer_v4.yaml").read_bytes(),
    )
    config = dict(load_yaml_mapping(ROOT / config_binding.path))
    decoding = dict(config["decoding"])
    decoding.update(schema_version=1, contract_id=config["contract_id"])
    config["decoding"] = decoding
    contract = LF022KimiV4ChallengeContract.model_validate(config)
    admission_binding = _write(
        root,
        "data/historical/admission.json",
        canonical_json_bytes(
            {
                "admission_id": f"lf022_execution_admission:{'a' * 64}",
                "route": {
                    "model_id": "moonshotai/Kimi-K2.7-Code",
                    "route_snapshot_revision": "rcp-catalog-sha256:" + "c" * 64,
                },
            }
        ),
    )
    selected: list[LF022KimiV4SelectedChallengeItem] = []
    for index in range(16):
        task = _execution_task(index)
        task_binding = _write(
            root,
            f"data/historical/tasks/{index:02d}.json",
            canonical_json_bytes(task.model_dump(mode="json")) + b"\n",
        )
        role = (
            "budget_exhausted" if index < 6 else "proof_bearing" if index < 8 else "prior_success"
        )
        selected.append(
            LF022KimiV4SelectedChallengeItem.model_construct(
                selection_rank=index,
                role=role,
                role_rank=index if index < 6 else index - 6 if index < 8 else index - 8,
                allocation_task_id=task.allocation_task.task_id,
                source_admission_record_id=task.allocation_task.admission_record_id,
                source_theorem_id=task.source.source_theorem_id,
                execution_task_id=task.execution_task_id,
                task=task_binding,
                terminal_id=f"lf022_execution_terminal:{index + 1:064x}",
                terminal=task_binding,
                final_wire_response_body=None,
                current_parser_outcome="prior_success" if False else "strict_variant_success",
            )
        )
    return LF022KimiV4ChallengeSelection.model_construct(
        selection_id=f"lf022_kimi_v4_selection:{'d' * 64}",
        v3_admission_id=f"lf022_execution_admission:{'a' * 64}",
        v3_admission=admission_binding,
        v4_contract=config_binding,
        v4_contract_hash=hash_canonical(contract.model_dump(mode="json")),
        v4_prompt=prompt_binding,
        selected=tuple(selected),
    )


def _success(index: int) -> RCPWireResponse:
    content = json.dumps(
        {
            "variants": [
                {
                    "candidate_lean": f"theorem candidate_{index} (n : Nat) : n = n + 0",
                    "intended_relation": "near_miss",
                    "intended_error_types": [],
                    "edit_summary": "fixture",
                    "confidence": 0.5,
                    "assumptions": [],
                    "potential_ambiguity": None,
                }
            ]
        }
    )
    return RCPWireResponse(
        status_code=200,
        headers={},
        body=canonical_json_bytes(
            {
                "id": f"chatcmpl-{index}",
                "model": "moonshotai/Kimi-K2.7-Code",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }
        ),
    )


def _length_response() -> RCPWireResponse:
    return RCPWireResponse(
        status_code=200,
        headers={},
        body=canonical_json_bytes(
            {
                "id": "chatcmpl-length",
                "model": "moonshotai/Kimi-K2.7-Code",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
            }
        ),
    )


def _credentials() -> KimiV4RuntimeCredentials:
    return KimiV4RuntimeCredentials(
        base_url="https://inference.rcp.epfl.ch/v1",
        api_key="super-secret-fixture-key",
    )


def test_capability_success_gates_remaining_and_creates_nonpromoting_qualification(
    tmp_path: Path,
) -> None:
    selection = _fixture(tmp_path)
    capability_transport = FakeTransport([_success(0)])
    capability = run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="capability",
        execute_public_requalification=True,
        credentials=_credentials(),
        transport=capability_transport,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    )
    assert capability_transport.calls == 1
    assert capability.terminals[0].status == "strict_variant_success"
    assert capability.qualification is None

    remaining_transport = FakeTransport([_success(index) for index in range(1, 16)])
    remaining = run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="remaining",
        execute_public_requalification=True,
        credentials=_credentials(),
        transport=remaining_transport,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    )
    assert remaining_transport.calls == 15
    assert remaining.qualification is not None
    assert remaining.qualification.status == "passed"
    assert remaining.qualification.production_admission_created is False
    assert remaining.qualification.training_eligible is False
    assert b"super-secret-fixture-key" not in b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )

    replay_transport = FakeTransport([])
    replay = run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="replay",
        transport=replay_transport,
        clock=lambda: NOW,
    )
    assert replay.network_calls_this_run == 0
    assert len(replay.terminals) == 16
    assert replay_transport.calls == 0


def test_failed_capability_forbids_remaining_challenge(tmp_path: Path) -> None:
    selection = _fixture(tmp_path)
    transport = FakeTransport([_length_response()])
    capability = run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="capability",
        execute_public_requalification=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    )
    assert capability.terminals[0].status == "provider_exhausted"
    assert capability.terminals[0].error_code == "output_budget_exhausted"
    assert transport.calls == 1
    with pytest.raises(LF022KimiV4RequalificationError, match="did not strictly pass"):
        run_verified_kimi_v4_requalification(
            repo_root=tmp_path,
            selection=selection,
            stage="remaining",
            execute_public_requalification=True,
            credentials=_credentials(),
            transport=FakeTransport([_success(1)]),
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    "response,error_code,status",
    [
        (
            RCPWireResponse(status_code=403, headers={}, body=b"forbidden"),
            "http_403",
            "provider_exhausted",
        ),
        (RCPTransportUnknownError("unknown"), "transport_unknown", "transport_unknown"),
    ],
)
def test_nonretryable_and_ambiguous_failures_make_exactly_one_call(
    tmp_path: Path,
    response: RCPWireResponse | Exception,
    error_code: str,
    status: str,
) -> None:
    selection = _fixture(tmp_path)
    transport = FakeTransport([response])
    result = run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="capability",
        execute_public_requalification=True,
        credentials=_credentials(),
        transport=transport,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    )
    assert transport.calls == 1
    assert result.terminals[0].error_code == error_code
    assert result.terminals[0].status == status


def test_live_flag_is_required_and_offline_preflight_makes_no_call(tmp_path: Path) -> None:
    selection = _fixture(tmp_path)
    transport = FakeTransport([_success(0)])
    result = run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="capability",
        transport=transport,
        clock=lambda: NOW,
    )
    assert not result.terminals
    assert result.network_calls_this_run == 0
    assert transport.calls == 0


def test_replay_rejects_tampered_wire_response(tmp_path: Path) -> None:
    selection = _fixture(tmp_path)
    run_verified_kimi_v4_requalification(
        repo_root=tmp_path,
        selection=selection,
        stage="capability",
        execute_public_requalification=True,
        credentials=_credentials(),
        transport=FakeTransport([_success(0)]),
        clock=lambda: NOW,
        sleeper=lambda _: None,
    )
    body = next(
        tmp_path.glob(
            "data/lf022_kimi_v4_requalification/v1/*/tasks/00/attempts/0000/wire/response.body"
        )
    )
    body.write_bytes(b"tampered")
    with pytest.raises(LF022KimiV4RequalificationError, match="differs from its binding"):
        run_verified_kimi_v4_requalification(
            repo_root=tmp_path,
            selection=selection,
            stage="replay",
            clock=lambda: NOW,
        )
