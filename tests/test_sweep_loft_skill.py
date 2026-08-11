from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan
from cad_agent.sweep_loft_skill import SweepLoftSkill


def _design(feature: dict) -> dict:
    return {
        "source_brief": "Create only the requested 3D part and save SLDPRT.",
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "material": "6061 aluminum"},
        "features": [feature],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def _sweep_feature() -> dict:
    return {
        "name": "BentRod",
        "type": "sweep",
        "required": True,
        "params": {
            "operation": "base",
            "mode": "new_model",
            "profile_type": "circle",
            "diameter_mm": 12,
            "path_plane": "front",
            "path_points_mm": [[0, 0], [125, 0]],
        },
    }


def _loft_feature() -> dict:
    return {
        "name": "RoundToRectTransition",
        "type": "loft",
        "required": True,
        "params": {
            "operation": "base",
            "mode": "new_model",
            "base_plane": "front",
            "sections": [
                {"offset_mm": 0, "shape": "circle", "diameter_mm": 40},
                {"offset_mm": 80, "shape": "rectangle", "length_mm": 60, "width_mm": 40},
            ],
        },
    }


def test_sweep_schema_normalizes_explicit_circular_profile_and_path() -> None:
    request = SweepLoftSkill.normalize_request("sweep", _sweep_feature()["params"], "model_3d")
    assert request["success"]
    assert request["mode"] == "new_model"
    assert request["diameter_mm"] == 12.0
    assert request["path_points_mm"] == [[0.0, 0.0], [125.0, 0.0]]
    assert request["path_length_mm"] == 125.0


def test_sweep_guard_rejects_implicit_or_unsupported_geometry() -> None:
    no_path = SweepLoftSkill.normalize_request("sweep", {"diameter_mm": 10}, "model_3d")
    non_circular = SweepLoftSkill.normalize_request(
        "sweep",
        {"profile_type": "rectangle", "diameter_mm": 10, "path_points_mm": [[0, 0], [50, 0]]},
        "model_3d",
    )
    duplicate = SweepLoftSkill.normalize_request(
        "sweep",
        {"diameter_mm": 10, "path_points_mm": [[0, 0], [0, 0], [50, 0]]},
        "model_3d",
    )
    assert not no_path["success"]
    assert not non_circular["success"]
    assert not duplicate["success"]


def test_sweep_accepts_explicit_tangent_line_arc_path() -> None:
    request = SweepLoftSkill.normalize_request(
        "sweep",
        {
            "diameter_mm": 10,
            "path": {
                "type": "segments",
                "segments": [
                    {"type": "line", "start_mm": [0, 0], "end_mm": [60, 0]},
                    {"type": "arc", "start_mm": [60, 0], "end_mm": [80, 20], "center_mm": [60, 20], "direction": "ccw"},
                    {"type": "line", "start_mm": [80, 20], "end_mm": [80, 70]},
                ],
            },
        },
        "model_3d",
    )
    assert request["success"]
    assert request["path_type"] == "segments"
    assert len(request["path_segments"]) == 3


def test_loft_schema_requires_ordered_closed_sections() -> None:
    request = SweepLoftSkill.normalize_request("loft", _loft_feature()["params"], "model_3d")
    assert request["success"]
    assert request["sections"][0]["shape"] == "circle"
    assert request["sections"][1]["shape"] == "rectangle"
    reversed_sections = dict(_loft_feature()["params"])
    reversed_sections["sections"] = list(reversed(reversed_sections["sections"]))
    unsupported_shape = dict(_loft_feature()["params"])
    unsupported_shape["sections"] = [
        {"offset_mm": 0, "shape": "open_spline"},
        {"offset_mm": 80, "shape": "circle", "diameter_mm": 40},
    ]
    assert not SweepLoftSkill.normalize_request("loft", reversed_sections, "model_3d")["success"]
    assert not SweepLoftSkill.normalize_request("loft", unsupported_shape, "model_3d")["success"]


def test_stage_gate_routes_new_shape_without_base_plate() -> None:
    for feature_type, feature in (("sweep", _sweep_feature()), ("loft", _loft_feature())):
        source = _design(feature)
        design = apply_stage_plan(source, infer_stage_plan(source["source_brief"], "model_3d"))
        expected = [feature_type, "save_sldprt"]
        assert not design["needs_confirmation"]
        assert design["execution_policy"]["allowed_skills"] == expected
        assert [step.skill_key for step in SkillPlanner().plan(design)] == expected
        assert [item["skill_key"] for item in SkillRouterAgent().route(design)["skill_pipeline"]] == expected


def _loft_cut_params() -> dict:
    return {
        "operation": "cut",
        "mode": "active_model",
        "base_plane": "front",
        "sections": [
            {
                "shape": "profile",
                "plane": {
                    "type": "fixed",
                    "origin_mm": [0, 60, -21],
                    "x_axis": [-1, 0, 0],
                    "y_axis": [0, 0.3303504247, 0.9438583564],
                },
                "segments": [
                    {"type": "line", "start_mm": [-3, 2], "end_mm": [-2, -4]},
                    {"type": "line", "start_mm": [-2, -4], "end_mm": [2, -4]},
                    {"type": "line", "start_mm": [2, -4], "end_mm": [3, 2]},
                    {"type": "line", "start_mm": [3, 2], "end_mm": [-3, 2]},
                ],
            },
            {"shape": "point", "offset_mm": 0, "point_mm": [0, 0]},
        ],
    }


