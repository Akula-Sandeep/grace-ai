"""Reject tracked local databases, test artifacts, generated builds, and debug scripts."""

import shutil
import subprocess
import sys
from pathlib import PurePosixPath

BLOCKED_NAMES = {
    ".coverage",
    "agent_output.txt",
    "ci-only-metrics",
    "coverage.xml",
    "pytest_failures.txt",
}
BLOCKED_DIRECTORIES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "coverage",
    "test-results",
    # Local work output (see .gitignore): eval transcripts, files set aside for deletion.
    "eval-reports",
    "_to_delete",
    ".archify",
}


def is_blocked_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    parts = path.parts
    if path.name in BLOCKED_NAMES or path.name.startswith(".coverage."):
        return True
    if any(part in BLOCKED_DIRECTORIES for part in parts):
        return True
    if any(part in {"venv", ".venv"} for part in parts):
        return True
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".dump"}:
        return True
    if path.name.endswith((".db-wal", ".db-shm")):
        return True
    if path.name.startswith("debug_") and path.suffix == ".py":
        return True
    return "Frontend" in parts and "dist" in parts


def blocked_tracked_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if is_blocked_path(path)]


def main() -> int:
    git_executable = shutil.which("git")
    if git_executable is None:
        print("Repository hygiene check requires Git.", file=sys.stderr)
        return 2
    result = subprocess.run(  # nosec B603; fixed Git args, resolved executable, no shell.  # noqa: S603
        [git_executable, "ls-files", "-z"], check=True, capture_output=True
    )
    paths = [path.decode("utf-8", errors="strict") for path in result.stdout.split(b"\0") if path]
    blocked = blocked_tracked_paths(paths)
    if blocked:
        print("Tracked local artifacts found:", file=sys.stderr)
        print("\n".join(sorted(blocked)), file=sys.stderr)
        return 1
    print(f"Repository hygiene passed for {len(paths)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
