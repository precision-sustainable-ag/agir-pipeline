#!/usr/bin/env python
import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict

import psycopg2.extras

from utils.db import get_conn

ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "reports" / "globus_index"
RAW_DIR = REPORT_ROOT / "raw"
SUMMARY_DIR = REPORT_ROOT / "summary"
SQL_PATH = ROOT / "sql" / "queries" / "globus_index" / "report_core.sql"

RAW_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class NamedQuery:
    name: str
    sql: str


def parse_sql_file(path: Path) -> List[NamedQuery]:
    """
    Very simple parser that expects blocks like:

        -- name: some_slug
        SELECT ...;
        ...

    Returns a list of NamedQuery objects.
    """
    text = path.read_text()
    lines = text.splitlines()

    queries: List[NamedQuery] = []
    current_name: str | None = None
    current_sql_lines: List[str] = []

    def flush():
        nonlocal current_name, current_sql_lines
        if current_name and current_sql_lines:
            sql = "\n".join(current_sql_lines).strip()
            if sql:
                queries.append(NamedQuery(name=current_name, sql=sql))
        current_name = None
        current_sql_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("-- name:"):
            # start of a new query block
            flush()
            current_name = stripped.split(":", 1)[1].strip()
        else:
            if current_name is not None:
                current_sql_lines.append(line)

    # last block
    flush()
    return queries


def fetch_all_dicts(cur, query: str) -> List[Dict]:
    cur.execute(query)
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def write_csv(path: Path, rows: List[Dict]) -> None:
    if not rows:
        # Write an empty file with no header
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    today = dt.date.today().isoformat()
    queries = parse_sql_file(SQL_PATH)

    total_stats: Dict = {}

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        for nq in queries:
            print(f"[INFO] Running query: {nq.name}")
            rows = fetch_all_dicts(cur, nq.sql)

            # Save CSV for every query
            csv_path = RAW_DIR / f"{today}_{nq.name}.csv"
            write_csv(csv_path, rows)

            # Grab total_stats row (for markdown) if present
            if "total_stats" in nq.name:
                total_stats = rows[0] if rows else {}

    # --- Markdown summary
    md_path = SUMMARY_DIR / f"{today}_globus_index_report.md"
    md_lines = [
        f"# Globus Index Report – {today}",
        "",
        "## 1. High-level stats",
        "",
        f"- **Total files**: {total_stats.get('total_files', 0)}",
        f"- **Total bytes**: {total_stats.get('total_bytes', 0)}",
        f"- **First indexed**: {total_stats.get('first_indexed')}",
        f"- **Last indexed**: {total_stats.get('last_indexed')}",
        "",
        "## 2. Generated CSVs",
        "",
        "The following CSVs were generated from the SQL in "
        f"`{SQL_PATH.relative_to(ROOT)}`:",
        "",
    ]

    # Just list everything in RAW_DIR for that date
    for csv_file in sorted(RAW_DIR.glob(f"{today}_*.csv")):
        md_lines.append(f"- [{csv_file.name}](../{csv_file.parent.name / csv_file.name})")

    md_lines.append("")
    md_lines.append(
        "You can load these CSVs into pandas/Excel or feed them into "
        "downstream pipelines."
    )

    md_path.write_text("\n".join(md_lines))
    print(f"Wrote report: {md_path}")


if __name__ == "__main__":
    main()
