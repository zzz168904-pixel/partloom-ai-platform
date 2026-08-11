from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from app import PipelineWorker
from cad_agent.agents_orchestrator.agents_runner import AgentsOrchestratorRunner
from cad_agent.agents_orchestrator.pipeline_executor_tool import PipelineExecutorTool
from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.cad_ir import CADIRCompiler
from cad_agent.design_planner import DesignPlanner
from cad_agent.gateway.models import PlanTaskRequest
from cad_agent.gateway.service import GatewayTaskCoordinator
from cad_agent.registry import CADAgentSkillManager
from unified_skill_manager import UnifiedSkillManager


def _invalid_design() -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": "Create a plate with an unspecified hole.",
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "length": 100, "width": 60, "thickness": 10},
        "features": [
            {"name": "Base", "type": "base_plate", "required": True, "params": {"length": 100, "width": 60, "thickness": 10}},
            {"name": "BadHole", "type": "simple_hole", "required": True, "params": {}},
        ],
        "outputs": ["SLDPRT"],
        "requested_stages": ["model_3d"],
        "forbidden_stages": ["drawing", "autocad_annotation", "export_files"],
        "stop_after": "model_3d",
        "unsupported_features": [],
        "risks": [],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def _valid_design() -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": "Create a 100 x 60 x 10 mm plate with one 20 mm center hole.",
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "length": 100, "width": 60, "thickness": 10},
        "features": [
            {"name": "Base", "type": "base_plate", "required": True, "params": {"length": 100, "width": 60, "thickness": 10}},
            {"name": "CenterHole", "type": "center_hole", "required": True, "params": {"hole_diameter": 20}},
        ],
        "outputs": ["SLDPRT"],
        "requested_stages": ["model_3d"],
        "unsupported_features": [],
        "risks": [],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_operation_catalog_is_closed_and_errors_are_structured() -> None:
    design = _valid_design()
    design["features"].append({"name": "Mystery", "type": "magic_cut", "required": True, "params": {}})

    result = CADIRCompiler().compile(design)

    assert not result.success
    error = next(item for item in result.errors if item["code"] == "unsupported_operation")
    assert set(error) == {"code", "message", "feature_id", "field"}
    assert error["feature_id"] == "mystery"


def test_structural_aliases_normalize_and_conflicts_block() -> None:
    design = _valid_design()
    design["features"].append({
        "name": "HolePattern",
        "type": "linear_pattern",
        "required": True,
        "params": {
            "seed_feature": "CenterHole",
            "count": 4,
            "count_1": 5,
            "spacing_1": 20,
            "direction_1": "+X",
        },
    })

    result = CADIRCompiler().compile(design)

    assert not result.success
    assert any(item["code"] == "conflicting_parameter_aliases" and item["field"] == "count" for item in result.errors)


def test_explicit_target_and_dependency_are_canonical_ids() -> None:
    design = _valid_design()
    design["features"][1]["params"]["geometry_context"] = {
        "target_body": "primary_solid",
        "target_feature": "Base",
        "target_face_role": "outer_horizontal_face",
        "face_origin": [0, 0, 10],
        "local_u_axis": [1, 0, 0],
        "local_v_axis": [0, 1, 0],
        "face_normal": [0, 0, 1],
    }

    result = CADIRCompiler().compile(design)

    assert result.success
    hole = result.ir["features"][1]
    assert hole["operation"] == "through_hole"
    assert hole["target"]["feature_ref"] == "base"
    assert hole["dependencies"] == [{"kind": "target_feature", "feature_id": "base"}]


def test_planner_boundary_blocks_invalid_required_parameter(tmp_path: Path) -> None:
    design = DesignPlanner(tmp_path)._normalize_design(_invalid_design(), "Create a plate with a hole.", stage_mode="model_3d")

    assert design["needs_confirmation"]
    assert not design["cad_ir_validation"]["success"]
    assert design["execution_policy"]["allowed_skills"] == []
    assert any(item.get("type") == "cad_ir_validation_error" for item in design["unsupported_features"])


def test_router_boundary_cannot_schedule_invalid_design() -> None:
    route = SkillRouterAgent().route(_invalid_design())

    assert route["skill_pipeline"] == []
    assert route["needs_confirmation"]
    assert not route["cad_ir_validation"]["success"]


