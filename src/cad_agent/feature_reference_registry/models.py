from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


REGISTRY_SCHEMA_VERSION = "cad.feature_reference_registry.v1"
CANDIDATE_SCHEMA_VERSION = "cad.planner_candidate.v1"

SOURCE_KINDS = {
    "builtin",
    "project",
    "verified_model",
    "standard_parts",
    "test_library",
    "plugin",
}
SKILL_STATUSES = {"production", "legacy", "planning_only", "test_only"}
VALUE_TYPES = {"number", "integer", "string", "boolean", "object", "array", "any"}
ENTITY_TYPES = {
    "Document",
    "Body",
    "Feature",
    "Sketch",
    "Face",
    "Edge",
    "Axis",
    "Plane",
    "Vertex",
    "View",
    "File",
}
REFERENCE_STATES = {"semantic", "resolved", "stale"}


class RegistryValidationError(ValueError):
    pass


class RegistryConflictError(RegistryValidationError):
    pass


class RegistryNotFoundError(KeyError):
    pass


def _require_identifier(value: str, field_name: str, *, operation: bool = False) -> str:
    text = str(value or "").strip()
    pattern = r"^[a-z][a-z0-9_]*$" if operation else r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
    if not text or re.fullmatch(pattern, text) is None:
        raise RegistryValidationError(f"{field_name} is not a valid identifier: {text!r}")
    return text


def _tuple_of_strings(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    return tuple(str(value).strip() for value in values if str(value).strip())


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    value_type: str = "any"
    required: bool = False
    unit: str | None = None
    aliases: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_identifier(self.name, "parameter.name", operation=True))
        if self.value_type not in VALUE_TYPES:
            raise RegistryValidationError(f"Unsupported parameter value_type: {self.value_type!r}")
        if self.unit not in {None, "mm", "deg", "ratio"}:
            raise RegistryValidationError(f"Unsupported parameter unit: {self.unit!r}")
        object.__setattr__(self, "aliases", _tuple_of_strings(self.aliases))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ParameterSpec":
        return cls(
            name=value.get("name", ""),
            value_type=value.get("value_type", "any"),
            required=bool(value.get("required")),
            unit=value.get("unit"),
            aliases=_tuple_of_strings(value.get("aliases")),
            description=str(value.get("description") or ""),
        )


@dataclass(frozen=True)
class ReferenceSpec:
    role: str
    entity_type: str
    required: bool = True
    multiple: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _require_identifier(self.role, "reference.role", operation=True))
        if self.entity_type not in ENTITY_TYPES:
            raise RegistryValidationError(f"Unsupported reference entity_type: {self.entity_type!r}")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReferenceSpec":
        return cls(
            role=value.get("role", ""),
            entity_type=value.get("entity_type", ""),
            required=bool(value.get("required", True)),
            multiple=bool(value.get("multiple")),
            description=str(value.get("description") or ""),
        )


@dataclass(frozen=True)
class SkillOperationSpec:
    operation: str
    parameters: tuple[ParameterSpec, ...] = ()
    required_any: tuple[tuple[str, ...], ...] = ()
    consumes: tuple[ReferenceSpec, ...] = ()
    produces: tuple[ReferenceSpec, ...] = ()
    context_requirements: tuple[str, ...] = ()
    conflict_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _require_identifier(self.operation, "operation", operation=True))
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise RegistryValidationError(f"Operation {self.operation!r} contains duplicate parameters.")
        object.__setattr__(
            self,
            "required_any",
            tuple(_tuple_of_strings(group) for group in self.required_any if _tuple_of_strings(group)),
        )
        object.__setattr__(self, "context_requirements", _tuple_of_strings(self.context_requirements))
        object.__setattr__(self, "conflict_rules", _tuple_of_strings(self.conflict_rules))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillOperationSpec":
        return cls(
            operation=value.get("operation", ""),
            parameters=tuple(ParameterSpec.from_dict(item) for item in value.get("parameters", [])),
            required_any=tuple(_tuple_of_strings(item) for item in value.get("required_any", [])),
            consumes=tuple(ReferenceSpec.from_dict(item) for item in value.get("consumes", [])),
            produces=tuple(ReferenceSpec.from_dict(item) for item in value.get("produces", [])),
            context_requirements=_tuple_of_strings(value.get("context_requirements")),
            conflict_rules=_tuple_of_strings(value.get("conflict_rules")),
        )


