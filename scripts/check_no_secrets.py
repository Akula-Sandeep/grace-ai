"""Fail if provider credentials appear in the repository (they leaked once via .env.example).

Scans text files for well-known key formats and for non-placeholder values
assigned to secret-looking variables in example/config files. Never prints the
secret itself, only the file, line and kind.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed git invocation below
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "venv",
    ".venv",
    "__pycache__",
    "test-results",
    "playwright-report",
}
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".md",
    ".txt",
    ".env",
    ".example",
    ".cfg",
    ".ini",
    ".sh",
    ".conf",
}
PATTERNS = {
    "Groq API key": re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|BEARER)[A-Z0-9_]*)\s*[=:]\s*['\"]?(?P<value>[^'\"\s#]+)"
)
PLACEHOLDER = re.compile(
    r"^(?:|change[-_]?me.*|your[-_].*|<.*>|\$\{.*\}|x+|\*+|example.*|dummy.*|test.*|ci-only.*|replace.*)$",
    re.I,
)


def files() -> list[Path]:
    try:
        out = subprocess.run(  # nosec B603 B607
            ["git", "ls-files", "-z"],  # noqa: S607 - fixed args, no shell
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode()
        return [ROOT / name for name in out.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.parts) & SKIP_DIRS)]


def main() -> int:
    findings = []
    for path in files():
        if path.suffix not in TEXT_SUFFIXES and not path.name.startswith(".env"):
            continue
        if path.name == "check_no_secrets.py":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        example = path.name.startswith(".env") or path.suffix in {".example", ".env"}
        for number, line in enumerate(lines, 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{number}: {kind}")
            match = ASSIGNMENT.match(line) if example else None
            if match and not PLACEHOLDER.match(match.group("value")) and len(match.group("value")) >= 12:
                findings.append(
                    f"{path.relative_to(ROOT)}:{number}: non-placeholder value for {match.group('name')}"
                )
    for finding in findings:
        print(finding)  # noqa: T201 - CLI output
    print(f"secret scan: {len(findings)} finding(s)")  # noqa: T201 - CLI output
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
