from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..cad_ir import CADIRCompiler
from .models import (
    FeatureTemplateNode,
    FeatureTreeNode,
    FeatureTreeRecord,
    ParameterSpec,
    PartTypeDefinition,
    ReferenceSpec,
    ResolvedReference,
    SkillOperationSpec,
    SkillRegistration,
)
from .registry import FeatureReferenceRegistry


SERVICE_OPERATION_CONTRACTS: dict[str, dict[str, Any]] = {
    "export_sldprt": {
        "required_parameters": [],
        "optional_parameters": ["target_path"],
        "required_any": [],
        "context_requirements": ["validated_part_document"],
        "conflict_rules": ["final_save_requires_valid_geometry"],
    },
    "export_step": {
        "required_parameters": [],
        "optional_parameters": ["target_path"],
        "required_any": [],
        "context_requirements": ["source_part_or_assembly"],
        "conflict_rules": ["output_must_be_explicitly_requested"],
    },
    "export_pdf": {
        "required_parameters": [],
        "optional_parameters": ["target_path"],
        "required_any": [],
        "context_requirements": ["source_drawing"],
        "conflict_rules": ["output_must_be_explicitly_requested"],
    },
    "export_dwg": {
        "required_parameters": [],
        "optional_parameters": ["target_path"],
        "required_any": [],
        "context_requirements": ["source_drawing"],
        "conflict_rules": ["output_must_be_explicitly_requested"],
    },
    "export_dxf": {
        "required_parameters": [],
        "optional_parameters": ["target_path"],
        "required_any": [],
        "context_requirements": ["source_drawing_or_flat_pattern"],
        "conflict_rules": ["output_must_be_explicitly_requested"],
    },
}


SPECIAL_SKILL_OPERATIONS: dict[str, tuple[str, ...]] = {
    "save_sldprt": ("export_sldprt",),
    "solidworks_drawing": ("drawing", "section_view", "detail_view"),
    "step_export": ("export_step",),
    "pdf_export": ("export_pdf",),
    "dwg_export": ("export_dwg", "export_dxf"),
    "autocad_annotation": ("autocad_annotation",),
    "solidworks_threaded_holes": ("threaded_hole", "bolt_circle_pattern"),
    "solidworks_cnc_fillet": ("fillet", "chamfer"),
}


def build_default_feature_registry(
    project_root: str | Path | None = None,
    *,
    include_verified_models: bool = True,
) -> FeatureReferenceRegistry:
    root = Path(project_root or Path(__file__).resolve().parents[3]).resolve()
    registry = FeatureReferenceRegistry()
    for skill in default_skill_registrations():
        registry.register_skill(skill)
    for part in default_part_types():
        registry.register_part_type(part)
    if include_verified_models:
        for record in verified_feature_trees(root):
            registry.register_feature_tree(record)
    return registry


def default_skill_registrations() -> tuple[SkillRegistration, ...]:
    # Imports are local to keep the schema package independent from the
    # existing runtime dispatcher during normal model imports.
    from ..registry import CADAgentSkillManager
    from ..skill_planner import SKILL_RULES

    runtime_contracts = CADAgentSkillManager.production_skill_contracts()
    skill_keys = {rule.skill_key for rule in SKILL_RULES} | set(runtime_contracts)
    skill_keys.add("solidworks_threaded_holes")
    registrations: list[SkillRegistration] = []
    for skill_key in sorted(skill_keys):
        contract = dict(runtime_contracts.get(skill_key) or _supplemental_skill_contract(skill_key))
        operation_names = SPECIAL_SKILL_OPERATIONS.get(skill_key)
        if operation_names is None:
            operation_names = (skill_key,)
        status = "test_only" if skill_key == "solidworks_cnc_fillet" else "production"
        production_ready = status == "production"
        registrations.append(
            SkillRegistration(
                skill_key=skill_key,
                operations=tuple(_operation_spec(name) for name in operation_names),
                status=status,
                source_kind="project",
                source_id="runtime_skill_catalog",
                capabilities=tuple(contract.get("capabilities") or operation_names),
                side_effects=tuple(contract.get("side_effects") or ()),
                output_types=tuple(
                    dict.fromkeys(
                        list(contract.get("output_types") or ())
                        + list(contract.get("conditional_output_types") or ())
                    )
                ),
                modifies_active_doc=bool(contract.get("modifies_active_doc")),
                creates_new_doc=bool(contract.get("creates_new_doc")),
                exports_files=bool(contract.get("exports_files")),
                uses_test_template=bool(contract.get("uses_test_template")),
                production_ready=production_ready,
                tags=("runtime", "candidate_planner_compatible"),
            )
        )
    return tuple(registrations)


