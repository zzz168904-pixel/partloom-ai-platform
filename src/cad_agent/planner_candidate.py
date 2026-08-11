from __future__ import annotations

import copy
import json
import re
from typing import Any

from .cad_ir import CADIRCompiler


CANDIDATE_SCHEMA_VERSION = "cad.planner_candidate.v1"
PLANNING_CONTRACT_VERSION = "candidate_planner.v1"

FEATURE_FIELDS = (
    "id",
    "operation",
    "depends_on",
    "target_body",
    "target_reference",
    "parameters",
    "evidence",
    "assumptions",
    "unresolved",
    "confidence",
)


def candidate_schema() -> dict[str, Any]:
    return {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "required": [
            "candidate_schema_version",
            "part_type",
            "task_type",
            "parameters",
            "features",
            "outputs",
            "assumptions",
            "unresolved",
            "confidence",
        ],
        "feature_required": list(FEATURE_FIELDS),
        "operations": list(CADIRCompiler.STANDARD_OPERATIONS),
        "operation_aliases": dict(CADIRCompiler.OPERATION_ALIASES),
        "operation_contracts": CADIRCompiler.operation_contracts(),
        "outputs": {"type": "array", "items": "string", "negative_requests_must_be_omitted": True},
        "unit_system": {"linear": "mm", "angle": "deg"},
    }


def parse_candidate_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    text = str(value or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("planner_output_not_json_object")
    try:
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"planner_output_invalid_json: {exc.msg}") from exc
    if not isinstance(result, dict):
        raise ValueError("planner_output_not_json_object")
    return result


