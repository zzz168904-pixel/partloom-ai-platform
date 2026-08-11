from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .active_model_feature_skill import ActiveModelFeatureSkill
from .active_model_pattern_skill import ActiveModelPatternSkill
from .active_model_through_hole import ActiveModelThroughHoleExecutor
from .feature_management_skill import FeatureManagementSkill
from .models import SkillResult


class GearSkill:
    """Create one standard full-depth involute spur gear Part.

    The production contract is intentionally narrow: one new Part, one module,
    one pressure angle, a cylindrical bore, and an optional straight keyway.
    It never loads Toolbox, a macro, a sample model, or an export template.
    """

    FEATURE_TYPE = "gear"
    GEAR_TYPES = {"spur", "spur_gear", "straight", "straight_spur"}

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"
        self.pattern_skill = ActiveModelPatternSkill(output_root, self.solidworks_skill_dir)

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == self.FEATURE_TYPE]
        if len(features) != 1:
            return SkillResult(False, "gear requires exactly one explicit gear feature per Part task.")

        run_dir = self.output_root / f"gear_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "gear_report.json"
        feature = features[0]
        request = self.normalize_request(feature.get("params", {}), plan.get("task_type"))
        if not request.get("success"):
            return self._result(False, str(request.get("message")), report_path, {
                "feature_type": self.FEATURE_TYPE,
                "invalid_request": request,
            })

        try:
            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document
            from sw_part import extrude_midplane, sketch

            sw, _active = connect_solidworks(visible=True)
            model = new_document(sw, "part")
            validation = self._validate_new_part(model)
            if not validation.get("success"):
                return self._result(False, str(validation.get("message")), report_path, validation)

            operation = self._create_gear(model, feature, request, sketch, extrude_midplane)
            if not operation.get("success"):
                return self._result(
                    False,
                    str(operation.get("message") or "SolidWorks gear creation failed."),
                    report_path,
                    {"feature_type": self.FEATURE_TYPE, "operations": [operation]},
                )

            model.ClearSelection2(True)
            model.ForceRebuild3(False)
            geometry = self._verify_geometry(model, request, operation)
            operation["geometry_validation"] = geometry
            if not geometry.get("success"):
                return self._result(
                    False,
                    str(geometry.get("message") or "Gear geometry validation failed."),
                    report_path,
                    {"feature_type": self.FEATURE_TYPE, "operations": [operation]},
                )

            material = str(
                feature.get("params", {}).get("material")
                or plan.get("parameters", {}).get("material")
                or ""
            ).strip()
            material_metadata = self._set_properties(model, request, material)
            model.ViewZoomtofit2()

            requested_path = str(plan.get("execution_model_path") or "").strip()
            part_path = Path(requested_path) if requested_path else run_dir / f"{self._safe_stem(feature.get('name'))}.SLDPRT"
            part_path.parent.mkdir(parents=True, exist_ok=True)
            if part_path.exists():
                return self._result(False, f"Gear output already exists and will not be overwritten: {part_path}", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })
            if not save_document(model, str(part_path)):
                return self._result(False, f"SolidWorks failed to save gear Part: {part_path}", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })
            if not part_path.is_file() or part_path.stat().st_size <= 0:
                return self._result(False, f"Saved gear Part is missing or empty: {part_path}", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            reopen = self._verify_reopen(sw, model, part_path, request, operation)
            reopened_model = reopen.pop("model", None)
            if not reopen.get("success"):
                return self._result(False, "Saved gear Part could not be reopened and verified.", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                    "reopen_validation": reopen,
                    "files": [str(part_path)],
                })

            active_title = str(get_com_member(reopened_model, "GetTitle") or "")
            data = {
                "active_doc": active_title,
                "mode": request["mode"],
                "feature_type": self.FEATURE_TYPE,
                "feature_created": True,
                "features_created": 1,
                "operations": [operation],
                "material": material,
                "material_metadata": material_metadata,
                "reopen_validation": reopen,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(reopened_model),
                "saved_by_this_skill": True,
                "side_effects": {
                    "modifies_active_doc": False,
                    "creates_new_doc": True,
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": [str(part_path)],
            }
            if bool(plan.get("execution_close_after_save", False)) and active_title:
                sw.CloseDoc(active_title)
                data["closed_after_save"] = True
            return self._result(True, "SolidWorks involute spur gear created and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"gear failed: {exc}", report_path, {
                "feature_type": self.FEATURE_TYPE,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        mode = str(params.get("mode") or "new_model").strip().lower()
        if mode != "new_model":
            return {"success": False, "message": "The first gear production contract supports mode=new_model only."}
        if task_type == "modify_3d":
            return {"success": False, "message": "modify_3d cannot create a new gear Part."}

        gear_type = str(params.get("gear_type") or params.get("type") or "spur").strip().lower()
        if gear_type not in cls.GEAR_TYPES:
            return {"success": False, "message": "gear_type must be spur; helical, bevel, worm, and rack gears are not yet executable."}

        module = cls._number(params, "module_mm", "module", "modulus")
        teeth_value = cls._number(params, "teeth", "tooth_count", "z")
        face_width = cls._number(params, "face_width_mm", "face_width", "width_mm", "width")
        pressure_angle = cls._number(params, "pressure_angle_deg", "pressure_angle")
        bore = cls._number(params, "bore_diameter_mm", "bore_diameter", "shaft_hole_diameter_mm")
        backlash = cls._number(params, "backlash_mm", "backlash")
        pressure_angle = 20.0 if pressure_angle is None else pressure_angle
        bore = 0.0 if bore is None else bore
        backlash = 0.0 if backlash is None else backlash

        if module is None or not 0.2 <= module <= 20.0:
            return {"success": False, "message": "module_mm must be between 0.2 and 20."}
        if teeth_value is None or abs(teeth_value - round(teeth_value)) > 1e-9:
            return {"success": False, "message": "teeth must be an integer."}
        teeth = int(round(teeth_value))
        if face_width is None or not 1.0 <= face_width <= 300.0:
            return {"success": False, "message": "face_width_mm must be between 1 and 300."}
        if not 14.5 <= pressure_angle <= 25.0:
            return {"success": False, "message": "pressure_angle_deg must be between 14.5 and 25 degrees."}
        minimum_teeth = max(12, int(math.ceil(2.0 / (math.sin(math.radians(pressure_angle)) ** 2))))
        if teeth < minimum_teeth or teeth > 120:
            return {
                "success": False,
                "message": f"teeth must be between {minimum_teeth} and 120 for a zero-profile-shift full-depth gear at this pressure angle.",
                "minimum_teeth": minimum_teeth,
            }

        pitch_radius = module * teeth / 2.0
        base_radius = pitch_radius * math.cos(math.radians(pressure_angle))
        outside_radius = pitch_radius + module
        root_radius = pitch_radius - 1.25 * module
        if root_radius <= 0:
            return {"success": False, "message": "The requested module and tooth count produce a non-positive root radius."}
        if bore < 0 or bore >= 2.0 * (root_radius - module):
            return {"success": False, "message": "bore_diameter_mm must leave at least one module of radial material inside the root circle."}
        circular_pitch = math.pi * module
        if backlash < 0 or backlash >= circular_pitch * 0.25:
            return {"success": False, "message": "backlash_mm must be non-negative and less than one quarter of the circular pitch."}

        flank_samples = int(params.get("flank_samples") or 7)
        if not 4 <= flank_samples <= 16:
            return {"success": False, "message": "flank_samples must be between 4 and 16."}

        keyway_result = cls._normalize_keyway(params.get("keyway"), bore, root_radius)
        if not keyway_result.get("success"):
            return keyway_result

        half_tooth_angle = math.pi / (2.0 * teeth) - backlash / (2.0 * pitch_radius)
        start_radius = max(base_radius, root_radius)
        start_angle = cls._flank_angle(start_radius, base_radius, pressure_angle, half_tooth_angle)
        tip_angle = cls._flank_angle(outside_radius, base_radius, pressure_angle, half_tooth_angle)
        if tip_angle <= 0 or start_angle >= math.pi / teeth:
            return {"success": False, "message": "The requested backlash leaves an invalid involute tooth thickness."}

        request = {
            "success": True,
            "mode": "new_model",
            "gear_type": "spur",
            "standard": str(params.get("standard") or "ISO full-depth involute"),
            "module_mm": float(module),
            "teeth": teeth,
            "pressure_angle_deg": float(pressure_angle),
            "face_width_mm": float(face_width),
            "bore_diameter_mm": float(bore),
            "backlash_mm": float(backlash),
            "profile_shift_coefficient": 0.0,
            "flank_samples": flank_samples,
            "pitch_diameter_mm": pitch_radius * 2.0,
            "base_diameter_mm": base_radius * 2.0,
            "outside_diameter_mm": outside_radius * 2.0,
            "root_diameter_mm": root_radius * 2.0,
            "circular_pitch_mm": circular_pitch,
            "minimum_teeth_without_undercut": minimum_teeth,
            "keyway": keyway_result["keyway"],
        }
        profile = cls.tooth_profile_points(request)
        request["tooth_profile_points_mm"] = profile
        request["expected_bounds_mm"] = cls.pattern_bounds(profile, teeth, root_radius)
        return request

    @classmethod
    def tooth_profile_points(cls, request: dict[str, Any]) -> list[list[float]]:
        module = float(request["module_mm"])
        teeth = int(request["teeth"])
        pressure_angle = float(request["pressure_angle_deg"])
        pitch_radius = float(request["pitch_diameter_mm"]) / 2.0
        base_radius = float(request["base_diameter_mm"]) / 2.0
        outside_radius = float(request["outside_diameter_mm"]) / 2.0
        root_radius = float(request["root_diameter_mm"]) / 2.0
        backlash = float(request["backlash_mm"])
        samples = int(request["flank_samples"])
        half_tooth = math.pi / (2.0 * teeth) - backlash / (2.0 * pitch_radius)
        start_radius = max(base_radius, root_radius)
        radii = [start_radius + (outside_radius - start_radius) * index / (samples - 1) for index in range(samples)]
        angles = [cls._flank_angle(radius, base_radius, pressure_angle, half_tooth) for radius in radii]
        # Keep the seed tooth well inside the root disk. When base and root
        # radii are close, a tiny overlap segment can be rejected by the
        # SolidWorks sketch kernel even though the involute itself is valid.
        overlap_radius = max(0.01, root_radius - min(module * 0.5, 1.0))

        lower = [cls._polar(radius, -angle) for radius, angle in zip(radii, angles)]
        upper = [cls._polar(radius, angle) for radius, angle in zip(reversed(radii), reversed(angles))]
        tip_angle = angles[-1]
        tip_arc = [
            cls._polar(outside_radius, -tip_angle + (2.0 * tip_angle * index / 4.0))
            for index in range(1, 4)
        ]
        points = [cls._polar(overlap_radius, -angles[0]), *lower, *tip_arc, *upper, cls._polar(overlap_radius, angles[0])]
        points.append(points[0][:])
        return [[round(x, 9), round(y, 9)] for x, y in points]

    @staticmethod
    def pattern_bounds(profile: list[list[float]], teeth: int, root_radius_mm: float) -> dict[str, float]:
        xs = [-float(root_radius_mm), float(root_radius_mm)]
        ys = [-float(root_radius_mm), float(root_radius_mm)]
        pitch = 2.0 * math.pi / int(teeth)
        for instance in range(int(teeth)):
            angle = instance * pitch
            c, s = math.cos(angle), math.sin(angle)
            for x, y in profile[:-1]:
                xs.append(float(x) * c - float(y) * s)
                ys.append(float(x) * s + float(y) * c)
        return {
            "xmin": min(xs),
            "xmax": max(xs),
            "ymin": min(ys),
            "ymax": max(ys),
            "x_span": max(xs) - min(xs),
            "y_span": max(ys) - min(ys),
        }

    def _create_gear(self, model: Any, feature: dict[str, Any], request: dict[str, Any], sketch: Any, extrude_midplane: Any) -> dict[str, Any]:
        with sketch(model, "Front Plane") as root_sketch:
            outer = model.SketchManager.CreateCircleByRadius(0.0, 0.0, 0.0, self._m(request["root_diameter_mm"] / 2.0))
            if outer is None:
                return {"success": False, "message": "SolidWorks failed to create the gear root circle."}
            if request["bore_diameter_mm"] > 0:
                bore = model.SketchManager.CreateCircleByRadius(0.0, 0.0, 0.0, self._m(request["bore_diameter_mm"] / 2.0))
                if bore is None:
                    return {"success": False, "message": "SolidWorks failed to create the gear bore circle."}
        root_feature = extrude_midplane(model, root_sketch, self._m(request["face_width_mm"]))
        if root_feature is None:
            return {"success": False, "message": "SolidWorks failed to extrude the gear root disk and bore."}
        ActiveModelFeatureSkill._name_feature(root_feature, "GearRootDisk")

        profile = request["tooth_profile_points_mm"]
        with sketch(model, "Front Plane") as tooth_sketch:
            for segment_index, (first, second) in enumerate(zip(profile, profile[1:]), start=1):
                line = None
                for attempt in range(3):
                    line = model.SketchManager.CreateLine(
                        self._m(first[0]), self._m(first[1]), 0.0,
                        self._m(second[0]), self._m(second[1]), 0.0,
                    )
                    if line is not None:
                        break
                    if attempt < 2:
                        time.sleep(0.05)
                if line is None:
                    return {
                        "success": False,
                        "message": f"SolidWorks failed to create involute tooth sketch segment {segment_index} after 3 attempts.",
                        "segment_index": segment_index,
                        "segment_points_mm": [first, second],
                    }
        tooth_feature = extrude_midplane(model, tooth_sketch, self._m(request["face_width_mm"]))
        if tooth_feature is None:
            return {"success": False, "message": "SolidWorks failed to extrude the involute seed tooth."}
        ActiveModelFeatureSkill._name_feature(tooth_feature, "GearTooth")
        model.ForceRebuild3(False)

        body_info = ActiveModelThroughHoleExecutor._body_info(model)
        if not body_info.get("success"):
            return {"success": False, "message": str(body_info.get("message") or "Gear seed produced no body.")}
        pattern_request = ActiveModelPatternSkill.normalize_request("circular_pattern", {
            "seed_features": ["GearTooth"],
            "count": request["teeth"],
            "total_angle_deg": 360.0,
            "axis": "z",
            "axis_center_mm": [0.0, 0.0],
            "axis_tolerance_mm": 0.05,
            "equal_spacing": True,
            "geometry_pattern": True,
        })
        if not pattern_request.get("success"):
            return {"success": False, "message": str(pattern_request.get("message")), "pattern_request": pattern_request}
        pattern = self.pattern_skill._create_circular_pattern(
            model,
            {"name": "GearToothPattern", "type": "circular_pattern"},
            pattern_request,
            body_info,
        )
        if not pattern.get("success"):
            return {"success": False, "message": str(pattern.get("message") or "Gear tooth pattern failed."), "pattern": pattern}

        axis = self._create_gear_axis(model)
        if not axis.get("success"):
            return axis

        keyway_feature_name = None
        if request["keyway"]["enabled"]:
            keyway = self._create_keyway(model, request)
            if not keyway.get("success"):
                return keyway
            keyway_feature_name = keyway["feature_name"]

        feature_name = str(feature.get("name") or "SpurGear")
        return {
            "success": True,
            "name": feature_name,
            "type": self.FEATURE_TYPE,
            "request": request,
            "root_feature_name": ActiveModelFeatureSkill._feature_name(root_feature),
            "tooth_feature_name": ActiveModelFeatureSkill._feature_name(tooth_feature),
            "pattern_feature_name": pattern.get("feature_name"),
            "axis_feature_name": axis.get("feature_name"),
            "keyway_feature_name": keyway_feature_name,
            "feature_name": pattern.get("feature_name"),
            "pattern": pattern,
        }

    @staticmethod
    def _create_gear_axis(model: Any) -> dict[str, Any]:
        before_tree = ActiveModelFeatureSkill._feature_tree(model)
        model.ClearSelection2(True)
        if not FeatureManagementSkill._select_named_plane(model, "Top Plane", append=False):
            return {"success": False, "message": "Could not select Top Plane for GearAxis."}
        if not FeatureManagementSkill._select_named_plane(model, "Right Plane", append=True):
            return {"success": False, "message": "Could not select Right Plane for GearAxis."}
        if not bool(model.InsertAxis2(True)):
            return {"success": False, "message": "SolidWorks failed to create GearAxis."}
        model.ForceRebuild3(False)
        axis = FeatureManagementSkill._new_feature(model, before_tree, ("axis", "refaxis"))
        if axis is None:
            return {"success": False, "message": "SolidWorks created no reference-axis feature for GearAxis."}
        ActiveModelFeatureSkill._name_feature(axis, "GearAxis")
        return {"success": True, "feature_name": ActiveModelFeatureSkill._feature_name(axis)}

    def _create_keyway(self, model: Any, request: dict[str, Any]) -> dict[str, Any]:
        body_info = ActiveModelThroughHoleExecutor._body_info(model)
        if not body_info.get("success"):
            return {"success": False, "message": "Could not inspect the gear body before keyway creation."}
        top_z = float(body_info["bbox"]["zmax"])
        face = ActiveModelFeatureSkill._find_planar_face_at_z(body_info["bodies"], top_z)
        if face is None:
            return {"success": False, "message": "Could not find the gear end face for the keyway sketch."}
        keyway = request["keyway"]
        half_width = self._m(keyway["width_mm"] / 2.0)
        bore_radius = self._m(request["bore_diameter_mm"] / 2.0)
        overlap = self._m(min(0.25, keyway["depth_mm"] * 0.25))
        outer_y = bore_radius + self._m(keyway["depth_mm"])
        sketch_name = ActiveModelFeatureSkill._create_face_sketch(
            model,
            face,
            lambda: model.SketchManager.CreateCornerRectangle(
                -half_width, bore_radius - overlap, top_z,
                half_width, outer_y, top_z,
            ),
        )
        cut = ActiveModelFeatureSkill._extrude_cut(model, sketch_name, self._m(request["face_width_mm"]), True, True)
        if cut is None:
            cut = ActiveModelFeatureSkill._extrude_cut(model, sketch_name, self._m(request["face_width_mm"]), True, False)
        if cut is None:
            return {"success": False, "message": "SolidWorks failed to cut the through keyway."}
        ActiveModelFeatureSkill._name_feature(cut, "GearKeyway")
        return {"success": True, "feature_name": ActiveModelFeatureSkill._feature_name(cut)}

    @classmethod
    def _verify_geometry(cls, model: Any, request: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
        body_info = ActiveModelThroughHoleExecutor._body_info(model)
        if not body_info.get("success") or len(body_info.get("bodies", [])) != 1:
            return {"success": False, "message": "Gear must contain exactly one solid body."}
        bbox = body_info["bbox"]
        actual = {
            "x_span": (float(bbox["xmax"]) - float(bbox["xmin"])) * 1000.0,
            "y_span": (float(bbox["ymax"]) - float(bbox["ymin"])) * 1000.0,
            "z_span": (float(bbox["zmax"]) - float(bbox["zmin"])) * 1000.0,
        }
        expected = request["expected_bounds_mm"]
        tolerance = max(0.15, float(request["outside_diameter_mm"]) * 0.005)
        bounds_ok = (
            abs(actual["x_span"] - float(expected["x_span"])) <= tolerance
            and abs(actual["y_span"] - float(expected["y_span"])) <= tolerance
            and abs(actual["z_span"] - float(request["face_width_mm"])) <= max(0.05, tolerance * 0.25)
        )
        feature_tree = ActiveModelFeatureSkill._feature_tree(model)
        feature_names = {item["name"] for item in feature_tree}
        pattern_instances = cls._pattern_instances(model, str(operation.get("pattern_feature_name") or "GearToothPattern"))
        bore_radii = cls._cylinder_radii_mm(body_info.get("bodies", []))
        bore_radius = float(request["bore_diameter_mm"]) / 2.0
        bore_ok = bore_radius <= 0 or any(abs(radius - bore_radius) <= 0.05 for radius in bore_radii)
        keyway_ok = not request["keyway"]["enabled"] or "GearKeyway" in feature_names
        axis_ok = "GearAxis" in feature_names
        pattern_ok = "GearToothPattern" in feature_names and (
            pattern_instances is None or pattern_instances == int(request["teeth"])
        )
        success = bounds_ok and bore_ok and keyway_ok and axis_ok and pattern_ok
        return {
            "success": success,
            "message": "Gear geometry matches the normalized request." if success else "Gear geometry does not match the normalized request.",
            "solid_body_count": len(body_info.get("bodies", [])),
            "actual_spans_mm": actual,
            "expected_bounds_mm": expected,
            "bounds_tolerance_mm": tolerance,
            "bounds_match": bounds_ok,
            "pattern_instances": pattern_instances,
            "expected_pattern_instances": int(request["teeth"]),
            "pattern_match": pattern_ok,
            "cylinder_radii_mm": bore_radii,
            "bore_match": bore_ok,
            "keyway_match": keyway_ok,
            "axis_match": axis_ok,
            "feature_names": sorted(feature_names),
            "bbox_m": bbox,
        }

    @classmethod
    def _verify_reopen(
        cls,
        sw: Any,
        model: Any,
        path: Path,
        request: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            current_path = str(ActiveModelThroughHoleExecutor._com_member(model, "GetPathName", default="") or "")
            if not current_path or Path(current_path).resolve() != path.resolve():
                return {"success": False, "message": "Active gear path changed before reopen validation."}
            title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
            if title:
                sw.CloseDoc(title)
            opened = sw.OpenDoc(str(path), 1)
            if isinstance(opened, tuple):
                opened = opened[0]
            if opened is None:
                opened = getattr(sw, "ActiveDoc", None)
            if opened is None:
                return {"success": False, "message": "SolidWorks OpenDoc returned no gear Part."}
            active_path = str(ActiveModelThroughHoleExecutor._com_member(opened, "GetPathName", default="") or "")
            geometry = cls._verify_geometry(opened, request, operation)
            success = bool(active_path and Path(active_path).resolve() == path.resolve() and geometry.get("success"))
            return {
                "success": success,
                "path": str(path),
                "active_path": active_path,
                "file_size": path.stat().st_size if path.is_file() else 0,
                "geometry_validation": geometry,
                "model": opened,
            }
        except Exception as exc:
            return {"success": False, "path": str(path), "error": repr(exc)}

    @staticmethod
    def _normalize_keyway(raw: Any, bore_diameter_mm: float, root_radius_mm: float) -> dict[str, Any]:
        if raw in (None, False):
            return {"success": True, "keyway": {"enabled": False, "width_mm": 0.0, "depth_mm": 0.0}}
        if raw is True:
            return {"success": False, "message": "keyway=true is ambiguous; provide width_mm and depth_mm."}
        if not isinstance(raw, dict):
            return {"success": False, "message": "keyway must be an object or false."}
        enabled = bool(raw.get("enabled", True))
        if not enabled:
            return {"success": True, "keyway": {"enabled": False, "width_mm": 0.0, "depth_mm": 0.0}}
        if bore_diameter_mm <= 0:
            return {"success": False, "message": "An enabled keyway requires a positive bore_diameter_mm."}
        width = GearSkill._number(raw, "width_mm", "width")
        depth = GearSkill._number(raw, "depth_mm", "depth")
        if width is None or depth is None or width <= 0 or depth <= 0:
            return {"success": False, "message": "keyway requires positive width_mm and depth_mm."}
        if width >= bore_diameter_mm or bore_diameter_mm / 2.0 + depth >= root_radius_mm:
            return {"success": False, "message": "The keyway must fit between the bore and root circle."}
        return {"success": True, "keyway": {"enabled": True, "width_mm": float(width), "depth_mm": float(depth)}}

    @staticmethod
    def _flank_angle(radius: float, base_radius: float, pressure_angle_deg: float, half_tooth_angle: float) -> float:
        ratio = max(1.0, float(radius) / float(base_radius))
        t = math.sqrt(max(0.0, ratio * ratio - 1.0))
        involute = t - math.atan(t)
        alpha = math.radians(float(pressure_angle_deg))
        pitch_involute = math.tan(alpha) - alpha
        return half_tooth_angle + pitch_involute - involute

    @staticmethod
    def _polar(radius: float, angle: float) -> list[float]:
        return [radius * math.cos(angle), radius * math.sin(angle)]

    @classmethod
    def _pattern_instances(cls, model: Any, feature_name: str) -> int | None:
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FeatureByName", feature_name)
        if feature is None:
            return None
        definition = ActiveModelThroughHoleExecutor._com_member(feature, "GetDefinition")
        if definition is None:
            return None
        value = ActiveModelThroughHoleExecutor._com_member(definition, "TotalInstances", default=None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _cylinder_radii_mm(cls, bodies: list[Any]) -> list[float]:
        radii: list[float] = []
        for body in bodies:
            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces") or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            for face in faces:
                surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface")
                if surface is None or not bool(ActiveModelThroughHoleExecutor._com_member(surface, "IsCylinder", default=False)):
                    continue
                values = ActiveModelThroughHoleExecutor._com_member(surface, "CylinderParams")
                if isinstance(values, (tuple, list)) and len(values) >= 7:
                    radii.append(round(abs(float(values[6])) * 1000.0, 6))
        return sorted(set(radii))

    @staticmethod
    def _set_properties(model: Any, request: dict[str, Any], material: str) -> dict[str, Any]:
        values = {
            "GearType": "Spur",
            "GearModuleMM": request["module_mm"],
            "GearTeeth": request["teeth"],
            "PressureAngleDeg": request["pressure_angle_deg"],
            "PitchDiameterMM": request["pitch_diameter_mm"],
            "OutsideDiameterMM": request["outside_diameter_mm"],
            "RootDiameterMM": request["root_diameter_mm"],
            "FaceWidthMM": request["face_width_mm"],
            "BoreDiameterMM": request["bore_diameter_mm"],
            "BacklashMM": request["backlash_mm"],
        }
        if material:
            values["Material"] = material
        written: list[str] = []
        errors: dict[str, str] = {}
        manager = model.Extension.CustomPropertyManager("")
        for name, value in values.items():
            try:
                manager.Add3(str(name), 30, str(value), 1)
                written.append(str(name))
            except Exception as exc:
                errors[str(name)] = repr(exc)
        return {"requested_material": material, "properties_written": written, "errors": errors}

    @staticmethod
    def _validate_new_part(model: Any) -> dict[str, Any]:
        if model is None:
            return {"success": False, "message": "SolidWorks did not create a Part document."}
        doc_type = int(ActiveModelThroughHoleExecutor._com_member(model, "GetType", default=0) or 0)
        title = str(ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="") or "")
        if doc_type != 1:
            return {"success": False, "message": f"Created document is not a Part: {title}", "active_doc": title}
        return {"success": True, "active_doc": title}

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
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "spur_gear")).strip("_")
        return stem[:80] or "spur_gear"

    @staticmethod
    def _m(value_mm: float) -> float:
        return float(value_mm) / 1000.0

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
