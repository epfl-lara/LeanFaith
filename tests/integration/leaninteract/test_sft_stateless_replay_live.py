"""Public synthetic replay regression for nonce-isolated SFT extraction.

The scale extractor processes independent, self-contained snippets in one
bounded Lean process.  This fixture exercises the real Mathlib/Aesop import
header, BigOperators, and ``ℝ`` around malformed and fallback routes.  It
checks that request-specific trie namespaces prevent either failure or prior
commands from changing later outcomes, while keeping the import cache shared.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanfaith.cli.pipeline import _extract_sft_chunk, default_mathlib_checkout

_PROJECT = default_mathlib_checkout()

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
    pytest.mark.skipif(
        not (_PROJECT / "lean-toolchain").is_file(),
        reason="pinned mathlib checkout unavailable",
    ),
]

_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_HEADER = "import Mathlib\nimport Aesop\nopen scoped BigOperators\n"


def _row(uuid: str, statement: str, *, lean_code: str | None = None) -> dict[str, object]:
    return {
        "uuid": uuid,
        "data_source": "leanfaith/public-synthetic-replay",
        "question": f"```lean4\n-- Public synthetic extraction replay fixture.\n{statement}\n```",
        # A definition deliberately makes the fallback route unavailable.
        "lean_code": lean_code or "def no_theorem_fallback : Nat := 0",
        "valid": False,
        "proof_repair": False,
    }


_ROWS = [
    _row(
        "synthetic-valid-0",
        _HEADER
        + "theorem repeated_prefix_0 (x : ℝ) : "
        + "(∑ i ∈ Finset.range 1, x) = x := by sorry",
    ),
    _row(
        "synthetic-invalid",
        _HEADER + "theorem repeated_prefix_invalid (x : ℝ) : x =",
        lean_code=_HEADER + "theorem repeated_prefix_invalid (x : ℝ) : x = := by sorry",
    ),
    _row(
        "synthetic-valid-fallback",
        _HEADER + "theorem repeated_prefix_fallback (x : ℝ) : x =",
        lean_code=(_HEADER + "theorem repeated_prefix_fallback (x : ℝ) : x = x := by rfl"),
    ),
    _row(
        "synthetic-valid-after-failure",
        _HEADER + "theorem repeated_prefix_after (x : ℝ) : x + 0 = x := by sorry",
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


def test_public_mathlib_chunk_replays_without_cross_request_state(tmp_path: Path) -> None:
    outputs: list[Path] = []
    stats = []
    for replay in range(2):
        out_dir = tmp_path / f"out-{replay}"
        outputs.append(out_dir)
        stats.append(
            _extract_sft_chunk(
                project_dir=_PROJECT,
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
    theorems = [
        json.loads(line)["theorem"]
        for line in (outputs[0] / "theorems" / "sft_classic.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    fallback = next(
        theorem for theorem in theorems if theorem["declaration_name"] == "repeated_prefix_fallback"
    )
    assert fallback["extraction_route"] == "lean_code_fallback"

    raw_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((tmp_path / "raw-0").glob("*.json"))
    )
    assert "unknown namespace `BigOperators`" not in raw_text
    assert "unknown identifier 'ℝ'" not in raw_text

    isolated_outputs: list[Path] = []
    for index, row in enumerate(_ROWS):
        out_dir = tmp_path / f"isolated-{index}"
        isolated_outputs.append(out_dir)
        _extract_sft_chunk(
            project_dir=_PROJECT,
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
