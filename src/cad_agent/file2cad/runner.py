from __future__ import annotations

import json
import os
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..autocad_annotation_skill import AutoCADAnnotationSkill
from ..autocad_skill import AutoCADSkill
from ..pdf2cad.autocad_reconstructor import PDF2CADAutoCADReconstructor
from ..runtime_config import output_root as public_output_root


AUTOCAD_INPUTS = {".dwg", ".dxf", ".dwt"}
SOLIDWORKS_INPUTS = {
    ".sldprt",
    ".sldasm",
    ".slddrw",
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".stl",
    ".x_t",
    ".x_b",
}


class CADFilePipelineRunner:
    """Normalize existing CAD/exchange files without redrawing valid geometry."""

    def __init__(self, output_root: str | Path, autocad_skill_dir: str | Path | None = None) -> None:
        self.output_root = Path(output_root).resolve()
        self.autocad_skill_dir = Path(autocad_skill_dir).resolve() if autocad_skill_dir else None

    def plan(
        self,
        source_path: str | Path,
        requested_outputs: list[str] | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(str(source))
        suffix = source.suffix.lower()
        if suffix not in AUTOCAD_INPUTS | SOLIDWORKS_INPUTS:
            raise ValueError(f"Unsupported CAD input format: {suffix or '<none>'}")
        outputs = self._normalize_outputs(requested_outputs or self._default_outputs(suffix))
        wants_annotation = self._annotation_requested(prompt)
        unsupported = self._unsupported_outputs(suffix, outputs)
        if suffix in AUTOCAD_INPUTS:
            pipeline = ["cad_file_open", "autocad_normalize"]
            if wants_annotation:
                pipeline.append("autocad_annotation")
            pipeline.extend(f"export_{item.lower().replace(' ', '_')}" for item in outputs)
            route = "autocad"
        else:
            pipeline = ["cad_file_open", "solidworks_import"]
            pipeline.extend(f"export_{item.lower().replace(' ', '_')}" for item in outputs)
            route = "solidworks"
        design = {
            "schema_version": "cad-file.design.v1",
            "source_type": "cad_exchange_file",
            "source_path": str(source),
            "source_format": suffix.lstrip(".").upper(),
            "intent": "normalize_existing_cad",
            "parameters": {"unit": "source", "source_path": str(source), "source_format": suffix.lstrip(".")},
            "features": [],
            "outputs": outputs,
            "requested_stages": ["cad_file_conversion"],
            "forbidden_stages": [],
            "stop_after": "cad_file_conversion",
            "skill_pipeline": pipeline,
            "route": route,
            "annotate": wants_annotation,
            "unsupported_features": unsupported,
            "needs_confirmation": bool(unsupported),
            "confirmation_reason": (
                "Unsupported output conversion: " + ", ".join(item["output"] for item in unsupported)
                if unsupported
                else ""
            ),
        }
        plan_dir = self.output_root / "cad_file_plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plan_dir / "design_plan.json"
        plan_path.write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "task_type": "cad_file_conversion",
            "requested_stages": ["cad_file_conversion"],
            "stop_after": "cad_file_conversion",
            "plan_lines": pipeline,
            "skipped_lines": [],
            "outputs": outputs,
            "needs_confirmation": bool(unsupported),
            "confirmation_reason": design["confirmation_reason"],
            "unexecutable_required_features": unsupported,
        }
        return {"design_json": design, "summary": summary, "artifacts": {"gateway_design_plan": str(plan_path)}}

    def run(
        self,
        source_path: str | Path,
        design_json: dict[str, Any] | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        plan = design_json or self.plan(source)["design_json"]
        if plan.get("needs_confirmation"):
            raise RuntimeError(plan.get("confirmation_reason") or "CAD conversion plan is not executable.")
        run_dir = self.output_root / f"file2cad_{datetime.now():%Y%m%d_%H%M%S_%f}"
        run_dir.mkdir(parents=True, exist_ok=True)
        copied_source = run_dir / f"source_input{source.suffix.lower()}"
        shutil.copy2(source, copied_source)
        artifacts: dict[str, str] = {"source_file": str(copied_source), "original_source_file": str(source)}
        steps: list[dict[str, Any]] = []

        def emit(name: str, **payload: Any) -> None:
            item = {"event": name, **payload}
            steps.append(item)
            if event_callback:
                event_callback(item)

        emit("pipeline_started", source=str(source), route=plan.get("route"))
        status = "failed"
        error = None
        traceback_text = None
        try:
            if source.suffix.lower() in AUTOCAD_INPUTS:
                produced = self._run_autocad(copied_source, plan, run_dir / "autocad", emit)
            else:
                produced = self._run_solidworks(copied_source, plan, run_dir / "solidworks", emit)
            artifacts.update(produced)
            missing = [item for item in plan.get("outputs", []) if not self._artifact_for_output(item, artifacts)]
            if missing:
                raise RuntimeError(f"Requested outputs were not generated: {missing}")
            status = "success"
        except Exception as exc:
            error = repr(exc)
            traceback_text = traceback.format_exc()
            emit("pipeline_exception", error=error, traceback=traceback_text)
        report_path = run_dir / "pipeline_report.json"
        payload = {
            "status": status,
            "source_path": str(source),
            "run_dir": str(run_dir),
            "report_path": str(report_path),
            "design_json": plan,
            "steps": steps,
            "artifacts": artifacts,
            "error": error,
            "traceback": traceback_text,
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts["pipeline_report"] = str(report_path)
        self._publish(run_dir, artifacts)
        emit("pipeline_finished", status=status, report_path=str(report_path))
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _run_autocad(
        self,
        source: Path,
        plan: dict[str, Any],
        out_dir: Path,
        emit: Callable[..., None],
    ) -> dict[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_autocad_scripts()
        import acad_review
        import acad_session

        original_connect = acad_session.connect_autocad
        acad_session.connect_autocad = AutoCADSkill._connect_autocad
        session = None
        artifacts: dict[str, str] = {}
        try:
            emit("step_started", skill_key="cad_file_open", source=str(source))
            session = acad_session.AutoCADSession(create_if_missing=True, visible=True).connect()
            session.open_document(source, read_only=False)
            emit("step_finished", skill_key="cad_file_open", status="success")
            target_dwg = out_dir / f"{source.stem}_Converted.dwg"
            session.save_as(target_dwg)
            if not target_dwg.is_file() or target_dwg.stat().st_size <= 0:
                raise RuntimeError("AutoCAD did not create a non-empty DWG.")
            artifacts["dwg"] = str(target_dwg)
            emit("step_finished", skill_key="autocad_normalize", status="success", path=str(target_dwg))
            outputs = set(plan.get("outputs", []))
            if "DXF" in outputs:
                target_dxf = out_dir / f"{source.stem}_Converted.dxf"
                session.export_dxf(target_dxf)
                if target_dxf.is_file() and target_dxf.stat().st_size > 0:
                    artifacts["dxf"] = str(target_dxf)
            if "PDF" in outputs:
                target_pdf = out_dir / f"{source.stem}_Converted.pdf"
                PDF2CADAutoCADReconstructor._export_pdf(session, target_pdf)
                if target_pdf.is_file() and target_pdf.stat().st_size > 0:
                    artifacts["pdf"] = str(target_pdf)
            review = acad_review.review_active(session)
            review_path = out_dir / "cad_file_review.json"
            review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts["cad_review"] = str(review_path)
            session.close_document(save_changes=False)
            session = None
        finally:
            if session is not None:
                try:
                    session.close_document(save_changes=False)
                except Exception:
                    pass
            acad_session.connect_autocad = original_connect

        if plan.get("annotate"):
            source_dwg = artifacts.get("dwg")
            result = AutoCADAnnotationSkill(self.output_root, self.autocad_skill_dir).run_plan(
                {"annotation_output_dir": str(out_dir / "annotation")},
                source_dwg_path=source_dwg,
            )
            if not result.success:
                raise RuntimeError(result.message)
            if result.path:
                artifacts["annotated_dwg"] = result.path
            report = result.data.get("report_path") or result.data.get("annotation_report")
            if report:
                artifacts["annotation_report"] = str(report)
        return artifacts

    def _run_solidworks(
        self,
        source: Path,
        plan: dict[str, Any],
        out_dir: Path,
        emit: Callable[..., None],
    ) -> dict[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("CAD_AGENT_NO_INTERACTIVE", "1")
        scripts = Path.home() / ".codex" / "skills" / "solidworks-automation" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from sw_session import SolidWorksSession

        emit("step_started", skill_key="solidworks_import", source=str(source))
        session = SolidWorksSession(wait_seconds=10, visible=True)
        import_method = "OpenDoc6"
        if source.suffix.lower() in {".step", ".stp", ".iges", ".igs"}:
            model = self._load_neutral_cad(session.sw, source)
            import_method = "LoadFile4"
        else:
            model = session.open(source, read_only=False, silent=True)
        if model is None:
            raise RuntimeError(f"SolidWorks could not import {source}")
        doc_type_member = getattr(model, "GetType", None)
        doc_type = int(doc_type_member() if callable(doc_type_member) else doc_type_member)
        stem = source.stem + "_Converted"
        artifacts: dict[str, str] = {}
        outputs = set(plan.get("outputs", []))
        native_ext = {1: ".SLDPRT", 2: ".SLDASM", 3: ".SLDDRW"}.get(doc_type, ".SLDPRT")
        native_key = {1: "sldprt", 2: "sldasm", 3: "slddrw"}.get(doc_type, "sldprt")
        native_target = out_dir / f"{stem}{native_ext}"
        if any(item in outputs for item in ("SLDPRT", "SLDASM", "SLDDRW")) or source.suffix.lower() not in {".sldprt", ".sldasm", ".slddrw"}:
            session.save(model, native_target)
            if native_target.is_file() and native_target.stat().st_size > 0:
                artifacts[native_key] = str(native_target)
        export_map = {
            "STEP": ("step", ".step"),
            "STL": ("stl", ".stl"),
            "IGES": ("iges", ".iges"),
            "DXF": ("dxf", ".dxf"),
            "DWG": ("dwg", ".dwg"),
            "PDF": ("pdf", ".pdf"),
        }
        for output, (key, extension) in export_map.items():
            if output not in outputs:
                continue
            target = out_dir / f"{stem}{extension}"
            success = session.export(model, target)
            if success and target.is_file() and target.stat().st_size > 0:
                artifacts[key] = str(target)
        emit("step_finished", skill_key="solidworks_import", status="success", import_method=import_method, outputs=artifacts)
        return artifacts

    @staticmethod
    def _load_neutral_cad(sw: Any, source: Path) -> Any:
        import pythoncom  # type: ignore
        from win32com.client import VARIANT  # type: ignore

        import_data = sw.GetImportFileData(str(source))
        if import_data is not None and hasattr(import_data, "MapConfigurationData"):
            try:
                import_data.MapConfigurationData = False
            except Exception:
                pass
        errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        model = sw.LoadFile4(str(source), "r", import_data, errors)
        if model is None:
            raise RuntimeError(f"SolidWorks LoadFile4 failed with error code {errors.value}: {source}")
        return model

    def _ensure_autocad_scripts(self) -> None:
        skill_dir = self.autocad_skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation" / "subskills" / "autocad-automation"
        )
        script_dir = str(skill_dir / "scripts")
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

    @staticmethod
    def _default_outputs(suffix: str) -> list[str]:
        if suffix == ".dxf":
            return ["DWG", "DXF"]
        if suffix in AUTOCAD_INPUTS:
            return ["DWG"]
        if suffix == ".sldasm":
            return ["SLDASM"]
        if suffix == ".slddrw":
            return ["SLDDRW"]
        return ["SLDPRT"]

    @staticmethod
    def _annotation_requested(prompt: str) -> bool:
        text = str(prompt or "").strip()
        lowered = text.lower()
        negative_patterns = (
            r"\bwithout\s+(?:any\s+)?(?:annotation|annotations|dimension|dimensions)\b",
            r"\b(?:do\s+not|don't|no)\s+(?:add\s+)?(?:annotate|annotation|annotations|dimension|dimensions)\b",
            r"(?:不要|不需要|无需|禁止|不)(?:自动)?(?:添加|生成)?(?:尺寸)?标注",
            r"(?:不要|不需要|无需|禁止|不)(?:添加|生成)?尺寸",
        )
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in negative_patterns):
            return False
        return any(token in lowered for token in ("annotat", "dimension")) or any(token in text for token in ("标注", "尺寸"))

    @staticmethod
    def _normalize_outputs(outputs: list[str]) -> list[str]:
        aliases = {
            "STP": "STEP",
            "IGS": "IGES",
            "ANNOTATED_DWG": "Annotated DWG",
            "SLDPRT": "SLDPRT",
            "SLDASM": "SLDASM",
            "SLDDRW": "SLDDRW",
        }
        normalized: list[str] = []
        for item in outputs:
            key = str(item).strip()
            canonical = aliases.get(key.upper(), key.upper())
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @staticmethod
    def _unsupported_outputs(suffix: str, outputs: list[str]) -> list[dict[str, str]]:
        if suffix in AUTOCAD_INPUTS:
            supported = {"DWG", "DXF", "PDF", "Annotated DWG"}
        elif suffix == ".slddrw":
            supported = {"SLDDRW", "DWG", "DXF", "PDF"}
        elif suffix == ".sldasm":
            supported = {"SLDASM", "STEP", "STL", "IGES"}
        else:
            supported = {"SLDPRT", "STEP", "STL", "IGES", "DXF", "DWG"}
        return [
            {"type": "unsupported_output", "output": item, "reason": f"{suffix} cannot be safely converted to {item} by the current production route."}
            for item in outputs
            if item not in supported
        ]

    @staticmethod
    def _artifact_for_output(output: str, artifacts: dict[str, str]) -> str | None:
        keys = {
            "DWG": "dwg",
            "DXF": "dxf",
            "PDF": "pdf",
            "Annotated DWG": "annotated_dwg",
            "SLDPRT": "sldprt",
            "SLDASM": "sldasm",
            "SLDDRW": "slddrw",
            "STEP": "step",
            "STL": "stl",
            "IGES": "iges",
        }
        value = artifacts.get(keys.get(output, ""))
        return value if value and Path(value).is_file() and Path(value).stat().st_size > 0 else None

    @staticmethod
    def _publish(run_dir: Path, artifacts: dict[str, str]) -> None:
        delivery = public_output_root() / run_dir.name
        delivery.mkdir(parents=True, exist_ok=True)
        artifacts["delivery_dir"] = str(delivery)
        for key, value in list(artifacts.items()):
            path = Path(value)
            if not path.is_file():
                continue
            target = delivery / path.name
            if path.resolve() != target.resolve():
                shutil.copy2(path, target)
            artifacts[f"delivery_{key}"] = str(target)