def default_part_types() -> tuple[PartTypeDefinition, ...]:
    return (
        PartTypeDefinition(
            part_type="rectangular_mounting_plate",
            category="machined_part",
            aliases=("rectangular_plate", "mounting_plate", "base_plate"),
            parameters=(
                ParameterSpec("length_mm", "number", True, "mm"),
                ParameterSpec("width_mm", "number", True, "mm"),
                ParameterSpec("thickness_mm", "number", True, "mm"),
                ParameterSpec("center_hole_diameter_mm", "number", False, "mm"),
                ParameterSpec("corner_radius_mm", "number", False, "mm"),
            ),
            feature_tree=(
                FeatureTemplateNode(
                    node_id="base_01",
                    operation="base_plate",
                    skill_key="base_plate",
                    parameter_bindings={
                        "length_mm": "$length_mm",
                        "width_mm": "$width_mm",
                        "thickness_mm": "$thickness_mm",
                    },
                    evidence="Parameterized rectangular mounting plate base.",
                ),
                FeatureTemplateNode(
                    node_id="center_hole_01",
                    operation="through_hole",
                    skill_key="through_hole",
                    depends_on=("base_01",),
                    parameter_bindings={
                        "diameter_mm": "$center_hole_diameter_mm",
                        "position": "center",
                    },
                    target_reference={"feature_id": "base_01", "role": "outer_planar_face"},
                    required=False,
                    evidence="Optional center through hole on the base outer planar face.",
                ),
                FeatureTemplateNode(
                    node_id="corner_fillet_01",
                    operation="fillet",
                    skill_key="fillet",
                    depends_on=("base_01",),
                    parameter_bindings={
                        "radius_mm": "$corner_radius_mm",
                        "edge_selector": "outer_vertical_edges",
                    },
                    target_reference={"feature_id": "base_01", "role": "selected_outer_edges"},
                    required=False,
                    evidence="Optional outside corner fillet.",
                ),
            ),
            source_id="builtin_part_families",
            tags=("plate", "mounting", "cnc"),
        ),
        PartTypeDefinition(
            part_type="flanged_sleeve",
            category="turned_part",
            aliases=("sleeve", "flanged_bushing"),
            parameters=(
                ParameterSpec("profile", "array", True),
                ParameterSpec("angle_deg", "number", False, "deg"),
            ),
            feature_tree=(
                FeatureTemplateNode(
                    node_id="revolve_01",
                    operation="revolve",
                    skill_key="revolve",
                    parameter_bindings={"profile": "$profile", "angle_deg": "$angle_deg"},
                    evidence="Closed half-profile revolved about its explicit axis.",
                ),
            ),
            source_id="builtin_part_families",
            tags=("revolve", "sleeve", "shaft_component"),
        ),
        PartTypeDefinition(
            part_type="sheet_metal_angle_bracket",
            category="sheet_metal",
            aliases=("angle_bracket", "bent_bracket"),
            parameters=(
                ParameterSpec("length_mm", "number", True, "mm"),
                ParameterSpec("width_mm", "number", True, "mm"),
                ParameterSpec("thickness_mm", "number", True, "mm"),
                ParameterSpec("bend_radius_mm", "number", True, "mm"),
                ParameterSpec("flange_length_mm", "number", True, "mm"),
                ParameterSpec("flange_angle_deg", "number", True, "deg"),
            ),
            feature_tree=(
                FeatureTemplateNode(
                    node_id="sheet_metal_01",
                    operation="sheet_metal",
                    skill_key="sheet_metal",
                    parameter_bindings={
                        "length_mm": "$length_mm",
                        "width_mm": "$width_mm",
                        "thickness_mm": "$thickness_mm",
                        "bend_radius_mm": "$bend_radius_mm",
                        "edge_flange": {
                            "length_mm": "$flange_length_mm",
                            "angle_deg": "$flange_angle_deg",
                        },
                    },
                    evidence="Native base flange followed by one explicit edge flange.",
                ),
            ),
            source_id="builtin_part_families",
            tags=("sheet_metal", "bracket"),
        ),
        PartTypeDefinition(
            part_type="spur_gear",
            category="power_transmission",
            aliases=("involute_spur_gear",),
            parameters=(
                ParameterSpec("module_mm", "number", True, "mm"),
                ParameterSpec("tooth_count", "integer", True),
                ParameterSpec("face_width_mm", "number", True, "mm"),
                ParameterSpec("bore_diameter_mm", "number", False, "mm"),
            ),
            feature_tree=(
                FeatureTemplateNode(
                    node_id="gear_01",
                    operation="gear",
                    skill_key="gear",
                    parameter_bindings={
                        "module_mm": "$module_mm",
                        "tooth_count": "$tooth_count",
                        "face_width_mm": "$face_width_mm",
                        "bore_diameter_mm": "$bore_diameter_mm",
                    },
                    evidence="Parameterized involute spur gear.",
                ),
            ),
            source_id="builtin_part_families",
            tags=("gear", "involute"),
        ),
        PartTypeDefinition(
            part_type="welded_frame",
            category="weldment",
            aliases=("structural_frame",),
            parameters=(
                ParameterSpec("profile_type", "string", True),
                ParameterSpec("profile_configuration", "string", True),
                ParameterSpec("path_segments_mm", "array", True, "mm"),
            ),
            feature_tree=(
                FeatureTemplateNode(
                    node_id="weldment_01",
                    operation="weldment",
                    skill_key="weldment",
                    parameter_bindings={
                        "profile_type": "$profile_type",
                        "profile_configuration": "$profile_configuration",
                        "path_segments_mm": "$path_segments_mm",
                    },
                    evidence="Native structural members from explicit 3D path segments.",
                ),
            ),
            source_id="builtin_part_families",
            tags=("weldment", "frame"),
        ),
    )


