from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.feature_management_skill import FeatureManagementSkill
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _design() -> dict:
    prompt = "创建100x80x50mm壳体，四侧拔模2度，顶面开口抽壳3mm，并建立距上视基准面20mm的偏置面，只保存SLDPRT"
    return {
        "source_brief": prompt,
        "parameters": {"unit": "mm", "length": 100.0, "width": 80.0, "thickness": 50.0, "material": "ABS"},
        "features": [
            {"name": "Base", "type": "base_plate", "required": True, "params": {"length": 100, "width": 80, "thickness": 50}},
            {"name": "OffsetPlane20", "type": "reference_geometry", "required": True, "params": {"reference_type": "offset_plane", "base_plane": "top", "offset_mm": 20}},
            {"name": "OuterDraft", "type": "draft", "required": True, "params": {"angle_deg": 2, "neutral_plane": "bottom", "target_faces": "outer_vertical_faces"}},
            {"name": "TopOpenShell", "type": "shell", "required": True, "params": {"thickness_mm": 3, "remove_faces": ["top"], "outward": False}},
        ],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_feature_management_schemas() -> None:
    shell = FeatureManagementSkill.normalize_request("shell", {"thickness_mm": 3, "remove_faces": ["top"]})
    draft = FeatureManagementSkill.normalize_request("draft", {"angle_deg": 2, "neutral_plane": "bottom", "target_faces": "outer_vertical_faces"})
    plane = FeatureManagementSkill.normalize_request("reference_geometry", {"reference_type": "offset_plane", "base_plane": "top", "offset_mm": 20})
    axis = FeatureManagementSkill.normalize_request("reference_geometry", {"reference_type": "axis_two_planes", "plane_1": "front", "plane_2": "top"})
    assert shell["success"] and shell["remove_faces"] == ["top"]
    assert draft["success"] and draft["angle_deg"] == 2.0
    assert plane["success"] and plane["base_plane"] == "Top Plane"
    assert axis["success"] and axis["plane_1"] != axis["plane_2"]


def test_ambiguous_face_selection_is_blocked() -> None:
    assert not FeatureManagementSkill.normalize_request("shell", {"thickness_mm": 3})["success"]
    assert not FeatureManagementSkill.normalize_request("draft", {"angle_deg": 2, "neutral_plane": "auto", "target_faces": "outer_vertical_faces"})["success"]
    assert not FeatureManagementSkill.normalize_request("reference_geometry", {"reference_type": "axis_two_planes", "plane_1": "top", "plane_2": "top"})["success"]


def test_draft_accepts_deterministic_planar_face_signatures() -> None:
    request = FeatureManagementSkill.normalize_request(
        "draft",
        {
            "angle_deg": 30,
            "neutral_plane": "explicit_planar_face_signature",
            "neutral_face_signature": {
                "normal_axis": 0,
                "plane_coordinate_mm": 2.59,
                "face_box_mm": [2.59, -2.45, -2.45, 2.59, 2.45, 2.45],
            },
            "target_faces": "explicit_planar_face_signatures",
            "target_face_signatures": [
                {
                    "normal_axis": 1,
                    "plane_coordinate_mm": -2.45,
                    "face_box_mm": [0.384391029, -2.45, -0.49, 2.59, -2.45, 0.49],
                }
            ],
            "face_tolerance_mm": 0.03,
            "flip_direction": True,
        },
    )

    assert request["success"]
    assert request["neutral_face_signature"]["plane_coordinate_m"] == 0.00259
    assert request["target_face_signatures"][0]["face_box_m"][1] == pytest.approx(-0.00245)
    assert request["flip_direction"] is True


def test_draft_rejects_underspecified_or_invalid_face_signatures() -> None:
    missing_targets = FeatureManagementSkill.normalize_request(
        "draft",
        {
            "angle_deg": 2,
            "neutral_plane": "top",
            "target_faces": "explicit_planar_face_signatures",
        },
    )
    vague_neutral = FeatureManagementSkill.normalize_request(
        "draft",
        {
            "angle_deg": 2,
            "neutral_plane": "explicit_planar_face_signature",
            "neutral_face_signature": {"normal_axis": 0},
            "target_faces": "outer_vertical_faces",
        },
    )

    assert not missing_targets["success"]
    assert not vague_neutral["success"]


def test_stage_router_and_skill_planner_preserve_management_order() -> None:
    source = _design()
    design = apply_stage_plan(source, infer_stage_plan(source["source_brief"], "auto"))
    assert not design["needs_confirmation"]
    assert design["execution_policy"]["allowed_skills"] == [
        "base_plate",
        "reference_geometry",
        "draft",
        "shell",
        "save_sldprt",
    ]
    expected = ["base_plate", "reference_geometry", "draft", "shell", "save_sldprt"]
    assert [step.skill_key for step in SkillPlanner().plan(design)] == expected
    assert [step["skill_key"] for step in SkillRouterAgent().route(design)["skill_pipeline"]] == expected


def test_offset_plane_uses_flip_constraint_and_measures_world_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    class FakePoint:
        def __init__(self, x: float, y: float, z: float) -> None:
            self.ArrayData = [x, y, z]

    class FakePlane:
        def __init__(self, origin_x: float) -> None:
            self.Name = ""
            self.CornerPoints = [
                FakePoint(origin_x, -0.1, -0.1),
                FakePoint(origin_x, 0.1, -0.1),
                FakePoint(origin_x, 0.1, 0.1),
                FakePoint(origin_x, -0.1, 0.1),
            ]

        def Select2(self, _append: bool, _mark: int) -> bool:
            return True

    class FakeFeatureManager:
        def InsertRefPlane(self, *args):
            calls.append(args)
            constraint, distance = args[:2]
            origin_x = -distance if constraint & 256 else distance
            return FakePlane(origin_x)

    class FakeModel:
        FeatureManager = FakeFeatureManager()

        def ClearSelection2(self, _all: bool) -> bool:
            return True

        def ForceRebuild3(self, _top_only: bool) -> bool:
            return True

        def BlankRefGeom(self) -> bool:
            return True

    monkeypatch.setattr(FeatureManagementSkill, "_select_named_plane", classmethod(lambda cls, model, name, append: True))
    monkeypatch.setattr("cad_agent.feature_management_skill.ActiveModelFeatureSkill._feature_tree", lambda model: [])
    skill = FeatureManagementSkill(tmp_path)
    model = FakeModel()

    negative = skill._create_reference_geometry(
        model,
        {"name": "PlaneNegative"},
        {
            "reference_type": "offset_plane",
            "base_plane": "Right Plane",
            "offset_mm": -50.0,
        },
        {},
    )
    positive = skill._create_reference_geometry(
        model,
        {"name": "PlanePositive"},
        {
            "reference_type": "offset_plane",
            "base_plane": "Right Plane",
            "offset_mm": 20.0,
        },
        {},
    )

    assert negative["success"]
    assert positive["success"]
    assert calls[0] == (264, 0.05, 0, 0.0, 0, 0.0)
    assert calls[1] == (8, 0.02, 0, 0.0, 0, 0.0)
    assert negative["geometry_validation"]["measured_offset_mm"] == pytest.approx(-50.0)
    assert positive["geometry_validation"]["measured_offset_mm"] == pytest.approx(20.0)


def test_offset_plane_rejects_tree_node_at_wrong_world_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePoint:
        ArrayData = [0, 0, 0]

    class FakePlane:
        Name = ""
        CornerPoints = [FakePoint(), FakePoint(), FakePoint(), FakePoint()]

    class FakeFeatureManager:
        def InsertRefPlane(self, *_args):
            return FakePlane()

    class FakeModel:
        FeatureManager = FakeFeatureManager()

        def ClearSelection2(self, _all: bool) -> bool:
            return True

        def ForceRebuild3(self, _top_only: bool) -> bool:
            return True

    monkeypatch.setattr(FeatureManagementSkill, "_select_named_plane", classmethod(lambda cls, model, name, append: True))
    monkeypatch.setattr("cad_agent.feature_management_skill.ActiveModelFeatureSkill._feature_tree", lambda model: [])
    result = FeatureManagementSkill(tmp_path)._create_reference_geometry(
        FakeModel(),
        {"name": "WrongPlane"},
        {
            "reference_type": "offset_plane",
            "base_plane": "Right Plane",
            "offset_mm": -50.0,
        },
        {},
    )

    assert not result["success"]
    assert result["geometry_validation"]["measured_offset_mm"] == 0.0
    assert "node exists" in result["message"]


if __name__ == "__main__":
    test_feature_management_schemas()
    test_ambiguous_face_selection_is_blocked()
    test_stage_router_and_skill_planner_preserve_management_order()
    print("Feature management Skill tests passed")
