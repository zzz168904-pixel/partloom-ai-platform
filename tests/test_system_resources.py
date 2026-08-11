from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.brain_models import BrainPlan
from cad_agent.pipeline.agent_pipeline_runner import AgentPipelineRunner
from cad_agent.system_resources import GIB, MemorySnapshot, evaluate_cad_resources


def _snapshot(*, commit_used: float, commit_limit: float, physical_free: float) -> MemorySnapshot:
    return MemorySnapshot(
        commit_used_bytes=int(commit_used * GIB),
        commit_limit_bytes=int(commit_limit * GIB),
        physical_available_bytes=int(physical_free * GIB),
        physical_total_bytes=16 * GIB,
    )


def test_cad_resource_preflight_accepts_healthy_memory() -> None:
    result = evaluate_cad_resources(_snapshot(commit_used=18, commit_limit=32, physical_free=5))

    assert result.ok
    assert result.reason == "resources_available"


def test_cad_resource_preflight_blocks_high_commit_ratio() -> None:
    result = evaluate_cad_resources(_snapshot(commit_used=26, commit_limit=28, physical_free=3))

    assert not result.ok
    assert "commit_ratio_high" in result.reason
    assert "启动 CAD 前阻断" in result.message


def test_cad_resource_preflight_blocks_low_commit_headroom() -> None:
    result = evaluate_cad_resources(_snapshot(commit_used=30.5, commit_limit=32, physical_free=4))

    assert not result.ok
    assert "commit_headroom_low" in result.reason


def test_cad_resource_preflight_blocks_low_physical_memory() -> None:
    result = evaluate_cad_resources(_snapshot(commit_used=20, commit_limit=32, physical_free=0.5))

    assert not result.ok
    assert "physical_memory_low" in result.reason


def test_missing_platform_probe_does_not_break_non_windows_planning() -> None:
    result = evaluate_cad_resources(None)

    assert result.ok
    assert result.reason == "resource_probe_unavailable"


def test_memory_report_uses_gigabytes_and_commit_percent() -> None:
    report = _snapshot(commit_used=24, commit_limit=30, physical_free=3).as_report()

    assert report["commit_free_gb"] == 6.0
    assert report["commit_percent"] == 80.0
    assert report["physical_available_gb"] == 3.0


def test_real_pipeline_stops_before_executor_when_resource_preflight_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import cad_agent.pipeline.agent_pipeline_runner as runner_module

    events: list[dict] = []
    executor_called = False

    class FakeBrain:
        def plan(self, prompt: str, stage_mode: str = "auto") -> BrainPlan:
            return BrainPlan(
                source_prompt=prompt,
                design_json={"part_family": "resource_guard_test"},
                skill_pipeline=[],
                model_provider="test",
            )

    class ForbiddenExecutor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def execute(self, context) -> None:
            nonlocal executor_called
            executor_called = True

    blocked = {
        "ok": False,
        "reason": "commit_ratio_high",
        "message": "blocked for test",
        "snapshot": {"commit_percent": 94.0, "commit_free_gb": 1.0},
    }
    monkeypatch.setattr(runner_module, "cad_resource_preflight", lambda: blocked)
    monkeypatch.setattr(runner_module, "PipelineExecutor", ForbiddenExecutor)
    monkeypatch.setattr(AgentPipelineRunner, "_publish_outputs", staticmethod(lambda *_args: None))

    runner = AgentPipelineRunner(
        tmp_path,
        skill_manager=object(),
        brain=FakeBrain(),
        execute_real_skills=True,
        event_callback=events.append,
    )
    context = runner.run("resource guard integration test")

    assert context.status == "failed"
    assert not executor_called
    assert context.final_model_validation["reason"] == "cad_resource_preflight_failed"
    assert context.report_path and context.report_path.exists()
    assert any(event.get("event") == "cad_resource_preflight_failed" for event in events)
