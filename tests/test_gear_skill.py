from __future__ import annotations

import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.design_planner import DesignPlanner
from cad_agent.gear_skill import GearSkill
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _gear_params() -> dict:
    return {
        "mode": "new_model",
        "gear_type": "spur",
        "module_mm": 2,
        "teeth": 24,
        "pressure_angle_deg": 20,
        "face_width_mm": 12,
        "bore_diameter_mm": 12,
        "backlash_mm": 0,
        "keyway": {"enabled": True, "width_mm": 4, "depth_mm": 2},
    }


def _gear_design() -> dict:
    return {
        "source_brief": (
            "Create one module 2, 24-tooth, 20 degree spur gear with a 12 mm face width, "
            "12 mm bore and 4 x 2 mm keyway. Save SLDPRT only."
        ),
        "task_type": "model_3d",
        "parameters": {"unit": "mm", "material": "45 steel"},
        "features": [
            {
                "name": "DriveGear",
                "type": "gear",
                "required": True,
                "params": _gear_params(),
            }
        ],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
        "needs_confirmation": False,
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }


def test_spur_gear_normalizes_to_standard_involute_dimensions() -> None:
    request = GearSkill.normalize_request(_gear_params(), "model_3d")

    assert request["success"]
    assert request["gear_type"] == "spur"
    assert request["pitch_diameter_mm"] == 48.0
    assert request["outside_diameter_mm"] == 52.0
    assert request["root_diameter_mm"] == 43.0
    assert request["base_diameter_mm"] == pytest_approx(48.0 * math.cos(math.radians(20)))
    assert request["keyway"] == {"enabled": True, "width_mm": 4.0, "depth_mm": 2.0}


def test_involute_tooth_profile_is_closed_symmetric_and_patterns_to_outside_diameter() -> None:
    request = GearSkill.normalize_request(_gear_params(), "model_3d")
    profile = request["tooth_profile_points_mm"]

    assert profile[0] == profile[-1]
    assert len(profile) == 2 * request["flank_samples"] + 6
    first_segment_length = math.dist(profile[0], profile[1])
    assert first_segment_length >= min(request["module_mm"] * 0.5, 1.0)
    samples = request["flank_samples"]
    lower_flank = profile[1 : 1 + samples]
    upper_flank = profile[4 + samples : 4 + 2 * samples]
    for lower, upper in zip(lower_flank, reversed(upper_flank)):
        assert lower[0] == pytest_approx(upper[0], abs=1e-8)
        assert lower[1] == pytest_approx(-upper[1], abs=1e-8)
    bounds = request["expected_bounds_mm"]
    assert bounds["x_span"] == pytest_approx(request["outside_diameter_mm"], abs=1e-6)
    assert bounds["y_span"] == pytest_approx(request["outside_diameter_mm"], abs=1e-6)


def test_gear_guards_reject_undercut_unsupported_type_and_ambiguous_keyway() -> None:
    undercut = {**_gear_params(), "teeth": 16}
    helical = {**_gear_params(), "gear_type": "helical", "helix_angle_deg": 15}
    ambiguous_keyway = {**_gear_params(), "keyway": True}

    assert not GearSkill.normalize_request(undercut, "model_3d")["success"]
    assert not GearSkill.normalize_request(helical, "model_3d")["success"]
    assert not GearSkill.normalize_request(ambiguous_keyway, "model_3d")["success"]


def test_stage_gate_routes_only_gear_and_sldprt_save() -> None:
    design = apply_stage_plan(_gear_design(), infer_stage_plan(_gear_design()["source_brief"], "model_3d"))

    assert not design["needs_confirmation"]
    assert design["execution_policy"]["allowed_skills"] == ["gear", "save_sldprt"]
    routed = SkillRouterAgent().route(design)
    assert [item["skill_key"] for item in routed["skill_pipeline"]] == ["gear", "save_sldprt"]
    assert routed["skill_pipeline"][0]["parameters"]["features"][0]["params"]["teeth"] == 24


def test_structured_gear_parameters_recover_to_one_feature(tmp_path: Path) -> None:
    raw = {
        "parameters": {"unit": "mm", "gear": _gear_params()},
        "features": [],
        "outputs": ["SLDPRT"],
        "review_plan": {"expected_outputs": ["SLDPRT"], "views": [], "checks": []},
    }
    design = DesignPlanner(tmp_path)._normalize_design(
        raw,
        "Create a module 2, 24 tooth spur gear and save SLDPRT only.",
        stage_mode="model_3d",
    )

    gears = [item for item in design["features"] if item.get("type") == "gear"]
    assert len(gears) == 1
    assert gears[0]["params"]["module_mm"] == 2
    assert design["execution_policy"]["allowed_skills"] == ["gear", "save_sldprt"]


def pytest_approx(value: float, *, abs: float = 1e-9):
    import pytest

    return pytest.approx(value, abs=abs)
