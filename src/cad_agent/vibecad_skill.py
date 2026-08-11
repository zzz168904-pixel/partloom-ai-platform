from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import SkillResult


THREAD_TABLE = {
    "M3": {"pitch": 0.5, "tap_drill": 2.5, "clearance": 3.4},
    "M4": {"pitch": 0.7, "tap_drill": 3.3, "clearance": 4.5},
    "M5": {"pitch": 0.8, "tap_drill": 4.2, "clearance": 5.5},
    "M6": {"pitch": 1.0, "tap_drill": 5.0, "clearance": 6.6},
    "M8": {"pitch": 1.25, "tap_drill": 6.8, "clearance": 9.0},
    "M10": {"pitch": 1.5, "tap_drill": 8.5, "clearance": 11.0},
}


class VibeCADSkill:
    """Natural language to deterministic CAD design plan.

    This module is intentionally independent from SolidWorks COM. It only
    understands intent, extracts parameters, and emits a JSON plan that other
    automation skills can execute.
    """

    key = "solidworks_vibecad"
    name = "SolidWorks VibeCAD"

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def run(self, brief: str) -> SkillResult:
        plan = self.build_plan(brief)
        run_dir = self.output_root / f"vibecad_agent_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "design_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success=True,
            message=f"VibeCAD design plan ready: {plan_path}",
            data=plan,
            path=str(plan_path),
            output=json.dumps(plan, ensure_ascii=False, indent=2),
        )

    def build_plan(self, brief: str) -> dict[str, Any]:
        text = self._normalize(brief)
        dimensions = self._extract_dimensions(text)
        material = self._extract_material(text)
        features = self._extract_features(text, dimensions)
        datums = self._extract_datums(text)
        tolerances = self._extract_tolerances(text)
        assembly_relations = self._extract_assembly_relations(text)
        required_skills = self._required_skills(text, features)
        if assembly_relations and "solidworks_assembly" not in required_skills:
            required_skills.append("solidworks_assembly")

        assumptions: list[str] = []
        if dimensions["source"] == "default":
            assumptions.append("未识别到完整外形尺寸，默认采用 100x50x10 mm 矩形板。")
        if material["source"] == "default":
            assumptions.append("未识别到材料，默认采用 plain carbon steel。")
        if not features:
            assumptions.append("未识别到特征，默认只生成基础矩形板。")

        operation_sequence = [
            "vibecad_parse_brief",
            "solidworks_create_base_part",
        ]
        if any(item["type"] in {"through_hole", "hole_pattern"} for item in features):
            operation_sequence.append("solidworks_cut_holes")
        if any(item["type"] == "threaded_hole" for item in features):
            operation_sequence.append("thread_skill_apply_tapped_holes")
        if any(item["type"] in {"fillet", "chamfer"} for item in features):
            operation_sequence.append("solidworks_apply_requested_edge_features")
        operation_sequence.extend(
            [
                "solidworks_generate_drawing",
                "autocad_annotate_2d_if_requested",
                "export_pdf_dwg",
            ]
        )

        return {
            "plan_version": 2,
            "source_brief": brief.strip(),
            "intent": self._detect_intent(text),
            "part_family": self._detect_part_family(text),
            "parameters": {
                "unit": "mm",
                "length": dimensions["length"],
                "width": dimensions["width"],
                "thickness": dimensions["thickness"],
                "material": material["name"],
            },
            "features": features,
            "datums": datums,
            "tolerances": tolerances,
            "assembly_relations": assembly_relations,
            "skill_pipeline": required_skills,
            "operation_sequence": operation_sequence,
            "assumptions": assumptions,
            "risk_register": [
                {
                    "risk": "自然语言中尺寸顺序可能不符合长宽厚习惯。",
                    "mitigation": "VibeCAD 只在识别到三元尺寸时自动映射为 length/width/thickness，并在 JSON 中保留 source_brief。",
                },
                {
                    "risk": "螺纹孔和普通通孔语义混淆。",
                    "mitigation": "出现攻丝、内螺纹、tap、threaded 等关键词时才路由 Thread Skill。",
                },
            ],
            "review_plan": {
                "expected_outputs": ["design_plan_json", "sldprt", "drawing", "pdf", "dwg"],
                "views": ["front", "top", "right", "isometric"],
                "checks": [
                    "parameters_match_brief",
                    "features_created",
                    "skill_pipeline_executed_in_order",
                    "outputs_exist",
                ],
            },
        }

    @staticmethod
    def _normalize(text: str) -> str:
        return text.replace("×", "x").replace("*", "x").replace("Ｘ", "x")

    def _extract_dimensions(self, text: str) -> dict[str, Any]:
        triplet = re.search(
            r"(?P<a>\d+(?:\.\d+)?)\s*x\s*(?P<b>\d+(?:\.\d+)?)\s*x\s*(?P<c>\d+(?:\.\d+)?)(?:\s*(?:mm|毫米))?",
            text,
            re.IGNORECASE,
        )
        if triplet:
            return {
                "length": float(triplet.group("a")),
                "width": float(triplet.group("b")),
                "thickness": float(triplet.group("c")),
                "source": "triplet",
            }

        length = self._number_after_keywords(text, ("长", "长度", "length", "l"))
        width = self._number_after_keywords(text, ("宽", "宽度", "width", "w"))
        thickness = self._number_after_keywords(text, ("厚", "厚度", "thickness", "t"))
        if length and width and thickness:
            return {
                "length": length,
                "width": width,
                "thickness": thickness,
                "source": "named_fields",
            }
        return {"length": 100.0, "width": 50.0, "thickness": 10.0, "source": "default"}

    def _extract_material(self, text: str) -> dict[str, str]:
        table = [
            (("钢", "steel", "q235", "45#"), "plain carbon steel"),
            (("不锈钢", "stainless"), "stainless steel"),
            (("铝", "aluminum", "aluminium", "6061", "7075"), "6061 aluminum"),
            (("铜", "brass", "copper"), "brass/copper"),
            (("塑料", "abs", "pom", "nylon"), "engineering plastic"),
        ]
        lowered = text.lower()
        for keywords, material in table:
            if any(keyword in lowered for keyword in keywords):
                return {"name": material, "source": "brief"}
        return {"name": "plain carbon steel", "source": "default"}

    def _extract_features(self, text: str, dimensions: dict[str, Any]) -> list[dict[str, Any]]:
        features: list[dict[str, Any]] = []
        hole_dia = self._extract_hole_diameter(text)
        if hole_dia:
            features.append(
                {
                    "name": "CenterHole" if self._has_center_position(text) else "Hole",
                    "type": "through_hole",
                    "params": {
                        "diameter": hole_dia,
                        "position": "center" if self._has_center_position(text) else "unspecified",
                    },
                    "required": True,
                    "target_skill": "solidworks_automation",
                }
            )

        thread = self._extract_thread(text)
        if thread:
            rule = THREAD_TABLE[thread]
            count = self._extract_count(text) or (4 if self._has_corner_pattern(text) else 1)
            features.append(
                {
                    "name": f"{thread}ThreadedHoles",
                    "type": "threaded_hole",
                    "params": {
                        "thread": thread,
                        "count": count,
                        "tap_drill": rule["tap_drill"],
                        "pitch": rule["pitch"],
                        "position": "four_corners" if self._has_corner_pattern(text) else "unspecified",
                    },
                    "required": True,
                    "target_skill": "solidworks_threaded_holes",
                }
            )

        radius = self._prefixed_number(text, "R")
        if radius is not None or "圆角" in text or "fillet" in text.lower():
            features.append(
                {
                    "name": "OuterFillet",
                    "type": "fillet",
                    "params": {"radius": radius or 2.0, "target": "outer_edges"},
                    "required": True,
                    "target_skill": "fillet",
                }
            )

        chamfer = self._prefixed_number(text, "C")
        if chamfer is not None or "倒角" in text or "chamfer" in text.lower():
            features.append(
                {
                    "name": "EdgeChamfer",
                    "type": "chamfer",
                    "params": {"distance": chamfer or 1.0, "target": "outer_edges"},
                    "required": True,
                    "target_skill": "chamfer",
                }
            )

        if "槽" in text or "slot" in text.lower():
            slot_width = self._number_after_keywords(text, ("槽宽", "slot width")) or min(dimensions["width"] / 4, 10)
            slot_length = self._number_after_keywords(text, ("槽长", "slot length")) or min(dimensions["length"] / 2, 30)
            features.append(
                {
                    "name": "Slot",
                    "type": "slot",
                    "params": {"width": slot_width, "length": slot_length, "position": "center"},
                    "required": True,
                    "target_skill": "solidworks_automation",
                }
            )
        pattern = self._extract_pattern(text)
        if pattern:
            seed = self._resolve_seed_feature(text, features, ("阵列", "pattern", "array"))
            if seed:
                pattern["params"]["seed_features"] = [seed["name"]]
            if pattern["type"] == "circular_pattern":
                axis_feature = self._central_axis_feature(features)
                if axis_feature and any(token in text.lower() for token in ("中心孔", "central hole", "center hole")):
                    pattern["params"]["axis_feature"] = axis_feature["name"]
            features.append(pattern)
        mirror = self._extract_mirror(text)
        if mirror:
            seed = self._resolve_seed_feature(text, features, ("镜像", "mirror", "对称"))
            if seed:
                mirror["params"]["seed_features"] = [seed["name"]]
            features.append(mirror)
        return features

    @staticmethod
    def _extract_pattern(text: str) -> dict[str, Any] | None:
        lowered = text.lower()
        if not any(token in lowered for token in ("阵列", "矩形阵列", "圆周阵列", "pattern", "array")):
            return None
        count_x = VibeCADSkill._number_after_keywords(text, ("x向", "x方向", "x count", "columns"))
        count_y = VibeCADSkill._number_after_keywords(text, ("y向", "y方向", "y count", "rows"))
        spacing = VibeCADSkill._number_after_keywords(text, ("间距", "spacing", "pitch"))
        count = VibeCADSkill._pattern_count(text)
        pattern_type = "circular_pattern" if any(token in lowered for token in ("圆周阵列", "circular pattern")) else "linear_pattern"
        if pattern_type == "circular_pattern":
            axis = VibeCADSkill._pattern_axis(text) or "z"
            angle = VibeCADSkill._number_after_keywords(text, ("总角度", "角度", "total angle", "angle")) or 360.0
            params: dict[str, Any] = {
                "count": count,
                "total_angle_deg": angle,
                "axis": axis,
                "equal_spacing": True,
                "geometry_pattern": True,
                "axis_source": "brief" if VibeCADSkill._pattern_axis(text) else "base_normal_convention",
            }
        else:
            direction = VibeCADSkill._linear_direction(text)
            primary_count = int(count_x) if count_x else count
            params = {
                "count_1": primary_count,
                "spacing_1": spacing,
                "direction_1": direction or ("x" if count_x else None),
                "count_2": int(count_y) if count_y else 1,
                "spacing_2": VibeCADSkill._number_after_keywords(text, ("y向间距", "y spacing")) if count_y else 0,
                "direction_2": "y" if count_y else None,
                "geometry_pattern": True,
            }
        return {
            "name": "Pattern",
            "type": pattern_type,
            "params": params,
            "required": True,
            "target_skill": pattern_type,
        }

    @staticmethod
    def _extract_mirror(text: str) -> dict[str, Any] | None:
        lowered = text.lower()
        if not any(token in lowered for token in ("镜像", "mirror", "对称")):
            return None
        plane = "Right Plane"
        if any(token in lowered for token in ("前视", "front")):
            plane = "Front Plane"
        elif any(token in lowered for token in ("上视", "top")):
            plane = "Top Plane"
        return {
            "name": "Mirror",
            "type": "mirror",
            "params": {"mirror_plane": plane, "scope": "features"},
            "required": True,
            "target_skill": "mirror",
        }

    @staticmethod
    def _pattern_count(text: str) -> int | None:
        patterns = (
            r"(?:线性阵列|圆周阵列|阵列|linear\s+pattern|circular\s+pattern|pattern|array)[^。；,，\n]{0,30}?(\d+)\s*(?:个|等分|instances?)?",
            r"(\d+)\s*(?:个|等分|instances?)[^。；,，\n]{0,20}?(?:线性阵列|圆周阵列|阵列|pattern|array)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _linear_direction(text: str) -> str | None:
        match = re.search(r"(?:沿|方向|direction)\s*([+-]?[XYZxyz])(?:\s*(?:轴|向|方向|axis))?", text, re.IGNORECASE)
        if not match:
            match = re.search(r"([+-]?[XYZxyz])\s*(?:轴|向|方向|axis)[^。；,，\n]{0,16}?(?:线性)?阵列", text, re.IGNORECASE)
        return match.group(1).lower() if match else None

    @staticmethod
    def _pattern_axis(text: str) -> str | None:
        match = re.search(r"(?:绕|about|around)\s*([+-]?[XYZxyz])\s*(?:轴|axis)", text, re.IGNORECASE)
        return match.group(1).lower() if match else None

    @staticmethod
    def _resolve_seed_feature(
        text: str,
        features: list[dict[str, Any]],
        operation_tokens: tuple[str, ...],
    ) -> dict[str, Any] | None:
        lowered = text.lower()
        positions = [lowered.find(token.lower()) for token in operation_tokens if lowered.find(token.lower()) >= 0]
        operation_index = min(positions) if positions else len(text)
        window = lowered[max(0, operation_index - 100):operation_index]
        semantic_types = (
            (("螺纹孔", "攻丝孔", "threaded hole", "tapped hole"), {"threaded_hole"}),
            (("中心孔", "通孔", "孔", "hole"), {"through_hole", "threaded_hole"}),
            (("凸台", "boss", "pad"), {"boss"}),
            (("型腔", "口袋", "pocket", "cavity"), {"pocket"}),
            (("腰型槽", "槽", "slot"), {"slot"}),
        )
        ranked: list[tuple[int, set[str]]] = []
        for tokens, types in semantic_types:
            position = max((window.rfind(token) for token in tokens), default=-1)
            if position >= 0:
                ranked.append((position, types))
        if ranked:
            _, preferred = max(ranked, key=lambda item: item[0])
            match = next((feature for feature in reversed(features) if feature.get("type") in preferred), None)
            if match:
                return match
        candidates = [
            feature
            for feature in features
            if feature.get("type") in {"through_hole", "threaded_hole", "boss", "pocket", "slot"}
        ]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _central_axis_feature(features: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next(
            (
                feature
                for feature in features
                if feature.get("type") == "through_hole"
                and str(feature.get("params", {}).get("position") or "").lower() == "center"
            ),
            None,
        )

    @staticmethod
    def _extract_datums(text: str) -> list[dict[str, Any]]:
        lowered = text.lower()
        datums: list[dict[str, Any]] = []
        if any(token in lowered for token in ("基准a", "datum a", "a基准")):
            datums.append({"name": "A", "type": "datum_plane", "reference": "primary_face"})
        if any(token in lowered for token in ("基准b", "datum b", "b基准")):
            datums.append({"name": "B", "type": "datum_plane", "reference": "secondary_face"})
        if any(token in lowered for token in ("基准c", "datum c", "c基准")):
            datums.append({"name": "C", "type": "datum_plane", "reference": "tertiary_face"})
        return datums

    @staticmethod
    def _extract_tolerances(text: str) -> list[dict[str, Any]]:
        tolerances: list[dict[str, Any]] = []
        for match in re.finditer(r"(?:公差|tolerance)\s*[±\+\-]?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE):
            tolerances.append({"type": "general", "value": float(match.group(1)), "unit": "mm"})
        plus_minus = re.search(r"±\s*(\d+(?:\.\d+)?)", text)
        if plus_minus and not tolerances:
            tolerances.append({"type": "general", "value": float(plus_minus.group(1)), "unit": "mm"})
        if "位置度" in text:
            match = re.search(r"位置度\s*(\d+(?:\.\d+)?)", text)
            tolerances.append({"type": "position", "value": float(match.group(1)) if match else None, "unit": "mm"})
        return tolerances

    @staticmethod
    def _extract_assembly_relations(text: str) -> list[dict[str, Any]]:
        lowered = text.lower()
        relations: list[dict[str, Any]] = []
        if any(token in lowered for token in ("同心", "concentric")):
            relations.append({"type": "concentric", "entities": [], "target_skill": "solidworks_assembly"})
        if any(token in lowered for token in ("重合", "coincident")):
            relations.append({"type": "coincident", "entities": [], "target_skill": "solidworks_assembly"})
        if any(token in lowered for token in ("距离配合", "distance mate")):
            relations.append({"type": "distance", "entities": [], "target_skill": "solidworks_assembly"})
        return relations

    @staticmethod
    def _required_skills(text: str, features: list[dict[str, Any]]) -> list[str]:
        skills = ["solidworks_vibecad", "solidworks_automation"]
        if any(feature["type"] == "threaded_hole" for feature in features):
            skills.append("solidworks_threaded_holes")
        if any(feature["type"] == "fillet" for feature in features):
            skills.append("fillet")
        if any(feature["type"] == "chamfer" for feature in features):
            skills.append("chamfer")
        lowered = text.lower()
        if any(token in lowered for token in ("dwg", "dxf", "autocad", "标注", "尺寸", "pdf")):
            skills.append("autocad")
        return skills

    @staticmethod
    def _detect_intent(text: str) -> str:
        lowered = text.lower()
        if any(token in lowered for token in ("工程图", "三视图", "drawing", "pdf", "dwg")):
            return "model_and_drawing"
        return "parametric_part"

    @staticmethod
    def _detect_part_family(text: str) -> str:
        lowered = text.lower()
        if "板" in text or "plate" in lowered:
            return "plate"
        if "支架" in text or "bracket" in lowered:
            return "bracket"
        if "安装座" in text or "mount" in lowered:
            return "cnc_mount"
        if "轴" in text or "shaft" in lowered:
            return "shaft"
        return "unknown"

    @staticmethod
    def _extract_hole_diameter(text: str) -> float | None:
        patterns = [
            r"[ΦφØø]\s*(\d+(?:\.\d+)?)",
            r"(?:直径|孔径|dia|diameter)\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:mm|毫米)?\s*(?:孔|通孔)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return float(match.group(1))
        return None

    @staticmethod
    def _extract_thread(text: str) -> str | None:
        match = re.search(r"(?<![A-Za-z0-9])(M(?:3|4|5|6|8|10))(?![A-Za-z0-9])", text, re.IGNORECASE)
        if not match:
            return None
        lowered = text.lower()
        if any(token in lowered for token in ("螺纹", "攻丝", "牙", "thread", "tap", "tapped")):
            return match.group(1).upper()
        return None

    @staticmethod
    def _extract_count(text: str) -> int | None:
        match = re.search(r"(\d+)\s*(?:个|x)?\s*(?:孔|螺纹孔|攻丝孔|holes?)", text, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _has_center_position(text: str) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in ("中间", "中心", "center", "central"))

    @staticmethod
    def _has_corner_pattern(text: str) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in ("四角", "四个角", "4角", "corner", "corners"))

    @staticmethod
    def _prefixed_number(text: str, prefix: str) -> float | None:
        match = re.search(rf"\b{re.escape(prefix)}\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        return float(match.group(1)) if match else None

    @staticmethod
    def _number_after_keywords(text: str, keywords: tuple[str, ...]) -> float | None:
        for keyword in keywords:
            pattern = rf"{re.escape(keyword)}\s*[:=：]?\s*(\d+(?:\.\d+)?)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return float(match.group(1))
        return None
