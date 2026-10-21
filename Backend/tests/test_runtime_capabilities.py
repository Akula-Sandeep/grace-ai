from app.capabilities import runtime_capabilities


def test_runtime_capabilities_report_optional_dependencies(monkeypatch):
    available = {"pgvector"}
    monkeypatch.setattr(
        "app.capabilities.importlib.util.find_spec",
        lambda package: object() if package in available else None,
    )

    assert runtime_capabilities() == {
        "pgvector": True,
        "sentence_transformers": False,
    }
