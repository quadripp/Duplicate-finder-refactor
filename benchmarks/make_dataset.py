"""Generate seeded synthetic folders for the benchmark.

The same seed always gives the same files, so results can be repeated.

Scenarios:
  mixed  about 1,500 files, about 850 MB, nested folders, some duplicates
  small  20,000 files under 16 KB, many sharing a size, some duplicates
  large  20 files of 30-80 MB, a few duplicated

Every scenario also contains "trap" files that look like duplicates to a
lazy check but are not:
  - same size and same first 1 MiB, different last byte
    (only a full hash can tell them apart)
  - same size, different first block
    (the first-block hash tells them apart)

Usage:
  python benchmarks/make_dataset.py mixed benchmarks/data/mixed
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
import sys
from pathlib import Path

KIB = 1024
MIB = 1024 * 1024


class TreeBuilder:
    """Writes files into a random set of nested folders, using one seeded RNG."""

    def __init__(self, root: Path, seed: int, n_dirs: int) -> None:
        self.root = root
        self.rng = random.Random(seed)
        self.counter = 0
        self.total_bytes = 0
        self.originals: list[Path] = []  # files that copies can be made from
        self.dirs = self._make_dirs(n_dirs)

    def _make_dirs(self, n_dirs: int) -> list[Path]:
        """Create n_dirs folders, each inside the root or inside an earlier folder."""
        dirs = [self.root]
        for i in range(n_dirs):
            parent = self.rng.choice(dirs)
            child = parent / f"dir{i:03d}"
            child.mkdir(parents=True)
            dirs.append(child)
        return dirs

    def _next_path(self, prefix: str) -> Path:
        self.counter += 1
        return self.rng.choice(self.dirs) / f"{prefix}_{self.counter:05d}.bin"

    def write(self, data: bytes, prefix: str = "file") -> Path:
        path = self._next_path(prefix)
        path.write_bytes(data)
        self.total_bytes += len(data)
        return path

    def write_random(self, size: int, prefix: str = "file") -> Path:
        path = self.write(self.rng.randbytes(size), prefix)
        self.originals.append(path)
        return path

    def copy(self, source: Path) -> Path:
        path = self._next_path("copy")
        shutil.copyfile(source, path)
        self.total_bytes += source.stat().st_size
        return path

    def add_copies(self, n_originals: int, max_extra_copies: int) -> None:
        """Pick n_originals files and give each 1..max_extra_copies copies."""
        for source in self.rng.sample(self.originals, n_originals):
            for _ in range(self.rng.randint(1, max_extra_copies)):
                self.copy(source)

    def add_traps(self, n_pairs: int, min_size: int, max_size: int) -> None:
        """Add both kinds of trap pair described in the module docstring."""
        for _ in range(n_pairs):
            size = self.rng.randint(min_size, max_size)
            data = self.rng.randbytes(size)
            self.write(data, "trap_lastbyte")
            # Flip the bits of the last byte only.
            self.write(data[:-1] + bytes([data[-1] ^ 0xFF]), "trap_lastbyte")

            size = self.rng.randint(min_size, max_size)
            self.write(self.rng.randbytes(size), "trap_firstblock")
            self.write(self.rng.randbytes(size), "trap_firstblock")

    def log_uniform_size(self, low: int, high: int) -> int:
        """Random size where small files are more common than big ones, as on a real disk."""
        return int(math.exp(self.rng.uniform(math.log(low), math.log(high))))


def make_mixed(root: Path, seed: int = 1) -> TreeBuilder:
    """Roughly 1,500 files and 850 MB, like a downloads or documents folder."""
    tb = TreeBuilder(root, seed, n_dirs=40)
    for _ in range(1250):
        tb.write_random(tb.log_uniform_size(1 * KIB, 2 * MIB))
    for _ in range(20):
        tb.write_random(tb.rng.randint(8 * MIB, 20 * MIB), "big")
    # Same-size files with different content, like fixed-size thumbnails.
    for _ in range(60):
        tb.write(tb.rng.randbytes(256 * KIB), "fixed")
    tb.add_copies(n_originals=100, max_extra_copies=2)
    tb.add_traps(n_pairs=10, min_size=2 * MIB, max_size=4 * MIB)
    for _ in range(5):
        tb.write(b"", "empty")
    return tb


def make_small(root: Path, seed: int = 2) -> TreeBuilder:
    """20,000 files under 16 KB. Many share a size, so stage 1 removes fewer."""
    tb = TreeBuilder(root, seed, n_dirs=100)
    n_traps = 50  # pairs of each kind, so 200 files
    n_copies_target = 2000
    for _ in range(20_000 - n_copies_target - 4 * n_traps - 10):
        tb.write_random(tb.rng.randint(1, 16 * KIB - 1))
    tb.add_copies(n_originals=1500, max_extra_copies=1)
    # Top up to exactly 2,000 copies so the file count is a round number.
    while tb.counter < 20_000 - 4 * n_traps - 10:
        tb.copy(tb.rng.choice(tb.originals))
    # These traps fit inside one block, so the first-block hash already
    # covers the whole file and catches the different last byte.
    tb.add_traps(n_pairs=n_traps, min_size=1 * KIB, max_size=16 * KIB - 1)
    for _ in range(10):
        tb.write(b"", "empty")
    return tb


def make_large(root: Path, seed: int = 3) -> TreeBuilder:
    """20 files of 30-80 MB: 10 unique, 3 duplicated pairs, 2 trap pairs."""
    tb = TreeBuilder(root, seed, n_dirs=4)
    for _ in range(13):
        tb.write_random(tb.rng.randint(30 * MIB, 80 * MIB), "big")
    tb.add_copies(n_originals=3, max_extra_copies=1)
    tb.add_traps(n_pairs=1, min_size=30 * MIB, max_size=80 * MIB)
    return tb


SCENARIOS = {"mixed": make_mixed, "small": make_small, "large": make_large}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("outdir", type=Path)
    args = parser.parse_args()

    # Refuse to write into an existing folder so we never mix with real files.
    if args.outdir.exists():
        sys.exit(f"{args.outdir} already exists; remove it first")
    args.outdir.mkdir(parents=True)
    tb = SCENARIOS[args.scenario](args.outdir)
    print(f"{args.scenario}: {tb.counter} files, {tb.total_bytes / MIB:.0f} MiB in {args.outdir}")


if __name__ == "__main__":
    main()