@dataclass(frozen=True)
class SkillRegistration:
    skill_key: str
    operations: tuple[SkillOperationSpec, ...]
    status: str = "production"
    version: str = "1.0"
    source_kind: str = "builtin"
    source_id: str = "project"
    capabilities: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    output_types: tuple[str, ...] = ()
    modifies_active_doc: bool = False
    creates_new_doc: bool = False
    exports_files: bool = False
    uses_test_template: bool = False
    production_ready: bool = True
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_key", _require_identifier(self.skill_key, "skill_key", operation=True))
        if not self.operations:
            raise RegistryValidationError(f"Skill {self.skill_key!r} must register at least one operation.")
        operation_names = [item.operation for item in self.operations]
        if len(operation_names) != len(set(operation_names)):
            raise RegistryValidationError(f"Skill {self.skill_key!r} contains duplicate operations.")
        if self.status not in SKILL_STATUSES:
            raise RegistryValidationError(f"Unsupported skill status: {self.status!r}")
        if self.source_kind not in SOURCE_KINDS:
            raise RegistryValidationError(f"Unsupported source_kind: {self.source_kind!r}")
        object.__setattr__(self, "capabilities", _tuple_of_strings(self.capabilities))
        object.__setattr__(self, "side_effects", _tuple_of_strings(self.side_effects))
        object.__setattr__(self, "output_types", _tuple_of_strings(self.output_types))
        object.__setattr__(self, "tags", _tuple_of_strings(self.tags))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillRegistration":
        return cls(
            skill_key=value.get("skill_key", ""),
            operations=tuple(SkillOperationSpec.from_dict(item) for item in value.get("operations", [])),
            status=value.get("status", "production"),
            version=str(value.get("version") or "1.0"),
            source_kind=value.get("source_kind", "builtin"),
            source_id=str(value.get("source_id") or "project"),
            capabilities=_tuple_of_strings(value.get("capabilities")),
            side_effects=_tuple_of_strings(value.get("side_effects")),
            output_types=_tuple_of_strings(value.get("output_types")),
            modifies_active_doc=bool(value.get("modifies_active_doc")),
            creates_new_doc=bool(value.get("creates_new_doc")),
            exports_files=bool(value.get("exports_files")),
            uses_test_template=bool(value.get("uses_test_template")),
            production_ready=bool(value.get("production_ready", True)),
            tags=_tuple_of_strings(value.get("tags")),
        )

    def operation(self, name: str) -> SkillOperationSpec | None:
        return next((item for item in self.operations if item.operation == name), None)


@dataclass(frozen=True)
class FeatureTemplateNode:
    node_id: str
    operation: str
    skill_key: str
    depends_on: tuple[str, ...] = ()
    parameter_bindings: dict[str, Any] = field(default_factory=dict)
    target_body: str = "body_01"
    target_reference: dict[str, Any] | None = None
    required: bool = True
    evidence: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _require_identifier(self.node_id, "feature.node_id"))
        object.__setattr__(self, "operation", _require_identifier(self.operation, "feature.operation", operation=True))
        object.__setattr__(self, "skill_key", _require_identifier(self.skill_key, "feature.skill_key", operation=True))
        object.__setattr__(self, "depends_on", _tuple_of_strings(self.depends_on))
        if self.target_reference is not None and not isinstance(self.target_reference, dict):
            raise RegistryValidationError("target_reference must be an object or null.")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FeatureTemplateNode":
        return cls(
            node_id=value.get("node_id", ""),
            operation=value.get("operation", ""),
            skill_key=value.get("skill_key", ""),
            depends_on=_tuple_of_strings(value.get("depends_on")),
            parameter_bindings=copy.deepcopy(value.get("parameter_bindings") or {}),
            target_body=str(value.get("target_body") or "body_01"),
            target_reference=copy.deepcopy(value.get("target_reference")),
            required=bool(value.get("required", True)),
            evidence=str(value.get("evidence") or ""),
        )


