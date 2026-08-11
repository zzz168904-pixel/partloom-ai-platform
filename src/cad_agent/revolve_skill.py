from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .advanced_feature_skill import AdvancedFeatureSkill
from .model_integrity import SolidWorksModelInspector
from .models import SkillResult
from .profile_geometry import normalize_profile_extrude_request


class RevolveSkill:
    """Create one deterministic base, boss, or cut revolve.

    The Skill accepts an explicit closed half-profile. It never executes an
    LLM-generated macro, loads a sample model, exports a neutral format, or
    closes another document.
    """

    OPERATIONS = {"base", "boss", "cut"}
    REOPEN_TIMEOUT_S = 15.0
    REOPEN_POLL_S = 0.25
    PLANES = {
        "front": "Front Plane",
        "front plane": "Front Plane",
        "前视基准面": "Front Plane",
        "top": "Top Plane",
        "top plane": "Top Plane",
        "上视基准面": "Top Plane",
        "right": "Right Plane",
        "right plane": "Right Plane",
        "右视基准面": "Right Plane",
    }

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == "revolve"]
        if not features:
            return SkillResult(False, "No revolve feature found in the production plan.")

        run_dir = self.output_root / f"revolve_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "revolve_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document
            from sw_part import sketch, sketch_arc, sketch_circle, sketch_line

            sw, active = connect_solidworks(visible=True)
            operations: list[dict[str, Any]] = []
            files: list[str] = []
            model = active

            for index, feature in enumerate(features):
                request = self.normalize_request(feature.get("params", {}), plan.get("task_type"))
                if not request.get("success"):
                    operations.append({**request, "name": feature.get("name") or f"Revolve{index + 1}"})
                    return self._result(
                        False,
                        str(request.get("message") or "Invalid revolve request."),
                        report_path,
                        {"feature_type": "revolve", "operations": operations},
                    )

                mode = request["mode"]
                if mode == "new_model":
                    if index > 0:
                        return self._result(
                            False,
                            "Only the first revolve can create the task Part; subsequent revolves must use active_model.",
                            report_path,
                            {"feature_type": "revolve", "operations": operations},
                        )
                    model = new_document(sw, "part")
                validation = self._validate_active_part(model, require_body=mode == "active_model")
                if not validation.get("success"):
                    return self._result(False, str(validation["message"]), report_path, validation)

                before = ActiveModelThroughHoleExecutor._body_info(model) if mode == "active_model" else {}
                before = self._with_body_evidence(before)
                operation = self._execute(
                    model,
                    feature,
                    request,
                    sketch,
                    sketch_line,
                    sketch_arc,
                    sketch_circle,
                )
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(
                        False,
                        str(operation.get("message") or "SolidWorks revolve failed."),
                        report_path,
                        {
                            "active_doc": validation.get("active_doc"),
                            "feature_type": "revolve",
                            "operations": operations,
                        },
                    )

                model.ForceRebuild3(False)
                after = self._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
                exact_bbox = SolidWorksModelInspector.exact_body_bbox(model)
                if exact_bbox:
                    after["bbox"] = exact_bbox
                geometry_check = self._verify_geometry(request, before, after)
                operation["geometry_validation"] = geometry_check
                operation["bbox_after_m"] = after.get("bbox", {})
                operation["body_count_after"] = after.get("body_count", 0)
                if not geometry_check.get("success"):
                    return self._result(
                        False,
                        str(geometry_check.get("message")),
                        report_path,
                        {
                            "active_doc": validation.get("active_doc"),
                            "feature_type": "revolve",
                            "operations": operations,
                        },
                    )

            if model is None:
                return self._result(False, "SolidWorks did not return a Part document.", report_path, {})

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            material = str(plan.get("parameters", {}).get("material") or "").strip()
            material_metadata = self._set_material_metadata(model, material)
            reopen_validation: dict[str, Any] = {"success": True, "skipped": True, "reason": "active-model operation"}

            if operations and operations[0].get("mode") == "new_model":
                requested_path = str(plan.get("execution_model_path") or "").strip()
                part_path = Path(requested_path) if requested_path else run_dir / "revolved_part.SLDPRT"
                part_path.parent.mkdir(parents=True, exist_ok=True)
                if not save_document(model, str(part_path)):
                    return self._result(
                        False,
                        f"SolidWorks failed to save revolved Part: {part_path}",
                        report_path,
                        {"operations": operations},
                    )
                if not part_path.is_file() or part_path.stat().st_size <= 0:
                    return self._result(
                        False,
                        f"Saved revolved Part is missing or empty: {part_path}",
                        report_path,
                        {"operations": operations},
                    )
                files.append(str(part_path))
                reopen_validation = self._verify_reopen(sw, model, part_path)
                if not reopen_validation.get("success"):
                    return self._result(
                        False,
                        f"Saved revolved Part could not be reopened: {part_path}",
                        report_path,
                        {"operations": operations, "reopen_validation": reopen_validation, "files": files},
                    )
                model = reopen_validation.pop("model")

            title = str(get_com_member(model, "GetTitle") or "")
            data = {
                "active_doc": title,
                "mode": operations[0].get("mode") if operations else "",
                "feature_type": "revolve",
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
            return self._result(True, "SolidWorks revolve feature created and verified.", report_path, data)
        except Exception as exc:
            return self._result(
                False,
                f"revolve failed: {exc}",
                report_path,
                {"feature_type": "revolve", "error": repr(exc), "traceback": traceback.format_exc()},
            )

    @classmethod
    def normalize_request(cls, params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        raw_mode = str(params.get("mode") or "").strip().lower()
        operation = str(
            params.get("body_operation")
            or params.get("operation")
            or params.get("revolve_type")
            or (raw_mode if raw_mode in cls.OPERATIONS else "base")
        ).strip().lower()
        operation = {"revolve_boss": "boss", "add": "boss", "revolve_cut": "cut"}.get(operation, operation)
        if operation not in cls.OPERATIONS:
            return {"success": False, "message": "revolve operation must be base, boss, or cut."}

        expected_body_result = str(
            params.get("expected_body_result")
            or params.get("body_result")
            or ("merged" if operation == "boss" else "new_body" if operation == "base" else "same_body")
        ).strip().lower().replace("-", "_").replace(" ", "_")
        expected_body_result = {
            "merge": "merged",
            "separate": "new_body",
            "separate_body": "new_body",
        }.get(expected_body_result, expected_body_result)
        allowed_body_results = {
            "base": {"new_body"},
            "boss": {"merged", "new_body"},
            "cut": {"same_body"},
        }[operation]
        if expected_body_result not in allowed_body_results:
            allowed = " or ".join(sorted(allowed_body_results))
            return {
                "success": False,
                "message": f"revolve {operation} expected_body_result must be {allowed}.",
            }

        mode = str(
            params.get("execution_mode")
            or (raw_mode if raw_mode in {"new_model", "active_model"} else "")
            or ("new_model" if operation == "base" else "active_model")
        ).strip().lower()
        if mode not in {"new_model", "active_model"}:
            return {"success": False, "message": "revolve mode must be new_model or active_model."}
        if operation == "base" and mode != "new_model":
            return {"success": False, "message": "A base revolve must use new_model mode."}
        if task_type == "modify_3d" and mode == "new_model":
            return {"success": False, "message": "modify_3d cannot create a new revolve Part."}

        angle = cls._number(params, "angle_deg", "angle")
        angle = 360.0 if angle is None else float(angle)
        if not 0.0 < angle <= 360.0:
            return {"success": False, "message": "revolve angle_deg must be greater than 0 and at most 360."}

        plane_key = str(params.get("sketch_plane") or params.get("plane") or "front").strip().lower()
        plane_key = plane_key.replace("_", " ")
        plane = cls.PLANES.get(plane_key)
        if plane is None:
            return {"success": False, "message": "revolve sketch_plane must be front, top, or right."}
        support_face_offset = cls._number(
            params,
            "support_face_offset_mm",
            "sketch_face_offset_mm",
            "face_offset_mm",
        )
        support_face_context = None
        if support_face_offset is not None:
            inferred_axis = {"Front Plane": "z", "Top Plane": "y", "Right Plane": "x"}[plane]
            support_axis = str(params.get("support_face_axis") or inferred_axis).strip().lower().lstrip("+")
            if support_axis not in {"x", "y", "z"}:
                return {"success": False, "message": "revolve support_face_axis must be x, y, or z."}
            point_key = {
                "x": "support_face_point_yz_mm",
                "y": "support_face_point_xz_mm",
                "z": "support_face_point_xy_mm",
            }[support_axis]
            support_point = (
                params.get(point_key)
                or params.get("support_face_point_uv_mm")
                or params.get("support_face_point_mm")
            )
            if not isinstance(support_point, (tuple, list)) or len(support_point) < 2:
                return {
                    "success": False,
                    "message": f"{point_key} must contain two millimetre coordinates for a face-supported revolve.",
                }
            support_face_context = {
                "axis": support_axis,
                "face_offset_mm": float(support_face_offset),
                "point_uv_mm": [float(support_point[0]), float(support_point[1])],
            }
        axis = str(params.get("axis") or "horizontal").strip().lower().replace("-", "_").replace(" ", "_")
        axis = {
            "x": "horizontal",
            "x_axis": "horizontal",
            "center_axis": "horizontal",
            "local_horizontal": "horizontal",
            "local_horizontal_axis": "horizontal",
            "y": "vertical",
            "y_axis": "vertical",
            "local_vertical": "vertical",
            "local_vertical_axis": "vertical",
        }.get(axis, axis)
        if axis not in {"horizontal", "vertical"}:
            return {"success": False, "message": "revolve axis must be horizontal/x or vertical/y."}

        profile_result = cls._normalize_profile(params)
        if not profile_result.get("success"):
            return profile_result
        points = profile_result["profile_points_mm"]
        bbox = profile_result["profile_bbox_mm"]
        axis_offset = cls._number(params, "axis_offset_mm", "axis_offset")
        axis_offset = 0.0 if axis_offset is None else float(axis_offset)
        radial_min = float(bbox["ymin"]) - axis_offset
        radial_max = float(bbox["ymax"]) - axis_offset
        if radial_min < -1e-9:
            return {"success": False, "message": "revolve profile radius values cannot cross the selected axis."}
        if radial_max <= 1e-9:
            return {"success": False, "message": "revolve profile must contain a positive radius from the selected axis."}
        design_dimensions = params.get("design_dimensions")
        semantic_validation: dict[str, Any] = {"success": True, "applicable": False}
        if isinstance(design_dimensions, dict):
            semantic_validation = cls.validate_profile_against_design_dimensions(points, design_dimensions)
            if not semantic_validation.get("success"):
                return {
                    "success": False,
                    "message": str(
                        semantic_validation.get("message")
                        or "Revolve profile does not match the explicit named dimensions."
                    ),
                    "semantic_validation": semantic_validation,
                }
        return {
            "success": True,
            "mode": mode,
            "operation": operation,
            "expected_body_result": expected_body_result,
            "angle_deg": angle,
            "angle_rad": math.radians(angle),
            "reverse_direction": bool(params.get("reverse_direction", False)),
            "sketch_plane": plane,
            "support_face_context": support_face_context,
            "axis": axis,
            "axis_offset_mm": axis_offset,
            "profile_source": str(params.get("profile_source") or profile_result["profile_source"]),
            "profile_points_mm": points,
            "profile_segments_mm": profile_result.get("profile_segments_mm", []),
            "profile_bbox_mm": bbox,
            "axial_min_mm": float(bbox["xmin"]),
            "axial_max_mm": float(bbox["xmax"]),
            "radius_max_mm": radial_max,
            "design_dimensions": dict(design_dimensions) if isinstance(design_dimensions, dict) else {},
            "semantic_profile_validation": semantic_validation,
        }

    @classmethod
    def profile_from_named_dimensions(
        cls,
        parameters: dict[str, Any],
        source_brief: str = "",
    ) -> dict[str, Any]:
        """Build a lossless flanged-sleeve half-profile from explicit dimensions.

        This is schema reconciliation, not geometry guessing. It applies only
        when the plan explicitly supplies every required flange/sleeve value
        and identifies which end carries the flange.
        """

        aliases = {
            "total_length_mm": ("total_length_mm", "total_length", "overall_length_mm", "overall_length"),
            "main_outer_diameter_mm": (
                "main_outer_diameter_mm",
                "main_outer_diameter",
                "body_outer_diameter_mm",
                "body_outer_diameter",
            ),
            "flange_outer_diameter_mm": (
                "flange_outer_diameter_mm",
                "flange_outer_diameter",
                "flange_diameter_mm",
                "flange_diameter",
            ),
            "flange_thickness_mm": ("flange_thickness_mm", "flange_thickness"),
            "bore_diameter_mm": (
                "bore_diameter_mm",
                "bore_diameter",
                "inner_diameter_mm",
                "inner_diameter",
                "through_bore_diameter_mm",
            ),
        }
        flange_keys = {
            *aliases["flange_outer_diameter_mm"],
            *aliases["flange_thickness_mm"],
        }
        if not any(key in parameters for key in flange_keys):
            return {"applicable": False, "success": True}

        dimensions: dict[str, float] = {}
        missing: list[str] = []
        for target, keys in aliases.items():
            value = cls._first_dimension(parameters, keys)
            if value is None:
                missing.append(target)
            else:
                dimensions[target] = float(value)
        if missing:
            return {
                "applicable": True,
                "success": False,
                "message": f"Named revolve dimensions are incomplete: {', '.join(missing)}.",
                "missing": missing,
            }

        side_text = str(
            parameters.get("flange_side")
            or parameters.get("flange_position")
            or parameters.get("flange_end")
            or ""
        ).strip().lower()
        prompt = source_brief.lower()
        if any(token in side_text for token in ("left", "start", "左", "前端")) or any(
            token in prompt for token in ("左端法兰", "左侧法兰", "left-end flange", "left end flange")
        ):
            flange_side = "left"
        elif any(token in side_text for token in ("right", "end", "右", "后端")) or any(
            token in prompt for token in ("右端法兰", "右侧法兰", "right-end flange", "right end flange")
        ):
            flange_side = "right"
        else:
            return {
                "applicable": True,
                "success": False,
                "message": "The flange end is ambiguous; specify left/start or right/end.",
                "missing": ["flange_side"],
            }

        total = dimensions["total_length_mm"]
        main_diameter = dimensions["main_outer_diameter_mm"]
        flange_diameter = dimensions["flange_outer_diameter_mm"]
        flange_thickness = dimensions["flange_thickness_mm"]
        bore_diameter = dimensions["bore_diameter_mm"]
        if total <= 0 or main_diameter <= 0 or flange_diameter <= 0 or flange_thickness <= 0 or bore_diameter < 0:
            return {"applicable": True, "success": False, "message": "Named revolve dimensions must be positive."}
        if flange_thickness >= total:
            return {
                "applicable": True,
                "success": False,
                "message": "flange_thickness_mm must be smaller than total_length_mm.",
            }
        if flange_diameter < main_diameter:
            return {
                "applicable": True,
                "success": False,
                "message": "flange_outer_diameter_mm cannot be smaller than main_outer_diameter_mm.",
            }
        if bore_diameter >= min(main_diameter, flange_diameter):
            return {
                "applicable": True,
                "success": False,
                "message": "bore_diameter_mm must be smaller than every outer diameter.",
            }

        inner_radius = bore_diameter / 2.0
        main_radius = main_diameter / 2.0
        flange_radius = flange_diameter / 2.0
        if flange_side == "left":
            points = [
                [0.0, inner_radius],
                [total, inner_radius],
                [total, main_radius],
                [flange_thickness, main_radius],
                [flange_thickness, flange_radius],
                [0.0, flange_radius],
                [0.0, inner_radius],
            ]
        else:
            shoulder = total - flange_thickness
            points = [
                [0.0, inner_radius],
                [total, inner_radius],
                [total, flange_radius],
                [shoulder, flange_radius],
                [shoulder, main_radius],
                [0.0, main_radius],
                [0.0, inner_radius],
            ]
        design_dimensions = {**dimensions, "flange_side": flange_side}
        validation = cls.validate_profile_against_design_dimensions(points, design_dimensions)
        return {
            "applicable": True,
            "success": bool(validation.get("success")),
            "message": str(validation.get("message") or "Named revolve profile ready."),
            "profile_points_mm": points,
            "design_dimensions": design_dimensions,
            "semantic_validation": validation,
        }

    @classmethod
    def validate_profile_against_design_dimensions(
        cls,
        points: list[list[float]],
        dimensions: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(points, list) or len(points) < 4:
            return {"success": False, "applicable": True, "message": "Revolve profile is missing or incomplete."}
        try:
            total = float(dimensions["total_length_mm"])
            main_radius = float(dimensions["main_outer_diameter_mm"]) / 2.0
            flange_radius = float(dimensions["flange_outer_diameter_mm"]) / 2.0
            flange_thickness = float(dimensions["flange_thickness_mm"])
            inner_radius = float(dimensions["bore_diameter_mm"]) / 2.0
        except (KeyError, TypeError, ValueError) as exc:
            return {"success": False, "applicable": True, "message": f"Invalid design_dimensions: {exc}"}

        axial_span = max(point[0] for point in points) - min(point[0] for point in points)
        radius_min = min(point[1] for point in points)
        radius_max = max(point[1] for point in points)
        flange_span = cls._horizontal_span_at_radius(points, flange_radius)
        main_span = cls._horizontal_span_at_radius(points, main_radius)
        checks = {
            "total_length": abs(axial_span - total) <= 1e-6,
            "bore_diameter": abs(radius_min - inner_radius) <= 1e-6,
            "flange_outer_diameter": abs(radius_max - flange_radius) <= 1e-6,
            "flange_thickness": abs(flange_span - flange_thickness) <= 1e-6,
            "main_outer_diameter": abs(main_span - (total - flange_thickness)) <= 1e-6,
        }
        return {
            "success": all(checks.values()),
            "applicable": True,
            "message": "Named revolve dimensions match the full profile."
            if all(checks.values())
            else "Revolve profile omits or contradicts one or more explicit named dimensions.",
            "checks": checks,
            "measured": {
                "total_length_mm": axial_span,
                "main_outer_diameter_mm": main_radius * 2.0,
                "flange_outer_diameter_mm": radius_max * 2.0,
                "flange_thickness_mm": flange_span,
                "bore_diameter_mm": radius_min * 2.0,
                "main_span_mm": main_span,
            },
        }

    @staticmethod
    def _first_dimension(parameters: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for key in keys:
            if key not in parameters:
                continue
            value = parameters[key]
            if isinstance(value, dict):
                value = value.get("value")
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _horizontal_span_at_radius(points: list[list[float]], radius: float) -> float:
        span = 0.0
        for first, second in zip(points, points[1:]):
            if abs(first[1] - radius) <= 1e-6 and abs(second[1] - radius) <= 1e-6:
                span += abs(second[0] - first[0])
        return span

    @classmethod
    def _normalize_profile(cls, params: dict[str, Any]) -> dict[str, Any]:
        raw_profile_segments = params.get("profile_segments") or params.get("profile_loop_segments")
        if isinstance(raw_profile_segments, list) and raw_profile_segments:
            normalized = normalize_profile_extrude_request({
                "unit": params.get("unit", "mm"),
                "body_operation": "base",
                "mode": "new_model",
                "sketch_plane": "front",
                "depth_mm": 1.0,
                "profiles": [{"role": "outer", "segments": raw_profile_segments}],
            })
            if not normalized.get("success"):
                return {
                    "success": False,
                    "message": str(normalized.get("message") or "Invalid revolve profile segments."),
                    "errors": list(normalized.get("errors") or []),
                }
            loops = list(normalized.get("profiles") or [])
            if len(loops) != 1 or loops[0].get("role") != "outer":
                return {"success": False, "message": "revolve profile_segments must define exactly one outer loop."}
            segments = list(loops[0].get("segments") or [])
            points: list[list[float]] = []
            for segment in segments:
                if segment["type"] in {"line", "arc"}:
                    start = [float(value) for value in segment["start_mm"]]
                    end = [float(value) for value in segment["end_mm"]]
                    if not points:
                        points.append(start)
                    points.append(end)
                elif segment["type"] == "circle":
                    center = segment["center_mm"]
                    radius = float(segment["radius_mm"])
                    points = [
                        [float(center[0]) + radius, float(center[1])],
                        [float(center[0]), float(center[1]) + radius],
                        [float(center[0]) - radius, float(center[1])],
                        [float(center[0]), float(center[1]) - radius],
                        [float(center[0]) + radius, float(center[1])],
                    ]
            metrics = dict(normalized.get("profile_metrics") or {})
            bbox = dict(metrics.get("bbox_mm") or {})
            if not points or not bbox:
                return {"success": False, "message": "revolve profile_segments did not produce a measurable closed loop."}
            return {
                "success": True,
                "profile_points_mm": points,
                "profile_segments_mm": segments,
                "profile_bbox_mm": bbox,
                "profile_source": "profile_segments",
            }

        raw_segments = params.get("segments") or params.get("shaft_segments")
        if isinstance(raw_segments, list) and raw_segments:
            points_result = cls._profile_from_segments(raw_segments)
            if not points_result.get("success"):
                return points_result
            points = points_result["points"]
            source = "segments"
        else:
            raw_profile = params.get("profile") or params.get("profile_points")
            if not isinstance(raw_profile, list) or len(raw_profile) < 3:
                return {
                    "success": False,
                    "message": "revolve requires profile points or shaft segments with explicit dimensions.",
                }
            try:
                points = [cls._profile_point(item) for item in raw_profile]
            except (TypeError, ValueError) as exc:
                return {"success": False, "message": f"Invalid revolve profile point: {exc}"}
            source = "profile"

        cleaned: list[list[float]] = []
        for point in points:
            normalized = [float(point[0]), float(point[1])]
            if cleaned and cls._same_point(cleaned[-1], normalized):
                continue
            cleaned.append(normalized)
        if len(cleaned) < 3:
            return {"success": False, "message": "revolve profile contains fewer than three unique points."}
        if not cls._same_point(cleaned[0], cleaned[-1]):
            cleaned.append(list(cleaned[0]))
        if len(cleaned) < 4:
            return {"success": False, "message": "revolve profile is not a closed polygon."}
        if max(point[0] for point in cleaned) - min(point[0] for point in cleaned) <= 0:
            return {"success": False, "message": "revolve profile must have a positive axial span."}
        area = cls._polygon_area(cleaned)
        if abs(area) <= 1e-6:
            return {"success": False, "message": "revolve profile area is zero."}
        if cls._self_intersects(cleaned):
            return {"success": False, "message": "revolve profile self-intersects."}
        return {
            "success": True,
            "profile_points_mm": cleaned,
            "profile_segments_mm": [],
            "profile_bbox_mm": {
                "xmin": min(point[0] for point in cleaned),
                "xmax": max(point[0] for point in cleaned),
                "ymin": min(point[1] for point in cleaned),
                "ymax": max(point[1] for point in cleaned),
            },
            "profile_source": source,
        }

    @classmethod
    def _profile_from_segments(cls, segments: list[Any]) -> dict[str, Any]:
        points: list[list[float]] = []
        cursor = 0.0
        first_start: float | None = None
        for index, item in enumerate(segments):
            if not isinstance(item, dict):
                return {"success": False, "message": f"shaft segment {index + 1} must be an object."}
            diameter = cls._number(item, "diameter_mm", "diameter")
            start = cls._number(item, "start_mm", "start")
            end = cls._number(item, "end_mm", "end")
            length = cls._number(item, "length_mm", "length")
            start = cursor if start is None else float(start)
            if end is None:
                if length is None:
                    return {"success": False, "message": f"shaft segment {index + 1} requires length_mm or end_mm."}
                end = start + float(length)
            if diameter is None or float(diameter) <= 0 or float(end) <= start:
                return {"success": False, "message": f"shaft segment {index + 1} has invalid diameter or axial extent."}
            if index and abs(start - cursor) > 1e-6:
                return {"success": False, "message": "shaft segments must be contiguous and ordered."}
            if first_start is None:
                first_start = start
                points.extend([[start, 0.0], [start, float(diameter) / 2.0]])
            elif not cls._same_point(points[-1], [start, float(diameter) / 2.0]):
                points.append([start, float(diameter) / 2.0])
            points.append([float(end), float(diameter) / 2.0])
            cursor = float(end)
        if first_start is None:
            return {"success": False, "message": "shaft segments are empty."}
        points.extend([[cursor, 0.0], [first_start, 0.0]])
        return {"success": True, "points": points}

    @staticmethod
    def _profile_point(item: Any) -> list[float]:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            return [float(item[0]), float(item[1])]
        if not isinstance(item, dict):
            raise TypeError("point must be [axial_mm, radius_mm] or an object")
        axial = next((item[key] for key in ("axial_mm", "x_mm", "z_mm", "axis_mm", "x") if key in item), None)
        radius = next((item[key] for key in ("radius_mm", "r_mm", "radius", "r", "y_mm", "y") if key in item), None)
        if axial is None or radius is None:
            raise ValueError("point requires axial_mm and radius_mm")
        return [float(axial), float(radius)]

    def _execute(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        sketch_factory: Any,
        sketch_line: Any,
        sketch_arc: Any,
        sketch_circle: Any,
    ) -> dict[str, Any]:
        axis_line = None
        sketch_name = ""
        points = request["profile_points_mm"]
        profile_segments = request.get("profile_segments_mm") or []
        axial_min = request["axial_min_mm"]
        axial_max = request["axial_max_mm"]
        axis_offset = self._m(request["axis_offset_mm"])
        margin = max(5.0, (axial_max - axial_min) * 0.1)
        def draw_revolve_sketch() -> dict[str, Any] | None:
            nonlocal axis_line
            sketch_manager = model.SketchManager
            previous_add_to_db = bool(getattr(sketch_manager, "AddToDB", False))
            previous_display = bool(getattr(sketch_manager, "DisplayWhenAdded", True))
            try:
                sketch_manager.AddToDB = True
                sketch_manager.DisplayWhenAdded = False
                if request["axis"] == "horizontal":
                    axis_line = sketch_manager.CreateCenterLine(
                        self._m(axial_min - margin), axis_offset, 0.0,
                        self._m(axial_max + margin), axis_offset, 0.0,
                    )
                else:
                    axis_line = sketch_manager.CreateCenterLine(
                        axis_offset, self._m(axial_min - margin), 0.0,
                        axis_offset, self._m(axial_max + margin), 0.0,
                    )
                if profile_segments:
                    for profile_segment in profile_segments:
                        segment = self._create_profile_segment(
                            model,
                            profile_segment,
                            request["axis"],
                            sketch_line,
                            sketch_arc,
                            sketch_circle,
                        )
                        if segment is None:
                            return {"success": False, "message": "SolidWorks failed to create a revolve profile segment."}
                else:
                    for first, second in zip(points, points[1:]):
                        x1, y1 = self._sketch_coordinates(first, request["axis"])
                        x2, y2 = self._sketch_coordinates(second, request["axis"])
                        segment = sketch_manager.CreateLine(
                            self._m(x1), self._m(y1), 0.0,
                            self._m(x2), self._m(y2), 0.0,
                        )
                        if segment is None:
                            return {"success": False, "message": "SolidWorks failed to create a revolve profile segment."}
            finally:
                sketch_manager.AddToDB = previous_add_to_db
                sketch_manager.DisplayWhenAdded = previous_display
            return None

        support = request.get("support_face_context")
        if support:
            body_info = ActiveModelThroughHoleExecutor._body_info(model)
            if not body_info.get("success"):
                return {"success": False, "message": str(body_info.get("message") or "No solid body for face-supported revolve.")}
            face_offset = self._m(support["face_offset_mm"])
            support_uv = [self._m(value) for value in support["point_uv_mm"]]
            support_point = AdvancedFeatureSkill._model_point(support["axis"], face_offset, support_uv)
            face = AdvancedFeatureSkill._find_planar_face(
                body_info["bodies"],
                support["axis"],
                face_offset,
                support_point,
            )
            if face is None:
                return {
                    "success": False,
                    "message": "Could not resolve the requested planar support face for the revolve sketch.",
                    "support_face_context": support,
                }
            draw_error: dict[str, Any] | None = None

            def draw_on_face() -> None:
                nonlocal draw_error
                draw_error = draw_revolve_sketch()

            sketch_name = ActiveModelFeatureSkill._create_face_sketch(model, face, draw_on_face)
            if draw_error:
                return draw_error
            if not sketch_name:
                return {"success": False, "message": "SolidWorks did not create the face-supported revolve sketch."}
        else:
            with sketch_factory(model, request["sketch_plane"]) as sketch_name:
                draw_error = draw_revolve_sketch()
                if draw_error:
                    return draw_error

        selected = self._select_profile_and_axis(model, sketch_name, axis_line)
        if not selected:
            return {"success": False, "message": "Could not select the revolve sketch and centerline."}
        created = model.FeatureManager.FeatureRevolve2(
            True,
            True,
            False,
            request["operation"] == "cut",
            bool(request["reverse_direction"]),
            False,
            0,
            0,
            float(request["angle_rad"]),
            0.0,
            False,
            False,
            0.0,
            0.0,
            0,
            0.0,
            0.0,
            request["operation"] != "base",
            False,
            True,
        )
        if created is None:
            return {"success": False, "message": "SolidWorks FeatureRevolve2 returned no feature."}
        name = str(feature.get("name") or "Revolve")
        ActiveModelFeatureSkill._name_feature(created, name)
        return {
            "success": True,
            "name": name,
            "type": "revolve",
            "mode": request["mode"],
            "operation": request["operation"],
            "expected_body_result": request["expected_body_result"],
            "angle_deg": request["angle_deg"],
            "axis": request["axis"],
            "axis_offset_mm": request["axis_offset_mm"],
            "sketch_plane": request["sketch_plane"],
            "support_face_context": request.get("support_face_context"),
            "profile_source": request["profile_source"],
            "profile_points_mm": points,
            "profile_segments_mm": profile_segments,
            "design_dimensions": request.get("design_dimensions", {}),
            "semantic_profile_validation": request.get("semantic_profile_validation", {}),
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    def _create_profile_segment(
        self,
        model: Any,
        segment: dict[str, Any],
        axis: str,
        sketch_line: Any,
        sketch_arc: Any,
        sketch_circle: Any,
    ) -> Any:
        if segment["type"] == "line":
            x1, y1 = self._sketch_coordinates(segment["start_mm"], axis)
            x2, y2 = self._sketch_coordinates(segment["end_mm"], axis)
            return sketch_line(model, self._m(x1), self._m(y1), self._m(x2), self._m(y2))
        center = segment["center_mm"]
        cx, cy = self._sketch_coordinates(center, axis)
        if segment["type"] == "circle":
            return sketch_circle(model, self._m(cx), self._m(cy), self._m(segment["radius_mm"]))
        start = segment["start_mm"]
        end = segment["end_mm"]
        x1, y1 = self._sketch_coordinates(start, axis)
        x2, y2 = self._sketch_coordinates(end, axis)
        direction = int(segment["direction"])
        if axis == "vertical":
            direction *= -1
        return sketch_arc(
            model,
            self._m(cx),
            self._m(cy),
            self._m(x1),
            self._m(y1),
            self._m(x2),
            self._m(y2),
            direction,
        )

    @staticmethod
    def _select_profile_and_axis(model: Any, sketch_name: str, axis_line: Any) -> bool:
        model.ClearSelection2(True)
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", sketch_name)
        sketch_selected = False
        if feature is not None:
            try:
                sketch_selected = bool(feature.Select2(False, 0))
            except Exception:
                sketch_selected = False
        if not sketch_selected:
            sketch_selected = ActiveModelFeatureSkill._select_sketch(model, sketch_name)
        if not sketch_selected:
            return False
        if axis_line is None:
            return True
        try:
            select_data = model.SelectionManager.CreateSelectData()
            select_data.Mark = 4
            if axis_line.Select4(True, select_data):
                return True
        except Exception:
            pass
        # A sketch with exactly one construction centerline is accepted by
        # FeatureRevolve2 on supported SolidWorks versions without a separate
        # axis selection. Keep the selected sketch as the deterministic fallback.
        return True

    @classmethod
    def _verify_geometry(
        cls,
        request: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> dict[str, Any]:
        if not after.get("success") or int(after.get("body_count", 0)) <= 0:
            return {"success": False, "message": "Revolve produced no solid body."}
        if request["operation"] == "base":
            volume_after = after.get("volume_m3")
            if volume_after is None or float(volume_after) <= 0.0:
                return {"success": False, "message": "Base revolve did not produce measurable solid volume."}
            bbox = after.get("bbox", {})
            axial_span = request["axial_max_mm"] - request["axial_min_mm"]
            expected_diameter = request["radius_max_mm"] * 2.0
            spans = sorted(
                [
                    abs(float(bbox.get("xmax", 0)) - float(bbox.get("xmin", 0))) * 1000.0,
                    abs(float(bbox.get("ymax", 0)) - float(bbox.get("ymin", 0))) * 1000.0,
                    abs(float(bbox.get("zmax", 0)) - float(bbox.get("zmin", 0))) * 1000.0,
                ]
            )
            if request["angle_deg"] >= 359.999:
                expected = sorted([axial_span, expected_diameter, expected_diameter])
                if any(abs(actual - target) > max(0.25, target * 0.01) for actual, target in zip(spans, expected)):
                    return {
                        "success": False,
                        "message": "Revolved body bounding box does not match the requested full-revolve profile.",
                        "expected_spans_mm": expected,
                        "actual_spans_mm": spans,
                    }
            return {
                "success": True,
                "expected_axial_span_mm": axial_span,
                "expected_diameter_mm": expected_diameter,
                "actual_spans_mm": spans,
                "volume_after_m3": float(volume_after),
            }

        volume_before = before.get("volume_m3")
        volume_after = after.get("volume_m3")
        if volume_before is None or volume_after is None:
            return {"success": False, "message": "Could not measure solid volume across the active-model revolve."}
        volume_before = float(volume_before)
        volume_after = float(volume_after)
        volume_delta = volume_after - volume_before
        volume_tolerance = max(abs(volume_before), abs(volume_after), 1e-12) * 1e-9
        body_count_before = int(before.get("body_count", 0))
        body_count_after = int(after.get("body_count", 0))
        if request["operation"] == "cut":
            if volume_delta >= -volume_tolerance:
                return {
                    "success": False,
                    "message": "Revolve cut did not remove measurable solid volume.",
                    "volume_before_m3": volume_before,
                    "volume_after_m3": volume_after,
                    "volume_delta_m3": volume_delta,
                }
        else:
            if volume_delta <= volume_tolerance:
                return {
                    "success": False,
                    "message": "Revolve boss did not add measurable solid volume.",
                    "volume_before_m3": volume_before,
                    "volume_after_m3": volume_after,
                    "volume_delta_m3": volume_delta,
                }
            expected_body_result = str(request.get("expected_body_result") or "merged")
            if expected_body_result == "merged" and body_count_after != body_count_before:
                return {
                    "success": False,
                    "message": "Revolve boss created a separate solid instead of merging with the target body.",
                    "body_count_before": body_count_before,
                    "body_count_after": body_count_after,
                    "volume_delta_m3": volume_delta,
                }
            if expected_body_result == "new_body" and body_count_after != body_count_before + 1:
                return {
                    "success": False,
                    "message": "Revolve boss did not create exactly one requested new solid body.",
                    "body_count_before": body_count_before,
                    "body_count_after": body_count_after,
                    "volume_delta_m3": volume_delta,
                }
        before_faces = sum(int(item.get("face_count", 0)) for item in before.get("body_info", []))
        after_faces = sum(int(item.get("face_count", 0)) for item in after.get("body_info", []))
        if before_faces == after_faces:
            return {"success": False, "message": "Active-model revolve did not change the body topology."}
        return {
            "success": True,
            "face_count_before": before_faces,
            "face_count_after": after_faces,
            "body_count_before": body_count_before,
            "body_count_after": body_count_after,
            "expected_body_result": request.get("expected_body_result"),
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_delta_m3": volume_delta,
        }

    @staticmethod
    def _with_body_evidence(info: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(info)
        bodies = list(info.get("bodies", [])) if info.get("success") else []
        body_info: list[dict[str, int]] = []
        for body in bodies:
            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces") or ()
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            body_info.append({"face_count": len(faces), "edge_count": len(edges)})
        enriched["body_count"] = len(bodies)
        enriched["body_info"] = body_info
        enriched["volume_m3"] = ActiveModelThroughHoleExecutor._solid_volume(bodies)
        return enriched

    @staticmethod
    def _validate_active_part(model: Any, require_body: bool) -> dict[str, Any]:
        if model is None:
            return {"success": False, "message": "ActiveDoc does not exist."}
        title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
        doc_type = int(ActiveModelThroughHoleExecutor._com_member(model, "GetType", default=0) or 0)
        if doc_type != 1:
            return {"success": False, "message": f"ActiveDoc is not a Part: {title}", "active_doc": title}
        if require_body:
            body = ActiveModelThroughHoleExecutor._body_info(model)
            if not body.get("success"):
                return {"success": False, "message": str(body.get("message")), "active_doc": title}
        return {"success": True, "active_doc": title}

    @staticmethod
    def _set_material_metadata(model: Any, material: str) -> dict[str, Any]:
        if not material:
            return {"requested": "", "custom_property_written": False}
        written = False
        error = ""
        try:
            manager = model.Extension.CustomPropertyManager("")
            result = manager.Add3("Material", 30, material, 1)
            written = result is not None
        except Exception as exc:
            error = repr(exc)
        return {"requested": material, "custom_property_written": written, "error": error}

    @staticmethod
    def _verify_reopen(sw: Any, model: Any, path: Path) -> dict[str, Any]:
        try:
            path = path.resolve()
            current_path = str(ActiveModelThroughHoleExecutor._com_member(model, "GetPathName", default="") or "")
            if not current_path or Path(current_path).resolve() != path:
                return {"success": False, "message": "Active task document path changed before reopen validation."}
            title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
            if title:
                sw.CloseDoc(title)
            opened = None
            errors: list[str] = []
            open_status: dict[str, Any] = {}
            for method_name, args in (
                ("OpenDoc6", (str(path), 1, 1, "", 0, 0)),
                ("OpenDoc", (str(path), 1)),
                ("LoadFile2", (str(path), "")),
            ):
                try:
                    result = getattr(sw, method_name)(*args)
                    candidate = result[0] if isinstance(result, tuple) else result
                    active = getattr(sw, "ActiveDoc", None)
                    active_path = str(
                        ActiveModelThroughHoleExecutor._com_member(
                            active,
                            "GetPathName",
                            default="",
                        )
                        or ""
                    )
                    if active_path and Path(active_path).resolve() == path:
                        opened = active
                    elif candidate is not None and not isinstance(candidate, (bool, int, float, str)):
                        opened = candidate
                    if isinstance(result, tuple):
                        open_status = {
                            "method": method_name,
                            "errors": int(result[1] or 0) if len(result) > 1 else 0,
                            "warnings": int(result[2] or 0) if len(result) > 2 else 0,
                        }
                    else:
                        open_status = {"method": method_name, "errors": 0, "warnings": 0}
                    if opened is not None:
                        break
                except Exception as exc:
                    errors.append(f"{method_name}: {exc!r}")
            info = (
                RevolveSkill._wait_for_reopened_body(opened)
                if opened is not None
                else {"success": False, "body_count": 0, "load_attempts": 0, "load_timed_out": False}
            )
            reopened_path = str(ActiveModelThroughHoleExecutor._com_member(opened, "GetPathName", default="") or "")
            success = bool(
                opened is not None
                and reopened_path
                and Path(reopened_path).resolve() == path
                and info.get("success")
                and int(info.get("body_count", 0)) > 0
            )
            return {
                "success": success,
                "path": str(path),
                "active_path": reopened_path,
                "body_count": info.get("body_count", 0),
                "bbox_m": info.get("bbox", {}),
                "load_attempts": info.get("load_attempts", 0),
                "load_wait_s": info.get("load_wait_s", 0.0),
                "load_timed_out": info.get("load_timed_out", False),
                "attempt_errors": errors,
                "open_status": open_status,
                "model": opened,
            }
        except Exception as exc:
            return {"success": False, "path": str(path), "error": repr(exc)}

    @staticmethod
    def _wait_for_reopened_body(model: Any) -> dict[str, Any]:
        """Wait for SolidWorks to finish materializing bodies after OpenDoc.

        Complex parts can expose their feature tree before GetBodies2 returns
        the loaded body. A bounded poll keeps reopen validation deterministic
        without treating that transient state as an empty model.
        """
        started = time.monotonic()
        attempts = 0
        last_info: dict[str, Any] = {"success": False, "body_count": 0}
        while True:
            attempts += 1
            try:
                ActiveModelThroughHoleExecutor._com_member(model, "ForceRebuild3", False, default=None)
            except Exception:
                pass
            last_info = RevolveSkill._with_body_evidence(
                ActiveModelThroughHoleExecutor._body_info(model)
            )
            if last_info.get("success") and int(last_info.get("body_count", 0) or 0) > 0:
                return {
                    **last_info,
                    "load_attempts": attempts,
                    "load_wait_s": round(time.monotonic() - started, 3),
                    "load_timed_out": False,
                }
            elapsed = time.monotonic() - started
            if elapsed >= RevolveSkill.REOPEN_TIMEOUT_S:
                return {
                    **last_info,
                    "load_attempts": attempts,
                    "load_wait_s": round(elapsed, 3),
                    "load_timed_out": True,
                }
            time.sleep(RevolveSkill.REOPEN_POLL_S)

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
    def _sketch_coordinates(point: list[float], axis: str) -> tuple[float, float]:
        return (point[0], point[1]) if axis == "horizontal" else (point[1], point[0])

    @staticmethod
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

    @staticmethod
    def _same_point(first: list[float], second: list[float], tolerance: float = 1e-9) -> bool:
        return abs(first[0] - second[0]) <= tolerance and abs(first[1] - second[1]) <= tolerance

    @staticmethod
    def _polygon_area(points: list[list[float]]) -> float:
        return 0.5 * sum(first[0] * second[1] - second[0] * first[1] for first, second in zip(points, points[1:]))

    @classmethod
    def _self_intersects(cls, points: list[list[float]]) -> bool:
        segments = list(zip(points, points[1:]))
        for first_index, (a1, a2) in enumerate(segments):
            for second_index, (b1, b2) in enumerate(segments):
                if second_index <= first_index + 1:
                    continue
                if first_index == 0 and second_index == len(segments) - 1:
                    continue
                if cls._segments_intersect(a1, a2, b1, b2):
                    return True
        return False

    @staticmethod
    def _segments_intersect(a: list[float], b: list[float], c: list[float], d: list[float]) -> bool:
        def orientation(p: list[float], q: list[float], r: list[float]) -> float:
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        o1 = orientation(a, b, c)
        o2 = orientation(a, b, d)
        o3 = orientation(c, d, a)
        o4 = orientation(c, d, b)
        return o1 * o2 < -1e-9 and o3 * o4 < -1e-9

    @staticmethod
    def _number(values: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = values.get(key)
            if value is not None and value != "":
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        existing_files = [str(item) for item in payload.get("files", [])]
        payload["files"] = list(dict.fromkeys(existing_files + [str(report_path)]))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success=success,
            message=message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
