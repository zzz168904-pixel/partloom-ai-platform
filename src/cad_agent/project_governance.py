from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROADMAP_SCHEMA_VERSION = "cad_agent.development_roadmap.v1"
TASK_SCHEMA_VERSION = "cad_agent.development_task.v1"
TASK_KINDS = {"mainline", "branch"}
TASK_STATES = {"planned", "in_progress", "blocked", "completed", "cancelled"}
ACTIVE_TASK_STATES = {"planned", "in_progress", "blocked"}

KNOWLEDGE_ROOT = Path(__file__).resolve().parent / "engineering_knowledge"
DEFAULT_ROADMAP_PATH = KNOWLEDGE_ROOT / "development_roadmap.json"
DEFAULT_CURRENT_TASK_PATH = KNOWLEDGE_ROOT / "current_development_task.json"


@dataclass(frozen=True)
class GovernanceValidation:
    success: bool
    status: str
    errors: tuple[dict[str, str], ...]
    warnings: tuple[dict[str, str], ...]
    mainline_phase_id: str
    objective_id: str
    task_kind: str
    return_target_phase_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status,
            "errors": [dict(item) for item in self.errors],
            "warnings": [dict(item) for item in self.warnings],
            "mainline_phase_id": self.mainline_phase_id,
            "objective_id": self.objective_id,
            "task_kind": self.task_kind,
            "return_target_phase_id": self.return_target_phase_id,
            "deviation_detected": not self.success,
        }


