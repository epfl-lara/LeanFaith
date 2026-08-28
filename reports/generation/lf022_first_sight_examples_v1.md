# LF-022 first sight: generated Lean-pair audit examples (v1)

## Status and scope

This report gives a small, readable view of data that has already completed the
provisional generation, Lean elaboration, and Codex audit path. The 12 examples
were selected for compactness and coverage of proposer families and audit
outcomes; they are not a random or statistically representative sample.

**These are audit examples, not semantic labels or training items.** The Codex
findings are single-model audit opinions. They have
`semantic_label=false`, `silver_record=false`, `training_eligible=false`, and
`evaluation_eligible=false`. “Lean-valid” below means that the candidate theorem
statement elaborated with a `sorry` placeholder. It does not mean that the
candidate is provable or faithful.

The reference statement is called **A** and the generated candidate is called
**B**. `A_stronger` and `B_stronger` are the Codex audit's claim-relation
verdicts, not conclusions independently certified by Lean.

## Bound provenance

| Artifact | SHA-256 | Role |
|---|---|---|
| `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl` | `69f37377902c5328bb0fa87e8dd014f3f19be095a228f2b3d3d805dcef451824` | Historical Qwen/Kimi/GLM audit findings; 493 Lean-valid candidates |
| `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/manifest.json` | `b2866946d6a8285ddaff79a60c3d7f91520907aebf681165a931c1701f99f8c3` | Historical audit manifest; judge `gpt-5.6-sol`, reasoning `xhigh`, audit only |
| `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl` | `657ec8e8f0b5ec6557b138a06608e998a26bead8fbd7ac6cd8415c586b43cd92` | Historical Lean checks |
| `reports/generation/lf022_kimi_v4_prefix256_codex_sol_xhigh_findings_v1.jsonl` | `9552a840db82eb9e0600d3e8dec15bc8467085027b658a648a8f9eeba8e79b69` | Kimi-v4 prefix audit findings; 201 Lean-valid candidates |
| `/storage/milikic/leanfaith/lf022_codex_audits/kimi_v4_641d13d_prefix256_v1/manifest.json` | `b2ea47f15495f88e8d1bb5703c3e7622c9b7b5a221685c0ed7526d27bf402c17` | Kimi-v4 audit manifest; judge `gpt-5.6-sol`, reasoning `xhigh`, audit only |
| `/storage/milikic/leanfaith/lf022_lean_checks/kimi_v4_641d13d_prefix256_v1/checks.jsonl` | `46972e934b26e9ee6df112a6e135223f83267b58e93ccde2be79e40d6ed54810` | Kimi-v4 Lean checks |
| `configs/generation/lf022_production_family_matrix_v1.json` | `488bfc9c77019b0a9d2e577564db60929156d08195b1e7d647a4e2da9d7aed6e` | Proposer-family to deployment mapping |

The bound proposer deployments are `Qwen/Qwen3.5-397B-A17B` (`qwen3`),
`moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`), and `zai-org/GLM-5.2`
(`glm5`). Provider-side checkpoint revisions were not disclosed; the family
matrix binds the provider deployment snapshot rather than an underlying weight
revision.

## Coverage at a glance

| # | Proposer model | Codex verdict | Codex relation | Lean status |
|---:|---|---|---|---|
| 1 | GLM-5.2 | not same claim | B stronger | elaborates with placeholder |
| 2 | Qwen3.5-397B | not same claim | A stronger | elaborates with placeholder |
| 3 | Qwen3.5-397B | not same claim | B stronger | elaborates with placeholder |
| 4 | Qwen3.5-397B | not same claim | incomparable | elaborates with placeholder |
| 5 | Qwen3.5-397B | same claim | equivalent | elaborates with placeholder |
| 6 | Qwen3.5-397B | uncertain | unresolved | elaborates with placeholder |
| 7 | Kimi-K2.7-Code | not same claim | A stronger | elaborates with placeholder |
| 8 | Kimi-K2.7-Code | not same claim | B stronger | elaborates with placeholder |
| 9 | Kimi-K2.7-Code | not same claim | incomparable | elaborates with placeholder |
| 10 | Kimi-K2.7-Code | same claim | equivalent | elaborates with placeholder |
| 11 | Kimi-K2.7-Code v4 | ambiguous | ambiguous | elaborates with placeholder |
| 12 | Kimi-K2.7-Code v4 | same claim | equivalent | elaborates with placeholder |

