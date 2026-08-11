from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.cad_ir import CADIRCompiler
from cad_agent.profile_extrude_skill import ProfileExtrudeSkill
from cad_agent.registry import CADAgentSkillManager
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _rectangle_params(**overrides: object) -> dict:
    params = {
        "body_operation": "base",
        "mode": "new_model",
        "sketch_plane": "front",
        "depth_mm": 6,
        "profiles": [
            {
                "role": "outer",
                "points_mm": [[-20, -10], [20, -10], [20, 10], [-20, 10], [-20, -10]],
            }
        ],
    }
    params.update(overrides)
    return params


def _design(params: dict | None = None, feature_type: str = "profile_extrude") -> dict:
    return {
        "source_brief": "Create one explicit closed-profile extrusion and save only SLDPRT.",
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "material": "65Mn"},
        "features": [
            {
                "name": "ProfileBlank",
                "type": feature_type,
                "required": True,
                "params": params or _rectangle_params(),
            }
        ],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def _retaining_ring_params() -> dict:
    return {
        "body_operation": "base",
        "mode": "new_model",
        "sketch_plane": "front",
        "depth_mm": 1.2,
        "profiles": [
            {
                "role": "outer",
                "segments": [
                    {"type": "arc", "start_mm": [1.5, -14.799120376], "end_mm": [-1.5, -14.799120376], "center_mm": [0, -0.93], "direction": 1},
                    {"type": "line", "start_mm": [-1.5, -14.799120376], "end_mm": [-1.5, -19.870696925]},
                    {"type": "arc", "start_mm": [-1.5, -19.870696925], "end_mm": [-9.5, -17.384482672], "center_mm": [0, -0.93], "direction": -1},
                    {"type": "line", "start_mm": [-9.5, -17.384482672], "end_mm": [-9.5, -13.795379661]},
                    {"type": "arc", "start_mm": [-9.5, -13.795379661], "end_mm": [9.5, -13.795379661], "center_mm": [0, 0], "direction": -1},
                    {"type": "line", "start_mm": [9.5, -13.795379661], "end_mm": [9.5, -17.384482672]},
                    {"type": "arc", "start_mm": [9.5, -17.384482672], "end_mm": [1.5, -19.870696925], "center_mm": [0, -0.93], "direction": -1},
                    {"type": "line", "start_mm": [1.5, -19.870696925], "end_mm": [1.5, -14.799120376]},
                ],
            },
            {"role": "inner", "segments": [{"type": "circle", "center_mm": [-5.5, -16.486349186], "radius_mm": 1}]},
            {"role": "inner", "segments": [{"type": "circle", "center_mm": [5.5, -16.486349186], "radius_mm": 1}]},
        ],
    }


def test_rectangle_profile_normalizes_and_measures_area() -> None:
    request = ProfileExtrudeSkill.normalize_request(_rectangle_params(), "model_3d")
    assert request["success"]
    assert request["body_operation"] == "base"
    assert request["sketch_plane"] == "Front Plane"
    assert request["profile_metrics"]["net_area_mm2"] == 800.0
    assert request["profile_metrics"]["bbox_mm"] == {
        "xmin": -20.0,
        "xmax": 20.0,
        "ymin": -10.0,
        "ymax": 10.0,
    }


def test_nested_profile_units_are_converted_to_millimetres() -> None:
    params = {
        "unit": "cm",
        "body_operation": "base",
        "mode": "new_model",
        "sketch_plane": "front",
        "depth": 0.6,
        "profiles": [
            {
                "role": "outer",
                "closed": True,
                "points": [[-2, -1], [2, -1], [2, 1], [-2, 1]],
            }
        ],
    }
    compiled = CADIRCompiler().compile(_design(params, "closed_profile_extrude"))
    assert compiled.success, compiled.errors
    feature = compiled.design["features"][0]
    assert feature["type"] == "profile_extrude"
    assert feature["params"]["depth_mm"] == 6.0
    assert feature["params"]["profile_metrics"]["bbox_mm"]["xmax"] == 20.0
    assert compiled.ir["features"][0]["operation"] == "profile_extrude"


