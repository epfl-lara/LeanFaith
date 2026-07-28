/-!
Fixture theorems for LeanInteract integration tests (PLAN.md §8.12, LF-008).
No mathlib dependency: the PR-Lean CI tier builds only this project (§27.2).
-/

/-- Addition on `Nat` is commutative (fixture). -/
theorem lf_add_comm (x y : Nat) : x + y = y + x := Nat.add_comm x y

/-- Zero is a left identity (fixture). -/
theorem lf_zero_add (n : Nat) : 0 + n = n := Nat.zero_add n

/-- A trivially true proposition (fixture). -/
theorem lf_trivial : True := trivial

theorem lf_proof_body_a : True := by
  have proofOnlySentinel : String := "LEANFAITH_PROOF_SENTINEL"
  trivial

theorem lf_proof_body_b : True := by
  exact True.intro

/-- Public type used only by the LF-021 offline collection smoke fixture. -/
inductive LeanFaithLF021OfflineToken : Type where
  | token

namespace LeanFaithPrivateFixture

private theorem hidden (n : Nat) : n = n := rfl

universe u

private theorem hiddenComplex {α : Type u} [Inhabited α] (x : α)
    (h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by
  exact ⟨rfl, h x⟩

theorem publicComplex {α : Type u} [Inhabited α] (x : α)
    (h : ∀ y : α, y = y) : ((fun z => z) x = x) ∧ x = x := by
  exact ⟨rfl, h x⟩

end LeanFaithPrivateFixture
