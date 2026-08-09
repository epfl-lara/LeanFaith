"""Public synthetic replay regression for stateless sft_classic extraction.

The scale extractor processes independent, self-contained dataset snippets in
one bounded Lean process.  This fixture interleaves a real syntax failure with
valid declarations that share long prefixes and checks that neither the
failure nor an earlier command changes later terminal outcomes on replay.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanfaith.cli.pipeline import _extract_sft_chunk
from leanfaith.config.paths import find_repo_root

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"


def _row(uuid: str, statement: str, *, lean_code: str | None = None) -> dict[str, object]:
    return {
        "uuid": uuid,
        "data_source": "leanfaith/public-synthetic-replay",
        "question": (
            f"```lean4\n/-- Public synthetic extraction replay fixture. -/\n{statement}\n```"
        ),
        # A definition deliberately makes the fallback route unavailable.
        "lean_code": lean_code or "def no_theorem_fallback : Nat := 0",
        "valid": False,
        "proof_repair": False,
    }


_ROWS = [
    _row("synthetic-valid-0", "theorem repeated_prefix_0 (n : Nat) : n = n := by sorry"),
    _row(
        "synthetic-invalid",
        "theorem repeated_prefix_invalid (n : Nat) : n =",
        lean_code="theorem repeated_prefix_invalid (n : Nat) : n = := by sorry",
    ),
    _row("synthetic-valid-1", "theorem repeated_prefix_1 (n : Nat) : n + 0 = n := by sorry"),
    _row(
        "synthetic-valid-2",
        "theorem repeated_prefix_2 (n : Nat) : Nat.succ n = n + 1 := by sorry",
    ),
]


def _semantic_projection(out_dir: Path) -> tuple[tuple[object, ...], ...]:
    projected: list[tuple[object, ...]] = []
    theorem_path = out_dir / "theorems" / "sft_classic.jsonl"
    if theorem_path.exists():
        for line in theorem_path.read_text(encoding="utf-8").splitlines():
            theorem = json.loads(line)["theorem"]
            projected.append(
                (
                    "accepted",
                    theorem["source_record_id"],
                    theorem["declaration_name"],
                    theorem["extraction_route"],
                    theorem["statement_content_hash"],
                )
            )
    failure_path = out_dir / "failures" / "sft_classic.jsonl"
    if failure_path.exists():
        for line in failure_path.read_text(encoding="utf-8").splitlines():
            failure = json.loads(line)
            if failure["outcome_level"] == "row":
                projected.append(
                    (
                        "failed",
                        failure["source_record"],
                        failure["code"],
                    )
                )
    return tuple(sorted(projected))


def test_public_synthetic_chunk_replays_without_cross_row_state(tmp_path: Path) -> None:
    outputs: list[Path] = []
    stats = []
    for replay in range(2):
        out_dir = tmp_path / f"out-{replay}"
        outputs.append(out_dir)
        stats.append(
            _extract_sft_chunk(
                project_dir=_FIXTURES,
                context_fingerprint=_CTX_FP,
                context_id=_CTX,
                raw_response_dir=tmp_path / f"raw-{replay}",
                rows=_ROWS,
                source_row_indices=list(range(len(_ROWS))),
                split="synthetic",
                row_offset=0,
                out_dir=out_dir,
                memory_hard_limit_mb=None,
                job_hash="a" * 64,
            )
        )

    assert [item.sources_processed for item in stats] == [4, 4]
    assert [item.accepted for item in stats] == [3, 3]
    assert [item.row_outcomes["source_non_elaboration"] for item in stats] == [1, 1]
    assert _semantic_projection(outputs[0]) == _semantic_projection(outputs[1])

    isolated_outputs: list[Path] = []
    for index, row in enumerate(_ROWS):
        out_dir = tmp_path / f"isolated-{index}"
        isolated_outputs.append(out_dir)
        _extract_sft_chunk(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            context_id=_CTX,
            raw_response_dir=tmp_path / f"raw-isolated-{index}",
            rows=[row],
            source_row_indices=[index],
            split="synthetic",
            row_offset=0,
            out_dir=out_dir,
            memory_hard_limit_mb=None,
            job_hash=f"{index + 1:064x}",
        )
    isolated_projection = tuple(
        sorted(record for out_dir in isolated_outputs for record in _semantic_projection(out_dir))
    )
    assert _semantic_projection(outputs[0]) == isolated_projection
