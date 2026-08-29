"""Unit tests for the Track D-3 LLM transform pilot harness (no live LLM calls)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from leanfaith.corpus2.llm_transforms import (
    DIRECTIONS,
    FewShot,
    ProviderCall,
    SourceStatement,
    build_fewshots,
    build_prompt,
    direction_for_index,
    expected_label,
    load_golden_train_pairs,
    make_record,
    parse_llm_output,
    provider_stats,
    sample_source_statements,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pair(
    pair_id: str,
    partition: str,
    label: bool,
    *,
    conflict: bool = False,
    provenance: str = "expert_human",
) -> dict[str, object]:
    return {
        "pair_id": pair_id,
        "group_key": f"group::{pair_id}",
        "partition": partition,
        "label": label,
        "label_conflict": conflict,
        "label_provenance": provenance,
        "reference_headless": f"(n : ℕ) : {pair_id} n = n",
        "candidate_headless": f"(m : ℕ) : {pair_id} m = m",
    }


def _write_pairs(path: Path, rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def golden_bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    rows: list[dict[str, object]] = []
    # 5 eligible positives + 5 eligible negatives in golden_train.
    for i in range(5):
        rows.append(_pair(f"train_pos_{i}", "golden_train", True))
        rows.append(_pair(f"train_neg_{i}", "golden_train", False))
    # Ineligible golden_train rows: conflict / non-expert provenance.
    rows.append(_pair("train_conflict", "golden_train", True, conflict=True))
    rows.append(_pair("train_auto", "golden_train", False, provenance="auto_typecheck_fail"))
    pairs = _write_pairs(tmp_path / "golden_train.jsonl", rows)
    split_sha = hashlib.sha256(pairs.read_bytes()).hexdigest()
    parent_sha = "a" * 64
    parent = tmp_path / "partition.json"
    parent.write_text(
        json.dumps(
            {
                "version": "golden_partition_v1",
                "canonical_pairs_sha256": parent_sha,
                "counts": {"golden_train": {"canonical_pairs": len(rows)}},
                "group_partitions": {str(row["group_key"]): "golden_train" for row in rows},
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / "golden_train.manifest.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_canonical_sha256": parent_sha,
                "split_sha256": split_sha,
                "partition": "golden_train",
                "row_count": len(rows),
                "group_count": len(rows),
            }
        ),
        encoding="utf-8",
    )
    return pairs, sidecar, parent


def _fewshots() -> list[FewShot]:
    return [
        FewShot("p1", "(n : ℕ) : n = n", "(m : ℕ) : m = m", "consistent"),
        FewShot("n1", "(n : ℕ) : n = n", "(n : ℕ) : n = n + 1", "inconsistent"),
    ]


# ---------------------------------------------------------------------------
# Gold rule + few-shot construction
# ---------------------------------------------------------------------------


class TestGoldRule:
    def test_loader_keeps_only_golden_train(self, golden_bundle: tuple[Path, Path, Path]) -> None:
        pairs, sidecar, parent = golden_bundle
        pool = load_golden_train_pairs(pairs, sidecar, parent)
        assert pool, "expected some golden_train pairs"
        assert all(row["partition"] == "golden_train" for row in pool)
        assert all(row["partition"] == "golden_train" for row in pool)


class TestBuildFewshots:
    def test_counts_and_verdicts(self, golden_bundle: tuple[Path, Path, Path]) -> None:
        pairs, sidecar, parent = golden_bundle
        shots = build_fewshots(pairs, 13, 3, 3, parent, sidecar)
        assert len(shots) == 6
        assert sum(1 for s in shots if s.verdict == "consistent") == 3
        assert sum(1 for s in shots if s.verdict == "inconsistent") == 3

    def test_filters_conflict_and_provenance(self, golden_bundle: tuple[Path, Path, Path]) -> None:
        # Sample everything eligible: the ineligible rows must still be absent.
        pairs, sidecar, parent = golden_bundle
        shots = build_fewshots(pairs, 1, 5, 5, parent, sidecar)
        ids = {s.pair_id for s in shots}
        assert "train_conflict" not in ids
        assert "train_auto" not in ids

    def test_deterministic_for_seed(self, golden_bundle: tuple[Path, Path, Path]) -> None:
        pairs, sidecar, parent = golden_bundle
        first = build_fewshots(
            pairs, 42, partition_manifest_path=parent, split_manifest_path=sidecar
        )
        second = build_fewshots(
            pairs, 42, partition_manifest_path=parent, split_manifest_path=sidecar
        )
        assert [s.pair_id for s in first] == [s.pair_id for s in second]
        other = build_fewshots(
            pairs, 43, partition_manifest_path=parent, split_manifest_path=sidecar
        )
        assert [s.pair_id for s in first] != [s.pair_id for s in other]

    def test_render_format(self) -> None:
        shot = FewShot("x", "(n : ℕ) : n = n", "(m : ℕ) : m = m", "consistent")
        rendered = shot.render()
        assert rendered.splitlines() == [
            "ORIGINAL: (n : ℕ) : n = n",
            "REWRITTEN: (m : ℕ) : m = m",
            "VERDICT: consistent",
        ]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    SOURCE = "(a b : ℝ) (h : a < b) : a ≤ b"

    def test_preserve_prompt(self) -> None:
        prompt = build_prompt(self.SOURCE, "preserve", _fewshots())
        assert self.SOURCE in prompt
        assert "PROVABLY EQUIVALENT" in prompt
        assert "consistency-preserving menu" in prompt
        assert "contrapositive" in prompt
        assert "definitional fold/unfold" in prompt
        assert 'intended_label MUST be "consistent"' in prompt
        assert '"rewritten_statement": str' in prompt
        assert '"confidence": float 0-1' in prompt

    def test_break_prompt(self) -> None:
        prompt = build_prompt(self.SOURCE, "break", _fewshots())
        assert "SUBTLY DIFFERENT" in prompt
        assert "consistency-breaking menu" in prompt
        assert "flip an inequality direction" in prompt
        assert "swap quantifier scopes" in prompt
        assert 'intended_label MUST be "inconsistent"' in prompt

    def test_fewshots_rendered_as_calibration(self) -> None:
        prompt = build_prompt(self.SOURCE, "preserve", _fewshots())
        assert "ORIGINAL: (n : ℕ) : n = n" in prompt
        assert "VERDICT: inconsistent" in prompt
        assert "NOT demonstrations of" in prompt

    def test_well_typed_requirement(self) -> None:
        prompt = build_prompt(self.SOURCE, "break", _fewshots())
        assert "well-typed" in prompt
        assert "import Mathlib" in prompt

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            build_prompt(self.SOURCE, "mutate", _fewshots())


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _valid_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "rewritten_statement": "(a b : ℝ) (h : a < b) : ¬b ≤ a",
        "intended_label": "consistent",
        "transformation": "contrapositive",
        "reasoning": "lt iff not ge",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class TestParseLlmOutput:
    def test_plain_last_line(self) -> None:
        parsed, err = parse_llm_output("Some reasoning here.\n" + _valid_payload())
        assert err is None
        assert parsed is not None
        assert parsed["transformation"] == "contrapositive"
        assert parsed["confidence"] == 0.9

    def test_json_fence(self) -> None:
        stdout = "Answer:\n```json\n" + _valid_payload() + "\n```\n"
        parsed, err = parse_llm_output(stdout)
        assert err is None
        assert parsed is not None

    def test_trailing_prose_after_json(self) -> None:
        stdout = _valid_payload() + "\ntokens used\n9,256\n"
        parsed, err = parse_llm_output(stdout)
        assert err is None
        assert parsed is not None

    def test_picks_schema_object_not_last_brace(self) -> None:
        stdout = _valid_payload() + '\n{"tokens": 123}\n'
        parsed, err = parse_llm_output(stdout)
        assert err is None
        assert parsed is not None
        assert "rewritten_statement" in parsed

    def test_no_json(self) -> None:
        parsed, err = parse_llm_output("I could not comply with this request.")
        assert parsed is None
        assert err is not None
        assert "no JSON" in err

    def test_missing_keys(self) -> None:
        parsed, err = parse_llm_output('{"rewritten_statement": "x", "confidence": 1}')
        assert parsed is None
        assert err is not None
        assert "missing required keys" in err

    def test_invalid_label(self) -> None:
        parsed, err = parse_llm_output(_valid_payload(intended_label="equivalent"))
        assert parsed is None
        assert err is not None
        assert "intended_label" in err

    def test_confidence_out_of_range(self) -> None:
        parsed, err = parse_llm_output(_valid_payload(confidence=1.5))
        assert parsed is None
        assert err is not None
        assert "confidence" in err

    def test_integer_confidence_coerced(self) -> None:
        parsed, err = parse_llm_output(_valid_payload(confidence=1))
        assert err is None
        assert parsed is not None
        assert parsed["confidence"] == 1.0
        assert isinstance(parsed["confidence"], float)

    def test_empty_rewrite_rejected(self) -> None:
        parsed, err = parse_llm_output(_valid_payload(rewritten_statement="  "))
        assert parsed is None
        assert err is not None


# ---------------------------------------------------------------------------
# Source sampling + directions
# ---------------------------------------------------------------------------


def _write_reprs(path: Path, n: int) -> Path:
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            headless = f"(x : ℕ) (h : x > {i}) : statement_{i:04d} x " + "= x " * 10
            fh.write(
                json.dumps(
                    {
                        "representation_id": f"repr:{i:04d}",
                        "content_hash": f"hash{i:04d}",
                        "headless": headless,
                    }
                )
                + "\n"
            )
        # Out-of-range rows: too short and too long.
        fh.write(
            json.dumps(
                {"representation_id": "repr:short", "content_hash": "s", "headless": "n : ℕ"}
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "representation_id": "repr:long",
                    "content_hash": "l",
                    "headless": "x" * 700,
                }
            )
            + "\n"
        )
    return path


class TestSampling:
    def test_disjoint_per_provider_and_deterministic(self, tmp_path: Path) -> None:
        reprs = _write_reprs(tmp_path / "reprs.jsonl", 40)
        first = sample_source_statements(reprs, seed=5, n_per_provider=10)
        second = sample_source_statements(reprs, seed=5, n_per_provider=10)
        assert {p: [s.statement_id for s in v] for p, v in first.items()} == {
            p: [s.statement_id for s in v] for p, v in second.items()
        }
        all_ids = [s.statement_id for stmts in first.values() for s in stmts]
        assert len(all_ids) == 30
        assert len(set(all_ids)) == 30, "providers must get different statements"
        assert "repr:short" not in all_ids
        assert "repr:long" not in all_ids

    def test_insufficient_pool_raises(self, tmp_path: Path) -> None:
        reprs = _write_reprs(tmp_path / "reprs.jsonl", 5)
        with pytest.raises(ValueError, match="need 30"):
            sample_source_statements(reprs, seed=0, n_per_provider=10)

    def test_direction_alternation(self) -> None:
        directions = [direction_for_index(i) for i in range(6)]
        assert directions == ["preserve", "break", "preserve", "break", "preserve", "break"]
        assert set(directions) == set(DIRECTIONS)

    def test_expected_label(self) -> None:
        assert expected_label("preserve") == "consistent"
        assert expected_label("break") == "inconsistent"


# ---------------------------------------------------------------------------
# Record assembly + stats (offline, synthetic ProviderCall objects)
# ---------------------------------------------------------------------------


def _statement() -> SourceStatement:
    return SourceStatement("repr:0001", "hash0001", "(a b : ℝ) (h : a < b) : a ≤ b")


class TestRecords:
    def test_parse_ok_record(self, tmp_path: Path) -> None:
        call = ProviderCall(returncode=0, stdout="reasoning\n" + _valid_payload(), stderr="")
        record = make_record(
            "codex", 0, _statement(), "preserve", "PROMPT", call, tmp_path / "raw.txt"
        )
        assert record.parse_ok
        assert record.parse_error is None
        assert record.intended_label == "consistent"
        assert record.label_matches_direction is True
        assert record.statement_id == "repr:0001"

    def test_label_direction_mismatch_flagged(self, tmp_path: Path) -> None:
        call = ProviderCall(returncode=0, stdout=_valid_payload(), stderr="")
        record = make_record(
            "codex", 1, _statement(), "break", "PROMPT", call, tmp_path / "raw.txt"
        )
        assert record.parse_ok
        assert record.label_matches_direction is False

    def test_timeout_record(self, tmp_path: Path) -> None:
        call = ProviderCall(returncode=None, stdout="", stderr="", timed_out=True)
        record = make_record(
            "lemex", 2, _statement(), "preserve", "PROMPT", call, tmp_path / "raw.txt"
        )
        assert not record.parse_ok
        assert record.parse_error is not None
        assert "timed out" in record.parse_error

    def test_provider_stats(self, tmp_path: Path) -> None:
        ok_call = ProviderCall(returncode=0, stdout=_valid_payload(), stderr="")
        bad_call = ProviderCall(returncode=0, stdout="no json here", stderr="")
        records = [
            make_record("codex", 0, _statement(), "preserve", "P", ok_call, tmp_path / "a"),
            make_record("codex", 1, _statement(), "break", "P", ok_call, tmp_path / "b"),
            make_record("codex", 2, _statement(), "preserve", "P", bad_call, tmp_path / "c"),
        ]
        stats = provider_stats(records)
        assert stats["n"] == 3
        assert stats["parse_ok"] == 2
        assert stats["timeouts"] == 0
        assert stats["intended_label_distribution"] == {"consistent": 2}
        assert stats["label_matches_direction"] == 1
        assert stats["mean_confidence"] == 0.9
        assert isinstance(stats["mean_rewritten_length_delta"], float)
