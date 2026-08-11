from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.agents_orchestrator import AgentsOrchestratorConfig, AgentsOrchestratorRunner, CADPlannerAgent, PipelineExecutorTool, SkillRouterAgent


PROMPT = """设计一个机械设备安装底座。
整体尺寸为长180mm、宽120mm、厚18mm，材料6061铝。
底板四角R8圆角。
四个角各有一个Φ8.5通孔，孔中心距离相邻两边各15mm。
中心有一个Φ40贯穿孔。
中心孔周围沿PCD80均布6个M6螺纹孔。
底板上表面有一个长100mm、宽55mm、高20mm的矩形凸台，凸台位于中心。
凸台顶部开一个长60mm、宽25mm、深10mm的矩形型腔，型腔四角R3。
底板左右两侧各有一个长45mm、宽12mm的腰型槽，槽中心距离底板中心线左右各45mm。
所有外边C1倒角。
自动生成SolidWorks零件模型。
自动生成工程图，包含前视图、俯视图、右视图、等轴测图。
AutoCAD 自动标注外形尺寸、厚度尺寸、中心孔Φ40、四角孔Φ8.5和孔定位尺寸、PCD80、6×M6螺纹孔、凸台尺寸、型腔尺寸、腰型槽尺寸、圆角R8/R3、倒角C1。
导出SLDPRT、SLDDRW、STEP、DWG、Annotated DWG、PDF、brain_plan.json、design_plan.json、pipeline_report.json、annotation_report.json。
"""


def test_planner_recognizes_complex_mounting_base() -> None:
    design = CADPlannerAgent(ROOT / "logs" / "test_skill_outputs").plan(PROMPT)
    feature_types = {item["type"] for item in design["features"]}
    expected = {
        "base_plate",
        "through_hole",
        "boss",
        "pocket",
        "fillet",
        "chamfer",
    }
    assert expected <= feature_types
    assert design["parameters"]["length"] == 180.0
    assert design["parameters"]["width"] == 120.0
    assert design["parameters"]["thickness"] == 18.0
    assert design["parameter_details"]["length"]["source_text"]
    assert design["parameter_details"]["length"]["confidence"] >= 0.9
    unsupported_types = {item["type"] for item in design["unsupported_features"]}
    assert {"threaded_hole", "bolt_circle_pattern"} <= unsupported_types
    assert not {"boss", "pocket", "slot", "chamfer"} & unsupported_types
    assert design["needs_confirmation"] is True
    assert design["execution_policy"]["unexecutable_required_features"]
    for required in ("parameters", "features", "outputs", "unsupported_features", "risks"):
        assert required in design


def test_router_outputs_expected_skill_pipeline() -> None:
    design = CADPlannerAgent(ROOT / "logs" / "test_skill_outputs").plan(PROMPT)
    routed = SkillRouterAgent().route(design)
    assert routed["skill_pipeline"] == []
    unsupported_types = {item["type"] for item in routed["unsupported_features"]}
    assert {"threaded_hole", "bolt_circle_pattern"} <= unsupported_types
    assert not {"through_hole", "boss", "pocket", "slot", "chamfer"} & unsupported_types


def test_agents_runner_calls_existing_pipeline_in_simulation() -> None:
    output_root = ROOT / "logs" / "test_skill_outputs"
    report = AgentsOrchestratorRunner(output_root, provider_id="local_fallback").run(PROMPT, execute_real_skills=False, run_pipeline=True)
    assert report["orchestrator"]["provider"] == "local_fallback"
    assert report["agent_statuses"]["planner"] == "success"
    assert report["agent_statuses"]["router"] == "failed"
    assert report["agent_statuses"]["executor"] == "failed"
    assert report["agent_statuses"]["validator"] == "failed"
    assert report["agent_statuses"]["recovery"] == "success"
    artifacts = report["artifacts"]
    assert Path(artifacts["agents_orchestrator_report"]).is_file()
    assert Path(artifacts["agents_design_plan"]).is_file()
    assert Path(artifacts["agents_skill_pipeline"]).is_file()
    pipeline_report = artifacts.get("pipeline_report")
    assert pipeline_report and Path(pipeline_report).is_file()
    assert report["pipeline_result"]["executor_tool"]["called_existing_runner"] is True
    assert report["status"] == "failed"
    assert report["pipeline_result"]["status"] == "failed"
    assert report["pipeline_result"]["executor_tool"]["precomputed_design_json_injected"] is True
    assert [item["type"] for item in report["pipeline_result"]["design_json"]["features"]] == [
        item["type"] for item in report["design_json"]["features"]
    ]
    assert report["validation"]["status"] == "failed"
    assert report["validation"]["checks"]["pipeline_report"]["size_gt_zero"] is True
    assert report["validation"]["checks"]["pipeline_report"]["json_parseable"] is True
    assert report["recovery"]["suggestions"]


def test_agents_sdk_installation_and_tool_wrapper() -> None:
    config = AgentsOrchestratorConfig.detect()
    assert config.provider_name in {"openai_agents_sdk", "deepseek", "local_fallback"}
    if config.sdk_available:
        import agents

        assert getattr(agents, "__version__", None)
        assert PipelineExecutorTool(ROOT / "logs" / "test_skill_outputs").as_agents_sdk_tool() is not None


if __name__ == "__main__":
    test_planner_recognizes_complex_mounting_base()
    test_router_outputs_expected_skill_pipeline()
    test_agents_runner_calls_existing_pipeline_in_simulation()
    test_agents_sdk_installation_and_tool_wrapper()
    print("Agents orchestrator tests passed")
