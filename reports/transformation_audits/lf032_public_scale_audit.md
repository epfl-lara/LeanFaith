# LF-032 public deterministic-v2 scale audit

**Audit date:** 2026-08-11

**Disposition:** completed public exploratory materialization; all retained pairs
remain provisional and ineligible for training, evaluation, labels, or gate credit

LF-032 materialized the first conservative deterministic-v2 families over the
frozen public mathlib corpus of 27,786 theorem statements. Each source was
tested against all six enabled families, producing 166,716 terminal results.
The run used LeanInteract for same-context elaboration and then applied the
family-specific inverse, whole-type identity, fingerprint, semantic-atom, and
representation audits required by the v2 contract.

## Frozen run

Artifact root:

```text
/storage/milikic/leanfaith/deterministic_v2/
  run_e2f071a_lf032_public_exploratory_v1/full_27786
```

| Check | Observed value |
|---|---:|
| source statements | 27,786 |
| enabled families per statement | 6 |
| result rows | 166,716 |
| provisional variants | 266 |
| audit quarantines | 6 |
| invalid candidates | 1 |
| not applicable | 166,443 |

The recorded result count equals `27,786 × 6`. The physical JSONL line count
equals 166,716, and its SHA-256 equals the manifest value:

```text
3f220f901e29e0cb1becf7578cb35cd029338cf46696324539ba39bf2aef9462
```

Family-level yield before replaying the six quarantines was:

| Family | Provisional | Quarantined | Invalid | Not applicable |
|---|---:|---:|---:|---:|
| P06 implicit arguments | 0 | 0 | 0 | 27,786 |
| P07 coercion surface | 0 | 0 | 0 | 27,786 |
| P09 projections | 2 | 0 | 0 | 27,784 |
| P10 constructors | 0 | 0 | 0 | 27,786 |
| P11 bounded quantifiers | 264 | 6 | 1 | 27,515 |
| P12 proof-arrow binder | 0 | 0 | 0 | 27,786 |

The zero-yield families are retained as negative coverage findings rather than
being weakened to manufacture examples. They remain useful registry stubs for
corpora whose surface syntax exposes those mechanisms.

## Fail-closed replay of quarantines

The six P11 quarantines exposed two replay-environment reconstruction defects:

1. a declaration originating inside a top-level `module` command must use its
   corresponding public lookup name when the proof-stripped inline replay drops
   module-private name mangling; and
2. `meta import` and `public meta import` commands must be hoisted with ordinary
   imports when reconstructing the inline environment.

Both fixes are covered by unit tests and a live LeanInteract module-private
fixture. The exact six-item replay is stored at:

```text
/storage/milikic/leanfaith/deterministic_v2/
  run_e2f071a_lf032_public_exploratory_v1/
  private_fix_replay_all/output_meta_fix
```

Its 36 result rows have SHA-256:

```text
0195037de852ad65f8326da9cfa0d919ce2527ca12aae97f5212198c66ca0851
```

Four of the original six P11 quarantines became clean provisional variants.
The final unique clean yield is therefore **270 pairs**: 2 from P09 and 268
from P11. The remaining P11 opportunities are two quarantines and one invalid
candidate.

The final two quarantines are intentional. Their source statements refer to a
module-private helper constant. Dropping module mode during inline replay
changes that helper to a public constant with a different kernel identity.
Although the presentation rewrite appears plausible, the alpha-identity and
semantic-atom audits correctly refuse to certify it. The single invalid
candidate is likewise retained as `candidate_invalid`; neither category is
converted into a semantic label.

## Credit boundary

This audit establishes executable yield and validates the fail-closed
materializer. It does not promote any transformation family and does not claim
F1 labels. Both manifests explicitly record:

```text
promoted_item_count = 0
resolved_label_count = 0
training_eligible = false
```

Promotion, training admission, and evaluation admission remain separate later
decisions under the existing family-audit and label-resolution policies.
