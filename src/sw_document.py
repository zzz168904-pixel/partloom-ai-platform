from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DOC_TYPES = {
    1: "Part",
    2: "Assembly",
    3: "Drawing",
}


@dataclass(frozen=True)
class DocumentInfo:
    file_name: str
    document_type: str
    full_path: str
    configuration: str


def read_com_value(default: str, value: Any) -> str:
    try:
        if callable(value):
            value = value()
        return default if value in (None, "") else str(value)
    except Exception:
        return default


def get_active_document(sw_app: Any) -> Any | None:
    try:
        return sw_app.ActiveDoc
    except Exception as exc:
        raise RuntimeError("Failed to get active SolidWorks document.") from exc


def get_configuration_name(doc: Any) -> str:
    try:
        config = doc.GetActiveConfiguration()
        if config is None:
            return "N/A"
        return read_com_value("N/A", config.Name)
    except Exception:
        return "N/A"


def get_document_info(doc: Any) -> DocumentInfo:
    try:
        full_path = read_com_value("Unsaved document", doc.GetPathName)
        title = read_com_value("Untitled", doc.GetTitle)
        file_name = Path(full_path).name if full_path != "Unsaved document" else title
        type_id = int(read_com_value("0", doc.GetType))
        document_type = DOC_TYPES.get(type_id, f"Unknown ({type_id})")

        return DocumentInfo(
            file_name=file_name,
            document_type=document_type,
            full_path=full_path,
            configuration=get_configuration_name(doc),
        )
    except Exception as exc:
        raise RuntimeError("Failed to read document information.") from exc
