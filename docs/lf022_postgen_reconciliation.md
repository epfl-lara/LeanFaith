# LF-022 post-generation reconciliation

`run-lf022-public-batch` deliberately distinguishes provider/executor terminals
from orchestration exceptions. An `executor_rejected` journal event is not a
terminal and cannot be counted as one. Before exact offline replay or downstream
full-batch checking, run:

```bash
uv run leanfaith reconcile-lf022-postgen \
  --root /path/to/execution-repository \
  --manifest /path/to/batch_manifest.json \
  --output-root /storage/outside/the/execution/repository \
  --require-offline-ready
```

The output root must remain outside the execution repository so reconciliation
evidence cannot change the frozen executor code-state hash. The command performs
no network calls and writes a content-addressed directory containing:

- `reconciliation.json`: an exact disjoint partition of every frozen task into
  `terminal_task_ids`, `error_task_ids`, and `missing_task_ids`;
- `retry_plan.json`, only when nonterminal tasks exist, preserving each task's
  original proposer family, model, and execution scope;
- `terminal_selector.json`, when at least one terminal exists, binding only
  verified terminal tasks, the exact canonical terminal journal events, the
  complete journal snapshot projection, and frozen task/terminal hashes.

Exit code `3` with `--require-offline-ready` means a live resume is required.
It is not an offline-replay failure and must not be bypassed by changing an
error into a provider terminal. A separate operator action must bind the exact
`retry_plan_id` and explicitly enable live retry. A later genuine terminal may
coexist with a historic error journal event; `historic_error_task_ids` preserves
that history.

## Incremental mechanical checking

A terminal selector is a safe snapshot while a larger frozen batch is still in
progress. It does not change the 9,207-task generation contract. The pooled Lean
checker accepts it with:

```bash
uv run leanfaith check-lf022-provisional-lean \
  --project mathlib=/path/to/mathlib \
  --input-root /path/to/execution-repository/data/lf022_execution \
  --output-root /storage/checks/<selector-id> \
  --postgen-selector /storage/reconciliation/<id>/terminal_selector.json \
  --expected-postgen-selector-id lf022_postgen_terminal_selector:<digest> \
  --root /path/to/execution-repository
```

Before deriving an output directory from a selector, verify it explicitly:

```bash
uv run leanfaith verify-lf022-postgen-selector \
  --root /path/to/execution-repository \
  --selector /storage/reconciliation/<id>/terminal_selector.json
```

On success this command prints only the verified selector content ID. It
hash/canonical-verifies the complete batch manifest and freeze request, every
route admission, every manifest task-binding identity, and each selected frozen
task. It deliberately does not parse unrelated task bodies or replay today's
public-pool, authorization, and denylist inputs. For every selected task it
reconstructs the exact prompt and preflight and verifies the complete persisted
attempt, provider-request, wire-request/response, provider-raw, generic-LLM,
parsed-output, provisional-variant, and terminal lineage under the current
verifier. The original exhaustive verifier remains available for callers that
need full current-policy replay; admission-bound historical-code replay remains
a separate, stronger whole-batch audit.
The Lean checker additionally requires `--input-root` to equal the batch's
frozen executor output root, requires the replayed selector to equal the
explicit ID used to choose its output directory, rechecks terminal hashes
during discovery, and rejects symlinked output-path components.
Missing and error tasks are excluded rather than reclassified. Results remain
mechanical, provisional, label-free, and ineligible for
training/evaluation/gate credit.

The selected-only verifier is read-only. The selector file/path/hash are also
captured after verification and checked again immediately before the Lean-check
manifest is written.

## Qwen 9,207 operational launchers

The versioned launchers under `/storage/milikic/leanfaith` are:

- `launch_qwen_full_postgen_v2.sh`: reconciles first and refuses offline replay
  unless all 9,207 tasks have verified terminals, then verifies and consumes
  the freshly reconciled terminal selector;
- `launch_qwen_nonterminal_retry_v1.sh`: deliberately exits `42` without live
  execution until an exact retry-plan verifier exists;
- `launch_qwen_incremental_leancheck_v1.sh`: consumes one exact terminal selector
  without provider access. It verifies the selector before creating any
  selector-derived output directory; verification failures go only to a fixed
  launcher-failure root.

Reviewed launcher SHA-256 bindings for this revision are:

```text
launch_qwen_full_postgen_v2.sh          4d8697208acf5c2607352f256c98a4c38c57228f4570908bef01c3e8a1f5a815
launch_qwen_nonterminal_retry_v1.sh     3e9eee01c3216bba73214cbf0c79e7b36863429e0ae5befc3cf71356910f69ed
launch_qwen_incremental_leancheck_v1.sh 5462a319260ec079da437375711768dbce32a1117269ad06d21dac4670750092
```

Old launchers are retained for forensic comparison. The new launchers do not
modify the frozen batch manifest or its task files.
