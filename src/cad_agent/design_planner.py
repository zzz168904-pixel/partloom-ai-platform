from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .brain_models import ModelProvider
from .cad_ir import CADIRCompiler
from .engineering_knowledge import EngineeringKnowledgeBase
from .model_providers import LocalRuleBasedProvider
from .stage_planner import apply_stage_plan, infer_stage_plan
from .vibecad_skill import VibeCADSkill


DESIGN_SCHEMA: dict[str, Any] = {
    "schema_version": "vibecad.design.v1",
    "required": [
        "parameters",
        "features",
        "datums",
        "tolerances",
        "assembly_relations",
        "review_plan",
    ],
    "feature_types": [
        "base_plate",
        "boss",
        "pocket",
        "through_hole",
        "threaded_hole",
        "bolt_circle_pattern",
        "slot",
        "fillet",
        "chamfer",
        "linear_pattern",
        "circular_pattern",
        "mirror",
        "side_boss",
        "side_hole",
        "gear",
        "gear_pair",
        "revolve",
        "profile_extrude",
        "sweep",
        "loft",
        "sheet_metal",
        "weldment",
        "freeform_surface",
        "shell",
        "rib",
        "draft",
        "reference_geometry",
        "equation",
        "configuration",
        "assembly_mate",
        "drawing",
        "section_view",
        "detail_view",
        "autocad_annotation",
    ],
}

DIRECT_GUI_COMMAND_PREFIX = "GUI_CAD_COMMAND_V1"


