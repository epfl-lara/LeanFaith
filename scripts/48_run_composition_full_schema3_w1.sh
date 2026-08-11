#!/usr/bin/env bash
set -euo pipefail

: "${LEANFAITH_CODE_ROOT:?set the clean pinned LeanFaith checkout}"
: "${LEANFAITH_EXPECTED_COMMIT:?set its exact full commit SHA}"
: "${LEANFAITH_COMPOSITION_FULL_SEED:?set the canonical 3,941-row seed directory}"
: "${LEANFAITH_LEAN_PROJECT:?set the clean pinned Lean project}"
: "${LEANFAITH_COMPOSITION_FULL_OUTPUT:?set the full-scale output root}"

python_bin="${LEANFAITH_PYTHON:-${LEANFAITH_CODE_ROOT}/.venv/bin/python}"
export PYTHONPATH="${LEANFAITH_CODE_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m leanfaith.cli.app \
  run-deterministic-v2-composition-full-scale \
  --code-root "${LEANFAITH_CODE_ROOT}" \
  --expected-commit "${LEANFAITH_EXPECTED_COMMIT}" \
  --seed-dir "${LEANFAITH_COMPOSITION_FULL_SEED}" \
  --project-dir "${LEANFAITH_LEAN_PROJECT}" \
  --output-root "${LEANFAITH_COMPOSITION_FULL_OUTPUT}"
