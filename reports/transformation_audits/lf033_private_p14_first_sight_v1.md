# LF-033/LF-034 private P14 and N12 first-sight audit

**Audit date:** 2026-08-11

**Disposition:** complete exploratory materialization; provisional candidates only

**Data boundary:** internal, nonredistributable `sft_classic` source

P14 makes one deliberately small change: it exchanges two neighboring,
independent, explicit theorem binders.  A `singleton` operation exchanges two
separate binder groups; a `typed-group` operation exchanges two names inside
one shared type group.  The candidate is retained only provisionally when Lean
accepts the reconstructed declaration and the elaborated tree, semantic atoms,
and inverse replay identify exactly the intended exchange.

This tracked report intentionally contains no private theorem text, binder
names, source rows, natural-language linkage, or raw artifacts.  The source's
undeclared license and `redistribution=false` policy do not permit those values
in Git.  The IDs and content hashes below are exact internal lookup keys for
authorized reviewers.

## Independently verified run

The run is bound to the exact frozen 5,000-record `sft_classic` subset.  A
read-only audit rehashed the input partitions, run specification, all 79 journal
batches, assembled result stream, and manifest; validated all 5,000 result
schemas; and compared every ordered source theorem ID, source representation
ID, rule ID, and deterministic seed with the frozen input.

| Check | Verified value |
|---|---|
| frozen records / terminal results | 5,000 / 5,000 |
| provisional variants | 870 |
| audit-quarantined candidates | 891 |
| invalid candidates | 7 |
| not applicable | 3,232 |
| resolved labels / promoted items / training eligibility | 0 / 0 / false |
| infrastructure or crash failure codes | 0 |
| manifest SHA-256 | `4995270548871056630d1ea1c0ac7e8baea81e3d4dbaf1a97a5e1f77577fdbc2` |
| run-spec SHA-256 | `907f7faeccf173f2b93570d10cbc92ca12b09b4508a990fa5c1c0c1550ca9ad5` |
| results SHA-256 | `f8e64047336367cfa4b065e561b3bce3ad6c9a1ef0fcefe8c81450f978a9f7a3` |
| journal tree hash | `f8b531b1f30944f18106873bd7afe33f784f96336ca734ddfaa32d5f913b6803` |
| frozen theorem partition SHA-256 | `3241ea0ff7f7e80a27ea6deafe680043c8ac8e782db049dcc551c50441115c30` |
| frozen representation partition SHA-256 | `c63bf8e2706d4fc3fff430bee920cb0c575b2947023a4141a4d0384f747cad24` |
| ordered theorem-ID hash | `e048af76bbd3980e62ff09549570fb456bcf8fea3e79497e384d19d225422df3` |
| ordered attempt-key hash | `416a271a81332e822412ef68e13c2d5901078c312f29a556a84f5b55b5152fab` |

The only candidate-level failure codes were `lean_invalid` for the seven
non-elaborating candidates and, for every quarantined candidate,
`candidate_tree_match_not_unique` plus
`semantic_atom_representation_mismatch`.  No failed proof search was used.

## Exact ID/hash examples

All five provisional examples below elaborated, passed the unique elaborated
tree check, passed the semantic-atom mapping, and inverted exactly back to the
source.  “Positions” are zero-based surface-binder positions; they disclose no
private content.

