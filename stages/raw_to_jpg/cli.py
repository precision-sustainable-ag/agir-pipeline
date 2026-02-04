#!/usr/bin/env python3
"""
CLI for RAW -> DNG -> JPG processing pipeline.
"""

import argparse
from pathlib import Path
from processor import Processor

def main():
    parser = argparse.ArgumentParser(
        description="Process RAW images to DNG and then to JPG using a camera config."
    )
    parser.add_argument(
        "--c",
        type=Path,
        required=True,
        help="Path to camera YAML configuration file."
    )
    parser.add_argument(
        "--i",
        type=Path,
        required=True,
        help="Directory containing RAW images to process."
    )
    parser.add_argument(
        "--o",
        type=Path,
        required=True,
        help="Directory where JPG images will be saved."
    )
    parser.add_argument(
        "--t",
        type=int,
        default=0,
        help="Number of parallel threads. Default 0 = sequential processing."
    )
    parser.add_argument(
        "--fs",
        action="store_true",
        help="Stop processing on first failure."
    )

    args = parser.parse_args()

    # Validate directories
    if not args.input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # initialize processor
    processor = Processor(args.config)

    # gather raw files
    raw_files = sorted(args.input_dir.glob("*.raw"))

    if not raw_files:
        print(f"No RAW files found in {args.input_dir}")
        return

    print(f"Processing {len(raw_files)} RAW files to {args.output_dir} ...")

    # Process batch
    results = processor.process_batch(
        raw_images=raw_files,
        output_dir=args.output_dir,
        fail_stop=args.fail_stop,
        max_workers=args.threads
    )

    print(f"Finished processing {len(results)} files.")


if __name__ == "__main__":
    main()

