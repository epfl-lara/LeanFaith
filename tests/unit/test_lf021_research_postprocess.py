from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.generation.research_collection import ResearchTerminalStatus
from leanfaith.generation.research_postprocess import (
    PostprocessArtifactBinding,
    ResearchPostprocessError,
    ResearchPostprocessInputBinding,
    ResearchPostprocessStatus,
    ResearchPostprocessTerminal,
    _canonical_candidate_keys_by_alpha,
    _prepare_candidates,
    _require_exact_raw_collection_artifacts,
    _resolve_bound_artifact,
    _write_terminals_and_reports,
    verify_research_postprocess,
)

UTC = datetime.datetime(2026, 7, 23, 22, 0, tzinfo=datetime.UTC)
ZERO = "0" * 64


@dataclass
class _FakeInvocation:
    invocation_id: str
    family_id: str
    problem_record_id: str
    seed: int
    parser_id: str = "parser_v1"
    parser_source_sha256: str = "5" * 64

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "invocation_id": self.invocation_id,
            "family_id": self.family_id,
            "problem_record_id": self.problem_record_id,
            "seed": self.seed,
            "parser_id": self.parser_id,
            "parser_source_sha256": self.parser_source_sha256,
        }


def _binding(name: str) -> PostprocessArtifactBinding:
    return PostprocessArtifactBinding(
        artifact=f"artifacts/{name}.json",
        sha256=sha256_hex(name.encode()),
    )


def _input_binding() -> ResearchPostprocessInputBinding:
    invocation_ids = tuple(
        sorted(
            f"research_collection_invocation:{sha256_hex(f'invocation-{index}'.encode())}"
            for index in range(9)
        )
    )
    registry = {
        key: _binding(f"registry-{key}")
        for key in (
            "active_registry",
            "base_registry",
            "code_bundle",
            "detailed_index",
            "input_manifest",
            "pointer_manifest",
        )
    }
    return ResearchPostprocessInputBinding(
        collection_plan=_binding("plan"),
        collection_manifest=_binding("collection-manifest"),
        collection_plan_id=f"research_collection_plan:{'1' * 64}",
        collection_plan_hash="2" * 64,
        collection_manifest_id=f"research_collection_manifest:{'3' * 64}",
        collection_terminal_artifacts={
            f"runs/terminal-{index}.json": sha256_hex(f"terminal-{index}".encode())
            for index in range(9)
        },
        collection_family_session_artifacts={
            "runs/family-session-start.json": sha256_hex(b"family-session-start")
        },
        problem_pool_records=_binding("problems"),
        context=_binding("context"),
        import_header=_binding("header"),
        reference_theorems=_binding("references"),
        reference_representations=_binding("reference-representations"),
        active_registry_artifacts=registry,
        active_registry_content_hash="4" * 64,
        implementation=_binding("implementation"),
        invocation_ids=invocation_ids,
        family_ids=("family_a", "family_b", "family_c"),
    )


def _write_bound_inputs(root: Path, binding: ResearchPostprocessInputBinding) -> None:
    named = {
        "plan": binding.collection_plan,
        "collection-manifest": binding.collection_manifest,
        "problems": binding.problem_pool_records,
        "context": binding.context,
        "header": binding.import_header,
        "references": binding.reference_theorems,
        "reference-representations": binding.reference_representations,
        "implementation": binding.implementation,
        **{f"registry-{key}": value for key, value in binding.active_registry_artifacts.items()},
    }
    for content, binding_artifact in named.items():
        path = root / binding_artifact.artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    for index, (terminal_artifact, _) in enumerate(binding.collection_terminal_artifacts.items()):
        path = root / terminal_artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"terminal-{index}".encode())
    for session_artifact in binding.collection_family_session_artifacts:
        path = root / session_artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"family-session-start")


