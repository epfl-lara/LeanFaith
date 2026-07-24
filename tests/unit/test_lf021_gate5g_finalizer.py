"""Fail-closed LF-021 Gate-5G finalizer over synthetic immutable fixtures."""

from __future__ import annotations

import datetime
import hmac
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.paths import RepoPaths
from leanfaith.generation import gate5g as gate5g_module
from leanfaith.generation import tranche_expansion as v1
from leanfaith.generation.frame_freeze_v3 import (
    EligiblePopulationItemV3,
    EligiblePopulationManifestV3,
    FrameBindingV3,
    FrameFreezeDecisionV3,
    FrameFreezeV3Error,
    FrameItemV3,
    PopulationMemberV3,
    SamplingSeedProvenanceV3,
    SeedLockV3,
    load_frame_items_v3,
)
from leanfaith.generation.gate5g import (
    Gate5GFinalizationError,
    validate_or_finalize_gate5g,
)
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GFamilyRevisionBinding,
    Gate5GLineageManifestV1,
    Gate5GReFormApplicability,
    Gate5GScopeLimitations,
    Gate5GStratumAccounting,
    Gate5GTrancheBindingV1,
    ValidatedRealOutputsManifestV1,
)

_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
_SAMPLING = "problem_aware_stratified_csprng_srs_without_replacement_v2"
_RANK_ALGORITHM = "hmac_sha256_keyed_rank_v1"
_DOMAIN = "leanfaith.lf021.prevalence.frame.v3"


@dataclass(frozen=True, slots=True)
class _Fixture:
    paths: RepoPaths
    decision: Path
    lineage: Path
    validated: Path
    coverage: Path
    phase: Path
    prevalence_design_policy: Path
    policy: Path


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    return hash_file(path)


def _binding(root: Path, path: Path) -> dict[str, str]:
    return {
        "artifact": str(path.relative_to(root)),
        "sha256": hash_file(path),
    }


def _artifact(root: Path, path: Path) -> v1.ArtifactBinding:
    return v1.ArtifactBinding.model_validate(_binding(root, path))


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


def _rank(*, seed: bytes, stratum: str, cluster_id: str) -> str:
    message = _DOMAIN.encode() + b"\x00" + stratum.encode() + b"\x00" + cluster_id.encode()
    return hmac.new(seed, message, sha256).hexdigest()


@pytest.fixture(autouse=True)
def _strict_v3_verifier_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Gate-5G tests isolated from the v3 module's own replay tests."""

    def fake_verify(
        *,
        repo_root: Path,
        policy_path: Path,
        decision_path: Path,
    ) -> SimpleNamespace:
        assert policy_path.is_file()
        decision = FrameFreezeDecisionV3.model_validate_json(decision_path.read_bytes())
        frame_path = repo_root / decision.frame.artifact
        frame_items = load_frame_items_v3(frame_path)
        provenance_path = repo_root / decision.sampling_seed_provenance.artifact
        provenance = SamplingSeedProvenanceV3.model_validate_json(provenance_path.read_bytes())
        seed_path = repo_root / provenance.sampling_seed.artifact
        population_manifest_path = repo_root / decision.population_manifest.artifact
        population_manifest = EligiblePopulationManifestV3.model_validate_json(
            population_manifest_path.read_bytes()
        )
        population_path = repo_root / decision.population_artifact.artifact
        population_items = tuple(
            EligiblePopulationItemV3.model_validate_json(line)
            for line in population_path.read_bytes().splitlines()
            if line.strip()
        )
        lock_path = (
            provenance_path.parent
            / "by_population"
            / f"{decision.population_id.rsplit(':', 1)[-1]}.json"
        )
        lock = SeedLockV3.model_validate_json(lock_path.read_bytes())
        return SimpleNamespace(
            decision=decision,
            decision_path=decision_path,
            decision_binding=_artifact(repo_root, decision_path),
            verified_v2_stop=SimpleNamespace(),
            population=SimpleNamespace(
                manifest=population_manifest,
                manifest_path=population_manifest_path,
                population_path=population_path,
                items=population_items,
            ),
            seed_provenance=provenance,
            seed_provenance_path=provenance_path,
            seed_bytes=seed_path.read_bytes(),
            seed_lock=lock,
            seed_lock_path=lock_path,
            frame_path=frame_path,
            frame_items=frame_items,
        )

    monkeypatch.setattr(gate5g_module, "verify_frame_freeze_v3", fake_verify)


