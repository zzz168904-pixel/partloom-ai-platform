from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.agents_orchestrator.agents_runner import AgentsOrchestratorRunner
from cad_agent.agents_orchestrator.cad_planner_agent import CADPlannerAgent
from cad_agent.agents_orchestrator.pipeline_executor_tool import PipelineExecutorTool
from cad_agent.planner_candidate import CANDIDATE_SCHEMA_VERSION
from cad_agent.planner_validator import PlannerValidator
from cad_agent.planning_regression_store import PlanningRegressionStore
from cad_agent.provider_registry import DeepSeekProvider
from cad_agent.registry import CADAgentSkillManager
from cad_agent.skill_planner import SkillPlanner


class ScriptedCandidateProvider:
    name = "deepseek"
    model = "deepseek-test"

    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def generate_design_json(self, prompt: str, schema: dict) -> dict:
        raise AssertionError("candidate providers must not use generate_design_json")

    def generate_candidate_output(
        self,
        prompt: str,
        schema: dict,
        repair_feedback: str | None = None,
    ) -> object:
        self.calls.append({"prompt": prompt, "schema": schema, "repair_feedback": repair_feedback})
        return self.outputs.pop(0)


def _feature(
    feature_id: str,
    operation: str,
    parameters: dict,
    *,
    depends_on: list[str] | None = None,
    target_reference: dict | None = None,
    confidence: float = 0.96,
    assumptions: list[str] | None = None,
    unresolved: list[str] | None = None,
) -> dict:
    return {
        "id": feature_id,
        "operation": operation,
        "depends_on": list(depends_on or []),
        "target_body": "body_01",
        "target_reference": target_reference,
        "parameters": parameters,
        "evidence": [f"user requested {feature_id}"],
        "assumptions": list(assumptions or []),
        "unresolved": list(unresolved or []),
        "confidence": confidence,
    }


def _candidate(
    features: list[dict],
    *,
    confidence: float = 0.96,
    assumptions: list[str] | None = None,
    unresolved: list[str] | None = None,
    part_type: str = "mechanical_part",
) -> dict:
    return {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "part_type": part_type,
        "task_type": "model_3d",
        "parameters": {"unit": "mm"},
        "features": features,
        "outputs": ["SLDPRT"],
        "assumptions": list(assumptions or []),
        "unresolved": list(unresolved or []),
        "confidence": confidence,
    }


def _base(confidence: float = 0.96, parameters: dict | None = None) -> dict:
    return _feature(
        "base_01",
        "base_plate",
        parameters or {"length_mm": 100, "width_mm": 60, "thickness_mm": 10},
        confidence=confidence,
    )


def _plan(tmp_path: Path, candidate: object, prompt: str = "创建100x60x10毫米底板，只保存SLDPRT。") -> tuple[dict, ScriptedCandidateProvider]:
    provider = ScriptedCandidateProvider([candidate])
    design = CADPlannerAgent(tmp_path, provider=provider).plan(prompt, stage_mode="model_3d")
    return design, provider


def test_plain_extruded_plate_becomes_valid_base_operation(tmp_path: Path) -> None:
    design, provider = _plan(tmp_path, _candidate([_base()], part_type="extruded_plate"))

    assert len(provider.calls) == 1
    assert [item["type"] for item in design["features"]] == ["base_plate"]
    assert design["cad_ir"]["features"][0]["parameters"] == {
        "length_mm": 100.0,
        "width_mm": 60.0,
        "thickness_mm": 10.0,
    }
    assert design["planning_validation"]["status"] == "approved"


def test_base_profile_extrude_does_not_require_a_target_reference(tmp_path: Path) -> None:
    profile = _feature(
        "hex_base",
        "profile_extrude",
        {
            "body_operation": "base",
            "sketch_plane": "front_plane",
            "profiles": [{
                "points": [[8.1, 0], [4.05, 7.0148], [-4.05, 7.0148], [-8.1, 0], [-4.05, -7.0148], [4.05, -7.0148]],
                "closed": True,
            }],
            "depth_mm": 6,
            "reverse_direction": False,
            "merge_result": False,
        },
        target_reference=None,
    )
    design, _provider = _plan(tmp_path, _candidate([profile], part_type="screw_plug"))

    assert design["planning_validation"]["status"] == "approved"
    assert not any(
        item["code"] == "target_reference_missing"
        for item in design["planning_validation"]["errors"]
    )


