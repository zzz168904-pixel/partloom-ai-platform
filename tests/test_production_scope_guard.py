from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager
from cad_agent.agents_orchestrator import CADPlannerAgent, SkillRouterAgent
from cad_agent.brain_models import BrainPlan, PlannedSkillStep
from cad_agent.pipeline import PipelineContext, PipelineExecutor
from cad_agent.pipeline.pipeline_logger import PipelineLogger


FULL_MODEL = (
    "\u521b\u5efa\u4e00\u4e2a\u957f100mm\u3001\u5bbd60mm\u3001\u539a10mm\u7684\u77e9\u5f62\u5b89\u88c5\u677f\uff0c"
    "\u56db\u89d2R5\u5706\u89d2\uff0c\u4e2d\u5fc3\u5f00\u4e00\u4e2a\u03c620\u8d2f\u7a7f\u5b54\u3002"
    "\u6750\u6599\u4e3a6061\u94dd\uff0c\u53ea\u751f\u62103D\u96f6\u4ef6\u3002"
)


def _plan(prompt: str, mode: str = "auto") -> tuple[dict, dict]:
    design = CADPlannerAgent(ROOT / "logs" / "production_scope_tests").plan(prompt, stage_mode=mode)
    return design, SkillRouterAgent().route(design)


def _keys(route: dict) -> list[str]:
    return [item["skill_key"] for item in route["skill_pipeline"]]


def test_1_model_only_is_minimal() -> None:
    design, route = _plan(FULL_MODEL)
    assert design["execution_policy"]["allowed_skills"] == ["base_plate", "fillet", "through_hole", "save_sldprt"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDPRT"]
    assert _keys(route) == ["base_plate", "fillet", "through_hole", "save_sldprt"]
    assert "solidworks_cnc_fillet" not in json.dumps(route)
    assert "STEP" not in design["execution_policy"]["expected_outputs"]


def test_2_base_only_saves_sldprt_only() -> None:
    design, route = _plan("\u521b\u5efa\u4e00\u4e2a100\u00d760\u00d710mm\u77e9\u5f62\u677f\uff0c\u53ea\u4fdd\u5b58SLDPRT")
    assert design["execution_policy"]["allowed_skills"] == ["base_plate", "save_sldprt"]
    assert _keys(route) == ["base_plate", "save_sldprt"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDPRT"]


def test_3_modify_current_model_only_routes_fillet() -> None:
    design, route = _plan("\u5728\u5f53\u524d\u6a21\u578b\u4e0a\u53ea\u589e\u52a0R3\u5706\u89d2", mode="modify_3d")
    assert design["task_type"] == "modify_3d"
    assert design["execution_policy"]["allowed_skills"] == ["fillet"]
    assert _keys(route) == ["fillet"]
    assert design["execution_policy"]["expected_outputs"] == []


def test_4_step_requires_explicit_request() -> None:
    design, route = _plan("\u521b\u5efa\u4e00\u4e2a100\u00d760\u00d710mm\u5e95\u677f\uff0c\u660e\u786e\u5bfc\u51faSTEP")
    assert design["execution_policy"]["allowed_skills"] == ["base_plate", "save_sldprt", "step_export"]
    assert _keys(route) == ["base_plate", "save_sldprt", "step_export"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDPRT", "STEP"]


def test_5_cnc_injection_is_blocked() -> None:
    design, _route = _plan(FULL_MODEL)
    run_dir = ROOT / "logs" / "production_scope_tests" / "injected_cnc_guard"
    run_dir.mkdir(parents=True, exist_ok=True)
    context = PipelineContext(prompt="injected cnc", output_root=run_dir.parent, run_dir=run_dir)
    context.design_json = design
    context.brain_plan = BrainPlan(
        source_prompt=context.prompt,
        design_json=design,
        skill_pipeline=[PlannedSkillStep("solidworks_cnc_fillet", "apply_fillet_chamfer", "injected test", {}, True)],
        model_provider="test",
    )
    context.log_path = run_dir / "pipeline.log.jsonl"
    executor = PipelineExecutor(CADAgentSkillManager(run_dir.parent), PipelineLogger(context.log_path), execute_real_skills=False)
    executor.execute(context)
    assert context.status == "failed"
    assert context.step_records[0].error and "blocked_by_allowed_skills_guard" in context.step_records[0].error
    assert "blocked_by_allowed_skills_guard" in context.log_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    test_1_model_only_is_minimal()
    test_2_base_only_saves_sldprt_only()
    test_3_modify_current_model_only_routes_fillet()
    test_4_step_requires_explicit_request()
    test_5_cnc_injection_is_blocked()
    print("Production scope guard tests passed")
