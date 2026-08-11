from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent import CADAgentSkillManager
from cad_agent.active_model_feature_skill import ActiveModelFeatureSkill
from cad_agent.active_model_through_hole import ActiveModelThroughHoleExecutor
from cad_agent.agents_orchestrator import CADPlannerAgent, SkillRouterAgent
from cad_agent.production_fillet_skill import ProductionFilletSkill


PROMPT = (
    "\u521b\u5efa\u4e00\u4e2a\u673a\u68b0\u8bbe\u5907\u5b89\u88c5\u5e95\u5ea7\uff0c\u6750\u6599\u4e3a6061\u94dd\u3002"
    "\u5e95\u677f\u5c3a\u5bf8\u4e3a\u957f160mm\u3001\u5bbd100mm\u3001\u539a12mm\uff0c\u56db\u89d2\u505aR8\u5706\u89d2\u3002"
    "\u5e95\u677f\u56db\u89d2\u5404\u5f001\u4e2a\u03c68.5\u8d2f\u7a7f\u5b54\uff0c4\u4e2a\u5b54\u4e2d\u5fc3\u8ddd\u79bb\u76f8\u90bb\u4e24\u8fb9\u5747\u4e3a15mm\u3002"
    "\u5e95\u677f\u4e2d\u5fc3\u5f001\u4e2a\u03c630\u8d2f\u7a7f\u5b54\u3002"
    "\u5728\u5e95\u677f\u4e0a\u8868\u9762\u4e2d\u5fc3\u521b\u5efa\u4e00\u4e2a\u957f80mm\u3001\u5bbd45mm\u3001\u9ad818mm\u7684\u77e9\u5f62\u51f8\u53f0\u3002"
    "\u5728\u51f8\u53f0\u9876\u90e8\u4e2d\u5fc3\u5f00\u4e00\u4e2a\u957f50mm\u3001\u5bbd22mm\u3001\u6df18mm\u7684\u77e9\u5f62\u578b\u8154\uff0c\u578b\u8154\u56db\u89d2R3\u3002"
    "\u5e95\u677f\u5de6\u53f3\u4e24\u4fa7\u5404\u5f001\u4e2a\u8170\u578b\u8d2f\u7a7f\u69fd\uff0c\u6bcf\u4e2a\u69fd\u603b\u957f36mm\u3001\u5bbd10mm\uff0c"
    "\u69fd\u4e2d\u5fc3\u5206\u522b\u4f4d\u4e8e\u5e95\u677f\u4e2d\u5fc3\u5de6\u53f340mm\u5904\u3002"
    "\u6240\u6709\u672a\u7279\u522b\u8bf4\u660e\u7684\u5916\u8fb9\u505aC1\u5012\u89d2\u3002"
    "\u53ea\u751f\u62103D\u96f6\u4ef6\u5e76\u4fdd\u5b58\u4e3aSLDPRT\u3002"
)

VARIANT_PROMPT = (
    "\u521b\u5efa\u4e00\u4e2a\u5b89\u88c5\u5e95\u5ea7\uff0c\u5e95\u677f\u957f120mm\u3001\u5bbd80mm\u3001\u539a10mm\uff0c\u56db\u89d2R6\u3002"
    "\u56db\u89d2\u5404\u5f00\u4e00\u4e2a\u03c66\u8d2f\u7a7f\u5b54\uff0c\u5b54\u4e2d\u5fc3\u8ddd\u79bb\u76f8\u90bb\u4e24\u8fb9\u574712mm\uff0c\u4e2d\u5fc3\u5f00\u03c624\u8d2f\u7a7f\u5b54\u3002"
    "\u4e2d\u5fc3\u521b\u5efa\u957f60mm\u3001\u5bbd36mm\u3001\u9ad814mm\u7684\u77e9\u5f62\u51f8\u53f0\uff0c"
    "\u9876\u90e8\u5f00\u957f36mm\u3001\u5bbd16mm\u3001\u6df16mm\u7684\u77e9\u5f62\u578b\u8154\uff0c\u578b\u8154\u56db\u89d2R2\u3002"
    "\u5de6\u53f3\u5404\u5f00\u4e00\u4e2a\u8170\u578b\u69fd\uff0c\u6bcf\u4e2a\u69fd\u603b\u957f28mm\u3001\u5bbd8mm\uff0c\u69fd\u4e2d\u5fc3\u4f4d\u4e8e\u4e2d\u5fc3\u5de6\u53f330mm\u5904\uff0c\u5916\u8fb9C0.8\u5012\u89d2\u3002"
    "\u53ea\u751f\u62103D\u96f6\u4ef6\u3002"
)