@dataclass(frozen=True)
class PartTypeDefinition:
    part_type: str
    category: str
    parameters: tuple[ParameterSpec, ...]
    feature_tree: tuple[FeatureTemplateNode, ...]
    aliases: tuple[str, ...] = ()
    finalizer_skills: tuple[str, ...] = ("save_sldprt",)
    source_kind: str = "builtin"
    source_id: str = "project"
    version: str = "1.0"
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "part_type", _require_identifier(self.part_type, "part_type", operation=True))
        if not str(self.category or "").strip():
            raise RegistryValidationError("part category is required.")
        if not self.feature_tree:
            raise RegistryValidationError(f"Part type {self.part_type!r} requires a feature tree.")
        if self.source_kind not in SOURCE_KINDS:
            raise RegistryValidationError(f"Unsupported source_kind: {self.source_kind!r}")
        object.__setattr__(self, "aliases", _tuple_of_strings(self.aliases))
        object.__setattr__(self, "finalizer_skills", _tuple_of_strings(self.finalizer_skills))
        object.__setattr__(self, "tags", _tuple_of_strings(self.tags))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartTypeDefinition":
        return cls(
            part_type=value.get("part_type", ""),
            category=str(value.get("category") or ""),
            parameters=tuple(ParameterSpec.from_dict(item) for item in value.get("parameters", [])),
            feature_tree=tuple(FeatureTemplateNode.from_dict(item) for item in value.get("feature_tree", [])),
            aliases=_tuple_of_strings(value.get("aliases")),
            finalizer_skills=_tuple_of_strings(value.get("finalizer_skills")),
            source_kind=value.get("source_kind", "builtin"),
            source_id=str(value.get("source_id") or "project"),
            version=str(value.get("version") or "1.0"),
            tags=_tuple_of_strings(value.get("tags")),
        )


