# P12 v1.1 scale materialization

**Status:** complete, replay-verified, and provisional only

P12 v1.1 was executed from detached commit
`5f0a12f27d14b453e1144a8666e4b717a50e22fe` over the exact frozen private
5,000-statement and public 27,786-statement inputs. It changes a root proof
arrow into an explicit unused proof binder, or the exact inverse, only when a
narrow source-visible proposition grammar applies.

## Results

| Result | Private 5,000 | Public 27,786 | Total |
|---|---:|---:|---:|
| syntactically applicable | 89 | 278 | 367 |
| provisional records passing Lean and all E0 identity audits | 82 | 99 | **181** |
| distinct source/candidate text pairs | 82 | 97 | **179** |
| audit-quarantined | 5 | 101 | 106 |
| candidate-invalid terminal status | 2 | 78 | 80 |
| not applicable | 4,911 | 27,508 | 32,419 |

The public invalid group contains 76 `lean_invalid` outcomes and two
`lean_crash` infrastructure outcomes. The crashes remain explicit failures;
they are not semantic negatives. The public quarantine group contains 97
complete alpha/semantic mismatch failures and four candidates that also lacked
candidate structural views. The private quarantine group contains five
complete alpha/semantic mismatch failures.

Every accepted pair passed same-context LeanInteract re-elaboration, exact
inverse replay, alpha-canonical theorem-type identity, and semantic-atom
identity. All outputs remain provisional, with zero resolved labels, zero
promoted items, and `training_eligible=false`.

The 181 accepted records contain 179 distinct source/candidate text pairs. Two
public statement pairs occur under both `Real` and `unitInterval` namespaces;
their records and lineages remain separate, while the text-level duplicate
count is reported explicitly.

## Reproducibility

| Artifact | SHA-256 |
|---|---|
| private manifest file | `d77de74d6565ac5658c436cb88ee03795efc36584947bb791f1880eff9c025de` |
| private results | `ffe6eaa82b1425690a06f18ffe27e32097c0678c796e406f09fa43d72b17f0b1` |
| private run specification | `c3166bdfc1972933548b3d1231b4fca9ff6f284a5007b67ebbc18e3c7dae0673` |
| public manifest file | `e87b41d85c7b110ed5b4921826d46cffd364bf026dd135ab6509d4ea612ea6e5` |
| public results | `5b8fb82ad80361251cd79b464d4a8819a3de5fd9ca6b84ca208a56e681f2b4af` |
| public run specification | `c1d33449528e5241d237ae73b2a0487c52580e2d0e50d611fa0b947b99c2b48b` |
| fail-closed replay verifier | `67a62cfd1432165bbd6690219f6353c53355ff7b218464c33be7dc852d5f059b` |
| replay log | `e09f76e577aaa02a35732122d27bc850fe8abee9e80ee79de2404720f0bd315d` |

The public job was intentionally interrupted at persisted batch boundaries to
reduce Lean REPL memory pressure: four workers were reduced to two and then
one. Worker count is an execution control and is not part of the immutable run
specification. All 218 journals remained bound to the same run, and the final
one-worker completion produced 27,786 terminal results. A subsequent complete
resume replay returned exit code zero and reproduced both corpora's manifest,
results, and run-specification hashes byte for byte.

The machine-readable companion report contains the exact status, failure,
launcher, source, and artifact bindings. It commits no private theorem text.
