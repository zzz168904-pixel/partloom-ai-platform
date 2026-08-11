from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager, VibeCADSkill


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    plan = VibeCADSkill(output_root).build_plan("生成120x80x18铝合金CNC安装座，R8圆角，C1倒角")
    if "solidworks_cnc_fillet" not in plan["skill_pipeline"]:
        raise AssertionError(f"Fillet/Chamfer skill not routed: {plan['skill_pipeline']}")

    result = CADAgentSkillManager(output_root).run_fillet_chamfer_plan(plan, allow_test_template=True)
    if not result.success:
        raise AssertionError(f"Fillet/Chamfer/CNC skill failed: {result.message}\n{result.output}\n{result.data}")

    output_dir = Path(str(result.path))
    checks = result.data.get("checks", {})
    for name, passed in checks.items():
        if not passed:
            raise AssertionError(f"failed check {name}: {checks}")

    print("Fillet/Chamfer/CNC Skill test passed")
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
