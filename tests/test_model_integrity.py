from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad_agent.model_integrity import ModelIntegrityGuard, ModelState, SolidWorksModelInspector
from cad_agent.brain_models import PlannedSkillStep
from cad_agent.pipeline.pipeline_context import PipelineContext, PipelineStepRecord
from cad_agent.pipeline.pipeline_executor import LifecycleResult, PipelineExecutor, SkillLifecycleAdapter
from cad_agent.pipeline.pipeline_logger import PipelineLogger
from cad_agent.registry import CADAgentSkillManager


class FakeBody:
    def __init__(self, volume: float) -> None:
        self.volume = volume

    def GetVolume(self) -> float:
        return self.volume

    def GetExtremePoint(self, x: float, y: float, z: float) -> tuple[bool, float, float, float]:
        values = [x, y, z]
        return True, values[0], values[1] * 2.0, values[2] * 3.0


class FakeFeature:
    def __init__(self, error: int = 0, next_feature: "FakeFeature | None" = None, name: str = "Feature") -> None:
        self.error = error
        self.next_feature = next_feature
        self.Name = name

    def GetErrorCode2(self) -> int:
        return self.error

    def GetNextFeature(self) -> "FakeFeature | None":
        return self.next_feature

    def GetTypeName2(self) -> str:
        return "MockFeature"


class FakeModel:
    def __init__(self, path: Path, *, volume: float = 1.0, features: int = 2, rebuild_error: bool = False) -> None:
        self.path = path
        self.volume = volume
        self.features = features
        self.rebuild_error = rebuild_error
        self.saved = 0

    def GetType(self) -> int:
        return 1

    def GetTitle(self) -> str:
        return self.path.name

    def GetPathName(self) -> str:
        return str(self.path)

    def ForceRebuild3(self, _top_only: bool) -> bool:
        return not self.rebuild_error

    def GetBodies2(self, body_type: int, _visible_only: bool) -> list[FakeBody]:
        return [FakeBody(self.volume)] if body_type == 0 and self.volume > 0 else []

    def FirstFeature(self) -> FakeFeature | None:
        current: FakeFeature | None = None
        for index in range(self.features):
            current = FakeFeature(next_feature=current, name=f"Feature{index + 1}")
        return current

    def Save(self) -> bool:
        self.saved += 1
        if not self.path.exists():
            self.path.write_bytes(b"valid-sldprt")
        return True


class FakeApp:
    def __init__(self, model: FakeModel) -> None:
        self.ActiveDoc = model
        self.closed: list[str] = []

    def CloseDoc(self, title: str) -> None:
        self.closed.append(title)
        self.ActiveDoc = None


class FakeConnector:
    def __init__(self, app: FakeApp) -> None:
        self.app = app
        self.opened: list[str] = []

    def connect(self) -> FakeApp:
        return self.app

    def disconnect(self) -> None:
        return None

    def get_active_doc(self) -> FakeModel | None:
        return self.app.ActiveDoc

    def open_model(self, path: str | Path) -> dict:
        self.opened.append(str(path))
        current = self.app.ActiveDoc
        if current is None:
            self.app.ActiveDoc = FakeModel(Path(path), volume=1.0, features=2)
        return {"success": True, "path": str(path)}


def _context(tmp_path: Path, model_path: Path, allowed: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        run_dir=tmp_path,
        artifacts={"solidworks_model": str(model_path)},
        design_json={"execution_policy": {"allowed_skills": allowed}},
        step_records=[],
        unexpected_outputs=[],
        add_artifact=lambda key, value: None,
    )


def test_inspector_records_bodies_volume_features_and_rebuild_errors(tmp_path: Path) -> None:
    model = FakeModel(tmp_path / "part.SLDPRT", volume=0.0025, features=4)
    state = SolidWorksModelInspector().inspect(model)
    assert state.valid_solid_part
    assert state.solid_body_count == 1
    assert state.volume_m3 == 0.0025
    assert state.feature_count == 4
    assert state.rebuild_error_count == 0


def test_inspector_uses_solidworks_force_rebuild_success_semantics(tmp_path: Path) -> None:
    model = FakeModel(tmp_path / "part.SLDPRT", rebuild_error=True)
    state = SolidWorksModelInspector().inspect(model)
    assert state.rebuild_error_count == 1


def test_inspector_counts_registered_subfeatures_when_feature_manager_exposes_them(tmp_path: Path) -> None:
    model = FakeModel(tmp_path / "part.SLDPRT", features=2)

    class FeatureManager:
        @staticmethod
        def GetFeatures(top_only: bool):
            assert top_only is False
            return [
                FakeFeature(name="Boss-Revolve1"),
                FakeFeature(name="stud_left_external_thread"),
                FakeFeature(name="stud_right_external_thread"),
            ]

    model.FeatureManager = FeatureManager()
    state = SolidWorksModelInspector().inspect(model)
    assert state.feature_count == 3
    assert state.feature_error_count == 0


