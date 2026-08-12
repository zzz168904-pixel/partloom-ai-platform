from __future__ import annotations

import argparse
import json
import re
import tomllib
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
REQUIRED_ROOT_FILES = {
    "CAPABILITIES.md",
    "COMMERCIAL_LICENSE.md",
    "COMPATIBILITY.md",
    "CONTRIBUTING.md",
    "CONTRIBUTOR_POLICY.md",
    "DISCLAIMER.md",
    "INSTALL.md",
    "LICENSE",
    "LICENSE_HISTORY.md",
    "NOTICE",
    "README.md",
    "RELEASE_POLICY.md",
    "SECURITY.md",
    "SOURCE_AVAILABLE_SCOPE.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "pyproject.toml",
}
OFFICIAL_REPOSITORY = "https://github.com/zzz168904-pixel/partloom-ai-platform"
EXPECTED_LICENSE = "PolyForm-Noncommercial-1.0.0"
ACTION_REF_PATTERN = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s#]+)", re.MULTILINE)


def _release_version_from_pep440(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)b(\d+)", version)
    return f"{match.group(1)}-beta.{match.group(2)}" if match else version


def _dependency_name(requirement: str) -> str:
    return re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def metadata_issues(root: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    for name in sorted(REQUIRED_ROOT_FILES):
        if not (root / name).is_file():
            issues.append({"kind": "missing_release_file", "path": name, "detail": name})

    if issues:
        return issues

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    release_version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if _release_version_from_pep440(str(project.get("version", ""))) != release_version:
        issues.append(
            {
                "kind": "version_mismatch",
                "path": "pyproject.toml",
                "detail": f"pyproject={project.get('version')} VERSION={release_version}",
            }
        )
    if project.get("license") != EXPECTED_LICENSE:
        issues.append(
            {
                "kind": "license_metadata_mismatch",
                "path": "pyproject.toml",
                "detail": str(project.get("license")),
            }
        )

    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    notice = (root / "NOTICE").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    third_party = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
    if "PolyForm Noncommercial License 1.0.0" not in license_text:
        issues.append({"kind": "license_text_mismatch", "path": "LICENSE", "detail": EXPECTED_LICENSE})
    if "Required Notice:" not in notice:
        issues.append({"kind": "required_notice_missing", "path": "NOTICE", "detail": "Required Notice:"})
    if OFFICIAL_REPOSITORY not in readme:
        issues.append({"kind": "official_repository_missing", "path": "README.md", "detail": OFFICIAL_REPOSITORY})
    if "源码可见" not in readme or "COMMERCIAL_LICENSE.md" not in readme:
        issues.append({"kind": "distribution_boundary_missing", "path": "README.md", "detail": "source-available/commercial boundary"})

    for requirement in project.get("dependencies", []):
        dependency = _dependency_name(str(requirement))
        if f"`{dependency}`" not in third_party:
            issues.append(
                {
                    "kind": "dependency_notice_missing",
                    "path": "THIRD_PARTY_NOTICES.md",
                    "detail": dependency,
                }
            )

    for workflow in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        if "pull_request_target" in text or "self-hosted" in text:
            issues.append(
                {
                    "kind": "unsafe_ci_trigger_or_runner",
                    "path": str(workflow.relative_to(root)),
                    "detail": "pull_request_target/self-hosted",
                }
            )
        for action_ref in ACTION_REF_PATTERN.findall(text):
            if action_ref.startswith("./"):
                continue
            if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", action_ref):
                issues.append(
                    {
                        "kind": "unpinned_github_action",
                        "path": str(workflow.relative_to(root)),
                        "detail": action_ref,
                    }
                )
    return issues


def should_skip(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part.lower() in EXCLUDED_DIRS for part in relative.parts)


def audit(root: Path) -> dict[str, object]:
    issues: list[dict[str, str]] = metadata_issues(root)
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
