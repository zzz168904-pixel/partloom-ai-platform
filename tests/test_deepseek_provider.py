from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.design_planner import DesignPlanner
from cad_agent.pipeline.cad_execution_lock import CADExecutionLock
from cad_agent.provider_registry import DeepSeekProvider, ProviderRegistry
from cad_agent.runtime_config import redact_secrets
from cad_agent.vibecad_skill import VibeCADSkill


class FakeDeepSeekProvider:
    name = "deepseek"

    def generate_design_json(self, _prompt: str, _schema: dict) -> dict:
        return {
            "parameters": {
                "material": {"name": "6061 aluminum"},
                "base_plate": {"length": 140, "width": 80, "thickness": 10},
            },
            "features": [
                {
                    "name": "BasePlate",
                    "type": "base_plate",
                    "params": {"length": 140, "width": 80, "thickness": 10},
                    "required": True,
                },
                {
                    "name": "CornerFillet",
                    "type": "fillet",
                    "params": {"radius": {"value": 5, "unit": "mm"}, "target": "four corners"},
                    "required": True,
                },
                {
                    "name": "CenterHole",
                    "type": "through_hole",
                    "params": {"diameter": 8, "position": "center of plate"},
                    "required": True,
                },
                {
                    "name": "HolePattern",
                    "type": "linear_pattern",
                    "params": {
                        "seed_features": ["CenterHole"],
                        "count_1": 4,
                        "spacing_1": 20,
                        "direction_1": "+X",
                    },
                    "required": True,
                },
            ],
            "outputs": ["SLDPRT"],
            "unsupported_features": [],
            "risks": [],
            "needs_confirmation": False,
            "review_plan": {"checks": ["features_created"]},
        }


class FakeDeepSeekCoordinateProvider:
    name = "deepseek"

    def generate_design_json(self, _prompt: str, _schema: dict) -> dict:
        return {
            "parameters": {"material": "6061 aluminum", "base_plate": {"length": 160, "width": 100, "thickness": 12}},
            "features": [
                {"name": "BasePlate", "type": "base_plate", "params": {"length": 160, "width": 100, "thickness": 12}},
                {"name": "CornerFillets", "type": "fillet", "params": {"radius": 8, "target": "four corners"}},
                {
                    "name": "CornerHoles",
                    "type": "through_hole",
                    "params": {
                        "diameter": 8.5,
                        "count": 4,
                        "placement": {"edge_offsets_mm": {"from_left": 15, "from_right": 15, "from_bottom": 15, "from_top": 15}},
                    },
                },
                {"name": "CenterHole", "type": "through_hole", "params": {"diameter": 30, "position_xy": [0, 0]}},
                {
                    "name": "Boss",
                    "type": "boss",
                    "params": {"length": 80, "width": 45, "height": 18, "position": "center_top", "position_xy": {"x": 80, "y": 50}},
                },
                {
                    "name": "Pocket",
                    "type": "pocket",
                    "params": {"length": 50, "width": 22, "depth": 8, "corner_radius": 3, "position_xy": {"x": 80, "y": 50}},
                },
                {
                    "name": "SideSlots",
                    "type": "slot",
                    "params": {
                        "count": 2,
                        "length": 36,
                        "width": 10,
                        "orientation": "along Y",
                        "centerline_offsets": [{"x": 40, "y": 50}, {"x": 120, "y": 50}],
                    },
                },
                {"name": "OuterChamfer", "type": "chamfer", "params": {"size": 1, "targets": "all_outer_edges"}},
            ],
            "outputs": ["SLDPRT"],
            "unsupported_features": [],
            "risks": [],
            "needs_confirmation": False,
            "review_plan": {"checks": ["features_created"]},
        }


