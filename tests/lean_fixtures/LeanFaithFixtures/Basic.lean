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
