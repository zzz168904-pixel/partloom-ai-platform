from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.cad_agent.project_governance import (
    DEFAULT_CURRENT_TASK_PATH,
    DEFAULT_ROADMAP_PATH,
    ProjectGovernance,
    TASK_SCHEMA_VERSION,
)


def _branch_task() -> dict:
    return {
        "schema_version": TASK_SCHEMA_VERSION,
        "id": "branch_resource_preflight",
        "title": "Fix a resource preflight regression",
        "task_kind": "branch",
        "phase_id": "public_beta",
        "objective_id": "release_hardening",
        "status": "in_progress",
        "scope": ["Repair the preflight regression without changing modeling scope."],
        "requested_changes": ["fix_executor", "add_regression"],
        "expected_artifacts": ["Focused regression test", "resource report"],
        "acceptance_tests": ["Focused test passes", "full suite passes"],
        "branch": {
            "parent_task_id": "partloom_public_beta_010",
            "branch_type": "resource_stability",
            "supports_objective_id": "release_hardening",
            "reason": "The current reference-part run is blocked by resource detection.",
            "max_iterations": 3,
            "return_to_mainline": True,
        },
    }


def test_default_roadmap_and_current_task_are_approved() -> None:
    governance = ProjectGovernance.load_default()
    status = governance.status()

    assert DEFAULT_ROADMAP_PATH.is_file()
    assert DEFAULT_CURRENT_TASK_PATH.is_file()
    assert status["current_phase"]["id"] == "public_beta"
    assert status["next_phase"] is None
    assert status["current_task"]["id"] == "partloom_public_beta_010"
    assert status["current_task_validation"]["success"] is True
    assert status["current_task_validation"]["deviation_detected"] is False


def test_valid_branch_task_remains_attached_to_current_mainline() -> None:
    result = ProjectGovernance.load_default().validate_task(_branch_task()).as_dict()

    assert result["success"] is True
    assert result["status"] == "approved"
    assert result["mainline_phase_id"] == "public_beta"
    assert result["return_target_phase_id"] == "public_beta"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda task: task.update(phase_id="future_phase"), "task_outside_current_mainline"),
        (
            lambda task: task["branch"].update(return_to_mainline=False),
            "branch_has_no_return",
        ),
        (
            lambda task: task["branch"].update(branch_type="feature_expansion"),
            "unsupported_branch_type",
        ),
        (
            lambda task: task["requested_changes"].append("direct_llm_to_executor"),
            "forbidden_phase_change",
        ),
    ],
)
def test_branch_deviation_is_blocked(mutation, expected_code: str) -> None:
    task = _branch_task()
    mutation(task)

    result = ProjectGovernance.load_default().validate_task(task).as_dict()

    assert result["success"] is False
    assert result["status"] == "blocked"
    assert result["deviation_detected"] is True
    assert expected_code in {item["code"] for item in result["errors"]}


def test_completed_branch_requires_tests_evidence_and_return() -> None:
    task = _branch_task()
    task["status"] = "completed"

    blocked = ProjectGovernance.load_default().validate_task(task).as_dict()
    assert blocked["success"] is False
    assert "missing_branch_completion" in {item["code"] for item in blocked["errors"]}

    task["completion"] = {
        "tests_passed": True,
        "returned_to_mainline": True,
        "evidence": ["tests/test_project_governance.py"],
    }
    approved = ProjectGovernance.load_default().validate_task(task).as_dict()
    assert approved["success"] is True


def test_invalid_roadmap_phase_order_fails_closed(tmp_path: Path) -> None:
    roadmap = json.loads(DEFAULT_ROADMAP_PATH.read_text(encoding="utf-8"))
    roadmap["phase_order"] = ["unknown_phase"]
    roadmap_path = tmp_path / "roadmap.json"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    with pytest.raises(ValueError, match="phase_order"):
        ProjectGovernance.from_files(roadmap_path)


def test_future_mainline_task_cannot_be_started_early() -> None:
    governance = ProjectGovernance.load_default()
    task = copy.deepcopy(governance.current_task)
    assert task is not None
    task["phase_id"] = "future_phase"
    task["objective_id"] = "future_objective"

    result = governance.validate_task(task).as_dict()

    assert result["success"] is False
    assert "task_outside_current_mainline" in {item["code"] for item in result["errors"]}
