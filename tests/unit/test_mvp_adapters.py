"""LF-011: ProofNetVerif mapping and repository inventory adapters."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.sources.mathlib import (
    CheckoutMismatchError,
    build_inventory,
    verify_checkout_revision,
)
from leanfaith.sources.proofnetverif import COLUMN_MAPPING, parse_row

_REPO_ROOT = find_repo_root(Path(__file__).parent)

# --- ProofNetVerif (§9.3) ---

_PNV_ROW = {
    "id": "Rudin|exercise_1_1a",
    "nl_statement": "If r is rational and x is irrational, prove that r + x is irrational.",
    "lean4_src_header": "import Mathlib\nopen Real\n",
    "lean4_formalization": "theorem exercise_1_1a (x : ℝ) (y : ℚ) (h : Irrational x) : Irrational (y + x) :=",
    "lean4_prediction": "theorem exercise_1_1a (x : ℝ) (y : ℚ) (h : Irrational x) : Irrational (x + y) :=",
    "correct": True,
}


def test_proofnetverif_mapping_is_9_3_verbatim() -> None:
    assert COLUMN_MAPPING == {
        "problem_id": "id",
        "nl_statement": "nl_statement",
        "lean_header": "lean4_src_header",
        "reference_lean": "lean4_formalization",
        "candidate_lean": "lean4_prediction",
        "source_label": "correct",
    }


def test_proofnetverif_row_parses_evaluation_only() -> None:
    parsed = parse_row(_PNV_ROW, split="valid")
    assert parsed.problem_id == "Rudin|exercise_1_1a"
    assert parsed.split == "valid"
    assert parsed.source_label is True
    assert parsed.usage == "evaluation_only"
    assert parsed.reference_lean.startswith("theorem exercise_1_1a")


def test_proofnetverif_missing_column_fails_closed() -> None:
    broken = dict(_PNV_ROW)
    del broken["correct"]
    with pytest.raises(KeyError):
        parse_row(broken, split="valid")


_REAL_PNV_SAMPLE = _REPO_ROOT / "data" / "raw" / "sources" / "proofnetverif" / "probe_sample.jsonl"


@pytest.mark.skipif(not _REAL_PNV_SAMPLE.is_file(), reason="probe sample not present")
def test_real_proofnetverif_sample_parses() -> None:
    rows = [json.loads(line) for line in _REAL_PNV_SAMPLE.read_text().strip().splitlines()]
    parsed = [parse_row(row, split="valid") for row in rows]
    assert len(parsed) == 100
    assert all(p.nl_statement for p in parsed)


# --- repository inventory ---


def _fixture_checkout(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "proj"
    checkout.mkdir()
    (checkout / "Lib").mkdir()
    (checkout / "Lib" / "A.lean").write_text("theorem a : True := trivial\n")
    (checkout / "Lib" / "B.lean").write_text("theorem b : True := trivial\n")
    (checkout / "README.md").write_text("not lean\n")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=checkout, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()
    return checkout, head


def test_inventory_is_deterministic_and_glob_scoped(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    first = build_inventory(
        checkout, source="fixture", revision=head, root_module="Lib", globs=("Lib/**/*.lean",)
    )
    second = build_inventory(
        checkout, source="fixture", revision=head, root_module="Lib", globs=("Lib/**/*.lean",)
    )
    assert first == second
    assert first.file_count == 2
    assert [f.relative_path for f in first.files] == ["Lib/A.lean", "Lib/B.lean"]
    assert all(len(f.sha256) == 64 for f in first.files)


def test_inventory_rejects_drifted_checkout(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    with pytest.raises(CheckoutMismatchError, match="pinned revision"):
        verify_checkout_revision(checkout, "0" * 40)
    assert verify_checkout_revision(checkout, head) == head


def test_inventory_respects_limit(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    limited = build_inventory(
        checkout,
        source="fixture",
        revision=head,
        root_module="Lib",
        globs=("Lib/**/*.lean",),
        limit=1,
    )
    assert limited.file_count == 1


# --- config binding (LF-011) ---


def test_hf_probe_config_binding_from_repo_configs() -> None:
    from leanfaith.sources.probe import hf_probe_config_from_yaml

    paths = RepoPaths.discover(Path(__file__).parent)
    sft = hf_probe_config_from_yaml(paths, "sft_classic")
    assert sft.dataset_id == "formalmathatepfl/sft_classic"
    assert sft.token is not None and sft.token.env == "HF_TOKEN"
    assert sft.revision == "0bf9f424309f668c2c2dd214aef6ec5d1d5c042f"
    assert sft.external_api_approved is None

    pnv = hf_probe_config_from_yaml(paths, "proofnetverif")
    assert pnv.dataset_id == "PAug/ProofNetVerif"
    assert pnv.sample_split == "valid"
    assert pnv.token is None

    numina = hf_probe_config_from_yaml(paths, "sft_classic_numina")
    assert numina.expected_columns == ("uuid", "question", "answer", "lean_code")
