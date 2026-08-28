# LeanFaith

*A lightweight, calibrated, and reference-aware metric for autoformalization
faithfulness.*

LeanFaith builds a calibrated learned metric that judges whether a candidate Lean 4
theorem statement faithfully expresses the same mathematical claim as a
natural-language statement or a trusted reference formalization — a stricter target
than truth-level logical equivalence.

[PLAN.md](PLAN.md) is the living plan (refocus v3, approved 2026-08-28): tracks A (evaluation),
B (repo surgery), D (data engine), T (staged S0–S3 training), with
[TRANSFORM_CATALOG_V2.md](TRANSFORM_CATALOG_V2.md) as the deterministic-transform design of
record. Read PLAN.md before contributing.

Historical note: the pre-refocus 4,578-line plan lives at
`docs/archive/PLAN-2026-08-frozen.md`, its 660-line status log at
`docs/archive/README-status-2026-08.md`, and the shelved human-annotation campaign under
`docs/archive/annotation/`. The `reports/` tree predating the refocus is likewise historical —
kept for provenance, not a guide to current work.

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

All four must pass in a clean checkout.

## Secrets

Copy `.env.example` names into your environment or secret manager; never commit
values. `HF_TOKEN` is required for the private `formalmathatepfl/sft_classic`
dataset (internal-research-only: its content must never be sent to external LLM
APIs; deterministic transforms on it are allowed).
