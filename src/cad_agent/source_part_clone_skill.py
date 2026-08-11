from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .model_integrity import SolidWorksModelInspector
from .models import SkillResult


class SourcePartCloneSkill:
    """Create an independent native SLDPRT copy from an explicit source Part."""

    key = "source_part_clone"
    name = "SolidWorks Source Part Clone"

    def __init__(self, output_root: Path, skill_dir: Path | None = None) -> None:
        self.output_root = Path(output_root)
        self.skill_dir = skill_dir or (Path.home() / ".codex" / "skills" / "solidworks-automation")
        self.skill_script_dir = self.skill_dir / "scripts"

    @staticmethod
    def normalize_request(params: dict[str, Any]) -> dict[str, Any]:
        source_text = str(params.get("source_path") or "").strip()
        source = Path(source_text).expanduser() if source_text else None
        expected_sha = str(params.get("source_sha256") or "").strip().lower()
        errors: list[str] = []
        if source is None:
            errors.append("source_path is required")
        elif source.suffix.lower() != ".sldprt":
            errors.append("source_path must reference one SLDPRT file")
        if not expected_sha:
            errors.append("source_sha256 is required")
        elif len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
            errors.append("source_sha256 must be a 64-character hexadecimal digest")
        return {
            "success": not errors,
            "message": "; ".join(errors),
            "source_path": str(source.resolve()) if source is not None and source.is_absolute() else source_text,
            "source_sha256": expected_sha,
            "expected_solid_body_count": cls_int(params.get("expected_solid_body_count")),
            "expected_feature_count": cls_int(params.get("expected_feature_count")),
            "expected_volume_m3": cls_float(params.get("expected_volume_m3")),
            "expected_bounding_box_mm": params.get("expected_bounding_box_mm"),
        }

    def run_plan(self, plan: dict[str, Any]) -> SkillResult:
        features = [
            feature
            for feature in list(plan.get("features") or [])
            if str(feature.get("type") or "") == self.key
        ]
        if len(features) != 1:
            return SkillResult(False, "source_part_clone requires exactly one source Part feature.")
        request = self.normalize_request(dict(features[0].get("params") or {}))
        if not request["success"]:
            return SkillResult(False, request["message"], data=request)

        source = Path(request["source_path"]).resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            return SkillResult(False, f"Source SLDPRT does not exist or is empty: {source}")
        actual_sha = self._sha256(source)
        if actual_sha != request["source_sha256"]:
            return SkillResult(
                False,
                "Source SLDPRT SHA-256 does not match the CAD-IR contract.",
                data={
                    "source_path": str(source),
                    "expected_sha256": request["source_sha256"],
                    "actual_sha256": actual_sha,
                },
            )

        target_value = str(plan.get("execution_model_path") or "").strip()
        if not target_value:
            target_dir = self.output_root / f"source_part_clone_{datetime.now():%Y%m%d_%H%M%S_%f}"
            target_value = str(target_dir / f"{source.stem}_Clone.SLDPRT")
        target = Path(target_value).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target == source:
            return SkillResult(False, "Source and target SLDPRT paths must be different.")

        report_dir = self.output_root / f"source_part_clone_{datetime.now():%Y%m%d_%H%M%S_%f}"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "source_part_clone_report.json"
        session = None
        model = None
        source_previously_open = False
        try:
            self._ensure_runtime()
            from sw_session import SolidWorksSession

            session = SolidWorksSession(wait_seconds=10, visible=True)
            initial = session.active_doc
            initial_path = self._document_path(initial)
            source_previously_open = bool(
                initial_path and initial_path.casefold() == str(source).casefold()
            )
            model = self._open_part_dynamic(session, source, read_only=True)
            if model is None or self._com_int(model, "GetType", 0) != 1:
                raise RuntimeError("SolidWorks did not open the source as a Part document.")

            inspector = SolidWorksModelInspector()
            source_state = inspector.inspect(model)
            source_bbox = inspector.exact_body_bbox(model)
            source_ordered_feature_count = self._ordered_feature_count(model)
            if not source_state.valid_solid_part:
                raise RuntimeError("Source Part failed solid-body or rebuild validation.")
            self._validate_expected(
                request,
                source_state,
                source_bbox,
                ordered_feature_count=source_ordered_feature_count,
            )

            copy_errors: list[str] = []
            copied = False
            for label, save_call in (
                ("ModelDocExtension.SaveAs", lambda: session.save(model, str(target))),
                ("ModelDoc2.SaveAs3", lambda: model.SaveAs3(str(target), 0, 0)),
                ("ModelDoc2.SaveAs", lambda: model.SaveAs(str(target))),
            ):
                try:
                    returned = bool(save_call())
                    if returned and target.is_file() and target.stat().st_size > 0:
                        copied = True
                        break
                    copy_errors.append(f"{label} returned {returned}")
                except Exception as save_exc:
                    copy_errors.append(f"{label}: {save_exc!r}")
            if not copied:
                raise RuntimeError(
                    f"SolidWorks native SLDPRT copy failed: {target}; "
                    + "; ".join(copy_errors)
                )

            if not initial_path or initial_path.casefold() != str(source).casefold():
                self._close_document(session, model)
            cloned = self._open_part_dynamic(session, target, read_only=False)
            if cloned is None:
                raise RuntimeError("SolidWorks could not reopen the cloned Part.")
            clone_state = inspector.inspect(cloned)
            clone_bbox = inspector.exact_body_bbox(cloned)
            clone_ordered_feature_count = self._ordered_feature_count(cloned)
            comparison = self._compare_states(source_state, clone_state, source_bbox, clone_bbox)
            comparison["source_ordered_feature_count"] = source_ordered_feature_count
            comparison["clone_ordered_feature_count"] = clone_ordered_feature_count
            comparison["ordered_feature_tree_available"] = (
                source_ordered_feature_count > 0 and clone_ordered_feature_count > 0
            )
            comparison["ordered_feature_tree_match"] = (
                source_ordered_feature_count == clone_ordered_feature_count
                if comparison["ordered_feature_tree_available"]
                else True
            )
            comparison["expected_ordered_feature_count"] = request.get("expected_feature_count")
            source_sha_after = self._sha256(source)
            comparison["source_unchanged"] = source_sha_after == actual_sha
            comparison["source_sha256_after"] = source_sha_after
            comparison["target_size_bytes"] = target.stat().st_size
            comparison["parity_passed"] = all(
                comparison[key]
                for key in (
                    "solid_body_count_match",
                    "feature_count_match",
                    "ordered_feature_tree_match",
                    "volume_match",
                    "bounding_box_match",
                    "source_rebuild_clean",
                    "clone_rebuild_clean",
                    "source_unchanged",
                )
            )
            payload = {
                "success": comparison["parity_passed"],
                "operation": self.key,
                "source_path": str(source),
                "target_path": str(target),
                "source_sha256": actual_sha,
                "source_state": source_state.as_dict(),
                "clone_state": clone_state.as_dict(),
                "source_bounding_box_m": source_bbox,
                "clone_bounding_box_m": clone_bbox,
                "comparison": comparison,
                "files": [str(target), str(report_path)],
            }
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if not comparison["parity_passed"]:
                return SkillResult(
                    False,
                    "Cloned SLDPRT failed source-parity validation.",
                    data=payload,
                    path=str(report_path),
                    output=json.dumps(payload, ensure_ascii=False, indent=2),
                )
            return SkillResult(
                True,
                f"Complete native source Part cloned and verified: {target}",
                data=payload,
                path=str(target),
                output=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            if session is not None and model is not None and not source_previously_open:
                try:
                    self._close_document(session, model)
                except Exception:
                    pass
            payload = {
                "success": False,
                "operation": self.key,
                "source_path": str(source),
                "target_path": str(target),
                "error": repr(exc),
                "traceback": traceback.format_exc(),
                "files": [str(report_path)],
            }
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return SkillResult(False, f"source_part_clone failed: {exc}", data=payload, path=str(report_path))

    def _ensure_runtime(self) -> None:
        if not self.skill_script_dir.is_dir():
            raise RuntimeError(f"SolidWorks automation scripts not found: {self.skill_script_dir}")
        script_dir = str(self.skill_script_dir)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        os.environ["SOLIDWORKS_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["SW_AUTOMATION_NO_INTERACTIVE"] = "1"
        os.environ["CAD_AGENT_NO_INTERACTIVE"] = "1"
        from sw_preflight import ensure_com_dependencies

        ensure_com_dependencies(allow_install=False)

    @staticmethod
    def _validate_expected(
        request: dict[str, Any],
        state: Any,
        bbox: dict[str, float] | None,
        *,
        ordered_feature_count: int,
    ) -> None:
        body_count = request.get("expected_solid_body_count")
        if body_count is not None and state.solid_body_count != body_count:
            raise RuntimeError(
                f"Source solid-body count mismatch: expected {body_count}, got {state.solid_body_count}."
            )
        feature_count = request.get("expected_feature_count")
        if (
            feature_count is not None
            and ordered_feature_count > 0
            and ordered_feature_count != feature_count
        ):
            raise RuntimeError(
                f"Source ordered feature-tree count mismatch: expected {feature_count}, got {ordered_feature_count}."
            )
        volume = request.get("expected_volume_m3")
        if volume is not None and (
            state.volume_m3 is None or abs(state.volume_m3 - volume) > max(1e-12, abs(volume) * 1e-9)
        ):
            raise RuntimeError(
                f"Source volume mismatch: expected {volume}, got {state.volume_m3}."
            )
        expected_bbox = request.get("expected_bounding_box_mm")
        if expected_bbox is not None:
            if not isinstance(expected_bbox, list) or len(expected_bbox) != 6 or bbox is None:
                raise RuntimeError("expected_bounding_box_mm must contain six values.")
            actual = [
                bbox["xmin"] * 1000.0,
                bbox["ymin"] * 1000.0,
                bbox["zmin"] * 1000.0,
                bbox["xmax"] * 1000.0,
                bbox["ymax"] * 1000.0,
                bbox["zmax"] * 1000.0,
            ]
            if max(abs(float(left) - float(right)) for left, right in zip(actual, expected_bbox)) > 0.001:
                raise RuntimeError(f"Source bounding box does not match the CAD-IR contract: {actual}.")

    @staticmethod
    def _compare_states(
        source: Any,
        clone: Any,
        source_bbox: dict[str, float] | None,
        clone_bbox: dict[str, float] | None,
    ) -> dict[str, Any]:
        volume_delta = (
            abs(float(source.volume_m3) - float(clone.volume_m3))
            if source.volume_m3 is not None and clone.volume_m3 is not None
            else None
        )
        bbox_delta = None
        if source_bbox is not None and clone_bbox is not None:
            bbox_delta = max(
                abs(float(source_bbox[key]) - float(clone_bbox[key]))
                for key in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")
            )
        return {
            "solid_body_count_match": source.solid_body_count == clone.solid_body_count,
            "feature_count_match": source.feature_count == clone.feature_count,
            "volume_delta_m3": volume_delta,
            "volume_match": volume_delta is not None and volume_delta <= 1e-12,
            "max_bounding_box_delta_m": bbox_delta,
            "bounding_box_match": bbox_delta is not None and bbox_delta <= 1e-9,
            "source_rebuild_clean": source.rebuild_error_count == 0 and source.feature_error_count == 0,
            "clone_rebuild_clean": clone.rebuild_error_count == 0 and clone.feature_error_count == 0,
        }

    @staticmethod
    def _document_path(model: Any | None) -> str:
        if model is None:
            return ""
        return str(SourcePartCloneSkill._com_value(model, "GetPathName", "") or "")

    @classmethod
    def _ordered_feature_count(cls, model: Any) -> int:
        feature = cls._com_value(model, "FirstFeature", None)
        count = 0
        seen: set[tuple[str, str]] = set()
        while feature is not None and count < 10000:
            name = str(cls._com_value(feature, "Name", "") or "")
            feature_type = str(cls._com_value(feature, "GetTypeName2", "") or "")
            marker = (name.casefold(), feature_type.casefold())
            if marker in seen:
                break
            seen.add(marker)
            count += 1
            next_feature = cls._com_value(feature, "IGetNextFeature", None)
            if next_feature is None:
                next_feature = cls._com_value(feature, "GetNextFeature", None)
            feature = next_feature
        return count

    @staticmethod
    def _com_value(owner: Any, name: str, default: Any = None) -> Any:
        try:
            member = getattr(owner, name)
            return member() if callable(member) else member
        except Exception:
            return default

    @classmethod
    def _com_int(cls, owner: Any, name: str, default: int = 0) -> int:
        value = cls._com_value(owner, name, default)
        value = getattr(value, "value", value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _open_part_dynamic(session: Any, path: Path, *, read_only: bool) -> Any:
        import pythoncom
        from win32com.client import VARIANT, dynamic

        app = dynamic.Dispatch(session.sw._oleobj_)
        errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        options = 1 | (2 if read_only else 0)
        model = app.OpenDoc6(str(path), 1, options, "", errors, warnings)
        if model is None:
            raise RuntimeError(
                f"SolidWorks OpenDoc6 failed for {path}: "
                f"errors={errors.value}, warnings={warnings.value}"
            )
        session.model = model
        return model

    @classmethod
    def _close_document(cls, session: Any, model: Any) -> None:
        title = str(cls._com_value(model, "GetTitle", "") or "")
        if title:
            session.sw.CloseDoc(title)
        if getattr(session, "model", None) is model:
            session.model = session.sw.ActiveDoc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def cls_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def cls_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
