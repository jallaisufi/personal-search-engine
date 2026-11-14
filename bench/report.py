#!/usr/bin/env python3
"""Turn bench/results/*.json into a Markdown table (standard library only)."""

import json
import sys
from pathlib import Path

ORDER = {"cold": 0, "warm": 1}


def main() -> int:
    results = Path(__file__).parent / "results"
    rows = [json.loads(path.read_text()) for path in sorted(results.glob("*.json"))]
    if not rows:
        print("No results yet. Run `make bench` first.", file=sys.stderr)
        return 1
    rows.sort(key=lambda r: (ORDER.get(r["scenario"], 9), r["label"]))

    print("| Scenario | Mode | Requests/s | p50 | p95 | p99 | Cache hits | Failed |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r['scenario']} search ({r['vus']} users) | {r['label']} "
            f"| {r['requests_per_second']:.0f} | {r['p50_ms']:.1f} ms | {r['p95_ms']:.1f} ms "
            f"| {r['p99_ms']:.1f} ms | {r['cache_hit_rate'] * 100:.0f}% "
            f"| {r['failed_rate'] * 100:.2f}% |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
