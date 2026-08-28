"""End-to-end file contract and resume behavior for the collect2 batch."""

from __future__ import annotations

import json
from pathlib import Path

from leanfaith.collect2.invoke import InvocationResult, ProviderSpec, resolve_local_profile
from leanfaith.collect2.pipeline import BatchTask, run_batch


def _blocklist(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": ["golden_blocklist_v1"],
                "near_dup_hashes": [],
                "group_keys": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_run_batch_writes_records_raw_manifest_and_resumes(tmp_path: Path) -> None:
    provider = ProviderSpec(kind="local_hf", model="stepfun")
    tasks = [
        BatchTask("problem-one", "One equals one.", "import Mathlib"),
        BatchTask("problem-two", "Two equals two.", "import Mathlib"),
    ]
    calls = 0

    def fake_invoke(rendered: object, spec: ProviderSpec) -> InvocationResult:
        nonlocal calls
        calls += 1
        from leanfaith.collect2.invoke import RenderedAutoformalizationTask

        assert isinstance(rendered, RenderedAutoformalizationTask)
        numeral = calls
        raw = (
            "</think>```Lean4\n"
            "import Mathlib\n"
            f"theorem {rendered.task.theorem_name} : {numeral} = {numeral} := by sorry\n"
            "```"
        )
        return InvocationResult(
            provider=spec.provider_label,
            model=resolve_local_profile(spec.model).repo_id,
            prompt=rendered.prompt,
            raw_output=raw,
            candidate_output=raw,
        )

    output = tmp_path / "run"
    result = run_batch(
        tasks,
        provider=provider,
        output_dir=output,
        blocklist_path=_blocklist(tmp_path / "blocklist.json"),
        repo_root=Path.cwd(),
        invoke_one=fake_invoke,
    )
    assert (result.accepted, result.rejected, result.resumed) == (2, 0, 0)
    records = [json.loads(line) for line in result.records_path.read_text().splitlines()]
    assert len(records) == 2
    assert set(records[0]) == {
        "problem_id",
        "provider",
        "model",
        "candidate_lean",
        "candidate_headless",
        "generator_prompt_sha256",
        "raw_output_path",
        "blocklist_screened",
    }
    assert all(record["blocklist_screened"] is True for record in records)
    assert all((output / record["raw_output_path"]).is_file() for record in records)
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["regime"] == "plain run manifest; no attestation or gates"
    assert manifest["counts"]["accepted_this_run"] == 2
    assert manifest["golden_blocklist"]["group_key_count"] == 0

    def must_not_invoke(rendered: object, spec: ProviderSpec) -> InvocationResult:
        raise AssertionError(f"resume invoked provider for {rendered!r}, {spec!r}")

    resumed = run_batch(
        tasks,
        provider=provider,
        output_dir=output,
        blocklist_path=tmp_path / "blocklist.json",
        repo_root=Path.cwd(),
        invoke_one=must_not_invoke,
    )
    assert (resumed.accepted, resumed.rejected, resumed.resumed) == (0, 0, 2)
    assert len(resumed.records_path.read_text().splitlines()) == 2
