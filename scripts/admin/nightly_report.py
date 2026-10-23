#!/usr/bin/env python3
"""
# scripts/admin/nightly_report.py

Daily pipeline report covering the last --hours (default 24):

  - Stage runs: stage_runs that ended in the window, per stage, with every
    failed/partial run listed. If no stage ran, each stage's last day of runs
    is reported instead.
  - Globus indexing: each globus_index_sqlite job in the window (Slurm state,
    inventory_runs per site, scopes that didn't finish cleanly, directories
    the crawl skipped per the job log), stage_runs invalidated as lost output,
    and whether the next nightly job is queued.
  - Slurm: pipeline jobs by name and state. Jobs that end FAILED/TIMEOUT/
    OUT_OF_MEMORY/... die before ingest, so they never reach stage_runs.
  - Leftovers: expired stage_leases, failed or stuck staged_inputs and
    result_syncs, plus the current queue.

Read-only: open_db(readonly=True) skips the migrations a writable open
applies. sacct/squeue only see this cluster, so Atlas stages show up once
their runs are synced to Ceres.

    .venv/bin/python scripts/admin/nightly_report.py               # print it
    .venv/bin/python scripts/admin/nightly_report.py --email someone@ncsu.edu

scripts/admin/nightly_report.sbatch emails it every morning, and
scripts/admin/dashboard.py shows the same report in its "Daily report" panel.
"""

import argparse
import html
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.sqlite_db import open_db

DEFAULT_DB = "/project/dash_agir/globus_index/globus_file_index.sqlite3"
DEFAULT_FROM = "matthew.kutugata@usda.gov"
SENDMAIL = "/usr/sbin/sendmail"
LOCAL_TZ = ZoneInfo("America/Chicago")

STAGES = ("raw_to_jpg", "jpg_to_det", "det_to_world", "det_to_seg", "seg_to_cut")
INDEX_JOB = "globus_index_sqlite"
# Not pipeline work, or reported in a section of their own.
IGNORED_JOBS = {INDEX_JOB, "agir_dashboard", "agir_nightly_report", "interactive"}
BAD_JOB_STATES = {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "CANCELLED",
                  "BOOT_FAIL", "PREEMPTED", "DEADLINE"}
# det_to_world_MD_2026-08-24 -> det_to_world (submit.py names jobs per batch).
BATCH_SUFFIX_RE = re.compile(r"_[A-Z]{2}_\d{4}-\d{2}-\d{2}$")
SKIPPED_DIR_RE = re.compile(r"Skipping directory due to error: (\S+?): (.*?)(?: for \S|$)")
# submit.py's input-staging gates (filter_completed_staged_inputs / filter_all_staged_inputs).
STAGING_SKIP_RE = re.compile(r"Skipping (\d+) batch\(es\) without (?:completed input staging|"
                             r"every staged input) for (\w+): (.*)")