## Examples

### 1. GLM changes exponent-zero identity into an arbitrary fixed-point claim

```lean
-- A: reference
theorem uzpow_zero : ∀ {R : Type u_0} [inst : CommSemiring R]
    [inst_1 : _root_.Module R (Additive ℤˣ)] (s : ℤˣ), s ^ 0 = 1

-- B: generated candidate
theorem uzpow_zero : ∀ {R : Type u_0} [inst : CommSemiring R]
    [inst_1 : _root_.Module R (Additive ℤˣ)] (s : ℤˣ), s ^ 0 = s
```

- Proposer: `zai-org/GLM-5.2` (`glm5`).
- Lean check: **valid with placeholder**; `lf022_lean_check:2d708d896e98e77404f1ceedb9db740dac567dc3b39ee398896464fa4beebce2` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:225`.
- Codex audit: `not_same_claim`, `B_stronger`, confidence `0.99`; finding `lf022_codex_audit_finding:8e561367f144326a30e39166271f32797555c85d1630ab7c7f542d497ecb34f2` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:154` (line SHA-256 `c62564f0511fee7a1b84d1b9a426f5641bed92b93d1451f1aac0b38fe4886c3b`).
- IDs: pair `pair:e6f5a8348f7d0ce7aa5ca4fb888cb3c07f9d1fdb92aa8a2f15a4d38a3c45323b`; variant `var:147299a5469abbade16dee0da000875061d75e13d90528bf10f1592c639a656d`; audit item `lf022_codex_audit_item:c067fa5952f2c069161c9490d151e98faa3aff54ebfc151af4674e1b5aa67973`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/c0/c067fa5952f2c069161c9490d151e98faa3aff54ebfc151af4674e1b5aa67973/input.json:1`, SHA-256 `6a8599a318635783badf5899248f4097d782a8b67f7040c3434a4b4b7486b266`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/1a/1a3035f58508d907a309c154890ed02b59948e8990321a18f8e8dcc74d2f18ff/provisional_variants.jsonl:1`, SHA-256 `0ef0d23d56ad477654005f0c9504f59f7ef1daa3480310798751e336490df643`.
- Plain-language note: exponent zero is always the identity `1`; asking it to equal every `s` is a much more restrictive and generally wrong claim.

### 2. Qwen drops one direction of an equivalence

```lean
-- A
theorem nat_iff : ∀ {f : ℕ → ℕ}, Primrec f ↔ Nat.Primrec f

-- B
theorem nat_iff : ∀ {f : ℕ → ℕ}, Primrec f → Nat.Primrec f
```

- Proposer: `Qwen/Qwen3.5-397B-A17B` (`qwen3`).
- Lean check: **valid with placeholder**; `lf022_lean_check:f9d603cbf66e4aeab8b5ef91693d01ef94aa94d33563240acae17ddb786fdce1` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:171`.
- Codex audit: `not_same_claim`, `A_stronger`, confidence `0.99`; finding `lf022_codex_audit_finding:35adf91d3034731354e19e51206809d53a490c9cbf71620db9518e7e709ad100` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:114` (line SHA-256 `d9be2dee72eff4305fc8c613b88601a73b50b974f1fd2f8d091c99f36dfe4a52`).
- IDs: pair `pair:7b0e752357e3b6270a844beb14681de2174b9b31a50e25072d31aa6c513f09ba`; variant `var:581bad7a81b2731402a6905b3186f35965a84a338110052537fc6d92b9ea3c3d`; audit item `lf022_codex_audit_item:23fd256118f08e9f1853e58f15da2c90443d4dc355b9ee2bc05d0148388f7f21`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/23/23fd256118f08e9f1853e58f15da2c90443d4dc355b9ee2bc05d0148388f7f21/input.json:1`, SHA-256 `86de2347875d6bd141e8284c1f0766e237c11f0338891480e17a04eed9bbb89f`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/04/048e02b27d4ef7a99f811fc45cbf5990fc5cec57ced5f53349eacfde11de3a1a/provisional_variants.jsonl:1`, SHA-256 `3905d03ce8763df3a0c3a7e911b0588d7986aaff93a4dca811ec5a5c82e92bf5`.
- Plain-language note: the reference states both directions, while the candidate states only one. This is a useful near miss even though truth-level proof checks of already-provable closed statements may obscure the content loss.

