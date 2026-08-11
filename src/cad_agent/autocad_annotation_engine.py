from __future__ import annotations

import json
import math
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .autocad_skill import AutoCADSkill


Point3 = tuple[float, float, float]
BBox = tuple[float, float, float, float]


@dataclass
class EntityInfo:
    handle: str
    object_name: str
    kind: str
    layer: str
    bbox: BBox | None = None
    start: Point3 | None = None
    end: Point3 | None = None
    center: Point3 | None = None
    radius: float | None = None
    points: list[Point3] = field(default_factory=list)


@dataclass
class ViewRegion:
    name: str
    bbox: BBox
    entities: list[EntityInfo]
    role: str = "orthographic"


@dataclass
class DrawingAnalysis:
    entities: list[EntityInfo]
    bbox: BBox | None
    counts: dict[str, int]
    view_regions: list[ViewRegion] = field(default_factory=list)

    @property
    def lines(self) -> list[EntityInfo]:
        return [item for item in self.entities if item.kind == "line"]

    @property
    def circles(self) -> list[EntityInfo]:
        return [item for item in self.entities if item.kind == "circle"]

    @property
    def arcs(self) -> list[EntityInfo]:
        return [item for item in self.entities if item.kind == "arc"]


@dataclass
class DimensionPlan:
    dim_type: str
    layer: str
    reason: str
    p1: Point3 | None = None
    p2: Point3 | None = None
    location: Point3 | None = None
    center: Point3 | None = None
    radius: float | None = None
    angle: float = 0.0
    text: str = ""


@dataclass
class AnnotationResult:
    success: bool
    source_dwg: str
    annotated_dwg: str | None = None
    pdf_path: str | None = None
    report_path: str | None = None
    dimensions_created: int = 0
    centerlines_created: int = 0
    center_marks_created: int = 0
    entities_analyzed: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EntityAnalyzer:
    """Reads existing DWG geometry without recreating it."""

    SKIP_TOKENS = ("Dimension", "MText", "Text", "Hatch", "Leader")

    def analyze(self, session: Any) -> DrawingAnalysis:
        entities: list[EntityInfo] = []
        counts: dict[str, int] = {}
        for entity in session.iter_model_entities():
            object_name = _safe_str(entity, "ObjectName")
            if any(token in object_name for token in self.SKIP_TOKENS):
                continue
            info = self._entity_info(entity, object_name)
            if info.kind == "unknown":
                continue
            entities.append(info)
            counts[info.kind] = counts.get(info.kind, 0) + 1
        bbox = _combine_bboxes(item.bbox for item in entities)
        return DrawingAnalysis(
            entities=entities,
            bbox=bbox,
            counts=counts,
            view_regions=_detect_view_regions(entities, bbox),
        )

    def _entity_info(self, entity: Any, object_name: str) -> EntityInfo:
        lower = object_name.lower()
        kind = "unknown"
        if "line" in lower and "poly" not in lower:
            kind = "line"
        elif "circle" in lower:
            kind = "circle"
        elif "arc" in lower:
            kind = "arc"
        elif "polyline" in lower:
            kind = "polyline"
        elif "blockreference" in lower:
            kind = "block"

        info = EntityInfo(
            handle=_safe_str(entity, "Handle"),
            object_name=object_name,
            kind=kind,
            layer=_safe_str(entity, "Layer") or "0",
            bbox=_entity_bbox(entity),
        )
        info.start = _point_attr(entity, "StartPoint")
        info.end = _point_attr(entity, "EndPoint")
        info.center = _point_attr(entity, "Center")
        info.radius = _float_attr(entity, "Radius")
        if kind == "polyline":
            info.points = _polyline_points(entity)
        return info


