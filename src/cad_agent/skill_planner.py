from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .brain_models import PlannedSkillStep


Predicate = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class SkillRule:
    skill_key: str
    action: str
    reason: str
    predicate: Predicate
    required: bool = True


def _feature_types(design: dict[str, Any]) -> set[str]:
    return {str(feature.get("type", "")) for feature in design.get("features", [])}


def _requested_outputs(design: dict[str, Any]) -> set[str]:
    explicit_outputs = {str(item).lower() for item in design.get("outputs", [])}
    # review_plan records validation wishes from older planners. It is not user
    # intent and must never schedule an export by itself.
    outputs = set(explicit_outputs)
    brief = str(design.get("source_brief", "")).lower()
    if "export_files" in _requested_stages(design):
        for token in ("step", "stp", "dwg", "dxf", "pdf", "stl", "iges", "igs"):
            if token in brief:
                outputs.add(token)
    return outputs


def _requested_stages(design: dict[str, Any]) -> set[str]:
    stages = set(str(item) for item in design.get("requested_stages", []))
    if not stages:
        stages.add("model_3d")
    return stages


def _stage_allowed(design: dict[str, Any], stage: str) -> bool:
    return stage in _requested_stages(design)


def _output_requested(design: dict[str, Any], *tokens: str) -> bool:
    outputs = _requested_outputs(design)
    for token in tokens:
        if token.lower() in outputs:
            outputs.add(token)
            return True
    return False


