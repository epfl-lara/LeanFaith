# Gate 3 representation-collision review

> **Current representation version:** The report below preserves the original
> `repr_v2` collision closure. The separate `repr_v3` audit re-enumerated and
> rechecked 152/152 clusters: 141 `semantic_atoms` projection losses and 11
> `signature_pp` erasures, all with disposition
> `expected_lossy_projection`. Current immutable evidence is recorded in
> `reports/milestones/phase_3_repr_v3_revalidation.md` and
> `reports/gates/gate_3_repr_v3.json`.

**Updated:** 2026-07-18
**Decision:** **PASS**

This report closes the manual-review portion of the Revision 4.1 Gate-3
representation audit on the frozen 10,000-theorem denominator.

## Mechanical collision classes

| Collision class | Count | Required disposition | Result |
|---|---:|---|---|
| cryptographic: same hash, different canonical bytes | 0 | zero | pass |
| canonical alpha: same fingerprint, different normalized expression | 0 | zero | pass |
| lossy `semantic_atoms` view | 141 | enumerate and review | pass |
| lossy `signature_pp` view | 11 | enumerate and review | pass |
| **lossy clusters total** | **152** | audit `min(200, cluster_count)` = 152 | **152/152 reviewed** |

Every lossy cluster has a stable `(view, view_hash)` key and a deterministic
reason code. The only reason codes are:

- `semantic_atom_projection_loss`: the atom view intentionally loses argument
  order, bound-variable position, or application structure;
- `pretty_print_erasure`: pretty printing intentionally suppresses coercions,
  inferred receivers, or other elaborated structure retained by explicit and
  alpha-normalized views.

All 152 reviews have disposition `expected_lossy_projection`. No review found
a cryptographic problem, alpha-normalization problem, proof leak, or
implementation defect. Of the current clusters, 145 matched previously
reviewed exact keys and seven new keys received independent review before
closure.

## Immutable evidence

- mechanical audit:
  `/storage/milikic/leanfaith/gate3/run_rev41_v3/audits/run_a_mechanical.json`,
  SHA-256
  `2ee5fd465bf0528c08b60f79709a7c050efae23a84f59a2b7f6fc165dbdf947b`;
- reviews:
  `/storage/milikic/leanfaith/gate3/run_rev41_v3/audits/manual_collision_reviews.jsonl`,
  SHA-256
  `a3f1511b22017855282f5189adf0554bdfdf8ed0d6a062a7e729c54b2b0762ce`;
- closure:
  `/storage/milikic/leanfaith/gate3/run_rev41_v3/audits/collision_closure.json`,
  SHA-256
  `d5bf43ff0219016ecea34c43f8fd982aa17671803f5ef80e35823aad83221785`.

The closure artifact records `required_count=152`, `reviewed_count=152`, an
empty `review_errors` list, `manual_audit_status=complete`, and
`gate_pass=true`.
