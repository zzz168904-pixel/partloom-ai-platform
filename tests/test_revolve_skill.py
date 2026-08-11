from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor
from cad_agent.design_planner import DesignPlanner
from cad_agent.revolve_skill import RevolveSkill
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _shaft_design() -> dict:
    return {
        "source_brief": "创建三段阶梯轴：φ30长40mm、φ45长60mm、φ35长30mm，只保存SLDPRT",
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "material": "40Cr"},
        "features": [
            {
                "name": "SteppedShaft",
                "type": "revolve",
                "required": True,
                "params": {
                    "operation": "base",
                    "mode": "new_model",
                    "sketch_plane": "front",
                    "axis": "horizontal",
                    "angle_deg": 360,
                    "segments": [
                        {"diameter_mm": 30, "length_mm": 40},
                        {"diameter_mm": 45, "length_mm": 60},
                        {"diameter_mm": 35, "length_mm": 30},
                    ],
                },
            }
        ],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_segments_normalize_to_closed_half_profile() -> None:
    request = RevolveSkill.normalize_request(_shaft_design()["features"][0]["params"], "model_3d")
    assert request["success"]
    assert request["operation"] == "base"
    assert request["mode"] == "new_model"
    assert request["profile_points_mm"][0] == [0.0, 0.0]
    assert request["profile_points_mm"][-1] == [0.0, 0.0]
    assert request["axial_max_mm"] == 130.0
    assert request["radius_max_mm"] == 22.5


def test_cad_ir_revolve_aliases_normalize_for_the_production_executor() -> None:
    request = RevolveSkill.normalize_request(
        {
            "body_operation": "cut",
            "execution_mode": "active_model",
            "sketch_plane": "right_plane",
            "axis": "local_horizontal_axis",
            "angle_deg": 360,
            "profile_points": [[0, 7], [0, 8.1], [0.635085296, 8.1], [0, 7]],
        },
        "model_3d",
    )

    assert request["success"], request
    assert request["operation"] == "cut"
    assert request["mode"] == "active_model"
    assert request["sketch_plane"] == "Right Plane"
    assert request["axis"] == "horizontal"


def test_face_supported_revolve_normalizes_for_source_counterbore() -> None:
    request = RevolveSkill.normalize_request(
        {
            "body_operation": "cut",
            "execution_mode": "active_model",
            "sketch_plane": "top",
            "support_face_axis": "y",
            "support_face_offset_mm": 80,
            "support_face_point_xz_mm": [-100, 0],
            "axis": "vertical",
            "axis_offset_mm": -50,
            "angle_deg": 360,
            "profile_points": [
                [45, -50],
                [48, -50],
                [48, -22.5],
                [45, -22.5],
                [45, -50],
            ],
        },
        "model_3d",
    )

    assert request["success"], request
    assert request["support_face_context"] == {
        "axis": "y",
        "face_offset_mm": 80.0,
        "point_uv_mm": [-100.0, 0.0],
    }
    assert request["axis"] == "vertical"
    assert request["axis_offset_mm"] == -50.0
    assert request["radius_max_mm"] == 27.5


def test_arc_profile_and_offset_axis_normalize_for_bearing_ball() -> None:
    request = RevolveSkill.normalize_request(
        {
            "body_operation": "base",
            "execution_mode": "new_model",
            "sketch_plane": "front",
            "axis": "horizontal",
            "axis_offset_mm": 23,
            "angle_deg": 360,
            "profile_segments": [
                {
                    "type": "arc",
                    "center_mm": [0, 23],
                    "start_mm": [4, 23],
                    "end_mm": [-4, 23],
                    "direction": 1,
                },
                {"type": "line", "start_mm": [-4, 23], "end_mm": [4, 23]},
            ],
        },
        "model_3d",
    )

    assert request["success"], request
    assert request["profile_source"] == "profile_segments"
    assert request["axis_offset_mm"] == 23.0
    assert request["axial_min_mm"] == -4.0
    assert request["axial_max_mm"] == 4.0
    assert request["radius_max_mm"] == 4.0
    assert [segment["type"] for segment in request["profile_segments_mm"]] == ["arc", "line"]


def test_profile_guard_rejects_axis_crossing_and_gaps() -> None:
    crossing = RevolveSkill.normalize_request(
        {"profile": [[0, 0], [0, 10], [20, -1], [20, 0]], "axis": "horizontal", "angle": 360},
        "model_3d",
    )
    assert not crossing["success"]
    gap = RevolveSkill.normalize_request(
        {
            "segments": [
                {"start_mm": 0, "end_mm": 20, "diameter_mm": 10},
                {"start_mm": 25, "end_mm": 40, "diameter_mm": 12},
            ]
        },
        "model_3d",
    )
    assert not gap["success"]


def test_stage_gate_routes_revolve_without_base_plate() -> None:
    design = apply_stage_plan(_shaft_design(), infer_stage_plan(_shaft_design()["source_brief"], "auto"))
    assert not design["needs_confirmation"]
    assert design["execution_policy"]["allowed_skills"] == ["revolve", "save_sldprt"]
    routed = SkillRouterAgent().route(design)
    keys = [item["skill_key"] for item in routed["skill_pipeline"]]
    assert keys == ["revolve", "save_sldprt"]
    assert "base_plate" not in keys


