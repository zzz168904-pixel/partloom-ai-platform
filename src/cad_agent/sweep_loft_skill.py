from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pythoncom
from win32com.client import VARIANT

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .feature_management_skill import FeatureManagementSkill
from .models import SkillResult
from .profile_geometry import normalize_profile_extrude_request
from .revolve_skill import RevolveSkill


class SweepLoftSkill:
    """Create deterministic sweeps and ordered-section boss/base/cut lofts."""

    FEATURE_TYPES = {"sweep", "loft"}
    PLANES = {
        "front": "Front Plane",
        "front plane": "Front Plane",
        "top": "Top Plane",
        "top plane": "Top Plane",
        "right": "Right Plane",
        "right plane": "Right Plane",
    }

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any], feature_type: str) -> SkillResult:
        if feature_type not in self.FEATURE_TYPES:
            return SkillResult(False, f"Unsupported sweep/loft feature type: {feature_type}")
        features = [item for item in plan.get("features", []) if item.get("type") == feature_type]
        if not features:
            return SkillResult(False, f"No {feature_type} feature found in the production plan.")

        run_dir = self.output_root / f"{feature_type}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / f"{feature_type}_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document

            sw, active = connect_solidworks(visible=True)
            model = active
            operations: list[dict[str, Any]] = []
            files: list[str] = []

            for index, feature in enumerate(features):
                request = self.normalize_request(feature_type, feature.get("params", {}), plan.get("task_type"))
                if not request.get("success"):
                    return self._result(False, str(request.get("message")), report_path, {
                        "feature_type": feature_type,
                        "operations": operations,
                        "invalid_request": request,
                    })
                if request["mode"] == "new_model":
                    if index > 0:
                        return self._result(False, "Only the first feature may create the task Part.", report_path, {
                            "feature_type": feature_type,
                            "operations": operations,
                        })
                    model = new_document(sw, "part")

                validation = RevolveSkill._validate_active_part(
                    model,
                    require_body=request["mode"] == "active_model",
                )
                if not validation.get("success"):
                    return self._result(False, str(validation.get("message")), report_path, validation)

                before = RevolveSkill._with_body_evidence(
                    ActiveModelThroughHoleExecutor._body_info(model)
                    if request["mode"] == "active_model"
                    else {"success": False}
                )
                before_tree = ActiveModelFeatureSkill._feature_tree(model)
                if feature_type == "sweep":
                    operation = self._create_sweep(model, feature, request)
                else:
                    operation = self._create_loft(model, feature, request)
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(False, str(operation.get("message")), report_path, {
                        "active_doc": validation.get("active_doc"),
                        "feature_type": feature_type,
                        "operations": operations,
                    })

                model.ForceRebuild3(False)
                after = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
                geometry = self._verify_geometry(feature_type, request, before, before_tree, after, model)
                operation["geometry_validation"] = geometry
                operation["bbox_after_m"] = after.get("bbox", {})
                operation["body_count_after"] = after.get("body_count", 0)
                if not geometry.get("success"):
                    return self._result(False, str(geometry.get("message")), report_path, {
                        "active_doc": validation.get("active_doc"),
                        "feature_type": feature_type,
                        "operations": operations,
                    })

            if model is None:
                return self._result(False, "SolidWorks did not return a Part document.", report_path, {})

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            material = str(plan.get("parameters", {}).get("material") or "").strip()
            material_metadata = RevolveSkill._set_material_metadata(model, material)
            reopen_validation: dict[str, Any] = {
                "success": True,
                "skipped": True,
                "reason": "active-model operation",
            }
            if operations and operations[0].get("mode") == "new_model":
                requested_path = str(plan.get("execution_model_path") or "").strip()
                default_name = "swept_part.SLDPRT" if feature_type == "sweep" else "lofted_part.SLDPRT"
                part_path = Path(requested_path) if requested_path else run_dir / default_name
                part_path.parent.mkdir(parents=True, exist_ok=True)
                if not save_document(model, str(part_path)):
                    return self._result(False, f"SolidWorks failed to save {feature_type} Part: {part_path}", report_path, {
                        "operations": operations,
                    })
                if not part_path.is_file() or part_path.stat().st_size <= 0:
                    return self._result(False, f"Saved {feature_type} Part is missing or empty: {part_path}", report_path, {
                        "operations": operations,
                    })
                files.append(str(part_path))
                reopen_validation = RevolveSkill._verify_reopen(sw, model, part_path)
                reopened_model = reopen_validation.pop("model", None)
                if not reopen_validation.get("success"):
                    return self._result(False, f"Saved {feature_type} Part could not be reopened: {part_path}", report_path, {
                        "operations": operations,
                        "reopen_validation": reopen_validation,
                        "files": files,
                    })
                model = reopened_model

            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": operations[0].get("mode") if operations else "",
                "feature_type": feature_type,
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "material": material,
                "material_metadata": material_metadata,
                "reopen_validation": reopen_validation,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "saved_by_this_skill": bool(files),
                "side_effects": {
                    "modifies_active_doc": any(item.get("mode") == "active_model" for item in operations),
                    "creates_new_doc": any(item.get("mode") == "new_model" for item in operations),
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": files,
            }
            return self._result(True, f"SolidWorks {feature_type} created and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"{feature_type} failed: {exc}", report_path, {
                "feature_type": feature_type,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(
        cls,
        feature_type: str,
        params: dict[str, Any],
        task_type: str | None = None,
    ) -> dict[str, Any]:
        if feature_type == "sweep":
            return cls._normalize_sweep(params, task_type)
        if feature_type == "loft":
            return cls._normalize_loft(params, task_type)
        return {"success": False, "message": f"Unsupported feature type: {feature_type}"}

    @classmethod
    def _normalize_sweep(cls, params: dict[str, Any], task_type: str | None) -> dict[str, Any]:
        operation = str(params.get("operation") or "base").strip().lower()
        operation = {"add": "boss", "swept_boss": "boss", "swept_base": "base", "swept_cut": "cut"}.get(operation, operation)
        if operation not in {"base", "boss", "cut"}:
            return {"success": False, "message": "sweep operation must be base, boss, or cut."}
        mode = str(params.get("mode") or ("new_model" if operation == "base" else "active_model")).strip().lower()
        mode_check = cls._validate_mode(mode, operation, task_type, "sweep")
        if mode_check:
            return mode_check

        profile = params.get("profile") if isinstance(params.get("profile"), dict) else {}
        profile_type = str(
            params.get("profile_type") or profile.get("shape") or profile.get("type") or "circle"
        ).strip().lower()
        if profile_type not in {"circle", "circular", "round"}:
            return {
                "success": False,
                "message": "The production sweep currently requires an explicit circular profile.",
                "unsupported_profile_type": profile_type,
            }
        diameter = cls._number(
            params,
            "diameter_mm",
            "profile_diameter_mm",
            "circular_profile_diameter_mm",
        )
        if diameter is None:
            diameter = cls._number(profile, "diameter_mm", "diameter")
        if diameter is None or diameter <= 0:
            return {"success": False, "message": "sweep circular profile requires diameter_mm > 0."}

        path_value = params.get("path_points_mm") or params.get("path_points") or params.get("path")
        if isinstance(path_value, dict):
            path_type = str(path_value.get("type") or "polyline").strip().lower()
            raw_points = path_value.get("points_mm") or path_value.get("points")
            raw_segments = path_value.get("segments")
        else:
            path_type = str(params.get("path_type") or "polyline").strip().lower()
            raw_points = path_value
            raw_segments = params.get("path_segments")
        if raw_segments is not None:
            path_type = "segments"
        if path_type not in {"polyline", "line", "segments"}:
            return {
                "success": False,
                "message": "The production sweep supports straight polylines or explicit tangent line/arc segments.",
                "unsupported_path_type": path_type,
            }
        if path_type == "segments":
            normalized_path = cls._normalize_path_segments(raw_segments, float(diameter))
            if not normalized_path.get("success"):
                return normalized_path
            path_points = normalized_path["path_points_mm"]
            path_segments = normalized_path["path_segments"]
            path_length = normalized_path["path_length_mm"]
        else:
            points = cls._normalize_points(raw_points)
            if not points.get("success"):
                return points
            path_points = points["points"]
            if len(path_points) < 2:
                return {"success": False, "message": "sweep path requires at least two points."}
            if len(path_points) > 2 and not cls._is_straight_polyline(path_points):
                return {
                    "success": False,
                    "message": "A sharp polyline corner is unsafe for a circular sweep; provide tangent line/arc path_segments.",
                }
            path_segments = [
                {"type": "line", "start_mm": first, "end_mm": second}
                for first, second in zip(path_points, path_points[1:])
            ]
            path_length = sum(math.dist(first, second) for first, second in zip(path_points, path_points[1:]))
            if path_length <= 0:
                return {"success": False, "message": "sweep path length must be greater than zero."}

        plane = cls._plane(params.get("path_plane") or params.get("sketch_plane") or params.get("plane") or "front")
        if plane is None:
            return {"success": False, "message": "sweep path_plane must be front, top, or right."}
        return {
            "success": True,
            "mode": mode,
            "operation": operation,
            "profile_type": "circle",
            "diameter_mm": float(diameter),
            "path_type": "segments" if path_type == "segments" else "polyline",
            "path_plane": plane,
            "path_points_mm": path_points,
            "path_segments": path_segments,
            "path_length_mm": path_length,
            "merge": operation == "boss",
            "direction": int(params.get("direction", 0) or 0),
        }

    @classmethod
    def _normalize_loft(cls, params: dict[str, Any], task_type: str | None) -> dict[str, Any]:
        operation = str(params.get("operation") or "base").strip().lower()
        operation = {
            "add": "boss",
            "lofted_boss": "boss",
            "lofted_base": "base",
            "lofted_cut": "cut",
        }.get(operation, operation)
        if operation not in {"base", "boss", "cut"}:
            return {"success": False, "message": "loft operation must be base, boss, or cut."}
        mode = str(params.get("mode") or ("new_model" if operation == "base" else "active_model")).strip().lower()
        mode_check = cls._validate_mode(mode, operation, task_type, "loft")
        if mode_check:
            return mode_check

        raw_sections = params.get("sections") or params.get("profiles")
        if not isinstance(raw_sections, list) or len(raw_sections) < 2:
            return {"success": False, "message": "loft requires at least two explicit closed sections."}
        sections: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_sections):
            normalized = cls._normalize_section(raw, index)
            if not normalized.get("success"):
                return normalized
            sections.append(normalized)
        point_indices = [index for index, item in enumerate(sections) if item["shape"] == "point"]
        if len(point_indices) > 1 or any(index not in {0, len(sections) - 1} for index in point_indices):
            return {"success": False, "message": "loft permits at most one point section, at the first or last position."}
        if len(point_indices) == len(sections):
            return {"success": False, "message": "loft requires at least one closed profile section."}
        standard_sections = [item for item in sections if item["plane_kind"] == "standard_offset"]
        if len(standard_sections) == len(sections):
            offsets = [float(item["offset_mm"]) for item in sections]
            if any(offset < 0 for offset in offsets):
                return {"success": False, "message": "loft section offsets must be non-negative in the current production executor."}
            if any(second <= first for first, second in zip(offsets, offsets[1:])):
                return {"success": False, "message": "loft section offsets must be unique and strictly increasing."}
        plane = cls._plane(params.get("base_plane") or params.get("sketch_plane") or "front")
        if plane is None:
            return {"success": False, "message": "loft base_plane must be front, top, or right."}
        return {
            "success": True,
            "mode": mode,
            "operation": operation,
            "base_plane": plane,
            "sections": sections,
            "keep_tangency": bool(params.get("keep_tangency", True)),
            "force_non_rational": bool(params.get("force_non_rational", False)),
            "merge": operation == "boss",
        }

    @classmethod
    def _normalize_section(cls, raw: Any, index: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": f"loft section {index + 1} must be an object."}
        shape = str(raw.get("shape") or raw.get("type") or "").strip().lower()
        shape = {
            "round": "circle",
            "circular": "circle",
            "square": "rectangle",
            "rect": "rectangle",
            "segments": "profile",
            "polygon": "profile",
            "vertex": "point",
        }.get(shape, shape)
        if shape not in {"circle", "rectangle", "profile", "point"}:
            return {
                "success": False,
                "message": f"loft section {index + 1} shape must be circle, rectangle, profile, or point.",
            }
        plane = cls._normalize_section_plane(raw, index)
        if not plane.get("success"):
            return plane
        center_raw = raw.get("point_mm") or raw.get("center_mm") or raw.get("center") or [0.0, 0.0]
        center = cls._point(center_raw)
        if center is None:
            return {"success": False, "message": f"loft section {index + 1} has an invalid center_mm."}
        result: dict[str, Any] = {
            "success": True,
            "shape": shape,
            "center_mm": center,
            **{key: value for key, value in plane.items() if key != "success"},
        }
        if shape == "circle":
            diameter = cls._number(raw, "diameter_mm", "diameter")
            radius = cls._number(raw, "radius_mm", "radius")
            diameter = float(diameter) if diameter is not None else (2.0 * float(radius) if radius is not None else 0.0)
            if diameter <= 0:
                return {"success": False, "message": f"loft circle section {index + 1} requires diameter_mm > 0."}
            result["diameter_mm"] = diameter
        elif shape == "rectangle":
            length = cls._number(raw, "length_mm", "length", "width_x_mm")
            width = cls._number(raw, "width_mm", "width", "height_y_mm")
            if length is None or width is None or length <= 0 or width <= 0:
                return {"success": False, "message": f"loft rectangle section {index + 1} requires positive length_mm and width_mm."}
            result["length_mm"] = float(length)
            result["width_mm"] = float(width)
        elif shape == "profile":
            raw_profiles = raw.get("profiles")
            if raw_profiles is None and raw.get("segments") is not None:
                raw_profiles = [{"role": "outer", "segments": raw["segments"]}]
            if raw_profiles is None and (raw.get("points_mm") is not None or raw.get("points") is not None):
                raw_profiles = [{
                    "role": "outer",
                    "points_mm": raw.get("points_mm") or raw.get("points"),
                    "closed": bool(raw.get("closed", False)),
                }]
            profile_request = normalize_profile_extrude_request(
                {
                    "body_operation": "base",
                    "mode": "new_model",
                    "sketch_plane": "front",
                    "depth_mm": 1.0,
                    "profiles": raw_profiles,
                },
                task_type="model_3d",
            )
            if not profile_request.get("success"):
                first_error = (profile_request.get("errors") or [{}])[0]
                return {
                    "success": False,
                    "message": f"loft profile section {index + 1} is invalid: {first_error.get('message') or profile_request.get('message')}",
                    "errors": profile_request.get("errors", []),
                }
            profiles = profile_request.get("profiles") or []
            if len(profiles) != 1 or profiles[0].get("role") != "outer":
                return {
                    "success": False,
                    "message": f"loft profile section {index + 1} requires exactly one outer closed loop.",
                }
            result["segments"] = profiles[0]["segments"]
        return result

    @classmethod
    def _normalize_section_plane(cls, raw: dict[str, Any], index: int) -> dict[str, Any]:
        plane = raw.get("plane")
        if not isinstance(plane, dict):
            offset = cls._number(raw, "offset_mm", "plane_offset_mm", "distance_mm")
            if offset is None:
                return {"success": False, "message": f"loft section {index + 1} requires offset_mm or an explicit fixed plane."}
            return {"success": True, "plane_kind": "standard_offset", "offset_mm": float(offset)}

        plane_type = str(plane.get("type") or "fixed").strip().lower()
        if plane_type not in {"fixed", "fixed_plane"}:
            return {"success": False, "message": f"loft section {index + 1} plane type must be fixed."}
        origin = cls._point3(plane.get("origin_mm") or plane.get("origin"))
        x_axis = cls._point3(plane.get("x_axis") or plane.get("u_axis"))
        y_axis = cls._point3(plane.get("y_axis") or plane.get("v_axis"))
        if origin is None or x_axis is None or y_axis is None:
            return {
                "success": False,
                "message": f"loft section {index + 1} fixed plane requires origin_mm, x_axis, and y_axis.",
            }
        x_length = math.sqrt(sum(value * value for value in x_axis))
        y_length = math.sqrt(sum(value * value for value in y_axis))
        if x_length <= 1e-9 or y_length <= 1e-9:
            return {"success": False, "message": f"loft section {index + 1} fixed-plane axes must be non-zero."}
        x_unit = [value / x_length for value in x_axis]
        y_unit = [value / y_length for value in y_axis]
        dot = sum(first * second for first, second in zip(x_unit, y_unit))
        if abs(dot) > 1e-6:
            return {"success": False, "message": f"loft section {index + 1} fixed-plane axes must be orthogonal."}
        return {
            "success": True,
            "plane_kind": "fixed",
            "offset_mm": 0.0,
            "plane_origin_mm": origin,
            "plane_x_axis": x_unit,
            "plane_y_axis": y_unit,
        }

    @staticmethod
    def _validate_mode(mode: str, operation: str, task_type: str | None, feature_type: str) -> dict[str, Any] | None:
        if mode not in {"new_model", "active_model"}:
            return {"success": False, "message": f"{feature_type} mode must be new_model or active_model."}
        if operation == "base" and mode != "new_model":
            return {"success": False, "message": f"A base {feature_type} must use new_model mode."}
        if operation != "base" and mode != "active_model":
            return {"success": False, "message": f"A {feature_type} {operation} must use active_model mode."}
        if task_type == "modify_3d" and mode == "new_model":
            return {"success": False, "message": f"modify_3d cannot create a new {feature_type} Part."}
        return None

    def _create_sweep(self, model: Any, feature: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        sketch_name = self._create_path_sketch(model, request)
        if not sketch_name:
            return {"success": False, "message": "SolidWorks failed to create the sweep path sketch."}
        if not self._select_feature(model, sketch_name, append=False, mark=4):
            return {"success": False, "message": "Could not select the sweep path sketch with mark 4."}
        if request["operation"] == "cut":
            created = model.FeatureManager.InsertCutSwept5(
                False, False, 0, True, False, 0, 0, False, 0.0, 0.0, 0, 0,
                False, True, 0.0, True, False, False, False, True,
                self._m(request["diameter_mm"]), int(request["direction"]),
            )
        else:
            created = model.FeatureManager.InsertProtrusionSwept4(
                False, False, 0, True, False, 0, 0, False, 0.0, 0.0, 0, 0,
                bool(request["merge"]), False, True, 0.0, True, True,
                self._m(request["diameter_mm"]), int(request["direction"]),
            )
        if created is None:
            return {"success": False, "message": "SolidWorks sweep API returned no feature."}
        name = str(feature.get("name") or "Sweep")
        ActiveModelFeatureSkill._name_feature(created, name)
        return {
            "success": True,
            "name": name,
            "type": "sweep",
            "mode": request["mode"],
            "operation": request["operation"],
            "request": request,
            "path_sketch": sketch_name,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    def _create_loft(self, model: Any, feature: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        sketches: list[str] = []
        planes: list[str] = []
        for index, section in enumerate(request["sections"]):
            plane_feature, plane_name = self._loft_plane(model, request["base_plane"], section, index)
            if plane_feature is None:
                return {"success": False, "message": f"Could not create/select plane for loft section {index + 1}."}
            sketch_name = self._create_section_sketch(model, plane_feature, section)
            if not sketch_name:
                return {"success": False, "message": f"Could not create loft section {index + 1}."}
            planes.append(plane_name)
            sketches.append(sketch_name)

        model.ClearSelection2(True)
        for index, sketch_name in enumerate(sketches):
            if not self._select_feature(model, sketch_name, append=index > 0, mark=1):
                return {"success": False, "message": f"Could not select loft section {index + 1} with mark 1."}
        if request["operation"] == "cut":
            created = model.FeatureManager.InsertCutBlend(
                False,
                bool(request["keep_tangency"]),
                bool(request["force_non_rational"]),
                1.0,
                0,
                0,
                False,
                0.0,
                0.0,
                0,
                True,
                True,
            )
        else:
            created = model.FeatureManager.InsertProtrusionBlend2(
                False,
                bool(request["keep_tangency"]),
                bool(request["force_non_rational"]),
                1.0,
                0,
                0,
                0.0,
                0.0,
                False,
                False,
                False,
                0.0,
                0.0,
                0,
                bool(request["merge"]),
                False,
                True,
                0,
            )
        if created is None:
            api_name = "InsertCutBlend" if request["operation"] == "cut" else "InsertProtrusionBlend2"
            return {"success": False, "message": f"SolidWorks {api_name} returned no feature."}
        name = str(feature.get("name") or "Loft")
        ActiveModelFeatureSkill._name_feature(created, name)
        return {
            "success": True,
            "name": name,
            "type": "loft",
            "mode": request["mode"],
            "operation": request["operation"],
            "request": request,
            "section_sketches": sketches,
            "section_planes": planes,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    def _create_path_sketch(self, model: Any, request: dict[str, Any]) -> str | None:
        if not self._begin_sketch(model, request["path_plane"]):
            return None
        active = model.SketchManager.ActiveSketch
        sketch_name = str(ActiveModelThroughHoleExecutor._com_member(active, "Name", default="") or "")
        success = True
        for path_segment in request["path_segments"]:
            first = path_segment["start_mm"]
            second = path_segment["end_mm"]
            if path_segment["type"] == "line":
                segment = model.SketchManager.CreateLine(
                    self._m(first[0]), self._m(first[1]), 0.0,
                    self._m(second[0]), self._m(second[1]), 0.0,
                )
            else:
                center = path_segment["center_mm"]
                segment = model.SketchManager.CreateArc(
                    self._m(center[0]), self._m(center[1]), 0.0,
                    self._m(first[0]), self._m(first[1]), 0.0,
                    self._m(second[0]), self._m(second[1]), 0.0,
                    1 if path_segment["direction"] == "ccw" else -1,
                )
            if segment is None:
                success = False
                break
        model.SketchManager.InsertSketch(True)
        return sketch_name if success and sketch_name else None

    def _loft_plane(self, model: Any, base_plane: str, section: dict[str, Any], index: int) -> tuple[Any | None, str]:
        model.ClearSelection2(True)
        if section["plane_kind"] == "fixed":
            origin = section["plane_origin_mm"]
            x_axis = section["plane_x_axis"]
            y_axis = section["plane_y_axis"]
            span_mm = 10.0
            points = (
                [self._m(value) for value in origin],
                [self._m(origin[axis] + span_mm * x_axis[axis]) for axis in range(3)],
                [self._m(origin[axis] + span_mm * y_axis[axis]) for axis in range(3)],
            )
            variants = tuple(
                VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(point))
                for point in points
            )
            plane = model.CreatePlaneFixed2(variants[0], variants[1], variants[2], False)
            if plane is None:
                return None, ""
            name = f"LoftSectionPlane{index + 1}_Fixed"
            ActiveModelFeatureSkill._name_feature(plane, name)
            selectable = self._selectable_plane_feature(model, plane)
            return selectable, name

        offset_mm = float(section["offset_mm"])
        if abs(float(offset_mm)) <= 1e-9:
            if not FeatureManagementSkill._select_named_plane(model, base_plane, False):
                return None, base_plane
            aliases = {
                "Front Plane": ("Front Plane", "\u524d\u89c6\u57fa\u51c6\u9762"),
                "Top Plane": ("Top Plane", "\u4e0a\u89c6\u57fa\u51c6\u9762"),
                "Right Plane": ("Right Plane", "\u53f3\u89c6\u57fa\u51c6\u9762"),
            }
            feature = None
            for localized in aliases[base_plane]:
                candidate = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", localized)
                if candidate is not None:
                    try:
                        if candidate.Select2(False, 0):
                            feature = candidate
                            break
                    except Exception:
                        continue
            return feature, base_plane
        if not FeatureManagementSkill._select_named_plane(model, base_plane, False):
            return None, ""
        plane = model.FeatureManager.InsertRefPlane(8, self._m(offset_mm), 0, 0.0, 0, 0.0)
        if plane is None:
            return None, ""
        name = f"LoftSectionPlane{index + 1}_{offset_mm:g}mm"
        ActiveModelFeatureSkill._name_feature(plane, name)
        return plane, name

    @staticmethod
    def _selectable_plane_feature(model: Any, created: Any) -> Any | None:
        if hasattr(created, "Select2"):
            return created
        candidate = ActiveModelThroughHoleExecutor._com_member(created, "GetFeature", default=None)
        if candidate is not None and hasattr(candidate, "Select2"):
            return candidate
        current = ActiveModelThroughHoleExecutor._com_member(model, "IFirstFeature")
        last = None
        while current is not None:
            if str(ActiveModelThroughHoleExecutor._com_member(current, "GetTypeName2", default="") or "") == "RefPlane":
                last = current
            current = ActiveModelThroughHoleExecutor._com_member(current, "IGetNextFeature")
        return last

    def _create_section_sketch(self, model: Any, plane_feature: Any, section: dict[str, Any]) -> str | None:
        model.ClearSelection2(True)
        try:
            if not plane_feature.Select2(False, 0):
                return None
        except Exception:
            return None
        model.SketchManager.InsertSketch(True)
        active = model.SketchManager.ActiveSketch
        sketch_name = str(ActiveModelThroughHoleExecutor._com_member(active, "Name", default="") or "")
        cx, cy = section["center_mm"]
        sketch_manager = model.SketchManager
        previous_add_to_db = bool(getattr(sketch_manager, "AddToDB", False))
        previous_display = bool(getattr(sketch_manager, "DisplayWhenAdded", True))
        created: Any = None
        try:
            # Exact CAD-IR profile coordinates must not be moved by sketch
            # inferencing or automatic relations between the short arc spans.
            sketch_manager.AddToDB = True
            sketch_manager.DisplayWhenAdded = False
            if section["shape"] == "point":
                created = sketch_manager.CreatePoint(*self._section_point_m(section, [cx, cy]))
            elif section["shape"] == "circle":
                created = sketch_manager.CreateCircleByRadius(
                    *self._section_point_m(section, [cx, cy]), self._m(section["diameter_mm"] / 2.0)
                )
            elif section["shape"] == "rectangle":
                created = sketch_manager.CreateCenterRectangle(
                    *self._section_point_m(section, [cx, cy]),
                    *self._section_point_m(
                        section,
                        [cx + section["length_mm"] / 2.0, cy + section["width_mm"] / 2.0],
                    ),
                )
            else:
                created_segments: list[Any] = []
                for segment in section["segments"]:
                    if segment["type"] == "line":
                        start, end = segment["start_mm"], segment["end_mm"]
                        item = sketch_manager.CreateLine(
                            *self._section_point_m(section, start),
                            *self._section_point_m(section, end),
                        )
                    elif segment["type"] == "arc":
                        center, start, end = segment["center_mm"], segment["start_mm"], segment["end_mm"]
                        item = sketch_manager.CreateArc(
                            *self._section_point_m(section, center),
                            *self._section_point_m(section, start),
                            *self._section_point_m(section, end),
                            self._sketch_arc_direction(segment["direction"]),
                        )
                    else:
                        center = segment["center_mm"]
                        item = sketch_manager.CreateCircleByRadius(
                            *self._section_point_m(section, center), self._m(segment["radius_mm"])
                        )
                    if item is None:
                        created_segments = []
                        break
                    created_segments.append(item)
                created = created_segments if created_segments else None
        finally:
            sketch_manager.AddToDB = previous_add_to_db
            sketch_manager.DisplayWhenAdded = previous_display
            sketch_manager.InsertSketch(True)
        return sketch_name if created is not None and sketch_name else None

    @classmethod
    def _section_point_m(cls, section: dict[str, Any], point_mm: list[float]) -> tuple[float, float, float]:
        """Return active-sketch UV coordinates in metres; SolidWorks applies the plane transform."""
        return cls._m(point_mm[0]), cls._m(point_mm[1]), 0.0

    @staticmethod
    def _sketch_arc_direction(value: Any) -> int:
        if isinstance(value, str):
            return 1 if value.strip().lower() in {"ccw", "counterclockwise", "+1", "1"} else -1
        return 1 if int(value) > 0 else -1

    @staticmethod
    def _begin_sketch(model: Any, plane_name: str) -> bool:
        model.ClearSelection2(True)
        if not FeatureManagementSkill._select_named_plane(model, plane_name, False):
            return False
        model.SketchManager.InsertSketch(True)
        return model.SketchManager.ActiveSketch is not None

    @staticmethod
    def _select_feature(model: Any, feature_name: str, append: bool, mark: int) -> bool:
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", feature_name)
        if feature is None:
            return False
        try:
            return bool(feature.Select2(append, mark))
        except Exception:
            return False

    @classmethod
    def _verify_geometry(
        cls,
        feature_type: str,
        request: dict[str, Any],
        before: dict[str, Any],
        before_tree: list[dict[str, str]],
        after: dict[str, Any],
        model: Any,
    ) -> dict[str, Any]:
        if not after.get("success") or int(after.get("body_count", 0)) <= 0:
            return {"success": False, "message": f"{feature_type} produced no solid body."}
        after_tree = ActiveModelFeatureSkill._feature_tree(model)
        if len(after_tree) <= len(before_tree):
            return {"success": False, "message": f"{feature_type} did not add a feature to the model tree."}
        if request["mode"] == "active_model":
            before_faces = sum(int(item.get("face_count", 0)) for item in before.get("body_info", []))
            after_faces = sum(int(item.get("face_count", 0)) for item in after.get("body_info", []))
            if before_faces == after_faces:
                return {"success": False, "message": f"Active-model {feature_type} did not change body topology."}
            return {"success": True, "face_count_before": before_faces, "face_count_after": after_faces}

        bbox = after.get("bbox", {})
        spans = sorted(abs(float(bbox.get(max_key, 0)) - float(bbox.get(min_key, 0))) * 1000.0 for min_key, max_key in (
            ("xmin", "xmax"), ("ymin", "ymax"), ("zmin", "zmax")
        ))
        if not spans or min(spans) <= 0:
            return {"success": False, "message": f"{feature_type} body has an invalid bounding box.", "actual_spans_mm": spans}
        if feature_type == "sweep":
            max_path_span = max(
                max(point[axis] for point in request["path_points_mm"])
                - min(point[axis] for point in request["path_points_mm"])
                for axis in (0, 1)
            )
            if max(spans) + 0.25 < max_path_span or min(spans) + 0.25 < request["diameter_mm"]:
                return {
                    "success": False,
                    "message": "Swept body envelope does not contain the requested path/profile.",
                    "actual_spans_mm": spans,
                    "path_span_mm": max_path_span,
                    "diameter_mm": request["diameter_mm"],
                }
        else:
            offset_span = request["sections"][-1]["offset_mm"] - request["sections"][0]["offset_mm"]
            if max(spans) + 0.25 < offset_span:
                return {
                    "success": False,
                    "message": "Loft body envelope does not span all requested sections.",
                    "actual_spans_mm": spans,
                    "section_span_mm": offset_span,
                }
        return {"success": True, "actual_spans_mm": spans}

    @classmethod
    def _normalize_points(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, list):
            return {"success": False, "message": "sweep path requires path_points_mm."}
        points: list[list[float]] = []
        for index, item in enumerate(raw):
            point = cls._point(item)
            if point is None:
                return {"success": False, "message": f"sweep path point {index + 1} is invalid."}
            if points and math.dist(points[-1], point) <= 1e-9:
                return {"success": False, "message": "sweep path contains a zero-length segment."}
            points.append(point)
        return {"success": True, "points": points}

    @classmethod
    def _normalize_path_segments(cls, raw: Any, diameter_mm: float) -> dict[str, Any]:
        if not isinstance(raw, list) or not raw:
            return {"success": False, "message": "sweep path_segments must be a non-empty list."}
        segments: list[dict[str, Any]] = []
        total_length = 0.0
        all_points: list[list[float]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                return {"success": False, "message": f"sweep path segment {index + 1} must be an object."}
            segment_type = str(item.get("type") or "line").strip().lower()
            start = cls._point(item.get("start_mm") or item.get("start"))
            end = cls._point(item.get("end_mm") or item.get("end"))
            if segment_type not in {"line", "arc"} or start is None or end is None:
                return {"success": False, "message": f"sweep path segment {index + 1} requires type, start_mm, and end_mm."}
            if math.dist(start, end) <= 1e-9:
                return {"success": False, "message": f"sweep path segment {index + 1} has zero length."}
            if segments and math.dist(segments[-1]["end_mm"], start) > 1e-6:
                return {"success": False, "message": "sweep path segments must be contiguous and ordered."}
            normalized: dict[str, Any] = {"type": segment_type, "start_mm": start, "end_mm": end}
            if segment_type == "line":
                segment_length = math.dist(start, end)
            else:
                center = cls._point(item.get("center_mm") or item.get("center"))
                direction = str(item.get("direction") or "ccw").strip().lower()
                if center is None or direction not in {"cw", "ccw"}:
                    return {"success": False, "message": f"arc segment {index + 1} requires center_mm and direction cw/ccw."}
                start_radius = math.dist(start, center)
                end_radius = math.dist(end, center)
                if start_radius <= diameter_mm / 2.0 or abs(start_radius - end_radius) > 1e-4:
                    return {"success": False, "message": f"arc segment {index + 1} radius must be consistent and greater than profile radius."}
                sweep_angle = cls._arc_sweep_angle(center, start, end, direction)
                if sweep_angle <= 1e-9:
                    return {"success": False, "message": f"arc segment {index + 1} has zero sweep angle."}
                normalized.update({
                    "center_mm": center,
                    "direction": direction,
                    "radius_mm": start_radius,
                    "sweep_angle_rad": sweep_angle,
                })
                segment_length = start_radius * sweep_angle
            if segments and not cls._segments_are_tangent(segments[-1], normalized):
                return {"success": False, "message": f"sweep path joint before segment {index + 1} is not tangent."}
            segments.append(normalized)
            total_length += segment_length
            if not all_points:
                all_points.append(start)
            all_points.append(end)
            if segment_type == "arc":
                all_points.extend(cls._arc_cardinal_points(normalized))
        return {
            "success": True,
            "path_segments": segments,
            "path_points_mm": all_points,
            "path_length_mm": total_length,
        }

    @staticmethod
    def _is_straight_polyline(points: list[list[float]], tolerance: float = 1e-8) -> bool:
        vectors = [
            [second[0] - first[0], second[1] - first[1]]
            for first, second in zip(points, points[1:])
        ]
        base = vectors[0]
        base_length = math.hypot(*base)
        for vector in vectors[1:]:
            length = math.hypot(*vector)
            cross = base[0] * vector[1] - base[1] * vector[0]
            dot = base[0] * vector[0] + base[1] * vector[1]
            if abs(cross) > tolerance * base_length * length or dot <= 0:
                return False
        return True

    @classmethod
    def _segments_are_tangent(cls, first: dict[str, Any], second: dict[str, Any]) -> bool:
        first_tangent = cls._segment_tangent(first, at_end=True)
        second_tangent = cls._segment_tangent(second, at_end=False)
        dot = first_tangent[0] * second_tangent[0] + first_tangent[1] * second_tangent[1]
        return dot >= math.cos(math.radians(2.0))

    @staticmethod
    def _segment_tangent(segment: dict[str, Any], at_end: bool) -> list[float]:
        if segment["type"] == "line":
            vector = [
                segment["end_mm"][0] - segment["start_mm"][0],
                segment["end_mm"][1] - segment["start_mm"][1],
            ]
        else:
            point = segment["end_mm"] if at_end else segment["start_mm"]
            radial = [point[0] - segment["center_mm"][0], point[1] - segment["center_mm"][1]]
            vector = [-radial[1], radial[0]] if segment["direction"] == "ccw" else [radial[1], -radial[0]]
        length = math.hypot(*vector)
        return [vector[0] / length, vector[1] / length]

    @staticmethod
    def _arc_sweep_angle(center: list[float], start: list[float], end: list[float], direction: str) -> float:
        start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
        end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
        return (end_angle - start_angle) % (2.0 * math.pi) if direction == "ccw" else (start_angle - end_angle) % (2.0 * math.pi)

    @classmethod
    def _arc_cardinal_points(cls, segment: dict[str, Any]) -> list[list[float]]:
        result: list[list[float]] = []
        center = segment["center_mm"]
        radius = segment["radius_mm"]
        start_angle = math.atan2(segment["start_mm"][1] - center[1], segment["start_mm"][0] - center[0])
        sweep = segment["sweep_angle_rad"]
        for angle in (0.0, math.pi / 2.0, math.pi, 3.0 * math.pi / 2.0):
            delta = (angle - start_angle) % (2.0 * math.pi) if segment["direction"] == "ccw" else (start_angle - angle) % (2.0 * math.pi)
            if delta <= sweep + 1e-9:
                result.append([center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)])
        return result

    @staticmethod
    def _point(value: Any) -> list[float] | None:
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                return [float(value[0]), float(value[1])]
            if isinstance(value, dict):
                x = next((value[key] for key in ("x_mm", "x", "u_mm", "u") if key in value), None)
                y = next((value[key] for key in ("y_mm", "y", "v_mm", "v") if key in value), None)
                if x is not None and y is not None:
                    return [float(x), float(y)]
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _point3(value: Any) -> list[float] | None:
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                return [float(value[0]), float(value[1]), float(value[2])]
            if isinstance(value, dict):
                values = [
                    next((value[key] for key in aliases if key in value), None)
                    for aliases in (("x_mm", "x"), ("y_mm", "y"), ("z_mm", "z"))
                ]
                if all(item is not None for item in values):
                    return [float(item) for item in values]
        except (TypeError, ValueError):
            return None
        return None

    @classmethod
    def _plane(cls, value: Any) -> str | None:
        return cls.PLANES.get(str(value).strip().lower())

    @staticmethod
    def _number(values: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            if key not in values or values[key] in (None, ""):
                continue
            try:
                return float(values[key])
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

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
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        payload["files"] = list(dict.fromkeys(
            [str(item) for item in payload.get("files", [])] + [str(report_path)]
        ))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success,
            message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
