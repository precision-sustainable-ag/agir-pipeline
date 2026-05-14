"""
Parse batch list files for manual SciNet job submission.

Expected file format (one batch per line):

    MD_2026-04-03 | 5:55:04 PM 7:33:16 PM
    TX_2026-03-15 | 8:00:00 AM 10:30:45 AM
    # lines starting with # are ignored

Fields:
  - batch_id : STATE_YYYY-MM-DD
  - start/end : 12-hour clock times in GMT on the batch date

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

def normalize_time_str(time_str: str) -> str:
    """
    Detects if the time string is in 12-hour (AM/PM) or 24-hour format
    and normalizes it into 24-hour 'HH:MM:SS'.
    
    Accepts both '1:25:18 PM' and '1:25:18PM'.
    """
    time_str = time_str.strip().upper()

    # Fix cases like '1:25:18PM' -> '1:25:18 PM'
    time_str = re.sub(r'(?<=\d)(AM|PM)$', r' \1', time_str)

    try:
        # Try parsing as 12-hour format (AM/PM)
        dt = datetime.datetime.strptime(time_str, "%I:%M:%S %p")
    except ValueError:
        try:
            # Try parsing as 24-hour format
            dt = datetime.datetime.strptime(time_str, "%H:%M:%S")
        except ValueError as e:
            raise ValueError(f"Unrecognized time format: {time_str}") from e

    return dt.strftime("%H:%M:%S")

def _parse_time_window(date: datetime.date, times_str: str) -> tuple[int, int]:
    matches = _TIME_RE.findall(times_str)
    # matches = [normalize_time_str(m) for m in matches]

    if len(matches) < 2:
        raise ValueError(
            f"Expected two HH:MM:SS AM/PM times, got: {times_str!r}"
        )
    fmt = "%Y-%m-%d %I:%M:%S %p"
    date_str = date.isoformat()
    
    start_dt = datetime.datetime.strptime(f"{date_str} {matches[0].strip()}", fmt)
    end_dt = datetime.datetime.strptime(f"{date_str} {matches[1].strip()}", fmt)
    start_epoch = int(start_dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    end_epoch = int(end_dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    if end_epoch < start_epoch:
        raise ValueError(
            f"end_time {matches[1].strip()!r} is before start_time {matches[0].strip()!r}"
        )
    return start_epoch, end_epoch


def parse_batch_list(path: str | Path) -> List[BatchEntry]:
    """Parse a batch list file and return a list of BatchEntry namedtuples.

    Raises ValueError on malformed lines.
    """
    entries: List[BatchEntry] = []
    path = Path(path)
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ValueError(f"Line {lineno}: missing '|' separator: {raw!r}")
        batch_part, times_part = line.split("|", maxsplit=1)
        batch_id = batch_part.strip()
        # Validate batch_id format: STATE_YYYY-MM-DD
        if not re.fullmatch(r"[A-Z]{2,3}_\d{4}-\d{2}-\d{2}", batch_id):
            raise ValueError(
                f"Line {lineno}: batch_id {batch_id!r} does not match STATE_YYYY-MM-DD"
            )
        date_str = batch_id.split("_", maxsplit=1)[1]
        try:
            date = datetime.date.fromisoformat(date_str)
        except ValueError as exc:
            raise ValueError(f"Line {lineno}: invalid date in batch_id: {exc}") from exc
        try:
            start_epoch, end_epoch = _parse_time_window(date, times_part)
        except ValueError as exc:
            raise ValueError(f"Line {lineno}: {exc}") from exc
        entries.append(BatchEntry(batch_id=batch_id, start_epoch=start_epoch, end_epoch=end_epoch))
    return entries