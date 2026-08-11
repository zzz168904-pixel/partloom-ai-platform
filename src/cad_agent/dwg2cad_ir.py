from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class DXFRecord:
    kind: str
    pairs: tuple[tuple[int, str], ...]

    def first(self, code: int, default: str = "") -> str:
        for pair_code, value in self.pairs:
            if pair_code == code:
                return value
        return default

    def values(self, code: int) -> list[str]:
        return [value for pair_code, value in self.pairs if pair_code == code]

    def number(self, code: int) -> float | None:
        value = self.first(code)
        if value == "":
            return None
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None

    def point(self, x_code: int) -> list[float] | None:
        x = self.number(x_code)
        y = self.number(x_code + 10)
        z = self.number(x_code + 20)
        if x is None or y is None:
            return None
        return [x, y, 0.0 if z is None else z]


def read_ascii_dxf(path: str | Path) -> dict[str, list[DXFRecord]]:
    source = Path(path).resolve()
    raw = source.read_bytes()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw.decode("gb18030")
    lines = decoded.splitlines()
    if len(lines) % 2:
        raise ValueError(f"DXF contains an odd number of group-code lines: {source}")
    pairs: list[tuple[int, str]] = []
    for index in range(0, len(lines), 2):
        try:
            code = int(lines[index].strip())
        except ValueError as exc:
            raise ValueError(f"Invalid DXF group code at line {index + 1}: {lines[index]!r}") from exc
        pairs.append((code, lines[index + 1].rstrip("\r\n")))

    sections: dict[str, list[DXFRecord]] = {}
    index = 0
    while index < len(pairs):
        code, value = pairs[index]
        if code != 0 or value != "SECTION":
            index += 1
            continue
        if index + 1 >= len(pairs) or pairs[index + 1][0] != 2:
            raise ValueError("DXF SECTION is missing its name.")
        section_name = pairs[index + 1][1]
        index += 2
        section_pairs: list[tuple[int, str]] = []
        while index < len(pairs) and pairs[index] != (0, "ENDSEC"):
            section_pairs.append(pairs[index])
            index += 1
        sections[section_name] = _records(section_pairs)
        index += 1
    return sections


def _records(pairs: list[tuple[int, str]]) -> list[DXFRecord]:
    records: list[DXFRecord] = []
    current_kind = ""
    current_pairs: list[tuple[int, str]] = []
    for code, value in pairs:
        if code == 0:
            if current_kind:
                records.append(DXFRecord(current_kind, tuple(current_pairs)))
            current_kind = value
            current_pairs = []
        elif current_kind:
            current_pairs.append((code, value))
    if current_kind:
        records.append(DXFRecord(current_kind, tuple(current_pairs)))
    return records


def _block_definitions(records: Iterable[DXFRecord]) -> dict[str, list[DXFRecord]]:
    blocks: dict[str, list[DXFRecord]] = {}
    current_name = ""
    current: list[DXFRecord] = []
    for record in records:
        if record.kind == "BLOCK":
            current_name = record.first(2)
            current = []
            continue
        if record.kind == "ENDBLK":
            if current_name:
                blocks[current_name] = current
            current_name = ""
            current = []
            continue
        if current_name:
            current.append(record)
    return blocks


_FONT_PREFIX = re.compile(r"\\f[^;]*;")
_FORMAT_CODES = re.compile(r"\\[ACFHQTW][^;]*;")