class DimensionPlacement:
    def __init__(self, bbox: BBox) -> None:
        self.bbox = bbox
        width = max(1.0, bbox[2] - bbox[0])
        height = max(1.0, bbox[3] - bbox[1])
        self.offset = max(8.0, min(width, height) * 0.12)
        self.occupied: list[BBox] = []

    def horizontal_location(self, p1: Point3, p2: Point3, below: bool = True) -> Point3:
        y = self.bbox[1] - self.offset if below else self.bbox[3] + self.offset
        return self._avoid((min(p1[0], p2[0]), y - self.offset * 0.15, max(p1[0], p2[0]), y + self.offset * 0.15), (p1[0] + p2[0]) / 2.0, y, "y")

    def vertical_location(self, p1: Point3, p2: Point3, left: bool = True) -> Point3:
        x = self.bbox[0] - self.offset if left else self.bbox[2] + self.offset
        return self._avoid((x - self.offset * 0.15, min(p1[1], p2[1]), x + self.offset * 0.15, max(p1[1], p2[1])), x, (p1[1] + p2[1]) / 2.0, "x")

    def circle_location(self, center: Point3, radius: float, index: int) -> Point3:
        angle = math.radians(35 + index * 18)
        distance = max(radius * 2.2, self.offset)
        x = center[0] + math.cos(angle) * distance
        y = center[1] + math.sin(angle) * distance
        return (x, y, 0.0)

    def _avoid(self, rect: BBox, x: float, y: float, axis: str) -> Point3:
        current = rect
        while any(_rects_overlap(current, other) for other in self.occupied):
            if axis == "y":
                y -= self.offset * 0.55
                current = (current[0], y - self.offset * 0.15, current[2], y + self.offset * 0.15)
            else:
                x -= self.offset * 0.55
                current = (x - self.offset * 0.15, current[1], x + self.offset * 0.15, current[3])
        self.occupied.append(current)
        return (x, y, 0.0)


