"""LF-011: ProofNetVerif mapping and repository inventory adapters."""

from __future__ import annotations

import hashlib
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
    (checkout / "Lib" / "NOTES.md").write_text("tracked root-module metadata\n")
    (checkout / "Lib.lean").write_text("import Lib.A\nimport Lib.B\n")
    (checkout / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage fixture\n")
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
    assert first.files[0].sha256 == hashlib.sha256(b"theorem a : True := trivial\n").hexdigest()


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


def test_inventory_rejects_modified_tracked_match(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    (checkout / "Lib" / "A.lean").write_text("theorem a : False := by contradiction\n")

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_rejects_tracked_change_elsewhere_in_root_module(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    (checkout / "Lib" / "NOTES.md").write_text("modified metadata\n")

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


@pytest.mark.parametrize("relative_path", ["Lib.lean", "lakefile.lean"])
def test_inventory_rejects_changed_root_or_project_input(
    tmp_path: Path, relative_path: str
) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    (checkout / relative_path).write_text("local project drift\n")

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_rejects_untracked_glob_match(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    (checkout / "Lib" / "Generated.lean").write_text("theorem generated : True := trivial\n")

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_rejects_untracked_symlink_match(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    (checkout / "Lib" / "Linked.lean").symlink_to("A.lean")

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_rejects_symlink_committed_at_pin(tmp_path: Path) -> None:
    checkout, _ = _fixture_checkout(tmp_path)
    (checkout / "Lib" / "Linked.lean").symlink_to("A.lean")
    subprocess.run(["git", "add", "Lib/Linked.lean"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add symlink"], cwd=checkout, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()

    with pytest.raises(CheckoutMismatchError, match="symbolic link"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_rejects_assume_unchanged_tampering(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "Lib/A.lean"],
        cwd=checkout,
        check=True,
    )
    (checkout / "Lib" / "A.lean").write_text("theorem hidden : False := by contradiction\n")

    with pytest.raises(CheckoutMismatchError, match="hidden or nonstandard index state"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_rejects_missing_skip_worktree_file(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    subprocess.run(
        ["git", "update-index", "--skip-worktree", "Lib/B.lean"],
        cwd=checkout,
        check=True,
    )
    (checkout / "Lib" / "B.lean").unlink()

    with pytest.raises(CheckoutMismatchError, match="hidden or nonstandard index state"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


@pytest.mark.parametrize(
    ("relative_path", "attribute_pattern"),
    [("Lib/A.lean", "Lib/*.lean"), ("Lib.lean", "Lib.lean")],
)
def test_inventory_compares_scoped_worktree_bytes_to_pinned_blob(
    tmp_path: Path,
    relative_path: str,
    attribute_pattern: str,
) -> None:
    checkout, _ = _fixture_checkout(tmp_path)
    subprocess.run(
        ["git", "config", "filter.pin.clean", "sed s/worktree/pinned/g"],
        cwd=checkout,
        check=True,
    )
    subprocess.run(["git", "config", "filter.pin.smudge", "cat"], cwd=checkout, check=True)
    (checkout / ".gitattributes").write_text(f"{attribute_pattern} filter=pin\n")
    (checkout / relative_path).write_text("worktree\n")
    subprocess.run(
        ["git", "add", ".gitattributes", relative_path],
        cwd=checkout,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "filtered blob"], cwd=checkout, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    with pytest.raises(
        CheckoutMismatchError,
        match=f"worktree bytes differ from pinned Git blob for {relative_path}",
    ):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
            limit=1,
        )


def test_inventory_disables_git_replace_objects(tmp_path: Path) -> None:
    checkout, pinned = _fixture_checkout(tmp_path)
    (checkout / "Lib" / "A.lean").write_text("theorem replacement : False := by contradiction\n")
    subprocess.run(["git", "add", "Lib/A.lean"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "replacement tree"], cwd=checkout, check=True)
    replacement = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "replace", pinned, replacement], cwd=checkout, check=True)
    subprocess.run(["git", "reset", "--hard", "-q", pinned], cwd=checkout, check=True)
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == pinned
    )
    assert not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=pinned,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_inventory_rejects_deleted_or_renamed_match(tmp_path: Path, operation: str) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    if operation == "delete":
        (checkout / "Lib" / "B.lean").unlink()
    else:
        subprocess.run(
            ["git", "mv", "Lib/B.lean", "Lib/Renamed.lean"],
            cwd=checkout,
            check=True,
        )

    with pytest.raises(CheckoutMismatchError, match="pinned inventory scope"):
        build_inventory(
            checkout,
            source="fixture",
            revision=head,
            root_module="Lib",
            globs=("Lib/**/*.lean",),
        )


def test_inventory_allows_drift_outside_root_and_globs(tmp_path: Path) -> None:
    checkout, head = _fixture_checkout(tmp_path)
    expected = build_inventory(
        checkout,
        source="fixture",
        revision=head,
        root_module="Lib",
        globs=("Lib/**/*.lean",),
    )
    (checkout / "README.md").write_text("local checkout note\n")

    actual = build_inventory(
        checkout,
        source="fixture",
        revision=head,
        root_module="Lib",
        globs=("Lib/**/*.lean",),
    )

    assert actual == expected


# --- config binding (LF-011) ---


def test_hf_probe_config_binding_from_repo_configs() -> None:
    from leanfaith.sources.probe import hf_probe_config_from_yaml

    paths = RepoPaths.discover(Path(__file__).parent)
    sft = hf_probe_config_from_yaml(paths, "sft_classic")
    assert sft.dataset_id == "formalmathatepfl/sft_classic"
    assert sft.token is not None and sft.token.env == "HF_TOKEN"
    assert sft.revision == "0bf9f424309f668c2c2dd214aef6ec5d1d5c042f"
    assert sft.external_api_approved is False
    assert sft.license_status == "undeclared"
    assert sft.external_transmission_allowed is False

    pnv = hf_probe_config_from_yaml(paths, "proofnetverif")
    assert pnv.dataset_id == "PAug/ProofNetVerif"
    assert pnv.sample_split == "valid"
    assert pnv.token is None

    numina = hf_probe_config_from_yaml(paths, "sft_classic_numina")
    assert numina.expected_columns == ("uuid", "question", "answer", "lean_code")
