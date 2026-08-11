from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .brain_models import BrainPlan, ModelProvider
from .design_planner import DesignPlanner
from .skill_planner import SkillPlanner


class AIBrain:
    """High-level CAD agent brain.

    The brain does not execute CAD software. It turns one user sentence into:
    prompt -> Design JSON -> Skill Pipeline.
    """

    def __init__(self, output_root: Path, provider: ModelProvider | None = None) -> None:
        self.output_root = output_root
        self.design_planner = DesignPlanner(output_root, provider=provider)
        self.skill_planner = SkillPlanner()

    def plan(self, prompt: str, stage_mode: str = "auto") -> BrainPlan:
        design = self.design_planner.plan(prompt, stage_mode=stage_mode)
        pipeline = self.skill_planner.plan(design)
        return BrainPlan(
            source_prompt=prompt.strip(),
            design_json=design,
            skill_pipeline=pipeline,
            model_provider=self.design_planner.provider_name,
        )

    def plan_and_save(self, prompt: str, stage_mode: str = "auto") -> tuple[BrainPlan, Path]:
        plan = self.plan(prompt, stage_mode=stage_mode)
        run_dir = self.output_root / f"ai_brain_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "brain_plan.json"
        path.write_text(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return plan, path