@dataclass(frozen=True)
class ResolvedReference:
    reference_id: str
    document_id: str
    feature_id: str
    role: str
    entity_type: str
    body_id: str | None = None
    persistent_token: str | None = None
    geometry_signature: dict[str, Any] = field(default_factory=dict)
    source_skill: str = ""
    state: str = "semantic"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_id", _require_identifier(self.reference_id, "reference_id"))
        object.__setattr__(self, "document_id", _require_identifier(self.document_id, "document_id"))
        object.__setattr__(self, "feature_id", _require_identifier(self.feature_id, "feature_id"))
        object.__setattr__(self, "role", _require_identifier(self.role, "reference.role", operation=True))
        if self.entity_type not in ENTITY_TYPES:
            raise RegistryValidationError(f"Unsupported reference entity_type: {self.entity_type!r}")
        if self.state not in REFERENCE_STATES:
            raise RegistryValidationError(f"Unsupported reference state: {self.state!r}")
        if self.persistent_token is not None:
            token = str(self.persistent_token).strip()
            if not token:
                raise RegistryValidationError("persistent_token cannot be an empty string.")
            object.__setattr__(self, "persistent_token", token)
        if self.state == "resolved" and not self.persistent_token:
            raise RegistryValidationError(
                "A resolved reference must contain a persistent_token."
            )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResolvedReference":
        return cls(
            reference_id=value.get("reference_id", ""),
            document_id=value.get("document_id", ""),
            feature_id=value.get("feature_id", ""),
            role=value.get("role", ""),
            entity_type=value.get("entity_type", ""),
            body_id=value.get("body_id"),
            persistent_token=value.get("persistent_token"),
            geometry_signature=copy.deepcopy(value.get("geometry_signature") or {}),
            source_skill=str(value.get("source_skill") or ""),
            state=value.get("state", "semantic"),
            metadata=copy.deepcopy(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class FeatureTreeNode:
    feature_id: str
    feature_name: str
    operation: str
    skill_key: str
    order: int
    depends_on: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    status: str = "verified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", _require_identifier(self.feature_id, "feature_id"))
        object.__setattr__(self, "operation", _require_identifier(self.operation, "operation", operation=True))
        object.__setattr__(self, "skill_key", _require_identifier(self.skill_key, "skill_key", operation=True))
        if self.order < 0:
            raise RegistryValidationError("feature order must be non-negative.")
        object.__setattr__(self, "depends_on", _tuple_of_strings(self.depends_on))
        object.__setattr__(self, "consumes", _tuple_of_strings(self.consumes))
        object.__setattr__(self, "produces", _tuple_of_strings(self.produces))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FeatureTreeNode":
        return cls(
            feature_id=value.get("feature_id", ""),
            feature_name=str(value.get("feature_name") or value.get("feature_id") or ""),
            operation=value.get("operation", ""),
            skill_key=value.get("skill_key", ""),
            order=int(value.get("order", 0)),
            depends_on=_tuple_of_strings(value.get("depends_on")),
            parameters=copy.deepcopy(value.get("parameters") or {}),
            consumes=_tuple_of_strings(value.get("consumes")),
            produces=_tuple_of_strings(value.get("produces")),
            status=str(value.get("status") or "verified"),
        )


@dataclass(frozen=True)
class FeatureTreeRecord:
    document_id: str
    part_type: str
    nodes: tuple[FeatureTreeNode, ...]
    references: tuple[ResolvedReference, ...] = ()
    model_path: str = ""
    verification_report: str = ""
    verified: bool = False
    source_kind: str = "project"
    source_id: str = "project"
    revision: str = "1"
    executable_template: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_id", _require_identifier(self.document_id, "document_id"))
        object.__setattr__(self, "part_type", _require_identifier(self.part_type, "part_type", operation=True))
        if not self.nodes:
            raise RegistryValidationError("A feature tree record requires at least one node.")
        if self.source_kind not in SOURCE_KINDS:
            raise RegistryValidationError(f"Unsupported source_kind: {self.source_kind!r}")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FeatureTreeRecord":
        return cls(
            document_id=value.get("document_id", ""),
            part_type=value.get("part_type", ""),
            nodes=tuple(FeatureTreeNode.from_dict(item) for item in value.get("nodes", [])),
            references=tuple(ResolvedReference.from_dict(item) for item in value.get("references", [])),
            model_path=str(value.get("model_path") or ""),
            verification_report=str(value.get("verification_report") or ""),
            verified=bool(value.get("verified")),
            source_kind=value.get("source_kind", "project"),
            source_id=str(value.get("source_id") or "project"),
            revision=str(value.get("revision") or "1"),
            executable_template=bool(value.get("executable_template")),
            metadata=copy.deepcopy(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class CompositionResult:
    part_type: str
    features: tuple[dict[str, Any], ...]
    skill_pipeline: tuple[str, ...]
    unresolved: tuple[str, ...]
    source_id: str
    registry_schema_version: str = REGISTRY_SCHEMA_VERSION

    @property
    def executable(self) -> bool:
        return not self.unresolved

    def as_candidate_plan(self, source_prompt: str = "") -> dict[str, Any]:
        confidence = 1.0 if self.executable else 0.0
        return {
            "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
            "source_prompt": source_prompt,
            "part_type": self.part_type,
            "task_type": "model_3d",
            "parameters": {"unit": "mm"},
            "features": copy.deepcopy(list(self.features)),
            "outputs": ["SLDPRT"] if "save_sldprt" in self.skill_pipeline else [],
            "assumptions": [],
            "unresolved": list(self.unresolved),
            "confidence": confidence,
            "registry_source": self.source_id,
        }


@dataclass(frozen=True)
class RegistryCatalog:
    source_id: str
    source_kind: str
    skills: tuple[SkillRegistration, ...] = ()
    part_types: tuple[PartTypeDefinition, ...] = ()
    feature_trees: tuple[FeatureTreeRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise RegistryValidationError(f"Unsupported catalog source_kind: {self.source_kind!r}")


@runtime_checkable
class RegistryProvider(Protocol):
    def load_catalog(self) -> RegistryCatalog:
        ...


def as_dict(value: Any) -> dict[str, Any]:
    return _json_value(asdict(value))


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return copy.deepcopy(value)


def registry_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": REGISTRY_SCHEMA_VERSION,
        "title": "CAD Feature Reference Registry Snapshot",
        "type": "object",
        "required": ["schema_version", "skills", "part_types", "feature_trees"],
        "properties": {
            "schema_version": {"const": REGISTRY_SCHEMA_VERSION},
            "skills": {"type": "array", "items": {"$ref": "#/$defs/skill"}},
            "part_types": {"type": "array", "items": {"$ref": "#/$defs/part_type"}},
            "feature_trees": {"type": "array", "items": {"$ref": "#/$defs/feature_tree"}},
        },
        "additionalProperties": False,
        "$defs": {
            "parameter": {
                "type": "object",
                "required": ["name", "value_type", "required", "unit", "aliases", "description"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "value_type": {"enum": sorted(VALUE_TYPES)},
                    "required": {"type": "boolean"},
                    "unit": {"enum": [None, "mm", "deg", "ratio"]},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "reference": {
                "type": "object",
                "required": ["role", "entity_type", "required", "multiple", "description"],
                "properties": {
                    "role": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "entity_type": {"enum": sorted(ENTITY_TYPES)},
                    "required": {"type": "boolean"},
                    "multiple": {"type": "boolean"},
                    "description": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "operation": {
                "type": "object",
                "required": [
                    "operation",
                    "parameters",
                    "required_any",
                    "consumes",
                    "produces",
                    "context_requirements",
                    "conflict_rules",
                ],
                "properties": {
                    "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "parameters": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/parameter"},
                    },
                    "required_any": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                    "consumes": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/reference"},
                    },
                    "produces": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/reference"},
                    },
                    "context_requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "conflict_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
            "skill": {
                "type": "object",
                "required": [
                    "skill_key",
                    "operations",
                    "status",
                    "version",
                    "source_kind",
                    "source_id",
                    "capabilities",
                    "side_effects",
                    "output_types",
                    "modifies_active_doc",
                    "creates_new_doc",
                    "exports_files",
                    "uses_test_template",
                    "production_ready",
                    "tags",
                ],
                "properties": {
                    "skill_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"$ref": "#/$defs/operation"},
                    },
                    "status": {"enum": sorted(SKILL_STATUSES)},
                    "version": {"type": "string"},
                    "source_kind": {"enum": sorted(SOURCE_KINDS)},
                    "source_id": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "side_effects": {"type": "array", "items": {"type": "string"}},
                    "output_types": {"type": "array", "items": {"type": "string"}},
                    "modifies_active_doc": {"type": "boolean"},
                    "creates_new_doc": {"type": "boolean"},
                    "exports_files": {"type": "boolean"},
                    "uses_test_template": {"type": "boolean"},
                    "production_ready": {"type": "boolean"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            "feature_template_node": {
                "type": "object",
                "required": [
                    "node_id",
                    "operation",
                    "skill_key",
                    "depends_on",
                    "parameter_bindings",
                    "target_body",
                    "target_reference",
                    "required",
                    "evidence",
                ],
                "properties": {
                    "node_id": {"type": "string"},
                    "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "skill_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "parameter_bindings": {"type": "object"},
                    "target_body": {"type": "string"},
                    "target_reference": {"type": ["object", "null"]},
                    "required": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "part_type": {
                "type": "object",
                "required": [
                    "part_type",
                    "category",
                    "parameters",
                    "feature_tree",
                    "aliases",
                    "finalizer_skills",
                    "source_kind",
                    "source_id",
                    "version",
                    "tags",
                ],
                "properties": {
                    "part_type": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "category": {"type": "string"},
                    "parameters": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/parameter"},
                    },
                    "feature_tree": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"$ref": "#/$defs/feature_template_node"},
                    },
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "finalizer_skills": {"type": "array", "items": {"type": "string"}},
                    "source_kind": {"enum": sorted(SOURCE_KINDS)},
                    "source_id": {"type": "string"},
                    "version": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            "resolved_reference": {
                "type": "object",
                "required": [
                    "reference_id",
                    "document_id",
                    "feature_id",
                    "role",
                    "entity_type",
                    "body_id",
                    "persistent_token",
                    "geometry_signature",
                    "source_skill",
                    "state",
                    "metadata",
                ],
                "properties": {
                    "reference_id": {"type": "string"},
                    "document_id": {"type": "string"},
                    "feature_id": {"type": "string"},
                    "role": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "entity_type": {"enum": sorted(ENTITY_TYPES)},
                    "body_id": {"type": ["string", "null"]},
                    "persistent_token": {"type": ["string", "null"]},
                    "geometry_signature": {"type": "object"},
                    "source_skill": {"type": "string"},
                    "state": {"enum": sorted(REFERENCE_STATES)},
                    "metadata": {"type": "object"},
                },
                "additionalProperties": False,
            },
            "feature_tree_node": {
                "type": "object",
                "required": [
                    "feature_id",
                    "feature_name",
                    "operation",
                    "skill_key",
                    "order",
                    "depends_on",
                    "parameters",
                    "consumes",
                    "produces",
                    "status",
                ],
                "properties": {
                    "feature_id": {"type": "string"},
                    "feature_name": {"type": "string"},
                    "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "skill_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "order": {"type": "integer", "minimum": 0},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "parameters": {"type": "object"},
                    "consumes": {"type": "array", "items": {"type": "string"}},
                    "produces": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "feature_tree": {
                "type": "object",
                "required": [
                    "document_id",
                    "part_type",
                    "nodes",
                    "references",
                    "model_path",
                    "verification_report",
                    "verified",
                    "source_kind",
                    "source_id",
                    "revision",
                    "executable_template",
                    "metadata",
                ],
                "properties": {
                    "document_id": {"type": "string"},
                    "part_type": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "nodes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"$ref": "#/$defs/feature_tree_node"},
                    },
                    "references": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/resolved_reference"},
                    },
                    "model_path": {"type": "string"},
                    "verification_report": {"type": "string"},
                    "verified": {"type": "boolean"},
                    "source_kind": {"enum": sorted(SOURCE_KINDS)},
                    "source_id": {"type": "string"},
                    "revision": {"type": "string"},
                    "executable_template": {"type": "boolean"},
                    "metadata": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
    }
