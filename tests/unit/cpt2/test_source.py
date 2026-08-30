from __future__ import annotations

import pytest

from leanfaith.cpt2.source import SourceRow, evenly_spaced_row_groups


def test_evenly_spaced_row_groups_include_both_ends() -> None:
    assert evenly_spaced_row_groups(318, 8) == (0, 45, 91, 136, 181, 226, 272, 317)


def test_source_label_requires_actual_bool() -> None:
    assert SourceRow("row", 0, 0, "theorem t : True := by trivial", True).is_valid is True
    with pytest.raises(TypeError, match="preserved as a bool"):
        SourceRow("row", 0, 0, "theorem t : True := by trivial", 1)  # type: ignore[arg-type]