BATCH_RE = re.compile(r"\b[A-Z]{2}_\d{4}-\d{2}-\d{2}\b")
OPEN_STAGING = ("planned", "requested", "submitted", "active")
OPEN_SYNC = ("requested", "transferring", "transferred", "verified")
STALE_AFTER = timedelta(days=7)  # stuck longer than this: counted, not listed
MAX_ROWS = 25  # per detail table; the rest are summarised as "... and N more"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(value) -> datetime | None:
    """DB timestamps ('...Z', '...+00:00'; naive = UTC) -> aware UTC."""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_slurm_ts(value) -> datetime | None:
    """sacct/squeue times are naive cluster-local ('Unknown'/'None' -> None)."""
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def sql_ts(dt: datetime) -> str:
    """Lower bound that string-compares correctly with the DB's UTC timestamps."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def fmt(dt: datetime | None) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%b %d %H:%M") if dt else "—"


def counts(c: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common())


def short_list(items: list[str], limit: int = 40) -> str:
    more = f" … and {len(items) - limit} more" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


# ---------------------------------------------------------------------------
# Report blocks: a section is a title, a one-line headline with a level
# ('bad' marks the report as needing attention, 'warn', 'ok', or ''), and a
# list of paragraphs and tables rendered by render_text/render_html.
# ---------------------------------------------------------------------------

def para(text: str, level: str = "") -> dict:
    return {"text": text, "level": level}


def table(cols: list[str], rows: list[list], limit: int = MAX_ROWS) -> dict:
    return {"cols": cols, "rows": rows[:limit], "more": max(len(rows) - limit, 0)}


def section(title: str, headline: str, level: str, blocks: list[dict]) -> dict:
    return {"title": title, "headline": headline, "level": level, "blocks": blocks}


# ---------------------------------------------------------------------------
# Slurm
# ---------------------------------------------------------------------------

def slurm_jobs(since: datetime, until: datetime | None = None) -> list[dict] | str:
    """This user's jobs that ran at any point in [since, until or now] (error text on failure)."""
    fields = ["job_id", "name", "state", "start", "end", "elapsed", "exit_code", "stdout", "stderr"]
    local = lambda dt: dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    window = ["--starttime", local(since)] + (["--endtime", local(until)] if until else [])
    try:
        out = subprocess.run(
            ["sacct", "-u", os.environ.get("USER", ""), "-X", "-n", "--parsable2", *window,
             "-o", "JobID,JobName,State,Start,End,Elapsed,ExitCode,StdOut,StdErr"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"sacct failed: {exc}"
    jobs = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != len(fields):
            continue
        job = dict(zip(fields, parts))
        job["start"], job["end"] = parse_slurm_ts(job["start"]), parse_slurm_ts(job["end"])
        if job["start"] is None:  # never started
            continue
        job["state"] = job["state"].split()[0]  # "CANCELLED by 12345" -> CANCELLED
        for key in ("stdout", "stderr"):
            job[key] = (job[key].replace("%j", job["job_id"]).replace("%x", job["name"])
                        .replace("%u", os.environ.get("USER", "")))
        jobs.append(job)
    return sorted(jobs, key=lambda j: j["start"])


def slurm_queue() -> list[dict] | str:
    fields = ["job_id", "name", "state", "start", "reason"]
    try:
        out = subprocess.run(
            ["squeue", "-u", os.environ.get("USER", ""), "-h", "-o", "%i|%j|%T|%S|%r"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"squeue failed: {exc}"
    return [dict(zip(fields, line.split("|"))) for line in out.splitlines()
            if line.count("|") == len(fields) - 1]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

STAGE_RUN_COLS = ("batch_id, stage, status, exit_code, started_at, ended_at, "
                  "n_units_discovered, n_units_succeeded, n_units_failed, logs_path")
# n_units_succeeded isn't populated by the stages, so show discovered/failed.
STAGE_SUMMARY_COLS = ["Stage", "Runs", "Statuses", "Batches", "Units", "Failed units",
                      "First start", "Last end"]


def _stage_runs(conn, start: datetime, end: datetime, stage: str | None = None) -> list[dict]:
    sql = f"SELECT {STAGE_RUN_COLS} FROM stage_runs WHERE ended_at >= ?"
    params = [sql_ts(start)]
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    runs = []
    for row in conn.execute(sql, params):
        run = dict(row)
        run["started"], run["ended"] = parse_ts(run["started_at"]), parse_ts(run["ended_at"])
        if run["ended"] and start <= run["ended"] <= end:
            runs.append(run)
    return sorted(runs, key=lambda r: r["ended"])


def _stage_summary(stage: str, runs: list[dict]) -> list:
    return [
        stage, len(runs), counts(Counter(r["status"] for r in runs)),
        len({r["batch_id"] for r in runs}),
        f"{sum(r['n_units_discovered'] for r in runs):,}", sum(r["n_units_failed"] for r in runs),
        fmt(min(r["started"] or r["ended"] for r in runs)), fmt(max(r["ended"] for r in runs)),
    ]


def _problem_table(runs: list[dict]) -> dict:
    return table(
        ["Stage", "Batch", "Status", "Exit", "Units failed", "Ended", "Logs"],
        [[r["stage"], r["batch_id"], r["status"], r["exit_code"],
          f"{r['n_units_failed']}/{r['n_units_discovered']}", fmt(r["ended"]), r["logs_path"] or ""]
         for r in runs],
    )


def _problems(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r["status"] != "success" or r["n_units_failed"]]


def _gate_skips(jobs: list[dict] | str) -> list[dict]:
    """Batches submit.py's input-staging gate refused to submit. submit.py only
    logs these (nothing reaches the DB), so the logs of the Slurm jobs that ran
    it (group runners, dashboard chains, sync_then_det_to_world) are the record."""
    if isinstance(jobs, str):
        return []
    skips = []
    for job in jobs:
        if job["name"] in IGNORED_JOBS:
            continue
        for path in dict.fromkeys(p for p in (job["stdout"], job["stderr"]) if p):
            try:
                with open(path, errors="replace") as fh:
                    matches = [m for line in fh if (m := STAGING_SKIP_RE.search(line))]
            except OSError:
                continue
            for m in matches:
                ids = BATCH_RE.findall(m.group(3))
                if int(m.group(1)) > len(ids):  # submit.py logs at most 10 IDs per line
                    ids.append(f"+{int(m.group(1)) - len(ids)} unlisted (job {job['job_id']})")
                skips += [{"stage": m.group(2), "batch_id": b, "job": job, "log": path} for b in ids]
    return skips


def _staging_problems(conn, jobs: list[dict] | str, start: datetime, end: datetime,
                      stage: str | None = None) -> tuple[list[list], int]:
    """Batches that didn't run in [start, end] because their inputs weren't
    staged: submit.py gate skips, unless the batch ran successfully afterwards
    (counted, not listed), plus staged_inputs rows that failed."""
    last_ok = {(r["stage"], r["batch_id"]): parse_ts(r["last"]) for r in conn.execute(
        "SELECT stage, batch_id, MAX(ended_at) AS last FROM stage_runs "
        "WHERE status = 'success' GROUP BY stage, batch_id")}
    latest, resolved = {}, set()
    for skip in _gate_skips(jobs):
        key = (skip["stage"], skip["batch_id"])
        if stage and skip["stage"] != stage:
            continue
        ok = last_ok.get(key)
        if ok and ok >= skip["job"]["start"]:
            resolved.add(key)
            continue
        job = skip["job"]
        latest[key] = [skip["stage"], skip["batch_id"], "not staged, skipped at submit",
                       fmt(job["end"] or job["start"]), f"job {job['job_id']}", skip["log"]]
    rows = sorted(latest.values())
    for r in conn.execute(
        "SELECT stage, batch_id, updated_at, globus_task_id, error_summary FROM staged_inputs "
        "WHERE status = 'failed' AND updated_at >= ? ORDER BY updated_at", (sql_ts(start),)
    ):
        updated = parse_ts(r["updated_at"])
        if (stage and r["stage"] != stage) or not updated or updated > end:
            continue
        rows.append([r["stage"], r["batch_id"], "Globus input staging failed", fmt(updated),
                     f"task {r['globus_task_id'] or '—'} · {(r['error_summary'] or '')[:160]}", ""])
    return rows, len(resolved - set(latest))


def _staging_blocks(rows: list[list], resolved: int, when: str, level: str) -> list[dict]:
    """Rows are [stage, batch, problem, when, detail, log]; logs are listed once."""
    blocks = []
    if rows:
        blocks += [para(f"Batches that didn't run because their inputs weren't staged {when} "
                        f"({len(rows)}):", level),
                   table(["Stage", "Batch", "Problem", "When", "Detail"], [r[:5] for r in rows],
                         limit=300)]
        for s in dict.fromkeys(r[0] for r in rows):
            ids = sorted({r[1] for r in rows if r[0] == s and BATCH_RE.fullmatch(r[1])})
            if ids:
                blocks.append(para(f"{s} batch IDs to rerun: {', '.join(ids)}", "muted"))
        logs = dict.fromkeys((r[4], r[5]) for r in rows if r[5])
        if logs:
            blocks.append(para("Logs: " + "; ".join(f"{job} {path}" for job, path in logs)
                               + ". stage_inputs.py logs why each batch wasn't planned (usually: not "
                               "returned by the readiness query) under logs/<stage>/input_staging/"
                               "stage_inputs/<date>/.", "muted"))
    if resolved:
        blocks.append(para(f"{resolved} other batch(es) skipped for unstaged inputs {when} "
                           "ran successfully later.", "muted"))
    return blocks


def stage_section(conn, since: datetime, now: datetime, hours: int,
                  jobs: list[dict] | str) -> dict:
    runs = _stage_runs(conn, since, now)
    last_end = {r["stage"]: parse_ts(r["last"]) for r in conn.execute(
        "SELECT stage, MAX(ended_at) AS last FROM stage_runs GROUP BY stage")}
    stages = list(STAGES) + sorted(set(last_end) - set(STAGES))
    by_stage = {s: [r for r in runs if r["stage"] == s] for s in stages}
    ran = [s for s in stages if by_stage[s]]
    # Checked even when nothing ran: a night where every batch failed staging
    # has no stage_runs at all.
    staging, resolved = _staging_problems(conn, jobs, since, now)
    staging_note = f"; {len(staging)} batch(es) didn't run: inputs not staged" if staging else ""

    if ran:
        blocks = [table(STAGE_SUMMARY_COLS, [_stage_summary(s, by_stage[s]) for s in ran])]
        blocks += [para(f"{s}: " + short_list(sorted({r['batch_id'] for r in by_stage[s]})), "muted")
                   for s in ran]
        problems = _problems(runs)
        if problems:
            blocks += [para(f"Failed, partial or with failed units ({len(problems)}):", "bad"),
                       _problem_table(problems)]
        blocks += _staging_blocks(staging, resolved, f"in the last {hours} h", "bad")
        idle = [f"{s} (last {fmt(last_end[s])})" for s in stages if s not in ran and last_end.get(s)]
        if idle:
            blocks.append(para("Didn't run: " + ", ".join(idle), "muted"))
        headline = (f"Stages: {len(runs)} run(s) — "
                    + ", ".join(f"{s} {counts(Counter(r['status'] for r in by_stage[s]))}" for s in ran)
                    + staging_note)
        return section("Stage runs", headline, "bad" if problems or staging else "ok", blocks)

    # Nothing ran: show each stage's last day of runs instead, including the
    # batches that day's jobs skipped for unstaged inputs.
    blocks = [para(f"No stage runs ended in the last {hours} h.", "bad" if staging else "")]
    blocks += _staging_blocks(staging, resolved, f"in the last {hours} h", "bad")
    blocks.append(para("Each stage's last day of runs (24 h up to its most recent run):"))
    last_runs, rows, old_staging, old_resolved = [], [], [], 0
    for s in stages:
        if not last_end.get(s):
            continue
        day_start = last_end[s] - timedelta(hours=24)
        stage_runs = _stage_runs(conn, day_start, last_end[s], stage=s)
        last_runs += stage_runs
        rows.append(_stage_summary(s, stage_runs))
        # The group runner keeps going after its last batch, so look a bit past it.
        day_end = last_end[s] + timedelta(hours=2)
        s_rows, s_resolved = _staging_problems(conn, slurm_jobs(day_start, day_end),
                                               day_start, day_end, stage=s)
        old_staging += s_rows
        old_resolved += s_resolved
    blocks.append(table(STAGE_SUMMARY_COLS, rows))
    problems = _problems(last_runs)
    if problems:
        blocks += [para(f"Failed, partial or with failed units on those days ({len(problems)}):", "warn"),
                   _problem_table(problems)]
    blocks += _staging_blocks(old_staging, old_resolved, "on those days", "warn")
    latest = max((s for s in last_end if last_end[s]), key=lambda s: last_end[s], default=None)
    headline = f"Stages: nothing ran in the last {hours} h" + staging_note
    if latest:
        headline += f" (most recent: {latest}, {fmt(last_end[latest])})"
    if old_staging:
        headline += f"; on those last days {len(old_staging)} batch(es) didn't run: inputs not staged"
    return section("Stage runs", headline, "bad" if staging else "warn" if old_staging else "", blocks)


INVENTORY_COLS = ("run_id, site, data_state, storage_root, status, started_at_ts_iso, "
                  "total_files_seen, total_errors, total_marked_stale, error_summary")


def _inventory_runs(conn, start: datetime, end: datetime) -> list[dict]:
    runs = []
    for row in conn.execute(
        f"SELECT {INVENTORY_COLS} FROM inventory_runs WHERE started_at_ts_iso >= ?",
        (sql_ts(start),),
    ):
        run = dict(row)
        run["started"] = parse_ts(run["started_at_ts_iso"])
        if run["started"] and start <= run["started"] <= end:
            runs.append(run)
    return runs


def _unclean(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r["status"] not in ("success", "skipped") or r["total_errors"]]


def _inventory_blocks(runs: list[dict]) -> list[dict]:
    rows = []
    for site in sorted({r["site"] for r in runs}):
        rs = [r for r in runs if r["site"] == site]
        c = Counter(r["status"] for r in rs)
        rows.append([site, len(rs), c["success"], c["partial"], c["skipped"], c["failed"],
                     c["running"], f"{sum(r['total_files_seen'] for r in rs):,}",
                     sum(r["total_errors"] for r in rs),
                     f"{sum(r['total_marked_stale'] for r in rs):,}"])
    blocks = [table(["Site", "Scopes", "success", "partial", "skipped", "failed", "running",
                     "Files seen", "Errors", "Marked stale"], rows)]
    unclean = _unclean(runs)
    if unclean:
        blocks += [para(f"Scopes that didn't finish cleanly ({len(unclean)}):", "warn"), table(
            ["Site", "Data state", "Storage root", "Status", "Errors", "Error summary"],
            [[r["site"], r["data_state"], r["storage_root"], r["status"], r["total_errors"],
              (r["error_summary"] or "")[:160]] for r in unclean],
        )]
    return blocks


def _skipped_dirs(log_path: str) -> list[tuple[str, str]] | None:
    """Directories globus_index.py skipped, from the job's stdout log."""
    try:
        with open(log_path, errors="replace") as fh:
            found = [m.groups() for line in fh if (m := SKIPPED_DIR_RE.search(line))]
    except OSError:
        return None
    return list(dict.fromkeys(found))


def indexing_section(conn, since: datetime, now: datetime, hours: int,
                     jobs: list[dict] | str, queue: list[dict] | str) -> dict:
    blocks, levels, notes = [], [], []
    index_jobs = [j for j in jobs if j["name"] == INDEX_JOB] if isinstance(jobs, list) else []
    if isinstance(jobs, str):
        blocks.append(para(jobs, "bad"))

    covered = set()
    for job in index_jobs:
        running = job["state"] == "RUNNING"
        level = "bad" if job["state"] in BAD_JOB_STATES else "warn" if running else "ok"
        end = now if running else (job["end"] or now)
        blocks.append(para(
            f"Job {job['job_id']} {job['state']}: {fmt(job['start'])} → "
            f"{'still running' if running else fmt(job['end'])} ({job['elapsed']}) · "
            f"log {job['stdout']}", level))
        runs = _inventory_runs(conn, job["start"], end + timedelta(minutes=1))
        covered |= {r["run_id"] for r in runs}
        if runs:
            blocks += _inventory_blocks(runs)
        else:
            blocks.append(para("No inventory_runs recorded by this job.", "warn"))
            level = "bad" if level == "ok" else level
        skipped = _skipped_dirs(job["stdout"])
        if skipped:
            blocks += [para(f"Directories the crawl skipped ({len(skipped)}):", "warn"),
                       table(["Directory", "Reason"], [list(s) for s in skipped])]
        unclean = _unclean(runs)
        if unclean and level == "ok":
            level = "warn"
        levels.append(level)
        notes.append(f"job {job['job_id']} {job['state']} {fmt(job['start'])}→"
                     f"{'now' if running else fmt(job['end'])}, {len(runs)} scopes"
                     + (f", {len(unclean)} not clean" if unclean else ""))

    others = [r for r in _inventory_runs(conn, since, now) if r["run_id"] not in covered]
    if others and index_jobs:
        blocks.append(para(
            f"{len(others)} other inventory run(s) outside the nightly job (e.g. "
            f"sync_atlas_results --refresh-inventory): {counts(Counter(r['status'] for r in others))}",
            "muted"))
    elif others:
        blocks += [para(f"No {INDEX_JOB} job found in sacct; inventory_runs in the window:", "warn")]
        blocks += _inventory_blocks(others)
        levels.append("warn")
        notes.append(f"{len(others)} inventory runs, no {INDEX_JOB} job found")
    elif not index_jobs:
        last = parse_ts(conn.execute("SELECT MAX(started_at_ts_iso) FROM inventory_runs").fetchone()[0])
        blocks.append(para(f"No indexing in the last {hours} h.", "bad"))
        if last:
            blocks.append(para(f"Last indexing activity (24 h up to {fmt(last)}):"))
            blocks += _inventory_blocks(_inventory_runs(conn, last - timedelta(hours=24), last))
        levels.append("bad")
        notes.append(f"nothing ran (last activity {fmt(last)})")

    try:
        invalidated = [dict(r) for r in conn.execute(
            "SELECT batch_id, stage, invalidated_at, invalidated_reason FROM stage_runs "
            "WHERE invalidated_at >= ? ORDER BY invalidated_at", (sql_ts(since),))]
    except sqlite3.OperationalError:  # pre-v13 DB without the columns
        invalidated = []
    if invalidated:
        blocks += [para(f"Lost-output invalidation: {len(invalidated)} stage run(s) invalidated, "
                        "so those batches are eligible to rerun:", "warn"),
                   table(["Batch", "Stage", "Invalidated", "Reason"],
                         [[r["batch_id"], r["stage"], fmt(parse_ts(r["invalidated_at"])),
                           r["invalidated_reason"] or ""] for r in invalidated])]
        notes.append(f"{len(invalidated)} stage runs invalidated")
    else:
        blocks.append(para("Lost-output invalidation: nothing invalidated.", "muted"))

    if isinstance(queue, list):
        nxt = [q for q in queue if q["name"] == INDEX_JOB]
        if nxt:
            blocks.append(para(f"Next {INDEX_JOB}: job {nxt[0]['job_id']} {nxt[0]['state']}, "
                               f"start {fmt(parse_slurm_ts(nxt[0]['start']))}.", "muted"))
        else:
            blocks.append(para(f"No {INDEX_JOB} job is queued or running, so the nightly chain "
                               "has stopped. Restart it from the repo root with "
                               "sbatch --begin=00:00 scripts/admin/globus_index.sh", "bad"))
            levels.append("bad")
            notes.append("nightly chain NOT queued")

    level = "bad" if "bad" in levels else "warn" if "warn" in levels else "ok"
    return section("Globus indexing", "Indexing: " + "; ".join(notes), level, blocks)


def slurm_section(jobs: list[dict] | str) -> dict:
    if isinstance(jobs, str):
        return section("Slurm jobs", f"Slurm: {jobs}", "warn", [para(jobs, "warn")])
    pipeline = [j for j in jobs if j["name"] not in IGNORED_JOBS]
    if not pipeline:
        return section("Slurm jobs", "Slurm: no pipeline jobs ran", "",
                       [para("No pipeline Slurm jobs ran on this cluster in the window.", "muted")])
    groups: dict[str, Counter] = {}
    for job in pipeline:
        groups.setdefault(BATCH_SUFFIX_RE.sub("", job["name"]), Counter())[job["state"]] += 1
    blocks = [table(["Job name", "Jobs", "States"],
                    [[name, sum(c.values()), counts(c)] for name, c in sorted(groups.items())])]
    bad = [j for j in pipeline if j["state"] in BAD_JOB_STATES]
    if bad:
        blocks += [para(f"Jobs that ended badly ({len(bad)}); these never reach stage_runs:", "bad"),
                   table(["Job ID", "Name", "State", "Start", "End", "Elapsed", "Exit", "Log"],
                         [[j["job_id"], j["name"], j["state"], fmt(j["start"]), fmt(j["end"]),
                           j["elapsed"], j["exit_code"], j["stdout"]] for j in bad])]
    headline = f"Slurm: {len(pipeline)} pipeline job(s) — {counts(Counter(j['state'] for j in pipeline))}"
    return section("Slurm jobs", headline, "bad" if bad else "ok", blocks)


def leftovers_section(conn, since: datetime, now: datetime) -> dict:
    """Things that went wrong recently. Older leftovers (stuck for more than
    STALE_AFTER) are only counted, so they don't repeat in every report."""
    blocks, notes, older, any_failed = [], [], [], False
    stuck_before = now - timedelta(hours=24)
    old_before = now - STALE_AFTER

    leases = [dict(r) for r in conn.execute(
        "SELECT batch_id, stage, slurm_job_id, claimed_at, expires_at FROM stage_leases "
        "WHERE expires_at <= ? ORDER BY expires_at", (sql_ts(now),))]
    new = [r for r in leases if parse_ts(r["expires_at"]) >= since]
    if new:
        blocks += [para(f"Stage leases that expired in the window without being released ({len(new)}):",
                        "warn"),
                   table(["Batch", "Stage", "Slurm job", "Claimed", "Expired"],
                         [[r["batch_id"], r["stage"], r["slurm_job_id"] or "",
                           fmt(parse_ts(r["claimed_at"])), fmt(parse_ts(r["expires_at"]))]
                          for r in new])]
        notes.append(f"{len(new)} lease(s) expired")
    if len(leases) > len(new):
        older.append(f"{len(leases) - len(new)} expired lease(s) since {fmt(parse_ts(leases[0]['expires_at']))}")

    # Failed staged_inputs are listed per stage in stage_section, with the
    # batches submit.py skipped; here only transfers that are stuck open.
    for label, sql, open_states, with_failed, noun in (
        ("Input staging", "SELECT batch_id, stage, status, NULL AS attempt_count, globus_task_id, "
         "error_summary, updated_at FROM staged_inputs", OPEN_STAGING, False, "staging transfer(s)"),
        ("Result syncs", "SELECT batch_id, stage, status, attempt_count, globus_task_id, "
         "error_summary, updated_at FROM result_syncs", OPEN_SYNC, True, "result sync(s)"),
    ):
        placeholders = ",".join("?" * len(open_states))
        where, params = f"(status IN ({placeholders}) AND updated_at < ?)", [*open_states, sql_ts(stuck_before)]
        if with_failed:
            where, params = where + " OR (status = 'failed' AND updated_at >= ?)", params + [sql_ts(since)]
        rows = [dict(r) for r in conn.execute(f"{sql} WHERE {where} ORDER BY updated_at", params)]
        for r in rows:
            r["updated"] = parse_ts(r["updated_at"])
        shown = [r for r in rows if r["status"] == "failed" or r["updated"] >= old_before]
        if shown:
            failed = sum(r["status"] == "failed" for r in shown)
            what = "failed in the window, or open" if with_failed else "open"
            blocks += [para(f"{label} {what} and not updated for over 24 h ({len(shown)}):",
                            "bad" if failed else "warn"),
                       table(["Batch", "Stage", "Status", "Tries", "Globus task", "Updated", "Error"],
                             [[r["batch_id"], r["stage"], r["status"], r["attempt_count"] or "",
                               r["globus_task_id"] or "", fmt(r["updated"]),
                               (r["error_summary"] or "")[:160]] for r in shown])]
            notes.append(f"{len(shown)} {noun} failed/stuck")
            any_failed |= bool(failed)
        if len(rows) > len(shown):
            older.append(f"{len(rows) - len(shown)} {noun} open since {fmt(rows[0]['updated'])}")

    ingested = conn.execute("SELECT COUNT(*) FROM result_syncs WHERE status = 'ingested' "
                            "AND ingested_at >= ?", (sql_ts(since),)).fetchone()[0]
    if not notes:
        blocks.append(para("Nothing failed or got stuck in leases, input staging or result sync.", "ok"))
    blocks.append(para(f"Result syncs ingested in the window: {ingested}.", "muted"))
    if older:
        blocks.append(para(f"Older leftovers (stuck more than {STALE_AFTER.days} days, not listed): "
                           + "; ".join(older), "muted"))
    headline = "Leases/staging/sync: " + ("; ".join(notes) if notes else "nothing new stuck")
    level = "bad" if any_failed else "warn" if notes else "ok"
    return section("Leases, input staging, result sync", headline, level, blocks)


def queue_section(queue: list[dict] | str) -> dict:
    if isinstance(queue, str):
        return section("Queue now", "", "", [para(queue, "warn")])
    if not queue:
        return section("Queue now", "", "", [para("Nothing queued or running.", "muted")])
    return section("Queue now", "", "", [table(
        ["Job ID", "Name", "State", "Start", "Reason"],
        [[q["job_id"], q["name"], q["state"], fmt(parse_slurm_ts(q["start"])) if
          parse_slurm_ts(q["start"]) else q["start"], q["reason"]] for q in queue],
    )])


def build_report(conn, hours: int = 24, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    jobs, queue = slurm_jobs(since), slurm_queue()
    sections = [
        stage_section(conn, since, now, hours, jobs),
        indexing_section(conn, since, now, hours, jobs, queue),
        slurm_section(jobs),
        leftovers_section(conn, since, now),
        queue_section(queue),
    ]
    attention = any(s["level"] == "bad" for s in sections)
    day = now.astimezone(LOCAL_TZ).strftime("%a %b %d")
    return {
        "generated": now,
        "since": since,
        "hours": hours,
        "sections": sections,
        "attention": attention,
        "subject": f"AgIR pipeline report {day}: {'needs attention' if attention else 'OK'}",
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

MARKS = {"bad": "!!", "warn": " !"}
LEVEL_CLASS = {"bad": "lv-bad", "warn": "lv-warn", "ok": "lv-ok", "muted": "muted"}
STATUS_COLS = {"Status", "State"}


def _window(report: dict) -> str:
    tz = report["generated"].astimezone(LOCAL_TZ).tzname()
    return (f"generated {fmt(report['generated'])} {tz} · last {report['hours']} h, "
            f"since {fmt(report['since'])}")


def render_text(report: dict) -> str:
    out = [report["subject"], _window(report), ""]
    out += [f"{MARKS.get(s['level'], '  ')} {s['headline']}" for s in report["sections"] if s["headline"]]
    for sec in report["sections"]:
        out += ["", f"== {sec['title']} =="]
        for block in sec["blocks"]:
            if "text" in block:
                out.append(f"{MARKS.get(block['level'], '').strip()} {block['text']}".strip())
                continue
            rows = [[str(c) for c in row] for row in block["rows"]]
            widths = [max(map(len, col)) for col in zip(block["cols"], *rows)]
            out += ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip()
                    for row in [block["cols"], *rows]]
            if block["more"]:
                out.append(f"  … and {block['more']} more")
    return "\n".join(out) + "\n"


def render_html(report: dict) -> str:
    """HTML fragment shared by the email and the dashboard panel."""
    e = lambda v: html.escape(str(v))
    out = [f'<div class="rp"><p class="muted">{e(_window(report))}</p><ul>']
    out += [f'<li class="{LEVEL_CLASS.get(s["level"], "")}">{e(s["headline"])}</li>'
            for s in report["sections"] if s["headline"]]
    out.append("</ul>")
    for sec in report["sections"]:
        out.append(f"<h3>{e(sec['title'])}</h3>")
        for block in sec["blocks"]:
            if "text" in block:
                out.append(f'<p class="{LEVEL_CLASS.get(block["level"], "")}">{e(block["text"])}</p>')
                continue
            status = [c in STATUS_COLS for c in block["cols"]]
            head = "".join(f"<th>{e(c)}</th>" for c in block["cols"])
            body = "".join(
                "<tr>" + "".join(f'<td><span class="s-{e(v)}">{e(v)}</span></td>' if is_status
                                 else f"<td>{e(v)}</td>" for v, is_status in zip(row, status))
                + "</tr>" for row in block["rows"])
            out.append(f'<div class="rp-table"><table><tr>{head}</tr>{body}</table></div>')
            if block["more"]:
                out.append(f'<p class="muted">… and {block["more"]} more</p>')
    out.append("</div>")
    return "\n".join(out)


EMAIL_CSS = """
body { font: 14px/1.45 -apple-system, "Segoe UI", Arial, sans-serif; color: #1c2330; }
h3 { font-size: 15px; margin: 20px 0 6px; padding-bottom: 3px; border-bottom: 1px solid #dfe3ea; }
p { margin: 6px 0; } ul { padding-left: 20px; }
table { border-collapse: collapse; font-size: 12.5px; margin: 4px 0 10px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #dfe3ea; vertical-align: top; }
th { color: #5d6778; font-weight: 600; }
.muted, .s-skipped { color: #5d6778; }
.lv-ok, .s-success, .s-COMPLETED, .s-ingested { color: #2f7d4f; }
.lv-warn, .s-partial, .s-partial_success, .s-running, .s-RUNNING, .s-PENDING, .s-active,
.s-requested, .s-transferring { color: #a86a12; }
.lv-bad, .s-failed, .s-canceled, .s-FAILED, .s-TIMEOUT, .s-OUT_OF_MEMORY, .s-NODE_FAIL,
.s-CANCELLED, .s-BOOT_FAIL, .s-PREEMPTED, .s-DEADLINE { color: #b3372f; font-weight: 600; }
"""


def send_email(to: list[str], sender: str, subject: str, text: str, html_body: str | None = None):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(
            '<!doctype html><html><head><meta charset="utf-8"><style>' + EMAIL_CSS
            + "</style></head><body>" + html_body + "</body></html>", subtype="html")
    subprocess.run([SENDMAIL, "-t", "-oi"], input=msg.as_bytes(), check=True, timeout=120)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--hours", type=int, default=24, help="Report window (default 24).")
    ap.add_argument("--email", nargs="+", metavar="ADDR",
                    help="Also email the report to these addresses via sendmail.")
    ap.add_argument("--from", dest="sender", default=DEFAULT_FROM, help=f"Default {DEFAULT_FROM}.")
    args = ap.parse_args()

    try:
        conn = open_db(args.db, readonly=True)
        try:
            report = build_report(conn, args.hours)
        finally:
            conn.close()
    except Exception:
        if not args.email:
            raise
        # Still email, so a broken report doesn't look like a quiet night.
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        day = datetime.now(LOCAL_TZ).strftime("%a %b %d")
        send_email(args.email, args.sender, f"AgIR pipeline report {day}: report FAILED",
                   f"scripts/admin/nightly_report.py failed on {os.uname().nodename}:\n\n{tb}")
        return 1

    text = render_text(report)
    print(text)
    if args.email:
        send_email(args.email, args.sender, report["subject"], text, render_html(report))
        print(f"Emailed to {', '.join(args.email)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