class DimensionStrategy:
    def build(self, analysis: DrawingAnalysis) -> list[DimensionPlan]:
        if analysis.bbox is None:
            return []
        if analysis.view_regions:
            return self._build_for_view_regions(analysis)
        return self._build_for_bbox(analysis, analysis.entities, analysis.bbox)

    def _build_for_view_regions(self, analysis: DrawingAnalysis) -> list[DimensionPlan]:
        plans: list[DimensionPlan] = []
        for region in analysis.view_regions:
            if region.role == "isometric":
                continue
            plans.extend(self._build_for_bbox(analysis, region.entities, region.bbox, region_name=region.name))
        if plans:
            return plans
        return self._build_for_bbox(analysis, analysis.entities, analysis.bbox)

    def _build_for_bbox(
        self,
        analysis: DrawingAnalysis,
        entities: Sequence[EntityInfo],
        bbox: BBox | None,
        region_name: str = "drawing",
    ) -> list[DimensionPlan]:
        if bbox is None:
            return []
        placement = DimensionPlacement(bbox)
        plans: list[DimensionPlan] = []
        min_x, min_y, max_x, max_y = bbox
        bottom_left = (min_x, min_y, 0.0)
        bottom_right = (max_x, min_y, 0.0)
        top_left = (min_x, max_y, 0.0)
        width = max_x - min_x
        height = max_y - min_y
        reason_prefix = "" if region_name == "drawing" else f"{region_name} "

        if width > 1.0:
            plans.append(
                DimensionPlan(
                    dim_type="rotated",
                    layer="A-ANNO-DIMS",
                    reason=f"{reason_prefix}overall length",
                    p1=bottom_left,
                    p2=bottom_right,
                    location=placement.horizontal_location(bottom_left, bottom_right, below=True),
                    angle=0.0,
                )
            )
        if height > 1.0:
            plans.append(
                DimensionPlan(
                    dim_type="rotated",
                    layer="A-ANNO-DIMS",
                    reason=f"{reason_prefix}overall height/width",
                    p1=bottom_left,
                    p2=top_left,
                    location=placement.vertical_location(bottom_left, top_left, left=True),
                    angle=math.pi / 2.0,
                )
            )
        circles = [item for item in entities if item.kind == "circle"]
        for index, circle in enumerate(circles):
            if circle.center is None or circle.radius is None or circle.radius <= 0:
                continue
            if not _point_inside_bbox(circle.center, bbox, tolerance=max(circle.radius, 1.0)):
                continue
            center = circle.center
            radius = circle.radius
            plans.append(
                DimensionPlan(
                    dim_type="diametric",
                    layer="A-ANNO-DIMS",
                    reason=f"{reason_prefix}hole diameter",
                    p1=(center[0] - radius, center[1], 0.0),
                    p2=(center[0] + radius, center[1], 0.0),
                    location=placement.circle_location(center, radius, index),
                )
            )
            plans.extend(self._center_plans(center, radius, bbox))
            plans.append(
                DimensionPlan(
                    dim_type="rotated",
                    layer="A-ANNO-DIMS",
                    reason=f"{reason_prefix}hole horizontal position",
                    p1=(min_x, center[1], 0.0),
                    p2=center,
                    location=placement.horizontal_location((min_x, center[1], 0.0), center, below=False),
                    angle=0.0,
                )
            )
            plans.append(
                DimensionPlan(
                    dim_type="rotated",
                    layer="A-ANNO-DIMS",
                    reason=f"{reason_prefix}hole vertical position",
                    p1=(center[0], min_y, 0.0),
                    p2=center,
                    location=placement.vertical_location((center[0], min_y, 0.0), center, left=False),
                    angle=math.pi / 2.0,
                )
            )
        arcs = [item for item in entities if item.kind == "arc"]
        for index, arc in enumerate(arcs):
            if arc.center is None or arc.radius is None or arc.radius <= 0:
                continue
            if not _point_inside_bbox(arc.center, bbox, tolerance=max(arc.radius, 1.0)):
                continue
            chord = arc.start or (arc.center[0] + arc.radius, arc.center[1], 0.0)
            plans.append(
                DimensionPlan(
                    dim_type="radial",
                    layer="A-ANNO-DIMS",
                    reason=f"{reason_prefix}arc/fillet radius",
                    center=arc.center,
                    p1=chord,
                    location=placement.circle_location(arc.center, arc.radius, index),
                )
            )
        return plans

    @staticmethod
    def _center_plans(center: Point3, radius: float, bbox: BBox) -> list[DimensionPlan]:
        length = max(radius * 3.0, min(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.06, 6.0)
        return [
            DimensionPlan(
                dim_type="centerline",
                layer="A-ANNO-CENTER",
                reason="hole centerline horizontal",
                p1=(center[0] - length, center[1], 0.0),
                p2=(center[0] + length, center[1], 0.0),
            ),
            DimensionPlan(
                dim_type="centerline",
                layer="A-ANNO-CENTER",
                reason="hole centerline vertical",
                p1=(center[0], center[1] - length, 0.0),
                p2=(center[0], center[1] + length, 0.0),
            ),
            DimensionPlan(
                dim_type="center_mark",
                layer="A-ANNO-CENTER",
                reason="hole center mark",
                center=center,
                radius=max(radius * 0.18, 1.0),
            ),
        ]


class DrawingReview:
    def review(self, session: Any) -> dict[str, Any]:
        counts: dict[str, int] = {}
        layers: dict[str, int] = {}
        for entity in session.iter_model_entities():
            object_name = _safe_str(entity, "ObjectName") or "unknown"
            layer = _safe_str(entity, "Layer") or "0"
            counts[object_name] = counts.get(object_name, 0) + 1
            layers[layer] = layers.get(layer, 0) + 1
        return {"entity_counts": counts, "layer_counts": layers}


class AutoCADAnnotationEngine:
    """Annotates a SolidWorks-exported DWG with native AutoCAD annotation objects."""

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = output_root
        self.skill_dir = skill_dir or (
            Path.home() / ".codex" / "skills" / "solidworks-automation" / "subskills" / "autocad-automation"
        )
        self.script_dir = self.skill_dir / "scripts"

    def annotate(self, source_dwg_path: str | Path, output_dir: str | Path | None = None) -> AnnotationResult:
        source = Path(source_dwg_path).resolve()
        if not source.exists():
            return AnnotationResult(False, str(source), errors=[f"Source DWG does not exist: {source}"])

        run_dir = (Path(output_dir) if output_dir else self.output_root / f"autocad_annotation_{datetime.now():%Y%m%d_%H%M%S}").resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        annotated_dwg = run_dir / f"{source.stem}_Annotated.dwg"
        pdf_path = run_dir / f"{source.stem}_Annotated.pdf"
        report_path = run_dir / "annotation_report.json"

        result = AnnotationResult(False, str(source), str(annotated_dwg), str(pdf_path), str(report_path))
        self._ensure_script_imports()
        import acad_session

        session = None
        original_connect = acad_session.connect_autocad
        acad_session.connect_autocad = AutoCADSkill._connect_autocad
        try:
            shutil.copy2(source, annotated_dwg)
            session = acad_session.AutoCADSession(create_if_missing=True, visible=True).connect()
            self._open_document_with_retry(session, annotated_dwg)
            self._bind_stable_active_document(session)
            session.activate_window()
            self._setup_layers(session)

            analysis = EntityAnalyzer().analyze(session)
            plans = DimensionStrategy().build(analysis)
            created = self._apply_plans(session, plans, result)
            result.entities_analyzed = len(analysis.entities)

            try:
                session.regen()
            except Exception as exc:
                result.errors.append(f"AutoCAD regen failed: {exc!r}")
            try:
                session.zoom_extents()
            except Exception as exc:
                result.errors.append(f"AutoCAD zoom extents failed: {exc!r}")
            dwg_saved = self._save_active_document(session, result)
            if annotated_dwg.exists():
                result.files.append(str(annotated_dwg))
            self._export_pdf_best_effort(session, pdf_path, result)

            try:
                review = DrawingReview().review(session)
            except Exception as exc:
                review = {"error": repr(exc)}
                result.errors.append(f"Drawing review failed: {exc!r}")
            payload = {
                "source_dwg": str(source),
                "annotated_dwg": str(annotated_dwg),
                "pdf_path": str(pdf_path) if pdf_path.exists() else None,
                "analysis": {
                    "bbox": analysis.bbox,
                    "counts": analysis.counts,
                    "view_regions": [
                        {
                            "name": region.name,
                            "bbox": region.bbox,
                            "role": region.role,
                            "entity_count": len(region.entities),
                            "layers": sorted({item.layer for item in region.entities}),
                        }
                        for region in analysis.view_regions
                    ],
                    "entities": [asdict(item) for item in analysis.entities],
                },
                "plans": [asdict(item) for item in plans],
                "created": created,
                "review": review,
                "errors": result.errors,
                "dwg_saved": dwg_saved,
            }
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            result.files.append(str(report_path))
            result.report_path = str(report_path)
            result.success = dwg_saved and annotated_dwg.exists() and annotated_dwg.stat().st_size > 0 and result.dimensions_created > 0
            return result
        except Exception as exc:
            result.errors.append(repr(exc))
            report_path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            result.files.append(str(report_path))
            return result
        finally:
            if session is not None:
                try:
                    session.close_document(save_changes=False)
                except Exception:
                    pass
            acad_session.connect_autocad = original_connect

    def _ensure_script_imports(self) -> None:
        script_dir = str(self.script_dir)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

    @staticmethod
    def _setup_layers(session: Any) -> None:
        for name, color, linetype in (
            ("A-ANNO-DIMS", 2, None),
            ("A-ANNO-CENTER", 1, "CENTER"),
            ("A-ANNO-TEXT", 3, None),
        ):
            try:
                layer = session.create_layer(name)
            except Exception:
                continue
            try:
                layer.Color = int(color)
            except Exception:
                pass
            if linetype:
                try:
                    session.active_document().Linetypes.Load(linetype, "acad.lin")
                    layer.Linetype = linetype
                except Exception:
                    pass

    @staticmethod
    def _bind_stable_active_document(session: Any, timeout_s: float = 8.0) -> None:
        deadline = time.time() + timeout_s
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                doc = session.app.ActiveDocument
                _ = doc.Name
                _ = doc.Layers.Count
                _ = doc.ModelSpace.Count
                session.doc = doc
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _open_document_with_retry(session: Any, path: Path, timeout_s: float = 30.0) -> None:
        deadline = time.time() + timeout_s
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                session.open_document(path, read_only=False)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.75)
        if last_error is not None:
            raise last_error

    def _apply_plans(self, session: Any, plans: Sequence[DimensionPlan], result: AnnotationResult) -> dict[str, int]:
        import acad_session

        created = {"dimension": 0, "centerline": 0, "center_mark": 0, "text_note": 0}
        model = session.model
        for plan in plans:
            try:
                entity = None
                if plan.dim_type == "rotated" and plan.p1 and plan.p2 and plan.location:
                    entity = model.AddDimRotated(
                        acad_session.acad_point(plan.p1),
                        acad_session.acad_point(plan.p2),
                        acad_session.acad_point(plan.location),
                        float(plan.angle),
                    )
                    created["dimension"] += 1
                    result.dimensions_created += 1
                elif plan.dim_type == "diametric" and plan.p1 and plan.p2:
                    try:
                        entity = model.AddDimDiametric(
                            acad_session.acad_point(plan.p1),
                            acad_session.acad_point(plan.p2),
                            0.0,
                        )
                    except Exception:
                        center = _midpoint(plan.p1, plan.p2)
                        entity = model.AddDimRadial(acad_session.acad_point(center), acad_session.acad_point(plan.p2), 0.0)
                    created["dimension"] += 1
                    result.dimensions_created += 1
                elif plan.dim_type == "radial" and plan.center and plan.p1:
                    entity = model.AddDimRadial(
                        acad_session.acad_point(plan.center),
                        acad_session.acad_point(plan.p1),
                        0.0,
                    )
                    created["dimension"] += 1
                    result.dimensions_created += 1
                elif plan.dim_type == "centerline" and plan.p1 and plan.p2:
                    entity = model.AddLine(acad_session.acad_point(plan.p1), acad_session.acad_point(plan.p2))
                    try:
                        entity.Layer = plan.layer
                    except Exception:
                        pass
                    created["centerline"] += 1
                    result.centerlines_created += 1
                elif plan.dim_type == "center_mark" and plan.center and plan.radius:
                    self._add_center_mark(session, plan.center, plan.radius, plan.layer)
                    created["center_mark"] += 1
                    result.center_marks_created += 1
                if entity is not None and plan.layer:
                    try:
                        entity.Layer = plan.layer
                    except Exception:
                        pass
            except Exception as exc:
                result.errors.append(f"{plan.dim_type} {plan.reason}: {exc!r}")
        return created

    @staticmethod
    def _save_active_document(session: Any, result: AnnotationResult) -> bool:
        target = str(Path(result.annotated_dwg or "").resolve()) if result.annotated_dwg else ""
        deadline = time.time() + 20.0
        last_messages: list[str] = []
        while time.time() < deadline:
            try:
                AutoCADAnnotationEngine._bind_stable_active_document(session, timeout_s=2.0)
            except Exception as exc:
                last_messages.append(f"DWG save bind failed: {exc!r}")
            docs = []
            for getter in (
                lambda: session.doc,
                lambda: session.app.ActiveDocument,
                lambda: session.active_document(),
            ):
                try:
                    doc = getter()
                    if doc is not None:
                        docs.append(doc)
                except Exception as exc:
                    last_messages.append(f"DWG save document getter failed: {exc!r}")
            for doc in docs:
                for save_call in (
                    lambda d=doc: d.Save(),
                    lambda d=doc: d.SaveAs(target),
                ):
                    try:
                        save_call()
                        return True
                    except Exception as exc:
                        last_messages.append(f"DWG save attempt failed: {exc!r}")
            time.sleep(1.0)
        result.errors.extend(last_messages[-6:])
        return False

    @staticmethod
    def _add_center_mark(session: Any, center: Point3, radius: float, layer: str) -> None:
        import acad_session

        x, y, _ = center
        for start, end in (
            ((x - radius, y, 0.0), (x + radius, y, 0.0)),
            ((x, y - radius, 0.0), (x, y + radius, 0.0)),
        ):
            entity = session.model.AddLine(acad_session.acad_point(start), acad_session.acad_point(end))
            try:
                entity.Layer = layer
            except Exception:
                pass

    @staticmethod
    def _export_pdf_best_effort(session: Any, pdf_path: Path, result: AnnotationResult) -> None:
        def _record_pdf() -> bool:
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                result.files.append(str(pdf_path))
                result.pdf_path = str(pdf_path)
                return True
            return False

        try:
            if pdf_path.exists():
                pdf_path.unlink()
            try:
                doc = session.app.ActiveDocument
            except Exception:
                doc = session.active_document()
            layout = doc.ActiveLayout
            try:
                layout.ConfigName = "DWG To PDF.pc3"
            except Exception:
                pass
            for attr, value in (
                ("PlotType", 1),
                ("UseStandardScale", True),
                ("StandardScale", 0),
                ("CenterPlot", True),
                ("PlotWithPlotStyles", True),
                ("StyleSheet", "monochrome.ctb"),
            ):
                try:
                    setattr(layout, attr, value)
                except Exception:
                    pass
            doc.Plot.QuietErrorMode = True
            try:
                session.delete_selection_set("CODEX_ANNOTATION_PDF")
                selection = doc.SelectionSets.Add("CODEX_ANNOTATION_PDF")
                selection.Select(5)
                doc.Export(str(pdf_path.with_suffix("")), "PDF", selection)
                try:
                    selection.Delete()
                except Exception:
                    pass
                if _record_pdf():
                    return
            except Exception as exc:
                result.errors.append(f"PDF export attempt failed: {exc!r}")
            plot_attempts = (
                lambda: doc.Plot.PlotToFile(str(pdf_path), "DWG To PDF.pc3"),
                lambda: doc.Plot.PlotToFile(str(pdf_path)),
            )
            for attempt in plot_attempts:
                try:
                    attempt()
                except Exception as exc:
                    result.errors.append(f"PDF plot attempt failed: {exc!r}")
                if _record_pdf():
                    return
            result.pdf_path = None
            result.errors.append("AutoCAD PlotToFile did not create PDF.")
        except Exception as exc:
            result.pdf_path = None
            result.errors.append(f"PDF export failed: {exc!r}")