def test_real_retaining_ring_line_arc_circle_profile_is_valid() -> None:
    request = ProfileExtrudeSkill.normalize_request(_retaining_ring_params(), "model_3d")
    assert request["success"], request
    assert request["profile_metrics"]["loop_count"] == 3
    assert request["profile_metrics"]["inner_loop_count"] == 2
    assert 311.0 < request["profile_metrics"]["net_area_mm2"] < 312.0


def test_open_point_profile_is_rejected_without_explicit_close() -> None:
    params = _rectangle_params(profiles=[{"role": "outer", "points_mm": [[0, 0], [20, 0], [20, 10], [0, 10]]}])
    result = ProfileExtrudeSkill.normalize_request(params, "model_3d")
    assert not result["success"]
    assert any(item["code"] == "profile_not_closed" for item in result["errors"])


def test_segment_gap_and_arc_radius_mismatch_are_rejected() -> None:
    gap = _rectangle_params(profiles=[{
        "role": "outer",
        "segments": [
            {"type": "line", "start_mm": [0, 0], "end_mm": [10, 0]},
            {"type": "line", "start_mm": [11, 0], "end_mm": [0, 10]},
            {"type": "line", "start_mm": [0, 10], "end_mm": [0, 0]},
        ],
    }])
    mismatch = _rectangle_params(profiles=[{
        "role": "outer",
        "segments": [
            {"type": "arc", "center_mm": [0, 0], "start_mm": [10, 0], "end_mm": [0, 12], "direction": "ccw"},
        ],
    }])
    assert any(item["code"] == "profile_segment_gap" for item in ProfileExtrudeSkill.normalize_request(gap)["errors"])
    assert any(item["code"] == "arc_radius_mismatch" for item in ProfileExtrudeSkill.normalize_request(mismatch)["errors"])


def test_self_intersection_is_rejected() -> None:
    params = _rectangle_params(profiles=[{
        "role": "outer",
        "points_mm": [[0, 0], [10, 10], [0, 10], [10, 0], [0, 0]],
    }])
    result = ProfileExtrudeSkill.normalize_request(params)
    assert not result["success"]
    assert any(item["code"] == "self_intersecting_profile" for item in result["errors"])


def test_inner_loop_must_be_inside_outer_loop() -> None:
    params = _rectangle_params(profiles=[
        {"role": "outer", "points_mm": [[0, 0], [20, 0], [20, 20], [0, 20], [0, 0]]},
        {"role": "inner", "segments": [{"type": "circle", "center_mm": [25, 10], "radius_mm": 2}]},
    ])
    result = ProfileExtrudeSkill.normalize_request(params)
    assert not result["success"]
    assert any(item["code"] == "inner_profile_outside_outer" for item in result["errors"])


def test_multiple_disconnected_outer_loops_are_supported() -> None:
    params = _rectangle_params(profiles=[
        {"role": "outer", "points_mm": [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]},
        {"role": "outer", "points_mm": [[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]},
    ])
    result = ProfileExtrudeSkill.normalize_request(params)
    assert result["success"], result
    assert result["profile_metrics"]["outer_loop_count"] == 2
    assert result["profile_metrics"]["inner_loop_count"] == 0
    assert result["profile_metrics"]["net_area_mm2"] == 200.0
    assert result["profile_metrics"]["bbox_mm"] == {
        "xmin": 0.0,
        "xmax": 30.0,
        "ymin": 0.0,
        "ymax": 10.0,
    }


def test_disconnected_auto_loops_are_classified_as_outer_profiles() -> None:
    params = _rectangle_params(profiles=[
        {"points_mm": [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]},
        {"points_mm": [[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]},
    ])
    result = ProfileExtrudeSkill.normalize_request(params)
    assert result["success"], result
    assert result["profile_metrics"]["outer_loop_count"] == 2
    assert [loop["role"] for loop in result["profiles"]] == ["outer", "outer"]


def test_operation_mode_and_end_condition_conflicts_are_blocked() -> None:
    active_base = ProfileExtrudeSkill.normalize_request(_rectangle_params(mode="active_model"))
    through_base = ProfileExtrudeSkill.normalize_request(_rectangle_params(end_condition="through_all"))
    cut = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "cut",
        "mode": "active_model",
        "end_condition": "through_all",
        "depth_mm": None,
    })
    assert not active_base["success"]
    assert not through_base["success"]
    assert cut["success"]
    assert cut["depth_mm"] == 0.0


