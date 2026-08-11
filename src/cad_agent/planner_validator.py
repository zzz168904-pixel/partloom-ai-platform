from __future__ import annotations

import copy
import math
from typing import Any

from .cad_ir import CADIRCompiler
from .planner_candidate import PLANNING_CONTRACT_VERSION


class ProductionCapabilityRegistry:
    """Read-only capability view derived from the existing production plan."""

    @staticmethod
    def registered_operations() -> set[str]:
        return set(CADIRCompiler.STANDARD_OPERATIONS)

    @staticmethod
    def rejected_required_features(design: dict[str, Any]) -> list[dict[str, Any]]:
        policy = design.get("execution_policy") or {}
        return list(policy.get("unexecutable_required_features") or [])


class PlannerValidator:
    VAGUE_TARGET_ROLES = {
        "top",
        "front",
        "right",
        "bottom",
        "left",
        "base_top",
        "top_face",
        "front_face",
        "right_face",
    }
    NEW_BODY_OPERATIONS = {"base_plate"} | set(CADIRCompiler.NEW_DOCUMENT_OPERATIONS)

    def validate(
        self,
        candidate: dict[str, Any],
        design: dict[str, Any],
        structural_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        errors = list(structural_errors or [])
        warnings: list[dict[str, Any]] = []
        registered = ProductionCapabilityRegistry.registered_operations()
        features = list(candidate.get("features") or [])
        positions = {str(item.get("id")): index for index, item in enumerate(features)}
        unsupported_operations: list[str] = []
        feature_checks: list[dict[str, Any]] = []

        for index, feature in enumerate(features):
            feature_id = str(feature.get("id") or "")
            operation = str(feature.get("operation") or "")
            feature_errors: list[str] = []
            if operation not in registered:
                unsupported_operations.append(operation)
                errors.append(self._issue(
                    "unsupported_operation",
                    f"Operation {operation!r} is not in the CAD-IR capability catalog.",
                    feature_id,
                    "operation",
                ))
                feature_errors.append("unsupported_operation")

            for dependency in list(feature.get("depends_on") or []):
                if dependency not in positions:
                    errors.append(self._issue(
                        "dependency_not_found",
                        f"Dependency {dependency!r} does not exist.",
                        feature_id,
                        "depends_on",
                    ))
                    feature_errors.append("dependency_not_found")
                elif positions[dependency] >= index:
                    errors.append(self._issue(
                        "dependency_order_invalid",
                        f"Dependency {dependency!r} must precede {feature_id!r}.",
                        feature_id,
                        "depends_on",
                    ))
                    feature_errors.append("dependency_order_invalid")

            target_body = str(feature.get("target_body") or "").strip()
            if not target_body:
                errors.append(self._issue(
                    "target_body_missing",
                    "target_body is required.",
                    feature_id,
                    "target_body",
                ))
                feature_errors.append("target_body_missing")

            if self._requires_target_reference(feature):
                target = feature.get("target_reference")
                if not isinstance(target, dict):
                    errors.append(self._issue(
                        "target_reference_missing",
                        "A modifier requires a semantic target_reference.",
                        feature_id,
                        "target_reference",
                    ))
                    feature_errors.append("target_reference_missing")
                else:
                    target_feature = str(target.get("feature_id") or "").strip()
                    role = str(target.get("role") or "").strip()
                    if target_feature not in positions:
                        errors.append(self._issue(
                            "target_reference_not_found",
                            f"Target feature {target_feature!r} does not exist.",
                            feature_id,
                            "target_reference.feature_id",
                        ))
                        feature_errors.append("target_reference_not_found")
                    elif positions[target_feature] >= index:
                        errors.append(self._issue(
                            "target_reference_order_invalid",
                            f"Target feature {target_feature!r} must precede {feature_id!r}.",
                            feature_id,
                            "target_reference.feature_id",
                        ))
                        feature_errors.append("target_reference_order_invalid")
                    if not role:
                        errors.append(self._issue(
                            "target_role_missing",
                            "target_reference.role is required.",
                            feature_id,
                            "target_reference.role",
                        ))
                        feature_errors.append("target_role_missing")
                    elif role.lower() in self.VAGUE_TARGET_ROLES:
                        errors.append(self._issue(
                            "target_role_ambiguous",
                            f"Target role {role!r} is too vague for production execution.",
                            feature_id,
                            "target_reference.role",
                        ))
                        feature_errors.append("target_role_ambiguous")

            if operation == "through_hole":
                params = feature.get("parameters") or {}
                has_location = any(
                    params.get(key) not in (None, "", [], {})
                    for key in ("position", "placement", "position_xy", "center_uv_mm", "edge_offsets_mm")
                )
                if not has_location:
                    errors.append(self._issue(
                        "hole_position_unresolved",
                        "Through-hole position is missing; the planner may not guess it.",
                        feature_id,
                        "parameters.position",
                    ))
                    feature_errors.append("hole_position_unresolved")

            feature_checks.append({
                "id": feature_id,
                "operation": operation,
                "valid": not feature_errors,
                "errors": feature_errors,
            })

        cad_ir = design.get("cad_ir") or {}
        if cad_ir.get("unit_system") != "mm":
            errors.append(self._issue(
                "unit_system_not_normalized",
                "CAD-IR linear units must be normalized to mm.",
                None,
                "cad_ir.unit_system",
            ))
        for item in list(cad_ir.get("errors") or []):
            errors.append(copy.deepcopy(item))

        for item in ProductionCapabilityRegistry.rejected_required_features(design):
            operation = str(item.get("type") or "")
            unsupported_operations.append(operation)
            errors.append(self._issue(
                "operation_not_production_executable",
                str(item.get("reason") or f"Operation {operation!r} has no compatible production executor."),
                str(item.get("name") or operation),
                "operation",
            ))

        assumptions = self._collect(candidate, "assumptions")
        unresolved = self._collect(candidate, "unresolved")
        for text in assumptions:
            warnings.append(self._issue(
                "assumption_requires_confirmation",
                text,
                None,
                "assumptions",
            ))
        for text in unresolved:
            errors.append(self._issue(
                "unresolved_information",
                text,
                None,
                "unresolved",
            ))

        model_confidences = [self._finite_confidence(candidate.get("confidence"))]
        model_confidences.extend(self._finite_confidence(item.get("confidence")) for item in features)
        model_confidence = min(model_confidences) if model_confidences else 0.0
        errors = self._dedupe_issues(errors)
        warnings = self._dedupe_issues(warnings)
        unsupported_operations = sorted(set(filter(None, unsupported_operations)))

        hard_block = bool(errors or unresolved or model_confidence < 0.70)
        if hard_block:
            status = "blocked"
            confirmation_allowed = False
            allow_pipeline = False
        elif assumptions or model_confidence < 0.90:
            status = "needs_confirmation"
            confirmation_allowed = True
            allow_pipeline = False
        else:
            status = "approved"
            confirmation_allowed = True
            allow_pipeline = True

        effective_confidence = 0.0 if errors else model_confidence
        return {
            "validator": "deterministic_planner_validator.v1",
            "validation_passed": not errors,
            "status": status,
            "model_confidence": model_confidence,
            "effective_confidence": effective_confidence,
            "confidence_policy": "deterministic_validation_overrides_model_confidence",
            "requires_user_confirmation": status == "needs_confirmation",
            "confirmation_allowed": confirmation_allowed,
            "user_confirmed": False,
            "allow_pipeline": allow_pipeline,
            "unit_system": {"linear": "mm", "angle": "deg"},
            "errors": errors,
            "warnings": warnings,
            "assumptions": assumptions,
            "unresolved": unresolved,
            "unsupported_operations": unsupported_operations,
            "feature_checks": feature_checks,
        }

    @staticmethod
    def _requires_target_reference(feature: dict[str, Any]) -> bool:
        operation = str(feature.get("operation") or "")
        if operation not in CADIRCompiler.MODIFIER_OPERATIONS:
            return False
        if operation != "profile_extrude":
            return True
        params = feature.get("parameters") or {}
        body_operation = str(
            params.get("body_operation")
            or params.get("operation")
            or "base"
        ).strip().lower()
        execution_mode = str(
            params.get("execution_mode")
            or params.get("mode")
            or ("new_model" if body_operation == "base" else "active_model")
        ).strip().lower()
        return body_operation != "base" or execution_mode == "active_model"

    @staticmethod
    def authorize_after_confirmation(design: dict[str, Any]) -> dict[str, Any]:
        updated = copy.deepcopy(design)
        decision = dict(updated.get("planning_validation") or {})
        if updated.get("planning_contract") != PLANNING_CONTRACT_VERSION:
            raise ValueError("planning_contract_missing")
        if decision.get("allow_pipeline") is True:
            decision["user_confirmed"] = True
            updated["planning_validation"] = decision
            return updated
        if decision.get("status") != "needs_confirmation" or not decision.get("confirmation_allowed"):
            raise ValueError("planner_validation_does_not_allow_confirmation")
        if decision.get("errors") or decision.get("unresolved"):
            raise ValueError("planner_validation_contains_blocking_errors")
        decision.update({
            "status": "approved_by_user",
            "requires_user_confirmation": False,
            "user_confirmed": True,
            "allow_pipeline": True,
        })
        updated["planning_validation"] = decision
        updated["needs_confirmation"] = False
        updated["confirmation_reason"] = ""
        from .stage_planner import apply_stage_plan

        policy = dict(updated.get("execution_policy") or {})
        updated = apply_stage_plan(updated, {
            "task_type": updated.get("task_type") or "model_3d",
            "requested_stages": list(updated.get("requested_stages") or ["model_3d"]),
            "forbidden_stages": list(updated.get("forbidden_stages") or []),
            "stop_after": updated.get("stop_after") or "model_3d",
            "requested_outputs": list(policy.get("expected_outputs") or updated.get("outputs") or []),
            "needs_confirmation": False,
            "confirmation_reason": "",
            "stage_mode": "confirmed_candidate",
        })
        if updated.get("execution_policy", {}).get("unexecutable_required_features"):
            raise ValueError("confirmed_plan_contains_unexecutable_features")
        updated["planning_validation"] = decision
        updated["needs_confirmation"] = False
        updated["confirmation_reason"] = ""
        return updated

    @staticmethod
    def execution_gate(design: dict[str, Any]) -> dict[str, Any]:
        if design.get("planning_contract") != PLANNING_CONTRACT_VERSION:
            return {"success": True, "reason": "legacy_non_candidate_plan"}
        decision = design.get("planning_validation") or {}
        if decision.get("allow_pipeline") is True and decision.get("validation_passed") is True:
            return {"success": True, "reason": "candidate_plan_authorized"}
        return {
            "success": False,
            "reason": "planner_validation_failed",
            "status": decision.get("status"),
            "errors": list(decision.get("errors") or []),
            "unresolved": list(decision.get("unresolved") or []),
            "requires_user_confirmation": bool(decision.get("requires_user_confirmation")),
        }

    @staticmethod
    def preview(candidate: dict[str, Any], validation: dict[str, Any], provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "part_type": candidate.get("part_type"),
            "task_type": candidate.get("task_type"),
            "confidence": validation.get("model_confidence"),
            "effective_confidence": validation.get("effective_confidence"),
            "status": validation.get("status"),
            "allow_execution": validation.get("allow_pipeline"),
            "confirmation_allowed": validation.get("confirmation_allowed"),
            "features": [
                {
                    "id": item.get("id"),
                    "operation": item.get("operation"),
                    "parameters": copy.deepcopy(item.get("parameters") or {}),
                    "depends_on": list(item.get("depends_on") or []),
                    "target_body": item.get("target_body"),
                    "target_reference": copy.deepcopy(item.get("target_reference")),
                    "evidence": list(item.get("evidence") or []),
                    "assumptions": list(item.get("assumptions") or []),
                    "unresolved": list(item.get("unresolved") or []),
                    "confidence": item.get("confidence"),
                }
                for item in candidate.get("features", [])
            ],
            "assumptions": list(validation.get("assumptions") or []),
            "unresolved": list(validation.get("unresolved") or []),
            "unsupported_operations": list(validation.get("unsupported_operations") or []),
            "errors": list(validation.get("errors") or []),
            "warnings": list(validation.get("warnings") or []),
        }

    @staticmethod
    def _collect(candidate: dict[str, Any], field: str) -> list[str]:
        values = [str(item) for item in list(candidate.get(field) or []) if str(item).strip()]
        for feature in candidate.get("features", []):
            values.extend(str(item) for item in list(feature.get(field) or []) if str(item).strip())
        return list(dict.fromkeys(values))

    @staticmethod
    def _finite_confidence(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(numeric):
            return 0.0
        return min(1.0, max(0.0, numeric))

    @staticmethod
    def _dedupe_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in items:
            normalized = {
                "code": str(item.get("code") or "validation_error"),
                "message": str(item.get("message") or "Validation failed."),
                "feature_id": item.get("feature_id"),
                "field": item.get("field"),
            }
            key = (
                normalized["code"],
                normalized["message"],
                str(normalized["feature_id"] or ""),
                str(normalized["field"] or ""),
            )
            if key not in seen:
                seen.add(key)
                result.append(normalized)
        return result

    @staticmethod
    def _issue(code: str, message: str, feature_id: str | None, field: str | None) -> dict[str, Any]:
        return {
            "code": code,
            "message": message,
            "feature_id": feature_id,
            "field": field,
        }
