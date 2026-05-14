import argparse
import time
from collections import Counter
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Iterable

from agir_db import AgirDB, setup_logging

log = setup_logging()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate and execute Globus transfer batch files with optional batch filters."
    )

    parser.add_argument(
        "--states",
        nargs="+",
        choices=["MD", "NC", "TX"],
        default=None,
        help="Filter batches by state prefix (e.g. --states NC MD).",
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Inclusive start date filter in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Inclusive end date filter in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="If set, do not execute the Globus transfer.",
    )

    return parser.parse_args()


def parse_iso_date(date_str: Optional[str]) -> Optional[date]:
    """Parse a YYYY-MM-DD string into a date object."""
    if date_str is None:
        return None

    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"Invalid date '{date_str}'. Expected format YYYY-MM-DD."
        ) from exc


def parse_batch_id(batch_id: str) -> tuple[str, date]:
    """
    Parse batch IDs of the form STATE_YYYY-MM-DD.

    Examples
    --------
    NC_2025-01-01
    MD_2024-07-12
    TX_2026-03-13
    """
    try:
        state, date_str = batch_id.split("_", 1)
        batch_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"Batch ID '{batch_id}' does not match expected format STATE_YYYY-MM-DD."
        ) from exc

    if state not in {"MD", "NC", "TX"}:
        raise ValueError(
            f"Batch ID '{batch_id}' has unexpected state prefix '{state}'. "
            "Expected one of: MD, NC, TX."
        )

    return state, batch_date