def test_cut_supports_explicit_through_all_both_directions() -> None:
    cut = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "cut",
        "mode": "active_model",
        "end_condition": "through_all_both_directions",
        "depth_mm": None,
    })
    assert cut["success"], cut
    assert cut["end_condition"] == "through_all_both"
    assert cut["depth_mm"] == 0.0


def test_cut_supports_explicit_mid_plane_depth() -> None:
    cut = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "cut",
        "mode": "active_model",
        "end_condition": "mid_plane",
        "depth_mm": 40,
    })
    assert cut["success"], cut
    assert cut["end_condition"] == "mid_plane"
    assert cut["depth_mm"] == 40.0


def test_mid_plane_cut_uses_solidworks_symmetric_end_condition(monkeypatch) -> None:
    calls: list[tuple] = []

    class FeatureManager:
        @staticmethod
        def FeatureCut4(*args):
            calls.append(args)
            return object()

    fake_sw_part = type("FakeSWPart", (), {"_ensure_sketch_selected": staticmethod(lambda *_: True)})
    monkeypatch.setitem(sys.modules, "sw_part", fake_sw_part)
    model = type("Model", (), {"FeatureManager": FeatureManager()})()

    result = ProfileExtrudeSkill._extrude_cut_midplane(
        model,
        "Sketch1",
        total_depth_m=0.04,
        flip=False,
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][0] is True
    assert calls[0][3] == 6
    assert calls[0][5] == 0.04


def test_through_all_both_uses_solidworks_combined_end_condition(monkeypatch) -> None:
    calls: list[tuple] = []

    class FeatureManager:
        @staticmethod
        def FeatureCut4(*args):
            calls.append(args)
            return object()

    fake_sw_part = type("FakeSWPart", (), {"_ensure_sketch_selected": staticmethod(lambda *_: True)})
    monkeypatch.setitem(sys.modules, "sw_part", fake_sw_part)
    model = type("Model", (), {"FeatureManager": FeatureManager()})()

    result = ProfileExtrudeSkill._extrude_cut_through_all_both(
        model,
        "Sketch1",
        direction=True,
        flip=False,
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][0] is False
    assert calls[0][3:5] == (1, 1)


def test_reversed_cut_sets_solidworks_dir_instead_of_sd(monkeypatch) -> None:
    calls: list[tuple] = []

    class FeatureManager:
        @staticmethod
        def FeatureCut4(*args):
            calls.append(args)
            return object()

    fake_sw_part = type("FakeSWPart", (), {"_ensure_sketch_selected": staticmethod(lambda *_: True)})
    monkeypatch.setitem(sys.modules, "sw_part", fake_sw_part)
    model = type("Model", (), {"FeatureManager": FeatureManager()})()

    result = ProfileExtrudeSkill._extrude_cut_reversed(
        model,
        "Sketch1",
        depth_m=0.00259,
        flip=False,
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][0] is True
    assert calls[0][2] is True
    assert calls[0][3] == 0
    assert calls[0][5] == 0.00259


def test_blind_boss_start_offset_is_normalized_to_millimetres() -> None:
    params = _rectangle_params(
        unit="cm",
        body_operation="boss",
        mode="active_model",
        depth_mm=None,
        depth=0.15,
        start_offset=1.475,
        flip_start_offset=True,
    )
    result = ProfileExtrudeSkill.normalize_request(params, "model_3d")
    assert result["success"], result
    assert result["depth_mm"] == 1.5
    assert result["start_offset_mm"] == 14.75
    assert result["flip_start_offset"] is True


def test_through_next_boss_supports_explicit_start_offset() -> None:
    result = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "boss",
        "mode": "active_model",
        "end_condition": "through_next",
        "depth_mm": None,
        "start_offset_mm": 222,
        "reverse_direction": True,
    })
    assert result["success"], result
    assert result["end_condition"] == "through_next"
    assert result["depth_mm"] == 0.0
    assert result["start_offset_mm"] == 222.0
    assert result["reverse_direction"] is True


