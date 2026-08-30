Think step by step to translate the mathematical statement below to Lean 4 and verify that the
formalization preserves the exact intended claim.

The target environment is Lean 4 with this fixed header:

```lean4
import Mathlib
```

Natural-language statement:

{{NL}}

Do not infer a mathematical proof. After any reflection, finish with exactly one final Lean fence
containing one theorem declaration named `sft2b_candidate`. Quantify every type, instance,
variable, and hypothesis needed by the claim. The theorem body must be only the placeholder
`by sorry`; the pipeline discards the declaration name and placeholder body and sends only the
proof-free proposition signature to Lean. Do not include a second declaration, alternative,
attribute, namespace, section, command, explanation after the fence, or any import other than
`import Mathlib`.

The required final envelope is:

```lean4
import Mathlib
theorem sft2b_candidate : <exact proposition> := by sorry
```
