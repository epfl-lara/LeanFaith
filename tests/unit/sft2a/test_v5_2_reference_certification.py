from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.mechanisms import planning_signature_from_goal_v1, signature_shape
from leanfaith.sft2a.models import SFT2AV52Config
from leanfaith.sft2a.parallel_rehearsal import (
    AtomicProviderBudget,
    ParallelRehearsalError,
    ParallelRootJournal,
    deterministic_parallel_compaction,
)
from leanfaith.sft2a.reference_certification import prepare_reference_pool
from leanfaith.sft2a.reference_certifier import (
    _compile_context,
    constant_lookup_command,
    term_elaboration_command,
)

_CONFIG = Path("configs/sft2a/closure_aware_v5_2.yaml")
_RECOVERY_CONFIG = Path("configs/sft2a/closure_aware_v5_2_recovery_v2.yaml")


def _loaded() -> LoadedSFT2AConfig:
    loaded = load_sft2a_config(_CONFIG)
    assert isinstance(loaded.config, SFT2AV52Config)
    return loaded


def _context(loaded: LoadedSFT2AConfig) -> CompileContext:
    source = loaded.config.root.compile_context
    return CompileContext(
        project_id=source.project_id,
        project_revision=source.project_revision,
        lean_version=source.lean_version,
        import_header=source.import_header,
        command_preamble=source.command_preamble,
        namespace_context=source.namespace_context,
        open_context=source.open_context,
        scoped_context=source.scoped_context,
        options=source.options,
    )


def test_v5_2_pool_is_exact_zero_call_and_contains_positive_canary() -> None:
    loaded = _loaded()
    config = cast(SFT2AV52Config, loaded.config)
    manifest = prepare_reference_pool(loaded)
    output = Path(config.staging_root) / config.reference_certification.output_subdir
    initial = [
        json.loads(line) for line in (output / "initial_pool.jsonl").read_text().splitlines()
    ]
    extension = [
        json.loads(line) for line in (output / "extension_pool.jsonl").read_text().splitlines()
    ]

    assert manifest["initial_source_counts"] == {
        "mathlib": 126,
        "physlib": 75,
        "cslib": 51,
        "compiler_data": 48,
    }
    assert manifest["extension_source_counts"] == manifest["initial_source_counts"]
    assert len(initial) == len(extension) == 300
    assert len({row["root_id"] for row in [*initial, *extension]}) == 600
    assert manifest["lean_requests_executed"] == 0
    assert manifest["provider_calls_executed"] == 0
    canary = [row for row in initial if row["declaration_name"] == "Cslib.LTS.mem_saturate_image_τ"]
    assert len(canary) == 1
    assert "Label" not in canary[0]["reference_signature"].split("HasTau", maxsplit=1)[0]
    assert hash_file(output / "pool.jsonl") == manifest["pool_sha256"]


def test_library_command_uses_actual_theorem_constant_type_without_term_round_trip() -> None:
    loaded = _loaded()
    command = constant_lookup_command(
        context=_context(loaded),
        declaration_name="Cslib.LTS.mem_saturate_image_τ",
        endpoint_id="test:constant",
        render_scope_id="test:scope",
    )

    assert "(← getEnv).find? lookup.toName" in command
    assert "some (.thmInfo info)" in command
    assert (
        'emitClosedProp\n          endpointId renderScopeId "loaded_constant_type" info.type'
        in command
    )
    assert "Term.elabTerm" not in command
    assert "non_theorem_constant" in command
    assert "Cslib.LTS.mem_saturate_image_τ" in command


def test_compiler_data_route_elaborates_one_proof_free_term() -> None:
    loaded = _loaded()
    context = _context(loaded)
    command = term_elaboration_command(
        context=context,
        signature="∀ (n : Nat), n = n",
        endpoint_id="test:term",
        render_scope_id="test:scope",
    )
    assert command.count("Term.elabTerm") == 1
    assert command.count("LeanFaith.GoalV1.emitClosedProp") == 1
    certifier_definition = command.split(
        "namespace LeanFaith.SFT2A.ReferenceCertification", maxsplit=1
    )[1]
    assert ":= by" not in certifier_definition
    assert "sorry" not in certifier_definition
    assert "axiom" not in certifier_definition


def test_certified_goal_shape_counts_true_implicit_binders() -> None:
    planning = planning_signature_from_goal_v1(
        "Label : Type u_0\nState : Type u_1\ns : State\ninst : HasTau Label\n"
        "lts : LTS State Label\n⊢ s ∈ lts.saturate.image s HasTau.τ"
    )
    shape = signature_shape(planning)
    assert shape.binder_count >= 5
    assert "(Label : Type u_0)" in planning
    assert "(State : Type u_1)" in planning


