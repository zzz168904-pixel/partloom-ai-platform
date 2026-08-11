from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.active_model_feature_skill import ActiveModelFeatureSkill
from cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor
from cad_agent.cad_ir import CADIRCompiler
from cad_agent.direct_cad_ir import DirectCADIRService
from cad_agent.geometry_resolver.models import GeometryCandidate
from cad_agent.geometry_resolver.solidworks_adapter import SolidWorksFaceInspector
from cad_agent.pipeline.pipeline_context import PipelineStepRecord
from cad_agent.pipeline.pipeline_executor import PipelineExecutor
from cad_agent.skill_planner import SkillPlanner


def _selector(**overrides: object) -> dict:
    value = {
        "type": "planar_face_signature",
        "axis": "z",
        "coordinate_mm": 0.0,
        "area_mm2": 1600.0,
        "normal_sign": 1,
        "coordinate_tolerance_mm": 0.02,
        "area_tolerance_mm2": 0.2,
        "body_index": 0,
    }
    value.update(overrides)
    return value


def _design() -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": "Create a 40 x 40 x 10 mm base and a 5 mm native dome.",
        "task_type": "model_3d",
        "part_family": "dome_acceptance_part",
        "parameters": {
            "unit": "mm",
            "length": 40,
            "width": 40,
            "thickness": 10,
        },
        "features": [
            {
                "name": "DomeBase",
                "type": "base_plate",
                "required": True,
                "params": {
                    "length_mm": 40,
                    "width_mm": 40,
                    "thickness_mm": 10,
                },
            },
            {
                "name": "FrontDome",
                "type": "dome",
                "required": True,
                "depends_on": ["DomeBase"],
                "target_body": "primary_solid",
                "target_reference": {
                    "feature_id": "DomeBase",
                    "role": "dome_planar_face",
                },
                "params": {
                    "mode": "active_model",
                    "height_mm": 5,
                    "reverse_direction": False,
                    "elliptical": False,
                    "face_selector": _selector(),
                },
            },
        ],
        "outputs": ["SLDPRT"],
    }


def _candidate(entity: object, **signature_overrides: object) -> GeometryCandidate:
    signature = {
        "entity_type": "Face",
        "surface_type": "plane",
        "planar": True,
        "normal": [0.0, 0.0, 1.0],
        "normal_axis": 2,
        "plane_coordinate_m": 0.0,
        "area_m2": 0.0016,
        "body_index": 0,
        "face_index": 1,
    }
    signature.update(signature_overrides)
    return GeometryCandidate(signature=signature, entity=entity)


def test_dome_request_requires_an_explicit_complete_face_signature() -> None:
    request = ActiveModelFeatureSkill.normalize_dome_request({
        "mode": "active_model",
        "height_mm": 5,
        "face_selector": _selector(),
    })

    assert request["success"]
    assert request["height_mm"] == 5.0
    assert request["face_selector"] == _selector()
    assert not ActiveModelFeatureSkill.normalize_dome_request(
        {"mode": "active_model", "height_mm": 5}
    )["success"]
    assert not ActiveModelFeatureSkill.normalize_dome_request({
        "mode": "new_model",
        "height_mm": 5,
        "face_selector": _selector(),
    })["success"]
    assert not ActiveModelFeatureSkill.normalize_dome_request({
        "mode": "active_model",
        "height_mm": 5,
        "face_selector": _selector(normal_sign=0),
    })["success"]


def test_cad_ir_dome_contract_and_target_preserve_the_face_signature() -> None:
    result = CADIRCompiler().compile(_design())

    assert result.success, result.errors
    assert [feature["id"] for feature in result.design["features"]] == [
        "domebase",
        "frontdome",
    ]
    contract = CADIRCompiler.operation_contracts()["dome"]
    assert contract["required_parameters"] == ["height_mm", "face_selector"]
    assert "face_signature_must_resolve_exactly_one_face" in contract["conflict_rules"]
    dome = result.ir["features"][1]
    assert dome["operation"] == "dome"
    assert dome["parameters"]["height_mm"] == 5.0
    assert dome["parameters"]["face_selector"] == _selector()
    assert dome["target"]["resolution"] == "explicit_planar_face_signature"
    assert dome["target"]["face_selector"] == _selector()


def test_cad_ir_blocks_dome_without_a_face_signature() -> None:
    design = _design()
    design["features"][1]["params"].pop("face_selector")

    result = CADIRCompiler().compile(design)

    assert not result.success
    assert any(
        issue["code"] in {"missing_required_parameter_group", "invalid_dome_request"}
        for issue in result.errors
    )


