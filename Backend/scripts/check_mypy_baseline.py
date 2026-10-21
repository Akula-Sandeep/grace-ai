"""Fail when the non-strict mypy error count exceeds its reviewed budget."""

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ERROR_BUDGET = BACKEND_DIR / "mypy-error-budget.txt"


def count_errors(output: str) -> int:
    return sum(": error:" in line for line in output.splitlines())


def main() -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-error-summary",
            "--show-error-codes",
            "app",
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        check=False,
        text=True,
    )
    output = result.stdout + result.stderr
    if output:
        print(output, end="")
    if result.returncode not in (0, 1):
        return result.returncode

    errors = count_errors(output)
    if result.returncode != 0 and errors == 0:
        print(
            f"mypy exited with status {result.returncode} without reporting type errors; "
            "this indicates a tool or configuration failure.",
            file=sys.stderr,
        )
        return result.returncode

    budget = int(ERROR_BUDGET.read_text(encoding="utf-8").strip())
    if errors > budget:
        print(
            f"mypy reported {errors} errors; reviewed baseline budget is {budget}.",
            file=sys.stderr,
        )
        return 1
    print(f"mypy baseline passed: {errors} errors (budget {budget}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