def normalize_dxf_text(value: str) -> str:
    text = str(value or "")
    text = _FONT_PREFIX.sub("", text)
    text = _FORMAT_CODES.sub("", text)
    replacements = {
        "{%%P}": "±",
        "%%P": "±",
        "{%%D}": "°",
        "%%D": "°",
        "%%C": "φ",
        "\\U+03c6": "φ",
        "\\U+03C6": "φ",
        "\\P": "\n",
        "{": "",
        "}": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.strip()


def _record_text(record: DXFRecord) -> str:
    if record.kind not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        return ""
    chunks = record.values(3)
    tail = record.first(1)
    if tail:
        chunks.append(tail)
    return normalize_dxf_text("".join(chunks))


def _block_texts(
    name: str,
    blocks: dict[str, list[DXFRecord]],
    *,
    seen: set[str] | None = None,
) -> list[str]:
    if not name:
        return []
    visited = set(seen or set())
    if name in visited:
        return []
    visited.add(name)
    result: list[str] = []
    for record in blocks.get(name, []):
        text = _record_text(record)
        if text:
            result.append(text)
        if record.kind == "INSERT":
            result.extend(_block_texts(record.first(2), blocks, seen=visited))
    return result


def _dimension_measurement(record: DXFRecord) -> float | None:
    stored = record.number(42)
    if stored is not None and stored >= 0.0:
        return stored
    first = record.point(13)
    second = record.point(14)
    if first is None or second is None:
        return None
    rotation_deg = record.number(50)
    if rotation_deg is None:
        return math.dist(first[:2], second[:2])
    rotation = math.radians(rotation_deg)
    delta_x = second[0] - first[0]
    delta_y = second[1] - first[1]
    return abs(delta_x * math.cos(rotation) + delta_y * math.sin(rotation))


def _dimension_record(record: DXFRecord, blocks: dict[str, list[DXFRecord]]) -> dict[str, Any]:
    block_name = record.first(2)
    override = normalize_dxf_text(record.first(1))
    block_texts = _block_texts(block_name, blocks)
    display_text = override or next((text for text in block_texts if text), "")
    subclasses = record.values(100)
    return {
        "handle": record.first(5),
        "layer": record.first(8),
        "kind": subclasses[-1] if subclasses else "AcDbDimension",
        "dimension_type": int(record.number(70) or 0) & 15,
        "block_name": block_name,
        "measurement_mm": _dimension_measurement(record),
        "display_text": display_text,
        "block_texts": block_texts,
        "definition_point_mm": record.point(10),
        "text_position_mm": record.point(11),
        "extension_point_1_mm": record.point(13),
        "extension_point_2_mm": record.point(14),
        "center_mm": record.point(15),
        "chord_point_mm": record.point(16),
        "rotation_deg": record.number(50),
    }


def _geometry_record(record: DXFRecord) -> dict[str, Any] | None:
    common = {
        "handle": record.first(5),
        "layer": record.first(8),
        "type": record.kind.lower(),
    }
    if record.kind == "LINE":
        return {**common, "start_mm": record.point(10), "end_mm": record.point(11)}
    if record.kind == "CIRCLE":
        return {**common, "center_mm": record.point(10), "radius_mm": record.number(40)}
    if record.kind == "ARC":
        return {
            **common,
            "center_mm": record.point(10),
            "radius_mm": record.number(40),
            "start_angle_deg": record.number(50),
            "end_angle_deg": record.number(51),
        }
    if record.kind in {"LWPOLYLINE", "POLYLINE"}:
        xs = record.values(10)
        ys = record.values(20)
        points = [[float(x), float(y), 0.0] for x, y in zip(xs, ys)]
        return {**common, "points_mm": points, "closed": bool(int(record.number(70) or 0) & 1)}
    return None


def geometry_endpoints(entity: dict[str, Any]) -> tuple[list[float], list[float]] | None:
    entity_type = str(entity.get("type") or "")
    if entity_type == "line":
        start = entity.get("start_mm")
        end = entity.get("end_mm")
        if isinstance(start, list) and isinstance(end, list):
            return start, end
    if entity_type == "arc":
        center = entity.get("center_mm")
        radius = entity.get("radius_mm")
        start_deg = entity.get("start_angle_deg")
        end_deg = entity.get("end_angle_deg")
        if (
            isinstance(center, list)
            and isinstance(radius, (int, float))
            and isinstance(start_deg, (int, float))
            and isinstance(end_deg, (int, float))
        ):
            start_angle = math.radians(float(start_deg))
            end_angle = math.radians(float(end_deg))
            return (
                [
                    float(center[0]) + float(radius) * math.cos(start_angle),
                    float(center[1]) + float(radius) * math.sin(start_angle),
                    float(center[2] if len(center) > 2 else 0.0),
                ],
                [
                    float(center[0]) + float(radius) * math.cos(end_angle),
                    float(center[1]) + float(radius) * math.sin(end_angle),
                    float(center[2] if len(center) > 2 else 0.0),
                ],
            )
    return None


def connected_profile_components(
    geometry: Iterable[dict[str, Any]],
    *,
    layer: str = "CAXA0",
    bbox_mm: tuple[float, float, float, float] | None = None,
    tolerance_mm: float = 0.02,
) -> list[dict[str, Any]]:
    edges: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    points: dict[tuple[int, int], list[float]] = {}
    source_by_handle: dict[str, dict[str, Any]] = {}

    def key(point: list[float]) -> tuple[int, int]:
        return (
            round(float(point[0]) / tolerance_mm),
            round(float(point[1]) / tolerance_mm),
        )

    for entity in geometry:
        if str(entity.get("layer") or "") != layer:
            continue
        endpoints = geometry_endpoints(entity)
        if endpoints is None:
            continue
        start, end = endpoints
        if bbox_mm is not None:
            min_x, min_y, max_x, max_y = bbox_mm
            if not all(
                min_x - tolerance_mm <= point[0] <= max_x + tolerance_mm
                and min_y - tolerance_mm <= point[1] <= max_y + tolerance_mm
                for point in (start, end)
            ):
                continue
        start_key = key(start)
        end_key = key(end)
        handle = str(entity.get("handle") or f"edge_{len(edges) + 1}")
        points.setdefault(start_key, start)
        points.setdefault(end_key, end)
        edges.append((handle, start_key, end_key))
        source_by_handle[handle] = entity

    adjacency: dict[tuple[int, int], list[tuple[tuple[int, int], str]]] = {}
    for handle, start, end in edges:
        adjacency.setdefault(start, []).append((end, handle))
        adjacency.setdefault(end, []).append((start, handle))

    components: list[dict[str, Any]] = []
    remaining = set(adjacency)
    while remaining:
        seed = next(iter(remaining))
        stack = [seed]
        nodes: set[tuple[int, int]] = set()
        handles: set[str] = set()
        while stack:
            node = stack.pop()
            if node in nodes:
                continue
            nodes.add(node)
            remaining.discard(node)
            for neighbor, handle in adjacency.get(node, []):
                handles.add(handle)
                if neighbor not in nodes:
                    stack.append(neighbor)
        coordinates = [points[node] for node in nodes]
        degrees = [len(adjacency.get(node, [])) for node in nodes]
        edge_count = len(handles)
        components.append(
            {
                "handles": sorted(handles),
                "entities": [source_by_handle[handle] for handle in sorted(handles)],
                "node_count": len(nodes),
                "edge_count": edge_count,
                "cycle_rank": max(0, edge_count - len(nodes) + 1),
                "all_nodes_degree_two": bool(degrees) and all(value == 2 for value in degrees),
                "bbox_mm": [
                    min(point[0] for point in coordinates),
                    min(point[1] for point in coordinates),
                    max(point[0] for point in coordinates),
                    max(point[1] for point in coordinates),
                ],
            }
        )
    return sorted(components, key=lambda item: (-item["cycle_rank"], -item["edge_count"], item["bbox_mm"]))


def profile_cycle_basis(component: dict[str, Any], tolerance_mm: float = 0.02) -> list[list[str]]:
    entities = list(component.get("entities") or [])

    def key(point: list[float]) -> tuple[int, int]:
        return (
            round(float(point[0]) / tolerance_mm),
            round(float(point[1]) / tolerance_mm),
        )

    parent: dict[tuple[int, int], tuple[int, int]] = {}
    tree: dict[tuple[int, int], list[tuple[tuple[int, int], str]]] = {}

    def find(node: tuple[int, int]) -> tuple[int, int]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(first: tuple[int, int], second: tuple[int, int]) -> bool:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return False
        parent[second_root] = first_root
        return True

    def tree_path(start: tuple[int, int], goal: tuple[int, int]) -> list[str]:
        stack: list[tuple[tuple[int, int], list[str]]] = [(start, [])]
        seen: set[tuple[int, int]] = set()
        while stack:
            node, handles = stack.pop()
            if node == goal:
                return handles
            if node in seen:
                continue
            seen.add(node)
            for neighbor, handle in tree.get(node, []):
                if neighbor not in seen:
                    stack.append((neighbor, [*handles, handle]))
        return []

    cycles: list[list[str]] = []
    for entity in entities:
        endpoints = geometry_endpoints(entity)
        if endpoints is None:
            continue
        start = key(endpoints[0])
        end = key(endpoints[1])
        handle = str(entity.get("handle") or "")
        if union(start, end):
            tree.setdefault(start, []).append((end, handle))
            tree.setdefault(end, []).append((start, handle))
            continue
        path = tree_path(start, end)
        if path:
            cycles.append([*path, handle])
    return cycles


def extract_drawing_ir(dxf_path: str | Path, source_dwg: str | Path | None = None) -> dict[str, Any]:
    source = Path(dxf_path).resolve()
    sections = read_ascii_dxf(source)
    blocks = _block_definitions(sections.get("BLOCKS", []))
    entities = sections.get("ENTITIES", [])
    geometry = [item for record in entities if (item := _geometry_record(record)) is not None]
    dimensions = [_dimension_record(record, blocks) for record in entities if record.kind == "DIMENSION"]

    texts: list[dict[str, Any]] = []
    for record in entities:
        text = _record_text(record)
        if text:
            texts.append(
                {
                    "handle": record.first(5),
                    "layer": record.first(8),
                    "type": record.kind.lower(),
                    "text": text,
                    "insertion_point_mm": record.point(10),
                }
            )
        if record.kind == "INSERT":
            for nested_text in _block_texts(record.first(2), blocks):
                texts.append(
                    {
                        "handle": record.first(5),
                        "layer": record.first(8),
                        "type": "block_text",
                        "block_name": record.first(2),
                        "text": nested_text,
                        "insertion_point_mm": record.point(10),
                    }
                )

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "version": "drawing.ir.v1",
        "source": {
            "dwg_path": str(Path(source_dwg).resolve()) if source_dwg else "",
            "dxf_evidence_path": str(source),
            "dxf_sha256": digest,
            "units": "mm",
        },
        "geometry": geometry,
        "dimensions": dimensions,
        "texts": texts,
        "statistics": {
            "geometry_count": len(geometry),
            "dimension_count": len(dimensions),
            "text_count": len(texts),
            "block_definition_count": len(blocks),
        },
    }