def test_inspector_exact_bbox_uses_body_extreme_points(tmp_path: Path) -> None:
    model = FakeModel(tmp_path / "part.SLDPRT")
    bbox = SolidWorksModelInspector.exact_body_bbox(model)
    assert bbox == {
        "xmin": -1.0,
        "ymin": -2.0,
        "zmin": -3.0,
        "xmax": 1.0,
        "ymax": 2.0,
        "zmax": 3.0,
        "length": 2.0,
        "width": 4.0,
        "thickness": 6.0,
    }


def test_begin_creates_checkpoint_and_subtractive_postcondition_passes(tmp_path: Path) -> None:
    model_path = tmp_path / "part.SLDPRT"
    model_path.write_bytes(b"before")
    model = FakeModel(model_path, volume=0.01, features=3)
    connector = FakeConnector(FakeApp(model))
    guard = ModelIntegrityGuard(connector_factory=lambda: connector)
    context = _context(tmp_path, model_path, ["through_hole", "save_sldprt"])

    transaction, precheck = guard.begin(context, "through_hole")
    assert precheck.success
    assert transaction is not None and transaction.checkpoint_path is not None
    assert transaction.checkpoint_path.read_bytes() == b"before"

    model.volume = 0.009
    model.features = 4
    postcondition = guard.verify_after(transaction, "through_hole")
    assert postcondition.success
    assert postcondition.checks["geometry_changed"]
    assert postcondition.checks["expected_volume_direction"]


def test_pattern_postcondition_accepts_subtractive_seed_volume_delta(tmp_path: Path) -> None:
    model_path = tmp_path / "part.SLDPRT"
    model_path.write_bytes(b"before")
    model = FakeModel(model_path, volume=0.01, features=3)
    connector = FakeConnector(FakeApp(model))
    guard = ModelIntegrityGuard(connector_factory=lambda: connector)
    context = _context(tmp_path, model_path, ["circular_pattern", "save_sldprt"])

    transaction, precheck = guard.begin(context, "circular_pattern")
    assert precheck.success and transaction is not None

    model.volume = 0.009
    model.features = 4
    postcondition = guard.verify_after(transaction, "circular_pattern")
    assert postcondition.success
    assert postcondition.checks["geometry_changed"]
    assert postcondition.checks["expected_volume_direction"]


def test_threaded_hole_uses_subtractive_transaction_and_rollback_gate() -> None:
    guard = ModelIntegrityGuard()

    assert guard.requires_transaction("threaded_hole")
    assert "threaded_hole" in guard.SUBTRACTIVE_OPERATIONS


def test_postcondition_rejects_noop_and_rebuild_error(tmp_path: Path) -> None:
    model_path = tmp_path / "part.SLDPRT"
    model_path.write_bytes(b"before")
    model = FakeModel(model_path, volume=0.01, features=3)
    connector = FakeConnector(FakeApp(model))
    guard = ModelIntegrityGuard(connector_factory=lambda: connector)
    context = _context(tmp_path, model_path, ["fillet"])
    transaction, precheck = guard.begin(context, "fillet")
    assert precheck.success and transaction is not None

    noop = guard.verify_after(transaction, "fillet")
    assert not noop.success
    assert not noop.checks["geometry_changed"]

    model.features = 4
    model.rebuild_error = True
    rebuild_failure = guard.verify_after(transaction, "fillet")
    assert not rebuild_failure.success
    assert not rebuild_failure.checks["no_rebuild_errors"]


def test_rollback_restores_checkpoint_and_reopens_task_model(tmp_path: Path) -> None:
    model_path = tmp_path / "part.SLDPRT"
    model_path.write_bytes(b"valid-before")
    model = FakeModel(model_path, volume=0.01, features=3)
    app = FakeApp(model)
    connector = FakeConnector(app)
    guard = ModelIntegrityGuard(connector_factory=lambda: connector)
    context = _context(tmp_path, model_path, ["pocket"])
    transaction, precheck = guard.begin(context, "pocket")
    assert precheck.success and transaction is not None

    model_path.write_bytes(b"broken-after")
    rollback = guard.rollback(context, transaction, "postcondition failed")
    assert rollback.success
    assert model_path.read_bytes() == b"valid-before"
    assert connector.opened == [str(model_path.resolve())]
    assert app.closed == [model_path.name]


def test_final_save_gate_requires_all_features_and_valid_active_part(tmp_path: Path) -> None:
    model_path = tmp_path / "part.SLDPRT"
    model_path.write_bytes(b"valid")
    model = FakeModel(model_path, volume=0.01, features=4)
    guard = ModelIntegrityGuard(connector_factory=lambda: FakeConnector(FakeApp(model)))
    context = _context(tmp_path, model_path, ["base_plate", "through_hole", "save_sldprt"])
    context.step_records = [SimpleNamespace(skill_key="base_plate", status="success", required=True)]

    blocked = guard.validate_final_save(context)
    assert not blocked.success
    assert blocked.data["missing_skills"] == ["through_hole"]

    context.step_records.append(SimpleNamespace(skill_key="through_hole", status="success", required=True))
    accepted = guard.validate_final_save(context)
    assert accepted.success
    assert accepted.checks["positive_volume"]