| Operation | Positions | Source theorem ID | Candidate theorem ID | Candidate code hash | Result ID |
|---|---:|---|---|---|---|
| singleton exchange | 2, 3 | `thm:0017baed5c73bb84bbae67a3c65552f4791752c4fb78b0260f35982997422203` | `thm:b1176af47c1bf5ad45d85f2f2600d14974d700e8503c42bbdbb201651baff8f1` | `7e94ce3bb41209c636206cdaf71bb376df076c811de9f2b26064cbb3d1f25573` | `v2e2_result:1f1dd6075953a64197fc29d2d9f25b881007499ffe8ec1922d541c0ac55216b7` |
| singleton exchange | 1, 2 | `thm:0132b9bb085afdbb66760f838a92ab5753a246779aad0c48108e5d1ea438f65c` | `thm:3a31b5e0f60c40cc019fa6879be1e37ba1801298bf8c66276b167527b151c848` | `857d0243b07cb68d279a1cbfe60a7d9aa05e2f5452e1ff94eab8df49d5915912` | `v2e2_result:335396cda2241d82cb47acff9d3182d782c76f73823978f4db1d755cf0768b4e` |
| singleton exchange | 0, 1 | `thm:02a5ab76745612cce23ca95d981d023ea69b12b0156865b233af061146881ab7` | `thm:0b4dad89268abba77e0548b3ed522e10551610ab3e45b99ccf08c55fe56f6539` | `2f1449bb0c1f77d2914763ee7734859f2e52a352cdc8f7f399c1ad73838c143c` | `v2e2_result:0fbd89c1c9a4232b1cb02510f3877b35ab643285928bd0026ee7ce9a4d6c3992` |
| typed-group exchange | 1, 2 | `thm:0059e72a45e67b5611443e8f8eeb86b10a8a70750ee76b27bd92f001d3122ac9` | `thm:34ccc7e4ba0b6bdfa45fba014f705060b545a960a10ef991f7829f51d7e56065` | `392d8174c4c230faac0a78fef7d35fa1dde51bcfeb9b4d20cfe201383bef11ef` | `v2e2_result:9700aaf3b092c2185322183f483def3cd90e2d2ff588df9440727702a4db4d50` |
| typed-group exchange | 0, 1 | `thm:0097fff020b8153030998b122831e46e9c5ff5cb9b8845993cd39a4565e296d7` | `thm:52460cdaf8a68c1e23af546c88de75f2fcc6c8c3ae5fc2e55293b8488a968990` | `b4d975b8a49ad31c6f655be4c170a157ccd20607babada43f15dee59511ec64c` | `v2e2_result:d56334bd7cd6b4c01ec6f8d920add8e14f2e9e63fa65cca4fbe316101e80c9e0` |

The next five candidates also elaborated and inverted to their source, but they
were correctly quarantined: the expanded theorem tree did not identify one
unique intended exchange and the semantic-atom representation therefore did
not support a clean mapping.  They receive no label or training credit.

| Operation | Positions | Source theorem ID | Candidate theorem ID | Candidate code hash | Result ID |
|---|---:|---|---|---|---|
| singleton exchange | 4, 5 | `thm:0020c418248042ea38ccc9a7fda239339e0532d62a648cf60222fbb5b3b65412` | `thm:f8155fc9e4f0bfa24d0e535f1d229846353dbd2d6def48e42874465c8baebff6` | `3034a8dc49e4f02cb51f36e4ed0af5d98b098fff7651e33c511c02bee2b24c08` | `v2e2_result:4cdb1370c31d3d55590a4858ca8b5c0ad94e8e50469a7e403fd4f64fc2bed652` |
| singleton exchange | 6, 7 | `thm:0091ae46b9605bb6e846da8c6ad4e07f18538cf93d86ead7bbf2edd79c5263b9` | `thm:69801d1775140c9a5cab8401120e05d0ea9ef1fb741deed6502a8f650e47551b` | `c4f3825348765bfd125bf56de5e9122a5f663d2649712e469096b17c67ab530f` | `v2e2_result:aa1f560292c56d9a31e8aefb80fcaaf74620ee9d13c2c02fccf48e2b11f6d70e` |
| singleton exchange | 8, 9 | `thm:00a16e7f09eac8ebc57fcc2023f23c4a2b8a5ca80ba61ee7b8eb743c2d1886e6` | `thm:e4c8b8e3da32588f249b158db3e2937267d5b17c615f6d972b785173e307c4f5` | `744a247044b4c3c062529918814165a0e7448da40797cfe88cd214619e3acdab` | `v2e2_result:c0ab57e6690a8b8fb5878a24ef23bc233c739a3336b70791b34d7b55f59f251b` |
| typed-group exchange | 1, 2 | `thm:19b385dd0cd7e4e97ef1fea674cc92bef2fa4f88b247e412095c0424434a39cf` | `thm:f7d7e77dedc3b31f49ea0303ea56e0ffbfff8dc54d4e435b68e0b69e8038abd8` | `824772f5080524ca0a7b0586f57275e7fb0e43b1e7321192195412f6c74ea9db` | `v2e2_result:9f163e48de97b86668a94074e7c98f5beb5dd385090740d7363e2ad7f97c0c74` |
| typed-group exchange | 2, 3 | `thm:3efcca15ee869955f8619f884e0b15b9f5a71ac9a2e6fafd15424efaf3e99078` | `thm:dbc72130e3b8a870f323a60f88b6a46ad47458d3ab3cae63984d202632134851` | `637d1bef74a7b876bf20737f37e997ab0e449ef392075a933abcf8feadc5e43a` | `v2e2_result:ff38e7cba07263c56a2a352d72e1aead5a72e3e0d329a64415bb1d41ee875366` |

