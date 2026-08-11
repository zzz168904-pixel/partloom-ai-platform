from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.parametric_management_skill import ParametricManagementSkill
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _design() -> dict:
    prompt = "Create a 100x60x10 plate with a Standard configuration and global variables. Save SLDPRT only."
    return {
        "source_brief": prompt,
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "length": 100, "width": 60, "thickness": 10},
        "features": [
            {"name": "Base", "type": "base_plate", "required": True, "params": {}},
            {
                "name": "StandardConfig",
                "type": "configuration",
                "required": True,
                "params": {"name": "Standard", "comment": "Production configuration", "activate": False},
            },
            {
                "name": "LengthVariable",
                "type": "equation",
                "required": True,
                "params": {"kind": "global_variable", "name": "PlateLength", "value": 100, "unit": "mm", "scope": "all"},
            },
        ],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_configuration_schema_builds_deterministic_options() -> None:
    request = ParametricManagementSkill.normalize_request(
        "configuration",
        {"name": "Machined", "alternate_name": "MACH", "activate": False},
    )
    assert request["success"]
    assert request["options"]["use_alternate_name"]
    assert request["options"]["dont_activate"]
    assert request["options_mask"] == 129


def test_equation_schema_supports_structured_and_native_syntax() -> None:
    structured = ParametricManagementSkill.normalize_request(
        "equation",
        {"kind": "global_variable", "name": "PlateLength", "value": 100, "unit": "mm"},
    )
    native = ParametricManagementSkill.normalize_request(
        "equation",
        {"kind": "dimension", "expression": '"D1@BasePlate" = "PlateLength"', "scope": "this"},
    )
    assert structured["success"] and structured["expression"] == '"PlateLength" = 100mm'
    assert native["success"] and native["lhs"] == "D1@BasePlate"


def test_parametric_schema_blocks_ambiguous_or_unsafe_requests() -> None:
    assert not ParametricManagementSkill.normalize_request("configuration", {"name": ""})["success"]
    assert not ParametricManagementSkill.normalize_request("equation", {"name": "X"})["success"]
    assert not ParametricManagementSkill.normalize_request(
        "equation", {"kind": "dimension", "name": "D1", "value": 10, "unit": "mm"}
    )["success"]
    assert not ParametricManagementSkill.normalize_request(
        "equation", {"expression": '"X" = 10mm; delete', "scope": "all"}
    )["success"]


def test_stage_router_runs_configuration_before_equation() -> None:
    source = _design()
    design = apply_stage_plan(source, infer_stage_plan(source["source_brief"], "model_3d"))
    expected = ["base_plate", "configuration", "equation", "save_sldprt"]
    assert design["execution_policy"]["allowed_skills"] == expected
    assert [step.skill_key for step in SkillPlanner().plan(design)] == expected
    assert [item["skill_key"] for item in SkillRouterAgent().route(design)["skill_pipeline"]] == expected

