"""Cheap, network-free validation for the parallel task handoff documents."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REQUIRED_TASK_BRIEFS: tuple[str, ...] = (
    "02_goal_v1.md",
    "05_existing_data_reuse.md",
    "10_cpt1.md",
    "20_cpt2.md",
    "30_sft1_deterministic.md",
    "40_sft2_llm_transforms.md",
    "50_sft2_autoformalizer.md",
    "60_eval_baselines.md",
    "70_training_ablations.md",
)

TASK_INVARIANTS: dict[str, tuple[str, ...]] = {
    "02_goal_v1.md": ("goal_v1.0", "elaborated|surface", "compile_context"),
    "05_existing_data_reuse.md": ("13,373", "27,327", "5,111"),
    "10_cpt1.md": (
        "formalmathatepfl/lean-docs",
        "question + answer",
        "golden_blocklist_v1.json",
        "Lemmy00/leanfaith-cpt1-v1",
    ),
    "20_cpt2.md": (
        "4,361,579",
        "isValid",
        "500-row",
        "theorem-string hash",
        "Lemmy00/leanfaith-cpt2-proof-validity-v1",
    ),
    "30_sft1_deterministic.md": (
        "Approval recorded",
        "5M pairs",
        "no per-pair Lean compilation",
        "0.70",
        "golden_blocklist_v1.json",
        "Lemmy00/leanfaith-sft1-deterministic-v1",
    ),
    "40_sft2_llm_transforms.md": (
        "proposer_intent+single_judge",
        "three total attempts",
        "blinded 10%",
        "Lemmy00/leanfaith-sft2-llm-transforms-v1",
    ),
    "50_sft2_autoformalizer.md": (
        "Codex, Lemex, and Claude",
        "unknown otherwise",
        "Invalid candidates",
        "Lemmy00/leanfaith-sft2-autoformalizer-v1",
    ),
    "60_eval_baselines.md": (
        "5,111",
        "2,555",
        "2,556",
        "expert_human",
        "auto_typecheck_fail",
        "no training split",
        "Lemmy00/leanfaith-eval-v2",
        "Lemmy00/leanfaith-eval-results-v2",
    ),
    "70_training_ablations.md": (
        "Ettin",
        "ModernBERT",
        "zero-Lean budget",
        "A100/H100",
    ),
}

ALLOWED_STATUSES = frozenset(
    {
        "not_started",
        "active",
        "waiting_user",
        "blocked",
        "pilot_ready",
        "pilot_passed",
        "scale_authorized",
        "scaling",
        "complete",
        "deferred",
    }
)

REQUIRED_METADATA = frozenset(
    {
        "task id",
        "status",
        "owner/session",
        "last updated",
        "dependencies",
        "next gate",
        "compute class",
        "lean budget",
        "local staging root",
    }
)

REQUIRED_SNIPPETS: tuple[str, ...] = (
    "Writable paths",
    "Lean is the bottleneck",
    "one-example",
    "## Acceptance criteria",
    "## Session kickoff prompt",
    "## Coordinator requests",
    "## Progress log (append-only)",
)

_METADATA_RE = re.compile(r"^> \*\*(?P<key>[^*]+):\*\*\s*(?P<value>.*)$", re.MULTILINE)


@dataclass(frozen=True)
class ParsedTaskPlan:
    """Small parsed surface used to find cross-file contract mistakes."""

    filename: str
    task_id: str
    status: str
    owner: str
    hf_destination: str
    staging_root: str


def _header_block(text: str) -> str:
    """Return only the first contiguous blockquote metadata block."""

    header: list[str] = []
    in_header = False
    for line in text.splitlines():
        if line.startswith(">"):
            in_header = True
            header.append(line)
        elif in_header:
            break
    return "\n".join(header)


def _metadata(text: str) -> dict[str, str]:
    return {
        match.group("key").strip().lower(): match.group("value").strip()
        for match in _METADATA_RE.finditer(_header_block(text))
    }


def discover_task_briefs(plans_dir: Path) -> tuple[str, ...]:
    """Discover task plans so newly added numeric briefs cannot evade validation."""

    return tuple(
        path.name
        for path in sorted(plans_dir.glob("[0-9][0-9]_*.md"))
        if path.name != "00_shared_contracts.md"
    )


def validate_task_text(filename: str, text: str) -> tuple[ParsedTaskPlan | None, list[str]]:
    """Validate one task brief without reading the filesystem."""

    errors: list[str] = []
    metadata = _metadata(text)
    missing_metadata = sorted(REQUIRED_METADATA - metadata.keys())
    if missing_metadata:
        errors.append(f"{filename}: missing metadata: {', '.join(missing_metadata)}")

    hf_key = "hf destination" if "hf destination" in metadata else "hf destinations"
    if hf_key not in metadata:
        errors.append(f"{filename}: missing metadata: hf destination(s)")

    for snippet in REQUIRED_SNIPPETS:
        if snippet.lower() not in text.lower():
            errors.append(f"{filename}: missing required contract text: {snippet!r}")

    status = metadata.get("status", "")
    if status and status not in ALLOWED_STATUSES:
        errors.append(f"{filename}: unsupported status {status!r}")

    task_id = metadata.get("task id", "")
    if task_id and not re.fullmatch(r"[A-Z][A-Z0-9-]*", task_id):
        errors.append(f"{filename}: invalid task ID {task_id!r}")

    owner = metadata.get("owner/session", "")
    if (
        status in {"active", "pilot_ready", "pilot_passed", "scale_authorized", "scaling"}
        and owner.lower() == "unassigned"
    ):
        errors.append(f"{filename}: status {status!r} requires an assigned owner/session")

    if filename == "30_sft1_deterministic.md":
        approval = metadata.get("approval recorded", "")
        if not approval:
            errors.append(f"{filename}: missing metadata: approval recorded")
        if status in {"scale_authorized", "scaling", "complete"} and approval.lower() == "pending":
            errors.append(f"{filename}: scale/complete status requires recorded transform approval")

    hf_destination = metadata.get(hf_key, "")
    has_no_destination = hf_destination.lower().startswith("none")
    if not has_no_destination:
        if "Lemmy00/" not in hf_destination:
            errors.append(f"{filename}: HF destination must use the Lemmy00 namespace")
        if "private" not in hf_destination.lower():
            errors.append(f"{filename}: HF destination must be explicitly private-first")

    if not task_id or not status or hf_key not in metadata:
        return None, errors

    return (
        ParsedTaskPlan(
            filename=filename,
            task_id=task_id,
            status=status,
            owner=owner,
            hf_destination=hf_destination,
            staging_root=metadata.get("local staging root", ""),
        ),
        errors,
    )


def validate_repository(root: Path) -> list[str]:
    """Validate the active plan hub and all independently owned task briefs."""

    errors: list[str] = []
    plans_dir = root / "plans"
    required_root_files = (
        root / "PLAN.md",
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "README.md",
        plans_dir / "README.md",
        plans_dir / "00_shared_contracts.md",
        plans_dir / "TASK_TEMPLATE.md",
        root / "docs/archive/PLAN-2026-08-30-refocus-v3.md",
    )
    for path in required_root_files:
        if not path.is_file():
            errors.append(f"missing handoff file: {path.relative_to(root)}")

    parsed: list[ParsedTaskPlan] = []
    index_path = plans_dir / "README.md"
    index_text = index_path.read_text() if index_path.is_file() else ""
    coordinator_text = (root / "PLAN.md").read_text() if (root / "PLAN.md").is_file() else ""

    discovered = discover_task_briefs(plans_dir)
    for filename in REQUIRED_TASK_BRIEFS:
        if filename not in discovered:
            errors.append(f"missing required task brief: plans/{filename}")

    for filename in discovered:
        path = plans_dir / filename
        text = path.read_text()
        task, task_errors = validate_task_text(filename, text)
        errors.extend(task_errors)
        for anchor in TASK_INVARIANTS.get(filename, ()):
            if anchor not in text:
                errors.append(f"{filename}: missing frozen invariant {anchor!r}")
        if task is not None:
            parsed.append(task)
        if filename not in index_text:
            errors.append(f"plans/README.md does not link {filename}")
        if filename not in coordinator_text:
            errors.append(f"PLAN.md does not link {filename}")

    task_ids = [task.task_id for task in parsed]
    duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicates:
        errors.append(f"duplicate task IDs: {', '.join(duplicates)}")

    staging_roots = [task.staging_root for task in parsed if task.staging_root]
    duplicate_roots = sorted(
        {staging_root for staging_root in staging_roots if staging_roots.count(staging_root) > 1}
    )
    if duplicate_roots:
        errors.append(f"duplicate local staging roots: {', '.join(duplicate_roots)}")

    template_path = plans_dir / "TASK_TEMPLATE.md"
    if template_path.is_file():
        _, template_errors = validate_task_text("TASK_TEMPLATE.md", template_path.read_text())
        errors.extend(template_errors)

    shared_path = plans_dir / "00_shared_contracts.md"
    if shared_path.is_file():
        shared_text = shared_path.read_text()
        for heading in (
            "## 2. Model-facing theorem representation",
            "## 3. Minimal schemas",
            "## 4. Label contracts",
            "## 6. Lean-efficiency contract",
            "## 8. Hugging Face release contract",
        ):
            if heading not in shared_text:
                errors.append(f"plans/00_shared_contracts.md missing heading: {heading}")

    return errors


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for local/pre-commit plan validation."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )
    args = parser.parse_args(argv)
    errors = validate_repository(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    task_count = len(discover_task_briefs(args.root.resolve() / "plans"))
    print(f"Plan contract OK: {task_count} task briefs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
