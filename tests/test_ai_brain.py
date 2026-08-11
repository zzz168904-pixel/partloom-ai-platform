from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cad_agent import AIBrain, CADAgentSkillManager, DesignPlanner


PROMPT = (
    "\u8bbe\u8ba1\u4e00\u4e2a100\u00d760\u00d710\u5b89\u88c5\u677f\uff0c"
    "\u4e2d\u95f4\u4e00\u4e2a\u03a620\u5b54\uff0c"
    "\u56db\u89d2R5\u5706\u89d2\uff0c\u6750\u65996061\u94dd\uff0c"
    "\u5bfc\u51faSTEP\u3001DWG\u548cPDF\u3002"
)


class FakeProvider:
    name = "fake-provider"

    def generate_design_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "parameters": {
                "unit": "mm",
                "length": 80,
                "width": 40,
                "thickness": 8,
                "material": "test material",
            },
            "features": [{"type": "chamfer", "params": {"distance": 1}}],
            "review_plan": {"expected_outputs": ["step"]},
        }


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main() -> int:
    output_root = ROOT / "logs" / "test_skill_outputs"
    brain = AIBrain(output_root)
    plan, path = brain.plan_and_save(PROMPT)

    params = plan.design_json["parameters"]
    assert_equal(params["length"], 100.0, "length")
    assert_equal(params["width"], 60.0, "width")
    assert_equal(params["thickness"], 10.0, "thickness")
    assert_equal(params["material"], "6061 aluminum", "material")

    features = {feature["type"]: feature for feature in plan.design_json["features"]}
    for expected in ("through_hole", "fillet"):
        if expected not in features:
            raise AssertionError(f"missing feature {expected}: {plan.design_json['features']}")
    assert_equal(features["through_hole"]["params"]["diameter"], 20.0, "through_hole diameter")
    assert_equal(features["through_hole"]["params"]["position"], "center", "through_hole position")
    assert_equal(features["fillet"]["params"]["radius"], 5.0, "fillet radius")

    pipeline = [step.skill_key for step in plan.skill_pipeline]
    for expected in (
        "base_plate",
        "fillet",
        "through_hole",
        "save_sldprt",
        "step_export",
        "pdf_export",
    ):
        if expected not in pipeline:
            raise AssertionError(f"missing pipeline step {expected}: {pipeline}")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert_equal(saved["model_provider"], "local-rule-based", "model provider")
    if not saved["skill_pipeline"]:
        raise AssertionError(f"empty saved pipeline: {saved}")

    fake_planner = DesignPlanner(output_root, provider=FakeProvider())
    fake_design = fake_planner.plan("fake request")
    assert_equal(fake_planner.provider_name, "fake-provider", "fake provider name")
    assert_equal(fake_design["schema_version"], "vibecad.design.v1", "normalized schema")
    assert_equal(fake_design["features"][0]["name"], "chamfer", "normalized feature name")

    manager = CADAgentSkillManager(output_root)
    manager_plan, manager_path = manager.plan_brain_and_save(PROMPT)
    if not manager_path.exists():
        raise AssertionError(f"missing manager brain plan file: {manager_path}")
    assert_equal(manager_plan.model_provider, "local-rule-based", "manager provider")

    print("AI Brain tests passed")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
