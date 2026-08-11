from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .models import (
    REGISTRY_SCHEMA_VERSION,
    CompositionResult,
    FeatureTreeRecord,
    PartTypeDefinition,
    RegistryCatalog,
    RegistryConflictError,
    RegistryNotFoundError,
    RegistryProvider,
    RegistryValidationError,
    ResolvedReference,
    SkillRegistration,
    as_dict,
    registry_schema,
)


def _lookup_key(value: str) -> str:
    return re.sub(r"[^\w]+", "_", str(value or "").strip().casefold()).strip("_")


class FeatureReferenceRegistry:
    """In-memory knowledge and lookup layer for CAD features and references.

    This registry does not execute Skills and does not hold live COM objects.
    SolidWorks persistent tokens are serialized through ResolvedReference;
    live entities are resolved by a separate backend and never retained here.
    """

    def __init__(self, *, allow_test_sources: bool = False) -> None:
        self.allow_test_sources = allow_test_sources
        self._skills: dict[str, SkillRegistration] = {}
        self._operation_index: dict[str, list[str]] = {}
        self._part_types: dict[str, PartTypeDefinition] = {}
        self._part_aliases: dict[str, str] = {}
        self._feature_trees: dict[str, FeatureTreeRecord] = {}
        self._references: dict[str, ResolvedReference] = {}

    @staticmethod
    def schema() -> dict[str, Any]:
        return registry_schema()

    def register_skill(self, skill: SkillRegistration, *, replace: bool = False) -> None:
        self._guard_source(skill.source_kind)
        if skill.status == "test_only" and skill.production_ready:
            raise RegistryValidationError("A test_only Skill cannot be production_ready.")
        if skill.skill_key in self._skills and not replace:
            raise RegistryConflictError(f"Skill is already registered: {skill.skill_key}")
        if replace and skill.skill_key in self._skills:
            self._remove_skill_indexes(self._skills[skill.skill_key])
        self._skills[skill.skill_key] = copy.deepcopy(skill)
        for operation in skill.operations:
            keys = self._operation_index.setdefault(operation.operation, [])
            if skill.skill_key not in keys:
                keys.append(skill.skill_key)
                keys.sort()

    def get_skill(self, skill_key: str) -> SkillRegistration:
        try:
            return copy.deepcopy(self._skills[skill_key])
        except KeyError as exc:
            raise RegistryNotFoundError(f"Unknown Skill: {skill_key}") from exc

    def find_skills(
        self,
        *,
        operation: str | None = None,
        capability: str | None = None,
        status: str | None = None,
        production_only: bool = True,
    ) -> list[SkillRegistration]:
        if operation:
            candidates = [self._skills[key] for key in self._operation_index.get(operation, [])]
        else:
            candidates = list(self._skills.values())
        result = []
        for skill in candidates:
            if production_only and (not skill.production_ready or skill.status != "production"):
                continue
            if capability and capability not in skill.capabilities:
                continue
            if status and skill.status != status:
                continue
            result.append(copy.deepcopy(skill))
        return sorted(result, key=lambda item: item.skill_key)

    def operation_contract(self, operation: str, *, production_only: bool = True) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for skill in self.find_skills(operation=operation, production_only=production_only):
            contract = skill.operation(operation)
            if contract is not None:
                result.append({"skill_key": skill.skill_key, "contract": as_dict(contract)})
        return result

    def register_part_type(self, part: PartTypeDefinition, *, replace: bool = False) -> None:
        self._guard_source(part.source_kind)
        if part.part_type in self._part_types and not replace:
            raise RegistryConflictError(f"Part type is already registered: {part.part_type}")
        self._validate_part_type(part)
        aliases = {_lookup_key(part.part_type), *(_lookup_key(value) for value in part.aliases)}
        aliases.discard("")
        for alias in aliases:
            existing = self._part_aliases.get(alias)
            if existing and existing != part.part_type and not replace:
                raise RegistryConflictError(f"Part alias {alias!r} is already assigned to {existing!r}.")
        if replace and part.part_type in self._part_types:
            self._remove_part_aliases(self._part_types[part.part_type])
        self._part_types[part.part_type] = copy.deepcopy(part)
        for alias in aliases:
            self._part_aliases[alias] = part.part_type

    def resolve_part_type(self, name_or_alias: str) -> PartTypeDefinition:
        canonical = self._part_aliases.get(_lookup_key(name_or_alias))
        if not canonical:
            raise RegistryNotFoundError(f"Unknown part type or alias: {name_or_alias}")
        return copy.deepcopy(self._part_types[canonical])

    def find_part_types(
        self,
        *,
        category: str | None = None,
        operation: str | None = None,
        source_kind: str | None = None,
    ) -> list[PartTypeDefinition]:
        result = []
        for part in self._part_types.values():
            if category and part.category != category:
                continue
            if source_kind and part.source_kind != source_kind:
                continue
            if operation and operation not in {node.operation for node in part.feature_tree}:
                continue
            result.append(copy.deepcopy(part))
        return sorted(result, key=lambda item: item.part_type)

    def compose(
        self,
        part_type: str,
        *,
        parameters: dict[str, Any] | None = None,
        requested_operations: Iterable[str] | None = None,
        include_finalizers: bool = True,
    ) -> CompositionResult:
        part = self.resolve_part_type(part_type)
        supplied = copy.deepcopy(parameters or {})
        requested = {str(value).strip() for value in (requested_operations or []) if str(value).strip()}
        node_by_id = {node.node_id: node for node in part.feature_tree}
        available = {node.operation for node in part.feature_tree} | {node.node_id for node in part.feature_tree}
        unknown = sorted(requested - available)
        if unknown:
            raise RegistryNotFoundError(
                f"Part type {part.part_type!r} does not define requested operations/nodes: {unknown}"
            )

        selected = {node.node_id for node in part.feature_tree if node.required}
        selected.update(
            node.node_id
            for node in part.feature_tree
            if node.operation in requested or node.node_id in requested
        )

        def include_dependencies(node_id: str) -> None:
            for dependency in node_by_id[node_id].depends_on:
                if dependency not in selected:
                    selected.add(dependency)
                    include_dependencies(dependency)

        for node_id in tuple(selected):
            include_dependencies(node_id)

        features: list[dict[str, Any]] = []
        unresolved: list[str] = []
        skill_pipeline: list[str] = []
        for node in part.feature_tree:
            if node.node_id not in selected:
                continue
            node_unresolved: list[str] = []
            resolved_parameters = self._resolve_bindings(
                node.parameter_bindings,
                supplied,
                node.node_id,
                node_unresolved,
            )
            unresolved.extend(node_unresolved)
            feature = {
                "id": node.node_id,
                "operation": node.operation,
                "depends_on": [value for value in node.depends_on if value in selected],
                "target_body": node.target_body,
                "target_reference": copy.deepcopy(node.target_reference),
                "parameters": resolved_parameters,
                "evidence": [node.evidence or f"feature_registry:{part.part_type}/{node.node_id}"],
                "assumptions": [],
                "unresolved": node_unresolved,
                "confidence": 1.0 if not node_unresolved else 0.0,
            }
            features.append(feature)
            if node.skill_key not in skill_pipeline:
                skill_pipeline.append(node.skill_key)
        if include_finalizers:
            for skill_key in part.finalizer_skills:
                if skill_key not in skill_pipeline:
                    skill_pipeline.append(skill_key)
        return CompositionResult(
            part_type=part.part_type,
            features=tuple(features),
            skill_pipeline=tuple(skill_pipeline),
            unresolved=tuple(dict.fromkeys(unresolved)),
            source_id=part.source_id,
        )

    def register_feature_tree(self, record: FeatureTreeRecord, *, replace: bool = False) -> None:
        self._guard_source(record.source_kind)
        if record.document_id in self._feature_trees and not replace:
            raise RegistryConflictError(f"Feature tree is already registered: {record.document_id}")
        self._validate_feature_tree(record)
        if replace and record.document_id in self._feature_trees:
            old = self._feature_trees[record.document_id]
            for reference in old.references:
                self._references.pop(reference.reference_id, None)
        for reference in record.references:
            if reference.reference_id in self._references:
                raise RegistryConflictError(f"Reference is already registered: {reference.reference_id}")
        self._feature_trees[record.document_id] = copy.deepcopy(record)
        for reference in record.references:
            self._references[reference.reference_id] = copy.deepcopy(reference)

    def get_feature_tree(self, document_id: str) -> FeatureTreeRecord:
        try:
            return copy.deepcopy(self._feature_trees[document_id])
        except KeyError as exc:
            raise RegistryNotFoundError(f"Unknown feature tree document: {document_id}") from exc

    def find_feature_trees(
        self,
        *,
        part_type: str | None = None,
        verified_only: bool = True,
        source_kind: str | None = None,
    ) -> list[FeatureTreeRecord]:
        canonical = self.resolve_part_type(part_type).part_type if part_type else None
        result = []
        for record in self._feature_trees.values():
            if canonical and record.part_type != canonical:
                continue
            if verified_only and not record.verified:
                continue
            if source_kind and record.source_kind != source_kind:
                continue
            result.append(copy.deepcopy(record))
        return sorted(result, key=lambda item: item.document_id)

    def get_reference(self, reference_id: str) -> ResolvedReference:
        try:
            return copy.deepcopy(self._references[reference_id])
        except KeyError as exc:
            raise RegistryNotFoundError(f"Unknown feature reference: {reference_id}") from exc

    def register_reference(self, reference: ResolvedReference, *, replace: bool = False) -> None:
        record = self._feature_trees.get(reference.document_id)
        if record is None:
            raise RegistryNotFoundError(
                f"Cannot register a reference for unknown document: {reference.document_id}"
            )
        if reference.feature_id not in {node.feature_id for node in record.nodes}:
            raise RegistryValidationError(
                f"Reference {reference.reference_id!r} targets unknown feature "
                f"{reference.feature_id!r} in document {reference.document_id!r}."
            )
        existing = self._references.get(reference.reference_id)
        if existing is not None and not replace:
            raise RegistryConflictError(f"Reference is already registered: {reference.reference_id}")
        if existing is not None and existing.document_id != reference.document_id:
            raise RegistryConflictError(
                f"Reference {reference.reference_id!r} is already owned by "
                f"document {existing.document_id!r}."
            )

        references = [
            item for item in record.references if item.reference_id != reference.reference_id
        ]
        references.append(copy.deepcopy(reference))
        updated = dataclasses.replace(record, references=tuple(references))
        self._validate_feature_tree(updated)
        self._feature_trees[record.document_id] = updated
        self._references[reference.reference_id] = copy.deepcopy(reference)

    def get_reference_for_role(
        self,
        *,
        document_id: str,
        feature_id: str,
        role: str,
        entity_type: str | None = None,
        require_persistent: bool = False,
    ) -> ResolvedReference:
        matches = self.find_references(
            document_id=document_id,
            feature_id=feature_id,
            role=role,
            entity_type=entity_type,
        )
        if not matches:
            raise RegistryNotFoundError(
                f"No reference matches document={document_id!r}, feature={feature_id!r}, "
                f"role={role!r}, entity_type={entity_type!r}."
            )
        if len(matches) != 1:
            raise RegistryConflictError(
                f"Reference role is ambiguous for document={document_id!r}, "
                f"feature={feature_id!r}, role={role!r}: "
                f"{[item.reference_id for item in matches]}"
            )
        reference = matches[0]
        if require_persistent and (
            reference.state != "resolved" or not reference.persistent_token
        ):
            raise RegistryNotFoundError(
                f"Reference {reference.reference_id!r} has no resolved persistent token."
            )
        return reference

    def find_references(
        self,
        *,
        document_id: str | None = None,
        feature_id: str | None = None,
        body_id: str | None = None,
        role: str | None = None,
        entity_type: str | None = None,
        state: str | None = None,
    ) -> list[ResolvedReference]:
        result = []
        for reference in self._references.values():
            if document_id and reference.document_id != document_id:
                continue
            if feature_id and reference.feature_id != feature_id:
                continue
            if body_id and reference.body_id != body_id:
                continue
            if role and reference.role != role:
                continue
            if entity_type and reference.entity_type != entity_type:
                continue
            if state and reference.state != state:
                continue
            result.append(copy.deepcopy(reference))
        return sorted(result, key=lambda item: item.reference_id)

    def register_provider(self, provider: RegistryProvider) -> dict[str, int]:
        catalog = provider.load_catalog()
        if not isinstance(catalog, RegistryCatalog):
            raise RegistryValidationError("RegistryProvider.load_catalog() must return RegistryCatalog.")
        self._guard_source(catalog.source_kind)
        backup = copy.deepcopy((
            self._skills,
            self._operation_index,
            self._part_types,
            self._part_aliases,
            self._feature_trees,
            self._references,
        ))
        try:
            for skill in catalog.skills:
                self.register_skill(skill)
            for part in catalog.part_types:
                self.register_part_type(part)
            for tree in catalog.feature_trees:
                self.register_feature_tree(tree)
        except Exception:
            (
                self._skills,
                self._operation_index,
                self._part_types,
                self._part_aliases,
                self._feature_trees,
                self._references,
            ) = backup
            raise
        return {
            "skills": len(catalog.skills),
            "part_types": len(catalog.part_types),
            "feature_trees": len(catalog.feature_trees),
        }

    def coverage_report(
        self,
        *,
        expected_skill_keys: Iterable[str] = (),
        expected_operations: Iterable[str] = (),
    ) -> dict[str, Any]:
        production_operations = {
            operation.operation
            for skill in self.find_skills(production_only=True)
            for operation in skill.operations
        }
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "skill_count": len(self._skills),
            "production_skill_count": len(self.find_skills(production_only=True)),
            "part_type_count": len(self._part_types),
            "verified_feature_tree_count": len(self.find_feature_trees(verified_only=True)),
            "missing_skill_keys": sorted(set(expected_skill_keys) - set(self._skills)),
            "missing_production_operations": sorted(set(expected_operations) - production_operations),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "skills": [as_dict(self._skills[key]) for key in sorted(self._skills)],
            "part_types": [as_dict(self._part_types[key]) for key in sorted(self._part_types)],
            "feature_trees": [as_dict(self._feature_trees[key]) for key in sorted(self._feature_trees)],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @classmethod
    def from_snapshot(
        cls,
        value: dict[str, Any],
        *,
        allow_test_sources: bool = False,
    ) -> "FeatureReferenceRegistry":
        if value.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryValidationError(
                f"Expected registry schema {REGISTRY_SCHEMA_VERSION}, got {value.get('schema_version')!r}."
            )
        registry = cls(allow_test_sources=allow_test_sources)
        for item in value.get("skills", []):
            registry.register_skill(SkillRegistration.from_dict(item))
        for item in value.get("part_types", []):
            registry.register_part_type(PartTypeDefinition.from_dict(item))
        for item in value.get("feature_trees", []):
            registry.register_feature_tree(FeatureTreeRecord.from_dict(item))
        return registry

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        allow_test_sources: bool = False,
    ) -> "FeatureReferenceRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RegistryValidationError("Registry snapshot must be a JSON object.")
        return cls.from_snapshot(payload, allow_test_sources=allow_test_sources)

    def _validate_part_type(self, part: PartTypeDefinition) -> None:
        parameter_names = {item.name for item in part.parameters}
        if len(parameter_names) != len(part.parameters):
            raise RegistryValidationError(f"Part type {part.part_type!r} contains duplicate parameters.")
        node_positions = {node.node_id: index for index, node in enumerate(part.feature_tree)}
        if len(node_positions) != len(part.feature_tree):
            raise RegistryValidationError(f"Part type {part.part_type!r} contains duplicate node IDs.")
        for index, node in enumerate(part.feature_tree):
            skill = self.get_skill(node.skill_key)
            if skill.operation(node.operation) is None:
                raise RegistryValidationError(
                    f"Feature node {node.node_id!r} uses operation {node.operation!r}, "
                    f"which is not registered by Skill {node.skill_key!r}."
                )
            for dependency in node.depends_on:
                if dependency not in node_positions:
                    raise RegistryValidationError(
                        f"Feature node {node.node_id!r} depends on unknown node {dependency!r}."
                    )
                if node_positions[dependency] >= index:
                    raise RegistryValidationError(
                        f"Feature node {node.node_id!r} dependency {dependency!r} must precede it."
                    )
            bound_names = self._binding_parameter_names(node.parameter_bindings)
            unknown_bindings = sorted(bound_names - parameter_names)
            if unknown_bindings:
                raise RegistryValidationError(
                    f"Feature node {node.node_id!r} references unknown part parameters: {unknown_bindings}"
                )
        for skill_key in part.finalizer_skills:
            self.get_skill(skill_key)

    def _validate_feature_tree(self, record: FeatureTreeRecord) -> None:
        self.resolve_part_type(record.part_type)
        ordered = sorted(record.nodes, key=lambda item: item.order)
        if list(record.nodes) != ordered:
            raise RegistryValidationError("Feature tree nodes must be stored in ascending order.")
        node_positions = {node.feature_id: index for index, node in enumerate(record.nodes)}
        if len(node_positions) != len(record.nodes):
            raise RegistryValidationError("Feature tree contains duplicate feature IDs.")
        reference_ids = {reference.reference_id for reference in record.references}
        if len(reference_ids) != len(record.references):
            raise RegistryValidationError("Feature tree contains duplicate reference IDs.")
        for index, node in enumerate(record.nodes):
            skill = self.get_skill(node.skill_key)
            if skill.operation(node.operation) is None:
                raise RegistryValidationError(
                    f"Feature {node.feature_id!r} operation is not provided by Skill {node.skill_key!r}."
                )
            for dependency in node.depends_on:
                if dependency not in node_positions or node_positions[dependency] >= index:
                    raise RegistryValidationError(
                        f"Feature {node.feature_id!r} has an invalid dependency: {dependency!r}."
                    )
            missing_consumed = sorted(set(node.consumes) - reference_ids)
            missing_produced = sorted(set(node.produces) - reference_ids)
            if missing_consumed or missing_produced:
                raise RegistryValidationError(
                    f"Feature {node.feature_id!r} references undeclared IDs: "
                    f"consumes={missing_consumed}, produces={missing_produced}."
                )
        for reference in record.references:
            if reference.document_id != record.document_id:
                raise RegistryValidationError(
                    f"Reference {reference.reference_id!r} belongs to another document."
                )
            if reference.feature_id not in node_positions:
                raise RegistryValidationError(
                    f"Reference {reference.reference_id!r} targets unknown feature {reference.feature_id!r}."
                )

    def _guard_source(self, source_kind: str) -> None:
        if source_kind == "test_library" and not self.allow_test_sources:
            raise RegistryValidationError("test_library sources are disabled in the production registry.")

    def _remove_skill_indexes(self, skill: SkillRegistration) -> None:
        for operation in skill.operations:
            keys = self._operation_index.get(operation.operation, [])
            if skill.skill_key in keys:
                keys.remove(skill.skill_key)
            if not keys:
                self._operation_index.pop(operation.operation, None)

    def _remove_part_aliases(self, part: PartTypeDefinition) -> None:
        for value in (part.part_type, *part.aliases):
            self._part_aliases.pop(_lookup_key(value), None)

    @classmethod
    def _resolve_bindings(
        cls,
        value: Any,
        parameters: dict[str, Any],
        node_id: str,
        unresolved: list[str],
        path: str = "parameters",
    ) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            name = value[1:]
            if name not in parameters or parameters[name] in (None, ""):
                unresolved.append(f"{node_id}.{path}: missing part parameter {name}")
                return None
            return copy.deepcopy(parameters[name])
        if isinstance(value, dict):
            return {
                key: cls._resolve_bindings(item, parameters, node_id, unresolved, f"{path}.{key}")
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._resolve_bindings(item, parameters, node_id, unresolved, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return copy.deepcopy(value)

    @classmethod
    def _binding_parameter_names(cls, value: Any) -> set[str]:
        if isinstance(value, str) and value.startswith("$"):
            return {value[1:]}
        if isinstance(value, dict):
            return set().union(*(cls._binding_parameter_names(item) for item in value.values())) if value else set()
        if isinstance(value, (list, tuple)):
            return set().union(*(cls._binding_parameter_names(item) for item in value)) if value else set()
        return set()