def normalize_candidate(
    raw: dict[str, Any],
    prompt: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if _is_legacy_design(raw):
        return legacy_design_to_candidate(raw, prompt), []

    errors: list[dict[str, Any]] = []
    for field in candidate_schema()["required"]:
        if field not in raw:
            errors.append(_issue(
                "candidate_field_missing",
                f"Candidate plan is missing {field}.",
                field=field,
            ))
    schema_version = str(raw.get("candidate_schema_version") or raw.get("schema_version") or "")
    if schema_version != CANDIDATE_SCHEMA_VERSION:
        errors.append(_issue(
            "candidate_schema_version_invalid",
            f"Expected {CANDIDATE_SCHEMA_VERSION}, got {schema_version or '<missing>'}.",
            field="candidate_schema_version",
        ))

    part_type = str(raw.get("part_type") or "").strip()
    if not part_type:
        errors.append(_issue("candidate_field_missing", "part_type is required.", field="part_type"))
    task_type = str(raw.get("task_type") or "model_3d").strip()
    features_raw = raw.get("features")
    if not isinstance(features_raw, list) or not features_raw:
        errors.append(_issue("candidate_features_invalid", "features must be a non-empty list.", field="features"))
        features_raw = []

    features: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(features_raw):
        path = f"features[{index}]"
        if not isinstance(item, dict):
            errors.append(_issue("candidate_feature_invalid", "Feature must be an object.", field=path))
            continue
        missing = [name for name in FEATURE_FIELDS if name not in item]
        for field in missing:
            errors.append(_issue(
                "candidate_field_missing",
                f"Feature {index + 1} is missing {field}.",
                field=f"{path}.{field}",
            ))

        feature_id = _slug(item.get("id") or f"feature_{index + 1}")
        if feature_id in seen_ids:
            errors.append(_issue(
                "candidate_feature_id_duplicate",
                f"Duplicate feature id: {feature_id}.",
                feature_id=feature_id,
                field=f"{path}.id",
            ))
        seen_ids.add(feature_id)

        source_operation = str(item.get("operation") or "").strip().lower()
        operation, ambiguous = CADIRCompiler._canonical_feature_type(source_operation)
        if ambiguous:
            errors.append(_issue(
                "ambiguous_feature_type",
                f"Operation {source_operation!r} is ambiguous.",
                feature_id=feature_id,
                field=f"{path}.operation",
            ))
        depends_on = item.get("depends_on") if isinstance(item.get("depends_on"), list) else []
        for field in ("depends_on", "evidence", "assumptions", "unresolved"):
            value = item.get(field)
            if field in item and (
                not isinstance(value, list)
                or any(not isinstance(entry, str) for entry in value)
            ):
                errors.append(_issue(
                    "candidate_list_field_invalid",
                    f"{path}.{field} must be a list of strings.",
                    feature_id=feature_id,
                    field=f"{path}.{field}",
                ))
        parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        target_reference = item.get("target_reference")
        if target_reference is not None and not isinstance(target_reference, dict):
            errors.append(_issue(
                "candidate_target_reference_invalid",
                "target_reference must be an object or null.",
                feature_id=feature_id,
                field=f"{path}.target_reference",
            ))
            target_reference = None
        confidence = _confidence(item.get("confidence"), errors, feature_id, f"{path}.confidence")
        features.append({
            "id": feature_id,
            "operation": operation,
            "source_operation": source_operation,
            "depends_on": [str(value).strip() for value in depends_on if str(value).strip()],
            "target_body": str(item.get("target_body") or "").strip(),
            "target_reference": copy.deepcopy(target_reference),
            "parameters": copy.deepcopy(parameters),
            "evidence": _string_list(item.get("evidence")),
            "assumptions": _string_list(item.get("assumptions")),
            "unresolved": _string_list(item.get("unresolved")),
            "confidence": confidence,
        })

    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        errors.append(_issue(
            "candidate_parameters_invalid",
            "parameters must be an object.",
            field="parameters",
        ))
        parameters = {}
    for field in ("outputs", "assumptions", "unresolved"):
        value = raw.get(field)
        if field in raw and (
            not isinstance(value, list)
            or any(not isinstance(entry, str) for entry in value)
        ):
            errors.append(_issue(
                "candidate_list_field_invalid",
                f"{field} must be a list of strings.",
                field=field,
            ))

    candidate = {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "source_prompt": prompt.strip(),
        "part_type": part_type or "unspecified_part",
        "task_type": task_type or "model_3d",
        "parameters": copy.deepcopy(parameters),
        "features": features,
        "outputs": _string_list(raw.get("outputs")),
        "assumptions": _string_list(raw.get("assumptions")),
        "unresolved": _string_list(raw.get("unresolved")),
        "confidence": _confidence(raw.get("confidence"), errors, None, "confidence"),
    }
    return candidate, errors


def legacy_design_to_candidate(raw: dict[str, Any], prompt: str) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    previous_body_feature: str | None = None
    top_confidence = _safe_confidence(raw.get("confidence"), 0.85)
    for index, item in enumerate(list(raw.get("features") or []), start=1):
        if not isinstance(item, dict):
            continue
        source_operation = str(item.get("type") or item.get("operation") or "").strip().lower()
        operation, _ambiguous = CADIRCompiler._canonical_feature_type(source_operation)
        feature_id = _slug(item.get("id") or item.get("name") or f"feature_{index}")
        params = copy.deepcopy(item.get("params") or item.get("parameters") or {})
        explicit_dependencies = item.get("depends_on") or item.get("dependencies") or []
        depends_on = explicit_dependencies if isinstance(explicit_dependencies, list) else [explicit_dependencies]
        target_reference = copy.deepcopy(item.get("target_reference"))
        geometry = params.get("geometry_context") or params.get("target_geometry")
        if target_reference is None and isinstance(geometry, dict):
            target_reference = {
                "feature_id": geometry.get("target_feature"),
                "role": geometry.get("target_face_role"),
            }
        if operation in CADIRCompiler.MODIFIER_OPERATIONS and target_reference is None and previous_body_feature:
            target_reference = {
                "feature_id": previous_body_feature,
                "role": _default_target_role(operation),
            }
        if operation in CADIRCompiler.MODIFIER_OPERATIONS and not depends_on and previous_body_feature:
            depends_on = [previous_body_feature]
        features.append({
            "id": feature_id,
            "operation": operation,
            "source_operation": source_operation,
            "depends_on": [str(value).strip() for value in depends_on if str(value).strip()],
            "target_body": str(item.get("target_body") or params.get("target_body") or "body_01"),
            "target_reference": target_reference,
            "parameters": params,
            "evidence": _string_list(item.get("evidence")) or [f"Legacy planner identified {operation}."],
            "assumptions": _string_list(item.get("assumptions")),
            "unresolved": _string_list(item.get("unresolved")),
            "confidence": _safe_confidence(item.get("confidence"), top_confidence),
        })
        if operation in ({"base_plate"} | CADIRCompiler.NEW_DOCUMENT_OPERATIONS | {"boss", "revolve", "sweep", "loft"}):
            previous_body_feature = feature_id

    unresolved = _string_list(raw.get("unresolved"))
    for item in list(raw.get("unsupported_features") or []):
        if isinstance(item, dict) and item.get("reason"):
            unresolved.append(str(item["reason"]))
    return {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "source_prompt": prompt.strip(),
        "part_type": str(raw.get("part_family") or raw.get("part_type") or "mechanical_part"),
        "task_type": str(raw.get("task_type") or "model_3d"),
        "parameters": copy.deepcopy(raw.get("parameters") or {}),
        "features": features,
        "outputs": _string_list(raw.get("outputs")),
        "assumptions": _string_list(raw.get("assumptions")),
        "unresolved": list(dict.fromkeys(unresolved)),
        "confidence": top_confidence,
    }


def candidate_to_design(candidate: dict[str, Any], prompt: str) -> dict[str, Any]:
    features = []
    for item in candidate.get("features", []):
        feature = {
            "id": item["id"],
            "name": item["id"],
            "type": item["operation"],
            "required": True,
            "params": copy.deepcopy(item.get("parameters") or {}),
            "depends_on": list(item.get("depends_on") or []),
            "target_body": item.get("target_body"),
            "target_reference": copy.deepcopy(item.get("target_reference")),
        }
        features.append(feature)
    return {
        "schema_version": "vibecad.design.v1",
        "source_brief": prompt.strip(),
        "part_family": candidate.get("part_type"),
        "task_type": candidate.get("task_type") or "model_3d",
        "parameters": copy.deepcopy(candidate.get("parameters") or {"unit": "mm"}),
        "features": features,
        "outputs": list(candidate.get("outputs") or []),
        "datums": [],
        "tolerances": [],
        "assembly_relations": [],
        "assumptions": list(candidate.get("assumptions") or []),
        "unresolved": list(candidate.get("unresolved") or []),
        "unsupported_features": [],
        "risks": [],
        "needs_confirmation": False,
        "review_plan": {
            "expected_outputs": list(candidate.get("outputs") or []),
            "views": [],
            "checks": ["planner_candidate_validated", "cad_ir_valid", "outputs_exist"],
        },
        "planning_contract": PLANNING_CONTRACT_VERSION,
    }


def _is_legacy_design(raw: dict[str, Any]) -> bool:
    if raw.get("candidate_schema_version") == CANDIDATE_SCHEMA_VERSION:
        return False
    features = raw.get("features")
    return isinstance(features, list) and any(
        isinstance(item, dict) and "type" in item and "operation" not in item
        for item in features
    )


def _default_target_role(operation: str) -> str:
    if operation in {"fillet", "chamfer"}:
        return "selected_outer_edges"
    if operation in {"linear_pattern", "circular_pattern", "mirror"}:
        return "seed_features"
    return "outer_planar_face"


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_") or "feature"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _safe_confidence(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, numeric))


def _confidence(
    value: Any,
    errors: list[dict[str, Any]],
    feature_id: str | None,
    field: str,
) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        errors.append(_issue(
            "candidate_confidence_invalid",
            "confidence must be a number from 0 to 1.",
            feature_id=feature_id,
            field=field,
        ))
        return 0.0
    if numeric < 0.0 or numeric > 1.0:
        errors.append(_issue(
            "candidate_confidence_invalid",
            "confidence must be a number from 0 to 1.",
            feature_id=feature_id,
            field=field,
        ))
    return min(1.0, max(0.0, numeric))


def _issue(
    code: str,
    message: str,
    *,
    feature_id: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "feature_id": feature_id,
        "field": field,
    }
