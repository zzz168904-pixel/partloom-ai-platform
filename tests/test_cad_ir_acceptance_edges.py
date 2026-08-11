from __future__ import annotations

import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor
from cad_agent.cad_ir import CADIRCompiler
from cad_agent.geometry_context import FaceContext


def _design(features: list[dict], unit: str = "mm") -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": "cad-ir acceptance edge test",
        "task_type": "model_3d",
        "parameters": {"unit": unit},
        "features": features,
        "unsupported_features": [],
        "risks": [],
    }


def _base() -> dict:
    return {
        "name": "Base",
        "type": "base_plate",
        "params": {"length": 100, "width": 60, "thickness": 10},
    }


def test_operation_contracts_cover_the_closed_catalog() -> None:
    contracts = CADIRCompiler.operation_contracts()

    assert tuple(contracts) == CADIRCompiler.STANDARD_OPERATIONS
    assert all(contract["context_requirements"] for contract in contracts.values())
    assert contracts["base_plate"]["required_parameters"] == [
        "length_mm",
        "width_mm",
        "thickness_mm",
    ]
    assert "center_hole" in contracts["through_hole"]["aliases"]
    assert contracts["linear_pattern"]["required_any"] == [
        ["seed_features"],
        ["count"],
        ["spacing_mm"],
        ["direction"],
    ]


def test_unknown_dependency_returns_structured_error() -> None:
    source = _design([
        _base(),
        {
            "name": "Hole",
            "type": "through_hole",
            "depends_on": ["DoesNotExist"],
            "params": {"diameter": 10, "position": "center"},
        },
    ])

    result = CADIRCompiler().compile(source)

    assert not result.success
    error = next(item for item in result.errors if item["code"] == "dependency_not_found")
    assert error == {
        "code": "dependency_not_found",
        "message": "Feature 'Hole' references unknown dependency 'DoesNotExist'.",
        "feature_id": "hole",
        "field": "dependencies",
    }


def test_dependency_cycle_is_rejected() -> None:
    source = _design([
        {
            "name": "EquationA",
            "type": "equation",
            "depends_on": ["EquationB"],
            "params": {"name": "A", "expression": "B + 1"},
        },
        {
            "name": "EquationB",
            "type": "equation",
            "depends_on": ["EquationA"],
            "params": {"name": "B", "expression": "A + 1"},
        },
    ])

    result = CADIRCompiler().compile(source)

    assert not result.success
    assert any(item["code"] == "dependency_cycle" for item in result.errors)


def test_pattern_spacing_converts_inches_to_millimetres() -> None:
    source = _design([
        _base(),
        {
            "name": "Hole",
            "type": "through_hole",
            "params": {"diameter": 8, "position": "center"},
        },
        {
            "name": "Pattern",
            "type": "linear_pattern",
            "params": {
                "seed_feature": "Hole",
                "count": 3,
                "spacing": {"value": 1, "unit": "in"},
                "direction": "x",
            },
        },
    ])

    result = CADIRCompiler().compile(source)

    assert result.success
    feature = result.design["features"][2]
    assert math.isclose(feature["params"]["spacing"], 25.4)
    assert math.isclose(feature["params"]["spacing_1"], 25.4)
    assert math.isclose(result.ir["features"][2]["parameters"]["spacing_mm"], 25.4)


def test_nested_unknown_unit_is_reported_as_unsupported_unit() -> None:
    source = _design([
        _base(),
        {
            "name": "BadUnitHole",
            "type": "through_hole",
            "params": {"diameter": {"value": 1, "unit": "yard"}},
        },
    ])

    result = CADIRCompiler().compile(source)

    assert not result.success
    assert any(
        item["code"] == "unsupported_unit" and item["field"] == "diameter_mm"
        for item in result.errors
    )
    assert not any(
        item["code"] == "missing_required_parameter" and item["field"] == "diameter_mm"
        for item in result.errors
    )


def test_bounding_box_location_updates_face_origin_without_mutating_source() -> None:
    context = FaceContext(
        target_body="primary_solid",
        target_feature="base_plate",
        target_face_role="outer_horizontal_face",
        face_normal=[0.0, 0.0, 1.0],
        face_origin=[0.0, 0.0, 0.0],
        local_u_axis=[1.0, 0.0, 0.0],
        local_v_axis=[0.0, 1.0, 0.0],
        placement_uv_mm=[[0.0, 0.0]],
        bounding_box_location="z_max",
    )

    adjusted = ActiveModelThroughHoleExecutor._context_at_bounding_box(
        context,
        {"zmin": -0.01, "zmax": 0.0},
    )

    assert context.face_origin == [0.0, 0.0, 0.0]
    assert adjusted.face_origin == [0.0, 0.0, 0.0]
    raised = ActiveModelThroughHoleExecutor._context_at_bounding_box(
        context,
        {"zmin": 0.0, "zmax": 0.012},
    )
    assert raised.face_origin == [0.0, 0.0, 12.0]


def test_solid_volume_falls_back_to_mass_properties() -> None:
    class BodyWithoutGetVolume:
        def GetMassProperties(self, density: float):
            assert density == 1.0
            return (0.0, 0.0, 0.0, 0.000123)

    volume = ActiveModelThroughHoleExecutor._solid_volume([BodyWithoutGetVolume()])

    assert volume == 0.000123
