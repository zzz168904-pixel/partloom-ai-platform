from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient

from cad_agent.gateway.models import PlanTaskRequest
from cad_agent.gateway.server import create_app
from cad_agent.gateway.service import GatewayTaskCoordinator


MODEL_ONLY = "\u521b\u5efa\u4e00\u4e2a100x60x10mm\u5e95\u677f\uff0c\u4e2d\u5fc3\u5f00\u76f4\u5f8420mm\u8d2f\u7a7f\u5b54\uff0c\u53ea\u751f\u62103D\u96f6\u4ef6"


def wait_for_terminal(coordinator: GatewayTaskCoordinator, task_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        snapshot = coordinator.snapshot(task_id, include_events=True)
        if snapshot["status"] in {"success", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.05)
    raise AssertionError("Gateway task did not finish in time")


def test_gateway_plan_confirm_simulated() -> None:
    with tempfile.TemporaryDirectory() as temp:
        coordinator = GatewayTaskCoordinator(Path(temp))
        planned = coordinator.plan(
            PlanTaskRequest(
                source_type="text",
                prompt=MODEL_ONLY,
                provider="local_fallback",
                execute_real_skills=False,
            )
        )
        assert planned["status"] == "awaiting_confirmation"
        assert planned["provider"]["id"] == "local_fallback"
        assert planned["design_json"]["requested_stages"] == ["model_3d"]
        assert "drawing" not in planned["design_json"]["requested_stages"]
        assert set(planned["design_json"]["execution_policy"]["expected_outputs"]) == {"SLDPRT"}
        coordinator.confirm(planned["task_id"])
        finished = wait_for_terminal(coordinator, planned["task_id"])
        assert finished["status"] == "success", finished.get("traceback")
        assert finished["report_path"] and Path(finished["report_path"]).is_file()
        assert any(item["event"] == "task_confirmed" for item in finished["events"])
        assert any(item["event"] == "task_finished" for item in finished["events"])


def test_gateway_cancel_before_confirmation() -> None:
    with tempfile.TemporaryDirectory() as temp:
        coordinator = GatewayTaskCoordinator(Path(temp))
        planned = coordinator.plan(
            PlanTaskRequest(source_type="text", prompt=MODEL_ONLY, provider="local_fallback", execute_real_skills=False)
        )
        cancelled = coordinator.cancel(planned["task_id"])
        assert cancelled["status"] == "cancelled"


def test_gateway_http_contract() -> None:
    with tempfile.TemporaryDirectory() as temp:
        coordinator = GatewayTaskCoordinator(Path(temp))
        client = TestClient(create_app(coordinator, token="test-token"))
        assert client.get("/health").status_code == 200
        assert client.get("/v1/providers").status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        providers = client.get("/v1/providers", headers=headers)
        assert providers.status_code == 200
        assert any(item["id"] == "local_fallback" for item in providers.json()["providers"])
        roadmap = client.get("/v1/project/roadmap", headers=headers)
        assert roadmap.status_code == 200
        assert roadmap.json()["current_phase"]["id"] == "public_beta"
        assert roadmap.json()["current_task_validation"]["success"] is True
        branch_task = {
            "schema_version": "cad_agent.development_task.v1",
            "id": "branch-doc-fix",
            "title": "Correct current-phase evidence documentation",
            "task_kind": "branch",
            "phase_id": "public_beta",
            "objective_id": "release_hardening",
            "status": "planned",
            "scope": ["Correct evidence metadata only."],
            "requested_changes": ["update_evidence"],
            "expected_artifacts": ["Corrected evidence record"],
            "acceptance_tests": ["Project governance tests pass"],
            "branch": {
                "parent_task_id": "partloom_public_beta_010",
                "branch_type": "documentation",
                "supports_objective_id": "release_hardening",
                "reason": "Evidence metadata blocks the current baseline.",
                "max_iterations": 1,
                "return_to_mainline": True,
            },
        }
        validated = client.post("/v1/project/tasks/validate", headers=headers, json=branch_task)
        assert validated.status_code == 200
        assert validated.json()["success"] is True
        response = client.post(
            "/v1/tasks/plan",
            headers=headers,
            json={
                "source_type": "text",
                "prompt": MODEL_ONLY,
                "provider": "local_fallback",
                "execute_real_skills": False,
            },
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        events = client.get(f"/v1/tasks/{task_id}/events", headers=headers)
        assert events.status_code == 200
        assert events.json()["events"]


if __name__ == "__main__":
    test_gateway_plan_confirm_simulated()
    test_gateway_cancel_before_confirmation()
    test_gateway_http_contract()
    print("Agent Gateway tests passed")
