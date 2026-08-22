import json
from pathlib import Path

from parameter_extract.parity import check_parity_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "live_bot_parity_v1.json"


def test_frozen_live_bot_fixture_matches():
    report = check_parity_fixture(FIXTURE)
    assert report.ok is True
    assert report.source_commit == "0ab6aa532cb22f399bc94393280c604cb6756d66"
    assert report.checked_points == 9
    assert report.expected_signals == 2


def test_parity_detects_a_changed_expected_value(tmp_path: Path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["expected"]["points"][0]["rsi"] += 0.001
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(payload), encoding="utf-8")
    report = check_parity_fixture(changed)
    assert report.ok is False
    assert any("rsi" in problem for problem in report.problems)