def test_final_save_gate_rejects_wrong_active_document(tmp_path: Path) -> None:
    expected = tmp_path / "expected.SLDPRT"
    other = tmp_path / "other.SLDPRT"
    expected.write_bytes(b"valid")
    other.write_bytes(b"other")
    model = FakeModel(other, volume=0.01, features=3)
    guard = ModelIntegrityGuard(connector_factory=lambda: FakeConnector(FakeApp(model)))
    context = _context(tmp_path, expected, ["base_plate", "save_sldprt"])
    context.step_records = [SimpleNamespace(skill_key="base_plate", status="success", required=True)]
    result = guard.validate_final_save(context)
    assert not result.success
    assert not result.checks["task_model_active"]


class FakePipelineGuard:
    def __init__(self, *, save_allowed: bool = True) -> None:
        self.save_allowed = save_allowed
        self.calls: list[str] = []

    def requires_transaction(self, skill_key: str) -> bool:
        return skill_key == "base_plate"

    def begin(self, _context: object, _skill_key: str) -> tuple[None, object]:
        raise AssertionError("base_plate must not begin from an existing model")

    def verify_after(self, _transaction: object, _skill_key: str):
        self.calls.append("verify_after")
        from cad_agent.model_integrity import IntegrityResult

        return IntegrityResult(True, "feature_postcondition_passed", "ok")

    def commit(self, _context: object, _transaction: object, _skill_key: str):
        self.calls.append("commit")
        from cad_agent.model_integrity import IntegrityResult

        return IntegrityResult(True, "checkpoint_committed", "ok")

    def rollback(self, _context: object, _transaction: object, _reason: str):
        self.calls.append("rollback")
        from cad_agent.model_integrity import IntegrityResult

        return IntegrityResult(True, "rollback_not_required", "ok")

    def validate_final_save(self, _context: object):
        self.calls.append("validate_final_save")
        from cad_agent.model_integrity import IntegrityResult

        return IntegrityResult(self.save_allowed, "final_save_gate_passed" if self.save_allowed else "final_save_gate_failed", "ok" if self.save_allowed else "blocked")


class FakeLifecycleAdapter(SkillLifecycleAdapter):
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.executed = 0

    def execute(self, _context: object, _step: object) -> LifecycleResult:
        self.executed += 1
        return LifecycleResult(self.success, "executed" if self.success else "failed")


def _pipeline_executor(tmp_path: Path, guard: FakePipelineGuard) -> PipelineExecutor:
    return PipelineExecutor(
        CADAgentSkillManager(tmp_path),
        PipelineLogger(tmp_path / "pipeline.jsonl"),
        execute_real_skills=True,
        model_integrity_guard=guard,  # type: ignore[arg-type]
    )


def test_pipeline_boundary_verifies_and_commits_geometry(tmp_path: Path) -> None:
    guard = FakePipelineGuard()
    executor = _pipeline_executor(tmp_path, guard)
    adapter = FakeLifecycleAdapter()
    executor.adapters["base_plate"] = adapter
    context = PipelineContext("test", tmp_path, tmp_path)
    context.design_json = {
        "execution_policy": {"allowed_skills": ["base_plate"], "expected_outputs": ["SLDPRT"]}
    }
    step = PlannedSkillStep("base_plate", "create", "test", {}, True)
    record = PipelineStepRecord("base_plate", "create", True)
    context.step_records.append(record)
    executor._execute_step(context, step, record)
    assert record.status == "success"
    assert guard.calls == ["verify_after", "commit"]
    assert adapter.executed == 1


def test_pipeline_boundary_blocks_save_before_adapter_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = FakePipelineGuard(save_allowed=False)
    executor = _pipeline_executor(tmp_path, guard)
    monkeypatch.setattr(
        executor,
        "_activate_expected_part",
        lambda _context: LifecycleResult(True, "expected Part active"),
    )
    adapter = FakeLifecycleAdapter()
    executor.adapters["save_sldprt"] = adapter
    context = PipelineContext("test", tmp_path, tmp_path)
    context.design_json = {
        "execution_policy": {"allowed_skills": ["save_sldprt"], "expected_outputs": ["SLDPRT"]}
    }
    step = PlannedSkillStep("save_sldprt", "save", "test", {}, True)
    record = PipelineStepRecord("save_sldprt", "save", True)
    context.step_records.append(record)
    executor._execute_step(context, step, record)
    assert record.status == "failed"
    assert record.lifecycle["prepare"] == "failed"
    assert adapter.executed == 0
    assert guard.calls == ["validate_final_save"]
