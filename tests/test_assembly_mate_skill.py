from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.assembly_mate_skill import AssemblyMateSkill
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _params(first: Path, second: Path) -> dict:
    return {
        "mode": "new_assembly",
        "components": [
            {"id": "base", "path": str(first), "position_mm": [0, 0, 0]},
            {"id": "cover", "path": str(second), "position_mm": [50, 0, 0]},
        ],
        "mates": [
            {
                "name": "FrontPlanes",
                "mate_type": "coincident",
                "entity_a": {"component": "base", "selector_type": "reference_plane", "plane": "front"},
                "entity_b": {"component": "cover", "selector_type": "reference_plane", "plane": "front"},
            }
        ],
    }


def _files(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "base.SLDPRT"
    second = tmp_path / "cover.SLDPRT"
    first.write_bytes(b"part-a")
    second.write_bytes(b"part-b")
    return first, second


def test_assembly_schema_normalizes_components_and_plane_mate(tmp_path: Path) -> None:
    first, second = _files(tmp_path)
    request = AssemblyMateSkill.normalize_request(_params(first, second))
    assert request["success"]
    assert request["mode"] == "new_assembly"
    assert [item["id"] for item in request["components"]] == ["base", "cover"]
    assert request["mates"][0]["mate_type"] == "coincident"
    assert request["mates"][0]["entity_a"]["aliases"][0] == "Front Plane"


def test_assembly_schema_blocks_missing_files_and_invalid_mates(tmp_path: Path) -> None:
    first, second = _files(tmp_path)
    missing = _params(first, tmp_path / "missing.SLDPRT")
    same_component = _params(first, second)
    same_component["mates"][0]["entity_b"]["component"] = "base"
    concentric_planes = _params(first, second)
    concentric_planes["mates"][0]["mate_type"] = "concentric"
    assert not AssemblyMateSkill.normalize_request(missing)["success"]
    assert not AssemblyMateSkill.normalize_request(same_component)["success"]
    assert not AssemblyMateSkill.normalize_request(concentric_planes)["success"]


def test_stage_router_runs_only_assembly_mate_and_sldasm(tmp_path: Path) -> None:
    first, second = _files(tmp_path)
    prompt = "Create an assembly from the two specified parts and save SLDASM only."
    source = {
        "source_brief": prompt,
        "task_type": "model_3d",
        "parameters": {"unit": "mm"},
        "features": [{"name": "TestAssembly", "type": "assembly_mate", "required": True, "params": _params(first, second)}],
        "outputs": ["SLDASM"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDASM"], "views": [], "checks": []},
    }
    design = apply_stage_plan(source, infer_stage_plan(prompt, "model_3d"))
    assert design["execution_policy"]["allowed_skills"] == ["assembly_mate"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDASM"]
    assert [step.skill_key for step in SkillPlanner().plan(design)] == ["assembly_mate"]
    assert [item["skill_key"] for item in SkillRouterAgent().route(design)["skill_pipeline"]] == ["assembly_mate"]

