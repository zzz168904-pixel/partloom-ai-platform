from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent import AgentPipelineRunner, CADAgentSkillManager
from cad_agent.agents_orchestrator import CADPlannerAgent, SkillRouterAgent


MODEL_ONLY = "\u521b\u5efa\u4e00\u4e2a100\u00d760\u00d710mm\u5e95\u677f\uff0c\u4e2d\u95f4\u5f00\u03c620\u5b54"
MODEL_STEP = "\u521b\u5efa\u4e00\u4e2a100\u00d760\u00d710mm\u5e95\u677f\uff0c\u5e76\u5bfc\u51faSTEP"
CURRENT_MODEL_DRAWING = "\u628a\u5f53\u524d\u6a21\u578b\u751f\u6210\u4e09\u89c6\u56fe\u5de5\u7a0b\u56fe"
CURRENT_DRAWING_EXPORT = "\u628a\u5f53\u524d\u5de5\u7a0b\u56fe\u5bfc\u51faPDF\u548cDWG"
FULL_PIPELINE = "\u5b8c\u6574\u751f\u6210\u96f6\u4ef6\u3001\u5de5\u7a0b\u56fe\u3001\u6807\u6ce8\u5e76\u5bfc\u51fa\u5168\u90e8\u683c\u5f0f"
INCOMPLETE_MODEL = "\u521b\u5efa\u6a21\u578b"
NEGATED_DOWNSTREAM = (
    "\u521b\u5efa\u4e00\u4e2a100\u00d760\u00d710mm\u5e95\u677f\uff0c\u53ea\u751f\u62103D\u96f6\u4ef6\u5e76\u4fdd\u5b58\u4e3aSLDPRT\uff0c"
    "\u4e0d\u751f\u6210\u5de5\u7a0b\u56fe\uff0c\u4e0d\u542f\u52a8AutoCAD\uff0c\u4e0d\u5bfc\u51faSTEP\u3001PDF\u3001DWG\u6216\u5176\u4ed6\u683c\u5f0f\u3002"
)
OUTPUT_SHAFT_MODEL_ONLY = (
    "只生成一个减速箱壳体三维零件，只保存SLDPRT，不生成工程图，不启动AutoCAD，"
    "不导出STEP、PDF或DWG。输入轴承孔直径38mm，输出轴承孔直径58mm；"
    "输入轴承座外径82mm，输出轴承座外径104mm。完成后立即停止，不生成其他文件。"
)


def _plan(prompt: str, mode: str = "auto") -> tuple[dict, dict]:
    design = CADPlannerAgent(ROOT / "logs" / "test_skill_outputs").plan(prompt, stage_mode=mode)
    route = SkillRouterAgent().route(design)
    return design, route


def _skill_keys(route: dict) -> set[str]:
    return {step["skill_key"] for step in route.get("skill_pipeline", [])}


def test_model_only_default_does_not_route_downstream() -> None:
    design, route = _plan(MODEL_ONLY)
    assert design["task_type"] == "model_3d"
    assert design["requested_stages"] == ["model_3d"]
    assert design["stop_after"] == "model_3d"
    keys = _skill_keys(route)
    assert {"base_plate", "save_sldprt"} <= keys
    assert "solidworks_drawing" not in keys
    assert "autocad_annotation" not in keys
    assert "step_export" not in keys
    assert "pdf_export" not in keys
    assert "dwg_export" not in keys


def test_model_plus_step_does_not_route_drawing() -> None:
    design, route = _plan(MODEL_STEP)
    assert design["requested_stages"] == ["model_3d", "export_files"]
    keys = _skill_keys(route)
    assert {"base_plate", "save_sldprt"} <= keys
    assert "step_export" in keys
    assert "solidworks_drawing" not in keys
    assert "autocad_annotation" not in keys
    assert "pdf_export" not in keys


def test_current_model_drawing_only() -> None:
    design, route = _plan(CURRENT_MODEL_DRAWING)
    assert design["task_type"] == "create_drawing"
    assert design["requested_stages"] == ["drawing"]
    assert _skill_keys(route) == {"solidworks_drawing"}


def test_current_drawing_export_only() -> None:
    design, route = _plan(CURRENT_DRAWING_EXPORT)
    assert design["task_type"] == "export_files"
    assert design["requested_stages"] == ["export_files"]
    keys = _skill_keys(route)
    assert "pdf_export" in keys
    assert "dwg_export" in keys
    assert "base_plate" not in keys
    assert "solidworks_drawing" not in keys


def test_full_pipeline_requires_explicit_full_phrase() -> None:
    design, route = _plan(FULL_PIPELINE)
    assert design["task_type"] == "full_pipeline"
    assert design["requested_stages"] == ["model_3d", "drawing", "autocad_annotation", "export_files"]
    keys = _skill_keys(route)
    assert {"base_plate", "solidworks_drawing", "autocad_annotation"} <= keys


def test_incomplete_model_needs_confirmation_and_no_cad_skills() -> None:
    design, route = _plan(INCOMPLETE_MODEL)
    assert design["needs_confirmation"] is True
    assert design["stop_after"] is None
    assert _skill_keys(route) == set()


def test_simulated_pipeline_stops_at_model_stage() -> None:
    output_root = ROOT / "logs" / "test_skill_outputs"
    context = AgentPipelineRunner(output_root, skill_manager=CADAgentSkillManager(output_root), execute_real_skills=False).run(MODEL_ONLY)
    assert context.status == "success"
    assert context.report_path and context.report_path.is_file()
    report = json.loads(context.report_path.read_text(encoding="utf-8"))
    assert report["design_json"]["requested_stages"] == ["model_3d"]
    step_keys = [step["skill_key"] for step in report["skill_pipeline"]]
    assert "base_plate" in step_keys
    assert "solidworks_drawing" not in step_keys
    assert "autocad_annotation" not in step_keys


def test_negated_downstream_stages_are_not_scheduled() -> None:
    design, route = _plan(NEGATED_DOWNSTREAM)
    assert design["requested_stages"] == ["model_3d"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDPRT"]
    keys = _skill_keys(route)
    assert "solidworks_drawing" not in keys
    assert "autocad_annotation" not in keys
    assert "step_export" not in keys
    assert "pdf_export" not in keys
    assert "dwg_export" not in keys


def test_output_shaft_terms_do_not_trigger_file_export() -> None:
    from cad_agent.stage_planner import infer_stage_plan

    plan = infer_stage_plan(OUTPUT_SHAFT_MODEL_ONLY, "auto")
    assert plan["task_type"] == "model_3d"
    assert plan["requested_stages"] == ["model_3d"]
    assert plan["requested_outputs"] == ["SLDPRT"]
    assert plan["stop_after"] == "model_3d"


if __name__ == "__main__":
    test_model_only_default_does_not_route_downstream()
    test_model_plus_step_does_not_route_drawing()
    test_current_model_drawing_only()
    test_current_drawing_export_only()
    test_full_pipeline_requires_explicit_full_phrase()
    test_incomplete_model_needs_confirmation_and_no_cad_skills()
    test_simulated_pipeline_stops_at_model_stage()
    test_negated_downstream_stages_are_not_scheduled()
    print("Stage gating tests passed")