def _translated_segment(
    entity: dict[str, Any],
    origin_x: float,
    axis_y: float,
    *,
    reverse: bool = False,
) -> dict[str, Any]:
    endpoints = geometry_endpoints(entity)
    if endpoints is None:
        raise ValueError(f"Entity does not define a line/arc segment: {entity.get('handle')}")
    start, end = endpoints
    if reverse:
        start, end = end, start

    def local(point: list[float]) -> list[float]:
        return [
            round(float(point[0]) - origin_x, 9),
            round(float(point[1]) - axis_y, 9),
        ]

    if entity.get("type") == "line":
        return {"type": "line", "start_mm": local(start), "end_mm": local(end)}
    center = entity.get("center_mm")
    if not isinstance(center, list):
        raise ValueError(f"Arc is missing its center: {entity.get('handle')}")
    return {
        "type": "arc",
        "center_mm": local(center),
        "start_mm": local(start),
        "end_mm": local(end),
        "direction": "cw" if reverse else "ccw",
    }


def _graphic_blade_count(drawing_ir: dict[str, Any]) -> tuple[int, list[str]]:
    circles = [
        entity
        for entity in drawing_ir.get("geometry", [])
        if entity.get("type") == "circle" and isinstance(entity.get("radius_mm"), (int, float))
    ]
    if not circles:
        return 0, []
    outer = max(circles, key=lambda entity: float(entity["radius_mm"]))
    center = outer.get("center_mm")
    if not isinstance(center, list):
        return 0, []
    candidates: list[dict[str, Any]] = []
    for entity in drawing_ir.get("geometry", []):
        if entity.get("type") != "arc":
            continue
        arc_center = entity.get("center_mm")
        radius = entity.get("radius_mm")
        if not isinstance(arc_center, list) or not isinstance(radius, (int, float)):
            continue
        if math.dist(center[:2], arc_center[:2]) <= 0.02 and abs(float(radius) - 89.0) <= 0.02:
            candidates.append(entity)
    return len(candidates), [str(entity.get("handle") or "") for entity in candidates]


