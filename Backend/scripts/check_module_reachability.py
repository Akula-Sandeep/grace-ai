"""Fail when an app module is not reachable from a runtime entry point.

Walks static imports (including function-local imports) from the API app, the
background workers and the CLI scripts that operators run. Code nobody can
reach is either wired in or removed; it must not silently accumulate again.
"""

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
ENTRY_POINTS = [
    "app.main",
    "app.jobs.process_background",
    "app.jobs.process_feedback_inference",
    "app.jobs.purge_deleted_accounts",
    "app.jobs.purge_deleted_conversations",
]
# Packages whose modules are loaded indirectly (alembic env, eval harness run by CI).
INDIRECT = {"app.evals"}


def module_name(path: Path) -> str:
    parts = path.relative_to(BACKEND).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {item for item in found if item.startswith("app")}


def main() -> int:
    modules = {module_name(p): p for p in APP.rglob("*.py")}
    reachable: set[str] = set()
    pending = list(ENTRY_POINTS)
    while pending:
        name = pending.pop()
        if name in reachable or name not in modules:
            continue
        reachable.add(name)
        parts = name.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))  # parent packages
        pending.extend(imports(modules[name], name))
    unreachable = sorted(
        name
        for name in modules
        if name not in reachable and not any(name == p or name.startswith(p + ".") for p in INDIRECT)
    )
    for name in unreachable:
        print(f"unreachable module: {name}")
    print(f"{len(reachable)} reachable modules, {len(unreachable)} unreachable")
    return 1 if unreachable else 0


if __name__ == "__main__":
    sys.exit(main())
