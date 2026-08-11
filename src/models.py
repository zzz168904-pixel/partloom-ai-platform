from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_TASKS = {"create_drawing"}
SUPPORTED_VIEWS = {"Front", "Top", "Right", "Isometric"}


@dataclass(frozen=True)
class DrawingRequest:
    task: str
    model_path: Path
    drawing_template: Path
    output_path: Path
    views: list[str]
    scale: str = "auto"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrawingRequest":
        required = ["task", "modelPath", "drawingTemplate", "outputPath", "views"]
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"Missing required JSON field(s): {', '.join(missing)}")

        task = str(data["task"])
        if task not in SUPPORTED_TASKS:
            raise ValueError(f"Unsupported task: {task}")

        views_raw = data["views"]
        if not isinstance(views_raw, list) or not views_raw:
            raise ValueError("Field 'views' must be a non-empty list.")

        views = [str(view) for view in views_raw]
        unsupported = [view for view in views if view not in SUPPORTED_VIEWS]
        if unsupported:
            raise ValueError(f"Unsupported view(s): {', '.join(unsupported)}")

        return cls(
            task=task,
            model_path=Path(str(data["modelPath"])),
            drawing_template=Path(str(data["drawingTemplate"])),
            output_path=Path(str(data["outputPath"])),
            views=views,
            scale=str(data.get("scale", "auto")),
        )

    def validate_files(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model file does not exist: {self.model_path}")
        if self.model_path.suffix.lower() not in {".sldprt", ".sldasm"}:
            raise ValueError("modelPath must point to a .SLDPRT or .SLDASM file.")
        if not self.drawing_template.exists():
            raise FileNotFoundError(
                f"Drawing template does not exist: {self.drawing_template}"
            )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
