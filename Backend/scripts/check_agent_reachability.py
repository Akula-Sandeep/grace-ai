"""Report MovieAgent methods unreachable from its runtime entry points."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
AGENT_FILE = APP / "agent" / "agent.py"
RUNTIME_ROOTS = {"__init__", "close", "run", "stream", "_shared_services", "compare_visuals"}


def main() -> int:
    tree = ast.parse(AGENT_FILE.read_text(encoding="utf-8"))
    agent_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MovieAgent"
    )
    methods = {
        node.name: node
        for node in agent_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    graph = {name: set() for name in methods}
    for name, node in methods.items():
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
            function = call.func
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "self"
                and function.attr in methods
            ):
                graph[name].add(function.attr)

    reachable = set()
    pending = list(RUNTIME_ROOTS & methods.keys())
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(graph[name] - reachable)

    for name in methods:
        state = "reachable" if name in reachable else "unreachable"
        print(f"{state}: MovieAgent.{name}")
    return 1 if set(methods) - reachable else 0


if __name__ == "__main__":
    raise SystemExit(main())
