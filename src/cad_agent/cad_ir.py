from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any

from .profile_geometry import normalize_profile_extrude_request


CAD_IR_VERSION = "cad.ir.v1"


@dataclass(frozen=True)
class CADIRCompileResult:
    success: bool
    design: dict[str, Any]
    ir: dict[str, Any]
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


@dataclass(frozen=True)
class QuantityField:
    canonical: str
    compatibility: str
    aliases: tuple[str, ...]
    required: bool = True
    positive: bool = True


class CADIRCompiler:
    """Compile planner output into a stable, millimetre-based CAD IR.

    Design JSON remains backward compatible for existing production skills.
    The nested ``cad_ir`` object is the canonical contract used by new code.
    """

    FEATURE_ALIASES = {
        "hole": "through_hole",
        "simple_hole": "through_hole",
        "center_hole": "through_hole",
        "centre_hole": "through_hole",
        "cut_circle": "through_hole",
        "shaft_thread": "external_thread",
        "external_screw_thread": "external_thread",
        "cosmetic_external_thread": "external_thread",
        "boss_extrude": "boss",
        "extruded_boss": "boss",
        "closed_profile_extrude": "profile_extrude",
        "arbitrary_profile_extrude": "profile_extrude",
        "sketch_profile_extrude": "profile_extrude",
        "cut_extrude": "pocket",
        "extruded_cut": "pocket",
        "rectangular_slot": "slot",
        "edge_round": "fillet",
        "round": "fillet",
        "bevel": "chamfer",
        "face_dome": "dome",
        "dome_feature": "dome",
        "datum_plane": "reference_geometry",
        "native_part_clone": "source_part_clone",
        "reference_part_clone": "source_part_clone",
    }
    OPERATION_ALIASES = FEATURE_ALIASES
    AMBIGUOUS_FEATURE_TYPES = {"extrude", "cut", "profile", "sketch"}
    MODIFIER_OPERATIONS = {
        "boss",
        "pocket",
        "through_hole",
        "threaded_hole",
        "external_thread",
        "bolt_circle_pattern",
        "slot",
        "fillet",
        "chamfer",
        "dome",
        "linear_pattern",
        "circular_pattern",
        "mirror",
        "side_boss",
        "side_hole",
        "rib",
        "shell",
        "draft",
        "profile_extrude",
    }
    NEW_DOCUMENT_OPERATIONS = {
        "source_part_clone",
        "base_plate",
        "gear",
        "gear_pair",
        "revolve",
        "profile_extrude",
        "sweep",
        "loft",
        "sheet_metal",
        "weldment",
        "freeform_surface",
        "assembly_mate",
    }
    STANDARD_OPERATIONS = (
        "source_part_clone",
        "base_plate",
        "boss",
        "pocket",
        "through_hole",
        "threaded_hole",
        "external_thread",
        "bolt_circle_pattern",
        "slot",
        "fillet",
        "chamfer",
        "dome",
        "linear_pattern",
        "circular_pattern",
        "mirror",
        "side_boss",
        "side_hole",
        "rib",
        "gear",
        "gear_pair",
        "revolve",
        "profile_extrude",
        "sweep",
        "loft",
        "sheet_metal",
        "weldment",
        "freeform_surface",
        "shell",
        "draft",
        "reference_geometry",
        "equation",
        "configuration",
        "assembly_mate",
        "drawing",
        "section_view",
        "detail_view",
        "autocad_annotation",
    )
    UNIT_FACTORS_MM = {
        "mm": 1.0,
        "millimeter": 1.0,
        "millimeters": 1.0,
        "millimetre": 1.0,
        "millimetres": 1.0,
        "毫米": 1.0,
        "cm": 10.0,
        "centimeter": 10.0,
        "centimeters": 10.0,
        "厘米": 10.0,
        "m": 1000.0,
        "meter": 1000.0,
        "meters": 1000.0,
        "米": 1000.0,
        "in": 25.4,
        "inch": 25.4,
        "inches": 25.4,
        "\"": 25.4,
        "英寸": 25.4,
    }
    FEATURE_FIELDS: dict[str, tuple[QuantityField, ...]] = {
        "base_plate": (
            QuantityField("length_mm", "length", ("length_mm", "length", "overall_length_mm", "overall_length")),
            QuantityField("width_mm", "width", ("width_mm", "width", "overall_width_mm", "overall_width")),
            QuantityField("thickness_mm", "thickness", ("thickness_mm", "thickness", "height_mm", "height")),
        ),
        "boss": (
            QuantityField("length_mm", "length", ("length_mm", "length")),
            QuantityField("width_mm", "width", ("width_mm", "width")),
            QuantityField("height_mm", "height", ("height_mm", "height", "depth_mm", "depth")),
        ),
        "pocket": (
            QuantityField("length_mm", "length", ("length_mm", "length")),
            QuantityField("width_mm", "width", ("width_mm", "width")),
            QuantityField("depth_mm", "depth", ("depth_mm", "depth")),
        ),
        "slot": (
            QuantityField("length_mm", "length", ("length_mm", "length", "slot_length_mm", "slot_length")),
            QuantityField("width_mm", "width", ("width_mm", "width", "slot_width_mm", "slot_width")),
            QuantityField("depth_mm", "depth", ("depth_mm", "depth"), required=False),
        ),
        "through_hole": (
            QuantityField(
                "diameter_mm",
                "diameter",
                ("diameter_mm", "diameter", "hole_diameter_mm", "hole_diameter"),
            ),
        ),
        "fillet": (
            QuantityField("radius_mm", "radius", ("radius_mm", "radius", "fillet_radius_mm", "fillet_radius")),
        ),
        "chamfer": (
            QuantityField(
                "size_mm",
                "size",
                ("size_mm", "size", "distance_mm", "distance", "chamfer_size_mm", "chamfer_size"),
            ),
            QuantityField(
                "diameter_mm",
                "diameter",
                ("diameter_mm", "diameter", "major_diameter_mm", "major_diameter"),
                required=False,
            ),
        ),
        "dome": (
            QuantityField(
                "height_mm",
                "height",
                ("height_mm", "height", "dome_height_mm", "dome_height"),
            ),
        ),
        "bolt_circle_pattern": (
            QuantityField("pcd_mm", "pcd", ("pcd_mm", "pcd", "pitch_circle_diameter_mm", "pitch_circle_diameter")),
            QuantityField("diameter_mm", "diameter", ("diameter_mm", "diameter", "hole_diameter_mm", "hole_diameter"), required=False),
        ),
        "side_boss": (
            QuantityField("diameter_mm", "diameter_mm", ("diameter_mm", "diameter")),
            QuantityField("depth_mm", "depth_mm", ("depth_mm", "depth")),
            QuantityField(
                "face_offset_mm",
                "face_offset_mm",
                ("face_offset_mm", "face_offset"),
                positive=False,
            ),
        ),
        "side_hole": (
            QuantityField("diameter_mm", "diameter_mm", ("diameter_mm", "diameter")),
            QuantityField(
                "face_offset_mm",
                "face_offset_mm",
                ("face_offset_mm", "face_offset"),
                positive=False,
            ),
            QuantityField("pcd_mm", "pcd_mm", ("pcd_mm", "pcd"), required=False),
        ),
        "rib": (
            QuantityField("width_mm", "width_mm", ("width_mm", "width"), required=False),
            QuantityField("height_mm", "height_mm", ("height_mm", "height"), required=False),
            QuantityField("depth_mm", "depth_mm", ("depth_mm", "depth"), required=False),
            QuantityField("thickness_mm", "thickness_mm", ("thickness_mm", "thickness"), required=False),
            QuantityField(
                "face_offset_mm",
                "face_offset_mm",
                ("face_offset_mm", "face_offset"),
                positive=False,
                required=False,
            ),
        ),
        "shell": (
            QuantityField("thickness_mm", "thickness_mm", ("thickness_mm", "thickness")),
        ),
        "sheet_metal": (
            QuantityField("thickness_mm", "thickness_mm", ("thickness_mm", "thickness")),
            QuantityField("length_mm", "length_mm", ("length_mm", "length"), required=False),
            QuantityField("width_mm", "width_mm", ("width_mm", "width"), required=False),
            QuantityField("bend_radius_mm", "bend_radius_mm", ("bend_radius_mm", "bend_radius"), required=False),
        ),
        "linear_pattern": (
            QuantityField("spacing_mm", "spacing", ("spacing_mm", "spacing", "spacing_1", "spacing_x")),
        ),
        "gear": (
            QuantityField("module_mm", "module_mm", ("module_mm", "module")),
            QuantityField("face_width_mm", "face_width_mm", ("face_width_mm", "face_width")),
        ),
        "sweep": (
            QuantityField("diameter_mm", "diameter_mm", ("diameter_mm", "diameter"), required=False),
        ),
        "threaded_hole": (
            QuantityField("thread_depth_mm", "thread_depth", ("thread_depth_mm", "thread_depth", "depth_mm", "depth"), required=False),
            QuantityField(
                "pilot_depth_mm",
                "pilot_depth",
                ("pilot_depth_mm", "pilot_depth", "tap_drill_depth_mm", "hole_depth_mm"),
                required=False,
            ),
            QuantityField(
                "tap_drill_diameter_mm",
                "tap_drill_diameter",
                ("tap_drill_diameter_mm", "tap_drill_mm", "tap_drill"),
                required=False,
            ),
            QuantityField(
                "face_offset_mm",
                "face_offset_mm",
                ("face_offset_mm", "face_offset", "support_face_offset_mm", "target_face_offset_mm"),
                positive=False,
            ),
        ),
        "external_thread": (
            QuantityField(
                "major_diameter_mm",
                "major_diameter",
                ("major_diameter_mm", "major_diameter", "diameter_mm", "diameter"),
            ),
            QuantityField("pitch_mm", "pitch", ("pitch_mm", "pitch")),
            QuantityField(
                "thread_length_mm",
                "thread_length",
                ("thread_length_mm", "thread_length", "length_mm", "length"),
            ),
        ),
    }
    RAW_ALIAS_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
        "source_part_clone": {
            "source_path": ("source_path", "reference_path", "input_sldprt"),
            "source_sha256": ("source_sha256", "sha256", "reference_sha256"),
            "expected_solid_body_count": ("expected_solid_body_count", "solid_body_count"),
            "expected_feature_count": ("expected_feature_count", "feature_count"),
            "expected_volume_m3": ("expected_volume_m3", "volume_m3"),
            "expected_bounding_box_mm": ("expected_bounding_box_mm", "bounding_box_mm"),
        },
        "threaded_hole": {
            "thread": ("thread", "thread_size", "designation"),
            "axis": ("axis", "hole_axis"),
            "center_yz_mm": ("center_yz_mm",),
            "center_xz_mm": ("center_xz_mm",),
            "center_xy_mm": ("center_xy_mm",),
            "center_uv_mm": ("center_uv_mm", "center_mm"),
        },
        "external_thread": {
            "designation": ("designation", "thread", "thread_size", "size"),
            "representation": ("representation", "thread_representation"),
            "thread_method": ("thread_method", "geometry_method"),
            "end": ("end", "shaft_end", "thread_end"),
            "axis": ("axis", "shaft_axis"),
            "standard": ("standard", "thread_standard"),
            "standard_type": ("standard_type", "profile_type", "thread_profile_type"),
            "edge_selector": ("edge_selector", "target_edge_selector"),
            "thread_callout": ("thread_callout", "callout", "note"),
            "axis_center_mm": ("axis_center_mm", "axis_center"),
            "right_handed": ("right_handed", "is_right_handed"),
            "trim_start_face": ("trim_start_face", "trim_start"),
            "trim_end_face": ("trim_end_face", "trim_end"),
            "thread_start_angle_deg": ("thread_start_angle_deg", "start_angle_deg"),
            "reverse_direction": ("reverse_direction", "reverse"),
        },
        "bolt_circle_pattern": {
            "count": ("count", "instances", "quantity"),
            "thread": ("thread", "thread_size", "designation"),
        },
        "linear_pattern": {
            "seed_features": ("seed_features", "seed_feature", "target_features", "target_feature"),
            "count": ("count", "count_1", "count_x", "instances"),
            "direction": ("direction", "direction_1"),
        },
        "circular_pattern": {
            "seed_features": ("seed_features", "seed_feature", "target_features", "target_feature"),
            "count": ("count", "instances"),
            "axis": ("axis", "direction_axis"),
            "axis_feature": ("axis_feature", "reference_axis", "pattern_axis_feature"),
            "total_angle_deg": ("total_angle_deg", "angle_deg", "angle"),
            "spacing_angle_deg": ("spacing_angle_deg", "spacing_deg", "angular_pitch_deg"),
        },
        "mirror": {
            "seed_features": ("seed_features", "seed_feature", "target_features", "target_feature"),
            "mirror_plane": ("mirror_plane", "plane"),
        },
        "configuration": {
            "name": ("name", "configuration_name"),
        },
        "profile_extrude": {
            "body_operation": ("body_operation", "extrude_operation", "result_operation", "operation"),
            "profiles": ("profiles", "loops", "profile"),
            "sketch_plane": ("sketch_plane", "plane"),
            "end_condition": ("end_condition", "extent"),
            "start_offset_mm": ("start_offset_mm", "start_offset"),
            "flip_side_to_cut": ("flip_side_to_cut", "flip_cut_side"),
        },
        "chamfer": {
            "targets": ("targets", "target", "edge_selector"),
            "axis": ("axis", "shaft_axis"),
        },
        "dome": {
            "face_selector": ("face_selector", "target_face_signature"),
            "reverse_direction": ("reverse_direction", "reverse"),
            "elliptical": ("elliptical", "elliptic"),
        },
        "revolve": {
            "body_operation": ("body_operation", "operation", "revolve_type"),
            "expected_body_result": ("expected_body_result", "body_result"),
            "execution_mode": ("execution_mode", "document_mode", "model_mode"),
            "sketch_plane": ("sketch_plane", "plane"),
            "axis": ("axis", "revolve_axis"),
            "profile_points": ("profile_points", "profile"),
            "profile_segments": ("profile_segments", "profile_loop_segments"),
            "axis_offset_mm": ("axis_offset_mm", "axis_offset"),
            "support_face_offset_mm": (
                "support_face_offset_mm",
                "sketch_face_offset_mm",
                "face_offset_mm",
            ),
            "support_face_axis": ("support_face_axis",),
            "support_face_point_xz_mm": ("support_face_point_xz_mm",),
            "support_face_point_yz_mm": ("support_face_point_yz_mm",),
            "support_face_point_xy_mm": ("support_face_point_xy_mm",),
            "support_face_point_uv_mm": (
                "support_face_point_uv_mm",
                "support_face_point_mm",
            ),
        },
        "rib": {
            "profile": ("profile", "rib_profile"),
            "sketch_plane_feature": (
                "sketch_plane_feature",
                "reference_plane_feature",
                "plane_feature",
            ),
            "line_points_mm": ("line_points_mm", "points_mm"),
        },
    }
    REQUIRED_PARAMETER_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
        "source_part_clone": (("source_path",), ("source_sha256",)),
        "threaded_hole": (
            ("thread",),
            ("axis",),
            ("face_offset_mm",),
            ("center_yz_mm", "center_xz_mm", "center_xy_mm", "center_uv_mm"),
        ),
        "external_thread": (
            ("designation",),
            ("representation",),
            ("end",),
            ("axis",),
            ("standard",),
            ("standard_type",),
            ("edge_selector",),
        ),
        "bolt_circle_pattern": (("count",), ("pcd",), ("thread", "diameter")),
        "linear_pattern": (("seed_features",), ("count",), ("spacing_mm",), ("direction",)),
        "circular_pattern": (("seed_features",), ("count",), ("axis",)),
        "mirror": (("seed_features",), ("mirror_plane",)),
        "dome": (("face_selector",),),
        "side_boss": (("axis",), ("center_xz_mm", "center_yz_mm", "center_xy_mm", "center_uv_mm", "center_mm")),
        "side_hole": (("axis",), ("center_xz_mm", "center_yz_mm", "center_xy_mm", "center_uv_mm", "center_mm")),
        "rib": (
            ("axis", "sketch_plane_feature"),
            ("center_positions_mm", "positions_mm", "line_points_mm"),
        ),
        "gear": (("module_mm", "module"), ("tooth_count", "teeth"), ("face_width_mm", "face_width")),
        "gear_pair": (("gear_a", "driver"), ("gear_b", "driven")),
        "revolve": (("profile", "profile_points", "profile_segments", "profile_loop_segments", "segments", "shaft_segments", "design_dimensions"),),
        "sweep": (("profile", "diameter_mm", "diameter"), ("path_points_mm", "path_points", "path_segments", "path")),
        "loft": (("sections", "profiles"),),
        "sheet_metal": (("lofted_bend", "sheet_metal_lofted_bend", "length_mm", "length"),),
        "weldment": (("wire_mesh", "path_segments_mm", "segments", "path_points_mm", "path_points"),),
        "freeform_surface": (("boundary_curves_mm", "boundary_curves"),),
        "shell": (("remove_faces", "opening_faces", "remove_face"),),
        "draft": (("angle_deg", "angle"), ("neutral_plane", "neutral_face"), ("target_faces", "faces")),
        "reference_geometry": (("reference_type", "type"),),
        "equation": (("expression", "rhs", "value"), ("name", "target")),
        "configuration": (("name",),),
        "assembly_mate": (("components",), ("mates", "mate_type")),
        "section_view": (("parent_view", "source_view"),),
        "detail_view": (("parent_view", "source_view"),),
    }
    OPERATION_CONTEXT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
        "source_part_clone": ("existing_source_sldprt", "independent_task_output"),
        "base_plate": ("new_part_document",),
        "boss": ("target_body", "target_face_or_unambiguous_default"),
        "pocket": ("target_body", "target_face_or_unambiguous_default"),
        "through_hole": ("target_body", "target_face_or_unambiguous_default"),
        "threaded_hole": ("target_body", "target_face_or_unambiguous_default"),
        "external_thread": ("target_body", "axial_cylindrical_end_edge"),
        "bolt_circle_pattern": ("target_body", "target_face_or_unambiguous_default"),
        "slot": ("target_body", "target_face_or_unambiguous_default"),
        "fillet": ("target_body", "edge_selector"),
        "chamfer": ("target_body", "edge_selector"),
        "dome": ("target_body", "explicit_planar_face_signature"),
        "linear_pattern": ("target_body", "seed_feature_refs"),
        "circular_pattern": ("target_body", "seed_feature_refs"),
        "mirror": ("target_body", "seed_feature_refs", "mirror_plane"),
        "side_boss": ("target_body", "local_frame"),
        "side_hole": ("target_body", "local_frame"),
        "rib": ("target_body", "local_frame"),
        "gear": ("new_part_document",),
        "gear_pair": ("new_part_or_assembly_document",),
        "revolve": ("new_or_active_part_document", "revolve_axis"),
        "profile_extrude": ("explicit_sketch_plane", "closed_profile_loops"),
        "sweep": ("new_part_document", "profile_and_path"),
        "loft": ("new_or_active_part_document", "ordered_sections"),
        "sheet_metal": ("new_part_document",),
        "weldment": ("new_part_document",),
        "freeform_surface": ("new_part_document",),
        "shell": ("target_body", "opening_face_refs"),
        "draft": ("target_body", "neutral_plane", "target_face_refs"),
        "reference_geometry": ("active_part_document",),
        "equation": ("active_part_document",),
        "configuration": ("active_model_document",),
        "assembly_mate": ("assembly_document", "component_refs"),
        "drawing": ("source_model_or_active_part",),
        "section_view": ("drawing_document", "parent_view_ref"),
        "detail_view": ("drawing_document", "parent_view_ref"),
        "autocad_annotation": ("source_dwg_or_active_drawing",),
    }
    COMMON_CONFLICT_RULES = (
        "canonical_alias_values_must_agree_after_unit_conversion",
        "quantities_must_be_finite_and_positive",
        "dependencies_must_exist_precede_the_consumer_and_be_acyclic",
        "target_feature_references_must_resolve",
    )

    @classmethod
    def operation_contracts(cls) -> dict[str, dict[str, Any]]:
        """Return the closed CAD-IR operation contracts used by tests and reports."""
        contracts: dict[str, dict[str, Any]] = {}
        for operation in cls.STANDARD_OPERATIONS:
            quantity_fields = cls.FEATURE_FIELDS.get(operation, ())
            conflict_rules = list(cls.COMMON_CONFLICT_RULES)
            if operation in {"through_hole", "bolt_circle_pattern", "linear_pattern", "circular_pattern"}:
                conflict_rules.append("count_must_be_a_positive_integer")
            if operation == "external_thread":
                conflict_rules.extend([
                    "designation_must_match_major_diameter_and_pitch",
                    "representation_must_be_cosmetic_or_modeled",
                    "modeled_representation_requires_cut_thread_method",
                    "end_must_be_min_or_max",
                    "axis_must_be_x_y_or_z",
                    "edge_selector_must_be_axial_major_diameter_end_edge",
                ])
            if operation == "profile_extrude":
                conflict_rules.extend([
                    "profile_segment_chains_must_be_contiguous_and_closed",
                    "profile_loops_must_not_self_intersect_or_overlap",
                    "multiple_outer_loops_must_be_disjoint_and_non_nested",
                    "base_requires_new_model_and_boss_or_cut_require_active_model",
                    "nonzero_start_offset_requires_blind_or_through_next_boss_or_blind_cut",
                    "flip_side_to_cut_requires_cut_operation",
                ])
            if operation == "dome":
                conflict_rules.extend([
                    "mode_must_be_active_model",
                    "face_selector_must_be_planar_face_signature",
                    "face_signature_must_resolve_exactly_one_face",
                ])
            contracts[operation] = {
                "aliases": sorted(
                    alias for alias, canonical in cls.OPERATION_ALIASES.items()
                    if canonical == operation
                ),
                "required_parameters": [
                    field.canonical for field in quantity_fields if field.required
                ],
                "optional_parameters": [
                    field.canonical for field in quantity_fields if not field.required
                ],
                "required_any": [
                    list(group) for group in cls.REQUIRED_PARAMETER_GROUPS.get(operation, ())
                ],
                "raw_alias_groups": {
                    canonical: list(aliases)
                    for canonical, aliases in cls.RAW_ALIAS_GROUPS.get(operation, {}).items()
                },
                "context_requirements": list(
                    cls.OPERATION_CONTEXT_REQUIREMENTS.get(operation, ())
                ),
                "conflict_rules": conflict_rules,
            }
            if operation == "profile_extrude":
                contracts[operation]["required_parameters"] = [
                    "body_operation",
                    "sketch_plane",
                    "profiles",
                ]
                contracts[operation]["optional_parameters"] = [
                    "mode",
                    "depth_mm",
                    "end_condition",
                    "reverse_direction",
                    "merge_result",
                    "start_offset_mm",
                    "flip_start_offset",
                    "flip_side_to_cut",
                ]
                contracts[operation]["required_any"] = [["depth_mm", "end_condition"]]
            elif operation == "revolve":
                contracts[operation]["required_parameters"] = [
                    "body_operation",
                    "execution_mode",
                    "sketch_plane",
                    "axis",
                ]
                contracts[operation]["optional_parameters"] = [
                    "angle_deg",
                    "reverse_direction",
                    "axis_offset_mm",
                ]
                contracts[operation]["required_any"] = [[
                    "profile_points",
                    "profile_segments",
                    "segments",
                    "shaft_segments",
                    "design_dimensions",
                ]]
                contracts[operation]["conflict_rules"] = [
                    "base_requires_new_model",
                    "boss_or_cut_requires_active_model",
                    "axis_must_be_local_horizontal_or_local_vertical",
                    "profile_must_be_closed_and_not_cross_axis",
                    "profile_segments_must_be_contiguous_and_closed",
                ]
            elif operation == "external_thread":
                contracts[operation]["required_parameters"] = [
                    "designation",
                    "representation",
                    "major_diameter_mm",
                    "pitch_mm",
                    "thread_length_mm",
                    "axis",
                    "end",
                    "standard",
                    "standard_type",
                    "edge_selector",
                ]
                contracts[operation]["optional_parameters"] = [
                    "thread_method",
                    "axis_center_mm",
                    "thread_callout",
                    "right_handed",
                    "trim_start_face",
                    "trim_end_face",
                    "thread_start_angle_deg",
                    "reverse_direction",
                ]
            elif operation == "dome":
                contracts[operation]["required_parameters"] = [
                    "height_mm",
                    "face_selector",
                ]
                contracts[operation]["optional_parameters"] = [
                    "mode",
                    "reverse_direction",
                    "elliptical",
                ]
        return contracts

    def compile(self, design: dict[str, Any]) -> CADIRCompileResult:
        normalized = copy.deepcopy(dict(design or {}))
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        parameters = dict(normalized.get("parameters") or {})
        default_unit = self._normalize_unit(parameters.get("unit") or "mm")
        if default_unit is None:
            errors.append(self._issue(
                "unsupported_unit",
                f"Unsupported design unit: {parameters.get('unit')!r}",
                field="parameters.unit",
            ))
            default_unit = "mm"
        parameters = self._normalize_global_parameters(parameters, default_unit, errors)
        normalized["parameters"] = parameters

        raw_features = normalized.get("features") or []
        if not isinstance(raw_features, list):
            errors.append(self._issue("features_not_list", "Design features must be a list.", field="features"))
            raw_features = []
        raw_features = self._with_explicit_base_operation(
            list(raw_features),
            parameters,
            str(normalized.get("task_type") or ""),
        )

        has_base_plate = any(
            isinstance(item, dict)
            and self._canonical_feature_type(str(item.get("type") or ""))[0] == "base_plate"
            for item in raw_features
        )
        normalized_features: list[dict[str, Any]] = []
        ir_features: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for index, raw in enumerate(raw_features, start=1):
            feature, ir_feature = self._compile_feature(
                raw,
                index,
                parameters,
                default_unit,
                has_base_plate,
                str(normalized.get("task_type") or ""),
                errors,
                warnings,
                used_ids,
            )
            if feature is not None:
                normalized_features.append(feature)
            if ir_feature is not None:
                ir_features.append(ir_feature)
        normalized["features"] = normalized_features
        self._resolve_dependencies(ir_features, errors)
        reconstruction_validation = self._validate_reconstruction_contract(
            normalized,
            ir_features,
            errors,
            warnings,
        )

        ir = {
            "version": CAD_IR_VERSION,
            "valid": not errors,
            "unit_system": "mm",
            "source_schema_version": str(normalized.get("schema_version") or ""),
            "task_type": str(normalized.get("task_type") or ""),
            "features": ir_features,
            "errors": errors,
            "warnings": warnings,
        }
        if reconstruction_validation is not None:
            ir["reconstruction_contract"] = copy.deepcopy(
                normalized.get("reconstruction_contract") or {}
            )
            ir["reconstruction_validation"] = copy.deepcopy(reconstruction_validation)
            normalized["reconstruction_validation"] = copy.deepcopy(reconstruction_validation)
        normalized["cad_ir"] = ir
        normalized["cad_ir_validation"] = {
            "success": not errors,
            "error_count": len(errors),
            "warning_count": len(warnings),
        }
        existing_unsupported = [
            item
            for item in list(normalized.get("unsupported_features") or [])
            if not (isinstance(item, dict) and item.get("type") == "cad_ir_validation_error")
        ]
        if errors:
            normalized["needs_confirmation"] = True
            existing_unsupported.extend(
                {
                    "type": "cad_ir_validation_error",
                    "required": True,
                    "feature_id": item.get("feature_id"),
                    "field": item.get("field"),
                    "reason": item["message"],
                }
                for item in errors
            )
            normalized.setdefault("confirmation_reason", errors[0]["message"])
        normalized["unsupported_features"] = existing_unsupported
        return CADIRCompileResult(not errors, normalized, ir, errors, warnings)

    def _validate_reconstruction_contract(
        self,
        design: dict[str, Any],
        ir_features: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        raw_contract = design.get("reconstruction_contract")
        if raw_contract in (None, {}):
            return None
        if not isinstance(raw_contract, dict):
            errors.append(self._issue(
                "invalid_reconstruction_contract",
                "reconstruction_contract must be an object.",
                field="reconstruction_contract",
            ))
            return {
                "status": "blocked",
                "delivery_level": "unknown",
                "gaps": ["reconstruction_contract is not an object"],
            }

        contract = copy.deepcopy(raw_contract)
        delivery_level = str(
            contract.get("delivery_level")
            or ("capability_stage" if contract.get("approximation_allowed") else "source_parity")
        ).strip().lower()
        if delivery_level not in {"capability_stage", "source_parity"}:
            errors.append(self._issue(
                "invalid_reconstruction_delivery_level",
                "reconstruction_contract.delivery_level must be capability_stage or source_parity.",
                field="reconstruction_contract.delivery_level",
            ))

        counts: dict[str, int] = {}
        feature_ids: set[str] = set()
        for feature in ir_features:
            operation = str(feature.get("operation") or "")
            counts[operation] = counts.get(operation, 0) + 1
            feature_ids.add(str(feature.get("id") or ""))

        gaps: list[dict[str, Any]] = []
        required_counts = contract.get("required_operation_counts") or {}
        if not isinstance(required_counts, dict):
            errors.append(self._issue(
                "invalid_reconstruction_operation_counts",
                "reconstruction_contract.required_operation_counts must be an object.",
                field="reconstruction_contract.required_operation_counts",
            ))
            required_counts = {}
        normalized_required_counts: dict[str, int] = {}
        for raw_operation, raw_count in required_counts.items():
            operation, ambiguous = self._canonical_feature_type(str(raw_operation))
            try:
                minimum = int(raw_count)
            except (TypeError, ValueError):
                minimum = -1
            if (
                ambiguous
                or operation not in self.STANDARD_OPERATIONS
                or minimum < 0
            ):
                errors.append(self._issue(
                    "invalid_reconstruction_operation_requirement",
                    f"Invalid reconstruction requirement {raw_operation!r}: {raw_count!r}.",
                    field=f"reconstruction_contract.required_operation_counts.{raw_operation}",
                ))
                continue
            normalized_required_counts[operation] = minimum
            actual = counts.get(operation, 0)
            if actual < minimum:
                gaps.append({
                    "kind": "operation_count",
                    "operation": operation,
                    "required": minimum,
                    "actual": actual,
                })

        raw_required_ids = contract.get("required_feature_ids") or []
        if not isinstance(raw_required_ids, list):
            errors.append(self._issue(
                "invalid_reconstruction_feature_ids",
                "reconstruction_contract.required_feature_ids must be a list.",
                field="reconstruction_contract.required_feature_ids",
            ))
            raw_required_ids = []
        required_ids = [str(value).strip() for value in raw_required_ids if str(value).strip()]
        for feature_id in required_ids:
            if feature_id not in feature_ids:
                gaps.append({
                    "kind": "feature_id",
                    "feature_id": feature_id,
                    "required": True,
                    "actual": False,
                })

        minimum_feature_count = contract.get("minimum_feature_count", 0)
        try:
            minimum_feature_count = int(minimum_feature_count or 0)
        except (TypeError, ValueError):
            minimum_feature_count = -1
        if minimum_feature_count < 0:
            errors.append(self._issue(
                "invalid_reconstruction_feature_count",
                "reconstruction_contract.minimum_feature_count must be a non-negative integer.",
                field="reconstruction_contract.minimum_feature_count",
            ))
            minimum_feature_count = 0
        if len(ir_features) < minimum_feature_count:
            gaps.append({
                "kind": "feature_count",
                "required": minimum_feature_count,
                "actual": len(ir_features),
            })

        forbidden_operations = contract.get("forbidden_operations") or []
        if not isinstance(forbidden_operations, list):
            errors.append(self._issue(
                "invalid_reconstruction_forbidden_operations",
                "reconstruction_contract.forbidden_operations must be a list.",
                field="reconstruction_contract.forbidden_operations",
            ))
            forbidden_operations = []
        normalized_forbidden: list[str] = []
        for raw_operation in forbidden_operations:
            operation, ambiguous = self._canonical_feature_type(str(raw_operation))
            operation = operation or str(raw_operation).strip()
            if ambiguous or operation not in self.STANDARD_OPERATIONS:
                errors.append(self._issue(
                    "invalid_reconstruction_forbidden_operation",
                    f"Unknown forbidden reconstruction operation: {raw_operation!r}.",
                    field="reconstruction_contract.forbidden_operations",
                ))
                continue
            normalized_forbidden.append(operation)
            if counts.get(operation, 0):
                gaps.append({
                    "kind": "forbidden_operation",
                    "operation": operation,
                    "required": 0,
                    "actual": counts[operation],
                })

        gap_severity = "error" if bool(contract.get("block_on_gap", True)) else "warning"
        for gap in gaps:
            operation = gap.get("operation")
            feature_id = gap.get("feature_id")
            label = operation or feature_id or gap.get("kind")
            message = (
                f"Reconstruction contract is incomplete for {label!r}: "
                f"required={gap.get('required')!r}, actual={gap.get('actual')!r}."
            )
            issue = self._issue(
                "reconstruction_contract_gap",
                message,
                feature_id=str(feature_id or "") or None,
                field="reconstruction_contract",
            )
            (errors if gap_severity == "error" else warnings).append(issue)

        if delivery_level == "source_parity":
            if bool(contract.get("approximation_allowed", False)):
                errors.append(self._issue(
                    "source_parity_cannot_allow_approximation",
                    "source_parity reconstruction cannot set approximation_allowed=true.",
                    field="reconstruction_contract.approximation_allowed",
                ))
            if design.get("assumptions") or design.get("unresolved"):
                errors.append(self._issue(
                    "source_parity_has_unresolved_information",
                    "source_parity reconstruction cannot contain assumptions or unresolved information.",
                    field="reconstruction_contract.delivery_level",
                ))

        validation = {
            "status": "blocked" if gaps and gap_severity == "error" else "passed",
            "delivery_level": delivery_level,
            "feature_count": len(ir_features),
            "operation_counts": counts,
            "required_operation_counts": normalized_required_counts,
            "required_feature_ids": required_ids,
            "minimum_feature_count": minimum_feature_count,
            "forbidden_operations": normalized_forbidden,
            "gaps": gaps,
            "block_on_gap": gap_severity == "error",
        }
        design["reconstruction_contract"] = contract
        return validation

    def _with_explicit_base_operation(
        self,
        features: list[Any],
        parameters: dict[str, Any],
        task_type: str,
    ) -> list[Any]:
        if task_type not in {"model_3d", "full_pipeline"}:
            return features
        feature_types = {
            self._canonical_feature_type(str(item.get("type") or ""))[0]
            for item in features
            if isinstance(item, dict)
        }
        if feature_types & self.NEW_DOCUMENT_OPERATIONS:
            return features
        if not (feature_types & self.MODIFIER_OPERATIONS):
            return features
        dimensions = {
            "length": parameters.get("length"),
            "width": parameters.get("width"),
            "thickness": parameters.get("thickness"),
        }
        if not all(isinstance(value, (int, float)) and float(value) > 0 for value in dimensions.values()):
            return features
        return [
            {
                "name": "BasePlate",
                "type": "base_plate",
                "required": True,
                "params": {key: float(value) for key, value in dimensions.items()},
                "cad_ir_inferred": True,
            },
            *features,
        ]

    def _compile_feature(
        self,
        raw: Any,
        index: int,
        global_parameters: dict[str, Any],
        default_unit: str,
        has_base_plate: bool,
        task_type: str,
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        used_ids: set[str],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not isinstance(raw, dict):
            errors.append(self._issue(
                "feature_not_object",
                f"Feature {index} must be an object.",
                field=f"features[{index - 1}]",
            ))
            return None, None
        feature = copy.deepcopy(raw)
        original_type = self._type_token(feature.get("type"))
        canonical_type, ambiguous = self._canonical_feature_type(original_type)
        name = str(feature.get("name") or canonical_type or f"Feature{index}").strip()
        feature_id = self._unique_id(name or f"feature_{index}", used_ids)
        if not canonical_type:
            errors.append(self._issue(
                "missing_feature_type",
                f"Feature {name!r} has no type.",
                feature_id=feature_id,
                field="type",
            ))
            canonical_type = original_type
        if ambiguous:
            errors.append(self._issue(
                "ambiguous_feature_type",
                f"Feature type {original_type!r} is ambiguous; specify a production operation such as boss or pocket.",
                feature_id=feature_id,
                field="type",
            ))
        elif canonical_type and canonical_type not in self.STANDARD_OPERATIONS:
            errors.append(self._issue(
                "unsupported_operation",
                f"Feature type {original_type!r} is not a registered CAD-IR operation.",
                feature_id=feature_id,
                field="type",
            ))
        # Keep the canonical identifier beside the display name so Pipeline
        # feature batches can never fall back to matching localized labels.
        feature["id"] = feature_id
        feature["name"] = name
        feature["type"] = canonical_type
        feature.setdefault("required", True)
        params = copy.deepcopy(feature.get("params") or {})
        if not isinstance(params, dict):
            errors.append(self._issue(
                "params_not_object",
                f"Feature {name!r} params must be an object.",
                feature_id=feature_id,
                field="params",
            ))
            params = {}
        feature_unit = self._normalize_unit(params.get("unit") or default_unit)
        if feature_unit is None:
            errors.append(self._issue(
                "unsupported_unit",
                f"Feature {name!r} uses unsupported unit {params.get('unit')!r}.",
                feature_id=feature_id,
                field="params.unit",
            ))
            feature_unit = "mm"
        params.pop("unit", None)
        params = self._normalize_operation_parameter_values(canonical_type, params)
        params, raw_parameter_sources = self._normalize_raw_aliases(
            canonical_type,
            params,
            feature_id,
            errors,
        )

        profile_request: dict[str, Any] | None = None
        if canonical_type == "profile_extrude":
            profile_result = normalize_profile_extrude_request(
                params,
                task_type=task_type,
                default_unit=feature_unit,
            )
            if not profile_result.get("success"):
                for issue in profile_result.get("errors", []):
                    errors.append(self._issue(
                        str(issue.get("code") or "invalid_profile_extrude"),
                        str(issue.get("message") or "Invalid closed profile extrusion."),
                        feature_id=feature_id,
                        field=str(issue.get("field") or "params"),
                    ))
            else:
                profile_request = {
                    key: copy.deepcopy(value)
                    for key, value in profile_result.items()
                    if key not in {"success", "message", "errors"}
                }
                params = copy.deepcopy(profile_request)

        ir_parameters: dict[str, Any] = {}
        parameter_sources: dict[str, list[str]] = dict(raw_parameter_sources)
        if profile_request is not None:
            for key in (
                "body_operation",
                "mode",
                "sketch_plane",
                "end_condition",
                "depth_mm",
                "reverse_direction",
                "merge_result",
                "profiles",
                "profile_metrics",
            ):
                if key in profile_request:
                    ir_parameters[key] = copy.deepcopy(profile_request[key])
                    parameter_sources.setdefault(key, [key])
        specs = self.FEATURE_FIELDS.get(canonical_type, ())
        for spec in specs:
            invalid_unit = self._unsupported_quantity_unit(params, spec.aliases)
            if invalid_unit is not None:
                alias, unit = invalid_unit
                errors.append(self._issue(
                    "unsupported_unit",
                    f"Feature {name!r} uses unsupported unit {unit!r} for {alias}.",
                    feature_id=feature_id,
                    field=spec.canonical,
                ))
                continue
            candidates = self._quantity_candidates(params, spec.aliases, feature_unit)
            if not candidates and canonical_type == "base_plate":
                candidates = self._quantity_candidates(global_parameters, spec.aliases, "mm")
            if not candidates:
                if spec.required and bool(feature.get("required", True)):
                    errors.append(self._issue(
                        "missing_required_parameter",
                        f"Feature {name!r} requires {spec.canonical}.",
                        feature_id=feature_id,
                        field=spec.canonical,
                    ))
                continue
            values = [item[1] for item in candidates]
            if not self._values_agree(values):
                errors.append(self._issue(
                    "conflicting_parameter_aliases",
                    f"Feature {name!r} provides conflicting values for {spec.canonical}: {candidates!r}",
                    feature_id=feature_id,
                    field=spec.canonical,
                ))
                continue
            value = float(values[0])
            if spec.positive and value <= 0:
                errors.append(self._issue(
                    "parameter_out_of_range",
                    f"Feature {name!r} requires {spec.canonical} > 0.",
                    feature_id=feature_id,
                    field=spec.canonical,
                ))
                continue
            for alias in spec.aliases:
                params.pop(alias, None)
            params[spec.compatibility] = value
            ir_parameters[spec.canonical] = value
            parameter_sources[spec.canonical] = [item[0] for item in candidates]

        for canonical in self.RAW_ALIAS_GROUPS.get(canonical_type, {}):
            if canonical in params:
                ir_parameters[canonical] = copy.deepcopy(params[canonical])

        self._validate_required_parameter_groups(
            canonical_type,
            params,
            ir_parameters,
            feature_id,
            name,
            bool(feature.get("required", True)),
            errors,
        )

        if canonical_type == "through_hole":
            if original_type in {"center_hole", "centre_hole"}:
                params.setdefault("position", "center")
            count = self._positive_integer(params.get("count", 1))
            if count is None:
                errors.append(self._issue(
                    "parameter_out_of_range",
                    f"Feature {name!r} requires a positive integer count.",
                    feature_id=feature_id,
                    field="count",
                ))
            else:
                params["count"] = count
                ir_parameters["count"] = count
        elif canonical_type in {"bolt_circle_pattern", "linear_pattern", "circular_pattern"}:
            count = self._positive_integer(params.get("count"))
            if count is None:
                errors.append(self._issue(
                    "parameter_out_of_range",
                    f"Feature {name!r} requires count to be a positive integer.",
                    feature_id=feature_id,
                    field="count",
                ))
            else:
                params["count"] = count
                ir_parameters["count"] = count
        elif canonical_type == "external_thread":
            self._validate_external_thread_parameters(
                params,
                ir_parameters,
                feature_id,
                name,
                errors,
            )
        elif canonical_type == "dome":
            from .active_model_feature_skill import ActiveModelFeatureSkill

            dome_request = ActiveModelFeatureSkill.normalize_dome_request(params)
            if not dome_request.get("success"):
                errors.append(self._issue(
                    "invalid_dome_request",
                    str(dome_request.get("message") or "Invalid dome request."),
                    feature_id=feature_id,
                    field="params",
                ))
            else:
                params.update({
                    "mode": dome_request["mode"],
                    "reverse_direction": dome_request["reverse_direction"],
                    "elliptical": dome_request["elliptical"],
                    "face_selector": copy.deepcopy(dome_request["face_selector"]),
                })
                ir_parameters.update({
                    "mode": dome_request["mode"],
                    "reverse_direction": dome_request["reverse_direction"],
                    "elliptical": dome_request["elliptical"],
                    "face_selector": copy.deepcopy(dome_request["face_selector"]),
                })

        params = self._with_compatibility_aliases(canonical_type, params)
        feature["params"] = params
        target = self._target_contract(canonical_type, params, has_base_plate, task_type, feature)
        if target["resolution"] == "unresolved" and canonical_type in {
            "through_hole",
            "boss",
            "pocket",
            "slot",
            "dome",
        }:
            warnings.append(self._issue(
                "target_geometry_unresolved",
                f"Feature {name!r} still requires Geometry Resolver before execution on a complex model.",
                feature_id=feature_id,
                field="target",
            ))
        ir_feature = {
            "id": feature_id,
            "name": name,
            "operation": canonical_type,
            "required": bool(feature.get("required", True)),
            "parameters": ir_parameters,
            "options": copy.deepcopy(params),
            "target": target,
            "dependencies": [
                {"kind": "feature", "ref": value}
                for value in self._dependency_refs(feature, params)
            ],
            "source": {
                "feature_type": original_type,
                "parameter_keys": parameter_sources,
            },
        }
        return feature, ir_feature

    @staticmethod
    def _with_compatibility_aliases(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(params)
        if operation == "linear_pattern":
            if "count" in result:
                result["count_1"] = result["count"]
            if "spacing" in result:
                result["spacing_1"] = result["spacing"]
            if "direction" in result:
                result["direction_1"] = result["direction"]
        elif operation == "revolve":
            if "body_operation" in result:
                result["operation"] = result["body_operation"]
            if "execution_mode" in result:
                result["mode"] = result["execution_mode"]
        return result

    @staticmethod
    def _normalize_operation_parameter_values(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(params)
        if operation == "external_thread":
            representation = str(result.get("representation") or "").strip().lower()
            result["representation"] = representation.replace("-", "_").replace(" ", "_")
            end = str(result.get("end") or result.get("shaft_end") or "").strip().lower()
            result["end"] = {
                "left": "min",
                "start": "min",
                "minimum": "min",
                "right": "max",
                "finish": "max",
                "maximum": "max",
            }.get(end, end)
            axis = str(result.get("axis") or result.get("shaft_axis") or "").strip().lower()
            result["axis"] = {
                "horizontal": "x",
                "local_horizontal": "x",
                "vertical": "y",
                "local_vertical": "y",
            }.get(axis, axis)
            standard = str(result.get("standard") or "").strip().lower().replace("-", "_").replace(" ", "_")
            result["standard"] = {"iso_metric": "iso", "metric_iso": "iso"}.get(standard, standard)
            standard_type = str(result.get("standard_type") or "").strip().lower().replace("-", "_").replace(" ", "_")
            result["standard_type"] = {"metricdie": "metric_die"}.get(standard_type, standard_type)
            selector = str(result.get("edge_selector") or "").strip().lower().replace("-", "_").replace(" ", "_")
            result["edge_selector"] = selector
            designation = str(result.get("designation") or result.get("thread") or "").strip().upper()
            designation = designation.replace("×", "X").replace(" ", "")
            result["designation"] = designation
            return result
        if operation != "revolve":
            return result

        operation_aliases = {
            "revolve_base": "base",
            "base": "base",
            "revolve_boss": "boss",
            "add": "boss",
            "boss": "boss",
            "revolve_cut": "cut",
            "cut": "cut",
        }
        mode_token = str(result.get("mode") or "").strip().lower()
        mode_token = mode_token.replace("-", "_").replace(" ", "_")
        if mode_token in operation_aliases:
            result.setdefault("body_operation", operation_aliases[mode_token])
            result.pop("mode", None)
        elif mode_token in {"new_model", "active_model"}:
            result.setdefault("execution_mode", mode_token)
            result.pop("mode", None)

        for key in ("body_operation", "operation", "revolve_type"):
            if key in result and result[key] not in (None, ""):
                token = str(result[key]).strip().lower().replace("-", "_").replace(" ", "_")
                result[key] = operation_aliases.get(token, token)

        body_operation = str(
            result.get("body_operation")
            or result.get("operation")
            or result.get("revolve_type")
            or "base"
        ).strip().lower()
        if "execution_mode" not in result:
            result["execution_mode"] = "new_model" if body_operation == "base" else "active_model"

        plane = str(result.get("sketch_plane") or result.get("plane") or "front").strip().lower()
        plane = plane.replace("-", "_").replace(" ", "_")
        result["sketch_plane"] = {
            "front_plane": "front",
            "top_plane": "top",
            "right_plane": "right",
        }.get(plane, plane)

        axis = str(result.get("axis") or result.get("revolve_axis") or "horizontal").strip().lower()
        axis = axis.replace("-", "_").replace(" ", "_")
        result["axis"] = {
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
        return result

    def _validate_external_thread_parameters(
        self,
        params: dict[str, Any],
        ir_parameters: dict[str, Any],
        feature_id: str,
        feature_name: str,
        errors: list[dict[str, Any]],
    ) -> None:
        representation = str(params.get("representation") or "").strip().lower()
        if representation not in {"cosmetic", "modeled"}:
            errors.append(self._issue(
                "unsupported_thread_representation",
                f"Feature {feature_name!r} requires representation='cosmetic' or 'modeled'.",
                feature_id=feature_id,
                field="representation",
            ))
        thread_method = str(params.get("thread_method") or "").strip().lower().replace("-", "_")
        if representation == "modeled" and thread_method not in {"cut", "swept_cut", "cut_thread"}:
            errors.append(self._issue(
                "invalid_modeled_thread_method",
                f"Feature {feature_name!r} requires thread_method='cut' for modeled external-thread geometry.",
                feature_id=feature_id,
                field="thread_method",
            ))
        if representation == "cosmetic" and thread_method not in {"", "cosmetic", "annotation"}:
            errors.append(self._issue(
                "invalid_cosmetic_thread_method",
                f"Feature {feature_name!r} cosmetic representation cannot request a geometry-cut thread method.",
                feature_id=feature_id,
                field="thread_method",
            ))
        if representation == "modeled":
            for field in ("right_handed", "trim_start_face", "trim_end_face"):
                if field not in params or not isinstance(params.get(field), bool):
                    errors.append(self._issue(
                        "modeled_thread_parameter_missing",
                        f"Feature {feature_name!r} modeled external thread requires explicit boolean {field!r}.",
                        feature_id=feature_id,
                        field=field,
                    ))
            start_angle = params.get("thread_start_angle_deg")
            try:
                valid_start_angle = start_angle is not None and math.isfinite(float(start_angle)) and 0.0 <= float(start_angle) < 360.0
            except (TypeError, ValueError):
                valid_start_angle = False
            if not valid_start_angle:
                errors.append(self._issue(
                    "invalid_thread_start_angle",
                    f"Feature {feature_name!r} modeled external thread requires thread_start_angle_deg in [0, 360).",
                    feature_id=feature_id,
                    field="thread_start_angle_deg",
                ))
        if str(params.get("end") or "").strip().lower() not in {"min", "max"}:
            errors.append(self._issue(
                "invalid_thread_end",
                f"Feature {feature_name!r} requires end='min' or end='max'.",
                feature_id=feature_id,
                field="end",
            ))
        if str(params.get("axis") or "").strip().lower() not in {"x", "y", "z"}:
            errors.append(self._issue(
                "invalid_thread_axis",
                f"Feature {feature_name!r} requires axis x, y, or z.",
                feature_id=feature_id,
                field="axis",
            ))
        if str(params.get("standard") or "").strip().lower() != "iso":
            errors.append(self._issue(
                "unsupported_thread_standard",
                f"Feature {feature_name!r} currently supports standard='iso' only.",
                feature_id=feature_id,
                field="standard",
            ))
        if str(params.get("standard_type") or "").strip().lower() != "metric_die":
            errors.append(self._issue(
                "unsupported_thread_standard_type",
                f"Feature {feature_name!r} currently supports standard_type='metric_die' only.",
                feature_id=feature_id,
                field="standard_type",
            ))
        if str(params.get("edge_selector") or "").strip().lower() != "axial_major_diameter_end_edge":
            errors.append(self._issue(
                "unsupported_thread_edge_selector",
                f"Feature {feature_name!r} requires edge_selector='axial_major_diameter_end_edge'.",
                feature_id=feature_id,
                field="edge_selector",
            ))
        axis_center = params.get("axis_center_mm")
        if axis_center is not None:
            valid_center = isinstance(axis_center, (tuple, list)) and len(axis_center) == 2
            if valid_center:
                try:
                    valid_center = all(math.isfinite(float(value)) for value in axis_center)
                except (TypeError, ValueError):
                    valid_center = False
            if not valid_center:
                errors.append(self._issue(
                    "invalid_thread_axis_center",
                    f"Feature {feature_name!r} axis_center_mm must contain two finite radial coordinates.",
                    feature_id=feature_id,
                    field="axis_center_mm",
                ))

        designation = str(params.get("designation") or "").strip().upper()
        match = re.fullmatch(r"M(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)", designation)
        if match is None:
            errors.append(self._issue(
                "invalid_thread_designation",
                f"Feature {feature_name!r} requires an explicit metric designation such as M16X2.0.",
                feature_id=feature_id,
                field="designation",
            ))
            return
        expected_major = float(match.group(1))
        expected_pitch = float(match.group(2))
        actual_major = float(ir_parameters.get("major_diameter_mm", 0.0) or 0.0)
        actual_pitch = float(ir_parameters.get("pitch_mm", 0.0) or 0.0)
        if not math.isclose(actual_major, expected_major, rel_tol=0.0, abs_tol=1e-6):
            errors.append(self._issue(
                "thread_designation_conflict",
                f"Feature {feature_name!r} designation {designation} conflicts with major_diameter_mm={actual_major}.",
                feature_id=feature_id,
                field="major_diameter_mm",
            ))
        if not math.isclose(actual_pitch, expected_pitch, rel_tol=0.0, abs_tol=1e-6):
            errors.append(self._issue(
                "thread_designation_conflict",
                f"Feature {feature_name!r} designation {designation} conflicts with pitch_mm={actual_pitch}.",
                feature_id=feature_id,
                field="pitch_mm",
            ))

    def _normalize_raw_aliases(
        self,
        operation: str,
        params: dict[str, Any],
        feature_id: str,
        errors: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        result = copy.deepcopy(params)
        sources: dict[str, list[str]] = {}
        for canonical, aliases in self.RAW_ALIAS_GROUPS.get(operation, {}).items():
            candidates = [(alias, result[alias]) for alias in aliases if alias in result and result[alias] not in (None, "")]
            if not candidates:
                continue
            normalized_values = [self._normalize_raw_value(canonical, value) for _, value in candidates]
            if any(value != normalized_values[0] for value in normalized_values[1:]):
                errors.append(self._issue(
                    "conflicting_parameter_aliases",
                    f"Feature {feature_id!r} provides conflicting values for {canonical}: {candidates!r}",
                    feature_id=feature_id,
                    field=canonical,
                ))
                continue
            for alias in aliases:
                result.pop(alias, None)
            result[canonical] = normalized_values[0]
            sources[canonical] = [alias for alias, _ in candidates]
        return result, sources

    @staticmethod
    def _normalize_raw_value(canonical: str, value: Any) -> Any:
        if canonical == "seed_features":
            values = value if isinstance(value, (list, tuple)) else [value]
            return [str(item).strip() for item in values if str(item).strip()]
        if canonical == "count":
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return value
            return int(numeric) if numeric.is_integer() else numeric
        if canonical in {
            "thread",
            "axis",
            "direction",
            "mirror_plane",
            "name",
            "body_operation",
            "execution_mode",
            "sketch_plane",
        }:
            return str(value).strip()
        return copy.deepcopy(value)

    def _validate_required_parameter_groups(
        self,
        operation: str,
        params: dict[str, Any],
        ir_parameters: dict[str, Any],
        feature_id: str,
        feature_name: str,
        required: bool,
        errors: list[dict[str, Any]],
    ) -> None:
        if not required:
            return
        for group in self.REQUIRED_PARAMETER_GROUPS.get(operation, ()):
            if any(self._parameter_present(params, ir_parameters, name) for name in group):
                continue
            field = "|".join(group)
            errors.append(self._issue(
                "missing_required_parameter",
                f"Feature {feature_name!r} requires one of: {', '.join(group)}.",
                feature_id=feature_id,
                field=field,
            ))

    @staticmethod
    def _parameter_present(params: dict[str, Any], ir_parameters: dict[str, Any], name: str) -> bool:
        for source in (ir_parameters, params):
            if name not in source:
                continue
            value = source[name]
            if value is None or value == "":
                continue
            if isinstance(value, (list, tuple, dict)) and not value:
                continue
            return True
        return False

    def _normalize_global_parameters(
        self,
        parameters: dict[str, Any],
        default_unit: str,
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = copy.deepcopy(parameters)
        groups = (
            QuantityField("length_mm", "length", ("length_mm", "length", "overall_length_mm", "overall_length"), required=False),
            QuantityField("width_mm", "width", ("width_mm", "width", "overall_width_mm", "overall_width"), required=False),
            QuantityField("thickness_mm", "thickness", ("thickness_mm", "thickness", "height_mm", "height"), required=False),
        )
        for spec in groups:
            invalid_unit = self._unsupported_quantity_unit(result, spec.aliases)
            if invalid_unit is not None:
                alias, unit = invalid_unit
                errors.append(self._issue(
                    "unsupported_unit",
                    f"Design parameter {alias!r} uses unsupported unit {unit!r}.",
                    field=f"parameters.{spec.canonical}",
                ))
                continue
            candidates = self._quantity_candidates(result, spec.aliases, default_unit)
            if not candidates:
                continue
            if not self._values_agree([item[1] for item in candidates]):
                errors.append(self._issue(
                    "conflicting_parameter_aliases",
                    f"Design parameters provide conflicting values for {spec.canonical}: {candidates!r}",
                    field=f"parameters.{spec.canonical}",
                ))
                continue
            for alias in spec.aliases:
                result.pop(alias, None)
            result[spec.compatibility] = float(candidates[0][1])
        result["unit"] = "mm"
        return result

    def _unsupported_quantity_unit(
        self,
        values: dict[str, Any],
        aliases: tuple[str, ...],
    ) -> tuple[str, Any] | None:
        for alias in aliases:
            value = values.get(alias)
            if not isinstance(value, dict) or value.get("unit") in (None, ""):
                continue
            if self._normalize_unit(value.get("unit")) is None:
                return alias, value.get("unit")
        return None

    def _quantity_candidates(
        self,
        values: dict[str, Any],
        aliases: tuple[str, ...],
        default_unit: str,
    ) -> list[tuple[str, float]]:
        candidates: list[tuple[str, float]] = []
        for alias in aliases:
            if alias not in values or values[alias] in (None, ""):
                continue
            key_unit = "mm" if alias.endswith("_mm") else default_unit
            converted = self._quantity_mm(values[alias], key_unit)
            if converted is not None:
                candidates.append((alias, converted))
        return candidates

    def _quantity_mm(self, value: Any, default_unit: str) -> float | None:
        unit = default_unit
        raw = value
        if isinstance(value, dict):
            raw = value.get("value")
            unit = self._normalize_unit(value.get("unit") or default_unit) or ""
        if unit not in self.UNIT_FACTORS_MM:
            return None
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return numeric * self.UNIT_FACTORS_MM[unit]

    @classmethod
    def _canonical_feature_type(cls, value: str) -> tuple[str, bool]:
        token = cls._type_token(value)
        if token in cls.AMBIGUOUS_FEATURE_TYPES:
            return token, True
        return cls.FEATURE_ALIASES.get(token, token), False

    @staticmethod
    def _type_token(value: Any) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")

    @classmethod
    def _normalize_unit(cls, value: Any) -> str | None:
        token = str(value or "").strip().lower()
        return token if token in cls.UNIT_FACTORS_MM else None

    @staticmethod
    def _values_agree(values: list[float]) -> bool:
        if not values:
            return True
        tolerance = max(1e-6, max(abs(value) for value in values) * 1e-6)
        return all(abs(value - values[0]) <= tolerance for value in values[1:])

    @staticmethod
    def _positive_integer(value: Any) -> int | None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
            return None
        return int(numeric)

    @staticmethod
    def _target_contract(
        feature_type: str,
        params: dict[str, Any],
        has_base_plate: bool,
        task_type: str,
        feature: dict[str, Any],
    ) -> dict[str, Any]:
        if feature_type == "dome" and isinstance(params.get("face_selector"), dict):
            semantic = feature.get("target_reference")
            semantic = semantic if isinstance(semantic, dict) else {}
            return {
                "resolution": "explicit_planar_face_signature",
                "body_ref": feature.get("target_body") or "primary_solid",
                "feature_ref": semantic.get("feature_id"),
                "face_role": semantic.get("role") or "dome_planar_face",
                "face_selector": copy.deepcopy(params["face_selector"]),
            }
        geometry = params.get("geometry_context") or params.get("target_geometry")
        if isinstance(geometry, dict) and geometry:
            return {
                "resolution": "explicit",
                "body_ref": geometry.get("target_body"),
                "feature_ref": geometry.get("target_feature"),
                "face_role": geometry.get("target_face_role"),
                "local_frame": {
                    "origin_mm": geometry.get("face_origin"),
                    "u_axis": geometry.get("local_u_axis"),
                    "v_axis": geometry.get("local_v_axis"),
                    "normal": geometry.get("face_normal"),
                },
            }
        semantic = feature.get("target_reference")
        if isinstance(semantic, dict) and semantic:
            return {
                "resolution": "semantic_reference",
                "body_ref": feature.get("target_body"),
                "feature_ref": semantic.get("feature_id"),
                "face_role": semantic.get("role"),
            }
        if feature_type == "base_plate":
            return {"resolution": "new_body", "body_ref": "primary_solid"}
        if feature_type == "profile_extrude":
            if str(params.get("mode") or "") == "new_model":
                return {
                    "resolution": "new_body",
                    "body_ref": "primary_solid",
                    "feature_ref": None,
                    "face_role": str(params.get("sketch_plane") or ""),
                }
            return {
                "resolution": "active_document",
                "body_ref": "active_part",
                "feature_ref": None,
                "face_role": str(params.get("sketch_plane") or ""),
            }
        if has_base_plate and feature_type in CADIRCompiler.MODIFIER_OPERATIONS:
            face_role = None
            if feature_type in {"through_hole", "threaded_hole", "bolt_circle_pattern", "boss", "pocket", "slot"}:
                face_role = "outer_horizontal_face"
            elif feature_type in {"fillet", "chamfer"}:
                face_role = "selected_edges"
            elif feature_type in {"linear_pattern", "circular_pattern", "mirror"}:
                face_role = "seed_features"
            return {
                "resolution": "base_plate_default",
                "body_ref": "primary_solid",
                "feature_ref": "base_plate",
                "face_role": face_role,
            }
        if task_type == "modify_3d" and feature_type in CADIRCompiler.MODIFIER_OPERATIONS:
            return {
                "resolution": "active_document",
                "body_ref": "active_part",
                "feature_ref": None,
                "face_role": params.get("target_face_role") or params.get("target_face"),
            }
        return {"resolution": "unresolved"}

    @classmethod
    def _dependency_refs(cls, feature: dict[str, Any], params: dict[str, Any]) -> list[str]:
        values: list[Any] = []
        for source in (
            feature.get("depends_on"),
            feature.get("dependencies"),
            params.get("depends_on"),
            params.get("dependencies"),
            params.get("seed_features"),
            params.get("seed_feature"),
        ):
            if source is None:
                continue
            values.extend(source if isinstance(source, (list, tuple)) else [source])
        result: list[str] = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("feature_id") or value.get("name") or value.get("ref")
            token = str(value or "").strip()
            if token and token not in result:
                result.append(token)
        return result

    def _resolve_dependencies(
        self,
        features: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> None:
        by_id = {str(item["id"]).casefold(): item for item in features}
        by_name = {str(item["name"]).casefold(): item for item in features}
        by_operation: dict[str, list[dict[str, Any]]] = {}
        for item in features:
            by_operation.setdefault(str(item["operation"]).casefold(), []).append(item)
        positions = {str(item["id"]): index for index, item in enumerate(features)}

        def resolve(ref: Any) -> dict[str, Any] | None:
            token = str(ref or "").strip().casefold()
            if not token:
                return None
            if token in by_id:
                return by_id[token]
            if token in by_name:
                return by_name[token]
            candidates = by_operation.get(token, [])
            return candidates[0] if len(candidates) == 1 else None

        for index, feature in enumerate(features):
            feature_id = str(feature["id"])
            dependencies: list[dict[str, Any]] = []
            for raw_dependency in list(feature.get("dependencies") or []):
                target = resolve(raw_dependency.get("ref"))
                if target is None:
                    errors.append(self._issue(
                        "dependency_not_found",
                        f"Feature {feature['name']!r} references unknown dependency {raw_dependency.get('ref')!r}.",
                        feature_id=feature_id,
                        field="dependencies",
                    ))
                    continue
                target_id = str(target["id"])
                if target_id == feature_id:
                    errors.append(self._issue(
                        "self_dependency",
                        f"Feature {feature['name']!r} cannot depend on itself.",
                        feature_id=feature_id,
                        field="dependencies",
                    ))
                    continue
                dependencies.append({"kind": "feature", "feature_id": target_id})

            target_ref = feature.get("target", {}).get("feature_ref")
            if target_ref:
                target = resolve(target_ref)
                if target is None:
                    errors.append(self._issue(
                        "target_reference_not_found",
                        f"Feature {feature['name']!r} targets unknown feature {target_ref!r}.",
                        feature_id=feature_id,
                        field="target.feature_ref",
                    ))
                else:
                    target_id = str(target["id"])
                    feature["target"]["feature_ref"] = target_id
                    dependencies.append({"kind": "target_feature", "feature_id": target_id})

            operation = str(feature.get("operation") or "")
            if (
                operation in self.MODIFIER_OPERATIONS
                and not dependencies
                and feature.get("target", {}).get("resolution") != "new_body"
            ):
                if feature.get("target", {}).get("resolution") == "active_document":
                    dependencies.append({
                        "kind": "active_document",
                        "document_ref": "active_part",
                    })
                elif index > 0:
                    dependencies.append({
                        "kind": "preceding_feature",
                        "feature_id": str(features[index - 1]["id"]),
                    })
                else:
                    errors.append(self._issue(
                        "missing_feature_dependency",
                        f"Feature {feature['name']!r} requires an earlier body feature or active_part dependency.",
                        feature_id=feature_id,
                        field="dependencies",
                    ))

            unique: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for dependency in dependencies:
                ref = str(dependency.get("feature_id") or dependency.get("document_ref") or "")
                key = (str(dependency.get("kind") or ""), ref)
                if key not in seen:
                    seen.add(key)
                    unique.append(dependency)
            feature["dependencies"] = unique
            for dependency in unique:
                dependency_id = dependency.get("feature_id")
                if dependency_id and positions.get(str(dependency_id), -1) >= index:
                    errors.append(self._issue(
                        "dependency_order_invalid",
                        f"Feature {feature['name']!r} depends on {dependency_id!r}, which is not earlier in the operation order.",
                        feature_id=feature_id,
                        field="dependencies",
                    ))

        graph = {
            str(item["id"]): [
                str(dep["feature_id"])
                for dep in item.get("dependencies", [])
                if dep.get("feature_id")
            ]
            for item in features
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            cyclic = any(visit(dependency) for dependency in graph.get(node, []))
            visiting.remove(node)
            visited.add(node)
            return cyclic

        if any(visit(node) for node in graph):
            errors.append(self._issue(
                "dependency_cycle",
                "CAD-IR feature dependencies contain a cycle.",
                field="features.dependencies",
            ))

    @staticmethod
    def _unique_id(name: str, used: set[str]) -> str:
        base = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_") or "feature"
        value = base
        index = 2
        while value in used:
            value = f"{base}_{index}"
            index += 1
        used.add(value)
        return value

    @staticmethod
    def _issue(
        code: str,
        message: str,
        *,
        feature_id: str | None = None,
        field: str | None = None,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "message": message,
            "feature_id": feature_id,
            "field": field,
        }
