from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.design_planner import DesignPlanner
from cad_agent.engineering_knowledge import EngineeringKnowledgeBase
from cad_agent.freeform_surface_skill import FreeformSurfaceSkill
from cad_agent.pipeline.pipeline_context import PipelineStepRecord
from cad_agent.pipeline.pipeline_executor import PipelineExecutor
from cad_agent.registry import CADAgentSkillManager
from cad_agent.sheet_metal_skill import SheetMetalSkill
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan
from cad_agent.weldment_skill import WeldmentSkill


def _sheet_feature() -> dict:
    return {
        "name": "UChannel",
        "type": "sheet_metal",
        "required": True,
        "params": {
            "mode": "new_model",
            "base_plane": "top",
            "length_mm": 100,
            "width_mm": 60,
            "thickness_mm": 2,
            "bend_radius_mm": 2,
            "edge_flanges": [
                {"edge": "width_positive", "height_mm": 20, "angle_deg": 90},
                {"edge": "width_negative", "height_mm": 20, "angle_deg": 90},
            ],
            "flat_pattern": {"enabled": False, "final_state": "folded"},
        },
    }


def _weldment_feature() -> dict:
    return {
        "name": "Frame",
        "type": "weldment",
        "required": True,
        "params": {
            "mode": "new_model",
            "standard": "iso",
            "profile_type": "square_tube",
            "profile_configuration": "20 x 20 x 2",
            "path_segments_mm": [
                [[-100, -60, 0], [100, -60, 0]],
                [[100, -60, 0], [100, 60, 0]],
                [[100, 60, 0], [-100, 60, 0]],
                [[-100, 60, 0], [-100, -60, 0]],
            ],
            "groups": [[0, 1, 2, 3]],
            "corner_treatment": "miter",
        },
    }


def _wire_mesh_feature() -> dict:
    return {
        "name": "WireMeshDoor",
        "type": "weldment",
        "required": True,
        "params": {
            "mode": "new_model",
            "wire_mesh": {
                "wire_diameter_mm": 4,
                "arc_radius_mm": 269,
                "arc_length_mm": 276.4,
                "outer_surface_chord_mm": 264.75,
                "transverse_profile_depth_mm": 37.7,
                "longitudinal_chord_pitches_mm": [54.38, 55.1, 55.1, 55.1, 54.38],
                "overall_height_mm": 338,
                "mesh_height_mm": 310,
                "end_pitch_mm": 45,
                "middle_pitch_mm": 54,
                "overall_depth_mm": 40.3,
                "longitudinal_count": 6,
                "transverse_count": 7,
            },
        },
    }


def _lofted_bend_feature() -> dict:
    return {
        "name": "LoftedTransition",
        "type": "sheet_metal",
        "required": True,
        "params": {
            "mode": "new_model",
            "thickness_mm": 2,
            "bend_radius_mm": 2,
            "lofted_bend": {
                "base_plane": "front",
                "plane_offset_mm": 80,
                "profile_1_points_mm": [[-40, 0], [-20, 20], [20, 20], [40, 0]],
                "profile_2_points_mm": [[-30, 0], [-15, 30], [15, 30], [30, 0]],
                "thickness_direction": "outside",
                "formed": False,
                "refer_to_endpoint": True,
                "facet_option": "bends_per_transition",
                "bends_per_transition": 4,
            },
            "flat_pattern": {"enabled": True, "final_state": "folded"},
        },
    }


def _helical_lofted_bend_feature() -> dict:
    return {
        "name": "HelicalBlade",
        "type": "sheet_metal",
        "required": True,
        "params": {
            "mode": "new_model",
            "thickness_mm": 3,
            "bend_radius_mm": 0.7366,
            "lofted_bend": {
                "profile_mode": "coaxial_helices",
                "base_plane": "top",
                "axis": "y",
                "center_uv_mm": [0, 0],
                "inner_radius_mm": 28.5,
                "outer_radius_mm": 61.5,
                "height_mm": 70,
                "pitch_mm": 70,
                "revolutions": 1,
                "start_angle_deg": 0,
                "clockwise": False,
                "reverse_direction": False,
                "thickness_direction": "outside",
                "formed": True,
                "refer_to_endpoint": True,
                "facet_option": "chord_tolerance",
                "chord_tolerance_mm": 0.5,
                "number_of_bend_lines": 3,
            },
            "flat_pattern": {"enabled": True, "final_state": "folded"},
        },
    }


def _forming_tool_feature() -> dict:
    return {
        "name": "DimpledPanel",
        "type": "sheet_metal",
        "required": True,
        "params": {
            "mode": "new_model",
            "base_plane": "top",
            "length_mm": 160,
            "width_mm": 120,
            "thickness_mm": 1.5,
            "bend_radius_mm": 1.5,
            "forming_tools": [{
                "tool": "dimple",
                "position_x_mm": 0,
                "position_y_mm": 0,
                "rotation_deg": 15,
                "link_to_library": False,
                "show_punch": True,
                "show_profile": True,
                "show_center": True,
            }],
            "flat_pattern": {"enabled": True, "final_state": "folded"},
        },
    }


def _surface_feature() -> dict:
    return {
        "name": "ErgonomicSurface",
        "type": "freeform_surface",
        "required": True,
        "params": {
            "mode": "new_model",
            "surface_type": "fill",
            "resolution": 3,
            "boundary_curves_mm": [
                [[-50, -30, 0], [0, -40, 15], [50, -30, 0]],
                [[50, -30, 0], [60, 0, 8], [50, 30, 0]],
                [[50, 30, 0], [0, 40, 20], [-50, 30, 0]],
                [[-50, 30, 0], [-60, 0, 8], [-50, -30, 0]],
            ],
        },
    }


