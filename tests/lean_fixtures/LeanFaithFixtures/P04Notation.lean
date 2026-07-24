/- Fixture-local P04-lite aliases. `autoImplicit false` in the live tests
ensures these are real notation declarations, not silently introduced
implicit identifiers. This module stays separate from Basic.lean so its
generated notation declarations cannot affect declaration-extraction fixtures.
-/

notation "ℕ" => Nat
notation "ℤ" => Int
