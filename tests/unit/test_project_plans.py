from pathlib import Path

from leanfaith.project_plans import discover_task_briefs, validate_repository, validate_task_text

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_repository_plan_contract_is_complete() -> None:
    assert validate_repository(REPOSITORY_ROOT) == []
    assert "02_goal_v1.md" in discover_task_briefs(REPOSITORY_ROOT / "plans")


def test_task_validator_rejects_missing_contract() -> None:
    task, errors = validate_task_text(
        "broken.md",
        """
# Broken

> **Task ID:** BROKEN
> **Status:** surprising
""",
    )

    assert task is None
    assert any("missing metadata" in error for error in errors)
    assert any("unsupported status" in error for error in errors)


def test_later_blockquote_does_not_override_header_metadata() -> None:
    text = (REPOSITORY_ROOT / "plans/10_cpt1.md").read_text()
    original_task, original_errors = validate_task_text("10_cpt1.md", text)
    task, errors = validate_task_text(
        "10_cpt1.md",
        f"{text}\n\n> **Status:** surprising\n> **Owner/session:** injected\n",
    )

    assert original_errors == []
    assert original_task is not None
    assert errors == []
    assert task is not None
    assert task.status == original_task.status
    assert task.owner == original_task.owner


def test_nonempty_hf_destination_must_be_private_lemmy00() -> None:
    text = (
        (REPOSITORY_ROOT / "plans/10_cpt1.md")
        .read_text()
        .replace(
            "private `Lemmy00/leanfaith-cpt1-v1`",
            "public `someone-else/leanfaith-cpt1-v1`",
        )
    )
    task, errors = validate_task_text("10_cpt1.md", text)

    assert task is not None
    assert any("Lemmy00 namespace" in error for error in errors)
    assert any("private-first" in error for error in errors)