def verified_feature_trees(project_root: Path) -> tuple[FeatureTreeRecord, ...]:
    report_path = project_root / "logs" / "deepseek_planner_e2e_acceptance" / "deepseek_planner_e2e_report.json"
    if not report_path.is_file():
        return ()
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    model_path = Path(str(report.get("solidworks", {}).get("model_path") or ""))
    validation = report.get("solidworks", {}).get("close_reopen_validation", {})
    if report.get("success") is not True or not model_path.is_file() or validation.get("success") is not True:
        return ()

    document_id = "verified_plate_center_hole_20260714"
    base_body = f"{document_id}:feat_01:primary_solid"
    base_feature = f"{document_id}:feat_01:base_feature"
    top_face = f"{document_id}:feat_01:outer_planar_face"
    cut_feature = f"{document_id}:feat_02:cut_feature"
    hole_axis = f"{document_id}:feat_02:hole_axis"
    cylindrical_face = f"{document_id}:feat_02:cylindrical_face"
    bbox = dict(validation.get("bbox_mm") or {})
    references = (
        ResolvedReference(
            base_body,
            document_id,
            "feat_01",
            "primary_solid",
            "Body",
            body_id="body_01",
            geometry_signature={
                "bbox_mm": bbox,
                "volume_m3": validation.get("volume_m3"),
            },
            source_skill="base_plate",
        ),
        ResolvedReference(
            base_feature,
            document_id,
            "feat_01",
            "base_feature",
            "Feature",
            body_id="body_01",
            geometry_signature={"solidworks_name": "BasePlate", "type": "Extrusion"},
            source_skill="base_plate",
        ),
        ResolvedReference(
            top_face,
            document_id,
            "feat_01",
            "outer_planar_face",
            "Face",
            body_id="body_01",
            geometry_signature={"normal": [0, 0, 1], "bbox_location": "z_max"},
            source_skill="base_plate",
        ),
        ResolvedReference(
            cut_feature,
            document_id,
            "feat_02",
            "cut_feature",
            "Feature",
            body_id="body_01",
            geometry_signature={"solidworks_name": "feat_02", "type": "ICE"},
            source_skill="through_hole",
        ),
        ResolvedReference(
            hole_axis,
            document_id,
            "feat_02",
            "hole_axis",
            "Axis",
            body_id="body_01",
            geometry_signature={"origin_uv_mm": [0, 0], "direction": [0, 0, 1]},
            source_skill="through_hole",
        ),
        ResolvedReference(
            cylindrical_face,
            document_id,
            "feat_02",
            "cylindrical_face",
            "Face",
            body_id="body_01",
            geometry_signature={"diameter_mm": 20.0, "extent": "through_all"},
            source_skill="through_hole",
        ),
    )
    nodes = (
        FeatureTreeNode(
            feature_id="feat_01",
            feature_name="BasePlate",
            operation="base_plate",
            skill_key="base_plate",
            order=0,
            parameters={"length_mm": 100.0, "width_mm": 60.0, "thickness_mm": 10.0},
            produces=(base_body, base_feature, top_face),
        ),
        FeatureTreeNode(
            feature_id="feat_02",
            feature_name="feat_02",
            operation="through_hole",
            skill_key="through_hole",
            order=1,
            depends_on=("feat_01",),
            parameters={"diameter_mm": 20.0, "position": "center"},
            consumes=(top_face,),
            produces=(cut_feature, hole_axis, cylindrical_face),
        ),
    )
    return (
        FeatureTreeRecord(
            document_id=document_id,
            part_type="rectangular_mounting_plate",
            nodes=nodes,
            references=references,
            model_path=str(model_path.resolve()),
            verification_report=str(report_path.resolve()),
            verified=True,
            source_kind="verified_model",
            source_id="deepseek_planner_e2e_acceptance",
            executable_template=False,
            metadata={
                "body_count": validation.get("body_count"),
                "bbox_mm": bbox,
                "volume_m3": validation.get("volume_m3"),
                "rebuild_error_count": validation.get("rebuild_error_count"),
                "feature_tree": validation.get("feature_tree", []),
            },
        ),
    )


