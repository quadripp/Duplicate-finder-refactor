"""Command line interface: python -m dupfinder PATH [--no-recursive] [--json] [--workers N]"""

import argparse
import json
import sys

from dupfinder.core import DEFAULT_WORKERS, ScanResult, scan


def print_report(result: ScanResult) -> None:
    """Print each duplicate group, then a summary laid out like the original's dup_summary."""
    for group in result.groups:
        print(f"{len(group)} identical files:")
        for path in group:
            print(f"    {path}")
        print()

    rows = [
        ("Total Files    ", result.total_files),
        ("Distinct Files ", result.distinct_files),
        ("Duplicate Files", result.duplicate_files),
        ("Skipped Files  ", len(result.skipped)),
    ]
    # Right-align the numbers, as the original did.
    width = max(len(str(value)) for _, value in rows)
    for label, value in rows:
        print(f"{label}: {str(value).rjust(width)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dupfinder", description="Find duplicate files.")
    parser.add_argument("path", help="folder to search")
    parser.add_argument("--no-recursive", action="store_true", help="only look at files directly inside PATH")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"threads used for hashing large files (default {DEFAULT_WORKERS}; 1 turns threading off)",
    )
    args = parser.parse_args(argv)

    if args.workers < 1:
        parser.error("--workers must be at least 1")

    try:
        result = scan(args.path, recursive=not args.no_recursive, workers=args.workers)
    except NotADirectoryError as error:
        parser.error(str(error))

    if args.json:
        output = {
            "groups": result.groups,
            "total_files": result.total_files,
            "distinct_files": result.distinct_files,
            "duplicate_files": result.duplicate_files,
            "skipped_files": len(result.skipped),
            "skipped": sorted(result.skipped),
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
