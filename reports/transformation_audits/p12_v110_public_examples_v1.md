# First public P12 v1.1 pairs

These are public mathlib examples from the completed P12 v1.1 materialization.
In each pair, statement A uses an anonymous proof-arrow premise and statement B
names the same premise with an unused explicit binder. LeanInteract confirmed
that B elaborates in the same context; exact inverse replay, alpha-canonical
type identity, and semantic-atom identity also passed.

They are mechanically audited presentation-equivalent **provisional
candidates**, not human labels and not training-admitted records.

## 1. Order premise

Result: `v2e0_result:e05245f130f0ad3b76fd27c61e5bdb260bc9c4fc54811a08ca6254697eedef01`

```lean
-- A
theorem exists_mem_multiset_le_of_prime {s : Multiset (Associates M₀)} {p : Associates M₀}
    (hp : Prime p) : p ≤ s.prod → ∃ a ∈ s, p ≤ a := by sorry

-- B
theorem exists_mem_multiset_le_of_prime {s : Multiset (Associates M₀)} {p : Associates M₀}
    (hp : Prime p) : (_h_p12v110 : p ≤ s.prod) → ∃ a ∈ s, p ≤ a := by sorry
```

## 2. Membership premise

Result: `v2e0_result:e9e8c4757dbbf44831699adc54005f9ff40b9cddac7fe62c1b40ab7d2d17e3c4`

```lean
-- A
@[to_additive] lemma smul_finset_subset_mul : a ∈ s → a • t ⊆ s * t := by sorry

-- B
@[to_additive] lemma smul_finset_subset_mul : (_h_p12v110 : a ∈ s) → a • t ⊆ s * t := by sorry
```

## 3. Negated divisibility conclusion

Result: `v2e0_result:abc59adc2f59c673083cbe76dac74ea06abf9ec4563664075a3de7b7f4a736e3`

```lean
-- A
lemma two_not_dvd_two_mul_sub_one {n} : 0 < n → ¬2 ∣ 2 * n - 1 := by sorry

-- B
lemma two_not_dvd_two_mul_sub_one {n} : (_h_p12v110 : 0 < n) → ¬2 ∣ 2 * n - 1 := by sorry
```

## 4. Chained membership premises

Result: `v2e0_result:291879d74c07e843deb48a8f5adecd6ac85440fef92c2d111d49f316ab4bb295`

```lean
-- A
protected theorem sub_mem : x ∈ p → y ∈ p → x - y ∈ p := by sorry

-- B
protected theorem sub_mem : (_h_p12v110 : x ∈ p) → y ∈ p → x - y ∈ p := by sorry
```

## 5. Membership-to-equivalence claim

Result: `v2e0_result:cca6164158eb6e24e6f53c027ea957799d61b53bab469ee5c8c05b7f39617527`

```lean
-- A
protected theorem add_mem_iff_left : y ∈ p → (x + y ∈ p ↔ x ∈ p) := by sorry

-- B
protected theorem add_mem_iff_left : (_h_p12v110 : y ∈ p) → (x + y ∈ p ↔ x ∈ p) := by sorry
```

## 6. Equality premise in a longer chain

Result: `v2e0_result:5cef0d756b4c4024de93d9cbef0784b73af8d50de128323806b2dcd1fe5e965f`

```lean
-- A
theorem eval₂Hom_congr {f₁ f₂ : R →+* S₁} {g₁ g₂ : σ → S₁} {p₁ p₂ : MvPolynomial σ R} :
    f₁ = f₂ → g₁ = g₂ → p₁ = p₂ → eval₂Hom f₁ g₁ p₁ = eval₂Hom f₂ g₂ p₂ := by sorry

-- B
theorem eval₂Hom_congr {f₁ f₂ : R →+* S₁} {g₁ g₂ : σ → S₁} {p₁ p₂ : MvPolynomial σ R} :
    (_h_p12v110 : f₁ = f₂) → g₁ = g₂ → p₁ = p₂ → eval₂Hom f₁ g₁ p₁ = eval₂Hom f₂ g₂ p₂ := by sorry
```