### 3. Qwen strengthens a non-strict logarithm bound to a strict one

```lean
-- A
theorem log_le_log : ∀ {x y : ℝ}, 0 < x → x ≤ y → Real.log x ≤ Real.log y

-- B
theorem log_le_log : ∀ {x y : ℝ}, 0 < x → x ≤ y → Real.log x < Real.log y
```

- Proposer: `Qwen/Qwen3.5-397B-A17B` (`qwen3`).
- Lean check: **valid with placeholder**; `lf022_lean_check:959cfcfbd9b1553138dda6f27990d27a654207c66ea4d47155a9f40e50e19360` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:355`.
- Codex audit: `not_same_claim`, `B_stronger`, confidence `0.99`; finding `lf022_codex_audit_finding:a5415beeee0f36dde19acd7477ccd4e0b21cfdc48186de91199f7f84c2980d59` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:255` (line SHA-256 `2b36cb48fa23fd68f00b9248652db769878ebf285150603643c65fe5720d504d`).
- IDs: pair `pair:2b34e03dc3f207eb810313e4a73e769b91c50d3ba981658eb39d1623ec733a0f`; variant `var:127c623996971f91f8d852e84445c8812cb334f2a4fd8873a3aede0c115c62ce`; audit item `lf022_codex_audit_item:8d8c46fdd5118fa074140295a2acb86aa0038d55e442f5d3e99c6a59175f1050`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/8d/8d8c46fdd5118fa074140295a2acb86aa0038d55e442f5d3e99c6a59175f1050/input.json:1`, SHA-256 `1aeb6a7e3e81f72fc569305f7b3b95c82173611ce56b915bed7d6853795ff2eb`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/56/5600fe48313675c74be8511d05aacc69cec425bd6bd38df89c6d04fa5d8b664b/provisional_variants.jsonl:1`, SHA-256 `4b761f798401a8122042c8f8df36fd1049342b91dcd7a85ea13bf565233c2ccc`.
- Plain-language note: the candidate incorrectly demands strict inequality even when `x = y`, where the logarithms are equal.

### 4. Qwen changes a known value to an incompatible value

```lean
-- A
theorem sinc_zero : Real.sinc 0 = 1

-- B
theorem sinc_zero : Real.sinc 0 = 0
```

- Proposer: `Qwen/Qwen3.5-397B-A17B` (`qwen3`).
- Lean check: **valid with placeholder**; `lf022_lean_check:265a43e4a3b3950cf7d1e492e63c4b97cd1c6530c6b906c8bf7056dbc396b625` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:281`.
- Codex audit: `not_same_claim`, `incomparable`, confidence `0.99`; finding `lf022_codex_audit_finding:b546d0940bce7e31ef1472448a110a22252ad0e3106ec95245fb2938f299799f` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:202` (line SHA-256 `79f0e59d33dacf13147dadfb58a321bf4a1fa1ee91484678350d6cd276919609`).
- IDs: pair `pair:d0e881ad87820baeb88d3ae001cc6bdee6eff3757a4560187190a86b1d2af658`; variant `var:1a5cb62157d6542e53e4907f099d3282a2238ad538508ff5e09443c24aba9ca8`; audit item `lf022_codex_audit_item:3cae7065cf18cab45e8069e653f374744e1242a7cbb4a1483a5c88810f4201d5`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/3c/3cae7065cf18cab45e8069e653f374744e1242a7cbb4a1483a5c88810f4201d5/input.json:1`, SHA-256 `d9b2df6fab84daef50ccf9eb8c02cf4c3cfb04e54e0f801deba0781a81a38872`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/38/388c3e7cbfde90b41f47be21e144e75c3bef3d1848a85fbd8caa8f39a51f8400/provisional_variants.jsonl:1`, SHA-256 `8e671afef0d970322e9fc45420862bfe787ac3fdd87b1ccaa1ba50a7f4f46788`.
- Plain-language note: both statements typecheck as propositions, but they assign different values to the same expression; typechecking alone cannot reject this semantic error.

