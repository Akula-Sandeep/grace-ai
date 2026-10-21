from types import SimpleNamespace

from scripts import check_mypy_baseline


def test_missing_mypy_tool_fails_instead_of_passing_empty_baseline(monkeypatch, capsys):
    monkeypatch.setattr(
        check_mypy_baseline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="No module named mypy", returncode=1),
    )

    assert check_mypy_baseline.main() == 1
    assert "tool or configuration failure" in capsys.readouterr().err