## Interpretation boundary

The 870 provisional rows are useful first-sight deterministic candidates, not
gold semantic pairs.  P14's engineering checks make them strong experimental
evidence, but family promotion and any training admission remain separate
decisions.  The 891 quarantined rows show why elaboration alone is not enough:
Lean can accept the candidate even when the structural certificate is not
unique.

## LF-034 N12 implication-converse first sight

N12 makes a different controlled edit: it exchanges the two sides of one
implication, turning “premise implies conclusion” into the converse direction.
This is a useful near-miss generator because the resulting Lean statement can
be perfectly well formed while expressing a materially different claim.  The
pipeline requires one exact elaborated-tree change, changed semantic atoms, and
an inverse replay back to the source.  These are mechanical checks only: N12's
negative intention is not a resolved semantic label.

The read-only audit used the same frozen 5,000-record `sft_classic` subset and
the same private-data boundary as P14.  It independently rehashed the inputs,
run specification, 79 journal batches, assembled results, and manifest;
validated all result schemas; and matched every ordered theorem ID,
representation ID, rule ID, and deterministic seed to the frozen subset.

| Check | Verified value |
|---|---|
| frozen records / terminal results | 5,000 / 5,000 |
| provisional converse variants | 2,478 |
| invalid candidates | 8 |
| not applicable | 2,514 |
| resolved labels / promoted items / training eligibility | 0 / 0 / false |
| infrastructure or crash failure codes | 0 |
| manifest SHA-256 | `ed1aad5cd262c453ae97a93ddd06ed988e04b4fad173abb93e6b0f8c269c6f9e` |
| run-spec SHA-256 | `149287f06a4824891129f5aff539b6f7700a2333dbdba7cfaf9d2d83df997304` |
| results SHA-256 | `14188792938585e52d684866fb2510c2bf7509f4c37084b3a583f59eb58afecb` |
| journal tree hash | `7ab78af414e1aa835512d74dfcd57e80bc2e58e0b8acf07fcd512c3f0523d3e3` |
| frozen theorem partition SHA-256 | `3241ea0ff7f7e80a27ea6deafe680043c8ac8e782db049dcc551c50441115c30` |
| frozen representation partition SHA-256 | `c63bf8e2706d4fc3fff430bee920cb0c575b2947023a4141a4d0384f747cad24` |
| ordered theorem-ID hash | `e048af76bbd3980e62ff09549570fb456bcf8fea3e79497e384d19d225422df3` |
| ordered attempt-key hash | `945c9338eb67ec68cf5c4955941d7e26127230d003038a3a254a86e44db813e4` |

The only failure code was `lean_invalid` on the eight candidates that did not
elaborate.  The 2,478 provisional candidates all elaborated, passed their exact
structural and semantic-atom checks, and inverted to the source.  No failed
proof search was used.

### Exact ID/hash examples

The first eight examples are provisional variants chosen across distinct
outer-binder positions.  “Outer index” is the zero-based position of the
selected implication hypothesis in the elaborated outer binder chain; it
reveals no private source content.