def test_through_next_boss_reaches_native_end_and_start_conditions(monkeypatch) -> None:
    calls: list[tuple] = []

    class FeatureManager:
        @staticmethod
        def FeatureExtrusion3(*args):
            calls.append(args)
            return object()

    fake_sw_part = type("FakeSWPart", (), {"_ensure_sketch_selected": staticmethod(lambda *_: True)})
    monkeypatch.setitem(sys.modules, "sw_part", fake_sw_part)
    model = type("Model", (), {"FeatureManager": FeatureManager()})()

    result = ProfileExtrudeSkill._extrude_boss_with_start_offset(
        model,
        "Sketch1",
        depth_m=0.0,
        direction=False,
        merge=True,
        start_offset_m=0.222,
        flip_start_offset=False,
        end_condition=2,
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][2] is False
    assert calls[0][3] == 2
    assert calls[0][5] == 0.01
    assert calls[0][17] is True
    assert calls[0][20:] == (3, 0.222, False)


def test_blind_cut_start_offset_is_normalized_to_millimetres() -> None:
    result = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "cut",
        "mode": "active_model",
        "depth_mm": 4,
        "start_offset_mm": 198,
        "flip_start_offset": False,
    })
    assert result["success"], result
    assert result["depth_mm"] == 4.0
    assert result["start_offset_mm"] == 198.0


def test_start_offset_is_rejected_for_non_blind_operations() -> None:
    cut = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "cut",
        "mode": "active_model",
        "end_condition": "through_all",
        "start_offset_mm": 2,
    })
    assert not cut["success"]
    assert any(item["code"] == "unsupported_start_offset_combination" for item in cut["errors"])


def test_blind_cut_start_offset_reaches_solidworks_start_condition(monkeypatch) -> None:
    calls: list[tuple] = []

    class FeatureManager:
        @staticmethod
        def FeatureCut4(*args):
            calls.append(args)
            return object()

    fake_sw_part = type("FakeSWPart", (), {"_ensure_sketch_selected": staticmethod(lambda *_: True)})
    monkeypatch.setitem(sys.modules, "sw_part", fake_sw_part)
    model = type("Model", (), {"FeatureManager": FeatureManager()})()

    result = ProfileExtrudeSkill._extrude_cut_with_start_offset(
        model,
        "Sketch1",
        depth_m=0.004,
        reverse_direction=True,
        flip=False,
        start_offset_m=0.198,
        flip_start_offset=False,
    )

    assert result is not None
    assert len(calls) == 1
    assert calls[0][0] is True
    assert calls[0][2] is True
    assert calls[0][3] == 0
    assert calls[0][5] == 0.004
    assert calls[0][23:] == (3, 0.198, False, False)


def test_flip_side_to_cut_is_explicit_and_cut_only() -> None:
    cut = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "cut",
        "mode": "active_model",
        "end_condition": "through_all",
        "flip_side_to_cut": True,
    })
    boss = ProfileExtrudeSkill.normalize_request({
        **_rectangle_params(),
        "body_operation": "boss",
        "mode": "active_model",
        "flip_side_to_cut": True,
    })
    assert cut["success"], cut
    assert cut["flip_side_to_cut"] is True
    assert not boss["success"]
    assert any(item["code"] == "flip_side_to_cut_operation_conflict" for item in boss["errors"])


def test_merged_boss_allows_body_count_reduction_when_solids_are_bridged() -> None:
    result = ProfileExtrudeSkill._verify_geometry(
        {"body_operation": "boss", "merge_result": True},
        {"volume_m3": 0.0009, "solid_body_count": 2},
        {
            "success": True,
            "volume_m3": 0.00091,
            "solid_body_count": 1,
            "rebuild_error_count": 0,
            "feature_error_count": 0,
        },
    )

    assert result["success"]
    assert result["volume_delta_m3"] > 0.0


def test_merged_boss_rejects_new_disconnected_solid_bodies() -> None:
    result = ProfileExtrudeSkill._verify_geometry(
        {"body_operation": "boss", "merge_result": True},
        {"volume_m3": 0.0009, "solid_body_count": 1},
        {
            "success": True,
            "volume_m3": 0.00091,
            "solid_body_count": 3,
            "rebuild_error_count": 0,
            "feature_error_count": 0,
        },
    )

    assert not result["success"]
    assert result["message"] == "Merged profile boss produced additional solid bodies."
    assert result["body_count_before"] == 1
    assert result["body_count_after"] == 3


