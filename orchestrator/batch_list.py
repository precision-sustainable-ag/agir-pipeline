"""
Parse batch list files for manual SciNet job submission.

Two supported line formats (one batch per line):

  Format A — with an explicit time window:

    MD_2026-04-03 | 5:55:04 PM 7:33:16 PM
    TX_2026-03-15 | 8:00:00 AM 10:30:45 AM

  Format B — bare batch_id, no time window (used by the SQLite job commands):

    MD_2026-04-03
    TX_2026-03-15

Lines starting with ``#`` are ignored.

Fields:
  - batch_id : STATE_YYYY-MM-DD
  - start/end : 12-hour clock times in GMT on the batch date (Format A only)

For Format B, ``start_epoch`` and ``end_epoch`` are both set to 0, signalling
to callers that no time-window constraint applies.

The epoch timestamps in RAW filenames are also GMT, so no timezone
conversion is needed — we just combine the batch date with the given
times and convert to UTC Unix epoch.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import List, NamedTuple


class BatchEntry(NamedTuple):
    batch_id: str
    start_epoch: int
    end_epoch: int


_TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)", re.IGNORECASE)
_BATCH_ID_RE = re.compile(r"^[A-Z]{2,3}_\d{4}-\d{2}-\d{2}$")


def _parse_time_window(date: datetime.date, times_str: str) -> tuple[int, int]:
    matches = _TIME_RE.findall(times_str)

    if len(matches) < 2:
        raise ValueError(
            f"Expected two HH:MM:SS AM/PM times, got: {times_str!r}"
        )
    fmt = "%Y-%m-%d %I:%M:%S %p"
    date_str = date.isoformat()

    start_dt = datetime.datetime.strptime(f"{date_str} {matches[0].strip()}", fmt)
    end_dt   = datetime.datetime.strptime(f"{date_str} {matches[1].strip()}", fmt)
    start_epoch = int(start_dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    end_epoch   = int(end_dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    if end_epoch <= start_epoch:
        raise ValueError(
            f"end_time must be after start_time: "
            f"{matches[0].strip()!r} vs {matches[1].strip()!r}"
        )
    return start_epoch, end_epoch


def parse_batch_list(path: str | Path) -> List[BatchEntry]:
    """Parse a batch list file and return a list of BatchEntry namedtuples.

    Accepts both Format A (``batch_id | start end``) and Format B (bare
    ``batch_id``) lines.  Format B entries have ``start_epoch=0`` and
    ``end_epoch=0``.

    Raises ValueError on malformed lines.
    """
    entries: List[BatchEntry] = []
    path = Path(path)
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if "|" in line:
            # ── Format A: batch_id | start_time end_time ─────────────────
            batch_part, times_part = line.split("|", maxsplit=1)
            batch_id = batch_part.strip()
            if not _BATCH_ID_RE.match(batch_id):
                raise ValueError(
                    f"Line {lineno}: batch_id {batch_id!r} does not match "
                    "STATE_YYYY-MM-DD"
                )
            date_str = batch_id.split("_", maxsplit=1)[1]
            try:
                date = datetime.date.fromisoformat(date_str)
            except ValueError as exc:
                raise ValueError(
                    f"Line {lineno}: invalid date in batch_id: {exc}"
                ) from exc
            try:
                start_epoch, end_epoch = _parse_time_window(date, times_part)
            except ValueError as exc:
                raise ValueError(f"Line {lineno}: {exc}") from exc
            entries.append(
                BatchEntry(batch_id=batch_id, start_epoch=start_epoch, end_epoch=end_epoch)
            )
        else:
            # ── Format B: bare batch_id (SQLite / no-window mode) ─────────
            batch_id = line
            if not _BATCH_ID_RE.match(batch_id):
                raise ValueError(
                    f"Line {lineno}: {batch_id!r} is not a valid batch_id "
                    "(STATE_YYYY-MM-DD) and contains no '|' separator"
                )
            entries.append(BatchEntry(batch_id=batch_id, start_epoch=0, end_epoch=0))

    # Reject duplicate exact windows (batch_id, start_epoch, end_epoch).
    seen_windows: set[tuple] = set()
    for entry in entries:
        window_key = (entry.batch_id, entry.start_epoch, entry.end_epoch)
        if window_key in seen_windows:
            raise ValueError(f"Duplicate window detected: {window_key}")
        seen_windows.add(window_key)

    return entries
