from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.cad_ir import CADIRCompiler
from cad_agent.feature_reference_registry import (
    REGISTRY_SCHEMA_VERSION,
    FeatureReferenceRegistry,
    FeatureTemplateNode,
    JsonCatalogProvider,
    ParameterSpec,
    PartTypeDefinition,
    RegistryCatalog,
    RegistryConflictError,
    RegistryNotFoundError,
    RegistryValidationError,
    SkillOperationSpec,
    SkillRegistration,
    StandardPartLibraryProvider,
    StaticCatalogProvider,
    TestModelLibraryProvider,
    build_default_feature_registry,
)
from cad_agent.registry import CADAgentSkillManager
from cad_agent.skill_planner import SKILL_RULES


def _skill(
    key: str,
    operation: str,
    *,
    source_kind: str = "project",
    source_id: str = "test",
) -> SkillRegistration:
    return SkillRegistration(
        skill_key=key,
        operations=(SkillOperationSpec(operation=operation),),
        source_kind=source_kind,
        source_id=source_id,
    )


def _part(
    name: str,
    skill_key: str,
    operation: str,
    *,
    aliases: tuple[str, ...] = (),
    source_kind: str = "project",
    source_id: str = "test",
) -> PartTypeDefinition:
    return PartTypeDefinition(
        part_type=name,
        category="test_part",
        parameters=(),
        feature_tree=(
            FeatureTemplateNode(
                node_id="feature_01",
                operation=operation,
                skill_key=skill_key,
            ),
        ),
        aliases=aliases,
        finalizer_skills=(),
        source_kind=source_kind,
        source_id=source_id,
    )


@pytest.fixture()
def registry() -> FeatureReferenceRegistry:
    return build_default_feature_registry(ROOT)


def test_schema_is_versioned_and_covers_all_registry_sections() -> None:
    schema = FeatureReferenceRegistry.schema()

    assert schema["$id"] == REGISTRY_SCHEMA_VERSION
    assert schema["required"] == ["schema_version", "skills", "part_types", "feature_trees"]
    assert {
        "skill",
        "operation",
        "part_type",
        "feature_template_node",
        "feature_tree",
        "feature_tree_node",
        "resolved_reference",
        "parameter",
        "reference",
    } <= set(schema["$defs"])
    assert schema["$defs"]["skill"]["properties"]["operations"]["items"] == {
        "$ref": "#/$defs/operation"
    }
    assert schema["$defs"]["feature_tree"]["properties"]["references"]["items"] == {
        "$ref": "#/$defs/resolved_reference"
    }


def test_default_registry_contains_every_current_skill_contract(registry: FeatureReferenceRegistry) -> None:
    expected = {rule.skill_key for rule in SKILL_RULES}
    expected.update(CADAgentSkillManager.production_skill_contracts())
    expected.add("solidworks_threaded_holes")
    report = registry.coverage_report(expected_skill_keys=expected)

    assert report["missing_skill_keys"] == []
    assert report["skill_count"] >= len(expected)


def test_every_cad_ir_operation_has_a_production_registration(registry: FeatureReferenceRegistry) -> None:
    report = registry.coverage_report(expected_operations=CADIRCompiler.STANDARD_OPERATIONS)

    assert report["missing_production_operations"] == []
    for operation in CADIRCompiler.STANDARD_OPERATIONS:
        assert registry.operation_contract(operation)


def test_each_production_operation_declares_inputs_or_context(registry: FeatureReferenceRegistry) -> None:
    for skill in registry.find_skills():
        assert skill.operations
        for operation in skill.operations:
            assert operation.parameters or operation.consumes or operation.context_requirements


def test_base_plate_contract_registers_inputs_and_reference_outputs(
    registry: FeatureReferenceRegistry,
) -> None:
    skill = registry.get_skill("base_plate")
    operation = skill.operation("base_plate")

    assert operation is not None
    assert {item.name for item in operation.parameters if item.required} == {
        "length_mm",
        "width_mm",
        "thickness_mm",
    }
    assert {item.role for item in operation.produces} >= {
        "primary_solid",
        "base_feature",
        "outer_planar_face",
    }
    assert skill.output_types == ("SLDPRT",)
    assert skill.creates_new_doc
    assert not skill.modifies_active_doc
    assert not skill.exports_files
    assert not skill.uses_test_template


def test_skill_side_effect_flags_match_runtime_scope_contracts(
    registry: FeatureReferenceRegistry,
) -> None:
    runtime = CADAgentSkillManager.production_skill_contracts()

    for skill_key, contract in runtime.items():
        skill = registry.get_skill(skill_key)
        assert skill.modifies_active_doc is bool(contract.get("modifies_active_doc"))
        assert skill.creates_new_doc is bool(contract.get("creates_new_doc"))
        assert skill.exports_files is bool(contract.get("exports_files"))
        assert skill.uses_test_template is bool(contract.get("uses_test_template"))


def test_legacy_cnc_test_adapter_is_not_a_production_candidate(
    registry: FeatureReferenceRegistry,
) -> None:
    test_skill = registry.get_skill("solidworks_cnc_fillet")

    assert test_skill.status == "test_only"
    assert not test_skill.production_ready
    assert "solidworks_cnc_fillet" not in {
        item.skill_key for item in registry.find_skills(operation="fillet")
    }


