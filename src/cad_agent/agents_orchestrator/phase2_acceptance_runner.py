from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..active_model_through_hole import ActiveModelThroughHoleExecutor
from ..registry import CADAgentSkillManager
from .agents_config import AgentsOrchestratorConfig
from .cad_planner_agent import CADPlannerAgent
from .recovery_agent import RecoveryAgent
from .skill_router_agent import SkillRouterAgent
from .validator_agent import ValidatorAgent


PHASE2_SEQUENCE = (
    "base_plate",
    "through_hole",
    "threaded_hole",
    "bolt_circle_pattern",
    "boss",
    "pocket",
    "slot",
    "fillet",
    "chamfer",
    "drawing",
    "autocad_annotation",
    "exports",
)


class Phase2RealAcceptanceRunner:
    """Stepwise real acceptance gate.

    This runner does not replace the existing Pipeline Runner. It calls the
    current Skill Manager only when the requested step is supported by the
    stable implementation, then stops at the first unsupported or failed step.
    """

    def __init__(
        self,
        output_root: Path,
        config: AgentsOrchestratorConfig | None = None,
        skill_manager: CADAgentSkillManager | None = None,
    ) -> None:
        self.output_root = output_root
        self.config = config or AgentsOrchestratorConfig.detect()
        self.skill_manager = skill_manager or CADAgentSkillManager(output_root)
        self.planner = CADPlannerAgent(output_root, self.config)
        self.router = SkillRouterAgent(self.config)
        self.validator = ValidatorAgent(self.config)
        self.recovery = RecoveryAgent(self.config)

    def run(self, prompt: str, execute_real_skills: bool = True, stop_after: str | None = None) -> dict[str, Any]:
        run_dir = self.output_root / f"phase2_acceptance_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        design = self.planner.plan(prompt)
        route = self.router.route(design)
        report: dict[str, Any] = {
            "status": "running",
            "prompt": prompt,
            "execute_real_skills": execute_real_skills,
            "provider": self.config.provider_name,
            "run_dir": str(run_dir),
            "design_json": design,
            "skill_route": route,
            "steps": [],
            "artifacts": {
                "design_json": str(run_dir / "design_plan.json"),
                "skill_route": str(run_dir / "skill_route.json"),
                "phase2_report": str(run_dir / "phase2_acceptance_report.json"),
            },
            "unsupported_features": list(route.get("unsupported_features", [])),
            "recovery": {},
        }
        self._write_json(run_dir / "design_plan.json", design)
        self._write_json(run_dir / "skill_route.json", route)

        for feature_type in PHASE2_SEQUENCE:
            step_record = self._run_feature_step(feature_type, design, run_dir, execute_real_skills)
            report["steps"].append(step_record)
            for key, value in step_record.get("artifacts", {}).items():
                if value:
                    report["artifacts"][key] = value
            self._write_json(run_dir / f"{len(report['steps']):02d}_{feature_type}.json", step_record)
            if step_record["status"] == "success" and stop_after == feature_type:
                report["status"] = "success"
                report["stopped_after"] = feature_type
                self._write_json(run_dir / "phase2_acceptance_report.json", report)
                return report
            if step_record["status"] != "success":
                report["status"] = "failed" if step_record.get("required", True) else "stopped"
                report["halted_at"] = feature_type
                report["recovery"] = self._phase2_recovery(step_record, report)
                self._write_json(run_dir / "phase2_acceptance_report.json", report)
                return report

        report["status"] = "success"
        self._write_json(run_dir / "phase2_acceptance_report.json", report)
        return report

    def _run_feature_step(
        self,
        feature_type: str,
        design: dict[str, Any],
        run_dir: Path,
        execute_real_skills: bool,
    ) -> dict[str, Any]:
        feature_records = self._features_for_step(feature_type, design)
        record: dict[str, Any] = {
            "feature_type": feature_type,
            "required": True,
            "input_parameters": feature_records,
            "status": "pending",
            "message": "",
            "artifacts": {},
            "execution_result": {},
        }
        if not feature_records and feature_type not in {"exports"}:
            record.update({"status": "skipped", "required": False, "message": "No feature of this type was requested."})
            return record

        support = self._support_status(feature_type, feature_records)
        record["support"] = support
        if not support["supported"]:
            record.update({"status": "failed", "message": support["reason"]})
            return record
        if not execute_real_skills:
            record.update({"status": "success", "message": "Simulated Phase 2 step.", "execution_result": {"simulated": True}})
            return record

        if feature_type == "base_plate":
            return self._execute_base_plate(record, design)
        if feature_type == "through_hole":
            return self._execute_through_hole(record)
        record.update({"status": "failed", "message": f"No real executor registered for Phase 2 feature step: {feature_type}"})
        return record

    def _execute_base_plate(self, record: dict[str, Any], design: dict[str, Any]) -> dict[str, Any]:
        base_only_plan = dict(design)
        base_only_plan["features"] = [feature for feature in design.get("features", []) if feature.get("type") == "base_plate"]
        try:
            result = self.skill_manager.run_solidworks_plan(base_only_plan)
        except Exception as exc:
            record["execution_result"] = {"success": False, "error": repr(exc)}
            record.update({"status": "failed", "message": f"SolidWorks base_plate execution raised: {exc}"})
            return record
        record["execution_result"] = {
            "success": result.success,
            "message": result.message,
            "path": result.path,
            "data": result.data,
        }
        if result.path:
            record["artifacts"]["solidworks_model"] = result.path
        if result.success and result.path and Path(result.path).is_file() and Path(result.path).stat().st_size > 0:
            record.update({"status": "success", "message": result.message})
        else:
            record.update({"status": "failed", "message": result.message})
        return record

    def _execute_through_hole(self, record: dict[str, Any]) -> dict[str, Any]:
        request = self._through_hole_request(record["input_parameters"])
        if request is None:
            record.update({"status": "failed", "message": "No through_hole feature matches active_model corner_offsets/count=4 schema."})
            return record
        record["active_model_request"] = request
        result = ActiveModelThroughHoleExecutor(self.output_root, self.skill_manager.paths.solidworks_skill_dir).run(request)
        record["execution_result"] = {
            "success": result.success,
            "message": result.message,
            "path": result.path,
            "data": result.data,
        }
        data = result.data if isinstance(result.data, dict) else {}
        record["current_skill"] = "through_hole"
        record["mode"] = "active_model"
        record["active_doc"] = data.get("active_doc")
        record["hole_centers_mm"] = data.get("hole_centers_mm", [])
        if data.get("saved_path"):
            record["artifacts"]["solidworks_model"] = data["saved_path"]
        if data.get("files"):
            record["artifacts"]["through_hole_report"] = data["files"][0]
        if result.success and self._validate_through_hole_result(data):
            record.update({"status": "success", "message": result.message, "validator": self._through_hole_validator(data)})
        else:
            record.update({"status": "failed", "message": result.message, "validator": self._through_hole_validator(data)})
        return record

    @staticmethod
    def _features_for_step(feature_type: str, design: dict[str, Any]) -> list[dict[str, Any]]:
        if feature_type == "exports":
            return [{"type": "exports", "params": {"outputs": design.get("outputs", [])}}]
        if feature_type == "through_hole":
            return [feature for feature in design.get("features", []) if feature.get("type") == "through_hole"]
        return [feature for feature in design.get("features", []) if feature.get("type") == feature_type]

    @staticmethod
    def _support_status(feature_type: str, feature_records: list[dict[str, Any]]) -> dict[str, Any]:
        if feature_type == "base_plate":
            return {"supported": True, "executor": "solidworks_automation", "reason": "Stable automation can create the base extrusion."}
        if feature_type == "through_hole":
            unsupported_reasons = []
            has_supported_corner_schema = False
            for feature in feature_records:
                params = feature.get("params", {})
                offsets = params.get("edge_offsets_mm") or params.get("edge_offsets")
                if int(params.get("count", 1) or 1) == 4 and offsets:
                    has_supported_corner_schema = True
                    continue
                if params.get("position") == "center":
                    unsupported_reasons.append("center through-hole is intentionally deferred in this acceptance run")
                elif params.get("count") and int(params.get("count", 1)) > 1:
                    unsupported_reasons.append("only count=4 corner_offsets is supported by the current active_model executor")
                else:
                    unsupported_reasons.append("unsupported through_hole placement for active_model executor")
            if not has_supported_corner_schema:
                return {
                    "supported": False,
                    "executor": "solidworks_automation",
                    "reason": "; ".join(sorted(set(unsupported_reasons))) or "No supported active_model corner_offsets through_hole request.",
                }
            return {
                "supported": True,
                "executor": "active_model_through_hole",
                "reason": "Active-model corner_offsets/count=4 through-hole executor is available; unsupported through-hole variants are deferred.",
                "deferred": sorted(set(unsupported_reasons)),
            }
        unsupported = {
            "threaded_hole": "Thread Skill currently creates an independent threaded-hole template, not threaded holes on the active mounting-base model.",
            "bolt_circle_pattern": "No integrated bolt-circle pattern executor exists for the active mounting-base model.",
            "boss": "No integrated boss executor exists in the stable Pipeline for this active model.",
            "pocket": "No integrated pocket/cavity executor exists in the stable Pipeline for this active model.",
            "slot": "No integrated slot executor exists in the stable Pipeline for this active model.",
            "fillet": "Fillet/CNC Skill currently creates an independent template and is not yet an active-model edge modifier.",
            "chamfer": "Fillet/CNC Skill currently creates an independent template and is not yet an active-model edge modifier.",
            "drawing": "Drawing generation is supported only after the full model is valid; earlier required feature steps have not passed.",
            "autocad_annotation": "AutoCAD annotation requires a real exported DWG from a valid drawing.",
            "exports": "Exports require a real completed model and drawing.",
        }
        return {"supported": False, "executor": None, "reason": unsupported.get(feature_type, "No registered Phase 2 executor.")}

    def _phase2_recovery(self, step_record: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        missing = []
        artifacts = report.get("artifacts", {})
        for key in ("solidworks_model", "solidworks_drawing", "step", "dwg", "annotated_dwg", "pdf", "autocad_annotation_report"):
            value = artifacts.get(key)
            if not value or not Path(value).exists():
                missing.append(key)
        return {
            "failed_step": step_record.get("feature_type"),
            "reason": step_record.get("message"),
            "validator": {
                "status": "failed" if step_record.get("required", True) else "partial",
                "missing": missing,
            },
            "suggestions": [
                f"Stop at {step_record.get('feature_type')} and do not execute downstream steps.",
                "Add or extend an active-model SolidWorks feature executor for this feature before retrying.",
                "Do not use independent template skills as proof that the feature exists on the requested mounting-base model.",
            ],
        }

    @staticmethod
    def _through_hole_request(features: list[dict[str, Any]]) -> dict[str, Any] | None:
        for feature in features:
            params = feature.get("params", {})
            offsets = params.get("edge_offsets_mm") or params.get("edge_offsets")
            if int(params.get("count", 1) or 1) == 4 and offsets:
                return {
                    "mode": "active_model",
                    "hole_type": "through",
                    "diameter_mm": float(params.get("diameter", 0) or 0),
                    "count": 4,
                    "placement": "corner_offsets",
                    "edge_offsets_mm": {
                        "x": float(offsets.get("x", 0) or 0),
                        "y": float(offsets.get("y", 0) or 0),
                    },
                }
        return None

    @staticmethod
    def _validate_through_hole_result(data: dict[str, Any]) -> bool:
        validator = Phase2RealAcceptanceRunner._through_hole_validator(data)
        return validator["status"] == "success"

    @staticmethod
    def _through_hole_validator(data: dict[str, Any]) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        saved_path = Path(str(data.get("saved_path", "")))
        checks["file_saved"] = saved_path.is_file() and saved_path.stat().st_size > 0 if str(saved_path) else False
        checks["reopen_success"] = bool((data.get("reopen_check") or {}).get("success"))
        checks["holes_created"] = int(data.get("holes_created", 0) or 0) == 4
        checks["diameter"] = abs(float(data.get("diameter_mm", 0) or 0) - 8.5) < 1e-6
        centers = data.get("hole_centers_mm", [])
        expected = [[-75.0, -45.0], [75.0, -45.0], [-75.0, 45.0], [75.0, 45.0]]
        checks["center_positions"] = len(centers) == 4 and all(
            any(abs(cx - ex) < 0.1 and abs(cy - ey) < 0.1 for cx, cy in centers)
            for ex, ey in expected
        )
        bbox = data.get("bbox_mm", {})
        checks["base_plate_size"] = (
            abs(float(bbox.get("length", 0) or 0) - 180.0) < 0.5
            and abs(float(bbox.get("width", 0) or 0) - 120.0) < 0.5
            and abs(float(bbox.get("thickness", 0) or 0) - 18.0) < 0.5
        )
        status = "success" if all(checks.values()) else "failed"
        return {"status": status, "checks": checks, "expected_centers_mm": expected, "actual_centers_mm": centers}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
