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
from .feature_management_skill import FeatureManagementSkill
from .models import SkillResult
from .revolve_skill import RevolveSkill


class SheetMetalSkill:
    """Create a rectangular native sheet-metal Part with bounded bend features."""

    FEATURE_TYPE = "sheet_metal"
    EDGE_NAMES = {
        "length_positive",
        "length_negative",
        "width_positive",
        "width_negative",
    }
    HEM_TYPES = {"open": 0}
    HEM_POSITIONS = {"inside": 0, "outside": 1}
    JOG_DIMENSION_POSITIONS = {"inside_offset": 1, "outside_offset": 2, "overall": 3}
    JOG_POSITIONS = {"bend_centerline": 1, "material_inside": 2, "material_outside": 3, "bend_outside": 4}
    LOFT_THICKNESS_DIRECTIONS = {"outside": 0, "inside": 1}
    LOFT_FACET_CHORD_TOLERANCE = 0
    LOFT_FACET_BENDS_PER_TRANSITION = 1
    HELIX_AXIS_PLANES = {
        "x": "Right Plane",
        "y": "Top Plane",
        "z": "Front Plane",
    }
    SW_FM_FORM_TOOL_INSTANCE = 73
    SW_SEL_FACES = 2
    SW_EXPORT_TO_DWG_SHEET_METAL = 1
    DXF_FLAT_GEOMETRY = 1
    DXF_BEND_LINES = 4
    DXF_SKETCHES = 8
    DXF_LIBRARY_FEATURES = 32
    DXF_FORMING_TOOLS = 64
    DXF_BOUNDING_BOX = 2048
    FORMING_TOOL_ALIASES = {
        "dimple": "dimple",
        "emboss_dimple": "dimple",
        "round_dimple": "dimple",
    }
    FORMING_TOOL_LIBRARY_FILES = {
        "dimple": ("embosses", "dimple.sldftp"),
    }
    FORMING_TOOL_MIN_EDGE_CLEARANCE_MM = {
        "dimple": 25.0,
    }
    SW_COMMAND_JOG = 372
    SW_COMMAND_PM_OK = -2
    SW_COMMAND_PM_CANCEL = -1

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == self.FEATURE_TYPE]
        if len(features) != 1:
            return SkillResult(False, "A sheet-metal task requires exactly one sheet_metal feature.")

        run_dir = self.output_root / f"sheet_metal_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "sheet_metal_report.json"
        try:
            request = self.normalize_request(features[0].get("params", {}), plan.get("task_type"))
            if not request.get("success"):
                return self._result(False, str(request.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "invalid_request": request,
                })

            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document

            sw, _active = connect_solidworks(visible=True)
            model = new_document(sw, "part")
            validation = RevolveSkill._validate_active_part(model, require_body=False)
            if not validation.get("success"):
                return self._result(False, str(validation.get("message")), report_path, validation)

            module = self._solidworks_module(sw)
            feature_manager = module.IFeatureManager(model.FeatureManager._oleobj_)
            operation = self._create_sheet_metal(sw, model, feature_manager, features[0], request)
            if not operation.get("success"):
                return self._result(False, str(operation.get("message")), report_path, {
                    "active_doc": validation.get("active_doc"),
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            model.ForceRebuild3(False)
            body = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
            geometry = self._verify_geometry(model, request, body, operation)
            operation["geometry_validation"] = geometry
            operation["bbox_after_m"] = body.get("bbox", {})
            operation["body_count_after"] = body.get("body_count", 0)
            if not geometry.get("success"):
                return self._result(False, str(geometry.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            flat_pattern = self._apply_flat_pattern_state(model, request)
            operation["flat_pattern"] = flat_pattern
            if not flat_pattern.get("success"):
                return self._result(False, str(flat_pattern.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            material = str(plan.get("parameters", {}).get("material") or "").strip()
            material_metadata = RevolveSkill._set_material_metadata(model, material)
            requested_path = str(plan.get("execution_model_path") or "").strip()
            part_path = (
                Path(requested_path) if requested_path else run_dir / "sheet_metal_part.SLDPRT"
            ).resolve()
            part_path.parent.mkdir(parents=True, exist_ok=True)
            if not save_document(model, str(part_path)) or not part_path.is_file() or part_path.stat().st_size <= 0:
                return self._result(False, f"SolidWorks failed to save sheet-metal Part: {part_path}", report_path, {
                    "operations": [operation],
                })

            reopen = RevolveSkill._verify_reopen(sw, model, part_path)
            if not reopen.get("success"):
                return self._result(False, f"Saved sheet-metal Part could not be reopened: {part_path}", report_path, {
                    "operations": [operation],
                    "reopen_validation": reopen,
                    "files": [str(part_path)],
                })
            model = reopen.pop("model")
            reopened_flat_pattern = self._inspect_flat_pattern_state(model, request)
            if not reopened_flat_pattern.get("success"):
                return self._result(False, str(reopened_flat_pattern.get("message")), report_path, {
                    "operations": [operation],
                    "reopen_validation": reopen,
                    "flat_pattern_validation": reopened_flat_pattern,
                    "files": [str(part_path)],
                })
            reopened_forming_tools = self._inspect_reopened_forming_tools(model, request)
            if not reopened_forming_tools.get("success"):
                return self._result(False, str(reopened_forming_tools.get("message")), report_path, {
                    "operations": [operation],
                    "reopen_validation": reopen,
                    "flat_pattern_validation": reopened_flat_pattern,
                    "forming_tool_validation": reopened_forming_tools,
                    "files": [str(part_path)],
                })
            files = [str(part_path)]
            dxf_export = {
                "success": True,
                "requested": False,
                "message": "Flat-pattern DXF export was not requested.",
            }
            if request["flat_pattern"]["export_dxf"]["enabled"]:
                requested_name = request["flat_pattern"]["export_dxf"]["file_name"]
                dxf_path = part_path.with_name(requested_name or f"{part_path.stem}_FlatPattern.dxf")
                dxf_export = self._export_flat_pattern_dxf(
                    sw,
                    model,
                    part_path,
                    dxf_path,
                    request["flat_pattern"]["export_dxf"],
                )
                if not dxf_export.get("success"):
                    return self._result(False, str(dxf_export.get("message")), report_path, {
                        "operations": [operation],
                        "reopen_validation": reopen,
                        "flat_pattern_validation": reopened_flat_pattern,
                        "forming_tool_validation": reopened_forming_tools,
                        "flat_pattern_dxf": dxf_export,
                        "files": files,
                    })
                files.append(str(dxf_path))
            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": "new_model",
                "feature_type": self.FEATURE_TYPE,
                "feature_created": True,
                "features_created": (
                    1
                    + len(request["edge_flanges"])
                    + len(request["sketched_bends"])
                    + len(request["hems"])
                    + len(request["jogs"])
                    + len(request["forming_tools"])
                ),
                "operations": [operation],
                "material": material,
                "material_metadata": material_metadata,
                "reopen_validation": reopen,
                "flat_pattern_validation": reopened_flat_pattern,
                "forming_tool_validation": reopened_forming_tools,
                "flat_pattern_dxf": dxf_export,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "saved_by_this_skill": True,
                "side_effects": {
                    "modifies_active_doc": False,
                    "creates_new_doc": True,
                    "exports_files": request["flat_pattern"]["export_dxf"]["enabled"],
                    "uses_template": False,
                },
                "files": files,
            }
            message = "Native SolidWorks sheet-metal Part created and verified."
            if request["flat_pattern"]["export_dxf"]["enabled"]:
                message = "Native SolidWorks sheet-metal Part and flat-pattern DXF created and verified."
            return self._result(True, message, report_path, data)
        except Exception as exc:
            return self._result(False, f"sheet_metal failed: {exc}", report_path, {
                "feature_type": self.FEATURE_TYPE,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        mode = str(params.get("mode") or "new_model").strip().lower()
        if mode != "new_model":
            return {"success": False, "message": "sheet_metal currently supports new_model mode only."}
        if task_type == "modify_3d":
            return {"success": False, "message": "modify_3d cannot create a new sheet-metal Part."}
        raw_lofted_bend = params.get("lofted_bend", params.get("sheet_metal_lofted_bend"))
        if raw_lofted_bend is not None:
            return cls._normalize_lofted_bend_request(params, raw_lofted_bend)
        base_plane = str(params.get("base_plane") or params.get("plane") or "top").strip().lower()
        if base_plane not in {"top", "top plane"}:
            return {"success": False, "message": "sheet_metal base_plane must be top."}

        length = cls._number(params, "length_mm", "length")
        width = cls._number(params, "width_mm", "width")
        thickness = cls._number(params, "thickness_mm", "thickness")
        radius = cls._number(params, "bend_radius_mm", "bend_radius", "radius_mm", "radius")
        if length is None or width is None or thickness is None or min(length, width, thickness) <= 0:
            return {"success": False, "message": "sheet_metal requires positive length_mm, width_mm, and thickness_mm."}
        radius = float(radius) if radius is not None else float(thickness)
        if radius <= 0 or radius > min(length, width) / 4.0:
            return {"success": False, "message": "sheet_metal bend_radius_mm is outside the supported range."}

        raw_flanges = params.get("edge_flanges", [])
        if raw_flanges is None:
            raw_flanges = []
        if not isinstance(raw_flanges, list):
            return {"success": False, "message": "sheet_metal edge_flanges must be a list."}
        flanges: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_flanges):
            if not isinstance(item, dict):
                return {"success": False, "message": f"edge flange {index + 1} must be an object."}
            edge = str(item.get("edge") or item.get("side") or "").strip().lower()
            if edge not in cls.EDGE_NAMES or edge in seen:
                return {"success": False, "message": f"edge flange {index + 1} has an invalid or duplicate edge."}
            height = cls._number(item, "height_mm", "height", "length_mm", "length")
            angle = cls._number(item, "angle_deg", "angle")
            angle = 90.0 if angle is None else float(angle)
            if height is None or height <= 0:
                return {"success": False, "message": f"edge flange {index + 1} requires height_mm > 0."}
            if angle < 30.0 or angle > 150.0:
                return {"success": False, "message": "edge flange angle_deg must be between 30 and 150 degrees."}
            flip_direction = item.get("flip_direction", item.get("reverse", False))
            if not isinstance(flip_direction, bool):
                return {"success": False, "message": f"edge flange {index + 1} flip_direction must be a JSON boolean."}
            seen.add(edge)
            flanges.append({
                "edge": edge,
                "height_mm": float(height),
                "angle_deg": angle,
                "flip_direction": flip_direction,
            })

        raw_bends = params.get("sketched_bends", params.get("sketch_bends", []))
        if raw_bends is None:
            raw_bends = []
        if not isinstance(raw_bends, list):
            return {"success": False, "message": "sheet_metal sketched_bends must be a list."}
        if len(raw_bends) > 1:
            return {"success": False, "message": "The current production contract supports one sketched bend per Part."}
        if raw_bends and flanges:
            return {"success": False, "message": "Sketched bends and edge flanges cannot be combined in the current contract."}

        bends: list[dict[str, Any]] = []
        for index, item in enumerate(raw_bends):
            if not isinstance(item, dict):
                return {"success": False, "message": f"sketched bend {index + 1} must be an object."}
            orientation_raw = str(
                item.get("line_orientation") or item.get("line_axis") or item.get("orientation") or ""
            ).strip().lower().replace("-", "_")
            orientation = {
                "width": "parallel_to_width",
                "along_width": "parallel_to_width",
                "parallel_to_width": "parallel_to_width",
                "length": "parallel_to_length",
                "along_length": "parallel_to_length",
                "parallel_to_length": "parallel_to_length",
            }.get(orientation_raw)
            if orientation is None:
                return {
                    "success": False,
                    "message": "sketched bend line_orientation must be parallel_to_width or parallel_to_length.",
                }
            offset = cls._number(item, "offset_mm", "center_offset_mm", "position_mm")
            if offset is None:
                return {"success": False, "message": f"sketched bend {index + 1} requires offset_mm from the base center."}
            angle = cls._number(item, "angle_deg", "angle")
            angle = 90.0 if angle is None else float(angle)
            bend_radius = cls._number(item, "bend_radius_mm", "radius_mm", "radius")
            bend_radius = radius if bend_radius is None else float(bend_radius)
            if angle < 30.0 or angle > 150.0:
                return {"success": False, "message": "sketched bend angle_deg must be between 30 and 150 degrees."}
            if bend_radius <= 0 or bend_radius > min(length, width) / 4.0:
                return {"success": False, "message": "sketched bend bend_radius_mm is outside the supported range."}
            reverse_direction = item.get("reverse_direction", item.get("reverse", False))
            if not isinstance(reverse_direction, bool):
                return {"success": False, "message": f"sketched bend {index + 1} reverse_direction must be a JSON boolean."}
            fixed_side = str(item.get("fixed_side") or "negative").strip().lower()
            if fixed_side not in {"negative", "positive"}:
                return {"success": False, "message": "sketched bend fixed_side must be negative or positive."}
            perpendicular_span = length if orientation == "parallel_to_width" else width
            minimum_leg = max(2.0 * thickness + bend_radius, 5.0)
            if abs(float(offset)) >= perpendicular_span / 2.0 - minimum_leg:
                return {
                    "success": False,
                    "message": f"sketched bend {index + 1} offset_mm leaves an unsupported bend leg.",
                }
            bends.append({
                "line_orientation": orientation,
                "offset_mm": float(offset),
                "angle_deg": angle,
                "bend_radius_mm": bend_radius,
                "reverse_direction": reverse_direction,
                "fixed_side": fixed_side,
            })

        raw_hems = params.get("hems", params.get("sheet_metal_hems", []))
        if raw_hems is None:
            raw_hems = []
        if not isinstance(raw_hems, list):
            return {"success": False, "message": "sheet_metal hems must be a list."}
        if len(raw_hems) > 1:
            return {"success": False, "message": "The current production contract supports one hem per Part."}
        if raw_hems and (flanges or bends):
            return {
                "success": False,
                "message": "A hem cannot be combined with edge flanges or sketched bends in the current contract.",
            }

        hems: list[dict[str, Any]] = []
        for index, item in enumerate(raw_hems):
            if not isinstance(item, dict):
                return {"success": False, "message": f"hem {index + 1} must be an object."}
            edge = str(item.get("edge") or item.get("side") or "").strip().lower()
            if edge not in cls.EDGE_NAMES:
                return {"success": False, "message": f"hem {index + 1} has an invalid edge."}
            hem_type = str(item.get("type") or item.get("hem_type") or "open").strip().lower().replace("-", "_")
            if hem_type not in cls.HEM_TYPES:
                return {
                    "success": False,
                    "message": "The current production contract supports open hems only.",
                }
            position = str(item.get("position") or item.get("bend_position") or "outside").strip().lower()
            if position not in cls.HEM_POSITIONS:
                return {"success": False, "message": "hem position must be inside or outside."}
            hem_length = cls._number(item, "length_mm", "length", "hem_length_mm")
            gap = cls._number(item, "gap_mm", "gap", "gap_distance_mm")
            miter_gap = cls._number(item, "miter_gap_mm", "miter_gap")
            hem_radius = cls._number(item, "bend_radius_mm", "radius_mm", "radius")
            if hem_length is None or gap is None:
                return {"success": False, "message": f"hem {index + 1} requires length_mm and gap_mm."}
            hem_length = float(hem_length)
            gap = float(gap)
            hem_radius = radius if hem_radius is None else float(hem_radius)
            miter_gap = max(thickness * 0.5, 0.5) if miter_gap is None else float(miter_gap)
            minimum_length = max(2.0 * hem_radius + thickness + gap, 3.0 * thickness)
            perpendicular_span = width if edge.startswith("width_") else length
            if hem_length < minimum_length:
                return {
                    "success": False,
                    "message": f"hem {index + 1} length_mm is too short for the requested radius, gap, and thickness.",
                }
            if hem_length > perpendicular_span * 0.45:
                return {"success": False, "message": f"hem {index + 1} length_mm exceeds the supported base span."}
            if gap <= 0 or gap > thickness * 3.0:
                return {"success": False, "message": f"hem {index + 1} gap_mm is outside the supported range."}
            if hem_radius <= 0 or hem_radius > min(length, width) / 4.0:
                return {"success": False, "message": f"hem {index + 1} bend_radius_mm is outside the supported range."}
            if miter_gap < 0 or miter_gap > hem_length / 4.0:
                return {"success": False, "message": f"hem {index + 1} miter_gap_mm is outside the supported range."}
            reverse_direction = item.get("reverse_direction", item.get("reverse", False))
            if not isinstance(reverse_direction, bool):
                return {"success": False, "message": f"hem {index + 1} reverse_direction must be a JSON boolean."}
            hems.append({
                "edge": edge,
                "type": hem_type,
                "position": position,
                "length_mm": hem_length,
                "gap_mm": gap,
                "bend_radius_mm": hem_radius,
                "miter_gap_mm": miter_gap,
                "reverse_direction": reverse_direction,
            })

        raw_jogs = params.get("jogs", params.get("sheet_metal_jogs", []))
        if raw_jogs is None:
            raw_jogs = []
        if not isinstance(raw_jogs, list):
            return {"success": False, "message": "sheet_metal jogs must be a list."}
        if len(raw_jogs) > 1:
            return {"success": False, "message": "The current production contract supports one jog per Part."}
        if raw_jogs and (flanges or bends or hems):
            return {
                "success": False,
                "message": "A jog cannot be combined with edge flanges, sketched bends, or hems in the current contract.",
            }

        jogs: list[dict[str, Any]] = []
        for index, item in enumerate(raw_jogs):
            if not isinstance(item, dict):
                return {"success": False, "message": f"jog {index + 1} must be an object."}
            orientation_raw = str(
                item.get("line_orientation") or item.get("line_axis") or item.get("orientation") or ""
            ).strip().lower().replace("-", "_")
            orientation = {
                "width": "parallel_to_width",
                "along_width": "parallel_to_width",
                "parallel_to_width": "parallel_to_width",
                "length": "parallel_to_length",
                "along_length": "parallel_to_length",
                "parallel_to_length": "parallel_to_length",
            }.get(orientation_raw)
            if orientation is None:
                return {
                    "success": False,
                    "message": "jog line_orientation must be parallel_to_width or parallel_to_length.",
                }
            line_offset = cls._number(item, "line_offset_mm", "offset_mm", "center_offset_mm", "position_mm")
            offset_distance = cls._number(item, "offset_distance_mm", "jog_height_mm", "height_mm")
            angle = cls._number(item, "angle_deg", "jog_angle_deg", "angle")
            jog_radius = cls._number(item, "bend_radius_mm", "radius_mm", "radius")
            if line_offset is None or offset_distance is None:
                return {
                    "success": False,
                    "message": f"jog {index + 1} requires line_offset_mm and offset_distance_mm.",
                }
            angle = 90.0 if angle is None else float(angle)
            jog_radius = radius if jog_radius is None else float(jog_radius)
            if angle < 30.0 or angle > 150.0:
                return {"success": False, "message": "jog angle_deg must be between 30 and 150 degrees."}
            if jog_radius <= 0 or jog_radius > min(length, width) / 4.0:
                return {"success": False, "message": "jog bend_radius_mm is outside the supported range."}
            offset_distance = float(offset_distance)
            if offset_distance <= 0 or offset_distance > min(length, width) / 4.0:
                return {"success": False, "message": "jog offset_distance_mm is outside the supported range."}
            fixed_side = str(item.get("fixed_side") or "negative").strip().lower()
            if fixed_side not in {"negative", "positive"}:
                return {"success": False, "message": "jog fixed_side must be negative or positive."}
            reverse_direction = item.get("reverse_direction", item.get("reverse", False))
            fix_projected_length = item.get("fix_projected_length", True)
            if not isinstance(reverse_direction, bool) or not isinstance(fix_projected_length, bool):
                return {
                    "success": False,
                    "message": "jog reverse_direction and fix_projected_length must be JSON booleans.",
                }
            if not fix_projected_length:
                return {
                    "success": False,
                    "message": "The current production jog contract requires fix_projected_length=true.",
                }
            dimension_raw = str(item.get("dimension_position") or "outside_offset").strip().lower().replace("-", "_")
            dimension_position = {
                "inside": "inside_offset",
                "inside_offset": "inside_offset",
                "outside": "outside_offset",
                "outside_offset": "outside_offset",
                "overall": "overall",
                "overall_position": "overall",
            }.get(dimension_raw)
            if dimension_position is None:
                return {
                    "success": False,
                    "message": "jog dimension_position must be inside_offset, outside_offset, or overall.",
                }
            if dimension_position != "outside_offset":
                return {
                    "success": False,
                    "message": "The current production jog contract supports outside_offset dimensions only.",
                }
            position_raw = str(item.get("jog_position") or "bend_centerline").strip().lower().replace("-", "_")
            jog_position = {
                "centerline": "bend_centerline",
                "bend_centerline": "bend_centerline",
                "material_inside": "material_inside",
                "material_outside": "material_outside",
                "bend_outside": "bend_outside",
            }.get(position_raw)
            if jog_position is None:
                return {
                    "success": False,
                    "message": "jog jog_position must be bend_centerline, material_inside, material_outside, or bend_outside.",
                }
            if jog_position != "bend_centerline":
                return {
                    "success": False,
                    "message": "The current production jog contract supports bend_centerline positioning only.",
                }
            perpendicular_span = length if orientation == "parallel_to_width" else width
            tangent = abs(math.tan(math.radians(angle)))
            transition_run = offset_distance / max(tangent, 0.25)
            minimum_leg = max(2.0 * (jog_radius + thickness) + transition_run, 10.0)
            if abs(float(line_offset)) >= perpendicular_span / 2.0 - minimum_leg:
                return {
                    "success": False,
                    "message": f"jog {index + 1} line_offset_mm leaves an unsupported moving leg.",
                }
            jogs.append({
                "line_orientation": orientation,
                "line_offset_mm": float(line_offset),
                "offset_distance_mm": offset_distance,
                "angle_deg": angle,
                "bend_radius_mm": jog_radius,
                "fixed_side": fixed_side,
                "reverse_direction": reverse_direction,
                "fix_projected_length": fix_projected_length,
                "dimension_position": dimension_position,
                "jog_position": jog_position,
            })

        forming_tools = cls._normalize_forming_tools(params, float(length), float(width), float(thickness))
        if not forming_tools.get("success"):
            return forming_tools
        if forming_tools["items"] and (flanges or bends or hems or jogs):
            return {
                "success": False,
                "message": (
                    "A forming tool cannot be combined with edge flanges, sketched bends, hems, "
                    "or jogs in the current production contract."
                ),
            }

        flat_pattern = cls._normalize_flat_pattern(params)
        if not flat_pattern.get("success"):
            return flat_pattern

        return {
            "success": True,
            "construction": "base_flange",
            "mode": "new_model",
            "base_plane": "Top Plane",
            "length_mm": float(length),
            "width_mm": float(width),
            "thickness_mm": float(thickness),
            "bend_radius_mm": radius,
            "edge_flanges": flanges,
            "sketched_bends": bends,
            "hems": hems,
            "jogs": jogs,
            "forming_tools": forming_tools["items"],
            "lofted_bend": None,
            "flat_pattern": flat_pattern,
        }

    @classmethod
    def _normalize_lofted_bend_request(
        cls,
        params: dict[str, Any],
        raw: Any,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": "sheet_metal lofted_bend must be an object."}
        for key in ("edge_flanges", "sketched_bends", "hems", "jogs", "forming_tools", "forming_tool"):
            if params.get(key):
                return {
                    "success": False,
                    "message": "A lofted bend cannot be combined with base-flange bend features in the current contract.",
                }

        profile_mode_raw = str(raw.get("profile_mode") or "").strip().lower().replace("-", "_")
        profile_mode = {
            "": "parallel_2d",
            "parallel": "parallel_2d",
            "parallel_profiles": "parallel_2d",
            "parallel_2d": "parallel_2d",
            "helical": "coaxial_helices",
            "helix": "coaxial_helices",
            "helical_profiles": "coaxial_helices",
            "coaxial_helix": "coaxial_helices",
            "coaxial_helices": "coaxial_helices",
        }.get(profile_mode_raw)
        if profile_mode is None:
            return {
                "success": False,
                "message": "lofted_bend profile_mode must be parallel_2d or coaxial_helices.",
            }

        thickness = cls._number(params, "thickness_mm", "thickness")
        if thickness is None:
            thickness = cls._number(raw, "thickness_mm", "thickness")
        radius = cls._number(params, "bend_radius_mm", "bend_radius", "radius_mm", "radius")
        if radius is None:
            radius = cls._number(raw, "bend_radius_mm", "bend_radius", "radius_mm", "radius")
        if thickness is None or radius is None:
            return {
                "success": False,
                "message": "lofted_bend requires thickness_mm and bend_radius_mm.",
            }
        thickness = float(thickness)
        radius = float(radius)
        if thickness <= 0 or radius <= 0:
            return {"success": False, "message": "lofted_bend thickness_mm and bend_radius_mm must be positive."}

        if profile_mode == "coaxial_helices":
            return cls._normalize_helical_lofted_bend_request(
                params,
                raw,
                thickness_mm=thickness,
                bend_radius_mm=radius,
            )

        base_plane = str(raw.get("base_plane") or params.get("base_plane") or "front").strip().lower()
        if base_plane not in {"front", "front plane"}:
            return {"success": False, "message": "lofted_bend base_plane must be front."}
        plane_offset = cls._number(raw, "plane_offset_mm", "profile_spacing_mm", "length_mm", "length")
        if plane_offset is None:
            return {
                "success": False,
                "message": "lofted_bend requires thickness_mm, bend_radius_mm, and plane_offset_mm.",
            }
        plane_offset = float(plane_offset)
        if plane_offset < max(5.0 * thickness, 2.0 * radius) or plane_offset > 2000.0:
            return {"success": False, "message": "lofted_bend plane_offset_mm is outside the supported range."}

        first = cls._normalize_loft_profile(
            raw.get("profile_1_points_mm", raw.get("start_profile_points_mm")),
            "profile_1_points_mm",
        )
        if not first.get("success"):
            return first
        second = cls._normalize_loft_profile(
            raw.get("profile_2_points_mm", raw.get("end_profile_points_mm")),
            "profile_2_points_mm",
        )
        if not second.get("success"):
            return second
        first_points = first["points_mm"]
        second_points = second["points_mm"]
        if len(first_points) != len(second_points):
            return {
                "success": False,
                "message": "lofted_bend profiles must contain the same number of correspondence points.",
            }

        min_profile_span = min(
            first["x_span_mm"],
            second["x_span_mm"],
            max(first["y_span_mm"], thickness),
            max(second["y_span_mm"], thickness),
        )
        if thickness >= min_profile_span / 2.0 or radius > min(first["x_span_mm"], second["x_span_mm"]) / 4.0:
            return {"success": False, "message": "lofted_bend thickness or bend radius is too large for the profiles."}

        direction = str(raw.get("thickness_direction") or "outside").strip().lower()
        if direction not in cls.LOFT_THICKNESS_DIRECTIONS:
            return {"success": False, "message": "lofted_bend thickness_direction must be outside or inside."}
        formed = raw.get("formed", False)
        refer_to_endpoint = raw.get("refer_to_endpoint", True)
        if formed is not False:
            return {"success": False, "message": "The current lofted-bend contract supports bent mode only."}
        if not isinstance(refer_to_endpoint, bool):
            return {"success": False, "message": "lofted_bend refer_to_endpoint must be a JSON boolean."}
        facet_option = str(raw.get("facet_option") or "bends_per_transition").strip().lower().replace("-", "_")
        if facet_option not in {"bends_per_transition", "bends_per_transition_segment"}:
            return {"success": False, "message": "lofted_bend facet_option must be bends_per_transition."}
        bends = raw.get("bends_per_transition", raw.get("num_bends", 4))
        if isinstance(bends, bool) or not isinstance(bends, (int, float)) or int(bends) != float(bends):
            return {"success": False, "message": "lofted_bend bends_per_transition must be an integer."}
        bends = int(bends)
        if bends < 2 or bends > 20:
            return {"success": False, "message": "lofted_bend bends_per_transition must be between 2 and 20."}

        flat_pattern = cls._normalize_flat_pattern(params)
        if not flat_pattern.get("success"):
            return flat_pattern
        return {
            "success": True,
            "construction": "lofted_bend",
            "mode": "new_model",
            "base_plane": "Front Plane",
            "length_mm": max(first["x_span_mm"], second["x_span_mm"]),
            "width_mm": max(first["y_span_mm"], second["y_span_mm"]),
            "thickness_mm": thickness,
            "bend_radius_mm": radius,
            "edge_flanges": [],
            "sketched_bends": [],
            "hems": [],
            "jogs": [],
            "forming_tools": [],
            "lofted_bend": {
                "profile_mode": "parallel_2d",
                "plane_offset_mm": plane_offset,
                "profile_1_points_mm": first_points,
                "profile_2_points_mm": second_points,
                "thickness_direction": direction,
                "formed": False,
                "refer_to_endpoint": refer_to_endpoint,
                "facet_option": "bends_per_transition",
                "bends_per_transition": bends,
            },
            "flat_pattern": flat_pattern,
        }

    @classmethod
    def _normalize_helical_lofted_bend_request(
        cls,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        thickness_mm: float,
        bend_radius_mm: float,
    ) -> dict[str, Any]:
        axis = str(raw.get("axis") or "").strip().lower()
        if axis not in cls.HELIX_AXIS_PLANES:
            return {"success": False, "message": "coaxial_helices requires axis x, y, or z."}

        plane_aliases = {
            "front": "Front Plane",
            "front plane": "Front Plane",
            "top": "Top Plane",
            "top plane": "Top Plane",
            "right": "Right Plane",
            "right plane": "Right Plane",
        }
        requested_plane = str(raw.get("base_plane") or params.get("base_plane") or "").strip().lower()
        base_plane = plane_aliases.get(requested_plane) if requested_plane else cls.HELIX_AXIS_PLANES[axis]
        if base_plane is None:
            return {"success": False, "message": "coaxial_helices base_plane is invalid."}
        if base_plane != cls.HELIX_AXIS_PLANES[axis]:
            return {
                "success": False,
                "message": f"coaxial_helices axis={axis} requires base_plane={cls.HELIX_AXIS_PLANES[axis]}.",
            }

        inner_radius = cls._number(raw, "inner_radius_mm", "profile_1_radius_mm", "radius_1_mm")
        outer_radius = cls._number(raw, "outer_radius_mm", "profile_2_radius_mm", "radius_2_mm")
        height = cls._number(raw, "height_mm", "axial_height_mm")
        pitch = cls._number(raw, "pitch_mm", "helix_pitch_mm")
        revolutions = cls._number(raw, "revolutions", "revolution")
        if None in {inner_radius, outer_radius, height, pitch, revolutions}:
            return {
                "success": False,
                "message": (
                    "coaxial_helices requires inner_radius_mm, outer_radius_mm, "
                    "height_mm, pitch_mm, and revolutions."
                ),
            }
        inner_radius = float(inner_radius)
        outer_radius = float(outer_radius)
        height = float(height)
        pitch = float(pitch)
        revolutions = float(revolutions)
        if min(inner_radius, outer_radius, height, pitch, revolutions) <= 0:
            return {"success": False, "message": "coaxial_helices dimensions must be positive."}
        if outer_radius <= inner_radius:
            return {"success": False, "message": "outer_radius_mm must be greater than inner_radius_mm."}
        if outer_radius - inner_radius <= thickness_mm:
            return {
                "success": False,
                "message": "The radial span between helical profiles must exceed sheet thickness.",
            }
        if max(inner_radius, outer_radius, height, pitch) > 2000.0 or revolutions > 100.0:
            return {"success": False, "message": "coaxial_helices parameters exceed the supported range."}
        expected_height = pitch * revolutions
        if not math.isclose(height, expected_height, rel_tol=1e-6, abs_tol=0.01):
            return {
                "success": False,
                "message": "coaxial_helices height_mm conflicts with pitch_mm * revolutions.",
            }

        center_uv = raw.get("center_uv_mm")
        if (
            not isinstance(center_uv, (tuple, list))
            or len(center_uv) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in center_uv)
        ):
            return {"success": False, "message": "coaxial_helices requires numeric center_uv_mm=[u, v]."}
        center_uv = [float(center_uv[0]), float(center_uv[1])]
        if not all(math.isfinite(value) for value in center_uv):
            return {"success": False, "message": "coaxial_helices center_uv_mm values must be finite."}

        clockwise = raw.get("clockwise")
        reverse_direction = raw.get("reverse_direction")
        formed = raw.get("formed")
        refer_to_endpoint = raw.get("refer_to_endpoint")
        booleans = {
            "clockwise": clockwise,
            "reverse_direction": reverse_direction,
            "formed": formed,
            "refer_to_endpoint": refer_to_endpoint,
        }
        for name, value in booleans.items():
            if not isinstance(value, bool):
                return {"success": False, "message": f"coaxial_helices {name} must be a JSON boolean."}
        if not formed:
            return {
                "success": False,
                "message": "coaxial_helices requires formed=true; faceted bent helices are not supported.",
            }
        taper = raw.get("taper")
        if taper is not None and taper is not False:
            return {"success": False, "message": "coaxial_helices does not support tapered helices."}

        start_angle = cls._number(raw, "start_angle_deg", "starting_angle_deg")
        if start_angle is None or not math.isfinite(float(start_angle)):
            return {"success": False, "message": "coaxial_helices requires a finite start_angle_deg."}
        start_angle = float(start_angle)
        if start_angle < -360.0 or start_angle > 360.0:
            return {"success": False, "message": "coaxial_helices start_angle_deg must be between -360 and 360."}

        thickness_direction = str(raw.get("thickness_direction") or "").strip().lower()
        if thickness_direction not in cls.LOFT_THICKNESS_DIRECTIONS:
            return {
                "success": False,
                "message": "coaxial_helices thickness_direction must be outside or inside.",
            }
        facet_option = str(raw.get("facet_option") or "").strip().lower().replace("-", "_")
        if facet_option not in {"chord_tolerance", "maximum_deviation"}:
            return {
                "success": False,
                "message": "coaxial_helices facet_option must be chord_tolerance.",
            }
        chord_tolerance = cls._number(raw, "chord_tolerance_mm", "maximum_deviation_mm")
        if chord_tolerance is None or not 0.01 <= float(chord_tolerance) <= 5.0:
            return {
                "success": False,
                "message": "coaxial_helices chord_tolerance_mm must be between 0.01 and 5.0.",
            }
        bend_lines = raw.get("number_of_bend_lines")
        if isinstance(bend_lines, bool) or not isinstance(bend_lines, (int, float)):
            return {"success": False, "message": "coaxial_helices number_of_bend_lines must be an integer."}
        if int(bend_lines) != float(bend_lines) or not 2 <= int(bend_lines) <= 20:
            return {
                "success": False,
                "message": "coaxial_helices number_of_bend_lines must be between 2 and 20.",
            }

        flat_pattern = cls._normalize_flat_pattern(params)
        if not flat_pattern.get("success"):
            return flat_pattern
        return {
            "success": True,
            "construction": "lofted_bend",
            "mode": "new_model",
            "base_plane": base_plane,
            "length_mm": 2.0 * outer_radius,
            "width_mm": height,
            "thickness_mm": thickness_mm,
            "bend_radius_mm": bend_radius_mm,
            "edge_flanges": [],
            "sketched_bends": [],
            "hems": [],
            "jogs": [],
            "forming_tools": [],
            "lofted_bend": {
                "profile_mode": "coaxial_helices",
                "axis": axis,
                "base_plane": base_plane,
                "center_uv_mm": center_uv,
                "inner_radius_mm": inner_radius,
                "outer_radius_mm": outer_radius,
                "height_mm": height,
                "pitch_mm": pitch,
                "revolutions": revolutions,
                "start_angle_deg": start_angle,
                "clockwise": clockwise,
                "reverse_direction": reverse_direction,
                "taper": False,
                "thickness_direction": thickness_direction,
                "formed": True,
                "refer_to_endpoint": refer_to_endpoint,
                "facet_option": "chord_tolerance",
                "chord_tolerance_mm": float(chord_tolerance),
                "number_of_bend_lines": int(bend_lines),
            },
            "flat_pattern": flat_pattern,
        }

    @staticmethod
    def _normalize_loft_profile(raw: Any, label: str) -> dict[str, Any]:
        if not isinstance(raw, list) or not 3 <= len(raw) <= 8:
            return {"success": False, "message": f"{label} must contain 3 to 8 ordered open-profile points."}
        points: list[list[float]] = []
        for index, point in enumerate(raw):
            if (
                not isinstance(point, (tuple, list))
                or len(point) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in point)
            ):
                return {"success": False, "message": f"{label}[{index}] must be a numeric [x_mm, y_mm] pair."}
            points.append([float(point[0]), float(point[1])])
        if points[0] == points[-1]:
            return {"success": False, "message": f"{label} must be open, not closed."}
        if any(points[index + 1][0] <= points[index][0] for index in range(len(points) - 1)):
            return {"success": False, "message": f"{label} x coordinates must be strictly increasing to prevent a twisted loft."}
        if any(points[index + 1] == points[index] for index in range(len(points) - 1)):
            return {"success": False, "message": f"{label} contains a zero-length segment."}
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        x_span = max(x_values) - min(x_values)
        y_span = max(y_values) - min(y_values)
        if x_span <= 0 or y_span <= 0:
            return {"success": False, "message": f"{label} must have positive width and height."}
        return {
            "success": True,
            "points_mm": points,
            "x_span_mm": x_span,
            "y_span_mm": y_span,
        }

    @classmethod
    def _normalize_forming_tools(
        cls,
        params: dict[str, Any],
        length_mm: float,
        width_mm: float,
        thickness_mm: float,
    ) -> dict[str, Any]:
        raw = params.get("forming_tools")
        if raw is None:
            raw = params.get("forming_tool", [])
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return {"success": False, "message": "sheet_metal forming_tools must be a list or object."}
        if len(raw) > 1:
            return {
                "success": False,
                "message": "The current production contract supports one forming tool per Part.",
            }

        items: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                return {"success": False, "message": f"forming tool {index + 1} must be an object."}
            tool_raw = str(item.get("tool") or item.get("type") or item.get("name") or "").strip().lower()
            tool_key = cls.FORMING_TOOL_ALIASES.get(tool_raw.replace("-", "_").replace(" ", "_"))
            if tool_key is None:
                supported = ", ".join(sorted(cls.FORMING_TOOL_LIBRARY_FILES))
                return {
                    "success": False,
                    "message": f"forming tool {index + 1} is not in the production whitelist: {supported}.",
                }
            position = item.get("position_mm", {})
            if position is None:
                position = {}
            if not isinstance(position, dict):
                return {"success": False, "message": "forming_tool.position_mm must be an object."}
            x = cls._number(item, "position_x_mm", "x_mm")
            y = cls._number(item, "position_y_mm", "y_mm")
            if x is None:
                x = cls._number(position, "x", "x_mm")
            if y is None:
                y = cls._number(position, "y", "y_mm")
            x = 0.0 if x is None else float(x)
            y = 0.0 if y is None else float(y)
            rotation = cls._number(item, "rotation_deg", "angle_deg", "rotation")
            rotation = 0.0 if rotation is None else float(rotation)
            if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(rotation):
                return {"success": False, "message": f"forming tool {index + 1} contains a non-finite value."}

            clearance = cls.FORMING_TOOL_MIN_EDGE_CLEARANCE_MM[tool_key]
            if min(length_mm, width_mm) < 2.0 * clearance:
                return {
                    "success": False,
                    "message": f"forming tool {tool_key} requires a sheet at least {2.0 * clearance:g} mm wide.",
                }
            if abs(x) > length_mm / 2.0 - clearance or abs(y) > width_mm / 2.0 - clearance:
                return {
                    "success": False,
                    "message": f"forming tool {index + 1} violates the {clearance:g} mm edge-clearance guard.",
                }

            link_to_library = item.get("link_to_library", False)
            show_punch = item.get("show_punch", True)
            show_profile = item.get("show_profile", True)
            show_center = item.get("show_center", True)
            booleans = (link_to_library, show_punch, show_profile, show_center)
            if any(not isinstance(value, bool) for value in booleans):
                return {
                    "success": False,
                    "message": "forming-tool link and flat-pattern display options must be JSON booleans.",
                }
            items.append({
                "tool": tool_key,
                "position_x_mm": x,
                "position_y_mm": y,
                "rotation_deg": rotation % 360.0,
                "link_to_library": link_to_library,
                "show_punch": show_punch,
                "show_profile": show_profile,
                "show_center": show_center,
                "minimum_edge_clearance_mm": clearance,
                "sheet_thickness_mm": thickness_mm,
            })
        return {"success": True, "items": items}

    @staticmethod
    def _normalize_flat_pattern(params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("flat_pattern")
        explicit_state = params.get("flat_pattern_state")
        requested = raw is not None or explicit_state is not None
        state: Any = explicit_state
        explicitly_disabled = False
        nested_export: Any = None

        if isinstance(raw, bool):
            requested = raw
            explicitly_disabled = not raw
            if state is None:
                state = "flattened" if raw else "folded"
        elif isinstance(raw, str):
            state = raw
        elif isinstance(raw, dict):
            enabled = raw.get("enabled", True)
            if not isinstance(enabled, bool):
                return {"success": False, "message": "flat_pattern.enabled must be a JSON boolean."}
            requested = enabled
            explicitly_disabled = not enabled
            state = raw.get("final_state", raw.get("state", state))
            nested_export = raw.get("export_dxf")
        elif raw is not None:
            return {"success": False, "message": "flat_pattern must be a boolean, state string, or object."}

        normalized = str(state or "folded").strip().lower().replace("-", "_")
        aliases = {
            "flat": "flattened",
            "unfolded": "flattened",
            "unfold": "flattened",
            "flattened": "flattened",
            "fold": "folded",
            "folded": "folded",
        }
        final_state = aliases.get(normalized)
        if final_state is None:
            return {"success": False, "message": "flat_pattern final state must be folded or flattened."}
        if not requested and final_state == "flattened":
            return {"success": False, "message": "flat_pattern must be enabled before requesting a flattened final state."}

        export_raw = nested_export
        if export_raw is None:
            export_raw = params.get("flat_pattern_dxf", params.get("export_flat_pattern_dxf"))
        export_enabled = False
        export_options: dict[str, Any] = {}
        if isinstance(export_raw, bool):
            export_enabled = export_raw
        elif isinstance(export_raw, dict):
            export_enabled = export_raw.get("enabled", True)
            if not isinstance(export_enabled, bool):
                return {"success": False, "message": "flat_pattern.export_dxf.enabled must be a JSON boolean."}
            export_options = export_raw
        elif export_raw is not None:
            return {"success": False, "message": "flat_pattern.export_dxf must be a boolean or object."}
        if explicitly_disabled and export_enabled:
            return {
                "success": False,
                "message": "flat_pattern cannot be disabled while export_dxf is enabled.",
            }

        option_defaults = {
            "include_bend_lines": True,
            "include_sketches": False,
            "include_library_features": False,
            "include_forming_tools": True,
            "include_bounding_box": False,
        }
        normalized_options: dict[str, bool] = {}
        for key, default in option_defaults.items():
            value = export_options.get(key, default)
            if not isinstance(value, bool):
                return {"success": False, "message": f"flat_pattern.export_dxf.{key} must be a JSON boolean."}
            normalized_options[key] = value

        file_name = str(export_options.get("file_name") or "").strip()
        if file_name:
            candidate = Path(file_name)
            if candidate.name != file_name or candidate.suffix.lower() not in {"", ".dxf"}:
                return {
                    "success": False,
                    "message": "flat_pattern.export_dxf.file_name must be a local .dxf file name without directories.",
                }
            file_name = f"{candidate.stem}.dxf"
        if export_enabled:
            requested = True
        return {
            "success": True,
            "requested": requested,
            "final_state": final_state,
            "export_dxf": {
                "enabled": export_enabled,
                "file_name": file_name,
                **normalized_options,
            },
        }

    def _create_sheet_metal(
        self,
        sw: Any,
        model: Any,
        feature_manager: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        if request.get("construction") == "lofted_bend":
            return self._create_lofted_bend(model, feature_manager, feature, request)
        if not FeatureManagementSkill._select_named_plane(model, request["base_plane"], False):
            return {"success": False, "message": "Could not select Top Plane for the base flange."}
        model.SketchManager.InsertSketch(True)
        rectangle = model.SketchManager.CreateCenterRectangle(
            0.0,
            0.0,
            0.0,
            self._m(request["length_mm"] / 2.0),
            self._m(request["width_mm"] / 2.0),
            0.0,
        )
        model.SketchManager.InsertSketch(True)
        if rectangle is None:
            return {"success": False, "message": "SolidWorks failed to create the sheet-metal base sketch."}

        empty_dispatch = None
        base = feature_manager.InsertSheetMetalBaseFlange2(
            self._m(request["thickness_mm"]),
            False,
            self._m(request["bend_radius_mm"]),
            0.02,
            0.01,
            False,
            0,
            0,
            1,
            empty_dispatch,
            False,
            2,
            0.0001,
            0.0001,
            0.5,
            True,
            False,
            True,
            True,
        )
        if base is None:
            return {"success": False, "message": "SolidWorks InsertSheetMetalBaseFlange2 returned no feature."}
        base_name = str(feature.get("name") or "SheetMetalBase")
        ActiveModelFeatureSkill._name_feature(base, base_name)
        model.ForceRebuild3(False)

        flange_results: list[dict[str, Any]] = []
        for index, flange in enumerate(request["edge_flanges"]):
            result = self._create_edge_flange(model, feature_manager, request, flange, empty_dispatch, index)
            flange_results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "message": str(result.get("message")),
                    "type": self.FEATURE_TYPE,
                    "request": request,
                    "base_feature_name": ActiveModelFeatureSkill._feature_name(base),
                    "edge_flanges": flange_results,
                }

        bend_results: list[dict[str, Any]] = []
        for index, bend in enumerate(request["sketched_bends"]):
            result = self._create_sketched_bend(model, feature_manager, request, bend, index)
            bend_results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "message": str(result.get("message")),
                    "type": self.FEATURE_TYPE,
                    "request": request,
                    "base_feature_name": ActiveModelFeatureSkill._feature_name(base),
                    "edge_flanges": flange_results,
                    "sketched_bends": bend_results,
                }

        hem_results: list[dict[str, Any]] = []
        for index, hem in enumerate(request["hems"]):
            result = self._create_hem(model, feature_manager, request, hem, index)
            hem_results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "message": str(result.get("message")),
                    "type": self.FEATURE_TYPE,
                    "request": request,
                    "base_feature_name": ActiveModelFeatureSkill._feature_name(base),
                    "edge_flanges": flange_results,
                    "sketched_bends": bend_results,
                    "hems": hem_results,
                }

        jog_results: list[dict[str, Any]] = []
        for index, jog in enumerate(request["jogs"]):
            result = self._create_jog(sw, model, request, jog, index)
            jog_results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "message": str(result.get("message")),
                    "type": self.FEATURE_TYPE,
                    "request": request,
                    "base_feature_name": ActiveModelFeatureSkill._feature_name(base),
                    "edge_flanges": flange_results,
                    "sketched_bends": bend_results,
                    "hems": hem_results,
                    "jogs": jog_results,
                }

        forming_tool_results: list[dict[str, Any]] = []
        for index, forming_tool in enumerate(request["forming_tools"]):
            result = self._create_forming_tool(sw, model, feature_manager, request, forming_tool, index)
            forming_tool_results.append(result)
            if not result.get("success"):
                return {
                    "success": False,
                    "message": str(result.get("message")),
                    "type": self.FEATURE_TYPE,
                    "request": request,
                    "base_feature_name": ActiveModelFeatureSkill._feature_name(base),
                    "edge_flanges": flange_results,
                    "sketched_bends": bend_results,
                    "hems": hem_results,
                    "jogs": jog_results,
                    "forming_tools": forming_tool_results,
                }

        return {
            "success": True,
            "name": base_name,
            "type": self.FEATURE_TYPE,
            "mode": "new_model",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(base),
            "base_feature_name": ActiveModelFeatureSkill._feature_name(base),
            "edge_flanges": flange_results,
            "sketched_bends": bend_results,
            "hems": hem_results,
            "jogs": jog_results,
            "forming_tools": forming_tool_results,
        }

    @classmethod
    def _resolve_forming_tool_path(cls, tool_key: str) -> Path | None:
        relative = cls.FORMING_TOOL_LIBRARY_FILES.get(tool_key)
        if relative is None:
            return None
        program_data = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
        solidworks_root = program_data / "SOLIDWORKS"
        candidates = list(solidworks_root.glob(
            str(Path("SOLIDWORKS *") / "design library" / "forming tools" / Path(*relative))
        ))
        candidates = [path.resolve() for path in candidates if path.is_file() and path.stat().st_size > 0]
        return sorted(candidates, key=lambda path: str(path).casefold(), reverse=True)[0] if candidates else None

    @staticmethod
    def _forming_tool_data(module: Any, value: Any) -> Any:
        if value is None:
            return None
        try:
            return module.ILibraryFormToolFeatureData(value._oleobj_)
        except Exception:
            return value

    def _create_forming_tool(
        self,
        sw: Any,
        model: Any,
        feature_manager: Any,
        request: dict[str, Any],
        forming_tool: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        tool_path = self._resolve_forming_tool_path(forming_tool["tool"])
        if tool_path is None:
            return {
                "success": False,
                "message": (
                    f"SolidWorks Design Library forming tool is unavailable: {forming_tool['tool']}. "
                    "Install the SOLIDWORKS forming-tools library before retrying."
                ),
            }

        before = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        if not before.get("success") or int(before.get("body_count", 0)) != 1:
            return {"success": False, "message": "A forming tool requires exactly one sheet-metal solid body."}
        placement_face = self._find_base_top_face(before)
        if placement_face is None:
            return {"success": False, "message": "Could not resolve the planar sheet-metal placement face."}

        bbox_before = before.get("bbox", {})
        x_m = self._m(forming_tool["position_x_mm"])
        z_m = self._m(forming_tool["position_y_mm"])
        top_y = float(bbox_before.get("ymax", 0.0))
        ray_origin_y = top_y + max(0.01, self._m(request["thickness_mm"] * 4.0))
        model.ClearSelection2(True)
        selected = model.Extension.SelectByRay(
            x_m,
            ray_origin_y,
            z_m,
            0.0,
            -1.0,
            0.0,
            0.0001,
            self.SW_SEL_FACES,
            False,
            0,
            0,
        )
        if not selected:
            return {
                "success": False,
                "message": "Could not select the requested forming-tool placement point on the sheet face.",
            }
        pick_point = ActiveModelThroughHoleExecutor._com_member(
            model.SelectionManager,
            "GetSelectionPoint2",
            1,
            -1,
            default=None,
        )
        pick_coordinates = self._coordinates(pick_point)
        pick_point_verified = (
            pick_coordinates is not None
            and abs(pick_coordinates[0] - x_m) <= 0.00025
            and abs(pick_coordinates[2] - z_m) <= 0.00025
        )
        if not pick_point_verified:
            model.ClearSelection2(True)
            return {
                "success": False,
                "message": "SolidWorks selected a forming-tool point that does not match the requested coordinates.",
                "requested_pick_point_m": [x_m, top_y, z_m],
                "actual_pick_point_m": list(pick_coordinates) if pick_coordinates is not None else None,
            }

        module = self._solidworks_module(sw)
        raw_definition = feature_manager.CreateDefinition(self.SW_FM_FORM_TOOL_INSTANCE)
        definition = self._forming_tool_data(module, raw_definition)
        if definition is None:
            model.ClearSelection2(True)
            return {"success": False, "message": "SolidWorks could not create forming-tool feature data."}
        definition.LinkToFormTool = forming_tool["link_to_library"]
        definition.FormToolPath = str(tool_path)
        definition.OverrideDocumentSettings(
            True,
            forming_tool["show_punch"],
            forming_tool["show_profile"],
            forming_tool["show_center"],
        )
        definition.RotationAngle = math.radians(forming_tool["rotation_deg"])
        created = feature_manager.CreateFeature(definition)
        model.ClearSelection2(True)
        if created is None:
            return {"success": False, "message": "SolidWorks failed to create the library forming-tool feature."}

        name = f"FormingTool{index + 1}_{forming_tool['tool'].title()}"
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        after = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        before_faces = sum(int(item.get("face_count", 0)) for item in before.get("body_info", []))
        after_faces = sum(int(item.get("face_count", 0)) for item in after.get("body_info", []))
        bbox_after = after.get("bbox", {})
        normal_span_after_mm = abs(
            float(bbox_after.get("ymax", 0.0)) - float(bbox_after.get("ymin", 0.0))
        ) * 1000.0

        actual_definition = self._forming_tool_data(
            module,
            ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition", default=None),
        )
        actual_path = str(
            ActiveModelThroughHoleExecutor._com_member(actual_definition, "FormToolPath", default="") or ""
        )
        actual_rotation = ActiveModelThroughHoleExecutor._com_member(
            actual_definition,
            "RotationAngle",
            default=None,
        )
        actual_link = ActiveModelThroughHoleExecutor._com_member(
            actual_definition,
            "LinkToFormTool",
            default=None,
        )
        requested_rotation = math.radians(forming_tool["rotation_deg"])
        rotation_delta = (
            abs((float(actual_rotation) - requested_rotation + math.pi) % (2.0 * math.pi) - math.pi)
            if actual_rotation is not None
            else math.inf
        )
        parameter_checks = {
            "tool_path": actual_path.casefold() == str(tool_path).casefold(),
            "rotation": rotation_delta <= math.radians(0.1),
            "library_link": actual_link is not None and bool(actual_link) == forming_tool["link_to_library"],
            "placement_point": pick_point_verified,
        }
        geometry_checks = {
            "single_solid_body": after.get("success") and int(after.get("body_count", 0)) == 1,
            "topology_changed": after_faces > before_faces,
            "formed_normal_span": normal_span_after_mm > request["thickness_mm"] + 0.1,
        }
        success = all(parameter_checks.values()) and all(geometry_checks.values())
        return {
            "success": success,
            "message": (
                "Native library forming tool created and verified."
                if success
                else "Forming-tool parameter or geometry verification failed."
            ),
            **forming_tool,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "feature_type_name": str(
                ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or ""
            ),
            "library_path": str(tool_path),
            "actual_library_path": actual_path,
            "actual_rotation_deg": math.degrees(float(actual_rotation)) if actual_rotation is not None else None,
            "actual_link_to_library": bool(actual_link) if actual_link is not None else None,
            "requested_pick_point_m": [x_m, top_y, z_m],
            "actual_pick_point_m": list(pick_coordinates) if pick_coordinates is not None else None,
            "parameter_checks": parameter_checks,
            "geometry_checks": geometry_checks,
            "face_count_before": before_faces,
            "face_count_after": after_faces,
            "normal_span_after_mm": normal_span_after_mm,
        }

    @staticmethod
    def _coordinates(value: Any) -> tuple[float, float, float] | None:
        if isinstance(value, (tuple, list)) and len(value) >= 3:
            try:
                return float(value[0]), float(value[1]), float(value[2])
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _formed_helical_geometry_checks(
        *,
        actual_profile_count: Any,
        actual_thickness_mm: float | None,
        requested_thickness_mm: float,
        actual_direction: Any,
        thickness_direction: str,
        actual_formed: Any,
        helix_checks: dict[str, dict[str, bool]],
    ) -> dict[str, bool]:
        return {
            "profile_count": int(actual_profile_count or 0) == 2,
            "thickness": actual_thickness_mm is not None
            and abs(actual_thickness_mm - requested_thickness_mm) <= 0.05,
            "thickness_direction": actual_direction is not None
            and bool(actual_direction) == (thickness_direction == "inside"),
            "formed_method": actual_formed is not None and bool(actual_formed) is True,
            "helix_parameters": all(all(values.values()) for values in helix_checks.values()),
        }

    @staticmethod
    def _body_volume_m3(body: Any) -> float | None:
        value = ActiveModelThroughHoleExecutor._com_member(body, "GetVolume", default=None)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        properties = ActiveModelThroughHoleExecutor._com_member(
            body,
            "GetMassProperties",
            1.0,
            default=None,
        )
        if isinstance(properties, (tuple, list)) and len(properties) > 3:
            try:
                value = float(properties[3])
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None
        return None

    def _create_helical_lofted_bend(
        self,
        model: Any,
        feature_manager: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        loft = request["lofted_bend"]
        sketch_manager = model.SketchManager
        circle_features: dict[str, Any] = {}
        helix_features: dict[str, Any] = {}

        for role in ("inner", "outer"):
            radius_mm = float(loft[f"{role}_radius_mm"])
            previous = self._find_feature_by_type(model, "Helix")
            previous_name = ActiveModelFeatureSkill._feature_name(previous) if previous is not None else ""
            model.ClearSelection2(True)
            if not FeatureManagementSkill._select_named_plane(model, loft["base_plane"], False):
                return {
                    "success": False,
                    "message": f"Could not select {loft['base_plane']} for the {role} helix base circle.",
                }
            sketch_manager.InsertSketch(True)
            circle = sketch_manager.CreateCircleByRadius(
                self._m(loft["center_uv_mm"][0]),
                self._m(loft["center_uv_mm"][1]),
                0.0,
                self._m(radius_mm),
            )
            sketch_manager.InsertSketch(True)
            if circle is None:
                return {"success": False, "message": f"SolidWorks failed to create the {role} helix base circle."}
            circle_feature = self._find_feature_by_type(model, "ProfileFeature")
            if circle_feature is None:
                return {"success": False, "message": f"Could not resolve the {role} helix base-circle sketch."}
            ActiveModelFeatureSkill._name_feature(circle_feature, f"HelixBaseCircle_{role.title()}")

            model.ClearSelection2(True)
            if not bool(ActiveModelThroughHoleExecutor._com_member(circle_feature, "Select2", False, 0, default=False)):
                return {"success": False, "message": f"Could not select the {role} helix base-circle sketch."}
            ActiveModelThroughHoleExecutor._com_member(
                model,
                "InsertHelix",
                loft["reverse_direction"],
                loft["clockwise"],
                False,
                False,
                0,
                self._m(loft["height_mm"]),
                self._m(loft["pitch_mm"]),
                loft["revolutions"],
                0.0,
                math.radians(loft["start_angle_deg"]),
            )
            helix_feature = self._find_feature_by_type(model, "Helix")
            current_name = ActiveModelFeatureSkill._feature_name(helix_feature) if helix_feature is not None else ""
            if helix_feature is None or current_name == previous_name:
                return {"success": False, "message": f"SolidWorks failed to create the {role} native helix."}
            ActiveModelFeatureSkill._name_feature(helix_feature, f"HelixGuide_{role.title()}")
            circle_features[role] = circle_feature
            helix_features[role] = helix_feature

        profile_features: dict[str, Any] = {}
        for role in ("outer", "inner"):
            previous = self._find_feature_by_type(model, "3DProfileFeature")
            previous_name = ActiveModelFeatureSkill._feature_name(previous) if previous is not None else ""
            model.ClearSelection2(True)
            sketch_manager.Insert3DSketch(True)
            selected = bool(
                ActiveModelThroughHoleExecutor._com_member(
                    helix_features[role],
                    "Select2",
                    False,
                    0,
                    default=False,
                )
            )
            if not selected:
                sketch_manager.Insert3DSketch(True)
                return {"success": False, "message": f"Could not select the {role} helix inside a 3D sketch."}
            ActiveModelThroughHoleExecutor._com_member(sketch_manager, "SketchUseEdge3", False, False)
            sketch_manager.Insert3DSketch(True)
            profile_feature = self._find_feature_by_type(model, "3DProfileFeature")
            current_name = ActiveModelFeatureSkill._feature_name(profile_feature) if profile_feature is not None else ""
            if profile_feature is None or current_name == previous_name:
                return {
                    "success": False,
                    "message": f"SolidWorks failed to convert the {role} helix into a 3D loft profile.",
                }
            ActiveModelFeatureSkill._name_feature(profile_feature, f"HelicalLoftProfile_{role.title()}")
            profile_features[role] = profile_feature

        model.ClearSelection2(True)
        outer_selected = bool(
            ActiveModelThroughHoleExecutor._com_member(
                profile_features["outer"],
                "Select2",
                False,
                1,
                default=False,
            )
        )
        inner_selected = bool(
            ActiveModelThroughHoleExecutor._com_member(
                profile_features["inner"],
                "Select2",
                True,
                1,
                default=False,
            )
        )
        if not outer_selected or not inner_selected:
            return {"success": False, "message": "Could not select both helical loft profiles with Mark=1."}

        created = feature_manager.InsertSheetMetalLoftedBend2(
            self.LOFT_THICKNESS_DIRECTIONS[loft["thickness_direction"]],
            self._m(request["thickness_mm"]),
            True,
            self._m(request["bend_radius_mm"]),
            loft["refer_to_endpoint"],
            self.LOFT_FACET_CHORD_TOLERANCE,
            self._m(loft["chord_tolerance_mm"]),
            loft["number_of_bend_lines"],
            0.0,
            0.0,
        )
        model.ClearSelection2(True)
        if created is None:
            return {"success": False, "message": "SolidWorks returned no formed helical lofted-bend feature."}
        name = str(feature.get("name") or "HelicalFormedLoftedBend")
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)

        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_profile_count = ActiveModelThroughHoleExecutor._com_member(definition, "GetProfileCount", default=0)
        actual_thickness = ActiveModelThroughHoleExecutor._com_member(definition, "Thickness", default=None)
        actual_direction = ActiveModelThroughHoleExecutor._com_member(definition, "Direction", default=None)
        actual_faceting = ActiveModelThroughHoleExecutor._com_member(definition, "FacetingOption", default=None)
        actual_facet_value = ActiveModelThroughHoleExecutor._com_member(definition, "FacetValue", default=None)
        actual_formed = ActiveModelThroughHoleExecutor._com_member(definition, "FormedMethod", default=None)
        actual_endpoint = ActiveModelThroughHoleExecutor._com_member(definition, "ReferToEndPoint", default=None)
        actual_bend_lines = ActiveModelThroughHoleExecutor._com_member(
            definition,
            "NumberOfBendLines",
            default=None,
        )
        actual_thickness_mm = float(actual_thickness) * 1000.0 if actual_thickness is not None else None
        actual_chord_tolerance_mm = (
            float(actual_facet_value) * 1000.0 if actual_facet_value is not None else None
        )

        helix_checks: dict[str, dict[str, bool]] = {}
        for role, helix_feature in helix_features.items():
            helix_definition = ActiveModelThroughHoleExecutor._com_member(helix_feature, "GetDefinition")
            actual_pitch = ActiveModelThroughHoleExecutor._com_member(helix_definition, "Pitch", default=None)
            actual_height = ActiveModelThroughHoleExecutor._com_member(helix_definition, "Height", default=None)
            actual_revolutions = ActiveModelThroughHoleExecutor._com_member(
                helix_definition,
                "Revolution",
                default=None,
            )
            actual_clockwise = ActiveModelThroughHoleExecutor._com_member(
                helix_definition,
                "Clockwise",
                default=None,
            )
            actual_reverse = ActiveModelThroughHoleExecutor._com_member(
                helix_definition,
                "ReverseDirection",
                default=None,
            )
            helix_checks[role] = {
                "pitch": actual_pitch is not None
                and abs(float(actual_pitch) * 1000.0 - loft["pitch_mm"]) <= 0.01,
                "height": actual_height is not None
                and abs(float(actual_height) * 1000.0 - loft["height_mm"]) <= 0.01,
                "revolutions": actual_revolutions is not None
                and abs(float(actual_revolutions) - loft["revolutions"]) <= 1e-6,
                "clockwise": actual_clockwise is not None
                and bool(actual_clockwise) == loft["clockwise"],
                "reverse_direction": actual_reverse is not None
                and bool(actual_reverse) == loft["reverse_direction"],
            }

        checks = self._formed_helical_geometry_checks(
            actual_profile_count=actual_profile_count,
            actual_thickness_mm=actual_thickness_mm,
            requested_thickness_mm=request["thickness_mm"],
            actual_direction=actual_direction,
            thickness_direction=loft["thickness_direction"],
            actual_formed=actual_formed,
            helix_checks=helix_checks,
        )
        nonoperative_reference_properties = {
            "reason": (
                "SOLIDWORKS ignores faceting, endpoint, chord-tolerance, and bend-count controls "
                "when InsertSheetMetalLoftedBend2 is called with BFormed=true."
            ),
            "requested": {
                "facet_option": loft["facet_option"],
                "chord_tolerance_mm": loft["chord_tolerance_mm"],
                "refer_to_endpoint": loft["refer_to_endpoint"],
                "number_of_bend_lines": loft["number_of_bend_lines"],
            },
            "observed": {
                "faceting_option": int(actual_faceting) if actual_faceting is not None else None,
                "chord_tolerance_mm": actual_chord_tolerance_mm,
                "refer_to_endpoint": bool(actual_endpoint) if actual_endpoint is not None else None,
                "number_of_bend_lines": int(actual_bend_lines) if actual_bend_lines is not None else None,
            },
        }

        for construction in (
            *circle_features.values(),
            *profile_features.values(),
        ):
            self._hide_sketch_feature(model, construction)
        for helix_feature in helix_features.values():
            self._hide_reference_feature(model, helix_feature)

        success = all(checks.values())
        return {
            "success": success,
            "message": (
                "Native formed helical lofted bend verified; nonoperative bend-line properties were recorded."
                if success
                else "Formed helical lofted-bend parameter verification failed."
            ),
            "name": name,
            "type": self.FEATURE_TYPE,
            "mode": "new_model",
            "profile_mode": "coaxial_helices",
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "feature_type_name": str(
                ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or ""
            ),
            "construction_features": {
                "base_circles": [
                    ActiveModelFeatureSkill._feature_name(item) for item in circle_features.values()
                ],
                "helixes": [
                    ActiveModelFeatureSkill._feature_name(item) for item in helix_features.values()
                ],
                "profiles_3d": [
                    ActiveModelFeatureSkill._feature_name(item) for item in profile_features.values()
                ],
            },
            "parameter_checks": checks,
            "helix_parameter_checks": helix_checks,
            "nonoperative_reference_properties": nonoperative_reference_properties,
            "actual_thickness_mm": actual_thickness_mm,
            "actual_chord_tolerance_mm": actual_chord_tolerance_mm,
            "actual_number_of_bend_lines": int(actual_bend_lines) if actual_bend_lines is not None else None,
            "lofted_bends": [
                {
                    "success": success,
                    "profile_mode": "coaxial_helices",
                    "parameter_checks": checks,
                    "feature_name": ActiveModelFeatureSkill._feature_name(created),
                }
            ],
            "request": request,
        }

    @staticmethod
    def _hide_sketch_feature(model: Any, feature: Any | None) -> None:
        if feature is None:
            return
        try:
            model.ClearSelection2(True)
            if bool(ActiveModelThroughHoleExecutor._com_member(feature, "Select2", False, 0, default=False)):
                ActiveModelThroughHoleExecutor._com_member(model, "BlankSketch")
        finally:
            model.ClearSelection2(True)

    @staticmethod
    def _hide_reference_feature(model: Any, feature: Any | None) -> None:
        if feature is None:
            return
        try:
            model.ClearSelection2(True)
            if bool(ActiveModelThroughHoleExecutor._com_member(feature, "Select2", False, 0, default=False)):
                ActiveModelThroughHoleExecutor._com_member(model, "BlankRefGeom")
        finally:
            model.ClearSelection2(True)

    def _create_lofted_bend(
        self,
        model: Any,
        feature_manager: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        loft = request["lofted_bend"]
        if loft.get("profile_mode") == "coaxial_helices":
            return self._create_helical_lofted_bend(model, feature_manager, feature, request)
        if not FeatureManagementSkill._select_named_plane(model, request["base_plane"], False):
            return {"success": False, "message": "Could not select Front Plane for the first lofted-bend profile."}
        first = self._create_open_profile_sketch(model, loft["profile_1_points_mm"], "LoftProfile1")
        if not first.get("success"):
            return first

        model.ClearSelection2(True)
        if not FeatureManagementSkill._select_named_plane(model, request["base_plane"], False):
            return {"success": False, "message": "Could not reselect Front Plane for the loft offset plane."}
        plane = model.FeatureManager.InsertRefPlane(
            8,
            self._m(loft["plane_offset_mm"]),
            0,
            0.0,
            0,
            0.0,
        )
        if plane is None:
            return {"success": False, "message": "SolidWorks failed to create the lofted-bend offset plane."}
        ActiveModelFeatureSkill._name_feature(plane, "LoftProfilePlane")
        model.ClearSelection2(True)
        if not plane.Select2(False, 0):
            return {"success": False, "message": "Could not select the lofted-bend offset plane."}
        second = self._create_open_profile_sketch(model, loft["profile_2_points_mm"], "LoftProfile2")
        if not second.get("success"):
            return second

        first_feature = first["feature"]
        second_feature = second["feature"]
        model.ClearSelection2(True)
        if not first_feature.Select2(False, 1) or not second_feature.Select2(True, 1):
            return {"success": False, "message": "Could not select both lofted-bend profiles with Mark=1."}
        created = feature_manager.InsertSheetMetalLoftedBend2(
            self.LOFT_THICKNESS_DIRECTIONS[loft["thickness_direction"]],
            self._m(request["thickness_mm"]),
            False,
            self._m(request["bend_radius_mm"]),
            loft["refer_to_endpoint"],
            self.LOFT_FACET_BENDS_PER_TRANSITION,
            0.0,
            loft["bends_per_transition"],
            0.0,
            0.0,
        )
        model.ClearSelection2(True)
        if created is None:
            return {"success": False, "message": "SolidWorks InsertSheetMetalLoftedBend2 returned no feature."}
        name = str(feature.get("name") or "LoftedBend")
        ActiveModelFeatureSkill._name_feature(created, name)
        reference_plane_hidden = False
        try:
            model.ClearSelection2(True)
            if plane.Select2(False, 0):
                ActiveModelThroughHoleExecutor._com_member(model, "BlankRefGeom")
                reference_plane_hidden = True
        finally:
            model.ClearSelection2(True)
        model.ForceRebuild3(False)
        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_profile_count = ActiveModelThroughHoleExecutor._com_member(definition, "GetProfileCount", default=0)
        actual_thickness = ActiveModelThroughHoleExecutor._com_member(definition, "Thickness", default=None)
        actual_direction = ActiveModelThroughHoleExecutor._com_member(definition, "Direction", default=None)
        actual_faceting = ActiveModelThroughHoleExecutor._com_member(definition, "FacetingOption", default=None)
        actual_facet_value = ActiveModelThroughHoleExecutor._com_member(definition, "FacetValue", default=None)
        actual_formed = ActiveModelThroughHoleExecutor._com_member(definition, "FormedMethod", default=None)
        actual_endpoint = ActiveModelThroughHoleExecutor._com_member(definition, "ReferToEndPoint", default=None)
        actual_thickness_mm = float(actual_thickness) * 1000.0 if actual_thickness is not None else None
        checks = {
            "profile_count": int(actual_profile_count or 0) == 2,
            "thickness": actual_thickness_mm is not None and abs(actual_thickness_mm - request["thickness_mm"]) <= 0.05,
            "thickness_direction": (
                actual_direction is not None
                and bool(actual_direction) == (loft["thickness_direction"] == "inside")
            ),
            "formed_method": actual_formed is not None and bool(actual_formed) is False,
            "faceting_option": (
                actual_faceting is not None
                and int(actual_faceting) == self.LOFT_FACET_BENDS_PER_TRANSITION
            ),
            "facet_value": (
                actual_facet_value is not None
                and abs(float(actual_facet_value) - loft["bends_per_transition"]) <= 0.01
            ),
            "refer_to_endpoint": (
                actual_endpoint is not None
                and bool(actual_endpoint) == loft["refer_to_endpoint"]
            ),
        }
        success = all(checks.values())
        return {
            "success": success,
            "message": "Native lofted bend verified." if success else "Lofted-bend parameter verification failed.",
            "name": name,
            "type": self.FEATURE_TYPE,
            "mode": "new_model",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "base_feature_name": ActiveModelFeatureSkill._feature_name(created),
            "edge_flanges": [],
            "sketched_bends": [],
            "hems": [],
            "jogs": [],
            "lofted_bends": [{
                "success": success,
                **loft,
                "actual_profile_count": int(actual_profile_count or 0),
                "actual_thickness_mm": actual_thickness_mm,
                "actual_direction": bool(actual_direction) if actual_direction is not None else None,
                "actual_faceting_option": int(actual_faceting) if actual_faceting is not None else None,
                "actual_facet_value": float(actual_facet_value) if actual_facet_value is not None else None,
                "actual_formed_method": bool(actual_formed) if actual_formed is not None else None,
                "actual_refer_to_endpoint": bool(actual_endpoint) if actual_endpoint is not None else None,
                "parameter_checks": checks,
                "profile_feature_names": [first["feature_name"], second["feature_name"]],
                "offset_plane_feature_name": ActiveModelFeatureSkill._feature_name(plane),
                "offset_plane_hidden": reference_plane_hidden,
                "feature_type_name": str(
                    ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or ""
                ),
                "feature_name": ActiveModelFeatureSkill._feature_name(created),
            }],
        }

    @classmethod
    def _create_open_profile_sketch(
        cls,
        model: Any,
        points_mm: list[list[float]],
        name: str,
    ) -> dict[str, Any]:
        model.SketchManager.InsertSketch(True)
        active = ActiveModelThroughHoleExecutor._com_member(model.SketchManager, "ActiveSketch")
        if active is None:
            return {"success": False, "message": f"SolidWorks did not enter sketch mode for {name}."}
        lines = [
            model.SketchManager.CreateLine(
                cls._m(start[0]),
                cls._m(start[1]),
                0.0,
                cls._m(end[0]),
                cls._m(end[1]),
                0.0,
            )
            for start, end in zip(points_mm, points_mm[1:])
        ]
        model.SketchManager.InsertSketch(True)
        if any(line is None for line in lines):
            return {"success": False, "message": f"SolidWorks failed to create all segments for {name}."}
        feature = cls._find_last_profile_feature(model)
        if feature is None:
            return {"success": False, "message": f"Could not resolve the sketch feature for {name}."}
        ActiveModelFeatureSkill._name_feature(feature, name)
        return {
            "success": True,
            "feature": feature,
            "feature_name": ActiveModelFeatureSkill._feature_name(feature),
            "segment_count": len(lines),
        }

    def _create_hem(
        self,
        model: Any,
        feature_manager: Any,
        request: dict[str, Any],
        hem: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        edge = self._find_base_edge(model, request, hem["edge"])
        if edge is None:
            return {"success": False, "message": f"Could not resolve sheet-metal hem edge {hem['edge']!r}."}

        model.ClearSelection2(True)
        try:
            select_data = ActiveModelThroughHoleExecutor._com_member(
                model.SelectionManager,
                "CreateSelectData",
            )
            selected = bool(select_data is not None and edge.Select4(False, select_data))
        except Exception as exc:
            return {"success": False, "message": f"Could not select the hem edge: {exc}"}
        if not selected:
            return {"success": False, "message": f"Could not select hem edge {hem['edge']!r}."}

        allowance = feature_manager.CreateCustomBendAllowance()
        if allowance is None:
            return {"success": False, "message": "SolidWorks could not create a custom bend allowance for the hem."}
        allowance.Type = 2  # swBendAllowanceKFactor in the installed SOLIDWORKS 2025 type library.
        allowance.KFactor = 0.5
        try:
            created = feature_manager.InsertSheetMetalHem2(
                self.HEM_TYPES[hem["type"]],
                self.HEM_POSITIONS[hem["position"]],
                hem["reverse_direction"],
                self._m(hem["length_mm"]),
                self._m(hem["gap_mm"]),
                0.0,
                self._m(hem["bend_radius_mm"]),
                self._m(hem["miter_gap_mm"]),
                allowance,
                True,
                3,  # swSheetMetalReliefObround in the installed 2025 type library.
                0,
                True,
                1.0,
                0.0,
                0.0,
            )
        except Exception as exc:
            model.ClearSelection2(True)
            return {"success": False, "message": f"SolidWorks InsertSheetMetalHem2 failed: {exc}"}
        model.ClearSelection2(True)
        if created is None:
            return {"success": False, "message": "SolidWorks InsertSheetMetalHem2 returned no feature."}

        name = f"Hem{index + 1}_{hem['edge']}"
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_type = ActiveModelThroughHoleExecutor._com_member(definition, "Type", default=None)
        actual_position = ActiveModelThroughHoleExecutor._com_member(definition, "BendPosition", default=None)
        actual_reverse = ActiveModelThroughHoleExecutor._com_member(definition, "ReverseDirection", default=None)
        actual_length = ActiveModelThroughHoleExecutor._com_member(definition, "Length", default=None)
        actual_gap = ActiveModelThroughHoleExecutor._com_member(definition, "GapDistance", default=None)
        actual_miter = ActiveModelThroughHoleExecutor._com_member(definition, "MiterGap", default=None)
        edge_count = ActiveModelThroughHoleExecutor._com_member(definition, "GetEdgesCount", default=0)
        actual_length_mm = float(actual_length) * 1000.0 if actual_length is not None else None
        actual_gap_mm = float(actual_gap) * 1000.0 if actual_gap is not None else None
        actual_miter_mm = float(actual_miter) * 1000.0 if actual_miter is not None else None
        checks = {
            "type": actual_type is not None and int(actual_type) == self.HEM_TYPES[hem["type"]],
            "position": actual_position is not None and int(actual_position) == self.HEM_POSITIONS[hem["position"]],
            "reverse_direction": actual_reverse is not None and bool(actual_reverse) == hem["reverse_direction"],
            "length": actual_length_mm is not None and abs(actual_length_mm - hem["length_mm"]) <= 0.05,
            "gap": actual_gap_mm is not None and abs(actual_gap_mm - hem["gap_mm"]) <= 0.05,
            "edge_count": int(edge_count or 0) == 1,
        }
        success = all(checks.values())
        return {
            "success": success,
            "message": "Native open hem verified." if success else "Hem parameter verification failed.",
            **hem,
            "actual_type": int(actual_type) if actual_type is not None else None,
            "actual_position": int(actual_position) if actual_position is not None else None,
            "actual_reverse_direction": bool(actual_reverse) if actual_reverse is not None else None,
            "actual_length_mm": actual_length_mm,
            "actual_gap_mm": actual_gap_mm,
            "actual_miter_gap_mm": actual_miter_mm,
            "edge_count": int(edge_count or 0),
            "parameter_checks": checks,
            "feature_type_name": str(
                ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or ""
            ),
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    def _create_jog(
        self,
        sw: Any,
        model: Any,
        request: dict[str, Any],
        jog: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        body = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        top_face = self._find_base_top_face(body)
        if top_face is None:
            return {"success": False, "message": "Could not resolve the flat top face for the jog."}

        model.ClearSelection2(True)
        if not ActiveModelFeatureSkill._select_face(model, top_face):
            return {"success": False, "message": "Could not select the sheet-metal face for the jog."}
        model.SketchManager.InsertSketch(True)
        active_sketch = model.SketchManager.ActiveSketch
        if active_sketch is None:
            return {"success": False, "message": "SolidWorks did not enter jog sketch mode."}
        sketch_name = str(ActiveModelThroughHoleExecutor._com_member(active_sketch, "Name", default="") or "")

        bbox = body.get("bbox", {})
        top_y = float(bbox.get("ymax", 0.0))
        offset = self._m(jog["line_offset_mm"])
        if jog["line_orientation"] == "parallel_to_width":
            start_model = (offset, top_y, -self._m(request["width_mm"] / 2.0))
            end_model = (offset, top_y, self._m(request["width_mm"] / 2.0))
        else:
            start_model = (-self._m(request["length_mm"] / 2.0), top_y, offset)
            end_model = (self._m(request["length_mm"] / 2.0), top_y, offset)
        try:
            from .advanced_feature_skill import AdvancedFeatureSkill

            start = AdvancedFeatureSkill._to_sketch_point(model, start_model)
            end = AdvancedFeatureSkill._to_sketch_point(model, end_model)
            line = model.SketchManager.CreateLine(*start, *end)
        except Exception as exc:
            model.SketchManager.InsertSketch(True)
            return {"success": False, "message": f"Could not create the jog line: {exc}"}
        model.SketchManager.InsertSketch(True)
        if line is None or not sketch_name:
            return {"success": False, "message": "SolidWorks did not create the jog line."}

        sketch_feature = self._find_last_profile_feature(model)
        if sketch_feature is None:
            return {"success": False, "message": "Could not resolve the jog sketch feature."}
        fixed_point = self._sketched_bend_fixed_point(
            request,
            {
                "line_orientation": jog["line_orientation"],
                "offset_mm": jog["line_offset_mm"],
                "fixed_side": jog["fixed_side"],
            },
            top_y,
        )
        model.ClearSelection2(True)
        try:
            if not sketch_feature.Select2(False, 0):
                return {"success": False, "message": "Could not select the jog sketch."}
            if not model.Extension.RunCommand(self.SW_COMMAND_JOG, "AI CAD Jog"):
                return {"success": False, "message": "SolidWorks could not start the native Jog command."}
            time.sleep(0.1)
            select_data = ActiveModelThroughHoleExecutor._com_member(
                model.SelectionManager,
                "CreateSelectData",
            )
            if select_data is None:
                sw.RunCommand(self.SW_COMMAND_PM_CANCEL, "")
                return {"success": False, "message": "Could not create fixed-face selection data for the jog."}
            select_data.Mark = 0
            select_data.X = fixed_point[0]
            select_data.Y = fixed_point[1]
            select_data.Z = fixed_point[2]
            if not top_face.Select4(True, select_data):
                sw.RunCommand(self.SW_COMMAND_PM_CANCEL, "")
                return {"success": False, "message": "Could not select the fixed side for the jog."}
            if not sw.RunCommand(self.SW_COMMAND_PM_OK, ""):
                sw.RunCommand(self.SW_COMMAND_PM_CANCEL, "")
                return {"success": False, "message": "SolidWorks could not confirm the native Jog command."}
            time.sleep(0.1)
        except Exception as exc:
            try:
                sw.RunCommand(self.SW_COMMAND_PM_CANCEL, "")
            except Exception:
                pass
            return {"success": False, "message": f"Could not create the native Jog feature: {exc}"}

        model.ClearSelection2(True)
        model.ForceRebuild3(False)
        created = self._find_last_feature_containing(model, "jog")
        if created is None:
            messages = self._solidworks_messages(sw)
            detail = f": {' | '.join(messages)}" if messages else ""
            return {"success": False, "message": f"SolidWorks did not create a native Jog feature{detail}."}

        name = f"Jog{index + 1}"
        ActiveModelFeatureSkill._name_feature(created, name)
        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        if definition is None:
            return {"success": False, "message": "SolidWorks returned no Jog feature definition."}
        module = self._solidworks_module(sw)
        try:
            typed_definition = module.IJogFeatureData(definition._oleobj_)
            typed_feature = module.IFeature(created._oleobj_)
            if not typed_definition.AccessSelections(model, None):
                return {"success": False, "message": "Could not access native Jog selections for parameter update."}
            fixed_face_available = typed_definition.FixedFace is not None
            typed_definition.UseDefaultBendRadius = False
            typed_definition.JogAngle = math.radians(jog["angle_deg"])
            typed_definition.BendRadius = self._m(jog["bend_radius_mm"])
            typed_definition.OffsetDistance = self._m(jog["offset_distance_mm"])
            typed_definition.ReverseDirection = jog["reverse_direction"]
            typed_definition.FixProjectedLength = jog["fix_projected_length"]
            typed_definition.DimensionPositionType = self.JOG_DIMENSION_POSITIONS[jog["dimension_position"]]
            typed_definition.JogPositionType = self.JOG_POSITIONS[jog["jog_position"]]
            modified = bool(typed_feature.ModifyDefinition(definition, model, None))
        except Exception as exc:
            try:
                ActiveModelThroughHoleExecutor._com_member(definition, "ReleaseSelectionAccess", default=None)
            except Exception:
                pass
            return {"success": False, "message": f"Could not apply native Jog parameters: {exc}"}
        if not modified:
            messages = self._solidworks_messages(sw)
            detail = f": {' | '.join(messages)}" if messages else ""
            return {"success": False, "message": f"SolidWorks rejected the requested Jog parameters{detail}."}
        model.ForceRebuild3(False)
        created = self._find_last_feature_containing(model, "jog")
        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_angle = ActiveModelThroughHoleExecutor._com_member(definition, "JogAngle", default=None)
        actual_radius = ActiveModelThroughHoleExecutor._com_member(definition, "BendRadius", default=None)
        actual_offset = ActiveModelThroughHoleExecutor._com_member(definition, "OffsetDistance", default=None)
        actual_reverse = ActiveModelThroughHoleExecutor._com_member(definition, "ReverseDirection", default=None)
        actual_fix_length = ActiveModelThroughHoleExecutor._com_member(definition, "FixProjectedLength", default=None)
        actual_dimension = ActiveModelThroughHoleExecutor._com_member(definition, "DimensionPositionType", default=None)
        actual_position = ActiveModelThroughHoleExecutor._com_member(definition, "JogPositionType", default=None)
        actual_fixed_point = ActiveModelThroughHoleExecutor._com_member(definition, "FixedPoint", default=None)
        actual_angle_deg = math.degrees(float(actual_angle)) if actual_angle is not None else None
        actual_radius_mm = float(actual_radius) * 1000.0 if actual_radius is not None else None
        actual_offset_mm = float(actual_offset) * 1000.0 if actual_offset is not None else None
        checks = {
            "angle": actual_angle_deg is not None and abs(abs(actual_angle_deg) - jog["angle_deg"]) <= 0.25,
            "radius": actual_radius_mm is not None and abs(actual_radius_mm - jog["bend_radius_mm"]) <= 0.05,
            "offset_distance": actual_offset_mm is not None and abs(actual_offset_mm - jog["offset_distance_mm"]) <= 0.05,
            "reverse_direction": actual_reverse is not None and bool(actual_reverse) == jog["reverse_direction"],
            "fix_projected_length": actual_fix_length is not None and bool(actual_fix_length) == jog["fix_projected_length"],
            "dimension_position": actual_dimension is not None and int(actual_dimension) == self.JOG_DIMENSION_POSITIONS[jog["dimension_position"]],
            "jog_position": actual_position is not None and int(actual_position) == self.JOG_POSITIONS[jog["jog_position"]],
            "fixed_face": fixed_face_available,
            "fixed_point": (
                isinstance(actual_fixed_point, (tuple, list))
                and len(actual_fixed_point) >= 3
                and all(abs(float(actual_fixed_point[i]) - fixed_point[i]) <= 1e-6 for i in range(3))
            ),
        }
        success = all(checks.values())
        return {
            "success": success,
            "message": "Native jog verified." if success else "Jog parameter verification failed.",
            **jog,
            "actual_angle_deg": actual_angle_deg,
            "actual_bend_radius_mm": actual_radius_mm,
            "actual_offset_distance_mm": actual_offset_mm,
            "actual_reverse_direction": bool(actual_reverse) if actual_reverse is not None else None,
            "actual_fix_projected_length": bool(actual_fix_length) if actual_fix_length is not None else None,
            "actual_dimension_position": int(actual_dimension) if actual_dimension is not None else None,
            "actual_jog_position": int(actual_position) if actual_position is not None else None,
            "parameter_checks": checks,
            "fixed_point_m": list(fixed_point),
            "actual_fixed_point_m": list(actual_fixed_point) if isinstance(actual_fixed_point, (tuple, list)) else None,
            "sketch_name": sketch_name,
            "feature_type_name": str(
                ActiveModelThroughHoleExecutor._com_member(created, "GetTypeName2", default="") or ""
            ),
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    @staticmethod
    def _solidworks_messages(sw: Any) -> list[str]:
        try:
            result = sw.GetErrorMessages()
        except Exception:
            return []
        if not isinstance(result, (tuple, list)) or len(result) < 2:
            return []
        messages = result[1]
        if not messages:
            return []
        if not isinstance(messages, (tuple, list)):
            messages = (messages,)
        return [str(message).strip() for message in messages if str(message).strip()]

    def _create_sketched_bend(
        self,
        model: Any,
        feature_manager: Any,
        request: dict[str, Any],
        bend: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        body = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        top_face = self._find_base_top_face(body)
        if top_face is None:
            return {"success": False, "message": "Could not resolve the flat top face for the sketched bend."}

        model.ClearSelection2(True)
        if not ActiveModelFeatureSkill._select_face(model, top_face):
            return {"success": False, "message": "Could not select the sheet-metal face for the sketched bend."}
        model.SketchManager.InsertSketch(True)
        active_sketch = model.SketchManager.ActiveSketch
        if active_sketch is None:
            return {"success": False, "message": "SolidWorks did not enter sketched-bend sketch mode."}
        sketch_name = str(ActiveModelThroughHoleExecutor._com_member(active_sketch, "Name", default="") or "")

        bbox = body.get("bbox", {})
        top_y = float(bbox.get("ymax", 0.0))
        offset = self._m(bend["offset_mm"])
        if bend["line_orientation"] == "parallel_to_width":
            start_model = (offset, top_y, -self._m(request["width_mm"] / 2.0))
            end_model = (offset, top_y, self._m(request["width_mm"] / 2.0))
        else:
            start_model = (-self._m(request["length_mm"] / 2.0), top_y, offset)
            end_model = (self._m(request["length_mm"] / 2.0), top_y, offset)
        try:
            from .advanced_feature_skill import AdvancedFeatureSkill

            start = AdvancedFeatureSkill._to_sketch_point(model, start_model)
            end = AdvancedFeatureSkill._to_sketch_point(model, end_model)
            line = model.SketchManager.CreateLine(*start, *end)
        except Exception as exc:
            model.SketchManager.InsertSketch(True)
            return {"success": False, "message": f"Could not create the sketched-bend line: {exc}"}
        model.SketchManager.InsertSketch(True)
        if line is None or not sketch_name:
            return {"success": False, "message": "SolidWorks did not create the sketched-bend line."}

        sketch_feature = self._find_last_profile_feature(model)
        if sketch_feature is None:
            return {"success": False, "message": "Could not resolve the sketched-bend sketch feature."}
        fixed_point = self._sketched_bend_fixed_point(request, bend, top_y)
        model.ClearSelection2(True)
        try:
            selected_sketch = bool(sketch_feature.Select2(False, 0))
            selected_fixed_side = bool(model.Extension.SelectByRay(
                fixed_point[0],
                fixed_point[1] + max(self._m(request["thickness_mm"] * 2.0), 0.01),
                fixed_point[2],
                0.0,
                -1.0,
                0.0,
                max(self._m(request["thickness_mm"] / 2.0), 0.0005),
                2,
                True,
                0,
                0,
            ))
            if not selected_sketch or not selected_fixed_side:
                return {"success": False, "message": "Could not select the sketch and fixed face for the bend."}
        except Exception as exc:
            return {"success": False, "message": f"Could not prepare sketched-bend selections: {exc}"}

        definition = feature_manager.CreateDefinition(35)
        if definition is None:
            return {"success": False, "message": "SolidWorks CreateDefinition(swFmSketchBend) returned no data."}
        definition.BendAngle = math.radians(bend["angle_deg"])
        definition.UseDefaultBendRadius = False
        definition.UseGaugeTable = False
        definition.BendRadius = self._m(bend["bend_radius_mm"])
        definition.ReverseDirection = bend["reverse_direction"]
        definition.UseDefaultBendAllowance = True
        created = feature_manager.CreateFeature(definition)
        if created is None:
            return {"success": False, "message": "SolidWorks failed to create the native sketched bend."}
        name = f"SketchedBend{index + 1}"
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)

        created_definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_angle = ActiveModelThroughHoleExecutor._com_member(created_definition, "BendAngle", default=None)
        actual_radius = ActiveModelThroughHoleExecutor._com_member(created_definition, "BendRadius", default=None)
        actual_angle_deg = math.degrees(float(actual_angle)) if actual_angle is not None else None
        actual_radius_mm = float(actual_radius) * 1000.0 if actual_radius is not None else None
        angle_ok = actual_angle_deg is not None and abs(abs(actual_angle_deg) - bend["angle_deg"]) <= 0.25
        radius_ok = actual_radius_mm is not None and abs(actual_radius_mm - bend["bend_radius_mm"]) <= 0.05
        success = angle_ok and radius_ok
        return {
            "success": success,
            "message": "Native sketched bend verified." if success else "Sketched-bend parameter verification failed.",
            **bend,
            "actual_angle_deg": actual_angle_deg,
            "actual_radius_mm": actual_radius_mm,
            "angle_verified": angle_ok,
            "radius_verified": radius_ok,
            "fixed_point_m": list(fixed_point),
            "sketch_name": sketch_name,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    @classmethod
    def _find_base_top_face(cls, body: dict[str, Any]) -> Any | None:
        bbox = body.get("bbox", {})
        target_y = float(bbox.get("ymax", 0.0))
        tolerance = max(1e-6, float(bbox.get("width", 0.0)) * 0.01)
        best = None
        best_area = -1.0
        for solid in body.get("bodies", []):
            faces = ActiveModelThroughHoleExecutor._com_member(solid, "GetFaces", default=()) or ()
            if not isinstance(faces, (tuple, list)):
                faces = (faces,)
            for face in faces:
                box = ActiveModelThroughHoleExecutor._com_member(face, "GetBox", default=None)
                if not box or len(box) < 6:
                    continue
                ymin, ymax = float(box[1]), float(box[4])
                if abs(ymax - ymin) > tolerance or abs(ymax - target_y) > tolerance:
                    continue
                surface = ActiveModelThroughHoleExecutor._com_member(face, "GetSurface", default=None)
                if surface is not None and ActiveModelThroughHoleExecutor._com_member(surface, "IsPlane", default=True) is False:
                    continue
                area = abs((float(box[3]) - float(box[0])) * (float(box[5]) - float(box[2])))
                if area > best_area:
                    best = face
                    best_area = area
        return best

    @staticmethod
    def _find_last_profile_feature(model: Any) -> Any | None:
        result = None
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None:
            feature_type = str(
                ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""
            )
            if feature_type == "ProfileFeature":
                result = feature
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        return result

    @staticmethod
    def _find_last_feature_containing(model: Any, type_token: str) -> Any | None:
        token = type_token.strip().lower()
        result = None
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None:
            feature_type = str(
                ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""
            ).lower()
            if token in feature_type:
                result = feature
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        return result

    @classmethod
    def _sketched_bend_fixed_point(
        cls,
        request: dict[str, Any],
        bend: dict[str, Any],
        top_y: float,
    ) -> tuple[float, float, float]:
        offset = cls._m(bend["offset_mm"])
        sign = -1.0 if bend["fixed_side"] == "negative" else 1.0
        if bend["line_orientation"] == "parallel_to_width":
            boundary = sign * cls._m(request["length_mm"] / 2.0)
            return ((offset + boundary) / 2.0, top_y, 0.0)
        boundary = sign * cls._m(request["width_mm"] / 2.0)
        return (0.0, top_y, (offset + boundary) / 2.0)

    def _create_edge_flange(
        self,
        model: Any,
        feature_manager: Any,
        request: dict[str, Any],
        flange: dict[str, Any],
        empty_dispatch: Any,
        index: int,
    ) -> dict[str, Any]:
        edge = self._find_base_edge(model, request, flange["edge"])
        if edge is None:
            return {"success": False, "message": f"Could not resolve sheet-metal edge {flange['edge']!r}."}

        angle_rad = math.radians(flange["angle_deg"])
        model.ClearSelection2(True)
        sketch_feature = model.InsertSketchForEdgeFlange(edge, angle_rad, flange["flip_direction"])
        if sketch_feature is None:
            return {"success": False, "message": f"Could not create an edge-flange sketch for {flange['edge']}."}
        try:
            if not sketch_feature.Select2(False, 0):
                return {"success": False, "message": f"Could not select the edge-flange sketch for {flange['edge']}."}
            model.EditSketch()
        except Exception as exc:
            return {"success": False, "message": f"Could not edit the edge-flange sketch: {exc}"}

        active_sketch = model.SketchManager.ActiveSketch
        if active_sketch is None:
            model.InsertSketch2(True)
            return {"success": False, "message": f"No active edge-flange sketch exists for {flange['edge']}."}
        try:
            select_data = ActiveModelThroughHoleExecutor._com_member(
                model.SelectionManager, "CreateSelectData"
            )
            if select_data is None or not edge.Select4(False, select_data) or not model.SketchManager.SketchUseEdge(False):
                model.InsertSketch2(True)
                return {"success": False, "message": f"Could not convert {flange['edge']} into the flange sketch."}
        except Exception as exc:
            model.InsertSketch2(True)
            return {"success": False, "message": f"Could not convert the flange edge: {exc}"}

        converted = ActiveModelThroughHoleExecutor._com_member(active_sketch, "GetSketchSegments", default=()) or ()
        if not isinstance(converted, (tuple, list)):
            converted = (converted,)
        if not converted:
            model.InsertSketch2(True)
            return {"success": False, "message": f"No converted sketch segment exists for {flange['edge']}."}
        base_line = converted[0]
        start = ActiveModelThroughHoleExecutor._com_member(base_line, "GetStartPoint2")
        end = ActiveModelThroughHoleExecutor._com_member(base_line, "GetEndPoint2")
        if start is None or end is None:
            model.InsertSketch2(True)
            return {"success": False, "message": f"Could not read the converted flange edge for {flange['edge']}."}
        sx, sy = float(start.X), float(start.Y)
        ex, ey = float(end.X), float(end.Y)
        height = self._m(flange["height_mm"])
        lines = [
            model.SketchManager.CreateLine(sx, sy, 0.0, sx, sy + height, 0.0),
            model.SketchManager.CreateLine(sx, sy + height, 0.0, ex, ey + height, 0.0),
            model.SketchManager.CreateLine(ex, ey, 0.0, ex, ey + height, 0.0),
        ]
        model.InsertSketch2(True)
        if any(line is None for line in lines):
            return {"success": False, "message": f"Could not create flange profile for {flange['edge']}."}

        created = feature_manager.InsertSheetMetalEdgeFlange(
            edge,
            sketch_feature,
            129,
            angle_rad,
            0.0,
            3,
            self._m(flange["height_mm"]),
            4,
            0.0,
            0.0,
            0.0,
            2,
            empty_dispatch,
        )
        if created is None:
            return {"success": False, "message": f"SolidWorks edge-flange API failed for {flange['edge']}."}
        name = f"EdgeFlange{index + 1}_{flange['edge']}"
        ActiveModelFeatureSkill._name_feature(created, name)
        model.ForceRebuild3(False)
        definition = ActiveModelThroughHoleExecutor._com_member(created, "GetDefinition")
        actual_angle_rad = ActiveModelThroughHoleExecutor._com_member(definition, "BendAngle", default=None)
        actual_angle_deg = math.degrees(float(actual_angle_rad)) if actual_angle_rad is not None else None
        angle_verified = actual_angle_deg is not None and abs(abs(actual_angle_deg) - flange["angle_deg"]) <= 0.25
        return {
            "success": angle_verified,
            "message": (
                "Native edge-flange angle verified."
                if angle_verified
                else f"SolidWorks edge-flange angle does not match {flange['angle_deg']} degrees."
            ),
            "edge": flange["edge"],
            "height_mm": flange["height_mm"],
            "angle_deg": flange["angle_deg"],
            "actual_angle_deg": actual_angle_deg,
            "angle_verified": angle_verified,
            "flip_direction": flange["flip_direction"],
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
        }

    @classmethod
    def _find_base_edge(cls, model: Any, request: dict[str, Any], edge_name: str) -> Any | None:
        body_info = ActiveModelThroughHoleExecutor._body_info(model)
        bodies = body_info.get("bodies", []) if isinstance(body_info, dict) else []
        if not bodies:
            return None
        length = cls._m(request["length_mm"])
        width = cls._m(request["width_mm"])
        tolerance = 5e-5
        candidates: list[tuple[float, Any]] = []
        for body in bodies:
            edges = ActiveModelThroughHoleExecutor._com_member(body, "GetEdges", default=()) or ()
            if not isinstance(edges, (tuple, list)):
                edges = (edges,)
            for edge in edges:
                endpoints = cls._edge_endpoints(edge)
                if endpoints is None:
                    continue
                first, second = endpoints
                if abs(first[1]) > tolerance or abs(second[1]) > tolerance:
                    continue
                dx = abs(second[0] - first[0])
                dz = abs(second[2] - first[2])
                midpoint = tuple((first[i] + second[i]) / 2.0 for i in range(3))
                if edge_name == "width_positive":
                    score = abs(midpoint[2] - width / 2.0) + abs(dx - length) + dz
                elif edge_name == "width_negative":
                    score = abs(midpoint[2] + width / 2.0) + abs(dx - length) + dz
                elif edge_name == "length_positive":
                    score = abs(midpoint[0] - length / 2.0) + abs(dz - width) + dx
                else:
                    score = abs(midpoint[0] + length / 2.0) + abs(dz - width) + dx
                candidates.append((score, edge))
        if not candidates:
            return None
        score, candidate = min(candidates, key=lambda item: item[0])
        return candidate if score <= max(length, width) * 0.02 + tolerance else None

    @staticmethod
    def _edge_endpoints(edge: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
        start = ActiveModelThroughHoleExecutor._com_member(edge, "GetStartVertex")
        end = ActiveModelThroughHoleExecutor._com_member(edge, "GetEndVertex")
        if start is None or end is None:
            return None
        first = ActiveModelThroughHoleExecutor._com_member(start, "GetPoint")
        second = ActiveModelThroughHoleExecutor._com_member(end, "GetPoint")
        if not isinstance(first, (tuple, list)) or not isinstance(second, (tuple, list)):
            return None
        return (
            (float(first[0]), float(first[1]), float(first[2])),
            (float(second[0]), float(second[1]), float(second[2])),
        )

    @classmethod
    def _verify_geometry(
        cls,
        model: Any,
        request: dict[str, Any],
        body: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        if request.get("construction") == "lofted_bend":
            return cls._verify_lofted_bend_geometry(model, request, body, operation)
        if not body.get("success") or int(body.get("body_count", 0)) != 1:
            return {"success": False, "message": "Sheet-metal execution did not produce exactly one solid body."}
        tree = ActiveModelFeatureSkill._feature_tree(model)
        types = {str(item.get("type") or "").lower() for item in tree}
        names = " ".join(str(item.get("name") or "").lower() for item in tree)
        has_sheet_metal = any("sheetmetal" in item or "sm" in item for item in types) or "sheet metal" in names or "钣金" in names
        has_base = any("baseflange" in item for item in types) or "baseflange" in names or "基体法兰" in names
        actual_flanges = [item for item in operation.get("edge_flanges", []) if item.get("success")]
        actual_bends = [item for item in operation.get("sketched_bends", []) if item.get("success")]
        actual_hems = [item for item in operation.get("hems", []) if item.get("success")]
        actual_jogs = [item for item in operation.get("jogs", []) if item.get("success")]
        actual_forming_tools = [item for item in operation.get("forming_tools", []) if item.get("success")]
        faces = sum(int(item.get("face_count", 0)) for item in body.get("body_info", []))
        bbox = body.get("bbox", {})
        normal_span_mm = abs(float(bbox.get("ymax", 0.0)) - float(bbox.get("ymin", 0.0))) * 1000.0
        edge_projection = max((
                item["height_mm"] * abs(math.sin(math.radians(item["angle_deg"])))
                for item in request["edge_flanges"]
            ), default=0.0)
        bend_projection = max((
            cls._sketched_bend_moving_leg(request, item)
            * abs(math.sin(math.radians(item["angle_deg"])))
            for item in request["sketched_bends"]
        ), default=0.0)
        hem_projection = max((
            request["thickness_mm"] + item["gap_mm"]
            for item in request["hems"]
        ), default=0.0)
        jog_projection = max((item["offset_distance_mm"] for item in request["jogs"]), default=0.0)
        expected_normal_span = request["thickness_mm"] + max(
            edge_projection,
            bend_projection,
            hem_projection,
            jog_projection,
        )
        envelope_ok = (
            not request["edge_flanges"]
            and not request["sketched_bends"]
            and not request["hems"]
            and not request["jogs"]
            or normal_span_mm >= expected_normal_span - request["bend_radius_mm"] - 0.25
        )
        topology_ok = (
            not request["edge_flanges"]
            and not request["sketched_bends"]
            and not request["hems"]
            and not request["jogs"]
        ) or faces > 6
        angle_ok = all(item.get("angle_verified") for item in actual_flanges)
        bend_parameters_ok = all(item.get("angle_verified") and item.get("radius_verified") for item in actual_bends)
        hem_parameters_ok = all(all(item.get("parameter_checks", {}).values()) for item in actual_hems)
        jog_parameters_ok = all(all(item.get("parameter_checks", {}).values()) for item in actual_jogs)
        forming_tool_parameters_ok = all(
            all(item.get("parameter_checks", {}).values()) for item in actual_forming_tools
        )
        forming_tool_geometry_ok = all(
            all(item.get("geometry_checks", {}).values()) for item in actual_forming_tools
        )
        has_sketched_bend = (
            not request["sketched_bends"]
            or any("sketchbend" in item for item in types)
            or "sketchedbend" in names.replace(" ", "")
        )
        has_hem = (
            not request["hems"]
            or any("hem" in item for item in types)
            or "hem" in names
        )
        has_jog = (
            not request["jogs"]
            or any("jog" in item for item in types)
            or "jog" in names
        )
        has_forming_tool = (
            not request["forming_tools"]
            or any(
                token in item
                for item in types
                for token in ("formtool", "formingtool", "libraryfeature")
            )
            or "formingtool" in names.replace(" ", "")
        )
        success = (
            has_sheet_metal
            and has_base
            and len(actual_flanges) == len(request["edge_flanges"])
            and len(actual_bends) == len(request["sketched_bends"])
            and len(actual_hems) == len(request["hems"])
            and len(actual_jogs) == len(request["jogs"])
            and len(actual_forming_tools) == len(request["forming_tools"])
            and angle_ok
            and bend_parameters_ok
            and hem_parameters_ok
            and jog_parameters_ok
            and forming_tool_parameters_ok
            and forming_tool_geometry_ok
            and has_sketched_bend
            and has_hem
            and has_jog
            and has_forming_tool
            and envelope_ok
            and topology_ok
        )
        return {
            "success": success,
            "message": "Sheet-metal base, bends, hems, and jogs verified." if success else "Sheet-metal feature-tree verification failed.",
            "sheet_metal_tree": has_sheet_metal,
            "base_flange_tree": has_base,
            "edge_flange_count": len(actual_flanges),
            "edge_flange_angles_verified": angle_ok,
            "sketched_bend_count": len(actual_bends),
            "sketched_bend_tree": has_sketched_bend,
            "sketched_bend_parameters_verified": bend_parameters_ok,
            "hem_count": len(actual_hems),
            "hem_tree": has_hem,
            "hem_parameters_verified": hem_parameters_ok,
            "jog_count": len(actual_jogs),
            "jog_tree": has_jog,
            "jog_parameters_verified": jog_parameters_ok,
            "forming_tool_count": len(actual_forming_tools),
            "forming_tool_tree": has_forming_tool,
            "forming_tool_parameters_verified": forming_tool_parameters_ok,
            "forming_tool_geometry_verified": forming_tool_geometry_ok,
            "solid_face_count": faces,
            "normal_span_mm": normal_span_mm,
            "expected_normal_span_mm": expected_normal_span,
            "envelope_ok": envelope_ok,
            "topology_ok": topology_ok,
        }

    @classmethod
    def _inspect_reopened_forming_tools(cls, model: Any, request: dict[str, Any]) -> dict[str, Any]:
        expected = request.get("forming_tools", [])
        if not expected:
            return {
                "success": True,
                "message": "No forming tool was requested.",
                "expected_count": 0,
                "actual_count": 0,
            }
        tree = ActiveModelFeatureSkill._feature_tree(model)
        candidates = [
            item
            for item in tree
            if any(
                token in str(item.get("type") or "").lower()
                for token in ("formtool", "formingtool", "libraryfeature")
            )
            or str(item.get("name") or "").lower().startswith("formingtool")
        ]
        body = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        face_count = sum(int(item.get("face_count", 0)) for item in body.get("body_info", []))
        success = (
            body.get("success")
            and int(body.get("body_count", 0)) == 1
            and len(candidates) == len(expected)
            and face_count > 6
        )
        return {
            "success": success,
            "message": (
                "Forming-tool feature persisted after save and reopen."
                if success
                else "Forming-tool feature did not persist correctly after save and reopen."
            ),
            "expected_count": len(expected),
            "actual_count": len(candidates),
            "features": candidates,
            "body_count": int(body.get("body_count", 0)),
            "face_count": face_count,
        }

    @classmethod
    def _verify_lofted_bend_geometry(
        cls,
        model: Any,
        request: dict[str, Any],
        body: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        loft = request["lofted_bend"]
        tree = ActiveModelFeatureSkill._feature_tree(model)
        has_loft = any("loftedbend" in str(item.get("type") or "").lower() for item in tree)
        has_flat_pattern = any("flatpattern" in str(item.get("type") or "").lower() for item in tree)
        actual = [item for item in operation.get("lofted_bends", []) if item.get("success")]
        parameters_ok = len(actual) == 1 and all(actual[0].get("parameter_checks", {}).values())
        faces = sum(int(item.get("face_count", 0)) for item in body.get("body_info", []))
        bbox = body.get("bbox", {})
        spans_mm = {
            "x": abs(float(bbox.get("xmax", 0.0)) - float(bbox.get("xmin", 0.0))) * 1000.0,
            "y": abs(float(bbox.get("ymax", 0.0)) - float(bbox.get("ymin", 0.0))) * 1000.0,
            "z": abs(float(bbox.get("zmax", 0.0)) - float(bbox.get("zmin", 0.0))) * 1000.0,
        }
        if loft.get("profile_mode") == "coaxial_helices":
            helix_count = sum(
                1 for item in tree if str(item.get("type") or "").lower() == "helix"
            )
            profile_3d_count = sum(
                1 for item in tree if str(item.get("type") or "").lower() == "3dprofilefeature"
            )
            radial_axes = [item for item in ("x", "y", "z") if item != loft["axis"]]
            expected_diameter = 2.0 * loft["outer_radius_mm"]
            envelope_ok = (
                spans_mm[loft["axis"]] >= loft["height_mm"] - 0.25
                and all(spans_mm[axis] >= expected_diameter - 0.25 for axis in radial_axes)
            )
            body_volumes = [cls._body_volume_m3(item) for item in body.get("bodies", [])]
            known_volumes = [
                float(value)
                for value in body_volumes
                if isinstance(value, (int, float)) and math.isfinite(float(value))
            ]
            volume_m3 = sum(known_volumes) if known_volumes else 0.0
            volume_ok = volume_m3 > 0.0
            success = (
                body.get("success")
                and int(body.get("body_count", 0)) == 1
                and has_loft
                and has_flat_pattern
                and parameters_ok
                and helix_count == 2
                and profile_3d_count == 2
                and faces >= 4
                and envelope_ok
                and volume_ok
            )
            return {
                "success": success,
                "message": (
                    "Native formed helical lofted-bend geometry verified."
                    if success
                    else "Formed helical lofted-bend geometry verification failed."
                ),
                "profile_mode": "coaxial_helices",
                "lofted_bend_tree": has_loft,
                "flat_pattern_tree": has_flat_pattern,
                "helix_count": helix_count,
                "profile_3d_count": profile_3d_count,
                "lofted_bend_count": len(actual),
                "parameters_verified": parameters_ok,
                "solid_body_count": int(body.get("body_count", 0)),
                "solid_face_count": faces,
                "volume_m3": volume_m3,
                "positive_volume": volume_ok,
                "bbox_spans_mm": spans_mm,
                "axis": loft["axis"],
                "expected_axial_height_mm": loft["height_mm"],
                "expected_outer_diameter_mm": expected_diameter,
                "envelope_ok": envelope_ok,
            }
        profile_x = max(
            max(point[0] for point in loft["profile_1_points_mm"])
            - min(point[0] for point in loft["profile_1_points_mm"]),
            max(point[0] for point in loft["profile_2_points_mm"])
            - min(point[0] for point in loft["profile_2_points_mm"]),
        )
        profile_y = max(
            max(point[1] for point in loft["profile_1_points_mm"])
            - min(point[1] for point in loft["profile_1_points_mm"]),
            max(point[1] for point in loft["profile_2_points_mm"])
            - min(point[1] for point in loft["profile_2_points_mm"]),
        )
        envelope_ok = (
            spans_mm["x"] >= profile_x - 0.25
            and spans_mm["y"] >= profile_y - 0.25
            and spans_mm["z"] >= loft["plane_offset_mm"] - 0.25
        )
        success = (
            body.get("success")
            and int(body.get("body_count", 0)) == 1
            and has_loft
            and has_flat_pattern
            and parameters_ok
            and faces > 10
            and envelope_ok
        )
        return {
            "success": success,
            "message": "Native lofted-bend geometry verified." if success else "Lofted-bend geometry verification failed.",
            "lofted_bend_tree": has_loft,
            "flat_pattern_tree": has_flat_pattern,
            "lofted_bend_count": len(actual),
            "parameters_verified": parameters_ok,
            "solid_body_count": int(body.get("body_count", 0)),
            "solid_face_count": faces,
            "bbox_spans_mm": spans_mm,
            "expected_profile_x_span_mm": profile_x,
            "expected_profile_y_span_mm": profile_y,
            "expected_plane_offset_mm": loft["plane_offset_mm"],
            "envelope_ok": envelope_ok,
        }

    def _export_flat_pattern_dxf(
        self,
        sw: Any,
        model: Any,
        part_path: Path,
        dxf_path: Path,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if dxf_path.exists():
            return {
                "success": False,
                "requested": True,
                "message": f"Refusing to overwrite an existing flat-pattern DXF: {dxf_path}",
            }
        dxf_path.parent.mkdir(parents=True, exist_ok=True)
        sheet_metal_options = self.DXF_FLAT_GEOMETRY
        if options["include_bend_lines"]:
            sheet_metal_options |= self.DXF_BEND_LINES
        if options["include_sketches"]:
            sheet_metal_options |= self.DXF_SKETCHES
        if options["include_library_features"]:
            sheet_metal_options |= self.DXF_LIBRARY_FEATURES
        if options["include_forming_tools"]:
            sheet_metal_options |= self.DXF_FORMING_TOOLS
        if options["include_bounding_box"]:
            sheet_metal_options |= self.DXF_BOUNDING_BOX

        module = self._solidworks_module(sw)
        try:
            part_doc = module.IPartDoc(model._oleobj_)
        except Exception as exc:
            return {
                "success": False,
                "requested": True,
                "message": f"Could not access IPartDoc for flat-pattern export: {exc}",
            }
        exported = part_doc.ExportToDWG2(
            str(dxf_path),
            str(part_path),
            self.SW_EXPORT_TO_DWG_SHEET_METAL,
            True,
            [0.0] * 12,
            False,
            False,
            sheet_metal_options,
            None,
        )
        if not exported or not dxf_path.is_file() or dxf_path.stat().st_size <= 0:
            return {
                "success": False,
                "requested": True,
                "message": f"SolidWorks ExportToDWG2 did not create a non-empty DXF: {dxf_path}",
                "api_return": bool(exported),
                "sheet_metal_options": sheet_metal_options,
            }
        inspection = self._inspect_dxf(dxf_path)
        return {
            "success": bool(inspection.get("success")),
            "requested": True,
            "message": (
                "SolidWorks flat-pattern DXF exported and verified."
                if inspection.get("success")
                else str(inspection.get("message"))
            ),
            "path": str(dxf_path),
            "source_part": str(part_path),
            "api_return": bool(exported),
            "sheet_metal_options": sheet_metal_options,
            "options": dict(options),
            "inspection": inspection,
        }

    @staticmethod
    def _inspect_dxf(path: Path) -> dict[str, Any]:
        payload = path.read_bytes()
        if payload.startswith(b"AutoCAD Binary DXF"):
            success = len(payload) > 512
            return {
                "success": success,
                "message": "Binary DXF header and payload verified." if success else "Binary DXF is too small.",
                "format": "binary_dxf",
                "size_bytes": len(payload),
                "geometry_entity_count": None,
                "geometry_entities": {},
                "layers": [],
            }
        text = payload.decode("utf-8", errors="replace")
        if "SECTION" not in text or "ENTITIES" not in text:
            text = payload.decode("cp1252", errors="replace")
        lines = [line.strip() for line in text.replace("\r", "").split("\n")]
        entity_types = {"LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"}
        counts = {entity: 0 for entity in sorted(entity_types)}
        layers: set[str] = set()
        pairs = [(lines[index], lines[index + 1]) for index in range(0, len(lines) - 1, 2)]
        entities: list[tuple[str, dict[str, list[str]]]] = []
        current_type: str | None = None
        current_values: dict[str, list[str]] = {}
        for code, value in pairs:
            if code == "8" and value:
                layers.add(value)
            if code == "0":
                if current_type is not None:
                    entities.append((current_type, current_values))
                current_type = value if value in entity_types else None
                current_values = {}
                if current_type is not None:
                    counts[current_type] += 1
            elif current_type is not None:
                current_values.setdefault(code, []).append(value)
        if current_type is not None:
            entities.append((current_type, current_values))

        x_values: list[float] = []
        y_values: list[float] = []
        for entity_type, values in entities:
            try:
                if entity_type == "LINE":
                    x_values.extend((float(values["10"][0]), float(values["11"][0])))
                    y_values.extend((float(values["20"][0]), float(values["21"][0])))
                elif entity_type in {"CIRCLE", "ARC"}:
                    center_x = float(values["10"][0])
                    center_y = float(values["20"][0])
                    radius = abs(float(values["40"][0]))
                    x_values.extend((center_x - radius, center_x + radius))
                    y_values.extend((center_y - radius, center_y + radius))
                else:
                    x_values.extend(float(value) for value in values.get("10", []))
                    y_values.extend(float(value) for value in values.get("20", []))
            except (KeyError, TypeError, ValueError):
                continue
        counts = {key: value for key, value in counts.items() if value}
        geometry_count = sum(counts.values())
        header_ok = all(token in text for token in ("SECTION", "ENTITIES", "EOF"))
        bounds = None
        spans = None
        if x_values and y_values:
            bounds = {
                "xmin": min(x_values),
                "ymin": min(y_values),
                "xmax": max(x_values),
                "ymax": max(y_values),
            }
            spans = {
                "x": bounds["xmax"] - bounds["xmin"],
                "y": bounds["ymax"] - bounds["ymin"],
            }
        success = (
            header_ok
            and geometry_count > 0
            and len(payload) > 256
            and spans is not None
            and spans["x"] > 0
            and spans["y"] > 0
        )
        return {
            "success": success,
            "message": (
                "ASCII DXF structure and geometry entities verified."
                if success
                else "DXF structure or geometry entity verification failed."
            ),
            "format": "ascii_dxf",
            "size_bytes": len(payload),
            "header_ok": header_ok,
            "geometry_entity_count": geometry_count,
            "geometry_entities": counts,
            "geometry_bounds": bounds,
            "geometry_spans": spans,
            "layers": sorted(layers),
        }

    @staticmethod
    def _sketched_bend_moving_leg(request: dict[str, Any], bend: dict[str, Any]) -> float:
        offset = float(bend["offset_mm"])
        span = request["length_mm"] if bend["line_orientation"] == "parallel_to_width" else request["width_mm"]
        if bend["fixed_side"] == "negative":
            return span / 2.0 - offset
        return span / 2.0 + offset

    @classmethod
    def _apply_flat_pattern_state(cls, model: Any, request: dict[str, Any]) -> dict[str, Any]:
        flat_pattern = cls._find_feature_by_type(model, "FlatPattern")
        if flat_pattern is None:
            return {"success": False, "message": "Native FlatPattern feature was not created."}
        before_suppressed = cls._feature_is_suppressed(flat_pattern)
        if before_suppressed is None:
            return {"success": False, "message": "Could not read FlatPattern suppression state."}

        final_state = request["flat_pattern"]["final_state"]
        desired_suppressed = final_state == "folded"
        changed = before_suppressed != desired_suppressed
        if changed:
            action = 0 if desired_suppressed else 1
            changed_ok = ActiveModelThroughHoleExecutor._com_member(
                flat_pattern,
                "SetSuppression2",
                action,
                1,
                None,
                default=False,
            )
            if not changed_ok:
                return {"success": False, "message": f"Could not switch FlatPattern to {final_state}."}
            model.ForceRebuild3(False)

        inspected = cls._inspect_flat_pattern_state(model, request)
        inspected.update({
            "requested": request["flat_pattern"]["requested"],
            "before_suppressed": before_suppressed,
            "changed": changed,
        })
        return inspected

    @classmethod
    def _inspect_flat_pattern_state(cls, model: Any, request: dict[str, Any]) -> dict[str, Any]:
        flat_pattern = cls._find_feature_by_type(model, "FlatPattern")
        if flat_pattern is None:
            return {"success": False, "message": "Native FlatPattern feature is missing."}
        suppressed = cls._feature_is_suppressed(flat_pattern)
        if suppressed is None:
            return {"success": False, "message": "Could not inspect FlatPattern suppression state."}

        expected_state = request["flat_pattern"]["final_state"]
        actual_state = "folded" if suppressed else "flattened"
        body = RevolveSkill._with_body_evidence(ActiveModelThroughHoleExecutor._body_info(model))
        bbox = body.get("bbox", {})
        spans_mm = [
            abs(float(bbox.get("xmax", 0.0)) - float(bbox.get("xmin", 0.0))) * 1000.0,
            abs(float(bbox.get("ymax", 0.0)) - float(bbox.get("ymin", 0.0))) * 1000.0,
            abs(float(bbox.get("zmax", 0.0)) - float(bbox.get("zmin", 0.0))) * 1000.0,
        ]
        flattened_geometry_ok = (
            actual_state != "flattened"
            or min(spans_mm, default=0.0) <= request["thickness_mm"] + 0.25
        )
        success = (
            body.get("success")
            and int(body.get("body_count", 0)) == 1
            and actual_state == expected_state
            and flattened_geometry_ok
        )
        return {
            "success": success,
            "message": (
                f"Native FlatPattern verified in {actual_state} state."
                if success
                else f"FlatPattern state verification failed: expected {expected_state}, got {actual_state}."
            ),
            "feature_name": str(
                ActiveModelThroughHoleExecutor._com_member(flat_pattern, "Name", default="") or ""
            ),
            "expected_state": expected_state,
            "actual_state": actual_state,
            "suppressed": suppressed,
            "body_count": int(body.get("body_count", 0)),
            "bbox_spans_mm": spans_mm,
            "flattened_geometry_ok": flattened_geometry_ok,
        }

    @staticmethod
    def _feature_is_suppressed(feature: Any) -> bool | None:
        value = ActiveModelThroughHoleExecutor._com_member(
            feature,
            "IsSuppressed2",
            1,
            None,
            default=None,
        )
        if isinstance(value, (tuple, list)):
            return bool(value[0]) if value else None
        return bool(value) if value is not None else None

    @staticmethod
    def _find_feature_by_type(model: Any, type_name: str) -> Any | None:
        result = None
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None:
            current_type = str(
                ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""
            )
            if current_type == type_name:
                result = feature
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        return result

    @staticmethod
    def _solidworks_module(sw: Any) -> Any:
        import win32com.client

        revision = ActiveModelThroughHoleExecutor._com_member(sw, "RevisionNumber", default="33.0")
        major = int(str(revision).split(".", 1)[0])
        return win32com.client.gencache.EnsureModule(
            "{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, major, 0
        )

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
            if values.get(key) not in (None, ""):
                try:
                    return float(values[key])
                except (TypeError, ValueError):
                    return None
        return None

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
        return SkillResult(success, message, data=payload, path=str(report_path), output=json.dumps(payload, ensure_ascii=False, indent=2))
