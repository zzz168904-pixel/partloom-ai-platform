from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from .autocad_annotation_engine import AutoCADAnnotationEngine
from .autocad_skill import AutoCADSkill
from .models import SkillResult


class AutoCADAnnotationSkill:
    """Pipeline adapter for native AutoCAD annotation on an existing DWG."""

    key = "autocad_annotation"
    name = "AutoCAD Annotation Skill"

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir
        self.engine = AutoCADAnnotationEngine(output_root, skill_dir)
        self.autocad = AutoCADSkill(output_root, skill_dir)

    def run_plan(self, plan: dict[str, Any], source_dwg_path: str | Path | None = None) -> SkillResult:
        source = source_dwg_path or plan.get("source_dwg_path") or plan.get("source_dwg")
        if source:
            result = None
            errors: list[str] = []
            base_output_dir = plan.get("annotation_output_dir")
            for attempt in range(1, 4):
                output_dir = base_output_dir
                if base_output_dir and attempt > 1:
                    output_dir = str(Path(base_output_dir).with_name(f"{Path(base_output_dir).name}_retry{attempt}"))
                result = self.engine.annotate(source, output_dir=output_dir)
                if result.success:
                    break
                errors.extend(result.errors)
                time.sleep(1.5 * attempt)
            assert result is not None
            if errors and result.errors != errors:
                result.errors = [*errors, *result.errors]
            data = result.as_dict()
            data["source_mode"] = "solidworks_exported_dwg"
            return SkillResult(
                success=result.success,
                message=(
                    f"AutoCAD native annotation completed: {result.annotated_dwg}"
                    if result.success
                    else f"AutoCAD native annotation failed: {source}"
                ),
                data=data,
                path=result.annotated_dwg,
                output="\n".join(result.errors),
            )

        result = self.autocad.run_plan(plan)
        data = dict(result.data)
        data["annotation_scope"] = [
            "length",
            "width",
            "thickness_note",
            "hole_diameter",
            "threaded_hole_note",
            "fillet_note",
            "chamfer_note",
            "layers",
        ]
        data["source_mode"] = "generated_2d_fallback"
        return SkillResult(
            success=result.success,
            message=f"AutoCAD fallback annotation completed: {result.path}",
            data=data,
            path=result.path,
            output=result.output,
        )
