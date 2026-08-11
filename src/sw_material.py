from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MaterialInfo:
    material: str
    database: str


def get_material_information(doc: Any) -> MaterialInfo:
    try:
        config_name = ""
        config = doc.GetActiveConfiguration()
        if config is not None:
            name = config.Name
            config_name = name() if callable(name) else str(name)

        database = ""
        material = doc.GetMaterialPropertyName2(config_name, database)
        return MaterialInfo(material=str(material or "N/A"), database=str(database or "N/A"))
    except Exception as exc:
        raise RuntimeError("Failed to read material information.") from exc
