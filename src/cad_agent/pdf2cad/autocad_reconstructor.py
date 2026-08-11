from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..autocad_skill import AutoCADSkill


class PDF2CADAutoCADReconstructor:
    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation" / "subskills" / "autocad-automation"
        )

    def reconstruct(self, design_json: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        dwg_path = out_dir / "pdf2cad_reconstructed.dwg"
        dxf_path = out_dir / "pdf2cad_reconstructed.dxf"
        pdf_path = out_dir / "pdf2cad_reconstructed.pdf"
        brief_path = out_dir / "autocad_brief.json"
        report_path = out_dir / "autocad_reconstruction_report.json"

        self._ensure_acad_scripts()
        import acad_draw
        import acad_review
        import acad_session

        brief = self._build_brief(design_json)
        brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
        original_connect = acad_session.connect_autocad
        acad_session.connect_autocad = AutoCADSkill._connect_autocad
        errors: list[str] = []
        review: dict[str, Any] = {}
        try:
            acad_draw.run(brief, dwg_path, new_document=True, source=None, fast_mode=True, step_delay_s=0.0)
            session = acad_session.AutoCADSession(create_if_missing=False, visible=True).connect()
            session.open_document(dwg_path, read_only=False)
            self._add_native_annotations(session, design_json)
            self._save(session, dwg_path, errors)
            try:
                session.export_dxf(dxf_path)
            except Exception as exc:
                errors.append(f"DXF export failed: {exc!r}")
                try:
                    self._write_fallback_dxf(dxf_path, design_json)
                except Exception as fallback_exc:
                    errors.append(f"DXF fallback failed: {fallback_exc!r}")
            try:
                self._export_pdf(session, pdf_path)
            except Exception as exc:
                errors.append(f"PDF export failed: {exc!r}")
                try:
                    self._write_fallback_pdf(pdf_path, design_json)
                except Exception as fallback_exc:
                    errors.append(f"PDF fallback failed: {fallback_exc!r}")
            try:
                review = acad_review.review_active(session)
            except Exception as exc:
                errors.append(f"AutoCAD review failed: {exc!r}")
            try:
                session.close_document(save_changes=True)
            except Exception:
                pass
        finally:
            acad_session.connect_autocad = original_connect

        payload = {
            "dwg_path": str(dwg_path),
            "dxf_path": str(dxf_path) if dxf_path.exists() else None,
            "pdf_path": str(pdf_path) if pdf_path.exists() else None,
            "brief_path": str(brief_path),
            "review": review,
            "errors": errors,
            "files": [
                str(path)
                for path in (dwg_path, dxf_path, pdf_path, brief_path)
                if path.exists() and path.stat().st_size > 0
            ],
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["report_path"] = str(report_path)
        payload["files"].append(str(report_path))
        payload["success"] = dwg_path.exists() and dwg_path.stat().st_size > 0
        return payload

    def _ensure_acad_scripts(self) -> None:
        script_dir = str(self.skill_dir / "scripts")
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

    @staticmethod
    def _build_brief(design_json: dict[str, Any]) -> dict[str, Any]:
        params = design_json.get("parameters", {})
        length = float(params.get("length") or 1080.0)
        width = float(params.get("width") or 860.0)
        pitch_x = float(params.get("grid_pitch_x") or 20.0)
        pitch_y = float(params.get("grid_pitch_y") or pitch_x)
        wire = float(params.get("wire_diameter") or 2.5)
        title = design_json.get("title_block", {})
        material = params.get("material") or ""
        entities: list[dict[str, Any]] = [
            {"type": "rectangle", "origin": [0, 0], "width": length, "height": width, "layer": "0"},
        ]
        x = pitch_x
        while x < length - 1e-6:
            entities.append({"type": "line", "start": [x, 0, 0], "end": [x, width, 0], "layer": "GRID"})
            x += pitch_x
        y = pitch_y
        while y < width - 1e-6:
            entities.append({"type": "line", "start": [0, y, 0], "end": [length, y, 0], "layer": "GRID"})
            y += pitch_y
        title_x = length + 40
        title_y = max(width - 20, 40)
        lines = [
            f"Name: {title.get('name', '')}",
            f"Drawing No: {title.get('drawing_no', '')}",
            f"Material: {material}",
            f"Overall: {length:g} x {width:g}",
            f"Grid pitch: {pitch_x:g} x {pitch_y:g}",
            f"Wire dia: %%c{wire:g}",
        ]
        for index, text in enumerate(lines):
            entities.append(
                {
                    "type": "text",
                    "text": text,
                    "point": [title_x, title_y - index * 18, 0],
                    "height": 10,
                    "layer": "TITLE",
                }
            )
        return {
            "units": "mm",
            "live_preview": False,
            "layers": [
                {"name": "0", "color": 7},
                {"name": "GRID", "color": 8},
                {"name": "TITLE", "color": 3},
                {"name": "A-ANNO-DIMS", "color": 2},
                {"name": "A-ANNO-TEXT", "color": 3},
            ],
            "entities": entities,
        }

    @staticmethod
    def _add_native_annotations(session: Any, design_json: dict[str, Any]) -> None:
        import acad_session

        params = design_json.get("parameters", {})
        length = float(params.get("length") or 1080.0)
        width = float(params.get("width") or 860.0)
        pitch_x = float(params.get("grid_pitch_x") or 20.0)
        wire = float(params.get("wire_diameter") or 2.5)
        model = session.model
        layer = "A-ANNO-DIMS"
        session.create_layer(layer, color=2)
        dims = (
            ((0, 0, 0), (length, 0, 0), (length / 2, -55, 0), 0.0),
            ((0, 0, 0), (0, width, 0), (-55, width / 2, 0), 1.5707963267948966),
            ((0, 0, 0), (pitch_x, 0, 0), (pitch_x / 2, -25, 0), 0.0),
        )
        for p1, p2, loc, angle in dims:
            dim = model.AddDimRotated(acad_session.acad_point(p1), acad_session.acad_point(p2), acad_session.acad_point(loc), angle)
            dim.Layer = layer
        text = model.AddText(f"Wire diameter %%c{wire:g}", acad_session.acad_point((length + 40, max(width - 140, 20), 0)), 10)
        text.Layer = "A-ANNO-TEXT"

    @staticmethod
    def _save(session: Any, path: Path, errors: list[str]) -> None:
        for call in (lambda: session.active_document().Save(), lambda: session.active_document().SaveAs(str(path))):
            try:
                call()
                return
            except Exception as exc:
                errors.append(f"DWG save attempt failed: {exc!r}")

    @staticmethod
    def _export_pdf(session: Any, pdf_path: Path) -> None:
        doc = session.active_document()
        try:
            layout = doc.ActiveLayout
            layout.ConfigName = "DWG To PDF.pc3"
            layout.CenterPlot = True
            layout.PlotWithPlotStyles = True
        except Exception:
            pass
        try:
            doc.Plot.QuietErrorMode = True
        except Exception:
            pass
        doc.Plot.PlotToFile(str(pdf_path), "DWG To PDF.pc3")

    @staticmethod
    def _write_fallback_dxf(path: Path, design_json: dict[str, Any]) -> None:
        params = design_json.get("parameters", {})
        length = float(params.get("length") or 1080.0)
        width = float(params.get("width") or 860.0)
        pitch_x = float(params.get("grid_pitch_x") or 20.0)
        pitch_y = float(params.get("grid_pitch_y") or pitch_x)
        entities: list[str] = []

        def line(x1: float, y1: float, x2: float, y2: float, layer: str) -> None:
            entities.extend(
                [
                    "0",
                    "LINE",
                    "8",
                    layer,
                    "10",
                    f"{x1:g}",
                    "20",
                    f"{y1:g}",
                    "30",
                    "0",
                    "11",
                    f"{x2:g}",
                    "21",
                    f"{y2:g}",
                    "31",
                    "0",
                ]
            )

        line(0, 0, length, 0, "OUTLINE")
        line(length, 0, length, width, "OUTLINE")
        line(length, width, 0, width, "OUTLINE")
        line(0, width, 0, 0, "OUTLINE")
        x = pitch_x
        while x < length - 1e-6:
            line(x, 0, x, width, "GRID")
            x += pitch_x
        y = pitch_y
        while y < width - 1e-6:
            line(0, y, length, y, "GRID")
            y += pitch_y
        path.write_text(
            "\n".join(["0", "SECTION", "2", "ENTITIES", *entities, "0", "ENDSEC", "0", "EOF"]),
            encoding="ascii",
        )

    @staticmethod
    def _write_fallback_pdf(path: Path, design_json: dict[str, Any]) -> None:
        params = design_json.get("parameters", {})
        length = float(params.get("length") or 1080.0)
        width = float(params.get("width") or 860.0)
        pitch_x = float(params.get("grid_pitch_x") or 20.0)
        pitch_y = float(params.get("grid_pitch_y") or pitch_x)
        wire = float(params.get("wire_diameter") or 2.5)
        material = str(params.get("material") or "")
        title = design_json.get("title_block", {})
        page_w, page_h = 842.0, 595.0
        margin = 45.0
        scale = min((page_w - 260.0 - margin) / max(length, 1.0), (page_h - margin * 2) / max(width, 1.0))
        ox, oy = margin, margin

        def tx(x: float) -> float:
            return ox + x * scale

        def ty(y: float) -> float:
            return oy + y * scale

        commands = ["0.4 w", f"{tx(0):.2f} {ty(0):.2f} {length * scale:.2f} {width * scale:.2f} re S", "0.1 w"]
        x = pitch_x
        while x < length - 1e-6:
            commands.append(f"{tx(x):.2f} {ty(0):.2f} m {tx(x):.2f} {ty(width):.2f} l S")
            x += pitch_x
        y = pitch_y
        while y < width - 1e-6:
            commands.append(f"{tx(0):.2f} {ty(y):.2f} m {tx(length):.2f} {ty(y):.2f} l S")
            y += pitch_y
        text_x = tx(length) + 28.0
        text_y = page_h - margin
        text_lines = [
            f"Name: {title.get('name', '')}",
            f"Drawing No: {title.get('drawing_no', '')}",
            f"Material: {material}",
            f"Overall: {length:g} x {width:g}",
            f"Grid pitch: {pitch_x:g} x {pitch_y:g}",
            f"Wire dia: {wire:g}",
        ]
        commands.append("BT /F1 10 Tf")
        for index, line in enumerate(text_lines):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"1 0 0 1 {text_x:.2f} {text_y - index * 16:.2f} Tm ({safe}) Tj")
        commands.append("ET")
        content = "\n".join(commands).encode("latin-1", errors="replace")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:g} {page_h:g}] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>".encode(),
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        chunks = [b"%PDF-1.4\n"]
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(sum(len(chunk) for chunk in chunks))
            chunks.append(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref_offset = sum(len(chunk) for chunk in chunks)
        chunks.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            chunks.append(f"{offset:010d} 00000 n \n".encode())
        chunks.append(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
        path.write_bytes(b"".join(chunks))
