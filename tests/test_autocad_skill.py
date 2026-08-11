from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager, VibeCADSkill


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    manager = CADAgentSkillManager(output_root)
    preflight = manager.run_autocad_preflight(launch=False)
    if not preflight.success:
        raise AssertionError(f"AutoCAD preflight failed: {preflight.output}")

    plan = VibeCADSkill(output_root).build_plan("画一个100×50×10钢板，中间一个Φ20孔，导出DWG")
    if "autocad" not in plan["skill_pipeline"]:
        raise AssertionError(f"AutoCAD skill not routed: {plan['skill_pipeline']}")
    result = manager.run_autocad_plan(plan)
    if not result.success:
        raise AssertionError(f"AutoCAD skill failed: {result.message}\n{result.output}")
    dwg_path = Path(str(result.path))
    if not dwg_path.exists():
        raise AssertionError(f"DWG missing: {dwg_path}")

    review_path = Path(str(result.data["review_path"]))
    if not review_path.exists():
        raise AssertionError(f"review missing: {review_path}")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if int(review.get("modelspace_entity_count", 0)) < 3:
        raise AssertionError(f"too few entities: {review}")
    layers = review.get("layer_counts", {})
    for layer in ("OUTLINE", "HOLES", "TEXT"):
        if layer not in layers:
            raise AssertionError(f"missing layer {layer}: {layers}")

    print("AutoCAD Skill test passed")
    print(dwg_path)
    print(review_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