def _noted_blade_count(drawing_ir: dict[str, Any]) -> tuple[int, str]:
    for item in drawing_ir.get("texts", []):
        text = str(item.get("text") or "")
        match = re.search(r"叶片\s*(\d+)\s*片均布", text)
        if match:
            return int(match.group(1)), str(item.get("handle") or "")
    return 0, ""


def _dimension_summary(drawing_ir: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in drawing_ir.get("dimensions", []):
        display = str(item.get("display_text") or "")
        measurement = item.get("measurement_mm")
        result.append(
            {
                "handle": item.get("handle"),
                "display_text": display,
                "measurement_mm": round(float(measurement), 6)
                if isinstance(measurement, (int, float))
                else None,
                "block_texts": item.get("block_texts") or [],
            }
        )
    return result


def build_impeller_cad_ir(
    drawing_ir: dict[str, Any],
    *,
    selected_blade_count: int | None = None,
    decision_source: str = "",
) -> dict[str, Any]:
    geometry = {
        str(entity.get("handle") or ""): entity
        for entity in drawing_ir.get("geometry", [])
        if entity.get("handle")
    }
    circles = [
        entity
        for entity in drawing_ir.get("geometry", [])
        if entity.get("type") == "circle" and isinstance(entity.get("radius_mm"), (int, float))
    ]
    if not circles:
        raise ValueError("The drawing does not contain a usable impeller front-view circle.")
    outer_circle = max(circles, key=lambda entity: float(entity["radius_mm"]))
    front_center = outer_circle.get("center_mm")
    if not isinstance(front_center, list):
        raise ValueError("The impeller front-view center is missing.")
    axis_y = float(front_center[1])

    positive_endpoints = [
        point
        for entity in drawing_ir.get("geometry", [])
        if str(entity.get("layer") or "") == "CAXA0"
        for point in (geometry_endpoints(entity) or ())
        if float(point[0]) > 0.0
    ]
    if not positive_endpoints:
        raise ValueError("The drawing does not contain a usable positive-X sectional view.")
    origin_x = min(float(point[0]) for point in positive_endpoints)

    component_handles: list[tuple[str, bool]] = [
        ("129", False),
        ("12A", False),
        ("108", True),
        ("10A", True),
        ("10C", True),
        ("10E", False),
        ("10D", False),
        ("10F", False),
        ("10B", False),
        ("128", True),
    ]
    missing = [handle for handle, _reverse in component_handles if handle not in geometry]
    profile_segments: list[dict[str, Any]] = []
    component_evidence: list[str] = []
    if not missing:
        for index, (handle, reverse) in enumerate(component_handles):
            if index == 5:
                previous_end = profile_segments[-1]["end_mm"]
                next_start = _translated_segment(geometry[handle], origin_x, axis_y)["start_mm"]
                profile_segments.append(
                    {
                        "type": "line",
                        "start_mm": previous_end,
                        "end_mm": next_start,
                    }
                )
                component_evidence.extend(["100", "121"])
            profile_segments.append(
                _translated_segment(geometry[handle], origin_x, axis_y, reverse=reverse)
            )
            component_evidence.append(handle)

    graphic_count, graphic_handles = _graphic_blade_count(drawing_ir)
    note_count, note_handle = _noted_blade_count(drawing_ir)
    conflict = bool(graphic_count and note_count and graphic_count != note_count)
    if selected_blade_count is not None and selected_blade_count < 2:
        raise ValueError("selected_blade_count must be at least 2.")

    top_blade_handles: list[tuple[str, bool]] = [
        ("81", False),
        ("80", True),
        ("83", True),
        ("84", False),
        ("82", False),
    ]
    blade_segments: list[dict[str, Any]] = []
    blade_missing = [handle for handle, _reverse in top_blade_handles if handle not in geometry]
    if not blade_missing:
        for index, (handle, reverse) in enumerate(top_blade_handles):
            if index == 4:
                previous_end = blade_segments[-1]["end_mm"]
                next_start = _translated_segment(
                    geometry[handle],
                    float(front_center[0]),
                    axis_y,
                    reverse=reverse,
                )["start_mm"]
                blade_segments.append(
                    {
                        "type": "arc",
                        "center_mm": [0.0, 0.0],
                        "start_mm": previous_end,
                        "end_mm": next_start,
                        "direction": "ccw",
                    }
                )
            blade_segments.append(
                _translated_segment(
                    geometry[handle],
                    float(front_center[0]),
                    axis_y,
                    reverse=reverse,
                )
            )

    features: list[dict[str, Any]] = []
    if profile_segments:
        features.append(
            {
                "id": "section_profile_component_a",
                "name": "SectionProfileComponentA",
                "operation": "revolve",
                "required": True,
                "parameters": {
                    "body_operation": "base",
                    "execution_mode": "new_model",
                    "sketch_plane": "front",
                    "axis": "horizontal",
                    "angle_deg": 360.0,
                    "reverse_direction": False,
                    "profile_segments": profile_segments,
                },
                "target": {
                    "resolution": "new_body",
                    "body_ref": "primary_solid",
                },
                "dependencies": [],
                "evidence": [
                    f"DWG section-view handles: {', '.join(component_evidence)}",
                    "Coordinates are translated from the native 1:1 millimetre section view without raster tracing.",
                ],
                "assumptions": [],
                "unresolved": [],
                "confidence": 1.0,
            }
        )

    if selected_blade_count is not None and blade_segments and profile_segments:
        features.extend(
            [
                {
                    "id": "seed_blade",
                    "name": "SeedBlade_DWGProfile",
                    "operation": "profile_extrude",
                    "required": True,
                    "parameters": {
                        "body_operation": "boss",
                        "mode": "active_model",
                        "sketch_plane": "right",
                        "end_condition": "blind",
                        "depth_mm": 6.0,
                        "start_offset_mm": 37.0,
                        "reverse_direction": False,
                        "merge_result": True,
                        "profiles": [
                            {
                                "role": "outer",
                                "segments": blade_segments,
                            }
                        ],
                    },
                    "target": {
                        "resolution": "semantic_reference",
                        "body_ref": "primary_solid",
                        "feature_ref": "section_profile_component_a",
                        "face_role": "revolved_solid",
                    },
                    "dependencies": [
                        {
                            "kind": "feature",
                            "feature_id": "section_profile_component_a",
                        }
                    ],
                    "evidence": [
                        f"DWG blade-profile handles: {', '.join(handle for handle, _reverse in top_blade_handles)}",
                        "Blind depth 6 mm is taken from the native axial dimension between the adjacent 4.5 mm shroud dimensions.",
                        "Start offset 37 mm is the native section-view distance from x=58.120003895 to x=95.120003895.",
                    ],
                    "assumptions": [],
                    "unresolved": [],
                    "confidence": 1.0,
                },
                {
                    "id": "blade_pattern",
                    "name": f"BladePattern_{selected_blade_count}x",
                    "operation": "circular_pattern",
                    "required": True,
                    "parameters": {
                        "seed_features": ["seed_blade"],
                        "count": selected_blade_count,
                        "total_angle_deg": 360.0,
                        "axis": "x",
                        "equal_spacing": True,
                        "geometry_pattern": True,
                        "reverse": False,
                    },
                    "target": {
                        "resolution": "semantic_reference",
                        "body_ref": "primary_solid",
                        "feature_ref": "seed_blade",
                        "face_role": "seed_features",
                    },
                    "dependencies": [
                        {
                            "kind": "feature",
                            "feature_id": "seed_blade",
                        }
                    ],
                    "evidence": [
                        f"Selected blade count: {selected_blade_count} ({decision_source or 'explicit conversion input'}).",
                        f"Technical note handle {note_handle} specifies {note_count} equally spaced blades.",
                    ],
                    "assumptions": [],
                    "unresolved": [],
                    "confidence": 1.0,
                },
            ]
        )

    section_unresolved = (
        "The sectional view contains overlapping half-section geometry that has not yet been "
        "deterministically segmented into hub, front shroud, rear shroud, and bore."
    )
    remaining_unresolved = (
        "The complete revolved body, keyed bore, final fillets, and 1x45-degree unspecified "
        "chamfers remain outside this conversion stage."
    )
    unresolved = [section_unresolved, remaining_unresolved]
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if conflict and selected_blade_count is None:
        message = (
            f"Blade-count conflict: vector geometry contains {graphic_count} equally spaced blade-tip arcs "
            f"but technical note handle {note_handle} specifies {note_count} blades."
        )
        unresolved.insert(0, message)
        errors.append(
            {
                "code": "drawing_conflict_blade_count",
                "message": message,
                "graphic_value": graphic_count,
                "note_value": note_count,
                "graphic_handles": graphic_handles,
                "note_handle": note_handle,
            }
        )
    elif conflict:
        warnings.append(
            {
                "code": "drawing_conflict_blade_count_resolved",
                "message": (
                    f"Blade-count conflict resolved to {selected_blade_count}: vector geometry contains "
                    f"{graphic_count} blade-tip arcs while technical note handle {note_handle} "
                    f"specifies {note_count} blades."
                ),
                "graphic_value": graphic_count,
                "note_value": note_count,
                "selected_value": selected_blade_count,
                "graphic_handles": graphic_handles,
                "note_handle": note_handle,
                "decision_source": decision_source or "explicit_conversion_input",
            }
        )
    if missing:
        errors.append(
            {
                "code": "section_profile_evidence_missing",
                "message": f"Required section profile handles are missing: {missing}",
            }
        )
    if blade_missing:
        errors.append(
            {
                "code": "blade_profile_evidence_missing",
                "message": f"Required blade profile handles are missing: {blade_missing}",
            }
        )

    return {
        "version": "cad.ir.v1",
        "valid": False,
        "unit_system": "mm",
        "task_type": "model_3d",
        "part_type": "radial_impeller",
        "outputs": ["SLDPRT"],
        "parameters": {
            "unit": "mm",
            "drawing_number": "NK0350",
            "material": "1Cr18Ni9Ti",
            "outer_diameter_mm": 226.0,
            "secondary_diameter_mm": 180.0,
            "overall_length_mm": 71.0,
            "shaft_bore_diameter_mm": 30.0,
            "keyway_width_mm": 8.0,
            "graphic_blade_count": graphic_count,
            "noted_blade_count": note_count,
            "selected_blade_count": selected_blade_count,
        },
        "source_drawing": {
            **dict(drawing_ir.get("source") or {}),
            "drawing_ir_version": drawing_ir.get("version"),
            "front_view_center_mm": front_center,
            "section_axis_y_mm": axis_y,
            "section_axial_origin_x_mm": origin_x,
            "dimensions": _dimension_summary(drawing_ir),
            "interpretation_decisions": [
                {
                    "field": "blade_count",
                    "selected_value": selected_blade_count,
                    "decision_source": decision_source or "explicit_conversion_input",
                    "graphic_value": graphic_count,
                    "note_value": note_count,
                }
            ]
            if selected_blade_count is not None
            else [],
        },
        "reconstruction_scope": {
            "classification": "drawing_derived_stage_1",
            "full_part_parity": False,
            "claim": "Only one deterministic closed sectional component is emitted as executable feature geometry; the full impeller is blocked.",
        },
        "reconstruction_contract": {
            "delivery_level": "source_parity",
            "approximation_allowed": False,
            "required_operation_counts": {
                "revolve": 2,
                "profile_extrude": 1,
                "circular_pattern": 1,
                "chamfer": 1,
            },
            "required_feature_ids": [
                "section_profile_component_a",
                "complete_axisymmetric_body",
                "seed_blade",
                "blade_pattern",
                "unspecified_edge_chamfers",
            ],
            "minimum_feature_count": 5,
            "block_on_gap": True,
        },
        "features": features,
        "recognized_feature_candidates": {
            "seed_blade": {
                "profile_segments": blade_segments,
                "source_handles": [handle for handle, _reverse in top_blade_handles],
                "root_arc_basis": "R27.5 native hub arc",
                "tip_arc_radius_mm": 89.0,
                "root_fillet_radius_mm": 5.0,
                "blade_thickness_in_view_mm": 8.0,
                "candidate_pattern_counts": sorted({value for value in (graphic_count, note_count) if value}),
                "selected_pattern_count": selected_blade_count,
                "pattern_axis": "x",
                "pattern_total_angle_deg": 360.0,
                "axial_start_offset_mm": 37.0,
                "axial_depth_mm": 6.0,
                "execution_ready": bool(
                    selected_blade_count is not None and blade_segments and profile_segments
                ),
            },
            "section_dimensions": {
                "diameters_mm": [226.0, 180.0, 66.4, 52.0, 48.0, 40.0, 30.0],
                "axial_dimensions_mm": [71.0, 35.0, 31.0, 16.0, 7.5, 6.0, 4.5, 4.5],
                "fillet_radii_mm": [9.0, 4.0],
                "unspecified_chamfer": "1x45deg",
            },
        },
        "errors": errors,
        "warnings": warnings,
        "unsupported_features": (
            [
                {
                    "type": "drawing_semantic_conflict",
                    "required": True,
                    "reason": unresolved[0],
                }
            ]
            if conflict and selected_blade_count is None
            else []
        )
        + [
            {
                "type": "incomplete_section_segmentation",
                "required": True,
                "reason": section_unresolved,
            },
        ],
        "assumptions": [],
        "unresolved": unresolved,
    }
