from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..project_governance import ProjectGovernance
from .models import PlanTaskRequest
from .service import GatewayTaskCoordinator, TERMINAL_STATES


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "gateway_tasks"


def token_file() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SolidWorksAIAgent"
    root.mkdir(parents=True, exist_ok=True)
    return root / "gateway.token"


def load_or_create_token(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    environment = os.environ.get("CAD_AGENT_GATEWAY_TOKEN", "").strip()
    if environment:
        return environment
    path = token_file()
    if path.exists():
        value = path.read_text(encoding="ascii", errors="ignore").strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="ascii")
    return value


def create_app(
    coordinator: GatewayTaskCoordinator | None = None,
    token: str | None = None,
    governance: ProjectGovernance | None = None,
) -> FastAPI:
    task_coordinator = coordinator or GatewayTaskCoordinator(DEFAULT_OUTPUT_ROOT)
    project_governance = governance or ProjectGovernance.load_default()
    expected_token = load_or_create_token(token)
    app = FastAPI(title="PartLoom AI Gateway", version="0.1.0-beta")
    app.state.coordinator = task_coordinator
    app.state.project_governance = project_governance
    app.state.token = expected_token

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {expected_token}":
            raise HTTPException(status_code=401, detail="Invalid gateway token")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ready",
            "service": "ai-cad-agent-gateway",
            "project_root": str(PROJECT_ROOT),
            "output_root": str(task_coordinator.output_root),
        }

    @app.get("/v1/providers", dependencies=[Depends(authorize)])
    def providers() -> dict[str, Any]:
        return {"providers": task_coordinator.providers()}

    @app.get("/v1/project/roadmap", dependencies=[Depends(authorize)])
    def project_roadmap() -> dict[str, Any]:
        return project_governance.status()

    @app.post("/v1/project/tasks/validate", dependencies=[Depends(authorize)])
    def validate_project_task(task: dict[str, Any]) -> dict[str, Any]:
        return project_governance.validate_task(task).as_dict()

    @app.post("/v1/tasks/plan", dependencies=[Depends(authorize)])
    def plan(request: PlanTaskRequest) -> dict[str, Any]:
        return task_coordinator.plan(request)

    @app.post("/v1/tasks/{task_id}/confirm", dependencies=[Depends(authorize)])
    def confirm(task_id: str) -> dict[str, Any]:
        try:
            return task_coordinator.confirm(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(authorize)])
    def cancel(task_id: str) -> dict[str, Any]:
        try:
            return task_coordinator.cancel(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc

    @app.post("/v1/tasks/{task_id}/retry", dependencies=[Depends(authorize)])
    def retry(task_id: str) -> dict[str, Any]:
        try:
            return task_coordinator.retry(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(authorize)])
    def task(task_id: str, include_events: bool = False) -> dict[str, Any]:
        try:
            return task_coordinator.snapshot(task_id, include_events=include_events)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc

    @app.get("/v1/tasks/{task_id}/events", dependencies=[Depends(authorize)])
    async def events(
        task_id: str,
        request: Request,
        after: int = Query(default=-1),
        stream: bool = Query(default=False),
    ) -> Any:
        try:
            task_coordinator.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        wants_stream = stream or "text/event-stream" in request.headers.get("accept", "")
        if not wants_stream:
            items = task_coordinator.events(task_id, after=after)
            return {"events": items, "next_after": items[-1]["index"] if items else after}

        async def generate() -> Any:
            cursor = after
            while True:
                items = task_coordinator.events(task_id, after=cursor)
                for item in items:
                    cursor = int(item["index"])
                    yield f"id: {cursor}\nevent: {item.get('event', 'message')}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                snapshot = task_coordinator.snapshot(task_id)
                if snapshot["status"] in TERMINAL_STATES and not items:
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the localhost PartLoom AI Gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAD_AGENT_GATEWAY_PORT", "8765")))
    parser.add_argument("--token", default=None)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The CAD Agent Gateway may only bind to localhost.")
    import uvicorn

    app = create_app(GatewayTaskCoordinator(args.output_root), token=args.token)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