### 5. Qwen removes a direction that is immediate from equality

```lean
-- A
theorem toMeasure_inj : ∀ {α : Type u_0} [inst : MeasurableSpace α]
    [MeasurableSingletonClass α] {p q : PMF α}, p.toMeasure = q.toMeasure ↔ p = q

-- B
theorem toMeasure_inj : ∀ {α : Type u_0} [inst : MeasurableSpace α]
    [MeasurableSingletonClass α] {p q : PMF α}, p.toMeasure = q.toMeasure → p = q
```

- Proposer: `Qwen/Qwen3.5-397B-A17B` (`qwen3`).
- Lean check: **valid with placeholder**; `lf022_lean_check:ea6e212212ef6beede501e8f2f30846930a93bf739c2616284533eb2a49a9b2b` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:103`.
- Codex audit: `same_claim`, `equivalent`, confidence `0.99`; finding `lf022_codex_audit_finding:5431a62baf2b5481092d1740a8293495d339460eaf91182d90bd6569fa3319a9` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:67` (line SHA-256 `24d0d897b64bc33059931d843932ad41c1dd1e82a9520133d379ebf9417ce78e`).
- IDs: pair `pair:93fbcc6caafe69d8af9c2748e6042f9a817044c15056a1d45c8a5e1dcce46c98`; variant `var:0ef0a3677957f1a53335ca81cd404f37d09e714540a578e94c1591416b644e7e`; audit item `lf022_codex_audit_item:b4b26e3445f659b49b033e08108c0b322cc820c99edc935b38e163788fb34e1c`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/b4/b4b26e3445f659b49b033e08108c0b322cc820c99edc935b38e163788fb34e1c/input.json:1`, SHA-256 `1c58f06f34451ba26775963913511ae6fe806d5a222004b23a8cd1c0c0cfe952`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/02/0281b3a744286c26f18ca660ab3fa9255eefadd8770d35540e65aa9a0f9aadc2/provisional_variants.jsonl:1`, SHA-256 `39e3c481c7fe267ae79336a9745f7be51c86c843372addb673419a2392c0d36a`.
- Plain-language note: `p = q` automatically implies equality of their measures, so the omitted reverse direction is logically immediate; the audit treats the one-way injectivity formulation as the same claim.

### 6. Qwen exposes a rendering problem and the auditor abstains

```lean
-- A
theorem congr_refl_right : ∀ {α : Sort u_0} {β : Sort u_1} {f g : α → β}
    (h : f = g) (a : α), ⋯ = ⋯

-- B
theorem congr_refl_right : ∀ {α : Sort u_0} {β : Sort u_1} {f g : α → β}
    (h : f = g) (a : α), f a = f a
```

