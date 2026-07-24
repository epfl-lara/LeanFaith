from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.datasets.denylist import (
    DenylistIndex,
    FrozenBenchmark,
    FrozenRegistry,
    nl_hash,
)
from leanfaith.generation.config import (
    NearDuplicateConfig,
    ProblemPoolConfig,
    ProblemPoolOutputConfig,
    ProblemPoolSourceConfig,
    SourceAuthorizationConfig,
    load_problem_pool_config,
)
from leanfaith.generation.problem_pool import (
    ProblemPoolBuildError,
    ProblemPoolCandidate,
    ProblemPoolDenylistBinding,
    ProblemPoolExclusionReason,
    build_problem_pool,
    to_public_trusted_problem,
)
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.ids import CONTEXT_PREFIX, THEOREM_PREFIX, make_id

ROOT = Path(__file__).resolve().parents[2]
CONTEXT_ID = make_id(CONTEXT_PREFIX, {"problem_pool": "fixture"})
REFERENCE_ID = make_id(THEOREM_PREFIX, {"problem_pool": "reference"})
UTC = datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC)
BENCHMARK_MANIFEST = "data/benchmarks/manifests/representation_signatures_v1.json"
BENCHMARK_MANIFEST_SHA256 = "a" * 64
ACTIVE_REGISTRY_SHA256 = "b" * 64
SOURCE_CONFIG_SHA256 = "c" * 64


def _source(
    source: str,
    *,
    enabled: bool = True,
    private: bool = False,
    external: bool = True,
    allowed_trust: tuple[NLTrust, ...] = (NLTrust.TRUSTED,),
) -> ProblemPoolSourceConfig:
    authorization = SourceAuthorizationConfig(
        source_revision="source-r1",
        license_id="undeclared" if private else "CC-BY-4.0",
        private_source=private,
        external_transmission=external,
        release_eligible=not private,
    )
    return ProblemPoolSourceConfig(
        source=source,
        source_config=f"configs/sources/{source}.yaml",
        source_config_sha256=SOURCE_CONFIG_SHA256,
        authorization=authorization,
        enabled=enabled,
        private_source=private,
        external_provider_eligible=external,
        allowed_trust=allowed_trust,
        require_reference_theorem=True,
    )


def _config(
    *sources: ProblemPoolSourceConfig,
    status: str = "ready",
) -> ProblemPoolConfig:
    return ProblemPoolConfig.model_validate(
        {
            "config_id": "problem_pool_v1",
            "status": status,
            "selection_seed": "problem-pool-test",
            "sources": [source.model_dump(mode="json") for source in sources],
            "active_benchmark_registry_manifest": BENCHMARK_MANIFEST,
            "active_benchmark_registry_manifest_sha256": BENCHMARK_MANIFEST_SHA256,
            "benchmark_preflight_required": True,
            "normalized_nl_exact_dedup": True,
            "near_duplicate": NearDuplicateConfig(
                status="frozen",
                method="supplied_group_ids",
                method_version="v1",
                threshold=1.0,
            ).model_dump(mode="json"),
            "private_source_external_transmission": False,
            "public_replication_profile": "configs/sources/public_replication.yaml",
            "outputs": ProblemPoolOutputConfig(
                records="data/parsed/real_outputs/problem_pool_v1.jsonl",
                failures="data/parsed/real_outputs/problem_pool_failures_v1.jsonl",
                manifest="data/parsed/real_outputs/problem_pool_manifest_v1.json",
                coverage_report="reports/generation_coverage.md",
            ).model_dump(mode="json"),
        }
    )


def _index(
    *,
    row_ids: tuple[str, ...] = (),
    protected_nl: tuple[str, ...] = (),
) -> DenylistIndex:
    benchmark = FrozenBenchmark(
        registry_key="fixture_benchmark",
        source_id="fixture/benchmark",
        revision="fixture-r1",
        resolved=True,
        row_ids=tuple(sorted(row_ids)),
        nl_hashes=tuple(sorted(nl_hash(text) for text in protected_nl)),
    )
    return DenylistIndex(
        FrozenRegistry(
            frozen_at=UTC,
            benchmarks=(benchmark,),
            representation_signatures_appended=True,
        )
    )


