"""Focused offline tests for the frozen corpus-v1 merge."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

import leanfaith.corpus2.build_v1 as corpus
from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import hash_file
from leanfaith.representations.views import signature_near_dup_hash


class FakeTokenizer:
    """Small deterministic tokenizer with the encode surface used by corpus-v1."""

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        pieces = re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)
        ids = [int(hashlib.sha256(piece.encode("utf-8")).hexdigest()[:8], 16) for piece in pieces]
        return [1, *ids, 2] if add_special_tokens else ids


class ReverseOverlengthTokenizer(FakeTokenizer):
    """Make only the B/A packing of one fixture exceed its configured limit."""

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        if (
            add_special_tokens
            and "reverse_a" in text
            and "reverse_b" in text
            and text.index("reverse_b") < text.index("reverse_a")
        ):
            return list(range(6))
        if add_special_tokens:
            return [1, 2, 3]
        return super().encode(text, add_special_tokens=False)


def _empty_blocklist() -> GoldenBlocklist:
    return GoldenBlocklist(frozenset(), frozenset(), frozenset())


def _candidate(
    index: int,
    *,
    split: Literal["train", "validation", "test"] | None = None,
    family: str | None = None,
    label: bool | None = None,
    reference: str | None = None,
    candidate: str | None = None,
    groups: tuple[str, ...] | None = None,
    source_kind: str = "fixture",
    private: bool = False,
) -> corpus.CorpusCandidate:
    return corpus.CorpusCandidate(
        origin_id=f"origin-{index:03d}",
        source_kind=source_kind,
        reference_headless=reference or f"(n : Nat) : n + {index + 1} = n + {index + 1}",
        candidate_headless=candidate or f"(n : Nat) : ({index + 1} + n) = ({index + 1} + n)",
        label=index % 2 == 0 if label is None else label,
        split_group_ids=groups or (f"root-{index:03d}",),
        family_ids=(family or f"family-{index % 10}",),
        provenance_ids=(f"provenance-{index:03d}",),
        split_anchor=split,
        private_source_content=private,
        redistribution_allowed=not private,
        external_transmission_allowed=not private,
        release_eligible=not private,
    )


def _config(tmp_path: Path, output_name: str = "corpus-v1") -> corpus.CorpusV1Config:
    return corpus.CorpusV1Config(
        output_root=tmp_path / output_name,
        tokenizer_dir=tmp_path / "tokenizer",
        tokenizer_files={},
        inputs={},
        enforce_storage_root=False,
        canary_epochs=2,
    )


def _miniature_candidates() -> list[corpus.CorpusCandidate]:
    rows: list[corpus.CorpusCandidate] = []
    for split_index, split in enumerate(corpus.SPLITS):
        for family_index in range(10):
            index = 10 * split_index + family_index
            rows.append(
                _candidate(
                    index,
                    split=split,
                    family=f"family-{family_index}",
                    label=family_index % 2 == 0,
                )
            )
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _materialize_miniature(
    tmp_path: Path, output_name: str = "corpus-v1"
) -> tuple[Path, FakeTokenizer, GoldenBlocklist, dict[str, Any]]:
    tokenizer = FakeTokenizer()
    blocklist = _empty_blocklist()
    config = _config(tmp_path, output_name)
    manifest = corpus.materialize_candidates(
        config,
        _miniature_candidates(),
        blocklist=blocklist,
        tokenizer=tokenizer,
    )
    return config.output_root, tokenizer, blocklist, manifest


def _merged_pair(index: int, family: str) -> corpus.MergedPair:
    return corpus.MergedPair(
        pair_id=f"pair-{index:03d}",
        pair_key=(f"hash-a-{index:03d}", f"hash-b-{index:03d}"),
        reference_headless=f"reference {index}",
        candidate_headless=f"candidate {index}",
        label=index % 2 == 0,
        split_group_ids=(f"root-{index:03d}",),
        family_ids=(family,),
        origin_ids=(f"origin-{index:03d}",),
        source_kinds=("fixture",),
        provenance_ids=(f"provenance-{index:03d}",),
        split_anchors=(),
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=True,
        release_eligible=True,
        forward_tokens=10,
        reverse_tokens=10,
    )


def _component_seed(
    pair_id: str,
    *,
    groups: tuple[str, ...],
    statements: tuple[str, str],
    anchors: tuple[str, ...],
) -> corpus.ComponentSeed:
    return corpus.ComponentSeed(
        pair_id=pair_id,
        pair_key=statements,
        split_group_ids=groups,
        statement_near_hashes=statements,
        split_anchors=anchors,
        origin_ids=(f"origin-{pair_id}",),
        source_kinds=("fixture",),
    )


def test_miniature_end_to_end_materializes_and_verifies(tmp_path: Path) -> None:
    output_root, tokenizer, blocklist, manifest = _materialize_miniature(tmp_path)

    replay = corpus.verify_corpus_v1(
        output_root,
        tokenizer=tokenizer,
        blocklist=blocklist,
    )

    assert replay == manifest
    assert manifest["counts"]["retained_records"] == 30
    assert manifest["counts"]["split"] == {"test": 10, "train": 10, "validation": 10}
    assert manifest["counts"]["family_memberships"] == {f"family-{index}": 3 for index in range(10)}
    assert manifest["counts"]["exclusions"] == {}
    assert {path.name for path in output_root.iterdir()} == {
        "components_v1.jsonl",
        "corpus_v1_manifest.json",
        "exclusions_v1.jsonl",
        "lexical_canary.json",
        "provenance_v1.jsonl",
        "records_test_v1.jsonl",
        "records_train_v1.jsonl",
        "records_validation_v1.jsonl",
        "run_config.json",
    }
    for split in corpus.SPLITS:
        rows = _read_jsonl(output_root / f"records_{split}_v1.jsonl")
        assert len(rows) == 10
        assert all(set(row) == corpus.TRAINER_FIELDS for row in rows)
    canary = json.loads((output_root / "lexical_canary.json").read_text(encoding="utf-8"))
    assert canary["method_version"] == "modernbert_token_bow_logistic_canary_v1"
    assert canary["diagnostic_splits"] == ["validation", "test"]

    deterministic_replay = corpus.materialize_candidates(
        _config(tmp_path),
        list(reversed(_miniature_candidates())),
        blocklist=blocklist,
        tokenizer=tokenizer,
    )
    assert deterministic_replay == manifest
    assert "created_utc" not in manifest


def test_screening_applies_blocklist_degenerate_and_both_token_orientations() -> None:
    protected = "(n : Nat) : n = n"
    blocklist = GoldenBlocklist(
        frozenset({signature_near_dup_hash(protected)}),
        frozenset({"blocked-root"}),
        frozenset({"blocked-root"}),
    )
    rows = [
        _candidate(0, reference=protected),
        _candidate(1, groups=("blocked-root",)),
        _candidate(
            2,
            reference="(n : Nat) : n + 0 = n",
            candidate="  (n : Nat) : n + 0   = n ",
        ),
        _candidate(3, reference="reverse_a", candidate="reverse_b"),
        _candidate(4, reference="ordinary_a", candidate="ordinary_b"),
    ]

    screened, exclusions = corpus.screen_candidates(
        rows,
        blocklist=blocklist,
        tokenizer=ReverseOverlengthTokenizer(),
        max_tokens=5,
    )

    assert [row.candidate.origin_id for row in screened] == ["origin-004"]
    assert Counter(row["reason"] for row in exclusions) == {
        "golden_blocklist": 2,
        "degenerate_near_identical_sides": 1,
        "overlength": 1,
    }
    overlength = next(row for row in exclusions if row["reason"] == "overlength")
    assert overlength["forward_tokens"] == 3
    assert overlength["reverse_tokens"] == 6


def test_unordered_dedup_merges_provenance_and_quarantines_any_label_conflict() -> None:
    left = _candidate(
        0,
        reference="left statement",
        candidate="right statement",
        groups=("root-a",),
        source_kind="v0_mixed_proxy",
    )
    reverse_private = _candidate(
        1,
        reference="right statement",
        candidate="left statement",
        groups=("root-b",),
        source_kind="recovered_codex_judged_v1",
        label=left.label,
        private=True,
    )
    screened, exclusions = corpus.screen_candidates(
        [reverse_private, left],
        blocklist=_empty_blocklist(),
        tokenizer=FakeTokenizer(),
    )
    merged, component_seeds, conflicts = corpus.deduplicate_pairs(screened)

    assert exclusions == []
    assert conflicts == []
    assert len(merged) == 1
    assert len(component_seeds) == 1
    row = merged[0]
    assert row.origin_ids == ("origin-000", "origin-001")
    assert row.split_group_ids == ("root-a", "root-b")
    assert row.source_kinds == ("recovered_codex_judged_v1", "v0_mixed_proxy")
    assert row.private_source_content is True
    assert row.redistribution_allowed is False
    assert row.external_transmission_allowed is False
    assert row.release_eligible is False

    conflicting = replace(reverse_private, label=not left.label)
    conflict_screened, _ = corpus.screen_candidates(
        [left, conflicting],
        blocklist=_empty_blocklist(),
        tokenizer=FakeTokenizer(),
    )
    retained, conflict_seeds, conflict_exclusions = corpus.deduplicate_pairs(conflict_screened)
    assert retained == []
    assert len(conflict_seeds) == 1
    assert [item["reason"] for item in conflict_exclusions] == ["conflicting_labels"]
    assert conflict_exclusions[0]["observed_labels"] == [False, True]


def test_direct_frozen_split_bridge_is_text_free_quarantine_before_lineage_union() -> None:
    train = _candidate(
        0,
        split="train",
        reference="left statement",
        candidate="right statement",
        groups=("train-root",),
        source_kind="v0_mixed_proxy",
    )
    test = _candidate(
        1,
        split="test",
        label=train.label,
        reference="right statement",
        candidate="left statement",
        groups=("test-root",),
        source_kind="v0_mixed_proxy",
    )
    screened, _ = corpus.screen_candidates(
        [train, test],
        blocklist=_empty_blocklist(),
        tokenizer=FakeTokenizer(),
    )
    merged, component_seeds, _ = corpus.deduplicate_pairs(screened)

    retained, retained_seeds, exclusions = corpus.quarantine_split_anchor_conflicts(
        merged, component_seeds
    )

    assert retained == []
    assert retained_seeds == {}
    assert len(component_seeds) == 1
    assert len(exclusions) == 1
    exclusion = exclusions[0]
    assert exclusion["reason"] == "split_anchor_component_conflict"
    assert exclusion["conflict_component_split_anchors"] == ["test", "train"]
    assert exclusion["conflict_component_group_ids"] == ["test-root", "train-root"]
    assert "reference_headless" not in exclusion
    assert "candidate_headless" not in exclusion


def test_components_union_pre_cap_groups_and_fail_on_crossed_v0_anchors() -> None:
    seeds = {
        "pair-a": _component_seed(
            "pair-a",
            groups=("root-a", "root-b"),
            statements=("1" * 64, "2" * 64),
            anchors=("train",),
        ),
        "pair-b": _component_seed(
            "pair-b",
            groups=("root-b", "root-c"),
            statements=("3" * 64, "4" * 64),
            anchors=("train",),
        ),
        "pair-c": _component_seed(
            "pair-c",
            groups=("root-d",),
            statements=("5" * 64, "6" * 64),
            anchors=("validation",),
        ),
    }

    item_components, components = corpus.build_components(seeds, seed=20260828)

    assert item_components["pair-a"] == item_components["pair-b"]
    assert item_components["pair-a"] != item_components["pair-c"]
    joined = next(item for item in components if item.component_id == item_components["pair-a"])
    assert joined.split_group_ids == ("root-a", "root-b", "root-c")
    assert joined.split == "train"
    assert joined.split_anchors == ("train",)

    with pytest.raises(corpus.CorpusV1Error, match="crosses frozen v0 splits"):
        corpus.build_components(
            {
                **seeds,
                "pair-d": _component_seed(
                    "pair-d",
                    groups=("root-c", "root-d"),
                    statements=("7" * 64, "8" * 64),
                    anchors=(),
                ),
            },
            seed=20260828,
        )


def test_components_union_distinct_pairs_that_share_either_statement_identity() -> None:
    shared = "2" * 64
    seeds = {
        "pair-a": _component_seed(
            "pair-a",
            groups=("root-a",),
            statements=("1" * 64, shared),
            anchors=("train",),
        ),
        "pair-b": _component_seed(
            "pair-b",
            groups=("root-b",),
            statements=(shared, "3" * 64),
            anchors=("train",),
        ),
    }

    item_components, [component] = corpus.build_components(seeds, seed=20260828)

    assert item_components["pair-a"] == item_components["pair-b"]
    assert component.split_group_ids == ("root-a", "root-b")
    assert component.statement_near_hashes == ("1" * 64, shared, "3" * 64)


def test_indirect_shared_statement_anchor_conflict_drops_the_whole_component() -> None:
    first_shared = "2" * 64
    second_shared = "3" * 64
    seeds = {
        "pair-train": _component_seed(
            "pair-train",
            groups=("root-train",),
            statements=("1" * 64, first_shared),
            anchors=("train",),
        ),
        "pair-bridge": _component_seed(
            "pair-bridge",
            groups=("root-bridge",),
            statements=(first_shared, second_shared),
            anchors=(),
        ),
        "pair-test": _component_seed(
            "pair-test",
            groups=("root-test",),
            statements=(second_shared, "4" * 64),
            anchors=("test",),
        ),
    }
    rows = [
        replace(_merged_pair(index, "fixture"), pair_id=pair_id)
        for index, pair_id in enumerate(seeds)
    ]

    retained, retained_seeds, exclusions = corpus.quarantine_split_anchor_conflicts(rows, seeds)

    assert retained == []
    assert retained_seeds == {}
    assert len(exclusions) == 3
    assert {row["pair_id"] for row in exclusions} == set(seeds)
    assert {row["reason"] for row in exclusions} == {"split_anchor_component_conflict"}
    assert len({row["conflict_component_id"] for row in exclusions}) == 1
    assert all(
        row["conflict_component_pair_ids"] == ["pair-bridge", "pair-test", "pair-train"]
        for row in exclusions
    )


def test_family_cap_is_fixed_point_deterministic_and_treats_sequences_as_memberships() -> None:
    rows = [
        *(_merged_pair(index, "a->b->c") for index in range(20)),
        *(_merged_pair(index, f"other-{index}") for index in range(20, 100)),
    ]

    retained, exclusions = corpus.apply_family_cap(rows, seed=20260828)
    replay, replay_exclusions = corpus.apply_family_cap(list(reversed(rows)), seed=20260828)

    assert [row.pair_id for row in retained] == [row.pair_id for row in replay]
    assert exclusions == replay_exclusions
    assert len(retained) == 88
    counts = Counter(family for row in retained for family in row.family_ids)
    assert counts["a->b->c"] == 8
    assert "a" not in counts and "b" not in counts and "c" not in counts
    assert all(10 * count <= len(retained) for count in counts.values())
    assert len(exclusions) == 12
    assert {row["trigger_family"] for row in exclusions} == {"a->b->c"}
    assert {row["keep_limit"] for row in exclusions} == {8}


def test_verifier_fails_closed_after_output_tampering(tmp_path: Path) -> None:
    output_root, tokenizer, blocklist, _ = _materialize_miniature(tmp_path, "tamper")
    train_path = output_root / "records_train_v1.jsonl"
    train_path.write_text(train_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(corpus.CorpusV1Error, match="output binding differs"):
        corpus.verify_corpus_v1(
            output_root,
            tokenizer=tokenizer,
            blocklist=blocklist,
        )


def test_verifier_replays_unanchored_component_split_after_hash_refresh(
    tmp_path: Path,
) -> None:
    output_root, tokenizer, blocklist, _ = _materialize_miniature(tmp_path, "split-tamper")
    components_path = output_root / "components_v1.jsonl"
    components = _read_jsonl(components_path)
    tampered = next(
        row
        for row in components
        if corpus._unanchored_component_split(row["component_id"], 20260828) != row["split"]
    )
    tampered["split_anchors"] = []
    components_path.write_bytes(b"".join(corpus._canonical_line(row) for row in components))
    manifest_path = output_root / "corpus_v1_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"]["components_v1.jsonl"]["sha256"] = hash_file(components_path)
    manifest_path.write_bytes(corpus._canonical_line(manifest))

    with pytest.raises(corpus.CorpusV1Error, match="deterministic assignment"):
        corpus.verify_corpus_v1(
            output_root,
            tokenizer=tokenizer,
            blocklist=blocklist,
        )


def test_production_config_freezes_queue_artifacts_without_importing_user_module() -> None:
    config = corpus.production_config(Path("/storage/milikic/leanfaith/corpus2/corpus_v1"))

    assert set(config.inputs) == corpus._PRODUCTION_INPUT_NAMES
    assert config.inputs["recovered_manifest"].sha256 == (
        "19a9d814823245f300c9c386514c9f4281322b0939d51a23ab13228df9cc0d1b"
    )
    assert config.inputs["recovered_judgments"].sha256 == (
        "2a6ef8c170a20e38047b3fbe6d1b842fb51abb0d0049552aa3f4bfac57b06025"
    )
    assert config.inputs["recovered_trainer"].sha256 == (
        "5de1f904904da6fa204a446e65c58d137a59a6a21d5afa15eb1ad24dbf3bf2f1"
    )
    implementation = Path(corpus.__file__).read_text(encoding="utf-8")
    assert "import leanfaith.corpus2.from_mixed_v0" not in implementation
    assert "from leanfaith.corpus2.from_mixed_v0 import" not in implementation

    with pytest.raises(ValueError, match="exactly the frozen production input set"):
        corpus.CorpusV1Config(
            output_root=config.output_root,
            tokenizer_dir=config.tokenizer_dir,
            tokenizer_files=config.tokenizer_files,
            inputs={
                **config.inputs,
                "final_test": corpus.FrozenFile(
                    path=Path("/storage/milikic/forbidden-final-test.jsonl"),
                    sha256="f" * 64,
                ),
            },
        )


def test_depth_loader_reads_private_theorem_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = {
        name: tmp_path / name
        for name in (
            "depth-manifest",
            "depth-pairs",
            "depth-representations",
            "depth-theorems",
            "public-representations",
            "public-theorems",
            "private-representations",
            "private-theorems",
        )
    }
    inputs = {
        name.replace("-", "_"): corpus.FrozenFile(path=path, sha256="a" * 64)
        for name, path in paths.items()
    }
    config = corpus.CorpusV1Config(
        output_root=tmp_path / "output",
        tokenizer_dir=tmp_path / "tokenizer",
        tokenizer_files={},
        inputs=inputs,
        enforce_storage_root=False,
    )
    context_id = "ctx:" + "1" * 64
    roots = ("anc:" + "2" * 64,)
    pair = SimpleNamespace(
        pair_id="depth-pair",
        original_source_representation_id="source-representation",
        original_source_theorem_id="source-theorem",
        selected_final_representation_id="final-representation",
        selected_final_theorem_id="final-theorem",
        context_id=context_id,
        root_ancestry_ids=roots,
        original_source_statement_content_hash="3" * 64,
        original_source_alpha_identity_fingerprint="5" * 64,
        final_alpha_identity_fingerprint="6" * 64,
        semantic_negative_hop_count=0,
        preserved_intention="equivalent_candidate",
        depth_three_sequences=("p20->p21->p20",),
        chain_ids=("chain-a", "chain-b", "chain-c"),
    )
    source_representation = SimpleNamespace(
        representation_id="source-representation",
        theorem_id="source-theorem",
        context_id=context_id,
        headless="source headless",
        alpha_identity_fingerprint="5" * 64,
    )
    final_representation = SimpleNamespace(
        representation_id="final-representation",
        theorem_id="final-theorem",
        context_id=context_id,
        headless="final headless",
        alpha_identity_fingerprint="6" * 64,
    )
    source_theorem = SimpleNamespace(
        theorem_id="source-theorem",
        context_id=context_id,
        root_ancestry_ids=roots,
        statement_content_hash="3" * 64,
    )
    final_theorem = SimpleNamespace(
        theorem_id="final-theorem",
        context_id=context_id,
        root_ancestry_ids=roots,
        statement_content_hash="4" * 64,
    )

    monkeypatch.setattr(
        corpus,
        "_read_json",
        lambda _path: {
            "method_version": "deterministic_v2_composition_third_hop_v2",
            "unique_pair_count": 1,
            "unique_output_sha256": "a" * 64,
            "representation_output_sha256": "a" * 64,
            "theorem_output_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(corpus, "_iter_jsonl", lambda _path: iter(((1, {}),)))
    monkeypatch.setattr(
        corpus,
        "DeterministicCompositionThirdHopPairRecord",
        SimpleNamespace(model_validate=lambda _payload: pair),
    )

    def fake_representations(
        path: Path, _needed: set[str], *, envelope_key: str | None = None
    ) -> dict[str, Any]:
        assert envelope_key is None
        if path == paths["depth-representations"]:
            return {"final-representation": final_representation}
        if path == paths["private-representations"]:
            return {"source-representation": source_representation}
        return {}

    def fake_theorems(
        path: Path, _needed: set[str], *, envelope_key: str | None = None
    ) -> dict[str, Any]:
        if path == paths["depth-theorems"]:
            assert envelope_key is None
            return {"final-theorem": final_theorem}
        if path == paths["private-theorems"]:
            assert envelope_key == "theorem"
            return {"source-theorem": source_theorem}
        assert path == paths["public-theorems"]
        assert envelope_key == "theorem"
        return {}

    monkeypatch.setattr(corpus, "_load_representations", fake_representations)
    monkeypatch.setattr(corpus, "_load_theorems", fake_theorems)

    [loaded] = corpus._load_depth_candidates(config)
    assert loaded.origin_id == "depth-pair"
    assert loaded.private_source_content is True
    assert loaded.external_transmission_allowed is False

    source_representation.alpha_identity_fingerprint = "7" * 64
    with pytest.raises(corpus.CorpusV1Error, match="lineage join differs"):
        corpus._load_depth_candidates(config)


def test_d3_job_check_and_public_representation_are_exactly_bound() -> None:
    candidate = "(n : Nat) : n + 0 = n"
    source = "(n : Nat) : n = n"
    job: dict[str, Any] = {
        "index": 7,
        "job_id": "d3-job-7",
        "source_statement": source,
        "statement_hash": "a" * 64,
        "statement_id": "repr:source",
        "theorem_id": "thm:source",
        "group_key": "anc:source",
        "assigned_family": "P20",
        "direction": "preserve",
        "prompt_sha256": "b" * 64,
        "provider": "codex",
    }
    record = {
        **{
            field: job[field]
            for field in (
                "index",
                "source_statement",
                "statement_hash",
                "statement_id",
                "assigned_family",
                "direction",
                "prompt_sha256",
                "provider",
            )
        },
        "transformation": "P20",
        "rewritten_statement": candidate,
    }
    check = {
        "job_id": "d3-job-7",
        "candidate_sha256": hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        "candidate_near_dup_hash": signature_near_dup_hash(candidate),
    }

    assert corpus._validate_d3_job_join(index=7, job=job, record=record, check=check) == candidate
    with pytest.raises(corpus.CorpusV1Error, match="identity differs"):
        corpus._validate_d3_job_join(
            index=7,
            job=job,
            record=record,
            check={**check, "candidate_sha256": "f" * 64},
        )

    representation: Any = SimpleNamespace(
        representation_id="repr:source",
        theorem_id="thm:source",
        context_id="ctx:source",
        headless=source,
        content_hash="a" * 64,
    )
    theorem: Any = SimpleNamespace(
        theorem_id="thm:source",
        context_id="ctx:source",
        root_ancestry_ids=("anc:source",),
    )
    corpus._validate_d3_source_join(
        index=7,
        job=job,
        representation=representation,
        theorem=theorem,
    )
    representation.content_hash = "c" * 64
    with pytest.raises(corpus.CorpusV1Error, match="source representation/theorem differs"):
        corpus._validate_d3_source_join(
            index=7,
            job=job,
            representation=representation,
            theorem=theorem,
        )