def _operation_spec(operation: str) -> SkillOperationSpec:
    contract = CADIRCompiler.operation_contracts().get(operation) or SERVICE_OPERATION_CONTRACTS.get(operation)
    if contract is None:
        contract = {
            "required_parameters": [],
            "optional_parameters": [],
            "required_any": [],
            "context_requirements": [],
            "conflict_rules": [],
        }
    required = list(contract.get("required_parameters") or ())
    optional = list(contract.get("optional_parameters") or ())
    required_any = [tuple(group) for group in contract.get("required_any", ())]
    aliases = dict(contract.get("raw_alias_groups") or {})
    names = list(dict.fromkeys(required + optional + [name for group in required_any for name in group]))
    parameters = tuple(
        ParameterSpec(
            name=name,
            value_type=_parameter_type(name),
            required=name in required,
            unit=_parameter_unit(name),
            aliases=tuple(aliases.get(name, ())),
        )
        for name in names
    )
    consumes, produces = _reference_contract(operation)
    return SkillOperationSpec(
        operation=operation,
        parameters=parameters,
        required_any=tuple(required_any),
        consumes=consumes,
        produces=produces,
        context_requirements=tuple(contract.get("context_requirements") or ()),
        conflict_rules=tuple(contract.get("conflict_rules") or ()),
    )


