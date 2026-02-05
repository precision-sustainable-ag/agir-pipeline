#!/usr/bin/env python3
"""
CLI for RAW -> DNG -> JPG processing pipeline with automatic batch logging.
"""

import argparse
from pathlib import Path
import sqlite3
from datetime import datetime
from .processor import Processor

LOG_DB_PATH = Path("logs.db")


def log_batch_run(num_files: int, output_dir: Path, success: bool):
    """Automatically log batch processing to logs.db.stage_runs."""
    LOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOG_DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS stage_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            num_files INTEGER,
            output_dir TEXT,
            success INTEGER
        )
    """)
    conn.commit()

    c.execute("""
        INSERT INTO stage_runs (timestamp, num_files, output_dir, success)
        VALUES (?, ?, ?, ?)
    """, (datetime.utcnow().isoformat(), num_files, str(output_dir), int(success)))
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Process RAW images to DNG and then to JPG using a camera config."
    )
    parser.add_argument("--c", type=Path, required=True, help="Path to camera YAML configuration file.")
    parser.add_argument("--i", type=Path, required=True, help="Directory containing RAW images to process.")
    parser.add_argument("--o", type=Path, required=True, help="Directory where JPG images will be saved.")
    parser.add_argument("--t", type=int, default=0, help="Number of parallel threads. Default 0 = sequential processing.")
    parser.add_argument("--fs", action="store_true", help="Stop processing on first failure.")

    args = parser.parse_args()

    # Validate directories
    if not args.i.exists():
        raise FileNotFoundError(f"Input directory does not exist: {args.i}")
    args.o.mkdir(parents=True, exist_ok=True)

    # Initialize processor
    processor = Processor(args.c)

    # Gather raw files
    raw_files = list(args.i.glob("*.RAW"))
    if not raw_files:
        print(f"No RAW files found in {args.i}")
        return

    print(f"Processing {len(raw_files)} RAW files to {args.o} ...")

    # Process batch
    success = True
    try:
        results = processor.process_batch(
            raw_images=raw_files,
            output_dir=args.o,
            fail_stop=args.fs,
            max_workers=args.t
        )
        print(f"Finished processing {len(results)} files.")
        for f in results:
            print(f" - {f}")
    except Exception as e:
        success = False
        print(f"Batch processing failed: {e}")

    # Log batch run automatically
    log_batch_run(num_files=len(raw_files), output_dir=args.o, success=success)


if __name__ == "__main__":
    main()
