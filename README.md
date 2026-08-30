# LeanFaith

*A lightweight, calibrated, and reference-aware metric for autoformalization
faithfulness.*

LeanFaith builds a calibrated learned metric that judges whether a candidate Lean 4
theorem statement faithfully expresses the same mathematical claim as a
natural-language statement or a trusted reference formalization — a stricter target
than truth-level logical equivalence.

[PLAN.md](PLAN.md) is the approved value-first data plan. Work is split into independent session
briefs in [plans/README.md](plans/README.md), with shared schemas, semantic labels, Lean budgets,
caching, and publication rules in
[plans/00_shared_contracts.md](plans/00_shared_contracts.md). Every contributor or agent must read
[AGENTS.md](AGENTS.md) and claim exactly one task brief before changing code.

The current priority is dataset preparation and evaluation, not full training. CPT1, CPT2, SFT1,
and SFT2 are separately versioned ablations; evaluation is mandatory. SFT1 bulk generation is
paused until the user reviews and approves the preserving/breaking transform catalog.

Historical note: the immediately preceding refocus execution ledger lives at
`docs/archive/PLAN-2026-08-30-refocus-v3.md`. The earlier 4,578-line implementation plan lives at
`docs/archive/PLAN-2026-08-frozen.md`, its 660-line status log at
`docs/archive/README-status-2026-08.md`, and the shelved human-annotation campaign under
`docs/archive/annotation/`. The `reports/` tree predating the refocus is likewise historical —
kept for provenance, not a guide to current work.

## Development setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev --group local-inference  # preserve shared training/inference dependencies
uv run leanfaith --help
```

Do not run plain `uv sync` in a shared checkout: its exact synchronization can remove the optional
Torch/Transformers group required by SFT2/TRAIN sessions.

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
uv run leanfaith-plans
```

All five must pass in a clean checkout.

## Secrets

Copy `.env.example` names into your environment or secret manager; never commit values or print
them in logs. `HF_TOKEN` is required for private Hugging Face inputs and private-first outputs under
`Lemmy00`. The owner's 2026-08-30 authorization for `formalmathatepfl/*` research use is recorded
in `policies/source_use_v2.yaml`; task-specific source and external-model rules still apply.
See [policies/README.md](policies/README.md) for active-versus-historical policy authority.
