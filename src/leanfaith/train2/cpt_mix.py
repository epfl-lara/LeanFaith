"""S0-v2 CPT mixture: file chunks + statement↔proof + signature views.

Owner direction (2026-08-28): raw source-file chunks alone under-teach how a
theorem's signature relates to its proof. The v2 corpus adds:

- ``statement_proof``: complete ``theorem … := by …`` records from the public
  ``formalmathatepfl/sft_classic_numina`` dataset (~100K compiled-valid
  rows) — the encoder sees signatures matched to their proofs;
- ``signature_view``: the same theorems' headless signatures rendered with
  the exact ``[HEADLESS]`` marker the downstream classifier consumes, so the
  packed-view tokens are in-distribution at CPT time.

Both new subsets are screened with the SAME golden contamination patterns as
the file chunks (Numina contains AMC/AIME/IMO problems that overlap miniF2F —
hits are expected, not hypothetical). Output: one mixed jsonl + validation
slice + manifest.

Run: ``python -m leanfaith.train2.cpt_mix``.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import tempfile
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.representations.views import normalize_headless
from leanfaith.train2.cpt_screen import build_patterns

_NUMINA_ROOT = Path(
    "/storage/milikic/datasets/formalmathatepfl___sft_classic_numina/default/0.0.0/"
    "9ba1be2e988c864a9b6c79a4e758a9944ff00cc6"
)
_SCREENED_V1 = Path("/storage/milikic/leanfaith/cpt/screened_v1")
_DEFAULT_PAIRS = Path("/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl")
_DEFAULT_OUT = Path("/storage/milikic/leanfaith/cpt/mixed_v2")

_HEADLESS_MARKER = "[HEADLESS]\n"
_THEOREM_RE = re.compile(r"^(theorem|lemma)\s", re.MULTILINE)
_WS = re.compile(r"\s+")
_VALIDATION_EVERY = 200


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _statement_of(lean_code: str) -> str | None:
    """The final theorem/lemma declaration's headless signature, if any."""

    last = None
    for found in _THEOREM_RE.finditer(lean_code):
        last = found
    if last is None:
        return None
    return normalize_headless(lean_code[last.start() :])


def build_mixed_corpus(
    numina_root: Path = _NUMINA_ROOT,
    screened_dir: Path = _SCREENED_V1,
    pairs_path: Path = _DEFAULT_PAIRS,
    out_dir: Path = _DEFAULT_OUT,
) -> dict[str, object]:
    from datasets import Dataset

    out_dir.mkdir(parents=True, exist_ok=True)
    patterns = build_patterns(pairs_path)

    # Load the compiled-valid numina rows once, then screen them with a single
    # grep -F pass (Aho-Corasick) instead of a quadratic Python scan.
    numina_rows: list[tuple[str | None, str]] = []
    for split_name in ("train", "test"):
        dataset = Dataset.from_file(str(numina_root / f"sft_classic_numina-{split_name}.arrow"))
        for row in dataset:
            if row.get("valid") is not True:
                continue
            lean_code = str(row["lean_code"]).strip()
            if lean_code:
                numina_rows.append((row.get("uuid"), lean_code))
    with tempfile.TemporaryDirectory(prefix="cpt_mix_") as tmp:
        collapsed_path = Path(tmp) / "numina_collapsed.txt"
        patterns_path = Path(tmp) / "patterns.txt"
        patterns_path.write_text("\n".join(patterns) + "\n", encoding="utf-8")
        with collapsed_path.open("w", encoding="utf-8") as sink:
            for _, lean_code in numina_rows:
                sink.write(_collapse(lean_code))
                sink.write("\n")
        completed = subprocess.run(
            ["grep", "-F", "-n", "-f", str(patterns_path), str(collapsed_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"grep failed: {completed.stderr[:500]}")
        contaminated_lines = {
            int(entry.split(":", 1)[0]) for entry in completed.stdout.splitlines() if ":" in entry
        }

    train_path = out_dir / "cpt_mixed_train_v2.jsonl"
    validation_path = out_dir / "cpt_mixed_validation_v2.jsonl"
    excluded_path = out_dir / "cpt_mixed_excluded_v2.jsonl"
    counts: dict[str, int] = {
        "file_chunk": 0,
        "statement_proof": 0,
        "signature_view": 0,
        "validation": 0,
        "excluded": 0,
    }
    emitted = 0

    with (
        train_path.open("w", encoding="utf-8") as train_sink,
        validation_path.open("w", encoding="utf-8") as validation_sink,
        excluded_path.open("w", encoding="utf-8") as excluded_sink,
    ):

        def emit(record: dict[str, object], subset_key: str) -> None:
            nonlocal emitted
            emitted += 1
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            if emitted % _VALIDATION_EVERY == 0:
                counts["validation"] += 1
                validation_sink.write(line)
            else:
                counts[subset_key] += 1
                train_sink.write(line)

        # (a) already-screened file chunks pass straight through.
        with (screened_dir / "cpt_train_screened_v1.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                emit(
                    {"text": record["text"], "subset": record.get("subset", "file_chunk")},
                    "file_chunk",
                )

        # (b)+(c) numina statement+proof and signature views (pre-screened).
        for line_number, (uuid, lean_code) in enumerate(numina_rows, start=1):
            if line_number in contaminated_lines:
                counts["excluded"] += 1
                excluded_sink.write(
                    json.dumps({"uuid": uuid, "subset": "numina_statement_proof"}, sort_keys=True)
                    + "\n"
                )
                continue
            emit({"text": lean_code, "subset": "numina_statement_proof"}, "statement_proof")
            statement = _statement_of(lean_code)
            if statement and len(statement) >= 40:
                emit(
                    {"text": _HEADLESS_MARKER + statement, "subset": "numina_signature_view"},
                    "signature_view",
                )

    manifest: dict[str, object] = {
        "command": "cpt_mix",
        "created_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "numina_revision": "9ba1be2e988c864a9b6c79a4e758a9944ff00cc6",
        "numina_access": "public",
        "screened_v1_manifest": str(screened_dir / "cpt_screen_manifest.json"),
        "golden_pairs": {"path": str(pairs_path), "sha256": hash_file(pairs_path)},
        "patterns_count": len(patterns),
        "rows": dict(counts, total_emitted=emitted),
        "validation_every": _VALIDATION_EVERY,
        "outputs": {
            "train": {"path": str(train_path), "sha256": hash_file(train_path)},
            "validation": {"path": str(validation_path), "sha256": hash_file(validation_path)},
        },
    }
    (out_dir / "cpt_mix_manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def main() -> None:
    manifest = build_mixed_corpus()
    print(json.dumps(manifest["rows"], indent=2, sort_keys=True))
    print(f"manifest -> {_DEFAULT_OUT / 'cpt_mix_manifest.json'}")


if __name__ == "__main__":
    main()