def _denylist(
    *,
    row_ids: tuple[str, ...] = (),
    protected_nl: tuple[str, ...] = (),
) -> ProblemPoolDenylistBinding:
    index = _index(row_ids=row_ids, protected_nl=protected_nl)
    return ProblemPoolDenylistBinding(
        index=index,
        manifest_path=BENCHMARK_MANIFEST,
        manifest_sha256=BENCHMARK_MANIFEST_SHA256,
        active_registry_sha256=ACTIVE_REGISTRY_SHA256,
        registry_content_hash=index.registry_content_hash,
    )


def _candidate(row: int, **overrides: object) -> ProblemPoolCandidate:
    values: dict[str, object] = {
        "problem_id": f"problem-{row}",
        "problem_group": f"nl-group:{row}",
        "source": "public_source",
        "source_revision": "source-r1",
        "source_split": "train",
        "source_record_id": f"row-{row}",
        "source_record_content_hash": f"{row % 10}" * 64,
        "nl_statement": f"Show that the fixture proposition {row} is true.",
        "nl_trust": NLTrust.TRUSTED,
        "nl_source_link": f"https://example.test/problems/{row}",
        "context_id": CONTEXT_ID,
        "import_header_artifact": f"artifacts/headers/{row}.lean",
        "import_header_hash": f"{(row + 1) % 10}" * 64,
        "reference_theorem_ids": (REFERENCE_ID,),
        "source_license": "CC-BY-4.0",
        "private_source_content": False,
        "release_eligible": True,
    }
    values.update(overrides)
    return ProblemPoolCandidate.model_validate(values)


def test_checked_in_disabled_config_fails_closed() -> None:
    checked_in = load_problem_pool_config(ROOT / "configs/generation/problem_pool_v1.yaml").config
    with pytest.raises(ProblemPoolBuildError, match="status=ready"):
        build_problem_pool(
            config=checked_in,
            denylist=_denylist(),
            candidates=(),
        )


def test_build_is_deterministic_order_independent_and_projects_public_trusted() -> None:
    config = _config(_source("public_source"))
    candidates = (_candidate(3), _candidate(1), _candidate(2))

    first = build_problem_pool(
        config=config,
        denylist=_denylist(),
        candidates=candidates,
    )
    second = build_problem_pool(
        config=config,
        denylist=_denylist(),
        candidates=tuple(reversed(candidates)),
    )

    assert first == second
    assert len(first.records) == len(candidates)
    assert [record.problem_record_id for record in first.records] == sorted(
        record.problem_record_id for record in first.records
    )
    assert {record.eligibility for record in first.records} == {"eligible"}
    assert {record.schema_version for record in first.records} == {2}
    assert {record.source_config_sha256 for record in first.records} == {SOURCE_CONFIG_SHA256}
    assert {record.denylist_manifest_sha256 for record in first.records} == {
        BENCHMARK_MANIFEST_SHA256
    }
    assert all(record.source_authorization_hash for record in first.records)
    assert all(record.denylist_registry_content_hash for record in first.records)
    assert len(first.public_trusted_problems) == 3
    projected = first.public_trusted_problems[0]
    assert projected.nl_trust is NLTrust.TRUSTED
    assert projected.source_is_public is True
    assert projected.external_transmission_allowed is True
    assert projected.source_license == "CC-BY-4.0"


