from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import SkillResult


class SolidWorksAutomationSkill:
    """Execute VibeCAD JSON plans through SolidWorks automation helpers.

    This adapter deliberately does not parse natural language. It accepts a
    normalized design plan and performs deterministic modeling steps.
    """

    key = "solidworks_automation"
    name = "SolidWorks Automation"

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.skill_script_dir = self.skill_dir / "scripts"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        return self._run_plan(plan)

    def run_base_plate_plan(self, plan: dict[str, Any], run_dir: Path) -> SkillResult:
        """Production entry: create only the requested base Part and metadata."""
        base_plan = dict(plan)
        base_plan["features"] = []
        return self._run_plan(base_plan, run_dir=run_dir)

    def save_active_part(self, run_dir: Path, target_path: str | None = None) -> SkillResult:
        """Production entry: save the current active Part without creating geometry."""
        try:
            self._ensure_skill_imports()
            self._ensure_non_interactive_com_dependencies()
            from sw_connect import connect_solidworks, get_com_member, save_document
        except Exception as exc:
            message = self._friendly_dependency_error(exc)
            return SkillResult(False, message or repr(exc), data={"error": repr(exc)})
        try:
            _sw, model = connect_solidworks(visible=True)
            if model is None or int(get_com_member(model, "GetType")) != 1:
                return SkillResult(False, "ActiveDoc is not a Part; SLDPRT was not saved.")
            rebuild_succeeded = bool(get_com_member(model, "ForceRebuild3", False))
            if not rebuild_succeeded:
                return SkillResult(False, "Active Part rebuild failed; SLDPRT was not saved.")

            current_path = str(get_com_member(model, "GetPathName") or "")
            target = Path(target_path) if target_path else (Path(current_path) if current_path else None)
            if target is None:
                target = run_dir / "production_part.SLDPRT"
            target = target.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)

            same_document = bool(current_path and Path(current_path).resolve() == target)
            saved = bool(save_document(model, None if same_document else str(target)))
            if not saved or not target.is_file() or target.stat().st_size <= 0:
                return SkillResult(False, f"Failed to save active Part: {target}")

            from .revolve_skill import RevolveSkill

            reopen_validation = RevolveSkill._verify_reopen(_sw, model, target)
            reopened_model = reopen_validation.pop("model", None)
            if not reopen_validation.get("success") or reopened_model is None:
                return SkillResult(
                    False,
                    f"Saved Part failed close/reopen geometry validation: {target}",
                    data={
                        "files": [str(target)],
                        "saved_path": str(target),
                        "reopen_validation": reopen_validation,
                    },
                    path=str(target),
                )
            return SkillResult(
                True,
                f"Active Part saved and reopened: {target}",
                data={
                    "files": [str(target)],
                    "saved_path": str(target),
                    "rebuild_succeeded": rebuild_succeeded,
                    "reopen_validation": reopen_validation,
                },
                path=str(target),
            )
        except Exception as exc:
            return SkillResult(False, f"save_sldprt failed: {exc}", data={"error": repr(exc)})

    def _run_plan(self, plan: dict[str, Any], run_dir: Path | None = None) -> SkillResult:
        try:
            self._ensure_skill_imports()
            self._ensure_non_interactive_com_dependencies()
            from sw_connect import mm
            from sw_part import extrude_boss, extrude_cut, sketch, sketch_circle, sketch_rectangle
            from sw_session import SolidWorksSession
        except Exception as exc:
            message = self._friendly_dependency_error(exc)
            if message:
                return SkillResult(False, message, output=message, data={"error": repr(exc)})
            raise

        params = plan.get("parameters", {})
        length = float(params.get("length", 100.0))
        width = float(params.get("width", 50.0))
        thickness = float(params.get("thickness", 10.0))

        run_dir = run_dir or (self.output_root / f"solidworks_automation_{datetime.now():%Y%m%d_%H%M%S}")
        run_dir.mkdir(parents=True, exist_ok=True)
        basename = self._safe_name(plan.get("part_family") or "vibecad_part")
        part_path = run_dir / f"{basename}.SLDPRT"
        parameter_path = run_dir / "executed_plan.json"

        session = SolidWorksSession(visible=True, wait_seconds=8)
        try:
            session.close(title=part_path.name)
        except Exception:
            pass
        model = session.new_part()

        with sketch(model, "Front Plane") as base_sketch:
            sketch_rectangle(model, 0.0, 0.0, mm(length), mm(width))
        base = extrude_boss(model, base_sketch, mm(thickness))
        if base is None:
            raise RuntimeError("SolidWorks failed to create base extrusion.")
        try:
            base.Name = "BasePlate"
        except Exception:
            pass

        material = str(params.get("material") or "")
        material_metadata = self._set_material_metadata(model, material)

        created_features: list[dict[str, Any]] = [
            {"name": "BasePlate", "type": "base_plate", "length": length, "width": width, "thickness": thickness}
        ]

        for feature in plan.get("features", []):
            feature_type = feature.get("type")
            feature_params = feature.get("params", {})
            if feature_type == "through_hole":
                diameter = float(feature_params.get("diameter", 0) or 0)
                if diameter <= 0:
                    continue
                x, y = self._feature_xy(feature_params, length, width)
                with sketch(model, "Front Plane") as hole_sketch:
                    sketch_circle(model, mm(x), mm(y), mm(diameter / 2.0))
                cut = extrude_cut(model, hole_sketch, mm(thickness + 2.0))
                if cut is None:
                    raise RuntimeError(f"SolidWorks failed to cut hole diameter {diameter} mm.")
                created_features.append(
                    {"name": feature.get("name", "Hole"), "type": "through_hole", "diameter": diameter, "x": x, "y": y}
                )

        model.ForceRebuild3(False)
        model.ViewZoomtofit2()
        if not session.save(model, str(part_path)):
            raise RuntimeError(f"SolidWorks failed to save part: {part_path}")

        executed_plan = {
            "source_plan": plan,
            "created_features": created_features,
            "part_path": str(part_path),
            "material": material,
            "material_metadata": material_metadata,
        }
        parameter_path.write_text(json.dumps(executed_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return SkillResult(
            success=True,
            message=f"SolidWorks part created: {part_path}",
            data=executed_plan,
            path=str(part_path),
            output=json.dumps(executed_plan, ensure_ascii=False, indent=2),
        )

    def run_plan_file(self, plan_path: Path) -> SkillResult:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        return self.run_plan(plan)

    def _ensure_skill_imports(self) -> None:
        if not self.skill_script_dir.exists():
            raise RuntimeError(f"SolidWorks automation scripts not found: {self.skill_script_dir}")
        script_dir = str(self.skill_script_dir)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        os.environ["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["CAD_AGENT_NO_INTERACTIVE"] = "1"

    @staticmethod
    def _set_material_metadata(model: Any, material: str) -> bool:
        if not material:
            return False
        try:
            manager = model.Extension.CustomPropertyManager("")
            manager.Add3("CAD_AGENT_MATERIAL", 30, material, 2)
            return True
        except Exception:
            return False

    @staticmethod
    def _ensure_non_interactive_com_dependencies() -> None:
        try:
            from sw_preflight import ensure_com_dependencies

            ensure_com_dependencies(allow_install=False)
        except ModuleNotFoundError as exc:
            if exc.name in {"pythoncom", "win32com", "win32com.client"}:
                raise RuntimeError("缺少 pywin32，请在终端执行 python -m pip install pywin32") from exc
            if exc.name == "comtypes":
                raise RuntimeError("缺少 comtypes，请在终端执行 python -m pip install comtypes") from exc
            raise

    @staticmethod
    def _friendly_dependency_error(exc: Exception) -> str | None:
        text = str(exc)
        lowered = text.lower()
        if "lost sys.stdin" in lowered:
            return "SolidWorks Automation 必须使用非交互模式，已禁止 sw_preflight.py 调用 input()。请重试；如果仍失败，请检查 pywin32/comtypes。"
        if "pywin32" in lowered or "pythoncom" in lowered or "win32com" in lowered:
            return "缺少 pywin32，请在终端执行 python -m pip install pywin32"
        if "comtypes" in lowered:
            return "缺少 comtypes，请在终端执行 python -m pip install comtypes"
        return None

    @staticmethod
    def _feature_xy(params: dict[str, Any], length: float, width: float) -> tuple[float, float]:
        position = str(params.get("position", "center")).lower()
        if position == "center":
            return 0.0, 0.0
        xy = params.get("position_xy") or params.get("xy")
        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
            return float(xy[0]), float(xy[1])
        return 0.0, 0.0

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
        return safe.strip("_") or "vibecad_part"
