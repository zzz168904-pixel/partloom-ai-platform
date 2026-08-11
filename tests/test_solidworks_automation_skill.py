from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager, VibeCADSkill


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    manager = CADAgentSkillManager(output_root)
    plan = VibeCADSkill(output_root).build_plan("画一个100×50×10钢板，中间一个Φ20孔")
    result = manager.run_solidworks_plan(plan)
    if not result.success:
        raise AssertionError(result)
    if not result.path or not Path(result.path).exists():
        raise AssertionError(f"part file missing: {result.path}")
    created = result.data.get("created_features", [])
    if not any(item.get("type") == "base_plate" for item in created):
        raise AssertionError(f"missing base plate feature: {created}")
    holes = [item for item in created if item.get("type") == "through_hole"]
    if not holes or holes[0].get("diameter") != 20.0:
        raise AssertionError(f"missing center through hole: {created}")
    print("SolidWorks Automation skill test passed")
    print(result.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
