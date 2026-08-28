"""Golden-contamination screen for the S0 CPT corpus (PLAN.md Track T, S0(b)).

MLM exposure to final-test theorem text is still test exposure, so the CPT
corpus is screened against the frozen golden blocklist before the full S0
run. Two pattern families, both matched on whitespace-collapsed text:

- problem-name anchors (``exercise_1_19a``, ``numbertheory_4x3m7y3neq2003``)
  from every golden group;
- statement fingerprints — the first ``fingerprint_chars`` characters of
  every collapsed golden reference/candidate headless statement.

Matching uses ``grep -F -f`` (Aho-Corasick) over a one-collapsed-chunk-per-
line projection of the corpus, so the scan stays fast at 470K rows. Output:
screened train jsonl, frozen validation slice, excluded rows (audit), and a
plain run manifest with per-subset exclusion counts.

Run: ``python -m leanfaith.train2.cpt_screen``.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import tempfile
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex

_WS = re.compile(r"\s+")

_DEFAULT_CORPUS = Path(
    "/storage/milikic/lean_cpt_updates/2026-08-12-curated-libraries/hf_cpt_dataset.jsonl"
)
_DEFAULT_PAIRS = Path("/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl")
_DEFAULT_OUT = Path("/storage/milikic/leanfaith/cpt/screened_v1")

_MIN_NAME_CHARS = 8
_MIN_STATEMENT_CHARS = 40
_FINGERPRINT_CHARS = 80
_VALIDATION_EVERY = 200  # deterministic ~0.5% held-out slice


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def build_patterns(pairs_path: Path) -> list[str]:
    """Problem-name anchors + statement fingerprints from the golden pairs."""

    patterns: set[str] = set()
    with pairs_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            name = record["group_key"].split("::", 1)[1]
            if len(name) >= _MIN_NAME_CHARS:
                patterns.add(name)
            for side in ("reference_headless", "candidate_headless"):
                collapsed = _collapse(record[side])
                if len(collapsed) >= _MIN_STATEMENT_CHARS:
                    patterns.add(collapsed[:_FINGERPRINT_CHARS])
    return sorted(patterns)


def screen_corpus(
    corpus_path: Path = _DEFAULT_CORPUS,
    pairs_path: Path = _DEFAULT_PAIRS,
    out_dir: Path = _DEFAULT_OUT,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    patterns = build_patterns(pairs_path)

    with tempfile.TemporaryDirectory(prefix="cpt_screen_") as tmp:
        collapsed_path = Path(tmp) / "corpus_collapsed.txt"
        patterns_path = Path(tmp) / "patterns.txt"
        patterns_path.write_text("\n".join(patterns) + "\n", encoding="utf-8")
        with (
            corpus_path.open(encoding="utf-8") as source,
            collapsed_path.open("w", encoding="utf-8") as sink,
        ):
            for line in source:
                text = json.loads(line).get("text", "") if line.strip() else ""
                sink.write(_collapse(text))
                sink.write("\n")
        completed = subprocess.run(
            ["grep", "-F", "-n", "-f", str(patterns_path), str(collapsed_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"grep failed: {completed.stderr[:500]}")
        hit_lines = {
            int(row.split(":", 1)[0]) for row in completed.stdout.splitlines() if ":" in row
        }

    train_path = out_dir / "cpt_train_screened_v1.jsonl"
    validation_path = out_dir / "cpt_validation_v1.jsonl"
    excluded_path = out_dir / "cpt_excluded_v1.jsonl"
    excluded_by_subset: dict[str, int] = {}
    kept = validation = excluded = 0
    with (
        corpus_path.open(encoding="utf-8") as source,
        train_path.open("w", encoding="utf-8") as train_sink,
        validation_path.open("w", encoding="utf-8") as validation_sink,
        excluded_path.open("w", encoding="utf-8") as excluded_sink,
    ):
        for number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            if number in hit_lines:
                excluded += 1
                subset = json.loads(line).get("subset", "unknown")
                excluded_by_subset[subset] = excluded_by_subset.get(subset, 0) + 1
                excluded_sink.write(line)
            elif number % _VALIDATION_EVERY == 0:
                validation += 1
                validation_sink.write(line)
            else:
                kept += 1
                train_sink.write(line)

    manifest: dict[str, object] = {
        "command": "cpt_screen",
        "created_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "corpus": {"path": str(corpus_path), "sha256": hash_file(corpus_path)},
        "golden_pairs": {"path": str(pairs_path), "sha256": hash_file(pairs_path)},
        "patterns": {
            "count": len(patterns),
            "sha256": sha256_hex("\n".join(patterns).encode("utf-8")),
            "min_name_chars": _MIN_NAME_CHARS,
            "min_statement_chars": _MIN_STATEMENT_CHARS,
            "fingerprint_chars": _FINGERPRINT_CHARS,
        },
        "rows": {
            "input": kept + validation + excluded,
            "train": kept,
            "validation": validation,
            "excluded": excluded,
            "excluded_by_subset": excluded_by_subset,
        },
        "validation_every": _VALIDATION_EVERY,
        "outputs": {
            "train": {"path": str(train_path), "sha256": hash_file(train_path)},
            "validation": {"path": str(validation_path), "sha256": hash_file(validation_path)},
            "excluded": {"path": str(excluded_path), "sha256": hash_file(excluded_path)},
        },
    }
    (out_dir / "cpt_screen_manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def main() -> None:
    manifest = screen_corpus()
    rows = manifest["rows"]
    print(json.dumps(rows, indent=2, sort_keys=True))
    print(f"manifest -> {_DEFAULT_OUT / 'cpt_screen_manifest.json'}")


if __name__ == "__main__":
    main()
