from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager, VibeCADSkill
from unified_skill_manager import SkillManager


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    skill = VibeCADSkill(output_root)

    plan = skill.build_plan("画一个100×50×10钢板，中间一个Φ20孔，导出PDF和DWG")
    params = plan["parameters"]
    assert_equal(params["length"], 100.0, "length")
    assert_equal(params["width"], 50.0, "width")
    assert_equal(params["thickness"], 10.0, "thickness")
    assert_equal(params["material"], "plain carbon steel", "material")
    assert_equal(plan["part_family"], "plate", "part_family")

    holes = [item for item in plan["features"] if item["type"] == "through_hole"]
    assert_equal(len(holes), 1, "through_hole_count")
    assert_equal(holes[0]["params"]["diameter"], 20.0, "hole_diameter")
    assert_equal(holes[0]["params"]["position"], "center", "hole_position")
    if "solidworks_vibecad" not in plan["skill_pipeline"] or "solidworks_automation" not in plan["skill_pipeline"]:
        raise AssertionError(f"unexpected skill pipeline: {plan['skill_pipeline']}")
    if "autocad" not in plan["skill_pipeline"]:
        raise AssertionError("PDF/DWG/dimension workflow should include autocad")

    threaded = skill.build_plan("生成120x80x12铝合金安装座，四角M6攻丝孔，R5圆角，C1倒角")
    threaded_features = {item["type"] for item in threaded["features"]}
    for expected in ("threaded_hole", "fillet", "chamfer"):
        if expected not in threaded_features:
            raise AssertionError(f"missing feature: {expected}")
    for expected_skill in ("solidworks_threaded_holes", "fillet"):
        if expected_skill not in threaded["skill_pipeline"]:
            raise AssertionError(f"missing routed skill: {expected_skill}")

    advanced = skill.build_plan(
        "生成100x50x10钢板，4个孔矩形阵列，间距20，关于右视基准镜像，"
        "基准A为底面，公差±0.05，两个轴同心装配"
    )
    advanced_types = {item["type"] for item in advanced["features"]}
    for expected in ("linear_pattern", "mirror"):
        if expected not in advanced_types:
            raise AssertionError(f"missing advanced feature {expected}: {advanced['features']}")
    if not advanced["datums"] or advanced["datums"][0]["name"] != "A":
        raise AssertionError(f"missing datum A: {advanced['datums']}")
    if not advanced["tolerances"] or advanced["tolerances"][0]["value"] != 0.05:
        raise AssertionError(f"missing tolerance: {advanced['tolerances']}")
    if not advanced["assembly_relations"] or advanced["assembly_relations"][0]["type"] != "concentric":
        raise AssertionError(f"missing assembly relation: {advanced['assembly_relations']}")
    if "solidworks_assembly" not in advanced["skill_pipeline"]:
        raise AssertionError(f"missing assembly pipeline skill: {advanced['skill_pipeline']}")

    manager = CADAgentSkillManager(output_root)
    route = manager.route("画一个100×50×10钢板，中间一个Φ20孔", [])
    if route is None or route.skill_key != "solidworks_vibecad":
        raise AssertionError(f"unexpected CADAgent route: {route}")

    legacy = SkillManager(output_root)
    legacy_route = legacy.route("画一个100×50×10钢板，中间一个Φ20孔", [])
    if legacy_route is None or legacy_route.skill_key != "solidworks_vibecad" or legacy_route.actions != ["vibecad_plan"]:
        raise AssertionError(f"unexpected legacy route: {legacy_route}")

    result = legacy.run_vibecad_plan("画一个100×50×10钢板，中间一个Φ20孔")
    if not result["success"] or not result.get("path") or not Path(str(result["path"])).exists():
        raise AssertionError(f"VibeCAD run failed: {result}")
    saved = json.loads(Path(str(result["path"])).read_text(encoding="utf-8"))
    assert_equal(saved["parameters"]["length"], 100.0, "saved_length")

    print("VibeCAD agent tests passed")
    print(result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
