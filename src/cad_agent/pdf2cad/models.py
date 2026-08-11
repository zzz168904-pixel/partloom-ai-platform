from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PdfDrawingIR:
    source_type: str = "pdf_engineering_drawing"
    geometry: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    title_block: dict[str, Any] = field(default_factory=dict)
    views: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_text: str = ""
    parser: str = "local-pdf-text"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PDF2CADResult:
    success: bool
    message: str
    run_dir: str
    report_path: str
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