- Proposer: `Qwen/Qwen3.5-397B-A17B` (`qwen3`).
- Lean check: **valid with placeholder**; `lf022_lean_check:482a95cbc7a8d21d33821bf14d3ec7cbb2c8e9286a798ba95baa6ba8758bcf40` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:299`.
- Codex audit: `uncertain`, relation unresolved, confidence `0.98`, expert review requested; finding `lf022_codex_audit_finding:85e75b46306fdd614f17d8562394888fbb08e2a297b4b4b8cd357957764c0339` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:214` (line SHA-256 `a512eb306ca4f21d35168c9f59658acc915cba18d695d1d32af887c5d787adf7`).
- IDs: pair `pair:a387ca4d98ee4622d4871151308e29ba6982e115623505402a0ec55060babd0f`; variant `var:361fb134b4671f8b2541241094366f9c819b914fa5f38c0771c6f290594f00db`; audit item `lf022_codex_audit_item:590073ae3ea3534c8aa1fcd11d2a8c85e425bd2a3b91cfd7b2ececdc1fa6ff80`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/59/590073ae3ea3534c8aa1fcd11d2a8c85e425bd2a3b91cfd7b2ececdc1fa6ff80/input.json:1`, SHA-256 `72efa7243f586a1103cf32bfb4ad3bb114a86a3a70ab8725090ab2792e0e3cb5`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/3f/3ff9e50c50ba1d22a408736f4ea0abbfede13cdd8762b2862e4b201cecd49dbc/provisional_variants.jsonl:1`, SHA-256 `8199a7d964de3a9dc2af48cfd165bcae8906afee14a6452fc35651a31911595e`.
- Plain-language note: the reference contains pretty-printer ellipses, so the actual expressions being compared are hidden. Abstention is appropriate, and this sample is evidence for filtering lossy renderings before semantic use.

### 7. Kimi weakens uniform continuity to continuity

```lean
-- A
theorem uniformContinuous : ∀ {X : Type u_0} [inst : UniformSpace X]
    {x y : X} (γ : Path x y), UniformContinuous ⇑γ

-- B
theorem uniformContinuous : ∀ {X : Type u_0} [inst : UniformSpace X]
    {x y : X} (γ : Path x y), Continuous ⇑γ
```

- Proposer: `moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`).
- Lean check: **valid with placeholder**; `lf022_lean_check:915a803665a232d8f98b5f7e0479339e51827bcf38c4df16bd9d9905f77c8799` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:425`.
- Codex audit: `not_same_claim`, `A_stronger`, confidence `0.98`; finding `lf022_codex_audit_finding:157676246227a80d3c35fa6c0d56463c5aee2a4ed7d7dd450c904b6e97d6eb82` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:307` (line SHA-256 `1cfb4a1d834472bd0166333d29bc2a45bfb45696c83faeb98f5693c1b199086f`).
- IDs: pair `pair:bb1856ea62cb53d366fd8f93dff20cb64d85abefaa54e21a202a1f7ab4638b0d`; variant `var:755b88ee2df1b1df2b380444b1e4e9b9fe59f27ea4be299a0cc0b90ebfac6006`; audit item `lf022_codex_audit_item:b3464f538771cfc93d473d6dece22f36575902bab32784a07a995467ddca043d`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/b3/b3464f538771cfc93d473d6dece22f36575902bab32784a07a995467ddca043d/input.json:1`, SHA-256 `0bd9fe8613565679eca5d0cedba7883b34770889db855af6cb7f3d2400cdb87f`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/7e/7e92038b9f4997b89a7e8ebf838b5701f9368da91524ba1da3c795b45d5fc6f3/provisional_variants.jsonl:1`, SHA-256 `60fc40f4ec25085f0a011b2c9415ee1addd2b992bd79b744712275eec5344576`.
- Plain-language note: uniform continuity is stronger than ordinary continuity in general, so the candidate loses part of the mathematical claim.

### 8. Kimi broadens the domain on which positivity is asserted

```lean
-- A
theorem qaryEntropy_pos : ∀ {q : ℕ} {p : ℝ},
    0 < p → p < 1 → 0 < Real.qaryEntropy q p

-- B
theorem qaryEntropy_pos : ∀ {q : ℕ} {p : ℝ},
    0 ≤ p → p ≤ 1 → 0 < Real.qaryEntropy q p
```

