from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


BODY_CONTEXT = ("整体尺寸", "外形尺寸", "主体尺寸", "外形", "overall", "body size")
MULTIPLY = r"[xX×*]"


@dataclass(frozen=True)
class ParsedDimension:
    value: float
    unit: str
    source_text: str
    confidence: float
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source_text": self.source_text,
            "confidence": self.confidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class BodyDimensionParse:
    dimensions: dict[str, ParsedDimension]
    risks: list[str]
    unsupported_features: list[dict[str, str]]

    @property
    def ok(self) -> bool:
        return {"length", "width", "thickness"} <= set(self.dimensions)

    def numeric_parameters(self) -> dict[str, float | str]:
        if not self.ok:
            return {"unit": "mm"}
        return {
            "unit": self.dimensions["length"].unit,
            "length": self.dimensions["length"].value,
            "width": self.dimensions["width"].value,
            "thickness": self.dimensions["thickness"].value,
        }

    def details(self) -> dict[str, dict[str, Any]]:
        return {key: value.as_dict() for key, value in self.dimensions.items()}


def parse_body_dimensions(text: str) -> BodyDimensionParse:
    """Parse main body dimensions without stealing feature dimensions."""

    candidates: list[dict[str, Any]] = []
    candidates.extend(_parse_chinese_labeled(text))
    candidates.extend(_parse_lwh(text))
    candidates.extend(_parse_triplets(text))
    candidates = _dedupe_candidates(candidates)
    if not candidates:
        return _missing_result()

    context_candidates = [item for item in candidates if item["has_body_context"]]
    preferred = context_candidates or candidates
    best_score = max(item["score"] for item in preferred)
    best = [item for item in preferred if item["score"] == best_score]

    if len(best) > 1 and _distinct_values(best):
        return BodyDimensionParse(
            dimensions={},
            risks=["主体尺寸存在多组候选，无法安全判断，请用户确认 length/width/thickness。"],
            unsupported_features=[
                {
                    "type": "ambiguous_body_dimensions",
                    "reason": "Multiple body-size candidates were found: "
                    + "; ".join(str(item["source_text"]) for item in best),
                }
            ],
        )
    chosen = best[0]
    return BodyDimensionParse(
        dimensions={
            "length": ParsedDimension(chosen["length"], chosen["unit"], chosen["length_source"], chosen["confidence"], chosen["source"]),
            "width": ParsedDimension(chosen["width"], chosen["unit"], chosen["width_source"], chosen["confidence"], chosen["source"]),
            "thickness": ParsedDimension(chosen["thickness"], chosen["unit"], chosen["thickness_source"], chosen["confidence"], chosen["source"]),
        },
        risks=[],
        unsupported_features=[],
    )


def _parse_chinese_labeled(text: str) -> list[dict[str, Any]]:
    windows = _candidate_windows(text)
    candidates: list[dict[str, Any]] = []
    for window, offset, has_context in windows:
        labels = {}
        for key, label in (("length", "长"), ("width", "宽"), ("thickness", "厚")):
            match = re.search(label + r"\s*(\d+(?:\.\d+)?)\s*(mm|毫米)?", window, flags=re.IGNORECASE)
            if match:
                labels[key] = {
                    "value": float(match.group(1)),
                    "source": match.group(0),
                    "start": offset + match.start(),
                }
        if {"length", "width", "thickness"} <= set(labels):
            source_text = _span_text(text, min(item["start"] for item in labels.values()), max(item["start"] + len(item["source"]) for item in labels.values()))
            candidates.append(
                _candidate(
                    labels["length"]["value"],
                    labels["width"]["value"],
                    labels["thickness"]["value"],
                    "mm",
                    labels["length"]["source"],
                    labels["width"]["source"],
                    labels["thickness"]["source"],
                    source_text,
                    "chinese_labeled_dimensions",
                    0.99 if has_context else 0.94,
                    has_context,
                )
            )
    return candidates


def _parse_lwh(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"\bL\s*(\d+(?:\.\d+)?)\s*(mm)?\s*[,;，\s]*W\s*(\d+(?:\.\d+)?)\s*(mm)?\s*[,;，\s]*H\s*(\d+(?:\.\d+)?)\s*(mm)?\b",
        flags=re.IGNORECASE,
    )
    candidates = []
    for match in pattern.finditer(text):
        candidates.append(
            _candidate(
                float(match.group(1)),
                float(match.group(3)),
                float(match.group(5)),
                "mm",
                match.group(0).split()[0] if match.group(0).split() else f"L{match.group(1)}",
                f"W{match.group(3)}",
                f"H{match.group(5)}",
                match.group(0),
                "lwh_labeled_dimensions",
                0.97,
                _has_context_near(text, match.start()),
            )
        )
    return candidates


def _parse_triplets(text: str) -> list[dict[str, Any]]:
    number = r"(\d+(?:\.\d+)?)\s*(?:mm|毫米)?"
    pattern = re.compile(number + r"\s*" + MULTIPLY + r"\s*" + number + r"\s*" + MULTIPLY + r"\s*" + number + r"\s*(?:mm|毫米)?", re.IGNORECASE)
    candidates = []
    for match in pattern.finditer(text):
        if _is_feature_context(text, match.start()):
            continue
        has_context = _has_context_near(text, match.start())
        confidence = 0.96 if has_context else 0.86
        candidates.append(
            _candidate(
                float(match.group(1)),
                float(match.group(2)),
                float(match.group(3)),
                "mm",
                match.group(1),
                match.group(2),
                match.group(3),
                match.group(0),
                "triplet_dimensions",
                confidence,
                has_context,
            )
        )
    return candidates


def _candidate_windows(text: str) -> list[tuple[str, int, bool]]:
    windows = [(text, 0, False)]
    for marker in BODY_CONTEXT:
        start = text.lower().find(marker.lower())
        if start >= 0:
            windows.insert(0, (text[start : start + 80], start, True))
    return windows


def _candidate(
    length: float,
    width: float,
    thickness: float,
    unit: str,
    length_source: str,
    width_source: str,
    thickness_source: str,
    source_text: str,
    source: str,
    confidence: float,
    has_context: bool,
) -> dict[str, Any]:
    score = confidence + (0.2 if has_context else 0.0)
    return {
        "length": length,
        "width": width,
        "thickness": thickness,
        "unit": unit,
        "length_source": length_source,
        "width_source": width_source,
        "thickness_source": thickness_source,
        "source_text": source_text,
        "source": source,
        "confidence": confidence,
        "has_body_context": has_context,
        "score": score,
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in candidates:
        key = (item["length"], item["width"], item["thickness"], item["source"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _distinct_values(candidates: list[dict[str, Any]]) -> bool:
    values = {(item["length"], item["width"], item["thickness"]) for item in candidates}
    return len(values) > 1


def _missing_result() -> BodyDimensionParse:
    return BodyDimensionParse(
        dimensions={},
        risks=["未能识别明确主体长宽厚尺寸，不能安全猜测主体尺寸。"],
        unsupported_features=[{"type": "missing_body_dimensions", "reason": "No unambiguous length/width/thickness expression was found."}],
    )


def _has_context_near(text: str, start: int) -> bool:
    before = text[max(0, start - 24) : start].lower()
    return any(marker.lower() in before for marker in BODY_CONTEXT)


def _is_feature_context(text: str, start: int) -> bool:
    before = text[max(0, start - 12) : start].lower()
    return any(token in before for token in ("r", "φ", "Φ", "pcd", "m", "距边", "孔", "圆角"))


def _span_text(text: str, start: int, end: int) -> str:
    return text[max(0, start) : min(len(text), end)]
