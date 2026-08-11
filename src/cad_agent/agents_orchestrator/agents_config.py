from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from typing import Any

from ..runtime_config import load_runtime_environment


@dataclass(frozen=True)
class AgentsOrchestratorConfig:
    """Optional OpenAI Agents SDK configuration.

    The CAD pipeline remains authoritative. This layer only plans, routes,
    validates, and proposes recovery steps.
    """

    model: str = "gpt-4.1-mini"
    provider: str = "openai-agents-python"
    sdk_available: bool = False
    api_key_configured: bool = False
    deepseek_api_key_configured: bool = False
    deepseek_model: str = "deepseek-v4-pro"

    @classmethod
    def detect(cls) -> "AgentsOrchestratorConfig":
        load_runtime_environment()
        return cls(
            model=os.environ.get("CAD_AGENT_OPENAI_MODEL", "gpt-4.1-mini"),
            sdk_available=importlib.util.find_spec("agents") is not None,
            api_key_configured=bool(os.environ.get("OPENAI_API_KEY")),
            deepseek_api_key_configured=bool(os.environ.get("DEEPSEEK_API_KEY")),
            deepseek_model=os.environ.get("CAD_AGENT_DEEPSEEK_MODEL", "deepseek-v4-pro"),
        )

    @property
    def mode(self) -> str:
        if self.sdk_ready:
            return "openai-agents-sdk"
        if self.deepseek_api_key_configured:
            return "provider-registry"
        return "local-fallback"

    @property
    def provider_name(self) -> str:
        if self.sdk_ready:
            return "openai_agents_sdk"
        if self.deepseek_api_key_configured:
            return "deepseek"
        return "local_fallback"

    @property
    def sdk_ready(self) -> bool:
        return self.sdk_available and self.api_key_configured

    def status_line(self) -> str:
        if self.sdk_ready:
            return f"Agents Orchestrator: ready, provider=openai_agents_sdk, model={self.model}, OPENAI_API_KEY=configured"
        if self.deepseek_api_key_configured:
            return f"Agents Orchestrator: ready, provider=deepseek, model={self.deepseek_model}, DEEPSEEK_API_KEY=configured"
        if self.sdk_available:
            return "Agents Orchestrator: SDK installed, OPENAI_API_KEY not configured, provider=local_fallback"
        return "Agents Orchestrator: SDK unavailable, fallback to existing CAD Agent Core"

    def create_sdk_agent(self, name: str, instructions: str) -> Any | None:
        """Create an OpenAI Agents SDK Agent when the optional package exists.

        This keeps SDK usage isolated from the CAD execution pipeline. If the
        package API changes or is unavailable, the local fallback remains active.
        """

        if not self.sdk_ready:
            return None
        try:
            from agents import Agent  # type: ignore

            return Agent(name=name, instructions=instructions, model=self.model)
        except Exception:
            return None