def test_recovery_uses_one_canonical_lookup_context_per_project() -> None:
    loaded = load_sft2a_config(_RECOVERY_CONFIG)
    assert isinstance(loaded.config, SFT2AV52Config)
    source_context = loaded.config.root.compile_context.model_copy(
        update={
            "namespace_context": ("Cslib", "LTS"),
            "open_context": ("Classical",),
            "scoped_context": ("BigOperators",),
        }
    )
    root = loaded.config.root.model_copy(
        update={"source": "cslib", "compile_context": source_context}
    )
    canonical = _compile_context(root)
    assert canonical.namespace_context == ()
    assert canonical.open_context == ()
    assert canonical.scoped_context == ()


def test_atomic_provider_budget_survives_restart_and_requires_opus_cost(tmp_path: Path) -> None:
    loaded = _loaded()
    config = cast(SFT2AV52Config, loaded.config)
    ceilings = config.rehearsal.ceilings.model_copy(
        update={
            "maximum_provider_calls": 2,
            "maximum_opus_calls": 2,
            "maximum_reported_opus_spend_usd": 1.0,
        }
    )
    path = tmp_path / "budget.jsonl"
    budget = AtomicProviderBudget(path, ceilings)
    reservation = budget.reserve(
        call_key="opus:one", kind="opus", worker_id="worker-0", maximum_charge_usd=0.6
    )
    with pytest.raises(ParallelRehearsalError, match="lacks reported cost"):
        budget.finalize(
            call_key="opus:one",
            reservation_id=reservation,
            response_sha256="a" * 64,
            reported_cost_usd=None,
        )
    budget.finalize(
        call_key="opus:one",
        reservation_id=reservation,
        response_sha256="a" * 64,
        reported_cost_usd=0.6,
    )

    restarted = AtomicProviderBudget(path, ceilings)
    with pytest.raises(ParallelRehearsalError, match="spend ceiling"):
        restarted.reserve(
            call_key="opus:two",
            kind="opus",
            worker_id="worker-1",
            maximum_charge_usd=0.5,
        )
    assert restarted.snapshot()["reported_opus_spend_usd"] == pytest.approx(0.6)


def test_parallel_root_journal_mid_and_between_root_resume(tmp_path: Path) -> None:
    journal = ParallelRootJournal(tmp_path / "roots.jsonl")
    assert journal.claim(root_id="root-a", worker_id="worker-0") == "claimed"
    journal.checkpoint(
        root_id="root-a", worker_id="worker-0", slot_id="preserve_0", artifact_hash="a" * 64
    )
    assert journal.claim(root_id="root-a", worker_id="worker-0") == "claimed"
    with pytest.raises(ParallelRehearsalError, match="another worker"):
        journal.claim(root_id="root-a", worker_id="worker-1")
    journal.complete(root_id="root-a", worker_id="worker-0", manifest_hash="b" * 64)
    assert journal.claim(root_id="root-a", worker_id="worker-1") == "replay_complete"


def test_simultaneous_duplicate_root_claim_is_refused(tmp_path: Path) -> None:
    journal = ParallelRootJournal(tmp_path / "roots.jsonl")
    outcomes: list[str] = []

    def claim(worker: str) -> None:
        try:
            outcomes.append(journal.claim(root_id="same-root", worker_id=worker))
        except ParallelRehearsalError:
            outcomes.append("refused")

    threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["claimed", "refused"]


def test_parallel_compaction_separates_planned_and_accepted_histograms(tmp_path: Path) -> None:
    rows = [
        {
            "row_id": "row-b",
            "candidate_rendered_goal_hash": "b" * 64,
            "candidate_closed_expr_hash": "c" * 64,
            "planned_mechanism": {"polarity": "preserving", "family": "binder_permutation"},
            "accepted_mechanism": {"polarity": "preserving", "family": "equation_orientation"},
        },
        {
            "row_id": "row-a",
            "candidate_rendered_goal_hash": "d" * 64,
            "candidate_closed_expr_hash": "e" * 64,
            "planned_mechanism": {"polarity": "breaking", "family": "premise_removal"},
            "accepted_mechanism": {"polarity": "breaking", "family": "boundary_shift"},
        },
    ]
    manifest = deterministic_parallel_compaction(rows, output=tmp_path / "compacted")
    compacted = (tmp_path / "compacted/rows.jsonl").read_text()
    assert compacted.index("row-a") < compacted.index("row-b")
    assert (
        manifest["planned_mechanism_histogram"] != manifest["accepted_mechanism_evidence_histogram"]
    )
    with pytest.raises(ParallelRehearsalError, match="cross-worker duplicate"):
        deterministic_parallel_compaction([rows[0], rows[0]], output=tmp_path / "duplicate")
