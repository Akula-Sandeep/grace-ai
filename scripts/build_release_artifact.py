"""Build a clean source release from tracked Git files and reject secret-bearing artifacts."""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = {
    ".git",
    "node_modules",
    "dist",
    "test-results",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "venv",
    ".venv",
    "ops/secrets",
}
FORBIDDEN_NAMES = {".env"}
SECRET_ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:JWT_SECRET|JWT_SIGNING_KEY|DATABASE_URL|REDIS_URL|REDIS_PASSWORD|"
    r"TMDB_API_KEY|OMDB_API_KEY|GROQ_API_KEY|GEMINI_API_KEY|VAPID_PRIVATE_KEY|"
    r"SMTP_PASSWORD|METRICS_BEARER_TOKEN|GRAFANA_ADMIN_PASSWORD)\s*=\s*(\S+)"
)
PLACEHOLDER_PREFIXES = (
    "replace-with-",
    "ci-",
    "your-",
    "example",
    "<",
)


def run_git(*args: str) -> bytes:
    # Fixed executable and argument vector; no shell and no user-controlled input.
    command = ["git", *args]
    return subprocess.check_output(command, stderr=subprocess.STDOUT)  # noqa: S603


def repo_root() -> Path:
    return Path(run_git("rev-parse", "--show-toplevel").decode().strip())


def tracked_files(root: Path) -> list[str]:
    raw = run_git("ls-files", "-z")
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def is_forbidden(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.name in FORBIDDEN_NAMES and not p.name.endswith(".example"):
        return True
    if any(part in FORBIDDEN_PARTS for part in p.parts):
        return True
    if p.parts[:2] == ("ops", "secrets"):
        return True
    return False


def validate_paths(paths: list[str]) -> None:
    blocked = sorted(path for path in paths if is_forbidden(path))
    if blocked:
        raise RuntimeError("Forbidden release paths are tracked:\n" + "\n".join(blocked))


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower().strip("\"'")
    return (
        lowered in {"changeme", "none", "null", "false"}
        or lowered.startswith(PLACEHOLDER_PREFIXES)
        or "ci-" in lowered
        or "replace-with" in lowered
        or "example.invalid" in lowered
        or "user:password@localhost" in lowered
        or "localhost:" in lowered
    )


def scan_tracked_content(root: Path, paths: list[str]) -> None:
    findings: list[str] = []
    for relative in paths:
        path = root / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if relative.endswith((".example", ".example.txt")) or ".env.example" in relative:
            continue
        for match in SECRET_ASSIGNMENT.finditer(text):
            value = match.group(1).strip().strip("\"'")
            if not looks_like_placeholder(value):
                findings.append(f"{relative}: {match.group(0).strip()[:120]}")
    if findings:
        raise RuntimeError(
            "Potential secret assignments found in tracked release content:\n" + "\n".join(findings)
        )


def _release_prefix() -> str:
    return f"Grace-Source-{run_git('rev-parse', '--short', 'HEAD').decode().strip()}"


def build_tar(root: Path, paths: list[str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = _release_prefix()
    with tarfile.open(output, "w:gz") as archive:
        for relative in paths:
            source = root / relative
            if not source.exists() or not source.is_file():
                continue
            info = archive.gettarinfo(str(source), arcname=f"{prefix}/{relative}")
            archive.addfile(info, io.BytesIO(source.read_bytes()))


def build_zip(root: Path, paths: list[str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = _release_prefix()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in paths:
            source = root / relative
            if not source.exists() or not source.is_file():
                continue
            archive.write(source, arcname=f"{prefix}/{relative}")


def build_release(root: Path, paths: list[str], output: Path) -> None:
    if output.name.lower().endswith(".zip"):
        build_zip(root, paths, output)
    else:
        build_tar(root, paths, output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="release/grace-source.tar.gz")
    args = parser.parse_args()
    root = repo_root()
    paths = tracked_files(root)
    validate_paths(paths)
    scan_tracked_content(root, paths)
    output = root / args.output
    build_release(root, paths, output)
    print(f"Release artifact: {output}")  # noqa: T201 - CLI output
    print(f"Tracked files packaged: {len(paths)}")  # noqa: T201 - CLI output
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        message = f"Release build failed: {exc}"
        print(message, file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc
