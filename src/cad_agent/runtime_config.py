from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG_FILES = (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local")
_LOADED = False


def application_data_dir() -> Path:
    configured = os.environ.get("PARTLOOM_APP_DATA", "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "PartLoomAI"


def output_root() -> Path:
    configured = os.environ.get("PARTLOOM_OUTPUT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "PartLoom_AI_Output"


def load_runtime_environment(force: bool = False) -> list[str]:
    """Load ignored machine-local configuration without overriding the shell."""
    global _LOADED
    if _LOADED and not force:
        return []
    loaded: list[str] = []
    try:
        from dotenv import load_dotenv  # type: ignore

        for path in LOCAL_CONFIG_FILES:
            if path.is_file():
                load_dotenv(path, override=False)
                loaded.append(str(path))
    except Exception:
        for path in LOCAL_CONFIG_FILES:
            if not path.is_file():
                continue
            _load_simple_env(path)
            loaded.append(str(path))
    _LOADED = True
    return loaded


def redact_secrets(value: Any) -> str:
    text = str(value)
    for key in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DASHSCOPE_API_KEY",
        "QWEN_API_KEY",
    ):
        secret = os.environ.get(key, "")
        if secret:
            text = text.replace(secret, f"<{key}:redacted>")
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<api-key:redacted>", text)


def _load_simple_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