class FakeDeepSeekWireMeshProvider:
    name = "deepseek"

    def generate_design_json(self, _prompt: str, _schema: dict) -> dict:
        return {
            "parameters": {
                "wire_diameter_mm": 4,
                "longitudinal_count": 6,
                "transverse_count": 7,
                "longitudinal_length_mm": 338,
                "transverse_coverage_height_mm": 310,
                "transverse_pitches_mm": [45, 54, 54, 54, 54, 45],
                "arc_radius_mm": 269,
                "arc_length_mm": 276.4,
                "overall_depth_mm": 276.4,
            },
            "features": [{
                "name": "WireMesh",
                "type": "weldment",
                "params": {
                    "mode": "new_model",
                    "standard": "iso",
                    "profile_type": "solid_round",
                    "profile_configuration": "D4",
                    "wire_mesh": {
                        "wire_diameter_mm": 4,
                        "arc_radius_mm": 269,
                        "arc_length_mm": 276.4,
                        "overall_height_mm": 310,
                        "mesh_height_mm": 310,
                        "end_pitch_mm": 45,
                        "middle_pitch_mm": 54,
                        "overall_depth_mm": 276.4,
                        "longitudinal_count": 6,
                        "transverse_count": 7,
                    },
                },
                "required": True,
            }],
            "outputs": ["SLDPRT"],
            "unsupported_features": [],
            "risks": [],
            "needs_confirmation": False,
            "review_plan": {"checks": ["wire_mesh_geometry"]},
        }


class FakeDeepSeekWrongFlatBarCountsProvider(FakeDeepSeekWireMeshProvider):
    def generate_design_json(self, prompt: str, schema: dict) -> dict:
        design = super().generate_design_json(prompt, schema)
        design["features"][0]["params"]["wire_mesh"]["flat_bar_frame"] = {
            "width_mm": 20,
            "thickness_mm": 3,
            "orientation": "edge_on",
            "support_side": "below_wire",
            "contact": "tangent",
            "curved_rail_count": 1,
            "curved_rail_radius_mm": 269,
            "straight_support_count": 6,
            "straight_support_length_mm": 310,
            "mounting_tabs": {"count": 1, "width_mm": 20, "thickness_mm": 3, "height_mm": 35},
        }
        return design


class FakeDeepSeekSwappedWireMeshProvider(FakeDeepSeekWrongFlatBarCountsProvider):
    def generate_design_json(self, prompt: str, schema: dict) -> dict:
        design = super().generate_design_json(prompt, schema)
        design["parameters"] = {}
        mesh = design["features"][0]["params"]["wire_mesh"]
        mesh.update({
            "wire_diameter_mm": 4,
            "arc_radius_mm": 269,
            "arc_length_mm": 338,
            "overall_height_mm": 310,
            "mesh_height_mm": 276.4,
            "longitudinal_count": 7,
            "transverse_count": 6,
        })
        return design