def test_source_trust_and_reference_rules_produce_explicit_terminal_exclusions() -> None:
    config = _config(
        _source("public_source"),
        _source("disabled_source", enabled=False, external=False),
    )
    candidates = (
        _candidate(1, source="missing_source"),
        _candidate(2, source="disabled_source"),
        _candidate(3, nl_trust=NLTrust.SYNTHETIC),
        _candidate(4, reference_theorem_ids=()),
    )

    result = build_problem_pool(
        config=config,
        denylist=_denylist(),
        candidates=candidates,
    )
    by_problem = {record.problem_id: record for record in result.records}

    assert by_problem["problem-1"].exclusion_reasons == ("source_not_configured",)
    assert by_problem["problem-2"].exclusion_reasons == ("source_disabled",)
    assert by_problem["problem-3"].exclusion_reasons == ("nl_trust_not_allowed",)
    assert by_problem["problem-4"].exclusion_reasons == ("missing_reference_theorem",)
    assert all(record.eligibility == "excluded" for record in result.records)
    assert all(record.denylist_checked for record in result.records)
    assert by_problem["problem-1"].source_config_sha256 is None
    assert by_problem["problem-1"].source_authorization_hash is None
    assert by_problem["problem-1"].source_license is None
    assert by_problem["problem-1"].denylist_manifest_sha256 == BENCHMARK_MANIFEST_SHA256
    assert result.public_trusted_problems == ()


def test_disabled_source_without_authorization_still_emits_terminal_v2_record() -> None:
    disabled = ProblemPoolSourceConfig(
        source="disabled_source",
        source_config="configs/sources/disabled_source.yaml",
        enabled=False,
        private_source=False,
        external_provider_eligible=False,
        allowed_trust=(NLTrust.TRUSTED,),
        require_reference_theorem=True,
    )
    result = build_problem_pool(
        config=_config(_source("public_source"), disabled),
        denylist=_denylist(),
        candidates=(_candidate(1, source="disabled_source"),),
    )

    record = result.records[0]
    assert record.schema_version == 2
    assert record.exclusion_reasons == ("source_disabled",)
    assert record.source_config_sha256 is None
    assert record.source_authorization_hash is None
    assert record.source_license is None
    assert record.denylist_manifest_sha256 == BENCHMARK_MANIFEST_SHA256


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_revision", "wrong-revision", "source revision"),
        ("source_license", "wrong-license", "source license"),
    ],
)
def test_enabled_source_candidate_must_match_bound_authorization(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ProblemPoolBuildError, match=message):
        build_problem_pool(
            config=_config(_source("public_source")),
            denylist=_denylist(),
            candidates=(_candidate(1, **{field: value}),),
        )


def test_problem_pool_rejects_denylist_binding_not_named_by_config() -> None:
    index = _index()
    wrong = ProblemPoolDenylistBinding(
        index=index,
        manifest_path=BENCHMARK_MANIFEST,
        manifest_sha256="f" * 64,
        active_registry_sha256=ACTIVE_REGISTRY_SHA256,
        registry_content_hash=index.registry_content_hash,
    )
    with pytest.raises(ProblemPoolBuildError, match="denylist binding"):
        build_problem_pool(
            config=_config(_source("public_source")),
            denylist=wrong,
            candidates=(_candidate(1),),
        )


def test_denylist_checks_row_identity_and_normalized_nl() -> None:
    protected_text = "This NL statement is protected."
    result = build_problem_pool(
        config=_config(_source("public_source")),
        denylist=_denylist(
            row_ids=("blocked-alias", "row-1"),
            protected_nl=(protected_text,),
        ),
        candidates=(
            _candidate(1),
            _candidate(
                2,
                nl_statement="  THIS nl statement  is protected. ",
                denylist_row_ids=("blocked-alias",),
            ),
        ),
    )
    by_problem = {record.problem_id: record for record in result.records}

    row_hit = by_problem["problem-1"]
    assert row_hit.exclusion_reasons == ("denylist_hit",)
    assert row_hit.denylist_hits == ("row_id:row-1",)

    combined = by_problem["problem-2"]
    assert combined.exclusion_reasons == ("denylist_hit",)
    assert combined.denylist_hits == (
        f"normalized_nl:{nl_hash(protected_text)}",
        "row_id:blocked-alias",
    )


