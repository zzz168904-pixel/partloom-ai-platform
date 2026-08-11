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
from .models import SkillResult
from .revolve_skill import RevolveSkill


class WeldmentSkill:
    """Create native structural members from explicit 3D line segments."""

    FEATURE_TYPE = "weldment"
    PROFILE_FILES = {
        "square_tube": "square tube.sldlfp",
        "pipe": "pipe.sldlfp",
        "solid_round": "solid_round_4mm.sldlfp",
    }
    SOLID_ROUND_CONFIGURATIONS = {"D4": 4.0}
    FLAT_BAR_PROFILE_FILE = "flat_bar_20x3.sldlfp"
    FLAT_BAR_CONFIGURATION = "FB20X3"
    WIRE_CLIP_PROFILE_FILE = "wire_clip_8x2.sldlfp"
    WIRE_CLIP_CONFIGURATION = "CLIP8X2"

    def __init__(self, output_root: Path, solidworks_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.solidworks_skill_dir = solidworks_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation"
        )
        self.script_dir = self.solidworks_skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [item for item in plan.get("features", []) if item.get("type") == self.FEATURE_TYPE]
        if len(features) != 1:
            return SkillResult(False, "A weldment task requires exactly one weldment feature.")

        run_dir = self.output_root / f"weldment_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "weldment_report.json"
        try:
            request = self.normalize_request(features[0].get("params", {}), plan.get("task_type"))
            if not request.get("success"):
                return self._result(False, str(request.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "invalid_request": request,
                })

            prototype_only = request.get("geometry_kind") == "flat_bar_prototype"
            profile_path = None if prototype_only else self._profile_path(request)
            if not prototype_only and profile_path is None:
                return self._result(False, "The requested SolidWorks weldment profile file is not installed.", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "request": request,
                })
            flat_bar_profile_path = self._flat_bar_profile_path(request)
            flat_bar_uses_profile = bool(
                request.get("flat_bar_frame")
                and request["flat_bar_frame"].get("body_mode") != "notched_flat_bar_grid"
            )
            if flat_bar_uses_profile and flat_bar_profile_path is None:
                return self._result(False, "The bundled 20 x 3 mm flat-bar weldment profile is not installed.", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "request": request,
                })
            wire_clip_profile_path = self._wire_clip_profile_path(request)
            if request.get("wire_clips") and wire_clip_profile_path is None:
                return self._result(False, "The bundled 8 x 2 mm wire-clip profile is not installed.", report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "request": request,
                })

            self._ensure_imports()
            from sw_connect import connect_solidworks, get_com_member, new_document, save_document

            sw, _active = connect_solidworks(visible=True)
            model = new_document(sw, "part")
            validation = RevolveSkill._validate_active_part(model, require_body=False)
            if not validation.get("success"):
                return self._result(False, str(validation.get("message")), report_path, validation)

            module = self._solidworks_module(sw)
            operation = self._create_weldment(
                model,
                module,
                sw,
                features[0],
                request,
                profile_path,
                flat_bar_profile_path,
                wire_clip_profile_path,
            )
            if not operation.get("success"):
                self._close_unsaved_generated_document(sw, model)
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
                self._close_unsaved_generated_document(sw, model)
                return self._result(False, str(geometry.get("message")), report_path, {
                    "feature_type": self.FEATURE_TYPE,
                    "operations": [operation],
                })

            material = str(plan.get("parameters", {}).get("material") or "").strip()
            material_metadata = RevolveSkill._set_material_metadata(model, material)
            requested_path = str(plan.get("execution_model_path") or "").strip()
            part_path = (Path(requested_path) if requested_path else run_dir / "weldment_part.SLDPRT").resolve()
            part_path.parent.mkdir(parents=True, exist_ok=True)
            if not save_document(model, str(part_path)) or not part_path.is_file() or part_path.stat().st_size <= 0:
                self._close_unsaved_generated_document(sw, model)
                return self._result(False, f"SolidWorks failed to save weldment Part: {part_path}", report_path, {
                    "operations": [operation],
                })
            reopen = RevolveSkill._verify_reopen(sw, model, part_path)
            if not reopen.get("success"):
                return self._result(False, f"Saved weldment Part could not be reopened: {part_path}", report_path, {
                    "operations": [operation],
                    "reopen_validation": reopen,
                    "files": [str(part_path)],
                })
            model = reopen.pop("model")
            data = {
                "active_doc": str(get_com_member(model, "GetTitle") or ""),
                "mode": "new_model",
                "feature_type": self.FEATURE_TYPE,
                "feature_created": True,
                "features_created": 1,
                "operations": [operation],
                "material": material,
                "material_metadata": material_metadata,
                "reopen_validation": reopen,
                "feature_tree": ActiveModelFeatureSkill._feature_tree(model),
                "saved_by_this_skill": True,
                "side_effects": {
                    "modifies_active_doc": False,
                    "creates_new_doc": True,
                    "exports_files": False,
                    "uses_template": False,
                },
                "files": [str(part_path)],
            }
            return self._result(True, "Native SolidWorks weldment created and verified.", report_path, data)
        except Exception as exc:
            return self._result(False, f"weldment failed: {exc}", report_path, {
                "feature_type": self.FEATURE_TYPE,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            })

    @classmethod
    def normalize_request(cls, params: dict[str, Any], task_type: str | None = None) -> dict[str, Any]:
        mode = str(params.get("mode") or "new_model").strip().lower()
        if mode != "new_model":
            return {"success": False, "message": "weldment currently supports new_model mode only."}
        if task_type == "modify_3d":
            return {"success": False, "message": "modify_3d cannot create a new weldment Part."}
        wire_mesh = None
        flat_bar_frame = None
        wire_clips = None
        wire_seat_notches = None
        normalized_params = dict(params)
        if params.get("wire_mesh") is not None:
            wire_mesh_result = cls._normalize_wire_mesh(params.get("wire_mesh"))
            if not wire_mesh_result.get("success"):
                return wire_mesh_result
            wire_mesh = wire_mesh_result["wire_mesh"]
            flat_bar_frame = wire_mesh_result.get("flat_bar_frame")
            wire_clips = (flat_bar_frame or {}).get("wire_clips")
            wire_seat_notches = (flat_bar_frame or {}).get("wire_seat_notches")
            normalized_params.update(
                {
                    "standard": "iso",
                    "profile_type": "solid_round",
                    "profile_configuration": "D4",
                    "path_segments_mm": wire_mesh_result["path_segments_mm"],
                    "groups": wire_mesh_result["groups"],
                    "corner_treatment": "miter",
                    "merge_arc_segment_bodies": False,
                }
            )

        if flat_bar_frame and flat_bar_frame.get("prototype_mode") in {"one_each", "joined_pair"}:
            return {
                "success": True,
                "mode": "new_model",
                "standard": "iso",
                "profile_type": "solid_round",
                "profile_configuration": "D4",
                "path_segments_mm": [],
                "groups": [],
                "corner_treatment": "miter",
                "merge_arc_segment_bodies": False,
                "allow_protrusion": False,
                "geometry_kind": "flat_bar_prototype",
                "wire_mesh": wire_mesh,
                "flat_bar_frame": flat_bar_frame,
                "wire_clips": None,
                "wire_seat_notches": wire_seat_notches,
            }

        standard = str(normalized_params.get("standard") or "iso").strip().lower()
        if standard != "iso":
            return {"success": False, "message": "The production weldment executor currently supports ISO profiles only."}
        profile_type = str(normalized_params.get("profile_type") or "square_tube").strip().lower().replace(" ", "_")
        if profile_type not in cls.PROFILE_FILES:
            return {"success": False, "message": "weldment profile_type must be square_tube, pipe, or solid_round."}
        configuration = str(
            normalized_params.get("profile_configuration") or normalized_params.get("configuration") or ""
        ).strip()
        if not configuration:
            return {"success": False, "message": "weldment requires profile_configuration."}
        if profile_type == "solid_round":
            alias = configuration.lower().replace(" ", "").replace("ø", "d").replace("φ", "d")
            if alias in {"4", "4mm", "d4", "d4mm"}:
                configuration = "D4"
            if configuration not in cls.SOLID_ROUND_CONFIGURATIONS:
                return {"success": False, "message": "solid_round currently supports only the bundled D4 profile."}

        segments_result = cls._normalize_segments(normalized_params)
        if not segments_result.get("success"):
            return segments_result
        segments = segments_result["path_segments_mm"]
        raw_groups = normalized_params.get("groups")
        groups: list[list[int]] = []
        if raw_groups is None:
            groups = [list(range(len(segments)))]
        elif isinstance(raw_groups, list):
            for group_index, raw_group in enumerate(raw_groups):
                if not isinstance(raw_group, list) or not raw_group:
                    return {"success": False, "message": f"weldment group {group_index + 1} must be a non-empty index list."}
                try:
                    group = [int(value) for value in raw_group]
                except (TypeError, ValueError):
                    return {"success": False, "message": f"weldment group {group_index + 1} contains a non-integer index."}
                if len(set(group)) != len(group) or min(group) < 0 or max(group) >= len(segments):
                    return {"success": False, "message": f"weldment group {group_index + 1} contains invalid segment indexes."}
                groups.append(group)
        else:
            return {"success": False, "message": "weldment groups must be a list of segment-index lists."}
        flattened = [index for group in groups for index in group]
        if sorted(flattened) != list(range(len(segments))):
            return {"success": False, "message": "Each weldment path segment must appear in exactly one group."}

        corner = str(normalized_params.get("corner_treatment") or "miter").strip().lower()
        if corner not in {"miter", "butt1", "butt2"}:
            return {"success": False, "message": "weldment corner_treatment must be miter, butt1, or butt2."}
        return {
            "success": True,
            "mode": "new_model",
            "standard": standard,
            "profile_type": profile_type,
            "profile_configuration": configuration,
            "path_segments_mm": segments,
            "groups": groups,
            "corner_treatment": corner,
            "merge_arc_segment_bodies": bool(normalized_params.get("merge_arc_segment_bodies", True)),
            "allow_protrusion": bool(normalized_params.get("allow_protrusion", False)),
            "geometry_kind": "wire_mesh" if wire_mesh else "structural_frame",
            "wire_mesh": wire_mesh,
            "flat_bar_frame": flat_bar_frame,
            "wire_clips": wire_clips,
            "wire_seat_notches": wire_seat_notches,
        }

    @classmethod
    def _normalize_wire_mesh(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": "weldment wire_mesh must be an object."}

        def number(name: str) -> float | None:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        required = (
            "wire_diameter_mm",
            "arc_radius_mm",
            "arc_length_mm",
            "overall_height_mm",
            "mesh_height_mm",
            "end_pitch_mm",
            "middle_pitch_mm",
            "overall_depth_mm",
        )
        values = {name: number(name) for name in required}
        if any(value is None for value in values.values()):
            return {"success": False, "message": "wire_mesh requires all drawing dimensions as numeric millimetres."}
        diameter = float(values["wire_diameter_mm"])
        inner_surface_radius = float(values["arc_radius_mm"])
        arc_length = float(values["arc_length_mm"])
        overall_height = float(values["overall_height_mm"])
        mesh_height = float(values["mesh_height_mm"])
        end_pitch = float(values["end_pitch_mm"])
        middle_pitch = float(values["middle_pitch_mm"])
        overall_depth = float(values["overall_depth_mm"])
        longitudinal_count = raw.get("longitudinal_count", 6)
        transverse_count = raw.get("transverse_count", 7)
        layering = str(raw.get("layering") or "transverse_arc_wires_on_top").strip().lower()
        layer_reference = str(raw.get("layer_reference") or "drawing_side_profile").strip().lower()
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != float(value)
               for value in (longitudinal_count, transverse_count)):
            return {"success": False, "message": "wire_mesh strand counts must be integers."}
        longitudinal_count = int(longitudinal_count)
        transverse_count = int(transverse_count)
        flat_bar_raw = raw.get("flat_bar_frame") if isinstance(raw.get("flat_bar_frame"), dict) else {}
        replace_longitudinal_wires = bool(flat_bar_raw.get("replace_longitudinal_wires", False))
        if abs(diameter - 4.0) > 0.01:
            return {"success": False, "message": "The current wire-mesh contract supports only D4 solid wire."}
        if longitudinal_count != 6 or transverse_count != 7:
            return {"success": False, "message": "The current drawing contract requires 6 longitudinal and 7 transverse wires."}
        if layering != "transverse_arc_wires_on_top":
            return {
                "success": False,
                "message": "The current wire-mesh contract requires transverse_arc_wires_on_top layering.",
            }
        if layer_reference != "drawing_side_profile":
            return {
                "success": False,
                "message": "Curved wire-mesh layering must use drawing_side_profile as its reference.",
            }
        wire_radius = diameter / 2.0
        transverse_centerline_radius = inner_surface_radius + wire_radius
        outer_surface_radius = inner_surface_radius + diameter
        longitudinal_center_locus_radius = outer_surface_radius + wire_radius
        supplied_transverse_radius = number("transverse_layer_radius_mm")
        supplied_longitudinal_radius = number("longitudinal_layer_radius_mm")
        if supplied_transverse_radius is not None and abs(
            supplied_transverse_radius - transverse_centerline_radius
        ) > 0.01:
            return {
                "success": False,
                "message": "transverse_layer_radius_mm must equal the D4 transverse-wire centerline radius R271.",
            }
        if supplied_longitudinal_radius is not None and abs(
            supplied_longitudinal_radius - longitudinal_center_locus_radius
        ) > 0.01:
            return {
                "success": False,
                "message": "longitudinal_layer_radius_mm must place longitudinal centers on the R275 locus.",
            }
        supplied_radius_contract = {
            "inner_surface_radius_mm": inner_surface_radius,
            "transverse_centerline_radius_mm": transverse_centerline_radius,
            "outer_surface_radius_mm": outer_surface_radius,
            "longitudinal_center_locus_radius_mm": longitudinal_center_locus_radius,
        }
        for field, expected in supplied_radius_contract.items():
            supplied = number(field)
            if supplied is not None and abs(supplied - expected) > 0.01:
                return {
                    "success": False,
                    "message": f"{field} must equal {expected:g} mm for the R269/D4 drawing profile.",
                }
        if min(
            inner_surface_radius,
            arc_length,
            overall_height,
            mesh_height,
            end_pitch,
            middle_pitch,
            overall_depth,
        ) <= 0:
            return {"success": False, "message": "wire_mesh dimensions must be positive."}
        # The drawing dimensions the outer R273 surface at 276.4 mm. R269 is
        # the inner surface; the D4 path itself is the R271 centerline.
        total_angle = arc_length / outer_surface_radius
        if not 0.05 < total_angle < math.pi:
            return {"success": False, "message": "wire_mesh arc length and radius produce an invalid included angle."}
        expected_mesh_height = diameter + 2.0 * end_pitch + (transverse_count - 3) * middle_pitch
        if abs(expected_mesh_height - mesh_height) > 0.5:
            return {
                "success": False,
                "message": "wire_mesh height chain is inconsistent with diameter, end pitches, and middle pitches.",
            }
        if overall_height < mesh_height or overall_height - mesh_height > 100.0:
            return {"success": False, "message": "wire_mesh overall height is inconsistent with the mesh height."}

        raw_chord_pitches = raw.get("longitudinal_chord_pitches_mm")
        station_layout_source = "drawing_chord_pitches"
        if raw_chord_pitches is None:
            angle_step = total_angle / (longitudinal_count - 1)
            chord_pitches = [
                2.0 * longitudinal_center_locus_radius * math.sin(angle_step / 2.0)
                for _ in range(longitudinal_count - 1)
            ]
            station_layout_source = "equal_angle_fallback"
        else:
            if not isinstance(raw_chord_pitches, (list, tuple)) or len(raw_chord_pitches) != longitudinal_count - 1:
                return {
                    "success": False,
                    "message": "longitudinal_chord_pitches_mm must contain one side-view chord pitch per adjacent wire pair.",
                }
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0
                for value in raw_chord_pitches
            ):
                return {
                    "success": False,
                    "message": "longitudinal_chord_pitches_mm values must be positive numbers.",
                }
            chord_pitches = [float(value) for value in raw_chord_pitches]
        if any(value >= 2.0 * longitudinal_center_locus_radius for value in chord_pitches):
            return {"success": False, "message": "A longitudinal side-view chord pitch exceeds the R275 locus diameter."}
        angle_gaps = [
            2.0 * math.asin(value / (2.0 * longitudinal_center_locus_radius))
            for value in chord_pitches
        ]
        station_angle_span = sum(angle_gaps)
        if station_angle_span > total_angle + math.radians(0.1):
            return {
                "success": False,
                "message": "Longitudinal wire stations extend beyond the transverse-wire side profile.",
            }
        angles = [-station_angle_span / 2.0]
        for gap in angle_gaps:
            angles.append(angles[-1] + gap)

        common_center_xz = [0.0, -transverse_centerline_radius]
        arc_points = [
            [
                transverse_centerline_radius * math.sin(angle),
                -transverse_centerline_radius + transverse_centerline_radius * math.cos(angle),
            ]
            for angle in angles
        ]
        longitudinal_centers = [
            [
                longitudinal_center_locus_radius * math.sin(angle),
                -transverse_centerline_radius + longitudinal_center_locus_radius * math.cos(angle),
            ]
            for angle in angles
        ]
        arc_endpoints = [
            [
                transverse_centerline_radius * math.sin(angle),
                -transverse_centerline_radius + transverse_centerline_radius * math.cos(angle),
            ]
            for angle in (-total_angle / 2.0, total_angle / 2.0)
        ]
        curve_depth = -arc_endpoints[0][1]
        calculated_outer_surface_chord = 2.0 * outer_surface_radius * math.sin(total_angle / 2.0)
        calculated_profile_depth = (
            outer_surface_radius - inner_surface_radius * math.cos(total_angle / 2.0)
        )
        maximum_longitudinal_z = max(point[1] + wire_radius for point in longitudinal_centers)
        minimum_transverse_z = (
            -transverse_centerline_radius
            + inner_surface_radius * math.cos(total_angle / 2.0)
        )
        calculated_drawing_depth = maximum_longitudinal_z - minimum_transverse_z
        calculated_bbox_depth = calculated_drawing_depth
        calculated_bbox_half_width = max(
            outer_surface_radius * math.sin(total_angle / 2.0),
            max(abs(point[0]) + wire_radius for point in longitudinal_centers),
        )
        calculated_bbox_width = 2.0 * calculated_bbox_half_width
        layer_contact_distances = [
            math.dist(arc_point, longitudinal_point)
            for arc_point, longitudinal_point in zip(arc_points, longitudinal_centers)
        ]
        arc_layer_radii = [
            math.dist(arc_point, common_center_xz)
            for arc_point in arc_points
        ]
        longitudinal_layer_radii = [
            math.dist(longitudinal_point, common_center_xz)
            for longitudinal_point in longitudinal_centers
        ]
        radial_layer_separations = [
            longitudinal_radius_value - arc_radius_value
            for arc_radius_value, longitudinal_radius_value in zip(
                arc_layer_radii,
                longitudinal_layer_radii,
            )
        ]
        transverse_profile_above_longitudinal = bool(
            min(radial_layer_separations) > 0.0
            and max(abs(value - diameter) for value in radial_layer_separations) <= 1e-6
            and max(abs(value - diameter) for value in layer_contact_distances) <= 1e-6
            and all(
                longitudinal_point[1] > arc_point[1]
                for arc_point, longitudinal_point in zip(arc_points, longitudinal_centers)
            )
        )
        supplied_outer_chord = number("outer_surface_chord_mm")
        if supplied_outer_chord is not None and abs(
            supplied_outer_chord - calculated_outer_surface_chord
        ) > 0.75:
            return {
                "success": False,
                "message": "wire_mesh outer-surface chord is inconsistent with R273 and the 276.4 mm outer arc.",
            }
        supplied_profile_depth = number("transverse_profile_depth_mm")
        if supplied_profile_depth is not None and abs(
            supplied_profile_depth - calculated_profile_depth
        ) > 0.75:
            return {
                "success": False,
                "message": "wire_mesh transverse profile depth is inconsistent with the R269/R273 drawing band.",
            }
        if abs(calculated_drawing_depth - overall_depth) > 0.75:
            return {
                "success": False,
                "message": "wire_mesh overall depth is inconsistent with the DWG side profile and tangent D4 layers.",
            }
        side_profile_matches_dwg = bool(
            arc_endpoints[0][1] < 0.0
            and arc_endpoints[1][1] < 0.0
            and transverse_profile_above_longitudinal
            and abs(calculated_outer_surface_chord - float(supplied_outer_chord or calculated_outer_surface_chord)) <= 0.75
            and abs(calculated_profile_depth - float(supplied_profile_depth or calculated_profile_depth)) <= 0.75
            and abs(calculated_drawing_depth - overall_depth) <= 0.75
        )

        center_span = mesh_height - diameter
        y_positions = [-center_span / 2.0, -center_span / 2.0 + end_pitch]
        for _ in range(transverse_count - 3):
            y_positions.append(y_positions[-1] + middle_pitch)
        y_positions.append(y_positions[-1] + end_pitch)
        if len(y_positions) != transverse_count or abs(y_positions[-1] - center_span / 2.0) > 0.5:
            return {"success": False, "message": "wire_mesh transverse-wire positions do not close the height chain."}

        segments: list[list[list[float]]] = []
        groups: list[list[int]] = []
        longitudinal_lengths = [
            overall_height if index in {0, longitudinal_count - 1} else mesh_height
            for index in range(longitudinal_count)
        ]
        if not replace_longitudinal_wires:
            for index, (x_value, z_value) in enumerate(longitudinal_centers):
                half_height = longitudinal_lengths[index] / 2.0
                path_index = len(segments)
                segments.append(
                    [[x_value, -half_height, z_value], [x_value, half_height, z_value]]
                )
                groups.append([path_index])
        arc_entities: list[dict[str, Any]] = []
        for y_value in y_positions:
            path_index = len(segments)
            start = [arc_endpoints[0][0], y_value, arc_endpoints[0][1]]
            end = [arc_endpoints[-1][0], y_value, arc_endpoints[-1][1]]
            point_on_arc = [0.0, y_value, 0.0]
            segments.append([start, end])
            arc_entities.append(
                {
                    "path_index": path_index,
                    "start_mm": start,
                    "end_mm": end,
                    "point_on_arc_mm": point_on_arc,
                }
            )
            groups.append([path_index])

        calculated_chord_pitches = [
            math.dist(first, second)
            for first, second in zip(longitudinal_centers, longitudinal_centers[1:])
        ]
        angle_step = station_angle_span / (longitudinal_count - 1)
        strand_pitch = sum(calculated_chord_pitches) / len(calculated_chord_pitches)
        wire_mesh = {
            "wire_diameter_mm": diameter,
            "arc_radius_mm": inner_surface_radius,
            "arc_length_mm": arc_length,
            "arc_radius_semantic": "inner_surface",
            "arc_length_semantic": "outer_surface_arc",
            "inner_surface_radius_mm": inner_surface_radius,
            "transverse_centerline_radius_mm": transverse_centerline_radius,
            "outer_surface_radius_mm": outer_surface_radius,
            "longitudinal_center_locus_radius_mm": longitudinal_center_locus_radius,
            "included_angle_rad": total_angle,
            "included_angle_deg": math.degrees(total_angle),
            "transverse_centerline_arc_length_mm": transverse_centerline_radius * total_angle,
            "overall_height_mm": overall_height,
            "mesh_height_mm": mesh_height,
            "end_pitch_mm": end_pitch,
            "middle_pitch_mm": middle_pitch,
            "overall_depth_mm": overall_depth,
            "longitudinal_count": longitudinal_count,
            "transverse_count": transverse_count,
            "arc_segment_count": longitudinal_count - 1,
            "calculated_arc_pitch_mm": strand_pitch,
            "calculated_angle_pitch_deg": math.degrees(angle_step),
            "calculated_curve_depth_mm": curve_depth,
            "calculated_outer_surface_chord_mm": calculated_outer_surface_chord,
            "calculated_transverse_profile_depth_mm": calculated_profile_depth,
            "calculated_drawing_depth_mm": calculated_drawing_depth,
            "calculated_bbox_depth_mm": calculated_bbox_depth,
            "calculated_bbox_width_mm": calculated_bbox_width,
            "layer_center_distance_mm": diameter,
            "minimum_wire_clearance_mm": 0.0,
            "layering": layering,
            "layer_reference": layer_reference,
            "side_profile_sag_direction": "negative_z_endpoints",
            "standard_top_view_screen_down_axis": "+Z",
            "transverse_profile_position": "upper",
            "longitudinal_profile_position": "lower",
            "side_profile_matches_dwg": side_profile_matches_dwg,
            "transverse_profile_above_longitudinal": transverse_profile_above_longitudinal,
            "transverse_layer_radius_mm": transverse_centerline_radius,
            "longitudinal_layer_radius_mm": longitudinal_center_locus_radius,
            "minimum_radial_layer_separation_mm": min(radial_layer_separations),
            "layer_contact_distances_mm": layer_contact_distances,
            "common_arc_center_xz_mm": common_center_xz,
            "curve_points_xz_mm": arc_points,
            "transverse_arc_endpoints_xz_mm": arc_endpoints,
            "longitudinal_centers_xz_mm": longitudinal_centers,
            "longitudinal_chord_pitches_mm": chord_pitches,
            "calculated_station_chord_pitches_mm": calculated_chord_pitches,
            "longitudinal_station_angles_rad": angles,
            "longitudinal_station_angles_deg": [math.degrees(value) for value in angles],
            "station_layout_source": station_layout_source,
            "outer_surface_chord_mm": supplied_outer_chord,
            "transverse_profile_depth_mm": supplied_profile_depth,
            "longitudinal_lengths_mm": longitudinal_lengths,
            "replace_longitudinal_wires": replace_longitudinal_wires,
            "longitudinal_wire_created_count": 0 if replace_longitudinal_wires else longitudinal_count,
            "extended_end_wires_only": True,
            "transverse_y_positions_mm": y_positions,
            "arc_entities_mm": arc_entities,
            "arc_entity_count": len(arc_entities),
            "continuous_transverse_wires": True,
            "path_segment_count": len(segments),
            "strand_group_count": len(groups),
        }
        flat_bar_frame = None
        if raw.get("flat_bar_frame") is not None:
            flat_bar_result = cls._normalize_flat_bar_frame(raw.get("flat_bar_frame"), wire_mesh)
            if not flat_bar_result.get("success"):
                return flat_bar_result
            flat_bar_frame = flat_bar_result["flat_bar_frame"]
            wire_mesh["flat_bar_frame"] = flat_bar_frame
        return {
            "success": True,
            "wire_mesh": wire_mesh,
            "flat_bar_frame": flat_bar_frame,
            "path_segments_mm": segments,
            "groups": groups,
        }

    @classmethod
    def _normalize_flat_bar_frame(cls, raw: Any, wire_mesh: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": "wire_mesh flat_bar_frame must be an object."}

        def number(name: str, default: float | None = None) -> float | None:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        width = number("width_mm")
        thickness = number("thickness_mm")
        straight_length = number("straight_support_length_mm")
        curved_count = raw.get("curved_rail_count")
        straight_count = raw.get("straight_support_count")
        construction_style = str(raw.get("construction_style") or "continuous_rails").strip().lower()
        rail_segment_count = raw.get("rail_segment_count", int(wire_mesh["longitudinal_count"]) - 1)
        if any(value is None for value in (width, thickness, straight_length)):
            return {"success": False, "message": "flat_bar_frame requires width, thickness, and straight-support length in millimetres."}
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != float(value)
               for value in (curved_count, straight_count, rail_segment_count)):
            return {"success": False, "message": "flat_bar_frame rail and support counts must be integers."}
        curved_count = int(curved_count)
        straight_count = int(straight_count)
        rail_segment_count = int(rail_segment_count)
        if (
            raw.get("wire_seat_notches") is not None
            and abs(float(width) - 25.0) <= 0.01
            and abs(float(thickness) - 4.0) <= 0.01
        ):
            return cls._normalize_notched_flat_bar_grid(
                raw,
                wire_mesh,
                width=float(width),
                thickness=float(thickness),
                straight_length=float(straight_length),
                curved_count=curved_count,
                straight_count=straight_count,
            )
        if abs(float(width) - 20.0) > 0.01 or abs(float(thickness) - 3.0) > 0.01:
            return {"success": False, "message": "The production flat-bar frame currently supports only the bundled 20 x 3 mm profile."}
        if curved_count != 2 or straight_count != int(wire_mesh["longitudinal_count"]):
            return {"success": False, "message": "The current frame contract requires two curved rails and one straight support under each longitudinal wire."}
        if construction_style not in {"continuous_rails", "segmented_basket"}:
            return {"success": False, "message": "flat_bar_frame construction_style must be continuous_rails or segmented_basket."}
        if construction_style == "segmented_basket" and rail_segment_count != straight_count - 1:
            return {"success": False, "message": "segmented_basket requires one flat-bar rail chord between every adjacent wire station."}
        if abs(float(straight_length) - float(wire_mesh["mesh_height_mm"])) > 0.5:
            return {"success": False, "message": "flat_bar_frame straight-support length must match the 310 mm inner-wire length."}
        if str(raw.get("orientation") or "edge_on").lower() != "edge_on":
            return {"success": False, "message": "flat_bar_frame currently supports edge_on orientation only."}
        if str(raw.get("support_side") or "below_wire").lower() != "below_wire":
            return {"success": False, "message": "flat_bar_frame must be positioned below the wire mesh."}
        if str(raw.get("contact") or "tangent").lower() != "tangent":
            return {"success": False, "message": "flat_bar_frame currently supports tangent wire contact only."}

        wire_seat_notches = None
        replace_longitudinal_wires = bool(
            raw.get("replace_longitudinal_wires", bool(raw.get("wire_seat_notches")))
        )
        if raw.get("wire_seat_notches") is not None:
            notch_result = cls._normalize_wire_seat_notches(raw.get("wire_seat_notches"), wire_mesh)
            if not notch_result.get("success"):
                return notch_result
            wire_seat_notches = notch_result["wire_seat_notches"]
            if not replace_longitudinal_wires:
                return {
                    "success": False,
                    "message": "Notched flat-bar supports must replace the coincident longitudinal D4 wires.",
                }
        if wire_seat_notches is not None and raw.get("wire_clips") is not None:
            return {
                "success": False,
                "message": "wire_seat_notches and external wire_clips cannot be requested together.",
            }

        radius = float(wire_mesh["arc_radius_mm"])
        diameter = float(wire_mesh["wire_diameter_mm"])
        width = float(width)
        thickness = float(thickness)
        total_angle = float(wire_mesh["arc_length_mm"]) / radius
        half_angle = total_angle / 2.0
        if wire_seat_notches is not None:
            support_center_radius = (
                radius
                - diameter / 2.0
                + float(wire_seat_notches["depth_mm"])
                - width / 2.0
            )
        else:
            support_center_radius = radius - diameter - diameter / 2.0 - width / 2.0
        if support_center_radius <= 0:
            return {"success": False, "message": "flat_bar_frame dimensions leave no valid support radius."}

        segments: list[dict[str, Any]] = []
        arc_entities: list[dict[str, Any]] = []
        groups: list[list[int]] = []
        group_angles: list[float] = []
        group_corner_treatments: list[bool] = []
        rail_y_positions = [-float(wire_mesh["overall_height_mm"]) / 2.0, float(wire_mesh["overall_height_mm"]) / 2.0]
        support_angles = [
            -total_angle / 2.0 + index * total_angle / (straight_count - 1)
            for index in range(straight_count)
        ]
        support_points = [
            [
                support_center_radius * math.sin(angle),
                radius - support_center_radius * math.cos(angle),
            ]
            for angle in support_angles
        ]
        for y_value in rail_y_positions:
            rail_indexes: list[int] = []
            if construction_style == "continuous_rails":
                path_index = len(segments)
                start = [support_points[0][0], y_value, support_points[0][1]]
                end = [support_points[-1][0], y_value, support_points[-1][1]]
                midpoint = [0.0, y_value, radius - support_center_radius]
                segments.append({"start_mm": start, "end_mm": end})
                arc_entities.append({
                    "path_index": path_index,
                    "start_mm": start,
                    "end_mm": end,
                    "point_on_arc_mm": midpoint,
                })
                rail_indexes.append(path_index)
            else:
                for index in range(rail_segment_count):
                    first = support_points[index]
                    second = support_points[index + 1]
                    path_index = len(segments)
                    segments.append({
                        "start_mm": [first[0], y_value, first[1]],
                        "end_mm": [second[0], y_value, second[1]],
                    })
                    rail_indexes.append(path_index)
            groups.append(rail_indexes)
            group_angles.append(math.pi / 2.0)
            group_corner_treatments.append(construction_style == "segmented_basket")

        if wire_seat_notches is None:
            for angle, point in zip(support_angles, support_points):
                half_length = float(straight_length) / 2.0
                path_index = len(segments)
                segments.append({
                    "start_mm": [point[0], -half_length, point[1]],
                    "end_mm": [point[0], half_length, point[1]],
                })
                groups.append([path_index])
                group_angles.append(math.pi / 2.0 - angle)
                group_corner_treatments.append(False)

        mounting_tabs = raw.get("mounting_tabs")
        normalized_tabs = None
        if mounting_tabs is not None:
            if not isinstance(mounting_tabs, dict):
                return {"success": False, "message": "flat_bar_frame mounting_tabs must be an object."}
            tab_count = mounting_tabs.get("count")
            tab_width = mounting_tabs.get("width_mm")
            tab_thickness = mounting_tabs.get("thickness_mm")
            tab_height = mounting_tabs.get("height_mm")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (tab_count, tab_width, tab_thickness, tab_height)):
                return {"success": False, "message": "mounting_tabs requires numeric count, width, thickness, and height."}
            if int(tab_count) != 4 or abs(float(tab_width) - width) > 0.01 or abs(float(tab_thickness) - thickness) > 0.01:
                return {"success": False, "message": "The current frame contract requires four mounting tabs using the same 20 x 3 mm profile."}
            if float(tab_height) <= 0:
                return {"success": False, "message": "mounting-tab height must be positive."}
            normalized_tabs = {
                "count": 4,
                "width_mm": width,
                "thickness_mm": thickness,
                "height_mm": float(tab_height),
            }
            for y_value in rail_y_positions:
                for angle in (-half_angle, half_angle):
                    start_radius = support_center_radius
                    end_radius = support_center_radius - float(tab_height)
                    path_index = len(segments)
                    segments.append({
                        "start_mm": [
                            start_radius * math.sin(angle),
                            y_value,
                            radius - start_radius * math.cos(angle),
                        ],
                        "end_mm": [
                            end_radius * math.sin(angle),
                            y_value,
                            radius - end_radius * math.cos(angle),
                        ],
                    })
                    groups.append([path_index])
                    group_angles.append(math.pi / 2.0)
                    group_corner_treatments.append(False)

        wire_clips = None
        if raw.get("wire_clips") is not None:
            clip_result = cls._normalize_wire_clips(
                raw.get("wire_clips"),
                wire_mesh,
                {
                    "width_mm": width,
                    "support_center_radius_mm": support_center_radius,
                    "support_angles_rad": support_angles,
                },
            )
            if not clip_result.get("success"):
                return clip_result
            wire_clips = clip_result["wire_clips"]

        return {
            "success": True,
            "flat_bar_frame": {
                "width_mm": width,
                "thickness_mm": thickness,
                "profile_configuration": cls.FLAT_BAR_CONFIGURATION,
                "orientation": "edge_on",
                "support_side": "below_wire",
                "contact": "tangent",
                "construction_style": construction_style,
                "curved_rail_count": curved_count,
                "rail_segment_count": rail_segment_count,
                "rail_member_count": curved_count if construction_style == "continuous_rails" else curved_count * rail_segment_count,
                "straight_support_count": straight_count,
                "straight_support_length_mm": float(straight_length),
                "reference_arc_radius_mm": radius,
                "support_center_radius_mm": support_center_radius,
                "wire_to_support_center_offset_mm": radius - support_center_radius,
                "replace_longitudinal_wires": replace_longitudinal_wires,
                "curved_rail_y_positions_mm": rail_y_positions,
                "mounting_tabs": normalized_tabs,
                "wire_clips": wire_clips,
                "wire_seat_notches": wire_seat_notches,
                "notched_support_body_count": straight_count if wire_seat_notches else 0,
                "support_angles_rad": support_angles,
                "path_segments_mm": segments,
                "arc_entities_mm": arc_entities,
                "groups": groups,
                "group_angles_rad": group_angles,
                "group_corner_treatments": group_corner_treatments,
                "path_segment_count": len(segments),
                "expected_arc_entity_count": len(arc_entities),
                "expected_body_count": len(segments),
            },
        }

    @classmethod
    def _normalize_notched_flat_bar_grid(
        cls,
        raw: dict[str, Any],
        wire_mesh: dict[str, Any],
        *,
        width: float,
        thickness: float,
        straight_length: float,
        curved_count: int,
        straight_count: int,
    ) -> dict[str, Any]:
        """Normalize the drawing-specific 4-by-5 notched flat-bar lattice."""

        prototype_mode = str(raw.get("prototype_mode") or "").strip().lower()
        if bool(raw.get("sample_one_each", False)):
            prototype_mode = "one_each"
        if prototype_mode not in {"", "one_each", "joined_pair"}:
            return {
                "success": False,
                "message": "flat_bar_frame prototype_mode must be omitted, one_each, or joined_pair.",
            }

        if abs(width - 25.0) > 0.01 or abs(thickness - 4.0) > 0.01:
            return {
                "success": False,
                "message": "The notched flat-bar grid requires the drawing-specified 25 x 4 mm flat bar.",
            }
        if curved_count != 4 or straight_count != 5:
            return {
                "success": False,
                "message": "The notched flat-bar grid requires 4 transverse curved bars and 5 longitudinal straight bars.",
            }
        if abs(straight_length - 338.6) > 0.5:
            return {
                "success": False,
                "message": "The five longitudinal flat bars must use the drawing length 338.6 mm.",
            }
        if bool(raw.get("replace_longitudinal_wires", False)):
            return {
                "success": False,
                "message": "The notched flat-bar grid is added to the D4 mesh and must not replace any wire.",
            }
        if raw.get("wire_clips") is not None:
            return {
                "success": False,
                "message": "Integral rectangular seats and external wire clips cannot be requested together.",
            }
        if str(raw.get("orientation") or "edge_on").strip().lower() != "edge_on":
            return {"success": False, "message": "The notched 25 x 4 flat bars must use edge_on orientation."}
        if str(raw.get("support_side") or "below_wire").strip().lower() != "below_wire":
            return {"success": False, "message": "The notched flat-bar grid must remain below the D4 wires."}
        if str(raw.get("contact") or "seated").strip().lower() not in {"seated", "notched", "tangent"}:
            return {"success": False, "message": "The notched flat-bar grid contact must be seated."}

        rail_radius_value = raw.get("curved_rail_radius_mm", 265.0)
        rail_chord_value = raw.get("curved_rail_overall_width_mm", 281.31)
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (rail_radius_value, rail_chord_value)
        ):
            return {"success": False, "message": "Curved flat-bar radius and overall width must be numeric."}
        rail_radius = float(rail_radius_value)
        rail_chord = float(rail_chord_value)
        if abs(rail_radius - 265.0) > 0.5 or abs(rail_chord - 281.31) > 0.75:
            return {
                "success": False,
                "message": "The transverse flat bars must match the R265 / 281.31 mm drawing profile.",
            }
        if rail_chord >= 2.0 * rail_radius:
            return {"success": False, "message": "The curved flat-bar chord exceeds its diameter."}

        raw_pitch_chain = raw.get("curved_rail_pitch_chain_mm", [94.0, 140.0, 94.0])
        if (
            not isinstance(raw_pitch_chain, (list, tuple))
            or len(raw_pitch_chain) != curved_count - 1
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0.0 for value in raw_pitch_chain)
        ):
            return {
                "success": False,
                "message": "curved_rail_pitch_chain_mm must contain three positive pitches for the four transverse bars.",
            }
        pitch_chain = [float(value) for value in raw_pitch_chain]
        raw_y_positions = raw.get("curved_rail_y_positions_mm")
        if raw_y_positions is None:
            y_positions = [-sum(pitch_chain) / 2.0]
            for pitch in pitch_chain:
                y_positions.append(y_positions[-1] + pitch)
        elif (
            isinstance(raw_y_positions, (list, tuple))
            and len(raw_y_positions) == curved_count
            and all(not isinstance(value, bool) and isinstance(value, (int, float)) for value in raw_y_positions)
        ):
            y_positions = [float(value) for value in raw_y_positions]
        else:
            return {
                "success": False,
                "message": "curved_rail_y_positions_mm must contain four numeric positions.",
            }
        if any(abs(value) > straight_length / 2.0 for value in y_positions):
            return {"success": False, "message": "A transverse flat bar lies outside the 338.6 mm straight bars."}

        wire_angles = [float(value) for value in wire_mesh.get("longitudinal_station_angles_rad", [])]
        if len(wire_angles) != straight_count + 1:
            return {"success": False, "message": "The D4 mesh does not provide six wire stations for five flat bars."}
        support_angles = [
            (first + second) / 2.0
            for first, second in zip(wire_angles, wire_angles[1:])
        ]
        rail_half_angle = math.asin(rail_chord / (2.0 * rail_radius))
        model_segment_count = raw.get("rail_model_segment_count", 24)
        if (
            isinstance(model_segment_count, bool)
            or not isinstance(model_segment_count, (int, float))
            or int(model_segment_count) != float(model_segment_count)
            or not 8 <= int(model_segment_count) <= 64
        ):
            return {"success": False, "message": "rail_model_segment_count must be an integer from 8 to 64."}

        prototype_only = prototype_mode in {"one_each", "joined_pair"}
        created_straight_count = 1 if prototype_only else straight_count
        created_curved_count = 1 if prototype_only else curved_count

        notch_result = cls._normalize_wire_seat_notches(
            raw.get("wire_seat_notches"),
            wire_mesh,
            straight_count=created_straight_count,
            curved_count=created_curved_count,
        )
        if not notch_result.get("success"):
            return notch_result
        joint_result = cls._normalize_flat_bar_joint_notches(
            raw.get("flat_bar_joint_notches"),
            width=width,
            thickness=thickness,
            straight_count=straight_count,
            curved_count=curved_count,
            created_straight_count=created_straight_count,
            created_curved_count=created_curved_count,
        )
        if not joint_result.get("success"):
            return joint_result

        support_center_radius = float(wire_mesh["outer_surface_radius_mm"]) - width / 2.0
        straight_wire_slot_offsets = [16.25, 61.25, 115.25, 169.25, 223.25, 277.25, 322.25]
        straight_joint_slot_offsets = [5.25, 99.25, 239.25, 333.25]
        curved_wire_slot_x_offsets = [-131.624232, -81.540869, -27.55, 27.55, 81.540869, 131.624233]
        curved_joint_slot_angles = [
            math.radians(value)
            for value in (-23.671200097, -11.858883028, 0.0, 11.858886655, 23.671200097)
        ]
        frame = {
            "body_mode": "notched_flat_bar_grid",
            "prototype_mode": prototype_mode or None,
            "prototype_only": prototype_only,
            "source_drawing": "active_dwg",
            "width_mm": width,
            "thickness_mm": thickness,
            "profile_configuration": None,
            "orientation": "edge_on",
            "support_side": "below_wire",
            "contact": "seated",
            "construction_style": "notched_flat_bar_grid_4x5",
            "curved_rail_count": curved_count,
            "curved_rail_radius_mm": rail_radius,
            "curved_rail_outer_radius_mm": rail_radius + width,
            "curved_rail_overall_width_mm": rail_chord,
            "curved_rail_half_angle_rad": rail_half_angle,
            "curved_rail_pitch_chain_mm": pitch_chain,
            "curved_rail_y_positions_mm": y_positions,
            "rail_model_segment_count": int(model_segment_count),
            "straight_support_count": straight_count,
            "straight_support_length_mm": straight_length,
            "created_straight_support_count": created_straight_count,
            "created_curved_rail_count": created_curved_count,
            "straight_wire_slot_center_offsets_from_top_mm": straight_wire_slot_offsets,
            "straight_joint_slot_center_offsets_from_top_mm": straight_joint_slot_offsets,
            "curved_wire_slot_x_offsets_mm": curved_wire_slot_x_offsets,
            "curved_joint_slot_angles_rad": curved_joint_slot_angles,
            "support_angles_rad": support_angles,
            "support_center_radius_mm": support_center_radius,
            "replace_longitudinal_wires": False,
            "wire_seat_notches": notch_result["wire_seat_notches"],
            "flat_bar_joint_notches": joint_result["flat_bar_joint_notches"],
            "notched_support_body_count": created_straight_count,
            "notched_curved_rail_body_count": created_curved_count,
            "expected_imported_body_count": created_straight_count + created_curved_count,
            "path_segments_mm": [],
            "arc_entities_mm": [],
            "groups": [],
            "group_angles_rad": [],
            "group_corner_treatments": [],
            "path_segment_count": 0,
            "expected_arc_entity_count": 0,
            "expected_body_count": 0,
        }
        return {"success": True, "flat_bar_frame": frame}

    @classmethod
    def _normalize_wire_seat_notches(
        cls,
        raw: Any,
        wire_mesh: dict[str, Any],
        *,
        straight_count: int | None = None,
        curved_count: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": "flat_bar_frame wire_seat_notches must be an object."}

        if straight_count is None and curved_count is None:
            width = raw.get("width_mm")
            depth = raw.get("depth_mm")
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (width, depth)
            ):
                return {"success": False, "message": "Legacy wire seats require numeric width_mm and depth_mm."}
            width = float(width)
            depth = float(depth)
            diameter = float(wire_mesh["wire_diameter_mm"])
            if not diameter <= width <= diameter + 2.0 or not 0.0 < depth <= diameter:
                return {"success": False, "message": "Legacy shallow wire-seat dimensions are outside the supported range."}
            if str(raw.get("style") or "").strip().lower() != "shallow_rectangular_edge_notch":
                return {"success": False, "message": "Legacy wire seats require shallow_rectangular_edge_notch style."}
            if str(raw.get("placement") or "").strip().lower() != "all_support_transverse_intersections":
                return {"success": False, "message": "Legacy wire seats must cover all support/transverse intersections."}
            support_count = int(wire_mesh["longitudinal_count"])
            notch_count_per_support = int(wire_mesh["transverse_count"])
            notch_count = support_count * notch_count_per_support
            return {
                "success": True,
                "wire_seat_notches": {
                    "style": "shallow_rectangular_edge_notch",
                    "placement": "all_support_transverse_intersections",
                    "width_mm": width,
                    "depth_mm": depth,
                    "clearance_mm": width - diameter,
                    "support_count": support_count,
                    "notch_count_per_support": notch_count_per_support,
                    "notch_count": notch_count,
                    "transverse_y_positions_mm": list(wire_mesh["transverse_y_positions_mm"]),
                    "minimum_support_face_count": 6 + 3 * notch_count_per_support,
                },
            }

        width = raw.get("width_mm", 4.2)
        straight_depth = raw.get("straight_depth_mm", raw.get("depth_mm", 12.0))
        curved_depth = raw.get("curved_depth_mm", 12.0)
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (width, straight_depth, curved_depth)
        ):
            return {
                "success": False,
                "message": "wire_seat_notches requires numeric width and straight/curved depths.",
            }
        width = float(width)
        straight_depth = float(straight_depth)
        curved_depth = float(curved_depth)
        diameter = float(wire_mesh["wire_diameter_mm"])
        if abs(width - 4.2) > 0.01:
            return {
                "success": False,
                "message": "The active DWG specifies 4.2 mm rectangular wire seats for D4 wire.",
            }
        if abs(straight_depth - 12.0) > 0.01 or abs(curved_depth - 12.0) > 0.01:
            return {
                "success": False,
                "message": "The active DWG wire seats must use 12 mm depth on both flat-bar types.",
            }
        placement = str(raw.get("placement") or "all_flat_bar_wire_contacts").strip().lower()
        if placement not in {"all_flat_bar_wire_contacts", "all_support_transverse_intersections"}:
            return {
                "success": False,
                "message": "wire_seat_notches must cover every flat-bar/wire contact.",
            }
        support_count = int(straight_count if straight_count is not None else wire_mesh["longitudinal_count"])
        rail_count = int(curved_count or 0)
        straight_notch_count_per_support = int(wire_mesh["transverse_count"])
        curved_notch_count_per_rail = int(wire_mesh["longitudinal_count"])
        straight_notch_count = support_count * straight_notch_count_per_support
        curved_notch_count = rail_count * curved_notch_count_per_rail
        notch_count = straight_notch_count + curved_notch_count
        return {
            "success": True,
            "wire_seat_notches": {
                "style": "drawing_rectangular_edge_notch",
                "placement": "all_flat_bar_wire_contacts",
                "width_mm": width,
                "clearance_mm": width - diameter,
                "exact_wire_diameter_fit": False,
                "fit": "dwg_clearance_fit",
                "straight_depth_mm": straight_depth,
                "curved_depth_mm": curved_depth,
                "support_count": support_count,
                "curved_rail_count": rail_count,
                "notch_count_per_support": straight_notch_count_per_support,
                "notch_count_per_curved_rail": curved_notch_count_per_rail,
                "straight_notch_count": straight_notch_count,
                "curved_notch_count": curved_notch_count,
                "notch_count": notch_count,
                "transverse_y_positions_mm": list(wire_mesh["transverse_y_positions_mm"]),
                "longitudinal_station_angles_rad": list(wire_mesh["longitudinal_station_angles_rad"]),
                "minimum_support_face_count": 6 + 3 * straight_notch_count_per_support,
                "minimum_curved_rail_face_count": 6 + 3 * curved_notch_count_per_rail,
            },
        }

    @staticmethod
    def _normalize_flat_bar_joint_notches(
        raw: Any,
        *,
        width: float,
        thickness: float,
        straight_count: int,
        curved_count: int,
        created_straight_count: int | None = None,
        created_curved_count: int | None = None,
    ) -> dict[str, Any]:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            return {"success": False, "message": "flat_bar_joint_notches must be an object."}
        slot_width = raw.get("width_mm", thickness)
        straight_depth = raw.get("straight_depth_mm", 13.0)
        curved_depth = raw.get("curved_depth_mm", 13.0)
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (slot_width, straight_depth, curved_depth)
        ):
            return {"success": False, "message": "Flat-bar joint notch dimensions must be numeric."}
        slot_width = float(slot_width)
        straight_depth = float(straight_depth)
        curved_depth = float(curved_depth)
        if abs(slot_width - thickness) > 0.01:
            return {"success": False, "message": "Flat-bar joint notch width must equal the 4 mm mating thickness."}
        if abs(straight_depth - 13.0) > 0.01 or abs(curved_depth - 13.0) > 0.01:
            return {"success": False, "message": "The active DWG specifies 13 mm flat-bar joint slots on both part types."}
        created_straight_count = int(created_straight_count or straight_count)
        created_curved_count = int(created_curved_count or curved_count)
        mating_curved_rail_indices = list(range(curved_count))
        joint_count = straight_count * curved_count
        straight_notch_count_per_support = curved_count
        curved_notch_count_per_rail = straight_count
        straight_notch_count = created_straight_count * straight_notch_count_per_support
        curved_notch_count = created_curved_count * curved_notch_count_per_rail
        return {
            "success": True,
            "flat_bar_joint_notches": {
                "style": "dwg_rectangular_interlocking_slots",
                "placement": "all_flat_bar_intersections",
                "width_mm": slot_width,
                "straight_depth_mm": straight_depth,
                "curved_depth_mm": curved_depth,
                "straight_notch_count_per_support": straight_notch_count_per_support,
                "curved_notch_count_per_rail": curved_notch_count_per_rail,
                "mating_curved_rail_indices": mating_curved_rail_indices,
                "joint_count": joint_count,
                "created_joint_pair_count": min(straight_notch_count, curved_notch_count),
                "straight_notch_count": straight_notch_count,
                "curved_notch_count": curved_notch_count,
                "notch_count": straight_notch_count + curved_notch_count,
            },
        }

    @classmethod
    def _normalize_wire_clips(
        cls,
        raw: Any,
        wire_mesh: dict[str, Any],
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"success": False, "message": "flat_bar_frame wire_clips must be an object."}

        def number(name: str) -> float | None:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        width = number("width_mm")
        thickness = number("thickness_mm")
        clearance = number("clearance_mm")
        if any(value is None for value in (width, thickness, clearance)):
            return {"success": False, "message": "wire_clips requires numeric width_mm, thickness_mm, and clearance_mm."}
        if abs(float(width) - 8.0) > 0.01 or abs(float(thickness) - 2.0) > 0.01:
            return {"success": False, "message": "The production wire-clip executor currently supports the bundled 8 x 2 mm profile only."}
        if not 0.0 <= float(clearance) <= 2.0:
            return {"success": False, "message": "wire_clips clearance_mm must be between 0 and 2 mm."}
        if str(raw.get("style") or "single_hook").strip().lower() != "single_hook":
            return {"success": False, "message": "wire_clips currently supports single_hook style only."}
        if str(raw.get("placement") or "").strip().lower() != "all_support_wire_intersections":
            return {"success": False, "message": "wire_clips must use all_support_wire_intersections placement."}

        radius = float(wire_mesh["arc_radius_mm"])
        wire_radius = float(wire_mesh["wire_diameter_mm"]) / 2.0
        support_radius = float(frame["support_center_radius_mm"])
        frame_width = float(frame["width_mm"])
        clip_width = float(width)
        clip_thickness = float(thickness)
        clip_clearance = float(clearance)
        y_half_span = wire_radius + clip_clearance + clip_thickness / 2.0
        leg_base_radius = support_radius + frame_width / 2.0 - 2.0
        hook_radius = radius + wire_radius + clip_clearance + clip_thickness / 2.0

        segments: list[dict[str, Any]] = []
        groups: list[list[int]] = []
        group_angles: list[float] = []
        intersections: list[dict[str, Any]] = []

        def point(angle: float, y_value: float, radial_value: float) -> list[float]:
            return [
                radial_value * math.sin(angle),
                y_value,
                radius - radial_value * math.cos(angle),
            ]

        support_angles = list(frame["support_angles_rad"])
        for support_index, angle in enumerate(support_angles):
            for wire_index, y_value in enumerate(wire_mesh["transverse_y_positions_mm"]):
                outer_y = float(y_value) + y_half_span
                inner_y = float(y_value) - y_half_span
                leg_index = len(segments)
                segments.append({
                    "start_mm": point(angle, outer_y, leg_base_radius),
                    "end_mm": point(angle, outer_y, hook_radius),
                })
                hook_index = len(segments)
                segments.append({
                    "start_mm": point(angle, outer_y, hook_radius),
                    "end_mm": point(angle, inner_y, hook_radius),
                })
                groups.append([leg_index, hook_index])
                group_angles.append(-angle)
                intersections.append({
                    "support_index": support_index,
                    "wire_index": wire_index,
                    "angle_rad": angle,
                    "y_mm": float(y_value),
                })

        clip_count = len(intersections)
        expected_count = int(wire_mesh["longitudinal_count"]) * int(wire_mesh["transverse_count"])
        if clip_count != expected_count:
            return {"success": False, "message": "wire_clips did not cover every support/wire intersection."}
        return {
            "success": True,
            "wire_clips": {
                "style": "single_hook",
                "placement": "all_support_wire_intersections",
                "width_mm": clip_width,
                "thickness_mm": clip_thickness,
                "clearance_mm": clip_clearance,
                "profile_configuration": cls.WIRE_CLIP_CONFIGURATION,
                "clip_count": clip_count,
                "calculated_hook_span_mm": y_half_span * 2.0,
                "calculated_leg_height_mm": hook_radius - leg_base_radius,
                "path_segments_mm": segments,
                "groups": groups,
                "group_angles_rad": group_angles,
                "path_segment_count": len(segments),
                "minimum_body_count": clip_count,
                "intersections": intersections,
            },
        }

    @classmethod
    def _normalize_segments(cls, params: dict[str, Any]) -> dict[str, Any]:
        raw_segments = params.get("path_segments_mm") or params.get("segments")
        if raw_segments is None:
            points = params.get("path_points_mm") or params.get("path_points")
            if not isinstance(points, list) or len(points) < 2:
                return {"success": False, "message": "weldment requires path_segments_mm or at least two path_points_mm."}
            raw_segments = [[first, second] for first, second in zip(points, points[1:])]
            if bool(params.get("closed", False)):
                raw_segments.append([points[-1], points[0]])
        if not isinstance(raw_segments, list) or not raw_segments:
            return {"success": False, "message": "weldment path_segments_mm must be a non-empty list."}

        result: list[dict[str, list[float]]] = []
        for index, item in enumerate(raw_segments):
            if isinstance(item, dict):
                start = cls._point3(item.get("start_mm") or item.get("start"))
                end = cls._point3(item.get("end_mm") or item.get("end"))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                start = cls._point3(item[0])
                end = cls._point3(item[1])
            else:
                start = end = None
            if start is None or end is None:
                return {"success": False, "message": f"weldment segment {index + 1} requires two 3D points."}
            if math.dist(start, end) <= 1e-6:
                return {"success": False, "message": f"weldment segment {index + 1} has zero length."}
            result.append({"start_mm": start, "end_mm": end})
        return {"success": True, "path_segments_mm": result}

    def _create_weldment(
        self,
        model: Any,
        module: Any,
        app: Any,
        feature: dict[str, Any],
        request: dict[str, Any],
        profile_path: Path | None,
        flat_bar_profile_path: Path | None = None,
        wire_clip_profile_path: Path | None = None,
    ) -> dict[str, Any]:
        import pythoncom
        from win32com.client import VARIANT

        feature_manager = module.IFeatureManager(model.FeatureManager._oleobj_)
        sketch_manager = module.ISketchManager(model.SketchManager._oleobj_)
        weldment_environment = feature_manager.InsertWeldmentFeature()
        if weldment_environment is None:
            return {"success": False, "message": "SolidWorks failed to initialize the weldment environment."}

        if request.get("geometry_kind") == "flat_bar_prototype":
            prototype_operation = self._create_notched_flat_bar_supports(
                model,
                module,
                app,
                request["flat_bar_frame"],
                request["wire_seat_notches"],
                request["wire_mesh"],
            )
            if not prototype_operation.get("success"):
                return prototype_operation
            name = str(feature.get("name") or "FlatBarPrototypeOneEach")
            ActiveModelFeatureSkill._name_feature(weldment_environment, name)
            model.ForceRebuild3(False)
            ActiveModelThroughHoleExecutor._com_member(model, "GraphicsRedraw2")
            return {
                "success": True,
                "name": name,
                "type": self.FEATURE_TYPE,
                "mode": "new_model",
                "request": request,
                "feature_name": ActiveModelFeatureSkill._feature_name(weldment_environment),
                "profile_path": None,
                "profile_configuration": None,
                "path_segment_count": 0,
                "arc_entity_count": 0,
                "group_count": 0,
                "path_sketch": "",
                "flat_bar_frame": None,
                "wire_seat_notches": prototype_operation,
                "wire_clips": None,
            }

        sketch_manager.Insert3DSketch(True)
        segments: list[Any] = []
        arc_entities = {
            int(item["path_index"]): item
            for item in (request.get("wire_mesh") or {}).get("arc_entities_mm", [])
        }
        for index, item in enumerate(request["path_segments_mm"]):
            first = item["start_mm"]
            second = item["end_mm"]
            arc = arc_entities.get(index)
            if arc is not None:
                point = arc["point_on_arc_mm"]
                segment = sketch_manager.Create3PointArc(
                    self._m(first[0]), self._m(first[1]), self._m(first[2]),
                    self._m(second[0]), self._m(second[1]), self._m(second[2]),
                    self._m(point[0]), self._m(point[1]), self._m(point[2]),
                )
            else:
                segment = sketch_manager.CreateLine(
                    self._m(first[0]), self._m(first[1]), self._m(first[2]),
                    self._m(second[0]), self._m(second[1]), self._m(second[2]),
                )
            if segment is None:
                sketch_manager.Insert3DSketch(True)
                return {"success": False, "message": "SolidWorks failed to create a weldment path entity."}
            segments.append(segment)
        sketch_manager.Insert3DSketch(True)

        groups: list[Any] = []
        corner_types = {"miter": 1, "butt1": 2, "butt2": 3}
        for indexes in request["groups"]:
            group = feature_manager.CreateStructuralMemberGroup()
            if group is None:
                return {"success": False, "message": "SolidWorks failed to create a structural-member group."}
            group.Segments = VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                [segments[index] for index in indexes],
            )
            group.ApplyCornerTreatment = True
            group.CornerTreatmentType = corner_types[request["corner_treatment"]]
            group.MergeArcSegmentBodies = request["merge_arc_segment_bodies"]
            groups.append(group)

        group_array = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, groups)
        created = feature_manager.InsertStructuralWeldment5(
            str(profile_path),
            1,
            request["allow_protrusion"],
            group_array,
            request["profile_configuration"],
        )
        if created is None:
            return {
                "success": False,
                "message": "SolidWorks InsertStructuralWeldment5 returned no feature. Check the profile configuration.",
                "profile_path": str(profile_path),
                "profile_configuration": request["profile_configuration"],
            }
        name = str(feature.get("name") or "WeldmentStructuralMember")
        ActiveModelFeatureSkill._name_feature(created, name)
        path_sketch = self._last_feature_of_type(model, "3DProfileFeature")
        flat_bar_operation = None
        wire_seat_notch_operation = None
        wire_clip_operation = None
        if (
            request.get("flat_bar_frame")
            and request["flat_bar_frame"].get("body_mode") != "notched_flat_bar_grid"
        ):
            if flat_bar_profile_path is None:
                return {"success": False, "message": "The flat-bar profile path was not resolved."}
            flat_bar_operation = self._create_flat_bar_frame(
                model,
                module,
                app,
                request["flat_bar_frame"],
                flat_bar_profile_path,
            )
            if not flat_bar_operation.get("success"):
                return flat_bar_operation
        if request.get("wire_seat_notches"):
            wire_seat_notch_operation = self._create_notched_flat_bar_supports(
                model,
                module,
                app,
                request["flat_bar_frame"],
                request["wire_seat_notches"],
                request["wire_mesh"],
            )
            if not wire_seat_notch_operation.get("success"):
                return wire_seat_notch_operation
        if request.get("wire_clips"):
            if wire_clip_profile_path is None:
                return {"success": False, "message": "The wire-clip profile path was not resolved."}
            wire_clip_operation = self._create_wire_clips(
                model,
                module,
                request["wire_clips"],
                wire_clip_profile_path,
            )
            if not wire_clip_operation.get("success"):
                return wire_clip_operation
        model.ForceRebuild3(False)
        self._hide_model_sketches(model)
        ActiveModelThroughHoleExecutor._com_member(model, "GraphicsRedraw2")
        return {
            "success": True,
            "name": name,
            "type": self.FEATURE_TYPE,
            "mode": "new_model",
            "request": request,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "profile_path": str(profile_path),
            "profile_configuration": request["profile_configuration"],
            "path_segment_count": len(segments),
            "arc_entity_count": len(arc_entities),
            "group_count": len(groups),
            "path_sketch": str(ActiveModelThroughHoleExecutor._com_member(path_sketch, "Name", default="") or ""),
            "flat_bar_frame": flat_bar_operation,
            "wire_seat_notches": wire_seat_notch_operation,
            "wire_clips": wire_clip_operation,
        }

    def _create_flat_bar_frame(
        self,
        model: Any,
        module: Any,
        app: Any,
        frame: dict[str, Any],
        profile_path: Path,
    ) -> dict[str, Any]:
        import pythoncom
        from win32com.client import VARIANT

        feature_manager = module.IFeatureManager(model.FeatureManager._oleobj_)
        sketch_manager = module.ISketchManager(model.SketchManager._oleobj_)
        sketch_manager.Insert3DSketch(True)
        segments: list[Any] = []
        arc_entities = {int(item["path_index"]): item for item in frame.get("arc_entities_mm", [])}
        for index, item in enumerate(frame["path_segments_mm"]):
            first = item["start_mm"]
            second = item["end_mm"]
            arc = arc_entities.get(index)
            if arc is not None:
                point = arc["point_on_arc_mm"]
                segment = sketch_manager.Create3PointArc(
                    self._m(first[0]), self._m(first[1]), self._m(first[2]),
                    self._m(second[0]), self._m(second[1]), self._m(second[2]),
                    self._m(point[0]), self._m(point[1]), self._m(point[2]),
                )
            else:
                segment = sketch_manager.CreateLine(
                    self._m(first[0]), self._m(first[1]), self._m(first[2]),
                    self._m(second[0]), self._m(second[1]), self._m(second[2]),
                )
            if segment is None:
                sketch_manager.Insert3DSketch(True)
                return {"success": False, "message": "SolidWorks failed to create a flat-bar frame path entity."}
            segments.append(segment)
        sketch_manager.Insert3DSketch(True)

        groups: list[Any] = []
        applied_angles: list[float] = []
        requested_angles = list(frame.get("group_angles_rad") or [])
        requested_corner_treatments = list(frame.get("group_corner_treatments") or [])
        for group_index, indexes in enumerate(frame["groups"]):
            group = feature_manager.CreateStructuralMemberGroup()
            if group is None:
                return {"success": False, "message": "SolidWorks failed to create a flat-bar structural-member group."}
            group.Segments = VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                [segments[index] for index in indexes],
            )
            apply_corner_treatment = bool(
                requested_corner_treatments[group_index]
                if group_index < len(requested_corner_treatments)
                else False
            )
            group.ApplyCornerTreatment = apply_corner_treatment
            if apply_corner_treatment:
                group.CornerTreatmentType = 1
            group.MergeArcSegmentBodies = False
            angle = float(requested_angles[group_index]) if group_index < len(requested_angles) else 0.0
            try:
                group.Angle = angle
                applied_angles.append(float(group.Angle))
            except Exception as exc:
                return {
                    "success": False,
                    "message": f"SolidWorks rejected flat-bar profile orientation for group {group_index + 1}: {exc}",
                }
            groups.append(group)

        created = feature_manager.InsertStructuralWeldment5(
            str(profile_path),
            1,
            False,
            VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, groups),
            str(frame["profile_configuration"]),
        )
        if created is None:
            return {
                "success": False,
                "message": "SolidWorks could not create the 20 x 3 mm flat-bar structural members.",
                "profile_path": str(profile_path),
                "profile_configuration": frame["profile_configuration"],
            }
        ActiveModelFeatureSkill._name_feature(created, "FlatBarFrame20x3")
        return {
            "success": True,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "profile_path": str(profile_path),
            "profile_configuration": frame["profile_configuration"],
            "path_segment_count": len(segments),
            "arc_entity_count": len(arc_entities),
            "group_count": len(groups),
            "applied_group_angles_rad": applied_angles,
        }

    def _create_notched_flat_bar_prototypes(
        self,
        model: Any,
        module: Any,
        app: Any,
        frame: dict[str, Any],
        notches: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one straight and one continuous curved DWG-derived flat bar."""

        import pythoncom
        from win32com.client import VARIANT

        if app is None:
            return {"success": False, "message": "SolidWorks is unavailable for flat-bar prototype creation."}
        modeler_raw = ActiveModelThroughHoleExecutor._com_member(app, "GetModeler")
        math_utility_raw = ActiveModelThroughHoleExecutor._com_member(app, "GetMathUtility")
        if modeler_raw is None or math_utility_raw is None:
            return {"success": False, "message": "SolidWorks Modeler or MathUtility is unavailable."}
        try:
            modeler = module.IModeler(modeler_raw._oleobj_)
            math_utility = module.IMathUtility(math_utility_raw._oleobj_)
            part = module.IPartDoc(model._oleobj_)
        except Exception as exc:
            return {"success": False, "message": f"SolidWorks body interfaces are unavailable: {exc}"}

        sw_body_cut = 15902
        create_feature_check = 1
        overshoot = 0.20
        width = float(frame["width_mm"])
        thickness = float(frame["thickness_mm"])
        straight_length = float(frame["straight_support_length_mm"])
        inner_radius = float(frame["curved_rail_radius_mm"])
        outer_radius = float(frame["curved_rail_outer_radius_mm"])
        chord = float(frame["curved_rail_overall_width_mm"])
        wire_slot_width = float(notches["width_mm"])
        wire_slot_depth = float(notches["straight_depth_mm"])
        joints = frame.get("flat_bar_joint_notches") or {}
        joint_slot_width = float(joints.get("width_mm", thickness))
        joint_slot_depth = float(joints.get("straight_depth_mm", 13.0))
        curved_joint_slot_depth = float(joints.get("curved_depth_mm", 13.0))
        prototype_mode = str(frame.get("prototype_mode") or "one_each")
        joined_pair = prototype_mode == "joined_pair"
        straight_center = [0.0, 0.0, 0.0] if joined_pair else [-175.0, 0.0, 0.0]
        curved_center = [0.0, 0.0, 0.0] if joined_pair else [175.0, 220.0, 0.0]

        def vector(values: list[float]) -> Any:
            return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, values)

        def transform_array(
            x_axis: list[float],
            y_axis: list[float],
            z_axis: list[float],
            center_mm: list[float],
        ) -> Any:
            data = [
                *x_axis,
                *y_axis,
                *z_axis,
                self._m(center_mm[0]),
                self._m(center_mm[1]),
                self._m(center_mm[2]),
                1.0,
                0.0,
                0.0,
                0.0,
            ]
            return math_utility.CreateTransform(vector(data))

        def oriented_box(
            center_mm: list[float],
            x_axis: list[float],
            y_axis: list[float],
            z_axis: list[float],
            width_mm: float,
            length_mm: float,
            height_mm: float,
        ) -> Any:
            body = modeler.CreateBodyFromBox3(vector([
                0.0,
                0.0,
                -self._m(height_mm) / 2.0,
                0.0,
                0.0,
                1.0,
                self._m(width_mm),
                self._m(length_mm),
                self._m(height_mm),
            ]))
            if body is None:
                return None
            transform = transform_array(x_axis, y_axis, z_axis, center_mm)
            if transform is None or not body.ApplyTransform(transform):
                return None
            return body

        def cut_box(
            target: Any,
            *,
            center_mm: list[float],
            x_axis: list[float],
            y_axis: list[float],
            width_mm: float,
            length_mm: float,
            label: str,
        ) -> tuple[Any | None, str | None]:
            tool = oriented_box(
                center_mm,
                x_axis,
                y_axis,
                [0.0, 0.0, 1.0],
                width_mm,
                length_mm,
                thickness + 2.0 * overshoot,
            )
            if tool is None:
                return None, f"SolidWorks failed to create the {label} cutting body."
            cut_bodies, error_code = target.Operations2(sw_body_cut, tool, 0)
            if int(error_code) != 0 or not cut_bodies:
                return None, f"SolidWorks failed to cut {label} (error={error_code})."
            return cut_bodies[0], None

        straight_body = oriented_box(
            straight_center,
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            width,
            straight_length,
            thickness,
        )
        if straight_body is None:
            return {"success": False, "message": "SolidWorks failed to create the straight 25 x 4 flat bar."}

        straight_wire_offsets = [
            float(value)
            for value in frame.get("straight_wire_slot_center_offsets_from_top_mm", [])
        ]
        straight_joint_offsets = [
            float(value)
            for value in frame.get("straight_joint_slot_center_offsets_from_top_mm", [])
        ]
        for index, offset in enumerate(straight_wire_offsets, start=1):
            y_value = straight_center[1] + straight_length / 2.0 - offset
            x_value = straight_center[0] - width / 2.0 + wire_slot_depth / 2.0 - overshoot / 2.0
            straight_body, error = cut_box(
                straight_body,
                center_mm=[x_value, y_value, 0.0],
                x_axis=[1.0, 0.0, 0.0],
                y_axis=[0.0, 1.0, 0.0],
                width_mm=wire_slot_depth + overshoot,
                length_mm=wire_slot_width,
                label=f"straight wire slot {index}",
            )
            if straight_body is None:
                return {"success": False, "message": error or "Straight wire-slot cut failed."}
        for index, offset in enumerate(straight_joint_offsets, start=1):
            y_value = straight_center[1] + straight_length / 2.0 - offset
            x_value = straight_center[0] + width / 2.0 - joint_slot_depth / 2.0 + overshoot / 2.0
            straight_body, error = cut_box(
                straight_body,
                center_mm=[x_value, y_value, 0.0],
                x_axis=[1.0, 0.0, 0.0],
                y_axis=[0.0, 1.0, 0.0],
                width_mm=joint_slot_depth + overshoot,
                length_mm=joint_slot_width,
                label=f"straight flat-bar joint slot {index}",
            )
            if straight_body is None:
                return {"success": False, "message": error or "Straight joint-slot cut failed."}

        def point(x_mm: float, y_mm: float, z_mm: float = -thickness / 2.0) -> list[float]:
            return [self._m(x_mm), self._m(y_mm), self._m(z_mm)]

        half_chord = chord / 2.0
        if half_chord >= inner_radius:
            return {"success": False, "message": "The curved flat-bar chord exceeds the inner diameter."}
        inner_y = -math.sqrt(inner_radius * inner_radius - half_chord * half_chord)
        outer_y = -math.sqrt(outer_radius * outer_radius - half_chord * half_chord)
        cx, cy = curved_center[0], curved_center[1]
        plane_surface = modeler.CreatePlanarSurface2(
            vector(point(cx, cy)),
            vector([0.0, 0.0, 1.0]),
            vector([1.0, 0.0, 0.0]),
        )
        if plane_surface is None:
            return {"success": False, "message": "SolidWorks failed to create the curved flat-bar profile plane."}

        def trimmed_line(start_mm: list[float], end_mm: list[float]) -> Any:
            start_m = point(start_mm[0], start_mm[1])
            end_m = point(end_mm[0], end_mm[1])
            direction = [end_m[i] - start_m[i] for i in range(3)]
            curve = modeler.CreateLine(vector(start_m), vector(direction))
            if curve is None:
                return None
            return curve.CreateTrimmedCurve2(*start_m, *end_m)

        def trimmed_arc(radius_mm: float, start_mm: list[float], end_mm: list[float], axis_z: float) -> Any:
            center_m = point(cx, cy)
            start_m = point(start_mm[0], start_mm[1])
            end_m = point(end_mm[0], end_mm[1])
            curve = modeler.CreateArc(
                vector(center_m),
                vector([0.0, 0.0, axis_z]),
                self._m(radius_mm),
                vector(start_m),
                vector(end_m),
            )
            if curve is None:
                return None
            return curve.CreateTrimmedCurve2(*start_m, *end_m)

        left_outer = [cx - half_chord, cy + outer_y]
        right_outer = [cx + half_chord, cy + outer_y]
        left_inner = [cx - half_chord, cy + inner_y]
        right_inner = [cx + half_chord, cy + inner_y]
        boundary_curves = [
            trimmed_arc(outer_radius, left_outer, right_outer, 1.0),
            trimmed_line(right_outer, right_inner),
            trimmed_arc(inner_radius, right_inner, left_inner, -1.0),
            trimmed_line(left_inner, left_outer),
        ]
        if any(curve is None for curve in boundary_curves):
            return {"success": False, "message": "SolidWorks failed to build the continuous annular-sector boundary."}
        sheet_body = plane_surface.CreateTrimmedSheet4(
            VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, boundary_curves),
            True,
        )
        if sheet_body is None:
            return {"success": False, "message": "SolidWorks failed to trim the continuous curved flat-bar profile."}
        extrusion_direction = math_utility.CreateVector(vector([0.0, 0.0, 1.0]))
        curved_body = modeler.CreateExtrudedBody(sheet_body, extrusion_direction, self._m(thickness))
        if curved_body is None:
            return {"success": False, "message": "SolidWorks failed to extrude the continuous curved flat bar."}

        curved_wire_offsets = [float(value) for value in frame.get("curved_wire_slot_x_offsets_mm", [])]
        for index, x_offset in enumerate(curved_wire_offsets, start=1):
            if abs(x_offset) >= inner_radius:
                return {"success": False, "message": f"Curved wire slot {index} lies outside R265."}
            opening_y = cy - math.sqrt(inner_radius * inner_radius - x_offset * x_offset)
            bottom_y = cy - math.sqrt(
                (inner_radius + wire_slot_depth) ** 2 - x_offset * x_offset
            )
            curved_body, error = cut_box(
                curved_body,
                center_mm=[cx + x_offset, (opening_y + bottom_y) / 2.0, 0.0],
                x_axis=[1.0, 0.0, 0.0],
                y_axis=[0.0, 1.0, 0.0],
                width_mm=wire_slot_width,
                length_mm=abs(opening_y - bottom_y) + 2.0 * overshoot,
                label=f"curved wire slot {index}",
            )
            if curved_body is None:
                return {"success": False, "message": error or "Curved wire-slot cut failed."}

        curved_joint_angles = [float(value) for value in frame.get("curved_joint_slot_angles_rad", [])]
        for index, angle in enumerate(curved_joint_angles, start=1):
            radial = [math.sin(angle), -math.cos(angle), 0.0]
            tangent = [math.cos(angle), math.sin(angle), 0.0]
            center_radius = inner_radius + curved_joint_slot_depth / 2.0
            curved_body, error = cut_box(
                curved_body,
                center_mm=[
                    cx + center_radius * radial[0],
                    cy + center_radius * radial[1],
                    0.0,
                ],
                x_axis=tangent,
                y_axis=radial,
                width_mm=joint_slot_width,
                length_mm=curved_joint_slot_depth + 2.0 * overshoot,
                label=f"curved flat-bar joint slot {index}",
            )
            if curved_body is None:
                return {"success": False, "message": error or "Curved joint-slot cut failed."}

        joined_center_world_mm: list[float] | None = None
        if joined_pair:
            if len(straight_joint_offsets) < 2 or len(curved_joint_angles) < 3:
                return {"success": False, "message": "The joined prototype requires the second straight slot and center curved slot."}
            joined_y = straight_length / 2.0 - straight_joint_offsets[1]
            joined_z = -(inner_radius + outer_radius) / 2.0
            straight_transform = transform_array(
                [0.0, 0.0, -1.0],
                [0.0, 1.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.0, 0.0, joined_z],
            )
            curved_transform = transform_array(
                [1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, -1.0, 0.0],
                [0.0, joined_y, 0.0],
            )
            if (
                straight_transform is None
                or curved_transform is None
                or not straight_body.ApplyTransform(straight_transform)
                or not curved_body.ApplyTransform(curved_transform)
            ):
                return {"success": False, "message": "SolidWorks failed to align the two flat bars at their mating slots."}
            joined_center_world_mm = [0.0, joined_y, joined_z]

        straight_faces = ActiveModelThroughHoleExecutor._com_member(straight_body, "GetFaces", default=()) or ()
        curved_faces = ActiveModelThroughHoleExecutor._com_member(curved_body, "GetFaces", default=()) or ()
        straight_feature = part.CreateFeatureFromBody3(straight_body, False, create_feature_check)
        curved_feature = part.CreateFeatureFromBody3(curved_body, False, create_feature_check)
        if straight_feature is None or curved_feature is None:
            return {"success": False, "message": "SolidWorks failed to insert both prototype flat-bar bodies."}
        name_suffix = "Joined" if joined_pair else "DWG"
        straight_name = f"FlatBarPrototype_Straight_{name_suffix}"
        curved_name = f"FlatBarPrototype_Curved_{name_suffix}"
        ActiveModelFeatureSkill._name_feature(straight_feature, straight_name)
        ActiveModelFeatureSkill._name_feature(curved_feature, curved_name)
        model.ForceRebuild3(False)
        return {
            "success": True,
            "prototype_mode": prototype_mode,
            "source_drawing": frame.get("source_drawing"),
            "support_count": 1,
            "curved_rail_count": 1,
            "straight_wire_notch_count": len(straight_wire_offsets),
            "curved_wire_notch_count": len(curved_wire_offsets),
            "wire_notch_count": len(straight_wire_offsets) + len(curved_wire_offsets),
            "straight_joint_notch_count": len(straight_joint_offsets),
            "curved_joint_notch_count": len(curved_joint_angles),
            "joint_notch_count": len(straight_joint_offsets) + len(curved_joint_angles),
            "notch_count": (
                len(straight_wire_offsets)
                + len(curved_wire_offsets)
                + len(straight_joint_offsets)
                + len(curved_joint_angles)
            ),
            "straight_total_notch_count": len(straight_wire_offsets) + len(straight_joint_offsets),
            "curved_total_notch_count": len(curved_wire_offsets) + len(curved_joint_angles),
            "notch_width_mm": wire_slot_width,
            "support_profile": f"{width:g}x{thickness:g}",
            "support_face_counts": [len(straight_faces)],
            "curved_rail_face_counts": [len(curved_faces)],
            "feature_names": [straight_name, curved_name],
            "continuous_curved_profile": True,
            "wire_bodies_created": 0,
            "joined_pair": joined_pair,
            "joined_straight_joint_slot_index": 2 if joined_pair else None,
            "joined_curved_joint_slot_index": 3 if joined_pair else None,
            "joined_center_world_mm": joined_center_world_mm,
        }

    def _create_notched_flat_bar_supports(
        self,
        model: Any,
        module: Any,
        app: Any,
        frame: dict[str, Any],
        notches: dict[str, Any],
        wire_mesh: dict[str, Any],
    ) -> dict[str, Any]:
        """Create the drawing-specific 4-by-5 flat-bar lattice and its seats."""

        if frame.get("prototype_mode") in {"one_each", "joined_pair"}:
            return self._create_notched_flat_bar_prototypes(model, module, app, frame, notches)

        import pythoncom
        from win32com.client import VARIANT

        if app is None:
            return {"success": False, "message": "SolidWorks application is unavailable for wire-seat notch creation."}
        modeler_raw = ActiveModelThroughHoleExecutor._com_member(app, "GetModeler")
        if modeler_raw is None:
            return {"success": False, "message": "SolidWorks Modeler is unavailable for wire-seat notch creation."}

        try:
            modeler = module.IModeler(modeler_raw._oleobj_)
            part = module.IPartDoc(model._oleobj_)
        except Exception as exc:
            return {"success": False, "message": f"SolidWorks body interfaces are unavailable: {exc}"}

        math_utility_raw = ActiveModelThroughHoleExecutor._com_member(app, "GetMathUtility")
        if math_utility_raw is None:
            return {"success": False, "message": "SolidWorks MathUtility is unavailable for oriented flat-bar bodies."}
        try:
            math_utility = module.IMathUtility(math_utility_raw._oleobj_)
        except Exception as exc:
            return {"success": False, "message": f"SolidWorks MathUtility interface is unavailable: {exc}"}

        sw_body_add = 15901
        sw_body_cut = 15902
        create_feature_check = 1
        boolean_overshoot = 0.20
        width_epsilon = 0.002
        support_width = float(frame["width_mm"])
        support_thickness = float(frame["thickness_mm"])
        support_length = float(frame["straight_support_length_mm"])
        support_radius = float(frame["support_center_radius_mm"])
        rail_radius = float(frame["curved_rail_radius_mm"])
        rail_half_angle = float(frame["curved_rail_half_angle_rad"])
        rail_segment_count = int(frame["rail_model_segment_count"])
        support_angles = [float(value) for value in frame.get("support_angles_rad", [])]
        rail_y_positions = [float(value) for value in frame.get("curved_rail_y_positions_mm", [])]
        wire_y_positions = [float(value) for value in notches.get("transverse_y_positions_mm", [])]
        wire_angles = [float(value) for value in notches.get("longitudinal_station_angles_rad", [])]
        notch_width = float(notches["width_mm"])
        straight_notch_depth = float(notches["straight_depth_mm"])
        curved_notch_depth = float(notches["curved_depth_mm"])
        joints = frame.get("flat_bar_joint_notches") or {}
        joint_width = float(joints.get("width_mm", support_thickness))
        straight_joint_depth = float(joints.get("straight_depth_mm", 12.0))
        curved_joint_depth = float(joints.get("curved_depth_mm", 13.0))
        mating_rail_indices = [int(value) for value in joints.get("mating_curved_rail_indices", [])]
        center_x, center_z = [float(value) for value in wire_mesh["common_arc_center_xz_mm"]]

        if len(support_angles) != int(frame["straight_support_count"]):
            return {"success": False, "message": "Five longitudinal flat-bar stations were not provided."}
        if len(rail_y_positions) != int(frame["curved_rail_count"]):
            return {"success": False, "message": "Four transverse curved flat-bar positions were not provided."}
        if len(wire_y_positions) != int(notches["notch_count_per_support"]):
            return {"success": False, "message": "The seven transverse-wire positions are incomplete."}
        if len(wire_angles) != int(notches["notch_count_per_curved_rail"]):
            return {"success": False, "message": "The six longitudinal-wire stations are incomplete."}

        def transform_array(
            x_axis: list[float],
            y_axis: list[float],
            z_axis: list[float],
            center_mm: list[float],
        ) -> Any:
            data = [
                *x_axis,
                *y_axis,
                *z_axis,
                self._m(center_mm[0]),
                self._m(center_mm[1]),
                self._m(center_mm[2]),
                1.0,
                0.0,
                0.0,
                0.0,
            ]
            return math_utility.CreateTransform(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, data))

        def oriented_box(
            center_mm: list[float],
            x_axis: list[float],
            y_axis: list[float],
            z_axis: list[float],
            width_mm: float,
            length_mm: float,
            height_mm: float,
        ) -> Any:
            body = modeler.CreateBodyFromBox3(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [
                0.0,
                0.0,
                -self._m(height_mm) / 2.0,
                0.0,
                0.0,
                1.0,
                self._m(width_mm),
                self._m(length_mm),
                self._m(height_mm),
            ]))
            if body is None:
                return None
            transform = transform_array(x_axis, y_axis, z_axis, center_mm)
            if transform is None or not body.ApplyTransform(transform):
                return None
            return body

        def cut_box(
            target: Any,
            *,
            center_mm: list[float],
            x_axis: list[float],
            y_axis: list[float],
            z_axis: list[float],
            width_mm: float,
            length_mm: float,
            height_mm: float,
            label: str,
        ) -> tuple[Any | None, str | None]:
            tool = oriented_box(center_mm, x_axis, y_axis, z_axis, width_mm, length_mm, height_mm)
            if tool is None:
                return None, f"SolidWorks failed to create the {label} cutting body."
            cut_bodies, error_code = target.Operations2(sw_body_cut, tool, 0)
            if int(error_code) != 0 or not cut_bodies:
                return None, f"SolidWorks failed to cut {label} (error={error_code})."
            return cut_bodies[0], None

        support_feature_names: list[str] = []
        support_face_counts: list[int] = []
        support_wire_notch_count = 0
        support_joint_notch_count = 0
        end_joint_y_positions = [rail_y_positions[index] for index in mating_rail_indices]

        for support_index, angle in enumerate(support_angles, start=1):
            radial = [math.sin(angle), 0.0, math.cos(angle)]
            longitudinal = [0.0, 1.0, 0.0]
            thickness_axis = [-math.cos(angle), 0.0, math.sin(angle)]
            center = [
                center_x + support_radius * radial[0],
                0.0,
                center_z + support_radius * radial[2],
            ]
            body = oriented_box(
                center,
                radial,
                longitudinal,
                thickness_axis,
                support_width,
                support_length,
                support_thickness,
            )
            if body is None:
                return {"success": False, "message": f"SolidWorks failed to create straight flat bar {support_index}."}

            wire_tool_depth = straight_notch_depth + boolean_overshoot
            wire_tool_radius = support_radius + support_width / 2.0 - straight_notch_depth / 2.0 + boolean_overshoot / 2.0
            for notch_index, y_value in enumerate(wire_y_positions, start=1):
                tool_center = [
                    center_x + wire_tool_radius * radial[0],
                    y_value,
                    center_z + wire_tool_radius * radial[2],
                ]
                body, error = cut_box(
                    body,
                    center_mm=tool_center,
                    x_axis=radial,
                    y_axis=longitudinal,
                    z_axis=thickness_axis,
                    width_mm=wire_tool_depth,
                    length_mm=notch_width + width_epsilon,
                    height_mm=support_thickness + boolean_overshoot,
                    label=f"straight flat-bar wire seat {support_index}:{notch_index}",
                )
                if body is None:
                    return {"success": False, "message": error or "Straight flat-bar wire-seat cut failed."}
                support_wire_notch_count += 1

            joint_tool_depth = straight_joint_depth + boolean_overshoot
            joint_tool_radius = support_radius - support_width / 2.0 + straight_joint_depth / 2.0 - boolean_overshoot / 2.0
            for joint_index, y_value in enumerate(end_joint_y_positions, start=1):
                tool_center = [
                    center_x + joint_tool_radius * radial[0],
                    y_value,
                    center_z + joint_tool_radius * radial[2],
                ]
                body, error = cut_box(
                    body,
                    center_mm=tool_center,
                    x_axis=radial,
                    y_axis=longitudinal,
                    z_axis=thickness_axis,
                    width_mm=joint_tool_depth,
                    length_mm=joint_width + width_epsilon,
                    height_mm=support_thickness + boolean_overshoot,
                    label=f"straight flat-bar end joint {support_index}:{joint_index}",
                )
                if body is None:
                    return {"success": False, "message": error or "Straight flat-bar end-joint cut failed."}
                support_joint_notch_count += 1

            faces = ActiveModelThroughHoleExecutor._com_member(body, "GetFaces", default=()) or ()
            face_count = len(faces)
            if face_count < int(notches["minimum_support_face_count"]):
                return {
                    "success": False,
                    "message": f"Straight flat bar {support_index} did not preserve its nine rectangular slots.",
                }
            created = part.CreateFeatureFromBody3(body, False, create_feature_check)
            if created is None:
                return {"success": False, "message": f"SolidWorks failed to insert straight flat bar {support_index}."}
            feature_name = f"NotchedStraightFlatBar{support_index:02d}"
            ActiveModelFeatureSkill._name_feature(created, feature_name)
            support_feature_names.append(ActiveModelFeatureSkill._feature_name(created) or feature_name)
            support_face_counts.append(face_count)

        rail_feature_names: list[str] = []
        rail_face_counts: list[int] = []
        curved_wire_notch_count = 0
        curved_joint_notch_count = 0
        rail_angles = [
            -rail_half_angle + (2.0 * rail_half_angle * index / rail_segment_count)
            for index in range(rail_segment_count + 1)
        ]

        for rail_index, y_value in enumerate(rail_y_positions):
            rail_body = None
            for segment_index, (first_angle, second_angle) in enumerate(zip(rail_angles, rail_angles[1:]), start=1):
                midpoint_angle = (first_angle + second_angle) / 2.0
                first = [
                    center_x + rail_radius * math.sin(first_angle),
                    y_value,
                    center_z + rail_radius * math.cos(first_angle),
                ]
                second = [
                    center_x + rail_radius * math.sin(second_angle),
                    y_value,
                    center_z + rail_radius * math.cos(second_angle),
                ]
                chord = [second[index] - first[index] for index in range(3)]
                chord_length = math.dist(first, second)
                tangent = [value / chord_length for value in chord]
                radial = [math.sin(midpoint_angle), 0.0, math.cos(midpoint_angle)]
                thickness_axis = [0.0, 1.0, 0.0]
                segment_center = [(first[index] + second[index]) / 2.0 for index in range(3)]
                segment_body = oriented_box(
                    segment_center,
                    radial,
                    tangent,
                    thickness_axis,
                    support_width,
                    chord_length + 0.30,
                    support_thickness,
                )
                if segment_body is None:
                    return {
                        "success": False,
                        "message": f"SolidWorks failed to create curved flat-bar segment {rail_index + 1}:{segment_index}.",
                    }
                if rail_body is None:
                    rail_body = segment_body
                else:
                    united, error_code = rail_body.Operations2(sw_body_add, segment_body, 0)
                    if int(error_code) != 0 or not united:
                        return {
                            "success": False,
                            "message": f"SolidWorks failed to unite curved flat-bar segment {rail_index + 1}:{segment_index} (error={error_code}).",
                        }
                    rail_body = united[0]

            for notch_index, angle in enumerate(wire_angles, start=1):
                radial = [math.sin(angle), 0.0, math.cos(angle)]
                tangent = [math.cos(angle), 0.0, -math.sin(angle)]
                thickness_axis = [0.0, 1.0, 0.0]
                tool_radius = rail_radius + support_width / 2.0 - curved_notch_depth / 2.0 + boolean_overshoot / 2.0
                tool_center = [
                    center_x + tool_radius * radial[0],
                    y_value,
                    center_z + tool_radius * radial[2],
                ]
                rail_body, error = cut_box(
                    rail_body,
                    center_mm=tool_center,
                    x_axis=radial,
                    y_axis=tangent,
                    z_axis=thickness_axis,
                    width_mm=curved_notch_depth + boolean_overshoot,
                    length_mm=notch_width + width_epsilon,
                    height_mm=support_thickness + boolean_overshoot,
                    label=f"curved flat-bar wire seat {rail_index + 1}:{notch_index}",
                )
                if rail_body is None:
                    return {"success": False, "message": error or "Curved flat-bar wire-seat cut failed."}
                curved_wire_notch_count += 1

            if rail_index in mating_rail_indices:
                for joint_index, angle in enumerate(support_angles, start=1):
                    radial = [math.sin(angle), 0.0, math.cos(angle)]
                    tangent = [math.cos(angle), 0.0, -math.sin(angle)]
                    thickness_axis = [0.0, 1.0, 0.0]
                    tool_radius = rail_radius - support_width / 2.0 + curved_joint_depth / 2.0 - boolean_overshoot / 2.0
                    tool_center = [
                        center_x + tool_radius * radial[0],
                        y_value,
                        center_z + tool_radius * radial[2],
                    ]
                    rail_body, error = cut_box(
                        rail_body,
                        center_mm=tool_center,
                        x_axis=radial,
                        y_axis=tangent,
                        z_axis=thickness_axis,
                        width_mm=curved_joint_depth + boolean_overshoot,
                        length_mm=joint_width + width_epsilon,
                        height_mm=support_thickness + boolean_overshoot,
                        label=f"curved flat-bar end joint {rail_index + 1}:{joint_index}",
                    )
                    if rail_body is None:
                        return {"success": False, "message": error or "Curved flat-bar end-joint cut failed."}
                    curved_joint_notch_count += 1

            faces = ActiveModelThroughHoleExecutor._com_member(rail_body, "GetFaces", default=()) or ()
            face_count = len(faces)
            if face_count < int(notches["minimum_curved_rail_face_count"]):
                return {"success": False, "message": f"Curved flat bar {rail_index + 1} lost one or more required slots."}
            created = part.CreateFeatureFromBody3(rail_body, False, create_feature_check)
            if created is None:
                return {"success": False, "message": f"SolidWorks failed to insert curved flat bar {rail_index + 1}."}
            feature_name = f"NotchedCurvedFlatBar{rail_index + 1:02d}"
            ActiveModelFeatureSkill._name_feature(created, feature_name)
            rail_feature_names.append(ActiveModelFeatureSkill._feature_name(created) or feature_name)
            rail_face_counts.append(face_count)

        model.ForceRebuild3(False)
        wire_notch_count = support_wire_notch_count + curved_wire_notch_count
        joint_notch_count = support_joint_notch_count + curved_joint_notch_count
        return {
            "success": True,
            "support_count": len(support_feature_names),
            "curved_rail_count": len(rail_feature_names),
            "straight_wire_notch_count": support_wire_notch_count,
            "curved_wire_notch_count": curved_wire_notch_count,
            "wire_notch_count": wire_notch_count,
            "straight_joint_notch_count": support_joint_notch_count,
            "curved_joint_notch_count": curved_joint_notch_count,
            "joint_notch_count": joint_notch_count,
            "joint_count": int(joints.get("joint_count", 0)),
            "notch_count": wire_notch_count + joint_notch_count,
            "notch_count_per_support": int(notches["notch_count_per_support"]) + int(joints.get("straight_notch_count_per_support", 0)),
            "notch_width_mm": notch_width,
            "support_profile": f"{support_width:g}x{support_thickness:g}",
            "support_face_counts": support_face_counts,
            "curved_rail_face_counts": rail_face_counts,
            "feature_names": support_feature_names + rail_feature_names,
        }

    def _create_wire_clips(
        self,
        model: Any,
        module: Any,
        clips: dict[str, Any],
        profile_path: Path,
    ) -> dict[str, Any]:
        import pythoncom
        from win32com.client import VARIANT

        feature_manager = module.IFeatureManager(model.FeatureManager._oleobj_)
        sketch_manager = module.ISketchManager(model.SketchManager._oleobj_)
        sketch_manager.Insert3DSketch(True)
        segments: list[Any] = []
        for item in clips["path_segments_mm"]:
            first = item["start_mm"]
            second = item["end_mm"]
            segment = sketch_manager.CreateLine(
                self._m(first[0]), self._m(first[1]), self._m(first[2]),
                self._m(second[0]), self._m(second[1]), self._m(second[2]),
            )
            if segment is None:
                sketch_manager.Insert3DSketch(True)
                return {"success": False, "message": "SolidWorks failed to create a wire-clip path entity."}
            segments.append(segment)
        sketch_manager.Insert3DSketch(True)

        groups: list[Any] = []
        applied_angles: list[float] = []
        requested_angles = list(clips.get("group_angles_rad") or [])
        for group_index, indexes in enumerate(clips["groups"]):
            group = feature_manager.CreateStructuralMemberGroup()
            if group is None:
                return {"success": False, "message": "SolidWorks failed to create a wire-clip structural-member group."}
            group.Segments = VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                [segments[index] for index in indexes],
            )
            group.ApplyCornerTreatment = True
            group.CornerTreatmentType = 1
            group.MergeArcSegmentBodies = True
            angle = float(requested_angles[group_index]) if group_index < len(requested_angles) else 0.0
            try:
                group.Angle = angle
                applied_angles.append(float(group.Angle))
            except Exception as exc:
                return {
                    "success": False,
                    "message": f"SolidWorks rejected wire-clip orientation for group {group_index + 1}: {exc}",
                }
            groups.append(group)

        created = feature_manager.InsertStructuralWeldment5(
            str(profile_path),
            1,
            True,
            VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, groups),
            str(clips["profile_configuration"]),
        )
        if created is None:
            return {
                "success": False,
                "message": "SolidWorks could not create the 8 x 2 mm wire-retaining clips.",
                "profile_path": str(profile_path),
                "profile_configuration": clips["profile_configuration"],
            }
        ActiveModelFeatureSkill._name_feature(created, "WireRetainingClips8x2")
        return {
            "success": True,
            "feature_name": ActiveModelFeatureSkill._feature_name(created),
            "profile_path": str(profile_path),
            "profile_configuration": clips["profile_configuration"],
            "path_segment_count": len(segments),
            "group_count": len(groups),
            "clip_count": int(clips["clip_count"]),
            "applied_group_angles_rad": applied_angles,
        }

    @staticmethod
    def _verify_geometry(
        model: Any,
        request: dict[str, Any],
        body: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        body_count = int(body.get("body_count", 0))
        flat_bar_frame = request.get("flat_bar_frame") or {}
        wire_clips = request.get("wire_clips") or {}
        wire_seat_notches = request.get("wire_seat_notches") or {}
        if request.get("geometry_kind") == "flat_bar_prototype":
            prototype = operation.get("wire_seat_notches") or {}
            expected_mode = str(flat_bar_frame.get("prototype_mode") or "one_each")
            straight_faces = list(prototype.get("support_face_counts") or [])
            curved_faces = list(prototype.get("curved_rail_face_counts") or [])
            feature_names = list(prototype.get("feature_names") or [])
            success = bool(
                body.get("success")
                and body_count == 2
                and prototype.get("success")
                and prototype.get("prototype_mode") == expected_mode
                and prototype.get("support_count") == 1
                and prototype.get("curved_rail_count") == 1
                and prototype.get("straight_total_notch_count") == 11
                and prototype.get("curved_total_notch_count") == 11
                and prototype.get("wire_bodies_created") == 0
                and prototype.get("continuous_curved_profile") is True
                and len(straight_faces) == 1
                and len(curved_faces) == 1
                and straight_faces[0] > 6
                and curved_faces[0] > 6
                and len(feature_names) == 2
                and (expected_mode != "joined_pair" or prototype.get("joined_pair") is True)
            )
            return {
                "success": success,
                "message": (
                    "Joined straight and curved notched flat bars verified."
                    if success and expected_mode == "joined_pair"
                    else "One straight and one continuous curved notched flat bar verified."
                    if success
                    else "Flat-bar prototype verification failed."
                ),
                "geometry_kind": "flat_bar_prototype",
                "prototype_mode": expected_mode,
                "body_count": body_count,
                "expected_body_count": 2,
                "feature_names": feature_names,
                "straight_total_notch_count": prototype.get("straight_total_notch_count"),
                "curved_total_notch_count": prototype.get("curved_total_notch_count"),
                "straight_face_count": straight_faces[0] if straight_faces else 0,
                "curved_face_count": curved_faces[0] if curved_faces else 0,
                "continuous_curved_profile": bool(prototype.get("continuous_curved_profile")),
                "wire_bodies_created": int(prototype.get("wire_bodies_created", -1)),
                "joined_pair": bool(prototype.get("joined_pair")),
                "joined_straight_joint_slot_index": prototype.get("joined_straight_joint_slot_index"),
                "joined_curved_joint_slot_index": prototype.get("joined_curved_joint_slot_index"),
                "joined_center_world_mm": prototype.get("joined_center_world_mm"),
                "source_drawing": prototype.get("source_drawing"),
            }
        expected_flat_bar_members = int(flat_bar_frame.get("expected_body_count", 0) or 0)
        expected_clip_members = int(wire_clips.get("minimum_body_count", 0) or 0)
        expected_notched_supports = int(flat_bar_frame.get("notched_support_body_count", 0) or 0)
        expected_members = (
            len(request["groups"])
            if request.get("geometry_kind") == "wire_mesh"
            else len(request["path_segments_mm"])
        ) + expected_flat_bar_members + expected_clip_members + expected_notched_supports
        expected_path_segments = len(request["path_segments_mm"])
        tree = ActiveModelFeatureSkill._feature_tree(model)
        text = " ".join(f"{item.get('name', '')} {item.get('type', '')}".lower() for item in tree)
        has_weldment = "weldment" in text or "焊件" in text
        has_structural_member = "weldmemberfeat" in text or "structural member" in text or "结构构件" in text
        structural_member_feature_count = sum(
            1
            for item in tree
            if str(item.get("type", "")).lower() == "weldmemberfeat"
        )
        bbox = body.get("bbox", {})
        spans_mm = {
            "x": abs(float(bbox.get("xmax", 0.0)) - float(bbox.get("xmin", 0.0))) * 1000.0,
            "y": abs(float(bbox.get("ymax", 0.0)) - float(bbox.get("ymin", 0.0))) * 1000.0,
            "z": abs(float(bbox.get("zmax", 0.0)) - float(bbox.get("zmin", 0.0))) * 1000.0,
        }
        wire_mesh = request.get("wire_mesh") or {}
        envelope_ok = True
        continuous_arcs_ok = True
        layer_order_ok = True
        flat_bar_paths_ok = True
        wire_clips_ok = True
        wire_seat_notches_ok = True
        if wire_mesh:
            expected_x = wire_mesh["calculated_bbox_width_mm"]
            if flat_bar_frame:
                envelope_ok = (
                    spans_mm["x"] >= expected_x - 2.0
                    and spans_mm["y"] >= wire_mesh["overall_height_mm"] - 2.0
                    and spans_mm["z"] >= wire_mesh["calculated_bbox_depth_mm"] - 2.0
                    and spans_mm["x"] <= expected_x + flat_bar_frame["width_mm"] * 2.0
                    and spans_mm["y"] <= wire_mesh["overall_height_mm"] + flat_bar_frame["width_mm"] * 2.0
                    and spans_mm["z"] <= (
                        wire_mesh["calculated_bbox_depth_mm"]
                        + flat_bar_frame["width_mm"]
                        + float((flat_bar_frame.get("mounting_tabs") or {}).get("height_mm", 0.0))
                        + 10.0
                    )
                )
            else:
                envelope_ok = (
                    abs(spans_mm["x"] - expected_x) <= 2.0
                    and abs(spans_mm["y"] - wire_mesh["overall_height_mm"]) <= 2.0
                    and abs(spans_mm["z"] - wire_mesh["calculated_bbox_depth_mm"]) <= 2.0
                )
            continuous_arcs_ok = bool(
                wire_mesh.get("continuous_transverse_wires")
                and operation.get("arc_entity_count") == wire_mesh.get("transverse_count")
            )
            layer_order_ok = bool(
                wire_mesh.get("layering") == "transverse_arc_wires_on_top"
                and wire_mesh.get("layer_reference") == "drawing_side_profile"
                and wire_mesh.get("side_profile_matches_dwg")
                and wire_mesh.get("transverse_profile_above_longitudinal")
                and wire_mesh.get("transverse_profile_position") == "upper"
                and wire_mesh.get("longitudinal_profile_position") == "lower"
                and wire_mesh.get("side_profile_sag_direction") == "negative_z_endpoints"
                and float(wire_mesh.get("minimum_radial_layer_separation_mm", 0.0)) > 0.0
                and all(
                    abs(float(value) - float(wire_mesh.get("wire_diameter_mm", 0.0))) <= 1e-6
                    for value in wire_mesh.get("layer_contact_distances_mm", [])
                )
            )
        if flat_bar_frame:
            flat_operation = operation.get("flat_bar_frame") or {}
            flat_bar_paths_ok = bool(
                flat_operation.get("success")
                and flat_operation.get("path_segment_count") == flat_bar_frame.get("path_segment_count")
                and flat_operation.get("group_count") == len(flat_bar_frame.get("groups", []))
                and flat_operation.get("arc_entity_count") == flat_bar_frame.get("expected_arc_entity_count")
                and structural_member_feature_count >= 2
            )
        if wire_clips:
            clip_operation = operation.get("wire_clips") or {}
            wire_clips_ok = bool(
                clip_operation.get("success")
                and clip_operation.get("path_segment_count") == wire_clips.get("path_segment_count")
                and clip_operation.get("group_count") == len(wire_clips.get("groups", []))
                and clip_operation.get("clip_count") == wire_clips.get("clip_count")
                and structural_member_feature_count >= 3
            )
        if wire_seat_notches:
            notch_operation = operation.get("wire_seat_notches") or {}
            face_counts = list(notch_operation.get("support_face_counts") or [])
            wire_seat_notches_ok = bool(
                notch_operation.get("success")
                and notch_operation.get("support_count") == wire_seat_notches.get("support_count")
                and notch_operation.get("notch_count") == wire_seat_notches.get("notch_count")
                and len(face_counts) == wire_seat_notches.get("support_count")
                and all(
                    int(value) >= int(wire_seat_notches.get("minimum_support_face_count", 0))
                    for value in face_counts
                )
                and wire_mesh.get("replace_longitudinal_wires") is True
                and wire_mesh.get("longitudinal_wire_created_count") == 0
            )
        success = bool(
            body.get("success")
            and body_count >= expected_members
            and operation.get("path_segment_count") == expected_path_segments
            and has_weldment
            and has_structural_member
            and envelope_ok
            and continuous_arcs_ok
            and layer_order_ok
            and flat_bar_paths_ok
            and wire_clips_ok
            and wire_seat_notches_ok
        )
        return {
            "success": success,
            "message": "Native weldment members verified." if success else "Weldment geometry verification failed.",
            "body_count": body_count,
            "expected_member_count": expected_members,
            "path_segment_count": expected_path_segments,
            "strand_group_count": len(request["groups"]),
            "weldment_tree": has_weldment,
            "structural_member_tree": has_structural_member,
            "structural_member_feature_count": structural_member_feature_count,
            "geometry_kind": request.get("geometry_kind"),
            "bbox_spans_mm": spans_mm,
            "envelope_ok": envelope_ok,
            "drawing_depth_mm": wire_mesh.get("calculated_drawing_depth_mm"),
            "layer_center_distance_mm": wire_mesh.get("layer_center_distance_mm"),
            "layering": wire_mesh.get("layering"),
            "layer_order_ok": layer_order_ok,
            "layer_reference": wire_mesh.get("layer_reference"),
            "side_profile_matches_dwg": bool(wire_mesh.get("side_profile_matches_dwg", False)),
            "side_profile_sag_direction": wire_mesh.get("side_profile_sag_direction"),
            "transverse_profile_position": wire_mesh.get("transverse_profile_position"),
            "longitudinal_profile_position": wire_mesh.get("longitudinal_profile_position"),
            "inner_surface_radius_mm": wire_mesh.get("inner_surface_radius_mm"),
            "transverse_centerline_radius_mm": wire_mesh.get("transverse_centerline_radius_mm"),
            "outer_surface_radius_mm": wire_mesh.get("outer_surface_radius_mm"),
            "longitudinal_center_locus_radius_mm": wire_mesh.get("longitudinal_center_locus_radius_mm"),
            "outer_surface_chord_mm": wire_mesh.get("calculated_outer_surface_chord_mm"),
            "transverse_profile_depth_mm": wire_mesh.get("calculated_transverse_profile_depth_mm"),
            "minimum_radial_layer_separation_mm": wire_mesh.get("minimum_radial_layer_separation_mm"),
            "longitudinal_lengths_mm": wire_mesh.get("longitudinal_lengths_mm", []),
            "continuous_transverse_wires": continuous_arcs_ok,
            "arc_entity_count": operation.get("arc_entity_count", 0),
            "flat_bar_frame_created": flat_bar_paths_ok if flat_bar_frame else False,
            "flat_bar_member_count": expected_flat_bar_members,
            "notched_flat_bar_support_count": expected_notched_supports,
            "flat_bar_profile": (
                f"{flat_bar_frame.get('width_mm')}x{flat_bar_frame.get('thickness_mm')}"
                if flat_bar_frame else ""
            ),
            "wire_clips_created": wire_clips_ok if wire_clips else False,
            "wire_clip_count": int(wire_clips.get("clip_count", 0) or 0),
            "wire_clip_profile": (
                f"{wire_clips.get('width_mm')}x{wire_clips.get('thickness_mm')}"
                if wire_clips else ""
            ),
            "wire_seat_notches_created": wire_seat_notches_ok if wire_seat_notches else False,
            "wire_seat_notch_count": int(wire_seat_notches.get("notch_count", 0) or 0),
            "wire_seat_notch_width_mm": wire_seat_notches.get("width_mm"),
            "wire_seat_notch_depth_mm": wire_seat_notches.get("depth_mm"),
            "replace_longitudinal_wires": bool(wire_mesh.get("replace_longitudinal_wires", False)),
            "longitudinal_wire_created_count": int(wire_mesh.get("longitudinal_wire_created_count", 0) or 0),
            "cut_list_scope": "not_requested",
        }

    @staticmethod
    def _close_unsaved_generated_document(sw: Any, model: Any) -> None:
        """Discard only the new, unsaved Part created by a failed weldment run."""
        try:
            path = ActiveModelThroughHoleExecutor._com_member(model, "GetPathName", default="")
            title = ActiveModelThroughHoleExecutor._com_member(model, "GetTitle", default="")
            if not path and title:
                ActiveModelThroughHoleExecutor._com_member(sw, "CloseDoc", str(title))
        except Exception:
            pass

    @classmethod
    def _profile_path(cls, request: dict[str, Any]) -> Path | None:
        candidates = []
        if request["profile_type"] == "solid_round":
            candidates.append(
                Path(__file__).resolve().parents[2]
                / "assets"
                / "weldment_profiles"
                / cls.PROFILE_FILES[request["profile_type"]]
            )
        candidates.extend([
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            / "SOLIDWORKS"
            / "SOLIDWORKS 2025"
            / "weldment profiles"
            / request["standard"]
            / cls.PROFILE_FILES[request["profile_type"]],
        ])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _flat_bar_profile_path(cls, request: dict[str, Any]) -> Path | None:
        if not request.get("flat_bar_frame"):
            return None
        candidate = (
            Path(__file__).resolve().parents[2]
            / "assets"
            / "weldment_profiles"
            / cls.FLAT_BAR_PROFILE_FILE
        )
        return candidate if candidate.is_file() else None

    @classmethod
    def _wire_clip_profile_path(cls, request: dict[str, Any]) -> Path | None:
        if not request.get("wire_clips"):
            return None
        candidate = (
            Path(__file__).resolve().parents[2]
            / "assets"
            / "weldment_profiles"
            / cls.WIRE_CLIP_PROFILE_FILE
        )
        return candidate if candidate.is_file() else None

    @staticmethod
    def _last_feature_of_type(model: Any, type_name: str) -> Any | None:
        result = None
        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None:
            current_type = str(ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or "")
            if current_type == type_name:
                result = feature
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        return result

    @staticmethod
    def _hide_sketch(model: Any, feature: Any | None) -> None:
        if feature is None:
            return
        try:
            model.ClearSelection2(True)
            if feature.Select2(False, 0):
                ActiveModelThroughHoleExecutor._com_member(model, "BlankSketch")
        except Exception:
            # Sketch visibility is cosmetic and must not invalidate valid weldment geometry.
            pass
        finally:
            model.ClearSelection2(True)

    @classmethod
    def _hide_model_sketches(cls, model: Any) -> None:
        visited = 0

        def visit(feature: Any, depth: int = 0) -> None:
            nonlocal visited
            if feature is None or depth > 8 or visited >= 500:
                return
            visited += 1
            feature_type = str(
                ActiveModelThroughHoleExecutor._com_member(feature, "GetTypeName2", default="") or ""
            )
            if feature_type in {"ProfileFeature", "3DProfileFeature"}:
                cls._hide_sketch(model, feature)
            child = ActiveModelThroughHoleExecutor._com_member(feature, "GetFirstSubFeature")
            while child is not None and visited < 500:
                visit(child, depth + 1)
                child = ActiveModelThroughHoleExecutor._com_member(child, "GetNextSubFeature")

        feature = ActiveModelThroughHoleExecutor._com_member(model, "FirstFeature")
        while feature is not None and visited < 500:
            visit(feature)
            feature = ActiveModelThroughHoleExecutor._com_member(feature, "GetNextFeature")
        model.ClearSelection2(True)

    @staticmethod
    def _point3(value: Any) -> list[float] | None:
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                return [float(value[0]), float(value[1]), float(value[2])]
            if isinstance(value, dict):
                return [float(value[key]) for key in ("x", "y", "z")]
        except (KeyError, TypeError, ValueError):
            return None
        return None

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
