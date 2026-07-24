"""Explicit binder-alignment specifications and conservative v1 checks."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.ids import PAIR_PREFIX, id_pattern


class ClaimAlignmentSpec(StrictModel):
    """A versioned explicit alignment; arbitrary proof scripts are forbidden."""

    schema_version: Literal[1] = 1
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    alignment_version: str
    template_id: Literal["alpha_identity_assumption_v1"]
    binder_map: dict[str, str]
    premise_map: dict[str, str]
    conclusion_role_map: dict[str, str]
    direction: Literal["A_to_B", "B_to_A", "both"]

    @model_validator(mode="after")
    def _indices_not_pretty_names(self) -> ClaimAlignmentSpec:
        for label, prefix, mapping in (
            ("binder_map", "binder", self.binder_map),
            ("premise_map", "premise", self.premise_map),
        ):
            for source, target in mapping.items():
                pattern = rf"^{prefix}:(0|[1-9][0-9]*)$"
                if not re.fullmatch(pattern, source) or not re.fullmatch(pattern, target):
                    raise ValueError(
                        f"{label} must use stable {prefix}:<ordinal> indices, not pretty names"
                    )
            if len(mapping.values()) != len(set(mapping.values())):
                raise ValueError(f"{label} targets must be one-to-one")
        if set(self.conclusion_role_map) != {"A"} or set(self.conclusion_role_map.values()) != {
            "B"
        }:
            raise ValueError("v1 conclusion_role_map must be exactly {'A': 'B'}")
        return self
