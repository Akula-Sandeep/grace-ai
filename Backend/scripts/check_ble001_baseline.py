"""Fail if broad exception handlers exceed the reviewed legacy baseline."""

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ERROR_BUDGET = BACKEND_DIR / "ble001-error-budget.txt"


def main() -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "BLE001",
            "--output-format",
            "json",
            "app",
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr, end="")
        return result.returncode

    try:
        findings = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"Ruff returned invalid JSON: {exc}", file=sys.stderr)
        return 2

    errors = sum(finding.get("code") == "BLE001" for finding in findings)
    budget = int(ERROR_BUDGET.read_text(encoding="utf-8").strip())
    if errors > budget:
        print(
            f"Ruff found {errors} broad exception handlers; " f"the reviewed legacy budget is {budget}.",
            file=sys.stderr,
        )
        return 1
    print(f"BLE001 baseline passed: {errors} findings (budget {budget}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