def _detect_view_regions(entities: Sequence[EntityInfo], drawing_bbox: BBox | None) -> list[ViewRegion]:
    candidates = _candidate_model_entities(entities, drawing_bbox)
    if not candidates:
        return []
    clusters = _cluster_entities(candidates, drawing_bbox)
    regions: list[ViewRegion] = []
    for index, cluster in enumerate(clusters, start=1):
        bbox = _combine_bboxes(item.bbox for item in cluster)
        if bbox is None:
            continue
        if _bbox_width(bbox) < 1.0 and _bbox_height(bbox) < 1.0:
            continue
        regions.append(
            ViewRegion(
                name=f"view_{index}",
                bbox=bbox,
                entities=cluster,
                role=_classify_view_region(cluster, bbox),
            )
        )
    return _order_view_regions(regions)


def _candidate_model_entities(entities: Sequence[EntityInfo], drawing_bbox: BBox | None) -> list[EntityInfo]:
    geometric = [item for item in entities if item.bbox and item.kind in {"line", "circle", "arc", "polyline"}]
    if not geometric:
        return []

    preferred = [item for item in geometric if _is_model_layer(item.layer)]
    if len(preferred) >= 3:
        return preferred

    if drawing_bbox is None:
        return geometric

    sheet_width = max(1.0, _bbox_width(drawing_bbox))
    sheet_height = max(1.0, _bbox_height(drawing_bbox))
    filtered: list[EntityInfo] = []
    for item in geometric:
        bbox = item.bbox
        if bbox is None:
            continue
        width = _bbox_width(bbox)
        height = _bbox_height(bbox)
        touches_sheet_edge = (
            abs(bbox[0] - drawing_bbox[0]) <= 1e-4
            or abs(bbox[1] - drawing_bbox[1]) <= 1e-4
            or abs(bbox[2] - drawing_bbox[2]) <= 1e-4
            or abs(bbox[3] - drawing_bbox[3]) <= 1e-4
        )
        if touches_sheet_edge and (width > sheet_width * 0.35 or height > sheet_height * 0.35):
            continue
        if width > sheet_width * 0.75 or height > sheet_height * 0.75:
            continue
        filtered.append(item)
    return filtered


