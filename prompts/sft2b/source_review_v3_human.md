# SFT2B v3 row-level source review instructions

Review every row independently and manually. The packet entry is the only authoritative review
input. A separate `automatic_dispositions.jsonl` file exists for provenance, but it is a
deterministic lexical-rule output—not a semantic judgment, human review, or expected verdict. Do
not copy its disposition or rationale as your own.

For each packet row, inspect all eight bound fields:

1. `nl_statement`
2. `reference_proposition`
3. `reference_theorem_id`
4. `reference_declaration_name`
5. `headless_signature`
6. `problem_identity`
7. `compile_context`
8. `provenance`

Decide whether the natural-language text is a standalone mathematical claim or problem and whether
the trusted Lean proposition formalizes that same intended claim. Quarantine solution/proof
fragments, answers presented without the problem, incomplete or context-dependent library
descriptions, mistranslations, mismatched claims, and other source-quality failures. Compilation or
proof-bearing provenance alone does not establish semantic alignment.

Allowed verdicts:

- `admit_standalone_aligned`
- `quarantine_solution_or_proof_fragment`
- `quarantine_incomplete_or_nonstandalone`
- `quarantine_misaligned`
- `quarantine_other_quality_failure`
- `needs_escalation`

Write one `sft2b_human_source_review_v3` JSON object per packet row. Copy the packet's
`packet_entry_id`, `source_id`, `reviewed_fields`, `reviewed_field_sha256`, and
`reviewed_source_sha256` exactly. Supply a stable, accountable human `reviewer_identity`, use
`reviewer_kind: "human"` and `method: "manual_row_level_source_alignment_v1"`, record a
timezone-aware UTC timestamp, write a row-specific rationale of at least 20 characters, and attest
`personally_reviewed_exact_fields: true`.

Compute `review_id` as the standard SFT2B stable ID with prefix `sft2b_human_review` over:

```json
{
  "packet_entry_id": "<copied packet_entry_id>",
  "source_id": "<copied source_id>",
  "reviewed_source_sha256": "<copied reviewed_source_sha256>",
  "reviewer_identity": "<accountable human identity>",
  "review_timestamp_utc": "<the parsed UTC datetime serialized with isoformat()>",
  "verdict": "<allowed verdict>"
}
```

`needs_escalation` records a real completed first review but does not clear the release gate. The
release verifier requires exactly one valid, fully bound human record for every packet row and zero
unresolved escalation verdicts. Opus, Terra, another model, deterministic rules, bulk scripts, or
agent-generated rationales do not satisfy this contract without an explicit contract change.
