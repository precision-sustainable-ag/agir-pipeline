#!/usr/bin/env python3
"""
CLI for RAW -> DNG -> JPG processing pipeline.
Supports processing a single file or a directory of RAW images.
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

    # Mutually exclusive group for directory or single file
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--i",
        type=Path,
        help="Directory containing RAW images to process."
    )
    group.add_argument(
        "--f",
        type=Path,
        help="Single RAW file to process."
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

    # Validate output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # initialize processor
    processor = Processor(args.c)

    # gather raw files
    if args.i:
        if not args.i.exists():
            raise FileNotFoundError(f"Input directory does not exist: {args.i}")
        raw_files = sorted(args.i.glob("*.raw"))
        if not raw_files:
            print(f"No RAW files found in {args.i}")
            return
    else:
        if not args.f.exists():
            raise FileNotFoundError(f"RAW file does not exist: {args.f}")
        raw_files = [args.f]

    print(f"Processing {len(raw_files)} RAW files to {args.o} ...")

    # Process batch
    results = processor.process_batch(
        raw_images=raw_files,
        output_dir=args.o,
        fail_stop=args.fs,
        max_workers=args.t
    )

    print(f"Finished processing {len(results)} files.")
    for f in results:
        print(f" - {f}")


if __name__ == "__main__":
    main()