class ProjectGovernance:
    def __init__(self, roadmap: dict[str, Any], current_task: dict[str, Any] | None = None) -> None:
        self.roadmap = copy.deepcopy(roadmap)
        self.current_task = copy.deepcopy(current_task) if current_task is not None else None
        errors = self._validate_roadmap()
        if errors:
            detail = "; ".join(item["message"] for item in errors)
            raise ValueError(f"Invalid development roadmap: {detail}")

    @classmethod
    def from_files(
        cls,
        roadmap_path: Path,
        current_task_path: Path | None = None,
    ) -> "ProjectGovernance":
        roadmap = json.loads(roadmap_path.resolve().read_text(encoding="utf-8"))
        current_task = None
        if current_task_path is not None:
            current_task = json.loads(current_task_path.resolve().read_text(encoding="utf-8"))
        return cls(roadmap, current_task)

    @classmethod
    def load_default(cls) -> "ProjectGovernance":
        return cls.from_files(DEFAULT_ROADMAP_PATH, DEFAULT_CURRENT_TASK_PATH)

    @property
    def current_phase_id(self) -> str:
        return str(self.roadmap["current_phase_id"])

    def phase(self, phase_id: str) -> dict[str, Any]:
        for phase in self.roadmap["phases"]:
            if phase["id"] == phase_id:
                return phase
        raise KeyError(phase_id)

    def current_phase(self) -> dict[str, Any]:
        return self.phase(self.current_phase_id)

    def next_phase(self) -> dict[str, Any] | None:
        order = list(self.roadmap["phase_order"])
        index = order.index(self.current_phase_id)
        return self.phase(order[index + 1]) if index + 1 < len(order) else None

    @staticmethod
    def _issue(code: str, message: str) -> dict[str, str]:
        return {"code": code, "message": message}

    def _validate_roadmap(self) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        if self.roadmap.get("schema_version") != ROADMAP_SCHEMA_VERSION:
            errors.append(self._issue("invalid_schema", "Development roadmap schema is not supported."))
            return errors
        phases = self.roadmap.get("phases")
        order = self.roadmap.get("phase_order")
        if not isinstance(phases, list) or not phases:
            errors.append(self._issue("missing_phases", "Development roadmap must contain phases."))
            return errors
        if not isinstance(order, list) or not order:
            errors.append(self._issue("missing_phase_order", "Development roadmap phase_order is required."))
            return errors

        phase_ids = [str(item.get("id") or "") for item in phases if isinstance(item, dict)]
        if len(phase_ids) != len(phases) or any(not item for item in phase_ids):
            errors.append(self._issue("invalid_phase", "Every development phase requires an id."))
        if len(set(phase_ids)) != len(phase_ids):
            errors.append(self._issue("duplicate_phase", "Development phase ids must be unique."))
        if order != phase_ids:
            errors.append(self._issue("phase_order_mismatch", "phase_order must exactly match ordered phase ids."))
        current = str(self.roadmap.get("current_phase_id") or "")
        if current not in phase_ids:
            errors.append(self._issue("unknown_current_phase", "current_phase_id is not in the roadmap."))
        active = [str(item.get("id")) for item in phases if item.get("status") == "in_progress"]
        if active != [current]:
            errors.append(
                self._issue(
                    "invalid_active_phase",
                    "Exactly the current phase must have status=in_progress.",
                )
            )

        positions = {phase_id: index for index, phase_id in enumerate(phase_ids)}
        for phase in phases:
            phase_id = str(phase.get("id") or "")
            workstreams = phase.get("workstreams")
            if not isinstance(workstreams, list) or not workstreams:
                errors.append(self._issue("missing_workstreams", f"{phase_id} has no workstreams."))
            else:
                workstream_ids = [str(item.get("id") or "") for item in workstreams if isinstance(item, dict)]
                if len(workstream_ids) != len(workstreams) or len(set(workstream_ids)) != len(workstream_ids):
                    errors.append(
                        self._issue(
                            "invalid_workstreams",
                            f"{phase_id} workstream ids must be non-empty and unique.",
                        )
                    )
            if not phase.get("exit_criteria"):
                errors.append(self._issue("missing_exit_criteria", f"{phase_id} has no exit criteria."))
            for dependency in phase.get("depends_on") or []:
                dependency_id = str(dependency)
                if dependency_id not in positions:
                    errors.append(
                        self._issue(
                            "unknown_phase_dependency",
                            f"{phase_id} depends on unknown phase {dependency_id}.",
                        )
                    )
                elif positions[dependency_id] >= positions.get(phase_id, -1):
                    errors.append(
                        self._issue(
                            "invalid_phase_dependency_order",
                            f"{phase_id} depends on non-prior phase {dependency_id}.",
                        )
                    )
        return errors

    def validate_task(self, task: dict[str, Any]) -> GovernanceValidation:
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        phase_id = str(task.get("phase_id") or "")
        objective_id = str(task.get("objective_id") or "")
        task_kind = str(task.get("task_kind") or "")
        state = str(task.get("status") or "")

        if task.get("schema_version") != TASK_SCHEMA_VERSION:
            errors.append(self._issue("invalid_task_schema", "Development task schema is not supported."))
        if not str(task.get("id") or "").strip():
            errors.append(self._issue("missing_task_id", "Development task id is required."))
        if task_kind not in TASK_KINDS:
            errors.append(self._issue("invalid_task_kind", "task_kind must be mainline or branch."))
        if state not in TASK_STATES:
            errors.append(self._issue("invalid_task_state", "Development task status is not supported."))

        phase_ids = set(self.roadmap["phase_order"])
        if phase_id not in phase_ids:
            errors.append(self._issue("unknown_task_phase", f"Task phase {phase_id!r} is not in the roadmap."))
            phase = None
        else:
            phase = self.phase(phase_id)
        if state in ACTIVE_TASK_STATES and phase_id != self.current_phase_id:
            errors.append(
                self._issue(
                    "task_outside_current_mainline",
                    f"Active task must support current phase {self.current_phase_id}.",
                )
            )

        workstream_ids = {
            str(item.get("id") or "")
            for item in (phase or {}).get("workstreams") or []
            if isinstance(item, dict)
        }
        if objective_id not in workstream_ids:
            errors.append(
                self._issue(
                    "unknown_objective",
                    f"Task objective {objective_id!r} is not a workstream in phase {phase_id!r}.",
                )
            )

        for field in ("scope", "expected_artifacts", "acceptance_tests"):
            value = task.get(field)
            if not isinstance(value, list) or not any(str(item).strip() for item in value):
                errors.append(self._issue(f"missing_{field}", f"Development task {field} must be non-empty."))

        requested_changes = {
            str(item).strip()
            for item in task.get("requested_changes") or []
            if str(item).strip()
        }
        forbidden_changes = {
            str(item).strip()
            for item in (phase or {}).get("forbidden_changes") or []
            if str(item).strip()
        }
        forbidden_requested = sorted(requested_changes & forbidden_changes)
        if forbidden_requested:
            errors.append(
                self._issue(
                    "forbidden_phase_change",
                    f"Task requests changes forbidden in the current phase: {forbidden_requested}.",
                )
            )

        if task_kind == "branch":
            branch = task.get("branch")
            policy = self.roadmap.get("branch_policy") or {}
            if not isinstance(branch, dict):
                errors.append(self._issue("missing_branch_policy", "Branch task requires a branch object."))
            else:
                for field in policy.get("required_fields") or []:
                    if branch.get(field) in (None, "", [], {}):
                        errors.append(
                            self._issue(
                                "missing_branch_field",
                                f"Branch task requires branch.{field}.",
                            )
                        )
                allowed_types = {str(item) for item in policy.get("allowed_types") or []}
                if str(branch.get("branch_type") or "") not in allowed_types:
                    errors.append(
                        self._issue(
                            "unsupported_branch_type",
                            f"Branch type {branch.get('branch_type')!r} is not allowed.",
                        )
                    )
                if str(branch.get("supports_objective_id") or "") != objective_id:
                    errors.append(
                        self._issue(
                            "branch_objective_mismatch",
                            "Branch supports_objective_id must match the current task objective.",
                        )
                    )
                if branch.get("return_to_mainline") is not True:
                    errors.append(
                        self._issue(
                            "branch_has_no_return",
                            "Branch task must declare return_to_mainline=true.",
                        )
                    )
                max_iterations = branch.get("max_iterations")
                if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or not 1 <= max_iterations <= 20:
                    errors.append(
                        self._issue(
                            "invalid_branch_timebox",
                            "Branch max_iterations must be an integer from 1 to 20.",
                        )
                    )
                if state == "completed":
                    completion = task.get("completion")
                    if not isinstance(completion, dict):
                        errors.append(
                            self._issue(
                                "missing_branch_completion",
                                "Completed branch task requires completion evidence.",
                            )
                        )
                    else:
                        if completion.get("tests_passed") is not True:
                            errors.append(
                                self._issue(
                                    "branch_tests_not_passed",
                                    "Completed branch task requires tests_passed=true.",
                                )
                            )
                        if completion.get("returned_to_mainline") is not True:
                            errors.append(
                                self._issue(
                                    "branch_not_returned",
                                    "Completed branch task requires returned_to_mainline=true.",
                                )
                            )
                        if not completion.get("evidence"):
                            errors.append(
                                self._issue(
                                    "branch_missing_evidence",
                                    "Completed branch task requires evidence.",
                                )
                            )
        elif task.get("branch"):
            warnings.append(
                self._issue(
                    "unused_branch_metadata",
                    "Mainline task contains branch metadata that will be ignored.",
                )
            )

        success = not errors
        return GovernanceValidation(
            success=success,
            status="approved" if success else "blocked",
            errors=tuple(errors),
            warnings=tuple(warnings),
            mainline_phase_id=self.current_phase_id,
            objective_id=objective_id,
            task_kind=task_kind,
            return_target_phase_id=self.current_phase_id,
        )

    def status(self) -> dict[str, Any]:
        current = self.current_phase()
        following = self.next_phase()
        current_task_validation = (
            self.validate_task(self.current_task).as_dict()
            if self.current_task is not None
            else None
        )
        return {
            "schema_version": ROADMAP_SCHEMA_VERSION,
            "roadmap_version": self.roadmap.get("roadmap_version"),
            "product_goal": self.roadmap.get("product_goal"),
            "current_phase": {
                "id": current["id"],
                "order": current["order"],
                "name": current["name"],
                "status": current["status"],
                "objective": current["objective"],
                "workstreams": copy.deepcopy(current["workstreams"]),
                "exit_criteria": list(current["exit_criteria"]),
                "forbidden_changes": list(current.get("forbidden_changes") or []),
            },
            "next_phase": (
                {
                    "id": following["id"],
                    "order": following["order"],
                    "name": following["name"],
                }
                if following is not None
                else None
            ),
            "current_task": copy.deepcopy(self.current_task),
            "current_task_validation": current_task_validation,
            "branch_policy": copy.deepcopy(self.roadmap["branch_policy"]),
            "global_rules": list(self.roadmap["global_rules"]),
        }