def test_dome_face_signature_must_resolve_exactly_one_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = object()
    second = object()
    candidates = [_candidate(first)]
    monkeypatch.setattr(
        SolidWorksFaceInspector,
        "enumerate_planar_faces",
        lambda self, bodies: tuple(candidates),
    )

    resolved = ActiveModelFeatureSkill._resolve_dome_face([object()], _selector())

    assert resolved["success"]
    assert resolved["face"] is first
    assert resolved["candidate_count"] == 1

    candidates.append(_candidate(second, face_index=2))
    ambiguous = ActiveModelFeatureSkill._resolve_dome_face([object()], _selector())
    assert not ambiguous["success"]
    assert ambiguous["candidate_count"] == 2
    assert "ambiguous" in ambiguous["message"].lower()

    candidates.clear()
    missing = ActiveModelFeatureSkill._resolve_dome_face([object()], _selector())
    assert not missing["success"]
    assert missing["candidate_count"] == 0


def test_native_dome_execution_requires_one_new_feature_and_volume_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DomeDefinition:
        Height = 0.005
        ReverseDir = False
        Elliptical = False

    class Feature:
        Name = "Dome1"

        def GetDefinition(self) -> DomeDefinition:
            return DomeDefinition()

    class Model:
        def ClearSelection2(self, clear_all: bool) -> None:
            assert clear_all is True

        def InsertDome(
            self,
            height_m: float,
            reverse_direction: bool,
            elliptical: bool,
        ) -> None:
            assert height_m == pytest.approx(0.005)
            assert reverse_direction is False
            assert elliptical is False

        def ForceRebuild3(self, top_only: bool) -> None:
            assert top_only is False

        def FeatureByName(self, name: str) -> Feature:
            assert name == "Dome1"
            return Feature()

    skill = ActiveModelFeatureSkill(tmp_path)
    tree = iter([
        [{"name": "Boss-Extrude1", "type": "Boss"}],
        [
            {"name": "Boss-Extrude1", "type": "Boss"},
            {"name": "Dome1", "type": "Dome"},
        ],
    ])
    monkeypatch.setattr(skill, "_feature_tree", lambda model: next(tree))
    monkeypatch.setattr(
        skill,
        "_resolve_dome_face",
        lambda bodies, selector: {
            "success": True,
            "face": object(),
            "signature": {"area_mm2": 1600.0, "plane_coordinate_mm": 0.0},
            "candidate_count": 1,
        },
    )
    monkeypatch.setattr(skill, "_select_face_with_mark", lambda model, face, mark: mark == 1)
    monkeypatch.setattr(skill, "_name_feature", lambda feature, name: setattr(feature, "Name", name))
    monkeypatch.setattr(skill, "_feature_name", lambda feature: feature.Name)
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_body_info",
        staticmethod(lambda model: {"success": True, "bodies": ["after"]}),
    )
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_solid_volume",
        staticmethod(lambda bodies: 1.0 if bodies == ["before"] else 1.1),
    )
    feature = _design()["features"][1]

    result = skill._create_dome(
        Model(),
        _design(),
        feature,
        {"success": True, "bodies": ["before"]},
    )

    assert result["success"]
    assert result["feature_type"] == "Dome"
    assert result["feature_name"] == "FrontDome"
    assert result["candidate_face_count"] == 1
    assert result["volume_before_m3"] == 1.0
    assert result["volume_after_m3"] == 1.1
    assert result["volume_changed"] is True
    assert result["native_definition"] == {
        "height_m": 0.005,
        "height_mm": 5.0,
        "reverse_direction": False,
        "elliptical": False,
        "matches_request": True,
    }


def test_direct_dome_plan_routes_only_the_requested_model_operations(
    tmp_path: Path,
) -> None:
    plan = DirectCADIRService(tmp_path).plan(
        json.dumps(_design()),
        stage_mode="model_3d",
    )

    assert plan["planning_validation"]["status"] == "approved"
    assert plan["execution_policy"]["allowed_skills"] == [
        "base_plate",
        "dome",
        "save_sldprt",
    ]
    assert [step.skill_key for step in SkillPlanner().plan(plan)] == [
        "base_plate",
        "dome",
        "save_sldprt",
    ]


def test_final_model_validator_rejects_dome_without_native_geometry_evidence() -> None:
    design = _design()
    request = ActiveModelFeatureSkill.normalize_dome_request(
        design["features"][1]["params"]
    )
    record = PipelineStepRecord(
        skill_key="dome",
        action="apply_dome",
        required=True,
        reason="test",
    )
    record.status = "success"
    record.data = {
        "feature_created": True,
        "operations": [{
            "success": True,
            "type": "dome",
            "name": "FrontDome",
            "feature_type": "Dome",
            "request": request,
            "candidate_face_count": 1,
            "native_definition": {
                "height_mm": 5.0,
                "matches_request": True,
            },
            "volume_changed": True,
        }]
    }

    assert PipelineExecutor._feature_operation_matches(design, record, "dome")
    record.data["operations"][0]["native_definition"]["height_mm"] = 4.0
    assert not PipelineExecutor._feature_operation_matches(design, record, "dome")
    record.data["operations"][0]["native_definition"]["height_mm"] = 5.0
    record.data["operations"][0]["volume_changed"] = False
    assert not PipelineExecutor._feature_operation_matches(design, record, "dome")
