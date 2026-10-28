"""Assert the explicitly synthetic 24-month first/repeat/one-changed-source protocol."""

import argparse
from pathlib import Path

from citibike.storage import read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    root = args.data_dir
    active = read_json(root / "f2/active.json")
    current = read_json(Path(active["path"]) / "manifest.json")
    previous = read_json(Path(current["previous_snapshot"]["path"]) / "manifest.json")
    assert previous["canonical_rows"] == 24
    assert current["canonical_rows"] == 25
    assert current["measurements"]["rebuilt_partitions"] == 1
    assert sum(p["reused"] for p in current["partitions"]) == 23
    assert len(current["partitions"]) == 24
    runs = [read_json(p) for p in (root / "f2/runs").glob("*.json")]
    assert any(r["mode"] == "unchanged" and r["rebuilt_partitions"] == 0 for r in runs)
    result = {
        "data_mode": "controlled_fixture",
        "scope": "24 tiny synthetic months; not real F2 history",
        "first_snapshot": previous["snapshot_id"],
        "changed_snapshot": current["snapshot_id"],
        "canonical_before": 24,
        "canonical_after": 25,
        "rebuilt_partitions": 1,
        "reused_partitions": 23,
        "old_and_new_boundaries_invalidated": (
            "Also covered by "
            "test_global_reconciliation_incremental_dependencies_and_rollback"
        ),
        "runs": runs,
    }
    if args.evidence:
        write_json(args.evidence, result)
    print(
        "Controlled history E2E passed: 24 -> 25 canonical rows; 1 rebuilt, 23 reused"
    )


if __name__ == "__main__":
    main()