def test_flanged_sleeve_named_dimensions_rebuild_the_complete_profile(tmp_path: Path) -> None:
    prompt = (
        "创建一个轴对称法兰轴套：总长70mm，主体外径Φ50mm；左端法兰外径Φ70mm、厚12mm；"
        "中心有Φ30mm贯穿孔，只生成3D零件并保存SLDPRT。"
    )
    raw = {
        "parameters": {
            "total_length": 70,
            "main_outer_diameter": 50,
            "flange_outer_diameter": 70,
            "flange_thickness": 12,
            "bore_diameter": 30,
            "material": "45钢",
        },
        "features": [
            {
                "name": "revolved_base",
                "type": "revolve",
                "required": True,
                "params": {
                    "operation": "base",
                    "mode": "new_model",
                    "axis": "horizontal",
                    "profile": [[0, 15], [12, 15], [12, 35], [0, 35]],
                },
            }
        ],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
        "outputs": ["SLDPRT"],
    }
    design = DesignPlanner(tmp_path)._normalize_design(raw, prompt, stage_mode="model_3d")
    params = design["features"][0]["params"]
    request = RevolveSkill.normalize_request(params, design["task_type"])

    assert request["success"]
    assert request["axial_max_mm"] - request["axial_min_mm"] == 70.0
    assert request["radius_max_mm"] == 35.0
    assert request["profile_points_mm"] == [
        [0.0, 15.0],
        [70.0, 15.0],
        [70.0, 25.0],
        [12.0, 25.0],
        [12.0, 35.0],
        [0.0, 35.0],
        [0.0, 15.0],
    ]
    assert params["profile_reconciliation"]["applied"] is True
    assert request["semantic_profile_validation"]["success"] is True
    assert design["execution_policy"]["allowed_skills"] == ["revolve", "save_sldprt"]


def test_named_dimension_guard_rejects_flange_only_profile() -> None:
    request = RevolveSkill.normalize_request(
        {
            "profile": [[0, 15], [12, 15], [12, 35], [0, 35]],
            "design_dimensions": {
                "total_length_mm": 70,
                "main_outer_diameter_mm": 50,
                "flange_outer_diameter_mm": 70,
                "flange_thickness_mm": 12,
                "bore_diameter_mm": 30,
                "flange_side": "left",
            },
        },
        "model_3d",
    )
    assert not request["success"]
    assert request["semantic_validation"]["checks"]["total_length"] is False
    assert request["semantic_validation"]["checks"]["main_outer_diameter"] is False


def test_explicit_revolve_intent_cannot_route_to_a_plate(tmp_path: Path) -> None:
    raw = {
        "parameters": {"length": 70, "width": 50, "thickness": 12},
        "features": [
            {
                "name": "WrongPlate",
                "type": "base_plate",
                "required": True,
                "params": {"length": 70, "width": 50, "thickness": 12},
            }
        ],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }
    design = DesignPlanner(tmp_path)._normalize_design(
        raw,
        "创建轴对称套筒，闭合截面绕中心轴旋转360°成型，只生成3D零件。",
        stage_mode="model_3d",
    )
    assert design["needs_confirmation"] is True
    assert any(item.get("type") == "feature_family_mismatch" for item in design["unsupported_features"])


def test_corrupted_design_brief_is_blocked(tmp_path: Path) -> None:
    raw = {
        "parameters": {"length": 70, "width": 50, "thickness": 12},
        "features": [
            {
                "name": "WrongPlate",
                "type": "base_plate",
                "required": True,
                "params": {"length": 70, "width": 50, "thickness": 12},
            }
        ],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }
    design = DesignPlanner(tmp_path)._normalize_design(raw, "????????????????????", stage_mode="model_3d")
    assert design["needs_confirmation"] is True
    assert any(item.get("type") == "planning_input_corrupted" for item in design["unsupported_features"])


def test_reopen_validation_waits_for_delayed_solid_body(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "delayed_body.SLDPRT"
    path.write_bytes(b"solidworks-part")

    class FakeBody:
        def GetFaces(self):
            return ()

        def GetEdges(self):
            return ()

    class FakeModel:
        def GetPathName(self):
            return str(path)

        def GetTitle(self):
            return path.name

        def ForceRebuild3(self, _top_only):
            return True

    class FakeSolidWorks:
        ActiveDoc = None

        def CloseDoc(self, _title):
            return None

        def OpenDoc(self, _path, _doc_type):
            self.ActiveDoc = FakeModel()
            return self.ActiveDoc

    body_results = iter(
        [
            {"success": False, "message": "Part contains no solid bodies."},
            {"success": False, "message": "Part contains no solid bodies."},
            {"success": True, "bodies": [FakeBody()], "bbox": {"xmin": 0.0, "xmax": 1.0}},
        ]
    )
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_body_info",
        staticmethod(lambda _model: next(body_results)),
    )
    monkeypatch.setattr(RevolveSkill, "REOPEN_POLL_S", 0.0)

    result = RevolveSkill._verify_reopen(FakeSolidWorks(), FakeModel(), path)

    assert result["success"] is True
    assert result["body_count"] == 1
    assert result["load_attempts"] == 3
    assert result["load_timed_out"] is False


