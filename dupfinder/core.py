"""Find duplicate files without reading every file in full.

The original pydupes computed an MD5 of every file. This module gets the
same answer in three stages, and each stage only looks at the files the
previous one could not rule out:

1. Group files by size. A file whose size no other file has cannot have
   a duplicate, so it is never opened.
2. For files that share a size, hash only the first block (1 MiB).
   Files that differ near the start drop out here.
3. For files whose size and first-block hash both match, hash the whole
   file. Only files that are very likely duplicates get this far.

A file no bigger than one block is finished after stage 2, because the
first-block hash already covers all of its content.
"""

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# Read files in 1 MiB pieces. Big enough that Python's per-call overhead is
# small, small enough that memory use stays flat even for very large files.
BLOCK_SIZE = 1024 * 1024

# BLAKE2b ran at the same speed as MD5 on the test machine, and unlike MD5
# nobody knows how to build two different files with the same BLAKE2b hash.
# See the README for the measurements.
HASH_NAME = "blake2b"

# Up to 4 threads, but never more than the machine has CPUs. With a warm
# cache the work is mostly hashing, which cannot use more cores than exist.
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)

# Files smaller than this are hashed in the main thread. For a tiny file,
# handing it to a worker thread costs more than hashing it. Measured: on
# 20,000 files under 16 KB, sending every file to the pool made the run
# about 3 times slower than doing them one by one.
THREAD_MIN_SIZE = 256 * 1024

# A hash function here takes a path and returns (hex digest, bytes read).
HashFunc = Callable[[str], tuple[str, int]]


@dataclass
class ScanResult:
    """Everything one scan found, plus counters used by the CLI and benchmark."""

    groups: list[list[str]] = field(default_factory=list)
    total_files: int = 0  # regular files checked (skipped files are not counted)
    skipped: list[str] = field(default_factory=list)  # paths that raised OSError
    bytes_read: int = 0  # bytes read from disk while hashing
    files_read_in_full: int = 0  # files whose whole content was hashed
    stage2_files: int = 0  # files that shared a size, so had their first block hashed
    stage3_files: int = 0  # files that also shared a first block, so were hashed in full

    @property
    def duplicate_files(self) -> int:
        """Extra copies. A group of 3 identical files counts as 2, like the original."""
        return sum(len(group) - 1 for group in self.groups)

    @property
    def distinct_files(self) -> int:
        """Number of different contents among the checked files.

        The original counted its hash dictionary. We never hash unique files,
        but every file is either the first of its kind or an extra copy, so
        total minus extra copies gives the same number.
        """
        return self.total_files - self.duplicate_files


def hash_first_block(path: str) -> tuple[str, int]:
    """Hash only the first BLOCK_SIZE bytes of a file."""
    hasher = hashlib.new(HASH_NAME)
    with open(path, "rb") as f:
        data = f.read(BLOCK_SIZE)
    hasher.update(data)
    return hasher.hexdigest(), len(data)


def hash_whole_file(path: str) -> tuple[str, int]:
    """Hash the whole file, reading BLOCK_SIZE bytes at a time.

    This reads the first block again even though stage 2 already read it.
    Carrying the stage 2 hash object over would save 1 MiB per file but
    make the code harder to follow, and stage 3 sees few files.
    """
    hasher = hashlib.new(HASH_NAME)
    bytes_read = 0
    with open(path, "rb") as f:
        # Reading in blocks keeps memory flat, even for a 50 GB file.
        while block := f.read(BLOCK_SIZE):
            hasher.update(block)
            bytes_read += len(block)
    return hasher.hexdigest(), bytes_read


def _try_hash(hash_func: HashFunc, path: str) -> tuple[str | None, int]:
    """Run hash_func, but return (None, 0) instead of raising if the file is unreadable.

    Catching the error here, inside the worker, means one bad file cannot
    stop the whole scan.
    """
    try:
        return hash_func(path)
    except OSError:
        # Covers PermissionError, FileNotFoundError (deleted mid-scan),
        # IsADirectoryError and disk read errors.
        return None, 0


def _hash_files(
    hash_func: HashFunc, files: list[tuple[str, int]], workers: int
) -> dict[str, tuple[str | None, int]]:
    """Hash each (path, size) in files and return {path: (digest or None, bytes read)}.

    Small files are hashed one by one in this thread. Large files go to a
    thread pool when workers > 1.
    """
    if workers <= 1:
        small = [path for path, _ in files]
        large = []
    else:
        small = [path for path, size in files if size < THREAD_MIN_SIZE]
        large = [path for path, size in files if size >= THREAD_MIN_SIZE]

    results = {path: _try_hash(hash_func, path) for path in small}

    if large:
        # hashlib releases the GIL while it hashes a big buffer, and a file
        # read waits on the operating system, so threads can overlap here.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {path: pool.submit(_try_hash, hash_func, path) for path in large}
            for path, future in futures.items():
                results[path] = future.result()
    return results


