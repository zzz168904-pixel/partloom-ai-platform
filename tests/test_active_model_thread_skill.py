from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.thread_skill import ThreadSkill
from cad_agent.registry import CADAgentSkillManager
from cad_agent.skill_planner import SkillPlanner


def test_source_m3_hole_wizard_request_normalizes() -> None:
    request = ThreadSkill.normalize_request(
        {
            "thread": "M3",
            "axis": "x",
            "face_offset_mm": -91,
            "center_yz_mm": [40, 0],
            "thread_depth_mm": 7,
            "pilot_depth_mm": 7,
            "tap_drill_diameter_mm": 2.5,
            "generic_hole_type": 4,
            "source_hole_type": 48,
            "standard_index": 13,
            "fastener_type_index": 358,
            "end_condition": 2,
        }
    )

    assert request["success"], request
    assert request["thread"] == "M3"
    assert request["axis"] == "x"
    assert request["face_offset_mm"] == -91.0
    assert request["center_uv_mm"] == [40.0, 0.0]
    assert request["thread_depth_mm"] == 7.0
    assert request["tap_drill_diameter_mm"] == 2.5
    assert request["end_condition"] == 2
    assert request["generic_hole_type"] == 4
    assert request["source_hole_type"] == 48


def test_source_m8_defaults_match_iso_metric_tap_drill() -> None:
    request = ThreadSkill.normalize_request(
        {
            "thread": "M8x1.25",
            "axis": "x",
            "face_offset_mm": 91,
            "center_yz_mm": [12, 0],
            "thread_depth_mm": 7,
        }
    )

    assert request["success"], request
    assert request["thread"] == "M8"
    assert request["pitch_mm"] == 1.25
    assert request["tap_drill_diameter_mm"] == 6.8
    assert request["pilot_depth_mm"] == 7.0


def test_threaded_hole_rejects_implicit_face_and_position() -> None:
    request = ThreadSkill.normalize_request({"thread": "M3"})

    assert request["success"] is False
    assert "axis" in request["message"]


def test_threaded_hole_rejects_detailed_type_as_generic_creation_type() -> None:
    request = ThreadSkill.normalize_request(
        {
            "thread": "M3",
            "axis": "x",
            "face_offset_mm": -91,
            "center_yz_mm": [40, 0],
            "generic_hole_type": 48,
        }
    )

    assert request["success"] is False
    assert "generic_hole_type=4" in request["message"]


def test_threaded_hole_can_resolve_nearby_trimmed_face_for_location_reposition(monkeypatch) -> None:
    face = object()

    def find_face(_bodies, _axis, _offset, point):
        return face if point[1] >= 0.0404 else None

    monkeypatch.setattr(
        "cad_agent.thread_skill.AdvancedFeatureSkill._find_planar_face",
        find_face,
    )
    support = ThreadSkill._resolve_support_face(
        [object()],
        "x",
        -0.091,
        (-0.091, 0.04, 0.0),
        search_radius=0.00125,
    )

    assert support["face"] is face
    assert support["reposition_required"] is True
    assert support["placement_point"][0] == -0.091
    assert support["placement_point"][1] >= 0.0404
    assert support["offset_m"] <= 0.00125


def test_threaded_hole_repositions_point_in_its_own_location_sketch(monkeypatch) -> None:
    class Point:
        X = 0.0
        Y = 0.028
        Z = 0.0

        def SetCoords(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z
            return True

    point = Point()

    class RelationManager:
        count = 1
        deleted = False

        def GetRelationsCount(self, relation_filter):
            assert relation_filter == 0
            return self.count

        def DeleteAllRelations(self):
            self.count = 0
            self.deleted = True
            return True

    relation_manager = RelationManager()

    class Sketch:
        RelationManager = relation_manager

        @staticmethod
        def GetSketchPoints2():
            return [point]

    sketch = Sketch()

    class SketchFeature:
        @staticmethod
        def GetTypeName2():
            return "ProfileFeature"

        @staticmethod
        def Select2(_append, _mark):
            return True

        @staticmethod
        def GetSpecificFeature2():
            return sketch

    sketch_feature = SketchFeature()

    class Created:
        @staticmethod
        def GetFirstSubFeature():
            return sketch_feature

    class SketchManager:
        inserted = False

        def InsertSketch(self, _update):
            self.inserted = True

    class Model:
        @staticmethod
        def ClearSelection2(_clear):
            return None

        @staticmethod
        def EditSketch():
            return None

        @staticmethod
        def ForceRebuild3(_top_only):
            return True

    Model.SketchManager = SketchManager()

    seen: dict[str, object] = {}

    def to_sketch(_model, model_point, sketch=None):
        seen["model_point"] = model_point
        seen["sketch"] = sketch
        return 0.0, 0.04, 0.0

    monkeypatch.setattr(
        "cad_agent.thread_skill.AdvancedFeatureSkill._to_sketch_point",
        to_sketch,
    )
    result = ThreadSkill._move_hole_location(
        Model(),
        Created(),
        (-0.091, 0.04, 0.0),
        (-0.091, 0.0405, 0.0),
    )

    assert result["success"]
    assert result["repositioned"]
    assert result["actual_point_sketch_mm"] == [0.0, 40.0, 0.0]
    assert result["relation_count_before"] == 1
    assert result["relation_count_after"] == 0
    assert result["relations_deleted"] is True
    assert relation_manager.deleted is True
    assert seen["sketch"] is sketch
    assert Model.SketchManager.inserted is True


def test_native_threaded_hole_is_registered_as_active_model_production_operation() -> None:
    contract = CADAgentSkillManager.production_skill_contracts()["threaded_hole"]

    assert contract["modifies_active_doc"] is True
    assert contract["creates_new_doc"] is False
    assert contract["uses_test_template"] is False
    design = {
        "execution_policy": {"allowed_skills": ["threaded_hole"]},
        "features": [],
    }
    assert [step.skill_key for step in SkillPlanner().plan(design)] == ["threaded_hole"]
