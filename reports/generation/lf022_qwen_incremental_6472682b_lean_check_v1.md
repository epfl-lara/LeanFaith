# LF-022 Qwen incremental Lean-check milestone

## Outcome

An exact, selector-bound snapshot of the live Qwen generation run has completed
mechanical Lean validation. The snapshot contains **1,019 generated candidate
statements**:

| Outcome | Count | Share |
|---|---:|---:|
| Elaborates with a proof placeholder | 718 | 70.46% |
| Invalid Lean statement | 301 | 29.54% |
| Infrastructure failure | 0 | 0% |

All 1,019 variant IDs and all 1,019 candidate-code hashes are unique within
this snapshot. The check ran from `2026-08-11T09:46:38Z` through
`2026-08-11T10:04:49Z` with one LeanInteract worker.

“Elaborates with a proof placeholder” means Lean accepts the candidate as a
well-formed theorem statement after the checker supplies `by sorry`. It does
**not** mean the theorem is true or that it preserves the reference claim.

## Snapshot boundary

The frozen selector contains 1,046 terminal generation tasks:

- 1,019 `provisional_variants_created` tasks, which produced the checked
  candidates;
- 21 `provider_exhausted` tasks;
- 5 `transport_unknown` tasks;
- 1 `proposer_parse_failed` task.

The other 8,161 tasks in the planned 9,207-task generation run were still
nonterminal when this point-in-time selector was frozen: 8,160 had no journal
terminal and one had an executor-error event. The live Qwen process continues
independently; this report does not claim that generation is complete.

## New data versus the historical LF-022 check pool

The snapshot was compared by exact `variant_id` with the earlier mixed-model
mechanical-check artifact:

| Comparison | Count |
|---|---:|
| Variants already mechanically checked | 438 |
| Variants new to that check pool | 581 |
| Lean-valid variants already present in the historical Codex findings | 309 |
| Lean-valid variants not previously present in those findings | 409 |

The overlap is retained deliberately in the full Codex audit: repeated
judgments provide a consistency signal, while the 409 previously unaudited
Lean-valid variants expand the judged pool. Dataset freezing will deduplicate
exact variants and normalize ancestry weights before training.

## Bound artifacts

| Artifact | SHA-256 |
|---|---|
| Terminal selector | `2491b4175dc9441b9028b8e422908c5d9c58e6acfac73440b78470e503c274a7` |
| Reconciliation report | `b641fd80f5c57f924bf799a48d93687e81ecbec24ffe1c263ae02e0ce2fbba2d` |
| `checks.jsonl` | `b53736c22fb7cc134081e33a9a4f7e5e1724d0e3f170b6930b05658ef0a2d6c4` |
| Lean-check manifest | `f106d1a299c89eca5b5c82df562bb6522b42ca1de79b9d2caa91dd1a042d65e9` |
| Lean-check launcher | `b949f57723d9c712b369d338db7d8df115edde6838dbc2fffa2b9d598dd7474b` |
| Historical mixed-model checks | `657ec8e8f0b5ec6557b138a06608e998a26bead8fbd7ac6cd8415c586b43cd92` |
| Historical Codex findings | `69f37377902c5328bb0fa87e8dd014f3f19be095a228f2b3d3d805dcef451824` |

The selector ID is
`lf022_postgen_terminal_selector:6472682bd2de081e9b007d82eb374556b2fb95ec29f0ea2c39ab2f42ca1582ee`.
The code snapshot is commit
`0e8d84c409098aa94eb8666a5d82259eeda2cb6a`.

## Follow-on work already running

- A one-example `gpt-5.6-sol`/`xhigh` Codex judgment completed successfully.
  The same immutable audit root is now processing all 718 Lean-valid
  candidates. These remain audit opinions, not human or silver labels.
- The deterministic equality-family scale queue started after the checker
  released its Lean memory. It is running private P18, private N18, public P18,
  and public N18 sequentially with one LeanInteract worker and no hard process
  memory cap.
- The live 9,207-task Qwen generation job remains active.

No semantic label, training record, evaluation item, or gate credit is created
by this milestone.