@pytest.mark.parametrize(
    "invoke",
    [
        lambda manager, plan, root: manager.run_solidworks_plan(plan),
        lambda manager, plan, root: manager.run_base_plate_plan(plan, root / "base"),
        lambda manager, plan, root: manager.run_production_fillet_plan(plan),
        lambda manager, plan, root: manager.run_active_model_through_hole_plan(plan),
        lambda manager, plan, root: manager.run_active_model_feature_plan(plan, "boss"),
        lambda manager, plan, root: manager.run_active_model_pattern_plan(plan, "linear_pattern"),
        lambda manager, plan, root: manager.run_advanced_feature_plan(plan, "side_boss"),
        lambda manager, plan, root: manager.run_revolve_plan(plan),
        lambda manager, plan, root: manager.run_profile_extrude_plan(plan),
        lambda manager, plan, root: manager.run_gear_plan(plan),
        lambda manager, plan, root: manager.run_gear_pair_plan(plan),
        lambda manager, plan, root: manager.run_sweep_plan(plan),
        lambda manager, plan, root: manager.run_loft_plan(plan),
        lambda manager, plan, root: manager.run_sheet_metal_plan(plan),
        lambda manager, plan, root: manager.run_weldment_plan(plan),
        lambda manager, plan, root: manager.run_freeform_surface_plan(plan),
        lambda manager, plan, root: manager.run_feature_management_plan(plan, "shell"),
        lambda manager, plan, root: manager.run_parametric_management_plan(plan, "configuration"),
        lambda manager, plan, root: manager.run_assembly_mate_plan(plan),
        lambda manager, plan, root: manager.run_thread_plan(plan),
    ],
)
def test_all_direct_skill_manager_production_entries_are_guarded(tmp_path: Path, invoke) -> None:
    result = invoke(CADAgentSkillManager(tmp_path), _invalid_design(), tmp_path)

    assert not result.success
    assert result.message == "cad_ir_validation_failed"
    assert result.data["gate"] == "skill_manager_boundary"


@pytest.mark.parametrize(
    "method_name",
    [
        "run_active_model_boss_plan",
        "run_active_model_pocket_plan",
        "run_active_model_slot_plan",
        "run_active_model_chamfer_plan",
        "run_active_model_linear_pattern_plan",
        "run_active_model_circular_pattern_plan",
        "run_active_model_mirror_plan",
        "run_side_boss_plan",
        "run_side_hole_plan",
        "run_rib_plan",
        "run_shell_plan",
        "run_draft_plan",
        "run_reference_geometry_plan",
        "run_configuration_plan",
        "run_equation_plan",
    ],
)
def test_legacy_production_wrappers_cannot_bypass_cad_ir(
    tmp_path: Path,
    method_name: str,
) -> None:
    manager = CADAgentSkillManager(tmp_path)

    result = getattr(manager, method_name)(_invalid_design())

    assert not result.success
    assert result.message == "cad_ir_validation_failed"
    assert result.data["gate"] == "skill_manager_boundary"


def test_direct_save_and_legacy_templates_are_blocked(tmp_path: Path) -> None:
    manager = CADAgentSkillManager(tmp_path)
    save = manager.save_active_part(tmp_path)
    legacy_cnc = manager.run_fillet_chamfer_plan(_valid_design())
    legacy_manager = UnifiedSkillManager(tmp_path)

    assert save.data["errors"][0]["code"] == "missing_validated_plan"
    assert legacy_cnc.data["error"] == "legacy_test_entry_blocked"
    assert legacy_manager.run_threaded_hole_template("M6 hole")["error"] == "legacy_test_entry_blocked"
    assert legacy_manager.run_cnc_mount_template()["error"] == "legacy_test_entry_blocked"


def test_agents_executor_precomputed_json_hits_pipeline_gate(tmp_path: Path) -> None:
    result = PipelineExecutorTool(tmp_path, execute_real_skills=False).run(
        "invalid precomputed task",
        stage_mode="model_3d",
        design_json=_invalid_design(),
    )

    assert result["status"] == "failed"
    assert result["skill_pipeline"] == []
    assert result["final_model_validation"]["reason"] == "cad_ir_validation_failed"
    assert Path(result["report_path"]).is_file()


def test_agents_orchestrator_precomputed_json_cannot_route_or_execute(tmp_path: Path) -> None:
    result = AgentsOrchestratorRunner(tmp_path, provider_id="local_fallback").run(
        "invalid precomputed task",
        execute_real_skills=False,
        run_pipeline=True,
        stage_mode="model_3d",
        design_json=_invalid_design(),
    )

    assert result["status"] == "failed"
    assert result["needs_confirmation"]
    assert result["skill_pipeline"] == []
    assert result["pipeline_result"]["final_model_validation"]["reason"] == "cad_ir_validation_failed"


def test_gateway_recompiles_planner_output_and_refuses_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "cad_agent.gateway.service.CADPlannerAgent.plan",
        lambda self, prompt, stage_mode="auto": _invalid_design(),
    )
    coordinator = GatewayTaskCoordinator(tmp_path)
    planned = coordinator.plan(PlanTaskRequest(prompt="invalid task", execute_real_skills=False))

    assert planned["status"] == "awaiting_confirmation"
    assert planned["summary"]["needs_confirmation"]
    with pytest.raises(RuntimeError, match="requires|requires a revised plan|diameter"):
        coordinator.confirm(planned["task_id"])


def test_gui_worker_precomputed_json_cannot_report_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.LOG_DIR", tmp_path)
    results: list[tuple[bool, str, dict]] = []
    worker = PipelineWorker(
        "invalid GUI task",
        execute_real_skills=False,
        stage_mode="model_3d",
        provider_id="local_fallback",
        design_json=_invalid_design(),
    )
    worker.result.connect(lambda ok, message, data: results.append((ok, message, data)))

    worker.run()

    assert results
    ok, _message, data = results[-1]
    assert not ok
    assert data["status"] == "failed"
    assert Path(data["report_path"]).is_file()
