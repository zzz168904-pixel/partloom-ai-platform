from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client
from win32com.client import VARIANT

from .active_model_through_hole import ActiveModelThroughHoleExecutor


def _member(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
    return ActiveModelThroughHoleExecutor._com_member(obj, name, *args, default=default)


def _feature_by_name(model: Any, requested_name: str) -> Any | None:
    direct = _member(model, "FeatureByName", requested_name, default=None)
    if direct is not None:
        return direct

    requested = requested_name.strip().casefold()
    feature = _member(model, "FirstFeature")
    seen: set[tuple[str, str]] = set()
    while feature is not None and len(seen) < 10000:
        name = str(_member(feature, "Name", default="") or "").strip()
        feature_type = str(_member(feature, "GetTypeName2", default="") or "").strip()
        marker = (name.casefold(), feature_type.casefold())
        if marker in seen:
            break
        seen.add(marker)
        if name.casefold() == requested:
            return feature
        next_feature = _member(feature, "IGetNextFeature", default=None)
        if next_feature is None:
            next_feature = _member(feature, "GetNextFeature", default=None)
        feature = next_feature
    return None


def apply_feature_fillet(model_path: Path, feature_name: str, radius_mm: float, output_name: str) -> dict[str, Any]:
    pythoncom.CoInitialize()
    sw = model = None
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        active = sw.ActiveDoc
        if active is None:
            raise RuntimeError("The task model must already be active before isolated fillet execution.")
        active_path = str(_member(active, "GetPathName", default="") or "").strip()
        if not active_path or Path(active_path).resolve() != model_path.resolve():
            raise RuntimeError(
                f"ActiveDoc does not match the task model: active={active_path!r}, expected={str(model_path)!r}"
            )
        model = active

        selected = False
        attempts = 0
        for attempts in range(1, 7):
            time.sleep(0.5)
            active_document = sw.ActiveDoc
            if active_document is not None:
                model = active_document
            model.ClearSelection2(True)
            feature = _feature_by_name(model, feature_name)
            if feature is not None and bool(_member(feature, "Select2", False, 0, default=False)):
                selected = True
                break
            model.ForceRebuild3(False)
        if not selected:
            raise RuntimeError(f"Target feature {feature_name!r} could not be selected in isolated COM session.")

        created = model.FeatureManager.FeatureFillet(195, float(radius_mm) / 1000.0, 0, 0, None, None, None)
        if created is None:
            raise RuntimeError("SolidWorks FeatureFillet returned no feature.")
        created.Name = output_name
        model.ClearSelection2(True)
        model.ForceRebuild3(False)
        errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        saved = _member(model, "Save3", 1, errors, warnings, default=False)
        if isinstance(saved, tuple):
            saved = saved[0] if saved else False
        if not bool(saved):
            saved = _member(model, "Save", default=False)
        if not bool(saved) or not model_path.is_file() or model_path.stat().st_size <= 0:
            raise RuntimeError(
                f"SolidWorks did not save the isolated fillet result: errors={errors.value}, warnings={warnings.value}"
            )
        return {
            "success": True,
            "model_path": str(model_path),
            "target_feature": feature_name,
            "radius_mm": radius_mm,
            "selection_attempts": attempts,
            "output_feature": output_name,
            "save_errors": int(errors.value or 0),
            "save_warnings": int(warnings.value or 0),
        }
    except Exception as exc:
        return {"success": False, "error": repr(exc), "model_path": str(model_path)}
    finally:
        pythoncom.CoUninitialize()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--radius-mm", type=float, required=True)
    parser.add_argument("--output-name", required=True)
    args = parser.parse_args()
    result = apply_feature_fillet(args.model.resolve(), args.feature, args.radius_mm, args.output_name)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
