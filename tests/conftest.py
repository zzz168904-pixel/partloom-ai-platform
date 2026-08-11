from __future__ import annotations

import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GENERATED_TEST_DIRS = (
    ROOT / "logs" / "test_skill_outputs",
    ROOT / "logs" / "active_feature_tests",
    ROOT / "logs" / "production_scope_tests",
    ROOT / "logs" / "gateway_http_acceptance",
    ROOT / "logs" / "gateway_tasks",
)


@pytest.fixture(scope="session", autouse=True)
def remove_generated_test_outputs() -> None:
    for path in GENERATED_TEST_DIRS:
        shutil.rmtree(path, ignore_errors=True)
    yield
    for path in GENERATED_TEST_DIRS:
        shutil.rmtree(path, ignore_errors=True)