def test_exact_normalized_nl_dedup_keeps_one_valid_canonical_record() -> None:
    left = _candidate(1, nl_statement="For all n, n = n.")
    right = _candidate(2, nl_statement="  FOR all n,   n = n. ")
    result = build_problem_pool(
        config=_config(_source("public_source")),
        denylist=_denylist(),
        candidates=(right, left),
    )

    canonical_id = min(left.problem_record_id, right.problem_record_id)
    eligible = [record for record in result.records if record.eligibility == "eligible"]
    duplicate = [record for record in result.records if record.eligibility == "excluded"]
    assert [record.problem_record_id for record in eligible] == [canonical_id]
    assert len(duplicate) == 1
    assert duplicate[0].exact_duplicate_of == canonical_id
    assert duplicate[0].exclusion_reasons == ("exact_normalized_nl_duplicate",)


def test_denylisted_member_protects_all_exact_normalized_nl_copies() -> None:
    blocked = _candidate(
        1,
        nl_statement="The same normalized problem.",
        denylist_row_ids=("blocked-alias",),
    )
    clean = _candidate(2, nl_statement="  THE same normalized problem. ")
    first = build_problem_pool(
        config=_config(_source("public_source")),
        denylist=_denylist(row_ids=("blocked-alias",)),
        candidates=(blocked, clean),
    )
    second = build_problem_pool(
        config=_config(_source("public_source")),
        denylist=_denylist(row_ids=("blocked-alias",)),
        candidates=(clean, blocked),
    )
    assert first == second
    by_problem = {record.problem_id: record for record in first.records}

    assert by_problem["problem-1"].eligibility == "excluded"
    assert by_problem["problem-1"].exclusion_reasons == ("denylist_hit",)
    assert by_problem["problem-1"].denylist_hits == ("row_id:blocked-alias",)
    assert by_problem["problem-2"].eligibility == "excluded"
    assert by_problem["problem-2"].exclusion_reasons == ("protected_exact_duplicate",)
    assert by_problem["problem-2"].exact_duplicate_of == by_problem["problem-1"].problem_record_id


def test_multiple_protected_members_choose_stable_protected_canonical() -> None:
    protected_left = _candidate(
        1,
        nl_statement="One protected problem.",
        denylist_row_ids=("blocked-left",),
    )
    protected_right = _candidate(
        2,
        nl_statement=" ONE protected problem. ",
        denylist_row_ids=("blocked-right",),
    )
    clean_copy = _candidate(3, nl_statement="one  PROTECTED problem.")
    result = build_problem_pool(
        config=_config(_source("public_source")),
        denylist=_denylist(row_ids=("blocked-left", "blocked-right")),
        candidates=(clean_copy, protected_right, protected_left),
    )

    protected_ids = {
        protected_left.problem_record_id,
        protected_right.problem_record_id,
    }
    canonical_id = min(protected_ids)
    by_id = {record.problem_record_id: record for record in result.records}
    assert by_id[canonical_id].exclusion_reasons == ("denylist_hit",)
    assert by_id[canonical_id].exact_duplicate_of is None

    other_protected_id = (protected_ids - {canonical_id}).pop()
    assert by_id[other_protected_id].exclusion_reasons == (
        "denylist_hit",
        "protected_exact_duplicate",
    )
    assert by_id[other_protected_id].exact_duplicate_of == canonical_id
    assert by_id[clean_copy.problem_record_id].exclusion_reasons == ("protected_exact_duplicate",)
    assert by_id[clean_copy.problem_record_id].exact_duplicate_of == canonical_id


