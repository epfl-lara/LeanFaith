#!/usr/bin/env bash
set -euo pipefail

: "${LEANFAITH_CODE_ROOT:?set LEANFAITH_CODE_ROOT to the clean pinned checkout}"
: "${LEANFAITH_EXPECTED_COMMIT:?set LEANFAITH_EXPECTED_COMMIT to its full 40-hex SHA}"
: "${LEANFAITH_COMPOSITION_SMOKE_SOURCE:?set the exact 64-row smoke source directory}"
: "${LEANFAITH_LEAN_PROJECT:?set the clean pinned Lean project directory}"
: "${LEANFAITH_COMPOSITION_SMOKE_OUTPUT:?set the new/restartable smoke output root}"

python_bin="${LEANFAITH_PYTHON:-${LEANFAITH_CODE_ROOT}/.venv/bin/python}"
args=(
  -m leanfaith.cli.app run-deterministic-v2-composition-smokes
  --code-root "${LEANFAITH_CODE_ROOT}"
  --expected-commit "${LEANFAITH_EXPECTED_COMMIT}"
  --source-dir "${LEANFAITH_COMPOSITION_SMOKE_SOURCE}"
  --project-dir "${LEANFAITH_LEAN_PROJECT}"
  --output-root "${LEANFAITH_COMPOSITION_SMOKE_OUTPUT}"
)

if [[ -n "${LEANFAITH_REUSE_P14_ROOT:-}" ]]; then
  : "${LEANFAITH_REUSE_P14_PRODUCER_COMMIT:?required with LEANFAITH_REUSE_P14_ROOT}"
  args+=(
    --reuse-root "p14=${LEANFAITH_REUSE_P14_ROOT}"
    --reuse-root-producer-commit "p14=${LEANFAITH_REUSE_P14_PRODUCER_COMMIT}"
  )
fi

export PYTHONPATH="${LEANFAITH_CODE_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" "${args[@]}"
