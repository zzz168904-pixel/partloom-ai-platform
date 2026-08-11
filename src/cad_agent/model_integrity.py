from __future__ import annotations

import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SW_DOC_PART = 1


def as_part_doc(model: Any) -> Any:
    """Return an IPartDoc proxy when OpenDoc returned a strict IModelDoc2."""
    if model is None or getattr(model, "_oleobj_", None) is None:
        return model
    try:
        import win32com.client

        return win32com.client.CastTo(model, "IPartDoc")
    except Exception:
        return model


def _com_value(member: Any, *args: Any, default: Any = None) -> Any:
    try:
        return member(*args) if callable(member) else member
    except Exception:
        return default


def _com_member(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
    """Read a SolidWorks COM method/property without losing proxy values."""
    if obj is None:
        return default
    try:
        member = getattr(obj, name)
    except Exception:
        return default
    try:
        if callable(member):
            return member(*args)
        return member if not args else default
    except TypeError:
        return member if not args else default
    except Exception as exc:
        message = str(exc)
        if not args and ("-2147352573" in message or "Member not found" in message):
            return member
        return default


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


@dataclass(frozen=True)
class ModelState:
    document_title: str
    document_path: str
    document_type: int
    body_count: int
    solid_body_count: int
    surface_body_count: int
    feature_count: int
    volume_m3: float | None
    rebuild_error_count: int
    feature_error_count: int

    @property
    def valid_solid_part(self) -> bool:
        return (
            self.document_type == SW_DOC_PART
            and self.solid_body_count > 0
            and self.volume_m3 is not None
            and self.volume_m3 > 0.0
            and self.rebuild_error_count == 0
            and self.feature_error_count == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_title": self.document_title,
            "document_path": self.document_path,
            "document_type": self.document_type,
            "body_count": self.body_count,
            "solid_body_count": self.solid_body_count,
            "surface_body_count": self.surface_body_count,
            "feature_count": self.feature_count,
            "volume_m3": self.volume_m3,
            "rebuild_error_count": self.rebuild_error_count,
            "feature_error_count": self.feature_error_count,
            "valid_solid_part": self.valid_solid_part,
        }


@dataclass(frozen=True)
class IntegrityResult:
    success: bool
    reason: str
    message: str
    checks: dict[str, bool] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "reason": self.reason,
            "message": self.message,
            "checks": dict(self.checks),
            "data": dict(self.data),
        }


@dataclass
class ModelTransaction:
    skill_key: str
    source_path: Path | None
    checkpoint_path: Path | None
    before: ModelState | None
    active: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_key": self.skill_key,
            "source_path": str(self.source_path) if self.source_path else None,
            "checkpoint_path": str(self.checkpoint_path) if self.checkpoint_path else None,
            "before": self.before.as_dict() if self.before else None,
            "active": self.active,
        }