def test_only_supplied_near_duplicate_groups_trigger_near_dedup() -> None:
    grouped_left = _candidate(
        1,
        nl_statement="Prove a statement about addition.",
        near_duplicate_group_ids=("near:shared",),
    )
    grouped_right = _candidate(
        2,
        nl_statement="Establish a related addition identity.",
        near_duplicate_group_ids=("near:shared",),
    )
    similar_without_group = _candidate(
        3,
        nl_statement="Prove one statement about addition.",
    )
    result = build_problem_pool(
        config=_config(_source("public_source")),
        denylist=_denylist(),
        candidates=(grouped_right, similar_without_group, grouped_left),
    )

    grouped_ids = {grouped_left.problem_record_id, grouped_right.problem_record_id}
    canonical_id = min(grouped_ids)
    by_id = {record.problem_record_id: record for record in result.records}
    assert by_id[canonical_id].eligibility == "eligible"
    duplicate_id = (grouped_ids - {canonical_id}).pop()
    assert by_id[duplicate_id].exclusion_reasons == ("supplied_near_duplicate",)
    assert by_id[duplicate_id].metadata["near_duplicate_canonical_id"] == canonical_id
    assert by_id[similar_without_group.problem_record_id].eligibility == "eligible"


def test_privacy_is_fail_closed_and_never_projected_for_external_prompting() -> None:
    config = _config(
        _source("public_source"),
        _source("private_source", private=True, external=False),
    )
    result = build_problem_pool(
        config=config,
        denylist=_denylist(),
        candidates=(
            _candidate(1, private_source_content=True),
            _candidate(
                2,
                source="private_source",
                source_license="undeclared",
                private_source_content=False,
            ),
        ),
    )

    assert len(result.records) == 2
    for record in result.records:
        assert record.eligibility == "eligible"
        assert record.private_source_content is True
        assert record.external_provider_eligible is False
        assert record.release_eligible is False
        with pytest.raises(ProblemPoolBuildError, match="eligible, public, trusted"):
            to_public_trusted_problem(record)
    assert result.public_trusted_problems == ()


def test_synthetic_public_problem_can_be_eligible_but_is_not_public_trusted() -> None:
    result = build_problem_pool(
        config=_config(
            _source(
                "public_source",
                allowed_trust=(NLTrust.TRUSTED, NLTrust.SYNTHETIC),
            )
        ),
        denylist=_denylist(),
        candidates=(_candidate(1, nl_trust=NLTrust.SYNTHETIC),),
    )
    assert result.records[0].eligibility == "eligible"
    assert result.records[0].external_provider_eligible is True
    assert result.public_trusted_problems == ()


def test_duplicate_immutable_candidate_identity_fails_instead_of_losing_accounting() -> None:
    candidate = _candidate(1)
    with pytest.raises(ProblemPoolBuildError, match="immutable identities"):
        build_problem_pool(
            config=_config(_source("public_source")),
            denylist=_denylist(),
            candidates=(candidate, candidate),
        )


def test_candidate_rejects_unsorted_provenance_and_blank_nl() -> None:
    with pytest.raises(ValidationError, match="near_duplicate_group_ids"):
        _candidate(1, near_duplicate_group_ids=("near:z", "near:a"))
    with pytest.raises(ValidationError, match="nl_statement"):
        _candidate(1, nl_statement="   ")


def test_reference_requirement_and_near_duplicate_method_are_not_configurable() -> None:
    source_payload = _source("public_source").model_dump(mode="json")
    source_payload["require_reference_theorem"] = False
    with pytest.raises(ValidationError, match="True"):
        ProblemPoolSourceConfig.model_validate(source_payload)

    with pytest.raises(ValidationError, match="supplied_group_ids"):
        NearDuplicateConfig.model_validate(
            {
                "status": "frozen",
                "method": "token_jaccard",
                "method_version": "v1",
                "threshold": 0.8,
            }
        )


def test_exclusion_reason_values_are_stable_and_complete() -> None:
    assert {reason.value for reason in ProblemPoolExclusionReason} == {
        "source_not_configured",
        "source_disabled",
        "nl_trust_not_allowed",
        "missing_reference_theorem",
        "denylist_hit",
        "protected_exact_duplicate",
        "exact_normalized_nl_duplicate",
        "supplied_near_duplicate",
    }
