"""Fresh-process import regressions for dependency boundaries.

These checks intentionally use a child interpreter.  The regular test suite
imports many modules during collection, which can hide cycles whose outcome
depends on import order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module_name",
    (
        "leanfaith.transforms.provisional_pair_combine",
        "leanfaith.transforms.composition_receipt_export",
    ),
)
def test_composition_modules_import_in_fresh_process(module_name: str) -> None:
    completed = subprocess.run(
        (sys.executable, "-c", f"import {module_name}"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "statement",
    (
        (
            "import leanfaith.transforms.provisional_pair_combine; "
            "from leanfaith.datasets import "
            "ExperimentalMachineSupervisionConfig, "
            "freeze_experimental_machine_supervision; "
            "assert ExperimentalMachineSupervisionConfig.__name__; "
            "assert callable(freeze_experimental_machine_supervision)"
        ),
        (
            "from leanfaith.datasets import "
            "ExperimentalMachineSupervisionConfig, "
            "freeze_experimental_machine_supervision; "
            "import leanfaith.transforms.provisional_pair_combine; "
            "assert ExperimentalMachineSupervisionConfig.__name__; "
            "assert callable(freeze_experimental_machine_supervision)"
        ),
        (
            "import leanfaith.datasets as datasets; "
            "assert 'ExperimentalMachineSupervisionConfig' in dir(datasets); "
            "assert 'freeze_experimental_machine_supervision' in dir(datasets)"
        ),
    ),
)
def test_lazy_dataset_public_api_works_in_fresh_process(statement: str) -> None:
    completed = subprocess.run(
        (sys.executable, "-c", statement),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