def test_deepseek_style_revolve_aliases_compile_to_the_production_executor(tmp_path: Path) -> None:
    profile = _feature(
        "hex_base",
        "profile_extrude",
        {
            "body_operation": "base",
            "sketch_plane": "front_plane",
            "profiles": [{
                "points": [[8.1, 0], [4.05, 7.0148], [-4.05, 7.0148], [-8.1, 0], [-4.05, -7.0148], [4.05, -7.0148]],
                "closed": True,
            }],
            "depth_mm": 6,
        },
        target_reference=None,
    )
    relief = _feature(
        "rear_relief",
        "revolve",
        {
            "mode": "cut",
            "sketch_plane": "right_plane",
            "axis": "local_horizontal_axis",
            "angle_deg": 360,
            "profile_points": [[0, 7], [0, 8.1], [0.635085296, 8.1], [0, 7]],
        },
        depends_on=["hex_base"],
    )
    stem = _feature(
        "stepped_stem",
        "revolve",
        {
            "body_operation": "boss",
            "sketch_plane": "right_plane",
            "axis": "local_horizontal",
            "angle_deg": 360,
            "profile_points": [[6, 0], [6, 7], [7, 7], [7, 3.5], [9, 3.5], [9, 4], [14, 4], [15, 3], [15, 0], [6, 0]],
        },
        depends_on=["rear_relief"],
    )
    design, _provider = _plan(
        tmp_path,
        _candidate([profile, relief, stem], part_type="screw_plug"),
        prompt="Create the specified screw plug and save only SLDPRT.",
    )

    assert design["planning_validation"]["status"] == "approved"
    assert design["execution_policy"]["unexecutable_required_features"] == []
    assert set(design["execution_policy"]["allowed_skills"]) == {
        "profile_extrude",
        "revolve",
        "save_sldprt",
    }
    assert [step.skill_key for step in SkillPlanner().plan(design)] == [
        "profile_extrude",
        "revolve",
        "save_sldprt",
    ]
    relief_params = design["features"][1]["params"]
    assert relief_params["body_operation"] == "cut"
    assert relief_params["execution_mode"] == "active_model"
    assert relief_params["operation"] == "cut"
    assert relief_params["mode"] == "active_model"
    assert relief_params["sketch_plane"] == "right"
    assert relief_params["axis"] == "horizontal"


def test_sheet_metal_base_and_ninety_degree_flange_are_preserved(tmp_path: Path) -> None:
    sheet = _feature(
        "sheet_01",
        "sheet_metal",
        {
            "mode": "new_model",
            "length_mm": 120,
            "width_mm": 80,
            "thickness_mm": 2,
            "bend_radius_mm": 2,
            "edge_flanges": [{"edge": "width_positive", "height_mm": 25, "angle_deg": 90}],
        },
    )
    design, _provider = _plan(tmp_path, _candidate([sheet], part_type="sheet_metal_bracket"))

    assert design["planning_validation"]["status"] == "approved"
    assert design["features"][0]["type"] == "sheet_metal"
    assert design["features"][0]["params"]["edge_flanges"][0]["angle_deg"] == 90


def test_through_hole_alias_never_becomes_threaded_hole(tmp_path: Path) -> None:
    hole = _feature(
        "hole_01",
        "simple_hole",
        {"diameter_mm": 12, "position": "center", "extent": "through_all"},
        depends_on=["base_01"],
        target_reference={"feature_id": "base_01", "role": "outer_planar_face"},
    )
    design, _provider = _plan(tmp_path, _candidate([_base(), hole]))

    assert design["features"][1]["type"] == "through_hole"
    assert all(item["type"] != "threaded_hole" for item in design["features"])