def _reference_contract(operation: str) -> tuple[tuple[ReferenceSpec, ...], tuple[ReferenceSpec, ...]]:
    file_roles = {
        "export_sldprt": "sldprt_file",
        "export_step": "step_file",
        "export_pdf": "pdf_file",
        "export_dwg": "dwg_file",
        "export_dxf": "dxf_file",
    }
    if operation in file_roles:
        return (
            (ReferenceSpec("source_document", "Document"),),
            (ReferenceSpec(file_roles[operation], "File"),),
        )
    if operation == "base_plate":
        return (), (
            ReferenceSpec("primary_solid", "Body"),
            ReferenceSpec("base_feature", "Feature"),
            ReferenceSpec("outer_planar_face", "Face", multiple=True),
        )
    if operation in {"gear", "revolve", "profile_extrude", "sweep", "loft", "sheet_metal", "weldment"}:
        return (), (
            ReferenceSpec("primary_solid", "Body", multiple=operation == "weldment"),
            ReferenceSpec("base_feature", "Feature"),
        )
    if operation == "freeform_surface":
        return (), (
            ReferenceSpec("surface_feature", "Feature"),
            ReferenceSpec("surface_face", "Face", multiple=True),
        )
    if operation in {"through_hole", "threaded_hole", "bolt_circle_pattern"}:
        return (
            ReferenceSpec("target_body", "Body"),
            ReferenceSpec("outer_planar_face", "Face"),
        ), (
            ReferenceSpec("cut_feature", "Feature"),
            ReferenceSpec("hole_axis", "Axis", multiple=operation == "bolt_circle_pattern"),
            ReferenceSpec("cylindrical_face", "Face", multiple=True),
        )
    if operation == "external_thread":
        return (
            ReferenceSpec("target_body", "Body"),
            ReferenceSpec("axial_major_diameter_end_edge", "Edge"),
        ), (
            ReferenceSpec("thread_feature", "Feature"),
            ReferenceSpec("cosmetic_thread_feature", "Feature", required=False),
            ReferenceSpec("modeled_thread_feature", "Feature", required=False),
        )
    if operation in {"pocket", "slot", "side_hole"}:
        return (
            ReferenceSpec("target_body", "Body"),
            ReferenceSpec("target_face", "Face"),
        ), (ReferenceSpec("cut_feature", "Feature"),)
    if operation in {"boss", "side_boss", "rib"}:
        return (
            ReferenceSpec("target_body", "Body"),
            ReferenceSpec("target_face", "Face"),
        ), (
            ReferenceSpec("added_feature", "Feature"),
            ReferenceSpec("outer_planar_face", "Face", multiple=True),
        )
    if operation in {"fillet", "chamfer"}:
        return (
            ReferenceSpec("target_body", "Body"),
            ReferenceSpec("selected_edges", "Edge", multiple=True),
        ), (
            ReferenceSpec("edge_feature", "Feature"),
            ReferenceSpec("modified_edges", "Edge", multiple=True),
        )
    if operation in {"linear_pattern", "circular_pattern", "mirror"}:
        return (
            ReferenceSpec("seed_features", "Feature", multiple=True),
            ReferenceSpec("pattern_reference", "Axis" if operation == "circular_pattern" else "Plane"),
        ), (
            ReferenceSpec("pattern_feature", "Feature"),
            ReferenceSpec("instance_features", "Feature", multiple=True),
        )
    if operation in {"shell", "draft"}:
        return (
            ReferenceSpec("target_body", "Body"),
            ReferenceSpec("target_faces", "Face", multiple=True),
        ), (ReferenceSpec("modified_feature", "Feature"),)
    if operation == "reference_geometry":
        return (ReferenceSpec("source_references", "Plane", multiple=True),), (
            ReferenceSpec("reference_plane", "Plane", required=False),
            ReferenceSpec("reference_axis", "Axis", required=False),
        )
    if operation in {"equation", "configuration"}:
        return (ReferenceSpec("active_document", "Document"),), (
            ReferenceSpec("document_property", "Feature"),
        )
    if operation == "assembly_mate":
        return (ReferenceSpec("component_references", "Feature", multiple=True),), (
            ReferenceSpec("mate_feature", "Feature", multiple=True),
        )
    if operation == "drawing":
        return (ReferenceSpec("source_document", "Document"),), (
            ReferenceSpec("drawing_document", "Document"),
            ReferenceSpec("drawing_view", "View", multiple=True),
        )
    if operation in {"section_view", "detail_view"}:
        return (ReferenceSpec("parent_view", "View"),), (ReferenceSpec("drawing_view", "View"),)
    if operation == "autocad_annotation":
        return (ReferenceSpec("source_dwg", "File"),), (ReferenceSpec("annotated_dwg", "File"),)
    return (), (ReferenceSpec("feature", "Feature"),)


def _parameter_type(name: str) -> str:
    if any(token in name for token in ("count", "teeth", "instances", "quantity")):
        return "integer"
    if name.endswith(("_mm", "_deg")):
        return "number"
    if any(token in name for token in ("points", "segments", "sections", "profiles", "components", "mates", "features", "faces")):
        return "array"
    if name in {"profile", "wire_mesh", "lofted_bend"}:
        return "object"
    return "any"


def _parameter_unit(name: str) -> str | None:
    if name.endswith("_mm"):
        return "mm"
    if name.endswith("_deg") or "angle" in name:
        return "deg"
    return None


def _supplemental_skill_contract(skill_key: str) -> dict[str, Any]:
    contracts = {
        "autocad_annotation": {
            "capabilities": ["autocad_annotation"],
            "side_effects": ["modifies_dwg", "exports_files"],
            "output_types": ["Annotated DWG"],
            "modifies_active_doc": False,
            "creates_new_doc": False,
            "exports_files": True,
            "uses_test_template": False,
        },
        "pdf_export": {
            "capabilities": ["export_pdf"],
            "side_effects": ["exports_files"],
            "output_types": ["PDF"],
            "modifies_active_doc": False,
            "creates_new_doc": False,
            "exports_files": True,
            "uses_test_template": False,
        },
        "dwg_export": {
            "capabilities": ["export_dwg", "export_dxf"],
            "side_effects": ["exports_files"],
            "output_types": ["DWG", "DXF"],
            "modifies_active_doc": False,
            "creates_new_doc": False,
            "exports_files": True,
            "uses_test_template": False,
        },
        "solidworks_threaded_holes": {
            "capabilities": ["threaded_hole", "bolt_circle_pattern"],
            "side_effects": ["modifies_active_doc"],
            "output_types": [],
            "modifies_active_doc": True,
            "creates_new_doc": False,
            "exports_files": False,
            "uses_test_template": False,
        },
    }
    return contracts.get(skill_key, {"capabilities": [skill_key], "side_effects": [], "output_types": []})
