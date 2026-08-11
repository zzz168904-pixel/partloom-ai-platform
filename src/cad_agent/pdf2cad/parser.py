from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..runtime_config import application_data_dir
from .models import PdfDrawingIR


def parse_pdf_to_ir(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    allow_test_fixture: bool = False,
) -> PdfDrawingIR:
    source = Path(pdf_path).resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mineru = _try_mineru(source, out_dir)
    if mineru:
        text = mineru.get("markdown") or mineru.get("text") or ""
        data = mineru.get("json") if isinstance(mineru.get("json"), dict) else {}
        ir = _ir_from_json(data, text)
        observations = mineru.get("ocr_observations") if isinstance(mineru.get("ocr_observations"), list) else []
        if observations:
            _enrich_ir_from_ocr(ir, observations)
        ir.parser = str(mineru.get("parser") or "mineru") + ("+rapidocr" if observations else "")
    else:
        text = _extract_pdf_text(source)
        if not text.strip():
            text = _try_ocr_text(source, out_dir) or ""
        ir = _ir_from_markdown(text)
        ir.parser = "ocr-text" if text and (out_dir / "ocr_output.txt").exists() else "local-pdf-text"
        if _is_empty_ir(ir):
            ir.parser = "unresolved-image-pdf"
            ir.notes.append(
                "No trustworthy engineering dimensions were extracted. Configure MinerU/OCR or provide a Markdown/JSON sidecar."
            )

    (out_dir / "pdf_parse.md").write_text(text or "", encoding="utf-8")
    (out_dir / "drawing_ir.json").write_text(json.dumps(ir.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return ir


def _is_empty_ir(ir: PdfDrawingIR) -> bool:
    return not (ir.geometry or ir.dimensions or ir.materials or ir.title_block or ir.notes)


def _mineru_root() -> Path:
    configured = os.environ.get("PARTLOOM_MINERU_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return application_data_dir() / "tools" / "mineru"


def _try_mineru(source: Path, out_dir: Path) -> dict[str, Any] | None:
    sidecar_json = source.with_suffix(".json")
    sidecar_md = source.with_suffix(".md")
    if sidecar_json.exists() or sidecar_md.exists():
        data: dict[str, Any] = {}
        text = ""
        if sidecar_json.exists():
            try:
                data = json.loads(sidecar_json.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if sidecar_md.exists():
            text = sidecar_md.read_text(encoding="utf-8", errors="replace")
        return {"parser": "mineru-sidecar", "json": data, "markdown": text}

    if os.environ.get("PDF2CAD_DISABLE_EXTERNAL_OCR", "").strip().lower() in {"1", "true", "yes"}:
        return None

    configured = os.environ.get("MINERU_COMMAND", "").strip()
    mineru_root = _mineru_root()
    candidates = [
        configured,
        shutil.which("magic-pdf") or "",
        shutil.which("mineru") or "",
        str(mineru_root / ".venv" / "Scripts" / "mineru.exe"),
        str(mineru_root / ".venv" / "Scripts" / "magic-pdf.exe"),
    ]
    command = next((item for item in candidates if item and (Path(item).is_file() or shutil.which(item))), None)
    if not command:
        return None
    backend = os.environ.get("MINERU_BACKEND", "pipeline").strip() or "pipeline"
    method = os.environ.get("MINERU_METHOD", "").strip() or ("auto" if _has_pdf_text_layer(source) else "ocr")
    language = os.environ.get("MINERU_LANGUAGE", "ch").strip() or "ch"
    timeout_s = int(os.environ.get("MINERU_TIMEOUT_SECONDS", "900"))
    model_cache = mineru_root / "model_cache"
    model_cache.mkdir(parents=True, exist_ok=True)
    process_env = os.environ.copy()
    process_env.setdefault("HF_HOME", str(model_cache / "HuggingFace"))
    process_env.setdefault("MODELSCOPE_CACHE", str(model_cache / "ModelScope"))
    try:
        process = subprocess.run(
            [command, "-p", str(source), "-o", str(out_dir), "-b", backend, "-m", method, "-l", language],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
            env=process_env,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    markdown_files = sorted(out_dir.rglob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    json_files = sorted(out_dir.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    text = markdown_files[0].read_text(encoding="utf-8", errors="replace") if markdown_files else process.stdout
    data = {}
    if json_files:
        try:
            data = json.loads(json_files[0].read_text(encoding="utf-8"))
        except Exception:
            data = {}
    observations = _try_rapidocr_images(out_dir, text)
    return {"parser": "mineru-cli", "json": data, "markdown": text, "ocr_observations": observations}


def _has_pdf_text_layer(source: Path) -> bool:
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(source))
        sample = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
        return len(sample.strip()) >= 80
    except Exception:
        return False


def _try_rapidocr_images(out_dir: Path, markdown: str) -> list[dict[str, Any]]:
    configured_python = os.environ.get("RAPIDOCR_PYTHON", "").strip()
    rapidocr_python = (
        Path(configured_python).expanduser()
        if configured_python
        else _mineru_root() / ".venv" / "Scripts" / "python.exe"
    )
    if not rapidocr_python.is_file():
        return []
    referenced = [Path(item).name for item in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)]
    images: list[Path] = []
    for name in referenced:
        images.extend(out_dir.rglob(name))
    if not images:
        images = sorted(
            [*out_dir.rglob("*.png"), *out_dir.rglob("*.jpg"), *out_dir.rglob("*.jpeg")],
            key=lambda item: item.stat().st_size,
            reverse=True,
        )[:5]
    images = list(dict.fromkeys(path.resolve() for path in images if path.is_file() and path.stat().st_size > 10_000))
    if not images:
        return []
    script = r'''
import json
import sys
import cv2
import numpy as np
from rapidocr import RapidOCR

engine = RapidOCR()
observations = []
for image_path in sys.argv[1:]:
    image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        continue
    for rotation in (0, 90, 270):
        if rotation == 90:
            frame = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == 270:
            frame = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        else:
            frame = image
        result = engine(frame)
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None) or ()
        scores = getattr(result, "scores", None) or ()
        height, width = frame.shape[:2]
        for index, text in enumerate(texts):
            box = boxes[index] if boxes is not None and index < len(boxes) else None
            center = [0.5, 0.5]
            if box is not None:
                center = [float(box[:, 0].mean()) / max(width, 1), float(box[:, 1].mean()) / max(height, 1)]
            observations.append({
                "image": image_path,
                "rotation": rotation,
                "text": str(text),
                "score": float(scores[index]) if index < len(scores) else 0.0,
                "center": center,
            })
print(json.dumps(observations, ensure_ascii=True))
'''
    try:
        process = subprocess.run(
            [str(rapidocr_python), "-c", script, *[str(path) for path in images]],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=240,
        )
        if process.returncode != 0 or not process.stdout.strip():
            (out_dir / "rapidocr_error.log").write_text(
                f"returncode={process.returncode}\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}",
                encoding="utf-8",
            )
            return []
        observations = None
        for line in reversed(process.stdout.strip().splitlines()):
            if line.lstrip().startswith("["):
                try:
                    observations = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if not isinstance(observations, list):
            (out_dir / "rapidocr_error.log").write_text(
                f"No JSON observation line found.\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}",
                encoding="utf-8",
            )
            return []
        (out_dir / "rapidocr_observations.json").write_text(
            json.dumps(observations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return observations
    except Exception as exc:
        (out_dir / "rapidocr_error.log").write_text(repr(exc), encoding="utf-8")
        return []


def _try_ocr_text(source: Path, out_dir: Path) -> str | None:
    """Use explicitly configured OCR tools without inventing drawing values."""

    sidecars = [source.with_suffix(".ocr.md"), source.with_suffix(".ocr.txt")]
    for sidecar in sidecars:
        if sidecar.is_file():
            text = sidecar.read_text(encoding="utf-8", errors="replace")
            (out_dir / "ocr_output.txt").write_text(text, encoding="utf-8")
            return text

    pdftotext = os.environ.get("PDFTOTEXT_COMMAND", "").strip() or shutil.which("pdftotext")
    if pdftotext:
        target = out_dir / "ocr_output.txt"
        try:
            process = subprocess.run(
                [str(pdftotext), "-layout", str(source), str(target)],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=90,
            )
            if process.returncode == 0 and target.is_file():
                text = target.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return text
        except Exception:
            pass
    return None


def _extract_pdf_text(source: Path) -> str:
    errors: list[str] = []
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(source)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        errors.append(repr(exc))
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(source))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        errors.append(repr(exc))
    bundled = _extract_pdf_text_with_bundled_python(source)
    if bundled is not None:
        return bundled
    raise RuntimeError("PDF text extraction failed: " + "; ".join(errors))


def _extract_pdf_text_with_bundled_python(source: Path) -> str | None:
    bundled_python = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "python"
        / "python.exe"
    )
    if not bundled_python.exists():
        return None
    script = r"""
from pathlib import Path
import sys
source = Path(sys.argv[1])
try:
    import pdfplumber
    with pdfplumber.open(str(source)) as pdf:
        print("\n".join(page.extract_text() or "" for page in pdf.pages))
        raise SystemExit(0)
except Exception:
    pass
try:
    from pypdf import PdfReader
    reader = PdfReader(str(source))
    print("\n".join(page.extract_text() or "" for page in reader.pages))
    raise SystemExit(0)
except Exception as exc:
    print(repr(exc), file=sys.stderr)
    raise SystemExit(2)
"""
    try:
        process = subprocess.run(
            [str(bundled_python), "-c", script, str(source)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return None
    if process.returncode == 0:
        return process.stdout
    return None


def _ir_from_json(data: dict[str, Any], fallback_text: str = "") -> PdfDrawingIR:
    if data.get("source_type") == "pdf_engineering_drawing":
        ir = PdfDrawingIR(
            geometry=list(data.get("geometry") or []),
            dimensions=list(data.get("dimensions") or []),
            materials=list(data.get("materials") or []),
            title_block=dict(data.get("title_block") or {}),
            views=list(data.get("views") or []),
            notes=list(data.get("notes") or []),
            raw_text=fallback_text,
        )
        return _enrich_ir_from_text(ir, fallback_text)
    return _ir_from_markdown(fallback_text or json.dumps(data, ensure_ascii=False))


def _ir_from_markdown(text: str) -> PdfDrawingIR:
    ir = PdfDrawingIR(raw_text=text)
    return _enrich_ir_from_text(ir, text)


def _enrich_ir_from_text(ir: PdfDrawingIR, text: str) -> PdfDrawingIR:
    normalized = _normalize_text(text)
    dims = _extract_dimensions(normalized)
    for item in dims:
        if item not in ir.dimensions:
            ir.dimensions.append(item)

    materials = re.findall(r"\b(SUS\s*(?:304|316)|6061|Q235|45#|316L|304)\b", normalized, re.IGNORECASE)
    for material in materials:
        clean = material.replace(" ", "").upper()
        if clean not in ir.materials:
            ir.materials.append(clean)
    if len(ir.materials) > 1:
        conflict = f"material_conflict: drawing contains {', '.join(ir.materials)}; user confirmation is required."
        if conflict not in ir.notes:
            ir.notes.append(conflict)

    drawing_no = _first_match(normalized, r"(?:图号|Drawing\s*No\.?|Part\s*No\.?)\s*[:：]?\s*([A-Za-z0-9_-]{4,})")
    if not drawing_no:
        drawing_no = _first_match(normalized, r"\b(\d{4}-\d{6,})\b")
    if drawing_no:
        ir.title_block.setdefault("drawing_no", drawing_no)

    name = _first_match(normalized, r"(?:名称|标题|Name|Title)\s*[:：]?\s*([\w\u4e00-\u9fff-]+)")
    if name:
        ir.title_block.setdefault("name", name.strip())

    scale = _first_match(normalized, r"(?:比例|Scale)\s*[:：]?\s*([0-9]+[:：][0-9]+)")
    if scale:
        ir.title_block.setdefault("scale", scale.replace("：", ":"))

    grid = _extract_grid_spacing(normalized)
    if grid:
        ir.geometry.append({"type": "grid", "pitch_x": grid[0], "pitch_y": grid[1]})
    overall = _extract_overall_size(normalized)
    if overall:
        ir.geometry.append({"type": "overall_rectangle", "length": overall[0], "width": overall[1]})
    wire = _extract_wire_diameter(normalized)
    if wire:
        ir.dimensions.append({"type": "diameter", "name": "wire_diameter", "value": wire, "symbol": "Ø"})
    for note in _extract_notes(normalized):
        if note not in ir.notes:
            ir.notes.append(note)
    return ir


def _enrich_ir_from_ocr(ir: PdfDrawingIR, observations: list[dict[str, Any]]) -> None:
    numeric: list[dict[str, Any]] = []
    for item in observations:
        text = str(item.get("text") or "").strip().replace("O", "0")
        score = float(item.get("score") or 0)
        if score < 0.70 or ":" in text:
            continue
        match = re.fullmatch(r"[^0-9]*([0-9]+(?:\.[0-9]+)?)[^0-9]*", text)
        if not match:
            continue
        value = float(match.group(1))
        if value <= 0 or value > 100_000:
            continue
        numeric.append({**item, "source_text": text, "value": value, "score": score})

    overall_values: dict[float, dict[str, Any]] = {}
    for item in numeric:
        value = float(item["value"])
        if 100 <= value <= 20_000 and item["score"] >= 0.82:
            current = overall_values.get(value)
            if current is None or item["score"] > current["score"]:
                overall_values[value] = item
    overall = sorted(overall_values.values(), key=lambda item: float(item["value"]), reverse=True)
    if len(overall) >= 2 and not _has_dimension(ir, "overall_length"):
        length_item, width_item = overall[0], overall[1]
        ir.dimensions.extend(
            [
                _ocr_dimension("overall_length", length_item),
                _ocr_dimension("overall_width", width_item),
            ]
        )
        ir.geometry.append(
            {
                "type": "overall_rectangle",
                "length": float(length_item["value"]),
                "width": float(width_item["value"]),
                "source": "rapidocr_spatial",
                "confidence": min(float(length_item["score"]), float(width_item["score"])),
            }
        )

    best_pair: tuple[dict[str, Any], dict[str, Any], float] | None = None
    decimals = [item for item in numeric if 0.1 <= item["value"] <= 20 and not float(item["value"]).is_integer() and item["score"] >= 0.85]
    pitches = [item for item in numeric if 5 <= item["value"] <= 100 and float(item["value"]).is_integer() and item["score"] >= 0.85]
    for decimal in decimals:
        for pitch in pitches:
            if decimal.get("image") != pitch.get("image") or decimal.get("rotation") != pitch.get("rotation"):
                continue
            a = decimal.get("center") or [0.5, 0.5]
            b = pitch.get("center") or [0.5, 0.5]
            distance = ((float(a[0]) - float(b[0])) ** 2 + (float(a[1]) - float(b[1])) ** 2) ** 0.5
            if distance <= 0.18 and (best_pair is None or distance < best_pair[2]):
                best_pair = (decimal, pitch, distance)
    if best_pair is not None:
        diameter_item, pitch_item, distance = best_pair
        pitch = float(pitch_item["value"])
        diameter = float(diameter_item["value"])
        if not _has_dimension(ir, "grid_pitch_x"):
            confidence = min(float(diameter_item["score"]), float(pitch_item["score"]), max(0.72, 0.96 - distance))
            ir.dimensions.extend(
                [
                    _ocr_dimension("grid_pitch_x", pitch_item, confidence=confidence),
                    _ocr_dimension("grid_pitch_y", pitch_item, confidence=confidence),
                    {
                        **_ocr_dimension("diameter", diameter_item, confidence=confidence),
                        "name": "wire_diameter",
                        "symbol": "Ø",
                        "inference": "decimal dimension spatially paired with orthogonal grid pitch",
                    },
                ]
            )
            ir.geometry.append(
                {
                    "type": "grid",
                    "pitch_x": pitch,
                    "pitch_y": pitch,
                    "source": "rapidocr_spatial",
                    "confidence": confidence,
                }
            )
            risk = (
                f"ocr_inference: interpreted {diameter:g} as wire diameter and {pitch:g} as grid pitch "
                f"from nearby dimension text (confidence={confidence:.2f})."
            )
            if risk not in ir.notes:
                ir.notes.append(risk)


def _ocr_dimension(dtype: str, item: dict[str, Any], confidence: float | None = None) -> dict[str, Any]:
    return {
        "type": dtype,
        "value": float(item["value"]),
        "unit": "mm",
        "source_text": str(item.get("source_text") or item.get("text") or ""),
        "source": "rapidocr_spatial",
        "confidence": round(float(confidence if confidence is not None else item.get("score") or 0), 4),
        "rotation": int(item.get("rotation") or 0),
    }


def _has_dimension(ir: PdfDrawingIR, dtype: str) -> bool:
    return any(item.get("type") == dtype for item in ir.dimensions)


def _normalize_text(text: str) -> str:
    return (
        text.replace("×", "x")
        .replace("X", "x")
        .replace("Φ", "Ø")
        .replace("φ", "Ø")
        .replace("：", ":")
    )


def _extract_dimensions(text: str) -> list[dict[str, Any]]:
    dims: list[dict[str, Any]] = []
    overall = _extract_overall_size(text)
    if overall:
        dims.extend(
            [
                {"type": "overall_length", "value": overall[0], "unit": "mm"},
                {"type": "overall_width", "value": overall[1], "unit": "mm"},
            ]
        )
    grid = _extract_grid_spacing(text)
    if grid:
        dims.extend(
            [
                {"type": "grid_pitch_x", "value": grid[0], "unit": "mm"},
                {"type": "grid_pitch_y", "value": grid[1], "unit": "mm"},
            ]
        )
    return dims


def _extract_overall_size(text: str) -> tuple[float, float] | None:
    patterns = (
        r"(?:整体尺寸|外形尺寸|Overall\s*Size|Size)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)",
        r"\b(\d{3,5}(?:\.\d+)?)\s*x\s*(\d{3,5}(?:\.\d+)?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return (float(match.group(1)), float(match.group(2)))
    return None


def _extract_grid_spacing(text: str) -> tuple[float, float] | None:
    patterns = (
        r"(?:网格间距|格距|Grid\s*Pitch|Pitch)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)",
        r"(?:网格间距|格距|Grid\s*Pitch|Pitch)\s*[:：]?\s*(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            first = float(match.group(1))
            second = float(match.group(2)) if match.lastindex and match.lastindex >= 2 and match.group(2) else first
            return (first, second)
    return None


def _extract_wire_diameter(text: str) -> float | None:
    match = re.search(r"(?:钢丝直径|丝径|Wire\s*Diameter|Wire\s*Dia\.?)\s*[:：]?\s*Ø?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"Ø\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _extract_notes(text: str) -> list[str]:
    notes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(token in stripped.lower() for token in ("note", "备注", "技术要求")):
            notes.append(stripped)
    return notes


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None
