"""报告输出：JSON + CSV 到 REPORT_DIR，文件名带 UTC 时间戳。"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H%M%SZ")


def write_report(
    report_dir: str,
    base_name: str,
    data: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    fieldnames: List[str],
) -> Dict[str, str]:
    """把摘要写 JSON、明细写 CSV，均带 UTC 时间戳。返回写出的文件路径。"""
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_iso()

    json_path = out_dir / f"{base_name}-{stamp}.json"
    payload = {
        "generated_at_utc": utc_now().isoformat(),
        "summary": data,
        "rows": rows,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    csv_path = out_dir / f"{base_name}-{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(row.get(k)) for k in fieldnames})

    return {"json": str(json_path), "csv": str(csv_path)}


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "__str__") and type(obj).__name__ == "Decimal":
        return str(obj)
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    return str(obj)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    return value