def _design(feature: dict) -> dict:
    return {
        "source_brief": "Create only this explicit 3D Part and save SLDPRT.",
        "task_type": "model_3d",
        "parameters": {"unit": "mm"},
        "features": [feature],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_sheet_metal_schema_and_fail_closed_rules() -> None:
    request = SheetMetalSkill.normalize_request(_sheet_feature()["params"], "model_3d")
    assert request["success"]
    assert request["edge_flanges"][0]["height_mm"] == 20.0
    angled = dict(_sheet_feature()["params"])
    angled["edge_flanges"] = [{"edge": "width_positive", "height_mm": 20, "angle_deg": 60}]
    angled["flat_pattern"] = {"enabled": True, "final_state": "flattened"}
    angled_request = SheetMetalSkill.normalize_request(angled, "model_3d")
    assert angled_request["success"]
    assert angled_request["edge_flanges"][0]["angle_deg"] == 60.0
    assert angled_request["flat_pattern"]["final_state"] == "flattened"
    bad_angle = dict(_sheet_feature()["params"])
    bad_angle["edge_flanges"] = [{"edge": "width_positive", "height_mm": 20, "angle_deg": 10}]
    duplicate = dict(_sheet_feature()["params"])
    duplicate["edge_flanges"] = [
        {"edge": "width_positive", "height_mm": 20},
        {"edge": "width_positive", "height_mm": 15},
    ]
    assert not SheetMetalSkill.normalize_request(bad_angle, "model_3d")["success"]
    assert not SheetMetalSkill.normalize_request(duplicate, "model_3d")["success"]
    bad_state = dict(_sheet_feature()["params"])
    bad_state["flat_pattern"] = {"enabled": True, "final_state": "exploded"}
    assert not SheetMetalSkill.normalize_request(bad_state, "model_3d")["success"]

    sketched = dict(_sheet_feature()["params"])
    sketched["edge_flanges"] = []
    sketched["sketched_bends"] = [{
        "line_orientation": "parallel_to_width",
        "offset_mm": 20,
        "angle_deg": 60,
        "bend_radius_mm": 2,
        "fixed_side": "negative",
        "reverse_direction": False,
    }]
    sketched_request = SheetMetalSkill.normalize_request(sketched, "model_3d")
    assert sketched_request["success"]
    assert sketched_request["sketched_bends"][0]["offset_mm"] == 20.0
    assert sketched_request["sketched_bends"][0]["line_orientation"] == "parallel_to_width"

    mixed = dict(sketched)
    mixed["edge_flanges"] = [{"edge": "width_positive", "height_mm": 20}]
    assert not SheetMetalSkill.normalize_request(mixed, "model_3d")["success"]
    missing_offset = dict(sketched)
    missing_offset["sketched_bends"] = [{"line_orientation": "parallel_to_width", "angle_deg": 60}]
    assert not SheetMetalSkill.normalize_request(missing_offset, "model_3d")["success"]
    short_leg = dict(sketched)
    short_leg["sketched_bends"] = [{"line_orientation": "parallel_to_width", "offset_mm": 49, "angle_deg": 60}]
    assert not SheetMetalSkill.normalize_request(short_leg, "model_3d")["success"]

    hemmed = dict(_sheet_feature()["params"])
    hemmed["edge_flanges"] = []
    hemmed["hems"] = [{
        "edge": "width_positive",
        "type": "open",
        "position": "outside",
        "length_mm": 12,
        "gap_mm": 1,
        "bend_radius_mm": 2,
        "reverse_direction": False,
    }]
    hem_request = SheetMetalSkill.normalize_request(hemmed, "model_3d")
    assert hem_request["success"]
    assert hem_request["hems"][0]["edge"] == "width_positive"
    assert hem_request["hems"][0]["length_mm"] == 12.0
    assert hem_request["hems"][0]["gap_mm"] == 1.0
    assert SheetMetalSkill.HEM_TYPES["open"] == 0
    assert SheetMetalSkill.HEM_POSITIONS["outside"] == 1

    mixed_hem = dict(hemmed)
    mixed_hem["edge_flanges"] = [{"edge": "width_negative", "height_mm": 20}]
    assert not SheetMetalSkill.normalize_request(mixed_hem, "model_3d")["success"]
    unsupported_hem = dict(hemmed)
    unsupported_hem["hems"] = [{"edge": "width_positive", "type": "closed", "length_mm": 12, "gap_mm": 1}]
    assert not SheetMetalSkill.normalize_request(unsupported_hem, "model_3d")["success"]
    short_hem = dict(hemmed)
    short_hem["hems"] = [{"edge": "width_positive", "type": "open", "length_mm": 4, "gap_mm": 1}]
    assert not SheetMetalSkill.normalize_request(short_hem, "model_3d")["success"]
    zero_gap_hem = dict(hemmed)
    zero_gap_hem["hems"] = [{"edge": "width_positive", "type": "open", "length_mm": 12, "gap_mm": 0}]
    assert not SheetMetalSkill.normalize_request(zero_gap_hem, "model_3d")["success"]

    jogged = dict(_sheet_feature()["params"])
    jogged["edge_flanges"] = []
    jogged["jogs"] = [{
        "line_orientation": "parallel_to_width",
        "line_offset_mm": 10,
        "offset_distance_mm": 8,
        "angle_deg": 90,
        "bend_radius_mm": 2,
        "fixed_side": "negative",
        "reverse_direction": False,
        "fix_projected_length": True,
        "dimension_position": "outside_offset",
        "jog_position": "bend_centerline",
    }]
    jog_request = SheetMetalSkill.normalize_request(jogged, "model_3d")
    assert jog_request["success"]
    assert jog_request["jogs"][0]["line_offset_mm"] == 10.0
    assert jog_request["jogs"][0]["offset_distance_mm"] == 8.0
    assert SheetMetalSkill.JOG_DIMENSION_POSITIONS["outside_offset"] == 2
    assert SheetMetalSkill.JOG_POSITIONS["bend_centerline"] == 1

    mixed_jog = dict(jogged)
    mixed_jog["hems"] = [{"edge": "width_positive", "type": "open", "length_mm": 12, "gap_mm": 1}]
    assert not SheetMetalSkill.normalize_request(mixed_jog, "model_3d")["success"]
    missing_jog_height = dict(jogged)
    missing_jog_height["jogs"] = [{"line_orientation": "parallel_to_width", "line_offset_mm": 10}]
    assert not SheetMetalSkill.normalize_request(missing_jog_height, "model_3d")["success"]
    bad_jog_position = dict(jogged)
    bad_jog_position["jogs"] = [{
        "line_orientation": "parallel_to_width",
        "line_offset_mm": 10,
        "offset_distance_mm": 8,
        "dimension_position": "middle",
    }]
    assert not SheetMetalSkill.normalize_request(bad_jog_position, "model_3d")["success"]
    non_fixed_jog = dict(jogged)
    non_fixed_jog["jogs"] = [{**jogged["jogs"][0], "fix_projected_length": False}]
    assert not SheetMetalSkill.normalize_request(non_fixed_jog, "model_3d")["success"]
    inside_dimension_jog = dict(jogged)
    inside_dimension_jog["jogs"] = [{**jogged["jogs"][0], "dimension_position": "inside_offset"}]
    assert not SheetMetalSkill.normalize_request(inside_dimension_jog, "model_3d")["success"]
    material_inside_jog = dict(jogged)
    material_inside_jog["jogs"] = [{**jogged["jogs"][0], "jog_position": "material_inside"}]
    assert not SheetMetalSkill.normalize_request(material_inside_jog, "model_3d")["success"]


def test_sheet_metal_forming_tool_schema_and_whitelist_guards() -> None:
    params = _forming_tool_feature()["params"]
    request = SheetMetalSkill.normalize_request(params, "model_3d")
    assert request["success"]
    assert request["forming_tools"] == [{
        "tool": "dimple",
        "position_x_mm": 0.0,
        "position_y_mm": 0.0,
        "rotation_deg": 15.0,
        "link_to_library": False,
        "show_punch": True,
        "show_profile": True,
        "show_center": True,
        "minimum_edge_clearance_mm": 25.0,
        "sheet_thickness_mm": 1.5,
    }]

    singular = {**params, "forming_tools": None, "forming_tool": {
        "tool": "round dimple",
        "position_mm": {"x": 5, "y": -5},
    }}
    singular_request = SheetMetalSkill.normalize_request(singular, "model_3d")
    assert singular_request["success"]
    assert singular_request["forming_tools"][0]["tool"] == "dimple"
    assert singular_request["forming_tools"][0]["position_x_mm"] == 5.0
    assert singular_request["forming_tools"][0]["position_y_mm"] == -5.0

    unknown = {**params, "forming_tools": [{"tool": "../untrusted.sldftp"}]}
    outside = {**params, "forming_tools": [{"tool": "dimple", "position_x_mm": 60}]}
    multiple = {**params, "forming_tools": [{"tool": "dimple"}, {"tool": "dimple"}]}
    mixed = {
        **params,
        "edge_flanges": [{"edge": "width_positive", "height_mm": 20}],
    }
    bad_boolean = {**params, "forming_tools": [{"tool": "dimple", "link_to_library": "false"}]}
    too_small = {**params, "width_mm": 45}
    for invalid in (unknown, outside, multiple, mixed, bad_boolean, too_small):
        assert not SheetMetalSkill.normalize_request(invalid, "model_3d")["success"]


def test_sheet_metal_flat_pattern_dxf_schema_and_native_route() -> None:
    params = _forming_tool_feature()["params"]
    params = {
        **params,
        "flat_pattern": {
            "enabled": True,
            "final_state": "folded",
            "export_dxf": {
                "enabled": True,
                "file_name": "formed_panel_flat.dxf",
                "include_bend_lines": True,
                "include_sketches": False,
                "include_library_features": False,
                "include_forming_tools": True,
                "include_bounding_box": False,
            },
        },
    }
    request = SheetMetalSkill.normalize_request(params, "model_3d")
    assert request["success"]
    export = request["flat_pattern"]["export_dxf"]
    assert export["enabled"] is True
    assert export["file_name"] == "formed_panel_flat.dxf"
    assert export["include_forming_tools"] is True

    source = _design({**_forming_tool_feature(), "params": params})
    source["source_brief"] = "Create this formed sheet-metal panel and export its flat pattern DXF."
    source["outputs"] = ["SLDPRT", "DXF"]
    stage_plan = {
        "task_type": "full_pipeline",
        "requested_stages": ["model_3d", "export_files"],
        "forbidden_stages": ["drawing", "autocad_annotation"],
        "stop_after": "export_files",
        "requested_outputs": ["SLDPRT", "DXF"],
        "needs_confirmation": False,
        "confirmation_reason": "",
    }
    design = apply_stage_plan(source, stage_plan)
    assert design["execution_policy"]["allowed_skills"] == ["sheet_metal", "save_sldprt"]
    assert [step.skill_key for step in SkillPlanner().plan(design)] == ["sheet_metal", "save_sldprt"]
    assert "dwg_export" not in design["execution_policy"]["allowed_skills"]

    top_level = {**_forming_tool_feature()["params"], "flat_pattern": None, "flat_pattern_dxf": True}
    assert SheetMetalSkill.normalize_request(top_level, "model_3d")["flat_pattern"]["export_dxf"]["enabled"]
    bad_name = {**params, "flat_pattern": {**params["flat_pattern"], "export_dxf": {
        "enabled": True,
        "file_name": "..\\escaped.dxf",
    }}}
    disabled = {**params, "flat_pattern": {"enabled": False, "export_dxf": True}}
    bad_option = {**params, "flat_pattern": {"enabled": True, "export_dxf": {
        "enabled": True,
        "include_bend_lines": "yes",
    }}}
    for invalid in (bad_name, disabled, bad_option):
        assert not SheetMetalSkill.normalize_request(invalid, "model_3d")["success"]


def test_flat_pattern_dxf_inspection_requires_geometry_and_reports_extent(tmp_path: Path) -> None:
    dxf = tmp_path / "flat.dxf"
    dxf.write_text(
        "0\nSECTION\n2\nENTITIES\n"
        "0\nLINE\n8\nOUTLINE\n10\n-80\n20\n-60\n11\n80\n21\n-60\n"
        "0\nLINE\n8\nOUTLINE\n10\n80\n20\n-60\n11\n80\n21\n60\n"
        "0\nLINE\n8\nOUTLINE\n10\n80\n20\n60\n11\n-80\n21\n60\n"
        "0\nLINE\n8\nOUTLINE\n10\n-80\n20\n60\n11\n-80\n21\n-60\n"
        "0\nCIRCLE\n8\nFORMING\n10\n0\n20\n0\n40\n10\n"
        "0\nENDSEC\n0\nEOF\n",
        encoding="ascii",
    )
    inspection = SheetMetalSkill._inspect_dxf(dxf)
    assert inspection["success"]
    assert inspection["geometry_entity_count"] == 5
    assert inspection["geometry_entities"] == {"CIRCLE": 1, "LINE": 4}
    assert inspection["geometry_spans"] == {"x": 160.0, "y": 120.0}
    assert inspection["layers"] == ["FORMING", "OUTLINE"]


def test_weldment_schema_and_group_coverage_guard() -> None:
    request = WeldmentSkill.normalize_request(_weldment_feature()["params"], "model_3d")
    assert request["success"]
    assert len(request["path_segments_mm"]) == 4
    missing_segment = dict(_weldment_feature()["params"])
    missing_segment["groups"] = [[0, 1, 2]]
    zero_length = dict(_weldment_feature()["params"])
    zero_length["path_segments_mm"] = [[[0, 0, 0], [0, 0, 0]]]
    assert not WeldmentSkill.normalize_request(missing_segment, "model_3d")["success"]
    assert not WeldmentSkill.normalize_request(zero_length, "model_3d")["success"]


def test_wire_mesh_schema_derives_single_weldment_path() -> None:
    request = WeldmentSkill.normalize_request(_wire_mesh_feature()["params"], "model_3d")
    assert request["success"]
    assert request["geometry_kind"] == "wire_mesh"
    assert request["profile_type"] == "solid_round"
    assert request["profile_configuration"] == "D4"
    assert len(request["path_segments_mm"]) == 13
    assert len(request["groups"]) == 13
    assert request["wire_mesh"]["longitudinal_count"] == 6
    assert request["wire_mesh"]["transverse_count"] == 7
    assert request["wire_mesh"]["arc_entity_count"] == 7
    assert request["wire_mesh"]["continuous_transverse_wires"] is True
    assert request["wire_mesh"]["layering"] == "transverse_arc_wires_on_top"
    assert request["wire_mesh"]["layer_center_distance_mm"] == 4
    assert request["wire_mesh"]["layer_reference"] == "drawing_side_profile"
    assert request["wire_mesh"]["side_profile_matches_dwg"] is True
    assert request["wire_mesh"]["transverse_profile_above_longitudinal"] is True
    assert request["wire_mesh"]["transverse_profile_position"] == "upper"
    assert request["wire_mesh"]["longitudinal_profile_position"] == "lower"
    assert request["wire_mesh"]["inner_surface_radius_mm"] == 269
    assert request["wire_mesh"]["transverse_centerline_radius_mm"] == 271
    assert request["wire_mesh"]["outer_surface_radius_mm"] == 273
    assert request["wire_mesh"]["longitudinal_center_locus_radius_mm"] == 275
    assert request["wire_mesh"]["minimum_radial_layer_separation_mm"] == pytest.approx(4.0)
    for arc_point, longitudinal_point, contact_distance in zip(
        request["wire_mesh"]["curve_points_xz_mm"],
        request["wire_mesh"]["longitudinal_centers_xz_mm"],
        request["wire_mesh"]["layer_contact_distances_mm"],
    ):
        arc_layer_radius = math.dist(arc_point, [0.0, -271.0])
        longitudinal_layer_radius = math.dist(longitudinal_point, [0.0, -271.0])
        assert longitudinal_layer_radius > arc_layer_radius
        assert abs(longitudinal_layer_radius - arc_layer_radius - 4.0) <= 1e-6
        assert longitudinal_point[1] > arc_point[1]
        assert abs(contact_distance - 4.0) <= 1e-6
    assert request["wire_mesh"]["longitudinal_lengths_mm"] == [338, 310, 310, 310, 310, 338]
    assert abs(request["wire_mesh"]["calculated_drawing_depth_mm"] - 40.3) <= 0.75
    assert abs(request["wire_mesh"]["calculated_outer_surface_chord_mm"] - 264.75) < 0.01
    assert abs(request["wire_mesh"]["calculated_transverse_profile_depth_mm"] - 37.7) < 0.05
    assert abs(request["wire_mesh"]["calculated_bbox_width_mm"] - 267.258971) < 0.001
    assert abs(request["wire_mesh"]["calculated_bbox_depth_mm"] - 40.35446) < 0.001
    assert abs(request["wire_mesh"]["calculated_angle_pitch_deg"] - 11.43898) < 0.001
    assert abs(request["wire_mesh"]["calculated_arc_pitch_mm"] - 54.812) < 0.001
    assert request["wire_mesh"]["calculated_station_chord_pitches_mm"] == pytest.approx(
        [54.38, 55.1, 55.1, 55.1, 54.38]
    )
    endpoints = request["wire_mesh"]["transverse_arc_endpoints_xz_mm"]
    assert endpoints[0][1] < 0.0 and endpoints[1][1] < 0.0

    wrong_diameter = {**_wire_mesh_feature()["params"], "wire_mesh": {**_wire_mesh_feature()["params"]["wire_mesh"], "wire_diameter_mm": 5}}
    wrong_height = {**_wire_mesh_feature()["params"], "wire_mesh": {**_wire_mesh_feature()["params"]["wire_mesh"], "mesh_height_mm": 312}}
    wrong_layer = {**_wire_mesh_feature()["params"], "wire_mesh": {**_wire_mesh_feature()["params"]["wire_mesh"], "layering": "transverse_arc_wires_below"}}
    wrong_layer_reference = {**_wire_mesh_feature()["params"], "wire_mesh": {**_wire_mesh_feature()["params"]["wire_mesh"], "layer_reference": "global_positive_z"}}
    wrong_radial_order = {**_wire_mesh_feature()["params"], "wire_mesh": {**_wire_mesh_feature()["params"]["wire_mesh"], "layer_reference": "drawing_side_profile", "transverse_layer_radius_mm": 275, "longitudinal_layer_radius_mm": 271}}
    assert not WeldmentSkill.normalize_request(wrong_diameter, "model_3d")["success"]
    assert not WeldmentSkill.normalize_request(wrong_height, "model_3d")["success"]
    assert not WeldmentSkill.normalize_request(wrong_layer, "model_3d")["success"]
    assert not WeldmentSkill.normalize_request(wrong_layer_reference, "model_3d")["success"]
    assert not WeldmentSkill.normalize_request(wrong_radial_order, "model_3d")["success"]


def test_wire_mesh_flat_bar_frame_derives_mixed_profile_paths() -> None:
    params = _wire_mesh_feature()["params"]
    params = {**params, "wire_mesh": {**params["wire_mesh"], "flat_bar_frame": {
        "width_mm": 20,
        "thickness_mm": 3,
        "orientation": "edge_on",
        "support_side": "below_wire",
        "contact": "tangent",
        "curved_rail_count": 2,
        "curved_rail_radius_mm": 269,
        "straight_support_count": 6,
        "straight_support_length_mm": 310,
        "mounting_tabs": {"count": 4, "width_mm": 20, "thickness_mm": 3, "height_mm": 35},
    }}}
    request = WeldmentSkill.normalize_request(params, "model_3d")
    assert request["success"]
    frame = request["flat_bar_frame"]
    assert frame["profile_configuration"] == "FB20X3"
    assert frame["support_center_radius_mm"] == 253
    assert frame["wire_to_support_center_offset_mm"] == 16
    assert frame["curved_rail_count"] == 2
    assert frame["straight_support_count"] == 6
    assert frame["path_segment_count"] == 12
    assert frame["expected_body_count"] == 12
    assert len(frame["arc_entities_mm"]) == 2
    assert len(frame["groups"]) == 12
    assert all(not enabled for enabled in frame["group_corner_treatments"])


def test_segmented_flat_bar_basket_derives_wire_retaining_clips() -> None:
    params = _wire_mesh_feature()["params"]
    params = {**params, "wire_mesh": {**params["wire_mesh"], "flat_bar_frame": {
        "width_mm": 20,
        "thickness_mm": 3,
        "orientation": "edge_on",
        "support_side": "below_wire",
        "contact": "tangent",
        "construction_style": "segmented_basket",
        "curved_rail_count": 2,
        "curved_rail_radius_mm": 269,
        "rail_segment_count": 5,
        "straight_support_count": 6,
        "straight_support_length_mm": 310,
        "mounting_tabs": {"count": 4, "width_mm": 20, "thickness_mm": 3, "height_mm": 35},
        "wire_clips": {
            "style": "single_hook",
            "placement": "all_support_wire_intersections",
            "width_mm": 8,
            "thickness_mm": 2,
            "clearance_mm": 0.3,
        },
    }}}
    request = WeldmentSkill.normalize_request(params, "model_3d")
    assert request["success"]
    frame = request["flat_bar_frame"]
    clips = request["wire_clips"]
    assert frame["construction_style"] == "segmented_basket"
    assert frame["rail_segment_count"] == 5
    assert frame["rail_member_count"] == 10
    assert frame["expected_arc_entity_count"] == 0
    assert frame["path_segment_count"] == 20
    assert frame["expected_body_count"] == 20
    assert len(frame["groups"]) == 12
    assert frame["group_corner_treatments"][:2] == [True, True]
    assert all(not enabled for enabled in frame["group_corner_treatments"][2:])
    assert clips["profile_configuration"] == "CLIP8X2"
    assert clips["clip_count"] == 42
    assert clips["path_segment_count"] == 84
    assert len(clips["groups"]) == 42
    assert abs(clips["calculated_hook_span_mm"] - 6.6) < 0.001
    assert abs(clips["calculated_leg_height_mm"] - 11.3) < 0.001


def test_segmented_flat_bar_basket_derives_shallow_wire_seat_notches() -> None:
    params = _wire_mesh_feature()["params"]
    params = {**params, "wire_mesh": {**params["wire_mesh"], "flat_bar_frame": {
        "width_mm": 20,
        "thickness_mm": 3,
        "orientation": "edge_on",
        "support_side": "below_wire",
        "contact": "tangent",
        "construction_style": "segmented_basket",
        "curved_rail_count": 2,
        "curved_rail_radius_mm": 269,
        "rail_segment_count": 5,
        "straight_support_count": 6,
        "straight_support_length_mm": 310,
        "replace_longitudinal_wires": True,
        "wire_seat_notches": {
            "style": "shallow_rectangular_edge_notch",
            "placement": "all_support_transverse_intersections",
            "width_mm": 4.6,
            "depth_mm": 2.0,
        },
    }}}
    request = WeldmentSkill.normalize_request(params, "model_3d")
    assert request["success"]
    frame = request["flat_bar_frame"]
    notches = request["wire_seat_notches"]
    assert request["wire_mesh"]["replace_longitudinal_wires"] is True
    assert request["wire_mesh"]["longitudinal_wire_created_count"] == 0
    assert len(request["path_segments_mm"]) == 7
    assert len(request["groups"]) == 7
    assert frame["support_center_radius_mm"] == 259
    assert frame["path_segment_count"] == 10
    assert frame["expected_body_count"] == 10
    assert frame["notched_support_body_count"] == 6
    assert len(frame["groups"]) == 2
    assert frame["group_corner_treatments"] == [True, True]
    assert notches["support_count"] == 6
    assert notches["notch_count_per_support"] == 7
    assert notches["notch_count"] == 42
    assert notches["width_mm"] == 4.6
    assert notches["depth_mm"] == 2.0
    assert request["wire_clips"] is None


def test_dwg_flat_bar_one_each_prototype_omits_all_wire_bodies() -> None:
    params = _wire_mesh_feature()["params"]
    params = {**params, "wire_mesh": {**params["wire_mesh"], "flat_bar_frame": {
        "width_mm": 25,
        "thickness_mm": 4,
        "orientation": "edge_on",
        "support_side": "below_wire",
        "contact": "seated",
        "curved_rail_count": 4,
        "straight_support_count": 5,
        "straight_support_length_mm": 338.6,
        "curved_rail_radius_mm": 265,
        "curved_rail_overall_width_mm": 281.31,
        "curved_rail_pitch_chain_mm": [94, 140, 94],
        "prototype_mode": "one_each",
        "wire_seat_notches": {
            "width_mm": 4.2,
            "straight_depth_mm": 12,
            "curved_depth_mm": 12,
        },
        "flat_bar_joint_notches": {
            "width_mm": 4,
            "straight_depth_mm": 13,
            "curved_depth_mm": 13,
        },
    }}}

    request = WeldmentSkill.normalize_request(params, "model_3d")

    assert request["success"]
    assert request["geometry_kind"] == "flat_bar_prototype"
    assert request["path_segments_mm"] == []
    assert request["groups"] == []
    frame = request["flat_bar_frame"]
    notches = request["wire_seat_notches"]
    joints = frame["flat_bar_joint_notches"]
    assert frame["prototype_mode"] == "one_each"
    assert frame["expected_imported_body_count"] == 2
    assert frame["notched_support_body_count"] == 1
    assert frame["notched_curved_rail_body_count"] == 1
    assert frame["straight_wire_slot_center_offsets_from_top_mm"] == [
        16.25, 61.25, 115.25, 169.25, 223.25, 277.25, 322.25,
    ]
    assert len(frame["curved_wire_slot_x_offsets_mm"]) == 6
    assert len(frame["curved_joint_slot_angles_rad"]) == 5
    assert notches["support_count"] == 1
    assert notches["curved_rail_count"] == 1
    assert notches["notch_count"] == 13
    assert notches["width_mm"] == 4.2
    assert notches["straight_depth_mm"] == 12
    assert notches["curved_depth_mm"] == 12
    assert joints["straight_notch_count"] == 4
    assert joints["curved_notch_count"] == 5
    assert joints["notch_count"] == 9


def test_dwg_flat_bar_joined_pair_uses_only_two_notched_bodies() -> None:
    params = _wire_mesh_feature()["params"]
    params = {**params, "wire_mesh": {**params["wire_mesh"], "flat_bar_frame": {
        "width_mm": 25,
        "thickness_mm": 4,
        "orientation": "edge_on",
        "support_side": "below_wire",
        "contact": "seated",
        "curved_rail_count": 4,
        "straight_support_count": 5,
        "straight_support_length_mm": 338.6,
        "curved_rail_radius_mm": 265,
        "curved_rail_overall_width_mm": 281.31,
        "curved_rail_pitch_chain_mm": [94, 140, 94],
        "prototype_mode": "joined_pair",
        "wire_seat_notches": {
            "width_mm": 4.2,
            "straight_depth_mm": 12,
            "curved_depth_mm": 12,
        },
        "flat_bar_joint_notches": {
            "width_mm": 4,
            "straight_depth_mm": 13,
            "curved_depth_mm": 13,
        },
    }}}

    request = WeldmentSkill.normalize_request(params, "model_3d")

    assert request["success"]
    assert request["geometry_kind"] == "flat_bar_prototype"
    assert request["path_segments_mm"] == []
    assert request["groups"] == []
    frame = request["flat_bar_frame"]
    assert frame["prototype_mode"] == "joined_pair"
    assert frame["prototype_only"] is True
    assert frame["expected_imported_body_count"] == 2
    assert frame["created_straight_support_count"] == 1
    assert frame["created_curved_rail_count"] == 1
    assert request["wire_seat_notches"]["support_count"] == 1
    assert request["wire_seat_notches"]["curved_rail_count"] == 1


def test_weldment_gate_reports_geometry_reason_instead_of_missing_executor() -> None:
    feature = _wire_mesh_feature()
    feature["params"]["wire_mesh"]["overall_depth_mm"] = 276.4
    source = {
        "source_brief": "Create this wire mesh and only save SLDPRT.",
        "task_type": "model_3d",
        "features": [feature],
        "outputs": ["SLDPRT"],
    }
    design = apply_stage_plan(source, infer_stage_plan(source["source_brief"], "model_3d"))
    blocked = design["execution_policy"]["unexecutable_required_features"]
    assert len(blocked) == 1
    assert "overall depth is inconsistent" in blocked[0]["reason"]
    assert "No compatible production executor" not in blocked[0]["reason"]


def test_wire_mesh_satisfies_alternative_weldment_command_parameters() -> None:
    knowledge = EngineeringKnowledgeBase()
    entry = knowledge.commands["weldment"]
    wire_mesh = _wire_mesh_feature()["params"]
    assert knowledge._missing_parameters(entry, wire_mesh) == []
    assert knowledge._missing_parameters(entry, {"profile_type": "pipe"}) == [
        "profile_type/profile_configuration/path_segments_mm/groups or wire_mesh"
    ]


def test_lofted_bend_schema_and_correspondence_guards() -> None:
    params = _lofted_bend_feature()["params"]
    request = SheetMetalSkill.normalize_request(params, "model_3d")
    assert request["success"]
    assert request["construction"] == "lofted_bend"
    assert request["lofted_bend"]["plane_offset_mm"] == 80.0
    assert request["lofted_bend"]["bends_per_transition"] == 4

    mismatched = {**params, "lofted_bend": {**params["lofted_bend"]}}
    mismatched["lofted_bend"]["profile_2_points_mm"] = [[-30, 0], [0, 30], [30, 0]]
    assert not SheetMetalSkill.normalize_request(mismatched, "model_3d")["success"]
    closed = {**params, "lofted_bend": {**params["lofted_bend"]}}
    closed["lofted_bend"]["profile_1_points_mm"] = [[-40, 0], [0, 20], [40, 0], [-40, 0]]
    assert not SheetMetalSkill.normalize_request(closed, "model_3d")["success"]
    twisted = {**params, "lofted_bend": {**params["lofted_bend"]}}
    twisted["lofted_bend"]["profile_2_points_mm"] = [[30, 0], [15, 30], [-15, 30], [-30, 0]]
    assert not SheetMetalSkill.normalize_request(twisted, "model_3d")["success"]
    mixed = {**params, "edge_flanges": [{"edge": "width_positive", "height_mm": 20}]}
    assert not SheetMetalSkill.normalize_request(mixed, "model_3d")["success"]


def test_helical_lofted_bend_schema_and_conflict_guards() -> None:
    params = _helical_lofted_bend_feature()["params"]
    request = SheetMetalSkill.normalize_request(params, "model_3d")

    assert request["success"]
    assert request["construction"] == "lofted_bend"
    assert request["base_plane"] == "Top Plane"
    assert request["lofted_bend"] == {
        "profile_mode": "coaxial_helices",
        "axis": "y",
        "base_plane": "Top Plane",
        "center_uv_mm": [0.0, 0.0],
        "inner_radius_mm": 28.5,
        "outer_radius_mm": 61.5,
        "height_mm": 70.0,
        "pitch_mm": 70.0,
        "revolutions": 1.0,
        "start_angle_deg": 0.0,
        "clockwise": False,
        "reverse_direction": False,
        "taper": False,
        "thickness_direction": "outside",
        "formed": True,
        "refer_to_endpoint": True,
        "facet_option": "chord_tolerance",
        "chord_tolerance_mm": 0.5,
        "number_of_bend_lines": 3,
    }

    def invalid(**changes: object) -> dict:
        loft = {**params["lofted_bend"], **changes}
        return SheetMetalSkill.normalize_request({**params, "lofted_bend": loft}, "model_3d")

    assert not invalid(base_plane="front")["success"]
    assert not invalid(outer_radius_mm=27)["success"]
    assert not invalid(height_mm=71)["success"]
    assert not invalid(center_uv_mm=None)["success"]
    assert not invalid(formed=False)["success"]
    assert not invalid(clockwise="false")["success"]
    assert not invalid(facet_option="bends_per_transition")["success"]
    assert not invalid(chord_tolerance_mm=0)["success"]
    assert not invalid(number_of_bend_lines=1)["success"]


def test_formed_helical_geometry_checks_ignore_nonoperative_bend_controls() -> None:
    checks = SheetMetalSkill._formed_helical_geometry_checks(
        actual_profile_count=2,
        actual_thickness_mm=3.0,
        requested_thickness_mm=3.0,
        actual_direction=False,
        thickness_direction="outside",
        actual_formed=True,
        helix_checks={
            "inner": {
                "pitch": True,
                "height": True,
                "revolutions": True,
                "clockwise": True,
                "reverse_direction": True,
            },
            "outer": {
                "pitch": True,
                "height": True,
                "revolutions": True,
                "clockwise": True,
                "reverse_direction": True,
            },
        },
    )

    assert all(checks.values())
    assert "bend_line_count" not in checks
    assert "chord_tolerance" not in checks
    assert "refer_to_endpoint" not in checks


def test_sheet_metal_body_volume_falls_back_to_mass_properties() -> None:
    class MassPropertiesOnlyBody:
        def GetMassProperties(self, density: float) -> tuple[float, ...]:
            assert density == 1.0
            return (0.0, 0.0, 0.0, 2.88716e-5)

    assert SheetMetalSkill._body_volume_m3(MassPropertiesOnlyBody()) == pytest.approx(2.88716e-5)


def test_freeform_surface_schema_requires_closed_contiguous_boundary() -> None:
    request = FreeformSurfaceSkill.normalize_request(_surface_feature()["params"], "model_3d")
    assert request["success"]
    assert len(request["boundary_curves_mm"]) == 4
    open_boundary = dict(_surface_feature()["params"])
    open_boundary["boundary_curves_mm"] = [list(curve) for curve in open_boundary["boundary_curves_mm"]]
    open_boundary["boundary_curves_mm"][1] = [[55, -30, 0], [60, 0, 8], [50, 30, 0]]
    assert not FreeformSurfaceSkill.normalize_request(open_boundary, "model_3d")["success"]


def test_new_model_features_route_without_default_base_plate() -> None:
    for feature in (_sheet_feature(), _lofted_bend_feature(), _weldment_feature(), _surface_feature()):
        source = _design(feature)
        design = apply_stage_plan(source, infer_stage_plan(source["source_brief"], "model_3d"))
        expected = [feature["type"], "save_sldprt"]
        assert not design["needs_confirmation"]
        assert design["execution_policy"]["allowed_skills"] == expected
        assert [step.skill_key for step in SkillPlanner().plan(design)] == expected
        assert [step["skill_key"] for step in SkillRouterAgent().route(design)["skill_pipeline"]] == expected
        assert "base_plate" not in expected


def test_registry_contracts_forbid_templates_and_exports() -> None:
    contracts = CADAgentSkillManager.production_skill_contracts()
    for key in ("sheet_metal", "weldment", "freeform_surface"):
        contract = contracts[key]
        assert contract["creates_new_doc"] is True
        assert contract["modifies_active_doc"] is False
        assert contract["exports_files"] is False
        assert contract["uses_test_template"] is False
    assert {"open_hem", "native_jog", "lofted_bend", "formed_helical_lofted_bend", "forming_tool_dimple", "flat_pattern_dxf"}.issubset(
        contracts["sheet_metal"]["capabilities"]
    )
    assert contracts["sheet_metal"]["conditional_output_types"] == ["DXF"]
    assert {"solid_round_d4", "curved_wire_mesh"}.issubset(contracts["weldment"]["capabilities"])


def test_final_validator_requires_exact_normalized_request_and_geometry_evidence() -> None:
    normalizers = {
        "sheet_metal": SheetMetalSkill.normalize_request,
        "weldment": WeldmentSkill.normalize_request,
        "freeform_surface": FreeformSurfaceSkill.normalize_request,
    }
    for feature in (_sheet_feature(), _lofted_bend_feature(), _weldment_feature(), _surface_feature()):
        feature_type = feature["type"]
        request = normalizers[feature_type](feature["params"], "model_3d")
        design = _design(feature)
        record = PipelineStepRecord(skill_key=feature_type, action="apply", required=True, reason="test")
        record.status = "success"
        record.data = {
            "feature_created": True,
            "operations": [{
                "success": True,
                "type": feature_type,
                "request": request,
                "feature_name": "NativeFeature1",
                "geometry_validation": {"success": True},
            }],
        }
        assert PipelineExecutor._new_model_feature_operation_matches(design, record, feature_type)
        record.data["operations"][0]["geometry_validation"] = {"success": False}
        assert not PipelineExecutor._new_model_feature_operation_matches(design, record, feature_type)


def test_explicit_feature_family_mismatch_fails_closed() -> None:
    wrong = [{"name": "WrongBase", "type": "base_plate", "params": {}, "required": True}]
    for brief, expected in (
        ("Create a sheet metal U-channel.", "sheet_metal"),
        ("Create an open hem on a sheet-metal edge.", "sheet_metal"),
        ("Create a jog in a sheet-metal plate.", "sheet_metal"),
        ("Create a welded frame weldment.", "weldment"),
        ("Create a native freeform surface.", "freeform_surface"),
    ):
        risks = DesignPlanner._feature_family_alignment_risks(brief, wrong)
        assert any(item.get("expected_feature") == expected for item in risks)


def test_opposite_sheet_metal_flanges_cannot_be_silently_dropped() -> None:
    feature = _sheet_feature()
    feature["params"]["edge_flanges"] = feature["params"]["edge_flanges"][:1]
    risks = DesignPlanner._feature_family_alignment_risks(
        "Create edge flanges on both sides of the sheet metal base.",
        [feature],
    )
    assert any(item.get("type") == "sheet_metal_edge_scope_mismatch" for item in risks)