def _cluster_entities(entities: Sequence[EntityInfo], drawing_bbox: BBox | None) -> list[list[EntityInfo]]:
    if drawing_bbox:
        pad = max(8.0, min(_bbox_width(drawing_bbox), _bbox_height(drawing_bbox)) * 0.03)
    else:
        own_bbox = _combine_bboxes(item.bbox for item in entities)
        pad = max(8.0, min(_bbox_width(own_bbox), _bbox_height(own_bbox)) * 0.08) if own_bbox else 8.0

    clusters: list[list[EntityInfo]] = []
    cluster_boxes: list[BBox] = []
    for item in sorted(entities, key=lambda entry: (_bbox_center(entry.bbox or (0, 0, 0, 0))[1], _bbox_center(entry.bbox or (0, 0, 0, 0))[0])):
        if item.bbox is None:
            continue
        expanded = _expand_bbox(item.bbox, pad)
        match_index: int | None = None
        for index, cluster_box in enumerate(cluster_boxes):
            if _rects_overlap(expanded, _expand_bbox(cluster_box, pad)):
                match_index = index
                break
        if match_index is None:
            clusters.append([item])
            cluster_boxes.append(item.bbox)
        else:
            clusters[match_index].append(item)
            cluster_boxes[match_index] = _combine_bboxes([cluster_boxes[match_index], item.bbox]) or cluster_boxes[match_index]

    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            if merged:
                break
            for j in range(i + 1, len(clusters)):
                if _rects_overlap(_expand_bbox(cluster_boxes[i], pad), _expand_bbox(cluster_boxes[j], pad)):
                    clusters[i].extend(clusters[j])
                    cluster_boxes[i] = _combine_bboxes([cluster_boxes[i], cluster_boxes[j]]) or cluster_boxes[i]
                    del clusters[j]
                    del cluster_boxes[j]
                    merged = True
                    break
    return clusters


