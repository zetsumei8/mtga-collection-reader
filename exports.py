"""Collection export writers."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence


def write_collection_exports(
    rows: Sequence[Mapping[str, Any]],
    output_dir: Path,
    report_data: Mapping[str, Any],
    reporter: Any = None,
) -> Dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "txt": output_dir / "mtga_collection.txt",
        "json": output_dir / "mtga_collection.json",
        "csv": output_dir / "mtga_collection.csv",
        "report": output_dir / "scan_report.json",
        "review": output_dir / "review_items.csv",
    }

    with paths["txt"].open("w", encoding="utf-8") as handle:
        for row in rows:
            set_code = str(row.get("set_code") or row.get("set") or "")
            collector = str(row.get("collector_number") or row.get("cn") or "")
            printing = f" ({set_code}) {collector}".rstrip() if set_code else ""
            handle.write(f"{int(row.get('quantity', row.get('count', 0)))} {row.get('name', 'Unknown')}{printing}\n")

    json_rows = [dict(row) for row in rows]
    paths["json"].write_text(json.dumps(json_rows, indent=2, default=str), encoding="utf-8")

    with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Count", "Name", "Edition", "Collector Number", "Condition", "Language", "Foil", "Tag"])
        for row in rows:
            writer.writerow(
                [
                    int(row.get("quantity", row.get("count", 0))),
                    row.get("name", "Unknown"),
                    row.get("set_code", row.get("set", "")),
                    row.get("collector_number", row.get("cn", "")),
                    "Near Mint",
                    "English",
                    "",
                    "Needs Review" if row.get("needs_review") or row.get("printing_uncertain") else "",
                ]
            )

    with paths["review"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Name", "Edition", "Collector Number", "Quantity", "Reason"])
        for row in rows:
            reasons = []
            if row.get("needs_review"):
                reasons.append("Recognition review")
            if row.get("printing_uncertain"):
                reasons.append("Printing uncertain")
            if reasons:
                writer.writerow(
                    [
                        row.get("name", "Unknown"),
                        row.get("set_code", ""),
                        row.get("collector_number", ""),
                        int(row.get("quantity", 0)),
                        "; ".join(reasons),
                    ]
                )

    paths["report"].write_text(json.dumps(dict(report_data), indent=2, default=str), encoding="utf-8")
    if reporter is not None:
        log = getattr(reporter, "log", None)
        if callable(log):
            log(f"Saved merged collection exports to {output_dir}")
    return paths
