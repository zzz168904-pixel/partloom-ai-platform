from __future__ import annotations

import json
import math
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .models import SkillResult


SUPPORTED_ADVANCED_FEATURES = {"side_boss", "side_hole", "rib"}


class AdvancedFeatureSkill:
    """Create gearbox-style side features on the active production Part.

    The Skill operates only on geometric planar faces. It does not create a
    document, load a sample model, save the Part, or export another format.
    """

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any], feature_type: str) -> SkillResult:
        if feature_type not in SUPPORTED_ADVANCED_FEATURES:
            return SkillResult(False, f"Unsupported advanced feature type: {feature_type}")
        features = [item for item in plan.get("features", []) if item.get("type") == feature_type]
        if not features:
            return SkillResult(False, f"No {feature_type} feature found in the production plan.")

        run_dir = self.output_root / f"advanced_{feature_type}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / f"advanced_{feature_type}_report.json"
        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member

            _sw, model = connect_solidworks(visible=True)
            if model is None:
                return self._result(False, "ActiveDoc does not exist.", report_path, {"feature_type": feature_type})
            title = str(get_com_member(model, "GetTitle") or "")
            if int(get_com_member(model, "GetType")) != 1:
                return self._result(False, f"ActiveDoc is not a Part: {title}", report_path, {"active_doc": title})

            operations: list[dict[str, Any]] = []
            for feature in features:
                request = self.normalize_request(feature_type, feature.get("params", {}))
                if not request.get("success"):
                    operations.append(request)
                    return self._result(
                        False,
                        str(request.get("message") or f"Invalid {feature_type} request."),
                        report_path,
                        {"active_doc": title, "feature_type": feature_type, "operations": operations},
                    )
                body_info = ActiveModelThroughHoleExecutor._body_info(model)
                if not body_info.get("success"):
                    return self._result(False, str(body_info.get("message")), report_path, {"active_doc": title, "operations": operations})
                operation = self._execute_feature(model, feature, request, body_info)
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(
                        False,
                        str(operation.get("message") or f"{feature_type} failed"),
                        report_path,
                        {"active_doc": title, "feature_type": feature_type, "operations": operations},
                    )

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            data = {
                "active_doc": title,
                "mode": "active_model",
                "feature_type": feature_type,
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
            return self._result(True, f"Active-model {feature_type} created.", report_path, data)
        except Exception as exc:
            return self._result(
                False,
                f"advanced {feature_type} failed: {exc}",
                report_path,
                {"feature_type": feature_type, "error": repr(exc), "traceback": traceback.format_exc()},
            )

    @classmethod
    def normalize_request(cls, feature_type: str, params: dict[str, Any]) -> dict[str, Any]:
        if feature_type not in SUPPORTED_ADVANCED_FEATURES:
            return {"success": False, "message": f"Unsupported advanced feature type: {feature_type}"}
        profile = str(params.get("profile") or params.get("rib_profile") or "").strip().lower()
        if feature_type == "rib" and profile in {"native_line", "line", "open_line"}:
            sketch_plane_feature = str(
                params.get("sketch_plane_feature")
                or params.get("reference_plane_feature")
                or params.get("plane_feature")
                or ""
            ).strip()
            points = params.get("line_points_mm") or params.get("points_mm")
            thickness = cls._number(params, "thickness_mm", "thickness", "width_mm", "width")
            if not sketch_plane_feature:
                return {"success": False, "message": "native_line rib requires sketch_plane_feature."}
            if (
                not isinstance(points, (tuple, list))
                or len(points) != 2
                or any(not isinstance(point, (tuple, list)) or len(point) < 2 for point in points)
            ):
                return {"success": False, "message": "native_line rib requires exactly two 2D line_points_mm."}
            if thickness is None or thickness <= 0.0:
                return {"success": False, "message": "native_line rib requires a positive thickness_mm."}
            draft_angle = float(params.get("draft_angle_deg", 0.0) or 0.0)
            if draft_angle < 0.0 or draft_angle >= 90.0:
                return {"success": False, "message": "rib draft_angle_deg must be at least 0 and less than 90."}
            return {
                "success": True,
                "type": "rib",
                "profile": "native_line",
                "sketch_plane_feature": sketch_plane_feature,
                "line_points_mm": [
                    [float(points[0][0]), float(points[0][1])],
                    [float(points[1][0]), float(points[1][1])],
                ],
                "thickness_mm": float(thickness),
                "is_two_sided": bool(params.get("is_two_sided", True)),
                "reverse_thickness_direction": bool(params.get("reverse_thickness_direction", False)),
                "reference_edge_index": int(params.get("reference_edge_index", 0)),
                "reverse_material_direction": bool(params.get("reverse_material_direction", True)),
                "is_drafted": bool(params.get("is_drafted", draft_angle > 0.0)),
                "draft_outward": bool(params.get("draft_outward", True)),
                "draft_angle_deg": draft_angle,
                "is_normal_to_sketch": bool(params.get("is_normal_to_sketch", False)),
                "is_drafted_from_wall": bool(params.get("is_drafted_from_wall", False)),
                "count": 1,
            }
        axis = str(params.get("axis") or "y").lower().strip().lstrip("+")
        if axis not in {"x", "y", "z"}:
            return {"success": False, "message": "axis must be 'x', 'y', or 'z'."}
        face_offset = cls._number(params, "face_offset_mm", "support_face_offset_mm", "target_face_offset_mm")
        if face_offset is None:
            return {"success": False, "message": "A signed face_offset_mm is required."}

        center_key = {
            "x": "center_yz_mm",
            "y": "center_xz_mm",
            "z": "center_xy_mm",
        }[axis]
        center = params.get(center_key) or params.get("center_mm") or params.get("center")
        if not isinstance(center, (tuple, list)) or len(center) < 2:
            return {"success": False, "message": f"{center_key} must contain two millimetre coordinates."}
        center_uv = [float(center[0]), float(center[1])]

        base = {
            "success": True,
            "type": feature_type,
            "axis": axis,
            "face_offset_mm": float(face_offset),
            "center_uv_mm": center_uv,
        }
        if feature_type == "side_boss":
            diameter = cls._number(params, "diameter_mm", "outer_diameter_mm", "diameter", "outer_diameter")
            depth = cls._number(params, "depth_mm", "depth", "length_mm", "length")
            if diameter is None or depth is None or min(diameter, depth) <= 0:
                return {"success": False, "message": "side_boss requires positive diameter_mm and depth_mm."}
            return {
                **base,
                "diameter_mm": float(diameter),
                "depth_mm": float(depth),
                "merge_result": True,
            }

        if feature_type == "side_hole":
            diameter = cls._number(params, "diameter_mm", "diameter", "hole_diameter_mm", "hole_diameter")
            if diameter is None or diameter <= 0:
                return {"success": False, "message": "side_hole requires a positive diameter_mm."}
            placement = str(params.get("placement") or "center").lower().strip()
            if placement not in {"center", "bolt_circle"}:
                return {"success": False, "message": "side_hole placement must be 'center' or 'bolt_circle'."}
            raw_end_condition = str(
                params.get("end_condition")
                or ("through_all" if params.get("through_all", True) else "blind")
            ).lower().strip()
            end_condition = {
                "throughnext": "through_next",
                "through_next": "through_next",
                "through all": "through_all",
                "through_all": "through_all",
                "blind": "blind",
            }.get(raw_end_condition)
            if end_condition is None:
                return {
                    "success": False,
                    "message": "side_hole end_condition must be 'through_next', 'through_all', or 'blind'.",
                }
            depth = cls._number(params, "depth_mm", "depth")
            if end_condition == "blind" and (depth is None or depth <= 0.0):
                return {
                    "success": False,
                    "message": "blind side_hole requires a positive depth_mm.",
                }
            request = {
                **base,
                "diameter_mm": float(diameter),
                "placement": placement,
                "end_condition": end_condition,
                "depth_mm": float(depth or 0.0),
                "through_all": end_condition == "through_all",
            }
            if placement == "center":
                request.update({"count": 1, "centers_uv_mm": [center_uv]})
                return request
            pcd = cls._number(params, "pcd_mm", "pcd", "pitch_circle_diameter_mm")
            count = int(params.get("count", 0) or 0)
            start_angle = float(params.get("start_angle_deg", 0) or 0)
            if pcd is None or pcd <= 0 or count < 2:
                return {"success": False, "message": "bolt_circle side_hole requires pcd_mm > 0 and count >= 2."}
            centers = []
            for index in range(count):
                angle = math.radians(start_angle + 360.0 * index / count)
                centers.append([
                    center_uv[0] + float(pcd) * math.cos(angle) / 2.0,
                    center_uv[1] + float(pcd) * math.sin(angle) / 2.0,
                ])
            request.update(
                {
                    "count": count,
                    "pcd_mm": float(pcd),
                    "start_angle_deg": start_angle,
                    "centers_uv_mm": centers,
                }
            )
            return request

        width = cls._number(params, "width_mm", "width", "thickness_mm", "thickness")
        height = cls._number(params, "height_mm", "height")
        depth = cls._number(params, "depth_mm", "depth")
        positions = params.get("center_positions_mm") or params.get("positions_mm")
        if positions is None:
            positions = [center_uv[0]]
        if not isinstance(positions, (tuple, list)) or not positions:
            return {"success": False, "message": "rib center_positions_mm must be a non-empty list."}
        if width is None or height is None or depth is None or min(width, height, depth) <= 0:
            return {"success": False, "message": "rib requires positive width_mm, height_mm, and depth_mm."}
        base_z = float(params.get("base_z_mm", center_uv[1] - float(height) / 2.0))
        return {
            **base,
            "profile": "rectangular",
            "width_mm": float(width),
            "height_mm": float(height),
            "depth_mm": float(depth),
            "base_z_mm": base_z,
            "center_positions_mm": [float(value) for value in positions],
            "count": len(positions),
        }

    def _execute_feature(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "side_boss": self._create_side_boss,
            "side_hole": self._create_side_hole,
            "rib": self._create_rib,
        }
        return handlers[str(feature.get("type"))](model, feature, request, body_info)

    def _create_side_boss(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        axis = request["axis"]
        face_offset = self._m(request["face_offset_mm"])
        center = [self._m(value) for value in request["center_uv_mm"]]
        diameter = self._m(request["diameter_mm"])
        depth = self._m(request["depth_mm"])
        point = self._model_point(axis, face_offset, center)
        face = self._find_planar_face(body_info["bodies"], axis, face_offset, point)
        if face is None:
            return self._failure(
                "Could not find an outward planar support face at the requested point.",
                request=request,
                start_point_model_mm=[round(value * 1000.0, 6) for value in point],
            )
        face_normal = self._face_normal(face)
        if face_normal is None:
            return self._failure(
                "The requested side support face does not expose a planar outward normal.",
                request=request,
            )
        outward_axis = self._axis_vector(axis, 1.0 if face_offset >= 0.0 else -1.0)
        if self._dot(face_normal, outward_axis) < 0.999:
            return self._failure(
                "The resolved support face normal does not point outward on the requested side.",
                request=request,
                face_normal=face_normal,
                requested_outward=outward_axis,
            )
        volume_before = ActiveModelThroughHoleExecutor._solid_volume(body_info["bodies"])
        body_count_before = len(body_info["bodies"])
        if volume_before is None:
            return self._failure(
                "Could not read the solid volume before creating the side boss.",
                request=request,
            )

        def draw_boss() -> None:
            sketch_point = self._to_sketch_point(model, point)
            model.SketchManager.CreateCircleByRadius(*sketch_point, diameter / 2.0)

        sketch_name = ActiveModelFeatureSkill._create_face_sketch(
            model,
            face,
            draw_boss,
        )
        # SOLIDWORKS defines a face-sketch boss's default direction as the
        # selected face normal. Reversing it would extrude back into the
        # body and can create a feature-tree node without adding material.
        reverse_direction = False
        created = ActiveModelFeatureSkill._extrude_boss(
            model,
            sketch_name,
            depth,
            direction=reverse_direction,
            merge=bool(request["merge_result"]),
        )
        start_condition = "sketch_face"
        if created is None:
            return self._failure(
                "SolidWorks failed to create the outward merged side boss.",
                request=request,
                face_normal=face_normal,
                reverse_direction=reverse_direction,
                start_condition=start_condition,
                sketch_name=sketch_name,
            )
        name = str(feature.get("name") or "SideBoss")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        body_after = ActiveModelThroughHoleExecutor._body_info(model)
        if not body_after.get("success"):
            return self._failure(
                "Could not inspect solid bodies after creating the side boss.",
                request=request,
                face_normal=face_normal,
            )
        bodies_after = list(body_after.get("bodies") or [])
        volume_after = ActiveModelThroughHoleExecutor._solid_volume(bodies_after)
        if volume_after is None:
            return self._failure(
                "Could not read the solid volume after creating the side boss.",
                request=request,
                face_normal=face_normal,
            )
        volume_delta = float(volume_after) - float(volume_before)
        volume_tolerance = max(abs(float(volume_before)), abs(float(volume_after)), 1e-12) * 1e-9
        if volume_delta <= volume_tolerance:
            return self._failure(
                "The side boss feature did not add material to the solid body.",
                request=request,
                face_normal=face_normal,
                volume_before_m3=volume_before,
                volume_after_m3=volume_after,
                volume_delta_m3=volume_delta,
            )
        if len(bodies_after) != body_count_before:
            return self._failure(
                "The side boss created a separate solid instead of merging with the target body.",
                request=request,
                face_normal=face_normal,
                body_count_before=body_count_before,
                body_count_after=len(bodies_after),
                volume_delta_m3=volume_delta,
            )
        expected_outer = face_offset + face_normal[{"x": 0, "y": 1, "z": 2}[axis]] * depth
        extent = self._feature_axis_extent(created, axis)
        if extent and not (extent[0] - 1e-5 <= expected_outer <= extent[1] + 1e-5):
            return self._failure(
                "The side boss did not extend outward to the requested depth.",
                request=request,
                expected_outer_m=expected_outer,
                feature_extent_m=extent,
            )
        return {
            "success": True,
            "name": name,
            "type": "side_boss",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "center_model_mm": [round(value * 1000.0, 6) for value in point],
            "outer_face_offset_mm": round(expected_outer * 1000.0, 6),
            "feature_extent_m": extent,
            "face_normal": face_normal,
            "extrusion_direction": face_normal,
            "reverse_direction": reverse_direction,
            "merge_result": True,
            "start_condition": start_condition,
            "support_face_offset_mm": request["face_offset_mm"],
            "start_offset_mm": 0.0,
            "sketch_name": sketch_name,
            "body_count_before": body_count_before,
            "body_count_after": len(bodies_after),
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_delta_m3": volume_delta,
        }

    def _create_side_hole(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        material_before = self._material_state(body_info)
        if not material_before.get("success"):
            return self._failure(str(material_before["message"]), request=request)
        axis = request["axis"]
        face_offset = self._m(request["face_offset_mm"])
        center = [self._m(value) for value in request["center_uv_mm"]]
        target_point = self._model_point(axis, face_offset, center)
        face = self._find_planar_face(body_info["bodies"], axis, face_offset, target_point)
        if face is None:
            return self._failure("Could not find the requested side-hole face.", request=request)
        centers = [[self._m(value) for value in pair] for pair in request["centers_uv_mm"]]
        radius = self._m(request["diameter_mm"]) / 2.0

        def draw_holes() -> None:
            for item in centers:
                point = self._model_point(axis, face_offset, item)
                sketch_point = self._to_sketch_point(model, point)
                model.SketchManager.CreateCircleByRadius(*sketch_point, radius)

        sketch_name = ActiveModelFeatureSkill._create_face_sketch(model, face, draw_holes)
        cut_depth = max(float(body_info["bbox"].get("length", 0)), float(body_info["bbox"].get("width", 0))) * 2.5
        end_condition = str(request["end_condition"])
        end_condition_code = {
            "blind": 0,
            "through_all": 1,
            "through_next": 2,
        }[end_condition]
        depth = (
            self._m(request["depth_mm"])
            if end_condition == "blind"
            else max(cut_depth, self._m(100.0))
        )
        created = self._extrude_cut_end_condition(
            model,
            sketch_name,
            end_condition_code=end_condition_code,
            depth=depth,
        )
        if created is None:
            return self._failure(
                f"SolidWorks failed to create the side holes with {end_condition!r} end condition.",
                request=request,
            )
        name = str(feature.get("name") or "SideHole")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        body_after = ActiveModelThroughHoleExecutor._body_info(model)
        if not body_after.get("success"):
            return self._failure(
                "Could not inspect solid bodies after creating the side hole.",
                request=request,
            )
        volume_after = ActiveModelThroughHoleExecutor._solid_volume(body_after["bodies"])
        volume_before = material_before["volume_before_m3"]
        if volume_after is None:
            return self._failure(
                "Could not read the solid volume after creating the side hole.",
                request=request,
            )
        volume_delta = float(volume_after) - float(volume_before)
        volume_tolerance = max(abs(float(volume_before)), abs(float(volume_after)), 1e-12) * 1e-9
        if volume_delta >= -volume_tolerance:
            return self._failure(
                "The side-hole feature did not remove measurable solid material.",
                request=request,
                volume_before_m3=volume_before,
                volume_after_m3=volume_after,
                volume_delta_m3=volume_delta,
            )
        body_count_before = int(material_before["body_count_before"])
        body_count_after = len(body_after["bodies"])
        if body_count_after != body_count_before:
            return self._failure(
                "The side-hole feature changed the solid-body count.",
                request=request,
                body_count_before=body_count_before,
                body_count_after=body_count_after,
                volume_delta_m3=volume_delta,
            )
        return {
            "success": True,
            "name": name,
            "type": "side_hole",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "count": len(centers),
            "diameter_mm": request["diameter_mm"],
            "end_condition": end_condition,
            "depth_mm": request["depth_mm"],
            "body_count_before": body_count_before,
            "body_count_after": body_count_after,
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_delta_m3": volume_delta,
            "centers_model_mm": [
                [round(value * 1000.0, 6) for value in self._model_point(axis, face_offset, item)]
                for item in centers
            ],
        }

    @staticmethod
    def _extrude_cut_end_condition(
        model: Any,
        sketch_name: str,
        *,
        end_condition_code: int,
        depth: float,
    ) -> Any | None:
        """Create a one-direction face-sketch cut with an explicit SW end condition."""
        if not ActiveModelFeatureSkill._select_sketch(model, sketch_name):
            return None
        return model.FeatureManager.FeatureCut4(
            True,
            False,
            False,
            int(end_condition_code),
            0,
            float(depth),
            0.0,
            False,
            False,
            False,
            False,
            0.0,
            0.0,
            False,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            True,
            False,
            0,
            0.0,
            False,
            False,
        )

    def _create_rib(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        if request.get("profile") == "native_line":
            return self._create_native_line_rib(model, feature, request, body_info)
        material_before = self._material_state(body_info)
        if not material_before.get("success"):
            return self._failure(str(material_before["message"]), request=request)
        axis = request["axis"]
        face_offset = self._m(request["face_offset_mm"])
        width = self._m(request["width_mm"])
        height = self._m(request["height_mm"])
        depth = self._m(request["depth_mm"])
        base_z = self._m(request["base_z_mm"])
        positions = [self._m(value) for value in request["center_positions_mm"]]
        center_z = base_z + height / 2.0
        first_point = self._model_point(axis, face_offset, [positions[0], center_z])
        face = self._find_planar_face(body_info["bodies"], axis, face_offset, first_point)
        if face is None:
            return self._failure("Could not find the requested rib support face.", request=request)

        def draw_ribs() -> None:
            for position in positions:
                points = [
                    self._model_point(axis, face_offset, [position - width / 2.0, base_z]),
                    self._model_point(axis, face_offset, [position + width / 2.0, base_z]),
                    self._model_point(axis, face_offset, [position + width / 2.0, base_z + height]),
                    self._model_point(axis, face_offset, [position - width / 2.0, base_z + height]),
                ]
                for start, end in zip(points, points[1:] + points[:1]):
                    sketch_start = self._to_sketch_point(model, start)
                    sketch_end = self._to_sketch_point(model, end)
                    model.SketchManager.CreateLine(*sketch_start, *sketch_end)

        sketch_name = ActiveModelFeatureSkill._create_face_sketch(model, face, draw_ribs)
        created = ActiveModelFeatureSkill._extrude_boss(model, sketch_name, depth, direction=False)
        if created is None:
            return self._failure("SolidWorks failed to create the side ribs.", request=request)
        name = str(feature.get("name") or "SideRibs")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        geometry_validation = self._verify_added_material(material_before, model)
        if not geometry_validation.get("success"):
            return self._failure(
                str(geometry_validation["message"]),
                request=request,
                geometry_validation=geometry_validation,
            )
        return {
            "success": True,
            "name": name,
            "type": "rib",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "count": len(positions),
            "center_positions_mm": request["center_positions_mm"],
            "geometry_validation": geometry_validation,
        }

    def _create_native_line_rib(
        self,
        model: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        material_before = self._material_state(body_info)
        if not material_before.get("success"):
            return self._failure(str(material_before["message"]), request=request)
        plane_name = request["sketch_plane_feature"]
        model.ClearSelection2(True)
        plane = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", plane_name)
        selected = False
        if plane is not None:
            try:
                selected = bool(plane.Select2(False, 0))
            except Exception:
                selected = False
        if not selected:
            try:
                selected = bool(
                    model.Extension.SelectByID2(plane_name, "PLANE", 0, 0, 0, False, 0, None, 0)
                )
            except Exception:
                selected = False
        if not selected:
            return self._failure(f"Could not select rib reference plane: {plane_name}", request=request)

        model.SketchManager.InsertSketch(True)
        active_sketch = ActiveModelThroughHoleExecutor._com_member(model.SketchManager, "ActiveSketch")
        if active_sketch is None:
            return self._failure("SolidWorks did not enter a rib sketch.", request=request)
        sketch_name = str(ActiveModelThroughHoleExecutor._com_member(active_sketch, "Name", default="") or "")
        start, end = request["line_points_mm"]
        segment = model.SketchManager.CreateLine(
            self._m(start[0]),
            self._m(start[1]),
            0.0,
            self._m(end[0]),
            self._m(end[1]),
            0.0,
        )
        model.SketchManager.InsertSketch(True)
        if segment is None or not sketch_name:
            return self._failure("SolidWorks failed to create the native rib line sketch.", request=request)
        if not ActiveModelFeatureSkill._select_sketch(model, sketch_name):
            return self._failure("Could not select the native rib sketch.", request=request)
        target_bodies = list(body_info.get("bodies") or [])
        if len(target_bodies) != 1:
            return self._failure(
                "Native rib creation requires exactly one target solid body.",
                request=request,
                target_body_count=len(target_bodies),
            )
        if not self._select_body_with_mark(model, target_bodies[0], 1):
            return self._failure(
                "Could not select the native rib target body with mark 1.",
                request=request,
            )

        rib_features_before = {
            ActiveModelFeatureSkill._feature_name(item)
            for item in self._features_of_type(model, "Rib")
        }
        model.FeatureManager.InsertRib(
            bool(request["is_two_sided"]),
            bool(request["reverse_thickness_direction"]),
            self._m(request["thickness_mm"]),
            int(request["reference_edge_index"]),
            bool(request["reverse_material_direction"]),
            bool(request["is_drafted"]),
            bool(request["draft_outward"]),
            math.radians(float(request["draft_angle_deg"])),
            bool(request["is_normal_to_sketch"]),
            bool(request["is_drafted_from_wall"]),
        )
        model.ForceRebuild3(False)
        created_candidates = [
            item
            for item in self._features_of_type(model, "Rib")
            if ActiveModelFeatureSkill._feature_name(item) not in rib_features_before
        ]
        if not created_candidates:
            return self._failure(
                "SolidWorks InsertRib did not create a new native Rib feature.",
                request=request,
            )
        created = created_candidates[-1]
        name = str(feature.get("name") or "Rib")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ClearSelection2(True)
        model.ForceRebuild3(False)
        type_name = str(ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or "")
        if type_name.casefold() != "rib":
            return self._failure(
                f"Unexpected native rib feature type: {type_name}",
                request=request,
                feature_type_name=type_name,
            )
        geometry_validation = self._verify_added_material(material_before, model)
        if not geometry_validation.get("success"):
            return self._failure(
                str(geometry_validation["message"]),
                request=request,
                feature_type_name=type_name,
                geometry_validation=geometry_validation,
            )
        return {
            "success": True,
            "name": name,
            "type": "rib",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "feature_type_name": type_name,
            "sketch_name": sketch_name,
            "sketch_plane_feature": plane_name,
            "line_points_mm": request["line_points_mm"],
            "thickness_mm": request["thickness_mm"],
            "geometry_validation": geometry_validation,
        }

    @staticmethod
    def _select_body_with_mark(model: Any, body: Any, mark: int) -> bool:
        member = ActiveModelThroughHoleExecutor._com_member
        selection_manager = member(model, "SelectionManager", default=None)
        select_data = member(selection_manager, "CreateSelectData", default=None)
        if select_data is None:
            return False
        try:
            select_data.Mark = int(mark)
            return bool(member(body, "Select2", True, select_data, default=False))
        except Exception:
            return False

    @staticmethod
    def _features_of_type(model: Any, feature_type: str) -> list[Any]:
        member = ActiveModelThroughHoleExecutor._com_member
        expected = str(feature_type).casefold()
        result: list[Any] = []
        feature = member(model, "FirstFeature")
        seen: set[tuple[str, str]] = set()
        while feature is not None and len(seen) < 500:
            name = ActiveModelFeatureSkill._feature_name(feature)
            current_type = str(member(feature, "GetTypeName2", default="") or "")
            identity = (name, current_type)
            if identity in seen:
                break
            seen.add(identity)
            if current_type.casefold() == expected:
                result.append(feature)
            feature = member(feature, "GetNextFeature")
        return result

    @staticmethod
    def _material_state(body_info: dict[str, Any]) -> dict[str, Any]:
        bodies = list(body_info.get("bodies") or [])
        volume = ActiveModelThroughHoleExecutor._solid_volume(bodies)
        if volume is None:
            return {"success": False, "message": "Could not measure solid volume before rib creation."}
        return {
            "success": True,
            "body_count_before": len(bodies),
            "volume_before_m3": float(volume),
        }

    @staticmethod
    def _verify_added_material(material_before: dict[str, Any], model: Any) -> dict[str, Any]:
        volume_before = material_before.get("volume_before_m3")
        body_count_before = int(material_before.get("body_count_before", 0))
        body_after = ActiveModelThroughHoleExecutor._body_info(model)
        bodies_after = list(body_after.get("bodies") or []) if body_after.get("success") else []
        volume_after = ActiveModelThroughHoleExecutor._solid_volume(bodies_after)
        if volume_before is None or volume_after is None:
            return {"success": False, "message": "Could not measure solid volume across rib creation."}
        volume_delta = float(volume_after) - float(volume_before)
        tolerance = max(abs(float(volume_before)), abs(float(volume_after)), 1e-12) * 1e-9
        if volume_delta <= tolerance:
            return {
                "success": False,
                "message": "The rib feature did not add measurable solid material.",
                "volume_before_m3": volume_before,
                "volume_after_m3": volume_after,
                "volume_delta_m3": volume_delta,
            }
        if len(bodies_after) != body_count_before:
            return {
                "success": False,
                "message": "The rib feature created a separate solid instead of merging.",
                "body_count_before": body_count_before,
                "body_count_after": len(bodies_after),
                "volume_delta_m3": volume_delta,
            }
        return {
            "success": True,
            "body_count_before": body_count_before,
            "body_count_after": len(bodies_after),
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_delta_m3": volume_delta,
        }

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
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

    @staticmethod
    def _model_point(axis: str, face_offset: float, uv: list[float]) -> tuple[float, float, float]:
        if axis == "y":
            return float(uv[0]), float(face_offset), float(uv[1])
        if axis == "z":
            return float(uv[0]), float(uv[1]), float(face_offset)
        return float(face_offset), float(uv[0]), float(uv[1])

    @staticmethod
    def _to_sketch_point(
        model: Any,
        point: tuple[float, float, float],
        sketch: Any | None = None,
    ) -> tuple[float, float, float]:
        """Transform a model-space point into the active 2D sketch space."""
        import pythoncom
        import win32com.client
        from win32com.client import VARIANT

        active_sketch = sketch if sketch is not None else model.SketchManager.ActiveSketch
        if active_sketch is None:
            raise RuntimeError("No active sketch is available for coordinate transformation.")
        app = win32com.client.GetActiveObject("SldWorks.Application")
        revision = ActiveModelThroughHoleExecutor._com_member(app, "RevisionNumber", default="33.0")
        major = int(str(revision).split(".", 1)[0])
        module = win32com.client.gencache.EnsureModule(
            "{83A33D31-27C5-11CE-BFD4-00400513BB57}",
            0,
            major,
            0,
        )
        math_utility_raw = ActiveModelThroughHoleExecutor._com_member(app, "GetMathUtility")
        transform_raw = ActiveModelThroughHoleExecutor._com_member(active_sketch, "ModelToSketchTransform")
        if math_utility_raw is None or transform_raw is None:
            raise RuntimeError("SolidWorks did not expose the sketch coordinate transform.")
        math_utility = module.IMathUtility(math_utility_raw._oleobj_)
        transform = module.IMathTransform(transform_raw._oleobj_)
        point_variant = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, list(point))
        math_point_raw = math_utility.CreatePoint(point_variant)
        math_point = module.IMathPoint(math_point_raw._oleobj_)
        transformed_raw = math_point.MultiplyTransform(transform)
        transformed = module.IMathPoint(transformed_raw._oleobj_)
        values = transformed.ArrayData
        return float(values[0]), float(values[1]), 0.0

    @staticmethod
    def _find_planar_face(
        bodies: list[Any],
        axis: str,
        target: float,
        point: tuple[float, float, float] | None = None,
    ) -> Any | None:
        axis_index = {"x": 0, "y": 1, "z": 2}[axis]
        other_indices = {
            "x": (1, 2),
            "y": (0, 2),
            "z": (0, 1),
        }[axis]
        tolerance = 2e-5
        containment_tolerance = 2e-4
        best = None
        best_area = -1.0
        for body in bodies:
            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces") or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            for face in faces:
                box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox")
                if not box or len(box) < 6:
                    continue
                box = [float(value) for value in box]
                if abs(box[axis_index + 3] - box[axis_index]) > tolerance:
                    continue
                if abs(box[axis_index] - target) > tolerance:
                    continue
                surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface")
                if surface is not None and ActiveModelThroughHoleExecutor._com_member(surface, "IsPlane", default=True) is False:
                    continue
                normal = AdvancedFeatureSkill._face_normal(face)
                if normal is None:
                    continue
                requested_sign = 1.0 if target >= 0.0 else -1.0
                if AdvancedFeatureSkill._dot(
                    normal,
                    AdvancedFeatureSkill._axis_vector(axis, requested_sign),
                ) < 0.999:
                    continue
                if point is not None and any(
                    point[index] < box[index] - containment_tolerance or point[index] > box[index + 3] + containment_tolerance
                    for index in other_indices
                ):
                    continue
                if point is not None and not AdvancedFeatureSkill._point_on_face(
                    face,
                    point,
                    containment_tolerance,
                ):
                    continue
                area_value = ActiveModelThroughHoleExecutor._com_member(face, "GetArea")
                area = (
                    float(area_value)
                    if area_value is not None
                    else abs(
                        (box[other_indices[0] + 3] - box[other_indices[0]])
                        * (box[other_indices[1] + 3] - box[other_indices[1]])
                    )
                )
                if area > best_area:
                    best = face
                    best_area = area
        return best

    @staticmethod
    def _face_normal(face: Any) -> list[float] | None:
        values = ActiveModelThroughHoleExecutor._com_member(face, "Normal")
        if not isinstance(values, (tuple, list)) or len(values) < 3:
            return None
        normal = [float(values[index]) for index in range(3)]
        magnitude = math.sqrt(sum(value * value for value in normal))
        if magnitude <= 1e-12:
            return None
        return [value / magnitude for value in normal]

    @staticmethod
    def _point_on_face(
        face: Any,
        point: tuple[float, float, float],
        tolerance: float,
    ) -> bool:
        closest = ActiveModelThroughHoleExecutor._com_member(
            face,
            "GetClosestPointOn",
            *point,
        )
        if not isinstance(closest, (tuple, list)) or len(closest) < 3:
            return False
        return math.dist(point, tuple(float(value) for value in closest[:3])) <= tolerance

    @staticmethod
    def _axis_vector(axis: str, sign: float) -> list[float]:
        result = [0.0, 0.0, 0.0]
        result[{"x": 0, "y": 1, "z": 2}[axis]] = 1.0 if sign >= 0.0 else -1.0
        return result

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right))

    @staticmethod
    def _feature_axis_extent(feature: Any, axis: str) -> list[float] | None:
        axis_index = {"x": 0, "y": 1, "z": 2}[axis]
        faces = ActiveModelThroughHoleExecutor._com_member(feature, "GetFaces") or ()
        if not isinstance(faces, (tuple, list)):
            faces = (faces,)
        values: list[float] = []
        for face in faces:
            box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox")
            if box and len(box) >= 6:
                values.extend((float(box[axis_index]), float(box[axis_index + 3])))
        return [min(values), max(values)] if values else None

    @staticmethod
    def _failure(message: str, **data: Any) -> dict[str, Any]:
        return {"success": False, "message": message, **data}

    @staticmethod
    def _result(success: bool, message: str, report_path: Path, data: dict[str, Any]) -> SkillResult:
        payload = {"success": success, "message": message, **data}
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["files"] = [str(report_path)]
        return SkillResult(
            success=success,
            message=message,
            data=payload,
            path=str(report_path),
            output=json.dumps(payload, ensure_ascii=False, indent=2),
        )