def _fixture(
    tmp_path: Path,
    *,
    item_count: int = 200,
    semantic_terminal_label: bool = False,
    sampling_method: str = _SAMPLING,
    replay_byte_identical: bool = True,
    test_replay_seed: bool = False,
) -> _Fixture:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    paths = RepoPaths(root=root)
    source_root = Path(__file__).resolve().parents[2]

    finalizer_policy = root / "configs/generation/lf021_gate5g_finalizer_v1.yaml"
    finalizer_policy.parent.mkdir(parents=True)
    shutil.copy2(
        source_root / "configs/generation/lf021_gate5g_finalizer_v1.yaml",
        finalizer_policy,
    )
    finalizer_implementation = root / "src/leanfaith/generation/gate5g.py"
    finalizer_implementation.parent.mkdir(parents=True)
    shutil.copy2(
        source_root / "src/leanfaith/generation/gate5g.py",
        finalizer_implementation,
    )
    base_prevalence_design = root / "policies/lf021_prevalence_design_v1.yaml"
    prevalence_design_policy = root / "policies/lf021_prevalence_design_v2.yaml"
    base_prevalence_design.parent.mkdir(parents=True)
    shutil.copy2(
        source_root / "policies/lf021_prevalence_design_v1.yaml",
        base_prevalence_design,
    )
    shutil.copy2(
        source_root / "policies/lf021_prevalence_design_v2.yaml",
        prevalence_design_policy,
    )

    frame_policy = root / "configs/generation/lf021_frame_freeze_v3.yaml"
    frame_implementation = root / "src/leanfaith/generation/frame_freeze_v3.py"
    _write_json(frame_policy, {"schema_version": 3, "fixture": "frame policy"})
    _write_json(frame_implementation, {"schema_version": 3, "fixture": "implementation"})
    v2_stop = root / "reports/generation/v2_stop.json"
    _write_json(v2_stop, {"schema_version": 2, "fixture": "verified stop"})

    tranche_id = "synthetic_s0"
    postprocess_manifest_id = "research_postprocess_v6_manifest:" + "1" * 64
    collection_terminals: dict[str, str] = {}
    postprocess_terminals: dict[str, str] = {}
    population_items: list[EligiblePopulationItemV3] = []
    family_counts: Counter[str] = Counter()
    pool_counts: Counter[str] = Counter()
    proxy_counts: Counter[str] = Counter()
    stratum_counts: Counter[str] = Counter()

    for index in range(item_count):
        family = _FAMILIES[index % len(_FAMILIES)]
        invocation_id = f"research_collection_invocation:{index + 1:064x}"
        problem_group = f"problem-group:{index + 1:064x}"
        problem_id = f"problem:{index + 1:064x}"
        theorem_id = f"thm:{index + 1:064x}"
        alpha = f"{index + 1:064x}"
        base = root / f"data/synthetic/{tranche_id}/invocations/{index:04d}"

        collection_terminal = base / "collection_terminal.json"
        collection_hash = _write_json(
            collection_terminal,
            {
                "schema_version": 5,
                "artifact_class": "research",
                "invocation_id": invocation_id,
                "family_id": family,
                "problem_record_id": problem_id,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        collection_terminals[str(collection_terminal.relative_to(root))] = collection_hash

        screening = base / "screening.json"
        screening_hash = _write_json(
            screening,
            {
                "schema_version": 1,
                "candidate_theorem_id": theorem_id,
                "problem_record_id": problem_id,
                "alpha_identity_fingerprint": alpha,
                "status": "clean",
                "benchmark_hits": [],
            },
        )
        representation = base / "representation.json"
        representation_hash = _write_json(
            representation,
            {
                "schema_version": 1,
                "theorem_id": theorem_id,
                "alpha_identity_fingerprint": alpha,
            },
        )
        terminal = base / "processing_terminal.json"
        terminal_hash = _write_json(
            terminal,
            {
                "schema_version": 6,
                "artifact_class": "research",
                "invocation_id": invocation_id,
                "family_id": family,
                "problem_record_id": problem_id,
                "status": "admitted_unresolved",
                "parser_executed": True,
                "lean_validation_executed": True,
                "screening_executed": True,
                "semantic_pool_admitted": True,
                "output_artifact_hashes": {
                    str(representation.relative_to(root)): representation_hash,
                    str(screening.relative_to(root)): screening_hash,
                },
                "candidate_theorem_id": theorem_id,
                "same_claim": True if semantic_terminal_label else None,
                "relation": None,
                "resolution_outcome": "unresolved",
                "quality_tier": "unknown",
                "requires_adjudication": True,
                "decision": "REVIEW",
                "semantic_labels_created": False,
                "supervision_eligible": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        postprocess_terminals[str(terminal.relative_to(root))] = terminal_hash

        terminal_binding = _artifact(root, terminal)
        screening_binding = _artifact(root, screening)
        representation_binding = _artifact(root, representation)
        member = PopulationMemberV3(
            invocation_id=invocation_id,
            problem_group=problem_group,
            problem_record_id=problem_id,
            family_id=family,
            pool_id="pool_a",
            source_proxy="source_a",
            postprocess_manifest_id=postprocess_manifest_id,
            terminal_artifact=terminal_binding,
            screening_artifact=screening_binding,
            representation_artifact=representation_binding,
        )
        stratum = f"{family}|pool_a|source_a"
        cluster_id = "candidate_cluster_v2:" + hash_canonical(
            {
                "schema": "lf021_problem_group_alpha_cluster_v2",
                "problem_group": problem_group,
                "alpha_identity_fingerprint": alpha,
            }
        )
        item_payload: dict[str, Any] = {
            "schema_version": 3,
            "cluster_id": cluster_id,
            "problem_group": problem_group,
            "alpha_identity_fingerprint": alpha,
            "representative_invocation_id": invocation_id,
            "representative_family_id": family,
            "representative_pool_id": "pool_a",
            "representative_source_proxy": "source_a",
            "representative_problem_record_id": problem_id,
            "terminal_artifact": terminal_binding.model_dump(mode="json"),
            "screening_artifact": screening_binding.model_dump(mode="json"),
            "representation_artifact": representation_binding.model_dump(mode="json"),
            "members": [member.model_dump(mode="json")],
            "contributing_invocation_ids": [invocation_id],
            "contributing_problem_record_ids": [problem_id],
            "contributing_family_ids": [family],
            "contributing_pool_ids": ["pool_a"],
            "contributing_source_proxies": ["source_a"],
            "postprocess_manifest_ids": [postprocess_manifest_id],
            "member_count": 1,
            "member_count_by_family": {family: 1},
            "member_count_by_pool": {"pool_a": 1},
            "member_count_by_source_proxy": {"source_a": 1},
            "sampling_stratum": stratum,
            "same_claim": None,
            "relation": None,
            "resolution_outcome": "unresolved",
            "quality_tier": "unknown",
            "requires_adjudication": True,
            "decision": "REVIEW",
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        population_id = "lf021_eligible_population_item_v3:" + hash_canonical(
            {"schema": "lf021_eligible_population_item_v3", **item_payload}
        )
        population_items.append(
            EligiblePopulationItemV3.model_validate(
                {"population_record_id": population_id, **item_payload}
            )
        )
        family_counts[family] += 1
        pool_counts["pool_a"] += 1
        proxy_counts["source_a"] += 1
        stratum_counts[stratum] += 1

    family_revision_bindings: list[Gate5GFamilyRevisionBinding] = []
    family_session_artifact_hashes: dict[str, str] = {}
    overlap_family_artifacts: dict[str, dict[str, str]] = {}
    for family_index, family in enumerate(_FAMILIES):
        session_id = f"research_family_session:{family_index + 1:064x}"
        family_root = root / f"data/synthetic/{tranche_id}/families/{family}"
        session_start = family_root / "family_session_start.json"
        session_end = family_root / "family_session_end.json"
        model_repo_id = f"synthetic/{family}"
        model_revision = f"{family_index + 10:040x}"
        _write_json(
            session_start,
            {
                "schema_version": 1,
                "family_id": family,
                "family_session_id": session_id,
                "model_repo_id": model_repo_id,
                "model_revision": model_revision,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        _write_json(
            session_end,
            {
                "schema_version": 1,
                "family_id": family,
                "family_session_id": session_id,
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        family_session_artifact_hashes.update(
            {
                str(session_start.relative_to(root)): hash_file(session_start),
                str(session_end.relative_to(root)): hash_file(session_end),
            }
        )
        family_revision_bindings.append(
            Gate5GFamilyRevisionBinding(
                family_id=family,
                model_repo_id=model_repo_id,
                model_revision=model_revision,
                session_start=Gate5GArtifactBinding.model_validate(_binding(root, session_start)),
                session_end=Gate5GArtifactBinding.model_validate(_binding(root, session_end)),
            )
        )
        overlap_family = root / f"reports/overlap/{family}.json"
        _write_json(
            overlap_family,
            {
                "schema_version": 1,
                "family_id": family,
                "status": "clear",
                "semantic_labels_created": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )
        overlap_family_artifacts[family] = _binding(root, overlap_family)

    overlap_manifest = root / "reports/overlap/bundle_manifest.json"
    _write_json(
        overlap_manifest,
        {
            "schema_version": 1,
            "family_artifacts": dict(sorted(overlap_family_artifacts.items())),
            "family_count": len(_FAMILIES),
            "semantic_labels_created": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        },
    )

    collection_manifest = root / f"data/synthetic/{tranche_id}/collection_manifest.json"
    collection_manifest_id = "research_collection_manifest_v5:" + "0" * 64
    _write_json(
        collection_manifest,
        {
            "schema_version": 5,
            "manifest_id": collection_manifest_id,
            "tranche_id": tranche_id,
            "expected_candidate_count": item_count,
            "terminal_candidate_count": item_count,
            "family_count": 3,
            "family_session_artifact_hashes": dict(sorted(family_session_artifact_hashes.items())),
            "terminal_artifact_hashes": dict(sorted(collection_terminals.items())),
            "semantic_labels_created": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        },
    )
    postprocess_manifest = root / f"data/synthetic/{tranche_id}/postprocess_manifest.json"
    _write_json(
        postprocess_manifest,
        {
            "schema_version": 6,
            "manifest_id": postprocess_manifest_id,
            "tranche_id": tranche_id,
            "expected_invocations": item_count,
            "terminal_invocations": item_count,
            "family_count": 3,
            "status_counts": {"admitted_unresolved": item_count},
            "terminal_artifacts": dict(sorted(postprocess_terminals.items())),
            "input_binding": {
                "collection_manifest": _binding(root, collection_manifest),
                "collection_manifest_id": collection_manifest_id,
            },
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        },
    )

    collection_replay = root / f"reports/replay/{tranche_id}_collection.json"
    postprocess_replay = root / f"reports/replay/{tranche_id}_postprocess.json"
    for kind, manifest, output in (
        ("collection", collection_manifest, collection_replay),
        ("postprocess", postprocess_manifest, postprocess_replay),
    ):
        _write_json(
            output,
            {
                "schema_version": 1,
                "report_kind": f"lf021_{kind}_replay_certificate_v1",
                "tranche_id": tranche_id,
                "manifest": _binding(root, manifest),
                "replayed": True,
                "byte_identical": replay_byte_identical,
                "first_tree_sha256": "2" * 64,
                "replay_tree_sha256": ("2" * 64 if replay_byte_identical else "3" * 64),
                "expected_record_count": item_count,
                "replay_record_count": item_count,
                "semantic_labels_inspected": False,
                "semantic_labels_created": False,
                "supervision_eligible": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            },
        )

    tranche = Gate5GTrancheBindingV1(
        tranche_id=tranche_id,
        collection_manifest=Gate5GArtifactBinding.model_validate(
            _binding(root, collection_manifest)
        ),
        postprocess_manifest={
            **_binding(root, postprocess_manifest),
            "manifest_id": postprocess_manifest_id,
            "tranche_id": tranche_id,
        },
        collection_replay=Gate5GArtifactBinding.model_validate(_binding(root, collection_replay)),
        postprocess_replay=Gate5GArtifactBinding.model_validate(_binding(root, postprocess_replay)),
        family_ids=_FAMILIES,
        family_revisions=tuple(family_revision_bindings),
        overlap_manifest=Gate5GArtifactBinding.model_validate(_binding(root, overlap_manifest)),
        pool_ids=("pool_a",),
        source_proxies=("source_a",),
        expected_invocations=item_count,
        collection_terminal_count=item_count,
        postprocess_terminal_count=item_count,
        benchmark_clear_compiling_count=item_count,
    )
    lineage_payload: dict[str, Any] = {
        "schema_version": 1,
        "tranches": [tranche.model_dump(mode="json")],
        "scalable_family_ids": _FAMILIES,
        "pool_ids": ["pool_a"],
        "source_proxies": ["source_a"],
        "total_expected_invocations": item_count,
        "total_collection_terminals": item_count,
        "total_postprocess_terminals": item_count,
        "total_benchmark_clear_compiling": item_count,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    lineage_id = "lf021_gate5g_lineage:" + hash_canonical(
        {"schema": "lf021_gate5g_lineage_v1", **lineage_payload}
    )
    lineage = root / "data/synthetic/lineage_manifest.json"
    _write_json(
        lineage,
        Gate5GLineageManifestV1.model_validate(
            {"manifest_id": lineage_id, **lineage_payload}
        ).model_dump(mode="json"),
    )

    population_items_tuple = tuple(
        sorted(population_items, key=lambda item: item.population_record_id)
    )
    population_path = root / "artifacts/generation/population.jsonl"
    population_path.parent.mkdir(parents=True)
    population_path.write_bytes(
        b"".join(
            canonical_json_bytes(item.model_dump(mode="json")) + b"\n"
            for item in population_items_tuple
        )
    )
    population_manifest_payload: dict[str, Any] = {
        "schema_version": 3,
        "policy_id": "lf021_problem_aware_frame_freeze_v3",
        "policy_artifact": _binding(root, frame_policy),
        "v2_stop_decision_id": "lf021_expansion_decision_v2:" + "3" * 64,
        "v2_stop_decision": _binding(root, v2_stop),
        "v2_fixed_salt_frame_reused": False,
        "population_artifact": _binding(root, population_path),
        "population_item_count": item_count,
        "population_member_count": item_count,
        "stratum_population_sizes": dict(sorted(stratum_counts.items())),
        "frozen_at": "2026-07-24T00:00:00Z",
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    population_manifest_id = "lf021_eligible_population_v3:" + hash_canonical(
        {
            "schema": "lf021_eligible_population_manifest_v3",
            **population_manifest_payload,
        }
    )
    population_manifest = root / "artifacts/generation/population.manifest.json"
    _write_json(
        population_manifest,
        EligiblePopulationManifestV3.model_validate(
            {
                "population_id": population_manifest_id,
                **population_manifest_payload,
            }
        ).model_dump(mode="json"),
    )

    seed_bytes = bytes(range(32))
    seed_sha = sha256_hex(seed_bytes)
    seed_path = root / f"artifacts/generation/seeds/{seed_sha}.bin"
    seed_path.parent.mkdir(parents=True)
    seed_path.write_bytes(seed_bytes)
    seed_source = (
        "test_replay_seed_256" if test_replay_seed else "os_csprng_secrets_token_bytes_256"
    )
    seed_payload: dict[str, Any] = {
        "schema_version": 3,
        "source": seed_source,
        "entropy_bits": 256,
        "generated_at": "2026-07-24T00:00:01Z",
        "single_draw": True,
        "population_id": population_manifest_id,
        "population_manifest": _binding(root, population_manifest),
        "population_artifact": _binding(root, population_path),
        "sampling_seed": _binding(root, seed_path),
        "sampling_seed_sha256": seed_sha,
        "external_beacon_provenance": None,
        "test_replay_only": test_replay_seed,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    seed_id = "lf021_sampling_seed_v3:" + hash_canonical(
        {"schema": "lf021_sampling_seed_provenance_v3", **seed_payload}
    )
    seed_provenance = root / "artifacts/generation/seeds/provenance.json"
    _write_json(
        seed_provenance,
        SamplingSeedProvenanceV3.model_validate(
            {"provenance_id": seed_id, **seed_payload}
        ).model_dump(mode="json"),
    )
    seed_lock = root / (
        f"artifacts/generation/seeds/by_population/{population_manifest_id.rsplit(':', 1)[-1]}.json"
    )
    _write_json(
        seed_lock,
        SeedLockV3(
            population_id=population_manifest_id,
            sampling_seed_sha256=seed_sha,
            sampling_seed_provenance=_artifact(root, seed_provenance),
        ).model_dump(mode="json"),
    )

    frame_items: list[FrameItemV3] = []
    for item in population_items_tuple:
        n_h = stratum_counts[item.sampling_stratum]
        frame_payload: dict[str, Any] = {
            "schema_version": 3,
            "population_manifest_id": population_manifest_id,
            "population_manifest": _binding(root, population_manifest),
            **item.model_dump(
                mode="json",
                exclude={
                    "schema_version",
                    "same_claim",
                    "relation",
                    "semantic_labels_created",
                    "supervision_eligible",
                    "gate_5g_credit_claimed",
                    "gate_5_closed",
                },
            ),
            "stratum_population_size": n_h,
            "stratum_sample_size": n_h,
            "inclusion_probability_numerator": n_h,
            "inclusion_probability_denominator": n_h,
            "sampling_method": sampling_method,
            "sampling_rank_algorithm": _RANK_ALGORITHM,
            "sampling_rank_digest": _rank(
                seed=seed_bytes,
                stratum=item.sampling_stratum,
                cluster_id=item.cluster_id,
            ),
            "sampling_seed_sha256": seed_sha,
            "sampling_seed_provenance": _binding(root, seed_provenance),
            "test_replay_only": test_replay_seed,
            "same_claim": None,
            "relation": None,
            "resolution_outcome": "unresolved",
            "quality_tier": "unknown",
            "requires_adjudication": True,
            "decision": "REVIEW",
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
        }
        frame_record_id = "lf021_prevalence_item_v3:" + hash_canonical(
            {"schema": "lf021_prevalence_frame_item_v3", **frame_payload}
        )
        frame_items.append(
            FrameItemV3.model_validate({"frame_record_id": frame_record_id, **frame_payload})
        )
    frame_items_tuple = tuple(sorted(frame_items, key=lambda item: item.frame_record_id))
    frame = root / "artifacts/generation/frame.jsonl"
    frame.write_bytes(
        b"".join(
            canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in frame_items_tuple
        )
    )
    frame_binding_payload: dict[str, Any] = {
        "artifact": str(frame.relative_to(root)),
        "sha256": hash_file(frame),
        "item_count": item_count,
        "population_id": population_manifest_id,
        "population_manifest": _binding(root, population_manifest),
        "sampling_method": sampling_method,
        "sampling_seed_sha256": seed_sha,
        "sampling_seed_provenance": _binding(root, seed_provenance),
        "test_replay_only": test_replay_seed,
    }
    frame_id = "lf021_prevalence_frame_v3:" + hash_canonical(
        {"schema": "lf021_prevalence_frame_binding_v3", **frame_binding_payload}
    )
    frame_binding = FrameBindingV3.model_validate({"frame_id": frame_id, **frame_binding_payload})

    observation = v1.ObservationBinding(
        tranche_id=tranche_id,
        postprocess_manifest=_artifact(root, postprocess_manifest),
        manifest_id=postprocess_manifest_id,
        postprocess_schema_version=6,
        input_binding_hash="4" * 64,
    )
    family_item_counts = dict(sorted(family_counts.items()))
    counts = v1.OperationalCounts(
        observed_tranche_count=1,
        total_invocations=item_count,
        raw_collected_count=item_count,
        parser_success_count=item_count,
        compile_success_count=item_count,
        benchmark_rejected_count=0,
        benchmark_clear_compile_count=item_count,
        duplicate_member_count=0,
        unique_compiling_count=item_count,
        unique_contribution_by_family=family_item_counts,
        unique_representative_by_family=family_item_counts,
        unique_contribution_by_pool={"pool_a": item_count},
        unique_representative_by_pool={"pool_a": item_count},
        unique_contribution_by_family_pool={
            f"{family}|pool_a": count for family, count in family_item_counts.items()
        },
        unique_contribution_by_source_proxy={"source_a": item_count},
    )
    decision_payload: dict[str, Any] = {
        "schema_version": 3,
        "policy_id": "lf021_problem_aware_frame_freeze_v3",
        "policy_artifact": _binding(root, frame_policy),
        "implementation_artifact": _binding(root, frame_implementation),
        "v2_stop_decision_id": "lf021_expansion_decision_v2:" + "3" * 64,
        "v2_stop_decision": _binding(root, v2_stop),
        "observations": [observation.model_dump(mode="json")],
        "counts": counts.model_dump(mode="json"),
        "coverage_deficits": [],
        "action": "freeze_preferred_frame",
        "next_tranche": None,
        "v2_stop_action": "freeze_preferred_frame",
        "v2_fixed_salt_sampling_method": (
            "problem_aware_stratified_hash_srs_without_replacement_v2"
        ),
        "v2_fixed_salt_frame_reused": False,
        "population_id": population_manifest_id,
        "population_manifest": _binding(root, population_manifest),
        "population_artifact": _binding(root, population_path),
        "population_item_count": item_count,
        "population_member_count": item_count,
        "stratum_population_sizes": dict(sorted(stratum_counts.items())),
        "stratum_sample_sizes": dict(sorted(stratum_counts.items())),
        "sampling_method": sampling_method,
        "sampling_rank_algorithm": _RANK_ALGORITHM,
        "sampling_domain_separator": _DOMAIN,
        "sampling_rank_message_encoding": ("utf8_domain_nul_stratum_nul_cluster_id_v1"),
        "sampling_seed_sha256": seed_sha,
        "sampling_seed_provenance": _binding(root, seed_provenance),
        "test_replay_only": test_replay_seed,
        "frame": frame_binding.model_dump(mode="json"),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    decision_id = "lf021_frame_freeze_decision_v3:" + hash_canonical(
        {"schema": "lf021_frame_freeze_decision_v3", **decision_payload}
    )
    decision_model = FrameFreezeDecisionV3.model_validate(
        {"decision_id": decision_id, **decision_payload}
    )
    decision = root / "reports/generation/frame_freeze_decision_v3.json"
    _write_json(decision, decision_model.model_dump(mode="json"))

    seed_lock_hash = hash_file(seed_lock)
    coverage_literals = (
        "lf021_prevalence_design_v2",
        hash_file(prevalence_design_policy),
        hash_file(base_prevalence_design),
        decision_id,
        hash_file(decision),
        decision_model.policy_id,
        decision_model.policy_artifact.sha256,
        decision_model.implementation_artifact.sha256,
        decision_model.v2_stop_decision_id,
        decision_model.v2_stop_decision.sha256,
        decision_model.population_id,
        decision_model.population_manifest.sha256,
        decision_model.population_artifact.sha256,
        frame_id,
        hash_file(frame),
        seed_sha,
        decision_model.sampling_seed_provenance.sha256,
        seed_sha,
        seed_lock_hash,
        lineage_id,
        hash_file(lineage),
        *_FAMILIES,
        "three_family_collection_only",
        "source proxy",
        "Gate 5 remains open",
    )
    coverage = root / "reports/generation_coverage.md"
    coverage.parent.mkdir(parents=True, exist_ok=True)
    coverage.write_text("\n".join(("# Synthetic coverage", *coverage_literals)) + "\n")

    strata = tuple(
        Gate5GStratumAccounting(
            stratum=stratum,
            population_size=count,
            sample_size=count,
        )
        for stratum, count in sorted(stratum_counts.items())
    )
    reform = Gate5GReFormApplicability(
        applicable=False,
        status="not_applicable",
        reason="none of the three scalable Gate-5G families is ReForm",
        overlap_report=None,
    )
    validated_payload: dict[str, Any] = {
        "schema_version": 1,
        "frame_freeze_decision": _binding(root, decision),
        "frame_freeze_decision_id": decision_id,
        "frame": _binding(root, frame),
        "frame_id": frame_id,
        "frame_item_count": item_count,
        "lineage_manifest": _binding(root, lineage),
        "lineage_manifest_id": lineage_id,
        "coverage_report": _binding(root, coverage),
        "sampling_method": sampling_method,
        "sampling_seed_sha256": seed_sha,
        "sampling_seed_provenance": _binding(root, seed_provenance),
        "family_item_counts": family_item_counts,
        "pool_item_counts": dict(sorted(pool_counts.items())),
        "source_proxy_item_counts": dict(sorted(proxy_counts.items())),
        "strata": [item.model_dump(mode="json") for item in strata],
        "scope_limitations": _scope().model_dump(mode="json"),
        "reform_applicability": reform.model_dump(mode="json"),
        "benchmark_clear_count": item_count,
        "compiling_count": item_count,
        "unresolved_count": item_count,
        "semantic_label_count": 0,
        "supervision_eligible_count": 0,
        "semantic_labels_created": False,
        "gate_5g_closed": False,
        "gate_5_closed": False,
    }
    validated_id = "lf021_validated_real_outputs:" + hash_canonical(
        {"schema": "lf021_validated_real_outputs_v1", **validated_payload}
    )
    validated = root / "data/real_outputs/validated/manifest_v1.json"
    _write_json(
        validated,
        ValidatedRealOutputsManifestV1.model_validate(
            {"manifest_id": validated_id, **validated_payload}
        ).model_dump(mode="json"),
    )

    phase = root / "reports/milestones/phase_5_real_outputs.md"
    phase.parent.mkdir(parents=True)
    phase.write_text(
        "\n".join(
            (
                "# Phase 5",
                *coverage_literals,
                hash_file(coverage),
                validated_id,
                hash_file(validated),
                "Gate 5G is ready to finalize",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return _Fixture(
        paths=paths,
        decision=decision,
        lineage=lineage,
        validated=validated,
        coverage=coverage,
        phase=phase,
        prevalence_design_policy=prevalence_design_policy,
        policy=finalizer_policy,
    )


def _run(fixture: _Fixture, **kwargs: object) -> object:
    return validate_or_finalize_gate5g(
        paths=fixture.paths,
        frame_freeze_decision_path=fixture.decision,
        lineage_manifest_path=fixture.lineage,
        validated_manifest_path=fixture.validated,
        coverage_report_path=fixture.coverage,
        phase_milestone_path=fixture.phase,
        prevalence_design_policy_path=fixture.prevalence_design_policy,
        policy_path=fixture.policy,
        **kwargs,
    )


def test_gate5g_dry_run_is_content_addressed_and_cannot_close_gate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    result = _run(fixture)
    assert result.validation_report.validation_status == "ready_to_finalize"
    assert result.validation_report.prevalence_design_policy_id == "lf021_prevalence_design_v2"
    assert result.validation_report.input_bindings.prevalence_design_policy.sha256 == hash_file(
        fixture.prevalence_design_policy
    )
    assert not result.validation_report.gate_5g_closed
    assert result.gate_report is None
    assert (fixture.paths.root / result.validation_report_path).is_file()
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()
    assert _run(fixture).validation_report_sha256 == result.validation_report_sha256


def test_gate5g_explicit_finalize_writes_only_canonical_gate_path(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    result = _run(
        fixture,
        finalize=True,
        finalized_date=datetime.date(2026, 7, 24),
    )
    assert result.gate_report is not None
    assert result.gate_report.gate_5g_closed
    assert not result.gate_report.gate_5_closed
    assert result.gate_report_path == "reports/gates/gate_5g.json"
    assert (fixture.paths.root / result.gate_report_path).is_file()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"item_count": 199}, "decision|frame"),
        (
            {"sampling_method": "problem_aware_stratified_hash_srs_without_replacement_v2"},
            "literal|sampling",
        ),
        ({"semantic_terminal_label": True}, "semantic label|same_claim"),
        ({"replay_byte_identical": False}, "replay"),
        ({"test_replay_seed": True}, "test/replay"),
    ],
)
def test_gate5g_invalid_synthetic_inputs_fail_without_gate_or_validation(
    tmp_path: Path,
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises((Gate5GFinalizationError, ValueError), match=match):
        fixture = _fixture(tmp_path, **kwargs)
        _run(fixture)
    root = tmp_path / "repo"
    assert not (root / "reports/gates/gate_5g.json").exists()
    assert not (root / "reports/generation/lf021_gate5g_finalization_v1").exists()


def test_gate5g_rejects_v2_decision_and_tampered_artifact_without_writing(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    decision = json.loads(fixture.decision.read_text(encoding="utf-8"))
    decision["schema_version"] = 2
    decision["decision_id"] = "lf021_expansion_decision_v2:" + "5" * 64
    _write_json(fixture.decision, decision)
    with pytest.raises((Gate5GFinalizationError, ValueError), match="decision"):
        _run(fixture)
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()

    fixture = _fixture(tmp_path / "tampered")
    first_terminal = next(
        (fixture.paths.root / "data/synthetic/synthetic_s0/invocations").glob(
            "*/processing_terminal.json"
        )
    )
    first_terminal.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(Gate5GFinalizationError, match=r"binding|hash|artifact"):
        _run(fixture)
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()


def test_gate5g_finalize_flag_and_date_are_an_explicit_pair(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(Gate5GFinalizationError, match="finalize mode"):
        _run(fixture, finalized_date=datetime.date(2026, 7, 24))
    with pytest.raises(Gate5GFinalizationError, match="finalize mode"):
        _run(fixture, finalize=True)
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()


def test_gate5g_rejects_tampered_family_revision_or_overlap_lineage(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    session_start = next(
        (fixture.paths.root / "data/synthetic/synthetic_s0/families").glob(
            "*/family_session_start.json"
        )
    )
    session_start.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(Gate5GFinalizationError, match=r"family session|artifact|hash"):
        _run(fixture)
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()

    overlap_fixture = _fixture(tmp_path / "overlap")
    overlap_family = next((overlap_fixture.paths.root / "reports/overlap").glob("*.json"))
    overlap_family.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(Gate5GFinalizationError, match=r"overlap|artifact|hash"):
        _run(overlap_fixture)
    assert not (overlap_fixture.paths.root / "reports/gates/gate_5g.json").exists()


def test_gate5g_rejects_tampered_prevalence_design_binding(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    base_policy = fixture.paths.root / "policies/lf021_prevalence_design_v1.yaml"
    base_policy.write_text(
        base_policy.read_text(encoding="utf-8") + "\n# tampered after freeze\n",
        encoding="utf-8",
    )
    with pytest.raises(Gate5GFinalizationError, match=r"prevalence design|base"):
        _run(fixture)
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()
    assert not (fixture.paths.root / "reports/generation/lf021_gate5g_finalization_v1").exists()


def test_gate5g_strict_v3_replay_failure_cannot_write_any_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def fail_replay(**_kwargs: object) -> object:
        raise FrameFreezeV3Error("synthetic HMAC replay mismatch")

    monkeypatch.setattr(gate5g_module, "verify_frame_freeze_v3", fail_replay)
    with pytest.raises(Gate5GFinalizationError, match="replay failed"):
        _run(
            fixture,
            finalize=True,
            finalized_date=datetime.date(2026, 7, 24),
        )
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()
    assert not (fixture.paths.root / "reports/generation/lf021_gate5g_finalization_v1").exists()


def test_close_gate5g_typer_dry_run_never_mutates_gate_path(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "close-gate5g",
            "--root",
            str(fixture.paths.root),
            "--frame-freeze-decision",
            str(fixture.decision),
            "--lineage-manifest",
            str(fixture.lineage),
            "--validated-manifest",
            str(fixture.validated),
            "--coverage-report",
            str(fixture.coverage),
            "--phase-milestone",
            str(fixture.phase),
            "--prevalence-design-policy",
            str(fixture.prevalence_design_policy),
            "--policy",
            str(fixture.policy),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "gate_5g_closed=false" in result.output
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()


def test_gate5g_publication_rejects_symlinked_namespace(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    outside = fixture.paths.root / "outside"
    outside.mkdir()
    namespace = fixture.paths.root / "reports/generation/lf021_gate5g_finalization_v1"
    namespace.parent.mkdir(parents=True, exist_ok=True)
    namespace.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        Gate5GFinalizationError,
        match=r"publication parent|symlink|trusted",
    ):
        _run(fixture)

    assert not list(outside.iterdir())
    assert not (fixture.paths.root / "reports/gates/gate_5g.json").exists()


def test_gate5g_publication_rejects_symlinked_final_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"identical\n")
    target = root / "reports/generation/validation.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    with pytest.raises(
        Gate5GFinalizationError,
        match=r"trusted regular file|symlink",
    ):
        gate5g_module._write_immutable(
            target,
            b"identical\n",
            repo_root=root,
            label="synthetic Gate-5G validation report",
        )

    assert target.is_symlink()
    assert outside.read_bytes() == b"identical\n"


def test_gate5g_publication_directory_swap_fails_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    trusted = root / "reports/generation/run"
    trusted.mkdir(parents=True)
    moved = root / "moved-trusted"
    outside = root / "outside"
    outside.mkdir()
    target = trusted / "validation.json"
    original_link = gate5g_module.os.link
    swapped = False

    def swap_then_link(
        src: str,
        dst: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        if not swapped:
            trusted.rename(moved)
            trusted.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(gate5g_module.os, "link", swap_then_link)
    with pytest.raises(
        Gate5GFinalizationError,
        match=r"symlink|reachable|path changed",
    ):
        gate5g_module._write_immutable(
            target,
            b"payload\n",
            repo_root=root,
            label="synthetic Gate-5G validation report",
        )

    assert not (outside / "validation.json").exists()
    assert not (moved / "validation.json").exists()
