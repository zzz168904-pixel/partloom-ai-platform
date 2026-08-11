from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.agents_orchestrator.agents_runner import AgentsOrchestratorRunner
from cad_agent.direct_cad_ir import DirectCADIRInputError, DirectCADIRService
from cad_agent.skill_planner import SkillPlanner


def _plate_design() -> dict:
    return {
        "schema_version": "vibecad.design.v1",
        "task_type": "model_3d",
        "part_family": "plate",
        "parameters": {"unit": "mm"},
        "features": [
            {
                "name": "Base",
                "type": "base_plate",
                "required": True,
                "params": {"length_mm": 100, "width_mm": 60, "thickness_mm": 10},
            },
            {
                "name": "CenterHole",
                "type": "through_hole",
                "required": True,
                "depends_on": ["Base"],
                "target_body": "primary_solid",
                "target_reference": {"feature_id": "Base", "role": "outer_horizontal_face"},
                "params": {"diameter_mm": 20, "position": "center"},
            },
        ],
        "outputs": ["SLDPRT"],
    }


def test_direct_design_json_uses_no_llm_and_is_approved(tmp_path: Path) -> None:
    plan = DirectCADIRService(tmp_path).plan(json.dumps(_plate_design()), stage_mode="auto")

    assert plan["planning_mode"] == "direct_cad_ir"
    assert plan["llm_provider"] == {
        "id": "direct_cad_ir",
        "model": "deterministic",
        "role": "llm_disabled",
        "enabled": False,
    }
    assert plan["planning_validation"]["status"] == "approved"
    assert plan["planning_validation"]["allow_pipeline"] is True
    assert plan["execution_policy"]["allowed_skills"] == [
        "base_plate",
        "through_hole",
        "save_sldprt",
    ]
    assert plan["execution_policy"]["expected_outputs"] == ["SLDPRT"]


def test_canonical_cad_ir_v1_is_converted_without_provider(tmp_path: Path) -> None:
    payload = {
        "version": "cad.ir.v1",
        "unit_system": "mm",
        "task_type": "model_3d",
        "outputs": ["SLDPRT"],
        "features": [
            {
                "id": "base",
                "name": "BasePlateDisplayName",
                "operation": "base_plate",
                "required": True,
                "parameters": {"length_mm": 100, "width_mm": 60, "thickness_mm": 10},
                "options": {},
                "target": {"resolution": "new_body", "body_ref": "primary_solid"},
                "dependencies": [],
            },
            {
                "id": "hole",
                "name": "Hole",
                "operation": "through_hole",
                "required": True,
                "parameters": {"diameter_mm": 20, "count": 1},
                "options": {"position": "center"},
                "target": {
                    "resolution": "semantic_reference",
                    "body_ref": "primary_solid",
                    "feature_ref": "base",
                    "face_role": "outer_horizontal_face",
                },
                "dependencies": [{"kind": "feature", "feature_id": "base"}],
            },
        ],
    }

    plan = DirectCADIRService(tmp_path).plan(json.dumps(payload), stage_mode="auto")

    assert plan["cad_ir"]["version"] == "cad.ir.v1"
    assert plan["cad_ir_validation"]["success"] is True
    assert [item["type"] for item in plan["features"]] == ["base_plate", "through_hole"]
    assert plan["planning_validation"]["unresolved"] == []


def test_canonical_cad_ir_preserves_scope_gaps_and_blocks_pipeline(
    tmp_path: Path,
) -> None:
    payload = {
        "version": "cad.ir.v1",
        "unit_system": "mm",
        "task_type": "model_3d",
        "outputs": ["SLDPRT"],
        "features": [
            {
                "id": "base",
                "operation": "base_plate",
                "required": True,
                "parameters": {
                    "length_mm": 100,
                    "width_mm": 60,
                    "thickness_mm": 10,
                },
                "options": {},
                "target": {"resolution": "new_body", "body_ref": "primary_solid"},
                "dependencies": [],
            }
        ],
        "unsupported_features": [
            {
                "type": "dome",
                "required": True,
                "reason": "No production executor is registered.",
            }
        ],
        "assumptions": ["A source face has not been resolved."],
        "unresolved": ["Dome height is known but its target face is unresolved."],
    }

    plan = DirectCADIRService(tmp_path).plan(
        json.dumps(payload),
        stage_mode="model_3d",
    )

    assert plan["planning_validation"]["status"] == "blocked"
    assert plan["planning_validation"]["allow_pipeline"] is False
    assert plan["execution_policy"]["allowed_skills"] == []
    assert plan["unsupported_features"] == payload["unsupported_features"]
    assert plan["assumptions"] == payload["assumptions"]
    assert plan["unresolved"] == payload["unresolved"]


