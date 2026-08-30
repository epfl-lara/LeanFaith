"""Deterministic selection tests for the full public S1 repair build."""

from __future__ import annotations

from pathlib import Path

from leanfaith.corpus2.build_v1 import CorpusCandidate, MergedPair, ScreenedCandidate
from leanfaith.corpus2.s1_public_repair import META_SOURCE_KIND
from leanfaith.corpus2.s1_public_repair_build import (
    D3_SOURCE,
    RECOVERED_SOURCE,
    MetaCapMetadata,
    S1PublicRepairBuildConfig,
    apply_meta_ancestry_cap,
    apply_ratio_caps,
    production_config,
    ratio_memberships_for_row,
)


def _screened(index: int, declaration: str) -> tuple[ScreenedCandidate, MetaCapMetadata]:
    origin_id = f"meta-origin-{index:03d}"
    candidate = CorpusCandidate(
        origin_id=origin_id,
        source_kind=META_SOURCE_KIND,
        reference_headless=f"(n : Nat) : n + {index} = n + {index}",
        candidate_headless=f"(n : Nat) : {index} + n = {index} + n",
        label=True,
        split_group_ids=(f"mathlib-declaration:{declaration}",),
        family_ids=("P20",),
        provenance_ids=(f"primary-{index}", f"audit-{index}"),
        split_anchor=None,
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=False,
        release_eligible=True,
    )
    metadata = MetaCapMetadata(
        origin_id=origin_id,
        declaration=declaration,
        family="P20",
        evidence_class="P-DEF",
        operation=f"unfold:Fixture{index}",
        source_site_hash=f"{index + 1:064x}",
        candidate_key=(
            declaration,
            "P20",
            f"unfold:Fixture{index}",
            f"/{index}",
            f"{index + 100:064x}",
        ),
    )
    return (
        ScreenedCandidate(
            candidate=candidate,
            reference_near_hash=f"{index + 200:064x}",
            candidate_near_hash=f"{index + 300:064x}",
            pair_key=(f"{index + 200:064x}", f"{index + 300:064x}"),
            forward_tokens=10,
            reverse_tokens=10,
        ),
        metadata,
    )


def _merged(
    index: int,
    *,
    source_kinds: tuple[str, ...] = ("fixture",),
    origin_ids: tuple[str, ...] | None = None,
    family: str = "fixture",
) -> MergedPair:
    return MergedPair(
        pair_id=f"pair-{index:03d}",
        pair_key=(f"hash-a-{index:03d}", f"hash-b-{index:03d}"),
        reference_headless=f"reference {index}",
        candidate_headless=f"candidate {index}",
        label=index % 2 == 0,
        split_group_ids=(f"ancestry-{index:03d}",),
        family_ids=(family,),
        origin_ids=origin_ids or (f"origin-{index:03d}",),
        source_kinds=source_kinds,
        provenance_ids=(f"provenance-{index:03d}",),
        split_anchors=(),
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=True,
        release_eligible=True,
        forward_tokens=10,
        reverse_tokens=10,
    )


def _config() -> S1PublicRepairBuildConfig:
    return production_config(Path("/storage/milikic/leanfaith/corpus2/repair-build-test"))


def test_meta_ancestry_cap_is_deterministic_and_keeps_four() -> None:
    fixtures = [_screened(index, "Fixture.same") for index in range(7)]
    rows = [item[0] for item in fixtures]
    metadata = {item.candidate.origin_id: meta for item, meta in fixtures}

    retained, exclusions = apply_meta_ancestry_cap(
        rows,
        metadata=metadata,
        seed=20260829,
        limit=4,
    )
    replay, replay_exclusions = apply_meta_ancestry_cap(
        list(reversed(rows)),
        metadata=metadata,
        seed=20260829,
        limit=4,
    )

    assert [row.candidate.origin_id for row in retained] == [
        row.candidate.origin_id for row in replay
    ]
    assert exclusions == replay_exclusions
    assert len(retained) == 4
    assert len(exclusions) == 3


def test_recovered_source_cap_reaches_twenty_percent_fixed_point() -> None:
    config = _config()
    rows = [
        _merged(
            index,
            source_kinds=(RECOVERED_SOURCE,) if index < 30 else ("fixture",),
        )
        for index in range(100)
    ]

    retained, exclusions = apply_ratio_caps(rows, metadata={}, config=config)
    replay, replay_exclusions = apply_ratio_caps(list(reversed(rows)), metadata={}, config=config)
    recovered = sum(RECOVERED_SOURCE in row.source_kinds for row in retained)

    assert [row.pair_id for row in retained] == [row.pair_id for row in replay]
    assert exclusions == replay_exclusions
    assert 100 * recovered <= config.recovered_source_percent * len(retained)
    assert len(retained) < len(rows)


def test_d3_overlap_is_protected_from_recovered_source_cap() -> None:
    config = _config()
    rows = [_merged(index, source_kinds=(RECOVERED_SOURCE, D3_SOURCE)) for index in range(30)] + [
        _merged(index + 30) for index in range(70)
    ]

    retained, exclusions = apply_ratio_caps(rows, metadata={}, config=config)

    assert len(retained) == 100
    assert exclusions == []


def test_new_meta_family_cap_uses_whole_corpus_denominator() -> None:
    config = _config()
    metadata: dict[str, MetaCapMetadata] = {}
    rows = [_merged(index) for index in range(80)]
    for index in range(20):
        origin_id = f"meta-{index:03d}"
        metadata[origin_id] = MetaCapMetadata(
            origin_id=origin_id,
            declaration=f"Fixture.{index}",
            family="P21",
            evidence_class="P-DEF",
            operation="betaIntroduce",
            source_site_hash=f"{index + 1:064x}",
            candidate_key=(
                f"Fixture.{index}",
                "P21",
                "betaIntroduce",
                "/1",
                f"{index + 100:064x}",
            ),
        )
        rows.append(
            _merged(
                index + 80,
                source_kinds=(META_SOURCE_KIND,),
                origin_ids=(origin_id,),
                family="P21",
            )
        )

    retained, _ = apply_ratio_caps(rows, metadata=metadata, config=config)
    meta_count = sum(META_SOURCE_KIND in row.source_kinds for row in retained)

    assert 100 * meta_count <= config.caps.family_percent * len(retained)


def test_ratio_memberships_record_source_and_meta_dimensions() -> None:
    config = _config()
    origin_id = "meta-001"
    metadata = {
        origin_id: MetaCapMetadata(
            origin_id=origin_id,
            declaration="Fixture.one",
            family="P20",
            evidence_class="P-DEF",
            operation="unfold:Fixture",
            source_site_hash="1" * 64,
            candidate_key=("Fixture.one", "P20", "unfold:Fixture", "/1", "2" * 64),
        )
    }
    row = _merged(
        1,
        source_kinds=(RECOVERED_SOURCE, META_SOURCE_KIND),
        origin_ids=(origin_id,),
        family="P20",
    )

    memberships = ratio_memberships_for_row(row, metadata=metadata, config=config)

    assert {item.rule for item in memberships} == {
        "recovered_source",
        "meta_family",
        "meta_mechanism",
        "meta_exact_template",
        "meta_exact_rewrite_lemma",
    }


def test_production_build_contract_binds_smoke_and_tokenizer() -> None:
    config = _config()

    assert config.smoke_manifest.sha256 == (
        "32f825b94d77ad578372537dfdc45a10c8a9dfbdeaeb9559ace3ae6687feaf49"
    )
    assert config.recovered_source_percent == 20
    assert config.canary_target_balanced_accuracy == 0.72
    assert set(config.tokenizer_files) == {
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    }
