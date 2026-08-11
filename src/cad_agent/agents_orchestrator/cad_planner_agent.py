from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..brain_models import ModelProvider
from ..candidate_planning_service import CandidatePlanningService
from ..design_planner import DIRECT_GUI_COMMAND_PREFIX, DesignPlanner
from ..stage_planner import apply_stage_plan, infer_stage_plan
from .agents_config import AgentsOrchestratorConfig
from .dimension_parser import parse_body_dimensions


class CADPlannerAgent:
    """Planner facade for the optional Agents SDK experiment."""

    def __init__(
        self,
        output_root: Path,
        config: AgentsOrchestratorConfig | None = None,
        provider: ModelProvider | None = None,
    ) -> None:
        self.output_root = output_root
        self.config = config or AgentsOrchestratorConfig.detect()
        self.local_planner = DesignPlanner(output_root, provider=provider)
        self.candidate_planner = CandidatePlanningService(output_root, self.local_planner)
        self.sdk_agent = self.config.create_sdk_agent(
            "CADPlannerAgent",
            "Convert mechanical design requests into normalized CAD Design JSON. Do not call CAD software.",
        )

    def plan(self, prompt: str, stage_mode: str = "auto") -> dict[str, Any]:
        provider = self.local_planner.provider
        first_line = prompt.strip().splitlines()[0].strip().upper() if prompt.strip() else ""
        if first_line == DIRECT_GUI_COMMAND_PREFIX:
            design = self.local_planner.plan(prompt, stage_mode=stage_mode)
        elif callable(getattr(provider, "generate_candidate_output", None)):
            design = self.candidate_planner.plan(prompt, provider, stage_mode=stage_mode)
        else:
            design = self.local_planner.plan(prompt, stage_mode=stage_mode)
            if self.local_planner.provider_name == "local-rule-based" and self._looks_like_complex_mounting_base(prompt):
                design = self._complex_mounting_base_plan(prompt, design)
        design.setdefault("source_brief", prompt.strip())
        design.setdefault("outputs", self._requested_outputs(prompt))
        design.setdefault("unsupported_features", [])
        design.setdefault("risks", [])
        design = apply_stage_plan(design, infer_stage_plan(prompt, stage_mode))

        planning_provider = str(
            (design.get("llm_provider") or {}).get("id")
            or design.get("planning_mode")
            or self.local_planner.provider_name
        )
        design["planner_agent"] = {
            "name": "CADPlannerAgent",
            "orchestrator_mode": self.config.mode,
            "sdk_available": self.config.sdk_available,
            "provider": planning_provider,
        }
        return design

    @staticmethod
    def _requested_outputs(prompt: str) -> list[str]:
        lower = prompt.lower()
        outputs = ["brain_plan_json", "design_plan_json", "pipeline_report_json"]
        mapping = {
            "sldprt": "SLDPRT",
            "solidworks": "SLDPRT",
            "slddrw": "SLDDRW",
            "工程图": "SLDDRW",
            "step": "STEP",
            "dwg": "DWG",
            "pdf": "PDF",
            "annotated": "Annotated DWG",
            "标注": "annotation_report_json",
        }
        for token, output in mapping.items():
            if token.lower() in lower or token in prompt:
                outputs.append(output)
        return list(dict.fromkeys(outputs))

    @staticmethod
    def _looks_like_complex_mounting_base(prompt: str) -> bool:
        return any(token in prompt for token in ("安装底座", "凸台", "型腔", "PCD", "腰型槽"))

    def _complex_mounting_base_plan(self, prompt: str, base: dict[str, Any]) -> dict[str, Any]:
        design = dict(base)
        dimension_parse = parse_body_dimensions(prompt)
        if dimension_parse.ok:
            numeric = dimension_parse.numeric_parameters()
            length = float(numeric["length"])
            width = float(numeric["width"])
            thickness = float(numeric["thickness"])
        else:
            length = width = thickness = 0.0
        material = "6061 aluminum" if "6061" in prompt else design.get("parameters", {}).get("material", "plain carbon steel")
        base_fillet_radius = self._match_number(prompt, r"(?:底板)?四角[^\n]*?R\s*(\d+(?:\.\d+)?)", 0.0)
        corner_hole_diameter = self._match_number(prompt, r"四角[^\n]*?[φΦ]\s*(\d+(?:\.\d+)?)", 0.0)
        edge_offset = self._match_number(prompt, r"相邻两边[^\n]*?(\d+(?:\.\d+)?)\s*mm", 0.0)
        center_hole_diameter = self._match_number(prompt, r"中心[^\n]*?[φΦ]\s*(\d+(?:\.\d+)?)", 0.0)
        boss_dimensions = self._match_dimensions(prompt, r"矩形凸台")
        pocket_dimensions = self._match_dimensions(prompt, r"矩形型腔", depth_label="深")
        slot_length = self._match_number(prompt, r"槽总长\s*(\d+(?:\.\d+)?)", 0.0)
        slot_width = self._match_number(prompt, r"槽总长[^\n]*?宽\s*(\d+(?:\.\d+)?)", 0.0)
        slot_offset = self._match_number(prompt, r"中心左右\s*(\d+(?:\.\d+)?)", 0.0)
        pcd = self._match_number(prompt, r"PCD\s*(\d+(?:\.\d+)?)", 0.0)
        # Chinese text is a ``\w`` character to Python regex, so ``\bM6\b``
        # does not match ``M6\u87ba\u7eb9\u5b54``. Use ASCII-only boundaries instead.
        thread_match = re.search(r"(?<![A-Za-z0-9])(M(?:3|4|5|6|8|10))(?![A-Za-z0-9])", prompt, flags=re.IGNORECASE)
        thread = thread_match.group(1).upper() if thread_match else ""

        features = [
            {
                "name": "BasePlate",
                "type": "base_plate",
                "params": {"length": length, "width": width, "thickness": thickness},
                "required": True,
            },
        ]
        if base_fillet_radius > 0:
            features.append({
                "name": "OuterCornerFillets",
                "type": "fillet",
                "params": {"radius": base_fillet_radius, "targets": "four_outer_corners"},
                "required": True,
            })
        if corner_hole_diameter > 0 and edge_offset > 0:
            features.append(
                {
                "name": "CornerMountingHoles",
                "type": "through_hole",
                "params": {
                    "diameter": corner_hole_diameter,
                    "count": 4,
                    "placement": "corner_offsets",
                    "edge_offsets_mm": {"x": edge_offset, "y": edge_offset},
                },
                "required": True,
                }
            )
        if center_hole_diameter > 0:
            features.append(
                {
                "name": "CenterThroughHole",
                "type": "through_hole",
                "params": {"diameter": center_hole_diameter, "position": "center"},
                "required": True,
                }
            )
        if pcd > 0 and thread:
            features.extend([
                {
                "name": "PCDThreadedHolePattern",
                "type": "bolt_circle_pattern",
                "params": {"pcd": pcd, "count": 6, "feature": {"type": "threaded_hole", "thread": thread}},
                "required": True,
                },
                {
                "name": f"{thread}ThreadedHoleSpec",
                "type": "threaded_hole",
                "params": {"thread": thread, "count": 6, "pattern": "PCDThreadedHolePattern", "pcd": pcd},
                "required": True,
                },
            ])
        if boss_dimensions:
            features.append(
                {
                "name": "CenterBoss",
                "type": "boss",
                "params": {**boss_dimensions, "position": "center_top"},
                "required": True,
                }
            )
        if pocket_dimensions:
            features.append(
                {
                "name": "TopPocket",
                "type": "pocket",
                "params": {**pocket_dimensions, "corner_radius": self._match_number(prompt, r"型腔四角\s*R\s*(\d+(?:\.\d+)?)", 0.0)},
                "required": True,
                }
            )
        if slot_length > 0 and slot_width > 0 and slot_offset > 0:
            features.append(
                {
                "name": "SideSlots",
                "type": "slot",
                "params": {
                    "count": 2,
                    "length": slot_length,
                    "width": slot_width,
                    "centerline_offsets": [-slot_offset, slot_offset],
                    "orientation": "y",
                },
                "required": True,
                }
            )
        chamfer = self._match_number(prompt, r"(?:外边|外轮廓)[^\n]*?C\s*(\d+(?:\.\d+)?)", 0.0)
        if chamfer > 0:
            features.append(
                {
                "name": "OuterChamfers",
                "type": "chamfer",
                "params": {"size": chamfer, "targets": "all_outer_edges"},
                "required": True,
                }
            )
        unsupported_types = {"bolt_circle_pattern", "threaded_hole"}
        unsupported = list(dimension_parse.unsupported_features) + [
            {
                "type": item.get("type"),
                "name": item.get("name"),
                "reason": "Current stable Pipeline can route this feature, but full geometry generation is not yet guaranteed by existing skills.",
            }
            for item in features
            if item.get("type") in unsupported_types
        ]
        design.update(
            {
                "intent": "model_drawing_and_exports",
                "part_family": "mounting_base",
                "parameters": {"unit": "mm", "length": length, "width": width, "thickness": thickness, "material": material},
                "parameter_details": dimension_parse.details(),
                "features": features,
                "outputs": [
                    "SLDPRT",
                    "SLDDRW",
                    "STEP",
                    "DWG",
                    "Annotated DWG",
                    "PDF",
                    "brain_plan_json",
                    "design_plan_json",
                    "pipeline_report_json",
                    "annotation_report_json",
                ],
                "unsupported_features": unsupported,
                "needs_confirmation": not dimension_parse.ok,
                "confirmation_reason": "Body length, width, and thickness require confirmation." if not dimension_parse.ok else "",
                "risks": dimension_parse.risks + [
                    "This experimental orchestrator does not replace the existing Pipeline Runner.",
                    "Threaded-hole and bolt-circle production executors remain separate capabilities.",
                ],
            }
        )
        return design

    @staticmethod
    def _extract_after(text: str, marker: str, default: float) -> float:
        match = re.search(re.escape(marker) + r"\s*(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
        return float(match.group(1)) if match else default

    @staticmethod
    def _match_number(text: str, pattern: str, default: float) -> float:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return float(match.group(1)) if match else default

    @staticmethod
    def _match_dimensions(text: str, noun: str, depth_label: str | None = None) -> dict[str, float] | None:
        third_label = depth_label or "高"
        # Use an overlapping look-ahead and take the last match nearest the
        # feature noun. This prevents an overall L/W phrase from spanning into
        # a later boss or pocket height.
        length_label = "\u957f"
        width_label = "\u5bbd"
        feature_matches = list(re.finditer(
            rf"(?={length_label}\s*(\d+(?:\.\d+)?)\s*mm[^\n]*?{width_label}\s*(\d+(?:\.\d+)?)\s*mm[^\n]*?{third_label}\s*(\d+(?:\.\d+)?)\s*mm[^\n]*?{noun})",
            text,
            flags=re.IGNORECASE,
        ))
        keys = ("length", "width", "depth" if depth_label else "height")
        if feature_matches:
            return {key: float(value) for key, value in zip(keys, feature_matches[-1].groups())}

        # Bind a feature's dimensional phrase to its noun so overall plate
        # dimensions earlier in the brief do not become boss or pocket sizes.
        noun_index = text.find(noun)
        scope = text[max(0, noun_index - 240): noun_index + len(noun)] if noun_index >= 0 else text
        matches = list(re.finditer(
            rf"长\s*(\d+(?:\.\d+)?)\s*mm[^\n]*?宽\s*(\d+(?:\.\d+)?)\s*mm[^\n]*?{third_label}\s*(\d+(?:\.\d+)?)\s*mm[^\n]*?{noun}",
            scope,
            flags=re.IGNORECASE,
        ))
        match = matches[-1] if matches else None
        if not match:
            return None
        keys = ("length", "width", "depth" if depth_label else "height")
        return {key: float(value) for key, value in zip(keys, match.groups())}
