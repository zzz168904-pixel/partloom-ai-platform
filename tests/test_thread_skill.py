from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager, VibeCADSkill


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    plan = VibeCADSkill(output_root).build_plan("生成60x36x20钢块，中间一个M6攻丝孔")
    if "solidworks_threaded_holes" not in plan["skill_pipeline"]:
        raise AssertionError(f"Thread skill not routed: {plan['skill_pipeline']}")
    result = CADAgentSkillManager(output_root).run_thread_plan(plan)
    if not result.success:
        raise AssertionError(f"Thread skill failed: {result.message}\n{result.output}")
    output_dir = Path(str(result.path))
    required_suffixes = (".SLDPRT", ".step", ".json")
    existing = list(output_dir.glob("*"))
    for suffix in required_suffixes:
        if not any(path.suffix.lower() == suffix.lower() for path in existing):
            raise AssertionError(f"missing {suffix} in {output_dir}: {existing}")
    print("Thread Skill test passed")
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
