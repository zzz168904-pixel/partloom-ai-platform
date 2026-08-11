from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.agents_orchestrator.agents_runner import AgentsOrchestratorRunner
from cad_agent.design_planner import DesignPlanner


DIRECT_WIRE_MESH = """GUI_CAD_COMMAND_V1
创建一个弧形钢丝网片多实体焊件，单位为毫米，材料为碳钢。圆钢轮廓为D4，钢丝直径4mm。
纵向钢丝6根。横向圆弧钢丝7根。两端纵向钢丝总长338mm，其余4根纵向钢丝长度310mm。
网片有效高度310mm。横丝中心距从上到下为45,54,54,54,54,45mm。
每根横向钢丝为连续真圆弧，R269是内表面半径，中心线R271，外表面R273，R273外表面圆弧总长276.4mm，外表面弦长264.75mm，圆弧带轮廓深度37.7mm。网片总深40.3mm。
侧视图中6根纵向钢丝相邻中心弦距从左到右为54.38,55.1,55.1,55.1,54.38mm，纵丝中心在R275轨迹上。层叠关系为横向圆弧钢丝在上层，纵向直钢丝在下层，两层中心距4mm并相切，标准顶视图为下凹U形。
本次只生成上述D4弧形钢丝网片，禁止附加扁铁、卡扣或其他非钢丝构件。只保存SLDPRT，不创建工程图，不启动AutoCAD，不导出其他格式。
"""


class FailingProvider:
    name = "must_not_be_called"

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("Direct GUI commands must bypass the configured LLM provider.")


def test_direct_gui_wire_mesh_bypasses_llm_and_routes_production(tmp_path) -> None:
    design = DesignPlanner(tmp_path, provider=FailingProvider()).plan(DIRECT_WIRE_MESH, stage_mode="model_3d")

    assert design["planning_mode"] == "direct_gui_command"
    assert design["llm_provider"]["id"] == "direct_gui_command"
    assert design["needs_confirmation"] is False
    assert design["unsupported_features"] == []
    assert [feature["type"] for feature in design["features"]] == ["weldment"]
    mesh = design["features"][0]["params"]["wire_mesh"]
    assert mesh["longitudinal_count"] == 6
    assert mesh["transverse_count"] == 7
    assert mesh["longitudinal_lengths_mm"] == [338.0, 310.0, 310.0, 310.0, 310.0, 338.0]
    assert mesh["end_pitch_mm"] == 45.0
    assert mesh["middle_pitch_mm"] == 54.0
    assert mesh["layering"] == "transverse_arc_wires_on_top"
    assert mesh["layer_reference"] == "drawing_side_profile"
    assert mesh["inner_surface_radius_mm"] == 269.0
    assert mesh["transverse_centerline_radius_mm"] == 271.0
    assert mesh["outer_surface_radius_mm"] == 273.0
    assert mesh["longitudinal_center_locus_radius_mm"] == 275.0
    assert mesh["transverse_layer_radius_mm"] == 271.0
    assert mesh["longitudinal_layer_radius_mm"] == 275.0
    assert mesh["longitudinal_chord_pitches_mm"] == [54.38, 55.1, 55.1, 55.1, 54.38]
    assert mesh["outer_surface_chord_mm"] == 264.75
    assert mesh["transverse_profile_depth_mm"] == 37.7
    assert design["execution_policy"]["allowed_skills"] == ["weldment", "save_sldprt"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDPRT"]
    assert not any("flat_bar" in str(item) or "clip" in str(item) for item in design["risks"])


def test_direct_gui_wire_mesh_missing_parameter_is_blocked_without_llm(tmp_path) -> None:
    prompt = DIRECT_WIRE_MESH.replace("网片总深40.3mm。", "")
    design = DesignPlanner(tmp_path, provider=FailingProvider()).plan(prompt, stage_mode="model_3d")

    assert design["needs_confirmation"] is True
    assert design["execution_policy"]["allowed_skills"] == []
    assert any(
        item.get("type") == "direct_gui_command_parameters_missing"
        and "overall_depth_mm" in item.get("missing_parameters", [])
        for item in design["unsupported_features"]
    )


def test_agents_report_effective_direct_planning_provider(tmp_path) -> None:
    report = AgentsOrchestratorRunner(tmp_path, provider_id="local_fallback").run(
        DIRECT_WIRE_MESH,
        execute_real_skills=False,
        run_pipeline=False,
        stage_mode="model_3d",
    )

    assert report["orchestrator"]["provider"] == "local_fallback"
    assert report["orchestrator"]["planning_provider"] == "direct_gui_command"
    assert report["design_json"]["planner_agent"]["provider"] == "direct_gui_command"
    assert [step["skill_key"] for step in report["skill_pipeline"]] == ["weldment", "save_sldprt"]
