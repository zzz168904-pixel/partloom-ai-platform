from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class EngineeringKnowledgeBase:
    """Versioned drafting rules and CAD command metadata for planners."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parent
        self.rules = self._load("mechanical_drawing_rules.json")
        self.catalog = self._load("cad_command_catalog.json")
        commands = self.catalog.get("commands", [])
        self.commands = {str(item["key"]): dict(item) for item in commands}

    @property
    def version(self) -> str:
        return str(self.rules.get("knowledge_version") or self.catalog.get("catalog_version") or "unknown")

    def planner_context(self) -> dict[str, Any]:
        return {
            "knowledge_version": self.version,
            "default_profile": self.rules.get("default_profile"),
            "drawing_rules": [
                {"id": item.get("id"), "topic": item.get("topic"), "rule": item.get("rule")}
                for item in self.rules.get("rules", [])
            ],
            "cad_commands": [
                {
                    "key": item.get("key"),
                    "domain": item.get("domain"),
                    "status": item.get("status"),
                    "required_parameters": item.get("required_parameters", []),
                }
                for item in self.catalog.get("commands", [])
            ],
            "planner_policy": [
                "Preserve every requested feature and output.",
                "Never invent a missing dimension, tolerance, datum or projection method.",
                "A planning_only command must be listed in unsupported_features for real execution.",
                "Return explicit parameters for every routed CAD command.",
            ],
        }

    def enrich_design(self, design: dict[str, Any]) -> dict[str, Any]:
        design["engineering_knowledge_version"] = self.version
        design["standards_profile"] = self._standards_profile(design)
        design["drawing_plan"] = self._drawing_plan(design)
        command_plan, new_unsupported = self._command_plan(design)
        design["cad_command_plan"] = command_plan
        unsupported = list(design.get("unsupported_features", []))
        for item in new_unsupported:
            identity = (item.get("type"), item.get("command"), item.get("reason"))
            if not any((old.get("type"), old.get("command"), old.get("reason")) == identity for old in unsupported):
                unsupported.append(item)
        design["unsupported_features"] = unsupported
        blocking = [item for item in new_unsupported if item.get("required", True)]
        if blocking:
            design["needs_confirmation"] = True
            reason = "Required CAD operations are not executable or have incomplete parameters: " + ", ".join(
                str(item.get("command") or item.get("type")) for item in blocking
            )
            existing = str(design.get("confirmation_reason") or "").strip()
            design["confirmation_reason"] = "; ".join(item for item in (existing, reason) if item)
        design["knowledge_sources"] = [
            {"id": item.get("id"), "url": item.get("url"), "status_checked": item.get("status_checked")}
            for item in self.rules.get("sources", [])
        ]
        return design

    def _command_plan(self, design: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        commands: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        requested_stages = set(design.get("requested_stages", []))
        parameters = dict(design.get("parameters", {}))

        if "model_3d" in requested_stages and all(parameters.get(key) is not None for key in ("length", "width", "thickness")):
            commands.append(self._planned_command("base_plate", parameters, "body_parameters", True))

        for feature in design.get("features", []):
            key = str(feature.get("type") or "")
            entry = self.commands.get(key)
            required = bool(feature.get("required", True))
            feature_params = dict(feature.get("params", {}))
            if key == "chamfer" and "size" not in feature_params and "distance" in feature_params:
                feature_params["size"] = feature_params["distance"]
            if key == "revolve":
                if "profile" not in feature_params and feature_params.get("segments"):
                    feature_params["profile"] = feature_params["segments"]
                if "angle" not in feature_params and feature_params.get("angle_deg") is not None:
                    feature_params["angle"] = feature_params["angle_deg"]
            if key == "shell":
                if "thickness" not in feature_params and feature_params.get("thickness_mm") is not None:
                    feature_params["thickness"] = feature_params["thickness_mm"]
                if "remove_faces" not in feature_params and feature_params.get("opening_faces") is not None:
                    feature_params["remove_faces"] = feature_params["opening_faces"]
            if key == "draft" and "angle" not in feature_params and feature_params.get("angle_deg") is not None:
                feature_params["angle"] = feature_params["angle_deg"]
            params = {**parameters, **feature_params}
            if entry is None:
                unsupported.append(
                    {
                        "type": "unknown_cad_command",
                        "command": key or "<missing>",
                        "reason": "The engineering command catalog has no definition for this requested feature.",
                        "required": required,
                    }
                )
                continue
            commands.append(self._planned_command(key, params, str(feature.get("name") or key), required))
            missing = self._missing_parameters(entry, params)
            if missing:
                unsupported.append(
                    {
                        "type": "incomplete_command_parameters",
                        "command": key,
                        "missing_parameters": missing,
                        "reason": f"Command {key} requires explicit parameters: {', '.join(missing)}.",
                        "required": required,
                    }
                )
            if entry.get("status") != "production":
                unsupported.append(
                    {
                        "type": "production_executor_missing",
                        "command": key,
                        "reason": f"Command {key} is understood by the planner but has no registered production executor.",
                        "required": required,
                    }
                )

        stage_commands = {
            "drawing": ("drawing", {"views": design.get("drawing_plan", {}).get("views", [])}),
            "autocad_annotation": ("autocad_annotation", {"intents": design.get("drawing_plan", {}).get("dimension_intents", [])}),
        }
        for stage, (key, params) in stage_commands.items():
            if stage in requested_stages:
                commands.append(self._planned_command(key, params, f"stage:{stage}", True))
        if "drawing" in requested_stages:
            for special_view in design.get("drawing_plan", {}).get("special_views", []):
                key = str(special_view.get("type") or "")
                if key in {"section_view", "detail_view"}:
                    commands.append(
                        self._planned_command(
                            key,
                            dict(special_view),
                            f"drawing_plan:{special_view.get('label') or key}",
                            bool(special_view.get("required", True)),
                        )
                    )

        output_commands = {
            "SLDPRT": "export_sldprt",
            "STEP": "export_step",
            "DWG": "export_dwg",
            "DXF": "export_dxf",
            "PDF": "export_pdf",
            "Annotated DWG": "autocad_annotation",
        }
        for output in design.get("outputs", []):
            key = output_commands.get(str(output))
            if key:
                commands.append(self._planned_command(key, {"format": output}, f"output:{output}", True))
        return self._deduplicate_commands(commands), unsupported

    def _drawing_plan(self, design: dict[str, Any]) -> dict[str, Any]:
        prompt = str(design.get("source_brief") or "")
        projection = "project_template"
        if any(token in prompt.lower() for token in ("first-angle", "first angle")) or "第一角" in prompt:
            projection = "first_angle"
        elif any(token in prompt.lower() for token in ("third-angle", "third angle")) or "第三角" in prompt:
            projection = "third_angle"

        intent_map = {
            "through_hole": ["diameter", "location_x", "location_y", "center_mark"],
            "threaded_hole": ["thread_callout", "location_x", "location_y", "center_mark"],
            "bolt_circle_pattern": ["pcd", "count", "thread_callout", "centerlines"],
            "slot": ["slot_length", "slot_width", "location", "centerlines"],
            "fillet": ["radius"],
            "chamfer": ["chamfer_callout"],
            "boss": ["boss_length", "boss_width", "boss_height"],
            "pocket": ["pocket_length", "pocket_width", "pocket_depth"],
        }
        intents = ["overall_length", "overall_width", "overall_height_or_thickness"]
        for feature in design.get("features", []):
            intents.extend(intent_map.get(str(feature.get("type") or ""), []))
        special_views = self._special_drawing_views(prompt, design.get("features", []))
        views = ["front", "top", "right", "isometric"]
        views.extend(str(item["type"]) for item in special_views)
        return {
            "profile": self._profile_name(design),
            "projection_method": projection,
            "views": views,
            "special_views": special_views,
            "dimension_intents": list(dict.fromkeys(intents)),
            "placement_strategy": "outside_views_then_collision_review",
            "native_annotation_policy": "associative_native_objects",
            "rule_ids": [item.get("id") for item in self.rules.get("rules", [])],
            "validation_checks": [
                "required_views_exist",
                "required_dimension_intents_covered",
                "no_duplicate_dimensions",
                "no_dimension_or_view_outside_usable_sheet",
                "no_unresolved_drawing_conflicts",
            ],
        }

    @staticmethod
    def _special_drawing_views(prompt: str, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lower = prompt.lower()
        feature_types = {str(item.get("type") or "") for item in features}
        section_requested = "section_view" in feature_types or any(
            token in lower for token in ("section view", "sectional view")
        ) or any(token in prompt for token in ("剖视图", "剖面图", "全剖图"))
        detail_requested = "detail_view" in feature_types or "detail view" in lower or any(
            token in prompt for token in ("局部放大图", "局部详图", "放大视图")
        )

        special_views: list[dict[str, Any]] = []
        if section_requested:
            source_view = "top" if "俯视图" in prompt else "right" if "右视图" in prompt else "front"
            orientation = "horizontal" if any(token in prompt for token in ("水平剖切", "横向剖切")) else "vertical"
            special_views.append(
                {
                    "type": "section_view",
                    "source_view": source_view,
                    "cutting_plane": {
                        "orientation": orientation,
                        "through": "center",
                        "source": "automatic_center_section_strategy",
                    },
                    "label": "A",
                    "required": True,
                }
            )
        if detail_requested:
            source_view = "top" if "俯视图" in prompt else "right" if "右视图" in prompt else "front" if "主视图" in prompt else "auto"
            scale = 2.0
            scale_match = re.search(
                r"(?:局部放大图|局部详图|放大视图|detail view)[^，。；\n]{0,24}?(\d+(?:\.\d+)?)\s*(?::|：|x|X|×)\s*1",
                prompt,
                flags=re.IGNORECASE,
            )
            if scale_match:
                scale = max(float(scale_match.group(1)), 1.01)
            special_views.append(
                {
                    "type": "detail_view",
                    "source_view": source_view,
                    "boundary": {
                        "normalized_center": [0.5, 0.5],
                        "radius_ratio": 0.24,
                        "source": "automatic_center_detail_strategy",
                    },
                    "scale": scale,
                    "label": "B",
                    "required": True,
                }
            )
        return special_views

    def _standards_profile(self, design: dict[str, Any]) -> dict[str, Any]:
        name = self._profile_name(design)
        profile = dict(self.rules.get("profiles", {}).get(name, {}))
        profile["name"] = name
        return profile

    def _profile_name(self, design: dict[str, Any]) -> str:
        prompt = str(design.get("source_brief") or "").lower()
        unit = str(design.get("parameters", {}).get("unit") or "mm").lower()
        if "asme" in prompt or unit in {"in", "inch", "inches"}:
            return "ASME_inch"
        return str(self.rules.get("default_profile") or "ISO_metric")

    def _planned_command(self, key: str, params: dict[str, Any], trigger: str, required: bool) -> dict[str, Any]:
        entry = self.commands.get(key, {})
        return {
            "command": key,
            "domain": entry.get("domain", "unknown"),
            "execution_status": entry.get("status", "unknown"),
            "trigger": trigger,
            "required": required,
            "parameters": params,
            "api_candidates": list(entry.get("api", [])),
        }

    @staticmethod
    def _missing_parameters(entry: dict[str, Any], params: dict[str, Any]) -> list[str]:
        aliases = {
            "pcd": ("pcd", "pcd_diameter", "pitch_circle_diameter"),
            "angle": ("angle", "total_angle"),
            "profiles": ("profiles", "sections"),
            "face_offset_mm": ("face_offset_mm", "support_face_offset_mm", "target_face_offset_mm"),
            "center_uv_mm": ("center_xz_mm", "center_yz_mm", "center_mm", "center"),
            "diameter_mm": ("diameter_mm", "diameter", "outer_diameter_mm", "outer_diameter", "hole_diameter_mm"),
            "depth_mm": ("depth_mm", "depth", "length_mm", "length"),
            "width_mm": ("width_mm", "width", "thickness_mm", "thickness"),
            "height_mm": ("height_mm", "height"),
        }

        def present(key: str) -> bool:
            candidates = aliases.get(key, (key,))
            return any(params.get(candidate) not in (None, "", [], {}) for candidate in candidates)

        missing: list[str] = []
        for raw_key in entry.get("required_parameters", []):
            requirement = str(raw_key).strip()
            alternatives = [item.strip() for item in requirement.split(" or ") if item.strip()]
            if len(alternatives) > 1:
                if not any(
                    all(present(key.strip()) for key in alternative.split("/") if key.strip())
                    for alternative in alternatives
                ):
                    missing.append(requirement)
                continue
            if not present(requirement):
                missing.append(requirement)
        return missing

    @staticmethod
    def _deduplicate_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in commands:
            identity = (str(item.get("command")), str(item.get("trigger")))
            if identity not in seen:
                seen.add(identity)
                result.append(item)
        return result

    def _load(self, name: str) -> dict[str, Any]:
        path = self.root / name
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"Engineering knowledge file must contain a JSON object: {path}")
        return data