def _terminal(
    *,
    binding: ResearchPostprocessInputBinding,
    invocation_id: str,
    family_id: str,
    index: int,
    status: ResearchPostprocessStatus,
) -> ResearchPostprocessTerminal:
    admitted = status is ResearchPostprocessStatus.ADMITTED_UNRESOLVED
    payload: dict[str, object] = {
        "schema_version": 1,
        "record_kind": "lf021_research_postprocess_terminal",
        "artifact_class": "research",
        "input_binding_hash": binding.binding_hash,
        "invocation_id": invocation_id,
        "invocation_payload_hash": hash_canonical(
            _FakeInvocation(
                invocation_id=invocation_id,
                family_id=family_id,
                problem_record_id=f"problem:{sha256_hex(f'problem-{index}'.encode())}",
                seed=index,
            ).model_dump(mode="json")
        ),
        "collection_terminal_id": (
            f"research_collection_terminal:{sha256_hex(f'raw-{index}'.encode())}"
        ),
        "collection_terminal_sha256": sha256_hex(f"terminal-{index}".encode()),
        "family_id": family_id,
        "problem_record_id": f"problem:{sha256_hex(f'problem-{index}'.encode())}",
        "seed": index,
        "status": status.value,
        "terminal_stage": "complete" if admitted else "collection",
        "record_time_basis": UTC.isoformat().replace("+00:00", "Z"),
        "parser_id": "parser_v1",
        "parser_source_sha256": "5" * 64,
        "parser_executed": admitted,
        "lean_validation_executed": admitted,
        "screening_executed": admitted,
        "semantic_pool_admitted": admitted,
        "raw_lineage_hashes": {},
        "output_artifact_hashes": {},
        "materialization_outcome": "materialized" if admitted else None,
        "screening_status": "clean" if admitted else None,
        "variant_id": f"variant:{'6' * 64}" if admitted else None,
        "candidate_theorem_id": f"thm:{'7' * 64}" if admitted else None,
        "representation_id": f"repr:{'8' * 64}" if admitted else None,
        "screening_id": f"candidate_screen:{'9' * 64}" if admitted else None,
        "pair_ids": (f"pair:{sha256_hex(f'pair-{index}'.encode())}",) if admitted else (),
        "nl_lean_id": f"nl-lean:{'a' * 64}" if admitted else None,
        "same_claim": None,
        "relation": None,
        "resolution_outcome": "unresolved" if admitted else None,
        "quality_tier": "unknown" if admitted else None,
        "requires_adjudication": admitted,
        "decision": "REVIEW" if admitted else None,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "failure_code": None if admitted else f"operational_failure_{index}",
        "failure_detail": None if admitted else "not a semantic negative",
    }
    terminal_id = "research_postprocess_terminal:" + hash_canonical(
        {"schema": "lf021_research_postprocess_terminal_v1", **payload}
    )
    return ResearchPostprocessTerminal.model_validate({"terminal_id": terminal_id, **payload})


def test_exact_nine_invocation_input_binding_is_enforced() -> None:
    binding = _input_binding()
    assert len(binding.invocation_ids) == 9
    with pytest.raises(ValueError, match="exactly nine"):
        ResearchPostprocessInputBinding.model_validate(
            {
                **binding.model_dump(mode="json"),
                "invocation_ids": binding.invocation_ids[:-1],
            }
        )


def test_absolute_content_addressed_gate_artifact_is_hash_bound(
    tmp_path: Path,
) -> None:
    external = tmp_path / "bulk" / "code_bundle.tar.gz"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"immutable gate code bundle")
    binding = PostprocessArtifactBinding(
        artifact=str(external),
        sha256=sha256_hex(external.read_bytes()),
        location_kind="absolute_content_addressed",
    )

    assert _resolve_bound_artifact(tmp_path / "repo", binding) == external.resolve()
    with pytest.raises(ValueError, match="absolute path"):
        PostprocessArtifactBinding(
            artifact="relative/code_bundle.tar.gz",
            sha256=binding.sha256,
            location_kind="absolute_content_addressed",
        )


def test_admitted_terminal_is_unresolved_review_only() -> None:
    binding = _input_binding()
    terminal = _terminal(
        binding=binding,
        invocation_id=binding.invocation_ids[0],
        family_id="family_a",
        index=0,
        status=ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
    )
    assert terminal.same_claim is None
    assert terminal.relation is None
    assert terminal.resolution_outcome == "unresolved"
    assert terminal.requires_adjudication is True
    assert terminal.decision == "REVIEW"
    assert terminal.semantic_labels_created is False
    assert terminal.gate_5g_credit_claimed is False

    with pytest.raises(ValueError, match="remain unresolved"):
        ResearchPostprocessTerminal.model_validate(
            {
                **terminal.model_dump(mode="json"),
                "decision": None,
            }
        )


def test_canonical_dedup_choice_is_permutation_invariant() -> None:
    identities = (
        ("a" * 64, "thm:" + "2" * 64, "invocation:z"),
        ("a" * 64, "thm:" + "1" * 64, "invocation:y"),
        ("a" * 64, "thm:" + "1" * 64, "invocation:x"),
        ("b" * 64, "thm:" + "3" * 64, "invocation:x"),
    )
    expected = {
        "a" * 64: ("thm:" + "1" * 64, "invocation:x"),
        "b" * 64: ("thm:" + "3" * 64, "invocation:x"),
    }
    assert _canonical_candidate_keys_by_alpha(identities) == expected
    assert _canonical_candidate_keys_by_alpha(tuple(reversed(identities))) == expected


