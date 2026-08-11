from __future__ import annotations

import json
import math
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .geometry_resolver.solidworks_adapter import SolidWorksFaceInspector
from .models import SkillResult
from .production_fillet_skill import ProductionFilletSkill


SUPPORTED_FEATURES = {"boss", "pocket", "slot", "chamfer", "dome", "external_thread"}


class ActiveModelFeatureSkill:
    """Apply common prismatic production features to the active Part.

    The entry points in this module never create a new document, invoke a
    demonstration template, or export a file. The final ``save_sldprt`` step
    owns persistence for the production pipeline.
    """

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any], feature_type: str) -> SkillResult:
        if feature_type not in SUPPORTED_FEATURES:
            return SkillResult(False, f"Unsupported active-model feature type: {feature_type}")
        features = [item for item in plan.get("features", []) if item.get("type") == feature_type]
        if not features:
            return SkillResult(False, f"No {feature_type} feature found in the production plan.")

        run_dir = self.output_root / f"active_{feature_type}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / f"active_model_{feature_type}_report.json"
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
                body_info = ActiveModelThroughHoleExecutor._body_info(model)
                if not body_info.get("success"):
                    return self._result(False, str(body_info.get("message")), report_path, {"active_doc": title, "operations": operations})
                operation = self._execute_feature(model, plan, feature, body_info)
                operations.append(operation)
                if not operation.get("success"):
                    return self._result(
                        False,
                        str(operation.get("message") or f"{feature_type} failed"),
                        report_path,
                        {"active_doc": title, "mode": "active_model", "feature_type": feature_type, "operations": operations},
                    )

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            model.ViewZoomtofit2()
            feature_tree = self._feature_tree(model)
            data = {
                "active_doc": title,
                "mode": "active_model",
                "feature_type": feature_type,
                "feature_created": True,
                "features_created": len(operations),
                "operations": operations,
                "feature_tree": feature_tree,
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
                f"active_model {feature_type} failed: {exc}",
                report_path,
                {"feature_type": feature_type, "error": repr(exc), "traceback": traceback.format_exc()},
            )

    def _execute_feature(
        self,
        model: Any,
        plan: dict[str, Any],
        feature: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        feature_type = str(feature.get("type"))
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "boss": self._create_boss,
            "pocket": self._create_pocket,
            "slot": self._create_slots,
            "chamfer": self._create_chamfer,
            "dome": self._create_dome,
            "external_thread": self._create_external_thread,
        }
        return handlers[feature_type](model, plan, feature, body_info)

    @staticmethod
    def normalize_dome_request(params: dict[str, Any]) -> dict[str, Any]:
        mode = str(params.get("mode") or params.get("execution_mode") or "active_model")
        mode = mode.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            height_mm = float(params.get("height_mm", params.get("height", 0)) or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "Dome height_mm must be numeric."}
        if not math.isfinite(height_mm) or height_mm <= 0:
            return {"success": False, "message": "Dome height_mm must be greater than zero."}
        if mode != "active_model":
            return {"success": False, "message": "Dome currently requires mode='active_model'."}

        selector = params.get("face_selector")
        if not isinstance(selector, dict):
            return {"success": False, "message": "Dome requires an explicit face_selector object."}
        selector_type = str(selector.get("type") or "").strip().lower()
        if selector_type != "planar_face_signature":
            return {
                "success": False,
                "message": "Dome face_selector.type must be 'planar_face_signature'.",
            }
        axis = str(selector.get("axis") or "").strip().lower()
        if axis not in {"x", "y", "z"}:
            return {"success": False, "message": "Dome face_selector.axis must be x, y, or z."}
        try:
            coordinate_mm = float(selector.get("coordinate_mm"))
            area_mm2 = float(selector.get("area_mm2"))
            normal_sign = int(selector.get("normal_sign"))
            coordinate_tolerance_mm = float(selector.get("coordinate_tolerance_mm", 0.05))
            area_tolerance_mm2 = float(
                selector.get("area_tolerance_mm2", max(0.1, abs(area_mm2) * 1e-4))
            )
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": (
                    "Dome face signature requires numeric coordinate_mm, area_mm2, "
                    "normal_sign, coordinate_tolerance_mm, and area_tolerance_mm2."
                ),
            }
        if not all(
            math.isfinite(value)
            for value in (
                coordinate_mm,
                area_mm2,
                coordinate_tolerance_mm,
                area_tolerance_mm2,
            )
        ):
            return {"success": False, "message": "Dome face signature values must be finite."}
        if area_mm2 <= 0 or coordinate_tolerance_mm <= 0 or area_tolerance_mm2 <= 0:
            return {
                "success": False,
                "message": "Dome face area and selector tolerances must be greater than zero.",
            }
        if normal_sign not in {-1, 1}:
            return {"success": False, "message": "Dome face_selector.normal_sign must be -1 or 1."}
        body_index = selector.get("body_index")
        if body_index is not None:
            try:
                body_index = int(body_index)
            except (TypeError, ValueError):
                return {"success": False, "message": "Dome face_selector.body_index must be an integer."}
            if body_index < 0:
                return {"success": False, "message": "Dome face_selector.body_index cannot be negative."}
        try:
            reverse_direction = ActiveModelFeatureSkill._bool_value(
                params.get("reverse_direction"),
                False,
                "Dome reverse_direction",
            )
            elliptical = ActiveModelFeatureSkill._bool_value(
                params.get("elliptical"),
                False,
                "Dome elliptical",
            )
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "mode": mode,
            "height_mm": height_mm,
            "reverse_direction": reverse_direction,
            "elliptical": elliptical,
            "face_selector": {
                "type": selector_type,
                "axis": axis,
                "coordinate_mm": coordinate_mm,
                "area_mm2": area_mm2,
                "normal_sign": normal_sign,
                "coordinate_tolerance_mm": coordinate_tolerance_mm,
                "area_tolerance_mm2": area_tolerance_mm2,
                "body_index": body_index,
            },
        }

    @staticmethod
    def _bool_value(value: Any, default: bool, label: str) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        token = str(value).strip().lower()
        if token in {"true", "yes", "1", "on"}:
            return True
        if token in {"false", "no", "0", "off"}:
            return False
        raise ValueError(f"{label} must be a boolean value.")

    @staticmethod
    def normalize_external_thread_request(params: dict[str, Any]) -> dict[str, Any]:
        designation = str(params.get("designation") or params.get("thread") or "").strip().upper()
        designation = designation.replace("\u00d7", "X").replace(" ", "")
        representation = str(params.get("representation") or "").strip().lower().replace("-", "_").replace(" ", "_")
        representation = {
            "real": "modeled",
            "physical": "modeled",
            "solid": "modeled",
            "modelled": "modeled",
            "modeled_geometry": "modeled",
            "3d": "modeled",
        }.get(representation, representation)
        end = str(params.get("end") or "").strip().lower()
        end = {"left": "min", "start": "min", "right": "max", "finish": "max"}.get(end, end)
        axis = str(params.get("axis") or "").strip().lower()
        axis = {"horizontal": "x", "local_horizontal": "x", "vertical": "y", "local_vertical": "y"}.get(axis, axis)
        standard = str(params.get("standard") or "").strip().lower().replace("-", "_").replace(" ", "_")
        standard = {"iso_metric": "iso", "metric_iso": "iso"}.get(standard, standard)
        standard_type = str(params.get("standard_type") or "").strip().lower().replace("-", "_").replace(" ", "_")
        standard_type = {"metricdie": "metric_die"}.get(standard_type, standard_type)
        edge_selector = str(params.get("edge_selector") or "").strip().lower().replace("-", "_").replace(" ", "_")
        thread_method = str(
            params.get("thread_method")
            or ("cut" if representation == "modeled" else "cosmetic")
        ).strip().lower().replace("-", "_").replace(" ", "_")
        thread_method = {
            "swept_cut": "cut",
            "cut_thread": "cut",
            "annotation": "cosmetic",
        }.get(thread_method, thread_method)
        try:
            major_diameter_mm = float(params.get("major_diameter", params.get("major_diameter_mm", 0)) or 0)
            pitch_mm = float(params.get("pitch", params.get("pitch_mm", 0)) or 0)
            thread_length_mm = float(params.get("thread_length", params.get("thread_length_mm", 0)) or 0)
            thread_start_angle_deg = float(params.get("thread_start_angle_deg", 0.0) or 0.0)
        except (TypeError, ValueError):
            return {"success": False, "message": "External-thread dimensions must be numeric."}
        try:
            right_handed = ActiveModelFeatureSkill._thread_bool(params.get("right_handed"), True)
            trim_start_face = ActiveModelFeatureSkill._thread_bool(params.get("trim_start_face"), True)
            trim_end_face = ActiveModelFeatureSkill._thread_bool(params.get("trim_end_face"), True)
            reverse_direction = (
                None
                if params.get("reverse_direction") is None
                else ActiveModelFeatureSkill._thread_bool(params.get("reverse_direction"), False)
            )
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        raw_axis_center = params.get("axis_center_mm", params.get("axis_center"))
        axis_center_mm: list[float] | None = None
        if raw_axis_center not in (None, ""):
            if not isinstance(raw_axis_center, (tuple, list)) or len(raw_axis_center) != 2:
                return {
                    "success": False,
                    "message": "External-thread axis_center_mm must contain the two radial coordinates.",
                }
            try:
                axis_center_mm = [float(raw_axis_center[0]), float(raw_axis_center[1])]
            except (TypeError, ValueError):
                return {"success": False, "message": "External-thread axis_center_mm values must be numeric."}
            if not all(math.isfinite(value) for value in axis_center_mm):
                return {"success": False, "message": "External-thread axis_center_mm values must be finite."}

        match = re.fullmatch(r"M(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)", designation)
        if match is None:
            return {"success": False, "message": "External thread requires an explicit designation such as M16X2.0."}
        if min(major_diameter_mm, pitch_mm, thread_length_mm) <= 0:
            return {"success": False, "message": "External-thread diameter, pitch, and length must be greater than zero."}
        if not math.isfinite(thread_start_angle_deg) or not 0.0 <= thread_start_angle_deg < 360.0:
            return {"success": False, "message": "External-thread thread_start_angle_deg must be in [0, 360)."}
        if not math.isclose(float(match.group(1)), major_diameter_mm, rel_tol=0.0, abs_tol=1e-6):
            return {"success": False, "message": "External-thread designation conflicts with major diameter."}
        if not math.isclose(float(match.group(2)), pitch_mm, rel_tol=0.0, abs_tol=1e-6):
            return {"success": False, "message": "External-thread designation conflicts with pitch."}
        constraints = {
            "representation": (representation, {"cosmetic", "modeled"}),
            "end": (end, {"min", "max"}),
            "axis": (axis, {"x", "y", "z"}),
            "standard": (standard, {"iso"}),
            "standard_type": (standard_type, {"metric_die"}),
            "edge_selector": (edge_selector, {"axial_major_diameter_end_edge"}),
        }
        for field, (value, allowed) in constraints.items():
            if value not in allowed:
                return {"success": False, "message": f"Unsupported external-thread {field}: {value or '<missing>'}."}
        expected_method = "cut" if representation == "modeled" else "cosmetic"
        if thread_method != expected_method:
            return {
                "success": False,
                "message": (
                    f"External-thread representation={representation!r} requires "
                    f"thread_method={expected_method!r}."
                ),
            }
        return {
            "success": True,
            "designation": designation,
            "representation": representation,
            "end": end,
            "axis": axis,
            "standard": standard,
            "standard_type": standard_type,
            "edge_selector": edge_selector,
            "major_diameter_mm": major_diameter_mm,
            "pitch_mm": pitch_mm,
            "thread_length_mm": thread_length_mm,
            "thread_callout": str(params.get("thread_callout") or designation),
            "axis_center_mm": axis_center_mm,
            "thread_method": thread_method,
            "right_handed": right_handed,
            "trim_start_face": trim_start_face,
            "trim_end_face": trim_end_face,
            "thread_start_angle_deg": thread_start_angle_deg,
            "reverse_direction": reverse_direction,
        }

    @staticmethod
    def _thread_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        token = str(value).strip().lower()
        if token in {"true", "yes", "1", "on"}:
            return True
        if token in {"false", "no", "0", "off"}:
            return False
        raise ValueError(f"External-thread boolean value is invalid: {value!r}.")

    def _create_boss(self, model: Any, plan: dict[str, Any], feature: dict[str, Any], body_info: dict[str, Any]) -> dict[str, Any]:
        params = feature.get("params", {})
        length = float(params.get("length", 0) or 0)
        width = float(params.get("width", 0) or 0)
        height = float(params.get("height", 0) or 0)
        bbox = body_info["bbox"]
        if min(length, width, height) <= 0:
            return self._failure("Boss length, width, and height must be greater than zero.")
        if length >= bbox["length"] * 1000.0 or width >= bbox["width"] * 1000.0:
            return self._failure("Boss footprint must fit inside the active body footprint.")

        cx, cy = self._feature_center(params, bbox)
        face = self._find_planar_face_at_z(body_info["bodies"], bbox["zmax"])
        if face is None:
            return self._failure("Could not find the top planar face for the boss.")
        sketch_name = self._create_face_sketch(
            model,
            face,
            lambda: model.SketchManager.CreateCenterRectangle(
                cx,
                cy,
                bbox["zmax"],
                cx + self._m(length / 2.0),
                cy + self._m(width / 2.0),
                bbox["zmax"],
            ),
        )
        created = self._extrude_boss(model, sketch_name, self._m(height), direction=False)
        if created is None:
            created = self._extrude_boss(model, sketch_name, self._m(height), direction=True)
        if created is None:
            return self._failure("SolidWorks FeatureExtrusion3 returned no boss feature.")
        self._name_feature(created, feature.get("name") or "CenterBoss")
        model.ForceRebuild3(False)
        after = ActiveModelThroughHoleExecutor._body_info(model)
        expected_zmax = bbox["zmax"] + self._m(height)
        actual_zmax = after.get("bbox", {}).get("zmax", bbox["zmax"])
        if actual_zmax < expected_zmax - self._m(0.25):
            return self._failure(
                "Boss feature did not increase the model height as requested.",
                expected_zmax_m=expected_zmax,
                actual_zmax_m=actual_zmax,
            )
        return {
            "success": True,
            "name": feature.get("name") or "CenterBoss",
            "type": "boss",
            "dimensions_mm": {"length": length, "width": width, "height": height},
            "center_mm": [round(cx * 1000.0, 6), round(cy * 1000.0, 6)],
            "bbox_before_m": bbox,
            "bbox_after_m": after.get("bbox", {}),
            "feature_name": self._feature_name(created),
        }

    def _create_pocket(self, model: Any, plan: dict[str, Any], feature: dict[str, Any], body_info: dict[str, Any]) -> dict[str, Any]:
        params = feature.get("params", {})
        length = float(params.get("length", 0) or 0)
        width = float(params.get("width", 0) or 0)
        depth = float(params.get("depth", 0) or 0)
        radius = float(params.get("corner_radius", 0) or 0)
        bbox = body_info["bbox"]
        if min(length, width, depth) <= 0:
            return self._failure("Pocket length, width, and depth must be greater than zero.")
        if radius < 0 or radius >= min(length, width) / 2.0:
            return self._failure("Pocket corner radius must be smaller than half the pocket width/length.")
        boss = next((item for item in plan.get("features", []) if item.get("type") == "boss"), None)
        boss_params = boss.get("params", {}) if boss else {}
        if boss and (length >= float(boss_params.get("length", 0) or 0) or width >= float(boss_params.get("width", 0) or 0)):
            return self._failure("Pocket footprint must fit inside the requested boss.")

        cx, cy = self._feature_center(params, bbox)
        top_z = bbox["zmax"]
        face = self._find_planar_face_at_z(body_info["bodies"], top_z)
        if face is None:
            return self._failure("Could not find the top planar face for the pocket.")
        draw_profile = (
            lambda: self._draw_rounded_rectangle(model, cx, cy, length, width, radius)
            if radius > 0
            else model.SketchManager.CreateCenterRectangle(
                cx,
                cy,
                top_z,
                cx + self._m(length / 2.0),
                cy + self._m(width / 2.0),
                top_z,
            )
        )
        sketch_name = self._create_face_sketch(
            model,
            face,
            draw_profile,
        )
        cut = self._extrude_cut(model, sketch_name, self._m(depth), through_all=False, direction=True)
        if cut is None:
            cut = self._extrude_cut(model, sketch_name, self._m(depth), through_all=False, direction=False)
        if cut is None:
            return self._failure("SolidWorks FeatureCut4 returned no pocket feature.")
        self._name_feature(cut, feature.get("name") or "TopPocket")

        # Rounded corners are part of the cut sketch. This remains stable when
        # side bores or other features split the resulting pocket edges.
        selected = 4 if radius > 0 else 0

        return {
            "success": True,
            "name": feature.get("name") or "TopPocket",
            "type": "pocket",
            "dimensions_mm": {"length": length, "width": width, "depth": depth, "corner_radius": radius},
            "center_mm": [round(cx * 1000.0, 6), round(cy * 1000.0, 6)],
            "cut_feature_name": self._feature_name(cut),
            "corner_feature_name": "rounded_sketch_profile" if radius > 0 else None,
            "corner_edges_selected": selected,
        }

    @classmethod
    def _draw_rounded_rectangle(
        cls,
        model: Any,
        cx: float,
        cy: float,
        length_mm: float,
        width_mm: float,
        radius_mm: float,
    ) -> None:
        radius = cls._m(radius_mm)
        raw = model.SketchManager.CreateCenterRectangle(
            cx,
            cy,
            0.0,
            cx + cls._m(length_mm / 2.0),
            cy + cls._m(width_mm / 2.0),
            0.0,
        )
        segments = list(raw) if isinstance(raw, (tuple, list)) else []
        lines = [
            segment
            for segment in segments
            if not bool(ActiveModelThroughHoleExecutor._com_member(segment, "ConstructionGeometry", default=False))
        ]
        if len(lines) != 4:
            raise RuntimeError(f"Expected four rectangle edges before sketch fillets; found {len(lines)}.")

        def midpoint(segment: Any) -> tuple[float, float]:
            start = ActiveModelThroughHoleExecutor._com_member(segment, "GetStartPoint2")
            end = ActiveModelThroughHoleExecutor._com_member(segment, "GetEndPoint2")
            if start is None or end is None:
                raise RuntimeError("Could not read a rectangle sketch segment endpoint.")
            return (
                (float(ActiveModelThroughHoleExecutor._com_member(start, "X", default=0.0)) + float(ActiveModelThroughHoleExecutor._com_member(end, "X", default=0.0))) / 2.0,
                (float(ActiveModelThroughHoleExecutor._com_member(start, "Y", default=0.0)) + float(ActiveModelThroughHoleExecutor._com_member(end, "Y", default=0.0))) / 2.0,
            )

        top = max(lines, key=lambda segment: midpoint(segment)[1])
        bottom = min(lines, key=lambda segment: midpoint(segment)[1])
        right = max(lines, key=lambda segment: midpoint(segment)[0])
        left = min(lines, key=lambda segment: midpoint(segment)[0])
        for first, second in ((top, right), (right, bottom), (bottom, left), (left, top)):
            model.ClearSelection2(True)
            first_selected = bool(first.Select2(False, 0))
            second_selected = bool(second.Select2(True, 0))
            if not first_selected or not second_selected:
                raise RuntimeError("Could not select rectangle edges for a sketch corner fillet.")
            if model.SketchManager.CreateFillet(radius, 2) is None:
                raise RuntimeError(f"SolidWorks failed to create sketch fillet R{radius_mm}.")
        model.ClearSelection2(True)

    def _create_slots(self, model: Any, plan: dict[str, Any], feature: dict[str, Any], body_info: dict[str, Any]) -> dict[str, Any]:
        params = feature.get("params", {})
        count = int(params.get("count", 0) or 0)
        length = float(params.get("length", 0) or 0)
        width = float(params.get("width", 0) or 0)
        offsets = [float(value) for value in params.get("centerline_offsets", [])]
        orientation = str(params.get("orientation") or "y").lower()
        if count <= 0 or len(offsets) != count:
            return self._failure("Slot count must match centerline_offsets.")
        if width <= 0 or length <= width:
            return self._failure("Slot length must be greater than slot width and both must be positive.")
        if orientation not in {"x", "y"}:
            return self._failure("Slot orientation must be 'x' or 'y'.")

        bbox = body_info["bbox"]
        base_thickness = float(plan.get("parameters", {}).get("thickness", 0) or 0)
        base_top_z = bbox["zmin"] + self._m(base_thickness)
        face = self._find_planar_face_at_z(body_info["bodies"], base_top_z)
        if face is None:
            return self._failure("Could not find the base top planar face for side slots.", target_z_m=base_top_z)
        radius = self._m(width / 2.0)
        half_line = self._m((length - width) / 2.0)
        centers: list[tuple[float, float]] = [(self._m(value), 0.0) for value in offsets]

        def draw_slots() -> None:
            for cx, cy in centers:
                if orientation == "y":
                    x1, y1, x2, y2 = cx, cy - half_line, cx, cy + half_line
                else:
                    x1, y1, x2, y2 = cx - half_line, cy, cx + half_line, cy
                created = model.SketchManager.CreateSketchSlot(
                    0,
                    radius,
                    radius,
                    x1,
                    y1,
                    0.0,
                    x2,
                    y2,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    1,
                    False,
                )
                if created is None:
                    raise RuntimeError(f"CreateSketchSlot failed at {(cx, cy)}")

        sketch_name = self._create_face_sketch(model, face, draw_slots)
        cut = self._extrude_cut(model, sketch_name, max(bbox["thickness"] * 2.0, self._m(20)), through_all=True, direction=True)
        if cut is None:
            cut = self._extrude_cut(model, sketch_name, max(bbox["thickness"] * 2.0, self._m(20)), through_all=True, direction=False)
        if cut is None:
            return self._failure("SolidWorks FeatureCut4 returned no slot cut feature.")
        self._name_feature(cut, feature.get("name") or "SideSlots")
        return {
            "success": True,
            "name": feature.get("name") or "SideSlots",
            "type": "slot",
            "count": count,
            "dimensions_mm": {"length": length, "width": width},
            "orientation": orientation,
            "centers_mm": [[round(cx * 1000.0, 6), round(cy * 1000.0, 6)] for cx, cy in centers],
            "feature_name": self._feature_name(cut),
            "base_top_z_m": base_top_z,
        }

    def _create_chamfer(self, model: Any, plan: dict[str, Any], feature: dict[str, Any], body_info: dict[str, Any]) -> dict[str, Any]:
        params = feature.get("params", {})
        size = float(params.get("size", 0) or 0)
        targets = str(params.get("targets") or params.get("edge_selector") or "all_outer_edges").strip().lower()
        if size <= 0:
            return self._failure("Chamfer size must be greater than zero.")
        if targets == "explicit_edge_signatures":
            signatures = params.get("edge_signatures")
            try:
                tolerance_mm = float(params.get("edge_tolerance_mm", 0) or 0)
            except (TypeError, ValueError):
                tolerance_mm = 0.0
            if not isinstance(signatures, list) or not signatures or tolerance_mm <= 0:
                return self._failure(
                    "Explicit chamfer selection requires edge_signatures and edge_tolerance_mm > 0.",
                    targets=targets,
                )
            selected = ProductionFilletSkill._select_explicit_edge_signatures(
                model,
                body_info.get("bodies", []),
                signatures,
                tolerance_mm=tolerance_mm,
            )
            expected = len(signatures)
            if selected != expected:
                return self._failure(
                    f"Expected {expected} explicit chamfer edges; selected {selected}.",
                    targets=targets,
                    selected_edges=selected,
                    expected_edge_signatures=expected,
                )
            volume_before = ActiveModelThroughHoleExecutor._solid_volume(body_info.get("bodies", []))
            body_count_before = len(body_info.get("bodies", []))
            created = model.FeatureManager.InsertFeatureChamfer(
                4,
                1,
                self._m(size),
                math.pi / 4.0,
                0,
                0,
                0,
                0,
            )
            if created is None:
                return self._failure(
                    "SolidWorks failed to chamfer the explicitly resolved edges.",
                    targets=targets,
                    selected_edges=selected,
                    expected_edge_signatures=expected,
                )
            self._name_feature(created, feature.get("name") or "ExplicitEdgeChamfer")
            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            after = ActiveModelThroughHoleExecutor._body_info(model)
            after_bodies = list(after.get("bodies", [])) if after.get("success") else []
            volume_after = ActiveModelThroughHoleExecutor._solid_volume(after_bodies)
            body_count_after = len(after_bodies)
            volume_delta = (
                float(volume_after) - float(volume_before)
                if volume_before is not None and volume_after is not None
                else None
            )
            volume_tolerance = max(abs(float(volume_before or 0.0)), abs(float(volume_after or 0.0)), 1e-12) * 1e-9
            if (
                volume_delta is None
                or volume_delta >= -volume_tolerance
                or body_count_after != body_count_before
            ):
                return self._failure(
                    "Explicit-edge chamfer did not remove measurable volume while preserving body count.",
                    feature_created=True,
                    feature_name=self._feature_name(created),
                    targets=targets,
                    selected_edges=selected,
                    expected_edge_signatures=expected,
                    volume_before_m3=volume_before,
                    volume_after_m3=volume_after,
                    volume_delta_m3=volume_delta,
                    body_count_before=body_count_before,
                    body_count_after=body_count_after,
                )
            return {
                "success": True,
                "name": feature.get("name") or "ExplicitEdgeChamfer",
                "type": "chamfer",
                "size_mm": size,
                "targets": targets,
                "edge_selector": targets,
                "selected_edges": selected,
                "expected_edge_signatures": expected,
                "edge_tolerance_mm": tolerance_mm,
                "features_created": 1,
                "feature_name": self._feature_name(created),
                "volume_before_m3": float(volume_before),
                "volume_after_m3": float(volume_after),
                "volume_delta_m3": volume_delta,
                "body_count_before": body_count_before,
                "body_count_after": body_count_after,
            }
        single_end_selectors = {
            "axial_min_outer_end_edge": "min",
            "axial_max_outer_end_edge": "max",
        }
        if targets in single_end_selectors:
            axis = str(params.get("axis") or "x")
            diameter = float(params.get("diameter", params.get("diameter_mm", 0)) or 0)
            if diameter <= 0:
                return self._failure(
                    "Single-end axial chamfer requires diameter_mm greater than zero.",
                    targets=targets,
                    axis=axis,
                )
            edge_result = self._find_axial_major_diameter_end_edge(
                body_info.get("bodies", []),
                body_info["bbox"],
                axis=axis,
                end=single_end_selectors[targets],
                major_diameter_mm=diameter,
            )
            if not edge_result.get("success"):
                return self._failure(
                    str(edge_result.get("message") or "Could not resolve the single axial chamfer edge."),
                    targets=targets,
                    axis=axis,
                    diameter_mm=diameter,
                    edge_resolution=edge_result,
                )
            model.ClearSelection2(True)
            edge = edge_result["edge"]
            selected = bool(
                ActiveModelThroughHoleExecutor._com_member(
                    edge,
                    "Select2",
                    False,
                    0,
                    default=False,
                )
            )
            if not selected:
                return self._failure(
                    "SolidWorks did not select the resolved single axial chamfer edge.",
                    targets=targets,
                    axis=axis,
                    diameter_mm=diameter,
                    edge_signature=edge_result.get("signature"),
                )
            created = model.FeatureManager.InsertFeatureChamfer(
                4,
                1,
                self._m(size),
                math.pi / 4.0,
                0,
                0,
                0,
                0,
            )
            if created is None:
                return self._failure(
                    "SolidWorks failed to chamfer the resolved single axial edge.",
                    targets=targets,
                    axis=axis,
                    diameter_mm=diameter,
                    edge_signature=edge_result.get("signature"),
                )
            self._name_feature(created, feature.get("name") or "SingleAxialEndChamfer")
            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            return {
                "success": True,
                "name": feature.get("name") or "SingleAxialEndChamfer",
                "type": "chamfer",
                "size_mm": size,
                "diameter_mm": diameter,
                "targets": targets,
                "axis": axis,
                "selected_edges": 1,
                "edge_signature": edge_result.get("signature"),
                "features_created": 1,
                "feature_name": self._feature_name(created),
            }
        axial_selectors = {
            "bearing_end_edges",
            "axial_ring_end_edges",
            "axial_outer_ring_end_edges",
            "axial_bearing_boundary_end_edges",
            "axial_outer_end_edges",
        }
        if targets in axial_selectors:
            axis = str(params.get("axis") or "x")
            selected = ProductionFilletSkill._select_axial_bearing_boundary_end_edges(
                model,
                body_info.get("bodies", []),
                body_info["bbox"],
                axis=axis,
            )
            expected_edges = 2 if targets == "axial_outer_end_edges" else 4
            if selected != expected_edges:
                return self._failure(
                    f"Expected {expected_edges} axial boundary end edges; selected {selected}.",
                    selected_edges=selected,
                    expected_edges=expected_edges,
                    targets=targets,
                    axis=axis,
                )
            created = model.FeatureManager.InsertFeatureChamfer(4, 1, self._m(size), math.pi / 4.0, 0, 0, 0, 0)
            if created is None:
                return self._failure(
                    "SolidWorks failed to chamfer the axial boundary end edges.",
                    selected_edges=selected,
                    targets=targets,
                    axis=axis,
                )
            self._name_feature(created, feature.get("name") or "AxialBoundaryChamfers")
            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            return {
                "success": True,
                "name": feature.get("name") or "AxialBoundaryChamfers",
                "type": "chamfer",
                "size_mm": size,
                "targets": targets,
                "axis": axis,
                "selected_edges": selected,
                "features_created": 1,
                "feature_name": self._feature_name(created),
            }
        if targets not in {"all_outer_edges", "outer_edges"}:
            return self._failure(f"Unsupported production chamfer target: {targets}")

        bbox = body_info["bbox"]
        part_params = plan.get("parameters", {})
        cx = (bbox["xmin"] + bbox["xmax"]) / 2.0
        cy = (bbox["ymin"] + bbox["ymax"]) / 2.0
        base_half_x = self._m(float(part_params.get("length", 0) or 0) / 2.0) or bbox["length"] / 2.0
        base_half_y = self._m(float(part_params.get("width", 0) or 0) / 2.0) or bbox["width"] / 2.0
        base_top_z = bbox["zmin"] + self._m(float(part_params.get("thickness", 0) or 0))
        groups: list[dict[str, Any]] = [
            {"name": "BaseTopOuter", "z": base_top_z, "cx": cx, "cy": cy, "half_x": base_half_x, "half_y": base_half_y},
            {"name": "BaseBottomOuter", "z": bbox["zmin"], "cx": cx, "cy": cy, "half_x": base_half_x, "half_y": base_half_y},
        ]
        boss = next((item for item in plan.get("features", []) if item.get("type") == "boss"), None)
        if boss:
            boss_params = boss.get("params", {})
            boss_cx, boss_cy = self._feature_center(boss_params, bbox)
            groups.insert(
                0,
                {
                    "name": "BossTopOuter",
                    "z": bbox["zmax"],
                    "cx": boss_cx,
                    "cy": boss_cy,
                    "half_x": self._m(float(boss_params.get("length", 0) or 0) / 2.0),
                    "half_y": self._m(float(boss_params.get("width", 0) or 0) / 2.0),
                },
            )

        created_groups: list[dict[str, Any]] = []
        for group in groups:
            current = ActiveModelThroughHoleExecutor._body_info(model)
            selected = self._select_rect_perimeter_edges(model, current.get("bodies", []), group)
            if selected <= 0:
                return self._failure(f"No external edges selected for chamfer group {group['name']}.", groups=created_groups)
            created = model.FeatureManager.InsertFeatureChamfer(4, 1, self._m(size), math.pi / 4.0, 0, 0, 0, 0)
            if created is None:
                return self._failure(f"SolidWorks failed to chamfer group {group['name']}.", selected_edges=selected, groups=created_groups)
            self._name_feature(created, f"{feature.get('name') or 'OuterChamfers'}_{group['name']}")
            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            created_groups.append({"group": group["name"], "selected_edges": selected, "feature_name": self._feature_name(created)})

        return {
            "success": True,
            "name": feature.get("name") or "OuterChamfers",
            "type": "chamfer",
            "size_mm": size,
            "targets": targets,
            "groups": created_groups,
            "features_created": len(created_groups),
        }

    def _create_dome(
        self,
        model: Any,
        plan: dict[str, Any],
        feature: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        request = self.normalize_dome_request(feature.get("params", {}))
        if not request.get("success"):
            return self._failure(str(request.get("message") or "Invalid dome request."))

        face_result = self._resolve_dome_face(
            body_info.get("bodies", []),
            request["face_selector"],
        )
        if not face_result.get("success"):
            return self._failure(
                str(face_result.get("message") or "Could not resolve dome face."),
                request=request,
                face_candidates=face_result.get("candidates", []),
                observed_planar_faces=face_result.get("observed_planar_faces", []),
            )

        before_tree = self._feature_tree(model)
        before_identities = {
            (str(item.get("name") or ""), str(item.get("type") or ""))
            for item in before_tree
        }
        volume_before = ActiveModelThroughHoleExecutor._solid_volume(
            body_info.get("bodies", [])
        )
        model.ClearSelection2(True)
        if not self._select_face_with_mark(model, face_result["face"], 1):
            return self._failure(
                "SolidWorks did not select the resolved dome face with selection mark 1.",
                request=request,
                face_signature=face_result.get("signature"),
            )

        insert_result = ActiveModelThroughHoleExecutor._com_member(
            model,
            "InsertDome",
            self._m(request["height_mm"]),
            request["reverse_direction"],
            request["elliptical"],
            default=None,
        )
        model.ClearSelection2(True)
        model.ForceRebuild3(False)
        after_tree = self._feature_tree(model)
        created = [
            item
            for item in after_tree
            if (str(item.get("name") or ""), str(item.get("type") or "")) not in before_identities
            and "dome" in str(item.get("type") or "").casefold()
        ]
        if len(created) != 1:
            return self._failure(
                "SolidWorks did not create exactly one native Dome feature.",
                request=request,
                insert_result=insert_result,
                created_candidates=created,
                face_signature=face_result.get("signature"),
            )

        original_name = str(created[0]["name"])
        requested_name = str(feature.get("id") or feature.get("name") or original_name)
        created_feature = ActiveModelThroughHoleExecutor._com_member(
            model,
            "FeatureByName",
            original_name,
            default=None,
        )
        created_definition = ActiveModelThroughHoleExecutor._com_member(
            created_feature,
            "GetDefinition",
            default=None,
        )
        native_height_m = ActiveModelThroughHoleExecutor._com_member(
            created_definition,
            "Height",
            default=None,
        )
        native_reverse_direction = ActiveModelThroughHoleExecutor._com_member(
            created_definition,
            "ReverseDir",
            default=None,
        )
        native_elliptical = ActiveModelThroughHoleExecutor._com_member(
            created_definition,
            "Elliptical",
            default=None,
        )
        definition_matches = (
            native_height_m is not None
            and math.isclose(
                float(native_height_m),
                self._m(request["height_mm"]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and bool(native_reverse_direction) == request["reverse_direction"]
            and bool(native_elliptical) == request["elliptical"]
        )
        if not definition_matches:
            return self._failure(
                "Native Dome FeatureData does not match the requested parameters.",
                request=request,
                feature_name=original_name,
                native_definition={
                    "height_m": native_height_m,
                    "reverse_direction": native_reverse_direction,
                    "elliptical": native_elliptical,
                },
            )
        if created_feature is not None:
            self._name_feature(created_feature, requested_name)
        final_name = self._feature_name(created_feature) or original_name
        body_after = ActiveModelThroughHoleExecutor._body_info(model)
        volume_after = (
            ActiveModelThroughHoleExecutor._solid_volume(body_after.get("bodies", []))
            if body_after.get("success")
            else None
        )
        if (
            volume_before is None
            or volume_after is None
            or math.isclose(volume_before, volume_after, rel_tol=0.0, abs_tol=1e-15)
        ):
            return self._failure(
                "Native Dome feature did not produce a measurable solid-volume change.",
                request=request,
                feature_name=final_name,
                volume_before_m3=volume_before,
                volume_after_m3=volume_after,
            )
        return {
            "success": True,
            "message": "Native SolidWorks Dome created on one explicitly resolved planar face.",
            "type": "dome",
            "name": str(feature.get("name") or requested_name),
            "feature_name": final_name,
            "feature_type": str(created[0]["type"]),
            "height_mm": request["height_mm"],
            "reverse_direction": request["reverse_direction"],
            "elliptical": request["elliptical"],
            "native_definition": {
                "height_m": float(native_height_m),
                "height_mm": float(native_height_m) * 1000.0,
                "reverse_direction": bool(native_reverse_direction),
                "elliptical": bool(native_elliptical),
                "matches_request": True,
            },
            "request": request,
            "face_signature": face_result["signature"],
            "candidate_face_count": face_result["candidate_count"],
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_changed": True,
        }

    @staticmethod
    def _resolve_dome_face(
        bodies: list[Any],
        selector: dict[str, Any],
    ) -> dict[str, Any]:
        axis_index = {"x": 0, "y": 1, "z": 2}[str(selector["axis"])]
        coordinate_mm = float(selector["coordinate_mm"])
        coordinate_tolerance_mm = float(selector["coordinate_tolerance_mm"])
        area_mm2 = float(selector["area_mm2"])
        area_tolerance_mm2 = float(selector["area_tolerance_mm2"])
        normal_sign = int(selector["normal_sign"])
        body_index = selector.get("body_index")
        inspector = SolidWorksFaceInspector()
        matches: list[Any] = []
        matched_evidence: list[dict[str, Any]] = []
        observed_evidence: list[dict[str, Any]] = []
        for candidate in inspector.enumerate_planar_faces(bodies):
            signature = dict(candidate.signature)
            normal = list(signature.get("normal") or [0.0, 0.0, 0.0])
            candidate_coordinate_mm = float(signature["plane_coordinate_m"]) * 1000.0
            candidate_area_mm2 = float(signature["area_m2"]) * 1_000_000.0
            normalized_signature = {
                **signature,
                "plane_coordinate_mm": candidate_coordinate_mm,
                "area_mm2": candidate_area_mm2,
            }
            observed_evidence.append(normalized_signature)
            if body_index is not None and signature.get("body_index") != body_index:
                continue
            if int(signature.get("normal_axis", -1)) != axis_index:
                continue
            if normal_sign * float(normal[axis_index]) <= 0.999:
                continue
            if abs(candidate_coordinate_mm - coordinate_mm) > coordinate_tolerance_mm:
                continue
            if abs(candidate_area_mm2 - area_mm2) > area_tolerance_mm2:
                continue
            matches.append(candidate)
            matched_evidence.append(normalized_signature)
        if len(matches) != 1:
            return {
                "success": False,
                "message": (
                    "Dome planar-face signature is ambiguous."
                    if len(matches) > 1
                    else "No planar face matches the explicit dome signature."
                ),
                "candidate_count": len(matches),
                "candidates": matched_evidence,
                "observed_planar_faces": observed_evidence,
            }
        return {
            "success": True,
            "face": matches[0].entity,
            "signature": matched_evidence[0],
            "candidate_count": 1,
        }

    @staticmethod
    def _select_face_with_mark(model: Any, face: Any, mark: int) -> bool:
        selection_manager = ActiveModelThroughHoleExecutor._com_member(
            model,
            "SelectionManager",
            default=None,
        )
        select_data = ActiveModelThroughHoleExecutor._com_member(
            selection_manager,
            "CreateSelectData",
            default=None,
        )
        if select_data is None:
            return False
        try:
            select_data.Mark = int(mark)
            return bool(face.Select4(False, select_data))
        except Exception:
            return False

    def _create_external_thread(
        self,
        model: Any,
        plan: dict[str, Any],
        feature: dict[str, Any],
        body_info: dict[str, Any],
    ) -> dict[str, Any]:
        request = self.normalize_external_thread_request(dict(feature.get("params") or {}))
        if not request.get("success"):
            return self._failure(str(request.get("message") or "Invalid external-thread request."), request=request)

        bbox = dict(body_info.get("bbox") or {})
        axis = str(request["axis"])
        axial_span_mm = float(bbox.get({"x": "length", "y": "width", "z": "thickness"}[axis], 0.0)) * 1000.0
        if axial_span_mm <= 0:
            return self._failure("Active body has no measurable span along the thread axis.", request=request)
        if float(request["thread_length_mm"]) >= axial_span_mm:
            return self._failure(
                "External-thread length must be shorter than the active-body axial span.",
                request=request,
                axial_span_mm=axial_span_mm,
            )

        edge_result = self._find_axial_major_diameter_end_edge(
            body_info.get("bodies", []),
            bbox,
            axis=axis,
            end=str(request["end"]),
            major_diameter_mm=float(request["major_diameter_mm"]),
            axis_center_mm=request.get("axis_center_mm"),
        )
        if not edge_result.get("success"):
            return self._failure(str(edge_result.get("message") or "External-thread target edge was not resolved."), request=request, edge_resolution=edge_result)

        if request["representation"] == "modeled":
            return self._create_modeled_external_thread(
                model,
                feature,
                body_info,
                request,
                edge_result,
            )

        model.ClearSelection2(True)
        edge = edge_result["edge"]
        if not bool(ActiveModelThroughHoleExecutor._com_member(edge, "Select2", False, 0, default=False)):
            return self._failure("SolidWorks did not select the resolved external-thread edge.", request=request, edge_signature=edge_result.get("signature"))

        api_size = str(request["designation"]).replace("X", "x")
        created = ActiveModelThroughHoleExecutor._com_member(
            model.FeatureManager,
            "InsertCosmeticThread3",
            8,
            "Metric Die",
            api_size,
            self._m(float(request["major_diameter_mm"])),
            0,
            self._m(float(request["thread_length_mm"])),
            str(request["thread_callout"]),
            default=None,
        )
        if created is None:
            model.ClearSelection2(True)
            return self._failure(
                "SolidWorks InsertCosmeticThread3 returned no external-thread feature.",
                request=request,
                edge_signature=edge_result.get("signature"),
                api_standard=8,
                api_standard_type="Metric Die",
                api_size=api_size,
            )

        requested_name = str(feature.get("name") or f"ExternalThread_{request['end']}_{request['designation']}")
        self._name_feature(created, requested_name)
        model.ClearSelection2(True)
        ActiveModelThroughHoleExecutor._com_member(model, "ForceRebuild3", False)
        ActiveModelThroughHoleExecutor._com_member(model, "ShowCosmeticThread")

        feature_name = self._feature_name(created)
        feature_type = str(ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or "")
        error_code = ActiveModelThroughHoleExecutor._com_member(created, "GetErrorCode2", default=None)
        if error_code is None:
            error_code = ActiveModelThroughHoleExecutor._com_member(created, "GetErrorCode", default=0)
        try:
            error_code = int(error_code or 0)
        except (TypeError, ValueError):
            error_code = -1
        if error_code != 0:
            return self._failure(
                "SolidWorks created the external-thread feature with a rebuild error.",
                request=request,
                feature_name=feature_name,
                feature_type=feature_type,
                feature_error_code=error_code,
            )

        registry_evidence = self._registered_feature(
            model,
            requested_name,
            expected_type="CosmeticThread",
        )
        if not registry_evidence.get("success"):
            return self._failure(
                "SolidWorks returned an external-thread object, but the feature was not registered in the model.",
                request=request,
                feature_name=feature_name,
                feature_type=feature_type,
                feature_registry_evidence=registry_evidence,
            )

        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition", default=None)
        definition_evidence = {
            "standard": ActiveModelThroughHoleExecutor._com_member(definition, "Standard", default=None),
            "standard_type": ActiveModelThroughHoleExecutor._com_member(definition, "StandardType", default=None),
            "size": ActiveModelThroughHoleExecutor._com_member(definition, "Size", default=None),
            "diameter_m": ActiveModelThroughHoleExecutor._com_member(definition, "Diameter", default=None),
            "blind_depth_m": ActiveModelThroughHoleExecutor._com_member(definition, "BlindDepth", default=None),
            "end_condition": ActiveModelThroughHoleExecutor._com_member(definition, "EndCondition", default=None),
            "thread_callout": ActiveModelThroughHoleExecutor._com_member(definition, "ThreadCallout", default=None),
        }
        return {
            "success": True,
            "name": requested_name,
            "type": "external_thread",
            "request": request,
            "edge_signature": edge_result.get("signature"),
            "candidate_edge_count": edge_result.get("candidate_count"),
            "feature_name": feature_name,
            "feature_type": feature_type,
            "feature_error_code": error_code,
            "feature_registry_evidence": registry_evidence,
            "definition_evidence": definition_evidence,
            "features_created": 1,
        }

    def _create_modeled_external_thread(
        self,
        model: Any,
        feature: dict[str, Any],
        body_info: dict[str, Any],
        request: dict[str, Any],
        edge_result: dict[str, Any],
    ) -> dict[str, Any]:
        profile_path = self._metric_die_profile_path()
        if profile_path is None:
            return self._failure(
                "SolidWorks Metric Die thread profile library was not found.",
                request=request,
                edge_signature=edge_result.get("signature"),
            )

        try:
            feature_manager = model.FeatureManager
        except Exception:
            feature_manager = None
        if feature_manager is None:
            return self._failure("SolidWorks FeatureManager is unavailable.", request=request)
        definition = ActiveModelThroughHoleExecutor._com_member(
            feature_manager,
            "CreateDefinition",
            87,
            default=None,
        )
        if definition is None:
            return self._failure(
                "SolidWorks CreateDefinition(swFmSweepThread) returned no ThreadFeatureData object.",
                request=request,
                profile_path=str(profile_path),
            )

        try:
            ActiveModelThroughHoleExecutor._com_member(definition, "InitializeThreadData")
            definition.Type = str(profile_path)
            definition.Size = str(request["designation"]).replace("X", "x")
            definition.Edge = edge_result["edge"]
            definition.ThreadMethod = 0
            definition.EndCondition = 0
            definition.BlindDepth = self._m(float(request["thread_length_mm"]))
            definition.DiameterOverride = True
            definition.Diameter = self._m(float(request["major_diameter_mm"]))
            definition.PitchOverride = True
            definition.Pitch = self._m(float(request["pitch_mm"]))
            definition.RightHanded = bool(request["right_handed"])
            definition.TrimStartFace = bool(request["trim_start_face"])
            definition.TrimEndFace = bool(request["trim_end_face"])
            definition.ThreadStartAngle = math.radians(float(request["thread_start_angle_deg"]))
            definition.MultipleStart = False
            definition.MaintainThreadLength = False
            definition.ReverseDirection = self._modeled_thread_reverse_direction(request, edge_result)
        except Exception as exc:
            return self._failure(
                f"SolidWorks ThreadFeatureData configuration failed: {exc}",
                request=request,
                profile_path=str(profile_path),
                edge_signature=edge_result.get("signature"),
            )

        volume_before = ActiveModelThroughHoleExecutor._solid_volume(list(body_info.get("bodies") or []))
        body_count_before = len(list(body_info.get("bodies") or []))
        created = ActiveModelThroughHoleExecutor._com_member(
            feature_manager,
            "CreateFeature",
            definition,
            default=None,
        )
        if created is None:
            return self._failure(
                "SolidWorks CreateFeature returned no modeled external-thread feature.",
                request=request,
                profile_path=str(profile_path),
                edge_signature=edge_result.get("signature"),
                reverse_direction=bool(definition.ReverseDirection),
            )

        requested_name = str(feature.get("name") or f"ExternalThread_{request['end']}_{request['designation']}")
        self._name_feature(created, requested_name)
        ActiveModelThroughHoleExecutor._com_member(model, "ClearSelection2", True)
        ActiveModelThroughHoleExecutor._com_member(model, "ForceRebuild3", False)

        feature_name = self._feature_name(created)
        feature_type = str(ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or "")
        error_code = ActiveModelThroughHoleExecutor._com_member(created, "GetErrorCode2", default=None)
        if error_code is None:
            error_code = ActiveModelThroughHoleExecutor._com_member(created, "GetErrorCode", default=0)
        try:
            error_code = int(error_code or 0)
        except (TypeError, ValueError):
            error_code = -1

        after = ActiveModelThroughHoleExecutor._body_info(model)
        volume_after = (
            ActiveModelThroughHoleExecutor._solid_volume(list(after.get("bodies") or []))
            if after.get("success")
            else None
        )
        body_count_after = len(list(after.get("bodies") or [])) if after.get("success") else 0
        volume_removed = (
            float(volume_before) - float(volume_after)
            if volume_before is not None and volume_after is not None
            else None
        )
        volume_tolerance = max(abs(float(volume_before or 0.0)) * 1e-10, 1e-15)
        geometry_success = bool(
            after.get("success")
            and body_count_before > 0
            and body_count_after == body_count_before
            and volume_removed is not None
            and volume_removed > volume_tolerance
        )
        geometry_evidence = {
            "success": geometry_success,
            "body_count_before": body_count_before,
            "body_count_after": body_count_after,
            "volume_before_m3": volume_before,
            "volume_after_m3": volume_after,
            "volume_removed_m3": volume_removed,
            "volume_tolerance_m3": volume_tolerance,
            "bbox_before_m": dict(body_info.get("bbox") or {}),
            "bbox_after_m": dict(after.get("bbox") or {}),
        }
        registry_evidence = self._registered_feature(model, requested_name, expected_type="SweepThread")
        if error_code != 0 or feature_type.casefold() != "sweepthread" or not registry_evidence.get("success") or not geometry_success:
            return self._failure(
                "Modeled external thread failed feature or geometry verification.",
                request=request,
                feature_name=feature_name,
                feature_type=feature_type,
                feature_error_code=error_code,
                feature_registry_evidence=registry_evidence,
                geometry_evidence=geometry_evidence,
                profile_path=str(profile_path),
            )

        created_definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition", default=None)
        definition_evidence = {
            "type": ActiveModelThroughHoleExecutor._com_member(created_definition, "Type", default=None),
            "size": ActiveModelThroughHoleExecutor._com_member(created_definition, "Size", default=None),
            "thread_method": ActiveModelThroughHoleExecutor._com_member(created_definition, "ThreadMethod", default=None),
            "end_condition": ActiveModelThroughHoleExecutor._com_member(created_definition, "EndCondition", default=None),
            "blind_depth_m": ActiveModelThroughHoleExecutor._com_member(created_definition, "BlindDepth", default=None),
            "diameter_m": ActiveModelThroughHoleExecutor._com_member(created_definition, "Diameter", default=None),
            "pitch_m": ActiveModelThroughHoleExecutor._com_member(created_definition, "Pitch", default=None),
            "right_handed": ActiveModelThroughHoleExecutor._com_member(created_definition, "RightHanded", default=None),
            "reverse_direction": ActiveModelThroughHoleExecutor._com_member(created_definition, "ReverseDirection", default=None),
        }
        return {
            "success": True,
            "name": requested_name,
            "type": "external_thread",
            "request": request,
            "edge_signature": edge_result.get("signature"),
            "candidate_edge_count": edge_result.get("candidate_count"),
            "feature_name": feature_name,
            "feature_type": feature_type,
            "feature_error_code": error_code,
            "feature_registry_evidence": registry_evidence,
            "definition_evidence": definition_evidence,
            "geometry_evidence": geometry_evidence,
            "profile_path": str(profile_path),
            "features_created": 1,
        }

    @staticmethod
    def _modeled_thread_reverse_direction(request: dict[str, Any], edge_result: dict[str, Any]) -> bool:
        explicit = request.get("reverse_direction")
        if explicit is not None:
            return bool(explicit)
        axis_index = {"x": 0, "y": 1, "z": 2}[str(request["axis"])]
        normal = list((edge_result.get("signature") or {}).get("normal") or [0.0, 0.0, 0.0])
        outward = float(normal[axis_index]) if len(normal) > axis_index else 0.0
        inward = 1.0 if request["end"] == "min" else -1.0
        return bool(outward and outward * inward < 0.0)

    @staticmethod
    def _metric_die_profile_path() -> Path | None:
        program_data = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
        root = program_data / "SOLIDWORKS"
        candidates = list(root.glob("SOLIDWORKS */Thread Profiles/Metric Die.SLDLFP"))
        candidates.extend(root.glob("SOLIDWORKS/Thread Profiles/Metric Die.SLDLFP"))
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            return None
        return sorted(existing, key=lambda path: path.parent.parent.name, reverse=True)[0]

    @staticmethod
    def _find_axial_major_diameter_end_edge(
        bodies: list[Any],
        bbox: dict[str, float],
        *,
        axis: str,
        end: str,
        major_diameter_mm: float,
        axis_center_mm: list[float] | tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        axis_names = ("x", "y", "z")
        if axis not in axis_names or end not in {"min", "max"}:
            return {"success": False, "message": "External-thread edge resolver received an invalid axis or end."}
        axis_index = axis_names.index(axis)
        radial_indices = [index for index in range(3) if index != axis_index]
        center = [
            (float(bbox[f"{name}min"]) + float(bbox[f"{name}max"])) / 2.0
            for name in axis_names
        ]
        center_source = "body_bounding_box"
        if axis_center_mm is not None:
            if len(axis_center_mm) != 2:
                return {
                    "success": False,
                    "message": "External-thread axis center must contain the two radial coordinates.",
                }
            try:
                for radial_index, value_mm in zip(radial_indices, axis_center_mm):
                    center[radial_index] = float(value_mm) / 1000.0
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "message": "External-thread axis center coordinates must be numeric.",
                }
            center_source = "explicit_axis_center_mm"
        target_radius = major_diameter_mm / 2000.0
        radial_tolerance = max(1e-7, target_radius * 0.0025)
        center_tolerance = max(1e-7, target_radius * 0.0025)
        candidates: list[tuple[Any, tuple[float, ...]]] = []
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                curve = ActiveModelThroughHoleExecutor._com_member(edge, "GetCurve", default=None)
                circle = ActiveModelThroughHoleExecutor._com_member(curve, "CircleParams", default=None)
                if not isinstance(circle, (tuple, list)) or len(circle) < 7:
                    continue
                try:
                    values = tuple(float(value) for value in circle[:7])
                except (TypeError, ValueError):
                    continue
                edge_center = values[:3]
                normal = values[3:6]
                radius = abs(values[6])
                if abs(normal[axis_index]) < 0.985:
                    continue
                if abs(radius - target_radius) > radial_tolerance:
                    continue
                if any(abs(edge_center[index] - center[index]) > center_tolerance for index in radial_indices):
                    continue
                candidates.append((edge, values))

        if not candidates:
            return {
                "success": False,
                "message": "No circular edge matched the requested shaft axis and major diameter.",
                "axis": axis,
                "end": end,
                "major_diameter_mm": major_diameter_mm,
                "candidate_count": 0,
                "axis_center_mm": [round(center[index] * 1000.0, 6) for index in radial_indices],
                "axis_center_source": center_source,
            }
        selected = min(candidates, key=lambda item: item[1][axis_index]) if end == "min" else max(candidates, key=lambda item: item[1][axis_index])
        values = selected[1]
        extreme = values[axis_index]
        tied = [item for item in candidates if abs(item[1][axis_index] - extreme) <= 1e-8]
        if len(tied) != 1:
            return {
                "success": False,
                "message": "External-thread edge resolution was ambiguous at the requested shaft end.",
                "axis": axis,
                "end": end,
                "major_diameter_mm": major_diameter_mm,
                "candidate_count": len(candidates),
                "ambiguous_count": len(tied),
                "axis_center_mm": [round(center[index] * 1000.0, 6) for index in radial_indices],
                "axis_center_source": center_source,
            }
        return {
            "success": True,
            "edge": selected[0],
            "candidate_count": len(candidates),
            "signature": {
                "center_mm": [round(value * 1000.0, 6) for value in values[:3]],
                "normal": [round(value, 6) for value in values[3:6]],
                "radius_mm": round(abs(values[6]) * 1000.0, 6),
                "axis": axis,
                "end": end,
                "axis_center_mm": [round(center[index] * 1000.0, 6) for index in radial_indices],
                "axis_center_source": center_source,
            },
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
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

    @staticmethod
    def _feature_center(params: dict[str, Any], bbox: dict[str, float]) -> tuple[float, float]:
        xy = params.get("position_xy") or params.get("center_xy")
        if isinstance(xy, (tuple, list)) and len(xy) >= 2:
            return float(xy[0]) / 1000.0, float(xy[1]) / 1000.0
        return (bbox["xmin"] + bbox["xmax"]) / 2.0, (bbox["ymin"] + bbox["ymax"]) / 2.0

    @staticmethod
    def _find_planar_face_at_z(bodies: list[Any], target_z: float) -> Any | None:
        tolerance = 1e-5
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
                zmin, zmax = float(box[2]), float(box[5])
                if abs(zmax - zmin) > tolerance or abs(zmax - target_z) > tolerance:
                    continue
                surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface")
                is_plane = ActiveModelThroughHoleExecutor._com_member(surface, "IsPlane", default=True) if surface is not None else True
                if is_plane is False:
                    continue
                area = abs((float(box[3]) - float(box[0])) * (float(box[4]) - float(box[1])))
                if area > best_area:
                    best = face
                    best_area = area
        return best

    @staticmethod
    def _create_face_sketch(model: Any, face: Any, draw: Callable[[], Any]) -> str:
        model.ClearSelection2(True)
        selected = ActiveModelFeatureSkill._select_face(model, face)
        if not selected:
            raise RuntimeError("Could not select target planar face.")
        model.SketchManager.InsertSketch(True)
        active = model.SketchManager.ActiveSketch
        if active is None:
            raise RuntimeError("SolidWorks did not enter sketch mode.")
        sketch_name = str(active.Name)
        draw()
        model.SketchManager.InsertSketch(True)
        return sketch_name

    @staticmethod
    def _select_face(model: Any, face: Any) -> bool:
        for _attempt in range(3):
            model.ClearSelection2(True)
            try:
                if face.Select2(False, 0):
                    return True
            except Exception:
                pass
            try:
                model.ForceRebuild3(False)
            except Exception:
                pass

        box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox")
        if not box or len(box) < 6:
            return False
        box = [float(value) for value in box]
        spans = [abs(box[index + 3] - box[index]) for index in range(3)]
        axis = min(range(3), key=spans.__getitem__)
        target = (box[axis] + box[axis + 3]) / 2.0
        direction = -1.0 if target >= 0 else 1.0
        origin_axis = target - direction
        other = [index for index in range(3) if index != axis]
        fractions = ((0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
        for first_fraction, second_fraction in fractions:
            point = [0.0, 0.0, 0.0]
            point[axis] = origin_axis
            point[other[0]] = box[other[0]] + spans[other[0]] * first_fraction
            point[other[1]] = box[other[1]] + spans[other[1]] * second_fraction
            ray = [0.0, 0.0, 0.0]
            ray[axis] = direction
            model.ClearSelection2(True)
            try:
                selected = bool(
                    model.Extension.SelectByRay(
                        point[0], point[1], point[2],
                        ray[0], ray[1], ray[2],
                        1e-5, 2, False, 0, 0,
                    )
                )
            except Exception:
                selected = False
            if not selected:
                continue
            selected_face = ActiveModelThroughHoleExecutor._com_member(model.SelectionManager, "GetSelectedObject6", 1, -1)
            selected_box = ActiveModelThroughHoleExecutor._com_member(selected_face, "GetBox")
            if selected_box and abs(float(selected_box[axis]) - target) <= 5e-5:
                return True
        model.ClearSelection2(True)
        return False

    @staticmethod
    def _select_sketch(model: Any, sketch_name: str) -> bool:
        model.ClearSelection2(True)
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", sketch_name)
        if feature is not None:
            try:
                if feature.Select2(False, 0):
                    return True
            except Exception:
                pass
        try:
            return bool(model.Extension.SelectByID2(sketch_name, "SKETCH", 0, 0, 0, False, 0, None, 0))
        except Exception:
            return False

    @classmethod
    def _extrude_boss(
        cls,
        model: Any,
        sketch_name: str,
        depth: float,
        direction: bool,
        *,
        merge: bool = True,
    ) -> Any | None:
        if not cls._select_sketch(model, sketch_name):
            return None
        return model.FeatureManager.FeatureExtrusion3(
            True, False, direction, 0, 0, depth, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, merge, False, True, 0, 0.0, False,
        )

    @classmethod
    def _extrude_cut(
        cls,
        model: Any,
        sketch_name: str,
        depth: float,
        through_all: bool,
        direction: bool,
    ) -> Any | None:
        if not cls._select_sketch(model, sketch_name):
            return None
        return model.FeatureManager.FeatureCut4(
            direction, False, False, 1 if through_all else 0, 0, depth, 0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False,
            True, True, True, True, False, 0, 0, False, False,
        )

    @staticmethod
    def _select_rect_vertical_edges(
        model: Any,
        bodies: list[Any],
        cx: float,
        cy: float,
        length: float,
        width: float,
        z_top: float,
        z_bottom: float,
    ) -> int:
        tolerance = 0.001
        corners = [(cx + sx * length / 2.0, cy + sy * width / 2.0) for sx in (-1, 1) for sy in (-1, 1)]
        model.ClearSelection2(True)
        selected = 0
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                points = ProductionFilletSkill._edge_points(edge)
                if points is None:
                    continue
                start, end = points
                if abs(end[2] - start[2]) < abs(z_top - z_bottom) * 0.75:
                    continue
                mx = (start[0] + end[0]) / 2.0
                my = (start[1] + end[1]) / 2.0
                mz = (start[2] + end[2]) / 2.0
                if not (min(z_top, z_bottom) - tolerance <= mz <= max(z_top, z_bottom) + tolerance):
                    continue
                if not any(abs(mx - x) <= tolerance and abs(my - y) <= tolerance for x, y in corners):
                    continue
                try:
                    if edge.Select2(selected > 0, 0):
                        selected += 1
                except Exception:
                    continue
        return selected

    @staticmethod
    def _select_rect_perimeter_edges(model: Any, bodies: list[Any], group: dict[str, Any]) -> int:
        tolerance = 0.0015
        target_z = float(group["z"])
        cx, cy = float(group["cx"]), float(group["cy"])
        half_x, half_y = float(group["half_x"]), float(group["half_y"])
        model.ClearSelection2(True)
        selected = 0
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges") or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                points = ProductionFilletSkill._edge_points(edge)
                if points is None:
                    continue
                start, end = points
                if abs(start[2] - target_z) > tolerance or abs(end[2] - target_z) > tolerance:
                    continue
                on_perimeter = all(
                    abs(abs(point[0] - cx) - half_x) <= tolerance or abs(abs(point[1] - cy) - half_y) <= tolerance
                    for point in (start, end)
                )
                if not on_perimeter:
                    continue
                try:
                    if edge.Select2(selected > 0, 0):
                        selected += 1
                except Exception:
                    continue
        return selected

    @staticmethod
    def _feature_tree(model: Any) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        manager = ActiveModelThroughHoleExecutor._com_member(model, "FeatureManager")
        features = ActiveModelThroughHoleExecutor._com_member(manager, "GetFeatures", False, default=None)
        if features is not None:
            if not isinstance(features, (tuple, list)):
                try:
                    features = list(features)
                except TypeError:
                    features = [features]
            for feature in features:
                item = {
                    "name": str(ActiveModelThroughHoleExecutor._com_member(feature, "Name", default="") or ""),
                    "type": str(ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""),
                }
                identity = (item["name"], item["type"])
                if identity not in seen:
                    seen.add(identity)
                    result.append(item)
            if result:
                return result[:500]

        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None and len(result) < 500:
            item = {
                "name": str(ActiveModelThroughHoleExecutor._com_member(feature, "Name", default="") or ""),
                "type": str(ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""),
            }
            identity = (item["name"], item["type"])
            if identity in seen:
                break
            seen.add(identity)
            result.append(item)
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        return result

    @staticmethod
    def _registered_feature(model: Any, name: str, *, expected_type: str) -> dict[str, Any]:
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", name, default=None)
        if feature is None:
            manager = ActiveModelThroughHoleExecutor._com_member(model, "FeatureManager")
            features = ActiveModelThroughHoleExecutor._com_member(manager, "GetFeatures", False, default=None)
            if features is not None:
                if not isinstance(features, (tuple, list)):
                    try:
                        features = list(features)
                    except TypeError:
                        features = [features]
                feature = next(
                    (
                        item
                        for item in features
                        if str(ActiveModelThroughHoleExecutor._com_member(item, "Name", default="") or "") == name
                    ),
                    None,
                )
        actual_name = str(ActiveModelThroughHoleExecutor._com_member(feature, "Name", default="") or "")
        actual_type = str(ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or "")
        success = feature is not None and actual_name == name and actual_type.casefold() == expected_type.casefold()
        return {
            "success": success,
            "name": actual_name,
            "type": actual_type,
            "expected_name": name,
            "expected_type": expected_type,
        }

    @staticmethod
    def _feature_name(feature: Any) -> str:
        if feature is None:
            return ""
        return str(ActiveModelThroughHoleExecutor._com_member(feature, "Name", default="") or "")

    @staticmethod
    def _name_feature(feature: Any, name: str) -> None:
        try:
            feature.Name = str(name)
        except Exception:
            pass

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