SKILL_RULES: tuple[SkillRule, ...] = (
    SkillRule(
        "source_part_clone",
        "clone_source_part",
        "Clone the explicit native source SLDPRT into the task directory and verify source parity.",
        lambda design: "source_part_clone" in _allowed_skills(design),
    ),
    SkillRule(
        "base_plate",
        "create_base_plate",
        "Create only the requested base Part in the task directory.",
        lambda design: "base_plate" in _allowed_skills(design),
    ),
    SkillRule(
        "revolve",
        "apply_revolve",
        "Create the requested explicit half-profile revolve without running a template.",
        lambda design: "revolve" in _allowed_skills(design),
    ),
    SkillRule(
        "profile_extrude",
        "apply_profile_extrude",
        "Extrude explicit closed line, arc, and circle profile loops without a template.",
        lambda design: "profile_extrude" in _allowed_skills(design),
    ),
    SkillRule(
        "dome",
        "apply_dome",
        "Create one native Dome on an explicitly resolved planar face.",
        lambda design: "dome" in _allowed_skills(design),
    ),
    SkillRule(
        "gear",
        "create_spur_gear",
        "Create one parameterized involute spur gear without Toolbox or templates.",
        lambda design: "gear" in _allowed_skills(design),
    ),
    SkillRule(
        "gear_pair",
        "create_spur_gear_pair",
        "Create two explicit spur gears, center-distance positioning, and a native Gear Mate.",
        lambda design: "gear_pair" in _allowed_skills(design),
    ),
    SkillRule(
        "sweep",
        "apply_sweep",
        "Create the explicit circular-profile sweep along the requested path.",
        lambda design: "sweep" in _allowed_skills(design),
    ),
    SkillRule(
        "loft",
        "apply_loft",
        "Create the requested loft from ordered closed sections.",
        lambda design: "loft" in _allowed_skills(design),
    ),
    SkillRule(
        "sheet_metal",
        "apply_sheet_metal",
        "Create the requested native base flange and bounded sheet-metal operations.",
        lambda design: "sheet_metal" in _allowed_skills(design),
    ),
    SkillRule(
        "weldment",
        "apply_weldment",
        "Create native structural members from the explicit 3D path and installed ISO profile.",
        lambda design: "weldment" in _allowed_skills(design),
    ),
    SkillRule(
        "freeform_surface",
        "apply_freeform_surface",
        "Create one native fill surface from the explicit closed 3D spline boundary.",
        lambda design: "freeform_surface" in _allowed_skills(design),
    ),
    SkillRule(
        "fillet",
        "apply_fillet",
        "Apply only the requested production fillet to the active Part.",
        lambda design: "fillet" in _allowed_skills(design),
    ),
    SkillRule(
        "boss",
        "apply_boss",
        "Add the requested boss to the active production Part.",
        lambda design: "boss" in _allowed_skills(design),
    ),
    SkillRule(
        "rib",
        "apply_rib",
        "Add only the requested side-wall reinforcing ribs to the active Part.",
        lambda design: "rib" in _allowed_skills(design),
    ),
    SkillRule(
        "side_boss",
        "apply_side_boss",
        "Add the requested bearing or flange bosses on explicit side faces.",
        lambda design: "side_boss" in _allowed_skills(design),
    ),
    SkillRule(
        "reference_geometry",
        "apply_reference_geometry",
        "Create only the requested offset plane or two-plane reference axis.",
        lambda design: "reference_geometry" in _allowed_skills(design),
    ),
    SkillRule(
        "draft",
        "apply_draft",
        "Draft only the explicitly selected outer faces about the requested neutral plane.",
        lambda design: "draft" in _allowed_skills(design),
    ),
    SkillRule(
        "shell",
        "apply_shell",
        "Shell the active Part with explicit wall thickness and opening faces.",
        lambda design: "shell" in _allowed_skills(design),
    ),
    SkillRule(
        "configuration",
        "apply_configuration",
        "Create only the explicitly named SolidWorks configuration.",
        lambda design: "configuration" in _allowed_skills(design),
    ),
    SkillRule(
        "equation",
        "apply_equation",
        "Add the explicit SolidWorks equation or global variable with its requested scope.",
        lambda design: "equation" in _allowed_skills(design),
    ),
    SkillRule(
        "chamfer",
        "apply_chamfer",
        "Apply only the requested production chamfer to external edges.",
        lambda design: "chamfer" in _allowed_skills(design),
    ),
    SkillRule(
        "external_thread",
        "apply_external_thread",
        "Apply only the requested ISO cosmetic external thread to an explicitly resolved shaft end.",
        lambda design: "external_thread" in _allowed_skills(design),
    ),
    SkillRule(
        "through_hole",
        "apply_through_hole",
        "Apply the requested holes to their explicit active-model faces.",
        lambda design: "through_hole" in _allowed_skills(design),
    ),
    SkillRule(
        "threaded_hole",
        "apply_threaded_hole",
        "Create native Hole Wizard tapped holes on explicitly resolved active-model faces.",
        lambda design: "threaded_hole" in _allowed_skills(design),
    ),
    SkillRule(
        "side_hole",
        "apply_side_hole",
        "Cut the requested side bearing bores or flange bolt-circle holes.",
        lambda design: "side_hole" in _allowed_skills(design),
    ),
    SkillRule(
        "pocket",
        "apply_pocket",
        "Cut the requested pocket in the active production Part.",
        lambda design: "pocket" in _allowed_skills(design),
    ),
    SkillRule(
        "slot",
        "apply_slot",
        "Cut the requested slots in the active production Part.",
        lambda design: "slot" in _allowed_skills(design),
    ),
    SkillRule(
        "linear_pattern",
        "apply_linear_pattern",
        "Pattern only the explicitly named seed features along the requested linear direction.",
        lambda design: "linear_pattern" in _allowed_skills(design),
    ),
    SkillRule(
        "circular_pattern",
        "apply_circular_pattern",
        "Pattern only the explicitly named seed features around the requested axis.",
        lambda design: "circular_pattern" in _allowed_skills(design),
    ),
    SkillRule(
        "mirror",
        "apply_mirror",
        "Mirror only the explicitly named seed features about the requested reference plane.",
        lambda design: "mirror" in _allowed_skills(design),
    ),
    SkillRule(
        "save_sldprt",
        "save_sldprt",
        "Save the final requested Part without exporting other formats.",
        lambda design: "save_sldprt" in _allowed_skills(design),
    ),
    SkillRule(
        "assembly_mate",
        "apply_mates",
        "Create or modify the explicit component assembly and apply only requested mates.",
        lambda design: "assembly_mate" in _allowed_skills(design),
    ),
    SkillRule(
        "solidworks_drawing",
        "generate_drawing",
        "Drawing request requires drawing generation.",
        lambda design: _stage_allowed(design, "drawing"),
    ),
    SkillRule(
        "autocad_annotation",
        "annotate_dwg",
        "AutoCAD annotation stage was requested.",
        lambda design: _stage_allowed(design, "autocad_annotation"),
    ),
    SkillRule(
        "step_export",
        "export_step",
        "STEP output was requested.",
        lambda design: _stage_allowed(design, "export_files") and _output_requested(design, "step", "stp"),
    ),
    SkillRule(
        "pdf_export",
        "export_pdf",
        "PDF output was requested.",
        lambda design: _stage_allowed(design, "export_files") and _output_requested(design, "pdf"),
    ),
    SkillRule(
        "dwg_export",
        "export_dwg",
        "DWG or generic DXF output was requested and allowed by the stage gate.",
        lambda design: (
            "dwg_export" in _allowed_skills(design)
            and _stage_allowed(design, "export_files")
            and _output_requested(design, "dwg", "dxf")
        ),
    ),
)


