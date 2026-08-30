"""Reproduce the frozen corpus-v0 trainer records from the legacy mixed corpus.

This is a historical adapter, not the builder for the new value-first SFT1
dataset. It remains in the repository because :mod:`leanfaith.corpus2.build_v1`
validates and consumes the manifest contract emitted here. The adapter applies
the golden blocklist to both sides, removes byte-identical pairs, asserts that
the old ``lf_alpha`` leak is absent, and drops packed pairs above 1,024 tokens
instead of silently truncating them.

Run: ``python -m leanfaith.corpus2.from_mixed_v0``.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Protocol, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.representations.views import signature_near_dup_hash

_MIXED = Path(
    "/storage/milikic/leanfaith/experimental_mixed_supervision/"
    "firsthop_kimi_qwen1125_composition_f7b398af_v1/records.jsonl"
)
_BLOCKLIST = Path("data/benchmarks/golden_blocklist_v1.json")
_OUT = Path("/storage/milikic/leanfaith/corpus2/v0_from_mixed")


_SNAPSHOT = Path(
    "/storage/milikic/models/hub/models--answerdotai--ModernBERT-base/snapshots/"
    "8949b909ec900327062f0ebf497f51aef5e6f0c8"
)
_MAX_TOKENS = 1024


class _Tokenizer(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...


def _load_tokenizer(snapshot_dir: Path) -> _Tokenizer:
    """Load the exact local tokenizer used to build the frozen artifact."""

    from transformers import AutoTokenizer

    return cast(
        _Tokenizer,
        AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            str(snapshot_dir), local_files_only=True, trust_remote_code=False
        ),
    )


def convert(
    mixed_path: Path = _MIXED,
    blocklist_path: Path = _BLOCKLIST,
    out_dir: Path = _OUT,
    snapshot_dir: Path = _SNAPSHOT,
    *,
    tokenizer: _Tokenizer | None = None,
) -> dict[str, object]:
    from leanfaith.eval.m1_runtime import pack_pair

    if tokenizer is None:
        tokenizer = _load_tokenizer(snapshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    blocked = set(json.loads(blocklist_path.read_text(encoding="utf-8"))["near_dup_hashes"])

    outputs = {
        split: (out_dir / f"records_{split}_v0.jsonl").open("w", encoding="utf-8")
        for split in ("train", "validation", "test")
    }
    counts: dict[str, int] = {
        "train": 0,
        "validation": 0,
        "test": 0,
        "blocklist_dropped": 0,
        "identical_dropped": 0,
        "overlength_dropped": 0,
    }
    try:
        with mixed_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                reference = row["source"]["headless"]
                candidate = row["candidate"]["headless"]
                assert "lf_alpha_" not in reference and "lf_alpha_" not in candidate, (
                    "P01 leak reached the mixed corpus"
                )
                if reference.strip() == candidate.strip():
                    counts["identical_dropped"] += 1
                    continue
                if (
                    signature_near_dup_hash(reference) in blocked
                    or signature_near_dup_hash(candidate) in blocked
                ):
                    counts["blocklist_dropped"] += 1
                    continue
                if (
                    len(tokenizer.encode(pack_pair(reference, candidate), add_special_tokens=True))
                    > _MAX_TOKENS
                ):
                    counts["overlength_dropped"] += 1
                    continue
                split = row["split"]
                record = {
                    "record_id": row["record_id"],
                    "reference_headless": reference,
                    "candidate_headless": candidate,
                    "label": row["pseudo_target"] == "same_claim",
                    "group_key": row["split_component_id"],
                    "family": "+".join(row.get("family_ids") or []) or None,
                    "source": row.get("pseudo_target_basis"),
                    "weight": None,
                }
                outputs[split].write(json.dumps(record, ensure_ascii=False) + "\n")
                counts[split] += 1
    finally:
        for sink in outputs.values():
            sink.close()

    manifest: dict[str, object] = {
        "command": "corpus2_from_mixed_v0",
        "created_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "mixed_corpus": {"path": str(mixed_path), "sha256": hash_file(mixed_path)},
        "blocklist": {"path": str(blocklist_path), "sha256": hash_file(blocklist_path)},
        "counts": counts,
        "outputs": {
            split: {
                "path": str(out_dir / f"records_{split}_v0.jsonl"),
                "sha256": hash_file(out_dir / f"records_{split}_v0.jsonl"),
            }
            for split in ("train", "validation", "test")
        },
    }
    (out_dir / "corpus_v0_manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def main() -> None:
    manifest = convert()
    print(json.dumps(manifest["counts"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