class SolidWorksModelInspector:
    """Read deterministic model state without retaining live COM objects."""

    def inspect(self, model: Any, *, rebuild: bool = True) -> ModelState:
        document_type = int(_com_value(getattr(model, "GetType", None), default=0) or 0)
        title = str(_com_value(getattr(model, "GetTitle", None), default="") or "")
        path = str(_com_value(getattr(model, "GetPathName", None), default="") or "")
        rebuild_errors = 0
        if rebuild:
            # SOLIDWORKS returns True when every feature rebuilds successfully.
            # Treat a False or unavailable result as one deterministic rebuild
            # failure; the previous implementation inverted this API contract.
            rebuild_succeeded = bool(
                _com_value(getattr(model, "ForceRebuild3", None), False, default=False)
            )
            rebuild_errors = 0 if rebuild_succeeded else 1

        solid_bodies = self._bodies(model, 0)
        surface_bodies = self._bodies(model, 1)
        volume_values = [self._body_volume(body) for body in solid_bodies]
        known_volumes = [value for value in volume_values if value is not None and math.isfinite(value)]
        feature_count, feature_errors = self._feature_status(model)
        return ModelState(
            document_title=title,
            document_path=path,
            document_type=document_type,
            body_count=len(solid_bodies) + len(surface_bodies),
            solid_body_count=len(solid_bodies),
            surface_body_count=len(surface_bodies),
            feature_count=feature_count,
            volume_m3=sum(known_volumes) if known_volumes else None,
            rebuild_error_count=rebuild_errors,
            feature_error_count=feature_errors,
        )

    @staticmethod
    def _bodies(model: Any, body_type: int) -> list[Any]:
        part_doc = as_part_doc(model)
        return _items(_com_value(getattr(part_doc, "GetBodies2", None), body_type, False, default=None))

    @classmethod
    def exact_body_bbox(cls, model: Any, body_type: int = 0) -> dict[str, float] | None:
        """Return an axis-aligned box from exact body extreme points.

        IBody2.GetBodyBox is explicitly approximate and may change after a
        rebuild, so it cannot be used for dimensional acceptance.
        """
        bodies = cls._bodies(model, body_type)
        if not bodies:
            return None
        bounds = {
            "xmin": float("inf"),
            "ymin": float("inf"),
            "zmin": float("inf"),
            "xmax": float("-inf"),
            "ymax": float("-inf"),
            "zmax": float("-inf"),
        }
        directions = (
            ("xmax", 0, (1.0, 0.0, 0.0)),
            ("xmin", 0, (-1.0, 0.0, 0.0)),
            ("ymax", 1, (0.0, 1.0, 0.0)),
            ("ymin", 1, (0.0, -1.0, 0.0)),
            ("zmax", 2, (0.0, 0.0, 1.0)),
            ("zmin", 2, (0.0, 0.0, -1.0)),
        )
        for body in bodies:
            for key, coordinate_index, direction in directions:
                result = _com_value(getattr(body, "GetExtremePoint", None), *direction, default=None)
                if not isinstance(result, (list, tuple)) or len(result) < 4 or not bool(result[0]):
                    return None
                coordinate = float(result[coordinate_index + 1])
                reducer = min if key.endswith("min") else max
                bounds[key] = reducer(bounds[key], coordinate)
        if any(not math.isfinite(value) for value in bounds.values()):
            return None
        bounds.update({
            "length": bounds["xmax"] - bounds["xmin"],
            "width": bounds["ymax"] - bounds["ymin"],
            "thickness": bounds["zmax"] - bounds["zmin"],
        })
        return bounds

    @staticmethod
    def _body_volume(body: Any) -> float | None:
        value = _com_value(getattr(body, "GetVolume", None), default=None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        properties = _com_value(getattr(body, "GetMassProperties", None), 1.0, default=None)
        if isinstance(properties, (list, tuple)) and len(properties) > 3:
            try:
                return float(properties[3])
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _feature_status(model: Any) -> tuple[int, int]:
        manager = _com_member(model, "FeatureManager", default=None)
        all_features = _items(_com_member(manager, "GetFeatures", False, default=None))
        if all_features:
            count = 0
            errors = 0
            seen: set[tuple[str, str]] = set()
            for feature in all_features:
                name = str(_com_member(feature, "Name", default="") or "").strip()
                feature_type = str(_com_member(feature, "GetTypeName2", default="") or "").strip()
                marker = (name.casefold(), feature_type.casefold())
                if marker in seen:
                    continue
                seen.add(marker)
                count += 1
                error_value = _com_member(feature, "GetErrorCode2", default=0)
                if isinstance(error_value, (list, tuple)):
                    error_value = error_value[0] if error_value else 0
                try:
                    errors += int(int(error_value or 0) != 0)
                except (TypeError, ValueError):
                    errors += 1
            return count, errors

        feature = _com_member(model, "FirstFeature", default=None)
        count = 0
        errors = 0
        seen: set[tuple[str, str]] = set()
        while feature is not None and count < 10000:
            name = str(_com_member(feature, "Name", default="") or "").strip()
            feature_type = str(
                _com_member(feature, "GetTypeName2", default="") or ""
            ).strip()
            marker = (name.casefold(), feature_type.casefold())
            if marker in seen:
                break
            seen.add(marker)
            count += 1
            error_value = _com_member(feature, "GetErrorCode2", default=0)
            if isinstance(error_value, (list, tuple)):
                error_value = error_value[0] if error_value else 0
            try:
                errors += int(int(error_value or 0) != 0)
            except (TypeError, ValueError):
                errors += 1
            next_feature = _com_member(feature, "IGetNextFeature", default=None)
            if next_feature is None:
                next_feature = _com_member(feature, "GetNextFeature", default=None)
            feature = next_feature
        return count, errors


class ModelIntegrityGuard:
    """Pipeline boundary for prechecks, postconditions, checkpoints and rollback."""

    ADDITIVE_OPERATIONS = {
        "base_plate", "boss", "rib", "side_boss",
    }
    SUBTRACTIVE_OPERATIONS = {"through_hole", "threaded_hole", "pocket", "slot", "side_hole"}
    # A pattern or mirror inherits the material effect of its seed feature. It
    # can therefore add or remove volume, so only require a real geometry
    # change instead of assuming an additive result.
    PATTERN_OPERATIONS = {"linear_pattern", "circular_pattern", "mirror"}
    SHAPE_OPERATIONS = {"fillet", "chamfer", "shell", "draft"}
    TRANSACTIONAL_OPERATIONS = ADDITIVE_OPERATIONS | SUBTRACTIVE_OPERATIONS | PATTERN_OPERATIONS | SHAPE_OPERATIONS

    def __init__(self, *, connector_factory: Any | None = None, inspector: SolidWorksModelInspector | None = None) -> None:
        self.connector_factory = connector_factory
        self.inspector = inspector or SolidWorksModelInspector()

    def requires_transaction(self, skill_key: str) -> bool:
        return skill_key in self.TRANSACTIONAL_OPERATIONS

    def begin(self, context: Any, skill_key: str) -> tuple[ModelTransaction | None, IntegrityResult]:
        connector = self._connector()
        try:
            model = self._active_part(connector)
            if model is None:
                return None, self._failure("active_part_missing", "No active SolidWorks Part is available for the feature precheck.")
            before = self.inspector.inspect(model)
            if not before.valid_solid_part:
                return None, self._state_failure("geometry_precheck_failed", "The active Part failed the geometry precheck.", before)
            source = self._expected_path(context, before)
            checkpoint = self._checkpoint_path(context, skill_key)
            persisted = self._persist_checkpoint(model, source, checkpoint)
            if not persisted.success:
                return None, persisted
            transaction = ModelTransaction(skill_key, source, checkpoint, before)
            return transaction, IntegrityResult(
                True,
                "geometry_precheck_passed",
                "Active Part and rollback checkpoint verified.",
                checks={"active_part": True, "valid_geometry": True, "checkpoint_created": True},
                data={"before": before.as_dict(), "checkpoint_path": str(checkpoint)},
            )
        finally:
            self._disconnect(connector)

    def verify_after(self, transaction: ModelTransaction | None, skill_key: str) -> IntegrityResult:
        connector = self._connector()
        try:
            model = self._active_part(connector)
            if model is None:
                return self._failure("postcondition_active_part_missing", "The active Part disappeared after feature execution.")
            after = self.inspector.inspect(model)
            before = transaction.before if transaction else None
            checks = {
                "part_document": after.document_type == SW_DOC_PART,
                "solid_body_exists": after.solid_body_count > 0,
                "positive_volume": after.volume_m3 is not None and after.volume_m3 > 0,
                "no_rebuild_errors": after.rebuild_error_count == 0,
                "no_feature_errors": after.feature_error_count == 0,
            }
            if before is not None:
                checks["feature_tree_not_reduced"] = after.feature_count >= before.feature_count
                changed = self._geometry_changed(before, after)
                checks["geometry_changed"] = changed
                if skill_key in self.ADDITIVE_OPERATIONS:
                    checks["expected_volume_direction"] = self._volume_delta(before, after) > 0
                elif skill_key in self.SUBTRACTIVE_OPERATIONS:
                    checks["expected_volume_direction"] = self._volume_delta(before, after) < 0
                else:
                    checks["expected_volume_direction"] = changed
            success = all(checks.values())
            return IntegrityResult(
                success,
                "feature_postcondition_passed" if success else "feature_postcondition_failed",
                "Feature geometry verified." if success else "Feature execution did not produce the required valid geometry change.",
                checks=checks,
                data={"before": before.as_dict() if before else None, "after": after.as_dict()},
            )
        finally:
            self._disconnect(connector)

    def commit(self, context: Any, transaction: ModelTransaction | None, skill_key: str) -> IntegrityResult:
        connector = self._connector()
        try:
            model = self._active_part(connector)
            if model is None:
                return self._failure("checkpoint_commit_failed", "No active Part is available for checkpoint commit.")
            source = self._expected_path(context, self.inspector.inspect(model, rebuild=False))
            if source is None:
                return self._failure("checkpoint_source_missing", "The active Part has no task-owned file path.")
            save_result = _com_value(getattr(model, "Save", None), default=False)
            if not source.is_file() or source.stat().st_size <= 0:
                return self._failure(
                    "checkpoint_commit_failed",
                    f"SolidWorks did not persist the validated model checkpoint: {source}",
                    data={"save_result": save_result},
                )
            target = self._checkpoint_path(context, skill_key, committed=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if transaction:
                transaction.active = False
            context.add_artifact("last_valid_checkpoint", target)
            return IntegrityResult(
                True,
                "checkpoint_committed",
                "Validated feature state committed as the latest recovery checkpoint.",
                checks={"model_saved": True, "checkpoint_created": True},
                data={"model_path": str(source), "checkpoint_path": str(target)},
            )
        except Exception as exc:
            return self._failure("checkpoint_commit_failed", f"Checkpoint commit failed: {exc}", data={"error": repr(exc)})
        finally:
            self._disconnect(connector)

    def rollback(self, context: Any, transaction: ModelTransaction | None, reason: str) -> IntegrityResult:
        if transaction is None or not transaction.active:
            return IntegrityResult(True, "rollback_not_required", "No active geometry transaction required rollback.")
        source = transaction.source_path
        checkpoint = transaction.checkpoint_path
        if source is None or checkpoint is None or not checkpoint.is_file():
            return self._failure("rollback_checkpoint_missing", "The rollback checkpoint is unavailable.")
        connector = self._connector()
        try:
            model = self._active_part(connector)
            if model is not None:
                title = str(_com_value(getattr(model, "GetTitle", None), default="") or "")
                app = getattr(connector, "app", None)
                if app is not None and title:
                    _com_value(getattr(app, "CloseDoc", None), title, default=None)
            shutil.copy2(checkpoint, source)
            opened = connector.open_model(source)
            success = bool(opened.get("success"))
            transaction.active = False
            return IntegrityResult(
                success,
                "rollback_completed" if success else "rollback_reopen_failed",
                "Restored the last valid Part checkpoint." if success else "Checkpoint file was restored but SolidWorks could not reopen it.",
                checks={"checkpoint_restored": True, "model_reopened": success},
                data={"reason": reason, "source_path": str(source), "checkpoint_path": str(checkpoint)},
            )
        except Exception as exc:
            return self._failure("rollback_failed", f"Model rollback failed: {exc}", data={"error": repr(exc), "reason": reason})
        finally:
            self._disconnect(connector)

    def validate_final_save(self, context: Any) -> IntegrityResult:
        policy = context.design_json.get("execution_policy", {})
        allowed = set(policy.get("allowed_skills", []))
        required_model_skills = allowed & self.TRANSACTIONAL_OPERATIONS
        succeeded = {record.skill_key for record in context.step_records if record.status == "success"}
        missing = sorted(required_model_skills - succeeded)
        checks = {
            "required_features_completed": not missing,
            "no_failed_required_steps": not any(record.required and record.status == "failed" for record in context.step_records),
            "no_unexpected_outputs": not context.unexpected_outputs,
        }
        connector = self._connector()
        try:
            model = self._active_part(connector)
            if model is None:
                checks["active_part"] = False
                return IntegrityResult(False, "final_save_gate_failed", "Final save blocked because no active Part is available.", checks, {"missing_skills": missing})
            state = self.inspector.inspect(model)
            expected = context.artifacts.get("solidworks_model")
            checks.update({
                "active_part": state.document_type == SW_DOC_PART,
                "solid_body_exists": state.solid_body_count > 0,
                "positive_volume": state.volume_m3 is not None and state.volume_m3 > 0,
                "no_rebuild_errors": state.rebuild_error_count == 0,
                "no_feature_errors": state.feature_error_count == 0,
                "task_model_active": not expected or self._same_path(expected, state.document_path),
            })
            success = all(checks.values())
            return IntegrityResult(
                success,
                "final_save_gate_passed" if success else "final_save_gate_failed",
                "Final model is eligible for delivery save." if success else "Final save blocked because model integrity or task scope checks failed.",
                checks,
                {"missing_skills": missing, "state": state.as_dict(), "expected_model": expected},
            )
        finally:
            self._disconnect(connector)

    def _connector(self) -> Any:
        if self.connector_factory is not None:
            return self.connector_factory()
        from sw_connector import SWConnector

        return SWConnector()

    @staticmethod
    def _active_part(connector: Any) -> Any | None:
        if connector.connect() is None:
            return None
        model = connector.get_active_doc() if hasattr(connector, "get_active_doc") else getattr(connector.app, "ActiveDoc", None)
        if model is None or int(_com_value(getattr(model, "GetType", None), default=0) or 0) != SW_DOC_PART:
            return None
        return model

    @staticmethod
    def _disconnect(connector: Any) -> None:
        try:
            connector.disconnect()
        except Exception:
            pass

    @staticmethod
    def _same_path(left: str | Path, right: str | Path) -> bool:
        if not left or not right:
            return False
        return str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()

    @classmethod
    def _expected_path(cls, context: Any, state: ModelState) -> Path | None:
        expected = context.artifacts.get("solidworks_model")
        if expected and state.document_path and not cls._same_path(expected, state.document_path):
            return None
        value = expected or state.document_path
        return Path(value).resolve() if value else None

    @staticmethod
    def _checkpoint_path(context: Any, skill_key: str, *, committed: bool = False) -> Path:
        records = len(context.step_records)
        suffix = "after" if committed else "before"
        safe_key = "".join(char if char.isalnum() or char in "_-" else "_" for char in skill_key)
        return context.run_dir / "checkpoints" / f"{records:03d}_{safe_key}_{suffix}.SLDPRT"

    @staticmethod
    def _persist_checkpoint(model: Any, source: Path | None, checkpoint: Path) -> IntegrityResult:
        if source is None:
            return IntegrityResult(False, "checkpoint_source_missing", "The active Part is not the task-owned model.")
        _com_value(getattr(model, "Save", None), default=False)
        if not source.is_file() or source.stat().st_size <= 0:
            return IntegrityResult(False, "checkpoint_source_missing", f"The task model is not persisted: {source}")
        try:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, checkpoint)
            return IntegrityResult(True, "checkpoint_created", "Rollback checkpoint created.", data={"checkpoint_path": str(checkpoint)})
        except Exception as exc:
            return IntegrityResult(False, "checkpoint_create_failed", f"Could not create rollback checkpoint: {exc}", data={"error": repr(exc)})

    @staticmethod
    def _volume_delta(before: ModelState, after: ModelState) -> float:
        if before.volume_m3 is None or after.volume_m3 is None:
            return 0.0
        tolerance = max(abs(before.volume_m3), abs(after.volume_m3), 1.0) * 1e-12
        delta = after.volume_m3 - before.volume_m3
        return 0.0 if abs(delta) <= tolerance else delta

    @classmethod
    def _geometry_changed(cls, before: ModelState, after: ModelState) -> bool:
        return (
            before.body_count != after.body_count
            or before.feature_count != after.feature_count
            or cls._volume_delta(before, after) != 0.0
        )

    @staticmethod
    def _failure(reason: str, message: str, *, data: dict[str, Any] | None = None) -> IntegrityResult:
        return IntegrityResult(False, reason, message, data=data or {})

    @staticmethod
    def _state_failure(reason: str, message: str, state: ModelState) -> IntegrityResult:
        checks = {
            "part_document": state.document_type == SW_DOC_PART,
            "solid_body_exists": state.solid_body_count > 0,
            "positive_volume": state.volume_m3 is not None and state.volume_m3 > 0,
            "no_rebuild_errors": state.rebuild_error_count == 0,
            "no_feature_errors": state.feature_error_count == 0,
        }
        return IntegrityResult(False, reason, message, checks, {"state": state.as_dict()})