def _allowed_skills(design: dict[str, Any]) -> set[str]:
    return set(design.get("execution_policy", {}).get("allowed_skills", []))


class SkillPlanner:
    """Create an executable Skill Pipeline from Design JSON."""

    def __init__(self, rules: tuple[SkillRule, ...] = SKILL_RULES) -> None:
        self.rules = rules

    def plan(self, design: dict[str, Any]) -> list[PlannedSkillStep]:
        steps: list[PlannedSkillStep] = []
        seen: set[tuple[str, str]] = set()
        for rule in self.rules:
            if not rule.predicate(design):
                continue
            key = (rule.skill_key, rule.action)
            if key in seen:
                continue
            seen.add(key)
            steps.append(
                PlannedSkillStep(
                    skill_key=rule.skill_key,
                    action=rule.action,
                    reason=rule.reason,
                    inputs={"design_json": design},
                    required=rule.required,
                )
            )
        return self._order_by_cad_ir(steps, design)

    @staticmethod
    def _order_by_cad_ir(
        steps: list[PlannedSkillStep],
        design: dict[str, Any],
    ) -> list[PlannedSkillStep]:
        """Keep production Skills in the validated CAD-IR feature order.

        Consecutive features handled by one Skill share a lifecycle call. If a
        different operation separates them, the Skill is scheduled again with
        an explicit feature-id batch. This preserves feature-tree order such as
        ``profile_extrude -> revolve -> fillet -> profile_extrude`` without
        adding a part-specific Skill.
        """
        templates = {step.skill_key: step for step in steps}
        groups = SkillPlanner._ordered_feature_groups(design, set(templates))
        if groups:
            ordered_steps: list[PlannedSkillStep] = []
            consumed: set[str] = set()
            for skill_key, feature_ids in groups:
                template = templates.get(skill_key)
                if template is None:
                    continue
                inputs = dict(template.inputs)
                inputs["feature_ids"] = feature_ids
                ordered_steps.append(
                    PlannedSkillStep(
                        skill_key=template.skill_key,
                        action=template.action,
                        reason=template.reason,
                        inputs=inputs,
                        required=template.required,
                    )
                )
                consumed.add(skill_key)
            if ordered_steps:
                return [
                    *ordered_steps,
                    *(step for step in steps if step.skill_key not in consumed),
                ]

        ordered_keys = SkillPlanner.ordered_skill_keys(
            design,
            [step.skill_key for step in steps],
        )
        if not ordered_keys:
            return steps
        priority = {skill_key: index for index, skill_key in enumerate(ordered_keys)}
        feature_steps = [step for step in steps if step.skill_key in priority]
        if not feature_steps:
            return steps
        ordered_features = sorted(feature_steps, key=lambda step: priority[step.skill_key])
        feature_keys = {id(step) for step in feature_steps}
        remaining = [step for step in steps if id(step) not in feature_keys]
        return [*ordered_features, *remaining]

    @staticmethod
    def _ordered_feature_groups(
        design: dict[str, Any],
        available: set[str],
    ) -> list[tuple[str, list[str]]]:
        """Return stable topological CAD-IR operation batches."""
        raw_features = design.get("cad_ir", {}).get("features", [])
        if not isinstance(raw_features, list) or not raw_features:
            return []

        features = [item for item in raw_features if isinstance(item, dict)]
        identifiers = [str(item.get("id") or "").strip() for item in features]
        if not identifiers or any(not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
            return []

        index_by_id = {feature_id: index for index, feature_id in enumerate(identifiers)}
        dependencies: dict[str, set[str]] = {feature_id: set() for feature_id in identifiers}
        consumers: dict[str, set[str]] = {feature_id: set() for feature_id in identifiers}
        for feature_id, feature in zip(identifiers, features):
            for dependency in list(feature.get("dependencies") or []):
                if isinstance(dependency, dict):
                    dependency_id = str(dependency.get("feature_id") or "").strip()
                else:
                    dependency_id = str(dependency or "").strip()
                if dependency_id not in dependencies or dependency_id == feature_id:
                    continue
                dependencies[feature_id].add(dependency_id)
                consumers[dependency_id].add(feature_id)

        ready = sorted(
            (feature_id for feature_id in identifiers if not dependencies[feature_id]),
            key=index_by_id.__getitem__,
        )
        ordered_ids: list[str] = []
        while ready:
            feature_id = ready.pop(0)
            ordered_ids.append(feature_id)
            for consumer in sorted(consumers[feature_id], key=index_by_id.__getitem__):
                dependencies[consumer].discard(feature_id)
                if not dependencies[consumer] and consumer not in ordered_ids and consumer not in ready:
                    ready.append(consumer)
                    ready.sort(key=index_by_id.__getitem__)
        if len(ordered_ids) != len(identifiers):
            return []

        feature_by_id = dict(zip(identifiers, features))
        groups: list[tuple[str, list[str]]] = []
        for feature_id in ordered_ids:
            operation = str(feature_by_id[feature_id].get("operation") or "").strip()
            if operation not in available:
                continue
            if groups and groups[-1][0] == operation:
                groups[-1][1].append(feature_id)
            else:
                groups.append((operation, [feature_id]))
        return groups

    @staticmethod
    def ordered_skill_keys(design: dict[str, Any], available: list[str]) -> list[str]:
        """Topologically apply explicit CAD-IR dependencies to legacy order."""
        legacy_order = list(dict.fromkeys(available))
        ir_features = design.get("cad_ir", {}).get("features", [])
        if not isinstance(ir_features, list) or not ir_features:
            return legacy_order

        operation_by_id = {
            str(feature.get("id") or ""): str(feature.get("operation") or "").strip()
            for feature in ir_features
            if str(feature.get("id") or "").strip()
        }
        edges: dict[str, set[str]] = {skill_key: set() for skill_key in legacy_order}
        indegree = {skill_key: 0 for skill_key in legacy_order}
        for feature in ir_features:
            operation = str(feature.get("operation") or "").strip()
            if operation not in indegree:
                continue
            for dependency in list(feature.get("dependencies") or []):
                dependency_operation = operation_by_id.get(str(dependency.get("feature_id") or ""), "")
                if dependency_operation not in indegree or dependency_operation == operation:
                    continue
                if operation not in edges[dependency_operation]:
                    edges[dependency_operation].add(operation)
                    indegree[operation] += 1

        priority = {skill_key: index for index, skill_key in enumerate(legacy_order)}
        ready = sorted(
            (skill_key for skill_key in legacy_order if indegree[skill_key] == 0),
            key=priority.__getitem__,
        )
        ordered: list[str] = []
        while ready:
            skill_key = ready.pop(0)
            ordered.append(skill_key)
            for consumer in sorted(edges[skill_key], key=priority.__getitem__):
                indegree[consumer] -= 1
                if indegree[consumer] == 0:
                    ready.append(consumer)
                    ready.sort(key=priority.__getitem__)
        return ordered if len(ordered) == len(legacy_order) else legacy_order