| Outcome | Outer index | Source theorem ID | Candidate theorem ID | Candidate code hash | Result ID |
|---|---:|---|---|---|---|
| provisional | 1 | `thm:00172eae416af1dabed858da59349524a03b347f87e46a814fbefcafb9a0cda3` | `thm:8080640341392ed082cd6d84841cab1c5df77500a42d922847779a51bf2f424b` | `e4befded024b237d74fcb92ee85fbba796acbb464a79fe7a5af832d9756174af` | `v2d0_result:556ad497b988b0b90a0bd8b6d1be00e11915c630b5a96f78082c5540f6da076b` |
| provisional | 7 | `thm:0017baed5c73bb84bbae67a3c65552f4791752c4fb78b0260f35982997422203` | `thm:8ab9fb739ab0e10aadc8ed7e4df8c12e49a69a9dcbbb3e4cc181de524b42c916` | `4fd879b550d225ea4d28ec3db39bed78939c902e9b6935aad16805eedcf712a0` | `v2d0_result:ca3c56c41cadbdd39a3939089aba1f5b1830deb3d8d5020c468f1c60c3562c6c` |
| provisional | 2 | `thm:00184bdbe406c87fba769739ca88c1b2b5bc917559215bbafd2aa9840e2f8a47` | `thm:b035334d9446d992d3c831a8ad3ed5f43bbb70fd27854127aa284d1777461ff2` | `28bd6476f801df8433823df07d932a38453068e2e44f53564d3c25c76dc11d86` | `v2d0_result:c2f2ae2329a0e8451304470f68fd7f5774ade00d562d4e4b8ee4e842812c832f` |
| provisional | 6 | `thm:0020c418248042ea38ccc9a7fda239339e0532d62a648cf60222fbb5b3b65412` | `thm:fcf34325d3ebf8d078f3c3eb3d4617ace18509baec6a2428437b16b343a544dd` | `4a32c766f81182834e56064729ced84aad21f31a0fcca0c2ed91e7f0954f93e6` | `v2d0_result:d53f7b06bf99cd3f06129bba0415cced95eddca8a622f9797654d157f1fa818a` |
| provisional | 9 | `thm:0039a5f8b305b1807f3f746e12131d0c693a3abde63b5e069f47724c8a88b0b0` | `thm:64bcd2dfcee4518b4b464e59a3202dd6b6414ac699ad7903c8e919df755ac6d2` | `8b4b4f15986ada876bc58ca603e462891ac646a2072b4ab9eef088324c8fc0ca` | `v2d0_result:547aba6f1fa02848455e537453a1fb8e677bd7d840d1a8f69ad4dfc4cf72a01e` |
| provisional | 5 | `thm:004f75572492709bcaf43a3712f260747431ead3d407c5d13d77f8199f40480e` | `thm:805754dffb9354425ea60663520c46a4cd0927031fd846de331fb7355192cd0c` | `aed388498c62a4d25d0bc24d9beea9cb619df25ebb3f3d7f40e1bb368cf2c377` | `v2d0_result:f3d907492d607b28ebc3d7836114820b624e3df54090f9ea73056ffa654c592d` |
| provisional | 8 | `thm:0091ae46b9605bb6e846da8c6ad4e07f18538cf93d86ead7bbf2edd79c5263b9` | `thm:9ba330f32a57af4daa156f45ad60c05263017c0b2867ea4ac5506225ebe53bbf` | `5be884f9e97a235ec8d2eb8b3edbc7060b4332257fdb32676628738c21667cf0` | `v2d0_result:25f7d37f2e2e0c2b2b29435f5a709be14d0faf873403d369a3a878073f66011f` |
| provisional | 10 | `thm:00a16e7f09eac8ebc57fcc2023f23c4a2b8a5ca80ba61ee7b8eb743c2d1886e6` | `thm:ab7250117ff43ecd2b381f6054e12777be04bc0d0fac86cfaf3e70130888bd52` | `60d1fd262de4ca5b628482d92fa943761fb3c909bcb43af487fe9e218bfb9ad2` | `v2d0_result:06f67f09f25ed08fbaa4b1eda6e2ff54a829b56a72a54f0009dd148f0d15e966` |
| Lean-invalid | 0 | `thm:19fc336443016379a1eb91e69bb0987df5fff2e6addd6db68437235006820bfc` | — | `1afb309be1d950a4856bed9dad93312ed65575f36ac19a315cc2cf5c83d26a0b` | `v2d0_result:a49059dcad1cb2bc5ac7bdd1db07cc0f01ee3a029b082dd2c5d20eb8253d76c3` |
| Lean-invalid | 0 | `thm:2dc4f45deb3ce62f865eb4d3b29d70c72b197210ec8042abc4535f40e71fdc31` | — | `249dc59e095812938a0209b78ad2774df28355fa147fc787994d56827bf5421c` | `v2d0_result:ac9b266f625212bdde868716a6f48af54feedb3a5539245deaa3bf7eada80f2d` |

The two invalid examples have no candidate theorem ID because Lean rejected
the generated declaration before a candidate theorem record could be created.
Their draft hashes and terminal result IDs remain exact and auditable.  As with
P14, no private statement, binder name, source row, or NL linkage is included
in this tracked report.
