from __future__ import annotations

import json
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .autocad_reconstructor import PDF2CADAutoCADReconstructor
from .models import PDF2CADResult
from .parser import parse_pdf_to_ir
from .understanding import ir_to_design_json
from ..runtime_config import output_root as public_output_root


class PDF2CADPipelineRunner:
    def __init__(self, output_root: Path, autocad_skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.autocad_skill_dir = autocad_skill_dir

    def plan(self, pdf_path: str | Path, mode: str = "2d") -> dict[str, Any]:
        source = Path(pdf_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(str(source))
        plan_dir = self.output_root / f"pdf2cad_plan_{datetime.now():%Y%m%d_%H%M%S_%f}"
        parse_dir = plan_dir / "pdf_parse"
        ir = parse_pdf_to_ir(source, parse_dir)
        design = ir_to_design_json(ir, mode=mode)
        missing = self._missing_required_values(design, mode)
        unsupported: list[dict[str, Any]] = []
        if mode == "3d":
            unsupported.append(
                {
                    "type": "pdf_3d_reconstruction",
                    "reason": "A PDF may only enter SolidWorks after part-family and complete 3D feature geometry are proven; the current IR is 2D-only.",
                }
            )
        if ir.parser == "unresolved-image-pdf":
            missing.append("trustworthy_pdf_parse")
        confirmation_risks = [item for item in design.get("risks", []) if item.get("requires_confirmation")]
        if confirmation_risks:
            unsupported.append(
                {
                    "type": "drawing_conflict",
                    "risks": confirmation_risks,
                    "reason": "Drawing data contains conflicting values; user confirmation is required before CAD reconstruction.",
                }
            )
        missing = list(dict.fromkeys(missing))
        if missing:
            unsupported.append(
                {
                    "type": "missing_drawing_parameters",
                    "values": missing,
                    "reason": "Required dimensions were not extracted; user confirmation or better OCR input is required.",
                }
            )
        design["unsupported_features"] = unsupported
        design["needs_confirmation"] = bool(unsupported)
        design["confirmation_reason"] = "; ".join(str(item.get("reason")) for item in unsupported)
        design["parser"] = ir.parser
        design_path = plan_dir / "design_plan.json"
        design_path.write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "task_type": "pdf_to_cad_2d" if mode == "2d" else "pdf_to_cad_3d",
            "requested_stages": ["pdf_parse", "drawing_understanding", "cad_reconstruction"],
            "stop_after": "cad_reconstruction",
            "plan_lines": list(design.get("skill_pipeline", [])),
            "skipped_lines": ["SolidWorks 3D"] if mode == "2d" else [],
            "outputs": ["DWG", "DXF", "PDF"] if mode == "2d" else ["SLDPRT", "STEP"],
            "needs_confirmation": bool(unsupported),
            "confirmation_reason": design["confirmation_reason"],
            "unexecutable_required_features": unsupported,
        }
        return {
            "parser": ir.parser,
            "design_json": design,
            "summary": summary,
            "artifacts": {
                "drawing_ir": str(parse_dir / "drawing_ir.json"),
                "pdf_parse_markdown": str(parse_dir / "pdf_parse.md"),
                "design_json": str(design_path),
            },
        }

    def run(
        self,
        pdf_path: str | Path,
        mode: str = "2d",
        design_json: dict[str, Any] | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> PDF2CADResult:
        source = Path(pdf_path).resolve()
        run_dir = self.output_root / f"pdf2cad_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "pipeline_report.json"
        pdf2cad_report = run_dir / "pdf2cad_report.json"
        artifacts: dict[str, str] = {"source_pdf": str(source)}
        errors: list[str] = []
        steps: list[dict[str, Any]] = []

        def step(name: str, status: str, **extra: Any) -> None:
            item = {"step": name, "status": status, **extra}
            steps.append(item)
            if event_callback:
                event_callback({"event": "pdf2cad_step", **item})

        try:
            copied_pdf = run_dir / source.name
            if source.exists():
                shutil.copy2(source, copied_pdf)
                artifacts["copied_source_pdf"] = str(copied_pdf)

            if design_json is None:
                planned = self.plan(source, mode=mode)
                design = dict(planned["design_json"])
                artifacts.update({key: str(value) for key, value in planned.get("artifacts", {}).items()})
                step("pdf_parse", "success", parser=planned.get("parser"))
            else:
                design = dict(design_json)
                step("pdf_parse", "success", parser=design.get("parser"), precomputed=True)
            if design.get("needs_confirmation"):
                raise RuntimeError(design.get("confirmation_reason") or "PDF drawing understanding is incomplete.")
            if mode != "2d":
                raise RuntimeError("PDF-to-3D is blocked until a complete 3D feature plan is available.")
            design_path = run_dir / "design_plan.json"
            design_path.write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts["design_json"] = str(design_path)
            step("drawing_understanding", "success")

            reconstructor = PDF2CADAutoCADReconstructor(self.output_root, self.autocad_skill_dir)
            reconstruction = reconstructor.reconstruct(design, run_dir / "autocad_reconstruction")
            if not reconstruction.get("success"):
                raise RuntimeError("AutoCAD reconstruction failed.")
            artifacts["dwg"] = str(reconstruction.get("dwg_path"))
            if reconstruction.get("dxf_path"):
                artifacts["dxf"] = str(reconstruction["dxf_path"])
            if reconstruction.get("pdf_path"):
                artifacts["pdf"] = str(reconstruction["pdf_path"])
            artifacts["autocad_reconstruction_report"] = str(reconstruction.get("report_path"))
            step("cad_reconstruction", "success")
            step("autocad_generate_dwg", "success", dwg_path=artifacts["dwg"])
            step("autocad_annotation", "success", mode="native dimensions in reconstruction")
            step("export_pdf_dwg", "success")

            status = "success"
            message = "PDF2CAD pipeline completed."
        except Exception as exc:
            status = "failed"
            message = f"PDF2CAD pipeline failed: {exc}"
            errors.append(traceback.format_exc())
            step("pipeline_exception", "failed", error=repr(exc))

        payload = {
            "status": status,
            "message": message,
            "source_pdf": str(source),
            "mode": mode,
            "run_dir": str(run_dir),
            "report_path": str(report_path),
            "steps": steps,
            "artifacts": artifacts,
            "errors": errors,
        }
        pdf2cad_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts["pdf2cad_report"] = str(pdf2cad_report)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts["pipeline_report"] = str(report_path)
        self._publish(run_dir, artifacts)
        return PDF2CADResult(status == "success", message, str(run_dir), str(report_path), artifacts, errors)

    @staticmethod
    def _missing_required_values(design: dict[str, Any], mode: str) -> list[str]:
        params = design.get("parameters", {})
        required = ["length", "width"] if mode == "2d" else ["length", "width", "thickness"]
        return [key for key in required if not isinstance(params.get(key), (int, float)) or float(params.get(key) or 0) <= 0]

    @staticmethod
    def _publish(run_dir: Path, artifacts: dict[str, str]) -> None:
        delivery_dir = public_output_root() / run_dir.name
        delivery_dir.mkdir(parents=True, exist_ok=True)
        artifacts["delivery_dir"] = str(delivery_dir)
        for key, value in list(artifacts.items()):
            path = Path(value)
            if path.is_file():
                try:
                    target = delivery_dir / path.name
                    if path.resolve() != target.resolve():
                        shutil.copy2(path, target)
                    artifacts[f"delivery_{key}"] = str(target)
                except Exception:
                    pass
