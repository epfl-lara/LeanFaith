from __future__ import annotations

from pathlib import Path

from leanfaith.sft2b.durable import AppendOnlyJournal, immutable_write


def test_journal_suppresses_duplicate_terminal_without_mutating_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    immutable_write(artifact, b'{"value":1}\n')
    journal = AppendOnlyJournal(
        tmp_path / "journal/events.jsonl",
        run_id=f"sft2b_run:{'a' * 64}",
        source_id=f"sft2b_source:{'b' * 64}",
    )

    assert journal.append(
        stage="source_recovered",
        terminal_key="source:recovered",
        artifact_path=artifact,
    )
    assert not journal.append(
        stage="source_recovered",
        terminal_key="source:recovered",
        artifact_path=artifact,
    )
    assert len(journal.events()) == 1
