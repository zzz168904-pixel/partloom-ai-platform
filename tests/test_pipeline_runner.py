from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import AgentPipelineRunner, CADAgentSkillManager


PROMPT = (
    "\u8bbe\u8ba1\u4e00\u4e2a100\u00d760\u00d710\u5b89\u88c5\u677f\uff0c"
    "\u4e2d\u95f4\u03a620\u5b54\uff0c"
    "\u56db\u89d2R5\u5706\u89d2\uff0c"
    "\u6750\u65996061\u94dd\uff0c"
    "\u5bfc\u51faSTEP\u3001DWG\u3001PDF\u3002"
)


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    manager = CADAgentSkillManager(output_root)
    runner = AgentPipelineRunner(output_root, skill_manager=manager, execute_real_skills=False)
    context = runner.run(PROMPT)

    if context.status != "success":
        raise AssertionError(f"pipeline did not succeed: {context.status}")
    if not context.report_path or not context.report_path.exists():
        raise AssertionError(f"missing pipeline report: {context.report_path}")
    if not context.log_path or not context.log_path.exists():
        raise AssertionError(f"missing pipeline log: {context.log_path}")

    report = json.loads(context.report_path.read_text(encoding="utf-8"))
    params = report["design_json"]["parameters"]
    if params["length"] != 100.0 or params["width"] != 60.0 or params["thickness"] != 10.0:
        raise AssertionError(f"unexpected parameters: {params}")

    step_keys = [step["skill_key"] for step in report["skill_pipeline"]]
    for expected in (
        "base_plate",
        "fillet",
        "through_hole",
        "save_sldprt",
        "step_export",
        "pdf_export",
        "dwg_export",
    ):
        if expected not in step_keys:
            raise AssertionError(f"missing pipeline step {expected}: {step_keys}")
    for forbidden in ("solidworks_drawing", "autocad_annotation", "solidworks_cnc_fillet"):
        if forbidden in step_keys:
            raise AssertionError(f"unexpected downstream step {forbidden}: {step_keys}")

    for step in report["skill_pipeline"]:
        if step["status"] != "success":
            raise AssertionError(f"step failed unexpectedly: {step}")
        for stage in ("prepare", "execute", "verify", "export", "cleanup"):
            if step["lifecycle"].get(stage) != "success":
                raise AssertionError(f"missing lifecycle stage {stage}: {step}")

    manager_result = manager.run_agent_pipeline(PROMPT, execute_real_skills=False)
    if manager_result["status"] != "success":
        raise AssertionError(f"manager pipeline failed: {manager_result['status']}")

    if os.environ.get("RUN_REAL_CAD_PIPELINE") == "1":
        real_context = AgentPipelineRunner(output_root, skill_manager=manager, execute_real_skills=True).run(PROMPT)
        if real_context.status != "success":
            raise AssertionError(f"real CAD pipeline failed: {real_context.as_dict()}")
        real_report = real_context.as_dict()
        for artifact in ("solidworks_model", "step", "dwg", "pdf"):
            path = real_report["artifacts"].get(artifact)
            if not path or not Path(path).exists():
                raise AssertionError(f"missing real artifact {artifact}: {path}")

    print("Pipeline runner tests passed")
    print(context.report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
