# Duplicate finder refactor

## What this is

This is a rewrite of [pydupes](https://github.com/deehzee/pydupes), a small duplicate file finder written by Debajyoti Nandi in 2016 and released under the MIT license. The original file is kept unchanged in `original/duplicates.py` (checked byte for byte against the upstream repository with `diff`). The new code lives in `dupfinder/`.

The goal was to find exactly the same duplicates while reading far less data. The tests and the benchmark both check that the two versions report the same groups of files.

## Why the original is slow

The original computes an MD5 hash of every file it finds, then groups files by hash.

- `md5sum()` (lines 16 to 23) reads the whole file in 64 KB blocks. There is no way to stop early.
- `find_dup0()` calls it on every file at line 70, and `find_dup1()` does the same at line 104. Nothing filters the files first.

A file whose size no other file has cannot have a duplicate. In the mixed benchmark folder, 1,150 of the 1,533 files have a unique size. The original reads all 1,150 of them in full anyway.

There are two smaller problems. `find_dup0()` (lines 50 to 79) and `find_dup1()` (lines 82 to 113) are near copies of each other, one for a single folder and one for a whole tree. And there is no error handling. One file that is deleted during the scan, or that cannot be opened, stops the whole run with an exception. Symlinks are opened like normal files, and a named pipe makes the scan hang forever (I checked this: the original was still waiting when I killed it after 5 seconds).

## The new pipeline

`dupfinder/core.py` finds duplicates in three stages. Each stage only looks at files the previous stage could not rule out.

1. List every regular file and its size. Group the files by size. A file alone in its size group is dropped without ever being opened. Empty files are put in one group straight away, since they all have the same (empty) content.
2. For files that share a size, hash the first 1 MiB. Split each size group by this hash. A file that is 1 MiB or smaller has now been read completely, so its group is final.
3. For larger files whose size and first-block hash both match, hash the whole file in 1 MiB blocks. Split again by this hash.

Here is how many files of the mixed benchmark folder reached each stage:

| Step | Files | What happens to them |
|---|---:|---|
| Listed | 1,533 | Size read from the directory, no file opened |
| Unique size | 1,150 | Dropped, never opened |
| Empty | 5 | Grouped together, never opened |
| Stage 2 | 378 | First 1 MiB hashed. 319 of them are 1 MiB or smaller, so they are finished here. |
| Stage 3 | 39 | Hashed in full. 19 of them form 7 groups. The other 20 are trap files that match in size and first block but differ in the last byte. |

The result is 101 groups: 93 from stage 2, 7 from stage 3 and the group of empty files. The original found the same 101 groups by hashing all 1,533 files.

Stage 3 reads each file's first block a second time. Keeping the stage 2 hash object around would avoid that, but it would make the code harder to follow. On the mixed set the repeated reads add about 41 MB to the 240 MB total.

## Results

Measured with `python benchmarks/benchmark.py` on a cloud Linux VM (Intel Xeon at 2.10 GHz, 2 logical CPUs, Python 3.13.17). Each time is the median of 3 runs with a warm page cache, meaning the files were already in memory. Times moved by 10% or more between runs, and they will be different on other hardware. The full output is in `benchmarks/results.md`. A few exploratory numbers further down come from earlier runs on the same VM, before a restart moved it from a 2.80 GHz Xeon to this one.

| Scenario | Files | Size (MB) | Original (s) | New, 2 threads (s) | Speedup | MB hashed, original | MB hashed, new | Files fully hashed, original | Files fully hashed, new | Same groups |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| mixed | 1,533 | 847 | 1.32 | 0.24 | 5.6x | 847 | 240 | 1,533 | 358 | yes (101 groups) |
| small | 20,000 | 164 | 0.47 | 0.34 | 1.4x | 164 | 121 | 20,000 | 14,741 | yes (1954 groups) |
| large | 20 | 1,174 | 1.85 | 0.40 | 4.6x | 1,174 | 505 | 20 | 8 | yes (3 groups) |

The three folders are built by `benchmarks/make_dataset.py` from a fixed seed:

- mixed: nested folders, sizes from 1 KB to 20 MB, about 160 extra copies
- small: 20,000 files under 16 KB, so many files share a size by chance
- large: 20 files of 30 to 80 MB, three of them duplicated

Each folder also has trap files. Some pairs have the same size and the same first block but a different last byte, so only a full hash can tell them apart. Other pairs have the same size and a different first block.

### Bytes read is the number to look at

With a warm cache the files are already in memory, so the timings mostly measure hashing speed and Python overhead. On a slow disk or a cold cache, reading the data takes most of the time, and the bytes columns are the better guide there. The new code read 72% less data on the mixed set, 57% less on the large set and 26% less on the small set.

The benchmark also does one cold-cache run of the mixed set, emptying the page cache through `/proc/sys/vm/drop_caches` before each run. There the original took 1.73 s and the new code 0.35 s (5.0x). This VM's disk is virtual and fast, and the host may cache it too, so this is still far from a slow hard drive.

The small-file set is where the new code gains least. 14,741 of the 20,000 files share a size with another file by chance, so stage 1 can only drop about a quarter of them. The new code also pays for one `stat` call per file to learn its size. In a separate test, listing the 20,000 files with their sizes took 0.06 s, against 0.01 s for the plain `os.walk` the original uses. So it reads 26% less data but is only 1.4x faster.

### Hash choice

The original uses MD5. I measured MD5 and BLAKE2b on this machine, plus SHA-1 for comparison:

| Test | MiB/s |
|---|---:|
| md5, data already in memory | 687 |
| blake2b, data already in memory | 653 |
| sha1, data already in memory | 1,614 |
| md5, 1 MiB read loop, 64 MiB file, warm cache | 602 |
| md5, hashlib.file_digest, 64 MiB file, warm cache | 602 |
| blake2b, 1 MiB read loop, 64 MiB file, warm cache | 603 |
| blake2b, hashlib.file_digest, 64 MiB file, warm cache | 637 |

MD5 and BLAKE2b ran within about 5% of each other in every run, and each came out ahead in some runs. When reading from a file the two were level (602 and 603 MiB/s here). I picked BLAKE2b. This tool is for finding duplicates, not for security, so either would work for ordinary files. The difference is that people know how to build two different files with the same MD5 on purpose. A duplicate finder is often used to decide what to delete, so a hash that cannot be fooled that way is the safer default, and against MD5 it costs nothing.

SHA-1 is the awkward one. On the first host it was 3 to 20% faster than BLAKE2b, depending on the run. On the current host it ran at 1,614 MiB/s, more than twice as fast, because this CPU has hardware SHA instructions (the `sha_ni` flag in `/proc/cpuinfo`). I still left it out. SHA-1 has public collisions too, so choosing it would undo the reason for dropping MD5, and its speed depends on which CPU the code lands on.

The choice does not change the results. `HASH_NAME` in `dupfinder/core.py` sets the algorithm, and the main equivalence test runs once with BLAKE2b and once with MD5.

`hashlib.file_digest` was not reliably faster than a plain read loop. In this run it tied for MD5 and was 6% faster for BLAKE2b, and in an earlier run it was 6% slower for BLAKE2b (596 against 631 MiB/s). I kept the loop. It also counts the bytes read, which the benchmark needs, and it works on Python 3.10, where `file_digest` does not exist.

The original read in 64 KB blocks. I measured 64 KB and 1 MiB reads at the same speed here, and chose 1 MiB because it is also the size of the first block in stage 2.

### Threads

Large files are hashed in a `ThreadPoolExecutor`. This helps because hashlib releases the GIL while it hashes a large buffer, so two threads can hash at the same time.

| Scenario | 1 thread (s) | 2 threads (s) | Change | 2 threads, no size cutoff (s) |
|---|---:|---:|---:|---:|
| mixed | 0.39 | 0.24 | 1.65x | 0.22 |
| small | 0.37 | 0.34 | 1.08x | 1.04 |
| large | 0.76 | 0.40 | 1.89x | 0.41 |

My first version sent every file to the pool. That made the small-file set about three times slower (the last column: 1.04 s against 0.34 s). For a file of a few KB, handing it to another thread costs more than hashing it. So only files of 256 KB or more go to the pool now, and smaller files are hashed one by one in the main thread. With that cutoff, threads make little difference on the small set. They were 1.08x faster in the run above and 0.86x (slightly slower) in a later run, which is within the run-to-run noise on this VM. In earlier test runs, cutoffs of 64 KB and 256 KB gave the same times, and 1 MB was about 10% slower on the mixed set.

The default is `min(4, os.cpu_count())` threads, which is 2 on this VM. Four threads were no faster than two here, which makes sense with only 2 CPUs. I could not test a machine with more cores, so the cap of 4 is a cautious guess, and `--workers` changes it. With a warm cache this is mostly CPU work. On a real disk, threads might help more by keeping several reads in flight, or less if the disk has to seek between files. I have not measured either case.

## Correctness checks

`tests/test_equivalence.py` builds small folders in pytest's temporary directory and runs both versions on them. The groups must match the original's `md5_dict` entries with two or more paths, as sorted lists. The tests cover:

- nested folders with pairs, a triplicate and a group of empty files
- both trap kinds, plus a duplicate larger than one block so stage 3 runs
- five random trees with the block size patched down to 16 bytes, so stage 3 runs on almost every group
- the non-recursive mode against `find_dup0`
- the summary counts against what `dup_summary` would print
- the threaded path, with the size cutoff set to 0 so every file goes through the pool
- a check that files with a unique size and empty files are never opened
- the cases where the new code behaves differently on purpose (next section), including an unreadable file, a file deleted mid-scan and a folder that cannot be listed

To check that the tests can fail, I made stage 3 skip its work for a moment. 15 of the 23 tests failed, then all passed again once the change was undone. The benchmark also compares the groups from every timed run against the original and exits with an error if they differ.

All 23 tests pass on Python 3.13 and on Python 3.10.

## Differences from the original

Symlinks. The original opens a symlink like a normal file, so a link and its target are reported as duplicates of each other. Deleting the "duplicate" target would then leave a broken link. The new code skips symlinks. A broken symlink makes the original crash with `FileNotFoundError`. Symlinks to folders are not followed by either version. Hard links are different: they are real second names for the same file, and both versions report them as duplicates.

Special files. Named pipes, sockets and device files are skipped. The original opens them, and a named pipe makes it wait forever.

Errors. A file that cannot be read (for example `PermissionError`) or that disappears before it is hashed (`FileNotFoundError`) is skipped, added to a skipped list, and left out of the totals. The scan carries on. The original stops with the exception. Folders that cannot be listed are also counted as skipped; the original's `os.walk` ignores them silently.

API. The original returns two dictionaries, `md5_dict` and `dup_count`, with an MD5 for every file. The new `find_duplicates(root, recursive=True)` returns a list of groups, each a sorted list of paths, and the list of groups is sorted too. I did not write a wrapper for the old API. It promises a hash for every file, and the new code only hashes the files it has to, so producing that structure would mean reading every file again and losing the speedup. `scan()` returns the groups together with the counters.

Paths. In non-recursive mode the original's `find_dup0` returns bare file names. The new code always returns full paths, in both modes.

Summary. The CLI prints total, distinct and duplicate counts in the same layout as `dup_summary`. The original computes "distinct" as the number of keys in `md5_dict`, which needs a hash of every file. The new code computes it as total files minus extra copies. Every file is either the first of its contents or an extra copy, so this gives the same number, and a test checks that. The CLI adds a fourth line, "Skipped Files". The group listing does not show a hash, because empty files are grouped without being hashed.

Bad root. For a path that does not exist, the original's `find_dup1` returns empty results and `find_dup0` raises `StopIteration`. The new code raises `NotADirectoryError`, and the CLI prints an error.

Python version. The original supports Python 2. The new code needs Python 3.10 or newer and uses only the standard library.

## How to run

From the repository root:

```
python -m dupfinder ~/Downloads
python -m dupfinder ~/Downloads --no-recursive
python -m dupfinder ~/Downloads --json
python -m dupfinder ~/Downloads --workers 1
```

Example output:

```
3 identical files:
    ./notes/todo.txt
    ./notes/todo_old.txt
    ./todo.txt

2 identical files:
    ./photos/2024/beach_copy.jpg
    ./photos/beach.jpg

Total Files    : 7
Distinct Files : 4
Duplicate Files: 3
Skipped Files  : 0
```

From Python:

```python
from dupfinder import find_duplicates

for group in find_duplicates("/path/to/folder"):
    print(group)
```

Tests:

```
pip install -r requirements.txt
python -m pytest
```

Benchmark (builds each folder under `benchmarks/data/`, measures it, then deletes it; about 50 seconds and at most 1.2 GB of disk at a time on the test VM). The cold-cache run needs root on Linux and is skipped otherwise:

```
python benchmarks/benchmark.py
```

To build one folder and keep it for your own experiments:

```
python benchmarks/make_dataset.py mixed benchmarks/data/mixed
```