def test_part_type_alias_resolves_to_canonical_definition(registry: FeatureReferenceRegistry) -> None:
    part = registry.resolve_part_type("mounting plate")

    assert part.part_type == "rectangular_mounting_plate"


def test_compose_selects_only_requested_optional_features_and_dependencies(
    registry: FeatureReferenceRegistry,
) -> None:
    result = registry.compose(
        "mounting_plate",
        parameters={
            "length_mm": 100,
            "width_mm": 60,
            "thickness_mm": 10,
            "center_hole_diameter_mm": 20,
        },
        requested_operations=["through_hole"],
    )

    assert result.executable
    assert [item["operation"] for item in result.features] == ["base_plate", "through_hole"]
    assert result.features[1]["depends_on"] == ["base_01"]
    assert result.features[1]["parameters"] == {"diameter_mm": 20, "position": "center"}
    assert result.skill_pipeline == ("base_plate", "through_hole", "save_sldprt")
    assert "fillet" not in result.skill_pipeline


def test_compose_marks_missing_requested_parameters_unresolved(
    registry: FeatureReferenceRegistry,
) -> None:
    result = registry.compose(
        "rectangular_plate",
        parameters={"length_mm": 100, "width_mm": 60, "thickness_mm": 10},
        requested_operations=["through_hole"],
    )

    assert not result.executable
    assert result.features[1]["parameters"]["diameter_mm"] is None
    assert result.unresolved == (
        "center_hole_01.parameters.diameter_mm: missing part parameter center_hole_diameter_mm",
    )


def test_compose_rejects_unknown_operations(registry: FeatureReferenceRegistry) -> None:
    with pytest.raises(RegistryNotFoundError, match="does not define requested"):
        registry.compose(
            "rectangular_plate",
            parameters={"length_mm": 100, "width_mm": 60, "thickness_mm": 10},
            requested_operations=["loft"],
        )


def test_part_dependency_must_exist_and_precede_consumer() -> None:
    target = FeatureReferenceRegistry()
    target.register_skill(_skill("base_skill", "base_plate"))
    bad = PartTypeDefinition(
        part_type="bad_dependency_part",
        category="test",
        parameters=(),
        feature_tree=(
            FeatureTemplateNode(
                node_id="first",
                operation="base_plate",
                skill_key="base_skill",
                depends_on=("later",),
            ),
            FeatureTemplateNode(
                node_id="later",
                operation="base_plate",
                skill_key="base_skill",
            ),
        ),
        finalizer_skills=(),
    )

    with pytest.raises(RegistryValidationError, match="must precede"):
        target.register_part_type(bad)


def test_part_parameter_bindings_must_reference_declared_parameters() -> None:
    target = FeatureReferenceRegistry()
    target.register_skill(_skill("base_skill", "base_plate"))
    bad = PartTypeDefinition(
        part_type="bad_binding_part",
        category="test",
        parameters=(ParameterSpec("length_mm", "number", True, "mm"),),
        feature_tree=(
            FeatureTemplateNode(
                node_id="base",
                operation="base_plate",
                skill_key="base_skill",
                parameter_bindings={"width_mm": "$missing_width_mm"},
            ),
        ),
        finalizer_skills=(),
    )

    with pytest.raises(RegistryValidationError, match="unknown part parameters"):
        target.register_part_type(bad)


def test_part_alias_conflicts_are_blocked() -> None:
    target = FeatureReferenceRegistry()
    target.register_skill(_skill("base_skill", "base_plate"))
    target.register_part_type(_part("part_a", "base_skill", "base_plate", aliases=("shared",)))

    with pytest.raises(RegistryConflictError, match="already assigned"):
        target.register_part_type(_part("part_b", "base_skill", "base_plate", aliases=("shared",)))


def test_verified_real_model_tree_and_semantic_references_are_queryable(
    registry: FeatureReferenceRegistry,
) -> None:
    trees = registry.find_feature_trees(part_type="mounting_plate")

    if not trees:
        pytest.skip("Clean public checkout does not bundle a verified proprietary CAD model.")

    assert len(trees) == 1
    tree = trees[0]
    assert tree.verified
    assert not tree.executable_template
    assert Path(tree.model_path).is_file()
    assert [(node.feature_id, node.operation) for node in tree.nodes] == [
        ("feat_01", "base_plate"),
        ("feat_02", "through_hole"),
    ]
    assert tree.nodes[1].depends_on == ("feat_01",)


def test_references_support_role_body_and_feature_queries(registry: FeatureReferenceRegistry) -> None:
    if not registry.find_feature_trees(part_type="mounting_plate"):
        pytest.skip("Clean public checkout does not bundle a verified proprietary CAD model.")

    top_faces = registry.find_references(
        feature_id="feat_01",
        body_id="body_01",
        role="outer_planar_face",
        entity_type="Face",
    )

    assert len(top_faces) == 1
    assert top_faces[0].geometry_signature["normal"] == [0, 0, 1]
    assert registry.find_references(feature_id="feat_02", role="hole_axis")[0].entity_type == "Axis"


