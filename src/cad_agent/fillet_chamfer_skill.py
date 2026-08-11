from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import SkillResult


class FilletChamferCNCSkill:
    key = "solidworks_cnc_fillet"
    name = "SolidWorks Fillet/Chamfer/CNC Skill"

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir or (
            Path.home()
            / ".codex"
            / "skills"
            / "solidworks-automation"
            / "subskills"
            / "solidworks-fillet-chamfer-cnc"
        )
        self.entry_script = self.skill_dir / "scripts" / "create_cnc_mount_template.py"

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        if not self._has_fillet_or_chamfer(plan):
            return SkillResult(False, "No fillet/chamfer/CNC feature found in plan.")

        run_dir = self.output_root / f"fillet_chamfer_cnc_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            [sys.executable, str(self.entry_script)],
            cwd=str(run_dir),
            env=self._non_interactive_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=240,
        )
        output = "\n".join(part for part in (process.stdout.strip(), process.stderr.strip()) if part)
        output_dir = run_dir / "solidworks_fillet_chamfer_output"
        files = [str(path) for path in output_dir.glob("*")] if output_dir.exists() else []
        checks = self._check_outputs(output_dir)
        result_data = {
            "output_dir": str(output_dir),
            "returncode": process.returncode,
            "files": files,
            "checks": checks,
        }
        (run_dir / "fillet_chamfer_skill_result.json").write_text(
            json.dumps(result_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return SkillResult(
            success=process.returncode == 0 and all(checks.values()),
            message=f"Fillet/Chamfer/CNC Skill finished with exit code {process.returncode}: {output_dir}",
            data=result_data,
            path=str(output_dir),
            output=output,
        )

    @staticmethod
    def _has_fillet_or_chamfer(plan: dict[str, Any]) -> bool:
        return any(feature.get("type") in {"fillet", "chamfer"} for feature in plan.get("features", []))

    @staticmethod
    def _check_outputs(output_dir: Path) -> dict[str, bool]:
        if not output_dir.exists():
            return {
                "output_dir_exists": False,
                "sldprt_exists": False,
                "step_exists": False,
                "parameters_json_exists": False,
                "review_report_exists": False,
                "preview_exists": False,
            }
        files = list(output_dir.glob("*"))
        return {
            "output_dir_exists": True,
            "sldprt_exists": any(path.suffix.lower() == ".sldprt" and path.stat().st_size > 0 for path in files),
            "step_exists": any(path.suffix.lower() == ".step" and path.stat().st_size > 0 for path in files),
            "parameters_json_exists": any(path.name.endswith("_parameters.json") and path.stat().st_size > 0 for path in files),
            "review_report_exists": any(path.name.endswith("_review_report.json") and path.stat().st_size > 0 for path in files),
            "preview_exists": any(
                ("isometric" in path.stem.lower() or "preview" in path.stem.lower())
                and path.suffix.lower() in {".bmp", ".png"}
                and path.stat().st_size > 0
                for path in files
            ),
        }

    @staticmethod
    def _non_interactive_env() -> dict[str, str]:
        env = os.environ.copy()
        env["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        env["CAD_AGENT_NO_INTERACTIVE"] = "1"
        return env