def _classify_view_region(entities: Sequence[EntityInfo], bbox: BBox) -> str:
    lines = [item for item in entities if item.kind == "line" and item.start and item.end]
    if not lines:
        return "orthographic"
    diagonal = 0
    for item in lines:
        assert item.start is not None and item.end is not None
        dx = abs(item.end[0] - item.start[0])
        dy = abs(item.end[1] - item.start[1])
        if dx > 1e-6 and dy > 1e-6:
            diagonal += 1
    if diagonal >= max(2, len(lines) * 0.35):
        return "isometric"
    if any(item.kind == "polyline" for item in entities) and diagonal >= 1:
        return "isometric"
    width = _bbox_width(bbox)
    height = _bbox_height(bbox)
    if width > height * 2.2:
        return "top"
    if height > width * 2.2:
        return "right"
    return "front"


def _order_view_regions(regions: Sequence[ViewRegion]) -> list[ViewRegion]:
    def key(region: ViewRegion) -> tuple[int, float, float, float]:
        role_rank = {"front": 0, "top": 1, "right": 2, "orthographic": 3, "isometric": 4}.get(region.role, 5)
        area = _bbox_width(region.bbox) * _bbox_height(region.bbox)
        center = _bbox_center(region.bbox)
        return (role_rank, -area, center[1], center[0])

    ordered: list[ViewRegion] = []
    for index, region in enumerate(sorted(regions, key=key), start=1):
        ordered.append(ViewRegion(name=f"{region.role}_{index}", bbox=region.bbox, entities=region.entities, role=region.role))
    return ordered