def test_bare_extrude_remains_ambiguous_and_cannot_bypass_cad_ir() -> None:
    result = CADIRCompiler().compile(_design(_rectangle_params(), "extrude"))
    assert not result.success
    assert any(item["code"] == "ambiguous_feature_type" for item in result.errors)


def test_stage_and_both_routers_select_only_profile_extrude_and_save() -> None:
    source = _design()
    compiled = CADIRCompiler().compile(source)
    assert compiled.success, compiled.errors
    staged = apply_stage_plan(compiled.design, infer_stage_plan(source["source_brief"], "model_3d"))
    assert not staged["needs_confirmation"]
    assert staged["execution_policy"]["allowed_skills"] == ["profile_extrude", "save_sldprt"]
    assert [step.skill_key for step in SkillPlanner().plan(staged)] == ["profile_extrude", "save_sldprt"]
    assert [item["skill_key"] for item in SkillRouterAgent().route(staged)["skill_pipeline"]] == [
        "profile_extrude",
        "save_sldprt",
    ]


def test_registry_contract_declares_no_template_or_export_side_effect() -> None:
    contract = CADAgentSkillManager.production_skill_contracts()["profile_extrude"]
    assert "closed_profile_extrude" in contract["capabilities"]
    assert contract["uses_test_template"] is False
    assert contract["exports_files"] is False
    assert contract["output_types"] == []


def test_skill_planner_uses_validated_cad_ir_operation_order() -> None:
    design = {
        "execution_policy": {
            "allowed_skills": ["profile_extrude", "revolve", "chamfer", "save_sldprt"],
        },
        "cad_ir": {
            "features": [
                {"id": "hex_base", "operation": "profile_extrude", "dependencies": []},
                {
                    "id": "rear_relief",
                    "operation": "revolve",
                    "dependencies": [{"kind": "feature", "feature_id": "hex_base"}],
                },
                {
                    "id": "outer_chamfer",
                    "operation": "chamfer",
                    "dependencies": [{"kind": "feature", "feature_id": "rear_relief"}],
                },
            ],
        },
    }
    assert [step.skill_key for step in SkillPlanner().plan(design)] == [
        "profile_extrude",
        "revolve",
        "chamfer",
        "save_sldprt",
    ]

    router_source = _design()
    router_source["features"].extend([
        {
            "name": "RearRelief",
            "type": "revolve",
            "required": True,
            "depends_on": ["ProfileBlank"],
            "params": {
                "operation": "cut",
                "mode": "active_model",
                "sketch_plane": "right",
                "axis": "vertical",
                "angle_deg": 360,
                "profile_points": [[0, 1], [0, 2], [-1, 2], [0, 1]],
            },
        },
        {
            "name": "OuterChamfer",
            "type": "chamfer",
            "required": True,
            "depends_on": ["RearRelief"],
            "params": {"size": 1, "targets": "outer_edges"},
        },
    ])
    compiled = CADIRCompiler().compile(router_source)
    assert compiled.success, compiled.errors
    staged = apply_stage_plan(compiled.design, infer_stage_plan(router_source["source_brief"], "model_3d"))
    assert [item["skill_key"] for item in SkillRouterAgent().route(staged)["skill_pipeline"]] == [
        "profile_extrude",
        "revolve",
        "chamfer",
        "save_sldprt",
    ]


def test_profile_extrude_operation_contract_is_complete() -> None:
    contract = CADIRCompiler.operation_contracts()["profile_extrude"]
    assert set(contract["required_parameters"]) == {"body_operation", "sketch_plane", "profiles"}
    assert "closed_profile_extrude" in contract["aliases"]
    assert "profile_segment_chains_must_be_contiguous_and_closed" in contract["conflict_rules"]
    assert "start_offset_mm" in contract["optional_parameters"]
    assert "flip_side_to_cut" in contract["optional_parameters"]
    assert "nonzero_start_offset_requires_blind_or_through_next_boss_or_blind_cut" in contract["conflict_rules"]
    assert "flip_side_to_cut_requires_cut_operation" in contract["conflict_rules"]