- Proposer: `moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`).
- Lean check: **valid with placeholder**; `lf022_lean_check:7b931bd58da006a1c6aa3a7622ff18f3f5ea0018d60c1a08395fc685c6f28237` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:199`.
- Codex audit: `not_same_claim`, `B_stronger`, confidence `0.99`; finding `lf022_codex_audit_finding:62d3b1edaa2908264e82f62cc6593caa58e4149d0c9da9477efbc80d733faab5` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:136` (line SHA-256 `b904009f606e37ff2d69cefb97dfc86a55058c3a60fd3f20d5100e8ce1dec43f`).
- IDs: pair `pair:2eccf5a27cced5b4880e53b62c7db015cf975f8e1c4c2fdc43db020512d40f1e`; variant `var:9046c93c33b0883fe4a321eebac184ec7ee536c53f8c80c29a797477815f2c60`; audit item `lf022_codex_audit_item:ae0dc4b6b5cb2018b7fa7a39863bb166b5ddd63bb5c22e57720edfa2811c7162`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/ae/ae0dc4b6b5cb2018b7fa7a39863bb166b5ddd63bb5c22e57720edfa2811c7162/input.json:1`, SHA-256 `9d25aee62cafa5a2e0247870592588deb2a96778a340364d4354cef528a45ec9`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/08/089d78dc285f5386cd5fd1a36c7b48e605d42a7529587ff193f1c9cc7c98a639/provisional_variants.jsonl:1`, SHA-256 `23d9bffa252bc84b9b22e965148b81da4af4ab9bfd87033f2e7d88a460004a26`.
- Plain-language note: replacing strict interior conditions with inclusive endpoint conditions asks the positivity conclusion to hold in more cases, making B a stronger and potentially false claim.

### 9. Kimi changes a concrete arithmetic value

```lean
-- A
theorem minFac_one : Nat.minFac 1 = 1

-- B
theorem minFac_one' : Nat.minFac 1 = 2
```

- Proposer: `moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`).
- Lean check: **valid with placeholder**; `lf022_lean_check:c1a184212a43476ef0608478d3041644c1bd63ee4defc767b86d4696c80e7f55` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:375`.
- Codex audit: `not_same_claim`, `incomparable`, confidence `0.99`; finding `lf022_codex_audit_finding:2a7253be9066045cc9f291dd4f32be7f70aca3753207ce806d0ec9c8294f2bee` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:272` (line SHA-256 `b0e98bbbb9f4a0d46f79fbfb938acb2d550c171610d647d0dc428d1bcd7c5409`).
- IDs: pair `pair:dc5a6f07d144f805a4a1a530ea8654ea74a3112fc19b62a7eb88a8d9c628eb18`; variant `var:e54b3a5fe4c0a507c0143aaf81e836e5c83ec7aeebf4f015915643d913c2efa3`; audit item `lf022_codex_audit_item:66ef026dc310b0c7e841402d3de7abef2e0e6659b3f0430cfb1cd4417724d3e1`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/66/66ef026dc310b0c7e841402d3de7abef2e0e6659b3f0430cfb1cd4417724d3e1/input.json:1`, SHA-256 `1791b16f845f8eb7b02735fd940cb57de8f3ace461344a74ff6d7bee48d73e0a`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/62/62066f17da81a66ef1f70926cafb92a702cfda9897a7d648fef51a91ec11f136/provisional_variants.jsonl:1`, SHA-256 `68eeb86e43947e0f3f4bbf2f534e37706259399647585e21b7b11a0876592677`.
- Plain-language note: this is a simple, type-correct but semantically wrong constant substitution.

### 10. Kimi removes a finite index shift from a limit

```lean
-- A
theorem tendsto_one_div_add_atTop_nhds_zero_nat :
    ∀ {𝕜 : Type u_0} [inst : DivisionSemiring 𝕜] [inst_1 : CharZero 𝕜]
      [inst_2 : TopologicalSpace 𝕜] [ContinuousSMul ℚ≥0 𝕜],
      Filter.Tendsto (fun n => 1 / (↑n + 1)) Filter.atTop (nhds 0)

-- B
theorem tendsto_one_div_atTop_nhds_zero_nat :
    ∀ {𝕜 : Type u_0} [inst : DivisionSemiring 𝕜] [inst_1 : CharZero 𝕜]
      [inst_2 : TopologicalSpace 𝕜] [ContinuousSMul ℚ≥0 𝕜],
      Filter.Tendsto (fun n => 1 / ↑n) Filter.atTop (nhds 0)
```

