from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.active_model_pattern_skill import ActiveModelPatternSkill
from cad_agent.agents_orchestrator.skill_router_agent import SkillRouterAgent
from cad_agent.design_planner import DesignPlanner
from cad_agent.skill_planner import SkillPlanner
from cad_agent.stage_planner import apply_stage_plan, infer_stage_plan


def _keys(items: list[object]) -> list[str]:
    result = []
    for item in items:
        result.append(item.skill_key if hasattr(item, "skill_key") else item["skill_key"])
    return result


def test_pattern_request_normalization() -> None:
    linear = ActiveModelPatternSkill.normalize_request(
        "linear_pattern",
        {"seed_feature": "SeedBoss", "count": 4, "spacing": 20, "direction": "+X"},
    )
    assert linear["success"], linear
    assert linear["seed_features"] == ["SeedBoss"]
    assert linear["count_1"] == 4
    assert linear["spacing_1_mm"] == 20
    assert linear["direction_1"] == "+x"

    circular = ActiveModelPatternSkill.normalize_request(
        "circular_pattern",
        {
            "seed_features": ["RadialSeedBoss"],
            "count": 6,
            "total_angle_deg": 360,
            "axis": "Z",
            "axis_feature": "CenterAxisHole",
        },
    )
    assert circular["success"], circular
    assert circular["count"] == 6
    assert circular["axis"] == "z"
    assert circular["pattern_angle_deg"] == 360

    fixed_pitch = ActiveModelPatternSkill.normalize_request(
        "circular_pattern",
        {
            "seed_features": ["CoolingFinSeed"],
            "count": 30,
            "spacing_angle_deg": 6,
            "equal_spacing": False,
            "axis": "x",
        },
    )
    assert fixed_pitch["success"], fixed_pitch
    assert fixed_pitch["equal_spacing"] is False
    assert fixed_pitch["spacing_angle_deg"] == 6
    assert fixed_pitch["pattern_angle_deg"] == 6

    mirror = ActiveModelPatternSkill.normalize_request(
        "mirror",
        {"seed_features": ["MirrorSeedBoss"], "mirror_plane": "YZ", "scope": "features"},
    )
    assert mirror["success"], mirror
    assert mirror["mirror_plane"] == "right"

    ambiguous = ActiveModelPatternSkill.normalize_request(
        "linear_pattern",
        {"count": 4, "spacing": 20, "direction": "x"},
    )
    assert not ambiguous["success"]
    assert "seed_features" in ambiguous["message"]


def test_circular_pattern_selects_explicit_reference_axis_before_cylindrical_fallback(tmp_path: Path) -> None:
    class FakeAxis:
        Name = "GearAxis_X"

        def __init__(self) -> None:
            self.selection = None

        @staticmethod
        def GetTypeName2() -> str:
            return "RefAxis"

        def Select2(self, append: bool, mark: int) -> bool:
            self.selection = (append, mark)
            return True

    axis = FakeAxis()
    skill = ActiveModelPatternSkill(tmp_path)
    skill._find_feature = lambda _model, name: axis if name == "GearAxis_X" else None
    result = skill._select_circular_axis(
        object(),
        {
            "bodies": [],
            "bbox": {"xmin": 0.0, "ymin": -0.03, "zmin": -0.03, "xmax": 0.05, "ymax": 0.03, "zmax": 0.03, "length": 0.05, "width": 0.06, "thickness": 0.06},
        },
        {"axis": "x", "axis_feature": "GearAxis_X", "axis_center_mm": [0.0, 0.0]},
        mark=1,
    )

    assert result["success"]
    assert result["reference_kind"] == "explicit_reference_axis"
    assert result["axis_feature"] == "GearAxis_X"
    assert axis.selection == (True, 1)


def test_natural_language_linear_pattern_routing() -> None:
    prompt = "创建一个140x80x10mm底板，中心开一个Φ8通孔，把中心孔沿+X方向线性阵列4个，间距20mm，只生成3D零件"
    design = DesignPlanner(ROOT / "logs" / "test_skill_outputs").plan(prompt)
    pattern = next(item for item in design["features"] if item["type"] == "linear_pattern")
    assert pattern["params"]["seed_features"] == ["CenterHole"]
    assert pattern["params"]["count_1"] == 4
    assert pattern["params"]["spacing_1"] == 20
    assert pattern["params"]["direction_1"] == "+x"
    assert not design["needs_confirmation"], design.get("confirmation_reason")
    assert design["execution_policy"]["allowed_skills"] == [
        "base_plate",
        "through_hole",
        "linear_pattern",
        "save_sldprt",
    ]
    assert _keys(SkillPlanner().plan(design)) == design["execution_policy"]["allowed_skills"]
    routed = SkillRouterAgent().route(design)
    assert _keys(routed["skill_pipeline"]) == design["execution_policy"]["allowed_skills"]
    routed_pattern = next(item for item in routed["skill_pipeline"] if item["skill_key"] == "linear_pattern")
    assert routed_pattern["parameters"]["features"][0]["params"]["seed_features"] == ["CenterHole"]


def test_manual_circular_and_mirror_scope_gate() -> None:
    prompt = "创建140x140x10mm零件，只生成3D零件"
    base = {
        "source_brief": prompt,
        "parameters": {"unit": "mm", "length": 140.0, "width": 140.0, "thickness": 10.0, "material": "6061 aluminum"},
        "features": [
            {"name": "BasePlate", "type": "base_plate", "params": {}, "required": True},
            {
                "name": "CircularPattern",
                "type": "circular_pattern",
                "params": {
                    "seed_features": ["RadialSeedBoss"],
                    "count": 6,
                    "total_angle_deg": 360,
                    "axis": "z",
                    "axis_feature": "CenterAxisHole",
                },
                "required": True,
            },
            {
                "name": "MirrorPattern",
                "type": "mirror",
                "params": {"seed_features": ["MirrorSeedBoss"], "mirror_plane": "Right Plane", "scope": "features"},
                "required": True,
            },
        ],
        "outputs": ["SLDPRT"],
        "unsupported_features": [],
    }
    design = apply_stage_plan(base, infer_stage_plan(prompt))
    assert not design["needs_confirmation"], design.get("confirmation_reason")
    assert design["execution_policy"]["allowed_skills"] == [
        "base_plate",
        "circular_pattern",
        "mirror",
        "save_sldprt",
    ]


def test_pattern_geometry_gate_rejects_tree_only_success() -> None:
    before = {
        "success": True,
        "solid_body_count": 1,
        "face_count": 20,
        "edge_count": 40,
        "volume_m3": 5.0e-4,
    }
    tree_only = {
        "success": True,
        "solid_body_count": 1,
        "face_count": 22,
        "edge_count": 44,
        "volume_m3": 5.0e-4,
    }
    changed = {
        "success": True,
        "solid_body_count": 1,
        "face_count": 24,
        "edge_count": 48,
        "volume_m3": 4.8e-4,
    }

    failure = ActiveModelPatternSkill._verify_geometry(before, tree_only)
    success = ActiveModelPatternSkill._verify_geometry(before, changed)

    assert failure["success"] is False
    assert "did not change" in failure["message"]
    assert success["success"] is True
    assert success["volume_delta_m3"] < 0.0


if __name__ == "__main__":
    test_pattern_request_normalization()
    test_natural_language_linear_pattern_routing()
    test_manual_circular_and_mirror_scope_gate()
    print("Pattern/mirror pipeline tests passed")
