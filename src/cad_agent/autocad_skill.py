from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import SkillResult


class AutoCADSkill:
    key = "autocad"
    name = "AutoCAD Automation"

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation" / "subskills" / "autocad-automation"
        )
        self.preflight_script = self.skill_dir / "scripts" / "acad_preflight.py"
        self.draw_script = self.skill_dir / "scripts" / "acad_draw.py"
        self.review_script = self.skill_dir / "scripts" / "acad_review.py"

    def run_preflight(self, launch: bool = False) -> SkillResult:
        try:
            app = self._connect_autocad(create_if_missing=launch, visible=True)
            data = {
                "prog_id": "AutoCAD.Application or versioned AutoCAD.Application.*",
                "version": str(getattr(app, "Version", "")),
                "caption": str(getattr(app, "Caption", "")),
            }
            return SkillResult(True, "AutoCAD preflight ok", data=data, output=json.dumps(data, ensure_ascii=False))
        except Exception:
            command = [sys.executable, str(self.preflight_script)]
            if launch:
                command.append("--launch")
            process = self._run(command, self.skill_dir, 60)
            if process["returncode"] == 0:
                return SkillResult(
                    True,
                    f"AutoCAD preflight exit code {process['returncode']}",
                    data={"returncode": process["returncode"]},
                    output=process["output"],
                )
        return SkillResult(
            success=process["returncode"] == 0,
            message=f"AutoCAD preflight exit code {process['returncode']}",
            data={"returncode": process["returncode"]},
            output=process["output"],
        )

    def run_plan(self, plan: dict[str, Any], fast: bool = True) -> SkillResult:
        run_dir = self.output_root / f"autocad_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        brief_path = run_dir / "acad_brief.json"
        dwg_path = run_dir / "vibecad_2d.dwg"
        review_path = run_dir / "acad_review.json"
        brief = self._plan_to_acad_brief(plan)
        brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

        try:
            review_report = self._draw_and_review_in_process(brief, dwg_path, review_path, fast=fast)
            draw_output = "in-process AutoCAD draw ok"
            review_output = json.dumps(review_report, ensure_ascii=False, indent=2)
            draw_returncode = 0
            review_returncode = 0
        except Exception as exc:
            draw_returncode = 1
            review_returncode = 1
            draw_output = repr(exc)
            review_output = ""

        success = draw_returncode == 0 and review_returncode == 0 and dwg_path.exists()
        data = {
            "brief_path": str(brief_path),
            "dwg_path": str(dwg_path),
            "review_path": str(review_path),
            "draw_returncode": draw_returncode,
            "review_returncode": review_returncode,
        }
        return SkillResult(
            success=success,
            message=f"AutoCAD DWG ready: {dwg_path}",
            data=data,
            path=str(dwg_path),
            output="\n".join(part for part in (draw_output, review_output) if part),
        )

    @staticmethod
    def _plan_to_acad_brief(plan: dict[str, Any]) -> dict[str, Any]:
        params = plan.get("parameters", {})
        length = float(params.get("length", 100.0))
        width = float(params.get("width", 50.0))
        origin = [-length / 2.0, -width / 2.0]
        entities: list[dict[str, Any]] = [
            {"type": "rectangle", "origin": origin, "width": length, "height": width, "layer": "OUTLINE"},
            {
                "type": "text",
                "text": f"{length:g} x {width:g} mm",
                "point": [origin[0], width / 2.0 + 8.0, 0],
                "height": max(2.5, min(length, width) / 18.0),
                "layer": "TEXT",
            },
        ]
        for feature in plan.get("features", []):
            if feature.get("type") not in {"through_hole", "threaded_hole"}:
                continue
            feature_params = feature.get("params", {})
            diameter = float(feature_params.get("diameter") or feature_params.get("tap_drill") or 0)
            if diameter <= 0:
                continue
            x, y = AutoCADSkill._feature_xy(feature_params)
            entities.append({"type": "circle", "center": [x, y, 0], "radius": diameter / 2.0, "layer": "HOLES"})
        return {
            "units": "mm",
            "live_preview": False,
            "layers": [
                {"name": "OUTLINE", "color": 7},
                {"name": "HOLES", "color": 1},
                {"name": "TEXT", "color": 3},
            ],
            "entities": entities,
        }

    @staticmethod
    def _feature_xy(params: dict[str, Any]) -> tuple[float, float]:
        if str(params.get("position", "")).lower() == "center":
            return 0.0, 0.0
        xy = params.get("position_xy") or params.get("xy")
        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
            return float(xy[0]), float(xy[1])
        return 0.0, 0.0

    def _draw_and_review_in_process(
        self,
        brief: dict[str, Any],
        dwg_path: Path,
        review_path: Path,
        fast: bool,
    ) -> dict[str, Any]:
        script_dir = str(self.skill_dir / "scripts")
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import acad_draw
        import acad_review
        import acad_session

        original_connect = acad_session.connect_autocad
        acad_session.connect_autocad = self._connect_autocad
        try:
            acad_draw.run(brief, dwg_path, new_document=True, source=None, fast_mode=fast, step_delay_s=0.0)
            session = acad_session.AutoCADSession(create_if_missing=False, visible=True).connect()
            session.open_document(dwg_path, read_only=True)
            report = acad_review.review_active(session)
            review_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return report
        finally:
            acad_session.connect_autocad = original_connect

    @staticmethod
    def _connect_autocad(create_if_missing: bool = True, visible: bool = True) -> Any:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        progids = (
            "AutoCAD.Application",
            "AutoCAD.Application.25.1",
            "AutoCAD.Application.25",
            "AutoCAD.Application.24.1",
            "AutoCAD.Application.24",
        )
        last_error: Exception | None = None
        for progid in progids:
            try:
                app = win32com.client.GetActiveObject(progid)
                try:
                    app.Visible = visible
                except Exception:
                    pass
                return app
            except Exception as exc:
                last_error = exc
        if not create_if_missing:
            raise RuntimeError(f"AutoCAD COM active object not found: {last_error!r}")
        for progid in progids:
            try:
                app = win32com.client.Dispatch(progid)
                try:
                    app.Visible = visible
                except Exception:
                    pass
                return app
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"AutoCAD COM unavailable: {last_error!r}")

    @staticmethod
    def _run(command: list[str], cwd: Path, timeout_s: int) -> dict[str, Any]:
        process = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
        )
        output = "\n".join(part for part in (process.stdout.strip(), process.stderr.strip()) if part)
        return {"returncode": process.returncode, "output": output}