- Proposer: `moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`).
- Lean check: **valid with placeholder**; `lf022_lean_check:08ec6f2a41b3afb7faac35a59e5542f80429b8bead8e96e7b0a63e2aedabe18d` at `/storage/milikic/leanfaith/lf022_lean_checks/rcp_5e672b9_v1/checks.jsonl:417`.
- Codex audit: `same_claim`, `equivalent`, confidence `0.99`; finding `lf022_codex_audit_finding:3fe59f287adcccb87a8b0c601fde280827179ebdb83e29aa1b12c7cfa96983d8` at `reports/generation/lf022_codex_sol_xhigh_v2_findings.jsonl:301` (line SHA-256 `21a97f7fdec9021bc091b355aad93e55611dcb1face9bc6278eb83c115bc5166`).
- IDs: pair `pair:0995960135cb43fae40c05820e9b99cf0f7e753beca236c9a954106f464c9c70`; variant `var:a60dc58da4f5b3e0651082284c240a9b982045dad3cc540450cc0985551922e2`; audit item `lf022_codex_audit_item:22220e6991f4519eda1d6416a7ac35738957ecec952e2fe37eba221391172e12`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audit/sol_xhigh_v2/items/22/22220e6991f4519eda1d6416a7ac35738957ecec952e2fe37eba221391172e12/input.json:1`, SHA-256 `7afee243dbe91024ef2449adc74000c73e3c6aa065681fa181864adedca8af1e`.
- Source variant: `/localhome/milikic/LeanFaith-rcp-5e672b9/data/lf022_execution/tasks/7c/7c07b420edfef04a41e940211dd5685f253b9b73b3118551da6ad7e90c003478/provisional_variants.jsonl:1`, SHA-256 `52b75eb730e18cdd7c0d0546ba6803ff80d4be2ddcf3c84b3d2104ecc35e005e`.
- Plain-language note: dropping a finite shift changes the sequence at early indices but not its eventual limit; this is a nontrivial positive example rather than a cosmetic rewrite.

### 11. Kimi v4 makes an inferred index type explicit, and the auditor abstains

```lean
-- A
theorem iInter_Iic_rat : ⋂ r, Set.Iic ↑r = ∅

-- B
theorem iInter_Iic_rat : ⋂ (r : ℕ), Set.Iic (↑r : ℝ) = ∅
```

- Proposer: `moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`), Kimi-v4 prefix-256 run.
- Lean check: **valid with placeholder**; `lf022_lean_check:7c0186e74e05b9841e036e164aa43d02f8ad839b98fe01a7641bcb8135d3f7b5` at `/storage/milikic/leanfaith/lf022_lean_checks/kimi_v4_641d13d_prefix256_v1/checks.jsonl:106`.
- Codex audit: `ambiguous`, `ambiguous`, confidence `0.98`, expert review requested; finding `lf022_codex_audit_finding:d818924ab5bf349167b16d530116628d9e48b9690089eb593915e74b7c5824c4` at `reports/generation/lf022_kimi_v4_prefix256_codex_sol_xhigh_findings_v1.jsonl:88` (line SHA-256 `c1ba6a1ccf5d7fb5d6823b5eeb212fd6a7534369a7d58067c4e4906f381afdbf`).
- IDs: pair `pair:bd736f933e557743ab5daae3f28986f2dbe8b22871c08b8b04d6958c83030ee0`; variant `var:9d40fcfe94b56465a1ec64be233795239e893e0cd123dfd2eac503ebb1288c69`; audit item `lf022_codex_audit_item:31cb2818e4728539b68185a567e4060b02ed55d1a8659efd89bb2051ebd86ede`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audits/kimi_v4_641d13d_prefix256_v1/items/31/31cb2818e4728539b68185a567e4060b02ed55d1a8659efd89bb2051ebd86ede/input.json:1`, SHA-256 `d45cbb18f3997f8c15f8f71bc5ba3bb3b4dbb5ee520003f0a4e21d4eac447752`.
- Source variant: `/localhome/milikic/LeanFaith-kimi-641d13d/data/lf022_execution/tasks/76/765551f9ed67b4c5364d07882269be220031f0769af0cf4897274c578968479c/provisional_variants.jsonl:1`, SHA-256 `f641e06d0111880ed2246485317a4ba73e070ed7d86170dcbe06b8a307e21268`.
- Plain-language note: the short reference hides the inferred type of `r`, while B fixes it to naturals and the codomain to reals. The auditor correctly avoids guessing whether that type specialization preserves the claim.

