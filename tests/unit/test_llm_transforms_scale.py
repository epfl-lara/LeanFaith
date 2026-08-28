"""Focused, offline tests for the Track D-3 Codex scale harness."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import leanfaith.corpus2.llm_transforms as transforms
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.train2.trainer import TrainingRecord


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_blocklist(
    path: Path,
    *,
    near_dup_hashes: Sequence[str] = (),
    group_keys: Sequence[str] = (),
) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": ["golden_blocklist_v1"],
                "near_dup_hashes": list(near_dup_hashes),
                "group_keys": list(group_keys),
            }
        ),
        encoding="utf-8",
    )
    return path


def _pair(pair_id: str, group_key: str, label: bool) -> dict[str, Any]:
    return {
        "pair_id": pair_id,
        "group_key": group_key,
        "partition": "golden_train",
        "label": label,
        "label_conflict": False,
        "label_provenance": "expert_human",
        "reference_headless": f"(n : ℕ) : n = n ∧ True -- {pair_id}",
        "candidate_headless": f"(m : ℕ) : m = m ∧ True -- {pair_id}",
    }


def _write_frozen_pairs(tmp_path: Path) -> tuple[Path, Path]:
    rows = [
        _pair("positive-0", "gold::positive-0", True),
        _pair("positive-1", "gold::positive-1", True),
        _pair("negative-0", "gold::negative-0", False),
        _pair("negative-1", "gold::negative-1", False),
    ]
    pairs_path = _write_jsonl(tmp_path / "pairs.jsonl", rows)
    manifest_path = tmp_path / "partition.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "golden_partition_v1",
                "canonical_pairs_sha256": _sha256_file(pairs_path),
                "group_partitions": {row["group_key"]: "golden_train" for row in rows},
            }
        ),
        encoding="utf-8",
    )
    return pairs_path, manifest_path


def _theorem_row(
    theorem_id: str,
    group_key: str,
    *,
    revision: str = "fixture-revision",
    eligible: bool = True,
) -> dict[str, Any]:
    return {
        "theorem": {
            "theorem_id": theorem_id,
            "root_ancestry_ids": [group_key],
            "parent_theorem_ids": [],
            "source": "mathlib",
            "source_revision": revision,
            "source_file": f"Mathlib/Fixture/{theorem_id}.lean",
            "source_range": [2, 3],
            "metadata": {"transform_source_eligible": eligible},
        }
    }


def _representation_row(theorem_id: str, headless: str) -> dict[str, Any]:
    return {
        "theorem_id": theorem_id,
        "representation_id": f"repr::{theorem_id}",
        "content_hash": hashlib.sha256(headless.encode()).hexdigest(),
        "headless": headless,
        "view_status": {"headless": "ok"},
    }


def _source(
    index: int,
    headless: str | None = None,
) -> transforms.SourceStatement:
    statement = headless or (
        f"(P Q R S : Prop) (h{index} : P → Q) (hQR : Q → R) (hRS : R → S) : P → S"
    )
    return transforms.SourceStatement(
        statement_id=f"repr::{index}",
        content_hash=hashlib.sha256(statement.encode()).hexdigest(),
        headless=statement,
        theorem_id=f"mathlib::theorem::{index}",
        group_key=f"mathlib::root::{index}",
        source_file=f"Mathlib/Fixture/T{index}.lean",
    )


def _job(index: int, statement: transforms.SourceStatement | None = None) -> transforms.ScaleJob:
    family = transforms.family_for_index(index)
    source = statement or _source(index)
    prompt = f"fixture prompt {index}"
    return transforms.ScaleJob(
        job_id=f"fixture-job-{index}",
        index=index,
        statement=source,
        direction=family.direction,
        family=family,
        prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        family_heuristic_matched=True,
    )


def _record(
    job: transforms.ScaleJob,
    candidate: str,
    **overrides: Any,
) -> transforms.TransformRecord:
    values: dict[str, Any] = {
        "provider": "codex",
        "index": job.index,
        "statement_id": job.statement.statement_id,
        "statement_hash": job.statement.content_hash,
        "source_statement": job.statement.headless,
        "direction": job.direction,
        "assigned_family": job.family.family_id,
        "prompt_sha256": job.prompt_sha256,
        "raw_stdout_path": f"/fixture/{job.job_id}.stdout",
        "returncode": 0,
        "timed_out": False,
        "fallback_used": False,
        "parse_ok": True,
        "rewritten_statement": candidate,
        "intended_label": ("consistent" if job.direction == "preserve" else "inconsistent"),
        "transformation": job.family.family_id,
        "reasoning": "fixture",
        "confidence": 0.9,
        "label_matches_direction": True,
        "family_matches_assignment": True,
        "rewrite_changed": True,
    }
    values.update(overrides)
    return transforms.TransformRecord(**values)


def _successful_provider_call(job: transforms.ScaleJob) -> transforms.ProviderCall:
    payload = {
        "rewritten_statement": f"(n : ℕ) : n = n ∧ True -- {job.family.family_id}",
        "intended_label": ("consistent" if job.direction == "preserve" else "inconsistent"),
        "transformation": job.family.family_id,
        "reasoning": "fixture transformation",
        "confidence": 0.9,
    }
    return transforms.ProviderCall(
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        elapsed_seconds=0.01,
    )


def test_family_schedule_and_assigned_prompt_are_frozen() -> None:
    families = [transforms.family_for_index(index) for index in range(200)]

    assert [family.family_id for family in families[:16:2]] == [
        "P23",
        "P27",
        "P29",
        "P31",
        "P20",
        "P32",
        "P36",
        "P28",
    ]
    assert [family.family_id for family in families[1:13:2]] == [
        "N21",
        "N22",
        "N24",
        "N25",
        "N26",
        "N23",
    ]
    assert all(
        family.direction == transforms.direction_for_index(index)
        for index, family in enumerate(families)
    )

    preserve_counts = {
        family_id: sum(family.family_id == family_id for family in families)
        for family_id in {family.family_id for family in transforms.PRESERVE_FAMILIES}
    }
    break_counts = {
        family_id: sum(family.family_id == family_id for family in families)
        for family_id in {family.family_id for family in transforms.BREAK_FAMILIES}
    }
    assert max(preserve_counts.values()) - min(preserve_counts.values()) == 1
    assert max(break_counts.values()) - min(break_counts.values()) == 1

    assigned = transforms.PRESERVE_FAMILIES[1]
    prompt = transforms.build_prompt(
        _source(0).headless,
        "preserve",
        [transforms.FewShot("p", "(n : ℕ) : n = n", "(m : ℕ) : m = m", "consistent")],
        assigned,
    )
    assert f"- {assigned.family_id}: {assigned.instruction}" in prompt
    assert f'`transformation` value MUST be exactly "{assigned.family_id}"' in prompt
    assert all(
        f"- {other.family_id}:" not in prompt
        for other in (*transforms.PRESERVE_FAMILIES, *transforms.BREAK_FAMILIES)
        if other.family_id != assigned.family_id
    )
    with pytest.raises(ValueError, match="has direction"):
        transforms.build_prompt(_source(0).headless, "break", [], assigned)
    with pytest.raises(ValueError, match="non-negative"):
        transforms.family_for_index(-1)


def test_frozen_gold_binding_rejects_hash_and_partition_drift(tmp_path: Path) -> None:
    pairs_path, manifest_path = _write_frozen_pairs(tmp_path)

    shots = transforms.build_fewshots(
        pairs_path,
        seed=7,
        k_pos=1,
        k_neg=1,
        partition_manifest_path=manifest_path,
    )
    assert {shot.verdict for shot in shots} == {"consistent", "inconsistent"}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected_group = "gold::positive-0"
    manifest["group_partitions"][selected_group] = "dev"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(transforms.GoldRuleViolation, match="frozen golden_train"):
        transforms.build_fewshots(
            pairs_path,
            seed=7,
            k_pos=1,
            k_neg=1,
            partition_manifest_path=manifest_path,
        )

    manifest["group_partitions"][selected_group] = "golden_train"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pairs_path.write_text(pairs_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(transforms.GoldRuleViolation, match="does not match frozen hash"):
        transforms.build_fewshots(
            pairs_path,
            seed=7,
            k_pos=1,
            k_neg=1,
            partition_manifest_path=manifest_path,
        )


def test_tiny_source_projection_preserves_ancestry_and_applies_blocklist(
    tmp_path: Path,
) -> None:
    statements = {
        "kept": "(a b c : ℕ) (hab : a = b) (hbc : b = c) : a = c ∧ a + b + c = c + b + a",
        "ineligible": "(P Q R : Prop) (hPQ : P → Q) (hQR : Q → R) : P → R ∧ True ∧ True",
        "group-blocked": ("(x y : ℤ) (hxy : x < y) : x ≤ y ∧ x + 1 ≤ y + 1 ∧ True ∧ True"),
        "hash-blocked": ("(s t : Set ℕ) (hst : s = t) : s ∪ t = t ∪ s ∧ s ∩ t = t ∩ s ∧ True"),
    }
    theorems = [
        _theorem_row("kept", "mathlib::root::kept"),
        _theorem_row("ineligible", "mathlib::root::ineligible", eligible=False),
        _theorem_row("group-blocked", "Mathlib::ROOT::Blocked"),
        _theorem_row("hash-blocked", "mathlib::root::hash-blocked"),
    ]
    representations = [
        _representation_row(theorem_id, headless) for theorem_id, headless in statements.items()
    ]
    theorems_path = _write_jsonl(tmp_path / "theorems.jsonl", theorems)
    reprs_path = _write_jsonl(tmp_path / "representations.jsonl", representations)
    blocklist_path = _write_blocklist(
        tmp_path / "blocklist.json",
        near_dup_hashes=[signature_near_dup_hash(statements["hash-blocked"])],
        group_keys=["mathlib::root::blocked"],
    )

    pool, stats = transforms.load_scale_source_statements(
        reprs_path,
        theorems_path,
        blocklist_path,
        seed=19,
        expected_source_revision="fixture-revision",
    )

    assert len(pool) == 1
    assert pool[0].theorem_id == "kept"
    assert pool[0].group_key == "mathlib::root::kept"
    assert pool[0].source_file == "Mathlib/Fixture/kept.lean"
    assert stats == transforms.SourcePoolStats(
        theorem_rows=4,
        representation_rows=4,
        joined_rows=4,
        headless_ok_rows=4,
        length_eligible_rows=4,
        transform_eligible_rows=3,
        blocked_source_rows=2,
        duplicate_source_rows=0,
        eligible_unique_rows=1,
    )


def test_provider_closes_stdin_and_freezes_codex_safety_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        invocations.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(transforms.subprocess, "run", fake_run)
    result = transforms.run_provider("codex", "PROMPT", timeout=17, cwd="/tmp/fixture")

    assert result.returncode == 0
    assert len(invocations) == 1
    command, kwargs = invocations[0]
    assert command[:2] == ["codex", "exec"]
    assert 'model_reasoning_effort="high"' in command
    assert command[command.index("--disable") : command.index("--disable") + 2] == [
        "--disable",
        "shell_tool",
    ]
    assert 'web_search="disabled"' in command
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["cwd"] == "/tmp/fixture"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 17


def test_production_scale_requires_exactly_200_jobs(tmp_path: Path) -> None:
    storage_output = Path("/storage/milikic/leanfaith") / (f"pytest-count-guard-{tmp_path.name}")

    with pytest.raises(ValueError, match="requires exactly 200 jobs, got 1"):
        transforms.run_scale(
            transforms.ScaleConfig(
                output_root=storage_output,
                count=1,
                enforce_storage_root=True,
            )
        )

    assert not storage_output.exists()


def test_production_mathlib_checkout_must_match_clean_pinned_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = transforms.ScaleConfig(
        output_root=Path("/storage/milikic/leanfaith/fixture-output"),
        mathlib_project=tmp_path / "mathlib",
        expected_source_revision="expected-revision",
        enforce_storage_root=True,
    )
    monkeypatch.setattr(transforms, "_git_rev", lambda _path: "wrong-revision")
    monkeypatch.setattr(transforms, "_git_is_clean", lambda _path: True)
    with pytest.raises(ValueError, match="revision mismatch"):
        transforms._validate_mathlib_checkout(config)

    monkeypatch.setattr(transforms, "_git_rev", lambda _path: "expected-revision")
    monkeypatch.setattr(transforms, "_git_is_clean", lambda _path: False)
    with pytest.raises(ValueError, match="requires a clean mathlib checkout"):
        transforms._validate_mathlib_checkout(config)


def test_contextual_lean_resume_rejects_source_prefix_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocklist_path = _write_blocklist(tmp_path / "blocklist.json")
    mathlib_project = tmp_path / "mathlib"
    source_path = mathlib_project / "Mathlib" / "Fixture" / "Context.lean"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("import Mathlib\n", encoding="utf-8")
    source = transforms.SourceStatement(
        statement_id="repr::context-resume",
        content_hash="fixture-hash",
        headless="(P : Prop) (h : P) : P",
        theorem_id="mathlib::context-resume",
        group_key="mathlib::context-resume",
        source_file="Mathlib/Fixture/Context.lean",
        source_range_start=2,
    )
    job = _job(0, source)
    record = _record(job, "(P : Prop) (h : P) : P ∧ True")
    monkeypatch.setattr(
        transforms.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    config = transforms.ScaleConfig(
        output_root=tmp_path / "lean-output",
        blocklist_path=blocklist_path,
        mathlib_project=mathlib_project,
        lean_batch_size=1,
        enforce_storage_root=False,
    )

    first, reused = transforms.run_lean_checks(config, [job], [record])
    assert reused == 0
    assert first[0].status == "valid"
    source_path.write_text("import Mathlib.Data.Nat.Basic\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Lean terminal source/config mismatch"):
        transforms.run_lean_checks(config, [job], [record])


def test_incomplete_provider_attempt_refuses_implicit_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(0)
    output_root = tmp_path / "output"
    attempt_dir = output_root / "items" / job.job_id / "attempts" / "000"
    attempt_dir.mkdir(parents=True)
    provider_calls = 0

    def unexpected_provider(*_args: Any, **_kwargs: Any) -> transforms.ProviderCall:
        nonlocal provider_calls
        provider_calls += 1
        return _successful_provider_call(job)

    monkeypatch.setattr(transforms, "run_provider", unexpected_provider)
    config = transforms.ScaleConfig(
        output_root=output_root,
        enforce_storage_root=False,
        retry_incomplete_attempts=False,
    )

    with pytest.raises(ValueError, match="refusing an implicit duplicate paid call"):
        transforms._execute_scale_job(config, job)

    assert provider_calls == 0
    assert not (output_root / "items" / job.job_id / "terminal.json").exists()


def _write_scale_sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    sources = {
        "arrow-source": ("(P Q R S : Prop) (hPQ : P → Q) (hQR : Q → R) (hRS : R → S) : P → S"),
        "numeric-source": (
            "(a b c : ℕ) (hab : a = b) (hbc : b = c) (hca : c = a) : a + b + c = c + b + a"
        ),
    }
    theorems_path = _write_jsonl(
        tmp_path / "scale-theorems.jsonl",
        [_theorem_row(theorem_id, f"mathlib::root::{theorem_id}") for theorem_id in sources],
    )
    reprs_path = _write_jsonl(
        tmp_path / "scale-representations.jsonl",
        [_representation_row(theorem_id, headless) for theorem_id, headless in sources.items()],
    )
    blocklist_path = _write_blocklist(tmp_path / "scale-blocklist.json")
    return reprs_path, theorems_path, blocklist_path


def test_provider_oserror_blocks_tiny_scale_with_zero_started_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs_path, partition_path = _write_frozen_pairs(tmp_path)
    reprs_path, theorems_path, blocklist_path = _write_scale_sources(tmp_path)
    output_root = tmp_path / "oserror-output"
    mathlib_project = tmp_path / "mathlib"
    mathlib_project.mkdir()
    source_path = mathlib_project / "Mathlib" / "Fixture" / "arrow-source.lean"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("import Mathlib\n", encoding="utf-8")

    def missing_codex(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        raise OSError("fixture Codex executable is missing")

    monkeypatch.setattr(transforms, "_codex_cli_version", lambda: "fixture-codex")
    monkeypatch.setattr(transforms, "_git_rev", lambda _path: "fixture-revision")
    monkeypatch.setattr(transforms, "_git_is_clean", lambda _path: True)
    monkeypatch.setattr(transforms.subprocess, "run", missing_codex)
    config = transforms.ScaleConfig(
        pairs_path=pairs_path,
        partition_manifest_path=partition_path,
        reprs_path=reprs_path,
        theorems_path=theorems_path,
        blocklist_path=blocklist_path,
        output_root=output_root,
        mathlib_project=mathlib_project,
        count=1,
        k_pos=1,
        k_neg=1,
        max_workers=1,
        lean_batch_size=1,
        expected_source_revision="fixture-revision",
        enforce_storage_root=False,
    )

    manifest = transforms.run_scale(config)

    assert manifest["status"] == "blocked"
    assert "not every frozen job started a Codex provider process" in manifest["blocked_reason"]
    assert manifest["generation"]["jobs_attempted_this_invocation"] == 1
    assert manifest["generation"]["calls_executed_this_invocation"] == 0
    assert manifest["generation"]["provider_processes_started"] == 0
    assert manifest["generation"]["provider_invocation_errors"] == 1
    assert manifest["generation"]["parse_ok"] == 0
    assert manifest["lean"]["outcomes"] == {"not_generated": 1}
    assert manifest["trainer"]["record_count"] == 0


@pytest.mark.parametrize(
    ("record_field", "corrupted_value"),
    [
        ("provider", "claude"),
        ("index", 99),
        ("statement_hash", "wrong-statement-hash"),
        ("source_statement", "(n : ℕ) : False"),
        ("direction", "break"),
        ("raw_stdout_path", "/wrong/stdout.txt"),
    ],
)
def test_corrupted_generation_terminal_full_record_binding_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_field: str,
    corrupted_value: object,
) -> None:
    job = _job(0)
    config = transforms.ScaleConfig(
        output_root=tmp_path / "output",
        enforce_storage_root=False,
    )
    monkeypatch.setattr(
        transforms,
        "run_provider",
        lambda *_args, **_kwargs: _successful_provider_call(job),
    )
    transforms._execute_scale_job(config, job)
    terminal_path = config.output_root / "items" / job.job_id / "terminal.json"
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal["record"][record_field] = corrupted_value
    terminal_path.write_text(json.dumps(terminal), encoding="utf-8")

    with pytest.raises(ValueError, match="terminal record mismatch"):
        transforms._load_scale_terminal(terminal_path, job)


def test_scale_resume_reuses_fake_provider_and_fake_lean_terminals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs_path, partition_path = _write_frozen_pairs(tmp_path)
    reprs_path, theorems_path, blocklist_path = _write_scale_sources(tmp_path)
    output_root = tmp_path / "scale-output"
    mathlib_project = tmp_path / "mathlib"
    mathlib_project.mkdir()
    for theorem_id in ("arrow-source", "numeric-source"):
        source_path = mathlib_project / "Mathlib" / "Fixture" / f"{theorem_id}.lean"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("import Mathlib\n", encoding="utf-8")
    provider_prompts: list[str] = []
    lean_commands: list[list[str]] = []

    def fake_provider(
        provider: str,
        prompt: str,
        **_: Any,
    ) -> transforms.ProviderCall:
        assert provider == "codex"
        provider_prompts.append(prompt)
        match = re.search(r"^- ([PN]\d+):", prompt, flags=re.MULTILINE)
        assert match is not None
        family_id = match.group(1)
        label = "consistent" if family_id.startswith("P") else "inconsistent"
        payload = {
            "rewritten_statement": (f"(n : ℕ) : n = n ∧ True -- generated by {family_id}"),
            "intended_label": label,
            "transformation": family_id,
            "reasoning": "fixture transformation",
            "confidence": 0.9,
        }
        return transforms.ProviderCall(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
            elapsed_seconds=0.01,
        )

    def fake_lean(command: list[str], **kwargs: Any) -> SimpleNamespace:
        lean_commands.append(command)
        assert command[:3] == ["lake", "env", "lean"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transforms, "run_provider", fake_provider)
    monkeypatch.setattr(transforms, "_codex_cli_version", lambda: "fixture-codex")
    monkeypatch.setattr(transforms, "_git_rev", lambda _path: "fixture-revision")
    monkeypatch.setattr(transforms, "_git_is_clean", lambda _path: True)
    monkeypatch.setattr(transforms.subprocess, "run", fake_lean)
    config = transforms.ScaleConfig(
        pairs_path=pairs_path,
        partition_manifest_path=partition_path,
        reprs_path=reprs_path,
        theorems_path=theorems_path,
        blocklist_path=blocklist_path,
        output_root=output_root,
        mathlib_project=mathlib_project,
        count=2,
        k_pos=1,
        k_neg=1,
        max_workers=1,
        lean_batch_size=2,
        expected_source_revision="fixture-revision",
        enforce_storage_root=False,
    )

    first = transforms.run_scale(config)
    first_records = (output_root / "records.jsonl").read_bytes()
    first_trainer = (output_root / "trainer_records.jsonl").read_bytes()
    generation_terminals = {
        path: path.read_bytes() for path in sorted((output_root / "items").glob("*/terminal.json"))
    }
    lean_terminals = {
        path: path.read_bytes()
        for path in sorted((output_root / "lean" / "items").glob("*/terminal.json"))
    }

    assert first["generation"]["calls_executed_this_invocation"] == 2
    assert first["trainer"]["record_count"] == 2
    assert len(provider_prompts) == 2
    assert len(lean_commands) == 2

    second = transforms.run_scale(config)

    assert second["generation"]["calls_executed_this_invocation"] == 0
    assert second["generation"]["terminals_reused_at_start"] == 2
    assert second["lean"]["terminals_reused_at_start"] == 2
    assert len(provider_prompts) == 2
    assert len(lean_commands) == 2
    assert (output_root / "records.jsonl").read_bytes() == first_records
    assert (output_root / "trainer_records.jsonl").read_bytes() == first_trainer
    assert all(path.read_bytes() == payload for path, payload in generation_terminals.items())
    assert all(path.read_bytes() == payload for path, payload in lean_terminals.items())


def test_lean_memory_limit_and_bisection_isolate_one_invalid_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocklist_path = _write_blocklist(tmp_path / "blocklist.json")
    mathlib_project = tmp_path / "mathlib"
    mathlib_project.mkdir()
    jobs = [_job(index) for index in range(3)]
    records = [
        _record(jobs[0], "(n : ℕ) : n = n ∧ True"),
        _record(jobs[1], "(n : ℕ) : BAD_CONST n = n"),
        _record(jobs[2], "(n : ℕ) : n + 0 = n"),
    ]
    invocations: list[tuple[list[str], dict[str, Any], str]] = []

    def fake_lean(command: list[str], **kwargs: Any) -> SimpleNamespace:
        source = Path(command[-1]).read_text(encoding="utf-8")
        invocations.append((command, kwargs, source))
        return SimpleNamespace(
            returncode=1 if "BAD_CONST" in source else 0,
            stdout="",
            stderr="unknown identifier BAD_CONST" if "BAD_CONST" in source else "",
        )

    monkeypatch.setattr(transforms.subprocess, "run", fake_lean)
    config = transforms.ScaleConfig(
        output_root=tmp_path / "lean-output",
        blocklist_path=blocklist_path,
        mathlib_project=mathlib_project,
        lean_batch_size=3,
        lean_memory_mb=24_576,
        lean_timeout=41,
        enforce_storage_root=False,
    )

    results, reused = transforms.run_lean_checks(config, jobs, records)

    assert reused == 0
    assert [result.status for result in results] == ["valid", "invalid", "valid"]
    assert len(invocations) == 5
    for command, kwargs, source in invocations:
        assert command[:5] == ["lake", "env", "lean", "-M", "24576"]
        assert kwargs["cwd"] == mathlib_project
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 41
        assert source.startswith("import Mathlib\n")


def test_contextual_lean_source_replaces_original_declaration_with_unique_name(
    tmp_path: Path,
) -> None:
    mathlib_project = tmp_path / "mathlib"
    source_path = mathlib_project / "Mathlib" / "Fixture" / "Context.lean"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "import Mathlib\n\nnamespace Fixture\n\nvariable (P Q : Prop)\n\n"
        "theorem original (h : P) : P := h\n\nend Fixture\n",
        encoding="utf-8",
    )
    source = transforms.SourceStatement(
        statement_id="repr::context",
        content_hash="fixture-hash",
        headless="(P Q : Prop) (h : P) : P",
        theorem_id="mathlib::context",
        group_key="mathlib::context",
        source_file="Mathlib/Fixture/Context.lean",
        source_range_start=7,
    )
    job = _job(0, source)
    rewritten = "(P Q : Prop) (h : P) : P ∧ True"
    candidate = transforms._LeanCandidate(
        job=job,
        statement=rewritten,
        candidate_sha256=hashlib.sha256(rewritten.encode()).hexdigest(),
        near_dup_hash=signature_near_dup_hash(rewritten),
        candidate_blocked=False,
    )
    config = transforms.ScaleConfig(
        output_root=tmp_path / "output",
        mathlib_project=mathlib_project,
        enforce_storage_root=False,
    )

    generated = transforms._lean_source_bytes(config, [candidate]).decode()

    assert generated.startswith("import Mathlib\n\nnamespace Fixture\n\nvariable (P Q : Prop)\n\n")
    assert "theorem original" not in generated
    assert f"theorem {transforms._lean_theorem_name(job)} {rewritten} := by" in generated
    assert generated.endswith("\n  sorry\n")


def test_corrupted_lean_batch_source_is_rejected_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocklist_path = _write_blocklist(tmp_path / "blocklist.json")
    mathlib_project = tmp_path / "mathlib"
    mathlib_project.mkdir()
    job = _job(0)
    record = _record(job, "(n : ℕ) : n = n ∧ True")
    lean_calls = 0

    def fake_lean(_command: list[str], **_kwargs: Any) -> SimpleNamespace:
        nonlocal lean_calls
        lean_calls += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transforms.subprocess, "run", fake_lean)
    config = transforms.ScaleConfig(
        output_root=tmp_path / "lean-output",
        blocklist_path=blocklist_path,
        mathlib_project=mathlib_project,
        lean_batch_size=1,
        lean_memory_mb=24_576,
        enforce_storage_root=False,
    )
    first, reused = transforms.run_lean_checks(config, [job], [record])
    assert reused == 0
    assert first[0].status == "valid"
    assert lean_calls == 1
    source_path = Path(str(first[0].batch_source_path))
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "\n-- corrupted after validation\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Lean terminal source/config mismatch"):
        transforms.run_lean_checks(config, [job], [record])

    assert lean_calls == 1


def test_trainer_export_uses_strict_schema_and_admission_filters() -> None:
    jobs = [_job(index) for index in range(4)]
    records = [
        _record(jobs[0], "(n : ℕ) : n = n ∧ True"),
        _record(jobs[1], "(n : ℕ) : n = n ∧ False"),
        _record(
            jobs[2],
            "(n : ℕ) : n + 0 = n",
            family_matches_assignment=False,
        ),
        _record(jobs[3], "(n : ℕ) : n = n ∨ False"),
    ]
    lean_results = [
        transforms.LeanCheckResult(job_id=jobs[0].job_id, candidate_sha256="a", status="valid"),
        transforms.LeanCheckResult(job_id=jobs[1].job_id, candidate_sha256="b", status="valid"),
        transforms.LeanCheckResult(job_id=jobs[2].job_id, candidate_sha256="c", status="valid"),
        transforms.LeanCheckResult(
            job_id=jobs[3].job_id,
            candidate_sha256="d",
            status="valid",
            candidate_blocked=True,
        ),
    ]

    rows = transforms.build_trainer_records(jobs, records, lean_results)

    assert len(rows) == 2
    assert [row["label"] for row in rows] == [True, False]
    assert all(type(row["label"]) is bool for row in rows)
    assert [row["family"] for row in rows] == [jobs[0].family.family_id, jobs[1].family.family_id]
    assert [row["group_key"] for row in rows] == [
        jobs[0].statement.group_key,
        jobs[1].statement.group_key,
    ]
    assert all(row["source"] == "d3_codex_scale_v1" for row in rows)
    assert len({row["record_id"] for row in rows}) == 2
    assert all(row["record_id"].startswith("d3:") for row in rows)
    assert [TrainingRecord.model_validate(row).model_dump(mode="json") for row in rows] == rows
