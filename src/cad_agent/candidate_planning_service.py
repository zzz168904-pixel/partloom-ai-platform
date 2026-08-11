from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .design_planner import DESIGN_SCHEMA, DesignPlanner
from .planner_candidate import (
    CANDIDATE_SCHEMA_VERSION,
    PLANNING_CONTRACT_VERSION,
    candidate_schema,
    candidate_to_design,
    normalize_candidate,
    parse_candidate_output,
)
from .planner_validator import PlannerValidator
from .planning_regression_store import PlanningRegressionStore


class CandidatePlanningService:
    """Provider-neutral candidate planning, normalization, and validation."""

    def __init__(self, output_root: Path, normalizer: DesignPlanner) -> None:
        self.output_root = output_root
        self.normalizer = normalizer
        self.validator = PlannerValidator()
        self.regressions = PlanningRegressionStore(output_root)

    def plan(
        self,
        prompt: str,
        provider: Any,
        stage_mode: str = "auto",
    ) -> dict[str, Any]:
        run_dir = self._run_dir()
        provider_name = str(getattr(provider, "name", "unknown_provider"))
        candidate_method = getattr(provider, "generate_candidate_output", None)
        supports_candidate = callable(candidate_method)
        raw_outputs: list[Any] = []
        provider_call_count = 0
        feedback = ""
        candidate: dict[str, Any] = self._empty_candidate(prompt)
        structural_errors: list[dict[str, Any]] = []
        design: dict[str, Any] = {}
        validation: dict[str, Any] = {}
        retry_reason = ""

        for attempt in range(2):
            try:
                provider_call_count += 1
                if supports_candidate:
                    raw = candidate_method(prompt, candidate_schema(), repair_feedback=feedback or None)
                else:
                    schema = {**DESIGN_SCHEMA, "candidate_schema": candidate_schema()}
                    raw = provider.generate_design_json(prompt, schema)
                raw_outputs.append(self._serializable(raw))
            except Exception as exc:
                raw_outputs.append({"attempt": provider_call_count, "provider_error": str(exc)})
                structural_errors = [self._issue(
                    "planner_provider_error",
                    str(exc),
                    None,
                    "provider_output",
                )]
                candidate = self._empty_candidate(prompt)
                break

            try:
                parsed = parse_candidate_output(raw)
                candidate, structural_errors = normalize_candidate(parsed, prompt)
            except Exception as exc:
                raw_outputs.append({"attempt": provider_call_count, "parse_error": str(exc)})
                structural_errors = [self._issue(
                    "planner_output_invalid_json",
                    str(exc),
                    None,
                    "provider_output",
                )]
                candidate = self._empty_candidate(prompt)
                if supports_candidate and attempt == 0:
                    retry_reason = "json_format_error"
                    feedback = (
                        f"The previous response was invalid JSON: {exc}. "
                        f"Return exactly one {CANDIDATE_SCHEMA_VERSION} JSON object and no prose."
                    )
                    continue
                break

            design = candidate_to_design(candidate, prompt)
            design = self.normalizer._normalize_design(design, prompt, stage_mode=stage_mode)
            validation = self.validator.validate(candidate, design, structural_errors)
            error_codes = {
                item.get("code")
                for item in validation.get("errors", [])
            }
            retryable = error_codes & {"unsupported_operation", "operation_not_production_executable"}
            missing_user_information = error_codes & {
                "unresolved_information",
                "hole_position_unresolved",
                "missing_required_parameter",
                "target_reference_missing",
                "target_role_missing",
                "target_role_ambiguous",
            }
            schema_retryable = error_codes & {
                "candidate_schema_version_invalid",
                "candidate_field_missing",
                "candidate_parameters_invalid",
                "candidate_list_field_invalid",
                "candidate_feature_invalid",
                "candidate_features_invalid",
                "candidate_confidence_invalid",
                "candidate_target_reference_invalid",
            }
            if supports_candidate and attempt == 0 and schema_retryable and not missing_user_information:
                retry_reason = "json_schema_error"
                feedback = (
                    "The previous JSON did not match the candidate schema. Fix these deterministic errors: "
                    + "; ".join(
                        f"{item.get('code')}: {item.get('message')}"
                        for item in validation.get("errors", [])
                        if item.get("code") in schema_retryable
                    )
                    + f". Include candidate_schema_version='{CANDIDATE_SCHEMA_VERSION}' and every required field."
                )
                continue
            if supports_candidate and attempt == 0 and retryable and not missing_user_information:
                retry_reason = "unsupported_operation"
                feedback = (
                    "The previous plan used unsupported or non-production operations. "
                    f"Use only these operations: {', '.join(candidate_schema()['operations'])}. "
                    "Do not omit requested geometry; if no valid operation can represent it, put it in unresolved."
                )
                continue
            break

        if not design:
            design = candidate_to_design(candidate, prompt)
            design = self.normalizer._normalize_design(design, prompt, stage_mode=stage_mode)
            validation = self.validator.validate(candidate, design, structural_errors)

        raw_path = run_dir / "raw_provider_outputs.json"
        candidate_path = run_dir / "candidate_plan.json"
        validation_path = run_dir / "planner_validation.json"
        raw_path.write_text(json.dumps(raw_outputs, ensure_ascii=False, indent=2), encoding="utf-8")
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
        design = self._attach_result(
            design,
            candidate,
            validation,
            provider_name,
            str(getattr(provider, "model", getattr(provider, "name", ""))),
            run_dir,
            raw_path,
            candidate_path,
            validation_path,
            provider_call_count,
            retry_reason,
        )
        if validation.get("status") != "approved":
            case_path = self.regressions.record(
                original_prompt=prompt,
                provider=provider_name,
                raw_llm_output=raw_outputs,
                normalized_cad_ir=copy.deepcopy(design.get("cad_ir") or {}),
                validation_errors=list(validation.get("errors") or []),
            )
            design["planning_artifacts"]["regression_case"] = str(case_path)
        return design

    def validate_legacy_design(
        self,
        prompt: str,
        design: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        run_dir = self._run_dir()
        candidate, structural_errors = normalize_candidate(design, prompt)
        validation = self.validator.validate(candidate, design, structural_errors)
        raw_path = run_dir / "raw_provider_outputs.json"
        candidate_path = run_dir / "candidate_plan.json"
        validation_path = run_dir / "planner_validation.json"
        raw_path.write_text(json.dumps([design], ensure_ascii=False, indent=2), encoding="utf-8")
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._attach_result(
            design,
            candidate,
            validation,
            provider_name,
            str(getattr(self.normalizer.provider, "model", getattr(self.normalizer.provider, "name", ""))),
            run_dir,
            raw_path,
            candidate_path,
            validation_path,
            1,
            "legacy_adapter",
        )

    def _attach_result(
        self,
        design: dict[str, Any],
        candidate: dict[str, Any],
        validation: dict[str, Any],
        provider_name: str,
        provider_model: str,
        run_dir: Path,
        raw_path: Path,
        candidate_path: Path,
        validation_path: Path,
        attempt_count: int,
        retry_reason: str,
    ) -> dict[str, Any]:
        result = copy.deepcopy(design)
        result["planning_contract"] = PLANNING_CONTRACT_VERSION
        result["planning_validation"] = copy.deepcopy(validation)
        result["planning_preview"] = self.validator.preview(candidate, validation, provider_name)
        result["planning_artifacts"] = {
            "run_dir": str(run_dir),
            "raw_provider_outputs": str(raw_path),
            "candidate_plan": str(candidate_path),
            "planner_validation": str(validation_path),
        }
        result["planning_retry"] = {
            "attempt_count": attempt_count,
            "retry_used": attempt_count > 1,
            "reason": retry_reason,
        }
        result["llm_provider"] = {
            "id": provider_name,
            "model": provider_model,
            "role": "candidate_planner_only",
        }
        status = str(validation.get("status") or "blocked")
        result["needs_confirmation"] = status != "approved"
        if status == "blocked":
            result["confirmation_reason"] = self._first_error(validation)
            policy = dict(result.get("execution_policy") or {})
            policy["allowed_skills"] = []
            result["execution_policy"] = policy
        elif status == "needs_confirmation":
            result["confirmation_reason"] = "Planner confidence or assumptions require user confirmation."
        return result

    def _run_dir(self) -> Path:
        path = self.output_root / f"candidate_planner_{datetime.now():%Y%m%d_%H%M%S_%f}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _empty_candidate(prompt: str) -> dict[str, Any]:
        return {
            "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
            "source_prompt": prompt,
            "part_type": "unspecified_part",
            "task_type": "model_3d",
            "parameters": {"unit": "mm"},
            "features": [],
            "outputs": [],
            "assumptions": [],
            "unresolved": ["The provider did not return a valid candidate plan."],
            "confidence": 0.0,
        }

    @staticmethod
    def _first_error(validation: dict[str, Any]) -> str:
        errors = list(validation.get("errors") or [])
        if errors:
            return str(errors[0].get("message") or "Planner validation failed.")
        return "Planner validation failed."

    @staticmethod
    def _serializable(value: Any) -> Any:
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _issue(code: str, message: str, feature_id: str | None, field: str | None) -> dict[str, Any]:
        return {"code": code, "message": message, "feature_id": feature_id, "field": field}