def _list_files(root: str, recursive: bool, skipped: list[str]) -> list[tuple[str, int]]:
    """Return (path, size) for every regular file under root.

    Symlinks and special files (pipes, sockets, devices) are ignored.
    Paths that raise OSError are added to `skipped` and the walk goes on.
    """
    files: list[tuple[str, int]] = []
    # A list of folders still to visit, instead of recursion, so a very deep
    # tree cannot hit Python's recursion limit.
    to_visit = [root]
    while to_visit:
        directory = to_visit.pop()
        try:
            # os.scandir gets each entry's type from the directory listing,
            # so checking for links and special files costs no extra calls.
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            # A link is not a second copy of the data. Reporting
                            # it as a duplicate could lead someone to delete the
                            # real file and keep only a broken link.
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                to_visit.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            files.append((entry.path, entry.stat(follow_symlinks=False).st_size))
                        # Anything else is a pipe, socket or device. Opening a
                        # named pipe can block forever, so we never touch them.
                    except OSError:
                        skipped.append(entry.path)
        except OSError:
            skipped.append(directory)
    return files


def _split_by_hash(
    groups: list[tuple[int, list[str]]],
    hash_func: HashFunc,
    workers: int,
    result: ScanResult,
) -> list[tuple[int, list[str]]]:
    """Hash every file in every group and split each group by digest.

    Each group is (size, paths). Only new groups with 2+ files are returned,
    because a file that matches nothing can no longer be a duplicate.
    """
    files = [(path, size) for size, paths in groups for path in paths]
    hashed = _hash_files(hash_func, files, workers)

    # The size is part of the key so that two files with the same first block
    # but different lengths can never end up in the same group.
    buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for path, size in files:
        digest, bytes_read = hashed[path]
        if digest is None:
            result.skipped.append(path)
            # It was counted when listed, but we never learned its content.
            result.total_files -= 1
            continue
        result.bytes_read += bytes_read
        if bytes_read == size:
            result.files_read_in_full += 1
        buckets[(size, digest)].append(path)

    return [(size, paths) for (size, _), paths in buckets.items() if len(paths) >= 2]


def scan(root: str, recursive: bool = True, workers: int = DEFAULT_WORKERS) -> ScanResult:
    """Find duplicate files under root and return the groups plus counters."""
    if not os.path.isdir(root):
        raise NotADirectoryError(f"not a directory: {root}")

    result = ScanResult()
    files = _list_files(root, recursive, result.skipped)
    result.total_files = len(files)

    # Stage 1: group by size. This needs no file reads at all.
    by_size: dict[int, list[str]] = defaultdict(list)
    for path, size in files:
        by_size[size].append(path)

    # Empty files all have the same (empty) content, so they are duplicates
    # of each other without being opened. The original groups them too,
    # because they share the MD5 of empty input.
    final_groups: list[list[str]] = []
    empty_files = by_size.pop(0, [])
    if len(empty_files) >= 2:
        final_groups.append(empty_files)

    same_size = [(size, paths) for size, paths in by_size.items() if len(paths) >= 2]

    # Stage 2: hash the first block of files that share a size.
    result.stage2_files = sum(len(paths) for _, paths in same_size)
    after_first_block = _split_by_hash(same_size, hash_first_block, workers, result)

    # A file no bigger than one block was read completely in stage 2,
    # so its first-block hash is already a hash of the whole file.
    needs_full_hash = []
    for size, paths in after_first_block:
        if size <= BLOCK_SIZE:
            final_groups.append(paths)
        else:
            needs_full_hash.append((size, paths))

    # Stage 3: full hash, only for files whose size and first block match.
    result.stage3_files = sum(len(paths) for _, paths in needs_full_hash)
    for _, paths in _split_by_hash(needs_full_hash, hash_whole_file, workers, result):
        final_groups.append(paths)

    # Sort inside and across groups so the output is the same on every run,
    # whatever order the operating system lists files in.
    result.groups = sorted(sorted(paths) for paths in final_groups)
    return result


def find_duplicates(root: str, recursive: bool = True, workers: int = DEFAULT_WORKERS) -> list[list[str]]:
    """Return groups of duplicate file paths under root.

    Each group is sorted, and the list of groups is sorted.
    Set recursive=False to look only at files directly inside root.
    """
    return scan(root, recursive, workers).groups
