from pathlib import Path

from citibike.serving.analytics_publish import validate_gold
from tests.fixtures_analytics import analytical_gold


def test_unchanged_and_changed_source_invalidate_both_boundary_months(
    tmp_path: Path,
) -> None:
    old = analytical_gold(tmp_path)
    assert analytical_gold(tmp_path) == old
    before = validate_gold(old)
    newer = analytical_gold(tmp_path, changed=True)
    after = validate_gold(newer)
    old_parts = {p["month"]: p["id"] for p in before["months"]}
    new_parts = {p["month"]: p["id"] for p in after["months"]}
    assert {m for m in old_parts if old_parts[m] != new_parts[m]} == {
        "2024-10",
        "2024-11",
    }
    assert old_parts["2024-09"] == new_parts["2024-09"]
    assert old_parts["2025-03"] == new_parts["2025-03"]
    assert before["months"] and old.exists()