def test_snapshot_round_trip_preserves_catalog_and_queries(
    registry: FeatureReferenceRegistry,
    tmp_path: Path,
) -> None:
    if not registry.find_feature_trees(part_type="mounting_plate"):
        pytest.skip("Clean public checkout does not bundle a verified proprietary CAD model.")

    path = registry.save(tmp_path / "feature_registry.json")
    restored = FeatureReferenceRegistry.load(path)

    assert restored.snapshot() == registry.snapshot()
    assert restored.resolve_part_type("base plate").part_type == "rectangular_mounting_plate"
    assert restored.find_references(role="cylindrical_face")[0].geometry_signature["diameter_mm"] == 20.0


def test_snapshot_is_already_json_native_before_writing(registry: FeatureReferenceRegistry) -> None:
    snapshot = registry.snapshot()

    assert json.loads(json.dumps(snapshot)) == snapshot


def test_production_registry_rejects_test_library_provider() -> None:
    catalog = RegistryCatalog(
        source_id="fixtures",
        source_kind="test_library",
        skills=(
            _skill(
                "fixture_skill",
                "base_plate",
                source_kind="test_library",
                source_id="fixtures",
            ),
        ),
    )

    with pytest.raises(RegistryValidationError, match="test_library sources are disabled"):
        FeatureReferenceRegistry().register_provider(StaticCatalogProvider(catalog))


def test_test_library_can_only_be_loaded_when_explicitly_enabled() -> None:
    catalog = RegistryCatalog(
        source_id="fixtures",
        source_kind="test_library",
        skills=(
            _skill(
                "fixture_skill",
                "base_plate",
                source_kind="test_library",
                source_id="fixtures",
            ),
        ),
    )
    target = FeatureReferenceRegistry(allow_test_sources=True)

    result = target.register_provider(StaticCatalogProvider(catalog))

    assert result == {"skills": 1, "part_types": 0, "feature_trees": 0}
    assert target.get_skill("fixture_skill").source_kind == "test_library"


def test_provider_registration_rolls_back_atomically_on_invalid_part() -> None:
    target = FeatureReferenceRegistry()
    catalog = RegistryCatalog(
        source_id="broken_plugin",
        source_kind="plugin",
        skills=(
            _skill("temporary_plugin_skill", "base_plate", source_kind="plugin"),
        ),
        part_types=(
            _part("broken_plugin_part", "missing_skill", "base_plate", source_kind="plugin"),
        ),
    )

    with pytest.raises(RegistryNotFoundError, match="Unknown Skill"):
        target.register_provider(StaticCatalogProvider(catalog))
    with pytest.raises(RegistryNotFoundError):
        target.get_skill("temporary_plugin_skill")


def test_json_test_provider_cannot_relabel_fixture_items_as_project_data(tmp_path: Path) -> None:
    path = tmp_path / "fixtures.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "source_id": "fixture_catalog",
                "source_kind": "test_library",
                "skills": [
                    {
                        "skill_key": "fixture_skill",
                        "operations": [{"operation": "base_plate"}],
                        "source_kind": "project",
                        "production_ready": True,
                    }
                ],
                "part_types": [],
                "feature_trees": [],
            }
        ),
        encoding="utf-8",
    )
    provider = TestModelLibraryProvider(path)

    assert provider.load_catalog().skills[0].source_kind == "test_library"
    with pytest.raises(RegistryValidationError, match="test_library sources are disabled"):
        FeatureReferenceRegistry().register_provider(provider)


def test_standard_part_provider_loads_external_part_definition(tmp_path: Path) -> None:
    path = tmp_path / "standard_parts.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "source_id": "iso_parts",
                "source_kind": "standard_parts",
                "skills": [],
                "part_types": [
                    {
                        "part_type": "iso_test_plate",
                        "category": "standard_part",
                        "parameters": [],
                        "feature_tree": [
                            {
                                "node_id": "base",
                                "operation": "base_plate",
                                "skill_key": "base_plate",
                            }
                        ],
                        "finalizer_skills": [],
                    }
                ],
                "feature_trees": [],
            }
        ),
        encoding="utf-8",
    )
    target = build_default_feature_registry(ROOT, include_verified_models=False)

    result = target.register_provider(StandardPartLibraryProvider(path))

    assert result["part_types"] == 1
    assert target.resolve_part_type("iso_test_plate").source_kind == "standard_parts"


def test_json_provider_rejects_wrong_schema_and_wrong_source_kind(tmp_path: Path) -> None:
    wrong_schema = tmp_path / "wrong_schema.json"
    wrong_schema.write_text(json.dumps({"schema_version": "old"}), encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="Expected catalog schema"):
        JsonCatalogProvider(wrong_schema).load_catalog()

    wrong_source = tmp_path / "wrong_source.json"
    wrong_source.write_text(
        json.dumps(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "source_id": "wrong",
                "source_kind": "project",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegistryValidationError, match="source_kind must be"):
        StandardPartLibraryProvider(wrong_source).load_catalog()
