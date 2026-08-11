from __future__ import annotations

import json
import math
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .assembly_mate_skill import AssemblyMateSkill
from .gear_skill import GearSkill
from .models import SkillResult


class GearPairSkill:
    """Create two standard spur gears and one native Gear Mate assembly."""

    FEATURE_TYPE = "gear_pair"

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == self.FEATURE_TYPE]
        requested_run_dir = str(plan.get("execution_gear_pair_dir") or "").strip()
        run_dir = Path(requested_run_dir) if requested_run_dir else self.output_root / f"gear_pair_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "gear_pair_report.json"
        if len(features) != 1:
            return self._result(False, "gear_pair requires exactly one explicit gear-pair feature.", report_path, {})

        request = self.normalize_request(features[0].get("params", {}), plan.get("task_type"))
        if not request.get("success"):
            return self._result(False, str(request.get("message")), report_path, {
                "feature_type": self.FEATURE_TYPE,
                "invalid_request": request,
            })

        try:
            component_dir = run_dir / "components"
            component_dir.mkdir(parents=True, exist_ok=True)
            part_a = component_dir / f"{self._safe_stem(request['gear_a']['name'])}.SLDPRT"
            part_b = component_dir / f"{self._safe_stem(request['gear_b']['name'])}.SLDPRT"
            assembly_path = run_dir / f"{self._safe_stem(features[0].get('name') or 'gear_pair')}.SLDASM"
            material = str(
                features[0].get("params", {}).get("material")
                or plan.get("parameters", {}).get("material")
                or ""
            ).strip()

            gear_runner = GearSkill(run_dir / "gear_runs", self.solidworks_skill_dir)
            gear_results: list[dict[str, Any]] = []
            for label, gear_request, path in (
                ("gear_a", request["gear_a"], part_a),
                ("gear_b", request["gear_b"], part_b),
            ):
                gear_plan = {
                    "task_type": "model_3d",
                    "parameters": {"unit": "mm", "material": material},
                    "features": [{
                        "name": gear_request["name"],
                        "type": "gear",
                        "required": True,
                        "params": gear_request,
                    }],
                    "execution_model_path": str(path),
                    "execution_close_after_save": True,
                }
                result = gear_runner.run_plan(gear_plan)
                gear_results.append({
                    "label": label,
                    "success": result.success,
                    "message": result.message,
                    "path": str(path),
                    "report_path": result.path,
                })
                if not result.success:
                    return self._result(False, f"{label} creation failed: {result.message}", report_path, {
                        "feature_type": self.FEATURE_TYPE,
                        "request": request,
                        "gear_results": gear_results,
                        "files": self._existing_files(part_a, part_b),
                    })

            assembly_request = self._assembly_request(request, part_a, part_b)
            assembly_runner = AssemblyMateSkill(run_dir / "assembly_runs", self.solidworks_skill_dir)
            assembly_result = assembly_runner.run_plan({
                "task_type": "model_3d",
                "features": [{
                    "name": "GearPairAssembly",
                    "type": "assembly_mate",
                    "required": True,
                    "params": assembly_request,
                }],
                "execution_assembly_path": str(assembly_path),
            })
            if not assembly_result.success:
                return self._result(False, f"Gear-pair assembly failed: {assembly_result.message}", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "request": request,
                    "gear_results": gear_results,
                    "assembly_result": assembly_result.data,
                    "files": self._existing_files(part_a, part_b, assembly_path),
                })

            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member

            _sw, model = connect_solidworks(visible=True)
            if model is None or int(get_com_member(model, "GetType") or 0) != 2:
                return self._result(False, "Gear-pair assembly is not the active SolidWorks document.", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "request": request,
                    "files": self._existing_files(part_a, part_b, assembly_path),
                })

            centers = self._component_centers(model)
            center_validation = self._validate_center_distance(
                centers,
                part_a,
                part_b,
                float(request["center_distance_mm"]),
            )
            interference = self._interference_check(model)
            mate_validation = self._validate_mates(assembly_result.data, request)
            geometry_validation = {
                "success": bool(
                    center_validation.get("success")
                    and interference.get("success")
                    and mate_validation.get("success")
                ),
                "center_distance": center_validation,
                "interference": interference,
                "gear_mate": mate_validation,
                "transmission_ratio": request["transmission_ratio"],
            }
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            if not geometry_validation["success"]:
                return self._result(False, "Gear-pair validation failed.", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "request": request,
                    "gear_results": gear_results,
                    "assembly_result": assembly_result.data,
                    "geometry_validation": geometry_validation,
                    "files": self._existing_files(part_a, part_b, assembly_path),
                })

            operation = {
                "success": True,
                "name": str(features[0].get("name") or "GearPair"),
                "type": self.FEATURE_TYPE,
                "request": request,
                "feature_name": "GearRatio",
                "geometry_validation": geometry_validation,
            }
            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": "new_assembly",
                "feature_type": self.FEATURE_TYPE,
                "feature_created": True,
                "features_created": 1,
                "gear_results": gear_results,
                "assembly_result": assembly_result.data,
                "operations": [operation],
                "material": material,
                "material_metadata": bool(material),
                "center_distance_mm": request["center_distance_mm"],
                "transmission_ratio": request["transmission_ratio"],
                "interference_count": interference["count"],
                "saved_by_this_skill": True,
                "side_effects": {
                    "modifies_active_doc": False,
                    "creates_new_doc": True,
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": self._existing_files(part_a, part_b, assembly_path),
            }
            return self._result(True, "SolidWorks spur-gear pair created, mated, and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"gear_pair failed: {exc}", report_path, {
                "feature_type": self.FEATURE_TYPE,
                "request": request,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        mode = str(params.get("mode") or "new_assembly").strip().lower()
        if mode != "new_assembly" or task_type == "modify_3d":
            return {"success": False, "message": "gear_pair supports mode=new_assembly only."}
        gear_a_raw = params.get("gear_a") or params.get("driver")
        gear_b_raw = params.get("gear_b") or params.get("driven")
        if not isinstance(gear_a_raw, dict) or not isinstance(gear_b_raw, dict):
            return {"success": False, "message": "gear_pair requires explicit gear_a and gear_b objects."}

        common = {
            "mode": "new_model",
            "gear_type": params.get("gear_type", "spur"),
            "module_mm": params.get("module_mm", params.get("module")),
            "pressure_angle_deg": params.get("pressure_angle_deg", params.get("pressure_angle", 20)),
            "face_width_mm": params.get("face_width_mm", params.get("face_width")),
            "backlash_mm": params.get("backlash_mm", params.get("backlash", 0)),
        }
        gears: list[dict[str, Any]] = []
        for index, raw in enumerate((gear_a_raw, gear_b_raw)):
            merged = {key: value for key, value in common.items() if value is not None}
            merged.update(raw)
            merged["mode"] = "new_model"
            normalized = GearSkill.normalize_request(merged, "model_3d")
            if not normalized.get("success"):
                return {
                    "success": False,
                    "message": f"gear_{'a' if index == 0 else 'b'} is invalid: {normalized.get('message')}",
                    "invalid_gear": normalized,
                }
            if normalized["bore_diameter_mm"] <= 0:
                return {"success": False, "message": "Both gear_pair components require a positive bore_diameter_mm for Gear Mate selection."}
            normalized["name"] = str(raw.get("name") or ("DriverGear" if index == 0 else "DrivenGear"))
            gears.append(normalized)

        gear_a, gear_b = gears
        module_tolerance = 1e-9
        if abs(gear_a["module_mm"] - gear_b["module_mm"]) > module_tolerance:
            return {"success": False, "message": "Mating spur gears must use the same module."}
        if abs(gear_a["pressure_angle_deg"] - gear_b["pressure_angle_deg"]) > 1e-9:
            return {"success": False, "message": "Mating spur gears must use the same pressure angle."}

        theoretical = gear_a["module_mm"] * (gear_a["teeth"] + gear_b["teeth"]) / 2.0
        requested = cls._number(params, "center_distance_mm", "center_distance")
        center_distance = theoretical if requested is None else requested
        tolerance = max(0.02, float(gear_a["module_mm"]) * 0.01)
        if abs(center_distance - theoretical) > tolerance:
            return {
                "success": False,
                "message": "center_distance_mm does not match the standard zero-profile-shift center distance.",
                "requested_center_distance_mm": center_distance,
                "theoretical_center_distance_mm": theoretical,
                "tolerance_mm": tolerance,
            }
        interference_check = bool(params.get("interference_check", True))
        if not interference_check:
            return {"success": False, "message": "The production gear_pair contract requires interference_check=true."}

        phase_b = 180.0 / gear_b["teeth"] if gear_b["teeth"] % 2 == 0 else 0.0
        return {
            "success": True,
            "mode": "new_assembly",
            "gear_type": "spur",
            "gear_a": gear_a,
            "gear_b": gear_b,
            "center_distance_mm": float(theoretical),
            "center_distance_tolerance_mm": tolerance,
            "initial_phase_b_deg": phase_b,
            "transmission_ratio": float(gear_b["teeth"]) / float(gear_a["teeth"]),
            "ratio_definition": "driven_teeth/driver_teeth",
            "interference_check": True,
        }

    @staticmethod
    def _assembly_request(request: dict[str, Any], part_a: Path, part_b: Path) -> dict[str, Any]:
        gear_a = request["gear_a"]
        gear_b = request["gear_b"]
        component_a = "driver"
        component_b = "driven"
        return {
            "mode": "new_assembly",
            "components": [
                {"id": component_a, "path": str(part_a), "position_mm": [0, 0, 0]},
                {
                    "id": component_b,
                    "path": str(part_b),
                    "position_mm": [request["center_distance_mm"], 0, 0],
                    "rotation_z_deg": request["initial_phase_b_deg"],
                },
            ],
            "mates": [
                {
                    "name": "GearFacesAligned",
                    "mate_type": "coincident",
                    "entity_a": {"component": component_a, "selector_type": "reference_plane", "plane": "front"},
                    "entity_b": {"component": component_b, "selector_type": "reference_plane", "plane": "front"},
                },
                {
                    "name": "GearCenterDistance",
                    "mate_type": "distance",
                    "distance_mm": request["center_distance_mm"],
                    "entity_a": {"component": component_a, "selector_type": "reference_axis", "name": "GearAxis"},
                    "entity_b": {"component": component_b, "selector_type": "reference_axis", "name": "GearAxis"},
                },
                {
                    "name": "GearCenterlinePlane",
                    "mate_type": "coincident",
                    "entity_a": {"component": component_a, "selector_type": "reference_plane", "plane": "top"},
                    "entity_b": {"component": component_b, "selector_type": "reference_axis", "name": "GearAxis"},
                },
                {
                    "name": "GearRatio",
                    "mate_type": "gear",
                    "gear_teeth_a": gear_a["teeth"],
                    "gear_teeth_b": gear_b["teeth"],
                    "entity_a": {
                        "component": component_a,
                        "selector_type": "cylinder",
                        "radius_mm": gear_a["bore_diameter_mm"] / 2.0,
                        "tolerance_mm": 0.05,
                    },
                    "entity_b": {
                        "component": component_b,
                        "selector_type": "cylinder",
                        "radius_mm": gear_b["bore_diameter_mm"] / 2.0,
                        "tolerance_mm": 0.05,
                    },
                },
            ],
        }

    @classmethod
    def _component_centers(cls, model: Any) -> dict[str, list[float]]:
        values = ActiveModelThroughHoleExecutor._com_member(model, "GetComponents", True) or ()
        if not isinstance(values, (tuple, list)):
            values = (values,)
        centers: dict[str, list[float]] = {}
        for component in values:
            path = str(ActiveModelThroughHoleExecutor._com_member(component, "GetPathName", default="") or "")
            transform = ActiveModelThroughHoleExecutor._com_member(component, "Transform2")
            array = ActiveModelThroughHoleExecutor._com_member(transform, "ArrayData") if transform is not None else None
            if path and isinstance(array, (tuple, list)) and len(array) >= 12:
                centers[str(Path(path).resolve()).casefold()] = [float(array[9]) * 1000.0, float(array[10]) * 1000.0, float(array[11]) * 1000.0]
        return centers

    @staticmethod
    def _validate_center_distance(
        centers: dict[str, list[float]],
        part_a: Path,
        part_b: Path,
        expected_mm: float,
    ) -> dict[str, Any]:
        center_a = centers.get(str(part_a.resolve()).casefold())
        center_b = centers.get(str(part_b.resolve()).casefold())
        if center_a is None or center_b is None:
            return {"success": False, "message": "Could not read both component transforms.", "centers_mm": centers}
        actual = math.dist(center_a[:2], center_b[:2])
        tolerance = max(0.05, expected_mm * 0.001)
        cross_axis_offset = abs(center_b[1] - center_a[1])
        direction = center_b[0] - center_a[0]
        return {
            "success": abs(actual - expected_mm) <= tolerance and cross_axis_offset <= tolerance and direction > 0.0,
            "expected_mm": expected_mm,
            "actual_mm": actual,
            "tolerance_mm": tolerance,
            "cross_axis_offset_mm": cross_axis_offset,
            "positive_x_direction": direction > 0.0,
            "driver_center_mm": center_a,
            "driven_center_mm": center_b,
        }

    @staticmethod
    def _validate_mates(assembly_data: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        operations = [item for item in assembly_data.get("operations", []) if item.get("success")]
        names = {str(item.get("name")) for item in operations}
        gear_ops = [item for item in operations if item.get("mate_type") == "gear"]
        ratio_ok = bool(
            len(gear_ops) == 1
            and float(gear_ops[0]["request"]["gear_teeth_a"]) == float(request["gear_a"]["teeth"])
            and float(gear_ops[0]["request"]["gear_teeth_b"]) == float(request["gear_b"]["teeth"])
        )
        expected = {"GearFacesAligned", "GearCenterDistance", "GearCenterlinePlane", "GearRatio"}
        return {
            "success": expected.issubset(names) and ratio_ok,
            "expected_mates": sorted(expected),
            "actual_mates": sorted(names),
            "ratio_match": ratio_ok,
            "ratio": request["transmission_ratio"],
        }

    @staticmethod
    def _interference_check(model: Any) -> dict[str, Any]:
        manager = ActiveModelThroughHoleExecutor._com_member(model, "InterferenceDetectionManager")
        if manager is None:
            return {"success": False, "message": "SolidWorks returned no InterferenceDetectionManager.", "count": -1}
        try:
            for name, value in (
                ("TreatSubAssembliesAsComponents", False),
                ("TreatCoincidenceAsInterference", False),
                ("IncludeMultibodyPartInterferences", False),
                ("UseTransform", True),
            ):
                try:
                    setattr(manager, name, value)
                except Exception:
                    pass
            count = int(ActiveModelThroughHoleExecutor._com_member(manager, "GetInterferenceCount", default=-1))
            return {
                "success": count == 0,
                "count": count,
                "treat_coincidence_as_interference": False,
                "use_transform": True,
            }
        finally:
            try:
                manager.Done()
            except Exception:
                pass

    def _ensure_imports(self) -> None:
        if not self.script_dir.exists():
            raise RuntimeError(f"SolidWorks automation scripts not found: {self.script_dir}")
        script_text = str(self.script_dir)
        if script_text not in sys.path:
            sys.path.insert(0, script_text)
        os.environ["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["CAD_AGENT_NO_INTERACTIVE"] = "1"

    @staticmethod
    def _number(values: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = values.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _safe_stem(value: Any) -> str:
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "gear")).strip("_")
        return stem[:80] or "gear"

    @staticmethod
    def _existing_files(*paths: Path) -> list[str]:
        return [str(path) for path in paths if path.is_file() and path.stat().st_size > 0]

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        payload["files"] = list(dict.fromkeys(
            [str(item) for item in payload.get("files", [])] + [str(report_path)]
        ))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success=success,
            message=message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