class DeepSeekProviderTests(unittest.TestCase):
    def test_deepseek_design_is_normalized_for_production(self) -> None:
        prompt = "Create a 140x80x10 mm plate with R5 corners and a patterned center hole. Only save SLDPRT."
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekProvider()).plan(prompt)

        self.assertEqual(design["parameters"]["length"], 140.0)
        self.assertEqual(design["parameters"]["width"], 80.0)
        self.assertEqual(design["parameters"]["thickness"], 10.0)
        self.assertEqual(design["parameters"]["material"], "6061 aluminum")
        features = {item["type"]: item["params"] for item in design["features"]}
        self.assertEqual(features["fillet"]["radius"], 5)
        self.assertEqual(features["fillet"]["target"], "four_outer_corners")
        self.assertEqual(features["through_hole"]["position"], "center")
        self.assertEqual(features["through_hole"]["count"], 1)
        self.assertEqual(features["linear_pattern"]["direction_1"], "+x")
        self.assertEqual(design["requested_stages"], ["model_3d"])
        self.assertEqual(design["execution_policy"]["expected_outputs"], ["SLDPRT"])

    def test_deepseek_absolute_coordinates_are_normalized(self) -> None:
        prompt = "Create the described mounting base and only save SLDPRT."
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekCoordinateProvider()).plan(prompt)

        features = {item["name"]: item["params"] for item in design["features"]}
        self.assertEqual(features["CornerHoles"]["placement"], "corner_offsets")
        self.assertEqual(features["CornerHoles"]["edge_offsets_mm"], {"x": 15.0, "y": 15.0})
        self.assertEqual(features["CenterHole"]["position"], "center")
        self.assertEqual(features["Boss"]["position_xy"], [0.0, 0.0])
        self.assertEqual(features["Pocket"]["position_xy"], [0.0, 0.0])
        self.assertEqual(features["SideSlots"]["orientation"], "y")
        self.assertEqual(features["SideSlots"]["centerline_offsets"], [-40.0, 40.0])
        self.assertEqual(features["OuterChamfer"]["size"], 1)
        self.assertEqual(design["execution_policy"]["unexecutable_required_features"], [])
        self.assertFalse(design["needs_confirmation"])

    def test_deepseek_wire_mesh_dimensions_are_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekWireMeshProvider()).plan(
                "Create the specified wire mesh and only save SLDPRT."
            )

        params = design["features"][0]["params"]
        mesh = params["wire_mesh"]
        self.assertEqual(mesh["overall_height_mm"], 338)
        self.assertEqual(mesh["mesh_height_mm"], 310)
        self.assertEqual(mesh["end_pitch_mm"], 45)
        self.assertEqual(mesh["middle_pitch_mm"], 54)
        self.assertAlmostEqual(mesh["overall_depth_mm"], 40.329685569365154)
        self.assertEqual(mesh["layer_reference"], "drawing_side_profile")
        self.assertEqual(mesh["inner_surface_radius_mm"], 269)
        self.assertEqual(mesh["transverse_centerline_radius_mm"], 271)
        self.assertEqual(mesh["outer_surface_radius_mm"], 273)
        self.assertEqual(mesh["longitudinal_center_locus_radius_mm"], 275)
        self.assertEqual(mesh["layering"], "transverse_arc_wires_on_top")
        self.assertEqual(mesh["wire_contact"], "tangent")
        self.assertEqual(mesh["layer_center_distance_mm"], 4)
        self.assertTrue(mesh["extended_end_wires_only"])
        self.assertTrue(design["parameters"]["transverse_arc_wires_on_top"])
        self.assertFalse(design["parameters"]["longitudinal_outer"])
        self.assertFalse(design["parameters"]["transverse_inner"])
        self.assertEqual(params["wire_mesh_parameter_sources"]["overall_height_mm"], "longitudinal_length_mm")
        self.assertEqual(design["unsupported_features"], [])
        self.assertFalse(design["needs_confirmation"])

    def test_deepseek_omitted_flat_bar_frame_is_recovered_from_explicit_brief(self) -> None:
        prompt = (
            "创建弧形钢丝网多实体焊件。扁铁规格为宽20mm、厚3mm。"
            "沿网片上下边缘各创建1根R269弧形扁铁；"
            "在6根纵向钢丝下方分别创建1根长度310mm的直扁铁。"
            "两端弧形扁铁增加20×3×35mm的竖直安装耳片。只保存SLDPRT。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekWireMeshProvider()).plan(prompt)

        mesh = design["features"][0]["params"]["wire_mesh"]
        frame = mesh["flat_bar_frame"]
        self.assertEqual(frame["width_mm"], 20)
        self.assertEqual(frame["thickness_mm"], 3)
        self.assertEqual(frame["curved_rail_count"], 2)
        self.assertEqual(frame["straight_support_count"], 6)
        self.assertEqual(frame["straight_support_length_mm"], 310)
        self.assertEqual(frame["mounting_tabs"]["count"], 4)
        self.assertEqual(frame["mounting_tabs"]["height_mm"], 35)
        self.assertEqual(design["execution_policy"]["unexecutable_required_features"], [])

    def test_flat_bar_request_without_dimensions_never_silently_drops_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekWireMeshProvider()).plan(
                "Create the wire mesh, add flat bar supports, and only save SLDPRT."
            )

        self.assertTrue(design["needs_confirmation"])
        self.assertTrue(any(
            item.get("type") == "wire_mesh_flat_bar_parameters_missing"
            for item in design["unsupported_features"]
        ))

    def test_explicit_each_and_both_ends_override_wrong_llm_counts(self) -> None:
        prompt = (
            "扁铁规格为宽20mm、厚3mm，沿网片上下边缘各创建1根R269弧形扁铁；"
            "在6根纵向钢丝下方分别创建1根长度310mm的直扁铁；"
            "两端弧形扁铁增加20×3×35mm的竖直安装耳片。只保存SLDPRT。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(
                Path(temporary),
                provider=FakeDeepSeekWrongFlatBarCountsProvider(),
            ).plan(prompt)

        frame = design["features"][0]["params"]["wire_mesh"]["flat_bar_frame"]
        self.assertEqual(frame["curved_rail_count"], 2)
        self.assertEqual(frame["mounting_tabs"]["count"], 4)
        self.assertEqual(design["execution_policy"]["allowed_skills"], ["weldment", "save_sldprt"])
        self.assertEqual(design["execution_policy"]["unexecutable_required_features"], [])

    def test_explicit_wire_mesh_contract_overrides_swapped_llm_fields(self) -> None:
        prompt = (
            "创建钢丝瓦片门三维网片零件，圆钢轮廓D4。钢丝直径4mm，纵向钢丝6根，"
            "横向圆弧钢丝7根；只有最左和最右两根纵向钢丝总长338mm，其余4根纵向钢丝长度310mm。"
            "横丝中心距从上到下为45、54、54、54、54、45mm。横向钢丝为连续R269圆弧，"
            "圆弧总长276.4mm。扁铁规格为宽20mm、厚3mm；沿网片上下边缘各创建1根R269弧形扁铁；"
            "在6根纵向钢丝下方分别创建1根长度310mm的直扁铁；"
            "两端弧形扁铁增加20×3×35mm的竖直安装耳片。只保存SLDPRT。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(
                Path(temporary),
                provider=FakeDeepSeekSwappedWireMeshProvider(),
            ).plan(prompt)

        mesh = design["features"][0]["params"]["wire_mesh"]
        self.assertEqual(mesh["wire_diameter_mm"], 4)
        self.assertEqual(mesh["longitudinal_count"], 6)
        self.assertEqual(mesh["transverse_count"], 7)
        self.assertEqual(mesh["overall_height_mm"], 338)
        self.assertEqual(mesh["mesh_height_mm"], 310)
        self.assertEqual(mesh["arc_radius_mm"], 269)
        self.assertEqual(mesh["arc_length_mm"], 276.4)
        self.assertEqual(mesh["end_pitch_mm"], 45)
        self.assertEqual(mesh["middle_pitch_mm"], 54)
        self.assertAlmostEqual(mesh["overall_depth_mm"], 40.329685569365154)
        self.assertEqual(mesh["flat_bar_frame"]["curved_rail_count"], 2)
        self.assertEqual(mesh["flat_bar_frame"]["straight_support_count"], 6)
        self.assertEqual(mesh["flat_bar_frame"]["mounting_tabs"]["count"], 4)
        self.assertEqual(design["execution_policy"]["allowed_skills"], ["weldment", "save_sldprt"])
        self.assertEqual(design["execution_policy"]["unexecutable_required_features"], [])

    def test_segmented_basket_and_clip_contract_is_recovered_from_brief(self) -> None:
        prompt = (
            "创建钢丝网片和扁铁组合骨架。扁铁规格为宽20mm、厚3mm，"
            "沿网片上下边缘各创建1根R269分段扁铁；"
            "在6根纵向钢丝下方分别创建1根长度310mm的直扁铁。"
            "每个扁铁与横向钢丝交点设置卡扣规格8×2mm，卡扣装配间隙0.3mm。"
            "两端弧形扁铁增加20×3×35mm的竖直安装耳片。只保存SLDPRT。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekWireMeshProvider()).plan(prompt)

        frame = design["features"][0]["params"]["wire_mesh"]["flat_bar_frame"]
        self.assertEqual(frame["construction_style"], "segmented_basket")
        self.assertEqual(frame["rail_segment_count"], 5)
        self.assertEqual(frame["wire_clips"]["width_mm"], 8)
        self.assertEqual(frame["wire_clips"]["thickness_mm"], 2)
        self.assertEqual(frame["wire_clips"]["clearance_mm"], 0.3)
        self.assertEqual(design["execution_policy"]["allowed_skills"], ["weldment", "save_sldprt"])
        self.assertEqual(design["execution_policy"]["unexecutable_required_features"], [])

    def test_flat_bar_rectangular_wire_seats_replace_external_clips(self) -> None:
        prompt = (
            "创建钢丝网片和扁铁组合骨架。扁铁规格为宽20mm、厚3mm，"
            "沿网片上下边缘各创建1根R269分段扁铁；"
            "在原6根纵向钢丝位置分别创建1根长度310mm的直扁铁并替代纵丝。"
            "每根直扁铁上缘在横向D4钢丝交点开矩形小凹槽，让横丝放入槽内，"
            "不增加外置卡扣。只保存SLDPRT。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekWireMeshProvider()).plan(prompt)

        mesh = design["features"][0]["params"]["wire_mesh"]
        frame = mesh["flat_bar_frame"]
        notches = frame["wire_seat_notches"]
        self.assertTrue(frame["replace_longitudinal_wires"])
        self.assertEqual(notches["style"], "shallow_rectangular_edge_notch")
        self.assertEqual(notches["placement"], "all_support_transverse_intersections")
        self.assertEqual(notches["width_mm"], 4.6)
        self.assertEqual(notches["depth_mm"], 2.0)
        self.assertEqual(notches["parameter_source"], "derived_from_wire_diameter")
        self.assertNotIn("wire_clips", frame)
        self.assertEqual(design["execution_policy"]["allowed_skills"], ["weldment", "save_sldprt"])
        self.assertEqual(design["execution_policy"]["unexecutable_required_features"], [])

    def test_clip_request_without_dimensions_requires_confirmation(self) -> None:
        prompt = (
            "创建钢丝网片。扁铁规格为宽20mm、厚3mm，在6根纵向钢丝下方分别创建1根长度310mm的直扁铁，"
            "沿网片上下边缘各创建1根R269弧形扁铁，并在所有交点增加卡扣。只保存SLDPRT。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            design = DesignPlanner(Path(temporary), provider=FakeDeepSeekWireMeshProvider()).plan(prompt)

        self.assertTrue(design["needs_confirmation"])
        self.assertTrue(any(
            item.get("type") == "wire_mesh_clip_parameters_missing"
            for item in design["unsupported_features"]
        ))

    def test_auto_provider_prefers_configured_deepseek(self) -> None:
        values = {
            "CAD_AGENT_DEFAULT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "unit-test-key",
            "CAD_AGENT_DEEPSEEK_MODEL": "deepseek-v4-pro",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, values, clear=False):
            provider, status = ProviderRegistry(VibeCADSkill(Path(temporary))).resolve("auto")

        self.assertIsInstance(provider, DeepSeekProvider)
        self.assertEqual(status.id, "deepseek")
        self.assertTrue(status.configured)
        self.assertTrue(status.available)
        self.assertEqual(status.model, "deepseek-v4-pro")

    def test_auto_provider_falls_back_when_cloud_keys_are_missing(self) -> None:
        values = {
            "CAD_AGENT_DEFAULT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "GEMINI_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "QWEN_API_KEY": "",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, values, clear=False):
            provider, status = ProviderRegistry(VibeCADSkill(Path(temporary))).resolve("auto")
        self.assertEqual(provider.name, "local-rule-based")
        self.assertEqual(status.id, "local_fallback")

    def test_api_keys_are_redacted(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-test-secret-value"}, clear=False):
            value = redact_secrets("request failed for unit-test-secret-value")
        self.assertNotIn("unit-test-secret-value", value)
        self.assertIn("redacted", value)

    def test_cad_execution_lock_blocks_a_second_holder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False):
            first = CADExecutionLock(timeout_s=0.2, poll_s=0.05)
            second = CADExecutionLock(timeout_s=0.2, poll_s=0.05)
            with first:
                with self.assertRaises(TimeoutError):
                    second.acquire()
            with second:
                self.assertIsNotNone(second.handle)


if __name__ == "__main__":
    unittest.main()
