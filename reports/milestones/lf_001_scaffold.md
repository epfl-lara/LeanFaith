# LF-001 — Repository scaffold and tooling

**Date:** 2026-07-10
**Scope (PLAN.md §26):** pyproject/uv/Typer/Ruff/Pytest/mypy/pre-commit; strict core modules.

## Delivered

- `pyproject.toml`: `requires-python ">=3.12,<3.13"`, runtime deps `lean-interact==0.11.4`,
  `pydantic>=2.7,<3`, `typer>=0.12`; dev group `pytest/mypy/ruff/pre-commit`;
  ruff (line-length 100, py312, `E,F,W,I,UP,B,C4,SIM,RUF`), mypy `strict = true`
  over the `leanfaith` package, pytest `testpaths = ["tests"]`.
- `uv.lock`: 45 packages resolved with uv 0.11.28 against CPython 3.12.
- Package skeleton per §7 tree: `src/leanfaith/__init__.py` (`__version__`),
  `src/leanfaith/py.typed`, `src/leanfaith/cli/{__init__.py,app.py}` with the
  `leanfaith` Typer entry point (`[project.scripts]`) and a `version` command.
- `tests/unit/test_scaffold.py`: import/version, CLI `--help`, CLI `version`,
  and an unknown-command failure path.
- `.pre-commit-config.yaml`: pre-commit-hooks v6.0.0 (whitespace with
  `--markdown-linebreak-ext=md`, EOF, yaml/toml, large files, merge conflicts)
  and ruff-pre-commit v0.15.21 (`ruff-check --fix`, `ruff-format`).
- `README.md` (dev setup + contributor instructions), `.env.example`
  (`HF_TOKEN`, `WANDB_API_KEY` names only), `.gitignore` additions for
  `data/`, `artifacts/`, `runs/`.

## Acceptance evidence (clean checkout)

```text
uv sync                      → ok (CPython 3.12, 45 packages)
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 4 files already formatted
uv run mypy                  → Success: no issues found in 3 source files
uv run pytest                → 4 passed
uv run leanfaith version     → 0.1.0
```

Failure path tested: unknown CLI command exits nonzero.

## Notes / deviations

- No CI workflow was added: rev 4.0's LF-001 does not list CI, and `.github/`
  is not a declared path in the §7 tree. Wiring the §27.2 CI tiers requires a
  one-line tree addition first (path-authority rule, §7).
- No run manifest: LF-001 has no pipeline command; manifest-writing lands with
  LF-003.

**Next:** LF-002 — config loader (strict schemas, hashes, secret references
including `HF_TOKEN`, unknown-key failure).
