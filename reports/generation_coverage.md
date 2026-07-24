# LF-021 generation coverage

Status: mechanical collection coverage is complete and ready for Gate 5G
finalization. Semantic faithfulness has not been adjudicated. **Gate 5 remains
open**.

## Scope

- `three_family_collection_only`
- reduced-data ablation: yes
- confirmatory D4/D5 eligibility: no
- held-out-generator claim eligibility: no
- semantic labels inspected: no
- semantic labels created: no
- supervision eligibility: no

The production lineage covers 16 immutable tranches: 12 original tranches and
4 post-exhaustion extension tranches. It contains 1,440 terminal invocations,
299 compile-and-benchmark-clear population members, 49 duplicate members, and
250 unique problem-aware eligible units. The frozen prevalence frame contains
240 units over 31 strata.

The frozen frame family counts are:

| Family | Items |
|---|---:|
| `goedel_formalizer_v2_8b` | 67 |
| `kimina_autoformalizer_7b` | 108 |
| `stepfun_formalizer_7b` | 65 |

The pool counts are 149 items from `algebra_gate3_docstrings_v1` and 91 items
from `cross_domain_docstrings_v1`.

## Frozen prevalence-design lineage

- Policy: `lf021_prevalence_design_v3`
- v3 SHA-256: `d00e096cf6c49224ffa45fc236f872daa57c0adcafe3f3fec4ffa39781a79504`
- v2 SHA-256: `af8a6238c3dab0b54c14a32d4a83274270d3ebd6340d6f2601c94de0de02bfb0`
- v1 SHA-256: `312bc2905eec9e9c30679aaecf1d12a90f46b7fa01f6e01bdfffe132cc584a27`

## Frozen collection and frame lineage

- Activation decision:
  `lf021_expansion_decision_v2:49ba0c86a7ba9777c98e6707902384e7d8c5a187c8bd64e3ff27c59aeb31c1e0`
  with SHA-256
  `9affe165e888022d24e29475962c66315e9ca864dc06fd3134ee57027afbe1f1`.
- Extension stop:
  `lf021_post_exhaustion_extension_decision_v1:ec7522abdd2219a2e9f2587f9ec4fa2818b3fd5f31237aae6a5ca63e55bd0cca`
  with SHA-256
  `9e5598a5d2576bb5b9c425b300d8e4e963244b6b40eda7d31f4f26c42e32bc5c`.
- Frame-freeze decision:
  `lf021_extended_frame_freeze_decision_v1:0574621043042ed62b486260de9bf633797f2471217f502be25b9aec3a46a19c`
  with SHA-256
  `e780d3bc6150f2fafe576170be792b6f13e60d7ef35567912d27d03b63d8eabc`.
- Eligible population:
  `lf021_extended_eligible_population_v1:410d50f581cf58c8929e48ee74a0a45edb21319637acfd00839cf24dc4fafe35`.
- Population manifest SHA-256:
  `b458bc68e4252ad8ef3361fee8198dd464486fbb2edb8f39eaf10d34249c89d3`.
- Population JSONL SHA-256:
  `309f488df94a297834114088c0e1799ebc96004f247842918174bbed8f6ccddc`.
- Frozen frame:
  `lf021_extended_prevalence_frame_v1:c4d248631456c45a05a6c4d929cda6a69c6b91e05e5e65ed4a8ca8eff0367312`
  with SHA-256
  `a07b352030a2c51fa51ebcabc00a3c1d1ecf2041318feabfe57a4c70fc365069`.
- Production sampling-seed SHA-256:
  `40430b68f9e4eb984f8c14a2e02f669a5bdb0dc522514010f0996d2c11d7b125`.
- Sampling-seed provenance SHA-256:
  `6bd82230c139c63b1760b22bd82d5179d52e375eb33acb57a47a28229b00ca1e`.
- Population-bound seed-lock SHA-256:
  `08e6315c3493cb1c9c33e3fbaca27364622069823385722b2e66d2e33ef24a23`.

## Reviewed extension authorizations

The four reviewed authorizations appear in exact extension-tranche order:

1. `lf021_reviewed_extension_collection_authorization_v1:8ada658826eb73ee93d3bdf4f901bc8b2412ee7186af42cfb92a664bb2b95acc`
   — SHA-256
   `84121a976cdc10b566f143c6d8df79378fe5047b15c305c980bfd0fbde9cde31`.
2. `lf021_reviewed_extension_collection_authorization_v1:1fda4c43ed93e1c67112ca27ce0a2f85c28c02658a2f08d833f2224db8ff5a37`
   — SHA-256
   `72a9e80f2b9178ddec2d9559d95da9ee60679d627e04d8c5ef22d28bdd53d0cf`.
3. `lf021_reviewed_extension_collection_authorization_v1:9bfb92228c4c12fe0843fec3e5d1eb73ba1b1f59ae4e3b7ee1cc3c8979dc1547`
   — SHA-256
   `cc25a702ae910c1a8a8c2e10dc70e4f605ebc9cadd1d39c5ec4079df03b681a0`.
4. `lf021_reviewed_extension_collection_authorization_v1:df36acefe9f8d6c7eee462d41d3660d323812c7b60db32b7a487f1edae8db436`
   — SHA-256
   `c0234e5b2c62283d160f457346004fab5f1e1822164fb20d75c9ec0bd500e606`.

## Exact Gate-5G lineage

- Manifest:
  `lf021_gate5g_lineage:ddac5e106c92b263ed96c9974eeadcef25f4980f1e777b06bca75de133b0aa1d`
- SHA-256:
  `a2bb9dba960a7906057647162a6ba00e17f26d0aa89180940e9e6112138ca761`
- Collection replay certificates: 16
- Postprocess replay certificates: 16
- Expected and terminal invocations: 1,440
- Compile-and-benchmark-clear outputs: 299

Gate 5G establishes mechanical collection, replay, lineage, and benchmark
clearance only. It does not establish semantic faithfulness. Human prevalence
annotation is the next required scientific step, so Gate 5 remains open.
