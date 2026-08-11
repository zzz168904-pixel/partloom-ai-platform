from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.assembly_mate_skill import AssemblyMateSkill
from cad_agent.gear_pair_skill import GearPairSkill
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _pair_params() -> dict:
    return {
        "mode": "new_assembly",
        "gear_type": "spur",
        "module_mm": 2,
        "pressure_angle_deg": 20,
        "face_width_mm": 12,
        "gear_a": {"name": "Driver", "teeth": 24, "bore_diameter_mm": 12},
        "gear_b": {"name": "Driven", "teeth": 36, "bore_diameter_mm": 16},
        "center_distance_mm": 60,
        "interference_check": True,
    }


def _design() -> dict:
    prompt = "Create a module 2 spur-gear pair with 24 and 36 teeth and save the two Parts and assembly only."
    return {
        "source_brief": prompt,
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "material": "45 steel"},
        "features": [{"name": "ReductionPair", "type": "gear_pair", "required": True, "params": _pair_params()}],
        "outputs": ["SLDPRT", "SLDASM"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT", "SLDASM"], "views": [], "checks": []},
    }


def test_pair_normalizes_center_distance_ratio_and_phase() -> None:
    request = GearPairSkill.normalize_request(_pair_params(), "model_3d")

    assert request["success"]
    assert request["center_distance_mm"] == 60.0
    assert request["transmission_ratio"] == 1.5
    assert request["initial_phase_b_deg"] == 5.0
    assert request["gear_a"]["pitch_diameter_mm"] == 48.0
    assert request["gear_b"]["pitch_diameter_mm"] == 72.0


def test_pair_rejects_wrong_center_module_and_missing_bore() -> None:
    wrong_center = {**_pair_params(), "center_distance_mm": 65}
    wrong_module = _pair_params()
    wrong_module["gear_b"] = {**wrong_module["gear_b"], "module_mm": 2.5}
    no_bore = _pair_params()
    no_bore["gear_a"] = {**no_bore["gear_a"], "bore_diameter_mm": 0}

    assert not GearPairSkill.normalize_request(wrong_center, "model_3d")["success"]
    assert not GearPairSkill.normalize_request(wrong_module, "model_3d")["success"]
    assert not GearPairSkill.normalize_request(no_bore, "model_3d")["success"]


def test_pair_assembly_request_has_axis_distance_rotation_and_gear_mate(tmp_path: Path) -> None:
    first = tmp_path / "driver.SLDPRT"
    second = tmp_path / "driven.SLDPRT"
    first.write_bytes(b"driver")
    second.write_bytes(b"driven")
    pair = GearPairSkill.normalize_request(_pair_params(), "model_3d")

    request = AssemblyMateSkill.normalize_request(GearPairSkill._assembly_request(pair, first, second))

    assert request["success"]
    assert request["components"][1]["position_mm"] == [60.0, 0.0, 0.0]
    assert request["components"][1]["rotation_z_deg"] == 5.0
    assert request["mates"][1]["mate_type"] == "distance"
    assert request["mates"][1]["entity_a"]["selector_type"] == "reference_axis"
    assert request["mates"][2]["name"] == "GearCenterlinePlane"
    assert request["mates"][2]["entity_a"]["selector_type"] == "reference_plane"
    assert request["mates"][2]["entity_b"]["selector_type"] == "reference_axis"
    assert request["mates"][3]["mate_type"] == "gear"
    assert request["mates"][3]["gear_teeth_a"] == 24.0
    assert request["mates"][3]["gear_teeth_b"] == 36.0


def test_center_validation_requires_positive_x_centerline() -> None:
    first = Path("C:/cad/driver.SLDPRT")
    second = Path("C:/cad/driven.SLDPRT")
    centers = {
        str(first.resolve()).casefold(): [0.0, 0.0, 0.0],
        str(second.resolve()).casefold(): [60.0, 0.0, 0.0],
    }

    assert GearPairSkill._validate_center_distance(centers, first, second, 60.0)["success"]
    centers[str(second.resolve()).casefold()] = [0.0, 60.0, 0.0]
    assert not GearPairSkill._validate_center_distance(centers, first, second, 60.0)["success"]


def test_stage_gate_routes_only_gear_pair_and_requires_parts_and_assembly() -> None:
    source = _design()
    design = apply_stage_plan(source, infer_stage_plan(source["source_brief"], "model_3d"))

    assert not design["needs_confirmation"]
    assert design["execution_policy"]["allowed_skills"] == ["gear_pair"]
    assert design["execution_policy"]["expected_outputs"] == ["SLDPRT", "SLDASM"]
    assert [item.skill_key for item in SkillPlanner().plan(design)] == ["gear_pair"]
    assert [item["skill_key"] for item in SkillRouterAgent().route(design)["skill_pipeline"]] == ["gear_pair"]