def _is_model_layer(layer: str) -> bool:
    value = (layer or "").strip().upper()
    return value == "0" or value.startswith("SLD") or value in {"MODEL", "OBJECT", "VISIBLE"}


def _entity_bbox(entity: Any) -> BBox | None:
    try:
        min_point, max_point = entity.GetBoundingBox()
        p1 = _point_tuple(min_point)
        p2 = _point_tuple(max_point)
        return (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))
    except Exception:
        return None


def _combine_bboxes(boxes: Iterable[BBox | None]) -> BBox | None:
    valid = [box for box in boxes if box is not None]
    if not valid:
        return None
    return (
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    )


def _bbox_width(bbox: BBox | None) -> float:
    if bbox is None:
        return 0.0
    return max(0.0, bbox[2] - bbox[0])


def _bbox_height(bbox: BBox | None) -> float:
    if bbox is None:
        return 0.0
    return max(0.0, bbox[3] - bbox[1])


def _bbox_center(bbox: BBox) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _expand_bbox(bbox: BBox, amount: float) -> BBox:
    return (bbox[0] - amount, bbox[1] - amount, bbox[2] + amount, bbox[3] + amount)


def _point_inside_bbox(point: Point3, bbox: BBox, tolerance: float = 0.0) -> bool:
    return (
        bbox[0] - tolerance <= point[0] <= bbox[2] + tolerance
        and bbox[1] - tolerance <= point[1] <= bbox[3] + tolerance
    )


def _point_attr(entity: Any, name: str) -> Point3 | None:
    try:
        return _point_tuple(getattr(entity, name))
    except Exception:
        return None


def _float_attr(entity: Any, name: str) -> float | None:
    try:
        return float(getattr(entity, name))
    except Exception:
        return None


def _safe_str(entity: Any, name: str) -> str:
    try:
        return str(getattr(entity, name))
    except Exception:
        return ""


def _point_tuple(value: Any) -> Point3:
    items = list(value)
    if len(items) == 2:
        items.append(0.0)
    return (float(items[0]), float(items[1]), float(items[2]))


def _polyline_points(entity: Any) -> list[Point3]:
    try:
        coords = list(entity.Coordinates)
    except Exception:
        return []
    step = 3 if len(coords) % 3 == 0 else 2
    points: list[Point3] = []
    for index in range(0, len(coords), step):
        chunk = coords[index : index + step]
        if len(chunk) >= 2:
            z = float(chunk[2]) if len(chunk) > 2 else 0.0
            points.append((float(chunk[0]), float(chunk[1]), z))
    return points


def _rects_overlap(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _midpoint(p1: Point3, p2: Point3) -> Point3:
    return ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0, (p1[2] + p2[2]) / 2.0)