def batch_matches_filters(
    batch_id: str,
    states_filter: Optional[Iterable[str]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> bool:
    """Return True if the batch passes all provided filters."""
    state, batch_date = parse_batch_id(batch_id)

    if states_filter is not None and state not in set(states_filter):
        return False

    if start_date is not None and batch_date < start_date:
        return False

    if end_date is not None and batch_date > end_date:
        return False

    return True


def write_transfer_report(
    report_path: Path,
    report_rows: list[dict],
    transfer_file_paths: list[str],
    states_filter: Optional[Iterable[str]],
    start_date: Optional[date],
    end_date: Optional[date],
    dry_run: bool,
) -> None:
    """Write a human-readable summary report for generated transfer files."""

    written_rows = [row for row in report_rows if row["status"] == "written"]
    skipped_rows = [row for row in report_rows if row["status"].startswith("skipped")]

    written_dates = []
    written_states = []

    for row in written_rows:
        try:
            state, batch_date = parse_batch_id(row["batch_id"])
            written_states.append(state)
            written_dates.append(batch_date)
        except ValueError:
            continue

    state_counts = Counter(written_states)

    with open(report_path, "w") as f:
        f.write("Globus Transfer Batch Report\n")
        f.write("============================\n")
        f.write(f"Generated at: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"Dry run: {dry_run}\n\n")

        f.write("Filters\n")
        f.write("-------\n")
        f.write(f"States: {', '.join(states_filter) if states_filter else 'None'}\n")
        f.write(f"Start date: {start_date if start_date else 'None'}\n")
        f.write(f"End date: {end_date if end_date else 'None'}\n\n")

        f.write("Summary\n")
        f.write("-------\n")
        f.write(f"Total DB batches found: {sum(row['db_batches_found'] for row in report_rows if row['row_type'] == 'summary')}\n")
        f.write(f"Total batches after removing 90daydata: {sum(row['refined_batches_found'] for row in report_rows if row['row_type'] == 'summary')}\n")
        f.write(f"Total transfer entries written: {len(written_rows)}\n")
        f.write(f"Total skipped batches: {len(skipped_rows)}\n")

        if written_dates:
            f.write(f"Written batch date range: {min(written_dates)} to {max(written_dates)}\n")
        else:
            f.write("Written batch date range: None\n")

        f.write("\nWritten batches by state\n")
        f.write("------------------------\n")
        if state_counts:
            for state in sorted(state_counts):
                f.write(f"{state}: {state_counts[state]}\n")
        else:
            f.write("None\n")

        f.write("\nGenerated transfer batch files\n")
        f.write("------------------------------\n")
        for path in transfer_file_paths:
            f.write(f"{path}\n")

        f.write("\nSkipped batches\n")
        f.write("---------------\n")
        if skipped_rows:
            for row in skipped_rows:
                f.write(
                    f"{row['batch_id']} | {row['data_state']} | {row['copy_domain']} | "
                    f"{row['status']} | {row.get('reason', '')}\n"
                )
        else:
            f.write("None\n")

        f.write("\nAll source and destination paths\n")
        f.write("--------------------------------\n")
        if written_rows:
            for row in written_rows:
                f.write(f"{row['src_path']} -> {row['dst_path']}\n")
        else:
            f.write("None\n")


def write_globus_transfer_batch_file(
    data_state: str,
    copy_domain: str,
    file_path: str,
    states_filter: Optional[Iterable[str]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[dict]:
    """
    Write a Globus transfer batch file for a given data_state/copy_domain,
    optionally filtering batches by state and date range.

    Returns
    -------
    list[dict]
        Report rows describing summary information, written paths, and skipped batches.
    """
    report_rows = []

    with AgirDB() as db:
        batches = db.transfers.get_batches_to_transfer(
            copy_domain=copy_domain,
            data_state=data_state,
            pick_policy="most_files",
        )

        refined_batches = []
        skipped_90daydata_count = 0

        for batch in batches:
            if "90daydata" in batch["storage_root"]:
                skipped_90daydata_count += 1
                continue
            refined_batches.append(batch)

        report_rows.append(
            {
                "row_type": "summary",
                "status": "summary",
                "data_state": data_state,
                "copy_domain": copy_domain,
                "db_batches_found": len(batches),
                "refined_batches_found": len(refined_batches),
                "skipped_90daydata_count": skipped_90daydata_count,
            }
        )

        written_count = 0
        skipped_count = 0

        with open(file_path, "w") as f:
            for batch in refined_batches:
                batch_id = batch["batch_id"]

                try:
                    if not batch_matches_filters(
                        batch_id=batch_id,
                        states_filter=states_filter,
                        start_date=start_date,
                        end_date=end_date,
                    ):
                        skipped_count += 1
                        report_rows.append(
                            {
                                "row_type": "batch",
                                "status": "skipped_filter",
                                "reason": "Did not match state/date filters",
                                "data_state": data_state,
                                "copy_domain": copy_domain,
                                "batch_id": batch_id,
                                "src_path": None,
                                "dst_path": None,
                            }
                        )
                        continue

                except ValueError as exc:
                    skipped_count += 1
                    log.warning(
                        f"Skipping batch with invalid batch_id '{batch_id}': {exc}"
                    )
                    report_rows.append(
                        {
                            "row_type": "batch",
                            "status": "skipped_invalid_batch_id",
                            "reason": str(exc),
                            "data_state": data_state,
                            "copy_domain": copy_domain,
                            "batch_id": batch_id,
                            "src_path": None,
                            "dst_path": None,
                        }
                    )
                    continue

                storage_root = batch["storage_root"]
                src_path = Path(storage_root) / data_state / batch_id
                dst_path = Path(f"/LTS/project/dash_agir/{data_state}/{batch_id}")

                if "developed" in data_state:
                    src_path = src_path / batch["parent_dir"]
                    dst_path = dst_path / batch["parent_dir"]

                f.write(f"{src_path} {dst_path} --recursive\n")

                report_rows.append(
                    {
                        "row_type": "batch",
                        "status": "written",
                        "reason": "",
                        "data_state": data_state,
                        "copy_domain": copy_domain,
                        "batch_id": batch_id,
                        "src_path": str(src_path),
                        "dst_path": str(dst_path),
                    }
                )

                written_count += 1

        log.info(
            f"Wrote {written_count} transfer entries to {file_path} "
            f"(skipped {skipped_count} due to filters or invalid batch IDs; "
            f"removed {skipped_90daydata_count} 90daydata batches)."
        )

    return report_rows


def main():
    args = parse_args()

    start_date = parse_iso_date(args.start_date)
    end_date = parse_iso_date(args.end_date)

    if start_date and end_date and start_date > end_date:
        raise ValueError(
            f"start-date ({start_date}) cannot be later than end-date ({end_date})."
        )

    # Define endpoints
    juno_endpoint = "904c2108-90cf-11e8-9672-0a6d4e044368"
    ncsu_endpoint = "2f7f6170-8d5c-11e9-8e6a-029d279f7e24"

    data_options = {
        "semifield-upload": [
            "upload_raw"
        ],
        # "semifield-developed-images": [
        #     "developed_images_jpg",
        #     "developed_metadata_json"
        # ]
    }

    file_paths = []
    all_report_rows = []

    script_root = Path(
        "/project/dash_agir/matthew.kutugata/repos/agir-pipeline/scripts/"
    )
    timestamp = int(time.time())

    report_path = script_root / f"globus_transfer_report_{timestamp}.txt"

    for data_state, copy_domains in data_options.items():
        for copy_domain in copy_domains:
            file_path = (
                script_root
                / f"globus_transfer_{data_state}_{copy_domain}_{timestamp}.txt"
            )

            report_rows = write_globus_transfer_batch_file(
                data_state=data_state,
                copy_domain=copy_domain,
                file_path=str(file_path),
                states_filter=args.states,
                start_date=start_date,
                end_date=end_date,
            )

            all_report_rows.extend(report_rows)
            file_paths.append(str(file_path))

            log.info(f"Globus transfer batch file written to: {file_path}")

            if file_path.stat().st_size == 0:
                log.warning(f"Skipping transfer because batch file is empty: {file_path}")
                continue

            cmd = [
                "globus",
                "transfer",
                ncsu_endpoint,
                juno_endpoint,
                "--batch",
                str(file_path),
                "--label",
                f"NCSU_to_JUNO_{data_state}_{copy_domain}_batch_transfer",
            ]

            with AgirDB() as db:
                db.transfers.execute_transfer(
                    cmd=cmd,
                    dry_run=args.dry_run,
                )

    write_transfer_report(
        report_path=report_path,
        report_rows=all_report_rows,
        transfer_file_paths=file_paths,
        states_filter=args.states,
        start_date=start_date,
        end_date=end_date,
        dry_run=args.dry_run,
    )

    log.info(f"Globus transfer report written to: {report_path}")


if __name__ == "__main__":
    main()