def test_missing_hole_position_requires_revised_input_without_retry(tmp_path: Path) -> None:
    hole = _feature(
        "hole_01",
        "through_hole",
        {"diameter_mm": 12, "extent": "through_all"},
        depends_on=["base_01"],
        target_reference={"feature_id": "base_01", "role": "outer_planar_face"},
        unresolved=["hole center local UV coordinates are missing"],
    )
    design, provider = _plan(tmp_path, _candidate([_base(), hole]))

    assert len(provider.calls) == 1
    assert design["needs_confirmation"] is True
    assert design["planning_validation"]["status"] == "blocked"
    assert design["planning_validation"]["confirmation_allowed"] is False
    assert {item["code"] for item in design["planning_validation"]["errors"]} >= {
        "hole_position_unresolved",
        "unresolved_information",
    }


def test_unsupported_operation_gets_one_replan_then_stops(tmp_path: Path) -> None:
    bad = _candidate([_feature("mystery_01", "magic_cut", {"depth_mm": 3})])
    provider = ScriptedCandidateProvider([bad, bad])
    design = CADPlannerAgent(tmp_path, provider=provider).plan("创建一个魔法切口。", stage_mode="model_3d")

    assert len(provider.calls) == 2
    assert provider.calls[1]["repair_feedback"]
    assert design["planning_retry"] == {
        "attempt_count": 2,
        "retry_used": True,
        "reason": "unsupported_operation",
    }
    assert design["planning_validation"]["status"] == "blocked"
    assert design["execution_policy"]["allowed_skills"] == []


def test_mixed_linear_units_are_normalized_to_millimetres(tmp_path: Path) -> None:
    mixed = _base(parameters={
        "length_mm": {"value": 10, "unit": "cm"},
        "width_mm": {"value": 2, "unit": "in"},
        "thickness_mm": {"value": 0.01, "unit": "m"},
    })
    design, _provider = _plan(tmp_path, _candidate([mixed]))
    params = design["cad_ir"]["features"][0]["parameters"]

    assert params == {"length_mm": 100.0, "width_mm": 50.8, "thickness_mm": 10.0}
    assert design["planning_validation"]["unit_system"] == {"linear": "mm", "angle": "deg"}


def test_dependency_order_error_is_deterministically_blocked(tmp_path: Path) -> None:
    hole = _feature(
        "hole_01",
        "through_hole",
        {"diameter_mm": 12, "position": "center"},
        depends_on=["base_01"],
        target_reference={"feature_id": "base_01", "role": "outer_planar_face"},
    )
    design, _provider = _plan(tmp_path, _candidate([hole, _base()]))

    codes = {item["code"] for item in design["planning_validation"]["errors"]}
    assert "dependency_order_invalid" in codes
    assert "target_reference_order_invalid" in codes
    assert not PlannerValidator.execution_gate(design)["success"]


def test_non_json_output_is_repaired_at_most_once(tmp_path: Path) -> None:
    provider = ScriptedCandidateProvider(["I will build the part now.", _candidate([_base()])])
    design = CADPlannerAgent(tmp_path, provider=provider).plan("创建底板。", stage_mode="model_3d")

    assert len(provider.calls) == 2
    assert provider.calls[1]["repair_feedback"].startswith("The previous response was invalid JSON")
    assert design["planning_retry"]["reason"] == "json_format_error"
    assert design["planning_validation"]["status"] == "approved"


def test_schema_error_is_repaired_once_without_guessing_geometry(tmp_path: Path) -> None:
    malformed = _candidate([_base()])
    malformed.pop("candidate_schema_version")
    provider = ScriptedCandidateProvider([malformed, _candidate([_base()])])
    design = CADPlannerAgent(tmp_path, provider=provider).plan("创建底板。", stage_mode="model_3d")

    assert len(provider.calls) == 2
    assert "candidate_schema_version" in provider.calls[1]["repair_feedback"]
    assert design["planning_retry"]["reason"] == "json_schema_error"
    assert design["planning_validation"]["status"] == "approved"


