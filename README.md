# LeanFaith

*Learning a Calibrated Faithfulness Metric for Lean 4 Autoformalization.*

LeanFaith builds a calibrated learned metric that judges whether a candidate Lean 4
theorem statement faithfully expresses the same mathematical claim as a
natural-language statement or a trusted reference formalization — a stricter target
than truth-level logical equivalence.

[PLAN.md](PLAN.md) is the authoritative specification (revision 4.0). Read it before
contributing: §7 is the single path authority, §25 is the coding-agent operating
contract, and §26 is the ordered implementation backlog.

## Development setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                    # create .venv and install pinned dependencies
uv run leanfaith --help
```

Install the git hooks once per clone:

```bash
uv run pre-commit install
```

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

All four must pass in a clean checkout (PLAN.md §26, LF-001 acceptance).

## Secrets

Copy `.env.example` names into your environment or secret manager; never commit
values. `HF_TOKEN` is required for the private `formalmathatepfl/sft_classic`
dataset probe (PLAN.md §9.2).

## Status

- **Done:** LF-001 — repository scaffold and tooling.
- **Next:** LF-002 (config loader) per PLAN.md §26; Phase 0 (Gate 0) locks
  policies, sources, providers, and the Lean/mathlib toolchain before any data
  generation.
