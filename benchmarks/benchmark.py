"""Compare the original pydupes with dupfinder on three synthetic folders.

For each scenario this script:
  1. builds the folder with make_dataset.py (same seed every time),
  2. runs the original once with its md5sum wrapped, to count bytes read,
  3. times the original and the new code 3 times each and takes the median,
  4. checks that both report exactly the same duplicate groups,
  5. deletes the folder.

The main timings use a warm page cache (the files were just written or
read), so they mostly measure hashing speed, not disk speed. If the script
may write to /proc/sys/vm/drop_caches (Linux, as root), it also times both
versions on the mixed folder with a cold cache, emptying the cache before
every run.

Usage:
  python benchmarks/benchmark.py            # all scenarios, writes results.md
  python benchmarks/benchmark.py --keep-data
"""

import argparse
import hashlib
import os
import platform
import shutil
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# Make the original file and the new package importable without installing.
sys.path.insert(0, str(REPO / "original"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import duplicates  # noqa: E402  (the original pydupes, unchanged)
import make_dataset  # noqa: E402
from dupfinder import core  # noqa: E402

DATA_DIR = HERE / "data"
RESULTS_FILE = HERE / "results.md"
REPEATS = 3
MB = 1_000_000
MIB = 1024 * 1024


def cpu_name() -> str:
    """Read the CPU model on Linux, with a fallback for other systems."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def original_groups(md5_dict: dict[str, list[str]]) -> list[list[str]]:
    """Turn the original's {md5: [paths]} into the same shape find_duplicates returns."""
    return sorted(sorted(paths) for paths in md5_dict.values() if len(paths) >= 2)


def count_original_reads(root: str) -> tuple[int, int, list[list[str]]]:
    """Run the original once with md5sum wrapped. Returns (bytes read, files hashed, groups)."""
    real_md5sum = duplicates.md5sum
    counts = {"bytes": 0, "files": 0}

    def counting_md5sum(fpath: str) -> str:
        digest = real_md5sum(fpath)
        # md5sum always reads the whole file, so the bytes read is its size.
        counts["bytes"] += os.path.getsize(fpath)
        counts["files"] += 1
        return digest

    # find_dup1 looks up md5sum in the module each time it is called, so
    # replacing the module attribute is enough to count every call.
    duplicates.md5sum = counting_md5sum
    try:
        md5_dict, _ = duplicates.find_dup(root, 1)
    finally:
        duplicates.md5sum = real_md5sum
    return counts["bytes"], counts["files"], original_groups(md5_dict)


def timed(func, *args, **kwargs) -> tuple[float, object]:
    start = time.perf_counter()
    value = func(*args, **kwargs)
    return time.perf_counter() - start, value


def drop_page_cache() -> bool:
    """Ask Linux to forget cached file data, so the next read hits the disk.

    Returns False where this is not allowed (not Linux, or not root).
    """
    try:
        os.sync()  # write out pending data first, or it cannot be dropped
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")  # 3 = file contents plus directory and inode caches
        return True
    except OSError:
        return False


def cold_times(root: str, workers: int) -> tuple[float, float]:
    """Median time of the original and the new code, with the cache emptied before each run."""
    original, new = [], []
    for _ in range(REPEATS):
        drop_page_cache()
        original.append(timed(duplicates.find_dup, root, 1)[0])
        drop_page_cache()
        new.append(timed(core.scan, root, workers=workers)[0])
    return statistics.median(original), statistics.median(new)


def run_scenario(name: str, workers: int, keep_data: bool, cold: bool) -> dict:
    root_path = DATA_DIR / name
    if root_path.exists():
        shutil.rmtree(root_path)
    print(f"[{name}] generating dataset...", flush=True)
    builder = make_dataset.SCENARIOS[name](root_path)
    root = str(root_path)
    # Write the new files out to disk now, so the kernel is not still
    # flushing them in the background while we time the runs.
    os.sync()

    try:
        orig_bytes, orig_files, orig_groups = count_original_reads(root)

        times: dict[str, list[float]] = {"original": [], "new_1": [], "new_n": [], "no_cutoff": []}
        new_results = []
        # Interleave the runs, so that if the machine slows down for a
        # moment it slows all of them roughly equally.
        for _ in range(REPEATS):
            t, _ = timed(duplicates.find_dup, root, 1)
            times["original"].append(t)
            t, result_1 = timed(core.scan, root, workers=1)
            times["new_1"].append(t)
            t, result_n = timed(core.scan, root, workers=workers)
            times["new_n"].append(t)
            # Once more with every file sent to the thread pool, however
            # small, to show why THREAD_MIN_SIZE exists.
            saved = core.THREAD_MIN_SIZE
            core.THREAD_MIN_SIZE = 0
            try:
                t, result_all = timed(core.scan, root, workers=workers)
            finally:
                core.THREAD_MIN_SIZE = saved
            times["no_cutoff"].append(t)
            new_results += [result_1, result_n, result_all]

        groups_match = all(r.groups == orig_groups for r in new_results)
        new = new_results[-1]
        row = {
            "name": name,
            "files": builder.counter,
            "size_mb": builder.total_bytes / MB,
            "dup_groups": len(orig_groups),
            "t_orig": statistics.median(times["original"]),
            "t_new_1": statistics.median(times["new_1"]),
            "t_new_n": statistics.median(times["new_n"]),
            "t_no_cutoff": statistics.median(times["no_cutoff"]),
            "orig_bytes": orig_bytes,
            "new_bytes": new.bytes_read,
            "orig_full": orig_files,
            "new_full": new.files_read_in_full,
            "stage2": new.stage2_files,
            "stage3": new.stage3_files,
            "match": groups_match,
        }
        if cold:
            row["cold_orig"], row["cold_new"] = cold_times(root, workers)
        print(f"[{name}] done: original {row['t_orig']:.2f}s, new {row['t_new_n']:.2f}s, match={groups_match}")
        return row
    finally:
        if not keep_data:
            shutil.rmtree(root_path)


def hash_throughput(seconds_each: float = 1.0) -> list[tuple[str, float]]:
    """Measure how fast each hash runs on data that is already in memory."""
    block = os.urandom(MIB)
    rows = []
    for name in ["md5", "blake2b", "sha1"]:
        hasher = hashlib.new(name)
        hashed = 0
        start = time.perf_counter()
        while time.perf_counter() - start < seconds_each:
            hasher.update(block)
            hashed += 1
        rows.append((name, hashed / (time.perf_counter() - start)))
    return rows


def file_digest_check(seconds_each: float = 1.0) -> list[tuple[str, float]]:
    """Compare our 1 MiB read loop with hashlib.file_digest on one 64 MiB file."""
    if not hasattr(hashlib, "file_digest"):  # added in Python 3.11
        return []
    path = DATA_DIR / "file_digest_test.bin"
    path.write_bytes(os.urandom(64 * MIB))
    rows = []
    try:
        for name in ["md5", "blake2b"]:
            for label in ["1 MiB read loop", "hashlib.file_digest"]:
                core.HASH_NAME = name
                passes = 0
                start = time.perf_counter()
                while time.perf_counter() - start < seconds_each:
                    if label == "hashlib.file_digest":
                        with open(path, "rb") as f:
                            hashlib.file_digest(f, name)
                    else:
                        core.hash_whole_file(str(path))
                    passes += 1
                rows.append((f"{name}, {label}", passes * 64 / (time.perf_counter() - start)))
    finally:
        core.HASH_NAME = "blake2b"
        path.unlink()
    return rows


def build_report(rows: list[dict], workers: int, hash_rows, digest_rows) -> str:
    lines = [
        "# Benchmark results",
        "",
        f"- Python {platform.python_version()} on {platform.system()} {platform.release()}",
        f"- CPU: {cpu_name()}, {os.cpu_count()} logical CPUs",
        f"- Median of {REPEATS} runs per cell, warm page cache unless the section says otherwise",
        f"- New code: hash {core.HASH_NAME}, block size {core.BLOCK_SIZE // MIB} MiB",
        "",
        "## Original vs new",
        "",
        f"| Scenario | Files | Size (MB) | Original (s) | New, {workers} threads (s) | Speedup "
        "| MB hashed, original | MB hashed, new | Files fully hashed, original | Files fully hashed, new "
        "| Same groups |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['files']:,} | {r['size_mb']:,.0f} | {r['t_orig']:.2f} | {r['t_new_n']:.2f} "
            f"| {r['t_orig'] / r['t_new_n']:.1f}x | {r['orig_bytes'] / MB:,.0f} | {r['new_bytes'] / MB:,.0f} "
            f"| {r['orig_full']:,} | {r['new_full']:,} | {'yes' if r['match'] else 'NO'} ({r['dup_groups']} groups) |"
        )
    lines += [
        "",
        "## Files left after each stage (new code)",
        "",
        "| Scenario | Files listed | Stage 2: share a size | Stage 3: share size and first block | Duplicate groups |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(f"| {r['name']} | {r['files']:,} | {r['stage2']:,} | {r['stage3']:,} | {r['dup_groups']} |")
    lines += [
        "",
        "## Threads",
        "",
        f"| Scenario | 1 thread (s) | {workers} threads (s) | Change | {workers} threads, no size cutoff (s) |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['t_new_1']:.2f} | {r['t_new_n']:.2f} | {r['t_new_1'] / r['t_new_n']:.2f}x "
            f"| {r['t_no_cutoff']:.2f} |"
        )
    cold_rows = [r for r in rows if "cold_orig" in r]
    if cold_rows:
        lines += [
            "",
            "## Cold cache",
            "",
            "Page cache emptied (drop_caches) before every run. Median of "
            f"{REPEATS} runs. This VM's disk is virtual, so it may itself be cached by the host.",
            "",
            f"| Scenario | Original (s) | New, {workers} threads (s) | Speedup |",
            "|---|---:|---:|---:|",
        ]
        for r in cold_rows:
            lines.append(
                f"| {r['name']} | {r['cold_orig']:.2f} | {r['cold_new']:.2f} | {r['cold_orig'] / r['cold_new']:.1f}x |"
            )
    lines += [
        "",
        "## Hash speed",
        "",
        "| Test | MiB/s |",
        "|---|---:|",
    ]
    for label, speed in hash_rows:
        lines.append(f"| {label}, data already in memory | {speed:,.0f} |")
    for label, speed in digest_rows:
        lines.append(f"| {label}, 64 MiB file, warm cache | {speed:,.0f} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark original pydupes against dupfinder.")
    parser.add_argument("--scenarios", nargs="+", default=list(make_dataset.SCENARIOS), choices=make_dataset.SCENARIOS)
    parser.add_argument("--workers", type=int, default=core.DEFAULT_WORKERS)
    parser.add_argument("--keep-data", action="store_true", help="do not delete the generated folders")
    args = parser.parse_args()

    print(f"Python {platform.python_version()}, CPU: {cpu_name()} ({os.cpu_count()} logical CPUs)")
    DATA_DIR.mkdir(exist_ok=True)
    start = time.perf_counter()

    cold = drop_page_cache()
    print("cold-cache runs: " + ("yes" if cold else "no (cannot write /proc/sys/vm/drop_caches)"))

    # Cold runs take longer, so they are done for the mixed folder only.
    rows = [run_scenario(name, args.workers, args.keep_data, cold and name == "mixed") for name in args.scenarios]
    print("measuring hash speed...", flush=True)
    hash_rows = hash_throughput()
    digest_rows = file_digest_check()

    report = build_report(rows, args.workers, hash_rows, digest_rows)
    print()
    print(report)
    RESULTS_FILE.write_text(report)
    print(f"Wrote {RESULTS_FILE.relative_to(REPO)} in {time.perf_counter() - start:.0f}s total.")

    if not all(r["match"] for r in rows):
        sys.exit("ERROR: duplicate groups differ from the original")


if __name__ == "__main__":
    main()
