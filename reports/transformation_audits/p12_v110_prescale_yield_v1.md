# P12 v1.1 pre-scale yield probe

Status: **read-only opportunity probe; not a materialization, label, promotion, or training artifact**.

## Frozen inputs

### Internal-only `sft_classic`

- Source subset: the frozen 5,000-row Gate-3 subset.
- Subset manifest SHA-256: `e0600a5b5b8dc20d2983e66daef78aef18cd0cc9c652414a35263ea55f0ac43f`.
- Theorem partition SHA-256: `3241ea0ff7f7e80a27ea6deafe680043c8ac8e782db049dcc551c50441115c30`.
- Representation partition SHA-256: `c63bf8e2706d4fc3fff430bee920cb0c575b2947023a4141a4d0384f747cad24`.

### Public mathlib

- Source universe: the frozen 27,786-statement LF-022 public extraction.
- Extraction manifest SHA-256: `b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06`.
- Theorem partition SHA-256: `7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7`.

### Transformation binding

- P12 v1.1 execution-profile effective hash: `4389b297ce3cb84a77b1d44de4938cffc84015b9453b69df902c27f51e85ace0`.
- P12 v1.1 execution-profile file SHA-256: `d5faef8f349af50970d5c7276d6be9fbe3dce717980009c7b87bcf2768e241f4`.
- Version-addendum file SHA-256: `08fcb0fde487aed973ecb34b4a89c252f064b43074e4d6fe621b795386f4948c`.

The probe ran only the deterministic source matcher. It did not call Lean and did not emit candidate records.

## Results

| Measure | Private 5,000 | Public 27,786 |
|---|---:|---:|
| P12 v1.0 syntactically applicable | 0 | 0 |
| P12 v1.1 syntactically applicable | 89 | 278 |
| New P12 opportunities over v1.0 | 89 | 278 |
| `unterminated_quoted_token` fail-closed outcomes | 57 | 2,507 |
| `missing_conclusion_colon` fail-closed outcomes | 3 | 427 |
| `mismatched_delimiter` fail-closed outcomes | 0 | 311 |
| `unsupported_declaration_shape` fail-closed outcomes | 0 | 191 |

The maximum expected materialized yield is **89 private plus 278 public provisional variants**. These are upper-bound opportunity counts, not claims that all candidates will pass Lean re-elaboration and the complete E0 audit.

An earlier pre-review probe found 91 private matches. Two contained additional nested arrows inside the proposed proof domain. A later lexical review also removed two public sites whose domains contained unspaced or decorated arrow glyphs. The final matcher rejects every arrow glyph in the proposed domain before considering equality or another visible proposition operator, so those cases are absent from the authoritative counts above.

## Safety boundary

P12 v1.1 is a separate matcher version and profile. It does not change the old P12 v1.0 config bytes or effective profile hash. It accepts only an immediate root arrow whose left side is a declared `Prop` variable or has a source-visible proposition root such as equality, order, membership, a logical connective, negation, `True`, or `False`. It rejects data-function domains, every additional arrow glyph in the domain, dependent or used named binders, quantifier/control prefixes, arbitrary predicate applications without visible proposition evidence, Boolean `==`, pipeline operators, and decorated operators containing the arrow glyph.

Every emitted candidate must still independently re-elaborate through LeanInteract and pass exact inverse replay, full alpha-canonical theorem-type identity, and semantic-atom identity. Successful outputs remain `provisional`, with zero resolved labels, zero promoted items, and `training_eligible=false`.
