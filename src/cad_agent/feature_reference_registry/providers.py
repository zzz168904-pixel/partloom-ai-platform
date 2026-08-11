from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    REGISTRY_SCHEMA_VERSION,
    FeatureTreeRecord,
    PartTypeDefinition,
    RegistryCatalog,
    RegistryValidationError,
    SkillRegistration,
)


@dataclass(frozen=True)
class StaticCatalogProvider:
    """Small adapter for plugin-owned or programmatically generated catalogs."""

    catalog: RegistryCatalog

    def load_catalog(self) -> RegistryCatalog:
        return self.catalog


@dataclass(frozen=True)
class JsonCatalogProvider:
    """Load a versioned external catalog without importing its implementation."""

    path: str | Path
    expected_source_kind: str | None = None

    def load_catalog(self) -> RegistryCatalog:
        source_path = Path(self.path)
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RegistryValidationError(f"Cannot read registry catalog: {source_path}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryValidationError(f"Registry catalog is not valid JSON: {source_path}") from exc
        if not isinstance(payload, dict):
            raise RegistryValidationError("Registry catalog must be a JSON object.")
        if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryValidationError(
                f"Expected catalog schema {REGISTRY_SCHEMA_VERSION}, "
                f"got {payload.get('schema_version')!r}."
            )

        source_id = str(payload.get("source_id") or source_path.stem)
        source_kind = str(payload.get("source_kind") or "")
        if self.expected_source_kind and source_kind != self.expected_source_kind:
            raise RegistryValidationError(
                f"Catalog source_kind must be {self.expected_source_kind!r}, got {source_kind!r}."
            )

        return RegistryCatalog(
            source_id=source_id,
            source_kind=source_kind,
            skills=tuple(
                SkillRegistration.from_dict(_owned(item, source_id, source_kind))
                for item in _items(payload, "skills")
            ),
            part_types=tuple(
                PartTypeDefinition.from_dict(_owned(item, source_id, source_kind))
                for item in _items(payload, "part_types")
            ),
            feature_trees=tuple(
                FeatureTreeRecord.from_dict(_owned(item, source_id, source_kind))
                for item in _items(payload, "feature_trees")
            ),
        )


class StandardPartLibraryProvider(JsonCatalogProvider):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path=path, expected_source_kind="standard_parts")


class TestModelLibraryProvider(JsonCatalogProvider):
    __test__ = False

    def __init__(self, path: str | Path) -> None:
        super().__init__(path=path, expected_source_kind="test_library")


def _items(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    values = payload.get(name, [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise RegistryValidationError(f"Catalog field {name!r} must be an array of objects.")
    return values


def _owned(value: dict[str, Any], source_id: str, source_kind: str) -> dict[str, Any]:
    # The top-level catalog identity is authoritative. A test library cannot
    # relabel individual records as production data.
    item = dict(value)
    item["source_id"] = source_id
    item["source_kind"] = source_kind
    return item