class DesignPlanner:
    """Produce normalized Design JSON from a user prompt."""

    def __init__(self, output_root: Path, provider: ModelProvider | None = None) -> None:
        self.output_root = output_root
        self.vibecad = VibeCADSkill(output_root)
        self.provider = provider or LocalRuleBasedProvider(self.vibecad)
        self.engineering_knowledge = EngineeringKnowledgeBase()

    @property
    def provider_name(self) -> str:
        return self.provider.name

    def plan(self, prompt: str, stage_mode: str = "auto") -> dict[str, Any]:
        design = self._direct_gui_command_design(prompt)
        if design is None:
            schema = {**DESIGN_SCHEMA, "engineering_knowledge": self.engineering_knowledge.planner_context()}
            design = self.provider.generate_design_json(prompt, schema)
        return self._normalize_design(design, prompt, stage_mode=stage_mode)

    @classmethod
    def _direct_gui_command_design(cls, prompt: str) -> dict[str, Any] | None:
        """Parse a fully specified Codex-to-GUI command without invoking an LLM."""
        stripped = str(prompt or "").strip()
        if not stripped:
            return None
        lines = stripped.splitlines()
        if lines[0].strip().upper() != DIRECT_GUI_COMMAND_PREFIX:
            return None

        brief = "\n".join(lines[1:]).strip()
        common: dict[str, Any] = {
            "schema_version": DESIGN_SCHEMA["schema_version"],
            "source_brief": stripped,
            "parameters": {"unit": "mm"},
            "features": [],
            "outputs": ["SLDPRT"],
            "datums": [],
            "tolerances": [],
            "assembly_relations": [],
            "assumptions": [],
            "risk_register": [],
            "risks": [],
            "unsupported_features": [],
            "needs_confirmation": False,
            "review_plan": {
                "expected_outputs": ["SLDPRT"],
                "views": ["isometric", "front", "right"],
                "checks": ["parameters_match_brief", "feature_count_matches", "outputs_exist"],
            },
            "llm_provider": {
                "id": "direct_gui_command",
                "model": "none",
                "response_format": "deterministic",
            },
            "planning_mode": "direct_gui_command",
        }

        if not re.search(r"钢丝网|钢丝瓦片门|网片|wire\s*mesh", brief, re.IGNORECASE):
            common["needs_confirmation"] = True
            common["confirmation_reason"] = "GUI_CAD_COMMAND_V1 currently requires a supported explicit feature contract."
            common["unsupported_features"] = [{
                "type": "direct_gui_command_feature_not_supported",
                "required": True,
                "reason": "The direct command does not contain a supported explicit feature contract.",
            }]
            return common

        mesh = cls._parse_wire_mesh_contract(brief)
        pitches = mesh.pop("transverse_pitches_mm", None)
        if isinstance(pitches, list) and len(pitches) == 6:
            symmetric = all(math.isclose(float(pitches[index]), float(pitches[-index - 1]), abs_tol=1e-6)
                            for index in range(3))
            middle_equal = all(math.isclose(float(value), float(pitches[1]), abs_tol=1e-6) for value in pitches[1:5])
            if symmetric and middle_equal:
                mesh["end_pitch_mm"] = float(pitches[0])
                mesh["middle_pitch_mm"] = float(pitches[1])

        required = (
            "wire_diameter_mm",
            "arc_radius_mm",
            "arc_length_mm",
            "overall_height_mm",
            "mesh_height_mm",
            "end_pitch_mm",
            "middle_pitch_mm",
            "overall_depth_mm",
            "longitudinal_count",
            "transverse_count",
            "longitudinal_chord_pitches_mm",
            "outer_surface_chord_mm",
            "transverse_profile_depth_mm",
        )
        missing = [key for key in required if mesh.get(key) is None]
        if missing:
            common["needs_confirmation"] = True
            common["confirmation_reason"] = "Direct wire-mesh command is missing required parameters: " + ", ".join(missing)
            common["unsupported_features"] = [{
                "type": "direct_gui_command_parameters_missing",
                "required": True,
                "missing_parameters": missing,
                "reason": common["confirmation_reason"],
            }]
            return common

        mesh.update({
            "layering": "transverse_arc_wires_on_top",
            "layer_reference": "drawing_side_profile",
            "arc_radius_semantic": "inner_surface",
            "arc_length_semantic": "outer_surface_arc",
            "inner_surface_radius_mm": float(mesh["arc_radius_mm"]),
            "transverse_centerline_radius_mm": float(mesh["arc_radius_mm"]) + float(mesh["wire_diameter_mm"]) / 2.0,
            "outer_surface_radius_mm": float(mesh["arc_radius_mm"]) + float(mesh["wire_diameter_mm"]),
            "longitudinal_center_locus_radius_mm": float(mesh["arc_radius_mm"]) + 1.5 * float(mesh["wire_diameter_mm"]),
            "transverse_layer_radius_mm": float(mesh["arc_radius_mm"]) + float(mesh["wire_diameter_mm"]) / 2.0,
            "longitudinal_layer_radius_mm": float(mesh["arc_radius_mm"]) + 1.5 * float(mesh["wire_diameter_mm"]),
            "side_profile_sag_direction": "negative_z_endpoints",
            "transverse_profile_position": "upper",
            "longitudinal_profile_position": "lower",
            "wire_contact": "tangent",
            "layer_center_distance_mm": float(mesh["wire_diameter_mm"]),
            "extended_end_wires_only": True,
            "continuous_transverse_wires": True,
        })
        longitudinal_count = int(mesh["longitudinal_count"])
        mesh["longitudinal_lengths_mm"] = [
            float(mesh["overall_height_mm"]) if index in {0, longitudinal_count - 1}
            else float(mesh["mesh_height_mm"])
            for index in range(longitudinal_count)
        ]
        material_match = re.search(r"材料(?:为|=|:|：)?\s*([^。；;\n]+)", brief, re.IGNORECASE)
        material = material_match.group(1).strip() if material_match else ""
        common["parameters"] = {"unit": "mm", "material": material, **mesh}
        common["features"] = [{
            "name": "CurvedWireMesh",
            "type": "weldment",
            "params": {
                "mode": "new_model",
                "standard": "iso",
                "profile_type": "solid_round",
                "profile_configuration": f"D{float(mesh['wire_diameter_mm']):g}",
                "wire_mesh": mesh,
            },
            "required": True,
        }]
        common["review_plan"] = {
            "expected_outputs": ["SLDPRT"],
            "views": ["isometric", "front", "right"],
            "checks": [
                "six_longitudinal_wires",
                "seven_continuous_transverse_arcs",
                "end_wires_338_inner_wires_310",
                "standard_top_view_matches_dwg_side_profile",
                "r269_inner_r271_center_r273_outer_r275_longitudinal_locus",
                "transverse_arcs_above_longitudinal_wires_and_tangent",
                "only_sldprt_output",
            ],
        }
        return common

    def _normalize_design(self, design: dict[str, Any], prompt: str, stage_mode: str = "auto") -> dict[str, Any]:
        normalized = dict(design)
        stage_plan = infer_stage_plan(prompt, stage_mode)
        normalized.setdefault("schema_version", DESIGN_SCHEMA["schema_version"])
        normalized.setdefault("source_brief", prompt.strip())
        normalized.setdefault("task_type", stage_plan.get("task_type", "model_3d"))
        normalized.setdefault("parameters", {})
        normalized.setdefault("features", [])
        normalized.setdefault("datums", [])
        normalized.setdefault("tolerances", [])
        normalized.setdefault("assembly_relations", [])
        normalized.setdefault("assumptions", [])
        normalized.setdefault("risk_register", [])
        normalized.setdefault("risks", [])
        normalized.setdefault("unsupported_features", [])
        normalized.setdefault("needs_confirmation", False)
        normalized.setdefault(
            "review_plan",
            {
                "expected_outputs": ["design_plan_json"],
                "views": [],
                "checks": ["schema_fields_present"],
            },
        )
        if not normalized["features"]:
            normalized["features"] = self._features_from_structured_parameters(normalized["parameters"])
        normalized = CADIRCompiler().compile(normalized).design
        normalized["features"] = [self._normalize_feature(feature) for feature in normalized["features"]]
        normalized["parameters"] = self._normalize_parameters(normalized["parameters"], normalized["features"])
        normalized["features"] = self._normalize_feature_coordinates(normalized["features"], normalized["parameters"])
        normalized["features"] = self._reconcile_wire_mesh_parameters(
            normalized["features"],
            normalized["parameters"],
            str(normalized.get("source_brief") or prompt),
        )
        normalized["features"], revolve_risks = self._reconcile_named_revolve_profiles(
            normalized["features"],
            normalized["parameters"],
            str(normalized.get("source_brief") or prompt),
        )
        intent_risks = self._feature_family_alignment_risks(
            str(normalized.get("source_brief") or prompt),
            normalized["features"],
        )
        flat_bar_risks = self._wire_mesh_flat_bar_alignment_risks(
            str(normalized.get("source_brief") or prompt),
            normalized["features"],
        )
        planning_risks = [*revolve_risks, *intent_risks, *flat_bar_risks]
        if planning_risks:
            normalized["risks"].extend(planning_risks)
            normalized["unsupported_features"].extend(
                {
                    "type": str(item.get("type") or "planning_semantic_conflict"),
                    "required": True,
                    "reason": str(item.get("description") or "The planned feature family requires confirmation."),
                }
                for item in planning_risks
            )
            normalized["needs_confirmation"] = True
        confirmation_risks = [
            risk
            for risk in [*normalized.get("risks", []), *normalized.get("risk_register", [])]
            if isinstance(risk, dict) and risk.get("needs_confirmation")
        ]
        if confirmation_risks:
            normalized["needs_confirmation"] = True
            if not normalized.get("confirmation_reason"):
                normalized["confirmation_reason"] = str(
                    confirmation_risks[0].get("description")
                    or confirmation_risks[0].get("risk")
                    or "Planner identified an ambiguous design risk."
                )
        normalized = apply_stage_plan(normalized, stage_plan)
        normalized = self.engineering_knowledge.enrich_design(normalized)
        return CADIRCompiler().compile(normalized).design

    @staticmethod
    def _feature_family_alignment_risks(
        source_brief: str,
        features: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        text = source_brief.strip().lower()
        risks: list[dict[str, Any]] = []
        if "�" in text or (text.count("?") >= 6 and text.count("?") / max(1, len(text)) >= 0.15):
            risks.append(
                {
                    "type": "planning_input_corrupted",
                    "description": "The design brief contains corrupted replacement characters and cannot be executed safely.",
                    "needs_confirmation": True,
                }
            )
            return risks

        feature_types = {str(feature.get("type") or "") for feature in features}
        explicit_revolve = any(
            token in text
            for token in (
                "轴对称",
                "旋转360",
                "旋转 360",
                "绕中心轴旋转",
                "旋转成型",
                "revolve",
                "axisymmetric",
            )
        )
        if explicit_revolve and "revolve" not in feature_types:
            risks.append(
                {
                    "type": "feature_family_mismatch",
                    "description": "The brief explicitly requires an axisymmetric revolve, but the plan contains no revolve feature.",
                    "needs_confirmation": True,
                    "expected_feature": "revolve",
                    "planned_features": sorted(feature_types),
                }
            )
        explicit_families = (
            ("sheet_metal", ("sheet metal", "base flange", "edge flange", "sketched bend", "hem", "jog", "lofted bend", "\u94a3\u91d1", "\u57fa\u4f53\u6cd5\u5170", "\u8fb9\u7ebf\u6cd5\u5170", "\u8349\u56fe\u6298\u5f2f", "\u6298\u5f2f\u7ebf", "\u6298\u8fb9", "\u5305\u8fb9", "\u8f6c\u6298", "\u9519\u4f4d\u6298\u5f2f", "\u653e\u6837\u6298\u5f2f")),
            ("weldment", ("weldment", "structural member", "welded frame", "wire mesh", "\u710a\u4ef6", "\u7ed3\u6784\u6784\u4ef6", "\u710a\u63a5\u6846\u67b6", "\u7f51\u7247", "\u94a2\u4e1d\u7f51", "\u94a2\u4e1d\u95e8")),
            ("freeform_surface", ("freeform surface", "fill surface", "\u81ea\u7531\u66f2\u9762", "\u586b\u5145\u66f2\u9762")),
        )
        for expected_feature, tokens in explicit_families:
            if any(token in text for token in tokens) and expected_feature not in feature_types:
                risks.append(
                    {
                        "type": "feature_family_mismatch",
                        "description": (
                            f"The brief explicitly requires {expected_feature}, but the plan contains no "
                            f"{expected_feature} feature."
                        ),
                        "needs_confirmation": True,
                        "expected_feature": expected_feature,
                        "planned_features": sorted(feature_types),
                    }
                )

        sheet_features = [feature for feature in features if feature.get("type") == "sheet_metal"]
        requests_opposite_flanges = any(
            token in text
            for token in ("both sides", "opposite sides", "positive and", "\u4e24\u4fa7", "\u4e24\u8fb9\u5404", "\u6b63\u8d1f\u4e24\u4fa7")
        )
        if requests_opposite_flanges and sheet_features:
            edge_flanges = sheet_features[0].get("params", {}).get("edge_flanges", [])
            if not isinstance(edge_flanges, list) or len(edge_flanges) < 2:
                risks.append(
                    {
                        "type": "sheet_metal_edge_scope_mismatch",
                        "description": "The brief requests opposite-side edge flanges, but fewer than two flanges were planned.",
                        "needs_confirmation": True,
                        "expected_feature": "sheet_metal",
                    }
                )
        return risks

    @staticmethod
    def _wire_mesh_flat_bar_alignment_risks(
        source_brief: str,
        features: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        text = str(source_brief or "")

        def positively_requests(pattern: str) -> bool:
            """Return true only when at least one matching term is not negated in its clause."""
            negation = re.compile(
                r"(?:禁止|不得|不可|不允许|不要|无需|无须|不再|不增加|不添加|不附加|"
                r"不创建|不生成|不使用|不包含|没有|不是|排除)[^。；;\n]{0,24}$",
                re.IGNORECASE,
            )
            for match in re.finditer(pattern, text, re.IGNORECASE):
                prefix = text[max(0, match.start() - 32):match.start()]
                if not negation.search(prefix):
                    return True
            return False

        requests_frame = positively_requests(r"扁铁|扁钢|flat\s*bar")
        requests_notches = positively_requests(
            r"矩形(?:小)?凹槽|矩形槽|卡槽|槽口|开(?:出|设|加工)?[^。；;\n]{0,12}?槽|wire[-\s]*seat\s*notch",
        )
        requests_clips = positively_requests(
            r"外置卡扣|独立卡扣|附加卡扣|(?:卡扣|扣片|压扣)(?:规格)?|wire\s*clip|retainer"
        )
        if not requests_frame and not requests_clips and not requests_notches:
            return []
        wire_mesh_features = [
            feature
            for feature in features
            if feature.get("type") == "weldment"
            and isinstance((feature.get("params") or {}).get("wire_mesh"), dict)
        ]
        has_frame = any(
            isinstance((feature.get("params") or {}).get("wire_mesh", {}).get("flat_bar_frame"), dict)
            for feature in wire_mesh_features
        )
        has_clips = any(
            isinstance((feature.get("params") or {}).get("wire_mesh", {}).get("flat_bar_frame", {}).get("wire_clips"), dict)
            for feature in wire_mesh_features
        )
        has_notches = any(
            isinstance(
                (feature.get("params") or {}).get("wire_mesh", {}).get("flat_bar_frame", {}).get("wire_seat_notches"),
                dict,
            )
            for feature in wire_mesh_features
        )
        risks: list[dict[str, Any]] = []
        if requests_frame and not has_frame:
            risks.append({
                "type": "wire_mesh_flat_bar_parameters_missing",
                "description": (
                    "The brief requests flat-bar supports, but width, thickness, rail/support count, "
                    "support length, or mounting-tab geometry could not be mapped to flat_bar_frame."
                ),
                "needs_confirmation": True,
                "expected_feature": "wire_mesh.flat_bar_frame",
            })
        if requests_clips and not has_clips:
            risks.append({
                "type": "wire_mesh_clip_parameters_missing",
                "description": (
                    "The brief requests wire-retaining clips, but clip width, thickness, clearance, "
                    "or intersection placement could not be mapped to flat_bar_frame.wire_clips."
                ),
                "needs_confirmation": True,
                "expected_feature": "wire_mesh.flat_bar_frame.wire_clips",
            })
        if requests_notches and not has_notches:
            risks.append({
                "type": "wire_mesh_notch_parameters_missing",
                "description": (
                    "The brief requests rectangular wire-seat notches in the flat bars, but the notch placement "
                    "or D4-derived width/depth could not be mapped to flat_bar_frame.wire_seat_notches."
                ),
                "needs_confirmation": True,
                "expected_feature": "wire_mesh.flat_bar_frame.wire_seat_notches",
            })
        return risks

    @staticmethod
    def _reconcile_named_revolve_profiles(
        features: list[dict[str, Any]],
        parameters: dict[str, Any],
        source_brief: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Make an LLM revolve profile agree with explicit named dimensions."""

        from .revolve_skill import RevolveSkill

        named = RevolveSkill.profile_from_named_dimensions(parameters, source_brief)
        if not named.get("applicable"):
            return features, []
        if not named.get("success"):
            return features, [
                {
                    "type": "revolve_dimension_conflict",
                    "description": str(named.get("message") or "Named revolve dimensions are incomplete."),
                    "needs_confirmation": True,
                    "missing": list(named.get("missing", [])),
                }
            ]

        indexes = [
            index
            for index, feature in enumerate(features)
            if feature.get("type") == "revolve"
            and str(feature.get("params", {}).get("operation") or "base").lower() == "base"
        ]
        if len(indexes) != 1:
            return features, [
                {
                    "type": "revolve_dimension_conflict",
                    "description": "Explicit flange/sleeve dimensions require exactly one base revolve feature.",
                    "needs_confirmation": True,
                }
            ]

        result = [dict(feature) for feature in features]
        index = indexes[0]
        feature = dict(result[index])
        params = dict(feature.get("params") or {})
        original_profile = params.get("profile") or params.get("profile_points") or params.get("segments")
        canonical_profile = [list(point) for point in named["profile_points_mm"]]
        profile_result = RevolveSkill._normalize_profile(params)
        already_matches = bool(
            profile_result.get("success")
            and RevolveSkill.validate_profile_against_design_dimensions(
                profile_result["profile_points_mm"], named["design_dimensions"]
            ).get("success")
        )
        params["profile"] = canonical_profile
        params.pop("profile_points", None)
        params.pop("segments", None)
        params.pop("shaft_segments", None)
        params["profile_source"] = "explicit_named_dimensions"
        params["design_dimensions"] = dict(named["design_dimensions"])
        params["profile_reconciliation"] = {
            "applied": not already_matches,
            "source": "explicit_named_dimensions",
            "original_profile": original_profile if not already_matches else None,
        }
        feature["params"] = params
        result[index] = feature
        return result, []

    @staticmethod
    def _reconcile_wire_mesh_parameters(
        features: list[dict[str, Any]],
        parameters: dict[str, Any],
        source_brief: str = "",
    ) -> list[dict[str, Any]]:
        """Keep nested wire-mesh geometry aligned with explicit drawing parameters."""

        aliases = {
            "wire_diameter_mm": ("wire_diameter_mm",),
            "longitudinal_count": ("longitudinal_count",),
            "transverse_count": ("transverse_count",),
            "overall_height_mm": ("longitudinal_total_length_mm", "longitudinal_length_mm"),
            "mesh_height_mm": ("transverse_coverage_height_mm", "mesh_height_mm"),
            "arc_radius_mm": ("arc_radius_mm",),
            "arc_length_mm": ("arc_total_length_mm", "arc_length_mm"),
            "overall_depth_mm": ("mesh_total_depth_mm",),
            "outer_surface_chord_mm": ("wire_mesh_outer_chord_mm", "outer_surface_chord_mm"),
            "transverse_profile_depth_mm": ("wire_mesh_profile_depth_mm", "transverse_profile_depth_mm"),
        }
        result: list[dict[str, Any]] = []
        for item in features:
            feature = dict(item)
            if feature.get("type") != "weldment":
                result.append(feature)
                continue
            params = dict(feature.get("params") or {})
            mesh = params.get("wire_mesh")
            if not isinstance(mesh, dict):
                result.append(feature)
                continue
            canonical = dict(mesh)
            reconciled: dict[str, Any] = {}
            for target_key, source_keys in aliases.items():
                source_key = next(
                    (key for key in source_keys if parameters.get(key) not in (None, "")),
                    None,
                )
                if source_key is not None:
                    canonical[target_key] = parameters[source_key]
                    reconciled[target_key] = source_key
            explicit_mesh = DesignPlanner._parse_wire_mesh_contract(source_brief)
            for target_key, value in explicit_mesh.items():
                if target_key == "transverse_pitches_mm":
                    continue
                canonical[target_key] = value
                reconciled[target_key] = f"source_brief:{target_key}"
            parameter_targets = {
                "wire_diameter_mm": "wire_diameter_mm",
                "longitudinal_count": "longitudinal_count",
                "transverse_count": "transverse_count",
                "overall_height_mm": "longitudinal_length_mm",
                "mesh_height_mm": "transverse_coverage_height_mm",
                "arc_radius_mm": "arc_radius_mm",
                "arc_length_mm": "arc_length_mm",
            }
            for mesh_key, parameter_key in parameter_targets.items():
                if mesh_key in explicit_mesh:
                    parameters[parameter_key] = explicit_mesh[mesh_key]
            pitch_source = next(
                (
                    key
                    for key in ("transverse_center_distances_mm", "transverse_pitches_mm")
                    if isinstance(parameters.get(key), list)
                ),
                None,
            )
            pitches = explicit_mesh.get("transverse_pitches_mm")
            if isinstance(pitches, list):
                pitch_source = "source_brief:transverse_pitches_mm"
                parameters["transverse_pitches_mm"] = pitches
            else:
                pitches = parameters.get(pitch_source) if pitch_source else None
            if isinstance(pitches, list) and len(pitches) >= 2:
                numeric = [float(value) for value in pitches]
                if abs(numeric[0] - numeric[-1]) <= 1e-6:
                    canonical["end_pitch_mm"] = numeric[0]
                    reconciled["end_pitch_mm"] = str(pitch_source)
                middle = numeric[1:-1]
                if middle and max(middle) - min(middle) <= 1e-6:
                    canonical["middle_pitch_mm"] = middle[0]
                    reconciled["middle_pitch_mm"] = str(pitch_source)
            diameter = canonical.get("wire_diameter_mm")
            radius = canonical.get("arc_radius_mm")
            arc_length = canonical.get("arc_length_mm")
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (diameter, radius, arc_length)):
                diameter_value = float(diameter)
                radius_value = float(radius)
                arc_length_value = float(arc_length)
                if diameter_value > 0 and radius_value > 0:
                    centerline_radius = radius_value + diameter_value / 2.0
                    outer_surface_radius = radius_value + diameter_value
                    longitudinal_locus_radius = radius_value + 1.5 * diameter_value
                    if 0 < arc_length_value / outer_surface_radius < math.pi:
                        total_angle = arc_length_value / outer_surface_radius
                        chord_values = canonical.get("longitudinal_chord_pitches_mm")
                        if (
                            isinstance(chord_values, list)
                            and len(chord_values) == int(canonical.get("longitudinal_count") or 6) - 1
                            and all(isinstance(value, (int, float)) and float(value) > 0.0 for value in chord_values)
                        ):
                            angle_gaps = [
                                2.0 * math.asin(float(value) / (2.0 * longitudinal_locus_radius))
                                for value in chord_values
                            ]
                            station_span = sum(angle_gaps)
                            station_angles = [-station_span / 2.0]
                            for gap in angle_gaps:
                                station_angles.append(station_angles[-1] + gap)
                        else:
                            station_count = int(canonical.get("longitudinal_count") or 6)
                            station_step = total_angle / (station_count - 1)
                            station_angles = [
                                -total_angle / 2.0 + index * station_step
                                for index in range(station_count)
                            ]
                            canonical["longitudinal_chord_pitches_mm"] = [
                                2.0 * longitudinal_locus_radius * math.sin(station_step / 2.0)
                                for _ in range(station_count - 1)
                            ]
                            reconciled["longitudinal_chord_pitches_mm"] = "derived_equal_angle_fallback"
                        expected_depth = (
                            max(
                                -centerline_radius
                                + longitudinal_locus_radius * math.cos(angle)
                                + diameter_value / 2.0
                                for angle in station_angles
                            )
                            - (
                                -centerline_radius
                                + radius_value * math.cos(total_angle / 2.0)
                            )
                        )
                        canonical.update({
                            "arc_radius_semantic": "inner_surface",
                            "arc_length_semantic": "outer_surface_arc",
                            "inner_surface_radius_mm": radius_value,
                            "transverse_centerline_radius_mm": centerline_radius,
                            "outer_surface_radius_mm": outer_surface_radius,
                            "longitudinal_center_locus_radius_mm": longitudinal_locus_radius,
                            "transverse_layer_radius_mm": centerline_radius,
                            "longitudinal_layer_radius_mm": longitudinal_locus_radius,
                        })
                        explicit_depth_in_brief = explicit_mesh.get("overall_depth_mm")
                        supplied_depth = canonical.get("overall_depth_mm")
                        if explicit_depth_in_brief is None and (
                            not isinstance(supplied_depth, (int, float))
                            or abs(float(supplied_depth) - expected_depth) > 0.75
                        ):
                            canonical["overall_depth_mm"] = expected_depth
                            reconciled["overall_depth_mm"] = "derived_from_dwg_side_profile"
            explicit_depth = parameters.get("mesh_total_depth_mm")
            if isinstance(explicit_depth, (int, float)) and not isinstance(explicit_depth, bool):
                canonical["overall_depth_mm"] = float(explicit_depth)
                reconciled["overall_depth_mm"] = "mesh_total_depth_mm"
            canonical["layering"] = "transverse_arc_wires_on_top"
            canonical["layer_reference"] = "drawing_side_profile"
            canonical["side_profile_sag_direction"] = "negative_z_endpoints"
            canonical["transverse_profile_position"] = "upper"
            canonical["longitudinal_profile_position"] = "lower"
            canonical["wire_contact"] = "tangent"
            canonical["layer_center_distance_mm"] = canonical.get("wire_diameter_mm")
            canonical["extended_end_wires_only"] = True
            parameters["longitudinal_outer"] = False
            parameters["transverse_inner"] = False
            parameters["transverse_arc_wires_on_top"] = True
            parameters["wire_mesh_layer_reference"] = "drawing_side_profile"
            parameters["wire_contact"] = "tangent"
            parameters["extended_end_wires_only"] = True
            flat_bar_frame = canonical.get("flat_bar_frame") or params.get("flat_bar_frame")
            parsed_flat_bar_frame = DesignPlanner._parse_flat_bar_frame(source_brief, canonical)
            if isinstance(parsed_flat_bar_frame, dict):
                existing_frame = dict(flat_bar_frame) if isinstance(flat_bar_frame, dict) else {}
                existing_tabs = existing_frame.get("mounting_tabs")
                parsed_tabs = parsed_flat_bar_frame.get("mounting_tabs")
                flat_bar_frame = {**existing_frame, **parsed_flat_bar_frame}
                if isinstance(existing_tabs, dict) or isinstance(parsed_tabs, dict):
                    flat_bar_frame["mounting_tabs"] = {
                        **(existing_tabs if isinstance(existing_tabs, dict) else {}),
                        **(parsed_tabs if isinstance(parsed_tabs, dict) else {}),
                    }
                if isinstance(parsed_flat_bar_frame.get("wire_seat_notches"), dict):
                    flat_bar_frame.pop("wire_clips", None)
            if isinstance(flat_bar_frame, dict) and flat_bar_frame:
                canonical["flat_bar_frame"] = flat_bar_frame
            params["wire_mesh"] = canonical
            if reconciled:
                params["wire_mesh_parameter_sources"] = reconciled
            feature["params"] = params
            result.append(feature)
        return result

    @staticmethod
    def _parse_wire_mesh_contract(source_brief: str) -> dict[str, Any]:
        """Recover explicit wire-mesh dimensions when an LLM swaps schema fields."""
        text = str(source_brief or "")
        if not re.search(r"钢丝网|钢丝瓦片门|网片|wire\s*mesh", text, re.IGNORECASE):
            return {}

        result: dict[str, Any] = {}

        def first_number(patterns: tuple[str, ...]) -> float | None:
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match is not None:
                    return float(match.group(1))
            return None

        diameter = first_number((
            r"钢丝直径\s*(?:[ΦφØ⌀])?\s*(\d+(?:\.\d+)?)\s*mm?",
            r"(?:圆钢轮廓|profile_configuration)\s*(?:为|=|:)?\s*D\s*(\d+(?:\.\d+)?)",
        ))
        longitudinal_count = first_number((
            r"纵向(?:钢丝)?\s*(\d+)\s*根",
            r"(\d+)\s*根纵向钢丝",
        ))
        transverse_count = first_number((
            r"横向(?:圆弧)?(?:钢丝)?\s*(\d+)\s*根",
            r"(\d+)\s*根横向(?:圆弧)?(?:钢丝)?",
        ))
        overall_height = first_number((
            r"纵向钢丝(?:的)?(?:总长|总长度)\s*(\d+(?:\.\d+)?)\s*mm?",
            r"(?:最左|两端)[^。；;\n]{0,36}?纵向钢丝(?:的)?(?:总长|长度)\s*(\d+(?:\.\d+)?)\s*mm?",
        ))
        mesh_height = first_number((
            r"横(?:向)?(?:钢)?丝(?:的)?(?:覆盖高度|覆盖长度)\s*(\d+(?:\.\d+)?)\s*mm?",
            r"其余\s*\d+\s*根纵向钢丝(?:的)?(?:总长|长度)\s*(\d+(?:\.\d+)?)\s*mm?",
            r"网片(?:有效)?高度\s*(\d+(?:\.\d+)?)\s*mm?",
        ))
        arc_radius = first_number((
            r"R\s*(\d+(?:\.\d+)?)\s*(?:是|为)?\s*(?:D\d+(?:\.\d+)?\s*)?(?:横向)?(?:圆弧)?钢丝(?:的)?内表面半径",
            r"(?:横向)?(?:圆弧)?钢丝(?:的)?内表面半径\s*(?:为|=|:|：)?\s*R?\s*(\d+(?:\.\d+)?)",
            r"R\s*(\d+(?:\.\d+)?)\s*(?:是|为)?\s*内表面半径",
            r"(?:横向)?(?:钢丝)?(?:为)?连续\s*R\s*(\d+(?:\.\d+)?)\s*圆弧",
            r"R\s*(\d+(?:\.\d+)?)\s*圆弧",
            r"圆弧半径\s*(?:R)?\s*(\d+(?:\.\d+)?)\s*mm?",
        ))
        arc_length = first_number((
            r"(\d+(?:\.\d+)?)\s*mm?\s*(?:是|为)?[^。；;\n]{0,28}?圆弧(?:总长|总长度|展开长|弧长)",
            r"圆弧(?:总长|总长度|展开长|弧长)\s*(\d+(?:\.\d+)?)\s*mm?",
            r"弧长\s*(\d+(?:\.\d+)?)\s*mm?",
        ))
        overall_depth = first_number((
            r"网片(?:的)?(?:总深|总深度)\s*(\d+(?:\.\d+)?)\s*mm?",
            r"overall_depth_mm\s*(?:=|:)?\s*(\d+(?:\.\d+)?)",
        ))
        outer_surface_chord = first_number((
            r"(?:外表面|外弧)?(?:参考)?弦长\s*(\d+(?:\.\d+)?)\s*mm?",
            r"outer_surface_chord_mm\s*(?:=|:)?\s*(\d+(?:\.\d+)?)",
        ))
        transverse_profile_depth = first_number((
            r"(?:圆弧带|横丝轮廓|圆弧轮廓)?(?:参考)?(?:弓高|轮廓深度)\s*(\d+(?:\.\d+)?)\s*mm?",
            r"transverse_profile_depth_mm\s*(?:=|:)?\s*(\d+(?:\.\d+)?)",
        ))

        numeric_values = {
            "wire_diameter_mm": diameter,
            "longitudinal_count": int(longitudinal_count) if longitudinal_count is not None else None,
            "transverse_count": int(transverse_count) if transverse_count is not None else None,
            "overall_height_mm": overall_height,
            "mesh_height_mm": mesh_height,
            "arc_radius_mm": arc_radius,
            "arc_length_mm": arc_length,
            "overall_depth_mm": overall_depth,
            "outer_surface_chord_mm": outer_surface_chord,
            "transverse_profile_depth_mm": transverse_profile_depth,
        }
        result.update({key: value for key, value in numeric_values.items() if value is not None})

        pitch_match = re.search(
            r"(?:横(?:向)?(?:钢)?丝)?中心距[^。；;\n]{0,28}?(?:为|[:：])\s*"
            r"([0-9]+(?:\.[0-9]+)?(?:\s*[,，、]\s*[0-9]+(?:\.[0-9]+)?)+)\s*mm?",
            text,
            re.IGNORECASE,
        )
        if pitch_match is not None:
            result["transverse_pitches_mm"] = [
                float(value)
                for value in re.split(r"\s*[,，、]\s*", pitch_match.group(1))
            ]
        longitudinal_pitch_match = re.search(
            r"(?:纵向钢丝|纵丝)[^。；;\n]{0,36}?(?:中心弦距|相邻中心距|弦距)[^。；;\n]{0,16}?(?:为|[:：])\s*"
            r"([0-9]+(?:\.[0-9]+)?(?:\s*[,，、]\s*[0-9]+(?:\.[0-9]+)?)+)\s*mm?",
            text,
            re.IGNORECASE,
        )
        if longitudinal_pitch_match is not None:
            result["longitudinal_chord_pitches_mm"] = [
                float(value)
                for value in re.split(r"\s*[,，、]\s*", longitudinal_pitch_match.group(1))
            ]
        return result

    @staticmethod
    def _parse_flat_bar_frame(source_brief: str, wire_mesh: dict[str, Any]) -> dict[str, Any] | None:
        text = str(source_brief or "")
        if not re.search(r"扁铁|扁钢|flat\s*bar", text, re.IGNORECASE):
            return None

        size_match = re.search(
            r"(?:扁铁|扁钢)(?:规格)?(?:为)?[^。；;\n]{0,24}?宽\s*(\d+(?:\.\d+)?)\s*mm?[^。；;\n]{0,12}?厚\s*(\d+(?:\.\d+)?)\s*mm?",
            text,
            re.IGNORECASE,
        )
        if size_match is None:
            size_match = re.search(
                r"(?:扁铁|扁钢|flat\s*bar)[^。；;\n]{0,24}?(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*mm?",
                text,
                re.IGNORECASE,
            )
        straight_match = re.search(r"长度\s*(\d+(?:\.\d+)?)\s*mm\s*的?直扁铁", text)
        straight_count_match = re.search(r"(?:在)?(?:原)?\s*(\d+)\s*根纵向钢丝(?:下方|位置)", text)
        tab_match = re.search(
            r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*mm\s*的?竖直安装耳片",
            text,
            re.IGNORECASE,
        )
        if size_match is None or straight_match is None or straight_count_match is None:
            return None

        width = float(size_match.group(1))
        thickness = float(size_match.group(2))
        straight_count = int(straight_count_match.group(1))
        curved_count = 2 if re.search(r"上下边缘各", text) else 0
        frame: dict[str, Any] = {
            "width_mm": width,
            "thickness_mm": thickness,
            "orientation": "edge_on",
            "support_side": "below_wire",
            "contact": "tangent",
            "construction_style": (
                "segmented_basket"
                if re.search(r"扁铁组合|分段扁铁|拼接扁铁|segmented\s*(?:flat\s*bar|basket)", text, re.IGNORECASE)
                else "continuous_rails"
            ),
            "curved_rail_count": curved_count,
            "curved_rail_radius_mm": float(wire_mesh.get("arc_radius_mm") or 0.0),
            "rail_segment_count": max(int(wire_mesh.get("longitudinal_count") or 6) - 1, 1),
            "straight_support_count": straight_count,
            "straight_support_length_mm": float(straight_match.group(1)),
        }
        if tab_match is not None:
            frame["mounting_tabs"] = {
                "count": curved_count * 2,
                "width_mm": float(tab_match.group(1)),
                "thickness_mm": float(tab_match.group(2)),
                "height_mm": float(tab_match.group(3)),
            }
        notch_requested = bool(re.search(
            r"矩形(?:小)?凹槽|矩形槽|卡槽|槽口|开(?:出|设|加工)?[^。；;\n]{0,12}?槽|wire[-\s]*seat\s*notch",
            text,
            re.IGNORECASE,
        ))
        if notch_requested:
            notch_dimensions = re.search(
                r"(?:凹槽|矩形槽|卡槽|槽口)(?:尺寸|规格)?(?:为)?[^。；;\n]{0,18}?"
                r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*mm?",
                text,
                re.IGNORECASE,
            )
            if notch_dimensions is None:
                notch_dimensions = re.search(
                    r"槽宽\s*(\d+(?:\.\d+)?)\s*mm?[^。；;\n]{0,18}?槽深\s*(\d+(?:\.\d+)?)\s*mm?",
                    text,
                    re.IGNORECASE,
                )
            diameter = float(wire_mesh.get("wire_diameter_mm") or 0.0)
            if notch_dimensions is not None:
                notch_width = float(notch_dimensions.group(1))
                notch_depth = float(notch_dimensions.group(2))
                notch_source = "explicit_brief"
            elif diameter > 0:
                notch_width = diameter + 0.6
                notch_depth = diameter / 2.0
                notch_source = "derived_from_wire_diameter"
            else:
                notch_width = notch_depth = 0.0
                notch_source = "missing_wire_diameter"
            if notch_width > 0 and notch_depth > 0:
                frame["replace_longitudinal_wires"] = True
                frame["wire_seat_notches"] = {
                    "style": "shallow_rectangular_edge_notch",
                    "placement": "all_support_transverse_intersections",
                    "width_mm": notch_width,
                    "depth_mm": notch_depth,
                    "parameter_source": notch_source,
                }
        clip_size = re.search(
            r"(?:卡扣|扣片|压扣)(?:规格)?(?:为)?[^。；;\n]{0,18}?"
            r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*mm?",
            text,
            re.IGNORECASE,
        )
        clearance = re.search(
            r"(?:卡扣)?(?:装配)?间隙\s*(\d+(?:\.\d+)?)\s*mm?",
            text,
            re.IGNORECASE,
        )
        all_intersections = bool(re.search(r"(?:每个|所有)[^。；;\n]{0,28}?交点", text))
        if not notch_requested and clip_size is not None and clearance is not None and all_intersections:
            frame["wire_clips"] = {
                "style": "single_hook",
                "placement": "all_support_wire_intersections",
                "width_mm": float(clip_size.group(1)),
                "thickness_mm": float(clip_size.group(2)),
                "clearance_mm": float(clearance.group(1)),
            }
        return frame

    @staticmethod
    def _features_from_structured_parameters(parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """Recover explicit feature groups from schema-drifted LLM output."""
        result: list[dict[str, Any]] = []
        groups: tuple[tuple[tuple[str, ...], str, str], ...] = (
            (("base_plate",), "base_plate", "BasePlate"),
            (("fillet", "fillets", "fillet_base", "base_fillet"), "fillet", "Fillet"),
            (("mounting_holes", "through_holes", "through_hole"), "through_hole", "ThroughHole"),
            (("boss", "bosses", "top_boss"), "boss", "Boss"),
            (("pocket", "pockets", "top_pocket", "inner_pocket"), "pocket", "Pocket"),
            (("slot", "slots"), "slot", "Slot"),
            (("side_boss", "side_bosses", "bearing_bosses"), "side_boss", "SideBoss"),
            (("side_hole", "side_holes", "bearing_bores"), "side_hole", "SideHole"),
            (("bolt_circle", "bolt_circles", "flange_hole_patterns"), "side_hole", "BoltCircle"),
            (("rib", "ribs", "stiffening_ribs"), "rib", "Rib"),
            (("gear", "gears", "spur_gear", "spur_gears"), "gear", "Gear"),
            (("gear_pair", "gear_pairs", "spur_gear_pair"), "gear_pair", "GearPair"),
            (("revolve", "revolves", "shaft_revolve"), "revolve", "Revolve"),
            (("profile_extrude", "profile_extrudes", "closed_profile_extrudes"), "profile_extrude", "ProfileExtrude"),
            (("sweep", "sweeps"), "sweep", "Sweep"),
            (("loft", "lofts"), "loft", "Loft"),
            (("sheet_metal", "sheet_metals"), "sheet_metal", "SheetMetal"),
            (("weldment", "weldments", "structural_frame"), "weldment", "Weldment"),
            (("freeform_surface", "freeform_surfaces", "fill_surface"), "freeform_surface", "FreeformSurface"),
            (("shell", "shells"), "shell", "Shell"),
            (("draft", "drafts"), "draft", "Draft"),
            (("reference_geometry", "reference_geometries", "references"), "reference_geometry", "ReferenceGeometry"),
            (("configuration", "configurations"), "configuration", "Configuration"),
            (("equation", "equations", "global_variables"), "equation", "Equation"),
            (("assembly_mate", "assembly_mates"), "assembly_mate", "AssemblyMate"),
            (("chamfer", "chamfers"), "chamfer", "Chamfer"),
        )
        used_keys: set[str] = set()
        for keys, feature_type, name_prefix in groups:
            for key in keys:
                if key in used_keys or key not in parameters:
                    continue
                value = parameters.get(key)
                items = value if isinstance(value, list) else [value]
                for index, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        continue
                    params = dict(item)
                    name = str(params.pop("name", "") or (name_prefix if len(items) == 1 else f"{name_prefix}{index}"))
                    required = bool(params.pop("required", True))
                    if feature_type == "side_hole" and name_prefix == "BoltCircle":
                        params.setdefault("placement", "bolt_circle")
                    result.append({"name": name, "type": feature_type, "params": params, "required": required})
                used_keys.add(key)
        return result

    @staticmethod
    def _normalize_feature(feature: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(feature)
        normalized.setdefault("name", normalized.get("type", "Feature"))
        params = dict(normalized.get("params") or {})
        feature_type = str(normalized.get("type") or "")
        feature_type = {
            "bearing_boss": "side_boss",
            "flange_boss": "side_boss",
            "bearing_bore": "side_hole",
            "flange_hole_pattern": "side_hole",
            "stiffening_rib": "rib",
            "reinforcing_rib": "rib",
        }.get(feature_type, feature_type)
        normalized["type"] = feature_type
        for key, value in list(params.items()):
            params[key] = DesignPlanner._plain_value(value)
        if feature_type == "fillet":
            target = str(params.get("target") or params.get("targets") or "outer_edges").lower()
            params["target"] = "four_outer_corners" if any(token in target for token in ("four", "4", "corner", "四角")) else "outer_edges"
        elif feature_type == "through_hole":
            position = str(params.get("position") or params.get("placement") or "").lower()
            if any(token in position for token in ("center", "centre", "中心", "中间")):
                params["position"] = "center"
            position_xy = params.get("position_xy") or params.get("xy")
            if isinstance(position_xy, (list, tuple)) and len(position_xy) >= 2:
                if abs(float(position_xy[0])) <= 1e-9 and abs(float(position_xy[1])) <= 1e-9:
                    params["position"] = "center"
            params["count"] = int(params.get("count", 1) or 1)
        elif feature_type == "chamfer":
            if "size" not in params and "distance" in params:
                params["size"] = params["distance"]
            target = str(params.get("targets") or params.get("target") or "all_outer_edges").lower()
            params["targets"] = "all_outer_edges" if any(token in target for token in ("all", "outer", "外")) else target
        elif feature_type == "boss":
            position = str(params.get("position") or "center_top").lower()
            params["position"] = "center_top" if "center" in position or "中心" in position else position
        elif feature_type == "rib":
            positions = params.get("center_positions_mm")
            if isinstance(positions, list) and positions and all(isinstance(item, (tuple, list)) and item for item in positions):
                params["center_positions_mm"] = [float(item[0]) for item in positions]
            axis = str(params.get("axis") or "y").lower()
            center_key = "center_xz_mm" if axis == "y" else "center_yz_mm"
            if params.get(center_key) is None:
                base_z = float(params.get("base_z_mm", 0) or 0)
                height = float(params.get("height_mm", params.get("height", 0)) or 0)
                params[center_key] = [0.0, base_z + height / 2.0]
        elif feature_type in {"linear_pattern", "circular_pattern"}:
            for key in ("direction", "direction_1", "direction_2", "axis"):
                if params.get(key) is not None:
                    params[key] = str(params[key]).lower().replace(" ", "")
        elif feature_type == "mirror" and params.get("scope") is None:
            params["scope"] = "features"
        normalized["params"] = params
        normalized.setdefault("required", True)
        return normalized

    @staticmethod
    def _normalize_feature_coordinates(features: list[dict[str, Any]], parameters: dict[str, Any]) -> list[dict[str, Any]]:
        length = float(parameters.get("length", 0) or 0)
        width = float(parameters.get("width", 0) or 0)
        normalized_features: list[dict[str, Any]] = []
        for feature in features:
            normalized = dict(feature)
            params = dict(normalized.get("params") or {})
            feature_type = str(normalized.get("type") or "")

            if feature_type == "through_hole":
                placement = params.get("placement")
                nested_offsets = placement.get("edge_offsets_mm") if isinstance(placement, dict) else None
                offsets = params.get("edge_offsets_mm") or params.get("edge_offsets") or nested_offsets
                canonical_offsets = DesignPlanner._canonical_edge_offsets(offsets)
                if canonical_offsets:
                    params["placement"] = "corner_offsets"
                    params["edge_offsets_mm"] = canonical_offsets
                    params.pop("edge_offsets", None)

            if feature_type in {"boss", "pocket"}:
                position_xy = params.get("position_xy") or params.get("center_xy")
                if isinstance(position_xy, dict) and "x" in position_xy and "y" in position_xy:
                    x = float(position_xy["x"])
                    y = float(position_xy["y"])
                    position = str(params.get("position") or "").lower()
                    if "center" in position or (length > 0 and width > 0 and abs(x - length / 2.0) <= 1e-6 and abs(y - width / 2.0) <= 1e-6):
                        params["position_xy"] = [0.0, 0.0]
                    elif x == 0.0 and y == 0.0:
                        params["position_xy"] = [0.0, 0.0]
                    elif length > 0 and width > 0 and 0 <= x <= length and 0 <= y <= width:
                        params["position_xy"] = [x - length / 2.0, y - width / 2.0]

            if feature_type == "slot":
                orientation = str(params.get("orientation") or "y").lower().replace(" ", "")
                if orientation in {"x", "+x", "-x", "alongx", "horizontal"} or orientation.endswith("x"):
                    orientation = "x"
                elif orientation in {"y", "+y", "-y", "alongy", "vertical"} or orientation.endswith("y"):
                    orientation = "y"
                params["orientation"] = orientation
                offsets = params.get("centerline_offsets")
                if isinstance(offsets, list) and offsets and all(isinstance(item, dict) for item in offsets):
                    axis = "x" if orientation == "y" else "y"
                    values = [float(item[axis]) for item in offsets if axis in item]
                    if len(values) == len(offsets):
                        span = length if axis == "x" else width
                        if span > 0 and all(0 <= value <= span for value in values) and abs(sum(values) / len(values) - span / 2.0) <= 1e-6:
                            values = [value - span / 2.0 for value in values]
                        params["centerline_offsets"] = values

            normalized["params"] = params
            normalized_features.append(normalized)
        return normalized_features

    @staticmethod
    def _canonical_edge_offsets(value: Any) -> dict[str, float] | None:
        if not isinstance(value, dict):
            return None
        if value.get("x") is not None and value.get("y") is not None:
            return {"x": float(value["x"]), "y": float(value["y"])}

        left = value.get("from_left", value.get("left"))
        right = value.get("from_right", value.get("right"))
        bottom = value.get("from_bottom", value.get("bottom"))
        top = value.get("from_top", value.get("top"))
        if left is None and right is None or bottom is None and top is None:
            return None
        if left is not None and right is not None and abs(float(left) - float(right)) > 1e-6:
            return None
        if bottom is not None and top is not None and abs(float(bottom) - float(top)) > 1e-6:
            return None
        return {
            "x": float(left if left is not None else right),
            "y": float(bottom if bottom is not None else top),
        }

    @staticmethod
    def _normalize_parameters(parameters: dict[str, Any], features: list[dict[str, Any]]) -> dict[str, Any]:
        result = {key: DesignPlanner._plain_value(value) for key, value in dict(parameters or {}).items()}
        base = next((item for item in features if item.get("type") == "base_plate"), None)
        nested = result.get("base_plate") if isinstance(result.get("base_plate"), dict) else {}
        source = dict(base.get("params", {})) if base else {}
        source = {**nested, **source}
        for key in ("length", "width", "thickness"):
            if not isinstance(result.get(key), (int, float)) and isinstance(source.get(key), (int, float)):
                result[key] = float(source[key])
        result.setdefault("unit", "mm")
        material = result.get("material")
        if isinstance(material, dict):
            result["material"] = str(material.get("name") or material.get("value") or "")
        return result

    @staticmethod
    def _plain_value(value: Any) -> Any:
        if isinstance(value, dict) and "value" in value:
            return value.get("value")
        if isinstance(value, list):
            return [DesignPlanner._plain_value(item) for item in value]
        if isinstance(value, dict):
            return {key: DesignPlanner._plain_value(item) for key, item in value.items()}
        return value