def test_raw_collection_requires_no_retry_boundaries() -> None:
    exact = {
        "family_session_start",
        "llm_attempt",
        "llm_call",
        "local_generation_result",
        "model_attempt_boundary",
        "provider_boundary",
        "provider_raw_response",
        "provider_request",
    }
    _require_exact_raw_collection_artifacts(
        cast(Any, SimpleNamespace(artifact_hashes=dict.fromkeys(exact, ZERO)))
    )
    with pytest.raises(
        ResearchPostprocessError,
        match="raw collection terminal artifact denominator differs",
    ):
        _require_exact_raw_collection_artifacts(
            cast(
                Any,
                SimpleNamespace(
                    artifact_hashes={key: ZERO for key in exact if key != "model_attempt_boundary"}
                ),
            )
        )


def test_runtime_failed_collection_rows_receive_all_nine_processing_terminals(
    tmp_path: Path,
) -> None:
    binding = _input_binding()
    collection_paths: dict[str, Path] = {}
    invocations: list[Any] = []
    collection_terminals: dict[str, Any] = {}
    for index, invocation_id in enumerate(binding.invocation_ids):
        family = binding.family_ids[index // 3]
        path = tmp_path / "collection" / f"{index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes({"index": index}) + b"\n")
        collection_paths[invocation_id] = path

        invocations.append(
            _FakeInvocation(
                invocation_id=invocation_id,
                family_id=family,
                problem_record_id=f"problem:{sha256_hex(f'problem-{index}'.encode())}",
                seed=index,
            )
        )
        collection_terminals[invocation_id] = SimpleNamespace(
            status=ResearchTerminalStatus.RUNTIME_FAILED,
            error_detail="model runtime failed",
            completed_at=UTC,
            terminal_id=(f"research_collection_terminal:{sha256_hex(f'raw-{index}'.encode())}"),
        )

    loaded = SimpleNamespace(
        plan=SimpleNamespace(family_bindings=()),
        invocations=tuple(invocations),
        collection_terminals=collection_terminals,
        collection_terminal_paths=collection_paths,
        input_binding=binding,
    )
    prepared, terminals = _prepare_candidates(
        cast(Any, loaded),
        backend=cast(Any, object()),
    )
    assert prepared == []
    assert len(terminals) == 9
    assert {terminal.status for terminal in terminals.values()} == {
        ResearchPostprocessStatus.COLLECTION_NOT_RAW
    }
    assert all(terminal.parser_executed is False for terminal in terminals.values())
    assert all(terminal.semantic_labels_created is False for terminal in terminals.values())


def test_family_and_global_accounting_manifest_replays(tmp_path: Path) -> None:
    binding = _input_binding()
    _write_bound_inputs(tmp_path, binding)
    statuses = (
        ResearchPostprocessStatus.COLLECTION_NOT_RAW,
        ResearchPostprocessStatus.PARSE_FAILED,
        ResearchPostprocessStatus.ADMITTED_UNRESOLVED,
    )
    terminals: dict[str, ResearchPostprocessTerminal] = {}
    collection_terminals: dict[str, Any] = {}
    collection_terminal_paths: dict[str, Path] = {}
    invocations: list[_FakeInvocation] = []
    for index, invocation_id in enumerate(binding.invocation_ids):
        family = binding.family_ids[index // 3]
        problem_record_id = f"problem:{sha256_hex(f'problem-{index}'.encode())}"
        invocations.append(
            _FakeInvocation(
                invocation_id=invocation_id,
                family_id=family,
                problem_record_id=problem_record_id,
                seed=index,
            )
        )
        terminal = _terminal(
            binding=binding,
            invocation_id=invocation_id,
            family_id=family,
            index=index,
            status=statuses[index % len(statuses)],
        )
        terminals[invocation_id] = terminal
        collection_terminals[invocation_id] = SimpleNamespace(
            status=(
                ResearchTerminalStatus.RUNTIME_FAILED
                if terminal.status is ResearchPostprocessStatus.COLLECTION_NOT_RAW
                else ResearchTerminalStatus.RAW_COLLECTED
            ),
            terminal_id=(f"research_collection_terminal:{sha256_hex(f'raw-{index}'.encode())}"),
        )
        collection_terminal_paths[invocation_id] = tmp_path / f"runs/terminal-{index}.json"

    loaded = SimpleNamespace(
        repo_root=tmp_path,
        output_root=tmp_path / "postprocess",
        input_binding=binding,
        invocations=tuple(invocations),
        collection_terminals=collection_terminals,
        collection_terminal_paths=collection_terminal_paths,
    )
    run = _write_terminals_and_reports(cast(Any, loaded), terminals)
    assert run.manifest.expected_invocations == 9
    assert run.manifest.terminal_invocations == 9
    assert run.manifest.admitted_nl_lean_count == 3
    assert len(run.family_reports) == 3
    assert all(report.terminal_invocations == 3 for report in run.family_reports)

    replay = verify_research_postprocess(cast(Any, loaded))
    assert replay == run.manifest
