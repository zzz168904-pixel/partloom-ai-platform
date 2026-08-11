from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.brain_models import BrainPlan
from cad_agent.cad_ir import CADIRCompiler
from cad_agent.design_planner import DesignPlanner
from cad_agent.pipeline.pipeline_context import PipelineContext
from cad_agent.pipeline.pipeline_executor import PipelineExecutor
from cad_agent.pipeline.pipeline_logger import PipelineLogger
from cad_agent.registry import CADAgentSkillManager


def _design(features: list[dict], unit: str = "mm") -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": "test",
        "task_type": "model_3d",
        "parameters": {"unit": unit},
        "features": features,
        "unsupported_features": [],
        "risks": [],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_aliases_compile_to_one_canonical_hole_contract() -> None:
    source = _design([
        {
            "name": "Base",
            "type": "base_plate",
            "required": True,
            "params": {"length": 100, "width": 60, "thickness": 10},
        },
        {
            "name": "CenterHole",
            "type": "center_hole",
            "required": True,
            "params": {"hole_diameter": {"value": 1, "unit": "in"}},
        }
    ])

    result = CADIRCompiler().compile(source)

    assert result.success
    assert result.design["features"][1]["type"] == "through_hole"
    assert result.design["features"][1]["params"]["diameter"] == 25.4
    assert result.design["features"][1]["params"]["position"] == "center"
    assert "hole_diameter" not in result.design["features"][1]["params"]
    assert result.ir["features"][1]["parameters"]["diameter_mm"] == 25.4
    assert result.ir["features"][1]["source"]["feature_type"] == "center_hole"
    assert result.ir["features"][1]["target"]["feature_ref"] == "base"
    assert result.ir["features"][1]["dependencies"] == [
        {"kind": "target_feature", "feature_id": "base"}
    ]


def test_conflicting_alias_values_are_blocked() -> None:
    source = _design([
        {
            "name": "BadHole",
            "type": "through_hole",
            "required": True,
            "params": {"diameter": 20, "diameter_mm": 22},
        }
    ])

    result = CADIRCompiler().compile(source)

    assert not result.success
    assert result.design["needs_confirmation"]
    assert result.errors[0]["code"] == "conflicting_parameter_aliases"
    assert result.design["unsupported_features"][0]["type"] == "cad_ir_validation_error"


def test_missing_required_geometry_parameter_is_blocked() -> None:
    result = CADIRCompiler().compile(_design([
        {"name": "MissingDiameter", "type": "simple_hole", "required": True, "params": {}}
    ]))

    assert not result.success
    assert any(item["field"] == "diameter_mm" for item in result.errors)


def test_global_and_base_plate_dimensions_convert_to_millimetres(tmp_path: Path) -> None:
    raw = _design([
        {
            "name": "Base",
            "type": "base_plate",
            "required": True,
            "params": {"length": 4, "width": 2, "thickness": 0.5},
        }
    ], unit="in")

    design = DesignPlanner(tmp_path)._normalize_design(raw, "Create only a plate.", stage_mode="model_3d")

    assert design["cad_ir_validation"]["success"]
    assert design["parameters"]["unit"] == "mm"
    assert design["features"][0]["params"]["length"] == 101.6
    assert design["features"][0]["params"]["width"] == 50.8
    assert design["features"][0]["params"]["thickness"] == 12.7
    assert design["cad_ir"]["features"][0]["parameters"]["length_mm"] == 101.6


def test_pipeline_blocks_invalid_cad_ir_before_any_skill(tmp_path: Path) -> None:
    design = _design([
        {"name": "MissingDiameter", "type": "through_hole", "required": True, "params": {}}
    ])
    context = PipelineContext(prompt="test", output_root=tmp_path, run_dir=tmp_path / "run")
    context.design_json = design
    context.brain_plan = BrainPlan(
        source_prompt="test",
        design_json=design,
        skill_pipeline=[],
        model_provider="test",
    )
    logger = PipelineLogger(tmp_path / "pipeline.jsonl")
    executor = PipelineExecutor(CADAgentSkillManager(tmp_path), logger, execute_real_skills=False)

    executor.execute(context)

    assert context.status == "failed"
    assert context.step_records == []
    assert context.final_model_validation["reason"] == "cad_ir_validation_failed"
    assert "cad_ir_validation_failed" in (tmp_path / "pipeline.jsonl").read_text(encoding="utf-8")


def test_forward_dependency_is_rejected() -> None:
    source = _design([
        {
            "name": "HoleFirst",
            "type": "through_hole",
            "depends_on": ["BaseLater"],
            "params": {"diameter": 10},
        },
        {
            "name": "BaseLater",
            "type": "base_plate",
            "params": {"length": 100, "width": 60, "thickness": 10},
        },
    ])

    result = CADIRCompiler().compile(source)

    assert not result.success
    assert any(item["code"] == "dependency_order_invalid" for item in result.errors)


def test_modify_task_uses_explicit_active_document_dependency() -> None:
    source = _design([
        {"name": "EdgeRound", "type": "fillet", "params": {"radius": 3}}
    ])
    source["task_type"] = "modify_3d"

    result = CADIRCompiler().compile(source)

    assert result.success
    assert result.ir["features"][0]["dependencies"] == [
        {"kind": "active_document", "document_ref": "active_part"}
    ]