def test_loft_cut_accepts_explicit_profile_fixed_plane_and_terminal_point() -> None:
    request = SweepLoftSkill.normalize_request("loft", _loft_cut_params(), "model_3d")
    assert request["success"]
    assert request["operation"] == "cut"
    assert request["mode"] == "active_model"
    assert request["sections"][0]["shape"] == "profile"
    assert request["sections"][0]["plane_kind"] == "fixed"
    assert len(request["sections"][0]["segments"]) == 4
    assert request["sections"][1]["shape"] == "point"


def test_loft_cut_rejects_open_profile_middle_point_and_ambiguous_plane_axes() -> None:
    open_profile = _loft_cut_params()
    open_profile["sections"][0]["segments"] = open_profile["sections"][0]["segments"][:-1]
    middle_point = _loft_cut_params()
    middle_point["sections"].insert(1, {"shape": "point", "offset_mm": 20, "point_mm": [0, 0]})
    bad_axes = _loft_cut_params()
    bad_axes["sections"][0]["plane"]["y_axis"] = [-1, 0, 0]
    wrong_mode = _loft_cut_params()
    wrong_mode["mode"] = "new_model"

    assert not SweepLoftSkill.normalize_request("loft", open_profile, "model_3d")["success"]
    assert not SweepLoftSkill.normalize_request("loft", middle_point, "model_3d")["success"]
    assert not SweepLoftSkill.normalize_request("loft", bad_axes, "model_3d")["success"]
    assert not SweepLoftSkill.normalize_request("loft", wrong_mode, "model_3d")["success"]


def test_loft_cut_calls_native_feature_manager_cut_api(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple] = []

    class FakeFeature:
        Name = ""

    class FakeFeatureManager:
        def InsertCutBlend(self, *args):
            calls.append(args)
            return FakeFeature()

    class FakeModel:
        FeatureManager = FakeFeatureManager()

        def ClearSelection2(self, _all):
            return True

    skill = SweepLoftSkill(tmp_path)
    request = SweepLoftSkill.normalize_request("loft", _loft_cut_params(), "model_3d")
    monkeypatch.setattr(skill, "_loft_plane", lambda model, base, section, index: (object(), f"Plane{index}"))
    monkeypatch.setattr(skill, "_create_section_sketch", lambda model, plane, section: f"Sketch{section['shape']}")
    monkeypatch.setattr(skill, "_select_feature", lambda model, name, append, mark: True)

    result = skill._create_loft(FakeModel(), {"name": "ToothGap"}, request)

    assert result["success"]
    assert result["operation"] == "cut"
    assert len(calls) == 1
    assert len(calls[0]) == 12


def test_fixed_loft_section_plane_uses_model_doc_api(tmp_path: Path) -> None:
    calls: list[tuple] = []

    class FakePlane:
        Name = ""

        def Select2(self, append, mark):
            return True

    class FakeModel:
        def ClearSelection2(self, _all):
            return True

        def CreatePlaneFixed2(self, *args):
            calls.append(args)
            return FakePlane()

    skill = SweepLoftSkill(tmp_path)
    request = SweepLoftSkill.normalize_request("loft", _loft_cut_params(), "model_3d")
    plane, name = skill._loft_plane(FakeModel(), request["base_plane"], request["sections"][0], 0)

    assert plane is not None
    assert name == "LoftSectionPlane1_Fixed"
    assert len(calls) == 1
    assert calls[0][-1] is False


def test_fixed_loft_section_points_remain_in_active_sketch_uv_space() -> None:
    request = SweepLoftSkill.normalize_request("loft", _loft_cut_params(), "model_3d")
    section = request["sections"][0]

    point_m = SweepLoftSkill._section_point_m(section, [2.0, 3.0])

    assert point_m == (0.002, 0.003, 0.0)


def test_loft_arc_direction_accepts_normalized_integer_and_text_values() -> None:
    assert SweepLoftSkill._sketch_arc_direction(1) == 1
    assert SweepLoftSkill._sketch_arc_direction(-1) == -1
    assert SweepLoftSkill._sketch_arc_direction("ccw") == 1
    assert SweepLoftSkill._sketch_arc_direction("cw") == -1


def test_loft_profile_disables_sketch_inferencing_and_restores_state(tmp_path: Path) -> None:
    observed: list[tuple[str, bool, bool]] = []

    class FakeSketch:
        Name = "Sketch1"

    class FakeSketchManager:
        AddToDB = False
        DisplayWhenAdded = True
        ActiveSketch = FakeSketch()

        def InsertSketch(self, _update):
            return True

        def CreateLine(self, *_args):
            observed.append(("line", self.AddToDB, self.DisplayWhenAdded))
            return object()

        def CreateArc(self, *_args):
            observed.append(("arc", self.AddToDB, self.DisplayWhenAdded))
            return object()

    class FakePlane:
        def Select2(self, _append, _mark):
            return True

    class FakeModel:
        SketchManager = FakeSketchManager()

        def ClearSelection2(self, _all):
            return True

    skill = SweepLoftSkill(tmp_path)
    section = SweepLoftSkill.normalize_request("loft", _loft_cut_params(), "model_3d")["sections"][0]

    sketch_name = skill._create_section_sketch(FakeModel(), FakePlane(), section)

    assert sketch_name == "Sketch1"
    assert observed == [("line", True, False)] * 4
    assert FakeModel.SketchManager.AddToDB is False
    assert FakeModel.SketchManager.DisplayWhenAdded is True
