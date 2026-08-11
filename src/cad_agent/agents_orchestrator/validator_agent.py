from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agents_config import AgentsOrchestratorConfig


class ValidatorAgent:
    """Validates report artifacts without calling CAD APIs."""

    def __init__(self, config: AgentsOrchestratorConfig | None = None) -> None:
        self.config = config or AgentsOrchestratorConfig.detect()
        self.sdk_agent = self.config.create_sdk_agent(
            "ValidatorAgent",
            "Validate CAD pipeline reports and expected output artifacts. Do not call CAD software.",
        )

    BASE_ARTIFACT_KEYS = ("brain_plan", "design_json", "pipeline_report")

    def validate(self, pipeline_report: str | Path | None) -> dict[str, Any]:
        if not pipeline_report:
            return {"status": "failed", "missing": ["pipeline_report"], "present": {}, "checked_paths": {}, "checks": {}}
        report_path = Path(pipeline_report)
        checked: dict[str, str] = {"pipeline_report": str(report_path)}
        present: dict[str, str] = {}
        missing: list[str] = []
        checks: dict[str, dict[str, Any]] = {}
        if report_path.is_file():
            present["pipeline_report"] = str(report_path)
            report = self._read_json(report_path)
            checks["pipeline_report"] = self._file_check(report_path, expect_json=True)
        else:
            return {"status": "failed", "missing": ["pipeline_report"], "present": present, "checked_paths": checked, "checks": checks}

        artifacts = report.get("artifacts", {}) if isinstance(report, dict) else {}
        scope_validation = report.get("final_model_validation", {}) if isinstance(report, dict) else {}
        expected_keys = self._expected_artifact_keys(report)
        for key in expected_keys:
            value = artifacts.get(key)
            if not value and key == "pipeline_report":
                value = str(report_path)
            if not value:
                missing.append(key)
                continue
            path = Path(value)
            checked[key] = str(path)
            expect_json = key in {"brain_plan", "design_json", "pipeline_report", "autocad_annotation_report"}
            checks[key] = self._file_check(path, expect_json=expect_json)
            if checks[key]["exists"] and checks[key]["size_gt_zero"] and (not expect_json or checks[key]["json_parseable"]):
                present[key] = str(path)
            else:
                missing.append(key)
        if scope_validation.get("status") == "failed":
            missing.append("final_model_scope_mismatch")
        if report.get("status") == "failed":
            status = "failed"
        else:
            status = "success" if not missing and report.get("status") == "success" else "partial" if present else "failed"
        return {"status": status, "missing": missing, "present": present, "checked_paths": checked, "checks": checks, "expected_artifact_keys": expected_keys, "final_model_validation": scope_validation}

    @classmethod
    def _expected_artifact_keys(cls, report: dict[str, Any]) -> tuple[str, ...]:
        design = report.get("design_json", {}) if isinstance(report, dict) else {}
        stages = set(design.get("requested_stages", []) or ["model_3d"])
        outputs = set(str(item) for item in design.get("outputs", []))
        keys = list(cls.BASE_ARTIFACT_KEYS)
        if "model_3d" in stages:
            keys.append("solidworks_model")
        if "drawing" in stages:
            keys.append("solidworks_drawing")
        if "autocad_annotation" in stages:
            keys.extend(["annotated_dwg", "autocad_annotation_report"])
        if "export_files" in stages:
            if "STEP" in outputs:
                keys.append("step")
            if "DWG" in outputs or "DXF" in outputs:
                keys.append("dwg")
            if "PDF" in outputs:
                keys.append("pdf")
            if "Annotated DWG" in outputs:
                keys.append("annotated_dwg")
        return tuple(dict.fromkeys(keys))

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _file_check(path: Path, expect_json: bool = False) -> dict[str, Any]:
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        json_parseable = None
        if expect_json and exists and size > 0:
            try:
                json.loads(path.read_text(encoding="utf-8"))
                json_parseable = True
            except Exception:
                json_parseable = False
        return {
            "path": str(path),
            "exists": exists,
            "size": size,
            "size_gt_zero": size > 0,
            "json_parseable": json_parseable,
        }
