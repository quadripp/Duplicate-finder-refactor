# Benchmark results

- Python 3.13.17 on Linux 6.18.44-fc-v185
- CPU: Intel(R) Xeon(R) Processor @ 2.10GHz, 2 logical CPUs
- Median of 3 runs per cell, warm page cache unless the section says otherwise
- New code: hash blake2b, block size 1 MiB

## Original vs new

| Scenario | Files | Size (MB) | Original (s) | New, 2 threads (s) | Speedup | MB hashed, original | MB hashed, new | Files fully hashed, original | Files fully hashed, new | Same groups |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| mixed | 1,533 | 847 | 1.32 | 0.24 | 5.6x | 847 | 240 | 1,533 | 358 | yes (101 groups) |
| small | 20,000 | 164 | 0.47 | 0.34 | 1.4x | 164 | 121 | 20,000 | 14,741 | yes (1954 groups) |
| large | 20 | 1,174 | 1.85 | 0.40 | 4.6x | 1,174 | 505 | 20 | 8 | yes (3 groups) |

## Files left after each stage (new code)

| Scenario | Files listed | Stage 2: share a size | Stage 3: share size and first block | Duplicate groups |
|---|---:|---:|---:|---:|
| mixed | 1,533 | 378 | 39 | 101 |
| small | 20,000 | 14,741 | 0 | 1954 |
| large | 20 | 10 | 8 | 3 |

## Threads

| Scenario | 1 thread (s) | 2 threads (s) | Change | 2 threads, no size cutoff (s) |
|---|---:|---:|---:|---:|
| mixed | 0.39 | 0.24 | 1.65x | 0.22 |
| small | 0.37 | 0.34 | 1.08x | 1.04 |
| large | 0.76 | 0.40 | 1.89x | 0.41 |

## Cold cache

Page cache emptied (drop_caches) before every run. Median of 3 runs. This VM's disk is virtual, so it may itself be cached by the host.

| Scenario | Original (s) | New, 2 threads (s) | Speedup |
|---|---:|---:|---:|
| mixed | 1.73 | 0.35 | 5.0x |

## Hash speed

| Test | MiB/s |
|---|---:|
| md5, data already in memory | 687 |
| blake2b, data already in memory | 653 |
| sha1, data already in memory | 1,614 |
| md5, 1 MiB read loop, 64 MiB file, warm cache | 602 |
| md5, hashlib.file_digest, 64 MiB file, warm cache | 602 |
| blake2b, 1 MiB read loop, 64 MiB file, warm cache | 603 |
| blake2b, hashlib.file_digest, 64 MiB file, warm cache | 637 |