def test_complex_model_routes_all_production_features() -> None:
    design = CADPlannerAgent(ROOT / "logs" / "active_feature_tests").plan(PROMPT)
    route = SkillRouterAgent().route(design)
    expected = ["base_plate", "fillet", "boss", "chamfer", "through_hole", "pocket", "slot", "save_sldprt"]
    assert design["needs_confirmation"] is False
    assert design["execution_policy"]["unexecutable_required_features"] == []
    assert design["execution_policy"]["allowed_skills"] == expected
    assert [step["skill_key"] for step in route["skill_pipeline"]] == expected
    assert "solidworks_cnc_fillet" not in expected


def test_feature_parameters_and_contracts() -> None:
    design = CADPlannerAgent(ROOT / "logs" / "active_feature_tests").plan(PROMPT)
    features = {item["name"]: item["params"] for item in design["features"]}
    assert features["CornerMountingHoles"]["edge_offsets_mm"] == {"x": 15.0, "y": 15.0}
    assert features["CenterThroughHole"]["diameter"] == 30.0
    assert features["CenterBoss"] == {"length": 80.0, "width": 45.0, "height": 18.0, "position": "center_top"}
    assert features["TopPocket"] == {"length": 50.0, "width": 22.0, "depth": 8.0, "corner_radius": 3.0}
    assert features["SideSlots"]["orientation"] == "y"

    manager = CADAgentSkillManager(ROOT / "logs" / "active_feature_tests")
    for key in ("boss", "pocket", "slot", "chamfer"):
        contract = manager.skill_contract(key)
        assert contract is not None
        assert contract["modifies_active_doc"] is True
        assert contract["creates_new_doc"] is False
        assert contract["exports_files"] is False
        assert contract["uses_test_template"] is False


def test_dimensions_are_parameterized_not_fixture_values() -> None:
    design = CADPlannerAgent(ROOT / "logs" / "active_feature_tests").plan(VARIANT_PROMPT)
    features = {item["name"]: item["params"] for item in design["features"]}
    assert design["parameters"]["length"] == 120.0
    assert design["parameters"]["width"] == 80.0
    assert design["parameters"]["thickness"] == 10.0
    assert features["OuterCornerFillets"]["radius"] == 6.0
    assert features["CornerMountingHoles"]["diameter"] == 6.0
    assert features["CornerMountingHoles"]["edge_offsets_mm"] == {"x": 12.0, "y": 12.0}
    assert features["CenterThroughHole"]["diameter"] == 24.0
    assert features["CenterBoss"]["length"] == 60.0
    assert features["TopPocket"]["depth"] == 6.0
    assert features["SideSlots"]["length"] == 28.0
    assert features["OuterChamfers"]["size"] == 0.8
    assert design["needs_confirmation"] is False


def test_explicit_closed_circle_chamfer_requires_volume_removal_and_preserves_bodies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeFeatureManager:
        def InsertFeatureChamfer(self, *args):
            assert args[2] == 0.00075
            return SimpleNamespace(Name="Chamfer1")

    class FakeModel:
        FeatureManager = FakeFeatureManager()

        def ClearSelection2(self, _clear_all):
            return None

        def ForceRebuild3(self, _top_only):
            return True

    monkeypatch.setattr(
        ProductionFilletSkill,
        "_select_explicit_edge_signatures",
        staticmethod(lambda _model, _bodies, signatures, *, tolerance_mm: len(signatures)),
    )
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_body_info",
        staticmethod(lambda _model: {"success": True, "bodies": ["after_a", "after_b"]}),
    )
    monkeypatch.setattr(
        ActiveModelThroughHoleExecutor,
        "_solid_volume",
        staticmethod(lambda bodies: 0.004313741 if bodies[0] == "after_a" else 0.004313757),
    )
    feature = {
        "name": "SourceExact_Chamfer1_C0_75",
        "params": {
            "size": 0.75,
            "targets": "explicit_edge_signatures",
            "edge_tolerance_mm": 0.001,
            "edge_signatures": [
                {
                    "curve_type": "circle",
                    "center_mm": [0.0, 0.0, 0.0],
                    "axis": [1.0, 0.0, 0.0],
                    "radius_mm": 9.5,
                }
            ],
        },
    }

    result = ActiveModelFeatureSkill(tmp_path)._create_chamfer(
        FakeModel(),
        {},
        feature,
        {"bodies": ["before_a", "before_b"]},
    )

    assert result["success"] is True
    assert result["selected_edges"] == 1
    assert result["expected_edge_signatures"] == 1
    assert result["volume_delta_m3"] < 0.0
    assert result["body_count_before"] == result["body_count_after"] == 2


if __name__ == "__main__":
    test_complex_model_routes_all_production_features()
    test_feature_parameters_and_contracts()
    test_dimensions_are_parameterized_not_fixture_values()
    print("Active-model feature pipeline tests passed")