def test_screw_plug_revolve_plan_no_longer_depends_on_deepseek_notes(tmp_path: Path) -> None:
    payload = {
        "schema_version": "vibecad.design.v1",
        "task_type": "model_3d",
        "part_family": "screw_plug",
        "parameters": {"unit": "mm"},
        "features": [
            {
                "name": "HexBody",
                "type": "profile_extrude",
                "params": {
                    "body_operation": "base",
                    "execution_mode": "new_model",
                    "sketch_plane": "front",
                    "profiles": [{
                        "points": [[8.1, 0], [4.05, 7.014805771], [-4.05, 7.014805771],
                                   [-8.1, 0], [-4.05, -7.014805771], [4.05, -7.014805771]],
                        "closed": True,
                    }],
                    "depth_mm": 6,
                    "end_condition": "blind",
                    "reverse_direction": False,
                    "merge_result": False,
                },
            },
            {
                "name": "BackRelief",
                "type": "revolve",
                "depends_on": ["HexBody"],
                "params": {
                    "body_operation": "cut",
                    "execution_mode": "active_model",
                    "sketch_plane": "right",
                    "axis": "horizontal",
                    "angle_deg": 360,
                    "profile_points": [[0, 7], [0, 8.1], [0.635085296, 8.1], [0, 7]],
                },
            },
            {
                "name": "ThreadBoss",
                "type": "revolve",
                "depends_on": ["BackRelief"],
                "params": {
                    "body_operation": "boss",
                    "execution_mode": "active_model",
                    "sketch_plane": "right",
                    "axis": "horizontal",
                    "angle_deg": 360,
                    "profile_points": [[6, 0], [6, 7], [7, 7], [7, 3.5], [9, 3.5],
                                       [9, 4], [14, 4], [15, 3], [15, 0], [6, 0]],
                },
            },
        ],
        "outputs": ["SLDPRT"],
    }

    plan = DirectCADIRService(tmp_path).plan(json.dumps(payload), stage_mode="model_3d")

    assert plan["planning_validation"]["status"] == "approved"
    assert plan["planning_validation"]["assumptions"] == []
    assert plan["planning_validation"]["unresolved"] == []
    assert set(plan["execution_policy"]["allowed_skills"]) == {
        "profile_extrude",
        "revolve",
        "save_sldprt",
    }
    assert [step.skill_key for step in SkillPlanner().plan(plan)] == [
        "profile_extrude",
        "revolve",
        "save_sldprt",
    ]


def test_invalid_json_and_candidate_json_are_rejected_before_planning(tmp_path: Path) -> None:
    service = DirectCADIRService(tmp_path)
    with pytest.raises(DirectCADIRInputError, match="one JSON object"):
        service.plan("create a plate")
    with pytest.raises(DirectCADIRInputError, match="not executable CAD-IR"):
        service.plan(json.dumps({"candidate_schema_version": "cad.planner_candidate.v1"}))


@pytest.mark.parametrize(
    ("input_builder", "expected_format"),
    [
        (lambda path, text: f"```json\n{text}\n```", "markdown_json_block"),
        (lambda path, text: f"```text\n{path}\n```", "json_file_path"),
        (lambda path, text: str(path), "json_file_path"),
        (
            lambda path, text: f"[motor CAD-IR](<{path}>)",
            "markdown_json_file_link",
        ),
        (
            lambda path, text: text.lstrip()[1:],
            "json_outer_object_repaired",
        ),
    ],
)
def test_direct_cad_ir_accepts_safe_gui_paste_formats(
    tmp_path: Path,
    input_builder,
    expected_format: str,
) -> None:
    payload = _plate_design()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path = tmp_path / "plate design.json"
    path.write_text(text, encoding="utf-8")

    plan = DirectCADIRService(tmp_path / expected_format).plan(
        input_builder(path, text),
        stage_mode="model_3d",
    )

    assert plan["planning_validation"]["status"] == "approved"
    assert plan["direct_cad_ir_input_format"] == expected_format
    assert [item["type"] for item in plan["features"]] == ["base_plate", "through_hole"]


def test_direct_cad_ir_does_not_hide_multiple_json_documents(tmp_path: Path) -> None:
    text = json.dumps(_plate_design(), ensure_ascii=False)
    with pytest.raises(DirectCADIRInputError, match="one JSON object"):
        DirectCADIRService(tmp_path).plan(f"{text}\n{text}")


def test_direct_cad_ir_reports_missing_json_file_before_planning(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(DirectCADIRInputError, match="does not exist"):
        DirectCADIRService(tmp_path / "planning").plan(str(missing))


def test_invalid_cad_ir_is_blocked_and_has_no_allowed_skills(tmp_path: Path) -> None:
    payload = _plate_design()
    payload["features"][1]["params"] = {"position": "center"}

    plan = DirectCADIRService(tmp_path).plan(json.dumps(payload), stage_mode="auto")

    assert plan["planning_validation"]["status"] == "blocked"
    assert plan["planning_validation"]["allow_pipeline"] is False
    assert plan["execution_policy"]["allowed_skills"] == []
    assert any(item["code"] == "missing_required_parameter" for item in plan["planning_validation"]["errors"])


def test_direct_cad_ir_dry_pipeline_reports_direct_provider(tmp_path: Path) -> None:
    plan = DirectCADIRService(tmp_path / "planning").plan(json.dumps(_plate_design()))
    report = AgentsOrchestratorRunner(tmp_path / "pipeline", provider_id="local_fallback").run(
        "Direct CAD-IR GUI input",
        execute_real_skills=False,
        run_pipeline=True,
        stage_mode="model_3d",
        design_json=plan,
    )

    assert report["status"] == "success"
    assert report["orchestrator"]["provider"] == "direct_cad_ir"
    assert report["orchestrator"]["planning_provider"] == "direct_cad_ir"
    assert report["orchestrator"]["llm_enabled"] is False
    assert [item["skill_key"] for item in report["skill_pipeline"]] == [
        "base_plate",
        "through_hole",
        "save_sldprt",
    ]


@pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM") not in {None, "offscreen"}, reason="GUI test requires offscreen Qt")
def test_gui_locks_provider_to_direct_cad_ir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from PySide6.QtWidgets import QApplication
    import app as gui_app

    application = QApplication.instance() or QApplication([])
    assert application is not None
    window = gui_app.MainWindow(tmp_path / "gui.log")
    try:
        assert window.provider_combo.currentData() == "direct_cad_ir"
        assert not window.provider_combo.isEnabled()
        assert "LLM disabled" in window.agent_core_label.text()
        assert "cad.ir.v1" in window.command_input.placeholderText()
        assert ".json 文件完整路径" in window.command_input.placeholderText()
    finally:
        window.status_timer.stop()
        window.activity_timer.stop()
        window.close()
