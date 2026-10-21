import json
from types import SimpleNamespace

from scripts import check_ble001_baseline


def test_ble001_budget_fails_when_legacy_finding_count_grows(monkeypatch, tmp_path):
    budget = tmp_path / "ble001-error-budget.txt"
    budget.write_text("1", encoding="utf-8")
    monkeypatch.setattr(check_ble001_baseline, "ERROR_BUDGET", budget)
    monkeypatch.setattr(
        check_ble001_baseline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps([{"code": "BLE001"}, {"code": "BLE001"}]),
            stderr="",
            returncode=1,
        ),
    )

    assert check_ble001_baseline.main() == 1
