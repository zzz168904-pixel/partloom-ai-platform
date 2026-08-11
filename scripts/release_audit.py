from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


EXCLUDED_DIRS = {
    ".build-venv",
    ".git",
    ".pytest_cache",
    "__pycache__",
    "artifacts",
    "build",
    "dist",
}
FORBIDDEN_DIRS = {
    "cad_test_dataset",
    "copies",
    "external",
    "logs",
    "output",
    "reports",
    "tmp",
}
FORBIDDEN_SUFFIXES = {
    ".bmp",
    ".dwg",
    ".dxf",
    ".gif",
    ".iges",
    ".igs",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".sldasm",
    ".slddrw",
    ".sldprt",
    ".step",
    ".stl",
    ".stp",
    ".webp",
    ".x_b",
    ".x_t",
}
TEXT_SUFFIXES = {
    "",
    ".cs",
    ".csproj",
    ".example",
    ".iss",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".spec",
    ".toml",
    ".txt",
    ".xml",
    ".yml",
    ".yaml",
}
FORBIDDEN_PATTERNS = {
    "api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "personal_user_path": re.compile(r"C:\\Users\\Lenovo", re.IGNORECASE),
    "private_workspace": re.compile(r"F:\\iwen-codex", re.IGNORECASE),
    "wechat_path": re.compile(r"(?:xwechat_files|wxid_)", re.IGNORECASE),
    "private_reference_library": re.compile(
        r"D:\\(?:精选零件|BaiduNetdiskDownload)", re.IGNORECASE
    ),
    "legacy_tool_path": re.compile(r"D:\\(?:AI_CAD|2025sdw)", re.IGNORECASE),
    "legacy_output_name": re.compile(r"SolidWorks_AI_Output", re.IGNORECASE),
    "customer_fixture_id": re.compile(r"(?:1178-1222001|1222001)"),
    "nonempty_env_secret": re.compile(
        r"(?im)^(?:OPENAI|DEEPSEEK|ANTHROPIC|GEMINI|DASHSCOPE)_API_KEY[ \t]*=[ \t]*[^\s#]+"
    ),
}


def should_skip(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part.lower() in EXCLUDED_DIRS for part in relative.parts)


def audit(root: Path) -> dict[str, object]:
    issues: list[dict[str, str]] = []
    checked_files = 0

    for path in sorted(root.rglob("*")):
        if should_skip(path, root):
            continue
        relative = path.relative_to(root)
        lower_parts = {part.lower() for part in relative.parts}
        if path.is_dir():
            forbidden = lower_parts & FORBIDDEN_DIRS
            contains_files = any(
                child.is_file() and not should_skip(child, root)
                for child in path.rglob("*")
            )
            if forbidden and contains_files:
                issues.append(
                    {
                        "kind": "forbidden_directory",
                        "path": str(relative),
                        "detail": ", ".join(sorted(forbidden)),
                    }
                )
            continue

        checked_files += 1
        if path.name.lower() == ".env.local":
            issues.append(
                {"kind": "secret_file", "path": str(relative), "detail": ".env.local"}
            )
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append(
                {
                    "kind": "private_or_generated_asset",
                    "path": str(relative),
                    "detail": path.suffix.lower(),
                }
            )
        if "examples" in lower_parts:
            if path.suffix.lower() != ".json" or "selected" in path.name.lower():
                issues.append(
                    {
                        "kind": "non_synthetic_example",
                        "path": str(relative),
                        "detail": "Public examples must be synthetic JSON CAD-IR files.",
                    }
                )

        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if relative.as_posix() == "scripts/release_audit.py":
            continue
        for name, pattern in FORBIDDEN_PATTERNS.items():
            if pattern.search(content):
                issues.append(
                    {"kind": name, "path": str(relative), "detail": pattern.pattern}
                )

    return {
        "schema_version": "partloom.release_audit.v1",
        "root": str(root),
        "checked_files": checked_files,
        "issue_count": len(issues),
        "passed": not issues,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    result = audit(root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"PartLoom release audit: checked={result['checked_files']} "
            f"issues={result['issue_count']} passed={result['passed']}"
        )
        for issue in result["issues"]:
            print(f"- [{issue['kind']}] {issue['path']}: {issue['detail']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