def test_reopen_prefers_dynamic_active_doc_after_open_doc6(tmp_path: Path) -> None:
    path = (tmp_path / "typed_open_result.SLDPRT").resolve()
    path.write_bytes(b"solidworks-part")

    class FakeBody:
        def GetBodyBox(self):
            return (0.0, 0.0, 0.0, 0.1, 0.05, 0.01)

        def GetFaces(self):
            return ()

        def GetEdges(self):
            return ()

    class FakeModel:
        def GetPathName(self):
            return str(path)

        def GetTitle(self):
            return path.name

        def ForceRebuild3(self, _top_only):
            return True

        def GetBodies2(self, _body_type, _visible_only):
            return (FakeBody(),)

    class TypedOpenResult:
        def GetPathName(self):
            return str(path)

    class FakeSolidWorks:
        def __init__(self):
            self.ActiveDoc = None

        def CloseDoc(self, _title):
            self.ActiveDoc = None

        def OpenDoc6(self, opened_path, _doc_type, _options, _configuration, _errors, _warnings):
            assert Path(opened_path) == path
            self.ActiveDoc = FakeModel()
            return TypedOpenResult(), 0, 0

    original = FakeModel()
    sw = FakeSolidWorks()
    result = RevolveSkill._verify_reopen(sw, original, path)

    assert result["success"] is True
    assert result["body_count"] == 1
    assert result["open_status"]["method"] == "OpenDoc6"
    assert result["model"] is sw.ActiveDoc


def test_active_revolve_cut_requires_measurable_volume_reduction() -> None:
    request = {"operation": "cut"}
    before = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 12, "edge_count": 24}],
        "volume_m3": 8.0e-4,
    }
    unchanged = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 14, "edge_count": 28}],
        "volume_m3": 8.0e-4,
    }
    removed = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 14, "edge_count": 28}],
        "volume_m3": 7.5e-4,
    }

    failure = RevolveSkill._verify_geometry(request, before, unchanged)
    success = RevolveSkill._verify_geometry(request, before, removed)

    assert failure["success"] is False
    assert "did not remove" in failure["message"]
    assert success["success"] is True
    assert success["volume_delta_m3"] < 0.0


def test_active_revolve_boss_requires_merged_volume_increase() -> None:
    request = {"operation": "boss"}
    before = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 12, "edge_count": 24}],
        "volume_m3": 8.0e-4,
    }
    separate_body = {
        "success": True,
        "body_count": 2,
        "body_info": [
            {"face_count": 12, "edge_count": 24},
            {"face_count": 3, "edge_count": 3},
        ],
        "volume_m3": 8.1e-4,
    }

    result = RevolveSkill._verify_geometry(request, before, separate_body)

    assert result["success"] is False
    assert "separate solid" in result["message"]


def test_active_revolve_boss_can_require_exactly_one_new_body() -> None:
    params = {
        "body_operation": "boss",
        "execution_mode": "active_model",
        "expected_body_result": "new_body",
        "profile": [[0, 10], [0, 20], [5, 20], [5, 10], [0, 10]],
    }
    request = RevolveSkill.normalize_request(params, "model_3d")
    before = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 12, "edge_count": 24}],
        "volume_m3": 8.0e-4,
    }
    separate_body = {
        "success": True,
        "body_count": 2,
        "body_info": [
            {"face_count": 12, "edge_count": 24},
            {"face_count": 3, "edge_count": 3},
        ],
        "volume_m3": 8.1e-4,
    }

    result = RevolveSkill._verify_geometry(request, before, separate_body)

    assert request["success"] is True
    assert request["expected_body_result"] == "new_body"
    assert result["success"] is True
    assert result["body_count_before"] == 1
    assert result["body_count_after"] == 2
    assert result["expected_body_result"] == "new_body"


def test_active_revolve_new_body_contract_rejects_accidental_merge() -> None:
    request = {"operation": "boss", "expected_body_result": "new_body"}
    before = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 12, "edge_count": 24}],
        "volume_m3": 8.0e-4,
    }
    merged = {
        "success": True,
        "body_count": 1,
        "body_info": [{"face_count": 14, "edge_count": 28}],
        "volume_m3": 8.1e-4,
    }

    result = RevolveSkill._verify_geometry(request, before, merged)

    assert result["success"] is False
    assert "exactly one requested new solid body" in result["message"]


if __name__ == "__main__":
    test_segments_normalize_to_closed_half_profile()
    test_profile_guard_rejects_axis_crossing_and_gaps()
    test_stage_gate_routes_revolve_without_base_plate()
    print("Revolve Skill tests passed")