### 12. Kimi v4 replaces a named filter operation with its notation

```lean
-- A
theorem coprod_neBot_left : ∀ {α : Type u_0} {β : Type u_1}
    {f : Filter α} {g : Filter β} [f.NeBot] [Nonempty β], (f.coprod g).NeBot

-- B
theorem coprod_neBot_left : ∀ {α : Type u_0} {β : Type u_1}
    {f : Filter α} {g : Filter β} [f.NeBot] [Nonempty β], (f ×ˢ g).NeBot
```

- Proposer: `moonshotai/Kimi-K2.7-Code` (`moonshot_kimi_k2`), Kimi-v4 prefix-256 run.
- Lean check: **valid with placeholder**; `lf022_lean_check:c318bf30e51d2db588e0740699e86e4e8321d74f6aca24060cd00998024e54da` at `/storage/milikic/leanfaith/lf022_lean_checks/kimi_v4_641d13d_prefix256_v1/checks.jsonl:148`.
- Codex audit: `same_claim`, `equivalent`, confidence `0.99`; finding `lf022_codex_audit_finding:ccdffecdb264e0122f173911d6dd723e1d2c6c95ae1dcb967e4748fa2552ebf9` at `reports/generation/lf022_kimi_v4_prefix256_codex_sol_xhigh_findings_v1.jsonl:126` (line SHA-256 `cfdf66882b3356e5c6051fec3990f30800b8c6ddb23ff88bcc371d1b4ce4853c`).
- IDs: pair `pair:db8273dded84b28cf0836e32b61f28fe9adcb103124e6968d406c3c3e77915fb`; variant `var:d9dcb5d5c4d2a220697449f36c657af45a6ad87a20c8a858264cb1641596c5f6`; audit item `lf022_codex_audit_item:163d37c7b83fcd2680958b15615ee64f35ab843af54a9e3e8d19218a0927fabf`.
- Bound pair: `/storage/milikic/leanfaith/lf022_codex_audits/kimi_v4_641d13d_prefix256_v1/items/16/163d37c7b83fcd2680958b15615ee64f35ab843af54a9e3e8d19218a0927fabf/input.json:1`, SHA-256 `018ca861fd317f7480b84233019a40fad3e9d25b66ad376fed92b81b5c201b49`.
- Source variant: `/localhome/milikic/LeanFaith-kimi-641d13d/data/lf022_execution/tasks/a2/a219f6d2a7ef99a18d69fa85115ba0a3c3a770071ce7750f8ca7d9b1b550fbfe/provisional_variants.jsonl:1`, SHA-256 `1e7a3ba0288ae887264a267d2e0b651e9546cfdc5c69983c354a57916aaa3886`.
- Plain-language note: the candidate uses notation for the same filter coproduct operation, so it is a useful positive case with a real surface-form change.

## What this first sight shows

- The generators already produce compact, plausible semantic near misses: strict
  versus non-strict inequalities, weakened conclusions, broadened domains, and
  wrong constants.
- They also produce potentially useful positive pairs, including logically
  redundant directions, finite index shifts, and notation changes.
- Lean elaboration efficiently removes malformed candidates but cannot tell a
  true theorem from a false or unfaithful proposition.
- The two abstention examples show why lossy pretty-printing and hidden inferred
  types must be filtered or represented more explicitly before promotion.
- Codex judgments are useful for triage and data inspection, but none of these
  examples becomes a label until the project's promotion/resolution policy is
  satisfied.
