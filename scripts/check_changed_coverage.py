#!/usr/bin/env python3
"""Enforce changed-line coverage and UI color-token checks for pull requests."""

import argparse
import re
import shutil
import subprocess
import sys

from Backend.scripts.coverage_threshold import (
    CHANGED_LINE_COVERAGE_MINIMUM,
    meets_changed_line_coverage,
)
from defusedxml import ElementTree as ET

HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
COLOR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(",
    re.IGNORECASE,
)


def _repo_path(path: str, prefix: str) -> str:
    path = path.replace("\\", "/")
    if path.startswith(prefix + "/"):
        return path
    return f"{prefix}/{path.lstrip('/')}"


def _changed_lines(base: str) -> tuple[dict[str, set[int]], list[tuple[str, int, str]]]:
    if base == "0" * 40:
        base = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
        raise ValueError("Coverage base must be a full Git commit id.")
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("Changed-line coverage requires Git.")
    diff = subprocess.run(  # nosec B603; fixed args and validated commit id, no shell.  # noqa: S603
        [
            git_executable,
            "diff",
            "--no-ext-diff",
            "--unified=0",
            f"{base}...HEAD",
            "--",
            "Backend/app",
            "Frontend/src",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    files: dict[str, set[int]] = {}
    additions: list[tuple[str, int, str]] = []
    path = None
    line_number = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            if (
                not (path.startswith("Backend/app/") or path.startswith("Frontend/src/"))
                or "/__tests__/" in path
                or "/generated/" in path  # generated API types (openapi-typescript), not hand-written code
                or re.search(r"\.(?:test|spec)\.[^.]+$", path)
            ):
                path = None
            continue
        hunk = HUNK.match(line)
        if hunk:
            line_number = int(hunk.group(1))
            continue
        if path is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            files.setdefault(path, set()).add(line_number)
            additions.append((path, line_number, line[1:]))
            line_number += 1
        elif line.startswith(" "):
            line_number += 1
    return files, additions


def _python_coverage(report: str) -> dict[str, dict[int, bool]]:
    root = ET.parse(report).getroot()
    files = {}
    for file_node in root.findall(".//class"):
        filename = file_node.attrib["filename"].replace("\\", "/")
        if "/app/" in filename:
            path = "Backend/app/" + filename.rsplit("/app/", 1)[1]
        elif filename.startswith("app/"):
            path = f"Backend/{filename}"
        else:
            path = f"Backend/app/{filename.lstrip('/')}"
        files[path] = {
            int(line.attrib["number"]): int(line.attrib["hits"]) > 0
            for line in file_node.findall("./lines/line")
        }
    return files


def _frontend_coverage(report: str) -> dict[str, dict[int, bool]]:
    files: dict[str, dict[int, bool]] = {}
    source = None
    with open(report, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line.startswith("SF:"):
                source = _repo_path(line[3:], "Frontend")
                files[source] = {}
            elif line.startswith("DA:") and source:
                line_number, hits, *_ = line[3:].split(",")
                files[source][int(line_number)] = int(hits) > 0
            elif line == "end_of_record":
                source = None
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Merge-base ref used to calculate changed lines.")
    parser.add_argument("--python-report", required=True, help="coverage.py XML report.")
    parser.add_argument("--frontend-report", required=True, help="Vitest lcov.info report.")
    args = parser.parse_args()

    changed, additions = _changed_lines(args.base)
    coverage = _python_coverage(args.python_report)
    coverage.update(_frontend_coverage(args.frontend_report))

    executable = []
    uncovered_color_literals = []
    for path, lines in changed.items():
        if not path.endswith((".py", ".js", ".jsx", ".ts", ".tsx")):
            continue
        report = coverage.get(path)
        if report is None:
            executable.extend(
                (path, line, False)
                for added_path, line, text in additions
                if added_path == path
                and text.strip()
                and not text.lstrip().startswith(("#", "//", "/*", "*"))
            )
            continue
        executable.extend((path, line, report[line]) for line in lines if line in report)

    for path, line, text in additions:
        if path.startswith("Frontend/src/") and "/__tests__/" not in path:
            is_token_definition = path.endswith(".css") and re.match(r"\s*--[\w-]+\s*:", text)
            if COLOR.search(text) and not is_token_definition:
                uncovered_color_literals.append(f"{path}:{line}")

    if uncovered_color_literals:
        sys.stdout.write("New hardcoded UI color literals are prohibited; use a design token:\n")
        sys.stdout.write("\n".join(uncovered_color_literals) + "\n")
        return 1

    if not executable:
        sys.stdout.write(
            "No changed executable lines were found in Backend/app or Frontend/src; "
            "coverage gate is N/A.\n"
        )
        return 0

    covered = sum(hit for _, _, hit in executable)
    percent = covered * 100 / len(executable)
    sys.stdout.write(f"Changed executable lines covered: {covered}/{len(executable)} " f"({percent:.1f}%).\n")
    for path, line, hit in executable:
        if not hit:
            sys.stdout.write(f"Uncovered: {path}:{line}\n")
    if not meets_changed_line_coverage(covered, len(executable)):
        sys.stdout.write(f"Changed-line coverage must be at least " f"{CHANGED_LINE_COVERAGE_MINIMUM}%.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