def test_blocked_candidate_never_constructs_pipeline_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hole = _feature(
        "hole_01",
        "through_hole",
        {"diameter_mm": 12},
        depends_on=["base_01"],
        target_reference={"feature_id": "base_01", "role": "outer_planar_face"},
        unresolved=["hole position is missing"],
    )
    design, _provider = _plan(tmp_path / "planner", _candidate([_base(), hole]))

    def forbidden_executor(*args, **kwargs):
        raise AssertionError("PipelineExecutorTool must not be constructed")

    monkeypatch.setattr("cad_agent.agents_orchestrator.agents_runner.PipelineExecutorTool", forbidden_executor)
    report = AgentsOrchestratorRunner(tmp_path / "runner", provider_id="local_fallback").run(
        "blocked candidate",
        execute_real_skills=False,
        run_pipeline=True,
        stage_mode="model_3d",
        design_json=design,
    )

    assert report["status"] == "failed"
    assert report["agent_statuses"]["executor"] == "blocked"
    assert report["pipeline_result"]["final_model_validation"]["reason"] == "planner_validation_failed"


def test_medium_confidence_requires_confirmation_then_dry_pipeline_runs(tmp_path: Path) -> None:
    medium = _candidate([_base(confidence=0.82)], confidence=0.82)
    design, _provider = _plan(tmp_path / "planner", medium)

    assert design["planning_validation"]["status"] == "needs_confirmation"
    assert not PlannerValidator.execution_gate(design)["success"]
    authorized = PlannerValidator.authorize_after_confirmation(design)
    assert PlannerValidator.execution_gate(authorized)["success"]
    assert authorized["execution_policy"]["allowed_skills"] == ["base_plate", "save_sldprt"]

    result = PipelineExecutorTool(tmp_path / "pipeline", execute_real_skills=False).run(
        "创建100x60x10毫米底板，只保存SLDPRT。",
        stage_mode="model_3d",
        design_json=authorized,
    )
    assert result["status"] == "success"
    assert [item["skill_key"] for item in result["skill_pipeline"]] == ["base_plate", "save_sldprt"]


def test_skill_manager_rejects_unauthorized_candidate_before_cad_ir_execution(tmp_path: Path) -> None:
    design, _provider = _plan(tmp_path / "planner", _candidate([_base()], confidence=0.65))
    result = CADAgentSkillManager(tmp_path / "skills").run_base_plate_plan(design, tmp_path / "run")

    assert not result.success
    assert result.message == "planner_validation_failed"
    assert result.data["gate"] == "skill_manager_planner_boundary"


def test_regression_case_contains_the_complete_audit_contract(tmp_path: Path) -> None:
    store = PlanningRegressionStore(tmp_path)
    path = store.record(
        original_prompt="bad plan",
        provider="deepseek",
        raw_llm_output="not json",
        normalized_cad_ir={"version": "cad.ir.v1"},
        validation_errors=[{"code": "planner_output_invalid_json"}],
        user_correction="use base_plate",
        final_correct_cad_ir={"features": []},
        execution_result={"status": "not_run"},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert {
        "original_prompt",
        "provider",
        "raw_llm_output",
        "normalized_cad_ir",
        "validation_errors",
        "user_correction",
        "final_correct_cad_ir",
        "execution_result",
    } <= set(payload)


def test_deepseek_candidate_provider_makes_one_planning_call_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_candidate([_base()]))))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    provider = DeepSeekProvider("unit-test-secret", "https://example.invalid", "deepseek-test")
    output = provider.generate_candidate_output(
        "创建底板",
        {"operations": ["base_plate"], "operation_contracts": {}},
    )

    assert json.loads(output)["features"][0]["operation"] == "base_plate"
    assert len(calls) == 1
    assert "tools" not in calls[0]
    assert "tool_choice" not in calls[0]
    assert "cannot call tools" in calls[0]["messages"][0]["content"]
    assert "never call tools" in calls[0]["messages"][1]["content"]
    assert "parameters.body_operation" in calls[0]["messages"][1]["content"]
    assert "parameters.execution_mode" in calls[0]["messages"][1]["content"]
