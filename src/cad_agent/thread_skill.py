from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .advanced_feature_skill import AdvancedFeatureSkill
from .models import SkillResult


class ThreadSkill:
    """Create native Hole Wizard tapped holes on the active production Part."""

    key = "solidworks_threaded_holes"
    name = "SolidWorks Thread Skill"
    SW_SEL_FACES = 2
    DEFAULT_STANDARD_INDEX = 13
    DEFAULT_FASTENER_TYPE_INDEX = 358
    # HoleWizard5 expects swWzdGeneralHoleTypes_e.swWzdTap here.  Values such
    # as 48 are detailed swWzdHoleTypes_e results exposed by feature data and
    # are not valid as the creation API's GenericHoleType argument.
    DEFAULT_GENERIC_HOLE_TYPE = 4
    DEFAULT_END_CONDITION = 2
    THREAD_TABLE = {
        "M3": {"designation": "M3", "tap_drill_diameter_mm": 2.5, "pitch_mm": 0.5},
        "M4": {"designation": "M4", "tap_drill_diameter_mm": 3.3, "pitch_mm": 0.7},
        "M5": {"designation": "M5", "tap_drill_diameter_mm": 4.2, "pitch_mm": 0.8},
        "M6": {"designation": "M6", "tap_drill_diameter_mm": 5.0, "pitch_mm": 1.0},
        "M8": {"designation": "M8", "tap_drill_diameter_mm": 6.8, "pitch_mm": 1.25},
        "M10": {"designation": "M10", "tap_drill_diameter_mm": 8.5, "pitch_mm": 1.5},
        "M12": {"designation": "M12", "tap_drill_diameter_mm": 10.2, "pitch_mm": 1.75},
    }

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation" / "subskills" / "solidworks-threaded-holes"
        )
        self.script_dir = self.skill_dir.parents[1] / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == "threaded_hole"]
        if not features:
            return SkillResult(False, "No threaded_hole feature found in plan.")

        run_dir = self.output_root / f"thread_skill_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "thread_skill_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member

            _sw, model = connect_solidworks(visible=True)
            if model is None:
                return self._result(False, "ActiveDoc does not exist.", report_path, {})
            title = str(get_com_member(model, "GetTitle") or "")
            if int(get_com_member(model, "GetType")) != 1:
                return self._result(False, f"ActiveDoc is not a Part: {title}", report_path, {"active_doc": title})

            operations: list[dict[str, Any]] = []
            for feature in features:
                request = self.normalize_request(feature.get("params", {}))
                if not request.get("success"):
                    operations.append(request)
                    return self._result(
                        False,
                        str(request.get("message") or "Invalid threaded-hole request."),
                        report_path,
                        {"active_doc": title, "operations": operations},
                    )
                body_info = ActiveModelThroughHoleExecutor._body_info(model)
                if not body_info.get("success"):
                    return self._result(False, str(body_info.get("message")), report_path, {"active_doc": title})
                operation = self._execute(model, feature, request, body_info)
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(
                        False,
                        str(operation.get("message") or "Hole Wizard feature failed."),
                        report_path,
                        {"active_doc": title, "operations": operations},
                    )

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            data = {
                "active_doc": title,
                "mode": "active_model",
                "feature_type": "threaded_hole",
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "saved_by_this_skill": False,
                "side_effects": {
                    "modifies_active_doc": True,
                    "creates_new_doc": False,
                    "exports_files": False,
                    "uses_template": False,
                },
            }
            return self._result(True, "Native active-model Hole Wizard feature created and verified.", report_path, data)
        except Exception as exc:
            return self._result(
                False,
                f"threaded_hole failed: {exc}",
                report_path,
                {"error": repr(exc), "traceback": traceback.format_exc()},
            )

    @classmethod
    def normalize_request(cls, params: dict[str, Any]) -> dict[str, Any]:
        raw_thread = str(params.get("thread") or params.get("thread_size") or params.get("designation") or "").upper()
        compact = raw_thread.replace(" ", "").replace("*", "X")
        thread = compact.split("X", 1)[0]
        spec = cls.THREAD_TABLE.get(thread)
        if spec is None:
            return {
                "success": False,
                "message": f"thread must be one of: {', '.join(cls.THREAD_TABLE)}.",
            }
        axis = str(params.get("axis") or "").strip().lower().lstrip("+")
        if axis not in {"x", "y", "z"}:
            return {"success": False, "message": "threaded_hole requires axis=x/y/z."}
        face_offset = cls._number(params, "face_offset_mm", "support_face_offset_mm", "target_face_offset_mm")
        if face_offset is None:
            return {"success": False, "message": "threaded_hole requires a signed face_offset_mm."}
        center_key = {"x": "center_yz_mm", "y": "center_xz_mm", "z": "center_xy_mm"}[axis]
        center = params.get(center_key) or params.get("center_uv_mm") or params.get("center_mm")
        if not isinstance(center, (tuple, list)) or len(center) < 2:
            return {"success": False, "message": f"{center_key} must contain two millimetre coordinates."}
        thread_depth = cls._number(params, "thread_depth_mm", "thread_depth", "depth_mm", "depth")
        thread_depth = float(thread_depth if thread_depth is not None else 2.0 * float(thread[1:]))
        pilot_depth = cls._number(params, "pilot_depth_mm", "tap_drill_depth_mm", "hole_depth_mm")
        pilot_depth = float(pilot_depth if pilot_depth is not None else thread_depth)
        tap_drill = cls._number(params, "tap_drill_diameter_mm", "tap_drill_mm", "tap_drill")
        tap_drill = float(tap_drill if tap_drill is not None else spec["tap_drill_diameter_mm"])
        if min(thread_depth, pilot_depth, tap_drill) <= 0.0:
            return {"success": False, "message": "thread, pilot, and tap-drill dimensions must be positive."}
        generic_hole_type = int(params.get("generic_hole_type", cls.DEFAULT_GENERIC_HOLE_TYPE))
        if generic_hole_type != cls.DEFAULT_GENERIC_HOLE_TYPE:
            return {
                "success": False,
                "message": "threaded_hole currently requires generic_hole_type=4 (swWzdTap).",
            }
        return {
            "success": True,
            "thread": spec["designation"],
            "pitch_mm": float(spec["pitch_mm"]),
            "axis": axis,
            "face_offset_mm": float(face_offset),
            "center_uv_mm": [float(center[0]), float(center[1])],
            "thread_depth_mm": thread_depth,
            "pilot_depth_mm": pilot_depth,
            "tap_drill_diameter_mm": tap_drill,
            "generic_hole_type": generic_hole_type,
            "source_hole_type": int(params["source_hole_type"]) if params.get("source_hole_type") is not None else None,
            "standard_index": int(params.get("standard_index", cls.DEFAULT_STANDARD_INDEX)),
            "fastener_type_index": int(params.get("fastener_type_index", cls.DEFAULT_FASTENER_TYPE_INDEX)),
            "end_condition": int(params.get("end_condition", cls.DEFAULT_END_CONDITION)),
            "thread_class": str(params.get("thread_class") or "1B"),
            "cosmetic_thread_type": int(params.get("cosmetic_thread_type", 1)),
            "thread_end_condition": int(params.get("thread_end_condition", 2)),
            "reverse_direction": bool(params.get("reverse_direction", False)),
        }

    def _execute(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        axis = request["axis"]
        face_offset = float(request["face_offset_mm"]) / 1000.0
        center = [float(value) / 1000.0 for value in request["center_uv_mm"]]
        point = AdvancedFeatureSkill._model_point(axis, face_offset, center)
        support = self._resolve_support_face(
            body_info["bodies"],
            axis,
            face_offset,
            point,
            search_radius=float(request["tap_drill_diameter_mm"]) / 2000.0,
        )
        face = support.get("face")
        placement_point = support.get("placement_point")
        if face is None or placement_point is None:
            return self._failure("Could not resolve the requested threaded-hole support face.", request)
        face_normal = AdvancedFeatureSkill._face_normal(face)
        if face_normal is None:
            return self._failure("The threaded-hole support face is not planar.", request)
        outward = AdvancedFeatureSkill._axis_vector(axis, 1.0 if face_offset >= 0.0 else -1.0)
        if AdvancedFeatureSkill._dot(face_normal, outward) < 0.999:
            return self._failure("The threaded-hole support face normal does not point outward.", request)

        volume_before = ActiveModelThroughHoleExecutor._solid_volume(body_info["bodies"])
        body_count_before = len(body_info["bodies"])
        if volume_before is None:
            return self._failure("Could not measure solid volume before Hole Wizard creation.", request)
        model.ClearSelection2(True)
        epsilon = 1e-4
        ray_start = tuple(placement_point[index] + face_normal[index] * epsilon for index in range(3))
        ray_direction = tuple(-value for value in face_normal)
        selected = bool(
            model.Extension.SelectByRay(
                ray_start[0],
                ray_start[1],
                ray_start[2],
                ray_direction[0],
                ray_direction[1],
                ray_direction[2],
                1e-5,
                self.SW_SEL_FACES,
                False,
                0,
                0,
            )
        )
        if not selected:
            return self._failure("SelectByRay could not select the Hole Wizard placement point.", request)

        minus_one = -1.0
        created = model.FeatureManager.HoleWizard5(
            int(request["generic_hole_type"]),
            int(request["standard_index"]),
            int(request["fastener_type_index"]),
            str(request["thread"]),
            int(request["end_condition"]),
            float(request["tap_drill_diameter_mm"]) / 1000.0,
            float(request["pilot_depth_mm"]) / 1000.0,
            minus_one,
            float(request["thread_depth_mm"]) / 1000.0,
            minus_one,
            minus_one,
            minus_one,
            minus_one,
            minus_one,
            float(request["cosmetic_thread_type"]),
            float(request["thread_end_condition"]),
            minus_one,
            minus_one,
            minus_one,
            minus_one,
            str(request["thread_class"]),
            bool(request["reverse_direction"]),
            True,
            True,
            False,
            False,
            False,
        )
        if created is None:
            return self._failure("SolidWorks HoleWizard5 returned no feature.", request)
        location = {
            "success": True,
            "repositioned": False,
            "selection_point_model_mm": [round(value * 1000.0, 6) for value in placement_point],
        }
        if math.dist(point, placement_point) > 1e-9:
            location = self._move_hole_location(model, created, point, placement_point)
            if not location.get("success"):
                return self._failure(
                    str(location.get("message") or "Could not set the Hole Wizard location point."),
                    request,
                    location=location,
                )
        name = str(feature.get("name") or f"{request['thread']}ThreadedHole")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ClearSelection2(True)
        model.ForceRebuild3(False)

        body_after = ActiveModelThroughHoleExecutor._body_info(model)
        if not body_after.get("success"):
            return self._failure("Could not inspect the solid after Hole Wizard creation.", request)
        bodies_after = list(body_after.get("bodies") or [])
        volume_after = ActiveModelThroughHoleExecutor._solid_volume(bodies_after)
        if volume_after is None:
            return self._failure("Could not measure solid volume after Hole Wizard creation.", request)
        volume_delta = float(volume_after) - float(volume_before)
        tolerance = max(abs(float(volume_before)), abs(float(volume_after)), 1e-12) * 1e-9
        if volume_delta >= -tolerance:
            return self._failure(
                "The Hole Wizard feature did not remove measurable solid volume.",
                request,
                volume_before_m3=volume_before,
                volume_after_m3=volume_after,
                volume_delta_m3=volume_delta,
            )
        if len(bodies_after) != body_count_before:
            return self._failure(
                "The Hole Wizard feature changed the solid-body count.",
                request,
                body_count_before=body_count_before,
                body_count_after=len(bodies_after),
            )
        type_name = str(ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or "")
        if type_name.casefold() not in {"holewzd", "holewizard"}:
            return self._failure(
                f"Unexpected threaded-hole feature type: {type_name}",
                request,
                feature_type_name=type_name,
            )
        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_size = str(ActiveModelThroughHoleExecutor._com_member(definition, "FastenerSize", default="") or "")
        if actual_size and actual_size.casefold() != str(request["thread"]).casefold():
            return self._failure(
                "Hole Wizard size does not match the requested thread.",
                request,
                actual_fastener_size=actual_size,
            )
        actual_hole_type = ActiveModelThroughHoleExecutor._com_member(definition, "Type", default=None)
        expected_hole_type = request.get("source_hole_type")
        if expected_hole_type is not None and int(actual_hole_type) != int(expected_hole_type):
            return self._failure(
                "Hole Wizard detailed type does not match the audited source type.",
                request,
                actual_hole_type=actual_hole_type,
                expected_hole_type=expected_hole_type,
            )
        return {
            "success": True,
            "name": name,
            "type": "threaded_hole",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "feature_type_name": type_name,
            "fastener_size": actual_size or request["thread"],
            "hole_type": actual_hole_type,
            "center_model_mm": [round(value * 1000.0, 6) for value in point],
            "face_normal": face_normal,
            "location": location,
            "body_count_before": body_count_before,
            "body_count_after": len(bodies_after),
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_delta_m3": volume_delta,
        }

    @staticmethod
    def _resolve_support_face(
        bodies: list[Any],
        axis: str,
        face_offset: float,
        target_point: tuple[float, float, float],
        *,
        search_radius: float,
    ) -> dict[str, Any]:
        exact = AdvancedFeatureSkill._find_planar_face(
            bodies,
            axis,
            face_offset,
            target_point,
        )
        if exact is not None:
            return {
                "face": exact,
                "placement_point": target_point,
                "reposition_required": False,
            }

        radius = max(float(search_radius), 0.0)
        if radius <= 0.0:
            return {}
        step = min(0.00025, radius / 4.0)
        step = max(step, 0.00005)
        steps = max(1, int(math.ceil(radius / step)))
        offsets: list[tuple[float, float]] = []
        for u_index in range(-steps, steps + 1):
            for v_index in range(-steps, steps + 1):
                if u_index == 0 and v_index == 0:
                    continue
                du = u_index * step
                dv = v_index * step
                if math.hypot(du, dv) <= radius + 1e-12:
                    offsets.append((du, dv))
        offsets.sort(key=lambda item: (math.hypot(*item), item[0], item[1]))

        axis_index = {"x": 0, "y": 1, "z": 2}[axis]
        other_indices = {
            "x": (1, 2),
            "y": (0, 2),
            "z": (0, 1),
        }[axis]
        for du, dv in offsets:
            candidate = list(target_point)
            candidate[axis_index] = face_offset
            candidate[other_indices[0]] += du
            candidate[other_indices[1]] += dv
            candidate_point = tuple(candidate)
            face = AdvancedFeatureSkill._find_planar_face(
                bodies,
                axis,
                face_offset,
                candidate_point,
            )
            if face is not None:
                return {
                    "face": face,
                    "placement_point": candidate_point,
                    "reposition_required": True,
                    "offset_m": math.dist(target_point, candidate_point),
                }
        return {}

    @staticmethod
    def _move_hole_location(
        model: Any,
        created: Any,
        target_point: tuple[float, float, float],
        placement_point: tuple[float, float, float],
    ) -> dict[str, Any]:
        member = ActiveModelThroughHoleExecutor._com_member
        sketch_feature = member(created, "GetFirstSubFeature")
        while sketch_feature is not None:
            feature_type = str(member(sketch_feature, "GetTypeName2", default="") or "")
            if feature_type in {"ProfileFeature", "3DProfileFeature"}:
                break
            sketch_feature = member(sketch_feature, "GetNextSubFeature")
        if sketch_feature is None:
            return {"success": False, "message": "Hole Wizard location sketch was not found."}

        model.ClearSelection2(True)
        selected = bool(member(sketch_feature, "Select2", False, 0, default=False))
        if not selected:
            return {"success": False, "message": "Hole Wizard location sketch could not be selected."}

        sketch_open = False
        try:
            member(model, "EditSketch")
            sketch_open = True
            specific = member(sketch_feature, "GetSpecificFeature2")
            points = member(specific, "GetSketchPoints2") or ()
            if not isinstance(points, (tuple, list)):
                points = (points,)
            if len(points) != 1:
                return {
                    "success": False,
                    "message": "Hole Wizard location sketch must contain exactly one user point.",
                    "point_count": len(points),
                }
            relation_manager = member(specific, "RelationManager")
            if relation_manager is None:
                return {
                    "success": False,
                    "message": "Hole Wizard location sketch relation manager was not available.",
                }
            relation_count_before = member(
                relation_manager,
                "GetRelationsCount",
                0,
                default=None,
            )
            if relation_count_before is None:
                return {
                    "success": False,
                    "message": "Hole Wizard location sketch relations could not be inspected.",
                }
            relation_count_before = int(relation_count_before)
            relations_deleted = False
            if relation_count_before:
                relations_deleted = bool(
                    member(relation_manager, "DeleteAllRelations", default=False)
                )
                relation_count_after = int(
                    member(relation_manager, "GetRelationsCount", 0, default=-1)
                )
                if not relations_deleted or relation_count_after != 0:
                    return {
                        "success": False,
                        "message": "Hole Wizard location sketch relations could not be removed.",
                        "relation_count_before": relation_count_before,
                        "relation_count_after": relation_count_after,
                    }
            else:
                relation_count_after = 0
            target_sketch = AdvancedFeatureSkill._to_sketch_point(
                model,
                target_point,
                sketch=specific,
            )
            moved = bool(member(points[0], "SetCoords", *target_sketch, default=False))
            if not moved:
                return {"success": False, "message": "Hole Wizard location point rejected the audited coordinates."}
            actual = [
                float(member(points[0], coordinate, default=0.0) or 0.0)
                for coordinate in ("X", "Y", "Z")
            ]
            if math.dist(tuple(actual), target_sketch) > 1e-7:
                return {
                    "success": False,
                    "message": "Hole Wizard location point does not match the audited coordinates.",
                    "target_sketch_m": list(target_sketch),
                    "actual_sketch_m": actual,
                }
            return {
                "success": True,
                "repositioned": True,
                "selection_point_model_mm": [round(value * 1000.0, 6) for value in placement_point],
                "target_point_model_mm": [round(value * 1000.0, 6) for value in target_point],
                "target_point_sketch_mm": [round(value * 1000.0, 6) for value in target_sketch],
                "actual_point_sketch_mm": [round(value * 1000.0, 6) for value in actual],
                "relation_count_before": relation_count_before,
                "relation_count_after": relation_count_after,
                "relations_deleted": relations_deleted,
            }
        finally:
            if sketch_open:
                member(model.SketchManager, "InsertSketch", True)
            model.ClearSelection2(True)
            member(model, "ForceRebuild3", False)

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
    def _number(params: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = params.get(key)
            if value is not None and value != "":
                return float(value)
        return None

    @staticmethod
    def _failure(message: str, request: dict[str, Any], **data: Any) -> dict[str, Any]:
        return {"success": False, "message": message, "request": request, **data}

    @staticmethod
    def _result(
        success: bool,
        message: str,
        report_path: Path,
        data: dict[str, Any],
    ) -> SkillResult:
        payload = {"success": success, "message": message, **data, "files": [str(report_path)]}
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success,
            message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _non_interactive_env() -> dict[str, str]:
        env = os.environ.copy()
        env["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["CAD_AGENT_NO_INTERACTIVE"] = "1"
        return env
